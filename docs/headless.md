# Headless mode

Amethyst headless mode gives scripts and external applications a stable way to
use the manager without importing its Python modules or starting Qt. Protocol
version 1 exposes game, profile, mod and downloaded-archive state, Nexus OAuth,
catalogue discovery, exact-ID metadata, managed downloads, and non-interactive
archive installation, plus asynchronous mod activation, ordering, removal,
recorded-archive reinstall, Nexus version changes, update and requirement checks, plugin
inventory, activation, ordering and LOOT sorting, profile lifecycle, profile-group
membership, profile conversion, managed Overwrite and Root Folder inventory and
file management, installed collection inventory, appended-mod activation,
safe appended-collection removal, collection load-order reset and Nexus
collection catalogue discovery, download, install, resume, update and append,
Wabbajack gallery and installation, deploy, restore, and managed game launch.

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
      "cancellable_jobs": ["nexus.download", "thunderstore.download", "modio.download", "downloads.inspect", "downloads.interrupted.resume", "downloads.interrupted.discard", "wabbajack.gallery", "wabbajack.package.download", "wabbajack.preflight", "wabbajack.archive.import", "wabbajack.install", "collections.install", "collections.import", "collections.resume", "collections.update", "collections.append", "collections.download"],
      "pausable_jobs": ["nexus.download", "thunderstore.download", "modio.download", "downloads.interrupted.resume"],
      "mutations": ["profiles.create", "profiles.groups.create", "profiles.groups.set_members", "profiles.groups.exclusions.set", "profiles.convert", "profiles.rename", "profiles.remove", "games.configure", "games.reconfigure", "games.launcher.set", "games.options.set", "games.routing.set", "games.blacklist.set", "wabbajack.remove", "wabbajack.reset_load_order", "wabbajack.archive.import", "wabbajack.install", "collections.install", "collections.manual.submit", "collections.import", "collections.resume", "collections.update", "collections.append", "collections.group", "collections.download", "collections.remove", "collections.reset_load_order", "collections.set_enabled", "profiles.select", "separators.create", "separators.rename", "separators.remove", "separators.settings.set", "mods.bundle.set", "mods.rename", "mods.copy", "mods.move_profile", "mods.set_enabled", "mods.move", "mods.groups.edit", "mods.requirements.ignore.set", "mods.properties.set", "mods.version.change", "mods.notes.set", "mods.remove", "mods.check_updates", "mods.reinstall", "plugins.set_enabled", "plugins.move", "plugins.sort", "boundaries.move", "boundaries.remove", "saves.export", "saves.import", "saves.transfer", "saves.remove", "deploy", "restore", "nexus.auth.login", "nexus.auth.logout", "modio.credentials.set", "modio.credentials.clear", "nexus.download", "thunderstore.download", "modio.download", "downloads.install", "downloads.interrupted.resume", "downloads.interrupted.discard", "downloads.locations.set", "downloads.hidden.set", "downloads.move", "downloads.remove", "launch", "launch.stop", "launch.settings.set", "wine.dll_overrides.set"],
      "wine_overrides": {
        "prefix_inventory": true,
        "revision_checked": true,
        "persistent_removals": true,
        "reset": true,
        "apply": true,
        "running_prefix_guard": true
      },
      "mod_notes": {
        "scope": "profile",
        "bulk": true,
        "append": true,
        "remove": true,
        "revision_checked": true,
        "maximum_note_length": 65536,
        "maximum_changes": 4096,
        "maximum_payload_bytes": 1048576
      },
      "mod_properties": {
        "version_label": true,
        "root_folder": true,
        "update_ignores": ["nexus", "thunderstore"],
        "revision_checked": true,
        "owner_resolution": true,
        "routing_sync": "affected_profiles"
      },
      "mod_versions": {
        "sources": ["nexus", "thunderstore", "modio"],
        "catalog": true,
        "installed_highlights": true,
        "name_matched_recommendations": true,
        "revision_checked": true,
        "owner_resolution": true,
        "cached_archives": true,
        "nxm_downloads": true,
        "installer_choices": true,
        "routing_sync": "affected_profiles",
        "plugin_sync": "affected_profiles",
        "thunderstore_dependencies": ["reject", "ignore"]
      },
      "thunderstore": {
        "version_catalog": true,
        "downloads": true,
        "authentication": false,
        "community_checked": true,
        "archive_identity": true,
        "installation_metadata": true,
        "integrity": "size_and_manifest"
      },
      "launch_settings": {
        "revision_checked": true,
        "launcher_mode": true,
        "arguments": true,
        "launch_options": true,
        "wayland": true,
        "lsfg": true,
        "mangohud": true,
        "game_toggles": true
      },
      "downloads": {
        "interrupted": {
          "inventory": true,
          "revision_checked": true,
          "resume": true,
          "discard": true
        }
      },
      "installation": {
        "source": "download_inventory",
        "inspection": true,
        "installer_policies": ["reject", "defaults", "verbatim"],
        "bain_selections": true,
        "fomod_selections": true,
        "bundle_selections": true,
        "multi_mod_archives": true,
        "existing_mod_policies": ["fail", "replace"],
        "interactive": false
      },
      "mods": {
        "requirements": {
          "inventory": true,
          "satisfaction": "enabled_profile_mods",
          "source": "cached_nexus_metadata",
          "disabled_providers": true,
          "required_by": true,
          "profile_ignores": true,
          "individual_ignores": true,
          "revisioned_ignores": true
        },
        "groups": {
          "inventory": true,
          "revisioned_edits": true,
          "cosmetic_headers": true,
          "actions": ["create", "add", "ungroup", "promote", "rename", "collapse", "dissolve"],
          "group_aware_moves": true
        },
        "update_check": {
          "sources": ["nexus", "thunderstore", "modio"],
          "selection": true,
          "persists_metadata": true
        },
        "reinstall": {
          "source": "recorded_archive",
          "archive_selection": true,
          "preserves_profile_state": true,
          "profile_group_routing": true,
          "requires_confirmation": true
        }
      },
      "collections": {
        "installed_inventory": true,
        "profile_filter": true,
        "state_revision": true,
        "installation": {
          "new_profile": true,
          "local_import": true,
          "premium_automatic": true,
          "optional_selection": true,
          "game_version_confirmation": true,
          "manual": true,
          "manual_archives": true,
          "download_only": true,
          "resume": true,
          "update": true,
          "append": true,
          "grouping": true,
          "thunderstore_entries": true,
          "thunderstore_downloads": true
        },
        "appended_mod_activation": true,
        "appended_removal_plan": true,
        "load_order_reset": true,
        "group_load_order_reset": true,
        "catalogue": {
          "search": true,
          "revision_detail": true,
          "manifest": true,
          "sorts": ["downloads", "endorsements", "rating", "recent"],
          "maximum_page_size": 50
        }
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
      "modio": {
        "game": "baldurs_gate_3",
        "credentials_management": true,
        "credentials_exposed": false,
        "update_check": true,
        "version_catalog": true,
        "downloads": true,
        "platform": "windows",
        "archive_identity": true,
        "installation_metadata": true,
        "integrity": "size_and_md5"
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
        "games.installations.list.start",
        "games.prefix.detect.start",
        "games.launcher.get",
        "games.launcher.set.start",
        "games.configure.start",
        "games.reconfigure.plan",
        "games.reconfigure.start",
        "games.options.get",
        "games.options.set.start",
        "games.routing.get",
        "games.routing.set.start",
        "games.blacklist.get",
        "games.blacklist.set.start",
        "profiles.list",
        "profiles.create.start",
        "profiles.groups.create.start",
        "profiles.groups.set_members.start",
        "profiles.groups.exclusions.get",
        "profiles.groups.exclusions.set.start",
        "profiles.convert.start",
        "profiles.rename.start",
        "profiles.remove.start",
        "profiles.select.start",
        "collections.installed.list",
        "collections.download.start",
        "collections.install.start",
        "collections.manual.get",
        "collections.manual.submit",
        "collections.import.start",
        "collections.resume.start",
        "collections.update.start",
        "collections.append.start",
        "collections.group.start",
        "collections.remove.plan.start",
        "collections.remove.start",
        "collections.reset_load_order.start",
        "collections.set_enabled.start",
        "wabbajack.list",
        "wabbajack.gallery.start",
        "wabbajack.package.download.start",
        "wabbajack.inspect.start",
        "wabbajack.preflight.start",
        "wabbajack.archive.import.start",
        "wabbajack.install.start",
        "wabbajack.remove.start",
        "wabbajack.reset_load_order.start",
        "downloads.list",
        "downloads.interrupted.list",
        "downloads.interrupted.resume.start",
        "downloads.interrupted.discard.start",
        "downloads.locations.get",
        "downloads.locations.set.start",
        "downloads.hidden.set.start",
        "downloads.move.plan.start",
        "downloads.move.start",
        "downloads.remove.plan.start",
        "downloads.remove.start",
        "downloads.inspect.start",
        "downloads.install.start",
        "nexus.auth.get",
        "nexus.auth.login.start",
        "nexus.auth.login.submit_code",
        "nexus.auth.login.cancel",
        "nexus.auth.logout.start",
        "modio.credentials.get",
        "modio.credentials.set.start",
        "modio.credentials.clear.start",
        "nexus.user.get.start",
        "nexus.collections.search.start",
        "nexus.collections.get.start",
        "nexus.mods.filters.start",
        "nexus.mods.search.start",
        "nexus.mods.get.start",
        "nexus.mods.files.start",
        "nexus.files.get.start",
        "nexus.download.start",
        "thunderstore.packages.versions.start",
        "thunderstore.download.start",
        "modio.mods.files.start",
        "modio.download.start",
        "mods.list",
        "mods.properties.get",
        "mods.properties.set.start",
        "mods.versions.get.start",
        "mods.version.change.start",
        "mods.notes.get",
        "mods.notes.set.start",
        "mods.requirements.get",
        "mods.requirements.ignore.set.start",
        "mods.groups.get",
        "mods.groups.edit.start",
        "mods.conflicts.inspect.start",
        "separators.create.start",
        "separators.rename.start",
        "separators.remove.start",
        "separators.settings.set.start",
        "mods.bundle.get",
        "mods.bundle.set.start",
        "mods.check_updates.start",
        "mods.reinstall.start",
        "mods.rename.plan",
        "mods.rename.start",
        "mods.copy.plan",
        "mods.copy.start",
        "mods.move_profile.plan",
        "mods.move_profile.start",
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
        "saves.locations.start",
        "saves.entries.start",
        "saves.export.start",
        "saves.import.start",
        "saves.transfer.start",
        "saves.remove.start",
        "deploy.start",
        "restore.start",
        "launch.get",
        "launch.settings.get",
        "launch.settings.set.start",
        "wine.dll_overrides.get",
        "wine.dll_overrides.set.start",
        "launch.start",
        "launch.stop",
        "jobs.get",
        "jobs.list",
        "jobs.events",
        "jobs.cancel",
        "jobs.pause",
        "jobs.resume"
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
| `games.installations.list.start` | `game` string | Scan Steam, Heroic, Lutris, Faugus, and non-Steam shortcuts for candidate installations. |
| `games.prefix.detect.start` | `game`, absolute `game_path`, `source`, and `launcher_id` for non-Steam launchers | Find the chosen install's Proton prefix without saving it. `source` is `steam`, `heroic`, `lutris`, `faugus`, or `shortcut`. |
| `games.launcher.get` | `game` string, `profile` string | Read the selected profile's saved launcher identities and revision. |
| `games.launcher.set.start` | `game`, `profile`, `source`, and `revision` from `games.launcher.get`; `launcher_id` for Heroic, Lutris, Faugus, or a non-Steam shortcut | Save one launcher identity and clear the alternatives for that profile. `source` is `steam`, `manual`, `heroic`, `lutris`, `faugus`, or `shortcut`. |
| `games.configure.start` | `game` string, `game_path` absolute path, `prefix_path`, `staging_path`, `save_path_override`, or OpenMW `appimage_path` absolute paths (optional), `deploy_mode` hardlink, symlink, copy, or `vfs` where supported (optional), `runtime_mode` native or proton for BG3 (optional) | Configure a game that has no saved paths and create its default profile. |
| `games.reconfigure.plan` | `game`, `profile`, and at least one of `game_path`, `prefix_path`, `staging_path`, `save_path_override`, `appimage_path`, `deploy_mode`, or `runtime_mode` | Preview a configured game's path changes and staging migration; `deploy_mode` accepts hardlink, symlink, copy, or `vfs` where supported. |
| `games.reconfigure.start` | Same target fields plus `revision` from the plan and `confirm` true; `migrate_staging` boolean when existing files would move | Apply the plan only while its source and destination state still match. |
| `games.options.get` | `game` string, `profile` string | List safe per-profile quick-configuration options and their current values. |
| `games.options.set.start` | `game` string, `profile` string, `key` string, `value`, `expected_value` | Change one available option if its current value still matches. |
| `games.routing.get` | `game` string | Read ordered per-game custom routing rules and their revision. |
| `games.routing.set.start` | `game`, `revision`, full `rules` array, `confirm` true; `profile` (optional) | Save validated rule edits and optionally reconcile one profile. |
| `games.blacklist.get` | `game` string | Read built-in, custom, disabled, and effective per-game conflict exclusions with a revision. |
| `games.blacklist.set.start` | `game`, `revision`, full `overrides` object, `confirm` true; `profile` (optional) | Save filename and folder exclusions and optionally reconcile one profile. |
| `profiles.list` | `game` string | List existing profiles and summary counts. |
| `profiles.create.start` | `game` string, `name` string, `profile_specific_mods` boolean (optional), `copy_from` string (optional) | Create or copy a profile. |
| `profiles.groups.create.start` | `game` string, `name` string, `members` array of profile names, `ini_source` string (optional), `overwrite_excluded` array of profile names (optional) | Create and materialize a profile group. |
| `profiles.groups.set_members.start` | `game` string, `profile` string, `members` array of profile names, `expected_members` array of profile names | Replace a group's ordered membership and rematerialize it. |
| `profiles.groups.exclusions.get` | `game`, group `profile` | List member mods excluded from a Profile Group and return a revision. |
| `profiles.groups.exclusions.set.start` | `game`, group `profile`, full `exclusions` array of `{member, mod}`, `revision`, `confirm` true | Save exclusions and rematerialize the group. |
| `profiles.convert.start` | `game` string, `profile` string, `confirm` boolean | Convert a shared-pool profile to profile-specific mod storage. |
| `profiles.rename.start` | `game` string, `profile` string, `new_name` string | Rename a profile. |
| `profiles.remove.start` | `game` string, `profile` string, `confirm` boolean | Permanently remove a profile. |
| `profiles.select.start` | `game` string, `profile` string | Persist and load the active profile. |
| `collections.installed.list` | `game` string, `profile` string (optional) | List installed standalone and appended collections. |
| `collections.download.start` | `game` and `slug`; `domain` and `revision` (optional) | Download every Nexus archive and pinned Thunderstore package for a collection without changing a profile. |
| `collections.install.start` | `game`, `profile`, and `slug`; `domain`, `revision`, `skipped_optional_file_ids`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Install a Nexus collection into a new private profile. |
| `collections.manual.get` | `job_id` | Read the current interactive archive request for a collection job. |
| `collections.manual.submit` | `job_id`, `request_id`, and either absolute `path` or `skip` true | Supply an archive or skip an optional request. |
| `collections.import.start` | `game`, `profile`, and absolute `path`; `skipped_optional_file_ids`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Import a local `.amethyst`, ZIP, or JSON profile manifest into a new private profile. |
| `collections.resume.start` | `game`, `profile`, `collection_id`, and `state_revision`; `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Resume an incomplete standalone Nexus collection installation. |
| `collections.update.start` | `game`, `profile`, `collection_id`, `state_revision`, `target_revision`, and `confirm` true; `skipped_optional_file_ids`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Update a standalone Nexus collection while retaining the prior revision until verification succeeds. |
| `collections.append.start` | `game`, `profile`, `slug`, and `confirm` true; `domain`, `revision`, `skipped_optional_file_ids`, `overwrite_existing`, `skip_existing`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Append a Nexus collection to an existing private profile. |
| `collections.group.start` | `game`, `profile`, `target`, `collection_id`, and `state_revision`; `group_name`, `ini_source`, and `overwrite_excluded` when creating a group | Add a complete standalone collection profile to a new or existing profile group. |
| `collections.remove.plan.start` | `game`, `profile`, `collection_id`, and `state_revision` | Preview the exact appended-collection mods that would be deleted or retained. |
| `collections.remove.start` | `game`, `profile`, `collection_id`, `state_revision`, `plan_revision`, and `confirm` true | Remove an appended collection only if its record and deletion plan remain unchanged. |
| `collections.reset_load_order.start` | `game`, `profile`, `collection_id`, `state_revision`, and `group` string (optional) | Restore one installed collection's authored mod and plugin order in a profile or containing profile group. |
| `collections.set_enabled.start` | `game`, `profile`, `collection_id`, `state_revision`, and `enabled` | Enable or disable the exclusively owned mods of an appended collection. |
| `wabbajack.list` | `game` string (optional) | List managed Wabbajack installations and referencing profiles. |
| `wabbajack.gallery.start` | `game` string, `refresh` boolean, `cached_only` boolean (all optional) | List authored Wabbajack packages from the gallery. |
| `wabbajack.package.download.start` | `entry_id` string, `expected_version` string (optional) | Download and inspect a gallery package. |
| `wabbajack.inspect.start` | `package_path` string | Inspect a local Wabbajack package's game, profiles, and archive plan. |
| `wabbajack.preflight.start` | `game` string, `package_path` string, `installation_name` string and `profiles` array for a new installation, or `installation_id` string for resume/repair/update; options below | Check an installation's package, source files, downloads, disk space, and setup requirements. |
| `wabbajack.archive.import.start` | `game`, `package_path`, `archive_hash`, `source_path` strings; `expected_identity` and absolute `downloads_path` (optional) | Verify and import a local archive required by the package. |
| `wabbajack.install.start` | Same installation selection as preflight; `conflict_policy` string and `accept_known_issues` boolean (optional) | Install, resume, repair, or update a local Wabbajack package. |
| `wabbajack.remove.start` | `game` string, `installation_id` string, `confirm` boolean | Remove a managed installation and its linked profiles. |
| `wabbajack.reset_load_order.start` | `game` string, `profile` string | Restore a Wabbajack profile's authored mod and plugin order. |
| `downloads.list` | `game` string, `profile` string | List downloaded mod archives and configured locations. |
| `downloads.interrupted.list` | `game`, `profile` | List unlocked saved Nexus partials in enabled download locations for this game's domains. |
| `downloads.interrupted.resume.start` | `game`, `profile`, `download_id`, per-item `revision`; optional fresh `nxm_url` | Resume one exact saved transfer using Premium or a matching NXM grant. |
| `downloads.interrupted.discard.start` | `game`, `profile`, `download_id`, per-item `revision`, `confirm` true | Delete one saved partial and its resume metadata. |
| `downloads.locations.get` | None | Read global download scan settings and their revision. |
| `downloads.locations.set.start` | `revision` from `downloads.locations.get`, full `settings` object | Change enabled scan locations and extra folders. |
| `downloads.hidden.set.start` | `game`, `profile`, `archive_ids` array, `hidden` boolean, `revision` from `downloads.list` | Hide or show selected archives in the desktop Downloads tab. |
| `downloads.move.plan.start` | `game`, `profile`, `archive_ids` array, `destination_location_id` from `downloads.list` | Inspect a move between enabled download locations, including destination collisions. |
| `downloads.move.start` | Same selection, `plan_revision`, `confirm: true`, `if_exists` `fail` or `replace` (optional) | Move selected archives with their Nexus identity sidecars. |
| `downloads.remove.plan.start` | `game`, `profile`, `archive_ids` array | Inspect the exact archives and sidecars that removal would delete. |
| `downloads.remove.start` | Same selection, `plan_revision` from the plan, `confirm: true` | Permanently delete selected downloaded archives and their sidecars. |
| `downloads.inspect.start` | `game` string, `profile` string, `archive_id` string, `name` string (optional) | Inspect an archive's installer choices and destination without installing it. |
| `downloads.install.start` | `game` string, `profile` string, `archive_id` string, `name` string (optional), `installer_policy` string (optional), `bain_selected` or `bundle_selected` array, or `fomod_selections` object (optional), `if_exists` string (optional) | Install one archive returned by `downloads.list`. |
| `nexus.auth.get` | None | Inspect saved-credential and active-login state without exposing credentials. |
| `nexus.auth.login.start` | `open_browser` boolean (optional) | Start a Nexus OAuth login flow and return its authorisation URL. |
| `nexus.auth.login.submit_code` | `code` string | Submit the manual fallback code for the active login. |
| `nexus.auth.login.cancel` | None | Cancel the active login flow. |
| `nexus.auth.logout.start` | `confirm` boolean | Clear saved Nexus OAuth and legacy API-key credentials. |
| `modio.credentials.get` | None | Inspect saved BG3 mod.io credential status without returning the key. |
| `modio.credentials.set.start` | `api_key` and `api_path` strings | Verify and save the BG3 mod.io connection. |
| `modio.credentials.clear.start` | `confirm` boolean | Clear saved BG3 mod.io credentials. |
| `nexus.user.get.start` | `refresh` boolean (optional) | Validate the login and return sanitized account information. |
| `nexus.collections.search.start` | `game` string; `domain`, `query`, `limit`, `offset`, `sort`, and `include_adult` (optional) | Browse or search the Nexus collection catalogue. |
| `nexus.collections.get.start` | `game`, `slug`; `domain`, `revision`, and `include_manifest` (optional) | Fetch one collection revision and optionally its authoritative manifest. |
| `nexus.mods.filters.start` | `game` string, `domain` string (optional) | Fetch category and tag facets for Nexus catalogue queries. |
| `nexus.mods.search.start` | `game` string plus query options | Browse or search the paged Nexus mod catalogue. |
| `nexus.mods.get.start` | `game` string, `mod_id` integer, `domain` string (optional) | Fetch one Nexus mod by exact ID. |
| `nexus.mods.files.start` | `game` string, `mod_id` integer, `domain` string (optional) | Fetch the files and update chain for one Nexus mod. |
| `nexus.files.get.start` | `game` string, `mod_id` integer, `file_id` integer, `domain` string (optional) | Fetch one exact Nexus file. |
| `nexus.download.start` | `game` string plus either exact IDs or `nxm_url` string | Download one Nexus archive into Amethyst's managed game cache. |
| `thunderstore.packages.versions.start` | `game`, `namespace`, `package` | Read a package's published versions and recommendation for the selected game's community. |
| `thunderstore.download.start` | `game`, `namespace`, `package`, `version` | Download an exact community-checked package version without authentication; pause and cancel are supported. |
| `modio.mods.files.start` | `game`, `mod_id` | Read all published Windows files for a known BG3 mod, with its live-file recommendation. |
| `modio.download.start` | `game`, `mod_id`, `file_id` | Download an exact BG3 mod.io file with size/MD5 verification; pause and cancel are supported. |
| `mods.list` | `game` string, `profile` string | List a profile's mods, separators and installed metadata. |
| `mods.properties.get` | `game`, `profile`, `mod` | Read version labels, root-folder routing, source-specific update ignores, ownership, affected profiles, and revision. |
| `mods.properties.set.start` | `game`, `profile`, `mod`, `revision`, non-empty `changes` object | Save supported metadata fields atomically and refresh affected routing when requested. |
| `mods.versions.get.start` | `game`, `profile`, `mod`; `source` (optional, `nexus`, `thunderstore`, or `modio`) | Read installed source identity, the version catalogue, recommendations, ownership, and revision. |
| `mods.version.change.start` | `game`, `profile`, `mod`, `revision`, `confirm:true`; Nexus/mod.io `file_id` or Thunderstore `version`; `source`, archive/download options, installer choices, and Thunderstore `dependency_policy` (optional) | Replace an installed mod with a selected source version and refresh affected profile routing and plugins. |
| `mods.notes.get` | `game`, `profile`; `mods` array (optional) | Read profile-local notes for real mods and the current revision. |
| `mods.notes.set.start` | `game`, `profile`, `revision`, `changes` object; `append` boolean (optional) | Replace, append, or remove selected notes atomically. |
| `mods.requirements.get` | `game`, `profile`; optional `mods` selection | Read live requirement status and enabled/disabled providers from saved Nexus graphs. |
| `mods.requirements.ignore.set.start` | `game`, `profile`, `mod`, `revision`; `ignored` and/or `requirement_ids` | Change a profile's owner warning or the mod's individual requirement ignores. |
| `mods.groups.get` | `game`, `profile` | Read profile-local mod groups and their combined order/state revision. |
| `mods.groups.edit.start` | `game`, `profile`, `revision`, `action`, action-specific fields | Create or edit real-leader groups and named cosmetic headers. |
| `mods.conflicts.inspect.start` | `game`, `profile`, exact `mod`; `section`, `offset`, `limit`, `revision` (optional) | Read a page of one mod's winning, losing, or uncontested files from Filegraph. |
| `separators.create.start` | `game`, `profile`, display `name`, `revision` from `mods.list`; `position` object (optional) | Add a separator above or below an existing mod-list entry, or at the top by default. |
| `separators.rename.start` | `game`, `profile`, internal `separator` name, display `new_name`, `revision` | Rename a separator and migrate its saved state. |
| `separators.remove.start` | `game`, `profile`, internal `separator` name, `revision`, `confirm` true | Remove a separator and its saved state without removing its mods. |
| `separators.settings.set.start` | `game`, `profile`, internal `separator` name, `revision`, `state_revision`, `changes` object | Change collapse, lock, colour, or deployment settings if both revisions still match. |
| `mods.bundle.get` | `game` string, `profile` string, `mod` string | Read an installed RE bundle's option groups and current selection. |
| `mods.bundle.set.start` | `game` string, `profile` string, `mod` string, `revision` string, `bundle_selected` array | Change the selected folders of an installed RE bundle. |
| `mods.check_updates.start` | `game` string, `profile` string; `mods` and `sources` arrays (optional) | Check profile-scoped Nexus, Thunderstore, and BG3 mod.io updates. |
| `mods.reinstall.start` | `game` string, `profile` string, `mod` string, `confirm` boolean, `archive_id` string (optional), `installer_policy` string (optional), `bain_selected` or `bundle_selected` array, or `fomod_selections` object (optional) | Reinstall a mod from its recorded archive. |
| `mods.rename.plan` | `game`, `profile`, `mod`, `new_name` | Inspect a staged-folder rename, affected profiles and groups, and any blockers. |
| `mods.rename.start` | Same selection, `plan_revision`, `confirm` true | Rename the folder and every referencing profile after rechecking the plan. |
| `mods.copy.plan` | `game`, `profile`, `target_profile`, `mods` array; `separator`, `renames`, `if_exists` (optional) | Inspect a cross-profile copy, including group members and collisions. |
| `mods.copy.start` | Same selection, `plan_revision`, `confirm: true` | Copy selected mods and optional separator to another profile. |
| `mods.move_profile.plan` | Same selection as `mods.copy.plan` | Inspect a cross-profile move and source-sharing constraints. |
| `mods.move_profile.start` | Same selection, `plan_revision`, `confirm: true` | Copy selected mods, then remove their source entries and unshared folders. |
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
| `saves.locations.start` | `game` string, `profile` string | Resolve the game's known save locations and an existing profile-saves folder. |
| `saves.entries.start` | `game`, `profile`, `location`; `directory`, `offset`, `limit` (optional) | List a save folder, with entry revisions and pagination. |
| `saves.export.start` | `game`, `profile`, `location`, absolute `archive_path`; `overwrite` boolean (optional) | Export matching saves to a ZIP archive. |
| `saves.import.start` | `game`, `profile`, `location`, absolute `archive_path`, `confirm` true | Import a ZIP archive, moving existing saves to a backup first. |
| `saves.transfer.start` | `game`, `profile`, `location`, `entry`, `revision`, `target_profile`; `move`, `overwrite`, `confirm` (optional) | Copy or move an entry into profile saves, with explicit confirmation for moves or replacements. |
| `saves.remove.start` | `game`, `profile`, `location`, `entry`, `revision`, `confirm` true | Delete one selected save entry. |
| `deploy.start` | `game` string, `profile` string, `options` object (optional) | Start a deployment job. |
| `restore.start` | `game` string, `profile` string | Start a restore job. |
| `launch.get` | `launch_id` string (optional) | Inspect the latest managed game-launch lifecycle. |
| `launch.settings.get` | `game`, `profile` | Read launch settings, executable key, scope, declared game toggles, and revision. |
| `launch.settings.set.start` | `game`, `profile`, `revision`, non-empty `changes` object | Save validated launch controls and selected-profile arguments while preserving unrelated settings. |
| `wine.dll_overrides.get` | `game`, `profile` | Read current prefix overrides, stored choices, defaults, persistent removals/resets, and revision. |
| `wine.dll_overrides.set.start` | `game`, `profile`, `revision`; optional `changes` object, `reset` array, `apply` boolean | Save DLL overrides or default choices and optionally apply them to the selected profile's idle prefix. |
| `launch.start` | `game` string, `profile` string | Launch the currently deployed profile through Amethyst's configured router. |
| `launch.stop` | `launch_id` string, `confirm` boolean | Stop a process tree started by the managed launch. |
| `jobs.get` | `job_id` string | Get the latest job snapshot. |
| `jobs.list` | `active_only` boolean (optional) | List jobs, newest first. |
| `jobs.events` | `job_id` string, `after` integer (optional), `wait_ms` integer (optional) | Read or long-poll ordered job events. |
| `jobs.cancel` | `job_id` string | Request cancellation of a job whose snapshot says `cancellable: true`. |
| `jobs.pause` | `job_id` string | Pause a job whose snapshot says `pausable: true`. |
| `jobs.resume` | `job_id` string | Resume a paused job. |

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
installed native LOOT binding is available. `features.vfs_deploy` and
`features.copy_deploy` identify the additional deployment methods a handler
supports. `features.appimage_path` identifies OpenMW's explicit AppImage path.

To find installation paths and launcher identities before configuring a game:

```json
{"jsonrpc":"2.0","id":"scan","method":"games.installations.list.start","params":{"game":"skyrim_se"}}
```

Poll the returned job with `jobs.get` or `jobs.events`. Its result contains
`installations` in the desktop wizard's detection order. Each candidate has
`source`, `game_path`, `prefix_path`, `launcher_id`, `prefix_mode`, and
`executable`. `prefix_mode` is `native`, `resolved`, or `detect`; a null
`prefix_path` with `detect` needs normal prefix detection or an explicit path.
The scan reads launcher state and does not change the saved game configuration.
For a candidate with `prefix_mode:"detect"`, use its path, source, and
non-Steam launcher ID in `games.prefix.detect.start` before calling
`games.configure.start`:

```json
{"jsonrpc":"2.0","id":"prefix","method":"games.prefix.detect.start","params":{"game":"skyrim_se","game_path":"/games/Skyrim Special Edition","source":"steam"}}
```

The job result has `prefix_path` or null when no prefix was found. Explicit
Steam scans ignore a saved non-Steam shortcut ID, so a shortcut's compatdata
cannot be mistaken for the selected Steam install's prefix. Native installs
do not need a Proton prefix.
After configuring or reconfiguring the selected path and prefix, call
`games.launcher.get`, then pass its `revision` and the candidate's `source`
and `launcher_id` to `games.launcher.set.start`. Steam and manual installs omit
`launcher_id`; shortcut IDs are unsigned 32-bit decimal app IDs. Setting a
launcher clears the other saved identities for that profile. A null `source`
from `games.launcher.get` means no non-Steam launcher
identity is saved; it does not distinguish Steam from a manual install.

Configure a newly discovered game:

```json
{"jsonrpc":"2.0","id":3,"method":"games.configure.start","params":{"game":"skyrim_se","game_path":"/games/Skyrim Special Edition","staging_path":"/games/Amethyst/Skyrim","deploy_mode":"symlink"}}
```

If omitted, `staging_path` uses Amethyst's per-game default under
`~/Games/Amethyst`. The operation checks game and prefix paths, staging ownership
and layout, game/staging overlap, and hard-link filesystem compatibility before
saving. It creates the normal `profiles/default` tree and returns any
`validate_install` warnings. A game with an existing `paths.json` must be
changed with `games.reconfigure.plan` and `games.reconfigure.start`; this
first-time method never replaces it. On a VFS-capable game,
`deploy_mode:"vfs"` enables VFS while preserving the physical fallback mode.
An explicit hardlink, symlink, or copy mode disables a previously saved VFS
setting. `save_path_override` selects a manual saves folder; if it already
exists, it must be a directory. OpenMW's `appimage_path` must point to an
existing file. Omit either path to use its default behavior.

Preview a change to an existing game, then apply the returned revision:

```json
{"jsonrpc":"2.0","id":"path-plan","method":"games.reconfigure.plan","params":{"game":"skyrim_se","profile":"default","staging_path":"/games/Amethyst/Skyrim-New"}}
{"jsonrpc":"2.0","id":"path-change","method":"games.reconfigure.start","params":{"game":"skyrim_se","profile":"default","staging_path":"/games/Amethyst/Skyrim-New","revision":"<revision from plan>","migrate_staging":true,"confirm":true}}
```

The plan reports current and target paths, changed fields, install warnings,
and the file count, size, and destination collisions for a staging move. Repeat
the same target fields in the start request. A changed configuration or staging
tree fails with `game_configuration_changed`; request a fresh plan. Restore a
deployed game before reconfiguring it. Game, prefix, runtime and deploy settings
follow the selected profile; the staging root is shared by the game. A VFS
deploy mode preserves the previous physical mode as its fallback. Changing
from VFS to a physical mode disables VFS for the selected profile. Plan-time
install warnings are preliminary when VFS changes; the job result validates
the saved setting again.
`prefix_path:null` clears the prefix and
`staging_path:null` selects the default staging root. `save_path_override:null`
returns to automatic save discovery; OpenMW `appimage_path:null` returns to
automatic AppImage detection. Manual save paths are shared across profiles,
while the OpenMW AppImage path follows the selected profile. Changing the default
profile's game path preserves other profiles' existing inherited paths. For a
staging change with files in the old root, set `migrate_staging` to true to
move missing files or false to leave them at the old root. Existing destination
files win; any failed moves are reported as an incomplete migration with the
new paths already saved.

Inspect and change an available per-profile game option:

```json
{"jsonrpc":"2.0","id":20,"method":"games.options.get","params":{"game":"skyrim_se","profile":"default"}}
{"jsonrpc":"2.0","id":21,"method":"games.options.set.start","params":{"game":"skyrim_se","profile":"default","key":"deploy_mode","value":"symlink","expected_value":"hardlink"}}
```

Options come from the same quick-configuration descriptors as the desktop
profile menu. Each row gives the key, label, current value, toggle or choice
kind, valid choices where applicable, and whether the game paths need a reload.
Use the returned current value as `expected_value`; the job rejects a stale
value with `game_option_changed`. An unavailable key or unsupported value is
rejected. Changing the deploy method or preferred OpenMW package while deployed
requires restoring the game first. A hard-link deploy method also requires the
staging folder and every deployment target to share a filesystem. Settings use
the selected profile's normal persistence and the shared game-operation lock.

`games.routing.get` returns the handler's built-in routing rules alongside
custom rules, in first-match order. Each row has an `id`, `enabled`, `values`,
`builtin`, and `defaults` for a built-in. To edit them, submit the complete
ordered `rules` array with its `revision` to `games.routing.set.start`.
Only `id`, `enabled`, and `values` are writable; the other fields may be echoed
unchanged. Use `id: null` to add a custom rule, omit a custom row to delete it,
or set `enabled: false` to disable a built-in without losing its restore path.
All built-in IDs must remain in the array. Each `values` object must include
all routing fields from the read result. Extensions and filenames are
alternative matches; folders constrain extension matches only. Earlier rules
take precedence. Rules with `to_prefix: true` need a configured Proton/Wine
prefix to place files there.

Routing is per game, so the save affects every profile. Supplying `profile`
also reconciles that profile's Filegraph immediately; other profiles refresh
on their next use. A later deploy applies the new destinations to game files.
A stale revision fails with `stale_routing_rules`. If the rules save but the
selected profile cannot refresh, the job reports `routing_sync_failed` with
`saved: true` and the saved snapshot.

`games.blacklist.get` returns built-in filename and folder patterns under
`defaults`, saved custom and disabled patterns under `overrides`, and the
resulting `effective` patterns. To change them, send the complete `overrides`
object and its `revision` to `games.blacklist.set.start`. The object must have
four arrays: `files`, `folders`, `disabled_files`, and `disabled_folders`.
Only built-in patterns or previously saved disabled patterns may appear in the
disabled arrays. To restore one, remove it from the disabled array. Patterns
are case-insensitive filename or folder globs without path separators.

The blacklist is per game and affects all profiles. Supplying `profile` also
reconciles that profile's Filegraph immediately; other profiles refresh on their
next use. A later deploy applies the exclusions to game files. A stale revision
fails with `stale_blacklist`. If the rules save but the selected profile cannot
refresh, the job reports `blacklist_sync_failed` with `saved: true` and the
saved snapshot.

List profiles:

```json
{"jsonrpc":"2.0","id":3,"method":"profiles.list","params":{"game":"skyrim_se"}}
```

Profiles include mod counts, profile-group membership, profile-specific staging
state, collection association, selected/deployed state, and `locked` and
`original_default` removal-protection flags.

List installed collections for every profile of one game:

```json
{"jsonrpc":"2.0","id":"collections","method":"collections.installed.list","params":{"game":"skyrim_se"}}
```

Add `"profile":"default"` to scan one profile. Results are flat: standalone
and appended installations each have their profile, collection identity,
revision, status, ownership member count, manifest counts, available card
metadata, and whether the profile lock permits toggling or removal. Legacy
installations may have a null `id`. `state_revision` changes when the saved
installation record changes and should be refreshed before later collection
mutations. `remove_method` identifies `collections.remove.start` for appended
collections and `profiles.remove.start` for standalone collection profiles;
`resume_method` identifies an incomplete standalone installation that can be
continued headlessly, and `update_method` identifies an installed standalone
collection that can move to another revision.
Corrupt collection records are skipped and reported in `warnings` without
hiding valid installations from the same profile.

Download all Nexus archives and pinned Thunderstore packages for a collection
without installing them:

```json
{"jsonrpc":"2.0","id":"collection-download","method":"collections.download.start","params":{"game":"skyrim_se","slug":"x2ezso","revision":13}}
```

This requires Nexus Premium and leaves profiles, staging folders, modlists and
plugin lists unchanged. Existing validated cache entries are reused. The result
reports downloaded, skipped and total counts and lists any off-site URLs that
still require user action. The `thunderstore` result gives that source's own
downloaded and failed counts. Cancelling keeps archives that already completed.

Install a Nexus collection into a new profile with a Nexus Premium account:

```json
{"jsonrpc":"2.0","id":"collection-install","method":"collections.install.start","params":{"game":"skyrim_se","profile":"My Collection","slug":"x2ezso","skipped_optional_file_ids":[12345]}}
```

With a free Nexus account, supply every selected Nexus archive in
`manual_archives`, keyed by the collection's Nexus file ID. The paths must be
absolute and point to existing regular files. For example:

```json
{"jsonrpc":"2.0","id":"collection-manual-install","method":"collections.install.start","params":{"game":"skyrim_se","profile":"My Collection","slug":"x2ezso","manual_archives":{"12345":"/home/user/Downloads/mod-12345.zip"}}}
```

This runs the collection's manual install pipeline without browser prompts.
The job checks the selected file IDs, sizes, available MD5 hashes, and ZIP
integrity before creating the profile. Supplied archives are kept after
installation. For an interactive browser handoff, set
`"manual_interactive":true` instead of `manual_archives`:

```json
{"jsonrpc":"2.0","id":"interactive-install","method":"collections.install.start","params":{"game":"skyrim_se","profile":"My Collection","slug":"x2ezso","manual_interactive":true}}
```

Poll `jobs.events` for a `manual_request` event, or call
`collections.manual.get` with the returned job ID. Each request includes a
`request_id`, Nexus file ID, browser URL, filename, size, and whether the mod
is optional. Download the archive in a browser, then submit its absolute path:

```json
{"jsonrpc":"2.0","id":"select-archive","method":"collections.manual.submit","params":{"job_id":"<job ID>","request_id":1,"path":"/home/user/Downloads/mod-12345.zip"}}
```

Use `"skip":true` instead of `path` only for an optional request. The file's
size, available MD5, and ZIP structure are checked before acceptance. A file
downloaded into a configured download location can also be detected without
submitting a path. Stale request IDs are rejected, and `jobs.cancel` stops a
waiting installation. If a submitted file changes before the installer reads
it, a `manual_rejected` event allows another submission for the same request.
The collection job occupies the job worker while it
waits, so use a browser or another process for manual downloads.
`manual_archives` is also available for resume, update, and append. Supply an
archive for every selected Nexus file ID in the target revision, including
files that may already be installed; the job reuses installed files and keeps
supplied archives. `manual_interactive` is available for local import, resume,
update, and append. Omit both manual options to use automatic Premium downloads.

The slug, revision, and optional file IDs can come from
`nexus.collections.get.start`. The job validates optional selections and the
collection schema before creating the profile. For supported games, a game
version mismatch fails with `collection_game_version_mismatch`; retry with
`"accept_game_version_mismatch":true` after presenting the returned versions
to the user. Installation uses authored FOMOD and BAIN choices where present
and otherwise uses their non-interactive defaults. Cancellation keeps the
partial private profile and ownership record so the desktop resume flow can
recover it. The completed result includes the installed collection record,
verification report, and any off-site URLs that still require user action.
Pinned Thunderstore entries in the manifest are downloaded through the public
Thunderstore service, installed before the Nexus entries, and merged into the
authored mod order. The result's `thunderstore` counts report those entries;
any failed package keeps the collection installation incomplete.

Import a local Amethyst profile into a new private profile:

```json
{"jsonrpc":"2.0","id":"profile-import","method":"collections.import.start","params":{"game":"skyrim_se","profile":"Imported Profile","path":"/home/user/Downloads/profile.amethyst"}}
```

The source path must be absolute and name a real `.amethyst`, ZIP, or JSON
file rather than a symbolic link. The job validates the manifest's game and
optional file IDs before creating the profile. Bundled mods and exported
profile state are restored from archives; pinned Thunderstore packages use the
public service. Automatic Nexus downloads require Premium. A free-account
import can provide `manual_archives` with every selected Nexus file ID mapped
to an archive path, or use `manual_interactive` for browser handoff, as with
`collections.install.start`.
Use `skipped_optional_file_ids` for optional Nexus entries and
`accept_game_version_mismatch` after presenting a reported version mismatch.
The result includes Nexus, bundled, and Thunderstore counts, the verification
report, and off-site URLs that still require manual installation.
Cancelled or incomplete local imports keep their partial profile for diagnosis
and removal, but do not advertise the Nexus resume or update methods; remove
the partial profile before importing it again with the same name.

Resume a cancelled, interrupted, or incomplete standalone installation using
the identity and revision returned by `collections.installed.list`:

```json
{"jsonrpc":"2.0","id":"collection-resume","method":"collections.resume.start","params":{"game":"skyrim_se","profile":"My Collection","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>"}}
```

The job rechecks the installation record while holding the game mutation lock,
keeps already installed matching files, and preserves the profile's current mod
and plugin state while filling missing work. The profile must use
profile-specific mods, must be unlocked and restored, and cannot currently be a
profile-group member. A stale revision fails with `collection_state_changed`.
An interrupted revision update uses a separate recovery path and currently
fails with `collection_update_pending`.

Update a standalone collection to a specific revision returned by
`nexus.collections.get.start`:

```json
{"jsonrpc":"2.0","id":"collection-update","method":"collections.update.start","params":{"game":"skyrim_se","profile":"My Collection","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>","target_revision":13,"confirm":true}}
```

Omitting `skipped_optional_file_ids` preserves the saved optional selection;
provide the complete desired list when the new revision changes its options.
The job keeps the old revision's files until the replacement verifies, carries
the enabled and locked state of replaced mods, and removes only folders that
the collection owns exclusively. Its result reports replacements, additions,
retained folders, and safe retirements. Cancellation or failure keeps the
`previous` revision metadata so the same target revision can be retried with a
fresh `state_revision`. Updates have the same private-profile, lock, deployment,
and profile-group restrictions as resume.

Append a collection to an existing private profile:

```json
{"jsonrpc":"2.0","id":"collection-append","method":"collections.append.start","params":{"game":"skyrim_se","profile":"default","slug":"x2ezso","overwrite_existing":false,"skip_existing":true,"confirm":true}}
```

The profile must use profile-specific mods, must be unlocked and restored, and
cannot be a profile-group member. Existing mods with the same Nexus identity
are reused. `skip_existing` keeps those reused mods in their existing profile
positions; otherwise the collection can move them into its authored order.
When a different mod already uses the collection's preferred folder name,
`overwrite_existing:false` installs a distinctly named copy, while
`overwrite_existing:true` explicitly replaces that folder. The two settings
are independent. Cancellation retains the partial appended-collection record,
so `collections.remove.plan.start` can preview safe cleanup of exclusively
owned files. A collection with the same slug cannot be appended twice.

Add a completed standalone collection profile to an existing profile group:

```json
{"jsonrpc":"2.0","id":"collection-group","method":"collections.group.start","params":{"game":"skyrim_se","profile":"My Collection","target":"Combined","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>"}}
```

To create a new group with another profile, set `target` to that profile and
provide `group_name`. The collection profile is the highest-priority member.
`ini_source` can select either member when their profile INIs conflict, and
`overwrite_excluded` can exclude either member's Overwrite and Root Folder
files. The operation rechecks the collection state and installation report
while holding the game mutation lock, materializes the group, verifies every
group mod through Filegraph, and rolls back a failed existing-group update.

Enable or disable the mods introduced exclusively by one appended collection:

```json
{"jsonrpc":"2.0","id":"collection-state","method":"collections.set_enabled.start","params":{"game":"skyrim_se","profile":"default","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>","enabled":false}}
```

The requested state is explicit, so retrying with a freshly listed revision is
safe. Shared, adopted, personal, and locked mods are left unchanged and counted
in `skipped`. The profile must be unlocked and restored. A stale revision fails
with `collection_state_changed`; success returns the changed mod names and the
updated installation record.

Prepare an appended-collection removal before asking the user to confirm it:

```json
{"jsonrpc":"2.0","id":"collection-remove-plan","method":"collections.remove.plan.start","params":{"game":"skyrim_se","profile":"default","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>"}}
```

The completed job returns the exact `delete` and `retain` name lists plus a
`plan.revision`. Shared, adopted, manual, symlinked, and uncertain folders are
retained. Submit both revisions and an explicit confirmation to remove it:

```json
{"jsonrpc":"2.0","id":"collection-remove","method":"collections.remove.start","params":{"game":"skyrim_se","profile":"default","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>","plan_revision":"<revision from the removal plan>","confirm":true}}
```

The removal job recomputes the plan while holding the game mutation lock. It
fails with `collection_state_changed` or `collection_removal_plan_changed` if
the preview is stale, so the client can display a fresh plan before asking for
confirmation again. The host profile and shared download archives are kept.

Restore the authored order of one installed collection:

```json
{"jsonrpc":"2.0","id":"collection-reset","method":"collections.reset_load_order.start","params":{"game":"skyrim_se","profile":"default","collection_id":"<id from inventory>","state_revision":"<state_revision from inventory>"}}
```

For a collection profile that contributes to a profile group, add the group's
name as `"group":"Combined"`; both the member and materialized group order are
updated together. The method moves only the selected collection's exclusively
owned mods and plugins through the existing slots, keeping unrelated content
in place. It uses the collection's Amethyst order snapshot when one is saved or
cached, otherwise it uses the stored manifest. A missing stored manifest is
fetched from Nexus when credentials are available. The member and group must
be unlocked and restored, and a stale record fails with
`collection_state_changed`.

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
time, symlink and hidden state, and whether that archive is already installed in the
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

`downloads.locations.get` returns the global scan `settings` object with
`extras`, `default_enabled`, and `cache_enabled`, plus the system Downloads
folder path and a `revision`. Submit that complete object and revision to
`downloads.locations.set.start` to change it. Extra folders must be distinct
absolute paths; unavailable folders may remain configured for later use. A
concurrent change fails with `stale_download_locations`.

`downloads.list` also returns `hidden_revision`. To hide or show archives in
the desktop Downloads tab, pass that revision, the selected opaque archive
IDs, and `hidden: true` or `false` to `downloads.hidden.set.start`. This changes
only the shared visibility preference; the files stay on disk and remain in
`downloads.list`. A concurrent visibility change fails with
`stale_hidden_archives`. Both settings are shared across games and with the
desktop application.

To move archives, choose a destination location ID and one to 64 archive IDs
from `downloads.list`, then call `downloads.move.plan.start`. The plan lists
the source and destination of each archive, its sidecars, and every existing
destination file that would be replaced. Pass the same selection, the plan's
`revision`, and `confirm: true` to `downloads.move.start`. The default
`if_exists: "fail"` preserves every destination collision; `"replace"` applies
the reviewed replacements. A stale plan fails with
`download_move_plan_changed`. Archives already in the destination are skipped.
Cross-filesystem moves copy to temporary files before changing destinations;
same-filesystem moves link and then remove the source. Both preserve Nexus
identity sidecars and hidden state, and report completed moves or recoverable
backups if a later file operation fails. Callers cannot choose an arbitrary
destination path.

To delete downloaded archives, submit one to 64 IDs from `downloads.list` to
`downloads.remove.plan.start`. The completed job returns a plan with archive
paths, installed status, sidecar paths, total bytes, and a `revision`. Review
that plan, then pass the same IDs, `plan_revision`, and `confirm: true` to
`downloads.remove.start`. This permanently deletes the files from the shared
download locations; it does not uninstall any mods already installed from
them. A modified archive or sidecar fails with `download_removal_plan_changed`
before deletion; a missing archive returns `archive_not_found`. If a later file
changes or deletion fails after earlier files were removed, the job error
reports the paths already removed. Callers cannot
submit arbitrary paths.

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
- Scripted C# FOMODs and unparseable XML FOMODs have no usable choices in
  Amethyst. Inspection reports `fallback_policy:"verbatim"`; set
  `installer_policy:"verbatim"` to copy the archive files as-is, matching the
  desktop installer fallback. Neither script nor XML choices run. The default
  `reject` policy returns `installer_unsupported`, and `verbatim` is rejected
  for ordinary installers.
- For BAIN, the `installer_choices_required` error includes ordered
  `subpackages` with their exact names, labels, default selections, and file
  counts. Retry with `bain_selected` set to the desired names, including an
  empty array to select none. Unknown names are rejected before staging. Do not
  combine `bain_selected` with `installer_policy: "defaults"`.
- For FOMOD, the same error includes ordered steps, groups, option names and
  descriptions, conditions, and a `defaults` selection. Submit
  `fomod_selections` as `{step_index: {group_name: [option_names]}}`, using the
  string indexes and exact names returned. Include every group of each visible
  step, with `[]` for no choice, and omit hidden steps. Amethyst reevaluates
  visibility, option availability, required choices, and group constraints
  against the current profile before staging. Do not combine explicit choices
  with BAIN choices or `installer_policy: "defaults"`.
- For RE Engine bundles, the error includes ordered groups, option folder IDs,
  labels, and a `defaults` array. Submit `bundle_selected` with the exact folder
  IDs to activate, including an empty array to select none where groups allow
  it. Select-one groups require exactly one real option, and label entries
  cannot be selected. The explicit selection overrides choices saved from an
  earlier installation. Do not combine it with other installer choices or
  `installer_policy: "defaults"`.
- An optional `name` selects the destination mod-folder name. It must be one
  safe path component. Without it, Amethyst applies its normal Nexus metadata,
  archive-name, and game-specific naming rules.

For example, explicitly replace a mod while accepting installer defaults:

```json
{"jsonrpc":"2.0","id":"install-defaults","method":"downloads.install.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>","name":"SkyUI","installer_policy":"defaults","if_exists":"replace"}}
```

Inspect an archive before installing to get its proposed name and destination,
installer type, support status, and available choices. The job result's
`installer.choices` uses the same selection details as
`installer_choices_required`. Inspection extracts to temporary space and does
not install files. It can be cancelled during extraction; repeat `name` on the
install call if you want that exact destination name.

```json
{"jsonrpc":"2.0","id":"inspect","method":"downloads.inspect.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>"}}
```

For a BAIN package, use the inspection result's subpackage names, then submit
the desired selection:

```json
{"jsonrpc":"2.0","id":"install-bain","method":"downloads.install.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>","bain_selected":["00 Core","10 Optional"]}}
```

For a FOMOD, the `defaults` map returned by `installer_choices_required` is a
valid starting point. Change the desired option names and supply the resulting
map explicitly:

```json
{"jsonrpc":"2.0","id":"install-fomod","method":"downloads.install.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>","fomod_selections":{"0":{"Texture Quality":["High"]}}}}
```

For an RE Engine bundle, use the folder IDs returned by
`installer_choices_required`:

```json
{"jsonrpc":"2.0","id":"install-bundle","method":"downloads.install.start","params":{"game":"resident_evil_village","profile":"default","archive_id":"archive_<sha256>","bundle_selected":["01 Main","03 Optional"]}}
```

A separate-folder multi-mod archive lists every destination in the inspection
result's `mods` array. Each folder becomes an independent mod. Pass
`"installer_policy":"defaults"` to install all of them; `if_exists` defaults
to `fail` if any destination already exists, or can be set to `replace` for
all existing destinations. The install result includes a `mods` array with
each installed name, path, enabled state, and replacement status; `mod` remains
the first installed entry.

The job uses the normal toolkit-neutral extraction, structure detection,
staging, metadata, filegraph, modlist, and plugin registration pipeline. The
source archive is retained. Managed Nexus identity sidecars preserve the exact
domain, mod ID, and file ID in the installed `meta.ini`, including offline
installs. Symlink archives and unsafe destinations are rejected.

A successful result contains the retained archive, installed mod name/path and
enabled state, installer type/policy (`explicit` for supplied FOMOD, BAIN, or
bundle choices),
and whether an existing mod was replaced.
Progress covers archive lookup, extraction, file copying, and indexing. Install
jobs are not cancellable once accepted because cancellation during staging could
leave profile state partially updated.

Install directly into a real profile. Profile groups require an ownership
choice, so the method rejects a group and returns its member names; call it
again with the intended member profile. Ordinary Fluffy multi-mod archives use
the selectable bundle path. When a plain archive has an unrecognised structure,
the non-interactive installer follows
its existing fallback and installs the layout as-is.
For FOMODs, accepted defaults are saved as the effective installer choices and
used for later dependency/rerun checks. An unmet FOMOD module prerequisite
finishes with `installer_dependencies_missing` before replacing any mod files.

### Reinstalling from a recorded archive

Reinstall one existing mod without making a client reproduce Amethyst's
metadata and profile-group rules:

```json
{"jsonrpc":"2.0","id":"reinstall","method":"mods.reinstall.start","params":{"game":"skyrim_se","profile":"default","mod":"SkyUI","confirm":true}}
```

The job resolves the exact, case-sensitive mod entry again under the shared
operation lock. It searches enabled download locations for the filename saved
in the installed `meta.ini`; exact Nexus, Thunderstore, or mod.io identities are
used as fallbacks when the archive was renamed. A conflicting archive source identity
is rejected even if its filename matches. The method never accepts an
unrelated archive as a reinstall source.

If more than one retained archive matches, the job finishes with
`reinstall_archive_ambiguous` and returns safe candidate summaries. Choose one
of those opaque IDs explicitly:

```json
{"jsonrpc":"2.0","id":"reinstall-picked","method":"mods.reinstall.start","params":{"game":"skyrim_se","profile":"default","mod":"SkyUI","archive_id":"archive_<sha256>","confirm":true}}
```

An explicit archive must still match the recorded filename or an exact saved
source identity. Managed Thunderstore/mod.io sidecars and archive integrity are
rechecked after preparation. Missing and mismatched archives produce `archive_not_found`
and `reinstall_archive_mismatch`; no staged files are changed in either case.
If the original archive is gone but exact Nexus IDs are available through
`mods.list`, download that same file first with `nexus.download.start`, refresh
`downloads.list`, then retry with its `archive_id`.

The reinstall forces the existing folder name, silently replaces that folder,
and preserves its modlist position, enabled state, root-folder/collection
ownership, endorsement state, ignored Nexus requirements, and independent
Thunderstore/mod.io identities and update flags. Source metadata is verified
before installation succeeds. A profile-group entry is routed back to its real member-profile owner
and the group is refreshed afterward. In shared staging, replacing a folder
still affects every profile that uses it. `confirm` must be true because the
existing folder is replaced. The previous folder is retained until installation
succeeds, and is restored if staging fails.

Installer choices use the same policy as `downloads.install.start`:
`installer_policy` defaults to `reject`; set it to `defaults` to explicitly
accept recommended/default FOMOD, BAIN, or bundle selections, or supply the
corresponding explicit choices. Bundle replacements carry saved selections
when explicit choices are omitted. Scripted or unparseable FOMODs require the
explicit `verbatim` policy to reinstall files without installer choices. A
separate-folder multi-mod archive cannot be used by this
one-mod reinstall method; use `downloads.install.start` to install all folders
after inspecting the archive. The source archive is retained, and the result
identifies both the requested profile and
the actual owner profile used for the reinstall.

### Changing installed Nexus versions

Read the available versions of an installed mod:

```json
{"jsonrpc":"2.0","id":"versions","method":"mods.versions.get.start","params":{"game":"skyrim_se","profile":"default","mod":"SkyUI"}}
```

Poll the job for `result.version_catalog` and `rate_limits`. The catalogue
includes the installed domain/mod/file identity, version, requested mod,
resolved owner, affected profiles, and `revision`. Files include descriptions,
categories, sizes, upload times, and `installed`, `recommended`, and
`older_match` flags. Name matching and category ordering use the same helpers
as the desktop Change Version window. A recommendation is a filename-family
match; it does not establish compatibility with the other installed mods.

Choose an exact `file_id` from that catalogue:

```json
{"jsonrpc":"2.0","id":"change-version","method":"mods.version.change.start","params":{"game":"skyrim_se","profile":"default","mod":"SkyUI","revision":"<catalogue revision>","file_id":12345,"confirm":true}}
```

Omitting `source` selects Nexus; `source:"thunderstore"` and `source:"modio"` use the workflows below.
The job checks the revision, owner, and source again under the game operation
lock, verifies that the file belongs to the mod's current Nexus listing, and
rechecks the installed state after archive preparation. `stale_mod_version`
requires a new catalogue request. Missing source identity or unavailable
affected-profile routing stops the change before replacing files.

An exact domain-qualified Nexus identity in the managed Downloads inventory
allows reuse of a retained archive. Supply `archive_id` to select one explicitly;
`mod_version_archive_ambiguous` returns choices when several retained archives
identify the same file. An identity mismatch produces
`mod_version_archive_mismatch`. Identity-less archives cannot be used for this
workflow. Archive and identity-sidecar changes during preparation produce
`mod_version_archive_changed`.

When no matching archive is retained, the shared Nexus downloader acquires it.
Automatic downloads require Premium. Free accounts may supply a keyed
`nxm_url` for the exact domain/mod/file instead of `archive_id`. NXM credentials
are excluded from public job options. The API login is still needed for
catalogue and metadata requests.

Installer policies and explicit FOMOD, BAIN, and bundle selections follow
`downloads.install.start`. A choices-required failure retains the archive
and reports its `archive_id` so clients can call `downloads.inspect.start`,
then retry with selections and a current catalogue revision.
Separate-folder multi-mod archives are rejected by this single-mod workflow.

The replacement keeps the folder name, modlist position, enabled and locked
state, root-folder layout, collection ownership, endorsement, and ignored
requirements. The chosen file supplies fresh version/source metadata and clears
the old Nexus update ignore. A Profile Group mod resolves to its member owner;
shared staging refreshes every profile using the same folder. Independent
private copies are unaffected. Plugin synchronization preserves existing
individual disabled states, retains plugins provided by other enabled mods,
registers newly available plugins, and removes proven stale plugin names.

The job result adds `version_change` with the chosen file/version, owner, and
`synced_profiles`. Installation failure restores the previous folder and saved
profile/installer state; `rolled_back:true` confirms that recovery succeeded.
`install_rollback_failed` retains a `backup_path` to the previous files.
`mod_version_rollback_failed` reports incomplete profile recovery.
`mod_version_sync_failed` reports `state_persisted:true`, the install result,
and profile failures when the new version was installed but routing or plugin
synchronization could not finish.

Version-change jobs are not cancellable once accepted. For a pausable or
cancellable acquisition step, download with `nexus.download.start` first,
then select its retained archive for the version change.

### Thunderstore versions and downloads

Thunderstore uses namespace/package/version identities. It requires no Nexus
login or Premium account. A game must declare a Thunderstore community.

```json
{"jsonrpc":"2.0","id":"ts-versions","method":"mods.versions.get.start","params":{"game":"riskofrain2","profile":"default","mod":"Example","source":"thunderstore"}}
```

The job's `version_catalog` has the same owner, affected-profile, and revision
fields as Nexus, plus `community`, `namespace`, `package`, installed version/full
name, a `versions` array, and `recommended_version`. Versions are sorted by
numeric version rather than lexical order. Each row includes its version/full
name, installed/recommended flags, date, download count, and dependency count.
A package without an installed mod can be queried with
`thunderstore.packages.versions.start`, using `game`, `namespace`, and `package`.

```json
{"jsonrpc":"2.0","id":"ts-change","method":"mods.version.change.start","params":{"game":"riskofrain2","profile":"default","mod":"Example","source":"thunderstore","revision":"<catalogue revision>","version":"2.0.0","confirm":true}}
```

Use `version`, not `file_id` or `nxm_url`, for Thunderstore. The selected version
must still appear in the live catalogue. It uses the same installer policies,
owner resolution, state preservation, replacement backup, profile/plugin refresh,
and typed rollback/synchronization errors as the Nexus workflow.
Only the selected source's update ignore is cleared; independent Nexus, Thunderstore,
and BG3 mod.io metadata is preserved during version changes.

The default `dependency_policy:"reject"` stops with
`thunderstore_dependencies_required` when an affected profile enables the mod
without an enabled provider at or above a required dependency version. The error
lists each missing namespace/package/version and profile. Disabled copies do
not force dependency installation. Download and install the required packages
first, then read a new catalogue revision and retry. Dependencies are rechecked
before staging. `dependency_policy:"ignore"` explicitly installs the chosen
version without adding or enabling dependencies; the result reports
`dependencies_not_installed`. New dependency packages are never added implicitly.

Download a dependency or any known package version into managed Downloads:

```json
{"jsonrpc":"2.0","id":"ts-download","method":"thunderstore.download.start","params":{"game":"riskofrain2","namespace":"ExampleOwner","package":"ExampleDependency","version":"1.0.0"}}
```

The standalone download checks the package's community and retrieves its
version metadata. Poll the job for `archive` and `thunderstore` details, then
use `downloads.list` and `downloads.install.start` with its opaque archive ID.
Installation verifies the identity again after preparation and stamps the
Thunderstore metadata, including dependency pins. These downloads support
`jobs.pause`, `jobs.resume`, and `jobs.cancel`; partial files are removed on
cancellation. Restart recovery of interrupted downloads currently supports
Nexus only. Version-change jobs remain non-cancellable, including acquisition
within the change; use the separate download method for acquisition control.

Managed downloads store a `.thunderstore.json` identity/metadata sidecar.
`downloads.list` exposes its safe identity as `thunderstore`; archive move and
removal methods include this sidecar. A retained archive may be selected with
`archive_id`. Without a sidecar, reuse requires the canonical
`namespace-package-version.zip` filename. In both cases, the archive's declared
size and root `manifest.json` name/version must match. Conflicting sidecars,
changed files, mismatched manifests, and ambiguous retained matches stop before
replacement. Thunderstore supplies no archive checksum, so size and manifest
checks cannot establish a cryptographic match to a published download.

### BG3 mod.io versions and downloads

Baldur's Gate 3 supports `source:"modio"` in the same installed-version methods:

```json
{"jsonrpc":"2.0","id":"modio-versions","method":"mods.versions.get.start","params":{"game":"baldurs_gate_3","profile":"default","mod":"Example","source":"modio"}}
```

The job requires saved mod.io credentials (an API key and assigned API path),
reads every page of published Windows files, and returns `game_id:6715`,
`mod_id`, installed file/version, owner/affected profiles, a revision, and a
`files` array. File rows include ID, version, filename, sizes, MD5, upload
timestamp, changelog, and installed/recommended flags. `recommended_file_id`
is the mod's current live Windows file, which may differ from the newest upload.
`modio.mods.files.start` returns the catalogue for a known `mod_id` without
requiring an installed mod.

```json
{"jsonrpc":"2.0","id":"modio-change","method":"mods.version.change.start","params":{"game":"baldurs_gate_3","profile":"default","mod":"Example","source":"modio","revision":"<catalogue revision>","file_id":12345,"confirm":true}}
```

Choose an exact `file_id`; `version`, `nxm_url`, and Thunderstore dependency
policies are not accepted. The job verifies current catalogue membership,
fetches fresh file details, and reuses or downloads an archive whose size and
MD5 match. The shared installer policies, owner resolution, rollback, and
affected-profile routing refresh apply. The new `[modio]` metadata records
the selected file and the current live file; choosing an older version can
therefore still report an available update. Independent Nexus/Thunderstore
metadata and flags are retained. BG3 generates `modsettings.lsx` during
deployment; version replacement stages the mod and refreshes routing.

For controlled acquisition before installation or replacement:

```json
{"jsonrpc":"2.0","id":"modio-download","method":"modio.download.start","params":{"game":"baldurs_gate_3","mod_id":123,"file_id":12345}}
```

Downloads use a freshly obtained signed URL and a separate HTTP session without
the API key. Signed URLs are omitted from job results, logs, and retained
metadata. `jobs.pause`, `jobs.resume`, and `jobs.cancel` are supported;
cancellation removes partial files. Size or MD5 failure leaves an existing
cached archive untouched. Process-restart recovery currently supports Nexus
downloads only. Acquisition inside a version-change job remains non-cancellable.

Archives are named `modio-6715-mod_id-file_id.zip` and accompanied by a
`.modio.json` sidecar containing the source identity, size/MD5, and source
metadata. `downloads.list` exposes a safe `modio` identity; move/removal
operations include the sidecar. `downloads.install.start` and reinstallation
verify retained identity and integrity and stamp metadata before completing.
An archive with conflicting Thunderstore/mod.io metadata sidecars is rejected
with `install_source_ambiguous`.

Use `archive_id` to choose among multiple retained matches or select an older
manually downloaded archive. A selected legacy archive without a sidecar must
still match the fresh API size and MD5. Sidecar conflicts, ambiguous matches,
unsafe paths, and archive changes stop before replacement. Missing credentials,
unavailable mods/files, failed requests, expired download URLs, and unverifiable
files are reported as typed job errors. These methods support BG3 only.

List mods:

```json
{"jsonrpc":"2.0","id":4,"method":"mods.list","params":{"game":"skyrim_se","profile":"default"}}
```

Entries use `highest_priority_first` order. `index` is the zero-based row in
`modlist.txt`, including separators. Priority zero is the lowest real mod and
the largest number is the highest; separators have a null priority. Real mods
include enabled and locked state, staging-folder presence, installed metadata,
update state, and Nexus, Thunderstore, or BG3 mod.io identity when available. A stale
`modlist.txt` entry remains visible with `folder_exists` set to false.

Nexus metadata includes the installed and latest file IDs and versions.
`requirements.nexus.all` exposes the saved requirement list. `missing` is
computed against enabled providers in the requested profile, even when saved
`missingRequirements` metadata is stale. `has_missing` applies the profile's
owner ignores and the mod's individual requirement ignores. The row also
includes `ignored`, `ignored_requirement_ids`, `graph_source`, and metadata
availability/error fields. The inventory's
`requirement_revision` covers the same state as `mods.requirements.get`.

Read methods return `operation_in_progress` while this service is changing game
state. Continue polling the active job and retry the read after it completes.

Inspect a mod's file conflicts:

```json
{"jsonrpc":"2.0","id":"conflicts","method":"mods.conflicts.inspect.start","params":{"game":"skyrim_se","profile":"default","mod":"SkyUI","section":"wins","limit":500}}
```

The job returns counts for `wins`, `losses`, and `uncontested`, a Filegraph
summary, and a page of rows from the requested section. `wins` rows include
`mods_beaten` as an array; `losses` rows include the `winner`. All rows give
the displayed file path and whether it is an archive member. Winning rows also
mark loose files that beat archive members with `archive_related`. The display
uses the same routing, archive, and per-game blacklist rules as Show Conflicts
in the desktop application.

`section` defaults to `wins`; `offset` defaults to 0 and `limit` to 500 (maximum
5000). Use `next_offset` to request the next page and pass the returned
`revision` on later requests. A changed graph fails with `stale_conflicts` so
pages from different generations are not mixed. Inspection may reconcile the
profile's Filegraph cache but does not deploy or change staged mod files.

### Edit installed bundle options

For an installed RE Engine bundle, `mods.bundle.get` returns its current
`selected` folder IDs and ordered `groups`, including each option's label and
selected state. It also returns a `revision` token for detecting later changes.
For a profile group, `owner_profile` and `owner_mod` identify the member whose
staged files and saved choices are being read. `recovery_pending` indicates an
interrupted edit that a subsequent set job can repair.

To change the bundle without reinstalling its archive, pass that revision and
the complete desired selection to `mods.bundle.set.start`:

```json
{"jsonrpc":"2.0","id":"bundle-edit","method":"mods.bundle.set.start","params":{"game":"resident_evil_village","profile":"default","mod":"My Bundle","revision":"<revision from mods.bundle.get>","bundle_selected":["02 Alternate"]}}
```

The job checks that the saved choices have not changed, validates folder IDs
and select-one groups, materializes the selected files, and updates Filegraph.
An unchanged selection is a no-op. Changing staged files does not update an
already deployed game; call `deploy.start` to apply the new files. A stale
revision returns `bundle_revision_changed`. If materialization fails, Amethyst
attempts to restore the previous selection and reports whether disk state may
have changed. A retry with the current revision repairs an interrupted edit.

### Read requirements and manage warnings

`mods.requirements.get` derives current requirement status from saved Nexus
metadata and local external-tool, alternative, and substitution rules. It does
not contact Nexus, require a login, or rewrite metadata. Use
`mods.check_updates.start` to fetch a fresh requirement graph.

```json
{"jsonrpc":"2.0","id":"requirements","method":"mods.requirements.get","params":{"game":"skyrim_se","profile":"default","mods":["Combat overhaul"]}}
```

Omit `mods` to inspect the entire profile. A selection limits returned owners;
providers and reverse dependencies are still resolved against the entire
profile. All names are exact, case-sensitive real mod names. Named group
headers and separators are not mods in this inventory.

Each row contains `name`, `enabled`, `domain`, `mod_id`, `metadata_available`,
`metadata_error` (null or a read-error type),
and the following requirement state:

- `all` is the saved complete requirement list, or the legacy missing list when
  no complete graph exists.
- `effective` applies local rules. Each entry has `domain`, `mod_id`, `name`,
  `ignored`, `status`, and `providers`. Status is `enabled`, `disabled`, or
  `missing`. Providers include their real folder name, enabled state, and Nexus
  identity, including accepted alternatives.
- `missing` contains effective requirements without an enabled provider. Ignored
  requirements remain listed.
- `required_by` lists profile mods whose effective graph uses this mod or an
  accepted alternative, along with their enabled state.
- `ignored` suppresses this owner's warning in the selected profile.
  `ignored_requirement_ids` applies individual ignores stored in `meta.ini`.
  `has_missing` reports whether a warning remains after both forms of ignore.
- `graph_source` is `full`, `legacy`, or `none`, identifying the saved field used.
  Cached data can be outdated or incomplete, and `none` does not prove the mod
  has no dependencies. Fetch fresh metadata before assuming a cached graph is
  complete.

Matching uses both Nexus domain and mod ID. A provider in another domain or
outside the profile cannot satisfy a requirement. An enabled duplicate or
alternative can satisfy it. To activate a disabled provider, pass its `name`
to `mods.set_enabled.start`; the next requirement read immediately reflects
that activation. No second network check is needed.

The response includes `revision`, `source: "cached_nexus_metadata"`,
`satisfaction: "enabled_profile_mods"`, and counts for returned mods, owners
with missing requirements, and owners still flagged. The revision includes the
entire profile's mod order/state, identities, effective graphs, and ignores,
even when the response is filtered to selected owners.

```json
{"jsonrpc":"2.0","id":"ignore-requirement","method":"mods.requirements.ignore.set.start","params":{"game":"skyrim_se","profile":"default","mod":"Combat overhaul","revision":"<requirement revision>","requirement_ids":[12345]}}
```

Supply `ignored` to set the selected owner's profile warning, `requirement_ids`
to replace its entire individual ignored-ID list, or both. Set `ignored` false
or pass an empty ID list to clear the corresponding ignores. IDs must be unique
positive integers from that owner's effective requirements or its existing
ignores. Individual IDs belong to the owner's Nexus domain and are stored with
the mod's metadata, so they also apply to profiles sharing that installed mod.
Owner warning ignores remain profile-local.

The job validates the revision under the shared operation, mod-list, profile
state, and metadata locks. Stale requests fail with `stale_mod_requirements`.
If a combined edit cannot save profile state, its metadata change is rolled
back. Unrelated state and metadata keys are preserved. The result returns
`changed` and the updated selected row and revision. Ignore edits do not
enable mods, change load order, or deploy.

### Check mod updates and Nexus requirements

Check every real mod listed in one profile:

```json
{"jsonrpc":"2.0","id":"updates","method":"mods.check_updates.start","params":{"game":"skyrim_se","profile":"default"}}
```

Pass `mods` to check an exact subset instead. An explicit array must contain
between 1 and 4,096 unique, case-sensitive mod-folder names from that profile:

```json
{"jsonrpc":"2.0","id":"updates-selected","method":"mods.check_updates.start","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","SSE Engine Fixes"]}}
```

By default, the job checks Nexus and Thunderstore metadata when the game has a
Nexus domain, or Thunderstore alone for games without one. For Baldur's Gate 3,
it also checks mod.io when a complete API key and path have been saved through
Amethyst. Pass `sources` with any supported values to select them explicitly:

```json
{"jsonrpc":"2.0","id":"updates-ts","method":"mods.check_updates.start","params":{"game":"riskofrain2","profile":"default","sources":["thunderstore"]}}
```

The Nexus check uses the saved login and the existing batched update and
requirement checker. In shared staging, only mods listed in the requested
profile and enabled there count as providers when requirements are evaluated.
A disabled mod, or an unrelated folder used by another profile, cannot satisfy
a dependency. Duplicate enabled providers and local alternative/substitution
rules are respected. The
Thunderstore check uses each mod's saved package identity and needs no login.
The mod.io check is available for Baldur's Gate 3 with saved credentials. It
also identifies older staged packages from their pak metadata when they have no
mod.io ID in `meta.ini` yet. Mods without metadata for any selected source are
returned in `skipped`. A selection containing no tracked mods succeeds without
making a network request.

The result keeps Nexus updates in `updates` and requirements in
`missing_requirements`, and reports Thunderstore packages separately in
`thunderstore_updates` and `thunderstore_unknown`, and BG3 mod.io results in
`modio_updates` and `modio_unknown`. It includes counts, the resolved selection,
nonfatal `source_errors`, and current Nexus rate-limit
information when Nexus was queried. The checkers persist their update state in
the existing `meta.ini` files, so a later `mods.list` reflects the refreshed
state. The job does not download, install, reorder, enable, or deploy anything.
For Nexus updates, use the returned exact IDs with the normal Nexus download
and archive-install methods, or query `nexus.mods.files.start` when no latest
file ID is available.

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

### Manage saves

Start with `saves.locations.start`, then use a returned absolute `path` as the
`location` in `saves.entries.start`. The latter lists at most 500 entries per
page, newest first. A returned directory can be opened by passing its
`entry` as `directory`; the save location itself cannot be deleted. Locations
with manifest file patterns show only matching top-level entries.

```json
{"jsonrpc":"2.0","id":1,"method":"saves.locations.start","params":{"game":"skyrim_se","profile":"default"}}
{"jsonrpc":"2.0","id":2,"method":"saves.entries.start","params":{"game":"skyrim_se","profile":"default","location":"/path/from/locations"}}
```

Use an entry's `revision` when copying, moving, or removing it. An entry whose
file identity or modification time changed fails with `stale_save_entry`.
`saves.transfer.start` copies by default;
`move: true` or `overwrite: true` requires `confirm: true`. Its destination
must be an existing profile of the same game, and the game must support
profile saves. `saves.remove.start` also requires confirmation.

Export writes to an absolute `.zip` path and refuses an existing archive
unless `overwrite: true` is supplied. Import requires `confirm: true`, accepts
only a location returned for the selected game and profile, and moves any
current saves to a `before-import` backup first. Manifest patterns also
constrain the archive's top-level files. These jobs share the game's operation
lock with other headless mutations; they are not cancellable mid-transfer.

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

### BG3 mod.io credentials

The headless and desktop applications share Baldur's Gate 3 mod.io credentials.
Read their status without exposing the API key:

```json
{"jsonrpc":"2.0","id":"modio-status","method":"modio.credentials.get","params":{}}
```

The result reports `configured`, `api_key_saved`, `api_path`,
`api_path_missing`, and `api_path_invalid`. An older key saved without an API
path is incomplete. To configure mod.io entirely headlessly, obtain the
read-only key and assigned API
path from [mod.io API Access](https://mod.io/me/access), then submit both:

```json
{"jsonrpc":"2.0","id":"modio-save","method":"modio.credentials.set.start","params":{"api_path":"https://u-123.modapi.io/v1","api_key":"<read-only key>"}}
```

The job tests the connection before saving it. Its options, events, and result
never include the API key. A failed verification leaves the previously saved
credentials in place. `api_path` must be the HTTPS `u-<number>.modapi.io/v1` or
`g-<number>.modapi.io/v1` path assigned by mod.io. Clear both saved values with:

```json
{"jsonrpc":"2.0","id":"modio-clear","method":"modio.credentials.clear.start","params":{"confirm":true}}
```

### Discovering Nexus collections

Browse the collection catalogue for a game's primary Nexus domain:

```json
{"jsonrpc":"2.0","id":"collection-search","method":"nexus.collections.search.start","params":{"game":"skyrim_se","sort":"downloads","limit":20,"offset":0}}
```

Add `query` for a case-insensitive collection-name search. `sort` accepts
`downloads`, `endorsements`, `rating`, or `recent`; `limit` must be from 1 to
50. Adult collections are removed from each returned page unless
`include_adult` is true. `page.returned` counts the unfiltered Nexus page,
`page.visible` counts returned entries, and `page.adult_hidden` reports the
difference. Use `page.next_offset` even when filtering leaves a short visible
page.

Fetch the latest published detail for a returned slug:

```json
{"jsonrpc":"2.0","id":"collection-detail","method":"nexus.collections.get.start","params":{"game":"skyrim_se","slug":"x2ezso"}}
```

Supply a positive `revision` to inspect that revision. The result includes the
available revisions, schema support, game versions, collection size, and exact
Nexus mod/file identities. Set `include_manifest` to true when the client needs
the authoritative load order, optional and off-site sources, installer choices,
or bundled metadata:

```json
{"jsonrpc":"2.0","id":"collection-manifest","method":"nexus.collections.get.start","params":{"game":"skyrim_se","slug":"x2ezso","revision":12,"include_manifest":true}}
```

Manifest retrieval may download the collection archive and cache it in the
game's managed download cache. Unsupported schemas fail manifest retrieval
with `collection_schema_unsupported`; missing or invalid archives fail with
`collection_manifest_unavailable`.

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

Pause and resume an in-process transfer with `jobs.pause` and `jobs.resume`:

```json
{"jsonrpc":"2.0","id":"pause-download","method":"jobs.pause","params":{"job_id":"<download job id>"}}
{"jsonrpc":"2.0","id":"resume-download","method":"jobs.resume","params":{"job_id":"<download job id>"}}
```

The job snapshot reports `pausable` and `paused`, and pause changes appear in
`jobs.events`. A pause takes effect at a transfer boundary and belongs to the
running job. Saved transfers interrupted by an exhausted network retry or a
process restart can be recovered separately as described below. Cancelling a
paused job wakes it and cleans up the claimed partial archive. Closing the stdio service resumes paused jobs so
accepted downloads can finish before the process exits.

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

### Recover interrupted downloads

List saved transfers after restarting the service:

```json
{"jsonrpc":"2.0","id":"partials","method":"downloads.interrupted.list","params":{"game":"skyrim_se","profile":"default"}}
```

The result contains enabled `locations` and `downloads`. Each download reports
an opaque `id`, a per-item `revision`, `name`, `partial_path`, `metadata_path`,
`location_id`, `location_kind`, Nexus `domain`, `mod_id`, `file_id`,
`downloaded_bytes`, `total_bytes`, and `requires_nxm`. A zero total means the
size is unknown. Active or locked transfers, malformed metadata, symlinked
partial files or metadata, and transfers for unrelated game domains are
excluded. Partials remain absent from the ordinary archive/install inventory.
The profile supplies the manager context; download locations are shared.

Resume the exact selected partial:

```json
{"jsonrpc":"2.0","id":"recover","method":"downloads.interrupted.resume.start","params":{"game":"skyrim_se","profile":"default","download_id":"interrupted_<sha256>","revision":"<item revision>"}}
```

Premium accounts can obtain a new signed link automatically. Free accounts
must supply a fresh `nxm_url` from Download with Manager for the saved domain,
mod, and file. Expired or mismatched links are rejected. Keys and signed URLs
are excluded from saved resume metadata and job options. `requires_nxm`
describes the original transfer; a Premium account can recover it without the
old NXM grant.

The shared downloader verifies the revision while claiming the partial,
validates HTTP Range responses and server validators, and restarts from zero
when necessary. Retries keep using the selected partial. A completed cached
archive or a sibling partial never replaces the selection. An occupied final
archive or identity sidecar blocks resume and leaves the partial intact.

Resume jobs support `jobs.pause`, `jobs.resume`, and `jobs.cancel`. Cancelling
before the partial is claimed leaves it intact; cancellation after claiming
removes that partial and its resume metadata. Exhausted transient retries
retain it for a later attempt. Completed archives and sibling partials remain.

Discard an interrupted transfer with explicit confirmation:

```json
{"jsonrpc":"2.0","id":"discard","method":"downloads.interrupted.discard.start","params":{"game":"skyrim_se","profile":"default","download_id":"interrupted_<sha256>","revision":"<item revision>","confirm":true}}
```

Discard checks the revision under the same download resource lock and a
partial-file lock. It deletes the selected partial and resume metadata. The
job can be cancelled while waiting for the resource lock; deletion itself is
one short operation. Re-list after `download_interrupted_changed`,
`download_interrupted_unavailable`, or `download_interrupted_busy`.
`download_interrupted_destination_exists` requires resolving the destination
collision. A filesystem deletion failure returns
`download_interrupted_discard_failed`; inspect the remaining files before
retrying.

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
cannot be copied through this method.
Do not combine `copy_from` and `profile_specific_mods`.

Create a profile group from existing profile-specific profiles:

```json
{"jsonrpc":"2.0","id":6,"method":"profiles.groups.create.start","params":{"game":"skyrim_se","name":"Combined","members":["Handheld","Travel"],"ini_source":"Handheld","overwrite_excluded":["Travel"]}}
```

Members are listed in priority order, highest first. `ini_source` selects the
member whose copy wins when profile INI filenames conflict; by default, the
highest-priority contributor wins. `overwrite_excluded` excludes selected
members' Overwrite and Root Folder files from the group's initial merge. Group
members must use profile-specific mods, and groups cannot be nested. The result
returns the created profile, members, INI source, and exclusions.

Replace a group's ordered members:

```json
{"jsonrpc":"2.0","id":10,"method":"profiles.groups.set_members.start","params":{"game":"skyrim_se","profile":"Combined","members":["Travel","Handheld"],"expected_members":["Handheld","Travel"]}}
```

This accepts the complete desired list, highest priority first, including an
empty list. `expected_members` must match the ordered `group_members` from a
recent `profiles.list`; a mismatch fails with `profile_group_changed` and
returns the current list. Group-local mods and files remain when all members are removed.
The group must be restored before changing its membership if it is deployed.
The result reports whether the list changed and, when it did, the previous
members. If materialization fails, Amethyst attempts to restore and rematerialize
the previous membership; the job error reports whether that membership was
restored.

Read or change the member mods excluded from a group:

```json
{"jsonrpc":"2.0","id":11,"method":"profiles.groups.exclusions.get","params":{"game":"skyrim_se","profile":"Combined"}}
{"jsonrpc":"2.0","id":12,"method":"profiles.groups.exclusions.set.start","params":{"game":"skyrim_se","profile":"Combined","exclusions":[{"member":"Handheld","mod":"SkyUI"}],"revision":"<revision from get>","confirm":true}}
```

`exclusions` is the complete desired list; send an empty list to restore all
member mods to the group. New entries must exist in that member; an existing
exclusion can be retained after its original folder has been renamed. A moved
mod is excluded automatically when its destination is one of the source group's
members. The exclusion follows the member profile if it is renamed and follows
a version-related folder rename when the old folder disappears. The group must
be restored before changing exclusions if it is deployed.

Convert a shared-pool profile before adding it to a group:

```json
{"jsonrpc":"2.0","id":11,"method":"profiles.convert.start","params":{"game":"skyrim_se","profile":"Handheld","confirm":true}}
```

Conversion copies the profile's listed mods into its private `mods` folder,
hardlinking static assets where possible. The shared pool remains available to
other profiles. The job returns the number of cloned mod folders. Locked or
deployed profiles cannot be converted, and the operation has no reverse method.

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
Completed Wabbajack storage is cleaned up when no profiles reference it.

Rename and removal fail with `profile_deployed` when the profile itself is
deployed or it is a member of the currently deployed group. Run `restore.start`
and retry so deployed links and runtime files can be recovered before their
profile data moves or disappears. Other expected job errors include
`profile_not_found`, `profile_exists`, `reserved_profile_name`,
`profile_protected`, `profile_locked`, `profile_copy_unsupported`, and the
operation-specific `profile_create_failed`, `profile_rename_failed`,
`profile_remove_failed`, or `profile_select_failed`.

List managed Wabbajack installations:

```json
{"jsonrpc":"2.0","id":12,"method":"wabbajack.list","params":{"game":"skyrim_se"}}
```

Omit `game` to scan all configured handlers. Each row includes its installation
ID, status, managed directory, linked profiles, locked profiles, and profile
groups that reference those profiles. `game_matches` is false when the stored
package game names another handler. Incomplete installations are included.

Browse the Wabbajack gallery:

```json
{"jsonrpc":"2.0","id":18,"method":"wabbajack.gallery.start","params":{"game":"skyrim_se"}}
```

The cancellable job returns authored entries with stable gallery IDs, titles,
versions, descriptions, download metadata and availability, plus feed warnings
and a `cached` flag. `refresh: true` fetches current feeds; `cached_only: true`
reads existing feeds without network access. The two flags are mutually
exclusive. An omitted `game` returns entries for all games.

Download a package by the `id` returned from the gallery:

```json
{"jsonrpc":"2.0","id":19,"method":"wabbajack.package.download.start","params":{"entry_id":"<id from gallery>","expected_version":"1.0"}}
```

The cancellable job resolves the ID from cached gallery feeds, verifies the
package size and hash when supplied by the feed, and inspects the downloaded
package. It returns `package_path`, `identity`, and `gallery_id` for preflight
and installation, along with the authored game and profiles.
`expected_version` prevents a changed gallery entry from being downloaded
silently. Cancelled downloads keep
their partial files for a later retry. Run `wabbajack.gallery.start` first if
the feed cache is empty or outdated.

Inspect a local package before choosing its profiles and install settings:

```json
{"jsonrpc":"2.0","id":15,"method":"wabbajack.inspect.start","params":{"package_path":"/path/to/list.wabbajack"}}
```

The job returns the package identity, authored game, profile choices, selected
profile, and archive and directive counts. Invalid packages fail with
`wabbajack_inspect_failed`.

Check a new installation before downloading or reconstructing files:

```json
{"jsonrpc":"2.0","id":16,"method":"wabbajack.preflight.start","params":{"game":"skyrim_se","package_path":"/path/to/list.wabbajack","expected_identity":"<identity from inspection>","installation_name":"my-list","profiles":["Main"]}}
```

`installation_name` is one safe directory name inside the game's managed
`.wabbajack` root. For `mode: "resume"`, `"repair"`, or `"update"`, supply
the `installation_id` from `wabbajack.list` instead; saved profiles, fixes,
setup options, downloads path, and original game roots are reused unless
overridden. `profiles`, `fixes`, `setup_options`, `downloads_path`, `game_roots`
(game name to original directory), and `clear_archives` can be supplied in
either mode. Pass `gallery_id` from `wabbajack.package.download.start` to keep
the gallery link and update metadata; Amethyst checks that a new installation's
package path and hash match that entry. The optional `expected_identity`
rejects a package changed after inspection. A new installation requires an
explicit `profiles` array; use an empty array only when the package has no
authored profiles.

The cancellable job returns `ok`, the resolved request paths and selections,
all checks with their status and resolution, `manual_archives` with exact hash,
size, source URL, and author instructions, additional setup tasks, archive
counts, byte estimates, known list issues, and stage timings. `ok: false` means
the preflight job ran successfully but found blocking checks. Manual checks
identify archives that must be supplied before installation. Package or filesystem failures are
reported as `wabbajack_preflight_failed`.

Import a manually downloaded archive using its `hash` from `manual_archives`:

```json
{"jsonrpc":"2.0","id":18,"method":"wabbajack.archive.import.start","params":{"game":"skyrim_se","package_path":"/path/to/list.wabbajack","expected_identity":"<identity from inspection>","archive_hash":"<hash from preflight>","source_path":"/path/to/downloaded/archive.7z"}}
```

The cancellable job verifies the package identity and exact archive size and
hash, then copies the file into the game's download cache. It reuses a matching
cached copy and never marks a user-supplied archive for automatic cleanup.
Pass the same `downloads_path` as preflight when using a custom cache. Retry
preflight after import.

Install with the same selection after reviewing a passing preflight:

```json
{"jsonrpc":"2.0","id":17,"method":"wabbajack.install.start","params":{"game":"skyrim_se","package_path":"/path/to/list.wabbajack","expected_identity":"<identity from inspection>","installation_name":"my-list","profiles":["Main"]}}
```

The job repeats preflight before installing. It uses the shared Wabbajack
installer and reports the managed installation ID, published profiles,
selected profile, installed file count, and remaining author instructions.
`jobs.cancel` stops it while retaining verified downloads and completed work
for a later `mode: "resume"` request. A preflight error fails the job with
`wabbajack_requirements_failed` and the blocking checks.

Headless installation never waits for an interactive download prompt. If
preflight finds manual archives, the job fails with
`wabbajack_manual_download_required` and their names and hashes. Import the
exact archives or place them in the shared download cache and retry; the
installer verifies their size and hash. The same error applies if an automatic
download falls back to manual assistance during installation. Known broken-list
notices require `accept_known_issues: true` after reviewing `known_issues` in the preflight
result. For updates or repairs, `conflict_policy` defaults to `reject` when
local changes need review; `keep` preserves every conflicting local file and
`author` chooses every authored version. These policies use the installer's
normal backup and publication process.

Remove an installation using the `id` returned by `wabbajack.list`:

```json
{"jsonrpc":"2.0","id":13,"method":"wabbajack.remove.start","params":{"game":"skyrim_se","installation_id":"<id from wabbajack.list>","confirm":true}}
```

Removal deletes the managed installation, its installed mods, Stock Game files,
saved package, work data, backups, local changes, and linked profiles. It prunes
those profiles from groups and restores a deployed profile when necessary.
Locked profiles block removal. Shared download archives and partial downloads
remain. The job result lists removed profiles, changed groups, and any restored
profile. A failed removal reports `wabbajack_remove_failed`.

Reset a Wabbajack profile's mod and plugin order from verified installation
baselines:

```json
{"jsonrpc":"2.0","id":14,"method":"wabbajack.reset_load_order.start","params":{"game":"skyrim_se","profile":"My Wabbajack List"}}
```

Amethyst backs up the profile files before rewriting them and keeps mods and
plugins added after the authored list. The result reports authored and extra
counts. It does not verify that the installed mod folders still exist.

### Manage separators

`mods.list` returns a SHA-256 `revision` of `modlist.txt`. Each separator row
includes its internal `name`, display name, and `separator_state` with collapse,
lock, colour, and deploy-override values. That state has its own `revision`.
The `separator_state.locked` value is the UI lock; a separator's top-level
`locked` reflects its modlist prefix. Pass the mod-list revision to a separator
edit; if another edit changed the list first, the job fails with
`stale_modlist` and returns the current revision. Refresh `mods.list` before
retrying.

```json
{"jsonrpc":"2.0","id":20,"method":"separators.create.start","params":{"game":"skyrim_se","profile":"default","name":"Patches","revision":"<revision from mods.list>","position":{"before":"SkyUI"}}}
```

`position` accepts exactly one `before` or `after` anchor named by a mod or
separator. Omit it to prepend the separator. A create that would split a saved
mod group fails with `separator_splits_mod_group`. Separator names must be
unique against existing mod and separator display names.

Rename or remove by the internal name returned by `mods.list`, such as
`Patches_separator`:

```json
{"jsonrpc":"2.0","id":21,"method":"separators.rename.start","params":{"game":"skyrim_se","profile":"default","separator":"Patches_separator","new_name":"Compatibility patches","revision":"<current revision>"}}
{"jsonrpc":"2.0","id":22,"method":"separators.remove.start","params":{"game":"skyrim_se","profile":"default","separator":"Compatibility patches_separator","revision":"<current revision>","confirm":true}}
```

Use the new revision returned by each successful job. Rename migrates saved
collapse, lock, colour, and deploy-override state; removal clears them. Edits
reconcile Filegraph state but do not deploy to the game folder.

Set one or more presentation or deployment fields using both revisions from
`mods.list`:

```json
{"jsonrpc":"2.0","id":23,"method":"separators.settings.set.start","params":{"game":"skyrim_se","profile":"default","separator":"Patches_separator","revision":"<modlist revision>","state_revision":"<separator_state.revision>","changes":{"locked":true,"color":"#336699","deploy_path":{"path":"/mnt/mods/patches","raw":false,"mode":"symlink","merge":true}}}}
```

`changes` may contain `collapsed` and `locked` booleans, `color` as `#rrggbb`
or null, and `deploy_path` as the four-field object above or null. A non-empty
deploy path must be absolute. An empty path is allowed for raw, mode, or merge
settings that apply at the game's default destination. Only deployment changes
trigger Filegraph reconciliation; a later `deploy.start` applies the new routing
to the game. A stale state fails with `stale_separator_state` and returns its
current revision.

