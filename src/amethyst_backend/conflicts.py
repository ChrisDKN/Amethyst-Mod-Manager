from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError


def inspect_runner(
    game: Any, profile_dir: Path, mod_name: str, section: str,
    offset: int, limit: int, revision: str | None,
    owner: str,
) -> Callable:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.games.conflict_blacklist import effective_rules
        from Utils.games.registry import game_data_subpath
        from Utils.mods.modlist import read_modlist
        from Utils.operations import acquire_game_operation
        from Utils.ui.conflicts import BSA_ROW_RE, compute_mod_conflicts
        from Utils.filegraph.service import FileGraphService

        with acquire_game_operation(game, "conflicts.inspect", owner=owner):
            if not profile_dir.is_dir() or profile_dir.is_symlink():
                raise JobError("profile_not_found",
                               "The selected profile is no longer available.",
                               {"profile": profile_dir.name})
            try:
                names = [entry.name for entry in read_modlist(
                    profile_dir / "modlist.txt") if not entry.is_separator]
            except (OSError, ValueError) as exc:
                raise JobError("resource_unavailable",
                               "Could not read the selected profile's mod list.",
                               {"profile": profile_dir.name,
                                "reason": str(exc)}) from exc
            if names.count(mod_name) != 1:
                matches = [name for name in names
                           if name.casefold() == mod_name.casefold()]
                raise JobError(
                    "ambiguous_mod" if names.count(mod_name) > 1 else "mod_not_found",
                    f"Mod '{mod_name}' does not uniquely match a profile entry.",
                    {"mod": mod_name, "matches": matches},
                )

            previous = getattr(game, "_active_profile_dir", None)
            try:
                game.set_active_profile_dir(profile_dir)
                game.load_paths(read_only=True)
                progress(0, 2, "Resolving profile conflicts")
                library = FileGraphService.open_library(
                    game, profile_dir, log_fn=log)
                library.ensure_ready(profile_dir)
                session = library.open_profile(profile_dir)
                session.ensure_reconciled()
                snapshot = session.snapshot()
                current_revision = (f"{snapshot.inventory_generation}:"
                                    f"{snapshot.generation}")
                if revision is not None and revision != current_revision:
                    raise JobError(
                        "stale_conflicts",
                        "The conflict snapshot changed; start paging again.",
                        {"revision": current_revision},
                    )
                progress(1, 2, "Reading mod conflicts")
                wins, losses, uncontested, archive_only = compute_mod_conflicts(
                    mod_name, snapshot=snapshot,
                    data_prefix=game_data_subpath(game),
                    ignore_rules=effective_rules(game), structured=True,
                )
                sections = {
                    "wins": wins, "losses": losses,
                    "uncontested": uncontested,
                }
                rows = sections[section]
                page = rows[offset:offset + limit]
                if section == "wins":
                    items = [{
                        "path": path, "mods_beaten": partners,
                        "archive_member": bool(BSA_ROW_RE.match(path)),
                        "archive_related": (path in archive_only
                                            or bool(BSA_ROW_RE.match(path))),
                    } for path, partners in page]
                elif section == "losses":
                    items = [{
                        "path": path, "winner": winner,
                        "archive_member": bool(BSA_ROW_RE.match(path)),
                    } for path, winner in page]
                else:
                    items = [{
                        "path": path,
                        "archive_member": bool(BSA_ROW_RE.match(path)),
                    } for path in page]
                progress(2, 2, "Mod conflicts ready")
                return {
                    "ok": True,
                    "profile": profile_dir.name,
                    "mod": mod_name,
                    "revision": current_revision,
                    "filegraph_generation": snapshot.generation,
                    "summary": asdict(snapshot.conflict_summary(mod_name)),
                    "counts": {name: len(values)
                               for name, values in sections.items()},
                    "section": section,
                    "offset": offset,
                    "limit": limit,
                    "next_offset": (offset + len(page)
                                    if offset + len(page) < len(rows) else None),
                    "rows": items,
                }
            except JobError:
                raise
            except Exception as exc:
                raise JobError("conflicts_unavailable",
                               "Could not resolve the selected mod's conflicts.",
                               {"profile": profile_dir.name, "mod": mod_name,
                                "reason": str(exc)}) from exc
            finally:
                try:
                    game.set_active_profile_dir(previous)
                    game.load_paths(read_only=True)
                except Exception as exc:
                    raise JobError(
                        "profile_restore_failed",
                        "Could not restore the previous game profile context.",
                        {"profile": profile_dir.name,
                         "reason": str(exc)},
                    ) from exc
    return run
