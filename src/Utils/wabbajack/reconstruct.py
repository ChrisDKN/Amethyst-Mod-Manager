from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import threading
import time
import zipfile
from collections import Counter
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .archive_build import rebuild_archive
from .hashes import canonical_hash, file_hash
from .manifest import archive_path, optional_game_file_directives, required_directives
from .patches import apply_octodiff
from .paths import WabbajackError, relative_path, within, source_path, source_candidates, check_tree
from .diagnostics import emit, emit_exception


def _patch_source(request, directive, source, root, member, target, stop,
                  progress, log=None, aliases=None):
    def candidates():
        if source:
            yield source
        if root is not None:
            yield from (p for p in source_candidates(root, member, aliases=aliases) if p != source)
    error = None
    attempted = 0
    for candidate in candidates():
        if stop.is_set():
            raise InterruptedError("Installation stopped")
        if not candidate.is_file():
            continue
        attempted += 1
        if directive.data.get("FromHash") is not None:
            actual = file_hash(candidate, stop)
            expected = canonical_hash(directive.data["FromHash"])
            if actual != expected:
                emit(log, "reconstruct.patch.source_rejected", output=directive.path,
                     source=candidate, actual_hash=actual, expected_hash=expected)
                continue
        emit(log, "reconstruct.patch.attempt", output=directive.path,
             source=candidate, attempt=attempted)
        try:
            with zipfile.ZipFile(request.package.path) as archive, archive.open(directive.data["PatchID"]) as patch:
                apply_octodiff(candidate, patch, target, directive.size,
                               directive.hash, stop, progress=progress, log=log)
            return
        except WabbajackError as exc:
            error = exc
            emit(log, "reconstruct.patch.attempt_failed", output=directive.path,
                 source=candidate, attempt=attempted,
                 exception_type=type(exc).__name__, exception=str(exc))
    raise WabbajackError(f"No archive source produced the verified patch output for {directive.path}: {error or 'required source hash not found'}")


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


def _open_zip_member(source, item, log):
    try:
        return source.open(item)
    except zipfile.BadZipFile:
        with open(source.filename, "rb") as stream:
            stream.seek(item.header_offset)
            header = stream.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise
            flags = struct.unpack_from("<H", header, 6)[0]
            length = struct.unpack_from("<H", header, 26)[0]
            name = stream.read(length).decode("utf-8" if flags & 0x800 else "cp437")
        if name == item.orig_filename or relative_path(name) != relative_path(item.orig_filename):
            raise
        from copy import copy
        compatible = copy(item)
        compatible.orig_filename = name
        emit(log, "extract.zip.separators_normalized", archive=source.filename,
             member=item.orig_filename, header_name=name)
        return source.open(compatible)


