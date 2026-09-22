from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .jobs import JobError


_SOURCE_KEYS = {
    "shortcut": "shortcut_appid",
    "heroic": "heroic_app_name",
    "lutris": "lutris_slug",
    "faugus": "faugus_gameid",
}
_ID_KEYS = tuple(_SOURCE_KEYS.values())


def _selected_game(game, profile: Path):
    if profile.is_symlink() or not profile.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    if not selected.load_paths(read_only=True):
        raise JobError("game_paths_unavailable", "The saved game paths could not be loaded.")
    return selected


def _identities(selected) -> dict[str, str]:
    return {key: selected.get_saved_launcher_id(key) for key in _ID_KEYS}


def _state_files(game) -> list[Path]:
    root = Path(game.get_profile_root())
    if root.is_symlink() or (root / "profiles").is_symlink():
        raise JobError("unsafe_staging_path", "The staging root is a symbolic link.")
    files = [Path(game._paths_file)]
    for profile in sorted((root / "profiles").iterdir()):
        if profile.is_symlink():
            raise JobError("unsafe_profile_path", "A profile is a symbolic link.")
        if profile.is_dir():
            files.extend((profile / "profile_state.json",
                          profile / "profile_settings.json"))
    for path in files:
        if path.is_symlink():
            raise JobError("unsafe_game_config", "A game configuration file is a symbolic link.")
    return files


def _revision(selected, profile: Path, files: list[Path]) -> str:
    basis = {
        "profile": str(profile),
        "game_path": str(selected.get_game_path() or ""),
        "prefix_path": str(selected.get_prefix_path() or ""),
        "identities": _identities(selected),
    }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode())
    for path in files:
        digest.update(str(path).encode())
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def get_launcher(game, profile: Path) -> dict:
    selected = _selected_game(game, profile)
    identities = _identities(selected)
    source = next((name for name, key in _SOURCE_KEYS.items()
                   if identities[key]), None)
    return {
        "game": {"id": str(game.game_id), "name": str(game.name)},
        "profile": profile.name,
        "game_path": str(selected.get_game_path() or "") or None,
        "prefix_path": str(selected.get_prefix_path() or "") or None,
        "source": source,
        "launcher_id": identities[_SOURCE_KEYS[source]] if source else None,
        "identities": identities,
        "revision": _revision(selected, profile, _state_files(game)),
    }


def set_launcher(game, profile: Path, source: str, launcher_id: str | None,
                 revision: str, log) -> dict:
    from Utils.deployment.locking import coordinated_game_mutation

    with coordinated_game_mutation(game, "headless.game_launcher"):
        selected = _selected_game(game, profile)
        files = _state_files(game)
        current_revision = _revision(selected, profile, files)
        if revision != current_revision:
            raise JobError("game_launcher_changed",
                           "The game configuration changed; read the launcher again.",
                           {"current_revision": current_revision})
        desired = {key: "" for key in _ID_KEYS}
        if source in _SOURCE_KEYS:
            desired[_SOURCE_KEYS[source]] = launcher_id or ""
        if _identities(selected) == desired:
            result = get_launcher(game, profile)
            result["changed"] = False
            return result
        snapshots = {path: path.read_bytes() if path.exists() else None
                     for path in files}
        try:
            selected.set_launcher_ids(**desired)
            saved = _selected_game(game, profile)
            if _identities(saved) != desired:
                raise JobError("game_launcher_save_failed",
                               "The launcher identity could not be saved.")
        except Exception as exc:
            for path, contents in snapshots.items():
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(contents)
            if isinstance(exc, JobError):
                raise
            raise JobError("game_launcher_save_failed",
                           "The launcher identity could not be saved.",
                           {"reason": str(exc)}) from exc
        active = getattr(game, "_active_profile_dir", None)
        if active is not None and Path(active).resolve() == profile.resolve():
            game.load_paths()
        log(f"Saved launcher identity for {game.name}/{profile.name}.")
        result = get_launcher(game, profile)
        result["changed"] = True
        return result
