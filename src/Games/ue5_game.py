"""
ue5_game.py
Abstract base class for Unreal Engine 5 games.

UE5 games ship mods as files destined for multiple locations inside the game
root (pak files → Content/Paks/, esp/esm plugins → Content/Dev/ObvData/Data/,
UE4SS lua mods → Binaries/Win64/ue4ss/Mods/, etc.).

This base class handles the multi-target deploy/restore pattern.  Subclasses
declare their routing rules via ``ue5_routing_rules`` and fill in the usual
identity/path properties.

Routing rules
-------------
Each rule is a dict with at least:
  ``dest``  - path relative to game root where matching files are deployed
              (e.g. ``"Content/Paks"``)

Match criteria (one or more):
  ``extensions``  - list of lowercase dotted extensions, e.g. ``[".pak", ".utoc"]``
  ``folder``      - top-level folder name inside the mod (case-insensitive),
                    e.g. ``"ue4ss"`` - matches when the first path segment of
                    the staged file equals this string
  ``strip``       - optional list of leading path segments to strip from the
                    staged relative path before writing to ``dest``
                    (e.g. strip ``["Content/Paks", "Paks"]`` so a staged file at
                    ``Content/Paks/MyMod.pak`` deploys as ``Content/Paks/MyMod.pak``
                    rather than ``Content/Paks/Content/Paks/MyMod.pak``)

Rules are evaluated in order; the first match wins.  Files that match no rule
are deployed to ``ue5_default_dest`` (defaults to the game root itself).

Deploy workflow
---------------
Unlike traditional games, UE5 mod destinations are not folders full of vanilla
files - they are either empty or contain unrelated game content that must not
be touched.  Deploy therefore works without a Core backup:

  1. Place each mod file directly into its resolved game destination.
  2. Track every placed file path in a deployed.txt manifest.

Restore:
  1. Read deployed.txt and delete every listed file.
  2. Remove any directories that became empty.
  3. Delete deployed.txt.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from Games.base_game import BaseGame
from Utils.vfs import ProfileVFSGameMixin
from Utils.deploy import (
    LinkMode, RestoreIncompleteError, load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    expand_separator_deploy_paths, expand_separator_raw_deploy,
    expand_separator_link_modes, _resolve_nocase, _resolve_root_path,
    _write_deploy_snapshot, _move_runtime_files, _FILEMAP_SNAPSHOT_NAME,
)
from Utils.deploy_custom_rules import (
    deploy_custom_rules, restore_custom_rules, compute_prefix_handled,
    canonicalize_declared_folders,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.atomic_write import write_atomic_text

_PROFILES_DIR = get_profiles_dir()

# Manifest written next to filemap.txt so restore knows exactly what to remove
_DEPLOYED_MANIFEST = "ue5_deployed.txt"

# VFS keeps deliberate physical separator targets separate from the normal
# UE5 manifest. A private-layer build must never leave synthetic relative
# entries where physical restore could interpret them against the real game.
_VFS_EXTERNAL_DEPLOYED_MANIFEST = "ue5_vfs_external_deployed.txt"
_VFS_PREFIX_CONTEXT = "ue5_vfs_prefix_context.json"
_VFS_CUSTOM_BACKUP_TEMP_DIR = "ue5_custom_vanilla_backup.build"

# Vanilla files displaced by mod files are backed up here (inside the game root)
_VANILLA_BACKUP_DIR = "Amethyst_vanilla_files"

# Custom-dir vanilla files displaced by mod files are backed up here (inside profile root).
# Files are stored with their full absolute path mirrored so restore can reconstruct them.
_CUSTOM_VANILLA_BACKUP_DIR = "ue5_custom_vanilla_backup"

# Sentinel mod name the filemap uses for the overwrite folder (see Utils.filemap)
_OVERWRITE_NAME = "[Overwrite]"


def _build_overwrite_lookup(
    overwrite_dir: Path,
    strip_prefixes: "set[str] | None",
) -> dict[str, Path]:
    """Index the overwrite folder by strip-normalised relative path.

    The overwrite folder stores files in DEPLOYED layout (restore moves
    runtime-generated files there under their game-root-relative paths), but
    the filemap index applied ``mod_folder_strip_prefixes`` when it scanned
    the folder - e.g. ``Binaries/Win64/ue4ss/Mods/X/a.txt`` was indexed as
    ``ue4ss/Mods/X/a.txt``.  Re-apply the same leading-segment strip here so
    a filemap entry can be mapped back to its real on-disk file regardless
    of how many wrapper folders the deployed layout carries.
    """
    strip_set = {s.lower() for s in (strip_prefixes or ())}
    lookup: dict[str, Path] = {}
    stack: list[tuple[str, str]] = [("", str(overwrite_dir))]
    while stack:
        prefix, current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((prefix + entry.name + "/", entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        stripped = prefix + entry.name
                        while "/" in stripped:
                            first, rest = stripped.split("/", 1)
                            if first.lower() in strip_set:
                                stripped = rest
                            else:
                                break
                        lookup[stripped.lower()] = Path(entry.path)
        except OSError:
            continue
    return lookup


# ---------------------------------------------------------------------------
# Routing rule dataclass
# ---------------------------------------------------------------------------

@dataclass
class UE5Rule:
    """A single file-routing rule for a UE5 game.

    Attributes:
        dest:       Game-root-relative destination directory.
        extensions: Match files with these lowercase extensions (e.g. ".pak").
        folder:     Match files whose first staged path segment equals this
                    value (case-insensitive), e.g. "ue4ss".
        folder_anywhere:
                    Match files where this folder name appears as any path
                    segment (case-insensitive), not just the first one. The
                    prefix above the matched segment is stripped automatically.
                    Used by user-defined custom rules so a folder rule like
                    "folder2" matches "Binaries/Win64/folder1/folder2/file"
                    and lands at "<dest>/folder2/file".
        prefix:     Match files whose staged path starts with this multi-segment
                    prefix (case-insensitive), e.g. "Binaries/Win64/ue4ss".
                    More specific than ``folder`` - checked first.
        filenames:  Match files whose basename (case-insensitive) is in this
                    list, e.g. ["enabled.txt"].  Checked after prefix/folder.
        strip:      Path prefixes to strip from the staged relative path
                    before placing the file inside ``dest``.
                    Checked longest-first so more-specific prefixes win.
    flatten:    When True, reduce the final path to just the filename,
                    discarding all directory components.  Useful for files
                    that must land flat in ``dest`` regardless of how they
                    are packaged inside the mod folder (e.g. .bk2 movies).
    loose_only: When True, the rule only matches files that are not inside
                    any folder (i.e. files with no directory components in
                    their relative path).  Default False.
    include_siblings:
                    When True, a single match drags the matched file's
                    *containing folder* along: every file under that folder
                    (in the same mod) is routed to ``dest`` too, preserving
                    its path relative to the containing folder. The matched
                    file lands at ``dest/<container_name>/<filename>``;
                    flatten is ignored for these matches. Resolution
                    happens in ``_resolve_filemap_entries`` since per-entry
                    resolution can't see siblings.
    """
    dest: str
    extensions: list[str] = field(default_factory=list)
    folder: str = ""
    folder_anywhere: str = ""
    prefix: str = ""
    filenames: list[str] = field(default_factory=list)
    strip: list[str] = field(default_factory=list)
    flatten: bool = False
    loose_only: bool = False
    include_siblings: bool = False


def _declared_folders(rule: UE5Rule) -> tuple[str, ...]:
    """Folder names *rule* spells out, for casing canonicalisation."""
    return tuple(n for n in (rule.prefix, rule.folder, rule.folder_anywhere) if n)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class UE5Game(ProfileVFSGameMixin, BaseGame):
    """Abstract base for Unreal Engine 5 games with multi-target mod routing."""

    _PREFIX_SKIP_DEST = "__prefix_skip__"
    vfs_root_payload_targets_data = True

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
    )

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        """UE4SS loads a lua mod's entry point from ``Scripts/main.lua`` (capital
        S) but discovers the mod folder by a lowercase ``scripts`` check - see
        UE4SS ``setup_mods()`` vs ``queue_start_lua_mod_by_path()``. On Windows
        the filesystem is case-insensitive so either casing works, but under
        Proton on a case-sensitive Linux filesystem a mod shipping a lowercase
        ``scripts`` folder is discovered yet fails to load its ``main.lua``.

        Pinning every ``scripts`` segment to ``Scripts`` at deploy time makes the
        casing UE4SS's loader expects, and also makes this manager's own
        ``Scripts/main.lua`` check in ``_collect_deployed_ue4ss_folders`` match,
        so lowercase-shipped mods get a ``mods.txt`` entry too.

        Every UE5 game (and UE5-deploy custom games, which merge this in) shares
        this default.
        """
        return {"scripts": "Scripts"}

    # -----------------------------------------------------------------------
    # Routing rules
    # -----------------------------------------------------------------------
    # The default ``ue5_routing_rules`` composition is:
    #
    #     [
    #         *_ue5_shared_pre_rules,            # LogicMods, pak, UE4SS normalisation
    #         *self._ue5_pre_passthrough_rules,  # game-specific specific-folder rules
    #         UE5Rule(dest="", folder="binaries"),
    #         UE5Rule(dest="", folder="content"),
    #         *self._ue5_post_passthrough_rules, # game-specific generic-extension rules
    #     ]
    #
    # Subclasses normally only need to override the two hook properties below.
    # Override ``ue5_routing_rules`` directly for fully custom orderings.

    @property
    def _ue5_shared_pre_rules(self) -> list[UE5Rule]:
        """Routing rules common to every UE5 game: LogicMods folder placement,
        .pak/.utoc/.ucas → ``Content/Paks/~mods``, and UE4SS folder
        normalisation.  Evaluated before the generic ``binaries``/``content``
        pass-through pair."""
        return [
            # LogicMods folder → Content/Paks/LogicMods/ (preserved as a folder
            # under Paks). Must come before the .pak extension rule so files
            # inside LogicMods don't get routed to ~mods/.
            UE5Rule(dest="Content/Paks", prefix="Content/Paks/LogicMods",
                    strip=["Content/Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", prefix="Paks/LogicMods",
                    strip=["Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", folder="LogicMods", flatten=True),
            # Pak / streaming files → Content/Paks/~mods/
            UE5Rule(
                dest="Content/Paks/~mods",
                extensions=[".pak", ".utoc", ".ucas"],
                strip=["Content/Paks/~mods", "Content/Paks/~Mods", "Content/Paks", "Paks", "Content", "~mods", "~Mods"],
                flatten=True,
            ),
            # Files already inside Content/Paks/~Mods (any casing) → normalise
            # to lowercase ~mods dest so only one folder is created on disk.
            UE5Rule(
                dest="Content/Paks/~mods",
                prefix="Content/Paks/~Mods",
                strip=["Content/Paks/~Mods", "Content/Paks/~mods"],
                flatten=True,
            ),
            # Mods shipping Binaries/Win64/UE4SS/… → normalise to lowercase
            # ue4ss dest so only one folder is ever created on disk.
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                prefix="Binaries/Win64/UE4SS",
                strip=["Binaries/Win64/UE4SS", "Binaries/Win64/ue4ss"],
                flatten=True,
            ),
            # ue4ss/ or UE4SS/ top-level folder → Binaries/Win64/ue4ss/
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss", "UE4SS"],
                flatten=True,
            ),
        ]

    @property
    def _ue5_pre_passthrough_rules(self) -> list[UE5Rule]:
        """Game-specific rules inserted between the shared pre-rules and the
        ``binaries``/``content`` pass-through.  Use this for rules that target
        a *specific* sub-folder (e.g. ``Content/Movies/Modern``) and must beat
        the generic ``folder="content"`` catch-all."""
        return []

    @property
    def _ue5_post_passthrough_rules(self) -> list[UE5Rule]:
        """Game-specific rules appended after the ``binaries``/``content``
        pass-through.  Use this for the per-game UE4SS Mods rule, Bink/.asi
        plugins, and the trailing loose-runtime ``[".dll", ".pdb"]`` rule."""
        return []

    def _custom_rules_as_ue5_rules(self) -> list[UE5Rule]:
        """Convert this game's ``custom_routing_rules`` (CustomRule) into
        UE5Rules for the manifest-deploy pipeline.

        Each CustomRule may name multiple folders, so it expands into one
        UE5Rule per folder; extension-/filename-only rules produce a single
        UE5Rule.  ``to_prefix`` rules are skipped here - the manifest deploy
        can't route into the Wine/Proton prefix, so those are honoured
        separately by ``deploy_custom_rules`` (see ``_prefix_routing_rules``).
        ``companion_extensions`` have no UE5Rule equivalent and are applied by
        ``_apply_companion_routing``.
        """
        rules: list[UE5Rule] = []
        for cr in self.custom_routing_rules:
            if getattr(cr, "to_prefix", False):
                continue
            if cr.folders:
                for folder in cr.folders:
                    norm_folder = folder.replace("\\", "/").strip("/")
                    exts = list(cr.extensions)
                    fnames = list(cr.filenames)
                    if "/" in norm_folder:
                        # Multi-segment: primary prefix rule. When flatten is
                        # ON, strip everything ABOVE the last segment so the
                        # matched folder (last segment) + contents land under
                        # dest. Parent of "Content/Paks/LogicMods" → strip
                        # "Content/Paks".
                        parent_strip = norm_folder.rsplit("/", 1)[0]
                        rules.append(UE5Rule(
                            dest=cr.dest, extensions=exts,
                            prefix=norm_folder, filenames=fnames,
                            strip=[parent_strip],
                            loose_only=cr.loose_only,
                            flatten=cr.flatten,
                            include_siblings=cr.include_siblings,
                        ))
                        # Also generate prefix rules for common UE5 packaging
                        # prefixes above the target folder (Paks, Content,
                        # Content/Paks). Strip the prefix above the matched
                        # folder for the flatten=True case.
                        ue5_prefixes = ["Paks", "Content/Paks", "Content"]
                        for ue_pfx in ue5_prefixes:
                            full = f"{ue_pfx}/{norm_folder}"
                            if full.lower() == norm_folder.lower():
                                continue
                            full_parent = f"{ue_pfx}/{parent_strip}"
                            rules.append(UE5Rule(
                                dest=cr.dest, extensions=exts,
                                prefix=full, filenames=fnames,
                                strip=[full_parent],
                                loose_only=cr.loose_only,
                                flatten=cr.flatten,
                                include_siblings=cr.include_siblings,
                            ))
                    else:
                        # Single-segment: match the folder name anywhere in
                        # the path; the prefix above it is auto-stripped so
                        # the matched folder + contents land under dest.
                        rules.append(UE5Rule(
                            dest=cr.dest, extensions=exts,
                            folder_anywhere=norm_folder, filenames=fnames,
                            loose_only=cr.loose_only,
                            flatten=cr.flatten,
                            include_siblings=cr.include_siblings,
                        ))
            elif cr.filenames:
                rules.append(UE5Rule(
                    dest=cr.dest,
                    extensions=list(cr.extensions),
                    filenames=list(cr.filenames),
                    loose_only=cr.loose_only,
                    flatten=cr.flatten,
                    include_siblings=cr.include_siblings,
                ))
            else:
                rules.append(UE5Rule(
                    dest=cr.dest,
                    extensions=list(cr.extensions),
                    loose_only=cr.loose_only,
                    flatten=cr.flatten,
                    include_siblings=cr.include_siblings,
                ))
        return rules

    @property
    def ue5_routing_rules(self) -> list[UE5Rule]:
        """Ordered list of routing rules.  First match wins.

        The game's ``custom_routing_rules`` are converted to UE5Rules and go
        FIRST so they take priority over the built-in UE5 defaults
        (``shared_pre + pre_passthrough + binaries/content + post_passthrough``),
        which act as fallbacks.  Override directly only if you need a
        fundamentally different layout."""
        return [
            *self._custom_rules_as_ue5_rules(),
            *self._ue5_shared_pre_rules,
            *self._ue5_pre_passthrough_rules,
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            *self._ue5_post_passthrough_rules,
        ]

    @property
    def ue5_default_dest(self) -> str:
        """Destination for files that match no rule.  Defaults to game root."""
        return ""

    # -----------------------------------------------------------------------
    # Archive (pak) conflict detection
    # -----------------------------------------------------------------------

    @property
    def archive_extensions(self) -> frozenset[str]:
        """Scan UE .pak and IoStore .utoc TOCs so mods that ship the same
        asset paths inside different archives get archive-conflict flags
        (Utils.ue_pak_reader).  Companion .ucas files hold only bulk data
        (no names) and are skipped."""
        return frozenset({".pak", ".utoc"})

    @property
    def archive_plugin_ordering(self) -> bool:
        """UE pak mounting is not plugin-driven - archive conflict winners
        follow mod priority, not any plugin load order."""
        return False

    def reshade_install_subdir(self, game_path: "Path") -> "Path | None":
        """ReShade must sit next to the real rendering binary, which in Unreal
        Engine games lives under ``<Project>/Binaries/Win64/`` rather than the
        bootstrap launcher at the game root (the official ReShade installer
        follows the launcher's embedded ``IDI_EXEC_FILE`` resource to find it).

        Locate the ``Binaries/Win64`` folder that holds the shipping exe and
        return it relative to *game_path*.  Falls back to the base behaviour
        (parent of :attr:`exe_name`) if the layout can't be found on disk.
        """
        if game_path is not None and Path(game_path).is_dir():
            root = Path(game_path)
            candidates: list[Path] = []
            # get_game_path() already resolves to the UE project root, so
            # Binaries/Win64 usually sits directly under it; allow a couple of
            # nesting levels in case a handler points at the install root.
            for pattern in ("Binaries/Win64", "*/Binaries/Win64", "*/*/Binaries/Win64"):
                for win64 in root.glob(pattern):
                    if win64.is_dir():
                        candidates.append(win64)
                if candidates:
                    break

            def _has_shipping_exe(d: Path) -> bool:
                try:
                    return any(p.name.lower().endswith("-shipping.exe") for p in d.iterdir())
                except OSError:
                    return False

            # Prefer a Win64 folder that actually contains a *-Shipping.exe.
            best = next((c for c in candidates if _has_shipping_exe(c)), None)
            if best is None and candidates:
                best = min(candidates, key=lambda p: len(p.parts))
            if best is not None:
                try:
                    return best.relative_to(root)
                except ValueError:
                    return best

        # Disk probe failed (game not installed yet, etc.). UE projects always
        # render from Binaries/Win64 relative to the project root, so default
        # there rather than the bootstrap launcher's folder.
        return Path("Binaries/Win64")

    # -----------------------------------------------------------------------
    # Paths (concrete default - subclasses may override)
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        return self._game_path

    def get_vfs_game_root(self) -> Path | None:
        """Full install root, including sibling Engine/ and bootstraps."""
        if self._game_path is None:
            return None
        configured = Path(self._game_path)
        exe_rel = Path(self.exe_name.replace("\\", "/"))
        # Configure historically pre-populated nested UE project roots. Walk a
        # few parents to recover the outer install when exe_name includes the
        # project prefix (MarvelGame/Marvel, OblivionRemastered, etc.).
        candidates = [configured, *list(configured.parents)[:3]]
        for candidate in candidates:
            hit = _resolve_nocase(candidate, exe_rel.as_posix())
            if hit is not None and hit.is_file():
                return candidate
        return configured

    def get_vfs_data_root(self) -> Path | None:
        """Nested UE project root used by all UE5 routing destinations."""
        return self.get_game_path()

    def vfs_relative_path(self, relative: str | Path) -> Path:
        """Translate project-relative framework paths to the outer install."""
        rel = Path(str(relative).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(
                f"UE5 VFS paths must be relative to the install: {relative}"
            )
        mount_root = self.get_vfs_game_root()
        project_root = self.get_vfs_data_root()
        if mount_root is None or project_root is None:
            return rel
        mount_root = Path(mount_root)
        project_root = Path(project_root)
        try:
            project_rel = project_root.resolve().relative_to(mount_root.resolve())
        except (OSError, ValueError):
            return rel
        rel_folded = tuple(part.casefold() for part in rel.parts)
        project_folded = tuple(part.casefold() for part in project_rel.parts)
        # Prefer an existing outer-root path and preserve its actual on-disk
        # casing. This covers top-level bootstraps and definitions whose
        # project prefix casing differs from the Linux install.
        outer_hit = _resolve_nocase(mount_root, rel.as_posix())
        if outer_hit is not None and outer_hit.is_file():
            return outer_hit.relative_to(mount_root)

        def _project_mapped(project_relative: Path) -> Path:
            # The loader itself can be mod-only, but its parent folders often
            # already exist in the vanilla project with unexpected casing.
            # Mirror the materializer's case-insensitive parent resolution so
            # virtual_file probes the exact path that was published.
            resolved = _resolve_root_path(project_root, project_relative)
            try:
                inner = resolved.relative_to(project_root)
            except ValueError:
                inner = project_relative
            return project_rel / inner

        if project_folded and rel_folded[:len(project_folded)] == project_folded:
            return _project_mapped(
                Path(*rel.parts[len(project_rel.parts):]))
        # Bootstrap executables and definitions already rooted at the outer
        # install win when present there. Mod-provided loaders/framework files
        # otherwise use the project-relative convention of UE5 handlers.
        return _project_mapped(rel)

    def _ue5_active_deploy_root(self) -> Path | None:
        target = getattr(self, "_vfs_ue5_target_root", None)
        return Path(target) if target is not None else self.get_game_path()

    def _ue5_deployed_manifest_path(self) -> Path:
        target = getattr(self, "_vfs_ue5_manifest_path", None)
        if target is not None:
            return Path(target)
        return self.get_profile_root() / _DEPLOYED_MANIFEST

    def _ue5_runtime_snapshot_path(self) -> Path:
        target = getattr(self, "_vfs_ue5_snapshot_path", None)
        if target is not None:
            return Path(target)
        return self.get_profile_root() / _FILEMAP_SNAPSHOT_NAME

    def _vfs_external_manifest_path(self, profile: str | None = None) -> Path:
        from Utils.vfs import state_dir
        return state_dir(self, profile) / _VFS_EXTERNAL_DEPLOYED_MANIFEST

    def _vfs_prefix_context_path(self, profile: str | None = None) -> Path:
        from Utils.vfs import state_dir
        return state_dir(self, profile) / _VFS_PREFIX_CONTEXT

    def _write_vfs_prefix_context(
        self, filemap: Path, profile: str | None = None,
    ) -> None:
        context_path = self._vfs_prefix_context_path(profile)
        prefix_root = self.get_prefix_path()
        if not self._prefix_routing_rules() or prefix_root is None:
            context_path.unlink(missing_ok=True)
            return
        context_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_text(
            context_path,
            json.dumps({
                "filemap": str(Path(filemap)),
                "prefix_root": str(Path(prefix_root)),
            }, indent=2, sort_keys=True) + "\n",
        )

    def _restore_vfs_prefix_targets(
        self, log_fn, profile: str | None = None,
    ) -> None:
        """Restore prefix routes using the paths captured at VFS deploy."""
        context_path = self._vfs_prefix_context_path(profile)
        filemap = self.get_effective_filemap_path()
        prefix_root = self.get_prefix_path()
        if context_path.is_file():
            try:
                payload = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    saved_filemap = payload.get("filemap")
                    saved_prefix = payload.get("prefix_root")
                    if saved_filemap:
                        filemap = Path(saved_filemap)
                    if saved_prefix:
                        prefix_root = Path(saved_prefix)
            except (OSError, ValueError):
                pass

        metadata = Path(filemap).parent
        artifacts = (
            metadata / "custom_rules_deployed.txt",
            metadata / "custom_rules_prefix_backup",
        )
        if artifacts[0].is_file():
            restore_custom_rules(
                Path(filemap),
                Path(self.get_vfs_game_root() or self._game_path),
                rules=[],
                log_fn=log_fn,
                prefix_root=Path(prefix_root) if prefix_root is not None else None,
            )
        elif artifacts[1].is_dir() and prefix_root is not None:
            # Interrupted placement can move an original into the backup
            # before the deployed-path log is published.
            from Utils.deploy_shared import _restore_backup_dir
            _restore_backup_dir(artifacts[1], Path(prefix_root), log_fn)
        if any(path.exists() for path in artifacts):
            raise RestoreIncompleteError(
                "Could not fully restore UE5 VFS files routed into the game prefix."
            )
        context_path.unlink(missing_ok=True)

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Routing helpers
    # -----------------------------------------------------------------------

    def _match_rule(
        self, rel_str: str,
    ) -> tuple[UE5Rule, list[str], bool] | None:
        """Return (rule, dynamic_strip, is_folder_match) for the first match,
        or None.

        ``dynamic_strip`` defaults to ``rule.strip`` but is overridden for
        ``folder_anywhere`` matches to strip the prefix above the matched
        segment so the matched folder is preserved under ``dest``.

        ``is_folder_match`` is True when the match came from folder/prefix/
        folder_anywhere (an "anchored" match where the matched folder should
        be preserved under dest when flatten=True), False for ext/filename-
        only matches (where flatten=True means bare filename).

        Extension matching uses filename-suffix logic so multi-dot extensions
        like ".dekcns.json" can be configured (Path.suffix returns only the
        last suffix). Within a rule, the longest extension is matched first.
        """
        norm = rel_str.replace("\\", "/")
        parts = norm.split("/")
        first_seg = parts[0].lower() if parts else ""
        basename = parts[-1].lower() if parts else ""
        is_loose = len(parts) == 1
        lower_segs = [p.lower() for p in parts]

        def _ext_hit(exts: list[str]) -> bool:
            # Longest-first so ".dekcns.json" wins over ".json" within the
            # rule's own list. Comparison is case-insensitive (basename is
            # already lowercased; rule.extensions is normalised on entry).
            for e in sorted(exts, key=len, reverse=True):
                el = e.lower()
                if basename.endswith(el) and len(basename) > len(el):
                    return True
            return False

        def _name_hit(names: list[str]) -> bool:
            # Filenames support glob patterns (``*``, ``?``, ``[seq]``) so
            # rules can target e.g. ``*.dekcns.json``. Plain names still match
            # by exact case-insensitive equality.
            for n in names:
                nl = n.lower()
                if any(c in nl for c in "*?["):
                    if fnmatch.fnmatchcase(basename, nl):
                        return True
                elif basename == nl:
                    return True
            return False

        for rule in self.ue5_routing_rules:
            # loose_only on prefix/folder/folder_anywhere: matched folder
            # must be at the top level (handled inline below).
            # loose_only on ext/filename-only: file itself must be loose
            # (handled by the late check before ext/filename branches).
            if rule.prefix and norm.lower().startswith(rule.prefix.lower() + "/"):
                # If the rule also has an extension filter, only match when
                # the file's extension is in the list.
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
                # loose_only: prefix must start at index 0 of the path
                # (always true since startswith already requires that), but
                # also require no segments above the prefix's last segment -
                # i.e. the prefix is anchored at the root.
                if rule.loose_only:
                    # startswith already anchors at root, so this is True.
                    pass
                return rule, rule.strip, True
            if rule.folder and first_seg == rule.folder.lower():
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
                # rule.folder always matches at the first segment, so
                # loose_only is automatically satisfied here.
                return rule, rule.strip, True
            if rule.folder_anywhere:
                target = rule.folder_anywhere.lower()
                # Search any directory segment (not the basename).
                hit_idx = -1
                for i, seg in enumerate(lower_segs[:-1]):
                    if seg == target:
                        hit_idx = i
                        break
                if hit_idx >= 0:
                    if rule.extensions and not _ext_hit(rule.extensions):
                        continue
                    # loose_only on folder_anywhere: matched folder must
                    # itself be at the top level.
                    if rule.loose_only and hit_idx != 0:
                        continue
                    if hit_idx == 0:
                        # Folder is already at root - no dynamic strip.
                        return rule, rule.strip, True
                    # Strip the prefix above the matched folder so the
                    # folder + contents land under dest.
                    dyn_prefix = "/".join(parts[:hit_idx])
                    return rule, [dyn_prefix, *rule.strip], True
            # For ext/filename-only rules, loose_only means the file itself
            # has no directory components.
            if rule.loose_only and not is_loose:
                continue
            if rule.filenames and _name_hit(rule.filenames):
                return rule, rule.strip, False
            if rule.extensions and _ext_hit(rule.extensions):
                return rule, rule.strip, False
        return None

    def _match_single_ue5_rule(
        self, rel_str: str, rule: "UE5Rule",
    ) -> tuple[list[str], bool] | None:
        """Run the same matching logic as ``_match_rule`` for a single rule.

        Returns ``(dyn_strip, is_folder_match)`` on a hit, else None. Used by
        ``_resolve_filemap_entries`` so include_siblings drags from earlier
        rules can claim files before later rules' primary matches run.
        """
        norm = rel_str.replace("\\", "/")
        parts = norm.split("/")
        first_seg = parts[0].lower() if parts else ""
        basename = parts[-1].lower() if parts else ""
        is_loose = len(parts) == 1
        lower_segs = [p.lower() for p in parts]

        def _ext_hit(exts):
            for e in sorted(exts, key=len, reverse=True):
                el = e.lower()
                if basename.endswith(el) and len(basename) > len(el):
                    return True
            return False

        def _name_hit(names):
            for n in names:
                nl = n.lower()
                if any(c in nl for c in "*?["):
                    if fnmatch.fnmatchcase(basename, nl):
                        return True
                elif basename == nl:
                    return True
            return False

        if rule.prefix and norm.lower().startswith(rule.prefix.lower() + "/"):
            if rule.extensions and not _ext_hit(rule.extensions):
                return None
            return rule.strip, True
        if rule.folder and first_seg == rule.folder.lower():
            if rule.extensions and not _ext_hit(rule.extensions):
                return None
            return rule.strip, True
        if rule.folder_anywhere:
            target = rule.folder_anywhere.lower()
            hit_idx = -1
            for i, seg in enumerate(lower_segs[:-1]):
                if seg == target:
                    hit_idx = i
                    break
            if hit_idx >= 0:
                if rule.extensions and not _ext_hit(rule.extensions):
                    return None
                if rule.loose_only and hit_idx != 0:
                    return None
                if hit_idx == 0:
                    return rule.strip, True
                dyn_prefix = "/".join(parts[:hit_idx])
                return [dyn_prefix, *rule.strip], True
        if rule.loose_only and not is_loose:
            return None
        if rule.filenames and _name_hit(rule.filenames):
            return rule.strip, False
        if rule.extensions and _ext_hit(rule.extensions):
            return rule.strip, False
        return None

    def _sibling_container(
        self, norm_rel: str, dyn_strip: list[str], is_folder_match: bool,
        mod_name: str,
    ) -> tuple[str, str] | None:
        """Return ``(container_path, container_name)`` for a matched entry.

        Container = topmost folder containing the matched file. See
        ``deploy_custom_rules._sibling_container`` for the rationale.
        """
        del dyn_strip, is_folder_match, mod_name  # unused
        if "/" not in norm_rel:
            return None
        container = norm_rel.split("/", 1)[0]
        return (container, container)

    def _apply_strip(self, rel_str: str, strips: list[str]) -> str:
        """Strip the longest matching prefix from rel_str (case-insensitive)."""
        norm = rel_str.replace("\\", "/")
        for prefix in sorted(strips, key=len, reverse=True):
            p = prefix.strip("/").lower()
            if norm.lower().startswith(p + "/"):
                return norm[len(p) + 1:]
        return norm

    def _resolve_entry(self, rel_str: str) -> tuple[str, str]:
        """Return (dest_rel, final_rel) for a filemap entry.

        dest_rel  - game-root-relative destination directory (may be "")
        final_rel - file path relative to dest_rel

        Placement under ``dest`` depends on rule.flatten:
        - flatten=False (default) - preserve the full mod-relative path under
          dest (no strip applied)
        - flatten=True + folder/prefix/folder_anywhere match - apply the
          rule's strip so the matched folder + contents land under dest.
          If the matched folder is already at the root (no parent to strip),
          the path is preserved as-is so the folder name itself is kept.
        - flatten=True + ext/filename-only match - bare filename under dest

        ``include_siblings`` only affects the matched file itself here (it
        lands at ``dest/<container_name>/<filename>``); resolving the
        sibling drag requires the full filemap and is handled by
        ``_resolve_filemap_entries``.

        Content-based LogicMods detection is likewise not applied here: it
        needs the owning mod name to find the archive on disk, and groups a
        pak with its same-stem .utoc/.ucas. Rule-based routing alone is what
        this returns, so a blueprint pak resolves to ``~mods`` here while
        ``_resolve_filemap_entries`` puts it in ``LogicMods``. Every consumer
        that decides real destinations (deploy, conflict keys, the Data tab)
        uses the whole-filemap resolver; this one is for single-path queries.
        """
        match = self._match_rule(rel_str)
        if match is not None:
            rule, dyn_strip, is_folder_match = match
            dest = rule.dest
            norm = rel_str.replace("\\", "/")
            # Per-entry resolution can't drag siblings (no view of other
            # entries), but it can still place the matched file inside its
            # container so single-file queries show the right destination.
            # Whole-mod drag (container_path == "") needs mod_name to form a
            # useful destination; without it we fall back to flatten/preserve.
            info = self._sibling_container(norm, dyn_strip, is_folder_match, "") \
                if rule.include_siblings else None
            container_path = info[0] if info is not None else None
            if info is not None and container_path:
                container_name = info[1]
                final_rel = container_name + "/" + norm[len(container_path) + 1:]
            elif rule.flatten:
                if is_folder_match:
                    # Folder/prefix/folder_anywhere match: strip parents above
                    # the matched folder, keep matched folder + contents.
                    # Empty strip means "no parent to strip" (folder is
                    # already at root) - preserve as-is.
                    final_rel = self._apply_strip(rel_str, dyn_strip) if dyn_strip else norm
                else:
                    # Ext/filename-only: bare filename.
                    final_rel = Path(norm).name
            else:
                # Preserve the full mod-relative path under dest.
                final_rel = norm
            final_rel = canonicalize_declared_folders(
                final_rel, _declared_folders(rule))
        else:
            dest = self.ue5_default_dest
            final_rel = rel_str.replace("\\", "/")
        return dest, final_rel

    def _prefix_routing_rules(self) -> list:
        """Subset of ``custom_routing_rules`` that target the Wine/Proton prefix.

        These are handled by ``deploy_custom_rules`` (with ``prefix_root`` set)
        before the UE5 manifest deploy runs, and are skipped by the manifest
        pipeline via the ``_PREFIX_SKIP_DEST`` sentinel.
        """
        return [r for r in self.custom_routing_rules
                if getattr(r, "to_prefix", False)]

    def _resolve_filemap_entries(
        self, entries: list[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        """Resolve a whole filemap at once, applying include_siblings drag.

        Returns ``[(staged_rel, mod_name, dest_rel, final_rel), ...]``.

        Per-entry resolution can't see other files; ``include_siblings``
        needs to know that file X under the matched file's containing folder
        in the same mod should ride along. This pass first runs
        ``_resolve_entry`` on every entry to find primaries, then for each
        ``include_siblings`` primary it overrides the resolution of every
        same-mod file under the same containing folder so they all land at
        ``dest/<container_name>/<rel-from-container>``.

        Entries claimed by ``custom_routing_rules`` with ``to_prefix=True`` are
        held out of UE5 rule resolution and re-appended with ``_PREFIX_SKIP_DEST``
        so the deploy loop and data tab know to leave them alone (they're placed
        separately by ``deploy_custom_rules`` with ``prefix_root`` set).
        """
        prefix_rules = self._prefix_routing_rules()
        prefix_handled: set[str] = set()
        if prefix_rules:
            prefix_handled, _ = compute_prefix_handled(
                entries, self.custom_routing_rules,
            )
            core_entries = [
                (sr, mn) for sr, mn in entries
                if sr.replace("\\", "/").lower() not in prefix_handled
            ]
        else:
            core_entries = entries

        # Default placement (no rule matches): default_dest + full path.
        default_dest = self.ue5_default_dest
        per_entry: list[tuple[str, str, str, str]] = [
            (sr, mn, default_dest, sr.replace("\\", "/"))
            for sr, mn in core_entries
        ]
        claimed: set[int] = set()

        # Process rules in declaration order. For each rule:
        #   1. Claim every still-unclaimed entry that this rule matches as
        #      a primary, placing it under rule.dest.
        #   2. If include_siblings, drag the container of every just-claimed
        #      primary so subsequent rules can't claim files inside it.
        # This ordering enforces "rule order wins" - an earlier rule's drag
        # pre-empts a later rule's primary match on the same files.
        for rule in self.ue5_routing_rules:
            new_primaries: list[tuple[int, list[str], bool]] = []
            for i, (staged_rel, mod_name) in enumerate(core_entries):
                if i in claimed:
                    continue
                hit = self._match_single_ue5_rule(staged_rel, rule)
                if hit is None:
                    continue
                dyn_strip, is_folder_match = hit
                norm = staged_rel.replace("\\", "/")
                declared = _declared_folders(rule)
                # Compute placement for this primary.
                if rule.include_siblings:
                    info = self._sibling_container(norm, dyn_strip, is_folder_match, mod_name)
                    if info is not None:
                        cont, cname = info
                        if cont:
                            tail = norm[len(cont) + 1:]
                        else:
                            tail = norm
                        final_rel = (cname + "/" + tail) if cname else tail
                        final_rel = canonicalize_declared_folders(final_rel, declared)
                        per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                        claimed.add(i)
                        new_primaries.append((i, dyn_strip, is_folder_match))
                        continue
                # Non-include_siblings: standard flatten / preserve placement.
                if rule.flatten:
                    if is_folder_match:
                        final_rel = self._apply_strip(staged_rel, dyn_strip) if dyn_strip else norm
                    else:
                        final_rel = Path(norm).name
                else:
                    final_rel = norm
                final_rel = canonicalize_declared_folders(final_rel, declared)
                per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                claimed.add(i)
            # Drag siblings for include_siblings primaries.
            if not rule.include_siblings or not new_primaries:
                continue
            drags: list[tuple[str, str, str, bool]] = []
            for pidx, dyn_strip, is_folder_match in new_primaries:
                staged_rel, mod_name = core_entries[pidx]
                norm = staged_rel.replace("\\", "/")
                info = self._sibling_container(norm, dyn_strip, is_folder_match, mod_name)
                if info is None:
                    continue
                cont, cname = info
                drags.append((cont.lower(), cname, mod_name, cont == ""))
            drags.sort(key=lambda t: (0 if t[3] else 1, -len(t[0])))
            seen_drags: set[tuple[str, str]] = set()
            for cont_lower, cname, primary_mod, is_whole in drags:
                key = (cont_lower, primary_mod)
                if key in seen_drags:
                    continue
                seen_drags.add(key)
                prefix_lower = cont_lower + "/" if cont_lower else ""
                for i, (staged_rel, mod_name) in enumerate(core_entries):
                    if i in claimed:
                        continue
                    if mod_name != primary_mod:
                        continue
                    norm = staged_rel.replace("\\", "/")
                    norm_lower = norm.lower()
                    if is_whole:
                        rel_in_container = norm
                    else:
                        if norm_lower == cont_lower:
                            continue
                        if not norm_lower.startswith(prefix_lower):
                            continue
                        rel_in_container = norm[len(cont_lower) + 1:]
                    final_rel = (cname + "/" + rel_in_container) if cname else rel_in_container
                    final_rel = canonicalize_declared_folders(
                        final_rel, _declared_folders(rule))
                    per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                    claimed.add(i)

        # Keep IoStore sets together and promote detected blueprint mods.
        # Runs after the rule loop so it can see where the rules actually sent
        # each file, and before the prefix append below so indices still line
        # up with core_entries.
        try:
            self._apply_logicmods_grouping(core_entries, per_entry)
        except Exception:
            # Advisory - any failure leaves the rules' own resolution alone.
            pass

        if prefix_handled:
            for sr, mn in entries:
                if sr.replace("\\", "/").lower() in prefix_handled:
                    per_entry.append(
                        (sr, mn, self._PREFIX_SKIP_DEST, sr.replace("\\", "/"))
                    )

        per_entry = self._apply_companion_routing(entries, per_entry)
        return self._canonicalize_routed_dir_casing(per_entry)

    def _canonicalize_routed_dir_casing(
        self, resolved: list[tuple[str, str, str, str]],
    ) -> list[tuple[str, str, str, str]]:
        """Unify folder casing after every UE5 route has been resolved.

        The filemap's normal casing pass runs in *staged* coordinates. Two
        paths can therefore be unrelated there but converge after a custom
        rule strips their different wrappers. For example::

            PalSchema/mods/A/file.lua
            wrapper/PalSchema/Mods/B/file.lua

        A flattening ``PalSchema`` rule puts both below the same deployed
        ``PalSchema`` directory. On a case-sensitive host that used to create
        sibling ``mods`` and ``Mods`` directories even though the Data tab's
        final-coordinate casing pass displayed only one of them.

        Canonicalize the complete ``dest/final`` paths here, after sibling,
        companion, and LogicMods routing. Every consumer of this resolver
        (deployment, conflict keys, and the Data tab) then sees the same final
        layout. Prefix-routed entries are excluded because their sentinel path
        is consumed by ``deploy_custom_rules`` in a separate namespace.
        """
        if not resolved or not getattr(self, "normalize_folder_case", True):
            return resolved
        try:
            from Utils.ui_config import load_normalize_folder_case
            if not load_normalize_folder_case():
                return resolved
            from Utils.filegraph_paths import canonicalize_dir_casing
        except Exception:
            return resolved

        joined: list[str] = []
        eligible: list[tuple[int, int]] = []
        for idx, (_staged, _mod, dest, final) in enumerate(resolved):
            if dest == self._PREFIX_SKIP_DEST:
                continue
            dest_norm = dest.replace("\\", "/").strip("/")
            final_norm = final.replace("\\", "/").lstrip("/")
            full = f"{dest_norm}/{final_norm}" if dest_norm else final_norm
            joined.append(full)
            eligible.append((idx, len(dest_norm.split("/")) if dest_norm else 0))
        if not joined:
            return resolved

        mapping = canonicalize_dir_casing(
            joined,
            getattr(self, "filemap_casing", "upper") or "upper",
            getattr(self, "filemap_casing_pins", None),
        )
        for (idx, dest_parts), full in zip(eligible, joined):
            canonical = mapping.get(full, full)
            if canonical == full:
                continue
            parts = canonical.split("/")
            dest = "/".join(parts[:dest_parts]) if dest_parts else ""
            final = "/".join(parts[dest_parts:])
            staged, mod, _old_dest, _old_final = resolved[idx]
            resolved[idx] = (staged, mod, dest, final)
        return resolved

    def _logicmods_staging_root(self, core_entries) -> "Path | None":
        """Staging root the entries in *core_entries* actually live under.

        Profiles with ``profile_specific_mods`` keep their mods in
        ``<profile_dir>/mods`` rather than the shared ``mods/`` next to
        ``profiles/``, so ``get_mod_staging_path`` alone points at an empty
        folder and every content probe would silently miss. Prefer the
        profile-aware path the deploy pipeline itself uses, then sanity-check it
        against a real mod name and fall back to the shared path.
        """
        candidates = []
        for getter in ("get_effective_mod_staging_path", "get_mod_staging_path"):
            fn = getattr(self, getter, None)
            if fn is None:
                continue
            try:
                p = fn()
            except Exception:
                continue
            if p:
                p = Path(p)
                if p not in candidates:
                    candidates.append(p)
        if not candidates:
            return None
        mod_names = {mn for _sr, mn in core_entries}
        for root in candidates:
            if any((root / mn).is_dir() for mn in mod_names):
                return root
        return candidates[0]

    def _apply_logicmods_grouping(self, core_entries, per_entry) -> None:
        """Fix up ``.pak``/``.utoc``/``.ucas`` placement in ``per_entry``.

        Two related corrections, both keyed on the *stem group* - same-folder
        same-stem archive files, which for an IoStore mod are one container set
        that must never be split across destinations:

        1. **Cohesion.** If any member of a group resolved into a LogicMods
           path, the rest follow it. Several UE handlers declare a
           ``.pak → Content/Paks/LogicMods`` rule listing no
           ``companion_extensions``, which sends the ``.pak`` to LogicMods and
           leaves its ``.utoc``/``.ucas`` to fall through to ``~mods`` - the mod
           then has a loader entry with no data behind it.
        2. **Promotion.** A group that landed outside LogicMods is probed:
           if it contains ``Mods/<Name>/ModActor`` it is a UE4SS blueprint mod
           and only runs from ``Content/Paks/LogicMods``, because
           BPModLoaderMod discovers mods solely by listing that one directory.
           See ``Utils.ue_logicmods_detect``.

        Cohesion is preferred over promotion so a group the rules already
        placed correctly keeps the layout they chose - flattening
        ``LogicMods/<Mod>/x.pak`` to the LogicMods root would strand a
        ``config.lua`` sitting beside it, which BPModLoaderMod reads from the
        mod's own subfolder.
        """
        from Utils.ue_logicmods_detect import logic_mod_entries, stem_groups

        by_mod: dict[str, list[tuple[int, str]]] = {}
        for i, (staged_rel, mod_name) in enumerate(core_entries):
            by_mod.setdefault(mod_name, []).append((i, staged_rel))

        def deploy_path(i: int) -> str:
            _sr, _mn, dest, final = per_entry[i]
            joined = (dest + "/" + final) if dest else final
            return joined.replace("\\", "/")

        def in_logicmods(i: int) -> bool:
            return "logicmods" in deploy_path(i).lower().split("/")

        promote: set[int] | None = None
        for mod_name, items in by_mod.items():
            index_of = {rel: i for i, rel in items}
            for members in stem_groups(rel for _i, rel in items).values():
                idxs = [index_of[m] for m in members if m in index_of]
                if len(idxs) < 1:
                    continue

                anchors = [i for i in idxs if in_logicmods(i)]
                if anchors:
                    if len(anchors) == len(idxs):
                        continue  # already consistent
                    # Prefer the .pak as anchor - it is what BPModLoaderMod
                    # enumerates, so its placement is the one to match.
                    anchor = next(
                        (i for i in anchors
                         if deploy_path(i).lower().endswith(".pak")),
                        anchors[0],
                    )
                    a_dest, a_final = per_entry[anchor][2], per_entry[anchor][3]
                    a_stem = a_final.replace("\\", "/").rsplit(".", 1)[0]
                    for i in idxs:
                        if i in anchors:
                            continue
                        staged_rel, mn, _d, _f = per_entry[i]
                        name = staged_rel.replace("\\", "/").rsplit("/", 1)[-1]
                        ext = name[name.rfind("."):] if "." in name else ""
                        per_entry[i] = (staged_rel, mn, a_dest, a_stem + ext)
                    continue

                if promote is None:
                    promote = logic_mod_entries(
                        self._logicmods_staging_root(core_entries), core_entries)
                if not promote or not all(i in promote for i in idxs):
                    continue
                # Archives inside a named subfolder that also holds other files
                # are a deliberate layout (a config.lua that BPModLoaderMod
                # reads from the mod's own subfolder, say); lifting just the
                # archives out of it would strand the rest, so leave the whole
                # folder to the rules - together and unpromoted beats split.
                # Files loose at the mod root carry no such grouping: a readme
                # or .modconfig.json beside a pak is routed on its own merits,
                # so those still promote.
                parent = members[0].replace("\\", "/").rpartition("/")[0].lower()
                if parent and any(
                    rel.replace("\\", "/").rpartition("/")[0].lower() == parent
                    and index_of.get(rel) not in idxs
                    for _i, rel in items
                ):
                    continue
                for i in idxs:
                    staged_rel, mn, _d, _f = per_entry[i]
                    per_entry[i] = (
                        staged_rel, mn, "Content/Paks/LogicMods",
                        Path(staged_rel.replace("\\", "/")).name,
                    )

    def _apply_companion_routing(self, entries, resolved):
        """Re-route same-folder same-stem siblings to ride along with a primary
        match from a ``custom_routing_rules`` entry that declares
        ``companion_extensions``.

        The UE5 rule pipeline has no companion concept, so companion files
        (e.g. ``Foo.ini`` next to ``Foo.asi``) otherwise fall through to
        whatever default rule catches their extension. This pass detects each
        companion, looks up its primary's resolution in ``resolved``, and
        overrides the companion's entry with the same ``dest_rel`` and a
        stem-swapped ``final_rel``.  A no-op for games with no companion rules.
        """
        user_rules = [r for r in self.custom_routing_rules
                      if getattr(r, "companion_extensions", None)
                      and not getattr(r, "to_prefix", False)]
        if not user_rules:
            return resolved
        from Utils.deploy_custom_rules import _match_single_rule, _normalise_rule
        import os
        # Index resolved entries by (staged_rel, mod_name) so we can overwrite.
        idx_by_key: dict[tuple[str, str], int] = {
            (sr, mn): i for i, (sr, mn, _d, _f) in enumerate(resolved)
        }
        # Group entries by (mod_name, parent_lower) for same-folder lookup.
        groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for sr, mn in entries:
            norm = sr.replace("\\", "/")
            parent_lower = norm.rsplit("/", 1)[0].lower() if "/" in norm else ""
            groups.setdefault((mn, parent_lower), []).append((sr, norm))
        # Match each user rule against entries to find its primaries; ride
        # along companions for each one.
        claimed: set[tuple[str, str]] = set()
        for rule in user_rules:
            _r, folders, exts, filenames = _normalise_rule(rule)
            companions = sorted(
                {c.lower() for c in rule.companion_extensions},
                key=len, reverse=True,
            )
            for sr, mn in entries:
                if (sr, mn) in claimed:
                    continue
                norm = sr.replace("\\", "/")
                rel_lower = norm.lower()
                hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
                if hit is None:
                    continue
                _strip_len, matched_ext = hit
                claimed.add((sr, mn))
                primary_idx = idx_by_key.get((sr, mn))
                if primary_idx is None:
                    continue
                _psr, _pmn, primary_dest, primary_final = resolved[primary_idx]
                parent_lower = norm.rsplit("/", 1)[0].lower() if "/" in norm else ""
                name_lower = norm.rsplit("/", 1)[-1].lower()
                if matched_ext and name_lower.endswith(matched_ext):
                    stem_lower = name_lower[: -len(matched_ext)]
                else:
                    stem_lower, _ = os.path.splitext(name_lower)
                stem_dot = stem_lower + "."
                # primary_final's basename may differ in case from name_lower
                # (e.g. flattened rules). Use the primary_final's actual base.
                primary_final_norm = primary_final.replace("\\", "/")
                primary_final_parent, _, primary_final_name = \
                    primary_final_norm.rpartition("/")
                for sib_sr, sib_norm in groups.get((mn, parent_lower), []):
                    if (sib_sr, mn) in claimed:
                        continue
                    sib_name_lower = sib_norm.rsplit("/", 1)[-1].lower()
                    if sib_name_lower == name_lower:
                        continue
                    if not sib_name_lower.startswith(stem_dot):
                        continue
                    sib_ext = next(
                        (c for c in companions
                         if sib_name_lower.endswith(c)
                         and len(sib_name_lower) > len(c)),
                        None,
                    )
                    if sib_ext is None:
                        continue
                    # Build the companion's final_rel by swapping the primary's
                    # filename for the companion's filename, preserving any
                    # parent directory structure the primary kept.
                    sib_base = sib_norm.rsplit("/", 1)[-1]
                    companion_final = (
                        f"{primary_final_parent}/{sib_base}"
                        if primary_final_parent else sib_base
                    )
                    sib_idx = idx_by_key.get((sib_sr, mn))
                    if sib_idx is None:
                        continue
                    resolved[sib_idx] = (sib_sr, mn, primary_dest, companion_final)
                    claimed.add((sib_sr, mn))
        return resolved

    # -----------------------------------------------------------------------
    # UE4SS mods.txt management
    # -----------------------------------------------------------------------

    def _resolve_ue4ss_mods_dest(self) -> str | None:
        """Return the game-root-relative dir where UE4SS lua mods land.

        Detected by walking ``ue5_routing_rules`` for a rule whose ``dest``
        ends in ``Mods`` (case-insensitive) and whose ``extensions`` include
        ``.lua``.  Returns the dest string (e.g. ``"Binaries/Win64/Mods"``
        or ``"Binaries/Win64/ue4ss/Mods"``) or ``None`` if no UE4SS lua
        rule is configured.
        """
        for rule in self.ue5_routing_rules:
            if not rule.dest:
                continue
            dest_norm = rule.dest.replace("\\", "/").rstrip("/")
            if not dest_norm.lower().endswith("/mods") and dest_norm.lower() != "mods":
                continue
            exts_lower = {e.lower() for e in rule.extensions}
            if ".lua" in exts_lower:
                return dest_norm
        return None

    @staticmethod
    def _parse_mods_txt_line(line: str) -> tuple[str | None, bool | None]:
        """Parse a ``<folder> : <0|1>`` line.  Returns (folder, enabled) or
        (None, None) for blank/comment/unrecognised lines."""
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            return None, None
        if ":" not in stripped:
            return None, None
        name_part, _, val_part = stripped.partition(":")
        name = name_part.strip()
        val = val_part.strip()
        if not name or val not in ("0", "1"):
            return None, None
        return name, val == "1"

    def _collect_ue4ss_disabled_consensus(
        self, mod_names: list[str], snapshot,
    ) -> set[str]:
        """Scan staged mod folders for ``mods.txt`` files and return the set
        of folder names that should default to ``: 0``.

        A folder defaults to disabled iff it appears in at least one source
        ``mods.txt`` AND every source that mentions it sets it to ``0``.
        Any ``: 1`` mention flips it to enabled. Folders not mentioned in
        any source default to enabled.

        Filegraph locates the small set of raw ``Mods/mods.txt`` sources; only
        those exact files are opened.

        Returns a set of lowercased folder names.
        """
        # Per-folder counts: lowered_folder_name -> [mentions, zero_mentions]
        counts: dict[str, list[int]] = {}

        def _ingest(text: str) -> None:
            for line in text.splitlines():
                folder, enabled = self._parse_mods_txt_line(line)
                if folder is None:
                    continue
                slot = counts.setdefault(folder.lower(), [0, 0])
                slot[0] += 1
                if not enabled:
                    slot[1] += 1

        enabled = {name.lower() for name in mod_names}
        from Utils.filegraph_service import source_path
        for mod_name, relative in snapshot.raw_files_by_basename(["mods.txt"]):
            if mod_name.lower() not in enabled:
                continue
            rel_text = bytes(relative).decode("utf-8", "surrogateescape")
            parent = rel_text.replace("\\", "/").rsplit("/", 2)[-2:-1]
            if not parent or parent[0].lower() != "mods":
                continue
            try:
                _ingest(source_path(
                    self, mod_name, relative).read_text(
                        encoding="utf-8", errors="replace"))
            except OSError:
                continue

        return {k for k, (mentions, zeros) in counts.items()
                if mentions > 0 and mentions == zeros}

    def _update_ue4ss_mods_txt(
        self,
        deployed_folder_names: set[str],
        disabled_folders: set[str] | None = None,
        log_fn=None,
    ) -> None:
        """Sync ``mods.txt`` to reflect the currently deployed UE4SS lua mods.

        ``disabled_folders`` (case-insensitive lowercased names) - fresh
        entries written for folders in this set get ``: 0`` instead of
        ``: 1``. Existing lines in the file are preserved as-is, so the
        user's manual edits survive.
        """
        _log = log_fn or (lambda _: None)

        dest_rel = self._resolve_ue4ss_mods_dest()
        if dest_rel is None:
            return
        game_path = self._ue5_active_deploy_root()
        if game_path is None:
            return

        # Match every existing directory segment case-insensitively. A
        # custom route may spell this destination ``mods`` while UE4SS or a
        # framework already created ``Mods`` on the case-sensitive host.
        mods_file = _resolve_root_path(
            game_path, Path(dest_rel) / "mods.txt")

        existing_lines: list[str] = []
        if mods_file.is_file():
            try:
                raw = mods_file.read_text(encoding="utf-8", errors="replace")
                existing_lines = raw.splitlines()
            except OSError as exc:
                _log(f"  WARN: could not read {mods_file}: {exc}")
                return

        deployed_lower = {n.lower() for n in deployed_folder_names}
        disabled_lower = {n.lower() for n in (disabled_folders or set())}

        def _entry_for(name: str) -> str:
            return f"{name} : {'0' if name.lower() in disabled_lower else '1'}"

        out_lines: list[str] = []
        seen_lower: set[str] = set()

        # Track Keybinds separately - UE4SS requires it loaded last (the
        # shipped file has a "; Built-in keybinds, do not move up!" comment
        # above it). We always force it to the bottom regardless of where
        # it appeared in the source file.
        keybinds_line: str | None = None
        keybinds_deployed = "keybinds" in deployed_lower

        for line in existing_lines:
            folder, _enabled = self._parse_mods_txt_line(line)
            if folder is None:
                # Comment / blank / unrecognised - keep as-is
                out_lines.append(line)
                continue
            f_lower = folder.lower()
            if f_lower in seen_lower:
                # Duplicate of an entry we've already emitted - drop
                continue
            if f_lower == "keybinds":
                # Defer Keybinds to end-of-file
                if keybinds_deployed and keybinds_line is None:
                    keybinds_line = line
                seen_lower.add(f_lower)
                continue
            if f_lower in deployed_lower:
                # Managed entry, still deployed - keep, mark as seen
                out_lines.append(line)
                seen_lower.add(f_lower)
            # Else: managed entry whose mod is no longer deployed - drop

        # Strip any trailing blank lines / comments left after Keybinds was
        # pulled out (so we don't double the trailing blank when re-appending).
        while out_lines and (
            not out_lines[-1].strip()
            or out_lines[-1].lstrip().startswith(";")
            or out_lines[-1].lstrip().startswith("#")
        ):
            # Only strip trailing blanks/comments if Keybinds is going to be
            # appended; otherwise leave them alone.
            if not keybinds_deployed:
                break
            out_lines.pop()

        # Append regular new entries (everything except Keybinds). Default
        # state honours disabled_folders (consensus from source mods.txt).
        new_names = sorted(
            n for n in deployed_folder_names
            if n.lower() not in seen_lower and n.lower() != "keybinds"
        )
        out_lines.extend(_entry_for(n) for n in new_names)

        # Append Keybinds last with the standard header comment.
        if keybinds_deployed:
            out_lines.append("")
            out_lines.append("; Built-in keybinds, do not move up!")
            # Preserve the original Keybinds line if it had a custom state
            # (e.g. user disabled it), else use the consensus default.
            keybinds_name = next(
                (n for n in deployed_folder_names if n.lower() == "keybinds"),
                "Keybinds",
            )
            out_lines.append(keybinds_line if keybinds_line else _entry_for(keybinds_name))

        # If nothing remains besides blanks/comments we ourselves emitted,
        # delete the file so the empty-dir sweep can clean up the parent.
        # Without this, the "; Built-in keybinds, do not move up!" header we
        # wrote at deploy time would survive restore and keep the file alive.
        has_real_content = any(
            self._parse_mods_txt_line(l)[0] is not None for l in out_lines
        )
        if not has_real_content:
            if mods_file.is_file():
                try:
                    mods_file.unlink()
                    _log("  Removed empty UE4SS mods.txt")
                except OSError as exc:
                    _log(f"  WARN: could not remove {mods_file}: {exc}")
            return

        new_content = "\r\n".join(out_lines) + "\r\n"

        # Skip the write if nothing changed (avoids touching mtime needlessly).
        if mods_file.is_file():
            try:
                if mods_file.read_bytes() == new_content.encode("utf-8"):
                    return
            except OSError:
                pass

        try:
            mods_file.parent.mkdir(parents=True, exist_ok=True)
            mods_file.write_text(new_content, encoding="utf-8", newline="")
            _log(f"  Updated UE4SS mods.txt ({len(deployed_folder_names)} entries)")
        except OSError as exc:
            _log(f"  WARN: could not write {mods_file}: {exc}")

    def _collect_deployed_ue4ss_folders(
        self, manifest: list[str], dest_rel: str,
    ) -> set[str]:
        """From a deploy manifest, find folder names directly under ``dest_rel``
        that should get a ``mods.txt`` entry.

        Filters applied (post-deploy, against the live game tree):
          - Skip if the folder contains ``enabled.txt`` (UE4SS auto-loads via
            the per-folder marker - duplicate entry is unnecessary).
          - Skip if the folder doesn't contain ``Scripts/main.lua`` (UE4SS
            only treats folders with a main.lua as actual mods; everything
            else is library/shared code).

        Manifest entries are game-root-relative (custom-dir absolute entries
        are skipped - UE4SS lua mods always land in the game tree).
        """
        prefix = dest_rel.replace("\\", "/").strip("/").lower() + "/"
        candidate_folders: set[str] = set()
        for entry in manifest:
            if not entry:
                continue
            if Path(entry).is_absolute():
                continue
            norm = entry.replace("\\", "/").lstrip("/")
            if not norm.lower().startswith(prefix):
                continue
            tail = norm[len(prefix):]
            first_seg, _, rest = tail.partition("/")
            if not first_seg or not rest:
                # Loose file directly in the Mods dir (not a mod folder) - skip
                continue
            candidate_folders.add(first_seg)

        # Filter against the live game tree: folder must have Scripts/main.lua
        # and must NOT have enabled.txt.
        game_path = self._ue5_active_deploy_root()
        if game_path is None:
            return candidate_folders
        mods_root = _resolve_root_path(
            game_path, Path(dest_rel) / "mods.txt").parent

        kept: set[str] = set()
        for folder in candidate_folders:
            folder_dir = mods_root / folder
            if (folder_dir / "enabled.txt").is_file():
                continue
            if not (folder_dir / "Scripts" / "main.lua").is_file():
                continue
            kept.add(folder)
        return kept

    # -----------------------------------------------------------------------
    # Deploy
    # -----------------------------------------------------------------------

    def deploy(
        self,
        log_fn=None,
        mode: LinkMode = LinkMode.HARDLINK,
        profile: str = "default",
        progress_fn=None,
    ) -> None:
        """Place each mod file directly into its resolved game destination.

        UE5 destination folders (Content/Paks, Binaries/Win64, etc.) contain
        game content that must not be moved.  We therefore skip the Core backup
        pattern and simply place files, tracking them in ue5_deployed.txt so
        restore() knows what to remove.
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self._ue5_active_deploy_root()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap = self.get_effective_filemap_path()
        from Utils.filegraph_deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        if (self.vfs_launch_enabled
                and not getattr(self, "_vfs_ue5_populating", False)):
            return self._deploy_vfs(
                profile=profile,
                filemap=filemap,
                staging=self.get_effective_mod_staging_path(),
                log_fn=_log,
                progress_fn=progress_fn,
            )

        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries)
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries)
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        prefix_per_mod_modes = per_mod_modes
        vfs_external_mods: set[str] = set()

        if getattr(self, "_vfs_ue5_populating", False):
            source_game = Path(getattr(self, "_vfs_ue5_source_game_root"))
            source_data = Path(getattr(self, "_vfs_ue5_source_data_root"))
            target_root = Path(getattr(self, "_vfs_ue5_outer_layer"))
            mapped: dict[str, Path] = {}
            external_mods: set[str] = set()
            for mod_name, raw_target in per_mod_deploy.items():
                target = Path(raw_target).expanduser()
                if not target.is_absolute():
                    target = source_data / target
                try:
                    rel = target.resolve().relative_to(source_data.resolve())
                    mapped[mod_name] = game_path / rel
                    continue
                except (OSError, ValueError):
                    pass
                try:
                    rel = target.resolve().relative_to(source_game.resolve())
                    mapped[mod_name] = target_root / rel
                except (OSError, ValueError):
                    mapped[mod_name] = target
                    external_mods.add(mod_name)
                    vfs_external_mods.add(mod_name)
            per_mod_deploy = mapped
            mapped_modes = dict(per_mod_modes or {})
            external_default = getattr(
                self, "_vfs_ue5_external_deploy_mode", LinkMode.HARDLINK)
            for mod_name in mapped:
                if mod_name in external_mods:
                    mapped_modes.setdefault(mod_name, external_default)
                else:
                    # Internal synthetic layers must never contain links back
                    # to staging, regardless of a separator override.
                    mapped_modes[mod_name] = LinkMode.HARDLINK
            per_mod_modes = mapped_modes or None

        prefix_rules = self._prefix_routing_rules()
        if prefix_rules:
            _log("Routing prefix-bound files via custom rules ...")
            deploy_custom_rules(
                filemap, game_path, staging,
                rules=prefix_rules,
                mode=(getattr(
                    self, "_vfs_ue5_external_deploy_mode", mode)
                    if getattr(self, "_vfs_ue5_populating", False)
                    else mode),
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=(
                    prefix_per_mod_modes
                    if getattr(self, "_vfs_ue5_populating", False)
                    else per_mod_modes
                ),
                raw_mods=per_mod_raw or None,
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )
        overwrite_dir = staging.parent / "overwrite"
        # Filemap entries for the overwrite folder carry strip-normalised
        # paths while the folder itself holds deployed-layout paths - a
        # direct join misses them (e.g. "ue4ss/..." vs the on-disk
        # "Binaries/Win64/ue4ss/...").  Index it once up front.
        overwrite_lookup = _build_overwrite_lookup(
            overwrite_dir, self.mod_folder_strip_prefixes)

        manifest: list[str] = []
        vanilla_backup_dir = (
            game_path / _VANILLA_BACKUP_DIR
            if getattr(self, "_vfs_ue5_populating", False)
            else (self._game_path or game_path) / _VANILLA_BACKUP_DIR
        )
        custom_vanilla_backup_dir = Path(getattr(
            self,
            "_vfs_ue5_custom_backup_dir",
            self.get_profile_root() / _CUSTOM_VANILLA_BACKUP_DIR,
        ))
        linked = 0
        skipped = 0
        backed_up = 0

        from Utils.filegraph_deploy import entries as filegraph_entries, legacy_lines
        lines = list(legacy_lines())
        filegraph_sources = {
            (entry.legacy_rel.lower(), entry.mod_name): entry.source_path
            for entry in filegraph_entries()
            if entry.legacy_rel and entry.source_path is not None
        }

        # Build priority map so flatten/strip collisions resolve to the highest
        # priority mod's file rather than whichever line happens to deploy last.
        # Index 0 in modlist == top priority, so lower rank wins.
        modlist_path = profile_dir / "modlist.txt"
        priority_map: dict[str, int] = {}
        if modlist_path.is_file():
            for rank, e in enumerate(read_modlist(modlist_path)):
                priority_map[e.name] = rank
        # Pre-resolve every entry and dedupe by final destination path.
        # When multiple staged paths resolve to the same on-disk target
        # (typical with flatten=True), the higher-priority mod wins.
        # Use _resolve_filemap_entries (not per-line _resolve_entry) so
        # rules with include_siblings can drag in same-mod files under the
        # matched file's containing folder.
        parsed = [tuple(line.split("\t", 1)) for line in lines]
        rule_resolved = {
            (sr, mn): (dr, fr)
            for sr, mn, dr, fr in self._resolve_filemap_entries(
                [(sr, mn) for sr, mn in parsed]
            )
        }
        resolved_by_dest: dict[str, tuple[int, str, str, str, Path, Path, bool, str]] = {}
        dest_case_cache: dict = {}
        prefix_skip_dest = getattr(self, "_PREFIX_SKIP_DEST", None)
        for staged_rel, mod_name in parsed:
            base_dir = per_mod_deploy.get(mod_name, game_path)
            in_custom_dir = base_dir != game_path
            if mod_name in per_mod_raw:
                final_rel = staged_rel.replace("\\", "/")
                dest_rel = ""
            else:
                dest_rel, final_rel = rule_resolved[(staged_rel, mod_name)]
                # Files routed into the Proton/Wine prefix are placed by
                # deploy_custom_rules before this loop runs; skip them here.
                if prefix_skip_dest is not None and dest_rel == prefix_skip_dest:
                    continue
            effective_rel = (Path(dest_rel) / final_rel
                             if dest_rel else Path(final_rel))
            # Resolve against folders already present in the target. This is
            # the final guard against creating ``mods`` beside an existing
            # ``Mods`` directory on Linux. The post-route canonicalization
            # above handles case variants created within this deploy batch;
            # this resolver handles directories that predate the deploy.
            dest_file = _resolve_root_path(
                base_dir, effective_rel, dest_case_cache)
            if getattr(self, "_vfs_ue5_populating", False):
                try:
                    dest_file.resolve().relative_to(base_dir.resolve())
                except ValueError as exc:
                    raise RuntimeError(
                        "UE5 VFS routing destination escapes its selected "
                        f"deployment root: {effective_rel}"
                    ) from exc
            dest_dir = dest_file.parent
            key = str(dest_file).casefold()
            # Overwrite-folder files layer user/runtime edits on top of every
            # mod, so they must win destination collisions - without this the
            # sentinel name (absent from modlist.txt) would rank LAST.
            if mod_name == _OVERWRITE_NAME:
                rank = -1
            else:
                rank = priority_map.get(mod_name, 1 << 30)
            existing = resolved_by_dest.get(key)
            if existing is None or rank < existing[0]:
                resolved_by_dest[key] = (
                    rank, staged_rel, mod_name, final_rel,
                    dest_dir, dest_file, in_custom_dir, dest_rel,
                )
        deploy_order = list(resolved_by_dest.values())
        total = len(deploy_order)

        # A VFS build seeds the physical UE4SS mods.txt into the private layer
        # before entering this loop, then regenerates it after placement. Skip
        # a mod-shipped copy only in that private build. Physical deployment
        # must keep its established backup/restore path for this file.
        ue4ss_dest_rel = self._resolve_ue4ss_mods_dest()
        managed_mods_txt: Path | None = (
            _resolve_root_path(
                game_path, Path(ue4ss_dest_rel) / "mods.txt", dest_case_cache)
            if (ue4ss_dest_rel is not None
                and getattr(self, "_vfs_ue5_populating", False))
            else None
        )

        for i, (_rank, staged_rel, mod_name, final_rel,
                dest_dir, dest_file, in_custom_dir, dest_rel) in enumerate(deploy_order):

            # Skip mod-shipped mods.txt at the managed location - we generate
            # the canonical file ourselves after the deploy loop, so placing
            # a mod's copy here would just be overwritten and creates churn
            # in the vanilla-backup logic.
            if (managed_mods_txt is not None and not in_custom_dir
                    and dest_file == managed_mods_txt):
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            src = filegraph_sources.get((staged_rel.lower(), mod_name))
            if src is None:
                _log(f"  WARN: source not found for {staged_rel} ({mod_name})")
                skipped += 1
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            try:
                if (dest_file.exists() and not dest_file.is_file()
                        and not dest_file.is_symlink()):
                    raise OSError(
                        "deployment destination exists but is not a regular "
                        "file or symlink"
                    )

                # Back up any real vanilla file before overwriting it.
                # Symlinks are our own previous deploys - don't back those up.
                if dest_file.is_file() and not dest_file.is_symlink():
                    if in_custom_dir:
                        # Mirror full absolute path so restore can reconstruct it.
                        rel_abs = dest_file.relative_to(dest_file.anchor)
                        backup_target = custom_vanilla_backup_dir / rel_abs
                    else:
                        game_rel = dest_file.relative_to(game_path)
                        backup_target = vanilla_backup_dir / game_rel
                    if not backup_target.exists():
                        backup_target.parent.mkdir(parents=True, exist_ok=True)
                        if (getattr(self, "_vfs_ue5_populating", False)
                                and mod_name in vfs_external_mods):
                            # Never expose a partially copied backup to crash
                            # recovery. The temporary mirror is disposable
                            # while os.replace publishes the completed copy
                            # atomically on the same profile filesystem.
                            backup_temp_root = Path(getattr(
                                self, "_vfs_ue5_custom_backup_temp_dir"))
                            backup_temp = backup_temp_root / rel_abs
                            backup_temp.parent.mkdir(
                                parents=True, exist_ok=True)
                            shutil.copy2(dest_file, backup_temp)
                            os.replace(backup_temp, backup_target)
                        else:
                            shutil.copy2(dest_file, backup_target)
                        backed_up += 1

                # External separator targets are the only deliberate physical
                # writes made by a UE5 VFS build. Journal each one after any
                # original has been safely copied, but before unlinking or
                # placing the mod file. This makes an interrupted deploy
                # rollback-safe even when the final UE5 manifest was never
                # written (for example if a progress callback raises).
                if (getattr(self, "_vfs_ue5_populating", False)
                        and mod_name in vfs_external_mods):
                    journal_path = Path(getattr(
                        self, "_vfs_ue5_external_journal_path"))
                    destination_line = str(dest_file)
                    journaled = getattr(
                        self, "_vfs_ue5_external_journal_entries")
                    if destination_line not in journaled:
                        # JSON-lines lets restore ignore an incomplete final
                        # record after a process crash. Flush the record to
                        # disk before changing the physical destination.
                        with journal_path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(destination_line) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        journaled.add(destination_line)

                dest_file.parent.mkdir(parents=True, exist_ok=True)
                if dest_file.exists() or dest_file.is_symlink():
                    dest_file.unlink()
                effective_mode = (per_mod_modes or {}).get(mod_name, mode)
                if effective_mode == LinkMode.SYMLINK:
                    dest_file.symlink_to(src)
                elif effective_mode == LinkMode.COPY:
                    shutil.copy2(src, dest_file)
                else:
                    try:
                        dest_file.hardlink_to(src)
                    except (OSError, NotImplementedError):
                        shutil.copy2(src, dest_file)
                # Record in manifest: absolute path for custom dirs, game-root-relative otherwise
                if in_custom_dir:
                    manifest.append(str(dest_file))
                else:
                    # Record the resolved, actual on-disk casing. Restore must
                    # address the path Linux created, not the route's spelling.
                    manifest.append(dest_file.relative_to(game_path).as_posix())
                linked += 1
            except OSError as exc:
                _log(f"  ERROR placing {final_rel}: {exc}")
                skipped += 1

            if progress_fn:
                progress_fn(i + 1, total)

        # Write manifest so restore() knows exactly what to remove
        manifest_path = self._ue5_deployed_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("\n".join(manifest), encoding="utf-8")

        # Sync UE4SS mods.txt for games that need it (Palworld-style loaders
        # which require an explicit enable list rather than per-folder enabled.txt).
        ue4ss_dest = self._resolve_ue4ss_mods_dest()
        if ue4ss_dest is not None:
            try:
                folders = self._collect_deployed_ue4ss_folders(manifest, ue4ss_dest)
                # Build the disabled-by-consensus set from every source
                # mods.txt across staging - a folder defaults to ``: 0`` only
                # if every mod that mentions it sets it to 0. Reuses the
                # catalog to avoid walking disk per mod.
                from Utils.filegraph_service import active_snapshot
                snapshot = active_snapshot(self)
                enabled_mods = [
                    e.name for e in read_modlist(modlist_path)
                    if e.enabled and not e.is_separator
                ]
                disabled = self._collect_ue4ss_disabled_consensus(
                    enabled_mods, snapshot,
                )
                self._update_ue4ss_mods_txt(folders, disabled_folders=disabled, log_fn=_log)
            except Exception as exc:
                _log(f"  WARN: could not update UE4SS mods.txt: {exc}")

        # Snapshot the game root so restore() can identify runtime-generated files
        # (saves, shader cache, config files written by the game after launch).
        if not getattr(self, "_vfs_ue5_populating", False):
            snapshot_path = self._ue5_runtime_snapshot_path()
            try:
                _write_deploy_snapshot(game_path, snapshot_path, log_fn=_log)
            except Exception as exc:
                _log(f"  WARN: could not write deploy snapshot: {exc}")

        backed_msg = f", {backed_up} vanilla file(s) backed up" if backed_up else ""
        _log(f"Deploy complete. {linked} file(s) placed{backed_msg}, {skipped} skipped.")

    def _vfs_populate_data_layer(
        self,
        *,
        destination: Path,
        outer_layer: Path,
        game_root: Path,
        data_root: Path,
        profile: str,
        filemap: Path,
        staging: Path,
        external_deploy_mode: LinkMode,
        log_fn,
        progress_fn=None,
    ) -> int:
        """Populate a private UE project layer with the normal UE5 resolver."""
        del staging  # Resolved through the handler's effective path.
        destination = Path(destination)
        outer_layer = Path(outer_layer)
        temp_manifest = outer_layer.parent / "ue5-private-deployed.txt"
        external_manifest = self._vfs_external_manifest_path(profile)

        def _read_manifest(path: Path) -> list[str]:
            try:
                raw_lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            lines: list[str] = []
            for raw_line in raw_lines:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith('"'):
                    try:
                        decoded = json.loads(line)
                    except (TypeError, ValueError):
                        # A torn append can only be the final record and was
                        # flushed before no corresponding placement occurred.
                        continue
                    if not isinstance(decoded, str):
                        continue
                    line = decoded
                lines.append(line)
            return lines

        def _inside_layer(path: Path) -> bool:
            for parent in (destination, outer_layer):
                try:
                    path.relative_to(parent)
                    return True
                except ValueError:
                    continue
            return False

        def _publish_external_entries(lines: list[str]) -> list[str]:
            # Merge the completed inner manifest with destinations journaled
            # incrementally by deploy(). The latter survives exceptions before
            # ue5-private-deployed.txt can be published.
            external = _read_manifest(external_manifest)
            external.extend(
                line for line in lines
                if Path(line).is_absolute() and not _inside_layer(Path(line))
            )
            external = list(dict.fromkeys(external))
            external_manifest.parent.mkdir(parents=True, exist_ok=True)
            # Keep an empty marker too: it distinguishes an interrupted VFS
            # build with possible prefix side effects from a physical deploy.
            write_atomic_text(
                external_manifest,
                "".join(json.dumps(line) + "\n" for line in external),
            )
            return external

        # Reverse deliberate physical side effects from the previous VFS view
        # before resolving its replacement. The normal UE5 manifest is never
        # involved, so a failed private build cannot endanger the real project.
        if external_manifest.exists():
            self._restore_vfs_external_targets(
                log_fn, manifest_path=external_manifest)
        if self._vfs_prefix_context_path(profile).exists():
            self._restore_vfs_prefix_targets(log_fn, profile)
        external_manifest.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_text(external_manifest, "")
        self._write_vfs_prefix_context(Path(filemap), profile)

        # Seed a physical UE4SS mods.txt into the private project layer so its
        # comments, manual states and built-ins survive regeneration without
        # ever writing through a hardlink to the vanilla inode.
        ue4ss_dest = self._resolve_ue4ss_mods_dest()
        if ue4ss_dest is not None:
            source_mods_txt = _resolve_root_path(
                Path(data_root), Path(ue4ss_dest) / "mods.txt")
            if source_mods_txt.is_file():
                private_mods_txt = _resolve_root_path(
                    destination, Path(ue4ss_dest) / "mods.txt")
                private_mods_txt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_mods_txt, private_mods_txt)

        self._vfs_ue5_populating = True
        self._vfs_ue5_target_root = destination
        self._vfs_ue5_outer_layer = outer_layer
        self._vfs_ue5_source_game_root = Path(game_root)
        self._vfs_ue5_source_data_root = Path(data_root)
        self._vfs_ue5_manifest_path = temp_manifest
        self._vfs_ue5_custom_backup_dir = (
            external_manifest.parent / _CUSTOM_VANILLA_BACKUP_DIR)
        self._vfs_ue5_custom_backup_temp_dir = (
            external_manifest.parent / _VFS_CUSTOM_BACKUP_TEMP_DIR)
        self._vfs_ue5_external_journal_path = external_manifest
        self._vfs_ue5_external_journal_entries: set[str] = set()
        self._vfs_ue5_external_deploy_mode = external_deploy_mode
        try:
            # Call the base implementation explicitly so a built-in subclass's
            # physical post-deploy step cannot escape into the real install.
            UE5Game.deploy(
                self,
                log_fn=log_fn,
                mode=LinkMode.HARDLINK,
                profile=profile,
                progress_fn=progress_fn,
            )
            extra_layer_files = getattr(
                self, "_vfs_populate_ue5_layer_files", None)
            if callable(extra_layer_files):
                extra_layer_files(destination, profile, log_fn)
            manifest_lines = _read_manifest(temp_manifest)
            _publish_external_entries(manifest_lines)
        except BaseException:
            # Record whatever the inner deploy managed to place before trying
            # to reverse it. If cleanup itself fails, the marker and remaining
            # backup are deliberately retained so Restore can retry safely.
            _publish_external_entries(_read_manifest(temp_manifest))
            try:
                self._restore_vfs_external_targets(
                    log_fn, manifest_path=external_manifest)
            except Exception as cleanup_exc:
                log_fn(
                    "  WARN: failed to roll back external UE5 VFS targets: "
                    f"{cleanup_exc}"
                )
            try:
                self._restore_vfs_prefix_targets(log_fn, profile)
            except Exception as cleanup_exc:
                log_fn(
                    "  WARN: failed to roll back prefix-routed UE5 VFS "
                    f"targets: {cleanup_exc}"
                )
            raise
        finally:
            for name in (
                "_vfs_ue5_populating",
                "_vfs_ue5_target_root",
                "_vfs_ue5_outer_layer",
                "_vfs_ue5_source_game_root",
                "_vfs_ue5_source_data_root",
                "_vfs_ue5_manifest_path",
                "_vfs_ue5_custom_backup_dir",
                "_vfs_ue5_custom_backup_temp_dir",
                "_vfs_ue5_external_journal_path",
                "_vfs_ue5_external_journal_entries",
                "_vfs_ue5_external_deploy_mode",
            ):
                try:
                    delattr(self, name)
                except AttributeError:
                    pass
            temp_manifest.unlink(missing_ok=True)
            synthetic_backup = destination / _VANILLA_BACKUP_DIR
            if synthetic_backup.is_dir():
                shutil.rmtree(synthetic_backup, ignore_errors=True)
            backup_temp = (
                external_manifest.parent / _VFS_CUSTOM_BACKUP_TEMP_DIR)
            if backup_temp.is_dir():
                shutil.rmtree(backup_temp, ignore_errors=True)
        return len(manifest_lines)

    def _find_staged_file(
        self,
        staging: Path,
        mod_name: str,
        staged_rel: str,
        mod_strips: list[str],
        overwrite_dir: Path,
        global_strips: set[str] | None = None,
        overwrite_lookup: dict[str, Path] | None = None,
    ) -> Path | None:
        """Locate the physical source file for a filemap entry.

        Tries in order:
          1. Overwrite dir (direct join, then the strip-normalised lookup -
             the folder holds deployed-layout paths such as
             ``Binaries/Win64/ue4ss/...`` while the filemap entry was
             indexed with strip prefixes applied)
          2. staging/<mod>/<staged_rel>  (direct)
          3. staging/<mod>/<global_strip>/<staged_rel>  (re-add stripped prefix)
          4. staging/<mod>/<per_mod_strip>/<staged_rel>
        """
        ow = overwrite_dir / staged_rel
        if ow.is_file():
            return ow

        norm = staged_rel.replace("\\", "/")

        if overwrite_lookup:
            src = overwrite_lookup.get(norm.lower())
            if src is not None:
                return src
        if mod_name == _OVERWRITE_NAME:
            # No staging folder exists for the overwrite sentinel - the
            # lookups below would just probe staging/[Overwrite]/.
            return None

        mod_root = staging / mod_name

        src = _resolve_nocase(mod_root, norm)
        if src is not None:
            return src

        # Re-add global strip prefixes (e.g. "oblivionremastered") - the
        # filemap stripped them during build but the file on disk still has them.
        # Use case-insensitive lookup since the prefix is stored lowercase.
        if global_strips:
            for prefix in sorted(global_strips, key=len, reverse=True):
                src = _resolve_nocase(mod_root, prefix + "/" + norm)
                if src is not None:
                    return src

        # Per-mod strip prefixes (user-configured ignore folders)
        for prefix in sorted(mod_strips, key=len, reverse=True):
            src = _resolve_nocase(mod_root, prefix + "/" + norm)
            if src is not None:
                return src

        return None

    # -----------------------------------------------------------------------
    # Restore
    # -----------------------------------------------------------------------

    def _restore_vfs_external_targets(
        self, log_fn, *, manifest_path: Path | None = None,
    ) -> tuple[int, int]:
        """Undo UE5 separator writes that intentionally sit outside the view.

        The manifest and backups remain retryable until every original has
        been restored. Successfully completed entries are removed from a
        partial manifest so a later retry cannot delete a restored vanilla
        file.
        """
        manifest_path = Path(
            manifest_path or self._vfs_external_manifest_path())
        # A process can stop while copying an original, before the completed
        # backup is atomically published. That temporary mirror is never a
        # valid restore source: the physical original has not been changed yet.
        backup_temp_dir = manifest_path.parent / _VFS_CUSTOM_BACKUP_TEMP_DIR
        if backup_temp_dir.is_dir():
            try:
                shutil.rmtree(backup_temp_dir)
            except OSError as exc:
                log_fn(
                    "  WARN: could not clear an incomplete external UE5 "
                    f"backup: {exc}"
                )
        try:
            raw_lines = manifest_path.read_text(
                encoding="utf-8").splitlines()
        except OSError:
            raw_lines = []
        decoded_lines: list[str] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('"'):
                try:
                    decoded = json.loads(line)
                except (TypeError, ValueError):
                    # Ignore a torn final append. Placement only starts after
                    # the complete journal record has been flushed to disk.
                    continue
                if not isinstance(decoded, str):
                    continue
                line = decoded
            if Path(line).is_absolute():
                decoded_lines.append(line)
        lines = list(dict.fromkeys(decoded_lines))
        removed = 0
        restored = 0
        dirs_to_check: set[Path] = set()
        unresolved: list[str] = []
        backup_dir = manifest_path.parent / _CUSTOM_VANILLA_BACKUP_DIR
        handled_backups: set[Path] = set()

        for line in lines:
            target = Path(line)
            dirs_to_check.add(target.parent)
            rel_abs = target.relative_to(target.anchor)
            backup_file = backup_dir / rel_abs
            if backup_file.is_file():
                handled_backups.add(backup_file)
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    removed += 1
                elif target.exists():
                    raise OSError("target is not a regular file")
            except OSError as exc:
                unresolved.append(line)
                log_fn(f"  WARN: could not remove external target {line}: {exc}")
                continue

            if backup_file.is_file():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), target)
                    restored += 1
                except OSError as exc:
                    unresolved.append(line)
                    log_fn(
                        f"  WARN: could not restore external vanilla "
                        f"{target}: {exc}"
                    )

        # A process can stop after moving an original aside but before the
        # deploy manifest is written. The empty VFS marker plus the mirrored
        # backup still gives us enough information to restore that original.
        if backup_dir.is_dir():
            for backup_file in list(backup_dir.rglob("*")):
                if not backup_file.is_file():
                    continue
                if backup_file in handled_backups:
                    continue
                rel = backup_file.relative_to(backup_dir)
                destination = Path("/") / rel
                try:
                    if destination.is_file() or destination.is_symlink():
                        destination.unlink()
                        removed += 1
                    elif destination.exists():
                        raise OSError("target is not a regular file")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), destination)
                    restored += 1
                except OSError as exc:
                    log_fn(
                        f"  WARN: could not restore external vanilla "
                        f"{destination}: {exc}"
                    )

        backups_remain = bool(
            backup_dir.is_dir()
            and any(path.is_file() for path in backup_dir.rglob("*"))
        )
        if unresolved or backups_remain:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic_text(
                manifest_path,
                "".join(
                    json.dumps(line) + "\n"
                    for line in dict.fromkeys(unresolved)
                ),
            )
            raise RestoreIncompleteError(
                "Some external UE5 VFS files could not be restored; "
                "bookkeeping was retained for another Restore attempt."
            )

        if backup_dir.is_dir():
            shutil.rmtree(backup_dir)

        for directory in sorted(
                dirs_to_check, key=lambda path: len(path.parts), reverse=True):
            try:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                pass
        manifest_path.unlink(missing_ok=True)
        return removed, restored

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove every file listed in ue5_deployed.txt, then delete empty dirs."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        from Utils.vfs import cleanup_deployment, has_deployment_state
        external_manifest = self._vfs_external_manifest_path()
        prefix_context = self._vfs_prefix_context_path()
        if (has_deployment_state(self)
                or external_manifest.exists()
                or prefix_context.exists()):
            self._restore_vfs_prefix_targets(_log)
            removed = restored = 0
            if external_manifest.exists():
                removed, restored = self._restore_vfs_external_targets(
                    _log, manifest_path=external_manifest)
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
            detail = ""
            if removed or restored:
                detail = (
                    f" ({removed} external file(s) removed, "
                    f"{restored} original file(s) restored)"
                )
            if not self._ue5_deployed_manifest_path().is_file():
                _log(f"Restore complete{detail}.")
                return
            _log(
                f"VFS restore complete{detail}; a physical UE deployment "
                "also remains and will be restored now."
            )

        prefix_rules = self._prefix_routing_rules()
        if prefix_rules:
            filemap = self.get_effective_filemap_path()
            _log("Restore: removing prefix-routed files ...")
            restore_custom_rules(
                filemap, self._game_path,
                rules=prefix_rules, log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )

        manifest_path = self._ue5_deployed_manifest_path()
        if not manifest_path.is_file():
            _log("Restore: no deployed manifest found - nothing to remove.")
            return

        # Move runtime-generated files (saves, shader cache, etc.) to overwrite/
        # before removing deployed files, using the snapshot taken at deploy time.
        snapshot_path = self._ue5_runtime_snapshot_path()
        overwrite_dir = self.get_effective_overwrite_path()
        if snapshot_path.is_file():
            _log("  Scanning game root for runtime-generated files ...")
            overwrite_dir.mkdir(parents=True, exist_ok=True)
            moved_rt = _move_runtime_files(game_path, snapshot_path, overwrite_dir, _log,
                                           restore_whitelist=self.restore_whitelist_matcher())
            _log(f"  Moved {moved_rt} runtime-generated file(s) to overwrite/.")
            try:
                snapshot_path.unlink()
            except OSError:
                pass

        lines = [
            l.strip() for l in manifest_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        removed = 0
        dirs_to_check: set[Path] = set()

        for rel in lines:
            # Absolute paths are custom-dir files; relative paths are game-root-relative
            is_abs = Path(rel).is_absolute()
            target = Path(rel) if is_abs else game_path / rel
            if target.is_file() or target.is_symlink():
                try:
                    target.unlink()
                    removed += 1
                    if is_abs:
                        dirs_to_check.add(target.parent)
                    else:
                        p = target.parent
                        while p != game_path:
                            dirs_to_check.add(p)
                            p = p.parent
                except OSError as exc:
                    _log(f"  WARN: could not remove {rel}: {exc}")

        # Strip our managed UE4SS mods.txt entries - leaves user sentinels intact,
        # or removes the file entirely if nothing else is left.
        ue4ss_dest = self._resolve_ue4ss_mods_dest()
        if ue4ss_dest is not None:
            try:
                self._update_ue4ss_mods_txt(set(), log_fn=_log)
                # Add the mods dir to the empty-dir sweep set so it can be
                # cleaned up if mods.txt was removed and nothing else remains.
                dirs_to_check.add(_resolve_root_path(
                    game_path, Path(ue4ss_dest) / "mods.txt").parent)
            except Exception as exc:
                _log(f"  WARN: could not clean UE4SS mods.txt: {exc}")

        # Restore any vanilla files that were displaced during deploy
        vanilla_backup_dir = (self._game_path or game_path) / _VANILLA_BACKUP_DIR
        restored_vanilla = 0
        if vanilla_backup_dir.is_dir():
            for backup_file in vanilla_backup_dir.rglob("*"):
                if not backup_file.is_file():
                    continue
                rel = backup_file.relative_to(vanilla_backup_dir)
                dest = game_path / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), dest)
                    restored_vanilla += 1
                except OSError as exc:
                    _log(f"  WARN: could not restore vanilla {rel}: {exc}")
            # Remove the backup dir (and any empty subdirs left behind)
            try:
                shutil.rmtree(vanilla_backup_dir)
            except OSError as exc:
                _log(f"  WARN: could not remove vanilla backup dir: {exc}")

        # Restore custom-dir vanilla files (e.g. engine.ini deployed to a
        # custom separator location outside the game root).
        custom_vanilla_backup_dir = self.get_profile_root() / _CUSTOM_VANILLA_BACKUP_DIR
        if custom_vanilla_backup_dir.is_dir():
            for backup_file in custom_vanilla_backup_dir.rglob("*"):
                if not backup_file.is_file():
                    continue
                # Reconstruct original absolute path from mirrored relative path.
                rel = backup_file.relative_to(custom_vanilla_backup_dir)
                dest = Path("/") / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), dest)
                    restored_vanilla += 1
                    _log(f"  Restored {dest.name} to custom location")
                except OSError as exc:
                    _log(f"  WARN: could not restore custom vanilla {dest}: {exc}")
            try:
                shutil.rmtree(custom_vanilla_backup_dir)
            except OSError as exc:
                _log(f"  WARN: could not remove custom vanilla backup dir: {exc}")

        # Remove directories that became empty, deepest first
        for d in sorted(dirs_to_check, key=lambda p: len(p.parts), reverse=True):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

        manifest_path.unlink(missing_ok=True)
        vanilla_msg = f", {restored_vanilla} vanilla file(s) restored" if restored_vanilla else ""
        _log(f"Restore complete. {removed} file(s) removed{vanilla_msg}.")

    def validate_install(self) -> list[str]:
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        return errors
