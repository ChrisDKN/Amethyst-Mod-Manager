from pathlib import Path

from Games.Bethesda.bethesda_base import BethesdaBase
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key


class Fallout_76(BethesdaBase):
    # Preserve the legacy metadata; LOOT and Synthesis are disabled for this game.
    synthesis_registry_name = "Fallout3"

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout3"

    uses_plugins_txt = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = False
    vanilla_plugins: list[str] = []
    vanilla_dlc_plugins: list[str] = []
    # Fallout 76 saves are server-side.
    supports_profile_saves = False

    @property
    def name(self) -> str:
        return "Fallout 76"

    @property
    def game_id(self) -> str:
        return "Fallout76"

    @property
    def exe_name(self) -> str:
        return "Fallout76.exe"

    @property
    def steam_id(self) -> str:
        return "1151340"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout76"

    @property
    def plugin_extensions(self) -> list[str]:
        # FO76 has no plugin system - disable all plugin tracking.
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml","*read*.txt","*.jpg","*.png","Fallout76Custom.ini"}

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(
                rule_id='fallout_76:967f4eb0cbaa', dest='', filenames=['dxgi.dll'], flatten=True,
            ),
            CustomRule(
                rule_id='fallout_76:42b2892ccb1f', dest='', folders=['Data'], flatten=True,
                loose_only=True,
            ),
            self._saves_routing_rule([".fos"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout76")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Fallout 76")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Fallout76.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout76Prefs.ini"
    _CUSTOM_INI_FILENAME = "Fallout76Custom.ini"
    _invalidation_archive_list_key = "sResourceArchive2List"

    # No plugins.txt - FO76 doesn't read one.
    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        return []

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        return

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        return

    @property
    def frameworks(self) -> dict[str, str]:
        # FO76 has no script extender - skip framework detection entirely.
        return {}

    @property
    def reshade_dll(self) -> str:
        return ""

    @property
    def reshade_arch(self) -> int:
        return 64

    def _restore_quarantined_dlls(self, log_fn, root: Path | None = None) -> None:
        target_root = root or self._game_path
        if target_root is None:
            return
        try:
            entries = list(target_root.iterdir())
        except OSError:
            return
        for nw in entries:
            if not nw.is_file() or not nw.name.lower().endswith(".dll.nwmode"):
                continue
            original = nw.with_name(nw.name[: -len(".nwmode")])
            try:
                if any(p.name.casefold() == original.name.casefold()
                       for p in target_root.iterdir()):
                    log_fn(f"  Kept {nw.name}: {original.name} already exists.")
                else:
                    nw.rename(original)
                    log_fn(f"  Restored DLL: {nw.name} → {original.name}")
            except OSError as exc:
                log_fn(f"  WARN: could not restore {nw.name}: {exc}")

    def swap_launcher(self, log_fn) -> None:
        # Recover files quarantined by older versions without hiding new DLLs.
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_root_write_path
            self._restore_quarantined_dlls(
                log_fn, virtual_root_write_path(self, "."))
            return
        self._restore_quarantined_dlls(log_fn)

    def _restore_launcher(self, log_fn) -> None:
        self._restore_quarantined_dlls(log_fn)

    def _deployed_mod_bsas(self) -> list[str]:
        from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
        from Utils.filegraph.deploy import current, entry_relative_to
        from Utils.mods.modlist import read_modlist

        active = current()
        data_dir = self.get_mod_data_path()
        if active is None or data_dir is None:
            return []
        mods = (read_modlist(self._active_profile_dir / "modlist.txt")
                if self._active_profile_dir is not None else [])
        ordered = [mod.name for mod in mods if not mod.is_separator]
        # The highest-priority mod is first in modlist.txt but must load last.
        ranks = {name: rank for rank, name in enumerate(reversed(ordered))}
        ranks[OVERWRITE_NAME] = len(ordered)
        ranks[ROOT_FOLDER_NAME] = len(ordered) + 1
        archives = []
        roots = {}
        for entry in active.plan.entries:
            relative = entry_relative_to(self, entry, data_dir, roots)
            if (not relative or "/" in relative or "\\" in relative
                    or not relative.lower().endswith(".ba2")):
                continue
            archives.append((ranks.get(entry.mod_name, 0), relative))
        archives.sort(key=lambda item: (item[0], item[1].casefold(), item[1]))
        seen = set()
        names = []
        for _rank, name in archives:
            if name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
        return names

    _FO76_CUSTOM_INI_DEFAULTS = (
        ("sResourceDataDirsFinal", "STRINGS\\"),
        ("bInvalidateOlderFiles", "1"),
    )

    def _fo76_custom_ini_paths(self) -> list[Path]:
        return [self._resolve_ini_path(d, self._CUSTOM_INI_FILENAME)
                for d in {p.parent for p in self._get_archive_ini_paths()}]

    def apply_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        custom_inis = self._fo76_custom_ini_paths()
        if not custom_inis:
            _log("  WARN: Prefix path not set - skipping FO76 archive sync.")
            return

        from Utils.bsa.invalidation import (
            append_to_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        prev = self._tracked_mod_bsas()
        new = self._deployed_mod_bsas()
        preserved = set()
        for ini in custom_inis:
            ini.parent.mkdir(parents=True, exist_ok=True)
            for k, v in self._FO76_CUSTOM_INI_DEFAULTS:
                if _read_ini_key(ini, "Archive", k) is None:
                    _set_ini_key(ini, "Archive", k, v)
            current = _read_ini_key(ini, "Archive", key) or ""
            updated = remove_many_from_archive_list(current, prev)
            preserved.update(name.strip().lower() for name in updated.split(",")
                             if name.strip())
            updated = append_to_archive_list(updated, new)
            if updated != current:
                _set_ini_key(ini, "Archive", key, updated)
        self._save_tracked_mod_bsas(
            [name for name in new if name.lower() not in preserved])
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Synced {len(new)} mod BA2(s) into {key} ({names}).")

    def revert_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        custom_inis = [i for i in self._fo76_custom_ini_paths() if i.is_file()]
        if not custom_inis:
            return
        from Utils.bsa.invalidation import remove_many_from_archive_list
        key = self._invalidation_archive_list_key
        tracked = self._tracked_mod_bsas()
        for ini in custom_inis:
            current = _read_ini_key(ini, "Archive", key)
            if current is None:
                continue
            updated = remove_many_from_archive_list(current, tracked)
            if updated != current:
                _set_ini_key(ini, "Archive", key, updated or None)
        self._save_tracked_mod_bsas([])
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Removed managed BA2 entries from {key} ({names}).")
