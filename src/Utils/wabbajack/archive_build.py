from __future__ import annotations

import struct
import os
import tempfile
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import lz4.frame

from Utils.atomic_write import atomic_writer
from .manifest import type_name
from .hashes import XXHash
from .paths import WabbajackError, relative_path, source_path
from .diagnostics import emit


def check_archive_state(state, files):
    kind = type_name(state.get("$type"))
    if kind == "BSAState":
        if int(state.get("Version", 0)) not in {103, 104, 105}:
            raise WabbajackError(f"Unsupported BSA version: {state.get('Version')}")
    elif kind == "TES3State":
        if int(state.get("VersionNumber", 256)) != 256:
            raise WabbajackError("Unsupported TES3 archive version")
    elif kind == "BA2State":
        if str(state.get("Type")) not in {"GNRL", "DX10", "0", "1"}:
            raise WabbajackError(f"Unsupported BA2 type: {state.get('Type')}")
        if int(state.get("Version", 0)) not in {1, 2, 3, 7, 8}:
            raise WabbajackError(f"Unsupported BA2 version: {state.get('Version')}")
        if int(state.get("Compression", 0)) not in {0, 1, 3}:
            raise WabbajackError(f"Unsupported BA2 compression: {state.get('Compression')}")
    else:
        raise WabbajackError(f"Unsupported archive state: {kind}")
    paths, indexes = {}, {}
    for item in files:
        path = relative_path(item["Path"]).casefold()
        index = int(item["Index"])
        if path in paths:
            raise WabbajackError(f"Duplicate archive member: {item['Path']} conflicts with {paths[path]}")
        if index < 0:
            raise WabbajackError(f"Invalid archive member index {index}: {item['Path']}")
        folder = path.rpartition("/")[0] if kind == "BSAState" else ""
        key = folder, index
        if key in indexes:
            location = f" in folder {folder or '(archive root)'}" if kind == "BSAState" else ""
            raise WabbajackError(f"Duplicate archive member index {index}{location}: {indexes[key]} and {item['Path']}")
        paths[path] = item["Path"]
        indexes[key] = item["Path"]
        if kind == "BA2State" and str(state.get("Type")) in {"DX10", "1"}:
            if not 1 <= int(item["Width"]) <= 16384 or not 1 <= int(item["Height"]) <= 16384:
                raise WabbajackError("Invalid texture dimensions")
            if not 1 <= int(item["NumMips"]) <= 15 or not 1 <= len(item["Chunks"]) <= 255:
                raise WabbajackError("Invalid texture mip or chunk count")
            if int(item.get("ChunkHdrLen", 24)) != 24 or int(item.get("TileMode", 0)) != 0:
                raise WabbajackError("Unsupported texture chunk layout or tiled texture")
            from Utils.ba2.writer import _mip_byte_size
            mip_sizes = [_mip_byte_size(max(1, int(item["Width"]) >> m),
                         max(1, int(item["Height"]) >> m), int(item["PixelFormat"]))
                         for m in range(int(item["NumMips"]))]
            if any(size is None for size in mip_sizes):
                raise WabbajackError(f"Unsupported texture format: {item['PixelFormat']}")
            next_mip = 0
            for chunk in item["Chunks"]:
                if not 0 <= int(chunk["StartMip"]) <= int(chunk["EndMip"]) < int(item["NumMips"]):
                    raise WabbajackError("Texture chunk mip range is invalid")
                if int(chunk["StartMip"]) != next_mip or int(chunk["FullSz"]) <= 0:
                    raise WabbajackError("Texture chunks have gaps, overlap, or invalid sizes")
                end = int(chunk["EndMip"]) + 1
                expected = sum(mip_sizes[next_mip:end]) * (6 if item.get("IsCubeMap") else 1)
                if int(chunk["FullSz"]) != expected:
                    raise WabbajackError("Texture chunk size differs from its declared mip range")
                next_mip = end
                if int(state.get("Compression", 0)) == 3 and int(chunk["FullSz"]) > 256 * 1024 * 1024:
                    raise WabbajackError("LZ4 texture chunk exceeds the 256 MiB conversion budget")
            if next_mip != int(item["NumMips"]):
                raise WabbajackError("Texture chunks do not cover every mip level")


