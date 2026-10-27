from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PREFIX = "manager_plugin:"


@dataclass(frozen=True)
class Wizard:
    id: str
    label: str
    entrypoint: Path
    description: str = ""
    category: str = ""
    panel: str = "plugins"


def declarations(data, root, entrypoint, ui, check_files):
    from .catalog import _read_context_menu_actions
    rows = data.get("wizards", [])
    if not isinstance(rows, list) or len(rows) > 32 or rows and ui != "qt":
        raise ValueError("wizards requires ui: qt and at most 32 declarations.")
    output, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid wizard declaration.")
        action, = _read_context_menu_actions({"context_menu_actions": [{
            "id": row.get("id"), "label": row.get("label"), "locations": ["modlist"],
            "entrypoint": row.get("entrypoint", str(entrypoint.relative_to(root.resolve())))
        }]}, root, entrypoint, check_files)
        if (action.id in ids or row.get("panel", "plugins") not in ("plugins", "modlist", "full")
                or any(not isinstance(row.get(key, ""), str) or len(row.get(key, "")) > 4000
                       for key in ("description", "category"))):
            raise ValueError("Wizard IDs must be unique and panel must be plugins, modlist or full.")
        ids.add(action.id)
        output.append(Wizard(action.id, action.label, action.entrypoint,
                             row.get("description", ""), row.get("category", ""), row.get("panel", "plugins")))
    return tuple(output)


def tools(game_id):
    from Games.base_game import WizardTool
    from .catalog import discover
    from .i18n import translate
    plugins, _ = discover(game_id)
    return [WizardTool(id=f"{PREFIX}{p.id}/{w.id}", label=translate(p, w.label),
                       description=translate(p, w.description), category=translate(p, w.category),
                       dialog_class_path=f"{PREFIX}{p.id}/{w.id}")
            for p in plugins for w in p.wizards]


def specification(key):
    from .catalog import discover
    from wizards_qt import QtWizardSpec
    plugins, _ = discover(None)
    for plugin in plugins:
        for wizard in plugin.wizards:
            if key == f"{PREFIX}{plugin.id}/{wizard.id}":
                def factory(game, *, log_fn, on_close, ctx, _plugin=plugin, _wizard=wizard, **extra):
                    from gui_qt.runtime.plugin_wizards import PluginWizard
                    return PluginWizard(_plugin, _wizard, ctx.plugin_context_factory, on_close, log_fn)
                return QtWizardSpec(factory, panel=wizard.panel)
    return None
