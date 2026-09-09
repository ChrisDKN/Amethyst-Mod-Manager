from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from .games import nexus_domain
from .hashes import file_hash
from .manifest import stock_folder
from .paths import WabbajackError, source_path, within
from .diagnostics import emit, emit_exception
from .post_install_rules import (display_rule, display_signature, matching_rules,
                                 omitted_stock_paths, post_install_notices, stock_copy_rule)

_MOD_METADATA_SIGNATURE = "wabbajack-meta:1:"


def is_mod_metadata(path):
    parts = path.casefold().split("/")
    return len(parts) == 4 and parts[:2] == ["root", "mods"] and parts[3] == "meta.ini"


def mod_metadata_signature(signature):
    return signature if signature.startswith(_MOD_METADATA_SIGNATURE) else _MOD_METADATA_SIGNATURE + signature


def display_supported(package):
    return any(display_rule(d.path) is not None for d in package.directives)


def stock_copy(request):
    stock = stock_folder(request.package)
    rule = stock_copy_rule(request)
    if rule:
        if not any(d.path.casefold() == (stock + "/" + rule.executable).casefold() for d in request.package.directives):
            return next((d.path[:len(stock)] for d in request.package.directives
                         if d.path.casefold().startswith(stock.casefold() + "/")), stock)
    return ""


def stock_sources(request, stop=None):
    from .setup_tasks import _files
    rule = stock_copy_rule(request)
    if rule is None:
        return
    root = next((p for name, p in request.game_roots.items() if nexus_domain(name) == rule.source_game), None)
    if root is None or not source_path(root, rule.executable).is_file():
        raise WabbajackError(rule.source_error)
    for rel, path in _files(root, stop):
        first = rel.split("/")[0].casefold()
        if first in rule.folders or ("/" not in rel and path.suffix.casefold() in rule.root_suffixes):
            if path.name.casefold() not in rule.exclude_names:
                yield rel, path


def preflight_post_install(request, check, stop=None, *, reusable=None, log=None):
    from .manifest import optional_game_file_directives
    from .store import publication_copy_required
    started = time.monotonic()
    size = 0
    emit(log, "post_install.preflight.started",
         stock_copy=stock_copy(request), rules=[rule.id for rule in matching_rules(request.package, request.game)],
         display=request.setup_options.get("display"))
    if stock_copy(request):
        try:
            files = list(stock_sources(request, stop))
            database = request.directory / "state.sqlite"
            old = {}
            if database.is_file():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    old = dict(db.execute("SELECT path,signature FROM outputs"))
            stock = stock_copy(request)
            ignored = optional_game_file_directives(request.package)
            authored = {("root/" + d.path).casefold() for d in request.package.directives
                        if d.path not in ignored}
            published_reuse = staged_reuse = authored_reuse = 0
            for rel, path in files:
                key = f"root/{stock}/{rel}"
                if key.casefold() in authored:
                    authored_reuse += 1
                    continue
                source_size = path.stat().st_size
                digest = None
                target = within(request.directory, key)
                if key in old and target.is_file():
                    digest = file_hash(path, stop)
                    if old[key] == "stock-copy:1:" + digest and file_hash(target, stop) == digest:
                        if reusable is not None:
                            reusable.add(key)
                        published_reuse += 1
                        continue
                staged = within(request.directory / "work" / "output", f"{stock}/{rel}")
                if staged.is_file() and not staged.is_symlink():
                    digest = digest or file_hash(path, stop)
                    if staged.stat().st_size == source_size and file_hash(staged, stop) == digest:
                        staged_reuse += 1
                        if publication_copy_required(staged, target,
                                                     request.directory / "work"):
                            size += source_size
                        continue
                size += source_size * 2
            check("pass", "Stock game setup", f"Copy {len(files):,} original game files into the managed stock game; original files remain unchanged")
            emit(log, "post_install.stock.plan", folder=stock, files=len(files),
                 reusable_files=published_reuse + staged_reuse,
                 published_reusable_files=published_reuse,
                 staged_reusable_files=staged_reuse,
                 authored_files=authored_reuse, required_bytes=size)
        except (ValueError, OSError, sqlite3.Error) as exc:
            emit_exception(log, "post_install.stock.preflight_failed", exc)
            check("error", "Stock game setup", exc)
    for notice in post_install_notices(request):
        check(notice.status, notice.name, notice.detail)
    display = request.setup_options.get("display")
    if display:
        if not display_supported(request.package):
            check("error", "Display settings", "This package has no supported resolution configuration files")
        elif not isinstance(display, list) or len(display) != 2 or any(type(n) is not int or not 320 <= n <= 16384 for n in display):
            check("error", "Display settings", "Choose a resolution between 320 and 16384 pixels per dimension")
        else:
            check("pass", "Display settings", f"Apply the selected {display[0]} × {display[1]} resolution to supported authored profile and display-tweak files")
    emit(log, "post_install.preflight.completed", required_bytes=size,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return size


def prepare_stock(request, store, desired, stop, progress, log=None):
    stock = stock_copy(request)
    if not stock:
        emit(log, "post_install.stock.skipped")
        return
    started = time.monotonic()
    sources = list(stock_sources(request, stop))
    emit(log, "post_install.stock.started", folder=stock, files=len(sources),
         bytes=sum(path.stat().st_size for _, path in sources))
    within(store.work / "output", stock).mkdir(parents=True, exist_ok=True)
    existing = {key.casefold() for key in desired}
    old = store.outputs()
    reused = copied = authored = 0
    for index, (rel, source) in enumerate(sources):
        if stop.is_set():
            raise InterruptedError("Stock game setup stopped")
        progress("Preparing stock game", index, len(sources), rel)
        key = f"root/{stock}/{rel}"
        if key.casefold() in existing:
            authored += 1
            continue
        digest = file_hash(source, stop)
        sig = "stock-copy:1:" + digest
        target = within(store.work / "output", f"{stock}/{rel}")
        prior = old.get(key)
        if prior and prior["signature"] == sig and store.target(key).is_file() and file_hash(store.target(key), stop) == digest:
            target = store.target(key)
            reused += 1
        elif not target.is_file() or file_hash(target, stop) != digest:
            store._copy(source, target, stop=stop, progress=lambda done, total:
                        progress("Preparing stock game", index, len(sources), f"{rel} ({100 * done // max(1, total)}%)"))
            copied += 1
        desired[key] = {"source": str(target), "authored_hash": digest, "signature": sig}
    progress("Preparing stock game", len(sources), len(sources), "Stock game verified")
    emit(log, "post_install.stock.completed", folder=stock, files=len(sources),
         reused_files=reused, copied_files=copied, authored_files=authored,
         elapsed_seconds=round(time.monotonic() - started, 3))


def _ini_values(text, section, values):
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"\s*\[" + re.escape(section) + r"\]\s*(?:[;#].*)?$", line, re.I)]
    if not starts:
        return text.rstrip("\r\n") + newline + f"[{section}]" + newline + "".join(f"{key}={value}{newline}" for key, value in values.items())
    start = starts[-1]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    found = set()
    for i in range(start + 1, end):
        for key, value in values.items():
            match = re.match(r"(\s*" + re.escape(key) + r"\s*=\s*)([^;#\r\n]*)(.*)", lines[i].rstrip("\r\n"), re.I)
            if match:
                lines[i] = match[1] + str(value) + match[3] + newline
                found.add(key)
    additions = [f"{key}={value}{newline}" for key, value in values.items() if key not in found]
    if additions and end and not lines[end - 1].endswith(("\r", "\n")):
        lines[end - 1] += newline
    lines[end:end] = additions
    return "".join(lines)


