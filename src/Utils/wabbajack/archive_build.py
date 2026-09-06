from __future__ import annotations

import struct
import zlib
from pathlib import Path

import lz4.frame

from Utils.atomic_write import atomic_writer
from .manifest import type_name
from .paths import WabbajackError, relative_path, source_path


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
    paths, indexes = set(), set()
    for item in files:
        path = relative_path(item["Path"]).casefold()
        index = int(item["Index"])
        if path in paths or index in indexes or index < 0:
            raise WabbajackError("Conflicting archive member or index")
        paths.add(path)
        indexes.add(index)
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


def _payload(source, out, compressed, stop, *, lz4=False, limit=None):
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
        out.write(encoder.compress(data) if encoder else data)
    if limit is not None and count != limit:
        raise WabbajackError("Archive source ended before its declared size")
    if encoder:
        out.write(encoder.flush())
    return out.tell() - before, count


def lz4_frame_encoder():
    return lz4.frame.LZ4FrameCompressor(compression_level=9)


def rebuild_archive(target: Path, root: Path, state: dict, files: list,
                    stop=None, progress=None):
    check_archive_state(state, files)
    files = sorted(files, key=lambda f: int(f["Index"]))
    for item in files:
        if not source_path(root, item["Path"]).is_file():
            raise WabbajackError(f"Missing archive source: {item['Path']}")
    kind = type_name(state["$type"])
    with atomic_writer(target, "w+b", encoding=None) as out:
        if kind == "TES3State":
            _tes3(out, root, state, files, stop)
        elif kind == "BSAState":
            _bsa(out, root, state, files, stop)
        else:
            _ba2(out, root, state, files, stop)
        out.flush()
        from .archive_io import verify_archive
        verify_archive(Path(out.name), root, files, stop)
        if progress:
            progress(len(files), len(files))


def _tes3(out, root, state, files, stop):
    names = [relative_path(f["Path"]).replace("/", "\\").encode("cp1252") + b"\0" for f in files]
    count = len(files)
    hash_offset = count * 12 + sum(map(len, names))
    out.write(struct.pack("<III", 256, hash_offset, count))
    offset = 0
    for f in files:
        size = source_path(root, f["Path"]).stat().st_size
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
        with source_path(root, f["Path"]).open("rb") as source:
            _payload(source, out, False, stop)


def _bsa(out, root, state, files, stop):
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
    for position, leaf, item in records:
        _check(stop)
        source = source_path(root, item["Path"])
        offset = out.tell()
        if flags & 0x100 and version >= 104:
            name = relative_path(item["Path"]).replace("/", "\\").encode("cp1252")
            if len(name) > 255:
                raise WabbajackError("BSA embedded filename is too long")
            out.write(bytes([len(name)]) + name)
        flip = bool(item.get("FlipCompression", False))
        compressed = bool(flags & 4) != flip
        if compressed:
            out.write(struct.pack("<I", source.stat().st_size))
        with source.open("rb") as stream:
            _payload(stream, out, compressed, stop, lz4=version == 105)
        end = out.tell()
        size = end - offset
        if size >= 1 << 30:
            raise WabbajackError("BSA member exceeds format size limit")
        out.seek(position)
        out.write(struct.pack("<QII", tes4_hash_file(leaf), size | (int(flip) << 30), offset))
        out.seek(end)
    end = out.tell()
    out.seek(36)
    for hash_value, count, offset in folder_records:
        out.write(struct.pack("<QIIQ", hash_value, count, 0, offset) if version == 105
                  else struct.pack("<QII", hash_value, count, offset))
    out.seek(end)


def _ba2(out, root, state, files, stop):
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
    for position, item in zip(positions, files):
        _check(stop)
        rel = relative_path(item["Path"]).replace("/", "\\")
        folder, _, leaf = rel.rpartition("\\")
        stem, dot, ext = leaf.rpartition(".")
        name_hash = int(item.get("NameHash", ba2_hash(stem if dot else leaf)))
        dir_hash = int(item.get("DirHash", ba2_hash(folder)))
        extension = str(item.get("Extension", ext)).encode("ascii")[:4].ljust(4, b"\0")
        source = source_path(root, item["Path"])
        if not texture:
            offset = out.tell()
            compressed = bool(item.get("Compressed"))
            with source.open("rb") as stream:
                packed, full = _payload(stream, out, compressed, stop)
            record = struct.pack("<I4sIIQIII", name_hash, extension, dir_hash,
                                 int(item.get("Flags", 0)), offset, packed if compressed else 0,
                                 full, int(item.get("Align", 0xBAADF00D)))
        else:
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
                for chunk in item["Chunks"]:
                    offset = out.tell()
                    count = int(chunk["FullSz"])
                    compressed = bool(chunk.get("Compressed"))
                    if compressed and int(state.get("Compression", 0)) == 3:
                        import lz4.block
                        if count > 256 * 1024 * 1024:
                            raise WabbajackError("LZ4 texture chunk exceeds the 256 MiB conversion budget")
                        data = stream.read(count)
                        if len(data) != count:
                            raise WabbajackError("Truncated texture chunk")
                        encoded = lz4.block.compress(data, mode="high_compression", compression=12, store_size=False)
                        out.write(encoded)
                        packed, full = len(encoded), len(data)
                    else:
                        packed, full = _payload(stream, out, compressed, stop, limit=count)
                    chunks.append(struct.pack("<QIIHHI", offset, packed if compressed else 0,
                        full, int(chunk["StartMip"]), int(chunk["EndMip"]), int(chunk.get("Align", 0xBAADF00D))))
                if stream.read(1):
                    raise WabbajackError(f"Texture chunks do not cover {rel}")
            record = struct.pack("<I4sIBBHHHBBBB", name_hash, extension, dir_hash,
                int(item.get("Unk8", 0)), len(chunks), int(item.get("ChunkHdrLen", 24)),
                int(item["Height"]), int(item["Width"]), int(item["NumMips"]),
                int(item["PixelFormat"]), int(item.get("IsCubeMap", 0)), int(item.get("TileMode", 0))) + b"".join(chunks)
        end = out.tell()
        out.seek(position)
        out.write(record)
        out.seek(end)
    names_at = out.tell() if state.get("HasNameTable", True) else 0
    if names_at:
        for item in files:
            name = relative_path(item["Path"]).replace("/", "\\").encode("utf-8")
            out.write(struct.pack("<H", len(name)) + name)
    end = out.tell()
    out.seek(16)
    out.write(struct.pack("<Q", names_at))
    out.seek(end)
