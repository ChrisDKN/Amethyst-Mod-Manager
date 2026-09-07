from __future__ import annotations

from .models import Check

_HELP = {
    "Archive reconstruction": (
        "The package describes how to rebuild a BSA or BA2 archive. Amethyst cannot safely build it with the reported member information or archive format.",
        "Check for an Amethyst update and the author's current modlist package. If this remains blocked, report the modlist name, version and full details below to Amethyst support. This may need an installer fix or a corrected package; downloading the same source archives again does not change the reconstruction instructions."),
    "Package integrity": (
        "The .wabbajack package is unreadable, damaged, or has changed since it was opened.",
        "Use Reload to fetch the package again, or download a fresh .wabbajack file from the author and open it with Choose file. Keep your existing mod downloads."),
    "Package identity": (
        "Resume and Repair use the package saved for this installation. The selected package describes a different authored version.",
        "Open the saved installation to Resume or Repair it, or choose Update to apply the new package."),
    "Deployment": (
        "The game currently has deployed mods. Installation and updates need exclusive access to its managed files.",
        "Close the game and any running mod tools, use Restore in Amethyst, then check requirements again."),
    "File catalog": (
        "Amethyst's native Filegraph component is unavailable. It is needed to publish and track the installed mods.",
        "Update or reinstall Amethyst from a complete build for your system. For a source checkout, build and install the native Filegraph component, then restart Amethyst."),
    "Game": (
        "The package targets a different game or edition from the game currently selected in Amethyst.",
        "Select the required game using the main game dropdown, then reopen this modlist and check requirements."),
    "Additional game": (
        "This modlist uses files from another game as well as the selected game.",
        "Install and configure the additional game named below in Amethyst, including the author's required DLC and version, then recheck."),
    "Required game file": (
        "A source file is missing or differs from the exact size and hash required by the package. Store, language, game updates and Creation Club versions can produce different files with the same name.",
        "Follow the specific version or content guidance below. Correct the original game location or obtain the author's required version, then check requirements again. Renaming a different file will not make its contents match."),
    "Required game files": (
        "The package reconstructs part of the installation from exact files in the original game, but those source files are absent.",
        "Check the original game location in Amethyst, install the game and required DLC, and launch the unmodified game once. Follow the linked author requirements for its store, language and version, then recheck."),
    "Required supporting files": (
        "The package captured generated or store-maintained files from the author's game directory. A missing or different supporting file does not show that the game executable is the wrong version.",
        "Launch the original game once and recheck. If the exact files remain unavailable and the author does not document them, use an updated package or report the listed files to the modlist author; the package may need to be rebuilt without volatile files."),
    "Ignored supporting files": (
        "The package captured direct copies of known volatile log or Steam metadata files. They are not game runtime content and can legitimately be absent or differ between systems.",
        "No action is required. Amethyst will omit these direct copies from the managed installation and continue. The affected paths remain listed for transparency."),
    "Game version": (
        "Files exist in the original game location, but their exact sizes or hashes differ from the source snapshot used to build this package.",
        "Use the game build, store and language named by the author. A newer file with the same name cannot produce the declared output. If the required build is no longer available and the author provides no supported downgrade, use an updated modlist package or contact its author."),
    "Creation Kit files": (
        "This package uses files installed by the game's Creation Kit as reconstruction sources. The Creation Kit is a separate Steam application and its files must match the version used by the author.",
        "Install the matching Creation Kit through Steam, select a Proton compatibility tool for it when required, and launch it once. Close it after it opens, then check requirements again. Follow the author's version requirement if the files still differ."),
    "Creation content": (
        "Required Creations or Creation Club files are missing or differ from the exact variants used by the package author. Steam-delivered and in-game-downloaded files can share a name while having different contents.",
        "Follow the linked author requirements and obtain the exact listed content through the required source. For Skyrim Rare Curios, use the author's specified Steam or in-game variant. Renaming or copying a different version will not satisfy the hash check."),
    "Required DLC or game plugin": (
        "An enabled profile needs a game or DLC plugin that is absent from both the original game and the package's planned files.",
        "Install the required DLC or Creation Club content through your game or store, following Author instructions, and check the original game location before rechecking."),
    "Required external mod": (
        "The profile enables a mod that the package does not supply and that is not present in this installation.",
        "Follow Author instructions to obtain or generate the required mod. Use its additional setup option if one is offered, or deselect the profile that requires it, then recheck."),
    "Manual download": (
        "This archive needs a browser download or file selection. This is normal for free Nexus accounts and hosts without automatic download support.",
        "Start installation and follow the browser prompts. Use Select File if the download is not detected. Choose the exact file version requested; Amethyst checks its size and hash while other automatic downloads continue."),
    "Runtime adjustment": (
        "A bundled mod needs a runtime dependency or a Linux compatibility adjustment. The entry below identifies the proposed change.",
        "Review and select the matching option under Linux adjustments in setup, then check requirements again. Required dependencies are installed automatically during installation; Proton tools can also install or repair them."),
    "Game runtime": (
        "The selected Windows game needs a configured Wine/Proton prefix, which holds its Windows settings and runtime dependencies.",
        "Configure Proton and the prefix for the selected game in Amethyst. Launch the original game once to initialise it, close it, then check requirements again."),
    "Windows path mapping": (
        "The selected prefix cannot translate one of the permanent Linux installation paths into a Windows drive path for bundled tools.",
        "Check the selected game's prefix and its Wine drive mappings. Make the listed location accessible through a mapped drive, then recheck."),
    "Texture conversion": (
        "The package needs DDS textures resized or converted. The isolated texture tool did not pass its capability check with the selected runtime.",
        "Select an installed build under Texture tool Proton, then use Prepare / repair texture tool. If GPU conversion fails, choose CPU only under Texture conversion and recheck. Include the details below if neither mode works."),
    "BSA setup": (
        "This list requires the original New Vegas archives to be rebuilt, including audio conversion. Amethyst checks the supported source files, required tools and existing generated output first.",
        "Follow the specific failure below: restore the required English vanilla sources, install FFmpeg if missing, or resolve the named output-folder conflict. Then recheck; Amethyst runs the supported BSA setup automatically during installation."),
    "Stock game setup": (
        "This list needs a separate copy of the original game inside its managed installation, but preparation of that copy failed.",
        "Check that the original game path is correct and readable, then address the file or permission error below and recheck. Keep the original game available for future verification."),
    "Stock game patch": (
        "The reconstructed New Vegas game copy needs the 4GB executable patch.",
        "Enable automatic New Vegas 4GB patching in the game's Amethyst settings, then recheck. Deployment applies the patch to the managed game copy."),
    "Disk space": (
        "Available space is below the estimate for downloads, installation, temporary extraction and update backups. Locations sharing a filesystem are counted together, with a reserve.",
        "Free space on the filesystem named below, or choose supported download and profile locations on a larger filesystem. Manage caches can remove unused downloads; keep files needed by active jobs. Then recheck."),
    "Filesystem path limits": (
        "A required filename or full path is longer than the destination filesystem supports.",
        "For a full-path limit, choose a shorter installation path. For a filename-component limit, use a filesystem that supports the named length. Keep the author's filenames intact and recheck."),
    "Path overlap": (
        "The download, managed installation or original game locations contain one another. This can mix source files with installation output.",
        "Choose separate download and managed installation directories outside the original game. Neither location should contain the other. Then recheck."),
    "Permissions": (
        "Amethyst cannot write to or access the selected location with your current user or application permissions.",
        "Choose a writable location or correct its permissions and, for Flatpak, its filesystem access. Check that the drive is mounted with write access, then recheck."),
    "Filesystem capabilities": (
        "The managed installation requires working hard links and symbolic links. The selected filesystem or its permissions failed that check.",
        "Use a Linux filesystem that supports both link types, such as ext4 or Btrfs, for the game's managed profile location. Check write permissions and application access, then recheck."),
    "Filesystem": (
        "The selected filesystem has an access problem or behaves differently from the case-sensitive paths expected by the installer.",
        "Read the detail below. Check drive access and permissions for an error; for a case-insensitivity warning, prefer a case-sensitive Linux filesystem for the managed installation."),
    "Installation directory": (
        "The chosen directory does not meet the managed installation's location, ownership or symbolic-link requirements.",
        "Use the default managed location under this game's profile root for a new installation. Open an existing installation through Installed lists to Resume, Repair or Update it. Preserve any existing files when correcting the location."),
    "Installation": (
        "Amethyst could not find a managed installation at the selected location.",
        "Choose the installation from Installed lists, or use Install with a new empty managed location."),
    "Operation": (
        "The requested operation is not one of the supported installation modes.",
        "Reopen setup and choose Install, Resume, Repair or Update. If the error persists, report it to Amethyst support."),
    "Profiles": (
        "The selected authored profiles are missing or no longer exist in this package.",
        "Select at least one of the profiles shown in setup, then check requirements again."),
    "Game layout": (
        "Amethyst cannot map this package's authored directory layout to the selected game's profiles and deployment paths.",
        "Confirm the game and edition, then check for an Amethyst update. If the layout remains unsupported, report the package name, version and details below to Amethyst support."),
    "Game-root deployment": (
        "This list contains files that must be deployed beside the game's executable, but the selected game handler does not support that layout.",
        "Confirm the selected game and check for an Amethyst update. This needs game-handler support before installation can complete; report the package and details to Amethyst support."),
    "Game-root conflict": (
        "Two authored files would be deployed to the same game-root location. Amethyst cannot choose which one the author intended.",
        "Check the Root file variant in setup if the list offers store-specific alternatives. If both files still target the same location, report the listed paths and package version to Amethyst support and the modlist author."),
    "Game-root mod": (
        "The automatic root-file mod would collide with an existing mod, a reserved name or its metadata.",
        "Follow the detail below to resolve the named unowned mod conflict while preserving your files. If the conflict is inside the authored package, report it with the package version; it may need a corrected package."),
    "Store-specific root files": (
        "This package supplies different game-root files for different stores. Only the variant for your installation should be deployed.",
        "Choose the matching Root file variant in setup, such as Steam/GOG or Epic, then recheck."),
    "Configuration file": (
        "A packaged configuration file is too large for the supported limit or cannot be read as the expected UTF-8 text.",
        "Open the author's current package. If the same file remains blocked, report its path and the package version to Amethyst support and the author; the configuration or format support needs correction."),
    "Affected profiles": (
        "These profiles share the installation's mod files, so changes to shared files affect all of them.",
        "Review the listed profiles before continuing. Resolve any update or repair conflicts when prompted; choose Keep mine for edits you want to retain."),
    "Authored profile changes": (
        "The new package changes the available profile selection. Removed authored profiles are retained for review.",
        "Review the added and removed profiles and adjust the selected profiles in setup before starting the update."),
    "Reusable outputs": (
        "Some existing output could not be fully verified during the requirements check. Installation must validate it before deciding what can be reused.",
        "Check the file or database access problem below. Keep existing files and backups; Resume or Repair will verify available content and rebuild required output as needed."),
    "Tool output configuration": (
        "The author assigned folders for files generated by tools. Some tools need that destination selected manually.",
        "After installation, select the named output mod in each affected tool before running it. Follow Author instructions for the tool's other settings."),
    "Author instructions": (
        "This is an author requirement or a setup step that Amethyst cannot fully automate or verify.",
        "Open Author instructions or Community at the top of setup and follow the requirement below. Keep track of any steps that must be completed after installation and before launching."),
    "Linux compatibility": (
        "File reconstruction can be verified without proving that every bundled Windows mod or tool works on Linux. This is a general compatibility reminder, not a detected failure in a specific mod.",
        "Read Author instructions and Community for Linux guidance and review the offered Linux adjustments. There is no single repair for this reminder; it does not block installation."),
    "Display settings": (
        "The selected resolution is invalid or the package has no supported configuration files for applying it.",
        "Choose a supported resolution in setup or keep the author's display settings, then recheck."),
}

