"""Bounded base assets for previews; callers receive independent shape lists."""
from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from pathlib import Path

_items = OrderedDict()
_lock = threading.Lock()
_bytes = 0
_LIMIT = 192 * 1024 * 1024


def input_key(value):
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(value).digest()
    if isinstance(value, (tuple, list)):
        return tuple(input_key(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, input_key(v)) for k, v in value.items()))
    return str(value) if isinstance(value, Path) else value


def _base(source, skeleton=False):
    global _bytes
    data = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    key = (skeleton, input_key(data))
    with _lock:
        hit = _items.pop(key, None)
        if hit is not None:
            _items[key] = hit
            return hit[0]
    if skeleton:
        from Utils.assets.skinning import read_skeleton
        value = read_skeleton(data)
        cost = len(value) * 2048
    else:
        from Utils.assets.nif import read_nif
        value = read_nif(data)
        cost = len(data) + sum(
            4096 + sum(len(getattr(s, attr)) * unit for attr, unit in (
                ("vertices", 160), ("normals", 160), ("tangents", 160),
                ("uvs", 128), ("triangles", 192), ("colors", 192),
                ("skin_weights", 448), ("bones", 1024))) for s in value.shapes)
    if cost <= _LIMIT:
        with _lock:
            old = _items.pop(key, None)
            _bytes -= old[1] if old else 0
            _items[key] = value, cost
            _bytes += cost
            while _bytes > _LIMIT:
                _, (_, size) = _items.popitem(last=False)
                _bytes -= size
    return value


def read_model(source):
    base = _base(source)
    model = copy.copy(base)
    model.shapes = []
    for original in base.shapes:
        shape = copy.copy(original)
        for name, value in vars(original).items():
            if isinstance(value, list):
                setattr(shape, name, list(value))
        model.shapes.append(shape)
    model.skipped = dict(base.skipped)
    return model


def skeleton_bones(source):
    return dict(_base(source, True))