### Rename a mod

Plan a rename before changing a staged folder:

```json
{"jsonrpc":"2.0","id":24,"method":"mods.rename.plan","params":{"game":"skyrim_se","profile":"default","mod":"Old name","new_name":"New name"}}
```

The plan returns `can_rename`, blockers, affected profiles and Profile Groups,
and a `revision`. A shared-staging rename updates every profile listing the
old folder, preserving each profile's enabled state and priority. A
profile-specific member rename rematerializes its Profile Groups. A group
member link must be renamed through the owning member profile; group-local
real folders can be renamed in the group itself. Staged collection-owned mods,
Wabbajack-managed profiles, collisions, locked mods, and symlinked source
folders are blocked. Restore an active deployment before renaming so deployed
links cannot point to a missing staging path.

After reviewing a passing plan, submit the same selection and revision:

```json
{"jsonrpc":"2.0","id":25,"method":"mods.rename.start","params":{"game":"skyrim_se","profile":"default","mod":"Old name","new_name":"New name","plan_revision":"<plan revision>","confirm":true}}
```

The job rechecks the plan, renames the folder, updates each mod-list entry and
its name-keyed state, refreshes Filegraph, and rematerializes affected groups.
A changed input fails with `stale_mod_rename_plan`. If a derived catalog or
group refresh fails after the folder and profile files are saved, the job
reports `mod_rename_sync_failed` with `renamed: true` and the affected names;
refresh or deploy the profile to retry reconciliation.

