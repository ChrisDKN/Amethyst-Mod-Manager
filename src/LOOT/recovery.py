from __future__ import annotations

import os
import uuid
from pathlib import Path


def recover_plugin_links(game, profile_dir: Path, snapshot, log_fn=None) -> int:
    """Quarantine undeployed plugin links whose exact sources are catalogued mods."""
    if snapshot is None or not getattr(game, "loot_sort_enabled", False):
        return 0
    data = Path(game.get_vanilla_plugins_path())
    active = getattr(game, "_active_profile_dir", None)
    if active is None or Path(active) != profile_dir or not data.is_dir():
        return 0

    def deployed():
        return (game.get_deploy_active()
                or data.with_name(data.name + "_Core").exists()
                or (data / ".mm_deployed").exists())

    if deployed():
        return 0
    extensions = tuple(ext.lower() for ext in
                       (getattr(game, "plugin_extensions", ()) or (".esp", ".esm", ".esl")))
    links = [p for p in data.iterdir() if p.is_symlink() and p.name.lower().endswith(extensions)]
    if not links:
        return 0
    from Utils.filegraph.service import FileGraphService
    from Utils.games.registry import _vanilla_plugins_for_game
    library = FileGraphService.open_library(game, profile_dir)
    session = library.open_profile(profile_dir)
    previous_dir = profile_dir.parent / game.get_last_deployed_profile()
    previous = (session if previous_dir == profile_dir else
                FileGraphService.open_library(game, previous_dir).open_profile(previous_dir))
    if (session.incomplete_operations() or previous.incomplete_operations()
            or session.deployed_entries() or previous.deployed_entries()):
        return 0
    vanilla = _vanilla_plugins_for_game(game)
    staging = Path(game.get_effective_mod_staging_path()).absolute()
    mods = set()
    for link in links:
        try:
            relative = Path(os.readlink(link)).relative_to(staging)
            if len(relative.parts) > 1 and ".." not in relative.parts:
                mods.add(relative.parts[0])
        except (OSError, ValueError):
            continue
    sources = set()
    for mod_name in mods:
        for entry in snapshot.mod_files(mod_name):
            source = staging / entry.mod_name / os.fsdecode(entry.source_rel)
            if source.is_relative_to(staging) and ".." not in source.parts:
                sources.add(str(source))
    recovery_root = data.parent / ".amethyst-loot-recovery"
    if recovery_root.is_symlink():
        return 0
    recovery = recovery_root / uuid.uuid4().hex
    count = 0
    for link in links:
        if link.name.lower() in vanilla:
            continue
        try:
            before = link.lstat()
            target = os.readlink(link)
            if target not in sources or Path(target).name.lower() != link.name.lower():
                continue
            after = link.lstat()
            if (deployed() or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
                    or os.readlink(link) != target):
                continue
            recovery.mkdir(parents=True, exist_ok=True)
            link.rename(recovery / link.name)
            count += 1
        except OSError as exc:
            if log_fn:
                log_fn(f"[loot] Could not recover {link.name}: {exc}")
    if count and log_fn:
        log_fn(f"[loot] Moved {count} leftover plugin link(s) to {recovery}. Mod files were preserved.")
    return count
