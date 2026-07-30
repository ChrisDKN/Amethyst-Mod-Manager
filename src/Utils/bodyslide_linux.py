"""
GUI-neutral core of the native-Linux BodySlide / Outfit Studio wizard.

Unlike the Proton wizard (Utils/bodyslide_tools.py) this runs a Linux build
straight on the host, so there is no prefix, no registry seeding and no
Config.xml rewriting: the fork exposes BSOS_* environment variables that win
over the stored configuration on every launch, so the wizard just downloads
the AppImage, deploys, and runs it with the right env.

Fork: https://github.com/ChrisDKN/BodySlide-and-Outfit-Studio

The variables the fork reads (see its GameUtil::ApplyEnvironmentOverrides and
ProjectUtil::GetDataDir):
  BSOS_TARGET_GAME       game name as it appears in GameUtil::TargetGames
                         ("SkyrimSpecialEdition", "Fallout4"; also accepts the
                         raw index). An unknown value is ignored by the tool
                         rather than silently selecting the wrong game.
  BSOS_GAME_DATA_PATH    the deployed Data folder.
  BSOS_OUTPUT_DATA_PATH  where built meshes are written — the output-capture
                         mod in staging, so the build lands in the mod list
                         instead of loose in the game folder.
  BSOS_APPDIR            writable data dir holding Config.xml / *.xml / logs.
                         The AppImage's AppRun seeds it from its own defaults
                         on first run and symlinks res/ + lang/ into it.

Slider data is NOT passed in: with BSOS_APPDIR holding no SliderSets folder,
the fork's GetProjectPath() falls back to <GameData>/CalienteTools/BodySlide,
which is exactly where deployed BodySlide mods land. That is why BSOS_APPDIR
must stay free of a SliderSets directory — its presence would make the tool
treat the data dir as the project dir and list nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

GITHUB_API_URL = (
    "https://api.github.com/repos/ChrisDKN/BodySlide-and-Outfit-Studio"
    "/releases/latest"
)
REPO_URL = "https://github.com/ChrisDKN/BodySlide-and-Outfit-Studio"
APPIMAGE_NAME = "BodySlide-and-Outfit-Studio-x86_64.AppImage"

# tool key → (display name, AppRun program argument, default output mod name)
TOOLS: dict[str, tuple[str, str, str]] = {
    "bodyslide":    ("BodySlide", "BodySlide", "BodySlide_files"),
    "outfitstudio": ("Outfit Studio", "OutfitStudio", "OutfitStudio_files"),
}


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Install location
# ---------------------------------------------------------------------------
#
# Shared across games rather than per-game Applications/: the AppImage is a
# ~46 MB self-contained binary with no per-game state (all of that travels in
# BSOS_APPDIR), so a copy per game would only duplicate downloads and update
# checks.

def tools_dir() -> Path:
    """~/.config/AmethystModManager/Tools/BodySlide-Linux/"""
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "Tools" / "BodySlide-Linux"


def appimage_path() -> Path:
    return tools_dir() / APPIMAGE_NAME


def version_file() -> Path:
    return tools_dir() / "version.txt"


def is_installed() -> bool:
    p = appimage_path()
    return p.is_file() and os.access(p, os.X_OK)


def installed_version() -> str | None:
    """Release tag of the installed AppImage, or None when not installed."""
    if not appimage_path().is_file():
        return None
    try:
        tag = version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tag or None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_latest_release() -> tuple[str, str]:
    """Return (tag, download_url) for the newest x86_64 AppImage asset.

    Deliberately not Utils.wizard_archives.fetch_latest_github_asset: that one
    only accepts ARCHIVE_EXTS (.zip/.7z/…), and the asset here is a bare
    AppImage. The .zsync sidecar published alongside it must be skipped.
    """
    import json
    import urllib.request

    from Utils.ca_bundle import get_ssl_context

    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15,
                                context=get_ssl_context()) as resp:
        data = json.loads(resp.read().decode())

    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".appimage") and "x86_64" in name.lower():
            return tag, asset["browser_download_url"]
    raise RuntimeError(
        f"No x86_64 AppImage asset in the latest release ({tag}).")


def download_appimage(url: str, tag: str, *, reporthook=None,
                      log_fn: Callable[[str], None] = _noop) -> Path:
    """Download *url* to :func:`appimage_path`, make it executable and record
    *tag*. Downloads to a temp name and renames, so a failed download never
    leaves a half-written AppImage in place of a working one."""
    from Utils.ca_bundle import download_file

    dest = appimage_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(dest.name + ".new")

    download_file(url, staged, reporthook=reporthook)
    staged.chmod(0o755)
    staged.replace(dest)
    try:
        version_file().write_text(tag + "\n", encoding="utf-8")
    except OSError as exc:
        log_fn(f"could not record version ({exc})")
    log_fn(f"installed {tag} → {dest}")
    return dest


# ---------------------------------------------------------------------------
# Per-game / per-profile environment
# ---------------------------------------------------------------------------

def safe_name(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw or "")


def target_game(game: "BaseGame") -> str | None:
    """The GameUtil::TargetGames name for *game*, or None when unsupported.

    Reuses the Proton wizard's mapping table — its tag strings are exactly the
    names the fork matches BSOS_TARGET_GAME against.
    """
    from Utils.bodyslide_tools import bodyslide_game
    mapping = bodyslide_game(game)
    return None if mapping is None else mapping[0]


def data_dir(game: "BaseGame", profile: str) -> Path:
    """Writable BSOS_APPDIR for this game+profile.

    Per profile because BuildSelection.xml (which outfits are ticked) belongs
    to a load order, not to the machine. Lives under the profile root's
    Applications/ folder, which the filemap never scans.
    """
    from Utils.xedit_tools import applications_dir
    return applications_dir(game, "BodySlide-Linux") / f"data_{safe_name(profile)}"


def build_env(game: "BaseGame", profile: str, output_dir: Path, *,
              base: "dict | None" = None,
              log_fn: Callable[[str], None] = _noop) -> dict:
    """Environment for a native launch: host env + the BSOS_* overrides.

    Starts from Utils.xdg.host_env() so a launch from inside our own AppImage
    doesn't hand the child our bundled loader/GTK paths (see
    Utils/appimage_env.py).
    """
    from Utils.xdg import host_env

    env = dict(base) if base is not None else host_env()

    app_dir = data_dir(game, profile)
    app_dir.mkdir(parents=True, exist_ok=True)
    # An empty SliderSets here would make GetProjectPath() return this folder
    # and stop looking, so the tool would list no outfits at all. It should
    # never exist, but a stray one is cheap to catch and impossible to debug
    # from the UI, so say so in the log rather than silently listing nothing.
    if (app_dir / "SliderSets").is_dir():
        log_fn(f"WARNING: {app_dir}/SliderSets exists — outfit discovery will "
               "use that folder instead of the deployed Data folder.")
    env["BSOS_APPDIR"] = str(app_dir)

    name = target_game(game)
    if name:
        env["BSOS_TARGET_GAME"] = name
    else:
        log_fn(f"no BodySlide target game for {game.name}; "
               "the tool will keep its configured game.")

    data_path = game.get_mod_data_path()
    if data_path is not None:
        env["BSOS_GAME_DATA_PATH"] = str(data_path)

    env["BSOS_OUTPUT_DATA_PATH"] = str(output_dir)
    return env


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def _in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _fuse_available() -> bool:
    return bool(shutil.which("fusermount3") or shutil.which("fusermount"))


def launch_command(appimage: Path, program: str, env: dict,
                   host_cwd: str) -> list[str]:
    """Command that runs *program* out of the AppImage.

    The AppRun takes the program name as its first argument (BodySlide is the
    default; "OutfitStudio" selects the other one).

    Inside our own flatpak sandbox the AppImage cannot be mounted (no FUSE in
    the runtime), so the launch is forwarded to the host — flatpak-spawn does
    not inherit the environment, so every var that differs from our own is
    re-exported with --env=, the same way Utils/steam_finder does it for
    Proton.
    """
    cmd = [str(appimage), program]
    if _in_flatpak() and shutil.which("flatpak-spawn"):
        fwd = [f"--env={k}={v}" for k, v in env.items()
               if os.environ.get(k) != v]
        cmd = ["flatpak-spawn", "--host", f"--directory={host_cwd}",
               *fwd, *cmd]
    return cmd


_FUSE_HINT = re.compile(r"fuse|dlopen|libfuse|mount", re.IGNORECASE)

# GTK chatter the tool emits by the dozen per window resize ("Negative content
# width …", host desktop modules the bundled GTK can't load). It says nothing
# about BodySlide and would bury the lines that matter in the app log, so it is
# dropped there — the retry buffer still sees it.
_GTK_NOISE = re.compile(r"\b(Gtk|Gdk|GLib|GLib-GObject)-(WARNING|Message|CRITICAL)\b")


def run_logged(appimage: Path, program: str, env: dict, *,
               log_fn: Callable[[str], None] = _noop,
               label: str = "BodySlide") -> int:
    """Run the AppImage, streaming its output to *log_fn*; returns the exit
    code. Blocks until the tool exits — call from a worker thread.

    Retries once with APPIMAGE_EXTRACT_AND_RUN=1 when the runtime could not
    mount itself: a host without FUSE (or a restricted sandbox) fails before
    the program ever starts, and self-extraction is the documented fallback.
    """
    home = os.path.expanduser("~")
    host_cwd = home if os.path.isdir(home) else "/"

    run_env = dict(env)
    if not _fuse_available():
        log_fn("no fusermount on PATH — running the AppImage self-extracted.")
        run_env["APPIMAGE_EXTRACT_AND_RUN"] = "1"

    rc, output = _run_once(appimage, program, run_env, host_cwd,
                           log_fn=log_fn, label=label)
    if rc != 0 and "APPIMAGE_EXTRACT_AND_RUN" not in run_env \
            and _FUSE_HINT.search(output):
        log_fn(f"{label}: AppImage could not be mounted — retrying "
               "self-extracted.")
        run_env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        rc, _ = _run_once(appimage, program, run_env, host_cwd,
                          log_fn=log_fn, label=label)
    return rc


def _run_once(appimage: Path, program: str, env: dict, host_cwd: str, *,
              log_fn: Callable[[str], None], label: str) -> tuple[int, str]:
    cmd = launch_command(appimage, program, env, host_cwd)
    log_fn(f"{label}: launching {' '.join(cmd[-2:])}")
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=host_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch — {exc}")
        raise

    assert proc.stdout is not None
    tail: list[str] = []
    suppressed = 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if _GTK_NOISE.search(line):
            suppressed += 1
        else:
            log_fn(f"{label}: {line}")
        tail.append(line)
        # Only the retry decision needs the text; keep the window small so a
        # chatty GL driver can't grow this without bound.
        if len(tail) > 40:
            del tail[0]
    rc = proc.wait()
    if suppressed:
        log_fn(f"{label}: suppressed {suppressed} GTK warning line(s).")
    if rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc, "\n".join(tail)
