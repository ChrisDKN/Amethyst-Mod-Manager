from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.wine import registry


_PACKAGES = {
    "4.0": ("dotNetFx40_Full_x86_x64.exe",
            "https://download.microsoft.com/download/9/5/A/95A9616B-7A37-4AF6-BC36-D6EA96C8DAAE/dotNetFx40_Full_x86_x64.exe",
            "65e064258f2e418816b304f646ff9e87af101e4c9552ab064bb74d281c38659f"),
    "4.8": ("ndp48-x86-x64-allos-enu.exe",
            "https://download.visualstudio.microsoft.com/download/pr/7afca223-55d2-470a-8edc-6a1739ae3252/abd170b4b0ec15ad0222a809b761a036/ndp48-x86-x64-allos-enu.exe",
            "95889d6de3f2070c07790ad6cf2000d33d9a1bdfc6a381725ab82ab1c314fd53"),
}
_PROBE = '''using System;
class Verify {
    static int Main() {
        if (Type.GetType("Mono.Runtime") != null || Environment.Version.Major != 4) return 1;
        Console.WriteLine("AMETHYST_CLR_OK " + Environment.Version);
        return 0;
    }
}
'''


def _exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _download(version, log):
    from Utils.ca_bundle import download_file
    from Utils.config_paths import get_dotnet_cache_dir
    name, url, expected = _PACKAGES[version]
    target = get_dotnet_cache_dir() / name

    def valid(path):
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == expected

    if not valid(target):
        log(f"Downloading Microsoft .NET Framework {version}…")
        with tempfile.TemporaryDirectory(prefix="framework-", dir=target.parent) as folder:
            pending = Path(folder) / name
            download_file(url, pending)
            if not valid(pending):
                raise RuntimeError(f".NET Framework {version} download failed checksum verification")
            pending.replace(target)
    return target


