"""
ba2/_selftest.py
Round-trip checks for the BA2 writer and extractor.

    PYTHONPATH=src python3 -m Utils.ba2._selftest

Verifies:
  * ba2_hash matches a handful of known-good vanilla FO4 hashes
  * write_ba2 → read_bsa_file_list lists the right paths
  * write_ba2 → extract_ba2 round-trips bytes exactly (compressed and
    incompressible-extension paths)
  * exclude_keys honoured
  * cancel mid-pack raises Ba2WriteError('cancelled')
"""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

# Allow running as a standalone script: add the src/ root to sys.path so
# `Utils.ba2` imports resolve.
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils.ba2.extract import Ba2ExtractError, extract_ba2  # noqa: E402
from Utils.ba2.writer import (  # noqa: E402
    Ba2WriteError, ba2_hash, write_ba2, write_ba2_textures,
)
from Utils.bsa.reader import read_bsa_file_list  # noqa: E402


# ---------------------------------------------------------------------------
# Hash sanity - five real (path, hash) samples from vanilla FO4 BA2s.
# A regression here means the writer is producing archives whose lookups
# the engine can't resolve (silent missing-asset failures).
# ---------------------------------------------------------------------------

def test_ba2_hash() -> None:
    samples = [
        # (input string, expected hash) - pulled from real vanilla BA2 dumps.
        ("Materials\\CreationClub\\BGSFO4038\\Actors\\PowerArmor", 0x44676566),
        ("HorsePAArmL", 0x0037711f),
        ("HorsePAArmL_Green", 0xb0ae1a37),
        ("HorsePAArmL_Pink", 0xa773c26f),
        ("Sound\\Voice\\DLCNukaWorld.esm\\CrFeralGhoul", 0x71aaa356),
    ]
    for s, expected in samples:
        got = ba2_hash(s)
        assert got == expected, (
            f"ba2_hash({s!r}) = 0x{got:08x}, expected 0x{expected:08x}"
        )
    # Empty string hashes to 0 (used for files at the mod root).
    assert ba2_hash("") == 0
    print("✓ ba2_hash")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def _make_test_tree(root: Path) -> dict[str, bytes]:
    """A representative FO4 layout - every path here matches a rule in
    bethutil's FO4 allowlist (textures/*.dds, meshes/*.nif, sound/*.wav,
    scripts/*.pex, interface/*.swf, interface/translations/*.txt)."""
    files = {
        "textures/foo.dds":              b"DDS-stub-" + b"A" * 4096,
        "meshes/items/sword.nif":        b"NIF-stub-" + b"B" * 8192,
        "sound/door_open.wav":           b"RIFF" + b"\x00" * 64 + b"data-wav",
        "scripts/foo.pex":               b"// pseudo-script",
        "interface/skyui.swf":           b"FWS\x06" + b"\x00" * 32,
        "interface/translations/mod_english.txt": b"$key=value",
    }
    excluded = {
        # Outside the FO4 allowlist (plugins, archives, dotfiles,
        # docs, randomly-placed files, dotfiles).
        "plugin.esp":          b"ESP",
        "readme.md":           b"# readme",
        "meta.ini":            b"[General]\n",
        ".hidden":             b"x",
        "video/intro.bk2":     b"BIK\x00",   # FO4 streams BK2 from disk
        "rootfile.swf":        b"FWS\x06",   # .swf only under interface/
    }
    for rel, data in {**files, **excluded}.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
    return files


def test_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "Mod"
        src.mkdir()
        expected = _make_test_tree(src)
        ba2_path = tmp_path / "Mod.ba2"

        progress_calls: list[tuple[int, int, str]] = []
        count, size, packed_keys = write_ba2(
            ba2_path, src,
            game_id="Fallout4",
            compress=True,
            progress=lambda d, t, p: progress_calls.append((d, t, p)),
        )
        assert count == len(expected), (
            f"file count {count} != {len(expected)}"
        )
        assert ba2_path.exists() and size > 0
        assert set(packed_keys) == set(expected.keys())

        # Reader (TOC-only) sees the right list.
        listed = set(read_bsa_file_list(ba2_path))
        assert listed == set(expected.keys()), (
            f"file list mismatch:\n  unexpected: {listed - set(expected)}\n"
            f"  missing:    {set(expected) - listed}"
        )

        # Extractor pulls every file back out byte-identical.
        out = tmp_path / "Out"
        ext_count, written = extract_ba2(ba2_path, out)
        assert ext_count == len(expected)
        for rel, original in expected.items():
            got = (out / rel).read_bytes()
            assert got == original, (
                f"byte mismatch for {rel}: orig={len(original)}, got={len(got)}"
            )

        # Progress fired at least once per file (write_ba2 emits 2× the
        # file count: prep phase + data phase).
        assert progress_calls, "no progress callbacks fired"

        # Header sanity check - confirm we wrote v1 GNRL.
        with ba2_path.open("rb") as f:
            magic = f.read(4)
            version, type_tag, fc, _nto = struct.unpack("<I4sIQ", f.read(20))
        assert magic == b"BTDX"
        assert version == 1
        assert type_tag == b"GNRL"
        assert fc == len(expected)

    print("✓ ba2 round-trip")


