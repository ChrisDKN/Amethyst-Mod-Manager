from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass

from Utils.fomod.installer import (
    evaluate_dependency, get_default_selections, plugin_dep_met,
    plugin_dep_unmet, resolve_plugin_type, update_flags, validate_selections,
)


def config_identity(config) -> str:
    raw = json.dumps(asdict(config), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class OptionState:
    plugin_type: str
    locked: bool
    missing_dependency: bool
    previously_selected: bool
    newly_available: bool


def option_states(group, flags, installed, active, loose, saved=(), rerun=False):
    types = {p.name: resolve_plugin_type(p, flags, installed, active, loose)
             for p in group.plugins}
    single_required = (group.group_type in ("SelectExactlyOne", "SelectAtMostOne")
                       and "Required" in types.values())
    return {
        p.name: OptionState(
            types[p.name],
            single_required or group.group_type == "SelectAll"
            or types[p.name] in ("Required", "NotUsable"),
            plugin_dep_unmet(p, active, installed, loose),
            p.name in saved,
            bool(rerun and p.name not in saved
                 and plugin_dep_met(p, active, installed, loose)),
        ) for p in group.plugins
    }


def constrain_group(group, selected, states):
    if group.group_type == "SelectAll":
        return [p.name for p in group.plugins]
    required = [p.name for p in group.plugins
                if states[p.name].plugin_type == "Required"]
    chosen = [p.name for p in group.plugins
              if p.name in selected and states[p.name].plugin_type != "NotUsable"]
    if group.group_type in ("SelectExactlyOne", "SelectAtMostOne"):
        return (required or chosen)[:1]
    return [p.name for p in group.plugins if p.name in chosen or p.name in required]


def initial_selections(step, flags, installed, active, loose, saved=None):
    defaults = get_default_selections(step, flags, installed, active, loose)
    result = {}
    for group in step.groups:
        states = option_states(group, flags, installed, active, loose)
        default = constrain_group(group, defaults.get(group.name, []), states)
        if not default and group.group_type in ("SelectExactlyOne", "SelectAtLeastOne"):
            default = [p.name for p in group.plugins
                       if states[p.name].plugin_type != "NotUsable"][:1]
        previous = (saved or {}).get(group.name, [])
        filtered = [name for name in previous if name in states
                    and states[name].plugin_type != "NotUsable"
                    and not states[name].missing_dependency]
        selected = filtered or default
        if filtered and group.group_type in ("SelectAny", "SelectAtLeastOne", "SelectAll"):
            selected = list(dict.fromkeys(filtered + default))
        result[group.name] = constrain_group(group, selected, states)
    return result


class FomodSession:
    def __init__(self, config, saved=None, context=None, draft=None, restore_saved=True):
        self.config = config
        self.saved = deepcopy(saved or {})
        self.restore_saved = restore_saved
        self.draft = deepcopy(draft) if draft is not None else {}
        self.context = context if context is not None else (set(), set(), set())
        self.recompute()

    def recompute(self, context=None):
        if context is not None:
            self.context = context
        installed, active, loose = self.context
        flags = {}
        self.visible = []
        self.states = {}
        self.errors = []
        self.selections = {}
        for si, step in enumerate(self.config.steps):
            if step.visible_condition is not None and not evaluate_dependency(
                    step.visible_condition, flags, installed, active,
                    version_pass=True, loose_files=loose):
                continue
            key = str(si)
            self.visible.append(si)
            saved = self.saved.get(key, {})
            if key not in self.draft:
                self.draft[key] = initial_selections(
                    step, flags, installed, active, loose,
                    saved if self.restore_saved else None)
            selections = self.draft[key]
            for gi, group in enumerate(step.groups):
                states = option_states(group, flags, installed, active, loose,
                                       saved.get(group.name, ()), bool(self.saved))
                self.states[si, gi] = states
                selections[group.name] = constrain_group(
                    group, selections.get(group.name, []), states)
            self.errors.extend((si, error) for error in validate_selections(
                step, selections, flags, installed, active, loose))
            self.selections[key] = deepcopy(selections)
            flags = update_flags(step, selections, flags)
        self.flags = flags

    def toggle(self, si, gi, name):
        if si not in self.visible or self.states[si, gi][name].locked:
            return
        group = self.config.steps[si].groups[gi]
        selected = list(self.draft[str(si)].get(group.name, []))
        if name in selected:
            if group.group_type == "SelectExactlyOne":
                return
            selected.remove(name)
        elif group.group_type in ("SelectExactlyOne", "SelectAtMostOne"):
            selected = [name]
        else:
            selected.append(name)
        self.draft[str(si)][group.name] = selected
        self.recompute()


@dataclass(frozen=True)
class FomodReinstallRequest:
    game_name: str
    profile_dir: str
    mod_name: str
    owner_profile_dir: str
    owner_mod_name: str
    config_id: str
    selections_json: str
    target_dir: str
    target_identity: tuple
    epoch: int

    @property
    def key(self):
        return self.game_name, self.profile_dir, self.mod_name

    def validate(self, config, context):
        if config is None or config_identity(config) != self.config_id:
            raise ValueError("The archive's FOMOD configuration has changed. Run a normal reinstall to review it.")
        selections = json.loads(self.selections_json)
        session = FomodSession(config, context=context, draft=selections)
        if session.errors:
            raise ValueError(session.errors[0][1])
        if session.selections != selections:
            raise ValueError("Available FOMOD choices have changed. Review the choices and try again.")
        return selections
