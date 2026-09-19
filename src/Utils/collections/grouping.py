from __future__ import annotations

import os
import re
from pathlib import Path

from Utils.atomic_write import write_atomic
from Utils.collections.options import CollectionInstallOptions
from Utils.profiles import groups
from Utils.profiles.state import merge_profile_settings, read_profile_settings


def profile_path(game, name: str) -> Path:
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
        raise ValueError("Invalid profile name.")
    root = game.get_profile_root() / "profiles"
    path = root / name
    if path.is_symlink():
        raise ValueError("Profile directories cannot be symbolic links.")
    return path


def available_name(game, value: str) -> str:
    base = re.sub(r"[^\w\s-]", "", value).strip().replace(" ", "_")[:60] or "Collection"
    root = game.get_profile_root() / "profiles"
    name = base
    number = 2
    while (root / name).exists():
        suffix = f"_{number}"
        name = base[:64 - len(suffix)] + suffix
        number += 1
    return name


def matching_profiles(game, domain: str, slug: str, revision) -> list[str]:
    from Utils.collections.manifest import parse_collection_url
    from Utils.games.registry import _profiles_for_game
    if not slug:
        return []
    matches = []
    for name in _profiles_for_game(game.name):
        try:
            path = profile_path(game, name)
        except ValueError:
            continue
        if groups.is_group(path):
            continue
        settings = read_profile_settings(path, None)
        found_slug, found_domain, found_rev = parse_collection_url(settings.get("collection_url", ""))
        if slug.startswith("import_"):
            raw_slug = settings.get("collection_url", "").partition("/collections/")[2].split("/revisions/")[0]
            if raw_slug == slug:
                found_slug = slug
        identity = settings.get("collection_identity")
        if isinstance(identity, dict):
            found_slug, found_domain, found_rev = identity.get("slug"), identity.get("domain"), identity.get("revision")
        if found_rev is None:
            from Utils.profiles.state import read_collection_revision
            found_rev = read_collection_revision(path)
        if (found_slug == slug and (found_domain or "").casefold() == domain.casefold()
                and str(found_rev) == str(revision)):
            matches.append(name)
    def incomplete(name):
        from Utils.profiles.state import read_collection_install_paused
        path = profile_path(game, name)
        report = read_profile_settings(path, None).get("collection_install_report") or {}
        return bool(read_collection_install_paused(path) or report.get("missing_required")
                    or report.get("failed_stages") or pending_group(path))
    return sorted(matches, key=lambda name: not incomplete(name))


def pending_group(profile_dir: Path) -> CollectionInstallOptions | None:
    value = read_profile_settings(profile_dir, None).get("pending_collection_group")
    if isinstance(value, dict):
        return CollectionInstallOptions.from_dict(value)
    return None


def save_pending_group(profile_dir: Path, options: CollectionInstallOptions) -> None:
    merge_profile_settings(profile_dir, {"pending_collection_group": options.to_dict()})


def check_target(game, options: CollectionInstallOptions, member: str = "") -> Path:
    if not getattr(game, "profile_groups_supported", True):
        raise ValueError("Profile groups are not supported for this game.")
    target = profile_path(game, options.target)
    if not target.is_dir():
        raise ValueError(f"Profile '{options.target}' no longer exists.")
    if groups.is_group(target) != options.target_is_group:
        raise ValueError("The grouping target has changed. Select it again.")
    if target.name == member:
        raise ValueError("Choose a different profile to group with.")
    if options.target_is_group:
        if groups.profile_is_locked(target):
            raise ValueError("The target group is locked.")
        if game.get_deploy_active() and game.get_last_deployed_profile() == target.name:
            raise ValueError("Restore the deployed group before adding a collection.")
    else:
        groups._validate_member(game, target.parent, target.name)
        name = options.group_name
        group_dir = profile_path(game, name)
        if group_dir.exists():
            if not (groups.is_group(group_dir)
                    and groups.get_members(group_dir) == [member, target.name]):
                raise ValueError(f"A profile or group named '{name}' already exists.")
    return target


