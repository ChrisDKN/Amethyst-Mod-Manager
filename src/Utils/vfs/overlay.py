"""Linux profile-local virtual game-view support.

The default backend materializes one complete, profile-local game tree from
hardlinks (with the standard symlink/copy fallback) and binds that tree over
the real game directory only inside the launched process' mount namespace.
This keeps file reads on the kernel filesystem instead of putting every Wine
lookup through FUSE.  The older kernel and FUSE overlay launchers remain
readable for already-deployed manifests and as diagnostic fallbacks.

UMU and Valve's Steam Linux Runtime create their own mount namespaces. Nesting
either inside the outer bubblewrap namespace can crash Wine during bootstrap,
so those launches execute the complete shadow tree directly instead. Native
handlers may opt into that route when they do not require the original install
path; other launchers keep the bind mount.

Only the launched process and its children see the private view. The real game
directory remains unmodified. Games opt into this module when their deployment
can be represented by a game-root layer plus one primary mod-data directory
(Bethesda's ``Data`` layout is the first implementation).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Iterable

from Utils import perftrace
from Utils.atomic_write import write_atomic_text
from Utils.deploy import (
    LinkMode,
    compute_rule_claims,
    deploy_custom_rules,
    deploy_filemap,
    deploy_root_flagged_mods,
    deploy_root_folder,
)
from Utils.deploy_shared import (
    RestoreIncompleteError,
    _do_link_ex,
    _resolve_nocase,
    _resolve_root_path,
)
from Utils.deploy_shared import (
    OVERWRITE_LOG_NAME,
    _move_runtime_files,
    _write_deploy_snapshot,
    create_probe_stub_dirs,
    deploy_case_alias_links,
)


STATE_DIR_NAME = ".amethyst-vfs"
MANIFEST_NAME = "manifest.json"
PENDING_NAME = "pending"
MANIFEST_VERSION = 1
RUNTIME_NAME = "runtime.sh"
BACKEND_KERNEL = "kernel-overlayfs"
BACKEND_FUSE = "fuse-overlayfs"
BACKEND_SHADOW = "shadow-tree"

SHADOW_NAME = "view"
SHADOW_BUILD_NAME = "view.build"
SHADOW_PREVIOUS_NAME = "view.previous"
ROOT_SNAPSHOT_NAME = "root-view-snapshot.txt"
DATA_SNAPSHOT_NAME = "data-view-snapshot.txt"
INCOMPLETE_VIEW_NAME = "view.incomplete"

_CUSTOM_RULE_ARTIFACTS = (
    "custom_rules_deployed.txt",
    "custom_rules_backup",
    "custom_rules_prefix_backup",
)
_CUSTOM_DEPLOY_ARTIFACTS = (
    "custom_deploy_log.txt",
    "custom_deploy_backup",
)


def _inside_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def _profile_dir(game, profile: str | None = None) -> Path:
    active = getattr(game, "_active_profile_dir", None)
    if profile:
        return game.get_profile_root() / "profiles" / profile
    if active is not None:
        return Path(active)
    return game.get_profile_root() / "profiles" / "default"


def state_dir(game, profile: str | None = None) -> Path:
    return _profile_dir(game, profile) / STATE_DIR_NAME


def _validate_state_root(state: Path, *, create: bool = False) -> Path:
    """Reject a VFS state root that could redirect managed operations.

    Cleanup deliberately removes fixed children below ``.amethyst-vfs``.  A
    symlink in place of that directory would make those otherwise-safe paths
    point outside the profile, so validate it before reading, writing, or
    deleting any deployment state.  Recheck after mkdir to narrow the race
    window with an external filesystem change.
    """
    if state.is_symlink():
        raise RuntimeError(
            f"Refusing symlinked profile VFS state directory: {state}")
    if os.path.lexists(state) and not state.is_dir():
        raise RuntimeError(
            f"Profile VFS state path is not a directory: {state}")
    if create:
        state.mkdir(parents=True, exist_ok=True)
        if state.is_symlink() or not state.is_dir():
            raise RuntimeError(
                f"Refusing unsafe profile VFS state directory: {state}")
    return state


def manifest_path(game, profile: str | None = None) -> Path:
    return state_dir(game, profile) / MANIFEST_NAME


def pending_path(game, profile: str | None = None) -> Path:
    """Marker retained when a VFS build stops before it can be published."""
    return state_dir(game, profile) / PENDING_NAME


def has_deployment_state(game, profile: str | None = None) -> bool:
    """Whether published, interrupted, or retained profile VFS state exists."""
    state = state_dir(game, profile)
    # Keep an invalid state root discoverable so Restore reports the safety
    # problem instead of silently treating the profile as undeployed.
    if state.is_symlink():
        return True
    return (manifest_path(game, profile).is_file()
            or pending_path(game, profile).is_file()
            or (_uses_root_folder_runtime(game)
                and _legacy_shadow_upper(state)))


def deployment_state_profiles(game) -> tuple[str, ...]:
    """Profiles with a published view or an interrupted-build marker.

    VFS state is deliberately profile-local, while the application's Restore
    worker starts from ``last_deployed``.  A first failed build has no successful
    deploy record yet, so discover its marker here.  Newest state is returned
    first; repeated Restore operations can therefore recover multiple stale
    profiles deterministically if an older version ever left more than one.
    """
    profiles_root = game.get_profile_root() / "profiles"
    try:
        profiles = list(profiles_root.iterdir())
    except OSError:
        return ()
    found: list[tuple[int, str]] = []
    for profile_dir in profiles:
        state = profile_dir / STATE_DIR_NAME
        if state.is_symlink():
            try:
                stamp = state.lstat().st_mtime_ns
            except OSError:
                stamp = 0
            found.append((stamp, profile_dir.name))
            continue
        stamps: list[int] = []
        for name in (MANIFEST_NAME, PENDING_NAME):
            try:
                stamps.append((state / name).stat().st_mtime_ns)
            except OSError:
                pass
        if _uses_root_folder_runtime(game) and _legacy_shadow_upper(state):
            try:
                log_path = state / "root-upper" / OVERWRITE_LOG_NAME
                stamps.append(log_path.stat().st_mtime_ns)
            except OSError:
                pass
        if stamps:
            found.append((max(stamps), profile_dir.name))
    found.sort(key=lambda item: (-item[0], item[1].casefold()))
    return tuple(name for _stamp, name in found)


def has_any_deployment_state(game) -> bool:
    """Whether any profile belonging to *game* retains VFS state."""
    return bool(deployment_state_profiles(game))


def _remove_tree(path: Path) -> None:
    """Remove a managed tree, including overlayfs's mode-000 work subdir."""
    if not path.exists() or path.is_symlink():
        return
    try:
        path.chmod(0o700)
    except OSError:
        pass
    for dirpath, dirnames, _filenames in os.walk(path):
        base = Path(dirpath)
        for name in dirnames:
            child = base / name
            if child.is_symlink():
                continue
            try:
                child.chmod(0o700)
            except OSError:
                pass
    shutil.rmtree(path)


def _remove_artifacts(parent: Path, names: Iterable[str]) -> None:
    """Remove deployment bookkeeping produced against a synthetic target."""
    for name in names:
        path = parent / name
        if path.is_dir() and not path.is_symlink():
            _remove_tree(path)
        else:
            path.unlink(missing_ok=True)


