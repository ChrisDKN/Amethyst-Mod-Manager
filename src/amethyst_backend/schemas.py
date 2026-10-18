from __future__ import annotations

from copy import deepcopy

DIALECT = "https://json-schema.org/draft/2020-12/schema"

_CONTRACTS = {
    'boundaries.list.start': ('boundary game profile', 'limit offset', "Scan the profile's managed Overwrite or Root Folder inventory."),
    'boundaries.move.start': ('boundary confirm game paths profile revision target_mod', 'create_mod', 'Move captured entries into a compatible existing or new mod.'),
    'boundaries.remove.start': ('boundary confirm game paths profile revision', '', 'Permanently remove captured entries.'),
    'collections.append.start': ('confirm game profile slug', 'accept_game_version_mismatch domain manual_archives manual_interactive overwrite_existing revision skip_existing skipped_optional_file_ids', 'Append a Nexus collection to an existing private profile.'),
    'collections.download.start': ('game slug', 'domain revision', 'Download every Nexus archive and pinned Thunderstore package for a collection without changing a profile.'),
    'collections.group.start': ('collection_id game profile state_revision target', 'group_name ini_source overwrite_excluded', 'Add a complete standalone collection profile to a new or existing profile group.'),
    'collections.import.start': ('game profile', 'accept_game_version_mismatch code manual_archives manual_interactive path skipped_optional_file_ids', 'Import a local `.amethyst`, ZIP, JSON, `.r2x`/renamed `.txt` manifest, or share code into a new private profile.'),
    'collections.install.start': ('game profile slug', 'accept_game_version_mismatch domain manual_archives manual_interactive revision skipped_optional_file_ids', 'Install a Nexus collection into a new private profile.'),
    'collections.installed.list': ('game', 'profile', 'List installed standalone and appended collections.'),
    'collections.manual.get': ('job_id', '', 'Read the current interactive archive request for a collection job.'),
    'collections.manual.submit': ('job_id request_id', 'path skip', 'Supply an archive or skip an optional request.'),
    'collections.remove.plan.start': ('collection_id game profile state_revision', '', 'Preview the exact appended-collection mods that would be deleted or retained.'),
    'collections.remove.start': ('collection_id confirm game plan_revision profile state_revision', '', 'Remove an appended collection only if its record and deletion plan remain unchanged.'),
    'collections.reset_load_order.start': ('collection_id game profile state_revision', 'group', "Restore one installed collection's authored mod and plugin order in a profile or containing profile group."),
    'collections.resume.start': ('collection_id game profile state_revision', 'accept_game_version_mismatch manual_archives manual_interactive', 'Resume an incomplete standalone Nexus collection installation.'),
    'collections.set_enabled.start': ('collection_id enabled game profile state_revision', '', 'Enable or disable the exclusively owned mods of an appended collection.'),
    'collections.update.start': ('collection_id confirm game profile state_revision target_revision', 'accept_game_version_mismatch manual_archives manual_interactive skipped_optional_file_ids', 'Update a standalone Nexus collection while retaining the prior revision until verification succeeds.'),
    'deploy.start': ('game profile', 'options', 'Start a deployment job.'),
    'downloads.hidden.set.start': ('archive_ids game hidden profile revision', '', 'Hide or show selected archives in the desktop Downloads tab.'),
    'downloads.inspect.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'downloads.inspect.start': ('archive_id game profile', 'name', "Inspect an archive's installer choices and destination without installing it."),
    'downloads.install.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'downloads.install.start': ('archive_id game profile', 'bain_selected bundle_selected fomod_selections if_exists installer_policy name', 'Install one archive returned by `downloads.list`.'),
    'downloads.interrupted.discard.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'downloads.interrupted.discard.start': ('confirm download_id game profile revision', '', 'Delete one saved partial and its resume metadata.'),
    'downloads.interrupted.list': ('game profile', '', "List unlocked saved Nexus partials in enabled download locations for this game's domains."),
    'downloads.interrupted.resume.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'downloads.interrupted.resume.start': ('download_id game profile revision', 'nxm_url', 'Resume one exact saved transfer using Premium or a matching NXM grant.'),
    'downloads.list': ('game profile', '', 'List downloaded mod archives and configured locations.'),
    'downloads.locations.get': ('', '', 'Read global download scan settings and their revision.'),
    'downloads.locations.set.start': ('revision settings', '', 'Change enabled scan locations and extra folders.'),
    'downloads.move.plan.start': ('archive_ids destination_location_id game profile', '', 'Inspect a move between enabled download locations, including destination collisions.'),
    'downloads.move.start': ('archive_ids confirm destination_location_id game plan_revision profile', 'if_exists', 'Move selected archives with their Nexus identity sidecars.'),
    'downloads.remove.plan.start': ('archive_ids game profile', '', 'Inspect the exact archives and sidecars that removal would delete.'),
    'downloads.remove.start': ('archive_ids confirm game plan_revision profile', '', 'Permanently delete selected downloaded archives and their sidecars.'),
    'games.blacklist.get': ('game', '', 'Read built-in, custom, disabled, and effective per-game conflict exclusions with a revision.'),
    'games.blacklist.set.start': ('confirm game overrides revision', 'profile', 'Save filename and folder exclusions and optionally reconcile one profile.'),
    'games.configure.start': ('game game_path', 'appimage_path deploy_mode prefix_path runtime_mode save_path_override staging_path', 'Configure a game that has no saved paths and create its default profile.'),
    'games.get': ('game', '', 'Return one game and its current state.'),
    'games.installations.list.start': ('game', '', 'Scan Steam, Heroic, Lutris, Faugus, Bottles, and non-Steam shortcuts for candidate installations.'),
    'games.launcher.get': ('game profile', '', "Read the selected profile's saved launcher identities and revision."),
    'games.launcher.set.start': ('game profile revision source', 'launcher_id', 'Save one launcher identity and clear the alternatives for that profile. `source` is `steam`, `manual`, `heroic`, `lutris`, `faugus`, `bottles`, or `shortcut`.'),
    'games.list': ('', 'configured_only', 'List discovered game handlers.'),
    'games.options.get': ('game profile', '', 'List safe per-profile quick-configuration options and their current values.'),
    'games.options.set.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'games.options.set.start': ('expected_value game key profile value', '', 'Change one available option if its current value still matches.'),
    'games.prefix.detect.start': ('game game_path source', 'launcher_id', "Find the chosen install's Wine/Proton prefix without saving it. `source` is `steam`, `heroic`, `lutris`, `faugus`, `bottles`, or `shortcut`."),
    'games.reconfigure.plan': ('game profile', 'appimage_path deploy_mode game_path prefix_path runtime_mode save_path_override staging_path', "Preview a configured game's path changes and staging migration; `deploy_mode` accepts hardlink, symlink, copy, or `vfs` where supported."),
    'games.reconfigure.start': ('confirm game profile revision', 'appimage_path deploy_mode game_path migrate_staging prefix_path runtime_mode save_path_override staging_path', 'Apply the plan only while its source and destination state still match.'),
    'games.routing.get': ('game', '', 'Read ordered per-game custom routing rules and their revision.'),
    'games.routing.set.start': ('confirm game revision rules', 'profile', 'Save validated rule edits and optionally reconcile one profile.'),
    'games.version.get.start': ('game profile', '', "Read the selected installation's game executable version using the Configure menu's detector. Returns `version:null` when no version can be detected."),
    'jobs.cancel': ('job_id', '', 'Request cancellation of a job whose snapshot says `cancellable: true`.'),
    'jobs.events': ('job_id', 'after wait_ms', 'Read or long-poll ordered job events.'),
    'jobs.get': ('job_id', '', 'Get the latest job snapshot.'),
    'jobs.history.get': ('job_id', '', ''),
    'jobs.history.list': ('', 'game limit offset', ''),
    'jobs.list': ('', 'active_only', 'List jobs, newest first.'),
    'jobs.pause': ('job_id', '', 'Pause a job whose snapshot says `pausable: true`.'),
    'jobs.recovery.list': ('', 'game limit offset', ''),
    'jobs.resume': ('job_id', '', 'Resume a paused job.'),
    'launch.active.list': ('', 'game', ''),
    'launch.get': ('', 'launch_id', 'Inspect the latest managed game-launch lifecycle.'),
    'launch.settings.get': ('game profile', '', 'Read launch settings, executable key, scope, declared game toggles, and revision.'),
    'launch.settings.set.start': ('changes game profile revision', '', 'Save validated launch controls and selected-profile arguments while preserving unrelated settings.'),
    'launch.start': ('game profile', '', "Launch the currently deployed profile through Amethyst's configured router."),
    'launch.stop': ('confirm launch_id', '', 'Stop a process tree started by the managed launch.'),
    'modio.credentials.clear.start': ('confirm', '', 'Clear saved BG3 mod.io credentials.'),
    'modio.credentials.get': ('', '', 'Inspect saved BG3 mod.io credential status without returning the key.'),
    'modio.credentials.set.start': ('api_key api_path', '', 'Verify and save the BG3 mod.io connection.'),
    'modio.download.start': ('file_id game mod_id', '', 'Download an exact BG3 mod.io file with size/MD5 verification; pause and cancel are supported.'),
    'modio.mods.files.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'modio.mods.files.start': ('game mod_id', '', 'Read all published Windows files for a known BG3 mod, with its live-file recommendation.'),
    'mods.bundle.get': ('game mod profile', '', "Read an installed RE bundle's option groups and current selection."),
    'mods.bundle.set.start': ('bundle_selected game mod profile revision', '', 'Change the selected folders of an installed RE bundle.'),
    'mods.check_updates.start': ('game profile', 'mods sources', 'Check profile-scoped Nexus, Thunderstore, and BG3 mod.io updates.'),
    'mods.conflicts.inspect.batch.start': ('game profile', 'limit mods offset revision section', 'Read file-detail pages for multiple mods from one shared snapshot.'),
    'mods.conflicts.inspect.start': ('game mod profile', 'limit offset revision section', "Read a page of one mod's winning, losing, or uncontested files from Filegraph."),
    'mods.conflicts.overview.start': ('game profile', 'limit mods offset revision', 'Read paged per-mod conflict summaries and relationships from one shared snapshot.'),
    'mods.copy.plan': ('game mods profile target_profile', 'if_exists renames separator', 'Inspect a cross-profile copy, including group members and collisions.'),
    'mods.copy.start': ('confirm game mods plan_revision profile target_profile', 'if_exists renames separator', 'Copy selected mods and optional separator to another profile.'),
    'mods.create.start': ('game name profile revision', 'enabled position root_folder', 'Create an empty mod at version `1.0`, preserving anchored groups and refreshing the native catalogue and affected Profile Groups.'),
    'mods.groups.edit.start': ('action game profile revision', 'collapsed group leader mods position title', 'Create or edit real-leader groups and named cosmetic headers.'),
    'mods.groups.get': ('game profile', '', 'Read profile-local mod groups and their combined order/state revision.'),
    'mods.layout.apply.start': ('confirm entries game plan_revision profile', 'groups preserve_conflicts', 'Validate the plan again, save the layout and reconcile Filegraph once.'),
    'mods.layout.plan.start': ('entries game profile', 'groups preserve_conflicts', 'Preview separator creation, renaming, removal/settings and mod/group ordering together.'),
    'mods.list': ('game profile', '', "List a profile's mods, separators and installed metadata."),
    'mods.move.start': ('game mods position profile', '', 'Move mods as a priority-ordered block.'),
    'mods.move_profile.plan': ('game mods profile target_profile', 'if_exists renames separator', 'Inspect a cross-profile move and source-sharing constraints.'),
    'mods.move_profile.start': ('confirm game mods plan_revision profile target_profile', 'if_exists renames separator', 'Copy selected mods, then remove their source entries and unshared folders.'),
    'mods.nexus.action.start': ('action confirm game mods profile revision', '', 'Endorse, abstain, track, or untrack selected Nexus identities, verify live state, and report partial results.'),
    'mods.nexus.get.start': ('game mods profile', '', 'Read live account endorsement/tracking state, selected owners, cached flags, and an action revision.'),
    'mods.notes.get': ('game profile', 'mods', 'Read profile-local notes for real mods and the current revision.'),
    'mods.notes.set.start': ('changes game profile revision', 'append', 'Replace, append, or remove selected notes atomically.'),
    'mods.properties.get': ('game mod profile', '', 'Read version labels, root-folder routing, source-specific update ignores, ownership, affected profiles, and revision.'),
    'mods.properties.get.batch.start': ('game profile', 'mods', 'Read properties and per-mod revisions with one shared ownership inventory.'),
    'mods.properties.set.batch.start': ('game items profile', '', 'Validate all property edits before writing and refresh each affected profile once.'),
    'mods.properties.set.start': ('changes game mod profile revision', '', 'Save supported metadata fields atomically and refresh affected routing when requested.'),
    'mods.quick_update.plan.start': ('game profile', 'mods source', 'Plan updates using the desktop Nexus filename-family matcher, with owner resolution and a revision.'),
    'mods.quick_update.start': ('confirm game profile revision', 'download_only installer_policy mods per_mod source', 'Acquire archives in parallel and optionally replace mods sequentially, with per-mod rollback and partial-result reporting.'),
    'mods.reinstall.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'mods.reinstall.start': ('confirm game mod profile', 'archive_id bain_selected bundle_selected fomod_selections installer_policy', 'Reinstall a mod from its recorded archive.'),
    'mods.remove.start': ('confirm game mods profile', '', 'Permanently remove one or more mods.'),
    'mods.rename.plan': ('game mod new_name profile', '', 'Inspect a staged-folder rename, affected profiles and groups, and any blockers.'),
    'mods.rename.start': ('confirm game mod new_name plan_revision profile', '', 'Rename the folder and every referencing profile after rechecking the plan.'),
    'mods.requirements.get': ('game profile', 'mods', 'Read live requirement status and enabled/disabled providers from saved Nexus graphs.'),
    'mods.requirements.ignore.set.start': ('game mod profile revision', 'ignored requirement_ids', "Change a profile's owner warning or the mod's individual requirement ignores."),
    'mods.set_enabled.start': ('changes game profile', '', 'Start a batch mod enable/disable job.'),
    'mods.version.change.start': ('confirm game mod profile revision', 'archive_id bain_selected bundle_selected dependency_policy file_id fomod_selections installer_policy nxm_url source version', 'Replace an installed mod with a selected source version and refresh affected profile routing and plugins.'),
    'mods.versions.get.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'mods.versions.get.start': ('game mod profile', 'source', 'Read installed source identity, the version catalogue, recommendations, ownership, and revision.'),
    'nexus.auth.get': ('', '', 'Inspect saved-credential and active-login state without exposing credentials.'),
    'nexus.auth.login.cancel': ('', '', 'Cancel the active login flow.'),
    'nexus.auth.login.start': ('', 'open_browser', 'Start a Nexus OAuth login flow and return its authorisation URL.'),
    'nexus.auth.login.submit_code': ('code', '', 'Submit the manual fallback code for the active login.'),
    'nexus.auth.logout.start': ('confirm', '', 'Clear saved Nexus OAuth and legacy API-key credentials.'),
    'nexus.collections.get.start': ('game slug', 'domain include_manifest revision', 'Fetch one collection revision and optionally its authoritative manifest.'),
    'nexus.collections.search.start': ('game', 'domain include_adult limit offset query sort', 'Browse or search the Nexus collection catalogue.'),
    'nexus.download.start': ('game', 'domain file_id mod_id nxm_url', "Download one Nexus archive into Amethyst's managed game cache."),
    'nexus.files.get.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'nexus.files.get.start': ('file_id game mod_id', 'domain', 'Fetch one exact Nexus file.'),
    'nexus.mods.files.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'nexus.mods.files.start': ('game mod_id', 'domain', 'Fetch the files and update chain for one Nexus mod.'),
    'nexus.mods.filters.start': ('game', 'domain', 'Fetch category and tag facets for Nexus catalogue queries.'),
    'nexus.mods.get.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'nexus.mods.get.start': ('game mod_id', 'domain', 'Fetch one Nexus mod by exact ID.'),
    'nexus.mods.search.start': ('game', 'categories created_since_days domain filters limit mode offset query sort uploader_id', 'Browse or search the paged Nexus mod catalogue.'),
    'nexus.user.get.start': ('', 'refresh', 'Validate the login and return sanitized account information.'),
    'plugins.list.start': ('game profile', '', "Resolve a profile's ordered plugin inventory and metadata."),
    'plugins.move.start': ('game plugins position profile', '', 'Move a contiguous plugin block within engine constraints.'),
    'plugins.set_enabled.start': ('changes game profile', '', 'Start a batch plugin enable/disable job.'),
    'plugins.sort.start': ('game profile', 'apply_order', 'Run LOOT, refresh metadata, and optionally apply its constrained order.'),
    'plugins.userlist.edit.start': ('action game profile revision', 'after before confirm description field group plugin plugins target', "Edit the selected profile's LOOT user metadata."),
    'plugins.userlist.get': ('game profile', '', 'Read effective LOOT rules, groups, assignments, cycle diagnostics and revision.'),
    'profiles.convert.start': ('confirm game profile', '', 'Convert a shared-pool profile to profile-specific mod storage.'),
    'profiles.copy.start': ('game name profile', '', 'Copy a private profile with disk-space checks, progress, cancellation, and path rebasing.'),
    'profiles.create.start': ('game name', 'copy_from profile_specific_mods', 'Create or copy a profile.'),
    'profiles.export.get.start': ('game profile', 'changes include_overwrite include_root_folder include_saves settings', 'Preview a profile archive, including patches, warnings, blockers, and a revision.'),
    'profiles.export.settings.save.start': ('game profile revision', 'changes include_overwrite include_root_folder include_saves name overwrite settings', "Save per-mod export settings under the selected profile's workshop folder."),
    'profiles.export.start': ('game path profile revision', 'allow_patch_warnings changes include_overwrite include_root_folder include_saves overwrite settings', 'Atomically write the previewed `.amethyst` or ZIP archive.'),
    'profiles.groups.create.start': ('game members name', 'ini_source overwrite_excluded', 'Create and materialize a profile group.'),
    'profiles.groups.exclusions.get': ('game profile', '', 'List member mods excluded from a Profile Group and return a revision.'),
    'profiles.groups.exclusions.set.start': ('confirm exclusions game profile revision', '', 'Save exclusions and rematerialize the group.'),
    'profiles.groups.set_members.start': ('expected_members game members profile', '', "Replace a group's ordered membership and rematerialize it."),
    'profiles.list': ('game', '', 'List existing profiles and summary counts.'),
    'profiles.remove.start': ('confirm game profile', '', 'Permanently remove a profile.'),
    'profiles.rename.start': ('game new_name profile', '', 'Rename a profile.'),
    'profiles.select.start': ('game profile', '', 'Persist and load the active profile.'),
    'profiles.share.get.start': ('game profile', '', 'Create a downloadable-mod share code from the selected profile.'),
    'profiles.share.inspect': ('code', '', 'Decode and validate a share code without downloading or installing mods.'),
    'restore.start': ('game profile', '', 'Start a restore job.'),
    'saves.entries.start': ('game location profile', 'directory limit offset', 'List a save folder, with entry revisions and pagination.'),
    'saves.export.start': ('archive_path game location profile', 'overwrite', 'Export matching saves to a ZIP archive.'),
    'saves.import.start': ('archive_path confirm game location profile', '', 'Import a ZIP archive, moving existing saves to a backup first.'),
    'saves.locations.start': ('game profile', '', "Resolve the game's known save locations and an existing profile-saves folder."),
    'saves.remove.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'saves.remove.start': ('confirm entry game location profile revision', '', 'Delete one selected save entry.'),
    'saves.transfer.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'saves.transfer.start': ('entry game location profile revision target_profile', 'confirm move overwrite', 'Copy or move an entry into profile saves, with explicit confirmation for moves or replacements.'),
    'separators.create.start': ('game name profile revision', 'position', 'Add a separator above or below an existing mod-list entry, or at the top by default.'),
    'separators.remove.start': ('confirm game profile revision separator', '', 'Remove a separator and its saved state without removing its mods.'),
    'separators.rename.start': ('game new_name profile revision separator', '', 'Rename a separator and migrate its saved state.'),
    'separators.settings.set.start': ('changes game profile revision separator state_revision', '', 'Change collapse, lock, colour, or deployment settings if both revisions still match.'),
    'system.handshake': ('', 'client protocol_version', 'Negotiate and inspect the protocol.'),
    'system.ping': ('', '', 'Check that the service is responsive.'),
    'system.schema.get': ('', 'method', ''),
    'thunderstore.download.start': ('game namespace package version', '', 'Download an exact community-checked package version without authentication; pause and cancel are supported.'),
    'thunderstore.packages.versions.batch.start': ('game items', 'on_error profile', 'Run selected items in one tracked batch with individual results.'),
    'thunderstore.packages.versions.start': ('game namespace package', '', "Read a package's published versions and recommendation for the selected game's community."),
    'tools.4gb_patch.apply.start': ('confirm game profile revision', '', 'Apply the built-in New Vegas or Oblivion 4GB patch, preserving the original backup.'),
    'tools.4gb_patch.inspect.start': ('game profile', '', 'Inspect a supported executable and backup, available patch actions, automatic-patching behavior, and a checked revision.'),
    'tools.4gb_patch.restore.start': ('confirm game profile revision', '', 'Restore and consume the executable backup, using the same recoverable transaction as Apply.'),
    'tools.archive_packing.apply.start': ('action confirm game mods profile revision', 'archives options', 'Recheck the plan, run the shared desktop batch, and refresh affected profiles and plugin lists under the operation lock.'),
    'tools.archive_packing.plan.start': ('action game mods profile', 'archives options', 'Inspect archive names, mod ownership, affected profiles and deployment restrictions; return an apply revision.'),
    'tools.archive_packing.scan.start': ('game profile', '', 'Refresh the archive inventory and assess enabled or tracked mods for BSA/BA2 packing and unpacking.'),
    'tools.list': ('game', 'profile', 'Discover built-in game tools, categories, related headless methods, and declared script-extender version/download choices without importing wizard views or external plugins.'),
    'tools.lod.acquire.start': ('game profile revision tool_id', '', 'Acquire the declared xLODGen GitHub release into the managed cache, or return the manual DynDOLOD standalone download link.'),
    'tools.lod.inspect.start': ('archive_path game profile revision tool_id', '', 'Inspect the selected 64-bit LOD application, archive identity, destination writes and plan revision without installing it.'),
    'tools.lod.install.start': ('archive_path confirm game plan_revision profile revision tool_id', 'allow_unverified_source', 'Recheck and install xLODGen, TexGen or DynDOLOD through the shared recoverable application installer.'),
    'tools.lod.run.plan.start': ('game profile revision tool_id', 'deploy launch_args launch_env mod_name output_path prefer_discrete_gpu prefix_mode proton_name replace_output root_folder_enabled save_settings winetricks_style', 'Plan generation, prefix, source identities, output replacement and dedicated work folder without changing files.'),
    'tools.lod.run.start': ('confirm game output_path plan_revision profile revision tool_id', 'deploy launch_args launch_env mod_name prefer_discrete_gpu prefix_mode proton_name replace_output root_folder_enabled save_settings winetricks_style', 'Run the tool, recover deployment and import its generated files as an enabled data mod.'),
    'tools.reshade.dll.acquire.start': ('architecture game profile', '', 'Download the current official full add-on ReShade installer over HTTPS, extract exactly the requested DLL into the managed cache, and return its path, version, size, and SHA-256. The job can be paused or cancelled.'),
    'tools.reshade.inspect.start': ('dll_name dll_path game mode profile shader_path', 'mod_name', 'Verify both acquired file identities, unpack the shader bundle into temporary storage, and preview the exact files, destination collisions, and plan revision.'),
    'tools.reshade.install.start': ('confirm dll_name dll_path game mode plan_revision profile shader_path', 'mod_name replace_existing', 'Recheck the plan, restore before direct game installation, and transactionally install the DLL, shader tree, ReShade.ini, and preset. Source files are retained. The returned `wine_override` must be applied separately.'),
    'tools.reshade.options.get': ('game profile', '', "Read the ReShade wizard's game defaults, rendering API and architecture choices, optional shader packs, profile-specific install locations, and d3dcompiler_47 status. No download or install occurs."),
    'tools.reshade.shaders.acquire.start': ('game profile', 'pack_indices preset_path', 'Acquire the official base shader pack plus selected optional packs. A preset with enabled effects selects all packs and trims the result to those effects. Return a cached ZIP bundle, hash, selected packs, and missing effects. The job can be paused or cancelled.'),
    'tools.script_extender.acquire.start': ('game revision tool_id', 'nxm_url profile version_index', 'Acquire a declared script-extender archive into the managed cache, or return a manual download handoff.'),
    'tools.script_extender.inspect.start': ('archive_path game mode profile revision tool_id', 'version_index', 'Extract into temporary storage and preview the exact tool installation destination, collisions, and a plan revision.'),
    'tools.script_extender.install.start': ('archive_path confirm game mode plan_revision profile revision tool_id', 'allow_unverified_source replace_existing version_index', 'Recheck the plan and transactionally install the selected script extender.'),
    'tools.smapi.acquire.start': ('game', '', 'Download the latest official SMAPI installer ZIP into the managed cache with a checked archive identity. This job can pause or cancel.'),
    'tools.smapi.inspect.start': ('archive_path game mode profile', 'mod_name', 'Prepare the native SMAPI payload and inspect destination writes, launcher backup, dependency manifest source, and a plan revision.'),
    'tools.smapi.install.start': ('archive_path confirm game mode plan_revision profile', 'allow_unverified_source mod_name replace_existing', 'Recheck the plan and install SMAPI with recoverable payload writes and native launcher setup.'),
    'tools.synthesis.acquire.start': ('game profile revision tool_id', '', 'Download the declared Synthesis ZIP release to the managed cache.'),
    'tools.synthesis.inspect.start': ('archive_path game profile revision tool_id', '', 'Inspect application files, source identity, destination writes and installation revision.'),
    'tools.synthesis.install.start': ('archive_path confirm game plan_revision profile revision tool_id', 'allow_unverified_source', 'Recheck and install Synthesis while preserving settings, patcher data and its prefix.'),
    'tools.synthesis.prefix.plan.start': ('game profile revision tool_id', 'proton_name save_settings', 'Inspect the dedicated prefix, installed runtime files, runner and game registration without launching Wine.'),
    'tools.synthesis.prefix.setup.start': ('confirm game plan_revision profile revision tool_id', 'proton_name save_settings', 'Prepare the dedicated 64-bit prefix, verify runtime execution and game registration, then finish prefix cleanup.'),
    'tools.wrye_bash.acquire.start': ('game profile revision tool_id', '', 'Download the declared Wrye Bash standalone GitHub release into the managed cache.'),
    'tools.wrye_bash.inspect.start': ('archive_path game profile revision tool_id', '', 'Inspect the executable, archive identity, destination writes and installation revision without installing.'),
    'tools.wrye_bash.install.start': ('archive_path confirm game plan_revision profile revision tool_id', 'allow_unverified_source', 'Recheck and install Wrye Bash through the shared recoverable application installer.'),
    'tools.wrye_bash.run.plan.start': ('game profile revision tool_id', 'deploy launch_args launch_env prefix_mode proton_name root_folder_enabled save_settings winetricks_style', 'Inspect the installed application, source files, prefix and game link without changing files.'),
    'tools.wrye_bash.run.start': ('confirm game plan_revision profile revision tool_id', 'deploy launch_args launch_env prefix_mode proton_name root_folder_enabled save_settings winetricks_style', 'Run Wrye Bash, preserve changed patch files and finish Restore under the shared operation locks.'),
    'tools.xedit.acquire.start': ('game profile revision tool_id', 'nxm_url', 'Download the selected pinned Nexus xEdit archive, or return manual source information for a build without an automatic source.'),
    'tools.xedit.inspect.start': ('archive_path game profile revision tool_id', '', 'Prepare the selected xEdit executable and inspect writes into its Applications folder with a plan revision.'),
    'tools.xedit.install.start': ('archive_path confirm game plan_revision profile revision tool_id', 'allow_unverified_source', 'Recheck and install the xEdit application with recoverable file writes, preserving unrelated application files and retaining the archive.'),
    'tools.xedit.run.plan.start': ('game profile revision tool_id', 'deploy launch_args launch_env mode plugins prefix_mode proton_name root_folder_enabled save_settings use_64bit winetricks_style', 'Inspect the installed application, profile and prefix without initializing or deploying anything; return a revision for interactive launch or batch cleaning.'),
    'tools.xedit.run.start': ('confirm game plan_revision profile revision tool_id', 'deploy launch_args launch_env mode plugins prefix_mode proton_name root_folder_enabled save_settings use_64bit winetricks_style', 'Deploy if requested, prepare the prefix, run xEdit or selected QuickAutoClean processes, preserve edited plugins and complete the standard Restore pipeline.'),
    'wabbajack.archive.import.start': ('archive_hash game package_path source_path', 'downloads_path expected_identity', 'Verify and import a local archive required by the package.'),
    'wabbajack.gallery.start': ('', 'cached_only game refresh', 'List authored Wabbajack packages from the gallery.'),
    'wabbajack.inspect.start': ('package_path', '', "Inspect a local Wabbajack package's game, profiles, and archive plan."),
    'wabbajack.install.start': ('game package_path', 'accept_known_issues clear_archives conflict_policy downloads_path expected_identity fixes gallery_id game_roots installation_id installation_name mode profiles setup_options', 'Install, resume, repair, or update a local Wabbajack package.'),
    'wabbajack.list': ('', 'game', 'List managed Wabbajack installations and referencing profiles.'),
    'wabbajack.package.download.start': ('entry_id', 'expected_version', 'Download and inspect a gallery package.'),
    'wabbajack.preflight.start': ('game package_path', 'clear_archives downloads_path expected_identity fixes gallery_id game_roots installation_id installation_name mode profiles setup_options', "Check an installation's package, source files, downloads, disk space, and setup requirements."),
    'wabbajack.remove.start': ('confirm game installation_id', '', 'Remove a managed installation and its linked profiles.'),
    'wabbajack.reset_load_order.start': ('game profile', '', "Restore a Wabbajack profile's authored mod and plugin order."),
    'wine.dependencies.install.start': ('confirm dependencies game profile revision', '', "Install selected optional dependencies into the selected profile's idle prefix and verify each outcome."),
    'wine.dependencies.list.start': ('game profile', '', 'List shared Wine dependency choices and their state, including components the game does not declare.'),
    'wine.dll_overrides.get': ('game profile', '', 'Read current prefix overrides, stored choices, defaults, persistent removals/resets, and revision.'),
    'wine.dll_overrides.set.start': ('game profile revision', 'apply changes reset', "Save DLL overrides or default choices and optionally apply them to the selected profile's idle prefix."),
    'wine.game_registry.inspect.start': ('game profile', '', "Inspect the selected Bethesda game's install-path registration and available runner without changing the prefix."),
    'wine.game_registry.write.start': ('confirm game profile revision', 'force', 'Write and verify the install path in the required registry views, including an explicit repeat write.'),
    'wine.prefix.health.start': ('game profile', 'check_proton', "Report the selected profile's prefix health and repairable checks without installing or changing configuration."),
    'wine.prefix.repair.start': ('game profile revision', 'all check_proton checks', 'Repair selected failing health checks and verify the live prefix afterward.'),
}

