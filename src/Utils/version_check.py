"""
App update check: fetch latest version from repo and compare.
Used by the app shell. No dependency on any gui modules.
"""

import os
import re
import shlex
import subprocess

from Utils.gh_cache import fetch_text as _gh_fetch_text

_APP_UPDATE_RELEASES_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases/latest"
_APP_UPDATE_RELEASES_LIST_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases?per_page=20"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"
_APP_UPDATE_INSTALLER_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh"
_APP_UPDATE_FLATPAK_BUNDLE_URL = (
    "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases/download/"
    "v{tag}/AmethystModManager.flatpak"
)
_APP_ID = "io.github.Amethyst.ModManager"

# Hosted Flatpak remote (GitHub Pages). Adding this remote lets the OS handle
# updates natively (`flatpak update`, GNOME Software, Discover) with delta
# downloads. `stable` and `beta` are the two OSTree branches published to it.
_FLATPAK_REMOTE_NAME = "modmanager-origin"
_FLATPAK_REMOTE_REPO_URL = "https://chrisdkn.github.io/Amethyst-Mod-Manager/repo/"
_FLATPAK_REMOTE_FILE_URL = (
    "https://chrisdkn.github.io/Amethyst-Mod-Manager/amethyst.flatpakrepo"
)

_AUR_API_URL = "https://aur.archlinux.org/rpc/v5/info/amethyst-mod-manager"
_AUR_PACKAGE_URL = "https://aur.archlinux.org/packages/amethyst-mod-manager"


def is_appimage() -> bool:
    """Return True if we are running inside an AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def is_flatpak() -> bool:
    """Return True if we are running as the Amethyst flatpak.

    NB: match FLATPAK_ID against OUR id, not "/.flatpak-info exists" —
    running from source inside another flatpak (e.g. a flatpak VS Code
    terminal) sandboxes us under that host app, so the file test would
    wrongly steer from-source sessions to the flatpak update path.
    """
    return os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager"


def _parse_version(s: str) -> tuple:
    """Parse a version string into a sortable tuple following SemVer pre-release rules.

    '1.3.1'         -> ((1, 3, 1), (1,))                # stable sorts last
    '1.3.1-beta.1'  -> ((1, 3, 1), (0, 'beta', 1))      # pre-release sorts before stable
    """
    s = s.strip().lstrip("v")
    if "-" in s:
        core, pre = s.split("-", 1)
    else:
        core, pre = s, ""
    nums = []
    for part in core.split("."):
        part = re.sub(r"[^0-9].*$", "", part)
        nums.append(int(part) if part.isdigit() else 0)
    if not pre:
        return (tuple(nums), (1,))
    pre_key: list = []
    for part in pre.split("."):
        pre_key.append(int(part) if part.isdigit() else part)
    return (tuple(nums), (0, *pre_key))


def _fetch_latest_version(
    allow_prerelease: bool = False,
    *,
    force: bool = False,
) -> tuple[str, bool] | None:
    """Return (tag, is_prerelease) of the highest applicable release, or None on error.

    With allow_prerelease=False, queries /releases/latest (stable-only).
    With allow_prerelease=True, lists recent releases and picks the highest non-draft
    by SemVer comparison — which may be either a stable or a pre-release.

    Uses ETag caching + a 1-hour throttle. Pass force=True to bypass the
    throttle (e.g. when the user manually toggles the pre-release channel).
    """
    import json
    try:
        if not allow_prerelease:
            raw = _gh_fetch_text(
                _APP_UPDATE_RELEASES_API_URL,
                timeout=10,
                min_interval=3600,
                force=force,
            )
            if raw is None:
                return None
            data = json.loads(raw)
            tag = data.get("tag_name", "").lstrip("v")
            return (tag, False) if tag else None

        raw = _gh_fetch_text(
            _APP_UPDATE_RELEASES_LIST_API_URL,
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        releases = json.loads(raw)
        candidates = [
            (r.get("tag_name", "").lstrip("v"), bool(r.get("prerelease", False)))
            for r in releases
            if not r.get("draft", False) and r.get("tag_name")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda tp: _parse_version(tp[0]), reverse=True)
        return candidates[0]
    except Exception:
        return None


def _fetch_aur_version(*, force: bool = False) -> str | None:
    """Fetch the current AUR package version; return None on error.

    The AUR version string includes a pkgrel suffix (e.g. '0.7.9-1').
    We strip everything from the first '-' onwards so callers get a plain
    version number comparable with __version__.

    Uses ETag caching + a 1-hour throttle (AUR supports conditional GETs too).
    """
    import json
    try:
        raw = _gh_fetch_text(
            _AUR_API_URL,
            accept="application/json",
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        data = json.loads(raw)
        results = data.get("results", [])
        if not results:
            return None
        ver = results[0].get("Version", "")
        # Strip pkgrel: '0.7.9-1' -> '0.7.9'
        ver = ver.split("-")[0]
        return ver if ver else None
    except Exception:
        return None


def _is_newer_version(current: str, latest: str) -> bool:
    """Return True if latest is newer than current (strictly greater)."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        return False


