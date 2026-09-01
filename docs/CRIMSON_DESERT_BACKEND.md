# Crimson Desert backend plan

Crimson Desert cannot use Amethyst's generic file deployment. Its PAZ/PAMT
archives and byte/field patches are version-sensitive and must be composed by
an archive-aware engine. The initial implementation therefore keeps Amethyst
as the user-facing manager and runs the MIT-licensed CDUMM engine out of
process. This avoids loading PyQt6 and PySide6 into the same process.

## Delivery stages

1. Discover Crimson Desert through Steam App ID `3321460` and validate the
   configured `bin64/CrimsonDesert.exe`. Ship the official Steam artwork as
   `src/icons/games/crimson_desert.png` so source, AppImage and Flatpak builds
   render the same complete game card.
2. Discover an explicitly configured CDUMM command or source checkout and run
   its read-only `self_check` JSON-lines worker command. Parse the configured
   game's PAPGT and every PAMT index without creating backend state.
3. Create a versioned backend-state mapping between Amethyst profile entries
   and CDUMM mod IDs. Import active staging folders through CDUMM's worker API;
   never copy PAZ/PAMT archives through Amethyst's generic deploy path.
4. Snapshot and verify vanilla state before enabling Apply. Refuse deployment
   when the game build changed, the game is running, the snapshot is missing,
   an import was skipped, or the backend reports a partial patch.
5. Implement Restore through CDUMM and prove byte-identical recovery in a
   synthetic game tree before touching the real installation.
6. Test one small, reversible real mod with Crimson Desert stopped. Verify the
   backend result, start the game, then restore and compare hashes.
7. Only after these gates pass, expose normal Deploy/Restore controls and
   prepare an upstream design discussion or pull request.

## Implemented development boundary

The development handler now performs the archive-aware path end to end:

- reads the selected Amethyst profile and accepts exactly one supported CDUMM
  source per enabled mod folder;
- creates CDUMM's vanilla hash snapshot before the first Apply;
- persists Amethyst-folder-to-CDUMM-ID mappings in
  `CDMods/amethyst-profile.json`, reimports changed sources in place, and only
  toggles mods owned by that mapping;
- applies and restores through CDUMM's subprocess worker; and
- confirms the resulting backend status after Apply.

The first real validation used Nexus mod 3211, file 14352, against Crimson
Desert 2.00.01. CDUMM applied the single `storeinfo.pabgb` intent, produced a
two-entry overlay in slot `0041`, reported the mod active, restored the vanilla
PAPGT hash, and successfully redeployed it through the Amethyst handler.

Remaining before an upstream pull request: add automated profile-sync tests,
surface worker warnings more cleanly in the GUI, package or declare the CDUMM
runtime dependency, and complete an in-game behavior check.