_BOOL = set('accept_game_version_mismatch accept_known_issues allow_patch_warnings allow_unverified_source active_only all append apply apply_order cached_only check_proton clear_archives collapsed configured_only confirm create_mod deploy download_only enabled force hidden ignored include_adult include_manifest include_overwrite include_root_folder include_saves manual_interactive migrate_staging move open_browser overwrite overwrite_existing prefer_discrete_gpu preserve_conflicts profile_specific_mods refresh replace_existing replace_output root_folder root_folder_enabled save_settings skip skip_existing use_64bit winetricks_style'.split())
_INTEGER = set('after created_since_days file_id limit mod_id offset protocol_version request_id uploader_id version_index wait_ms'.split())
_ARRAY = set('archive_ids bain_selected bundle_selected categories checks dependencies entries exclusions expected_members fixes items members mods overwrite_excluded pack_indices paths plugins profiles requirement_ids reset rules skipped_optional_file_ids sources'.split())
_OBJECT = set('archives changes client filters fomod_selections game_roots groups manual_archives options overrides per_mod renames settings setup_options'.split())
_DYNAMIC = {'expected_value', 'value', 'position'}
_JOB = {
    'type': 'object',
    'required': ['id', 'kind', 'state', 'created_at', 'cancellable', 'pausable', 'event_sequence'],
    'properties': {
        'id': {'type': 'string'}, 'kind': {'type': 'string'},
        'state': {'enum': ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted']},
        'game': {'type': ['object', 'null']}, 'profile': {'type': ['string', 'null']},
        'options': {'type': 'object'}, 'created_at': {'type': 'string'},
        'started_at': {'type': ['string', 'null']}, 'finished_at': {'type': ['string', 'null']},
        'progress': {'type': ['object', 'null']}, 'result': {'type': ['object', 'null']},
        'error': {'type': ['object', 'null']}, 'cancellable': {'type': 'boolean'},
        'pausable': {'type': 'boolean'}, 'paused': {'type': 'boolean'},
        'cancel_requested': {'type': 'boolean'}, 'event_sequence': {'type': 'integer'},
        'history_persisted': {'type': 'boolean'},
    },
}
_ERROR = {
    '$schema': DIALECT, 'type': 'object', 'required': ['code', 'message', 'data'],
    'properties': {
        'code': {'type': 'integer'}, 'message': {'type': 'string'},
        'data': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string'}, 'details': {'type': 'object'},
        }},
    },
}


