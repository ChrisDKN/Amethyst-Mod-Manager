from __future__ import annotations

from .models import Check

_HELP = {
    "Archive reconstruction": (
        "Amethyst cannot rebuild a BSA or BA2 file using the instructions in this modlist.",
        "Update Amethyst and download the latest version of the modlist, then check again. If it still fails, send the modlist name, version and details below to Amethyst support. Downloading the same mod archives again will not fix this."),
    "Package integrity": (
        "The .wabbajack file is damaged, unreadable or has changed since you opened it.",
        "Select Reload to download it again. If that fails, download a fresh .wabbajack file from the author and open it with Choose file. You can keep your existing mod downloads."),
    "Package identity": (
        "The selected modlist is a different version from the one used for this installation.",
        "Open the existing installation from Installed lists to Resume or Repair it. Choose Update if you want to install the newly selected version."),
    "Deployment": (
        "Mods are currently deployed to the game, so Amethyst cannot safely change the installation.",
        "Close the game and any modding tools. Select Restore in Amethyst, then check requirements again."),
    "File catalog": (
        "A required Amethyst component named Filegraph is missing or could not start.",
        "Update or reinstall Amethyst, then restart it. If you run Amethyst from source, build and install the native Filegraph component first."),
    "Game": (
        "This modlist is for a different game or edition from the one selected in Amethyst.",
        "Select the correct game from the main game menu. Reopen the modlist, then check requirements again."),
    "Additional game": (
        "This modlist also needs files from another game.",
        "Install the game named below with the required version and DLC. Add or select that game in Amethyst, then check requirements again."),
    "Required game file": (
        "A required game file is missing or is not the exact version expected by this modlist. Files with the same name can differ between stores, languages and game updates.",
        "Read the details below to identify the file. Check that Amethyst uses the correct game folder, then install the game version or content required by the author and check again. Renaming another file will not fix it."),
    "Required game files": (
        "This modlist needs original game files that Amethyst cannot find.",
        "Check the game folder selected in Amethyst. Install the required game and DLC, launch the unmodified game once, then check requirements again. Also follow the author's requirements for the store, language and game version."),
    "Required supporting files": (
        "This modlist needs an extra file from the game or its tools. This does not necessarily mean that your game version is wrong.",
        "Read the affected-file list and follow the author's setup instructions. If Scripts.zip is listed, install the matching Creation Kit. If the author does not explain how to obtain the file, try the latest modlist or report the file to the modlist author."),
    "Ignored supporting files": (
        "The modlist contains optional logs, store information or editor files that are not needed for the installation.",
        "No action is needed. Amethyst will leave out the files listed below and continue."),
    "Game version": (
        "Your game files do not match the version used to create this modlist.",
        "Install the game version, store edition and language required by the author, then check again. If that version is no longer available and the author provides no downgrade instructions, use a newer modlist or contact its author."),
    "Creation Kit files": (
        "This modlist needs files from the Creation Kit, which is installed separately from the game.",
        "Install the required Creation Kit through Steam. Select a Proton version for it if needed, launch it once, close it, then check requirements again. Follow the author's version instructions if files still do not match."),
    "Creation content": (
        "A required Creation or Creation Club file is missing or is a different version. Steam and the in-game Creations menu can install different files for the same content.",
        "Install or update the affected content using the source required by the modlist author.\n\n"
        "For lowercase ccbgssse037-curios files:\n"
        "1. Delete both Rare Curios files from the game's Data folder.\n"
        "2. Launch Skyrim through Steam and open Creations.\n"
        "3. Find Rare Curios and download it.\n"
        "4. Wait for the download to finish, then exit the game.\n"
        "5. Do not verify the game through Steam afterward.\n"
        "6. Check requirements again.\n\n"
        "Renaming the old files will not fix them."),
    "Required DLC or game plugin": (
        "A selected profile needs a game, DLC or Creation Club plugin that is not installed.",
        "Install the plugin or DLC named below through the game or its store. Follow the author's instructions, check that Amethyst uses the correct game folder, then check requirements again."),
    "Manual download": (
        "Amethyst cannot download this file automatically. This is normal for free Nexus accounts and some download sites.",
        "Start the installation and follow the browser prompt. If Amethyst does not find the download, choose Select File and select it yourself. Make sure you download the exact version shown below."),
    "Runtime adjustment": (
        "This modlist needs an extra setting or dependency to work on Linux.",
        "Review Linux adjustments in setup and select the option described below, then check requirements again. Amethyst installs required dependencies during installation; Proton tools can also install or repair them."),
    "Game runtime": (
        "This Windows game does not yet have a working Proton configuration in Amethyst.",
        "Configure Proton for the game in Amethyst. Launch the original game once, close it, then check requirements again."),
    "Windows path mapping": (
        "A Windows tool in this modlist cannot access one of the selected Linux folders.",
        "Check the game's Proton prefix and Wine drive mappings. Map the folder named below to a Windows drive, then check requirements again."),
    "Texture conversion": (
        "This modlist needs to convert DDS textures, but the selected converter is not working.",
        "Open Texture conversion, choose a converter and select Install / repair. Texconv also needs a version selected under Texture tool Proton; Native Compressonator does not. Check requirements again after the repair."),
    "BSA setup": (
        "This modlist needs Amethyst to rebuild original Fallout: New Vegas archives, but part of that setup is not ready.",
        "Follow the specific error below. Restore the required English game files, install FFmpeg if it is missing, or resolve the named output-folder conflict. Then check requirements again; Amethyst will rebuild the archives during installation."),
    "Native MPI installer": (
        "The tool Amethyst uses to build files from an MPI package is missing or cannot run.",
        "Open Setup tools and install or repair the Native MPI installer. Then return to this modlist and check requirements again."),
    "Stock game setup": (
        "This modlist needs its own copy of the original game files, but Amethyst could not prepare it.",
        "Check that Amethyst uses the correct original game folder and can read it. Fix the file or permission error shown below, then check requirements again. Do not remove the original game files."),
    "Stock game patch": (
        "The separate Fallout: New Vegas game copy needs the 4 GB patch.",
        "Enable automatic New Vegas 4 GB patching in the game's Amethyst settings, then check requirements again. Amethyst will apply it during deployment."),
    "Disk space": (
        "There is not enough free space for the download, installation, temporary files and backups.",
        "Free space on the drive named below, or move the download or profile folder to a larger supported drive. Manage caches can remove unused downloads, but keep files used by active installations. Then check requirements again."),
    "Filesystem path limits": (
        "A required file path is too long for the selected drive.",
        "Choose a shorter installation folder. If the detail says a single filename is too long, choose a drive with a filesystem that supports longer names. Do not rename the modlist's files. Then check requirements again."),
    "Path overlap": (
        "The game, download and installation folders overlap, which could mix or overwrite their files.",
        "Choose separate download and installation folders outside the original game folder. The download and installation folders must not be inside one another. Then check requirements again."),
    "Permissions": (
        "Amethyst cannot read or write the selected folder.",
        "Choose a folder you can write to, or fix its permissions. If you use Flatpak, allow Amethyst to access the folder. Also check that the drive is mounted as writable, then check requirements again."),
    "Filesystem capabilities": (
        "The selected drive cannot create the file links Amethyst needs for this installation.",
        "Move the profile to a Linux filesystem that supports hard links and symbolic links, such as ext4 or Btrfs. Check folder permissions and application access, then check requirements again."),
    "Filesystem": (
        "The selected drive has a filesystem limitation or access problem.",
        "Read the details below. If hard links are unavailable, you can continue, but the installation will use more space. Fix any drive-access or permission error. For a case-sensitivity warning, move the installation to a case-sensitive Linux filesystem."),
    "Installation directory": (
        "The selected installation folder is not safe or supported for this operation.",
        "For a new installation, use the default folder inside the game's profile location. To Resume, Repair or Update an existing installation, open it from Installed lists. Do not delete existing files while correcting the folder."),
    "Installation": (
        "Amethyst cannot find an existing modlist installation in the selected folder.",
        "Open the installation from Installed lists. To create a new installation instead, choose Install and select a new empty folder."),
    "Operation": (
        "Amethyst could not recognise the requested installation action.",
        "Reopen setup and choose Install, Resume, Repair or Update. If the error appears again, send the details below to Amethyst support."),
    "Profiles": (
        "No valid modlist profile is selected.",
        "Select at least one available profile in setup, then check requirements again."),
    "Game layout": (
        "Amethyst does not know how to install this modlist's folder layout for the selected game.",
        "Confirm that the correct game and edition are selected, then update Amethyst and check again. If it still fails, send the modlist name, version and details below to Amethyst support."),
    "Game-root deployment": (
        "This modlist puts files beside the game's executable, but Amethyst does not support that for the selected game.",
        "Confirm that the correct game is selected and update Amethyst. If it still fails, send the modlist name and details below to Amethyst support."),
    "Game-root conflict": (
        "Two modlist files would be placed at the same location beside the game executable.",
        "If setup offers a Root file variant, choose the one for your store, such as Steam/GOG or Epic. Check requirements again. If the conflict remains, send the listed paths and modlist version to Amethyst support and the modlist author."),
    "Game-root mod": (
        "A mod that places files beside the game executable conflicts with an existing mod, name or metadata file.",
        "Follow the details below to resolve the named conflict without deleting files you want to keep. If both sides come from the modlist, report the conflict and modlist version to its author."),
    "Store-specific root files": (
        "This modlist includes different files for different game stores.",
        "In setup, choose the Root file variant that matches your store, such as Steam/GOG or Epic. Then check requirements again."),
    "Configuration file": (
        "A configuration file in the modlist is invalid or too large to read safely.",
        "Download the latest version of the modlist and check again. If the same file still fails, send its path and the modlist version to Amethyst support and the modlist author."),
    "Affected profiles": (
        "The profiles listed below share mod files, so this operation will affect all of them.",
        "Review the affected profiles before continuing. During an Update or Repair, choose Keep mine for changes you want to preserve."),
    "Authored profile changes": (
        "This modlist update adds or removes profile choices.",
        "Review the added and removed profiles below. Select the profiles you want in setup before starting the update."),
    "Reusable outputs": (
        "Amethyst could not confirm whether some existing installation files can be reused.",
        "Check the file or database error below. Keep your existing files and backups. Resume or Repair will verify them again and rebuild only what is needed."),
    "Tool output configuration": (
        "A bundled modding tool needs you to choose where its generated files will be saved.",
        "After installation, open each tool named below and select the listed output mod before running it. Follow Author instructions for any other tool settings."),
    "Author instructions": (
        "The modlist author requires a step that Amethyst cannot complete or verify for you.",
        "Open Author instructions or Community at the top of setup and follow the step shown below. Complete any remaining instructions before launching the modlist."),
    "Linux compatibility": (
        "Amethyst can install the files, but it cannot guarantee that every Windows mod or tool in this modlist works on Linux. This is a reminder, not a detected failure.",
        "Review Linux adjustments and any Community instructions. You can continue installing; there is no specific error to fix here."),
    "Display settings": (
        "Amethyst cannot apply the selected screen resolution to this modlist.",
        "Choose another supported resolution in setup, or keep the author's display settings. Then check requirements again."),
}

