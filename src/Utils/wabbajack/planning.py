from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace

from .diagnostics import emit
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


def plan_update(request, log=None):
    from .profiles import referenced_profiles
    from .adapters import ROOT_MOD_NAME, adapter_for
    info = installation_info(request.directory, log)
    if not info:
        raise ValueError("Installation does not exist")
    with sqlite3.connect((request.directory / "state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        old = {path: sig for path, sig in db.execute("SELECT path,signature FROM outputs WHERE path LIKE 'root/%'")}
    adapter = adapter_for(request.package, request.game,
                          store=request.setup_options.get("store", ""), log=log)
    from .manifest import optional_game_file_directives
    ignored = optional_game_file_directives(request.package)
    new = {"root/" + rel: signature(d, request) for d in request.package.directives
           if d.path not in ignored and d.path.split("/")[0].casefold() != "temp_bsa_files"
           and (rel := adapter.installed_path(d.path))}
    from .requirements import setup_tasks
    from .setup_tasks import output_mods
    for task in setup_tasks(request.package, request.profiles):
        record = info.get("setup_tasks", {}).get(task.id, {})
        sig = record.get("signature", "setup:pending")
        if request.setup_options.get(task.id, {}) != record.get("option", {}):
            sig = "setup:pending"
        new.update({key: sig for key in record.get("outputs", {}) if key not in new})
    for name in output_mods(request):
        key = f"root/mods/{name}/meta.ini"
        new.setdefault(key, old.get(key, "output-mod"))
    from .post_install import stock_copy
    from .post_install_rules import display_rule, display_signature, omitted_stock_paths
    stock = stock_copy(request)
    if stock:
        new.update({key: sig for key, sig in old.items() if key.startswith(f"root/{stock}/") and key not in new})
    omitted = omitted_stock_paths(request)
    if omitted:
        for key in list(new):
            if key.casefold() in omitted:
                new.pop(key)
    display = request.setup_options.get("display")
    if display:
        for key in list(new):
            if display_rule(key, installed=True) is not None:
                new[key] = display_signature(display, new[key])
    if any(adapter.root_mod_destination(d.path) for d in request.package.directives):
        key = f"root/mods/{ROOT_MOD_NAME}/meta.ini"
        new[key] = old.get(key, "root-mod")
    from .bsa_setup import PREFIX, RECIPE, requirement, sources, expected_outputs, library_paths
    if requirement(request.package):
        try:
            new.update(expected_outputs(sources(request, log=log)))
            new[PREFIX + "meta.ini"] = RECIPE
            by_path = {d.path: d for d in request.package.directives}
            for name, path in library_paths(request.package).items():
                new[PREFIX + "root/" + name] = "bsa-library:" + signature(by_path[path], request)
        except (OSError, ValueError) as exc:
            emit(log, "update.plan.bsa_fallback", exception_type=type(exc).__name__,
                 exception=str(exc))
            new.update({p: sig for p, sig in old.items() if p.startswith(PREFIX)})
    profiles = set(info.get("selected_profiles", []))
    selected = set(request.profiles)
    plan = UpdatePlan(sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys()),
        sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
        sorted(selected - profiles), sorted(profiles - selected),
        [p.name for p in referenced_profiles(request.directory,
                                             request.game.get_profile_root(), log)])
    emit(log, "update.plan.completed", old_outputs=len(old), new_outputs=len(new),
         added=len(plan.added), removed=len(plan.removed), changed=len(plan.changed),
         added_profiles=plan.added_profiles, removed_profiles=plan.removed_profiles,
         affected_profiles=plan.affected_profiles)
    return plan


def repair(request, **kwargs):
    from .install import run_install
    return run_install(replace(request, mode="repair"), **kwargs)


def update(request, **kwargs):
    from .install import run_install
    return run_install(replace(request, mode="update"), **kwargs)
