"""Per-game overrides for deployment exclusions and conflict reporting."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field

from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_game_config_dir


@dataclass(frozen=True)
class BlacklistOverrides:
    files: frozenset[str] = field(default_factory=frozenset)
    folders: frozenset[str] = field(default_factory=frozenset)
    disabled_files: frozenset[str] = field(default_factory=frozenset)
    disabled_folders: frozenset[str] = field(default_factory=frozenset)
    conflict_only_files: frozenset[str] = field(default_factory=frozenset)
    conflict_only_folders: frozenset[str] = field(default_factory=frozenset)
    disabled_conflict_only_files: frozenset[str] = field(default_factory=frozenset)
    disabled_conflict_only_folders: frozenset[str] = field(default_factory=frozenset)


def normalize_pattern(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Blacklist patterns must be strings.")
    pattern = value.strip().lower()
    if not pattern or any(char in pattern for char in ("/", "\\", "\n", "\r", "\0")):
        raise ValueError("Enter a filename or folder-name pattern without path separators.")
    return pattern


def builtin_rules(game, *, conflict_only: bool = False
                  ) -> tuple[frozenset[str], frozenset[str]]:
    prefix = "conflict_detection_ignore_" if conflict_only else "conflict_ignore_"
    return tuple(
        frozenset(str(pattern).lower() for pattern in
                  (getattr(game, prefix + suffix, None) or ()))
        for suffix in ("filenames", "foldernames")
    )


def load_overrides(game) -> BlacklistOverrides:
    path = get_game_config_dir(game.name) / "conflict_blacklist.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BlacklistOverrides()
    if not isinstance(raw, dict):
        raise ValueError("Invalid blacklist settings.")
    values = {}
    for key in BlacklistOverrides.__dataclass_fields__:
        patterns = raw.get(key, [])
        if not isinstance(patterns, list):
            raise ValueError("Invalid blacklist pattern list.")
        values[key] = frozenset(normalize_pattern(value) for value in patterns)
    return BlacklistOverrides(**values)


def save_overrides(game, overrides: BlacklistOverrides) -> None:
    payload = {
        key: sorted({normalize_pattern(pattern) for pattern in patterns})
        for key, patterns in asdict(overrides).items()
    }
    path = get_game_config_dir(game.name) / "conflict_blacklist.json"
    write_atomic_text(path, json.dumps(payload, indent=2) + "\n")


def effective_rules(game, overrides: BlacklistOverrides | None = None,
                    *, conflict_only: bool = False
                    ) -> tuple[frozenset[str], frozenset[str]]:
    if overrides is None:
        overrides = load_overrides(game)
    defaults = builtin_rules(game, conflict_only=conflict_only)
    prefix = "conflict_only_" if conflict_only else ""
    return tuple(
        (patterns - getattr(overrides, "disabled_" + prefix + kind))
        | getattr(overrides, prefix + kind)
        for kind, patterns in zip(("files", "folders"), defaults)
    )


def path_is_ignored(path: str, rules) -> bool:
    value = str(path).replace("\\", "/").strip("/").lower()
    if not value:
        return False
    filenames, folders = rules
    parts = value.split("/")
    return (
        any(fnmatch.fnmatchcase(parts[-1], str(pattern).lower())
            for pattern in filenames)
        or any(
            fnmatch.fnmatchcase(folder, str(pattern).lower())
            for folder in parts[:-1]
            for pattern in folders
        )
    )
