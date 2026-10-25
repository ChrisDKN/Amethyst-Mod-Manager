# Headless mode

For extensions launched from **Plugins**, see
[Manager plugins](manager-plugins.md). That host wraps this API with settings,
write previews, and desktop coordination.

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
job to finish before exiting. Interactive installation and collection jobs, pending installer
session preparation, and single-file Nexus browser handoffs are cooperatively
cancelled on disconnect, including jobs that have not reached their next prompt.
Temporary installer sessions are cleaned up after accepted jobs finish.
Other jobs finish normally so a disconnect does not terminate a mutation halfway
through.

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
      "batching": {"maximum_items": 256, "methods": {"downloads.inspect.batch.start": "downloads.inspect.start", "downloads.install.batch.start": "downloads.install.start", "mods.reinstall.batch.start": "mods.reinstall.start", "mods.versions.get.batch.start": "mods.versions.get.start", "nexus.mods.get.batch.start": "nexus.mods.get.start", "nexus.mods.files.batch.start": "nexus.mods.files.start", "nexus.files.get.batch.start": "nexus.files.get.start", "thunderstore.packages.versions.batch.start": "thunderstore.packages.versions.start", "modio.mods.files.batch.start": "modio.mods.files.start", "downloads.interrupted.resume.batch.start": "downloads.interrupted.resume.start", "downloads.interrupted.discard.batch.start": "downloads.interrupted.discard.start", "saves.transfer.batch.start": "saves.transfer.start", "saves.remove.batch.start": "saves.remove.start", "games.options.set.batch.start": "games.options.set.start"}, "shared_conflict_snapshot": true, "layout_plan_apply": true, "shared_property_inventory": true, "partial_results": "error.details.batch", "cancellation": "between_items_and_supported_active_operations"},
      "events": true,
      "event_delivery": "poll",
      "cancellation": true,
      "game_version": {
        "read_only": true,
        "profile_aware": true,
        "source": "executable_resources"
      },
      "cancellable_jobs": ["profiles.copy", "tools.synthesis.prefix.plan", "tools.synthesis.prefix.setup", "tools.synthesis.acquire", "tools.synthesis.inspect", "tools.synthesis.install", "tools.wrye_bash.acquire", "tools.wrye_bash.inspect", "tools.wrye_bash.install", "tools.wrye_bash.run.plan", "tools.wrye_bash.run", "tools.lod.run.plan", "tools.lod.run", "tools.lod.acquire", "tools.lod.inspect", "tools.lod.install", "tools.archive_packing.scan", "tools.archive_packing.plan", "tools.archive_packing.apply", "tools.xedit.run.plan", "tools.xedit.run", "tools.xedit.acquire", "tools.xedit.inspect", "tools.xedit.install", "tools.smapi.acquire", "tools.smapi.inspect", "tools.smapi.install", "tools.4gb_patch.inspect", "tools.4gb_patch.apply", "tools.4gb_patch.restore", "tools.reshade.dll.acquire", "tools.reshade.shaders.acquire", "tools.reshade.inspect", "tools.reshade.install", "tools.script_extender.acquire", "tools.script_extender.inspect", "tools.script_extender.install", "nexus.download", "thunderstore.download", "modio.download", "mods.quick_update", "mods.nexus.action", "downloads.inspect", "downloads.interrupted.resume", "downloads.interrupted.discard", "wabbajack.gallery", "wabbajack.package.download", "wabbajack.preflight", "wabbajack.archive.import", "wabbajack.install", "collections.install", "collections.import", "collections.resume", "collections.update", "collections.append", "collections.download", "profiles.share", "profiles.export.inspect", "profiles.export", "profiles.export.settings.save", "wine.prefix.health", "wine.prefix.repair", "wine.game_registry.inspect", "wine.game_registry.write", "wine.dependencies.list", "wine.dependencies.install", "downloads.inspect.batch", "downloads.install.batch", "mods.reinstall.batch", "mods.versions.get.batch", "nexus.mods.get.batch", "nexus.mods.files.batch", "nexus.files.get.batch", "thunderstore.packages.versions.batch", "modio.mods.files.batch", "downloads.interrupted.resume.batch", "downloads.interrupted.discard.batch", "saves.transfer.batch", "saves.remove.batch", "games.options.set.batch", "mods.conflicts.overview", "mods.conflicts.inspect.batch"],
      "pausable_jobs": ["tools.synthesis.acquire", "tools.wrye_bash.acquire", "tools.lod.acquire", "tools.xedit.acquire", "tools.smapi.acquire", "tools.reshade.dll.acquire", "tools.reshade.shaders.acquire", "tools.script_extender.acquire", "nexus.download", "thunderstore.download", "modio.download", "mods.quick_update", "downloads.interrupted.resume", "downloads.interrupted.resume.batch"],
      "mutations": ["profiles.create", "profiles.copy", "profiles.groups.create", "profiles.groups.set_members", "profiles.groups.exclusions.set", "profiles.convert", "profiles.rename", "profiles.remove", "games.configure", "games.reconfigure", "games.launcher.set", "games.options.set", "games.routing.set", "games.blacklist.set", "wabbajack.remove", "wabbajack.reset_load_order", "wabbajack.archive.import", "wabbajack.install", "collections.install", "collections.manual.submit", "collections.import", "collections.resume", "collections.update", "collections.append", "collections.group", "collections.download", "collections.remove", "collections.reset_load_order", "collections.set_enabled", "profiles.select", "profiles.export", "profiles.export.settings.save", "separators.create", "separators.rename", "separators.remove", "separators.settings.set", "mods.bundle.set", "mods.create", "mods.nexus.action", "mods.rename", "mods.copy", "mods.move_profile", "mods.set_enabled", "mods.move", "mods.groups.edit", "mods.requirements.ignore.set", "mods.properties.set", "mods.version.change", "mods.notes.set", "mods.remove", "mods.check_updates", "mods.quick_update", "mods.reinstall", "plugins.set_enabled", "plugins.move", "plugins.sort", "plugins.userlist.edit", "boundaries.move", "boundaries.remove", "saves.export", "saves.import", "saves.transfer", "saves.remove", "deploy", "restore", "nexus.auth.login", "nexus.auth.logout", "modio.credentials.set", "modio.credentials.clear", "tools.synthesis.prefix.setup", "tools.synthesis.acquire", "tools.synthesis.install", "tools.wrye_bash.acquire", "tools.wrye_bash.install", "tools.wrye_bash.run", "tools.lod.run", "tools.lod.acquire", "tools.lod.install", "tools.archive_packing.apply", "tools.xedit.acquire", "tools.xedit.install", "tools.xedit.run", "tools.smapi.acquire", "tools.smapi.install", "tools.4gb_patch.apply", "tools.4gb_patch.restore", "tools.reshade.dll.acquire", "tools.reshade.shaders.acquire", "tools.reshade.install", "tools.script_extender.acquire", "tools.script_extender.install", "nexus.download", "thunderstore.download", "modio.download", "downloads.install", "downloads.interrupted.resume", "downloads.interrupted.discard", "downloads.locations.set", "downloads.hidden.set", "downloads.move", "downloads.remove", "launch", "launch.stop", "launch.settings.set", "wine.dll_overrides.set", "wine.prefix.repair", "wine.game_registry.write", "wine.dependencies.install", "downloads.install.batch", "mods.reinstall.batch", "downloads.interrupted.resume.batch", "downloads.interrupted.discard.batch", "saves.transfer.batch", "saves.remove.batch", "games.options.set.batch", "mods.layout.apply", "mods.properties.set.batch"],
      "plugin_userlist": {
        "actions": ["set_rules", "set_group", "remove_plugins", "save_group", "remove_group", "flip_rule", "clear", "reset"],
        "scope": "profile",
        "revision_checked": true,
        "game_fallback": true,
        "preserves_metadata": true,
        "cycle_diagnostics": "userlist_only",
        "maximum_file_bytes": 4194304,
        "maximum_names": 4096
      },
      "wine_overrides": {
        "prefix_inventory": true,
        "revision_checked": true,
        "persistent_removals": true,
        "reset": true,
        "apply": true,
        "running_prefix_guard": true
      },
      "prefix_health": {
        "report": true,
        "read_only": true,
        "repair": true,
        "fix_all": true,
        "revision_checked": true,
        "live_verification": true,
        "shared_desktop_dispatch": true,
        "running_prefix_guard": true,
        "strict_prefix": true,
        "repair_scope": "reported_checks",
        "cancellation": "between_checks_and_repairs"
      },
      "wine_dependencies": {
        "report": true,
        "read_only": true,
        "install": true,
        "revision_checked": true,
        "optional_components": true,
        "shared_desktop_dispatch": true,
        "running_prefix_guard": true,
        "strict_prefix": true,
        "verification": ["live", "installer_marker"],
        "cancellation": "between_checks_and_installers"
      },
      "game_registry": {
        "inspect": true,
        "write": true,
        "force": true,
        "revision_checked": true,
        "verified_views": true,
        "shared_desktop_execution": true,
        "running_prefix_guard": true,
        "cancellation": "process_group_and_prefix_cleanup"
      },
      "tool_catalogue": {
        "built_in": true,
        "profile_aware": true,
        "read_only": true,
        "synthesis": {
          "acquisition": true,
          "inspection": true,
          "installation": true,
          "prefix_setup": true,
          "pipeline_execution": false,
          "revision_checked": true,
          "shared_desktop_installation": true,
          "shared_desktop_setup": true
        },
        "wrye_bash": {
          "acquisition": true,
          "inspection": true,
          "installation": true,
          "launch": true,
          "patch_capture": true,
          "restore_after_run": true,
          "revision_checked": true,
          "shared_desktop_execution": true
        },
        "lod": {
          "acquisition": true,
          "inspection": true,
          "installation": true,
          "revision_checked": true,
          "shared_desktop_installation": true,
          "generation": true,
          "output_import": true,
          "restore_after_run": true
        },
        "archive_packing": {
          "scan": true,
          "plan": true,
          "pack": true,
          "unpack": true,
          "tracked_unpack": true,
          "selected_archives": true,
          "revision_checked": true,
          "shared_desktop_execution": true
        },
        "xedit": {
          "acquisition": true,
          "inspection": true,
          "installation": true,
          "launch": true,
          "quick_auto_clean": true,
          "revision_checked": true
        },
        "smapi": {
          "acquisition": true,
          "inspection": true,
          "installation": true,
          "install_modes": ["game", "root", "mod"],
          "revision_checked": true
        },
        "four_gb_patch": {
          "inspect": true,
          "apply": true,
          "restore": true,
          "revision_checked": true,
          "shared_desktop_transaction": true
        },
        "script_extender_sources": true,
        "script_extender_acquisition": true,
        "script_extender_inspection": true,
        "script_extender_installation": true,
        "reshade_options": true,
        "reshade_dll_acquisition": true,
        "reshade_shader_acquisition": true,
        "reshade_inspection": true,
        "reshade_installation": true,
        "reshade_workflow": true,
        "reshade_shader_replacement": true,
        "external_plugins": false
      },
      "profile_share": {
        "format": "AMMCODE1",
        "sources": ["nexus", "thunderstore"],
        "inspect": true,
        "import": true,
        "profile_groups": true,
        "maximum_code_length": 8388608,
        "maximum_manifest_bytes": 16777216,
        "upload": false,
        "local_files": false
      },
      "profile_export": {
        "format": "amethyst",
        "sources": ["nexus", "thunderstore", "direct", "browse", "manual", "bundle", "ignore"],
        "preview": true,
        "saved_settings": true,
        "profile_groups": true,
        "file_edits": true,
        "revision_checked": true,
        "atomic": true,
        "overwrite": "explicit",
        "maximum_changes": 4096,
        "maximum_changes_bytes": 8388608,
        "cancellation": "file_chunks_and_between_patch_files"
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
      "quick_update": {
        "sources": ["nexus"],
        "plan": true,
        "revision_checked": true,
        "group_selection": true,
        "cached_archives": true,
        "parallel_downloads": true,
        "download_only": true,
        "installer_choices": true,
        "partial_results": true,
        "rollback": "per_mod",
        "cancellation": "acquisition_and_between_replacements"
      },
      "mod_creation": {
        "empty": true,
        "version": "1.0",
        "revision_checked": true,
        "positions": ["top", "bottom", "before", "after"],
        "group_anchor_policy": "outside_group",
        "enabled": true,
        "root_folder": true,
        "profile_groups": true,
        "rollback": true
      },
      "nexus_actions": {
        "actions": ["endorse", "abstain", "track", "untrack"],
        "live_state": true,
        "revision_checked": true,
        "group_selection": true,
        "identity_deduplication": true,
        "remote_verification": true,
        "metadata_scope": "selected_owners",
        "partial_results": true,
        "cancellation": "between_targets"
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
        "interactive": true
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
        "games.version.get.start",
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
        "tools.list",
        "tools.synthesis.acquire.start",
        "tools.synthesis.inspect.start",
        "tools.synthesis.install.start",
        "tools.synthesis.prefix.plan.start",
        "tools.synthesis.prefix.setup.start",
        "tools.wrye_bash.acquire.start",
        "tools.wrye_bash.inspect.start",
        "tools.wrye_bash.install.start",
        "tools.wrye_bash.run.plan.start",
        "tools.wrye_bash.run.start",
        "tools.lod.acquire.start",
        "tools.lod.inspect.start",
        "tools.lod.install.start",
        "tools.lod.run.plan.start",
        "tools.lod.run.start",
        "tools.archive_packing.scan.start",
        "tools.archive_packing.plan.start",
        "tools.archive_packing.apply.start",
        "tools.xedit.acquire.start",
        "tools.xedit.inspect.start",
        "tools.xedit.install.start",
        "tools.xedit.run.plan.start",
        "tools.xedit.run.start",
        "tools.smapi.acquire.start",
        "tools.smapi.inspect.start",
        "tools.smapi.install.start",
        "tools.4gb_patch.inspect.start",
        "tools.4gb_patch.apply.start",
        "tools.4gb_patch.restore.start",
        "tools.reshade.options.get",
        "tools.reshade.dll.acquire.start",
        "tools.reshade.shaders.acquire.start",
        "tools.reshade.inspect.start",
        "tools.reshade.install.start",
        "tools.script_extender.acquire.start",
        "tools.script_extender.inspect.start",
        "tools.script_extender.install.start",
        "profiles.list",
        "profiles.create.start",
        "profiles.copy.start",
        "profiles.groups.create.start",
        "profiles.groups.set_members.start",
        "profiles.groups.exclusions.get",
        "profiles.groups.exclusions.set.start",
        "profiles.convert.start",
        "profiles.rename.start",
        "profiles.remove.start",
        "profiles.select.start",
        "profiles.share.get.start",
        "profiles.share.inspect",
        "profiles.export.get.start",
        "profiles.export.start",
        "profiles.export.settings.save.start",
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
        "mods.create.start",
        "mods.nexus.get.start",
        "mods.nexus.action.start",
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
        "mods.quick_update.plan.start",
        "mods.quick_update.start",
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
        "plugins.userlist.get",
        "plugins.userlist.edit.start",
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
        "wine.prefix.health.start",
        "wine.prefix.repair.start",
        "wine.game_registry.inspect.start",
        "wine.game_registry.write.start",
        "wine.dependencies.list.start",
        "wine.dependencies.install.start",
        "launch.start",
        "launch.stop",
        "jobs.get",
        "jobs.list",
        "jobs.events",
        "jobs.cancel",
        "jobs.pause",
        "jobs.resume",
        "mods.conflicts.overview.start",
        "mods.conflicts.inspect.batch.start",
        "mods.layout.plan.start",
        "mods.layout.apply.start",
        "mods.properties.get.batch.start",
        "mods.properties.set.batch.start",
        "downloads.inspect.batch.start",
        "downloads.install.batch.start",
        "mods.reinstall.batch.start",
        "mods.versions.get.batch.start",
        "nexus.mods.get.batch.start",
        "nexus.mods.files.batch.start",
        "nexus.files.get.batch.start",
        "thunderstore.packages.versions.batch.start",
        "modio.mods.files.batch.start",
        "downloads.interrupted.resume.batch.start",
        "downloads.interrupted.discard.batch.start",
        "saves.transfer.batch.start",
        "saves.remove.batch.start",
        "games.options.set.batch.start"
      ]
    }
  }
}
```

The server rejects an unsupported `protocol_version`. Clients must ignore
unknown fields in successful results so compatible capabilities and methods can
be added without breaking them.

## Application appearance

These methods are global and do not acquire a game's Filegraph lock. They can
be used while the desktop manager is open.

| Method | Parameters | Result |
| --- | --- | --- |
| `appearance.themes.list` | `{}` | `themes`: objects with `id`, `name`, and `custom` |
| `appearance.get` | `{}` | Saved `theme_id` |
| `appearance.set` | `theme_id`, `confirm: true` | Saved `theme_id` |

`appearance.set` accepts only IDs returned by `appearance.themes.list`, including
`custom:` and enabled `plugin:<plugin_id>:<theme_id>` IDs. Plugin themes use
`custom: false`. It is a synchronous mutation, not a job. It persists the shared
application theme. A running Qt manager watches the config and applies the change
live; without a running GUI it takes effect on the next launch. `appearance.get`
returns the saved selection, including during a temporary theme-editor preview.
Standard manager scripts must use `client.apply()` for `appearance.set`.

```json
{"jsonrpc":"2.0","id":1,"method":"appearance.set","params":{"theme_id":"pipboy","confirm":true}}
```

## Methods

All method parameters are named and must be supplied as a JSON object. Unknown
parameters are rejected.

`system.schema.get` returns the machine-readable catalogue. Omit parameters for
all methods, or pass `{"method":"downloads.install.start"}` for one method.
Each entry provides `name`, `description`, `mutating`, and standalone `params`,
`result`, and `error` schemas using
[JSON Schema 2020-12](https://json-schema.org/draft/2020-12/json-schema-core).
The handshake advertises this endpoint and schema version separately from the
transport protocol version.

Schemas cover parameter names, required fields, common types, job envelopes,
and selected conditional forms, including provider version changes and LOOT
userlist actions. Batch item schemas reuse the corresponding single-operation
parameters. Dynamic nested settings, operation-specific job results, and
game-dependent constraints remain open; `validation_scope` identifies this
boundary. Server-side validation and state revisions remain authoritative.
Result schemas allow additional properties for compatible additions.

| Method | Parameters | Purpose |
| --- | --- | --- |
| `system.handshake` | `protocol_version` integer (optional), `client` object (optional) | Negotiate and inspect the protocol. |
| `system.ping` | None | Check that the service is responsive. |
| `games.list` | `configured_only` boolean (optional) | List discovered game handlers. |
| `games.get` | `game` string | Return one game and its current state. |
| `games.version.get.start` | `game` string, `profile` string | Read the selected installation's game executable version using the Configure menu's detector. Returns `version:null` when no version can be detected. |
| `games.installations.list.start` | `game` string | Scan built-in launchers and enabled plugin launcher providers for candidate installations. Plugin results include `provider_id`, `provider_label` and `provider_icon` (resolved manifest path or null). |
| `games.prefix.detect.start` | `game`, absolute `game_path`, `source`, and `launcher_id` for non-Steam launchers; `provider_id` for plugins | Find the chosen installation's prefix without saving it. Sources include built-in launchers and `plugin`. |
| `games.launcher.get` | `game` string, `profile` string | Read the selected profile's saved launcher identities and revision. |
| `launchers.entries.list.start` | None | List enabled plugin launcher entries without configuring a game. |
| `launchers.entry.get.start` | `provider_id`, `launcher_id` | Read one launcher entry, its settings, writable status and revision. |
| `launchers.entry.set.start` | `provider_id`, `launcher_id`, `changes`, `revision` | Save shared settings through a writable provider while its prefix is idle. |
| `launchers.entry.run.start` | `provider_id`, `launcher_id`; optional `executable`, `arguments` | Start a tracked application, automatically using its managed deployed VFS profile when matched, or a temporary installer/tool in its prefix; use `launch.get` and `launch.stop` for its lifecycle. |
| `games.launcher.set.start` | `game`, `profile`, `source`, and `revision` from `games.launcher.get`; `launcher_id` for external launchers, plus `provider_id` for plugins | Save one launcher identity and clear the alternatives for that profile. Sources include built-in launchers, `manual`, and `plugin`. |
| `games.configure.start` | `game` string, `game_path` absolute path, `prefix_path`, `staging_path`, `save_path_override`, or OpenMW `appimage_path` absolute paths (optional), `deploy_mode` hardlink, symlink, copy, or `vfs` where supported (optional), `runtime_mode` native or proton for BG3 (optional) | Configure a game that has no saved paths and create its default profile. |
| `games.reconfigure.plan` | `game`, `profile`, and at least one of `game_path`, `prefix_path`, `staging_path`, `save_path_override`, `appimage_path`, `deploy_mode`, or `runtime_mode` | Preview a configured game's path changes and staging migration; `deploy_mode` accepts hardlink, symlink, copy, or `vfs` where supported. |
| `games.reconfigure.start` | Same target fields plus `revision` from the plan and `confirm` true; `migrate_staging` boolean when existing files would move | Apply the plan only while its source and destination state still match. |
| `games.options.get` | `game` string, `profile` string | List safe per-profile quick-configuration options and their current values. |
| `games.options.set.start` | `game` string, `profile` string, `key` string, `value`, `expected_value` | Change one available option if its current value still matches. |
| `games.routing.get` | `game` string | Read ordered per-game custom routing rules and their revision. |
| `games.routing.set.start` | `game`, `revision`, full `rules` array, `confirm` true; `profile` (optional) | Save validated rule edits and optionally reconcile one profile. |
| `games.blacklist.get` | `game` string | Read built-in, custom, disabled, and effective per-game conflict exclusions with a revision. |
| `games.blacklist.set.start` | `game`, `revision`, full `overrides` object, `confirm` true; `profile` (optional) | Save filename and folder exclusions and optionally reconcile one profile. |
| `tools.list` | `game`; `profile` (optional) | Discover built-in game tools, categories, related headless methods, and declared script-extender version/download choices without importing wizard views or external plugins. |
| `tools.synthesis.acquire.start` | `game`, `profile`, `tool_id`, catalogue `revision` | Download the declared Synthesis ZIP release to the managed cache. |
| `tools.synthesis.inspect.start` | Acquisition parameters plus absolute `archive_path` | Inspect application files, source identity, destination writes and installation revision. |
| `tools.synthesis.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source:true` for a manual archive | Recheck and install Synthesis while preserving settings, patcher data and its prefix. |
| `tools.synthesis.prefix.plan.start` | Acquisition parameters; optional installed `proton_name`, boolean `save_settings` | Inspect the dedicated prefix, installed runtime files, runner and game registration without launching Wine. |
| `tools.synthesis.prefix.setup.start` | Identical prefix planning parameters plus `plan_revision`, `confirm:true` | Prepare the dedicated 64-bit prefix, verify runtime execution and game registration, then finish prefix cleanup. |
| `tools.wrye_bash.acquire.start` | `game`, `profile`, `tool_id`, catalogue `revision` | Download the declared Wrye Bash standalone GitHub release into the managed cache. |
| `tools.wrye_bash.inspect.start` | Acquisition parameters plus absolute `archive_path` | Inspect the executable, archive identity, destination writes and installation revision without installing. |
| `tools.wrye_bash.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source:true` for a manual archive | Recheck and install Wrye Bash through the shared recoverable application installer. |
| `tools.wrye_bash.run.plan.start` | Acquisition parameters; optional launch settings, `deploy`, `root_folder_enabled`, `save_settings` | Inspect the installed application, source files, prefix and game link without changing files. |
| `tools.wrye_bash.run.start` | Identical planning parameters plus `plan_revision`, `confirm:true` | Run Wrye Bash, preserve changed patch files and finish Restore under the shared operation locks. |
| `tools.lod.acquire.start` | `game`, `profile`, `tool_id`, catalogue `revision` | Acquire the declared xLODGen GitHub release into the managed cache, or return the manual DynDOLOD standalone download link. |
| `tools.lod.inspect.start` | Acquisition parameters plus absolute `archive_path` | Inspect the selected 64-bit LOD application, archive identity, destination writes and plan revision without installing it. |
| `tools.lod.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source:true` for a manual archive | Recheck and install xLODGen, TexGen or DynDOLOD through the shared recoverable application installer. |
| `tools.lod.run.plan.start` | Installation choice parameters; optional run settings, `output_path`, `mod_name`, `replace_output` | Plan generation, prefix, source identities, output replacement and dedicated work folder without changing files. |
| `tools.lod.run.start` | The same run parameters plus the returned `output_path`, `plan_revision`, `confirm:true` | Run the tool, recover deployment and import its generated files as an enabled data mod. |
| `tools.archive_packing.scan.start` | `game`, `profile` | Refresh the archive inventory and assess enabled or tracked mods for BSA/BA2 packing and unpacking. |
| `tools.archive_packing.plan.start` | `game`, `profile`, `action` (`pack` or `unpack`), `mods`; packing `options` or unpack `archives` (optional) | Inspect archive names, mod ownership, affected profiles and deployment restrictions; return an apply revision. |
| `tools.archive_packing.apply.start` | Identical planning parameters plus `revision`, `confirm:true` | Recheck the plan, run the shared desktop batch, and refresh affected profiles and plugin lists under the operation lock. |
| `tools.xedit.acquire.start` | `game`, `profile`, `tool_id`, catalogue `revision`; matching `nxm_url` (optional) | Download the selected pinned Nexus xEdit archive, or return manual source information for a build without an automatic source. |
| `tools.xedit.inspect.start` | `game`, `profile`, `tool_id`, catalogue `revision`, absolute `archive_path` | Prepare the selected xEdit executable and inspect writes into its Applications folder with a plan revision. |
| `tools.xedit.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source:true` for a manual archive | Recheck and install the xEdit application with recoverable file writes, preserving unrelated application files and retaining the archive. |
| `tools.xedit.run.plan.start` | `game`, `profile`, `tool_id`, catalogue `revision`; run mode, selected dirty plugins and launch settings (optional) | Inspect the installed application, profile and prefix without initializing or deploying anything; return a revision for interactive launch or batch cleaning. |
| `tools.xedit.run.start` | Identical planning parameters plus `plan_revision`, `confirm:true` | Deploy if requested, prepare the prefix, run xEdit or selected QuickAutoClean processes, preserve edited plugins and complete the standard Restore pipeline. |
| `tools.smapi.acquire.start` | `game` | Download the latest official SMAPI installer ZIP into the managed cache with a checked archive identity. This job can pause or cancel. |
| `tools.smapi.inspect.start` | `game`, `profile`, absolute ZIP `archive_path`, `mode` (`game`, `root`, `mod`); `mod_name` in mod mode (optional, default `SMAPI`) | Prepare the native SMAPI payload and inspect destination writes, launcher backup, dependency manifest source, and a plan revision. |
| `tools.smapi.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source:true` for a manual archive, `replace_existing:true` for an existing managed-mod folder | Recheck the plan and install SMAPI with recoverable payload writes and native launcher setup. |
| `tools.4gb_patch.inspect.start` | `game`, `profile` | Inspect a supported executable and backup, available patch actions, automatic-patching behavior, and a checked revision. |
| `tools.4gb_patch.apply.start` | `game`, `profile`, inspection `revision`, `confirm:true` | Apply the built-in New Vegas or Oblivion 4GB patch, preserving the original backup. |
| `tools.4gb_patch.restore.start` | `game`, `profile`, inspection `revision`, `confirm:true` | Restore and consume the executable backup, using the same recoverable transaction as Apply. |
| `tools.reshade.options.get` | `game`, `profile` | Read the ReShade wizard's game defaults, rendering API and architecture choices, optional shader packs, profile-specific install locations, and d3dcompiler_47 status. No download or install occurs. |
| `tools.reshade.dll.acquire.start` | `game`, `profile`, `architecture` (`32` or `64`) | Download the current official full add-on ReShade installer over HTTPS, extract exactly the requested DLL into the managed cache, and return its path, version, size, and SHA-256. The job can be paused or cancelled. |
| `tools.reshade.shaders.acquire.start` | `game`, `profile`; `pack_indices` from `tools.reshade.options.get` (optional), absolute `preset_path` to an `.ini` or `.txt` file (optional) | Acquire the official base shader pack plus selected optional packs. A preset with enabled effects selects all packs and trims the result to those effects. Return a cached ZIP bundle, hash, selected packs, and missing effects. The job can be paused or cancelled. |
| `tools.reshade.inspect.start` | `game`, `profile`, absolute acquired `dll_path` and `shader_path`, `dll_name` from ReShade options, `mode` (`game`, `root`, or `mod`); `mod_name` for managed mods (optional, defaults to `ReShade`) | Verify both acquired file identities, unpack the shader bundle into temporary storage, and preview the exact files, destination collisions, and plan revision. |
| `tools.reshade.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `replace_existing:true` when replacing a managed mod | Recheck the plan, restore before direct game installation, and transactionally install the DLL, shader tree, ReShade.ini, and preset. Source files are retained. The returned `wine_override` must be applied separately. |
| `tools.script_extender.acquire.start` | `game`, `tool_id`, `revision` from `tools.list`; matching `profile` (optional), `version_index` for multi-version tools, `nxm_url` for the exact pinned Nexus file (optional) | Acquire a declared script-extender archive into the managed cache, or return a manual download handoff. |
| `tools.script_extender.inspect.start` | `game`, `profile`, `tool_id`, catalogue `revision`, absolute `archive_path`, `mode` (`game`, `root`, or `mod`); `version_index` for multi-version tools | Extract into temporary storage and preview the exact tool installation destination, collisions, and a plan revision. |
| `tools.script_extender.install.start` | Inspection parameters plus `plan_revision`, `confirm:true`; `allow_unverified_source` and `replace_existing` when required | Recheck the plan and transactionally install the selected script extender. |
| `profiles.list` | `game` string | List existing profiles and summary counts. |
| `profiles.create.start` | `game` string, `name` string, `profile_specific_mods` boolean (optional), `copy_from` string (optional) | Create or copy a profile. |
| `profiles.copy.start` | `game`, source `profile`, destination `name` | Copy a private profile with disk-space checks, progress, cancellation, and path rebasing. |
| `profiles.groups.create.start` | `game` string, `name` string, `members` array of profile names, `ini_source` string (optional), `overwrite_excluded` array of profile names (optional) | Create and materialize a profile group. |
| `profiles.groups.set_members.start` | `game` string, `profile` string, `members` array of profile names, `expected_members` array of profile names | Replace a group's ordered membership and rematerialize it. |
| `profiles.groups.exclusions.get` | `game`, group `profile` | List member mods excluded from a Profile Group and return a revision. |
| `profiles.groups.exclusions.set.start` | `game`, group `profile`, full `exclusions` array of `{member, mod}`, `revision`, `confirm` true | Save exclusions and rematerialize the group. |
| `profiles.convert.start` | `game` string, `profile` string, `confirm` boolean | Convert a shared-pool profile to profile-specific mod storage. |
| `profiles.rename.start` | `game` string, `profile` string, `new_name` string | Rename a profile. |
| `profiles.remove.start` | `game` string, `profile` string, `confirm` boolean | Permanently remove a profile. |
| `profiles.select.start` | `game` string, `profile` string | Persist and load the active profile. |
| `profiles.share.get.start` | `game` string, `profile` string | Create a downloadable-mod share code from the selected profile. |
| `profiles.share.inspect` | `code` string | Decode and validate a share code without downloading or installing mods. |
| `profiles.export.get.start` | `game`, `profile`; `settings`, `changes`, `include_saves`, `include_overwrite`, `include_root_folder` (optional) | Preview a profile archive, including patches, warnings, blockers, and a revision. |
| `profiles.export.start` | Preview parameters, `revision`, absolute `path`; `overwrite`, `allow_patch_warnings` (optional) | Atomically write the previewed `.amethyst` or ZIP archive. |
| `profiles.export.settings.save.start` | Preview parameters and `revision`; JSON `name`, `overwrite` (optional) | Save per-mod export settings under the selected profile's workshop folder. |
| `collections.installed.list` | `game` string, `profile` string (optional) | List installed standalone and appended collections. |
| `collections.download.start` | `game` and `slug`; `domain` and `revision` (optional) | Download every Nexus archive and pinned Thunderstore package for a collection without changing a profile. |
| `collections.install.start` | `game`, `profile`, and `slug`; `domain`, `revision`, `skipped_optional_file_ids`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Install a Nexus collection into a new private profile. |
| `collections.manual.get` | `job_id` | Read the current interactive archive request for a collection job. |
| `collections.manual.submit` | `job_id`, `request_id`, and either absolute `path` or `skip` true | Supply an archive or skip an optional request. |
| `collections.import.start` | `game`, `profile`, and exactly one of absolute `path` or `code`; `skipped_optional_file_ids`, `accept_game_version_mismatch`, `manual_archives`, and `manual_interactive` (optional) | Import a local `.amethyst`, ZIP, JSON, `.r2x`/renamed `.txt` manifest, or share code into a new private profile. |
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
| `manager_plugins.providers.list` | `game`, optional source URI `source` | List enabled matching download-provider declarations without importing plugin code. |
| `manager_plugins.download.resolve.start` | `game`, `plugin_id`, `provider_id`, `source` | Resolve an artifact to a public descriptor/revision in a cancellable read job. |
| `manager_plugins.download.start` | Same source selection, resolved `revision`, `confirm:true` | Download through a registered HTTP or custom transfer provider with normal progress, pause, cancellation, and archive verification. |
| `manager_plugins.download.resolve.batch.start` | `game`, `items`, optional `on_error` | Resolve 1–256 provider requests in one read job. |
| `manager_plugins.download.batch.start` | `game`, `items`, optional `on_error` | Download 1–256 reviewed provider requests in one pausable job; every item supplies `revision` and `confirm:true`. |
| `manager_plugins.dependencies.get.start` | `game`, `items`, optional `revision` | Expand provider source requests in dependency-first order and return a graph revision; an optional revision rejects a changed graph. |
| `manager_plugins.action.start` | `game`, `profile`, `mod`, `source`, `action_id`, `revision`, `catalogue_revision`, `confirm:true` | Perform a reviewed repository action for an installed plugin artifact. |
| `downloads.install.start` | `game` string, `profile` string, `archive_id` string, `name` string (optional), `installer_policy` string (optional), `bain_selected` or `bundle_selected` array, or `fomod_selections` object (optional), `if_exists` string (optional), `installer_revision` string (optional) | Install one archive returned by `downloads.list`; pass a plugin installer's inspected revision to reject a changed file mapping. |
| `installers.sessions.open.start` | `game`, `profile`, `archive_id`; `name` (optional) | Prepare an XML FOMOD wizard without installing files. |
| `installers.sessions.get` | `session_id` | Read visible steps, selections, option states, errors and revision. |
| `installers.sessions.update` | `session_id`, `revision`, integer `step`, integer `group`, exact `option` name | Toggle an option and recompute conditional steps and option states. |
| `installers.sessions.image.get` | `session_id`, integer `step`, integer `group`, exact `option` name | Return a bounded option image as base64 JPEG. |
| `installers.sessions.install.start` | `session_id`, `revision`, `confirm:true`; `if_exists` (optional) | Revalidate and install the reviewed choices. |
| `installers.sessions.close` | `session_id` | Discard a wizard and clean up its temporary extraction. |
| `nexus.download.manual.start` | `game`, `profile`, `mod_id`, `file_id`; `domain` (optional) | Return a browser URL and wait for the exact browser-downloaded file, retaining its Nexus identity. |
| `thunderstore.install.plan.start` | `game`, `profile`, `namespace`, `package`, exact `version` | Resolve dependencies and preview installations, replacements and activation. |
| `thunderstore.install.start` | `game`, `profile`, `plan_id`, `revision`, `confirm:true`; `dependencies`, `if_exists`, `installer_policy`, `bain_selected`, `fomod_selections`, `bundle_selected` (optional) | Apply the dependency plan in order with partial-result reporting. |
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
| `mods.create.start` | `game`, `profile`, `name`, `revision` from `mods.groups.get` or `mods.list.group_revision`; `position`, `enabled`, `root_folder` (optional) | Create an empty mod at version `1.0`, preserving anchored groups and refreshing the native catalogue and affected Profile Groups. |
| `mods.nexus.get.start` | `game`, `profile`, non-empty `mods` array | Read live account endorsement/tracking state, selected owners, cached flags, and an action revision. |
| `mods.nexus.action.start` | `game`, `profile`, matching `mods`, `revision`, `action`, `confirm:true` | Endorse, abstain, track, or untrack selected Nexus identities, verify live state, and report partial results. |
| `mods.properties.get` | `game`, `profile`, `mod` | Read version labels, root-folder routing, source-specific update ignores, ownership, affected profiles, and revision. |
| `mods.properties.set.start` | `game`, `profile`, `mod`, `revision`, non-empty `changes` object | Save supported metadata fields atomically and refresh affected routing when requested. |
| `mods.versions.get.start` | `game`, `profile`, `mod`; `source` (optional, `nexus`, `thunderstore`, `modio`, or `plugin:<plugin-id>/<provider-id>`) | Read installed source identity, the version catalogue, recommendations, ownership, and revision. |
| `mods.version.change.start` | `game`, `profile`, `mod`, `revision`, `confirm:true`; Nexus/mod.io `file_id` or Thunderstore `version`; `source`, archive/download options, installer choices, and Thunderstore `dependency_policy` (optional) | Replace an installed mod with a selected source version and refresh affected profile routing and plugins. |
| `mods.quick_update.plan.start` | `game`, `profile`; `mods` array and `source` (`nexus` or `plugins`, optional) | Plan Nexus filename-family updates or plugin repository recommendations, with owner resolution and a revision. |
| `mods.quick_update.start` | `game`, `profile`, plan `revision`, `confirm:true`; matching `mods`, `download_only`, `installer_policy`, and `per_mod` options (optional) | Acquire archives in parallel and optionally replace mods sequentially, with per-mod rollback and partial-result reporting. |
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
| `plugins.userlist.get` | `game` string, `profile` string | Read effective LOOT rules, groups, assignments, cycle diagnostics and revision. |
| `plugins.userlist.edit.start` | `game` string, `profile` string, `revision` string, `action` string, action-specific parameters | Edit the selected profile's LOOT user metadata. |
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
| `wine.game_registry.inspect.start` | `game`, `profile` | Inspect the selected Bethesda game's install-path registration and available runner without changing the prefix. |
| `wine.game_registry.write.start` | `game`, `profile`, `revision`, `confirm:true`; optional boolean `force` (default `true`) | Write and verify the install path in the required registry views, including an explicit repeat write. |
| `wine.prefix.health.start` | `game`, `profile`; optional boolean `check_proton` | Report the selected profile's prefix health and repairable checks without installing or changing configuration. |
| `wine.prefix.repair.start` | `game`, `profile`, `revision`, and either `checks` array or `all:true`; optional boolean `check_proton` | Repair selected failing health checks and verify the live prefix afterward. |
| `wine.dependencies.list.start` | `game`, `profile` | List shared Wine dependency choices and their state, including components the game does not declare. |
| `wine.dependencies.install.start` | `game`, `profile`, `revision`, non-empty `dependencies` array, `confirm:true` | Install selected optional dependencies into the selected profile's idle prefix and verify each outcome. |
| `launch.start` | `game` string, `profile` string | Launch the currently deployed profile through Amethyst's configured router. |
| `launch.stop` | `launch_id` string, `confirm` boolean | Stop a process tree started by the managed launch. |
| `jobs.get` | `job_id` string | Get the latest job snapshot. |
| `jobs.list` | `active_only` boolean (optional) | List jobs, newest first. |
| `jobs.events` | `job_id` string, `after` integer (optional), `wait_ms` integer (optional) | Read or long-poll ordered job events. |
| `jobs.cancel` | `job_id` string | Request cancellation of a job whose snapshot says `cancellable: true`. |
| `jobs.pause` | `job_id` string | Pause a job whose snapshot says `pausable: true`. |
| `jobs.resume` | `job_id` string | Resume a paused job. |

