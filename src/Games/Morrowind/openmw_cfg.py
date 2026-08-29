"""
openmw_cfg.py
Utility for managing data= and content= entries in openmw.cfg.

OpenMW uses ~/.config/openmw/openmw.cfg (native/AppImage) or
~/.var/app/org.openmw.OpenMW/config/openmw/openmw.cfg (Flatpak).

Unlike Morrowind.ini, load order is determined solely by the order of
content= lines - no mtime manipulation is needed.

Managed keys (fully replaced on every deploy):
  data=             - directories OpenMW searches for assets and plugins
  content=          - ordered plugin load list
  groundcover=      - grass/groundcover plugins (preserved if caller passes None)
  fallback-archive= - BSA archives mounted by OpenMW

All other lines (sections, comments, and other key=value pairs) are left
untouched.
"""

from __future__ import annotations

from pathlib import Path

# Vanilla masters are always present and always load first.
_VANILLA_MASTERS = [
    "Morrowind.esm",
    "Tribunal.esm",
    "Bloodmoon.esm",
]

# Content the ENGINE loads by itself, out of its own VFS
# (resources/vfs/builtin.omwscripts). Listing it again in openmw.cfg is not a
# harmless duplicate: OpenMW 0.51 refuses to start with
#   E] Content file specified more than once: builtin.omwscripts. Aborting...
#   I] Quitting peacefully.
# and exits 0 - so the launch looked "clean" while the game never appeared. It
# used to be a vanilla master here, which meant every deploy AND every restore
# rewrote the game into that state; only opening OpenMW's own launcher (which
# rewrites content= from its own profile) cleared it.
_ENGINE_BUILTIN_CONTENT = {"builtin.omwscripts"}
_GROUNDCOVER_EXTENSIONS = (".esp", ".esm", ".omwaddon", ".omwgame")


def _dedup_content(names: list[str]) -> list[str]:
    """Drop engine-provided and repeated content names, keeping first position.

    Any duplicate is fatal to OpenMW, not just the builtin one - a plugin that
    reaches us twice (plugins.txt plus a vanilla master, two mods shipping the
    same esp name) would kill the launch just as silently.
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.strip().lower()
        if not key or key in seen or key in _ENGINE_BUILTIN_CONTENT:
            continue
        seen.add(key)
        out.append(name)
    return out

# Vanilla BSAs - always included as fallback-archive entries before mod BSAs.
_VANILLA_BSAS = [
    "Morrowind.bsa",
    "Tribunal.bsa",
    "Bloodmoon.bsa",
]

# Exact key names we own (lowercase).
_MANAGED_KEYS = {"data", "content", "groundcover", "fallback-archive"}


def _is_managed_line(line: str) -> bool:
    """Return True if this config line is one we manage."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if "=" not in stripped:
        return False
    key = stripped.split("=", 1)[0].strip().lower()
    return key in _MANAGED_KEYS


def _read_plugins_txt(plugins_txt: Path) -> list[str]:
    """Return the ordered list of active plugin filenames from plugins.txt.

    Handles MO2-style '*' prefixes for active entries; '#' lines are comments.
    """
    from Utils.plugins import read_plugins
    return [
        entry.name for entry in read_plugins(plugins_txt, star_prefix=True)
        if entry.enabled
    ]


def read_groundcover_entries(cfg_path: Path) -> list[str]:
    """Return active groundcover= values from an OpenMW config."""
    if not cfg_path.is_file():
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().lower() != "groundcover":
            continue
        name = value.strip().strip('"')
        low = name.lower()
        if name and low not in seen:
            seen.add(low)
            result.append(name)
    return result


