# Manager plugins

Installed plugins appear in the **Plugins** toolbar menu, to the left of
Notifications, and run on demand for the selected game and profile. The host provides
saved settings, a review screen, logs, backend job tracking, and a manager
refresh after changes. The manager stays unavailable for editing until the
plugin window closes; queued installs resume afterward.
Before starting a plugin, the host releases its cached Filegraph sessions so
the plugin's headless backend can open the library. Desktop conflict builds
remain blocked until the plugin closes. Existing immutable snapshots can
still be displayed; the next desktop build opens a fresh session.

Set `"show_in_menu": false` in `plugin.json` for a passive plugin that does
not need an entry in the Plugins dropdown. The default is `true`. This hides
its primary entry and its tab/panel entries in that menu. Services, widgets,
toolbar buttons, commands, and other integrations continue to work. The plugin
remains listed in the Plugin Store for settings, updates, and enablement.

## Install a plugin

Choose **Plugins → Browse plugins…** to open the plugin store. It
lists plugins from your enabled GitHub repositories, plus your installed plugins.
The built-in repository is `ChrisDKN/Amethyst-Mod-Manager`, branch `Resources`,
folder `Plugins/v2`. You can browse without selecting a game.
The store shows each plugin's author, description, available and installed
versions, and minimum supported Amethyst version. Search matches names,
authors, and descriptions.

When a plugin folder contains `README.md` (case-insensitive), the **README** tab
renders its Markdown beside **Details**. Catalogue entries use the README from
the selected repository version; local-only plugins use their installed copy.
The README loads in the background and is limited to 1 MiB.

**Download**, **Update**, **Remove**, **Enable**, and **Disable** queue changes for the next manager
launch. Use **Restart manager** to apply them, or close the store and restart
later. **Cancel pending change** discards an individual queued change. Queued
changes survive closing the store and manager; existing plugins keep their
current files and behaviour until restart. Updates are manual: the manager
does not check for or install store updates at startup. **Refresh** checks the
catalogue again; if GitHub is unavailable, the store reports the failure and
shows its cached catalogue when available. Installed plugins can still be
removed, enabled, or disabled offline. Disabling leaves the plugin folder and
all saved settings/data in place. After restart, disabled plugins contribute
no services, widgets, buttons, menus, scripts, launch hooks, icon overrides, or themes. They remain
visible in the browser for updates, removal, or re-enabling. Manager enablement
is separate from any setting named `enabled` provided by a plugin.

Each plugin ID can have one pending change. Cancel that change before queuing
another. Updates and repository replacements preserve its enabled/disabled
state. Removing a plugin clears its manager enablement state; a subsequent
installation starts enabled.

Updates preserve the latest `settings.json` and `data.json` at application
time, including changes saved after downloading the update. The previous
folder is retained as a backup for other custom files. Removal keeps the
folder outside plugin discovery by default; reinstalling restores the standard
settings and data. Selecting **Delete saved data** during removal deletes the
removed folder, this plugin's store backups, and its legacy host-managed
settings/data files. It does not delete arbitrary files a plugin has written
elsewhere. Backups, downloaded packages, queued operations, repository
definitions, and catalogue caches live under `plugin_store/` in Amethyst's
config directory.

Plugins installed manually without version metadata appear as **Installed
locally · version unknown**. **Replace with store version** requires
confirmation, preserves standard settings/data, and backs up the original
folder. Duplicate installed IDs must be resolved manually before the store
can change that plugin. Existing `Plugins/Headless/` installations are updated
in their actual location.

Queued changes are validated and applied before plugin discovery on startup.
If another Amethyst instance is running, application is deferred. Failed or
interrupted replacements restore the previous installation and leave the
change pending for retry or cancellation; startup reports the result.

### Custom repositories

Choose **Repositories…** in the browser to add, edit, remove, enable, or disable
repository sources. Enter an HTTPS GitHub repository URL such as
`https://github.com/Arcitec/Amethyst-Mod-Manager`, then specify the branch and
folder separately. A trailing `/` or `.git` on repository URLs is accepted;
GitHub `/tree/...` links should be split into repository, branch, and folder.
A blank branch follows the repository's default branch; a blank folder uses
its root. Only public GitHub repositories are supported.

The configured folder can contain a single plugin's `plugin.json`, or immediate
subfolders containing one plugin each. Discovery does not recursively search
the entire repository. Save changes and close the repository editor to refresh
the browser. The built-in source can be disabled and restored, but its URL,
branch, and folder cannot be edited or removed.

Sources have independent catalogue caches. An unavailable source reports its
failure and uses its cached catalogue when possible, while other sources remain
available. Removing or disabling a source keeps installed plugins and queued
downloads intact; it does not disable plugins installed from that source.

Plugins sharing an ID across repositories appear as separate source-labelled
entries, although only one copy of that ID can be installed. **Update** follows
the recorded installation source. **Replace from this repository…** explicitly
switches sources after confirmation, including when the selected version is
equal or older. Installations without recorded source metadata also require
confirmation when adopting a repository version. Replacement preserves standard
settings/data, retains the old folder as a backup, and preserves enablement.
Duplicate IDs within one configured source are reported and cannot be installed.

### Manual installation

Choose **Plugins → Open Plugins Folder…**. Create a subfolder containing
`plugin.json` and a Python entrypoint. The usual location is:

```text
~/.config/AmethystModManager/Plugins/my_plugin/
    plugin.json
    main.py
```

The path follows Amethyst's config directory, including `XDG_CONFIG_HOME` and
Flatpak's config location. Reopen the Plugins menu to discover changes; discovery
reads manifests without importing the entrypoint. Invalid manifests are reported
in the menu. Plugin IDs must be unique.

The previous `Plugins/Headless/<plugin>/` layout is still supported. To migrate,
move the plugin folder directly into `Plugins/`; saved settings remain intact.
If both locations contain the same plugin ID, the direct `Plugins/` copy takes
precedence and the duplicate is reported in the menu.

## Translations

Plugins follow the manager's language setting, including **System default**.
The resolved language code is also supplied as `context["language"]` to Qt
views, widgets, services, and stdio clients. Language changes follow the
manager's existing restart flow.

Add UTF-8 JSON files inside the plugin's `translations/` folder:

```text
my_plugin/
    plugin.json
    main.py
    translations/
        en.json
        de.json
        pt_BR.json
```

Each file maps English source text to translated text. Start with `en.json`
containing identical keys and values; contributors can copy it and translate
only the values. Use the manager's language codes (the `<code>` part of
`amethyst_<code>.qm`). All manager languages are supported without declaring
them in the manifest or compiling the plugin's translation files.

```json
{
  "My plugin": "Mein Plugin",
  "Downloaded {0} mods": "{0} Mods heruntergeladen"
}
```

Translate plugin-owned text before inserting dynamic values:

```python
from pathlib import Path
from amethyst_plugins import Translator

tr = Translator(Path(__file__).parent)
label.setText(tr("Downloaded {0} mods").format(count))
```

`Translator(directory, language=context["language"])` can select the supplied
language explicitly. Stdio plugins can also use `client.tr("Downloaded {0} mods")`.
Existing Qt `self.tr()` calls must use this plugin translator to read JSON
catalogues. Keep identifiers, paths, protocol values, and user-provided names
untranslated.

The host translates installed plugin names, descriptions, menu/button/tab
labels, settings labels, and declared flag/column/filter/command/preview labels
using the same catalogue. Remote store entries retain their source text until
the plugin is installed. Manifest values remain English; IDs and saved settings
are unchanged.

Lookups fall back per entry from a regional language (e.g. `pt_BR`) to its base
language (`pt`), then `en`, then the source text. Missing or malformed files,
empty values, and entries with changed format placeholders fall back safely.
Preserve placeholders and format specifications exactly, including `{0}`,
`{name}`, and `{0:.1f}`. Files are limited to 4 MiB and must remain inside the
plugin directory. Plugin updates back up custom files as described above;
keep a separate copy of contributed translations for future releases.

## Theme packs

Plugins can supply one or more themes shown in **Settings → Appearance → Themes**,
with the same previews and immediate selection as built-in themes. A theme-only
plugin needs a manifest and JSON palettes; no Python entrypoint is required:

```text
Plugins/my_themes/
    plugin.json
    palettes/
        midnight.json
        sunrise.json
```

```json
{
  "api_version": 1,
  "id": "my_themes",
  "name": "My theme pack",
  "description": "Midnight and Sunrise themes for the manager.",
  "version": "1.0.0",
  "author": "Your name",
  "min_manager_version": "2.6.0-beta.6",
  "ui": "themes",
  "themes": {
    "midnight": "palettes/midnight.json",
    "sunrise": "palettes/sunrise.json"
  }
}
```

For example, `palettes/midnight.json`:

```json
{
  "NAME": "Midnight",
  "BASE": "amethyst",
  "CTK_APPEARANCE": "dark",
  "PALETTE": {
    "BG_DEEP": "#101827",
    "ACCENT": "#38bdf8",
    "ACCENT_HOV": "#7dd3fc"
  }
}
```

`BASE` names a built-in theme and defaults to `amethyst`. Missing palette keys
inherit from that theme, including keys added in later manager versions.
`CTK_APPEARANCE` is `light` or `dark` and defaults to the base theme's value.
Use `"BASE": "light"` for a light palette. `NAME` is required, with at most 200
characters; `PALETTE` must contain at least one override. A JSON exported from
the theme editor can also be used directly.

Palette keys use uppercase letters, digits, and underscores. Values are strings:
hex colours (`#RGB`, `#RRGGBB`, `#AARRGGBB`), named colours, or numeric
strings for scanline settings. Light/dark pairs use two-element string arrays.
Files are UTF-8 JSON, limited to 1 MiB each. Invalid palettes are skipped and
reported in the manager log; other valid themes in the pack remain available.

The `themes` field maps up to 32 local theme IDs to JSON paths inside the plugin
folder. IDs use lowercase letters, digits, dots, underscores, and dashes, starting
with a letter or digit, and are limited to 100 characters. Paths must remain
inside the plugin folder, including symlink targets; absolute paths and traversal
are rejected. The same field is available alongside existing `stdio`, `qt`, and
`icons` declarations. Theme-only plugins can also supply `icon_overrides` and
settings, but cannot declare executable features or game restrictions.

Themes have stable IDs such as `plugin:my_themes:midnight`, avoiding collisions
with built-in themes, user themes, and other packs. Keep the plugin and local
theme IDs unchanged across updates. The existing `appearance.themes.list` and
`appearance.set` APIs include these IDs, so theme-switching plugins can use them.
The `custom` flag continues to identify user JSON themes (`custom:` IDs).

Install, update, enable, disable, and remove packs in **Plugins → Browse plugins…**.
Restart to apply changes, including manual palette or manifest edits. Themes
load directly from enabled plugin folders without importing plugin entrypoints;
no copies are placed in the user's custom-theme folder. If the selected theme
becomes unavailable, the manager displays Amethyst. The saved selection is kept,
so restoring the same theme ID restores its selection on a later launch.
Plugin theme palettes are also available as starting points in the theme editor;
saving an edited copy creates an independent user theme.

## Icon packs

Plugins can replace manager icons, including game images, without changing the
bundled files. An icon-only plugin needs `plugin.json` and its images; no Python
entrypoint is required:

```text
Plugins/my_icons/
    plugin.json
    images/
        settings.png
        skyrim.svg
```

```json
{
  "api_version": 1,
  "id": "my_icons",
  "name": "My icon pack",
  "description": "Alternative manager and game icons.",
  "version": "1.0.0",
  "author": "Your name",
  "min_manager_version": "2.6.0-beta.3",
  "ui": "icons",
  "icon_overrides": {
    "settings.png": "images/settings.png",
    "games/skyrim_se.png": "images/skyrim.svg"
  }
}
```

Keys are case-sensitive paths relative to `src/icons/`, such as `plugins.png`,
`check_white.png`, `Logo.png`, or `games/Fallout4.png`. Values are image paths
relative to the plugin folder, and must stay inside it, including symlink
targets. Absolute paths and `..` components are rejected. A manifest can declare
up to 512 replacements. The target filename stays `.png`; the replacement can
use any format supported by the installed Qt image readers, including PNG and
SVG. The manager decodes replacements before using them; unsupported or corrupt
images are skipped and logged. See [Qt image loading](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QImageReader.html).

The same `icon_overrides` field is available to existing `stdio` and `qt`
plugins alongside their normal declarations. Overrides are global, including
when a script's `game_ids` restricts its runnable actions. Icon-only plugins
can declare settings, but cannot declare actions, services, or game restrictions.

Install, update, enable, disable, and remove packs through **Plugins → Browse
plugins…** as usual. Restart after any pack change, including a manual image or
manifest edit. Selecting an icon pack in the Plugins menu opens the browser.
Overrides load after queued store changes have applied, without importing
plugin code. The initial startup checks use bundled artwork; the splash and
window icon then refresh with the enabled pack before the main window opens.

Packs can replace a subset of icons; other icons keep their bundled appearance.
If multiple enabled plugins replace the same path, the lexicographically first
plugin ID wins, and the conflict is logged. Disable the competing replacement
in the pack's settings (if provided), or disable the pack, and restart to change
that choice. Missing replacement files invalidate that
plugin's manifest; its overrides do not load. A corrupt replacement affects
only that icon, allowing another enabled pack or the bundled icon to supply it.

Game replacements appear in the game picker, game selector, and play menu;
`games/<custom_game_id>.png` can also supply an image for a custom game.
Existing custom-game images remain the fallback when no pack supplies them.
Theme-tinted controls still tint the replacement's alpha shape, including
rotated arrows and stylesheet icons. Game images retain their own colours.
Generated glyphs, embedded executable icons, downloaded thumbnails, and
desktop launcher/taskbar artwork supplied by the OS are outside `src/icons/`
and are unaffected.

### Individual icon switches

An override can reference a declared boolean setting instead of always being
active. String-valued overrides remain supported. For example:

```json
{
  "settings": [
    {"key": "use_search", "label": "Use this pack's search icon", "type": "boolean", "default": true}
  ],
  "icon_overrides": {
    "search.png": {"path": "images/search.png", "enabled_setting": "use_search"}
  }
}
```