class _Installer:
    def __init__(self, proton, env, prefix, log):
        self.proton = Path(proton)
        self.prefix = registry.normalize_pfx(Path(prefix)).resolve()
        self.env = dict(env, WINEPREFIX=str(self.prefix), WINEDEBUG="-all",
                        PROTON_USE_XALIA="0", PROTON_DISABLE_XALIA="1")
        self.log = log
        self.job = self.prefix.parent / (".amethyst-dotnet48-" + hashlib.sha256(str(self.prefix).encode()).hexdigest()[:16])
        self.backup = self.job / "backup"
        self.journal = self.job / "state.json"
        self.marker = self.job.name + ".snapshot"
        self.started = False

    @contextmanager
    def locked(self):
        with (self.job / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(".NET Framework setup is already running for this prefix") from None
            try:
                yield
            finally:
                if self.started:
                    self.stop()

    def run(self, *args, label=".NET Framework setup", timeout=120, allow_failure=False, overrides=None):
        from Utils.launchers.steam import proton_run_command
        from Utils.wine.protontricks import run_prefix_installer
        env = dict(self.env)
        if overrides:
            env["WINEDLLOVERRIDES"] = overrides
        self.started = True
        rc, output = run_prefix_installer(
            proton_run_command(self.proton, "runinprefix", *map(str, args), env=env, host_cwd=self.prefix),
            env, self.prefix, label=label, log_fn=self.log, timeout=timeout,
            proton_script=self.proton, compat_data=env.get("STEAM_COMPAT_DATA_PATH", self.prefix))
        if rc not in (0, 194) and not allow_failure:
            details = self.job / "installer-error.log"
            details.write_text(output, encoding="utf-8")
            raise RuntimeError(f"{label} failed (exit {rc}): {output}")
        return rc, output

    def stop(self):
        from Utils.executables.launch import shutdown_prefix_wineserver
        from Utils.processes.game import matching_pids, prefix_markers
        shutdown_prefix_wineserver(self.proton, self.env.get("STEAM_COMPAT_DATA_PATH", self.prefix))
        import time
        for _ in range(50):
            pids = matching_pids(prefix_markers(self.prefix))
            if pids == set():
                return
            time.sleep(0.1)
        raise RuntimeError("Prefix processes have not stopped; the backup is retained. Close its game/tools and retry.")

    def state(self, value):
        write_atomic_text(self.journal, json.dumps({"prefix": str(self.prefix), "state": value}))
        with self.journal.open("rb") as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(self.job, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def recover(self):
        if not self.journal.is_file():
            if self.backup.exists():
                raise RuntimeError(f"Unidentified .NET backup needs review: {self.backup}")
            return
        data = json.loads(self.journal.read_text())
        if data.get("prefix") != str(self.prefix):
            raise RuntimeError(".NET backup belongs to a different prefix")
        state = data.get("state")
        if state in {"changing", "restoring"}:
            self.stop()
            self.log("Restoring the prefix backup from the unsuccessful .NET installation…")
            if (self.backup / self.marker).is_file():
                self.state("restoring")
                _exchange(self.prefix, self.backup)
            elif state != "restoring" or not (self.prefix / self.marker).is_file():
                raise RuntimeError(f".NET prefix backup is missing: {self.backup}")
            self.state("restored")
        elif state not in {"copying", "complete", "restored"}:
            raise RuntimeError(f"Unknown .NET recovery state: {state}")
        if self.backup.exists():
            shutil.rmtree(self.backup)
        (self.prefix / self.marker).unlink(missing_ok=True)
        self.journal.unlink()

    def snapshot(self):
        self.stop()
        self.log("Backing up the game prefix before .NET Framework setup…")
        self.state("copying")
        result = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(self.prefix), str(self.backup)],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise RuntimeError(f"Could not back up the prefix: {result.stderr}")
        if shutil.disk_usage(self.prefix).free < 2 * 1024 ** 3:
            raise RuntimeError(".NET Framework setup needs at least 2 GiB free after the prefix backup")
        (self.backup / self.marker).write_text(str(self.prefix))
        first, second = self.job / "exchange-a", self.job / "exchange-b"
        first.mkdir()
        second.mkdir()
        try:
            _exchange(first, second)
        finally:
            first.rmdir()
            second.rmdir()
        self.state("changing")

    def native_clr(self):
        from Utils.wine.health import dll_origin, DllOrigin
        return any(dll_origin(self.prefix, "clr.dll", subdir=f"Microsoft.NET/{folder}/v4.0.30319") is DllOrigin.NATIVE
                   for folder in ("Framework", "Framework64"))

    def configure(self):
        for name in ("mscoree", "*mscoree"):
            self.run("reg", "add", r"HKCU\Software\Wine\DllOverrides", "/v", name,
                     "/t", "REG_SZ", "/d", "native", "/f")
        for key in (r"HKLM\Software\Microsoft\.NETFramework", r"HKLM\Software\Wow6432Node\Microsoft\.NETFramework"):
            self.run("reg", "add", key, "/v", "OnlyUseLatestCLR", "/t", "REG_DWORD", "/d", "1", "/f")

    def remove_mono(self):
        self.log("Removing Wine Mono from this prefix and clearing its .NET setup entries…")
        products = set()
        for root in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            pattern = re.escape(registry.escape_key(root)) + r"\\\\\{[0-9A-Fa-f-]{36}\}"
            for key, values in registry.find_sections(self.prefix, pattern):
                if values.get("displayname", "").startswith("Wine Mono"):
                    products.add(key.rsplit("\\", 1)[-1])
        for guid in sorted(products):
            self.run("msiexec", "/x", guid, "/qn", "/norestart", label="Removing Wine Mono", timeout=300)
        for key in (r"HKLM\Software\Microsoft\NET Framework Setup\NDP\v4",
                    r"HKLM\Software\Wow6432Node\Microsoft\NET Framework Setup\NDP\v4"):
            self.run("reg", "delete", key, "/f", allow_failure=True)
        self.stop()
        from Utils.wine.health import dll_origin, DllOrigin
        for folder in ("system32", "syswow64"):
            if dll_origin(self.prefix, "mscoree.dll", subdir=folder) is DllOrigin.BUILTIN:
                (self.prefix / "drive_c/windows" / folder / "mscoree.dll").unlink()

    def probe(self):
        from Utils.wine.health import detect_dotnet48
        if detect_dotnet48(self.prefix) is not True:
            return False
        self.log("Checking Microsoft CLR execution…")
        temp = self.prefix / "drive_c/windows/temp"
        temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="amethyst-clr-", dir=temp) as folder:
            source = Path(folder) / "verify.cs"
            source.write_text(_PROBE)
            win = "C:\\" + str(source.relative_to(self.prefix / "drive_c")).replace("/", "\\")
            architectures = [("Framework", "x86")]
            if (self.prefix / "drive_c/windows/syswow64").is_dir():
                architectures.append(("Framework64", "x64"))
            for framework, arch in architectures:
                output = win + "." + arch + ".exe"
                compiler = f"C:\\windows\\Microsoft.NET\\{framework}\\v4.0.30319\\csc.exe"
                self.run(compiler, "/nologo", "/noconfig", "/platform:" + arch, "/out:" + output, win,
                         label=f"Verifying {arch} .NET compiler")
                _, text = self.run(output, label=f"Verifying {arch} Microsoft CLR")
                if "AMETHYST_CLR_OK " not in text:
                    raise RuntimeError(f"The {arch} Microsoft CLR did not pass verification: {text}")
        return True

    def install(self):
        from Utils.processes.game import matching_pids, prefix_markers
        if matching_pids(prefix_markers(self.prefix)) != set():
            raise RuntimeError("Close the game and all tools using its prefix before installing .NET Framework 4.8")
        self.job.mkdir(exist_ok=True, mode=0o700)
        if self.job.is_symlink() or any((self.job / name).is_symlink() for name in ("backup", "failed", "state.json", "lock")):
            raise RuntimeError(f"Unexpected symlink in the .NET recovery directory: {self.job}")
        with self.locked():
            self.recover()
            try:
                if self.probe():
                    self.log(".NET Framework 4.8 is already installed and passed execution checks.")
                    return True
            except RuntimeError as exc:
                self.log(f"Existing .NET installation needs repair: {exc}")
                self.stop()
            packages = {v: _download(v, self.log) for v in (("4.8",) if self.native_clr() else ("4.0", "4.8"))}
            try:
                self.snapshot()
            except Exception:
                self.recover()
                raise
            try:
                version = registry.read_value(self.prefix, r"Software\Wine", "Version", hive=registry.HIVE_USER)
                if "4.0" in packages:
                    self.remove_mono()
                    self.run("winecfg", "-v", "winxp")
                    self.log("Installing .NET Framework 4.0 prerequisite; this can take several minutes…")
                    self.run(packages["4.0"], "/q", '/c:install.exe /q /norestart',
                             label="Installing .NET Framework 4.0", timeout=1800, overrides="fusion=b")
                    self.configure()
                self.run("winecfg", "-v", "win7")
                self.log("Installing .NET Framework 4.8; this can take several minutes…")
                release = registry.read_value(self.prefix, r"Software\Microsoft\NET Framework Setup\NDP\v4\Full", "Release") or "0"
                try:
                    installed = int(release.removeprefix("dword:"), 16 if release.startswith("dword:") else 10)
                except ValueError:
                    installed = 0
                repair = ["/repair"] if installed >= 528040 and self.native_clr() else []
                self.run(packages["4.8"], *repair, "/q", "/norestart", label="Installing .NET Framework 4.8",
                         timeout=1800, overrides="fusion=b")
                self.configure()
                if version:
                    self.run("winecfg", "-v", version)
                else:
                    self.run("reg", "delete", r"HKCU\Software\Wine", "/v", "Version", "/f", allow_failure=True)
                self.stop()
                if not self.probe():
                    raise RuntimeError("Microsoft .NET Framework 4.8 files or registry verification failed")
                self.stop()
                self.state("complete")
            except BaseException:
                self.recover()
                raise
            self.recover()
            from Utils.wine.protontricks import mark_dep_installed, winetricks_verb_dep_key
            mark_dep_installed(self.prefix, winetricks_verb_dep_key("dotnet48"))
            self.log(".NET Framework 4.8 installed; Microsoft CLR execution verified.")
            return True


def install_framework_runtime(proton, env, prefix, log_fn):
    from Utils.wine.protontricks import prefix_downgrade_warning
    if not prefix or not (registry.normalize_pfx(Path(prefix)) / "user.reg").is_file():
        log_fn("Configure and initialize the selected game prefix before installing .NET Framework 4.8.")
        return False
    warning = prefix_downgrade_warning(proton, env.get("STEAM_COMPAT_DATA_PATH"))
    if warning:
        log_fn(warning)
        return False
    installer = _Installer(proton, env, prefix, log_fn)
    compat = env.get("STEAM_COMPAT_DATA_PATH")
    if Path(proton).name not in {"wine", "wine64"} and (
            not compat or (Path(compat) / "pfx").resolve() != installer.prefix):
        log_fn("The selected Proton environment does not point to the configured prefix.")
        return False
    try:
        return installer.install()
    except Exception as exc:
        log_fn(f".NET Framework 4.8: {exc}")
        return False
