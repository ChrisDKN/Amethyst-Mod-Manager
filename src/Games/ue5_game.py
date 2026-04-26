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
  ``dest``  — path relative to game root where matching files are deployed
              (e.g. ``"Content/Paks"``)

Match criteria (one or more):
  ``extensions``  — list of lowercase dotted extensions, e.g. ``[".pak", ".utoc"]``
  ``folder``      — top-level folder name inside the mod (case-insensitive),
                    e.g. ``"ue4ss"`` — matches when the first path segment of
                    the staged file equals this string
  ``strip``       — optional list of leading path segments to strip from the
                    staged relative path before writing to ``dest``
                    (e.g. strip ``["Content/Paks", "Paks"]`` so a staged file at
                    ``Content/Paks/MyMod.pak`` deploys as ``Content/Paks/MyMod.pak``
                    rather than ``Content/Paks/Content/Paks/MyMod.pak``)

Rules are evaluated in order; the first match wins.  Files that match no rule
are deployed to ``ue5_default_dest`` (defaults to the game root itself).

Deploy workflow
---------------
Unlike traditional games, UE5 mod destinations are not folders full of vanilla
files — they are either empty or contain unrelated game content that must not
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
import shutil
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_raw_deploy, _resolve_nocase, _write_deploy_snapshot, _move_runtime_files, _FILEMAP_SNAPSHOT_NAME
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()

# Manifest written next to filemap.txt so restore knows exactly what to remove
_DEPLOYED_MANIFEST = "ue5_deployed.txt"

# Vanilla files displaced by mod files are backed up here (inside the game root)
_VANILLA_BACKUP_DIR = "Amethyst_vanilla_files"

