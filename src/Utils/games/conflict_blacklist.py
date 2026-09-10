"""Per-game overrides for handler filename and folder exclusions."""

from __future__ import annotations

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


def normalize_pattern(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Blacklist patterns must be strings.")
    pattern = value.strip().lower()
    if not pattern or any(char in pattern for char in ("/", "\\", "\n", "\r", "\0")):
        raise ValueError("Enter a filename or folder-name pattern without path separators.")
    return pattern


def builtin_rules(game) -> tuple[frozenset[str], frozenset[str]]:
    return tuple(
        frozenset(str(pattern).lower() for pattern in
                  (getattr(game, attribute, None) or ()))
        for attribute in ("conflict_ignore_filenames", "conflict_ignore_foldernames")
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


def effective_rules(game, overrides: BlacklistOverrides | None = None
                    ) -> tuple[frozenset[str], frozenset[str]]:
    if overrides is None:
        overrides = load_overrides(game)
    files, folders = builtin_rules(game)
    return ((files - overrides.disabled_files) | overrides.files,
            (folders - overrides.disabled_folders) | overrides.folders)
