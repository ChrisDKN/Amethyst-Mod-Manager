from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as xml
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_BACKUP_DIR = "Backup"
_MANIFEST = ".amethyst-modimporter.json"
_MARKER = "MODIFIED by Mod Importer @ "
_MISSING = object()


class SjsonError(ValueError):
    pass


class _RawString(str):
    pass


class _SjsonParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> SjsonError:
        line = self.text.count("\n", 0, self.pos) + 1
        start = self.text.rfind("\n", 0, self.pos)
        return SjsonError(f"{message} at line {line}, column {self.pos - start}")

    def peek(self, count: int = 1) -> str:
        return self.text[self.pos:self.pos + count]

    def skip_space(self) -> None:
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.peek(2) == "//":
                end = self.text.find("\n", self.pos + 2)
                self.pos = len(self.text) if end < 0 else end + 1
            elif self.peek(2) == "/*":
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.error("Unterminated comment")
                self.pos = end + 2
            else:
                return

    def string(self, *, identifier: bool = False) -> str:
        self.skip_space()
        if self.peek(3) == '"""':
            self.pos += 3
            start = self.pos
            while True:
                end = self.text.find('"""', self.pos)
                if end < 0:
                    raise self.error("Unterminated raw string")
                while end + 3 < len(self.text) and self.text[end + 3] == '"':
                    end += 1
                value = self.text[start:end]
                self.pos = end + 3
                return _RawString(value)
        if self.peek() == '"':
            self.pos += 1
            start = self.pos
            end = self.text.find('"', start)
            if end < 0:
                raise self.error("Unterminated string")
            self.pos = end + 1
            return self.text[start:end]
        if not identifier:
            raise self.error("Quoted string expected")
        start = self.pos
        while self.pos < len(self.text) and (
                self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
            self.pos += 1
        if self.pos == start:
            raise self.error("Identifier expected")
        return self.text[start:self.pos]

    def value(self):
        self.skip_space()
        if self.peek() == "{":
            return self.mapping(braced=True)
        if self.peek() == "[":
            return self.sequence()
        if self.peek() == '"':
            return self.string()
        for token, value in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(token, self.pos):
                self.pos += len(token)
                return value
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in " \t\r\n,]}":
            self.pos += 1
        raw = self.text[start:self.pos]
        try:
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        except ValueError as exc:
            raise self.error(f"Invalid value {raw!r}") from exc

    def mapping(self, *, braced: bool = False) -> dict:
        result: dict = {}
        self.skip_space()
        if self.peek() == "{":
            braced = True
            self.pos += 1
        while True:
            self.skip_space()
            if self.pos >= len(self.text):
                if braced:
                    raise self.error("Unterminated object")
                return result
            if braced and self.peek() == "}":
                self.pos += 1
                return result
            key = self.string(identifier=True)
            self.skip_space()
            if self.peek() in ("=", ":"):
                self.pos += 1
            result[key] = self.value()
            self.skip_space()
            if self.peek() == ",":
                self.pos += 1

    def sequence(self) -> list:
        result = []
        self.pos += 1
        while True:
            self.skip_space()
            if self.pos >= len(self.text):
                raise self.error("Unterminated list")
            if self.peek() == "]":
                self.pos += 1
                return result
            result.append(self.value())
            self.skip_space()
            if self.peek() == ",":
                self.pos += 1


def _sjson_load(text: str) -> dict:
    return _SjsonParser(text).mapping()


def _sjson_dump(value, *, level: int = 0) -> str:
    indent = "  "
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, _RawString):
        return f'"""{value}"""'
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + "".join(_sjson_dump(v, level=level + 1) for v in value) + "]"
    if isinstance(value, dict):
        lines = ["{"]
        for key, item in value.items():
            rendered_key = key if key.replace("_", "").isalnum() else f'"{key}"'
            rendered = _sjson_dump(item, level=level + 1)
            lines.append(f"{indent * (level + 1)}{rendered_key} = {rendered}")
        lines.append(f"{indent * level}}}")
        return "\n".join(lines)
    raise SjsonError(f"Unsupported SJSON value: {type(value).__name__}")


