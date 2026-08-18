"""On-demand install and command building for DSMSPortable.

Elden Ring keeps every param table (weapons, shops, enemy stats, map layout) in
one ``regulation.bin``.  Two mods that touch completely unrelated tables still
collide, because the whole file is a single VFS entry - me3 serves exactly one
of them and the rest are discarded.  Load order cannot fix that; the file has to
be *merged*.

DSMSPortable is the command-line half of DSMapStudio, and it can do both halves
of the job::

    -D diffparamfile   diff a modded regulation against vanilla -> massedit script
    -C / -M+           apply CSV rows / massedit scripts onto a base regulation

So a merge is: diff every mod against vanilla, then replay all of those edits
onto one base file in mod-list order.  Later ``-M`` scripts win, which is why
:func:`plan_merge` feeds them lowest-priority first - the same reversal the .me3
package order uses.

It is a Windows .NET 6 program (its runtimeconfig requires
``Microsoft.WindowsDesktop.App``), so it runs through the game's Proton prefix,
the same way the xEdit/BodySlide wizards run their tools.  Never bundled: fetched
into the manager's tools folder on demand, like umu-run and me3.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_LATEST_API = "https://api.github.com/repos/mountlover/DSMSPortable/releases/latest"

EXE_NAME = "DSMSPortable.exe"

# Elden Ring's game-type code for -G.
GAME_TYPE_ELDEN_RING = "ER"

# Oodle compressor, needed to write a compressed regulation. DSMSPortable's own
# install script copies it out of the game folder for exactly this reason.
OODLE_DLL = "oo2core_6_win64.dll"

REGULATION_NAME = "regulation.bin"

# FindGamepath() rejects an Elden Ring gamepath that has no EldenRing.exe in it
# (exit 3).  Spelled exactly as DSMSPortable spells it - it checks with a plain
# File.Exists, and the real game ships a lowercase "eldenring.exe", so on a
# case-sensitive filesystem only this casing satisfies the check.
GAME_EXE_NAME = "EldenRing.exe"

# AES-256-CBC key Elden Ring's regulation.bin is encrypted with (the IV is the
# file's own first 16 bytes).  Same constant SoulsFormats uses.
_ER_REGULATION_KEY = bytes.fromhex(
    "99BFFC366A6BC8C6F5827D093602D676C42892A01C207FB024D3AF4E493FEF99")

# Offset of the ZSTD compression-level byte inside the decrypted DCP header,
# and the only value SoulsFormats will accept there.
_ZSTD_LEVEL_OFFSET = 0x30
_ZSTD_LEVEL_EXPECTED = 0x15  # 21

_attempted = False
_attempt_lock = threading.Lock()


def _decrypt_regulation(blob: bytes) -> "tuple[bytes, bytes] | None":
    """Return (iv, plaintext) for an encrypted regulation, else None."""
    if len(blob) <= 16 or blob[:4] == b"BND4":
        return None
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
    except ImportError:
        return None
    iv, enc = blob[:16], blob[16:]
    enc = enc[:len(enc) // 16 * 16]
    try:
        dec = Cipher(algorithms.AES(_ER_REGULATION_KEY), modes.CBC(iv)).decryptor()
        return iv, dec.update(enc) + dec.finalize()
    except Exception:
        return None


def normalize_zstd_level(path: Path, log_fn: "LogFn | None" = None) -> bool:
    """Rewrite *path* so its ZSTD compression level reads as 21.  True if changed.

    Elden Ring switched regulation.bin to ZSTD in 1.12, and SoulsFormats'
    DecompressDCPZSTD asserts the level byte is exactly 0x15::

        br.AssertASCII("ZSTD");
        br.AssertInt32(0x20);
        br.AssertByte(0x15);        // throws on anything else

    Format detection keys only on the "ZSTD" tag, so a regulation packed at any
    other level is routed to that decompressor and then rejected.  Most mod
    tools repack at level 15, which is why practically every modded regulation
    trips it while vanilla does not.

    The failure is invisible: the throw happens inside ParamBank's loader task,
    which DSMSPortable queues without observing, so nothing is printed and the
    load simply never completes - reported as "Failed due to timeout" (exit 7).

    Only that one byte changes; the compressed payload is untouched and still
    decompresses identically (verified against the declared uncompressed size).
    """
    log = _log_fn(log_fn)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        log(f"could not read {path.name}: {exc}")
        return False
    got = _decrypt_regulation(blob)
    if got is None:
        return False
    iv, plain = got
    if len(plain) <= _ZSTD_LEVEL_OFFSET:
        return False
    # Only touch DCP-ZSTD files; DFLT/KRAK regulations have no such byte.
    if plain[0x28:0x2C] != b"ZSTD":
        return False
    level = plain[_ZSTD_LEVEL_OFFSET]
    if level == _ZSTD_LEVEL_EXPECTED:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        patched = bytearray(plain)
        patched[_ZSTD_LEVEL_OFFSET] = _ZSTD_LEVEL_EXPECTED
        pad = (-len(patched)) % 16
        enc = Cipher(algorithms.AES(_ER_REGULATION_KEY),
                     modes.CBC(iv)).encryptor()
        out = iv + enc.update(bytes(patched) + b"\x00" * pad) + enc.finalize()
        path.write_bytes(out)
    except Exception as exc:
        log(f"could not normalize {path.name}: {exc}")
        return False
    log(f"{path.name}: ZSTD level {level:#04x} -> {_ZSTD_LEVEL_EXPECTED:#04x} "
        "(DSMSPortable only accepts 21)")
    return True


def _noop(_msg: str) -> None:
    pass


def _log_fn(log_fn: "LogFn | None") -> LogFn:
    if log_fn is not None:
        return log_fn
    try:
        from Utils.app_log import app_log
        return lambda m: app_log(f"dsms: {m}")
    except Exception:
        return _noop


# Folder name under Profiles/<game>/Applications/, like the other wizard tools.
APP_DIR = "DSMSPortable"


def bundled_dir(game) -> Path:
    """Profiles/<game>/Applications/DSMSPortable/ - where wizard tools live.

    Same home as xEdit, BodySlide and friends, so an isolated Proton prefix can
    sit beside the exe the way every other wizard expects.
    """
    from Utils.xedit_tools import applications_dir
    return applications_dir(game, APP_DIR)


def find_dsms(game) -> "Path | None":
    """Return DSMSPortable.exe, or None when it is not installed."""
    exe = bundled_dir(game) / EXE_NAME
    return exe if exe.is_file() else None


def _fetch_latest(log: LogFn) -> "tuple[str, str] | None":
    """Return (tag, zip_url) for the newest release, or None."""
    from Utils.ca_bundle import get_ssl_context
    try:
        req = urllib.request.Request(
            _LATEST_API, headers={"User-Agent": "Amethyst-Mod-Manager"})
        with urllib.request.urlopen(req, timeout=30,
                                    context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"could not reach the DSMSPortable release feed: {exc}")
        return None
    tag = data.get("tag_name") or ""
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if name.lower().endswith(".zip"):
            url = asset.get("browser_download_url") or ""
            if tag and url:
                return tag, url
    log("no .zip asset in the latest DSMSPortable release.")
    return None


def install_dsms(game, log_fn: "LogFn | None" = None) -> bool:
    """Download and unpack DSMSPortable into the game's Applications folder."""
    log = _log_fn(log_fn)
    latest = _fetch_latest(log)
    if latest is None:
        return False
    tag, url = latest

    log(f"downloading DSMSPortable {tag} (~40 MB) ...")
    from Utils.ca_bundle import download_file
    dest = bundled_dir(game)
    tmp_zip = dest.parent / f"dsms-{tag}.zip"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        download_file(url, tmp_zip)
    except Exception as exc:
        log(f"download failed: {exc}")
        return False

    try:
        # Replace wholesale: a half-extracted previous attempt would leave
        # mismatched DLLs behind.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp_zip) as zf:
            for member in zf.namelist():
                # Refuse absolute/traversing entries before extracting.
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    log(f"refusing suspicious archive entry: {member}")
                    return False
            zf.extractall(dest)
    except (zipfile.BadZipFile, OSError) as exc:
        log(f"could not unpack DSMSPortable: {exc}")
        return False
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
        except OSError:
            pass

    if find_dsms(game) is None:
        log(f"{EXE_NAME} missing after extraction.")
        return False
    log(f"DSMSPortable {tag} installed to {dest}.")
    return True