_SETUP_TASKS = {"Tale of Two Wastelands", "YUPTTW update", "Ultimate Edition ESM Fixes Remastered",
                "Unofficial Fallout 3 ESM Patcher", "Fallout 3 BSA Decompressor"}


def make_check(status, name, detail, items=()):
    detail = str(detail)
    if status == "pass":
        explanation = "This requirement passed. Any automatic setup described below will run during installation."
        resolution = "No action is needed now. Follow any next step shown in Details after installation."
    elif name == "Archive extraction":
        explanation = "Amethyst cannot open a required mod archive."
        resolution = ("Download an unencrypted version or ask the author for a supported archive. Amethyst cannot open password-protected archives automatically."
                      if "Password-protected" in detail else "Install 7-Zip and make sure Amethyst can access it, then check requirements again. If 7-Zip is already installed, check Flatpak access and the details below.")
    elif name in _SETUP_TASKS:
        explanation = "The selected profile needs extra files that must be generated by another installer or tool."
        resolution = "In Additional setup, select the MPI package requested by the author or a complete existing output mod. Follow the game and tool requirements below, confirm the version matches the author's instructions, then check requirements again."
    else:
        explanation, resolution = _HELP.get(name, (
            "This requirement needs your attention before Amethyst can continue.",
            "Follow the instructions in the details below, then check requirements again. If there is no clear action, send the full details and modlist version to Amethyst support."))
    return Check(status, name, detail, explanation, resolution, tuple(items))
