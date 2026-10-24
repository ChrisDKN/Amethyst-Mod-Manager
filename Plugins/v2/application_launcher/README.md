# Application Launcher

Open **Plugins → Application Launcher** after restarting Amethyst with the
launcher-provider APIs installed. No selected game is required.

The provider's `icon` in `launcher_providers` points to `icons/amethyst.png`
inside this plugin folder. Configure Game's Detected installs buttons and
Add Game's launcher badges use this bundled Amethyst logo.

Use **Add EXE** for an existing application, or **Install Application** to run an
EXE/MSI installer. Save its setup to launch the installer, then select the
installed EXE when it exits. Edit the game directory if its executable lives
under a nested bin folder. Supported games appear in Amethyst's normal Add Game
workflow; the plugin does not configure games automatically. If an application
was added before its custom game definition existed, scans re-evaluate the
default executable-folder guess using that definition. Explicitly changed game
directories are kept.

New prefixes default to `prefixes/<application name>` inside this folder, with
numeric suffixes for duplicates. Existing prefix paths are preserved. Set a different
default for new applications, select another entry's shared prefix, or choose a
custom per-entry path. A shared prefix has one runner selection. Renaming an
entry or changing Proton does not change its prefix location.

Each application displays the icon embedded in its EXE. The compact editor has
an installed-Proton version dropdown and a Browse option for custom runners.
File/folder pickers use the same portal chooser as the manager.

The editor includes installed/custom Proton selection, arguments, launch options,
plain Wine, prefix tools, Wayland, MAKO/LSFG and MangoHud. Proton builds must be
installed separately. UMU uses Amethyst's existing setup and may download the
required Steam runtime at first launch. Initialize a prefix with Proton before
using plain Wine or prefix tools. Configure DLL overrides in winecfg's Libraries
tab; choose Windows components in Winetricks.

Select an installed application and click **Add Steam shortcut…**. Choose the
Steam account from the dropdown; display names and account IDs distinguish
accounts, and native and Flatpak Steam installations are listed separately.
Sign into Steam at least once so its account folder exists. Exit Steam completely
before adding, then reopen it to see the application in your library.

Shortcuts launch through Amethyst's headless service using the application's
current saved prefix, runner and launch settings, without opening the manager UI.
Amethyst does not need to be open. Re-adding updates the same shortcut for that
account and keeps its Steam app ID, tags and other existing properties. Rename
an application and re-add it to update its Steam title. Remove shortcuts through
Steam when no longer needed.

For managed games with a deployed VFS profile, **Launch** and Steam shortcuts
automatically use the manager's Play path, including its VFS view, script extender
and launch hooks. This requires a manager build with Application Launcher VFS
routing. The configured launcher entry, or the same game directory, prefix and
game executable, identifies the managed installation. The deployed profile is
used even when another profile is selected in the manager. No copied launch
command is needed. Missing VFS state or multiple matching deployed profiles
produce an error instead of starting without mods. After Restore, the application
launches normally. Other deployed mod profiles still require Amethyst's Play
control.

The selected account's previous shortcuts file is backed up beside it as
`shortcuts.vdf.amethyst.bak`. Existing unrelated shortcuts are preserved.
Launch helpers live in the persistent `shortcuts/` directory; launch diagnostics
are written to `shortcuts/last-launch.log`. Keep Amethyst and this plugin installed.
Source shortcuts depend on the source checkout and its Python environment;
AppImage shortcuts depend on that AppImage path and a host `/usr/bin/python3`.
Re-add shortcuts after moving your Amethyst installation. Flatpak Steam must
allow `org.freedesktop.Flatpak` host execution to run external launchers, as with
Faugus; this plugin does not change Flatpak permissions.

Prefix moves require an empty destination and idle, restored linked profiles.
They retain the source until settings and linked paths are saved. Remove offers
to keep the prefix or permanently delete it after confirming its location.
Shared, running, and linked prefixes cannot be deleted. Interrupted removals
are recovered when reopening or refreshing the launcher.
Keep this panel open while its application runs;
use Stop before closing. Prefix operations must finish before the editor closes.

Library/preferences are in `data.json`; prefixes and interrupted-operation
records are in the declared persistent directories. Plugin-store updates retain
these directories, including shortcut launch helpers. External prefixes are not deleted by plugin removal.


Translations follow the manager's language setting. `translations/en.json` is the
English source catalogue; copy it to another language code and translate its
values to add a language. See `translations/README.md` for placeholders and
fallback rules. The application editor lives inside this plugin so its labels
and messages can use the same catalogue without changing the manager's editor.