def update_openmw_cfg(
    cfg_path: Path,
    data_dirs: list[Path],
    plugins_txt: Path,
    groundcover_plugins: list[str] | None = None,
    fallback_archives: list[str] | None = None,
    log_fn=None,
) -> None:
    """Rewrite the managed data= / content= / groundcover= / fallback-archive= entries in openmw.cfg.

    Args:
        cfg_path:            Path to openmw.cfg.
        data_dirs:           Ordered list of data directories to write as data= entries.
                             Later entries take priority in OpenMW's VFS (override earlier).
        plugins_txt:         Path to the profile's plugins.txt.
        groundcover_plugins: Plugin names classified as groundcover. Enabled matching
                             entries are removed from content= and written as
                             groundcover=. When None, existing groundcover= lines are
                             preserved and kept out of content=.
        fallback_archives:   Ordered list of .bsa archive names to write as
                             fallback-archive= entries.  When None, existing
                             fallback-archive= lines from the cfg are preserved unchanged.
        log_fn:              Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    # ------------------------------------------------------------------
    # Read existing cfg, stripping managed lines.
    # When groundcover_plugins is None, collect existing groundcover= values
    # so we can re-emit them unchanged.
    # ------------------------------------------------------------------
    preserved: list[str] = []
    existing_groundcover: list[str] = []
    existing_fallback_archives: list[str] = []

    if cfg_path.is_file():
        for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            is_kv = stripped and not stripped.startswith("#") and "=" in stripped
            if is_kv:
                key = stripped.split("=", 1)[0].strip().lower()
                if key == "groundcover" and groundcover_plugins is None:
                    # Preserve existing groundcover= lines when caller did not supply overrides.
                    existing_groundcover.append(stripped.split("=", 1)[1].strip().strip('"'))
                    continue
                if key == "fallback-archive" and fallback_archives is None:
                    # Preserve existing fallback-archive= lines when caller did not supply overrides.
                    existing_fallback_archives.append(stripped.split("=", 1)[1].strip().strip('"'))
                    continue
                if key in _MANAGED_KEYS:
                    continue
            preserved.append(raw)

    # Strip trailing blank lines so the managed block attaches cleanly.
    while preserved and not preserved[-1].strip():
        preserved.pop()

    # ------------------------------------------------------------------
    # Build the ordered plugin list.
    # ------------------------------------------------------------------
    active = _read_plugins_txt(plugins_txt)
    vanilla_lower = {p.lower() for p in _VANILLA_MASTERS}
    if groundcover_plugins is None:
        gc_list = _dedup_content(existing_groundcover)
    else:
        selected = {name.lower() for name in groundcover_plugins}
        gc_list = _dedup_content([
            plugin for plugin in active
            if plugin.lower() in selected and plugin.lower() not in vanilla_lower
            and plugin.lower().endswith(_GROUNDCOVER_EXTENSIONS)
        ])
    groundcover_lower = {name.lower() for name in gc_list}
    user_plugins = [
        plugin for plugin in active
        if plugin.lower() not in vanilla_lower
        and plugin.lower() not in groundcover_lower
    ]
    ordered = _dedup_content(_VANILLA_MASTERS + user_plugins)

    # ------------------------------------------------------------------
    # Assemble managed block.
    # ------------------------------------------------------------------
    managed: list[str] = [""]  # blank separator
    for d in data_dirs:
        managed.append(f'data="{d}"')
    for plugin in ordered:
        managed.append(f"content={plugin}")
    for gc in gc_list:
        managed.append(f"groundcover={gc}")
    if fallback_archives is not None:
        # Always include vanilla BSAs first, then mod BSAs (deduped).
        vanilla_bsa_lower = {b.lower() for b in _VANILLA_BSAS}
        mod_bsas = [b for b in fallback_archives if b.lower() not in vanilla_bsa_lower]
        fa_list = _VANILLA_BSAS + mod_bsas
    else:
        fa_list = existing_fallback_archives
    for fa in fa_list:
        managed.append(f"fallback-archive={fa}")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(preserved + managed) + "\n", encoding="utf-8")
    _log(
        f"  Wrote {len(data_dirs)} data dir(s), {len(ordered)} content plugin(s), "
        f"{len(gc_list)} groundcover plugin(s), and {len(fa_list)} "
        f"fallback-archive(s) to {cfg_path}."
    )


def restore_openmw_cfg(
    cfg_path: Path,
    data_dirs: list[Path],
    log_fn=None,
) -> None:
    """Restore openmw.cfg to vanilla state: base data dirs and vanilla masters only.

    Args:
        cfg_path:  Path to openmw.cfg.
        data_dirs: Vanilla data directories (typically just the game's Data Files dir).
        log_fn:    Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    preserved: list[str] = []
    if cfg_path.is_file():
        for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            is_kv = stripped and not stripped.startswith("#") and "=" in stripped
            if is_kv:
                key = stripped.split("=", 1)[0].strip().lower()
                if key in _MANAGED_KEYS:
                    continue
            preserved.append(raw)

    while preserved and not preserved[-1].strip():
        preserved.pop()

    managed: list[str] = [""]
    for d in data_dirs:
        managed.append(f'data="{d}"')
    vanilla = _dedup_content(_VANILLA_MASTERS)
    for plugin in vanilla:
        managed.append(f"content={plugin}")
    for bsa in _VANILLA_BSAS:
        managed.append(f"fallback-archive={bsa}")

    cfg_path.write_text("\n".join(preserved + managed) + "\n", encoding="utf-8")
    _log(
        f"  Restored openmw.cfg to {len(data_dirs)} vanilla data dir(s) "
        f"and {len(vanilla)} vanilla plugin(s)."
    )
