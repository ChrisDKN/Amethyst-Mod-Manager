from __future__ import annotations

from dataclasses import dataclass

from .post_install_rules import Match


@dataclass(frozen=True)
class BrokenList:
    id: str
    match: Match
    reason: str
    recovery: str
    confirmed_on: str
    links: tuple[tuple[str, str], ...] = ()
    archive_hash: str = ""
    source_url: str = ""

    def matches(self, package):
        if not self.match.matches(package):
            return False
        if not self.archive_hash:
            return True
        archive = package.archives.get(self.archive_hash)
        return archive is not None and (
            not self.source_url or archive.state.get("Url") == self.source_url)


BROKEN_LISTS = (
    BrokenList(
        id="deckborn-maxsu-poise",
        match=Match(domain="skyrimspecialedition", names=("Deckborn",)),
        reason="The MaxsuPoise v0.34 download was replaced with a different build under the same filename. "
               "This package still requires the original archive, so the current download is rejected.",
        recovery="Use the original MaxsuPoise.v0.34.7z (5,005,800 bytes), or a corrected Deckborn package. "
                 "Users reported an archived copy in Wabbajack's authored files with an ID beginning e5601f65-. "
                 "Its current availability has not been verified. If you obtain it, use Select File when prompted.",
        confirmed_on="2026-09-12",
        links=(
            ("Reported workaround", "https://www.nexusmods.com/skyrimspecialedition/mods/129937?tab=posts"),
            ("Find archived file", "https://build.wabbajack.org/authored_files"),
        ),
        archive_hash="fD8xf0jwND0=",
        source_url="https://github.com/SkyHorizon3/MaxsuPoise/releases/download/v0.34/MaxsuPoise.v0.34.7z",
    ),
)


def matching_broken_lists(package):
    return tuple(issue for issue in BROKEN_LISTS if issue.matches(package))
