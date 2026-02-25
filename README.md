
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
- **Linux Native** — Designed for Linux
- **Multi-game support** — Support for many games
- **FOMOD support** — Full Fomod support with last selections saved.
- **LOOT support** — Plugins for games that use LOOT can be sorted using LOOT.
- **Nexus API Support** — Integration with features provided by the Nexus Mods Api

## Game Support and Status

### Working

- Skyrim Special Edition
- Skyrim
- Fallout 4
- Fallout 3
- Fallout 3 GOTY
- Fallout New Vegas
- Oblivion
- Starfield
- Skyrim VR
- Fallout 4 VR
- Baldur's Gate 3
- Cyberpunk 2077
- Witcher 3
- Kingdom Come: Deliverance II
- Subnautica
- Subnautica Below Zero
- Valheim
- TCG Card Shop Simulator
- Stardew Valley
- The Sims 4
- Lethal Company
- Mewgenics

### Game Support to Add

- Oblivion Remastered
- Morrowind
- Hogwarts Legacy
- And more

## Supporting Applications

The manager supports many supporting applications used to mod games. Place the applications in the games applications folder and they will be auto detected. The arguments/config used to run them will be auto-generated to make setup easier.

### Currently Supported and Working

- **Pandora Behaviour Engine** — Working with `--tesv:` and `--output:` args
- **SSEEdit** — Working with `-d` and `-o` args
- **pgpatcher** — Working (requires `d3dcompiler_47` installed to the game prefix via Protontricks)
- **DynDOLOD** — Working with `-d` and `-o` args
- **TexGen** — Working with `-d` and `-o` args
- **Bethini Pie** — Just works
- **Vramr** - Working using an experimental python wrapper using compressonator instead of using texconv through proton
- **Witcher 3 Script merger** - Working, Game path added to config automatically

### Not Yet Added or Tested

- Bodyslide and Outfits Studio
- Synthesis
- Wrye Bash — Should work / not yet added/tested

## Usage

1. Add a game with the **+** icon in the top left.
2. It should auto-detect your install path and Proton prefix, but you can change these if needed.
3. Change the staging directory if you wish — this is where your mods are stored.
4. Use the **Install Mod** button to install a new mod.  
   Optionally, you can install from the Downloads tab if the mod is in your downloads folder.
5. Sort your mods in the mod list panel. You can add separators to group them.
6. If using a LOOT-supported game, you can sort and move plugins in the Plugins tab.
7. Click **Deploy** to move the mods to the game folder, or **Restore** to undo this.

You can also add multiple profiles with different configurations — simply create/swap to that profile and deploy it.

## Running Windows Apps (e.g. SSEEdit)

1. Add the folder containing the exe to Applications folder in the game's staging path.
2. Hit **Refresh** on the top right.
3. You can configure the exe to change the arguments or the output mod/folder.
4. Make sure your game is deployed before running so the application gets the right files.
5. Hit **Run exe** — it will run using the Proton version and prefix the game uses.

## Backwards Compatibility with Mod Organiser 2

> **Not currently recommended**

You can move your mods, overwrite, `modlist.txt`, and `plugins.txt` from Mod Organiser. These should be recognised by Amethyst Mod Manager. This is not fully tested yet, so don't do this with large mod lists.

## Needs Testing

As this is an early alpha build, the following needs testing:

- Support on multiple Linux distros
- Verification that all added games work
- Baulders Gate 3 testing - The Mod manager can build modsettings.lsx but further testing is needed to know if it's working fully

## VRAMr

VRAMr (Version 15.0723) works by using an experimental python wrapper. The optimisation step is also changed to use compressonator which is faster with native Linux support vs running texconv through Wine/Proton

**Any issues with this should be reported here and not to the VRAMr devs, This is experimental and not an offical Linux release**

- Place the Vramr folder in Skyrim Special Edition/Applications/ in the games staging foler
- In Amethyst mod manager, run VRAMr.bat
- It will run the wrapper script, Progress will be added to the log
- Output will be placed as a mod in a VRAMr folder

Support for other applications (BENDr and ParallaxR) may be added in future

## Planned Features

- Ability to change theme/colours of the GUI
- Properly Cleanup / Move files created by mods 
