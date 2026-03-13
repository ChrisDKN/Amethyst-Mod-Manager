
<p align="center">
    <img width="250" src="src/icons/Logo.png" alt="Logo">
</p>
<h1 align="center">Amethyst Mod Manager</h1>

<h3 align="center">A mod manager for Linux.</h3>

<p align="center">
    <img width="800" src="src/icons/ui.png" alt="ui">
</p>

## Key Features

- **Mod Organiser like interface** - Designed to look and behave like Mod Organiser
- **Collections** - Install Nexus Mods collections straight into the manager
- **Linux Native** — Designed for Linux
- **Multi-game support** — Support for many games
- **FOMOD support** — Full Fomod support with last selections saved.
- **LOOT support** — Plugins for games that use LOOT can be sorted using LOOT.
- **Nexus API Support** — Integration with features provided by the Nexus Mods Api
- **Root Folder builder** — Files placed in the managers root folder separator are deployed to the games root folder and cleaned up on restore.

## Install

Run the following command in a terminal. It will appear in your applications menu under Games and Utilities.
**The Application may ask to set a password, This is for the OS keyring to store your nexus API key as we do not store it in a plain text file. Set the password to anything you want**

```bash
curl -sSL https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh | bash
```

## Games Supported

- Skyrim (Normal, SE and VR)
- Fallout 3 (Normal and Goty)
- Fallout 4 (Normal and VR)
- Fallout New Vegas
- Starfield
- Oblivion
- Oblivion Remastered
- Baldur's Gate 3
- Witcher 3
- Cyberpunk 2077
- Mewgenics
- Stardew Valley
- Kingdom Come Deliverance (1 and 2)
- Hogwarts Legacy
- Marvel Rivals
- Subnautica
- Subnautica Below Zero
- Resident Evil Requiem
- The Sims 4
- Tcg Card Shop Simulator
- Valheim
- Lethal Company
- Mount & Blade II: Bannerlord
- The manager now has the ability to define custom games. See the Wiki for the guide

## Usage

1. Add a game with the **+** icon in the top left.
2. It should auto-detect your install path and Proton prefix, but you can change these if needed.
3. Change the staging directory if you wish — this is where your mods are stored.
4. Use the **Install Mod** button to install a new mod.  
   Optionally, you can install from the Downloads tab if the mod is in your downloads folder.
5. Sort your mods in the mod list panel. You can add separators to group them.
6. If using a LOOT-supported game, you can sort and move plugins in the Plugins tab.
7. Click **Deploy** to move the mods to the game folder, or **Restore** to undo this.
8. Run the game via your normal method, Steam/Heroic/Lutris. You can also run the game in the top right with the run exe button.

You can also add multiple profiles with different configurations — simply create/swap to that profile and deploy it.

## Collections

The manager has the ability to add Nexus collections straight into the manager. Here's how it works:
- This feature only works for nexus premium users, There's no mechanism currently to manually download the mods.
- The collections page will show the top collections for the selected game, A url can be entered to a specific collection instead.
- When installing a collection they are downloaded in size order largest first
- They are then installed, also in size order, smallest first. The application may "freeze" while extracting large zip files. This is normal
- Some mods come with fomod settings, meaning the fomod menu is skipped for some mods. Some others will still pop up and need manual input.
- The authors load order is applied when the collection completes
- Collections are installed as separate profiles and can be switched between, letting you easily swap modlists.
- There are some limitations, Not all collections will work fully due to missing mods or our game handlers coming across a mod that has been shipped/packaged in an unusual fashion. Some may require some manual intervention to get to work.


## Supporting Applications

The manager supports many supporting applications used to mod games. Place the applications in the games applications folder (**In the staging folder**) and they will be auto detected. The arguments/config used to run them will be auto-generated to make setup easier.

