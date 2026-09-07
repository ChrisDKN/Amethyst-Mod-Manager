from __future__ import annotations

import hashlib
import io
import os
import struct
import tempfile
import unittest
from pathlib import Path

from .archive_build import rebuild_archive
from .archive_io import extract_bethesda, verify_archive
from .hashes import XXHash, file_hash
from .patches import apply_octodiff
from .paths import WabbajackError, relative_path, within, source_path
from .store import Store


def digest(data):
    value = XXHash()
    value.update(data)
    return value.digest()


def delta(output, commands):
    return b"OCTODELTA\x01\x04SHA1\x14\0\0\0" + hashlib.sha1(output).digest() + b">>>" + commands


class IntegrityChecks(unittest.TestCase):
    def test_long_names_and_atomic_publication(self):
        import threading
        import zipfile
        from Utils.atomic_write import atomic_writer, write_atomic
        from .paths import check_path_length, cache_path, auxiliary_path
        from .reconstruct import extract_safe
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name_max = os.pathconf(root, "PC_NAME_MAX")
            name = "x" * (name_max - 4) + ".txt"
            target = root / name
            write_atomic(target, b"original")
            with self.assertRaises(RuntimeError):
                with atomic_writer(target, "wb", encoding=None) as stream:
                    stream.write(b"incomplete")
                    raise RuntimeError("interrupted")
            self.assertEqual(target.read_bytes(), b"original")
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as source:
                source.writestr(name, b"complete")
            extract_safe(archive, root / "out", threading.Event(), lambda *_: None)
            self.assertEqual((root / "out" / name).read_bytes(), b"complete")
            check_path_length(root, ("folder/" * 50) + name)
            with self.assertRaises(WabbajackError):
                check_path_length(root, "é" * (name_max // 2 + 1))
            cached = cache_path(root, "0" * 16, "é" * 125 + ".7z")
            for path in (cached, auxiliary_path(cached, ".chunks"), auxiliary_path(cached, ".invalid-1234567890123456789")):
                self.assertLessEqual(len(os.fsencode(path.name)), name_max)
            store = Store(root / ".wabbajack" / "installation", root)
            try:
                staged = store.work / "source"
                staged.write_bytes(b"published")
                store._place(staged, root / "published" / name, digest(b"published"))
                self.assertEqual((root / "published" / name).read_bytes(), b"published")
            finally:
                store.close()

    def test_case_colliding_sources(self):
        import json
        import zipfile
        from types import SimpleNamespace
        from Utils.downloads.install import InstallCallbacks, InstallControl
        from .manifest import inspect_package
        from .reconstruct import Reconstruction
        from .archive_io import records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Folder/File.txt", b"wrong!")
                archive.writestr("Folder/file.txt", b"right!")
            key = file_hash(source)
            output = b"right!!"
            patch = delta(output, b"\x60" + struct.pack("<qq", 0, 6) + b"\x80" + struct.pack("<q", 1) + b"!")
            manifest = {"Name": "Case variants", "Archives": [{"Name": source.name, "Hash": key, "Size": source.stat().st_size, "State": {"$type": "Http"}}],
                        "Directives": [{"$type": kind, "To": f"mods/Example/{index}.txt", "Hash": digest(data), "Size": len(data),
                                        "ArchiveHashPath": [key, member], **extra}
                                       for index, (kind, member, data, extra) in enumerate([
                                           ("FromArchive", "Folder/FILE.txt", b"right!", {}),
                                           ("FromArchive", "Folder/File.txt", b"right!", {}),
                                           ("PatchedFromArchive", "Folder/File.txt", output, {"PatchID": "patch"}),
                                           ("PatchedFromArchive", "Folder/FILE.txt", output, {"PatchID": "patch"})])]}
            package_path = root / "case.wabbajack"
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("modlist", json.dumps(manifest))
                archive.writestr("patch", patch)
            package = inspect_package(package_path)
            directory = root / ".wabbajack" / "installation"
            request = SimpleNamespace(package=package, directory=directory, downloads=root / "downloads", game_roots={})
            store = Store(directory, root)
            try:
                reconstruction = Reconstruction(request, store, InstallCallbacks(), InstallControl())
                reconstruction.needed_archives()
                reconstruction.install_archive(package.archives[key], source)
                for directive in package.directives:
                    self.assertEqual(file_hash(reconstruction.output / directive.path), directive.hash)
            finally:
                store.close()
            archive = root / "case.bsa"
            names = b"File.txt\0file.txt\0"
            toc = struct.pack("<4I", 1, 0, 1, 1) + struct.pack("<2I", 0, 9) + names
            archive.write_bytes(struct.pack("<3I", 256, len(toc), 2) + toc + bytes(16) + b"AB")
            with self.assertRaises(WabbajackError):
                records(archive)
            extract_bethesda(archive, root / "bsa")
            self.assertEqual((root / "bsa/File.txt").read_bytes(), b"A")
            self.assertEqual(source_path(root / "bsa", "FILE.txt", expected=digest(b"B")).read_bytes(), b"B")
            with self.assertRaises(WabbajackError):
                source_path(root / "bsa", "FILE.txt", expected=digest(b"missing"))

    def test_compiled_profile_integrity(self):
        import json
        import zipfile
        from types import SimpleNamespace
        from Utils.downloads.install import InstallCallbacks, InstallControl
        from .manifest import inspect_package
        from .reconstruct import Reconstruction
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_path = root / "fixture.wabbajack"
            content = b"+Test\n"
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("profile", content)
                archive.writestr("bad", b"corrupted")
                archive.writestr("modlist", json.dumps({"Name": "Integrity", "Directives": [
                    {"$type": "InlineFile", "To": "profiles/Main/modlist.txt", "Hash": digest(content + b"-Unused\n"),
                     "Size": len(content) + 8, "SourceDataID": "profile"},
                    {"$type": "InlineFile", "To": "mods/Test/a.txt", "Hash": digest(b"original"),
                     "Size": 8, "SourceDataID": "bad"}]}))
            package = inspect_package(package_path)
            self.assertEqual(package.directives[0].output_hash, digest(content))
            self.assertFalse(package.directives[1].embedded_hash)
            directory = root / ".wabbajack" / "list"
            store = Store(directory, root)
            request = SimpleNamespace(package=package, directory=directory, downloads=root / "downloads", game_roots={})
            reconstruction = Reconstruction(request, store, InstallCallbacks(), InstallControl())
            with self.assertRaises(WabbajackError):
                reconstruction.finish()
            self.assertEqual(reconstruction.results["profiles/Main/modlist.txt"]["authored_hash"], digest(content))
            store.close()

    def test_octodiff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "target"
            source.write_bytes(b"abcdef")
            output = b"abXYZef!"
            commands = (b"\x60" + struct.pack("<qq", 0, 2) + b"\x80" + struct.pack("<q", 3) + b"XYZ"
                        + b"\x60" + struct.pack("<qq", 4, 2) + b"\x80" + struct.pack("<q", 1) + b"!")
            valid = delta(output, commands)
            apply_octodiff(source, io.BytesIO(valid), target, len(output), digest(output))
            self.assertEqual(target.read_bytes(), output)
            invalid = [valid[:-1], valid[:-1] + b"?", delta(output, b"\x60" + struct.pack("<qq", 5, 3)),
                       delta(output, b"\x80" + struct.pack("<q", -1)), valid + b"\x42",
                       delta(output, b"\x60" + struct.pack("<qq", -1, 1)),
                       delta(b"bad checksum", commands)]
            for data in invalid:
                with self.subTest(data=data[-20:]):
                    with self.assertRaises(WabbajackError):
                        apply_octodiff(source, io.BytesIO(data), target, len(output), digest(output))
                    self.assertEqual(target.read_bytes(), output)

    def test_archive_reconstruction(self):
        from Utils.ba2.extract import _make_dds_header
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "Meshes").mkdir(parents=True)
            (source / "Meshes" / "A.nif").write_bytes(b"fixture" * 10000)
            files = [{"Path": "Meshes/A.nif", "Index": 0, "Hash1": 0, "Hash2": 1,
                      "Compressed": True, "FlipCompression": False}]
            states = [{"$type": "TES3State", "VersionNumber": 256}]
            states += [{"$type": "BSAState", "Version": version, "ArchiveFlags": flags, "FileFlags": 1}
                       for version in (103, 104, 105) for flags in (0, 3, 7, 0x107)]
            states += [{"$type": "BA2State", "Type": "GNRL", "Version": version, "HasNameTable": names}
                       for version in (1, 2, 3, 7, 8) for names in (True, False)]
            for i, state in enumerate(states):
                with self.subTest(state=state):
                    rebuild_archive(root / f"archive{i}", source, state, files)
                    verify_archive(root / f"archive{i}", source, files)
            data = _make_dds_header(height=8, width=8, mip_count=3, dxgi_format=71, legacy=True) + bytes(range(48))
            self.assertEqual(len(data), 176)
            self.assertEqual(data[84:88], b"DXT1")
            self.assertEqual(struct.unpack_from("<6I", data, 8), (0xa1007, 8, 8, 32, 1, 3))
            plain = _make_dds_header(height=8, width=8, mip_count=1, dxgi_format=28, legacy=True)
            self.assertEqual(len(plain), 128)
            self.assertEqual(struct.unpack_from("<6I", plain, 8), (0x2100f, 8, 8, 32, 1, 1))
            (source / "A.dds").write_bytes(data)
            textures = [{"Path": "A.dds", "Index": 0, "Height": 8, "Width": 8, "NumMips": 3,
                         "PixelFormat": 71, "Chunks": [{"FullSz": 40, "StartMip": 0, "EndMip": 1, "Compressed": True},
                         {"FullSz": 8, "StartMip": 2, "EndMip": 2, "Compressed": False}]}]
            for version, compression in ((1, 0), (2, 0), (3, 0), (3, 3), (7, 0), (8, 0)):
                rebuild_archive(root / "texture.ba2", source, {"$type": "BA2State", "Type": "DX10",
                    "Version": version, "Compression": compression}, textures)
            extract_bethesda(root / "texture.ba2", root / "out")
            self.assertEqual((root / "out" / "a.dds").read_bytes(), data)
            broken = bytearray((root / "texture.ba2").read_bytes())
            struct.pack_into("<Q", broken, 48, len(broken) + 1)
            (root / "broken.ba2").write_bytes(broken)
            with self.assertRaises(WabbajackError):
                extract_bethesda(root / "broken.ba2", root / "bad")

    def test_paths(self):
        for path in ("../escape", "a/../b", "C:\\escape", "\\\\server\\share", "a//b", "a/./b", "a\0b"):
            with self.assertRaises(WabbajackError):
                relative_path(path)
        self.assertEqual(relative_path("Mods\\Name\\Mixed Case.txt"), "Mods/Name/Mixed Case.txt")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Mixed" / "Case").mkdir(parents=True)
            (root / "Mixed" / "case").mkdir()
            (root / "Mixed" / "case" / "File.txt").write_bytes(b"case")
            self.assertEqual(source_path(root, "Mixed/Case/file.txt").read_bytes(), b"case")
            (root / "inside").mkdir()
            (root / "inside" / "link").symlink_to(root)
            with self.assertRaises(WabbajackError):
                within(root / "inside", "link/outside")
            installation = root / ".wabbajack" / "list"
            installation.mkdir(parents=True)
            (installation / "root").symlink_to(root / "inside")
            with self.assertRaises(WabbajackError):
                Store(installation, root)

    def test_update_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / ".wabbajack" / "list"
            old, new = root / "old", root / "new"
            old.write_bytes(b"original")
            new.write_bytes(b"replacement")
            def desired(path):
                return {"root/mods/A/a.txt": {"source": str(path), "authored_hash": file_hash(path), "signature": path.name}}
            store = Store(directory, root)
            with store.exclusive():
                current, conflicts = store.preview(desired(old))
                self.assertFalse(conflicts)
                store.publish(desired(old), {}, current, {"version": "1"})
            store.close()
            child = os.fork()
            if child == 0:
                store = Store(directory, root)
                with store.exclusive():
                    current, _ = store.preview(desired(new))
                    original_copy = store._copy
                    def interrupted(source, target, **kwargs):
                        original_copy(source, target, **kwargs)
                        if target == store.root / "mods/A/a.txt":
                            os._exit(91)
                    store._copy = interrupted
                    store.publish(desired(new), {}, current, {"version": "2"})
                os._exit(92)
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 91)
            store = Store(directory, root)
            with store.exclusive():
                target = store.root / "mods/A/a.txt"
                self.assertEqual(target.read_bytes(), b"original")
                self.assertEqual(store.get("version"), "1")
                target.write_bytes(b"my changes")
                current, conflicts = store.preview(desired(new))
                self.assertEqual(len(conflicts), 1)
                store.publish(desired(new), {conflicts[0].path: "keep"}, current, {"version": "2"})
                self.assertEqual(target.read_bytes(), b"my changes")
            store.close()


if __name__ == "__main__":
    unittest.main()
