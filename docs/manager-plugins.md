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
`plugin_settings/<id>.json` in Amethyst's config directory.

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
| `host.context` | None | `api_version`, `game`, `profile`, `plugin_id`, saved `settings`, backend `capabilities` |
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

## Current boundaries

- Scripts are trusted local programs with the same filesystem permissions as
  Amethyst. Method declarations and write previews govern the host API; they
  are not a sandbox against direct filesystem access or subprocesses.
- This version supports manual menu actions, plain-text previews, simple
  settings, and custom Qt panels. Automatic install/profile hooks and plugin
  installation/update management are not implemented.
- Standard `stdio` plugins cannot start or stop managed game launches yet.
  Custom Qt panels can own a headless launch, but must wait for it to finish
  before calling `on_close`.
- Use the public JSON API for manager operations, not internal game objects.
  Standalone use of the Python client requires a host implementing the
  protocol above.
- Local source checks do not establish packaged or live-game behaviour. Do not
  run a second manager process or external editor against the same profile
  during a plugin operation.
