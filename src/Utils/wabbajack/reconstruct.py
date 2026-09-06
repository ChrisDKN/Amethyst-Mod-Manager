from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
import zipfile
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .archive_build import rebuild_archive
from .hashes import canonical_hash, file_hash
from .manifest import archive_path, required_directives
from .patches import apply_octodiff
from .paths import WabbajackError, relative_path, within, source_path, check_tree


def signature(directive, request):
    data = {"directive": directive.data, "root": str(request.directory / "root"),
            "games": {k: str(v) for k, v in request.game_roots.items()},
            "downloads": str(request.downloads), "format": 1}
    if directive.embedded_hash:
        data["embedded_hash"] = directive.embedded_hash
    if directive.kind == "RemappedInlineFile":
        from .runtime import windows_path
        data["windows"] = [windows_path(request.game, p) for p in
                           [request.directory / "root", request.downloads, *(request.game_roots[k] for k in sorted(request.game_roots))]]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def remap(text, request):
    from .runtime import windows_path
    paths = {"GAME": request.game_roots.get(request.package.game, request.game.get_game_path()),
             "MO2": request.directory / "root", "DOWNLOAD": request.downloads}
    for name, path in paths.items():
        windows = windows_path(request.game, path)
        for style, value in (("BACK", windows), ("DOUBLE_BACK", windows.replace("\\", "\\\\")),
                             ("FORWARD", windows.replace("\\", "/"))):
            text = text.replace("{--||" + name + "_PATH_MAGIC_" + style + "||--}", value)
    return text