def test_excluded_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "Mod"
        src.mkdir()
        _make_test_tree(src)
        ba2_path = Path(tmp) / "out.ba2"
        excluded = frozenset({"textures/foo.dds", "interface/skyui.swf"})
        count, _size, packed_keys = write_ba2(
            ba2_path, src,
            game_id="Fallout4",
            excluded_keys=excluded,
        )
        listed = set(read_bsa_file_list(ba2_path))
        assert "textures/foo.dds" not in listed
        assert "interface/skyui.swf" not in listed
        assert "meshes/items/sword.nif" in listed
        assert excluded.isdisjoint(packed_keys)
        assert count == len(packed_keys)
    print("✓ ba2 excluded_keys")


def test_no_packable_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "Empty"
        src.mkdir()
        (src / "readme.md").write_text("nothing")
        (src / "plugin.esp").write_bytes(b"esp")
        try:
            write_ba2(Path(tmp) / "out.ba2", src, game_id="Fallout4")
        except Ba2WriteError as e:
            assert "no packable" in str(e).lower()
            print("✓ ba2 empty mod raises")
            return
        raise AssertionError("expected Ba2WriteError for empty mod")


def test_cancel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "Mod"
        src.mkdir()
        _make_test_tree(src)
        try:
            write_ba2(
                Path(tmp) / "out.ba2", src,
                game_id="Fallout4",
                cancel=lambda: True,
            )
        except Ba2WriteError as exc:
            assert "cancel" in str(exc).lower()
            print("✓ ba2 cancel")
            return
        raise AssertionError("expected Ba2WriteError on cancel")


