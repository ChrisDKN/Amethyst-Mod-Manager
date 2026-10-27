from __future__ import annotations

from Utils import json_limits as json
import re
from dataclasses import dataclass
from pathlib import Path


FOOTER_LOCATIONS = (
    "modlist_footer", "plugins_footer", "mod_files_footer", "text_files_footer",
    "data_footer", "downloads_footer", "overrides_footer", "saves_footer",
)

CONTEXT_MENU_LOCATIONS = (
    "modlist", "plugins", "mod_files", "text_files", "data", "downloads",
    "overrides", "saves", "nexus", "thunderstore", "collections", "wabbajack",
    "tabs", "text", "launcher", "notifications", "preview", "suggestions",
)
CONTEXT_ITEM_TYPES = (
    "external_mod", "external_separator",
    "mod", "separator", "overwrite", "root_folder", "game_plugin", "file",
    "directory", "save", "nexus_mod", "thunderstore_package", "collection",
    "modlist", "tab", "text", "launcher", "notification", "preview", "suggestion",
)
LIFECYCLE_EVENTS = frozenset({"before_archive", "after_extract", "before_install", "after_install",
                              "before_deploy", "after_deploy"})


@dataclass(frozen=True)
class ContextMenuAction:
    id: str
    label: str
    locations: tuple[str, ...]
    entrypoint: Path
    item_types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    min_selection: int = 0
    max_selection: int | None = None


@dataclass(frozen=True)
class UIExtension:
    id: str
    label: str
    type: str
    location: str
    icon: Path | None = None
    action: str = "open_plugin"
    target: str = ""
    game_visibility: str = ""


@dataclass(frozen=True)
class ModlistSection:
    id: str
    label: str
    actions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Flag:
    id: str
    label: str
    icon: Path
    panels: tuple[str, ...]
    tooltip: str


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    panels: tuple[str, ...]
    type: str = "text"
    width: int = 120


@dataclass(frozen=True)
class Filter:
    id: str
    label: str
    panels: tuple[str, ...]


@dataclass(frozen=True)
class FilePreview:
    id: str
    label: str
    extensions: tuple[str, ...]
    entrypoint: Path
    priority: int = 0


@dataclass(frozen=True)
class Command:
    id: str
    label: str
    action: str
    target: str
    context: ContextMenuAction
    toolbar: bool = False
    context_menu: bool = False


@dataclass(frozen=True)
class Installer:
    id: str
    label: str
    entrypoint: Path
    extensions: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ()
    priority: int = 0
    interactive: bool = False
    formats: tuple[str, ...] = ("plain",)


@dataclass(frozen=True)
class DownloadProvider:
    id: str
    label: str
    entrypoint: Path
    schemes: tuple[str, ...]
    hosts: tuple[str, ...] = ()
    download_entrypoint: Path | None = None
    repository_entrypoint: Path | None = None
    protocols: tuple[str, ...] = ()


@dataclass(frozen=True)
class LauncherProvider:
    id: str
    label: str
    entrypoint: Path
    icon: Path | None = None


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    entrypoint: Path | None
    methods: tuple[str, ...]
    settings: tuple[dict, ...] = ()
    ui: str = "stdio"
    ui_extensions: tuple[UIExtension, ...] = ()
    service: bool = False
    directory: Path | None = None
    version: str = ""
    author: str = ""
    min_manager_version: str = ""
    context_menu_actions: tuple[ContextMenuAction, ...] = ()
    launch_hooks: Path | None = None
    menu_tab: str = ""
    flags: tuple[Flag, ...] = ()
    flag_provider: Path | None = None
    icon_overrides: tuple[tuple[str, Path], ...] = ()
    icon_settings: tuple[tuple[str, str], ...] = ()
    columns: tuple[Column, ...] = ()
    column_provider: Path | None = None
    filters: tuple[Filter, ...] = ()
    filter_provider: Path | None = None
    diagnostic_provider: Path | None = None
    lifecycle_hooks: tuple[tuple[str, Path], ...] = ()
    file_previews: tuple[FilePreview, ...] = ()
    commands: tuple[Command, ...] = ()
    installers: tuple[Installer, ...] = ()
    download_providers: tuple[DownloadProvider, ...] = ()
    requires_game: bool = True
    launcher_providers: tuple[LauncherProvider, ...] = ()
    persistent_directories: tuple[str, ...] = ()
    game_handlers: tuple[Path, ...] = ()
    themes: tuple[tuple[str, Path], ...] = ()
    show_in_menu: bool = True
    modlist_sections: tuple[ModlistSection, ...] = ()
    warnings: tuple[str, ...] = ()
    wizards: tuple = ()
    operation_guards: tuple[tuple[str, Path], ...] = ()
    optional_methods: tuple[str, ...] = ()
    plugin_dependencies: tuple = ()
    plugin_conflicts: tuple[str, ...] = ()
    load_after: tuple[str, ...] = ()

    @property
    def available_methods(self):
        from amethyst_backend.methods import METHOD_NAMES
        return tuple(dict.fromkeys((*self.methods, *(m for m in self.optional_methods if m in METHOD_NAMES))))


