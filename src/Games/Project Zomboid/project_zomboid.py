"""
project_zomboid.py
Game handler for Project Zomboid (Steam App ID 108600, Nexus: projectzomboid).

Mod structure
-------------
A Project Zomboid mod is a folder containing a lowercase ``mod.info`` text
file.  Two layouts exist:

  * Build 41 / simple: ``<ModFolder>/mod.info`` plus ``<ModFolder>/media/...``
  * Build 42 bundles:  ``<ModFolder>/common/`` (mandatory, may be empty) plus
    one or more game-version subfolders (``42``, ``42.1``, ``42.0.0``, ...)
    each carrying its own ``mod.info``.  The loadable unit is the WHOLE
    ``<ModFolder>`` - the game picks the matching version folder itself - so
    the classifier below must link the bundle, never an individual version
    folder.

Workshop items bundle one or more mods under ``<WorkshopID>/mods/<ModFolder>``
(Steam cache) or ``.../contents/mods/<ModFolder>`` (dev layout).  The
classifier walks staged content recursively, so both wrappers are handled;
``workshop.txt`` / ``workshop.preview.jpg`` upload metadata sits outside the
mod folders and is never deployed.

Load order
----------
There is none.  PZ has no plugins.txt / loadorder.txt and no priority field;
enabled-mod state lives in each save's own metadata, so enabling/disabling is
done in-game (Mods screen).  The manager stages and deploys mod folders only.

Deploy target
-------------
PZ reads manual mods from the user cache dir, NOT the game install:

  * Windows:        ``%UserProfile%/Zomboid/mods``
  * Linux native:   ``~/Zomboid/mods``
  * Proton prefix:  ``<prefix>/drive_c/users/<user>/Zomboid/mods``

The handler resolves the Zomboid cache dir as:

  1. ``zomboid_user_dir`` from paths.json (set for ``-cachedir=`` users) -
     via ``set_zomboid_user_dir()`` or by editing paths.json by hand.
  2. The configured Proton prefix (``drive_c/users/steamuser/Zomboid``,
     falling back to any ``drive_c/users/*/Zomboid`` that exists).
  3. ``~/Zomboid`` (native default).

Deployed folder names have no ordering semantics, so mods link under their
own folder name (typically the Mod ID).  Existing entries in ``mods/`` are
backed up to ``mods_Core/`` during deploy and swapped back on restore; files
that appeared at runtime (configs, logs) are rescued into ``overwrite/``.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deployment import LinkMode
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Backup of pre-existing entries in Zomboid/mods, created next to it.
_CORE_DIR_NAME = "mods_Core"

# Log of every file linked into mods/ during the last deploy, grouped per
# deployed folder with its staged-mod mapping.  Restore diffs the deployed
# tree against this to find runtime-generated files (mod configs, logs) and
# rescue them into overwrite/ instead of deleting them.
_MODS_DEPLOY_LOG = "mods_deployed.txt"

# Path of the Zomboid cache dir inside a Proton prefix.  steamuser is the
# standard Proton account; other users are probed as a fallback.
_PREFIX_USERS_SUBPATH = "drive_c/users"

# Build 42 version-folder names are pure dotted numbers (``42``, ``42.1.5``).
_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+)*$")

# Files at the staging root that belong to the mod manager, not to the mod
# itself - they must never appear in the game.
_STAGING_METADATA = frozenset({"meta.ini", "mm_ignore"})


class ProjectZomboid(BaseGame):

    # The deploy dir may live on a different filesystem than staging (Proton
    # prefix, custom cache dir), so symlinks are the safe default and "copy"
    # must stay selectable.
    deploy_mode_supports_copy = True
    deploy_mode_fallback = LinkMode.SYMLINK

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.SYMLINK
        self._staging_path: Path | None = None
        self._zomboid_user_dir: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Project Zomboid"

    @property
    def game_id(self) -> str:
        return "Project_Zomboid"

    @property
    def exe_name(self) -> str:
        return "ProjectZomboid64.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["ProjectZomboid64", "projectzomboid.sh"]

    @property
    def steam_id(self) -> str:
        return "108600"

    @property
    def nexus_game_domain(self) -> str:
        return "projectzomboid"

    @property
    def mods_dir(self) -> str:
        return "mods"

    # -----------------------------------------------------------------------
    # Install routing
    # -----------------------------------------------------------------------

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"mods"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        # Nexus archives usually ship the mod folder (mod.info inside) at the
        # zip root with no mods/ wrapper - install them as-is.
        return True

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        # Workshop zips carry contents/mods/<Mod>/... - peel both wrappers so
        # the filemap shows the inner mod paths.
        return {"mods", "contents"}

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        # Keep .txt deployable: PZ mods may ship .txt data files.  Workshop
        # upload metadata never reaches a mod folder (the classifier links
        # mod units only).
        return {"readme*", "changelog*", "license*", "licence*", "*.md"}

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def root_folder_deploy_enabled(self) -> bool:
        # PZ loads nothing from the game install root - Root_Folder writes
        # there would be dead weight.
        return False

    @property
    def default_deploy_mode(self) -> str | None:
        return "symlink"

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """The Zomboid cache mods folder - outside the game install."""
        return self.resolve_zomboid_dir() / self.mods_dir

    def resolve_zomboid_dir(self) -> Path:
        """Resolve the Zomboid cache dir: explicit override > Proton prefix
        > native ``~/Zomboid``."""
        if self._zomboid_user_dir is not None:
            return self._zomboid_user_dir
        prefix = self.get_prefix_path()
        if prefix is not None and (prefix / "drive_c" / "users").is_dir():
            return self._prefix_zomboid_dir(prefix)
        return Path.home() / "Zomboid"

    @staticmethod
    def _prefix_zomboid_dir(prefix: Path) -> Path:
        users = prefix / _PREFIX_USERS_SUBPATH
        cand = users / "steamuser" / "Zomboid"
        if cand.is_dir() or not users.is_dir():
            return cand
        for user_dir in sorted(users.iterdir()):
            zomboid = user_dir / "Zomboid"
            if zomboid.is_dir():
                return zomboid
        return cand

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # load_paths / save_paths are inherited from BaseGame (profile-aware);
    # the Zomboid cache dir override rides along as a paths.json extra.

    def _load_paths_extra(self, data: dict) -> None:
        raw = data.get("zomboid_user_dir", "")
        self._zomboid_user_dir = Path(raw) if raw else None

    def _save_paths_extra(self) -> dict:
        return {
            "zomboid_user_dir": (str(self._zomboid_user_dir)
                                 if self._zomboid_user_dir else ""),
        }

    def set_zomboid_user_dir(self, path: Path | str | None) -> None:
        """Pin the Zomboid cache dir (for ``-cachedir=`` or relocated homes)."""
        self._zomboid_user_dir = Path(path) if path else None
        self.save_paths()

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate_install(self) -> list[str]:
        # Zomboid/mods is created on demand at deploy time (the cache dir may
        # not exist until the game has been launched once), so only the game
        # path is required here.
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        return errors

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.SYMLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Link every enabled staged mod's PZ mod folders into
        ``<Zomboid>/mods/``.

        Steps:
          1. Move existing ``mods/`` entries → ``mods_Core/`` (backup).
          2. Classify each enabled staging folder into PZ mod units
             (recursive ``mod.info`` marker, B42-bundle aware).
          3. Per-file link each unit under its own folder name, honouring the
             shared filter chain (per-mod exclusions, ignore filenames).
        """
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        mods_dir = self.get_mod_data_path()
        if mods_dir is None:
            raise RuntimeError("Could not resolve the Zomboid cache directory.")
        mods_dir.mkdir(parents=True, exist_ok=True)

        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        modlist_path = profile_dir / "modlist.txt"
        if not modlist_path.is_file():
            raise RuntimeError(
                f"modlist.txt not found: {modlist_path}\n"
                "Enable at least one mod before deploying."
            )

        entries = read_modlist(modlist_path)
        enabled = [
            e for e in entries
            if e.enabled and not e.is_separator
            and (staging / e.name).is_dir()
        ]

        # The deploy must place exactly the files the Data tab shows, so it
        # reuses the SAME filter chain the filemap is built from:
        #   * per-mod "Disable" exclusions (Mod Files tab), and
        #   * conflict_ignore_filenames.
        from Utils.filegraph.paths import build_path_filters
        from Utils.mods.files import translate_exclusions_for_engine
        from Utils.profiles.state import read_mod_strip_prefixes
        per_mod_prefs = read_mod_strip_prefixes(profile_dir)
        excluded_by_mod = translate_exclusions_for_engine(
            profile_dir, staging, None, per_mod_prefs)
        path_filters = build_path_filters(
            self.conflict_ignore_filenames, None, None, excluded_by_mod)
        strip_map = {
            m: {p.lower() for p in prefs}
            for m, prefs in per_mod_prefs.items()
        }

        # --- Classify staged content into PZ mod units ---
        units: list[tuple[int, str, Path]] = []  # (modlist_idx, staged_name, unit_dir)
        for idx, entry in enumerate(enabled):
            found: list[Path] = []
            _collect_mod_units(staging / entry.name, found)
            if not found:
                _log(f"  WARN: {entry.name} contains no mod.info anywhere - "
                     "not a recognizable Project Zomboid mod, skipped.")
            for unit in found:
                units.append((idx, entry.name, unit))

        total = len(units)
        _log(f"Deploying {total} mod folder(s) into {mods_dir} ({mode.name}) ...")

        # --- Step 1: back up existing mods/ entries ---
        core_dir = mods_dir.parent / _CORE_DIR_NAME
        _log(f"Step 1: Moving existing {mods_dir.name}/ entries → "
             f"{core_dir.name}/ ...")
        moved = self._move_vanilla_aside(mods_dir, core_dir, _log)
        _log(f"  Moved {moved} existing mod folder(s) to {core_dir.name}/.")
        mods_dir.mkdir(parents=True, exist_ok=True)

        # --- Step 2: link every mod unit ---
        deployed_sections: list[tuple[str, str, str, list[str]]] = []
        overwrite_dir = self.get_effective_overwrite_path()
        overlaid_total = 0
        done = 0
        for _idx, staged_name, unit in units:
            dst = mods_dir / _safe_folder_name(unit.name)
            # Filter keys are relative to the staged mod root; `unit` may be a
            # subfolder of it (contents/mods/<Mod>/ layout), so prepend that
            # offset when testing each file against the filter chain.
            offset = _subtree_offset(staging / staged_name, unit)
            keep = _make_keep(path_filters, staged_name, offset.lower(),
                              strip_map.get(staged_name))
            try:
                placed_rels = _deploy_mod_folder(unit, dst, mode, keep)
                # Overlay runtime files rescued to overwrite/ by earlier
                # restores - only while their owning mod is enabled/deployed.
                ow_src = overwrite_dir / "mods" / staged_name
                if offset:
                    ow_src = ow_src / offset.rstrip("/")
                ow_rels = _overlay_overwrite_files(ow_src, dst, mode, _log)
                if ow_rels:
                    overlaid_total += len(ow_rels)
                    placed_rels += ow_rels
                if not placed_rels:
                    try:
                        shutil.rmtree(dst)
                    except OSError:
                        pass
                    _log(f"  {staged_name} / {unit.name} → skipped "
                         "(all files disabled)")
                else:
                    deployed_sections.append(
                        (dst.name, staged_name, offset, placed_rels))
                    _log(f"  {staged_name} / {unit.name} → {dst.name}"
                         + (f" (+{len(ow_rels)} overwrite file(s))"
                            if ow_rels else ""))
            except OSError as err:
                _log(f"  ERROR: failed to deploy {staged_name}/{unit.name}: {err}")
            done += 1
            if progress_fn is not None:
                progress_fn(done, total)

        # Persist the deploy log for restore(); written even when empty so a
        # stale log from a previous deploy is cleared.
        try:
            _write_mods_deploy_log(profile_dir / _MODS_DEPLOY_LOG,
                                   deployed_sections)
        except OSError as err:
            _log(f"  WARN: could not write {_MODS_DEPLOY_LOG}: {err}")

        _log(f"Deploy complete. {len(deployed_sections)} mod folder(s) linked."
             + (f" {overlaid_total} overwrite file(s) overlaid."
                if overlaid_total else ""))

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Undo a previous deploy.

        - Removes the deployed mod folders, rescuing any runtime-generated
          files (per the deploy log) into ``overwrite/mods/<mod>/`` first.
        - Moves ``mods_Core/`` back to ``mods/``.

        Entries in ``mods/`` that the last deploy didn't create are left in
        place (they're runtime/user-created); the next deploy moves them into
        ``mods_Core/`` alongside any pre-deploy content so they keep
        surviving cycles.
        """
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")
        mods_dir = self.get_mod_data_path()
        if mods_dir is None:
            raise RuntimeError("Could not resolve the Zomboid cache directory.")
        core_dir = mods_dir.parent / _CORE_DIR_NAME

        deployed_map = None
        mods_log_path = None
        profile_dir = self._active_profile_dir
        if profile_dir is not None:
            mods_log_path = profile_dir / _MODS_DEPLOY_LOG
            deployed_map = _read_mods_deploy_log(mods_log_path)
        overwrite_dir = self.get_effective_overwrite_path()

        removed_mods = 0
        preserved = 0
        rescued: list[str] = []
        if mods_dir.is_dir():
            legacy_wipe = deployed_map is None and core_dir.is_dir()
            _log(f"Restore: clearing {mods_dir.name}/ ...")
            for child in list(mods_dir.iterdir()):
                try:
                    if child.is_symlink():
                        # Whole-folder symlinks are always deploy leftovers.
                        child.unlink()
                        removed_mods += 1
                        continue
                    entry = (deployed_map.get(child.name)
                             if deployed_map is not None else None)
                    if entry is not None and child.is_dir():
                        staged_name, offset, rels = entry
                        rescued.extend(_rescue_runtime_files(
                            child, offset, rels, overwrite_dir,
                            staged_name, _log))
                        shutil.rmtree(child)
                        removed_mods += 1
                    elif legacy_wipe:
                        if child.is_file():
                            child.unlink()
                        else:
                            shutil.rmtree(child)
                        removed_mods += 1
                    else:
                        preserved += 1
                except OSError as err:
                    _log(f"  WARN: could not remove {child.name}: {err}")
            _log(f"  Removed {removed_mods} entry/entries from {mods_dir.name}/.")
            if rescued:
                _log(f"  Rescued {len(rescued)} runtime file(s) into overwrite/.")
                # Feed the same restore log the standard deploy writes so the
                # rescued files show under Overwrite ▸ Log in the modlist.
                from Utils.deployment.shared import _append_overwrite_log
                _append_overwrite_log(overwrite_dir, rescued, log_fn=_log)
            if preserved:
                _log(f"  Left {preserved} non-deployed entry/entries in place.")
        if mods_log_path is not None:
            try:
                mods_log_path.unlink()
            except OSError:
                pass

        if core_dir.is_dir():
            _log(f"Restore: moving {core_dir.name}/ back to {mods_dir.name}/ ...")
            restored = 0
            mods_dir.mkdir(parents=True, exist_ok=True)
            for child in list(core_dir.iterdir()):
                try:
                    shutil.move(str(child), str(mods_dir / child.name))
                    restored += 1
                except OSError as err:
                    _log(f"  WARN: could not restore {child.name}: {err}")
            try:
                core_dir.rmdir()
            except OSError:
                pass
            _log(f"  Restored {restored} mod folder(s).")
        else:
            _log("Restore: no backup present - nothing to restore.")

        _log("Restore complete.")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _move_vanilla_aside(mods_dir: Path, core_dir: Path, log_fn) -> int:
        """Move every top-level entry currently inside ``mods_dir`` into
        ``core_dir`` so the pre-deploy layout can be restored later.

        Skips entries that are already symlinks (leftovers from a previous
        deploy that wasn't properly restored) - those are simply unlinked.
        """
        if not mods_dir.is_dir():
            return 0
        core_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for child in list(mods_dir.iterdir()):
            try:
                if child.is_symlink():
                    child.unlink()
                    continue
                shutil.move(str(child), str(core_dir / child.name))
                moved += 1
            except OSError as err:
                log_fn(f"  WARN: could not move {child.name} to "
                       f"{core_dir.name}/: {err}")
        return moved


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _has_modinfo(folder: Path) -> bool:
    """True when a file named ``mod.info`` (lowercase filename required by
    PZ on Linux/macOS, matched case-insensitively for safety) sits directly
    inside ``folder`` - that's the marker of a loadable B41-style mod."""
    try:
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.lower() == "mod.info":
                return True
    except OSError:
        pass
    return False


def _is_b42_bundle(folder: Path) -> bool:
    """True when ``folder`` is a Build 42 mod bundle: a direct ``common/``
    child (mandatory in B42, even when empty) plus at least one direct child
    directory carrying its own ``mod.info`` (the per-game-version folders).
    The whole folder is the loadable unit - never link version folders alone.
    """
    has_common = False
    version_or_mod_dirs: list[Path] = []
    try:
        for entry in os.scandir(folder):
            if not entry.is_dir():
                continue
            if entry.name.lower() == "common":
                has_common = True
            elif _has_modinfo(Path(entry.path)):
                version_or_mod_dirs.append(Path(entry.path))
    except OSError:
        return False
    return has_common and bool(version_or_mod_dirs)


def _collect_mod_units(folder: Path, out: list[Path]) -> None:
    """Find every loadable PZ mod unit under *folder*, appending its Path to
    *out*.

    Recursion handles arbitrarily-wrapped staged layouts:
      - **Flat** (Nexus zips, no wrapper): ``<stage>/ModA/mod.info`` - the
        staging root itself (or a child) is the mod.
      - **Workshop items**: ``<stage>/contents/mods/<ModID>/mod.info`` (dev
        layout) or ``<stage>/mods/<ModID>/mod.info`` (Steam cache layout) -
        recurse through the wrapper.
      - **Multi-mod packs**: several mod folders in one archive.
      - **B42 bundles**: ``<stage>/<Mod>/common/`` + ``<Mod>/42/mod.info`` -
        the bundle is ONE unit (never the version folders).

    A folder with ``mod.info`` is a complete mod - its subfolders belong to
    it, so recursion never descends into one.
    """
    if _has_modinfo(folder):
        out.append(folder)
        return
    if _is_b42_bundle(folder):
        out.append(folder)
        return
    try:
        children = [e for e in os.scandir(folder) if e.is_dir()]
    except OSError:
        return
    for entry in children:
        _collect_mod_units(Path(entry.path), out)


def _safe_folder_name(name: str) -> str:
    """Replace path separators in a mod name so it can be used as a folder."""
    return name.replace("/", "_").replace("\\", "_")


def _subtree_offset(stage_root: Path, unit: Path) -> str:
    """Return the forward-slash prefix (real case, with trailing '/') of
    ``unit`` relative to ``stage_root``, or '' when they're the same folder.

    Filter keys are relative to the staged mod root; wrapped units re-base a
    file's unit-relative path back onto the staged-root key space (lowercase
    it for that use).  The real-case form also rebuilds staging paths when
    restore rescues runtime files.
    """
    if unit == stage_root:
        return ""
    try:
        return unit.relative_to(stage_root).as_posix() + "/"
    except ValueError:
        return ""


def _make_keep(path_filters, mod_name: str, offset: str, strip=None):
    """Build a ``keep(rel_key_lower) -> bool`` predicate that mirrors the
    filemap filter chain for ``mod_name``.  ``rel_key_lower`` is relative to
    the walked root; ``offset`` re-bases it onto the staged-root key space.

    ``strip`` is an optional set of lowercase strip prefixes (the mod's "Top
    Level" promotions).  The keys in *path_filters* were translated into the
    post-strip space, so any wrapper prefix is peeled off the combined
    ``offset + rel_key`` before testing - otherwise "Disable" exclusions on
    wrapped content never match and the files deploy anyway."""
    if strip:
        from Utils.mods.files import rel_key_after_strip
        return lambda rel_key: path_filters.accepts(
            mod_name, rel_key_after_strip(offset + rel_key, strip))
    return lambda rel_key: path_filters.accepts(mod_name, offset + rel_key)


def _deploy_mod_folder(src: Path, dst: Path, mode: LinkMode,
                       keep=None) -> list[str]:
    """Place ``src`` (a PZ mod unit) at ``dst``, file-by-file.

    Every file is linked individually (rather than symlinking the whole
    folder) so the same per-file filtering the filemap uses can be honoured
    uniformly:

    SYMLINK  - one symlink per file.
    HARDLINK - one hardlink per file.
    COPY     - copy each file preserving metadata.

    ``keep`` is a predicate ``(rel_key_lower) -> bool`` (rel_key relative to
    ``src``, forward-slash); files for which it returns False are skipped,
    and directories left empty as a result are not created - so the deployed
    tree matches the Data tab exactly.  ``dst`` must not exist on entry - any
    pre-existing directory with the same name is removed first so re-deploys
    stay idempotent (same Mod ID in two staged mods: the modlist-later one
    wins).

    Returns the dst-relative POSIX path (real case) of every file placed, for
    the mods deploy log.
    """
    keep = keep or (lambda _rel: True)
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)

    dst.mkdir(parents=True, exist_ok=True)
    placed: list[str] = []
    src_str = str(src)
    dst_str = str(dst)
    for root, _dirs, files in os.walk(src_str):
        rel = os.path.relpath(root, src_str)
        target_dir = dst_str if rel == "." else os.path.join(dst_str, rel)
        rel_real = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        rel_prefix = rel_real.lower()
        made_dir = rel == "."   # dst root always exists already
        for fname in files:
            # Manager metadata at the mod root (meta.ini etc.) is excluded
            # from the filemap at index level, so keep() never sees it -
            # filter it here too (flat layout puts it at the walk root).
            if rel == "." and fname.lower() in _STAGING_METADATA:
                continue
            if not keep(rel_prefix + fname.lower()):
                continue
            if not made_dir:
                os.makedirs(target_dir, exist_ok=True)
                made_dir = True
            s = os.path.join(root, fname)
            d = os.path.join(target_dir, fname)
            if mode is LinkMode.SYMLINK:
                os.symlink(s, d)
            elif mode is LinkMode.COPY:
                shutil.copy2(s, d)
            else:
                os.link(s, d)
            placed.append(rel_real + fname)
    return placed


def _write_mods_deploy_log(
    path: Path,
    sections: list[tuple[str, str, str, list[str]]],
) -> None:
    """Persist what deploy placed into mods/, one section per deployed
    folder:

        > <dst_name>\t<staged_name>\t<offset>
        <rel path of each file placed, relative to the deployed folder>

    ``offset`` re-bases rels onto the staged-mod key space (wrapped layout);
    it is '' for flat staging.  Restore diffs the on-disk tree against this
    to spot runtime-generated files.
    """
    lines: list[str] = []
    for dst_name, staged_name, offset, rels in sections:
        lines.append(f"> {dst_name}\t{staged_name}\t{offset}")
        lines.extend(rels)
    path.write_text("\n".join(lines) + ("\n" if lines else ""),
                    encoding="utf-8")


def _read_mods_deploy_log(
    path: Path,
) -> "dict[str, tuple[str, str, set[str]]] | None":
    """Parse the mods deploy log written by :func:`_write_mods_deploy_log`.

    Returns ``{dst_name: (staged_name, offset, deployed_rels_lower)}``, or
    None when the log is missing/unreadable (pre-log deploy - the caller
    falls back to the legacy wipe).  An empty dict is a valid log (deploy
    placed no mod folders).
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    entries: dict[str, tuple[str, str, set[str]]] = {}
    current: set[str] | None = None
    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("> "):
            parts = raw[2:].split("\t")
            if len(parts) != 3:
                current = None
                continue
            dst_name, staged_name, offset = parts
            current = set()
            entries[dst_name] = (staged_name, offset, current)
        elif current is not None:
            current.add(raw.lower())
    return entries


def _rescue_runtime_files(
    deployed_dir: Path,
    offset: str,
    deployed_rels: set[str],
    overwrite_dir: Path,
    staged_name: str,
    log_fn,
) -> list[str]:
    """Move runtime-generated files out of *deployed_dir* before it is
    deleted.

    Any regular file not recorded in the deploy log appeared after deploy
    (mod configs, logs, generated dumps).  It is moved to
    ``overwrite/mods/<staged_name>/<offset><rel>`` - the overwrite folder
    owns runtime data; deploy overlays it back onto the deployed folder
    while the owning mod stays enabled.  An existing overwrite copy is
    replaced (the file being rescued is the newest version).

    Returns the overwrite-root-relative POSIX path of every file moved (the
    restore appends them to the overwrite log the UI shows via
    Overwrite ▸ Log).
    """
    moved: list[str] = []
    dep_str = str(deployed_dir)
    for root, _dirs, files in os.walk(dep_str):
        rel_dir = os.path.relpath(root, dep_str)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for fname in files:
            rel = prefix + fname
            if rel.lower() in deployed_rels:
                continue                      # we deployed it - delete normally
            src = os.path.join(root, fname)
            if os.path.islink(src):
                continue                      # a link is never runtime data
            ow_rel = f"mods/{staged_name}/{offset}{rel}"
            dst = overwrite_dir / ow_rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                shutil.move(src, str(dst))
                moved.append(ow_rel)
            except OSError as err:
                log_fn(f"  WARN: could not rescue {rel}: {err}")
    return moved


def _overlay_overwrite_files(ow_dir: Path, dst: Path, mode: LinkMode,
                             log_fn) -> list[str]:
    """Link every file under *ow_dir* (the mod's slice of overwrite/) into
    the deployed folder *dst*, replacing mod-shipped files on collision -
    runtime data always wins over the shipped default.  Called only for mods
    being deployed, so overwrite content of disabled/removed mods never
    reaches the game.

    Returns the dst-relative POSIX rels placed, for the mods deploy log.
    """
    if not ow_dir.is_dir():
        return []
    placed: list[str] = []
    ow_str = str(ow_dir)
    dst_str = str(dst)
    for root, _dirs, files in os.walk(ow_str):
        rel_dir = os.path.relpath(root, ow_str)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for fname in files:
            s = os.path.join(root, fname)
            d = os.path.join(dst_str, prefix.replace("/", os.sep) + fname)
            try:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                if os.path.islink(d) or os.path.exists(d):
                    os.unlink(d)
                if mode is LinkMode.SYMLINK:
                    os.symlink(s, d)
                elif mode is LinkMode.COPY:
                    shutil.copy2(s, d)
                else:
                    os.link(s, d)
                placed.append(prefix + fname)
            except OSError as err:
                log_fn(f"    WARN: overwrite overlay {prefix + fname}: {err}")
    return placed
