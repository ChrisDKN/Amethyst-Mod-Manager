"""Game/profile state controller for the Qt UI.

Thin wrapper over the real (toolkit-neutral) helpers in gui.game_helpers and
Games.base_game so the Qt app drives the same load flow as the Tk app:
discover games, list profiles, switch the active game/profile, and resolve the
active modlist.txt + staging dir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.game_helpers import (
    _load_games, _profiles_for_game, _load_last_game, _save_last_game, _GAMES,
)
from Utils.ui_config import load_last_session, save_last_session


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
    framework_file_mods: set = field(default_factory=set)
    # Mods whose loose file overrides some archive's copy of it. In no other
    # capability set (they ship no archive; the loose maps can't see archives)
    # yet toggling one flips two icons - hence its own entry in the disable
    # fast-path guard (see app._toggle_skips_conflict_scan).
    loose_beats_bsa_mods: set = field(default_factory=set)


class GameState:
    def __init__(self):
        self.game_names: list[str] = []
        self.game_name: str | None = None
        self.profile: str | None = None

    # -- discovery / load ---------------------------------------------------
    def load(self) -> None:
        """Discover games and select the last-used game + profile (from
        amethyst.ini [session], falling back to last_game.json / first game and
        the first profile). Populates game_names / game_name / profile."""
        self.game_names = _load_games()
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
        # Restore the profile: prefer the global session profile (the one open
        # when the app last closed), then this game's own last-active profile,
        # then the first profile. Records it as the game's last-active too.
        g = self.game
        per_game = g.get_last_active_profile() if g is not None else None
        self.profile = (self._select_profile(sess_profile)
                        if sess_profile else None) or \
            self._select_profile(per_game)
        self._apply_active_profile()
        self._materialize_if_group()
        self._save_last_active_profile()

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

    def _materialize_if_group(self) -> None:
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
            materialize_if_group(g, pdir)
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

    def bsa_index_path(self) -> Path | None:
        """Location of bsa_index.bin (next to filemap.txt), or None."""
        staging = self.staging_dir()
        if staging is None:
            return None
        p = staging.parent / "bsa_index.bin"
        return p if p.is_file() else None

    def build_conflicts(self, log_fn=None, rescan_index: bool = False) -> "ConflictData":
        """Build the filemap for the active game/profile and return a ConflictData
        with loose + BSA conflict codes, the override maps (for highlights), and a
        plugin→owner map. Expensive - run off-thread. Empty on failure.

        rescan_index=True forces a full re-scan of every mod folder from disk
        (the Refresh path) so file changes inside existing mods are picked up."""
        g = self.game
        if g is None or not self.profile:
            return ConflictData()
        from Utils.deploy_pipeline import _build_filemap_for_game
        from gui_qt.modlist_data import display_codes_from_conflict_map
        from Utils.perftrace import span
        log = log_fn or (lambda _m: None)
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
        with span("_build_filemap_for_game"):
            result = _build_filemap_for_game(
                g, self.profile, log_fn=log, rescan_index=rescan_index)
        if not result:
            return ConflictData()
        _count, _conflict_map, overrides, overridden_by = result
        with span("display_codes+copy_maps"):
            data = ConflictData(
                # Use the backend's real conflict_map so FULL (redundant) is kept.
                loose_codes=display_codes_from_conflict_map(_conflict_map),
                overrides={k: set(v) for k, v in (overrides or {}).items()},
                overridden_by={k: set(v) for k, v in (overridden_by or {}).items()},
                uuid_codes=dict(getattr(result, "uuid_codes", None) or {}),
            )
        with span("_build_bsa_conflicts"):
            (data.bsa_codes, data.bsa_overrides,
             data.bsa_overridden_by, lob) = self._build_bsa_conflicts(g, log)
        # A loose file beating a BSA is a loose-file win the filemap can't see;
        # fold it in or the winner shows no icon. Base copy = re-merge source.
        data.loose_codes_base = dict(data.loose_codes)
        self._merge_loose_beats_bsa(data.loose_codes, lob)
        data.loose_beats_bsa_mods = {m for m, b in (lob or {}).items() if b}
        with span("_build_plugin_owner"):
            data.plugin_owner = self._build_plugin_owner(g)
        with span("_build_index_flag_mods"):
            (data.prertx_mods, data.root_rule_mods, data.plugin_mods,
             data.bsa_mods, data.framework_file_mods) = \
                self._build_index_flag_mods(g)
        with span("detect_frameworks"):
            data.framework_statuses = self._detect_frameworks(g)
        return data

    def _detect_frameworks(self, g) -> list:
        """Framework banner statuses (worker-side; see ConflictData)."""
        try:
            from Utils.framework_detect import detect_frameworks
            staging = self.staging_dir()
            fm = (staging.parent / "filemap.txt") if staging is not None else None
            return detect_frameworks(g, fm, self.modlist_path(),
                                     rf_toggle_enabled=True)
        except Exception as exc:
            print(f"[gui_qt] framework detect error: {exc}", flush=True)
            return []

    def _build_index_flag_mods(self, g) -> "tuple[set, set, set, set, set]":
        """(prertx_mods, root_rule_mods, plugin_mods, bsa_mods,
        framework_file_mods) from modindex.bin. pre-RTX = a mod with a file
        under a remapped source prefix (e.g. natives/x64/); root-rule = a mod
        owning files matched by a custom routing rule with dest="". Ports
        gui/modlist_panel pre-RTX detect (~9307) + _compute_root_rule_mods
        (1699). The last three are the toggle-capability sets (see
        ConflictData). Runs on the conflict worker.

        All scans depend only on the index content + static game rules - a
        mod toggle/reorder doesn't touch modindex.bin - so the result is
        cached by (index path, mtime) and per-toggle rebuilds skip the
        ~100-150 ms file walk entirely."""
        from Utils.perftrace import span
        _empty = (set(), set(), set(), set(), set())
        staging = self.staging_dir()
        if staging is None:
            return _empty
        index_path = staging.parent / "modindex.bin"
        try:
            _st = index_path.stat()
            stat_key = (_st.st_mtime_ns, _st.st_size)
        except OSError:
            return _empty
        cache_key = (str(index_path), stat_key, getattr(g, "name", None))
        cached = getattr(self, "_flag_mods_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        try:
            from Utils.filemap import read_mod_index
            with span("flag_mods: read_mod_index"):
                index = read_mod_index(index_path)
        except Exception:
            index = None
        if not index:
            return _empty
        # pre-RTX: any file under a remapped source prefix.
        prertx: set = set()
        try:
            prefixes = [k.lower() for k in
                        (getattr(g, "mod_deploy_path_remap", {}) or {})]
        except Exception:
            prefixes = []
        if prefixes:
            with span("flag_mods: prertx scan"):
                for mod_name, entry in index.items():
                    normal = entry[0] if isinstance(entry, tuple) else {}
                    for rel_key in normal:
                        if any(rel_key.startswith(p) for p in prefixes):
                            prertx.add(mod_name)
                            break
        # Toggle capabilities: which mods ship plugin files / BSA-BA2s / files
        # basename-matching a framework exe (framework_detect matches staged
        # keys and disabled-mod files by basename). Root-namespace files
        # included - broader only makes the fast path MORE conservative.
        plugin_mods: set = set()
        bsa_mods: set = set()
        framework_mods: set = set()
        plugin_exts = tuple(e.lower() for e in
                            (getattr(g, "plugin_extensions", []) or [])) \
            or (".esp", ".esm", ".esl")
        archive_exts = tuple(e.lower() for e in
                             (getattr(g, "archive_extensions", None) or ())) \
            or (".bsa", ".ba2")
        try:
            fw_basenames = {
                str(p).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
                for p in (getattr(g, "frameworks", {}) or {}).values()}
        except Exception:
            fw_basenames = set()
        with span("flag_mods: capability scan"):
            for mod_name, entry in index.items():
                if not isinstance(entry, tuple):
                    continue
                normal = entry[0] or {}
                root = (entry[1] or {}) if len(entry) >= 2 else {}
                for rel_key in (*normal, *root):
                    base = rel_key.rsplit("/", 1)[-1]
                    if base.endswith(plugin_exts):
                        plugin_mods.add(mod_name)
                    if base.endswith(archive_exts):
                        bsa_mods.add(mod_name)
                    if fw_basenames and base in fw_basenames:
                        framework_mods.add(mod_name)
        # root-rule: mods owning files matched by a dest="" custom routing rule.
        root_rule: set = set()
        try:
            rules = list(getattr(g, "custom_routing_rules", None) or [])
            if any(r.dest == "" and not r.to_prefix for r in rules):
                from Utils.deploy_custom_rules import mods_matching_root_rules
                with span("flag_mods: root-rule match"):
                    mod_files = {
                        name: (list(entry[0].values()) + list(entry[1].values()))
                        for name, entry in index.items()
                        if isinstance(entry, tuple) and len(entry) >= 2}
                    root_rule = mods_matching_root_rules(mod_files, rules)
        except Exception:
            root_rule = set()
        result = (prertx, root_rule, plugin_mods, bsa_mods, framework_mods)
        self._flag_mods_cache = (cache_key, result)
        return result

    def _build_plugin_owner(self, g) -> dict[str, str]:
        """Map plugin filename (lower) → the mod that wins it, from filemap.txt.
        Used for plugin↔mod cross-panel highlighting."""
        staging = self.staging_dir()
        if staging is None:
            return {}
        exts = tuple(e.lower() for e in (getattr(g, "plugin_extensions", []) or []))
        if not exts:
            exts = (".esp", ".esm", ".esl")
        owner: dict[str, str] = {}
        # filemap_root.txt second: root-flagged mods deploy after (and over)
        # the main deploy, so they win same-named plugins.
        for fm in (staging.parent / "filemap.txt",
                   staging.parent / "filemap_root.txt"):
            if not fm.is_file():
                continue
            try:
                for line in fm.read_text(encoding="utf-8").splitlines():
                    if "\t" not in line:
                        continue
                    rel_key, mod = line.split("\t", 1)
                    base = rel_key.replace("\\", "/").rsplit("/", 1)[-1].lower()
                    if base.endswith(exts):
                        owner[base] = mod
            except Exception:
                continue
        return owner

    def _build_bsa_conflicts(self, g, log):
        """Compute BSA/BA2 archive conflicts. Returns (codes, overrides,
        overridden_by, loose_overrides_bsa) - codes as 1 win / -1 lose /
        2 mixed / 3 fully-overridden; the maps key mod → set(mods). Empty
        for non-archive games or on failure.

        loose_overrides_bsa (loose_mod → {bsa_mod}) is handed back rather than
        folded into ``codes``: that win belongs on the loose icon, not the
        archive one (the winner often ships no archive). See
        _merge_loose_beats_bsa.

        The "Hide BSA conflicts" setting empties the pipeline entirely (Tk
        parity) so the expensive parse is skipped and no codes are produced.
        It only applies to Bethesda BSA/BA2 games - UE pak conflicts are
        always shown (two paks touching the same asset is exactly the signal
        pak-game users need; there's no vanilla-BSA noise to hide there)."""
        empty = ({}, {}, {}, {})
        exts = frozenset(getattr(g, "archive_extensions", frozenset()) or frozenset())
        if not exts:
            return empty
        from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
        if not (exts & UE_ARCHIVE_EXTENSIONS):
            try:
                from Utils.ui_config import load_hide_bsa_conflicts
                if load_hide_bsa_conflicts():
                    return empty
            except Exception:
                pass
        staging = self.staging_dir()
        ml = self.modlist_path()
        if staging is None or ml is None or not ml.is_file():
            return empty
        try:
            from Utils.bsa_filemap import (
                build_bsa_conflicts, read_bsa_index, rebuild_bsa_index,
            )
            from Utils.plugins import read_loadorder
        except Exception:
            return empty
        out_dir = staging.parent
        bsa_index = out_dir / "bsa_index.bin"
        try:
            # Gate on whether the index PARSES, not on whether the file exists.
            # An index that is missing, corrupt, or written by an older version
            # all mean "nothing usable has been scanned" - existence alone left
            # a bad index (e.g. an older version's empty one) stuck forever,
            # silently reporting zero archive conflicts for every mod.
            if read_bsa_index(bsa_index) is None:
                rebuild_bsa_index(bsa_index, staging, exts, log_fn=log,
                                  follow_toplevel_links_under=g.get_profile_root() / "profiles")
            pdir = self.profile_dir()
            # UE pak mounting is not plugin-driven - winners follow pure mod
            # priority there, so skip the plugin load-order refinement.
            use_plugin_order = getattr(g, "archive_plugin_ordering", True)
            plugin_order = (read_loadorder(pdir / "loadorder.txt")
                            if (use_plugin_order and pdir is not None) else None)
            plugin_exts = frozenset(getattr(g, "plugin_extensions", []) or [])
            (bsa_map, bsa_over, bsa_overby, lob, bol) = build_bsa_conflicts(
                ml, bsa_index, exts,
                loose_index_path=out_dir / "modindex.bin",
                plugin_order=plugin_order or None,
                plugin_extensions=plugin_exts or None,
                # UE paks resolve by (_P boost, basename) mount order.
                archive_name_ordering=bool(exts & UE_ARCHIVE_EXTENSIONS),
                log_fn=log,
                # Resolved deploy map so Mod Files ▸ Disable exclusions clear
                # the flag (modindex.bin still lists excluded files). Fresh
                # here: the full path rebuilds it just before this, and the
                # plugin-toggle path doesn't touch it.
                loose_filemap_path=out_dir / "filemap.txt",
            )
        except Exception as exc:
            log(f"BSA conflict build failed: {exc}")
            return empty
        # build_bsa_conflicts returns CONFLICT_* codes; normalise to our scheme
        # (1 win / -1 lose / 2 mixed) matching conflicts_from_filemap.
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
        )
        codes: dict[str, int] = {}
        for name, c in bsa_map.items():
            if c == CONFLICT_WINS:
                codes[name] = 1
            elif c == CONFLICT_LOSES:
                codes[name] = -1
            elif c == CONFLICT_PARTIAL:
                codes[name] = 2
            elif c == CONFLICT_FULL:
                # Zero surviving paths - every file in the mod's archives is
                # overridden (by other archives and/or loose files). Its own
                # icon, same split as the loose codes.
                codes[name] = 3
        # Fold loose↔BSA cross relationships so highlights match the engine's
        # "loose beats BSA" rule (lob: loose_mod→{bsa_mod}, bol: bsa_mod→{loose}).
        over = {k: set(v) for k, v in (bsa_over or {}).items()}
        overby = {k: set(v) for k, v in (bsa_overby or {}).items()}
        for loose_mod, bsa_mods in (lob or {}).items():
            over.setdefault(loose_mod, set()).update(bsa_mods)
        for bsa_mod, loose_mods in (bol or {}).items():
            overby.setdefault(bsa_mod, set()).update(loose_mods)
        return codes, over, overby, {k: set(v) for k, v in (lob or {}).items()}

    @staticmethod
    def _merge_loose_beats_bsa(loose_codes: dict, lob: dict) -> dict:
        """Upgrade *loose_codes* in place for mods whose loose files override a
        BSA (lob: loose_mod → {bsa_mod, ...}).

        filemap.txt only ranks loose-vs-loose, so a mod winning solely against
        an archive sits at NONE (no icon). Fold that win into the loose code:

            NONE    → WINS      (its only conflict is over the BSA)
            LOSES   → PARTIAL   (loses to a loose mod, but beats a BSA)
            FULL    → PARTIAL   (fully overridden loosely, still beats a BSA -
                                 a surviving win means it isn't redundant)
            WINS/PARTIAL        unchanged - already showing a win."""
        from gui_qt.modlist_data import (
            DISP_WINS, DISP_LOSES, DISP_PARTIAL, DISP_FULL,
        )
        for loose_mod, bsa_mods in (lob or {}).items():
            if not bsa_mods:
                continue
            cur = loose_codes.get(loose_mod)
            if cur in (None, 0):
                loose_codes[loose_mod] = DISP_WINS
            elif cur in (DISP_LOSES, DISP_FULL):
                loose_codes[loose_mod] = DISP_PARTIAL
        return loose_codes

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