_SETUP_TASKS = {"Tale of Two Wastelands", "YUPTTW update", "Ultimate Edition ESM Fixes Remastered",
                "Unofficial Fallout 3 ESM Patcher", "Fallout 3 BSA Decompressor"}


def make_check(status, name, detail, items=()):
    detail = str(detail)
    if status == "pass":
        explanation = "This check is ready to proceed. Any planned setup described below will run during installation."
        resolution = "No action is needed for this check."
    elif name == "Archive extraction":
        explanation = "A required source archive cannot be extracted with the available tools or supported archive settings."
        resolution = ("Obtain an unencrypted source or a supported package from the author. Amethyst cannot automatically extract password-protected archives."
                      if "Password-protected" in detail else "Install 7-Zip and make it available to Amethyst, then recheck. If it is already installed, check application or Flatpak access and the details below.")
    elif name in _SETUP_TASKS:
        explanation = "The selected profile needs additional generated content that is not supplied as ordinary mod files in this package."
        resolution = "Select the author's required MPI package or a complete existing output mod in additional setup. Follow the source-game and tool requirements below, then recheck. Confirm the external content's version matches the author instructions."
    else:
        explanation, resolution = _HELP.get(name, (
            "The requirements check reported a condition that needs your attention before continuing.",
            "Follow the specific requirement below and check requirements again after making changes. If the message has no applicable action, report the full details and modlist version to Amethyst support."))
    return Check(status, name, detail, explanation, resolution, tuple(items))