def _assert_under(path: Path, parent: Path, label: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Unsafe profile VFS {label} path: {path}") from exc


def _safe_clear(path: Path, parent: Path) -> None:
    _assert_under(path, parent, "state")
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace symlinked VFS state: {path}")
    if path.exists():
        _remove_tree(path)


def _bubblewrap_binary() -> str | None:
    if _inside_flatpak():
        # The host binary is resolved by flatpak-spawn, not against the
        # Freedesktop runtime's PATH inside Amethyst's sandbox.
        return "bwrap" if shutil.which("flatpak-spawn") else None
    system = shutil.which("bwrap")
    if system:
        return system

    # Portable/AppImage builds carry an ordinary unprivileged fallback under
    # a private name. Do not put it on PATH as `bwrap`: the distribution's
    # package must remain authoritative when present (it may carry distro-
    # specific hardening or a privileged installation for hosts which disable
    # unprivileged user namespaces). APPDIR can leak from an unrelated
    # AppImage into child processes, so trust it only when this module itself
    # was loaded from that mount.
    appdir_text = os.environ.get("APPDIR", "")
    if not appdir_text:
        return None
    try:
        appdir = Path(appdir_text).resolve()
        Path(__file__).resolve().relative_to(appdir)
    except (OSError, ValueError):
        return None
    for candidate in (
        appdir / "bin" / "amethyst-bwrap",
        appdir / "usr" / "bin" / "amethyst-bwrap",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _bubblewrap_invocation() -> list[str]:
    binary = _bubblewrap_binary()
    if binary is None:
        return []
    if _inside_flatpak():
        return ["flatpak-spawn", "--host", binary]
    return [binary]


def _bubblewrap_help() -> tuple[bool, str, str]:
    """Return ``(available, reason, help_text)`` for the host bwrap."""
    invocation = _bubblewrap_invocation()
    if not invocation:
        if _inside_flatpak():
            return False, "Flatpak host spawning is unavailable", ""
        return False, "bubblewrap (bwrap) is not installed", ""
    try:
        probe = subprocess.run(
            [*invocation, "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bubblewrap could not be queried: {exc}", ""
    help_text = probe.stdout or ""
    if probe.returncode != 0:
        return False, "bubblewrap returned an error during its capability check", help_text
    return True, "", help_text


def _bubblewrap_status() -> tuple[bool, str]:
    """Return whether bubblewrap can provide a private bind namespace."""
    ok, reason, help_text = _bubblewrap_help()
    if not ok:
        return False, reason
    if "--bind" not in help_text or "--dev-bind" not in help_text:
        return False, "this bubblewrap build lacks bind-mount support"
    return True, ""


def bubblewrap_status() -> tuple[bool, str]:
    """Return whether this bubblewrap build supports native overlay mounts."""
    ok, reason, help_text = _bubblewrap_help()
    if not ok:
        return False, reason
    if "--overlay-src" not in help_text or "--overlay " not in help_text:
        return False, "this bubblewrap build does not support native overlay mounts"
    return True, ""


def fuse_overlay_status() -> tuple[bool, str]:
    """Return whether the host has the userspace-overlay runtime dependencies."""
    required = ("bwrap", "fuse-overlayfs", "fusermount3", "mountpoint", "flock")
    if _inside_flatpak():
        if not shutil.which("flatpak-spawn"):
            return False, "Flatpak host spawning is unavailable"
        checks = " && ".join(f"command -v {name} >/dev/null" for name in required)
        checks += " && test -r /dev/fuse && test -w /dev/fuse"
        try:
            probe = subprocess.run(
                ["flatpak-spawn", "--host", "/bin/sh", "-c", checks],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"host FUSE tools could not be queried: {exc}"
        if probe.returncode != 0:
            return False, (
                "the host needs bwrap, fuse-overlayfs, fuse3 and access to /dev/fuse"
            )
        return True, ""

    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        return False, "required host tool(s) not found: " + ", ".join(missing)
    fuse_device = Path("/dev/fuse")
    if not fuse_device.exists() or not os.access(fuse_device, os.R_OK | os.W_OK):
        return False, "/dev/fuse is unavailable to this process"
    return True, ""


def _install_runtime(state: Path) -> Path:
    """Copy the FUSE companion somewhere visible outside a Flatpak sandbox."""
    source = Path(__file__).with_name("runtime.sh")
    try:
        body = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Profile VFS runtime is missing: {source}") from exc
    target = state / RUNTIME_NAME
    write_atomic_text(target, body)
    target.chmod(0o755)
    return target


def _mapped_separator_dirs(per_mod_deploy: dict[str, Path], game_root: Path,
                           data_root: Path, root_layer: Path,
                           data_layer: Path
                           ) -> tuple[dict[str, Path], set[str]]:
    """Route separator destinations to the private layer or the real target.

    Destinations below the game directory are rewritten into the profile-local
    shadow payload.  External destinations (typically a Proton-prefix save or
    configuration directory) cannot be covered by the game-root bind mount, so
    they retain the normal reversible physical separator deployment.
    """
    mapped: dict[str, Path] = {}
    external: set[str] = set()
    resolved_game = game_root.resolve()
    try:
        data_rel = data_root.resolve().relative_to(resolved_game)
    except ValueError as exc:
        raise RuntimeError(
            f"VFS mod-data directory must be inside the game directory: {data_root}"
        ) from exc
    data_key = tuple(part.casefold() for part in data_rel.parts)
    for mod_name, raw in per_mod_deploy.items():
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = game_root / target
        try:
            rel = target.resolve().relative_to(resolved_game)
        except ValueError:
            # Preserve the configured spelling rather than its resolved path.
            # Proton prefixes commonly contain symlinked path components, and
            # normal separator deployment/cleanup bookkeeping is intentionally
            # expressed in terms of the user-selected destination.
            mapped[mod_name] = target
            external.add(mod_name)
            continue
        rel_key = tuple(part.casefold() for part in rel.parts)
        if data_key and rel_key[:len(data_key)] == data_key:
            mapped[mod_name] = data_layer.joinpath(*rel.parts[len(data_key):])
        else:
            mapped[mod_name] = root_layer / rel
    return mapped, external


def _overwrite_entries() -> set[str]:
    """Paths supplied by [Overwrite], which is mounted as Data's upper layer."""
    from Utils.filegraph_deploy import legacy_rows
    return {
        rel.replace("\\", "/").lower()
        for rel, owner in legacy_rows()
        if owner == "[Overwrite]"
    }


def _reject_symlink_payload(layer: Path) -> None:
    """A writable open through a lower-layer symlink could alter staging."""
    stack = [str(layer)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    # DirEntry normally answers both checks from d_type,
                    # avoiding one lstat per deployed file on Linux.
                    if entry.is_symlink():
                        raise RuntimeError(
                            "Profile VFS could not create a hardlink-safe "
                            f"layer; a symbolic link was produced at "
                            f"{entry.path}. Move the profile/staging folder "
                            "to a hardlink-capable filesystem."
                        )
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except RuntimeError:
            raise
        except OSError as exc:
            raise RuntimeError(
                f"Profile VFS could not validate its private layer at "
                f"{current}: {exc}"
            ) from exc


def _merge_tree(source: Path, destination: Path) -> None:
    """Move a synthetic payload into a layer, replacing case-insensitively.

    Later physical deploy stages win in this order too: custom rules/normal
    Data first, then Root_Folder and root-flagged payload.
    """
    if not source.is_dir():
        return
    cache: dict = {}
    files: list[tuple[Path, Path]] = []
    for dirpath, _dirnames, filenames in os.walk(source):
        base = Path(dirpath)
        for name in filenames:
            src = base / name
            files.append((src, src.relative_to(source)))
    for src, rel in files:
        dst = _resolve_root_path(destination, rel, cache)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_dir() and not dst.is_symlink():
            _remove_tree(dst)
        else:
            dst.unlink(missing_ok=True)
        src.rename(dst)
    _remove_tree(source)


def _remove_path(path: Path) -> None:
    """Remove one existing file, link, or directory without following links."""
    if path.is_dir() and not path.is_symlink():
        _remove_tree(path)
    else:
        path.unlink(missing_ok=True)


def _resolved_parent(destination: Path, rel: Path, cache: dict) -> Path:
    """Resolve *rel*'s parent using the physical deploy casing rules."""
    if rel.parent == Path("."):
        return destination
    marker = rel.parent / ".amethyst-shadow-placeholder"
    return _resolve_root_path(destination, marker, cache).parent


def _materialize_tree(
    source: Path,
    destination: Path,
    *,
    replace: bool,
    move: bool = False,
    exclude: set[str] | None = None,
) -> tuple[int, int, int]:
    """Merge *source* into a physical shadow tree.

    Regular files are hardlinked, falling back through the same symlink/copy
    policy as normal Amethyst deployment.  ``move`` is used for the temporary
    resolved mod layers: moving their already-created hardlinks avoids adding
    another set of directory entries.  Symbolic links are cloned verbatim and
    never followed while walking.

    ``exclude`` contains lowercased, source-relative paths which must not be
    published. Returns ``(hardlinks_or_moves, symlinks, copies)``.
    """
    if not source.is_dir():
        return 0, 0, 0

    destination.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    ensured_parents = {str(destination)}
    linked = symlinked = copied = 0
    source_text = str(source)
    prefix_len = len(source_text) + 1

    for dirpath, dirnames, filenames in os.walk(source_text, followlinks=False):
        base = Path(dirpath)
        rel_dir_text = dirpath[prefix_len:] if dirpath != source_text else ""
        rel_dir = Path(rel_dir_text) if rel_dir_text else Path()

        link_dirs: list[str] = []
        for name in list(dirnames):
            if (base / name).is_symlink():
                dirnames.remove(name)
                link_dirs.append(name)

        # Preserve empty directories. Non-empty parents are created below as
        # their files are placed, so this does not add work for large trees.
        if not dirnames and not filenames and not link_dirs and rel_dir.parts:
            empty_target = _resolved_parent(
                destination, rel_dir / ".amethyst-shadow-placeholder", cache)
            empty_target.mkdir(parents=True, exist_ok=True)

        for name in (*link_dirs, *filenames):
            src = base / name
            rel = rel_dir / name if rel_dir.parts else Path(name)
            if exclude and rel.as_posix().lower() in exclude:
                continue
            parent = _resolved_parent(destination, rel, cache)
            parent_text = str(parent)
            if parent_text not in ensured_parents:
                parent.mkdir(parents=True, exist_ok=True)
                ensured_parents.add(parent_text)

            # Directory aliases intentionally retain their exact sibling
            # spelling (Data/data/DATA). Regular files retain the filemap's
            # spelling while their parent directories merge case-insensitively.
            dst = parent / name
            if os.path.lexists(dst):
                if not replace:
                    continue
                _remove_path(dst)

            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
                symlinked += 1
                if move:
                    src.unlink(missing_ok=True)
                continue

            if move:
                src.rename(dst)
                linked += 1
                continue

            actual_mode, transfer_error = _do_link_ex(
                str(src), str(dst), LinkMode.HARDLINK)
            if transfer_error is not None:
                raise transfer_error
            if actual_mode is LinkMode.SYMLINK:
                symlinked += 1
            elif actual_mode is LinkMode.HARDLINK:
                linked += 1
            else:
                copied += 1

    if move:
        _remove_tree(source)
    return linked, symlinked, copied


def _move_disjoint_subtrees(source: Path, destination: Path) -> int:
    """Rename non-colliding directories from *source* into *destination*.

    Resolved VFS mod layers are already private trees made from hardlinks. A
    normal per-file merge would walk and rename every entry a second time even
    when an entire loose-file subtree (``meshes/``, ``textures/``, etc.) has no
    counterpart in the vanilla view. A same-filesystem directory rename is
    atomic and independent of the number of files below it.

    Only directories with no case-insensitive destination match are moved as
    a unit. Unique matching directories are recursed into so their disjoint
    children can still take the fast path. Ambiguous case-variant collisions
    are deliberately left to :func:`_materialize_tree`, preserving its
    established per-file destination resolution.
    """
    if not source.is_dir() or source.is_symlink():
        return 0
    destination.mkdir(parents=True, exist_ok=True)

    try:
        with os.scandir(destination) as entries:
            destination_dirs: dict[str, list[str]] = {}
            for entry in entries:
                # Match the established casing resolver, which treats a
                # symlink-to-directory as occupying that directory spelling.
                # We never recurse through it below.
                if entry.is_dir():
                    destination_dirs.setdefault(
                        entry.name.lower(), []).append(entry.name)
        with os.scandir(source) as entries:
            source_dirs = [
                entry.name for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return 0

    renamed = 0
    for name in source_dirs:
        src = source / name
        matches = destination_dirs.get(name.lower(), ())
        if not matches:
            target = destination / name
            # A non-directory with the same spelling is a real collision and
            # must retain the normal replace/error behavior below.
            if os.path.lexists(target):
                continue
            try:
                src.rename(target)
            except OSError:
                continue
            destination_dirs.setdefault(name.lower(), []).append(name)
            renamed += 1
            continue

        if (len(matches) != 1
                or (destination / matches[0]).is_symlink()):
            continue
        renamed += _move_disjoint_subtrees(src, destination / matches[0])
        try:
            src.rmdir()
        except OSError:
            pass
    return renamed


def _move_materialized_tree(
    source: Path,
    destination: Path,
    *,
    replace: bool,
) -> None:
    """Move a disposable resolved layer into the shadow efficiently."""
    if not source.is_dir():
        return
    if replace:
        _move_disjoint_subtrees(source, destination)
    # Files in colliding directories, root-level files, symlinks, and any
    # rename that the filesystem declined retain the established slow path.
    _materialize_tree(source, destination, replace=replace, move=True)


def _shadow_paths(payload: dict) -> tuple[Path, Path, Path]:
    """Return the shadow root, its data directory, and data-relative path."""
    view = Path(payload["view_root"])
    game_root = Path(payload["game_root"])
    data_root = Path(payload["data_root"])
    try:
        data_rel = data_root.relative_to(game_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Profile VFS data path is outside the game root: {data_root}"
        ) from exc
    return view, view.joinpath(*data_rel.parts), data_rel


def _configured_vfs_roots(game) -> tuple[Path, Path, Path]:
    """Resolved configured game/data roots and their safe relative path."""
    game_root_getter = getattr(game, "get_vfs_game_root", None)
    raw_game_root = (
        game_root_getter() if callable(game_root_getter)
        else game.get_game_path()
    )
    data_root_getter = getattr(game, "get_vfs_data_root", None)
    raw_data_root = (
        data_root_getter() if callable(data_root_getter)
        else game.get_mod_data_path()
    )
    if raw_game_root is None or raw_data_root is None:
        raise RuntimeError("The game no longer exposes its deployed VFS paths.")
    game_root = Path(raw_game_root).resolve(strict=False)
    data_root = Path(raw_data_root).resolve(strict=False)
    try:
        data_rel = data_root.relative_to(game_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Profile VFS data path is outside the game root: {data_root}"
        ) from exc
    return game_root, data_root, data_rel


def _recorded_vfs_roots(payload: dict) -> tuple[Path, Path, Path]:
    """Return the manifest's canonical game/data roots and safe relationship.

    Runtime capture belongs to the view that was deployed, even when the user
    has since selected a different installation.  The recorded roots are not
    traversed or written through here; they are used only to recover the old
    data-relative path inside the fixed, profile-owned shadow view.
    """
    paths: dict[str, Path] = {}
    for key in ("game_root", "data_root"):
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(f"Profile VFS manifest is missing {key!r}.")
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise RuntimeError(
                f"Unsafe profile VFS manifest {key} path: {candidate}"
            )
        paths[key] = candidate.resolve(strict=False)
    try:
        data_rel = paths["data_root"].relative_to(paths["game_root"])
    except ValueError as exc:
        raise RuntimeError(
            "Profile VFS manifest data path is outside its recorded game "
            f"root: {paths['data_root']}"
        ) from exc
    return paths["game_root"], paths["data_root"], data_rel


def _recorded_path(payload: dict, key: str, expected: Path) -> Path:
    """Validate one absolute manifest path against its managed destination."""
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"Profile VFS manifest is missing {key!r}.")
    candidate = Path(raw)
    if (not candidate.is_absolute()
            or candidate.resolve(strict=False) != expected.resolve(strict=False)):
        raise RuntimeError(
            f"Unsafe profile VFS manifest {key} path: {candidate}"
        )
    return expected


def _validated_shadow_paths(
    game, payload: dict, state: Path, *, use_recorded_roots: bool = False,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Return a shadow manifest's paths only when they match managed state.

    A manifest is profile bookkeeping, not authority to traverse arbitrary
    paths. In particular cleanup/runtime capture must never follow a corrupted
    ``view_root`` into the physical install or an arbitrary user directory.
    Capture may use the recorded game/data roots solely to recover the old
    relative data path after the configured installation has changed.
    """
    if payload.get("backend") != BACKEND_SHADOW:
        raise RuntimeError("The deployed profile does not use a shadow view.")
    if use_recorded_roots:
        game_root, data_root, data_rel = _recorded_vfs_roots(payload)
    else:
        game_root, data_root, data_rel = _configured_vfs_roots(game)
        _recorded_path(payload, "game_root", game_root)
        _recorded_path(payload, "data_root", data_root)

    view = _recorded_path(payload, "view_root", state / SHADOW_NAME)
    root_upper = _recorded_path(payload, "root_upper", state / "root-upper")
    expected_data_upper = Path(
        game.get_effective_overwrite_path()).resolve(strict=False)
    data_upper = _recorded_path(
        payload, "data_upper", expected_data_upper)

    _assert_under(view, state, "shadow view")
    _assert_under(root_upper, state, "root upper")
    if view.is_symlink():
        raise RuntimeError(f"Refusing symlinked profile VFS shadow view: {view}")
    if root_upper.is_symlink():
        raise RuntimeError(f"Refusing symlinked profile VFS root upper: {root_upper}")

    # The primary data path is traversed recursively during snapshot/capture.
    # Reject a symlink in that fixed path even when its final target happens to
    # remain under the view; materialized primary roots are real directories.
    view_data = view
    for part in data_rel.parts:
        view_data /= part
        if view_data.is_symlink():
            raise RuntimeError(
                f"Refusing symlinked profile VFS data path: {view_data}"
            )
    return (
        view, view_data, data_rel, game_root, data_root,
        root_upper, data_upper,
    )


def effective_shadow_root(game) -> Path:
    """Return the published profile-local game view after validating it.

    Game-specific post-view hooks may need to generate files or inspect their
    resolved deployment. Keep manifest parsing and path validation here so a
    handler never has to trust a raw JSON path or reach into VFS internals.
    """
    payload = _load_manifest(game)
    state = manifest_path(game).parent
    view, _view_data, _data_rel, *_rest = _validated_shadow_paths(
        game, payload, state)
    if not view.is_dir():
        raise RuntimeError(f"The profile VFS shadow view is missing: {view}")
    return view


def effective_shadow_data_root(game) -> Path:
    """Return the validated primary data directory in the published view.

    Wizard tools normally receive the configured, physical game path and see
    the shadow through :func:`wrap_command`.  Host-side post-processing (for
    example completing xEdit's deferred plugin rename) cannot see that mount
    namespace, so it needs the corresponding real path inside the profile.
    """
    payload = _load_manifest(game)
    state = manifest_path(game).parent
    _view, view_data, _data_rel, *_rest = _validated_shadow_paths(
        game, payload, state)
    if not view_data.is_dir():
        raise RuntimeError(
            f"The profile VFS shadow data directory is missing: {view_data}")
    return view_data


def effective_tool_game_root(game) -> Path:
    """Return the game tree host-side wizard code should inspect.

    External processes normally keep using the configured path and receive a
    bind mount through :func:`wrap_command`. Native tools and Python wrappers
    inspect files in the manager's own namespace, so they need the materialized
    view path directly while a VFS deployment is published.
    """
    if manifest_path(game).is_file():
        return effective_shadow_root(game)
    root = game.get_game_path()
    if root is None:
        raise RuntimeError("Game path not configured.")
    return Path(root)


def effective_tool_data_root(game) -> Path:
    """Return the primary data tree host-side wizard code should inspect."""
    if manifest_path(game).is_file():
        return effective_shadow_data_root(game)
    data = game.get_mod_data_path()
    if data is None:
        raise RuntimeError("Game data path not configured.")
    return Path(data)


def _capture_shadow_runtime(game, payload: dict, state: Path,
                            log_fn=None, *, retain_root: bool = True) -> int:
    """Move files created in a published shadow view into profile storage."""
    if payload.get("backend") != BACKEND_SHADOW:
        return 0
    (view, view_data, data_rel, _game_root, _data_root,
     root_upper, data_upper) = _validated_shadow_paths(
        game, payload, state, use_recorded_roots=True)

    _log = log_fn or (lambda _message: None)
    root_destination = (
        _root_runtime_destination(game, state, root_upper)
        if retain_root else root_upper
    )
    promoted = (
        _promote_shadow_root_upper(root_upper, root_destination)
        if retain_root else 0
    )
    if promoted:
        _log(
            f"VFS: moved {promoted} retained root file(s) into Root_Folder/."
        )
    if not view.is_dir() or not view_data.is_dir():
        return 0
    moved_data = _move_runtime_files(
        view_data,
        state / DATA_SNAPSHOT_NAME,
        data_upper,
        log_fn=_log,
    )
    if not data_rel.parts:
        if moved_data:
            _log(
                f"VFS: captured {moved_data} runtime-created file(s) from "
                "the shadow view."
            )
        return moved_data
    moved_root = _move_runtime_files(
        view,
        state / ROOT_SNAPSHOT_NAME,
        root_destination,
        log_fn=_log,
        exclude_dirs=(data_rel.as_posix(),),
    )
    moved = moved_data + moved_root
    if moved:
        _log(f"VFS: captured {moved} runtime-created file(s) from the shadow view.")
    return moved


def _uses_root_folder_runtime(game) -> bool:
    if getattr(game, "vfs_root_payload_targets_data", False):
        return False
    enabled = getattr(game, "root_folder_deploy_enabled", True)
    if callable(enabled):
        enabled = enabled()
    return bool(enabled) and callable(
        getattr(game, "get_effective_root_folder_path", None))


def _legacy_shadow_upper(state: Path) -> bool:
    root_upper = state / "root-upper"
    return (not root_upper.is_symlink()
            and (root_upper / OVERWRITE_LOG_NAME).is_file())


def _root_runtime_destination(game, state: Path, root_upper: Path) -> Path:
    if not _uses_root_folder_runtime(game):
        return root_upper
    try:
        destination = Path(game.get_effective_root_folder_path())
        destination.resolve(strict=False).relative_to(
            state.resolve(strict=False))
    except ValueError:
        return destination
    except (OSError, TypeError):
        pass
    return root_upper


def _promote_shadow_root_upper(
    root_upper: Path, destination: Path,
) -> int:
    if (destination.resolve(strict=False) == root_upper.resolve(strict=False)
            or not root_upper.is_dir() or root_upper.is_symlink()):
        return 0

    return sum(_materialize_tree(
        root_upper, destination, replace=False, move=True))


def _snapshot_shadow_view(
    *, state: Path, view: Path, view_data: Path, data_rel: Path,
    log_fn=None,
) -> None:
    """Write the runtime-capture baseline for one published shadow view."""
    with perftrace.span("vfs: snapshot published view"):
        if data_rel.parts:
            _write_deploy_snapshot(
                view,
                state / ROOT_SNAPSHOT_NAME,
                log_fn=log_fn,
                exclude_dirs=(data_rel.as_posix(),),
                strict=True,
            )
        else:
            (state / ROOT_SNAPSHOT_NAME).unlink(missing_ok=True)
        _write_deploy_snapshot(
            view_data,
            state / DATA_SNAPSHOT_NAME,
            log_fn=log_fn,
            strict=True,
        )


def finalize_deployment(game, *, log_fn=None) -> None:
    """Snapshot a completed shadow deploy after all game-specific hooks run."""
    payload = _load_manifest(game)
    if payload.get("backend") != BACKEND_SHADOW:
        return
    state = manifest_path(game).parent
    view, view_data, data_rel, *_rest = _validated_shadow_paths(
        game, payload, state)
    _snapshot_shadow_view(
        state=state,
        view=view,
        view_data=view_data,
        data_rel=data_rel,
        log_fn=log_fn,
    )
    # Delete last: if either snapshot write fails, cleanup/redeploy must still
    # treat this as an incomplete deploy and avoid capturing partial hook data.
    (state / INCOMPLETE_VIEW_NAME).unlink(missing_ok=True)


def _directory_children(parent: Path) -> list[str]:
    """Real, non-symlink directory names directly below *parent*."""
    try:
        with os.scandir(parent) as entries:
            return [
                entry.name for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []


def _resolve_union_dir(
    roots: Iterable[Path], parts: Iterable[str]
) -> tuple[tuple[str, ...], tuple[Path, ...]] | None:
    """Resolve a directory case-insensitively across overlay-style *roots*.

    The returned spelling follows the first (highest-priority) matching layer,
    while the second item retains every matching real directory.  Keeping all
    parents matters when deciding whether an alias name is already occupied in
    a lower layer: publishing a symlink over such an entry would hide real game
    content instead of merely accelerating Wine's lookup.
    """
    current = tuple(Path(root) for root in roots if Path(root).is_dir())
    actual: list[str] = []
    for wanted in parts:
        folded = wanted.casefold()
        matches: list[tuple[Path, str]] = []
        for parent in current:
            names = _directory_children(parent)
            name = next((item for item in names if item == wanted), None)
            if name is None:
                name = next(
                    (item for item in names if item.casefold() == folded), None)
            if name is not None:
                matches.append((parent / name, name))
        if not matches:
            return None
        actual.append(matches[0][1])
        current = tuple(path for path, _name in matches)
    return tuple(actual), current


def _union_name_occupied(parents: Iterable[Path], name: str) -> bool:
    """Whether an exact spelling is already present in any merged layer."""
    return any(os.path.lexists(parent / name) for parent in parents)


def _vfs_dir_route(
    rel: str,
    *,
    game_root: Path,
    data_root: Path,
    root_layer: Path,
    data_layer: Path,
    data_upper: Path,
) -> tuple[Path, tuple[Path, ...], tuple[str, ...]] | None:
    """Map one game-root-relative directory into the nested VFS layers."""
    cleaned = rel.replace("\\", "/").strip("/")
    if not cleaned:
        return None
    parts = tuple(part for part in cleaned.split("/") if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        return None
    try:
        data_rel = data_root.relative_to(game_root)
    except ValueError:
        return None
    data_parts = tuple(data_rel.parts)
    folded = tuple(part.casefold() for part in parts)
    data_folded = tuple(part.casefold() for part in data_parts)

    # The Data directory itself belongs to the outer root view. Everything
    # below it belongs to the nested Data view, whose overwrite is its upper.
    if data_folded and folded[:len(data_folded)] == data_folded:
        if len(parts) == len(data_parts):
            return root_layer, (root_layer, game_root), parts
        inner = parts[len(data_parts):]
        return data_layer, (data_upper, data_layer, data_root), inner
    return root_layer, (root_layer, game_root), parts


def _create_vfs_probe_stubs(
    game,
    *,
    game_root: Path,
    data_root: Path,
    root_layer: Path,
    data_layer: Path,
    data_upper: Path,
) -> int:
    """Create configured missing-directory stubs in the private lower layers."""
    created = 0
    for raw in getattr(game, "probe_stub_dirs", None) or ():
        route = _vfs_dir_route(
            raw,
            game_root=game_root,
            data_root=data_root,
            root_layer=root_layer,
            data_layer=data_layer,
            data_upper=data_upper,
        )
        if route is None:
            continue
        layer, roots, parts = route
        if _resolve_union_dir(roots, parts) is not None:
            continue

        parent_parts = parts[:-1]
        resolved_parent = _resolve_union_dir(roots, parent_parts)
        actual_parent = resolved_parent[0] if resolved_parent is not None \
            else parent_parts
        target = layer.joinpath(*actual_parent, parts[-1])
        try:
            target.mkdir(parents=True, exist_ok=True)
            created += 1
        except OSError:
            # This is a launch-time optimization, not a correctness condition.
            # The caller reports the total produced without failing deployment.
            continue
    return created


def _create_case_alias(
    layer: Path,
    actual_parts: tuple[str, ...],
    parent_dirs: tuple[Path, ...],
    extra_spelling: str | None,
) -> int:
    """Publish safe sibling aliases for one resolved virtual directory."""
    if not actual_parts:
        return 0
    real_name = actual_parts[-1]
    layer_parent = layer.joinpath(*actual_parts[:-1])
    try:
        layer_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0

    # Include the freshly created layer parent in occupancy checks even when it
    # did not exist while the merged directory was resolved.
    occupied_parents = (layer_parent, *parent_dirs)
    variants = {real_name.lower(), real_name.upper()}
    if extra_spelling:
        variants.add(extra_spelling)
    created = 0
    for variant in variants - {real_name}:
        # Never shadow a real entry from any layer. This is the VFS equivalent
        # of the physical helper's "real entries are never replaced" rule.
        if _union_name_occupied(occupied_parents, variant):
            continue
        alias = layer_parent / variant
        try:
            os.symlink(real_name, alias)
            created += 1
        except OSError:
            continue
    return created


def _create_vfs_case_aliases(
    game,
    *,
    game_root: Path,
    data_root: Path,
    root_layer: Path,
    data_layer: Path,
    data_upper: Path,
) -> int:
    """Materialize the normal Wine case aliases inside the private VFS view."""
    created = 0
    for raw in getattr(game, "case_alias_dirs", None) or ():
        cleaned = raw.replace("\\", "/").strip("/")
        wildcard = cleaned.endswith("/*")
        base = cleaned[:-2] if wildcard else cleaned
        route = _vfs_dir_route(
            base,
            game_root=game_root,
            data_root=data_root,
            root_layer=root_layer,
            data_layer=data_layer,
            data_upper=data_upper,
        )
        if route is None:
            continue
        layer, roots, parts = route
        resolved = _resolve_union_dir(roots, parts)
        if resolved is None:
            continue
        actual_parts, matching_dirs = resolved

        if wildcard:
            children_by_parent = tuple(
                (parent, _directory_children(parent))
                for parent in matching_dirs
            )
            children: dict[str, str] = {}
            for _parent, names in children_by_parent:
                for name in names:
                    children.setdefault(name.casefold(), name)
            for real_name in children.values():
                created += _create_case_alias(
                    layer,
                    (*actual_parts, real_name),
                    matching_dirs,
                    None,
                )
            continue

        wanted = parts[-1]
        created += _create_case_alias(
            layer,
            actual_parts,
            tuple(path.parent for path in matching_dirs),
            wanted if wanted != actual_parts[-1] else None,
        )
    return created


def build_layers(
    game,
    *,
    profile: str,
    filemap: Path,
    staging: Path,
    per_mod_strip: dict[str, list[str]],
    per_mod_deploy: dict[str, Path],
    raw_mods: set[str] | None,
    excluded_raw: dict[str, set[str]] | None,
    root_folder_enabled: bool,
    per_mod_subdirs: dict[str, str] | None = None,
    per_mod_link_modes: dict[str, LinkMode] | None = None,
    external_deploy_mode: LinkMode = LinkMode.HARDLINK,
    file_exclude: set[str] | None = None,
    log_fn=None,
    progress_fn=None,
) -> tuple[int, int]:
    """Build and publish the active profile's resolved private game view."""
    _log = log_fn or (lambda _message: None)

    game_root_getter = getattr(game, "get_vfs_game_root", None)
    raw_game_root = (
        game_root_getter() if callable(game_root_getter)
        else game.get_game_path()
    )
    if raw_game_root is None:
        raise RuntimeError("The game does not expose a VFS installation root.")
    game_root = Path(raw_game_root).resolve()
    data_root_getter = getattr(game, "get_vfs_data_root", None)
    raw_data_root = (
        data_root_getter() if callable(data_root_getter)
        else game.get_mod_data_path()
    )
    if raw_data_root is None:
        raise RuntimeError("The game does not expose a primary mod-data directory.")
    data_root = Path(raw_data_root).resolve()
    if data_root.exists() and not data_root.is_dir():
        raise RuntimeError(f"Mod-data path is not a directory: {data_root}")
    try:
        data_rel = data_root.relative_to(game_root)
    except ValueError as exc:
        raise RuntimeError(
            f"VFS mod-data directory must be inside the game directory: {data_root}"
        ) from exc

    state = state_dir(game, profile)
    _validate_state_root(state)
    # Putting VFS state below its own source would recursively materialize the
    # private view into itself.
    try:
        state.resolve().relative_to(game_root)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            "The profile/staging directory is inside the game folder. Move it "
            "outside the game installation before enabling VFS."
        )

    _validate_state_root(state, create=True)
    # Keep an explicit marker from before the first reversible physical side
    # effect until the resolved view is published. Restore can then distinguish
    # an interrupted first VFS build from an ordinary physical deployment.
    write_atomic_text(state / PENDING_NAME, profile + "\n")
    existing_manifest = state / MANIFEST_NAME
    incomplete_view = state / INCOMPLETE_VIEW_NAME
    if existing_manifest.is_file():
        if incomplete_view.is_file():
            _log(
                "VFS: discarding an unfinalized previous view without "
                "capturing its partial deploy output."
            )
        else:
            try:
                existing_payload = json.loads(
                    existing_manifest.read_text(encoding="utf-8"))
                if isinstance(existing_payload, dict):
                    _capture_shadow_runtime(
                        game, existing_payload, state, log_fn=_log)
            except (OSError, ValueError, RuntimeError) as exc:
                _log(f"  WARN: could not capture the previous VFS view: {exc}")

    build = state / "lower.build"
    published = state / "lower"
    shadow_build = state / SHADOW_BUILD_NAME
    shadow = state / SHADOW_NAME
    shadow_previous = state / SHADOW_PREVIOUS_NAME
    _safe_clear(build, state)
    _safe_clear(shadow_build, state)
    # Recover the old published view if the previous process stopped in the
    # tiny interval between rotating it and publishing its replacement.
    if (shadow_previous.is_dir() and not shadow_previous.is_symlink()
            and not os.path.lexists(shadow)):
        shadow_previous.rename(shadow)
    else:
        _safe_clear(shadow_previous, state)
    build.mkdir(parents=True)
    root_layer = build / "root"
    data_layer = build / "data"
    routed_layer = build / "routed"
    root_payload = build / "root-payload"
    root_layer.mkdir()
    data_layer.mkdir()
    routed_layer.mkdir()
    root_payload.mkdir()

    metadata_dir = filemap.parent
    populate_data_layer = getattr(game, "_vfs_populate_data_layer", None)
    custom_rules = (
        [] if callable(populate_data_layer)
        else list(getattr(game, "custom_routing_rules", None) or [])
    )
    game_rules = [rule for rule in custom_rules if not rule.to_prefix]
    prefix_rules = [rule for rule in custom_rules if rule.to_prefix]
    for rule in custom_rules:
        for raw_dest in (rule.dest, *getattr(rule, "mirror_dests", ())):
            normalized = str(raw_dest or "").replace("\\", "/")
            dest = Path(normalized)
            if (dest.is_absolute()
                    or PureWindowsPath(normalized).is_absolute()
                    or ".." in dest.parts):
                raise RuntimeError(
                    "Profile VFS custom-rule destinations must remain "
                    f"relative to their game or prefix root: {raw_dest!r}"
                )
    # Most handlers route only inside the game and can use the established
    # helper directly. Precompute ownership only when two namespaces compete,
    # or when we must determine whether a missing prefix is actually needed.
    game_claims: set[str] | None = None
    prefix_claims: set[str] | None = None
    needs_claim_partition = bool(game_rules and prefix_rules)
    needs_prefix_probe = bool(
        prefix_rules and game.get_prefix_path() is None)
    routing_entries: list[tuple[str, str]] = []
    if needs_claim_partition or needs_prefix_probe:
        from Utils.filegraph_deploy import legacy_rows
        routing_entries = [
            (relative, mod_name)
            for relative, mod_name in legacy_rows()
            if not raw_mods or mod_name not in raw_mods
        ]
        game_claims, prefix_claims = compute_rule_claims(
            routing_entries, custom_rules)

    if prefix_claims and game.get_prefix_path() is None:
        # A skipped prefix rule must never fall through into the game/data
        # layer. Detect only rules that actually claim an enabled file so a
        # definition may retain optional prefix routes without blocking an
        # unrelated modlist.
        raise RuntimeError(
            "Profile VFS cannot route files into the Proton prefix because "
            "this profile has no prefix configured. Configure the game's "
            "Wine/Proton prefix, then deploy again."
        )

    # Route root/Data rules against a private synthetic game directory.
    file_exclude_normalized: set[str] = {
        str(path).replace("\\", "/").lower()
        for path in (file_exclude or ())
    }
    custom_exclude: set[str] = set(file_exclude_normalized)
    if game_rules and (game_claims is None or game_claims):
        _log("VFS: resolving custom root/Data routing rules ...")
        # Synthetic game rules need private bookkeeping. Reusing the real
        # filemap parent would let their self-heal/cleanup consume a previous
        # physical prefix-route journal and destroy its recoverable backup.
        routing_metadata = build / "routing-metadata"
        routing_metadata.mkdir()
        routing_state = routing_metadata / "catalog-input"
        try:
            custom_exclude |= deploy_custom_rules(
                routing_state, routed_layer, staging,
                rules=game_rules,
                mode=LinkMode.HARDLINK,
                strip_prefixes=game.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes={},
                raw_mods=raw_mods,
                log_fn=_log,
                progress_fn=progress_fn,
                claim_paths=game_claims,
            )
        finally:
            _remove_artifacts(routing_metadata, _CUSTOM_RULE_ARTIFACTS)

        if data_rel.parts:
            routed_data = routed_layer.joinpath(*data_rel.parts)
            _merge_tree(routed_data, data_layer)
            _merge_tree(routed_layer, root_layer)
        else:
            # Root-deploy games use the same directory as both game and data
            # root. Every non-prefix rule therefore belongs in the one private
            # data layer; trying to split routed_layer from itself removes it
            # before the root pass can run.
            _merge_tree(routed_layer, data_layer)
        if game_claims is not None:
            custom_exclude |= game_claims

    # Prefix routes (loose saves) intentionally remain real prefix state and
    # retain the normal restore manifest. They never write to the game root.
    if prefix_rules and (prefix_claims is None or prefix_claims):
        custom_exclude |= deploy_custom_rules(
            filemap, game_root, staging,
            rules=prefix_rules,
            mode=external_deploy_mode,
            strip_prefixes=game.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_link_modes=per_mod_link_modes or {},
            raw_mods=raw_mods,
            log_fn=_log,
            # Prefix placement now journals every candidate before mutation,
            # so even a cancelling/raising UI callback is recoverable.
            progress_fn=progress_fn,
            prefix_root=game.get_prefix_path(),
            claim_paths=prefix_claims,
        )
        if prefix_claims is not None:
            custom_exclude |= prefix_claims

    # Overwrite is materialized last and therefore wins. Do not duplicate its
    # winning entries into the temporary resolved mod layer. Entries whose
    # destination is remapped are the exception: they must pass through
    # deploy_filemap so the source remains at its original overwrite path but
    # appears at the handler-defined destination in the shadow.
    overwrite_entries = _overwrite_entries()
    routed_overwrite_entries = custom_exclude & overwrite_entries
    path_remap = dict(getattr(game, "mod_deploy_path_remap", None) or {})
    remap_prefixes = tuple(
        str(prefix).replace("\\", "/").lower()
        for prefix in path_remap
    )
    remapped_overwrite_entries = (
        {
            entry for entry in overwrite_entries
            if any(entry.startswith(prefix) for prefix in remap_prefixes)
        } - routed_overwrite_entries
        if not callable(populate_data_layer) else set()
    )
    custom_exclude |= overwrite_entries - remapped_overwrite_entries
    if callable(populate_data_layer):
        mapped_deploy: dict[str, Path] = {}
        external_deploy_mods: set[str] = set()
        external_link_modes: dict[str, LinkMode] = {}
    else:
        mapped_deploy, external_deploy_mods = _mapped_separator_dirs(
            per_mod_deploy, game_root, data_root, root_layer, data_layer)
        external_link_modes = {
            mod_name: (per_mod_link_modes or {}).get(
                mod_name, external_deploy_mode)
            for mod_name in external_deploy_mods
        }
        if external_deploy_mods:
            names = ", ".join(sorted(external_deploy_mods, key=str.casefold))
            _log(
                "VFS: deploying external separator target(s) physically with "
                f"restore tracking: {names}."
            )

    _log(f"VFS: building the resolved {data_root.name} layer ...")
    try:
        with perftrace.span("vfs: resolve mod layer"):
            if callable(populate_data_layer):
                linked_data = int(populate_data_layer(
                    destination=data_layer,
                    outer_layer=root_layer,
                    game_root=game_root,
                    data_root=data_root,
                    profile=profile,
                    filemap=filemap,
                    staging=staging,
                    external_deploy_mode=external_deploy_mode,
                    log_fn=_log,
                    progress_fn=progress_fn,
                ) or 0)
            else:
                linked_data, _placed = deploy_filemap(
                    filemap, data_layer, staging,
                    mode=LinkMode.HARDLINK,
                    strip_prefixes=game.mod_folder_strip_prefixes,
                    per_mod_strip_prefixes=per_mod_strip,
                    per_mod_deploy_dirs=mapped_deploy or None,
                    # Internal destinations must remain hardlinks in the private
                    # materialization. Only external separator targets inherit
                    # their physical mode or explicit separator override.
                    per_mod_link_modes=external_link_modes,
                    log_fn=_log,
                    progress_fn=progress_fn,
                    exclude=custom_exclude or None,
                    per_mod_subdirs=per_mod_subdirs,
                    path_remap=path_remap or None,
                    replace_existing=True,
                    source_resolver=getattr(
                        game, "_vfs_resolve_staged_file", None),
                )
    finally:
        # Paths mapped into lower.build are disposable, but external separator
        # targets need the standard log/backup until Restore removes the
        # deployed files and puts any originals back.
        if not callable(populate_data_layer) and not external_deploy_mods:
            _remove_artifacts(metadata_dir, _CUSTOM_DEPLOY_ARTIFACTS)

    linked_root = 0
    # Root_Folder and root-flagged deployment share one recovery journal.
    # Keep the synthetic build's journal inside lower.build so it cannot
    # overwrite/delete recovery state belonging to a coexisting physical
    # deployment in filemap.parent.
    root_metadata = build / "root-metadata"
    try:
        if root_folder_enabled:
            linked_root += deploy_root_folder(
                game.get_effective_root_folder_path(), root_payload,
                mode=LinkMode.HARDLINK, log_fn=_log,
                metadata_dir=root_metadata)
        linked_root += deploy_root_flagged_mods(
            root_metadata / "catalog-input", root_payload, staging,
            mode=LinkMode.HARDLINK,
            strip_prefixes=game.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            excluded_raw=excluded_raw or None,
            log_fn=_log,
            metadata_dir=root_metadata,
        )
    finally:
        # This directory is wholly synthetic; remove it as one unit. It may
        # never have been created when both root payload sources were empty.
        if root_metadata.exists() or root_metadata.is_symlink():
            _safe_clear(root_metadata, build)

    # Reject links supplied by mods before adding our own controlled, sibling-
    # relative aliases. A mod-provided link could make a writable open escape
    # into staging; the aliases below resolve only inside the private view.
    _reject_symlink_payload(build)

    data_upper = Path(game.get_effective_overwrite_path())
    data_upper.mkdir(parents=True, exist_ok=True)
    root_upper = state / "root-upper"
    root_upper.mkdir(parents=True, exist_ok=True)

    # Build a complete physical view. Base game files are linked first, then
    # resolved mod layers and persistent root/overwrite content. Root_Folder
    # and root-flagged payload is applied last, matching deploy_pipeline's
    # physical ordering: those explicit root sources are the final authority
    # even when they collide with an [Overwrite] entry.
    shadow_build.mkdir(parents=True)
    with perftrace.span("vfs: materialize base game"):
        base_counts = _materialize_tree(
            game_root, shadow_build, replace=False)
    shadow_data = shadow_build.joinpath(*data_rel.parts)
    shadow_data.mkdir(parents=True, exist_ok=True)
    with perftrace.span("vfs: merge resolved layers"):
        _move_materialized_tree(root_layer, shadow_build, replace=True)
        _move_materialized_tree(data_layer, shadow_data, replace=True)
        _materialize_tree(root_upper, shadow_build, replace=True)
        upper_exclude = (
            file_exclude_normalized
            | routed_overwrite_entries
            | remapped_overwrite_entries
        )
        _materialize_tree(
            data_upper,
            shadow_data,
            replace=True,
            # An overwrite-owned file claimed by a custom rule was already placed
            # at that rule's destination. Do not also expose it at its original
            # data-relative path; unrouted overwrite entries still win normally.
            exclude=upper_exclude or None,
        )
    # A small number of handlers generate metadata from the fully resolved
    # mod view, but physical deployment creates that metadata before the
    # pipeline applies Root_Folder/root-flagged payloads. Give them the same
    # ordering here so explicit root payload remains the final authority.
    pre_root_hook = getattr(game, "_vfs_pre_root_payload_build", None)
    if callable(pre_root_hook):
        pre_root_hook(
            view_root=shadow_build,
            profile=profile,
            filemap=filemap,
            staging=staging,
            log_fn=_log,
        )
    if getattr(game, "vfs_root_payload_targets_data", False):
        # UE project handlers physically treat Root_Folder as relative to the
        # nested project/deploy root, not the outer install we materialize.
        _move_materialized_tree(
            root_payload, shadow_data, replace=True)
    else:
        # Generic Root_Folder/filemap_root paths are outer-install-relative;
        # this naturally covers payload below a nested primary data folder.
        _move_materialized_tree(
            root_payload, shadow_build, replace=True)
    _remove_tree(build)

    if getattr(game, "case_alias_links", True):
        with perftrace.span("vfs: case aliases"):
            stub_count = create_probe_stub_dirs(
                shadow_build,
                getattr(game, "probe_stub_dirs", None),
                log_fn=_log,
            )
            alias_count = deploy_case_alias_links(
                shadow_build,
                getattr(game, "case_alias_dirs", None),
                log_fn=_log,
            )
        if stub_count:
            _log(f"  VFS probe stubs: {stub_count} empty dir(s) created.")
        if alias_count:
            _log(f"  VFS case aliases: {alias_count} symlink(s) created.")

    # Keep this marker across publication and every fallible game-specific
    # post-view hook. It is written only after the replacement is completely
    # materialized, so an earlier build failure leaves a previously finalized
    # view eligible for ordinary runtime capture. finalize_deployment removes
    # it only after a strict, immediate snapshot refresh succeeds.
    write_atomic_text(incomplete_view, "building\n")
    _safe_clear(published, state)
    # Keep the previous working deployment until the replacement has been
    # renamed successfully. A disk/permission failure during publication must
    # not turn an otherwise launchable profile into no deployment at all.
    if shadow.is_symlink():
        _safe_clear(shadow, state)
    elif shadow.exists():
        shadow.rename(shadow_previous)
    try:
        shadow_build.rename(shadow)
    except Exception:
        if shadow_previous.exists() and not shadow.exists():
            shadow_previous.rename(shadow)
        raise
    _safe_clear(shadow_previous, state)

    hardlinks, symlinks, copies = base_counts
    materialization = f"{hardlinks} hardlink(s)"
    if symlinks:
        materialization += f", {symlinks} symlink(s)"
    if copies:
        materialization += f", {copies} copied file(s)"
    _log(f"VFS: materialized the private base game view with {materialization}.")

    payload = {
        "version": MANIFEST_VERSION,
        "profile": profile,
        "backend": BACKEND_SHADOW,
        "game_root": str(game_root),
        "data_root": str(data_root),
        "view_root": str(shadow),
        "root_layer": str(shadow),
        "data_layer": str(shadow.joinpath(*data_rel.parts)),
        "root_upper": str(root_upper),
        "data_upper": str(data_upper.resolve()),
    }

    def _publish_manifest() -> None:
        write_atomic_text(
            state / MANIFEST_NAME,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        (state / PENDING_NAME).unlink(missing_ok=True)

    def _probe_bind_launch() -> tuple[bool, str]:
        try:
            probe = subprocess.run(
                wrap_command(game, ["/bin/true"]),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        return probe.returncode == 0, (probe.stdout or "").strip()

    _publish_manifest()
    # Some shadow launches are used directly and need no outer bubblewrap
    # namespace. Probe the bind route when available, but do not discard an
    # otherwise valid deployment merely because bwrap is unavailable or a host
    # policy rejects nested namespaces. Bind-based launches re-check and report
    # the same reason before starting.
    direct_launch_available = bool(
        getattr(game, "vfs_direct_shadow_launch", False))
    fallback_label = (
        "direct shadow/UMU/Steam Runtime launches remain available"
        if direct_launch_available
        else "direct UMU/Steam Runtime launches remain available"
    )
    bind_available, bind_reason = _bubblewrap_status()
    if bind_available:
        shadow_ok, shadow_detail = _probe_bind_launch()
        if not shadow_ok:
            _log(
                f"  WARN: VFS bind-mount launch validation failed; "
                f"{fallback_label}"
                + (f": {shadow_detail}" if shadow_detail else ".")
            )
    else:
        _log(
            f"  NOTE: VFS bind-mount launches are unavailable; "
            f"{fallback_label} ({bind_reason})."
        )

    _log(
        f"VFS: published {linked_data} {data_root.name} + "
        f"{linked_root} root file(s) "
        f"using {BACKEND_SHADOW}."
    )
    return linked_data, linked_root


def _load_manifest(game) -> dict:
    path = manifest_path(game)
    _validate_state_root(path.parent)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "The profile VFS has not been deployed. Deploy "
            "the profile before launching."
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise RuntimeError("The profile VFS manifest is missing or uses an unsupported version.")
    return payload


def _empty_work_dir(path: Path, state: Path) -> None:
    _safe_clear(path, state)
    path.mkdir(parents=True)


def _place_wrapper_on_host(wrapper: list[str], command: list[str]
                           ) -> tuple[list[str], list[str]]:
    """Put *wrapper* inside an existing Flatpak host portal invocation.

    ``proton_run_command`` already adds ``flatpak-spawn --host`` when the app
    is sandboxed.  Reuse that one escape so the VFS runtime remains the parent
    of Proton instead of spawning a second portal outside its namespace.
    """
    host_command = list(command)
    if not _inside_flatpak():
        return list(wrapper), host_command
    if host_command and Path(host_command[0]).name == "flatpak-spawn":
        portal_prefix = [host_command[0]]
        index = 1
        while index < len(host_command) and host_command[index].startswith("-"):
            portal_prefix.append(host_command[index])
            index += 1
        return [*portal_prefix, *wrapper], host_command[index:]
    return ["flatpak-spawn", "--host", *wrapper], host_command


def _forward_flatpak_host_environment(
        wrapper: list[str], env: dict[str, str] | None,
        *, directory: Path | None = None) -> None:
    """Forward launch context through an existing host portal in place."""
    if not wrapper or Path(wrapper[0]).name != "flatpak-spawn":
        return
    from Utils.flatpak_env import flatpak_forward_env_args
    index = 1
    while index < len(wrapper) and wrapper[index].startswith("-"):
        index += 1
    portal_args = flatpak_forward_env_args(env)
    if directory is not None \
            and not any(token.startswith("--directory=") for token in wrapper):
        portal_args.insert(0, f"--directory={directory}")
    wrapper[index:index] = portal_args


def _uses_umu(command: list[str]) -> bool:
    """Whether *command* invokes umu-run, possibly behind another wrapper."""
    return any(Path(token).name == "umu-run" for token in command)


def _uses_steam_linux_runtime(command: list[str]) -> bool:
    """Whether *command* invokes Valve's pressure-vessel entry point."""
    return any(Path(token).name == "_v2-entry-point" for token in command)


def _retarget_shadow_paths(command: list[str], game_root: Path,
                           view: Path) -> tuple[list[str], bool, Path | None]:
    """Rewrite existing absolute game-root arguments into *view*."""
    direct = list(command)
    replaced = False
    launch_cwd: Path | None = None
    resolved_root = game_root.resolve()
    for index, token in enumerate(direct):
        candidate = Path(token)
        if not candidate.is_absolute():
            continue
        try:
            # Manifests deliberately store canonical paths. A launcher may
            # still pass the user's saved spelling through a symlinked Steam
            # library or custom install alias, so canonicalize the argument
            # before deriving its position in the shadow tree. strict=False
            # also handles mod-only executables that do not exist physically.
            relative = candidate.resolve(strict=False).relative_to(
                resolved_root)
        except (OSError, ValueError):
            continue
        shadow_candidate = view / relative
        if not shadow_candidate.exists():
            # Wine paths are case-insensitive, while the materialized Linux
            # tree retains the winning file's real spelling.
            resolved_shadow = _resolve_nocase(view, relative.as_posix())
            if resolved_shadow is None:
                continue
            shadow_candidate = resolved_shadow
        direct[index] = str(shadow_candidate)
        replaced = True
        if launch_cwd is None and shadow_candidate.is_file():
            launch_cwd = shadow_candidate.parent
    return direct, replaced, launch_cwd


def sandbox_passthrough_command(
        game, command: list[str],
        ) -> tuple[list[str], Path, dict[str, str]]:
    """Retarget a launcher command for execution in its existing sandbox.

    Flatpak launchers append commands containing paths which only exist inside
    *their* sandbox (for example Heroic's ``/app/bin/gamemoderun``).  Escaping
    that whole argv to the host makes those paths invalid.  The CLI bridge
    uses this helper on the host only to validate the deployed view and rewrite
    game-root arguments; the launcher's original shell then executes the
    returned argv inside the launcher Flatpak.

    No mount namespace is created here.  Deploy grants the launcher read/write
    access to the materialized view, so a direct shadow path is both sufficient
    and the only way to preserve sandbox-private runner paths.
    """
    if not command:
        raise RuntimeError("No launcher command was supplied to the VFS bridge.")

    payload = _load_manifest(game)
    if payload.get("backend", BACKEND_KERNEL) != BACKEND_SHADOW:
        raise RuntimeError(
            "The Flatpak launcher bridge requires a shadow-tree VFS deploy."
        )
    state = manifest_path(game).parent
    (view, _view_data, _data_rel, game_root, _data_root,
     _root_upper, _data_upper) = _validated_shadow_paths(
        game, payload, state)
    if not view.is_dir():
        raise RuntimeError(f"Profile VFS shadow root is missing: {view}")

    direct, _replaced, launch_cwd = _retarget_shadow_paths(
        command, game_root, view)
    have_virtual_exe = launch_cwd is not None
    if launch_cwd is None:
        # A preferred loader may already have been replaced with its virtual
        # path before this helper is called. Derive its correct nested working
        # directory even when some other install-root argument was rewritten.
        resolved_view = view.resolve(strict=False)
        for token in direct:
            candidate = Path(token)
            if not candidate.is_absolute():
                continue
            try:
                candidate.resolve(strict=False).relative_to(resolved_view)
            except (OSError, ValueError):
                continue
            if candidate.is_file():
                have_virtual_exe = True
                launch_cwd = candidate.parent
                break
    if not have_virtual_exe:
        # Never emit a command which only rewrote an install-directory option
        # but left its executable outside the private view.
        raise RuntimeError(
            "The launcher command does not contain a game executable "
            f"under {game_root}; check the generated wrapper settings."
        )

    return (
        direct,
        launch_cwd or view,
        {"STEAM_COMPAT_INSTALL_PATH": str(view)},
    )


def _direct_shadow_umu_command(command: list[str], game_root: Path,
                               view: Path,
                               env: dict[str, str] | None) -> list[str] | None:
    """Retarget an UMU game launch into the materialized shadow directory.

    UMU derives ``STEAM_COMPAT_INSTALL_PATH`` from the executable and starts
    Proton in its inherited working directory. Rewriting root-relative path
    arguments and changing that directory therefore gives UMU the same
    complete profile view without nesting two bubblewrap runtimes.
    """
    if not _uses_umu(command):
        return None

    # A host-started UMU launch can safely use the materialized profile path
    # as its install root.  When Amethyst itself is a Flatpak, however, games
    # launched that way consistently die in Proton before their own startup
    # logging (exit 245).  The exact same UMU/Proton and prefix work through
    # the portal when the view is instead bound over the original, short game
    # path.  UMU's pressure-vessel preserves that outer host mount, so let the
    # shared fallback below build ``flatpak-spawn --host bwrap --bind ...``
    # rather than rewriting the executable to ``.amethyst-vfs/view``.
    #
    # Keeping this conditional also avoids adding a mount namespace to the
    # native/AppImage path, where direct-shadow UMU launches are established
    # and faster.
    if _inside_flatpak():
        return None
    direct, replaced, launch_cwd = _retarget_shadow_paths(
        command, game_root, view)
    if not replaced:
        return None

    # UMU otherwise derives this from exe.parent, which is too deep for nested
    # Unreal project binaries. UMU manages STEAM_COMPAT_MOUNTS itself.
    shadow_env = {"STEAM_COMPAT_INSTALL_PATH": str(view)}
    if env is not None:
        env.update(shadow_env)
    # Make UMU and its Proton subprocess inherit the shadow directory as cwd.
    # Keep this wrapper inside an existing flatpak-spawn --host prefix when the
    # manager itself is sandboxed.
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-umu", str(launch_cwd or view),
    ], direct)
    return [
        *wrapper,
        "/usr/bin/env",
        *(f"{key}={value}" for key, value in shadow_env.items()),
        *host_command,
    ]


def _steam_runtime_shadow_env(source: dict[str, str], game_root: Path,
                              view: Path) -> dict[str, str]:
    """Return pressure-vessel path variables for a direct shadow launch."""
    mounts: list[str] = []
    for mount in source.get("STEAM_COMPAT_MOUNTS", "").split(":"):
        if mount and mount not in mounts:
            mounts.append(mount)
    # Keep the physical install visible for any absolute paths stored by the
    # game while making the complete profile view the runtime's install root.
    for mount in (str(game_root), str(view)):
        if mount not in mounts:
            mounts.append(mount)
    return {
        "STEAM_COMPAT_INSTALL_PATH": str(view),
        "STEAM_COMPAT_MOUNTS": ":".join(mounts),
    }


def _direct_shadow_steam_runtime_command(
        command: list[str], game_root: Path, view: Path,
        env: dict[str, str] | None) -> list[str] | None:
    """Run pressure-vessel directly against the materialized shadow tree."""
    if not _uses_steam_linux_runtime(command):
        return None
    direct, replaced, launch_cwd = _retarget_shadow_paths(
        command, game_root, view)
    if not replaced:
        return None

    shadow_env = _steam_runtime_shadow_env(
        env if env is not None else os.environ, game_root, view)
    if env is not None:
        env.update(shadow_env)

    # Put the shell inside a pre-existing host portal before inserting env;
    # otherwise the env prefix hides flatpak-spawn and nests a second portal.
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-steam-runtime", str(launch_cwd or view),
    ], direct)
    forwarded_env = dict(env if env is not None else os.environ)
    forwarded_env.update(shadow_env)
    _forward_flatpak_host_environment(
        wrapper, forwarded_env, directory=game_root)
    return [
        *wrapper,
        "/usr/bin/env",
        *(f"{key}={value}" for key, value in shadow_env.items()),
        *host_command,
    ]


def _bound_shadow_steam_runtime_command(
        command: list[str], game_root: Path, view: Path,
        env: dict[str, str] | None) -> list[str] | None:
    """Bind the view at the short game path *inside* pressure-vessel.

    Skyrim needs its configured install path to remain visible because deeply
    nested loose assets can otherwise cross Windows' legacy path limit. An
    outer bubblewrap works for bare Proton, but pressure-vessel constructs a
    new filesystem namespace and can replace that outer view—most visibly
    when Amethyst first escapes its Flatpak. Steam Linux Runtime ships its own
    ``srt-bwrap`` specifically for this environment, so make the final bind
    the command executed by the runtime and start Proton inside that bind.
    """
    runtime_index = next(
        (index for index, token in enumerate(command)
         if Path(token).name == "_v2-entry-point"),
        None,
    )
    if runtime_index is None:
        return None
    try:
        separator_index = command.index("--", runtime_index + 1)
    except ValueError:
        return None

    runtime_root = Path(command[runtime_index]).parent
    runtime_bwrap = (
        runtime_root / "pressure-vessel" / "libexec"
        / "steam-runtime-tools-0" / "srt-bwrap"
    )
    if not runtime_bwrap.is_file():
        raise RuntimeError(
            "Steam Linux Runtime does not contain its required srt-bwrap "
            f"helper: {runtime_bwrap}"
        )

    source_env = env if env is not None else os.environ
    shadow_env = _steam_runtime_shadow_env(source_env, game_root, view)
    # Unlike direct-shadow mode, pressure-vessel must continue reporting the
    # configured short install path. The nested bind supplies the private view
    # at precisely that destination.
    shadow_env["STEAM_COMPAT_INSTALL_PATH"] = str(game_root)
    if env is not None:
        env.update(shadow_env)

    inner_bind = [
        str(runtime_bwrap),
        "--die-with-parent",
        "--dev-bind", "/", "/",
        "--bind", str(view), str(game_root),
        "--",
    ]
    direct = [
        *command[:separator_index + 1],
        *inner_bind,
        *command[separator_index + 1:],
    ]
    # A manager-Play Proton command already starts with flatpak-spawn --host,
    # while a native Steam Launch Options command does not: Steam is on the
    # host, but it re-enters Amethyst's Flatpak to deploy. Reuse the former or
    # add the latter here so srt-bwrap never attempts a nested namespace inside
    # Amethyst's sandbox. Keep the environment after the portal but before the
    # runtime; sandbox-side variables are not forwarded automatically.
    wrapper, host_command = _place_wrapper_on_host([], direct)
    # ``flatpak-spawn --host`` starts from the desktop session rather than
    # inheriting the environment of the Steam command that entered our
    # Flatpak.  A native Steam Launch Options handoff therefore loses its app
    # ID, compatdata path and selected runtime unless we forward them at the
    # portal boundary.  Manager Play already supplies these flags in its
    # Proton command; keep the generated Steam shortcut equivalent.
    forwarded_env = dict(source_env)
    forwarded_env.update(shadow_env)
    _forward_flatpak_host_environment(
        wrapper, forwarded_env, directory=game_root)
    runtime_index = next(
        index for index, token in enumerate(host_command)
        if Path(token).name == "_v2-entry-point"
    )
    return [
        *wrapper,
        *host_command[:runtime_index],
        "/usr/bin/env",
        *(f"{key}={value}" for key, value in shadow_env.items()),
        *host_command[runtime_index:],
    ]


def _direct_shadow_opt_in_command(command: list[str], game_root: Path,
                                  view: Path,
                                  env: dict[str, str] | None) -> list[str]:
    """Run an explicitly compatible native handler from the complete view."""
    direct, _replaced, launch_cwd = _retarget_shadow_paths(
        command, game_root, view)
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-direct", str(launch_cwd or view),
    ], direct)
    # ``flatpak-spawn --host`` starts from the desktop-session environment,
    # not from Popen's sandbox-side env. Forward the pinned Steam app ID and
    # any explicit Launch Options variables before the host shell starts.
    _forward_flatpak_host_environment(wrapper, env, directory=game_root)
    return [*wrapper, *host_command]


def wrap_command(game, command: list[str],
                 env: dict[str, str] | None = None) -> list[str]:
    """Wrap *command* in the deployed profile's private game view."""
    if not command:
        raise RuntimeError("No launch command was supplied to the profile VFS.")

    payload = _load_manifest(game)
    state = manifest_path(game).parent
    backend = payload.get("backend", BACKEND_KERNEL)

    if backend == BACKEND_SHADOW:
        (view, view_data, _data_rel, game_root, data_root,
         _root_upper, _data_upper) = _validated_shadow_paths(
            game, payload, state)
        for label, path in (
            ("game root", game_root),
            ("shadow root", view),
            ("shadow data", view_data),
        ):
            if not path.is_dir():
                raise RuntimeError(f"Profile VFS {label} is missing: {path}")
        bind_at_game_root = bool(
            getattr(game, "vfs_bind_launch_at_game_root", False))
        if bind_at_game_root:
            bound_runtime = _bound_shadow_steam_runtime_command(
                command, game_root, view, env)
            if bound_runtime is not None:
                return bound_runtime
        if not bind_at_game_root:
            direct_umu = _direct_shadow_umu_command(
                command, game_root, view, env)
            if direct_umu is not None:
                return direct_umu
            direct_runtime = _direct_shadow_steam_runtime_command(
                command, game_root, view, env)
            if direct_runtime is not None:
                return direct_runtime
        if (not bind_at_game_root
                and getattr(game, "vfs_direct_shadow_launch", False)):
            return _direct_shadow_opt_in_command(
                command, game_root, view, env)
        ok, reason = _bubblewrap_status()
        if not ok:
            raise RuntimeError(f"Profile VFS is unavailable: {reason}.")
        wrapper, host_command = _place_wrapper_on_host(
            [_bubblewrap_binary() or "bwrap"], command)
        return [
            *wrapper,
            "--die-with-parent",
            "--dev-bind", "/", "/",
            "--bind", str(view), str(game_root),
            "--",
            *host_command,
        ]

    keys = (
        "game_root", "data_root", "root_layer", "data_layer",
        "root_upper", "data_upper", "root_work", "data_work",
    )
    paths = {key: Path(payload[key]) for key in keys}
    for key in ("game_root", "data_root", "root_layer", "data_layer"):
        if not paths[key].is_dir():
            raise RuntimeError(f"Profile VFS path is missing ({key}): {paths[key]}")
    for key in ("root_upper", "data_upper"):
        paths[key].mkdir(parents=True, exist_ok=True)
    for key in ("root_work", "data_work"):
        _empty_work_dir(paths[key], state)

    if paths["root_upper"].stat().st_dev != paths["root_work"].stat().st_dev:
        raise RuntimeError("Profile VFS root upper/work directories are on different filesystems.")
    if paths["data_upper"].stat().st_dev != paths["data_work"].stat().st_dev:
        raise RuntimeError("Profile VFS data upper/work directories are on different filesystems.")

    if backend == BACKEND_FUSE:
        ok, reason = fuse_overlay_status()
        if not ok:
            raise RuntimeError(
                f"Profile VFS fuse-overlayfs backend is unavailable: {reason}."
            )
        runtime = Path(payload.get("runtime") or state / RUNTIME_NAME)
        _assert_under(runtime, state, "runtime")
        if not runtime.is_file():
            raise RuntimeError(
                f"Profile VFS runtime is missing: {runtime}. Redeploy the profile."
            )
        wrapper, host_command = _place_wrapper_on_host(
            [
                "/bin/sh", str(runtime), str(state),
                str(paths["game_root"]), str(paths["data_root"]),
                str(paths["root_layer"]), str(paths["data_layer"]),
                str(paths["root_upper"]), str(paths["data_upper"]),
                str(paths["root_work"]), str(paths["data_work"]), "--",
            ],
            command,
        )
        return [*wrapper, *host_command]

    if backend != BACKEND_KERNEL:
        raise RuntimeError(
            f"Profile VFS manifest selects an unknown backend: {backend!r}."
        )
    ok, reason = bubblewrap_status()
    if not ok:
        raise RuntimeError(
            f"Profile VFS native OverlayFS backend is unavailable: {reason}."
        )
    wrapper, host_command = _place_wrapper_on_host(
        [_bubblewrap_binary() or "bwrap"], command)

    return [
        *wrapper,
        "--die-with-parent",
        "--dev-bind", "/", "/",
        "--overlay-src", str(paths["game_root"]),
        "--overlay-src", str(paths["root_layer"]),
        "--overlay", str(paths["root_upper"]), str(paths["root_work"]),
        str(paths["game_root"]),
        "--overlay-src", str(paths["data_root"]),
        "--overlay-src", str(paths["data_layer"]),
        "--overlay", str(paths["data_upper"]), str(paths["data_work"]),
        str(paths["data_root"]),
        "--",
        *host_command,
    ]


def _mapped_virtual_relative(game, relative: str | Path) -> Path:
    """Return a validated path relative to the published outer view."""
    mapper = getattr(game, "vfs_relative_path", None)
    rel = Path(mapper(relative) if callable(mapper) else relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError(
            f"VFS paths must remain relative to the game view: {relative}"
        )
    return rel


def _virtual_file_location(game, relative: str | Path) -> tuple[dict, Path] | None:
    """Return the manifest and actual-cased outer-view path for a file."""
    try:
        payload = _load_manifest(game)
        rel = _mapped_virtual_relative(game, relative)
    except RuntimeError:
        return None
    if payload.get("backend") == BACKEND_SHADOW:
        try:
            state = manifest_path(game).parent
            view, _view_data, _data_rel, *_rest = _validated_shadow_paths(
                game, payload, state)
            candidate = view / rel
            if not candidate.is_file():
                candidate = _resolve_nocase(view, rel.as_posix())
            if candidate is None or not candidate.is_file():
                return None
            return payload, candidate.relative_to(view)
        except (KeyError, RuntimeError):
            return None

    game_root = Path(payload["game_root"])
    data_root = Path(payload["data_root"])
    try:
        data_rel = data_root.relative_to(game_root)
    except ValueError:
        data_rel = Path(data_root.name)
    rel_key = tuple(part.casefold() for part in rel.parts)
    data_key = tuple(part.casefold() for part in data_rel.parts)
    if data_key and rel_key[:len(data_key)] == data_key:
        inner = Path(*rel.parts[len(data_key):])
        for key in ("data_upper", "data_layer", "data_root"):
            base = Path(payload[key])
            candidate = base / inner
            if not candidate.is_file():
                candidate = _resolve_nocase(base, inner.as_posix())
            if candidate is not None and candidate.is_file():
                return payload, data_rel / candidate.relative_to(base)
        return None
    for base in (Path(payload["root_layer"]), game_root):
        candidate = base / rel
        if not candidate.is_file():
            candidate = _resolve_nocase(base, rel.as_posix())
        if candidate is not None and candidate.is_file():
            return payload, candidate.relative_to(base)
    return None


def virtual_file_path(game, relative: str | Path) -> Path | None:
    """Logical game-root path for a file, preserving its published casing."""
    location = _virtual_file_location(game, relative)
    if location is None:
        return None
    payload, rel = location
    if payload.get("backend") == BACKEND_SHADOW:
        try:
            state = manifest_path(game).parent
            (_view, _view_data, _data_rel, game_root, _data_root,
             _root_upper, _data_upper) = _validated_shadow_paths(
                game, payload, state)
        except RuntimeError:
            return None
        return game_root / rel
    return Path(payload["game_root"]) / rel


def virtual_file(game, relative: str) -> bool:
    """Whether a root-relative file exists anywhere in the resolved VFS."""
    return virtual_file_path(game, relative) is not None


def virtual_data_write_path(game, relative: str | Path) -> Path:
    """Return a managed data-layer path for deploy-generated VFS content."""
    payload = _load_manifest(game)
    if payload.get("backend") == BACKEND_SHADOW:
        state = manifest_path(game).parent
        (_view, data_layer, _data_rel, _game_root, _data_root,
         _root_upper, _data_upper) = _validated_shadow_paths(
            game, payload, state)
    else:
        data_layer = Path(payload["data_layer"])
    target = data_layer / Path(relative)
    _assert_under(target, data_layer, "data write")
    return target


def virtual_root_write_path(game, relative: str | Path) -> Path:
    """Return a managed root-layer path for deploy-generated VFS content."""
    payload = _load_manifest(game)
    if payload.get("backend") == BACKEND_SHADOW:
        state = manifest_path(game).parent
        (root_layer, _view_data, _data_rel, _game_root, _data_root,
         _root_upper, _data_upper) = _validated_shadow_paths(
            game, payload, state)
    else:
        root_layer = Path(payload["root_layer"])
    target = root_layer / Path(relative)
    _assert_under(target, root_layer, "root write")
    return target


def prefer_virtual_executable(game, command: list[str], relative: str) -> list[str]:
    """Replace the game executable in a launcher's passthrough command."""
    target_path = virtual_file_path(game, relative)
    if target_path is None:
        return list(command)
    target = str(target_path)
    declared = [getattr(game, "exe_name", "")]
    declared.extend(getattr(game, "exe_name_alts", None) or [])
    declared.extend(getattr(game, "direct_launch_exes", None) or [])
    launcher_names = {Path(name).name.casefold() for name in declared if name}
    out = list(command)
    for index in range(len(out) - 1, -1, -1):
        token_name = out[index].strip('"').replace("\\", "/").rsplit("/", 1)[-1]
        if token_name.casefold() in launcher_names:
            out[index] = target
            return out
    raise RuntimeError(
        f"The launcher did not pass a {getattr(game, 'name', 'game')} "
        "executable; check the generated wrapper settings. Received: "
        + shlex.join(command)
    )


def cleanup_deployment(game, *, preserve_upper: bool = True, log_fn=None) -> None:
    """Remove the published view/layers; optionally retain profile writes."""
    _log = log_fn or (lambda _message: None)
    state = state_dir(game)
    try:
        _validate_state_root(state)
    except RuntimeError as exc:
        # This profile is still discoverably deployed, but proceeding could
        # delete outside it.  Mark the failure as authoritative so deploy
        # orchestration cannot dismiss it as a harmless first-deploy error.
        raise RestoreIncompleteError(str(exc)) from exc
    if not os.path.lexists(state):
        return
    pending = state / PENDING_NAME
    incomplete_view = state / INCOMPLETE_VIEW_NAME
    # A cleanup can fail because a runtime still holds a mount or because the
    # underlying drive becomes unavailable. Publish the retry marker before
    # removing the manifest and retain it until every managed path is gone, so
    # profile discovery can always direct a later Restore back here.
    write_atomic_text(pending, "cleanup\n")
    manifest = state / MANIFEST_NAME
    if manifest.is_file() and not incomplete_view.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                _capture_shadow_runtime(
                    game, payload, state, log_fn=_log,
                    retain_root=preserve_upper,
                )
        except (OSError, ValueError, RuntimeError) as exc:
            _log(f"  WARN: could not capture the VFS shadow view: {exc}")
    elif manifest.is_file():
        _log(
            "VFS: removing an unfinalized view without capturing partial "
            "deploy output."
        )
    if (preserve_upper and _uses_root_folder_runtime(game)
            and _legacy_shadow_upper(state)):
        root_upper = state / "root-upper"
        root_destination = _root_runtime_destination(game, state, root_upper)
        promoted = _promote_shadow_root_upper(root_upper, root_destination)
        if promoted:
            _log(
                f"VFS: moved {promoted} retained root file(s) into "
                "Root_Folder/."
            )
    for name in (
        MANIFEST_NAME, RUNTIME_NAME, "runtime.lock", "lower", "lower.build",
        "root-work", "data-work", SHADOW_NAME, SHADOW_BUILD_NAME,
        SHADOW_PREVIOUS_NAME, ROOT_SNAPSHOT_NAME, DATA_SNAPSHOT_NAME,
        INCOMPLETE_VIEW_NAME,
    ):
        path = state / name
        if path.is_dir() and not path.is_symlink():
            _remove_tree(path)
        else:
            path.unlink(missing_ok=True)
    if not preserve_upper:
        upper = state / "root-upper"
        if upper.is_dir() and not upper.is_symlink():
            _remove_tree(upper)
    # Delete the discovery marker last. Any exception above intentionally
    # leaves it in place and keeps the profile visible as deployed.
    pending.unlink(missing_ok=True)
    _log("VFS: unpublished the profile's private game view.")
