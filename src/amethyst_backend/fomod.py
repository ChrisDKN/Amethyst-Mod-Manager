from __future__ import annotations

from .jobs import JobError


def _visible(step, flags, context):
    from Utils.fomod.installer import evaluate_dependency

    installed, active, loose = context
    return (step.visible_condition is None or evaluate_dependency(
        step.visible_condition, flags, installed, active,
        version_pass=True, loose_files=loose))


def describe_choices(prepared):
    from Utils.fomod.installer import (
        describe_dependency, get_default_install_selections,
        get_default_selections, resolve_plugin_type, update_flags,
    )

    config = prepared.fomod_config
    context = prepared.fomod_context
    installed, active, loose = context
    defaults = get_default_install_selections(config, *context)
    flags = {}
    steps = []
    for index, step in enumerate(config.steps):
        visible = _visible(step, flags, context)
        selected = get_default_selections(
            step, flags, installed, active, loose)
        steps.append({
            "index": str(index),
            "name": step.name,
            "visible_by_default": visible,
            "visibility_condition": (describe_dependency(step.visible_condition)
                                     if step.visible_condition else None),
            "groups": [{
                "name": group.name,
                "label": group.display_name or group.name,
                "type": group.group_type,
                "default_selected": selected.get(group.name, []),
                "options": [{
                    "name": plugin.name,
                    "description": plugin.description,
                    "type_by_default": resolve_plugin_type(
                        plugin, flags, installed, active, loose),
                } for plugin in group.plugins],
            } for group in step.groups],
        })
        if visible:
            flags = update_flags(step, defaults[str(index)], flags)
    return {"defaults": defaults, "steps": steps}


def validate_choices(prepared, selections):
    from Utils.fomod.installer import (
        resolve_plugin_type, update_flags, validate_selections,
    )

    config = prepared.fomod_config
    installed, active, loose = prepared.fomod_context
    valid_steps = {str(index) for index in range(len(config.steps))}
    unknown_steps = sorted(set(selections) - valid_steps)
    if unknown_steps:
        raise JobError("installer_selection_invalid",
                       "The FOMOD selection contains unknown steps.",
                       {"steps": unknown_steps})
    flags = {}
    for index, step in enumerate(config.steps):
        key = str(index)
        visible = _visible(step, flags, prepared.fomod_context)
        selected = selections.get(key)
        if not visible:
            if selected is not None:
                raise JobError("installer_selection_invalid",
                               "The FOMOD selection includes a hidden step.",
                               {"step": key, "name": step.name})
            continue
        if selected is None:
            raise JobError("installer_selection_invalid",
                           "The FOMOD selection omits a visible step.",
                           {"step": key, "name": step.name})
        groups = {group.name: group for group in step.groups}
        if set(selected) != set(groups):
            raise JobError("installer_selection_invalid",
                           "The FOMOD selection must include every visible group.",
                           {"step": key, "missing": sorted(set(groups) - set(selected)),
                            "unknown": sorted(set(selected) - set(groups))})
        for group in step.groups:
            names = selected[group.name]
            if len(names) != len(set(names)):
                raise JobError("installer_selection_invalid",
                               "The FOMOD selection repeats an option.",
                               {"step": key, "group": group.name})
            available = {plugin.name: plugin for plugin in group.plugins}
            unknown = [name for name in names if name not in available]
            if unknown:
                raise JobError("installer_selection_invalid",
                               "The FOMOD selection contains unknown options.",
                               {"step": key, "group": group.name,
                                "unknown": unknown})
            if group.group_type == "SelectAll":
                if set(names) != set(available):
                    raise JobError("installer_selection_invalid",
                                   "Every SelectAll option must be included.",
                                   {"step": key, "group": group.name})
                continue
            types = {name: resolve_plugin_type(plugin, flags, installed, active, loose)
                     for name, plugin in available.items()}
            unusable = [name for name in names if types[name] == "NotUsable"]
            if unusable:
                raise JobError("installer_selection_invalid",
                               "The selected FOMOD options are unavailable.",
                               {"step": key, "group": group.name,
                                "options": unusable})
            required = [name for name, kind in types.items() if kind == "Required"]
            if any(name not in names for name in required):
                raise JobError("installer_selection_invalid",
                               "Required FOMOD options must be selected.",
                               {"step": key, "group": group.name,
                                "required": required})
        errors = validate_selections(step, selected, flags, installed, active, loose)
        if errors:
            raise JobError("installer_selection_invalid",
                           "The FOMOD selection violates a group constraint.",
                           {"step": key, "errors": errors})
        flags = update_flags(step, selected, flags)
