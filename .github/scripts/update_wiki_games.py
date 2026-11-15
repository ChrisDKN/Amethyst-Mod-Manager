"""Rewrite the "Supported games" table in the wiki's Home.md.

The list is every built-in handler the app registers plus every custom handler
on the Resources branch, so it matches what the Add Game menu offers. The Notes
column is kept from the wiki: edit a note there and it survives the next run.

    PYTHONPATH=src python .github/scripts/update_wiki_games.py "<Custom Handlers dir>" <Home.md>
"""

import html
import json
import re
import sys
from pathlib import Path

from Utils.games.discovery import discover_games, get_load_failures

handlers_dir, home = map(Path, sys.argv[1:3])

names = set(discover_games())
if failures := get_load_failures():
    sys.exit(f"handlers failed to load: {failures}")
handler_files = sorted(handlers_dir.glob("*.json"))
if not handler_files:
    sys.exit(f"no custom handlers in {handlers_dir}")
names |= {json.loads(p.read_text(encoding="utf-8"))["name"] for p in handler_files}
names = sorted(names, key=str.casefold)

text = home.read_text(encoding="utf-8")
# Only the first <table> under the heading is generated; text around it stays.
section = re.compile(r"(^## Supported games\n(?:(?!^## ).)*?)<table>(.*?)</table>", re.M | re.S)
match = section.search(text)
if not match:
    sys.exit(f'no <table> under "## Supported games" in {home}')
notes = {html.unescape(game): html.unescape(note)
         for game, note in re.findall(r"<td>([^<]*)</td><td>([^<]*)</td>", match[2])}
for game in sorted(set(notes) - set(names)):
    if notes[game]:
        print(f'::warning::note for "{game}" dropped, no handler has that name: {notes[game]}')


def cells(game: str) -> str:
    return f"<td>{html.escape(game, quote=False)}</td><td>{html.escape(notes.get(game, ''), quote=False)}</td>"


# Two columns, read top to bottom: the first half on the left.
half = (len(names) + 1) // 2
rows = "\n".join(f"<tr>{cells(left)}{cells(right)}</tr>"
                 for left, right in zip(names[:half], names[half:] + [""]))
table = ("<table>\n<tr>\n  <th>Game</th><th>Notes</th>\n  <th>Game</th><th>Notes</th>\n</tr>\n"
         f"{rows}\n</table>")
home.write_text(section.sub(lambda m: m[1] + table, text, count=1), encoding="utf-8")
print(f"{len(names)} games")
