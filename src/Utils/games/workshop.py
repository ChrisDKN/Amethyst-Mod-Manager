"""Verified Workshop games; see docs/steam-workshop-support.md."""

WORKSHOP_GAMES = {
    "933480": "Enderal: Forgotten Stories",
    "1297900": "Gothic 1 Remake",
    "1465460": "Infection Free Zone",
    "1084160": "Jagged Alliance 3",
    "1771300": "Kingdom Come: Deliverance II",
    "784080": "MechWarrior 5: Mercenaries",
    "261550": "Mount & Blade II: Bannerlord",
    "1623730": "Palworld",
    "703080": "Planet Zoo",
    "294100": "RimWorld",
    "1643320": "S.T.A.L.K.E.R. 2: Heart of Chornobyl",
    "72850": "The Elder Scrolls V: Skyrim",
    "2868840": "Slay the Spire 2",
    "292030": "The Witcher 3: Wild Hunt",
    "392160": "X4: Foundations",
}

_WORKSHOP_APP_ALIASES = {"499450": "292030"}

WORKSHOP_INSTALL_EXCLUSIONS = {
    "1465460": "Workshop maps need routing outside BepInEx/plugins.",
    "703080": "Workshop blueprints need routing outside win64/ovldata.",
}


def workshop_app_id(game) -> str:
    """Use the store App ID; launch IDs can refer to non-Steam shortcuts."""
    app_id = str(getattr(game, "steam_id", "") or "").strip()
    return _WORKSHOP_APP_ALIASES.get(app_id, app_id)


def supports_workshop_install(game) -> bool:
    app_id = workshop_app_id(game)
    return app_id in WORKSHOP_GAMES and app_id not in WORKSHOP_INSTALL_EXCLUSIONS
