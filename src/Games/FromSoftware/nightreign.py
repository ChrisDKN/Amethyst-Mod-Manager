"""
nightreign.py
Game handler for ELDEN RING NIGHTREIGN, modded through the me3 loader.

Nightreign runs the same engine as ELDEN RING and ships the same on-disk shape:
the retail install nests everything in ``Game/`` beside ``start_protected_game
.exe``, assets live in encrypted DVDBND archives (data0-3.bhd/.bdt, dlc01, sd/)
and a loose ``regulation.bin`` sits next to the executable.  me3 supports it
natively as game id ``nightreign``.  Every mechanism EldenRing implements -
.me3 generation, loose-file routing, the Elden Mod Loader proxy deploy and its
runtime-capture/restore pair - therefore applies unchanged, so this handler is a
subclass that only restates identity and the two places Nightreign differs.

The two differences:

  - Elden Mod Loader is published on ELDEN RING's Nexus domain, not Nightreign's,
    even though the very same build is what Nightreign users install.  The
    Nightreign domain stays primary (so browsing and search show Nightreign
    mods) and eldenring is accepted as a secondary domain, which is what lets an
    nxm:// link from the EML page hand off here and keeps its update checks
    working.

  - regulation.bin merging is not available.  The merge path decodes PARAMs
    against ELDEN RING paramdefs and refuses any regulation not detected as
    "ER", so the wizard would always fail; it is removed rather than offered.
"""

from __future__ import annotations

from Games.base_game import WizardTool
from Games.FromSoftware.elden_ring import EldenRing


class EldenRingNightreign(EldenRing):

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Elden Ring Nightreign"

    @property
    def game_id(self) -> str:
        return "nightreign"

    @property
    def exe_name(self) -> str:
        # Same nested layout as ELDEN RING: Steam installs into
        # "ELDEN RING NIGHTREIGN/Game/", so the subpath is part of the name.
        return "Game/nightreign.exe"

    @property
    def exe_name_alts(self) -> list[str]:
        # For a user who pointed straight at Game/
        return ["nightreign.exe"]

    @property
    def steam_id(self) -> str:
        return "2622380"

    @property
    def nexus_game_domain(self) -> str:
        return "eldenringnightreign"

    @property
    def additional_nexus_domains(self) -> list[str]:
        # Allows a user to install Elden Mod loader
        return ["eldenring"]

    @property
    def me3_cli_id(self) -> str:
        """The game id me3's CLI expects for -g."""
        return "nightreign"

    # -----------------------------------------------------------------------
    # Wizards
    # -----------------------------------------------------------------------

    @property
    def wizard_tools(self) -> list[WizardTool]:
        # Inherit ELDEN RING's list minus the regulation merger: merging decodes
        # PARAMs with ELDEN RING paramdefs and rejects a non-"ER" regulation, so
        # offering it here would only ever produce an error.
        return [tool for tool in super().wizard_tools
                if tool.id != "merge_regulation"]
