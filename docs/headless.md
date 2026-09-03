# Headless mode

Amethyst headless mode gives scripts and external applications a stable way to
use the manager without importing its Python modules or starting Qt. Protocol
version 1 exposes game, profile and mod state plus asynchronous mod activation,
ordering, removal, deploy, and restore jobs.

The public boundary is JSON-RPC 2.0 over newline-delimited JSON (NDJSON).
Amethyst reads one JSON value per line from standard input and writes one JSON
response per line to standard output. Diagnostics and operation tracebacks go
to standard error, so clients must only parse standard output.

## Starting the service

For a native or package-manager installation:

```bash
amethyst-mod-manager-cli headless --stdio
```

`rpc` is an alias for `headless`:

```bash
amethyst-mod-manager-cli rpc --stdio
```

Run an AppImage directly:

```bash
./AmethystModManager-x86_64.AppImage headless --stdio
```

Run the service from the Flatpak:

```bash
flatpak run --command=amethyst-mod-manager-cli \
  io.github.Amethyst.ModManager headless --stdio
```

Run it from a source checkout:

```bash
PYTHONPATH=src .venv/bin/python src/cli.py headless --stdio
```

The process remains alive until standard input closes. JSON-RPC requests are
handled sequentially, while mutation jobs run in a background worker.
On end-of-input, Amethyst stops accepting requests and waits for every accepted
job to finish before exiting. This prevents a client disconnect from terminating
a mutation halfway through.

For a single request, pipe one line into the service:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"system.ping","params":{}}' \
  | amethyst-mod-manager-cli headless --stdio
```

The older one-shot `deploy`, `restore`, `launch` and listing CLI commands remain
available for people and launcher wrappers. Their human-readable output is not
a versioned integration API.

## Handshake

A client should make `system.handshake` its first request and supply the newest
protocol version it supports:

```json
{"jsonrpc":"2.0","id":1,"method":"system.handshake","params":{"protocol_version":1,"client":{"name":"example","version":"1.0"}}}
```

The response describes the protocol and available features:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocol": {
      "name": "amethyst.headless",
      "version": 1,
      "minimum_version": 1,
      "maximum_version": 1,
      "rpc": "2.0",
      "framing": "ndjson"
    },
    "application": {
      "name": "Amethyst Mod Manager",
      "version": "<installed version>"
    },
    "capabilities": {
      "read_only": false,
      "events": true,
      "event_delivery": "poll",
      "cancellation": false,
      "mutations": ["mods.set_enabled", "mods.move", "mods.remove", "deploy", "restore"],
      "maximum_event_wait_ms": 30000,
      "methods": [
        "system.handshake",
        "system.ping",
        "games.list",
        "games.get",
        "profiles.list",
        "mods.list",
        "mods.set_enabled.start",
        "mods.move.start",
        "mods.remove.start",
        "deploy.start",
        "restore.start",
        "jobs.get",
        "jobs.list",
        "jobs.events"
      ]
    }
  }
}
```

The server rejects an unsupported `protocol_version`. Clients must ignore
unknown fields in successful results so compatible capabilities and methods can
be added without breaking them.

## Methods

All method parameters are named and must be supplied as a JSON object. Unknown
parameters are rejected.

| Method | Parameters | Purpose |
| --- | --- | --- |
| `system.handshake` | `protocol_version` integer (optional), `client` object (optional) | Negotiate and inspect the protocol. |
| `system.ping` | None | Check that the service is responsive. |
| `games.list` | `configured_only` boolean (optional) | List discovered game handlers. |
| `games.get` | `game` string | Return one game and its current state. |
| `profiles.list` | `game` string | List existing profiles and summary counts. |
| `mods.list` | `game` string, `profile` string | List a profile's mods, separators and installed metadata. |
| `mods.set_enabled.start` | `game` string, `profile` string, `changes` array | Start a batch mod enable/disable job. |
| `mods.move.start` | `game` string, `profile` string, `mods` array, `position` object | Move mods as a priority-ordered block. |
| `mods.remove.start` | `game` string, `profile` string, `mods` array, `confirm` boolean | Permanently remove one or more mods. |
| `deploy.start` | `game` string, `profile` string, `options` object (optional) | Start a deployment job. |
| `restore.start` | `game` string, `profile` string | Start a restore job. |
| `jobs.get` | `job_id` string | Get the latest job snapshot. |
| `jobs.list` | `active_only` boolean (optional) | List jobs, newest first. |
| `jobs.events` | `job_id` string, `after` integer (optional), `wait_ms` integer (optional) | Read or long-poll ordered job events. |

