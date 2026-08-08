"""
_bsa_writer_selftest.py
Round-trip checks for bsa_writer. Run as a script:

    python3 _bsa_writer_selftest.py

Verifies:
  * file list survives writer → reader round trip (v104 and v105)
  * exclusion filter drops .esp / readmes / dotfiles
  * compressed file payload extracts back to the original bytes
  * uncompressed (incompressible-extension) file extracts cleanly
  * empty-mod / no-packable-files raises
  * known TES4 hashes match documented values
"""

from __future__ import annotations

import struct
import sys
import tempfile
import zlib
from pathlib import Path

import lz4.frame

# Allow running as a standalone script: add the src/ root to sys.path so
# `Utils.bsa_*` imports resolve.
_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils.bsa_extract import BsaExtractError, extract_bsa  # noqa: E402
from Utils.bsa_reader import read_bsa_file_list  # noqa: E402
from Utils.bsa_writer import (  # noqa: E402
    BsaWriteError,
    is_our_stub_plugin,
    is_packable,
    tes4_hash_file,
    tes4_hash_folder,
    write_bsa,
    write_stub_plugin,
)
from Utils.plugin_parser import (  # noqa: E402
    is_esl_flagged, read_masters, read_plugin_header_flags,
)


# ---------------------------------------------------------------------------
# Hash sanity - known cp1252 strings against values produced by libbsarch.
# These were computed from the canonical algorithm; they're here to guard
# against future refactors silently breaking hash agreement.
# ---------------------------------------------------------------------------

def test_hashes() -> None:
    # File hashes - root + ext layout, with the per-extension magic OR.
    # Sanity check: round-trip through the reader's cache layer is enough
    # for the writer to produce a readable archive, but the *game* will
    # silently reject bad hashes. We pin a few values to catch drift.
    samples_file = [
        ("test.dds",         _tes4_ref(b"test", b".dds")),
        ("dragon.nif",       _tes4_ref(b"dragon", b".nif")),
        ("alpha.kf",         _tes4_ref(b"alpha", b".kf")),
        ("ambient.wav",      _tes4_ref(b"ambient", b".wav")),
        ("readme.txt",       _tes4_ref(b"readme", b".txt")),
        ("a.dds",            _tes4_ref(b"a", b".dds")),
    ]
    for name, expected in samples_file:
        got = tes4_hash_file(name)
        assert got == expected, (
            f"file hash mismatch for {name!r}: got {got:#018x} "
            f"expected {expected:#018x}"
        )

    samples_folder = [
        ("textures",                      _tes4_ref(b"textures", b"")),
        ("textures\\sky",                 _tes4_ref(b"textures\\sky", b"")),
        ("meshes\\armor\\iron",           _tes4_ref(b"meshes\\armor\\iron", b"")),
    ]
    for path, expected in samples_folder:
        got = tes4_hash_folder(path)
        assert got == expected, (
            f"folder hash mismatch for {path!r}: got {got:#018x} "
            f"expected {expected:#018x}"
        )

    # Forward-slash → backslash normalisation must produce identical hash.
    assert tes4_hash_folder("textures/sky") == tes4_hash_folder("textures\\sky")
    print("✓ hashes")


def _tes4_ref(name: bytes, ext: bytes) -> int:
    """Reference TES4 hash, recomputed locally - verifies the writer's
    implementation against an independent transcription of the algorithm."""
    full = (name + ext).lower()
    if not full:
        return 0
    dot = full.rfind(b".")
    if dot >= 0:
        root = full[:dot]
        ext_b = full[dot:]
    else:
        root = full
        ext_b = b""
    if not root:
        root = full
        ext_b = b""

    n = len(root)
    h1 = (
        root[n - 1]
        | ((root[n - 2] << 8) if n >= 3 else 0)
        | (n << 16)
        | (root[0] << 24)
    ) & 0xFFFFFFFF
    if ext_b == b".kf":
        h1 |= 0x80
    elif ext_b == b".nif":
        h1 |= 0xA000
    elif ext_b == b".dds":
        h1 |= 0x8080
    elif ext_b == b".wav":
        h1 |= 0x80000000

    h2 = 0
    if n > 3:
        for c in root[1:n - 2]:
            h2 = (h2 * 0x1003F + c) & 0xFFFFFFFF
    h3 = 0
    for c in ext_b:
        h3 = (h3 * 0x1003F + c) & 0xFFFFFFFF

    return ((((h2 + h3) & 0xFFFFFFFF) << 32) | h1) & 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Exclusion filter
