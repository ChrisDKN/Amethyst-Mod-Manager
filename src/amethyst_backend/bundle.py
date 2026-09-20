from __future__ import annotations

from .jobs import JobError


def describe_choices(prepared):
    from Utils.re_engine.bundle import layout_to_spec

    spec = layout_to_spec(prepared.bundle_layout)
    return {
        "bundle_name": prepared.bundle_layout.bundle_name,
        "defaults": [option.folder for group in spec.groups
                     for option in group.options if option.selected],
        "groups": [{
            "name": group.name,
            "select_one": group.select_one,
            "options": [{
                "folder": option.folder,
                "label": option.label,
                "is_label": option.is_label,
                "default_selected": option.selected,
            } for option in group.options],
        } for group in spec.groups],
    }


def validate_choices(prepared, selected):
    from Utils.re_engine.bundle import layout_to_spec

    spec = layout_to_spec(prepared.bundle_layout)
    choices = set(selected)
    available = {option.folder for group in spec.groups
                 for option in group.options if not option.is_label}
    unknown = sorted(choices - available)
    if unknown or len(choices) != len(selected):
        raise JobError("installer_selection_invalid",
                       "The bundle selection contains unknown or repeated options.",
                       {"unknown": unknown})
    for group in spec.groups:
        if group.select_one:
            real = {option.folder for option in group.options
                    if not option.is_label}
            if real and len(choices & real) != 1:
                raise JobError("installer_selection_invalid",
                               "Exactly one option is required for this bundle group.",
                               {"group": group.name, "options": sorted(real)})
