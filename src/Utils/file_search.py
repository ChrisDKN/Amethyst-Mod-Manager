"""Shared search-query parsing for the file-tree search boxes (Mod Files, Text
Files, Data tabs).

These boxes filter a flat list of file paths, so the only useful `!token` is a
FILETYPE filter - `!.dds` (or the bare `!dds`, normalised to a leading dot).
Everything else in the query is a plain-text needle matched against the path.

`parse_file_query` splits a raw query into `(needle, exts)`:
  - `needle`   lowercased plain-text substring (may be "")
  - `exts`     frozenset of lowercased extensions WITH a leading dot (may be empty)

Multiple `!` tokens accumulate (OR - a file matches if its extension is any of
them), and the needle ANDs with the extension set - mirroring the modlist token
search convention. Callers keep a file when:
    (not exts   or  Path(path).suffix.lower() in exts)
    and (not needle  or  needle in <path/其他 haystack>)
"""

from __future__ import annotations


def parse_file_query(query: str) -> tuple[str, frozenset[str]]:
    """Split *query* into (text_needle, filetype_exts). See module docstring."""
    raw = query or ""
    words: list[str] = []
    exts: set[str] = set()
    for term in raw.split():
        if term.startswith("!") and len(term) > 1:
            tok = term[1:].lower()
            if not tok.startswith("."):
                tok = "." + tok
            exts.add(tok)
        else:
            words.append(term)
    needle = " ".join(words).strip().lower()
    return needle, frozenset(exts)
