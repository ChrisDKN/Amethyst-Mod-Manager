"""
openmw_cfg.py
Utility for managing data= and content= entries in openmw.cfg.

OpenMW uses ~/.config/openmw/openmw.cfg (native) or
~/.var/app/org.openmw.OpenMW/config/openmw/openmw.cfg (Flatpak).

Unlike Morrowind.ini, load order is determined solely by the order of
content= lines — no mtime manipulation is needed.

Managed keys (fully replaced on every deploy):
  data=        — directories OpenMW searches for assets and plugins
  content=     — ordered plugin load list
  groundcover= — grass/groundcover plugins (preserved if caller passes None)

All other lines (sections, comments, and other key=value pairs) are left
untouched.
"""

from __future__ import annotations

from pathlib import Path

# Vanilla masters are always present and always load first.
_VANILLA_MASTERS = [
    "Morrowind.esm",
    "builtin.omwscripts",
    "Tribunal.esm",
    "Bloodmoon.esm",
]

# Vanilla BSAs — always included as fallback-archive entries before mod BSAs.
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
    if not plugins_txt.is_file():
        return []
    plugins: list[str] = []
    for raw in plugins_txt.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            line = line[1:].strip()
        # Normalise extension to lowercase.
        dot = line.rfind(".")
        if dot != -1:
            line = line[:dot] + line[dot:].lower()
        plugins.append(line)
    return plugins


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
        groundcover_plugins: Explicit list of groundcover plugin names to write.  When
                             None, any existing groundcover= lines from the cfg are
                             preserved unchanged.
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
    user_plugins = [p for p in active if p.lower() not in vanilla_lower]
    ordered = _VANILLA_MASTERS + user_plugins

    # ------------------------------------------------------------------
    # Assemble managed block.
    # ------------------------------------------------------------------
    managed: list[str] = [""]  # blank separator
    for d in data_dirs:
        managed.append(f'data="{d}"')
    for plugin in ordered:
        managed.append(f"content={plugin}")
    gc_list = groundcover_plugins if groundcover_plugins is not None else existing_groundcover
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
        f"  Wrote {len(data_dirs)} data dir(s), {len(ordered)} plugin(s), "
        f"and {len(fa_list)} fallback-archive(s) to {cfg_path}."
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
    for plugin in _VANILLA_MASTERS:
        managed.append(f"content={plugin}")
    for bsa in _VANILLA_BSAS:
        managed.append(f"fallback-archive={bsa}")

    cfg_path.write_text("\n".join(preserved + managed) + "\n", encoding="utf-8")
    _log(
        f"  Restored openmw.cfg to {len(data_dirs)} vanilla data dir(s) "
        f"and {len(_VANILLA_MASTERS)} vanilla plugin(s)."
    )