def profile_ready(profile_dir: Path, manifest: dict | None = None) -> bool:
    import json
    from Nexus.nexus_meta import read_meta
    from Utils.profiles.state import read_collection_optional_skipped, read_collection_install_paused
    settings = read_profile_settings(profile_dir, None)
    if read_collection_install_paused(profile_dir):
        return False
    report = settings.get("collection_install_report")
    if isinstance(report, dict) and (report.get("missing_required") or report.get("failed_stages")):
        return False
    if isinstance(report, dict) and report.get("verified"):
        return all((profile_dir / "mods" / name).is_dir()
                   for name in report.get("required_folders", []))
    try:
        if manifest is None:
            manifest = json.loads((profile_dir / "collection.json").read_text(encoding="utf-8"))
        staged = {}
        for folder in (profile_dir / "mods").iterdir():
            if folder.is_dir():
                meta = read_meta(folder / "meta.ini")
                if meta.file_id:
                    staged[int(meta.file_id)] = folder
        skipped = read_collection_optional_skipped(profile_dir)
        for mod in manifest.get("mods", []):
            src = mod.get("source") or {}
            fid = int(src.get("fileId") or 0)
            if fid and not (mod.get("optional") and fid in skipped) and fid not in staged:
                return False
        from Utils.collections.install import required_bundle_folders
        _found, missing = required_bundle_folders(profile_dir / "mods", manifest)
        if missing:
            return False
        return bool(manifest)
    except (OSError, ValueError, TypeError):
        return False


class GroupSnapshot:
    def __init__(self, path: Path):
        self.path = path
        self.files = {p: p.read_bytes() for p in path.iterdir()
                      if p.is_file() and not p.is_symlink()
                      and p.suffix in (".json", ".txt", ".yaml", ".ini")}
        mods = path / "mods"
        self.links = {p.name: os.readlink(p) for p in mods.iterdir() if p.is_symlink()}
        self.runtime = {p for directory in ("overwrite", "Root_Folder", "ini files")
                        for p in (path / directory).rglob("*")}

    def restore(self, game, log) -> None:
        for p in self.path.iterdir():
            if p.is_file() and not p.is_symlink() and p.suffix in (".json", ".txt", ".yaml", ".ini") and p not in self.files:
                p.unlink()
        for path, data in self.files.items():
            write_atomic(path, data)
        mods = self.path / "mods"
        for p in mods.iterdir():
            if p.is_symlink() and (p.name not in self.links or os.readlink(p) != self.links[p.name]):
                p.unlink()
        for name, target in self.links.items():
            if not (mods / name).is_symlink():
                (mods / name).symlink_to(target)
        for directory in ("overwrite", "Root_Folder", "ini files"):
            for p in sorted((self.path / directory).rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if p not in self.runtime:
                    if p.is_dir() and not p.is_symlink():
                        p.rmdir()
                    else:
                        p.unlink()
        from Utils.filegraph.service import FileGraphService
        try:
            FileGraphService.open_library(game, self.path, log_fn=log).rebuild(self.path)
        except Exception as exc:
            log(f"Profile Group: refresh needed after rollback: {exc}")


def _verify_group(game, group_dir: Path) -> None:
    from Utils.filegraph.service import FileGraphService
    from Utils.mods.modlist import read_modlist
    library = FileGraphService.open_library(game, group_dir)
    library.ensure_ready(group_dir)
    catalogued = {name.lower() for name in library.manifest_fingerprints()}
    for entry in read_modlist(group_dir / "modlist.txt"):
        if not entry.is_separator and (not (group_dir / "mods" / entry.name).is_dir()
                                      or entry.name.lower() not in catalogued):
            raise ValueError(f"The group could not prepare '{entry.name}'. Refresh its member profile and retry.")
    library.open_profile(group_dir).reconcile(operation_hint={"kind": "collection_group"})


def finalize_group(game, member_dir: Path, options: CollectionInstallOptions, *,
                   ini_source=None, overwrite_excluded=None, log_fn=None) -> Path:
    from Utils.deployment.locking import game_mutation_lock
    log = log_fn or (lambda _message: None)
    with game_mutation_lock(game):
        target = check_target(game, options, member_dir.name)
        if not profile_ready(member_dir):
            raise ValueError("Repair the collection installation before grouping it.")
        groups._validate_member(game, target.parent, member_dir.name)
        if options.target_is_group:
            with groups.group_build_lock(target):
                if member_dir.name not in groups.get_members(target):
                    snapshot = GroupSnapshot(target)
                    try:
                        groups.add_member(game, target, member_dir.name, priority=0, log_fn=log)
                        _verify_group(game, target)
                    except Exception:
                        snapshot.restore(game, log)
                        raise
            result = target
        else:
            result = profile_path(game, options.group_name)
            if not result.exists():
                result = groups.create_group(
                    game, options.group_name, [member_dir.name, target.name],
                    ini_source=ini_source, overwrite_excluded=overwrite_excluded, log_fn=log)
                try:
                    _verify_group(game, result)
                except Exception:
                    import shutil
                    shutil.rmtree(result)
                    raise
        merge_profile_settings(member_dir, {"pending_collection_group": None})
        return result
