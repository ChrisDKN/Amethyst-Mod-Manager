from __future__ import annotations

import configparser
import json
from dataclasses import dataclass, field
from pathlib import Path

from Utils.atomic_write import atomic_writer
from Utils.mods.metadata import meta_file_lock


SECTION = "AmethystArchivePacking"


@dataclass
class PackingState:
    archives: list[str] = field(default_factory=list)
    generated_stubs: list[str] = field(default_factory=list)


def _config(meta_path: Path):
    cp = configparser.RawConfigParser(allow_no_value=True, strict=False)
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as stream:
            cp.read_file(stream)
    return cp


def _names(cp, key, extensions):
    raw = cp.get(SECTION, key, fallback="[]")
    values = json.loads(raw)
    if not isinstance(values, list) or any(
        not isinstance(name, str) or not name or "/" in name or "\\" in name
        or "\0" in name or Path(name).suffix.lower() not in extensions
        for name in values
    ):
        raise ValueError("Invalid archive packing metadata")
    return sorted(set(values), key=str.lower)


def read_packing_state(mod_dir: Path) -> PackingState:
    cp = _config(mod_dir / "meta.ini")
    return PackingState(
        _names(cp, "archives", {".bsa", ".ba2"}),
        _names(cp, "generatedStubs", {".esp", ".esm", ".esl"}),
    )


def stage_packing_state(mod_dir: Path, state: PackingState, target: Path):
    cp = _config(mod_dir / "meta.ini")
    cp.remove_section(SECTION)
    if state.archives or state.generated_stubs:
        cp.add_section(SECTION)
        cp.set(SECTION, "archives", json.dumps(sorted(set(state.archives), key=str.lower),
                                             ensure_ascii=False))
        cp.set(SECTION, "generatedStubs", json.dumps(sorted(set(state.generated_stubs), key=str.lower),
                                                    ensure_ascii=False))
    with atomic_writer(target) as stream:
        cp.write(stream)


def tracked_archives(mod_dir: Path, state: PackingState) -> list[Path]:
    root = mod_dir.resolve()
    return [mod_dir / name for name in state.archives
            if (mod_dir / name).is_file() and (mod_dir / name).resolve().parent == root]


def clear_packing_state(mod_dir: Path):
    meta_path = mod_dir / "meta.ini"
    with meta_file_lock(meta_path):
        cp = _config(meta_path)
        if cp.remove_section(SECTION):
            with atomic_writer(meta_path) as stream:
                cp.write(stream)
