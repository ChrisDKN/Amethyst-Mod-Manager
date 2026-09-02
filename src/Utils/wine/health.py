"""Detect what a Wine/Proton prefix actually contains, independent of markers.

``protontricks.amethyst_deps.json`` only records what *Amethyst* installed, so
prefixes built by hand (winetricks/protontricks directly, another manager, or
Amethyst before the marker existed) look empty and get needlessly re-installed.
These detectors read the prefix itself instead.

The ground truth is a quirk of Wine's PE builtins: every builtin DLL carries the
ASCII bytes ``Wine builtin DLL`` at byte offset 64, in the DOS stub where a real
PE has "This program cannot be run in DOS mode". A 16-byte read at that offset
therefore tells Wine's own DLL apart from a genuine Microsoft one. Verified
across ~18 real compatdata prefixes.

The obvious registry check is a **trap** and is deliberately not used: Proton
pre-seeds ``Software\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64`` with
``Installed=dword:00000001`` in *every* prefix it creates while the DLLs are
still Wine builtins. See :func:`check_vcredist`.

Import rule, load-bearing: this module must never import ``Utils.wine.protontricks``
at module scope, and no detector may call ``is_dep_installed`` - the marker ->
detector map lives in ``protontricks`` so the dependency stays one-way and both
an import cycle and infinite recursion are structurally impossible. The single
function-local ``protontricks`` import (``prefix_downgrade_warning``, inside
:func:`check_proton_binding`) is unreachable from the ``is_dep_installed`` path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from Utils.wine import registry as wine_reg
from Utils.wine.proton import DOTNET_VERSIONS

# --- Wine builtin detection -------------------------------------------------
BUILTIN_MARKER = b"Wine builtin DLL"
BUILTIN_MARKER_OFFSET = 64
# Below this a file cannot be a real DLL. compatdata/0/pfx really does contain
# a 0-byte d3dcompiler_47.dll, and calling that "native" would report a broken
# prefix as healthy.
_MIN_PE_BYTES = 4096

# Known d3dcompiler_47 builds, by exact size.
_D3D_FXC2_SIZE = 4_346_120        # Mozilla fxc2 (Win 8.1 SDK) - what we install
_D3D_WINETRICKS_SIZE = 4_173_928  # winetricks verb's older DLL - fails X3676

_VCREDIST_DLLS = (
    "vcruntime140.dll",
    "msvcp140.dll",
    "vcruntime140_1.dll",
    "msvcp140_atomic_wait.dll",
)
# The x64 runtime is what Amethyst installs (vc_redist.x64.exe), so these three
# decide the verdict; msvcp140_atomic_wait is reported but never flips it.
_VCREDIST_REQUIRED = _VCREDIST_DLLS[:3]

# Only the genuine Microsoft bundle installer writes these. Used for the
# version string shown to the user - never as a presence test.
_VCREDIST_BUNDLE_RE = (
    re.escape(wine_reg.escape_key(r"Software\Classes\Installer\Dependencies"))
    + r"\\\\VC,redist\.(?:x64|x86),[^,]+,[^,]+,bundle"
)

_LAV_DIR = ("drive_c", "Program Files (x86)", "LAV Filters")
_LAV_AX = ("LAVSplitter.ax", "LAVAudio.ax", "LAVVideo.ax")

_BETHESDA_KEY_64 = r"Software\Bethesda Softworks"
_BETHESDA_KEY_32 = r"Software\Wow6432Node\Bethesda Softworks"
_INSTALLED_PATH_VALUE = "Installed Path"


class HealthStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    WARN = "warn"
    UNKNOWN = "unknown"


class DllOrigin(str, Enum):
    ABSENT = "absent"
    BUILTIN = "builtin"        # Wine's own PE
    NATIVE = "native"          # a genuine Microsoft/vendor DLL
    UNREADABLE = "unreadable"  # 0-byte, truncated or unreadable


@dataclass(frozen=True)
class HealthCheck:
    """One row of a prefix health report.

    *check_id* is stable and untranslated so the GUI can map it to a localised
    label; *detail* is deliberately technical (paths, sizes, versions) and is
    shown verbatim, since Utils/ is never translated. *fix_token* is None for
    rows nothing can auto-repair.
    """
    check_id: str
    status: HealthStatus
    label: str
    detail: str = ""
    fix_token: "str | None" = None
    evidence: dict = field(default_factory=dict)


# --- low-level probes -------------------------------------------------------
def dll_origin(pfx: Path, dll_name: str, *, subdir: str = "system32") -> DllOrigin:
    """Classify ``drive_c/windows/<subdir>/<dll_name>`` as builtin or native.

    Callers MUST treat UNREADABLE as "not installed" - never as native.
    """
    path = Path(pfx) / "drive_c" / "windows" / subdir / dll_name
    try:
        size = path.stat().st_size
    except OSError:
        return DllOrigin.ABSENT
    if size < _MIN_PE_BYTES:
        return DllOrigin.UNREADABLE
    try:
        with path.open("rb") as fh:
            fh.seek(BUILTIN_MARKER_OFFSET)
            marker = fh.read(len(BUILTIN_MARKER))
    except OSError:
        return DllOrigin.UNREADABLE
    return DllOrigin.BUILTIN if marker == BUILTIN_MARKER else DllOrigin.NATIVE


def dll_size(pfx: Path, dll_name: str, *, subdir: str = "system32") -> "int | None":
    """Size of a prefix DLL in bytes, or None when it is absent/unreadable."""
    try:
        return (Path(pfx) / "drive_c" / "windows" / subdir / dll_name).stat().st_size
    except OSError:
        return None


def _prefix_usable(pfx: Path) -> bool:
    """True when *pfx* looks like a real Wine prefix we can interrogate."""
    pfx = Path(pfx)
    return (pfx / "drive_c").is_dir() and (pfx / wine_reg.HIVE_USER).is_file()


# --- VC++ Redistributable ---------------------------------------------------
def detect_vcredist(prefix_path: Path) -> "bool | None":
    """True when the native x64 VC++ runtime DLLs are in the prefix.

    Cheap: a stat plus a 16-byte read per DLL, no registry parsing. None when
    the prefix is unusable, meaning "cannot tell".
    """
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    if not _prefix_usable(pfx):
        return None
    return all(dll_origin(pfx, d) is DllOrigin.NATIVE for d in _VCREDIST_REQUIRED)


def _vcredist_bundles(pfx: Path) -> list[str]:
    """DisplayName of each genuine VC++ redist bundle recorded in system.reg.

    x64 first - that is the runtime Amethyst installs, and a prefix often
    carries an unrelated x86 bundle a game's own installer dropped in.
    """
    out: list[str] = []
    for key, values in wine_reg.find_sections(pfx, _VCREDIST_BUNDLE_RE):
        name = values.get("displayname") or values.get("version")
        if name:
            out.append((0 if ",x64," in key.lower() else 1, name))
    return [name for _rank, name in sorted(set(out))]


def check_vcredist(prefix_path: Path) -> HealthCheck:
    """Report the VC++ Redistributable state of a prefix.

    Deliberately ignores ``Software\\Microsoft\\VisualStudio\\14.0\\VC\\
    Runtimes\\x64``: Proton pre-seeds that key with ``Installed=1`` and a
    plausible Version in every prefix it creates, while the DLLs are still Wine
    builtins (verified on prefixes 1066890, 1449850, 220200, 2060160). Only the
    DLL bytes decide the verdict.
    """
    label = "VC++ Redistributable (x64)"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    origins = {d: dll_origin(pfx, d) for d in _VCREDIST_DLLS}
    evidence = {d: o.value for d, o in origins.items()}

    native = [d for d in _VCREDIST_REQUIRED if origins[d] is DllOrigin.NATIVE]
    non_native = [d for d in _VCREDIST_REQUIRED if origins[d] is not DllOrigin.NATIVE]

    bundles = _vcredist_bundles(pfx)
    if bundles:
        evidence["bundles"] = bundles
    suffix = f" ({'; '.join(bundles)})" if bundles else ""

    if not non_native:
        return HealthCheck("vcredist", HealthStatus.OK, label,
                           f"native runtime DLLs installed{suffix}",
                           None, evidence)
    if native:
        return HealthCheck(
            "vcredist", HealthStatus.WARN, label,
            "partial install - still Wine builtins: " + ", ".join(non_native) + suffix,
            "vcredist", evidence)
    return HealthCheck("vcredist", HealthStatus.MISSING, label,
                       "not installed - the prefix has only Wine's builtin "
                       "vcruntime/msvcp DLLs",
                       "vcredist", evidence)


# --- d3dcompiler_47 ---------------------------------------------------------
def detect_d3dcompiler_47(prefix_path: Path) -> "bool | None":
    """True when a native d3dcompiler_47.dll has been dropped into the prefix."""
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    if not _prefix_usable(pfx):
        return None
    return dll_origin(pfx, "d3dcompiler_47.dll") is DllOrigin.NATIVE


def _d3d_build_name(size: "int | None") -> str:
    if size == _D3D_FXC2_SIZE:
        return "Mozilla fxc2 (Win 8.1 SDK) build"
    if size == _D3D_WINETRICKS_SIZE:
        return ("winetricks/older Microsoft build - may fail Community Shaders "
                "/ ENB with shader error X3676")
    return f"unrecognised native build ({size} bytes)"


def check_d3dcompiler_47(prefix_path: Path) -> HealthCheck:
    """Report the d3dcompiler_47 state, naming which build is installed."""
    label = "d3dcompiler_47 (shader compiler)"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    origin = dll_origin(pfx, "d3dcompiler_47.dll")
    size = dll_size(pfx, "d3dcompiler_47.dll")
    evidence = {"origin": origin.value, "size": size}

    if origin is not DllOrigin.NATIVE:
        detail = {
            DllOrigin.BUILTIN: "not installed - the prefix has Wine's builtin DLL",
            DllOrigin.ABSENT: "not installed - d3dcompiler_47.dll is absent",
            DllOrigin.UNREADABLE: f"not installed - the DLL is a {size}-byte stub",
        }[origin]
        return HealthCheck("d3dcompiler_47", HealthStatus.MISSING, label,
                           detail, "d3dcompiler_47", evidence)

    # Native DLL present. Wine falls back to native once the builtin file is
    # overwritten, but the explicit override is what our installer sets and is
    # what makes the choice deterministic.
    override = wine_reg.read_value(
        pfx, r"Software\Wine\DllOverrides", "d3dcompiler_47",
        hive=wine_reg.HIVE_USER)
    evidence["override"] = override
    build = _d3d_build_name(size)
    if not override:
        return HealthCheck(
            "d3dcompiler_47", HealthStatus.WARN, label,
            f"native DLL present ({build}) but no 'native' DLL override is set "
            "- Wine may still prefer its builtin",
            "d3dcompiler_47", evidence)
    return HealthCheck("d3dcompiler_47", HealthStatus.OK, label,
                       f"{build}, override = {override}", None, evidence)


# --- legacy DirectX redist DLLs ---------------------------------------------
_DX_REDIST_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    # Single-DLL verbs.
    "d3dcompiler_42": ("d3dcompiler_42 (legacy shader compiler)",
                       ("d3dcompiler_42",)),
    "d3dcompiler_43": ("d3dcompiler_43 (legacy shader compiler)",
                       ("d3dcompiler_43",)),
    "d3dcompiler_46": ("d3dcompiler_46 (legacy shader compiler)",
                       ("d3dcompiler_46",)),
    "d3dx9_43": ("d3dx9_43 (legacy DirectX 9 runtime)", ("d3dx9_43",)),
    "d3dx10_43": ("d3dx10_43 (legacy DirectX 10 runtime)", ("d3dx10_43",)),
    "d3dx11_42": ("d3dx11_42 (legacy DirectX 11 runtime)", ("d3dx11_42",)),
    "d3dx11_43": ("d3dx11_43 (legacy DirectX 11 runtime)", ("d3dx11_43",)),
    # Meta-verbs: the newest member is the one games actually bind to, so it
    # decides the verdict for the whole family.
    "d3dx9": ("d3dx9 (all legacy DirectX 9 runtimes)",
              ("d3dx9_43", "d3dx9_42", "d3dx9_36")),
    "d3dx10": ("d3dx10 (all legacy DirectX 10 runtimes)",
               ("d3dx10_43", "d3dx10_42")),
    # DirectShow / VB runtime verbs.
    "quartz": ("quartz (DirectShow runtime)", ("quartz",)),
    "dx8vb": ("dx8vb (DirectX 8 Visual Basic runtime)", ("dx8vb",)),
}


def _dx_redist_present(pfx: Path, dlls: "tuple[str, ...]") -> bool:
    """True when every required DLL is native in at least one bitness dir."""
    return all(
        any(dll_origin(pfx, f"{d}.dll", subdir=sub) is DllOrigin.NATIVE
            for sub in ("system32", "syswow64"))
        for d in dlls
    )


def _detect_dx_redist(dlls: "tuple[str, ...]") -> "Callable[[Path], bool | None]":
    def _detect(prefix_path: Path) -> "bool | None":
        pfx = wine_reg.normalize_pfx(Path(prefix_path))
        if not _prefix_usable(pfx):
            return None
        return _dx_redist_present(pfx, dlls)
    return _detect


def _check_dx_redist(verb: str) -> "Callable[[Path], HealthCheck]":
    label, dlls = _DX_REDIST_SPECS[verb]

    def _check(prefix_path: Path) -> HealthCheck:
        pfx = wine_reg.normalize_pfx(Path(prefix_path))
        native: list[str] = []
        stubbed: list[str] = []
        for d in dlls:
            origins = {sub: dll_origin(pfx, f"{d}.dll", subdir=sub)
                       for sub in ("system32", "syswow64")}
            if any(o is DllOrigin.NATIVE for o in origins.values()):
                native.append(d)
            elif any(o is not DllOrigin.ABSENT for o in origins.values()):
                stubbed.append(d)
        evidence = {"native": native, "stubbed": stubbed,
                    "required": list(dlls)}

        missing = [d for d in dlls if d not in native]
        if missing:
            detail = f"not installed - missing {', '.join(f'{d}.dll' for d in missing)}"
            if stubbed:
                detail += f" (stub/builtin present for {', '.join(stubbed)})"
            return HealthCheck(verb, HealthStatus.MISSING, label, detail,
                               verb, evidence)

        return HealthCheck(verb, HealthStatus.OK, label,
                           f"native DLL(s) present: {', '.join(native)}",
                           None, evidence)
    return _check


# --- DXVK -------------------------------------------------------------------
# Proton ALREADY ships DXVK and wires it up; the winetricks dxvk verb overwrites
# those DLLs with whatever build winetricks bundles, which is usually older than
# Proton's and is a known way to break a working prefix. So this is reported for
# information only - never MISSING, and deliberately no fix_token, so no Fix
# button and no participation in Fix All.
_DXVK_DLLS = ("d3d11", "d3d10core", "d3d9", "dxgi")


def detect_dxvk(prefix_path: Path) -> "bool | None":
    """True when the prefix's d3d11.dll is DXVK rather than Wine's builtin."""
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    if not _prefix_usable(pfx):
        return None
    return dll_origin(pfx, "d3d11.dll") is DllOrigin.NATIVE


def check_dxvk(prefix_path: Path) -> HealthCheck:
    """Report which DXVK DLLs are in place, without ever demanding a change."""
    label = "DXVK (Direct3D → Vulkan)"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    native = [d for d in _DXVK_DLLS
              if dll_origin(pfx, f"{d}.dll") is DllOrigin.NATIVE]
    evidence = {"native": native}

    if len(native) == len(_DXVK_DLLS):
        return HealthCheck("dxvk", HealthStatus.OK, label,
                           "active (Proton normally provides this)",
                           None, evidence)
    if native:
        return HealthCheck("dxvk", HealthStatus.OK, label,
                           f"partially present: {', '.join(native)} "
                           "- normal for some Proton builds", None, evidence)
    return HealthCheck("dxvk", HealthStatus.OK, label,
                       "using Wine's builtin D3D - Proton usually supplies DXVK "
                       "itself; installing it by hand can override a newer build",
                       None, evidence)


# --- LAV Filters ------------------------------------------------------------
def detect_lavfilters(prefix_path: Path) -> "bool | None":
    """True when the LAV Filters DirectShow codecs are installed."""
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    if not _prefix_usable(pfx):
        return None
    lav = pfx.joinpath(*_LAV_DIR) / "x86"
    return all((lav / ax).is_file() for ax in _LAV_AX)


def check_lavfilters(prefix_path: Path) -> HealthCheck:
    """Report LAV Filters, distinguishing "files there" from "COM-registered"."""
    label = "LAV Filters (DirectShow codecs)"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    lav_root = pfx.joinpath(*_LAV_DIR)
    lav = lav_root / "x86"
    present = [ax for ax in _LAV_AX if (lav / ax).is_file()]
    missing = [ax for ax in _LAV_AX if ax not in present]
    evidence = {"present": present, "missing": missing}

    if not lav_root.is_dir():
        return HealthCheck("lavfilters", HealthStatus.MISSING, label,
                           "not installed - games that stream radio/music "
                           "through DirectShow will play silent",
                           "lavfilters", evidence)
    if missing:
        return HealthCheck("lavfilters", HealthStatus.WARN, label,
                           "incomplete install - missing " + ", ".join(missing),
                           "lavfilters", evidence)

    # Files alone are not enough: without the COM registrations DirectShow
    # never finds the filters and the game is still silent.
    registered = any(wine_reg.hive_contains(pfx, ax) for ax in _LAV_AX)
    evidence["com_registered"] = registered
    if not registered:
        return HealthCheck("lavfilters", HealthStatus.WARN, label,
                           "files installed but not registered as DirectShow "
                           "filters - the game will not see the codecs",
                           "lavfilters", evidence)
    return HealthCheck("lavfilters", HealthStatus.OK, label,
                       "installed and registered as DirectShow filters",
                       None, evidence)


# --- .NET Windows Desktop Runtime ------------------------------------------
_DOTNET_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_DOTNET_DESKTOP_PROBES = (
    "WindowsBase.dll", "PresentationFramework.dll", "System.Windows.Forms.dll",
)


def _dotnet_desktop_versions(prefix_path: Path) -> list[str]:
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    dotnet_root = pfx / "drive_c" / "Program Files" / "dotnet"
    shared = dotnet_root / "shared" / "Microsoft.WindowsDesktop.App"
    if not (dotnet_root / "dotnet.exe").is_file() or not shared.is_dir():
        return []

    versions: list[str] = []
    try:
        children = list(shared.iterdir())
    except OSError:
        return []
    for child in children:
        if (child.is_dir() and _DOTNET_VERSION_RE.fullmatch(child.name)
                and any((child / dll).is_file() for dll in _DOTNET_DESKTOP_PROBES)):
            versions.append(child.name)
    return sorted(versions, key=lambda value: tuple(
        int(part) for part in _DOTNET_VERSION_RE.fullmatch(value).groups()[:3]))


def _detect_dotnet_desktop(version: str):
    def _detect(prefix_path: Path) -> "bool | None":
        pfx = wine_reg.normalize_pfx(Path(prefix_path))
        if not _prefix_usable(pfx):
            return None
        return any(item.split(".", 1)[0] == version
                   for item in _dotnet_desktop_versions(pfx))
    return _detect


def _check_dotnet_desktop(version: str):
    token = f"dotnet{version}"
    label = f".NET {version} Desktop Runtime"

    def _check(prefix_path: Path) -> HealthCheck:
        pfx = wine_reg.normalize_pfx(Path(prefix_path))
        versions = _dotnet_desktop_versions(pfx)
        matching = [item for item in versions if item.split(".", 1)[0] == version]
        evidence = {"required_major": version, "installed": versions}
        if matching:
            return HealthCheck(token, HealthStatus.OK, label,
                               "installed: " + ", ".join(matching),
                               None, evidence)
        detail = f"not installed - no {version}.x Windows Desktop runtime found"
        if versions:
            detail += " (found " + ", ".join(versions) + ")"
        return HealthCheck(token, HealthStatus.MISSING, label, detail,
                           token, evidence)
    return _check


# --- game path in the prefix registry ---------------------------------------
def _normalise_wine_path(value: str) -> str:
    """Fold a Wine path for comparison - case and trailing slash are moot."""
    return value.replace("/", "\\").rstrip("\\").lower()


def wine_path_to_posix(pfx: Path, value: str) -> "Path | None":
    """Resolve ``X:\\dir\\file`` to a host path via the prefix's dosdevices.

    Comparing the raw strings is not enough: ``Z:`` is only the root mapping,
    and a prefix may reach the same directory through another letter (Steam
    libraries on removable media get their own drive). None when the drive
    letter has no mapping - which is itself meaningful, since a registration
    naming a letter the prefix no longer has is genuinely stale.
    """
    if len(value) < 2 or value[1] != ":":
        return None
    link = Path(pfx) / "dosdevices" / f"{value[0].lower()}:"
    if not link.is_symlink() and not link.exists():
        return None
    rest = value[2:].replace("\\", "/").strip("/")
    try:
        target = link.resolve()
    except OSError:
        return None
    return target / rest if rest else target


# Drive letters Proton creates and destroys per launch, so a registration
# naming one is not stale just because the symlink is absent right now:
#   s: = "gamedrive"  -> the game's Steam library dir
#   t: = "steamdrive" -> the Steam install dir
# See setup_dir_drive() in Proton's own `proton` script - when the compat
# option is off it *removes* the symlink, which is why a perfectly good
# registration routinely points at an unmapped letter.
_PROTON_DYNAMIC_DRIVES = {"s", "t"}


def _tail_match(rest: str, expected_posix: Path) -> bool:
    """True when *expected_posix* ends with the ``X:\\`` remainder *rest*.

    Used only for Proton's dynamic drives, where the letter's target is a
    library/Steam root we cannot resolve while it is unmapped, but the tail
    below that root is unambiguous ("common/Fallout New Vegas").
    """
    tail = rest.replace("\\", "/").strip("/").lower()
    if not tail:
        return False
    return str(expected_posix).replace("\\", "/").rstrip("/").lower().endswith("/" + tail)


def _same_install(pfx: Path, registered: str, expected: str) -> bool:
    """True when two Wine paths name the same install.

    Compares the resolved host paths, not the strings: drive letters differ
    (a library on removable media gets its own), and the standard Steam layout
    symlinks ``~/.steam/steam`` to ``~/.local/share/Steam``, so the same
    directory is routinely spelled two ways.
    """
    if _normalise_wine_path(registered) == _normalise_wine_path(expected):
        return True
    a = wine_path_to_posix(pfx, registered)
    b = wine_path_to_posix(pfx, expected)
    if a is not None and b is not None:
        try:
            a, b = a.resolve(), b.resolve()
        except OSError:
            return False
        return str(a).rstrip("/").lower() == str(b).rstrip("/").lower()
    # Unmapped letter. If Proton owns it, judge by the tail instead - the
    # symlink is simply absent until the next launch turns the option on.
    if (a is None and len(registered) > 1 and registered[1] == ":"
            and registered[0].lower() in _PROTON_DYNAMIC_DRIVES):
        target = b if b is not None else Path(expected[2:].replace("\\", "/"))
        return _tail_match(registered[2:], target)
    return False


def check_game_registry(
    prefix_path: Path,
    registry_name: str,
    game_path: "Path | None" = None,
) -> HealthCheck:
    """Report whether the game's install path is registered for Bethesda tools.

    What matters is that the key exists and points at the right install; which
    of the two registry views holds it deliberately does NOT affect the status.
    Steam and ``Utils.bethesda.registry`` both write the Wow6432Node view (Wine's
    ``reg add`` collapses the 64-bit write into it), so 32-bit-only is the
    normal, healthy state - and since no write we can make will produce the
    64-bit view, flagging it would be an unfixable warning with a Fix button
    that provably cannot clear it. The tools cope: BodySlide / Outfit Studio
    ship 64-bit only now and work off the key the game itself writes.
    """
    label = "Game path in prefix registry"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    key64 = f"{_BETHESDA_KEY_64}\\{registry_name}"
    key32 = f"{_BETHESDA_KEY_32}\\{registry_name}"
    val64 = wine_reg.read_value(pfx, key64, _INSTALLED_PATH_VALUE)
    val32 = wine_reg.read_value(pfx, key32, _INSTALLED_PATH_VALUE)
    evidence = {"registry_name": registry_name, "x64": val64, "wow6432node": val32}

    found = [v for v in (val64, val32) if v]
    if not found:
        return HealthCheck("game_registry", HealthStatus.MISSING, label,
                           f"'{registry_name}' is not registered - Bethesda "
                           "tools will not find the game install",
                           "game_registry", evidence)

    # Stale registration: points somewhere other than the configured install.
    if game_path is not None:
        try:
            from Utils.bethesda.registry import _posix_to_wine_path
            expected = _posix_to_wine_path(Path(game_path))
        except Exception:
            expected = None
        if expected is not None:
            stale = [v for v in found if not _same_install(pfx, v, expected)]
            if stale:
                evidence["expected"] = expected
                return HealthCheck(
                    "game_registry", HealthStatus.WARN, label,
                    f"registered to a different path: {stale[0]} "
                    f"(expected {expected})",
                    "game_registry", evidence)

    views = ", ".join(v for v, present in
                      (("Wow6432Node", val32), ("64-bit", val64)) if present)
    return HealthCheck("game_registry", HealthStatus.OK, label,
                       f"registered ({found[0]}) in the {views} view"
                       + ("s" if val64 and val32 else ""),
                       None, evidence)


# --- Steam first-launch install script ---------------------------------------
_INSTALLSCRIPT_NAMES = ("installscript.vdf", "InstallScript.vdf")
_HASRUNKEY_RE = re.compile(r'"hasrunkey"\s+"([^"]+)"', re.IGNORECASE)
_RUNPROCESS_RE = re.compile(r'"run process"', re.IGNORECASE)
_PROCESS_RE = re.compile(r'"process \d+"\s+"([^"]+)"', re.IGNORECASE)


def parse_installscript(game_path: Path) -> "tuple[list[str], list[str]] | None":
    """Parse the game's Steam InstallScript.vdf, if it has first-run work.

    Returns ``(hasrunkeys, process_basenames)`` - the registry keys Steam
    stamps after running the script, and the redist installers it runs
    (e.g. vcredist_x86.exe, DXSETUP.exe). None when there is no script or it
    has no ``run process`` section (a registry-only script needs no first
    launch; everything it writes, the game-registry fix already covers).
    """
    text = None
    for name in _INSTALLSCRIPT_NAMES:
        p = Path(game_path) / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                break
        except OSError:
            return None
    if text is None or not _RUNPROCESS_RE.search(text):
        return None
    # VDF string literals escape backslashes ("HKEY_LOCAL_MACHINE\\Software\\…").
    keys = [k.replace("\\\\", "\\") for k in _HASRUNKEY_RE.findall(text)]
    procs = [proc.replace("\\\\", "\\").replace("\\", "/").rsplit("/", 1)[-1]
             for proc in _PROCESS_RE.findall(text)]
    if not keys:
        return None
    return keys, procs


def _steam_managed_prefix(game, prefix_path: Path) -> bool:
    """True when the prefix is Steam's own compatdata/<appid> for this game.

    Only there does Steam ever run the install script - a Heroic/Lutris/Faugus
    prefix never gets it, and "launch through Steam" is not actionable advice
    for those, so the first-launch row is gated on this. The handler declaring
    a steam_id is not enough: the user may own the game on Epic/GOG.
    """
    known = {str(s) for s in
             [getattr(game, "steam_id", None),
              *(getattr(game, "alt_steam_ids", None) or [])] if s}
    if not known:
        return False
    return any(part in known for part in Path(prefix_path).parts[::-1])


def check_steam_first_launch(
    prefix_path: Path, game_path: "Path | None",
) -> "HealthCheck | None":
    """Report whether Steam's first-launch install script has run in the prefix.

    Discovered by diffing three from-scratch FNV prefixes: launching through
    the script extender builds the prefix but skips Steam's install script, so
    ``vcredist_x86.exe`` (VC++ 2008, the game is 32-bit) and ``DXSETUP.exe``
    (DirectX June 2010: d3dx9_*, XACT, X3DAudio, XAudio2) never run and the
    game will not start - installing our own deps does not substitute. The
    script is gated on its ``hasrunkey``, which is therefore a reliable
    marker: absent in both broken prefixes, present in the working one.
    """
    if not game_path:
        return None
    parsed = parse_installscript(Path(game_path))
    if parsed is None:
        return None
    keys, procs = parsed
    label = "Steam first-launch setup"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))

    hklm = "hkey_local_machine\\"
    for key in keys:
        rel = key[len(hklm):] if key.lower().startswith(hklm) else key
        # 32-bit installers land in Wow6432Node; check both views.
        wow = re.sub(r"^(software)\\", r"\1\\Wow6432Node\\", rel,
                     flags=re.IGNORECASE)
        if wine_reg.key_exists(pfx, rel) or wine_reg.key_exists(pfx, wow):
            return HealthCheck("steam_first_launch", HealthStatus.OK, label,
                               "Steam's install script has run "
                               f"({', '.join(procs) or 'redists installed'})",
                               None, {"hasrunkey": rel})
    return HealthCheck(
        "steam_first_launch", HealthStatus.MISSING, label,
        "Steam's first-launch setup has never run in this prefix - it installs "
        + (", ".join(procs) or "the game's redistributables")
        + ". Launch the game unmodded through Steam once, then re-check",
        None, {"hasrunkeys": keys, "processes": procs})


def check_game_inis(game) -> "HealthCheck | None":
    """Report whether the game's My Games INI files exist in the prefix.

    Duck-typed on the handler's ``_get_archive_ini_paths`` (Bethesda family).
    The launcher creates these on first run; without them the game fails to
    start, so the advice matches the first-launch row.
    """
    getter = getattr(game, "_get_archive_ini_paths", None)
    if getter is None:
        return None
    try:
        paths = getter() or []
    except Exception:
        return None
    if not paths:
        return None
    label = "Game INI files"
    missing = [p.name for p in paths if not Path(p).is_file()]
    if missing:
        return HealthCheck(
            "game_inis", HealthStatus.MISSING, label,
            "missing from My Games: " + ", ".join(missing)
            + " - the launcher creates them; run the game unmodded once",
            None, {"missing": missing})
    return HealthCheck("game_inis", HealthStatus.OK, label,
                       ", ".join(p.name for p in paths) + " present",
                       None, {"paths": [str(p) for p in paths]})


# --- prefix sanity ----------------------------------------------------------
def check_prefix_exists(prefix_path: "Path | None") -> HealthCheck:
    """Report whether a prefix directory has been resolved and exists."""
    label = "Proton prefix"
    if not prefix_path:
        return HealthCheck("prefix_exists", HealthStatus.MISSING, label,
                           "no prefix is configured for this game - launch it "
                           "once, or set the prefix in Configure Game")
    pfx = Path(prefix_path)
    if not pfx.is_dir():
        return HealthCheck("prefix_exists", HealthStatus.MISSING, label,
                           f"configured prefix does not exist: {pfx}")
    return HealthCheck("prefix_exists", HealthStatus.OK, label, str(pfx))


def check_prefix_structure(prefix_path: Path) -> HealthCheck:
    """Report whether the prefix has been initialised (drive_c + hives)."""
    label = "Prefix structure"
    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    missing = [n for n in ("drive_c", wine_reg.HIVE_USER, wine_reg.HIVE_SYSTEM)
               if not (pfx / n).exists()]
    if missing:
        return HealthCheck("prefix_structure", HealthStatus.MISSING, label,
                           "prefix is not initialised - missing "
                           + ", ".join(missing)
                           + " (launch the game once to build it)",
                           None, {"missing": missing})
    return HealthCheck("prefix_structure", HealthStatus.OK, label,
                       "drive_c, user.reg and system.reg present")


def check_runner_bound(prefix_path: Path) -> HealthCheck:
    """Report which Proton/Wine runner the prefix records in config_info."""
    label = "Prefix runner"
    from Utils.wine.prefix import read_prefix_runner, resolve_compat_data
    compat_data = resolve_compat_data(Path(prefix_path))
    runner = read_prefix_runner(compat_data)
    if not runner:
        # Lutris/Faugus prefixes legitimately lack config_info until first run.
        return HealthCheck("runner_bound", HealthStatus.WARN, label,
                           "the prefix records no runner (config_info absent) "
                           "- it has probably never been launched",
                           None, {"compat_data": str(compat_data)})
    return HealthCheck("runner_bound", HealthStatus.OK, label, runner,
                       None, {"runner": runner, "compat_data": str(compat_data)})


def check_proton_binding(
    prefix_path: Path, proton_script: "Path | None",
) -> list[HealthCheck]:
    """Report the Proton build Amethyst would drive this prefix with.

    Emits a second WARN row when running it would hang (GH#333 downgrade).
    """
    label = "Proton build"
    if proton_script is None:
        return [HealthCheck("proton_bound", HealthStatus.WARN, label,
                            "no Proton/Wine runner could be resolved for this "
                            "game - installers and wizards cannot run")]
    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        # Classic lutris-wine: a bare wine binary, not a Proton tree.
        return [HealthCheck("proton_bound", HealthStatus.OK, label,
                            f"Wine runner: {script.parent.parent.name or script}",
                            None, {"wine": str(script)})]

    out = [HealthCheck("proton_bound", HealthStatus.OK, label,
                       script.parent.name, None, {"proton": str(script)})]
    try:
        from Utils.wine.prefix import resolve_compat_data
        from Utils.wine.protontricks import prefix_downgrade_warning
        warning = prefix_downgrade_warning(
            script, resolve_compat_data(Path(prefix_path)))
    except Exception:
        warning = None
    if warning:
        out.append(HealthCheck("proton_downgrade", HealthStatus.WARN,
                               "Proton / prefix version", warning))
    return out


# --- component registry -----------------------------------------------------
@dataclass(frozen=True)
class ComponentSpec:
    """One installable prefix component, keyed by its auto_install_deps token."""
    check_id: str
    label: str
    detect: Callable[[Path], "bool | None"]
    check: Callable[[Path], HealthCheck]
    fix_token: str


COMPONENT_SPECS: dict[str, ComponentSpec] = {
    "vcredist": ComponentSpec(
        "vcredist", "VC++ Redistributable (x64)",
        detect_vcredist, check_vcredist, "vcredist"),
    "d3dcompiler_47": ComponentSpec(
        "d3dcompiler_47", "d3dcompiler_47 (shader compiler)",
        detect_d3dcompiler_47, check_d3dcompiler_47, "d3dcompiler_47"),
    "lavfilters": ComponentSpec(
        "lavfilters", "LAV Filters (DirectShow codecs)",
        detect_lavfilters, check_lavfilters, "lavfilters"),
    **{
        f"dotnet{_version}": ComponentSpec(
            f"dotnet{_version}", f".NET {_version} Desktop Runtime",
            _detect_dotnet_desktop(_version), _check_dotnet_desktop(_version),
            f"dotnet{_version}")
        for _version in DOTNET_VERSIONS
    },
    **{
        _verb: ComponentSpec(_verb, _label,
                             _detect_dx_redist(_dlls), _check_dx_redist(_verb),
                             _verb)
        for _verb, (_label, _dlls) in _DX_REDIST_SPECS.items()
    },
    # Informational only - no fix_token, so no Fix button (see check_dxvk).
    "dxvk": ComponentSpec("dxvk", "DXVK (Direct3D → Vulkan)",
                          detect_dxvk, check_dxvk, ""),
}


def detect_component(token: str, prefix_path: Path) -> "bool | None":
    """Cheap real-detection for one ``auto_install_deps`` token.

    None means "cannot tell" (unknown token, or an unusable prefix).
    """
    spec = COMPONENT_SPECS.get(token)
    if spec is None:
        return None
    return spec.detect(Path(prefix_path))


def check_component(token: str, prefix_path: Path) -> "HealthCheck | None":
    """Full report for one token, or None when nothing can check it."""
    spec = COMPONENT_SPECS.get(token)
    if spec is None:
        return None
    return spec.check(Path(prefix_path))


# --- composition ------------------------------------------------------------
def _declared_tokens(game) -> list[str]:
    """Tokens from the handler that we have a detector for, in declared order.

    ``auto_install_deps`` comes first (installed automatically when the game is
    added), then ``prefix_health_extras`` - components a handler wants reported
    and offered as a Fix, but that are too slow or too situational to install
    for every user up front.
    """
    try:
        deps = list(getattr(game, "auto_install_deps", []) or [])
    except Exception:
        deps = []
    try:
        deps += list(getattr(game, "prefix_health_extras", []) or [])
    except Exception:
        pass
    seen: set[str] = set()
    out: list[str] = []
    for token in deps:
        if token in COMPONENT_SPECS and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def component_checks_for_game(game, prefix_path: Path) -> list[HealthCheck]:
    """Component + registry rows a handler declares, in display order.

    Driven purely by ``auto_install_deps`` and ``synthesis_registry_name`` - no
    game is named here, so a handler gains rows by declaring them.
    """
    rows = [check_component(t, prefix_path) for t in _declared_tokens(game)]
    rows = [r for r in rows if r is not None]

    registry_name = getattr(game, "synthesis_registry_name", None)
    if registry_name:
        game_path = None
        try:
            if hasattr(game, "get_game_path"):
                got = game.get_game_path()
                game_path = Path(got) if got else None
        except Exception:
            game_path = None
        rows.append(check_game_registry(prefix_path, registry_name, game_path))
    return rows


def _unknown_rows(game) -> list[HealthCheck]:
    """UNKNOWN placeholders used when the prefix cannot be interrogated."""
    rows = [
        HealthCheck(spec.check_id, HealthStatus.UNKNOWN, spec.label,
                    "cannot check - no usable prefix")
        for spec in (COMPONENT_SPECS[t] for t in _declared_tokens(game))
    ]
    if getattr(game, "synthesis_registry_name", None):
        rows.append(HealthCheck("game_registry", HealthStatus.UNKNOWN,
                                "Game path in prefix registry",
                                "cannot check - no usable prefix"))
    return rows


def run_prefix_health(
    game,
    *,
    proton_script: "Path | None" = None,
    check_proton: bool = False,
) -> list[HealthCheck]:
    """Full health report for *game*.

    Resolving Proton is slow and can hit the network, so the caller owns that
    step: pass ``check_proton=True`` with whatever ``resolve_proton_env``
    returned (``proton_script=None`` then reports the resolve failure). Left at
    the default this function is pure filesystem and spawns no subprocesses.
    """
    prefix_path = None
    try:
        if hasattr(game, "get_prefix_path"):
            prefix_path = game.get_prefix_path()
    except Exception:
        prefix_path = None

    exists = check_prefix_exists(prefix_path)
    if exists.status is not HealthStatus.OK:
        return [exists] + _unknown_rows(game)

    pfx = wine_reg.normalize_pfx(Path(prefix_path))
    rows = [exists]
    structure = check_prefix_structure(pfx)
    rows.append(structure)
    if structure.status is not HealthStatus.OK:
        return rows + _unknown_rows(game)

    rows.append(check_runner_bound(Path(prefix_path)))
    if check_proton:
        rows.extend(check_proton_binding(Path(prefix_path), proton_script))

    game_path = None
    try:
        if hasattr(game, "get_game_path"):
            got = game.get_game_path()
            game_path = Path(got) if got else None
    except Exception:
        game_path = None
    if _steam_managed_prefix(game, Path(prefix_path)):
        first_launch = check_steam_first_launch(pfx, game_path)
        if first_launch is not None:
            rows.append(first_launch)
    inis = check_game_inis(game)
    if inis is not None:
        rows.append(inis)

    rows.extend(component_checks_for_game(game, pfx))
    return rows