### Copy mods to another profile

List the source profile, then plan a copy using its exact mod names:

```json
{"jsonrpc":"2.0","id":26,"method":"mods.copy.plan","params":{"game":"skyrim_se","profile":"default","target_profile":"Handheld","mods":["Patch A","Patch B"]}}
```

Selecting a mod-group leader includes its members in source priority order;
selecting only a member copies that member. The plan shows target names,
shared-folder cases, and collisions. An optional `separator` copies its block
and saved separator state; `mods` may be empty for an empty separator. Optional
`renames` maps source mod names to new target folder names. Target Profile
Groups are excluded; copy into a member profile so group reconciliation can
bring it in. When copying from a Profile Group, saved installer choices come
from the group if present, otherwise from the owning member.

`if_exists` defaults to `fail`. Set it to `skip` to omit colliding mods, or
`replace` to replace reviewed target folders. Replacement is refused when a
locked target entry or another shared-staging profile uses the folder. Copying
a folder already shared by both profiles only adds its target mod-list entry.
Submit the same choices, the plan's `revision`, and `confirm: true` to
`mods.copy.start`. A changed mod list, profile state, or selected folder fails
with `mod_copy_plan_changed` before committing the copy. The source profile
remains intact. Complete mod groups keep their membership and collapsed state;
the result reports any groups that could not be preserved. A target Filegraph
failure after copying returns `mod_copy_sync_failed` and identifies the copied
mods. Copying stages files; apply them to the game with `deploy.start`.

