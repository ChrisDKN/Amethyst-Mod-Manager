"""Preview explicit SkyPatcher NPC outfit assignments and outfit item edits."""

from __future__ import annotations

import configparser
from pathlib import Path

from Utils.assets.resolver import DirCache, normalise, _graph_coordinates
from Utils.npc.body import BodyRecords, _enabled_mods, _first_with_owner

_PREFIX = "skse/plugins/skypatcher"
_KEYS = {
    "npc": {"filterbynpcs", "outfitdefault"},
    "outfit": {"filterbyoutfits", "clear", "formstoadd", "formstoremove"},
}


class _RuntimeOutfits(BodyRecords):
    def key(self, formid):
        # Runtime references already contain the owning plugin and local FormID.
        return formid


def _files(root, cancel):
    root = Path(root)
    dirs = DirCache()
    for name in ("skypatcher.ini", "skypatcher.dll"):
        path = dirs.resolve(root, f"skse/plugins/{name}")
        if path is not None:
            yield f"skse/plugins/{name}", path
    parents = [root]
    for name in ("skse", "plugins", "skypatcher"):
        children = []
        for parent in parents:
            try:
                children.extend(p for p in parent.iterdir()
                                if p.name.lower() == name and p.is_dir())
            except OSError:
                pass
        parents = children
    for parent in parents:
        for kind in _KEYS:
            try:
                branches = [p for p in parent.iterdir()
                            if p.name.lower() == kind and p.is_dir()]
                for branch in branches:
                    for path in branch.rglob("*"):
                        if cancel and cancel():
                            return
                        if path.suffix.lower() == ".ini" and path.is_file():
                            yield normalise(path.relative_to(root).as_posix()), path
            except OSError:
                pass


def _sources(profile_dir, staging, data_dir, game, snapshot, cancel, log):
    sources = {}
    mods = _enabled_mods(profile_dir)
    if data_dir is not None:
        for key, path in _files(data_dir, cancel):
            if staging is not None:
                try:
                    owner = path.resolve().relative_to(Path(staging)).parts[0]
                    if owner not in mods:
                        continue
                except (ValueError, OSError, IndexError):
                    pass
            sources[key] = path
    if snapshot is not None and game is not None:
        try:
            from Utils.filegraph.service import source_path
            prefixes = _graph_coordinates((_PREFIX,), game)
            for winner in snapshot.asset_winner_sources(prefixes):
                if cancel and cancel():
                    return {}
                if winner.namespace == "archive":
                    continue
                key = normalise(winner.legacy_rel)
                if winner.namespace == "root":
                    for prefix in prefixes[1:]:
                        if key.startswith(prefix):
                            key = _PREFIX + key[len(prefix):]
                            break
                if (key in (f"{_PREFIX}.ini", f"{_PREFIX}.dll")
                        or key.startswith(_PREFIX + "/")):
                    sources[key] = source_path(
                        game, winner.mod_name, winner.source_rel)
            return sources
        except Exception as exc:                         # noqa: BLE001
            log(f"SkyPatcher config lookup fell back to mod folders: {exc}")
    if staging is not None:
        for mod in reversed(mods):
            if cancel and cancel():
                return {}
            sources.update(_files(Path(staging) / mod, cancel))
    if game is not None:
        overwrite = getattr(game, "get_effective_overwrite_path", None)
        if callable(overwrite):
            sources.update(_files(overwrite(), cancel))
    return sources


