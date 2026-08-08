"""
GUI-neutral core of the native-Linux BodySlide / Outfit Studio wizard.

Unlike the Proton wizard (Utils/bodyslide_tools.py) this runs a Linux build
straight on the host, so there is no prefix, no registry seeding and no
Config.xml rewriting: the fork exposes BSOS_* environment variables that win
over the stored configuration on every launch, so the wizard just downloads
the build, deploys, and runs it with the right env.

Fork: https://github.com/ChrisDKN/BodySlide-and-Outfit-Studio-Appimage

We ship the **portable tarball**, not the AppImage. The tarball is a plain
relocatable directory with its own bundled loader and libc, so it needs no
FUSE mount and - unlike an AppImage - runs unchanged inside our own flatpak
sandbox, with no flatpak-spawn --host hop. The tarball also carries a launcher
script per program (``<root>/BodySlide``, ``<root>/OutfitStudio``) that sets up
sharun, PATH and BSOS_BINDIR.

The variables the fork reads (see its GameUtil::ApplyEnvironmentOverrides and
ProjectUtil::GetDataDir):
  BSOS_TARGET_GAME       game name as it appears in GameUtil::TargetGames
                         ("SkyrimSpecialEdition", "Fallout4"; also accepts the
                         raw index). An unknown value is ignored by the tool
                         rather than silently selecting the wrong game.
  BSOS_GAME_DATA_PATH    the deployed Data folder.
  BSOS_OUTPUT_DATA_PATH  where built meshes are written - the output-capture
                         mod in staging, so the build lands in the mod list
                         instead of loose in the game folder.
  BSOS_APPDIR            writable data dir holding Config.xml / *.xml / logs.

Slider data is NOT passed in: with BSOS_APPDIR holding no SliderSets folder,
the fork's GetProjectPath() falls back to <GameData>/CalienteTools/BodySlide,
which is exactly where deployed BodySlide mods land. That is why BSOS_APPDIR
must stay free of a SliderSets directory - its presence would make the tool
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
    "https://api.github.com/repos/ChrisDKN/BodySlide-and-Outfit-Studio-Appimage"
    "/releases/latest"
)
REPO_URL = "https://github.com/ChrisDKN/BodySlide-and-Outfit-Studio-Appimage"

# tool key → (display name, launcher basename, default output mod name)
TOOLS: dict[str, tuple[str, str, str]] = {
    "bodyslide":    ("BodySlide", "BodySlide", "BodySlide_files"),
    "outfitstudio": ("Outfit Studio", "OutfitStudio", "OutfitStudio_files"),
}

# Seeded into a per-profile BSOS_APPDIR on first use - see seed_data_dir().
_SEED_XML = ("Config.xml", "BodySlide.xml", "OutfitStudio.xml",
             "BuildSelection.xml", "RefTemplates.xml")
_SEED_LINKS = ("res", "lang")


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Install location
# ---------------------------------------------------------------------------
#
# Shared across games rather than per-game Applications/: the tree is a
# self-contained ~170 MB bundle with no per-game state (all of that travels in
# BSOS_APPDIR), so a copy per game would only duplicate downloads and updates.

def tools_dir() -> Path:
    """~/.config/AmethystModManager/Tools/BodySlide-Linux/"""
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "Tools" / "BodySlide-Linux"


def install_root() -> Path:
    """The extracted tarball tree."""
    return tools_dir() / "current"


def version_file() -> Path:
    return tools_dir() / "version.txt"


def launcher_path(program: str) -> Path:
    """The tarball's launcher script for *program* ("BodySlide"/"OutfitStudio")."""
    return install_root() / program


def is_installed() -> bool:
    return all(os.access(launcher_path(p), os.X_OK)
               for _n, p, _o in TOOLS.values())