The `game` parameter accepts the canonical `game_id`, display name, primary
Steam app ID, or an alternate Steam app ID. Clients should retain the canonical
`id` returned by Amethyst and use it for later requests. If a Steam app ID
belongs to multiple handlers, Amethyst selects it only when exactly one handler
is configured; otherwise it returns `ambiguous_game` with the candidates.

### Reading manager state

List configured games:

```json
{"jsonrpc":"2.0","id":2,"method":"games.list","params":{"configured_only":true}}
```

Each game includes its canonical identity, configuration and deployment state,
paths, store and mod-source identifiers, and available features.
`load_failures` identifies handler modules which could not be discovered.

List profiles:

```json
{"jsonrpc":"2.0","id":3,"method":"profiles.list","params":{"game":"skyrim_se"}}
```

Profiles include mod counts, profile-group membership, profile-specific staging
state, collection association, and selected/deployed state.

List mods:

```json
{"jsonrpc":"2.0","id":4,"method":"mods.list","params":{"game":"skyrim_se","profile":"default"}}
```

Entries use `highest_priority_first` order. `index` is the zero-based row in
`modlist.txt`, including separators. Priority zero is the lowest real mod and
the largest number is the highest; separators have a null priority. Real mods
include enabled and locked state, staging-folder presence, installed metadata,
update state, and Nexus or Thunderstore identity when available. A stale
`modlist.txt` entry remains visible with `folder_exists` set to false.

Read methods return `operation_in_progress` while this service is changing game
state. Continue polling the active job and retry the read after it completes.

## Mutation jobs

Mutating calls return immediately with a job snapshot. They do not hold the
JSON-RPC request open for the filesystem operation.
Only one mutable job may be outstanding in a headless process; another start
request returns `operation_in_progress` until that job reaches a final state.

### Enable and disable mods

Submit all related activation changes in one job. Each mod name must exactly
match the `name` returned by `mods.list`; names are case-sensitive. The batch is
validated before `modlist.txt` is changed, so a missing, locked, duplicate, or
separator entry rejects the complete batch.

```json
{"jsonrpc":"2.0","id":5,"method":"mods.set_enabled.start","params":{"game":"skyrim_se","profile":"default","changes":[{"name":"SkyUI","enabled":true},{"name":"Old textures","enabled":false}]}}
```

The job synchronizes `plugins.txt` and `loadorder.txt`, then reconciles the
profile's Filegraph state. It does not deploy. Start `deploy.start` explicitly
when the new state should be applied to the game folder.

A successful result reports which entries changed and which already had the
requested state:

```json
{
  "ok": true,
  "changed": [{"name": "SkyUI", "enabled": true}],
  "unchanged": [{"name": "Old textures", "enabled": false}],
  "plugins_changed": true,
  "filegraph_reconciled": true,
  "filegraph_generation": 42
}
```

`plugins_changed` reports whether the plugin files needed rewriting;
`filegraph_reconciled` is false when the existing generation was already
current.

Expected job errors are `mod_not_found`, `ambiguous_mod`, `mod_locked`, and
`mod_not_toggleable`. If plugin or Filegraph synchronization fails after the
atomic mod-list write, the job returns `mod_state_sync_failed`; its details say
whether the mod list changed and identify the failed stage. Retrying the same
desired states also retries synchronization.

### Move mods

