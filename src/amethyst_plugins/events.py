from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps


EVENT_NAMES = frozenset({
    "context.changed", "mods.changed", "mods.added", "mods.removed", "mods.enabled",
    "mods.reordered", "plugins.changed", "plugins.added", "plugins.removed",
    "plugins.enabled", "plugins.reordered", "install.completed", "deploy.completed",
    "restore.completed", "job.completed", "selection.changed",
})
_lock = threading.RLock()
_history = deque(maxlen=512)
_listeners = {}
_sequence = 0
_listener_sequence = 0


def publish_event(name, game_id="", profile="", **data):
    global _sequence
    if name not in EVENT_NAMES:
        raise ValueError(f"Unknown manager event: {name}")
    with _lock:
        _sequence += 1
        event = {"sequence": _sequence, "type": name, "game_id": game_id,
                 "profile": profile, "time": datetime.now(timezone.utc).isoformat(),
                 "data": deepcopy(data)}
        _history.append(event)
        listeners = tuple(_listeners.values())
    for callback in listeners:
        try:
            callback(deepcopy(event))
        except Exception:
            pass
    return deepcopy(event)


def read_events(after=0, *, game_id=None, profile=None):
    if type(after) is not int or after < 0:
        raise ValueError("Event cursor must be a nonnegative integer.")
    with _lock:
        oldest = _history[0]["sequence"] if _history else _sequence + 1
        return {"events": deepcopy([
            e for e in _history if e["sequence"] > after
            and (game_id is None or e["game_id"] == game_id)
            and (profile is None or e["profile"] == profile)
        ]), "next_sequence": _sequence, "truncated": after < oldest - 1}


def listen(callback):
    global _listener_sequence
    with _lock:
        _listener_sequence += 1
        key = _listener_sequence
        _listeners[key] = callback
    def unsubscribe():
        with _lock:
            _listeners.pop(key, None)
    return unsubscribe


def operation_event(kind):
    def decorate(function):
        @wraps(function)
        def run(game, profile, *args, **kwargs):
            success = False
            try:
                result = function(game, profile, *args, **kwargs)
                success = bool(result)
                return result
            finally:
                publish_event(f"{kind}.completed", game.game_id, str(profile), success=success)
        return run
    return decorate
