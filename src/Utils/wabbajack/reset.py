from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from Utils.atomic_write import write_atomic
from Utils.deployment.locking import game_mutation_lock
from Utils.mods.modlist import modlist_lock
from Utils.plugins import invalidate_plugins_cache
from Utils.profiles.backup import create_backup
from Utils.profiles.state import read_profile_settings
from .hashes import XXHash
from .paths import WabbajackError, relative_path


_FILES = ("modlist.txt", "plugins.txt", "loadorder.txt")


def _lines(data, *, mods=False):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    result = {}
    for line in text.splitlines():
        if mods:
            if line[:1] not in {"+", "-", "*"} or len(line) < 2:
                continue
            name = line[1:]
        else:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.removeprefix("*")
        result.setdefault(name.casefold(), line)
    return result


def _with_extras(authored, current, known, *, mods=False):
    extras = [line for name, line in _lines(current, mods=mods).items()
              if name not in known]
    if not extras:
        return authored, 0
    try:
        text = authored.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = authored.decode("cp1252")
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.rstrip("\r\n") + newline + newline.join(extras) + newline
    if mods:
        encoding = "utf-8"
    else:
        from Utils.plugins import _plugins_txt_encoding
        encoding = _plugins_txt_encoding(text)
    count = sum(not line[1:].endswith("_separator") for line in extras) if mods else len(extras)
    return text.encode(encoding), count


def _saved_order(directory, settings):
    database = directory / "state.sqlite"
    if database.is_symlink():
        raise WabbajackError("The saved installation database is a symbolic link")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        metadata = {key: json.loads(value) for key, value in
                    db.execute("SELECT key,value FROM metadata")}
        if metadata.get("id") != settings.get("wabbajack_install_id"):
            raise WabbajackError("The profile belongs to a different Wabbajack installation")
        if metadata.get("status") != "complete" or db.execute("SELECT 1 FROM journal LIMIT 1").fetchone():
            raise WabbajackError("Finish the current Wabbajack installation before resetting its load order")
        name = metadata.get("profile_names", {}).get(settings.get("wabbajack_profile"))
        if not name or "/" in relative_path(name):
            raise WabbajackError("The original Wabbajack profile could not be found")
        files = {}
        for filename in _FILES:
            key = f"profiles/{name}/{filename}"
            row = db.execute(
                "SELECT b.content,o.authored_hash FROM outputs o "
                "LEFT JOIN baselines b ON b.path=o.path WHERE o.path=?", (key,)).fetchone()
            if row is None:
                continue
            content, expected = row
            if content is None or len(content) > 2 * 1024 * 1024:
                raise WabbajackError(f"The saved original {filename} is unavailable")
            digest = XXHash()
            digest.update(content)
            if digest.digest() != expected:
                raise WabbajackError(f"The saved original {filename} failed verification")
            files[filename] = content
    if "modlist.txt" not in files:
        raise WabbajackError("This installation has no saved mod order")
    return files


def reset_load_order(game, profile_dir, log_fn=None):
    log = log_fn or (lambda message: None)
    profile_dir = Path(profile_dir)
    root = Path(game.get_profile_root()).resolve()
    if profile_dir.is_symlink() or profile_dir.resolve().parent != root / "profiles":
        raise WabbajackError("The profile is outside this game's profile directory")
    settings = read_profile_settings(profile_dir)
    saved = settings.get("wabbajack_directory")
    if not saved or not settings.get("wabbajack_install_id"):
        raise WabbajackError("The active profile is not a Wabbajack profile")
    directory = Path(saved)
    if directory.is_symlink() or directory.resolve().parent != root / ".wabbajack":
        raise WabbajackError("The installation is outside managed Wabbajack storage")
    lock_path = directory / "install.lock"
    if lock_path.is_symlink():
        raise WabbajackError("The installation lock is a symbolic link")
    with game_mutation_lock(game), lock_path.open("rb") as lock, modlist_lock(profile_dir / "modlist.txt"):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WabbajackError("This modlist already has an active operation") from exc
        authored = _saved_order(directory.resolve(), settings)
        before = {}
        for filename in authored:
            path = profile_dir / filename
            if path.is_symlink():
                raise WabbajackError(f"The profile's {filename} is a symbolic link")
            before[filename] = path.read_bytes() if path.is_file() else None
        known_plugins = set(_lines(authored.get("plugins.txt", b""))) | set(_lines(authored.get("loadorder.txt", b"")))
        payloads, extra_counts = {}, {}
        for filename, content in authored.items():
            mods = filename == "modlist.txt"
            known = set(_lines(content, mods=True)) if mods else known_plugins
            payloads[filename], extra_counts[filename] = _with_extras(
                content, before[filename] or b"", known, mods=mods)
        create_backup(profile_dir, log_fn=log)
        written = []
        try:
            for filename, content in payloads.items():
                write_atomic(profile_dir / filename, content)
                written.append(filename)
        except Exception:
            for filename in reversed(written):
                path = profile_dir / filename
                if before[filename] is None:
                    path.unlink()
                else:
                    write_atomic(path, before[filename])
            raise
        finally:
            for filename in ("plugins.txt", "loadorder.txt"):
                invalidate_plugins_cache(profile_dir / filename)
    ordered = sum(not name.endswith("_separator") for name in _lines(authored["modlist.txt"], mods=True))
    log(f"Wabbajack load order reset: {ordered} mods, {len(known_plugins)} plugins; "
        "restored saved profile files without checking installed mod files.")
    return {"ordered": ordered, "plugins": len(known_plugins),
            "extra_mods": extra_counts.get("modlist.txt", 0),
            "extra_plugins": max(extra_counts.get("plugins.txt", 0), extra_counts.get("loadorder.txt", 0))}