def installed_version() -> str | None:
    """Release tag of the installed build, or None when not installed."""
    if not is_installed():
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
    """Return (tag, download_url) for the newest x86_64 portable tarball.

    Deliberately not Utils.wizard_archives.fetch_latest_github_asset: that one
    only accepts ARCHIVE_EXTS (.zip/.7z/…), and would also have to be taught to
    skip the AppImage and .zsync assets published alongside the tarball.
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
        name = asset.get("name", "").lower()
        if name.endswith(".tar.zst") and "x86_64" in name:
            return tag, asset["browser_download_url"]
    raise RuntimeError(
        f"No x86_64 .tar.zst asset in the latest release ({tag}).")


def install_release(url: str, tag: str, *, reporthook=None,
                    log_fn: Callable[[str], None] = _noop) -> Path:
    """Download and extract *url*, replacing any existing install.

    Extraction goes to a staging directory that only replaces the live tree
    once it is complete, so a failed download or extraction never leaves a
    half-populated bundle behind. The old tree is removed rather than merged:
    a new release renames libraries, and leftovers from the previous version
    would sit in lib/ shadowing nothing but wasting space at best.
    """
    import tempfile

    from Utils.ca_bundle import download_file
    from Utils.wizard_archives import extract_to_dir

    root = install_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.with_name(root.name + ".new")
    old = root.with_name(root.name + ".old")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)

    with tempfile.TemporaryDirectory(dir=str(root.parent)) as tmpdir:
        archive = Path(tmpdir) / "bodyslide.tar.zst"
        download_file(url, archive, reporthook=reporthook)
        log_fn(f"extracting {archive.name}…")
        unpacked = Path(tmpdir) / "x"
        unpacked.mkdir()
        extract_to_dir(archive, unpacked)

        # The tarball wraps everything in one versioned directory; move that
        # up so the launcher always lives at a stable path.
        entries = [e for e in unpacked.iterdir() if e.name != "__MACOSX"]
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked
        src.rename(staging)

    missing = [p for _n, p, _o in TOOLS.values()
               if not os.access(staging / p, os.X_OK)]
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(
            "Launcher(s) missing from the archive: " + ", ".join(missing))

    if root.exists():
        root.rename(old)
    staging.rename(root)
    shutil.rmtree(old, ignore_errors=True)

    try:
        version_file().write_text(tag + "\n", encoding="utf-8")
    except OSError as exc:
        log_fn(f"could not record version ({exc})")
    log_fn(f"installed {tag} → {root}")
    return root


# ---------------------------------------------------------------------------
# Per-game / per-profile environment
# ---------------------------------------------------------------------------

def safe_name(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw or "")


def target_game(game: "BaseGame") -> str | None:
    """The GameUtil::TargetGames name for *game*, or None when unsupported.

    Reuses the Proton wizard's mapping table - its tag strings are exactly the
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


def seed_data_dir(app_dir: Path, root: Path,
                  log_fn: Callable[[str], None] = _noop) -> None:
    """Make *app_dir* usable as BSOS_APPDIR for the install at *root*.

    The tarball's own launcher only defaults BSOS_APPDIR to the tarball root,
    which is already populated; it does nothing when a caller points the
    variable elsewhere. But the programs resolve res/ and lang/ RELATIVE TO
    the data dir - wx loads res/xrc/BodySlide.xrc from there - so an
    un-seeded data dir fails at startup with "Cannot open resources file".
    The AppImage's AppRun does this seeding; for the tarball it is ours to do.

    res/ and lang/ are symlinked (never copied) so an update to *root* is
    picked up immediately; the XML defaults are copied once and then owned by
    the program, which rewrites them on exit.
    """
    app_dir.mkdir(parents=True, exist_ok=True)

    for name in _SEED_LINKS:
        link, target = app_dir / name, root / name
        if not target.exists():
            log_fn(f"WARNING: {target} missing from the install.")
            continue
        # Refresh dangling/stale links (the install path can change across
        # updates); a real directory in their place is assumed deliberate.
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
            link.unlink()
        elif link.exists():
            continue
        link.symlink_to(target)

    for name in _SEED_XML:
        dst, src = app_dir / name, root / name
        if dst.exists() or not src.is_file():
            continue
        try:
            shutil.copy2(src, dst)
            dst.chmod(dst.stat().st_mode | 0o200)
        except OSError as exc:
            log_fn(f"could not seed {name} ({exc})")


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
    seed_data_dir(app_dir, install_root(), log_fn=log_fn)
    # An empty SliderSets here would make GetProjectPath() return this folder
    # and stop looking, so the tool would list no outfits at all. It should
    # never exist, but a stray one is cheap to catch and impossible to debug
    # from the UI, so say so in the log rather than silently listing nothing.
    if (app_dir / "SliderSets").is_dir():
        log_fn(f"WARNING: {app_dir}/SliderSets exists - outfit discovery will "
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

# GTK chatter the tool emits by the dozen per window resize ("Negative content
# width …", host desktop modules the bundled GTK can't load). It says nothing
# about BodySlide and would bury the lines that matter in the app log.
_GTK_NOISE = re.compile(r"\b(Gtk|Gdk|GLib|GLib-GObject)-(WARNING|Message|CRITICAL)\b")


def run_logged(program: str, env: dict, *,
               log_fn: Callable[[str], None] = _noop,
               label: str = "BodySlide") -> int:
    """Run the tarball launcher for *program*, streaming output to *log_fn*.

    Blocks until the tool exits - call from a worker thread. No flatpak-spawn
    hop: the bundle carries its own loader and libc, so it runs inside our
    sandbox as-is.
    """
    launcher = launcher_path(program)
    home = os.path.expanduser("~")
    cwd = home if os.path.isdir(home) else "/"

    log_fn(f"{label}: launching {launcher}")
    try:
        proc = subprocess.Popen(
            [str(launcher)],
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch - {exc}")
        raise

    assert proc.stdout is not None
    suppressed = 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if _GTK_NOISE.search(line):
            suppressed += 1
        else:
            log_fn(f"{label}: {line}")
    rc = proc.wait()
    if suppressed:
        log_fn(f"{label}: suppressed {suppressed} GTK warning line(s).")
    if rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc
