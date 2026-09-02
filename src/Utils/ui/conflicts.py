"""File-level conflict computation for the toolkit-neutral conflicts view.

Given a mod, produces three lists of the files it provides plus a tint set:
  - files_win        : (path, "modA, modB")  - this mod overrides those mods here
  - files_lose       : (path, winning_mod)   - this mod is overridden here
  - files_no_conflict: [path]                - no other enabled mod provides it
  - bsa_win_paths    : {path}                - win rows beating archive contents
                        only; the UI tints these cyan like the archive rows

Both loose files and BSA/BA2 archive contents are projected from one immutable
Filegraph snapshot (BSA rows are prefixed ``archive.bsa : inner/path``).
"""

from __future__ import annotations

import re

# Rows whose path looks like ``archive.bsa : inner/path`` come from an archive.
BSA_ROW_RE = re.compile(r"^[^/\\:]+\.(?:bsa|ba2)\s+:\s", re.IGNORECASE)


def _compute_snapshot_conflicts(
    mod_name: str, snapshot, data_prefix: str = "",
):
    """File-level projection from one immutable graph generation."""
    files_win: list[tuple[str, str]] = []
    files_lose: list[tuple[str, str]] = []
    files_no_conflict: list[str] = []
    archive_only: set[str] = set()
    identity_wins: set[str] = set()
    identity_losses: set[str] = set()
    for edge in snapshot.conflict_state().edges:
        if edge.kind != "identity":
            continue
        if edge.winner == mod_name:
            identity_wins.add(edge.loser)
        elif edge.loser == mod_name:
            identity_losses.add(edge.winner)

    def _owners(providers):
        """Provider owners in load order with consecutive duplicates folded."""
        result = []
        for provider in providers:
            if not result or result[-1] != provider.mod_name:
                result.append(provider.mod_name)
        return result

    data_prefix = str(data_prefix or "").replace("\\", "/").strip("/")
    data_prefix_lower = data_prefix.lower()

    def _display(file) -> str:
        legacy = (file.legacy_rel or file.source).replace("\\", "/")
        destination = file.destination.replace("\\", "/")
        if file.namespace == "root":
            prefix = data_prefix_lower + "/" if data_prefix_lower else ""
            if prefix and legacy.lower().startswith(prefix):
                return legacy[len(prefix):]
            return legacy
        expected = (
            f"{data_prefix}/{legacy}" if data_prefix and legacy else
            data_prefix or legacy
        )
        # Ordinary Data-domain candidates retain the old filemap-relative
        # display. A real routing transform (UE/custom/Witcher) remains in
        # final game-root coordinates, matching the old conflict key view.
        return legacy if destination.lower() == expected.lower() else destination

    files = (*snapshot.mod_files(mod_name), *snapshot.archive_files(mod_name))
    for file in files:
        display = _display(file)
        if file.provider_kind == "archive_member":
            archive = file.source.replace("\\", "/").rsplit("/", 1)[-1]
            display = f"{archive} : {display}"
        is_archive = file.provider_kind == "archive_member"
        if not is_archive and not file.deployable:
            # The retired index still listed disabled Mod Files rows, while the
            # resolved map had no winner for them. Preserve that diagnostic in
            # Show Conflicts without making the raw file a graph provider.
            files_lose.append((display, "(no winner - disabled?)"))
            continue
        by_namespace = {
            namespace: snapshot.providers(
                file.target, file.destination_key, namespace=namespace)
            for namespace in ("normal", "root", "archive")
        }
        owners = {
            namespace: _owners(providers)
            for namespace, providers in by_namespace.items()
        }
        active_candidate = any(
            provider.candidate_id == file.candidate_id
            for providers in by_namespace.values()
            for provider in providers
        )
        # Keep the provider namespaces on each adjacency.  A mod can provide
        # the same destination from both a loose file and an archive; folding
        # those into one owner-only edge makes its archive row inherit the
        # loose row's result (and vice versa).
        direct: list[tuple[str, str, str, str, str]] = []
        for namespace, kind in (("normal", "loose"), ("root", "loose"),
                                ("archive", "archive")):
            direct.extend(
                (loser, winner, kind, namespace, namespace)
                for loser, winner in zip(
                    owners[namespace], owners[namespace][1:])
            )
        if owners["normal"] and owners["root"]:
            direct.append((
                owners["normal"][-1], owners["root"][-1], "loose",
                "normal", "root",
            ))
        if (snapshot.loose_beats_archive
                and owners["archive"] and owners["normal"]):
            direct.append((
                owners["archive"][-1], owners["normal"][-1],
                "loose_archive", "archive", "normal"))

        beaten: dict[str, str] = {
            loser: kind
            for loser, winner, kind, _loser_namespace, winner_namespace in direct
            if (winner == mod_name and winner_namespace == file.namespace
                and loser != mod_name)
        }
        destination_involved = any(
            (loser == mod_name and loser_namespace == file.namespace)
            or (winner == mod_name and winner_namespace == file.namespace)
            for (loser, winner, _kind, loser_namespace,
                 winner_namespace) in direct)
        if not destination_involved and file.conflict_status:
            for loser in identity_wins:
                beaten.setdefault(loser, "identity")
        if beaten:
            partners = sorted(beaten)
            files_win.append((display, ", ".join(partners)))
            if all(beaten[partner] == "loose_archive" for partner in partners):
                archive_only.add(display)

        winner = next((
            winner
            for loser, winner, _kind, loser_namespace, _winner_namespace in direct
            if loser == mod_name and loser_namespace == file.namespace
        ), None)
        if winner is not None and winner != mod_name:
            files_lose.append((display, winner))
        elif (not destination_involved and file.conflict_status < 0
              and identity_losses):
            files_lose.append((display, sorted(identity_losses)[-1]))
        elif not is_archive and not active_candidate:
            files_lose.append((display, "(no winner - disabled?)"))
        elif is_archive and not active_candidate and winner is None:
            # The retired archive view omitted an inactive archive member when
            # no other archive/loose provider won that path.
            continue
        elif not beaten:
            files_no_conflict.append(display)
    files_win.sort(key=lambda row: row[0].lower())
    files_lose.sort(key=lambda row: row[0].lower())
    files_no_conflict.sort(key=str.lower)
    return files_win, files_lose, files_no_conflict, archive_only


def compute_mod_conflicts(mod_name: str, *, snapshot, data_prefix: str = ""):
    """Return file-level conflicts from a required pinned snapshot."""
    if snapshot is None:
        raise RuntimeError("Show Conflicts requires a published Filegraph snapshot")
    return _compute_snapshot_conflicts(mod_name, snapshot, data_prefix)
