"""
Toolkit-neutral executable launch logic for the play bar.

Ports the persistence + launch dispatch out of the Tk exe launcher
(gui/plugin_panel_exe_launcher.py, gui/dialogs.py, wizards/_proton_prefix.py)
so the Qt GUI can use it without importing tkinter. File formats and paths are
identical to the Tk app so settings are shared between both:

- <staging>.parent/Applications/custom_exes.json        - manual exe list
- ~/.config/AmethystModManager/games/<game>/exe_launch_mode.json
      exe_name → "auto"|"steam"|"heroic"|"none"
      "__deploy_before_launch" → bool (default True)
      "__proton_override_<exe>" → Proton dir name ('' = game default)
      "__launch_options_<exe>" → Steam-style launch options string
      "__hidden_auto_exes" → [exe names] hidden auto-detected framework exes
- exe_args.json (global, or per-profile for profile-specific-mods profiles)
      exe_name → argument string

Launch entry points (launch_game / launch_exe_via_proton) are synchronous and
must be called from a worker thread; they only report through log_fn.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import re
from pathlib import Path

from Utils.config_paths import (
    get_exe_args_path,
    get_game_config_dir,
    get_game_config_path,
    get_profile_exe_args_path,
)
from Utils import launch_report
from Utils.protontricks import strip_appimage_env
from Utils.sleep_inhibit import inhibit_sleep
from Utils.xdg import spawn_watched

_LAUNCH_MODE_FILE = "exe_launch_mode.json"
_CUSTOM_EXES_FILE = "custom_exes.json"

EXE_PICKER_FILTERS = [
    ("Executables (*.exe, *.bat, *.jar, *.sh)",
     ["*.exe", "*.bat", "*.jar", "*.sh"]),
    ("All files", ["*"]),
]

# Java-runtime modes for .jar entries, persisted per-exe in exe_launch_mode.json.
JAR_RUNTIME_HOST = "host"      # run with the host's `java` (no Proton) - default
JAR_RUNTIME_PROTON = "proton"  # run a Windows Java inside the game's Proton prefix


def is_jar(path) -> bool:
    return str(path).lower().endswith(".jar")


def is_shell_script(path) -> bool:
    """True for a .sh entry - a native Linux launcher, never a Wine target."""
    return str(path).lower().endswith(".sh")


def _noop_log(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Custom exe registry - per profile, stored in profile_state.json
#
# The list of manually-added / staging-scanned exes is scoped to the active
# profile (via the ``custom_exes`` key in profile_state.json) so a tool added
# under one profile doesn't leak into others. Existing users' entries in the
# legacy shared Applications/custom_exes.json are migrated on first load.
# ---------------------------------------------------------------------------

def _active_profile_dir(game) -> Path | None:
    return getattr(game, "_active_profile_dir", None) if game is not None else None


def _legacy_custom_exes_path(game) -> Path | None:
    """Old shared location: <staging>.parent/Applications/custom_exes.json."""
    if game is None or not hasattr(game, "get_mod_staging_path"):
        return None
    return game.get_mod_staging_path().parent / "Applications" / _CUSTOM_EXES_FILE


def _read_legacy_exes(game) -> list[Path]:
    p = _legacy_custom_exes_path(game)
    if p is None or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [Path(s) for s in data if Path(s).is_file()]
    except (OSError, ValueError):
        return []


def load_custom_exes(game) -> list[Path]:
    """Return this profile's saved custom exe Paths (entries that still exist).

    Reads the ``custom_exes`` key from the active profile's profile_state.json.
    When that key is absent, migrates any entries from the legacy shared
    Applications/custom_exes.json so upgrading users keep their exes on the
    profile that's active at upgrade time.
    """
    pdir = _active_profile_dir(game)
    if pdir is None:
        # No active profile (edge case): read the legacy shared list read-only.
        return _read_legacy_exes(game)
    from Utils.profile_state import read_custom_exes, read_profile_state
    raw = read_custom_exes(pdir)
    if not raw and "custom_exes" not in read_profile_state(pdir):
        migrated = _read_legacy_exes(game)
        if migrated:
            save_custom_exes(game, migrated)
            return migrated
    return [Path(s) for s in raw if Path(s).is_file()]


def save_custom_exes(game, paths: list[Path]) -> None:
    pdir = _active_profile_dir(game)
    if pdir is None:
        return
    from Utils.profile_state import write_custom_exes
    write_custom_exes(pdir, [str(x) for x in paths])


def add_custom_exe(game, path: Path) -> None:
    existing = load_custom_exes(game)
    if path not in existing:
        existing.append(path)
        save_custom_exes(game, existing)


def remove_custom_exe(game, path: Path) -> None:
    existing = load_custom_exes(game)
    remaining = [p for p in existing if p != path]
    if len(remaining) != len(existing):
        save_custom_exes(game, remaining)


# Launchable file types picked up by the staging scan. ``.sh`` is here for
# Linux-native games, whose loaders (BepInEx) ship as shell scripts.
STAGING_EXE_SUFFIXES = (".exe", ".bat", ".jar", ".sh")

# Directory names that mark a wine/Proton prefix. The Applications/ folder holds
# some tools alongside their own prefixes, which are full of Windows system exes
# (apphost.exe, arp.exe, …) - skip any path under one so the picker only shows
# the tools themselves, not their prefix internals.
_PREFIX_DIR_NAMES = frozenset({"pfx", "drive_c"})


def _walk_launchables(root: Path, seen: set, found: list,
                      skip_key: "str | None" = None,
                      expand_links: bool = False) -> None:
    """os.walk *root* for launchable files, pruning prefix dirs before descent.

    *expand_links* - STAGING roots only: a Profile Group's mods/ is a farm of
    per-mod symlinks os.walk won't descend into, so expand them to their real
    targets (identity for a normal staging folder). Kept off for the game
    folder - a symlinked game subdir shouldn't widen that scan."""
    bases = [Path(root)]
    if expand_links:
        try:
            from Utils.profile_groups import staging_walk_roots
            bases = staging_walk_roots(Path(root))
        except Exception:
            pass
    for base in bases:
        _walk_one_launchable_root(base, seen, found, skip_key)


def _walk_one_launchable_root(root: Path, seen: set, found: list,
                              skip_key: "str | None" = None) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune wine/Proton prefix trees in place so the walk never descends
        # into them (rglob visited every Windows system exe first and only
        # filtered them out afterwards).
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _PREFIX_DIR_NAMES]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in STAGING_EXE_SUFFIXES:
                continue
            full = os.path.join(dirpath, fname)
            if not os.path.isfile(full):  # broken symlink / special file
                continue
            if skip_key is not None and full.lower() == skip_key:
                continue
            # Dedupe key: plain paths are already unique within a walk and
            # across deduped roots - only a symlink can alias another entry,
            # so resolve just those instead of stat-resolving every file.
            key = os.path.realpath(full) if os.path.islink(full) else full
            if key in seen:
                continue
            seen.add(key)
            found.append(Path(full))


def scan_staging_exes(game) -> list[Path]:
    """Return launchable files found under the game's staging area.

    Scans both the profile ``mods/`` folder (installed mod tools) and the
    sibling ``Applications/`` folder (wizard tools like xEdit / BodySlide /
    Script Merger) recursively for ``.exe`` / ``.bat`` / ``.jar`` / ``.sh``
    files, while pruning wine/Proton prefix trees (``pfx`` / ``drive_c``) that
    would otherwise flood the list with Windows system exes. Results are
    de-duplicated by resolved path and sorted by name for a stable, searchable
    picker list. Returns ``[]`` when the game has no staging path or the
    folders can't be read.
    """
    if game is None or not hasattr(game, "get_mod_staging_path"):
        return []
    # Honour profile-specific mods: the active profile may keep its mods under
    # <profile>/mods/ instead of the shared mods/ folder. Applications/ always
    # lives next to profiles/ (shared), so anchor it off the shared staging path.
    shared = game.get_mod_staging_path()
    if hasattr(game, "get_effective_mod_staging_path"):
        active_mods = game.get_effective_mod_staging_path()
    else:
        active_mods = shared
    # Dedupe roots by resolved path: without profile-specific mods the
    # effective staging path IS the shared mods/ folder, and walking the same
    # tree twice doubles the scan cost for nothing.
    roots: dict[Path, Path] = {}
    for root in (active_mods, shared, shared.parent / "Applications"):
        try:
            rkey = root.resolve()
        except OSError:
            rkey = root
        roots.setdefault(rkey, root)
    seen: set[str] = set()
    found: list[Path] = []
    for root in roots.values():
        try:
            if not root.is_dir():
                continue
            _walk_launchables(root, seen, found, expand_links=True)
        except OSError:
            continue
    found.sort(key=lambda p: (p.name.lower(), str(p).lower()))
    return found


def scan_game_folder_exes(game) -> list[Path]:
    """Return launchable files found under the game's install folder.

    Recursively scans the game root for ``.exe`` / ``.bat`` / ``.jar`` /
    ``.sh`` files - including files deployed there by mods - so tools that must
    run from the game folder (patchers, injectors, bundled utilities, a
    Linux-native game's ``run_bepinex.sh``) can be added to the play-bar
    dropdown. Prunes wine/Proton prefix trees (isolated tool prefixes
    live in ``prefix_<Proton>/`` next to their exe) and skips the game's own
    resolved launch exe, which already is the Play entry. Same dedup/sort as
    scan_staging_exes. Returns ``[]`` when the game has no configured path.
    """
    if game is None or not hasattr(game, "get_game_path"):
        return []
    root = game.get_game_path()
    if root is None:
        return []
    root = Path(root)
    game_exe = resolve_game_exe(game)
    game_exe_key = str(game_exe).lower() if game_exe is not None else None
    seen: set[str] = set()
    found: list[Path] = []
    try:
        if not root.is_dir():
            return []
        _walk_launchables(root, seen, found, skip_key=game_exe_key)
    except OSError:
        pass
    found.sort(key=lambda p: (p.name.lower(), str(p).lower()))
    return found


def detect_framework_exes(game, framework_states: "dict | None" = None) -> list[Path]:
    """Framework launcher exes (script extenders) for the play-bar dropdown.

    Reads the game class's ``framework_launch_exes`` declaration and returns
    the entries actually present in the game root (case-insensitive walk) so
    the dropdown can list them without a manual "Add custom EXE". They launch
    through launch_exe_via_proton like any custom exe: game prefix + Steam
    app-id env for Steam installs, the Lutris/Heroic runner fallbacks
    otherwise, cwd = the exe's folder (the game root for root-level loaders).

    *framework_states* is an optional {label: STATE_*} map - the framework
    banner's detect_frameworks result. An entry whose state is
    STATE_NOT_DEPLOYED (staged in the modlist but not deployed yet) is
    included as its FUTURE game-root path even though the file isn't on disk:
    the Run button deploys first, which materialises it. Without the map only
    on-disk exes are returned.

    Skips entries the user hid from the dropdown (hide_auto_exe) and the
    game's own resolved launch exe - a present preferred_launch_exe (OBSE64)
    already IS the Play entry, so listing it again would duplicate it.

    Only Steam installs get these auto entries: the game path (profile-aware
    - per-profile pinned paths are already loaded into the game) must sit in
    a Steam library. Non-Steam installs (Lutris/Heroic/GOG) can still run a
    script extender by adding it as a custom exe.
    """
    if game is None:
        return []
    try:
        declared = getattr(game, "framework_launch_exes", None) or {}
    except Exception:
        declared = {}
    if not declared:
        return []
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return []
    if not game_is_steam_install(game):
        return []
    from Utils.framework_detect import STATE_NOT_DEPLOYED, resolve_file_ci
    hidden = load_hidden_auto_exes(game)
    game_exe = resolve_game_exe(game)
    out: list[Path] = []
    for label, rel in declared.items():
        exe = resolve_file_ci(Path(game_path), Path(rel))
        if exe is None:
            # Not in the game root - list it anyway when it's staged and a
            # deploy would put it there (Run deploys before launching).
            state = (framework_states or {}).get(label)
            if state != STATE_NOT_DEPLOYED:
                continue
            exe = Path(game_path) / rel
        if exe.name in hidden:
            continue
        if game_exe is not None and str(exe).lower() == str(game_exe).lower():
            continue
        if exe not in out:
            out.append(exe)
    return out


# ---------------------------------------------------------------------------
# exe_launch_mode.json - per-game launch settings
# ---------------------------------------------------------------------------

def _launch_mode_path(game) -> Path | None:
    if game is None:
        return None
    return get_game_config_dir(game.name) / _LAUNCH_MODE_FILE


def _read_launch_mode_data(game) -> dict:
    p = _launch_mode_path(game)
    if p is None or not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_launch_mode_key(game, key: str, value) -> None:
    """Set (or, when value is falsy-and-poppable, remove) one key."""
    p = _launch_mode_path(game)
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read_launch_mode_data(game)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_launch_mode(game, exe_name: str) -> str:
    """Saved launch mode for exe_name: 'auto' | 'steam' | 'heroic' |
    'lutris' | 'faugus' | 'none'."""
    return _read_launch_mode_data(game).get(exe_name, "auto")


