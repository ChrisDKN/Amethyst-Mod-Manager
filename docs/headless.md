# Headless mode

Amethyst headless mode gives scripts and external applications a stable way to
use the manager without importing its Python modules or starting Qt. Protocol
version 1 exposes game, profile, mod and downloaded-archive state, Nexus OAuth,
catalogue discovery, exact-ID metadata, managed downloads, and non-interactive
archive installation, plus asynchronous mod activation, ordering, removal,
plugin inventory, activation, ordering and LOOT sorting, profile lifecycle,
managed Overwrite and Root Folder inventory and file management, deploy,
restore, and managed game launch.

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
handled sequentially, while filesystem and network jobs run in a background
worker.
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
      "cancellation": true,
      "cancellable_jobs": ["nexus.download"],
      "mutations": ["profiles.create", "profiles.rename", "profiles.remove", "profiles.select", "mods.set_enabled", "mods.move", "mods.remove", "plugins.set_enabled", "plugins.move", "plugins.sort", "boundaries.move", "boundaries.remove", "deploy", "restore", "nexus.auth.login", "nexus.auth.logout", "nexus.download", "downloads.install", "launch", "launch.stop"],
      "installation": {
        "source": "download_inventory",
        "installer_policies": ["reject", "defaults"],
        "existing_mod_policies": ["fail", "replace"],
        "interactive": false
      },
      "nexus": {
        "authentication": "oauth_pkce",
        "browser_open_optional": true,
        "manual_code_fallback": true,
        "credentials_exposed": false,
        "download_modes": ["premium", "nxm"],
        "catalogue": {
          "query_modes": ["browse", "name", "author", "uploader_id", "trending"],
          "sorts": ["downloads", "endorsements", "created", "updated"],
          "maximum_page_size": 50,
          "filters": ["tag_includes", "tag_excludes", "languages", "hide_translations", "min_file_size_kb", "max_file_size_kb", "min_downloads", "max_downloads", "min_endorsements", "max_endorsements", "adult", "supports_vortex", "has_updated", "title_contains", "description_contains", "author_contains", "uploader_contains"]
        }
      },
      "launch": {
        "requires_deployed_profile": true,
        "deploy_automatically": false,
        "lifecycle_polling": true,
        "stop": true,
        "arbitrary_commands": false
      },
      "plugins": {
        "inventory": "job",
        "order": "first_loaded_first",
        "activation": true,
        "constrained_block_move": true,
        "automatic_sort": true,
        "sorter": "loot",
        "metadata_refresh": true
      },
      "boundaries": {
        "inventory": "job",
        "types": ["overwrite", "root_folder"],
        "default_page_size": 500,
        "maximum_page_size": 5000,
        "revisioned": true,
        "move_to_mod": true,
        "create_mod": true,
        "remove": true,
        "mutation_requires_revision": true
      },
      "maximum_event_wait_ms": 30000,
      "methods": [
        "system.handshake",
        "system.ping",
        "games.list",
        "games.get",
        "profiles.list",
        "profiles.create.start",
        "profiles.rename.start",
        "profiles.remove.start",
        "profiles.select.start",
        "downloads.list",
        "downloads.install.start",
        "nexus.auth.get",
        "nexus.auth.login.start",
        "nexus.auth.login.submit_code",
        "nexus.auth.login.cancel",
        "nexus.auth.logout.start",
        "nexus.user.get.start",
        "nexus.mods.filters.start",
        "nexus.mods.search.start",
        "nexus.mods.get.start",
        "nexus.mods.files.start",
        "nexus.files.get.start",
        "nexus.download.start",
        "mods.list",
        "mods.set_enabled.start",
        "mods.move.start",
        "mods.remove.start",
        "plugins.list.start",
        "plugins.set_enabled.start",
        "plugins.move.start",
        "plugins.sort.start",
        "boundaries.list.start",
        "boundaries.move.start",
        "boundaries.remove.start",
        "deploy.start",
        "restore.start",
        "launch.get",
        "launch.start",
        "launch.stop",
        "jobs.get",
        "jobs.list",
        "jobs.events",
        "jobs.cancel"
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
| `profiles.create.start` | `game` string, `name` string, `profile_specific_mods` boolean (optional), `copy_from` string (optional) | Create or copy a profile. |
| `profiles.rename.start` | `game` string, `profile` string, `new_name` string | Rename a profile. |
| `profiles.remove.start` | `game` string, `profile` string, `confirm` boolean | Permanently remove a profile. |
| `profiles.select.start` | `game` string, `profile` string | Persist and load the active profile. |
| `downloads.list` | `game` string, `profile` string | List downloaded mod archives and configured locations. |
| `downloads.install.start` | `game` string, `profile` string, `archive_id` string, `name` string (optional), `installer_policy` string (optional), `if_exists` string (optional) | Install one archive returned by `downloads.list`. |
| `nexus.auth.get` | None | Inspect saved-credential and active-login state without exposing credentials. |
| `nexus.auth.login.start` | `open_browser` boolean (optional) | Start a Nexus OAuth login flow and return its authorisation URL. |
| `nexus.auth.login.submit_code` | `code` string | Submit the manual fallback code for the active login. |
| `nexus.auth.login.cancel` | None | Cancel the active login flow. |
| `nexus.auth.logout.start` | `confirm` boolean | Clear saved Nexus OAuth and legacy API-key credentials. |
| `nexus.user.get.start` | `refresh` boolean (optional) | Validate the login and return sanitized account information. |
| `nexus.mods.filters.start` | `game` string, `domain` string (optional) | Fetch category and tag facets for Nexus catalogue queries. |
| `nexus.mods.search.start` | `game` string plus query options | Browse or search the paged Nexus mod catalogue. |
| `nexus.mods.get.start` | `game` string, `mod_id` integer, `domain` string (optional) | Fetch one Nexus mod by exact ID. |
| `nexus.mods.files.start` | `game` string, `mod_id` integer, `domain` string (optional) | Fetch the files and update chain for one Nexus mod. |
| `nexus.files.get.start` | `game` string, `mod_id` integer, `file_id` integer, `domain` string (optional) | Fetch one exact Nexus file. |
| `nexus.download.start` | `game` string plus either exact IDs or `nxm_url` string | Download one Nexus archive into Amethyst's managed game cache. |
| `mods.list` | `game` string, `profile` string | List a profile's mods, separators and installed metadata. |
| `mods.set_enabled.start` | `game` string, `profile` string, `changes` array | Start a batch mod enable/disable job. |
| `mods.move.start` | `game` string, `profile` string, `mods` array, `position` object | Move mods as a priority-ordered block. |
| `mods.remove.start` | `game` string, `profile` string, `mods` array, `confirm` boolean | Permanently remove one or more mods. |
| `plugins.list.start` | `game` string, `profile` string | Resolve a profile's ordered plugin inventory and metadata. |
| `plugins.set_enabled.start` | `game` string, `profile` string, `changes` array | Start a batch plugin enable/disable job. |
| `plugins.move.start` | `game` string, `profile` string, `plugins` array, `position` object | Move a contiguous plugin block within engine constraints. |
| `plugins.sort.start` | `game` string, `profile` string, `apply_order` boolean (optional) | Run LOOT, refresh metadata, and optionally apply its constrained order. |
| `boundaries.list.start` | `game` string, `profile` string, `boundary` string, `offset` integer (optional), `limit` integer (optional) | Scan the profile's managed Overwrite or Root Folder inventory. |
| `boundaries.move.start` | `game` string, `profile` string, `boundary` string, `paths` array, `revision` string, `target_mod` string, `create_mod` boolean (optional), `confirm` boolean | Move captured entries into a compatible existing or new mod. |
| `boundaries.remove.start` | `game` string, `profile` string, `boundary` string, `paths` array, `revision` string, `confirm` boolean | Permanently remove captured entries. |
| `deploy.start` | `game` string, `profile` string, `options` object (optional) | Start a deployment job. |
| `restore.start` | `game` string, `profile` string | Start a restore job. |
| `launch.get` | `launch_id` string (optional) | Inspect the latest managed game-launch lifecycle. |
| `launch.start` | `game` string, `profile` string | Launch the currently deployed profile through Amethyst's configured router. |
| `launch.stop` | `launch_id` string, `confirm` boolean | Stop a process tree started by the managed launch. |
| `jobs.get` | `job_id` string | Get the latest job snapshot. |
| `jobs.list` | `active_only` boolean (optional) | List jobs, newest first. |
| `jobs.events` | `job_id` string, `after` integer (optional), `wait_ms` integer (optional) | Read or long-poll ordered job events. |
| `jobs.cancel` | `job_id` string | Request cancellation of a job whose snapshot says `cancellable: true`. |

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
`features.plugin_sort` is true only when the handler supports LOOT and the
installed native LOOT binding is available.

List profiles:

```json
{"jsonrpc":"2.0","id":3,"method":"profiles.list","params":{"game":"skyrim_se"}}
```

Profiles include mod counts, profile-group membership, profile-specific staging
state, collection association, selected/deployed state, and `locked` and
`original_default` removal-protection flags.

List downloaded archives for a profile:

```json
{"jsonrpc":"2.0","id":"downloads-list","method":"downloads.list","params":{"game":"skyrim_se","profile":"default"}}
```

The response scans the same enabled locations as the desktop Downloads tab:
the system Downloads folder, the selected game's cache, and configured extra
folders. Each location includes an opaque `id`, `kind`, path, archive count,
and byte total. Empty existing enabled locations remain in the result;
unavailable extra paths are omitted by the shared scanner.

Each archive includes an opaque path-derived `id`, name, absolute path,
location, extension, byte size, UTC `downloaded_at` timestamp, raw modified
time, symlink state, and whether that archive is already installed in the
requested profile. When the filename or Amethyst's sidecars identify a Nexus
download, `nexus` contains the available `domain`, `mod_id`, and `file_id`;
fields may be null when they cannot be recovered reliably. Downloads made
through the headless or desktop Nexus downloader persist all three values.
Zero-byte browser placeholders and non-archive files are omitted.

Archive ordering matches the desktop manager: the system Downloads folder is
newest first, while the game cache and extra locations are alphabetical. The
top-level `counts` object reports location, archive, installed, not-installed,
and total-byte counts. Archive and location IDs are opaque and should be
refreshed after files move or download-location settings change.

### Installing a downloaded archive

Use the opaque archive ID from `downloads.list`; callers do not submit an
arbitrary filesystem path:

```json
{"jsonrpc":"2.0","id":"install","method":"downloads.install.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>"}}
```

The default policies are deliberately conservative:

- `if_exists` defaults to `fail`. Set it to `replace` only when replacing the
  same-named staged mod is intended. Replacement keeps its modlist position and
  enabled state where the existing installer supports that carry-over. In a
  shared-staging setup, replacing the folder changes it for every profile that
  uses that shared mod.
- `installer_policy` defaults to `reject`. A FOMOD with choices, BAIN package,
  or RE Engine bundle finishes with `installer_choices_required` without
  staging files. Set the policy to `defaults` to explicitly use the installer's
  recommended/default selections.
- An optional `name` selects the destination mod-folder name. It must be one
  safe path component. Without it, Amethyst applies its normal Nexus metadata,
  archive-name, and game-specific naming rules.

For example, explicitly replace a mod while accepting installer defaults:

```json
{"jsonrpc":"2.0","id":"install-defaults","method":"downloads.install.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>","name":"SkyUI","installer_policy":"defaults","if_exists":"replace"}}
```

The job uses the normal toolkit-neutral extraction, structure detection,
staging, metadata, filegraph, modlist, and plugin registration pipeline. The
source archive is retained. Managed Nexus identity sidecars preserve the exact
domain, mod ID, and file ID in the installed `meta.ini`, including offline
installs. Symlink archives and unsafe destinations are rejected.

A successful result contains the retained archive, installed mod name/path and
enabled state, installer type/policy, and whether an existing mod was replaced.
Progress covers archive lookup, extraction, file copying, and indexing. Install
jobs are not cancellable once accepted because cancellation during staging could
leave profile state partially updated.

Install directly into a real profile. Profile groups require an ownership
choice, so the method rejects a group and returns its member names; call it
again with the intended member profile. Scripted or unparseable FOMODs and
multi-mod archives currently finish with `installer_unsupported`. When a plain
archive has an unrecognised structure, the non-interactive installer follows
its existing fallback and installs the layout as-is.

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

### Inspect captured files

Overwrite and Root Folder can contain thousands of files produced by games and
modding tools, so their recursive inventory runs as a job. Scan Overwrite with:

```json
{"jsonrpc":"2.0","id":"overwrite-list","method":"boundaries.list.start","params":{"game":"skyrim_se","profile":"default","boundary":"overwrite"}}
```

Use `"boundary":"root_folder"` for files captured relative to the game root.
The manager resolves both folders from the requested profile; clients cannot
submit a filesystem root. `scope` is `profile` for profile-specific staging and
`shared` when the folder is shared by the game's normal profiles. The result
also reports the resolved boundary and mod-staging paths.

`entries` is a case-insensitively sorted flat list. Each entry has a relative
`path`, `kind` (`file`, `directory`, or `symlink`), nullable regular-file
`size`, UTC `modified_at`, and an `empty` flag for directories. Manager
bookkeeping such as `meta.ini`, `.mm_overwrite_log.txt`, and
`.mm_merge_inventory.xml`, plus macOS archive junk, is omitted. Symlink targets
are not exposed or followed. Top-level counts describe the complete visible
inventory, and `bytes` counts regular-file content only.

Responses default to 500 entries and accept `offset` and `limit`; the maximum
page size is 5,000. `page.next_offset` is null after the last page. Every page
also carries an opaque `revision` calculated from the complete visible folder
state. If that revision changes between pages, discard the partial result and
restart at offset zero. Do not parse or persist a revision as file identity.

The inventory method is read-only, uses the shared game-operation lock, and is
rejected while this service is managing a running game. A missing boundary
folder is a successful empty result with `exists: false`; individual entries
that disappear or become unreadable during a scan are omitted and reported in
`warnings`.

Move listed entries into an existing mod with the inventory revision and an
explicit confirmation:

```json
{"jsonrpc":"2.0","id":"overwrite-move","method":"boundaries.move.start","params":{"game":"skyrim_se","profile":"default","boundary":"overwrite","paths":["SKSE/Plugins/tool.log"],"revision":"<revision from inventory>","target_mod":"Generated files","confirm":true}}
```

Set `create_mod` to true to create an enabled, highest-priority mod before the
move. A mod created from Root Folder is marked for game-root deployment; one
created from Overwrite uses normal Data deployment. Existing targets must have
the same deployment-root mode, which prevents a move from silently changing
where the files will deploy. An unmanaged symbolic-link target is rejected;
validated Profile Group links remain supported.

```json
{"jsonrpc":"2.0","id":"root-move","method":"boundaries.move.start","params":{"game":"skyrim_se","profile":"default","boundary":"root_folder","paths":["skse64.log"],"revision":"<revision from inventory>","target_mod":"Root output","create_mod":true,"confirm":true}}
```

Permanently remove listed entries with:

```json
{"jsonrpc":"2.0","id":"overwrite-remove","method":"boundaries.remove.start","params":{"game":"skyrim_se","profile":"default","boundary":"overwrite","paths":["obsolete.log"],"revision":"<revision from inventory>","confirm":true}}
```

Mutation paths must exactly match entries from one complete, consistent
inventory. Files, symlinks, and directories whose `empty` value is true are
accepted. Expand a non-empty directory into its visible files and empty
subdirectories before submitting it; omitted bookkeeping is never deleted by
selecting a parent directory. Up to 4,096 paths can be changed in one job.

The service checks the complete boundary revision before preparing the
operation and again immediately before changing files. A stale request fails
with `boundary_revision_changed`, includes the current revision, and changes
nothing. Moving merges directories and replaces a same-path file or symlink in
the target mod; incompatible file/directory collisions are reported. Both
methods prune newly empty source directories, refresh the affected Filegraph
manifests, and reconcile the requested profile before succeeding. Their result
contains the new boundary `revision` and `filegraph_generation`. A rare partial
filesystem failure is returned as `boundary_move_incomplete` or
`boundary_remove_incomplete`, with completed and failed paths in the error
details.

When inventory reports `scope: "shared"`, both the boundary and any target mod
folder can be shared with other profiles. The explicit confirmation applies to
that shared storage, not only to the requested profile.

### Manage plugins

Plugin discovery reads FileGraph winners and plugin headers, so it runs as a
job even though it does not change `plugins.txt` or `loadorder.txt`:

```json
{"jsonrpc":"2.0","id":"plugins","method":"plugins.list.start","params":{"game":"skyrim_se","profile":"default"}}
```

The successful job result orders entries `first_loaded_first`. `position` is
the zero-based load-order position including game-managed plugins.
`game_index` is the normal hexadecimal index or an `FE:xxx` light-plugin index;
it is null for a disabled plugin. `masters` contains lower-case header
dependencies, or null when the plugin header could not be resolved.

Each plugin also reports `enabled`, `vanilla`, and `locked`. A plugin lock
prevents directly moving that entry but does not prevent activation changes.
`flags` is an array containing any applicable values from:

- `missing_masters`, `late_masters`, `version_mismatched_masters`
- `light`, `master`, `blueprint`, `groundcover`
- `loot_messages`, `dirty`, `bash_tags`
- `userlist`, `userlist_cycle`, `esl_eligible`, `esl_unsafe`

The three dependency-warning name lists are also returned under `warnings` so
clients do not need to interpret tooltip text. Top-level counts cover total,
enabled, disabled, vanilla, locked, and dependency-warning entries. The result
includes the pinned `filegraph_generation` used for the scan. Inventory scans
apply the manager's effective primary/master ordering in memory but do not
persist its automatic stale-entry repairs.

When cached LOOT metadata exists, each entry's `loot` object contains its
effective messages, unresolved requirements, incompatibilities, locations,
CRC-matched cleaning data, and Bash Tag suggestions; otherwise it is null.
Top-level `loot` reports the cache timestamp, game ID, and general masterlist
messages. Treat its text and URLs as untrusted remote metadata when rendering
them in an external client.

Enable or disable several plugins atomically after validation:

```json
{"jsonrpc":"2.0","id":"plugin-state","method":"plugins.set_enabled.start","params":{"game":"skyrim_se","profile":"default","changes":[{"name":"SkyUI_SE.esp","enabled":true},{"name":"OldPatch.esp","enabled":false}]}}
```

Names must exactly match the case-preserving `name` from the inventory. The
complete batch is resolved before either plugin file is written. Vanilla
game-managed entries cannot be disabled; asking to keep one enabled is an
unchanged success. Plugin load-order locks do not block enable/disable. The
result separates `changed` and `unchanged` values and returns updated counts.

Move one plugin, or a contiguous block in its current order, before or after an
exact inventory entry:

```json
{"jsonrpc":"2.0","id":"plugin-move","method":"plugins.move.start","params":{"game":"skyrim_se","profile":"default","plugins":["Patch A.esp","Patch B.esp"],"position":{"after":"Overhaul.esp"}}}
```

The request array selects the block; its saved relative order comes from the
current load order. A block cannot contain a vanilla or locked plugin, cannot
be non-contiguous, and on engines with distinct load-order regions cannot mix
master, normal, or Blueprint regions. The manager clamps the destination to
keep vanilla entries pinned, masters before their dependants, and engine load
regions intact, matching desktop drag behaviour. `constrained` reports that
the requested anchor could not be reached exactly; this may produce a safe
no-op with `changed: false`. `positions` gives the final position of each moved
plugin and `crossed` identifies entries whose relative order changed.

Run the built-in LOOT integration and apply the resulting order:

```json
{"jsonrpc":"2.0","id":"plugin-sort","method":"plugins.sort.start","params":{"game":"skyrim_se","profile":"default"}}
```

The method passes only the manager-resolved game, Data, staging, FileGraph
winner, userlist, and plugin state to LOOT. It accepts no executable, path,
masterlist URL, or command arguments from the client. A profile-local
`userlist.yaml` wins; otherwise Amethyst uses the configured game-wide LOOT
userlist. Locked plugins are held at their saved
indices before engine master/Blueprint and fixed-primary ordering is enforced.
Enable/disable state is preserved case-insensitively.

Masterlists and the LOOT prelude use Amethyst's normal cache. A stale cache is
refreshed from the handler's built-in `loot/*` repository configuration and a
failed refresh falls back to an existing cache. This operation may therefore
use the network, but the RPC client cannot redirect it to another host.
LOOT runs against an isolated temporary game view assembled from the selected
FileGraph snapshot. Undeployed plugin headers and files needed by metadata
conditions are linked into that view; the game's Data directory is not changed.

Set `apply_order` to false to refresh metadata and preview the constrained LOOT
result without changing `plugins.txt` or `loadorder.txt`:

```json
{"jsonrpc":"2.0","id":"plugin-preview","method":"plugins.sort.start","params":{"game":"skyrim_se","profile":"default","apply_order":false}}
```

The result includes `order_applied`, `changed`, `would_change`, `sorted_count`,
the pinned `filegraph_generation`, excluded `locked` entries, final proposed
`order`, and the names whose positions would move. `metadata` reports whether
`loot.json` was saved, how many plugins received metadata, and any general
messages. A metadata write failure is returned in `warnings` and does not
prevent a valid order from being applied. `userlist_source` is `profile`,
`game`, or null.

`capabilities.plugins.automatic_sort` and `metadata_refresh` are false when the
native LOOT binding is unavailable. Individual games also advertise
`features.plugin_sort`; clients should check both instead of assuming every
plugin-based game supports LOOT.

Activation and order changes rewrite each manager plugin file atomically. They
do not copy `plugins.txt`, restamp deployed files, or otherwise deploy. Call
`deploy.start` explicitly to apply the new state to the game, including mtime
load order on games that require it. These jobs use the shared game-operation
lock and are rejected while the same headless service is managing a running
game.

Expected job errors include `plugin_not_found`, `ambiguous_plugin`,
`plugin_required`, `plugin_not_movable`, `plugin_locked`,
`plugin_move_not_contiguous`, `plugin_move_mixed_regions`,
`plugin_state_unavailable`, `plugin_state_write_failed`, `loot_unavailable`,
`loot_configuration_invalid`, `loot_masterlist_unavailable`, `loot_cycle`,
`loot_sort_invalid_result`, and `loot_sort_failed`. A case-only name mismatch
is `plugin_not_found` with `expected_name` in its details.

### Nexus authentication

The headless service shares the desktop application's saved Nexus login. It
never returns access tokens, refresh tokens, API keys, or the account email.
Inspect the local credential and login-flow state with:

```json
{"jsonrpc":"2.0","id":"nexus-auth","method":"nexus.auth.get","params":{}}
```

`credentials.authenticated` means that an OAuth credential is stored; use
`nexus.user.get.start` when the client needs Nexus to validate it. The status
also reports the access-token expiry and whether a refresh will be required,
but never the token itself. `login` is the separate browser flow state and is
one of `idle`, `starting`, `waiting_for_user`, `exchanging`, `succeeded`,
`failed`, or `cancelled`.

Start a login without asking the machine running Amethyst to open a browser:

```json
{"jsonrpc":"2.0","id":"nexus-login","method":"nexus.auth.login.start","params":{"open_browser":false}}
```

Open the returned `login.authorize_url` in a browser. The browser redirect must
be able to reach `http://localhost:7890/callback` on the machine running the
headless process. Set `open_browser` to true when Amethyst should open that URL
locally. Poll `nexus.auth.get` until the login reaches a final state; an
unfinished flow times out after five minutes.

If the localhost redirect cannot complete, use the code from Nexus Mods'
"Having issues?" fallback on the same active flow:

```json
{"jsonrpc":"2.0","id":"nexus-code","method":"nexus.auth.login.submit_code","params":{"code":"<base64 code from Nexus>"}}
```

The code is passed directly to the OAuth client and is never returned in a
response, job snapshot, event, or log. Cancel a waiting flow with
`nexus.auth.login.cancel`.

Validate the saved session and retrieve the non-sensitive account fields:

```json
{"jsonrpc":"2.0","id":"nexus-user","method":"nexus.user.get.start","params":{}}
```

The successful job result contains `user_id`, display `name`, premium and
supporter flags, `profile_url`, and the most recently observed hourly and daily
rate limits. Set `refresh` to true to bypass the five-minute account cache.

### Discovering Nexus mods

Catalogue calls use the saved Nexus login and run as jobs. First fetch the
filter vocabulary for the selected game and domain:

```json
{"jsonrpc":"2.0","id":"nexus-filters","method":"nexus.mods.filters.start","params":{"game":"skyrim_se"}}
```

The result contains `categories`, with `category_id`, `name`, and nullable
`parent_category_id`, plus the game's Nexus `tags`. Search requests select
categories and tags by their returned names; IDs are supplied so a client can
build the category tree and retain stable UI keys. Fetch the facets again after
changing `domain` on a game with multiple Nexus domains.

Browse the most downloaded mods by omitting `mode`, or set it explicitly:

```json
{"jsonrpc":"2.0","id":"nexus-browse","method":"nexus.mods.search.start","params":{"game":"skyrim_se","mode":"browse","sort":"downloads","limit":20,"offset":0}}
```

Search by mod name and apply server-side filters:

```json
{"jsonrpc":"2.0","id":"nexus-search","method":"nexus.mods.search.start","params":{"game":"skyrim_se","mode":"name","query":"display tweaks","categories":["Utilities"],"sort":"endorsements","filters":{"tag_includes":["Performance Optimization"],"languages":["English"],"adult":false,"min_endorsements":10}}}
```

The query modes are:

- `browse`: list top mods. `created_since_days`, from 1 through 36,500, can
  restrict this mode to newly published mods.
- `name`: substring-search mod names. `query` is required and must contain at
  least two characters.
- `author`: match an uploader's current display name. `query` is required.
- `uploader_id`: list mods from one stable positive Nexus member ID. Supply
  `uploader_id` instead of `query`.
- `trending`: list mods published in the last seven days, ordered by
  endorsements. Its sort is fixed, so do not supply `sort`.

For modes other than `trending`, `sort` accepts `downloads`, `endorsements`,
`created`, or `updated` and defaults to `downloads`. `limit` defaults to 20 and
must be from 1 to 50. `offset` defaults to zero. A result contains the
normalized `query`, serialized `mods`, current rate limits, and a `page` object.
When a full page is returned, `page.next_offset` is the candidate offset for
the next request; a short or empty page sets it to null. Nexus does not provide
a total count through this query, so clients should stop after a short page.

`filters` accepts these optional fields:

- `tag_includes`, `tag_excludes`, and `languages` are arrays of names from
  `nexus.mods.filters.start`. Every included tag is required, every excluded
  tag is rejected, and a mod may match any selected language.
- `hide_translations` and `has_updated` are booleans.
- `min_file_size_kb`, `max_file_size_kb`, `min_downloads`, `max_downloads`,
  `min_endorsements`, and `max_endorsements` are integers from zero through
  2,147,483,647.
- `adult` and `supports_vortex` accept true, false, or null. Null means no
  filter.
- `title_contains`, `description_contains`, `author_contains`, and
  `uploader_contains` accept text from 2 to 512 characters.

Minimum values cannot exceed their corresponding maximum, duplicate list
entries are rejected, and a tag cannot be both included and excluded. All
filters combine with the selected domain, categories, query mode, and each
other. Use `nexus.mods.get.start` for authoritative detail after the user
selects a catalogue result.

### Exact Nexus metadata and downloads

Fetch exact mod and file metadata using positive numeric Nexus IDs:

```json
{"jsonrpc":"2.0","id":"nexus-mod","method":"nexus.mods.get.start","params":{"game":"skyrim_se","mod_id":3863}}
{"jsonrpc":"2.0","id":"nexus-files","method":"nexus.mods.files.start","params":{"game":"skyrim_se","mod_id":3863}}
{"jsonrpc":"2.0","id":"nexus-file","method":"nexus.files.get.start","params":{"game":"skyrim_se","mod_id":3863,"file_id":1000172397}}
```

The game's primary Nexus domain is used by default. Games that accept content
from more than one Nexus site expose all valid values in
`games.list`/`games.get`; pass `domain` to select a non-primary value. Amethyst
rejects domains outside that handler's list instead of making an arbitrary
cross-game request.

These network calls are jobs. Poll them like deployment jobs and inspect their
structured final errors. Expected Nexus errors include
`nexus_not_authenticated`, `nexus_auth_expired`, `nexus_auth_unavailable`,
`nexus_rate_limited`, and `nexus_request_failed`.

Download an exact file by ID:

```json
{"jsonrpc":"2.0","id":"nexus-download","method":"nexus.download.start","params":{"game":"skyrim_se","mod_id":3863,"file_id":1000172397}}
```

Without `nxm_url`, Amethyst first reuses a complete exact match already in the
managed game cache. A new direct transfer requires a Nexus Premium account;
free accounts receive `nexus_premium_required` and should use the NXM form.

For a free account, have the user select "Download with Manager" on the Nexus
website and pass the resulting URL as a single opaque value:

```json
{"jsonrpc":"2.0","id":"nexus-download-free","method":"nexus.download.start","params":{"game":"skyrim_se","nxm_url":"nxm://skyrimspecialedition/mods/3863/files/1000172397?key=<one-time-key>&expires=<unix-time>"}}
```

Do not combine `nxm_url` with `domain`, `mod_id`, or `file_id`; those values are
read from the link and checked against the selected game. Amethyst rejects an
expired link before queuing work. The raw URL is discarded after validation;
the one-time key and expiry are held only by the transfer runner. None appear
in job options, results, events, or diagnostics. Clients should apply the same
care to their own RPC request logging.

Downloads go to Amethyst's configured per-game cache, obey the shared download
speed limit, and never install automatically. A successful result contains the
resolved source identity, file metadata, absolute archive path, byte size, and
`from_cache`. Amethyst writes non-secret identity sidecars so
`downloads.list` can recover the Nexus domain and exact IDs after a restart.
Existing files are not overwritten; collision names retain compound extensions
such as `.tar.gz`. Attempts to download the same Nexus file into the same cache
are serialized across Amethyst processes.

The job reports byte progress with phase `download`. Cancel it with:

```json
{"jsonrpc":"2.0","id":"cancel-download","method":"jobs.cancel","params":{"job_id":"<download job id>"}}
```

Cancellation is cooperative while an API request or network read is in
progress. Partial archives and their managed sidecars are removed. If the
transfer completed before the request took effect, the job succeeds and keeps
the complete archive. Other expected final errors are
`nexus_premium_required`, `nexus_download_failed`, and
`nexus_download_unsafe_path`.

Clear both the shared OAuth login and any legacy Nexus API key with:

```json
{"jsonrpc":"2.0","id":"nexus-logout","method":"nexus.auth.logout.start","params":{"confirm":true}}
```

Logout is a job because keyring access can block. It also cancels an unfinished
browser login so a late callback cannot restore credentials after logout.

## Jobs

Job-producing calls return immediately with a snapshot. They do not hold the
JSON-RPC request open for filesystem or network work. Only one queued or
running job may be outstanding in a headless process; another job-producing
request returns `operation_in_progress` until it reaches a final state. The
OAuth browser flow is separate from this queue.

### Manage profiles

Create an empty profile with its own private mod staging folder:

```json
{"jsonrpc":"2.0","id":5,"method":"profiles.create.start","params":{"game":"skyrim_se","name":"Handheld","profile_specific_mods":true}}
```

An empty profile-specific profile starts with no installed mods plus private
`mods`, `overwrite`, and `Root_Folder` directories. Its plugin files may still
be seeded with game-required vanilla entries. With
`profile_specific_mods` omitted or false, a new shared-staging profile clones
the default profile's state and continues to use the game's common mod pool.

Copy an existing profile by supplying `copy_from`:

```json
{"jsonrpc":"2.0","id":6,"method":"profiles.create.start","params":{"game":"skyrim_se","name":"Handheld Copy","copy_from":"Handheld"}}
```

A copy inherits the source's shared or profile-specific storage mode. Copying a
profile-specific profile also copies its installed mod folders and may take
significant time and disk space. The `backups/` directory and standard
generated Filegraph projection and index files are not copied. The
original-default and profile lock flags are cleared on the copy. Profile groups
cannot be copied through
this method; manage their member definition in the desktop application for now.
Do not combine `copy_from` and `profile_specific_mods`.

Select a profile and persist it as the game's last-active profile:

```json
{"jsonrpc":"2.0","id":7,"method":"profiles.select.start","params":{"game":"skyrim_se","profile":"Handheld Copy"}}
```

Selection loads that profile's path overrides and materializes it when it is a
profile group. It does not deploy or restore the game, and other RPC methods
still require an explicit `profile` parameter.

Rename a profile:

```json
{"jsonrpc":"2.0","id":8,"method":"profiles.rename.start","params":{"game":"skyrim_se","profile":"Handheld Copy","new_name":"Travel"}}
```

Names are single safe folder components, cannot be `default`, and cannot
case-insensitively collide with another profile. Renaming a profile-group member
also updates each group's member list and link farm. If the original default is
renamed, it retains `original_default` protection.

Remove a profile permanently:

```json
{"jsonrpc":"2.0","id":9,"method":"profiles.remove.start","params":{"game":"skyrim_se","profile":"Travel","confirm":true}}
```

Removal requires `confirm: true`. The default/original-default profile and
profiles with `locked: true` cannot be removed. Removing a profile-specific
profile deletes its private mods; removing a shared-staging profile leaves the
common mod pool untouched. Removing a group deletes the group and any
group-local files but leaves its member profiles untouched. Removing a member
also prunes it from every group and rematerializes those groups. The result
reports `groups_updated` and the selected `fallback_profile`, if applicable.

Rename and removal fail with `profile_deployed` when the profile itself is
deployed or it is a member of the currently deployed group. Run `restore.start`
and retry so deployed links and runtime files can be recovered before their
profile data moves or disappears. Other expected job errors include
`profile_not_found`, `profile_exists`, `reserved_profile_name`,
`profile_protected`, `profile_locked`, `profile_copy_unsupported`, and the
operation-specific `profile_create_failed`, `profile_rename_failed`,
`profile_remove_failed`, or `profile_select_failed`.

### Enable and disable mods

Submit all related activation changes in one job. Each mod name must exactly
match the `name` returned by `mods.list`; names are case-sensitive. The batch is
validated before `modlist.txt` is changed, so a missing, locked, duplicate, or
separator entry rejects the complete batch.

```json
{"jsonrpc":"2.0","id":10,"method":"mods.set_enabled.start","params":{"game":"skyrim_se","profile":"default","changes":[{"name":"SkyUI","enabled":true},{"name":"Old textures","enabled":false}]}}
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
{"jsonrpc":"2.0","id":11,"method":"mods.move.start","params":{"game":"skyrim_se","profile":"default","mods":["Patch A","Patch B"],"position":{"after":"Patches_separator"}}}
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
{"jsonrpc":"2.0","id":12,"method":"mods.remove.start","params":{"game":"skyrim_se","profile":"default","mods":["Old textures","Superseded patch"],"confirm":true}}
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
{"jsonrpc":"2.0","id":13,"method":"deploy.start","params":{"game":"skyrim_se","profile":"default","options":{"root_folder_enabled":true,"backup":true}}}
```

Both deploy options default to `true`:

- `root_folder_enabled` deploys the profile's shared `Root_Folder` payload.
- `backup` creates the normal profile backup before deployment.

Start a restore and return the handler to the named profile afterward:

```json
{"jsonrpc":"2.0","id":14,"method":"restore.start","params":{"game":"skyrim_se","profile":"default"}}
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
    "cancellable": false,
    "cancel_requested": false,
    "event_sequence": 1
  }
}
```

Use `jobs.get` for the current snapshot or `jobs.list` to recover job IDs while
the same headless process remains alive:

```json
{"jsonrpc":"2.0","id":15,"method":"jobs.get","params":{"job_id":"09b37ca9237540918fe86ff5e359331c"}}
```

Job states are `queued`, `running`, `succeeded`, `failed`, and `cancelled`.
Account and logout jobs are not tied to a game or profile, so those snapshot
fields are null.

### Polling events

Events carry logs, progress, cancellation requests, and state transitions.
Pass the last processed sequence as `after`. `wait_ms` enables a long poll and
may be from 0 to 30000:

```json
{"jsonrpc":"2.0","id":16,"method":"jobs.events","params":{"job_id":"09b37ca9237540918fe86ff5e359331c","after":0,"wait_ms":1000}}
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

### Launching a deployed profile

Launch the game only after `deploy.start` has succeeded for the same profile:

```json
{"jsonrpc":"2.0","id":"launch","method":"launch.start","params":{"game":"skyrim_se","profile":"default"}}
```

This is a managed game launch, not an arbitrary command runner. Amethyst loads
the profile's settings and uses the same native, Steam, Heroic, Lutris, Faugus,
Proton, Flatpak-forwarding, and profile-VFS router as the desktop Play button.
It does not accept a command, executable path, environment, or arguments from
the RPC client. Saved launcher settings remain authoritative.

The named profile must be the currently deployed profile. The start job fails
with `launch_requires_deployment` when the game is restored, or
`launch_profile_mismatch` when another profile is deployed. The method never
deploys implicitly; this keeps a launch request from unexpectedly performing a
large filesystem mutation.

`launch.start` returns both its normal job snapshot and a separate `launch`
snapshot with an opaque `id`. A successful job means the existing router
dispatched a process or launcher hand-off. The game lifecycle continues after
that job completes, so poll it separately:

```json
{"jsonrpc":"2.0","id":"launch-state","method":"launch.get","params":{"launch_id":"<launch id>"}}
```

Launch states are `starting`, `running`, `stopping`, `stopped`, `finished`, and
`failed`. `active` covers the first three. `stop_supported` becomes true once
Amethyst has a scoped process identity it can stop. The snapshot also includes
timestamps, a monotonic `sequence`, a structured `error`, and the newest 200
launch log records. A launcher fallback or an immediately exiting game can
change the launch to `failed` after the dispatch job has succeeded, so continue
polling the launch state rather than treating job success as proof that a game
window appeared.

With no `launch_id`, `launch.get` returns the newest snapshot or null before any
attempt. Only the newest attempt is retained, and only one managed launch may
be active in a headless process. Profile, mod, install, deploy, and restore
mutations for the same game are rejected with `operation_in_progress` while
its managed launch remains active; reads and Nexus requests remain available.

Stop the tracked process tree with an explicit confirmation:

```json
{"jsonrpc":"2.0","id":"launch-stop","method":"launch.stop","params":{"launch_id":"<launch id>","confirm":true}}
```

Stopping is scoped by the launch token or launcher identity used by the desktop
Play button; it does not kill processes by executable name. The request returns
without waiting for the termination worker, normally at `stopping`; poll until
it reaches `stopped`. A launch which has already finished returns
`launch_not_stoppable`.

Closing the headless service detaches its lifecycle monitor and leaves the game
running. Call `launch.stop` first when the client intends to stop the game.
Other Amethyst processes cannot see this in-process lifecycle snapshot, so do
not deploy, restore, install, or remove files for that game from another
manager instance while it is running.

### Locking and failures

Headless profile, archive-install, mod and plugin management, and launch
dispatch jobs and the standard Amethyst Deploy and Restore entry
points—desktop, one-shot CLI, and headless—use the same operation coordinator.
Operations are serialized
within one process. Across native, AppImage and Flatpak processes, advisory
locks cover the game identity and resolved game, profile, staging and deployment
paths.

If another process already holds a relevant lock, the accepted job finishes as
`failed` with `error.name` set to `operation_busy`. Its error details identify
the holder when metadata was available. Lock files persist between runs; their
existence does not mean an operation is active because the kernel lock is the
authority.

Install jobs can report `archive_not_found`, `unsafe_archive`,
`unsafe_install_destination`, `installer_choices_required`,
`installer_unsupported`, `mod_already_exists`, and `install_failed`. Other final
job error names include `deploy_failed`, `restore_failed`,
`resource_unavailable`, and `internal_error`. Human-readable pipeline details
remain available in the job's `log` events.

Only snapshots with `cancellable: true` accept `jobs.cancel`; protocol version
1 currently limits this to Nexus downloads. Filesystem mutation jobs cannot be
cancelled because doing so could abandon a partially updated, restored, or
deployed filesystem. The separate OAuth browser flow can be cancelled with
`nexus.auth.login.cancel`.

Headless deployment uses the same non-interactive policy as the one-shot CLI:
there is no UI confirmation dialog. Configure and test unusual game-specific
deployment requirements in the desktop application first.

## Errors

Request validation and lookup failures use the JSON-RPC error object.
`error.data.name` is the stable machine-readable Amethyst error name:

```json
{
  "jsonrpc": "2.0",
  "id": 17,
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
| `-32007` | `operation_in_progress` | Another job is active, or the selected game has a managed launch running. |
| `-32008` | `game_not_configured` | The selected game has no usable configured path. |
| `-32009` | `operation_not_supported` or `loot_unavailable` | The selected handler or installation cannot perform the requested operation. |
| `-32010` | `unsupported_protocol_version` | The requested protocol version is incompatible. |
| `-32011` | `nexus_login_in_progress` or `nexus_login_unavailable` | The OAuth browser flow could not be started. |
| `-32012` | `nexus_download_link_expired` | The supplied one-time NXM link has expired. |
| `-32013` | `job_not_cancellable` | The active job does not support cooperative cancellation. |
| `-32014` | `launch_not_found` | The requested launch is not the newest attempt retained by this process. |
| `-32015` | `launch_not_stoppable` | The launch has no running process tree left to stop. |

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
no output line. Use request IDs for methods ending in `.start`; otherwise the
caller does not directly receive the created job ID or login-flow state.

## Current limitations

- Interactive FOMOD/BAIN/bundle choices are not represented in protocol version
  1. Clients can explicitly accept defaults; scripted FOMOD and multi-mod
  archives remain unsupported by the headless installer.
- Progress delivery is polling-only. Nexus downloads can be cancelled; other
  running jobs cannot.
- The shared operation lock currently coordinates headless profile,
  archive-install, mod activation, ordering and removal, plugin management, and
  launch-dispatch jobs and the standard deploy/restore flows, not every desktop
  mod/plugin-list edit, installer, wizard, configuration action, or a game's
  full running lifetime. Do not run those tools while a headless mutation is
  active.
- The transport is a child process over standard I/O; there is no socket daemon
  or remote authentication layer yet.
- Python modules under `amethyst_backend`, `Utils`, `Games` and `Nexus` remain
  implementation details. External applications should use the JSON protocol.

The service has the same filesystem permissions as the user who starts it.
Only pass requests from trusted local applications.