### Move mods to another profile

Use `mods.move_profile.plan` with the same selection and collision options as
`mods.copy.plan`, then send the returned `revision` as `plan_revision` with
`confirm: true` to `mods.move_profile.start`. The plan rejects locked source
mods and moves while a different profile is deployed. Moving from a Profile
Group to a profile outside that group removes every contributing member copy;
the plan lists those copies. Moving into one of the group's own members retains
the target member's copy, removes other contributing member copies, and saves an
exclusion so group refresh does not adopt it again. The plan lists that
`group_exclusion` for each mod, and the result lists `group_exclusions` added.
An exclusion can be removed with `profiles.groups.exclusions.set.start`. A
`separator` is copied to the target but remains in the source. With
`if_exists: "skip"`, colliding mods stay in the source.

The move first completes and indexes the target copy. It then removes each
selected source entry and its installer choices. A physical source folder is
kept when the target or another profile uses the same shared staging folder;
otherwise it is removed. Complete mod groups must be preserved at the target
before source cleanup starts. A source-cleanup failure returns
`mod_move_source_cleanup_failed` with `copied`, `moved`, and
`source_remaining`: the target copy stays available, and the result identifies
which source entries still need attention. A later Filegraph or Profile Group
refresh failure returns `mod_move_sync_failed` with its `stage`. Deploy the
target to apply the move to the game.

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

