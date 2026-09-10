from __future__ import annotations

import codecs
import struct
import zlib
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .hashes import XXHash
from .paths import WabbajackError, relative_path, within

BLOCK = 1024 * 1024


def utf8_chunks(stream, stop=None, *, errors="strict"):
    decoder = codecs.getincrementaldecoder("utf-8-sig")(errors=errors)
    while True:
        if stop is not None and stop.is_set():
            raise InterruptedError("Operation stopped")
        data = stream.read(BLOCK)
        if not data:
            break
        text = decoder.decode(data)
        if text:
            yield text
    text = decoder.decode(b"", final=True)
    if text:
        yield text


def _read(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise WabbajackError("Truncated archive metadata")
    return data


def records(path, file_states=None, *, mpi_paths=False, allow_case_variants=False,
            allow_hash_only=False):
    """Optional hash-only IDs support validation of archives without stored paths."""
    with Path(path).open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(0)
        magic = _read(stream, 4)
        stream.seek(0)
        result = []
        if magic == b"BTDX":
            from Utils.ba2.extract import _parse_records, _make_dds_header
            rows, names = _parse_records(stream, [f["Path"] for f in file_states] if file_states is not None else None)
            for name, row in zip(names, rows):
                header = b""
                if row["type"] == "DX10":
                    chunks = row["chunks"]
                    header = _make_dds_header(height=row["height"], width=row["width"],
                        mip_count=row["num_mips"], dxgi_format=row["dxgi_format"],
                        cube_map=bool(row.get("cube_map")), legacy=True)
                    if row.get("cube_map"):
                        header = bytearray(header)
                        struct.pack_into("<I", header, 108, 0x1000 | (0x400008 if row["num_mips"] > 1 else 0))
                        struct.pack_into("<I", header, 112, 0)
                        header = bytes(header)
                else:
                    chunks = [row]
                segments = [(c["data_offset"], c["packed_size"] or c["unpacked_size"],
                             c["unpacked_size"], ("lz4block" if row.get("compression") == 3 else "zlib")
                             if c["packed_size"] else "") for c in chunks]
                result.append((name, header, segments))
        elif magic == b"BSA\0":
            from Utils.bsa.extract import _parse_toc
            header = _read(stream, 36)
            _, version, toc, flags, folders, files, folder_names, file_names, _ = struct.unpack("<4s8I", header)
            if version not in (103, 104, 105):
                raise WabbajackError(f"Unsupported BSA version: {version}")
            if files > 1_000_000 or folders > files or file_names + folder_names > size:
                raise WabbajackError("Invalid BSA metadata sizes")
            stream.seek(0)
            if flags & 3 == 3:
                info, rows = _parse_toc(stream)
            else:
                if file_states is None and not allow_hash_only:
                    raise WabbajackError("BSA member paths are absent from this source archive")
                from Utils.bsa.writer import tes4_hash_file, tes4_hash_folder
                by_hash = None
                if file_states is not None:
                    by_hash = {(tes4_hash_folder(relative_path(f["Path"]).rpartition("/")[0].replace("/", "\\")),
                                tes4_hash_file(relative_path(f["Path"]).rsplit("/", 1)[-1])): f["Path"] for f in file_states}
                stream.seek(toc)
                groups = []
                for _ in range(folders):
                    raw = _read(stream, 24 if version == 105 else 16)
                    groups.append(struct.unpack_from("<QI", raw))
                if sum(count for _, count in groups) != files:
                    raise WabbajackError("BSA member count differs from its header")
                rows = []
                for folder_hash, count in groups:
                    if flags & 1:
                        _read(stream, _read(stream, 1)[0])
                    for _ in range(count):
                        file_hash, field, offset = struct.unpack("<QII", _read(stream, 16))
                        name = (by_hash.get((folder_hash, file_hash)) if by_hash is not None
                                else f"{folder_hash:016x}/{file_hash:016x}")
                        if name is None:
                            raise WabbajackError("Reconstructed BSA member hash differs")
                        rows.append((name, field, offset))
                info = {"archive_compressed": bool(flags & 4), "version": version,
                        "embed_filenames": version >= 104 and bool(flags & 0x100)}
            for name, field, offset in rows:
                packed = field & 0x3fffffff
                full = packed
                compressed = info["archive_compressed"] != bool(field & 0x40000000)
                stream.seek(offset)
                if info["embed_filenames"]:
                    length = _read(stream, 1)[0] + 1
                    offset += length
                    packed -= length
                    stream.seek(offset)
                if compressed:
                    full = struct.unpack("<I", _read(stream, 4))[0]
                    packed -= 4
                    offset += 4
                else:
                    full = packed
                result.append((name, b"", [(offset, packed, full,
                    ("lz4frame" if info["version"] == 105 else "zlib") if compressed else "")]))
        elif magic == struct.pack("<I", 256):
            _, hashes, count = struct.unpack("<III", _read(stream, 12))
            if count > 1_000_000 or hashes < count * 12 or hashes + 12 + count * 8 > size:
                raise WabbajackError("Invalid TES3 archive metadata")
            rows = [struct.unpack("<II", _read(stream, 8)) for _ in range(count)]
            offsets = [struct.unpack("<I", _read(stream, 4))[0] for _ in range(count)]
            names = _read(stream, hashes - count * 12)
            data_start = 12 + hashes + count * 8
            for (full, offset), name_offset in zip(rows, offsets):
                end = names.find(b"\0", name_offset)
                if name_offset >= len(names) or end < 0:
                    raise WabbajackError("Invalid TES3 filename offset")
                result.append((names[name_offset:end].decode("cp1252"), b"",
                               [(data_start + offset, full, full, "")]))
        else:
            raise WabbajackError("Unrecognized Bethesda archive")
        seen = set()
        if mpi_paths:
            result = [(name[2:] if name.startswith("./") else name, header, segments)
                      for name, header, segments in result]
        for name, _, segments in result:
            name = relative_path(name)
            if not allow_case_variants:
                name = name.casefold()
            if name in seen:
                raise WabbajackError("Duplicate archive member")
            seen.add(name)
            for offset, packed, full, compression in segments:
                if min(offset, packed, full) < 0 or offset + packed > size:
                    raise WabbajackError("Archive member exceeds file boundaries")
                if compression == "lz4block" and max(packed, full) > 256 * BLOCK:
                    raise WabbajackError("LZ4 texture chunk exceeds the 256 MiB conversion budget")
        return result


def read_member(stream, record, write, stop=None):
    _, header, segments = record
    write(header)
    for offset, packed, full, compression in segments:
        if stop is not None and stop.is_set():
            raise InterruptedError("Archive processing stopped")
        if packed == full == 0:
            continue
        stream.seek(offset)
        if compression == "lz4block":
            import lz4.block
            decoded = lz4.block.decompress(_read(stream, packed), uncompressed_size=full)
            if len(decoded) != full:
                raise WabbajackError("Archive member failed decompression validation")
            if stop is not None and stop.is_set():
                raise InterruptedError("Archive processing stopped")
            write(decoded)
            del decoded
            continue
        if compression == "zlib":
            decoder = zlib.decompressobj()
        elif compression == "lz4frame":
            import lz4.frame
            decoder = lz4.frame.LZ4FrameDecompressor()
        else:
            decoder = None
        count, remaining = 0, packed
        while remaining:
            if stop is not None and stop.is_set():
                raise InterruptedError("Archive processing stopped")
            data = _read(stream, min(BLOCK, remaining))
            remaining -= len(data)
            while True:
                decoded = decoder.decompress(data, max_length=BLOCK) if decoder else data
                count += len(decoded)
                if count > full:
                    raise WabbajackError("Archive member exceeds declared output size")
                write(decoded)
                if compression == "zlib" and decoder.unconsumed_tail:
                    data = decoder.unconsumed_tail
                elif compression == "lz4frame" and not decoder.needs_input and not decoder.eof:
                    data = b""
                else:
                    break
        if count != full or (decoder and (not decoder.eof or decoder.unused_data)):
            raise WabbajackError("Archive member failed decompression validation")


def select_bethesda(source, members=None, aliases=None):
    rows = records(source, allow_case_variants=True)
    with Path(source).open("rb") as stream:
        legacy_names = ((members is not None or aliases is not None)
                        and stream.read(8) == b"BSA\0\x67\0\0\0")
    selected = []
    for row in rows:
        name = relative_path(row[0])
        alias = name
        if legacy_names and "+" in name:
            alias = "/".join(part.encode("latin-1").decode("utf-7", "ignore")
                             for part in name.split("/"))
        if members is not None and not {name.casefold(), alias.casefold()} & members:
            continue
        selected.append(row)
        if aliases is not None and alias.casefold() != name.casefold():
            aliases.setdefault(alias.casefold(), []).append(name)
    return selected


def extract_bethesda(source, root, stop=None, progress=None, *, excluded_paths=frozenset(), aliases=None,
                     members=None, selected_records=None):
    rows = select_bethesda(source, members, aliases) if selected_records is None else selected_records
    rows = [row for row in rows if relative_path(row[0]).casefold() not in excluded_paths]
    total = sum(len(header) + sum(c[2] for c in segments) for _, header, segments in rows)
    completed = 0
    with Path(source).open("rb") as stream:
        for row in rows:
            if stop is not None and stop.is_set():
                raise InterruptedError("Archive processing stopped")
            target = within(root, row[0])
            with atomic_writer(target, "wb", encoding=None) as out:
                def write(data):
                    nonlocal completed
                    out.write(data)
                    completed += len(data)
                    if progress:
                        progress(completed, total)
                try:
                    read_member(stream, row, write, stop)
                except WabbajackError as exc:
                    raise WabbajackError(f"{Path(source).name}: {row[0]}: {exc}") from exc


def verify_archive(path, root, files, stop=None, progress=None, *, expected_hashes=None,
                   memory_budget=None):
    from .paths import source_path
    rows = records(path, files)
    if len(rows) != len(files):
        raise WabbajackError("Reconstructed archive member count differs")
    expected = {relative_path(f["Path"]).casefold() for f in files}
    if expected_hashes is not None and set(expected_hashes) != expected:
        raise WabbajackError("Reconstructed archive verification records differ")
    with Path(path).open("rb") as stream:
        for index, row in enumerate(rows):
            if stop is not None and stop.is_set():
                raise InterruptedError("Archive processing stopped")
            name = relative_path(row[0]).casefold()
            if name not in expected:
                raise WabbajackError("Reconstructed archive contains an unexpected file")
            actual = XXHash()
            skip, count = len(row[1]), 0
            def collect(data):
                nonlocal skip, count
                chunk = data[skip:]
                skip = max(0, skip - len(data))
                count += len(chunk)
                actual.update(chunk)
            cost = 16 * BLOCK + max((packed + full for _, packed, full, compression in row[2]
                                    if compression == "lz4block"), default=0)
            if memory_budget is not None:
                memory_budget.acquire(cost, cancel=stop)
            try:
                read_member(stream, row, collect, stop)
            finally:
                if memory_budget is not None:
                    memory_budget.release(cost)
            if expected_hashes is None:
                source = source_path(root, row[0])
                wanted, wanted_size = XXHash(), 0
                with source.open("rb") as incoming:
                    if row[1]:
                        header = _read(incoming, 128)
                        if header[84:88] == b"DX10":
                            _read(incoming, 20)
                    while data := incoming.read(BLOCK):
                        if stop is not None and stop.is_set():
                            raise InterruptedError("Archive processing stopped")
                        wanted.update(data)
                        wanted_size += len(data)
                wanted_digest = wanted.digest()
            else:
                wanted_size, wanted_digest = expected_hashes[name]
            if count != wanted_size or actual.digest() != wanted_digest:
                raise WabbajackError(f"Reconstructed archive content differs: {row[0]}")
            if progress:
                progress(index + 1, len(rows))
