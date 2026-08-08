"""
fomod_installer.py
Stateless logic engine for FOMOD installation.
No UI, no file I/O. All functions are pure.
"""

from __future__ import annotations

from Utils.fomod_parser import (
    Dependency, InstallStep, ModuleConfig, Plugin,
)


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def evaluate_dependency(dep: Dependency, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None,
                        version_pass: bool = False,
                        loose_files: set[str] | None = None) -> bool:
    """
    Recursively evaluate a Dependency tree.

    flag_state:      current flag name → value mapping
    installed_files: set of all plugin names known to be present (lower-case),
                     regardless of whether they are enabled or disabled.
    active_files:    set of enabled/active plugin names (lower-case).
                     If None, falls back to treating installed_files as active.
    version_pass:    When True, unevaluable version dependencies
                     (gameDependency, foseDependency, etc.) evaluate to True.
                     Use for step visibility so steps with version gates are
                     still shown. For typeDescriptor pattern matching leave
                     False so patterns fall through to the default type.
    loose_files:     Optional set of every deployed file's relative path
                     (lower-case, forward-slash separators). Used to resolve
                     <fileDependency> nodes that reference a loose asset path
                     (e.g. "textures/foo.dds") rather than a plugin name. MO2
                     evaluates fileDependency against the whole virtual file
                     tree, not just plugins; this set restores that behaviour.

    Returns True if the condition is satisfied.
    """
    if dep.dep_type == "composite":
        if not dep.sub_deps:
            return True  # Empty composite = no restriction = pass
        results = [evaluate_dependency(d, flag_state, installed_files,
                                        active_files, version_pass, loose_files)
                   for d in dep.sub_deps]
        if dep.operator.lower() == "or":
            return any(results)
        return all(results)  # default: "And"

    if dep.dep_type == "flag":
        return flag_state.get(dep.flag_name, "") == dep.flag_value

    if dep.dep_type == "file":
        # Case-insensitive - FOMOD was designed for Windows. Strip surrounding
        # whitespace: some FOMODs ship a stray trailing space in the name (e.g.
        # "Blacksmith Chests.esp "), which would otherwise fail the plugin-suffix
        # test → misrouted to the loose-asset path → the plugin condition never
        # matches even when it's installed + active.
        key = dep.file_name.strip().lower()
        # A fileDependency may reference an arbitrary loose asset path, not just
        # a plugin name. Detect that (path separator, or an extension that isn't
        # a plugin) and resolve it against the deployed file tree when we have
        # one. MO2 checks the whole virtual tree here.
        norm_key = key.replace("\\", "/")
        looks_loose = ("/" in norm_key
                       or not norm_key.endswith((".esp", ".esm", ".esl")))
        if looks_loose and loose_files is not None:
            present = norm_key in loose_files
            # Loose assets have no enable/disable concept - present == active.
            if dep.file_state == "Inactive":
                return False
            if dep.file_state == "Missing":
                return not present
            return present  # "Active"
        if looks_loose and loose_files is None:
            # No deployed file tree available to check a loose-asset gate.
            # Match the version-gate philosophy: don't silently fail closed.
            #   Active/Inactive  → satisfied when version_pass (lenient context)
            #   Missing          → satisfied (we can't prove it's present)
            if dep.file_state == "Missing":
                return True
            return version_pass

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

    if dep.dep_type == "version":
        return version_pass

    if dep.dep_type == "unsatisfiable":
        return False

    # Unknown type - pass through
    return True


# ---------------------------------------------------------------------------
# Plugin type resolution
# ---------------------------------------------------------------------------

def resolve_plugin_type(plugin: Plugin, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None,
                        loose_files: set[str] | None = None) -> str:
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
        if evaluate_dependency(dep, flag_state, installed_files, active_files,
                               loose_files=loose_files):
            return type_name

    # No pattern matched. If the default is NotUsable but the pattern set
    # includes a Required outcome, it means this is a version-detection group
    # where we couldn't auto-detect the game version. Let the user choose freely.
    if td.default_type == "NotUsable" and any(t == "Required" for _, t in td.patterns):
        return "Optional"

    return td.default_type


# ---------------------------------------------------------------------------
# Pre-install module dependency gate
# ---------------------------------------------------------------------------