def save_launch_mode(game, exe_name: str, mode: str) -> None:
    _write_launch_mode_key(game, exe_name, mode)


def load_hidden_auto_exes(game) -> set[str]:
    """Exe basenames the user hid from the auto-detected dropdown entries."""
    val = _read_launch_mode_data(game).get("__hidden_auto_exes", [])
    return {str(n) for n in val} if isinstance(val, list) else set()


def hide_auto_exe(game, exe_name: str) -> None:
    """Hide an auto-detected framework exe from the play-bar dropdown."""
    hidden = load_hidden_auto_exes(game)
    hidden.add(exe_name)
    _write_launch_mode_key(game, "__hidden_auto_exes", sorted(hidden))


def load_deploy_before_launch(game) -> bool:
    return bool(_read_launch_mode_data(game).get("__deploy_before_launch", True))


def save_deploy_before_launch(game, enabled: bool) -> None:
    _write_launch_mode_key(game, "__deploy_before_launch", bool(enabled))


def load_proton_override(game, exe_name: str) -> str | None:
    """Saved Proton override name, '' for game default, None if never saved."""
    data = _read_launch_mode_data(game)
    return data.get(f"__proton_override_{exe_name}")


def save_proton_override(game, exe_name: str, proton_name: str) -> None:
    _write_launch_mode_key(game, f"__proton_override_{exe_name}",
                           proton_name if proton_name else None)


def load_launch_options(game, exe_name: str) -> str:
    return _read_launch_mode_data(game).get(f"__launch_options_{exe_name}", "")


def save_launch_options(game, exe_name: str, options: str) -> None:
    _write_launch_mode_key(game, f"__launch_options_{exe_name}",
                           options if options else None)


def load_deploy_on_run(game, exe_name: str) -> bool:
    """Whether to deploy the modlist before running this exe (default False)."""
    return bool(_read_launch_mode_data(game).get(f"__deploy_on_run_{exe_name}",
                                                 False))


def save_deploy_on_run(game, exe_name: str, enabled: bool) -> None:
    _write_launch_mode_key(game, f"__deploy_on_run_{exe_name}",
                           True if enabled else None)


def load_jar_runtime(game, exe_name: str) -> str:
    """Saved Java runtime for a .jar entry: 'host' (default) or 'proton'."""
    val = _read_launch_mode_data(game).get(f"__jar_runtime_{exe_name}")
    return val if val == JAR_RUNTIME_PROTON else JAR_RUNTIME_HOST


def save_jar_runtime(game, exe_name: str, runtime: str) -> None:
    _write_launch_mode_key(
        game, f"__jar_runtime_{exe_name}",
        runtime if runtime == JAR_RUNTIME_PROTON else None)


# ---------------------------------------------------------------------------
# exe_args.json - per-exe launch arguments
# ---------------------------------------------------------------------------

def exe_args_file(game) -> Path:
    """The exe_args.json to use for *game*'s active profile.

    Profiles with the profile_specific_mods flag store args inside the profile
    dir so each profile can have independent tool output paths; everything
    else shares the global file.
    """
    try:
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None:
            from Utils.profile_state import profile_uses_specific_mods
            if profile_uses_specific_mods(Path(active_dir)):
                return get_profile_exe_args_path(Path(active_dir))
    except Exception:
        pass
    return get_exe_args_path()


def load_exe_args(game, exe_name: str) -> str:
    """Saved args for an exe, profile-local file first, then the global file."""
    try:
        profile_file = exe_args_file(game)
        if profile_file.is_file():
            data = json.loads(profile_file.read_text(encoding="utf-8"))
            if exe_name in data:
                return data[exe_name]
    except (OSError, ValueError):
        pass
    try:
        data = json.loads(get_exe_args_path().read_text(encoding="utf-8"))
        return data.get(exe_name, "")
    except (OSError, ValueError):
        return ""