### Manage mod properties

Read one real mod's editable metadata and current revision:

```json
{"jsonrpc":"2.0","id":"properties","method":"mods.properties.get","params":{"game":"skyrim_se","profile":"default","mod":"Example"}}
```

The result's `properties` includes `version_label`, effective displayed `version`,
`version_source`, `source_versions`, `root_folder`, `updates`, and
`metadata_available`. Each supported update source reports its raw
`ignore_update`, `ignored_version`, effective `ignored`, `latest_version`, and
visible `has_update`. `owner_profile`, `owner_mod`, `path`, and `scope` identify
the metadata being edited. `affected_profiles` lists each profile and its mod
names that share that metadata. A Profile Group farm entry resolves to its
private member's source folder; a group-local mod stays local to the group.
Mods in shared staging affect every profile that lists that folder. Private
copies with the same name stay independent. Notes remain profile-local.

Set one or more properties in a single job:

```json
{"jsonrpc":"2.0","id":"save-properties","method":"mods.properties.set.start","params":{"game":"skyrim_se","profile":"default","mod":"Example","revision":"<property revision>","changes":{"version_label":"1.2-local","root_folder":true,"ignore_updates":{"nexus":true}}}}
```

Supported `changes` fields are:

- `version_label`: a single-line string of at most 512 characters. Whitespace
  is trimmed; null or empty text removes the common `[General] version` label
  and restores the available source-version fallback. The common label is also
  Nexus's installed version used by its update checker. Thunderstore and mod.io
  installed versions, package identity, archive names, and file IDs are preserved.
  This edits metadata; it does not download or install another version.
