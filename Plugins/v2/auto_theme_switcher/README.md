# Auto Theme Switcher

Requires Amethyst 2.6.0 or newer, providing `service: true` Qt plugins and the
`appearance.*` APIs. Copy this folder into the directory opened by
**Wizards → Plugins → Open Plugins Folder…**, then restart Amethyst.

Open **Wizards → Plugins → Auto Theme Switcher**. Choose
a fallback theme and a theme for each added game, then Save. The current game's
selection applies immediately after the modal closes. Subsequent game switches
and startup apply that game's theme automatically. Built-in and custom themes
are supported. Unassigned games and missing themes use the fallback; if the
fallback disappears, Amethyst's default theme is used.

Disable automatic switching in the modal to leave theme selection manual.
Cancel discards unsaved choices. Profile switches do not reapply the theme.
Manual theme choices remain until the next game switch or saved plugin change.
Before the first Save, the plugin leaves the current theme alone.

Settings are saved in `data.json` beside this plugin's `plugin.json`, so
copying the plugin folder also copies its configuration. Older settings from
`plugin_data/amethyst.auto_theme_switcher.json` are migrated automatically; the
old file remains as a backup. Mappings for games temporarily removed from the
manager are retained. Removing the plugin stops automation and leaves the last
selected theme in place.