def ensure_dsms(game, log_fn: "LogFn | None" = None) -> "Path | None":
    """Return a usable DSMSPortable, fetching one once per session."""
    global _attempted
    found = find_dsms(game)
    if found is not None:
        return found
    with _attempt_lock:
        if _attempted:
            return None
        _attempted = True
    if not install_dsms(game, log_fn):
        return None
    return find_dsms(game)


# ---------------------------------------------------------------------------
# Merge planning (pure logic - no Proton, no subprocess)
# ---------------------------------------------------------------------------

@dataclass
class RegulationSource:
    """One enabled mod contributing param edits."""

    name: str
    regulation: "Path | None" = None
    csvs: list[Path] = field(default_factory=list)

    @property
    def contributes(self) -> bool:
        return self.regulation is not None or bool(self.csvs)


def find_regulation_sources(mod_dirs: list[tuple[str, Path]]
                            ) -> list[RegulationSource]:
    """Collect the regulation.bin / param CSVs each staged mod ships.

    A mod's CSVs only count as "this mod states its own edits" when they sit in
    the SAME directory as the regulation they belong to.  Mods routinely ship
    CSVs that are nothing of the kind - reference exports for other editors
    (``Fields CSVs (for Smithbox)/``) or the param files of an optional variant
    the user did not pick (``Optional - Custom Icons/``).  Treating those as the
    edit set makes :func:`plan_merge` skip the mod's diff, so its real changes
    are dropped and someone else's reference rows are merged in their place.
    """
    out: list[RegulationSource] = []
    for name, mod_dir in mod_dirs:
        if not mod_dir.is_dir():
            continue
        reg = None
        try:
            # Shallowest regulation wins, so a top-level file beats one inside
            # an optional-variant subfolder.
            candidates = [p for p in mod_dir.rglob("*")
                          if p.is_file() and p.name.lower() == REGULATION_NAME]
            if candidates:
                reg = min(candidates,
                          key=lambda p: (len(p.relative_to(mod_dir).parts),
                                         str(p).lower()))
        except OSError:
            continue

        # Only CSVs alongside the regulation (or anywhere, when the mod ships
        # no regulation at all and CSVs are all it has).
        csv_root = reg.parent if reg is not None else mod_dir
        csvs: list[Path] = []
        try:
            if reg is not None:
                csvs = sorted(p for p in csv_root.iterdir()
                              if p.is_file() and p.suffix.lower() == ".csv")
            else:
                csvs = sorted(p for p in csv_root.rglob("*")
                              if p.is_file() and p.suffix.lower() == ".csv")
        except OSError:
            csvs = []

        src = RegulationSource(name, reg, csvs)
        if src.contributes:
            out.append(src)
    return out