def extract_safe(archive: Path, target: Path, stop, log, budget=None, progress=None):
    started = time.monotonic()
    emit(log, "extract.started", archive=archive, target=target,
         compressed_bytes=archive.stat().st_size)
    target.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            items = source.infolist()
            emit(log, "extract.format", archive=archive, format="zip",
                 members=len(items), expanded_bytes=sum(item.file_size for item in items),
                 compression_methods=sorted({item.compress_type for item in items}),
                 encrypted=sum(bool(item.flag_bits & 1) for item in items))
            entries, seen = [], {}
            for item in items:
                name = relative_path(item.orig_filename.rstrip("/\\"))
                path = within(target, name)
                directory = (item.orig_filename.endswith(("/", "\\"))
                             or stat.S_ISDIR(item.external_attr >> 16)
                             or bool(item.external_attr & 0x10))
                if name in seen and not (directory and seen[name]):
                    raise WabbajackError(f"Conflicting ZIP destination: {name}")
                if directory and item.file_size:
                    raise WabbajackError(f"ZIP directory contains file data: {name}")
                seen[name] = directory
                entries.append((item, path, directory))
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise WabbajackError("Symbolic links are not supported in source archives")
                if item.flag_bits & 1:
                    raise WabbajackError(f"Password-protected source archive is unsupported: {archive.name}")
            native_methods = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                              zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
            if all(item.compress_type in native_methods for item in items):
                total = sum(i.file_size for i in items)
                completed = 0
                if budget:
                    budget(total)
                for item, path, directory in entries:
                    if directory:
                        path.mkdir(parents=True, exist_ok=True)
                        continue
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
                    with _open_zip_member(source, item, log) as incoming, atomic_writer(path, "wb", encoding=None) as out:
                        while data := incoming.read(1024 * 1024):
                            if stop.is_set():
                                raise InterruptedError("Installation stopped")
                            out.write(data)
                            completed += len(data)
                            if progress:
                                progress(completed, total)
                    path.chmod(((item.external_attr >> 16) & 0o777) | 0o600)
                emit(log, "extract.completed", archive=archive, target=target,
                     format="zip-native", members=len(items), expanded_bytes=total,
                     elapsed_seconds=round(time.monotonic() - started, 3))
                return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            items = source.getmembers()
            total = sum(i.size for i in items)
            emit(log, "extract.format", archive=archive, format="tar",
                 members=len(items), expanded_bytes=total)
            completed = 0
            if budget:
                budget(total)
            for item in source:
                within(target, item.name.rstrip("/"))
                if not item.isfile() and not item.isdir():
                    raise WabbajackError("Special files are not supported in source archives")
            for item in items:
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
                            completed += len(data)
                            if progress:
                                progress(completed, total)
                    path.chmod((item.mode & 0o777) | 0o600)
        emit(log, "extract.completed", archive=archive, target=target,
             format="tar-native", members=len(items), expanded_bytes=total,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return
    tool = next((shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za") if shutil.which(n)), None)
    if not tool:
        raise WabbajackError("7-Zip is required to inspect and extract this archive")
    result = subprocess.run([tool, "l", "-slt", "-ba", "--", str(archive)],
                            capture_output=True, text=True, timeout=120)
    emit(log, "extract.7zip.inspect", archive=archive, tool=tool,
         exit_code=result.returncode, stdout_tail=result.stdout[-2000:],
         stderr_tail=result.stderr[-2000:])
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
    if not _extract_archive(str(archive), str(target), log, stop, errors,
                            progress_cb=(lambda pct: progress(pct, 100)) if progress else None):
        raise WabbajackError(f"Extraction failed: {archive.name}: {'; '.join(errors)}")
    check_tree(target)
    emit(log, "extract.completed", archive=archive, target=target,
         format="7zip", expanded_bytes=expanded,
         elapsed_seconds=round(time.monotonic() - started, 3))


class _ArchiveProgress:
    def __init__(self, directives, emit):
        sources = set()
        for directive in directives:
            _, members = archive_path(directive.data)
            sources.update(tuple(m.casefold() for m in members[:depth]) for depth in range(len(members)))
        extraction_share = 0.5 if sources else 0.0
        output_size = sum(max(1, d.output_size) for d in directives)
        self.weights = {key: extraction_share / len(sources) for key in sources}
        self.weights.update({d.index: (1 - extraction_share) * max(1, d.output_size) / output_size
                             for d in directives})
        self.fractions = {}
        self.completed = 0.0
        self.last_value = 0
        self.last_emit = time.monotonic()
        self.finished = False
        self.lock = threading.Lock()
        self.emit = emit
        emit(0, 1000)

    def update(self, key, current, total):
        if total <= 0:
            return
        with self.lock:
            if self.finished:
                return
            previous = self.fractions.get(key, 0.0)
            fraction = max(previous, min(1.0, current / total))
            self.fractions[key] = fraction
            self.completed += (fraction - previous) * self.weights[key]
            value = min(999, int(self.completed * 1000))
            now = time.monotonic()
            if value > self.last_value and now - self.last_emit >= 0.1:
                self.last_value, self.last_emit = value, now
                self.emit(value, 1000)

    def finish(self):
        with self.lock:
            self.finished = True
            self.emit(1000, 1000)


class Reconstruction:
    def __init__(self, request, store, callbacks, control):
        from .adapters import adapter_for
        self.request, self.store, self.cb, self.control = request, store, callbacks, control
        self.adapter = adapter_for(
            request.package, getattr(request, "game", None),
            store=getattr(request, "setup_options", {}).get("store", ""),
            log=callbacks.on_log)
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
        self._reuse_counts = {"staged": 0, "installed": 0, "rejected": 0}
        emit(self.cb.on_log, "reconstruct.initialized",
             adapter=type(self.adapter).__name__, output=self.output,
             source_archives=len(self.by_archive), prior_outputs=len(self.old),
             directives=len(request.package.directives))

    def _reuse(self, d):
        if d.path in self.results:
            return True
        found = self._find_reusable(d)
        if found is None:
            return False
        self._accept_reuse(d, found)
        return True

    def _find_reusable(self, d, completed=None):
        def verified(path, expected):
            try:
                info = path.stat()
            except FileNotFoundError:
                return None
            if stat.S_ISREG(info.st_mode) and file_hash(path, self.control.stop) == expected:
                return self.store._stamp(info)
            return None
        sig = signature(d, self.request)
        target = within(self.output, d.path)
        if completed is None:
            cached_hash = self.store.completed(d.path, sig)
        else:
            row = completed.get(d.path)
            cached_hash = row[1] if row and row[0] == sig else None
        if cached_hash and (stamp := verified(target, cached_hash)):
            return target, sig, cached_hash, "staged", stamp
        if cached_hash:
            with self._lock:
                self._reuse_counts["rejected"] += 1
            emit(self.cb.on_log, "reconstruct.reuse.rejected", path=d.path,
                 source="staged", expected_hash=cached_hash,
                 exists=target.is_file())
        for rel in dict.fromkeys((self.adapter.installed_path(d.path), d.path)):
            if rel is None:
                continue
            old = self.old.get("root/" + rel)
            if not old or old["signature"] != sig:
                continue
            existing = within(self.store.root, rel)
            if stamp := verified(existing, old["authored_hash"]):
                return existing, sig, old["authored_hash"], "installed", stamp
            with self._lock:
                self._reuse_counts["rejected"] += 1
            emit(self.cb.on_log, "reconstruct.reuse.rejected", path=d.path,
                 source="installed", expected_hash=old["authored_hash"],
                 exists=existing.is_file())
        return None

    def _accept_reuse(self, d, found):
        source, sig, digest, kind, stamp = found
        if self.control.stop.is_set():
            raise InterruptedError("Installation stopped")
        if (self.store._stamp(source.stat()) != stamp
                and file_hash(source, self.control.stop) != digest):
            raise WabbajackError(f"Reusable file changed while preparing archives: {d.path}")
        if kind == "installed":
            target = within(self.output, d.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            try:
                os.link(source, target)
            except OSError:
                self.store._copy(source, target, self.control.stop)
        else:
            target = source
        self._record(d, target, sig, digest, persist=kind != "staged")
        with self._lock:
            self._reuse_counts[kind] += 1

    def _installed_reuse_candidate(self, d):
        if not self.old:
            return False
        return any("root/" + rel in self.old for rel in dict.fromkeys(
            (self.adapter.installed_path(d.path), d.path)) if rel is not None)

    def _record(self, directive, path, sig=None, actual=None, *, persist=True):
        sig = sig or signature(directive, self.request)
        actual = actual or file_hash(path, self.control.stop)
        if directive.deterministic and (actual != directive.output_hash or path.stat().st_size != directive.output_size):
            raise WabbajackError(f"Output failed verification: {directive.path}")
        if persist:
            self.store.record_completed(directive.path, sig, actual)
        with self._lock:
            self._result_keys.add(directive.path.casefold())
            self.results[directive.path] = {"source": str(path), "authored_hash": actual, "signature": sig}

    def needed_archives(self, progress=None):
        from .verification import VerificationCache, cached_files, parallel_verify, verification_scope
        started = time.monotonic()
        completed = self.store.completed_outputs()
        candidates = [d for d in self.request.package.directives
                      if d.path not in self.results
                      and (d.path in completed or self._installed_reuse_candidate(d))]
        special = [d for d in candidates if d.kind in {"CreateBSA", "MergedPatch"}]
        count, total = 0, len(candidates)
        def notify(detail):
            if progress:
                progress("Verifying reusable installation files", count, total, detail)
        def verify(d):
            return d, self._find_reusable(d, completed)
        def reuse(directives):
            nonlocal count
            pending = {d.path: d for d in directives}
            staged = [path for path in pending if path in completed]
            for path, digest, info in cached_files(self.output, staged, verify=True):
                d = pending[path]
                sig = signature(d, self.request)
                if completed[path] == (sig, digest):
                    self._accept_reuse(d, (self.output / path, sig, digest,
                                          "staged", self.store._stamp(info)))
                    del pending[path]
                    count += 1
                    notify(d.path)
            for d, found in parallel_verify(verify, pending.values(), self.control.stop,
                                            size=lambda d: d.output_size):
                if found is not None:
                    self._accept_reuse(d, found)
                count += 1
                notify(d.path)
        notify("Checking previously completed files")
        cache = VerificationCache(directory=self.request.downloads / ".wabbajack-checks")
        with verification_scope(cache, self.control.stop, log=self.cb.on_log):
            reuse(special)
            required = required_directives(self.request.package, self.results,
                                           optional_game_file_directives(self.request.package))
            self._skipped_dependencies = {d.path for d in self.request.package.directives if d.path not in required}
            remaining = [d for d in candidates if d.kind not in {"CreateBSA", "MergedPatch"}]
            count += sum(d.path in self._skipped_dependencies for d in remaining)
            reuse(d for d in remaining if d.path not in self._skipped_dependencies)
        archives = [a for key, a in self.request.package.archives.items()
                    if any(d.path not in self._skipped_dependencies and d.path not in self.results
                           for d in self.by_archive.get(key, []))]
        self.store.flush_completed()
        notify(f"Reusing {len(self.results):,} files; {len(archives):,} archives needed")
        emit(self.cb.on_log, "reconstruct.plan", required_archives=len(archives),
             required_bytes=sum(item.size for item in archives),
             skipped_dependencies=len(self._skipped_dependencies),
             reuse_candidates=total, elapsed_seconds=round(time.monotonic() - started, 3),
             reused=self._reuse_counts)
        return archives

    def install_archive(self, archive, path):
        started = time.monotonic()
        row = self._row(archive)
        self.cb.on_extract_add(row, archive.name)
        scratch = self.store.work / "extract" / hashlib.sha256(archive.key.encode()).hexdigest()
        extracted = {}
        aliases = {}
        reserved = 0
        current_directive = None
        source_extraction_seconds = 0.0
        hardlinked_outputs = 0
        copied_outputs = 0
        emit(self.cb.on_log, "reconstruct.archive.started", archive=archive.name,
             kind=archive.kind, source=path, source_bytes=archive.size,
             directives=len(self.by_archive.get(archive.key, [])), scratch=scratch)
        def reserve(count):
            nonlocal reserved
            with self._lock:
                free = shutil.disk_usage(scratch).free
                if count + self._temporary_bytes + 512 * 1024 ** 2 > free:
                    raise WabbajackError(f"Not enough temporary space to extract {archive.name}; free space and resume")
                self._temporary_bytes += count
                reserved += count
                emit(self.cb.on_log, "reconstruct.temporary_reserved",
                     archive=archive.name, bytes=count,
                     archive_reserved_bytes=reserved,
                     total_reserved_bytes=self._temporary_bytes,
                     free_bytes=free)
        try:
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch.mkdir(parents=True)
            planning_started = time.monotonic()
            directives = []
            archive_directives = self.by_archive.get(archive.key, [])
            completed_candidates = self.store.completed_paths(
                d.path for d in archive_directives)
            reuse_candidates = 0
            for d in archive_directives:
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                if d.path in self._skipped_dependencies:
                    continue
                reusable = (d.path in self.results or
                            d.path in completed_candidates or
                            self._installed_reuse_candidate(d))
                reuse_candidates += int(reusable)
                if not reusable or not self._reuse(d):
                    directives.append(d)
            planning_seconds = time.monotonic() - planning_started
            progress = _ArchiveProgress(directives,
                lambda current, total: self.cb.on_extract_update(row, current, total))
            emit(self.cb.on_log, "reconstruct.archive.outputs", archive=archive.name,
                 required_directives=len(directives),
                 reuse_candidates=reuse_candidates,
                 kinds=dict(Counter(d.kind for d in directives)))
            for d in directives:
                current_directive = d
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                _, members = archive_path(d.data)
                source = path
                for depth, member in enumerate(members):
                    cache_key = str(source)
                    root = extracted.get(cache_key)
                    if root is None:
                        extraction_started = time.monotonic()
                        key = tuple(m.casefold() for m in members[:depth])
                        extracting = lambda current, total, key=key: progress.update(key, current, total)
                        root = scratch / str(len(extracted))
                        emit(self.cb.on_log, "reconstruct.source_extract.started",
                             archive=archive.name, source=source, member=member,
                             depth=depth, target=root,
                             format="bethesda" if source.suffix.lower() in {".bsa", ".ba2"}
                             else "general")
                        if source.suffix.lower() in {".bsa", ".ba2"}:
                            from .archive_io import records
                            reserve(sum(len(header) + sum(c[2] for c in segments) for _, header, segments in records(source, allow_case_variants=True)))
                            aliases[cache_key] = self._extract_bethesda(source, root, extracting)
                        else:
                            extract_safe(source, root, self.control.stop, self.cb.on_log, reserve, extracting)
                        progress.update(key, 1, 1)
                        extracted[cache_key] = root
                        source_extraction_seconds += time.monotonic() - extraction_started
                        emit(self.cb.on_log, "reconstruct.source_extract.completed",
                             archive=archive.name, source=source, depth=depth,
                             target=root,
                             elapsed_seconds=round(
                                 time.monotonic() - extraction_started, 3))
                    expected, size = "", None
                    if depth == len(members) - 1:
                        if d.kind == "FromArchive":
                            expected, size = d.output_hash, d.output_size
                        elif d.kind == "PatchedFromArchive":
                            expected = d.data.get("FromHash", "")
                    try:
                        source = source_path(
                            root, member, expected=expected, size=size,
                            stop=self.control.stop, aliases=aliases.get(cache_key))
                    except WabbajackError:
                        if d.kind == "PatchedFromArchive" and depth == len(members) - 1:
                            source = None
                        else:
                            raise
                target = within(self.output, d.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                linked = None
                def copying(current, total, key=d.index):
                    progress.update(key, current * 9, total * 10)
                if d.kind == "PatchedFromArchive":
                    _patch_source(self.request, d, source, root if members else None,
                                  members[-1] if members else "", target,
                                  self.control.stop, copying, self.cb.on_log,
                                  aliases.get(cache_key) if members else None)
                elif d.kind == "TransformedTexture":
                    from .textures import transform_texture
                    transform_texture(self.request, source, target,
                                      d.data["ImageState"], self.control.stop,
                                      log=self.cb.on_log)
                else:
                    linked = self.store._stage(
                        source, target, stop=self.control.stop, progress=copying,
                        size=d.output_size)
                try:
                    actual = (canonical_hash(d.output_hash)
                              if d.kind == "FromArchive" and members and linked
                              else None)
                    self._record(d, target, actual=actual)
                except WabbajackError:
                    if d.kind != "FromArchive" or not members:
                        raise
                    alternate = source_path(root, members[-1], expected=d.output_hash,
                                            size=d.output_size, stop=self.control.stop,
                                            aliases=aliases.get(cache_key))
                    if alternate == source:
                        raise
                    linked = self.store._stage(
                        alternate, target, stop=self.control.stop, progress=copying,
                        size=d.output_size)
                    self._record(d, target, actual=(
                        canonical_hash(d.output_hash) if linked else None))
                if linked is True:
                    hardlinked_outputs += 1
                elif linked is False:
                    copied_outputs += 1
                progress.update(d.index, 1, 1)
            progress.finish()
            self.store.flush_completed()
            elapsed = time.monotonic() - started
            emit(self.cb.on_log, "reconstruct.archive.completed", archive=archive.name,
                 outputs=len(directives), extracted_sources=len(extracted),
                 output_bytes=sum(d.output_size for d in directives),
                 hardlinked_outputs=hardlinked_outputs,
                 copied_outputs=copied_outputs, reserved_bytes=reserved,
                 planning_seconds=round(planning_seconds, 3),
                 source_extraction_seconds=round(source_extraction_seconds, 3),
                 output_processing_seconds=round(max(
                     0.0, elapsed - source_extraction_seconds -
                     planning_seconds), 3),
                 elapsed_seconds=round(elapsed, 3))
        except BaseException as exc:
            emit_exception(self.cb.on_log, "reconstruct.archive.failed", exc,
                           archive=archive.name, source=path,
                           directive=current_directive.path if current_directive else None,
                           directive_kind=current_directive.kind
                           if current_directive else None,
                           elapsed_seconds=round(time.monotonic() - started, 3))
            raise
        finally:
            cleanup_started = time.monotonic()
            shutil.rmtree(scratch, ignore_errors=True)
            emit(self.cb.on_log, "reconstruct.scratch.cleaned",
                 archive=archive.name, target=scratch,
                 elapsed_seconds=round(time.monotonic() - cleanup_started, 3))
            with self._lock:
                self._temporary_bytes -= reserved
            self.cb.on_extract_remove(row)

    def _row(self, archive):
        return list(self.request.package.archives).index(archive.key) + 1

    def _extract_bethesda(self, source, root, progress=None):
        from .archive_io import extract_bethesda
        started = time.monotonic()
        emit(self.cb.on_log, "extract.bethesda.started", archive=source,
             target=root)
        aliases = {}
        extract_bethesda(source, root, self.control.stop, progress=progress, aliases=aliases)
        emit(self.cb.on_log, "extract.bethesda.completed", archive=source,
             target=root, legacy_path_aliases=len(aliases),
             elapsed_seconds=round(time.monotonic() - started, 3))
        return aliases

    def finish(self, progress=None):
        directives = self.request.package.directives
        inline = [d for d in directives if d.kind in {"InlineFile", "RemappedInlineFile", "PropertyFile", "ArchiveMeta"}]
        started = time.monotonic()
        emit(self.cb.on_log, "reconstruct.finish.started", inline=len(inline),
             special=sum(d.kind in {"CreateBSA", "MergedPatch"} for d in directives),
             existing_results=len(self.results))
        with zipfile.ZipFile(self.request.package.path) as archive:
            for index, d in enumerate(inline):
                if progress:
                    progress("Preparing provided files", index, len(inline), d.path)
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                if d.path in self._skipped_dependencies or self._reuse(d):
                    continue
                emit(self.cb.on_log, "reconstruct.inline.started", path=d.path,
                     kind=d.kind, source_data_id=d.data.get("SourceDataID"))
                target = within(self.output, d.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if d.kind == "RemappedInlineFile":
                    data = archive.read(d.data["SourceDataID"]).decode("utf-8-sig")
                    with atomic_writer(target) as out:
                        out.write(remap(data, self.request))
                else:
                    with archive.open(d.data["SourceDataID"]) as source, atomic_writer(target, "wb", encoding=None) as out:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                self._record(d, target)
                emit(self.cb.on_log, "reconstruct.inline.completed", path=d.path,
                     kind=d.kind, remapped=d.kind == "RemappedInlineFile",
                     bytes=target.stat().st_size,
                     hash=self.results[d.path]["authored_hash"])
        if progress:
            progress("Preparing provided files", len(inline), len(inline), "Provided files verified")
        pending = [d for d in directives if d.kind in {"CreateBSA", "MergedPatch"}]
        total = len(pending)
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
                special_started = time.monotonic()
                emit(self.cb.on_log, "reconstruct.special.started", path=d.path,
                     kind=d.kind, dependencies=len(required),
                     dependency_sample=required[:50],
                     dependency_sample_truncated=len(required) > 50)
                if progress:
                    progress("Reconstructing archives and patches", total - len(pending), total, d.path)
                if d.kind == "CreateBSA":
                    rebuild_archive(target, source_path(self.output, base),
                                    d.data["State"], d.data["FileStates"],
                                    self.control.stop, log=self.cb.on_log)
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
                            apply_octodiff(basis, patch, target, d.size, d.hash,
                                           self.control.stop, log=self.cb.on_log)
                    finally:
                        basis.unlink(missing_ok=True)
                self._record(d, target)
                emit(self.cb.on_log, "reconstruct.special.completed", path=d.path,
                     kind=d.kind, bytes=target.stat().st_size,
                     hash=self.results[d.path]["authored_hash"],
                     elapsed_seconds=round(time.monotonic() - special_started, 3))
                pending.remove(d)
                progressed = True
                if progress:
                    progress("Reconstructing archives and patches", total - len(pending), total, d.path)
            if not progressed:
                raise WabbajackError("Required reconstruction dependencies have not completed")
        missing = [d.path for d in directives if d.path not in self.results and d.path not in self._skipped_dependencies]
        if missing:
            raise WabbajackError(f"Missing required outputs: {', '.join(missing[:8])}")
        removed = 0
        for path in self.output.rglob("*"):
            if path.is_file() and path.relative_to(self.output).as_posix() not in self.results:
                path.unlink()
                removed += 1
        result = {"root/" + path: row for path, row in self.results.items()
                  if path.split("/")[0].casefold() != "temp_bsa_files"}
        emit(self.cb.on_log, "reconstruct.finish.completed", outputs=len(result),
             temporary_outputs=len(self.results) - len(result),
             stale_files_removed=removed, reused=self._reuse_counts,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return result
