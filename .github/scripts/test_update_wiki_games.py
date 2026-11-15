import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO

from update_wiki_games import rewrite_games_table

HOME_PAGE = textwrap.dedent("""\
    # Amethyst

    ## Supported games

    Intro written by hand.

    <table>
    <tr><th>Game</th><th>Notes</th><th>Game</th><th>Notes</th></tr>
    <tr><td>Daggerfall Unity</td><td>Native Linux build</td><td>Skyrim</td><td>Normal, SE and VR</td></tr>
    <tr><td>Removed Game</td><td>old note</td><td></td><td></td></tr>
    </table>

    Outro written by hand.

    ## Building from source

    Steps.
    """)
GAME_NAMES = ["Daggerfall Unity", "Mount & Blade II: Bannerlord", "Prey (2017)", "Skyrim"]


def rewrite_quietly(page: str) -> str:
    with redirect_stdout(StringIO()):
        return rewrite_games_table(page, GAME_NAMES)


class RewriteGamesTableTest(unittest.TestCase):
    def test_lists_every_game_and_keeps_notes_and_hand_written_text(self):
        expected = textwrap.dedent("""\
            # Amethyst

            ## Supported games

            Intro written by hand.

            <table>
            <tr>
              <th>Game</th><th>Notes</th>
              <th>Game</th><th>Notes</th>
            </tr>
            <tr><td>Daggerfall Unity</td><td>Native Linux build</td><td>Prey (2017)</td><td></td></tr>
            <tr><td>Mount &amp; Blade II: Bannerlord</td><td></td><td>Skyrim</td><td>Normal, SE and VR</td></tr>
            </table>

            Outro written by hand.

            ## Building from source

            Steps.
            """)

        self.assertEqual(expected, rewrite_quietly(HOME_PAGE))

    def test_second_run_changes_nothing(self):
        once = rewrite_quietly(HOME_PAGE)

        self.assertEqual(once, rewrite_quietly(once))

    def test_warns_about_a_note_whose_game_is_gone(self):
        log = StringIO()
        with redirect_stdout(log):
            rewrite_games_table(HOME_PAGE, GAME_NAMES)

        self.assertEqual('::warning::note for "Removed Game" dropped, no handler has that name: old note\n',
                         log.getvalue())

    def test_refuses_a_page_without_the_games_table(self):
        with self.assertRaises(ValueError):
            rewrite_games_table("## Supported games\n\nNo table.\n\n## Next\n", GAME_NAMES)


if __name__ == "__main__":
    unittest.main()