def save_exe_args(game, exe_name: str, args_str: str) -> None:
    p = exe_args_file(game)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[exe_name] = args_str
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Launch options parser (Steam-style)
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def split_preserving_backslash(s: str) -> list:
    """shlex.split but without treating ``\\`` as an escape character.

    Steam-style options for a Windows target contain paths like
    ``C:\\java8\\bin\\java.exe``; the default POSIX shlex would strip the
    backslashes (turning it into ``C:java8binjava.exe``). We keep whitespace
    splitting and quotes but disable escapes so Windows paths survive.
    """
    lex = shlex.shlex(s, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # don't treat backslash as an escape char
    try:
        return list(lex)
    except ValueError:
        return s.split()


def parse_launch_options(opts: str, command: list,
                         split_fn=shlex.split) -> tuple[dict, list]:
    """Parse Steam-style launch options into (env_vars, final_command).

    Tokens matching KEY=VALUE are extracted as environment variables.
    If ``%command%`` is present it is replaced by the actual *command* list
    (wrappers before it are prepended; tokens after it are appended).
    If ``%command%`` is absent the remaining tokens are appended as a suffix.

    *split_fn* tokenises each side; pass ``split_preserving_backslash`` when the
    options carry Windows paths (jar launches) so backslashes aren't eaten.
    """
    opts = (opts or "").strip()
    if not opts:
        return {}, list(command)

    env_vars: dict = {}

    if "%command%" in opts:
        idx = opts.index("%command%")
        prefix_str = opts[:idx]
        suffix_str = opts[idx + len("%command%"):]

        try:
            prefix_tokens = split_fn(prefix_str)
        except ValueError:
            prefix_tokens = prefix_str.split()
        try:
            suffix_tokens = split_fn(suffix_str)
        except ValueError:
            suffix_tokens = suffix_str.split()

        wrappers: list = []
        for token in prefix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                wrappers.append(token)

        suffix: list = []
        for token in suffix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, wrappers + list(command) + suffix
    else:
        try:
            tokens = split_fn(opts)
        except ValueError:
            tokens = opts.split()

        suffix = []
        for token in tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, list(command) + suffix


# ---------------------------------------------------------------------------
# Game exe resolution + install detection
# ---------------------------------------------------------------------------

def resolve_game_exe(game) -> Path | None:
    """Resolve the game's launch exe on disk.

    exe_name / exe_name_alts against game_path, recursive fallback for bare
    names (UE5 games keep the exe in Binaries/Win64/); a present
    preferred_launch_exe (e.g. a script extender) wins.
    """
    if game is None:
        return None
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return None
    exe_name = getattr(game, "exe_name", None)
    exe_name_alts = list(getattr(game, "exe_name_alts", []) or [])
    candidates_rel = [n for n in [exe_name, *exe_name_alts] if n]

    found_exe: Path | None = None
    for rel in candidates_rel:
        candidate = game_path / rel
        if candidate.is_file():
            found_exe = candidate
            break
    if found_exe is None:
        try:
            for rel in candidates_rel:
                bare = Path(rel).name
                for hit in game_path.rglob(bare):
                    if hit.is_file():
                        found_exe = hit
                        break
                if found_exe is not None:
                    break
        except OSError:
            pass

    preferred_rel = getattr(game, "preferred_launch_exe", "")
    if preferred_rel:
        preferred = game_path / preferred_rel
        if preferred.is_file():
            return preferred
    return found_exe


def game_exe_key(game) -> str:
    """The exe filename used to key the game's launch settings.

    Matches Tk, which keys exe_launch_mode.json by the resolved dropdown
    entry's filename (the preferred launch exe when present). Falls back to
    the configured exe_name when nothing resolves on disk.
    """
    resolved = resolve_game_exe(game)
    if resolved is not None:
        return resolved.name
    preferred_rel = getattr(game, "preferred_launch_exe", "")
    if preferred_rel:
        return Path(preferred_rel).name
    exe_name = getattr(game, "exe_name", None)
    return Path(exe_name).name if exe_name else ""


def effective_steam_id(game) -> str:
    from Utils.steam_finder import game_steam_id
    return game_steam_id(game)


def set_game_steam_context(env: dict, steam_id: str) -> None:
    """Pin a child process to the Steam app it is about to launch.

    Amethyst itself may have been started by Steam/Gaming Mode, in which case
    its environment carries the non-Steam shortcut's IDs.  ``setdefault``
    would preserve that unrelated context and can make SteamAPI/DLC checks in
    the child resolve against the manager rather than the game.
    """
    app_id = str(steam_id)
    env["SteamAppId"] = app_id
    env["SteamGameId"] = app_id
    env["SteamOverlayGameId"] = app_id
    env["STEAM_COMPAT_APP_ID"] = app_id


def game_is_steam_install(game) -> bool:
    """True if the game folder lives inside a Steam library (steamapps/common)."""
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return False
    from Utils.steam_finder import find_steam_libraries
    try:
        resolved = game_path.resolve()
        for lib in find_steam_libraries():
            if resolved.is_relative_to(lib.resolve()):
                return True
    except Exception:
        pass
    return False


def _saved_launcher_id(game, key: str) -> str:
    """Launcher id saved for the game's ACTIVE profile, or "".

    Goes through the handler, which overlays the active profile's pinned
    override on paths.json - a profile pointed at a different install of the
    same game must launch its own, not the default profile's. Falls back to a
    plain paths.json read for objects that predate the accessor.
    """
    getter = getattr(game, "get_saved_launcher_id", None)
    if callable(getter):
        try:
            return (getter(key) or "").strip()
        except Exception:
            return ""
    if not hasattr(game, "name"):
        return ""
    try:
        paths_file = get_game_config_path(game.name)
        if paths_file.is_file():
            data = json.loads(paths_file.read_text(encoding="utf-8"))
            return str((data or {}).get(key, "") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def heroic_app_names_for_launch(game) -> list:
    """Heroic app names for launch - handler-declared names and the value
    saved in paths.json at configure time are authoritative; the exe scan
    runs only when neither is set, since generic launcher names collide
    across games (e.g. FalloutLauncher.exe ships with both Fallout 3 GOTY
    and classic Fallout on GOG)."""
    names = [n for n in (getattr(game, "heroic_app_names", []) or []) if n]
    saved = _saved_launcher_id(game, "heroic_app_name")
    if saved and saved not in names:
        # Saved first: it records what configure actually resolved to, so it
        # beats the handler's declared list.
        names.insert(0, saved)
    if names:
        return names

    from Utils.heroic_finder import find_heroic_app_name_by_exe
    exe_names = [getattr(game, "exe_name", None)]
    exe_names += list(getattr(game, "exe_name_alts", []) or [])
    for exe in [e for e in exe_names if e]:
        try:
            found = find_heroic_app_name_by_exe(exe)
        except Exception:
            found = None
        if found and found not in names:
            names.append(found)
    return names


def game_is_heroic_install(game) -> bool:
    app_names = heroic_app_names_for_launch(game)
    if not app_names:
        return False
    from Utils.heroic_finder import find_heroic_launch_info
    try:
        return find_heroic_launch_info(app_names) is not None
    except Exception:
        return False


def lutris_slugs_for_launch(game) -> list:
    """Lutris slugs for launch - detected by matching the game's exe against
    Lutris's installed games, plus the saved paths.json value (written when
    the game was configured via Lutris detection)."""
    from Utils.lutris_finder import find_lutris_slugs_by_exes
    exe_names = [getattr(game, "exe_name", None)]
    exe_names += list(getattr(game, "exe_name_alts", []) or [])
    try:
        # One pass over Lutris's DB + YAML for all names - per-name lookups
        # re-scanned everything once per alt on every Play click.
        slugs = find_lutris_slugs_by_exes([e for e in exe_names if e])
    except Exception:
        slugs = []

    if not slugs:
        saved = _saved_launcher_id(game, "lutris_slug")
        if saved:
            slugs = [saved]
    return slugs


def game_is_lutris_install(game) -> bool:
    slugs = lutris_slugs_for_launch(game)
    if not slugs:
        return False
    from Utils.lutris_finder import find_lutris_launch_info
    try:
        return find_lutris_launch_info(slugs) is not None
    except Exception:
        return False


def faugus_gameids_for_launch(game) -> list:
    """Faugus gameids for launch - detected by matching the game's exe
    against Faugus's games.json, plus the saved paths.json value (written
    when the game was configured via Faugus detection)."""
    from Utils.faugus_finder import find_faugus_gameids_by_exes
    exe_names = [getattr(game, "exe_name", None)]
    exe_names += list(getattr(game, "exe_name_alts", []) or [])
    try:
        gameids = find_faugus_gameids_by_exes([e for e in exe_names if e])
    except Exception:
        gameids = []

    if not gameids:
        saved = _saved_launcher_id(game, "faugus_gameid")
        if saved:
            gameids = [saved]
    return gameids


def game_is_faugus_install(game) -> bool:
    gameids = faugus_gameids_for_launch(game)
    if not gameids:
        return False
    from Utils.faugus_finder import find_faugus_launch_info
    try:
        return find_faugus_launch_info(gameids) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Steam / Heroic / Lutris / Faugus launch
# ---------------------------------------------------------------------------

def spawn_process_watched(cmd: list, *, env: "dict | None" = None,
                          cwd=None, label: str, log_fn=_noop_log) -> None:
    """Popen *cmd* detached and watch its exit so failures aren't silent.

    The old ``Popen(..., stdout=DEVNULL, stderr=DEVNULL)`` pattern made a
    Proton/game process that died instantly (bad prefix, missing runtime,
    umu error) indistinguishable from a successful launch - the log ended at
    "launching …" and the Play button looked like it did nothing.

    stderr goes to a temp *file* (not a pipe: a pipe would EPIPE the game if
    our app exits first, and a full pipe buffer could block it); a daemon
    thread waits for the exit and logs the code plus a stderr tail. The exit
    line also fires on a normal quit hours later - that's fine, it confirms
    the lifecycle in the session log.

    Must be a NAMED temp file: ``tempfile.TemporaryFile`` is an anonymous
    O_TMPFILE fd on Linux, and 32-bit wine cold-boot segfaults (exit 245,
    NULL read in host libc) when a std handle is a nameless fd - the cause
    of the intermittent skse64_loader 245 crashes (fresh wineserver = crash,
    warm = survives). Verified against GE-Proton10-33.
    """
    import tempfile
    import threading
    import time

    errfile = None
    try:
        errfile = tempfile.NamedTemporaryFile(prefix="amm-launch-stderr-")
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=errfile if errfile is not None else subprocess.DEVNULL,
        )
    except Exception as e:
        if errfile is not None:
            errfile.close()
        log_fn(f"{label} error: {e}")
        launch_report.mark_failed(f"{label} could not be started - {e}")
        return
    log_fn(f"{label}: started (pid {proc.pid})")
    # Captured here: _watch runs on its own thread, where the thread-local
    # binding no longer applies.
    rep = launch_report.current()
    started = time.monotonic()
    if rep is not None:
        rep.mark_spawned()

    def _watch() -> None:
        rc = proc.wait()
        # Read stderr BEFORE reporting the exit: a launcher that refuses to
        # start (me3 with no matching Proton, say) explains itself there, and
        # that explanation is what the failure popup should show instead of a
        # bare exit code.
        tail = ""
        if errfile is not None:
            try:
                errfile.seek(0, os.SEEK_END)
                size = errfile.tell()
                errfile.seek(max(0, size - 4096))
                tail = errfile.read().decode(errors="replace").strip()
            except Exception:
                tail = ""
            finally:
                try:
                    errfile.close()
                except Exception:
                    pass
        launch_report.mark_exit(rep, started, rc, label, stderr_tail=tail)
        if rc == 0:
            log_fn(f"{label}: exited cleanly (rc=0)")
            return
        log_fn(f"{label}: exited with code {rc}")
        for line in tail.splitlines()[-8:]:
            if line.strip():
                log_fn(f"{label}:   {line}")

    threading.Thread(target=_watch, daemon=True).start()


def launch_via_steam(steam_id: str, log_fn=_noop_log,
                     extra_args: "list[str] | None" = None) -> None:
    """Launch through Steam (steam://rungameid) so the Steam API initialises.

    Inside a Flatpak sandbox the runtime has no `steam` binary and its own
    xdg-open can't resolve steam:// URLs, so we must forward to the host via
    ``flatpak-spawn --host``. A bare ``subprocess.Popen`` of that command
    "succeeds" (it finds flatpak-spawn) even when the *host* side fails -
    wrong host CWD, missing binary - which is why the Play button silently
    did nothing. ``spawn_watched`` fixes the CWD, watches the real exit code,
    and chains to the next candidate on failure.

    *extra_args* are forwarded to the game's command line. Two forms, tried
    in this order when args are present:
      1. ``steam -applaunch <id> <args>`` - the documented CLI form, Steam
         reliably appends the args (after the user's Launch Options).
      2. the rungameid URL with args after ``//`` ('+'-separated, URL-quoted)
         - best-effort: some Steam builds ignore URL args, but it's the only
         route into a Flatpak Steam (no host ``steam`` binary). xdg-open
         exits 0 once the URL is handed off, so a dropped arg can't be
         detected - which is why it must come *after* -applaunch here,
         reversing the argless candidate order.

    A non-Steam shortcut is addressed differently: rungameid wants the 64-bit
    composite id built from the shortcut's app id, and ``-applaunch`` doesn't
    accept shortcuts at all (it only resolves real store app ids), so that
    candidate is dropped for them.
    """
    in_flatpak = Path("/.flatpak-info").exists()
    have_spawn = shutil.which("flatpak-spawn") is not None

    # Shortcuts have no appmanifest, so Steam addresses them by a composite id
    # rather than the bare app id a store game uses.
    is_shortcut = False
    run_id = str(steam_id)
    try:
        from Utils.steam_shortcuts import is_shortcut_appid, run_gameid
        if is_shortcut_appid(steam_id):
            composite = run_gameid(steam_id)
            if composite:
                is_shortcut, run_id = True, composite
    except Exception:
        pass

    def _url_candidates(target: str) -> "list[list[str]]":
        """The steam:// routes, best first.

        Host xdg-open leads: it hands the URL to whichever Steam the user
        actually has - native, Flatpak com.valvesoftware.Steam or Snap -
        whereas a bare ``steam`` binary only exists for a native install, so
        it is the fallback rather than the primary.
        """
        if in_flatpak and have_spawn:
            return [
                ["flatpak-spawn", "--host", "xdg-open", target],
                ["flatpak-spawn", "--host", "steam", target],
                ["xdg-open", target],
            ]
        return [
            ["xdg-open", target],
            ["steam", target],
        ]

    log_fn(f"Play: launching via Steam "
           f"({'non-Steam shortcut' if is_shortcut else 'app'} {steam_id}) ...")
    url = f"steam://rungameid/{run_id}"
    if extra_args and is_shortcut:
        # No -applaunch route for a shortcut; the URL form is all there is, so
        # it keeps the full route chain rather than a trimmed one.
        from urllib.parse import quote
        url += "//" + "+".join(quote(a, safe="-_.~") for a in extra_args)
        log_fn(f"Play: forwarding launch args through Steam: {' '.join(extra_args)}")
        candidates = _url_candidates(url)
    elif extra_args:
        from urllib.parse import quote
        url += "//" + "+".join(quote(a, safe="-_.~") for a in extra_args)
        applaunch = ["steam", "-applaunch", steam_id, *extra_args]
        log_fn(f"Play: forwarding launch args through Steam: {' '.join(extra_args)}")
        if in_flatpak and have_spawn:
            candidates = [
                ["flatpak-spawn", "--host", *applaunch],
                ["flatpak-spawn", "--host", "xdg-open", url],
                ["xdg-open", url],
            ]
        else:
            candidates = [
                applaunch,
                ["xdg-open", url],
            ]
    else:
        # Ordered candidates, each falling through to the next on non-zero exit.
        candidates = _url_candidates(url)

    def _try(idx: int) -> None:
        if idx >= len(candidates):
            msg = "Play error: could not reach Steam (no working launcher)."
            log_fn(msg)
            launch_report.mark_failed(msg)
            return
        spawn_watched(
            candidates[idx],
            f"Play steam://{steam_id}",
            log_fn,
            on_fail=lambda: _try(idx + 1),
            log_success=True,
        )

    _try(0)


def launch_via_heroic(heroic_app_names: list, log_fn=_noop_log) -> bool:
    """Launch through Heroic (heroic://launch). Returns False if the game
    isn't in a Heroic library (caller may fall through to Proton).

    Same chain-on-failure pattern as launch_via_steam: xdg-open only works
    when a heroic:// scheme handler is registered on the host, which AppImage
    Heroic installs don't do - fall through to invoking the Heroic flatpak /
    binary directly with the URL as argument (Heroic accepts it via its
    protocol Exec line)."""
    from Utils.heroic_finder import find_heroic_launch_info
    info = find_heroic_launch_info(heroic_app_names)
    if info is None:
        log_fn("Play: game not found in Heroic library.")
        return False
    store, app_name = info
    url = f"heroic://launch/{store}/{app_name}"
    log_fn(f"Play: launching via Heroic ({store}/{app_name}) ...")

    in_flatpak = Path("/.flatpak-info").exists()
    host = (["flatpak-spawn", "--host"]
            if in_flatpak and shutil.which("flatpak-spawn") else [])
    candidates = [
        [*host, "xdg-open", url],
        [*host, "flatpak", "run", "com.heroicgameslauncher.hgl", url],
        [*host, "heroic", url],
    ]

    def _try(idx: int) -> None:
        if idx >= len(candidates):
            msg = "Play error: could not reach Heroic (no working launcher)."
            log_fn(msg)
            launch_report.mark_failed(msg)
            return
        spawn_watched(
            candidates[idx],
            f"Play heroic://{store}/{app_name}",
            log_fn,
            on_fail=lambda: _try(idx + 1),
            log_success=True,
        )

    _try(0)
    return True


def launch_via_lutris(slugs: list, log_fn=_noop_log) -> bool:
    """Launch through Lutris (lutris:rungame/<slug>). Returns False if the
    game isn't in a Lutris library (caller may fall through to Proton).

    Lutris runs the game itself - its configured runner, env and runtime -
    so this works for both flavors of Lutris install. Flatpak Lutris is
    invoked with ``flatpak run``; from inside our own sandbox everything is
    forwarded to the host via ``flatpak-spawn --host`` (same chain-on-failure
    pattern as launch_via_steam)."""
    from Utils.lutris_finder import find_lutris_launch_info
    try:
        info = find_lutris_launch_info(slugs)
    except Exception:
        info = None
    if info is None:
        log_fn("Play: game not found in Lutris library.")
        return False
    slug, lutris_is_flatpak = info
    url = f"lutris:rungame/{slug}"
    log_fn(f"Play: launching via Lutris ({slug}"
           f"{', flatpak' if lutris_is_flatpak else ''}) ...")

    in_flatpak = Path("/.flatpak-info").exists()
    host = (["flatpak-spawn", "--host"]
            if in_flatpak and shutil.which("flatpak-spawn") else [])
    if lutris_is_flatpak:
        # Most direct for the flatpak; xdg-open as a backstop (routes the
        # lutris: URL to whichever Lutris registered the scheme handler).
        candidates = [
            [*host, "flatpak", "run", "net.lutris.Lutris", url],
            [*host, "xdg-open", url],
        ]
    else:
        # Native and AppImage installs both register the ``lutris:`` URL
        # scheme with the desktop, so xdg-open reaches them without a
        # ``lutris`` binary on PATH (AppImage installs have none). The bare
        # ``lutris`` command is only a fallback for the rare setup where the
        # scheme isn't registered but the binary is installed.
        candidates = [
            [*host, "xdg-open", url],
            [*host, "lutris", url],
        ]
        # AppImage installs aren't launchable by command or reachable via the
        # lutris: scheme (which may be registered to a *different* Lutris, e.g.
        # a co-installed Flatpak). When the user has pointed us at the AppImage
        # file, run it directly with the URL first so the request reaches the
        # instance that actually owns the game - and so Play can start Lutris
        # when it isn't already open.
        from Utils.ui_config import load_lutris_appimage_path
        appimage = load_lutris_appimage_path()
        if appimage and Path(appimage).is_file():
            candidates.insert(0, [*host, appimage, url])

    def _try(idx: int) -> None:
        if idx >= len(candidates):
            msg = "Play error: could not reach Lutris (no working launcher)."
            log_fn(msg)
            launch_report.mark_failed(msg)
            return
        spawn_watched(
            candidates[idx],
            f"Play lutris:{slug}",
            log_fn,
            on_fail=lambda: _try(idx + 1),
            log_success=True,
        )

    _try(0)
    return True


def launch_via_faugus(gameids: list, log_fn=_noop_log) -> bool:
    """Launch through Faugus (faugus-launcher --game <gameid>). Returns False
    if the game isn't in a Faugus library (caller may fall through to Proton).

    Faugus runs the game itself - its configured runner, env, MangoHud etc. -
    via umu. There is no URL scheme, so the flatpak is invoked with
    ``flatpak run`` and native installs by command; an AppImage install has
    no binary on PATH, so when the user has pointed us at the AppImage file
    it is tried first (same chain-on-failure pattern as launch_via_lutris)."""
    from Utils.faugus_finder import FAUGUS_FLATPAK_ID, find_faugus_launch_info
    try:
        info = find_faugus_launch_info(gameids)
    except Exception:
        info = None
    if info is None:
        log_fn("Play: game not found in Faugus library.")
        return False
    gameid, faugus_is_flatpak = info
    log_fn(f"Play: launching via Faugus ({gameid}"
           f"{', flatpak' if faugus_is_flatpak else ''}) ...")

    in_flatpak = Path("/.flatpak-info").exists()
    host = (["flatpak-spawn", "--host"]
            if in_flatpak and shutil.which("flatpak-spawn") else [])
    if faugus_is_flatpak:
        candidates = [
            [*host, "flatpak", "run", FAUGUS_FLATPAK_ID, "--game", gameid],
        ]
    else:
        candidates = [
            [*host, "faugus-launcher", "--game", gameid],
        ]
        from Utils.ui_config import load_faugus_appimage_path
        appimage = load_faugus_appimage_path()
        if appimage and Path(appimage).is_file():
            candidates.insert(0, [*host, appimage, "--game", gameid])

    def _try(idx: int) -> None:
        if idx >= len(candidates):
            msg = "Play error: could not reach Faugus (no working launcher)."
            log_fn(msg)
            launch_report.mark_failed(msg)
            return
        spawn_watched(
            candidates[idx],
            f"Play faugus:{gameid}",
            log_fn,
            on_fail=lambda: _try(idx + 1),
            log_success=True,
        )

    _try(0)
    return True


# ---------------------------------------------------------------------------
# Bethesda tool-prefix setup (ported from wizards/_proton_prefix.py, which
# imports customtkinter and therefore can't be reused from Qt)
# ---------------------------------------------------------------------------

def link_plugins_txt(game, pfx: Path, log_fn=_noop_log) -> None:
    """Symlink the deployed profile's plugins.txt into a tool prefix.

    No-op for games without the Bethesda plugins.txt machinery.
    """
    if not hasattr(game, "_symlink_plugins_txt"):
        return
    profile = ""
    try:
        profile = game.get_last_deployed_profile() or ""
    except Exception:
        pass
    try:
        game._symlink_plugins_txt(profile or "default", log_fn, prefix_root=pfx)
    except Exception as exc:
        log_fn(f"plugins.txt link failed: {exc}")


def link_mygames(game, pfx: Path, log_fn=_noop_log) -> None:
    """Symlink the game prefix's My Games/<Game> dir into a tool prefix.

    Gives tools that read the game INIs (xEdit needs Skyrim.ini or it exits
    with a fatal error) the same files the game itself uses.
    """
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    docs = getattr(game, "_MYGAMES_DOCS", None)
    sub = getattr(game, "_MYGAMES_SUBPATH", None)
    if game_pfx is None or docs is None or sub is None:
        return
    src = game_pfx / docs / sub
    if not src.is_dir():
        log_fn(f"game-prefix My Games folder not found ({src}) - skipping link.")
        return
    dst = pfx / docs / sub
    if dst.is_symlink() or dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src, target_is_directory=True)
        log_fn(f"linked My Games → {dst}")
    except OSError as exc:
        log_fn(f"My Games link failed: {exc}")


