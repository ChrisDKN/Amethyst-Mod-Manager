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
from pathlib import Path
from typing import Iterable

from Utils.atomic_write import write_atomic_text
from Utils.deploy import (
    LinkMode,
    deploy_custom_rules,
    deploy_filemap,
    deploy_root_flagged_mods,
    deploy_root_folder,
)
from Utils.deploy_shared import _resolve_root_path
from Utils.deploy_shared import (
    _move_runtime_files,
    _transfer,
    _write_deploy_snapshot,
    create_probe_stub_dirs,
    deploy_case_alias_links,
)


STATE_DIR_NAME = ".amethyst-vfs"
MANIFEST_NAME = "manifest.json"
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

_CUSTOM_RULE_ARTIFACTS = (
    "custom_rules_deployed.txt",
    "custom_rules_backup",
    "custom_rules_prefix_backup",
)
_ROOT_DEPLOY_ARTIFACTS = (
    "root_folder_deployed.txt",
    "root_deploy_identities.json",
    "Root_Backup",
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


def manifest_path(game, profile: str | None = None) -> Path:
    return state_dir(game, profile) / MANIFEST_NAME


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
    return shutil.which("bwrap")


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


def _overwrite_entries(filemap: Path) -> set[str]:
    """Paths supplied by [Overwrite], which is mounted as Data's upper layer."""
    out: set[str] = set()
    with filemap.open(encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel, owner = line.split("\t", 1)
            if owner == "[Overwrite]":
                out.add(rel.replace("\\", "/").lower())
    return out


def _reject_symlink_payload(layer: Path) -> None:
    """A writable open through a lower-layer symlink could alter staging."""
    for dirpath, dirnames, filenames in os.walk(layer):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            candidate = base / name
            if candidate.is_symlink():
                raise RuntimeError(
                    "Profile VFS could not create a hardlink-safe layer; "
                    f"a symbolic link was produced at {candidate}. Move the "
                    "profile/staging folder to a hardlink-capable filesystem."
                )


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
            parent.mkdir(parents=True, exist_ok=True)

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

            _transfer(src, dst, LinkMode.HARDLINK)
            if dst.is_symlink():
                symlinked += 1
            else:
                try:
                    if os.path.samefile(src, dst):
                        linked += 1
                    else:
                        copied += 1
                except OSError:
                    copied += 1

    if move:
        _remove_tree(source)
    return linked, symlinked, copied


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


def _capture_shadow_runtime(payload: dict, state: Path, log_fn=None) -> int:
    """Move files created in a published shadow view into profile storage."""
    if payload.get("backend") != BACKEND_SHADOW:
        return 0
    view, view_data, data_rel = _shadow_paths(payload)
    if not view.is_dir() or not view_data.is_dir():
        return 0

    _log = log_fn or (lambda _message: None)
    moved_data = _move_runtime_files(
        view_data,
        state / DATA_SNAPSHOT_NAME,
        Path(payload["data_upper"]),
        log_fn=_log,
    )
    moved_root = _move_runtime_files(
        view,
        state / ROOT_SNAPSHOT_NAME,
        Path(payload["root_upper"]),
        log_fn=_log,
        exclude_dirs=(data_rel.as_posix(),),
    )
    moved = moved_data + moved_root
    if moved:
        _log(f"VFS: captured {moved} runtime-created file(s) from the shadow view.")
    return moved


def finalize_deployment(game, *, log_fn=None) -> None:
    """Snapshot a completed shadow deploy after all game-specific hooks run."""
    payload = _load_manifest(game)
    if payload.get("backend") != BACKEND_SHADOW:
        return
    state = manifest_path(game).parent
    view, view_data, data_rel = _shadow_paths(payload)
    _write_deploy_snapshot(
        view,
        state / ROOT_SNAPSHOT_NAME,
        log_fn=log_fn,
        exclude_dirs=(data_rel.as_posix(),),
    )
    _write_deploy_snapshot(
        view_data,
        state / DATA_SNAPSHOT_NAME,
        log_fn=log_fn,
    )


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

    game_root = Path(game.get_game_path()).resolve()
    raw_data_root = game.get_mod_data_path()
    if raw_data_root is None:
        raise RuntimeError("The game does not expose a primary mod-data directory.")
    data_root = Path(raw_data_root).resolve()
    if data_root.exists() and not data_root.is_dir():
        raise RuntimeError(f"Mod-data path is not a directory: {data_root}")

    state = state_dir(game, profile)
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

    state.mkdir(parents=True, exist_ok=True)
    existing_manifest = state / MANIFEST_NAME
    if existing_manifest.is_file():
        try:
            existing_payload = json.loads(
                existing_manifest.read_text(encoding="utf-8"))
            if isinstance(existing_payload, dict):
                _capture_shadow_runtime(existing_payload, state, log_fn=_log)
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
    custom_rules = list(getattr(game, "custom_routing_rules", None) or [])
    game_rules = [rule for rule in custom_rules if not rule.to_prefix]
    prefix_rules = [rule for rule in custom_rules if rule.to_prefix]

    # Route root/Data rules against a private synthetic game directory.
    file_exclude_normalized: set[str] = {
        str(path).replace("\\", "/").lower()
        for path in (file_exclude or ())
    }
    custom_exclude: set[str] = set(file_exclude_normalized)
    if game_rules:
        _log("VFS: resolving custom root/Data routing rules ...")
        try:
            custom_exclude |= deploy_custom_rules(
                filemap, routed_layer, staging,
                rules=game_rules,
                mode=LinkMode.HARDLINK,
                strip_prefixes=game.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes={},
                raw_mods=raw_mods,
                log_fn=_log,
                progress_fn=progress_fn,
            )
        finally:
            _remove_artifacts(metadata_dir, _CUSTOM_RULE_ARTIFACTS)

        try:
            data_rel = data_root.relative_to(game_root)
        except ValueError as exc:
            raise RuntimeError(
                f"VFS mod-data directory must be inside the game directory: {data_root}"
            ) from exc
        routed_data = routed_layer.joinpath(*data_rel.parts)
        if routed_data.is_dir():
            for child in list(routed_data.iterdir()):
                child.rename(data_layer / child.name)
            routed_data.rmdir()
        for child in list(routed_layer.iterdir()):
            child.rename(root_layer / child.name)
        routed_layer.rmdir()

    # Prefix routes (loose saves) intentionally remain real prefix state and
    # retain the normal restore manifest. They never write to the game root.
    if prefix_rules:
        custom_exclude |= deploy_custom_rules(
            filemap, game_root, staging,
            rules=prefix_rules,
            mode=LinkMode.HARDLINK,
            strip_prefixes=game.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_link_modes={},
            raw_mods=raw_mods,
            log_fn=_log,
            progress_fn=progress_fn,
            prefix_root=game.get_prefix_path(),
        )

    # Overwrite is materialized last and therefore wins. Do not duplicate its
    # winning entries into the temporary resolved mod layer.
    custom_exclude |= _overwrite_entries(filemap)
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
        linked_data, _placed = deploy_filemap(
            filemap, data_layer, staging,
            mode=LinkMode.HARDLINK,
            strip_prefixes=game.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=mapped_deploy or None,
            # Internal destinations must remain hardlinks in the private
            # materialization.  Only external separator targets inherit their
            # normal physical deploy mode or explicit separator override.
            per_mod_link_modes=external_link_modes,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            per_mod_subdirs=per_mod_subdirs,
        )
    finally:
        # Paths mapped into lower.build are disposable, but external separator
        # targets need the standard log/backup until Restore removes the
        # deployed files and puts any originals back.
        if not external_deploy_mods:
            _remove_artifacts(metadata_dir, _CUSTOM_DEPLOY_ARTIFACTS)

    linked_root = 0
    try:
        if root_folder_enabled:
            linked_root += deploy_root_folder(
                game.get_effective_root_folder_path(), root_payload,
                mode=LinkMode.HARDLINK, log_fn=_log)
        linked_root += deploy_root_flagged_mods(
            filemap.parent / "filemap_root.txt", root_payload, staging,
            mode=LinkMode.HARDLINK,
            strip_prefixes=game.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            excluded_raw=excluded_raw or None,
            log_fn=_log,
        )
    finally:
        _remove_artifacts(metadata_dir, _ROOT_DEPLOY_ARTIFACTS)

    # The root payload may itself contain files for the primary mod-data path;
    # merge those entries into the data payload before materialization.
    payload_data = root_payload
    for part in data_root.relative_to(game_root).parts:
        payload_data = next(
            (child for child in payload_data.iterdir()
             if child.name.casefold() == part.casefold() and child.is_dir()),
            payload_data / part,
        )
        if not payload_data.is_dir():
            break
    _merge_tree(payload_data, data_layer)
    _merge_tree(root_payload, root_layer)

    # Reject links supplied by mods before adding our own controlled, sibling-
    # relative aliases. A mod-provided link could make a writable open escape
    # into staging; the aliases below resolve only inside the private view.
    _reject_symlink_payload(build)

    data_upper = Path(game.get_effective_overwrite_path())
    data_upper.mkdir(parents=True, exist_ok=True)
    root_upper = state / "root-upper"
    root_upper.mkdir(parents=True, exist_ok=True)

    # Build a complete physical view. Base game files are linked first, then
    # resolved mod layers, followed by persistent root/overwrite content. The
    # final tree therefore has normal kernel dentries and exactly the same
    # winner ordering as the nested overlay implementation.
    shadow_build.mkdir(parents=True)
    base_counts = _materialize_tree(
        game_root, shadow_build, replace=False)
    data_rel = data_root.relative_to(game_root)
    shadow_data = shadow_build.joinpath(*data_rel.parts)
    shadow_data.mkdir(parents=True, exist_ok=True)
    _materialize_tree(root_layer, shadow_build, replace=True, move=True)
    _materialize_tree(data_layer, shadow_data, replace=True, move=True)
    _materialize_tree(root_upper, shadow_build, replace=True)
    _materialize_tree(
        data_upper,
        shadow_data,
        replace=True,
        exclude=file_exclude_normalized or None,
    )
    _remove_tree(build)

    if getattr(game, "case_alias_links", True):
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


def _uses_umu(command: list[str]) -> bool:
    """Whether *command* invokes umu-run, possibly behind another wrapper."""
    return any(Path(token).name == "umu-run" for token in command)


def _uses_steam_linux_runtime(command: list[str]) -> bool:
    """Whether *command* invokes Valve's pressure-vessel entry point."""
    return any(Path(token).name == "_v2-entry-point" for token in command)


def _retarget_shadow_paths(command: list[str], game_root: Path,
                           view: Path) -> tuple[list[str], bool]:
    """Rewrite existing absolute game-root arguments into *view*."""
    direct = list(command)
    replaced = False
    for index, token in enumerate(direct):
        candidate = Path(token)
        if not candidate.is_absolute():
            continue
        try:
            relative = candidate.relative_to(game_root)
        except ValueError:
            continue
        shadow_candidate = view / relative
        if not shadow_candidate.exists():
            continue
        direct[index] = str(shadow_candidate)
        replaced = True
    return direct, replaced


def _direct_shadow_umu_command(command: list[str], game_root: Path,
                               view: Path) -> list[str] | None:
    """Retarget an UMU game launch into the materialized shadow directory.

    UMU derives ``STEAM_COMPAT_INSTALL_PATH`` from the executable and starts
    Proton in its inherited working directory. Rewriting root-relative path
    arguments and changing that directory therefore gives UMU the same
    complete profile view without nesting two bubblewrap runtimes.
    """
    if not _uses_umu(command):
        return None
    direct, replaced = _retarget_shadow_paths(command, game_root, view)
    if not replaced:
        return None

    # Make UMU and its Proton subprocess inherit the shadow directory as cwd.
    # Keep this wrapper inside an existing flatpak-spawn --host prefix when the
    # manager itself is sandboxed.
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-umu", str(view),
    ], direct)
    return [*wrapper, *host_command]


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
    direct, replaced = _retarget_shadow_paths(command, game_root, view)
    if not replaced:
        return None

    shadow_env = _steam_runtime_shadow_env(
        env if env is not None else os.environ, game_root, view)
    if env is not None:
        env.update(shadow_env)

    # Explicitly apply these values inside a possible flatpak-spawn --host
    # prefix as that prefix was assembled before the VFS wrapper adjusted the
    # launch environment. The shell also gives Proton the shadow as its cwd.
    host_env = ["/usr/bin/env", *(
        f"{key}={value}" for key, value in shadow_env.items()), *direct]
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-steam-runtime", str(view),
    ], host_env)
    return [*wrapper, *host_command]


