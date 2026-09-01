# Crimson Desert backend plan

Crimson Desert cannot use Amethyst's generic file deployment. Its PAZ/PAMT
archives and byte/field patches are version-sensitive and must be composed by
an archive-aware engine. The initial implementation therefore keeps Amethyst
as the user-facing manager and runs the MIT-licensed CDUMM engine out of
process. This avoids loading PyQt6 and PySide6 into the same process.

## Delivery stages

1. Discover Crimson Desert through Steam App ID `3321460` and validate the
   configured `bin64/CrimsonDesert.exe`.
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

## Current prototype boundary

The handler and backend adapter are intentionally fail-closed. They discover
the game and can validate CDUMM, but Deploy and Restore stop before modifying
game files until profile synchronisation and recovery are implemented.
