from __future__ import annotations

import sys
import threading
from collections import OrderedDict


def retained_size(value) -> int:
    size = 0
    seen = set()
    stack = [value]
    while stack:
        item = stack.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(item)
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            stack.extend(item)
        elif hasattr(item, "__dict__"):
            stack.append(vars(item))
    return size


class ByteLruCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self._items = OrderedDict()
        self._costs = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            try:
                value = self._items.pop(key)
            except KeyError:
                return default
            self._items[key] = value
            return value

    def put(self, key, value) -> None:
        cost = retained_size(key) + retained_size(value)
        with self._lock:
            if key in self._items:
                self._items.pop(key)
                self._bytes -= self._costs.pop(key, 0)
            self._items[key] = value
            self._costs[key] = cost
            self._bytes += cost
            while self._bytes > self.max_bytes and self._items:
                old_key, _old_value = self._items.popitem(last=False)
                self._bytes -= self._costs.pop(old_key, 0)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._costs.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def usage(self) -> tuple[int, int]:
        with self._lock:
            return len(self._items), self._bytes
