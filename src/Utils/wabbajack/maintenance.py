from __future__ import annotations

import shutil
from pathlib import Path

from .diagnostics import emit, emit_exception
from .games import configured_games
from .profiles import referenced_profiles
from .store import Store, installations


def cache_items():
    from Utils.downloads.cache import dir_size
    result = []
    for game in configured_games().values():
        root = Path(game.get_profile_root())
        for info in installations(root):
            directory = Path(info["directory"])
            if info.get("status") == "committing":
                continue
            paths = [directory / "work", directory / "backups"]
            abandoned = not referenced_profiles(directory, root)
            if abandoned:
                paths = [directory]
            count = sum(dir_size(p) for p in paths if p.is_dir() and not p.is_symlink())
            if count:
                result.append({"directory": directory, "profile_root": root, "bytes": count,
                               "name": info.get("name", directory.name), "abandoned": abandoned})
    return result


def clear_caches():
    from Utils.app_log import app_log
    log = lambda message: app_log("[wabbajack] " + message)
    count, errors = 0, []
    items = cache_items()
    emit(log, "cache.cleanup.started", items=len(items),
         bytes=sum(item["bytes"] for item in items))
    for item in items:
        store = None
        try:
            store = Store(item["directory"], item["profile_root"], log=log)
            with store.exclusive():
                if not referenced_profiles(store.directory, store.profile_root, log):
                    store.close()
                    store = None
                    shutil.rmtree(item["directory"])
                else:
                    for name in ("work", "backups"):
                        path = store.directory / name
                        if path.is_symlink():
                            raise ValueError("Managed cache directory was replaced by a link")
                        if path.is_dir():
                            shutil.rmtree(path)
                    with store.db:
                        store.db.execute("DELETE FROM completed")
                count += 1
        except Exception as exc:
            emit_exception(log, "cache.cleanup.item_failed", exc,
                           name=item["name"], directory=item["directory"])
            errors.append(f"{item['name']}: {exc}")
        finally:
            if store is not None:
                store.close()
    emit(log, "cache.cleanup.completed", removed=count, errors=len(errors))
    return count, errors
