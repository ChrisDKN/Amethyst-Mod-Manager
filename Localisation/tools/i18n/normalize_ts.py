#!/usr/bin/env python3
"""Normalise a Qt .ts file to a single canonical special-character encoding, so
different tools stop producing spurious diffs against each other.

The problem: `pyside6-lupdate` writes quotes/apostrophes as XML ENTITIES
(&quot; &apos;), while Qt Linguist (some versions) and hand-edited / contributor
files use LITERAL characters ("  '). Both are valid and compile to the IDENTICAL
.qm — but mixing them makes every edited line churn in git.

We standardise on LITERAL quotes/apostrophes (the human-readable, contributor-
friendly form). `&amp;`, `&lt;`, `&gt;` are LEFT as entities — those MUST stay
escaped to keep the XML valid.

This only touches text INSIDE <source> / <translation> / <comment> / etc. (not
the tags themselves), by decoding &quot;→" and &apos;→' there. Idempotent.

Usage:
    python3 tools/i18n/normalize_ts.py <file.ts> [more.ts ...]
    python3 tools/i18n/normalize_ts.py --check <file.ts>   # exit 1 if changes needed

Pure standard library; safe to run repeatedly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Only decode inside element TEXT, never in tags/attributes. .ts text lives
# between > and < ; decode the two "cosmetic" entities there. &amp;/&lt;/&gt;
# are left alone (removing them would corrupt the XML).
_TEXT = re.compile(r">([^<]*)<")


def _decode_text(m: "re.Match") -> str:
    body = m.group(1)
    if "&quot;" in body or "&apos;" in body:
        body = body.replace("&quot;", '"').replace("&apos;", "'")
    return ">" + body + "<"


def normalize(text: str) -> str:
    return _TEXT.sub(_decode_text, text)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    if not args:
        print(__doc__)
        return 2
    changed_any = False
    for a in args:
        p = Path(a)
        if not p.is_file():
            print(f"  {a}: not found", file=sys.stderr)
            continue
        src = p.read_text(encoding="utf-8")
        out = normalize(src)
        if out != src:
            changed_any = True
            if check:
                print(f"  {a}: NEEDS normalising")
            else:
                p.write_text(out, encoding="utf-8")
                print(f"  {a}: normalised")
        elif not check:
            print(f"  {a}: already clean")
    return 1 if (check and changed_any) else 0


if __name__ == "__main__":
    raise SystemExit(main())
