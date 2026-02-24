"""
fomod_parser.py
Pure XML parsing for FOMOD ModuleConfig.xml and info.xml.
No UI, no file I/O beyond reading the XML files.
Uses only stdlib (xml.etree.ElementTree).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FileInstall:
    """A <file> or <folder> install instruction."""
    source: str       # Raw path from XML (may have Windows backslashes)
    destination: str  # Raw path from XML
    priority: int
    is_folder: bool   # True if <folder>, False if <file>

    @property
    def source_path(self) -> str:
        return self.source.replace("\\", os.sep).replace("/", os.sep)

    @property
    def destination_path(self) -> str:
        return self.destination.replace("\\", os.sep).replace("/", os.sep)


@dataclass
class Dependency:
    """
    A single dependency node or a composite (And/Or) group.

    dep_type = "flag"      → flag_name + flag_value
    dep_type = "file"      → file_name + file_state
    dep_type = "composite" → operator + sub_deps
    """
    dep_type: str
    operator: str = "And"
    flag_name: str = ""
    flag_value: str = ""
    file_name: str = ""
    file_state: str = "Active"   # "Active" | "Inactive" | "Missing"
    sub_deps: list[Dependency] = field(default_factory=list)


@dataclass
class TypeDescriptor:
    """
    Parsed typeDescriptor element.

    Simple form:       <type name="Optional"/>
    Conditional form:  <dependencyType><defaultType .../><patterns>...</patterns></dependencyType>
    """
    plugin_type: str = "Optional"   # Optional|Required|Recommended|CouldBeUsable|NotUsable
    is_conditional: bool = False
    default_type: str = "Optional"
    # List of (Dependency, type_name) pairs — first matching pattern wins
    patterns: list[tuple[Dependency, str]] = field(default_factory=list)


@dataclass
class Plugin:
    """A single installer option inside a group."""
    name: str
    description: str = ""
    image_path: str = ""   # Raw path from XML; may use backslashes
    files: list[FileInstall] = field(default_factory=list)
    condition_flags: dict[str, str] = field(default_factory=dict)  # flag name → value to set
    type_descriptor: TypeDescriptor = field(default_factory=TypeDescriptor)

    @property
    def image_os_path(self) -> str:
        return self.image_path.replace("\\", os.sep).replace("/", os.sep)


@dataclass
class Group:
    """A group of plugins within an install step."""
    name: str
    group_type: str   # SelectExactlyOne|SelectAtMostOne|SelectAtLeastOne|SelectAny|SelectAll
    plugins: list[Plugin] = field(default_factory=list)


@dataclass
class InstallStep:
    """A single page/step in the FOMOD wizard."""
    name: str
    groups: list[Group] = field(default_factory=list)
    visible_condition: Optional[Dependency] = None  # None = always visible


@dataclass
class ConditionalInstallPattern:
    """A single pattern inside <conditionalFileInstalls>."""
    dependency: Dependency = field(default_factory=lambda: Dependency(dep_type="composite"))
    files: list[FileInstall] = field(default_factory=list)


@dataclass
class ModuleConfig:
    """Top-level parsed FOMOD configuration."""
    name: str = ""
    module_image_path: str = ""
    steps: list[InstallStep] = field(default_factory=list)
    required_files: list[FileInstall] = field(default_factory=list)
    conditional_file_installs: list[ConditionalInstallPattern] = field(default_factory=list)


@dataclass
class ModInfo:
    """Parsed from fomod/info.xml (optional)."""
    name: str = ""
    author: str = ""
    version: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# Internal parse helpers
# ---------------------------------------------------------------------------

def _text(el: Optional[ET.Element]) -> str:
    """Safely get stripped text from an element, or empty string."""
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _parse_dependency(el: ET.Element) -> Dependency:
    """Recursively parse a dependency element."""
    tag = el.tag
    # Strip namespace if present (some FOMOD XML uses namespaces)
    if "}" in tag:
        tag = tag.split("}", 1)[1]

    if tag == "dependencies":
        operator = el.get("operator", "And")
        sub_deps = [_parse_dependency(child) for child in el]
        return Dependency(dep_type="composite", operator=operator, sub_deps=sub_deps)
    elif tag == "flagDependency":
        return Dependency(
            dep_type="flag",
            flag_name=el.get("flag", ""),
            flag_value=el.get("value", ""),
        )
    elif tag == "fileDependency":
        return Dependency(
            dep_type="file",
            file_name=el.get("file", ""),
            file_state=el.get("state", "Active"),
        )
    else:
        # Unknown dependency type — treat as a no-op composite (always true)
        return Dependency(dep_type="composite", operator="And", sub_deps=[])


def _parse_files(files_el: ET.Element) -> list[FileInstall]:
    """Parse a <files> element containing <file> and <folder> children."""
    result = []
    for child in files_el:
        tag = child.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]
        if tag in ("file", "folder"):
            result.append(FileInstall(
                source=child.get("source", ""),
                destination=child.get("destination", ""),
                priority=int(child.get("priority", "0")),
                is_folder=(tag == "folder"),
            ))
    return result


def _parse_type_descriptor(td_el: ET.Element) -> TypeDescriptor:
    """
    Parse a <typeDescriptor> element.
    Handles both simple (<type name="..."/>) and conditional (<dependencyType>) forms.
    """
    # Strip namespace helper
    def local(el: ET.Element) -> str:
        t = el.tag
        return t.split("}", 1)[1] if "}" in t else t

    for child in td_el:
        child_tag = local(child)
        if child_tag == "type":
            return TypeDescriptor(plugin_type=child.get("name", "Optional"))
        elif child_tag == "dependencyType":
            td = TypeDescriptor(is_conditional=True)
            for sub in child:
                sub_tag = local(sub)
                if sub_tag == "defaultType":
                    td.default_type = sub.get("name", "Optional")
                    td.plugin_type = td.default_type
                elif sub_tag == "patterns":
                    for pattern in sub:
                        p_tag = local(pattern)
                        if p_tag != "pattern":
                            continue
                        dep_el = pattern.find("dependencies")
                        if dep_el is None:
                            # Try with namespace
                            for c in pattern:
                                if local(c) == "dependencies":
                                    dep_el = c
                                    break
                        type_el = None
                        for c in pattern:
                            if local(c) == "type":
                                type_el = c
                                break
                        if dep_el is not None and type_el is not None:
                            dep = _parse_dependency(dep_el)
                            type_name = type_el.get("name", "Optional")
                            td.patterns.append((dep, type_name))
            return td

    return TypeDescriptor()


def _find(el: ET.Element, tag: str) -> Optional[ET.Element]:
    """Find a child by local tag name, ignoring XML namespaces."""
    for child in el:
        child_tag = child.tag
        if "}" in child_tag:
            child_tag = child_tag.split("}", 1)[1]
        if child_tag == tag:
            return child
    return None


def _findall(el: ET.Element, tag: str) -> list[ET.Element]:
    """Find all children by local tag name, ignoring XML namespaces."""
    result = []
    for child in el:
        child_tag = child.tag
        if "}" in child_tag:
            child_tag = child_tag.split("}", 1)[1]
        if child_tag == tag:
            result.append(child)
    return result


def _parse_plugin(plugin_el: ET.Element) -> Plugin:
    """Parse a single <plugin> element."""
    name = plugin_el.get("name", "")
    plugin = Plugin(name=name)

    desc_el = _find(plugin_el, "description")
    if desc_el is not None:
        plugin.description = _text(desc_el)

    img_el = _find(plugin_el, "image")
    if img_el is not None:
        plugin.image_path = img_el.get("path", "")

    files_el = _find(plugin_el, "files")
    if files_el is not None:
        plugin.files = _parse_files(files_el)

    flags_el = _find(plugin_el, "conditionFlags")
    if flags_el is not None:
        for flag_el in _findall(flags_el, "flag"):
            flag_name = flag_el.get("name", "")
            flag_val = _text(flag_el)
            if flag_name:
                plugin.condition_flags[flag_name] = flag_val

    td_el = _find(plugin_el, "typeDescriptor")
    if td_el is not None:
        plugin.type_descriptor = _parse_type_descriptor(td_el)

    return plugin


def _parse_group(group_el: ET.Element) -> Group:
    """Parse a single <group> element."""
    name = group_el.get("name", "")
    group_type = group_el.get("type", "SelectAny")
    group = Group(name=name, group_type=group_type)

    plugins_el = _find(group_el, "plugins")
    if plugins_el is not None:
        for plugin_el in _findall(plugins_el, "plugin"):
            group.plugins.append(_parse_plugin(plugin_el))

    return group


def _parse_install_step(step_el: ET.Element) -> InstallStep:
    """Parse a single <installStep> element."""
    name = step_el.get("name", "")
    step = InstallStep(name=name)

    # Optional visibility condition
    visible_el = _find(step_el, "visible")
    if visible_el is not None:
        deps_el = _find(visible_el, "dependencies")
        if deps_el is not None:
            step.visible_condition = _parse_dependency(deps_el)

    # Groups
    groups_el = _find(step_el, "optionalFileGroups")
    if groups_el is not None:
        for group_el in _findall(groups_el, "group"):
            step.groups.append(_parse_group(group_el))

    return step


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_fomod(extracted_root: str) -> Optional[tuple[str, str]]:
    """
    Given the root of an extracted mod archive, find ModuleConfig.xml.

    Archives often have one or more wrapper folders before the actual mod root,
    e.g.:  extract_dir/<archive name>/<mod name (FOMOD)>/Fomod/ModuleConfig.xml
    This walks up to 3 levels deep to find any fomod/ directory.

    Returns (mod_root, config_path) where:
      mod_root    — the directory that contains the fomod/ folder
                    (FOMOD source paths are relative to this)
      config_path — full path to ModuleConfig.xml
    Returns None if no FOMOD installer is found.
    """
    root = Path(extracted_root)

    # Check each directory as a potential mod root (including the root itself),
    # up to 3 levels deep.
    def _check(d: Path) -> Optional[tuple[str, str]]:
        try:
            for child in d.iterdir():
                if child.is_dir() and child.name.lower() == "fomod":
                    config = child / "ModuleConfig.xml"
                    if config.is_file():
                        return str(d), str(config)
        except PermissionError:
            pass
        return None

    # BFS: root first, then its children, then grandchildren, then great-grandchildren
    candidates: list[Path] = [root]
    for _ in range(3):
        next_level: list[Path] = []
        for d in candidates:
            hit = _check(d)
            if hit:
                return hit
            try:
                for child in sorted(d.iterdir()):
                    if child.is_dir():
                        next_level.append(child)
            except PermissionError:
                continue
        candidates = next_level

    return None


def parse_module_config(xml_path: str) -> ModuleConfig:
    """
    Parse a ModuleConfig.xml file and return a ModuleConfig dataclass.
    Uses ET.parse() which automatically handles UTF-16 BOM encoding.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    config = ModuleConfig()

    # Module name
    name_el = _find(root, "moduleName")
    if name_el is not None:
        config.name = _text(name_el)

    # Module header image
    img_el = _find(root, "moduleImage")
    if img_el is not None:
        config.module_image_path = img_el.get("path", "")

    # Required files (always installed)
    req_el = _find(root, "requiredInstallFiles")
    if req_el is not None:
        config.required_files = _parse_files(req_el)

    # Install steps
    steps_el = _find(root, "installSteps")
    if steps_el is not None:
        for step_el in _findall(steps_el, "installStep"):
            config.steps.append(_parse_install_step(step_el))

    # Conditional file installs
    cfi_el = _find(root, "conditionalFileInstalls")
    if cfi_el is not None:
        patterns_el = _find(cfi_el, "patterns")
        if patterns_el is not None:
            for pattern_el in _findall(patterns_el, "pattern"):
                dep_el = _find(pattern_el, "dependencies")
                files_el = _find(pattern_el, "files")
                pattern = ConditionalInstallPattern()
                if dep_el is not None:
                    pattern.dependency = _parse_dependency(dep_el)
                if files_el is not None:
                    pattern.files = _parse_files(files_el)
                config.conditional_file_installs.append(pattern)

    return config


def parse_mod_info(xml_path: str) -> ModInfo:
    """
    Parse a fomod/info.xml file.
    Returns an empty ModInfo if the file is not found or fails to parse.
    """
    if not os.path.isfile(xml_path):
        return ModInfo()
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        info = ModInfo()
        for tag, attr in (("Name", "name"), ("Author", "author"),
                           ("Version", "version"), ("Description", "description")):
            el = _find(root, tag)
            if el is not None:
                setattr(info, attr, _text(el))
        return info
    except ET.ParseError:
        return ModInfo()