_DOCUMENTS_REL = Path("drive_c/users/steamuser/Documents")


def link_game_documents(game, pfx: Path, subpath, log_fn=_noop_log) -> None:
    """Link the game prefix's Documents/<subpath> folder into a tool prefix.

    Some tools (e.g. Witcher 3 Script Merger) put a FileSystemWatcher on the
    game's user-documents folder (Documents\\The Witcher 3, where the mod
    load order lives) and crash on construction if it doesn't exist.  A fresh
    isolated/shared tool prefix has no such folder, so we symlink the game
    prefix's real one in (keeping the load order in sync).  If the game prefix
    doesn't have it either, create an empty directory so the watcher is happy.
    """
    sub = Path(subpath)
    dst = pfx / _DOCUMENTS_REL / sub
    if dst.is_symlink() or dst.exists():
        return
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    src = (Path(game_pfx) / "pfx" / _DOCUMENTS_REL / sub
           if game_pfx is not None else None)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src is not None and src.is_dir():
            dst.symlink_to(src, target_is_directory=True)
            log_fn(f"linked Documents/{sub} → {dst}")
        else:
            dst.mkdir(parents=True, exist_ok=True)
            log_fn(f"created empty Documents/{sub} in tool prefix "
                   "(game prefix copy not found).")
    except OSError as exc:
        log_fn(f"Documents/{sub} link failed: {exc}")


def enable_show_dotfiles(proton_script: Path, env: dict,
                         log_fn=_noop_log) -> None:
    """Set Wine's ``ShowDotFiles=Y`` in the prefix behind *proton_script*/*env*.

    Enables browsing of Unix dot-dirs (e.g. under Z:) from Wine file dialogs so
    tools can reach mod-manager data. Idempotent - ``reg add /f`` overwrites any
    existing value - so it's safe to call on every prefix resolution, not just
    on first creation; that also repairs prefixes made before this behaviour.
    """
    from Utils.steam_finder import proton_run_command
    try:
        subprocess.run(
            # runinprefix: no steam.exe shim, so the write doesn't flash the
            # game as "Running" in Steam (prefix already exists by this point).
            proton_run_command(proton_script, "runinprefix", "reg", "add",
                               r"HKCU\Software\Wine", "/v", "ShowDotFiles",
                               "/t", "REG_SZ", "/d", "Y", "/f", env=env),
            env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as exc:
        log_fn(f"ShowDotFiles: could not enable ({exc}).")


def get_tool_prefix_env(
    exe_path: Path, proton_name: str, prefix_dir: Path | None = None,
    steam_id: str | None = None,
) -> tuple[Path, Path, dict] | None:
    """Resolve (proton_script, prefix_dir, env) for a tool's isolated prefix.

    proton_name is the display name from the dropdown (e.g. "Proton 10.0").
    Returns None if the Proton version can't be found. The prefix directory is
    created if missing; wineboot initialises it when brand new (synchronous,
    up to 60s - call from a worker thread).

    *steam_id* is accepted for call-site symmetry but intentionally NOT used to
    set SteamAppId - see the app-context note below (the tool env pins app 0 so
    Steam Input doesn't apply the game's controller profile to the tool).
    """
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_steam_root_for_proton_script,
        proton_run_command,
    )
    # Steam-less box with no umu anywhere: fetch one before resolving, or the
    # steam-root lookup below has to fail (there is no viable launcher).
    from Utils.umu_launcher import ensure_umu_run
    ensure_umu_run()

    proton_script = find_any_installed_proton(proton_name)
    if proton_script is None:
        return None

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None

    if prefix_dir is None:
        prefix_dir = exe_path.parent / f"prefix_{proton_script.parent.name}"
    is_new = not (prefix_dir / "pfx").is_dir()
    prefix_dir.mkdir(parents=True, exist_ok=True)

    env = strip_appimage_env(os.environ.copy())
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    # App *context* of 0 (not the game's AppId): lsteamclient asserts when it
    # attaches with no app context at all, so it needs *some* value - but the
    # real game AppId also makes Steam Input bind the game's controller profile
    # to the tool (mouse-less, no on-screen keyboard, trackpad-as-mouse gone),
    # even though "runinprefix" keeps the game from showing as Running. Tools in
    # an isolated prefix don't need SteamAPI_Init to see the *game*, so app 0
    # keeps lsteamclient happy while leaving the desktop/OSK input profile in
    # place. The game's own launch path still sets the real AppId (where the
    # profile swap is wanted).
    env["SteamAppId"] = "0"
    env["SteamGameId"] = "0"
    env["SteamOverlayGameId"] = "0"
    env["STEAM_COMPAT_APP_ID"] = "0"

    if is_new:
        try:
            from Utils.steam_finder import steam_client_installed
            subprocess.run(
                # Must stay on the "run" verb: it's what triggers Proton's
                # full prefix setup (dist files, DLL overrides, tracked_files)
                # on a brand-new prefix - "runinprefix" deliberately skips it.
                proton_run_command(proton_script, "run", "wineboot", "--init",
                                   env=env),
                env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # Steam-less systems boot the prefix through umu-run, whose
                # first ever run downloads the Steam Linux Runtime - allow
                # for that instead of aborting the init at 60s.
                timeout=60 if steam_client_installed() else 600,
            )
        except Exception:
            pass

    # Enable "Show dotfiles" so tools can browse Unix dot-dirs under Z:.
    # Applied on every resolution (idempotent) - always ensures the property is
    # set, and repairs prefixes created before this behaviour existed.
    enable_show_dotfiles(proton_script, env)

    return proton_script, prefix_dir, env


def prepare_tool_prefix(exe_path: Path, proton_name: str, game,
                        log_fn=_noop_log) -> tuple[Path, Path, dict] | None:
    """get_tool_prefix_env + the Bethesda registry/plugins.txt/My Games setup.

    Mirrors Tk's ExeConfigPanel._get_selected_tool_env. Synchronous (wineboot
    on first use) - call from a worker thread.
    """
    result = get_tool_prefix_env(
        exe_path, proton_name, steam_id=effective_steam_id(game),
    )
    if result is None:
        from Utils.steam_finder import steamless_launch_error
        reason = steamless_launch_error()
        log_fn(f"Prefix tools: {reason}" if reason else
               f"Prefix tools: could not find Proton '{proton_name}'.")
        return None
    proton_script, prefix_dir, env = result
    if getattr(game, "synthesis_registry_name", None):
        from Utils.bethesda_registry import maybe_register_for_game
        maybe_register_for_game(
            prefix_dir=prefix_dir,
            proton_script=proton_script,
            env=env,
            game=game,
            log_fn=log_fn,
        )
    pfx = prefix_dir / "pfx"
    link_plugins_txt(game, pfx, lambda m: log_fn(f"Prefix tools: {m}"))
    link_mygames(game, pfx, lambda m: log_fn(f"Prefix tools: {m}"))
    return result


# ---------------------------------------------------------------------------
# Wizard-tool prefix placement (ported from wizards/_proton_prefix.py; file
# formats identical so choices are shared with the Tk wizards)
# ---------------------------------------------------------------------------

# Prefix-placement modes persisted per-exe alongside the Proton override.
PREFIX_MODE_ISOLATED = "isolated"  # prefix_<Proton>/ next to the exe (default)
PREFIX_MODE_SHARED = "shared"      # wine_prefixes/shared_<Proton>/, one per Proton
PREFIX_MODE_GAME = "game"          # reuse the game's own prefix

_LAUNCH_ENV_FILE = "launch_env.json"
_LAUNCH_ARGS_FILE = "launch_args.json"
_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def shared_prefix_dir(proton_dir_name: str) -> Path:
    """Return the shared tool prefix dir for a Proton version (one per version).

    Lives under the app config ``wine_prefixes/`` folder so it is shared by
    every wizard tool that opts into the shared prefix and survives Clear Cache.
    """
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"shared_{proton_dir_name}"


def load_prefix_mode(game, exe_name: str) -> str:
    """Return the saved prefix-placement mode for exe_name (isolated default)."""
    val = _read_launch_mode_data(game).get(f"__prefix_mode_{exe_name}")
    return val if val in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME) else PREFIX_MODE_ISOLATED


def save_prefix_mode(game, exe_name: str, mode: str) -> None:
    """Persist the prefix-placement mode for exe_name (isolated = remove key)."""
    _write_launch_mode_key(
        game, f"__prefix_mode_{exe_name}",
        mode if mode in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME) else None)


def load_winetricks_style(game, exe_name: str) -> bool:
    """Whether exe_name should launch via plain Wine (winetricks-style)
    instead of the proton script. Off by default; opt-in per tool."""
    return bool(_read_launch_mode_data(game).get(
        f"__winetricks_style_{exe_name}", False))


def save_winetricks_style(game, exe_name: str, enabled: bool) -> None:
    """Persist the winetricks-style launch choice (off = remove key)."""
    _write_launch_mode_key(game, f"__winetricks_style_{exe_name}",
                           True if enabled else None)


def load_tool_launch_env(exe: Path | None) -> str:
    """Return the saved env-var string for this exe ('' if none)."""
    if exe is None:
        return ""
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(exe.name) or ""
    except (OSError, ValueError):
        return ""


def save_tool_launch_env(exe: Path | None, text: str) -> None:
    """Persist the env-var string in launch_env.json next to the exe."""
    if exe is None:
        return
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if text:
        data[exe.name] = text
    else:
        data.pop(exe.name, None)
    try:
        if data:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        elif p.is_file():
            p.unlink()
    except OSError:
        pass


def load_tool_launch_args(exe: Path | None) -> str:
    """Return the saved extra launch-args string for this exe ('' if none)."""
    if exe is None:
        return ""
    p = exe.parent / _LAUNCH_ARGS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(exe.name) or ""
    except (OSError, ValueError):
        return ""


def save_tool_launch_args(exe: Path | None, text: str) -> None:
    """Persist the extra launch-args string in launch_args.json next to the exe."""
    if exe is None:
        return
    p = exe.parent / _LAUNCH_ARGS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if text:
        data[exe.name] = text
    else:
        data.pop(exe.name, None)
    try:
        if data:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        elif p.is_file():
            p.unlink()
    except OSError:
        pass


def parse_launch_args(text: str) -> list:
    """Split a launch-args string into a list of tokens (shell-quoting aware)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def parse_env_overrides(text: str) -> dict:
    """Parse a space-separated KEY=VALUE string into a dict (bad tokens skipped)."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    out: dict = {}
    for token in tokens:
        if _ENV_VAR_RE.match(token):
            k, v = token.split("=", 1)
            out[k] = v
    return out