This fragment belongs in the plugin's normal manifest. `enabled_setting` must
name a boolean setting declared by that plugin. When it is false, the pack
does not compete for that icon: another enabled pack can supply it, or the
manager uses its bundled image. Other replacements from the pack stay active.
Use **Plugins → Browse plugins… → select the pack → Settings…**, save the
switches, and restart. Existing packs must declare these settings to expose
individual switches. Icon-only plugins can declare up to 512 settings.

## Script plugin manifest

Unknown top-level manifest fields produce warnings, including suggestions for
likely misspellings. They do not prevent loading otherwise valid plugins.
Run `amethyst-mod-manager-cli plugin-validate /path/to/plugin` (or
`python src/cli.py plugin-validate /path/to/plugin`) for an import-free JSON
report of manifest/file validation, compatibility, declared backend methods,
and unreadable settings. Exit status is 1 for errors and 0 otherwise.

```json
{
  "api_version": 1,
  "id": "example.profile_report",
  "name": "Profile Report",
  "version": "1.0.0",
  "author": "Your name",
  "min_manager_version": "2.6.0-beta.1",
  "description": "Count the installed and enabled mods in this profile.",
  "entrypoint": "main.py",
  "methods": ["mods.list"],
  "settings": [
    {"key": "show_names", "label": "List mod names", "type": "boolean", "default": false}
  ]
}
```

### Publishing in the store

For the built-in source, publish one folder per plugin under `Plugins/v2` on
the `Resources` branch. Alternatively, publish to your own public GitHub
repository and share its URL, branch, and folder with users. A source folder
may contain one plugin directly, or immediate plugin subfolders containing
`plugin.json`, the entrypoint, and any nested assets or Python modules. No
separate catalogue index or ZIP is needed. The existing manifest fields remain
required; the store additionally requires nonempty `author`, `version`, and
`min_manager_version`. These fields are optional for older manual installations.

Versions use SemVer, including prereleases such as `2.6.0-beta.1`. Increase the
plugin version when publishing an update; unchanged or older versions are not
offered as updates. A stable version sorts after its prereleases; build metadata
does not affect ordering. A minimum manager version without a prerelease suffix
also accepts prereleases of that same release: `2.6.0` accepts `2.6.0-beta.1`.
An explicit prerelease minimum such as `2.6.0-beta.2` still excludes earlier
prereleases. Set the minimum manager version to the first release providing the
capabilities your plugin uses. Incompatible plugins remain
visible, but cannot be downloaded or updated, and installed manifests declaring
an unmet minimum version cannot load.

Keep user-specific `settings.json` and `data.json` out of published folders;
initialize defaults in plugin code instead. Packages support up to 1,024 files
and 64 MiB total. Symlinks, submodules, unsafe paths, duplicate IDs, and incomplete
packages are rejected. Browsing never imports plugin code. Downloads are pinned
to the catalogue's commit and checked against its Git blob hashes before being
queued. Third-party Python dependencies are not installed by the store.

An optional `game_ids` array limits the menu entry to those exact game IDs.
Omit it, or use an empty array, for all games. Settings support `boolean`,
`text`, `integer`, `number`, `choice`, `file`, and `directory`, with a matching
typed `default`. They are saved per plugin under
`settings.json` beside `plugin.json`.

Numeric settings accept optional `minimum` and `maximum`. Integers are bounded
to ±2,147,483,647; numbers to ±10¹² and are edited with 12 decimal places.
Choice settings require a `choices` array of unique strings; translated labels
do not change saved values. Text/path settings accept `max_length` (1–4096,
default 4096). `description` supplies a translated tooltip. File and directory
settings offer portal-aware Browse buttons and allow empty defaults. Paths are
saved as text; plugins validate existence when using them. Invalid saved values
fall back to defaults; new values are validated before saving.

```json
"settings": [
  {"key": "workers", "label": "Workers", "type": "integer", "default": 2, "minimum": 1, "maximum": 8},
  {"key": "mode", "label": "Mode", "type": "choice", "default": "fast", "choices": ["fast", "full"]},
  {"key": "output", "label": "Output folder", "type": "directory", "default": ""}
]
```

## Optional methods and plugin dependencies

`methods` remains the required backend API list. `optional_methods` declares
additional methods that may be absent on older managers. Supported optional
methods join the plugin's allowed methods; absent ones do not prevent startup.
Use `client.supports(method)` or `context["available_methods"]` before exposing
an optional action. Stdio hosts never enable managed launch methods this way.

```json
"optional_methods": ["manager_plugins.metadata.get"],
"plugin_dependencies": [
  {"id": "example.shared", "min_version": "1.0.0", "max_version": "2.0.0"},
  {"id": "example.extra", "optional": true}
],
"plugin_conflicts": ["example.alternative"],
"load_after": ["example.appearance"]
```

Dependencies use inclusive SemVer bounds and initialize before their dependents.
Unversioned plugins cannot satisfy versioned dependencies. Missing, disabled,
incompatible or blocked required plugins block their dependents with diagnostics;
optional dependencies may be absent/incompatible. Conflicts block both enabled
plugins. `load_after` adds ordering when the named plugin is present. Cycles and
plugins depending on them do not load. Game restrictions still apply: a required
dependency must be available for the current game. These declarations refer to
manager plugins, independently of downloaded-mod dependencies.

The store shows dependencies/conflicts; it does not automatically install,
enable, remove or replace companion plugins. Changes still apply at restart.
Disabling/removing a required companion leaves dependents installed but unable
to contribute until the dependency is restored.

## Plugin settings in the browser

Installed plugins with a nonempty `settings` declaration have a **Settings…**
button in the plugin browser. The manager builds the form from the manifest
without importing or running plugin code. This works for `stdio`, `qt`, and
`icons` plugins, including disabled plugins, and does not require a selected
game. The form uses the installed manifest when a store update is available.

Settings support switches, text/path fields, numeric controls and dropdowns.
**Save** writes them to
the plugin's `settings.json`, preserving unrelated stored keys. **Cancel**
leaves the file unchanged; **Restore defaults** changes the form until saved.
Save errors stay visible in the form. Script and Qt plugins can declare up
to 32 settings. Services and widgets receive settings through their normal
context refresh; scripts receive them on their next run. Icon settings apply
after restart. The browser's restart button is available after saving settings,
even when no install or enablement change is queued.

Settings belong to the installed plugin, so they can also be edited while an
update is queued; the store preserves the latest file when applying the update.
Duplicate installed IDs must be resolved before settings can be edited.

## Plugin-local storage

Unreadable or malformed settings are reported in the plugin settings editor.
**Reset damaged settings** saves the original file to a uniquely named backup
before writing the declared defaults. A failed reset retains the original and
any backup; ordinary Save never overwrites malformed settings.

Keep settings and other persistent plugin data inside the installed plugin
folder by default, so copying that folder also copies its configuration:

```text
Plugins/my_plugin/
    plugin.json
    main.py
    settings.json   # Host-managed manifest settings
    data.json       # Custom modal/service settings and other plugin data
```

All plugin contexts include `plugin_dir`, `settings_path`, and `data_path` as
absolute strings. These point to the manifest folder even when the entrypoint
is in a subdirectory. Qt modals and services can use `data` and `save_data()`
for custom settings. Both host-managed files use atomic writes and accept JSON
objects up to 1 MiB. Manifest settings still receive their declared defaults and
type checks; custom data has no required schema.

This is a convention, not a restriction. Plugins may manage their own files,
formats, or storage locations. The host does not redirect or enforce their
direct filesystem access. Keep plugin configuration when updating a plugin folder.
Manager-owned panel visibility and sizes remain in the global `plugin_ui.json`.

Existing `plugin_settings/<id>.json` and `plugin_data/<id>.json` files are copied
into the corresponding plugin-local files on first load. Existing local files
always take precedence, and the legacy copies remain as backups. If the plugin
folder is read-only, legacy values remain readable; saving through host helpers
reports an error instead of silently writing elsewhere.

```python
from amethyst_plugins import Client

client = Client()
inventory = client.call("mods.list", client.target)
counts = inventory["counts"]
client.log(f"{counts['mods']} mods; {counts['enabled']} enabled")
if client.context["settings"]["show_names"]:
    for mod in inventory["mods"]:
        if mod["kind"] == "mod":
            client.log(mod["name"])
```

The host uses Amethyst's Python interpreter and provides the client package.
Third-party Python dependencies are not installed automatically. The working
directory is the plugin folder. Keep stdout for protocol messages; use
`client.log()` or stderr for diagnostics. Existing `PLUGIN_INFO` Qt/Tk wizard
scripts use a separate mechanism and are not automatically converted.

## Read, plan, review, apply

Declare every headless method the plugin needs in `methods`. The host checks
them against the installed backend's handshake before running the script.
Handshake, ping, and `jobs.get/list/events/cancel/pause/resume` are available
automatically. Full headless capabilities are in `client.context["capabilities"]`;
availability does not grant undeclared methods.

Use `client.call(method, parameters)` for reads and planning. For a job-returning
method, `client.wait(response)` polls until completion, returns its result, or
raises `PluginError` with backend error details. Job submission is not success.

For a write, call `client.apply(method, parameters, title=..., summary=...,
details=...)`. The host shows the supplied plain-text preview and lets the user
inspect the exact API request. Approval executes that same request; cancellation
returns `None`. Direct calls to operations listed as mutations in the backend
handshake are refused. Backend `confirm`, revision, lock, and validation
requirements still apply.

For example, after constructing a complete layout request:

```python
plan = client.wait(client.call("mods.layout.plan.start", request))
if plan["changed"]:
    response = client.apply(
        "mods.layout.apply.start",
        {**request, "plan_revision": plan["plan_revision"], "confirm": True},
        title="Apply new layout?",
        summary=f"Move {len(plan['moved'])} mods.",
        details="\n".join(plan["entries"]),
    )
    if response is not None:
        client.wait(response)
```

See [headless.md](headless.md) for method parameters.

## Host protocol and lifecycle

The default `stdio` plugin is a separate process. It sends one JSON-RPC 2.0 request per stdout
line and reads one response per stdin line. Requests need an integer or string
`id`, a method, and object parameters. Send requests sequentially. JSON-RPC
batches and notifications are not part of host protocol version 1. Plugin
requests and responses are limited to 4 MiB of UTF-8 JSON. The Python client
implements this exchange. JSON uses a shared 128-level nesting ceiling and
rejects nonfinite values, including numeric overflow. Manifests retain their
64 KiB limit; settings and data retain their 1 MiB limits. File reads enforce
their size limits while reading, including if a file grows during the read.

Host methods:

| Method | Parameters | Result |
| --- | --- | --- |
| `host.context` | None | `api_version`, `game`, `profile`, `plugin_id`, `plugin_dir`, `settings_path`, `data_path`, saved `settings`, backend `capabilities` |
| `host.log` | `message` string | `true` |
| `host.apply` | Declared `method`, object `params`, strings `title`, `summary`, `details` | Backend result after approval, or `null` on cancellation |
| `host.inventory` | None | Detached cached desktop inventory snapshot |
| `host.events` | Optional nonnegative integer `after` | Process-local events for this session's game/profile, `next_sequence`, `truncated` |
| `host.selection` | Optional `location`, `source_id` | Detached cached list selection and current item |
| `host.notify` | Notification object | Accepted notification ID |
| `host.notifications.dismiss` | `id` | `true` after queuing dismissal |

Other methods use the headless parameter/result format. The service is scoped
to the selected game; its jobs belong to this plugin session. The selected
profile in context is the default target, not a filesystem access restriction.

Stop terminates the plugin process and requests cancellation of supported jobs.
The host waits for accepted manager operations to finish before releasing the
desktop. Noncancellable operations may take time. Stopping does not undo
completed changes. If a plugin exits without waiting for its jobs, the host
still waits and reports failures. Errors after a partial write still trigger
a manager refresh when the window closes.

## Custom Qt panels

Set `"ui": "qt"` in the manifest to provide an embedded PySide6 panel instead
of the standard script runner. The entrypoint exports
`create_view(context, parent, on_close)` and returns a child `QWidget` with a
`request_close()` method. Do not return a dialog or another top-level window.
The manager supplies a modal overlay, blocks desktop editing, and calls
`request_close()` when Escape or the manager's close button is pressed.

Qt panels may set `"requires_game": false` in their manifest to open before a
game is configured. The default is `true`. Such panels must accept empty
`game` and `game_id` strings and a null `profile` in their context. This also
enables their `open_plugin` toolbar buttons and Plugins-menu entry without a
selected game. After a panel reports changes, the host rediscovers handlers,
retains the selected game by ID when possible, and refreshes its views.

The returned widget may set a `preferred_size` attribute to a positive
`PySide6.QtCore.QSize` to request the outer modal card's width and height,
including its title and margins. The host fits it within the window, subject
to its minimum card size. Omit this attribute for the default 900 × 800 panel.

The panel calls `on_close(changed)` only after its operations and child
processes have finished. Set `changed=True` if anything may have changed,
including partially failed writes. The manager refreshes configured games and
profiles on completion. A panel's own Close button should call its
`request_close()` method too. Keep construction passive; start processes and
operations from user actions after the panel has been returned.

Context contains `api_version`, `plugin_id`, `game` (display name), `game_id`,
`profile`, saved `settings`, `headless_command` (an executable and argument
list), and `headless_documentation` (the installed manual path, or `null`).
Use the supplied command to start an independent JSON-RPC backend. Relative
imports within the plugin folder are supported. Qt panels run in the GUI
process; blocking work must use asynchronous processes or workers. The host
does not change the working directory for Qt panels; resolve plugin resources
relative to `__file__`.

Unlike the standard script runner, Qt panels own their backend sessions,
write approval UI, job tracking, cancellation, and managed-launch lifecycle.
The manifest's `methods` list is not enforced on independently started
backends; use `[]` if the panel discovers methods dynamically. Only install
trusted panels: their Python code executes with the manager's privileges.
Close the panel before replacing or removing its plugin folder.

### Portal file choosers