def _parameter(method, name):
    if method.startswith('profiles.export.'):
        if name == 'changes':
            return {'type': 'array', 'maxItems': 4096, 'items': {'type': 'object'}}
        if name == 'settings':
            return {'type': 'string', 'default': 'latest'}
    if method in {'mods.set_enabled.start', 'plugins.set_enabled.start'} and name == 'changes':
        return {'type': 'array', 'items': {
            'type': 'object', 'required': ['name', 'enabled'], 'additionalProperties': False,
            'properties': {'name': {'type': 'string'}, 'enabled': {'type': 'boolean'}},
        }}
    if name in _DYNAMIC:
        return {'description': 'The accepted shape depends on the selected operation or game.'}
    if name in {'revision', 'target_revision'}:
        return {'type': ['string', 'integer', 'null']}
    if method == 'plugins.userlist.edit.start' and name in {'after', 'before'}:
        return {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4096}
    if name in _BOOL:
        return {'type': 'boolean'}
    if name in _INTEGER:
        result = {'type': 'integer'}
        if name in {'after', 'offset', 'version_index', 'wait_ms'}:
            result['minimum'] = 0
        if name == 'wait_ms':
            result['maximum'] = 30000
        if name == 'protocol_version':
            result['type'] = ['integer', 'null']
        return result
    if name in _ARRAY:
        return {'type': 'array'}
    if name in _OBJECT:
        return {'type': ['object', 'null']} if name == 'client' else {'type': 'object'}
    if name in {'prefix_path', 'staging_path', 'save_path_override', 'appimage_path', 'launcher_id',
                'deploy_mode', 'runtime_mode'}:
        return {'type': ['string', 'null']}
    if name == 'installer_policy':
        return {'enum': ['reject', 'defaults', 'verbatim'], 'default': 'reject'}
    if name == 'on_error':
        return {'enum': ['stop', 'continue'], 'default': 'stop'}
    return {'type': 'string'}