def describe_dependency(dep: Dependency, indent: int = 0) -> str:
    """Human-readable one-line-per-clause description of a Dependency tree."""
    pad = "  " * indent
    if dep.dep_type == "composite":
        if not dep.sub_deps:
            return f"{pad}(no conditions)"
        op = dep.operator.upper()
        lines = [f"{pad}{op}:"]
        for d in dep.sub_deps:
            lines.append(describe_dependency(d, indent + 1))
        return "\n".join(lines)
    if dep.dep_type == "flag":
        return f"{pad}flag {dep.flag_name!r} == {dep.flag_value!r}"
    if dep.dep_type == "file":
        return f"{pad}file {dep.file_name!r} state={dep.file_state}"
    if dep.dep_type == "version":
        return f"{pad}game/extender version (unchecked)"
    if dep.dep_type == "unsatisfiable":
        return f"{pad}<unknown dependency>"
    return f"{pad}{dep.dep_type}"


def check_module_dependencies(
    config: ModuleConfig,
    installed_files: set[str] | None = None,
    active_files: set[str] | None = None,
    loose_files: set[str] | None = None,
) -> tuple[bool, str]:
    """
    Evaluate <moduleDependencies> before the wizard runs.

    Returns (ok, message). `ok` is True when the gate passes (or is absent).
    When False, `message` is a human-readable description of what failed -
    suitable to show to the user so they can decide whether to proceed.

    Version / script-extender dependencies are treated as passing (version_pass
    is True) so that we don't block installs when we can't detect the game
    version.
    """
    if config.module_dependency is None:
        return True, ""
    inst = installed_files or set()
    ok = evaluate_dependency(config.module_dependency, {}, inst, active_files,
                             version_pass=True, loose_files=loose_files)
    if ok:
        return True, ""
    return False, describe_dependency(config.module_dependency)


# ---------------------------------------------------------------------------
# Step visibility
# ---------------------------------------------------------------------------

def get_visible_steps(config: ModuleConfig, flag_state: dict[str, str],
                      installed_files: set[str],
                      active_files: set[str] | None = None,
                      loose_files: set[str] | None = None) -> list[InstallStep]:
    """
    Filter config.steps to only those whose visible_condition is satisfied.
    Steps with no condition (None) are always visible.
    Returns the ordered list of visible InstallStep objects.
    """
    visible = []
    for step in config.steps:
        if step.visible_condition is None:
            visible.append(step)
        elif evaluate_dependency(step.visible_condition, flag_state,
                                  installed_files, active_files,
                                  version_pass=True, loose_files=loose_files):
            visible.append(step)
    return visible


# ---------------------------------------------------------------------------
# Default selections
# ---------------------------------------------------------------------------

