import argparse
import html
import json
import re
import sys
from pathlib import Path

from Utils.games.discovery import discover_games, get_load_failures

GAMES_TABLE = re.compile(r"(?P<before>^## Supported games\n(?:(?!^## ).)*?)<table>(?P<rows>.*?)</table>",
                         re.MULTILINE | re.DOTALL)
GAME_AND_NOTE_CELLS = re.compile(r"<td>(?P<game>[^<]*)</td><td>(?P<note>[^<]*)</td>")
TABLE_HEADER = "<tr>\n  <th>Game</th><th>Notes</th>\n  <th>Game</th><th>Notes</th>\n</tr>"


def collect_game_names(custom_handlers_dir: Path) -> list[str]:
    built_in_names = set(discover_games())
    if failures := get_load_failures():
        sys.exit(f"handlers failed to load: {failures}")
    handler_files = sorted(custom_handlers_dir.glob("*.json"))
    if not handler_files:
        sys.exit(f"no custom handlers in {custom_handlers_dir}")
    custom_names = {json.loads(handler.read_text(encoding="utf-8"))["name"] for handler in handler_files}
    return sorted(built_in_names | custom_names, key=str.casefold)


def read_notes(table_rows: str) -> dict[str, str]:
    return {html.unescape(cells["game"]): html.unescape(cells["note"])
            for cells in GAME_AND_NOTE_CELLS.finditer(table_rows)}


def warn_about_dropped_notes(notes: dict[str, str], game_names: list[str]) -> None:
    for game in sorted(set(notes) - set(game_names)):
        if notes[game]:
            print(f'::warning::note for "{game}" dropped, no handler has that name: {notes[game]}')


def render_cells(game: str, notes: dict[str, str]) -> str:
    return (f"<td>{html.escape(game, quote=False)}</td>"
            f"<td>{html.escape(notes.get(game, ''), quote=False)}</td>")


def render_table(game_names: list[str], notes: dict[str, str]) -> str:
    left_column_length = (len(game_names) + 1) // 2
    left_column = game_names[:left_column_length]
    right_column = game_names[left_column_length:] + [""]
    rows = "\n".join(f"<tr>{render_cells(left, notes)}{render_cells(right, notes)}</tr>"
                     for left, right in zip(left_column, right_column))
    return f"<table>\n{TABLE_HEADER}\n{rows}\n</table>"


def main() -> None:
    parser = argparse.ArgumentParser(description='Rewrite the "Supported games" table in the wiki Home page.')
    parser.add_argument("custom_handlers_dir", type=Path, help='"Custom Handlers" folder of the Resources branch')
    parser.add_argument("home_page", type=Path, help="Home.md of the wiki repository")
    arguments = parser.parse_args()

    game_names = collect_game_names(arguments.custom_handlers_dir)
    page = arguments.home_page.read_text(encoding="utf-8")
    games_table = GAMES_TABLE.search(page)
    if not games_table:
        sys.exit(f'no <table> under "## Supported games" in {arguments.home_page}')

    notes = read_notes(games_table["rows"])
    warn_about_dropped_notes(notes, game_names)
    new_table = games_table["before"] + render_table(game_names, notes)
    new_page = page[:games_table.start()] + new_table + page[games_table.end():]
    arguments.home_page.write_text(new_page, encoding="utf-8")
    print(f"{len(game_names)} games")


if __name__ == "__main__":
    main()
