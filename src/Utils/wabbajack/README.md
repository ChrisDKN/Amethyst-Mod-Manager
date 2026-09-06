# Wabbajack installation

Open **Nexus → Browse Wabbajack modlists…** in the Qt application. The tab can be detached. Browsing, cached feeds, package inspection, and local README viewing do not require Nexus authentication. Setup selects a configured game, authored profiles, download directory, and managed installation directory. All authored profiles are selected initially; adult gallery entries are hidden initially.

Run **Check requirements** before installing. Existing installations offer Resume, Repair, and Update. Conflicts show installed/author text differences and explicit Keep Mine or Use Author choices. Shared-file changes affect every profile linked to that installation. INIs, saves, enabled states, plugin order, and runtime output remain per profile.

`inspect_package`, `preflight`, `run_install`, `plan_update`, `repair`, and `update` are exported by `Utils.wabbajack`. `InstallRequest` supplies the game adapter, permanent paths, selected profiles, original game sources, optional Nexus API, accepted runtime adjustments, and conflict resolver. `InstallCallbacks` and `InstallControl` come from `Utils.downloads.install`; collection entrypoints retain their existing names and policies. Workers report progress through callbacks and never create Qt widgets.

Each installation lives under `<game profile root>/.wabbajack/<installation>/`. Its versioned SQLite database records package identity, ownership, authored baselines, actual hashes, progress, and publication recovery. Profiles use relative links to its shared mods directory. Download caches are independent. Removing the final profile reference removes completed managed installation data; Cache Manager can clear abandoned jobs and update backups.

Downloads require exact size and xxHash64 identity. Required archives cannot be skipped. Nexus links match the pending game domain, mod ID, and file ID. Premium acquisition, manual browser/file selection, HTTP, and multipart CDN acquisition share scheduling and bandwidth controls. Downloads and completed reconstruction survive interrupted jobs. A game-level lock excludes deployment during installation; publication uses backups and a durable rollback journal.

Archive and patch processing streams data and limits concurrent work. ZIP compression unsupported by Python is routed through 7-Zip. TES3, BSA 103/104/105, and BA2 GNRL/DX10 versions 1/2/3/7/8 have explicit reconstruction writers with content readback. BA2 extraction selects legacy/DX10 DDS headers and validates declared mip ranges and chunk sizes. LZ4 texture chunks are limited to 256 MiB; tiled textures and password-protected source archives receive explicit failures. Temporary extraction space is estimated before downloading and checked against actual expansion before extraction.

Deterministic files must match their expected hashes and sizes. Remapped files, rebuilt archives, textures, and archive metadata record the actual verified result. Compiled `profiles/*/modlist.txt` files are a format exception: packages can contain cleaned selections while retaining hashes/sizes of the original uncompiled file. Inspection retains that original metadata, derives the expected output from the embedded profile, and reports the discrepancy in preflight. Only this recognized profile format receives that treatment; ordinary inline-file mismatches remain errors.

Texture setup pins Microsoft's Texconv `may2026` executable, SHA256 `dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06`. **Install Texture Tool** prepares its own Proton prefix and VC++ runtime. Game-prefix dependencies are separate reviewable choices. Stock-game profiles use their reconstructed game-path override and direct prefix launch, retaining the original game location for source verification.

## Validation recorded 2026-09-06

| Public package | Manifest version | Result |
| --- | --- | --- |
| Fear of the Wanderer | 0.0.1.0 | Real premium acquisition, reconstruction and publication completed for the New Vegas profile. Independently rechecked 8,121 deterministic/embedded-profile outputs and four remapped outputs. The TTW profile was excluded because its external TTW installation was absent. |
| The Midnight Ride | 26.7.26 | Real premium acquisition, reconstruction and publication completed for both authored profiles. Independently rechecked 2,430 deterministic/embedded-profile outputs and three remapped outputs. |
| Keizaal | 8.0.2 | Package/CDN integrity and real-game preflight checked. Installation blocked: 17 required game-source files verified, but 118 were missing or differed from the required versions. |

Package SHA256 identities:

- Fear of the Wanderer: `e451f45a2aa187881ac8af3734d6579bb0c14d490e379a4645ef2f5b28cca725`
- The Midnight Ride: `58642143ea15eeb0aae2bab0cc15ab5ff883d657251c79536e872d2a232bb885`
- Keizaal: `8ec2d329b8fa4467a0c9ae3fcff8cd25efbc412b12c4b9bbb51650022a996947`

Real downloads exposed and exercised PPMd ZIP extraction, mixed-case source directories, cross-game Nexus tools, and compiled-profile metadata. Failed jobs were resumed using retained archives and completed outputs. Validation installations used isolated profile roots, with no deployment into the configured games.

Additional checks:

- Live gallery: 220 entries; offline cache fallback and individual-feed failure handling.
- Simulated service responses: free/manual acquisition while HTTP continues, wrong-file rejection, cross-game NXM matching, expired premium links, resumed HTTP ranges, multipart part/final checksums, and cancellation.
- Temporary installations: nested archives, game-file Octodiff, ordered merged patches, BSA building, restart reuse, per-profile INIs/saves/overwrite, root payloads, stock-game launch configuration, shared mods, three-way INI updates, conflict choices, profile duplication/groups, and final-reference cleanup.
- Real pinned Texconv execution: capability probe and BC1-to-BC7 resize in an isolated Proton prefix; reconstructed DDS metadata compared against its output.
- Native Filegraph: shared-profile invalidation and refreshed catalogs.
- Qt QTest interaction with an offscreen platform: setup/preflight/install/completion, conflict review, and mandatory manual-download controls.
- Focused corruption checks in `_selftest.py`: Octodiff bounds/checksum/atomic replacement, archive reconstruction, path boundaries/casing, compiled-profile integrity, and process interruption during publication. Existing BA2 checks also pass.
- Collection compatibility imports, BSDIFF application, and provided-INI handling; Python compilation/static checks, Meson source coverage, and Flatpak dependency YAML parsing.

No game was launched. Authored manual-install folders and Linux compatibility instructions remain requirements for the user to review. Real free-account browser interaction, a desktop-rendered Qt session, complete AppImage/Flatpak builds, and a full end-to-end collection reinstallation were not validated. Meson was unavailable in this development environment. Proof-of-concept migration and modlist compilation/export are excluded.
