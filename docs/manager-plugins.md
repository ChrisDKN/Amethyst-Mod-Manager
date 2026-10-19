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
no services, widgets, buttons, menus, scripts, or launch hooks. They remain
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

## Current boundaries

- Scripts are trusted local programs with the same filesystem permissions as
  Amethyst. Method declarations and write previews govern the host API; they
  are not a sandbox against direct filesystem access or subprocesses.
- This version supports manual menu actions, plain-text previews, simple
  settings, custom Qt panels, declared UI extensions, and persistent Qt services.
  Automatic install hooks and plugin installation/update management are not
  implemented.
- Standard `stdio` plugins cannot start or stop managed game launches yet.
  Custom Qt panels can own a headless launch, but must wait for it to finish
  before calling `on_close`.
- Use the public JSON API for manager operations, not internal game objects.
  Standalone use of the Python client requires a host implementing the
  protocol above.
- Local source checks do not establish packaged or live-game behaviour. Do not
  run a second manager process or external editor against the same profile
  during a plugin operation.
