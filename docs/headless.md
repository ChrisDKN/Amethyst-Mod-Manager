# Headless mode

Amethyst headless mode gives scripts and external applications a stable way to
read Amethyst state without importing its Python modules or starting Qt. It is
currently read-only. Installing, changing mod state, deploying and restoring
will be added after those operations share an interprocess lock with the
desktop application.

The public boundary is a versioned JSON-RPC 2.0 protocol. Amethyst reads one
JSON value per line from standard input and writes one JSON response per line
to standard output. Startup diagnostics and errors are written to standard
error, so clients must only parse standard output.

## Starting the service

For a native or package-manager installation:

```bash
amethyst-mod-manager-cli headless --stdio
```

The `rpc` command is an alias for `headless`:

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

The process remains alive until its standard input is closed. Requests are
handled sequentially in this first protocol version.

The older one-shot `deploy`, `restore`, `launch` and listing CLI commands still
exist for people and launcher wrappers. Their human-readable output is not a
versioned integration API; applications should use headless mode.

For a single shell request, pipe one line into the service:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"system.ping","params":{}}' \
  | amethyst-mod-manager-cli headless --stdio
```

## Handshake

A client should make `system.handshake` its first request and supply the newest
protocol version it supports:

```json
{"jsonrpc":"2.0","id":1,"method":"system.handshake","params":{"protocol_version":1,"client":{"name":"example","version":"1.0"}}}
```

The response describes the protocol, application and currently available
features:

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
      "read_only": true,
      "events": false,
      "cancellation": false,
      "methods": [
        "system.handshake",
        "system.ping",
        "games.list",
        "games.get",
        "profiles.list",
        "mods.list"
      ]
    }
  }
}
```

The server rejects an unsupported `protocol_version`. Clients must ignore
unknown fields in successful results so compatible fields can be added without
breaking them.

## Methods

All method parameters are named and must be supplied as a JSON object. Unknown
parameters are rejected to catch integration mistakes early.

| Method | Parameters | Purpose |
| --- | --- | --- |
| `system.handshake` | `protocol_version` integer (optional), `client` object (optional) | Negotiate and inspect the protocol. |
| `system.ping` | None | Check that the service is responsive. |
| `games.list` | `configured_only` boolean (optional) | List discovered game handlers. |
| `games.get` | `game` string | Return one game and its current state. |
| `profiles.list` | `game` string | List existing profiles and summary counts. |
| `mods.list` | `game` string, `profile` string | List a profile's mods, separators and installed metadata. |

The `game` parameter accepts the canonical `game_id`, display name, primary
Steam app ID, or an alternate Steam app ID. Clients should retain the
canonical `id` returned in the result and use that for later requests.
If a Steam app ID belongs to multiple handlers, Amethyst selects it only when
exactly one of those handlers is configured; otherwise it returns
`ambiguous_game` with the candidates.

### List games

```json
{"jsonrpc":"2.0","id":2,"method":"games.list","params":{"configured_only":true}}
```

Each game includes:

- Canonical `id` and display `name`
- Configuration and deployment state
- Game and profile paths
- Primary and alternate Steam app IDs
- Nexus and Thunderstore identifiers
- Available game features

`load_failures` reports any game handlers which failed during discovery.

### Get a game by Steam app ID

```json
{"jsonrpc":"2.0","id":3,"method":"games.get","params":{"game":"489830"}}
```

This is useful for launchers which know the currently selected Steam title but
do not know Amethyst's game ID.

### List profiles

```json
{"jsonrpc":"2.0","id":4,"method":"profiles.list","params":{"game":"skyrim_se"}}
```

Only profile directories which currently exist are returned. The response
includes mod counts, profile-group membership, profile-specific staging state,
collection association, and whether the profile is selected or deployed.

### List mods

```json
{"jsonrpc":"2.0","id":5,"method":"mods.list","params":{"game":"skyrim_se","profile":"default"}}
```

Entries are returned in `highest_priority_first` order. `index` is the
zero-based row in `modlist.txt`, including separators. `priority` matches the
desktop application: priority zero is the lowest real mod and the largest
number is the highest. Separators have a null priority.

Each real mod includes its enabled and locked state, staging-folder presence,
version, install time, update state, and available Nexus or Thunderstore
identity. A stale `modlist.txt` entry remains in the response with
`folder_exists` set to false.

## Errors

Errors use the JSON-RPC error object. `error.code` is numeric for JSON-RPC
compatibility; `error.data.name` is the stable, machine-readable Amethyst error
name.

```json
{
  "jsonrpc": "2.0",
  "id": 6,
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
| `-32603` | `internal_error` | An unexpected operation failure occurred; inspect standard error. |
| `-32001` | `game_not_found` | No game matched the supplied identifier. |
| `-32002` | `profile_not_found` | The requested profile does not exist. |
| `-32003` | `resource_unavailable` | Amethyst could not read a required local resource. |
| `-32004` | `ambiguous_game` | An identifier matched multiple game handlers. |
| `-32005` | `ambiguous_profile` | A case-insensitive name matched multiple profiles. |
| `-32010` | `unsupported_protocol_version` | The requested protocol version is incompatible. |

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

    def close(self):
        self.process.stdin.close()
        self.process.wait()


client = AmethystClient([
    "amethyst-mod-manager-cli", "headless", "--stdio"
])
try:
    client.call("system.handshake", {"protocol_version": 1})
    games = client.call("games.list", {"configured_only": True})
    print(games)
finally:
    client.close()
```

The example inherits the parent's standard error stream. A long-running
application which captures standard error instead must drain it continuously
to prevent the child process from blocking on a full pipe.

## Batches and notifications

JSON-RPC batch arrays are accepted as one NDJSON line. Their responses are
returned as one array on one line. Requests without an `id` are notifications
and do not receive a response. A batch containing only notifications likewise
produces no output line.

## Current limitations

- All methods are read-only.
- Requests run sequentially and there are no progress events yet.
- Nexus browsing, downloads, installation, deployment and restoration are not
  exposed yet.
- Closing standard input is the graceful way to stop the service.
- Python modules under `amethyst_backend`, `Utils`, `Games` and `Nexus` are
  implementation details. External applications should use the JSON protocol
  instead of importing them.
