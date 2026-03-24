"""
fomod_installer.py
Stateless logic engine for FOMOD installation.
No UI, no file I/O. All functions are pure.
"""

from __future__ import annotations

from Utils.fomod_parser import (
    ConditionalInstallPattern, Dependency, FileInstall, Group, InstallStep,
    ModuleConfig, Plugin, TypeDescriptor,
)


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def evaluate_dependency(dep: Dependency, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None) -> bool:
    """
    Recursively evaluate a Dependency tree.

    flag_state:      current flag name → value mapping
    installed_files: set of all plugin names known to be present (lower-case),
                     regardless of whether they are enabled or disabled.
    active_files:    set of enabled/active plugin names (lower-case).
                     If None, falls back to treating installed_files as active.

    Returns True if the condition is satisfied.
    """
    if dep.dep_type == "composite":
        if not dep.sub_deps:
            return True  # Empty composite = no restriction = pass
        results = [evaluate_dependency(d, flag_state, installed_files, active_files)
                   for d in dep.sub_deps]
        if dep.operator.lower() == "or":
            return any(results)
        return all(results)  # default: "And"

    if dep.dep_type == "flag":
        return flag_state.get(dep.flag_name, "") == dep.flag_value

    if dep.dep_type == "file":
        # Case-insensitive — FOMOD was designed for Windows
        key = dep.file_name.lower()
        present = key in installed_files
        if dep.file_state == "Active":
            # Active: file must be present AND enabled
            if active_files is not None:
                return key in active_files
            return present
        if dep.file_state == "Inactive":
            # Inactive: file is present (installed) but NOT enabled
            if active_files is not None:
                return present and key not in active_files
            return False
        # "Missing": file must not be installed at all
        return not present

    if dep.dep_type == "unsatisfiable":
        return False

    # Unknown type — pass through
    return True


# ---------------------------------------------------------------------------
# Plugin type resolution
# ---------------------------------------------------------------------------

def resolve_plugin_type(plugin: Plugin, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None) -> str:
    """
    Evaluate a plugin's typeDescriptor to get its effective type string.
    For simple typeDescriptors returns the static type directly.
    For conditional typeDescriptors, evaluates patterns in order and returns
    the first matching type, or default_type if none match.

    Returns one of: "Optional" | "Required" | "Recommended" | "CouldBeUsable" | "NotUsable"
    """
    td = plugin.type_descriptor
    if not td.is_conditional:
        return td.plugin_type

    for dep, type_name in td.patterns:
        if evaluate_dependency(dep, flag_state, installed_files, active_files):
            return type_name

    # No pattern matched. If the default is NotUsable but the pattern set
    # includes a Required outcome, it means this is a version-detection group
    # where we couldn't auto-detect the game version. Let the user choose freely.
    if td.default_type == "NotUsable" and any(t == "Required" for _, t in td.patterns):
        return "Optional"

    return td.default_type


# ---------------------------------------------------------------------------
# Step visibility
# ---------------------------------------------------------------------------

def get_visible_steps(config: ModuleConfig, flag_state: dict[str, str],
                      installed_files: set[str],
                      active_files: set[str] | None = None) -> list[InstallStep]:
    """
    Filter config.steps to only those whose visible_condition is satisfied.
    Steps with no condition (None) are always visible.
    Returns the ordered list of visible InstallStep objects.
    """
    visible = []
    for step in config.steps:
        if step.visible_condition is None:
            visible.append(step)
        elif evaluate_dependency(step.visible_condition, flag_state, installed_files, active_files):
            visible.append(step)
    return visible


# ---------------------------------------------------------------------------
# Default selections
# ---------------------------------------------------------------------------