def _params(method):
    from .batch import METHODS
    from .service import SNAPSHOT_METHODS

    required, optional, _ = _CONTRACTS[method]
    names = (required + ' ' + optional).split()
    schema = {'type': 'object', 'properties': {name: _parameter(method, name) for name in names},
              'required': required.split(), 'additionalProperties': False}
    if method in SNAPSHOT_METHODS:
        schema['properties']['allow_cached'] = {'type': 'boolean', 'default': True}
    if method in METHODS:
        child = _params(METHODS[method])
        if 'profile' in child['required']:
            schema['required'].append('profile')
        child['required'] = [key for key in child['required'] if key not in {'game', 'profile'}]
        for key in ('game', 'profile'):
            child['properties'].pop(key, None)
        schema['properties']['items'] = {'type': 'array', 'minItems': 1, 'maxItems': 256, 'items': child}
    if method == 'plugins.userlist.edit.start':
        from .plugin_userlist import ACTIONS
        common = {'game', 'profile', 'revision', 'action'}
        schema['oneOf'] = []
        for action, (needed, allowed) in ACTIONS.items():
            names = common | needed | allowed
            schema['oneOf'].append({
                'properties': {**{name: {} for name in names}, 'action': {'const': action}},
                'required': sorted(common | needed), 'additionalProperties': False,
            })
    if method == 'mods.version.change.start':
        schema['oneOf'] = [
            {'properties': {'source': {'const': 'thunderstore'}}, 'required': ['source', 'version']},
            {'properties': {'source': {'enum': ['nexus', 'modio']}}, 'required': ['file_id']},
        ]
    if method == 'collections.import.start':
        schema['oneOf'] = [{'required': ['path']}, {'required': ['code']}]
    return schema