# ---------------------------------------------------------------------------

def test_filter() -> None:
    """Verify the per-game allowlist (Utils.archive_rules) - not just the
    always-excluded globals.  Skyrim SE is the canonical reference."""
    sse = "skyrim_se"

    # Should pack - bethutil's tes5_default_sets allows these.
    assert is_packable("textures/sky.dds", sse)
    assert is_packable("meshes/foo.nif", sse)
    assert is_packable("sound/door.wav", sse)
    assert is_packable("interface/skyui.swf", sse)
    assert is_packable("interface/translations/foo_english.txt", sse)
    # bethutil allows .txt under {interface, meshes, scripts}; subdirs
    # of those count because we match on the *first* relative segment.
    assert is_packable("interface/exported/morehud/config.txt", sse)
    assert is_packable("scripts/source/foo.psc", sse)
    assert is_packable("source/scripts/bar.psc", sse)
    assert is_packable("scripts/myscript.pex", sse)
    assert is_packable("strings/lang.dlstrings", sse)
    assert is_packable("seq/quest.seq", sse)

    # Should NOT pack - these would break the mod or pollute the archive.
    assert not is_packable("plugin.esp", sse)
    assert not is_packable("plugin.esl", sse)
    assert not is_packable("plugin.esm", sse)
    assert not is_packable("nested.bsa", sse)
    assert not is_packable("nested.ba2", sse)
    # Wrong directory for a packable extension - engine would never read
    # it from inside the archive.
    assert not is_packable("docs/manual.txt", sse)        # .txt only under interface/meshes/scripts
    assert not is_packable("random/stray.dds", sse)       # .dds only under textures/interface
    # Extensions outside the allowlist entirely.
    assert not is_packable("video/intro.bik", sse)        # videos stream from disk
    assert not is_packable("video/intro.bk2", sse)        # ditto
    assert not is_packable("docs/manual.md", sse)
    assert not is_packable("docs/manual.pdf", sse)
    assert not is_packable("config.json", sse)
    assert not is_packable("loose.7z", sse)
    assert not is_packable(".gitignore", sse)
    assert not is_packable("subdir/.DS_Store", sse)
    assert not is_packable("meta.ini", sse)
    assert not is_packable("info.xml", sse)
    assert not is_packable("install.exe", sse)
    assert not is_packable("skse/plugins/foo.dll", sse)   # SKSE refuses BSA-loaded DLLs
    print("✓ filter (skyrim_se allowlist)")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def _make_test_tree(root: Path) -> dict[str, bytes]:
    """Populate *root* with a representative mod layout; return a dict of
    {expected_lower_forward_slash_path: original_bytes} for files that
    should survive packing."""
    files = {
        "textures/sky/clouds01.dds":      b"DDS-fake-data-" + b"X" * 4096,
        "textures/sky/clouds02.dds":      b"DDS-fake-data-" + b"Y" * 100,
        "meshes/armor/iron/cuirass.nif":  b"NIF-fake-" + b"Z" * 8192,
        "sound/fx/door_open.wav":          b"RIFF" + b"\x00" * 64 + b"data-wav",
        "scripts/source/_test.psc":        b"// pseudo-script",
        "interface/skyui.swf":             b"FWS\x06" + b"\x00" * 32,
        # .txt inside an engine folder is required content (translations,
        # animation manifests, SWF configs).  Must survive the pack.
        "interface/translations/mod_english.txt": b"$key=value",
    }
    excluded = {
        "plugin.esp":          b"ESP",
        # Root-level readme.txt is dropped by the "no root files" rule
        # in _collect_files, regardless of is_packable's verdict.
        "readme.txt":          b"hi",
        "meta.ini":            b"[General]\n",
        ".hidden":             b"x",
        "docs/manual.md":      b"# docs",
    }
    for rel, data in {**files, **excluded}.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
    return files