# Custom-dir vanilla files displaced by mod files are backed up here (inside profile root).
# Files are stored with their full absolute path mirrored so restore can reconstruct them.
_CUSTOM_VANILLA_BACKUP_DIR = "ue5_custom_vanilla_backup"


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
                    More specific than ``folder`` — checked first.
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


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class UE5Game(BaseGame):
    """Abstract base for Unreal Engine 5 games with multi-target mod routing."""

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Subclasses must provide
    # -----------------------------------------------------------------------

    @property
    @abstractmethod
    def ue5_routing_rules(self) -> list[UE5Rule]:
        """Ordered list of routing rules.  First match wins."""

    @property
    def ue5_default_dest(self) -> str:
        """Destination for files that match no rule.  Defaults to game root."""
        return ""

    # -----------------------------------------------------------------------
    # Paths (concrete default — subclasses may override)
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence (shared boilerplate)
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._migrate_old_config()
        if not self._paths_file.exists():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_pfx = data.get("prefix_path", "")
            if raw_pfx:
                self._prefix_path = Path(raw_pfx)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.SYMLINK,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._validate_staging()
            if not self._prefix_path or not self._prefix_path.is_dir():
                found = find_prefix(self.steam_id)
                if found:
                    self._prefix_path = found
                    self.save_paths()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._prefix_path = None
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.COPY:    "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

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
            if rule.loose_only and not is_loose:
                continue
            if rule.prefix and norm.lower().startswith(rule.prefix.lower() + "/"):
                # If the rule also has an extension filter, only match when
                # the file's extension is in the list.
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
                return rule, rule.strip, True
            if rule.folder and first_seg == rule.folder.lower():
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
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
                    if hit_idx == 0:
                        # Folder is already at root — no dynamic strip.
                        return rule, rule.strip, True
                    # Strip the prefix above the matched folder so the
                    # folder + contents land under dest.
                    dyn_prefix = "/".join(parts[:hit_idx])
                    return rule, [dyn_prefix, *rule.strip], True
            if rule.filenames and _name_hit(rule.filenames):
                return rule, rule.strip, False
            if rule.extensions and _ext_hit(rule.extensions):
                return rule, rule.strip, False
        return None

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

        dest_rel  — game-root-relative destination directory (may be "")
        final_rel — file path relative to dest_rel

        Placement under ``dest`` depends on rule.flatten:
        - flatten=False (default) — preserve the full mod-relative path under
          dest (no strip applied)
        - flatten=True + folder/prefix/folder_anywhere match — apply the
          rule's strip so the matched folder + contents land under dest.
          If the matched folder is already at the root (no parent to strip),
          the path is preserved as-is so the folder name itself is kept.
        - flatten=True + ext/filename-only match — bare filename under dest
        """
        match = self._match_rule(rel_str)
        if match is not None:
            rule, dyn_strip, is_folder_match = match
            dest = rule.dest
            norm = rel_str.replace("\\", "/")
            if rule.flatten:
                if is_folder_match:
                    # Folder/prefix/folder_anywhere match: strip parents above
                    # the matched folder, keep matched folder + contents.
                    # Empty strip means "no parent to strip" (folder is
                    # already at root) — preserve as-is.
                    final_rel = self._apply_strip(rel_str, dyn_strip) if dyn_strip else norm
                else:
                    # Ext/filename-only: bare filename.
                    final_rel = Path(norm).name
            else:
                # Preserve the full mod-relative path under dest.
                final_rel = norm
        else:
            dest = self.ue5_default_dest
            final_rel = rel_str.replace("\\", "/")
        return dest, final_rel

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

        game_path = self.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap = self.get_effective_filemap_path()
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries)
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries)
        overwrite_dir = staging.parent / "overwrite"

        manifest: list[str] = []
        vanilla_backup_dir = (self._game_path or game_path) / _VANILLA_BACKUP_DIR
        custom_vanilla_backup_dir = self.get_profile_root() / _CUSTOM_VANILLA_BACKUP_DIR
        linked = 0
        skipped = 0
        backed_up = 0

        lines = [
            line.rstrip("\n")
            for line in filemap.read_text(encoding="utf-8").splitlines()
            if "\t" in line
        ]

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
        resolved_by_dest: dict[str, tuple[int, str, str, str, Path, Path, bool, str]] = {}
        for line in lines:
            staged_rel, mod_name = line.split("\t", 1)
            base_dir = per_mod_deploy.get(mod_name, game_path)
            in_custom_dir = base_dir != game_path
            if mod_name in per_mod_raw:
                final_rel = staged_rel.replace("\\", "/")
                dest_rel = ""
                dest_dir = base_dir
                dest_file = dest_dir / final_rel
            else:
                dest_rel, final_rel = self._resolve_entry(staged_rel)
                dest_dir = (base_dir / dest_rel) if dest_rel else base_dir
                dest_file = dest_dir / final_rel
            key = str(dest_file)
            rank = priority_map.get(mod_name, 1 << 30)
            existing = resolved_by_dest.get(key)
            if existing is None or rank < existing[0]:
                resolved_by_dest[key] = (
                    rank, staged_rel, mod_name, final_rel,
                    dest_dir, dest_file, in_custom_dir, dest_rel,
                )
        deploy_order = list(resolved_by_dest.values())
        total = len(deploy_order)

        for i, (_rank, staged_rel, mod_name, final_rel,
                dest_dir, dest_file, in_custom_dir, dest_rel) in enumerate(deploy_order):

            src = self._find_staged_file(
                staging, mod_name, staged_rel,
                per_mod_strip.get(mod_name, []),
                overwrite_dir,
                global_strips=self.mod_folder_strip_prefixes,
            )
            if src is None:
                _log(f"  WARN: source not found for {staged_rel} ({mod_name})")
                skipped += 1
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Back up any real vanilla file before overwriting it.
                # Symlinks are our own previous deploys — don't back those up.
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
                        shutil.copy2(dest_file, backup_target)
                        backed_up += 1

                if dest_file.exists() or dest_file.is_symlink():
                    dest_file.unlink()
                if mode == LinkMode.SYMLINK:
                    dest_file.symlink_to(src)
                elif mode == LinkMode.COPY:
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
                    manifest.append(
                        ((dest_rel + "/" + final_rel) if dest_rel else final_rel)
                    )
                linked += 1
            except OSError as exc:
                _log(f"  ERROR placing {final_rel}: {exc}")
                skipped += 1

            if progress_fn:
                progress_fn(i + 1, total)

        # Write manifest so restore() knows exactly what to remove
        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        manifest_path.write_text("\n".join(manifest), encoding="utf-8")

        # Snapshot the game root so restore() can identify runtime-generated files
        # (saves, shader cache, config files written by the game after launch).
        snapshot_path = self.get_profile_root() / _FILEMAP_SNAPSHOT_NAME
        try:
            _write_deploy_snapshot(game_path, snapshot_path, log_fn=_log)
        except Exception as exc:
            _log(f"  WARN: could not write deploy snapshot: {exc}")

        backed_msg = f", {backed_up} vanilla file(s) backed up" if backed_up else ""
        _log(f"Deploy complete. {linked} file(s) placed{backed_msg}, {skipped} skipped.")

    def _find_staged_file(
        self,
        staging: Path,
        mod_name: str,
        staged_rel: str,
        mod_strips: list[str],
        overwrite_dir: Path,
        global_strips: set[str] | None = None,
    ) -> Path | None:
        """Locate the physical source file for a filemap entry.

        Tries in order:
          1. Overwrite dir
          2. staging/<mod>/<staged_rel>  (direct)
          3. staging/<mod>/<global_strip>/<staged_rel>  (re-add stripped prefix)
          4. staging/<mod>/<per_mod_strip>/<staged_rel>
        """
        ow = overwrite_dir / staged_rel
        if ow.is_file():
            return ow

        mod_root = staging / mod_name
        norm = staged_rel.replace("\\", "/")

        src = _resolve_nocase(mod_root, norm)
        if src is not None:
            return src

        # Re-add global strip prefixes (e.g. "oblivionremastered") — the
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

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove every file listed in ue5_deployed.txt, then delete empty dirs."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        if not manifest_path.is_file():
            _log("Restore: no deployed manifest found — nothing to remove.")
            return

        # Move runtime-generated files (saves, shader cache, etc.) to overwrite/
        # before removing deployed files, using the snapshot taken at deploy time.
        snapshot_path = self.get_profile_root() / _FILEMAP_SNAPSHOT_NAME
        overwrite_dir = self.get_effective_overwrite_path()
        if snapshot_path.is_file():
            _log("  Scanning game root for runtime-generated files ...")
            overwrite_dir.mkdir(parents=True, exist_ok=True)
            moved_rt = _move_runtime_files(game_path, snapshot_path, overwrite_dir, _log)
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