def parse_param_versions(lines: "list[str]") -> "tuple[str, str]":
    """Return (game_version, mod_version) from a ``-V`` run's output.

    DSMSPortable prints these as ``Vanilla Param Version:`` and ``Specified
    Param Version:`` - the game's regulation and the file being read.  Either
    is "" when the line is absent.
    """
    game = mod = ""
    for ln in lines:
        for label, is_game in (("Vanilla Param Version", True),
                               ("Specified Param Version", False)):
            idx = ln.find(label)
            if idx < 0:
                continue
            # Split AFTER the label, never on the first ":" in the line - the
            # caller's lines carry a "[HH:MM:SS] ... :" log prefix, so a plain
            # split(":", 1) captures the timestamp's tail and every version
            # then compares unequal (a mod matching the game reported as a
            # mismatch).
            tail = ln[idx + len(label):].lstrip(": \t")
            value = "".join(c for c in tail.strip() if c.isdigit())
            if not value:
                continue
            if is_game:
                game = value
            else:
                mod = value
    return game, mod


def describe_param_version(version: str) -> str:
    """Render a param version as the game patch users know it.

    The field reads ``M mm p ssss``: major digit, two minor digits, one patch
    digit, then a build tail.  Anchored on a known pair - an Elden Ring 1.16.1
    install reports ``11611000`` - which is what rules out reading two digits
    as the patch (that would make it 1.16.11).  A zero patch is dropped the way
    FromSoftware label releases, so ``11601000`` is 1.16.

    The raw value is logged alongside this, so a report stays unambiguous even
    if a future patch breaks the pattern.
    """
    digits = "".join(c for c in version if c.isdigit())
    if len(digits) < 4:
        return version or "unknown"
    major, minor, patch = int(digits[0]), int(digits[1:3]), int(digits[3])
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"


def missing_param_tables(lines: "list[str]") -> "list[str]":
    """Param tables an export could not find, from a ``-X`` run's output.

    These are tables the game's regulation has but the mod's does not - a
    consequence of the mod being built for an older patch.  Any edits the mod
    made in them cannot be recovered.
    """
    marker = "Could not find param by name of "
    out: list[str] = []
    for ln in lines:
        if marker in ln:
            name = ln.split(marker, 1)[1].strip()
            if name and name not in out:
                out.append(name)
    return out


def win_path(path: Path, prefix: "Path | None" = None) -> str:
    r"""Return *path* as a Wine ``Z:\`` path.

    Every path handed to DSMSPortable MUST go through here.  Its argument
    parser treats anything starting with ``/`` as a switch::

        return (arg[0] == '\\' || arg[0] == '/' || arg[0] == '-');

    so a Linux absolute path is read as a flag and rejected with
    "ERROR: Invalid switch: /tmp/...", after which it prints its help and
    exits 5.
    """
    from Utils.wine_paths import to_wine_path
    return to_wine_path(path, prefix)