Qt plugins should use `gui_qt.dialogs.portal_picker` instead of calling
`QFileDialog` directly. Its `pick_file(parent, title, callback, filters=None)`,
`pick_folder(parent, title, callback)`, and `pick_save_file(parent, title,
callback, current_name="manifest.json", filters=None)` use the manager's
portal/desktop chooser fallback chain. Callbacks run on the GUI thread and are
discarded after the parent view closes. Cancellation returns `None`; selections
are `Path` objects. Filters use `(label, [glob, ...])` pairs.

```python
from gui_qt.dialogs.portal_picker import pick_folder

pick_folder(self, "Choose output location", self.folder_selected)
```

Keep picker callbacks short and recheck whether the panel is busy before
changing fields or starting work.

## Main-window UI extensions

Plugins can declare `ui_extensions` alongside their existing manifest fields.
These add controls after the built-in provider buttons, beside Settings, or in the lists' bottom button rows,
detachable tabs, or fixed panels to the right of or below the main lists on the
**Mods** page. Existing plugins need no changes. For example, a shortcut to a
plugin's existing panel is:

```json
"ui_extensions": [
  {
    "id": "open",
    "label": "Assistant",
    "type": "button",
    "location": "toolbar",
    "icon": "assistant.svg",
    "action": "open_plugin"
  }
]
```

Set the manifest's top-level `"menu_tab": "browser"` to make its primary Plugins
menu entry open the declared tab widget with ID `browser`. This replaces that
tab's separate menu entry and follows its game visibility. Other tab and panel
entries remain available. Omit `menu_tab` to keep the primary entry opening the
plugin's operation panel; `open_plugin()` still opens that panel in either case.

Each extension needs a plugin-local `id`, a nonempty `label` (at most 200
characters), a `type` (`button` or `widget`), and a `location` (`toolbar`,
`providers`, `tab`, `right_panel`, `bottom_panel`, `plugins_panel`, or one of the footer locations below).
IDs follow the same format as plugin IDs and must be unique within the manifest. At most 32
extensions are supported.
Optional icons are paths inside the plugin folder; missing or unreadable toolbar
icons use the generic wizard icon. Footer buttons use text alone unless a valid
icon is supplied. Button declarations do not import the entrypoint.

Buttons use `location: toolbar`, `providers`, or one of the footer locations. Their `action`
defaults to `open_plugin` and opens their own plugin through the usual busy
checks, modal locking, and refresh flow. To open a declared tab instead, use
`action: open_tab` and set
`target` to that tab's extension ID. These shortcuts work for both `stdio` and
`qt` plugins.

`location: providers` supports declarative buttons only. They appear immediately
after the built-in Nexus/Thunderstore/Wabbajack buttons, use the same labelled
style and compact-header behaviour, and invoke their action directly. For a
browser tab, declare `type: button`, `action: open_tab`, and its `target` tab ID.
Provider buttons do not use the small plugin toolbar's widget popovers.

Any extension may declare `game_visibility: "workshop_support"`, naming a map
in that plugin's `data.json`. It contributes only when
`data["workshop_support"][selected_game_id]` is exactly `true`; missing, false,
or invalid values hide it. Persistent services can check support asynchronously
and publish this map through `save_data()`. Saving data refreshes the extension
list, including removal of tabs that no longer apply to the selected game.
This filters interface extensions; it does not filter the plugin's service or
its main Plugins menu entry.

`modlist_footer` adds a text button after Refresh Modlist in the modlist panel's
button row, including the optional second Modlist tab. It uses the existing
button style and wraps with the built-in controls on narrow panels. This
location supports buttons only. For example:

```json
"ui_extensions": [
  {"id": "sort", "label": "Category Sorter", "type": "button",
   "location": "modlist_footer", "action": "open_plugin"}
]
```

The plugins panel's sub-tabs each support their own footer location:

| Location | Tab |
| --- | --- |
| `plugins_footer` | Plugins |
| `mod_files_footer` | Mod Files |
| `text_files_footer` | Text Files |
| `data_footer` | Data |
| `downloads_footer` | Downloads |
| `overrides_footer` | Overrides |
| `saves_footer` | Saves |

These locations support buttons only. Buttons appear after the tab's built-in
controls, use the same style and wrap onto another row when needed. They appear
when that tab is first opened, including tabs available only for certain games.
Declare separate extensions with unique IDs to show a shortcut on multiple tabs:

```json
"ui_extensions": [
  {"id": "plugins-shortcut", "label": "My Tool", "type": "button",
   "location": "plugins_footer", "action": "open_plugin"},
  {"id": "files-shortcut", "label": "My Tool", "type": "button",
   "location": "mod_files_footer", "action": "open_plugin"}
]
```

Custom widgets require `ui: qt`. For example:

```json
"ui_extensions": [
  {"id": "status", "label": "Profile status", "type": "widget", "location": "toolbar"},
  {"id": "report", "label": "Profile report", "type": "widget", "location": "tab"},
  {"id": "notes", "label": "Notes", "type": "widget", "location": "right_panel"},
  {"id": "activity", "label": "Activity", "type": "widget", "location": "bottom_panel"}
]
```

`location: plugins_panel` adds a widget tab beside **Data** in the plugins
column. It uses the same `create_widget`, `set_context`, and `dispose` lifecycle
as other widget locations. Its context includes `extension_id` so a plugin
can return different widgets from the same factory. Built-in tab indexes stay
unchanged. The host hides the built-in tool footer while a custom tab is
selected; put custom controls inside the widget. Hide or restore these tabs
from the Plugins menu. Disabling or removing the plugin disposes its tab.

The entrypoint exports `create_widget(context, parent)` in addition to its
existing `create_view` factory. Each call must return
a new child `QWidget` with `set_context(context)` and `dispose()` methods:

```python
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class StatusWidget(QWidget):
    def __init__(self, context, parent):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(self)
        layout.addWidget(self.label)
        button = QPushButton("Open plugin", self)
        button.clicked.connect(lambda: self.context["open_plugin"]())
        layout.addWidget(button)
        self.set_context(context)

    def set_context(self, context):
        self.context = context
        self.label.setText(f"{context['game']} · {context['profile']}")

    def dispose(self):
        pass


def create_widget(context, parent):
    return StatusWidget(context, parent)
```

Context contains `api_version`, `plugin_id`, `extension_id`, `location`, `game`
(display name), `game_id`, `profile`, saved `settings`, a `palette` dictionary,
and `toolbar_orientation` (`horizontal` or `vertical`). The factory receives
the initial context; `set_context` receives replacements when these values
change. Use `extension_id` to return different widgets for different declarations.
The `open_plugin()` callback opens the plugin's existing panel; `open_tab(id)`
opens one of the same plugin's declared tabs. Callbacks stop working after the
widget is disposed. Context supplies neither a backend command nor a manager
object: embedded widgets display information and handle their own UI; manager
operations belong in the existing guarded plugin panel.

Toolbar controls follow the toolbar when it moves. The strip is bounded to four
button widths, or less when a sidebar has insufficient height, with extra
controls in an overflow menu. Larger custom widgets,
and custom widgets in vertical toolbars, open in labelled in-window popovers.
Tabs open on demand directly under **Plugins**, labelled with the
plugin and tab names, and use the existing detach and panel-pinning controls.
Fixed panels appear automatically
on first discovery; their close buttons and the same menu hide or restore them.
Visibility and panel sizes are saved separately in `plugin_ui.json`. Multiple
fixed panels share a tabbed container; an empty container takes no space.

`game_ids` filters every contribution. Buttons need a configured game to launch;
custom widgets are constructed only with a configured game selected. All
extension surfaces, including detached tabs, become unavailable while a modal
plugin is active. The UI updates after that plugin closes.

Keep factories, context updates, and cleanup quick and passive. `dispose()` must
stop timers and disconnect external signals synchronously; it cannot veto
removal. It runs on tab closure, panel hiding, removal of a contribution,
incompatible game changes, and application shutdown. Moving, detaching, or
pinning a widget preserves its instance. Normal Qt child ownership handles
deletion. Factory or context-update failures disable that contribution for the
session and appear in the Plugins menu diagnostics and application log.

Manifest discovery remains import-free. Startup and opening the Plugins menu
reconcile declarations; visible toolbar widgets and fixed panels then load the
Qt entrypoint, and tabs load it on first opening. Modules stay alive until their
widgets are destroyed. Restart Amethyst after changing already-loaded Python
code; live code replacement is not supported. Persistent widgets use their own
module namespace, separate from each modal `create_view` session. As with Qt
panels, install only plugins you trust.

## Read-only modlist sections

Qt services and widgets can publish display-only rows beneath their own
separator at the bottom of the modlist, including detached presentations.
Declare sections and their actions in `plugin.json`; discovery remains import-free:

```json
"modlist_sections": [{
  "id": "external", "label": "External mods",
  "actions": [
    {"id": "open", "label": "Open provider page"},
    {"id": "refresh", "label": "Refresh", "selection": "section"}
  ]
}]
```

Publish on the GUI thread using the widget/service context callback:

```python
context["set_modlist_rows"]("external", [
    {"id": "123", "name": "Example mod", "tooltip": "Managed externally",
     "values": {"category": "External provider", "version": "1.0"}}
], game_id=context["game_id"], profile=context["profile"], on_action=handle_action)
```

Capture the game/profile when starting background work and pass those captured
values when publishing its result. A stale target or disposed context is
rejected. Pass `None` instead of a list to remove the section; an empty list
keeps its separator and section actions. One context owns each section.
Rows disappear on target changes, context disposal, or plugin disablement.

At most 16 sections per plugin, 10000 rows per section, and 8 MiB of JSON per
section are accepted. Row IDs must be unique and match
`[a-zA-Z0-9_.:-]{1,128}`. Names are nonempty strings up to 1000 characters;
tooltips are strings up to 8000 characters. Optional `values` may contain
`category`, `version`, `author`, `size`, `installed`, and `updated`, each a
display string of at most 4000 characters.

Rows have no checkbox or priority and cannot be moved, renamed, removed, or
used as archive-drop targets. They are separate from the natural mod entries:
they are never written to `modlist.txt`, included in managed-mod inventories,
or passed into deployment. Column sorting keeps each external section together.

Right-click actions belong to the declaring plugin. `selection: "items"`
(the default) operates on selected rows from that section; `"section"` also
appears on its separator and receives no item IDs. `handle_action` receives
`action_id`, `section_id`, `ids`, `game_id`, and `profile` on the GUI thread.
The host revalidates the target, owner, and IDs before dispatch. Callbacks
should return promptly; use workers for network work and open the guarded
operation panel for manager changes. Service contexts now also expose
`open_plugin()` for that handoff. External selection snapshots use
`external_mod`/`external_separator`, provider and section IDs, and
`read_only: true`; they contain no staging path.

## Context menu actions

Plugins can add script actions under **Plugins → Plugin name** in the manager's
context menus. Declare up to 32 actions in `context_menu_actions`; existing
manifests need no changes. Menu construction reads declarations without
importing or running plugin code.

```json
"context_menu_actions": [
  {
    "id": "inspect",
    "label": "Inspect selected files",
    "locations": ["mod_files", "text_files", "data", "downloads"],
    "entrypoint": "inspect.py",
    "filters": {
      "item_types": ["file"],
      "extensions": [".ini", ".json", ".zip"],
      "min_selection": 1,
      "max_selection": 20
    }
  }
]
```

Each action needs a unique plugin-local `id` using the same format as plugin
IDs, a nonempty `label` of at most 200 characters, and a nonempty `locations`
array. `entrypoint` is optional and defaults to the plugin's main entrypoint.
It must reference a Python file inside the plugin folder. Both `stdio` and `qt`
plugins use the standard script runner for these actions; Qt plugins should
declare a separate script when their main entrypoint only provides Qt factories.

| Location | Menu | Item types |
| --- | --- | --- |
| `modlist` | Mods, separators, and boundary rows, including the second Modlist tab | `mod`, `separator`, `overwrite`, `root_folder` |
| `plugins` | Game plugins, including vanilla entries | `game_plugin` |
| `mod_files` | Mod Files | `file`, `directory` |
| `text_files` | Text Files | `file`, `directory` |
| `data` | Data | `file`, `directory` |
| `downloads` | Downloaded archives | `file` |
| `overrides` | Overwrite and Root Folder file managers | `file`, `directory` |
| `saves` | Saves | `save`, `directory` |
| `nexus` | Nexus mod cards | `nexus_mod` |
| `thunderstore` | Thunderstore package cards | `thunderstore_package` |
| `collections` | Collection cards and installed appended-collection menus | `collection` |
| `wabbajack` | Wabbajack modlist cards | `modlist` |
| `tabs` | Tab management, including permanent tabs | `tab` |
| `text` | Standard line-edit and text-editor menus, including detached windows | `text` |
| `launcher` | Launcher mod-path insertion and prefix tools | `launcher` |
| `notifications` | Notification history and download progress menus | `notification` |
| `preview` | NIF preview View menu | `preview` |
| `suggestions` | Text-input and installer name suggestions | `suggestion` |

`filters` is optional. `item_types` and `extensions` are lists; omitted or empty
lists impose no restriction. Every item in the effective selection must match
each supplied filter. Extensions include the leading dot, match without case,
and may be compound suffixes such as `.tar.gz`; directory items never match an
extension filter. Selection limits are nonnegative integers, inclusive, and
default to zero minimum and no maximum. Item-type or extension filters do not
match an empty selection. These declarations combine with the plugin's existing
`game_ids` filter. Unknown locations, item types, filters, unsafe entrypoints,
duplicate action IDs, and invalid selection ranges reject the manifest.

Choosing an action opens the standard progress panel and starts it immediately
with the plugin's saved settings. It provides logs, Stop, and the usual Apply
preview for writes. The same method declarations, busy checks, manager locking,
FileGraph handoff, and post-change refresh apply. Actions require a configured
game and are unavailable while another plugin or the plugin store is open.
The host rechecks the manifest and active game/profile before starting; reopen
the menu if either changed. Exceptions appear in the progress panel or plugin
diagnostics. The menu closes before the action starts.

The script receives two additional fields in `Client.context`:

- `action_id`: the declaration's ID.
- `menu_context`: a JSON object with `location`, `clicked` (an item or `null`),
  and `selection` (the effective selected items).

Items always contain `type` and `name`. Depending on the source they also contain
`path` (an absolute disk path), `relative_path`, `mod_name`, `enabled`, `vanilla`,
or catalogue identifiers such as `mod_id`, `slug`, `namespace`, and `package_name`.
Catalogue entries include their domain/community where applicable; Nexus cards
also include their URL. Tab items include `id` when the tab has a host key.
These values are snapshots, not live widget or manager references. Some virtual
folders, synthetic entries, and archive members have no disk path.

Right-clicking within a selection passes that selection; clicking outside it
targets the clicked item. Downloads uses its existing checked-path selection
only when the clicked archive is checked. Nexus uses the checked cards only
when the clicked card is checked. Other single-item views pass one item; an empty
Mod Files area passes an empty selection and the containing `mod_name`.

For `text`, `menu_context.selected_text` and the clicked item's `text` contain
only selected text, with an empty selection list when no text is selected.
The clicked item also supplies `read_only` and the file `path` for the built-in
file editor. Password fields expose no text. Standard editing actions remain
available and widgets with disabled context menus are respected. Scripts do not
receive callbacks to replace editor contents. Suggestions expose the current
proposed name as `text`. Notification items describe the whole history/progress
menu with a `count`; `utility` distinguishes `history` and `downloads`. Launcher
menus similarly supply `utility` as `insert_mod_path` or `prefix_tools`.

For example, save this as `inspect.py` for the declaration above:

```python
from amethyst_plugins import Client

client = Client()
menu = client.context.get("menu_context", {})
for item in menu.get("selection", []):
    client.log(item.get("path", item["name"]))
```

This example needs no headless methods. For manager changes, declare the
required methods and use `client.apply()` with a preview as described above.

## Custom row flags

Plugins can add icons to the **Flags** column in the modlist and game plugins
panels. The plugin author defines the conditions in Python; the manager does
not impose a set of conditions. Add these fields to your existing `plugin.json`:

```json
{
  "flag_provider": "flags.py",
  "flags": [
    {
      "id": "review",
      "label": "Needs review",
      "icon": "icons/review.png",
      "panels": ["modlist", "plugins"],
      "tooltip": "This item matches the plugin's review conditions."
    }
  ]
}
```

`flag_provider` names a Python file inside the plugin folder. Each of up to
32 flags needs a unique `id`, a nonempty `label` (up to 200 characters), an
`icon` file relative to the plugin folder, and one or both `panels`: `modlist`
and `plugins`. An optional `tooltip` defaults to the label and accepts up to
4,000 characters. IDs follow the same rules as UI extension IDs. Icons and
provider files must remain inside the plugin folder, including after resolving
symlinks. Use an image format supported by Qt, such as PNG or SVG. Transparent,
square icons work best. Flags do not require `ui: qt` or a persistent service.
Manifest discovery does not import the provider.

Provide `evaluate_flags(context)` in `flags.py`. For example, the author of
this plugin decides that disabled mods and game plugins need review:

```python
def evaluate_flags(context):
    return {
        "modlist": {
            row["name"]: ["review"]
            for row in context["modlist"] if not row["enabled"]
        },
        "plugins": {
            row["name"]: ["review"]
            for row in context["plugins"] if not row["enabled"]
        },
    }
```

The return value maps panel names to row-name mappings. Each row maps to a
list of declared flag IDs allowed in that panel. Use the exact `name` supplied
in the context. Omitted rows, panels, or empty ID lists have no flags from this
plugin; every evaluation replaces that plugin's previous result. Unknown rows
or flag IDs reject that provider's result and are reported in the manager log.
Different plugins can reuse an ID; their flags remain independent.

The context is a detached snapshot containing:

- `api_version`, `plugin_id`, `plugin_dir`, `settings`, and saved `data`.
- `game_id`, `game`, `game_path`, `profile`, `profile_path`, and `mod_staging_path`.
- `modlist`: ordinary mods in natural order, including disabled mods. Each row
  contains `name`, `display_name`, `enabled`, `locked`, `path`, `priority`,
  built-in `flags`, `version`, and `category`. Separators, synthetic group
  headers, Overwrite, and Root Folder are excluded. `priority` is the zero-based
  position among ordinary mods in natural order.
- `plugins`: game plugins in natural load order, including disabled and vanilla
  plugins. Each row contains `name`, `enabled`, `vanilla`, built-in `flags`,
  `priority`, `path` (empty if unresolved), and `masters`.

Conditions run in a background worker after list changes and approximately
every five seconds, so they can also inspect plugin-owned files, settings, or
other external state. The module is loaded afresh for each evaluation; store
persistent state in the plugin's normal storage. Keep evaluations bounded and
read-only, and do not create Qt widgets or access the manager UI from them.
Manager editing operations belong in the existing reviewed plugin actions.
New evaluations pause while a modal manager plugin is open. Results from an older
list, game, or profile are discarded. A failing provider loses its own flags;
other providers continue to contribute and subsequent evaluations retry.

Icons appear after built-in flags, with the plugin's name and flag text in
the hover tooltip. They also appear in collapsed mod-group and separator
summaries and break ties when sorting the Flags column. They are status
indicators; use `context_menu_actions` for related commands. Independent
modlist tabs share the same results. Disabling, removing, or filtering out a
plugin by game removes its flags through normal plugin discovery.

## Custom list columns

Plugins can append sortable columns to the Mods and game Plugins lists:

```json
"column_provider": "columns.py",
"columns": [
  {"id": "score", "label": "Review score", "panels": ["modlist", "plugins"],
   "type": "number", "width": 100}
]
```

Declare up to 32 columns with unique plugin-local IDs, labels of at most 200
characters, and `panels` containing `modlist`, `plugins`, or both. `type` is
`text` (default), `number`, or `boolean`. Optional `width` is 40–600 pixels
(default 120). `column_provider` must name a Python file inside the plugin.
Discovery reads these declarations without importing the provider.

The provider exports `evaluate_columns(context)`. It receives the same detached
inventory snapshot as `evaluate_flags`, including plugin settings and data:

```python
def evaluate_columns(context):
    return {
        "modlist": {
            row["name"]: {"score": {"value": row["priority"], "tooltip": "Review order"}}
            for row in context["modlist"]
        },
        "plugins": {
            row["name"]: {"score": len(row["masters"])}
            for row in context["plugins"]
        },
    }
```

Return panel → exact row name → declared column ID → value. A cell may be a
plain value or an object with `value` and optional `tooltip`. Values must match
the declared type; `null` and omitted cells display blank. Text and tooltips
are limited to 4,000 characters; numbers must be finite. Unknown rows, columns,
or invalid values reject that provider's result and clear its previous cells.

Evaluation runs in a background worker after list changes and approximately
every five seconds. Keep it bounded and read-only. Results from an old game,
profile, or inventory are discarded, and evaluation pauses during manager
plugin operations. Each evaluation replaces the provider's previous values.

Headers include the plugin name. Columns participate in the existing visibility
menu and persist widths, order, visibility, and sorting under stable
`plugin:<plugin ID>/<column ID>` keys. Numeric columns sort numerically. Display
sorting leaves the actual mod priority and game load order untouched. The
second Modlist tab shares cell values and keeps its own display order. Removing
a column clears a sort that targeted it.

## Custom filters and saved views

Plugins can declare include/exclude filters for the Mods and game Plugins lists:

```json
"filter_provider": "filters.py",
"filters": [
  {"id": "review", "label": "Needs review", "panels": ["modlist", "plugins"]}
]
```

Declare at most 32 filters with unique plugin-local IDs, labels of at most 200
characters, and `panels` containing `modlist`, `plugins`, or both. The Python
`filter_provider` must remain inside the plugin folder. Discovery is import-free.

The provider exports `evaluate_filters(context)` and returns panel → exact row
name → list of matching declared filter IDs. Omitted rows match none:

```python
def evaluate_filters(context):
    return {
        panel: {
            row["name"]: ["review"]
            for row in context[panel]
            if "my_plugin/review" in row["row_flags"]
        }
        for panel in ("modlist", "plugins")
    }
```

The context has the same inventory, paths, settings and data as flag providers,
plus each row's `row_flags` (current namespaced plugin flag IDs) and `columns`
(current plugin column values keyed by `plugin:<plugin ID>/<column ID>`).
Providers may inspect other data to decide membership. Evaluation runs in a
background worker after inventory, flag, or column changes and approximately
every five seconds. Keep it bounded and read-only. Old-context results are
discarded. Invalid results clear that provider's previous membership; its
filters stop narrowing the list until a successful evaluation.

Both lists' column menus offer **Plugin filters**, grouped by plugin, with
**Include matches** and **Exclude matches** for each filter. The filter sidebars
also provide a **Plugin filters and saved views** button. Active conditions AND
together, then combine with existing search, native filters and collapse rules.
Separators remain visible when their blocks contain matching mods. Clear all
filters also clears plugin filters. Selections persist in manager-owned UI
state and are independent for the two Modlist views and the game Plugins view.

**Saved views** lets users save, apply and delete up to 64 named views per list.
A saved view captures search text, native and plugin filter selections, column
visibility/order/widths, and display sorting. These presets live in
`plugin_ui.json`; they do not change mod priorities or game load order. Missing
plugin columns and unavailable filters are ignored when applying a view.

## Persistent Qt services and appearance controls

A `ui: qt` plugin may declare `"service": true` to run independently of its
modal and widgets. The entrypoint exports `create_service(context)`, returning
an object with `set_context(context)` and `dispose()`. The factory receives the
initial selection at startup. `set_context` receives replacements when the
selected game, profile, configured-game list, saved settings, or plugin data
changes. Services can exist without a selected game (`game` and `game_id` are
empty strings). No toolbar widget or open tab is required.

Service contexts and modal `create_view` contexts include these additional fields:

| Field | Value |
| --- | --- |
| `configured_games` | Added games as objects containing `id` and `name` |
| `plugin_dir`, `settings_path`, `data_path` | Absolute paths to the plugin folder and its default storage files |
| `data` | This plugin's saved JSON object, initially `{}` |
| `save_data(data)` | Atomically save a JSON object of at most 1 MiB |
| `api_call(method, params={})` | Call a supported GUI-host API synchronously |

Plugin data is separate from manifest settings and saved at
`data.json` beside the plugin's manifest by default. Save failures raise an
exception. The service receives saved modal changes after the modal closes;
use `on_close(False)` when only plugin configuration changed, to avoid a mod
inventory refresh. Services, modals, and widgets have separate Python module
instances; use saved data to share configuration.