def _direct_shadow_opt_in_command(command: list[str], game_root: Path,
                                  view: Path) -> list[str]:
    """Run an explicitly compatible native handler from the complete view."""
    direct, _replaced = _retarget_shadow_paths(command, game_root, view)
    wrapper, host_command = _place_wrapper_on_host([
        "/bin/sh", "-c",
        'cd "$1" && shift && exec "$@"',
        "amethyst-vfs-direct", str(view),
    ], direct)
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
        view, view_data, _data_rel = _shadow_paths(payload)
        game_root = Path(payload["game_root"])
        data_root = Path(payload["data_root"])
        for label, path in (
            ("game root", game_root),
            ("shadow root", view),
            ("shadow data", view_data),
        ):
            if not path.is_dir():
                raise RuntimeError(f"Profile VFS {label} is missing: {path}")
        direct_umu = _direct_shadow_umu_command(command, game_root, view)
        if direct_umu is not None:
            return direct_umu
        direct_runtime = _direct_shadow_steam_runtime_command(
            command, game_root, view, env)
        if direct_runtime is not None:
            return direct_runtime
        if getattr(game, "vfs_direct_shadow_launch", False):
            return _direct_shadow_opt_in_command(command, game_root, view)
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


def virtual_file(game, relative: str) -> bool:
    """Whether a root-relative file exists anywhere in the resolved VFS."""
    try:
        payload = _load_manifest(game)
    except RuntimeError:
        return False
    rel = Path(relative)
    if payload.get("backend") == BACKEND_SHADOW:
        try:
            view, _view_data, _data_rel = _shadow_paths(payload)
            return (view / rel).is_file()
        except (KeyError, RuntimeError):
            return False

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
        return any((Path(payload[key]) / inner).is_file() for key in (
            "data_upper", "data_layer", "data_root",
        ))
    return (Path(payload["root_layer"]) / rel).is_file() or (
        game_root / rel).is_file()