def get_default_selections(step: InstallStep, flag_state: dict[str, str],
                           installed_files: set[str],
                           active_files: set[str] | None = None) -> dict[str, list[str]]:
    """
    Compute default plugin selections for a step based on group types and plugin types.
    Returns {group_name: [plugin_name, ...]}
    """
    defaults: dict[str, list[str]] = {}

    for group in step.groups:
        plugins = group.plugins
        if not plugins:
            defaults[group.name] = []
            continue

        gtype = group.group_type
        plugin_types = [resolve_plugin_type(p, flag_state, installed_files, active_files)
                        for p in plugins]

        if gtype == "SelectAll":
            defaults[group.name] = [p.name for p in plugins]

        elif gtype == "SelectExactlyOne":
            # Required → Recommended → first
            for i, p in enumerate(plugins):
                if plugin_types[i] == "Required":
                    defaults[group.name] = [p.name]
                    break
            else:
                for i, p in enumerate(plugins):
                    if plugin_types[i] == "Recommended":
                        defaults[group.name] = [p.name]
                        break
                else:
                    defaults[group.name] = [plugins[0].name]

        elif gtype == "SelectAtMostOne":
            # Required → Recommended → none
            for i, p in enumerate(plugins):
                if plugin_types[i] == "Required":
                    defaults[group.name] = [p.name]
                    break
            else:
                for i, p in enumerate(plugins):
                    if plugin_types[i] == "Recommended":
                        defaults[group.name] = [p.name]
                        break
                else:
                    defaults[group.name] = []

        elif gtype in ("SelectAtLeastOne", "SelectAny"):
            # All Required + Recommended; fallback to [first] for SelectAtLeastOne
            selected = [p.name for p, t in zip(plugins, plugin_types)
                        if t in ("Required", "Recommended")]
            if not selected and gtype == "SelectAtLeastOne":
                selected = [plugins[0].name]
            defaults[group.name] = selected

        else:
            defaults[group.name] = []

    return defaults


# ---------------------------------------------------------------------------
# Flag state update
# ---------------------------------------------------------------------------

def update_flags(step: InstallStep, selections: dict[str, list[str]],
                 flag_state: dict[str, str]) -> dict[str, str]:
    """
    After a step is completed, apply conditionFlags from all selected plugins.
    Returns an updated copy of flag_state.

    selections: {group_name: [plugin_name, ...]} for this step only.
    """
    new_state = dict(flag_state)
    for group in step.groups:
        selected_names = set(selections.get(group.name, []))
        for plugin in group.plugins:
            if plugin.name in selected_names:
                new_state.update(plugin.condition_flags)
    return new_state


# ---------------------------------------------------------------------------
# File resolution
# ---------------------------------------------------------------------------

def resolve_files(config: ModuleConfig,
                  all_selections: dict[str, dict[str, list[str]]],
                  installed_files: set[str] | None = None,
                  active_files: set[str] | None = None) -> list[tuple[str, str, bool]]:
    """
    Build the final file install list from required files + user selections
    + conditional file installs.

    all_selections: {step_name: {group_name: [plugin_name, ...]}}
    Returns list of (source_path, destination_path, is_folder) tuples with OS-normalized paths.
    Required files are always included first.
    """
    result: list[tuple[str, str, bool]] = []
    inst_files = installed_files or set()

    # Always-install files
    for fi in config.required_files:
        result.append((fi.source_path, fi.destination_path, fi.is_folder))

    # Selected plugin files — collect with priority for sorting
    prioritized: list[tuple[int, str, str, bool]] = []

    # Build final flag state by replaying all steps in order
    flag_state: dict[str, str] = {}
    for i, step in enumerate(config.steps):
        # Accept both new index-keyed format (str(i)) and old name-keyed format
        # (step.name) for backward compatibility with previously saved JSON files.
        step_selections = all_selections.get(str(i)) or all_selections.get(step.name, {})
        for group in step.groups:
            selected_names = set(step_selections.get(group.name, []))
            for plugin in group.plugins:
                if plugin.name in selected_names:
                    for fi in plugin.files:
                        prioritized.append((fi.priority, fi.source_path,
                                            fi.destination_path, fi.is_folder))
                    flag_state.update(plugin.condition_flags)

    # Conditional file installs — evaluate each pattern against final flag state
    for pattern in config.conditional_file_installs:
        if evaluate_dependency(pattern.dependency, flag_state, inst_files, active_files):
            for fi in pattern.files:
                prioritized.append((fi.priority, fi.source_path,
                                    fi.destination_path, fi.is_folder))

    # Sort by priority (lower number = install first)
    prioritized.sort(key=lambda x: x[0])
    for _, src, dst, is_folder in prioritized:
        result.append((src, dst, is_folder))

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_selections(step: InstallStep,
                        selections: dict[str, list[str]]) -> list[str]:
    """
    Check if current selections satisfy each group's type constraint.
    Returns a list of error messages (empty = all valid).
    """
    errors: list[str] = []

    for group in step.groups:
        selected = selections.get(group.name, [])
        count = len(selected)
        gtype = group.group_type

        if gtype == "SelectExactlyOne" and count != 1:
            errors.append(f'"{group.name}": select exactly one option.')
        elif gtype == "SelectAtLeastOne" and count < 1:
            errors.append(f'"{group.name}": select at least one option.')
        elif gtype == "SelectAtMostOne" and count > 1:
            errors.append(f'"{group.name}": select at most one option.')
        # SelectAny and SelectAll have no constraint to enforce here

    return errors
