# Manager plugins

Installed plugins appear under **Wizards → Plugins** and run on demand for
the selected game and profile. The host provides
saved settings, a review screen, logs, backend job tracking, and a manager
refresh after changes. The manager stays unavailable for editing until the
plugin window closes; queued installs resume afterward.
Before starting a plugin, the host releases its cached Filegraph sessions so
the plugin's headless backend can open the library. Desktop conflict builds
remain blocked until the plugin closes. Existing immutable snapshots can
still be displayed; the next desktop build opens a fresh session.

## Install a plugin

Choose **Wizards → Plugins → Open Plugins Folder…**. Create a subfolder containing
`plugin.json` and a Python entrypoint. The usual location is:

```text
~/.config/AmethystModManager/Plugins/my_plugin/
    plugin.json
    main.py
```

The path follows Amethyst's config directory, including `XDG_CONFIG_HOME` and
Flatpak's config location. Reopen the Wizards menu to discover changes; discovery
reads manifests without importing the entrypoint. Invalid manifests are reported
in the submenu. Plugin IDs must be unique.

The previous `Plugins/Headless/<plugin>/` layout is still supported. To migrate,
move the plugin folder directly into `Plugins/`; saved settings remain intact.
If both locations contain the same plugin ID, the direct `Plugins/` copy takes
precedence and the duplicate is reported in the submenu.

```json
{
  "api_version": 1,
  "id": "example.profile_report",
  "name": "Profile Report",
  "description": "Count the installed and enabled mods in this profile.",
  "entrypoint": "main.py",
  "methods": ["mods.list"],
  "settings": [
    {"key": "show_names", "label": "List mod names", "type": "boolean", "default": false}
  ]
}
```

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
These add controls beside Settings, detachable tabs, or fixed panels to the
right of or below the main lists on the **Mods** page. Existing plugins need
no changes. For example, a shortcut to a plugin's existing panel is:

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
`modlist_footer`, `tab`, `right_panel`, or `bottom_panel`). IDs follow the same
format as plugin IDs and must be unique within the manifest. At most 32
extensions are supported.
Optional icons are paths inside the plugin folder; missing or unreadable toolbar
icons use the generic wizard icon. Footer buttons use text alone unless a valid
icon is supplied. Button declarations do not import the entrypoint.

Buttons use `location: toolbar` or `location: modlist_footer`. Their `action`
defaults to `open_plugin` and opens their own plugin through the usual busy
checks, modal locking, and refresh flow. To open a declared tab instead, use
`action: open_tab` and set
`target` to that tab's extension ID. These shortcuts work for both `stdio` and
`qt` plugins.

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
Tabs open on demand under **Wizards → Plugins → Interface elements**, and use
the existing detach and panel-pinning controls. Fixed panels appear automatically
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
session and appear in the interface menu diagnostics and application log.

Manifest discovery remains import-free. Startup and opening the Plugins menu
reconcile declarations; visible toolbar widgets and fixed panels then load the
Qt entrypoint, and tabs load it on first opening. Modules stay alive until their
widgets are destroyed. Restart Amethyst after changing already-loaded Python
code; live code replacement is not supported. Persistent widgets use their own
module namespace, separate from each modal `create_view` session. As with Qt
panels, install only plugins you trust.

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
session, logged, and reported under **Interface elements**. Restart after editing
loaded service code. Existing plugins need no changes.

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
