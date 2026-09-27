from __future__ import annotations

import configparser
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from .jobs import JobError


class RequirementState:
    def __init__(self, game, profile, raw, state, entries):
        from Nexus.nexus_meta import normalise_game_domain, read_meta
        from Nexus.nexus_requirements import RequirementIndex
        from Utils.mods.metadata import meta_file_lock
        from Utils.profiles import state as profile_state

        self.entries = [entry for entry in entries
                        if not entry.is_separator and not entry.is_group_header]
        settings = profile_state.read_profile_settings(profile, state)
        self.staging = (profile / "mods" if settings.get("profile_specific_mods", False)
                        else Path(game.get_mod_staging_path()))
        self.domain = normalise_game_domain(getattr(game, "nexus_game_domain", "") or "")
        self.ignored = profile_state.read_ignored_missing_requirements(profile, state)
        self.metas = {}
        self.metadata_errors = {}
        for entry in self.entries:
            if (Path(entry.name).name != entry.name or entry.name in (".", "..")
                    or any(char in entry.name for char in "\x00\r\n")):
                continue
            path = self.staging / entry.name / "meta.ini"
            if path.is_file():
                with meta_file_lock(path):
                    try:
                        self.metas[entry.name] = read_meta(path)
                    except (OSError, ValueError, configparser.Error) as exc:
                        self.metadata_errors[entry.name] = type(exc).__name__
        self.index = RequirementIndex((entry.name for entry in self.entries if entry.enabled), self.domain)
        self.index.refresh(self.metas)
        self.enabled = {entry.name for entry in self.entries if entry.enabled}
        self.order = {entry.name: rank for rank, entry in enumerate(self.entries)}
        self.flagged = self.index.flagged(self.ignored)
        self.rows = {entry.name: self._row(entry.name) for entry in self.entries}
        payload = json.dumps({"mods": list(self.rows.values()), "ignored": sorted(self.ignored)},
                             sort_keys=True, ensure_ascii=True).encode("ascii")
        self.revision = hashlib.sha256(raw + b"\0" + payload).hexdigest()

    def _row(self, name):
        from Nexus.nexus_meta import normalise_game_domain, parse_req_pairs

        meta = self.metas.get(name)
        domain = (normalise_game_domain(meta.game_domain) or self.domain) if meta else self.domain
        full = meta.nexus_requirements if meta else ""
        legacy = meta.missing_requirements if meta else ""
        effective = []
        for mid, (label, identities) in self.index.requirements.get(name, {}).items():
            candidates = {provider for identity in identities
                          for provider in self.index.installed.get(identity, ())}
            providers = [{"name": provider, "enabled": provider in self.enabled,
                          "domain": self.index.identities[provider][0],
                          "mod_id": self.index.identities[provider][1]}
                         for provider in sorted(candidates, key=self.order.__getitem__)]
            status = ("enabled" if any(provider["enabled"] for provider in providers)
                      else "disabled" if providers else "missing")
            effective.append({"domain": domain, "mod_id": mid, "name": label,
                              "status": status, "providers": providers,
                              "ignored": mid in self.index.ignored.get(name, ())})
        identity = self.index.identities.get(name)
        owners = self.index.dependents.get(identity, ()) if identity else ()
        return {"name": name, "enabled": name in self.enabled, "domain": domain,
                "mod_id": meta.mod_id if meta else 0,
                "metadata_available": meta is not None,
                "metadata_error": self.metadata_errors.get(name),
                "graph_source": "full" if full else "legacy" if legacy else "none",
                "all": [{"mod_id": mid, "name": label} for mid, label in parse_req_pairs(full or legacy)],
                "missing": [{"mod_id": mid, "name": label}
                            for mid, label in self.index.missing.get(name, ())],
                "effective": effective,
                "required_by": [{"name": owner, "enabled": owner in self.enabled}
                                for owner in sorted(owners, key=self.order.__getitem__)],
                "ignored": name in self.ignored,
                "ignored_requirement_ids": sorted(self.index.ignored.get(name, ())),
                "has_missing": name in self.flagged}

    def summary(self, name):
        row = self.rows[name]
        return {key: row[key] for key in ("all", "missing", "graph_source", "metadata_available", "metadata_error", "ignored",
                                         "ignored_requirement_ids", "has_missing")}

    def inventory(self, profile, mods=None):
        names = list(self.rows) if mods is None else mods
        for name in names:
            count = sum(entry.name == name for entry in self.entries)
            if count != 1:
                raise JobError("mod_not_found" if not count else "ambiguous_mod",
                               "A requested mod is not unique in the profile.",
                               {"mod": name, "matches": count})
        return {"profile": profile.name, "revision": self.revision,
                "source": "cached_nexus_metadata", "satisfaction": "enabled_profile_mods",
                "mods": [self.rows[name] for name in names],
                "counts": {"mods": len(names),
                           "missing": sum(bool(self.rows[name]["missing"]) for name in names),
                           "flagged": sum(self.rows[name]["has_missing"] for name in names)}}

    def missing_report(self, names, details=()):
        from Nexus.nexus_meta import normalise_game_domain

        originals = {(row["mod"], normalise_game_domain(item["domain"]) or self.rows[row["mod"]]["domain"],
                      item["mod_id"]): item
                     for row in details if row["mod"] in self.rows for item in row["missing"]}
        rows = []
        for name in sorted(names, key=self.order.__getitem__):
            row = self.rows[name]
            if row["missing"]:
                rows.append({"mod": name, "mod_id": row["mod_id"],
                             "ignored": row["ignored"], "has_missing": row["has_missing"],
                             "missing": [{"domain": row["domain"], **item,
                                          "url": f"https://www.nexusmods.com/{row['domain']}/mods/{item['mod_id']}"
                                          if row["domain"] else "", "notes": ""}
                                         for item in row["missing"]]})
                for item in rows[-1]["missing"]:
                    original = originals.get((name, item["domain"], item["mod_id"]), {})
                    item["url"] = original.get("url") or item["url"]
                    item["notes"] = original.get("notes") or ""
        return rows


