#!/usr/bin/env python3
"""Download the language .ts files from the Resources branch's Localisation/
folder, so you can refresh them locally without switching branches.

Workflow this saves:
    (before)  git checkout Resources → copy .ts to temp → git checkout Testing
              → move temp into src/translations → refresh → move back → push
    (now)     pull_from_resources.py → refresh → push the updated .ts to Resources

Fetches every amethyst_<code>.ts from
  github.com/ChrisDKN/Amethyst-Mod-Manager (branch Resources, folder Localisation)
into the target dir (default: src/translations). Skips amethyst_en.ts — the
English base is generated locally from the code, not pulled.

Usage:
    python3 tools/i18n/pull_from_resources.py [dest-dir] [--include-en]

    dest-dir      where to write the .ts (default: src/translations)
    --include-en  also pull amethyst_en.ts (normally skipped)

Only the standard library. No auth needed (public repo); GitHub allows a modest
number of unauthenticated API calls per hour, plenty for this.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_API = ("https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/"
        "contents/Localisation?ref=Resources")
_UA = {"User-Agent": "amethyst-i18n-tools", "Accept": "application/vnd.github+json"}


def _get(url: str, accept: str | None = None) -> bytes:
    headers = dict(_UA)
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    dest = Path(args[0]) if args else (REPO / "src" / "translations")
    include_en = "--include-en" in flags
    dest.mkdir(parents=True, exist_ok=True)

    print(f"listing Resources:Localisation/ …")
    try:
        listing = json.loads(_get(_API))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("  GitHub rate-limited this IP (unauthenticated). "
                  "Wait a bit and retry.", file=sys.stderr)
        else:
            print(f"  GitHub API error {e.code}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"  could not reach GitHub: {e}", file=sys.stderr)
        return 1

    entries = [
        e for e in listing
        if isinstance(e, dict) and e.get("type") == "file"
        and e.get("name", "").startswith("amethyst_")
        and e.get("name", "").endswith(".ts")
        and (include_en or e.get("name") != "amethyst_en.ts")
    ]
    if not entries:
        print("  no .ts files found on Resources:Localisation/", file=sys.stderr)
        return 1

    print(f"downloading {len(entries)} file(s) → {dest}")
    ok = 0
    for e in sorted(entries, key=lambda x: x["name"]):
        name = e["name"]
        url = e.get("download_url")
        if not url:
            print(f"  {name}: no download_url (skipped)")
            continue
        try:
            raw = _get(url, accept="*/*")
            # Normalise quotes/apostrophes to literal on the way in, so a
            # pulled file is already in the canonical form the tooling uses
            # (avoids entity churn if Resources holds an entity-encoded copy).
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "normalize_ts", Path(__file__).parent / "normalize_ts.py")
                nm = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(nm)
                text = nm.normalize(raw.decode("utf-8"))
                (dest / name).write_text(text, encoding="utf-8")
            except Exception:
                (dest / name).write_bytes(raw)
            # quick sanity: count <source> lines
            n = raw.count(b"<source>")
            ok += 1
            print(f"  {name}: {n} strings")
        except Exception as ex:
            print(f"  {name}: FAILED ({ex})")
    print(f"done — {ok}/{len(entries)} downloaded into {dest}")
    print("Next: refresh them (GUI or refresh_translations.sh), then push the "
          "updated .ts + .qm to the Resources branch.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
