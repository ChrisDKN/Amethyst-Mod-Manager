from __future__ import annotations

import threading
from pathlib import Path

from LOOT.loot_sorter import is_available, loot
from LOOT.worker import LootWorker


_worker = None
_lock = threading.Lock()


def check_esl_eligible(plugin_path: Path, game_type_attr: str) -> bool:
    global _worker
    if (not is_available() or not plugin_path.is_file()
            or getattr(loot.GameType, game_type_attr, None) is None):
        return False
    with _lock:
        try:
            if (_worker is None or _worker.process is None
                    or _worker.process.poll() is not None):
                if _worker is not None:
                    _worker.close()
                _worker = LootWorker()
            return _worker.call("eligibility", path=str(plugin_path.absolute()),
                                game_type=game_type_attr, timeout=30.0)
        except Exception as exc:
            from Utils.app_log import app_log
            app_log(f"[loot] ESL eligibility failed for {plugin_path.name}: {exc}")
            return False