- `root_folder`: a boolean directing this mod's future deployment to the game
  root. Every affected profile's Filegraph is refreshed, including profiles in
  which the mod is disabled. Existing deployed game files are unchanged.
- `ignore_updates`: a non-empty map of `nexus` and/or `thunderstore` to booleans.
  The requested source must have a saved identity. Nexus captures the currently
  known latest version when muted; its update checker can flag a newer release
  again. Thunderstore mutes future update flags. Setting false clears both the
  source's mute and specific ignored version, restoring updates from cached
  file IDs/version comparisons where possible. This does not make network
  requests. mod.io update ignores are not supported.

The revision binds the metadata contents and file identity, profile configuration,
mod lists, and resolved ownership. Changed inputs fail with `stale_mod_properties`.
Separators, cosmetic group headers, missing folders, and unsafe metadata paths
cannot be edited. All requested fields are validated before saving. Invalid INI
content returns `invalid_mod_metadata`; an unavailable update source returns
`update_source_unavailable`. Invalid property requests are rejected before a job
starts. Writes use shared locks across metadata integrations and Profile Group
aliases, an atomic replacement, and verification. Failed saves restore the
original bytes and report `mod_properties_save_failed`; failed restoration reports
`mod_properties_rollback_failed`.

If a profile's saved configuration cannot be inspected, it appears in
`routing_scan_errors`. Labels and update ignores can still be edited, but a root
request fails before saving with `mod_properties_routing_unavailable`. If metadata
saves successfully and routing later fails, the job reports
`mod_properties_sync_failed` with `state_persisted: true`, the updated `properties`,
`changed_fields`, `synced_profiles`, and `failures`. Read the current revision and
retry the root-folder request; routing is refreshed even when the flag already
matches. The successful result contains `changed`, `changed_fields`, updated
`properties`, and `synced_profiles`. These operations leave the active profile
unchanged and do not launch the game.