def _safe_get(value, key):
    if isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
        return value[key]
    if isinstance(value, dict):
        return value.get(key, _MISSING)
    return _MISSING


def _clear_missing(value):
    if isinstance(value, dict):
        return {key: _clear_missing(item) for key, item in value.items()
                if item is not _MISSING}
    if isinstance(value, list):
        return [_clear_missing(item) for item in value if item is not _MISSING]
    return value


def _sjson_matches(value, pattern) -> bool:
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(
            key in value and _sjson_matches(value[key], item)
            for key, item in pattern.items())
    if isinstance(pattern, list):
        return isinstance(value, list) and all(
            index < len(value) and _sjson_matches(value[index], item)
            for index, item in enumerate(pattern))
    return value == pattern


def _sjson_search(value, queries) -> object:
    pairs = list(queries)
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    else:
        return value
    for match, replacement in pairs:
        for key, item in items:
            if _sjson_matches(item, match):
                value[key] = _sjson_merge(item, replacement)
    return value


def _sjson_merge(base, patch):
    if patch is _MISSING:
        return base
    if patch == "_delete":
        return _MISSING
    if isinstance(patch, dict) and patch.get("_sequence"):
        sequence = []
        for key, value in patch.items():
            try:
                index = int(key)
            except ValueError:
                continue
            if index >= len(sequence):
                sequence.extend([_MISSING] * (index - len(sequence) + 1))
            sequence[index] = value
        patch = sequence
    if type(base) is type(patch):
        if isinstance(patch, list):
            if patch and patch[0] == "_append":
                base.extend(patch[1:])
                return base
            if patch and patch[0] == "_search":
                query = patch[1] if len(patch) > 1 else []
                return _sjson_search(base, zip(query[::2], query[1::2]))
            if patch and patch[0] == "_replace":
                return patch[1:]
            base.extend([_MISSING] * max(0, len(patch) - len(base)))
            for index, item in enumerate(patch):
                base[index] = _sjson_merge(_safe_get(base, index), item)
            return base
        if isinstance(patch, dict):
            if "_search" in patch:
                query = patch["_search"]
                return _sjson_search(base, zip(query[::2], query[1::2]))
            if patch.get("_replace"):
                return {key: item for key, item in patch.items() if key != "_replace"}
            for key, item in patch.items():
                base[key] = _sjson_merge(_safe_get(base, key), item)
            return base
    return patch


@dataclass
class _Action:
    source: Path
    mode: str
    priority: int
    order: int


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _split_lines(body: str) -> list[str]:
    groups = (line.strip().split('"') for line in body.splitlines())
    lines: list[str] = []
    line_index = -1
    multiline = False

    def consume(group: str, even: bool, line_comment: bool) -> tuple[bool, bool]:
        nonlocal line_index, multiline
        if multiline:
            parts = group.split(":-", 1)
            if len(parts) == 1:
                return not even, line_comment
            even = False
            multiline = False
            group = parts[1]
        if even:
            lines[line_index] += f'"{group}"'
            return even, line_comment
        uncommented, *commented = group.split("::", 1)
        line_comment = bool(commented)
        before, *after = uncommented.split("-:", 1)
        statements = before.split(";")
        lines[line_index] += statements[0]
        for statement in statements[1:]:
            lines.append(statement)
            line_index += 1
        if after:
            multiline = True
            return consume(after[0], even, line_comment)
        return even, line_comment

    for parts in groups:
        line_index += 1
        lines.append("")
        even = False
        line_comment = False
        for group in parts:
            even, line_comment = consume(group, even, line_comment)
            if line_comment:
                break
            even = not even
    return lines


def _tokens(line: str) -> list[str]:
    result: list[str] = []
    for index, group in enumerate(line.strip().split('"')):
        values = [group] if index % 2 else group.replace(" ", ",").split(",")
        result.extend(value for value in values if value)
    return result


