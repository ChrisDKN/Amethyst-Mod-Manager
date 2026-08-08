"""
heroic_appnames.py
Central registry of Heroic (Epic/GOG) app names, keyed by internal game_id.

BaseGame.heroic_app_names resolves from here - handlers no longer declare
app names themselves.  Because the key is the game_id, entries work for any
handler class (no inheritance leaks to subclasses) and equally for custom
games, whose definitions carry a game_id of their own.

When a game_id has entries here, Heroic detection matches by app name and
the exe-name scan is skipped entirely: generic launcher names collide
across games (e.g. FalloutLauncher.exe ships with both Fallout 3 GOTY and
classic Fallout on GOG), so an exe match can resolve to the wrong title.

Identifiers per store:
  - Epic (Legendary): the appName string - a codename like 'Jaguar' for
    older catalog entries, a 32-char hex id for newer ones.
  - GOG: the numeric product id as a string.  List the *installable game*
    id; store "pack" products aren't what Heroic installs.

Looking up ids without installing the game - use the helper, which searches
Heroic's local library plus both store catalogues and prints a ready-to-paste
entry for this file:

    tools/heroic_appname_lookup.py "Fallout 3" --game-id Fallout3GOTY

It automates: GOG catalog.gog.com/v1/catalog?query=like:<title> → if the hit
is a pack, api.gog.com/v2/games/<id> _links.includesGames gives the
installable game id; Epic api.egdata.app/autocomplete?query=<title> → the
BASE_GAME offer's namespace → api.egdata.app/sandboxes/<ns>/assets → the
artifactId is the Legendary appName (ignoring Staging/Dummy assets).
"""

from __future__ import annotations

# game_id → Heroic app names (any mix of Epic appNames and GOG product ids).
HEROIC_APP_NAMES: dict[str, list[str]] = {
    "Fallout3": [
        "1454315831", "1248282609", "adeae8bbfc94427db57c7dfecce3f1d4"],
    "Fallout3GOTY": [
        "1454315831", "1248282609", "adeae8bbfc94427db57c7dfecce3f1d4"],
    "Subnautica": [
        "Jaguar"],
    "Subnautica_Below_Zero": [
        "Foxglove"],
    "Fallout_London": [
        "1897848199"],
}


def heroic_app_names_for(game_id: str) -> list[str]:
    """Registered Heroic app names for game_id (copy; empty if none)."""
    return list(HEROIC_APP_NAMES.get(game_id, ()))
