"""Grant flatpak-sandboxed game launchers access to our symlink targets.

Games launched by a flatpak launcher (Heroic on Steam Deck/Bazzite being the
common case) run inside that launcher's bubblewrap sandbox.  The sandbox only
mounts the paths listed in the flatpak's manifest and user overrides, so a
symlink our deploy creates inside the game folder or wine prefix dangles when
its target — the mod staging folder or the profile dir (ini files) — is not
one of those paths.  The game then reports the file as missing even though
the link is perfectly valid on the host (GH#275: symlinked nvse_1_4.dll and
FalloutCustom.ini unreadable under flatpak Heroic while hardlinks work).

`flatpak override --user <app> --filesystem=<path>` makes a target visible
inside the sandbox; this module applies that override automatically at
deploy time.  Grants are read-write because Bethesda games write their ini
files back through the My Games symlinks.  The override is persisted by
flatpak (~/.local/share/flatpak/overrides/<app>), so after the first grant
the coverage check short-circuits and nothing is spawned again.

Kill switch: AMM_FLATPAK_OVERRIDE=0.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

LogFn = Callable[[str], None]

HEROIC_FLATPAK_ID = "com.heroicgameslauncher.hgl"
STEAM_FLATPAK_ID = "com.valvesoftware.Steam"
LUTRIS_FLATPAK_ID = "net.lutris.Lutris"

# Friendly launcher names for user-facing messages.
_APP_NAMES = {
    HEROIC_FLATPAK_ID: "Heroic",
    STEAM_FLATPAK_ID: "Steam",
    LUTRIS_FLATPAK_ID: "Lutris",
}

_HOME = Path.home()


# ---------------------------------------------------------------------------
# Detection — which flatpak sandbox (if any) will the game run inside?
# ---------------------------------------------------------------------------

def _flatpak_app_owning_path(path: Path) -> Optional[str]:
    """Flatpak app id whose sandbox data dir (~/.var/app/<id>) contains *path*."""
    try:
        rel = path.relative_to(_HOME / ".var" / "app")
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _heroic_flatpak_present() -> bool:
    return (_HOME / ".var" / "app" / HEROIC_FLATPAK_ID).is_dir()


def _steam_flatpak_owns_path(path: Path) -> bool:
    """True when *path* lies inside a Steam library listed by the FLATPAK
    Steam install.

    Games in the flatpak Steam's default library live under ~/.var/app and
    are caught by the path check alone; this covers its EXTERNAL libraries
    (SD card, second drive) — those games still run inside the Steam sandbox
    even though their files don't.  The flatpak's own libraryfolders.vdf is
    the authority on which libraries belong to it.
    """
    steam_root = (_HOME / ".var" / "app" / STEAM_FLATPAK_ID /
                  ".local" / "share" / "Steam")
    if not steam_root.is_dir():
        return False
    try:
        from Utils.steam_finder import parse_vdf_libraries, _VDF_FILENAMES
    except Exception:
        return False
    try:
        path = Path(path).resolve()
    except OSError:
        return False
    for parent in (steam_root / "steamapps", steam_root / "config", steam_root):
        for name in _VDF_FILENAMES:
            vdf = parent / name
            if not vdf.is_file():
                continue
            for common in parse_vdf_libraries(vdf):
                try:
                    path.relative_to(Path(common).resolve())
                    return True
                except (ValueError, OSError):
                    pass
    return False


def sandbox_app_for_game(game, game_root: Optional[Path]) -> Optional[str]:
    """Return the flatpak app id that will sandbox this game at runtime.

    Ways a game ends up sandboxed:
      * its files live under ~/.var/app/<id>/ (launcher keeps games in its
        own data dir — flatpak Steam's default library included) — the id is
        read straight off the path;
      * it sits in one of the flatpak Steam's EXTERNAL libraries (SD card /
        second drive) — files outside ~/.var/app, process still sandboxed;
      * it is a Heroic- or Lutris-managed install and that launcher's
        flatpak exists — same shape as the Steam external-library case.
    If the user also has the native launcher installed and that one actually
    starts the game, the extra override is harmless.
    """
    if game_root:
        app = _flatpak_app_owning_path(Path(game_root))
        if app:
            return app
        if _steam_flatpak_owns_path(Path(game_root)):
            return STEAM_FLATPAK_ID
    if _heroic_flatpak_present():
        try:
            from Utils.exe_launch import game_is_heroic_install
            if game_is_heroic_install(game):
                return HEROIC_FLATPAK_ID
        except Exception:
            pass
    # Lutris flatpak grants --filesystem=home by default, so its games are
    # usually fine — but the baseline check in ensure_symlink_target_access
    # handles that (silent no-op); this covers staging outside home (/mnt)
    # and Flatseal-tightened setups.
    if (_HOME / ".var" / "app" / LUTRIS_FLATPAK_ID).is_dir():
        try:
            from Utils.exe_launch import game_is_lutris_install
            if game_is_lutris_install(game):
                return LUTRIS_FLATPAK_ID
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Override-file coverage check
# ---------------------------------------------------------------------------

def _override_file(app_id: str) -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(_HOME / ".local" / "share")
    return Path(data_home) / "flatpak" / "overrides" / app_id


def _read_override_text(app_id: str) -> str:
    """Current user-override keyfile content for *app_id* ('' if none).

    Flatpak masks its own data dir (~/.local/share/flatpak) inside every
    sandbox — even with --filesystem=home — so from inside our flatpak the
    file can only be read via the host CLI.
    """
    if Path("/.flatpak-info").is_file():
        try:
            res = subprocess.run(
                _host_cmd(["flatpak", "override", "--user", "--show", app_id]),
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return res.stdout if res.returncode == 0 else ""
    try:
        return _override_file(app_id).read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_filesystems(text: str) -> "tuple[set[str], list[Path]]":
    """Parse `filesystems=` entries out of a flatpak keyfile.

    Returns (tokens, paths): tokens are the broad grants 'home' and 'host';
    paths are plain absolute entries.  xdg-* specials and negations are
    ignored (a missed match just re-runs the idempotent `flatpak override`).
    """
    tokens: set[str] = set()
    paths: list[Path] = []
    in_context = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_context = line == "[Context]"
            continue
        if not in_context or not line.startswith("filesystems="):
            continue
        for entry in line.split("=", 1)[1].split(";"):
            entry = entry.strip()
            if not entry or entry.startswith("!"):
                continue
            # Strip access suffix (:ro / :rw / :create)
            for suffix in (":ro", ":rw", ":create"):
                if entry.endswith(suffix):
                    entry = entry[: -len(suffix)]
                    break
            if entry in ("home", "host"):
                tokens.add(entry)
                continue
            if entry.startswith("~"):
                entry = str(_HOME) + entry[1:]
            if entry.startswith("/"):
                paths.append(Path(entry))
    return tokens, paths


def _granted_filesystems(app_id: str) -> "tuple[set[str], list[Path]]":
    """Filesystem grants from the app's user override file."""
    return _parse_filesystems(_read_override_text(app_id))


