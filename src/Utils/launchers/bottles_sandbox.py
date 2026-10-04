"""Filesystem access for Bottles' supported containers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from Utils.launchers.bottles import BOTTLES_CPAK_ORIGIN, BOTTLES_FLATPAK_ID


def _run(*args):
    from Utils.flatpak.sandbox import _host_cmd
    try:
        result = subprocess.run(_host_cmd(list(args)), capture_output=True,
                                text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{args[0]} did not respond within 30 seconds") from exc
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()
                           or f"{args[0]} exited with status {result.returncode}")
    return result.stdout


def _cpak_permissions():
    apps = json.loads(_run("cpak", "list", "--json"))
    app = next((item for item in apps if item.get("origin") == BOTTLES_CPAK_ORIGIN), None)
    if app is None:
        raise RuntimeError("Bottles is not listed as an installed Cpak application")
    version = app.get("version", "")
    if not version or Path(version).name != version or version in (".", ".."):
        raise RuntimeError("Cpak returned an invalid Bottles version")
    override = Path.home() / ".config/cpak/overrides" / BOTTLES_CPAK_ORIGIN / version / "cpak.json"
    config = (json.loads(override.read_text(encoding="utf-8")) if override.exists()
              else app["parsed_override"])
    permissions = config.get("filesystem", [])
    if not isinstance(permissions, list) or any(not isinstance(p, dict) for p in permissions):
        raise RuntimeError("Cpak returned invalid filesystem permissions")
    return permissions


def _cpak_covers(entry, path):
    if entry.get("access") != "read-write":
        return False
    value = entry.get("path", "")
    if value in ("host", "/"):
        return True
    if value == "home" or value.startswith("home/"):
        base = Path.home() / value.removeprefix("home").lstrip("/")
    elif value.startswith("/"):
        base = Path(value)
    else:
        return False
    return path == base or base in path.parents


def ensure_bottles_paths(root, paths, log_fn=None):
    log = log_fn or (lambda message: None)
    package = root.package
    if package not in ("flatpak", "cpak"):
        return
    wanted = []
    for raw in paths:
        if raw is None:
            continue
        path = Path(raw).absolute()
        for candidate in (path, path.resolve()):
            if candidate not in wanted:
                wanted.append(candidate)
    if package == "cpak":
        permissions = _cpak_permissions()
        missing = [path for path in wanted
                   if not any(_cpak_covers(entry, path) for entry in permissions)]
        if not missing:
            return
        if os.environ.get("AMM_CPAK_OVERRIDE", "1") == "0":
            raise RuntimeError("Cpak needs access to " + ", ".join(map(str, missing)))
        for path in missing:
            value = ("home/" + str(path.relative_to(Path.home()))
                     if path.is_relative_to(Path.home()) else str(path))
            permissions.append({"path": value, "access": "read-write"})
        _run("cpak", "override", BOTTLES_CPAK_ORIGIN, "--key", "filesystem",
             "--value", json.dumps(permissions))
        log("Bottles: granted Cpak access to " + ", ".join(map(str, missing))
            + ". Restart Bottles for the new access to take effect.")
        return

    from Utils.flatpak.sandbox import (
        _baseline_filesystems, _granted_filesystems, _covered, _grant_paths,
    )
    tokens, granted = _granted_filesystems(BOTTLES_FLATPAK_ID)
    base_tokens, base_grants = _baseline_filesystems(BOTTLES_FLATPAK_ID)
    granted += base_grants + [root.data_dir.parent.parent]
    missing = [path for path in wanted if not _covered(path, granted, tokens | base_tokens)]
    if missing:
        if (os.environ.get("AMM_FLATPAK_OVERRIDE", "1") == "0"
                or not _grant_paths(BOTTLES_FLATPAK_ID, missing, log)):
            raise RuntimeError("Bottles Flatpak needs access to " + ", ".join(map(str, missing)))


def ensure_cpak_game_paths(game, game_root, staging, profile_dir, log_fn):
    from Utils.executables.launch import bottles_programs_for_launch
    from Utils.launchers.bottles import find_bottles_launch_info, _cpak_export
    from Utils.flatpak.sandbox import _wanted_roots
    if not _cpak_export().is_file():
        return False
    info = find_bottles_launch_info(bottles_programs_for_launch(game))
    if info is None or info.root.package != "cpak":
        return False
    ensure_bottles_paths(info.root, [info.prefix, game_root,
                                   *_wanted_roots(staging, profile_dir)], log_fn)
    log_fn("Bottles (Cpak): external launcher handoff is unavailable; "
           "Cpak does not expose host commands to the bottle.")
    return True
