"""
ja3_packs.py
Route loose .hpk map/pack files into the game install Packs/ tree.

Most JA3 mods install into the prefix AppData Mods/ folder (handled by the
normal deploy).  A few ship a loose ``<something>.hpk`` (NOT ``ModContent.hpk``)
that must replace a vanilla pack inside the game install — usually
``Packs/Maps/<name>.hpk``.  These overwrite vanilla files, so the originals are
backed up on deploy and put back on restore.

Routing of a loose .hpk:
  - basename ``ModContent.hpk``  → leave for the normal AppData deploy.
  - matches a vanilla file under ``Packs/`` (by basename) → replace that exact
    file, backing up the original under ``packs_backup/``.
  - no vanilla match → skip and warn (never guess a location).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping

from Utils.deploy_shared import LinkMode, _do_link, _restore_backup_dir, resolve_mod_dir

_PACKS_DIR = "Packs"
_BACKUP_DIR_NAME = "packs_backup"
_SKIP_NAME = "modcontent.hpk"  # lowercase; stays in AppData


def _vanilla_hpk_index(game_root: Path) -> dict[str, Path]:
    """Map lowercase basename → absolute path for every .hpk under Packs/."""
    index: dict[str, Path] = {}
    packs = game_root / _PACKS_DIR
    if not packs.is_dir():
        return index
    for p in packs.rglob("*.hpk"):
        if p.is_file():
            index.setdefault(p.name.lower(), p)  # first match wins
    return index


def _iter_loose_hpks(filemap_path: Path, staging: "Path | Mapping[str, Path]"):
    """Yield (mod_name, rel_str, src_path) for every routable loose .hpk.

    "Loose" here means a .hpk that is not ModContent.hpk; its position inside
    the mod folder is irrelevant — what matters is that it matches a vanilla
    pack by basename (checked by the caller).
    """
    if not filemap_path.is_file():
        return
    for line in filemap_path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        rel_str, mod_name = line.rstrip("\n").split("\t", 1)
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        if not name.lower().endswith(".hpk"):
            continue
        if name.lower() == _SKIP_NAME:
            continue
        mod_dir = resolve_mod_dir(staging, mod_name)
        if mod_dir is None:
            continue
        src = mod_dir / rel_str
        if src.is_file():
            yield mod_name, rel_str, src


def deploy_packs(
    filemap_path: Path,
    staging: "Path | Mapping[str, Path]",
    game_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
    appdata_mods_dir: Path | None = None,
) -> int:
    """Replace vanilla Packs/ files with matching mod .hpk files (backed up).

    Run AFTER the normal AppData deploy.  When ``appdata_mods_dir`` is given,
    the routed .hpk's copy under AppData Mods/ is removed so it lives only in
    Packs/ (per the mods' install instructions).

    Returns the number of files deployed.
    """
    _log = log_fn or (lambda _: None)
    backup_dir = filemap_path.parent / _BACKUP_DIR_NAME
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    index = _vanilla_hpk_index(game_root)
    deployed = 0
    for mod_name, rel_str, src in _iter_loose_hpks(filemap_path, staging):
        name = src.name
        vanilla = index.get(name.lower())
        if vanilla is None:
            _log(
                f"  WARN: '{name}' ({mod_name}) has no matching vanilla pack "
                f"under {_PACKS_DIR}/ — skipped (place it manually)."
            )
            continue
        # Back up the vanilla file (mirrored under game_root) before replacing.
        rel_to_root = vanilla.relative_to(game_root)
        backup_file = backup_dir / rel_to_root
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vanilla, backup_file)
        vanilla.unlink()
        err = _do_link(str(src), str(vanilla), mode)
        if err is not None:
            # Linking failed; fall back to a plain copy so the mod still applies.
            shutil.copy2(src, vanilla)
        deployed += 1
        _log(f"  Routed '{name}' → {rel_to_root} (vanilla backed up).")
        # Remove the AppData copy that the normal deploy placed for this .hpk,
        # so it lives only in Packs/.
        if appdata_mods_dir is not None:
            appdata_copy = appdata_mods_dir / rel_str
            try:
                if appdata_copy.is_file():
                    appdata_copy.unlink()
            except OSError:
                pass
    if deployed:
        _log(f"  Routed {deployed} pack file(s) into {_PACKS_DIR}/.")
    return deployed


def restore_packs(filemap_path: Path, game_root: Path, log_fn=None) -> int:
    """Restore vanilla Packs/ files from packs_backup/ and remove the backup."""
    _log = log_fn or (lambda _: None)
    backup_dir = filemap_path.parent / _BACKUP_DIR_NAME
    if not backup_dir.is_dir():
        return 0
    restored = _restore_backup_dir(
        backup_dir, game_root, log_fn=_log, label=_BACKUP_DIR_NAME,
        swallow_errors=True,
    )
    if restored:
        _log(f"  Restored {restored} vanilla pack file(s) into {_PACKS_DIR}/.")
    return restored