def run_installer(allow_prerelease: bool = False):
    """Run the AppImage installer in a detached subprocess.

    The AppImage runtime sets SSL_CERT_FILE / CURL_CA_BUNDLE to a path inside
    its own mount point.  That mount is gone once the app exits, so curl would
    fail with a certificate error.  We scrub those variables (and any other
    AppImage-injected ones) from the child environment before launching.
    Output is logged to $XDG_CONFIG_HOME/amethyst-update.log for debugging.
    sleep 2 gives the app time to fully exit before the installer overwrites
    the running AppImage.
    """
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")
    installer_args = " --prerelease" if allow_prerelease else ""
    cmd = (
        f"sleep 2 && "
        f"SCRIPT=$(mktemp /tmp/amethyst-installer-XXXXXX.sh) && "
        f"curl -sSL {shlex.quote(_APP_UPDATE_INSTALLER_URL)} -o \"$SCRIPT\" && "
        f"chmod +x \"$SCRIPT\" && "
        f"bash \"$SCRIPT\"{installer_args} && "
        f"rm -f \"$SCRIPT\" && "
        f"nohup \"$HOME/Applications/AmethystModManager-x86_64.AppImage\" &>/dev/null &"
    )

    # Build a clean environment: start from the current env then strip every
    # variable that the AppImage runtime injects and that would be invalid once
    # the mount is gone. This env is also what the relaunched AppImage inherits
    # (the command above nohups it), so a missed var poisons the NEW process:
    # a leftover MOD_MANAGER_GAMES pointing at the old mount made game_loader
    # scan the dying mount and every handler fail with ENOENT (GH#340). Use the
    # shared list — a local copy here is exactly how that drift happened.
    from Utils.appimage_env import strip_appimage_vars
    clean_env = strip_appimage_vars(os.environ.copy())

    try:
        # with-block: close OUR copy of the log fd once the child has spawned
        # (the child keeps its own inherited duplicate).
        with open(log_path, "w", encoding="utf-8") as log_file:
            subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=clean_env,
            )
    except Exception:
        pass


def run_flatpak_installer(latest_tag: str) -> bool:
    """Download the latest .flatpak bundle and reinstall it on the host.

    The AppImage path replaces the running binary in-place; a Flatpak can't do
    that from inside its own sandbox, so we forward the install to the host's
    ``flatpak`` CLI via ``flatpak-spawn --host`` (our manifest grants
    ``--talk-name=org.freedesktop.Flatpak``, which is what makes this reachable).

    Flow, run detached so it survives our own shutdown:
      1. curl the release's ``AmethystModManager.flatpak`` bundle to a temp file.
      2. ``flatpak install <scope> --bundle --reinstall -y`` it on the host.
      3. relaunch ``flatpak run <app-id>`` and clean up the temp bundle.

    Output is logged to $XDG_CONFIG_HOME/amethyst-update.log (same as AppImage;
    under flatpak XDG_CONFIG_HOME is redirected into ~/.var/app/<id>/config).
    A ``sleep 2`` lets us exit first. Returns True if the child launched.

    NB: the bundle is stamped without a tag, so the download URL carries the
    version. ``latest_tag`` is the release tag (with or without a leading 'v').
    """
    import shutil

    if not shutil.which("flatpak-spawn"):
        return False

    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")

    tag = latest_tag.lstrip("v")
    bundle_url = _APP_UPDATE_FLATPAK_BUNDLE_URL.format(tag=tag)

    # The temp bundle must live somewhere the HOST flatpak can read. ~/Downloads
    # is inside our --filesystem=home grant AND visible to the host, so it works
    # from both sides; /tmp is sandbox-private and unreadable to the host.
    dl_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except Exception:
        dl_dir = os.path.expanduser("~")
    bundle_path = os.path.join(dl_dir, "AmethystModManager.update.flatpak")

    # curl runs in-sandbox (network is granted); install/run go to the host.
    # --directory=/ avoids the portal failing on the app's sandbox-only cwd.
    host = "flatpak-spawn --host --directory=/"
    _q_bundle = shlex.quote(bundle_path)
    # Install into whichever installation already holds us — a hardcoded --user
    # would fork a second copy alongside a system-wide install.
    scope = _install_scope()
    cmd = (
        f"sleep 2 && "
        f"curl -fsSL {shlex.quote(bundle_url)} -o {_q_bundle} && "
        f"{host} flatpak install {scope} --bundle --reinstall --noninteractive -y "
        f"{_q_bundle} && "
        f"rm -f {_q_bundle} && "
        f"{host} flatpak run {shlex.quote(_APP_ID)} &>/dev/null &"
    )

    try:
        # with-block: close OUR copy of the log fd once the child has spawned
        # (the child keeps its own inherited duplicate).
        with open(log_path, "w", encoding="utf-8") as log_file:
            subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return True
    except Exception:
        return False


