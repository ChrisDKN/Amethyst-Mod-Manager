from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile


_runner_lock = threading.Lock()


def _inside_flatpak():
    return Path("/.flatpak-info").is_file()


def _host_launcher():
    spawn = shutil.which("flatpak-spawn")
    if spawn is None:
        raise RuntimeError("Legacy installers require flatpak-spawn with host access inside Flatpak.")
    return [spawn, "--host", "--watch-bus", "--directory=/"]


def _host_path(path):
    path = Path(path)
    if _inside_flatpak() and path.is_relative_to("/run/host"):
        return Path("/") / path.relative_to("/run/host")
    return path


def _host_visible(path):
    info = Path(path).stat()
    probe = subprocess.run([*_host_launcher(), "stat", "--format=%d:%i", "--", str(_host_path(path))],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode and "stat:" not in probe.stderr:
        raise RuntimeError("Flatpak host access failed: " + probe.stderr.strip()[-1000:])
    return probe.returncode == 0 and probe.stdout.strip() == f"{info.st_dev}:{info.st_ino}"


def detect_omods(archive, extracted, files):
    archive, extracted = Path(archive), Path(extracted)
    if archive.suffix.lower() == ".omod":
        return [archive]
    return sorted((extracted / row["path"] for row in files
                   if Path(row["path"]).suffix.lower() == ".omod"),
                  key=lambda p: (len(p.relative_to(extracted).parts),
                                 p.relative_to(extracted).as_posix().casefold(),
                                 p.relative_to(extracted).as_posix()))


def helper_path():
    override = os.environ.get("AMETHYST_LEGACY_INSTALLER")
    root = Path(__file__).resolve().parent / "helper"
    executable = "Amethyst.LegacyInstaller.exe"
    path = Path(override) if override else root / "publish" / executable
    if override or path.is_file():
        if not path.is_file():
            raise RuntimeError("The selected legacy installer helper is unavailable.")
        return path.resolve()
    bundle = root / "runtime.zip"
    if not bundle.is_file():
        raise RuntimeError("The plugin's installer helper is unavailable. Reinstall the Legacy Scripted Installers plugin.")
    with bundle.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "AmethystModManager/legacy-installers/helpers"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / digest
    marker = destination / ".complete"
    if marker.is_file() and marker.read_text() == digest and (destination / executable).is_file():
        return (destination / executable).resolve()
    with tempfile.TemporaryDirectory(prefix="unpack-", dir=cache) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        with zipfile.ZipFile(bundle) as archive:
            entries = archive.infolist()
            if len(entries) > 1024 or sum(entry.file_size for entry in entries) > 256 * 1024 * 1024:
                raise ValueError("The bundled legacy runtime exceeds its extraction limits.")
            names = set()
            for entry in entries:
                name = _relative(entry.filename.rstrip("/"))
                if name.casefold() in names or entry.external_attr >> 16 & 0o170000 == 0o120000:
                    raise ValueError("The bundled legacy runtime contains duplicate paths or links.")
                names.add(name.casefold())
                target = staging / name
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        manifest = json.loads((staging / "build-manifest.json").read_text())
        files = manifest["files"]
        actual = {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()}
        if actual != set(files) | {"build-manifest.json"} or executable not in files:
            raise ValueError("The bundled legacy runtime is incomplete.")
        for name, expected in files.items():
            with (staging / _relative(name)).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError(f"The bundled legacy runtime failed verification: {name}")
        (staging / ".complete").write_text(digest)
        try:
            staging.rename(destination)
        except OSError:
            if not marker.is_file() or marker.read_text() != digest or not (destination / executable).is_file():
                raise
    return (destination / executable).resolve()


def _relative(value, *, empty=False):
    if not isinstance(value, str):
        raise ValueError("Installer paths must be strings.")
    value = value.replace("\\", "/")
    if empty and not value:
        return ""
    if (not value or len(value) > 4096 or ":" in value
            or any(ord(c) < 32 for c in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError(f"Unsafe installer path: {value!r}")
    return value


def _find(root, relative):
    current = Path(root)
    for part in relative.split("/"):
        exact = current / part
        if exact.exists():
            current = exact
            continue
        matches = [p for p in current.iterdir() if p.name.casefold() == part.casefold()] if current.is_dir() else []
        if len(matches) != 1:
            return None
        current = matches[0]
    return current if current.is_file() else None


class LegacyContext:
    def __init__(self, prepared):
        from Utils.mods.install import _collection_plugin_context
        self.prepared = prepared
        self.game = prepared.game
        self.profile = prepared.profile_dir
        self.installed, self.active, _ = _collection_plugin_context(
            self.game, self.profile, include_loose_files=False)
        self.loose = None
        self.sources = {}
        self.ini_targets = {}
        self.read_identities = {}
        self.ini_values = {}
        self.ini_contents = {}
        self.bsa_copies = {}
        self.work = None

    def source(self, relative):
        relative = _relative(relative)
        key = relative.casefold()
        if key not in self.sources:
            from Utils.filegraph.service import resolved_data_sources
            resolved = resolved_data_sources(self.game, self.profile, [relative])
            self.sources[key] = resolved.get(relative) or _find(self.game.get_mod_data_path(), relative)
        return self.sources[key]

    def ini_target(self, filename):
        filename = _relative(filename)
        allowed = {name.casefold(): name for name in (
            getattr(self.game, "_ARCHIVE_INI_FILENAME", ""),
            getattr(self.game, "_ARCHIVE_PREFS_INI_FILENAME", ""),
            getattr(self.game, "_CUSTOM_INI_FILENAME", "")) if name}
        if filename.casefold() not in allowed:
            raise ValueError(f"Unsupported game INI: {filename}")
        filename = allowed[filename.casefold()]
        if filename not in self.ini_targets:
            if getattr(self.game, "profile_ini_files", False):
                root = self.profile / getattr(self.game, "_PROFILE_INI_SUBDIR", "ini files")
                path = _find(root, filename)
            else:
                root = self.game._mygames_path()
                path = _find(root, filename) if root is not None else None
            if path is None:
                raise ValueError(f"Create {filename} for this profile before running its installer.")
            self.ini_targets[filename] = path.resolve()
        return self.ini_targets[filename]

    def read(self, path):
        path = Path(path)
        info = path.stat()
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError("Legacy installer reads are limited to 64 MiB per file.")
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if self.read_identities.setdefault(path, identity) != identity:
            raise ValueError("A file used by the legacy installer changed; run installation again.")
        content = path.read_bytes()
        after = path.stat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity:
            raise ValueError("A file used by the legacy installer changed while reading it.")
        return content

    def call(self, operation, args):
        args = args or {}
        if operation in {"read", "exists", "bsa"}:
            path = self.source(args["path"])
            if operation == "exists":
                return path is not None
            if path is None:
                raise FileNotFoundError(args["path"])
            if operation == "bsa":
                from Utils.wine.paths import to_wine_path
                if path not in self.bsa_copies:
                    info = path.stat()
                    self.read_identities.setdefault(path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
                    target = self.work / "reads" / (str(len(self.bsa_copies)) + ".bsa")
                    target.parent.mkdir(exist_ok=True)
                    _copy_file(path, target)
                    self.validate()
                    self.bsa_copies[path] = target
                return to_wine_path(self.bsa_copies[path])
            return base64.b64encode(self.read(path)).decode("ascii")
        if operation == "plugins":
            return sorted(self.active if args["active"] else self.installed)
        if operation == "mods":
            from Utils.mods.modlist import read_modlist
            return [row.name for row in read_modlist(self.profile / "modlist.txt") if row.enabled]
        if operation == "ini":
            import re
            path = self.ini_target(args["file"])
            if path not in self.ini_values:
                content = self.read(path)
                self.ini_contents[path] = content
                values, section = {}, ""
                for line in content.decode("utf-8", errors="replace").splitlines():
                    header = re.fullmatch(r"\s*\[([^\]]+)\]\s*", line)
                    if header:
                        section = header[1].strip().casefold()
                    elif "=" in line and not line.lstrip().startswith((";", "#")):
                        key, value = line.split("=", 1)
                        values.setdefault((section, key.strip().casefold()), value)
                self.ini_values[path] = values
            return self.ini_values[path].get((args["section"].strip("[]").casefold(), args["key"].casefold()), "")
        if operation == "list":
            if self.loose is None:
                from Utils.mods.install import _collection_plugin_context
                _, _, self.loose = _collection_plugin_context(self.game, self.profile)
            folder = _relative(args["path"].replace("\\", "/").rstrip("/"), empty=True).casefold()
            prefix = folder + "/" if folder else ""
            pattern = args["pattern"]
            if not isinstance(pattern, str) or "/" in pattern or "\\" in pattern:
                raise ValueError("Invalid installer file pattern.")
            return sorted(path for path in self.loose if path.startswith(prefix)
                          and (args["recursive"] or "/" not in path[len(prefix):])
                          and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern.casefold()))
        if operation in {"extender", "version"}:
            name = args.get("name", "")
            game_root = self.game.get_game_path()
            extender = getattr(self.game, "_script_extender_exe", "")
            if operation == "extender":
                return bool(extender and _find(game_root, extender) is not None)
            if name == "game":
                filename = getattr(self.game, "exe_name", "")
            elif name.casefold() == "obge":
                filename = "obse/plugins/obge.dll"
            elif "/" in name:
                filename = _relative(name)
            else:
                filename = extender
            path = _find(game_root, filename) if filename else None
            if path is None and "/" in filename:
                path = self.source(filename)
            if path is not None:
                from Utils.executables.icon import extract_exe_version
                version = extract_exe_version(path)
                if version:
                    return version
            return "0.0.0.0"
        raise ValueError(f"Unsupported legacy installer request: {operation}")

    def validate(self, *, ini_only=False):
        for path, identity in self.read_identities.items():
            if ini_only and path not in self.ini_targets.values():
                continue
            info = path.stat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != identity:
                raise ValueError("A file used by the legacy installer changed; run installation again.")


def _copy_file(source, target):
    copier = shutil.which("cp") if os.name != "nt" else None
    if copier:
        subprocess.run([copier, "--reflink=auto", "--", str(source), str(target)], check=True)
    else:
        shutil.copyfile(source, target)


def _sandbox(runner, helper, work, environment, arguments, *, source=None):
    flatpak = _inside_flatpak()
    if flatpak:
        command = [*_host_launcher(), "bwrap"]
    else:
        sandbox = shutil.which("bwrap")
        if sandbox is None:
            raise RuntimeError("Legacy installers require bubblewrap (bwrap) on Linux.")
        command = [sandbox]
    command += ["--die-with-parent", "--unshare-net", "--unshare-pid",
               "--unshare-ipc", "--new-session", "--cap-drop", "ALL", "--tmpfs", "/tmp",
               "--dev", "/dev", "--proc", "/proc", "--clearenv", "--chdir", str(work)]
    for key, value in environment.items():
        command += ["--setenv", key, value]
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if flatpak or Path(path).exists():
            command += ["--ro-bind-try" if flatpak else "--ro-bind", path, path]
    for path in ("/etc/ld.so.cache", "/etc/localtime", "/etc/passwd", "/etc/group",
                 "/etc/nsswitch.conf", "/etc/fonts", "/etc/alternatives", "/etc/vulkan",
                 "/sys/devices/system/cpu", "/sys/devices/system/node"):
        if flatpak or Path(path).exists():
            command += ["--ro-bind-try" if flatpak else "--ro-bind", path, path]
    for path in dict.fromkeys((runner.parent.parent, helper.parent, source)):
        if path is not None:
            command += ["--ro-bind", str(_host_path(path)), str(path)]
    command += ["--bind", str(_host_path(work)), str(work)]
    machine_id = work / "machine-id"
    if not machine_id.exists():
        machine_id.write_text(hashlib.sha256(str(work).encode()).hexdigest()[:32] + "\n")
    command += ["--ro-bind", str(machine_id), "/etc/machine-id"]
    reads = work / "reads"
    reads.mkdir(exist_ok=True)
    command += ["--ro-bind", str(reads), str(reads)]
    authority = environment.get("XAUTHORITY")
    if authority and Path(authority).is_file():
        command += ["--ro-bind", authority, authority]
    display = environment.get("DISPLAY", "")
    if display.startswith(":"):
        number = display[1:].split(".", 1)[0]
        if number.isdigit():
            socket = Path("/tmp/.X11-unix") / ("X" + number)
            if flatpak or socket.exists():
                command += ["--ro-bind-try" if flatpak else "--ro-bind", str(socket), str(socket)]
    return command + ["--", *map(str, arguments)]


def _wait_process(command, environment, work, log_path, cancel, *, timeout=120):
    from Utils.operations import OperationCancelled
    started = time.monotonic()
    with log_path.open("ab") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=errors,
                                   stderr=errors, env=os.environ.copy() if _inside_flatpak() else environment,
                                   cwd=work, start_new_session=True)
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise OperationCancelled("Legacy installation cancelled.")
                if time.monotonic() - started > timeout:
                    raise RuntimeError("Wine prefix preparation timed out.")
                time.sleep(0.1)
            if process.returncode:
                detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(f"Wine prefix preparation failed: {detail}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def _prepare_prefix(runner, helper, work, environment, log, cancel):
    import fcntl
    from Utils.operations import OperationCancelled
    server = runner.parent / "wineserver"
    runtime = runner.parent.parent / "lib" / "wine" / "x86_64-windows" / "ntdll.dll"
    identity = [(str(path), path.stat().st_size, path.stat().st_mtime_ns)
                for path in (runner, server, runtime) if path.is_file()]
    key = hashlib.sha256(json.dumps([2, identity]).encode()).hexdigest()[:24]
    root = Path(os.environ.get("AMETHYST_LEGACY_CACHE") or
                Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) /
                "AmethystModManager" / "legacy-installers")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _inside_flatpak() and not _host_visible(root):
        raise RuntimeError("Set AMETHYST_LEGACY_CACHE to a directory accessible from the Flatpak and host.")
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("The legacy installer cache must be owned by the current user.")
    root.chmod(0o700)
    template = root / key
    with (root / (key + ".lock")).open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if cancel is not None and cancel.is_set():
                    raise OperationCancelled("Legacy installation cancelled.")
                time.sleep(0.1)
        if not (template / "ready.json").is_file():
            log("Preparing a clean Wine prefix for this runner…")
            with tempfile.TemporaryDirectory(prefix=key + "-", dir=root) as temporary:
                seed = Path(temporary)
                for folder in ("prefix", "home", "tmp", "cache", "runtime"):
                    (seed / folder).mkdir()
                seed_env = environment.copy()
                seed_env.update(WINEPREFIX=str(seed / "prefix"), HOME=str(seed / "home"),
                                TMPDIR=str(seed / "tmp"), XDG_CACHE_HOME=str(seed / "cache"),
                                XDG_RUNTIME_DIR=str(seed / "runtime"))
                script = '"$1" wineboot.exe --init; result=$?; "$2" -k; "$2" -w; exit "$result"'
                command = _sandbox(runner, helper, seed, seed_env,
                                   ["/bin/sh", "-c", script, "sh", runner, server])
                _wait_process(command, seed_env, seed, seed / "boot.log", cancel)
                for path in (seed / "prefix").rglob("*"):
                    if path.is_symlink() and path.name not in {"c:", "z:"}:
                        target = path.resolve()
                        if not target.is_relative_to(seed / "prefix"):
                            path.unlink()
                            path.mkdir()
                (seed / "ready.json").write_text(json.dumps(identity), encoding="utf-8")
                if template.exists():
                    shutil.rmtree(template)
                seed.rename(template)
        subprocess.run(["cp", "-a", "--reflink=auto", "--", str(template / "prefix") + "/.",
                        str(work / "prefix")], check=True)
    log("Using an isolated copy of the clean Wine prefix.")


def _command(game, work, request, log, cancel=None):
    helper = helper_path()
    if os.name == "nt":
        return [str(helper), str(request)], os.environ.copy()
    from Utils.launchers.steam import find_any_installed_proton
    from Utils.wine.paths import to_wine_path
    from Utils.wine.proton import resolve_proton_env
    from Utils.wine.protontricks import strip_appimage_env
    runner, environment = resolve_proton_env(game, log, read_only=True)
    environment = strip_appimage_env((environment or os.environ).copy())
    runner = Path(runner) if runner else find_any_installed_proton()
    if runner is not None:
        runner = Path(runner)
        if runner.name not in {"wine", "wine64"}:
            runner = next((runner.parent / folder / name for folder in ("files/bin", "dist/bin")
                           for name in ("wine64", "wine") if (runner.parent / folder / name).is_file()), None)
    runner = runner or shutil.which("wine64") or shutil.which("wine")
    if runner is None:
        raise RuntimeError("Install Wine or Proton to run legacy mod installers.")
    if not _inside_flatpak() and shutil.which("bwrap") is None:
        raise RuntimeError("Legacy installers require bubblewrap (bwrap) on Linux.")
    prefix = work / "prefix"
    prefix.mkdir()
    for folder in ("home", "tmp", "cache"):
        (work / folder).mkdir()
    runner = Path(runner).resolve()
    environment = {key: value for key, value in environment.items()
                   if key in {"DISPLAY", "XAUTHORITY", "LANG", "TZ"} or key.startswith("LC_")}
    authority = environment.get("XAUTHORITY")
    if _inside_flatpak() and authority and Path(authority).is_file():
        target = work / "Xauthority"
        _copy_file(authority, target)
        target.chmod(0o600)
        environment["XAUTHORITY"] = str(target)
    (work / "runtime").mkdir(mode=0o700)
    environment.update(PATH=str(runner.parent) + ":/usr/bin:/bin", WINEPREFIX=str(prefix),
                       WINEARCH="win64", WINEDEBUG="-all", WINEDLLOVERRIDES="mshtml=;winemenubuilder.exe=",
                       DOTNET_EnableWriteXorExecute="0", DOTNET_SYSTEM_GLOBALIZATION_USENLS="1",
                       HOME=str(work / "home"), TMPDIR=str(work / "tmp"),
                       XDG_CACHE_HOME=str(work / "cache"), XDG_RUNTIME_DIR=str(work / "runtime"))
    _prepare_prefix(runner, helper, work, environment, log, cancel)
    command = _sandbox(runner, helper, work, environment, [runner, helper, to_wine_path(request)],
                       source=work.parent)
    return command, os.environ.copy() if _inside_flatpak() else environment


def run_installer(prepared, *, log, cancel=None, work=None):
    work = work or prepared.extract_dir / ".plugin-install"
    work.mkdir(exist_ok=True)
    if not _inside_flatpak() or (_host_visible(prepared.extract_dir) and _host_visible(work)):
        return _run_installer(prepared, log=log, cancel=cancel, work=work)
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "AmethystModManager/legacy-installers/sessions"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _host_visible(cache):
        raise RuntimeError("Legacy installers need a Flatpak cache directory accessible from the host.")
    log("Copying installer inputs from Flatpak temporary storage to the shared cache…")
    with tempfile.TemporaryDirectory(prefix="install-", dir=cache) as temporary:
        shared = Path(temporary)
        source_root = shared / "source"
        source_root.mkdir()
        subprocess.run(["cp", "-a", "--reflink=auto", "--", str(prepared.extract_dir) + "/.", str(source_root)], check=True)
        relocated = copy.copy(prepared)
        relocated.extract_dir = source_root
        kind, source, script = prepared.legacy_installer
        if source.is_relative_to(prepared.extract_dir):
            source = source_root / source.relative_to(prepared.extract_dir)
        relocated.legacy_installer = (kind, source,
            source_root / script.relative_to(prepared.extract_dir) if script else None)
        relocated.legacy_packages = [source_root / path.relative_to(prepared.extract_dir)
                                     for path in getattr(prepared, "legacy_packages", ())]
        shared_work = source_root / ".plugin-install"
        try:
            output, files, edits, plugins, reader = _run_installer(relocated, log=log, cancel=cancel, work=shared_work)
            shutil.copytree(output, work / "output")
            reader.work = work
            return work / "output", files, edits, plugins, reader
        finally:
            for name in ("runner.log", "result.json"):
                path = shared_work / name
                if path.is_file():
                    shutil.copy2(path, work / name)


def _run_installer(prepared, *, log, cancel=None, work=None):
    from Utils.operations import OperationCancelled
    from Utils.wine.paths import to_wine_path
    work = work or prepared.extract_dir / ".plugin-install"
    work.mkdir(exist_ok=True)
    context = LegacyContext(prepared)
    context.work = work
    started = time.monotonic()
    output, result = work / "output", work / "result.json"
    kind, source, script = prepared.legacy_installer
    if kind == "omod" and not source.is_relative_to(prepared.extract_dir):
        snapshot = work / "input.omod"
        _copy_file(source, snapshot)
        source = snapshot
    wine_path = str if os.name == "nt" else to_wine_path
    request = work / "request.json"
    payload = {"kind": kind, "source": wine_path(source),
        "script": wine_path(script) if script else "", "output": wine_path(output),
        "work": wine_path(work / "unpacked"), "result": wine_path(result),
        "memory_budget": min(1024 ** 3, os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 4)
            if os.name != "nt" else 1024 ** 3}
    packages = getattr(prepared, "legacy_packages", ())
    if packages:
        parent = Path(os.path.commonpath([path.parent for path in packages]))
        payload["packages"] = []
        for path in packages:
            if (not path.is_relative_to(prepared.extract_dir) or not path.is_file()
                    or path.resolve() != path.absolute()):
                raise ValueError("OMOD package must be a file inside the extracted archive without links.")
            payload["packages"].append({"source": wine_path(path), "label": path.relative_to(parent).as_posix()})
    request.write_text(json.dumps(payload), encoding="utf-8")
    command, env = _command(prepared.game, work, request, log, cancel)
    log(f"Legacy installer preparation: {time.monotonic() - started:.2f}s.")
    with _runner_lock, (work / "runner.log").open("wb") as errors:
        log(f"Opening {kind.upper()} legacy installer for {prepared.mod_name}…")
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, env=env, cwd=work, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b""
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise OperationCancelled("Legacy installation cancelled.")
                for key, _ in selector.select(0.2):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    buffer += block
                    if len(buffer) > 1024 * 1024:
                        raise ValueError("Invalid legacy installer protocol output.")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(message, dict) or "op" not in message:
                            continue
                        try:
                            reply = {"result": context.call(message["op"], message.get("args"))}
                        except Exception as exc:
                            raise RuntimeError(f"Legacy installer request {message['op']} failed: {exc}") from exc
                        process.stdin.write(json.dumps(reply).encode("utf-8") + b"\n")
                        process.stdin.flush()
            if process.returncode == 2:
                raise OperationCancelled("Legacy installer cancelled.")
            if process.returncode or not result.is_file():
                errors.flush()
                detail = (work / "runner.log").read_text(encoding="utf-8", errors="replace")[-6000:]
                raise RuntimeError(f"Legacy installer cancelled or failed. {detail}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdin.close()
            process.stdout.close()
            selector.close()
    for line in (work / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("Legacy timing:", "Legacy package:")):
            log(line[:300])
    if cancel is not None and cancel.is_set():
        raise OperationCancelled("Legacy installation cancelled.")
    context.validate()
    if result.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("The legacy installer result exceeds 4 MiB.")
    plan = json.loads(result.read_text(encoding="utf-8"))
    if plan.get("version") != 1 or not isinstance(plan.get("ini"), list) or not isinstance(plan.get("plugins"), dict):
        raise ValueError("Invalid legacy installer result.")
    files = []
    destinations = set()
    for path in output.rglob("*"):
        if path.is_symlink():
            raise ValueError("Legacy installer output cannot contain symlinks.")
        if path.is_file():
            relative = _relative(path.relative_to(output).as_posix())
            if relative.casefold() in destinations or relative.casefold() == "meta.ini":
                raise ValueError(f"Conflicting legacy installer output: {relative}")
            destinations.add(relative.casefold())
            files.append((relative, relative, False))
    if not files:
        raise ValueError("The legacy installer did not select any files.")
    ini_edits = []
    for edit in plan["ini"]:
        if not isinstance(edit, dict) or set(edit) != {"file", "section", "key", "value"}:
            raise ValueError("Invalid legacy INI edit.")
        if any(not isinstance(value, str) or "\n" in value or "\r" in value for value in edit.values()):
            raise ValueError("Invalid legacy INI value.")
        target = context.ini_target(edit["file"])
        context.ini_contents.setdefault(target, context.read(target))
        ini_edits.append((target, edit["section"].strip("[]"), edit["key"], edit["value"]))
    for plugin, enabled in plan["plugins"].items():
        if "/" in _relative(plugin) or not isinstance(enabled, bool) or not plugin.lower().endswith((".esp", ".esm", ".esl")):
            raise ValueError("Invalid legacy plugin activation.")
        if plugin.casefold() not in destinations and plugin.casefold() not in context.installed:
            raise ValueError(f"The installer requested an absent plugin: {plugin}")
    if ini_edits and plan.get("settings_confirmed") is not True:
        raise OperationCancelled("The legacy helper did not confirm the game INI changes; rebuild the helper.")
    context.validate()
    return output, files, ini_edits, plan["plugins"], context


def apply_settings(prepared, edits, plugins, log, *, reader, writes, work):
    from Games.Bethesda.bethesda_ini import _set_ini_key
    from Utils.atomic_write import write_atomic
    from Utils.plugins import read_plugins, write_plugins, PluginEntry
    plugin_path = prepared.profile_dir / "plugins.txt"
    pending = {}
    with tempfile.TemporaryDirectory(prefix="settings-", dir=work) as temporary:
        for path, section, key, value in edits:
            if path not in pending:
                target = Path(temporary) / str(len(pending))
                target.write_bytes(reader.ini_contents[path])
                pending[path] = target
            _set_ini_key(pending[path], section, key, value, case_insensitive=True)
        reader.validate(ini_only=True)
        for path, target in pending.items():
            before, after = reader.ini_contents[path], target.read_bytes()
            if path.read_bytes() != before:
                raise ValueError(f"{path.name} changed; run installation again.")
            if before != after:
                writes[path] = (before, after)
                write_atomic(path, after)
    if plugins:
        star = bool(getattr(prepared.game, "plugins_use_star_prefix", True))
        rows = read_plugins(plugin_path, star_prefix=star)
        wanted = {name.casefold(): enabled for name, enabled in plugins.items()}
        rows = [PluginEntry(row.name, wanted.get(row.name.casefold(), row.enabled)) for row in rows]
        present = {row.name.casefold() for row in rows}
        rows.extend(PluginEntry(name, enabled) for name, enabled in plugins.items() if name.casefold() not in present)
        write_plugins(plugin_path, rows, star_prefix=star)
    log(f"Legacy installer: applied {len(edits)} INI change(s).")
