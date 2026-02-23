"""
base_game.py
Abstract base class that all game handlers must subclass.

To add support for a new game:
  1. Create a new .py file in the Games/ directory
  2. Subclass BaseGame and implement all abstract methods/properties
  3. Drop the file in — it will be auto-discovered by Utils/game_loader.py
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.config_paths import get_game_config_path

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
    def mod_root_deploy_folders(self) -> set[str]:
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
    def steam_id(self) -> str:
        """
        Steam App ID for this game, e.g. '377160' for Fallout 4.
        Used to locate the game's Steam compatibility data / Proton prefix.
        Return an empty string for non-Steam games or if not applicable.
        """
        return ""

    @property
    def heroic_app_names(self) -> list[str]:
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
    def wizard_tools(self) -> list[WizardTool]:
        """
        Per-game helper tools shown in the Wizard dialog.

        Override this in a game subclass to register tools that aid with
        game-specific setup tasks (e.g. downgrading, patching, installing
        runtimes).  Each entry is a :class:`WizardTool` whose
        ``dialog_class_path`` points to the CTkToplevel that implements the
        multi-step wizard.

        Return an empty list (the default) to hide the Wizard button for
        this game.
        """
        return []

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

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    @property
    def _paths_file(self) -> Path:
        """Path to this game's paths.json in the user config directory.

        Resolves to: ~/.config/AmethystModManager/games/<game_name>/paths.json

        Stored outside the app bundle so it survives AppImage updates and
        works correctly when the AppImage filesystem is mounted read-only.
        """
        return get_game_config_path(self.name)

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