Move one or more mods as a block before or after an exact entry from
`mods.list`. The anchor may be another mod or a separator. `before` places the
block closer to the top of the highest-priority-first list; `after` places it
closer to the bottom. Moving after a separator therefore places the block at the
start of that separator's group.

```json
{"jsonrpc":"2.0","id":6,"method":"mods.move.start","params":{"game":"skyrim_se","profile":"default","mods":["Patch A","Patch B"],"position":{"after":"Patches_separator"}}}
```

`position` must contain exactly one `before` or `after` key. All names are
case-sensitive. The anchor cannot also be in `mods`, and separators and locked
mods cannot be moved. The complete request is validated before the atomic
mod-list write.

The block follows its current profile order, regardless of the order of names
in the request. This preserves the relative priority of a multi-selection in
the same way as the desktop manager.

```json
{
  "ok": true,
  "changed": true,
  "moved": ["Patch A", "Patch B"],
  "crossed": ["Gameplay overhaul"],
  "position": {"after": "Patches_separator"},
  "filegraph_reconciled": true,
  "filegraph_generation": 43
}
```

`crossed` lists real mods whose priority relationship with the moved block
changed. Expected move errors are `mod_not_found`, `ambiguous_mod`,
`mod_not_movable`, `mod_locked`, `move_anchor_not_found`,
and `ambiguous_move_anchor`. A post-write reconciliation failure uses
`mod_order_sync_failed`; retrying the same move retries reconciliation.

Moving mods does not deploy. Call `deploy.start` explicitly when the new order
should be applied to the game folder.

### Remove mods

Removal is permanent, so the request must include `"confirm": true`. A client
should show its own confirmation UI before sending the request.

```json
{"jsonrpc":"2.0","id":7,"method":"mods.remove.start","params":{"game":"skyrim_se","profile":"default","mods":["Old textures","Superseded patch"],"confirm":true}}
```

Names must exactly match real mod entries returned by `mods.list`. The complete
batch is validated before cleanup begins. A missing, duplicate, separator, or
locked entry rejects the complete batch without deleting anything.

Removal deletes the staging folders, safely undeploys committed files which
still match their staged sources, removes owned plugin entries and Filegraph
records, clears per-mod profile state, and atomically removes the mod-list rows.
A stale mod-list entry whose folder is already absent may also be removed; it is
reported in `missing_folders`.

```json
{
  "ok": true,
  "removed": ["Old textures", "Superseded patch"],
  "missing_folders": [],
  "scope": "profile"
}
```

`scope` describes the storage affected:

- `profile` means the selected profile has its own mod staging folder.
- `shared_staging` means the folder is deleted from the game's shared mod pool;
  other profiles which list it will then have a stale entry.
- `profile_group_and_members` means Amethyst also deletes the owning member
  copy and any other member copy with the same persisted mod identity. A
  group-local mod is removed only from the group. If any affected member is
  profile-locked, the whole request fails with `mod_locked`.

Clients can inspect `is_group`, `group_members`, and `profile_specific_mods`
from `profiles.list` before presenting the confirmation.

Expected errors are `mod_not_found`, `ambiguous_mod`, `mod_not_removable`, and
`mod_locked`. A filesystem or catalog failure uses `mod_removal_failed` and
keeps the selected profile's mod-list rows so the cleanup can be retried. If
the folders were deleted but the final profile-state write failed, the error is
`mod_removal_sync_failed` and includes the affected names.

When the game is deployed, only destinations still proven to be owned by the
removed mods are unlinked. Run `deploy.start` afterward to apply surviving
conflict winners to the game folder.

### Deploy and restore

Start a deployment:

```json
{"jsonrpc":"2.0","id":8,"method":"deploy.start","params":{"game":"skyrim_se","profile":"default","options":{"root_folder_enabled":true,"backup":true}}}
```

Both deploy options default to `true`:

- `root_folder_enabled` deploys the profile's shared `Root_Folder` payload.
- `backup` creates the normal profile backup before deployment.

Start a restore and return the handler to the named profile afterward:

