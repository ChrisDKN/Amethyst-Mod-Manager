from __future__ import annotations

import difflib
import json

_MISSING = object()


def _value(old, mine, author):
    if mine == old or mine == author:
        return author
    if author == old:
        return mine
    if all(isinstance(v, dict) for v in (old, mine, author)):
        result = {}
        for key in old.keys() | mine.keys() | author.keys():
            value = _value(old.get(key, _MISSING), mine.get(key, _MISSING), author.get(key, _MISSING))
            if value is not _MISSING:
                result[key] = value
        return result
    raise ValueError("Overlapping changes")


def merge_content(path, baseline, current, authored):
    if current == baseline or current == authored:
        return authored
    if authored == baseline:
        return current
    if path.endswith(".json"):
        merged = _value(json.loads(baseline), json.loads(current), json.loads(authored))
        return json.dumps(merged, indent=2, ensure_ascii=False).encode("utf-8")
    base = baseline.decode("utf-8-sig").splitlines(keepends=True)
    def edits(data):
        lines = data.decode("utf-8-sig").splitlines(keepends=True)
        return [(i, j, lines[a:b]) for op, i, j, a, b in difflib.SequenceMatcher(a=base, b=lines, autojunk=False).get_opcodes() if op != "equal"]
    mine, author = edits(current), edits(authored)
    for i, j, replacement in mine:
        for a, b, other in author:
            if (i, j, replacement) == (a, b, other):
                continue
            if max(i, a) < min(j, b) or (i == j and a <= i <= b) or (a == b and i <= a <= j):
                raise ValueError("Overlapping line changes")
    combined = mine + [e for e in author if e not in mine]
    for i, j, replacement in sorted(combined, key=lambda e: (e[0], e[1]), reverse=True):
        base[i:j] = replacement
    return "".join(base).encode("utf-8")


def baseline_candidate(key):
    return (key.startswith("profiles/") or key.lower().endswith((".ini", ".json", ".txt"))) and not protected(key)


def protected(key):
    parts = key.lower().split("/")
    return any(p in {"saves", "overwrite"} for p in parts[1:-1])
