"""Translation markers for wizard-tool strings defined in ``Games/*.py``.

Game handlers are toolkit-neutral (plain ABCs with no ``self.tr``), so their
``WizardTool`` labels, descriptions and category headers stay canonical English
at the definition site and are translated where the Qt layer renders them --
see ``_tr_wizard`` in ``gui_qt/app.py``.

``pyside6-lupdate`` only extracts a string when the literal is spelled out at a
``QT_TRANSLATE_NOOP`` call, so every canonical string is listed below verbatim.
Regenerate after adding or rewording a WizardTool: the tuples must stay in sync
with the handlers or the new string ships untranslated.
"""

from PySide6.QtCore import QT_TRANSLATE_NOOP

TR_CONTEXT = "WizardTools"

# Tool labels -- the menu entry text.
WIZARD_LABELS = (
    QT_TRANSLATE_NOOP("WizardTools", "mod.io API Key"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Wrye Bash"),
    QT_TRANSLATE_NOOP("WizardTools", "Run xEdit (Discord version)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run xEdit QAC (Discord version)"),
    QT_TRANSLATE_NOOP("WizardTools", "Downgrade Fallout 3"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (FOSE)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run {0}"),
    QT_TRANSLATE_NOOP("WizardTools", "Run {0} QAC"),
    QT_TRANSLATE_NOOP("WizardTools", "Downgrade Fallout 3 GOTY"),
    QT_TRANSLATE_NOOP("WizardTools", "Run BodySlide (Linux)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Outfit Studio (Linux)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run BodySlide"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Outfit Studio"),
    QT_TRANSLATE_NOOP("WizardTools", "Downgrade Fallout 4"),
    QT_TRANSLATE_NOOP("WizardTools", "Downgrade Skyrim Special Edition"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (F4SE)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run BethINI Pie"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (F4SEVR)"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (xNVSE)"),
    QT_TRANSLATE_NOOP("WizardTools", "Apply 4GB Patch"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Tale of Two Wastelands"),
    QT_TRANSLATE_NOOP("WizardTools", "BSA Decompressor"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Ultimate Edition ESM Fixes"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Viva New Vegas"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Viva New Vegas Extended"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (OBSE)"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (SKSE)"),
    QT_TRANSLATE_NOOP("WizardTools", "SkyGen - Patch Generator"),
    QT_TRANSLATE_NOOP("WizardTools", "Plugin Audit & Cleanup"),
    QT_TRANSLATE_NOOP("WizardTools", "BSA Pack Candidates"),
    QT_TRANSLATE_NOOP("WizardTools", "SSE Display Tweaks Config"),
    QT_TRANSLATE_NOOP("WizardTools", "Engine Fixes Config"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Pandora"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (SKSE64)"),
    QT_TRANSLATE_NOOP("WizardTools", "Run PGPatcher"),
    QT_TRANSLATE_NOOP("WizardTools", "Run SSEEdit"),
    QT_TRANSLATE_NOOP("WizardTools", "Run SSEEdit QAC"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Creation Kit"),
    QT_TRANSLATE_NOOP("WizardTools", "Run ESLifier"),
    QT_TRANSLATE_NOOP("WizardTools", "Run TexGen"),
    QT_TRANSLATE_NOOP("WizardTools", "Run DynDOLOD"),
    QT_TRANSLATE_NOOP("WizardTools", "Run xLODGen"),
    QT_TRANSLATE_NOOP("WizardTools", "Run VRAMr"),
    QT_TRANSLATE_NOOP("WizardTools", "Run BENDr"),
    QT_TRANSLATE_NOOP("WizardTools", "Run ParallaxR"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (SKSEVR)"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Script Extender (SFSE)"),
    QT_TRANSLATE_NOOP("WizardTools", "Patch Game (dtkit-patch)"),
    QT_TRANSLATE_NOOP("WizardTools", "Install me3"),
    QT_TRANSLATE_NOOP("WizardTools", "Merge regulation.bin"),
    QT_TRANSLATE_NOOP("WizardTools", "GPAK unpack / repack"),
    QT_TRANSLATE_NOOP("WizardTools", "Install MGE XE"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Morrowind Code Patch"),
    QT_TRANSLATE_NOOP("WizardTools", "Repair PAK files"),
    QT_TRANSLATE_NOOP("WizardTools", "Run Script Merger"),
    QT_TRANSLATE_NOOP("WizardTools", "Install ReShade"),
)

# Tool descriptions -- the menu entry tooltip.
WIZARD_DESCRIPTIONS = (
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Enter a mod.io key to enable update checks for manually-installed "
        "mod.io mods."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and run Wrye Bash."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run {0} -{1} from the latest xEdit build, released "
        "through the xEdit Discord."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run {0} -{1} -quickautoclean from the latest xEdit "
        "build, released through the xEdit Discord."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Downgrade to pre-Anniversary Edition so that the script extender "
        "(FOSE) works correctly."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and install FOSE into the game folder."),
    QT_TRANSLATE_NOOP("WizardTools", "Install {0}, deploy mods, and run {1}."),
    QT_TRANSLATE_NOOP("WizardTools", "Deploy mods and run {0}QuickAutoClean.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and run the native Linux BodySlide, no Proton prefix "
        "needed."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and run the native Linux Outfit Studio, no Proton prefix "
        "needed."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run BodySlide from the Data folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run Outfit Studio from the Data folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download the latest Fallout 4 Steam Downgrader (game or Creation "
        "Kit) and run it from the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download the latest Skyrim Special Edition Steam Downgrader (game "
        "or Creation Kit) and run it from the game folder."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and install F4SE into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install BethINI Pie and configure Fallout 4 INI settings."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install F4SEVR into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install xNVSE into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Patch FalloutNV.exe to use 4 GB of memory (keeps a backup that can "
        "be restored)."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Run the native Linux TTW installer (merges Fallout 3 + New Vegas) "
        "and add the result as a mod. Requires Fallout 3 installed and a "
        "TTW .mpi package from mod.pub."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Decompress the vanilla BSA archives for faster loading (native "
        "Linux MPI installer) and add the result as a mod. Needs the FNV "
        "BSA Decompressor download from Nexus."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Patch the vanilla .esm masters with community bugfixes (native "
        "Linux MPI installer) and add the result as a mod. Needs the "
        "Ultimate Edition ESM Fixes Remastered download from Nexus."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download the curated Viva New Vegas modlist profile and install it"),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download the curated Viva New Vegas Extended modlist profile and "
        "install it"),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install BethINI Pie and configure Fallout New Vegas INI settings."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and install OBSE into the game folder."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and install SKSE into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Scan your load order for BOS / SkyPatcher patch coverage and "
        "generate new patches."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Scan load order for safe-to-disable plugins, then clean up "
        "orphaned SkyGen BOS/SkyPatcher INIs for plugins that must stay "
        "enabled."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Create or edit SSEDisplayTweaks.ini with per-setting toggles and "
        "descriptions."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Create or edit EngineFixes.toml with per-setting toggles and "
        "descriptions."),
    QT_TRANSLATE_NOOP("WizardTools", "Deploy mods and run Pandora Behaviour Engine+."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install SKSE64 into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install PGPatcher, deploy mods, and run PGPatcher.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install SSEEdit, deploy mods, and run SSEEdit.exe."),
    QT_TRANSLATE_NOOP("WizardTools", "Deploy mods and run SSEEditQuickAutoClean.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run xTESEdit.exe -SSE from the latest xEdit build, "
        "released through the xEdit Discord."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and run xTESEdit.exe -SSE -quickautoclean from the "
        "latest xEdit build, released through the xEdit Discord."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install Creation Kit Platform Extended, deploy mods, and run "
        "CreationKit.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install ESLifier and flag/compact plugins into the light (ESL) "
        "space."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install DynDOLOD tools, deploy mods, and run TexGenx64.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install DynDOLOD tools, deploy mods, and run DynDOLODx64.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install xLODGen, deploy mods, and run xLODGenx64.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install BethINI Pie and configure Skyrim SE INI settings."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download VRAMr from Nexus, deploy mods, and run texture "
        "optimisation."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download BENDr from Nexus, deploy mods, and process normal maps."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download ParallaxR from Nexus, deploy mods, and process parallax "
        "textures."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Scan your load order for Base Object Swapper / SkyPatcher patch "
        "coverage and generate new BOS or SP INI patches."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Scan load order for safe-to-disable plugins, then disable them or "
        "clean up orphaned SkyGen BOS/SkyPatcher INIs for plugins that must "
        "stay enabled."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Rank mods by how many files they could pack into a BSA/BA2, and flag "
        "the ones that would break if packed."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install SKSEVR into the game folder."),
    QT_TRANSLATE_NOOP("WizardTools", "Download and install SFSE into the game folder."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Install BethINI Pie and configure Starfield INI settings."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods and toggle the Darktide Mod Loader bundle patch (runs "
        "the shipped dtkit-patch.exe under Proton). Re-run this wizard "
        "after every game update."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install the me3 mod loader that loads mods for this "
        "game."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Combine the param edits of every enabled mod into one "
        "regulation.bin, instead of only the highest-priority one taking "
        "effect."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Unpack resources.gpak to Unpacked/ or repack Unpacked/ to "
        "resources.gpak in the game root."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install MGE XE (Morrowind Graphics Extender), which "
        "includes MWSE."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and run the Morrowind Code Patch to apply engine-level "
        "bug fixes and improvements."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Restore vanilla PAK entries from the failsafe manifest in the game "
        "root. Use if the game won't load after mods were removed."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Deploy mods, install Script Merger, and run "
        "WitcherScriptMerger.exe."),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Download and install ReShade into the game folder."),
)

# Category headers -- the submenu titles (Utils/wizard_catalog.CATEGORY_ORDER).
WIZARD_CATEGORIES = (
    QT_TRANSLATE_NOOP("WizardTools", "Setup and Installers"),
    QT_TRANSLATE_NOOP("WizardTools", "Install Modlist"),
    QT_TRANSLATE_NOOP("WizardTools", "Body and Outfits"),
    QT_TRANSLATE_NOOP("WizardTools", "Animation and Physics"),
    QT_TRANSLATE_NOOP("WizardTools", "DynDOLOD"),
    QT_TRANSLATE_NOOP("WizardTools", "RSuite (experimental)"),
    QT_TRANSLATE_NOOP("WizardTools", "Patchers and Cleanup"),
    QT_TRANSLATE_NOOP("WizardTools", "xEdit"),
    QT_TRANSLATE_NOOP("WizardTools", "Load Order and Config"),
    QT_TRANSLATE_NOOP("WizardTools", "INI Tweaks"),
    QT_TRANSLATE_NOOP("WizardTools", "NIF Viewer"),
    QT_TRANSLATE_NOOP("WizardTools", "Other"),
)

# LaunchToggle label/hint -- the handler-declared checkboxes in the play-bar's
# Launch settings dialog (BaseGame.launch_toggles). Same story as the tools
# above: declared in a toolkit-neutral handler, translated at display in
# gui_qt/launcher_settings_overlay.py.
LAUNCH_TOGGLES = (
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "Skip the OpenMW launcher (start the game directly)"),
    QT_TRANSLATE_NOOP(
        "WizardTools",
        "The launcher keeps its own copy of the load order and writes it "
        "back to openmw.cfg, which can overwrite what Amethyst deployed. "
        "Off: the launcher opens as usual."),
)
