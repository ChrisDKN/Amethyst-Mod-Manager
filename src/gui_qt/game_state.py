"""Game/profile state controller for the Qt UI.

Thin wrapper over the real (toolkit-neutral) helpers in gui.game_helpers and
Games.base_game so the Qt app drives the same load flow as the Tk app:
discover games, list profiles, switch the active game/profile, and resolve the
active modlist.txt + staging dir.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.game_helpers import (
    _load_games, _profiles_for_game, _load_last_game, _save_last_game, _GAMES,
)
from Utils.ui_config import load_last_session, save_last_session


def _loose_code_without_archive_wins(summary, archive_wins: int) -> int:
    """Return the legacy loose status with loose-over-archive wins hidden.

    Filegraph correctly counts a loose provider beating an archive member as a
    loose win. Bethesda's ``Hide BSA conflicts`` presentation rule deliberately
    hides that contribution as well as the archive icon and partner map.
    """
    wins = max(0, int(summary.loose_wins) - max(0, int(archive_wins)))
    losses = max(0, int(summary.loose_losses))
    if wins:
        return 2 if losses else 1
    if not losses:
        return 0
    return 3 if int(summary.loose_surviving) == 0 else -1


@dataclass
class ConflictData:
    """Everything the modlist/plugins panels need to draw conflicts + cross-panel
    highlights. All maps key on mod name. *_codes are 1 win / -1 lose / 2 mixed /
    3 fully-overridden.
    *_overrides[mod] = mods this mod beats; *_overridden_by[mod] = mods that beat
    it. plugin_owner maps a plugin filename (lower) → the mod that deploys it."""
    loose_codes: dict[str, int] = field(default_factory=dict)
    # loose_codes before the _merge_loose_beats_bsa upgrade. The plugin-toggle
    # recompute re-merges from this pristine copy so repeated toggles can't
    # compound NONE→WINS→PARTIAL.
    loose_codes_base: dict[str, int] | None = None
    bsa_codes: dict[str, int] = field(default_factory=dict)
    # BG3 mods whose .pak duplicates another mod's module UUID - its own icon,
    # kept out of loose_codes so an identity clash doesn't light up two.
    uuid_codes: dict[str, int] = field(default_factory=dict)
    overrides: dict[str, set] = field(default_factory=dict)
    overridden_by: dict[str, set] = field(default_factory=dict)
    bsa_overrides: dict[str, set] = field(default_factory=dict)
    bsa_overridden_by: dict[str, set] = field(default_factory=dict)
    plugin_owner: dict[str, str] = field(default_factory=dict)
    # Filemap/index-derived flag inputs (mod names): mods with pre-RTX
    # (natives/x64) files (info flag) and mods owning root-rule-routed files
    # (root flag). Computed from modindex.bin in build_conflicts.
    prertx_mods: set = field(default_factory=set)
    root_rule_mods: set = field(default_factory=set)
    # Framework banner rows (list[FrameworkStatus]) precomputed on the conflict
    # worker - detect_frameworks re-reads filemap.txt (+ the mod index), which
    # is too slow for the UI thread on a 100k-file modlist.
    framework_statuses: list = field(default_factory=list)
    # Toggle-capability sets (index-derived, same cached scan as the flags):
    # mods shipping plugin files / BSA-BA2 archives / files whose basename
    # matches a framework exe. Drive the disable fast path - a mod in none of
    # these can be disabled without recomputing plugin_owner, BSA conflicts or
    # framework statuses (see app._toggle_skips_conflict_scan).
    plugin_mods: set = field(default_factory=set)
    bsa_mods: set = field(default_factory=set)
    archive_plugin_stems: dict[str, frozenset[str]] = field(default_factory=dict)
    edge_refcounts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    loose_archive_winner_counts: dict[str, int] = field(default_factory=dict)
    framework_file_mods: set = field(default_factory=set)
    # Mods whose loose file overrides some archive's copy of it. In no other
    # capability set (they ship no archive; the loose maps can't see archives)
    # yet toggling one flips two icons - hence its own entry in the disable
    # fast-path guard (see app._toggle_skips_conflict_scan).
    loose_beats_bsa_mods: set = field(default_factory=set)
    # Generation-pinned filegraph state used by downstream views. The UI
    # applies summary/partner data from this same generation and never reparses
    # a compatibility map to rediscover it.
    snapshot: object | None = None
    resolution_delta: object | None = None
    # True only when this ConflictData instance already represented the
    # delta's base generation and the maps above were updated in place.  A
    # restored native profile may return a small/no-op delta while the Python
    # presentation cache is empty (notably on application startup or profile
    # switch).  Consumers must perform a full initial projection in that case.
    projection_is_incremental: bool = False
    profile_id: str = ""


class GameState:
    def __init__(self):
        self.game_names: list[str] = []
        self.game_name: str | None = None
        self.profile: str | None = None
        self._filegraph_conflict_cache: dict[tuple[str, bool], ConflictData] = {}
        # Keep the active native library warm.  FileGraphService's registry is
        # intentionally weak so unused libraries can be reclaimed, but without
        # an owner here every build_conflicts() call became a cold SQLite/graph
        # restoration once its local variables went out of scope.  Deployment
        # and the post-deploy refresh then rebuilt a million-provider graph
        # repeatedly during one UI session.
        self._filegraph_library = None
        self._filegraph_profile = None

    # -- discovery / load ---------------------------------------------------
    def load(self, timing=None) -> None:
        """Discover games and select the last-used game + profile (from
        amethyst.ini [session], falling back to last_game.json / first game and
        the first profile). Populates game_names / game_name / profile."""
        self.game_names = _load_games(timing=timing)
        phase_started = time.perf_counter()
        sess_game, sess_profile = load_last_session()
        last = _load_last_game()
        if sess_game and sess_game in self.game_names:
            self.game_name = sess_game
        elif last and last in self.game_names:
            self.game_name = last
        elif self.game_names and self.game_names[0] != "No games configured":
            self.game_name = self.game_names[0]
        else:
            self.game_name = None
        if timing is not None:
            timing.record("Restore the last active game",
                          phase_started=phase_started,
                          category="configuration")
        # Restore the profile: prefer the global session profile (the one open
        # when the app last closed), then this game's own last-active profile,
        # then the first profile. Records it as the game's last-active too.
        g = self.game
        phase_started = time.perf_counter()
        per_game = g.get_last_active_profile() if g is not None else None
        self.profile = (self._select_profile(sess_profile)
                        if sess_profile else None) or \
            self._select_profile(per_game)
        if timing is not None:
            timing.record("Find and select the active profile",
                          phase_started=phase_started,
                          category="configuration")
        phase_started = time.perf_counter()
        self._apply_active_profile()
        if timing is not None:
            timing.record("Load active game/profile paths",
                          phase_started=phase_started,
                          category="game setup")
        phase_started = time.perf_counter()
        self._materialize_if_group(timing=timing)
        if timing is not None:
            timing.record("Reconcile active profile group (aggregate)",
                          phase_started=phase_started,
                          category="aggregate")
        phase_started = time.perf_counter()
        self._save_last_active_profile()
        if timing is not None:
            timing.record("Persist active profile session",
                          phase_started=phase_started,
                          category="configuration")

    # -- current handler ----------------------------------------------------
    @property
    def game(self):
        return _GAMES.get(self.game_name) if self.game_name else None

    def profiles(self) -> list[str]:
        return _profiles_for_game(self.game_name) if self.game_name else []

    # -- switching ----------------------------------------------------------
    def set_game(self, name: str) -> None:
        if name == self.game_name or name not in self.game_names:
            return
        self.game_name = name
        _save_last_game(name)
        # Restore the profile last used ON THIS GAME (Tk parity - top_bar uses
        # game.get_last_active_profile()), falling back to the first profile.
        self._select_last_active_profile()
        self._apply_active_profile()
        self._materialize_if_group()
        self._save_last_active_profile()
        save_last_session(self.game_name, self.profile)

    def set_profile(self, profile: str) -> None:
        if profile == self.profile:
            return
        self.profile = profile
        self._apply_active_profile()
        self._materialize_if_group()
        # Remember this as the game's last active profile so switching away and
        # back returns here (Tk parity - top_bar._on_profile_change).
        self._save_last_active_profile()
        save_last_session(self.game_name, self.profile)

    def _materialize_if_group(self, timing=None) -> None:
        """Reconcile the just-activated profile when it is a Profile Group.

        Runs ONLY on real profile-identity changes (load / set_game /
        set_profile) - never from reassert_active_profile, which fires on
        every reload and must stay a cheap no-op. Ordering matters: callers
        run sync_modlist_with_mods_folder after switching, and that sync
        drops entries whose folder is missing - the link farm must be
        reconciled first."""
        g = self.game
        pdir = self.profile_dir()
        if g is None or pdir is None:
            return
        try:
            from Utils.profile_groups import materialize_if_group
            materialize_if_group(g, pdir, timing=timing)
        except Exception as exc:
            print(f"[gui_qt] profile-group reconcile failed: {exc}", flush=True)

    # -- resolved paths -----------------------------------------------------
    def modlist_path(self) -> Path | None:
        g = self.game
        if g is None or not self.profile:
            return None
        return g.get_profile_root() / "profiles" / self.profile / "modlist.txt"

    def profile_dir(self) -> Path | None:
        """Active profile dir - where per-profile state (collapsed separators,
        separator locks, etc.) is stored."""
        g = self.game
        if g is None or not self.profile:
            return None
        return g.get_profile_root() / "profiles" / self.profile

    def staging_dir(self) -> Path | None:
        g = self.game
        if g is None:
            return None
        try:
            p = g.get_effective_mod_staging_path()
            return p if p.is_dir() else None
        except Exception:
            return None

    def build_conflicts(self, log_fn=None, rescan_index: bool = False,
                        operation_hint: dict | None = None,
                        timing=None) -> "ConflictData":
        """Reconcile and project one generation of native filegraph state."""
        g = self.game
        if g is None or not self.profile:
            if timing is not None:
                timing.finish("conflict build skipped: no active profile",
                              lane="worker")
            return ConflictData()
        import time
        from Utils.perftrace import span
        log = log_fn or (lambda _m: None)
        phase_started = time.perf_counter()
        # Flat-staging heal (Tk parity): wrap manually-copied flat mods before
        # the index/filemap build so deploy targets Mods/<Name>/ correctly. A
        # fix forces a full rescan - the index still has the pre-wrap layout.
        if getattr(g, "mod_staging_requires_subdir", False):
            try:
                from Utils.mod_install import fix_flat_staging_folders
                names, exts = getattr(g, "mod_staging_wrap_signals",
                                      ({"manifest.json"}, set()))
                guard = getattr(g, "mod_staging_already_structured_markers",
                                set())
                staging = self.staging_dir()
                fixed = (fix_flat_staging_folders(staging, names, exts, guard)
                         if staging is not None else [])
                if fixed:
                    rescan_index = True
                    log(f"Auto-fixed {len(fixed)} mod(s) with flat staging "
                        f"structure: " + ", ".join(fixed))
            except Exception as exc:
                log(f"Flat-staging check failed: {exc}")
        if timing is not None:
            timing.mark("flat-staging validation complete",
                        phase_started=phase_started, lane="worker")
        profile_dir = self.profile_dir()
        if profile_dir is None:
            if timing is not None:
                timing.finish("conflict build skipped: profile path unavailable",
                              lane="worker")
            return ConflictData()
        from Utils.filegraph_service import FileGraphService
        phase_started = time.perf_counter()
        with span("filegraph.open_library"):
            library = FileGraphService.open_library(
                g, profile_dir, log_fn=log)
        if timing is not None:
            timing.mark("Filegraph library opened",
                        phase_started=phase_started, lane="worker")
        self._filegraph_library = library
        phase_started = time.perf_counter()
        with span("filegraph.refresh" if rescan_index else "filegraph.ensure_ready"):
            if rescan_index:
                library.refresh(profile_dir)
            else:
                library.ensure_ready(profile_dir)
        if timing is not None:
            timing.mark(
                "Filegraph catalog refreshed" if rescan_index
                else "Filegraph catalog readiness checked",
                phase_started=phase_started, lane="worker")
        phase_started = time.perf_counter()
        session = library.open_profile(profile_dir)
        self._filegraph_profile = session
        if timing is not None:
            timing.mark("profile session opened",
                        phase_started=phase_started, lane="worker")
        with span("filegraph.reconcile"):
            delta = session.reconcile(operation_hint=operation_hint,
                                      timing=timing)
        snapshot = session.snapshot()
        profile_id = str(profile_dir.resolve(strict=False))
        phase_started = time.perf_counter()

        # Archive visibility is presentation state, so include it in the cache
        # key. Changing the setting performs one full projection, while ordinary
        # toggles and moves apply only the native edge/summary delta.
        archive_exts = frozenset(
            getattr(g, "archive_extensions", frozenset()) or frozenset())
        show_archives = bool(archive_exts)
        if show_archives:
            try:
                from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
                is_ue = bool(archive_exts & UE_ARCHIVE_EXTENSIONS)
            except Exception:
                is_ue = False
            if not is_ue:
                try:
                    from Utils.ui_config import load_hide_bsa_conflicts
                    show_archives = not load_hide_bsa_conflicts()
                except Exception:
                    pass
        cache_key = (profile_id, show_archives)
        data = self._filegraph_conflict_cache.get(cache_key)
        can_apply_delta = bool(
            data is not None
            and not delta.full_rebuild
            and data.snapshot is not None
            and data.snapshot.generation == delta.base_generation
        )
        from Utils.filegraph_adapter import (
            FLAG_ARCHIVE, FLAG_FRAMEWORK, FLAG_PLUGIN, FLAG_PRE_RTX,
            FLAG_ROOT_RULE,
        )

        if can_apply_delta:
            data.snapshot = snapshot
            data.resolution_delta = delta
            data.projection_is_incremental = True
            for name, summary in delta.changed_summaries.items():
                data.loose_codes[name] = summary.loose_code
                data.loose_codes_base[name] = summary.loose_code
                if show_archives:
                    data.bsa_codes[name] = summary.archive_code
                if summary.identity_code:
                    data.uuid_codes[name] = summary.identity_code
                else:
                    data.uuid_codes.pop(name, None)
            for plugin, owner in delta.changed_plugin_owners.items():
                if owner is None:
                    data.plugin_owner.pop(plugin, None)
                else:
                    data.plugin_owner[plugin] = owner
            for name, flags in delta.changed_capability_flags.items():
                value = int(flags or 0)
                for members, flag in (
                    (data.prertx_mods, FLAG_PRE_RTX),
                    (data.root_rule_mods, FLAG_ROOT_RULE),
                    (data.plugin_mods, FLAG_PLUGIN),
                    (data.bsa_mods, FLAG_ARCHIVE),
                    (data.framework_file_mods, FLAG_FRAMEWORK),
                ):
                    if value & flag:
                        members.add(name)
                    else:
                        members.discard(name)

            def _set_partner(overrides, overridden_by, edge, present):
                if present:
                    overrides.setdefault(edge.winner, set()).add(edge.loser)
                    overridden_by.setdefault(edge.loser, set()).add(edge.winner)
                else:
                    winners = overrides.get(edge.winner)
                    if winners is not None:
                        winners.discard(edge.loser)
                        if not winners:
                            overrides.pop(edge.winner, None)
                    losers = overridden_by.get(edge.loser)
                    if losers is not None:
                        losers.discard(edge.winner)
                        if not losers:
                            overridden_by.pop(edge.loser, None)

            for edge in delta.changed_edges:
                edge_key = (edge.kind, edge.loser, edge.winner)
                old_refcount = data.edge_refcounts.get(edge_key, 0)
                if edge.refcount:
                    data.edge_refcounts[edge_key] = edge.refcount
                else:
                    data.edge_refcounts.pop(edge_key, None)
                if edge.kind in ("loose", "identity"):
                    pair_present = any(
                        data.edge_refcounts.get(
                            (kind, edge.loser, edge.winner), 0)
                        for kind in ("loose", "identity")
                    )
                    _set_partner(
                        data.overrides, data.overridden_by, edge,
                        pair_present)
                elif edge.kind in ("archive", "loose_archive") and show_archives:
                    pair_present = any(
                        data.edge_refcounts.get((kind, edge.loser, edge.winner), 0)
                        for kind in ("archive", "loose_archive")
                    )
                    _set_partner(
                        data.bsa_overrides, data.bsa_overridden_by, edge,
                        pair_present)
                if edge.kind == "loose_archive":
                    total = max(
                        0,
                        data.loose_archive_winner_counts.get(edge.winner, 0)
                        + edge.refcount - old_refcount,
                    )
                    if total:
                        data.loose_archive_winner_counts[edge.winner] = total
                        if show_archives:
                            data.loose_beats_bsa_mods.add(edge.winner)
                    else:
                        data.loose_archive_winner_counts.pop(edge.winner, None)
                        data.loose_beats_bsa_mods.discard(edge.winner)
            if not show_archives:
                for name, summary in delta.changed_summaries.items():
                    visible_code = _loose_code_without_archive_wins(
                        summary,
                        data.loose_archive_winner_counts.get(name, 0),
                    )
                    data.loose_codes[name] = visible_code
                    data.loose_codes_base[name] = visible_code
        else:
            with span("filegraph.conflict_state"):
                resolved = snapshot.conflict_state()
            data = ConflictData(
                snapshot=snapshot,
                resolution_delta=delta,
                projection_is_incremental=False,
                profile_id=profile_id,
            )
            data.loose_codes = {
                name: summary.loose_code
                for name, summary in resolved.summaries.items()
            }
            data.loose_codes_base = dict(data.loose_codes)
            if show_archives:
                data.bsa_codes = {
                    name: summary.archive_code
                    for name, summary in resolved.summaries.items()
                }
            data.uuid_codes = {
                name: summary.identity_code
                for name, summary in resolved.summaries.items()
                if summary.identity_code
            }
            for edge in resolved.edges:
                edge_key = (edge.kind, edge.loser, edge.winner)
                data.edge_refcounts[edge_key] = edge.refcount
                if edge.kind in ("loose", "identity"):
                    data.overrides.setdefault(edge.winner, set()).add(edge.loser)
                    data.overridden_by.setdefault(edge.loser, set()).add(edge.winner)
                elif edge.kind in ("archive", "loose_archive") and show_archives:
                    data.bsa_overrides.setdefault(edge.winner, set()).add(edge.loser)
                    data.bsa_overridden_by.setdefault(edge.loser, set()).add(edge.winner)
                if edge.kind == "loose_archive":
                    data.loose_archive_winner_counts[edge.winner] = (
                        data.loose_archive_winner_counts.get(edge.winner, 0)
                        + edge.refcount)
                    if show_archives:
                        data.loose_beats_bsa_mods.add(edge.winner)
            if not show_archives:
                for name, summary in resolved.summaries.items():
                    visible_code = _loose_code_without_archive_wins(
                        summary,
                        data.loose_archive_winner_counts.get(name, 0),
                    )
                    data.loose_codes[name] = visible_code
                    data.loose_codes_base[name] = visible_code
            data.plugin_owner = dict(resolved.plugin_owners)
            data.archive_plugin_stems = dict(resolved.archive_plugin_stems)
            for name, flags in resolved.capability_flags.items():
                if flags & FLAG_PRE_RTX:
                    data.prertx_mods.add(name)
                if flags & FLAG_ROOT_RULE:
                    data.root_rule_mods.add(name)
                if flags & FLAG_PLUGIN:
                    data.plugin_mods.add(name)
                if flags & FLAG_ARCHIVE:
                    data.bsa_mods.add(name)
                if flags & FLAG_FRAMEWORK:
                    data.framework_file_mods.add(name)

        if timing is not None:
            timing.mark(
                "incremental conflict projection applied"
                if can_apply_delta else "full conflict projection built",
                phase_started=phase_started, lane="worker")
        phase_started = time.perf_counter()
        hinted_mods = set((operation_hint or {}).get("mods", ()))
        reuse_frameworks = bool(
            can_apply_delta
            and (operation_hint or {}).get("kind") in {
                "toggle", "enable", "disable", "move", "move_block",
            }
            and hinted_mods.isdisjoint(data.framework_file_mods)
        )
        if not reuse_frameworks:
            with span("filegraph.detect_frameworks"):
                from Utils.framework_detect import detect_frameworks_snapshot
                data.framework_statuses = detect_frameworks_snapshot(
                    g, snapshot, self.modlist_path(), rf_toggle_enabled=True)
        if timing is not None:
            timing.mark(
                "framework statuses reused (changed mods have no framework files)"
                if reuse_frameworks else "framework statuses resolved",
                phase_started=phase_started, lane="worker")
        self._filegraph_conflict_cache[cache_key] = data
        return data

    # -- internals ----------------------------------------------------------
    def _select_profile(self, preferred: "str | None") -> "str | None":
        """*preferred* if it's a real profile for the current game, else the first
        profile, else None."""
        profs = self.profiles()
        if preferred and preferred in profs:
            return preferred
        return profs[0] if profs else None

    def _select_last_active_profile(self) -> None:
        """Set self.profile to this game's saved last-active profile (if it still
        exists), else the first profile. Used when switching games."""
        g = self.game
        preferred = g.get_last_active_profile() if g is not None else None
        self.profile = self._select_profile(preferred)

    def _save_last_active_profile(self) -> None:
        """Persist self.profile as the current game's last-active profile, so a
        later switch back to this game restores it (Tk parity)."""
        g = self.game
        if g is not None and self.profile:
            try:
                g.save_last_active_profile(self.profile)
            except Exception:
                pass

    def reassert_active_profile(self) -> None:
        """Force the game object's ``_active_profile_dir`` back in sync with our
        ``profile``.

        Background workers (restore, profile-remove, bundle import) temporarily
        swap the game's ``_active_profile_dir`` and restore it in a ``finally``
        block. If the user switches profiles while one runs - or a worker
        restores to the wrong value (e.g. ``None``/default) - the game object
        can be left pointing at a different profile than the dropdown shows,
        which makes path-derived actions (Open ▸ Staging/Profile folder, etc.)
        resolve to the wrong profile. Call this before reading any path that
        depends on the active profile so GameState stays the single authority.
        """
        self._apply_active_profile()

    def _apply_active_profile(self) -> None:
        g = self.game
        if g is not None and self.profile:
            g.set_active_profile_dir(
                g.get_profile_root() / "profiles" / self.profile)
            # Re-resolve paths so this profile's game/prefix/deploy-mode
            # overrides take effect - or fall back to the default profile's
            # values when it has none (Tk parity: top_bar re-ran load_paths on
            # every profile switch). Without this the previous profile's paths
            # stay live on the game object.
            try:
                from Utils.perftrace import span
                with span("game.load_paths"):
                    g.load_paths()
            except Exception:
                pass