def read_requirements(game, profile: Path):
    from Utils.mods.groups import group_edit_state

    with group_edit_state(profile / "modlist.txt") as (raw, state, entries, groups):
        return RequirementState(game, profile, raw, state, entries)


def set_ignored(game, profile: Path, mod: str, revision: str, options, owner, log, progress):
    from Nexus.nexus_meta import parse_req_pairs, read_meta, set_meta_keys
    from Utils.atomic_write import write_atomic
    from Utils.mods.groups import group_edit_state
    from Utils.mods.metadata import meta_file_lock
    from Utils.operations import acquire_game_operation
    from Utils.profiles import state as profile_state

    with acquire_game_operation(game, "mods.requirements.ignore.set", owner=owner):
        if not profile.is_dir() or profile.is_symlink():
            raise JobError("profile_not_found", "The selected profile is unavailable.")
        progress(0, 1, "Saving requirement ignores")
        with group_edit_state(profile / "modlist.txt") as (raw, state, entries, groups):
            current = RequirementState(game, profile, raw, state, entries)
            current.inventory(profile, [mod])
            path = current.staging / mod / "meta.ini"
            lock = meta_file_lock(path) if "requirement_ids" in options else nullcontext()
            with lock:
                if "requirement_ids" in options:
                    current = RequirementState(game, profile, raw, state, entries)
                if current.revision != revision:
                    raise JobError("stale_mod_requirements", "The requirement state changed; read it again.",
                                   {"revision": current.revision})
                meta = current.metas.get(mod)
                value = None
                if "requirement_ids" in options:
                    if meta is None:
                        raise JobError("mod_metadata_unavailable", "The mod has no saved Nexus metadata.",
                                       {"mod": mod})
                    known = current.index.requirements.get(mod, {})
                    existing = current.index.ignored.get(mod, set())
                    unknown = set(options["requirement_ids"]) - known.keys() - existing
                    if unknown:
                        raise JobError("requirement_not_found", "An ignored ID is not a known requirement of this mod.",
                                       {"mod": mod, "requirement_ids": sorted(unknown)})
                    old_labels = dict(parse_req_pairs(meta.ignored_requirements))
                    value = ";".join(f"{mid}:{(known[mid][0] if mid in known else old_labels.get(mid, '')).replace(';', ',')}"
                                     for mid in options["requirement_ids"])
                ignored = set(current.ignored)
                if "ignored" in options:
                    if options["ignored"]:
                        ignored.add(mod)
                    else:
                        ignored.discard(mod)
                profile_changed = ignored != current.ignored
                metadata_changed = value is not None and value != meta.ignored_requirements
                updated_state = dict(state)
                if profile_changed:
                    if ignored:
                        updated_state["ignored_missing_requirements"] = sorted(ignored)
                    else:
                        updated_state.pop("ignored_missing_requirements", None)
                old_metadata = path.read_bytes() if metadata_changed else None
                if metadata_changed:
                    set_meta_keys(path, {"ignoredRequirements": value})
                    if read_meta(path).ignored_requirements != value:
                        raise JobError("requirement_ignore_save_failed", "The requirement ignores could not be saved.",
                                       {"mod": mod})
                try:
                    if profile_changed:
                        profile_state.write_profile_state(profile, updated_state)
                except BaseException:
                    if metadata_changed:
                        write_atomic(path, old_metadata)
                    raise
                updated = RequirementState(game, profile, raw, updated_state, entries)
                result = updated.inventory(profile, [mod])
        progress(1, 1, "Requirement ignores saved")
        changed = profile_changed or metadata_changed
        log("Saved requirement ignores." if changed else "Requirement ignores already matched the request.")
        return {**result, "ok": True, "changed": changed}