### Manage mod notes

Read notes for all real mods, or pass a non-empty `mods` array to select names:

```json
{"jsonrpc":"2.0","id":"notes","method":"mods.notes.get","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","Example"]}}
```

The result contains `profile`, `scope:"profile"`, `revision`, `counts`, and
`mods` rows with `name` and `note`. Mods without a note return an empty string.
The revision covers the profile identity, mod list, and all saved notes;
filtering the returned rows does not change it. `mods.list` also includes
the current `note` on each real mod row.

Set different notes in one request:

```json
{"jsonrpc":"2.0","id":"save-notes","method":"mods.notes.set.start","params":{"game":"skyrim_se","profile":"default","revision":"<note revision>","changes":{"SkyUI":"Requires SKSE","Example":null}}}
```

By default strings replace the selected note, with surrounding whitespace
trimmed. JSON `null` or an empty trimmed string removes a note. Set
`append:true` to append each non-empty string on a new line after the existing
note. Empty strings do nothing in append mode; `null` still removes the note.
The job returns `ok`, `changed`, `changed_mods`, and updated inventory and
revision. Repeating a replacement or removal is a no-op when it already
matches; each append adds the requested text again.

Changes use exact real mod names. Separators and synthetic group headers
cannot own notes; clients can use `mods.groups.get` to resolve a group into
its real members. Invalid selections return `mod_not_found`,
`ambiguous_mod`, or `mod_not_noteable`. Notes are allowed on locked mods and mods whose staged
folder is absent because they do not modify installed files. Profile Groups
keep their own notes: member-profile notes and source metadata are untouched.
Notes for unselected or currently absent mods remain stored.

A request can contain at most 4096 changes and 1 MiB of note text encoded as
UTF-8. Each note, including the result of an append, is limited to 65536
characters. NULs and invalid Unicode are rejected. Invalid request shapes
return `invalid_params`; an oversized appended result fails with
`invalid_mod_notes` before saving any note.

Writes hold the game operation lease, shared note-edit lock, mod-list lock,
and profile-state lock. Desktop note edits use the same writer and reread
current state before updating selected keys. Unrelated profile settings are
preserved. Stale revisions fail with `stale_mod_notes`. Invalid saved JSON or
note state returns `invalid_mod_notes_state`; unsafe state paths return
`unsafe_profile_path`. Failed or unverifiable saves restore the original
profile-state bytes, with `mod_notes_save_failed` or
`mod_notes_rollback_failed`. Reads and writes leave the active profile and
mod-list order unchanged.

### Manage mod groups

These are profile-local groups of mods in the mod list. Profile Groups, which
combine several profiles, use the separate `profiles.groups.*` methods.

`mods.groups.get` returns `groups`, an object keyed by the real leader's mod
name or a synthetic `@group:<id>` header identifier, and `revision`. Every group
has `members` in profile order and a `collapsed` boolean. Named cosmetic groups
also have `title`; their identifier represents a header, never an installed mod.
`mods.list` includes the same `groups` object and revision as `group_revision`.
Its existing `revision` still covers only `modlist.txt`.

```json
{"jsonrpc":"2.0","id":80,"method":"mods.groups.get","params":{"game":"skyrim_se","profile":"default"}}
```

Read the revision immediately before calling `mods.groups.edit.start`:

```json
{"jsonrpc":"2.0","id":81,"method":"mods.groups.edit.start","params":{"game":"skyrim_se","profile":"default","revision":"<group revision>","action":"create","title":"Combat patches","mods":["Patch A","Patch B"]}}
```

| Action | Fields | Behavior |
|---|---|---|
| `create` | `mods`, exactly one of `leader` or `title`; optional `collapsed` | Group the selection under an ungrouped real mod or create a named header. Collapse defaults to true. |
| `add` | `group`, `mods`; optional `position` | Add mods to the group. A position contains one `before` or `after` anchor within the target group; otherwise append. |
| `ungroup` | `mods` | Detach selected children and move them after the remaining group. Selecting a leader or header dissolves its group. |
| `promote` | `group`, `leader` | Promote an existing member to real leader, retaining the other members. A named group becomes a real-leader group. |
| `rename` | `group`, `title` | Rename a cosmetic header. Rename an installed real leader through `mods.rename.*`. |
| `collapse` | `collapsed`; optional `group` | Change one group's collapse state, or all groups when `group` is omitted. |
| `dissolve` | `group` | Remove the grouping while preserving mod order. |

Names and group identifiers are case-sensitive. Selecting an existing leader
or header for `create` or `add` includes all its members and folds the source
group into the new group. Creating groups or adding members rejects locked
selected mods and separators. Ungrouping cannot move a locked selected child;
collapse, header rename, and dissolution do not move or activate mods.
Titles must contain 1 to 256 characters on one line. No header is written to
`modlist.txt`, created in staging, or included in Filegraph's mod inventory.
An add position cannot precede the group's leader or header. Groups with no
remaining members are dissolved automatically.

An edit checks both current mod order and group state before writing. A stale
revision fails with `stale_mod_groups` and returns the current inventory.
Mod order and grouping are saved under their shared locks, with order rolled
back if the group-state write fails. Existing unrelated profile state is
preserved. The result includes the updated `groups` and `revision`, the resulting
`group` identifier, `changed`, `order_changed`, `moved`, and Filegraph status.
Only edits that change real mod order reconcile Filegraph. If reconciliation
fails after saving, `mod_group_order_sync_failed` reports `state_persisted: true`
and the current revision; read the state again before another edit. Changes do
not deploy the profile.

### Move mods

Move one or more mods as a block before or after an exact entry from
`mods.list`. The anchor may be another mod, a separator, or a header from
`mods.groups.get`. `before` places the
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

Selecting a real group leader or a named header moves the complete group.
All expanded members are checked for locks, and a member of the moved group
cannot be used as its anchor. Moving a child within its own group keeps its
membership; moving it outside detaches it. Before/after anchors inside another
group place the selection before/after the complete target group, preserving
its contiguity. Joining a group explicitly uses the `add` group-edit action.

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
    "pausable": false,
    "paused": false,
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

Events carry logs, progress, pause changes, cancellation requests, and state transitions.
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

### Manage Wine DLL overrides

Read both the selected profile's current prefix registry and saved game choices:

```json
{"jsonrpc":"2.0","id":"dll-overrides","method":"wine.dll_overrides.get","params":{"game":"skyrim_se","profile":"default"}}
```

The result's `wine_overrides` contains `prefix_path`, `registry_present`,
`registry_available`, `registry_error`, `revision`, `load_orders`,
`prefix_overrides`, `stored_overrides`, `handler_overrides`,
`discovered_overrides`, `configured_overrides`, `overrides`, `removed`, and
`reset`. Compatdata parents are normalized to their `pfx` directory. The
`overrides` view uses current prefix values where present. The configured view
merges adjacent-DLL discovery, handler defaults, and stored choices in that
order, excluding persistent removals. This lets a client distinguish current
runtime state from settings that will apply on a later deployment.

Set or remove named overrides:

```json
{"jsonrpc":"2.0","id":"save-dll-overrides","method":"wine.dll_overrides.set.start","params":{"game":"skyrim_se","profile":"default","revision":"<override revision>","changes":{"winhttp":"native,builtin","version":null,"example":""}}}
```

Names are case-insensitive basenames with letters, digits, underscores, dots,
hyphens, and an optional leading `*`. A `.dll` suffix on a new name is stripped,
matching Wine's editor; an existing registry/config key keeps its exact name.
Names are limited to 255 characters and must be unique after normalization.
Modes are `native,builtin`, `builtin,native`, `native`, `builtin`, or the empty
string for disabled. The shared aliases `n,b`, `b,n`, `n`, `b`, and `disabled`
are normalized. JSON `null` removes the stored and prefix override and
persistently suppresses discovery/defaults from reintroducing it on deploy.

Release a pinned or suppressed override and restore inherited defaults:

```json
{"jsonrpc":"2.0","id":"reset-dll-overrides","method":"wine.dll_overrides.set.start","params":{"game":"skyrim_se","profile":"default","revision":"<override revision>","reset":["winhttp"]}}
```

Reset choices persist until explicitly replaced or removed. They use the
current discovery/handler default if one exists, or clear the explicit prefix
entry otherwise. Later deployments honor them without pinning a handler
version's default in the saved override map. `reset` and `changes` must target
distinct names. Each selection is limited to 1024 entries.

`apply` defaults to true. Application synchronizes stored overrides and edits,
restores requested default choices, and removes suppressed entries; unrelated
prefix entries remain. Setting `apply:false` saves only configuration. An
apply-only request with no edits applies saved choices and persistent resets.
Stored settings are per game; immediate application targets the selected
profile's prefix. If no registry exists yet, saved configuration is
retained and the result reports `applied:false` with
`apply_reason:"prefix_unavailable"`. Deferred requests report `"deferred"`.

Before applying, the job checks actual prefix processes. `wine_prefix_in_use`
or `wine_prefix_scan_unavailable` leaves both configuration and registry
unchanged. Shared file locks cover configuration and registry writers, and the
game operation lease prevents concurrent managed mutations. The revision
includes both files, prefix identity, defaults, and discovery. Stale state
returns `wine_overrides_changed`. Save/apply failures restore the original
bytes; errors include `wine_overrides_save_failed`,
`wine_overrides_apply_failed`, and `wine_overrides_rollback_failed`.

An invalid registry is reported as `registry_error:"invalid_wine_registry"`;
configuration can still be saved with `apply:false`, and apply attempts are
rejected. Symlinked state files return `unsafe_wine_overrides`. Invalid saved
JSON, names, or modes return `invalid_wine_overrides`. Invalid parameter shapes
return `invalid_params`.
Results report `changed`, `saved`, `applied`, `apply_reason`, and the updated
`wine_overrides` snapshot. Reads and writes leave the active profile unchanged.

### Configure launch settings

Read settings for the selected profile and the game's current executable key:

```json
{"jsonrpc":"2.0","id":"launch-settings","method":"launch.settings.get","params":{"game":"skyrim_se","profile":"default"}}
```

The result's `launch_settings` contains `executable_key`, `revision`, `settings`,
`available_toggles`, and `scope`. Most controls are saved per game and apply to
all its profiles. `scope.arguments` is `profile` for profile-specific mod
storage and `global` otherwise. Profile arguments fall back to the global
executable arguments until explicitly saved. The resolved executable key can
change when a preferred script extender becomes available, which invalidates
an older revision.

Send only the fields to change:

```json
{"jsonrpc":"2.0","id":"save-launch-settings","method":"launch.settings.set.start","params":{"game":"skyrim_se","profile":"default","revision":"<settings revision>","changes":{"mode":"none","arguments":"--example","mangohud":{"enabled":true,"display":"fps_only","fps_limit":144}}}}
```

Supported fields are:

- `mode`: `auto`, `steam`, `heroic`, `lutris`, `faugus`, or `none`.
- `deploy_before_launch`: boolean. This controls desktop Play behavior;
  `launch.start` continues to require an explicitly deployed profile.
- `arguments`: executable argument string, using the reported arguments scope.
- `launch_options`: Steam-style environment, wrapper, `%command%`, and suffix
  options, parsed by the shared launch router.
- `wayland`: boolean.
- `mangohud`: an object accepting any subset of `enabled` (boolean), `display`
  (`default`, `fps_only`, `full`), `position` (`default`, `top-left`, `top-right`,
  `middle-left`, `middle-right`, `bottom-left`, `bottom-right`, `top-center`,
  `bottom-center`), `fps_limit` (integer 0–1000), and `extra_options` (text).
  Zero/default controls retain the user's MangoHud configuration. Vulkan
  launches set `MANGOHUD=1`; OpenGL can use `mangohud %command%` in launch options.
- `lsfg`: an object accepting any subset of `enabled`, `allow_fp16`,
  `performance_mode`, `override_present_mode`, `preserve_swapchain_image_count`,
  and `legacy_hdr_mode` (booleans), `dll_path` and `log_file` (text), `multiplier`
  (integer 1–20), `flow_scale` (number 0.25–1), `pacing_mode` (`vsync`),
  `log_level` (`error`, `warning`, `info`, `debug`), and `legacy_present_mode`
  (`fifo`, `mailbox`, `immediate`).
- `toggles`: an object mapping keys from `available_toggles` to boolean values.

Unknown controls, incorrect types, non-finite numbers, out-of-range values, and
null characters are rejected with `invalid_launch_settings`. Text is limited
to 32768 characters. Nested control objects are partial updates. Unrelated
executables, future stored fields, and unedited controls are preserved.
Explicitly saving inherited arguments creates a profile override, even when
its value currently matches the global fallback.

The job checks `revision` under shared settings-file locks and the game's
operation lease. Changed state returns `launch_settings_changed`. A failed
multi-file save restores the original bytes; failures report
`launch_settings_save_failed` or `launch_settings_rollback_failed`. Invalid or
symlinked files return `invalid_launch_settings` or `unsafe_launch_settings`.
`changed:false` means no persisted settings changed.

Successful saves refresh an existing launcher handoff where the game's shared
launcher integration requires one. The result includes `handoff_path` and an
optional `warning`. `launcher_handoff_refresh_failed` means the settings were
saved but the generated launcher script could not be refreshed. Reads and
writes leave the manager's active profile unchanged. Settings changes are
rejected while a managed launch for the game is active.

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

Headless profile, archive-install/reinstall, mod, update-check,
plugin-management, and launch-dispatch jobs, standard Amethyst Deploy and
Restore, Wabbajack installation/removal/reset, collection group finalization
and reset, and profile conversion use the same operation coordinator.
Operations are serialized within one process. Across native, AppImage and
Flatpak processes, advisory locks cover the game identity and resolved game,
profile, staging and deployment paths.

If another process already holds a relevant lock, the accepted job finishes as
`failed` with `error.name` set to `operation_busy`. Its error details identify
the holder when metadata was available. Lock files persist between runs; their
existence does not mean an operation is active because the kernel lock is the
authority.

Install jobs can report `archive_not_found`, `unsafe_archive`,
`unsafe_install_destination`, `installer_choices_required`,
`installer_unsupported`, `installer_dependencies_missing`,
`mod_already_exists`, and `install_failed`. Other final
job error names include `deploy_failed`, `restore_failed`,
`resource_unavailable`, and `internal_error`. Human-readable pipeline details
remain available in the job's `log` events.

Only snapshots with `cancellable: true` accept `jobs.cancel`; this includes
Nexus downloads and Wabbajack gallery, package, preflight, and install jobs.
Only `nexus.download` jobs currently report `pausable: true` and accept
`jobs.pause` or `jobs.resume`. Other jobs return `job_not_pausable`.
Other filesystem mutation jobs cannot be cancelled because doing so could
abandon a partially updated, restored, or deployed filesystem. The separate
OAuth browser flow can be cancelled with `nexus.auth.login.cancel`.

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
| `-32003` | `resource_unavailable` or `installation_unavailable` | Amethyst could not read a required local resource or find a managed installation. |
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
| `-32016` | `job_not_pausable` | The job does not support pause/resume. |

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

## Remaining desktop parity

Core profile/mod/plugin management, archive installation, collections,
Wabbajack, deployment, restoration, and managed launch have public headless
methods. A large method count does not establish complete desktop parity.
The following feature families still need public workflows or wider validation:

- Remaining mod controls: Quick Update workflows, empty-mod creation, and source actions such as Nexus endorsement/tracking.
- Source browsing and search beyond the existing Nexus queries, exact Thunderstore package/version catalogue, and known-mod mod.io file catalogue.
- Profile export and sharing, remaining profile settings, and operational
  application settings beyond the existing game and download configuration APIs.
- Editing LOOT user rules, groups, and plugin group assignments. Sorting already
  reads the selected profile's existing userlist.
- Prefix Health and game-specific tool/setup wizards. Individual wizards need
  an audit of their parameters, shared execution code, and interaction requirements;
  the desktop tool catalogue contains many separate workflows.
- Consistent operation locking across desktop/headless mutations and the full
  game-running lifetime, plus cancellation and restart recovery where still absent.
- Packaged installations and broader real-game validation. Source fixtures and
  successful protocol calls do not prove Flatpak/AppImage or live-game behavior.

These families vary substantially in size; the game-specific tools require the
broadest remaining audit. The supported methods and limitations below are the
current contract, rather than a claim that these families are complete.

## Current limitations

- Nexus collections can be installed into a new private profile and
  installed collections can be resumed, updated, appended, listed, reset,
  safely removed, and have their appended mods enabled or disabled. Automatic
  Nexus downloads require Premium; free-account installs use pre-supplied
  archives or interactive browser handoff.
- `mods.check_updates.start` refreshes Nexus Mods, Thunderstore, and BG3 mod.io
  metadata when the respective credentials are available.
- Scripted FOMOD code cannot run in Amethyst, and unparseable FOMOD XML cannot
  supply choices. Headless clients may explicitly choose the same verbatim
  file-copy fallback as the desktop installer. Ordinary Fluffy multi-mod
  archives are selectable bundles; independent folder-per-mod archives can be
  installed together.
- Saved game, prefix, runtime, deploy-mode, staging, manual-save, and OpenMW
  AppImage paths can be changed through a revision-checked plan. Launcher
  identities have a separate revision-checked setter. Staging migration is not
  cancellable once it starts. Other Configure Game wizard options remain
  desktop-only unless exposed by `games.options.get`.
- Cross-profile mod copying and moving include Profile Group sources. Moving
  into a member saves an inspectable group exclusion; the group must be
  restored before the move.
- Progress delivery is polling-only. Archive inspection, Nexus, Thunderstore, and mod.io downloads,
  collection download, installation, resume, update and append, and Wabbajack
  gallery, package, preflight, archive-import, and install jobs can be cancelled;
  most other running jobs cannot. Nexus, Thunderstore, and mod.io downloads and interrupted-download
  resume jobs can be paused and resumed during the same headless process.
  Saved Nexus partials can also be listed, resumed, or discarded after a restart.
- The shared operation lock also covers Wabbajack installation/removal/reset,
  collection group finalization, appended-mod activation and removal,
  collection load-order resets, game reconfiguration, and profile conversion.
  Other desktop mod/plugin-list edits, installers, wizards, configuration
  actions, and a game's full running
  lifetime remain outside it. Do not run those tools while a headless mutation
  is active.
- The transport is a child process over standard I/O; there is no socket daemon
  or remote authentication layer yet.
- Python modules under `amethyst_backend`, `Utils`, `Games` and `Nexus` remain
  implementation details. External applications should use the JSON protocol.

The service has the same filesystem permissions as the user who starts it.
Only pass requests from trusted local applications.
