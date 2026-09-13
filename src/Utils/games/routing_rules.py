"""Per-game ordered overrides for handler routing rules."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_game_config_dir
from Utils.deployment.shared import CustomRule


_FIELDS = tuple(key for key in CustomRule.__dataclass_fields__ if key != "rule_id")
_LISTS = ("extensions", "folders", "filenames", "companion_extensions",
          "mirror_dests", "exclude_extensions")
_FLAGS = ("loose_only", "flatten", "include_siblings", "to_prefix")


def rule_values(rule: CustomRule) -> dict:
    return {key: value for key, value in asdict(rule).items() if key in _FIELDS}


def legacy_rule_id(values: dict) -> str:
    match = {key: sorted(str(v).lower() for v in values.get(key, []))
             for key in ("extensions", "folders", "filenames")}
    digest = hashlib.sha256(json.dumps(match, sort_keys=True).encode()).hexdigest()[:20]
    return "legacy:" + digest


def definition_rule_ids(rules: list[dict]) -> list[str]:
    counts = {}
    result = []
    for rule in rules:
        key = rule.get("rule_id") or legacy_rule_id(rule)
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid routing rule identifier.")
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            if rule.get("rule_id"):
                raise ValueError("Duplicate routing rule identifier: " + key)
            key += ":" + str(counts[key])
        result.append(key)
    if len(set(result)) != len(result):
        raise ValueError("Duplicate routing rule identifiers.")
    return result


def _relative_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if (value.startswith("/") or PureWindowsPath(value).drive
            or ".." in value.split("/") or any(c in value for c in "\0\r\n")):
        raise ValueError("Destinations must be relative paths without '..'.")
    return value.rstrip("/")


def validate_rule(values: dict) -> CustomRule:
    if not isinstance(values, dict) or set(values) - set(_FIELDS):
        raise ValueError("Invalid routing rule settings.")
    result = rule_values(CustomRule(dest=""))
    result.update(copy.deepcopy(values))
    if not isinstance(result["dest"], str):
        raise ValueError("Enter a relative destination.")
    result["dest"] = _relative_path(result["dest"])
    for key in _FLAGS:
        if type(result[key]) is not bool:
            raise ValueError("Invalid routing option: " + key)
    for key in _LISTS:
        values = result[key]
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("Invalid routing list: " + key)
        cleaned = []
        for value in values:
            value = value.strip()
            if not value or any(c in value for c in "\0\r\n"):
                raise ValueError("Routing entries cannot be empty or contain line breaks.")
            if key in ("mirror_dests", "folders"):
                value = _relative_path(value)
                if key == "folders" and (not value or value == "."):
                    raise ValueError("Enter a folder name or relative folder path.")
            else:
                if "/" in value or "\\" in value:
                    raise ValueError("Filename and extension matches cannot contain paths.")
                value = value.lower()
            if key.endswith("extensions"):
                if not value.startswith(".") or len(value) == 1 or any(c in value for c in "*?[]"):
                    raise ValueError("Extensions must start with '.', without wildcards.")
            cleaned.append(value)
        result[key] = cleaned
    if not any(result[key] for key in ("extensions", "folders", "filenames")):
        raise ValueError("Specify at least one extension, folder, or filename match.")
    return CustomRule(**result)


@dataclass
class RoutingEntry:
    key: str
    defaults: dict | None
    edits: dict = field(default_factory=dict)
    disabled: bool = False
    moved: bool = False

    @property
    def builtin(self) -> bool:
        return self.defaults is not None

    @property
    def rule(self) -> CustomRule:
        return CustomRule(**((self.defaults or {}) | self.edits))

    def edit(self, rule: CustomRule) -> None:
        old = rule_values(self.rule)
        for key, value in rule_values(rule).items():
            if value == old[key]:
                continue
            if self.defaults is not None and value == self.defaults[key]:
                self.edits.pop(key, None)
            else:
                self.edits[key] = value


def new_entry(rule: CustomRule) -> RoutingEntry:
    return RoutingEntry("user:" + uuid4().hex, None, rule_values(rule))


def _path(game) -> Path:
    return get_game_config_dir(game.name) / "routing_rules.json"


def _signature(path: Path) -> tuple:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return ()


@lru_cache(maxsize=64)
def _read(path: Path, signature: tuple) -> dict:
    if not signature:
        return {"version": 1, "rules": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("rules"), list)):
        raise ValueError("Invalid routing overrides: " + str(path))
    seen = set()
    for row in raw["rules"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in seen
                or type(row.get("disabled")) is not bool
                or type(row.get("moved", False)) is not bool
                or not isinstance(row.get("edits"), dict)
                or set(row["edits"]) - set(_FIELDS)):
            raise ValueError("Invalid routing override entry: " + str(path))
        seen.add(row["id"])
        defaults = row.get("defaults")
        if defaults is not None:
            defaults = rule_values(validate_rule(defaults))
            row["defaults"] = defaults
        normalized = rule_values(validate_rule((defaults or {}) | row["edits"]))
        row["edits"] = ({key: normalized[key] for key in row["edits"]}
                        if defaults is not None else normalized)
    return raw


def builtin_entries(game) -> list[RoutingEntry]:
    rules = list(game.custom_routing_rules)
    ids = definition_rule_ids([asdict(rule) for rule in rules])
    return [RoutingEntry(key, rule_values(rule)) for key, rule in zip(ids, rules)]


def _resolve(defaults: list[RoutingEntry], raw: dict) -> list[RoutingEntry]:
    available = {row.key: row for row in defaults}
    result = []
    for saved in raw["rules"]:
        key = saved["id"]
        original = saved.get("defaults")
        edits = copy.deepcopy(saved["edits"])
        disabled = saved["disabled"]
        moved = saved.get("moved", False)
        if original is not None:
            current_key = key
            if key.startswith("legacy:"):
                stem, suffix = key.rsplit(":", 1)
                base = stem if key.count(":") > 1 and suffix.isdigit() else key
                exact = [
                    candidate_key for candidate_key, candidate in available.items()
                    if (candidate_key == base or candidate_key.startswith(base + ":"))
                    and candidate.defaults == original
                ]
                if len(exact) == 1:
                    current_key = exact[0]
            current = available.pop(current_key, None)
            if current is not None:
                current.edits = edits
                current.disabled = disabled
                current.moved = moved
                validate_rule(rule_values(current.rule))
                result.append(current)
            elif edits or disabled or moved:
                result.append(RoutingEntry("retired:" + key, None,
                                           copy.deepcopy(original) | edits, disabled))
        else:
            result.append(RoutingEntry(key, None, edits, disabled))
    result.extend(available.values())
    return result


def load_entries(game) -> list[RoutingEntry]:
    path = _path(game)
    return _resolve(builtin_entries(game), _read(path, _signature(path)))


def save_entries(game, entries: list[RoutingEntry]) -> None:
    rows = []
    for entry in entries:
        validate_rule(rule_values(entry.rule))
        rows.append({"id": entry.key, "defaults": entry.defaults,
                     "edits": entry.edits, "disabled": entry.disabled,
                     "moved": entry.moved})
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate routing rule identifiers.")
    path = _path(game)
    write_atomic_text(path, json.dumps({"version": 1, "rules": rows}, indent=2) + "\n")
    _read.cache_clear()


def effective_rules(game) -> list[CustomRule]:
    path = _path(game)
    signature = _signature(path)
    game._routing_overrides_active = bool(signature)
    defaults = list(game.custom_routing_rules)
    if not signature:
        return defaults
    cached = getattr(game, "_effective_routing_cache", None)
    if cached is not None and cached[:3] == (path, signature, defaults):
        return cached[3]
    raw = _read(path, signature)
    ids = definition_rule_ids([asdict(rule) for rule in defaults])
    entries = _resolve([RoutingEntry(key, rule_values(rule))
                        for key, rule in zip(ids, defaults)], raw)
    rules = [entry.rule for entry in entries if not entry.disabled]
    game._effective_routing_cache = (path, signature, copy.deepcopy(defaults), rules)
    return rules


def get_rules(game) -> list[CustomRule]:
    rules = getattr(game, "effective_custom_routing_rules", None)
    if rules is None:
        rules = getattr(game, "custom_routing_rules", ())
    return rules or []
