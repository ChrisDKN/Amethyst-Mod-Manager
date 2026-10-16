from __future__ import annotations

import copy
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .jobs import JobCancelled, JobError


class CachedSnapshot:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.state = snapshot.conflict_state()
        self.providers_cache = OrderedDict()

    def __getattr__(self, name):
        return getattr(self.snapshot, name)

    def conflict_state(self):
        return self.state

    def providers(self, target, path, kinds=(), namespace="normal"):
        key = (target, path, tuple(kinds), namespace)
        if key not in self.providers_cache:
            self.providers_cache[key] = self.snapshot.providers(target, path, kinds, namespace)
            if len(self.providers_cache) > 65536:
                self.providers_cache.popitem(last=False)
        return self.providers_cache[key]


@contextmanager
def profile_snapshot(game, profile, log):
    from Utils.filegraph.service import FileGraphService
    from Utils.mods.modlist import read_modlist

    if not profile.is_dir() or profile.is_symlink():
        raise JobError("profile_not_found", "The selected profile is no longer available.", {"profile": profile.name})
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    selected.load_paths(read_only=True)
    entries = [entry for entry in read_modlist(profile / "modlist.txt") if not entry.is_separator]
    library = FileGraphService.open_library(selected, profile, log_fn=log)
    library.ensure_ready(profile)
    session = library.open_profile(profile)
    session.ensure_reconciled()
    yield selected, entries, CachedSnapshot(session.snapshot())


def snapshot_revision(snapshot):
    return f"{snapshot.inventory_generation}:{snapshot.generation}"


def _selection(entries, mods):
    names = [entry.name for entry in entries]
    chosen = list(names) if mods is None else list(mods)
    for mod in chosen:
        count = names.count(mod)
        if count != 1:
            raise JobError("ambiguous_mod" if count > 1 else "mod_not_found",
                           f"Mod '{mod}' does not uniquely match a profile entry.", {"mod": mod, "matches": count})
    return chosen


def conflict_rows(game, snapshot, mod):
    from Utils.games.conflict_blacklist import effective_rules
    from Utils.games.registry import game_data_subpath
    from Utils.ui.conflicts import compute_mod_conflicts

    return compute_mod_conflicts(mod, snapshot=snapshot, data_prefix=game_data_subpath(game),
                                 ignore_rules=effective_rules(game), structured=True)


def inspection(game, profile, snapshot, mod, section, offset, limit):
    from Utils.ui.conflicts import BSA_ROW_RE

    wins, losses, uncontested, archive_only = conflict_rows(game, snapshot, mod)
    sections = {"wins": wins, "losses": losses, "uncontested": uncontested}
    rows = sections[section]
    page = rows[offset:offset + limit]
    if section == "wins":
        items = [{"path": path, "mods_beaten": partners,
                  "archive_member": bool(BSA_ROW_RE.match(path)),
                  "archive_related": path in archive_only or bool(BSA_ROW_RE.match(path))}
                 for path, partners in page]
    elif section == "losses":
        items = [{"path": path, "winner": winner, "archive_member": bool(BSA_ROW_RE.match(path))}
                 for path, winner in page]
    else:
        items = [{"path": path, "archive_member": bool(BSA_ROW_RE.match(path))} for path in page]
    return {"ok": True, "profile": profile.name, "mod": mod,
            "revision": snapshot_revision(snapshot), "filegraph_generation": snapshot.generation,
            "summary": asdict(snapshot.conflict_summary(mod)),
            "counts": {key: len(value) for key, value in sections.items()},
            "section": section, "offset": offset, "limit": limit,
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
            "rows": items}


def overview(game, entries, snapshot, mods=None, progress=lambda *_: None, cancel=None):
    chosen = _selection(entries, mods)
    all_names = {entry.name for entry in entries}
    selected_names = set(chosen)
    relations = {}
    items = []
    for index, mod in enumerate(chosen):
        if cancel is not None and cancel.is_set():
            raise JobCancelled("Conflict inspection cancelled.")
        wins, losses, uncontested, _archives = conflict_rows(game, snapshot, mod)
        beaten = set()
        paths_by_partner = {}
        for path, partners in wins:
            for loser in partners:
                if loser in all_names:
                    beaten.add(loser)
                    paths_by_partner.setdefault(loser, set()).add(path)
        for loser, paths in paths_by_partner.items():
            relations[(mod, loser)] = len(paths)
        losing_to = {winner for _path, winner in losses if winner in all_names}
        paths_by_winner = {}
        for path, winner in losses:
            if winner in all_names and winner not in selected_names:
                paths_by_winner.setdefault(winner, set()).add(path)
        for winner, paths in paths_by_winner.items():
            relations[(winner, mod)] = len(paths)
        items.append({"mod": mod, "summary": asdict(snapshot.conflict_summary(mod)),
                      "counts": {"wins": len(wins), "losses": len(losses), "uncontested": len(uncontested)},
                      "overrides": sorted(beaten), "overridden_by": sorted(losing_to)})
        progress(index + 1, len(chosen), "Reading profile conflicts")
    return items, [{"winner": winner, "loser": loser, "file_count": count}
                   for (winner, loser), count in sorted(relations.items())]


def batch_runner(game, profile, mods, section, offset, limit, revision, cancel, *, summary=False):
    def inspect(log, progress):
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "conflicts.inspect", owner="headless:conflicts"):
            with profile_snapshot(game, profile, log) as (selected, entries, snapshot):
                current = snapshot_revision(snapshot)
                if revision is not None and revision != current:
                    raise JobError("stale_conflicts", "The conflict snapshot changed; start paging again.", {"revision": current})
                chosen = _selection(entries, mods)
                if summary:
                    items, relationships = overview(selected, entries, snapshot, chosen, progress, cancel)
                    page = items[offset:offset + limit]
                    visible = {item["mod"] for item in page}
                    relationships = [row for row in relationships if row["winner"] in visible or row["loser"] in visible]
                    return {"ok": True, "profile": profile.name, "revision": current,
                            "order": "highest_priority_first", "mods": page, "relationships": relationships,
                            "total": len(items), "offset": offset, "limit": limit,
                            "next_offset": offset + len(page) if offset + len(page) < len(items) else None}
                items = []
                for index, mod in enumerate(chosen):
                    if cancel is not None and cancel.is_set():
                        raise JobCancelled("Conflict inspection cancelled.", {"items": items})
                    items.append(inspection(selected, profile, snapshot, mod, section, offset, limit))
                    progress(index + 1, len(chosen), "Reading mod conflicts")
                return {"ok": True, "profile": profile.name, "revision": current, "items": items}

    def run(log, progress):
        try:
            return inspect(log, progress)
        except (JobError, JobCancelled):
            raise
        except Exception as exc:
            raise JobError("conflicts_unavailable", "Could not resolve profile conflicts.",
                           {"profile": profile.name, "reason": str(exc)}) from exc
    return run


def inspect_runner(game, profile_dir, mod_name, section, offset, limit, revision, owner):
    batch = batch_runner(game, profile_dir, [mod_name], section, offset, limit, revision, None)
    def run(log, progress):
        return batch(log, progress)["items"][0]
    return run
