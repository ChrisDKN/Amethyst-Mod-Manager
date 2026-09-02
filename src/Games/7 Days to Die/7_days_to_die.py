"""
7_days_to_die.py
Game handler for 7 Days to Die.

Load order primer
-----------------
7 Days to Die has no loadorder.txt / plugins.txt / priority field.  The game
enumerates ``<game_root>/Mods/*/`` and loads every mod found there in strict
**alphabetical** order.  The community convention is to prefix folder names
with a numeric index (``00_Core``, ``10_Overhaul``, ``99_Tweaks``) to force a
particular load order.

This handler emulates that convention automatically.  When the user deploys,
each enabled mod that *is a Mods/-style mod* (i.e. contains a top-level
``ModInfo.xml``) has its staging folder linked into ``Mods/NNNN_<ModName>/``
where ``NNNN`` is a zero-padded integer derived from the modlist position:
the highest-priority mod (index 0) gets the *highest* NNNN so it sorts last
and therefore **loads last / wins** on any conflicting XPath patch.  Any
leading ``<digits>-`` or ``<digits>_`` prefix a mod already ships with is
stripped first so our ordering is authoritative.

Non-``Mods/`` content
---------------------
Some 7D2D mods ship only loose files for the game's ``Data/`` tree
(e.g. custom POI prefabs under ``Data/Prefabs``, replacement ``.assets``
files under ``7DaysToDie_Data``).  Those have no load-order semantics -
they are plain file replacements - so they are deployed file-by-file from
low-priority to high-priority, with higher-priority mods overwriting lower.
Mixed mods (both ``ModInfo.xml`` and loose ``Data/`` files in one staging
folder) are treated as Mods/-style mods since that is the 7D2D convention.

Mod structure
-------------
Mods install into ``<game_root>/Mods/`` or ``<game_root>/Data/`` etc.
Staged mods live in ``Profiles/7 Days to Die/mods/<ModName>/``.
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

# Zero-padding width for the priority prefix (``0001_Foo``).  Four digits is
# enough for 9999 enabled mods - the modlist cap in practice is well under that.
_PRIORITY_WIDTH = 4

# Strip any leading numeric ordering prefix the mod author may have baked into
# the folder name (``00-Foo``, ``0_Bar``, ``99_Baz``) so our own prefix is
# authoritative.  The regex is deliberately narrow: digits + one separator.
_EXISTING_PREFIX_RE = re.compile(r"^\d+[-_]")

# Log of every mod-owned path written into the game's Data/ tree during the
# last deploy.  Restore reads this to know exactly which Data files to remove
# so it doesn't touch vanilla content.
_DATA_DEPLOY_LOG = "data_deployed.txt"

# Log of every file linked into Mods/ during the last deploy, grouped per
# deployed folder with its staged-mod mapping.  Restore diffs the deployed
# tree against this to find runtime-generated files (mod configs, logs,
# generated dumps) and rescue them instead of deleting them.
_MODS_DEPLOY_LOG = "mods_deployed.txt"

# ``.assets`` files replace Unity asset bundles in ``7DaysToDie_Data/``;
# all other loose content in a Data/-style mod routes to the Prefabs folder.
_ASSETS_EXTS: frozenset[str] = frozenset({".assets"})

# Dest paths are game-root-relative.
_PREFAB_DEST = "Data/Prefabs"
_ASSETS_DEST = "7DaysToDie_Data"


class SevenDaysToDie(BaseGame):

    # 7DTD can deploy by copying, so the saved "copy" mode must be honoured.
    deploy_mode_supports_copy = True

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "7 Days to Die"

    @property
    def game_id(self) -> str:
        return "7_Days_to_Die"

    @property
    def exe_name(self) -> str:
        return "7dLauncher.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        return ["7DaysToDie.exe"]

    @property
    def steam_id(self) -> str:
        return "251570"

    @property
    def nexus_game_domain(self) -> str:
        return "7daystodie"

    @property
    def mods_dir(self) -> str:
        return "Mods"

    # -----------------------------------------------------------------------
    # Mod handling
    # -----------------------------------------------------------------------

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"mods"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"mods"}

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.txt", "*.md", "readme*", "changelog*", "license*"}

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def normalize_folder_case(self) -> bool:
        return True

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        if self._game_path is None:
            return None
        return self._game_path / self.mods_dir

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # load_paths / save_paths are inherited from BaseGame (profile-aware);
    # deploy_mode_supports_copy preserves the "copy" deploy mode, and
    # prefix_numbering is per-profile via the profile-aware _save_settings.

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

    @property
    def prefix_numbering(self) -> bool:
        """If True (default), deployed Mods/ folders are prefixed with a
        zero-padded ``NNNN_`` index derived from the modlist position so the
        game's strict-alphabetical load order matches the manager's priority.

        When False, mods are linked under their bare folder name (with any
        author-supplied numeric prefix preserved) and load order falls back to
        plain alphabetical order - useful for users who manage 7D2D ordering
        themselves or whose mods rely on their original folder names.
        """
        return self._load_settings().get("prefix_numbering", True)

    @prefix_numbering.setter
    def prefix_numbering(self, value: bool) -> None:
        data = self._load_settings()
        data["prefix_numbering"] = bool(value)
        self._save_settings(data)

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Two-channel deploy: atomic Mods/-folder prefix-deploy + loose file
        deploy for Data/-style mods.

        Steps:
          1. Move vanilla ``Mods/`` → ``Mods_Core/``.
          2. For each enabled mod, decide:
               - has ``ModInfo.xml`` at root  → Mods/-style, folder-link with
                 priority prefix.
               - otherwise                    → Data/-style, loose-file link
                 everything into the game root.
          3. Log every Data/-style path so restore can remove only mod-owned
             files without touching vanilla.
        """
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        mods_dir = game_root / self.mods_dir
        staging  = self.get_effective_mod_staging_path()
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
        #   * conflict_ignore_filenames (drops readme/changelog/*.txt etc.).
        # Per-mod strip prefixes ("Top Level" promotions from the Mod Files
        # tab). Exclusions are stored as RAW keys but the keep-filter offset
        # below has these prefixes peeled off, so translate into that same
        # space - otherwise "Disable" silently misses and the files deploy.
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

        # Walk each enabled staging folder and split its *contents* into:
        #   - mods_folders: inner dirs that contain ModInfo.xml (each becomes
        #     its own Mods/NNNN_<name>/ on disk)
        #   - data_items:   everything else (files and non-mod subdirs) -
        #     routed via the loose-file deploy so extensions like .nim land
        #     in Data/Prefabs/.
        # The manager-level staging folder always wraps the real mod content
        # (because mod_staging_requires_subdir=True), so the ModInfo.xml never
        # sits at the staging root itself - we look one level deeper.
        mods_folders: list[tuple[int, str, Path]] = []  # (modlist_idx, staged_name, inner_mod_dir)
        data_items:   list[tuple[int, str, list[Path]]] = []  # (idx, staged_name, loose paths)
        for idx, entry in enumerate(enabled):
            src = staging / entry.name
            inner_mods, loose = _classify_stage_children(src)
            for inner in inner_mods:
                mods_folders.append((idx, entry.name, inner))
            if loose:
                data_items.append((idx, entry.name, loose))

        # --- Step 1: back up vanilla Mods/ ---
        core_dir = game_root / f"{self.mods_dir}_Core"
        _log(f"Step 1: Moving {mods_dir.name}/ → {core_dir.name}/ ...")
        moved = self._move_vanilla_aside(mods_dir, core_dir, _log)
        _log(f"  Moved {moved} vanilla mod folder(s) to {core_dir.name}/.")
        mods_dir.mkdir(parents=True, exist_ok=True)

        # --- Step 2: prefix-link every inner Mods/-style folder ---
        total_mods = len(mods_folders)
        total_data = len(data_items)
        _log(f"Step 2: Linking {total_mods} mod folder(s) into "
             f"{mods_dir.name}/ ({mode.name}) ...")

        done = 0
        total_steps = total_mods + total_data
        use_prefix = self.prefix_numbering
        if not use_prefix:
            _log("  (Folder numbering disabled - linking mods under their "
                 "original folder names; load order is plain alphabetical.)")
        # Per deployed folder: (dst_name, staged_name, offset, placed rels) -
        # persisted so restore can tell deployed files from runtime-generated.
        deployed_sections: list[tuple[str, str, str, list[str]]] = []
        overwrite_dir = self.get_effective_overwrite_path()
        overlaid_total = 0
        for n, (_idx, staged_name, inner) in enumerate(mods_folders):
            if use_prefix:
                # mods_folders is in modlist order (idx 0 = highest priority),
                # so n=0 needs the LARGEST NNNN to sort last alphabetically and
                # win on any conflicting XPath patch.
                priority = total_mods - n
                prefix = str(priority).zfill(_PRIORITY_WIDTH)
                bare_name = _strip_existing_prefix(inner.name)
                dst_name = f"{prefix}_{_safe_folder_name(bare_name)}"
            else:
                # Numbering off: preserve the mod's own folder name (including
                # any author-supplied numeric prefix) so its intended ordering
                # survives untouched.
                dst_name = _safe_folder_name(inner.name)
            dst = mods_dir / dst_name
            # Filter keys are relative to the staged mod root; `inner` may be a
            # subfolder of it (nested / wrapped layout), so prepend that offset
            # when testing each file against the shared filemap filter chain.
            # `offset` is the RAW disk offset (used to locate overwrite/ files
            # and record deployed sections); `keep_offset` re-bases onto the
            # POST-STRIP key space the exclusion/filemap keys live in, so
            # "Disable" checkboxes on wrapped inner mods actually match.
            offset = _subtree_offset(staging / staged_name, inner)
            keep = _make_keep(path_filters, staged_name, offset.lower(),
                              strip_map.get(staged_name))
            try:
                placed_rels = _deploy_mod_folder(inner, dst, mode, keep)
                # Overlay runtime files rescued to overwrite/ by earlier
                # restores - only while their owning mod is enabled/deployed.
                ow_src = overwrite_dir / "Mods" / staged_name
                if offset:
                    ow_src = ow_src / offset.rstrip("/")
                ow_rels = _overlay_overwrite_files(ow_src, dst, mode, _log)
                if ow_rels:
                    overlaid_total += len(ow_rels)
                    placed_rels += ow_rels
                if not placed_rels:
                    # Every file in this inner mod was excluded ("Disable" on a
                    # whole variant) - don't leave an empty Mods/<name>/ behind,
                    # which 7D2D would list as a broken mod with no ModInfo.xml.
                    try:
                        shutil.rmtree(dst)
                    except OSError:
                        pass
                    _log(f"  {staged_name} / {inner.name} → skipped "
                         "(all files disabled)")
                else:
                    deployed_sections.append(
                        (dst_name, staged_name, offset, placed_rels))
                    _log(f"  {staged_name} / {inner.name} → {dst_name}"
                         + (f" (+{len(ow_rels)} overwrite file(s))"
                            if ow_rels else ""))
            except OSError as err:
                _log(f"  ERROR: failed to deploy {staged_name}/{inner.name}: {err}")
            done += 1
            if progress_fn is not None:
                progress_fn(done, total_steps)

        # --- Step 3: loose-file deploy for Data/-style content (low → high priority) ---
        deployed_log_path = profile_dir / _DATA_DEPLOY_LOG
        placed_paths: list[str] = []
        if data_items:
            _log(f"Step 3: Linking loose content from {total_data} mod(s) into "
                 f"game root ({mode.name}) ...")
            # Reverse so lower-priority mods get placed first; higher-priority
            # overwrites on conflict.  data_items is in high→low order.
            data_items_low_to_high = list(reversed(data_items))
            files_placed = 0
            for _idx, staged_name, loose in data_items_low_to_high:
                mod_root = staging / staged_name
                keep = _make_keep(path_filters, staged_name, "",
                                  strip_map.get(staged_name))
                placed = _deploy_loose_items(
                    mod_root, loose, game_root, mode, _log, keep)
                placed_paths.extend(placed)
                files_placed += len(placed)
                _log(f"  {staged_name}: {len(placed)} file(s)")
                done += 1
                if progress_fn is not None:
                    progress_fn(done, total_steps)
            _log(f"  Placed {files_placed} loose file(s) from Data/-style content.")

        # Persist the log of deployed Data/ paths for restore().  We write the
        # file even when empty so a stale log from a previous deploy is cleared.
        try:
            deployed_log_path.parent.mkdir(parents=True, exist_ok=True)
            deployed_log_path.write_text(
                "\n".join(placed_paths) + ("\n" if placed_paths else ""),
                encoding="utf-8",
            )
        except OSError as err:
            _log(f"  WARN: could not write {_DATA_DEPLOY_LOG}: {err}")

        # Same for the Mods/ deploy log (also written when empty, clearing any
        # stale log so restore never diffs against a previous deploy's state).
        try:
            _write_mods_deploy_log(
                profile_dir / _MODS_DEPLOY_LOG, deployed_sections)
        except OSError as err:
            _log(f"  WARN: could not write {_MODS_DEPLOY_LOG}: {err}")

        _log(f"Deploy complete. {total_mods} Mods/-style folder(s) + "
             f"{total_data} mod(s) with loose content."
             + (f" {overlaid_total} overwrite file(s) overlaid."
                if overlaid_total else ""))

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Undo a previous deploy.

        - Removes the deployed ``Mods/`` folders, rescuing any runtime-generated
          files (per the Mods/ deploy log) into ``overwrite/Mods/<mod>/`` first,
          and moves ``Mods_Core/`` back.  Deploy overlays the rescued files back
          while their owning mod is enabled.
        - Reads the Data/-style deploy log and unlinks each listed file.

        Entries in ``Mods/`` that the last deploy didn't create are left in
        place (they're runtime/user-created); the next deploy moves them into
        ``Mods_Core/`` alongside vanilla content so they keep surviving cycles.
        """
        _log = log_fn or (lambda _: None)
        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        mods_dir = game_root / self.mods_dir
        core_dir = game_root / f"{self.mods_dir}_Core"

        # --- Remove Data/-style deployed files, per the log ---
        profile_dir = self._active_profile_dir
        if profile_dir is not None:
            log_path = profile_dir / _DATA_DEPLOY_LOG
            if log_path.is_file():
                _log("Restore: removing Data/-style deployed files ...")
                removed = 0
                stale_dirs: set[str] = set()
                try:
                    for raw in log_path.read_text(encoding="utf-8").splitlines():
                        p = raw.strip()
                        if not p:
                            continue
                        try:
                            pth = Path(p)
                            if pth.is_symlink() or pth.is_file():
                                pth.unlink()
                                removed += 1
                                stale_dirs.add(str(pth.parent))
                        except OSError as err:
                            _log(f"  WARN: could not remove {p}: {err}")
                except OSError as err:
                    _log(f"  WARN: could not read {log_path}: {err}")
                # Remove now-empty directories we touched (best effort,
                # deepest first so children are cleared before parents).
                for d in sorted(stale_dirs, key=len, reverse=True):
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass
                try:
                    log_path.unlink()
                except OSError:
                    pass
                _log(f"  Removed {removed} Data/-style file(s).")

        # --- Clear deployed Mods/ entries and swap vanilla back ---
        # The Mods/ deploy log says exactly which folders/files the last deploy
        # placed.  Files inside those folders that are NOT in the log appeared
        # at runtime (mod configs, logs, generated dumps) - rescue them into
        # overwrite/ so the next deploy overlays them back, instead of deleting
        # them.  With no log (pre-log deploy) fall back to the old
        # wipe-everything behaviour, but only when a vanilla backup proves a
        # deploy actually happened - otherwise Mods/ holds vanilla/user content
        # and must not be touched.
        deployed_map = None
        mods_log_path = None
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
            _log(f"  Restored {restored} vanilla mod folder(s).")
        else:
            _log("Restore: no vanilla backup present - nothing to restore.")

        _log("Restore complete.")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _move_vanilla_aside(mods_dir: Path, core_dir: Path, log_fn) -> int:
        """Move every top-level entry currently inside ``mods_dir`` into
        ``core_dir`` so the vanilla layout can be restored later.

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
    """Return True if a direct child named ``ModInfo.xml`` (case-insensitive)
    exists inside ``folder``.  That's the sole requirement for 7D2D to
    recognise a directory as a Mods/ entry."""
    try:
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.lower() == "modinfo.xml":
                return True
    except OSError:
        pass
    return False


# Files at the staging root that belong to the mod manager, not to the mod
# itself - they must never appear in the game install.
_STAGING_METADATA = frozenset({"meta.ini", "mm_ignore"})


def _contains_modinfo(folder: Path) -> bool:
    """Return True if ``folder`` contains a ``ModInfo.xml``-bearing directory
    anywhere in its subtree (including itself).  Used to decide whether a
    directory with no direct ModInfo.xml is a *wrapper* around real mods
    (recurse into it) or genuine Data/-style content (deploy as loose)."""
    if _has_modinfo(folder):
        return True
    try:
        for entry in os.scandir(folder):
            if entry.is_dir() and _contains_modinfo(Path(entry.path)):
                return True
    except OSError:
        pass
    return False


def _classify_stage_children(stage_root: Path) -> tuple[list[Path], list[Path]]:
    """Split the contents of a staging folder into Mods/-style inner mod dirs
    and Data/-style loose paths.

    Each entry of the returned ``inner_mods`` list is a directory that has a
    direct ``ModInfo.xml`` - deploy it as its own ``Mods/NNNN_<name>/``.

    ``loose`` is a list of Path objects (files *and* directories) that should
    be linked into the game root via the loose-file pipeline.  Manager
    metadata files (``meta.ini`` etc.) are silently excluded.

    Handles arbitrarily-wrapped staged layouts:
      - **Flat** (post-strip, current installs): ``<stage>/ModInfo.xml`` -
        the staging folder itself is the mod.
      - **Nested / variant packs**: ``<stage>/<Wrapper>/<InnerMod>/ModInfo.xml``
        - a directory with no ModInfo.xml but containing mods deeper down is a
        wrapper; recurse into it rather than dumping it to loose.  This keeps
        repacked packs (a redundant top folder, or several variant sub-mods)
        landing in ``Mods/`` instead of ``Data/Prefabs/``.

    A directory that has ModInfo.xml is a complete mod - its own subfolders
    (``Config/`` etc.) belong to it, so recursion never descends *into* one.
    """
    inner_mods: list[Path] = []
    loose: list[Path] = []

    def _walk(folder: Path) -> None:
        # A ModInfo.xml here means the whole folder is one mod - stop.
        if _has_modinfo(folder):
            inner_mods.append(folder)
            return
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                p = Path(entry.path)
                if _contains_modinfo(p):
                    # A mod folder, or a wrapper around one - recurse.
                    _walk(p)
                else:
                    # No mods anywhere inside - genuine Data/-style content.
                    loose.append(p)
            elif entry.is_file():
                if entry.name.lower() in _STAGING_METADATA:
                    continue
                loose.append(Path(entry.path))

    _walk(stage_root)
    return inner_mods, loose


def _strip_existing_prefix(name: str) -> str:
    """Remove a leading numeric ordering prefix (e.g. ``00-`` or ``99_``).

    Preserves everything after the first separator.  If the mod name has no
    such prefix, or stripping would leave an empty string, the original is
    returned unchanged.
    """
    m = _EXISTING_PREFIX_RE.match(name)
    if not m:
        return name
    stripped = name[m.end():]
    return stripped or name


def _safe_folder_name(name: str) -> str:
    """Replace path separators in a mod name so it can be used as a folder."""
    return name.replace("/", "_").replace("\\", "_")


def _subtree_offset(stage_root: Path, inner: Path) -> str:
    """Return the forward-slash prefix (real case, with trailing '/') of
    ``inner`` relative to ``stage_root``, or '' when they're the same folder.

    Filter keys are relative to the staged mod root; in the legacy nested layout
    ``inner`` is a child of it, so this offset re-bases a file's inner-relative
    path back onto the staged-root key space the filters expect (lowercase it
    for that use).  The real-case form also rebuilds staging paths when restore
    rescues runtime files.
    """
    if inner == stage_root:
        return ""
    try:
        return inner.relative_to(stage_root).as_posix() + "/"
    except ValueError:
        return ""


def _make_keep(path_filters, mod_name: str, offset: str, strip=None):
    """Build a ``keep(rel_key_lower) -> bool`` predicate that mirrors the
    filemap filter chain for ``mod_name``.  ``rel_key_lower`` is relative to the
    walked root; ``offset`` re-bases it onto the staged-root key space.

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
    """Place ``src`` (a whole mod staging folder) at ``dst``, file-by-file.

    Every file is linked individually (rather than symlinking the whole folder)
    so the same per-file filtering the filemap uses can be honoured uniformly:

    SYMLINK  - one symlink per file.
    HARDLINK - one hardlink per file (default).
    COPY     - copy each file preserving metadata.

    ``keep`` is a predicate ``(rel_key_lower) -> bool`` (rel_key relative to
    ``src``, forward-slash); files for which it returns False are skipped, and
    directories left empty as a result are not created - so the deployed tree
    matches the Data tab exactly.  ``dst`` must not exist on entry - any
    pre-existing directory with the same name is removed first so re-deploys
    stay idempotent.

    Returns the dst-relative POSIX path (real case) of every file placed, for
    the Mods/ deploy log.
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
    """Persist what deploy placed into Mods/, one section per deployed folder:

        > <dst_name>\\t<staged_name>\\t<offset>
        <rel path of each file placed, relative to the deployed folder>

    ``offset`` re-bases rels onto the staged-mod key space (legacy nested
    layout); it is '' for flat staging.  Restore diffs the on-disk tree against
    this to spot runtime-generated files.
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
    """Parse the Mods/ deploy log written by :func:`_write_mods_deploy_log`.

    Returns ``{dst_name: (staged_name, offset, deployed_rels_lower)}``, or
    None when the log is missing/unreadable (pre-log deploy - the caller falls
    back to the legacy wipe).  An empty dict is a valid log (deploy placed no
    Mods/-style folders).
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
    """Move runtime-generated files out of *deployed_dir* before it is deleted.

    Any regular file not recorded in the deploy log appeared after deploy (mod
    configs, logs, generated dumps).  It is moved to
    ``overwrite/Mods/<staged_name>/<offset><rel>`` - the overwrite folder owns
    runtime data (Stardew-style); deploy overlays it back onto the deployed
    folder while the owning mod stays enabled.  An existing overwrite copy is
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
            ow_rel = f"Mods/{staged_name}/{offset}{rel}"
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
    """Link every file under *ow_dir* (the mod's slice of overwrite/) into the
    deployed folder *dst*, replacing mod-shipped files on collision - runtime
    data always wins over the shipped default.  Called only for mods being
    deployed, so overwrite content of disabled/removed mods never reaches the
    game (Stardew-style orphan skip).

    Returns the dst-relative POSIX rels placed, for the Mods/ deploy log.
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


def _route_loose_file(rel: str, fname: str) -> str | None:
    """Return the game-root-relative destination for a file, or None to skip.

    ``rel`` is the POSIX-normalised path (relative to the mod staging root)
    of the directory the file lives in - ``"."`` means the file is directly
    at the staging root.

    Routing:
      - File already under ``Data/``, ``7DaysToDie_Data/``, ``Mods/`` or
        ``Config/`` → preserve full relative path.
      - Anywhere else, detect by extension:
          ``.nim/.mesh/.ins/.tts/.xml/.jpg/.bak`` + a few prefab-companion
          extensions → ``Data/Prefabs/<fname>`` (flat).
          ``.assets`` → ``7DaysToDie_Data/<fname>``.
      - Readme/changelog/license-style filenames → skipped entirely.
      - Anything else → also routed to ``Data/Prefabs/`` as a safe default,
        since Data/-style mods in 7D2D are overwhelmingly POI prefab packs.
    """
    rel_l = rel.replace("\\", "/").lower()
    first_seg = rel_l.split("/", 1)[0] if rel_l else ""
    if first_seg in ("data", "7daystodie_data", "mods", "config"):
        return rel + "/" + fname if rel != "." else fname

    fname_l = fname.lower()
    if _is_junk(fname_l):
        return None

    ext = os.path.splitext(fname_l)[1]
    if ext in _ASSETS_EXTS:
        return f"{_ASSETS_DEST}/{fname}"

    # Default everything else (prefab-known extensions or otherwise) to
    # Data/Prefabs/.  This matches how POI/prefab authors ship their files.
    return f"{_PREFAB_DEST}/{fname}"


# Filename stems recognised as documentation - deploy would never want these
# landing in the game root.  Match is a prefix check against the lowercased
# stem so ``Readme_v2.txt`` and ``CHANGELOG.md`` are both caught.
_JUNK_STEM_PREFIXES = ("readme", "changelog", "license", "licence",
                       "credits", "manifest", "install")


def _is_junk(fname_l: str) -> bool:
    """Return True for documentation-style filenames that shouldn't be deployed."""
    stem = fname_l.rsplit(".", 1)[0]
    return stem.startswith(_JUNK_STEM_PREFIXES)


def _deploy_loose_items(
    stage_root: Path,
    items: list[Path],
    dst_root: Path,
    mode: LinkMode,
    log_fn,
    keep=None,
) -> list[str]:
    """Route each path in ``items`` under ``dst_root``.

    Items may be files (handled directly) or directories (walked - their
    contents inherit the directory's relative position under ``stage_root``
    for routing purposes).  Higher-priority callers invoke this later so
    existing destination files are replaced on conflict.

    ``keep`` is a predicate ``(rel_key_lower) -> bool`` (rel_key relative to
    ``stage_root``, forward-slash) mirroring the filemap filter chain; files
    for which it returns False are skipped.

    Returns absolute destination paths written, for restore cleanup.
    """
    keep = keep or (lambda _rel: True)
    placed: list[str] = []
    stage_str = str(stage_root)
    dst_str = str(dst_root)

    def _place(src_path: str, rel_dir: str, fname: str) -> None:
        rel_key = (fname if rel_dir == "." else f"{rel_dir}/{fname}").lower()
        if not keep(rel_key):
            return
        routed = _route_loose_file(rel_dir, fname)
        if routed is None:
            return
        d = os.path.join(dst_str, routed.replace("/", os.sep))
        d_parent = os.path.dirname(d)
        try:
            if d_parent:
                os.makedirs(d_parent, exist_ok=True)
            if os.path.islink(d) or os.path.exists(d):
                os.unlink(d)
            if mode is LinkMode.SYMLINK:
                os.symlink(src_path, d)
            elif mode is LinkMode.COPY:
                shutil.copy2(src_path, d)
            else:
                os.link(src_path, d)
            placed.append(d)
        except OSError as err:
            log_fn(f"    WARN: link {src_path} → {d}: {err}")

    for item in items:
        if item.is_file():
            _place(str(item), ".", item.name)
            continue
        if not item.is_dir():
            continue
        # Walk the subtree - the item itself is a subdir of stage_root so its
        # relative path starts with the dir name (e.g. "AJ_Refuge_Camp").
        item_str = str(item)
        for root, _dirs, files in os.walk(item_str):
            rel = os.path.relpath(root, stage_str).replace("\\", "/")
            for fname in files:
                if rel == "." and fname.lower() in _STAGING_METADATA:
                    continue
                _place(os.path.join(root, fname), rel, fname)
    return placed