_baseline_cache: "dict[str, tuple[set[str], list[Path]]]" = {}


def _baseline_filesystems(app_id: str) -> "tuple[set[str], list[Path]]":
    """Filesystem grants baked into the app's own manifest.

    Needed so launchers that already ship broad access (Lutris grants
    `home`) don't get a pointless override + restart prompt.  Cached per
    process — manifest permissions only change on app updates.
    """
    if app_id in _baseline_cache:
        return _baseline_cache[app_id]
    try:
        res = subprocess.run(
            _host_cmd(["flatpak", "info", "--show-permissions", app_id]),
            capture_output=True, text=True, timeout=15,
        )
        text = res.stdout if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    parsed = _parse_filesystems(text)
    _baseline_cache[app_id] = parsed
    return parsed


def _covered(path: Path, granted: "Iterable[Path]",
             tokens: "set[str]" = frozenset()) -> bool:
    if "host" in tokens:
        return True
    if "home" in tokens and (path == _HOME or _HOME in path.parents):
        return True
    for g in granted:
        if path == g or g in path.parents:
            return True
    return False


# ---------------------------------------------------------------------------
# Grant
# ---------------------------------------------------------------------------

def _host_cmd(cmd: "list[str]") -> "list[str]":
    """Prefix with flatpak-spawn when we ourselves run inside a flatpak."""
    if Path("/.flatpak-info").is_file():
        return ["flatpak-spawn", "--host", "--directory=/", *cmd]
    return cmd