`api_call` currently supports only `appearance.themes.list`, `appearance.get`,
and `appearance.set`, using the [headless API](headless.md#application-appearance)
parameter/result format. Declare each required method in `methods`. A successful
GUI-host `appearance.set` persists and immediately applies the theme. It requires
`confirm: true`; plugin settings should provide the user's control over automatic
selection. Other manager operations remain in the guarded modal/backend flow.
UI-extension widget contexts do not receive these additional callbacks.

Services load automatically after manifest discovery and remain active when
their modal closes or widgets are hidden. Keep construction, context handling,
and cleanup quick and nonblocking. Context updates pause while a modal plugin
is active and resume afterward. Theme changes alone do not trigger service
context updates. `dispose()` runs synchronously on plugin removal, an incompatible
game change, failure, and shutdown; it must stop timers and disconnect signals.
Host callbacks become invalid on disposal. A failed service is disabled for the
session, logged, and reported under **Plugins**. Restart after editing
loaded service code. Existing plugins need no changes.

## Manager events and inventory reads

Qt service, modal and UI-extension contexts provide:

| Callback | Behaviour |
| --- | --- |
| `inventory()` | Return a detached copy of the latest idle desktop inventory, using the flag-provider snapshot fields. No backend process or filesystem scan is started. |
| `subscribe(names, callback, scope="current")` | Subscribe to one event name or a list of names. Returns an idempotent unsubscribe callback. `scope="current"` follows the selected game/profile; `scope="all"` receives events for every target in this process. |

Subscribe on the GUI thread and keep callbacks quick. They run on the GUI thread
after coalesced inventory updates. Callback exceptions remove only that
subscription and appear in the manager log. There are at most 32 subscriptions
per context. Disposing a service, widget or modal automatically removes its
subscriptions, including when creation fails. Host callbacks reject disposed
contexts. A plugin may unsubscribe earlier from its own `dispose()`.

```python
def start(context, on_change):
    current = context["inventory"]()
    unsubscribe = context["subscribe"](
        ["context.changed", "mods.changed", "plugins.changed"], on_change
    )
    return current, unsubscribe
```

Events are objects containing `sequence`, `type`, UTC `time`, `game_id`,
`profile`, and a `data` object:

| Event | Data |
| --- | --- |
| `context.changed` | `previous`: previous `[game_id, profile]`, or `null` |
| `mods.changed`, `plugins.changed` | Inventory snapshot changed; read `inventory()` for current rows |
| `mods.added`, `mods.removed`, `plugins.added`, `plugins.removed` | `names` |
| `mods.enabled`, `plugins.enabled` | `changes`: `{name, enabled}` objects |
| `mods.reordered`, `plugins.reordered` | `names` in natural order; display-only sorting does not produce this event |
| `install.completed` | `success`; desktop archive batches also include `installed`, `total`, and `names`; backend install jobs include `job_id`, `kind`, and terminal `state` |
| `deploy.completed`, `restore.completed` | `success`, including false results and raised failures |
| `job.completed` | `job_id`, `kind`, and terminal `state`; backend job results and credentials are not included |
| `selection.changed` | `location`, `source_id`, `items`, `current`, `selection_count`, `truncated`, and `active` |

Inventory events describe differences between accepted desktop snapshots.
Opening another profile establishes a new baseline instead of reporting every
old mod removed and every new mod added. Installation completion is separate
from inventory additions: an update can replace an existing mod. Deployment
and Restore events come from the shared pipelines after the operation lease
has been released.

GUI delivery and snapshot refresh pause during modal plugin operations and
resume when the desktop becomes available. The cached inventory can therefore
be older than changes made by a running script; use the declared backend read
methods for the latest operation state. Events preserve their original target.
Each process retains up to 512 history entries and the GUI bounds pending
delivery to the same number; overflow is reported in the log.

Scripts use `client.inventory()` and `client.events(after)` (host methods, so
no backend declaration is needed). Save the returned `next_sequence` and pass
it as the next cursor. `truncated: true` means history was lost and a full
inventory read is appropriate. Script polling is scoped to its captured target
and continues while its modal is open. This is a process-local event stream:
independently launched Headless processes and other running manager instances
do not share the desktop's events.

## Live list selection

Qt services, modals and UI-extension widgets receive
`selection(location=None, source_id=None)`. Scripts use
`client.selection(location=None, source_id=None)` (`host.selection`). Reads
return a detached cached snapshot without changing the user's selection:

```python
def start(context, show_selection):
    show_selection(context["selection"]("modlist"))
    return context["subscribe"](
        "selection.changed",
        lambda event: show_selection(context["selection"]("modlist")),
    )
```

Omit both arguments to follow the last focused list. Focusing a plugin panel
keeps that list as the active source. A `location` requests the last focused
source in that location; `source_id` requests a specific view:

| Location | Source IDs |
| --- | --- |
| `modlist` | `modlist`, `modlist_tab` |
| `plugins` | `plugins` |
| `mod_files`, `text_files`, `data`, `downloads`, `saves` | Same as location |
| `overrides` | `overwrite`, `root_folder` |

The snapshot contains `game_id`, `profile`, `location`, `source_id`, `items`,
`current`, `selection_count`, `truncated`, and `active`. Items use the same
JSON descriptors as context-menu actions: `type`, `name`, and available path,
mod name, relative path, enabled state, or other relevant fields. `current`
is the keyboard-current item, which can be outside the selection, or `null`.
No Qt indexes, model objects or text-input contents are exposed.

Snapshots contain at most 256 selected items, with the full selected-row count
and an explicit `truncated` flag. Unresolved source paths are omitted. Selection
changes are coalesced for approximately 50 ms and publish `selection.changed`
through the normal event API. Its `active` field distinguishes the focused
list from updates to other views. Focusing a different list also produces an
event when that list's selection has not changed.

Changing game/profile invalidates cached selections immediately. Closing a
source clears its cached selection. Lazy and detached views use the same
binding and disposal flow. As with inventory reads, capture and GUI event
delivery pause during modal plugin operations; scripts receive the captured
desktop selection and can still poll operation events.

## Notifications and diagnostics

Qt service, modal and UI-extension contexts provide `notify(notification)` and
`dismiss_notification(id)`. Scripts use `client.notify(notification)` and
`client.dismiss_notification(id)`. These host methods do not need backend
method declarations. Posting from a script worker is marshalled to the GUI.

```python
context["notify"]({
    "id": "dependency-review",
    "title": "Dependency check",
    "message": "Example mod requires attention.",
    "severity": "warning",
    "details": "Explain the finding and recommended next step here.",
    "actions": [
        {"type": "select", "label": "Select affected mod",
         "location": "modlist", "names": ["Example mod"]},
        {"type": "run_action", "label": "Review fix", "action_id": "fix",
         "location": "modlist", "names": ["Example mod"]},
    ],
})
```

`id` uses the plugin ID format and is scoped to the plugin, game and profile.
Reposting the same ID updates its history entry; identical content is ignored.
`title` defaults to the plugin name and is limited to 200 characters. `message`
is required and limited to 4,000 characters. `severity` is `info` (default),
`success`, `warning`, or `error`. Optional plain-text `details` supports 64,000
characters and adds an **Open report** button with a scrollable report. Set
`toast: false` to update notification history without a transient toast.

Declare at most eight `actions`, each with `type` and a label of at most 100
characters:

| Type | Additional fields | Behaviour |
| --- | --- | --- |
| `report` | None | Open this notification's report |
| `select` | `location`: `modlist` or `plugins`; `names` | Reveal and select up to 256 named items, clearing that view's filters/search and expanding mod groups as needed |
| `open_plugin` | None | Open this plugin through the normal guarded panel flow |
| `open_tab` | `target`: declared tab ID | Open this plugin's existing tab |
| `run_action` | `action_id`: declared context-menu action; `location`; optional `names` | Revalidate the target and action filters, then use the standard reviewed script runner |

The history entry identifies its plugin and target. Actions require the
original game/profile and a currently available plugin. Missing items or
changed action declarations are rejected. Reports remain readable as history
even after the user switches profiles. Action buttons close the notification
dropdown before opening another surface. Dismissing an ID removes its history
entry; transient toasts finish their normal display lifetime.

For automatic reports, declare `"diagnostic_provider": "diagnostics.py"` in
the manifest. The Python file must remain inside the plugin folder; discovery
does not import it. Export `evaluate_diagnostics(context)` and return up to 64
notification objects with unique IDs. The context uses the same detached
inventory, settings and data as flag providers:

```python
def evaluate_diagnostics(context):
    disabled = [row["name"] for row in context["modlist"] if not row["enabled"]]
    if not disabled:
        return []
    return [{
        "id": "disabled-review",
        "message": f"{len(disabled)} mods are disabled.",
        "details": "\n".join(disabled),
        "actions": [{"type": "select", "label": "Select mods",
                     "location": "modlist", "names": disabled[:256]}],
    }]
```

Diagnostics run in a background worker after inventory changes and
approximately every five seconds. Keep evaluation bounded and read-only.
Each accepted result replaces that provider's current-target diagnostics;
omitted or resolved findings are removed. Invalid results clear its previous
findings and appear in the manager log. Old-context results are discarded.
Unchanged findings do not repeat toasts; new or changed findings produce one
summary toast per provider rather than a toast for every row. Manager-owned
notification history and action metadata remain bounded.

## Installation and deployment lifecycle hooks

Declare automatic validation and observation callbacks in `lifecycle_hooks`:

```json
"lifecycle_hooks": {
  "before_archive": "hooks.py",
  "after_extract": "hooks.py",
  "before_install": "hooks.py",
  "after_install": "hooks.py",
  "before_deploy": "hooks.py",
  "after_deploy": "hooks.py"
}
```

Declare only the callbacks needed. Each path must name a Python file inside
the plugin folder; discovery remains import-free. The file exports a function
with the declared event name, taking one context object. Existing `game_ids`
filters apply. A declared boolean setting named `enabled`, when false, disables
the plugin's lifecycle hooks before importing their code.

| Callback | Point in the operation |
| --- | --- |
| `before_archive` | Before the shared archive preparation/extraction step, including archive inspection |
| `after_extract` | After extraction and installer detection, before the prepared archive is handed to the caller or installer UI |
| `before_install` | Before staging/replacing mod files; collection callbacks run when choices are resolved and the archive will actually be installed |
| `after_install` | After a successful installation and normal cleanup; final installed paths are available |
| `before_deploy` | Inside the shared deployment lease, before the deployment pipeline runs |
| `after_deploy` | After the pipeline attempt and before releasing its lease; includes failed/rejected attempts |

Validation callbacks may return `None` or `True` to proceed, return `False` to
reject, or raise an exception with an explanation. Rejections identify the
plugin and event. Archive inspection rejection cleans extraction resources;
install rejection occurs before replacing destination files. A rejected
deployment returns failure and does not run the pipeline or launch a game.
`after_install` and `after_deploy` are observers: their failures are logged and
do not change the completed operation's result.

```python
from pathlib import Path


def after_extract(context):
    if (Path(context["extract_dir"]) / "unsupported-layout.txt").exists():
        raise ValueError("This archive needs a different installer.")


def after_install(context):
    for mod in context["installed_mods"]:
        context["log"](f"Installed {mod['name']} at {mod['path']}")
```

Context includes `api_version`, `plugin_id`, `plugin_dir`, saved `settings`
and `data`, `event`, `game_id`, `game`, `profile`, `profile_dir`, `log(message)`,
and `check_cancelled()`. Archive/install callbacks add `archive_path` and the
requested `mod_name`; extracted/prepared callbacks also add `extract_dir`,
`source_root`, and `installer` (`plain`, `fomod`, `bain`, `bundle`, or `multi_mod`).
`after_install` adds `success: true` and `installed_mods` as `{name, path}`
objects, including every output of a multi-mod archive. The user may have
renamed a mod after the pre-install callback; use these final output names.
Its extraction paths may already have been removed. `after_deploy` adds
`success` and `started` (false when validation prevented pipeline execution).

Hooks run synchronously on the operation's worker, without Qt objects or a
manager object. Keep them bounded, use them for validation/inspection, and call
`check_cancelled()` in long loops when cancellation is available. Each callback
loads its module afresh and unloads relative imports afterward. Persistent
state belongs in plugin-owned data. Manager mutations and proposed fixes belong
in the existing reviewed actions. These trusted Python callbacks are not a
filesystem sandbox.

Common archive preparation/install paths cover desktop, CLI, Headless and
collection installs, including multi-mod archives. Deferred installers do not
run install hooks until they actually stage files. The deployment hook uses
the shared pipeline, including deploy-before-launch. Compiled Wabbajack profile
assembly has its own pipeline; use its `install.completed` event to observe
completion rather than expecting per-archive callbacks.

## Mod operation guards

Plugins can reject user mod changes through `operation_guards`:

```json
"operation_guards": {
  "mods.set_enabled": "guards.py", "mods.move": "guards.py",
  "mods.rename": "guards.py", "mods.remove": "guards.py"
}
```

Each declared module exports `validate_operation(context)` and returns `None`
or `True` to allow, `False` to reject, or raises an exception explaining why.
Contexts include plugin/game/profile identifiers, `profile_dir`, settings,
data, `operation`, and `mods` (the affected names). Enable changes add
`changes: [{name, enabled}]`, rename adds `new_name`, and moves add natural
`old_order`/`new_order` plus `position` for backend anchor moves. Moving other
mods past a protected mod does not include that protected mod in `mods`.
Declare `manager_plugins.metadata.get` to read the plugin's namespace through
`metadata`. Guards run before persistence or destructive file operations in
the desktop and shared backend paths. Group removal preflights affected member
profiles before any deletion. Rejected desktop list edits restore the saved
model and do not emit activation changes. Manager disablement and a false
plugin `enabled` setting suppress guards.

Keep these trusted callbacks quick, read-only and free of UI/network work.
They can run on the GUI thread or an operation worker and are loaded afresh.
Recursive guarded mutations are rejected. These are operation validations,
not a filesystem sandbox or hooks for observing installation/restore work.

## Custom file previews

Qt plugins can register viewers by file extension without importing viewer code
during discovery:

```json
"file_previews": [
  {"id": "config", "label": "Config inspector", "extensions": [".myconfig", ".packed.json"],
   "entrypoint": "preview.py", "priority": 0}
]
```

Declare up to 32 viewers with unique plugin-local IDs, labels of at most 200
characters, and 1–32 extension suffixes. Suffix matching is case-insensitive and
supports compound extensions. `entrypoint` defaults to the plugin's normal
entrypoint and must remain a Python file inside the plugin folder. `priority`
is an optional integer from -100 to 100. The plugin must use `ui: qt`; its
normal panel, services and other extensions keep their existing contracts.

The viewer module exports `create_preview(context, parent)`, returning a new
child QWidget with `set_context(context)` and synchronous `dispose()` methods:

```python
from PySide6.QtWidgets import QLabel


class Preview(QLabel):
    def __init__(self, context, parent):
        super().__init__(parent)
        self.set_context(context)

    def set_context(self, context):
        self.setText(context["relative_path"])

    def dispose(self):
        pass


def create_preview(context, parent):
    return Preview(context, parent)
```

Context includes the service context's target, settings, plugin-local data and
storage helpers, inventory/selection reads, subscriptions and notification
callbacks. It adds `preview_id`, `location: "preview"`, absolute `path`,
`relative_path`, `file_size`, `modified_ns`, and the current `palette`. It does
not expose a manager object or the synchronous appearance mutation callback.
Keep factory/context handling passive and quick; read large files using workers
and stop workers/timers/subscriptions in `dispose()`.

Clicking matching files in Mod Files, Text Files, Data, Saves, or the Overwrite
and Root Folder managers opens a panel-scoped viewer tab. Each declared viewer
reuses its tab and widget as the selected file changes. Detaching or pinning a
tab preserves that instance. Closing it disposes the context and unloads the
module; changing game/profile or removing the declaration closes it as well.
Preview tabs follow the manager's modal busy state.

When multiple viewers match, longer suffixes win, followed by higher priority
and then plugin/viewer ID. File context menus also offer each matching viewer
under **Plugins → Plugin name → Preview with …**, including downloaded archives
and game plugin files when a physical path is available. These explicit choices
leave the existing built-in menu actions available. A failing viewer is logged
and disabled until its declaration changes or Amethyst restarts; another matching
viewer or the existing file handler can then handle the click.

Viewer paths refer to physical loose files. Reading members inside a packed
archive still uses the manager's archive workflows; declaring a suffix does not
automatically extract archive members.

## Metadata on profiles and mods

Plugins can attach structured JSON records to the selected profile and its
ordinary mods. Records live in the profile's `profile_state.json`, under
`plugin_metadata` and the plugin ID. They are separate from plugin-local
`settings.json` and `data.json`.

Declare `manager_plugins.metadata.get` and `manager_plugins.metadata.set.start`
in `methods`. Script helpers keep requests in the plugin's namespace and route
writes through the normal preview:

```python
current = client.metadata(["Example mod"])
response = client.set_metadata(
    {"mods": {"Example mod": {
        "value": {"reviewed": True, "tags": ["compatibility-checked"]},
        "portable": True,
    }}},
    current["revision"],
    summary="Mark Example mod as reviewed.",
)
if response is not None:
    client.wait(response)
```

`client.metadata(mods=None)` returns `plugin_id`, target `profile`, a revision,
profile-level `metadata` (a record or `null`), and `mods` mapping requested mod
names to records or `null`. Omit `mods` to read all ordinary mods; use `[]` for
only profile metadata. Separators, cosmetic group headers, unknown names and
ambiguous names are rejected.

`set_metadata(changes, revision)` replaces selected records. `changes` may have
`profile` and/or a `mods` mapping. Every record is an object containing an object
`value` and optional boolean `portable` (default false). Use `null` to remove a
record. Each record supports finite JSON values with at most 16 nesting levels
and 64 KiB of serialized JSON. A namespace is limited to 1 MiB and a profile's
combined plugin metadata to 8 MiB. Each request supports up to 4,096 mod names.
Edits are atomic, preserve unrelated profile state and legacy settings, and
refuse a changed mod inventory or namespace revision. Reads do not create or
migrate profile state.

The raw backend methods are documented in [headless.md](headless.md#plugin-owned-metadata).
They take `game`, `profile` and `plugin_id`; writes also require the revision,
changes, and `confirm: true`. Standard script sessions reject requests for
another plugin's namespace. Qt plugins use their reviewed backend operation
flow for writes. Declaring the read method also adds the plugin's own
`metadata` namespace (`profile` record and `mods` record mapping) to flag,
column, filter, diagnostic and lifecycle callback contexts. Background
providers share one metadata read per evaluation batch.

The manager handles record identity and portability:

- Mod renames re-key records. Mod copy/link/move carries the mod's records into
  the destination profile, including an explicitly chosen new folder name.
  Profile-level records stay with their profile.
- Successful removal drops the removed mod's records, including affected
  profiles that share its physical staging folder and dependent linked mods.
- Profile Groups adopt member mod records once, then own their copies. Group
  reconciliation migrates renamed rows and drops removed rows.
- Full profile copies and backups retain all records. Metadata values are
  opaque: profile copying leaves them unchanged while rebasing the manager's
  own path settings.
- Profile archives and Amethyst collection state include only records marked
  `portable: true`. Collection import maps authored mod names to installed
  names. Local records remain local. Mark only records intended for sharing
  portable.

Removing a manager plugin leaves its profile annotations available for a later
reinstall. A plugin can clear its records explicitly through the same reviewed
metadata API.

## Launcher providers

Plugins can expose installed applications to desktop and Headless game detection
without opening a Qt panel. Declare a provider in `plugin.json`:

```json
{
  "launcher_providers": [
    {"id": "library", "label": "My Launcher", "entrypoint": "provider.py", "icon": "icons/launcher.png"}
  ],
  "persistent_directories": ["prefixes"]
}
```

The provider identity is `<plugin-id>/<provider-id>`. Its Python module exports
`list_entries(context)` and `get_entry(context, entry_id)`. Optional
`update_entry(context, entry_id, changes, revision)` makes settings writable.
Return JSON-compatible objects; `list_entries` returns a list. Keep this module
independent of Qt. The manager imports it only in a separate worker process,
with a 20-second discovery/read timeout. Failed providers and malformed entries
are reported independently. Providers are trusted local code, not a sandbox.

The optional `icon` names a file inside the plugin folder. Amethyst uses it for
Configure Game's **Detected installs** buttons and Add Game's launcher badges,
preserving its original colours. Absolute paths, traversal and symlink escapes
are rejected. Omitted or unreadable images use the generic Plugins icon.
This declaration is independent of toolbar shortcuts and does not import Python
code during manifest discovery. Application Launcher bundles its Amethyst logo
at `icons/amethyst.png` and declares that path on its launcher provider.

Context includes `api_version`, `plugin_id`, `provider_id`, `plugin_dir`,
`data_path`, saved `settings` and `data`, `log(message)`, and `check_cancelled()`.
Discovery must be read-only: never initialize prefixes, download runners or
launch applications from either read callback. Return empty lists for missing
libraries. Update callbacks must re-read their storage, reject stale revisions,
and write atomically. Updates finish before cancellation/closing; the worker
allows up to one hour for prefix relocation.

An entry has these fields:

| Field | Value |
| --- | --- |
| `id`, `name`, `revision` | Stable entry ID, display name, opaque storage revision |
| `game_path`, `executable` | Absolute installation root and launch executable |
| `prefix_path` | Actual Wine prefix containing `drive_c`, or null for native apps |
| `compat_data_path` | Optional Proton compatibility-data root; distinct from `prefix_path` |
| `runner` | `{"kind":"proton","path":"/path/to/proton"}`, `wine` with an absolute binary path, or `{"kind":"native"}` |
| `arguments`, `launch_options` | Argument text and Amethyst's existing `%command%` launch-options syntax |
| `installed` | Boolean, default true; false excludes installer-only entries from detection |
| `plain_wine`, `wayland` | Boolean launch controls, default false |
| `lsfg`, `mangohud` | Existing Amethyst frame-generation and overlay settings objects |
| `game_id`, `umu_id` | Optional explicit handler ID and UMU game-fix ID |

Amethyst adds `provider_id`, `provider_label`, `provider_icon` (the resolved
manifest icon path or null), and `writable` to returned entries. Entry callbacks
cannot override the provider's manifest icon.
Game detection validates supported handlers against files under `game_path`;
display names are not matching evidence. Multiple matching entries remain
separate choices. Unsupported applications stay in the launcher library.

Headless methods `launchers.entries.list.start`, `launchers.entry.get.start`,
`launchers.entry.set.start`, and `launchers.entry.run.start` work without a
configured game. The latter three require `provider_id` and `launcher_id`.
Set additionally requires `changes` and `revision`. Run accepts optional
`executable` and `arguments` overrides for installers/utilities, without changing
the saved entry. Poll the returned job to completion. Runs use the existing
`launch.get` / `launch.stop` lifecycle and hold the prefix lock until completion.

A normal installed-entry run automatically uses the manager's Play path when
one matching managed profile is deployed with VFS enabled. Matching uses the
saved provider/entry identity, or the same installation directory, prefix and
game executable. It selects the deployed profile, applies its loader and launch
hooks, and retains the entry's runner/settings. Temporary argument overrides
still apply. Temporary executable overrides retain the standalone tool path and
cannot bypass the deployed-profile guard. Missing VFS state and ambiguous
deployments fail explicitly; runs never deploy a profile automatically. After
Restore, ordinary entry launching resumes. VFS launch results identify the managed
game under `launch.game`, retain the entry ID as `launch.game.launcher_id`, and
include the deployed profile under `launch.profile`.

Installation results use `source: "plugin"`, `provider_id`, `provider_label`,
`provider_icon`, and the entry's `launcher_id`. `games.prefix.detect.start` and
`games.launcher.set.start` accept the same identity. The latter requires paths
already configured for that installation. Existing launcher formats remain
valid. `launch.settings.get` reports provider ownership and writable status;
shared controls are saved through the provider while deployment and game toggles
remain Amethyst settings. A missing/disabled provider leaves profiles intact
and fails launching explicitly instead of falling back to another launcher.

### Persistent plugin directories

`persistent_directories` is an optional list of top-level directory names.
Packages must not contain files at these locations. Updates and repository
replacement transfer existing directories by rename, preserving prefix files,
symlinks and identities without duplicating their contents into each backup.
The store journals these transfers and reverses them during failed-update
recovery. Retained removal/reinstallation preserves directories; **Delete saved
data** removes them along with the retained plugin folder. External prefix
locations are never removed by the store. Previously declared directories stay
retained across replacement by a plugin version that omits the declaration.

### Application Launcher

The installed **Application Launcher** plugin opens without a selected game.
It adds existing EXEs or runs EXE/MSI installers, then asks for the installed
EXE. New prefixes use the application name, with numeric suffixes for duplicates;
existing prefix paths remain unchanged. Its default prefixes live inside its plugin folder, with a configurable
default location and per-entry custom/shared prefix selection. It uses installed
Proton builds or an explicit runner path; UMU can provision its Steam runtime
on first launch. Proton builds are not downloaded by this plugin.

Applications display their executable's embedded icon. The configure panel uses
a compact layout and an installed-Proton version dropdown, with a custom-runner
Browse option. Plugin file/folder selection follows the manager's portal chooser.

Remove offers to keep or delete the prefix. Deletion confirms its path, refuses
shared/in-use or linked prefixes, and recovers interrupted removals. Prefix moves require an empty destination and retain
the source until linked configuration and library updates succeed. Restore
linked profiles before changing installation paths or runners. Launch deployed
games through Amethyst's Play control so the profile's VFS and launch hooks are
used. MAKO/LSFG and MangoHud use Amethyst's existing setup/settings panels.

## Launch hooks

An installed plugin may declare `"launch_hooks": "launch.py"` to prepare a
deployed profile immediately before the launch router runs. The path must name
a Python file inside the plugin folder; it may be the normal entrypoint.
Use `"game_ids": ["skyrim_se"]` to restrict discovery to Skyrim Special Edition,
including AE. Filtering happens before importing the launch module. Omit
`game_ids` for a plugin intended for every game. A boolean manifest setting
named `enabled`, when false, disables its launch hooks before import.

The module exports `before_launch(context)` and optionally
`after_launch(context, token)`. The first callback returns an arbitrary token
passed to the second. Return `None` when there is nothing to do. Callbacks run
in the launch/monitor worker, independently of Qt widgets and persistent
services. Keep stdout reserved for Headless/CLI protocols and log through
`context["log"](message)`. Use only trusted plugins: these hooks perform their
declared automatic filesystem work without a per-launch Apply dialog.

Launch-hook messages go to the desktop Log panel and persistent session log
when started through Play/Run. Headless launches include them in the launch
logs and launch job output; CLI handoffs use the CLI's launch log destination.
Include your plugin name in messages so users can identify their source.
Script plugins use `Client.log(message)` (`host.log`), which the standard
plugin runner also forwards to the desktop log.

Context contains `api_version`, `plugin_id`, `plugin_dir`, `settings_path`,
`data_path`, saved `settings`, `load_data()`, `save_data(object)`, `game_id`,
`game` (display name), `profile`, and `profile_dir`. Launch fields are:

| Field | Value |
| --- | --- |
| `game_path`, `data_root` | Physical deployed game/data tree, or validated VFS shadow paths |
| `configured_game_path` | Game path used by the launch router |
| `overwrite_path` | Selected profile's effective Overwrite folder |
| `wine_prefix` | Saved game prefix, or `null` |
| `executable` | Selected custom executable, or `null` for the game entry |
| `is_game` | Whether this entry starts the game/framework loader rather than a tool |

`resolve_data_files(paths)` accepts at most 256 data-relative paths and returns
a mapping to the active profile's winning loose Filegraph source paths. It reads
the existing catalogue without acquiring the native library's writer lock or
reconciling it. Missing or unavailable catalogue entries are omitted; callers
must check that returned files still exist. Runtime-created files may exist in
the deployed game/VFS view before Filegraph knows about them, so check that view
as well.

Desktop Play/Run and Headless `launch.start` use these hooks after any optional
deployment. Nested game-to-script-extender routing runs them once. Undeployed
games skip hooks; a selected/deployed profile mismatch refuses a hooked launch.
An exception in preparation prevents dispatch and runs cleanup for modules
already loaded, including the failing module with a `None` token. Plugins must
clean up partial work if their preparation fails before returning a token.
Tracked preparation keeps manager editing locked. Stop requests cancellation;
long operations should call `context["check_cancelled"]()` periodically, which
raises if the launch was cancelled. The game is not dispatched after a cancelled
preparation. Callbacks must finish before the manager becomes available again.

For tracked launches, `after_launch` runs before the manager receives the final
idle notification, with `context["completion"]` containing `started`,
`cancelled`, `stopped`, and known direct-child `returncodes`. A launcher-owned
process may expose no exit code. Cleanup runs once in reverse preparation order;
its errors are logged without suppressing other cleanup. Modules are unloaded
afterward. A process appearing does not establish successful game startup or
cache completion; plugins must verify their own output.

CLI launcher handoffs (`launch ... -- <command>`) also prepare the deployed
profile before command construction, including Flatpak bridge handoffs. These
replace or leave the manager process and cannot promise an exit callback.
Calls without a bound launch session have the same limitation. Persist pending
work before writing configuration, and recover it on a later launch. Vanilla
passthrough without a deployed profile remains unchanged.

The source plugin in `Plugins/v2/automatic_crdw/` demonstrates this interface.
It is installed separately, limits itself to Skyrim SE, selects CACHE/LOAD
from deployed asset changes, and verifies profile-specific cache files.

## Download providers

Declare a repository or download source in the manifest:

```json
"download_providers": [
  {
    "id": "example",
    "label": "Example repository",
    "entrypoint": "provider.py",
    "schemes": ["example", "https"],
    "hosts": ["project", "example.com"]
  }
],
"methods": [
  "manager_plugins.providers.list",
  "manager_plugins.download.resolve.start",
  "manager_plugins.download.start"
]
```

Each plugin can declare up to 32 providers with unique IDs and labels. The
entrypoint defaults to its main script. `schemes` requires 1–16 lowercase URI
schemes; optional `hosts` contains up to 32 exact lowercase hostnames, without
wildcards or ports. Host restrictions match the input source URI. Registry
lookup is import-free, respects game filters and plugin enablement, and skips
plugins whose boolean `enabled` setting is false. Operating-system link handlers
require the separate `protocols` declaration below; plugins can present links through their own browser,
commands, or settings.

The resolver module exports `resolve_download(context)`:

```python
def resolve_download(context):
    context["check_cancelled"]()
    item = lookup_file(context["source"])
    return {
        "url": item.download_url,
        "file_name": "Example.zip",
        "identity": "project/file/version-1",
        "size_bytes": item.size,
        "sha256": item.sha256,
        "headers": {"Authorization": "Bearer " + access_token()},
        "metadata": {"name": "Example", "version": "1.0", "author": "Example author"}
    }
```

The worker context contains `api_version`, `plugin_id`, `provider_id`,
`plugin_dir`, `settings`, `data`, `game_id`, `game` (display name), `source`,
`log(message)`, and `check_cancelled()`. The latter also waits while a download
job is paused. Modules are unloaded after callbacks. Keep resolver work
cooperative and use bounded timeouts for external calls. The context carries
game identifiers rather than a mutable manager/game object.

Required artifact fields are `file_name` (one supported archive filename, at
most 240 UTF-8 bytes), `identity` (a stable artifact/version ID, 1–1000
characters), and positive integer `size_bytes`. Optional lowercase `sha256`
pins the expected content; the manager computes and records a checksum for
every completed download. `metadata` is a finite JSON object of at most 64 KiB;
its optional `name`, `version`, `author`, and `description` fields must be
strings of at most 4000 characters. Metadata is public and persistent.

The default `transport` is `http`. Its `url` must use HTTP/HTTPS with a hostname
and no embedded credentials. `headers` supports at most 32 valid HTTP headers
(64 KiB combined); use it for authentication when required. URLs, headers, and
optional finite JSON `private` data (up to 1 MiB) stay out of plan responses,
source sidecars, and job options. They are held for the transfer. Avoid logging
credentials or putting them in public metadata or artifact identities.

SDKs and external tools can use `transport: "plugin"`. Declare an additional
plugin-relative `download_entrypoint` Python file and export
`download_archive(context)` from it. The descriptor uses the same artifact
fields and places transfer-specific information in `private`:

```python
def resolve_download(context):
    item = sdk.lookup(context["source"])
    return {"transport": "plugin", "file_name": "Example.zip",
            "identity": item.version_id, "size_bytes": item.size,
            "sha256": item.sha256, "private": {"file_id": item.id}}

def download_archive(context):
    with open(context["destination"], "wb") as output:
        done = 0
        for chunk in sdk.chunks(context["private"]["file_id"]):
            context["check_cancelled"]()
            context["throttle"](len(chunk))
            output.write(chunk)
            done += len(chunk)
            context["report_progress"](done, context["artifact"]["size_bytes"])
```

The transfer context adds the original `source`, a detached public `artifact`,
detached `private` data, the manager-owned temporary `destination`,
`report_progress(done, total=0)`, and `throttle(byte_count)`. Return `None` after
writing a regular archive at `destination`. External tools should write to that
path and cooperate with `check_cancelled()` for pause/cancel; bounded or
cancellable SDK/process calls remain the provider's responsibility. The throttle
helper uses the manager's aggregate bandwidth limit. HTTP transfers apply that
limit automatically. These trusted callbacks share Amethyst's filesystem
permissions; temporary-path checks govern the handoff rather than sandboxing
the plugin.

Use the public API to resolve, review, and start downloads:

| Method | Parameters / behavior |
| --- | --- |
| `manager_plugins.providers.list` | `game`, optional `source`; returns enabled matching declarations and discovery errors |
| `manager_plugins.download.resolve.start` | `game`, `plugin_id`, `provider_id`, `source`; returns a cancellable read job whose result is the public artifact and revision |
| `manager_plugins.download.start` | The same selection plus resolved `revision` and `confirm: true`; returns a normal cancellable/pausable download job |

The script client provides matching helpers. `resolve_download` waits for the
read job; `download_from_provider` submits through the standard `host.apply`
review, and its accepted job must still be waited on:

```python
import json
from amethyst_plugins.client import Client

client = Client()
source = "example://project/file"
plan = client.resolve_download("example", source)
result = client.wait(client.download_from_provider(
    "example", source, plan["revision"],
    summary="Download " + plan["file_name"],
    details=json.dumps(plan, indent=2),
))
client.log(result["archive"]["path"])
```

`client.download_providers(source=None)` lists declarations. The resolve and
download helpers default `plugin_id` to this plugin; pass another enabled
plugin ID to use its registered provider. Persistent Qt contexts can use
`read_async` for the resolution job and run a declared script command for the
reviewed download.

Starting a download resolves the source again and verifies the reviewed
artifact revision. The revision covers the game, input-source fingerprint,
provider declaration, resolver/transfer scripts, settings/data, transport, and
public artifact fields. Transient returned URL, header, and private values are
excluded so signed URLs can refresh. Changed public fields or plugin inputs
require a new review. Raw input URIs are omitted from stored job options.

Both transports use normal jobs, operation locks, and temporary cache storage.
The manager checks the declared size, optional expected checksum, and archive
format before publication. Existing archives are preserved, with a unique
filename allocated for repeats or publication collisions. Failures/cancellation
clean up temporary downloads. Custom transports must cooperate with cancellation;
completed-file validation still belongs to the manager.

Completed archives appear in the game's ordinary Downloads inventory with a
`provider` summary. Their `.plugin.json` sidecar records source identity, size,
actual SHA-256, and public metadata. Download move/remove operations carry or
delete this sidecar. Normal installation rechecks the source checksum before
extraction, uses the optional metadata name as a default folder name, and saves
version/author plus a `[ManagerPluginSource]` section in `meta.ini`. That section
includes JSON `metadata`; read it with interpolation disabled. Source records
remain usable when the provider plugin is subsequently unavailable. Installation
lifecycle callbacks receive the public record as `download_source`.

For installation, locate the returned archive path in `downloads.list`, use its
opaque archive ID with `downloads.inspect.start`, then call
`downloads.install.start` through the normal reviewed flow, passing any plugin
installer revision. Download completion itself only adds the retained archive.
Repository callbacks can integrate update checks, versions, actions and dependency
planning with the manager, as described below.

### Reviewed workflows and bulk downloads

Script plugins can approve a finite workflow once with `client.apply_batch(steps,
title=..., summary=..., details=...)`. Declare each backend method in `methods`;
do not declare the host method itself. Each step is exactly `{"method": ...,
"params": {...}}`. The host displays the workflow and waits for each accepted
job to finish before starting the next step. Reads do not need individual approval.
An approval applies only to this detached workflow, not to later requests.

Use `client.result_ref(step_index, *path)` to select a JSON field from an earlier
step's final result. For example, download an already reviewed artifact and install
the resulting archive with one approval:

```python
artifact = client.resolve_download("files", source)
result = client.apply_batch([
    {"method": "manager_plugins.download.start", "params": {
        "game": client.context["game"], "plugin_id": client.context["plugin_id"],
        "provider_id": "files", "source": source,
        "revision": artifact["revision"], "confirm": True}},
    {"method": "downloads.install.start", "params": {
        **client.target,
        "archive_path": client.result_ref(0, "archive", "path"),
        "installer_policy": "reject"}},
], title="Install mod?", summary="Download and install Example.")
if result is not None and not result["ok"]:
    client.log(result)
```

Workflows support 1–1024 steps, up to 3 MiB, and backward result references with
up to 16 path components. The returned object has `ok`, `state`, indexed `items`
and `remaining`. Failure or cancellation stops later steps; completed changes
remain completed. Normal backend revisions, confirmations and locks still apply.
The workflow is not an atomic transaction or a permission to run future actions.
Stop cancels a supported active job and waits for noncancellable operations.
Installer choices still need explicit selections or an explicit defaults policy.
Workflows reject `interactive:true`, `manual_interactive:true`, and
`nexus.download.manual.start`, including interactive items inside backend batches.
Run those operations individually so the plugin can receive and answer requests.

`manager_plugins.download.resolve.batch.start` and
`manager_plugins.download.batch.start` accept the normal batch `game`, `items`
and optional `on_error`. Items contain the single-item parameters without `game`.
Downloads retain per-item revisions and `confirm:true`; the download batch also
supports pause/resume. Both return ordinary tracked jobs with per-item outcomes.

For a dependency-aware install, declare `manager_plugins.dependencies.get.start`,
`manager_plugins.download.resolve.start`, `manager_plugins.download.start` and
`downloads.install.start`, then use:

```python
plan = client.plan_provider_install([
    {"plugin_id": client.context["plugin_id"], "provider_id": "files", "source": source}
])
result = client.install_provider_plan(plan)
```

Planning is read-only and resolves all artifacts before approval. Execution checks
the dependency graph revision and each artifact revision, then downloads and installs
in dependency-first order. Existing destination collisions use the shared installer's
normal policy; authors can build explicit reuse/enable or replacement steps with
`apply_batch` when required. Host support is advertised separately in
`client.context["host_capabilities"]`.

### Repository callbacks

Add `"repository_entrypoint": "repository.py"` to a download-provider declaration.
The module exports `get_versions(context)` and may export
`get_dependencies(context)` and `perform_action(context)`. It uses the same
plugin/game/settings/data/cancellation/log context as the resolver. `get_versions`
and `perform_action` additionally receive `installed`, the public source record
from the installed mod; `perform_action` also receives `action_id`.

```python
def get_versions(context):
    return {
        "versions": [
            {"id": "project/file/2", "version": "2.0", "source": "example://files/2"}
        ],
        "recommended": "project/file/2",
        "url": "https://example.com/project",
        "actions": [{"id": "endorse", "label": "Endorse"}],
    }

def get_dependencies(context):
    return []

def perform_action(context):
    return api.perform(context["action_id"], context["installed"]["identity"])
```

Catalogue version IDs must match resolver artifact `identity` values; version labels
must match `metadata.version`. A catalogue allows 256 unique versions, an optional
`recommended` ID (or `null`), an HTTP(S) page URL and up to 32 actions. Versions may
also supply a display `label` and `dependencies`. Dependencies are
`{plugin_id, provider_id, source}` requests, including requests for other enabled
providers. `get_dependencies` receives `source` and returns the same request array.
It is optional; omission means no dependencies. Graphs reject cycles and exceedance
of 256 artifacts or 32 levels. Callback results are finite JSON up to 1 MiB.
Callbacks must use bounded requests and cooperate with `check_cancelled()`.

`mods.list` includes the public record under `sources.plugin`. Source identifiers
for version APIs use `plugin:<plugin-id>/<provider-id>`. The standard
`mods.versions.get.start` returns the catalogue, installed-property `revision` and
`catalogue_revision`. `mods.version.change.start` accepts that source, both revisions,
the selected string `version` ID and `confirm:true`; it uses the shared recoverable
replacement and profile-refresh path. Dependencies must already be installed and
enabled before a version change. `manager_plugins.action.start` accepts the same
target/revisions and `action_id`, with `confirm:true`. Repository actions are for
site operations; manager changes belong in reviewed backend calls.

`mods.check_updates.start` includes enabled plugin repositories by default; use
`sources:["plugins"]` to check only them. Results include `plugin_updates` and
per-mod errors. `mods.quick_update.plan.start` and `mods.quick_update.start` accept
`source:"plugins"`, including multiple providers in one plan. Quick Update stops
after a failure and reports completed and remaining items. Per-mod installer options
and download-only execution are supported. Individual replacements retain the shared
rollback behaviour; a multi-mod update is not an all-or-nothing transaction.

The desktop Check Updates button, provider update flags, source filter, update-ignore
properties, **Provider versions and actions** menu and **Quick Update from providers**
menu use these interfaces. A disabled or absent provider leaves installed files and
their recorded identity intact; network operations need the provider enabled.

For portable collections, return a stable, public source URI in
`metadata.source`. Amethyst profile exports and share codes can then record
`source.type:"plugin"`, provider IDs, that URI, artifact identity, SHA-256 and size.
Import verifies the pins and uses the enabled provider to acquire the archive.
Every provider dependency must have its own pinned manifest entry. Collection
downloads/installations participate in normal cancellation, ownership and load order.
Do not put credentials or expiring grants in catalogue URLs, dependency URIs,
`metadata.source`, other public metadata or action results. Use resolver headers or
private transfer data for authentication. Site login and token-refresh UI remain
plugin-owned; the manager does not supply a universal OAuth flow.

### Website protocol links

Add `"protocols":["example"]` to a provider whose `schemes` includes `example`.
These explicit declarations register custom operating-system protocols at startup;
listing a scheme under `schemes` alone remains a resolver filter. HTTP(S), file,
Steam and the manager's built-in protocols are reserved. Existing alternative
associations are retained; obsolete plugin registrations are removed on startup.

Browser links and `--plugin-source example://...` use the existing single-instance
handoff. The selected configured game must have exactly one matching enabled provider.
Ambiguous or unavailable matches are reported. The manager opens a review panel with
the resolved downloads and dependencies before installation. Plugin disablement and
game filters apply to incoming links too.

## Installer providers

Plugins can recognise custom layouts inside supported archives and supply the
file mapping used by the shared installer:

```json
"installers": [
  {
    "id": "custom_layout",
    "label": "Custom package layout",
    "entrypoint": "installer.py",
    "extensions": [".zip", ".7z"],
    "required_files": ["package.json"],
    "priority": 0
  }
]
```

```python
def inspect_install(context):
    context["check_cancelled"]()
    if not recognises_package(context["extract_dir"]):
        return None
    return {
        "files": [
            {"source": "payload/example.dds", "destination": "Textures/example.dds"}
        ]
    }
```

Each plugin can declare up to 32 installers with unique IDs and labels. The
entrypoint defaults to its main script. `extensions` is an optional list of
case-insensitive archive suffixes; `required_files` optionally requires exact
archive-relative filenames, compared case-insensitively. Paths are relative to
the full extracted tree, including wrapper folders. These declarations are
read without importing plugin code. Native FOMOD, BAIN, bundle, and multi-mod
installers take precedence; plugin recognisers run for otherwise plain archives.
Compiled collection replication that explicitly disables installer detection
keeps its authored layout.

Candidates run by descending `priority` (-100 to 100, default 0), then plugin ID
and installer ID. A disabled plugin or one whose boolean `enabled` setting is
false is skipped. `None` declines the archive; the first returned mapping wins.
An exception or malformed mapping aborts preparation and cleans up extraction.
This supports custom layouts within the manager's archive formats; extraction
still uses the existing archive engine.

`inspect_install(context)` runs on the installer worker. Its context contains
`api_version`, `plugin_id`, `installer_id`, `plugin_dir`, `settings`, `data`,
`game_id`, `game`, `profile`, `profile_path`, `archive_path`, `extract_dir`,
`mod_name`, and `files` (`{path, size_bytes}` records). `metadata` contains the
plugin's own namespace when it declares `manager_plugins.metadata.get`, otherwise
an empty object. `log(message)` prefixes messages with the plugin name;
`check_cancelled()` raises when cancellation is requested. Do not create Qt
widgets or write manager state here. Modules are unloaded after recognition.

Return exactly `{"files": [...]}` with one or more `{source, destination}` file
pairs. Both paths must be nonempty relative paths using forward slashes, with
no traversal, drive prefixes, or control characters. Sources must be regular
files from the supplied inventory. Expand folders into individual records;
the inventory and returned mapping are each limited to 200,000 files. Duplicate
destinations, including case-insensitive duplicates and file/directory
collisions, are rejected. Omitted files are excluded. The manager applies the
mapping through its normal copier, then runs game post-install handling,
metadata creation, modlist registration, and indexing. Destination directory
casing follows the game's existing rules.

Preparation records the selected source identities, archive identity, plugin
declaration, script, settings, and data. Installation revalidates them before
replacing an existing mod. Copy cancellation/failure removes a new partial mod
or restores the previous installation. Existing locks, progress, reinstall
handling, and lifecycle hooks remain in charge. Lifecycle contexts identify the
installer as `plugin` and include a `plugin_installer` summary.

`downloads.inspect.start` returns `installer.type: "plugin"`, `plugin_id`,
`installer_id`, `label`, `revision`, `file_count`, `total_bytes`, and the first
100 file pairs (`truncated` indicates more). Pass that revision as
`installer_revision` to `downloads.install.start` to refuse a changed plan. The
revision is stable across repeated extractions of the same archive and plan.
Script plugins should include this summary in their normal `host.apply` preview
and wait for the installation job to finish. Desktop installations use the same
recognition/staging path and report the selected installer in the operation log.

### Desktop installer sessions

Installer declarations may add `interactive: true` and `formats`, a list of
input format identifiers. The default is `["plain"]`; additional recognised
inputs are `scripted_fomod` and `omod`. XML FOMOD, BAIN, bundle and multi-mod
installers keep their existing precedence. These fields do not import code
during discovery.

An interactive provider's `inspect_install(context)` returns
`{"interactive": true, "state": {...}}`. `state` must be a JSON object of at
most 1 MiB. Recognition does not open a window or change manager state. The
manager includes the state in the inspection revision, defers desktop batch
installs until the sequential phase, and reports `requires_desktop: true` with
`supported: false` to Headless clients. Headless and collection jobs do not run
desktop sessions.

At desktop installation time, the manager loads the provider again and calls:

```python
from pathlib import Path


def run_install(context, state):
    output = Path(context["output_dir"])
    output.mkdir()
    (output / "generated.txt").write_text("Generated content")
    return {"files": [{"source": "generated.txt", "destination": "generated.txt"}]}
```

The runtime context includes the usual plugin/game/profile/archive identifiers
and paths, plus `game_handler` (the selected game handler), `work_dir`,
`output_dir`, `cancel` (an event or `None`), `check_cancelled()`, `log(message)`,
and `progress` (a callback or `None`). It contains no Qt widgets or main-window
object. This is a trusted in-process Python API; providers using internal game
handler methods must track their compatibility. External script execution and
its sandbox are the provider's responsibility.

Write generated files under the supplied output directory. Return the same
`{source, destination}` mapping shape as ordinary installer providers. The host
checks containment, file identities, symlinks, duplicate destinations and
file/directory collisions before using the normal staging path. `meta.ini` is
reserved. `state` may hold temporary Python objects after execution begins;
those objects are session-local and are not persisted.

Providers may also export these optional transaction callbacks:

```python
def validate_install(context, state, phase, *, replacing=False):
    pass

def commit_install(context, state):
    pass

def rollback_install(context, state):
    pass
```

Validation phases are `ready` (generated files accepted), `replace` (before
preserving an existing mod), `stage` (before copying) and `commit` (before
applying side effects). `replacing` is supplied during staging so a provider
can distinguish its own replacement from an unexpected external file change.
Commit runs after staging and profile registration. Rollback runs if execution,
staging, commit or final verification fails. Rollback failures are logged while
the manager continues restoring its mod files and profile lists.

The provider must snapshot and restore any additional state it changes, such
as game INIs, and clean up child processes in `finally` blocks. No cancellation
check is injected into rollback. Modules are retained for the transaction,
then unloaded before temporary extraction resources are released. Disabling or
removing the provider prevents subsequent recognition/installation; recognised
scripted packages without a provider are rejected instead of copied as ordinary files.


## Asynchronous Qt reads

Persistent services, custom widgets, file viewers, and modal Qt panels receive
`context["read_async"]`. It calls declared public read methods on a background
worker and delivers callbacks on the GUI thread:

```python
cancel_read = context["read_async"](
    "mods.files.get.start",
    {"mod": "Example Mod", "limit": 500},
    lambda result: show_files(result["files"]),
    on_error=lambda error: show_error(error["message"]),
    on_progress=lambda job: show_progress(job["progress"]),
)
```

The plugin must declare `mods.files.get.start` in `methods`. Submit and cancel
reads on the GUI thread. Pass `None` or an object for parameters; `game` and
`profile` are filled from the captured active target when the method accepts
them. Explicit target overrides must match that target. Metadata reads also
default `plugin_id` to this plugin's ID and reject other namespaces. Parameters
are detached finite JSON and limited to 1 MiB.

For synchronous backend reads, the result callback receives the normal response.
For `.start` reads, the host polls the accepted job to a terminal state and
delivers its final `result`, rather than the submission response. Optional
progress callbacks receive `{id, state, progress}`. Errors use
`{name, message, details}`; omitted error callbacks write to the manager log.
Callback exceptions are logged. Backend schemas reject mutation methods before
execution; writes continue through the existing reviewed operation flow.

The returned function cancels that request and suppresses subsequent callbacks.
Queued work is removed; active cancellable jobs receive `jobs.cancel`.
Synchronous calls and noncancellable jobs may finish in the background. Closing
the owner, switching the target, refreshing a changed/removed plugin, or manager
shutdown cancels its requests and drops stale callbacks. Switching away and back
does not revive a cancelled request. Widgets are recreated when their plugin
declaration changes so callbacks retain the correct methods and lifecycle.

Reads run serially with at most 32 outstanding requests per context and 128
across the manager. Cancel obsolete work before issuing replacement requests.
Normal backend validation, revisions, and operation locks still apply, so a
query can fail as busy while another manager operation is running. File/conflict
queries may refresh backend catalogue caches and read jobs use the usual job
history. For immediate list data, prefer `inventory()` and `selection()`; they
use the manager's detached caches.

## Reviewed asynchronous Qt operations

Qt services, widgets, modals and plugin wizards receive `apply_async` and
`apply_batch_async`. They use the host's existing review, operation lock,
Filegraph release, job tracking, cancellation and refresh flow without running
a plugin script or requiring the plugin to manage a Headless subprocess.

```python
cancel = context["apply_async"](
    "mods.set_enabled.start", {"changes": [{"name": "Example", "enabled": False}]},
    lambda result: show_result(result), title="Disable Example?",
    summary="Disable the selected mod.", on_error=lambda error: show_error(error["message"])
)
```

Declare each backend method in `methods` or a supported `optional_methods`.
`game`, `profile` and metadata `plugin_id` default to the captured target;
explicit overrides must match it. Undeclared methods, managed launches and
interactive workflow steps are rejected. Batch calls accept the same finite
steps and backward result references as `client.apply_batch`; completed writes
remain applied if a later step fails. One Qt operation may be pending at a time.

Both calls accept `title`, `summary`, `details`, `on_error`, and `on_progress`.
Callbacks run on the GUI thread. A single-operation success callback receives
the final backend result; batches receive the workflow result. Declining review
returns `None`. Errors include `message` and the partial `workflow` when
available. Progress includes `step` (zero-based), `total_steps`, `job_id`,
`state`, and `progress`. The returned callback cancels a pending/running request;
disposal cancels automatically and suppresses callbacks. Submit/cancel on the
GUI thread. Busy or stale requests fail instead of silently waiting for a
different target. Host capability `asynchronous_writes` identifies support.

An originating custom modal is hidden during host review and restored afterward;
its manager refresh happens when that modal closes. Closing the originating
modal while a reviewed operation runs waits for accepted work to finish before
releasing the manager lock. Ordinary service/widget
requests refresh through the normal host flow. Contexts invalidated by a target
change or disposal receive no late callbacks. Keep callback work nonblocking.

## Registered commands

Declare reusable actions without duplicating their implementation across toolbar
buttons, context menus, and shortcuts:

```json
"commands": [
  {
    "id": "inspect",
    "label": "Inspect selected mods",
    "entrypoint": "inspect.py",
    "toolbar": true,
    "context_menu": true,
    "locations": ["modlist"],
    "filters": {"item_types": ["mod"], "min_selection": 1}
  }
],
"ui_extensions": [
  {
    "id": "inspect_footer",
    "label": "Inspect",
    "type": "button",
    "location": "modlist_footer",
    "action": "run_command",
    "target": "inspect"
  }
]
```

Commands use unique IDs and labels, with the same limits and selection filters
as context-menu actions. At most 32 commands are allowed per plugin. Discovery
reads declarations without importing code. `locations` defaults to all supported
context-menu locations. `toolbar` and `context_menu` default to false.

The default `action` is `run_script`; `entrypoint` defaults to the plugin's main
script. The manager opens the standard guarded script runner with
`client.context["action_id"] == "command:inspect"` and
`client.context["menu_context"]["command_id"] == "inspect"`. The latter also
contains `location`, `selection`, and `clicked`. Toolbar, keyboard, and mouse
invocations capture the active list's current selection; context menus capture
their clicked/selected items. The command palette captures selection when opened.
Truncated selections are refused. The manager revalidates the game, profile,
manifest, filters, selected list names, and supplied file paths before dispatch.
Writes still require `host.apply` review and the normal backend operation locks.

Alternatively, use `action: "open_plugin"` or `action: "open_tab"` with a `target`
naming a tab declared by this plugin. Persistent Qt services, custom widgets,
and preview contexts can call `context["run_command"]("inspect")`; it queues the
same dispatch and returns whether the command matched and could be queued.
Callbacks from disposed contexts are refused. This helper is unavailable inside
the modal Qt panel, where the plugin already owns the operation session.
Notification actions can reference a command using
`{"type": "run_command", "label": "Inspect", "command_id": "inspect"}`.

Users can find all current-game commands in **Plugins → Commands…**, search by
plugin/action name, run a matching command, and check **Toolbar** to pin it.
Pinning overrides the author's toolbar default and persists in manager UI
settings. A declared toolbar `run_command` button also supplies the default
placement; unpinning hides it. Footer/provider buttons are independent.

**Settings → Shortcuts** lists commands from all enabled plugins, including
plugins for other games, with no default binding. Users can assign keyboard or
mouse bindings and reset an individual command to **Unassigned**. Conflicts with
built-in or other enabled-plugin bindings are rejected. Shortcut IDs use
`plugin/<plugin-id>/<command-id>`; saved bindings survive disabling/uninstalling
a plugin until explicitly reset. Global keyboard/mouse disable switches apply.
Commands run only in their active game, while editing is available; shortcuts
are suppressed during text input, overlays, and manager operations. Plugin
shortcuts also work in detached manager tabs. Holding a key does not repeat a
command.

## Plugin wizards

Qt plugins can contribute first-class Wizard menu entries independently of
their main panel. Entries support categories, favourites and panel-scoped tabs.
Discovery reads declarations without importing their Python modules:

```json
"wizards": [{"id": "setup", "label": "Example setup", "entrypoint": "setup.py",
             "category": "Setup and Installers", "panel": "modlist"}]
```

Up to 32 unique entries are allowed. `entrypoint` defaults to the main plugin
entrypoint; `panel` is `plugins` (default), `modlist`, or `full`. Optional
`description` supplies the menu tooltip. Manifest `game_ids` and manager
enablement apply. Wizard IDs are namespaced as `manager_plugin:<plugin>/<wizard>`.
The module exports `create_wizard(context, parent, on_close)`, returning a child
QWidget with synchronous `dispose()`. Context contains the normal service
callbacks plus `wizard_id` and `log`. Call `on_close()` to close the tab. Closing
the tab or changing target disposes its context and unloads its module. Keep
construction/cleanup nonblocking; use reviewed host operations for mutations.

## Python game handlers

Declare plugin-relative handler modules independently of download providers:

```json
"game_handlers": ["games/example.py"]
```

Each file declares one or more concrete `Games.base_game.BaseGame` subclasses
with zero-argument constructors. Relative imports inside the handler directory
are supported. For games fitting existing deployment behaviour, a handler can
subclass the appropriate existing game base rather than reimplementing it:

```python
from Games.Custom.custom_game import StandardCustomGame

class ExampleGame(StandardCustomGame):
    def __init__(self):
        super().__init__({"name": "Example Game", "game_id": "example_game",
                          "exe_name": "Example.exe", "mod_data_path": "Mods"})
```

Up to 32 unique Python files are allowed. Manifest discovery validates containment
without importing them; actual game discovery loads enabled plugins' handlers.
Names and game IDs must be unique across built-in, custom and plugin games.
Plugins cannot replace another handler. A failing module reports a discovery
failure and does not remove other games. Handler modules remain loaded for their
instances' lifetime. Keep constructors bounded and free of network operations.
Disabling or removing a plugin removes its games from discovery after restart;
saved paths, profiles and mods are retained. JSON `games.custom.*` APIs remain
available for definitions that do not need Python behaviour.

These handler classes use Amethyst's Python game API, which may change between
manager versions. Set `min_manager_version` and validate the packaged plugin
against the manager versions you support. Modules execute as trusted Python with
the manager's privileges, like other executable plugin contributions.

## Current boundaries

- Scripts are trusted local programs with the same filesystem permissions as
  Amethyst. Method declarations and write previews govern the host API; they
  are not a sandbox against direct filesystem access or subprocesses.
- Standard `stdio` plugins cannot start or stop managed game launches yet.
  Custom Qt panels can own a headless launch, but must wait for it to finish
  before calling `on_close`.
- Use the public JSON API for manager operations, not internal game objects.
  Standalone use of the Python client requires a host implementing the
  protocol above.
- Local source checks do not establish packaged or live-game behaviour. Do not
  run a second manager process or external editor against the same profile
  during a plugin operation.
