"""Apply deployed Witcher mod priorities and restore the previous settings."""

from __future__ import annotations

import codecs
import json
import re
from pathlib import Path

from Utils.atomic_write import write_atomic, write_atomic_text
from Utils.deployment import _resolve_root_path
from Utils.mods.modlist import read_modlist
from Utils.wine.prefix import normalize_prefix_path

_JOURNAL = "witcher3-mod-settings.json"
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)", re.MULTILINE)
_SETTING = re.compile(r"^[ \t]*(Enabled|Priority)[ \t]*=", re.IGNORECASE)


def _decode(data: bytes) -> tuple[str, str, bytes]:
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"),
                          (codecs.BOM_UTF16_LE, "utf-16-le"),
                          (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, "surrogateescape"), encoding, bom
    return data.decode("utf-8", "surrogateescape"), "utf-8", b""


def _blocks(text: str) -> list[tuple[str, str]]:
    headers = list(_SECTION.finditer(text))
    result = [("", text[:headers[0].start()] if headers else text)]
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        result.append((header.group(1).strip().casefold(), text[header.start():end]))
    return result


def _replace_blocks(text: str, replacements: dict[str, str]) -> str:
    remaining = dict(replacements)
    output = []
    for name, block in _blocks(text):
        if name in replacements:
            block = remaining.pop(name, "")
        if not block:
            continue
        if output and not output[-1].endswith("\n"):
            output.append("\n")
        output.append(block)
    result = "".join(output)
    for block in remaining.values():
        if not block:
            continue
        if result and not result.endswith("\n"):
            result += "\n"
        result += block
    return result


def _set_priority(block: str, name: str, priority: int, newline: str) -> str:
    values = {"enabled": "Enabled=1", "priority": f"Priority={priority}"}
    lines = []
    for line in (block or f"[{name}]{newline}").splitlines(keepends=True):
        match = _SETTING.match(line)
        if match:
            value = values.pop(match.group(1).casefold(), None)
            if value is not None:
                lines.append(value + newline)
        else:
            lines.append(line)
    result = "".join(lines)
    if result and not result.endswith("\n"):
        result += newline
    return result + "".join(value + newline for value in values.values())


def _deployed_mods(game) -> list[str]:
    from Utils.filegraph.deploy import require_active
    from Utils.vfs import effective_tool_game_root

    active = require_active()
    profile = game._active_profile_dir or game.get_profile_root() / "profiles/default"
    modlist = profile / "modlist.txt"
    if not modlist.is_file():
        raise RuntimeError(f"Witcher 3 modlist not found: {modlist}")
    ranks = {entry.name: index for index, entry in enumerate(read_modlist(modlist))
             if entry.enabled and not entry.is_separator}
    last_rank = max(ranks.values(), default=-1) + 1
    folders: dict[str, tuple[int, str]] = {}
    for entry in active.plan.entries:
        parts = entry.destination.replace("\\", "/").split("/")
        if (entry.target != "game" or len(parts) < 3
                or parts[0].casefold() != "mods"
                or not parts[1].casefold().startswith("mod")):
            continue
        name = parts[1]
        rank = (-1 if entry.provider_kind in {"overwrite", "root"}
                else ranks.get(entry.mod_name, last_rank))
        key = name.casefold()
        previous = folders.get(key)
        if previous is None or rank < previous[0]:
            folders[key] = (rank, name)

    view = effective_tool_game_root(game)
    cache = {}
    present = []
    for rank, name in folders.values():
        folder = _resolve_root_path(view, Path("mods") / name, dir_cache=cache)
        if folder.is_dir():
            present.append(("_mergedfiles" not in name.casefold(), rank,
                            folder.name.casefold(), folder.name))
    return [item[-1] for item in sorted(present)]


def restore_mod_settings(game, log_fn=None) -> None:
    journal = game.get_profile_root() / _JOURNAL
    if not journal.is_file():
        return
    from Utils.deployment.shared import RestoreIncompleteError

    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
        target = Path(state["path"])
        if not target.is_absolute():
            raise ValueError("Invalid Witcher mod-settings journal path")
        original = bytes.fromhex(state["original"])
        written = bytes.fromhex(state["written"])
        current = target.read_bytes() if target.exists() else None
        if current is None or current == written:
            restored = original
        else:
            text, encoding, bom = _decode(current)
            old_blocks = dict(_blocks(_decode(original)[0]))
            replacements = {name: old_blocks.get(name, "") for name in state["managed"]}
            restored = bom + _replace_blocks(text, replacements).encode(
                encoding, "surrogateescape")
        if not state["existed"] and not restored:
            target.unlink(missing_ok=True)
        elif current != restored:
            write_atomic(target, restored)
        journal.unlink()
    except Exception as exc:
        raise RestoreIncompleteError(f"Could not restore Witcher mod settings: {exc}") from exc
    if log_fn:
        log_fn(f"Witcher 3 load order: restored {target}.")


def sync_mod_settings(game, log_fn=None) -> None:
    log = log_fn or (lambda _message: None)
    restore_mod_settings(game, log_fn=log)
    names = _deployed_mods(game)
    if not names:
        return
    prefix = game.get_prefix_path()
    if prefix is None:
        log("Witcher 3 load order: configure the game prefix to sync mods.settings.")
        return
    prefix = normalize_prefix_path(Path(prefix))
    if not (prefix / "drive_c").is_dir():
        log("Witcher 3 load order: launch the game once to create its prefix, then deploy again.")
        return
    if len(names) > 9999:
        raise RuntimeError("Witcher 3 supports at most 9999 explicit mod priorities")
    target = _resolve_root_path(prefix, Path(
        "drive_c/users/steamuser/Documents/The Witcher 3/mods.settings")).resolve()
    existed = target.is_file()
    original = target.read_bytes() if existed else b""
    text, encoding, bom = _decode(original)
    blocks = dict(_blocks(text))
    newline = "\r\n" if "\r\n" in text else "\n"
    replacements = {
        name.casefold(): _set_priority(blocks.get(name.casefold(), ""), name, index, newline)
        for index, name in enumerate(names, 1)
    }
    written = bom + _replace_blocks(text, replacements).encode(encoding, "surrogateescape")
    journal = game.get_profile_root() / _JOURNAL
    write_atomic_text(journal, json.dumps({
        "path": str(target), "existed": existed, "managed": list(replacements),
        "original": original.hex(), "written": written.hex(),
    }))
    if written != original:
        write_atomic(target, written)
    log(f"Witcher 3 load order: synced {len(names)} mod folder(s) to {target}.")
