"""Toolkit-neutral projection of Filegraph destinations for the Data tab."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


DataPath = tuple[str, ...]
DataRow = tuple[int, DataPath, str]
_ROOT_ORDER = {"<root>": 0, "<prefix>": 1, "<external>": 2}


def name_sort_key(name: str, *, at_root: bool = False):
    return (_ROOT_ORDER.get(name, 3) if at_root else 0, name.casefold(), name)


def path_parts(path: str | DataPath) -> DataPath:
    if isinstance(path, tuple):
        return path
    return tuple(part for part in path.replace("\\", "/").split("/") if part)


def path_text(path: DataPath) -> str:
    return "/".join(part.lstrip("/") for part in path)


def file_extension(parts: DataPath) -> str:
    name = parts[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


@dataclass
class DestinationProjection:
    game_root: Path | None = None
    prefix_root: Path | None = None
    data_root: Path | None = None
    staging_root: Path | None = None
    _targets: dict[str, DataPath] = field(default_factory=dict, compare=False)
    _data_parts: DataPath | None = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.game_root is not None and self.data_root is not None:
            if self.game_root.is_relative_to(self.data_root):
                self._data_parts = ()
            elif self.data_root.is_relative_to(self.game_root):
                self._data_parts = tuple(
                    part.casefold()
                    for part in self.data_root.relative_to(self.game_root).parts)

    @classmethod
    def from_game(cls, game):
        def root(getter):
            try:
                value = getattr(game, getter)()
                return Path(value).expanduser().resolve(strict=False) if value else None
            except (AttributeError, OSError, RuntimeError, ValueError):
                return None

        return cls(
            root("get_game_path"), root("get_prefix_path"),
            root("get_mod_data_path"), root("get_effective_mod_staging_path"))

    @property
    def key(self):
        return self.game_root, self.prefix_root, self.data_root, self.staging_root

    def project(self, candidate_id, mod_name, target, destination) -> DataRow | None:
        if target == "game":
            base = ("<root>",)
        elif target == "prefix":
            base = ("<prefix>",)
        elif target.startswith("custom:"):
            base = self._targets.get(target)
            if base is None:
                root = Path(target[len("custom:"):])
                base = ("<external>", str(root))
                for label, parent in (("<prefix>", self.prefix_root),
                                      ("<root>", self.game_root)):
                    if parent is not None and root.is_relative_to(parent):
                        base = (label, *root.relative_to(parent).parts)
                        break
                self._targets[target] = base
        else:
            return None
        parts = base + path_parts(destination)
        count = len(self._data_parts or ())
        if (self._data_parts is not None and parts[0] == "<root>"
                and len(parts) > count + 1
                and tuple(part.casefold() for part in parts[1:count + 1])
                == self._data_parts):
            parts = parts[count + 1:]
        return candidate_id, parts, mod_name

    def absolute_path(self, parts: DataPath) -> Path | None:
        if not parts:
            return None
        if parts[0] == "<external>" and len(parts) > 1:
            return Path(parts[1]).joinpath(*parts[2:])
        root = {"<root>": self.game_root, "<prefix>": self.prefix_root}.get(parts[0])
        if parts[0] in _ROOT_ORDER:
            return root.joinpath(*parts[1:]) if root is not None else None
        if self._data_parts is None:
            return None
        root = self.data_root if self._data_parts else self.game_root
        return root.joinpath(*parts)


def project_entries(game, projection: DestinationProjection, entries) -> list[DataRow]:
    rows = []
    game_rows = []
    hook = getattr(game, "data_tab_display_paths", None)
    for candidate_id, mod, target, destination, _contested in entries:
        row = projection.project(candidate_id, mod, target, destination)
        if row is not None:
            if target == "game" and callable(hook):
                game_rows.append((len(rows), (destination, mod)))
            rows.append(row)
    if game_rows and callable(hook):
        try:
            shown = list(hook([entry for _index, entry in game_rows]))
        except Exception:
            return rows
        if len(shown) == len(game_rows) and all(
                isinstance(path, str) and path_parts(path) for path in shown):
            for (index, _entry), path in zip(game_rows, shown):
                candidate_id, _parts, mod = rows[index]
                rows[index] = candidate_id, path_parts(path), mod
    return rows


def build_data_tree(entries: list[DataRow],
                    contested_ids: set[int] | None = None, *,
                    only_conflicts: bool = False,
                    inc_exts: frozenset | None = None,
                    exc_exts: frozenset | None = None,
                    keep_extra=None) -> dict:
    contested_ids = contested_ids or set()
    inc_exts = inc_exts or frozenset()
    exc_exts = exc_exts or frozenset()
    tree: dict = {}
    for candidate_id, parts, mod_name in entries:
        if only_conflicts and candidate_id not in contested_ids:
            continue
        extension = file_extension(parts)
        if inc_exts and extension not in inc_exts:
            continue
        if extension in exc_exts:
            continue
        if keep_extra is not None and not keep_extra(
                path_text(parts).casefold(), mod_name):
            continue
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append((parts[-1], mod_name, candidate_id))
    return tree


def filetype_counts(entries: list[DataRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _candidate_id, parts, _mod in entries:
        extension = file_extension(parts)
        counts[extension] = counts.get(extension, 0) + 1
    return counts
