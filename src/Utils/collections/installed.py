from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.mods.modlist import read_modlist, write_modlist, modlist_lock
from Utils.profiles.state import read_profile_settings, read_collection_revision
from .ownership import read_ownership, write_ownership

INSTALLED_COLLECTIONS_DIR = "installed_collections"


def _safe_filename(slug: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in slug) or "collection"


def save_record(record: dict) -> None:
    path = Path(record["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {key: val for key, val in record.items() if key != "path"}
    write_atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _read_record(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if (not isinstance(value, dict) or not value.get("slug")
            or not isinstance(value.get("members", {}), dict)
            or not isinstance(value.get("manifest", {}), dict)
            or not isinstance(value.get("card", {}), dict)):
        raise ValueError("Invalid collection record")
    value["path"] = path
    return value


def begin_collection(profile_dir: Path, *, slug: str, revision, card: dict,
                     manifest: dict, appended: bool) -> dict:
    path = Path(profile_dir) / INSTALLED_COLLECTIONS_DIR / (
        f"{_safe_filename(slug)}.json" if appended else "_primary.json")
    record = _read_record(path) if path.exists() else {}
    if record and record["slug"] != slug:
        raise ValueError("Collection identity changed")
    if record and record.get("revision") != revision and not record.get("previous"):
        record["previous"] = {"revision": record.get("revision"),
                              "manifest": record.get("manifest") or {}}
    record.update(version=2, slug=slug, revision=revision,
                  install_id=record.get("install_id") or uuid.uuid4().hex,
                  kind="appended" if appended else "standalone",
                  status="installing", card={**(record.get("card") or {}), **(card or {})},
                  manifest=manifest, path=path,
                  saved=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    record.setdefault("members", {})
    save_record(record)
    return record


def record_appended_collection(profile_dir: Path, *, slug: str, revision,
                               card: dict, manifest: dict, log_fn=None) -> Path:
    return begin_collection(profile_dir, slug=slug, revision=revision,
                            card=card, manifest=manifest, appended=True)["path"]


def list_appended_collections(profile_dir, log_fn=None) -> list[dict]:
    log = log_fn or (lambda _m: None)
    if not profile_dir:
        return []
    out = []
    for path in (Path(profile_dir) / INSTALLED_COLLECTIONS_DIR).glob("*.json"):
        try:
            value = _read_record(path)
            if value.get("kind") != "standalone":
                out.append(value)
        except (OSError, ValueError) as exc:
            log(f"Could not read collection record {path}: {exc}")
    return sorted(out, key=lambda value: collection_title(value).casefold())


def collection_title(record: dict, fallback="") -> str:
    card = record.get("card") or {}
    info = (record.get("manifest") or {}).get("info") or {}
    return str(card.get("name") or info.get("name") or fallback or record.get("slug") or "")


def primary_collection(profile_dir: Path, settings=None) -> dict | None:
    from .manifest import parse_collection_url
    profile_dir = Path(profile_dir)
    settings = read_profile_settings(profile_dir, None) if settings is None else settings
    if settings.get("is_group"):
        return None
    identity = settings.get("collection_identity") or {}
    if not isinstance(identity, dict):
        identity = {}
    slug, domain, revision = parse_collection_url(settings.get("collection_url") or "")
    slug = identity.get("slug") or slug
    domain = identity.get("domain") or domain
    revision = read_collection_revision(profile_dir) or identity.get("revision") or revision
    path = profile_dir / INSTALLED_COLLECTIONS_DIR / "_primary.json"
    try:
        record = _read_record(path)
        if slug and record["slug"] != slug:
            raise ValueError("Collection identity changed")
    except (OSError, ValueError):
        if not slug:
            return None
        record = {"slug": slug, "kind": "standalone", "members": {}}
    record.setdefault("revision", revision)
    record["card"] = {**(record.get("card") or {}), **(settings.get("collection_card") or {})}
    record["domain"] = domain
    if not record.get("manifest"):
        try:
            record["manifest"] = json.loads((profile_dir / "collection.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record["manifest"] = {}
    if not record.get("status"):
        report = settings.get("collection_install_report") or {}
        from Utils.profiles.state import read_collection_install_paused
        record["status"] = ("paused" if read_collection_install_paused(profile_dir)
                            else "complete" if report.get("verified") and not (
                                report.get("failed_stages") or report.get("missing_required"))
                            else "incomplete" if report else "unknown")
    return record


def collection_profile_label(profile_dir: Path, settings=None) -> str:
    settings = read_profile_settings(profile_dir, None) if settings is None else settings
    name = profile_dir.name
    if settings.get("collection_custom_name"):
        return name
    title = str((settings.get("collection_card") or {}).get("name") or "")
    if not title:
        try:
            manifest = json.loads((profile_dir / "collection.json").read_text(encoding="utf-8"))
            title = str((manifest.get("info") or {}).get("name") or "")
        except (OSError, ValueError):
            return name
    if not title:
        return name
    base = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:60] or "Collection"
    generated = settings.get("collection_generated_profile_name")
    if generated == name or re.fullmatch(re.escape(base) + r"(?:_Rev\d+)?(?:_\d+)?", name):
        return title
    revision = read_collection_revision(profile_dir)
    old = re.sub(r"[^\w\s-]", "", f"{title}_Rev{revision}").strip().replace(" ", "_")[:60]
    return title if revision is not None and re.fullmatch(re.escape(old) + r"(?:_\d+)?", name) else name


@dataclass(frozen=True)
class InstalledCollection:
    game_name: str
    profile_dir: Path
    record: dict
    locked: bool = False
    appended_collections: tuple[InstalledCollection, ...] = ()

    @property
    def title(self):
        return collection_title(self.record, self.profile_dir.name)

    @property
    def appended(self):
        return self.record.get("kind", "appended") == "appended"


def installed_collections(log_fn=None) -> list[InstalledCollection]:
    from Utils.games.registry import _GAMES
    from Utils.profiles.groups import profile_is_locked
    log = log_fn or (lambda _m: None)
    records, seen = [], set()
    for game_name, game in _GAMES.items():
        if not game.is_configured():
            continue
        root = Path(game.get_profile_root()) / "profiles"
        if root.resolve() in seen or not root.is_dir():
            continue
        seen.add(root.resolve())
        for profile in sorted(root.iterdir()):
            if not profile.is_dir() or profile.is_symlink():
                continue
            try:
                primary = primary_collection(profile)
                locked = profile_is_locked(profile)
                appended = tuple(InstalledCollection(game_name, profile, value, locked)
                                 for value in list_appended_collections(profile, log))
                if primary:
                    records.append(InstalledCollection(game_name, profile, primary, locked, appended))
                elif appended:
                    records.append(InstalledCollection(game_name, profile,
                        {"kind": "host", "card": {"name": profile.name}}, locked, appended))
            except (OSError, ValueError, TypeError) as exc:
                log(f"Could not scan collections in {profile}: {exc}")
    return sorted(records, key=lambda item: (item.game_name.casefold(), item.title.casefold(),
                                             str(item.profile_dir), str(item.record.get("revision"))))


def toggle_appended_collection_mods(game, profile_dir: Path, record: dict,
                                    *, log_fn=None) -> tuple[bool, list[str], int]:
    from Utils.deployment.locking import game_mutation_lock
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.modlist import ModEntry
    from Utils.plugins.sync import sync_plugins_for_mods
    from Utils.profiles.groups import is_group, member_of_groups, profile_is_locked

    profile_dir = Path(profile_dir)
    expected = Path(game.get_profile_root()) / "profiles" / profile_dir.name
    if profile_dir.is_symlink() or profile_dir.resolve() != expected.resolve() or not profile_dir.is_dir():
        raise ValueError("The collection profile is no longer available")
    with game_mutation_lock(game):
        if profile_is_locked(profile_dir) or is_group(profile_dir):
            raise ValueError("Unlock the collection profile before changing its mods")
        if game.get_deploy_active():
            affected = {profile_dir.name, *member_of_groups(game, profile_dir.name)}
            if game.get_last_deployed_profile() in affected:
                raise ValueError("Restore the deployed profile or group before changing this collection")
        path = Path(record.get("path") or "")
        if (not record.get("path") or path.is_symlink()
                or path.parent.resolve() != (profile_dir / INSTALLED_COLLECTIONS_DIR).resolve()):
            raise ValueError("The appended collection record changed")
        fresh = _read_record(path)
        install_id = fresh.get("install_id")
        if (fresh.get("kind") != "appended" or not install_id
                or install_id != record.get("install_id") or fresh["slug"] != record.get("slug")):
            raise ValueError("The appended collection installation changed")
        staging = Path(resolve_target_staging(game, profile_dir))
        modlist_path = profile_dir / "modlist.txt"
        with modlist_lock(modlist_path):
            entries = read_modlist(modlist_path)
            eligible = []
            skipped = 0
            for entry in entries:
                if entry.is_separator or entry.locked or Path(entry.name).name != entry.name:
                    continue
                folder = staging / entry.name
                if not folder.is_dir() or folder.is_symlink():
                    continue
                owner = read_ownership(folder / "meta.ini")
                memberships = set(owner.get("installations", ()))
                if install_id not in memberships:
                    continue
                if not owner.get("introduced") or memberships != {install_id}:
                    skipped += 1
                    continue
                eligible.append(entry)
            if not eligible:
                raise ValueError("No exclusively owned mods are available to toggle; shared and personal mods were left unchanged")
            enable = not all(entry.enabled for entry in eligible)
            names = {entry.name.casefold() for entry in eligible}
            changes = [(entry.name, enable) for entry in eligible if entry.enabled != enable]
            if changes:
                write_modlist(modlist_path, [
                    ModEntry(entry.name, enable, entry.locked, entry.is_separator)
                    if entry.name.casefold() in names else entry for entry in entries])
                previous_disabled = dict(fresh.get("disabled_members") or {})
                if enable:
                    fresh.pop("disabled_members", None)
                else:
                    fresh["disabled_members"] = {
                        **previous_disabled,
                        **{read_ownership(staging / name / "meta.ini")["mod_id"]: name
                           for name, _ in changes}}
                fresh["mods_enabled"] = enable
                try:
                    save_record(fresh)
                except Exception:
                    write_modlist(modlist_path, entries)
                    raise
        if changes:
            try:
                sync_plugins_for_mods(game, profile_dir, staging, changes, log_fn=log_fn)
            except Exception as exc:
                raise RuntimeError(f"Mod states changed, but plugin synchronization failed: {exc}") from exc
        return enable, [name for name, _ in changes], skipped


def _restore_adopted_mod_states(game, profile_dir: Path, staging: Path, record: dict) -> None:
    disabled = set((record.get("disabled_members") or {}))
    primary = primary_collection(profile_dir)
    if not disabled or not primary or not primary.get("install_id"):
        return
    from Utils.mods.modlist import ModEntry
    from Utils.plugins.sync import sync_plugins_for_mods
    modlist_path = profile_dir / "modlist.txt"
    changes = []
    with modlist_lock(modlist_path):
        entries = read_modlist(modlist_path)
        restored = []
        for entry in entries:
            folder = staging / entry.name
            owner = read_ownership(folder / "meta.ini") if not entry.is_separator else {}
            restore = (not entry.is_separator and not entry.enabled and not entry.locked
                       and owner.get("mod_id") in disabled
                       and primary["install_id"] in owner.get("installations", ())
                       and _manifest_match(folder, primary.get("manifest") or {}))
            if restore:
                changes.append((entry.name, True))
                restored.append(ModEntry(entry.name, True, entry.locked, False))
            else:
                restored.append(entry)
        if changes:
            write_modlist(modlist_path, restored)
    if changes:
        sync_plugins_for_mods(game, profile_dir, staging, changes)


@dataclass
class CollectionRemovalPlan:
    delete: list[str] = field(default_factory=list)
    retain: list[str] = field(default_factory=list)
    release: dict[str, str] = field(default_factory=dict)


def _manifest_match(folder: Path, manifest: dict) -> bool:
    from Nexus.nexus_meta import read_meta, normalise_game_domain
    meta = read_meta(folder / "meta.ini")
    domain = normalise_game_domain(meta.game_domain)
    for entry in (manifest or {}).get("mods", []):
        source = entry.get("source") or {}
        try:
            fid = int(source.get("fileId") or 0)
        except (ValueError, TypeError):
            fid = 0
        if fid and fid in (meta.file_id, meta.collection_source_file_id):
            wanted_domain = normalise_game_domain(source.get("domainName") or "")
            if not wanted_domain or not domain or wanted_domain == domain:
                return True
        if str(entry.get("name") or "").casefold() == folder.name.casefold():
            return True
    return False


def plan_collection_removal(game, profile_dir: Path, record: dict,
                            candidates=None) -> CollectionRemovalPlan:
    from Utils.mods.copy import resolve_target_staging
    profile_dir = Path(profile_dir)
    staging = Path(resolve_target_staging(game, profile_dir))
    install_id = record.get("install_id")
    selected = None if candidates is None else {name.casefold() for name in candidates}
    result = CollectionRemovalPlan()
    other_records = list_appended_collections(profile_dir)
    primary = primary_collection(profile_dir)
    if primary:
        other_records.append(primary)
    other_records = [value for value in other_records
                     if not install_id or value.get("install_id") != install_id]
    protected_paths = set()
    for profile in profile_dir.parent.iterdir():
        if profile.is_dir() and profile.resolve() != profile_dir.resolve():
            other_staging = Path(resolve_target_staging(game, profile))
            protected_paths.update((other_staging / entry.name).resolve()
                                   for entry in read_modlist(profile / "modlist.txt") if not entry.is_separator)
    listed = {entry.name for entry in read_modlist(profile_dir / "modlist.txt") if not entry.is_separator}
    if not staging.is_dir():
        return result
    for folder in sorted(staging.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if selected is not None and folder.name.casefold() not in selected:
            continue
        ownership = read_ownership(folder / "meta.ini")
        memberships = set(ownership.get("installations", []))
        member = bool(install_id and install_id in memberships)
        if not member:
            from Nexus.nexus_meta import read_meta
            legacy_tag = read_meta(folder / "meta.ini").from_collection == record.get("slug")
            if folder.name in listed and (legacy_tag or _manifest_match(folder, record.get("manifest") or {})
                                          or folder.name in (record.get("members") or {}).values()):
                result.retain.append(folder.name)
            continue
        result.release[folder.name] = ownership["mod_id"]
        shared = bool(memberships - {install_id}) or folder.resolve() in protected_paths
        if not shared:
            shared = any(
                (ownership["mod_id"] in (value.get("members") or {})
                 if value.get("version") == 2 and value.get("status") == "complete"
                 and value.get("install_id")
                 else _manifest_match(folder, value.get("manifest") or {}))
                for value in other_records)
        if ownership["introduced"] and not shared and not folder.is_symlink():
            result.delete.append(folder.name)
        else:
            result.retain.append(folder.name)
    for name, mod_id in (record.get("pending_delete") or {}).items():
        if Path(name).name == name and (selected is None or name.casefold() in selected):
            folder = staging / name
            if not folder.exists() and name not in result.delete:
                result.delete.append(name)
                result.release[name] = mod_id
    return result


def resolve_owned_mod_names(game, profile_dir: Path, record: dict) -> list[str]:
    return plan_collection_removal(game, profile_dir, record).delete


def release_profile_collection_memberships(game, profile_dir: Path) -> None:
    from Utils.mods.copy import resolve_target_staging
    records = list_appended_collections(profile_dir)
    primary = primary_collection(profile_dir)
    if primary:
        records.append(primary)
    ids = {record["install_id"] for record in records if record.get("install_id")}
    staging = Path(resolve_target_staging(game, profile_dir))
    if not ids or not staging.is_dir():
        return
    for folder in staging.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        meta = folder / "meta.ini"
        value = read_ownership(meta)
        if ids.intersection(value.get("installations", [])):
            value["installations"] = [item for item in value["installations"] if item not in ids]
            write_ownership(meta, value)


def remove_collection_members(game, profile_dir: Path, record: dict, *, candidates=None,
                              delete_record=False, log_fn=None) -> CollectionRemovalPlan:
    from Utils.deployment.locking import game_mutation_lock
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.remove import remove_mods
    from Utils.profiles.groups import profile_is_locked, is_group, member_of_groups
    log = log_fn or (lambda _m: None)
    profile_dir = Path(profile_dir)
    expected = Path(game.get_profile_root()) / "profiles" / profile_dir.name
    if profile_dir.is_symlink() or profile_dir.resolve() != expected.resolve() or not profile_dir.is_dir():
        raise ValueError("The collection profile is no longer available")
    with game_mutation_lock(game):
        if profile_is_locked(profile_dir) or is_group(profile_dir):
            raise ValueError("Unlock the collection profile before removing mods")
        if game.get_deploy_active():
            affected = {profile_dir.name, *member_of_groups(game, profile_dir.name)}
            if game.get_last_deployed_profile() in affected:
                raise ValueError("Restore the deployed profile or group before removing this collection")
        if record.get("path"):
            path = Path(record["path"])
            if path.is_symlink() or path.parent.resolve() != (profile_dir / INSTALLED_COLLECTIONS_DIR).resolve():
                raise ValueError("The collection record changed")
            fresh = _read_record(path)
            if fresh.get("install_id") != record.get("install_id") or fresh["slug"] != record["slug"]:
                raise ValueError("The collection installation changed")
            record = fresh
        plan = plan_collection_removal(game, profile_dir, record, candidates)
        staging = Path(resolve_target_staging(game, profile_dir))
        if plan.delete:
            if not record.get("path"):
                raise ValueError("Missing collection ownership record")
            record["pending_delete"] = {name: plan.release[name] for name in plan.delete}
            save_record(record)
            previous = getattr(game, "_active_profile_dir", None)
            try:
                game.set_active_profile_dir(profile_dir)
                game.load_paths()
                remove_mods(game, profile_dir, plan.delete, staging_root=staging, log_fn=log, strict=True)
            finally:
                game.set_active_profile_dir(previous)
                game.load_paths()
            with modlist_lock(profile_dir / "modlist.txt"):
                entries = read_modlist(profile_dir / "modlist.txt")
                removed = {name.casefold() for name in plan.delete}
                write_modlist(profile_dir / "modlist.txt", [entry for entry in entries
                    if entry.is_separator or entry.name.casefold() not in removed])
        for name, mod_id in plan.release.items():
            path = staging / name / "meta.ini"
            if name in plan.delete:
                continue
            value = read_ownership(path)
            if value.get("mod_id") != mod_id:
                raise ValueError(f"Mod changed during removal: {name}")
            value["installations"] = [item for item in value["installations"] if item != record.get("install_id")]
            write_ownership(path, value)
        if delete_record and record.get("path"):
            _restore_adopted_mod_states(game, profile_dir, staging, record)
            Path(record["path"]).unlink()
        elif record.get("path"):
            record.pop("pending_delete", None)
            record["members"] = {key: value for key, value in (record.get("members") or {}).items()
                                 if key not in set(plan.release.values())}
            save_record(record)
        log(f"Collection removed: {len(plan.delete)} mods deleted, {len(plan.retain)} retained")
        return plan


def remove_appended_collection(game, profile_dir: Path, record: dict,
                               mod_names=None, log_fn=None) -> CollectionRemovalPlan:
    return remove_collection_members(game, profile_dir, record, delete_record=True, log_fn=log_fn)