def get_default_selections(step: InstallStep, flag_state: dict[str, str],
                           installed_files: set[str],
                           active_files: set[str] | None = None,
                           loose_files: set[str] | None = None) -> dict[str, list[str]]:
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
        plugin_types = [resolve_plugin_type(p, flag_state, installed_files,
                                            active_files, loose_files)
                        for p in plugins]

        if gtype == "SelectAll":
            defaults[group.name] = [p.name for p in plugins]

        elif gtype == "SelectExactlyOne":
            # Required → Recommended → first selectable (skip NotUsable, which
            # is greyed out / un-clickable in the UI - never default to it)
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
                    for i, p in enumerate(plugins):
                        if plugin_types[i] != "NotUsable":
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
    SelectAll groups contribute every plugin's flags even if the selections
    dict is empty for that group (matches MO2's preselect behaviour).
    """
    new_state = dict(flag_state)
    for group in step.groups:
        selected_names = set(selections.get(group.name, []))
        for plugin in group.plugins:
            if group.group_type == "SelectAll" or plugin.name in selected_names:
                new_state.update(plugin.condition_flags)
    return new_state


# ---------------------------------------------------------------------------
# File resolution
# ---------------------------------------------------------------------------

def resolve_files(config: ModuleConfig,
                  all_selections: dict[str, dict[str, list[str]]],
                  installed_files: set[str] | None = None,
                  active_files: set[str] | None = None,
                  loose_files: set[str] | None = None) -> list[tuple[str, str, bool]]:
    """
    Build the final file install list from required files + user selections
    + conditional file installs.

    all_selections: {step_name: {group_name: [plugin_name, ...]}}
    Returns list of (source_path, destination_path, is_folder) tuples with OS-normalized paths.

    Install order matches MO2's three-phase scheme:
      1. <requiredInstallFiles>        (always first, sorted by priority)
      2. <plugin> files from selections + alwaysInstall/installIfUsable files
      3. <conditionalFileInstalls>     (always last, sorted by priority)
    """
    inst_files = installed_files or set()

    required: list[tuple[int, str, str, bool]] = []
    options: list[tuple[int, str, str, bool]] = []
    conditional: list[tuple[int, str, str, bool]] = []

    for fi in config.required_files:
        required.append((fi.priority, fi.source_path,
                         fi.destination_path, fi.is_folder))

    # Build final flag state by replaying all steps in order
    flag_state: dict[str, str] = {}
    for i, step in enumerate(config.steps):
        # Skip steps whose visibility condition is not satisfied by the flags
        # accumulated so far. If a step was invisible the user never saw it,
        # so its SelectAll/selected plugins must not contribute files or flags.
        if step.visible_condition is not None:
            if not evaluate_dependency(step.visible_condition, flag_state,
                                       inst_files, active_files,
                                       version_pass=True, loose_files=loose_files):
                continue
        # Accept both new index-keyed format (str(i)) and old name-keyed format
        # for backward compatibility with previously saved selection JSON.
        step_selections = all_selections.get(str(i)) or all_selections.get(step.name, {})
        for group in step.groups:
            selected_names = set(step_selections.get(group.name, []))
            for plugin in group.plugins:
                ptype = resolve_plugin_type(plugin, flag_state, inst_files,
                                            active_files, loose_files)
                is_selected = (group.group_type == "SelectAll"
                               or plugin.name in selected_names)
                if is_selected:
                    for fi in plugin.files:
                        options.append((fi.priority, fi.source_path,
                                        fi.destination_path, fi.is_folder))
                    flag_state.update(plugin.condition_flags)
                else:
                    # alwaysInstall / installIfUsable files install even when
                    # the plugin is not selected.
                    for fi in plugin.files:
                        if fi.always_install or (fi.install_if_usable
                                                 and ptype != "NotUsable"):
                            options.append((fi.priority, fi.source_path,
                                            fi.destination_path, fi.is_folder))

    # Conditional file installs - evaluated against final flag state.
    # version_pass=True: unevaluable engine/game version gates
    # (gameDependency, foseDependency, nvseDependency, …) are treated as
    # satisfied, matching MO2/Vortex - we can't know the user's script-extender
    # version, so we install the gated payload rather than silently dropping it.
    # Without this, stepless FOMODs whose entire payload sits in
    # <conditionalFileInstalls> behind a version gate would install 0 files.
    for pattern in config.conditional_file_installs:
        if evaluate_dependency(pattern.dependency, flag_state, inst_files,
                               active_files, version_pass=True,
                               loose_files=loose_files):
            for fi in pattern.files:
                conditional.append((fi.priority, fi.source_path,
                                    fi.destination_path, fi.is_folder))

    required.sort(key=lambda x: x[0])
    options.sort(key=lambda x: x[0])
    conditional.sort(key=lambda x: x[0])

    result: list[tuple[str, str, bool]] = []
    for bucket in (required, options, conditional):
        for _, src, dst, is_folder in bucket:
            result.append((src, dst, is_folder))
    return result


# Delimiters for the rerun-flag clause strings stored in meta.ini. These MUST be
# characters that can never appear in a Bethesda plugin filename - Windows forbids
# < > : " / \ | ? * in filenames, so all four below are collision-proof. (An
# earlier version used "+"/"!", which ARE legal in plugin names - e.g. "YUP - Base
# Game + All DLC.esm" - so splitting on "+" shredded the name into non-existent
# members and the flag fired forever. Never use a filename-legal delimiter here.)
FLAG_OPT_SEP = ";"      # separates independent option-conditions
FLAG_OR_SEP = "|"       # OR alternatives within one option
FLAG_AND_SEP = "<"      # AND members within one alternative clause
FLAG_ABSENT = ">"       # prefix: this plugin must be ABSENT (state="Missing")


def _dep_plugin_name(dep: "Dependency") -> str:
    """The cleaned plugin-name literal of a fileDependency for the rerun-flag
    clauses, or "" if it shouldn't drive the flag. Strips surrounding whitespace
    (some FOMODs ship a stray trailing space, e.g. "Blacksmith Chests.esp ", which
    would otherwise fail the .esp suffix test and never match plugins.txt) and
    skips loose-asset paths.

    State handling - the flag only models load-order presence (enabled / absent):
      * ``Active``   → bare name (must be PRESENT + enabled),
      * ``Missing``  → ``!name`` (must be ABSENT),
      * ``Inactive`` → "" (DROPPED). "Present but disabled" is a variant/version
        selector (e.g. a SkyrimVR-vs-SE or Beard-Mask-on/off gate), NOT a "you need
        this mod" requirement - recording it as present-required would fire the
        flag the instant the option installs (the plugin is, by definition, not
        enabled), and there's no clean present/absent literal for "disabled"."""
    if dep is None or dep.dep_type != "file" or dep.file_state == "Inactive":
        return ""
    name = (dep.file_name or "").strip()
    norm = name.lower().replace("\\", "/")
    if "/" in norm or not norm.endswith((".esp", ".esm", ".esl")):
        return ""
    return (FLAG_ABSENT + name) if dep.file_state == "Missing" else name


def _pattern_has_inactive_plugin(dep: "Dependency") -> bool:
    """True if *dep*'s tree references any real plugin with state="Inactive".
    The rerun-flag clause format can't represent "present but DISABLED" (only
    enabled / absent), so an option gated on an Inactive plugin can't be faithfully
    encoded - we exclude it from the flag rather than fire a clause that the
    wizard's real evaluation then contradicts (flag on, but nothing selectable)."""
    if dep is None:
        return False
    if dep.dep_type == "file":
        if dep.file_state != "Inactive":
            return False
        norm = (dep.file_name or "").strip().lower().replace("\\", "/")
        return "/" not in norm and norm.endswith((".esp", ".esm", ".esl"))
    if dep.dep_type == "composite":
        return any(_pattern_has_inactive_plugin(s) for s in dep.sub_deps)
    return False


def _pattern_dep_groups(dep: "Dependency") -> list[list[str]]:
    """Turn a pattern's Dependency tree into AND-groups of plugin-name literals,
    where the pattern matches when EVERY literal in ANY one group holds. Each
    inner list is an AND-clause; the outer list is an OR over clauses.

    A literal is a plugin name (must be PRESENT) or ``!name`` (must be ABSENT, from
    a ``state="Missing"`` gate). So a mixed
    ``And(Thaumaturgy.esp Active, gaunt.esl Missing)`` → ``[["Thaumaturgy.esp",
    "!cccbhsse001-gaunt.esl"]]``.

    Handles the shapes real FOMODs use: a bare fileDependency, an And-composite
    (all must hold → one group), and an Or-composite (each branch → its own
    group). Nested composites recurse. Non-file leaves (flag/version) are ignored.
    """
    if dep is None:
        return []
    if dep.dep_type == "file":
        name = _dep_plugin_name(dep)
        return [[name]] if name else []
    if dep.dep_type != "composite":
        return []  # flag / version / unsatisfiable - not plugin-driven
    if not dep.sub_deps:
        return []
    if dep.operator.lower() == "or":
        # Union of each branch's groups.
        out: list[list[str]] = []
        for sub in dep.sub_deps:
            out.extend(_pattern_dep_groups(sub))
        return out
    # "And": every sub must hold. Cartesian-combine the sub-groups so the result
    # is still a flat OR-of-ANDs. In practice sub-deps are single fileDependencies
    # (one group each), so this collapses to a single combined AND-group.
    combos: list[list[str]] = [[]]
    for sub in dep.sub_deps:
        sub_groups = _pattern_dep_groups(sub)
        if not sub_groups:
            continue  # non-plugin clause (flag/version) - doesn't add plugins
        new_combos: list[list[str]] = []
        for base in combos:
            for g in sub_groups:
                new_combos.append(base + g)
        combos = new_combos
    return [c for c in combos if c]


def _option_is_file_gated(plugin: "Plugin") -> bool:
    """True when *plugin* is UNUSABLE unless a fileDependency holds - the only
    shape for which "the dep went away → the patch is orphaned" is true.

    Both authoring styles count:
      * ``defaultType="NotUsable"`` flipped usable by a pattern, and
      * a usable default plus a file-driven ``NotUsable`` pattern (the mirror).
    A plain ``Optional``/``Recommended`` default with no NotUsable anywhere is a
    standalone option whose patterns are only pre-check hints.
    """
    td = plugin.type_descriptor
    if td.default_type == "NotUsable":
        return True
    return any(t == "NotUsable" and _dep_references_file(dep)
               for dep, t in td.patterns)


def _collect_dep_plugin_clauses(config: ModuleConfig, all_selections: dict,
                                want_selected: bool) -> list[str]:
    """Shared walk for the two dep collectors. Returns one string PER OPTION-CONDITION
    from the ``fileDependency`` type patterns of options that were SELECTED
    (``want_selected=True``) or NOT selected (``False``).

    Each string is an OR-of-ANDs (the original pattern shape, preserved so OR
    conditions evaluate correctly), using the FLAG_* delimiters (all
    filename-illegal so plugin names with "+"/"!" don't collide):
      * FLAG_AND_SEP ("<") joins an AND-clause (all plugins must hold),
      * FLAG_OR_SEP ("|") joins the OR alternatives of ONE option,
      * FLAG_ABSENT (">") prefixes a name that must be ABSENT (state="Missing").
    e.g. ``UntarnishedUI_Subtitle.esp|UntarnishedUI_Blur.esp`` (OR),
         ``Thaumaturgy.esp<>gaunt.esl`` (AND with a Missing gate).
    The caller FLAG_OPT_SEP (";")-joins these per-option strings.

    Mirrors :func:`resolve_files`: invisible steps (unsatisfied visibility
    conditions) are skipped, SelectAll groups count every plugin as selected.
    Deduped case-insensitively.
    """
    all_selections = all_selections or {}
    option_conditions: list[str] = []
    seen: set[str] = set()
    # Replay flag state so step-visibility gates match what the user actually saw.
    flag_state: dict[str, str] = {}
    for i, step in enumerate(config.steps):
        if step.visible_condition is not None:
            if not evaluate_dependency(step.visible_condition, flag_state,
                                       set(), None, version_pass=True):
                continue
        step_selections = all_selections.get(str(i)) or \
            all_selections.get(step.name, {})
        for group in step.groups:
            selected_names = set(step_selections.get(group.name, []))
            for plugin in group.plugins:
                is_selected = (group.group_type == "SelectAll"
                               or plugin.name in selected_names)
                if is_selected:
                    flag_state.update(plugin.condition_flags)
                if is_selected != want_selected:
                    continue
                # On the SELECTED side the flag means "this patch is now orphaned",
                # which only makes sense for an option that is UNUSABLE without its
                # deps. An option that is usable on its own and merely gets promoted
                # to Recommended/Required when some plugin is around (defaultType
                # "Optional" + a Recommended pattern - e.g. CACO's main file vs
                # WACCF) is NOT gated: losing that plugin orphans nothing, so
                # recording it would fire the flag on a perfectly valid install.
                if want_selected and not _option_is_file_gated(plugin):
                    continue
                # Collect the OR-of-AND clauses from this plugin's type patterns
                # that make the option USABLE/relevant. A "NotUsable" pattern marks
                # the OPPOSITE (when the option is broken), so merging it in would
                # produce a nonsense always-true condition (e.g. an option with
                # "Recommended when SurWR.esp Active" + "NotUsable when SurWR.esp
                # Missing/Inactive" → "SurWR.esp | !SurWR.esp", always true → flag
                # fires on install). Skip NotUsable patterns.
                clauses: list[list[str]] = []
                for pattern_dep, _t in plugin.type_descriptor.patterns:
                    if _t == "NotUsable":
                        continue
                    # An Inactive ("present but disabled") gate can't be encoded in
                    # the enabled/absent clause format - dropping it would leave a
                    # WEAKER clause that fires the flag when the option isn't really
                    # selectable (flag on, nothing blue in the wizard). Skip the
                    # whole pattern so the flag stays consistent with the wizard.
                    if _pattern_has_inactive_plugin(pattern_dep):
                        continue
                    clauses.extend(_pattern_dep_groups(pattern_dep))
                clauses = [c for c in clauses if c]
                if not clauses:
                    continue
                cond = FLAG_OR_SEP.join(
                    FLAG_AND_SEP.join(c) for c in clauses)
                key = cond.lower()
                if key in seen:
                    continue
                seen.add(key)
                option_conditions.append(cond)
    return option_conditions


def collect_unselected_dep_plugins(config: ModuleConfig,
                                   all_selections: dict) -> list[str]:
    """One OR-of-ANDs condition string per UNSELECTED option (see
    :func:`_collect_dep_plugin_clauses` for the FLAG_* delimiter format). The
    caller ``;``-joins these; the pending flag fires when ANY option's condition
    becomes fully satisfiable - a patch you skipped is now relevant.

    These are patches the FOMOD would offer (or make required/recommended) *if*
    the named plugin(s) were present - so if they appear later the FOMOD is worth
    re-running.
    """
    return _collect_dep_plugin_clauses(config, all_selections,
                                       want_selected=False)


def collect_selected_dep_plugins(config: ModuleConfig,
                                 all_selections: dict) -> list[str]:
    """One OR-of-ANDs condition string per SELECTED option - the plugins those
    installed patches depend on. Same FLAG_* delimiter format as
    :func:`collect_unselected_dep_plugins`. The active flag fires when ANY option's
    condition is NO LONGER satisfied (none of its OR alternatives hold) - the
    installed patch is now orphaned/invalid, so rerun to drop it.
    """
    return _collect_dep_plugin_clauses(config, all_selections,
                                       want_selected=True)


def _dep_references_file(dep: "Dependency") -> bool:
    """True if *dep*'s tree contains any fileDependency leaf (of any state) - i.e.
    its satisfaction depends on the presence/absence of a plugin or asset. Used to
    decide whether an option is 'plugin-driven' (dimmable) at all."""
    if dep is None:
        return False
    if dep.dep_type == "file":
        return True
    if dep.dep_type == "composite":
        return any(_dep_references_file(s) for s in dep.sub_deps)
    return False


def _file_only_projection(dep: "Dependency") -> "Dependency | None":
    """Reduce *dep* to just its file-referencing part, dropping flag/version
    leaves - so a MIXED pattern like ``And(flag HD=active, file "Lux Via.esp"
    Active, flag Lux=inactive)`` is judged on its FILE gate alone.

    This exists because :func:`plugin_dep_unmet`/:func:`plugin_dep_met` run at
    STYLE time, before any in-wizard selection has set a flag. Evaluating a
    mixed ``And`` whole would always fail on those empty flags and falsely dim
    an option whose file gate is actually satisfied (GH: Embers XD "Lux Via").
    The docstrings promise "we only want to dim on FILE state" - this makes it
    true.

    Rules (semantics-preserving for dim/highlight intent):
      * ``file`` leaf  → returned as-is.
      * ``And``        → keep only the file-referencing sub-deps. In an AND the
        file part is a NECESSARY condition, so "file part holds" is exactly the
        right signal; the flag/version members are dropped.
      * ``Or``         → project only when EVERY alternative is file-referencing.
        If any alternative is flag/version-only, the option is satisfiable
        without a file (e.g. ``Or(file X Active, flag Lux=active)``), so it is
        NOT a pure file gate - return None so it is treated as non-dimmable.
      * ``flag``/``version``/None → None (not file-driven).
    """
    if dep is None:
        return None
    if dep.dep_type == "file":
        return dep
    if dep.dep_type != "composite":
        return None
    op = (dep.operator or "And").lower()
    if op == "or":
        # A file gate is the sole path to satisfaction only if no alternative is
        # a bare flag/version - otherwise the option can be reached without the
        # file, so it must not dim.
        projected = [_file_only_projection(s) for s in dep.sub_deps]
        if not projected or any(p is None for p in projected):
            return None
        return Dependency(dep_type="composite", operator="Or",
                          sub_deps=[p for p in projected])
    # And (default): keep only the file-referencing members.
    kept = [_file_only_projection(s) for s in dep.sub_deps
            if _dep_references_file(s)]
    kept = [k for k in kept if k is not None]
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return Dependency(dep_type="composite", operator="And", sub_deps=kept)


def plugin_dep_unmet(plugin: "Plugin", active_files: set[str] | None,
                     installed_files: set[str] | None = None,
                     loose_files: set[str] | None = None) -> bool:
    """True when *plugin* has a fileDependency-driven type pattern whose FILE
    gate is NOT currently satisfied - used to dim (as a hint, not lock) an
    option whose plugin condition isn't met.

    Only the file part of each pattern is evaluated (see
    :func:`_file_only_projection`): a mixed ``And(flag …, file X Active)`` is
    judged on ``file X Active`` alone, because flags are only set by in-wizard
    selections and are empty at style time. This honours multi-file conditions
    correctly - ``And(Thaumaturgy.esp Active, gaunt.esl Missing)`` is met only
    when Thaumaturgy is active AND gaunt is absent. Flag/version-only patterns
    are not plugin-driven and never dim the option.

    Returns False when no pattern has a file gate at all, or when at least one
    file gate currently evaluates True.
    """
    active = active_files or set()
    installed = installed_files if installed_files is not None else active
    saw_file_dep = False
    for pattern_dep, _type_name in plugin.type_descriptor.patterns:
        file_dep = _file_only_projection(pattern_dep)
        if file_dep is None:
            continue
        saw_file_dep = True
        # version_pass=True: unknown engine/extender version gates are lenient,
        # matching resolve_plugin_type - we only want to dim on FILE state.
        if evaluate_dependency(file_dep, {}, installed, active,
                               version_pass=True, loose_files=loose_files):
            return False   # a file gate is satisfied → not unmet
    return saw_file_dep


def plugin_dep_met(plugin: "Plugin", active_files: set[str] | None,
                   installed_files: set[str] | None = None,
                   loose_files: set[str] | None = None) -> bool:
    """True when *plugin* HAS a file gate in its type pattern and that gate is
    currently SATISFIED - i.e. the option is gated on a plugin and that gate is
    now met. Used to highlight options that became available since the last run
    (blue on rerun). Only the file part is evaluated (see
    :func:`_file_only_projection`) so mixed flag+file ``And`` patterns are judged
    on their file gate, not on flags that are empty at style time. Options with
    no file gate return False (they were always available, not 'newly' so)."""
    active = active_files or set()
    installed = installed_files if installed_files is not None else active
    for pattern_dep, _type_name in plugin.type_descriptor.patterns:
        file_dep = _file_only_projection(pattern_dep)
        if file_dep is None:
            continue
        if evaluate_dependency(file_dep, {}, installed, active,
                               version_pass=True, loose_files=loose_files):
            return True
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_selections(step: InstallStep,
                        selections: dict[str, list[str]],
                        flag_state: dict[str, str] | None = None,
                        installed_files: set[str] | None = None,
                        active_files: set[str] | None = None,
                        loose_files: set[str] | None = None) -> list[str]:
    """
    Check if current selections satisfy each group's type constraint.
    Returns a list of error messages (empty = all valid).

    When the resolution context (flag_state / installed_files / active_files)
    is supplied, "select one/at-least-one" requirements are waived for groups
    in which every plugin resolves to NotUsable - such a group is impossible
    to satisfy and must not hard-block the install. This mirrors MO2, which
    lets the user proceed past a degenerate all-NotUsable group.
    """
    errors: list[str] = []
    inst = installed_files or set()
    flags = flag_state or {}

    for group in step.groups:
        selected = selections.get(group.name, [])
        count = len(selected)
        gtype = group.group_type

        # A group is "satisfiable" only if at least one plugin is selectable
        # (not NotUsable). Without the resolution context we assume it is, to
        # preserve the original strict behaviour for callers that don't pass it.
        if flag_state is None and installed_files is None and active_files is None:
            satisfiable = True
        else:
            satisfiable = any(
                resolve_plugin_type(p, flags, inst, active_files, loose_files) != "NotUsable"
                for p in group.plugins
            )

        if gtype == "SelectExactlyOne":
            if satisfiable and count != 1:
                errors.append(f'"{group.name}": select exactly one option.')
        elif gtype == "SelectAtLeastOne":
            if satisfiable and count < 1:
                errors.append(f'"{group.name}": select at least one option.')
        elif gtype == "SelectAtMostOne" and count > 1:
            errors.append(f'"{group.name}": select at most one option.')
        # SelectAny and SelectAll have no constraint to enforce here

    return errors