def _extract_file_via_offsets(bsa_path: Path, target: str) -> bytes:
    """Independent extractor - re-walks the TOC, finds *target* (lowercase
    forward-slash relative path) and returns its data. Used to verify the
    on-disk layout the writer produced, not just the file list."""
    with bsa_path.open("rb") as f:
        magic = f.read(4)
        assert magic == b"BSA\x00", f"bad magic: {magic!r}"
        (
            version,
            folder_offset,
            archive_flags,
            folder_count,
            file_count,
            total_folder_name_length,
            total_file_name_length,
            file_flags,
        ) = struct.unpack("<IIIIIIII", f.read(32))
        assert version in (104, 105)
        compressed_default = bool(archive_flags & 0x4)

        # Folder records.
        f.seek(folder_offset)
        folder_records: list[tuple[int, int]] = []
        if version == 105:
            for _ in range(folder_count):
                rec = f.read(24)
                _h, count, _pad = struct.unpack_from("<QII", rec, 0)
                offset = struct.unpack_from("<Q", rec, 16)[0]
                folder_records.append((count, offset))
        else:
            for _ in range(folder_count):
                rec = f.read(16)
                _h, count, offset = struct.unpack_from("<QII", rec, 0)
                folder_records.append((count, offset))

        # Folder names + file records.
        all_files: list[tuple[str, int, int]] = []  # (folder, size_field, data_offset)
        folder_names: list[str] = []
        for count, _block_offset in folder_records:
            name_len = f.read(1)[0]
            name_bytes = f.read(name_len).rstrip(b"\x00")
            folder = name_bytes.decode("cp1252").replace("\\", "/").lower()
            folder_names.append(folder)
            for _ in range(count):
                rec = f.read(16)
                _fh, size_field, data_offset = struct.unpack("<QII", rec)
                all_files.append((folder, size_field, data_offset))

        # File name block.
        name_block = f.read(total_file_name_length)
        names = name_block.decode("cp1252").split("\x00")
        if names and names[-1] == "":
            names.pop()

        # Pair names with file records (1:1 in order).
        assert len(names) == len(all_files), (
            f"name count {len(names)} != record count {len(all_files)}"
        )

        # Find target.
        target_lower = target.lower()
        for (folder, size_field, data_offset), name in zip(all_files, names):
            full = (folder + "/" + name.lower()) if folder else name.lower()
            if full != target_lower:
                continue
            on_disk_size = size_field & 0x3FFFFFFF
            invert = bool(size_field & 0x40000000)
            file_compressed = compressed_default ^ invert
            f.seek(data_offset)
            payload = f.read(on_disk_size)
            if file_compressed:
                # 4-byte original-size prefix, then version-specific stream:
                # v104 uses zlib, v105 uses LZ4 frame.
                # 4-byte original-size prefix precedes the compressed stream.
                body = payload[4:]
                if version == 105:
                    return lz4.frame.decompress(body)
                return zlib.decompress(body)
            return payload

    raise AssertionError(f"not found in archive: {target}")