def load_outfits(records, profile_dir, staging, data_dir, *, game=None,
                 snapshot=None, cancel=None, log=None):
    log = log or (lambda _message: None)
    sources = _sources(profile_dir, staging, data_dir, game, snapshot, cancel, log)
    if cancel and cancel():
        return None
    dll = sources.get(f"{_PREFIX}.dll")
    if dll is None or not dll.is_file():
        return None
    settings = configparser.ConfigParser(
        interpolation=None, strict=False, inline_comment_prefixes=(";", "#"))
    path = sources.get(f"{_PREFIX}.ini")
    if path is not None:
        try:
            settings.read_string(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, configparser.Error) as exc:
            log(f"SkyPatcher settings could not be read: {exc}")
            return None
    enabled = {}
    section = next((s for s in settings.sections() if s.lower() == "patcher"),
                   "Patcher")
    for kind in _KEYS:
        try:
            enabled[kind] = settings.getint(
                section, f"iEnable{kind.upper()}Patching", fallback=0)
        except ValueError:
            enabled[kind] = 0
            log(f"SkyPatcher {kind} patching has an invalid enable setting")

    forms, identifiers, plugins = {}, {}, {}
    for record in records:
        plugins[record.name] = record.is_light
        for key, (kind, editor_id) in record.forms.items():
            if key in forms:
                continue
            forms[key] = kind
            if editor_id:
                identifiers.setdefault(editor_id.lower(), key)

    def resolve(value, kinds):
        if "|" in value:
            plugin, raw = value.split("|", 1)
            plugin = plugin.strip().lower()
            if plugin not in plugins:
                return None
            try:
                mask = 0xFFF if plugins[plugin] else 0xFFFFFF
                key = plugin, int(raw.strip(), 16) & mask
            except ValueError:
                return None
        else:
            key = identifiers.get(value.lower())
        return key if forms.get(key) in kinds else None

    patch = _RuntimeOutfits(name="[SkyPatcher]")
    applied = 0
    for relative, path in sorted(sources.items()):
        if cancel and cancel():
            return None
        pieces = relative.split("/")
        if (not relative.endswith(".ini") or len(pieces) < 5
                or pieces[3] not in _KEYS):
            continue
        kind = pieces[3]
        if not enabled[kind]:
            continue
        plugin = path.stem.lower()
        if any(ext in plugin for ext in (".esp", ".esm", ".esl")):
            if plugin not in plugins:
                continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            log(f"SkyPatcher could not read {relative}: {exc}")
            continue
        for number, line in enumerate(lines, 1):
            if cancel and cancel():
                return None
            fields = {}
            line = line.partition(";")[0].strip()
            if not line or line.startswith("["):
                continue
            for item in line.split(":"):
                key, sep, value = item.partition("=")
                if sep:
                    fields.setdefault(key.strip().lower(), value.strip())
            actions = (("outfitdefault",) if kind == "npc" else
                       ("clear", "formstoadd", "formstoremove"))
            if not any(key in fields for key in actions):
                continue
            location = f"{relative}:{number}"
            unknown = fields.keys() - _KEYS[kind]
            if unknown:
                log(f"SkyPatcher skipped {location}: unsupported field(s) "
                    + ", ".join(sorted(unknown)))
                continue
            filter_key = "filterbynpcs" if kind == "npc" else "filterbyoutfits"
            target_kind = b"NPC_" if kind == "npc" else b"OTFT"
            targets = []
            missing = []
            for value in fields.get(filter_key, "").split(","):
                value = value.strip()
                if not value or value.lower() == "none":
                    continue
                target = resolve(value, (target_kind,))
                if target is None:
                    missing.append(value)
                else:
                    targets.append(target)
            if missing:
                log(f"SkyPatcher unresolved {filter_key} at {location}: "
                    + ", ".join(missing))
            if not targets:
                log(f"SkyPatcher skipped {location}: no explicit resolved target")
                continue
            if kind == "npc":
                value = fields.get("outfitdefault", "")
                if value.lower() in ("", "none"):
                    continue
                outfit = resolve(value, (b"OTFT",))
                if outfit is None:
                    log(f"SkyPatcher unresolved outfitDefault at {location}: {value}")
                    continue
                patch.npc_outfit.update((target, outfit) for target in targets)
            else:
                edits = {}
                for field in ("formstoadd", "formstoremove"):
                    edits[field] = []
                    missing = []
                    for value in fields.get(field, "").split(","):
                        value = value.strip()
                        if not value or value.lower() == "none":
                            continue
                        item = resolve(value, (b"ARMO", b"LVLI"))
                        if item is None:
                            missing.append(value)
                        else:
                            edits[field].append(item)
                    if missing:
                        log(f"SkyPatcher unresolved {field} at {location}: "
                            + ", ".join(missing))
                clear = fields.get("clear", "").lower() not in ("", "none")
                if not clear and not any(edits.values()):
                    continue
                for target in targets:
                    if target not in patch.outfit_items:
                        items, owner = _first_with_owner(records, "outfit_items", target)
                        patch.outfit_items[target] = [owner.key(raw) for raw in
                                                     (items or ())] if owner else []
                    items = [] if clear else patch.outfit_items[target]
                    patch.outfit_items[target] = [item for item in items
                                                 if item not in edits["formstoremove"]]
                    patch.outfit_items[target].extend(edits["formstoadd"])
            applied += 1
    if applied:
        log(f"SkyPatcher applied {applied} outfit rule(s): "
            f"{len(patch.npc_outfit)} NPC assignment(s), "
            f"{len(patch.outfit_items)} edited outfit(s)")
        return patch
    return None
