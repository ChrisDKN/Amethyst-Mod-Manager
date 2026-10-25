from __future__ import annotations

import json as _json
import math
from pathlib import Path


MAX_JSON_DEPTH = 128
MAX_JSON_BYTES = 8 * 1024 * 1024
JSONDecodeError = _json.JSONDecodeError


def _check(raw, max_bytes, max_depth):
    if max_bytes is not None and len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("JSON size limit exceeded.")
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError("JSON nesting limit exceeded.")
        elif char in "]}":
            depth -= 1


def _constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite.")
    return number


def loads(raw, *, max_bytes=MAX_JSON_BYTES, max_depth=MAX_JSON_DEPTH):
    if isinstance(raw, (bytes, bytearray)):
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError("JSON size limit exceeded.")
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise TypeError("Expected JSON text or UTF-8 bytes.")
    _check(raw, max_bytes, max_depth)
    try:
        return _json.loads(raw, parse_constant=_constant, parse_float=_float)
    except RecursionError as exc:
        raise ValueError("JSON nesting limit exceeded.") from exc


def dumps(value, *, max_bytes=None, max_depth=MAX_JSON_DEPTH, **kwargs):
    kwargs["allow_nan"] = False
    try:
        raw = _json.dumps(value, **kwargs)
    except RecursionError as exc:
        raise ValueError("JSON nesting limit exceeded.") from exc
    _check(raw, max_bytes, max_depth)
    return raw


def read_json(path, *, max_bytes=MAX_JSON_BYTES, max_depth=MAX_JSON_DEPTH):
    with Path(path).open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    return loads(raw, max_bytes=max_bytes, max_depth=max_depth)