| Status | Application | Notes |
|--------|-------------|-------|
| Working | **Pandora Behaviour Engine** | `--tesv:` and `--output:` args applied at runtime|
| Working | **SSEEdit** | `-d` and `-o` args applied at runtime|
| Working | **pgpatcher** | Requires `d3dcompiler_47` and `.net8 desktop runtime` installed to the game prefix via Protontricks. Config auto generated to include Data directory and output folder |
| Working | **DynDOLOD** | `-d` and `-o` args applied at runtime|
| Working | **TexGen** | `-d` and `-o` args applied at runtime|
| Working | **xLodGen** | `-d` and `-o` args. Game argument appended at runtime |
| Working | **Bethini Pie** | Just works |
| Experimental | **Vramr** | Experimental python wrapper See below for instructions|
| Experimental | **Bendr** | Experimental python wrapper See below for instructions|
| Experimental | **ParallaxR** | Experimental python wrapper See below for instructions|
| Working | **Wrye Bash** | `-o` Auto generated for selected game at runtime |
| Working | **Synthesis** | Requires .net10 sdk and .net5 runtime installed into the prefix (Use the proton tools window to do this) |
| Working | **Bodyslide and Outfits Studio** | Add as a mod > Deploy > refresh the exe list > Run the exe and it should work |
| Working | **Witcher 3 Script merger** | Game path added to config automatically |
| Working | **Witcher 3 Script merger Fresh and Automated Edition** | Game path added to config automatically. Requires .net 8 Runtime installed into the prefix |
| Maybe | **Npc plugin chooser** | Game paths are applied to config at runtime, Can't seem to generate npc portraits and has some problems under proton |

The other xedit applications for the other games also work as well as the quickautoclean applications.

## Running Windows Apps (e.g. SSEEdit)

1. Add the folder containing the exe to Applications folder in the game's staging path.
2. Hit **Refresh** on the top right.
3. You can configure the exe to change the arguments or the output mod/folder.
4. Make sure your game is deployed before running so the application gets the right files.
5. Hit **Run exe** — it will run using the Proton version and prefix the game uses.

## VRAMr + BENDr + ParallaxR

The following applications were tested and run in Ge-proton 10, It is recommended to have ge-proton 10 installed for these to work. These are very experimental and may not work as expected.

VRAMr (v15.0723 and v16+) works by using an experimental python wrapper. Run VRAMr.bat (v15) or VRAMr.exe (v16+) from the dropdown. The optimisation step uses Compressonator for native Linux support, which is faster than running texconv through Wine/Proton.

BENDr (Version 2.21) Uses a similar process

ParallaxR (Version 2.0) Uses a similar process, Requires a patched ucrtbase.dll (This one can be quite slow, Not much I can do about that)

> Why is ucrtbase.dll included? Wine is missing implementations for a few C99 math functions (crealf, cimagf, etc.) that HeightMap.exe requires. This is a copy of Wine's own ucrtbase.dll with those ~4 stub functions patched to work correctly.

**Any issues with this should be reported here and not to the VRAMr devs, This is experimental and not an offical Linux release**

- Place the Vramr/Bendr/ParallaxR folder in Skyrim Special Edition/Applications/ in the games staging folder
- In Amethyst mod manager, run VRAMr.bat or VRAMr.exe (depending on version) / BENDr.bat / ParallaxR.bat **Make sure your modlist is deployed first**
- It will run the wrapper script, Progress will be added to the log, A temporary wine prefix will be created in the home directory
- Output will be placed as a mod in a VRAMr/BENDr/ParallaxR folder. Refresh the modlist and enable it. Delete it if you want to do another run but remember to deploy before you do so.

## Planned Features

- Adding/Installing collections directly into the manager as a new profile, This is currently working, See the wiki for screenshots of what this will look like. This Will be added once the application is registered with Nexus and some more polish is added.

## Supporting the project

- This is where I'd put a ko-fi link or something. Give your money to a more worthwhile cause. Your feedback is enough