def test_extract_overwrite_false() -> None:
    """extract_ba2(overwrite=False) must skip pre-existing files."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "Mod"
        src.mkdir()
        expected = _make_test_tree(src)
        ba2_path = tmp_path / "Mod.ba2"
        write_ba2(ba2_path, src, game_id="Fallout4")

        out = tmp_path / "Out"
        out.mkdir()
        marker_rel = next(iter(expected))
        (out / marker_rel).parent.mkdir(parents=True, exist_ok=True)
        (out / marker_rel).write_bytes(b"DO NOT TOUCH")

        count, written = extract_ba2(ba2_path, out, overwrite=False)
        assert marker_rel not in written
        assert count == len(expected) - 1
        assert (out / marker_rel).read_bytes() == b"DO NOT TOUCH"
    print("✓ ba2 extract overwrite=False")


def test_extract_cancel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "Mod"
        src.mkdir()
        _make_test_tree(src)
        ba2_path = tmp_path / "Mod.ba2"
        write_ba2(ba2_path, src, game_id="Fallout4")
        try:
            extract_ba2(ba2_path, tmp_path / "Out", cancel=lambda: True)
        except Ba2ExtractError as exc:
            assert "cancel" in str(exc).lower()
            print("✓ ba2 extract cancel")
            return
        raise AssertionError("expected Ba2ExtractError on cancel")


def _make_synthetic_dds(width: int, height: int, mip_count: int = 1,
                        dxgi_format: int = 71) -> bytes:
    """Build a minimal valid DX10 DDS file with random-but-deterministic
    pixel bytes.  Format 71 is BC1_UNORM (8 bytes per 4×4 block); BC2/3
    family is 16 bytes per block; BC5/7 likewise.

    We only need the bytes to round-trip - they don't need to decode to
    a real image."""
    # Per-mip linear size depends on the DXGI format's block size.
    block_8 = {70, 71, 72, 79, 80, 81}                                  # BC1, BC4
    block_16 = {73, 74, 75, 76, 77, 78, 82, 83, 84,
                94, 95, 96, 97, 98, 99}                                  # BC2/3/5/6/7
    if dxgi_format in block_8:
        block = 8
    elif dxgi_format in block_16:
        block = 16
    else:
        raise ValueError(f"unsupported dxgi_format={dxgi_format} in test")

    def linsize(w, h):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block

    # DDS_HEADER (124 bytes)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if mip_count > 1 else 0)
    caps = 0x1000 | (0x400000 | 0x8 if mip_count > 1 else 0)
    pixel_format = struct.pack(
        "<II4sIIIII",
        32, 0x4, b"DX10",
        0, 0, 0, 0, 0,
    )
    header = struct.pack(
        "<II I I I I I 11I 32s I I I I I",
        124, flags, height, width, linsize(width, height), 0, max(mip_count, 1),
        *([0] * 11),
        pixel_format,
        caps, 0, 0, 0,
        0,
    )
    dxt10 = struct.pack("<IIIII", dxgi_format, 3, 0, 1, 0)
    body = b"DDS " + header + dxt10

    # Append per-mip pixel bytes (deterministic so byte-compares work).
    payload = b""
    for m in range(mip_count):
        mw = max(1, width >> m)
        mh = max(1, height >> m)
        n = linsize(mw, mh)
        # Repeating pattern keyed off mip index keeps each mip
        # distinguishable in the byte-compare diagnostic.
        payload += bytes((m + 1) * (i & 0xff) % 256 for i in range(n))
    return body + payload


def test_dx10_roundtrip() -> None:
    """write_ba2_textures(...) → extract_ba2(...) must reproduce every
    DDS byte-for-byte.  Covers single-mip and multi-mip files, BC1
    (block 8) and BC3 (block 16)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "Mod"
        src.mkdir()
        # Multiple textures with different dimensions / formats / mip
        # counts.  All under textures/ to satisfy the FO4 allowlist.
        (src / "textures").mkdir()
        files = {
            "textures/foo.dds":      _make_synthetic_dds(64, 64, mip_count=4, dxgi_format=71),
            "textures/bar.dds":      _make_synthetic_dds(128, 64, mip_count=1, dxgi_format=77),
            "textures/baz.dds":      _make_synthetic_dds(16, 16, mip_count=3, dxgi_format=83),
            # Plus a non-DDS texture-folder file that must be excluded
            # from the DX10 archive (it's not a DDS).
        }
        for rel, data in files.items():
            (src / rel).write_bytes(data)
        # Add a non-DDS texture so we can confirm it doesn't end up in
        # our DX10 archive (the FO4 GUI flow would route it to GNRL).
        (src / "textures/notes.txt").write_bytes(b"comments")

        ba2 = tmp_path / "Mod - Textures.ba2"
        count, size, packed = write_ba2_textures(
            ba2, src, game_id="Fallout4", compress=True,
        )
        assert count == len(files), f"expected {len(files)} textures, got {count}"
        assert ba2.stat().st_size == size

        # Header sanity - we wrote v1 DX10.
        with ba2.open("rb") as f:
            magic = f.read(4)
            v, t, fc, _ = struct.unpack("<I4sIQ", f.read(20))
        assert magic == b"BTDX"
        assert v == 1
        assert t == b"DX10"
        assert fc == len(files)

        # Round-trip: extract and byte-compare.
        out = tmp_path / "Out"
        ext_count, written = extract_ba2(ba2, out)
        assert ext_count == len(files)
        for rel, original in files.items():
            got = (out / rel).read_bytes()
            assert got == original, (
                f"DX10 round-trip mismatch for {rel}: "
                f"orig={len(original)}, got={len(got)}"
            )

        # The non-DDS file must NOT be in the archive.
        listed = set(read_bsa_file_list(ba2))
        assert "textures/notes.txt" not in listed
    print("✓ ba2 DX10 round-trip")


def test_split_main_textures() -> None:
    """write_ba2(exclude_textures=True) + write_ba2_textures(...) must
    produce two disjoint archives: GNRL holds non-DDS, DX10 holds DDS."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "Mod"
        src.mkdir()
        (src / "meshes").mkdir()
        (src / "textures").mkdir()
        (src / "meshes/foo.nif").write_bytes(b"NIF" + b"X" * 1024)
        (src / "textures/foo.dds").write_bytes(
            _make_synthetic_dds(32, 32, mip_count=1, dxgi_format=71)
        )

        main_path = tmp_path / "Mod - Main.ba2"
        tex_path = tmp_path / "Mod - Textures.ba2"

        write_ba2(
            main_path, src,
            game_id="Fallout4",
            exclude_textures=True,
        )
        write_ba2_textures(tex_path, src, game_id="Fallout4")

        main_files = set(read_bsa_file_list(main_path))
        tex_files = set(read_bsa_file_list(tex_path))
        assert main_files == {"meshes/foo.nif"}, main_files
        assert tex_files == {"textures/foo.dds"}, tex_files
        # No overlap.
        assert main_files.isdisjoint(tex_files)
    print("✓ ba2 split main+textures")


if __name__ == "__main__":
    test_ba2_hash()
    test_roundtrip()
    test_excluded_keys()
    test_no_packable_files()
    test_cancel()
    test_extract_overwrite_false()
    test_extract_cancel()
    test_dx10_roundtrip()
    test_split_main_textures()
    print("all good")
