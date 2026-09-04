#!/usr/bin/env python3
"""Machine-translate a Qt .ts with a LOCAL LibreTranslate server (free, offline).

A fallback for tools/i18n/i18n_deepl.py when the DeepL quota is exhausted. Same
behaviour: reads the English base for the string list, translates the unfinished
entries, writes finished <translation>s. Quality is rougher than DeepL — treat
as a reviewable placeholder.

Start a local server first (models auto-download on first run), e.g.:
    libretranslate --load-only en,nl,cs --host 127.0.0.1 --port 5000

Then:
    AMM_I18N_OUT_DIR=src/translations \\
        python3 tools/i18n/i18n_libre.py nl --only-unfinished
    AMM_LT_URL=http://127.0.0.1:5000 python3 tools/i18n/i18n_libre.py cs

Options mirror i18n_deepl.py: --only-unfinished, --limit=N, --dry-run.
Env: AMM_LT_URL (default http://127.0.0.1:5000), AMM_I18N_OUT_DIR (output dir).

Placeholders {0},{1},… are swapped for opaque sentinels LibreTranslate won't
touch, then restored — so they survive verbatim. No third-party deps.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from ts_merge import (read_existing, pending_texts, apply_translations,
                      write_ts)

EN_TS = (Path(__file__).resolve().parents[2]
         / "src" / "translations" / "amethyst_en.ts")
OUT_DIR = Path(os.environ.get(
    "AMM_I18N_OUT_DIR",
    str(Path(__file__).resolve().parents[2] / "Localisation")))
LT_URL = os.environ.get("AMM_LT_URL", "http://127.0.0.1:5000").rstrip("/")

# Map our .qm codes → LibreTranslate's language codes. Mostly identical, but two
# differ (verified against libretranslate.com/languages):
#   pt_BR -> pt-BR  (LT DOES have Brazilian Portuguese as a distinct code)
#   zh    -> zh-Hans (LT has NO plain "zh"; our zh is Simplified Chinese)
# All 12 of our shipped languages are supported.
_LT_TARGET = {
    "nl": "nl", "cs": "cs", "de": "de", "fr": "fr", "es": "es", "it": "it",
    "pt": "pt", "pt_BR": "pt-BR", "ru": "ru", "pl": "pl",
    "zh": "zh-Hans", "ja": "ja", "ko": "ko",
    # extras LT supports if ever wanted: uk, tr, ar, sv, da, nb, fi, hu, ro, …
    "uk": "uk", "tr": "tr",
}

_PLACEHOLDER = re.compile(r"\{\d+\}")
# Sentinel LibreTranslate leaves intact (⟦⟧/PUA chars get DROPPED; ASCII
# "XPHnX" survives verbatim). Restored to {n} afterwards.
_SENT = "XPH{}X"


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace {0},{1},… with opaque sentinels; return (text, originals)."""
    originals: list[str] = []

    def sub(m):
        originals.append(m.group(0))
        return _SENT.format(len(originals) - 1)

    return _PLACEHOLDER.sub(sub, text), originals


def _unprotect(text: str, originals: list[str]) -> str:
    for i, orig in enumerate(originals):
        text = text.replace(_SENT.format(i), orig)
    # Some engines add spaces inside the sentinel — be lenient.
    return text


def _translate_one(text: str, target: str) -> str:
    prot, originals = _protect(text)
    body = json.dumps({
        "q": prot, "source": "en", "target": target, "format": "text",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LT_URL}/translate", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return _unprotect(out["translatedText"], originals)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(
                f"LibreTranslate {e.code}: {e.read().decode('utf-8','replace')[:200]}")
        except urllib.error.URLError as e:
            raise SystemExit(
                f"Can't reach LibreTranslate at {LT_URL} ({e}). "
                f"Start it: libretranslate --load-only en,{target}")
    raise SystemExit("LibreTranslate: repeated errors, giving up.")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    raw = args[0].replace("-", "_")
    short = (f"{raw.split('_')[0].lower()}_{raw.split('_')[1].upper()}"
             if "_" in raw else raw.lower())
    lt_code = _LT_TARGET.get(short) or _LT_TARGET.get(short.split("_")[0])
    if lt_code is None:
        raise SystemExit(f"LibreTranslate has no target for '{short}'. "
                         f"Known: {', '.join(sorted(_LT_TARGET))}")

    if not EN_TS.is_file():
        raise SystemExit(f"{EN_TS} not found — run ./tools/i18n/i18n_update.sh first.")
    limit = None
    for f in flags:
        if f.startswith("--limit"):
            try:
                limit = int(f.split("=", 1)[1])
            except (IndexError, ValueError):
                raise SystemExit("Use --limit=N")
    only_unfinished = "--only-unfinished" in flags
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_ts = OUT_DIR / f"amethyst_{short}.ts"

    # Unique source strings from the English base.
    en_root = ET.parse(EN_TS).getroot()
    sources = sorted({(m.find("source").text or "")
                      for m in en_root.iter("message")} - {""})

    # Keyed by (context, source), not source alone — see ts_merge for why
    # (the same word is translated differently per context, and a source-only
    # key silently overwrites a translator's per-context work).
    existing = read_existing(out_ts) if only_unfinished else {}

    # Pending if ANY context still needs the text, not just if it's unknown.
    todo = pending_texts(en_root, existing)
    if limit is not None:
        todo = todo[:limit]
    print(f"{short}: {len(sources)} strings, {len(existing)} done, "
          f"{len(todo)} to translate (LibreTranslate @ {LT_URL})"
          + (" (dry-run)" if "--dry-run" in flags else ""))
    if "--dry-run" in flags:
        for s in todo[:15]:
            print("  →", repr(s))
        return 0

    fresh: dict[str, str] = {}
    for i, s in enumerate(todo, 1):
        fresh[s] = _translate_one(s, lt_code)
        if i % 20 == 0 or i == len(todo):
            print(f"  …{i}/{len(todo)}")

    # Clone the English base, swap in the translations per (context, source).
    tree = ET.parse(EN_TS)
    root = tree.getroot()
    root.set("language", short)
    kept, new = apply_translations(root, existing, fresh)

    write_ts(root, out_ts)
    print(f"wrote {out_ts} ({kept} kept, {new} translated)")
    print(f"now compile:  pyside6-lrelease {out_ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
