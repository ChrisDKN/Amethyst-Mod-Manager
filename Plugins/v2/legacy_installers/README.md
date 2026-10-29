# Legacy Scripted Installers

Installs original C# FOMOD and OMOD packages in Amethyst 2.6.0 or later.
Install through the plugin store, or copy this whole folder into
`~/.config/AmethystModManager/Plugins/` and restart Amethyst.

Use **Install Mod** with the original mod archive. The original installer opens
through Wine/Proton and returns its selected files to Amethyst for staging.
Archives containing multiple OMODs open a package picker first. Choose one package
per installation; nested packages are listed with their folder names. Other files
in the outer archive, such as optional loose-file extras, are not added automatically.
Requested game INI edits appear in a confirmation dialog before installation;
Cancel is selected by default. Failed installation rolls back applied settings.
The helper uses a disposable Wine prefix rather than changing the game's prefix.

Linux requires Wine/Proton and bubblewrap (`bwrap`). Flatpak installations use
these host tools through `flatpak-spawn`, with the installer's own sandbox kept
in place. The standard Amethyst Flatpak already grants the required host access.
Install bubblewrap on the host if it is missing; adding it inside Flatpak is not
required. Inputs in Flatpak's private temporary storage use a shared cache during
installation and are cleaned up afterwards.

The .NET runtime is included;
no separate .NET installation is needed. On first use, the helper is unpacked
and verified under `~/.cache/AmethystModManager/legacy-installers/helpers/`
(or `$XDG_CACHE_HOME`). Later installs reuse it. Allow about 123 MiB for this
cache in addition to the plugin download.

Supports the OBMM script language and recognised C# OMOD scripts for DarNified UI
1.3.2, DarkUI'd DarN 1.6 and Horse Armor Revamped 1.8. Other OMOD C# scripts,
Python/VB scripts, compiled FOMOD `script.dll`, scripted load-order changes and
shader/binary/plugin edits are unsupported. These desktop installers do not run
in collection or Headless jobs.

Compatibility is experimental. Original installers for Project Nevada 2.5,
Unified HUD Project 3.7 with Radar HUD, Horse Armor Revamped 1.8 and DarNified UI
1.3.2 completed in disposable Wine/Xvfb checks; gameplay was not tested.
Disabling the plugin leaves installed mods in place.

This plugin and linked helper are GPL-3.0-only. See LICENSE, NOTICE,
THIRD-PARTY-NOTICES.txt and licenses/ for dependency notices.
[Download the matching source and build instructions](../../Sources/legacy_installers/Legacy-Scripted-Installers-1.1.4-source.zip).

OBMM scripts support comma-separated arguments, quoted escapes, and the built-in
`%NewLine%` and `%Tab%` variables, including selection descriptions.
Selections also skip the Default block after a matching Case has run.