def virtual_data_write_path(game, relative: str | Path) -> Path:
    """Return a managed data-layer path for deploy-generated VFS content."""
    payload = _load_manifest(game)
    data_layer = Path(payload["data_layer"])
    target = data_layer / Path(relative)
    _assert_under(target, data_layer, "data write")
    return target


def virtual_root_write_path(game, relative: str | Path) -> Path:
    """Return a managed root-layer path for deploy-generated VFS content."""
    payload = _load_manifest(game)
    root_layer = Path(payload["root_layer"])
    target = root_layer / Path(relative)
    _assert_under(target, root_layer, "root write")
    return target


def prefer_virtual_executable(game, command: list[str], relative: str) -> list[str]:
    """Replace the game executable in a launcher's passthrough command."""
    if not virtual_file(game, relative):
        return list(command)
    target = str(Path(game.get_game_path()) / relative)
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
    if not state.exists():
        return
    manifest = state / MANIFEST_NAME
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                _capture_shadow_runtime(payload, state, log_fn=_log)
        except (OSError, ValueError, RuntimeError) as exc:
            _log(f"  WARN: could not capture the VFS shadow view: {exc}")
    for name in (
        MANIFEST_NAME, RUNTIME_NAME, "runtime.lock", "lower", "lower.build",
        "root-work", "data-work", SHADOW_NAME, SHADOW_BUILD_NAME,
        SHADOW_PREVIOUS_NAME, ROOT_SNAPSHOT_NAME, DATA_SNAPSHOT_NAME,
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
    _log("VFS: unpublished the profile's private game view.")
