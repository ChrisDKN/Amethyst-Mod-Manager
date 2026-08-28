"""Read back the FOMOD choices made when a mod was installed.

Installs mirror the wizard's selections to ``<profile>/fomod/<mod>.json``
(``{step: {group: [plugin, ...]}}``) and the installer's ``ModuleConfig.xml``
to ``<profile>/fomod/<mod>.xml``. The JSON alone only carries the names the
sidecar happened to record; pairing it with the config recovers the wizard's
page order, the groups the user did NOT pick, the selection type of each group
and the option descriptions - i.e. what the wizard actually looked like.

GUI-neutral: returns plain dataclasses, imports nothing from gui_qt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChoiceOption:
    """One plugin row inside a group."""
    name: str
    selected: bool
    description: str = ""


@dataclass
class ChoiceGroup:
    """One group (question) on a wizard page."""
    name: str
    group_type: str = ""
    options: list[ChoiceOption] = field(default_factory=list)

    @property
    def selected_names(self) -> list[str]:
        return [o.name for o in self.options if o.selected]


@dataclass
class ChoiceStep:
    """One wizard page."""
    name: str
    groups: list[ChoiceGroup] = field(default_factory=list)

    @property
    def has_selection(self) -> bool:
        return any(g.selected_names for g in self.groups)


@dataclass
class FomodChoices:
    """A mod's recorded install choices, ready to display."""
    mod_name: str
    steps: list[ChoiceStep] = field(default_factory=list)
    # True when the ModuleConfig was available, so unselected options and the
    # authored page/group order are real rather than reconstructed from names.
    from_config: bool = False
    source: str = ""          # path the selections came from (for the footer)

    @property
    def is_empty(self) -> bool:
        return not any(s.has_selection for s in self.steps)


# ---------------------------------------------------------------------------
# Sidecar lookup
# ---------------------------------------------------------------------------

def selection_path(mod_name: str, profile_dir, game_name: str = ""):
    """The sidecar holding *mod_name*'s selections, or None.

    Profile-local first (what the installer mirrors and what export reads),
    then the global per-game copy for mods installed before the mirror existed.
    """
    candidates = []
    if profile_dir:
        candidates.append(Path(profile_dir) / "fomod" / f"{mod_name}.json")
    if game_name:
        try:
            from Utils.config_paths import get_fomod_selections_path
            candidates.append(get_fomod_selections_path(game_name, mod_name))
        except Exception:
            pass
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def has_choices(mod_name: str, profile_dir, game_name: str = "") -> bool:
    """True when a FOMOD selection sidecar exists for *mod_name* (gates the
    right-click entry, so it must stay cheap - one or two stat() calls)."""
    return selection_path(mod_name, profile_dir, game_name) is not None


def _load_selections(path: Path) -> "dict | None":
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_config(mod_name: str, profile_dir):
    """The mod's parsed ModuleConfig from the profile copy, or None.

    Deliberately does not fall back to re-reading the archive: this is a read
    only viewer and the archive may be huge, gone, or on slow storage.
    """
    if not profile_dir:
        return None
    saved = Path(profile_dir) / "fomod" / f"{mod_name}.xml"
    try:
        if not saved.is_file():
            return None
    except OSError:
        return None
    try:
        from Utils.fomod_parser import parse_module_config
        return parse_module_config(str(saved))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Matching sidecar keys back to the config
# ---------------------------------------------------------------------------

def _resolve_group(step, group_name: str, plugin_names: list):
    """The config group matching *group_name*; for a blank or unmatched name,
    the unique group that contains every selected plugin. Mirrors
    collection_export._fomod_options - sidecars key groups loosely (stripped
    names, and Vortex-written manifests leave single-group pages unnamed)."""
    gkey = (group_name or "").strip()
    if gkey:
        for g in step.groups:
            if (g.name or "").strip() == gkey:
                return g
            if (getattr(g, "display_name", "") or "").strip() == gkey:
                return g
    wanted = [(p or "").strip() for p in plugin_names]
    cands = []
    for g in step.groups:
        have = {(p.name or "").strip() for p in g.plugins}
        if all(w in have for w in wanted):
            cands.append(g)
    return cands[0] if len(cands) == 1 else None