def _launcher_providers(data, root, check_files):
    items = data.get("launcher_providers", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 launcher providers.")
    providers, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid launcher provider.")
        pid, label, name = item.get("id"), item.get("label"), item.get("entrypoint")
        if (not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", pid)
                or pid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200 or not isinstance(name, str) or not name
                or Path(name).is_absolute() or "\\" in name or ".." in Path(name).parts):
            raise ValueError("Launcher providers require a unique id, label and plugin-relative Python file.")
        path = (root / name).resolve()
        if (not path.is_relative_to(root.resolve()) or path.suffix != ".py"
                or check_files and not path.is_file()):
            raise ValueError("Launcher provider must be a Python file inside the plugin folder.")
        image = None
        if "icon" in item:
            asset = item["icon"]
            if (not isinstance(asset, str) or not asset or Path(asset).is_absolute()
                    or "\\" in asset or ".." in Path(asset).parts):
                raise ValueError("Launcher provider icons must be plugin-relative files.")
            image = (root / asset).resolve()
            if not image.is_relative_to(root.resolve()) or check_files and not image.is_file():
                raise ValueError("Launcher provider icons must be files inside the plugin folder.")
        ids.add(pid)
        providers.append(LauncherProvider(pid, label.strip(), path, image))
    return tuple(providers)


def _persistent_directories(data):
    items = data.get("persistent_directories", [])
    if (not isinstance(items, list) or len(items) > 32 or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", name)
            or name == "__pycache__" for name in items) or len(set(items)) != len(items)):
        raise ValueError("persistent_directories must contain unique top-level directory names.")
    return tuple(items)


def version_key(value):
    if not isinstance(value, str):
        raise ValueError("Expected a SemVer version string.")
    match = re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", value)
    if match is None:
        raise ValueError(f"Invalid SemVer version: {value}")
    prerelease = match[4]
    parts = prerelease.split(".") if prerelease else []
    if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in parts):
        raise ValueError(f"Invalid SemVer prerelease: {value}")
    return (tuple(int(match[i]) for i in (1, 2, 3)), not prerelease,
            tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts))


def compatibility_error(plugin):
    from version import __version__
    from amethyst_backend.methods import METHOD_NAMES
    if plugin.min_manager_version:
        current = version_key(__version__)
        minimum = version_key(plugin.min_manager_version)
        if minimum[1]:
            current = (current[0], True, ())
        if current < minimum:
            return f"Requires Amethyst {plugin.min_manager_version} or newer (installed: {__version__})."
    missing = set(plugin.methods) - set(METHOD_NAMES)
    if missing:
        return "Unavailable required backend methods: " + ", ".join(sorted(missing))
    return ""


