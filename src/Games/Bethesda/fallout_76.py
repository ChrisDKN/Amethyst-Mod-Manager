"""
fallout_76.py
Fallout 76 game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key


class Fallout_76(Fallout_3):
    """Fallout 76 - a BA2-based Bethesda game with NO plugin system.

    The live game blocks .esp/.esm plugins, so there is no plugins.txt, no load
    order, and no LOOT/Synthesis. Mods load exclusively via the comma-separated
    ``sResourceArchive2List`` key in ``Fallout76Custom.ini`` (My Games/Fallout 76).
    We auto-sync that key from the deployed mod .ba2 files on every deploy/restore,
    mirroring the Vortex FO76 extension. The Archive tab (gated on archive_extensions)
    surfaces the deployed BA2s.
    """

    # No plugin system at all - empty plugin_extensions disables the Plugins tab,
    # load-order tracking, master logic, ESL flags, and orphan-plugin scanning.
    uses_plugins_txt = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = False
    vanilla_plugins: list[str] = []
    vanilla_dlc_plugins: list[str] = []
    # Saves are server-side (character files live on Bethesda's servers); the
    # local My Games\Fallout 76 folder holds only config/screenshots, so there
    # is no Saves folder to redirect.
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
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["dxgi.dll"], flatten=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout76")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Fallout 76")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Fallout76.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout76Prefs.ini"
    _CUSTOM_INI_FILENAME = "Fallout76Custom.ini"
    # BA2-based - no dummy BSA, only the sResourceArchive2List sync below.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None
    # We manage the archive list ourselves (see apply/revert below), so leave the
    # inherited FO3/FNV mod-BSA append path off.
    _archive_list_needs_mod_bsas = False
    _archive_list_fix_name = None
    _archive_list_fix_path = None
    _invalidation_archive_list_key = "sResourceArchive2List"

    # No plugins.txt - FO76 doesn't read one.
    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        return []

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        return

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        return

    @property
    def wizard_tools(self) -> list[WizardTool]:
        # No SE / Wrye Bash / BethINI - none apply to FO76.
        return self._base_wizard_tools()

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

    # -- Non-whitelisted DLL handling --------------------------------------
    # FO76's anti-cheat refuses to launch if unexpected *.dll files sit in the
    # game root. A mod that ships a stray DLL there would brick the game, so on
    # deploy we rename any non-whitelisted root DLL to <name>.dll.nwmode and on
    # restore we rename it back. Mirrors Fo76ini's RenameAddedDLLs/RestoreAddedDLLs.
    # Whitelist = the DLLs the vanilla game ships with (lower-cased for matching).
    _FO76_DLL_WHITELIST = frozenset({
        "bink2w64.dll", "chrome_elf.dll", "concrt140.dll", "d3dcompiler_43.dll",
        "d3dcompiler_46.dll", "d3dcompiler_47.dll", "libcef.dll", "libegl.dll",
        "libglesv2.dll", "msvcp140.dll", "ortp_x64.dll", "steam_api64.dll",
        "vccorlib140.dll", "vcruntime140.dll", "vivoxsdk_x64.dll", "dxgi.dll", 
        "vivoxsdk.dll", "xaudio2_9redist.dll"
    })

    def _rename_non_whitelisted_dlls(self, log_fn) -> None:
        """Rename non-whitelisted root *.dll → *.dll.nwmode so FO76 will launch."""
        if self._game_path is None:
            return
        try:
            entries = list(self._game_path.iterdir())
        except OSError:
            return
        for dll in entries:
            # Case-insensitive .dll match - the prefix FS is case-preserving and
            # mods may ship MyMod.DLL etc.
            if not dll.is_file() or not dll.name.lower().endswith(".dll"):
                continue
            if dll.name.lower() in self._FO76_DLL_WHITELIST:
                continue
            target = dll.with_name(dll.name + ".nwmode")
            try:
                if target.exists():
                    dll.unlink()  # a prior .nwmode already holds the original
                    log_fn(f"  Removed duplicate non-whitelisted DLL: {dll.name}")
                else:
                    dll.rename(target)
                    log_fn(f"  Renamed non-whitelisted DLL: {dll.name} → {target.name}")
            except OSError as exc:
                log_fn(f"  WARN: could not rename {dll.name}: {exc}")

    def _restore_non_whitelisted_dlls(self, log_fn) -> None:
        """Rename *.dll.nwmode back to *.dll on restore."""
        if self._game_path is None:
            return
        try:
            entries = list(self._game_path.iterdir())
        except OSError:
            return
        for nw in entries:
            if not nw.is_file() or not nw.name.lower().endswith(".nwmode"):
                continue
            original = nw.with_name(nw.name[: -len(".nwmode")])
            try:
                if original.exists():
                    nw.unlink()  # original was re-added during deploy - drop the stash
                    log_fn(f"  Removed stale {nw.name} ({original.name} present)")
                else:
                    nw.rename(original)
                    log_fn(f"  Restored DLL: {nw.name} → {original.name}")
            except OSError as exc:
                log_fn(f"  WARN: could not restore {nw.name}: {exc}")

    def swap_launcher(self, log_fn) -> None:
        # FO76 has no SE launcher to swap - repurpose this post-deploy hook to
        # quarantine non-whitelisted DLLs (game files are all in place by now).
        self._rename_non_whitelisted_dlls(log_fn)

    def _restore_launcher(self, log_fn) -> None:
        # Undo the DLL quarantine on restore (mirrors swap_launcher above).
        self._restore_non_whitelisted_dlls(log_fn)

    # -- sResourceArchive2List sync ----------------------------------------
    # FO76's only load mechanism. We keep the enabled mods' .ba2 filenames in
    # Fallout76Custom.ini's sResourceArchive2List, preserving any user-added
    # entries, and remove them again on restore. The set of "ours" is tracked in
    # managed_archives.txt so removed mods don't leave stale entries.

    _FO76_CUSTOM_INI_DEFAULTS = (
        ("sResourceDataDirsFinal", "STRINGS\\"),
        ("bInvalidateOlderFiles", "1"),
    )

    def _fo76_custom_ini_paths(self) -> list[Path]:
        return [d / self._CUSTOM_INI_FILENAME
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

        from Utils.bsa_invalidation import (
            append_to_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        prev = self._tracked_mod_bsas()
        new = self._deployed_mod_bsas()
        for ini in custom_inis:
            ini.parent.mkdir(parents=True, exist_ok=True)
            # Seed the Vortex-style defaults only when absent (don't clobber
            # user edits to these keys).
            for k, v in self._FO76_CUSTOM_INI_DEFAULTS:
                if _read_ini_key(ini, "Archive", k) is None:
                    _set_ini_key(ini, "Archive", k, v)
            current = _read_ini_key(ini, "Archive", key) or ""
            # Drop the .ba2 entries we previously added, then re-add what's
            # deployed now - user-added entries (never in the sidecar) survive.
            updated = remove_many_from_archive_list(current, prev)
            updated = append_to_archive_list(updated, new)
            if updated != current:
                _set_ini_key(ini, "Archive", key, updated)
        self._save_tracked_mod_bsas(new)
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Synced {len(new)} mod BA2(s) into {key} ({names}).")

    def revert_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        custom_inis = [i for i in self._fo76_custom_ini_paths() if i.is_file()]
        if not custom_inis:
            return
        from Utils.bsa_invalidation import remove_many_from_archive_list
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
