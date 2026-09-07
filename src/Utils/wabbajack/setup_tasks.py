from __future__ import annotations

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
from pathlib import Path

from .archive_io import read_member, records
from .games import nexus_domain
from .hashes import file_hash
from .paths import WabbajackError, relative_path, source_path, within
from .requirements import profile_configuration, setup_tasks

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


def _source_hash(path, stop):
    before = _stamp(path)
    digest = file_hash(path, stop)
    if before != _stamp(path):
        raise WabbajackError(f"Setup source changed during verification: {path}")
    return digest


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


def _mpi_outputs(manifest):
    locations = _locations(manifest)
    outputs = {}
    for asset in manifest["Assets"]:
        if len(asset) < 7 or not 0 <= int(asset[5]) < len(locations):
            raise WabbajackError("MPI asset references an unknown destination")
        location = locations[int(asset[5])]
        value = str(location.get("Value", "")).replace("\\", "/")
        if not value.upper().startswith("%DESTINATION%"):
            raise WabbajackError(f"MPI writes outside its output directory: {value}")
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
    return outputs


def _verify_mod(task, root, stop=None, *, content=False):
    def archive(path):
        rows = records(path)
        if content:
            with path.open("rb") as source:
                for row in rows:
                    read_member(source, row, lambda data: None, stop)
    for name in task.masters:
        path = source_path(root, name)
        if not path.is_file() or path.stat().st_size < 24:
            raise WabbajackError(f"{task.label} output is incomplete: missing {name}")
        if path.suffix.casefold() == ".bsa":
            archive(path)
        else:
            with path.open("rb") as stream:
                if stream.read(4) != b"TES4":
                    raise WabbajackError(f"Invalid Bethesda plugin: {path}")
    if task.id.startswith("ttw:"):
        archives = [p for _, p in _files(root, stop) if p.suffix.casefold() == ".bsa"]
        if not any(p.name.casefold().startswith("taleoftwowastelands") for p in archives):
            raise WabbajackError("Select the complete TTW output including its BSAs, not just the ESM")
        for path in archives:
            archive(path)


def _sandbox(command, work):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise WabbajackError("Install bubblewrap to run MPI with read-only source games")
    return [bwrap, "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
            "--tmpfs", "/tmp", "--bind", str(work), str(work), "--proc", "/proc",
            "--dev", "/dev", "--chdir", str(work), "--setenv", "HOME", str(work),
            "--setenv", "TMPDIR", str(work), "--", *map(str, command)]