def reset_mod_endorsements(store, desired, stop, progress, log=None):
    rows = [(key, row) for key, row in desired.items() if is_mod_metadata(key)]
    changed = 0
    for index, (key, row) in enumerate(rows):
        if stop.is_set():
            raise InterruptedError("Mod metadata adjustment stopped")
        progress("Preparing mod metadata", index, len(rows), key)
        source = Path(row["source"])
        if source.stat().st_size > 8 * 1024 ** 2:
            raise WabbajackError(f"Mod metadata exceeds the adjustment limit: {key}")
        raw = source.read_bytes()
        text = raw.decode("utf-8-sig", errors="surrogateescape")
        updated = _ini_values(text, "General", {"endorsed": 0})
        signature = mod_metadata_signature(row["signature"])
        if updated == text:
            desired[key] = {**row, "signature": signature}
            continue
        target = within(store.work / "mod-metadata", key)
        write_atomic_text(target, updated,
                          encoding="utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8",
                          errors="surrogateescape")
        digest = file_hash(target, stop)
        desired[key] = {**row, "source": str(target), "authored_hash": digest,
                        "signature": signature}
        changed += 1
    if rows:
        progress("Preparing mod metadata", len(rows), len(rows),
                 "Wabbajack mod endorsements reset")
    emit(log, "post_install.mod_metadata.completed", files=len(rows), changed=changed)


def apply_adjustments(request, store, desired, stop, progress, log=None):
    started = time.monotonic()
    reset_mod_endorsements(store, desired, stop, progress, log=log)
    omitted = omitted_stock_paths(request)
    if omitted:
        removed = []
        for key in list(desired):
            if key.casefold() in omitted:
                desired.pop(key)
                removed.append(key)
        emit(log, "post_install.adjustment.omit_stock_files", removed=removed)
    display = request.setup_options.get("display")
    if not display:
        emit(log, "post_install.display.skipped")
        return
    width, height = display
    count = 0
    for key, row in list(desired.items()):
        if stop.is_set():
            raise InterruptedError("Configuration adjustment stopped")
        rule = display_rule(key, installed=True)
        if rule is None:
            continue
        values = {name: value.format(width=width, height=height) for name, value in rule.values}
        source = Path(row["source"])
        if source.stat().st_size > 8 * 1024 ** 2:
            raise WabbajackError(f"Configuration exceeds the display-adjustment limit: {key}")
        text = source.read_bytes().decode("utf-8-sig")
        text = _ini_values(text, rule.section, values)
        target = within(store.work / "adjustments", key)
        write_atomic_text(target, text)
        digest = file_hash(target, stop)
        desired[key] = {"source": str(target), "authored_hash": digest,
                        "signature": display_signature(display, row["signature"])}
        count += 1
        emit(log, "post_install.display.file", path=key, width=width,
             height=height, hash=digest)
        progress("Applying selected display settings", count, 0, key)
    emit(log, "post_install.display.completed", files=count,
         width=width, height=height,
         elapsed_seconds=round(time.monotonic() - started, 3))