def _grant_paths(app_id: str, paths: "list[Path]", log_fn: LogFn) -> bool:
    cmd = ["flatpak", "override", "--user", app_id]
    cmd += [f"--filesystem={p}" for p in paths]
    try:
        res = subprocess.run(
            _host_cmd(cmd), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_fn(f"  WARN: flatpak override failed to run: {exc}")
        return False
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        log_fn(f"  WARN: flatpak override failed ({err}) — grant access "
               f"manually: flatpak override --user {app_id} " +
               " ".join(f"--filesystem='{p}'" for p in paths))
        return False
    return True


def _wanted_roots(staging: Optional[Path],
                  profile_dir: Optional[Path]) -> "list[Path]":
    """Symlink-target roots the sandbox must see, deduped by ancestry.

    The shared staging path is `<staging_root>/mods`; granting its parent
    also covers siblings like overwrite/.  Never widen to $HOME itself.

    Profile Group: the group's staging is a per-mod symlink farm resolving
    into MEMBER profile dirs — granting only the group dir would leave every
    deployed symlink dangling inside the sandbox. Widen to the profiles/
    root (still far narrower than $HOME) so all members are covered.
    """
    candidates = [staging, profile_dir]
    try:
        if profile_dir is not None:
            from Utils.profile_groups import is_group
            pdir = Path(profile_dir).expanduser()
            if is_group(pdir) and pdir.parent.name == "profiles":
                candidates.append(pdir.parent)
    except Exception:
        pass
    wanted: list[Path] = []
    for p in candidates:
        if not p:
            continue
        p = Path(p).expanduser()
        if p.name == "mods" and p.parent not in (_HOME, Path("/")):
            p = p.parent
        if p in (_HOME, Path("/")) or not p.is_absolute():
            continue
        if not _covered(p, wanted):
            wanted = [w for w in wanted if not _covered(w, [p])]
            wanted.append(p)
    return wanted


def ensure_symlink_target_access(
    game,
    *,
    game_root: Optional[Path],
    staging: Optional[Path],
    profile_dir: Optional[Path],
    log_fn: LogFn,
) -> None:
    """Make deploy symlink targets visible to the game's flatpak sandbox.

    No-op when the game is not sandbox-launched, the paths are already
    granted, or AMM_FLATPAK_OVERRIDE=0.  Never raises.
    """
    if os.environ.get("AMM_FLATPAK_OVERRIDE", "1") == "0":
        return
    try:
        app_id = sandbox_app_for_game(game, game_root)
        if not app_id:
            return
        wanted = _wanted_roots(staging, profile_dir)
        tokens, granted = _granted_filesystems(app_id)
        missing = [p for p in wanted if not _covered(p, granted, tokens)]
        if missing:
            # The manifest itself may already grant enough (Lutris ships
            # --filesystem=home) — only override what neither layer covers.
            btokens, bgranted = _baseline_filesystems(app_id)
            missing = [p for p in missing
                       if not _covered(p, bgranted, btokens)]
        if not missing:
            return
        if _grant_paths(app_id, missing, log_fn):
            log_fn(f"  Granted the {app_id} flatpak access to "
                   f"{', '.join(str(p) for p in missing)} so symlinked mod "
                   f"files resolve inside its sandbox.")
            log_fn(f"  NOTE: restart the launcher ({app_id}) for the new "
                   f"access to take effect.")
            _notify_restart_needed(app_id, missing)
    except Exception as exc:
        log_fn(f"  WARN: flatpak sandbox access check failed: {exc}")


def _notify_restart_needed(app_id: str, granted: "list[Path]") -> None:
    """Popup (when a GUI is attached) telling the user to restart the
    launcher — the sandbox only picks the new grants up on a fresh start."""
    launcher = _APP_NAMES.get(app_id, app_id)
    try:
        from Utils import ui_hooks
        paths = "\n".join(f"  {p}" for p in granted)
        ui_hooks.warn(
            f"Restart {launcher} to finish mod setup",
            (f"The {launcher} flatpak was granted access to:\n\n{paths}\n\n"
             f"so the mod files deployed as symlinks are visible inside its "
             f"sandbox.\n\nFully close and restart {launcher} before "
             f"launching the game — until then the game will not see these "
             f"mod files."),
            height=300,
        )
    except Exception:
        pass  # a failed popup must never break the deploy
