"""
witcher_3.py
Game handler for The Witcher 3: Wild Hunt.

Mod structure and routing
--------------------------
Mods ship in the format  <modname>/content/…
The first path segment (<modname>) determines the game-root destination.
Routing is applied at *deploy time*, not install time:

  modname starts with "mod" (case-insensitive)
      → deployed under  <game_root>/mods/<modname>/…
        e.g. modBetterElvesAllInOne/content/foo.xml
             → <game>/mods/modBetterElvesAllInOne/content/foo.xml

  modname starts with "dlc" (case-insensitive)
      → deployed under  <game_root>/dlc/<modname>/…
        e.g. dlcBetterElvesAllInOne/content/foo.xml
             → <game>/dlc/dlcBetterElvesAllInOne/content/foo.xml

  First segment is "bin" (or any other unrecognised name)
      → deployed directly at  <game_root>/<path>

Mods with no mod*/dlc*/bin structure are installed as-is and land at the
game root (mod_install_as_is_if_no_match = True).

Deploy writes a manifest (tw3_deployed.txt) so restore() knows exactly
what to remove.  Vanilla files displaced by mods are backed up in
Amethyst_vanilla_files/ and moved back on restore.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_raw_deploy, _resolve_nocase, _resolve_root_path
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix
from Utils.tw3_filelist import update_menu_filelists

_PROFILES_DIR = get_profiles_dir()

# Manifest written next to filemap.txt so restore knows exactly what to remove
_DEPLOYED_MANIFEST = "tw3_deployed.txt"

# Vanilla files displaced by mod files are backed up here (inside the game root)
_VANILLA_BACKUP_DIR = "Amethyst_vanilla_files"


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------

# Folder names that act as containers in the game root or in mod archives.
# These are skipped during segment scanning so that the actual mod-name
# segment (modFoo / dlcFoo) can be found deeper in the path.
#   "mods"  — game-root container and common archive wrapper
#   "dlc"   — game-root container (may appear before dlcFoo folders)
#   "dlcs"  — alternative plural container name
_SKIP_SEGMENTS = frozenset({"mods", "dlc", "dlcs"})

# Top-level game-root folders that are NOT prefixed — they should be deployed
# directly at the game root with their own folder name preserved.
# When one of these is found (possibly buried under an archive wrapper like
# "Full/" or "Lite/"), everything from that segment onward is kept and
# deployed to the game root ("").
_ROOT_SEGMENTS = frozenset({"bin"})


def _route_path(staged_rel: str) -> tuple[str, str]:
    """Return (dest_prefix, final_rel) for a staged filemap path.

    Scans directory segments (not the filename) looking for:
      - A segment in _SKIP_SEGMENTS  → skip it and look deeper
      - A segment in _ROOT_SEGMENTS  → deploy path-from-here at game root
      - A segment starting with "mod" → deploy under mods/
      - A segment starting with "dlc" → deploy under dlc/

    All other segments (archive wrappers like "Full/", "Lite/", version
    folders, etc.) are silently skipped so that the correct inner structure
    is found regardless of how many wrapper folders the archive contains.

    Returns:
      dest_prefix — game-root-relative destination directory (empty = root)
      final_rel   — staged_rel starting from the qualifying segment, so the
                    modname folder lands directly inside mods/ or dlc/

    Examples:
      "modFoo/content/x.xml"                      → ("mods", "modFoo/content/x.xml")
      "TrueFires_v1.01/modFoo/content/x.xml"      → ("mods", "modFoo/content/x.xml")
      "mods/modFoo/content/x.xml"                 → ("mods", "modFoo/content/x.xml")
      "Full/mods/modFoo/content/x.xml"            → ("mods", "modFoo/content/x.xml")
      "dlcFoo/content/x.xml"                      → ("dlc",  "dlcFoo/content/x.xml")
      "Full/DLC/dlcFoo/content/x.xml"             → ("dlc",  "dlcFoo/content/x.xml")
      "bin/x64/d3d11.dll"                         → ("",     "bin/x64/d3d11.dll")
      "Full/bin/config/r4game/user_config.xml"    → ("",     "bin/config/r4game/user_config.xml")
    """
    norm     = staged_rel.replace("\\", "/")
    segments = norm.split("/")

    # Scan every segment except the last (filename)
    for i, seg in enumerate(segments[:-1]):
        low = seg.lower()
        if low in _SKIP_SEGMENTS:
            continue          # known container — look deeper
        if low in _ROOT_SEGMENTS:
            return "", "/".join(segments[i:])   # e.g. bin/... at game root
        if low.startswith("mod"):
            return "mods", "/".join(segments[i:])
        if low.startswith("dlc"):
            return "dlc", "/".join(segments[i:])

    # No recognised folder found — deploy to game root as-is
    return "", norm


# ---------------------------------------------------------------------------
# Game handler
# ---------------------------------------------------------------------------

class Witcher3(BaseGame):

    def __init__(self):
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
        return "The Witcher 3"

    @property
    def game_id(self) -> str:
        return "witcher_3"

    @property
    def exe_name(self) -> str:
        return "bin/x64/witcher3.exe"

    @property
    def steam_id(self) -> str:
        return "292030"

    @property
    def alt_steam_ids(self) -> list[str]:
        return ["499450"]  # The Witcher 3: Wild Hunt – Game of the Year Edition

    @property
    def nexus_game_domain(self) -> str:
        return "witcher3"

    @property
    def mod_install_prefix(self) -> str:
        """No install-time prefix — routing is resolved at deploy time."""
        return ""

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        """Install mods with any structure; deploy-time routing handles placement."""
        return True

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the game root (mods/, dlc/, bin/, etc.)."""
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
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
                found = None
                for sid in [self.steam_id] + self.alt_steam_ids:
                    found = find_prefix(sid)
                    if found:
                        break
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

    def set_staging_path(self, path: "Path | str | None") -> None:
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
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into their correct game-root subdirectories.

        Each staged filemap entry is routed by _route_path():
          mod*  → <game_root>/mods/<path>
          dlc*  → <game_root>/dlc/<path>
          bin/  → <game_root>/bin/<path>
          other → <game_root>/<path>

        Vanilla files displaced by mods are backed up in Amethyst_vanilla_files/
        so restore() can put them back.  Every placed file is recorded in
        tw3_deployed.txt (next to filemap.txt).
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self._game_path
        filemap   = self.get_effective_filemap_path()
        staging   = self.get_effective_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir        = self.get_profile_root() / "profiles" / profile
        per_mod_strip      = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries)
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries)
        overwrite_dir      = staging.parent / "overwrite"
        vanilla_backup_dir = game_path / _VANILLA_BACKUP_DIR

        # Load any existing manifest so we can distinguish previously-deployed
        # mod files (hardlinks that look like regular files) from real vanilla
        # files.  Without this, a re-deploy without a prior restore would
        # incorrectly back up its own hardlinks as "vanilla files".
        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        _already_deployed: set[str] = set()
        if manifest_path.is_file():
            try:
                _already_deployed = {
                    ln.strip().lower()
                    for ln in manifest_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                }
            except OSError:
                pass

        manifest:  list[str] = []
        linked    = 0
        skipped   = 0
        backed_up = 0
        nocase_cache: dict[Path, dict[str, list[Path]]] = {}
        _dst_dir_cache: dict[Path, dict[str, str]] = {}
        # Track files placed in THIS deploy run so that duplicate filemap
        # entries routing to the same destination don't back each other up.
        # (e.g. Full/mods/modBrutalBlood/… and Lite/mods/modBrutalBlood/…
        # both route to mods/modBrutalBlood/… — the second placement must not
        # treat the first hardlink as a vanilla file.)
        _placed_this_run: set[str] = set()

        lines = [
            ln.rstrip("\n")
            for ln in filemap.read_text(encoding="utf-8").splitlines()
            if "\t" in ln
        ]
        total = len(lines)

        for i, line in enumerate(lines):
            staged_rel, mod_name = line.split("\t", 1)

            base_dir = per_mod_deploy.get(mod_name, game_path)
            in_custom_dir = base_dir != game_path

            if mod_name in per_mod_raw:
                # Raw deploy: skip routing rules, place file as-is
                final_rel = staged_rel.replace("\\", "/")
                dest_dir  = base_dir
                dest_file = dest_dir / final_rel
            else:
                dest_prefix, final_rel = _route_path(staged_rel)
                dest_dir  = (base_dir / dest_prefix) if dest_prefix else base_dir
                dest_file = dest_dir / final_rel

            src = self._find_staged_file(
                staging, mod_name, staged_rel,
                per_mod_strip.get(mod_name, []),
                overwrite_dir,
                nocase_cache,
            )
            if src is None:
                _log(f"  WARN: source not found for {staged_rel} ({mod_name})")
                skipped += 1
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            try:
                # Full case-insensitive resolution from the game root so that
                # intermediate directories with different casing
                # (e.g. the mod stages "Content/" but "content/" already
                # exists on disk) are matched at every path segment, not just
                # at the filename level.
                if in_custom_dir:
                    actual_dest = dest_file
                else:
                    rel_from_game = dest_file.relative_to(game_path)
                    actual_dest = _resolve_root_path(
                        game_path, rel_from_game, dir_cache=_dst_dir_cache
                    )

                # If this logical destination was already placed in this deploy
                # run, skip it entirely — a later same-dest entry (e.g. a
                # lowercase "content/" folder that's the mod-author's vanilla
                # backup sitting next to an uppercase "Content/" mod folder)
                # must NOT overwrite the already-placed modded file.
                # Do this BEFORE mkdir so the duplicate-cased directory is
                # never created in the game folder.
                # For custom-dir files use the absolute path as the dedup key.
                if in_custom_dir:
                    game_rel_lower = str(actual_dest).lower()
                else:
                    game_rel       = actual_dest.relative_to(game_path)
                    game_rel_lower = game_rel.as_posix().lower()
                if game_rel_lower in _placed_this_run:
                    skipped += 1
                    if progress_fn:
                        progress_fn(i + 1, total)
                    continue

                # Place at actual_dest (existing on-disk casing) so we don't
                # create a duplicate-cased sibling directory.
                actual_dest.parent.mkdir(parents=True, exist_ok=True)

                # Back up any real vanilla file before overwriting it.
                # Skip if:
                #  - it's a symlink (our own previous deploy)
                #  - it's already listed in the prior manifest (previous deploy
                #    that wasn't restored — hardlinks look like regular files)
                #  - its inode matches the source (hardlink from a previous
                #    deploy not captured by the manifest)
                if actual_dest.is_file() and not actual_dest.is_symlink() and not in_custom_dir:
                    if game_rel_lower not in _already_deployed:
                        try:
                            is_our_hardlink = (
                                actual_dest.stat().st_ino == src.stat().st_ino
                            )
                        except OSError:
                            is_our_hardlink = False
                        if not is_our_hardlink:
                            backup_target = vanilla_backup_dir / game_rel
                            if not backup_target.exists():
                                backup_target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(actual_dest, backup_target)
                                backed_up += 1

                if actual_dest.exists() or actual_dest.is_symlink():
                    actual_dest.unlink()

                if mode == LinkMode.SYMLINK:
                    actual_dest.symlink_to(src)
                elif mode == LinkMode.COPY:
                    shutil.copy2(src, actual_dest)
                else:
                    try:
                        actual_dest.hardlink_to(src)
                    except (OSError, NotImplementedError):
                        shutil.copy2(src, actual_dest)

                # Record in manifest: absolute path for custom dirs, game-root-relative otherwise
                if in_custom_dir:
                    manifest.append(str(actual_dest))
                else:
                    game_file_rel = actual_dest.relative_to(game_path).as_posix()
                    manifest.append(game_file_rel)
                _placed_this_run.add(game_rel_lower)
                linked += 1
            except OSError as exc:
                _log(f"  ERROR placing {final_rel}: {exc}")
                skipped += 1

            if progress_fn:
                progress_fn(i + 1, total)

        # Write manifest so restore() knows exactly what to remove
        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        manifest_path.write_text("\n".join(manifest), encoding="utf-8")

        backed_msg = f", {backed_up} vanilla file(s) backed up" if backed_up else ""
        _log(f"Deploy complete. {linked} file(s) placed{backed_msg}, {skipped} skipped.")

        update_menu_filelists(game_path, log_fn=_log)

    def _find_staged_file(
        self,
        staging: Path,
        mod_name: str,
        staged_rel: str,
        mod_strips: list[str],
        overwrite_dir: Path,
        cache: dict,
    ) -> Path | None:
        """Locate the physical source file for a filemap entry.

        The filemap stores *routed* paths (e.g. ``mods/modFoo/content/x``)
        rather than raw staging paths.  We therefore have to reverse the
        routing to find the file in staging:

          1. Overwrite dir — check first, always wins.
          2. Direct match under staging/<mod>/  (handles mods whose archive
             already has the right structure, e.g. mods/modFoo/content/…).
          3. Strip leading dest-prefix ("mods/", "dlc/") and try again —
             covers mods where the archive root IS the mod-name folder,
             e.g. staging contains modFoo/content/… directly.
          4. One-level deep search in staging/<mod>/ — covers mods whose
             archive wraps the mod-name folder in an extra directory,
             e.g. TrueFires_v1.01_part_1/modFoo/content/…
          5. Per-mod strip prefixes (user-configured overrides).
        """
        ow = overwrite_dir / staged_rel
        if ow.is_file():
            return ow

        mod_root = staging / mod_name
        norm     = staged_rel.replace("\\", "/")

        # 2. Direct match (handles archives already structured as mods/modFoo/…)
        src = _resolve_nocase(mod_root, norm, cache=cache)
        if src is not None:
            return src

        # 3. Strip leading dest-prefix and try again
        inner = norm
        for dest_prefix in ("mods/", "dlc/"):
            if norm.lower().startswith(dest_prefix):
                inner = norm[len(dest_prefix):]
                break
        if inner != norm:
            src = _resolve_nocase(mod_root, inner, cache=cache)
            if src is not None:
                return src

        # 4. One-level deep scan — the archive may have an extra wrapper folder
        #    (e.g. TrueFires_v1.01_part_1/, Full/, Lite/) between the staging
        #    root and the mod content.  Try both:
        #      a) inner (dest-prefix stripped) — e.g. modFoo/content/x inside Full/
        #      b) norm  (full routed path)     — e.g. mods/modFoo/content/x inside Full/
        #    The second form handles archives like Full/mods/modFoo/content/x
        #    where the mods/ container is inside the wrapper directory.
        try:
            for sub in mod_root.iterdir():
                if sub.is_dir():
                    src = _resolve_nocase(sub, inner, cache=cache)
                    if src is not None:
                        return src
                    if inner != norm:
                        src = _resolve_nocase(sub, norm, cache=cache)
                        if src is not None:
                            return src
        except OSError:
            pass

        # 5. Per-mod strip prefixes (user-configured ignore folders)
        for prefix in sorted(mod_strips, key=len, reverse=True):
            src = _resolve_nocase(mod_root, prefix + "/" + norm, cache=cache)
            if src is not None:
                return src

        return None

    # -----------------------------------------------------------------------
    # Filemap post-processing
    # -----------------------------------------------------------------------

    def post_build_filemap(self, filemap_path: Path, staging_path: Path) -> None:
        """Rewrite filemap.txt so every path reflects the deployed game-root
        structure rather than the raw staging structure.

        Staging paths such as  ``TrueFires_v1.01_part_1/modTrueFires/content/x``
        become the routed paths  ``mods/modTrueFires/content/x``  by applying
        ``_route_path`` to each line.  This makes the treeview and conflict
        display match the actual layout of the game root.

        Mods with "ignore deployment rules" set are left as-is since their
        staged paths are already their final destinations.
        """
        if not filemap_path.is_file():
            return

        # Determine which mods have raw deploy enabled so we skip routing them.
        _raw_mods: set[str] = set()
        if self._active_profile_dir is not None:
            try:
                _sd = load_separator_deploy_paths(self._active_profile_dir)
                _se = read_modlist(self._active_profile_dir / "modlist.txt") if _sd else []
                _raw_mods = expand_separator_raw_deploy(_sd, _se)
            except Exception:
                pass

        lines = filemap_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        for line in lines:
            if "\t" not in line:
                out.append(line)
                continue
            staged_rel, mod_name = line.split("\t", 1)
            if mod_name in _raw_mods:
                out.append(staged_rel + "\t" + mod_name)
            else:
                dest_prefix, final_rel = _route_path(staged_rel)
                routed_rel = (dest_prefix + "/" + final_rel) if dest_prefix else final_rel
                out.append(routed_rel + "\t" + mod_name)
        filemap_path.write_text("\n".join(out), encoding="utf-8")

    # -----------------------------------------------------------------------
    # Restore
    # -----------------------------------------------------------------------

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove every deployed mod file, restore displaced vanilla files,
        prune empty directories, and preserve _MergedFiles folders.
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path     = self._game_path
        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST

        if not manifest_path.is_file():
            _log("Restore: no deployed manifest found — nothing to remove.")
        else:
            lines = [
                ln.strip()
                for ln in manifest_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
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

            # Restore vanilla files that were displaced during deploy
            vanilla_backup_dir = game_path / _VANILLA_BACKUP_DIR
            restored_vanilla = 0
            if vanilla_backup_dir.is_dir():
                for backup_file in vanilla_backup_dir.rglob("*"):
                    if not backup_file.is_file():
                        continue
                    rel  = backup_file.relative_to(vanilla_backup_dir)
                    dest = game_path / rel
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup_file), dest)
                        restored_vanilla += 1
                    except OSError as exc:
                        _log(f"  WARN: could not restore vanilla {rel}: {exc}")
                try:
                    shutil.rmtree(vanilla_backup_dir)
                except OSError as exc:
                    _log(f"  WARN: could not remove vanilla backup dir: {exc}")

            # Prune directories that became empty, deepest first
            for d in sorted(dirs_to_check, key=lambda p: len(p.parts), reverse=True):
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass

            manifest_path.unlink(missing_ok=True)
            vanilla_msg = (
                f", {restored_vanilla} vanilla file(s) restored"
                if restored_vanilla else ""
            )
            _log(f"Restore complete. {removed} file(s) removed{vanilla_msg}.")

        # Preserve _MergedFiles folders so they survive the restore.
        # If the active profile uses profile-specific mods, store them inside
        # the profile directory so they don't bleed across profiles.
        mods_dir = game_path / "mods"
        if mods_dir.is_dir():
            profile_specific = False
            if self._active_profile_dir is not None:
                try:
                    from gui.game_helpers import profile_uses_specific_mods  # type: ignore
                    profile_specific = profile_uses_specific_mods(self._active_profile_dir)
                except Exception:
                    pass

            if profile_specific:
                merged_base = self._active_profile_dir
            else:
                merged_base = self.get_profile_root()

            merged_dir = merged_base / "mods" / "Merged_Mods" / "mods"
            merged_dir.mkdir(parents=True, exist_ok=True)
            for folder in mods_dir.iterdir():
                if folder.is_dir() and "_MergedFiles" in folder.name:
                    dest = merged_dir / folder.name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.move(str(folder), dest)
                    _log(f"Moved merged files folder '{folder.name}' to {merged_dir}.")

        update_menu_filelists(game_path, log_fn=_log)

