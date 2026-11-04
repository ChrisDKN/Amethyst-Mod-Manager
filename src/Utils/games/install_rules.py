"""Ordered plain-archive install rules and per-game overrides."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_game_config_dir
from Utils.games.routing_rules import definition_rule_ids, validate_rule as validate_routing_rule


@dataclass
class InstallRule:
    dest: str
    extensions: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    loose_only: bool = False
    flatten: bool = False
    include_siblings: bool = False
    rule_id: str = ""


_FIELDS = tuple(key for key in InstallRule.__dataclass_fields__ if key != "rule_id")


def rule_values(rule: InstallRule) -> dict:
    return {key: value for key, value in asdict(rule).items() if key in _FIELDS}


def validate_rule(values: dict) -> InstallRule:
    if not isinstance(values, dict) or set(values) - set(_FIELDS):
        raise ValueError("Invalid install rule settings.")
    normalized = asdict(validate_routing_rule(values))
    return InstallRule(**{key: normalized[key] for key in _FIELDS})


@dataclass
class InstallEntry:
    key: str
    defaults: dict | None
    edits: dict = field(default_factory=dict)
    disabled: bool = False
    moved: bool = False

    @property
    def builtin(self) -> bool:
        return self.defaults is not None

    @property
    def rule(self) -> InstallRule:
        return InstallRule(**((self.defaults or {}) | self.edits))

    def edit(self, rule: InstallRule) -> None:
        old = rule_values(self.rule)
        for key, value in rule_values(rule).items():
            if value == old[key]:
                continue
            if self.defaults is not None and value == self.defaults[key]:
                self.edits.pop(key, None)
            else:
                self.edits[key] = value


def new_entry(rule: InstallRule) -> InstallEntry:
    return InstallEntry("user:" + uuid4().hex, None, rule_values(rule))


def _path(game) -> Path:
    return get_game_config_dir(game.name) / "install_rules.json"


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
        raise ValueError("Invalid install overrides: " + str(path))
    seen = set()
    for row in raw["rules"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in seen
                or type(row.get("disabled")) is not bool
                or type(row.get("moved", False)) is not bool
                or not isinstance(row.get("edits"), dict)
                or set(row["edits"]) - set(_FIELDS)):
            raise ValueError("Invalid install override entry: " + str(path))
        seen.add(row["id"])
        defaults = row.get("defaults")
        if defaults is not None:
            defaults = rule_values(validate_rule(defaults))
            row["defaults"] = defaults
        normalized = rule_values(validate_rule((defaults or {}) | row["edits"]))
        row["edits"] = ({key: normalized[key] for key in row["edits"]}
                        if defaults is not None else normalized)
    return raw


def builtin_entries(game) -> list[InstallEntry]:
    rules = list(game.custom_install_rules)
    ids = definition_rule_ids([asdict(rule) for rule in rules])
    return [InstallEntry(key, rule_values(rule)) for key, rule in zip(ids, rules)]


def _resolve(defaults: list[InstallEntry], raw: dict) -> list[InstallEntry]:
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
                result.append(InstallEntry("retired:" + key, None,
                                           copy.deepcopy(original) | edits, disabled))
        else:
            result.append(InstallEntry(key, None, edits, disabled))
    # Fill unmoved slots in handler order, keeping custom and moved entries anchored.
    untouched = {entry.key: entry for entry in result if entry.builtin and not entry.moved}
    untouched.update(available)
    ordered = iter(untouched[row.key] for row in defaults if row.key in untouched)
    slots = [index for index, entry in enumerate(result) if entry.builtin and not entry.moved]
    merged = []
    for index, entry in enumerate(result):
        merged.append(next(ordered) if entry.builtin and not entry.moved else entry)
        if slots and index == slots[-1]:
            merged.extend(ordered)
    if not slots:
        merged.extend(ordered)
    return merged


def load_entries(game) -> list[InstallEntry]:
    path = _path(game)
    return _resolve(builtin_entries(game), _read(path, _signature(path)))


def save_entries(game, entries: list[InstallEntry]) -> None:
    if len(entries) > 512:
        raise ValueError("At most 512 install rules are allowed.")
    rows = []
    for entry in entries:
        validate_rule(rule_values(entry.rule))
        rows.append({"id": entry.key, "defaults": entry.defaults,
                     "edits": entry.edits, "disabled": entry.disabled,
                     "moved": entry.moved})
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate install rule identifiers.")
    path = _path(game)
    write_atomic_text(path, json.dumps({"version": 1, "rules": rows}, indent=2) + "\n")
    _read.cache_clear()


def effective_rules(game) -> list[InstallRule]:
    path = _path(game)
    signature = _signature(path)
    game._install_overrides_active = bool(signature)
    defaults = list(game.custom_install_rules)
    if not signature:
        return defaults
    cached = getattr(game, "_effective_install_cache", None)
    if cached is not None and cached[:3] == (path, signature, defaults):
        return cached[3]
    raw = _read(path, signature)
    ids = definition_rule_ids([asdict(rule) for rule in defaults])
    entries = _resolve([InstallEntry(key, rule_values(rule))
                        for key, rule in zip(ids, defaults)], raw)
    rules = [entry.rule for entry in entries if not entry.disabled]
    game._effective_install_cache = (path, signature, copy.deepcopy(defaults), rules)
    return rules


def get_rules(game) -> list[InstallRule]:
    rules = getattr(game, "effective_custom_install_rules", None)
    if rules is None:
        rules = getattr(game, "custom_install_rules", ())
    return rules or []
