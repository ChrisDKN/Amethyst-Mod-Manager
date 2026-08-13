
<p align="center">
    <img width="200" src="src/icons/Logo.png" alt="Logo">
</p>
<h1 align="center">Amethyst Mod Manager</h1>

<h3 align="center">A mod manager for Linux.</h3>
<h5 align="center">
  <a href="https://www.nexusmods.com/site/mods/1714">Nexus</a> |
  <a href="https://github.com/ChrisDKN/Amethyst-Mod-Manager/wiki">Wiki</a> |
  <a href="https://ko-fi.com/chrisdkn">Ko-Fi</a>
</h4>

<p align="center">
    <img width="800" src="src/icons/ui.png" alt="ui">
</p>

## Key Features

- **Mo2 style interface** - If it's not broke, don't fix it
- **Install Nexus Collections** - Handles fast mod installs, Collection load orders, applies fomod options and mod diff patches automatically.
- **In app Nexus Browser** - View and install mods straight into the manager, from the manager
- **In app Thunderstore Browser** - View and install mods for Thunderstore supported games
- **Loot support** - Libloot is built into the application and optimised for fast plugin sorting
- **Update checking** - Quickly check all mods for Nexus updates. Bg3 mods installed via mod.io can also be checked for updates
- **Multi game support** - Bethesda, RE Engine (including pak invalidation), Bg3, CP2077 and a lot more. Designed to make adding game support easy
- **Automated tool setup** - Run things like Pandora,pgpatcher,dyndolod with a few clicks
- **Root folder building** - Most mods that need to go to root do so automatically, no setup needed. Anything else can be toggled to go to root with a couple clicks
- **Smart game restore** - Amethyst uses hardlinks and symlinks but will restore the game to it's previous state while moving any runtime generated files back to staging
- **Multi platform detection** - Detects games installed by Steam, Heroic, Lutris and Faugus

---

## Install

**The Application may ask to set a password, This is for the OS keyring to store your nexus API key as we do not store it in a plain text file. Set the password to anything you want**

### Appimage
Run the following command in a terminal. It will appear in your applications menu under Games and Utilities.

```bash
curl -sSL https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh | bash
```

The application will notifiy when a new update goes live, Pressing the update button will rerun the script and update to any new version

### Flatpak

Run the following command

```bash
flatpak install --user https://chrisdkn.github.io/Amethyst-Mod-Manager/amethyst.flatpakref
```

The remote can also be added manually where you can then install from the app store/discover store

```bash
flatpak remote-add --user --if-not-exists modmanager-origin https://chrisdkn.github.io/Amethyst-Mod-Manager/amethyst.flatpakrepo
flatpak install --user modmanager-origin io.github.Amethyst.ModManager//stable
```

The repo carries two branches, `stable` and `beta`, so name the one you want.

Updates will be shown in your package manager

Manually swap to the beta branch

```bash
flatpak install --user --reinstall modmanager-origin io.github.Amethyst.ModManager//beta
```

