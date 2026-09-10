from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import threading
import time
import zipfile
import zlib
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .archive_build import rebuild_archive
from .archive_io import utf8_chunks
from .hashes import XXHash, canonical_hash, file_hash
from .manifest import archive_path, optional_game_file_directives, required_directives
from .patches import apply_octodiff
from .paths import WabbajackError, relative_path, within, source_path, source_candidates
from .diagnostics import emit, emit_exception


_PATH_MAGIC = tuple(
    "{--||" + name + "_PATH_MAGIC_" + style + "||--}"
    for name in ("GAME", "MO2", "DOWNLOAD")
    for style in ("BACK", "DOUBLE_BACK", "FORWARD")
)
_PATH_MAGIC_PATTERN = re.compile("|".join(map(re.escape, _PATH_MAGIC)))
_PATH_MAGIC_RETAIN = max(map(len, _PATH_MAGIC)) - 1


def _patch_source(patch_archive, directive, source, root, member, target, stop,
                  progress, log=None, aliases=None, source_verified=False,
                  metrics=None):
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
        reused_source_hash = (source_verified and candidate == source)
        if directive.data.get("FromHash") is not None and not reused_source_hash:
            hash_started = time.monotonic()
            actual = file_hash(candidate, stop)
            if metrics is not None:
                metrics["source_hash_seconds"] += time.monotonic() - hash_started
            expected = canonical_hash(directive.data["FromHash"])
            if actual != expected:
                emit(log, "reconstruct.patch.source_rejected", output=directive.path,
                     source=candidate, actual_hash=actual, expected_hash=expected)
                continue
        if metrics is not None:
            metrics["source_hashes_reused"] += int(reused_source_hash)
        emit(log, "reconstruct.patch.attempt", output=directive.path,
             source=candidate, attempt=attempted)
        try:
            entry_started = time.monotonic()
            patch = patch_archive.open(directive.data["PatchID"])
            if metrics is not None:
                metrics["entry_open_seconds"] += time.monotonic() - entry_started
            with patch:
                apply_started = time.monotonic()
                try:
                    digest = apply_octodiff(candidate, patch, target,
                                            directive.size, directive.hash, stop,
                                            progress=progress, log=log)
                finally:
                    if metrics is not None:
                        metrics["apply_seconds"] += (
                            time.monotonic() - apply_started)
            return digest
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


def _remap_values(request):
    from .runtime import windows_path
    paths = {"GAME": request.game_roots.get(request.package.game, request.game.get_game_path()),
             "MO2": request.directory / "root", "DOWNLOAD": request.downloads}
    replacements = {}
    for name, path in paths.items():
        windows = windows_path(request.game, path)
        for style, value in (("BACK", windows), ("DOUBLE_BACK", windows.replace("\\", "\\\\")),
                             ("FORWARD", windows.replace("\\", "/"))):
            replacements["{--||" + name + "_PATH_MAGIC_" + style + "||--}"] = value
    return replacements


def remap(text, request):
    for token, value in _remap_values(request).items():
        text = text.replace(token, value)
    return text


def _remap_prefix(text, replacements, final):
    limit = len(text) if final else max(0, len(text) - _PATH_MAGIC_RETAIN)
    cursor = 0
    consumed = limit
    pieces = []
    for match in _PATH_MAGIC_PATTERN.finditer(text):
        if match.start() >= limit:
            break
        pieces.extend((text[cursor:match.start()], replacements[match.group()]))
        cursor = match.end()
        consumed = max(consumed, cursor)
    pieces.append(text[cursor:consumed])
    return "".join(pieces), text[consumed:]


def _remapped_bytes(source, request, stop=None):
    replacements = _remap_values(request)
    pending = ""
    for chunk in utf8_chunks(source, stop):
        pending += chunk
        output, pending = _remap_prefix(pending, replacements, False)
        if output:
            yield output.replace("\n", os.linesep).encode("utf-8")
    output, pending = _remap_prefix(pending, replacements, True)
    if output:
        yield output.replace("\n", os.linesep).encode("utf-8")


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


def _archive_member_path(value, *, directory=False, size=0):
    name = str(value).replace("\\", "/").rstrip("/")
    if directory and not size and name == ".":
        return None
    return relative_path(name)