def extract_safe(archive: Path, target: Path, stop, log, budget=None):
    target.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                within(target, item.filename.rstrip("/"))
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise WabbajackError("Symbolic links are not supported in source archives")
                if item.flag_bits & 1:
                    raise WabbajackError(f"Password-protected source archive is unsupported: {archive.name}")
            if all(i.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA} for i in source.infolist()):
                if budget:
                    budget(sum(i.file_size for i in source.infolist()))
                for item in source.infolist():
                    path = within(target, item.filename.rstrip("/"))
                    if item.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                        continue
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
                    with source.open(item) as incoming, atomic_writer(path, "wb", encoding=None) as out:
                        while data := incoming.read(1024 * 1024):
                            if stop.is_set():
                                raise InterruptedError("Installation stopped")
                            out.write(data)
                    path.chmod(((item.external_attr >> 16) & 0o777) | 0o600)
                return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            if budget:
                budget(sum(i.size for i in source.getmembers()))
            for item in source:
                within(target, item.name.rstrip("/"))
                if not item.isfile() and not item.isdir():
                    raise WabbajackError("Special files are not supported in source archives")
            for item in source.getmembers():
                if stop.is_set():
                    raise InterruptedError("Installation stopped")
                path = within(target, item.name.rstrip("/"))
                if item.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    with source.extractfile(item) as incoming, atomic_writer(path, "wb", encoding=None) as out:
                        while data := incoming.read(1024 * 1024):
                            if stop.is_set():
                                raise InterruptedError("Installation stopped")
                            out.write(data)
                    path.chmod((item.mode & 0o777) | 0o600)
        return
    tool = next((shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za") if shutil.which(n)), None)
    if not tool:
        raise WabbajackError("7-Zip is required to inspect and extract this archive")
    result = subprocess.run([tool, "l", "-slt", "-ba", "--", str(archive)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise WabbajackError(f"Cannot inspect archive: {archive.name}: {result.stderr[:300]}")
    expanded = 0
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(" = ")
        if key == "Path" and sep:
            within(target, value.rstrip("/"))
        if key in {"Symbolic Link", "Hard Link"} and value:
            raise WabbajackError("Links are not supported in source archives")
        if key == "Size" and value.isdecimal():
            expanded += int(value)
    if budget:
        budget(expanded)
    from Utils.mods.install import _extract_archive
    errors = []
    if not _extract_archive(str(archive), str(target), log, stop, errors):
        raise WabbajackError(f"Extraction failed: {archive.name}: {'; '.join(errors)}")
    check_tree(target)


class Reconstruction:
    def __init__(self, request, store, callbacks, control):
        self.request, self.store, self.cb, self.control = request, store, callbacks, control
        self.output = store.work / "output"
        self.output.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.results = {}
        self._result_keys = set()
        self._temporary_bytes = 0
        self.by_archive = {}
        for d in request.package.directives:
            if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
                self.by_archive.setdefault(archive_path(d.data)[0], []).append(d)
        self.old = store.outputs()
        self._skipped_dependencies = set()

    def _reuse(self, d):
        if d.path in self.results:
            return True
        sig = signature(d, self.request)
        target = within(self.output, d.path)
        cached_hash = self.store.completed(d.path, sig)
        if cached_hash and target.is_file() and file_hash(target, self.control.stop) == cached_hash:
            self._record(d, target, sig, cached_hash)
            return True
        old = self.old.get("root/" + d.path)
        existing = within(self.store.root, d.path)
        if old and old["signature"] == sig and existing.is_file() and file_hash(existing, self.control.stop) == old["authored_hash"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            try:
                os.link(existing, target)
            except OSError:
                self.store._copy(existing, target)
            self._record(d, target, sig, old["authored_hash"])
            return True
        return False

    def _record(self, directive, path, sig=None, actual=None):
        sig = sig or signature(directive, self.request)
        actual = actual or file_hash(path, self.control.stop)
        if directive.deterministic and (actual != directive.output_hash or path.stat().st_size != directive.output_size):
            raise WabbajackError(f"Output failed verification: {directive.path}")
        self.store.record_completed(directive.path, sig, actual)
        with self._lock:
            self._result_keys.add(directive.path.casefold())
            self.results[directive.path] = {"source": str(path), "authored_hash": actual, "signature": sig}

    def needed_archives(self):
        reused = set()
        for directive in self.request.package.directives:
            if directive.kind not in {"CreateBSA", "MergedPatch"}:
                continue
            if self._reuse(directive):
                reused.add(directive.path)
        required = required_directives(self.request.package, reused)
        self._skipped_dependencies = {d.path for d in self.request.package.directives if d.path not in required}
        return [a for key, a in self.request.package.archives.items()
                if any(d.path not in self._skipped_dependencies and not self._reuse(d) for d in self.by_archive.get(key, []))]

    def install_archive(self, archive, path):
        self.cb.on_extract_add(self._row(archive), archive.name)
        scratch = self.store.work / "extract" / hashlib.sha256(archive.key.encode()).hexdigest()
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True)
        extracted = {}
        reserved = 0
        def reserve(count):
            nonlocal reserved
            with self._lock:
                free = shutil.disk_usage(scratch).free
                if count + self._temporary_bytes + 512 * 1024 ** 2 > free:
                    raise WabbajackError(f"Not enough temporary space to extract {archive.name}; free space and resume")
                self._temporary_bytes += count
                reserved += count
        try:
            for d in self.by_archive.get(archive.key, []):
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                if d.path in self._skipped_dependencies or self._reuse(d):
                    continue
                _, members = archive_path(d.data)
                source = path
                for depth, member in enumerate(members):
                    cache_key = str(source)
                    root = extracted.get(cache_key)
                    if root is None:
                        root = scratch / str(len(extracted))
                        if source.suffix.lower() in {".bsa", ".ba2"}:
                            from .archive_io import records
                            reserve(sum(len(header) + sum(c[2] for c in segments) for _, header, segments in records(source)))
                            self._extract_bethesda(source, root)
                        else:
                            extract_safe(source, root, self.control.stop, self.cb.on_log, reserve)
                        extracted[cache_key] = root
                    source = source_path(root, member)
                target = within(self.output, d.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if d.kind == "PatchedFromArchive":
                    if d.data.get("FromHash") is not None and file_hash(source, self.control.stop) != canonical_hash(d.data["FromHash"]):
                        raise WabbajackError(f"Patch source has changed: {d.path}")
                    with zipfile.ZipFile(self.request.package.path) as z, z.open(d.data["PatchID"]) as patch:
                        apply_octodiff(source, patch, target, d.size, d.hash, self.control.stop)
                elif d.kind == "TransformedTexture":
                    from .textures import transform_texture
                    transform_texture(self.request, source, target, d.data["ImageState"], self.control.stop)
                else:
                    self.store._copy(source, target, stop=self.control.stop)
                self._record(d, target)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            with self._lock:
                self._temporary_bytes -= reserved
            self.cb.on_extract_remove(self._row(archive))

    def _row(self, archive):
        return list(self.request.package.archives).index(archive.key) + 1

    def _extract_bethesda(self, source, root):
        from .archive_io import extract_bethesda
        extract_bethesda(source, root, self.control.stop)

    def finish(self):
        directives = self.request.package.directives
        with zipfile.ZipFile(self.request.package.path) as archive:
            for d in directives:
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                if d.kind not in {"InlineFile", "RemappedInlineFile", "PropertyFile", "ArchiveMeta"}:
                    continue
                if d.path in self._skipped_dependencies or self._reuse(d):
                    continue
                target = within(self.output, d.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if d.kind == "RemappedInlineFile":
                    data = archive.read(d.data["SourceDataID"]).decode("utf-8-sig")
                    target.write_text(remap(data, self.request), encoding="utf-8")
                else:
                    with archive.open(d.data["SourceDataID"]) as source, atomic_writer(target, "wb", encoding=None) as out:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                self._record(d, target)
        pending = [d for d in directives if d.kind in {"CreateBSA", "MergedPatch"}]
        while pending:
            progressed = False
            for d in pending[:]:
                if d.path in self._skipped_dependencies or self._reuse(d):
                    pending.remove(d)
                    progressed = True
                    continue
                target = within(self.output, d.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if d.kind == "CreateBSA":
                    base = "TEMP_BSA_FILES/" + relative_path(d.data["TempID"])
                    required = [f"{base}/{relative_path(f['Path'])}" for f in d.data["FileStates"]]
                else:
                    required = [relative_path(s["RelativePath"]) for s in d.data["Sources"]]
                if not all(p.casefold() in self._result_keys for p in required):
                    continue
                self.cb.on_status(f"Reconstructing {d.path}")
                if d.kind == "CreateBSA":
                    rebuild_archive(target, source_path(self.output, base), d.data["State"], d.data["FileStates"], self.control.stop)
                else:
                    basis = self.store.work / f"merge-{d.index}.tmp"
                    try:
                        with basis.open("wb") as output:
                            for item in d.data["Sources"]:
                                source = source_path(self.output, item["RelativePath"])
                                if file_hash(source, self.control.stop) != canonical_hash(item["Hash"]):
                                    raise WabbajackError(f"Merge source failed verification: {item['RelativePath']}")
                                with source.open("rb") as incoming:
                                    shutil.copyfileobj(incoming, output, 1024 * 1024)
                        with zipfile.ZipFile(self.request.package.path) as z, z.open(d.data["PatchID"]) as patch:
                            apply_octodiff(basis, patch, target, d.size, d.hash, self.control.stop)
                    finally:
                        basis.unlink(missing_ok=True)
                self._record(d, target)
                pending.remove(d)
                progressed = True
            if not progressed:
                raise WabbajackError("Required reconstruction dependencies have not completed")
        missing = [d.path for d in directives if d.path not in self.results and d.path not in self._skipped_dependencies]
        if missing:
            raise WabbajackError(f"Missing required outputs: {', '.join(missing[:8])}")
        for path in self.output.rglob("*"):
            if path.is_file() and path.relative_to(self.output).as_posix() not in self.results:
                path.unlink()
        return {"root/" + path: row for path, row in self.results.items()
                if path.split("/")[0].casefold() != "temp_bsa_files"}