| `mods.conflicts.overview.start` | `game`, `profile`; `mods`, `offset`, `limit`, `revision` (optional) | Read paged per-mod conflict summaries and relationships from one shared snapshot. |
| `mods.conflicts.inspect.batch.start` | `game`, `profile`; `mods`, `section`, `offset`, `limit`, `revision` (optional) | Read file-detail pages for multiple mods from one shared snapshot. |
| `mods.layout.plan.start` | `game`, `profile`, complete `entries` array; full `groups` object, `preserve_conflicts` (optional) | Preview separator creation, renaming, removal/settings and mod/group ordering together. |
| `mods.layout.apply.start` | Same layout request plus `plan_revision` and `confirm:true` | Validate the plan again, save the layout and reconcile Filegraph once. |
| `mods.properties.get.batch.start` | `game`, `profile`; `mods` (optional) | Read properties and per-mod revisions with one shared ownership inventory. |
| `mods.properties.set.batch.start` | `game`, `profile`, `items` array of `mod`, `revision`, `changes` | Validate all property edits before writing and refresh each affected profile once. |
| `downloads.inspect.batch.start` | `game`, `profile`, `items` array of `downloads.inspect.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `downloads.install.batch.start` | `game`, `profile`, `items` array of `downloads.install.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `mods.reinstall.batch.start` | `game`, `profile`, `items` array of `mods.reinstall.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `mods.versions.get.batch.start` | `game`, `profile`, `items` array of `mods.versions.get.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `nexus.mods.get.batch.start` | `game`, `items` array of `nexus.mods.get.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `nexus.mods.files.batch.start` | `game`, `items` array of `nexus.mods.files.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `nexus.files.get.batch.start` | `game`, `items` array of `nexus.files.get.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `thunderstore.packages.versions.batch.start` | `game`, `items` array of `thunderstore.packages.versions.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `modio.mods.files.batch.start` | `game`, `items` array of `modio.mods.files.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `downloads.interrupted.resume.batch.start` | `game`, `profile`, `items` array of `downloads.interrupted.resume.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `downloads.interrupted.discard.batch.start` | `game`, `profile`, `items` array of `downloads.interrupted.discard.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `saves.transfer.batch.start` | `game`, `profile`, `items` array of `saves.transfer.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `saves.remove.batch.start` | `game`, `profile`, `items` array of `saves.remove.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |
| `games.options.set.batch.start` | `game`, `profile`, `items` array of `games.options.set.start` parameters without game/profile; `on_error` (optional) | Run selected items in one tracked batch with individual results. |

The `game` parameter accepts the canonical `game_id`, display name, primary
Steam app ID, or an alternate Steam app ID. Clients should retain the canonical
`id` returned by Amethyst and use it for later requests. If a Steam app ID
belongs to multiple handlers, Amethyst selects it only when exactly one handler
is configured; otherwise it returns `ambiguous_game` with the candidates.

### Previous-version rollback

`mods.rollback.get.start` accepts `game`, `profile`, `mod`, and optional
`archive_id`. It reports whether a previous Nexus or Thunderstore version was
recorded, its exact identity, cached archive availability, and the current mod
`revision`. Ambiguous archive matches require an explicit archive selection.

`mods.rollback.start` adds that `revision` and `confirm:true`. It restores the
recorded version through the existing installation replacement transaction and
refreshes affected profiles/plugins. The current mod folder name, order, enabled
state, and unrelated metadata are preserved. A successful rollback consumes the
previous-version record, matching desktop history semantics.

Rollback requires a matching cached archive; use the existing provider download
methods first if it is missing. The usual `installer_policy`, `bain_selected`,
`fomod_selections`, and `bundle_selected` options apply. Thunderstore dependencies
are checked unless `dependency_policy:"ignore"` is explicitly selected.
mod.io rollback is unavailable because previous-version records currently cover
Nexus and Thunderstore only. Linked mods retain their existing owner restrictions.

### Thunderstore and mod.io browsing

`thunderstore.packages.search.start` accepts `game`, optional `query`, a
zero-based `page`, and `sort`: `last-updated` (default), `newest`,
`most-downloaded`, or `top-rated`. Optional `included_categories` and
`excluded_categories` are arrays of string category IDs. `section` is a section
UUID. `include_adult` and `include_deprecated` default false. Obtain category
and section choices using `thunderstore.packages.filters.start` with `game`.
Search results contain `packages`, `total`, `page_size` (20), and `next_page`.
Use a returned namespace/name with the existing package version and download APIs.
Provider failures return a failed job instead of an empty successful search.

`modio.mods.search.start` accepts `game:"baldurs_gate_3"`, optional `query`,
`sort` (`downloads`, `updated`, `newest`, or `name`), `offset`, and `limit`
(1–100, default 20). It uses the saved mod.io credentials and the Windows
platform context. Results contain `mods`, `total`, `offset`, `limit`, and
`next_offset`; mod summaries expose exact mod/latest-file IDs and public page
URLs, without signed download links. Search follows mod.io's
[Get Mods](https://docs.mod.io/restapi/docs/get-mods) endpoint and
[filtering/pagination](https://docs.mod.io/restapiref/) conventions.
All three methods return jobs and can browse games before local configuration.

### Retained job events

`jobs.history.events` takes `job_id`, optional `after` (event sequence, default
zero), and `limit` (1–1000, default 500). It returns persisted events, including
logs, progress, and state changes, after the owning service exits. Use
`next_sequence` while `has_more` is true. `complete` describes the job outcome,
not whether the current page is the last page.

The newest 1000 events are retained within a 4 MiB per-job limit. Oversized event
payloads are replaced by sequence/type/state metadata and `data_omitted:true`.
`truncated:true` indicates older events are unavailable. Pre-existing jobs may
return `available:false`. Retention follows the associated job history record.
Events use the same credential and signed-URL redaction as saved job results.

State transitions flush events immediately; other events flush at most once
per second when new events arrive. A process crash can lose the unflushed tail.
`jobs.events` remains the live polling interface. Event storage failures are
reported separately in the handshake and history response, without aborting
the underlying operation. This adds diagnostics, not automatic job resumption.

### Global application settings

`settings.get` reads the preferences from the Settings menu without starting a
GUI or seeding configuration defaults. It returns `revision`, a flat `settings`
object, `definitions` (types, choices, ranges, group and restart requirements),
`shortcut_defaults`, `effective_shortcuts`, and the number of `dismissed_notices`.

```json
{"jsonrpc":"2.0","id":"preferences","method":"settings.get"}
{"jsonrpc":"2.0","id":"save-preferences","method":"settings.set.start","params":{"revision":"<revision from settings.get>","changes":{"max_concurrent":4,"max_extract_workers":2,"download_speed_limit":20,"download_order":"smallest","extraction_low_priority":true,"nexus_free_download_nxm":false}}}
```

Poll `jobs.get` using the returned job ID. Changes are partial: omitted settings
are retained. Arrays and object-valued settings replace that setting in full.
Unknown keys, wrong types, unsupported choices, conflicting shortcuts, invalid
regular expressions/replacements and out-of-range numbers are rejected. The
complete request is staged through the shared Settings save helpers, then
published with one atomic INI write. A failed save does not publish earlier
changes from the request. Symlinked and malformed settings files are rejected.
Files created by the API carry the current schema marker so desktop startup
retains them. Explicitly incompatible schema versions require desktop migration.

The job rechecks the revision while holding the settings-edit lease. A changed
revision returns `settings_changed`; read again before retrying. Unrelated INI
sections and option names are preserved. Results include `changed`,
`changed_keys`, `restart_required`, `desktop_refresh_required`, `cleared_notices`
and the updated settings snapshot. A read-back failure after a successful save
reports `state_persisted:true`.

Available groups and keys:

| Group | Settings |
| --- | --- |
| Appearance | `language`, `ui_scale` (`auto` or 0.5–2), `date_format`, `default_priority_mode`, `header_position`, `header_force_compact`, `hidden_header_buttons`, `hide_bsa_conflicts`, `hide_endorsed_flag`, `show_summary_tooltips`, `hide_kofi_button`, `hide_endorse_button` |
| Archives | `clear_archive_after_install`, `keep_fomod_archives`, `install_mods_disabled`, `rename_mod_after_install` |
| Downloads | `max_concurrent`, `download_speed_limit` (MB/s; 0 means unlimited), `download_order`, `nexus_download_server`, `nexus_free_download_nxm`, `download_only` |
| Extraction | `max_extract_workers`, `extraction_cpu_threads` (0 means all cores), `extraction_low_priority` |
| General | `normalize_folder_case`, `restore_on_close`, `install_name_patterns` |
| Updates | `allow_prerelease`, `update_notifications` |
| Paths | `default_staging_path`, `download_cache_path`, `custom_proton_path`, `heroic_config_path`, `lutris_data_path`, `lutris_appimage_path`, `faugus_data_path`, `faugus_appimage_path`, `bottles_data_path`, `bottles_appimage_path`, `steam_libraries_vdf_path` |
| Advanced | `restore_whitelist` (`files` and `folders` arrays), `app_env_vars` (`name`, `value`, optional boolean `enabled`) |
| Shortcuts | `shortcut_overrides` (action IDs to portable bindings), `disabled_shortcut_inputs` (array of `keyboard` and/or `mouse`) |

Language choices come from bundled and user-installed translation files; an
empty language means system locale. Paths must be absolute (leading `~` is
expanded); an empty string restores automatic/default resolution. Changing
staging or download defaults does not move existing files. Existing staging
access checks also apply to headless writes.

`install_name_patterns` uses ordered objects with `search`, `replace`, optional
`enabled`, and optional built-in `id`/`label`. An empty array disables all rules.
`shortcut_overrides` supplies the complete override map; omitted actions use
their defaults, so `{}` resets bindings. Bindings use the desktop validator,
including reserved mouse buttons and duplicate bindings. For example:

```json
{"jsonrpc":"2.0","id":"shortcuts","method":"settings.set.start","params":{"revision":"<current revision>","changes":{"shortcut_overrides":{"rename":"Ctrl+Shift+Q","browser_back":"Alt+Mouse 4"},"disabled_shortcut_inputs":["mouse"]}}}
```

Language, UI scale and saved application environment variables require a
restart. Other operational preferences are read by their usual consumers on
subsequent operations; changing a preference does not execute a desktop prompt,
restore games, rename installed mods or install an update. Presentation changes
in another running desktop process may require reopening/restarting it; the API
reports `desktop_refresh_required` instead of promising live widget updates.
The bandwidth cap is also updated in the headless process after saving. Settings
mutations follow the service's idle-job guard, so finish active jobs first.

Use the existing `appearance.*` methods for theme selection,
`downloads.locations.*` for scan folders, `nexus.auth.*` for Nexus login, and
`modio.credentials.*` for BG3 mod.io credentials. Reset hidden prompts with
`settings.notices.reset.start`, passing the current settings `revision` and
`confirm:true`; the update notification preference is preserved.

### Nexus link forwarding and LoversLab login

| Method | Parameters | Result |
| --- | --- | --- |
| `nexus.forwarding.get` | None | `revision`, saved domain-to-handler `routes` |
| `nexus.forwarding.handlers.list` | None | `handlers` with `id` and `name` |
| `nexus.forwarding.set.start` | `revision`, lowercase Nexus `domain`, `handler` | Tracked forwarding save job |
| `loverslab.credentials.get` | None | `credentials.configured`; no email or password |
| `loverslab.credentials.set.start` | `email`, `password` | Verify the login and securely save it |
| `loverslab.credentials.clear.start` | `confirm:true` | Clear saved login credentials |

A forwarding handler is `amethyst`, an absolute `.desktop` launcher path, or
`null` to remove the saved choice. External launchers undergo the same direct
NXM-launcher validation as the desktop menu. Revision checks preserve choices
for other domains. Saving a choice does not launch the selected manager.

LoversLab uses the existing secure credential store and login check. Passwords
and email addresses are excluded from job options, results, errors and history.
Site security challenges may still require a browser/manual download. Credential
verification is a network operation; a failed check leaves the saved login in
place. These methods never return the saved password.

### Individual mod file settings

`mods.files.get.start` takes `game`, `profile`, `mod`, and optional `offset`,
`limit` (1–5000, default 500) and `revision`. The cancellable job returns raw
file `path` values, saved `enabled`/`root` flags, current destinations, winning
status, file sizes, available `directories`, `strip_prefixes`, and a `revision`.
Pass the same revision when reading subsequent pages to reject changed state.
Archive members are excluded; these operations apply to installed physical files.

`mods.files.edit.start` requires the same selection, `revision`, and
`confirm:true`. Supply `changes:[{"path":"Data/example.txt","enabled":false}]`
and/or a complete `strip_prefixes` list. A change can set `enabled`, `root`, or
both. File paths and directory prefixes must exactly match the inventory.
Prefix entries identify folders to strip from deployment paths; nested folders
may require their ancestors too. An empty prefix list restores default routing.
Alternatively, use `reset:true` without other edits to clear all three per-mod
file-setting overrides.

Edits preserve unrelated state and save in one atomic profile-state write.
They require an unlocked, restored profile and reject stale revisions. The
profile and dependent groups are reconciled afterward; a failure reports
`mod_files_sync_failed` with `state_persisted:true`. These settings affect the
selected profile; they do not rename, delete, or rewrite source files, including
files shared by linked mods. Apply Deploy afterward to publish the changed view.

### Installed storage reporting

`profiles.storage.get.start` accepts `game`, `profile`, optional exact `mods`,
and `include_overwrite`/`include_root_folder` booleans (both default true).
The cancellable job returns per-mod measurements and profile `totals`:
`files`, `logical_bytes`, `allocated_bytes`, and their `unique_` equivalents.
Unique totals count each device/inode once across all selected mods, including
cross-profile links. Per-mod unique totals must not be added to derive profile
unique totals. Allocated bytes use filesystem block counts and do not establish
exclusive ownership of reflinked or compressed extents.

Each mod also has `winning`, `conflict_losing`, `inactive`, and `unmapped`
categories based on the current Filegraph snapshot. These describe the planned
profile view, not proof that files are presently deployed. Metadata and other
unindexed files remain included in the overall storage total. Physical archives
are measured once; archive members are counted but their expanded bytes are not
added or advertised as reclaimable space. `reclaimable_bytes` is therefore null.

Directory entries themselves, nested symlink targets, and special files are not
measured. Linked mod roots are followed. Missing/unreadable entries and skipped
links are reported, with `complete:false` rather than a misleading complete
total. The snapshot revision and filesystem measurements are a point-in-time
report; external filesystem changes can still race the scan.

### Profile state backups

`profiles.backups.list.start` accepts `game` and `profile`. Its job result contains
the current profile `revision`, `backups`, and any `unavailable` entries. Each
backup has an `id`, its own `revision`, label, retention flags, byte size, file list,
and mod/plugin counts. These snapshots contain profile state, not installed mod
contents or game saves.

`profiles.backups.create.start` takes `game`, `profile`, the current `revision`,
and an optional `label`. New backups are manual and retained until removed.
`profiles.backups.edit.start` takes `backup_id`, `backup_revision`, `game`,
`profile`, and a `label` and/or boolean `kept`. A manual backup remains retained
even if its kept flag is cleared. `profiles.backups.remove.start` takes the same
identity fields plus `confirm:true`.

`profiles.backups.restore.start` requires `game`, `profile`, `backup_id`,
`backup_revision`, the current profile `revision`, and `confirm:true`. The
profile must be unlocked and restored, including any deployed group using it.
Backups with different storage/group layouts are rejected. Restore first creates
a retained recovery backup, restores state, then refreshes the profile and its
dependent groups. Copy failures attempt rollback; incomplete recovery reports
`backup_restore_rollback_failed` and the recovery backup ID. An index refresh
failure reports `backup_restore_sync_failed` with `state_persisted:true`.
Old backups retain their original missing-file semantics; new backups also
record absent files so restoration can remove state created after the snapshot.

All five calls return jobs. Mutations recheck revisions under the shared game
operation lock. Symlinked backup roots, folders, and state files are rejected.

### Reading manager state

List configured games:

```json
{"jsonrpc":"2.0","id":2,"method":"games.list","params":{"configured_only":true}}
```

Each game includes its canonical identity, configuration and deployment state,
paths, store and mod-source identifiers, and available features.
`load_failures` identifies handler modules which could not be discovered.
`features.plugin_sort` is true only when the handler supports LOOT and the
installed native LOOT binding is available. `features.plugin_userlist`
identifies handlers supporting user-rule editing independently of native LOOT
availability. `features.vfs_deploy` and `features.copy_deploy` identify the
additional deployment methods a handler
supports. `features.appimage_path` identifies OpenMW's explicit AppImage path.

Read the installed game version before choosing a version-dependent mod or tool:

```json
{"jsonrpc":"2.0","id":"game-version","method":"games.version.get.start","params":{"game":"skyrim_se","profile":"default"}}
```

Poll the returned job with `jobs.get` or `jobs.events`. Its result contains
`game`, `profile`, `game_path`, and `version` (for example, `"1.6.1170.0"`).
The lookup reads the selected profile's effective game path without saving
configuration or changing the active profile. It uses the same best-effort
executable-resource detector as the Configure menu, preferring the handler's
declared game executables. `version:null` means no version could be read,
including missing executables and installs without readable Windows version
resources. Preserve the returned version; do not infer it from the store or
an installed mod's version. `games.get` and `games.list` do not probe executables.

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
and `launcher_id` to `games.launcher.set.start`. Plugin installations also pass
their namespaced `provider_id` to both prefix detection and launcher selection.
See [launcher providers](manager-plugins.md#launcher-providers) for the entry and
shared-settings contract. Steam and manual installs omit
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

### Define and edit custom games

The `games.custom.*` methods expose the complete JSON custom-game editor without
Qt. Definition fields, presets, share codes, advanced installation settings,
Wine overrides, dependencies, frameworks, default routing rules, restore
whitelists, and banner URLs are supported. These methods do not write Python
handlers. Read `games.custom.schema.get` for the field schema.

| Method | Parameters | Description |
| --- | --- | --- |
| `games.custom.schema.get` | `{}` | Return `definition_schema` for complete custom game definitions. |
| `games.custom.templates.list` | `{}` | Return `templates` matching the editor's built-in presets; fill in a game name and executable before saving. |
| `games.custom.list` | `{}` | Return selected JSON definitions, source identities and revisions under `games`, including unconfigured games. |
| `games.custom.get` | `game` | Read `definition`, `source`, `source_key`, and `revision` for an existing custom game. |
| `games.custom.validate` | `definition` | Validate and return a normalized definition without saving it. |
| `games.custom.code.import` | `code` | Decode an `AMMGAME1:` share code into a validated unsaved definition with a generated ID. |
| `games.custom.code.export` | `definition` | Return a portable `code`; internal metadata and the game ID are excluded. |
| `games.custom.save.plan` | `definition`; `game` (optional) | Preview creating a game, or editing the existing game identified by `game`. Return normalized `definition`, `previous_definition`, field `changes`, and the plan `revision`. |
| `games.custom.save.start` | `definition`, `revision`, `confirm:true`; `game` (optional) | Apply the exact plan, back up the previous definition, select the local handler, cache its banner, and reload the backend handler. |
| `games.custom.remove.plan` | `game` | Preview removing the custom handler and all its source candidates; retain game files, configuration and profiles. |
| `games.custom.remove.start` | `game`, `revision`, `confirm:true` | Apply the removal plan with a backup; remove the cached banner and backend handler. |

Supply the full definition when saving. Preserve fields outside the requested
edit. Only `name` and `exe_name` are required when creating; `game_id` is derived
from the name if omitted. `deploy_type` is `standard`, `root`, or `ue5`.
`mod_data_path` selects the target subdirectory. The schema describes every
editor field, including optional arrays and dictionaries. Additional JSON
metadata is retained. Paths inside definitions are relative; absolute paths and
parent traversal are rejected. Installation paths belong in
`games.configure.start`, which can be called immediately after the definition
job succeeds. Creating a definition does not configure or deploy the game.

For edits, read `games.custom.get` and pass `game` to both save calls. Keep the
existing `game_id`. Use the plan's normalized definition and revision in the
start call; the read snapshot's revision is not a save-plan revision. Changes
to definitions, selected sources or saved paths invalidate the plan. Name/ID
collisions are rejected. Non-editable handlers require developer mode, matching
the desktop editor. Restore active deployments and stop managed games before
editing or removing their handlers.

A repository-handler edit becomes the selected local definition. A rename
moves the game's saved configuration to the new display name and keeps its
existing staging/profile location. Per-game routing overrides are retained;
`custom_routing_rules` in the definition edits handler defaults, while
`games.routing.*` edits Data-tab overrides for any game.

Previous definitions and source-selection metadata are retained under
`handler_store/backups/<game_id>/<backup_id>/`; the job returns `backup`.
Removing a handler backs up every available source candidate. Profiles and game
files are never deleted by handler removal. Banner images are downloaded to
`custom_games/<game_id>.png`; a failed banner download returns a warning
alongside `saved:true`, and the definition remains usable. Jobs must reach a
terminal state before their result is reported as successful.

Example create workflow:

```json
{"jsonrpc":"2.0","id":"custom-plan","method":"games.custom.save.plan","params":{"definition":{"name":"Example Game","exe_name":"Game/Binaries/Win64/Game.exe","deploy_type":"ue5","mod_data_path":"Game","custom_routing_rules":[{"dest":"Game/Content/Paks/~mods","extensions":[".pak"]}]}}}
```

Call `games.custom.save.start` with the returned `definition`, `revision`, and
`confirm:true`, wait through `jobs.get`/`jobs.events`, then configure the returned
`game_id` using `games.configure.start`. The assistant discovers these methods
automatically through `system.handshake`.

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

`collections.import.start` also accepts an absolute `.r2x` path or a renamed
`.txt` copy. The selected game's Thunderstore community is checked before
creating the profile. Available pinned packages, order, and enabled states are
imported through the shared collection installer; omitted packages appear in
the installation report. This imports the manifest, not r2modman's local config
files. Catalogue/download calls require internet access but no Nexus account.

Profile archives retain empty bundled mod folders, installer sidecars, saves,
INI files, and runtime `Root_Folder` files. An exported Profile Group restores
as a standalone private profile without its original member links/settings.
Archive packing streams into a temporary sibling before atomic publication;
cancellation, changed input files, or write errors preserve the prior output.

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

Collection user-rule merges preserve unrelated LOOT metadata, including
conditional rules, tags, messages, and group descriptions. Authored ordering
lists replace the corresponding collection plugin/group rules, and group names
remain case-sensitive. Invalid or ambiguous userlists are reported without
rewriting the file.

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

### Discover game tools

Read the game's built-in tool catalogue, optionally using one profile's paths
and installed-tool gates:

```json
{"jsonrpc":"2.0","id":"tools","method":"tools.list","params":{"game":"skyrim_se","profile":"default"}}
```

The result contains `game`, optional `profile`, `scope:"built_in"`,
`external_plugins_loaded:false`, `tools`, ordered `categories`, and `revision`.
Each tool has a stable `id`, canonical-English `label` and `description`,
`category`, known launch `executables`, and `headless` information.
`headless.workflow_available` distinguishes an exposed workflow from a desktop
wizard; `related_methods` identifies existing APIs that cover part or all of
its work. A related method alone does not establish full wizard parity.
The revision identifies this catalogue snapshot, not an installation plan.

For `install_reshade`, call `tools.reshade.options.get` with a configured game
and profile. It returns the game's default proxy DLL and architecture, shader
pack choices, and the paths for game, Root_Folder, and managed-mod installation.
The `d3dcompiler_47` state reports whether the selected Proton prefix already
has the dependency and whether its saved prefix is initialized for installation.
Its `list_method` and `install_method` identify the optional dependency APIs. Use
`tools.reshade.dll.acquire.start` with the desired architecture to acquire the
official full add-on DLL; the job returns a local DLL path and a matching
`.dll.reshade.json` identity file. Shader packs and preset trimming can be
prepared with `tools.reshade.shaders.acquire.start`. Its ZIP bundle has a
matching `.zip.reshade.json` identity file.
When a preset has a `Techniques=` list, every optional pack is fetched before
trimming; a preset without enabled effects is ignored and `pack_indices` apply.
Pass the acquired DLL and shader bundle paths to `tools.reshade.inspect.start`,
then submit its revision to `tools.reshade.install.start` with `confirm:true`.
For managed mods, `replace_existing:true` is required to replace an existing
folder. Direct game installs restore the game first. Root_Folder and managed-mod
installs need deployment. The file installer retains both acquired sources and
replaces the complete `reshade-shaders` tree, removing effects and textures
from previous installs even when they are absent from the new bundle. Inspection
reports relative `destination.replaces_trees`; an existing tree appears once in
`overwrites`, and `existing_entries` counts its files, links, and directories.
The plan revision includes the full old tree, including obsolete files.
Unrelated files outside the tree remain. When no preset is supplied, existing
regular `ReShadePreset.ini` files are preserved in game/Root_Folder installs;
Root_Folder also preserves an existing regular `ReShade.log`. Inspection lists
these under `destination.preserves`. A supplied preset is installed normally.
Managed-mod replacement replaces the whole folder, including previous presets,
logs, and other custom files; this requires `replace_existing:true`.

Tree replacements and other payload file changes share a transaction. Old trees
remain in sibling backups until all file writes and source checks succeed.
Cancellation or failure during file installation restores the old trees/files.
If another writer changes an output or backup before rollback, the job returns
`tool_install_rollback_failed` with `state_persisted:true` and retained backup
paths instead of overwriting that change. A successful install reports
`destination.replaced_trees` and `destination.preserved`. Game/profile
configuration changes during installation stop and roll back payload writes.
The initial Restore step, and later indexing/group-refresh failures after
publication, are outside this file transaction and may leave persisted state.
Use `wine.dll_overrides.get` and `wine.dll_overrides.set.start` to apply the
returned override to the selected prefix, or add it to Steam launch options
when no prefix is configured. For optional d3dcompiler_47 installation, list
Wine dependencies and submit the returned revision with
`dependencies:["d3dcompiler_47"]` and `confirm:true`. This also works when the
game's health report does not declare that component. The built-in ReShade
catalogue now advertises `headless.workflow_available:true`: its workflow
composes acquisition, inspection/installation, optional dependency installation,
and Wine override management. Dependency and override jobs remain separate
requests, so their changes are not rolled back with the ReShade file payload.
This advertises public workflow coverage; fixtures do not prove downloaded
release, packaged, or live-game rendering behavior.

Script-extender tools also contain `script_extender.version_required`,
`install_modes` (`game`, `root`, `mod`), and declared `versions`. Each version
has an `index`, `label`, `description`, archive-name `archive_keywords`,
`strip_top_dir` folder policy, and
a `source` object. Sources are `nexus` (exact `domain`, `mod_id`, `file_id`,
and `download_method`), `github_release`, `direct`, or `manual`. URLs describe
the handler's choices; this call does not query providers, pick a build for the
installed game, download archives, or run installers. In particular, Skyrim
SE retains the pinned `2.3.1 Steam` Nexus file and separate older/GOG builds.

To acquire one of those choices, use the revision from `tools.list` and pass
the same optional profile. Multi-version tools require an explicit index:

```json
{"jsonrpc":"2.0","id":"acquire-skse","method":"tools.script_extender.acquire.start","params":{"game":"skyrim_se","tool_id":"install_se_skyrimse","version_index":0,"revision":"<tools.list revision>"}}
```

The job uses only the source declared by that built-in tool. A pinned Nexus
choice downloads the exact file through the existing Nexus downloader;
Premium is required unless `nxm_url` identifies the same file with a valid
manager-download key. Other choices fetch their declared GitHub release asset
or direct HTTPS archive. Downloads are pausable and cancellable, publish only
complete archives to the per-game cache, and retain existing files by choosing
a free filename. The job result gives the archive path. A manual-only choice
returns `manual_download_required:true`, its URL, and archive-name keywords.
The NXM key is never included in job options or events. Acquisition alone
does not install the extender. Use its returned archive path, or an absolute
path to an archive downloaded manually, with the inspection method:

```json
{"jsonrpc":"2.0","id":"inspect-skse","method":"tools.script_extender.inspect.start","params":{"game":"skyrim_se","profile":"default","tool_id":"install_se_skyrimse","version_index":0,"revision":"<tools.list revision>","archive_path":"/absolute/path/to/archive.7z","mode":"game"}}
```

The inspection job extracts into temporary storage without installing files.
Its result identifies the selected version, SHA-256 of the archive, file count,
destination, existing files that would be overwritten, whether game mode will
restore first, whether a matching local download identity was found, and an
opaque `revision`. Installation requires those same parameters and the returned
revision:

```json
{"jsonrpc":"2.0","id":"install-skse","method":"tools.script_extender.install.start","params":{"game":"skyrim_se","profile":"default","tool_id":"install_se_skyrimse","version_index":0,"revision":"<tools.list revision>","archive_path":"/absolute/path/to/archive.7z","mode":"game","plan_revision":"<inspection revision>","confirm":true}}
```

If the archive has no matching local `.tool.json` or `.nexus.json` identity,
set `allow_unverified_source:true` to install a manually supplied archive.
Those sidecars record source identity locally; they are not publisher
signatures. Replacing an existing managed mod also requires
`replace_existing:true`. The install job checks the archive and destination
again under the game operation lock before mutation, preserves previous files
if installation fails, refreshes groups containing a newly installed managed
mod, and keeps the source archive for retry. A failed group refresh reports
`state_persisted:true` because the mod installation has already completed.
Game mode runs
the same restore-before-install step as the desktop wizard; cancellation after
restore starts may leave the game restored even when tool extraction is
rolled back, and the cancelled job reports that state.

Omit `profile` to inspect global paths, including an unconfigured game's
setup tools. A selected profile must already exist. Reads preserve the active
profile and legacy profile files; installed-tool gates may build Filegraph
caches. External Python wizard plugins are excluded because their discovery
executes plugin code and may import GUI classes. Desktop discovery retains
those plugins. Invalid/duplicate built-in tool metadata returns
`invalid_tool_catalogue` or `tool_catalogue_unavailable`.

### Pack and unpack BSA/BA2 archives

The built-in archive-packing wizard advertises a complete workflow in
`tools.list`. Start a scan for a configured game and existing profile:

```json
{"jsonrpc":"2.0","id":"scan-archives","method":"tools.archive_packing.scan.start","params":{"game":"skyrim_se","profile":"default"}}
```

The job returns the supported archive `kind`, safe default `options`, and
`candidates` with file/conflict counts, `can_pack`, `can_unpack`, skip reasons,
and archive groups. Disabled mods with recorded packing ownership remain
available for unpacking. Scans and plans refresh Filegraph caches but preserve
mod files, profile state and legacy state files.

Choose installed mod folder names and prepare a batch:

```json
{"jsonrpc":"2.0","id":"plan-archives","method":"tools.archive_packing.plan.start","params":{"game":"skyrim_se","profile":"default","action":"pack","mods":["Example Mod"],"options":{"compress":true,"delete_loose":false,"skip_winners":true,"split_textures":false}}}
```

All four options are booleans with the defaults shown above. With
`skip_winners:true`, winning conflicting files stay loose. Files routed to the
game root stay loose, and existing excluded files remain excluded. BA2 uses separate general and texture
archives, and candidates requiring separate texture archives force splitting.
Packing keeps existing archived members, preserves unrelated metadata, records
archive ownership, and creates a loading plugin when needed. By default it
retains loose files and disables the packed raw paths in the selected profile.
`delete_loose:true` removes those loose files. Physical folder changes also
affect profiles sharing that storage; exclusion updates belong to the selected
profile, matching the desktop batch.

The plan reports physical owners, affected profiles, archive names, loading
plugins and `can_apply`/`restore_required`. Deployed folders, including aliases
shared with another deployed profile, require Restore. Unreadable deployment
state also blocks application. Profile Group targets use their member-owned
folders; selecting two aliases of one physical folder is rejected.

Apply the same parameters with the returned `revision` and `confirm:true`.
Changed sources, inventory, routing rules, blacklist, ownership, profile state
or options require a new plan. Inputs are checked again before publication.
Changed loose sources are retained and kept enabled if cleanup cannot safely
finish. Unrelated metadata and existing plugin enable/order state are retained;
generated or removed loading plugins are synchronized in every affected
profile. Desktop and headless batches share the operation and game mutation
locks; headless application holds them through the final refresh.

For unpacking, use `action:"unpack"` and omit packing options. Omitting
`archives` selects only archives recorded by the manager. Explicit selection
also supports untracked archives:

```json
{"jsonrpc":"2.0","id":"plan-unpack","method":"tools.archive_packing.plan.start","params":{"game":"skyrim_se","profile":"default","action":"unpack","mods":["Example Mod"],"archives":{"Example Mod":["Example.bsa"]}}}
```

Unpacking preserves existing loose files, removes the selected archives and
unneeded manager-generated loading plugins, updates ownership, and re-enables
the extracted raw paths. Archive names must identify regular BSA/BA2 files
inside their selected mod. Nested links or ambiguous paths in selected mods
are rejected.

All three jobs support `jobs.cancel`. Cancellation is cooperative during
inventory work and archive writing/extraction. Publication and required
profile recovery finish before releasing the lock. Completed mods stay
completed; remaining mods are skipped. A cancelled apply reports its partial
outcomes and refreshed profiles in `error.details`.

A completed batch returns per-mod `outcomes`, `counts`, `ok`,
`state_persisted`, and `synced_profiles`. A job can reach `succeeded` with
`ok:false` when individual mods failed; inspect the counts and outcomes.
Packed loose-file reports contain a count and a sample of at most 20 paths.
Failure after publication reports partial state. Failed rollback retains
original backups and identifies their recovery directory; changed published
or extracted files are retained. A refresh failure returns
`archive_operation_sync_failed` with completed outcomes. Fixture checks cover
native Filegraph, both archive formats and Profile Groups; in-game loading and
packaged execution require separate validation.

### Install Synthesis and prepare its prefix

Built-in Synthesis tools expose `synthesis` metadata in `tools.list` with their
application directory, executable, source and protected paths. These methods
cover acquisition, installation and prefix setup; `headless.workflow_available`
remains `false` because pipeline execution is still pending.

```json
{"jsonrpc":"2.0","id":"synthesis-download","method":"tools.synthesis.acquire.start","params":{"game":"skyrim_se","profile":"default","tool_id":"<Synthesis tool ID>","revision":"<tools.list revision>"}}
```

The acquisition job downloads the latest release's `Synthesis.zip` from the
[upstream releases](https://github.com/Mutagen-Modding/Synthesis/releases/latest),
retains it in the managed cache, and records its checksum and source identity.
Use the returned `archive.path` for inspection:

```json
{"jsonrpc":"2.0","id":"synthesis-inspect","method":"tools.synthesis.inspect.start","params":{"game":"skyrim_se","profile":"default","tool_id":"<Synthesis tool ID>","revision":"<tools.list revision>","archive_path":"/absolute/Synthesis.zip"}}
```

Inspection returns the archive checksum, source verification status,
application destination, existing files to overwrite and an installation
`revision`. It does not install files or initialize a prefix. The archive must
contain `Synthesis.exe`; all application files, including a bundled CLI and
runtime libraries, are retained through the shared guarded extractor.

```json
{"jsonrpc":"2.0","id":"synthesis-install","method":"tools.synthesis.install.start","params":{"game":"skyrim_se","profile":"default","tool_id":"<Synthesis tool ID>","revision":"<tools.list revision>","archive_path":"/absolute/Synthesis.zip","plan_revision":"<inspection revision>","confirm":true}}
```

A manually supplied archive also requires `allow_unverified_source:true`.
Changed catalogue choices return `stale_tool_catalogue`; changed inputs or
application destinations return `stale_synthesis_plan`. Installation rejects
an application destination inside the game directory, a linked prefix, an
archive containing protected paths, and a running or unreadable prefix process
scan. The application folder remains `Applications/Synthesis` alongside staging.
Existing `PipelineSettings.json`, `GuiSettings.json`, `Data`, `.git`, logs and
`prefix` contents are preserved. This follows the
[upstream update guidance](https://mutagen-modding.github.io/Synthesis/Updating-UI/).

Installation uses the game operation, mutation and Synthesis prefix locks, and
rolls back its file writes on failure or cancellation. It neither deploys nor
restores the profile. Acquisition supports `jobs.pause`, `jobs.resume` and
`jobs.cancel`; inspection and installation support cancellation, with checks at
archive extraction and file-write boundaries. The Qt download/update page uses
the same acquisition, inspection and installation runners.

After installation, plan the dedicated prefix setup:

```json
{"jsonrpc":"2.0","id":"synthesis-prefix-plan","method":"tools.synthesis.prefix.plan.start","params":{"game":"skyrim_se","profile":"default","tool_id":"<Synthesis tool ID>","revision":"<tools.list revision>","proton_name":"<installed Proton build name>","save_settings":true}}
```

The plan returns its `revision`, runner and prefix paths, existing component
files, process-scan availability and game registration in both registry views.
It does not start Wine or create the prefix. `components` describes files;
`runtime_execution_verified:false` means the runtime has not been executed.
`can_setup:false` reports a busy prefix or unavailable process scan. An existing
32-bit prefix is rejected.

`proton_name` selects an exact installed build name, including a registered
custom build. If omitted, the saved Synthesis selection is used, otherwise the
first discovered build. An unavailable saved or explicit selection does not
silently fall back. `save_settings` defaults to `false`; `true` atomically saves
the selected build in the existing per-game Synthesis settings during setup.
Keep the same optional parameters when applying the plan:

```json
{"jsonrpc":"2.0","id":"synthesis-prefix-setup","method":"tools.synthesis.prefix.setup.start","params":{"game":"skyrim_se","profile":"default","tool_id":"<Synthesis tool ID>","revision":"<tools.list revision>","proton_name":"<installed Proton build name>","save_settings":true,"plan_revision":"<prefix plan revision>","confirm":true}}
```

Setup targets `Applications/Synthesis/prefix/pfx`, independently of the game's
prefix and inherited Wine environment. It initializes a 64-bit prefix, prepares
Visual C++ and the .NET 10 SDK, ensures Desktop runtimes 10, 8, 7 and 6, imports
CA and SDK trusted roots, applies the existing compatibility patches and fonts,
and writes NuGet configuration. These components preserve the desktop setup
policy. Synthesis requires an SDK to compile patchers; older patchers can need
their own runtime, as described in the
[upstream installation guide](https://mutagen-modding.github.io/Synthesis/Installation/).

An old `.done` marker alone cannot establish that a runtime is installed.
Successful setup checks component files, runs `dotnet --list-sdks` and
`dotnet --list-runtimes` through the selected Wine build, verifies the shared
Bethesda game registration in both registry views, and flushes the dedicated
wineserver. The runtime commands follow the
[official .NET CLI reference](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet).
Installer cache reuse requires matching URL, size and SHA-256 metadata; new
downloads are streamed to a temporary sibling and checked for a Windows PE
header and complete byte count before publication. This verifies cache
identity and format, rather than an Authenticode signature.

Planning and setup support `jobs.cancel`. Setup holds the game operation,
mutation, dedicated prefix and registry-state locks through cleanup and final
readback. Changed inputs return `stale_synthesis_prefix`; missing or ambiguous
runners, linked setup paths, active prefixes and unverifiable cleanup return
typed errors. Cancellation checks download chunks and step boundaries, and
terminates owned installer process groups. An in-flight network read can wait
for its timeout; wineserver cleanup is attempted before the job ends. Cleanup
failures are reported explicitly, and a changed prefix owner or cleanup binary
prevents cleanup from targeting a replacement.

Wine installation is not rolled back on failure or cancellation. Error details
report completed and failed steps, `state_persisted` and `settings_persisted`;
inspect the result, plan again and rerun setup to repair missing components.
Setup neither deploys nor restores the profile and does not run patchers. The
Qt setup page uses the same planning and setup runners. CLI pipeline execution,
patch output capture and Restore remain separate unfinished work.

Temporary-prefix checks exercise the shared setup steps and native process
stand-ins, including failed readback and cancellation. They do not establish
real Wine/.NET installation, CDN availability, patcher execution or packaged
behavior. Setup also relies on the selected Wine build's runtime environment;
it does not provision a virtual display.

### Install and run Wrye Bash

The built-in Wrye Bash entries expose `wrye_bash` metadata in `tools.list` with
`Wrye Bash.exe`, the Applications directory, archive keywords and the declared
[upstream GitHub source](https://github.com/wrye-bash/wrye-bash/releases/latest).
Use the selected profile's catalogue revision throughout this workflow.

```json
{"jsonrpc":"2.0","id":"wrye-download","method":"tools.wrye_bash.acquire.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_wrye_bash_skyrimse","revision":"<tools.list revision>"}}
```

The cancellable, pausable acquisition job selects a release asset whose name
contains both `standalone` and `executable`. It publishes an archive and source
identity into the managed cache only after the download and archive checks
finish. Return values include `archive.path`, size, SHA-256 and release tag.
An interrupted acquisition does not publish its partial archive. A completed
archive is retained for reuse, including across the built-in Wrye Bash entries
for other games. Paused acquisitions can resume in the same service process.

```json
{"jsonrpc":"2.0","id":"wrye-inspect","method":"tools.wrye_bash.inspect.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_wrye_bash_skyrimse","revision":"<tools.list revision>","archive_path":"/absolute/Wrye.Bash.Standalone.Executable.zip"}}
```

Inspection flattens the archive's Mopy wrapper in a temporary payload and
reports its source identity, destination, overwritten-file list, file count and
installation `revision`. It leaves the application and profile state unchanged.

```json
{"jsonrpc":"2.0","id":"wrye-install","method":"tools.wrye_bash.install.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_wrye_bash_skyrimse","revision":"<tools.list revision>","archive_path":"/absolute/Wrye.Bash.Standalone.Executable.zip","plan_revision":"<inspection revision>","confirm":true}}
```

Installation shares the desktop transaction. It rechecks archive, catalogue,
configuration, destination and source identities before publication; unrelated
application files and the downloaded archive are retained. A manually supplied
archive needs `allow_unverified_source:true`. Changed inputs return
`stale_wrye_bash_plan` or `stale_tool_catalogue`. Inspection and installation can
be cancelled, but cannot be paused or resumed after restart.

```json
{"jsonrpc":"2.0","id":"wrye-plan","method":"tools.wrye_bash.run.plan.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_wrye_bash_skyrimse","revision":"<tools.list revision>","prefix_mode":"isolated","deploy":true}}
```

Run planning is read-only. It returns a run `revision`, resolved `settings`,
`exe_path`, runner and prefix, final `arguments`, `game_link`, `output_path`
(Overwrite), deployment/settings choices and `restore_after_run:true`.
Optional settings are `proton_name`, `prefix_mode` (`isolated`, `shared`, or
`game`), `launch_args`, `launch_env`, `winetricks_style`, `deploy` (default true),
`root_folder_enabled` (default true), and `save_settings` (default false).
Saved per-tool settings supply omitted launch choices. Runs require a concrete
profile, an installed runner and an idle prefix with an available process scan.
The manager supplies Wrye Bash's `-o` game path; saved/extra arguments cannot
replace it, and environment overrides cannot redirect the selected prefix.

```json
{"jsonrpc":"2.0","id":"wrye-run","method":"tools.wrye_bash.run.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_wrye_bash_skyrimse","revision":"<tools.list revision>","prefix_mode":"isolated","deploy":true,"plan_revision":"<run revision>","confirm":true}}
```

Execution rechecks the plan before mutation and shares the desktop lifecycle.
The game, mutation and prefix locks cover deployment, prefix preparation, the
process, patch capture and Restore. An installation-specific link under
`C:\wb_games` gives Wrye Bash a writable C-drive working path without collisions
between installs sharing a folder name. Existing wrong links or occupied link
paths are rejected. Registry seeding and plugins/My Games links use the shared
Bethesda prefix preparation. Data directories come from the game handler,
including Morrowind's `Data Files` layout.

After process cleanup, changed/new nonempty `Bashed Patch*.esp/esm/esl` files
from a physical deployment are copied to Overwrite before Restore. VFS runs
use private plugin copies and shared persistence to save modified plugins to
their owner or Overwrite. Successful results include `exit_codes`,
`captured_plugins`, `saved_vfs_plugins`, `restore_attempted`, `restored`, and
`needs_deployment`; deploy again to use newly captured output. Wrye Bash may
change its linked plugin/load-order lists during the run. Changed unrelated
configuration, application binaries or deployment ownership trigger a stale or
recovery error. Output capture failure retains the deployed state for recovery.

Run planning and execution accept `jobs.cancel`. Execution stops the owned
process group, preserves completed output and attempts required Restore before
releasing locks. Prefix setup may delay cancellation at an existing blocking
Wine call. Failures and cancellations include partial lifecycle results and
`state_persisted` where mutation was attempted. Runs cannot be paused or resumed
after restart. `external_tool_ui:true` means Wrye Bash itself requires a graphical
session; the manager workflow has no Qt dependency and does not automate building
a patch inside Wrye Bash's UI. Fixture/offscreen checks do not establish actual
Wine/Proton, VFS mount or packaged-runtime behavior.

### Install xLODGen, TexGen and DynDOLOD

The built-in tools expose a `lod` entry in `tools.list` describing their fixed
64-bit executable, Applications directory, archive keywords, game flag,
output-directory name and source. xLODGen choices cover the existing supported
Bethesda handlers. TexGen and DynDOLOD share one standalone application
bundle. Source choices follow the [xLODGen README](https://github.com/sheson/xLODGen/blob/main/README.md)
and [DynDOLOD downloads](https://dyndolod.info/Downloads).

Acquire the selected tool using its catalogue revision:

```json
{"jsonrpc":"2.0","id":"get-lod","method":"tools.lod.acquire.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_xlodgen_skyrimse","revision":"revision-from-tools.list"}}
```

xLODGen resolves the declared GitHub release asset, streams it into the game's
download cache and records its digest and source identity. Downloads support
pause, resume and cancellation during the same headless process. An acquired
xLODGen archive can be reused for another supported game; its generic source
identity is shared while each installation still pins that game's tool choice
and profile. Existing cache files are preserved.

TexGen and DynDOLOD return `manual_download_required:true`, a Nexus download
URL and archive keywords. No file is pinned for automatic Nexus acquisition.
Archives downloaded through the public Nexus download method are recognized
when their identity belongs to the declared DynDOLOD standalone mod.

Inspect the selected archive before installation:

```json
{"jsonrpc":"2.0","id":"inspect-lod","method":"tools.lod.inspect.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_xlodgen_skyrimse","revision":"revision-from-tools.list","archive_path":"/absolute/path/to/xLODGen.7z"}}
```

Inspection extracts into temporary storage, selects the application folder,
requires the chosen executable and checks extracted files for links, special
files and case collisions. The result
contains the archive digest and `source_verified`, file count, executable and
destination paths, existing output collisions and a plan `revision`. The
archive, application files and legacy profile files remain unchanged.

Call `tools.lod.install.start` with the same parameters, the inspection
`plan_revision` and `confirm:true`. Set `allow_unverified_source:true` when
the archive has no matching recorded download identity. Mismatched identities
remain errors. Applications live in the game's shared
`Applications/xLODGen` or `Applications/DynDOLOD` directory, outside managed
mods. Staging inside the game directory returns `lod_installation_in_game`.

Installation preserves unrelated application files and retains the source
archive. It holds the shared operation and game mutation locks, rechecks
configuration, source identity and destination state, and rolls back its own
file writes on failure or cancellation. Changed plans return `stale_lod_plan`;
changed choices return `stale_tool_catalogue`. Incomplete rollback identifies
the retained recovery files. The desktop wizard uses the same installer and
closing it requests cooperative cancellation.

Acquisition, inspection and installation require no manager dialogs.
Installing the application does not launch it. Generation is a separate,
revision-checked workflow described below. Live downloads, desktop presentation
and real LOD/Proton execution require separate validation.

### Generate and import LOD

`tools.lod.run.plan.start` plans the selected xLODGen, TexGen or DynDOLOD run.
All three tools use their declared 64-bit executable and game flag. The manager
remains free of Qt; the external tool still presents its own generation UI.
The API reports `external_tool_ui:true`. Configure generation in that tool and
close it after it finishes, following the
[official generation instructions](https://dyndolod.info/Generation-Instructions).

```json
{"jsonrpc":"2.0","id":"plan-lod","method":"tools.lod.run.plan.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_xlodgen_skyrimse","revision":"revision-from-tools.list"}}
```

Optional settings are `proton_name`, `prefix_mode` (`isolated`, `shared` or
`game`), `launch_args`, `launch_env`, `winetricks_style`,
`prefer_discrete_gpu`, `deploy`, `root_folder_enabled` and `save_settings`.
Saved settings are used unless overridden; `deploy` defaults to true and
`save_settings` defaults to false. TexGen and DynDOLOD apply the same discrete
GPU selection used by the desktop wizard. The manager supplies `-d:`, `-o:`
and the game flag; overriding these or the managed INI/plugin paths in
`launch_args` returns `lod_argument_override`. Prefix redirection must use
`prefix_mode`, rather than conflicting environment variables.

The default `mod_name` is `xLODGen_Output`, `TexGen_Output` or
`DynDOLOD_Output`. Disable or remove the previous output before planning.
Enabled previous output returns `lod_previous_output_enabled`; a locked
replacement returns `lod_output_locked`. `replace_output:true` explicitly
permits replacing an existing disabled output. The previous mod stays in
place until generation and deployment recovery succeed.

The plan returns its `revision`, runner/prefix details, arguments,
`output_mod` and an absolute `output_path`. When no path is supplied, the
manager chooses a new folder under `$XDG_DATA_HOME/AmethystModManager/lod-runs`
(or `~/.local/share/AmethystModManager/lod-runs`). No work folder is created by
planning. Custom paths must be new and outside game, profile, staging,
application and prefix folders. Generation writes into its `files` subfolder,
then imports those files as a mod, as required by the
[upstream output guidance](https://github.com/sheson/xLODGen/blob/main/README.md).
Profile groups must select a concrete profile to own the new output.

```json
{"jsonrpc":"2.0","id":"run-lod","method":"tools.lod.run.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_xlodgen_skyrimse","revision":"revision-from-tools.list","output_path":"/absolute/work/folder/from-plan","plan_revision":"revision-from-run-plan","confirm":true}}
```

Execution must repeat the planning options and the returned `output_path`.
It rechecks the profile, tool and helper binaries, runner, prefix, input files
including linked assets, output target and catalogue revision before writing.
Deployment and Restore reload the inspected path choices without discovering
or persisting a different game prefix.
Changed inputs return `stale_lod_run`. The shared game, prefix and output
leases remain held through preparation, the complete tool process, deployment
recovery and output registration. Cancellation stops the managed tool and
performs recovery before reporting a terminal job state.

A zero exit code and nonempty validated output are required for import. Links,
special files and case collisions are rejected. Import replaces the entire
selected output mod, registers it at highest priority as an enabled data mod,
indexes its files and synchronizes generated plugins. The result reports
`output_mod`, `file_count`, `restored`, `output_retained` and
`needs_deployment:true`. Deploy again to use the generated output. Successful
imports remove their work folder; failed or cancelled runs retain it for
inspection or manual recovery and report its path. Registration rollback
preserves external edits and identifies the retained original backup when
recovery cannot safely finish. Index/plugin synchronization failures report
`lod_output_sync_failed` with the installed mod and `state_persisted:true`.
The desktop wizard uses the same run, import and recovery paths.

### Install an xEdit application

The four built-in Nexus/Discord xEdit and QuickAutoClean wizard types expose
an `xedit` entry in `tools.list`. It describes the selected executable, optional
64-bit executable, Applications subfolder, game mode, archive keywords, and
source. These values and the run workflow are shared with the desktop view.
`headless.workflow_available` is true. Interactive xEdit still opens the
external tool's window; the manager itself does not start Qt.

Acquire a pinned Nexus build using the revision from the selected profile's
catalogue:

```json
{"jsonrpc":"2.0","id":"get-xedit","method":"tools.xedit.acquire.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_sseedit_skyrimse","revision":"revision-from-tools.list"}}
```

Nexus downloads use the existing authentication and Premium/NXM rules. An
optional `nxm_url` must identify the declared domain, mod, and file. A build
without a pinned Nexus file returns `manual_download_required:true` with its
source and keywords. Discord builds require a supplied archive; acquisition
does not scrape Discord or substitute an older GitHub build.

Inspect the archive before installing it:

```json
{"jsonrpc":"2.0","id":"inspect-xedit","method":"tools.xedit.inspect.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_sseedit_skyrimse","revision":"revision-from-tools.list","archive_path":"/absolute/path/to/SSEEdit.7z"}}
```

The shared installer extracts into temporary storage, collapses wrapper
folders using the desktop helper, and requires the selected executable in the
new payload. It rejects a wrong build even when an older executable already
exists at the destination. Optional executables, Edit Scripts, and other
payload files retain their directory layout. The destination is the existing
`Applications/<app_dir>` next to the game's shared mods folder, including
when the selected profile uses private mods. This keeps xEdit outside the
game's Data folder, as described by the
[upstream installation guide](https://tes5edit.github.io/docs/2-overview.html#AcquisitionandInstallation).

Inspection returns `destination.path`, `destination.exe_path`, an overwrite
count and sample, source identity, file count, and `revision`. Installation
takes the same parameters plus `plan_revision` and `confirm:true`. A manual
archive without a matching managed identity requires
`allow_unverified_source:true`. Conflicting or mismatched sidecars are rejected
even with that flag. Existing payload files are updated; files absent from the
new payload, including user settings, scripts, and prefix state, remain.
Both headless and desktop installation retain the source archive. No modlist
Restore, deployment, indexing, prefix initialization, or xEdit process launch
occurs during installation.

The plan includes catalogue choices, archive/source identity, game/profile
configuration, and affected destination files/directories. A stale plan returns
`stale_xedit_plan`; changed tool choices return `stale_tool_catalogue`.
Configuration and sidecar changes during publication roll back payload writes.
Destination links in directory positions are rejected. Replacing a leaf link
does not modify its target. The desktop and headless installers share the
operation/deployment locks and file transaction. Failure/cancellation restores
previous payload files; concurrent output edits can return
`tool_install_rollback_failed` with `state_persisted:true` and retained backups.
Cancellation is cooperative between extraction and file writes; an archive
extractor finishes its current call first. These checks do not establish a
live xEdit/Proton run or packaged behavior.

### Run xEdit and QuickAutoClean

Plan a run after installing the selected application:

```json
{"jsonrpc":"2.0","id":"plan-clean","method":"tools.xedit.run.plan.start","params":{"game":"skyrim_se","profile":"default","tool_id":"run_sseedit_qac_skyrimse","revision":"revision-from-tools.list","mode":"qac_all","plugins":["Example.esp"],"prefix_mode":"isolated"}}
```

The cancellable planning job reads installed executables, saved launch settings,
the selected profile and LOOT's dirty-plugin report. It does not initialize a
prefix, download UMU, deploy or launch a process. The result includes `revision`,
`settings`, `exe_path`, `prefix`, base `arguments`, `dirty_plugins`, selected
`plugins`, `can_run`, and `external_tool_ui`.

`mode` defaults to `interactive`. `qac_all` requires a QuickAutoClean tool and
uses enabled dirty plugins, including present vanilla plugins. Omit `plugins`
to select the entire report, or supply unique names from it. An empty batch
has `can_run:false` and cannot execute. Each selected plugin runs in its own
process using `-autoload` and `-autoexit`, following the
[upstream command-line options](https://raw.githubusercontent.com/TES5Edit/TES5Edit/dev-4.1.6/whatsnew.md).
The actual tool can still display errors or other windows; these methods do
not convert xEdit itself into a console application.

Optional settings are `use_64bit`, `proton_name`, `prefix_mode` (`isolated`,
`shared`, or `game`), `launch_args`, `launch_env`, and `winetricks_style`.
Omitted settings use the same saved values as the desktop view. `game` uses
the game's configured runner and prefix, including classic Wine prefixes;
`proton_name` is a preference for isolated/shared Proton selection. Prefix
redirection through `launch_env` is rejected. `deploy` and
`root_folder_enabled` default to true. With `deploy:false`, an existing
deployment must belong to the selected profile. A deployed profile with
different game/prefix paths must be restored separately first.

Submit the identical parameters to `tools.xedit.run.start`, adding the run
plan's `plan_revision` and `confirm:true`. Changing the executable, runner,
prefix registry, profile configuration, deployment state, or plugin sources
invalidates the plan with `stale_xedit_run`. `save_settings:true` persists
the chosen launch settings before preparation; otherwise request overrides
apply only to this run.

The operation and prefix locks cover settings, Deploy, prefix preparation,
all tool processes and recovery. An active or uninspectable prefix is rejected
before mutation. After each process, pending saves are finalized and changed
VFS plugins are copied to their owning mod or Overwrite. The standard Restore
pipeline recovers Data, Root Folder and Filegraph state before releasing the
locks. Nonzero exits fail the job after recovery; successful results report
`completed_plugins`, `exit_codes`, `finalized_saves`, `saved_vfs_plugins`, and
preparation/recovery flags.

Cancellation terminates only this run's registered tool processes, preserves
edited plugins and completes recovery. Prefix initialization and Deploy/Restore
are cooperative boundaries: cancellation waits for an in-flight call.
Errors include the preparation/recovery flags and `state_persisted` when
changes were attempted. `xedit_recovery_failed` means saving or Restore failed;
inspect its `failures` and recover the deployment before continuing. A failed
plugin save keeps the deployed view available for recovery. Process restart
recovery and real xEdit/Proton runs remain separate validation work.

### Install SMAPI for Stardew Valley

The built-in `sdv_smapi` tool advertises `headless.workflow_available:true`.
Its `smapi` catalogue entry describes the official GitHub source, archive
keywords, and three installation modes. Acquire the installer through
`tools.smapi.acquire.start`:

```json
{"jsonrpc":"2.0","id":"get-smapi","method":"tools.smapi.acquire.start","params":{"game":"Stardew_Valley"}}
```

Poll the job to obtain `archive.path`. The cached ZIP has a matching
`.zip.tool.json` sidecar. Acquisition checks that the selected release asset
belongs to `Pathoschild/SMAPI`, and supports pause/cancel during download.
The archive remains available after installation. Inspect the chosen destination:

```json
{"jsonrpc":"2.0","id":"inspect-smapi","method":"tools.smapi.inspect.start","params":{"game":"Stardew_Valley","profile":"default","archive_path":"/absolute/path/to/SMAPI-installer.zip","mode":"mod","mod_name":"SMAPI"}}
```

The result includes `archive.source_verified`, destination paths and overwrites,
`destination.preserves`, `destination.removes`, `game_sources`,
`restore_before_install`, `needs_deployment`, and `revision`. Pass the same
selection with `plan_revision` and `confirm:true` to `tools.smapi.install.start`.
For manually selected archives without managed source identity, add
`allow_unverified_source:true`. Replacing an existing managed-mod folder also
requires `replace_existing:true`; this replaces the complete folder. Profile
groups must install into a member profile. Unsupported games return
`smapi_unavailable`.

The shared native installer prepares only `internal/linux/install.dat`, or the
older `internal/unix/install.dat`, inside temporary storage. Archive traversal,
links, encrypted entries, duplicate paths, ambiguous platform payloads, and
oversized payloads are rejected. The nested archive is limited to 256 MiB,
individual files to 128 MiB, and the expanded tree to 512 MiB and 50,000 entries.
The payload must include the native apphost, Unix launcher, and SMAPI assembly.
It cannot replace reserved vanilla files. The selected game must have the native
dependency manifest, and a direct install needs a regular vanilla launcher or
existing launcher backup. These assumptions match SMAPI's
[native launcher and manifest setup](https://github.com/Pathoschild/SMAPI/blob/develop/src/SMAPI.Installer/InteractiveInstaller.cs).

Game mode restores the modlist before writing, then refreshes game-derived
files from the restored game. A deployed profile using a different game path
returns `smapi_restore_target_mismatch` before Restore. An existing regular
`StardewValley-original` is preserved; otherwise the current vanilla launcher
is backed up. SMAPI's Unix launcher becomes `StardewValley`, and the game's
`Stardew Valley.deps.json` becomes `StardewModdingAPI.deps.json`. Staged installs
leave the game launcher untouched and use normal deployment backups.
Both native launchers are marked executable. An obsolete `unix-launcher.sh`
destination leaf is removed without following a link. Unrelated files outside
the selected payload remain in game/Root_Folder installs. This implements the
manager's native install workflow; it does not perform upstream uninstall or
global-user-data migration steps.

The revision includes archive/source identity, game/profile configuration,
deployment state, native game source files, and destination state. Changes
before publication return `stale_smapi_plan` or a source-specific error.
Native manifest changes during publication roll back payload writes. Files,
launcher backup creation, and launcher-leaf removal share a file transaction.
Failures/cancellation recover those changes; concurrent output edits can return
`tool_install_rollback_failed` with retained backups and `state_persisted:true`.
Desktop installation now uses the same payload preparation, transaction, and
operation lock. Modlist Restore, later indexing, and profile-group refresh are
outside the file transaction and can leave persisted state, reported through
error details. Cancellation occurs during archive/copy preparation and between
payload writes; modlist Restore finishes its current call first. A successful
install reports `needs_deployment` and any refreshed profile groups. Source
fixtures do not establish live SMAPI startup or Flatpak/AppImage behavior.

### Apply or restore a 4GB executable patch

The New Vegas and Oblivion patch tools advertise
`headless.workflow_available:true` in `tools.list`. They use the same native
patch code as the desktop wizards. New Vegas recognizes supported executable
hashes and applies the existing LAA/NVSE-loader patches. Oblivion checks its PE
header and updates the LAA flag and checksum; it does not install OBSE.

Inspect the configured game/profile first:

```json
{"jsonrpc":"2.0","id":"inspect-4gb","method":"tools.4gb_patch.inspect.start","params":{"game":"FalloutNV","profile":"default"}}
```

Poll the job. Its result contains `state` (`missing`, `unknown`, `patchable`,
or `patched`), `variant`, executable SHA-1/SHA-256, `backup_sha256`,
`apply_available`, `restore_available`, `restore_before_mutation`,
`automatic_patch_enabled`, and `revision`. Unsupported games return
`patch4gb_unavailable`. Unrecognized executables cannot be patched.

Pass the revision and `confirm:true` to the desired action:

```json
{"jsonrpc":"2.0","id":"apply-4gb","method":"tools.4gb_patch.apply.start","params":{"game":"FalloutNV","profile":"default","revision":"<inspection revision>","confirm":true}}
```

Use `tools.4gb_patch.restore.start` with the same parameters and a fresh
inspection revision to restore a backup. A changed executable, backup, game
path, profile configuration, or deployment state returns `stale_patch4gb_plan`.
The revision is checked before any modlist Restore. If mods are deployed,
both actions restore the modlist first and leave it restored. When the last
deployed profile points to another game folder, the operation stops with
`patch4gb_restore_target_mismatch`; restore that profile first. Symlinked
executables can be inspected while deployed but must become regular files
after Restore. Backups must be regular files. Executables/backups larger than
256 MiB or empty files are rejected.

Patch preparation works on temporary copies. The executable and backup are
published through a shared file journal: Apply creates a backup only when
absent, preserving an existing backup; backup Restore replaces the executable
and consumes that backup. Failure or cancellation during publication recovers
both files. Recovery never overwrites a concurrently edited output; an
incomplete recovery returns `patch4gb_rollback_failed`, `state_persisted:true`,
and the affected paths and retained backups. Desktop patch workers also use
this transaction and the shared game-operation lock.

A successful result includes `action`, `restore_attempted`, and a fresh
`report`. If New Vegas automatic patch management already consumed the backup
during modlist Restore, its executable hash must match the inspected backup;
the result reports `backup_restored_by_modlist_restore:true`. Automatic patch
management remains configured as before; when enabled, a later New Vegas
deploy can reapply the patch. Jobs can be cancelled during inspection/copying
and between publication steps. The existing patch core and modlist Restore
finish their current call before cancellation is observed. Modlist Restore
and failures after publication may leave persisted state; error details
report `restore_attempted` and `state_persisted`. Source fixtures do not prove
a real game launch or packaged build.

### Interactive FOMOD sessions

Clients can present controller or web installer pages without importing Amethyst
modules. Discover support through `capabilities.installer_sessions` and prepare
an archive returned by `downloads.list`:

```json
{"jsonrpc":"2.0","id":"wizard-open","method":"installers.sessions.open.start","params":{"game":"skyrim_se","profile":"default","archive_id":"archive_<sha256>"}}
```

Poll the job to completion. Its result contains `session` with `id`, `revision`,
`title`, proposed `mod`, visible `steps`, effective `selections`, module dependency
status and validation `errors`. Each step and group has its original integer
`index`; these are not positions in the filtered visible list. Options include
their exact `name`, description, selected state, effective type, lock status,
missing-dependency and previous-selection flags, and `image_available`.

```json
{"jsonrpc":"2.0","id":"wizard-choice","method":"installers.sessions.update","params":{"session_id":"<id>","revision":"<current revision>","step":0,"group":0,"option":"High"}}
{"jsonrpc":"2.0","id":"wizard-image","method":"installers.sessions.image.get","params":{"session_id":"<id>","step":0,"group":0,"option":"High"}}
```

Updating toggles the requested option using the shared desktop FOMOD session.
The returned `session` recomputes conditional steps, required/unavailable options
and validation errors. Locked choices remain unchanged. Read the latest state
with `installers.sessions.get` and use its revision for the next update or install.
Images return `image.mime_type:"image/jpeg"` and base64 bytes in `image.data`;
they are limited to 960 by 540 pixels and cannot address files outside the archive.

```json
{"jsonrpc":"2.0","id":"wizard-install","method":"installers.sessions.install.start","params":{"session_id":"<id>","revision":"<current revision>","confirm":true,"if_exists":"fail"}}
```

Installation reopens the retained archive through the normal install pipeline,
checks archive identity, FOMOD configuration, dependency context and profile state,
and validates the choices before staging. Changed inputs fail with
`stale_installer_session`; reopen the wizard to review them. `if_exists` defaults
to `fail`; use `replace` explicitly for an existing mod. Installation can be cancelled during preparation and file staging. The current
mod is rolled back before cancellation completes. Success closes the session; failure retains it for
review or explicit `installers.sessions.close`.

Sessions currently support parsed XML FOMODs. BAIN and bundles continue to use
`downloads.inspect.start` with explicit selections in `downloads.install.start`.
At most eight sessions can retain temporary extractions per process. Sessions
expire after 30 minutes without access. Temporary extractions are reclaimed on
subsequent session access or shutdown; saved drafts remain until installed or
explicitly closed. List drafts with `installers.sessions.saved.list`, then call
`installers.sessions.resume.start` with a saved `session_id`. Reopening extracts
the archive again, checks its identity and the original profile binding, and
restores the draft choices. Changed archives, dependencies or profile files
return `stale_installer_session`; open a fresh session to review those changes.
Resume returns a new session ID and revision. `installers.sessions.close` also
removes an expired or saved draft. Drafts live under the manager configuration's
`headless/installers` directory. They do not resume an install that was killed
while writing files.

### Single-file Nexus browser downloads

The OAuth-authenticated browser handoff supports free accounts whose browser saves
an archive instead of delivering an NXM link:

```json
{"jsonrpc":"2.0","id":"manual-download","method":"nexus.download.manual.start","params":{"game":"skyrim_se","profile":"default","mod_id":3863,"file_id":1000172397}}
```

The initial response contains `job` and a public `browser_url` for that exact file.
The client opens this URL in its own browser; Amethyst does not open a desktop
browser or append `nmm=1`. The user completes Nexus's browser download normally.
The job validates the OAuth session and file metadata, then uses the shared
browser watcher over enabled Downloads and extra download locations. An existing
matching complete archive is also accepted. Progress reports the waiting/download
phases; an idle timeout fails with `nexus_manual_download_timeout`.

Success returns `archive.id`, path, size, exact `source` identity and file metadata.
Amethyst saves the matching Nexus identity sidecar for subsequent installs and
rejects a conflicting existing identity. Call `downloads.inspect.start`, an
installer session, or `downloads.install.start` with that archive ID. This job
does not install files, copy the browser archive, or bypass Nexus download rules.
Cancelling the job or disconnecting stops the watcher and retains browser files.
Like other jobs, it occupies the service's single job slot while waiting.

### Thunderstore dependency installation

Choose an exact version from `thunderstore.packages.versions.start`, then request
a plan for a real profile:

```json
{"jsonrpc":"2.0","id":"ts-plan","method":"thunderstore.install.plan.start","params":{"game":"riskofrain2","profile":"default","namespace":"ExampleAuthor","package":"ExampleMod","version":"1.2.3"}}
```

The completed job returns `plan.id`, `revision`, community, staging path, whether
staging is shared, total download bytes,
version conflicts and `packages` in dependency-first order. Each package includes
its exact version/dependencies, destination `mod_name`, existing installed version,
`root` flag and action: `install`, `replace`, `enable` or `satisfied`.
The shared resolver chooses the highest pin when dependencies request conflicting
versions. Already-enabled dependencies at a sufficient version need no changes;
disabled sufficient versions are offered for activation. Selected installations
and replacements are enabled, including the requested root package.
Replacing a mod in shared staging also changes that folder for other profiles
using it; review the plan before allowing replacements.

```json
{"jsonrpc":"2.0","id":"ts-install","method":"thunderstore.install.start","params":{"game":"riskofrain2","profile":"default","plan_id":"<plan id>","revision":"<plan revision>","confirm":true,"if_exists":"replace"}}
```

Omit `dependencies` to select every planned dependency. Supply an explicit array
of package IDs (`namespace-package`) to choose a subset, or `[]` to skip them all;
the root is always selected. Skips are explicit opt-outs and appear in the result.
Replacement requires `if_exists:"replace"`; the default is `fail`.
`installer_policy` is `reject` by default or `defaults` when explicitly accepted.
Explicit FOMOD/BAIN/bundle selections apply to the root; dependency archives use
the conservative reject policy. A dependency requiring interactive choices stops
the operation so it can be reviewed through the archive/installer APIs.

Plans are process-local, expire after 30 minutes, and retain at most eight entries.
Application rechecks profile/staging state, package sizes/dependencies and community
membership under the operation lock before changing files. A changed library or
source fails with `stale_thunderstore_plan`. Each package uses the shared download
and install transactions, preserving source metadata and existing folder names.
Cancellation is cooperative during downloads and between packages; an active
installation finishes its transaction before stopping. Completed packages remain
installed. Success returns `installed`, `enabled`, `satisfied`, `skipped_dependencies`
and `remaining`. Failures/cancellation include that report in `error.details.report`;
inspect the nested cause and current inventory before planning a retry.

### Installing a downloaded archive

Use the opaque archive ID from `downloads.list`, or supply `archive_path` with
an absolute local archive path. Supply exactly one source. Local archives do
not need to be in a scanned download directory and are retained in place:

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
and reinstall jobs can be cancelled during extraction, while waiting for choices,
and between copied files. Cancellation removes the current incomplete new mod
or restores its previous folder. Once profile/index commits begin, that mod
finishes committing. Multi-mod installs retain already completed mods and report
them in cancellation details; each current replacement has its own rollback.
There is no pause or automatic restart of an interrupted filesystem transaction.

Install directly into a real profile. Profile groups require an ownership
choice, so the method rejects a group and returns its member names; call it
again with the intended member profile. Ordinary Fluffy multi-mod archives use
the selectable bundle path. When a plain archive has an unrecognised structure,
the non-interactive installer follows
its existing fallback and installs the layout as-is.
For FOMODs, accepted defaults are saved as the effective installer choices and
used for later dependency/rerun checks. An unmet FOMOD module prerequisite
finishes with `installer_dependencies_missing` before replacing any mod files.

### Layout, game-specific and collection installer questions

Set `interactive:true` on `downloads.install.start`, `mods.reinstall.start`,
or collection install/import/resume/update/append jobs to answer installer
questions over the persistent stdio connection. This is separate from
`manual_interactive`, which handles collection archive acquisition. Author-saved
collection FOMOD/BAIN selections still apply automatically; missing choices use
these requests. Without `interactive`, collection defaults remain unchanged.

Poll `installers.requests.get` with `job_id`. Its `requests` array contains
independent questions, each with an `id`, `revision`, `type`, and its available
choices. Jobs also emit `installer_request` events. More than one collection
worker can wait for a response, so handle every returned request.

```json
{"jsonrpc":"2.0","id":"questions","method":"installers.requests.get","params":{"job_id":"<install job ID>"}}
{"jsonrpc":"2.0","id":"map","method":"installers.requests.submit","params":{"job_id":"<install job ID>","request_id":"<request ID>","revision":"<request revision>","answer":{"content_prefix":"textures"}}}
```

- `layout` requests include required directory names and a bounded file preview.
  Answer with `content_prefix`, or an empty string to accept the current layout.
  A non-interactive archive install that needs this GUI mapping fails with
  `installer_layout_required`. Retry with an explicit `content_prefix` instead.
  The prefix is used when automatic structure detection needs a mapping.
- `choice` requests include a title, prompt, option labels and default index.
  Answer with `{"option":"<exact label>"}`. DAO `OverrideConfig.xml` variants
  use this route rather than silently retaining defaults in interactive jobs.
- `bain` requests include subpackage names, labels and defaults. Answer with
  `{"selected":["00 Core"]}`; an empty array is accepted.
- `fomod` requests include visible steps, groups, options, effective selections
  and errors. Toggle choices using `installers.requests.update` with `job_id`,
  `request_id`, `revision`, integer `step` and `group`, and exact `option` name.
  Use the returned revision for subsequent actions. Answer with
  `{"confirm":true}` once the selections are valid. Conditional visibility is
  recomputed by the same session model as the desktop wizard.

Responses with stale revisions or invalid selections are rejected without
releasing the waiting worker. Cancel the job to stop waiting; closing the service
also cancels pending interactive jobs. Running collection questions are not
persisted. Standalone FOMOD sessions provide saved drafts as described above.

### Installing independent mods from one archive

Plain Fluffy folder-per-mod archives keep their existing single-bundle default.
Archive inspection exposes `installer.choices.independent_mods` when those
folders can also be installed separately. Explicit `multi_mod_selected`,
`multi_mod_names`, or `multi_mod_policies` opts into that separate-mod workflow:

```json
{"jsonrpc":"2.0","id":"split","method":"downloads.install.start","params":{"game":"devil_may_cry_5","profile":"default","archive_path":"/path/to/mods.zip","multi_mod_selected":["Alpha","Beta"],"multi_mod_names":{"Alpha":"Alpha Alternative"},"multi_mod_policies":{"Beta":"replace"}}}
```

Selection and mapping keys are exact source mod names from inspection.
`multi_mod_names` changes destination folder names. `multi_mod_policies` accepts
`fail`, `replace`, or `skip` per source mod; omitted entries use `if_exists`.
At least one mod must remain selected. Unknown source names and duplicate or
unsafe destinations fail before staging. Authored bundles with `AddonFor` or
`NameAsBundle` continue to use `bundle_selected`.

Each installed mod records its original source name in `meta.ini`.
`mods.reinstall.start` can then reinstall that one source mod from its recorded
archive, including a renamed destination, without replacing its siblings.

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
separate-folder multi-mod archive can reinstall an independent mod when its
source name was recorded by the separate-mod installation workflow. The source archive is retained, and the result
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

### Nexus Quick Update

Read a plan before updating a selection or every flagged Nexus mod:

```json
{"jsonrpc":"2.0","id":"quick-plan","method":"mods.quick_update.plan.start","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","@group:example"]}}
```

Poll the job for `result.plan`. The plan expands mod groups to real members,
keeps profile order, resolves Profile Group member owners, and avoids updating
the same owner twice. It reports `updates`, `skipped`, `failed`, `counts`, and
`revision`. Each update identifies the exact domain/mod/file, installed and
target version, affected profiles, and owner. Nexus login is needed to read
live file catalogues. Other sources are unsupported by Quick Update.

Omitting `mods` considers only Nexus mods with a saved pending-update flag.
An explicit selection also checks unflagged mods. Both respect update ignores
and ignored versions. The newest file in the installed file's display-name
family is selected using the desktop matcher; renamed local folders retain
that family when the installed file is still listed. A mod without a suitable
name match is skipped and can use Change Version instead.

Execute with the same selection and the returned revision:

```json
{"jsonrpc":"2.0","id":"quick-update","method":"mods.quick_update.start","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","@group:example"],"revision":"<plan revision>","confirm":true,"installer_policy":"reject","per_mod":{"SkyUI":{"nxm_url":"nxm://skyrimspecialedition/mods/3863/files/12345?key=<key>&expires=<expires>"}}}}
```

The job replans under the game operation lock before acquiring anything.
`quick_update_plan_changed` includes a new plan for changed metadata, group
selection, owners, profile settings, or live recommendations. Archive-install
history is excluded from mod revisions so earlier successful replacements do
not invalidate later targets. Mod metadata and other profile state remain
guarded before each replacement.

Matching cached archives work without Premium. Missing archives use the shared
Nexus downloader; automatic acquisition requires Premium, while free accounts
can provide an exact keyed NXM link. `per_mod` keys must identify planned updates,
and each value accepts `archive_id` or `nxm_url`, an optional installer policy,
and explicit `bain_selected`, `fomod_selections`, or `bundle_selected`. NXM
credentials are excluded from public job options. Ambiguous caches require an
explicit archive choice. Acquired archives and identity sidecars are checked
again before installation.

Downloads run in parallel using the collection download concurrency setting.
Set `download_only:true` to retain archives without replacing mods or clearing
their update flags. Installations run sequentially with the version-change
transaction above. Folder names, order, enabled/locked state, source metadata,
collection ownership, and individual plugin states follow that workflow.
The default installer policy rejects archives requiring choices; inspect the
reported archive and retry with explicit choices or `defaults` when appropriate.

Success returns `result.quick_update` with `downloaded`, `updated`, `skipped`,
`failed`, `remaining`, and `counts`. Each successful update includes its complete
version-change result. `quick_update_incomplete` returns the same summary under
`error.details.quick_update` when any target fails. Earlier successes and
acquired archives are retained; rollback applies to the failing mod. A persisted
installation/refresh failure or incomplete rollback stops later replacements.

Pause/resume applies during acquisition and between replacements. A current
replacement completes its atomic operation before pausing or cancelling.
Cancellation after acquisition starts reports the same partial summary under
`error.details.quick_update`; cancellation while replanning can stop before
that summary exists. The planning job itself is not cancellable.

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

Amethyst converts its stored plugin `before` rules into reciprocal LOOT
`after` rules in the worker's temporary input. The selected userlist is not
rewritten. Conditions and other file metadata remain attached to the converted
rule. Regex plugin owners cannot supply `before` rules. Shared userlist edits
preserve metadata such as tags, messages, group descriptions, and conditions
on retained rules; YAML comments and formatting are not preserved.

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

### LOOT user rules and groups

Read the selected profile's effective user metadata:

```json
{"jsonrpc":"2.0","id":"userlist","method":"plugins.userlist.get","params":{"game":"skyrim_se","profile":"default"}}
```

`result.userlist` reports `exists`, the profile-local `path`, `source`
(`profile`, `game`, or null), `source_path`, a `revision`, plugin
rules/assignments, group definitions/descriptions, and `diagnostics`.
Plugin names are matched without case; group names are case-sensitive.
The `default` group exists implicitly even when no definition is stored.
Rule target arrays list each name once. `file_metadata` identifies retained
structured rules, including their conditions, and `has_other_metadata`
identifies plugin entries carrying fields beyond ordering and group assignment.

Every edit requires the current `revision` and an `action`:

```json
{"jsonrpc":"2.0","id":"rule-edit","method":"plugins.userlist.edit.start","params":{"game":"skyrim_se","profile":"default","revision":"<revision from plugins.userlist.get>","action":"set_rules","plugin":"Example.esp","after":["Dependency.esp"],"before":["Patch.esp"]}}
```

| Action | Additional parameters | Behavior |
| --- | --- | --- |
| `set_rules` | `plugin`, `after` array, `before` array | Replace both rule lists, preserving other plugin metadata and conditions on retained targets. |
| `set_group` | `plugins` array, `group` | Assign the selected plugins, preserving rules and other metadata. |
| `remove_plugins` | `plugins` array | Remove the selected plugins' entire user metadata entries. Rules owned by other plugins remain. |
| `save_group` | `group`, `after` array; optional `description` | Create or update a group definition and its ordering. Other fields remain; an omitted description is preserved. |
| `remove_group` | `group` | Remove the user definition and user-group ordering references. Assigned plugins return to `default`; entries retain rules and other metadata, while bare assignment-only entries are removed. |
| `flip_rule` | `plugin`, `field` (`before` or `after`), `target` | Move the existing rule to the opposite direction, preserving its file metadata. |
| `clear` | `confirm:true` | Clear all user metadata and write an explicit empty profile userlist, suppressing the game-wide fallback. |
| `reset` | `confirm:true` | Remove the profile userlist and return to the game-wide fallback, if one exists. |

All name arrays accept at most 4096 entries. Rule arrays and group `after`
arrays may be empty; plugin-selection arrays must be nonempty. Duplicate
names are rejected. Plugin filenames must use the selected handler's plugin
extensions and cannot be regex patterns or paths. Referenced plugins need
not currently be installed. Group names have a 255-character limit and group
descriptions a 4096-character limit. Self-ordering rules and removing
`default` are rejected. New rules are unconditional; retained structured
rules keep their existing conditions and file metadata.

Edits affect only the selected profile's `userlist.yaml`, including when
that profile is a Profile Group. Member-profile userlists, the game-wide
fallback, plugin enablement, load order, and mod order are not rewritten.
A changed edit with no profile userlist starts from a copy of the effective
game-wide userlist, so existing user metadata is preserved.
`remove_group` affects user metadata; it does not remove masterlist groups.

The job result includes `changed`, `action`, and the updated `userlist`
with a fresh revision. Matching requests can finish without rewriting the
file. Revisions cover profile identity, relevant configured paths, and both
profile and fallback bytes. Edits check the revision again before committing.
A stale request returns `stale_plugin_userlist` without overwriting the
newer data. Writes are atomic and verified; failures attempt to restore the
exact previous bytes. Successful recovery reports
`plugin_userlist_write_failed` with `rolled_back:true`; incomplete recovery
reports `plugin_userlist_rollback_failed` with `state_persisted:true`.

Diagnostics report plugin cycles, their components and rule reasons, group
cycles even without assigned plugins, and group names not defined in the
userlist. A missing user definition may be supplied by the masterlist.
Diagnostics analyze user metadata only: plugin masters, masterlist rules,
and runtime conditions are not evaluated. Conditional edges are included
conservatively; saving may leave a reported cycle for subsequent edits.
Run `plugins.sort.start` to evaluate the complete LOOT inputs and apply
an order. `plugins.list.start` reads the saved userlist flags immediately.

Editing requires a configured handler with `features.plugin_userlist`;
the native LOOT binding is required for sorting, but not for editing.
These jobs use the shared operation lock, reject mutations during a managed
launch, and do not support cancellation or pause. Invalid/ambiguous YAML is
`userlist_invalid`; symlinks are `unsafe_userlist_path`; missing edits are
`loot_group_not_found` or `plugin_rule_not_found`. Userlist files must be
regular files of at most 4 MiB.

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

## Inventory snapshots during jobs

`games.list`, `games.get`, `profiles.list`, `mods.list`, and `downloads.list`
cache successful reads by method and parameters. During a background job, a
repeat request can return that immutable cached inventory instead of failing
with `operation_in_progress`. Responses include `snapshot.cached`,
`snapshot.captured_at`, `snapshot.age_seconds`, and `snapshot.active_job_id`.
Pass `allow_cached:false` to require a fresh read. A request with no cached
inventory still requires an idle service. The cache is bounded to 64 entries
and 32 MiB and is not persisted. Cached revisions may be stale; mutation methods
continue to validate their expected state before writing.

## Jobs

Accepted, running, and final job snapshots are persisted under the current
Amethyst configuration directory's `headless/jobs` folder. Each snapshot has
`history_persisted`; storage failures are also exposed as `storage_error` by
the history inventory and handshake. The service continues operating if a
snapshot cannot be saved, so clients requiring durability must check this flag.

- `jobs.history.get` takes `job_id` and retrieves the persisted snapshot.
- `jobs.history.list` takes optional canonical game ID `game`, `offset` (default
  0), and `limit` (default 100, maximum 500).
- `jobs.recovery.list` uses the same parameters and filters failed, cancelled,
  and interrupted jobs, with `recovery_methods` suggesting inventory and
  recovery APIs. It never retries a mutation automatically.

History records include a `job`, `owner_active`, and `owner_session`. If the
owning process died while a job was queued, running, or waiting for input, its history state is
`interrupted` with error `outcome_unknown`: changes may have completed before
the process could record success. Inspect current state before deciding what
to retry. Jobs in another live process remain queued/running/waiting and cannot be
controlled through this service's `jobs.cancel` or `jobs.events`.

The newest 500 records are retained; older live jobs are not pruned. Retained
event logs can be read with `jobs.history.events` as described above. Known
credential fields and URL query strings are redacted.
Records exceeding 16 MiB omit their result and set `result_omitted:true`.
`AMETHYST_JOB_HISTORY_DIR` overrides the storage folder for isolated clients or
fixtures. Normal `jobs.get`, `jobs.list`, and `jobs.events` retain their existing
in-process semantics.

Job-producing calls return immediately with a snapshot. They do not hold the
JSON-RPC request open for filesystem or network work. Only one queued or
running job may be outstanding in a headless process; another job-producing
request returns `operation_in_progress` until it reaches a final state. The
OAuth browser flow is separate from this queue.

### Plugin repository workflows

Provider declarations and callback schemas are documented in
[Manager plugins](manager-plugins.md#repository-callbacks). `mods.list` reports
the installed public source under `sources.plugin`, including update state when
available. Generic version source IDs have the form
`plugin:<plugin-id>/<provider-id>`.

`mods.versions.get.start` accepts these source IDs and returns provider versions,
the installed-property `revision`, and `catalogue_revision`.
`mods.version.change.start` requires both revisions, `version` as a provider's
string version ID, and `confirm:true`; it uses the normal recoverable replacement
path. Missing dependencies reject replacement before installing. Repository
catalogues, actions and dependency references are public JSON; credentials belong
in provider-owned authentication storage and private transfer fields.

`mods.check_updates.start` includes plugin repositories by default and accepts
`sources:["plugins"]`. Its result includes `plugin_updates` and source errors.
`mods.quick_update.plan.start` and `mods.quick_update.start` accept
`source:"plugins"`; plans can cover multiple providers. Execution checks its
revision, preserves individual mod rollback, and reports partial results if a later
item fails. `download_only` and per-mod installer options are supported. A missing
or disabled plugin leaves existing installs intact and makes its network operations
unavailable.

Dependency requests are `{plugin_id, provider_id, source}` objects.
`manager_plugins.dependencies.get.start` returns `items` in dependency-first order
and `revision`. It rejects dependency cycles, graphs over 256 artifacts or depth 32,
and an explicitly supplied stale revision. Provider download batches accept the
same single-request parameters as the existing methods without per-item `game`.
The download batch is pausable; both batches use the ordinary partial-result
contract below.

Headless itself does not display approval dialogs. Script plugins hosted by the
desktop can use `host.apply_batch` to approve a fixed workflow once; see
[reviewed workflows](manager-plugins.md#reviewed-workflows-and-bulk-downloads).
Custom Qt panels retain ownership of their approval UI.

Amethyst collection manifests and share codes support pinned `source.type:"plugin"`
entries with `plugin_id`, `provider_id`, public source URI `source`, `identity`,
`sha256`, and positive integer `fileSize`. Every provider dependency needs its own
pinned entry. Enabled plugins resolve and acquire these archives through the shared
collection pipeline. Python game-handler declarations and custom website protocol
registration are described in the plugin manual; normal game discovery includes
enabled plugin handlers.

### Batch workflows

The batch methods are also listed in `system.handshake.capabilities.batching`.
Existing single-item methods remain supported. All methods use the normal
backend validation, path guards and provider authentication.

Independent batches accept 1–256 `items`, a common `game`, and `profile` when
the corresponding single-item method requires it. An item supplies the other
parameters of that method, including its individual revisions, confirmations,
installer choices or NXM grant. Items cannot override the common game/profile.
All item parameter objects are validated before the parent job is submitted.
Runtime checks still occur immediately before each operation.

```json
{"jsonrpc":"2.0","id":"install-many","method":"downloads.install.batch.start","params":{"game":"skyrim_se","profile":"Experiment","items":[{"archive_id":"<first ID>","installer_policy":"reject"},{"archive_id":"<second ID>","name":"Optional patch","installer_policy":"defaults"}],"on_error":"stop"}}
```

Items execute sequentially in their requested order. Archive inspection,
installation and reinstall share the downloaded-archive inventory. Repeated
read selections and provider identities reuse results within that batch only;
installed-version rows retain their own installed file/version and ownership.
Duplicate explicit mutation targets are rejected; other destination collisions
are checked again when each item runs. Save selections cannot
include both a directory and one of its children; transfers cannot overwrite
each other's same-named destination entries within a batch.

`on_error` defaults to `stop`: after an item fails, later items remain unrun.
`continue` attempts every item unless recovery of a failed mutation itself
fails. Successful jobs return `total`, `completed`, `cancelled`, `failed`,
indexed `items` with `state` and `result`, and `remaining` indices.
A failure sets the parent job to `failed` with the same report in
`error.details.batch`; a cancellation sets it to `cancelled` and retains the
report there. Individual errors retain their own names, messages and details.
A batch is not an all-or-nothing transaction: completed installs, transfers,
settings changes and removals remain completed when a later item fails.

Cancellation stops between items and also cancels the active item when its
single-item implementation supports cancellation. An active non-cancellable
install or mutation finishes before the batch stops. Interrupted-download
resume batches additionally support pause/resume, including the active
transfer. Use ordinary `jobs.get` and `jobs.events` for the parent job.

### Batch conflicts and layout editing

`mods.conflicts.overview.start` accepts an optional selection of exact mod
names; omitting `mods` inspects the whole profile. It resolves Filegraph once,
then applies the same routed-path, archive and blacklist filtering as individual
conflict inspection. The result contains paged `mods` with counts and
`overrides`/`overridden_by`, plus `relationships` with `winner`, `loser` and
`file_count`. Relationships touching a returned mod can reference a mod outside
the selection and may repeat across pages. `offset`/`limit` page the mod list,
not the relationships. The order is highest priority first for the default
whole-profile selection; an explicit selection retains the requested order.

For file details, `mods.conflicts.inspect.batch.start` returns `items` in the
same shape as single-mod inspection, sharing one snapshot across all selected
mods. Here `section`, `offset` and `limit` apply to each mod's file-detail page.
Both methods accept the returned `revision` on subsequent requests and reject
stale generations. Both are cancellable. A selection contains 1–4096 unique
names when supplied; limits default to 500 and can be at most 5000.

A layout request supplies every real mod exactly once in its desired
highest-priority-first order. String entries refer to existing mods or
separators. Separator objects create or update separators using display names;
`from` names an existing internal separator when renaming it. Omitted existing
separators are removed. `settings` supports the same `collapsed`, `locked`,
`color` and `deploy_path` fields as individual separator settings.

```json
{"jsonrpc":"2.0","id":"layout-plan","method":"mods.layout.plan.start","params":{"game":"skyrim_se","profile":"Experiment","entries":[{"separator":"Expansions"},"Expansion A","Expansion B",{"separator":"Patches","settings":{"color":"#8844cc"}},"Expansion Patch"],"preserve_conflicts":true}}
```

The plan returns the final entry names, separator state, moved mods and a
`plan_revision`. Apply using `mods.layout.apply.start` with the identical
request, that revision, and `confirm:true`. Stale order, state or conflict
snapshots are rejected. A successful apply saves the complete layout and
reconciles Filegraph once. Save errors restore the prior mod list and profile
state when recovery succeeds; incomplete recovery or a later reconciliation
failure explicitly reports `state_persisted:true`.

`preserve_conflicts` defaults to true. It rejects reversals in the relative
order of currently conflicting mods, and changes to any mod's effective
separator deployment routing. It preserves current ordering constraints;
it does not execute the game or simulate mod-specific configuration merges.
Use false only when those changes are intended. Locked mods and separators
remain protected. Existing mod groups must remain contiguous with their real
leader first. Supply an optional complete `groups` object, using the same
member/title/collapse fields as `mods.groups.get`, to change grouping in the
same layout transaction. Group headers are implicit and are not listed in
`entries`. Layout plans allow at most 16384 entries.

### Batch mod properties

`mods.properties.get.batch.start` takes optional `mods`; omission reads all
real mods. Its result has an `items` array of exact `mod` names, `state`, and
`properties` or `error`. A missing/unavailable mod is an individual read error;
check both the overall `ok` flag and item states. Every successful row includes
the normal per-mod property revision and ownership information.

Pass up to 256 `{mod, revision, changes}` objects to
`mods.properties.set.batch.start`. It reads the ownership inventory once,
locks the selected metadata, and validates every revision before writing any
of them. Conflicting edits through aliases of the same source mod are rejected.
Metadata save failures roll back already-written metadata and report whether
recovery succeeded. Root-folder changes reconcile each affected profile once.
A reconciliation failure reports persisted metadata and the per-profile
failures. The existing `version_label`, `root_folder` and `ignore_updates`
property fields and source restrictions apply.

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

For a full copy of a profile with private mods, prefer the shared desktop copy workflow:

```json
{"jsonrpc":"2.0","id":"copy-profile","method":"profiles.copy.start","params":{"game":"skyrim_se","profile":"Handheld","name":"Handheld Copy"}}
```

This job checks free space, reports byte progress, supports `jobs.cancel`, rebases
stored paths, and excludes deployment recovery records and generated indexes.
It publishes the destination only after copying succeeds. Profile groups and
shared-staging profiles are not accepted; use `profiles.create.start` for those
shared-staging copies. Neither copy method changes the selected profile.

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

### Profile share codes

Create a code for a selected profile without changing the active profile:

```json
{"jsonrpc":"2.0","id":1,"method":"profiles.share.get.start","params":{"game":"skyrim_se","profile":"Handheld"}}
```

The cancellable `profiles.share` job returns `code`, the decoded `manifest`,
`profile`, an input `revision`, `sources` counts, `mod_count`,
`known_download_bytes`, `requires_nexus`, and `skipped`. Creation reads saved
metadata and installer choices; it does not contact a download provider.
Shared-pool and private profiles are supported. A Profile Group must already
be materialized, and its member-owned mod metadata and installer choices are
resolved from the real owner. The selected profile's choices take precedence
over member-local choices, followed by globally saved choices.

The code carries Nexus mod/file IDs or complete Thunderstore package pins,
per-mod enabled state, optional/root flags, installer choices, mod order, and
separator colors/locks. Cosmetic group headers are omitted. Mods without a
usable download pin appear in `skipped` with `reason:"no_download_pin"`.
An incomplete Thunderstore mirror falls back to its valid Nexus pin.
If no mods can be exported, the job returns `profile_share_empty` with the
skipped rows. Nexus file IDs must be distinct, as required by the importer.

Inspect a code before choosing import options, then import it into a new
private profile using the existing collection workflow:

```json
{"jsonrpc":"2.0","id":2,"method":"profiles.share.inspect","params":{"code":"<AMMCODE1 code>"}}
{"jsonrpc":"2.0","id":3,"method":"collections.import.start","params":{"game":"skyrim_se","profile":"Imported Handheld","code":"<AMMCODE1 code>","manual_interactive":true}}
```

Inspection returns `manifest`, `sources`, `mod_count`, `known_download_bytes`,
and `requires_nexus`. It accepts surrounding whitespace, line breaks, and an
omitted `AMMCODE1:` prefix. Invalid codes return `share_code_invalid`.
Code text is limited to 8 MiB and decompressed manifests to 16 MiB; truncated
or trailing compressed data is rejected. Local files and off-site download
instructions cannot be carried by a share code.

Code imports use the same account checks, optional skips, game-version
checks, manual archive/browser handoff, progress, cancellation, and recovery
records as local profile imports. Automatic Nexus acquisition requires
Premium; Thunderstore-only codes do not require Nexus authentication.
Cancelled or incomplete code imports retain their partial profile and manifest
for diagnosis/removal. They do not advertise Nexus collection resume/update;
remove the partial profile before importing again under the same name.
Exactly one of `path` or `code` must be supplied. Links cannot be pasted as
codes; public share-link upload/fetch remains outside this workflow. Use the
profile archive methods below to include local files and runtime state.

Creation holds the shared operation lease and checks input metadata, choices,
profile routing, and configuration again before returning. A changed input
returns `stale_profile_share`; no code is returned from that job. Symlinked
inputs are rejected. Cancellation stops between metadata reads or after code
generation. Codes carry neither bundled files, local edits, saves, overwrite
files, plugin order, nor LOOT user rules.

### Export a profile archive

Preview the selected profile's export configuration before writing an archive:

```json
{"jsonrpc":"2.0","id":1,"method":"profiles.export.get.start","params":{"game":"skyrim_se","profile":"Handheld","settings":"defaults","changes":[{"mod":"Local Fixes","source":"bundle"},{"mod":"Unused Mod","source":"ignore"}]}}
```

The cancellable `profiles.export.inspect` job returns `profile`, `revision`,
normalized `options`, selected `settings`, `settings_files`, per-mod `rows`,
`manifest`, `blockers`, `can_export`, `missing_archives`, `warnings`,
`requires_patch_warning_acknowledgement`, `bundled_mods`,
`bundled_downloadable_mods`, `member_count`, and `packing_bytes`. The byte count
is the uncompressed input size, including manifest and patches. Preview reads
local state and retained archives; it does not contact download providers.
Patch previews can be expensive because they calculate the actual binary diffs.

All three methods accept the same optional configuration:

- `settings`: `latest` (default), `defaults`, or a saved JSON basename.
  `latest` applies the newest file under `<profile>/workshop/`, if any.
- `changes`: at most 4096 unique per-mod objects, totaling at most 8 MiB of JSON.
  Each object requires `mod` and can set the fields below. Explicit changes
  are applied after saved settings.
- `include_saves`, `include_overwrite`, `include_root_folder`: booleans,
  defaulting to true. These select the profile's saves and its effective
  overwrite/runtime roots.

| Change field | Meaning |
| --- | --- |
| `source` | `nexus`, `thunderstore`, `plugin`, `direct`, `browse`, `manual`, `bundle`, or `ignore`. |
| `mod_id`, `file_id`, `version`, `size_bytes`, `game_domain` | Author a Nexus pin and its metadata. Numeric fields are non-negative integers. Changing `file_id` clears the old size unless a new size is supplied. |
| `ts_version`, `ts_size_bytes` | Select a version of the installed Thunderstore package. Changing the version clears its old size. Package namespace/name come from installed metadata. |
| `optional`, `phase`, `update_policy` | Optional mod flag, non-negative install phase, and Nexus policy `exact`, `prefer`, or `latest`. |
| `fomod_export` | Include saved FOMOD/BAIN choices in the manifest. Setting true requires saved choices. |
| `save_edits` | Compare installed files against a retained original archive and include representable binary patches. |
| `direct_url`, `instructions` | HTTP(S) download URL and browser/manual acquisition instructions. `direct` requires a URL. |
| `archive_id` | A retained archive ID from `downloads.list`, used to resolve ambiguous originals for patches. |

Installed Nexus/Thunderstore pins provide initial download sources. Unpinned
local mods and incomplete Thunderstore pins default to bundles. `ignore`
omits a mod from both the manifest and exported modlist. Invalid provider pins,
duplicate Nexus file IDs, unavailable mod folders, ambiguous patch archives,
and invalid source URLs appear in `blockers`. An empty export is blocked.
`bundled_downloadable_mods` identifies bundle rows that also have Nexus pins,
allowing clients to present the desktop redistribution notice.

Write the archive using the same preview options and the returned revision:

```json
{"jsonrpc":"2.0","id":2,"method":"profiles.export.start","params":{"game":"skyrim_se","profile":"Handheld","settings":"defaults","changes":[{"mod":"Local Fixes","source":"bundle"},{"mod":"Unused Mod","source":"ignore"}],"revision":"<revision from preview>","path":"/home/deck/Exports/Handheld.amethyst"}}
```

The cancellable `profiles.export` job returns `profile`, `archive_path`,
`archive_bytes`, `packing_bytes`, `mod_count`, `manifest`, `warnings`, and
`revision`. Existing output requires `overwrite:true`. Output must be outside
the manager's profile/mod/overwrite/runtime source folders, and its path
cannot cross symbolic links. A sibling temporary archive is streamed and
published only after input/output checks succeed. Cancellation, failure, or
changed inputs leave the prior output intact.

Archives carry bundled files (including empty mods), binary patches, installer
choices, mod order/enabled/root flags, separator state, profile state, plugin
order, profile-local LOOT rules, INIs, installed-collection records, and the
selected saves/overwrite/runtime files. Cosmetic group headers are omitted.
Shared-pool and private profiles are supported. A materialized Profile Group
resolves real member-owned folders and choices; the exported profile becomes
a standalone private profile on import. The selected profile's choices take
precedence over member-local choices, followed by globally saved choices.
Legacy profile state is read without migrating source files. Export does not
change the manager's active profile.

Patch originals are matched by the full Nexus domain/mod/file pin or
Thunderstore package/version pin. Off-site rows use the recorded installation
filename or an explicitly selected archive ID. Multiple matching originals
require `archive_id`; a mismatched provider archive is a blocker. Originals
must remain in the download inventory. Missing originals, added/deleted files,
and edits the patch writer cannot represent appear in `warnings`. Export
requires `allow_patch_warnings:true` to acknowledge those omissions; changing
the row to a bundle includes its files verbatim. Cancellation checks occur
between patch files and archive chunks; one native extraction or binary diff
must finish before cancellation can take effect.

Save per-mod settings with the same options and revision:

```json
{"jsonrpc":"2.0","id":3,"method":"profiles.export.settings.save.start","params":{"game":"skyrim_se","profile":"Handheld","settings":"defaults","changes":[{"mod":"Local Fixes","source":"bundle"}],"revision":"<revision from matching preview>","name":"portable.json"}}
```

The cancellable `profiles.export.settings.save` job returns `profile`,
`settings`, absolute `path`, and the number of `rows`. Omit `name` to generate
a unique timestamped JSON filename. Existing files require `overwrite:true`.
Unfinished configurations with blockers can be saved. Settings are compatible
with the desktop exporter and preserve source, optional/phase/policy flags,
installer-choice toggles, edit requests, archive selections, and version pins.
Installed Nexus IDs and domains remain authoritative when reloading saved
settings; explicit `changes` can override them for a particular export.
Saved Thunderstore versions are deliberate pins and take precedence.
Inclusion flags are request options and are not stored in per-mod settings.

These jobs use the shared game operation lease and reject a managed running
game. Export and settings writes require a matching preview revision; changed
inputs or options return `stale_profile_export`. Inputs are also checked
before publication. Metadata/settings files are limited to 16 MiB each;
symlinked or non-regular inputs are rejected. Jobs are cancellable, not
pausable, and cannot resume after restarting the process. Import a completed
archive through `collections.import.start` with `path` and a new profile name.

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

### Create an empty mod

Read `mods.list` or `mods.groups.get` before creating a mod:

```json
{"jsonrpc":"2.0","id":"new-mod","method":"mods.create.start","params":{"game":"skyrim_se","profile":"default","name":"Generated Output","revision":"<group_revision from mods.list>","position":{"after":"SkyUI"}}}
```

Use the group revision, which guards both order and grouping. `stale_mod_groups`
requires a fresh inventory. `position` defaults to `"top"` and accepts `"bottom"`
or one `before`/`after` anchor. Anchors can be real mods, separators, or cosmetic
group headers. An anchor inside a mod group places the new mod before or after
the entire group, preserving the group's membership and contiguity. Positions
follow natural priority order, independent of desktop sorting or reversal.

The new folder contains only `meta.ini`, with version `1.0` and an installed
timestamp. Mods start enabled and unlocked; `enabled:false` and
`root_folder:true` are optional. New names must already be safe Wine folder
names. Reserved boundary/group/separator names and case-insensitive collisions
with listed names, group titles, files, symlinks, or unlisted staging folders
are rejected. Existing metadata is never replaced by this operation.

The selected profile determines shared or private staging. Shared creation
adds the entry to the selected profile and checks other profiles using that
staging for name collisions. Independent private copies are unaffected.
Creation in a private member refreshes its dependent Profile Groups; creation
directly in a Profile Group creates a group-local mod. Empty profiles are
supported. No Nexus login is required.

Poll the job for `result.mod`, `scope`, `affected_profiles`, `synced_profiles`,
the resulting mod-list `revision`, and `group_revision`. The native catalogue
is refreshed so the new mod is available to subsequent manager operations.
Creation/save failures remove the new folder and restore the previous mod list;
`rolled_back:true` confirms recovery. `mod_creation_rollback_failed` reports
incomplete recovery, and `mod_creation_sync_failed` reports a persisted creation
whose catalogue or dependent profiles could not refresh. Both include
`state_persisted:true`. Creation jobs are not cancellable.

### Nexus endorsement and tracking

Read the live account state before changing selected mods:

```json
{"jsonrpc":"2.0","id":"nexus-state","method":"mods.nexus.get.start","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","@group:example"]}}
```

Poll for `result.nexus_actions`. It includes the validated account `user_id`,
`targets`, `skipped`, `failed`, `counts`, and `revision`. Mod groups expand to
their real members in natural order. Each target identifies one domain-qualified
Nexus mod, its selected installed versions/owners, live `endorsement`
(`endorsed`, `abstained`, or `undecided`), and live `tracked` state. Owners also
include `cached_endorsed`, their revision, and affected profile aliases. Local
mods without a Nexus identity are skipped. Nexus login is required; Premium is
not required. Provider response shapes follow the
[official Nexus client types](https://github.com/Nexus-Mods/node-nexus-api/blob/master/src/types.ts).

Apply one action using the same selection and revision:

```json
{"jsonrpc":"2.0","id":"nexus-endorse","method":"mods.nexus.action.start","params":{"game":"skyrim_se","profile":"default","mods":["SkyUI","@group:example"],"revision":"<revision from Nexus state job>","action":"endorse","confirm":true}}
```

`action` accepts `endorse`, `abstain`, `track`, or `untrack`. The job rereads
account identity, live state, selected owners, metadata, and groups under the
game operation lock. `stale_nexus_actions` includes a new inventory and stops
before account changes. Multiple selected parts/versions of the same Nexus
mod produce one account request, using the first selected version for an
endorsement/abstention. Already-matching account states avoid duplicate writes.
Nexus can reject endorsements because of its download/version/time requirements;
those failures remain visible in the per-target report.

Every target is checked against fresh account lists before success is reported.
An API acknowledgement alone does not establish the requested state;
`nexus_action_unverified` reports a mismatch. Endorse/abstain then save only the
selected physical owners' endorsement flags, rechecking ownership and metadata
before writing. Shared staging and Profile Group aliases see that same metadata;
independent private copies outside the selection retain their cached flags.
Other metadata keys and provider sections are preserved. Tracking has no local
metadata flag and is read from Nexus each time. `mods.list.sources.nexus.endorsed`
exposes the cached endorsement flag without requiring a login.

Success returns `result.nexus_actions` with `completed`, `skipped`, `failed`,
`remaining`, and `counts`. Completed rows include live state, whether the account
was already in that state (`unchanged`), and metadata outcomes. On partial
failure, `nexus_actions_incomplete` returns the same summary in
`error.details.nexus_actions`. Failed rows identify the `phase`,
`request_attempted`, and `remote_verified`; `remote_may_have_changed` remains true
when an attempted request could have changed the account, including ambiguous
network failures. `state_persisted` also accounts for successful local writes
and incomplete metadata recovery.

Remote account changes are retained. A failed metadata edit restores that
owner's previous bytes and reports `metadata_restored:true`; incomplete recovery
reports `nexus_action_metadata_rollback_failed`. Earlier completed targets and
saved owner flags remain visible in the summary. Authentication, rate-limit,
malformed account-state, and incomplete recovery failures stop later targets.
Cancellation finishes the current target's request, verification, and cache
updates, then reports completed and remaining targets under
`error.details.nexus_actions`. These jobs can be cancelled between targets and
cannot be paused.

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
- `ignore_updates`: a non-empty map of `nexus`, `thunderstore` and/or `plugin` to booleans.
  The requested source must have a saved identity. Nexus captures the currently
  known latest version when muted; its update checker can flag a newer release
  again. Thunderstore and plugin sources mute future update flags. Setting false clears both the
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

### Plugin-owned metadata

`manager_plugins.metadata.get` reads JSON annotations under one manager plugin's
namespace. Required parameters are `game`, `profile`, and `plugin_id`. Optional
`mods` is an array of up to 4,096 ordinary mod names; omit it for all mods or
use `[]` for profile metadata only.

```json
{"jsonrpc":"2.0","id":"metadata","method":"manager_plugins.metadata.get","params":{"game":"skyrim_se","profile":"default","plugin_id":"example.inspector","mods":["Example mod"]}}
```

The result contains target `game`/`profile`, `plugin_id`, a `revision`,
profile-level `metadata` (record or null), and `mods` mapping names to records
or null. Records contain an object `value` and boolean `portable` (default
false). The revision covers the namespace and real mod inventory, including
records outside a filtered read.

```json
{"jsonrpc":"2.0","id":"save-metadata","method":"manager_plugins.metadata.set.start","params":{"game":"skyrim_se","profile":"default","plugin_id":"example.inspector","revision":"<revision from metadata get>","confirm":true,"changes":{"profile":{"value":{"last_audit":"passed"}},"mods":{"Example mod":{"value":{"reviewed":true},"portable":true}}}}}
```

The setter is a mutation job and requires an idle game, no managed launch, an
exact current revision, and `confirm:true`. Poll to completion. `changes` may
contain `profile` and/or a `mods` object. Records replace the corresponding
value; null removes it. Writes preserve other plugin namespaces and native
profile state. `stale_plugin_metadata` requires another read and review.

Values must be finite JSON with at most 16 nesting levels. Record JSON is
limited to 64 KiB, each namespace to 1 MiB, and total plugin metadata in a
profile to 8 MiB. Invalid state is left unchanged. Reads do not create state.
Metadata follows mod rename/copy/removal and profile copy/backup. Only records
marked portable travel through profile/collection exports; plugin-owned values
are not rewritten as native manager paths. See
[manager-plugins.md](manager-plugins.md#metadata-on-profiles-and-mods) for the
script helpers, callback contexts, and Profile Group behaviour.

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

Job states are `queued`, `running`, `waiting_for_input`, `succeeded`, `failed`,
and `cancelled`. Treat only the final three as completed. Historical snapshots
can also report `interrupted` after their owning process ends unexpectedly.

`waiting_for_input` means the job needs an installer answer, a manual collection
archive, or a browser download. The `waiting_for` object identifies its `type`
and, for API questions, the request ID and method used to read it. Answer through
`installers.requests.submit` or `collections.manual.submit`; browser handoffs
resume when the matching file arrives. `waiting_for` becomes null after the
wait ends. State changes are included in both live and retained events.

`requires_client` identifies jobs cancelled when their client disconnects,
including interactive installations before their first question. Independent
accepted operations still drain normally. The handshake advertises this contract
under `capabilities.input_waits`.
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

### Check and repair prefix health

Read the selected profile's saved prefix and its actual runtime components:

```json
{"jsonrpc":"2.0","id":"prefix-health","method":"wine.prefix.health.start","params":{"game":"skyrim_se","profile":"default"}}
```

The cancellable `wine.prefix.health` job returns `profile`, `scope:"prefix"`,
configured `prefix_path`, `normalized_prefix`, `check_proton`, `runner_path`,
`runner_error`, `checks`, `summary` counts for `ok`/`missing`/`warn`/`unknown`,
`repairable_checks`, `process_scan_available`, `prefix_pids`, and `revision`.
Each check contains stable `id`, `status`, canonical-English `label`, technical
`detail`, `fix_token`, `fixable`, and detector `evidence`.

`check_proton` defaults to true and resolves an already installed Wine/Proton
runner. Setting false omits the runner-binding/downgrade checks. Reports use
local files, do not download UMU or installers, do not change `user.reg`, and
do not migrate legacy profile state or change the active profile. A saved
missing prefix stays missing instead of falling back to another detected
Steam prefix. Compatdata parents normalize to their actual `pfx` directory.

The report uses the same detectors as the desktop Prefix Health overlay:
prefix existence/structure, runner binding, Steam first-launch state, game
INIs, handler-declared components, and Bethesda install-path registrations.
Component rows follow `auto_install_deps` and `prefix_health_extras`, including
VC++, DirectX components, .NET desktop/framework runtimes, and LAV Filters.
Live DLL/registry evidence takes precedence over stale dependency markers.
DXVK is informational and is never offered as an automatic repair.
Unavailable/uninitialized prefixes return missing/unknown rows without
offering runtime repairs. Malformed saved configuration is rejected.

Repair selected fixable checks or request the equivalent of desktop Fix All:

```json
{"jsonrpc":"2.0","id":"repair-quartz","method":"wine.prefix.repair.start","params":{"game":"skyrim_se","profile":"default","revision":"<revision from matching health report>","checks":["quartz"]}}
{"jsonrpc":"2.0","id":"repair-all","method":"wine.prefix.repair.start","params":{"game":"skyrim_se","profile":"default","revision":"<revision from matching health report>","all":true}}
```

Use the same `check_proton` option as the report. `checks` accepts at most 128
distinct nonempty IDs, each at most 255 characters. Exactly one of a nonempty
`checks` array or `all:true` is required. Every explicit target must be present
and fixable in the current report; unsupported, healthy, or informational rows
return `prefix_health_not_repairable` before any installer runs. `all:true`
with no fixable checks returns a successful no-op. Prefix/configuration/report
changes return `stale_prefix_health`; scan again before retrying.

The cancellable `wine.prefix.repair` job runs the shared desktop installers
sequentially against the selected prefix. It returns `profile`, `prefix_path`,
per-check `repairs`, counts `repaired`/`failed`/`already_healthy`, `no_op`, and
the refreshed `health` report. A repair records `id`, outcome `status`,
`attempted`, `installer_succeeded`, and resulting `check_status`; attempted
repairs also provide `detail` and an `error` when the installer raised.
Installer success requires a healthy post-install detector result. Failures
are reported per check and the remaining requested repairs continue.
Completed repairs remain applied when another check fails or is cancelled;
there is no prefix-wide rollback.

Repairs may download installers and run Wine/Proton/winetricks. DirectX and
LAV repairs retain the selected prefix rather than falling back to another
Steam app's prefix. A normalized prefix lease and the shared game mutation
lock serialize repairs with managed operations; desktop Proton-menu installers
use the same guard. A managed running game is rejected. Before installing,
active prefix processes return `wine_prefix_in_use`, and an unavailable process
scan returns `wine_prefix_scan_unavailable`. Missing/uninitialized prefixes
return `wine_prefix_unavailable`.

Both jobs are cancellable, not pausable or resumable after restart. Health
checks stop between detectors. Repair cancellation waits for the current
installer to finish, then stops before the next repair; cancellation errors
include completed `repairs`. Saved game/prefix choices are checked between
repairs, and a changed target stops the job with completed outcomes preserved.
Use another health report to inspect partial results and request remaining
repairs. Fixture checks cover dispatch and live file-based verification;
actual downloaded installer, desktop, packaged, and real-game behavior still
requires environment-specific validation.

### Optional Wine dependencies

List the shared desktop dependency choices for the selected profile's prefix:

```json
{"jsonrpc":"2.0","id":"dependencies","method":"wine.dependencies.list.start","params":{"game":"skyrim_se","profile":"default"}}
```

The cancellable `wine.dependencies.list` job returns `profile`, `scope:"prefix"`,
configured `prefix_path`, `normalized_prefix`, `initialized`, `dependencies`,
`process_scan_available`, `prefix_pids`, and `revision`. Each dependency has
`id`, `label`, `declared`, `status`, `installed`, `installable`, `verification`,
`detail`, and `evidence`. Choices include VC++ Redistributable, .NET Framework
4.8 and the shared desktop runtime versions, curated legacy DirectX components,
d3dcompiler_47, LAV Filters, and the combined XACT installers. The exact IDs
come from the report. `declared` identifies components requested by the game;
other choices are still available. DXVK and registry-path repair are excluded.

Reports read local files without downloading installers, running Wine, applying
overrides, migrating legacy profile state, or changing the selected profile.
Saved missing prefixes stay missing. Compatdata parents normalize to `pfx`.
Unavailable/uninitialized prefixes have unknown dependency state and
`installable:false`. Eligibility does not guarantee that a working Wine/Proton
runner is available for an installer.

```json
{"jsonrpc":"2.0","id":"install-compiler","method":"wine.dependencies.install.start","params":{"game":"skyrim_se","profile":"default","revision":"<revision from dependency report>","dependencies":["d3dcompiler_47"],"confirm":true}}
```

`dependencies` accepts 1-128 distinct nonempty IDs, each at most 255 characters.
Only installable choices from the report are accepted; arbitrary winetricks
verbs and executable paths are not exposed. Stale configuration, registry,
dependency-marker, or detector state returns `stale_wine_dependencies` before
installation. Request a fresh report and use its revision.

The cancellable `wine.dependencies.install` job runs installers sequentially,
retaining the selected prefix rather than falling back to another Steam app's
prefix. It uses the shared game/prefix locks and rejects missing or uninitialized
prefixes, managed running games, active prefix processes, and unavailable
process scans. Prefix usage and configuration are checked between installers.
Dependency installers may download packages, run Wine/Proton/winetricks, and
apply their required native DLL overrides.

The result contains `profile`, resolved `prefix_path`, `installations`, counts
`installed`/`failed`/`already_installed`, and a refreshed `report`. Each outcome
contains `id`, `status`, `attempted`, `installer_succeeded`, resulting `dependency`
state, and an `error` when the installer raised. Target-change failures may
return an attempted outcome before verification is possible. Existing healthy
components are skipped. A successful installer must also pass verification.
`verification:"live"` uses the shared file/registry detectors; for d3dcompiler_47,
this includes a native 32-bit DLL when `syswow64` exists and a native-first
override. `verification:"installer_marker"` means the component has no live
detector: XACT requires completion markers for both `xact` and `xact_x64`, which
do not prove the DLLs still exist or that audio works in a game.

Failures are reported per dependency and remaining dependencies continue.
Completed changes remain applied; there is no prefix-wide rollback. Cancellation
waits for the current dependency installer to finish, then stops before the next
dependency. Cancellation and job errors include completed/attempted
`installations`. Jobs cannot be paused or resumed after restart. List dependencies
again to inspect partial results. Fixture validation does not establish actual
downloaded-installer or real-prefix/game behavior.

### Register a Bethesda game path

```json
{"jsonrpc":"2.0","id":"registry","method":"wine.game_registry.inspect.start","params":{"game":"skyrim_se","profile":"default"}}
```

The cancellable `wine.game_registry.inspect` job returns a read-only report with
`revision`, `registry_name`, `game_path`, normalized `prefix_path`, `runner_path`,
`initialized`, `process_scan_available`, `prefix_pids`, `can_write`, and
`registration`. Games without a Bethesda registry name return
`game_registry_unavailable`. A missing or uninitialized prefix is reportable,
but cannot be written. Registry data includes `architecture`, `expected_value`,
`registered`, and rows with `view`, `key`, `value`, `required`, and `matches`.
Matching accepts equivalent mapped Wine paths, casing and trailing separators.

```json
{"jsonrpc":"2.0","id":"register","method":"wine.game_registry.write.start","params":{"game":"skyrim_se","profile":"default","revision":"<inspection revision>","confirm":true,"force":true}}
```

The cancellable write job shares the desktop Register Game Path execution path.
It writes `Installed Path` under `HKLM\Software\Bethesda Softworks\<Game>`
using explicit `/reg:64` and `/reg:32` switches in a 64-bit prefix; the 32-bit
view appears under `Wow6432Node`. A `win32` prefix requires only `/reg:32`.
`force:true` is the default and repeats the write even when both values and a
cache marker already match. With `force:false`, a verified registration returns
`no_op:true` without starting Wine or changing the marker. An old marker alone
never establishes success.

Writes require an initialized, idle prefix, a resolved installed Wine/Proton
runner and an unchanged inspection revision. The shared game, prefix and hive
locks cover command execution, process cleanup and verification. Managed game
launches, active prefix processes and unavailable process scans prevent writes.
Runner resolution retains the profile's saved prefix and does not download UMU
or change unrelated registry settings. Changed inputs return
`stale_game_registry`; inspect again before retrying.

Successful results include `ok`, `no_op`, `attempted_views`, `written_views`,
`state_persisted`, refreshed `registration`, and `needs_deployment:false`.
View IDs in attempted/written lists are `64` and `32`. Registry values are read
back after prefix process cleanup; successful command exit codes alone do not
establish success. A failure or cancellation may leave a completed view written;
its details identify attempted/written views and resulting registration. There
is no prefix-wide rollback. Cancellation stops the command process group and
cleans up the prefix before releasing the locks. Inspect again to see partial
results. These jobs cannot be paused or resumed after restart. Fixture checks
and offscreen desktop checks do not establish actual Wine/Proton behavior.

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
the profile's settings and uses the same native, Steam, Heroic, Lutris, Faugus, Bottles,
Proton, Flatpak-forwarding, and profile-VFS router as the desktop Play button.
It does not accept a command, executable path, environment, or arguments from
the RPC client. Saved launcher settings remain authoritative.

Installed [plugin launch hooks](manager-plugins.md#launch-hooks) also run for
this route. They can prepare the deployed profile before dispatch and verify
their output after the tracked launch ends. Game filters apply before plugin
code is imported. Preparation failures prevent launch; hook diagnostics appear
in launch logs. Stop can cancel preparation as well as a running game.

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
`launch.active.list` accepts an optional `game` and returns active launch records
shared with other manager processes. These records protect coordinator-backed
mutations after dispatch and after the launching service disconnects. Process
identity uses launch markers and PID start times, never executable names.
An unavailable process scan blocks mutation conservatively; a pending external
launcher has a startup grace period. Finished records are pruned when the next
coordinated operation acquires the game's locks. Other processes can inspect
these records but cannot adopt the original service's stop control or logs.

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
archive install/reinstall, Nexus downloads and Wabbajack gallery, package, preflight, and install jobs.
Use each job's `pausable` flag and the handshake's `pausable_jobs` list to
discover pause/resume support, including supported provider downloads, tool
acquisition, Quick Update, and interrupted-download resume jobs. Other jobs
return `job_not_pausable`.
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

Requests are limited to 8,388,608 UTF-8 bytes per line (including the newline),
128 levels of JSON nesting, and 256 requests per JSON-RPC batch. Oversized lines,
excessive nesting, non-JSON numeric constants, overflowing numbers such as
`1e9999`, and decoder limit errors return `parse_error`; oversized batches return
`invalid_request`. The next input line is still processed. Invalid backend
results return `internal_error` for that request without terminating the service.

## Remaining desktop parity

Core profile/mod/plugin management, archive installation, collections,
Wabbajack, deployment, restoration, and managed launch have public headless
methods. A large method count does not establish complete desktop parity.
The following feature families still need public workflows or wider validation:

- Additional provider features beyond Nexus, Thunderstore, and BG3 mod.io browsing and downloads.
- Public share-link transport and remaining profile settings.
- Desktop actions such as managing caches, syncing translations, editing theme
  palettes, and installing application updates. Global Settings preferences are
  available through `settings.get` and `settings.set.start`; these action buttons
  are separate workflows.
- Game-specific tool/setup wizards. Individual wizards need
  an audit of their parameters, shared execution code, and interaction requirements;
  the desktop tool catalogue contains many separate workflows.
- Consistent operation locking across desktop/headless mutations and the full
  game-running lifetime, plus cancellation and restart recovery where still absent.
- Packaged installations and broader real-game validation. Source fixtures and
  successful protocol calls do not prove Flatpak/AppImage or live-game behavior.

The built-in catalogue audit currently covers 67 game handlers and 304 tool
declarations across 56 wizard classes. Of these, 182 declarations across 16
classes advertise a headless workflow; 122 declarations across 40 classes still
lack one. Declarations repeat shared workflows across games, so these counts
are not an overall completion percentage. Larger remaining families include
NIF Viewer, Synthesis, xTranslator, Workshop and EET. Available
workflows still need the packaged and real-game validation described above.

These families vary substantially in size; the game-specific tools require the
broadest remaining audit. The supported methods and limitations below are the
current contract, rather than a claim that these families are complete.

### Scope for an initial release

The October 15, 2026 merge audit exposes 198 public methods. All 67 handlers
and all backend modules load with GUI imports blocked. Isolated filesystem
fixtures exercise configuration, ZIP installation, mod activation, profile copy,
linked-mod inventory/deployment/removal, Profile Group deployment, managed native
process launch, Restore,
Nexus previous-version history, and failed-replacement rollback. A mocked
Thunderstore catalogue/download fixture exercises `.r2x` import and disabled
states. ReShade extraction/cancellation and offscreen desktop imports also pass.
These checks do not establish packaged or live-game behaviour.

Nexus and Thunderstore version changes now save the desktop-compatible previous
version record inside the install transaction. Nexus Quick Update uses that same
replacement path. Headless rollback restores recorded versions from cached archives.
`mods.list` includes a `link` object with source path/profile, dependent profiles,
availability, and read-only status. Linked mods can be enabled, ordered, deployed,
and removed; creating new cross-profile links remains a desktop action.

The initial testing scope is profile/mod/plugin management, archive installation,
collections, Wabbajack, deployment, Restore, and managed launch, with advertised
optional tools. Before recommending a stable release, rebuild and exercise the
AppImage/Flatpak entry points and native components, then validate representative
real-game workflows, provider downloads, cancellation, and recovery. Concurrent
use with desktop tools or another manager process also needs wider validation;
the shared coordinator does not cover every desktop mutation.

Full wizard parity is a separate expansion. Synthesis pipeline execution,
Workshop, EET, xTranslator and graphical viewers can be deferred, as can broader
source browsing, share-link transport and remaining profile settings.
A remote daemon is unnecessary for the local stdio release.

## Current limitations

- Nexus collections can be installed into a new private profile and
  installed collections can be resumed, updated, appended, listed, reset,
  safely removed, and have their appended mods enabled or disabled. Automatic
  Nexus downloads require Premium; free-account installs use pre-supplied
  archives or interactive browser handoff. Installer questions can use
  `interactive:true` independently of browser handoff.
- `mods.check_updates.start` refreshes Nexus Mods, Thunderstore, and BG3 mod.io
  metadata when the respective credentials are available.
- Scripted FOMOD code cannot run in Amethyst, and unparseable FOMOD XML cannot
  supply choices. Headless clients may explicitly choose the same verbatim
  file-copy fallback as the desktop installer. Ordinary Fluffy multi-mod
  archives are selectable bundles; independent folder-per-mod archives can
  also be explicitly selected, renamed and installed as separate mods.
- Saved game, prefix, runtime, deploy-mode, staging, manual-save, and OpenMW
  AppImage paths can be changed through a revision-checked plan. Launcher
  identities have a separate revision-checked setter. Staging migration is not
  cancellable once it starts. Other Configure Game wizard options remain
  desktop-only unless exposed by `games.options.get`.
- Cross-profile mod copying and moving include Profile Group sources. Moving
  into a member saves an inspectable group exclusion; the group must be
  restored before the move.
- Progress delivery is polling-only. Full profile copying, Nexus mod actions, Nexus Quick Update, archive inspection, Nexus, Thunderstore, and mod.io downloads,
  Synthesis acquisition, inspection, installation and prefix setup, Wrye Bash acquisition, inspection, installation and runs, LOD generation, inspection and installation, BSA/BA2 scanning, planning, packing and unpacking, xEdit inspection, installation, run planning and execution, ReShade and SMAPI inspection and installation, 4GB patch inspection/application/backup restoration, Bethesda registry inspection and writing, optional Wine dependency reports and installation, collection download, installation, resume, update and append, and Wabbajack
  gallery, package, preflight, archive-import, and install jobs can be cancelled;
  archive installation/reinstallation can also be cancelled before profile commits;
  most other running jobs cannot. Nexus Quick Update, Nexus, Thunderstore, and mod.io downloads and interrupted-download
  resume jobs and Wrye Bash/LOD/xEdit/SMAPI acquisition can be paused and resumed during the same headless process.
  Saved Nexus partials can also be listed, resumed, or discarded after a restart.
- The shared operation lock also covers Wabbajack installation/removal/reset,
  collection group finalization, appended-mod activation and removal,
  collection load-order resets, game reconfiguration, profile conversion,
  desktop Proton-menu installers, Bethesda registry writes, ReShade installation, Script Extender archive installation, and
  manual MGE XE archive installation, Wrye Bash installation and runs, LOD/xEdit/SMAPI installation, LOD and xEdit runs and recovery, BSA/BA2 archive batches, and desktop/headless 4GB executable patch jobs.
  Other desktop mod/plugin-list edits, installers, wizards, configuration
  actions remain outside it. Do not run those tools while a headless mutation
  or managed game launch is active.
- The transport is a child process over standard I/O; there is no socket daemon
  or remote authentication layer yet.
- Python modules under `amethyst_backend`, `Utils`, `Games` and `Nexus` remain
  implementation details. External applications should use the JSON protocol.

The service has the same filesystem permissions as the user who starts it.
Only pass requests from trusted local applications.
