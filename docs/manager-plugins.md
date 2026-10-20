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

## Install a plugin

Choose **Plugins → Browse plugins…** to open the plugin store. It
lists plugins from your enabled GitHub repositories, plus your installed plugins.
The built-in repository is `ChrisDKN/Amethyst-Mod-Manager`, branch `Resources`,
folder `Plugins/v2`. You can browse without selecting a game.
The store shows each plugin's author, description, available and installed
versions, and minimum supported Amethyst version. Search matches names,
authors, and descriptions.

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
no services, widgets, buttons, menus, scripts, launch hooks, or icon overrides. They remain
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
Omit it, or use an empty array, for all games. Settings support `boolean` and
`text`, with a matching typed `default`. They are saved per plugin under
`settings.json` beside `plugin.json`.

## Plugin settings in the browser

Installed plugins with a nonempty `settings` declaration have a **Settings…**
button in the plugin browser. The manager builds the form from the manifest
without importing or running plugin code. This works for `stdio`, `qt`, and
`icons` plugins, including disabled plugins, and does not require a selected
game. The form uses the installed manifest when a store update is available.

Settings support boolean switches and text fields. **Save** writes them to
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
requests are limited to 4 MiB. The Python client implements this exchange.

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
`providers`, `tab`, `right_panel`, `bottom_panel`, or one of the footer locations below).
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

## Current boundaries

- Scripts are trusted local programs with the same filesystem permissions as
  Amethyst. Method declarations and write previews govern the host API; they
  are not a sandbox against direct filesystem access or subprocesses.
- This version supports manual menu actions, plain-text previews, simple
  settings, custom Qt panels, declared UI extensions, and persistent Qt services.
  It also supports lifecycle validation/observation hooks and the plugin store's
  queued installation/update management.
- Standard `stdio` plugins cannot start or stop managed game launches yet.
  Custom Qt panels can own a headless launch, but must wait for it to finish
  before calling `on_close`.
- Use the public JSON API for manager operations, not internal game objects.
  Standalone use of the Python client requires a host implementing the
  protocol above.
- Local source checks do not establish packaged or live-game behaviour. Do not
  run a second manager process or external editor against the same profile
  during a plugin operation.
