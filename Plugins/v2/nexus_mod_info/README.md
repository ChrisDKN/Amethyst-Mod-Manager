# Nexus mod info

An external Amethyst 2.6.0+ Qt plugin. Shows the selected mod's Nexus image,
short description, total downloads, endorsements, Nexus version and installed
version in a resizable panel directly beneath the modlist. The optional second
modlist gets its own panel, including when detached.

The image occupies the left half of the panel. Mod stats appear above the short
description on the right. The image scales with the panel and keeps its original
proportions.

Copy `amethyst.nexus_mod_info` into Amethyst's `Plugins` directory and restart
Amethyst. Connect Nexus Mods in Amethyst Settings, then select a mod with a Nexus
ID and game domain in its `meta.ini`. Separators and unlinked mods show an empty
state. Multiple selections follow the current selected mod.

The panel is purely informative and updates automatically when a mod is selected.
Drag the divider above the panel to resize it, or drag it all the way down to
hide it completely. Drag the divider back up to show it again. You can also
disable **Show Nexus info beneath the modlist** in the plugin's settings. The
panel distinguishes the current Nexus version from the installed version.

Click the mod image to open it in Amethyst's image viewer tab. The viewer loads
the full-size image in the background and supports zoom, pan and double-click
to fit. Clicking the same mod's image again focuses its existing tab. Image tabs
also work when the second modlist is detached, and close when the plugin unloads.

Nexus requests run on a single background worker after a short selection delay.
Successful results are cached in memory for ten minutes (at most 64 mods).
Obsolete queued requests and late results are discarded. Failed requests retain
saved local descriptions and versions; unavailable counts display a dash.
Images load through Amethyst's existing thumbnail loader. The description is the
plain-text Nexus summary, the same short description used by the modlist hover
tooltip. Embedded description resources are not loaded.

The plugin uses Amethyst's saved Nexus OAuth session and existing Nexus client;
it does not save credentials or modify mod metadata. Downloads and endorsements
come from the client's GraphQL lookup, along with the short description. The
plugin does not request or display the full description page.

Amethyst currently has no public widget location inside the modlist column, so
the service wraps each existing `ModListView` in a vertical splitter. It uses the
public selection events to update its content and restores the original layout
when unloaded. This attachment depends on Amethyst's Qt layout; future manager
layout changes may require a plugin update. No manager source changes are needed.
