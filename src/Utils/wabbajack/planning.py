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
    from .adapters import ROOT_MOD_NAME, adapter_for
    info = installation_info(request.directory)
    if not info:
        raise ValueError("Installation does not exist")
    with sqlite3.connect((request.directory / "state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        old = {path: sig for path, sig in db.execute("SELECT path,signature FROM outputs WHERE path LIKE 'root/%'")}
    adapter = adapter_for(request.package, request.game, store=request.setup_options.get("store", ""))
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
    from .post_install import stock_copy, nuclear_sunset
    stock = stock_copy(request)
    if stock:
        new.update({key: sig for key, sig in old.items() if key.startswith(f"root/{stock}/") and key not in new})
    if nuclear_sunset(request.package) and "nuclear:proton-dxvk" in request.fixes:
        from .manifest import stock_folder
        stock = stock_folder(request.package)
        for key in list(new):
            if key.casefold() in {f"root/{stock}/{name}".casefold() for name in ("d3d9.dll", "dxvk.conf")}:
                new.pop(key)
    display = request.setup_options.get("display")
    if display:
        for key in list(new):
            if key.startswith("root/mods/") and key.casefold().endswith("/ssedisplaytweaks.ini"):
                new[key] = f"display:1:{display[0]}x{display[1]}:" + new[key]
    if any(adapter.root_mod_destination(d.path) for d in request.package.directives):
        key = f"root/mods/{ROOT_MOD_NAME}/meta.ini"
        new[key] = old.get(key, "root-mod")
    from .bsa_setup import PREFIX, RECIPE, requirement, sources, expected_outputs, library_paths
    if requirement(request.package):
        try:
            new.update(expected_outputs(sources(request)))
            new[PREFIX + "meta.ini"] = RECIPE
            by_path = {d.path: d for d in request.package.directives}
            for name, path in library_paths(request.package).items():
                new[PREFIX + "root/" + name] = "bsa-library:" + signature(by_path[path], request)
        except (OSError, ValueError):
            new.update({p: sig for p, sig in old.items() if p.startswith(PREFIX)})
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
