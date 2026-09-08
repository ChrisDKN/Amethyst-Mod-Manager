from __future__ import annotations

import configparser
import re
import zipfile
from dataclasses import dataclass, field

from .games import nexus_domain
from .manifest import qvalue
from .paths import WabbajackError, relative_path
from .post_install_rules import OUTPUT_RULES


@dataclass
class AuthoredProfile:
    mods: list[str] = field(default_factory=list)
    plugins: set[str] = field(default_factory=set)
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SetupTask:
    id: str
    label: str
    mod: str
    profiles: tuple[str, ...]
    masters: tuple[str, ...]
    mpi_titles: tuple[str, ...] = ()


def profile_configuration(package, selected=None):
    selected = set(package.profiles if selected is None else selected)
    result = {name: AuthoredProfile() for name in selected}
    with zipfile.ZipFile(package.path) as archive:
        for directive in package.directives:
            parts = directive.path.split("/")
            if len(parts) != 3 or parts[0].casefold() != "profiles" or parts[1] not in selected:
                continue
            kind = parts[2].casefold()
            if kind not in {"modlist.txt", "plugins.txt", "settings.ini"}:
                continue
            member = directive.data.get("SourceDataID")
            if not member:
                raise WabbajackError(f"Cannot inspect required profile configuration before reconstruction: {directive.path}")
            if archive.getinfo(member).file_size > 8 * 1024 ** 2:
                raise WabbajackError(f"Profile configuration exceeds 8 MiB: {directive.path}")
            text = archive.read(member).decode("utf-8-sig")
            profile = result[parts[1]]
            if kind == "modlist.txt":
                profile.mods = [line[1:] for line in text.splitlines()
                                if line.startswith("+") and not line.casefold().endswith("_separator")]
                for name in profile.mods:
                    if "/" in relative_path(name):
                        raise WabbajackError(f"Invalid mod name in {directive.path}: {name}")
            elif kind == "plugins.txt":
                lines = text.splitlines()
                starred = any(line.startswith("*") for line in lines)
                profile.plugins = {line.strip().removeprefix("*").casefold() for line in lines
                                   if line.strip() and not line.startswith("#")
                                   and (not starred or line.startswith("*"))}
            else:
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                cp.optionxform = str
                cp.read_string(text)
                if cp.has_section("custom_overrides"):
                    for executable, value in cp["custom_overrides"].items():
                        name = qvalue(value).strip()
                        if not name:
                            continue
                        if "/" in relative_path(name):
                            raise WabbajackError(f"Invalid output mod in {directive.path}: {name}")
                        profile.outputs[qvalue(executable)] = name
    mod_names = {d.path.split("/")[1].casefold(): d.path.split("/")[1]
                 for d in package.directives if d.path.startswith("mods/")}
    for profile in result.values():
        for mod in profile.mods:
            mod_names.setdefault(mod.casefold(), mod)
        profile.outputs = {exe: mod_names.setdefault(name.casefold(), name) for exe, name in profile.outputs.items()}
    return result


def setup_tasks(package, selected=None, configuration=None):
    config = configuration if configuration is not None else profile_configuration(package, selected)
    domain = nexus_domain(package.game)
    provided = {d.path.casefold() for d in package.directives}
    provided_mods = {path.split("/")[1] for path in provided if path.startswith("mods/")}
    grouped = {}
    definitions = [rule for rule in OUTPUT_RULES if rule.domain == domain]
    for profile_name, profile in config.items():
        for rule in definitions:
            task_id, label, aliases, masters, titles = rule.id, rule.label, rule.aliases, rule.masters, rule.mpi_titles
            if rule.required_plugin and rule.required_plugin not in profile.plugins:
                continue
            if rule.companion_master and rule.companion_master.casefold() in profile.plugins:
                supplied = any(f"mods/{mod}/{rule.companion_master}".casefold() in provided for mod in profile.mods)
                separate = bool(rule.companion_alias) and any(rule.companion_alias in mod.casefold() for mod in profile.mods)
                if not supplied and not separate:
                    masters = (*masters, rule.companion_master)
            candidates = [mod for mod in profile.mods if any(alias in mod.casefold() for alias in aliases)
                          and not any(word in mod.casefold() for word in ("patches", "compatibility", "translations"))]
            exact = [mod for mod in candidates if any(re.fullmatch(re.escape(alias) + r"(?:\s*\(TTW\))?(?:\s+v?\d+(?:\.\d+)*)?",
                     re.sub(r"\[[^]]*\]", "", mod).strip(" -_"), re.I) for alias in aliases)]
            if exact:
                candidates = exact
            if rule.prefer_unprefixed:
                names = {mod.casefold() for mod in candidates}
                candidates = [mod for mod in candidates if not (
                    mod.casefold().startswith("[nodelete] ")
                    and mod.casefold() in provided_mods
                    and mod[len("[NoDelete] "):].casefold() in names)]
            if rule.required_plugin and len(candidates) != 1:
                raise WabbajackError(f"Profile {profile_name} requires {task_id.upper()} but has no unambiguous authored {task_id.upper()} mod slot")
            for mod in candidates:
                if all(f"mods/{mod}/{name}".casefold() in provided for name in masters):
                    continue
                key = task_id + ":" + mod
                row = grouped.setdefault(key, [task_id, label, mod, [], masters, titles])
                row[3].append(profile_name)
                row[4] = tuple(dict.fromkeys((*row[4], *masters)))
    return [SetupTask(key, row[1], row[2], tuple(row[3]), row[4], row[5])
            for key, row in grouped.items()]
