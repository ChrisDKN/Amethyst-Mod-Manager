from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace

from .reconstruct import signature
from .store import installation_info


@dataclass
class UpdatePlan:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    added_profiles: list[str] = field(default_factory=list)
    removed_profiles: list[str] = field(default_factory=list)
    affected_profiles: list[str] = field(default_factory=list)


def plan_update(request):
    from .profiles import referenced_profiles
    info = installation_info(request.directory)
    if not info:
        raise ValueError("Installation does not exist")
    with sqlite3.connect((request.directory / "state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        old = {path: sig for path, sig in db.execute("SELECT path,signature FROM outputs WHERE path LIKE 'root/%'")}
    new = {"root/" + d.path: signature(d, request) for d in request.package.directives
           if d.path.split("/")[0].casefold() != "temp_bsa_files"}
    profiles = set(info.get("selected_profiles", []))
    selected = set(request.profiles)
    return UpdatePlan(sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys()),
        sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
        sorted(selected - profiles), sorted(profiles - selected),
        [p.name for p in referenced_profiles(request.directory, request.game.get_profile_root())])


def repair(request, **kwargs):
    from .install import run_install
    return run_install(replace(request, mode="repair"), **kwargs)


def update(request, **kwargs):
    from .install import run_install
    return run_install(replace(request, mode="update"), **kwargs)