def diff_command(exe: Path, vanilla_dir: Path, modded: Path, *,
                 out_dir: Path, params: "list[str] | None" = None,
                 game_type: str = GAME_TYPE_ELDEN_RING,
                 prefix: "Path | None" = None) -> list[str]:
    """Argv exporting the rows *modded* changes, as CSV, in ONE param load.

    Deliberately NOT ``-D``.  A ``-D`` run loads params twice in a single
    process, and the second load deadlocks under Proton: ``PB:LoadParams``
    spawns ``PB:LoadParamMeta`` (186 XML files for Elden Ring) as a background
    task that nothing waits for, since the poll loop watches only
    ``IsLoadingParams``.  That meta task is reliably still live when the second
    load starts, and ``TaskManager.Run`` keys tasks by NAME with ``wait: true``,
    so the second run blocks on ``t.Wait()`` before it can clear the loading
    flag - giving the "Failed due to timeout" exit 7 every single time.

    ``-X param:modified`` avoids the problem entirely: the export runs after the
    one load ``main()`` already does, and ``modified`` filters on
    ``GetVanillaDiffRows`` - rows that differ from the gamepath's regulation.
    So pointing ``-P`` at a folder holding the *vanilla* file and passing the
    *mod* as the input paramfile yields exactly the mod's own edits.

    ``-V`` is free and prints both param versions with a mismatch warning.

    *params* names the param tables to export.  Each is passed as
    ``Name:modified`` so only changed rows are written; a bare ``-X`` would dump
    every row of all 194 tables, and replaying those onto the base would make
    each mod clobber the previous one wholesale instead of merging row by row.
    """
    w = lambda p: win_path(p, prefix)  # noqa: E731
    cmd = [
        str(exe), w(modded),
        "-G", game_type,
        "-P", w(vanilla_dir),
    ]
    cmd.append("-X")
    cmd += [f"{name}:modified" for name in (params or ())]
    cmd += ["-O", w(out_dir), "-V"]
    return cmd


def param_names(exe: Path) -> list[str]:
    """Param table names DSMSPortable knows, from its bundled Paramdex defs."""
    defs = exe.parent / "Assets" / "Paramdex" / GAME_TYPE_ELDEN_RING / "Defs"
    try:
        return sorted(p.stem for p in defs.glob("*.xml"))
    except OSError:
        return []


def merge_command(exe: Path, base: Path, *, game_path: Path, out_path: Path,
                  csvs: "list[Path] | None" = None,
                  masseditos: "list[Path] | None" = None,
                  game_type: str = GAME_TYPE_ELDEN_RING,
                  prefix: "Path | None" = None) -> list[str]:
    """Argv that applies CSV rows and massedit scripts onto *base*.

    Scripts are applied in the order given and later edits win, so the caller
    must pass them lowest-priority first.  ``-M+`` (rather than ``-M``) creates
    rows a mod adds that the base does not have yet.
    """
    w = lambda p: win_path(p, prefix)  # noqa: E731
    cmd = [str(exe), w(base), "-G", game_type, "-P", w(game_path)]
    if csvs:
        cmd.append("-C")
        cmd += [w(p) for p in csvs]
    if masseditos:
        cmd.append("-M+")
        cmd += [w(p) for p in masseditos]
    cmd += ["-O", w(out_path)]
    return cmd


@dataclass
class MergePlan:
    """What a merge will do, in the order it will happen."""

    sources: list[RegulationSource]
    # Mods needing a vanilla diff first (they ship only a regulation.bin).
    needs_diff: list[RegulationSource]
    csvs: list[Path]

    @property
    def is_useful(self) -> bool:
        """A merge only makes sense with more than one contributor."""
        return len(self.sources) > 1


def plan_merge(sources: list[RegulationSource]) -> MergePlan:
    """Order *sources* (modlist order in) for DSMSPortable (lowest priority first).

    Amethyst's list is highest-priority-first while DSMSPortable applies edits in
    the order given and lets later ones win, so the list is reversed - the same
    inversion the .me3 package order needs.
    """
    ordered = list(reversed(sources))
    csvs: list[Path] = []
    needs_diff: list[RegulationSource] = []
    for src in ordered:
        csvs.extend(src.csvs)
        # A mod shipping CSVs already states its edits; only a bare
        # regulation.bin has to be diffed against vanilla to recover them.
        if src.regulation is not None and not src.csvs:
            needs_diff.append(src)
    return MergePlan(ordered, needs_diff, csvs)