# ── Hosted-remote update path (preferred over the bundle download) ──────────
#
# Once the app is installed from our GitHub Pages remote, updates are the OS's
# job: `flatpak update` pulls only changed OSTree objects (delta), and GNOME
# Software / Discover surface the update natively. These helpers (a) tell
# whether we're already tracking the remote, (b) enrol a bundle-installed user
# onto it, and (c) trigger an update. All host calls go via `flatpak-spawn
# --host` — our manifest grants `--talk-name=org.freedesktop.Flatpak`.


def _host_flatpak(*args: str, timeout: int = 60):
    """Run `flatpak <args>` on the host, returning CompletedProcess or None.

    Uses flatpak-spawn --host (we're sandboxed). Returns None if flatpak-spawn
    is unavailable or the call raises, so callers can treat that as "unknown".
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return None
    try:
        # --directory=/ is REQUIRED: the portal spawns the host command in the
        # caller's cwd, and the app runs from /app/share/amethyst-mod-manager —
        # a sandbox-only path. Without it every call fails with "Portal call
        # failed: Failed to change to directory" (same fix as proton_tools).
        return subprocess.run(
            ["flatpak-spawn", "--host", "--directory=/", "flatpak", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


_install_scope_cache: str | None = None


def _install_scope() -> str:
    """"--user" or "--system": which flatpak installation holds our app.

    `flatpak remote-add` / `install` default to --system when run without a
    scope flag (which is what the documented remote-add one-liner does), so a
    system-wide install with a system-wide remote is a perfectly normal setup —
    but every remote query has to be made in the SAME scope or flatpak answers
    "Remote not found in the user installation" and we misread that as "channel
    not published". Parsed from `flatpak info`'s "Installation:" line; defaults
    to --user (the historical assumption) when undeterminable.

    Memoised: this feeds several helpers per update check and each call is a
    portal round-trip. `_reset_scope_cache()` clears it after an enroll.
    """
    global _install_scope_cache
    if _install_scope_cache is not None:
        return _install_scope_cache
    scope = "--user"
    cp = _host_flatpak("info", _APP_ID)
    if cp is not None and cp.returncode == 0:
        for line in cp.stdout.splitlines():
            k, _, v = line.partition(":")
            if k.strip().lower() == "installation":
                scope = "--system" if v.strip().lower().startswith("system") \
                    else "--user"
                break
    _install_scope_cache = scope
    return scope


def _reset_scope_cache() -> None:
    """Forget the memoised installation scope (call after enroll/reinstall)."""
    global _install_scope_cache
    _install_scope_cache = None


def _flatpak_scopes() -> tuple[str, str]:
    """Scopes to probe, our own installation's scope first.

    Order matters when the same repo URL is configured in both scopes: the
    remote we should be talking to is the one in the scope our app lives in.
    """
    inst = _install_scope()
    return (inst, "--system" if inst == "--user" else "--user")


def _remote_for_our_url() -> tuple[str, str] | None:
    """(name, scope) of the configured remote pointing at our hosted repo.

    Matched by URL, NOT by name: a bundle installed with --repo-url gets an
    auto-created origin named "<app>-origin" (e.g. modmanager-origin), while a
    the one-liner/enroll path uses the same name. Both point at the same repo,
    so URL is the reliable identity. Trailing slashes are normalised.

    Queries BOTH scopes and includes --show-disabled: bundle-created origins
    are flagged `no-enumerate` (and can be disabled by a duplicate-URL clash),
    so a plain `flatpak remotes` hides them. Scope also matters — a user bundle
    install lands in the --user list, which the default (system) query omits,
    and a `flatpak remote-add` without a scope flag lands in --system.
    """
    want = _FLATPAK_REMOTE_REPO_URL.rstrip("/")
    for scope in _flatpak_scopes():
        cp = _host_flatpak("remotes", scope, "--show-disabled",
                           "--columns=name,url")
        if cp is None or cp.returncode != 0:
            continue
        for line in cp.stdout.splitlines():
            parts = line.split("\t") if "\t" in line else line.split(None, 1)
            if len(parts) != 2:
                continue
            name, url = parts[0].strip(), parts[1].strip().rstrip("/")
            if url == want:
                return name, scope
    return None


def _remote_name_for_our_url() -> str | None:
    """Name of the configured remote pointing at our hosted repo, or None."""
    found = _remote_for_our_url()
    return found[0] if found else None


def flatpak_installed_from_remote() -> bool:
    """True if our install's origin is a remote pointing at our hosted repo.

    Matched by the origin remote's URL (not its name), so it recognises both
    the enroll/one-liner remote AND the auto-created "<app>-origin" a
    --repo-url bundle install creates (now the SAME name). Bundle installs WITHOUT
    --repo-url (or any non-remote install) have no matching origin → False.
    Conservatively returns False when the host can't be queried.
    """
    cp = _host_flatpak("info", "--show-origin", _APP_ID)
    if cp is None or cp.returncode != 0:
        return False
    origin = cp.stdout.strip()
    if not origin:
        return False
    our_remote = _remote_name_for_our_url()
    return our_remote is not None and origin == our_remote


def _effective_remote() -> tuple[str, str]:
    """(name, scope) to target for remote queries and the reinstall.

    Prefers the actually-configured remote for our URL (which may be the
    auto-created "<app>-origin" on a --repo-url bundle install), falling back
    to the canonical name in our own installation's scope when none is
    configured yet (the enroll path is about to create it there).
    """
    return _remote_for_our_url() or (_FLATPAK_REMOTE_NAME, _install_scope())


def polish_flatpak_origin() -> None:
    """Make a bundle-created origin remote presentable (idempotent, quiet).

    A --repo-url bundle install auto-creates its origin remote flagged
    no-enumerate with the bundle filename as its title. Software centers skip
    appstream downloads for no-enumerate remotes, so Discover shows updates as
    "<version> → <branch>" (it falls back to the branch name when the target
    has no appstream version). Flip the flag and set a proper title so update
    entries read "2.0.4-beta.4 → 2.0.4-beta.5" instead. No-op when the remote
    is absent, already enumerable, or the host can't be reached.

    Deliberately limited to --user remotes: `remote-modify --system` writes to
    /var/lib/flatpak and needs polkit, and this runs unattended at startup — a
    surprise auth prompt every launch would be far worse than a cosmetic
    Discover label. System remotes added from our .flatpakrepo are enumerable
    anyway, so there is nothing to heal there.
    """
    found = _remote_for_our_url()
    if not found:
        return
    name, scope = found
    if scope != "--user":
        return
    cp = _host_flatpak("remotes", scope, "--show-disabled",
                       "--columns=name,options")
    if cp is None or cp.returncode != 0:
        return
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip() == name:
            if "no-enumerate" in (parts[1] if len(parts) > 1 else ""):
                _host_flatpak("remote-modify", scope, name, "--enumerate",
                              "--title=Amethyst Mod Manager")
            break


def flatpak_remote_branch_available(branch: str) -> bool:
    """True if the hosted remote actually carries our app on *branch*.

    The remote's branches are created lazily by CI (`beta` doesn't exist until
    the first beta tag is published), so an install/switch targeting a missing
    branch would fail silently in the detached child. Callers use this to
    surface "channel not published yet" instead.
    """
    name, scope = _effective_remote()
    cp = _host_flatpak("remote-info", scope, name, f"{_APP_ID}//{branch}")
    return cp is not None and cp.returncode == 0


def _installed_flatpak_state() -> tuple[str, str] | None:
    """(branch, commit) of the installed app, or None if undeterminable.

    Scope-free `flatpak info` — the app exists in exactly one installation and
    hardcoding --user made this return None for system-wide installs.
    """
    cp = _host_flatpak("info", _APP_ID)
    if cp is None or cp.returncode != 0:
        return None
    branch = commit = ""
    for line in cp.stdout.splitlines():
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if k == "branch":
            branch = v.strip()
        elif k == "commit":
            commit = v.strip()
    return (branch, commit) if branch and commit else None


def flatpak_remote_update_ready(branch: str) -> bool | None:
    """Does the hosted remote's *branch* offer something we don't have?

    True  → remote head differs from the installed commit, or the installed
            branch differs from the requested channel (a switch is wanted).
    False → remote is reachable and we're already current — nothing to offer,
            even if GitHub Releases claims a newer tag (Pages publish lag).
    None  → couldn't determine (host unreachable etc.); caller falls back to
            the GitHub-only decision.
    """
    if not flatpak_remote_branch_available(branch):
        return False  # nothing installable on that channel
    name, scope = _effective_remote()
    cp = _host_flatpak("remote-info", scope, name, f"{_APP_ID}//{branch}")
    if cp is None or cp.returncode != 0:
        return None
    remote_commit = ""
    for line in cp.stdout.splitlines():
        k, _, v = line.partition(":")
        if k.strip().lower() == "commit":
            remote_commit = v.strip()
            break
    installed = _installed_flatpak_state()
    if not remote_commit or installed is None:
        return None
    inst_branch, inst_commit = installed
    if inst_branch != branch:
        return True  # channel switch requested
    # flatpak sometimes prints truncated commits; compare by prefix.
    shorter = min(len(inst_commit), len(remote_commit))
    return inst_commit[:shorter] != remote_commit[:shorter]


def _launch_remote_reinstall(branch: str) -> str:
    """Detached: reinstall our app from the remote on *branch*, then relaunch.

    Shared tail of enroll/update. Returns "launched" or "unavailable".
    """
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")

    # --directory=/ avoids the portal failing on the app's sandbox-only cwd.
    host = "flatpak-spawn --host --directory=/"
    ref = f"{_APP_ID}/x86_64/{branch}"
    # Target the actual configured remote for our URL — a --repo-url bundle
    # install named it "<app>-origin". After enroll's own remote-add this
    # resolves to that same name; for an update it's whatever the
    # user has. Reinstall pins the branch (handles same-branch update AND
    # channel switch — `flatpak update` won't cross branches).
    #
    # The scope must be the remote's own: --user against a system-wide remote
    # fails with "Remote not found in the user installation". A --system
    # reinstall goes through polkit, so the desktop's auth agent prompts — that
    # is fine here because this only ever runs from an explicit Update click.
    remote, scope = _effective_remote()
    cmd = (
        f"sleep 2 && "
        f"{host} flatpak install {scope} --reinstall --noninteractive -y "
        f"{shlex.quote(remote)} {shlex.quote(ref)} && "
        f"{host} flatpak run {shlex.quote(_APP_ID)} &>/dev/null &"
    )
    try:
        # with-block: close OUR copy of the log fd once the child has spawned
        # (the child keeps its own inherited duplicate).
        with open(log_path, "w", encoding="utf-8") as log_file:
            subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return "launched"
    except Exception:
        return "unavailable"


def enroll_flatpak_remote(*, allow_prerelease: bool = False) -> str:
    """Add the hosted remote and reinstall the app from it.

    One-time migration for bundle-installed users; afterwards updates are
    native `flatpak update`. The remote-add runs SYNCHRONOUSLY (idempotent via
    --if-not-exists) so we can probe the requested channel before committing;
    the reinstall+relaunch then runs detached with a 2s delay so we exit first.

    Returns "launched" (child started — caller should close the app),
    "no-branch" (remote reachable but the requested channel isn't published
    yet, e.g. beta before the first beta release), or "unavailable" (host
    flatpak unreachable). GPG verification stays on — the .flatpakrepo the
    remote-add consumes carries the signing key.
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return "unavailable"

    branch = "beta" if allow_prerelease else "stable"
    # Only add when nothing already points at our repo. Adding a --user remote
    # while a --system one carries the same URL makes flatpak disable one of
    # them ("Can't fetch summary from disabled remote"), which breaks updates
    # for a user who followed the documented remote-add one-liner.
    if _remote_for_our_url() is None:
        # Add it in the scope our app lives in, so the reinstall below can
        # target the existing installation instead of forking a second one.
        cp = _host_flatpak("remote-add", _install_scope(), "--if-not-exists",
                           _FLATPAK_REMOTE_NAME, _FLATPAK_REMOTE_FILE_URL,
                           timeout=120)
        if cp is None or cp.returncode != 0:
            return "unavailable"
    if not flatpak_remote_branch_available(branch):
        return "no-branch"
    return _launch_remote_reinstall(branch)


def update_flatpak_from_remote(*, allow_prerelease: bool = False) -> str:
    """Update (or branch-switch) the app from the hosted remote.

    Returns "launched" (reinstall started — caller should close the app),
    "no-branch" (the requested channel isn't published on the remote, so the
    detached install would fail silently — surface it instead), or
    "unavailable" (host flatpak unreachable).
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return "unavailable"

    branch = "beta" if allow_prerelease else "stable"
    if not flatpak_remote_branch_available(branch):
        return "no-branch"
    return _launch_remote_reinstall(branch)