```json
{"jsonrpc":"2.0","id":9,"method":"restore.start","params":{"game":"skyrim_se","profile":"default"}}
```

Restore acts on the last deployed profile so runtime files go back to the
correct overwrite folder. The supplied profile controls which profile Amethyst
loads again when restore finishes.

A successful start result has this shape:

```json
{
  "job": {
    "id": "09b37ca9237540918fe86ff5e359331c",
    "kind": "deploy",
    "state": "queued",
    "game": {"id": "skyrim_se", "name": "Skyrim Special Edition"},
    "profile": "default",
    "options": {"root_folder_enabled": true, "backup": true},
    "created_at": "2026-09-03T12:00:00Z",
    "started_at": null,
    "finished_at": null,
    "progress": null,
    "result": null,
    "error": null,
    "event_sequence": 1
  }
}
```

Use `jobs.get` for the current snapshot or `jobs.list` to recover job IDs while
the same headless process remains alive:

```json
{"jsonrpc":"2.0","id":10,"method":"jobs.get","params":{"job_id":"09b37ca9237540918fe86ff5e359331c"}}
```

Job states are `queued`, `running`, `succeeded`, and `failed`.

### Polling events

Events carry logs, progress and state transitions. Pass the last processed
sequence as `after`. `wait_ms` enables a long poll and may be from 0 to 30000:

```json
{"jsonrpc":"2.0","id":11,"method":"jobs.events","params":{"job_id":"09b37ca9237540918fe86ff5e359331c","after":0,"wait_ms":1000}}
```

```json
{
  "job_id": "09b37ca9237540918fe86ff5e359331c",
  "events": [
    {"sequence": 1, "timestamp": "2026-09-03T12:00:00Z", "type": "state", "state": "queued"},
    {"sequence": 2, "timestamp": "2026-09-03T12:00:00Z", "type": "state", "state": "running"},
    {"sequence": 3, "timestamp": "2026-09-03T12:00:01Z", "type": "log", "message": "Incremental deploy: existing deployment reused - skipping restore."},
    {"sequence": 4, "timestamp": "2026-09-03T12:00:01Z", "type": "progress", "done": 10, "total": 100, "phase": "Deploying files"}
  ],
  "next_sequence": 4,
  "oldest_sequence": 1,
  "truncated": false,
  "complete": false
}
```

Use `next_sequence` in the next request. Event history is bounded; if a slow
consumer falls behind, `truncated` becomes true and the first returned event's
sequence equals `oldest_sequence`. Always use `jobs.get` after `complete` becomes
true to read the authoritative final `result` or structured `error`.
Progress events are rate-limited during file-heavy operations; the job snapshot
always retains the latest progress value.

Event polling occupies the sequential request loop for up to `wait_ms`, so a
client that also sends interactive requests should use short waits.

### Locking and failures

Headless mod activation, ordering and removal jobs and the standard Amethyst
Deploy and Restore entry points—desktop, one-shot CLI, and headless—use the
same operation coordinator.
Operations are serialized
within one process. Across native, AppImage and Flatpak processes, advisory
locks cover the game identity and resolved game, profile, staging and deployment
paths.

If another process already holds a relevant lock, the accepted job finishes as
`failed` with `error.name` set to `operation_busy`. Its error details identify
the holder when metadata was available. Lock files persist between runs; their
existence does not mean an operation is active because the kernel lock is the
authority.

Other final job error names are `deploy_failed`, `restore_failed`,
`resource_unavailable`, and `internal_error`. Human-readable pipeline details
remain available in the job's `log` events.

Mutation jobs are not cancellable in protocol version 1. Once accepted, allow
the job to reach a final state. This avoids abandoning a partially updated,
restored, or deployed filesystem.

Headless deployment uses the same non-interactive policy as the one-shot CLI:
there is no UI confirmation dialog. Configure and test unusual game-specific
deployment requirements in the desktop application first.

## Errors

