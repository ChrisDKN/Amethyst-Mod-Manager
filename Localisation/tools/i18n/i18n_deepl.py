#!/usr/bin/env python3
"""Machine-translate a Qt .ts file with the DeepL API (for TEST use).

Reads src/translations/amethyst_en.ts (the built-in English base — the app
ships English only) for the full string list, sends each <source> to DeepL, and
writes the result to Localisation/amethyst_<lang>.ts as finished <translation>s.
Compile it with `pyside6-lrelease Localisation/amethyst_<lang>.ts` (or the
standalone Localisation/tools/compile.sh); the .ts + .qm are then committed to
the Resources repo's Localisation/ folder, which the app syncs at runtime.

This produces a MACHINE translation for testing the UI end-to-end — it is not a
substitute for a human translator (idioms, mod-manager jargon, length, and tone
will be imperfect). The generated .ts stays fully editable.

Setup:
    export DEEPL_API_KEY=xxxxxxxx-....-....:fx     # from deepl.com account
    python3 tools/i18n/i18n_deepl.py fr                 # translate EN -> French

Options:
    --only-unfinished   translate only strings not already translated in the
                        target .ts (preserves existing human translations)
    --limit=N           translate at most N strings (quick smoke test)
    --dry-run           show what would be sent; call no API

Placeholders: {0}, {1}, ... are wrapped in <x> tags and sent with
tag_handling=xml + a request NOT to translate them, so DeepL preserves them
verbatim (and may reorder them to fit the target grammar, which is fine —
str.format accepts any order).

No third-party dependency: uses only the Python standard library.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from ts_merge import (read_existing, pending_texts, apply_translations,
                      write_ts)

# The English base (the full string list DeepL reads from) is the built-in
# source in src/translations/ — that folder ships ENGLISH ONLY.
EN_TS = (Path(__file__).resolve().parents[2]
         / "src" / "translations" / "amethyst_en.ts")
# Non-English translations are written here. Defaults to Localisation/, but the
# AMM_I18N_OUT_DIR env var overrides it (e.g. src/translations when that's where
# the language .ts currently live). refresh_translations.sh sets this to match
# its own target dir so DeepL writes back to the same files it merged into.
OUT_DIR = Path(
    os.environ.get(
        "AMM_I18N_OUT_DIR",
        str(Path(__file__).resolve().parents[2] / "Localisation"),
    )
)

# DeepL target-language codes differ slightly from our short codes.
DEEPL_TARGET = {
    "de": "DE", "fr": "FR", "es": "ES", "it": "IT",
    "pt": "PT-PT",       # European Portuguese
    "pt_BR": "PT-BR",    # Brazilian Portuguese (distinct from pt)
    "nl": "NL", "pl": "PL", "ru": "RU", "ja": "JA", "zh": "ZH",
    "cs": "CS", "da": "DA", "sv": "SV", "uk": "UK", "ko": "KO",
}

# {0}, {1}, ... — the str.format fields we must protect.
_PLACEHOLDER = re.compile(r"\{\d+\}")


def _endpoint(key: str) -> str:
    # Free-tier keys end in ":fx" and use the free host.
    return ("https://api-free.deepl.com/v2/translate"
            if key.endswith(":fx")
            else "https://api.deepl.com/v2/translate")


def _protect(text: str) -> str:
    """Prepare text for tag_handling=xml: XML-escape literal &/</> so DeepL's
    parser accepts it, then wrap {N} placeholders in <x> tags to keep them
    untranslated."""
    # Escape ampersands/angle brackets that would otherwise be invalid XML.
    text = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    # Placeholders were {0} -> now "{0}" (digits+braces survive escaping); wrap.
    return _PLACEHOLDER.sub(lambda m: f"<x>{m.group(0)}</x>", text)


def _unprotect(text: str) -> str:
    """Reverse _protect on DeepL's output: drop the <x> wrapper, then unescape
    the XML entities back to literal characters."""
    text = text.replace("<x>", "").replace("</x>", "")
    return (text.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&"))


def _translate_batch(key: str, texts: list[str], target: str) -> list[str]:
    """POST a batch of strings to DeepL; return the translations in order."""
    data = [("target_lang", target),
            ("source_lang", "EN"), ("tag_handling", "xml"),
            ("ignore_tags", "x"), ("preserve_formatting", "1")]
    data += [("text", _protect(t)) for t in texts]
    body = urllib.parse.urlencode(data).encode("utf-8")
    # Header-based auth (form-body auth_key was deprecated Nov 2025).
    req = urllib.request.Request(
        _endpoint(key), data=body, method="POST",
        headers={"Authorization": f"DeepL-Auth-Key {key}"})

    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return [_unprotect(t["text"]) for t in payload["translations"]]
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:   # rate-limited / transient
                wait = 2 ** attempt
                print(f"   HTTP {e.code}; retry in {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"DeepL error {e.code}: {detail}")
    raise SystemExit("DeepL: giving up after repeated errors.")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    # Normalise to Qt convention: lowercase language, uppercase region
    # (e.g. "pt-br"/"PT_BR" -> "pt_BR"). Bare language codes stay lowercase.
    raw = args[0].replace("-", "_")
    if "_" in raw:
        lang, _, region = raw.partition("_")
        short = f"{lang.lower()}_{region.upper()}"
    else:
        short = raw.lower()
    target = DEEPL_TARGET.get(short)
    if target is None:
        raise SystemExit(f"Unknown/unsupported language '{short}'. "
                         f"Known: {', '.join(sorted(DEEPL_TARGET))}")

    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not key and "--dry-run" not in flags:
        raise SystemExit("Set DEEPL_API_KEY (see the script header).")

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

    # Source strings come from the English base. If translating only the
    # unfinished ones, read the existing target .ts to see what's already done.
    en_root = ET.parse(EN_TS).getroot()
    sources = sorted({(m.find("source").text or "")
                      for m in en_root.iter("message")} - {""})

    # Existing work is keyed by (context, source) — NOT by source alone. The
    # same English word lives in many contexts ("Save" is in 15) and a human may
    # translate it differently in each; keying on source alone lets the last one
    # read overwrite all the others, silently destroying their work on every
    # refresh.
    existing = read_existing(out_ts) if only_unfinished else {}

    # The API still works on unique TEXTS (translating "Save" 15 times is pure
    # waste), but a text counts as pending if ANY context still needs it.
    todo = pending_texts(en_root, existing)
    if limit is not None:
        todo = todo[:limit]

    print(f"{short}: {len(sources)} source strings, "
          f"{len(existing)} already done, {len(todo)} to translate"
          + (" (dry-run)" if "--dry-run" in flags else ""))

    if "--dry-run" in flags:
        for s in todo[:15]:
            print("  →", repr(_protect(s)))
        return 0

    # Translate in batches (DeepL accepts up to 50 texts / request).
    fresh: dict[str, str] = {}
    BATCH = 40
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        got = _translate_batch(key, chunk, target)
        for src, tr in zip(chunk, got):
            fresh[src] = tr
        print(f"  …{min(i + BATCH, len(todo))}/{len(todo)}")

    # Build the target .ts by cloning the English base and swapping translations
    # in per (context, source) — existing human work wins over a fresh guess.
    tree = ET.parse(EN_TS)
    root = tree.getroot()
    root.set("language", short)
    kept, new = apply_translations(root, existing, fresh)

    write_ts(root, out_ts)
    print(f"wrote {out_ts} ({kept} kept, {new} translated)")
    print(f"now compile it:  pyside6-lrelease {out_ts}")
    print("  (or, from Localisation/:  ./tools/compile.sh "
          f"amethyst_{short}.ts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
