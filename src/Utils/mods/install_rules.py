from __future__ import annotations

import secrets
import string
from pathlib import PurePosixPath

from Utils.deployment.shared import CustomRule
from Utils.deployment.custom_rules import (
    _match_single_rule, _normalise_rule, canonicalize_declared_folders,
)
from Utils.games.install_rules import InstallRule, rule_values, validate_rule


class InstallRuleConflict(ValueError):
    pass


def apply_install_rules(file_list: list[tuple[str, str, bool]], rules: list[InstallRule],
                        mod_name: str, state: dict) -> list[tuple[str, str, bool]]:
    from Utils.mods.install import _normalise_relative_install_path, validate_mod_folder_name
    from Utils.filegraph.paths import EXCLUDE_NAMES

    reserved = {".mm_bundle", *EXCLUDE_NAMES}

    def protected(path):
        return path.split("/", 1)[0].lower() in reserved

    paths = [_normalise_relative_install_path(d, label="install destination").rstrip("/")
             for _, d, _ in file_list]
    claims = {}
    for raw in rules:
        rule = validate_rule(rule_values(raw))
        destination = rule.dest
        if "<modname>" in destination:
            destination = destination.replace("<modname>", validate_mod_folder_name(mod_name))
        if "<random>" in destination:
            token = state.get("random", "")
            if not token:
                token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
                state["random"] = token
            if len(token) != 12 or any(c not in string.ascii_letters + string.digits for c in token):
                raise ValueError("Invalid saved install-rule random value.")
            destination = destination.replace("<random>", token)
        destination = _normalise_relative_install_path(
            destination, label="install rule destination", allow_empty=True).rstrip("/")
        match_rule, folders, extensions, filenames = _normalise_rule(CustomRule(**rule_values(rule)))
        primaries = []
        for index, path in enumerate(paths):
            if index in claims or protected(path):
                continue
            hit = _match_single_rule(path.lower(), match_rule, folders, extensions, filenames)
            if hit is not None:
                primaries.append((index, hit[0]))

        def claim(index, tail):
            tail = canonicalize_declared_folders(tail, tuple(rule.folders))
            claims[index] = f"{destination}/{tail}" if destination else tail

        for index, strip_len in primaries:
            path = paths[index]
            if rule.include_siblings or not rule.flatten:
                tail = path
            elif strip_len >= 0:
                tail = path[strip_len:].lstrip("/")
            else:
                tail = path.rsplit("/", 1)[-1]
            claim(index, tail)
        if rule.include_siblings:
            containers = {paths[i].split("/", 1)[0].lower() if "/" in paths[i] else ""
                          for i, _ in primaries}
            for index, path in enumerate(paths):
                if index in claims or protected(path):
                    continue
                if "" in containers or path.split("/", 1)[0].lower() in containers:
                    claim(index, path)

    if not claims:
        return file_list
    result = []
    destinations = {}
    for index, (source, _original, is_folder) in enumerate(file_list):
        destination = claims.get(index, paths[index])
        destination = _normalise_relative_install_path(
            destination, label="install rule output", allow_empty=False)
        if index in claims and protected(destination):
            raise ValueError(f"Install rule targets reserved metadata: {destination}")
        key = destination.lower()
        if key in destinations and paths[destinations[key][0]].lower() != paths[index].lower():
            raise InstallRuleConflict(f"Install rules produce conflicting destinations: {destination}")
        destinations[key] = (index, is_folder)
        result.append((source, destination, is_folder))
    for path in destinations:
        for parent in PurePosixPath(path).parents:
            if parent.as_posix() in destinations and not destinations[parent.as_posix()][1]:
                raise InstallRuleConflict(f"Install rules produce a file/folder conflict: {path}")
    return result