def extract_safe(archive: Path, target: Path, stop, log, budget=None, progress=None,
                 *, members=None, resources=None):
    from .extraction import working_memory, zip_memory, extract_selected, finish_extraction
    started = time.monotonic()
    budgeted = 0
    def reserve_budget(total):
        nonlocal budgeted
        additional = max(0, total - budgeted)
        if budget and additional:
            budget(additional)
        budgeted = max(budgeted, total)
    emit(log, "extract.started", archive=archive, target=target,
         compressed_bytes=archive.stat().st_size)
    target.mkdir(parents=True, exist_ok=True)
    zip_fallback = False
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as source:
                items = source.infolist()
                emit(log, "extract.format", archive=archive, format="zip",
                     members=len(items), expanded_bytes=sum(item.file_size for item in items),
                     compression_methods=sorted({item.compress_type for item in items}),
                     encrypted=sum(bool(item.flag_bits & 1) for item in items))
                entries, seen = [], {}
                for item in items:
                    directory = (item.orig_filename.endswith(("/", "\\"))
                                 or stat.S_ISDIR(item.external_attr >> 16)
                                 or bool(item.external_attr & 0x10))
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise WabbajackError("Symbolic links are not supported in source archives")
                    if item.flag_bits & 1:
                        raise WabbajackError(f"Password-protected source archive is unsupported: {archive.name}")
                    name = _archive_member_path(item.orig_filename, directory=directory,
                                                size=item.file_size)
                    if name is None:
                        continue
                    path = within(target, name)
                    if name in seen and not (directory and seen[name]):
                        raise WabbajackError(f"Conflicting ZIP destination: {name}")
                    if directory and item.file_size:
                        raise WabbajackError(f"ZIP directory contains file data: {name}")
                    seen[name] = directory
                    if members is None or name.casefold() in members:
                        entries.append((item, path, directory))
                native_methods = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                  zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
                selected = [item for item, _, _ in entries]
                if all(item.compress_type in native_methods for item in selected):
                    total = sum(i.file_size for i in selected)
                    completed = 0
                    reserve_budget(total)
                    with resources(zip_memory(source, selected), total) if resources else nullcontext():
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
                         format="zip-native", members=len(selected), expanded_bytes=total,
                         elapsed_seconds=round(time.monotonic() - started, 3))
                    return
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            zip_fallback = True
            emit(log, "extract.zip.native_failed", archive=archive,
                 exception_type=type(exc).__name__, exception=str(exc),
                 fallback="7zip")
            shutil.rmtree(target)
            target.mkdir(parents=True)
    if not zip_fallback and tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            items = source.getmembers()
            emit(log, "extract.format", archive=archive, format="tar",
                 members=len(items), expanded_bytes=sum(i.size for i in items))
            completed = 0
            entries = []
            for item in items:
                if not item.isfile() and not item.isdir():
                    raise WabbajackError("Special files are not supported in source archives")
                name = _archive_member_path(item.name, directory=item.isdir(), size=item.size)
                if name is None:
                    continue
                path = within(target, name)
                if members is None or name.casefold() in members:
                    entries.append((item, path))
            total = sum(item.size for item, _ in entries)
            reserve_budget(total)
            with resources(256 * 1024 ** 2, total) if resources else nullcontext():
                for item, path in entries:
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
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
             format="tar-native", members=len(entries), expanded_bytes=total,
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
    entries, entry = [], {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(" = ")
        if not line.strip():
            if entry:
                entries.append(entry)
                entry = {}
            continue
        if sep:
            entry[key] = value
        if key in {"Symbolic Link", "Hard Link"} and value:
            raise WabbajackError("Links are not supported in source archives")
    if entry:
        entries.append(entry)
    validated = []
    for entry in entries:
        if "Path" not in entry:
            continue
        size = int(entry.get("Size") or "0")
        directory = (entry.get("Folder") == "+"
                     or entry.get("Attributes", "").startswith("D"))
        name = _archive_member_path(entry["Path"], directory=directory, size=size)
        if name is None:
            continue
        within(target, name)
        validated.append((entry, name, directory, size))
    expanded = sum(size for _, _, _, size in validated)
    selected = [item for item in validated if members is None or item[1].casefold() in members]
    names = [entry["Path"] for entry, _, directory, _ in selected if not directory]
    matched = {name.casefold() for _, name, directory, _ in selected if not directory}
    selective = (members is not None and members <= matched and
                 all(name == name.strip() and not name.startswith(('"', '\ufeff'))
                     and not any(c in name for c in "\r\n") for name in names))
    selected_bytes = sum(size for _, _, _, size in selected) if selective else expanded
    reserve_budget(selected_bytes)
    memory_bytes = working_memory({entry.get("Method", "") for entry, _, _, _ in validated})
    emit(log, "extract.selection", archive=archive, selective=selective,
         archive_members=len(validated), selected_members=len(names) if selective else len(validated),
         expanded_bytes=expanded, selected_bytes=selected_bytes,
         working_memory_bytes=memory_bytes)
    try:
        with resources(memory_bytes, selected_bytes) if resources else nullcontext():
            if selective:
                if names:
                    extract_selected(tool, archive, target, names, stop, log,
                                     (lambda pct: progress(pct, 100)) if progress else None)
            else:
                from Utils.mods.install import _extract_archive
                errors = []
                if not _extract_archive(str(archive), str(target), log, stop, errors,
                                        progress_cb=(lambda pct: progress(pct, 100)) if progress else None,
                                        cpu_threads=2 if resources else None,
                                        finalize=lambda folder: finish_extraction(folder, stop, log)):
                    raise WabbajackError(f"Extraction failed: {archive.name}: {'; '.join(errors)}")
    except WabbajackError as exc:
        if not selective or stop.is_set():
            raise
        emit(log, "extract.selection.fallback", archive=archive, exception=str(exc))
        shutil.rmtree(target)
        return extract_safe(archive, target, stop, log, reserve_budget, progress,
                            resources=resources)
    emit(log, "extract.completed", archive=archive, target=target,
         format="7zip", expanded_bytes=selected_bytes,
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
        self._stale_paths = ({path.relative_to(self.output).as_posix()
                              for path in self.output.rglob("*") if path.is_file()}
                             if self.output.exists() else set())
        self.output.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._record_delay = threading.local()
        self.results = {}
        self._result_keys = set()
        self._temporary_bytes = 0
        self.extraction_memory = None
        self._build_pool = None
        self._build_stop = threading.Event()
        self._build_futures = {}
        self._build_pending = {}
        self._build_waiters = {}
        self._build_directives = {}
        self._build_error = None
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
            self.store.prepare_directory(target.parent)
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
            delayed = getattr(self._record_delay, "paths", None)
            if delayed is not None:
                delayed.append(directive.path)
            else:
                self._release_dependency(directive.path)

    def _release_dependency(self, path):
        for dependent in self._build_waiters.pop(path.casefold(), ()):
            self._build_pending[dependent].discard(path.casefold())
            if not self._build_pending[dependent]:
                self._submit_build(dependent)

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
        parents = {d.path.rpartition("/")[0] for d in self.request.package.directives
                   if d.path not in self.results and d.path not in self._skipped_dependencies}
        for parent in sorted(parents):
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
            self.store.prepare_directory(within(self.output, parent) if parent else self.output)
        notify(f"Reusing {len(self.results):,} files; {len(archives):,} archives needed")
        emit(self.cb.on_log, "reconstruct.plan", required_archives=len(archives),
             required_bytes=sum(item.size for item in archives),
             skipped_dependencies=len(self._skipped_dependencies),
             reuse_candidates=total, elapsed_seconds=round(time.monotonic() - started, 3),
             reused=self._reuse_counts)
        return archives

    def install_archive(self, archive, path):
        started = time.monotonic()
        self._record_delay.paths = []
        succeeded = False
        row = self._row(archive)
        self.cb.on_extract_add(row, archive.name)
        scratch = self.store.work / "extract" / hashlib.sha256(archive.key.encode()).hexdigest()
        extracted = {}
        aliases = {}
        reserved = 0
        current_directive = None
        source_extraction_seconds = 0.0
        extraction_wait_seconds = 0.0
        hardlinked_outputs = 0
        copied_outputs = 0
        patch_archive = None
        patch_metrics = Counter()
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
        @contextmanager
        def resources(working_bytes, expanded_bytes):
            nonlocal extraction_wait_seconds
            from .extraction import LARGE_BYTES
            memory = self.extraction_memory
            if memory is None:
                yield
                return
            large = max(source.stat().st_size, expanded_bytes) >= LARGE_BYTES
            wait_started = time.monotonic()
            def waiting():
                self.cb.on_extract_wait(row, archive.name)
                emit(self.cb.on_log, "extract.capacity.wait", archive=archive.name,
                     source=source, working_memory_bytes=working_bytes,
                     selected_bytes=expanded_bytes, large=large)
            memory.acquire(working_bytes, cancel=self.control.stop, large=large,
                           on_wait=waiting)
            waited = time.monotonic() - wait_started
            extraction_wait_seconds += waited
            try:
                self.cb.on_extract_add(row, archive.name)
                self.cb.on_extract_update(row, progress.last_value, 1000)
                emit(self.cb.on_log, "extract.capacity.acquired", archive=archive.name,
                     source=source, working_memory_bytes=working_bytes,
                     selected_bytes=expanded_bytes, large=large,
                     wait_seconds=round(waited, 3))
                yield
            finally:
                memory.release(working_bytes, large=large)
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
            required_members = {}
            for d in directives:
                _, members = archive_path(d.data)
                for depth, member in enumerate(members):
                    prefix = tuple(m.casefold() for m in members[:depth])
                    required_members.setdefault(prefix, set()).add(member.casefold())
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
                        previous_wait = extraction_wait_seconds
                        key = tuple(m.casefold() for m in members[:depth])
                        extracting = lambda current, total, key=key: progress.update(key, current, total)
                        root = scratch / str(len(extracted))
                        emit(self.cb.on_log, "reconstruct.source_extract.started",
                             archive=archive.name, source=source, member=member,
                             depth=depth, target=root,
                             format="bethesda" if source.suffix.lower() in {".bsa", ".ba2"}
                             else "general")
                        if source.suffix.lower() in {".bsa", ".ba2"}:
                            from .archive_io import select_bethesda
                            aliases[cache_key] = {}
                            rows = select_bethesda(source, required_members[key], aliases[cache_key])
                            expanded_bytes = sum(len(header) + sum(c[2] for c in segments)
                                                 for _, header, segments in rows)
                            working_bytes = 64 * 1024 ** 2 + max((packed + full
                                for _, _, segments in rows for _, packed, full, compression in segments
                                if compression == "lz4block"), default=0)
                            reserve(expanded_bytes)
                            with resources(working_bytes, expanded_bytes):
                                self._extract_bethesda(source, root, extracting, rows=rows,
                                                       aliases=aliases[cache_key])
                        else:
                            extract_safe(source, root, self.control.stop, self.cb.on_log, reserve, extracting,
                                         members=required_members[key], resources=resources)
                        progress.update(key, 1, 1)
                        extracted[cache_key] = root
                        waited = extraction_wait_seconds - previous_wait
                        source_extraction_seconds += time.monotonic() - extraction_started - waited
                        emit(self.cb.on_log, "reconstruct.source_extract.completed",
                             archive=archive.name, source=source, depth=depth,
                             target=root,
                             wait_seconds=round(waited, 3),
                             elapsed_seconds=round(
                                 time.monotonic() - extraction_started - waited, 3))
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
                self.store.prepare_directory(target.parent)
                linked = None
                patched_hash = None
                def copying(current, total, key=d.index):
                    progress.update(key, current * 9, total * 10)
                if d.kind == "PatchedFromArchive":
                    if patch_archive is None:
                        package_started = time.monotonic()
                        patch_archive = zipfile.ZipFile(self.request.package.path)
                        patch_metrics["package_open_seconds"] += (
                            time.monotonic() - package_started)
                        patch_metrics["package_opens"] += 1
                    patched_hash = _patch_source(
                        patch_archive, d, source, root if members else None,
                        members[-1] if members else "", target,
                        self.control.stop, copying, self.cb.on_log,
                        aliases.get(cache_key) if members else None,
                        source_verified=(source is not None and bool(members)
                                         and d.data.get("FromHash") is not None),
                        metrics=patch_metrics)
                    patch_metrics["outputs"] += 1
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
                    if d.kind == "PatchedFromArchive":
                        actual = patched_hash
                    elif d.kind == "FromArchive" and members and linked:
                        actual = canonical_hash(d.output_hash)
                    else:
                        actual = None
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
                 extraction_wait_seconds=round(extraction_wait_seconds, 3),
                 patch_outputs=patch_metrics["outputs"],
                 patch_package_opens=patch_metrics["package_opens"],
                 patch_package_open_seconds=round(
                     patch_metrics["package_open_seconds"], 3),
                 patch_entry_open_seconds=round(
                     patch_metrics["entry_open_seconds"], 3),
                 patch_source_hash_seconds=round(
                     patch_metrics["source_hash_seconds"], 3),
                 patch_source_hashes_reused=patch_metrics["source_hashes_reused"],
                 patch_output_hashes_reused=patch_metrics["outputs"],
                 patch_apply_seconds=round(patch_metrics["apply_seconds"], 3),
                 output_processing_seconds=round(max(
                     0.0, elapsed - source_extraction_seconds -
                     planning_seconds - extraction_wait_seconds), 3),
                 elapsed_seconds=round(elapsed, 3))
            succeeded = True
        except BaseException as exc:
            emit_exception(self.cb.on_log, "reconstruct.archive.failed", exc,
                           archive=archive.name, source=path,
                           directive=current_directive.path if current_directive else None,
                           directive_kind=current_directive.kind
                           if current_directive else None,
                           elapsed_seconds=round(time.monotonic() - started, 3))
            raise
        finally:
            if patch_archive is not None:
                patch_archive.close()
            cleanup_started = time.monotonic()
            shutil.rmtree(scratch, ignore_errors=True)
            emit(self.cb.on_log, "reconstruct.scratch.cleaned",
                 archive=archive.name, target=scratch,
                 elapsed_seconds=round(time.monotonic() - cleanup_started, 3))
            with self._lock:
                self._temporary_bytes -= reserved
                recorded = self._record_delay.paths
                del self._record_delay.paths
                if succeeded:
                    for output in recorded:
                        self._release_dependency(output)
            self.cb.on_extract_remove(row)
        return extraction_wait_seconds

    def _row(self, archive):
        return list(self.request.package.archives).index(archive.key) + 1

    def _extract_bethesda(self, source, root, progress=None, *, rows=None, aliases=None):
        from .archive_io import extract_bethesda
        started = time.monotonic()
        emit(self.cb.on_log, "extract.bethesda.started", archive=source,
             target=root, selected_members=len(rows) if rows is not None else None)
        if aliases is None:
            aliases = {}
        extract_bethesda(source, root, self.control.stop, progress=progress, aliases=aliases,
                         selected_records=rows)
        emit(self.cb.on_log, "extract.bethesda.completed", archive=source,
             target=root, legacy_path_aliases=len(aliases),
             elapsed_seconds=round(time.monotonic() - started, 3))
        return aliases

    def _write_inline(self, archive, member, directive):
        target = within(self.output, directive.path)
        digest = XXHash()
        written = 0
        with archive.open(member) as source, atomic_writer(
                target, "wb", encoding=None, prepare_parent=self.store.prepare_directory) as out:
            if directive.kind == "RemappedInlineFile":
                chunks = _remapped_bytes(source, self.request, self.control.stop)
            else:
                chunks = iter(lambda: source.read(1024 * 1024), b"")
            for data in chunks:
                if self.control.stop.is_set():
                    raise InterruptedError("Installation stopped")
                written += len(data)
                if directive.deterministic and written > directive.output_size:
                    raise WabbajackError(f"Output exceeds declared size: {directive.path}")
                out.write(data)
                digest.update(data)
            actual = digest.digest()
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
            if directive.deterministic and (
                    actual != directive.output_hash or written != directive.output_size):
                raise WabbajackError(f"Output failed verification: {directive.path}")
        self._record(directive, target, actual=actual)
        return written

    def _prepare_inline(self, inline, progress=None):
        started = time.monotonic()
        done = reused = skipped = written = output_bytes = 0
        current = None
        last_progress = 0.0
        def notify(detail, force=False):
            nonlocal last_progress
            now = time.monotonic()
            if progress and (force or (done < len(inline) and now - last_progress >= 0.1)):
                progress("Preparing provided files", done, len(inline), detail)
                last_progress = now
        notify("Planning provided files", force=True)
        try:
            completed = self.store.completed_outputs()
            with zipfile.ZipFile(self.request.package.path) as archive:
                pending = []
                for d in inline:
                    current = d
                    if self.control.stop.is_set():
                        raise InterruptedError("Installation stopped")
                    if d.path in self._skipped_dependencies:
                        skipped += 1
                    elif d.path in self.results:
                        reused += 1
                    else:
                        found = (self._find_reusable(d, completed)
                                 if d.path in completed or self._installed_reuse_candidate(d) else None)
                        if found is not None:
                            self._accept_reuse(d, found)
                            reused += 1
                        else:
                            pending.append((d, archive.getinfo(d.data["SourceDataID"])))
                    done = reused + skipped
                    notify(d.path)
                del completed
                pending.sort(key=lambda item: item[1].header_offset)
                planning_seconds = time.monotonic() - started
                emit(self.cb.on_log, "reconstruct.inline.batch.started", total=len(inline),
                     pending=len(pending), reused=reused, skipped=skipped,
                     packed_bytes=sum(member.compress_size for _, member in pending),
                     source_bytes=sum(member.file_size for _, member in pending),
                     order="archive-offset", planning_seconds=round(planning_seconds, 3))
                last_log = time.monotonic()
                for d, member in pending:
                    current = d
                    if self.control.stop.is_set():
                        raise InterruptedError("Installation stopped")
                    notify(d.path)
                    output_bytes += self._write_inline(archive, member, d)
                    written += 1
                    done += 1
                    now = time.monotonic()
                    if written % 1024 == 0 or now - last_log >= 2:
                        emit(self.cb.on_log, "reconstruct.inline.progress", completed=done,
                             total=len(inline), written=written, output_bytes=output_bytes,
                             path=d.path, elapsed_seconds=round(now - started, 3))
                        last_log = now
                    notify(d.path)
            self.store.flush_completed()
            emit(self.cb.on_log, "reconstruct.inline.batch.completed", total=len(inline),
                 written=written, reused=reused, skipped=skipped, output_bytes=output_bytes,
                 planning_seconds=round(planning_seconds, 3),
                 elapsed_seconds=round(time.monotonic() - started, 3))
            notify("Provided files verified", force=True)
        except BaseException as exc:
            emit_exception(self.cb.on_log, "reconstruct.inline.failed", exc,
                           path=current.path if current else None,
                           kind=current.kind if current else None,
                           source_data_id=current.data.get("SourceDataID") if current else None,
                           completed=done, written=written, output_bytes=output_bytes,
                           elapsed_seconds=round(time.monotonic() - started, 3))
            raise

    @staticmethod
    def _dependencies(directive):
        if directive.kind == "CreateBSA":
            base = "TEMP_BSA_FILES/" + relative_path(directive.data["TempID"])
            return [f"{base}/{relative_path(item['Path'])}" for item in directive.data["FileStates"]]
        return [relative_path(item["RelativePath"]) for item in directive.data["Sources"]]

    def start_builds(self):
        with self._lock:
            if self._build_pool is not None:
                return
            directives = [d for d in self.request.package.directives
                          if d.kind in {"CreateBSA", "MergedPatch"}
                          and d.path not in self.results and d.path not in self._skipped_dependencies]
            if not directives:
                return
            self._build_stop.clear()
            self._build_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wabbajack-build")
            self._build_directives = {d.path: d for d in directives}
            for directive in directives:
                required = {path.casefold() for path in self._dependencies(directive)} - self._result_keys
                self._build_pending[directive.path] = required
                for path in required:
                    self._build_waiters.setdefault(path, []).append(directive.path)
            for path, required in list(self._build_pending.items()):
                if not required:
                    self._submit_build(path)
        emit(self.cb.on_log, "reconstruct.builds.started", outputs=len(directives), workers=1)

    def _submit_build(self, path):
        self._build_pending.pop(path)
        self._build_futures[path] = self._build_pool.submit(self._build_special, self._build_directives[path])

    def close_builds(self):
        with self._lock:
            pool, self._build_pool = self._build_pool, None
            self._build_stop.set()
            self._build_waiters.clear()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def _build_special(self, directive):
        from .acquire import _CombinedStop
        stop = _CombinedStop(self.control.stop, self._build_stop)
        try:
            if stop.is_set():
                raise InterruptedError("Installation stopped")
            required = self._dependencies(directive)
            target = within(self.output, directive.path)
            self.store.prepare_directory(target.parent)
            started = time.monotonic()
            emit(self.cb.on_log, "reconstruct.special.started", path=directive.path,
                 kind=directive.kind, dependencies=len(required),
                 dependency_sample=required[:50], dependency_sample_truncated=len(required) > 50)
            actual = None
            if directive.kind == "CreateBSA":
                base = "TEMP_BSA_FILES/" + relative_path(directive.data["TempID"])
                rebuild_archive(target, source_path(self.output, base),
                                directive.data["State"], directive.data["FileStates"], stop,
                                log=self.cb.on_log, memory_budget=self.extraction_memory)
            else:
                basis = self.store.work / f"merge-{directive.index}.tmp"
                try:
                    with basis.open("wb") as output:
                        for item in directive.data["Sources"]:
                            source = source_path(self.output, item["RelativePath"])
                            before = self.store._stamp(source.stat())
                            digest = XXHash()
                            with source.open("rb") as incoming:
                                while data := incoming.read(1024 * 1024):
                                    if stop.is_set():
                                        raise InterruptedError("Installation stopped")
                                    digest.update(data)
                                    output.write(data)
                            if (self.store._stamp(source.stat()) != before
                                    or digest.digest() != canonical_hash(item["Hash"])):
                                raise WabbajackError(f"Merge source failed verification: {item['RelativePath']}")
                    with zipfile.ZipFile(self.request.package.path) as archive, archive.open(directive.data["PatchID"]) as patch:
                        actual = apply_octodiff(basis, patch, target, directive.size, directive.hash,
                                               stop, log=self.cb.on_log)
                finally:
                    basis.unlink(missing_ok=True)
            if stop.is_set():
                raise InterruptedError("Installation stopped")
            self._record(directive, target, actual=actual)
            emit(self.cb.on_log, "reconstruct.special.completed", path=directive.path,
                 kind=directive.kind, bytes=target.stat().st_size,
                 hash=self.results[directive.path]["authored_hash"],
                 elapsed_seconds=round(time.monotonic() - started, 3))
        except BaseException as exc:
            with self._lock:
                self._build_error = exc
            raise

    def _wait_builds(self, progress):
        while True:
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
            with self._lock:
                if self._build_error is not None:
                    raise self._build_error
                active = {future for future in self._build_futures.values() if not future.done()}
                remaining = len(active) + len(self._build_pending)
                total = len(self._build_directives)
                detail = next((path for path, future in self._build_futures.items() if not future.done()), "")
            if progress:
                progress("Reconstructing archives and patches", total - remaining, total,
                         detail or "Reconstructed outputs verified")
            if not active:
                if remaining:
                    raise WabbajackError("Required reconstruction dependencies have not completed")
                return
            finished, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in finished:
                future.result()

    def finish(self, progress=None):
        directives = self.request.package.directives
        inline = [d for d in directives if d.kind in {"InlineFile", "RemappedInlineFile", "PropertyFile", "ArchiveMeta"}]
        started = time.monotonic()
        emit(self.cb.on_log, "reconstruct.finish.started", inline=len(inline),
             special=sum(d.kind in {"CreateBSA", "MergedPatch"} for d in directives),
             existing_results=len(self.results))
        try:
            if self._build_pool is None:
                for directive in directives:
                    if directive.kind in {"CreateBSA", "MergedPatch"} and directive.path not in self._skipped_dependencies:
                        self._reuse(directive)
                self.start_builds()
            self._prepare_inline(inline, progress)
            self._wait_builds(progress)
        finally:
            self.close_builds()
        missing = [d.path for d in directives if d.path not in self.results and d.path not in self._skipped_dependencies]
        if missing:
            raise WabbajackError(f"Missing required outputs: {', '.join(missing[:8])}")
        removed = 0
        for relative in self._stale_paths - self.results.keys():
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
            within(self.output, relative).unlink(missing_ok=True)
            removed += 1
        result = {"root/" + path: row for path, row in self.results.items()
                  if path.split("/")[0].casefold() != "temp_bsa_files"}
        emit(self.cb.on_log, "reconstruct.finish.completed", outputs=len(result),
             temporary_outputs=len(self.results) - len(result),
             stale_files_removed=removed, reused=self._reuse_counts,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return result