def shutdown_prefix_wineserver(proton_script: Path, compat_data: Path,
                               log_fn=None) -> None:
    """Kill leftover wine processes still attached to a tool prefix.

    Proton sidecars (xalia.exe, services.exe, explorer.exe) can keep the
    prefix's wineserver alive indefinitely after the tool itself closes;
    they outlive the app and linger until the desktop session ends.
    """
    try:
        script = Path(proton_script)
        if script.name in ("wine", "wine64"):
            # Lutris wine binary: wineserver sits next to wine.
            bin_dir = script.parent if (script.parent / "wineserver").is_file() else None
        else:
            proton_dir = script.parent
            bin_dir = next(
                (proton_dir / d / "bin" for d in ("files", "dist")
                 if (proton_dir / d / "bin" / "wineserver").is_file()),
                None,
            )
        if bin_dir is None:
            return
        env = strip_appimage_env(os.environ.copy())
        pfx = Path(compat_data) / "pfx"
        env["WINEPREFIX"] = str(pfx if pfx.exists() else Path(compat_data))
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        subprocess.run(
            [str(bin_dir / "wineserver"), "-k"],
            env=env, timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if log_fn is not None:
            log_fn("tool prefix wineserver shut down")
    except Exception:
        pass


def get_game_prefix_env(game, log_fn=_noop_log, *,
                        allow_runner_fallback: bool = False):
    """Resolve (proton_script, compat_data, env) for the game's OWN prefix.

    Reuses the existing game prefix (already initialised by the game), so no
    wineboot is run. Picks the Proton version Steam assigns to the game;
    with *allow_runner_fallback* it falls back to the prefix's recorded
    runner / any installed Proton when there is no Steam mapping (Heroic and
    GOG installs - mirrors the Tk downgrade/Morrowind wizards' resolver).
    Returns None on failure (after logging why).
    """
    from Utils.steam_finder import (
        find_proton_for_game, find_steam_root_for_proton_script,
    )
    from Utils.umu_launcher import ensure_umu_run
    ensure_umu_run(log_fn)

    pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if pfx is None or not Path(pfx).is_dir():
        log_fn("game prefix not found - deploy/launch the game once, or pick "
               "a different prefix option.")
        return None

    # Classic lutris-wine prefixes run tools with the Lutris runner's own
    # wine binary (proton_run_command handles the wine-binary form); the
    # prefix root doubles as the compat-data path.
    from Utils.proton_tools import _resolve_lutris_wine_env
    wine_bin, wenv = _resolve_lutris_wine_env(Path(pfx), log_fn)
    if wine_bin is not None:
        return wine_bin, Path(pfx), wenv

    from Utils.proton_prefix import resolve_compat_data
    steam_id = effective_steam_id(game)
    proton_script = find_proton_for_game(steam_id) if steam_id else None
    if proton_script is None and allow_runner_fallback:
        from Utils.proton_prefix import read_prefix_runner
        from Utils.steam_finder import find_any_installed_proton
        preferred_runner = read_prefix_runner(resolve_compat_data(Path(pfx)))
        if not preferred_runner:
            # Fresh Lutris umu prefixes record the runner in the game yml
            # rather than config_info.
            try:
                from Utils.lutris_finder import find_lutris_proton_name_for_prefix
                preferred_runner = find_lutris_proton_name_for_prefix(Path(pfx)) or ""
            except Exception:
                preferred_runner = ""
        if not preferred_runner:
            # Faugus records the runner in games.json (resolved through its
            # own "<family> Latest" name matching).
            try:
                from Utils.faugus_finder import find_faugus_proton_for_prefix
                faugus_script = find_faugus_proton_for_prefix(Path(pfx))
                if faugus_script is not None:
                    preferred_runner = faugus_script.parent.name
            except Exception:
                pass
        proton_script = find_any_installed_proton(preferred_runner)
        if proton_script is not None:
            log_fn(f"using fallback Proton tool {proton_script.parent.name} "
                   "(no per-game Steam mapping found).")
    if proton_script is None:
        log_fn("could not resolve the game's Proton version - pick a "
               "different prefix option.")
        return None
    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None
    # Steam layout: compat data is the pfx's parent; Heroic/Lutris layouts:
    # the prefix root itself.
    compat_data = resolve_compat_data(Path(pfx))
    env = strip_appimage_env(os.environ.copy())
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    if steam_id:
        set_game_steam_context(env, steam_id)
    return proton_script, compat_data, env


def resolve_tool_prefix(exe: Path, game, proton_name: str, prefix_mode: str,
                        log_fn=_noop_log, *,
                        isolated_prefix_dir: "Path | None" = None):
    """Resolve (proton_script, compat_data, env) for a wizard tool's prefix.

    Honours the chosen placement mode:
      * isolated - creates/initialises prefix_<ProtonName>/ next to the exe,
                   or *isolated_prefix_dir* when given (tools whose exe sits
                   somewhere a prefix shouldn't go, e.g. Creation Kit in the
                   game root, relocate it)
      * shared   - creates/initialises wine_prefixes/shared_<ProtonName>/
      * game     - reuses the game's own prefix (no init)
    Saved per-exe env-var overrides (launch_env.json) are merged into env.
    First use of an isolated/shared prefix runs a synchronous wineboot -
    only call from a worker thread. Returns None on failure.

    Port of the Tk ProtonPrefixStepMixin._get_tool_env (note the different
    tuple order: compat_data before env, matching get_tool_prefix_env).
    """
    if prefix_mode == PREFIX_MODE_GAME:
        result = get_game_prefix_env(game, log_fn=log_fn)
    else:
        target = isolated_prefix_dir
        if prefix_mode == PREFIX_MODE_SHARED:
            from Utils.steam_finder import find_any_installed_proton
            proton_script = find_any_installed_proton(proton_name)
            if proton_script is None:
                log_fn(f"could not find Proton '{proton_name}'.")
                return None
            target = shared_prefix_dir(proton_script.parent.name)
        result = get_tool_prefix_env(
            exe, proton_name, prefix_dir=target,
            steam_id=effective_steam_id(game),
        )
    if result is None:
        # A Steam-less box with no umu-run has no viable launcher at all; say
        # so instead of leaving the caller's vague "could not find Proton"
        # (GH#320 - the reporter's SSEEdit run failed here).
        from Utils.steam_finder import steamless_launch_error
        reason = steamless_launch_error()
        if reason:
            log_fn(reason)
        return None
    proton_script, compat_data, env = result
    extra = parse_env_overrides(load_tool_launch_env(exe))
    if extra:
        env.update(extra)
        log_fn("applying saved env vars: "
               + " ".join(f"{k}={v}" for k, v in extra.items()))
    # Marker consumed by run_tool_logged: launch this tool winetricks-style
    # (plain wine, no proton session - see run_tool_winetricks_style). Set
    # here so every wizard honours the Proton-step checkbox without each
    # run call having to look the setting up itself.
    if load_winetricks_style(game, exe.name):
        env["AMM_WINETRICKS_STYLE"] = "1"
    return proton_script, compat_data, env


def run_tool_logged(
    proton_script: Path,
    exe: Path,
    env: dict,
    log_fn=_noop_log,
    *,
    extra_args: "list[str] | None" = None,
    cwd: "Path | None" = None,
    label: str | None = None,
    winedebug: str = "+err,+warn,fixme-all",
) -> int:
    """Launch *exe* through Proton and stream its output to *log_fn*.

    Replaces the old ``Popen(..., stdout=DEVNULL, stderr=DEVNULL)`` pattern the
    wizard tools all used, which silently swallowed crash traces (e.g.
    WitcherScriptMerger dying instantly with no visible cause). We now:

      * force ``WINEDEBUG`` (unless the caller already set one in *env*) so
        Wine-side load failures are emitted, and
      * merge the tool's stdout+stderr and pump it line-by-line to *log_fn*.

    Blocks until the process exits (call from a worker thread) and returns the
    exit code. *proton_script* and *env* come from ``resolve_tool_prefix``;
    *extra_args* are appended after the exe (e.g. xEdit's data-path flag).
    """
    from Utils.steam_finder import proton_run_command

    label = label or exe.name

    # Winetricks-style launch chosen on the Proton step (marker set by
    # resolve_tool_prefix): bypass the proton script entirely and run the
    # tool the way winetricks does. The prefix location travels in env.
    if env.get("AMM_WINETRICKS_STYLE") == "1":
        prefix = env.get("STEAM_COMPAT_DATA_PATH") or env.get("WINEPREFIX")
        if prefix:
            # The plain-Wine helper deliberately rebuilds a clean environment
            # to drop Proton/Steam session state. Preserve only the host GPU
            # selectors: TexGen/DynDOLOD's discrete-GPU option relies on these
            # reaching the texconv child process even in winetricks-style mode.
            gpu_env_keys = (
                "DRI_PRIME", "__NV_PRIME_RENDER_OFFLOAD",
                "__VK_LAYER_NV_optimus", "__GLX_VENDOR_LIBRARY_NAME",
            )
            return run_tool_winetricks_style(
                proton_script, exe, Path(prefix), log_fn=log_fn,
                extra_args=extra_args,
                extra_env={key: env.get(key) for key in gpu_env_keys},
                cwd=cwd, label=label)
        log_fn(f"{label}: winetricks-style launch requested but no prefix "
               "path in env - falling back to Proton.")

    # Only set WINEDEBUG when the caller hasn't chosen its own channels
    # (BodySlide sets +wgl,+opengl for its GL trace and must win).
    env.setdefault("WINEDEBUG", winedebug)

    # Proton's Xalia UI-automation sidecar is useless to these tools and
    # actively harmful: it crashes on its own (C# traces from
    # NonclientScrollProvider land in our merged pipe, mislabelled with
    # whichever tool we happen to be running) and it destabilises wxWidgets
    # apps by flooding them with window-handle queries. It also inherits our
    # stdout, so it keeps writing after the tool exits. Proton 10 renamed the
    # knob, so set both; setdefault lets a caller opt back in.
    env.setdefault("PROTON_DISABLE_XALIA", "1")
    env.setdefault("PROTON_USE_XALIA", "0")

    # "runinprefix" (not "run"): the run verb boots Proton's steam.exe shim,
    # which attaches to the Steam client and shows the game as "Running" in
    # Steam for the whole tool session (and aborts when no client is
    # reachable). The shim's other services don't apply here: the prefix was
    # already created/updated by get_tool_prefix_env's wineboot step, and every
    # caller converts its path arguments to wine paths itself.
    # Run the tool from its own folder (or the caller's cwd). Pass this to
    # proton_run_command too: inside the flatpak sandbox the portal chdirs the
    # host process itself, and the Popen cwd below is ignored - without it a
    # tool writing files relative to its cwd (e.g. WitcherScriptMerger's
    # MergeInventory.xml) would land at Z:\\ (host "/", unwritable).
    tool_cwd = str(cwd) if cwd is not None else str(exe.parent)
    cmd = proton_run_command(proton_script, "runinprefix", str(exe), env=env,
                             host_cwd=tool_cwd)
    if extra_args:
        cmd = cmd + list(extra_args)

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=tool_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch - {exc}")
        raise

    # A patcher can run for an hour with no input; without this the Deck
    # autosuspends mid-build and Wine rarely survives the resume.
    with inhibit_sleep(f"{label} is running", lambda m: log_fn(f"{label}: {m}")):
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log_fn(f"{label}: {line}")
        rc = proc.wait()
    if rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc


def run_tool_winetricks_style(
    proton_script: Path,
    exe: Path,
    compat_data: Path,
    log_fn=_noop_log,
    *,
    extra_args: "list[str] | None" = None,
    extra_env: "dict | None" = None,
    cwd: "Path | None" = None,
    label: str | None = None,
) -> int:
    """Launch *exe* exactly the way winetricks' "Run an arbitrary executable"
    does: plain ``wine start.exe`` against WINEPREFIX, no ``proton`` script.

    Rationale (Steam Deck): the Proton session env carries the bridge to the
    running Steam client (STEAM_COMPAT_CLIENT_INSTALL_PATH + lsteamclient), and
    some users report Steam Input still swapping to the game's controller
    profile - locking the trackpad/OSK out of the tool's UI - even with
    SteamAppId neutralised. Running an exe from the winetricks GUI never has
    that problem, because winetricks knows nothing of Steam: it inherits the
    desktop environment, sets WINEPREFIX, and calls the raw wine binary. This
    helper mirrors that launch byte-for-byte (env from ``os.environ`` + \
WINEPREFIX + Proton's bin on PATH, ``wine start.exe <exe>``), with only two
    deviations: ``/wait`` so the wizard flow can block until the tool exits,
    and ``/unix`` in place of winetricks' winepath round-trip.

    The prefix itself must already exist (still created/updated through the
    proton script - only the tool launch goes bare). Saved per-exe env-var
    overrides (launch_env.json) are merged like the Proton path does;
    *extra_env* is applied last, a value of ``None`` removes the variable.
    Blocking - call from a worker thread. Returns the exit code.
    """
    label = label or exe.name
    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        # Lutris/Heroic classic-wine prefix: the runner's wine IS the script.
        wine_bin = script
    else:
        wine_bin = next(
            (script.parent / d / "bin" / "wine" for d in ("files", "dist")
             if (script.parent / d / "bin" / "wine").is_file()),
            None,
        )
    if wine_bin is None:
        from Utils.protontricks import _get_proton_bin
        found = _get_proton_bin()
        wine_bin = Path(found) / "wine" if found else None
    if wine_bin is None or not wine_bin.is_file():
        log_fn(f"{label}: no bare wine binary found for {script.parent.name}.")
        return 1
    bin_dir = wine_bin.parent

    pfx = Path(compat_data) / "pfx"
    env = strip_appimage_env(os.environ.copy())
    env["WINEPREFIX"] = str(pfx if pfx.is_dir() else Path(compat_data))
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    # Same Xalia suppression as the Proton path - set before the saved/extra
    # overrides so a caller can still turn it back on.
    env["PROTON_DISABLE_XALIA"] = "1"
    env["PROTON_USE_XALIA"] = "0"
    saved = parse_env_overrides(load_tool_launch_env(exe))
    if saved:
        env.update(saved)
        log_fn(f"{label}: applying saved env vars: "
               + " ".join(f"{k}={v}" for k, v in saved.items()))
    for k, v in (extra_env or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v

    cmd = [str(wine_bin), "start.exe", "/wait", "/unix", str(exe)]
    if extra_args:
        cmd = cmd + list(extra_args)
    log_fn(f"{label}: launching with plain Wine (winetricks-style): "
           f"{' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(cwd) if cwd is not None else str(exe.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch - {exc}")
        launch_report.mark_failed(f"{label}: failed to launch - {exc}")
        raise
    import time as _time
    rep = launch_report.current()
    started = _time.monotonic()
    if rep is not None:
        rep.mark_spawned()

    with inhibit_sleep(f"{label} is running", lambda m: log_fn(f"{label}: {m}")):
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log_fn(f"{label}: {line}")
        rc = proc.wait()
    launch_report.mark_exit(rep, started, rc, label)
    if rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc


def launch_winetricks_in_prefix(wineprefix: Path, log_fn=_noop_log) -> None:
    """Launch the winetricks GUI against *wineprefix* (a .../pfx dir),
    downloading winetricks/cabextract on demand."""
    from Utils.protontricks import (
        _bundled_winetricks,
        _get_proton_bin,
        cabextract_installed,
        install_cabextract,
        install_winetricks,
        winetricks_installed,
    )

    if not wineprefix.is_dir():
        log_fn("Prefix tools: no Wine prefix is available - cannot launch winetricks.")
        return
    if not winetricks_installed():
        log_fn("Prefix tools: winetricks not found - downloading …")
        if not install_winetricks(log_fn=lambda m: log_fn(f"Prefix tools: {m}")):
            return
    if not cabextract_installed():
        log_fn("Prefix tools: cabextract not found - downloading a portable copy …")
        if not install_cabextract(log_fn=lambda m: log_fn(f"Prefix tools: {m}")):
            return

    wt = _bundled_winetricks()
    env = strip_appimage_env(os.environ.copy())
    env["WINEPREFIX"] = str(wineprefix)
    path_prefix = str(wt.parent)
    from Utils.protontricks import wine_bin_dir_for_prefix
    proton_bin = wine_bin_dir_for_prefix(wineprefix, env) or _get_proton_bin()
    if proton_bin:
        path_prefix = proton_bin + os.pathsep + path_prefix
    env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")

    log_fn(f"Prefix tools: launching winetricks GUI against {wineprefix.parent.name} …")
    try:
        subprocess.Popen(
            [str(wt), "--gui"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log_fn(f"Prefix tools error: {e}")


def launch_wine_tool_in_prefix(proton_script: Path, prefix_dir: Path, env: dict,
                               tool: str, log_fn=_noop_log) -> bool:
    """Launch a bundled wine tool (``winecfg`` / ``regedit``) inside an isolated
    tool prefix. Returns False if the launch failed.

    *proton_script*/*env* come from ``get_tool_prefix_env`` (env already carries
    STEAM_COMPAT_DATA_PATH); *prefix_dir* is that same isolated prefix. Mirrors
    proton_tools.wine_tool_command: uses Proton's ``runinprefix`` verb (running
    the raw ``files/bin/wine`` binary core-dumps on modern GE-Proton) and, inside
    our own Flatpak sandbox, forwards the launch to the host.
    """
    from Utils.proton_tools import _host_forward
    from Utils.steam_finder import proton_run_command

    env["WINEPREFIX"] = str(prefix_dir / "pfx")
    cmd = proton_run_command(proton_script, "runinprefix", tool, env=env)
    cmd = _host_forward(cmd, env, lambda m: log_fn(f"Prefix tools: {m}"))
    log_fn(f"Prefix tools: launching {tool} …")
    try:
        subprocess.Popen(cmd, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log_fn(f"Prefix tools error: {e}")
        return False


# ---------------------------------------------------------------------------
# Launch entry points
# ---------------------------------------------------------------------------

def launch_game(game, log_fn=_noop_log) -> None:
    """Launch the game itself: native command / Steam / Heroic / Proton,
    honouring the saved launch mode. Call from a worker thread."""
    from Utils.xdg import host_env
    native_cmd = getattr(game, "get_launch_command", lambda: None)()
    if native_cmd is not None:
        log_fn(f"Play: launching natively: {' '.join(native_cmd)}")
        spawn_process_watched(native_cmd, env=host_env(),
                              label="Play (native)", log_fn=log_fn)
        return

    # A game whose mods are served by an external loader (me3) has no usable
    # Proton route: falling through would start it VANILLA - no mods, and none
    # of the profile's save isolation - while looking like a success. Refuse
    # with the handler's own reason instead.
    if getattr(game, "native_launch_required", False):
        reason = ""
        try:
            reason = game.native_launch_blocked_reason() or ""
        except Exception:
            pass
        reason = reason or (
            "the external mod loader is not ready, and launching without it "
            "would start the game with no mods.")
        log_fn(f"Play: refusing to launch - {reason}")
        launch_report.mark_failed(launch_report.actionable(reason))
        return

    mode = load_launch_mode(game, game_exe_key(game))
    steam_id = effective_steam_id(game)
    heroic_app_names = heroic_app_names_for_launch(game)
    is_steam = game_is_steam_install(game)
    # A game added to Steam as a non-Steam shortcut is a Steam route too, even
    # though its files sit outside every Steam library so game_is_steam_install
    # is False. effective_steam_id() only returns a shortcut id when the user
    # configured the game through that shortcut, so this can't hijack a game
    # that merely happens to have one lying around.
    is_shortcut = False
    if steam_id:
        try:
            from Utils.steam_shortcuts import is_shortcut_appid
            is_shortcut = is_shortcut_appid(steam_id)
        except Exception:
            is_shortcut = False
    default_args = list(getattr(game, "default_launch_args", []) or [])

    def _note_launcher_args(launcher: str) -> None:
        # heroic:// / lutris: / faugus URLs can't carry a command line - the
        # handler's deploy warning tells the user where to add the args.
        if default_args:
            log_fn(f"Play: note - the {launcher} route can't pass launch "
                   f"args; make sure '{' '.join(default_args)}' is set in "
                   f"{launcher}'s own launch settings for this game.")

    # One routing-trace line so a user's session log shows WHY a launch route
    # was chosen or skipped - "the Play button does nothing" reports are
    # undiagnosable without it.
    pkg = ("flatpak" if Path("/.flatpak-info").exists()
           else "appimage" if os.environ.get("APPIMAGE") else "host")
    log_fn(f"Play: routing - mode={mode}, steam_id={steam_id or 'none'}, "
           f"steam_library_install={is_steam}, "
           f"non_steam_shortcut={is_shortcut}, "
           f"heroic={','.join(heroic_app_names) if heroic_app_names else 'none'}, "
           f"pkg={pkg}")

    if mode == "steam":
        if steam_id:
            launch_via_steam(steam_id, log_fn, extra_args=default_args or None)
        else:
            log_fn("Play: launch mode is Steam but game has no Steam ID.")
        return

    if mode == "heroic":
        if heroic_app_names:
            _note_launcher_args("Heroic")
            launch_via_heroic(heroic_app_names, log_fn)
        else:
            log_fn("Play: launch mode is Heroic but game has no Heroic app name.")
        return

    if mode == "lutris":
        slugs = lutris_slugs_for_launch(game)
        if slugs:
            _note_launcher_args("Lutris")
            launch_via_lutris(slugs, log_fn)
        else:
            log_fn("Play: launch mode is Lutris but the game was not found in Lutris.")
        return

    if mode == "faugus":
        gameids = faugus_gameids_for_launch(game)
        if gameids:
            _note_launcher_args("Faugus")
            launch_via_faugus(gameids, log_fn)
        else:
            log_fn("Play: launch mode is Faugus but the game was not found in Faugus.")
        return

    if mode != "none":  # "auto"
        if steam_id and (is_steam or is_shortcut):
            launch_via_steam(steam_id, log_fn, extra_args=default_args or None)
            return
        if heroic_app_names and game_is_heroic_install(game):
            _note_launcher_args("Heroic")
            if launch_via_heroic(heroic_app_names, log_fn):
                return
        # Lutris/Faugus last among launchers (computed lazily - the scans
        # read Lutris's sqlite DB + yml configs / Faugus's games.json).
        lutris_slugs = lutris_slugs_for_launch(game)
        if lutris_slugs:
            if launch_via_lutris(lutris_slugs, log_fn):
                _note_launcher_args("Lutris")
                return
        faugus_gameids = faugus_gameids_for_launch(game)
        if faugus_gameids:
            if launch_via_faugus(faugus_gameids, log_fn):
                _note_launcher_args("Faugus")
                return
        log_fn("Play: no Steam/Heroic/Lutris/Faugus route matched - launching "
               "the game executable directly.")

    exe_path = resolve_game_exe(game)
    if exe_path is None:
        log_fn("Play: could not find the game's executable on disk.")
        return

    # Native Linux binary (no .exe/.bat suffix): run directly instead of
    # routing through Proton, which would fail on an ELF executable.
    if exe_path.suffix.lower() not in (".exe", ".bat"):
        log_fn(f"Play: launching native binary: {exe_path}")
        spawn_process_watched([str(exe_path)], env=host_env(),
                              cwd=exe_path.parent,
                              label="Play (native)", log_fn=log_fn)
        return

    launch_exe_via_proton(exe_path, game, log_fn)


def is_framework_launch_exe(game, exe_name: str) -> bool:
    """True when *exe_name* is a declared framework launcher (script extender).

    These start the game rather than a tool, so they need the game's own
    prefix and Steam app context - see launch_exe_via_proton.
    """
    if game is None or not exe_name:
        return False
    try:
        declared = getattr(game, "framework_launch_exes", None) or {}
    except Exception:
        return False
    target = exe_name.lower()
    return any(Path(rel).name.lower() == target for rel in declared.values())


def is_game_launch_exe(game, exe_path: Path) -> bool:
    """True when *exe_path* starts the game rather than a utility.

    Besides the primary launcher and framework loaders, handlers may declare
    direct runtime binaries such as SkyrimSE.exe.  Those binaries need the
    Steam shim/game verb just as much as the launcher does; treating one as a
    generic tool uses ``runinprefix`` and leaves SteamAPI partially detached.
    """
    if game is None:
        return False
    if is_framework_launch_exe(game, exe_path.name):
        return True
    game_exe = resolve_game_exe(game)
    if (game_exe is not None
            and str(exe_path).lower() == str(game_exe).lower()):
        return True
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return False
    try:
        declared = getattr(game, "direct_launch_exes", None) or []
    except Exception:
        declared = []
    target = str(exe_path).lower()
    return any(target == str(Path(game_path) / rel).lower() for rel in declared)


def steam_launch_options_for_game(game, log_fn=_noop_log) -> str:
    """Steam's saved Launch Options for this game, for a launcher-less launch.

    Only a fallback: options set in the app's own launch settings win. Steam
    applies these when it starts the game, so without them a "None" launch
    silently drops what the user relies on (SteamDeck=0, a gamemoderun/mangohud
    wrapper).
    """
    steam_id = effective_steam_id(game)
    if not steam_id:
        return ""
    try:
        from Utils.steam_finder import steam_launch_options
        opts = steam_launch_options(steam_id)
    except Exception as e:
        log_fn(f"Run EXE: could not read Steam's launch options - {e}")
        return ""
    if not opts:
        return ""
    # Popen does no tilde expansion, so a wrapper like "~/lsfg %command%"
    # would fail to exec where it works under Steam.
    opts = re.sub(r"(?<![\w~/])~/", str(Path.home()) + "/", opts)
    log_fn(f"Run EXE: using Steam's launch options for app {steam_id}: {opts}")
    return opts


def epic_args_for_game(game, log_fn=_noop_log) -> list:
    """Epic (EOS) login arguments for a Heroic-managed Epic game, else []."""
    app_names = heroic_app_names_for_launch(game)
    if not app_names:
        return []
    try:
        from Utils.heroic_finder import epic_launch_args, find_heroic_launch_info
        info = find_heroic_launch_info(app_names)
        if info is None or info[0] != "legendary":
            return []
        return epic_launch_args(info[1], lambda m: log_fn(f"Run EXE: {m}"))
    except Exception as e:
        log_fn(f"Run EXE: could not fetch Epic launch arguments - {e}")
        return []


def steam_compat_mounts(game, exe_path: Path) -> dict:
    """Extra paths a Steam Linux Runtime launch must see, as env vars.

    pressure-vessel exposes $HOME, the game install and whatever Steam names
    explicitly, so a deployed mod that symlinks into a staging tree elsewhere
    (SD card, second drive) would dangle inside the container.
    STEAM_COMPAT_MOUNTS is Steam's channel for that; a bare Proton call
    ignores it.
    """
    paths: list[str] = []

    def _add(candidate) -> None:
        if candidate is None:
            return
        try:
            p = Path(candidate)
            if not p.is_dir():
                return
            real = str(p.resolve())
        except OSError:
            return
        # A ":" in the path would split into two bogus entries - drop it
        # rather than corrupt the list.
        if ":" in real or real in paths:
            return
        paths.append(real)

    for getter in ("get_effective_mod_staging_path", "get_mod_staging_path"):
        fn = getattr(game, getter, None)
        if fn is None:
            continue
        try:
            staging = fn()
        except Exception:
            continue
        _add(staging)
        # The staging root itself covers the sibling Applications/ (wizard
        # tools) and profiles/ trees a launch may reach into.
        if staging is not None:
            _add(Path(staging).parent)
    _add(exe_path.parent)
    try:
        _add(exe_path.resolve().parent)
    except OSError:
        pass
    return {"STEAM_COMPAT_MOUNTS": ":".join(paths)} if paths else {}


def launch_exe_via_proton(exe_path: Path, game, log_fn=_noop_log) -> None:
    """Standard Proton launch path for .exe files. Call from a worker thread.

    Uses the game's prefix by default; a saved per-exe Proton override runs in
    an isolated prefix_<Proton>/ next to the exe (with Bethesda registry /
    plugins.txt / My Games setup mirrored from the wizard prefixes).

    Non-Steam prefixes (Lutris, Heroic, hand-made): classic lutris-wine
    prefixes run with the runner's own wine binary; Proton-managed ones run
    through umu-run (as Lutris and modern Heroic do) so the launch never
    attaches to the Steam client - no Steam ownership needed, no "running"
    status in Steam, and the Steam Linux Runtime container is used (fixes
    missing audio vs a raw `proton run`).
    """
    from Utils.proton_prefix import read_prefix_runner, resolve_compat_data
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_proton_for_game,
        find_steam_root_for_proton_script,
        list_installed_proton,
        proton_run_command,
    )
    from Utils.umu_launcher import ensure_umu_run
    ensure_umu_run(log_fn)

    proton_override_name = load_proton_override(game, exe_path.name)
    # Script extenders always use the game's prefix. The settings UI disables
    # the picker for these, but an override saved before that gate existed (or
    # edited by hand) must not resurrect the isolated-prefix path.
    if proton_override_name and is_framework_launch_exe(game, exe_path.name):
        log_fn(f"Run EXE: {exe_path.name} is a script extender - ignoring the "
               f"'{proton_override_name}' override and using the game's prefix "
               "(it launches the game, which needs the game's Steam app ID).")
        proton_override_name = None
    lutris_env_extra = None  # set for classic lutris-wine prefixes only
    umu_bin = None  # set for Lutris umu/Proton prefixes when umu-run exists
    prefix_path = None  # game-prefix branch only (None with a Proton override)
    lutris_is_prefix = False
    if proton_override_name:
        # Try exact match first, then prefix match ("Proton 10" → "Proton 10.0")
        proton_script = find_any_installed_proton(proton_override_name)
        if proton_script is None:
            override_lower = proton_override_name.lower()
            for candidate in list_installed_proton():
                if candidate.parent.name.lower().startswith(override_lower):
                    proton_script = candidate
                    break
        if proton_script is None:
            log_fn(f"Run EXE: Proton override '{proton_override_name}' not found.")
            return
        # Dedicated prefix next to the exe so it's isolated from the game prefix
        compat_data = exe_path.parent / f"prefix_{proton_script.parent.name}"
        compat_data.mkdir(parents=True, exist_ok=True)
        log_fn(f"Run EXE: using {proton_script.parent.name} with isolated prefix.")
    else:
        prefix_path = (
            game.get_prefix_path()
            if hasattr(game, "get_prefix_path") else None
        )
        if prefix_path is None or not prefix_path.is_dir():
            log_fn("Run EXE: Proton prefix not configured for this game.")
            return

        compat_data = resolve_compat_data(prefix_path)

        proton_script = None
        lutris_is_prefix = False
        try:
            from Utils.lutris_finder import (
                is_lutris_prefix, find_lutris_wine_for_prefix,
                find_lutris_proton_name_for_prefix, lutris_wine_env)
            lutris_is_prefix = is_lutris_prefix(prefix_path)
            if lutris_is_prefix:
                # Classic lutris-wine prefix: launch with the Lutris runner's
                # own wine binary (umu/Proton-made ones use Proton below).
                wine_bin = find_lutris_wine_for_prefix(prefix_path)
                if wine_bin is not None:
                    proton_script = wine_bin
                    lutris_env_extra = lutris_wine_env(wine_bin, prefix_path)
                    log_fn(f"Run EXE: Lutris prefix - using Lutris wine "
                           f"runner {wine_bin.parent.parent.name}.")
        except Exception:
            lutris_is_prefix = False

        steam_id = effective_steam_id(game)
        if proton_script is None:
            proton_script = find_proton_for_game(steam_id) if steam_id else None
        if proton_script is None and lutris_is_prefix:
            # Fresh Lutris prefixes have no config_info yet - the runner is
            # recorded in the game's Lutris yml instead.
            lutris_runner = find_lutris_proton_name_for_prefix(prefix_path)
            if lutris_runner:
                proton_script = find_any_installed_proton(lutris_runner)
                if proton_script is not None:
                    log_fn(f"Run EXE: using Lutris-configured Proton "
                           f"{proton_script.parent.name}.")
        if proton_script is None:
            # Faugus records the game's runner in games.json - using it keeps
            # tool launches on the same Proton Faugus itself uses.
            try:
                from Utils.faugus_finder import find_faugus_proton_for_prefix
                proton_script = find_faugus_proton_for_prefix(prefix_path)
            except Exception:
                proton_script = None
            if proton_script is not None:
                log_fn(f"Run EXE: using Faugus-configured Proton "
                       f"{proton_script.parent.name}.")
        if proton_script is None:
            # Heroic records the game's runner in its GamesConfig - using it
            # keeps tool launches on the same Proton Heroic itself uses.
            try:
                from Utils.heroic_finder import find_heroic_proton_for_prefix
                proton_script = find_heroic_proton_for_prefix(prefix_path)
            except Exception:
                proton_script = None
            if proton_script is not None:
                log_fn(f"Run EXE: using Heroic-configured Proton "
                       f"{proton_script.parent.name}.")
        if proton_script is None:
            # Use the same Proton version the prefix was built with.
            preferred_runner = read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                if steam_id:
                    log_fn(
                        f"Run EXE: could not find Proton version for app {steam_id}, "
                        "and no installed Proton tool was found."
                    )
                else:
                    log_fn("Run EXE: no Steam ID and no installed Proton tool was found.")
                return
            log_fn(
                f"Run EXE: using fallback Proton tool {proton_script.parent.name} "
                "(no per-game Steam mapping found)."
            )

        # Steam-managed prefixes always live at steamapps/compatdata/<appid>;
        # anything else (Lutris, Heroic, hand-made) is a non-Steam prefix.
        steam_managed = compat_data.parent.name.lower() == "compatdata"
        if lutris_env_extra is None and (lutris_is_prefix or not steam_managed):
            # Non-Steam Proton prefix: launch through umu-run, the same
            # launcher Lutris (and modern Heroic) use. Raw `proton run`
            # attaches to the Steam client (SteamAppId + client install
            # path), so the game shows as "running" in Steam, needs to be
            # owned there, and runs outside the Steam Linux Runtime container
            # (broken audio on some setups). umu runs Proton inside the
            # container with no Steam client attach at all.
            from Utils.lutris_finder import find_umu_run
            umu_bin = find_umu_run()
            if umu_bin is None:
                log_fn("Run EXE: umu-run not found - falling back to Proton "
                       "without the Steam Linux Runtime container.")

    env = strip_appimage_env(os.environ.copy())
    if lutris_env_extra is not None:
        # Bare wine invocation: WINEPREFIX + runner libs; no Steam client or
        # compat-data plumbing applies.
        env.update(lutris_env_extra)
        for key in ("SteamAppId", "SteamGameId", "SteamOverlayGameId",
                    "STEAM_COMPAT_APP_ID"):
            env.pop(key, None)
    elif umu_bin is not None:
        # umu derives its own compat plumbing from WINEPREFIX + PROTONPATH;
        # deliberately no STEAM_COMPAT_* / SteamAppId here - that's what
        # makes the launch independent of the Steam client. Proton resolves
        # the actual prefix as $WINEPREFIX/pfx (umu adds a pfx → . self-link
        # when absent), so WINEPREFIX is the compat-data root - except for
        # Lutris-shaped prefixes (drive_c at the prefix path itself; for a
        # bare hand-made prefix resolve_compat_data returns the parent,
        # which would be a different prefix).
        env["WINEPREFIX"] = str(prefix_path if lutris_is_prefix
                                else compat_data)
        env["PROTONPATH"] = str(proton_script.parent)
        # A parent Steam/Gaming Mode launch can leave an unrelated app context
        # in Amethyst's environment. umu is deliberately Steam-independent.
        for key in ("SteamAppId", "SteamGameId", "SteamOverlayGameId",
                    "STEAM_COMPAT_APP_ID"):
            env.pop(key, None)
        env["GAMEID"] = "umu-default"
    else:
        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            log_fn("Run EXE: could not determine Steam root for the selected Proton tool.")
            return
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        # Proton expects these to locate the game install and per-game shader/
        # compat caches; without them GE-Proton falls back to app ID 0.
        game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
        if game_path and not proton_override_name:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if not proton_override_name:
            steam_id = effective_steam_id(game)
            if steam_id:
                set_game_steam_context(env, steam_id)
        else:
            # An isolated tool prefix must not inherit Amethyst's Steam
            # shortcut/game context. Keep lsteamclient neutral, matching
            # get_tool_prefix_env's dedicated-prefix path.
            env["SteamAppId"] = "0"
            env["SteamGameId"] = "0"
            env["SteamOverlayGameId"] = "0"
            env["STEAM_COMPAT_APP_ID"] = "0"
        for key, value in steam_compat_mounts(game, exe_path).items():
            env[key] = value

    if proton_override_name:
        # Bethesda games: mirror the wizard-prefix setup so tools in the
        # isolated prefix see the game path (registry), the deployed
        # plugins.txt and the game's My Games INIs. All no-ops otherwise.
        if getattr(game, "synthesis_registry_name", None):
            from Utils.bethesda_registry import maybe_register_for_game
            maybe_register_for_game(
                prefix_dir=compat_data,
                proton_script=proton_script,
                env=env,
                game=game,
                log_fn=log_fn,
            )
        pfx = compat_data / "pfx"
        link_plugins_txt(game, pfx, lambda m: log_fn(f"Run EXE: {m}"))
        link_mygames(game, pfx, lambda m: log_fn(f"Run EXE: {m}"))

    try:
        extra_args = shlex.split(load_exe_args(game, exe_path.name))
    except ValueError as e:
        log_fn(f"Run EXE: invalid arguments - {e}")
        return

    if load_winetricks_style(game, exe_path.name):
        # Per-exe opt-in mirrored from the wizards' Proton step: bypass the
        # Proton session and run bare `wine start.exe` against the resolved
        # prefix (see run_tool_winetricks_style). The prefix itself is still
        # created/updated through Proton. Env vars from Launch Options are
        # applied; wrappers/%command% are not - there is no wrapped command.
        extra_env, _ = parse_launch_options(
            load_launch_options(game, exe_path.name), [])
        pfx_root = prefix_path if lutris_is_prefix else compat_data
        if (lutris_env_extra is None
                and not (pfx_root / "pfx" / "user.reg").is_file()
                and not (pfx_root / "user.reg").is_file()):
            log_fn("Run EXE: initialising the prefix via Proton before the "
                   "plain-Wine launch …")
            try:
                from Utils.steam_finder import steam_client_installed
                subprocess.run(
                    # "run" (not "runinprefix"): triggers Proton's full
                    # first-time prefix setup, same as get_tool_prefix_env.
                    proton_run_command(proton_script, "run", "wineboot",
                                       "--init", env=env),
                    env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    # umu-run's first ever run on a Steam-less system
                    # downloads the Steam Linux Runtime - allow for it.
                    timeout=60 if steam_client_installed() else 600,
                )
            except Exception:
                pass
        run_tool_winetricks_style(
            proton_script, exe_path, pfx_root, log_fn=log_fn,
            extra_args=extra_args, extra_env=extra_env or None,
            label=exe_path.name)
        return

    runner_name = (proton_script.parent.parent.name
                   if lutris_env_extra is not None else proton_script.parent.name)
    if umu_bin is not None:
        runner_name = f"{runner_name} (umu)"
    log_fn(f"Run EXE: launching {exe_path.name} via {runner_name} ...")

    launches_game = (
        not proton_override_name
        and is_game_launch_exe(game, exe_path))
    # Epic builds need their EOS login arguments or they die at the main menu
    # ("Missing platform services") - Heroic passes them, so a direct launch
    # must too. Only when we ARE the launcher: mode=heroic never gets here.
    epic_args = epic_args_for_game(game, log_fn) if launches_game else []
    extra_args = extra_args + epic_args

    # Handler-declared default args (e.g. Cyberpunk's -modded). Prepended so
    # the user's own saved args stay last; skipped when already passed.
    # Asked per-exe: some args only fit some launch targets (Cyberpunk keeps
    # --launcher-skip away from the REDprelauncher Run entry).
    if launches_game:
        try:
            _declared = game.default_launch_args_for_exe(exe_path.name)
        except AttributeError:
            _declared = getattr(game, "default_launch_args", []) or []
        default_args = [a for a in _declared if a not in extra_args]
        if default_args:
            log_fn(f"Run EXE: adding default launch args: {' '.join(default_args)}")
            extra_args = default_args + extra_args

    # Apply launch-option env vars before building the command: when the
    # command gets wrapped in flatpak-spawn --host, proton_run_command
    # explicitly forwards launch/runtime values via --env= flags, so env must
    # be final here.
    launch_opts = load_launch_options(game, exe_path.name)
    if not launch_opts and launches_game:
        launch_opts = steam_launch_options_for_game(game, log_fn)
    env_updates, _ = parse_launch_options(launch_opts, [])
    if env_updates:
        env.update(env_updates)

    if umu_bin is not None:
        from Utils.lutris_finder import umu_run_command
        base_cmd = umu_run_command(
            umu_bin, str(exe_path), env=env,
            host_cwd=exe_path.parent) + extra_args
    else:
        # Anything that starts the game needs "waitforexitandrun" (the verb
        # Steam itself uses): it boots the steam.exe shim, without which
        # Steam-stub DRM fails SteamAPI init even with SteamAppId in env -
        # NVSE shows "Application load error 5:0000065434", skse64_loader
        # exits 245. Steam Input swapping to the game's controller profile is
        # correct here; it IS the game.
        # Tools keep "runinprefix" - no shim, so no "Running" status and no
        # profile swap, which on Steam Deck would lock the trackpad/OSK out of
        # the tool's UI. A never-booted prefix still needs "run" for Proton's
        # initial setup.
        if launches_game:
            verb = "waitforexitandrun"
        else:
            verb = ("runinprefix"
                    if (compat_data / "pfx" / "user.reg").is_file() else "run")
        base_cmd = proton_run_command(
            proton_script, verb, str(exe_path), env=env,
            host_cwd=exe_path.parent) + extra_args
        # proton_run_command routes game launches through Steam's runtime
        # container (as Steam does). Name it in the log: a container failure
        # looks nothing like a Proton failure, and the escape hatch has to be
        # discoverable from the session log.
        runtime_dir = next((Path(a).parent.name for a in base_cmd
                            if a.endswith("_v2-entry-point")), "")
        if runtime_dir:
            log_fn(f"Run EXE: using the Steam Linux Runtime container "
                   f"({runtime_dir}) - set AMM_STEAM_RUNTIME=0 to launch "
                   "Proton bare instead.")
    if not launch_opts:
        final_cmd = base_cmd
    else:
        _, final_cmd = parse_launch_options(launch_opts, base_cmd)

    # The Epic exchange code is a live credential - never write it to a log the
    # user may attach to a bug report.
    from Utils.heroic_finder import redact_epic_args
    log_fn(f"Run EXE:   cmd: {' '.join(redact_epic_args(final_cmd))}")
    _env_keys = (
        "WINE_D3D_CONFIG", "PROTON_USE_WINED3D", "WINEDLLOVERRIDES",
        "STEAM_COMPAT_DATA_PATH", "WINEDEBUG", "DXVK_HUD", "PROTON_LOG",
        "WINEPREFIX", "PROTONPATH", "GAMEID",
        # App context: a missing/zero SteamAppId is what DRM load errors report.
        "SteamAppId", "SteamGameId", "SteamOverlayGameId",
        "STEAM_COMPAT_APP_ID", "STEAM_COMPAT_INSTALL_PATH",
        "STEAM_COMPAT_MOUNTS",
    )
    _env_summary = " ".join(
        f"{k}={env.get(k)}" for k in _env_keys if env.get(k) is not None
    )
    if _env_summary:
        log_fn(f"Run EXE:   env: {_env_summary}")

    spawn_process_watched(final_cmd, env=env, cwd=exe_path.parent,
                          label=f"Run EXE {exe_path.name}", log_fn=log_fn)


def resolve_jar_prefix_env(jar_path: Path, game, log_fn=_noop_log):
    """Resolve (proton_script, compat_data, env) for running a .jar under Proton.

    Follows the same rule as regular exes (launch_exe_via_proton): with no
    Proton override the game's own prefix is used; with an override an isolated
    ``prefix_<Proton>/`` is created next to the jar. Returns None on failure
    (after logging why). First use of an isolated prefix runs wineboot - call
    from a worker thread.
    """
    from Utils.steam_finder import (
        find_any_installed_proton, list_installed_proton,
    )
    override = load_proton_override(game, jar_path.name)
    if not override:
        # Game prefix (no wineboot; already initialised by the game).
        return get_game_prefix_env(
            game, log_fn=lambda m: log_fn(f"Run JAR: {m}"),
            allow_runner_fallback=True)

    # Specific Proton → isolated prefix_<Proton>/ next to the jar.
    proton_script = find_any_installed_proton(override)
    if proton_script is None:
        override_lower = override.lower()
        for candidate in list_installed_proton():
            if candidate.parent.name.lower().startswith(override_lower):
                proton_script = candidate
                break
    if proton_script is None:
        log_fn(f"Run JAR: Proton override '{override}' not found.")
        return None
    prefix_dir = jar_path.parent / f"prefix_{proton_script.parent.name}"
    result = get_tool_prefix_env(
        jar_path, override, prefix_dir=prefix_dir,
        steam_id=effective_steam_id(game))
    return result


def launch_shell_script(sh_path: Path, game, log_fn=_noop_log) -> None:
    """Run a .sh entry natively on the host. Call from a worker thread.

    Linux-native games launch through a shell script (BepInEx's
    run_bepinex.sh sets the loader's env, then execs the game binary), so this
    never goes near Proton - handing a text file to wine would just fail. cwd
    is the script's own folder because those scripts resolve the game
    executable relative to themselves.
    """
    from Utils.xdg import host_env
    env = host_env()

    try:
        extra_args = shlex.split(load_exe_args(game, sh_path.name))
    except ValueError as e:
        log_fn(f"Run SH: invalid arguments - {e}")
        return

    cmd = [str(sh_path)]
    if not os.access(sh_path, os.X_OK):
        # Mod archives routinely lose the exec bit; restore it, and fall back
        # to an explicit interpreter when the file can't be chmod'ed.
        try:
            os.chmod(sh_path, os.stat(sh_path).st_mode | 0o111)
            log_fn(f"Run SH: {sh_path.name} was not executable - "
                   "added the executable bit.")
        except OSError as e:
            log_fn(f"Run SH: {sh_path.name} is not executable ({e}) - "
                   "running it with /bin/sh instead.")
            cmd = ["/bin/sh", str(sh_path)]

    launch_opts = load_launch_options(game, sh_path.name)
    if launch_opts:
        env_updates, cmd = parse_launch_options(launch_opts, cmd)
        if env_updates:
            env.update(env_updates)
        if not cmd:
            log_fn("Run SH: launch options produced no command.")
            return
    cmd += extra_args

    # A wrapper from Launch Options (gamemoderun, mangohud) that isn't
    # installed would otherwise fail as a bare Popen error.
    launcher = cmd[0]
    if os.sep not in launcher and shutil.which(launcher) is None:
        log_fn(f"Run SH error: '{launcher}' not found - check Launch Options.")
        launch_report.mark_failed(f"'{launcher}' not found")
        return

    log_fn(f"Run SH: launching {sh_path.name} ...")
    log_fn(f"Run SH:   cmd: {' '.join(cmd)}")
    spawn_process_watched(cmd, env=env, cwd=sh_path.parent,
                          label=f"Run SH ({sh_path.name})", log_fn=log_fn)


def launch_jar(jar_path: Path, game, log_fn=_noop_log) -> None:
    """Launch a .jar via a user-supplied Java command. Call from a worker thread.

    Java runtimes can't be run through ``proton run <jar>``, so the actual
    command comes from the exe's Launch Options / Launch arguments, which the
    user fills in themselves. ``%command%`` is substituted with the jar's path
    so a typical invocation looks like ``java -jar %command%``.

    Two runtime modes (per-exe, saved in exe_launch_mode.json):
      * host   - the jar path is the native Unix path and the command runs
                 directly on the host (its `java`); no Proton.
      * proton - the jar path is the prefix's Z: Wine path and the whole
                 command is wrapped in ``proton run`` so a Windows Java inside
                 a Proton prefix runs it. Which prefix follows the exe's Proton
                 override: none → the game's prefix; a specific version → an
                 isolated ``prefix_<Proton>/`` next to the jar.
    """
    runtime = load_jar_runtime(game, jar_path.name)
    launch_opts = load_launch_options(game, jar_path.name)
    args_str = load_exe_args(game, jar_path.name)

    if runtime == JAR_RUNTIME_PROTON:
        result = resolve_jar_prefix_env(jar_path, game, log_fn=log_fn)
        if result is None:
            return
        from Utils.steam_finder import proton_run_command
        from Utils.wine_paths import to_wine_path
        proton_script, compat_data, env = result
        jar_token = to_wine_path(jar_path)
        # Windows target: keep backslashes in paths and any file arguments the
        # user added.
        extra_args = split_preserving_backslash(args_str)
        # Always launch the bundled Windows Java on the jar; the user doesn't
        # have to type a command. We reference java.exe by its in-prefix C:
        # path (C:\java8\bin\java.exe) rather than a Z: path - a Z: path into
        # the prefix's own drive (and one with spaces, e.g. "Proton -
        # Experimental") is what made the launch fail.
        from Utils.jre_prefix import java_exe_in_prefix, JAVA_EXE_WIN
        java_native = java_exe_in_prefix(compat_data)
        if not java_native.is_file():
            log_fn("Run JAR: no Java in this prefix - click 'Install Java into "
                   "prefix' in the exe settings first (it installs into the "
                   "prefix the Proton version selects).")
            return
        jvm_cmd = [JAVA_EXE_WIN, "-jar", jar_token]
        # Launch Options are appended as extra flags. Steam-style %command% is
        # still honoured (it stands for the whole java command) for power users;
        # otherwise env vars are extracted and the rest appended after the jar.
        if launch_opts:
            env_updates, jvm_cmd = parse_launch_options(
                launch_opts, jvm_cmd, split_fn=split_preserving_backslash)
            if env_updates:
                env.update(env_updates)
        final_cmd = proton_run_command(
            proton_script, "run", *jvm_cmd, env=env,
            host_cwd=jar_path.parent) + extra_args
    else:  # host
        env = strip_appimage_env(os.environ.copy())
        jar_token = str(jar_path)
        try:
            extra_args = shlex.split(args_str)
        except ValueError as e:
            log_fn(f"Run JAR: invalid arguments - {e}")
            return
        opts_for_cmd = launch_opts or "java -jar %command%"
        env_updates, host_cmd = parse_launch_options(opts_for_cmd, [jar_token])
        if env_updates:
            env.update(env_updates)
        if not host_cmd:
            log_fn("Run JAR: launch options produced no command - add e.g. "
                   "'java -jar %command%' in Launch Options.")
            return
        # Fail loudly when the launcher (java) isn't installed - otherwise the
        # process errors out invisibly and nothing opens.
        launcher = host_cmd[0]
        if os.sep not in launcher and shutil.which(launcher) is None:
            in_flatpak = Path("/.flatpak-info").exists()
            if in_flatpak and shutil.which("flatpak-spawn"):
                # The host may have java even if the sandbox doesn't - try it.
                host_cmd = ["flatpak-spawn", "--host", *host_cmd]
                log_fn(f"Run JAR: '{launcher}' not in sandbox - forwarding to host.")
            else:
                log_fn(f"Run JAR error: '{launcher}' not found. Install a Java "
                       "runtime (e.g. `sudo pacman -S jre-openjdk` / your "
                       "distro's JRE) or set the full path in Launch Options.")
                return
        final_cmd = host_cmd + extra_args

    log_fn(f"Run JAR: launching {jar_path.name} ({runtime}) ...")
    log_fn(f"Run JAR:   cmd: {' '.join(final_cmd)}")
    try:
        proc = subprocess.Popen(
            final_cmd,
            env=env,
            cwd=str(jar_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except Exception as e:
        log_fn(f"Run JAR error: {e}")
        launch_report.mark_failed(f"Run JAR error: {e}")
        return
    import time as _time
    rep = launch_report.current()
    started = _time.monotonic()
    if rep is not None:
        rep.mark_spawned()

    # Stream the launcher's output to the log so failures (missing java.exe in
    # the prefix, a jar that crashes on start) are visible instead of silent.
    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log_fn(f"Run JAR: {line}")
        rc = proc.wait()
        launch_report.mark_exit(rep, started, rc, f"Run JAR {jar_path.name}")
        if rc != 0:
            log_fn(f"Run JAR: {jar_path.name} exited with code {rc}")

    import threading
    threading.Thread(target=_pump, daemon=True).start()