def _check(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")


def _payload(source, out, compressed, stop, *, lz4=False, limit=None, digest=None):
    before, count = out.tell(), 0
    encoder = (lz4_frame_encoder() if lz4 else zlib.compressobj(9)) if compressed else None
    if lz4 and encoder:
        out.write(encoder.begin())
    while limit is None or count < limit:
        _check(stop)
        data = source.read(min(1024 * 1024, limit - count) if limit is not None else 1024 * 1024)
        if not data:
            break
        count += len(data)
        if digest is not None:
            digest.update(data)
        out.write(encoder.compress(data) if encoder else data)
    if limit is not None and count != limit:
        raise WabbajackError("Archive source ended before its declared size")
    if encoder:
        out.write(encoder.flush())
    return out.tell() - before, count


def lz4_frame_encoder():
    return lz4.frame.LZ4FrameCompressor(compression_level=9)


class _BuildStop:
    def __init__(self, stop):
        self.stop = stop
        self.failed = threading.Event()

    def is_set(self):
        return self.failed.is_set() or (self.stop is not None and self.stop.is_set())


def _stamp(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _write_members(out, sources, files, prepare, stop, memory_budget, max_workers,
                   memory_cost=None, *, parallel=None):
    stop = _BuildStop(stop)
    pending, reserved = deque(), 0
    upcoming = iter(files)
    item = next(upcoming, None)
    spool_limit = 256 * 1024 * 1024

    def run(item, target):
        path, stamp = sources[relative_path(item["Path"])]
        cost = memory_cost(item) if memory_cost else 32 * 1024 * 1024
        memory_budget.acquire(cost, cancel=stop)
        try:
            _check(stop)
            if _stamp(path) != stamp:
                raise WabbajackError(f"Archive source changed: {item['Path']}")
            result = prepare(item, path, stamp[2], target, stop)
            if _stamp(path) != stamp:
                raise WabbajackError(f"Archive source changed: {item['Path']}")
            return result
        finally:
            memory_budget.release(cost)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wabbajack-build") as pool:
        try:
            while item is not None or pending:
                _check(stop)
                while item is not None and len(pending) < max_workers and max_workers > 1:
                    if parallel is not None and not parallel(item):
                        break
                    size = sources[relative_path(item["Path"])][1][2]
                    cost = size + size // 128 + 1024 * 1024
                    if reserved + cost > spool_limit:
                        break
                    spool = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024,
                                                         dir=Path(out.name).parent)
                    try:
                        future = pool.submit(run, item, spool)
                    except BaseException:
                        spool.close()
                        raise
                    pending.append((item, future, spool, cost))
                    reserved += cost
                    item = next(upcoming, None)
                if pending:
                    current, future, spool, cost = pending[0]
                    result = future.result()
                    spool.seek(0)
                    offset = out.tell()
                    _payload(spool, out, False, stop)
                    spool.close()
                    pending.popleft()
                    reserved -= cost
                else:
                    current, offset = item, out.tell()
                    result = run(item, out)
                    item = next(upcoming, None)
                yield current, offset, result
        finally:
            stop.failed.set()
            for _, future, _, _ in pending:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            for _, _, spool, _ in pending:
                spool.close()


def rebuild_archive(target: Path, root: Path, state: dict, files: list,
                    stop=None, progress=None, log=None, *, memory_budget=None, max_workers=2):
    started = time.monotonic()
    check_archive_state(state, files)
    files = sorted(files, key=lambda f: int(f["Index"]))
    sources = {}
    for item in files:
        _check(stop)
        path = source_path(root, item["Path"])
        if not path.is_file():
            raise WabbajackError(f"Missing archive source: {item['Path']}")
        sources[relative_path(item["Path"])] = path, _stamp(path)
    max_workers = max(1, min(int(max_workers), os.cpu_count() or 1, 4))
    if memory_budget is None:
        from Utils.archives.budget import ExtractionMemoryBudget
        memory_budget = ExtractionMemoryBudget(max_workers=max_workers,
                                               max_budget_bytes=512 * 1024 * 1024)
    kind = type_name(state["$type"])
    emit(log, "archive.rebuild.started", target=target, source_root=root,
         kind=kind, version=state.get("Version", state.get("VersionNumber")),
         archive_type=state.get("Type"), compression=state.get("Compression"),
         archive_flags=state.get("ArchiveFlags"), file_flags=state.get("FileFlags"),
         members=len(files), source_bytes=sum(stamp[2] for _, stamp in sources.values()),
         preparation_workers=max_workers)
    expected_hashes = {}
    with atomic_writer(target, "w+b", encoding=None) as out:
        preparing = (lambda cur, total: progress(cur, total * 2)) if progress else None
        if kind == "TES3State":
            _tes3(out, sources, state, files, stop, expected_hashes)
        elif kind == "BSAState":
            _bsa(out, sources, state, files, stop, expected_hashes,
                 memory_budget, max_workers, preparing)
        else:
            _ba2(out, sources, state, files, stop, expected_hashes,
                 memory_budget, max_workers, preparing)
        out.flush()
        verification_started = time.monotonic()
        build_seconds = verification_started - started
        from .archive_io import verify_archive
        verify_archive(Path(out.name), root, files, stop,
            (lambda cur, total: progress(total + cur, total * 2)) if progress else None,
            expected_hashes=expected_hashes, memory_budget=memory_budget)
        for name, (path, stamp) in sources.items():
            _check(stop)
            if _stamp(path) != stamp:
                raise WabbajackError(f"Archive source changed: {name}")
        if progress:
            progress(len(files), len(files))
    emit(log, "archive.rebuild.completed", target=target,
         kind=kind, members=len(files), bytes=target.stat().st_size,
         preparation_workers=max_workers, build_seconds=round(build_seconds, 3),
         verification_seconds=round(time.monotonic() - verification_started, 3),
         source_hashes_reused=len(expected_hashes),
         elapsed_seconds=round(time.monotonic() - started, 3))


def _tes3(out, sources, state, files, stop, expected_hashes):
    names = [relative_path(f["Path"]).replace("/", "\\").encode("cp1252") + b"\0" for f in files]
    count = len(files)
    hash_offset = count * 12 + sum(map(len, names))
    out.write(struct.pack("<III", 256, hash_offset, count))
    offset = 0
    for f in files:
        size = sources[relative_path(f["Path"])][1][2]
        out.write(struct.pack("<II", size, offset))
        offset += size
    offset = 0
    for name in names:
        out.write(struct.pack("<I", offset))
        offset += len(name)
    for name in names:
        out.write(name)
    for f in files:
        out.write(struct.pack("<II", int(f["Hash1"]), int(f["Hash2"])))
    for f in files:
        path, stamp = sources[relative_path(f["Path"])]
        digest = XXHash()
        with path.open("rb") as source:
            _, size = _payload(source, out, False, stop, limit=stamp[2], digest=digest)
        expected_hashes[relative_path(f["Path"]).casefold()] = size, digest.digest()


def _bsa(out, sources, state, files, stop, expected_hashes, memory_budget, max_workers,
         progress=None):
    from Utils.bsa.writer import tes4_hash_file, tes4_hash_folder
    version, flags = int(state["Version"]), int(state["ArchiveFlags"])
    folders = {}
    for item in files:
        path = relative_path(item["Path"])
        folder, _, leaf = path.rpartition("/")
        folders.setdefault(folder, []).append((leaf, item))
    folders = list(folders.items())
    folder_names = sum(len(name.encode("cp1252")) + 1 for name, _ in folders) if flags & 1 else 0
    file_names = sum(len(leaf.encode("cp1252")) + 1 for _, group in folders for leaf, _ in group) if flags & 2 else 0
    out.write(struct.pack("<4s8I", b"BSA\0", version, 36, flags, len(folders), len(files),
                          folder_names, file_names, int(state.get("FileFlags", 0))))
    record_size = 24 if version == 105 else 16
    out.write(bytes(record_size * len(folders)))
    folder_records, records = [], []
    for name, group in folders:
        block = out.tell()
        if flags & 1:
            encoded = name.replace("/", "\\").encode("cp1252") + b"\0"
            if len(encoded) > 255:
                raise WabbajackError("BSA folder name is too long")
            out.write(bytes([len(encoded)]) + encoded)
        folder_records.append((tes4_hash_folder(name.replace("/", "\\")), len(group), block + file_names))
        for leaf, item in group:
            records.append((out.tell(), leaf, item))
            out.write(bytes(16))
    if flags & 2:
        for _, leaf, _ in records:
            out.write(leaf.encode("cp1252") + b"\0")
    def compressed(item):
        return bool(flags & 4) != bool(item.get("FlipCompression", False))

    def prepare(item, source, size, target, stopping):
        if flags & 0x100 and version >= 104:
            name = relative_path(item["Path"]).replace("/", "\\").encode("cp1252")
            if len(name) > 255:
                raise WabbajackError("BSA embedded filename is too long")
            target.write(bytes([len(name)]) + name)
        packing = compressed(item)
        if packing:
            target.write(struct.pack("<I", size))
        digest = XXHash()
        with source.open("rb") as stream:
            _, full = _payload(stream, target, packing, stopping, lz4=version == 105,
                               limit=size, digest=digest)
        return full, digest.digest()

    ordered = [item for _, _, item in records]
    with closing(_write_members(out, sources, ordered, prepare, stop, memory_budget,
                                max_workers, parallel=compressed)) as prepared:
        for index, ((position, leaf, _), (item, offset, digest)) in enumerate(zip(records, prepared)):
            end = out.tell()
            size = end - offset
            if size >= 1 << 30:
                raise WabbajackError("BSA member exceeds format size limit")
            flip = bool(item.get("FlipCompression", False))
            out.seek(position)
            out.write(struct.pack("<QII", tes4_hash_file(leaf), size | (int(flip) << 30), offset))
            out.seek(end)
            expected_hashes[relative_path(item["Path"]).casefold()] = digest
            if progress:
                progress(index + 1, len(records))
    end = out.tell()
    out.seek(36)
    for hash_value, count, offset in folder_records:
        out.write(struct.pack("<QIIQ", hash_value, count, 0, offset) if version == 105
                  else struct.pack("<QII", hash_value, count, offset))
    out.seek(end)


def _ba2(out, sources, state, files, stop, expected_hashes, memory_budget, max_workers,
         progress=None):
    from Utils.ba2.writer import _parse_dds, ba2_hash
    version = int(state["Version"])
    texture = str(state.get("Type")) in {"DX10", "1"}
    out.write(struct.pack("<4sI4sIQ", b"BTDX", version, b"DX10" if texture else b"GNRL", len(files), 0))
    if version in {2, 3}:
        out.write(struct.pack("<II", int(state.get("Unknown1", 0)), int(state.get("Unknown2", 0))))
    if version == 3:
        out.write(struct.pack("<I", int(state.get("Compression", 0))))
    positions = []
    for item in files:
        positions.append(out.tell())
        out.write(bytes(24 + 24 * len(item["Chunks"]) if texture else 36))
    def prepare(item, source, size, target, stopping):
        digest = XXHash()
        start = target.tell()
        if not texture:
            compressed = bool(item.get("Compressed"))
            with source.open("rb") as stream:
                _, full = _payload(stream, target, compressed, stopping, limit=size, digest=digest)
            return full, digest.digest(), None
        else:
            rel = relative_path(item["Path"])
            with source.open("rb") as stream:
                import mmap
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    info = _parse_dds(mapped)
                if (info["width"] != int(item["Width"]) or info["height"] != int(item["Height"])
                        or info["dxgi_format"] != int(item["PixelFormat"])
                        or info["mip_count"] != int(item["NumMips"])):
                    raise WabbajackError(f"Texture source metadata differs: {rel}")
                stream.seek(int(info["pixel_data_offset"]))
                chunks = []
                full_size = 0
                for chunk in item["Chunks"]:
                    _check(stopping)
                    offset = target.tell() - start
                    count = int(chunk["FullSz"])
                    compressed = bool(chunk.get("Compressed"))
                    if compressed and int(state.get("Compression", 0)) == 3:
                        import lz4.block
                        if count > 256 * 1024 * 1024:
                            raise WabbajackError("LZ4 texture chunk exceeds the 256 MiB conversion budget")
                        data = stream.read(count)
                        if len(data) != count:
                            raise WabbajackError("Truncated texture chunk")
                        digest.update(data)
                        encoded = lz4.block.compress(data, mode="high_compression", compression=12, store_size=False)
                        _check(stopping)
                        target.write(encoded)
                        packed, full = len(encoded), len(data)
                        del data, encoded
                    else:
                        packed, full = _payload(stream, target, compressed, stopping,
                                                limit=count, digest=digest)
                    full_size += full
                    chunks.append((offset, packed if compressed else 0, full,
                        int(chunk["StartMip"]), int(chunk["EndMip"]), int(chunk.get("Align", 0xBAADF00D))))
                if stream.read(1):
                    raise WabbajackError(f"Texture chunks do not cover {rel}")
            return full_size, digest.digest(), chunks

    def memory_cost(item):
        chunk = max((int(c["FullSz"]) for c in item.get("Chunks", []) if c.get("Compressed")), default=0)
        return 32 * 1024 * 1024 + (chunk * 2 if int(state.get("Compression", 0)) == 3 else 0)

    def compressed(item):
        if texture:
            return any(chunk.get("Compressed") for chunk in item["Chunks"])
        return bool(item.get("Compressed"))

    with closing(_write_members(out, sources, files, prepare, stop, memory_budget,
                                max_workers, memory_cost, parallel=compressed)) as prepared:
        for index, (position, (item, offset, (full, digest, chunks))) in enumerate(zip(positions, prepared)):
            rel = relative_path(item["Path"]).replace("/", "\\")
            folder, _, leaf = rel.rpartition("\\")
            stem, dot, ext = leaf.rpartition(".")
            name_hash = int(item.get("NameHash", ba2_hash(stem if dot else leaf)))
            dir_hash = int(item.get("DirHash", ba2_hash(folder)))
            extension = str(item.get("Extension", ext)).encode("ascii")[:4].ljust(4, b"\0")
            end = out.tell()
            if not texture:
                record = struct.pack("<I4sIIQIII", name_hash, extension, dir_hash,
                    int(item.get("Flags", 0)), offset, end - offset if item.get("Compressed") else 0,
                    full, int(item.get("Align", 0xBAADF00D)))
            else:
                record = struct.pack("<I4sIBBHHHBBBB", name_hash, extension, dir_hash,
                    int(item.get("Unk8", 0)), len(chunks), int(item.get("ChunkHdrLen", 24)),
                    int(item["Height"]), int(item["Width"]), int(item["NumMips"]),
                    int(item["PixelFormat"]), int(item.get("IsCubeMap", 0)), int(item.get("TileMode", 0)))
                record += b"".join(struct.pack("<QIIHHI", offset + chunk[0], *chunk[1:]) for chunk in chunks)
            out.seek(position)
            out.write(record)
            out.seek(end)
            expected_hashes[relative_path(item["Path"]).casefold()] = full, digest
            if progress:
                progress(index + 1, len(files))
    names_at = out.tell() if state.get("HasNameTable", True) else 0
    if names_at:
        for item in files:
            name = relative_path(item["Path"]).replace("/", "\\").encode("utf-8")
            out.write(struct.pack("<H", len(name)) + name)
    end = out.tell()
    out.seek(16)
    out.write(struct.pack("<Q", names_at))
    out.seek(end)