def _resolve_step(steps, key, groups_sel: dict):
    """The config step for one sidecar entry, content-verified: every selected
    group (and its plugins) must exist in the step we return. Steps are keyed
    by index, but a skipped (condition-hidden) page drifts that index, so the
    match is confirmed against the contents before it is trusted."""
    def _ok(step):
        return all(_resolve_group(step, gn, pl) is not None
                   for gn, pl in (groups_sel or {}).items())

    kstr = (str(key) if key is not None else "").strip()
    step = next((s for s in steps if s.name == key), None)
    if step is None and kstr:
        step = next((s for s in steps if (s.name or "").strip() == kstr), None)
    if step is not None and _ok(step):
        return step
    try:
        idx = int(kstr)
    except (TypeError, ValueError):
        idx = -1
    if 0 <= idx < len(steps) and _ok(steps[idx]):
        return steps[idx]
    cands = [s for s in steps if _ok(s)]
    return cands[0] if len(cands) == 1 else None


def _from_config(selections: dict, config) -> "list[ChoiceStep] | None":
    """Full view: every page/group/option from the config, with the recorded
    picks flagged. None when the sidecar can't be mapped onto this config."""
    steps = getattr(config, "steps", None) or []
    if not steps:
        return None

    # step index -> {group index -> set(selected plugin names)}
    picked: dict = {}
    for key, groups_sel in selections.items():
        groups_sel = groups_sel if isinstance(groups_sel, dict) else {}
        step = _resolve_step(steps, key, groups_sel)
        if step is None:
            return None
        si = steps.index(step)
        for group_name, plugin_names in groups_sel.items():
            plugin_names = plugin_names if isinstance(plugin_names, list) else []
            group = _resolve_group(step, group_name, plugin_names)
            if group is None:
                return None
            gi = step.groups.index(group)
            names = {(p or "").strip() for p in plugin_names}
            picked.setdefault(si, {}).setdefault(gi, set()).update(names)

    # Pages the wizard never reached leave no sidecar entry at all; showing
    # them as "everything unselected" would read as deliberate opt-outs, so
    # only visited pages are rendered.
    out: list[ChoiceStep] = []
    for si, step in enumerate(steps):
        if si not in picked:
            continue
        groups_out: list[ChoiceGroup] = []
        for gi, group in enumerate(step.groups):
            sel = picked[si].get(gi, set())
            options = [
                ChoiceOption(name=p.name,
                             selected=(p.name or "").strip() in sel,
                             description=(p.description or "").strip())
                for p in group.plugins
            ]
            groups_out.append(ChoiceGroup(
                name=(getattr(group, "display_name", "") or group.name),
                group_type=group.group_type or "",
                options=options))
        out.append(ChoiceStep(name=step.name, groups=groups_out))
    return out


def _from_selections(selections: dict) -> list[ChoiceStep]:
    """Fallback view when no ModuleConfig copy survives: the selections alone.
    Only picked options are known, so nothing unselected can be shown."""
    def _sort_key(item):
        try:
            return (0, int(str(item[0]).strip()), "")
        except (TypeError, ValueError):
            return (1, 0, str(item[0]))

    out: list[ChoiceStep] = []
    for key, groups_sel in sorted(selections.items(), key=_sort_key):
        groups_sel = groups_sel if isinstance(groups_sel, dict) else {}
        groups_out = []
        for group_name, plugin_names in groups_sel.items():
            plugin_names = plugin_names if isinstance(plugin_names, list) else []
            groups_out.append(ChoiceGroup(
                name=(group_name or "").strip(),
                options=[ChoiceOption(name=n, selected=True)
                         for n in plugin_names]))
        kstr = str(key).strip()
        # A bare index is meaningless on its own - label it as a page number.
        name = kstr if not kstr.isdigit() else ""
        out.append(ChoiceStep(name=name, groups=groups_out))
    return out


def load_choices(mod_name: str, profile_dir,
                 game_name: str = "") -> "FomodChoices | None":
    """*mod_name*'s recorded FOMOD choices, or None when it has no sidecar."""
    path = selection_path(mod_name, profile_dir, game_name)
    if path is None:
        return None
    selections = _load_selections(path)
    if selections is None:
        return None
    config = _load_config(mod_name, profile_dir)
    steps = _from_config(selections, config) if config is not None else None
    if steps is None:
        return FomodChoices(mod_name=mod_name,
                            steps=_from_selections(selections),
                            from_config=False, source=str(path))
    return FomodChoices(mod_name=mod_name, steps=steps,
                        from_config=True, source=str(path))
