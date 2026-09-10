from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .archive_io import read_member, records
from .games import nexus_domain
from .hashes import file_hash
from .paths import WabbajackError, relative_path, source_path, within
from .requirements import profile_configuration, setup_tasks
from .diagnostics import emit, emit_exception

VERSION = 1


def _mpi_relative(value):
    return relative_path(str(value).replace("\\", "/").removeprefix("./"))


def _stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("Additional setup stopped")


def _files(root, stop=None):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise WabbajackError(f"Select a real mod directory: {root}")
    seen = set()
    for parent, folders, files in os.walk(root, followlinks=False):
        _stop(stop)
        for name in folders + files:
            path = Path(parent) / name
            rel = relative_path(path.relative_to(root).as_posix())
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise WabbajackError(f"Setup source contains a link or special file: {path}")
            if rel.casefold() in seen:
                raise WabbajackError(f"Setup source has conflicting Windows paths: {rel}")
            seen.add(rel.casefold())
            if path.is_file():
                yield rel, path


def _digest(path, algorithm, stop=None):
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            _stop(stop)
            digest.update(data)
    return digest.hexdigest()


def _stamp(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _source_hash(path, stop, *, with_stamp=False):
    before = _stamp(path)
    digest = file_hash(path, stop)
    if before != _stamp(path):
        raise WabbajackError(f"Setup source changed during verification: {path}")
    return (digest, before) if with_stamp else digest


def _stage_private_output(store, source, destination, files, stop, progress, label):
    _stop(stop)
    total_bytes = sum(path.stat().st_size for _, path in files)
    progress(label, 0, total_bytes, label)
    try:
        source.replace(destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        copied = 0
        for rel, path in files:
            _stop(stop)
            store._copy(path, within(destination, rel), stop=stop,
                        progress=lambda done, total: progress(label, copied + done, total_bytes, rel))
            copied += path.stat().st_size
    else:
        store._sync_directory(destination.parent)
    progress(label, total_bytes, total_bytes, label)


def mpi_manifest(path, stop=None):
    rows = records(path, mpi_paths=True)
    matches = [row for row in rows if row[0].casefold() == "_package/index.json"]
    if len(matches) != 1 or sum(segment[2] for segment in matches[0][2]) > 128 * 1024 ** 2:
        raise WabbajackError("MPI package has no valid bounded manifest")
    output = io.BytesIO()
    with Path(path).open("rb") as source:
        read_member(source, matches[0], output.write, stop)
    manifest = json.loads(output.getvalue().decode("utf-8-sig"))
    if not isinstance(manifest.get("Package"), dict) or not isinstance(manifest.get("Assets"), list):
        raise WabbajackError("Invalid MPI package metadata")
    return manifest


def _locations(manifest):
    locations = manifest.get("Locations", [])
    if len(locations) == 2 and all(isinstance(row, list) for row in locations):
        return locations[1]
    if locations and all(isinstance(row, dict) for row in locations):
        return locations
    raise WabbajackError("Unsupported MPI location metadata")


def _mpi_path(value, roots, destination=None):
    value = str(value).replace("\\", "/")
    variables = {"FNVROOT": roots.get("newvegas"), "FO3ROOT": roots.get("fallout3"),
                 "FNVDATA": roots["newvegas"] / "Data" if "newvegas" in roots else None,
                 "FO3DATA": roots["fallout3"] / "Data" if "fallout3" in roots else None,
                 "DESTINATION": destination}
    first, _, tail = value.partition("/")
    base = variables.get(first.strip("%").upper()) if first.startswith("%") and first.endswith("%") else None
    if base is None:
        raise WabbajackError(f"Unsupported MPI location: {value}")
    return source_path(base, tail) if tail else Path(base)


def _source_roots(request):
    roots = {nexus_domain(name): Path(path) for name, path in request.game_roots.items()}
    if request.setup_options.get("fallout3"):
        roots["fallout3"] = Path(request.setup_options["fallout3"]).expanduser().absolute()
    elif "fallout3" not in roots:
        from Utils.bethesda.ttw import find_fo3_install
        fo3 = find_fo3_install()
        if fo3:
            roots["fallout3"] = fo3
    return roots


def _managed_mpi_roots(request, task):
    if not task.id.startswith("ttw:"):
        return {}
    from .manifest import stock_folder
    from .post_install_rules import stock_copy_rule
    rule = stock_copy_rule(request)
    stock = stock_folder(request.package)
    if rule and stock and rule.source_game == "newvegas":
        return {"FNVROOT": stock}
    return {}


def _input_boundary(request, path):
    path = path.resolve()
    work = (request.directory / "work").resolve()
    if path.is_relative_to(work) or work.is_relative_to(path):
        raise WabbajackError("Select an external setup source outside this installation's temporary work directory")


def _mpi_sources(task, manifest, roots, stop=None):
    from Utils.bethesda.ttw import FO3_REQUIRED_ESMS
    required = ["newvegas", "fallout3"] if task.id.startswith("ttw:") else ["fallout3" if task.id.startswith("fo3-") else "newvegas"]
    for game in required:
        root = roots.get(game)
        if root is None or not root.is_dir():
            raise WabbajackError(f"Configure the original {game} installation for {task.label}")
        exe = "Fallout3.exe" if game == "fallout3" else "FalloutNV.exe"
        if not source_path(root, exe).is_file():
            raise WabbajackError(f"{exe} is missing from {root}")
        if task.id.startswith("ttw:"):
            masters = FO3_REQUIRED_ESMS if game == "fallout3" else ["FalloutNV.esm", "DeadMoney.esm", "HonestHearts.esm", "OldWorldBlues.esm", "LonesomeRoad.esm", "GunRunnersArsenal.esm"]
            for name in masters:
                if not source_path(root, "Data/" + name).is_file():
                    raise WabbajackError(f"Required game/DLC master is missing: {game}: {name}")
    locations = _locations(manifest)
    source_files = {}
    for location in locations:
        value = str(location.get("Value", ""))
        if "%DESTINATION%" in value.upper() or "INI%" in value.upper():
            continue
        path = _mpi_path(value, roots)
        if location.get("Type") == 1:
            if not path.is_file():
                raise WabbajackError(f"MPI source archive is missing: {path}")
            source_files[str(path)] = _source_hash(path, stop)
    for check in manifest.get("Checks", []):
        if check.get("Type") != 0:
            continue
        loc = int(check.get("Loc", -1))
        if not 0 <= loc < len(locations):
            raise WabbajackError("MPI check references an unknown location")
        value = str(locations[loc].get("Value", ""))
        if "%DESTINATION%" in value.upper():
            continue
        base = _mpi_path(value, roots)
        rel = _mpi_relative(check["File"])
        path = within(base, rel)
        for part in rel.split("/"):
            matches = [p for p in base.iterdir() if p.name.casefold() == part.casefold()] if base.is_dir() else []
            if len(matches) > 1:
                raise WabbajackError(f"Ambiguous MPI source: {rel}")
            if not matches:
                break
            base = matches[0]
        else:
            path = source_path(_mpi_path(value, roots), rel)
        available = path.is_file()
        checksums = str(check.get("Checksums", "")).split()
        if available and checksums:
            if any(len(digest) != 40 for digest in checksums):
                raise WabbajackError("Unsupported MPI source checksum format")
            available = _digest(path, "sha1", stop).casefold() in {digest.casefold() for digest in checksums}
        if available == bool(check.get("Inverted", False)):
            raise WabbajackError(f"{path.name}: {check.get('CustomMessage') or 'MPI source verification failed'}")
    for asset in manifest["Assets"]:
        _stop(stop)
        if not isinstance(asset, list) or len(asset) < 7:
            raise WabbajackError("Unsupported MPI asset metadata")
        loc = int(asset[4])
        if loc < 0 or loc >= len(locations):
            if int(asset[1]) == 1:
                continue
            raise WabbajackError("MPI asset references an unknown source location")
        location = locations[loc]
        if location.get("Type") != 0 or int(asset[1]) == 1:
            continue
        value = str(location.get("Value", ""))
        if "%DESTINATION%" in value.upper():
            continue
        path = source_path(_mpi_path(value, roots), _mpi_relative(asset[6]))
        if not path.is_file():
            raise WabbajackError(f"MPI source file is missing: {path}")
        if str(path) not in source_files:
            source_files[str(path)] = _source_hash(path, stop)
    return source_files


def _mpi_outputs(manifest, managed_roots=None):
    managed_roots = managed_roots or {}
    locations = _locations(manifest)
    outputs = {}
    root_outputs = {}
    for asset in manifest["Assets"]:
        if len(asset) < 7 or not 0 <= int(asset[5]) < len(locations):
            raise WabbajackError("MPI asset references an unknown destination")
        location = locations[int(asset[5])]
        value = str(location.get("Value", "")).replace("\\", "/")
        if not value.upper().startswith("%DESTINATION%"):
            first, _, tail = value.partition("/")
            variable = first.strip("%").upper() if first.startswith("%") and first.endswith("%") else ""
            if variable not in managed_roots or location.get("Type") != 0 or int(asset[1]) != 1:
                raise WabbajackError(f"MPI writes outside its managed output: {value}")
            name = _mpi_relative(asset[7] if len(asset) > 7 and asset[7] else asset[6])
            key = relative_path("/".join(filter(None, (tail, name))))
            root_outputs.setdefault(variable, set()).add(key)
            continue
        prefix = value[len("%DESTINATION%"):].lstrip("/")
        name = _mpi_relative(asset[7] if len(asset) > 7 and asset[7] else asset[6])
        if location.get("Type") == 2:
            key = relative_path(prefix)
            outputs.setdefault(key, set()).add(name.casefold())
        elif location.get("Type") == 0:
            key = relative_path("/".join(filter(None, (prefix, name))))
            outputs[key] = None
        else:
            raise WabbajackError("MPI destination is not an output file or archive")
    if not outputs:
        raise WabbajackError("MPI declares no output files")
    return outputs, root_outputs


def _mpi_output_aliases(task, outputs):
    if not task.id.startswith("ttw:"):
        return None
    aliases = {}
    for relative in outputs:
        path = Path(relative)
        if path.name.casefold().startswith("new "):
            aliases[relative.casefold()] = (path.with_name(path.name[4:]).as_posix(),)
    return aliases or None


def _verify_mod(task, root, stop=None, *, content=False):
    def archive(path):
        rows = records(path, allow_hash_only=True)
        if content:
            with path.open("rb") as source:
                for row in rows:
                    read_member(source, row, lambda data: None, stop)
    archives = []
    for name in task.masters:
        path = source_path(root, name)
        if not path.is_file() or path.stat().st_size < 24:
            raise WabbajackError(f"{task.label} output is incomplete: missing {name}")
        if path.suffix.casefold() == ".bsa":
            archives.append(path)
        else:
            with path.open("rb") as stream:
                if stream.read(4) != b"TES4":
                    raise WabbajackError(f"Invalid Bethesda plugin: {path}")
    if task.id.startswith("ttw:"):
        ttw_archives = [p for _, p in _files(root, stop) if p.suffix.casefold() == ".bsa"]
        if not any(p.name.casefold().startswith("taleoftwowastelands") for p in ttw_archives):
            raise WabbajackError("Select the complete TTW output including its BSAs, not just the ESM")
        archives.extend(ttw_archives)
    archives = list(dict.fromkeys(archives))
    if content:
        from .verification import parallel_verify
        for _ in parallel_verify(archive, archives, stop, size=lambda path: path.stat().st_size):
            pass
    else:
        for path in archives:
            archive(path)


def _archive_mod_root(task, root, stop=None):
    try:
        _verify_mod(task, root, stop)
        return root
    except WabbajackError as direct_error:
        masters = {name.casefold() for name in task.masters}
        candidates = {path.parent for _, path in _files(root, stop)
                      if path.name.casefold() in masters}
        valid = []
        for candidate in candidates:
            try:
                _verify_mod(task, candidate, stop)
                valid.append(candidate)
            except WabbajackError:
                pass
        if len(valid) == 1:
            return valid[0]
        if len(valid) > 1:
            raise WabbajackError(f"{task.label} archive contains multiple possible output folders")
        raise direct_error


def _extract_output_archive(task, archive, target, stop, log=None,
                            budget=None, progress=None):
    archive = Path(archive)
    if not archive.is_file():
        raise WabbajackError(f"Output archive is missing: {archive}")
    stop = stop or threading.Event()
    from .reconstruct import extract_safe
    extract_safe(archive, target, stop, log or (lambda _: None),
                 budget=budget, progress=progress)
    return _archive_mod_root(task, target, stop)


def _sandbox(command, work, writable=()):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise WabbajackError("Install bubblewrap to run MPI with read-only source games")
    sandboxed = [bwrap, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
                 "--tmpfs", "/tmp", "--bind", str(work), str(work)]
    for path in dict.fromkeys(Path(path).resolve() for path in writable):
        if not path.is_dir() or path.is_symlink():
            raise WabbajackError(f"MPI managed output is not a real directory: {path}")
        sandboxed.extend(["--bind", str(path), str(path)])
    return [*sandboxed, "--proc", "/proc", "--dev", "/dev", "--chdir", str(work),
            "--setenv", "HOME", str(work), "--setenv", "TMPDIR", str(work),
            "--", *map(str, command)]


def _run(command, work, stop, progress, label, log=None, *, writable=()):
    started = time.monotonic()
    sandboxed = _sandbox(command, work, writable)
    emit(log, "setup.process.started", label=label, command=sandboxed,
         work=work)
    process = subprocess.Popen(sandboxed, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    pending = b""
    tail = []
    completed, total = 0, 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                _stop(stop)
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        break
                    pending += data.replace(b"\r", b"\n")
                    lines = pending.split(b"\n")
                    pending = lines.pop()[-65536:]
                    for line in lines:
                        text = line.decode("utf-8", "replace").strip()
                        if text:
                            tail = (tail + [text])[-12:]
                            emit(log, "setup.process.output", label=label,
                                 output=text[:2000])
                            match = re.search(r"\bAssets:\s*(\d+)\s*/\s*(\d+)", text)
                            if match and 0 <= int(match[1]) <= int(match[2]):
                                completed, total = max(completed, int(match[1])), max(total, int(match[2]))
                            if progress:
                                progress(label, completed, total, text[:500])
        code = process.wait()
        emit(log, "setup.process.completed", label=label, exit_code=code,
             elapsed_seconds=round(time.monotonic() - started, 3), tail=tail)
        if code:
            raise WabbajackError(f"{label} failed ({code}): " + "\n".join(tail))
    finally:
        if process.poll() is None:
            emit(log, "setup.process.terminating", label=label,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        process.stdout.close()


def _previous(info, task):
    pending = info.get("pending_setup_tasks", {})
    return pending.get(task.id) or info.get("setup_tasks", {}).get(task.id)


def _reusable(request, task, record, stop=None):
    if not record or record.get("version") != VERSION or not record.get("outputs"):
        return None
    if any(f"root/mods/{task.mod}/{name}".casefold() not in {key.casefold() for key in record["outputs"]}
           for name in task.masters):
        return None
    option = request.setup_options.get(task.id, {})
    if option.get("mpi") and Path(option["mpi"]).is_file():
        if file_hash(Path(option["mpi"]), stop) != record.get("mpi_hash"):
            return None
    if option.get("source") and Path(option["source"]).is_dir():
        current = {rel: file_hash(path, stop) for rel, path in _files(Path(option["source"]), stop)}
        if current != record.get("source_hashes"):
            return None
    if option.get("archive") and Path(option["archive"]).is_file():
        if file_hash(Path(option["archive"]), stop) != record.get("archive_hash"):
            return None
    result = {}
    prefixes = (f"root/mods/{task.mod}/".casefold(),
                *(f"root/{stock}/".casefold()
                  for stock in _managed_mpi_roots(request, task).values()))
    for key, digest in record["outputs"].items():
        if not key.casefold().startswith(prefixes):
            return None
        candidates = [within(request.directory, key),
                      within(request.directory / "work" / "setup-output", key),
                      within(request.directory / "work" / "output", key.removeprefix("root/"))]
        path = next((path for path in candidates if path.is_file() and not path.is_symlink()
                     and file_hash(path, stop) == digest), None)
        if path is None:
            return None
        result[key] = {"source": str(path), "authored_hash": digest,
                       "signature": record["signature"]}
    return result


def preflight_tasks(request, check, stop=None, *, configuration=None,
                    reusable=None, log=None):
    from .store import installation_info
    from Utils.bethesda.ttw import find_ttw_installer
    tasks = setup_tasks(request.package, request.profiles, configuration)
    info = installation_info(request.directory, log) or {}
    estimates = 0
    emit(log, "setup.preflight.started", tasks=len(tasks),
         task_ids=[task.id for task in tasks])
    for task in tasks:
        _stop(stop)
        option = request.setup_options.get(task.id, {})
        previous = _previous(info, task)
        emit(log, "setup.preflight.task", task_id=task.id, label=task.label,
             mod=task.mod, profiles=task.profiles, option=option,
             previous=bool(previous))
        try:
            reused = _reusable(request, task, previous, stop) if option == (previous or {}).get("option", {}) else None
            if reused:
                if reusable is not None:
                    reusable.update(key for key, row in reused.items() if Path(row["source"]) == request.directory / key)
                check("pass", task.label, f"Verified reusable setup in {task.mod}")
                if previous.get("package_identity") != request.package.identity:
                    check("warning", task.label, "Review the new author's required external content version; the saved setup will be reused.")
                emit(log, "setup.preflight.reused", task_id=task.id,
                     outputs=len(reused))
                continue
            source = option.get("source", "")
            archive = option.get("archive", "")
            mpi = option.get("mpi", "")
            if source:
                root = Path(source).expanduser().absolute()
                _input_boundary(request, root)
                _verify_mod(task, root, stop)
                files = list(_files(root, stop))
                estimates += sum(path.stat().st_size for _, path in files) * 2
                emit(log, "setup.preflight.import", task_id=task.id,
                     source=root, files=len(files),
                     bytes=sum(path.stat().st_size for _, path in files))
                check("pass", task.label, f"Import {len(files):,} files into {task.mod}; required masters and archive indexes checked, authored priority preserved")
                check("warning", task.label, "Existing output has no package-supplied hash or version guarantee. Confirm it is the version required by the author.")
            elif archive:
                if not task.id.startswith("yupttw:"):
                    raise WabbajackError(f"Output archive import is not supported for {task.label}")
                path = Path(archive).expanduser().absolute()
                _input_boundary(request, path)
                expanded = [0]
                with tempfile.TemporaryDirectory(prefix="amethyst-setup-archive-") as folder:
                    root = _extract_output_archive(task, path, Path(folder), stop, log,
                                                   budget=lambda size: expanded.__setitem__(0, size))
                    files = list(_files(root, stop))
                    size = sum(file.stat().st_size for _, file in files)
                estimates += size * 2
                emit(log, "setup.preflight.archive", task_id=task.id,
                     archive=path, files=len(files), bytes=size,
                     expanded_bytes=expanded[0])
                check("pass", task.label, f"Extract {len(files):,} files from {path.name} into {task.mod}; required masters checked and authored priority preserved")
                check("warning", task.label, "The archive has no package-supplied version guarantee. Confirm it is the version required by the author.")
            elif mpi and task.mpi_titles:
                path = Path(mpi).expanduser().absolute()
                _input_boundary(request, path)
                manifest = mpi_manifest(path, stop)
                title = str(manifest["Package"].get("Title", ""))
                emit(log, "setup.preflight.mpi", task_id=task.id, path=path,
                     title=title, version=manifest["Package"].get("Version", ""),
                     assets=len(manifest.get("Assets", [])))
                if title.casefold() not in {name.casefold() for name in task.mpi_titles}:
                    raise WabbajackError(f"Wrong MPI package: {title}; select {task.label}")
                managed_roots = _managed_mpi_roots(request, task)
                expected, root_outputs = _mpi_outputs(manifest, managed_roots)
                for name in task.masters:
                    if name.casefold() not in {p.casefold().removeprefix("new ") for p in expected}:
                        raise WabbajackError(f"This MPI version does not provide the authored requirement {name}")
                sources = _mpi_sources(task, manifest, _source_roots(request), stop)
                tool = find_ttw_installer(request.game)
                if not tool or not os.access(tool, os.X_OK):
                    raise WabbajackError("Install the native MPI tool using the setup button")
                with tempfile.TemporaryDirectory(prefix="amethyst-mpi-probe-") as folder:
                    _run([tool, "install", "--help"], Path(folder), stop, None,
                         "MPI capability probe", log)
                source_bytes = sum(Path(p).stat().st_size for p in sources)
                workspace_bytes = source_bytes * 2 + path.stat().st_size * 3
                estimates += max(workspace_bytes,
                                 50 * 1024 ** 3 if task.id.startswith("ttw:") else 0)
                managed_count = sum(map(len, root_outputs.values()))
                managed_detail = f"; {managed_count:,} managed stock-game files" if managed_count else ""
                check("pass", task.label, f"Build {title} {manifest['Package'].get('Version', '')} into {task.mod}; {len(sources):,} source files verified{managed_detail}")
                emit(log, "setup.preflight.mpi_ready", task_id=task.id,
                     tool=tool, sources=len(sources))
            else:
                raise WabbajackError("Select the author-required MPI package or an existing complete output mod"
                                     if task.mpi_titles else "Select the author-required output archive or extracted output mod")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            emit_exception(log, "setup.preflight.failed", exc, task_id=task.id)
            check("error", task.label, str(exc))
    emit(log, "setup.preflight.completed", tasks=len(tasks),
         estimated_bytes=estimates)
    return tasks, estimates


def run_tasks(request, store, desired, stop, progress, log=None):
    started = time.monotonic()
    from Utils.bethesda.ttw import find_ttw_installer
    tasks = setup_tasks(request.package, request.profiles)
    pending = {}
    previous_tasks = store.get("pending_setup_tasks", {})
    store.set("setup_options", request.setup_options)
    emit(log, "setup.run.started", tasks=len(tasks),
         task_ids=[task.id for task in tasks])
    for task in tasks:
        task_started = time.monotonic()
        _stop(stop)
        progress("Additional setup", 0, len(tasks), task.label)
        option = request.setup_options.get(task.id, {})
        previous = _previous({"pending_setup_tasks": previous_tasks,
                              "setup_tasks": store.get("setup_tasks", {})}, task)
        emit(log, "setup.task.started", task_id=task.id, label=task.label,
             mod=task.mod, option=option, previous=bool(previous))
        reuse = _reusable(request, task, previous, stop) if option == (previous or {}).get("option", {}) else None
        if reuse:
            generated = reuse
            record = previous
            emit(log, "setup.task.reused", task_id=task.id,
                 outputs=len(generated))
        else:
            destination = within(store.work / "setup-output", f"root/mods/{task.mod}")
            _input_boundary(request, Path(option.get("source") or option.get("archive") or option.get("mpi") or "/"))
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir(parents=True)
            source = option.get("source", "")
            archive = option.get("archive", "")
            identity = {"version": VERSION, "option": option, "package_identity": request.package.identity}
            managed_output_files = {}
            verified_outputs = {}
            if source:
                root = Path(source).expanduser().absolute()
                _verify_mod(task, root, stop)
                files = list(_files(root, stop))
                emit(log, "setup.task.import.started", task_id=task.id,
                     source=root, files=len(files),
                     bytes=sum(path.stat().st_size for _, path in files))
                identity["source_hashes"] = {}
                copied, total_bytes = 0, sum(path.stat().st_size for _, path in files)
                for rel, path in files:
                    _stop(stop)
                    store._copy(path, within(destination, rel), stop=stop,
                                progress=lambda done, total: progress("Importing " + task.label, copied + done, total_bytes, rel))
                    copied += path.stat().st_size
                    target = within(destination, rel)
                    digest, stamp = _source_hash(target, stop, with_stamp=True)
                    identity["source_hashes"][rel] = digest
                    verified_outputs[target] = (digest, stamp)
            elif archive:
                if not task.id.startswith("yupttw:"):
                    raise WabbajackError(f"Output archive import is not supported for {task.label}")
                path = Path(archive).expanduser().absolute()
                before = _stamp(path)
                identity["archive_hash"] = file_hash(path, stop)
                with tempfile.TemporaryDirectory(prefix="archive-", dir=store.work) as folder:
                    root = _extract_output_archive(task, path, Path(folder), stop, log,
                        progress=lambda current, total: progress("Extracting " + task.label, current, total, path.name))
                    files = list(_files(root, stop))
                    emit(log, "setup.task.archive.started", task_id=task.id,
                         archive=path, files=len(files),
                         bytes=sum(file.stat().st_size for _, file in files))
                    _stage_private_output(store, root, destination, files, stop, progress,
                                          "Importing " + task.label)
                if before != _stamp(path) and file_hash(path, stop) != identity["archive_hash"]:
                    raise WabbajackError(f"Setup archive changed during import: {path}")
            else:
                mpi = Path(option.get("mpi", "")).expanduser().absolute()
                manifest = mpi_manifest(mpi, stop)
                emit(log, "setup.task.mpi.started", task_id=task.id, mpi=mpi,
                     title=manifest["Package"].get("Title", ""),
                     version=manifest["Package"].get("Version", ""),
                     assets=len(manifest.get("Assets", [])))
                if str(manifest["Package"].get("Title", "")).casefold() not in {name.casefold() for name in task.mpi_titles}:
                    raise WabbajackError(f"Incorrect MPI for {task.label}")
                roots = _source_roots(request)
                identity["sources"] = _mpi_sources(task, manifest, roots, stop)
                emit(log, "setup.task.sources.verified", task_id=task.id,
                     sources=len(identity["sources"]), roots=roots)
                source_stamps = {path: _stamp(path) for path in identity["sources"]}
                identity["mpi_hash"] = file_hash(mpi, stop)
                identity["package"] = manifest["Package"]
                managed_roots = _managed_mpi_roots(request, task)
                expected, root_outputs = _mpi_outputs(manifest, managed_roots)
                output_aliases = _mpi_output_aliases(task, expected)
                tool = find_ttw_installer(request.game)
                if not tool:
                    raise WabbajackError("The native MPI tool is missing")
                identity["tool_hash"] = file_hash(tool, stop)
                emit(log, "setup.task.tool.verified", task_id=task.id,
                     tool=tool, hash=identity["tool_hash"])
                with tempfile.TemporaryDirectory(prefix="mpi-", dir=store.work) as folder:
                    work = Path(folder)
                    package = work / "input.mpi"
                    store._copy(mpi, package, stop=stop)
                    if file_hash(package, stop) != identity["mpi_hash"]:
                        raise WabbajackError("MPI package changed while staging it")
                    built = work / "output"
                    built.mkdir()
                    runner = work / "runner" / tool.name
                    store._copy(tool, runner, stop=stop)
                    if (tool.parent / "tools").is_dir():
                        for rel, path in _files(tool.parent / "tools", stop):
                            store._copy(path, within(runner.parent / "tools", rel), stop=stop)
                    run_roots = dict(roots)
                    writable = []
                    for variable, stock in managed_roots.items():
                        game = {"FNVROOT": "newvegas"}.get(variable)
                        if game:
                            root = within(store.work / "output", stock)
                            run_roots[game] = root
                            writable.append(root)
                    command = [runner, "install", "--mpi", package, "--dest", built]
                    for game, flag in (("newvegas", "--fnv"), ("fallout3", "--fo3")):
                        if game in run_roots:
                            command.extend([flag, run_roots[game]])
                    _run(command, work, stop, progress, "Building " + task.label,
                         log, writable=writable)
                    for path, stamp in source_stamps.items():
                        if _stamp(path) != stamp and file_hash(Path(path), stop) != identity["sources"][path]:
                            raise WabbajackError(f"Original game file changed during setup: {path}")
                    for rel, members in expected.items():
                        path = source_path(built, rel, aliases=output_aliases)
                        if not path.is_file():
                            raise WabbajackError(f"MPI did not produce its declared output: {rel}")
                        if members is not None:
                            states = [{"Path": name} for name in members]
                            actual = {row[0].casefold() for row in records(path, states)}
                            if actual != members:
                                raise WabbajackError(f"MPI archive contents differ from its manifest: {rel}")
                    for variable, paths in root_outputs.items():
                        root = run_roots[{"FNVROOT": "newvegas"}[variable]]
                        stock = managed_roots[variable]
                        for rel in paths:
                            path = source_path(root, rel)
                            if not path.is_file() or path.is_symlink():
                                raise WabbajackError(f"MPI did not produce its managed stock-game output: {rel}")
                            managed_output_files[f"root/{stock}/{rel}"] = path
                    if task.id.startswith("fo3-bsa:"):
                        for name in task.masters:
                            source_path(built, "New " + name).replace(built / name)
                    _verify_mod(task, built, stop)
                    built_files = list(_files(built, stop))
                    _stage_private_output(store, built, destination, built_files, stop, progress,
                                          "Staging " + task.label)
            _verify_mod(task, destination, stop, content=True)
            generated = {}
            output_files = {f"root/mods/{task.mod}/{rel}": path
                            for rel, path in _files(destination, stop)}
            output_files.update(managed_output_files)
            output_hashes = {}
            for key, path in output_files.items():
                _stop(stop)
                verified = verified_outputs.get(path)
                if verified and _stamp(path) == verified[1]:
                    digest = verified[0]
                else:
                    digest = _source_hash(path, stop)
                    if verified and digest != verified[0]:
                        raise WabbajackError(f"Setup output changed during verification: {path}")
                output_hashes[key] = digest
            sig = "setup:" + hashlib.sha256(json.dumps([identity, output_hashes], sort_keys=True).encode()).hexdigest()
            for key, digest in output_hashes.items():
                generated[key] = {"source": str(output_files[key]),
                                  "authored_hash": digest, "signature": sig}
            record = {**identity, "signature": sig, "outputs": output_hashes}
            emit(log, "setup.task.outputs.verified", task_id=task.id,
                 outputs=len(output_hashes), signature=sig)
        authored = {key.casefold(): key for key in desired}
        managed_prefixes = tuple(f"root/{stock}/".casefold()
                                 for stock in _managed_mpi_roots(request, task).values())
        record = {**record, "outputs": dict(record["outputs"])}
        for key, row in generated.items():
            existing = authored.get(key.casefold())
            if existing:
                if Path(key).name.casefold() == "meta.ini":
                    record["outputs"].pop(key, None)
                    continue
                if desired[existing]["authored_hash"] != row["authored_hash"]:
                    if (key.casefold().startswith(managed_prefixes)
                            and desired[existing]["signature"].startswith("stock-copy:")):
                        desired[existing] = row
                        continue
                    raise WabbajackError(f"Additional setup conflicts with an authored file: {key}")
                continue
            desired[key] = row
        pending[task.id] = record
        store.set("pending_setup_tasks", {**previous_tasks, **pending})
        progress("Additional setup", len(pending), len(tasks), task.label + " verified")
        emit(log, "setup.task.completed", task_id=task.id,
             outputs=len(record.get("outputs", {})),
             elapsed_seconds=round(time.monotonic() - task_started, 3))
    from Utils.atomic_write import write_atomic_text
    for name in output_mods(request):
        key = f"root/mods/{name}/meta.ini"
        if not any(p.casefold() == key.casefold() for p in desired):
            meta = within(store.work / "output-mods", f"{name}/meta.ini")
            write_atomic_text(meta, "[General]\n")
            digest = file_hash(meta, stop)
            desired[key] = {"source": str(meta), "authored_hash": digest, "signature": "output-mod:" + digest}
    store.set("pending_setup_tasks", pending)
    emit(log, "setup.run.completed", tasks=len(pending),
         output_mods=sorted(output_mods(request)),
         elapsed_seconds=round(time.monotonic() - started, 3))
    return pending


def output_mods(request):
    config = profile_configuration(request.package, request.profiles)
    return {name for profile in config.values() for name in profile.outputs.values()}