Request validation and lookup failures use the JSON-RPC error object.
`error.data.name` is the stable machine-readable Amethyst error name:

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "error": {
    "code": -32001,
    "message": "Game 'missing' was not found.",
    "data": {
      "name": "game_not_found",
      "details": {"game": "missing"}
    }
  }
}
```

| Numeric code | Name | Meaning |
| --- | --- | --- |
| `-32700` | `parse_error` | The input line is not valid JSON. |
| `-32600` | `invalid_request` | The JSON-RPC request envelope is invalid. |
| `-32601` | `method_not_found` | The requested method is unavailable. |
| `-32602` | `invalid_params` | Required, unknown or incorrectly typed parameters were supplied. |
| `-32603` | `internal_error` | An unexpected request failure occurred; inspect standard error. |
| `-32001` | `game_not_found` | No game matched the supplied identifier. |
| `-32002` | `profile_not_found` | The requested profile does not exist. |
| `-32003` | `resource_unavailable` | Amethyst could not read a required local resource. |
| `-32004` | `ambiguous_game` | An identifier matched multiple game handlers. |
| `-32005` | `ambiguous_profile` | A case-insensitive name matched multiple profiles. |
| `-32006` | `job_not_found` | The requested job is unknown or has expired from bounded history. |
| `-32007` | `operation_in_progress` | A local mutation is in progress; retry the state request later. |
| `-32008` | `game_not_configured` | The selected game has no usable configured path. |
| `-32009` | `operation_not_supported` | The selected handler cannot perform the requested operation. |
| `-32010` | `unsupported_protocol_version` | The requested protocol version is incompatible. |

An operation which starts successfully can still fail later. That failure is
stored on the job rather than returned as a JSON-RPC error.

## Python client example

```python
import json
import subprocess


class AmethystClient:
    def __init__(self, command):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.request_id = 0

    def call(self, method, params=None):
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    def wait_job(self, job_id):
        sequence = 0
        while True:
            page = self.call("jobs.events", {
                "job_id": job_id,
                "after": sequence,
                "wait_ms": 1000,
            })
            sequence = page["next_sequence"]
            for event in page["events"]:
                if event["type"] == "log":
                    print(event["message"])
            if page["complete"]:
                return self.call("jobs.get", {"job_id": job_id})["job"]

    def close(self):
        self.process.stdin.close()
        self.process.wait()


client = AmethystClient([
    "amethyst-mod-manager-cli", "headless", "--stdio"
])
try:
    client.call("system.handshake", {"protocol_version": 1})
    started = client.call("deploy.start", {
        "game": "skyrim_se",
        "profile": "default",
    })
    job = client.wait_job(started["job"]["id"])
    if job["state"] != "succeeded":
        raise RuntimeError(job["error"])
finally:
    client.close()
```

The example inherits the parent's standard error stream. A long-running client
which captures standard error must drain it continuously to prevent the child
process from blocking on a full pipe.

## Batches and notifications

JSON-RPC batch arrays are accepted as one NDJSON line and their responses are
returned as one array on one line. Requests without an `id` are notifications
and do not receive a response. A batch containing only notifications produces
no output line. Use request IDs for `mods.set_enabled.start`, `mods.move.start`,
`mods.remove.start`, `deploy.start`, and `restore.start`; otherwise the caller
does not directly receive the created job ID.

## Current limitations

- Nexus browsing, downloads, installation, and launch are not exposed yet.
- Progress delivery is polling-only and running jobs cannot be cancelled.
- The shared operation lock currently coordinates headless mod activation,
  ordering and removal jobs and the standard deploy/restore flows, not every
  desktop mod-list edit, installer, wizard, or configuration action. Do not run
  those tools while a headless mutation is active.
- The transport is a child process over standard I/O; there is no socket daemon
  or remote authentication layer yet.
- Python modules under `amethyst_backend`, `Utils`, `Games` and `Nexus` remain
  implementation details. External applications should use the JSON protocol.

The service has the same filesystem permissions as the user who starts it.
Only pass requests from trusted local applications.