(swap `--user` for `--system` if that's where you installed it)

Installing from a bundle skips the 32-bit compat extensions that running Windows tools (Proton/wine) requires - The app installs them automatically on first launch, or you can add them yourself:

```bash
flatpak install --user flathub org.freedesktop.Platform.Compat.i386//24.08 org.freedesktop.Platform.GL32.default//24.08
```

### AUR
<a href='https://aur.archlinux.org/packages/amethyst-mod-manager'>
	<img width='240' alt='Get on AUR' src='https://upload.wikimedia.org/wikipedia/commons/e/e8/Archlinux-logo-standard-version.png'/>
</a>

---

## Games Supported

<table>
<tr>
  <th>Game</th><th>Notes</th>
  <th>Game</th><th>Notes</th>
</tr>
<tr><td>7 Days to Die</td><td></td><td>Oblivion</td><td></td></tr>
<tr><td>Abiotic Factor</td><td></td><td>Oblivion Remastered</td><td></td></tr>
<tr><td>Baldur's Gate 3</td><td></td><td>Pacific Drive</td><td></td></tr>
<tr><td>Blade &amp; Sorcery</td><td></td><td>Palworld</td><td></td></tr>
<tr><td>Crash Bandicoot 4</td><td></td><td>Payday 2</td><td></td></tr>
<tr><td>Cyberpunk 2077</td><td></td><td>Planet Zoo</td><td></td></tr>
<tr><td>Daggerfall Unity</td><td>Native Linux build</td><td>Pragmata</td><td></td></tr>
<tr><td>Darktide</td><td></td><td>Ready Or Not</td><td></td></tr>
<tr><td>Devil May Cry 5</td><td></td><td>Red Dead Redemption 2</td><td></td></tr>
<tr><td>Dragon Age Origins</td><td></td><td>Resident Evil</td><td>2, 3, 4, 7, Village, Requiem</td></tr>
<tr><td>Dragons Dogma 2</td><td></td><td>Rimworld</td><td></td></tr>
<tr><td>Enderal</td><td>Normal and SE</td><td>RuneScape: Dragonwilds</td><td></td></tr>
<tr><td>Expedition 33</td><td></td><td>Schedule 1</td><td></td></tr>
<tr><td>Fallout 3</td><td>Normal and Goty</td><td>Skyrim</td><td>Normal, SE and VR</td></tr>
<tr><td>Fallout 4</td><td>Normal and VR</td><td>Slay The Spire 2</td><td></td></tr>
<tr><td>Fallout 76</td><td></td><td>Slime Rancher</td><td></td></tr>
<tr><td>Fallout New Vegas</td><td></td><td>Spyro Reignited Trilogy</td><td></td></tr>
<tr><td>FF VII Remake Intergrade</td><td></td><td>Stalker 2</td><td></td></tr>
<tr><td>FF XII: The Zodiac Age</td><td></td><td>Stardew Valley</td><td></td></tr>
<tr><td>Gothic 1 Remake</td><td></td><td>Starfield</td><td></td></tr>
<tr><td>Green Hell</td><td></td><td>StarRupture</td><td></td></tr>
<tr><td>Halo: Campaign Evolved</td><td></td><td>Stellar Blade</td><td></td></tr>
<tr><td>Hogwarts Legacy</td><td></td><td>Street Fighter 6</td><td></td></tr>
<tr><td>Hollow Knight: Silksong</td><td></td><td>Subnautica</td><td></td></tr>
<tr><td>Jagged Alliance 3</td><td></td><td>Subnautica 2</td><td></td></tr>
<tr><td>Kingdom Come Deliverance</td><td>1 and 2</td><td>Subnautica Below Zero</td><td></td></tr>
<tr><td>Kingdom Hearts 3</td><td></td><td>Supermarket Simulator</td><td></td></tr>
<tr><td>Lethal Company</td><td></td><td>SW: Jedi Fallen Order</td><td></td></tr>
<tr><td>Marvel Rivals</td><td></td><td>TCG Card Shop Simulator</td><td></td></tr>
<tr><td>MechWarrior 5: Mercenaries</td><td></td><td>The First Berserker: Khazan</td><td></td></tr>
<tr><td>Mewgenics</td><td></td><td>The Last Caretaker</td><td></td></tr>
<tr><td>Monster Hunter Rise</td><td></td><td>The Sims 4</td><td></td></tr>
<tr><td>Monster Hunter Wilds</td><td></td><td>Valheim</td><td></td></tr>
<tr><td>Monster Hunter World</td><td></td><td>Windrose</td><td></td></tr>
<tr><td>Morrowind</td><td>Normal and OpenMW</td><td>Witcher 3</td><td></td></tr>
<tr><td>Mount &amp; Blade II: Bannerlord</td><td></td><td>X4 Foundations</td><td></td></tr>
<tr><td>My Summer Car</td><td></td><td></td><td></td></tr>
<tr><td>No Mans Sky</td><td></td><td></td><td></td></tr>
</table>

---

## Wiki

See the [wiki](https://github.com/ChrisDKN/Amethyst-Mod-Manager/wiki) page for a detailed guide on how to the use the mod manager and its functions. 

The wiki is also built into the manager and can be viewed there.

For building from source see the section on the wiki [here](https://github.com/ChrisDKN/Amethyst-Mod-Manager/wiki#building-from-source) 

## Supporting the project

Your feedback is enough and is greatly appreciated as this benefits everyone but if you'd like donate you can on Ko-fi 

<a href='https://ko-fi.com/R6R51XJ80I' target='_blank'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi6.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>