def _run(command, work, stop, progress, label):
    process = subprocess.Popen(_sandbox(command, work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
                            match = re.search(r"\bAssets:\s*(\d+)\s*/\s*(\d+)", text)
                            if match and 0 <= int(match[1]) <= int(match[2]):
                                completed, total = max(completed, int(match[1])), max(total, int(match[2]))
                            if progress:
                                progress(label, completed, total, text[:500])
        code = process.wait()
        if code:
            raise WabbajackError(f"{label} failed ({code}): " + "\n".join(tail))
    finally:
        if process.poll() is None:
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
    result = {}
    for key, digest in record["outputs"].items():
        if not key.startswith(f"root/mods/{task.mod}/"):
            return None
        candidates = [within(request.directory, key), within(request.directory / "work" / "setup-output", key)]
        path = next((path for path in candidates if path.is_file() and not path.is_symlink()
                     and file_hash(path, stop) == digest), None)
        if path is None:
            return None
        result[key] = {"source": str(path), "authored_hash": digest,
                       "signature": record["signature"]}
    return result


def preflight_tasks(request, check, stop=None, *, configuration=None, reusable=None):
    from .store import installation_info
    from Utils.bethesda.ttw import find_ttw_installer
    tasks = setup_tasks(request.package, request.profiles, configuration)
    info = installation_info(request.directory) or {}
    estimates = 0
    for task in tasks:
        _stop(stop)
        option = request.setup_options.get(task.id, {})
        previous = _previous(info, task)
        try:
            reused = _reusable(request, task, previous, stop) if option == (previous or {}).get("option", {}) else None
            if reused:
                if reusable is not None:
                    reusable.update(key for key, row in reused.items() if Path(row["source"]) == request.directory / key)
                check("pass", task.label, f"Verified reusable setup in {task.mod}")
                if previous.get("package_identity") != request.package.identity:
                    check("warning", task.label, "Review the new author's required external content version; the saved setup will be reused.")
                continue
            source = option.get("source", "")
            mpi = option.get("mpi", "")
            if source:
                root = Path(source).expanduser().absolute()
                _input_boundary(request, root)
                _verify_mod(task, root, stop)
                files = list(_files(root, stop))
                estimates += sum(path.stat().st_size for _, path in files) * 2
                check("pass", task.label, f"Import {len(files):,} files into {task.mod}; required masters and archive indexes checked, authored priority preserved")
                check("warning", task.label, "Existing output has no package-supplied hash or version guarantee. Confirm it is the version required by the author.")
            elif mpi and task.mpi_titles:
                path = Path(mpi).expanduser().absolute()
                _input_boundary(request, path)
                manifest = mpi_manifest(path, stop)
                title = str(manifest["Package"].get("Title", ""))
                if title.casefold() not in {name.casefold() for name in task.mpi_titles}:
                    raise WabbajackError(f"Wrong MPI package: {title}; select {task.label}")
                expected = _mpi_outputs(manifest)
                for name in task.masters:
                    if name.casefold() not in {p.casefold().removeprefix("new ") for p in expected}:
                        raise WabbajackError(f"This MPI version does not provide the authored requirement {name}")
                sources = _mpi_sources(task, manifest, _source_roots(request), stop)
                tool = find_ttw_installer(request.game)
                if not tool or not os.access(tool, os.X_OK):
                    raise WabbajackError("Install the native MPI tool using the setup button")
                with tempfile.TemporaryDirectory(prefix="amethyst-mpi-probe-") as folder:
                    _run([tool, "install", "--help"], Path(folder), stop, None, "MPI capability probe")
                estimates += max(sum(Path(p).stat().st_size for p in sources) * 6,
                                 50 * 1024 ** 3 if task.id.startswith("ttw:") else path.stat().st_size * 6)
                check("pass", task.label, f"Build {title} {manifest['Package'].get('Version', '')} into {task.mod}; {len(sources):,} source files verified")
            else:
                raise WabbajackError("Select the author-required MPI package or an existing complete output mod"
                                     if task.mpi_titles else "Select the author-required, extracted output mod")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            check("error", task.label, str(exc))
    return tasks, estimates


def run_tasks(request, store, desired, stop, progress):
    from Utils.bethesda.ttw import find_ttw_installer
    tasks = setup_tasks(request.package, request.profiles)
    pending = {}
    previous_tasks = store.get("pending_setup_tasks", {})
    store.set("setup_options", request.setup_options)
    for task in tasks:
        _stop(stop)
        progress("Additional setup", 0, len(tasks), task.label)
        option = request.setup_options.get(task.id, {})
        previous = _previous({"pending_setup_tasks": previous_tasks,
                              "setup_tasks": store.get("setup_tasks", {})}, task)
        reuse = _reusable(request, task, previous, stop) if option == (previous or {}).get("option", {}) else None
        if reuse:
            generated = reuse
            record = previous
        else:
            destination = within(store.work / "setup-output", f"root/mods/{task.mod}")
            _input_boundary(request, Path(option.get("source") or option.get("mpi") or "/"))
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir(parents=True)
            source = option.get("source", "")
            identity = {"version": VERSION, "option": option, "package_identity": request.package.identity}
            if source:
                root = Path(source).expanduser().absolute()
                _verify_mod(task, root, stop)
                files = list(_files(root, stop))
                identity["source_hashes"] = {}
                copied, total_bytes = 0, sum(path.stat().st_size for _, path in files)
                for rel, path in files:
                    _stop(stop)
                    store._copy(path, within(destination, rel), stop=stop,
                                progress=lambda done, total: progress("Importing " + task.label, copied + done, total_bytes, rel))
                    copied += path.stat().st_size
                    identity["source_hashes"][rel] = file_hash(within(destination, rel), stop)
            else:
                mpi = Path(option.get("mpi", "")).expanduser().absolute()
                manifest = mpi_manifest(mpi, stop)
                if str(manifest["Package"].get("Title", "")).casefold() not in {name.casefold() for name in task.mpi_titles}:
                    raise WabbajackError(f"Incorrect MPI for {task.label}")
                roots = _source_roots(request)
                identity["sources"] = _mpi_sources(task, manifest, roots, stop)
                source_stamps = {path: _stamp(path) for path in identity["sources"]}
                identity["mpi_hash"] = file_hash(mpi, stop)
                identity["package"] = manifest["Package"]
                expected = _mpi_outputs(manifest)
                tool = find_ttw_installer(request.game)
                if not tool:
                    raise WabbajackError("The native MPI tool is missing")
                identity["tool_hash"] = file_hash(tool, stop)
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
                    command = [runner, "install", "--mpi", package, "--dest", built]
                    for game, flag in (("newvegas", "--fnv"), ("fallout3", "--fo3")):
                        if game in roots:
                            command.extend([flag, roots[game]])
                    _run(command, work, stop, progress, "Building " + task.label)
                    for path, stamp in source_stamps.items():
                        if _stamp(path) != stamp and file_hash(Path(path), stop) != identity["sources"][path]:
                            raise WabbajackError(f"Original game file changed during setup: {path}")
                    for rel, members in expected.items():
                        path = source_path(built, rel)
                        if not path.is_file():
                            raise WabbajackError(f"MPI did not produce its declared output: {rel}")
                        if members is not None:
                            actual = {row[0].casefold() for row in records(path)}
                            if actual != members:
                                raise WabbajackError(f"MPI archive contents differ from its manifest: {rel}")
                    if task.id.startswith("fo3-bsa:"):
                        for name in task.masters:
                            source_path(built, "New " + name).replace(built / name)
                    _verify_mod(task, built, stop)
                    built_files = list(_files(built, stop))
                    copied, total_bytes = 0, sum(path.stat().st_size for _, path in built_files)
                    for rel, path in built_files:
                        store._copy(path, within(destination, rel), stop=stop,
                                    progress=lambda done, total: progress("Staging " + task.label, copied + done, total_bytes, rel))
                        copied += path.stat().st_size
            _verify_mod(task, destination, stop, content=True)
            generated = {}
            output_hashes = {}
            for rel, path in _files(destination, stop):
                key = f"root/mods/{task.mod}/{rel}"
                output_hashes[key] = file_hash(path, stop)
            sig = "setup:" + hashlib.sha256(json.dumps([identity, output_hashes], sort_keys=True).encode()).hexdigest()
            for key, digest in output_hashes.items():
                generated[key] = {"source": str(within(store.work / "setup-output", key)),
                                  "authored_hash": digest, "signature": sig}
            record = {**identity, "signature": sig, "outputs": output_hashes}
        authored = {key.casefold(): key for key in desired}
        record = {**record, "outputs": dict(record["outputs"])}
        for key, row in generated.items():
            existing = authored.get(key.casefold())
            if existing:
                if Path(key).name.casefold() == "meta.ini":
                    record["outputs"].pop(key, None)
                    continue
                if desired[existing]["authored_hash"] != row["authored_hash"]:
                    raise WabbajackError(f"Additional setup conflicts with an authored file: {key}")
                continue
            desired[key] = row
        pending[task.id] = record
        store.set("pending_setup_tasks", {**previous_tasks, **pending})
        progress("Additional setup", len(pending), len(tasks), task.label + " verified")
    from Utils.atomic_write import write_atomic_text
    for name in output_mods(request):
        key = f"root/mods/{name}/meta.ini"
        if not any(p.casefold() == key.casefold() for p in desired):
            meta = within(store.work / "output-mods", f"{name}/meta.ini")
            write_atomic_text(meta, "[General]\n")
            digest = file_hash(meta, stop)
            desired[key] = {"source": str(meta), "authored_hash": digest, "signature": "output-mod:" + digest}
    store.set("pending_setup_tasks", pending)
    return pending


def output_mods(request):
    config = profile_configuration(request.package, request.profiles)
    return {name for profile in config.values() for name in profile.outputs.values()}