def _read_ui_extensions(data, root, ui):
    items = data.get("ui_extensions", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 UI extensions.")
    extensions, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid UI extension.")
        eid, label = item.get("id"), item.get("label")
        if (not isinstance(eid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", eid)
                or eid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200):
            raise ValueError("UI extensions need a unique id and a nonempty label (at most 200 characters).")
        kind, location = item.get("type"), item.get("location")
        if kind not in ("button", "widget") or location not in (
                "toolbar", "providers", "tab", "right_panel", "bottom_panel", "plugins_panel", *FOOTER_LOCATIONS):
            raise ValueError("Invalid UI extension type or location.")
        if kind == "button" and location not in ("toolbar", "providers", *FOOTER_LOCATIONS):
            raise ValueError("UI buttons must use the toolbar, providers, or a footer location.")
        if location in (*FOOTER_LOCATIONS, "providers") and kind != "button":
            raise ValueError("Provider and footer locations support buttons only.")
        if kind == "widget" and ui != "qt":
            raise ValueError("Custom widgets require ui: qt.")
        icon_path = None
        if "icon" in item:
            name = item["icon"]
            if not isinstance(name, str) or not name or Path(name).is_absolute():
                raise ValueError("UI extension icons must be plugin-relative paths.")
            icon_path = (root / name).resolve()
            if not icon_path.is_relative_to(root.resolve()):
                raise ValueError("UI extension icons must remain inside the plugin folder.")
        action, target = item.get("action", "open_plugin"), item.get("target", "")
        if action not in ("open_plugin", "open_tab", "run_command") or not isinstance(target, str):
            raise ValueError("Invalid UI extension action.")
        visibility = item.get("game_visibility", "")
        if (not isinstance(visibility, str) or visibility
                and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", visibility)):
            raise ValueError("game_visibility must name a plugin data key.")
        ids.add(eid)
        extensions.append(UIExtension(eid, label, kind, location, icon_path, action, target, visibility))
    tabs = {e.id for e in extensions if e.location == "tab"}
    if any(e.type == "button" and e.action == "open_tab" and e.target not in tabs
           for e in extensions):
        raise ValueError("open_tab must target a tab declared by the same plugin.")
    return tuple(extensions)


def plugins_dir():
    from Utils.config_paths import get_plugins_dir
    return get_plugins_dir()


def _read_modlist_sections(data, ui):
    items = data.get("modlist_sections", [])
    if not isinstance(items, list) or len(items) > 16 or items and ui != "qt":
        raise ValueError("modlist_sections requires Qt and at most 16 sections.")
    sections, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "label", "actions"}:
            raise ValueError("Invalid modlist section.")
        sid, label = item.get("id"), item.get("label")
        if (not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", sid)
                or sid in ids or not isinstance(label, str) or not label.strip() or len(label) > 200):
            raise ValueError("Modlist sections require unique IDs and nonempty labels.")
        actions, action_ids = [], set()
        declarations = item.get("actions", [])
        if not isinstance(declarations, list) or len(declarations) > 32:
            raise ValueError("Expected at most 32 section actions.")
        for action in declarations:
            if not isinstance(action, dict) or set(action) - {"id", "label", "selection"}:
                raise ValueError("Invalid section action.")
            aid, text, selection = action.get("id"), action.get("label"), action.get("selection", "items")
            if (not isinstance(aid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", aid)
                    or aid in action_ids or not isinstance(text, str) or not text.strip()
                    or len(text) > 200 or selection not in ("items", "section")):
                raise ValueError("Invalid section action ID, label, or selection.")
            action_ids.add(aid)
            actions.append((aid, text, selection))
        ids.add(sid)
        sections.append(ModlistSection(sid, label, tuple(actions)))
    return tuple(sections)


def _read_context_menu_actions(data, root, entrypoint, check_files):
    items = data.get("context_menu_actions", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 context menu actions.")
    actions, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid context menu action.")
        aid, label = item.get("id"), item.get("label")
        if (not isinstance(aid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", aid)
                or aid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200):
            raise ValueError("Context menu actions need a unique id and a nonempty label (at most 200 characters).")
        locations = item.get("locations")
        if (not isinstance(locations, list) or not locations
                or any(not isinstance(v, str) or v not in CONTEXT_MENU_LOCATIONS for v in locations)):
            raise ValueError("Invalid context menu locations.")
        script = entrypoint
        if "entrypoint" in item:
            name = item["entrypoint"]
            if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
                raise ValueError("Context action entrypoints must be plugin-relative Python files.")
            script = (root / name).resolve()
        if (not script.is_relative_to(root.resolve()) or script.suffix != ".py"
                or check_files and not script.is_file()):
            raise ValueError("Context action entrypoints must be Python files inside the plugin folder.")
        filters = item.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - {
                "item_types", "extensions", "min_selection", "max_selection"}:
            raise ValueError("Invalid context menu filters.")
        kinds, extensions = filters.get("item_types", []), filters.get("extensions", [])
        if (not isinstance(kinds, list)
                or any(not isinstance(v, str) or v not in CONTEXT_ITEM_TYPES for v in kinds)):
            raise ValueError("Invalid context menu item types.")
        if (not isinstance(extensions, list) or any(
                not isinstance(v, str) or not re.fullmatch(r"\.[a-zA-Z0-9][a-zA-Z0-9._-]*", v)
                for v in extensions)):
            raise ValueError("Context menu extensions must start with a dot.")
        minimum, maximum = filters.get("min_selection", 0), filters.get("max_selection")
        if (type(minimum) is not int or minimum < 0
                or maximum is not None and (type(maximum) is not int or maximum < minimum)):
            raise ValueError("Invalid context menu selection limits.")
        ids.add(aid)
        actions.append(ContextMenuAction(aid, label, tuple(locations), script,
                                        tuple(kinds), tuple(v.lower() for v in extensions),
                                        minimum, maximum))
    return tuple(actions)


def _read_commands(data, root, entrypoint, extensions, check_files):
    items = data.get("commands", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin commands.")
    commands, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin command.")
        action, target = item.get("action", "run_script"), item.get("target", "")
        if action not in ("run_script", "open_plugin", "open_tab") or not isinstance(target, str):
            raise ValueError("Invalid command action or target.")
        if action == "open_tab" and not any(
                e.id == target and e.location == "tab" for e in extensions):
            raise ValueError("Command open_tab must target a declared tab.")
        if action != "open_tab" and target:
            raise ValueError("Only open_tab commands accept a target.")
        if any(type(item.get(key, False)) is not bool for key in ("toolbar", "context_menu")):
            raise ValueError("Command toolbar and context_menu must be boolean.")
        declaration = {key: item[key] for key in ("id", "label", "entrypoint", "filters") if key in item}
        declaration["locations"] = item.get("locations", list(CONTEXT_MENU_LOCATIONS))
        context, = _read_context_menu_actions({"context_menu_actions": [declaration]}, root, entrypoint, check_files)
        if context.id in ids:
            raise ValueError("Command IDs must be unique.")
        ids.add(context.id)
        commands.append(Command(context.id, context.label, action, target, context,
                                item.get("toolbar", False), item.get("context_menu", False)))
    if any(e.action == "run_command" and (e.type != "button" or e.target not in ids) for e in extensions):
        raise ValueError("run_command buttons must target a command declared by the same plugin.")
    return tuple(commands)


def _read_installers(data, root, entrypoint, check_files):
    items = data.get("installers", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin installers.")
    output, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin installer.")
        action = {key: item[key] for key in ("id", "label", "entrypoint") if key in item}
        action["locations"] = ["downloads"]
        declaration, = _read_context_menu_actions({"context_menu_actions": [action]}, root, entrypoint, check_files)
        if declaration.id in ids:
            raise ValueError("Installer IDs must be unique.")
        extensions = item.get("extensions", [])
        if (not isinstance(extensions, list) or len(extensions) > 32 or any(
                not isinstance(ext, str) or not re.fullmatch(r"\.[a-zA-Z0-9][a-zA-Z0-9._-]*", ext) for ext in extensions)):
            raise ValueError("Installer extensions must start with a dot.")
        markers = item.get("required_files", [])
        if (not isinstance(markers, list) or len(markers) > 32 or any(
                not isinstance(path, str) or len(path) > 4096 or "\\" in path or ":" in path
                or any(ord(char) < 32 for char in path)
                or any(part in ("", ".", "..") for part in path.split("/")) for path in markers)):
            raise ValueError("Installer required_files must contain safe archive-relative file paths.")
        priority = item.get("priority", 0)
        if type(priority) is not int or not -100 <= priority <= 100:
            raise ValueError("Installer priority must be an integer from -100 to 100.")
        interactive = item.get("interactive", False)
        formats = item.get("formats", ["plain"])
        if type(interactive) is not bool:
            raise ValueError("Installer interactive must be boolean.")
        if (not isinstance(formats, list) or not 1 <= len(formats) <= 32 or any(
                not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind)
                for kind in formats) or len(set(formats)) != len(formats)):
            raise ValueError("Installer formats must contain unique format identifiers.")
        ids.add(declaration.id)
        output.append(Installer(declaration.id, declaration.label, declaration.entrypoint,
                                tuple(ext.lower() for ext in extensions), tuple(markers), priority,
                                interactive, tuple(formats)))
    return tuple(output)


def _read_download_providers(data, root, entrypoint, check_files):
    items = data.get("download_providers", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin download providers.")
    providers, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin download provider.")
        action = {key: item[key] for key in ("id", "label", "entrypoint") if key in item}
        action["locations"] = ["downloads"]
        declaration, = _read_context_menu_actions({"context_menu_actions": [action]}, root, entrypoint, check_files)
        schemes, hosts = item.get("schemes"), item.get("hosts", [])
        if (not isinstance(schemes, list) or not 1 <= len(schemes) <= 16 or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9+.-]{0,31}", value) for value in schemes)):
            raise ValueError("Download providers require 1 to 16 URI schemes in lowercase.")
        if (not isinstance(hosts, list) or len(hosts) > 32 or any(
                not isinstance(value, str) or len(value) > 253 or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or ".." in value for value in hosts)):
            raise ValueError("Download provider hosts must be lowercase hostnames without wildcards or ports.")
        if declaration.id in ids:
            raise ValueError("Download provider IDs must be unique.")
        download_entrypoint = None
        if "download_entrypoint" in item:
            transfer_action = dict(action, entrypoint=item["download_entrypoint"])
            transfer, = _read_context_menu_actions({"context_menu_actions": [transfer_action]}, root, entrypoint, check_files)
            download_entrypoint = transfer.entrypoint
        ids.add(declaration.id)
        repository_entrypoint = None
        if "repository_entrypoint" in item:
            repository, = _read_context_menu_actions({"context_menu_actions": [dict(action, entrypoint=item["repository_entrypoint"])]}, root, entrypoint, check_files)
            repository_entrypoint = repository.entrypoint
        protocols = item.get("protocols", [])
        reserved = {"http", "https", "file", "ftp", "mailto", "nxm", "ror2mm", "modl", "steam"}
        if (not isinstance(protocols, list) or len(protocols) > 16
                or any(not isinstance(scheme, str) or scheme not in schemes or scheme in reserved for scheme in protocols)
                or len(protocols) != len(set(protocols))):
            raise ValueError("protocols must contain unique custom schemes declared in schemes; built-in schemes are reserved.")
        providers.append(DownloadProvider(declaration.id, declaration.label, declaration.entrypoint,
                                          tuple(schemes), tuple(hosts), download_entrypoint, repository_entrypoint, tuple(protocols)))
    return tuple(providers)


def _read_flags(data, root, check_files):
    items = data.get("flags", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin flags.")
    flags, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin flag.")
        fid, label = item.get("id"), item.get("label")
        if (not isinstance(fid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", fid)
                or fid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200):
            raise ValueError("Plugin flags need a unique id and a nonempty label (at most 200 characters).")
        panels = item.get("panels")
        if (not isinstance(panels, list) or not panels
                or any(p not in ("modlist", "plugins") for p in panels)):
            raise ValueError("Plugin flag panels must contain modlist or plugins.")
        tooltip = item.get("tooltip", label)
        if not isinstance(tooltip, str) or not tooltip.strip() or len(tooltip) > 4000:
            raise ValueError("Plugin flag tooltips must contain 1 to 4000 characters.")
        name = item.get("icon")
        if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
            raise ValueError("Plugin flag icons must be plugin-relative files.")
        icon = (root / name).resolve()
        if not icon.is_relative_to(root.resolve()) or check_files and not icon.is_file():
            raise ValueError("Plugin flag icons must be files inside the plugin folder.")
        flags.append(Flag(fid, label, icon, tuple(panels), tooltip))
        ids.add(fid)
    name = data.get("flag_provider")
    if not flags and name is None:
        return (), None
    if not flags or not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("Plugin flags require a plugin-relative Python flag_provider.")
    provider = (root / name).resolve()
    if (not provider.is_relative_to(root.resolve()) or provider.suffix != ".py"
            or check_files and not provider.is_file()):
        raise ValueError("flag_provider must be a Python file inside the plugin folder.")
    return tuple(flags), provider


def _read_columns(data, root, check_files):
    items = data.get("columns", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin columns.")
    columns, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin column.")
        cid, label, panels = item.get("id"), item.get("label"), item.get("panels")
        kind, width = item.get("type", "text"), item.get("width", 120)
        if (not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", cid)
                or cid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200 or not isinstance(panels, list) or not panels
                or any(p not in ("modlist", "plugins") for p in panels)
                or kind not in ("text", "number", "boolean")
                or type(width) is not int or not 40 <= width <= 600):
            raise ValueError("Columns require unique IDs, labels, panels, valid types and widths (40-600).")
        columns.append(Column(cid, label, tuple(panels), kind, width))
        ids.add(cid)
    name = data.get("column_provider")
    if not columns and name is None:
        return (), None
    if not columns or not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("Plugin columns require a plugin-relative Python column_provider.")
    provider = (root / name).resolve()
    if (not provider.is_relative_to(root.resolve()) or provider.suffix != ".py"
            or check_files and not provider.is_file()):
        raise ValueError("column_provider must be a Python file inside the plugin folder.")
    return tuple(columns), provider


def _read_filters(data, root, check_files):
    items = data.get("filters", [])
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Expected at most 32 plugin filters.")
    filters, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid plugin filter.")
        fid, label, panels = item.get("id"), item.get("label"), item.get("panels")
        if (not isinstance(fid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", fid)
                or fid in ids or not isinstance(label, str) or not label.strip()
                or len(label) > 200 or not isinstance(panels, list) or not panels
                or any(p not in ("modlist", "plugins") for p in panels)):
            raise ValueError("Filters require unique IDs, labels and modlist/plugins panels.")
        filters.append(Filter(fid, label, tuple(panels)))
        ids.add(fid)
    name = data.get("filter_provider")
    if not filters and name is None:
        return (), None
    if not filters or not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("Plugin filters require a plugin-relative Python filter_provider.")
    provider = (root / name).resolve()
    if (not provider.is_relative_to(root.resolve()) or provider.suffix != ".py"
            or check_files and not provider.is_file()):
        raise ValueError("filter_provider must be a Python file inside the plugin folder.")
    return tuple(filters), provider


def _read_file_previews(data, root, entrypoint, ui, check_files):
    items = data.get("file_previews", [])
    if not isinstance(items, list) or len(items) > 32 or items and ui != "qt":
        raise ValueError("Expected at most 32 file_previews, requiring ui: qt.")
    previews, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid file preview.")
        pid, label, extensions = item.get("id"), item.get("label"), item.get("extensions")
        priority = item.get("priority", 0)
        if (not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", pid)
                or pid in ids or not isinstance(label, str) or not label.strip() or len(label) > 200
                or not isinstance(extensions, list) or not 1 <= len(extensions) <= 32
                or any(not isinstance(ext, str) or not re.fullmatch(r"\.[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,49}", ext) for ext in extensions)
                or type(priority) is not int or not -100 <= priority <= 100):
            raise ValueError("Previews require unique IDs, labels, extension suffixes and a priority from -100 to 100.")
        name = item.get("entrypoint")
        path = entrypoint
        if name is not None:
            if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
                raise ValueError("Preview entrypoints must be plugin-relative Python files.")
            path = (root / name).resolve()
        if (path is None or not path.is_relative_to(root.resolve()) or path.suffix != ".py"
                or check_files and not path.is_file()):
            raise ValueError("Preview entrypoints must be Python files inside the plugin folder.")
        previews.append(FilePreview(pid, label, tuple(dict.fromkeys(ext.lower() for ext in extensions)), path, priority))
        ids.add(pid)
    return tuple(previews)


def _read_icon_overrides(data, root, check_files, settings):
    items = data.get("icon_overrides", {})
    if not isinstance(items, dict) or len(items) > 512:
        raise ValueError("Expected at most 512 icon overrides.")
    overrides, controls = [], []
    boolean_keys = {s["key"] for s in settings if s["type"] == "boolean"}
    for target, value in items.items():
        if isinstance(value, dict):
            if set(value) - {"path", "enabled_setting"}:
                raise ValueError("Invalid icon override fields.")
            name, setting = value.get("path"), value.get("enabled_setting")
            if not isinstance(setting, str) or setting not in boolean_keys:
                raise ValueError("Icon enabled_setting must name a declared boolean setting.")
            controls.append((target, setting))
        else:
            name = value
        for value in (target, name):
            if (not isinstance(value, str) or not value or "\\" in value or ":" in value
                    or Path(value).is_absolute()
                    or any(part in ("", ".", "..") for part in value.split("/"))):
                raise ValueError("Icon overrides must use relative paths without traversal.")
        if Path(target).suffix.lower() != ".png":
            raise ValueError("Icon override targets must name PNG assets relative to icons/.")
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or check_files and not path.is_file():
            raise ValueError("Icon replacements must be files inside the plugin folder.")
        overrides.append((target, path))
    return tuple(overrides), tuple(controls)


def _read_themes(data, root, check_files):
    items = data.get("themes", {})
    if not isinstance(items, dict) or len(items) > 32:
        raise ValueError("Expected at most 32 themes mapped from IDs to JSON files.")
    themes = []
    for tid, name in items.items():
        if (not isinstance(tid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", tid)
                or not isinstance(name, str) or not name or "\\" in name or ":" in name
                or Path(name).is_absolute()
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("Themes require valid IDs and plugin-relative JSON paths without traversal.")
        path = (root / name).resolve()
        if (not path.is_relative_to(root.resolve()) or path.suffix != ".json"
                or check_files and not path.is_file()):
            raise ValueError("Theme files must be JSON files inside the plugin folder.")
        themes.append((tid, path))
    return tuple(themes)


def read_plugin(path):
    data = json.read_json(path, max_bytes=65536)
    return parse_plugin(data, path.parent)


def parse_plugin(data, directory, *, check_files=True, check_compatibility=True, store=False):
    from .diagnostics import manifest_warnings
    directory = Path(directory)
    if not isinstance(data, dict) or type(data.get("api_version")) is not int or data["api_version"] != 1:
        raise ValueError("Expected plugin api_version 1.")
    plugin_id = data.get("id", "")
    if not isinstance(plugin_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", plugin_id):
        raise ValueError("Invalid plugin id.")
    for key in ("name", "description"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Missing {key}.")
    ui = data.get("ui", "stdio")
    if ui not in ("stdio", "qt", "icons", "themes"):
        raise ValueError("Expected ui to be stdio, qt, icons, or themes.")
    entrypoint = None
    if ui not in ("icons", "themes"):
        name = data.get("entrypoint")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Missing entrypoint.")
        entrypoint = (directory / name).resolve()
        if (Path(name).is_absolute() or "\\" in name
                or not entrypoint.is_relative_to(directory.resolve())
                or entrypoint.suffix != ".py" or check_files and not entrypoint.is_file()):
            raise ValueError("Entrypoint must be a Python file inside the plugin folder.")
    methods = data.get("methods", [] if ui in ("icons", "themes") else None)
    if not isinstance(methods, list) or not all(isinstance(m, str) and m for m in methods):
        raise ValueError("Expected a list of headless methods.")
    optional_methods = data.get("optional_methods", [])
    if (not isinstance(optional_methods, list) or len(optional_methods) > 256
            or any(not isinstance(m, str) or not m for m in optional_methods)
            or len(set(optional_methods)) != len(optional_methods) or set(optional_methods) & set(methods)):
        raise ValueError("optional_methods must contain unique backend method names separate from methods.")
    service = data.get("service", False)
    if type(service) is not bool or service and ui != "qt":
        raise ValueError("service must be a boolean and requires ui: qt.")
    show_in_menu = data.get("show_in_menu", True)
    if type(show_in_menu) is not bool:
        raise ValueError("show_in_menu must be a boolean.")
    settings = data.get("settings", [])
    limit = 512 if ui == "icons" else 32
    from .settings import validate_schema
    validate_schema(settings, limit)
    games = data.get("game_ids", [])
    if not isinstance(games, list) or not all(isinstance(g, str) for g in games):
        raise ValueError("game_ids must be a list of game identifiers.")
    if ui in ("icons", "themes") and any(data.get(key) for key in (
            "entrypoint", "methods", "game_ids", "ui_extensions", "service",
            "context_menu_actions", "launch_hooks", "menu_tab", "flags", "flag_provider",
            "columns", "column_provider", "filters", "filter_provider", "diagnostic_provider", "lifecycle_hooks", "file_previews", "commands", "installers", "download_providers", "launcher_providers", "game_handlers", "modlist_sections", "wizards", "operation_guards", "optional_methods")):
        raise ValueError("Asset-only plugins cannot declare executable features or game restrictions.")
    for key in ("version", "author", "min_manager_version"):
        value = data.get(key, "")
        if not isinstance(value, str) or store and not value.strip():
            raise ValueError(f"Missing or invalid {key}.")
        if value and key != "author":
            version_key(value)
    hooks = data.get("launch_hooks")
    if hooks is not None:
        if not isinstance(hooks, str) or not hooks or Path(hooks).is_absolute() or "\\" in hooks:
            raise ValueError("launch_hooks must name a plugin-relative Python file.")
        hooks = (directory / hooks).resolve()
        if (not hooks.is_relative_to(directory.resolve()) or hooks.suffix != ".py"
                or check_files and not hooks.is_file()):
            raise ValueError("Launch hooks must be a Python file inside the plugin folder.")
    requires_game = data.get("requires_game", True)
    if type(requires_game) is not bool or not requires_game and ui != "qt":
        raise ValueError("requires_game must be a boolean; only Qt plugins can run without a game.")
    extensions = _read_ui_extensions(data, directory, ui)
    menu_tab = data.get("menu_tab", "")
    if (not isinstance(menu_tab, str) or menu_tab and not any(
            e.id == menu_tab and e.type == "widget" and e.location == "tab"
            for e in extensions)):
        raise ValueError("menu_tab must name a declared tab widget.")
    flags, provider = _read_flags(data, directory, check_files)
    columns, column_provider = _read_columns(data, directory, check_files)
    filters, filter_provider = _read_filters(data, directory, check_files)
    diagnostic_provider = data.get("diagnostic_provider")
    if diagnostic_provider is not None:
        if (not isinstance(diagnostic_provider, str) or not diagnostic_provider
                or Path(diagnostic_provider).is_absolute() or "\\" in diagnostic_provider):
            raise ValueError("diagnostic_provider must be a plugin-relative Python file.")
        diagnostic_provider = (directory / diagnostic_provider).resolve()
        if (not diagnostic_provider.is_relative_to(directory.resolve()) or diagnostic_provider.suffix != ".py"
                or check_files and not diagnostic_provider.is_file()):
            raise ValueError("diagnostic_provider must be a Python file inside the plugin folder.")
    lifecycle = data.get("lifecycle_hooks", {})
    if not isinstance(lifecycle, dict) or set(lifecycle) - LIFECYCLE_EVENTS:
        raise ValueError("lifecycle_hooks must map supported lifecycle events to Python files.")
    lifecycle_hooks = []
    for event, name in lifecycle.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
            raise ValueError("Lifecycle hooks must name plugin-relative Python files.")
        path = (directory / name).resolve()
        if (not path.is_relative_to(directory.resolve()) or path.suffix != ".py"
                or check_files and not path.is_file()):
            raise ValueError("Lifecycle hooks must be Python files inside the plugin folder.")
        lifecycle_hooks.append((event, path))
    overrides, icon_settings = _read_icon_overrides(data, directory, check_files, settings)
    if ui == "icons" and not overrides:
        raise ValueError("ui: icons requires icon_overrides.")
    themes = _read_themes(data, directory, check_files)
    if ui == "themes" and not themes:
        raise ValueError("ui: themes requires themes.")
    handler_files = data.get("game_handlers", [])
    if not isinstance(handler_files, list) or len(handler_files) > 32 or len(set(map(str, handler_files))) != len(handler_files):
        raise ValueError("game_handlers must contain up to 32 unique plugin-relative Python files.")
    handlers = []
    for name in handler_files:
        if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
            raise ValueError("Game handlers must name plugin-relative Python files.")
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or path.suffix != ".py" or check_files and not path.is_file():
            raise ValueError("Game handlers must be Python files inside the plugin folder.")
        handlers.append(path)
    from .wizards import declarations as wizard_declarations
    from .compatibility import declarations as dependencies, identifiers
    from .guards import OPERATIONS
    guard_data = data.get("operation_guards", {})
    if not isinstance(guard_data, dict) or set(guard_data) - OPERATIONS:
        raise ValueError("operation_guards must map supported mod operations to Python files.")
    guard_paths = []
    for operation, filename in guard_data.items():
        action, = _read_context_menu_actions({"context_menu_actions": [{"id": "guard", "label": "Guard",
            "locations": ["modlist"], "entrypoint": filename}]}, directory, entrypoint, check_files)
        guard_paths.append((operation, action.entrypoint))
    plugin = Plugin(plugin_id, data["name"], data["description"], entrypoint,
                  tuple(methods), tuple(settings), ui,
                  extensions, service,
                  directory.resolve(), data.get("version", ""), data.get("author", ""),
                  data.get("min_manager_version", ""),
                  _read_context_menu_actions(data, directory, entrypoint, check_files), hooks, menu_tab,
                  flags, provider, overrides, icon_settings, columns, column_provider,
                  filters, filter_provider, diagnostic_provider, tuple(lifecycle_hooks),
                  _read_file_previews(data, directory, entrypoint, ui, check_files),
                  _read_commands(data, directory, entrypoint, extensions, check_files),
                  _read_installers(data, directory, entrypoint, check_files),
                  _read_download_providers(data, directory, entrypoint, check_files), requires_game,
                  _launcher_providers(data, directory, check_files), _persistent_directories(data), tuple(handlers), themes,
                  show_in_menu=show_in_menu, modlist_sections=_read_modlist_sections(data, ui),
                  warnings=manifest_warnings(data),
                  wizards=wizard_declarations(data, directory, entrypoint, ui, check_files),
                  operation_guards=tuple(guard_paths), optional_methods=tuple(optional_methods),
                  plugin_dependencies=dependencies(data, plugin_id),
                  plugin_conflicts=identifiers(data.get("plugin_conflicts", []), "plugin_conflicts", plugin_id),
                  load_after=identifiers(data.get("load_after", []), "load_after", plugin_id))
    if check_compatibility and (error := compatibility_error(plugin)):
        raise ValueError(error)
    if store and check_files and any((directory / name).exists() for name in plugin.persistent_directories):
        raise ValueError("Plugin packages cannot populate persistent directories.")
    return plugin, games


def discover(game_id, directory=None, *, resolve_dependencies=True):
    plugins, errors = [], []
    seen = set()
    root = Path(directory) if directory is not None else plugins_dir()
    from amethyst_plugins.store import PluginStore
    try:
        disabled = PluginStore(plugins=root).disabled_ids()
    except (OSError, ValueError, TypeError) as exc:
        return [], [f"Plugin enablement: {exc}"]
    paths = sorted(root.glob("*/plugin.json"))
    paths.extend(sorted((root / "Headless").glob("*/plugin.json")))
    for path in paths:
        try:
            plugin, games = read_plugin(path)
            if plugin.id in disabled:
                continue
            errors.extend(f"{plugin.id}: {warning}" for warning in plugin.warnings)
            if plugin.id in seen:
                raise ValueError(f"Duplicate plugin id: {plugin.id}")
            seen.add(plugin.id)
            if game_id is None or not games or game_id in games:
                plugins.append(plugin)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path.parent.relative_to(root)}: {exc}")
    if not resolve_dependencies:
        return sorted(plugins, key=lambda p: p.name.casefold()), errors
    from .compatibility import resolve
    ordered, dependency_errors = resolve(plugins)
    return ordered, errors + dependency_errors


def plugin_directory(plugin):
    return plugin.directory or plugin.entrypoint.parent


def settings_path(plugin):
    return plugin_directory(plugin) / "settings.json"


def data_path(plugin):
    return plugin_directory(plugin) / "data.json"


def _read_storage(path):
    data = json.read_json(path, max_bytes=1024 * 1024)
    if not isinstance(data, dict):
        raise ValueError("Plugin settings and data must be JSON objects.")
    return data


def _save_storage(path, data):
    from Utils.atomic_write import write_atomic
    if not isinstance(data, dict):
        raise ValueError("Plugin settings and data must be JSON objects.")
    raw = json.dumps(data, max_bytes=1024 * 1024, ensure_ascii=True).encode()
    write_atomic(path, raw)


def _load_storage(plugin, path, legacy_directory):
    from Utils.config_paths import get_config_dir
    try:
        return _read_storage(path)
    except FileNotFoundError:
        pass
    try:
        data = _read_storage(get_config_dir() / legacy_directory / f"{plugin.id}.json")
    except FileNotFoundError:
        return {}
    try:
        _save_storage(path, data)
    except OSError:
        pass
    return data


def load_settings(plugin):
    from .settings import validate_value
    try:
        saved = _load_storage(plugin, settings_path(plugin), "plugin_settings")
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    values = {}
    for setting in plugin.settings:
        try:
            values[setting["key"]] = validate_value(setting, saved.get(setting["key"], setting["default"]))
        except ValueError:
            values[setting["key"]] = setting["default"]
    return values


def save_settings(plugin, settings):
    from .settings import validate_value
    if not isinstance(settings, dict):
        raise ValueError("Plugin settings must be a JSON object.")
    for setting in plugin.settings:
        if setting["key"] in settings:
            validate_value(setting, settings[setting["key"]])
    path = settings_path(plugin)
    try:
        saved = _read_storage(path)
    except FileNotFoundError:
        saved = {}
    _save_storage(path, {**saved, **settings})


def settings_error(plugin):
    from Utils.config_paths import get_config_dir
    try:
        try:
            _read_storage(settings_path(plugin))
        except FileNotFoundError:
            try:
                _read_storage(get_config_dir(create=False) / "plugin_settings" / f"{plugin.id}.json")
            except FileNotFoundError:
                pass
    except (OSError, ValueError) as exc:
        return str(exc)
    return ""


def reset_settings(plugin):
    import shutil
    import uuid
    from Utils.config_paths import get_config_dir
    path = settings_path(plugin)
    if path.is_symlink():
        raise ValueError("Cannot reset settings stored through a symbolic link.")
    source = path if path.exists() else get_config_dir() / "plugin_settings" / f"{plugin.id}.json"
    backup = None
    if source.is_symlink():
        raise ValueError("Cannot reset settings stored through a symbolic link.")
    if source.exists():
        backup = source.with_name(f"{source.name}.backup-{uuid.uuid4().hex}")
        shutil.copy2(source, backup)
    _save_storage(path, {s["key"]: s["default"] for s in plugin.settings})
    return backup


def load_data(plugin):
    return _load_storage(plugin, data_path(plugin), "plugin_data")


def save_data(plugin, data):
    _save_storage(data_path(plugin), data)
