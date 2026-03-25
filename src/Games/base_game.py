"""
base_game.py
Abstract base class that all game handlers must subclass.

To add support for a new game:
  1. Create a new .py file in the Games/ directory
  2. Subclass BaseGame and implement all abstract methods/properties
  3. Drop the file in — it will be auto-discovered by Utils/game_loader.py
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.config_paths import get_game_config_dir, get_game_config_path

if TYPE_CHECKING:
    from typing import Any


# ---------------------------------------------------------------------------
# Wizard tool descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WizardTool:
    """Describes a single wizard tool available for a game.

    Attributes:
        id:          Unique machine-readable key, e.g. ``"downgrade_fo3"``.
        label:       Short human-readable name shown on the button.
        description: One-line explanation shown below the label.
        dialog_class_path:  Dotted import path to the dialog class,
                            e.g. ``"wizards.fallout_downgrade.FalloutDowngradeWizard"``.
                            Resolved lazily at runtime so game modules don't
                            need to import heavy GUI code at load time.
        extra:       Arbitrary keyword arguments forwarded to the dialog
                     constructor.  Lets a single dialog class serve multiple
                     games with different configuration (URLs, keywords, etc.).
    """
    id: str
    label: str
    description: str = ""
    dialog_class_path: str = ""
    extra: dict = field(default_factory=dict)


class BaseGame(ABC):

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Human-readable display name, e.g. 'Skyrim Special Edition'.
        Must match the subfolder name under Games/ and Profiles/.
        """

    @property
    @abstractmethod
    def game_id(self) -> str:
        """
        Filesystem-safe identifier, e.g. 'skyrim_se'.
        Typically matches the handler's .py filename (without extension).
        """

    @property
    @abstractmethod
    def exe_name(self) -> str:
        """
        The game's executable filename used to locate it in Steam libraries.
        e.g. 'SkyrimSELauncher.exe'
        """

    @property
    def exe_name_alts(self) -> list[str]:
        """
        Additional executable paths to try when auto-detecting the game install.
        Used for games that ship different exe paths depending on the store
        (e.g. Steam vs Epic/Heroic).  Checked after ``exe_name``.
        """
        return []

    @property
    def root_folder_deploy_enabled(self) -> bool:
        """
        Whether Root_Folder deployment is supported for this game.
        When True (the default), the GUI will transfer files from
        Profiles/<game>/Root_Folder/ into the game's install root on deploy
        (subject to the per-session checkbox in the mod list).
        Set to False in a game handler to permanently disable this feature
        regardless of the checkbox state (e.g. for games where writing to the
        install root would be unsafe or unsupported).
        """
        return True

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        """
        Lowercase top-level folder names to strip from mod files during filemap
        building.  Useful for games where mod authors sometimes wrap their files
        in a redundant top-level folder that the game already provides.

        Example: Subnautica mods are installed into BepInEx/Plugins/, but some
        authors ship their mod as plugins/MyMod/MyMod.dll.  Declaring {"plugins"}
        here causes that leading "plugins/" segment to be stripped so the file
        lands at MyMod/MyMod.dll inside the target directory.

        Stripping is applied only to the first path segment and only when it
        matches one of these names (case-insensitive).  Mods whose files do not
        start with a listed prefix are unaffected.

        Return an empty set (the default) to disable stripping entirely.
        """
        return set()

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        """
        Lowercase filenames that are excluded from conflict detection.

        Files whose name (not path) matches an entry here are counted in the
        filemap as normal but do not contribute to a mod's conflict status.
        Useful for metadata files that many mods ship (e.g. modinfo.ini) which would otherwise cause spurious conflict markers.

        Return an empty set (the default) to disable.
        """
        return set()

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        """
        Like mod_folder_strip_prefixes, but applied AFTER mod_required_top_level_folders
        and mod_auto_strip_until_required have run at install time.

        Use this when the folder to strip is the same one declared in
        mod_required_top_level_folders — so auto-strip can first normalise the
        mod structure down to that folder, then this property removes it.

        Example: REFramework mods must contain a top-level reframework/ folder
        (validated by mod_required_top_level_folders), which is then stripped
        here so files are staged as bare content paths.

        Also applied during filemap building (same as mod_folder_strip_prefixes).

        Return an empty set (the default) to disable.
        """
        return set()

    @property
    def mod_install_prefix(self) -> str:
        """
        A forward-slash path segment automatically prepended to every file's
        destination when a mod is installed.  Applied during install before the
        mod_required_top_level_folders check, so the check sees the final path.

        Example: Witcher 3 mods normally live in <game>/mods/<ModName>/…
        Declaring "mods" here means a mod shipped as ModName/content/… lands at
        mods/ModName/content/… without the user having to do anything.

        Return an empty string (the default) to disable prepending entirely.
        """
        return ""

    @property
    def mod_install_extensions(self) -> set[str]:
        """
        When non-empty, only files whose extension (lowercase, including the
        leading dot) appears in this set will be included in the filemap.
        Files with other extensions are silently excluded during filemap build.

        Example: Baldur's Gate 3 mods only need .pak files in the Mods folder;
        declaring {".pak"} here filters out .txt readme files, images, etc.

        Return an empty set (the default) to include all files.
        """
        return set()

    @property
    def mod_root_deploy_folders(self) -> set[str]: # Legacy, Use custom routing rules instead
        """
        Lowercase top-level folder names inside a mod that should be deployed
        to the game's root directory instead of the normal mod data path.

        During filemap building, files whose first path segment (after
        strip-prefix processing) matches one of these names are written to a
        separate ``filemap_root.txt`` and bypass the ``mod_install_extensions``
        filter.  The game's ``deploy()`` method is responsible for deploying
        that file into the game root.

        Example: Baldur's Gate 3 mods normally install ``.pak`` files into the
        Proton-prefix Mods folder, but some mods ship a ``bin/`` folder that
        must land in the game's install root.  Declaring ``{"bin"}`` here
        routes those files accordingly.

        Return an empty set (the default) to disable this behaviour.
        """
        return set()

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        """
        Lowercase top-level folder names that are valid install roots for this
        game's mods.  When non-empty, the installer checks whether a mod
        contains at least one of these as its top-level directory.  If none
        match, the user is prompted to set a prefix path before the mod is
        installed (MO2-style "Set data directory").

        Example: Cyberpunk 2077 mods must live under archive/, bin/, r6/, or
        red4ext/ — declaring these here causes a warning dialog to appear when
        an author ships loose files with no recognised top-level folder.

        Return an empty set (the default) to disable this check entirely.
        """
        return set()

    @property
    def mod_auto_strip_until_required(self) -> bool:
        """
        When True and mod_required_top_level_folders is non-empty, the
        installer will try to strip leading path segments (one or more
        top-level folders) until at least one remaining top-level folder
        is in mod_required_top_level_folders, instead of showing the
        prefix dialog. When False (the default), the dialog is shown
        when no required top-level folder is found.
        """
        return False

    @property
    def mod_required_file_types(self) -> set[str]:
        """
        File extensions (e.g. {".esp", ".esm", ".esl"}) that are recognised as
        valid top-level content for this game's mods.

        Can be used in two ways:

        1. Alongside mod_required_top_level_folders (fallback): if the
           top-level folder check fails, the installer checks whether the mod
           contains a file with one of these extensions at its root (or can be
           stripped down to one) before falling through to the prefix dialog.

        2. Standalone (without mod_required_top_level_folders): if no required
           top-level folders are declared, this check runs on its own — the mod
           must have a qualifying file at its top level, or the auto-strip /
           prefix-dialog / install-as-is fallbacks apply as normal.

        mod_auto_strip_until_required and mod_install_as_is_if_no_match both
        apply in either mode.

        Return an empty set (the default) to disable this check entirely.
        """
        return set()

    @property
    def additional_install_logic(self) -> list:
        """
        List of callables for game-specific post-install steps.

        Each callable is invoked as fn(dest_root, mod_name, log_fn) after the
        mod files are copied to staging. Use for game-specific file moves or
        transformations (e.g. moving loose .archive files to archive/pc/mod
        for Cyberpunk 2077).

        Return an empty list (the default) for no additional logic.
        """
        return []

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        """
        When True, if both mod_required_top_level_folders and
        mod_required_file_types checks fail for a mod, the mod is installed
        as-is without stripping any folders and without showing the prefix
        dialog.  Use this for games where some mods have a predictable
        structure and others are completely free-form.

        When False (the default), the prefix dialog is shown instead.
        """
        return False

    @property
    def mod_supports_bundles(self) -> bool:
        """
        When True, the installer detects bundle mods — archives whose top-level
        subfolders each contain a ``modinfo.ini`` with the same ``nameasbundle``
        value — and installs every variant as a separate staged mod named
        ``<bundle_name>__<variant_name>``.  The mod list then shows them as a
        radio-button group where enabling one variant auto-disables the others.

        Defaults to False.  Enable in game handlers that use the Fluffy-style
        bundle format (e.g. Resident Evil Village).
        """
        return False

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        """
        Prefix substitutions applied to deployed file paths (destination only).

        Keys are lowercase source prefixes; values are the replacement prefix.
        Applied before the file is written to the game root.

        Example: RE2/RE3 ship mods with ``natives/x64/`` paths but the game
        installs files under ``natives/STM/``.  Declaring
        ``{"natives/x64/": "natives/STM/"}`` here causes the deploy to write
        files to the correct location while the filemap still records the
        original ``natives/x64/`` relative path.

        Defaults to an empty dict (no remapping).
        """
        return {}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        """
        File extension substitutions applied when computing PAK hashes.

        Some RE Engine games store assets with a different format-version suffix
        in the PAK than what mod authors ship.  For example, RE2/RE3 store
        textures as ``.tex.34`` inside the PAK, but mods are distributed with
        ``.tex.10``.  Declaring ``{".tex.10": ".tex.34"}`` here causes the
        patcher to hash the remapped filename when searching the PAK.

        Defaults to an empty dict (no remapping).
        """
        return {}

    @property
    def normalize_folder_case(self) -> bool:
        """
        When True (the default), folder segments that differ only in case across
        mods are unified to a single canonical casing (most-uppercase wins).
        This is correct for case-insensitive games (Windows/Bethesda etc.) where
        ``Scripts/`` and ``scripts/`` are the same directory.

        Set to False for games whose mod loader runs on a case-sensitive file
        system and respects the exact folder names provided by mod authors
        (e.g. Stardew Valley on Linux, where ``Music/`` and ``music/`` are
        different directories).  When False, folder names in the filemap are
        left exactly as each mod provides them — no cross-mod unification.
        """
        return True

    @property
    def mod_staging_requires_subdir(self) -> bool:
        """
        When True, each mod's staging folder must contain a named subdirectory
        at its top level — loose files must NOT sit at the staging folder root.

        This applies to games like Stardew Valley where the mod loader requires
        mods to live in <game>/Mods/<ModName>/.  The staging structure must
        therefore be mods/<StagingName>/<ModName>/... so the filemap records
        <ModName>/... and deploys to Mods/<ModName>/....

        A common mistake is copying an existing Mods/<ModName>/ folder directly
        into staging (resulting in mods/<ModName>/manifest.json at root).  When
        this flag is True the mod list panel automatically wraps the contents of
        any flat staging folder inside a subdirectory named after the folder
        before building the filemap, preventing silent mis-deployment.

        Return False (the default) to disable this behaviour.
        """
        return False

    @property
    def frameworks(self) -> dict[str, str]:
        """
        A mapping of framework display names to their executable filenames.

        The plugin panel checks whether each executable exists in the game's
        root directory **or** in the active profile's Root_Folder staging
        directory, and displays a status banner at the top of the Plugins tab:
          • Green  — "Script Extender Installed"
          • Red    — "Script Extender Not Present"

        Example (Skyrim SE):
            {"Script Extender": "skse64_loader.exe"}

        Return an empty dict (the default) to show no banners.
        """
        return {}

    @property
    def preferred_launch_exe(self) -> str:
        """
        Optional game-root-relative path to an alternative executable that
        should be shown first in the exe dropdown and treated as the launch
        exe (green Play button), but ONLY when the file is present on disk.

        Use this for games where a script extender or mod loader must be
        launched instead of the normal game executable, but where replacing
        the original exe on disk would cause errors (e.g. Oblivion Remastered
        with obse64_loader.exe).

        When the file at this path does not exist, the normal ``exe_name``
        is used as the launch exe instead.

        Return an empty string (the default) to always use ``exe_name``.
        """
        return ""

    @property
    def steam_id(self) -> str:
        """
        Steam App ID for this game, e.g. '377160' for Fallout 4.
        Used to locate the game's Steam compatibility data / Proton prefix.
        Return an empty string for non-Steam games or if not applicable.
        """
        return ""

    @property
    def alt_steam_ids(self) -> list[str]:
        """
        Additional Steam App IDs for alternate editions of this game
        (e.g. GOTY, Complete Edition) that share the same game folder and
        Proton prefix layout.  Checked when the primary steam_id prefix is
        not found.  Return an empty list if there are no alternates.
        """
        return []

    @property
    def heroic_app_names(self) -> list[str]: # Legacy, App names are now detected automatically
        """
        Heroic Games Launcher app identifiers for this game.

        Used as a fallback when the game is not found in Steam libraries.
        Heroic uses different identifiers per store:
          - Epic Games: the 'appName' string from the Epic catalogue
            e.g. 'Pewee' for Cyberpunk 2077
          - GOG: the numeric product ID as a string, or the exact game title
            e.g. '1207658924' or 'The Witcher 3: Wild Hunt'

        List multiple values if the game may appear under different IDs or
        across stores, e.g. ['Pewee', '1207658924'].

        Return an empty list (the default) to disable Heroic detection for
        this game.
        """
        return []

    def get_prefix_path(self) -> Path | None:
        """
        Return the saved Proton prefix path (the pfx/ directory) for this game,
        or None if not set.  Subclasses persist this in paths.json.
        """
        return None

    def set_prefix_path(self, path: "Path | str | None") -> None:
        """
        Save the Proton prefix path and persist it to paths.json.
        Subclasses should override this to write it alongside game_path.
        """

    @property
    def plugin_extensions(self) -> list[str]:
        """
        File extensions that this game treats as plugins (loaded by the engine).
        e.g. ['.esp', '.esl', '.esm'] for Bethesda games.
        Return an empty list to disable all plugin tracking for this game.
        Subclasses override this to enable plugin panel functionality.
        """
        return []

    @property
    def plugins_use_star_prefix(self) -> bool:
        """
        Whether plugins.txt uses the MO2-style '*Name' prefix for enabled plugins.
        Most Bethesda games use True. Oblivion Remastered uses False (bare names only).
        """
        return True

    @property
    def plugins_include_vanilla(self) -> bool:
        """
        Whether vanilla (base-game) plugins should be written into plugins.txt.
        Standard Bethesda games exclude them (the engine handles them separately).
        Oblivion Remastered requires all plugins, including vanilla, to be listed.
        """
        return False

    @property
    def loot_sort_enabled(self) -> bool:
        """
        Whether LOOT plugin sorting is supported for this game.
        Return False for games that don't use or need LOOT (e.g. Subnautica).
        When False, the Sort Plugins button will do nothing.
        Subclasses that support LOOT should override this to return True
        and also provide loot_game_type and loot_masterlist_url.
        """
        return False

    @property
    def loot_game_type(self) -> str:
        """
        The libloot GameType attribute name for this game, e.g. 'SkyrimSE'.
        Only used when loot_sort_enabled is True.
        Must match an attribute of loot.GameType in the libloot Python bindings.
        """
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        """
        URL to download the LOOT masterlist YAML for this game.
        Only used when loot_sort_enabled is True.
        e.g. 'https://raw.githubusercontent.com/loot/skyrimse/v0.21/masterlist.yaml'
        The masterlist is stored as ~/.config/AmethystModManager/LOOT/data/masterlist_<game_id>.yaml.
        Return an empty string if no masterlist URL is known.
        """
        return ""

    @property
    def nexus_game_domain(self) -> str:
        """
        Nexus Mods game domain name used for API requests.
        e.g. 'skyrimspecialedition', 'cyberpunk2077', 'baldursgate3'
        This is the subdomain used in URLs like nexusmods.com/<domain>/mods/...
        Return an empty string to disable Nexus integration for this game.
        """
        return ""

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        """
        Wine DLL overrides to apply to the Proton prefix on every deploy.

        Maps DLL name → load order string using Wine's notation:
          ``"native,builtin"``  — try the Windows DLL first, then Wine's
          ``"native"``          — Windows DLL only
          ``"builtin"``         — Wine's built-in only
          ``"disabled"``        — block the DLL entirely

        These are written into ``user.reg`` under
        ``[Software\\\\Wine\\\\DllOverrides]`` each time ``deploy()`` runs,
        so users do not need to configure winecfg manually.

        Example (BepInEx)::

            return {"winhttp": "native,builtin"}

        Return an empty dict (the default) to leave the prefix unchanged.
        """
        return {}

    @property
    def custom_routing_rules(self) -> list:
        """
        A list of CustomRule objects (from Utils.deploy) that route specific
        file types to a game-root-relative destination directory during deploy.

        Files matching a rule are placed into game_root / rule.dest using only
        their bare filename, and are excluded from the normal deploy destination.

        Example (RE Engine .pak files)::

            from Utils.deploy import CustomRule
            return [CustomRule(dest="pak_mods", extensions=[".pak"])]

        Return an empty list (the default) to use normal routing for all files.
        """
        return []

    @property
    def restore_before_deploy(self) -> bool:
        """
        When True (the default), the GUI runs restore() before deploy() when
        the user clicks Deploy, to clean the game state first. Set to False
        for games where deploy() itself restores then applies mods in one
        cycle (e.g. unpack → remove modded → add mods → repack).
        """
        return True

    @property
    def reshade_dll(self) -> str | None:
        """
        DLL name that ReShade should be installed as in the game folder.

        The correct value depends on the graphics API the game uses:
          - DirectX 9          → ``"d3d9.dll"``
          - DirectX 10/11/12   → ``"dxgi.dll"``
          - OpenGL             → ``"opengl32.dll"``

        When set, a *Install ReShade* entry is automatically added to
        :attr:`wizard_tools` so the wizard appears in the Wizard menu
        without any further changes to the game handler.

        Return ``None`` (the default) to disable ReShade support for this
        game.
        """
        return None

    @property
    def reshade_arch(self) -> int:
        """
        Executable architecture for ReShade DLL selection: ``32`` or ``64``.

        Controls whether ``ReShade32.dll`` or ``ReShade64.dll`` is extracted
        from the installer.  Defaults to ``64``; override to ``32`` for
        legacy 32-bit games (e.g. Fallout 3, Fallout New Vegas, Oblivion,
        Skyrim classic).
        """
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        """
        Per-game helper tools shown in the Wizard dialog.

        Override this in a game subclass to register tools that aid with
        game-specific setup tasks (e.g. downgrading, patching, installing
        runtimes).  Each entry is a :class:`WizardTool` whose
        ``dialog_class_path`` points to the CTkToplevel that implements the
        multi-step wizard.

        Subclasses that add their own tools should extend via::

            @property
            def wizard_tools(self):
                return self._base_wizard_tools() + [WizardTool(...)]

        Return an empty list (the default) to hide the Wizard button for
        this game.
        """
        return self._base_wizard_tools()

    def _base_wizard_tools(self) -> list[WizardTool]:
        """Return the framework-level wizard tools (e.g. ReShade).

        Subclasses that override :attr:`wizard_tools` and need to include
        the auto-generated base tools (such as Install ReShade) should call
        this method rather than ``super().wizard_tools``, to avoid
        accidentally inheriting intermediate class tools::

            @property
            def wizard_tools(self):
                return self._base_wizard_tools() + [WizardTool(...)]
        """
        tools: list[WizardTool] = []
        if self.reshade_dll:
            tools.append(WizardTool(
                id="install_reshade",
                label="Install ReShade",
                description=f"Download and install ReShade ({self.reshade_dll}) into the game folder.",
                dialog_class_path="wizards.reshade.ReShadeWizard",
                extra={"reshade_dll": self.reshade_dll, "reshade_arch": self.reshade_arch},
            ))
        return tools

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_game_path(self) -> Path | None:
        """
        Return the root install directory of the game, or None if not set.
        e.g. /home/deck/.steam/steamapps/common/Skyrim Special Edition
        """

    @abstractmethod
    def get_mod_data_path(self) -> Path | None:
        """
        Return the directory inside the game where mod files are installed.
        e.g. for Skyrim SE: <game_path>/Data
        Returns None if game_path is not configured.
        """

    def get_vanilla_plugins_path(self) -> Path | None:
        """
        Return the directory that contains the game's own (vanilla) plugin files.
        Used to detect which .esp/.esm files belong to the base game so they can
        be shown as non-disableable entries in the plugins panel.

        Defaults to get_mod_data_path(). Override for games whose vanilla plugins
        live in a different subdirectory (e.g. Oblivion Remastered).
        """
        return self.get_mod_data_path()

    @abstractmethod
    def get_mod_staging_path(self) -> Path:
        """
        Return the path where this manager stages installed mods before
        linking them into the game. Always returns a Path regardless of
        whether the directory exists yet.
        e.g. Profiles/Skyrim Special Edition/mods/
        """

    def get_profile_root(self) -> Path:
        """
        Return the root directory that contains the profiles/ folder.

        - Default (no custom staging): the parent of get_mod_staging_path(),
          i.e. Profiles/<game>/ so that profiles/ sits alongside mods/.
        - Custom staging path: the staging path itself is the root, so
          profiles/ lives inside it (e.g. /mnt/ssd/MySkyrimMods/profiles/).
        """
        if self._staging_path is not None:
            return self._staging_path
        return self.get_mod_staging_path().parent

    # Active profile directory — set by the UI whenever the user switches profiles.
    _active_profile_dir: "Path | None" = None

    def set_active_profile_dir(self, profile_dir: "Path | None") -> None:
        """Record which profile folder is currently active.

        Call this whenever the user selects a profile so that
        :meth:`get_effective_mod_staging_path` can decide whether to route
        mods into the profile-specific folder or the shared folder.
        """
        self._active_profile_dir = profile_dir

    def get_effective_mod_staging_path(self) -> Path:
        """Return the mods staging path that should be used for the active profile.

        If the active profile has the ``profile_specific_mods`` flag set in
        ``profile_state.json`` (profile_settings), returns ``<profile_dir>/mods/`` so that all
        mod files are kept inside the profile folder itself.

        Otherwise falls back to the standard :meth:`get_mod_staging_path`
        (shared ``mods/`` folder next to ``profiles/``), which preserves the
        existing behaviour for all profiles without the flag.
        """
        if self._active_profile_dir is not None:
            try:
                # Import here to avoid a circular import at module level.
                from gui.game_helpers import profile_uses_specific_mods  # type: ignore
                if profile_uses_specific_mods(self._active_profile_dir):
                    return self._active_profile_dir / "mods"
            except Exception:
                pass
        return self.get_mod_staging_path()

    def get_effective_overwrite_path(self) -> Path:
        """Return the overwrite directory for the active profile.

        For profiles with the ``profile_specific_mods`` flag the overwrite
        folder lives inside the profile directory itself (a sibling of the
        profile-specific ``mods/`` folder).  For all other profiles it
        falls back to ``<profile_root>/overwrite/``, which is the original
        shared location that sits alongside the shared ``mods/`` folder.

        This is always consistent with :meth:`get_effective_mod_staging_path`:
        ``overwrite/`` is a sibling of ``mods/`` regardless of which root they
        live under.
        """
        return self.get_effective_mod_staging_path().parent / "overwrite"

    def get_effective_filemap_path(self) -> Path:
        """Return the filemap.txt path for the active profile.

        For profile-specific-mods profiles this is ``<profile_dir>/filemap.txt``
        so that each profile maintains its own independent filemap.
        For normal profiles it falls back to ``<profile_root>/filemap.txt``.
        """
        return self.get_effective_mod_staging_path().parent / "filemap.txt"

    def get_effective_root_folder_path(self) -> Path:
        """Return the Root_Folder staging path for the active profile.

        For profiles with the ``profile_specific_mods`` flag the Root_Folder
        lives inside the profile directory itself (a sibling of the
        profile-specific ``mods/`` folder).  For all other profiles it
        falls back to ``<profile_root>/Root_Folder/``, the original shared
        location that sits alongside the shared ``mods/`` folder.
        """
        if self._active_profile_dir is not None:
            try:
                from gui.game_helpers import profile_uses_specific_mods  # type: ignore
                if profile_uses_specific_mods(self._active_profile_dir):
                    return self._active_profile_dir / "Root_Folder"
            except Exception:
                pass
        return self.get_profile_root() / "Root_Folder"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    @property
    def _deploy_state_file(self) -> Path:
        """Path to deploy_state.json for this game.

        Stores the name of the last profile that was successfully deployed so
        that restore() can direct runtime-generated files (ShaderCache, saves,
        etc.) to the correct overwrite folder even when the user has since
        switched to a different profile.
        """
        return self._paths_file.parent / "deploy_state.json"

    def get_last_deployed_profile(self) -> str:
        """Return the name of the last successfully deployed profile, or 'default'."""
        try:
            data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            return data.get("last_deployed") or "default"
        except (OSError, ValueError):
            return "default"

    def save_last_deployed_profile(self, profile_name: str) -> None:
        """Persist profile_name as the last successfully deployed profile."""
        try:
            self._deploy_state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["last_deployed"] = profile_name
            self._deploy_state_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get_last_active_profile(self) -> str:
        """Return the name of the last active profile for this game, or 'default'."""
        try:
            data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            return data.get("last_active_profile") or "default"
        except (OSError, ValueError):
            return "default"

    def save_last_active_profile(self, profile_name: str) -> None:
        """Persist profile_name as the last active profile for this game."""
        try:
            self._deploy_state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["last_active_profile"] = profile_name
            self._deploy_state_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @property
    def _paths_file(self) -> Path:
        """Path to this game's paths.json in the user config directory.

        Resolves to: ~/.config/AmethystModManager/games/<game_name>/paths.json

        Stored outside the app bundle so it survives AppImage updates and
        works correctly when the AppImage filesystem is mounted read-only.
        """
        return get_game_config_path(self.name)

    @property
    def _settings_file(self) -> Path:
        """Path to this game's game_settings.json in the user config directory.

        Stores lightweight per-game UI preferences (e.g. auto_deploy) that are
        independent of the path configuration persisted in paths.json.

        Resolves to: ~/.config/AmethystModManager/games/<game_name>/game_settings.json
        """
        return get_game_config_dir(self.name) / "game_settings.json"

    def _load_settings(self) -> dict:
        try:
            if self._settings_file.exists():
                return json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_settings(self, data: dict) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def auto_deploy(self) -> bool:
        """If True, the manager will automatically deploy after every filemap rebuild."""
        return self._load_settings().get("auto_deploy", False)

    @auto_deploy.setter
    def auto_deploy(self, value: bool) -> None:
        data = self._load_settings()
        data["auto_deploy"] = bool(value)
        self._save_settings(data)

    def _migrate_old_config(self) -> None:
        """One-time migration: copy paths.json from the old in-tree location.

        Previous versions stored config at Games/<name>/paths.json inside the
        project directory.  On first run after upgrading, this copies any
        existing file to the new ~/.config location so settings are preserved.
        The original file is left in place (harmless, and safe for rollback).
        """
        old_path = Path(__file__).parent / self.name / "paths.json"
        new_path = self._paths_file
        if old_path.is_file() and not new_path.is_file():
            shutil.copy2(old_path, new_path)

    @abstractmethod
    def load_paths(self) -> bool:
        """
        Load path configuration from the user config directory.
        Returns True if a valid game_path was loaded, False otherwise.
        """

    @abstractmethod
    def save_paths(self) -> None:
        """Write current path configuration to the user config directory."""

    def set_game_path(self, path: Path | str | None) -> None:
        """
        Convenience: set game_path and immediately persist it.
        Pass None to clear the configured path.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement set_game_path()"
        )

    def _validate_staging(self) -> None:
        """Check that a custom staging path still exists on disk.

        Called during load_paths().  If the user set a custom staging
        directory and that directory has since been deleted, the game
        config is stale — clear paths.json so the user must re-add the
        game through the Add Game dialog.
        """
        if self._staging_path is not None and not self._staging_path.is_dir():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            # Wipe the persisted config so the game shows as unconfigured.
            try:
                self._paths_file.unlink(missing_ok=True)
            except OSError:
                pass

    # -----------------------------------------------------------------------
    # Validation (concrete — subclasses may override)
    # -----------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Returns True if game_path is set and the directory exists on disk."""
        p = self.get_game_path()
        return p is not None and p.exists()

    def validate_install(self) -> list[str]:
        """
        Check that the game is ready to receive mod installs.
        Returns a list of human-readable error strings; empty list = all good.
        """
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        data_path = self.get_mod_data_path()
        if data_path is not None and not data_path.exists():
            errors.append(f"Mod data directory does not exist: {data_path}")
        return errors

    def post_build_filemap(self, filemap_path: "Path", staging_path: "Path") -> None:
        """Called after build_filemap() writes filemap.txt.

        Override in game handlers that need to rewrite the filemap paths to
        reflect the actual deployed layout rather than the staging layout.
        For example, Witcher 3 transforms staging paths such as
        ``TrueFires_v1.01/modTrueFires/content/x.xml`` into the routed path
        ``mods/modTrueFires/content/x.xml`` so the treeview and filemap both
        match the real game-root structure.

        The default implementation is a no-op.
        """

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Called after Clean Game Folder removes deployed files.
        Override in game handlers that need extra cleanup (e.g. resetting
        modsettings.lsx to vanilla for BG3).
        """
