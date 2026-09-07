from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from .games import nexus_domain
from .hashes import file_hash
from .manifest import stock_folder
from .paths import WabbajackError, source_path, within


def nuclear_sunset(package):
    return nexus_domain(package.game) == "newvegas" and package.name.casefold().strip() == "nuclear sunset"


def display_supported(package):
    return any((d.path.startswith("mods/") and Path(d.path).name.casefold() == "ssedisplaytweaks.ini")
               or (d.path.startswith("profiles/") and len(d.path.split("/")) == 3
                   and (Path(d.path).name.casefold().endswith("prefs.ini") or Path(d.path).name.casefold() in {
                       "oblivion.ini", "falloutcustom.ini", "skyrimcustom.ini", "fallout76custom.ini", "user.settings", "dx12user.settings"}))
               for d in package.directives)


def stock_copy(request):
    stock = stock_folder(request.package)
    if nuclear_sunset(request.package) and stock.casefold() == "[nodelete] stock new vegas":
        if not any(d.path.casefold() == (stock + "/FalloutNV.exe").casefold() for d in request.package.directives):
            return next((d.path[:len(stock)] for d in request.package.directives
                         if d.path.casefold().startswith(stock.casefold() + "/")), stock)
    return ""


def stock_sources(request, stop=None):
    from .setup_tasks import _files
    root = next((p for name, p in request.game_roots.items() if nexus_domain(name) == "newvegas"), None)
    if root is None or not source_path(root, "FalloutNV.exe").is_file():
        raise WabbajackError("Select the original New Vegas installation for the stock-game copy")
    for rel, path in _files(root, stop):
        first = rel.split("/")[0].casefold()
        if first in {"data", "fallout new vegas", "redists", "directx"} or ("/" not in rel and path.suffix.casefold() in {".exe", ".dll", ".ini", ".vdf"}):
            if path.name.casefold() not in {"falloutnv_backup.exe", "fnvpatch.exe", "patcher.exe"}:
                yield rel, path


def preflight_post_install(request, check, stop=None, *, reusable=None):
    size = 0
    if stock_copy(request):
        try:
            files = list(stock_sources(request, stop))
            database = request.directory / "state.sqlite"
            old = {}
            if database.is_file():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    old = dict(db.execute("SELECT path,signature FROM outputs"))
            stock = stock_copy(request)
            for rel, path in files:
                key = f"root/{stock}/{rel}"
                target = within(request.directory, key)
                if key in old and target.is_file():
                    digest = file_hash(path, stop)
                    if old[key] == "stock-copy:1:" + digest and file_hash(target, stop) == digest:
                        if reusable is not None:
                            reusable.add(key)
                        continue
                size += path.stat().st_size * 2
            check("pass", "Stock game setup", f"Copy {len(files):,} original game files into the managed stock game; original files remain unchanged")
        except (ValueError, OSError, sqlite3.Error) as exc:
            check("error", "Stock game setup", exc)
    if nuclear_sunset(request.package):
        check("warning", "Author instructions", "Nuclear Sunset's Linux guide specifies Proton 11 and a separate YUPTTW update. Confirm the selected runtime and external content match the guide before launch.")
        if not getattr(request.game, "auto_4gb_patch", False):
            check("error", "Stock game patch", "Enable automatic New Vegas 4GB patching so deployment patches the managed stock executable")
    radio = [d for d in request.package.directives if d.path.split("/")[0].strip("_ ").casefold() == "radio fix"]
    if radio:
        check("manual", "Author instructions", "The supplied Radio Fix requires audio conversion after reconstruction. Its batch-script format has not been verified for automatic processing; follow the author's radio setup instructions before launching.")
    display = request.setup_options.get("display")
    if display:
        if not display_supported(request.package):
            check("error", "Display settings", "This package has no supported resolution configuration files")
        elif not isinstance(display, list) or len(display) != 2 or any(type(n) is not int or not 320 <= n <= 16384 for n in display):
            check("error", "Display settings", "Choose a resolution between 320 and 16384 pixels per dimension")
        else:
            check("pass", "Display settings", f"Apply the selected {display[0]} × {display[1]} resolution to supported authored profile and display-tweak files")
    return size


def prepare_stock(request, store, desired, stop, progress):
    stock = stock_copy(request)
    if not stock:
        return
    sources = list(stock_sources(request, stop))
    within(store.work / "output", stock).mkdir(parents=True, exist_ok=True)
    existing = {key.casefold() for key in desired}
    old = store.outputs()
    for index, (rel, source) in enumerate(sources):
        if stop.is_set():
            raise InterruptedError("Stock game setup stopped")
        progress("Preparing stock game", index, len(sources), rel)
        key = f"root/{stock}/{rel}"
        if key.casefold() in existing:
            continue
        digest = file_hash(source, stop)
        sig = "stock-copy:1:" + digest
        target = within(store.work / "output", f"{stock}/{rel}")
        prior = old.get(key)
        if prior and prior["signature"] == sig and store.target(key).is_file() and file_hash(store.target(key), stop) == digest:
            target = store.target(key)
        elif not target.is_file() or file_hash(target, stop) != digest:
            store._copy(source, target, stop=stop, progress=lambda done, total:
                        progress("Preparing stock game", index, len(sources), f"{rel} ({100 * done // max(1, total)}%)"))
        desired[key] = {"source": str(target), "authored_hash": digest, "signature": sig}
    progress("Preparing stock game", len(sources), len(sources), "Stock game verified")


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


def apply_adjustments(request, store, desired, stop, progress):
    stock = stock_folder(request.package)
    if nuclear_sunset(request.package) and "nuclear:proton-dxvk" in request.fixes and stock:
        for key in list(desired):
            if key.casefold() in {f"root/{stock}/{name}".casefold() for name in ("d3d9.dll", "dxvk.conf")}:
                desired.pop(key)
    display = request.setup_options.get("display")
    if not display:
        return
    width, height = display
    count = 0
    for key, row in list(desired.items()):
        if stop.is_set():
            raise InterruptedError("Configuration adjustment stopped")
        name = Path(key).name.casefold()
        profile = key.startswith("profiles/") and "/ini files/" in key
        values = None
        if profile and (name.endswith("prefs.ini") or name in {"oblivion.ini", "falloutcustom.ini", "skyrimcustom.ini", "fallout76custom.ini"}):
            values = ("Display", {"iSize W": width, "iSize H": height})
        elif profile and name in {"user.settings", "dx12user.settings"}:
            values = ("Viewport", {"Resolution": f"{width}x{height}"})
        elif key.startswith("root/mods/") and name == "ssedisplaytweaks.ini":
            values = ("Render", {"Resolution": f"{width}x{height}"})
        if values is None:
            continue
        source = Path(row["source"])
        if source.stat().st_size > 8 * 1024 ** 2:
            raise WabbajackError(f"Configuration exceeds the display-adjustment limit: {key}")
        text = source.read_bytes().decode("utf-8-sig")
        text = _ini_values(text, *values)
        target = within(store.work / "adjustments", key)
        write_atomic_text(target, text)
        digest = file_hash(target, stop)
        desired[key] = {"source": str(target), "authored_hash": digest,
                        "signature": f"display:1:{width}x{height}:" + row["signature"]}
        count += 1
        progress("Applying selected display settings", count, 0, key)