class ModImporter:
    def __init__(self, game_root: Path, log_fn=None) -> None:
        self.game_root = Path(game_root)
        self.content = self.game_root / "Content"
        self.mods = self.content / "Mods"
        self.backup = self.content / _BACKUP_DIR
        self.log = log_fn or (lambda _message: None)
        self.actions: dict[Path, list[_Action]] = defaultdict(list)
        self.visited: set[Path] = set()
        self.order = 0

    def _path(self, raw: str, *, base: Path) -> Path:
        path = Path(os.path.abspath(base / raw.replace("\\", "/")))
        if not _inside(path, self.content):
            raise RuntimeError(f"Mod Importer path escapes Content: {raw}")
        return path

    def _add(self, directory: Path, sources: list[str], targets: list[str],
             mode: str, priority: int, *, reverse: bool = False) -> None:
        for raw in sources:
            source = self._path(raw, base=directory)
            expanded = sorted(source.iterdir(), key=lambda path: path.name.casefold()) \
                if source.is_dir() else [source]
            for item in expanded:
                for target_raw in targets:
                    target = self._path(target_raw, base=self.content)
                    self.order += 1
                    action = _Action(item, mode, -priority if reverse else priority,
                                     -self.order if reverse else self.order)
                    if reverse:
                        self.actions[target].insert(0, action)
                    else:
                        self.actions[target].append(action)

    def load_modfile(self, filename: Path) -> None:
        filename = Path(os.path.abspath(filename))
        if filename in self.visited or not filename.is_file():
            return
        if not _inside(filename, self.mods):
            raise RuntimeError(f"Modfile escapes Mods: {filename}")
        self.visited.add(filename)
        targets = ["Scripts/RoomManager.lua"]
        priority = 100
        directory = filename.parent
        for line_number, line in enumerate(
                _split_lines(filename.read_text(encoding="utf-8-sig")), 1):
            tokens = _tokens(line)
            if not tokens:
                continue
            lower = [token.casefold() for token in tokens]
            if lower[0] == "to":
                targets = tokens[1:] or ["Scripts/RoomManager.lua"]
            elif lower[:2] == ["load", "priority"] or lower[0] == "priority":
                index = 2 if lower[0] == "load" else 1
                priority = 100
                if len(tokens) > index:
                    try:
                        priority = int(tokens[index])
                    except ValueError:
                        pass
            elif lower[0] == "include":
                for raw in tokens[1:]:
                    include = self._path(raw, base=directory)
                    if include.is_dir():
                        for child in sorted(include.iterdir(), key=lambda p: p.name.casefold()):
                            if child.is_file():
                                self.load_modfile(child)
                    else:
                        self.load_modfile(include)
            elif lower[:2] == ["top", "import"]:
                self._add(directory, tokens[2:], targets, "top_import", priority,
                          reverse=True)
            elif lower[0] in {"import", "replace", "xml", "sjson", "csv"}:
                self._add(directory, tokens[1:], targets, lower[0], priority)
            else:
                self.log(f"  WARN: ignored {filename.relative_to(self.content)}:"
                         f"{line_number}: {line.strip()}")

    def collect(self) -> None:
        if not self.mods.is_dir():
            return
        for filename in sorted(self.mods.rglob("modfile.txt"),
                               key=lambda path: str(path).casefold()):
            self.load_modfile(filename)

    def _manifest_path(self) -> Path:
        return self.backup / _MANIFEST

    def _write_manifest(self, entries: list[dict]) -> None:
        self.backup.mkdir(parents=True, exist_ok=True)
        target = self._manifest_path()
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "files": entries}, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _replace_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        os.close(fd)
        try:
            shutil.copy2(source, temporary, follow_symlinks=True)
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _backup_targets(self) -> None:
        entries: list[dict] = []
        self.backup.mkdir(parents=True, exist_ok=True)
        for target in sorted(self.actions, key=lambda path: str(path).casefold()):
            relative = target.relative_to(self.content)
            if relative.parts and relative.parts[0].casefold() == _BACKUP_DIR.casefold():
                raise RuntimeError("Mod Importer cannot target its Backup directory")
            existed = target.is_file() or target.is_symlink()
            entries.append({"path": relative.as_posix(), "existed": existed})
            if existed:
                backup = self.backup / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup, follow_symlinks=True)
        self._write_manifest(entries)

    def _materialize(self, target: Path) -> None:
        if target.is_file() or target.is_symlink():
            self._replace_copy(target, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _xml_merge(base, patch):
        if type(base) is not type(patch):
            return patch
        if isinstance(patch, dict):
            for key, value in patch.items():
                base[key] = ModImporter._xml_merge(base.get(key), value)
            return base
        if isinstance(patch, xml.ElementTree):
            root = ModImporter._xml_merge(base.getroot(), patch.getroot())
            if root is not None:
                base._setroot(root)
            return base
        if isinstance(patch, xml.Element):
            for tag in dict.fromkeys(child.tag for child in patch):
                patch_children = patch.findall(tag)
                base_children = base.findall(tag)
                for index, patch_child in enumerate(patch_children):
                    if index >= len(base_children):
                        base.append(patch_child)
                        continue
                    child = base_children[index]
                    if patch_child.get("_delete", "").casefold() not in ("", "0", "false"):
                        base.remove(child)
                    elif patch_child.get("_replace", "").casefold() not in ("", "0", "false"):
                        child.text, child.tail = patch_child.text, patch_child.tail
                        child.attrib = {key: value for key, value in patch_child.attrib.items()
                                        if key != "_replace"}
                    else:
                        child.text = ModImporter._xml_merge(child.text, patch_child.text)
                        child.tail = ModImporter._xml_merge(child.tail, patch_child.tail)
                        child.attrib = ModImporter._xml_merge(child.attrib, patch_child.attrib)
                        ModImporter._xml_merge(child, patch_child)
            return base
        return patch

    @staticmethod
    def _csv_merge(target: Path, source: Path) -> None:
        with target.open("r", newline="", encoding="utf-8-sig") as stream:
            base = list(csv.reader(stream))
        with source.open("r", newline="", encoding="utf-8-sig") as stream:
            patch = list(csv.reader(stream))
        row = column = 0
        append = replace = False
        for values in patch:
            if (len(values) == 2 and values[0].startswith("<")
                    and values[-1].endswith(">")):
                row, column = int(values[0][1:]), int(values[-1][:-1])
                append = replace = False
            elif values == ["_append"]:
                append, replace = True, False
            elif values == ["_replace"]:
                append, replace = False, True
            elif append:
                base.append(values)
            elif replace:
                base[row] = values
                row += 1
            else:
                current = column
                for value in values:
                    if value == "_delete":
                        base[row][current] = ""
                    elif value:
                        base[row][current] = value
                    current += 1
                row += 1
        with target.open("w", newline="", encoding="utf-8-sig") as stream:
            csv.writer(stream, quoting=csv.QUOTE_MINIMAL).writerows(base)

    def _apply_action(self, target: Path, action: _Action) -> None:
        source = action.source
        if action.mode not in {"import", "top_import"} and not source.exists():
            raise FileNotFoundError(f"Mod Importer source not found: {source}")
        relative = source.relative_to(self.content).as_posix()
        if action.mode == "replace":
            self._replace_copy(source, target)
        elif action.mode == "import":
            with target.open("a", encoding="utf-8") as stream:
                stream.write(f'\nImport "../{relative}"')
        elif action.mode == "top_import":
            body = target.read_text(encoding="utf-8-sig") if target.exists() else ""
            target.write_text(f'Import "../{relative}"\n{body}', encoding="utf-8")
        elif action.mode == "sjson":
            base = _sjson_load(target.read_text(encoding="utf-8-sig"))
            patch = _sjson_load(source.read_text(encoding="utf-8-sig"))
            merged = _clear_missing(_sjson_merge(base, patch))
            target.write_text(_sjson_dump(merged, level=0), encoding="utf-8")
        elif action.mode == "xml":
            declaration = ""
            with target.open("r", encoding="utf-8-sig") as stream:
                first = stream.readline()
                if first.startswith("<?xml") and first.rstrip().endswith("?>"):
                    declaration = first
            base = xml.parse(target)
            patch = xml.parse(source)
            self._xml_merge(base, patch)
            base.write(target, encoding="unicode")
            if declaration:
                target.write_text(declaration + target.read_text(encoding="utf-8"),
                                  encoding="utf-8")
        elif action.mode == "csv":
            self._csv_merge(target, source)

    @staticmethod
    def _marker_for(target: Path) -> str:
        suffix = target.suffix.casefold()
        stamp = _MARKER + str(datetime.now())
        if suffix == ".lua":
            return "\n-- " + stamp
        if suffix == ".xml":
            return "\n<!-- " + stamp + " -->"
        if suffix == ".sjson":
            return "\n/* " + stamp + " */"
        if suffix == ".csv":
            return "\n" + stamp
        return ""

    def apply(self) -> tuple[int, int]:
        restored = self.restore()
        self.collect()
        if not self.actions:
            self.log("Hades Mod Importer: no modfile.txt directives found.")
            return restored, 0
        self._backup_targets()
        try:
            for target, actions in sorted(self.actions.items(), key=lambda item: str(item[0])):
                self._materialize(target)
                for action in sorted(actions, key=lambda item: (item.priority, item.order)):
                    self._apply_action(target, action)
                marker = self._marker_for(target)
                if marker:
                    with target.open("a", encoding="utf-8") as stream:
                        stream.write(marker)
        except Exception:
            self.restore()
            raise
        count = sum(len(actions) for actions in self.actions.values())
        self.log(f"Hades Mod Importer: applied {count} directive(s) to "
                 f"{len(self.actions)} game file(s).")
        return restored, count

    def restore(self) -> int:
        if not self.backup.is_dir():
            return 0
        manifest = self._manifest_path()
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                entries = payload["files"] if payload.get("version") == 1 else []
            except (OSError, ValueError, KeyError) as exc:
                raise RuntimeError(f"Invalid Hades Mod Importer backup: {exc}") from exc
            restored = 0
            for entry in entries:
                target = self._path(str(entry["path"]), base=self.content)
                if entry.get("existed"):
                    backup = self.backup / str(entry["path"])
                    if not backup.is_file():
                        raise RuntimeError(f"Missing Hades Mod Importer backup: {backup}")
                    self._replace_copy(backup, target)
                else:
                    target.unlink(missing_ok=True)
                restored += 1
            shutil.rmtree(self.backup)
            self.log(f"Hades Mod Importer: restored {restored} previously edited file(s).")
            return restored
        return self._restore_legacy()

    def _restore_legacy(self) -> int:
        restored = 0
        processed = False
        for backup in sorted((path for path in self.backup.rglob("*") if path.is_file()),
                             key=lambda path: len(path.parts), reverse=True):
            relative = backup.relative_to(self.backup)
            delete_marker = backup.suffix.casefold() == ".del"
            target_relative = Path(str(relative)[:-4]) if delete_marker else relative
            target = self._path(target_relative.as_posix(), base=self.content)
            known_text = target.suffix.casefold() in {".lua", ".xml", ".sjson", ".csv"}
            marked = False
            if target.is_file() and known_text:
                try:
                    marked = _MARKER in target.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError):
                    pass
            if not delete_marker and known_text and not marked:
                continue
            processed = True
            if delete_marker:
                target.unlink(missing_ok=True)
            else:
                self._replace_copy(backup, target)
            backup.unlink(missing_ok=True)
            restored += 1
        if processed:
            for directory in sorted((path for path in self.backup.rglob("*") if path.is_dir()),
                                    key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                self.backup.rmdir()
            except OSError:
                pass
            self.log(f"Hades Mod Importer: restored {restored} legacy backup file(s).")
        return restored


def apply_mod_imports(game_root: Path, log_fn=None) -> tuple[int, int]:
    return ModImporter(game_root, log_fn=log_fn).apply()


def restore_mod_imports(game_root: Path, log_fn=None) -> int:
    return ModImporter(game_root, log_fn=log_fn).restore()
