#!/usr/bin/env python3
"""Validate a Qt .ts file for problems that survive translation and only bite
at RUNTIME - where a translator can never see them.

Two classes of problem, both silent today:

1. NO CONTEXT. A ``<context>`` with an empty ``<name/>`` means pyside6-lupdate
   could not work out which class a ``self.tr()`` call belongs to. Those strings
   are dead: the app looks them up under the real class context and never gets a
   hit, so a translator fills in an entry that does nothing. Known causes are
   aliasing the translate function (``tr = QCoreApplication.translate`` - lupdate
   only matches the literal name and reads ``tr(a, b)`` as
   ``tr(source, disambiguation)``) and a ``def`` inside a block in a MODULE-LEVEL
   function, which breaks lupdate's scope tracking for the rest of that file.

2. UNSAFE PLACEHOLDERS. Our translated strings are fed to ``str.format()``. A
   translation that renames ``{0}`` to ``{O}``, invents a ``{2}`` the source
   never had, or leaves an unbalanced brace raises KeyError / IndexError /
   ValueError *inside a Qt callback* - which in this app poisons the callback
   rather than surfacing a clean error. Dropping a placeholder is safe (format
   ignores extra args) so it is reported as a warning, not a failure.

Usage:
    check_ts.py <file.ts> [more.ts ...]      # report; exit 1 if anything fatal
    check_ts.py --quarantine <file.ts>       # instead of failing on an unsafe
                                             # translation, mark it
                                             # type="unfinished" so lrelease
                                             # omits it and Qt falls back to
                                             # English. The text is left in the
                                             # file for the translator to fix.
    check_ts.py --warnings <file.ts>         # also report non-fatal warnings

Pure standard library; safe to run repeatedly.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# The str.format fields we care about: {0}, {1}, ... Anything else brace-shaped
# is caught by the trial format() below rather than by pattern matching.
_INDEX = re.compile(r"\{(\d+)\}")


def _indices(text: str) -> set[int]:
    return {int(m.group(1)) for m in _INDEX.finditer(text or "")}


def _format_safe(text: str, argc: int) -> str | None:
    """Return None if ``text`` survives .format() with *argc* positional args,
    else a short description of how it fails. ``argc`` comes from the SOURCE
    string, because that is what the call site actually passes."""
    try:
        (text or "").format(*[""] * argc)
    except Exception as e:                      # KeyError/IndexError/ValueError
        return f"{type(e).__name__}: {e}"
    return None


def check(path: Path, quarantine: bool = False
          ) -> tuple[list[str], list[str], int]:
    """Return (fatal, warnings, quarantined) for one .ts.

    With *quarantine*, an unsafe translation is marked unfinished in place
    rather than being fatal — but it still counts, because the caller must
    report it: the string silently reverts to English until someone fixes it.
    """
    fatal: list[str] = []
    warn: list[str] = []
    quarantined = 0
    tree = ET.parse(path)
    root = tree.getroot()
    touched = False

    for ctx in root.findall("context"):
        name_el = ctx.find("name")
        name = (name_el.text or "") if name_el is not None else ""
        msgs = ctx.findall("message")

        if not name.strip() and msgs:
            for m in msgs:
                src = m.find("source")
                s = (src.text or "") if src is not None else ""
                fatal.append(f"no context (untranslatable): {s[:90]!r}")

        for m in msgs:
            src_el, tr_el = m.find("source"), m.find("translation")
            if src_el is None or tr_el is None:
                continue
            source = src_el.text or ""
            trans = tr_el.text or ""
            # An unfinished/empty entry never reaches format() - Qt falls back
            # to the source string, so there is nothing to validate.
            if not trans or tr_el.get("type") == "unfinished":
                continue

            src_idx = _indices(source)
            # The call site passes as many args as the SOURCE declares.
            argc = (max(src_idx) + 1) if src_idx else 0
            where = f"[{name or '?'}] {source[:60]!r}"

            problem = _format_safe(trans, argc)
            if problem is None:
                missing = src_idx - _indices(trans)
                if missing:
                    warn.append(
                        f"{where}: translation drops "
                        f"{', '.join('{%d}' % i for i in sorted(missing))} "
                        f"(safe, but the value is lost)")
                continue

            if quarantine:
                tr_el.set("type", "unfinished")
                touched = True
                quarantined += 1
                warn.append(f"{where}: QUARANTINED -> {problem}")
            else:
                fatal.append(f"{where}: unsafe translation -> {problem}\n"
                             f"      translation: {trans[:90]!r}")

    if touched:
        body = ET.tostring(root, encoding="unicode")
        path.write_text('<?xml version="1.0" encoding="utf-8"?>\n'
                        '<!DOCTYPE TS>\n' + body + "\n", encoding="utf-8")
    return fatal, warn, quarantined


def main() -> int:
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not files:
        print(__doc__)
        return 2
    quarantine = "--quarantine" in flags
    show_warn = "--warnings" in flags or quarantine

    rc = 0
    for f in files:
        p = Path(f)
        if not p.is_file():
            print(f"  {f}: not found", file=sys.stderr)
            rc = 1
            continue
        try:
            fatal, warn, quarantined = check(p, quarantine=quarantine)
        except ET.ParseError as e:
            print(f"error: {f} is not valid XML: {e}", file=sys.stderr)
            rc = 1
            continue
        if fatal:
            print(f"error: {len(fatal)} problem(s) in {f}:", file=sys.stderr)
            for line in fatal:
                print(f"  - {line}", file=sys.stderr)
            rc = 1
        if warn and show_warn:
            print(f"note: {len(warn)} warning(s) in {f}:", file=sys.stderr)
            for line in warn:
                print(f"  - {line}", file=sys.stderr)
        # A quarantined entry is non-fatal (the build continues, the string
        # falls back to English) but NOT clean — the caller has to surface it or
        # the language quietly loses strings on every refresh. Dropped-
        # placeholder warnings alone do not count; they need no action.
        if quarantined:
            rc = 1
        if not fatal and not warn:
            print(f"  {f}: clean")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