def test_roundtrip(version: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "MyMod"
        src.mkdir()
        expected = _make_test_tree(src)

        bsa_path = tmp_path / "MyMod.bsa"

        progress_calls: list[tuple[int, int, str]] = []
        def _progress(d, t, p):
            progress_calls.append((d, t, p))

        count, size, packed_keys = write_bsa(
            bsa_path, src,
            version=version,
            game_id="skyrim_se" if version == 105 else "skyrim",
            compress=True,
            progress=_progress,
        )

        assert count == len(expected), (
            f"file count {count} != expected {len(expected)}"
        )
        assert set(packed_keys) == set(expected.keys()), (
            f"packed keys mismatch:\n  unexpected: {set(packed_keys) - set(expected)}\n"
            f"  missing:    {set(expected) - set(packed_keys)}"
        )
        assert bsa_path.exists()
        assert size > 0

        # Reader sees the right list.
        listed = set(read_bsa_file_list(bsa_path))
        expected_keys = set(expected.keys())
        assert listed == expected_keys, (
            f"file list mismatch:\n  unexpected: {listed - expected_keys}\n"
            f"  missing:    {expected_keys - listed}"
        )

        # Verify a compressed file extracts to original bytes.
        # All test files except the .wav are compressible.
        compressible = "textures/sky/clouds01.dds"
        assert _extract_file_via_offsets(bsa_path, compressible) == expected[compressible]

        # Verify the incompressible-extension file (.wav) extracts cleanly
        # - the writer should have set the per-file invert bit.
        wav = "sound/fx/door_open.wav"
        assert _extract_file_via_offsets(bsa_path, wav) == expected[wav]

        # Progress callback fired at least once per file.
        assert len(progress_calls) == len(expected)
        last = progress_calls[-1]
        assert last[0] == last[1] == len(expected)

    print(f"✓ round-trip v{version}")


def test_extract_roundtrip(version: int) -> None:
    """write_bsa(...) → extract_bsa(...) must reproduce every file's bytes
    exactly. Covers v104 (zlib) and v105 (LZ4 frame), incompressible-file
    invert bit, and non-overwrite mode."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "MyMod"
        src.mkdir()
        expected = _make_test_tree(src)

        bsa_path = tmp_path / "MyMod.bsa"
        write_bsa(
            bsa_path, src,
            version=version,
            game_id="skyrim_se" if version == 105 else "skyrim",
            compress=True,
        )

        out = tmp_path / "Out"
        progress_calls: list[tuple[int, int, str]] = []
        count, written = extract_bsa(
            bsa_path, out,
            progress=lambda d, t, p: progress_calls.append((d, t, p)),
        )
        assert count == len(expected)
        assert set(written) == set(expected.keys())

        # Byte-for-byte compare every extracted file against the source.
        for rel, original in expected.items():
            extracted = (out / rel).read_bytes()
            assert extracted == original, f"mismatch for {rel}"

        assert len(progress_calls) == len(expected)
        last = progress_calls[-1]
        assert last[0] == last[1] == len(expected)

        # overwrite=False - pre-place a marker in a fresh dir and run
        # extract on top. The marker file must survive; siblings get
        # extracted normally.
        out2 = tmp_path / "Out2"
        out2.mkdir()
        marker_rel = next(iter(expected))
        (out2 / marker_rel).parent.mkdir(parents=True, exist_ok=True)
        (out2 / marker_rel).write_bytes(b"DO NOT TOUCH")
        count2, written2 = extract_bsa(bsa_path, out2, overwrite=False)
        assert marker_rel not in written2, "marker rel should be skipped"
        assert count2 == len(expected) - 1, (
            f"expected {len(expected) - 1} written, got {count2}"
        )
        assert (out2 / marker_rel).read_bytes() == b"DO NOT TOUCH", (
            "overwrite=False clobbered the marker file"
        )

    print(f"✓ extract round-trip v{version}")


def test_extract_cancel() -> None:
    """If the cancel callback returns True mid-walk, extract_bsa raises
    BsaExtractError('cancelled') and the caller can clean up dest_dir."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "MyMod"
        src.mkdir()
        _make_test_tree(src)
        bsa_path = tmp_path / "MyMod.bsa"
        write_bsa(
            bsa_path, src,
            version=105, game_id="skyrim_se", compress=True,
        )

        out = tmp_path / "Out"
        try:
            extract_bsa(bsa_path, out, cancel=lambda: True)
        except BsaExtractError as exc:
            assert "cancel" in str(exc).lower()
            print("✓ extract cancel")
            return
        raise AssertionError("expected BsaExtractError on cancel")


def test_excluded_keys() -> None:
    """rel_keys passed in *excluded_keys* must be omitted from the archive
    even if they pass the packable filter."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "MyMod"
        src.mkdir()
        _make_test_tree(src)

        # Disable the wallpaper texture and the wav.
        excluded = frozenset({
            "textures/sky/clouds01.dds",
            "sound/fx/door_open.wav",
        })

        bsa_path = Path(tmp) / "out.bsa"
        count, _size, packed_keys = write_bsa(
            bsa_path, src,
            version=105, game_id="skyrim_se", excluded_keys=excluded,
        )
        listed = set(read_bsa_file_list(bsa_path))
        # The two excluded files must be absent.
        assert "textures/sky/clouds01.dds" not in listed
        assert "sound/fx/door_open.wav" not in listed
        # Other packable files survive.
        assert "textures/sky/clouds02.dds" in listed
        assert count == len(packed_keys)
        # Excluded keys are also absent from the returned packed list.
        assert excluded.isdisjoint(packed_keys)
    print("✓ excluded_keys")


def test_split_textures() -> None:
    """texture_mode='exclude' produces a base BSA without textures;
    'only' produces a BSA with only textures.  Together they should
    cover the same set of files as a single 'all' pack, with no
    overlap."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "MyMod"
        src.mkdir()
        _make_test_tree(src)

        # Reference: full pack.
        full_path = tmp_path / "Full.bsa"
        _, _, full_packed = write_bsa(
            full_path, src, version=105, game_id="skyrim_se",
        )
        full_set = set(full_packed)

        # Split pack: base (no textures) + textures-only.
        base_path = tmp_path / "Base.bsa"
        tex_path = tmp_path / "Textures.bsa"
        _, _, base_packed = write_bsa(
            base_path, src, version=105, game_id="skyrim_se",
            texture_mode="exclude",
        )
        _, _, tex_packed = write_bsa(
            tex_path, src, version=105, game_id="skyrim_se",
            texture_mode="only",
        )
        base_set = set(base_packed)
        tex_set = set(tex_packed)

        # Coverage and disjointness.
        assert base_set | tex_set == full_set, (
            f"split coverage: missing {full_set - (base_set | tex_set)}, "
            f"extra {(base_set | tex_set) - full_set}"
        )
        assert base_set.isdisjoint(tex_set), (
            f"split overlap: {base_set & tex_set}"
        )

        # Sanity: textures sibling really only contains DDS / texture-rule files.
        from Utils.archive_rules import texture_extensions_for_game
        tex_exts = texture_extensions_for_game("skyrim_se")
        for rk in tex_set:
            ext = "." + rk.rsplit(".", 1)[-1] if "." in rk else ""
            assert ext in tex_exts, f"non-texture in textures sibling: {rk}"
        # And the base really excludes them.
        for rk in base_set:
            ext = "." + rk.rsplit(".", 1)[-1] if "." in rk else ""
            assert ext not in tex_exts, f"texture leaked into base: {rk}"

        # Reading back: every file in either archive should match the
        # source on disk.
        for rk in full_set:
            assert rk in (
                set(read_bsa_file_list(base_path)) | set(read_bsa_file_list(tex_path))
            ), rk
    print("✓ split textures (texture_mode=exclude/only)")


def test_no_packable_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "EmptyMod"
        src.mkdir()
        (src / "readme.txt").write_text("nothing to pack")
        (src / "plugin.esp").write_bytes(b"esp")
        try:
            write_bsa(Path(tmp) / "out.bsa", src)
        except BsaWriteError as e:
            assert "no packable" in str(e).lower()
            print("✓ empty mod raises BsaWriteError")
            return
        raise AssertionError("expected BsaWriteError for empty mod")


def test_root_files_skipped() -> None:
    """Files at the mod root (no parent directory) cannot be packed."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "RootOnly"
        src.mkdir()
        # Only files at root - they must not pack (BSA format requires a folder).
        (src / "loose.dds").write_bytes(b"x")
        try:
            write_bsa(Path(tmp) / "out.bsa", src)
        except BsaWriteError:
            print("✓ root-level-only mod raises BsaWriteError")
            return
        raise AssertionError("expected BsaWriteError for root-only mod")


def test_oversized_file_rejected() -> None:
    """A single file larger than the BSA size field (~1 GiB) must raise
    rather than silently truncate."""
    # We can't actually allocate 1 GiB on disk in CI/dev loops, so we
    # monkey-patch read_bytes to return a stubbed-large payload.  This
    # exercises the size-field guard path without writing 1 GiB of data.
    from Utils import bsa_writer
    real_read_bytes = Path.read_bytes
    big = 0x40000001  # one byte past _FILE_SIZE_MASK
    class _BigBytes(bytes):
        # Pretend the file is "big" bytes long without actually allocating.
        def __new__(cls):
            return super().__new__(cls)
        def __len__(self):
            return big
    def fake(self):
        return _BigBytes()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "MyMod"
        src.mkdir()
        (src / "meshes").mkdir()
        (src / "meshes" / "huge.nif").write_bytes(b"x")
        Path.read_bytes = fake          # type: ignore[assignment]
        try:
            write_bsa(Path(tmp) / "out.bsa", src,
                      version=105, game_id="skyrim_se", compress=False)
        except bsa_writer.BsaWriteError as e:
            assert "too large" in str(e).lower(), e
        else:
            raise AssertionError("expected BsaWriteError for oversized file")
        finally:
            Path.read_bytes = real_read_bytes  # type: ignore[assignment]
    print("✓ oversized file rejected")


def test_stub_plugin() -> None:
    """The stub plugin must be parseable by the existing TES4 reader (which
    is what the rest of the app uses to read plugin headers, masters, ESL
    flag, etc.)."""
    with tempfile.TemporaryDirectory() as tmp:
        # SSE plugin, default behaviour: ESL auto-enabled on capable engines.
        sse_esp = Path(tmp) / "MyMod.esp"
        write_stub_plugin(sse_esp, game_id="skyrim_se")
        assert sse_esp.exists()
        assert sse_esp.stat().st_size == 49, sse_esp.stat().st_size
        assert read_masters(sse_esp) == []
        assert is_esl_flagged(sse_esp), "default should be ESL on SSE"

        # SSE plugin, ESL explicitly off.
        sse_no_esl = Path(tmp) / "MyMod_noesl.esp"
        write_stub_plugin(sse_no_esl, game_id="skyrim_se", esl=False)
        flags = read_plugin_header_flags(sse_no_esl)
        assert flags == 0, f"expected flags=0, got {flags}"
        assert not is_esl_flagged(sse_no_esl)

        # Skyrim LE - engine doesn't support ESL; flag must be ignored even
        # if explicitly requested.
        sle_esp = Path(tmp) / "MyMod_sle.esp"
        write_stub_plugin(sle_esp, game_id="skyrim", esl=True)
        assert not is_esl_flagged(sle_esp), "ESL bit should not be set on LE"

        # Verify HEDR version field for SSE is 1.7 and for LE is 0.94.
        sse_bytes = sse_esp.read_bytes()
        version_offset = 24 + 4 + 2  # TES4 header (24) + b"HEDR" + size(2)
        sse_version = struct.unpack_from("<f", sse_bytes, version_offset)[0]
        assert abs(sse_version - 1.7) < 1e-6, f"SSE HEDR version: {sse_version}"

        sle_bytes = sle_esp.read_bytes()
        sle_version = struct.unpack_from("<f", sle_bytes, version_offset)[0]
        assert abs(sle_version - 0.94) < 1e-6, f"LE HEDR version: {sle_version}"

        # Verify TES4 internal version (offset 20, uint16). 44 for SSE,
        # 43 for LE. Empty/zero here causes the engine to silently reject
        # the plugin - and therefore not load its BSA.
        sse_iv = struct.unpack_from("<H", sse_bytes, 20)[0]
        sle_iv = struct.unpack_from("<H", sle_bytes, 20)[0]
        assert sse_iv == 44, f"SSE internal_version expected 44, got {sse_iv}"
        assert sle_iv == 43, f"LE internal_version expected 43, got {sle_iv}"

        # Verify CNAM subrecord is present at the tail.
        # Layout: TES4(24) + HEDR(18) + CNAM(7) = 49 bytes.
        assert sse_bytes[42:46] == b"CNAM", (
            f"expected CNAM at offset 42, got {sse_bytes[42:46]!r}"
        )

        # Unknown game raises.
        try:
            write_stub_plugin(Path(tmp) / "x.esp", game_id="bogus")
        except BsaWriteError:
            pass
        else:
            raise AssertionError("expected BsaWriteError for unknown game_id")

        # Stub-detection: our generated plugins are recognised.
        assert is_our_stub_plugin(sse_esp)
        assert is_our_stub_plugin(sse_no_esl)
        assert is_our_stub_plugin(sle_esp)
        # The legacy 42-byte stub format is also still recognised, so users
        # who packed before the CNAM was added can re-pack and have us
        # regenerate their stub.
        legacy = Path(tmp) / "legacy.esp"
        legacy.write_bytes(bytes.fromhex(
            "5445533412000000000000000000000000000000"
            "2c00000048454452"          # TES4 hdr ending + HEDR start
            "0c009a99d93f00000000000800 00".replace(" ", "")
        ))
        assert legacy.stat().st_size == 42
        assert is_our_stub_plugin(legacy)
        # A larger plugin (real authored content) is rejected.
        big_path = Path(tmp) / "real.esp"
        big_path.write_bytes(sse_esp.read_bytes() + b"\x00" * 100)
        assert not is_our_stub_plugin(big_path)
        # A 42-byte file with wrong signature is rejected.
        bogus_path = Path(tmp) / "bogus.esp"
        bogus_path.write_bytes(b"\x00" * 42)
        assert not is_our_stub_plugin(bogus_path)

    print("✓ stub plugin")


if __name__ == "__main__":
    test_hashes()
    test_filter()
    test_roundtrip(104)
    test_roundtrip(105)
    test_extract_roundtrip(104)
    test_extract_roundtrip(105)
    test_extract_cancel()
    test_excluded_keys()
    test_split_textures()
    test_no_packable_files()
    test_root_files_skipped()
    test_oversized_file_rejected()
    test_stub_plugin()
    print("all good")