def _result(method):
    schema = {'$schema': DIALECT, 'type': 'object'}
    job_methods = {'jobs.get', 'jobs.cancel', 'jobs.pause', 'jobs.resume', 'jobs.history.get'}
    if (method.endswith('.start') and method != 'nexus.auth.login.start') or method in job_methods:
        schema.update(required=['job'], properties={'job': {'$ref': '#/$defs/job'}},
                      **{'$defs': {'job': deepcopy(_JOB)}})
    elif method == 'jobs.list':
        schema.update(required=['jobs'], properties={'jobs': {'type': 'array', 'items': {'$ref': '#/$defs/job'}}},
                      **{'$defs': {'job': deepcopy(_JOB)}})
    elif method in {'jobs.history.list', 'jobs.recovery.list'}:
        schema.update(required=['records', 'total', 'offset', 'limit'], properties={
            'records': {'type': 'array', 'items': {'type': 'object', 'required': ['job'],
                        'properties': {'job': {'$ref': '#/$defs/job'}}}},
            'total': {'type': 'integer'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'},
        }, **{'$defs': {'job': deepcopy(_JOB)}})
    elif method == 'jobs.events':
        schema.update(required=['job_id', 'events', 'next_sequence', 'complete'], properties={
            'job_id': {'type': 'string'}, 'events': {'type': 'array', 'items': {'type': 'object'}},
            'next_sequence': {'type': 'integer'}, 'oldest_sequence': {'type': 'integer'},
            'truncated': {'type': 'boolean'}, 'complete': {'type': 'boolean'},
        })
    elif method in {'launch.get', 'launch.stop'}:
        schema.update(required=['launch'], properties={'launch': {'type': ['object', 'null']}})
    else:
        roots = {
            'system.handshake': {'protocol': 'object', 'application': 'object', 'capabilities': 'object'},
            'system.ping': {'ok': 'boolean', 'protocol_version': 'integer', 'application_version': 'string'},
            'system.schema.get': {'schema_version': 'integer', 'methods': 'array'},
            'games.list': {'games': 'array'}, 'games.get': {'game': 'object'},
            'profiles.list': {'profiles': 'array'}, 'mods.list': {'mods': 'array'},
            'launch.active.list': {'launches': 'array'},
        }.get(method, {})
        if roots:
            schema.update(required=list(roots), properties={key: {'type': value} for key, value in roots.items()})
    return schema


def catalogue(methods, mutations, method=None):
    if set(methods) != set(_CONTRACTS):
        raise RuntimeError('The method schema catalogue must match the registered methods.')
    selected = [method] if method is not None else list(methods)
    records = []
    for name in selected:
        params = {'$schema': DIALECT, **_params(name)}
        records.append({
            'name': name, 'description': _CONTRACTS[name][2],
            'mutating': (name.removesuffix('.start') in mutations or name in
                         {'jobs.cancel', 'jobs.pause', 'jobs.resume',
                          'nexus.auth.login.submit_code', 'nexus.auth.login.cancel'}),
            'params': params, 'result': _result(name), 'error': deepcopy(_ERROR),
            'validation_scope': 'parameter_names_and_common_types',
        })
    return {'schema_version': 1, 'dialect': DIALECT, 'methods': records}
