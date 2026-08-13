"""Qt main window - header / body / footer rows + bottom log panel.

  header row : top bar (game/profile + actions) | play bar      (fixed split)
  body row   : modlist ║ plugins                                (draggable)
  footer row : mod tools | plugin tools                         (fixed split)
  log panel  : drag-resizable log text area + control bar
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal, QTimer, QT_TRANSLATE_NOOP
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QMainWindow, QToolButton, QWidget, QSplitter, QApplication,
    QLabel, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QTextBrowser,
    QFrame, QLineEdit, QPushButton, QMenu, QStackedWidget, QSizePolicy,
)

from gui_qt.theme_qt import apply_theme, active_palette, _c, contrast_text
from gui_qt.icons import icon, hamburger_icon
from gui_qt.modlist_model import ModListModel, COL_SIZE
from gui_qt.modlist_view import ModListView
from gui_qt.selector_button import SelectorButton, SplitPressHighlighter
from gui_qt.flow_layout import FlowLayout
from gui_qt.game_state import GameState
from gui_qt.detachable_tabs import DetachableTabWidget
from gui_qt.notification_center import NotificationHistory, NotificationButton
from gui_qt import glue
from Utils.proton_tools import DOTNET_VERSIONS
# Diagnostic prints here run on worker threads and use flush=True. Under
# Flatpak/AppImage stdout often has no reader, so a full pipe buffer makes
# print() raise BrokenPipeError, which killed the worker (e.g. the play/deploy
# build). safe_print swallows stream errors; the text is mirrored to the GUI
# log anyway. Shadowing the builtin makes every print(...) below crash-proof.
from Utils.app_log import safe_print as print  # noqa: A004


def _load_bg3_modio(stem: str):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path (the folder
    name has a space, so it isn't importable by dotted path). Cached in
    sys.modules under f"{stem}_bg3" - shared with the Tk copy and the wizard."""
    import importlib.util
    import sys as _sys
    mod_name = f"{stem}_bg3"
    cached = _sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent
               / "Games" / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    _sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _modio_key_present(game) -> bool:
    """True if this is BG3 and complete mod.io credentials are configured."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return False
        key, api_path = _load_bg3_modio("modio_key").load_modio_credentials()
        return bool(key and api_path)
    except Exception:
        return False


def _modio_api_path_missing(game) -> bool:
    """True for a migrated BG3 setup that has a key but no API path."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return False
        key, api_path = _load_bg3_modio("modio_key").load_modio_credentials()
        return bool(key and not api_path)
    except Exception:
        return False


def _check_modio_updates(game, staging, log_fn, only_names=None):
    """BG3-only mod.io update check. Returns a list of ModioUpdateInfo (mods
    with a newer file, or unknown installed version), or [] for other games /
    when no mod.io key is configured. *only_names* restricts the check to those
    staging folder names. Never raises."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return []
        api_key, api_path = _load_bg3_modio("modio_key").load_modio_credentials()
        if not api_key or not api_path:
            if api_key:
                log_fn("mod.io: API path missing - open the mod.io API Key tool.")
            return []
        checker = _load_bg3_modio("modio_update_checker")
        return checker.check_for_updates(
            Path(staging), api_key, api_path, progress_cb=log_fn,
            only_names=only_names)
    except Exception as e:
        log_fn(f"mod.io: update check failed - {e}")
        return []


# Set when the user changes language: run() re-execs the process after the event
# loop exits so the whole UI rebuilds in the new language.
_RESTART_REQUESTED = False

# Sentinel emitted on _one_install_done when a FOMOD/BAIN archive was handed off
# to a detached wizard tab: the pipeline advances to the next queued archive
# without counting or renaming it (the detached wizard owns its own outcome).
_WIZARD_HANDOFF = object()


# Quick-configure submenu labels come from the GUI-free Utils.quick_configure and
# are shown via self.tr(opt["label"]) / self.tr(clabel) in MainWindow - but
# lupdate can't see through that variable, so the source literals are registered
# here (context "MainWindow", matching the tr() call site) for extraction. Keep
# in sync with build_quick_configure_options().
_QUICK_CONFIGURE_TR = (
    QT_TRANSLATE_NOOP("MainWindow", "Deploy Method"),
    QT_TRANSLATE_NOOP("MainWindow", "Symlink"),
    QT_TRANSLATE_NOOP("MainWindow", "Symlink (Recommended)"),
    QT_TRANSLATE_NOOP("MainWindow", "Hardlink"),
    QT_TRANSLATE_NOOP("MainWindow", "Hardlink (Recommended)"),
    QT_TRANSLATE_NOOP("MainWindow", "Swap launcher with script extender on deploy"),
    QT_TRANSLATE_NOOP("MainWindow", "Apply the 4GB patch automatically on deploy"),
    QT_TRANSLATE_NOOP("MainWindow", "Auto deploy (on enable/disable/reorder)"),
    QT_TRANSLATE_NOOP("MainWindow", "Automatic archive invalidation (prefer loose files over BSAs)"),
    QT_TRANSLATE_NOOP("MainWindow", "Create case-alias symlinks on deploy (Faster load times)"),
    QT_TRANSLATE_NOOP("MainWindow", "Use profile-specific INI files"),
    QT_TRANSLATE_NOOP("MainWindow", "Use profile-specific saves"),
    QT_TRANSLATE_NOOP("MainWindow", "Prepend load-order numbers to mod folders"),
    QT_TRANSLATE_NOOP("MainWindow", "Manage load order in DFU"),
    QT_TRANSLATE_NOOP("MainWindow", "Game Patch Version"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 8"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 7"),
    QT_TRANSLATE_NOOP("MainWindow", "Patch 6"),
    QT_TRANSLATE_NOOP("MainWindow", "Plugins file name"),
)


class _CurrentPageStack(QStackedWidget):
    """A QStackedWidget that sizes to its *currently visible* page instead of
    the tallest one.

    QStackedLayout keeps every page laid out and reserves height for the tallest
    one, so a taller hidden page (e.g. the Downloads footer wrapping its buttons
    onto a second row at narrow widths) leaves a blank row above shorter visible
    pages. Reporting the current page's size hints isn't enough - the layout's
    own minimum still wins - so we also clamp our maximum height to the current
    page's wrapped (heightForWidth) height, refreshed on resize and page swap."""

    def _current_height(self) -> int:
        w = self.currentWidget()
        if w is None:
            return -1
        if w.hasHeightForWidth():
            return w.heightForWidth(self.width() or w.sizeHint().width())
        return w.sizeHint().height()

    def clamp_to_current(self) -> None:
        """Clamp our max height to the current page so hidden (taller) pages
        don't reserve extra rows. Safe to call after a page swap or resize."""
        h = self._current_height()
        if h >= 0:
            # A max (not fixed) height is enough to drop the extra rows the
            # hidden pages reserve, without fighting the parent layout.
            self.setMaximumHeight(h)
            self.updateGeometry()

    def sizeHint(self) -> QSize:            # noqa: N802
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:     # noqa: N802
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()

    def hasHeightForWidth(self) -> bool:    # noqa: N802
        w = self.currentWidget()
        return w.hasHeightForWidth() if w is not None else False

    def heightForWidth(self, width: int) -> int:   # noqa: N802
        w = self.currentWidget()
        if w is not None and w.hasHeightForWidth():
            return w.heightForWidth(width)
        return super().heightForWidth(width)

    def resizeEvent(self, e):               # noqa: N802
        super().resizeEvent(e)
        self.clamp_to_current()


class _HeaderBar(QWidget):
    """The top bar. Reports its own resizes so the action buttons can collapse
    to icon-only when the window is too narrow for their labels (tiling the
    manager beside another window) - see MainWindow._sync_header_compact."""

    def __init__(self, on_resize):
        super().__init__()
        self._on_resize = on_resize

    def resizeEvent(self, e):               # noqa: N802
        super().resizeEvent(e)
        self._on_resize()


class _InstalledRoot:
    """Stand-in "requested mod" row for the Thunderstore dependency modal.

    A manually installed package is already on disk, so it gets the modal's
    fixed (checkbox-less) row while only its missing dependencies are
    selectable. The overlay reads just ``full_name`` and ``file_size``, and
    hands the object back in its result - the caller filters it out.
    """

    def __init__(self, full_name: str):
        self.full_name = full_name
        self.file_size = 0


class MainWindow(QMainWindow):
    # Carries (generation, ConflictData) from a worker thread to the UI thread
    # (queued connection - thread-safe). See _rebuild_conflicts_async.
    _conflicts_ready = Signal(int, object)
    # (generation, bsa_codes, bsa_overrides, bsa_overridden_by, loose_codes) -
    # BSA-only recompute after a plugin toggle/reorder (no filemap rebuild).
    # loose_codes is the re-merged loose map, or None if it couldn't be built.
    _bsa_conflicts_ready = Signal(int, object, object, object, object)
    # (generation) - filemap-only rebuild finished (disable fast path: the
    # conflict scan was provably redundant). See _rebuild_filemap_light_async.
    _filemap_light_done = Signal(int)
    # (generation, list[FrameworkStatus]) from the framework-detect worker -
    # detect_frameworks reads filemap.txt + the mod index, too slow for the UI
    # thread on a big modlist. See _refresh_framework_banner.
    _framework_statuses_ready = Signal(int, object)
    # Deploy/restore worker → UI thread (thread-safe queued connections).
    _op_progress = Signal(int, int, object)   # (done, total, phase|None)
    _op_log = Signal(str)
    _op_done = Signal(str, bool, object)       # (kind, success, warnings-list)
    # Install worker → UI thread.
    _install_done = Signal(int, int, object)   # (ok_count, total, names-list)
    _prepared_ready = Signal(object)           # (PreparedInstall|None)
    _one_install_done = Signal(object)         # (installed name|None)
    # A detached FOMOD/BAIN wizard finished staging on a worker thread → UI
    # thread reports the toast / rename prompt / modlist reload. Payload:
    # (installed name|None, PreparedInstall, prev_name|None).
    _wizard_finish_done = Signal(object, object, object)
    # Parallel phase of a multi-archive install finished → UI thread starts the
    # deferred FOMOD/BAIN phase. (installed-names list, deferred-paths list)
    _install_batch_stage_done = Signal(object, object)
    # Worker asks the UI to show the Set-Prefix overlay; payload carries a
    # result holder + threading.Event the worker blocks on.
    _need_prefix = Signal(object)              # (dict with required/file_list/...)
    # Worker asks the UI to show the Mod-Already-Exists overlay (same blocking
    # holder+Event handshake as _need_prefix).
    _mod_exists = Signal(object)               # (dict with mod_name/conflict/holder/event)
    # Deploy worker asks the UI to show the Cyberpunk CET symlink warning
    # (same blocking holder+Event handshake as _need_prefix).
    _confirm_cet = Signal(object)              # (dict with holder/event)
    # Deploy worker asks the UI to show the Windows-filesystem (NTFS/exFAT)
    # advisory (same handshake; GH#307).
    _confirm_windows_fs = Signal(object)       # (dict with holder/event/hits/fp/game_name)
    # Deploy worker asks the UI to show the Fallout 3 Anniversary-Edition
    # downgrade prompt (same handshake).
    _confirm_downgrade = Signal(object)        # (dict with holder/event/version/game)
    # Proton-tools installer worker → UI thread.
    _proton_done = Signal(str, bool)           # (title, success)
    # GL capability probe worker → UI thread (see _start_gl_warmup).
    _gl_probe_done = Signal(bool)              # (OpenGL widget path usable)
    _nif_archive_ready = Signal(int, object, object)  # gen, bytes|None, context
    # Nexus validate() worker → UI thread (username or None).
    _nexus_validated = Signal(object)          # (username str | None)
    # LOOT Sort Plugins worker → UI thread (SortResult | None on error).
    _sort_plugins_ready = Signal(object)
    # LOOT record-overlap worker → UI thread: (target plugin name,
    # list[str] overlaps | None on error). Full libloot load, so off-thread.
    _overlap_ready = Signal(str, object)
    # Plugin reload worker → UI thread: (gen, rows, plugin_paths, userlist
    # state, prune report). The disk work (per-plugin header reads) runs
    # off-thread; a generation counter drops results from a superseded reload.
    _plugins_loaded = Signal(int, object, object, object, object)
    # Deferred ESL-eligibility worker → UI thread (gen, {name_lower: PF bit}).
    _esl_elig_ready = Signal(int, object)
    # Size-column disk walk worker → UI thread (gen, sizes, size_bytes).
    _sizes_ready = Signal(int, object, object)
    # Overwrite / Root Folder file-count walk → UI thread (gen, counts dict).
    _boundary_counts_ready = Signal(int, object)
    # Modlist meta.ini read worker → UI thread (gen, payload dict).
    _modlist_meta_ready = Signal(int, object)
    # Filter-data disk scan worker → UI thread (gen, payload dict | None).
    _filter_data_ready = Signal(int, object)
    # Nexus OAuth client (background thread) → UI thread.
    # (kind, payload): kind in {"token","error","status"}.
    _oauth_event = Signal(str, object)
    # Check-for-updates worker → UI thread ((updates, missing) | None on error).
    _updates_ready = Signal(object)
    # App self-update check worker → UI thread
    # ((current, latest, mode, is_prerelease, is_downgrade)).
    _app_update_found = Signal(object)
    # Endorse/abstain worker → UI thread ({"ok": n, "endorse": bool}).
    _endorse_done = Signal(object)
    # Track worker → UI thread ({"ok": n}).
    _track_done = Signal(object)
    # "Endorse AMM" worker → UI thread ({"state": str, "message": str}).
    _amm_endorse_done = Signal(object)
    # ui_hooks.warn from any backend thread → OK-only popup on the UI thread
    # ((title, message, card_h|None)).
    _warn_popup = Signal(str, str, object)
    _play_toast_done = Signal(str, str)
    # Copy/Move-to-profile worker → UI thread (result dict).
    _copy_done = Signal(object)
    # Collection update: staging meta.ini scan (worker) → apply (UI).
    _col_update_scan_done = Signal(object)
    # Install-a-Nexus-mod-by-id flow (used by Missing Requirements) → UI thread.
    _qu_resolved = Signal(object, object)         # (queue list, skipped list)
    _qu_downloaded = Signal(object, object)       # (dl_items list, failed list)
    _qu_dl_progress = Signal("qlonglong", "qlonglong")  # aggregate (cur_bytes, total_bytes; 64-bit: >2GB)
    _reinstall_downloaded = Signal(object, object)  # (dl_items list, failed list)
    _reinstall_dl_progress = Signal("qlonglong", "qlonglong")  # aggregate bytes (64-bit)
    _thunderstore_reinstall_downloaded = Signal(object)
    # Non-premium reinstall: file metadata resolved → arm the manual flow.
    # ([(mod_name, domain, mod_id, file_id, NexusModFile-like), …]).
    _reinstall_manual_ready = Signal(object)
    # Non-premium reinstall: a browser download landed (or an existing archive
    # was found). Payload: (mod_name, archive_path, prebuilt_meta|None, dl_key,
    # installed Thunderstore meta|None).
    _reinstall_manual_found = Signal(object)
    _req_install_files = Signal(object, object)   # (ctx dict, files|None)
    _req_install_dl = Signal(object, object, object)  # (archive|None, meta|None, dl_key)
    _req_install_prog = Signal(object, object, "qlonglong", "qlonglong")  # (dl_key, name, downloaded, total bytes; 64-bit: >2GB)
    # Collection reset-load-order worker → UI thread (result dict).
    _reset_done = Signal(object)
    # Collection INSTALL worker → UI thread. Every callback is a single emit; the
    # connected slots (UI thread) update the install overlay. (verb, payload)
    # batches keep the surface small. See _install_collection.
    _col_status = Signal(str)
    _col_progress = Signal(object)             # float | None
    _col_agg = Signal("qlonglong", "qlonglong", float)  # bytes cur, total, MB/s (64-bit: >2GB)
    _col_display_total = Signal("qlonglong")   # true collection size (bytes, 64-bit)
    _col_dl = Signal(str, object)              # ("start"|"update"|"finish", payload)
    _col_extract = Signal(str, object)         # ("queue"|"add"|"update"|"remove", payload)
    _col_row = Signal(int)                     # file_id installed
    _col_manual = Signal(object)               # manual-mode current-mod payload dict
    _col_finished = Signal(str, object)        # ("done"|"paused"|"cancelled", payload)
    _appended_col_removed = Signal(str, bool)  # appended-collection remove worker → UI
    _col_import_done = Signal(object)          # (profile_name, installed, total, skipped)
    _import_file_picked = Signal(object)       # portal picker result (Path|None) → UI thread
    _export_code_ready = Signal(object, int)   # (code str|None, mod_count) share-code build → UI thread
    _install_files_picked = Signal(object)     # portal picker result (list[Path]) → UI thread
    _custom_exe_picked = Signal(object)        # portal picker result (Path|None) → UI thread
    # Worker blocks on these to show the deferred FOMOD / BAIN pickers (holder+Event).
    _col_fomod = Signal(object)
    _col_bain = Signal(object)
    # BSA/BA2 pack + unpack workers → UI thread (result dict | error str). See
    # _on_pack_bsa / _on_unpack_bsa.
    _bsa_op_done = Signal(object)
    # Custom-handler background sync worker → UI thread (files were written).
    _handlers_synced = Signal()
    # Manual force-update of one repo handler → UI thread: (game name, status).
    _handler_force_updated = Signal(str, str)
    # Language (.qm) background sync worker → UI thread (translations updated).
    _languages_synced = Signal()
    # Ludusavi save-path data sync worker → UI thread (newer data adopted).
    _ludusavi_synced = Signal()
    # Flatpak 32-bit extension repair worker → UI thread (install succeeded?).
    _i386_repair_done = Signal(bool)
    # NXM link received from a second instance via the IPC socket (fires on a
    # worker thread → marshal to the UI thread). Also used for --nxm at startup.
    _nxm_received = Signal(str)
    # NXM download worker → UI thread: (DownloadResult, mod_info, file_info).
    _nxm_download_done = Signal(object)
    # Thunderstore ror2mm:// link received from a second instance (worker
    # thread → UI thread). Shares the NXM IPC socket; routed by scheme.
    _ror2mm_received = Signal(str)
    # Thunderstore resolve worker → UI thread: (Ror2mmLink, ResolveResult,
    # packages) - the dependency confirmation modal is shown from here.
    _ror2mm_resolved = Signal(object)
    # Thunderstore download worker → UI thread:
    # (ThunderstoreDownloadResult, Ror2mmLink, version_info, dl_key).
    _ror2mm_download_done = Signal(object)
    # Thunderstore-only update check worker → UI thread: (results, toast, subset).
    _ts_updates_ready = Signal(object)
    # Manifest-identify worker → UI thread: (results|None, toast).
    _ts_identify_ready = Signal(object)
    # Post-install auto-identify → UI thread: [(mod_name, package_id, version)].
    _ts_auto_identified = Signal(object)

    _PLAY_BAR_W = 380       # play-bar (header right) fixed width
    _BTN_H = 42          # consistent height for all header buttons (~30% bigger)
    _ICON_PX = 24        # header button icon size
    _FOOT_BTN_H = 28     # compact height for footer tool buttons
    _FOOT_ICON_PX = 16   # footer button icon size

    #: Filters footer button attr -> (filter-panel attr, search-box attr).
    #: _sync_filters_btn reads both to decide whether the button lights up.
    _FILTER_BTN_SOURCES = {
        "_modlist_filters_btn": ("_modlist_filter_panel",    "_modlist_search"),
        "_plugin_filters_btn":  ("_plugin_filter_panel",     "_plugins_search"),
        "_mf_filters_btn":      ("_mod_files_filter_panel",  "_mod_files_search"),
        "_data_filters_btn":    ("_data_filter_panel",       "_data_search"),
        "_saves_filters_btn":   ("_saves_filter_panel",      "_saves_search"),
        "_dl_filters_btn":      ("_downloads_filter_panel",  "_dl_search"),
        "_tf_filters_btn":      ("_text_files_filter_panel", "_tf_search"),
    }

    def __init__(self, app, splash=None):
        super().__init__()
        self._app = app
        # Startup splash: held open past show() and dismissed on the first
        # completed conflict rebuild (the last of the heavy first-load work).
        # A watchdog closes it anyway if that signal never arrives.
        self._splash = splash
        self._splash_dismissed = False
        self._gl_warmup = None            # 1px QOpenGLWidget, see _start_gl_warmup
        self._gl_probing = False
        self._pal = active_palette()
        self._gs = GameState()
        self._gs.load()
        self._conflicts_ready.connect(self._on_conflicts_ready)
        self._bsa_conflicts_ready.connect(self._on_bsa_conflicts_ready)
        self._filemap_light_done.connect(self._on_filemap_light_done)
        self._framework_statuses_ready.connect(self._on_framework_statuses)
        self._nif_archive_ready.connect(self._on_nif_archive_ready)
        from gui_qt.worker import LatestWorker
        self._nif_archive_jobs = LatestWorker("nif-archive-read")
        self._nif_open_gen = 0
        # Drops stale framework-detect results (game switched mid-compute).
        self._framework_gen = 0
        # Deploy/restore state + notification host.
        self._deploy_running = False
        self._deploy_rerun_pending = False
        # Auto-deploy guard: a deploy triggers _reload_modlist → conflict rebuild
        # → _on_conflicts_ready; without this flag that would re-fire auto-deploy
        # in an infinite loop (Tk parity: gui.py _auto_deploy_in_progress).
        self._auto_deploy_in_progress = False
        self._op_silent = False   # silent (auto) deploy: suppress progress popup
        self._post_deploy_action = None   # launch closure run after deploy succeeds
        self._deploy_done_hooks: list = []   # wizard on_done(ok) one-shots
        self._restore_done_hooks: list = []  # wizard on_done(ok) one-shots
        self._progress_popup = None
        self._notifier = None
        self._notif_history = NotificationHistory(self)
        self._op_progress.connect(self._on_op_progress)
        self._op_log.connect(self._append_log)
        self._op_done.connect(self._on_op_done)
        self._warn_popup.connect(self._show_warn_popup)
        self._play_toast_handle = None
        self._play_toast_done.connect(self._end_play_toast)
        self._init_log_file()   # one on-disk log file per session
        self._bsa_op_running = False
        self._bsa_op_done.connect(self._on_bsa_op_done)
        # Long-running external tools (VRAMr/BENDr/ParallaxR) that read the
        # DEPLOYED Data folder: label per holder, so two tools can't clear each
        # other's lock. Non-empty = deploy/restore/play refuse (_tool_busy).
        self._tool_locks: dict[str, str] = {}
        self._install_running = False
        self._pending_install_batches: list[dict] = []
        self._install_handoff_paths: set[str] = set()
        self._pending_thunderstore_meta: dict[str, tuple] = {}
        # Detached FOMOD/BAIN wizards: an open wizard is pure UI wait (no
        # staging), so it must NOT hold the install pipeline - each opens in its
        # own uniquely-keyed tab and stages independently on finish. Counter for
        # unique tab keys + a queue that serialises only the staging step (which
        # mutates the shared game._active_profile_dir and MUST stay one-at-a-time
        # against installs/deploys - the wrong-profile bug _install_paths guards).
        self._wizard_seq = 0
        self._staged_finish_queue: list = []
        self._staged_finish_running = False
        # Deploy/Restore requested while a detached-wizard staging job held the
        # shared game - deferred here and re-run once the staged queue drains
        # (installs have their own _pending_install_batches queue instead).
        self._pending_after_staged: list = []
        self._active_downloads: dict[str, dict] = {}   # key → name/done/total/fin
        self._install_done.connect(self._on_install_done)
        self._prepared_ready.connect(self._on_prepared_ready)
        self._one_install_done.connect(self._on_one_install_done)
        self._wizard_finish_done.connect(self._on_wizard_finish_done)
        self._install_batch_stage_done.connect(self._on_install_batch_stage_done)
        self._need_prefix.connect(self._on_need_prefix_ui)
        self._mod_exists.connect(self._on_mod_exists_ui)
        self._confirm_cet.connect(self._on_confirm_cet_ui)
        self._confirm_windows_fs.connect(self._on_confirm_windows_fs_ui)
        self._confirm_downgrade.connect(self._on_confirm_downgrade_ui)
        self._proton_busy = False
        self._proton_done_cb = None     # one-shot completion hook (health check)
        self._proton_done.connect(self._on_proton_done)
        # Game-scoped panel views (lazily built; closed on game change).
        self._profile_settings_view = None
        self._dll_overrides_view = None
        self._sort_running = False
        self._overlap_running = False
        self._sort_plugins_ready.connect(self._on_sort_plugins_ready)
        self._overlap_ready.connect(self._on_overlap_ready)
        self._plugins_gen = 0
        self._plugins_loaded.connect(self._on_plugins_loaded)
        self._esl_elig_ready.connect(self._on_esl_elig_ready)
        self._sizes_gen = 0
        self._sizes_ready.connect(self._on_sizes_ready)
        self._boundary_counts_gen = 0
        self._boundary_counts_ready.connect(self._on_boundary_counts_ready)
        self._modlist_meta_gen = 0
        self._modlist_meta_ready.connect(self._on_modlist_meta_ready)
        self._filter_data_gen = 0
        self._filter_data_ready.connect(self._on_filter_data_ready)
        try:
            from version import __version__ as _mm_version
        except Exception:
            _mm_version = ""
        self.setWindowTitle(
            self.tr("Amethyst Mod Manager - v{0}").format(_mm_version) if _mm_version
            else self.tr("Amethyst Mod Manager")
        )
        want_w, want_h = 1280, 800
        try:
            scr = (app or QApplication.instance()).primaryScreen()
            if scr is not None:
                ag = scr.availableGeometry()
                want_w = min(want_w, max(640, ag.width() - 40))
                want_h = min(want_h, max(480, ag.height() - 80))
        except Exception:
            pass
        self.setMinimumSize(want_w, want_h)
        # Debounced as-you-go persistence of the window geometry + body
        # splitter, so the state survives exits that never reach closeEvent
        # (Ctrl+C in the launching terminal, SIGKILL, crashes).
        self._win_state_timer = QTimer(self)
        self._win_state_timer.setSingleShot(True)
        self._win_state_timer.setInterval(1000)
        self._win_state_timer.timeout.connect(self._save_window_state)
        self.resize(want_w, want_h)
        # Restore the last-session window position/size/maximized state.
        # restoreGeometry() validates the saved rect against the current screen
        # layout (a since-unplugged monitor falls back on-screen), so a stale
        # save can't strand the window where the user can't see it.
        restored = False
        try:
            from Utils.ui_config import load_qt_window_state
            geo = load_qt_window_state().get("geometry")
            if geo:
                from PySide6.QtCore import QByteArray
                restored = bool(self.restoreGeometry(
                    QByteArray.fromBase64(geo.encode("ascii"))))
        except Exception:
            restored = False
        # Centre on the primary screen. The WM otherwise defaults the window to
        # (0,0) in GLOBAL coords, which lands OFF-SCREEN on a multi-head / offset
        # layout (e.g. a primary screen whose origin isn't at x=0) - the window
        # then "doesn't open" because it's drawn where you can't see it.
        if not restored:
            try:
                scr = (app or QApplication.instance()).primaryScreen()
                if scr is not None:
                    ag = scr.availableGeometry()
                    self.move(ag.center().x() - want_w // 2,
                              ag.center().y() - want_h // 2)
            except Exception:
                pass

        # Header+body+footer go in a vertical splitter with the log text area
        # so the log is drag-resizable; the log control bar stays fixed below.
        main_content = QWidget()
        mc = QVBoxLayout(main_content)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(0)
        mc.addWidget(self._build_header_row())
        mc.addWidget(self._build_body_row(), 1)
        # (The tool footers now live inside each panel - see _build_body_row /
        # _build_modlist_area - not in a separate window-wide row.)

        # The main content is the permanent first tab; overlay-style views (Add
        # Game, Nexus browser, …) open as further tabs that can be detached.
        self._tabs = DetachableTabWidget()
        self._tabs.add_permanent(main_content, self.tr("Mods"))
        # Let the tab bar's right-click "Pin to…" menu move any tab between the
        # modlist panel, the plugins panel, or full screen. The stacks were built
        # in _build_body_row above.
        self._tabs.register_scope_targets(
            self._modlist_panel_stack, self._plugins_panel_stack)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setObjectName("LogView")
        self._log_view.setMinimumHeight(0)   # can collapse fully

        self._vsplit = QSplitter(Qt.Vertical)
        self._vsplit.addWidget(self._tabs)
        self._vsplit.addWidget(self._log_view)
        self._vsplit.setStretchFactor(0, 1)
        self._vsplit.setStretchFactor(1, 0)
        self._vsplit.setCollapsible(0, True)     # drag past min → log fills window
        self._vsplit.setCollapsible(1, True)     # log collapses to 0; handle stays
        self._vsplit.setHandleWidth(4)
        self._vsplit.splitterMoved.connect(lambda *_: self._sync_log_controls())

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._vsplit, 1)
        outer.addWidget(self._build_log_bar())   # fixed control bar
        self.setCentralWidget(central)

        from gui_qt.file_pickers import build_pickers
        wired = glue.register_all(
            app, log=self._append_log, parent_window=self,
            # Backend warnings (ui_hooks.warn) become an OK-only popup; the
            # signal hop makes the call safe from any worker thread.
            warn=lambda title, message, height=None, **kw: self._warn_popup.emit(
                str(title), str(message), height),
            # Last-resort QFileDialog pickers, so hosts with no portal
            # FileChooser backend (Sway et al.) still get a usable browser.
            file_pickers=build_pickers(self),
        )
        print("[gui_qt] glue wired:", ", ".join(wired))

        # Start with the log collapsed (deferred until the layout has real size).
        QTimer.singleShot(0, lambda: (self._vsplit.setSizes(
            [self._vsplit.height(), 0]), self._sync_log_controls()))

        # Populate selectors from discovered games and load the active modlist.
        self._populate_selectors()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

        # Global keyboard shortcuts (F2/F5/Ctrl+D/… - Tk parity) + last-panel
        # tracking so shortcuts route to the modlist or plugins panel.
        from gui_qt.shortcuts import register_shortcuts
        register_shortcuts(self)

        # Connect to Nexus (if logged in) so the footer can show the username +
        # rate limits; validate runs on a worker so startup isn't blocked.
        self._nexus_api = None
        self._nexus_validated.connect(self._on_nexus_validated)
        # Live OAuth login client while a browser login is in flight (kept so the
        # "Paste login code" fallback can feed its session). None when idle.
        self._oauth_client = None
        self._oauth_event.connect(self._on_oauth_event)
        # Check-for-updates: re-entrancy guard + worker→UI signal.
        self._updates_running = False
        self._updates_ready.connect(self._on_updates_ready)
        # Quick Update: resolve (worker) → download (worker) → install (batch).
        self._quick_updating = False
        self._qu_resolved.connect(self._on_qu_resolved)
        self._qu_downloaded.connect(self._on_qu_downloaded)
        self._qu_dl_progress.connect(self._on_qu_dl_progress)
        self._reinstall_downloaded.connect(self._on_reinstall_downloaded)
        self._reinstall_dl_progress.connect(self._on_reinstall_dl_progress)
        self._thunderstore_reinstall_downloaded.connect(
            self._on_thunderstore_reinstall_downloaded)
        self._reinstall_manual_ready.connect(self._on_reinstall_manual_ready)
        self._reinstall_manual_found.connect(self._on_reinstall_manual_found)
        self._endorse_done.connect(self._on_endorse_done)
        self._track_done.connect(self._on_track_done)
        self._amm_endorse_done.connect(self._on_amm_endorse_done)
        self._copy_done.connect(self._on_copy_done)
        self._col_update_scan_done.connect(self._finish_collection_update)
        # App-level non-premium installs (reinstall / missing requirements):
        # (domain, mod_id) → (ManualDownloadWatcher, dl_key) while waiting
        # for a browser download to land. (The Nexus browser / Change Version
        # tabs keep their own registries.)
        self._app_manual_watchers = {}
        # Install-a-Nexus-mod-by-id (Missing Requirements) flow.
        self._req_installing = False
        self._req_install_files.connect(self._on_req_install_files)
        self._req_install_dl.connect(self._on_req_install_dl)
        self._req_install_prog.connect(
            lambda key, name, d, t: self._nexus_download_progress(key, name, d, t))
        self._reset_done.connect(self._on_reset_done)
        self._reset_running = False
        # Collection install state + Signal→UI-thread wiring.
        self._col_install_running = False
        self._col_install_overlay = None
        self._col_install_control = None
        self._col_install_slug = ""
        self._col_bundle_zip = ""      # local .amethyst pending bundle extraction
        self._col_offsite = []         # (name, url) manual mods - reminder on done
        self._col_created_profile = False  # this run created the target profile (GH#278)
        self._col_profile_name = ""
        self._col_status.connect(self._on_col_status)
        self._col_progress.connect(self._on_col_progress)
        self._col_agg.connect(self._on_col_agg)
        self._col_display_total.connect(self._on_col_display_total)
        self._col_dl.connect(self._on_col_dl)
        self._col_extract.connect(self._on_col_extract)
        self._col_row.connect(self._on_col_row)
        self._col_manual.connect(self._on_col_manual)
        self._col_finished.connect(self._on_col_finished)
        self._appended_col_removed.connect(self._on_appended_col_removed)
        self._col_import_done.connect(self._on_import_bundle_done)
        self._import_file_picked.connect(self._on_import_file_picked)
        self._export_code_ready.connect(self._on_export_code_ready)
        self._install_files_picked.connect(self._on_install_files_picked)
        self._custom_exe_picked.connect(self._on_custom_exe_picked)
        self._col_fomod.connect(self._on_col_fomod_ui)
        self._col_bain.connect(self._on_col_bain_ui)
        QTimer.singleShot(0, self._ensure_nexus_api)
        # NXM protocol handling: the IPC callback fires on a worker thread, so
        # it emits _nxm_received to hop onto the UI thread. Process a --nxm URL
        # passed on our own command line once the window has finished building.
        self._nxm_install_queue: list = []
        self._nxm_received.connect(self._receive_nxm)
        self._nxm_download_done.connect(self._on_nxm_download_done)
        self._ror2mm_received.connect(self._receive_ror2mm)
        self._ror2mm_resolved.connect(self._on_ror2mm_resolved)
        self._ror2mm_download_done.connect(self._on_ror2mm_download_done)
        self._ts_updates_ready.connect(self._on_thunderstore_updates_ready)
        self._ts_identify_ready.connect(self._on_thunderstore_identify_ready)
        self._ts_auto_identified.connect(self._on_thunderstore_auto_identified)
        self._handle_nxm_argv()
        self._handle_ror2mm_argv()
        # Silently sync custom handlers + Qt wizard plugins from the Resources
        # branch on GitHub (background threads). A fresh/updated build re-fetches
        # immediately because the gh_cache is wiped when the app version changes.
        self._handlers_synced.connect(self._on_handlers_synced)
        self._handler_force_updated.connect(self._on_handler_force_updated)
        self._languages_synced.connect(self._on_languages_synced)
        self._ludusavi_synced.connect(self._on_ludusavi_synced)
        try:
            from Utils.gh_cache import clear_if_version_changed
            clear_if_version_changed(_mm_version)
        except Exception:
            pass
        QTimer.singleShot(3000, self._start_gh_sync)
        # Sweep leftover "<Data>.mm_trash-*" dirs from restores whose deferred
        # background delete was interrupted (crash / app close mid-delete).
        QTimer.singleShot(0, self._sweep_deploy_trash_startup)
        # App self-update check (Tk parity: after(2000, …)). AppImage/flatpak
        # compare against GitHub releases, everything else against the AUR.
        self._update_overlay = None
        self._app_update_found.connect(self._on_app_update_found)
        QTimer.singleShot(2000, self._check_for_app_update)
        # First-run onboarding: show it (as a fullscreen tab) only when the flag
        # is unset/0 AND no games are configured. Either a completed onboarding
        # or an existing game is enough to skip it. Deferred so the window
        # finishes building first.
        self._onboarding_view = None
        from Utils.ui_config import load_onboarding_complete
        from Utils.game_helpers import _GAMES
        configured = sum(1 for g in _GAMES.values() if g.is_configured())
        if not load_onboarding_complete() and configured == 0:
            QTimer.singleShot(0, self._open_onboarding_tab)

        # Warn if any game handler failed to load. A broken handler used to make
        # its game silently vanish from the list, which users mistook for the
        # game (and its mods) being deleted - mods actually live untouched in
        # ~/.config and Profiles/. Deferred so the log panel is live first.
        QTimer.singleShot(0, self._warn_handler_load_failures)

        # Flatpak self-heal: bundle installs (release zip / Warehouse) don't
        # pull the Compat.i386 extension the manifest declares, leaving
        # /lib/ld-linux.so.2 dangling - every Proton/wine run in the sandbox
        # then fails with "could not open". Install the refs in the background.
        self._i386_repair_done.connect(self._on_i386_repair_done)
        self._i386_toast = None
        QTimer.singleShot(1000, self._check_flatpak_i386)

        # Splash watchdog: the splash is normally dismissed by the first
        # _on_conflicts_ready, but a game with no profile / empty modlist may
        # never trigger a conflict rebuild. Close it unconditionally after a
        # short grace period so it can never hang on screen.
        if self._splash is not None:
            QTimer.singleShot(4000, self._dismiss_splash)

    def _dismiss_splash(self):
        """Reveal the finished window and close the startup splash, exactly once.

        The window is shown at zero opacity while loading (see run()); restoring
        opacity here makes the now fully-rendered UI appear all at once, then the
        splash closes on top of it."""
        if self._splash_dismissed:
            return
        self._splash_dismissed = True
        self.setWindowOpacity(1.0)
        s, self._splash = self._splash, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        self.raise_()
        self.activateWindow()

    def _start_gl_warmup(self):
        """Probe the OpenGL path on a worker thread and, if the answer lands
        before the window is drawn, park a 1px QOpenGLWidget on it.

        The FIRST QOpenGLWidget added to a shown window makes Qt recreate the
        native window as a GL-composited surface - a full-window flash that
        reads as an app restart. Doing it while the splash still covers a
        zero-opacity window hides it, so opening the first mesh is seamless.

        Three rules keep the launch path out of harm's way:

        * The probe runs a child interpreter (see gui_qt/gl_support) and can
          take a second or more, so it never runs on the GUI thread. It cannot
          run *in* this process at all: a GLX stack with no matching FBConfig
          makes Qt qFatal, which killed the app when the probe was inline.
        * A verdict that arrives after the window is on screen is dropped -
          the flash would then be visible for no reason, and nif_preview pays
          it only if a mesh is actually opened.
        * Only games that ship the NIF Viewer start any of this, so nobody
          else's launch spawns a probe or composites through GL (a broken GL
          stack blacks out the whole window - GH#350).
        """
        if self._gl_warmup is not None or self._gl_probing:
            return
        game = getattr(getattr(self, "_gs", None), "game", None)
        game_id = getattr(game, "game_id", "") or ""
        if not game_id:
            return
        try:
            from Utils.plugin_loader import get_builtin_wizard_tools_for_game
            if not any(t.id == "nif_viewer"
                       for t in get_builtin_wizard_tools_for_game(game_id)):
                return
        except Exception:
            return
        from gui_qt.gl_support import prime_platform
        prime_platform()      # GUI thread: the worker must not ask Qt itself
        self._gl_probing = True
        self._gl_probe_done.connect(self._on_gl_probe_done)

        def _run():
            from gui_qt.safe_emit import safe_emit
            from gui_qt.gl_support import gl_status
            try:
                ok = gl_status()[0]
            except Exception:
                ok = False
            safe_emit(self._gl_probe_done, ok)

        import threading
        threading.Thread(target=_run, daemon=True).start()

        # Oblivion-era meshes need the vendored nif.xml interpreter.  Its
        # first parse is a few hundred milliseconds, so pay that once beside
        # the existing GL warm-up instead of on the first selected mesh.
        def _warm_nif_spec():
            try:
                from Utils.nif_xml import load_spec
                load_spec()
            except Exception:
                pass

        if game_id == "oblivion":
            threading.Thread(target=_warm_nif_spec, daemon=True,
                             name="nif-spec-warmup").start()

    def _on_gl_probe_done(self, ok: bool):
        """UI thread: the GL probe answered. Warm composition up only while it
        is still free to do so - see _start_gl_warmup."""
        self._gl_probing = False
        if not ok or self._gl_warmup is not None:
            return
        if self.isVisible() and self.windowOpacity() > 0.01:
            return                    # on screen already: the flash would show
        try:
            from PySide6.QtOpenGLWidgets import QOpenGLWidget
            warm = QOpenGLWidget(self)
            warm.setFixedSize(1, 1)
            warm.move(-4, -4)
            warm.setAttribute(Qt.WA_TransparentForMouseEvents)
            warm.lower()
            warm.show()
            self._gl_warmup = warm
        except Exception:
            pass

    def _warn_handler_load_failures(self):
        """If any game handler failed to load during discovery, log each one and
        show a single toast. Without this a broken handler just removes its game
        from the list with no trace, which reads as "my game/mods got deleted"
        (mods are safe - they live in ~/.config and Profiles/, untouched)."""
        try:
            from Utils.game_loader import get_load_failures
            failures = get_load_failures()
        except Exception:
            return
        if not failures:
            return
        for rel, reason in failures:
            self._append_log(f"[game] handler failed to load: {rel} - {reason}")
        n = len(failures)
        first = failures[0][0]
        detail = first if n == 1 else self.tr("{0} and {1} more").format(first, n - 1)
        self._notify(
            self.tr("A game handler failed to load ({0}). Affected games are "
                    "hidden, but your mods are safe - see the log.").format(detail),
            state="warning",
            sticky=True,
        )

    def _check_flatpak_i386(self):
        """Flatpak startup self-heal: install the 32-bit compat extensions when
        the sandbox lacks them (bundle installs skip the manifest's related
        refs). Without them every in-sandbox Proton/wine run - dtkit-patch,
        vcredist, wizard exes - dies with "/lib/ld-linux.so.2: could not open".
        """
        from Utils.flatpak_i386 import i386_support_missing
        if not i386_support_missing():
            return
        from Utils.ui_config import load_suppress_i386_warning
        if load_suppress_i386_warning():
            self._append_log("[flatpak] 32-bit support missing but the warning "
                             "is suppressed - skipping self-heal.")
            return
        self._append_log("[flatpak] 32-bit support missing (Compat.i386 "
                         "extension not installed) - installing from Flathub …")
        self._i386_toast = self._notify(
            self.tr("Installing 32-bit support (needed to run Windows tools) …"),
            sticky=True,
        )

        def _run():
            from gui_qt.safe_emit import safe_emit
            from Utils.flatpak_i386 import install_i386_extensions
            from Utils.app_log import app_log
            ok = install_i386_extensions(log_fn=lambda m: app_log(f"[flatpak] {m}"))
            safe_emit(self._i386_repair_done, ok)

        import threading
        threading.Thread(target=_run, daemon=True).start()

    def _on_i386_repair_done(self, ok: bool):
        """UI thread: 32-bit extension install finished - swap the toast."""
        toast, self._i386_toast = self._i386_toast, None
        if toast is not None:
            try:
                toast.dismiss()
            except Exception:
                pass
        if ok:
            # Extensions mount when the sandbox is (re)created, not live.
            self._notify(
                self.tr("32-bit support installed - restart the app before "
                        "running Windows tools."),
                state="success", sticky=True,
            )
        else:
            from Utils.flatpak_i386 import MANUAL_INSTALL_CMD
            self._append_log(f"[flatpak] install manually: {MANUAL_INSTALL_CMD}")
            from gui_qt.confirm_overlay import ConfirmOverlay

            def _done(dont_show_again: bool):
                if dont_show_again:
                    from Utils.ui_config import save_suppress_i386_warning
                    save_suppress_i386_warning(True)
                    self._append_log("[flatpak] 32-bit support warning suppressed "
                                     "at the user's request.")

            ConfirmOverlay.show_over(
                self,
                self.tr("32-bit support could not be installed"),
                self.tr(
                    "Amethyst could not install 32-bit support automatically. "
                    "Windows tools (and some games) may fail to run until it is "
                    "installed. Run this on a terminal, then restart the app:\n\n"
                    "{0}"
                ).format(MANUAL_INSTALL_CMD),
                _done,
                confirm_label=self.tr("Don't show again"),
                cancel_label=self.tr("OK"),
                danger=False,
            )

    def _populate_selectors(self):
        """Fill the game/profile selectors from the current GameState."""
        gs = self._gs
        if gs.game_name:
            self._game_selector.set_items(gs.game_names, current=gs.game_name)
        else:
            # No games configured - the button invites the user to add one
            # instead of showing stale placeholder names.
            self._game_selector.set_items([], current=self.tr("Add game"))
        self._refresh_play_selector()
        # The 'Edit custom game…' entry depends on the active game.
        self._refresh_game_actions()
        profs = gs.profiles()
        if profs:
            self._set_profile_selector_items(profs, current=gs.profile)
        self._refresh_profile_actions()

    # ---------------------------------------------------------- header row
    def _build_header_row(self) -> QWidget:
        """Full-width top bar (selectors + action buttons) - spans the whole
        window so the buttons have room. The Play section now lives at the top
        of the plugins panel (see _build_body_row), not here."""
        return self._left_header()

    # ---------------------------------------------------------- body row
    def _build_body_row(self) -> QWidget:
        # Each side is a self-contained column: the modlist column owns its tool
        # footer (buttons + search), and the plugins column owns the Play bar on
        # top plus the plugins tool footer at the bottom. The footers live INSIDE
        # the panels (not a separate window-wide row) so they move/resize with
        # their panel. The splitter divides the two columns.
        right_col = QWidget()
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(0)
        play = self._play_bar()
        self._play_bar_widget = play
        # Build the plugins panel FIRST - it creates the sub-tab views (incl.
        # _text_files_view) that the footers below reference.
        plugins_body = self._build_plugins()
        # The plugins column footer is a stack: it swaps to the active sub-tab's
        # tools (Plugins tools ↔ Mod Files Pack/Unpack + search).
        # _CurrentPageStack (not a plain QStackedWidget): sizes to the visible
        # footer only, so the Downloads footer wrapping its buttons to a second
        # row at min width doesn't add a blank row to the Plugins/etc. footers.
        self._plugin_footer_stack = _CurrentPageStack()
        for _page in (self._plugins_footer(), self._mod_files_footer(),
                      self._data_footer(), self._downloads_footer(),
                      self._text_files_footer(), self._overrides_footer(),
                      self._saves_footer()):
            self._enable_height_for_width(_page)
            self._plugin_footer_stack.addWidget(_page)
        # The stack must also report height via heightForWidth so its parent
        # (plugins_panel's QVBoxLayout) fits it to the visible page's wrapped
        # height at the actual width, not the inflated min-width sizeHint.
        self._enable_height_for_width(self._plugin_footer_stack)
        # Re-fit the stack (and thus the panel) whenever the visible page swaps.
        self._plugin_footer_stack.currentChanged.connect(
            lambda _i: self._plugin_footer_stack.clamp_to_current())
        # The whole plugins panel (Play bar + sub-tab strip + content + footer)
        # lives in a stack so a panel-scoped tab (Change Version) can take it
        # over entirely - including the Play bar's row, giving scoped tabs the
        # full column height. Page 0 = Play bar + plugins panel + footer.
        plugins_panel = QWidget()
        pp = QVBoxLayout(plugins_panel)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.setSpacing(0)
        pp.addWidget(play)
        pp.addWidget(plugins_body, 1)
        pp.addWidget(self._plugin_footer_stack)
        # Inline userlist edit bars (hidden until opened from the plugins
        # context menu - Tk parity: rows 5/6 at the bottom of the plugins tab).
        from gui_qt.userlist_bars import UserlistBar, GroupBar
        self._ul_bar = UserlistBar(self._userlist_path,
                                   self._on_userlist_bar_saved)
        self._grp_bar = GroupBar(self._userlist_path,
                                 self._on_userlist_bar_saved)
        pp.addWidget(self._ul_bar)
        pp.addWidget(self._grp_bar)
        self._plugins_panel_stack = QStackedWidget()
        self._plugins_panel_stack.addWidget(plugins_panel)               # page 0
        rc.addWidget(self._plugins_panel_stack, 1)
        self._right_col = right_col

        # The modlist column lives in a stack so a panel-scoped tab (e.g. an
        # image preview) can take over JUST the modlist region while the plugins
        # panel (with the Mod Files tree) stays live. Page 0 = the real modlist.
        self._modlist_panel_stack = QStackedWidget()
        self._modlist_panel_stack.addWidget(self._build_modlist_area())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._modlist_panel_stack)
        split.addWidget(right_col)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        # Wider handle so a panel collapsed to one edge still has an easy-to-grab
        # grip (the default 1px line is nearly invisible / hard to click).
        split.setHandleWidth(8)
        split.setChildrenCollapsible(True)
        # Last-session splitter position, else the default split. Saved sizes
        # are absolute pixels at the saved window size; QSplitter rescales them
        # proportionally when the restored window size differs.
        saved_sizes = None
        try:
            from Utils.ui_config import load_qt_window_state
            saved_sizes = load_qt_window_state().get("body_split")
        except Exception:
            saved_sizes = None
        split.setSizes(saved_sizes or [620, 480])
        split.splitterMoved.connect(
            lambda *_: self._schedule_window_state_save())
        self._body_split = split
        self._wire_cross_panel()
        return split

    def _wire_cross_panel(self):
        """Connect modlist ↔ plugins selection so picking a mod highlights its
        plugins (+ conflict-tinted ones) and picking a plugin highlights its
        owning mod (Tk parity)."""
        self._conflict_data = None
        self._conflict_maps_current = False
        mv, pv = self._modlist_view, self._plugin_view
        mv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_mod_selection_changed())
        pv.selectionModel().selectionChanged.connect(
            lambda *_: self._on_plugin_selection_changed())

    def _suppress_xpanel(self) -> bool:
        return getattr(self, "_xpanel_busy", False)

    def _on_mod_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            names = mv.selected_mod_names()
            # Change Version tab (if visible) follows the selection.
            self._follow_selection_change_version()
            if self._req_tab_active():
                # View Requirements mode: conflict highlights are disabled -
                # the panel retargets to the selection (multiple mods pool
                # their requirements together; a selected separator contributes
                # its whole block, matching selected_mod_names) and its
                # on_data_changed callback applies the purple/blue tints once
                # the deferred rebuild ends.
                self._view_requirements_view.show_mods(names)
                pv.clearSelection()
                self._update_mod_files_selection(names)
                return
            # Modlist rows: green/red tint on ALL conflict partners (loose+BSA).
            higher, lower = mv.conflict_partners(names)
            # Tk quirk: the [Overwrite] band lights GREEN when it wins over the
            # selection (so the user sees Overwrite is active), even though a
            # normal winning mod would be red. Flip it from lower→higher.
            from Utils.filemap import OVERWRITE_NAME
            if OVERWRITE_NAME in lower:
                lower = lower - {OVERWRITE_NAME}
                higher = higher | {OVERWRITE_NAME}
            mv.model().set_highlights(higher=higher, lower=lower)
            # Plugins panel: orange the selected mods' plugins. Green/red is
            # applied ONLY for BSA conflicts (Tk parity) - loose-file conflicts
            # do NOT colour plugins - and only to plugins that own a BSA.
            cd = self._conflict_data
            owner = cd.plugin_owner if cd else {}
            bsa_higher, bsa_lower = mv.bsa_conflict_partners(names)
            pv.set_highlight_from_mods(
                names, bsa_higher, bsa_lower, owner,
                bsa_index_path=self._gs.bsa_index_path())
            # Picking a mod clears any plugin selection (mutual exclusivity).
            pv.clearSelection()
            # Feed the Mod Files tab the single selected mod (real mods only).
            self._update_mod_files_selection(names)
        finally:
            self._xpanel_busy = False

    def _update_mod_files_selection(self, _names):
        """Show the single selected mod in the Mod Files tab. A separator or a
        multi-selection shows nothing (the separator overview is a later step)."""
        mv = getattr(self, "_mod_files_view", None)
        if mv is None:
            return
        rows = self._modlist_view.selectionModel().selectedRows()
        if len(rows) != 1:
            mv.show_mod(None)
            return
        e = self._modlist_model.entry(rows[0].row())
        from gui_qt.modlist_model import _BOUNDARY_NAMES
        # The synthetic Overwrite / Root Folder rows have a real on-disk folder,
        # so they show their files in the Mod Files tab like a mod. Only true
        # (user) separators show nothing.
        if e.is_separator and e.name not in _BOUNDARY_NAMES:
            mv.show_mod(None)
        else:
            mv.show_mod(e.name)

    def _open_settings_tab(self):
        """Open the Settings tab scoped over the MODLIST panel (like the image
        preview / text editor): it shows in the modlist region (in the shared top
        tab bar) while the plugins panel and the rest of the UI stay live.
        Re-clicking the gear focuses the existing tab."""
        from gui_qt.settings_view import SettingsView
        if self._tabs.has_key("settings"):
            self._tabs.focus_key("settings")
            return
        view = SettingsView(self)
        self._tabs.open_scoped_tab(
            view, self.tr("Settings"), self._modlist_panel_stack, key="settings")

    def _open_install_name_patterns_tab(self):
        """Open the custom install-name rules editor scoped over the MODLIST
        panel (like Settings). Re-opening focuses the existing tab."""
        from gui_qt.install_name_patterns_view import InstallNamePatternsView
        if self._tabs.has_key("install_name_patterns"):
            self._tabs.focus_key("install_name_patterns")
            return
        view = InstallNamePatternsView(self)
        self._tabs.open_scoped_tab(
            view, self.tr("Install-name Rules"),
            self._modlist_panel_stack, key="install_name_patterns")

    def _open_env_vars_tab(self):
        """Open the app environment-variable editor scoped over the MODLIST
        panel (like Settings). Re-opening focuses the existing tab."""
        from gui_qt.env_vars_view import EnvVarsView
        if self._tabs.has_key("env_vars"):
            self._tabs.focus_key("env_vars")
            return
        view = EnvVarsView(self)
        self._tabs.open_scoped_tab(
            view, self.tr("Environment Variables"),
            self._modlist_panel_stack, key="env_vars")

    def _open_theme_editor_tab(self):
        """Open the Theme Editor as a full-screen tab (its own key). Editing a
        theme touches the whole window, so unlike Settings it takes over the
        entire UI rather than a single panel. Re-opening focuses the existing
        tab."""
        from gui_qt.theme_editor_view import ThemeEditorView
        if self._tabs.has_key("theme_editor"):
            self._tabs.focus_key("theme_editor")
            return
        view = ThemeEditorView(self)
        self._tabs.open_tab(view, self.tr("Theme Editor"), key="theme_editor")

    def _open_image_preview_tab(self, path, rel_str):
        """Open an image/.dds preview as a MODLIST-PANEL-SCOPED tab: it shows in
        the modlist region (in the shared top tab bar) while the Mod Files tree
        in the plugins panel stays live. Reuses one preview tab - browsing to a
        new image swaps it in place (Tk parity)."""
        from pathlib import Path as _P
        from gui_qt.image_preview import ImagePreview
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        existing = getattr(self, "_image_preview_widget", None)
        if existing is not None and self._tabs.has_key("mf_image_preview"):
            existing.set_image(_P(path), name)
            self._tabs.focus_key("mf_image_preview")
            self._tabs.set_tab_title("mf_image_preview", name)
            return
        widget = ImagePreview(_P(path), name)
        self._image_preview_widget = widget
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="mf_image_preview")

    def _open_nif_preview_tab(self, path, rel_str):
        """Open a .nif in the 3D viewer as a modlist-panel-scoped tab (reused)."""
        from pathlib import Path as _P
        from gui_qt.nif_preview import NifPreview
        self._nif_open_gen += 1
        self._nif_archive_jobs.discard_pending()
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        roots = self._nif_texture_roots(_P(path))
        resolver = self._nif_asset_resolver()
        # A resolver already covers every enabled mod and vanilla archive.
        # Supplement it only with this mesh's own mod directory: that keeps
        # sibling texture archives working for disabled mods without the old
        # enumeration/indexing of every staged mod.
        if resolver is not None:
            data_dir = self._nif_game_data_dir()
            archives = ([roots[0]] if roots and roots[0] != data_dir else [])
        else:
            archives = self._nif_archive_roots(_P(path))
        existing = getattr(self, "_nif_preview_widget", None)
        if existing is None or not self._tabs.has_key("mf_nif_preview"):
            existing = NifPreview(None, name, resolver=resolver,
                                  log_fn=self._append_log)
            self._nif_preview_widget = existing
            self._tabs.open_scoped_tab(
                existing, name, self._modlist_panel_stack, key="mf_nif_preview")
        else:
            self._tabs.focus_key("mf_nif_preview")
            self._tabs.set_tab_title("mf_nif_preview", name)

        def _load(override=None, keep_view=False):
            existing.set_nif(_P(path), name, roots, archives, resolver,
                             override, keep_view)

        # A texture swap keeps the camera; only opening the mesh frames it.
        self._nif_texture_controller(resolver).arm(
            str(path), lambda ov: _load(ov, keep_view=True))
        _load()

    def _nif_texture_roots(self, path):
        """Loose-texture roots: mod's own staging root, then the data folder."""
        from gui_qt.nif_preview import default_texture_roots
        roots = default_texture_roots(path)
        data = self._nif_game_data_dir()
        if data is not None and data not in roots:
            roots.append(data)
        return roots

    def _open_nif_from_archive(self, archive_path, inner_path):
        """Preview a .nif stored inside a BSA/BA2 without unpacking it."""
        from pathlib import Path as _P
        from gui_qt.nif_preview import NifPreview
        archive = _P(archive_path)
        name = inner_path.replace("\\", "/").rsplit("/", 1)[-1]
        from Utils.archive_lookup import ArchiveLookup, find_archives
        from gui_qt.nif_preview import ASSET_PREFIXES
        archives = ArchiveLookup(find_archives([archive.parent]),
                                 keep_prefix=ASSET_PREFIXES)
        resolver = self._nif_asset_resolver()
        existing = getattr(self, "_nif_preview_widget", None)
        if existing is None or not self._tabs.has_key("mf_nif_preview"):
            existing = NifPreview(None, name, resolver=resolver,
                                  log_fn=self._append_log)
            self._nif_preview_widget = existing
            self._tabs.open_scoped_tab(
                existing, name, self._modlist_panel_stack, key="mf_nif_preview")
        else:
            self._tabs.focus_key("mf_nif_preview")
            self._tabs.set_tab_title("mf_nif_preview", name)
        existing.cancel_load()
        existing.set_title(name, self.tr("Loading…"))

        self._nif_open_gen += 1
        gen = self._nif_open_gen
        source_token = f"{archive}::{inner_path}"
        controller = self._nif_texture_controller(resolver)
        # Invalidate the previous mesh's provider scan and hide its picker
        # while this archive member is still being decompressed.
        controller.arm(source_token, lambda _override: None)
        context = (archive, inner_path, name, resolver, archives, existing)

        def worker():
            error = ""
            try:
                data = self._read_archive_member(archive, inner_path)
            except Exception as exc:                     # noqa: BLE001
                data = None
                error = str(exc)
            from gui_qt.safe_emit import safe_emit
            safe_emit(self._nif_archive_ready, gen, data, (context, error))

        self._nif_archive_jobs.submit(worker)

    def _on_nif_archive_ready(self, gen: int, data, payload):
        """UI-thread continuation for an archive member read/decompression."""
        if gen != self._nif_open_gen:
            return
        context, error = payload
        archive, inner_path, name, resolver, archives, existing = context
        if existing is not getattr(self, "_nif_preview_widget", None) \
                or not self._tabs.has_key("mf_nif_preview"):
            return
        if not data:
            message = self.tr("Could not read {0} from {1}").format(
                inner_path, archive.name)
            if error:
                message += f": {error}"
            self._append_log(message)
            existing.set_title(name, message)
            existing.clear(message)
            return

        def _load(override=None, keep_view=False):
            # inner_path lets plugin TXST overrides apply; the ESPs sit next
            # to the archive in the mod folder.
            existing.set_nif_data(data, name, resolver, archives, override,
                                  keep_view, mesh_rel=inner_path,
                                  plugin_dirs=[archive.parent])

        self._nif_texture_controller(resolver).arm(
            f"{archive}::{inner_path}", lambda ov: _load(ov, keep_view=True))
        _load()

    def _nif_texture_controller(self, resolver):
        """Texture-source picker (shared with the NIF Viewer tab), built once
        per preview widget and re-pointed at the current game/profile."""
        widget = self._nif_preview_widget
        ctl = getattr(widget, "_tex_source_ctl", None)
        if ctl is None:
            from gui_qt.nif_texture_sources import TextureSourceController
            ctl = TextureSourceController(widget, self._append_log)
            widget._tex_source_ctl = ctl
        ctl.configure(self._gs.staging_dir(), self._gs.modlist_path(),
                      self._nif_game_data_dir(), resolver)
        return ctl

    def _read_archive_member(self, archive, inner_path):
        """Return one member's bytes from a BSA/BA2, or None if absent."""
        from Utils.mesh_catalog import read_archive_member
        return read_archive_member(archive, inner_path)

    def _nif_asset_resolver(self):
        """Resolver for what the GAME would load; None without a profile."""
        try:
            from Utils.asset_resolver import AssetResolver
            from gui_qt.nif_preview import ASSET_PREFIXES
            staging = self._gs.staging_dir()
            if staging is None:
                return None
            return AssetResolver(
                staging_dir=staging,
                modlist_path=self._gs.modlist_path(),
                profile_dir=self._gs.profile_dir(),
                data_dir=self._nif_game_data_dir(),
                game=getattr(self._gs, "game", None),
                keep_prefix=ASSET_PREFIXES,
            )
        except Exception:
            return None

    def _nif_archive_roots(self, path):
        """Archive roots: every staged mod (own first), then the data folder."""
        from gui_qt.nif_preview import default_archive_roots
        roots = default_archive_roots(path)
        data = self._nif_game_data_dir()
        if data is not None and data not in roots:
            roots.append(data)
        return roots

    def _nif_game_data_dir(self):
        """The configured game's data folder, or None when unavailable."""
        try:
            game = getattr(self._gs, "game", None)
            data = game.get_mod_data_path() if game is not None else None
        except Exception:
            return None
        if not data:
            return None
        from pathlib import Path as _P
        d = _P(data)
        return d if d.is_dir() else None

    def _open_bsa_preview_tab(self, path, rel_str):
        """Open a BSA/BA2 archive's contents as a MODLIST-PANEL-SCOPED tab: it
        shows in the modlist region (in the shared top tab bar) while the Mod
        Files tree in the plugins panel stays live. Reuses one preview tab -
        clicking another archive swaps it in place. Replaces the old Tk Archive
        tab; the TOC is read without decompressing any file data."""
        from pathlib import Path as _P
        from gui_qt.bsa_preview import BsaPreview
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        existing = getattr(self, "_bsa_preview_widget", None)
        if existing is not None and self._tabs.has_key("mf_bsa_preview"):
            existing.set_archive(_P(path), name)
            self._tabs.focus_key("mf_bsa_preview")
            self._tabs.set_tab_title("mf_bsa_preview", name)
            return
        widget = BsaPreview(_P(path), name,
                            conflict_fn=self._bsa_preview_conflicts)
        widget.on_open_nif = self._open_nif_from_archive
        widget.close_requested.connect(
            lambda: self._tabs.close_tab("mf_bsa_preview"))
        self._bsa_preview_widget = widget
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key="mf_bsa_preview")

    def _bsa_preview_conflicts(self, archive_path):
        """Per-file conflict codes for the archive preview: {contained_path:
        +1 (this mod's copy wins) / -1 (loses)} judged for the archive's
        owning mod against every enabled mod's archives - the same winner map
        the modlist icons and Show Conflicts tab use. Runs on the preview's
        daemon thread. Returns {} for unowned paths (overwrite/), disabled
        mods, or any failure - the preview just shows no tints then."""
        try:
            from pathlib import Path as _P
            g = self._gs.game
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            if g is None or staging is None or ml is None or not ml.is_file():
                return {}
            rel = _P(archive_path).resolve().relative_to(staging.resolve())
            mod_name = rel.parts[0]

            from Utils.bsa_filemap import read_bsa_index, compute_bsa_winner_map
            from Utils.modlist import read_modlist
            from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
            index = read_bsa_index(staging.parent / "bsa_index.bin") or {}
            enabled = [e for e in read_modlist(ml)
                       if not e.is_separator and e.enabled]
            if mod_name not in {e.name for e in enabled}:
                return {}
            prio = [e.name for e in reversed(enabled)]
            exts = frozenset(
                getattr(g, "archive_extensions", frozenset()) or frozenset())
            if getattr(g, "archive_plugin_ordering", True):
                from Utils.plugins import read_loadorder
                pdir = self._gs.profile_dir()
                plugin_order = (read_loadorder(pdir / "loadorder.txt")
                                if pdir is not None else None)
                plugin_exts = frozenset(
                    getattr(g, "plugin_extensions", []) or [])
            else:
                plugin_order = None
                plugin_exts = None
            is_ue = bool(exts & UE_ARCHIVE_EXTENSIONS)
            winner, losers = compute_bsa_winner_map(
                index, prio, plugin_order or None, plugin_exts or None,
                staging.parent / "modindex.bin", is_ue)
            codes = {}
            for fp, lose_list in losers.items():
                if winner.get(fp) == mod_name:
                    codes[fp] = 1
                elif mod_name in lose_list:
                    codes[fp] = -1
            if not is_ue:
                my_bsa_paths = {
                    fp
                    for _bsa, _mt, paths in index.get(mod_name, [])
                    for fp in paths
                }
                if my_bsa_paths:
                    # Prefer filemap.txt (resolved deploy map) so Mod Files ▸
                    # Disable exclusions count - modindex.bin still lists
                    # excluded files. Same preference as build_bsa_conflicts.
                    loose_resolved = False
                    try:
                        fm_text = (staging.parent / "filemap.txt").read_text(
                            encoding="utf-8")
                    except OSError:
                        fm_text = None
                    if fm_text is not None:
                        loose_resolved = True
                        prio_set = set(prio)
                        for line in fm_text.splitlines():
                            if "\t" not in line:
                                continue
                            rel_str, mod = line.split("\t", 1)
                            if mod == mod_name or mod not in prio_set:
                                continue
                            rel_key = rel_str.replace("\\", "/").lower()
                            if rel_key in my_bsa_paths:
                                # Any loose file at a BSA path beats the BSA
                                # (engine loads BSAs first, then loose on top).
                                codes[rel_key] = -1
                    if not loose_resolved:
                        from Utils.filemap import read_mod_index
                        loose_index = read_mod_index(
                            staging.parent / "modindex.bin") or {}
                        for cand in prio:
                            if cand == mod_name:
                                continue
                            entry = loose_index.get(cand)
                            if not entry:
                                continue
                            normal, _root = entry
                            for rel_key in normal:
                                if rel_key in my_bsa_paths:
                                    # A higher-priority loose file wins outright; a
                                    # lower-priority one still beats the BSA (engine
                                    # loads BSAs first, then loose on top).
                                    codes[rel_key] = -1
            return codes
        except Exception:
            return {}

    def _open_text_editor_tab(self, path, rel_str, find_kw=None):
        """Open a text file in a save-capable editor as a MODLIST-PANEL-SCOPED tab
        (the other panels stay live). Each distinct file gets its OWN tab so
        several can be open at once; re-opening the same file focuses its tab.
        The tab title gets a '*' while there are unsaved edits.
        *find_kw* (the active content-search keyword) is pre-highlighted."""
        from pathlib import Path as _P
        from gui_qt.text_editor import TextEditor
        p = _P(path)
        name = rel_str.replace("\\", "/").rsplit("/", 1)[-1]
        key = "tf_text_editor:" + str(p.resolve() if p.exists() else p)
        editors = getattr(self, "_text_editor_widgets", None)
        if editors is None:
            editors = self._text_editor_widgets = {}
        # Same file already open → just focus it (and re-apply the search).
        if key in editors and self._tabs.has_key(key):
            widget = editors[key]
            if find_kw:
                widget.find_text(find_kw)
            self._tabs.focus_key(key)
            return
        widget = TextEditor(p, name)
        widget.setProperty("_te_key", key)
        editors[key] = widget
        widget.dirty_changed.connect(
            lambda dirty, w=widget: self._on_text_editor_dirty(w, dirty))
        widget.saved.connect(self._on_text_editor_saved)
        widget.destroyed.connect(lambda *_a, k=key: self._text_editor_widgets.pop(k, None))
        self._tabs.open_scoped_tab(
            widget, name, self._modlist_panel_stack, key=key)
        if find_kw:
            widget.find_text(find_kw)

    def _on_text_editor_dirty(self, widget, dirty):
        key = widget.property("_te_key")
        if key and self._tabs.has_key(key):
            self._tabs.set_tab_title(
                key, (widget.name + " *") if dirty else widget.name)

    def _on_text_editor_saved(self):
        # File content changed on disk → the Text Files content search may shift.
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        self._notify(self.tr("Saved"), "success")

    def _on_mod_files_changed(self):
        """A Top Level / Root / Disable edit changed deploy state - force a full
        index rescan (strip prefixes apply at scan time) + rebuild conflicts."""
        # Instant feedback: update the modlist "modified in Mod Files" eye flag
        # now (the async rescan below refreshes the rest a moment later).
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_modified_mf(self._build_modified_mf_mods())
        # Exclusion edits change the Overrides tab's checkbox states too (a
        # pak disabled in Mod Files shows unchecked there). Re-reads from disk,
        # so an Overrides-originated toggle just refreshes to the same state.
        if hasattr(self, "_overrides_view"):
            self._overrides_view.mark_dirty()
        self._rebuild_conflicts_async(rescan_index=True)

    def _on_plugin_selection_changed(self):
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv, pv = self._modlist_view, self._plugin_view
            owner = (self._conflict_data.plugin_owner
                     if self._conflict_data else {})
            mods = pv.selected_owner_mods(owner)
            # Plugin selected → orange its owning mod, clear mod conflict tint.
            # Suppressed in View Requirements mode (purple/blue tints own the
            # modlist while that tab is open).
            if not self._req_tab_active():
                mv.set_highlighted_mods(mods)
            mv.clearSelection()
            # Green-tick the selected plugin's masters in the plugin marker
            # strip (Tk parity).
            pv.set_master_highlight(self._selected_plugin_masters())
        finally:
            self._xpanel_busy = False
        # Follow the selection in the open Plugin Rules tab (Tk parity:
        # _on_plugin_row_selected_cb → set_selected_plugin).
        if self._tabs.has_key("plugin_rules"):
            rules_view = getattr(self, "_plugin_rules_view", None)
            sel = self._plugin_view.selectionModel().selectedRows()
            if rules_view is not None and sel:
                rules_view.set_selected_plugin(
                    self._plugin_model.row(sel[0].row()).name)

    def _selected_plugin_masters(self) -> set:
        """Lowercase master filenames of the currently-selected plugin(s), read
        from each plugin's resolved on-disk path. Empty if nothing selected or
        paths are unavailable."""
        pv = self._plugin_view
        rows = pv.selectionModel().selectedRows()
        if not rows:
            return set()
        paths = getattr(self, "_plugin_paths", None)
        if not paths:
            return set()
        from Utils.plugin_parser import read_masters
        masters: set = set()
        for idx in rows:
            r = self._plugin_model.row(idx.row())
            p = paths.get(r.name.lower())
            if p is None:
                continue
            try:
                masters.update(m.lower() for m in read_masters(p))
            except Exception:
                pass
        return masters

    def _on_data_select_mod(self, mod):
        """A Data-tab file row was selected → orange-highlight its winning mod in
        the modlist (Tk parity); a folder row clears it."""
        if self._suppress_xpanel():
            return
        self._xpanel_busy = True
        try:
            mv = self._modlist_view
            mv.set_highlighted_mods({mod} if mod else set())
            mv.clearSelection()
        finally:
            self._xpanel_busy = False

    # ---------------------------------------------------------- panel footers
    @staticmethod
    def _enable_height_for_width(w: QWidget) -> None:
        """Make a footer widget report its height via heightForWidth (the wrapped
        FlowLayout height at the *actual* width) rather than its inflated
        sizeHint (which Qt evaluates at the widget's MINIMUM width, i.e. every
        button on its own row). Without this, a footer whose buttons only wrap
        at narrow widths still reserves the fully-wrapped tall height, leaving a
        blank gap above the search box."""
        pol = w.sizePolicy()
        pol.setHeightForWidth(True)
        w.setSizePolicy(pol)

    def _modlist_footer(self) -> QWidget:
        """Buttons row + search box - lives at the bottom of the modlist panel."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # FlowLayout so longer translated labels wrap to a second row instead of
        # overflowing the panel (see gui_qt/flow_layout.py). Centred so the row
        # stays aligned with the middle of the modlist panel at any width.
        btns = FlowLayout(spacing=4, center=True)
        # label -> handler ("" = no-op stub, needs a dialog/auth - wired later).
        _handlers = {
            "Expand all": self._on_toggle_collapse_all,
            "Enable all": self._on_toggle_enable_all,
            "Refresh Modlist": self._on_refresh_modlist,
            "Check Updates": self._on_check_updates,
            "Restore backup": self._open_restore_backup_tab,
        }
        # (canonical key, translated display). Key drives handler lookup + the
        # == comparisons below; display is the visible button text.
        self._modlist_footer_btns: list[QToolButton] = []
        for label, disp in [("Expand all", self.tr("Expand all")),
                            ("Enable all", self.tr("Enable all")),
                            ("Check Updates", self.tr("Check Updates")),
                            ("Restore backup", self.tr("Restore backup")),
                            ("Refresh Modlist", self.tr("Refresh Modlist"))]:
            b = self._text_button(disp, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            if label in _handlers:
                b.clicked.connect(_handlers[label])
            if label == "Expand all":
                self._expand_all_btn = b
            elif label == "Enable all":
                self._enable_all_btn = b
            elif label == "Check Updates":
                self._check_updates_btn = b
            btns.addWidget(b)
            self._modlist_footer_btns.append(b)
        v.addLayout(btns)

        # Search row: an enabled/total count label (blue outline) to the left of
        # a full-width search box. The count label replaces the old Enabled /
        # Disabled stat pill row.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("ModlistCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#ModlistCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._modlist_count = count
        search_row.addWidget(count)

        # Filters button - blue, to the left of the search icon (mirrors the
        # plugins footer). Toggles the modlist filter side-panel.
        self._modlist_filters_btn = self._filters_button()
        self._modlist_filters_btn.clicked.connect(self._toggle_modlist_filters)
        self._modlist_footer_btns.append(self._modlist_filters_btn)
        search_row.addWidget(self._modlist_filters_btn)

        # Search box spans the remaining footer width. (It used to be pinned to
        # the summed button-row width so its edge lined up with the last button;
        # that assumed a single, non-wrapping row - with the FlowLayout the
        # buttons can wrap, so a fixed cap would over/under-shoot. Full width is
        # simpler and robust across languages.)
        search_row.addWidget(
            self._search_icon_label(self._modlist_search_tooltip()))

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search mods… (try !update, !fomod, !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_modlist_search)
        self._modlist_search = search
        # A search query narrows the list just like a panel filter, so it
        # lights the Filters button too (see _sync_filters_btn).
        search.textChanged.connect(
            lambda _t, a="_modlist_filters_btn": self._sync_filters_btn(a))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._enable_height_for_width(bar)
        return bar

    def _plugins_footer(self) -> QWidget:
        """Colored tool buttons + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        _made = {}
        self._plugin_footer_btns: list = []
        for label, disp, key in [
            ("Sort Plugins", self.tr("Sort Plugins"), "BTN_SUCCESS"),
            ("Refresh Plugins", self.tr("Refresh Plugins"), "BTN_INFO"),
            ("Groups", self.tr("Groups"), "BTN_INFO"),
            ("Plugin Rules", self.tr("Plugin Rules"), "BTN_INFO"),
        ]:
            b = self._color_button(disp, _c(self._pal, key), compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            btns.addWidget(b)
            _made[label] = b
            self._plugin_footer_btns.append(b)
        v.addLayout(btns)
        self._plugin_sort_btn = _made["Sort Plugins"]
        self._plugin_refresh_btn = _made["Refresh Plugins"]
        self._plugin_groups_btn = _made["Groups"]
        self._plugin_rules_btn = _made["Plugin Rules"]
        self._plugin_filters_btn = self._filters_button()
        self._plugin_footer_btns.append(self._plugin_filters_btn)
        self._plugin_sort_btn.clicked.connect(self._on_sort_plugins)
        self._plugin_refresh_btn.clicked.connect(self._on_refresh_plugins)
        self._plugin_groups_btn.clicked.connect(self._open_plugin_groups_tab)
        self._plugin_rules_btn.clicked.connect(self._open_plugin_rules_tab)
        self._plugin_filters_btn.clicked.connect(self._toggle_plugin_filters)

        # Search row: a total / non-ESL count label (blue outline) to the left
        # of a full-width search box, mirroring the modlist footer.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("PluginCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#PluginCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._plugin_count = count
        search_row.addWidget(count)
        search_row.addWidget(self._plugin_filters_btn)
        search_row.addWidget(self._search_icon_label())

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search plugins…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_plugin_search)
        self._plugins_search = search
        search.textChanged.connect(
            lambda _t, a="_plugin_filters_btn": self._sync_filters_btn(a))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        return bar

    def _mod_files_footer(self) -> QWidget:
        """Pack/Unpack BSA + Filters buttons + search, shown under the plugins
        column when the Mod Files sub-tab is active (replaces the plugin tools)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._mf_pack_btn = self._color_button(
            self.tr("Pack BSA"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._mf_pack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_pack_btn.setEnabled(False)
        self._mf_pack_btn.clicked.connect(self._on_pack_bsa)
        self._mf_unpack_btn = self._color_button(
            self.tr("Unpack BSA"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._mf_unpack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_unpack_btn.setEnabled(False)
        self._mf_unpack_btn.clicked.connect(self._on_unpack_bsa)
        self._mf_filters_btn = self._filters_button()
        self._mf_filters_btn.clicked.connect(self._toggle_mod_files_filters)
        self._mf_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._mf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_expand_btn.clicked.connect(self._on_mf_expand_clicked)
        self._mf_reset_btn = self._text_button(self.tr("Reset"), compact=True)
        self._mf_reset_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_reset_btn.setEnabled(False)
        self._mf_reset_btn.setToolTip(
            self.tr("Undo this mod's Mod Files changes (Top Level, Root, "
                    "Disable). Mod files themselves are not touched."))
        self._mf_reset_btn.clicked.connect(self._on_mf_reset_clicked)
        self._equalize_button_widths(self._mf_pack_btn, self._mf_unpack_btn)
        btns.addWidget(self._mf_expand_btn)
        btns.addWidget(self._mf_reset_btn)
        btns.addWidget(self._mf_pack_btn)
        btns.addWidget(self._mf_unpack_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._mf_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(
            lambda t: self._mod_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._mod_files_search = search
        search.textChanged.connect(
            lambda _t, a="_mf_filters_btn": self._sync_filters_btn(a))
        return bar

    def _data_footer(self) -> QWidget:
        """Filters + Expand-all + search, shown under the plugins column when the
        Data sub-tab is active (no Pack/Unpack - Data is read-only)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._data_filters_btn = self._filters_button()
        self._data_filters_btn.clicked.connect(self._toggle_data_filters)
        self._data_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._data_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_expand_btn.clicked.connect(self._on_data_expand_clicked)
        btns.addWidget(self._data_expand_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._data_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._data_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._data_search = search
        search.textChanged.connect(
            lambda _t, a="_data_filters_btn": self._sync_filters_btn(a))
        return bar

    def _on_data_expand_clicked(self):
        expanded = self._data_view._toggle_expand_all()
        self._data_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                      else self.tr("⊞ Expand all"))

    def _overrides_footer(self) -> QWidget:
        """Refresh, shown under the plugins column when the Overrides sub-tab
        is active (BG3 override paks)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)
        btns = FlowLayout(spacing=4)
        refresh = self._text_button(self.tr("↻ Refresh"), compact=True)
        refresh.setFixedHeight(self._FOOT_BTN_H)
        refresh.clicked.connect(lambda: self._overrides_view.mark_dirty())
        btns.addWidget(refresh)
        v.addLayout(btns)
        return bar

    def _saves_footer(self) -> QWidget:
        """Refresh / Open folder + a location summary, Filters and search, shown
        under the plugins column when the Saves sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)
        btns = FlowLayout(spacing=4)
        refresh = self._text_button(self.tr("↻ Refresh"), compact=True)
        refresh.setFixedHeight(self._FOOT_BTN_H)
        refresh.clicked.connect(lambda: self._saves_view.mark_dirty())
        btns.addWidget(refresh)
        self._saves_open_btn = self._color_button(
            self.tr("Open folder"), _c(self._pal, "BTN_PURPLE"), compact=True)
        self._saves_open_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_open_btn.setEnabled(False)
        self._saves_open_btn.clicked.connect(lambda: self._saves_view.open_folder())
        btns.addWidget(self._saves_open_btn)
        # Export packs the save folder into a zip; Import puts one back (after
        # moving the current contents aside).
        self._saves_export_btn = self._color_button(
            self.tr("Export"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._saves_export_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_export_btn.setEnabled(False)
        self._saves_export_btn.clicked.connect(lambda: self._saves_view.start_export())
        btns.addWidget(self._saves_export_btn)
        self._saves_import_btn = self._color_button(
            self.tr("Import"), _c(self._pal, "BTN_WARN_ORANGE"), compact=True)
        self._saves_import_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_import_btn.setEnabled(False)
        self._saves_import_btn.clicked.connect(lambda: self._saves_view.start_import())
        btns.addWidget(self._saves_import_btn)
        self._saves_status = QLabel("")
        self._saves_status.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._saves_status)
        v.addLayout(btns)

        self._saves_filters_btn = self._filters_button()
        self._saves_filters_btn.clicked.connect(self._toggle_saves_filters)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._saves_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search saves… (try !.ess)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._saves_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._saves_search = search
        search.textChanged.connect(
            lambda _t, a="_saves_filters_btn": self._sync_filters_btn(a))

        self._saves_view.status_changed.connect(self._saves_status.setText)
        # Open folder falls back to the save location itself, and Export/Import
        # need a single unambiguous folder - so all three follow the view's
        # state rather than the selection alone (and never run mid-transfer).
        for sig in (self._saves_view.selection_changed,
                    self._saves_view.busy_changed):
            sig.connect(lambda *_: self._sync_saves_buttons())
        self._saves_view.notify.connect(self._on_saves_notify)
        return bar

    def _sync_saves_buttons(self):
        """Sync the Saves footer buttons to what the tab can currently do."""
        can = self._saves_view.can_transfer()
        self._saves_export_btn.setEnabled(can)
        self._saves_import_btn.setEnabled(can)
        self._saves_open_btn.setEnabled(self._saves_view.can_open())

    def _on_saves_notify(self, message: str, kind: str):
        """Progress/result line from a save export or import."""
        self._saves_status.setText(message)
        if kind in ("success", "error"):
            self._notify(message, kind)

    def _downloads_footer(self) -> QWidget:
        """Install Selected / Remove Selected / Locations / Filters + search,
        shown under the plugins column when the Downloads sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._dl_install_btn = self._color_button(
            self.tr("Install Selected"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        self._dl_install_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_install_btn.setEnabled(False)
        self._dl_install_btn.clicked.connect(
            lambda: self._downloads_view.install_selected())
        self._dl_move_btn = self._color_button(
            self.tr("Move Selected"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_move_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_move_btn.setEnabled(False)
        self._dl_move_btn.clicked.connect(self._on_downloads_move)
        self._dl_remove_btn = self._color_button(
            self.tr("Remove Selected"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._dl_remove_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_remove_btn.setEnabled(False)
        self._dl_remove_btn.clicked.connect(self._on_downloads_remove)
        self._dl_locations_btn = self._color_button(
            self.tr("Locations"), _c(self._pal, "BTN_INFO"), compact=True)
        self._dl_locations_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_locations_btn.clicked.connect(self._on_downloads_locations)
        self._dl_filters_btn = self._filters_button()
        self._dl_filters_btn.clicked.connect(self._toggle_downloads_filters)
        self._equalize_button_widths(
            self._dl_install_btn, self._dl_move_btn, self._dl_remove_btn)
        btns.addWidget(self._dl_install_btn)
        btns.addWidget(self._dl_move_btn)
        btns.addWidget(self._dl_remove_btn)
        btns.addWidget(self._dl_locations_btn)
        v.addLayout(btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._dl_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search downloads…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._downloads_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._dl_search = search
        search.textChanged.connect(
            lambda _t, a="_dl_filters_btn": self._sync_filters_btn(a))
        return bar

    def _update_downloads_footer(self):
        n = self._downloads_view.checked_count()
        for attr, label in (("_dl_install_btn", self.tr("Install Selected")),
                            ("_dl_move_btn", self.tr("Move Selected")),
                            ("_dl_remove_btn", self.tr("Remove Selected"))):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(self.tr("{0} ({1})").format(label, n) if n else label)

    def _on_downloads_locations(self):
        from gui_qt.download_locations_overlay import DownloadLocationsOverlay
        DownloadLocationsOverlay.show_over(
            self,
            lambda saved: self._downloads_view.refresh() if saved else None)

    def _on_downloads_remove(self):
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        names = [Path(p).name for p in paths]

        def _confirmed(ok):
            if not ok:
                return
            removed = 0
            for p in paths:
                try:
                    Path(p).unlink()
                    removed += 1
                except OSError as exc:
                    print(f"[gui_qt] remove failed: {p}: {exc}", flush=True)
            self._notify(self.tr("Removed {0} archive(s)").format(removed), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Remove archives"),
            self.tr("Permanently delete {0} archive(s) from disk?").format(len(paths)),
            _confirmed, confirm_label=self.tr("Delete"), list_items=names)

    def _on_downloads_move(self):
        """Move the checked archives between the *configured* download locations.
        Opens a borderless overlay listing those locations (Downloads / Mod
        Manager cache / extras) rather than a native folder browser."""
        paths = self._downloads_view.checked_paths()
        if not paths:
            return
        game_name = getattr(self._gs, "game_name", None)
        from gui_qt.move_downloads_overlay import MoveDownloadsOverlay
        MoveDownloadsOverlay.show_over(
            self, len(paths), game_name,
            lambda dest: self._move_downloads_to(paths, dest))

    def _move_downloads_to(self, paths, dest):
        if not dest or not paths:
            return
        dest_dir = Path(dest)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notify(self.tr("Cannot use that folder: {0}").format(exc), "error")
            return

        # Anything that would overwrite an existing file at the destination needs
        # confirmation before we clobber it.
        clashes = [p for p in paths
                   if (dest_dir / Path(p).name).exists()
                   and Path(dest_dir / Path(p).name).resolve() != Path(p).resolve()]

        def _do_move():
            import shutil
            moved = 0
            for p in paths:
                src = Path(p)
                target = dest_dir / src.name
                try:
                    if src.resolve() == target.resolve():
                        continue  # already there
                except OSError:
                    pass
                try:
                    if target.exists():
                        target.unlink()
                    shutil.move(str(src), str(target))
                    moved += 1
                except OSError as exc:
                    print(f"[gui_qt] move failed: {p} -> {target}: {exc}", flush=True)
            self._notify(self.tr("Moved {0} archive(s)").format(moved), "info")
            self._downloads_view.clear_checks()
            self._downloads_view.refresh()

        if clashes:
            names = "\n".join(Path(p).name for p in clashes[:20])
            more = f"\n… and {len(clashes) - 20} more" if len(clashes) > 20 else ""
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self, self.tr("Overwrite archives"),
                self.tr("{0} file(s) already exist in that folder and will be "
                        "overwritten:\n\n").format(len(clashes)) + names + more,
                lambda ok: _do_move() if ok else None,
                confirm_label=self.tr("Overwrite"))
        else:
            _do_move()

    def _text_files_footer(self) -> QWidget:
        """Search Content / Filters + search, shown under the plugins column when
        the Text Files sub-tab is active (read-only - no Pack/Install)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Inline content-search popup (hidden until "Search Content" is clicked).
        self._tf_content_bar = QWidget()
        cbl = QHBoxLayout(self._tf_content_bar)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(4)
        lbl = QLabel(self.tr("Find in files:"))
        lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        cbl.addWidget(lbl)
        self._tf_content_input = QLineEdit()
        self._tf_content_input.setPlaceholderText(self.tr("Text to search for…"))
        self._tf_content_input.setClearButtonEnabled(True)
        self._tf_content_input.returnPressed.connect(self._run_tf_content_search)
        cbl.addWidget(self._tf_content_input, 1)
        go = self._color_button(self.tr("Search"), _c(self._pal, "BTN_SUCCESS"), compact=True)
        go.setFixedHeight(self._FOOT_BTN_H)
        go.clicked.connect(self._run_tf_content_search)
        cbl.addWidget(go)
        close = self._text_button("✕", compact=True)
        close.setFixedHeight(self._FOOT_BTN_H)
        close.clicked.connect(self._close_tf_content_bar)
        cbl.addWidget(close)
        self._tf_content_bar.setVisible(False)
        v.addWidget(self._tf_content_bar)

        btns = FlowLayout(spacing=4)
        self._tf_content_btn = self._color_button(
            self.tr("Search Content"), _c(self._pal, "BTN_INFO"), compact=True)
        self._tf_content_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_content_btn.clicked.connect(self._on_text_files_content_search)
        self._tf_filters_btn = self._filters_button()
        self._tf_filters_btn.clicked.connect(self._toggle_text_files_filters)
        self._tf_expand_btn = self._text_button(
            self.tr("⊞ Expand all"), compact=True)
        self._tf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_expand_btn.clicked.connect(self._on_tf_expand_clicked)
        btns.addWidget(self._tf_expand_btn)
        btns.addWidget(self._tf_content_btn)
        # A dim status label showing the active content-search keyword.
        self._tf_content_status = QLabel("")
        self._tf_content_status.setStyleSheet(
            f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._tf_content_status)
        v.addLayout(btns)
        self._text_files_view.content_status_changed.connect(
            self._on_tf_content_status)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._tf_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._text_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._tf_search = search
        search.textChanged.connect(
            lambda _t, a="_tf_filters_btn": self._sync_filters_btn(a))
        return bar

    def _set_tf_content_bar_visible(self, visible: bool):
        """Show/hide the inline find-in-files bar, then re-clamp the footer
        stack: its max height is pinned to the current page's height (see
        _CurrentPageStack), so the page growing a row would otherwise be
        squashed into the old clamped height."""
        self._tf_content_bar.setVisible(visible)
        self._plugin_footer_stack.clamp_to_current()

    def _on_text_files_content_search(self):
        """Toggle the inline content-search bar. When a search is active, this
        clears it; otherwise it opens the input bar above the footer + focuses it."""
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()
            self._tf_content_input.clear()
            self._set_tf_content_bar_visible(False)
            return
        showing = not self._tf_content_bar.isVisible()
        self._set_tf_content_bar_visible(showing)
        if showing:
            self._tf_content_input.setFocus()
            self._tf_content_input.selectAll()

    def _on_tf_expand_clicked(self):
        expanded = self._text_files_view._toggle_expand_all()
        self._tf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _run_tf_content_search(self):
        kw = self._tf_content_input.text().strip()
        if kw:
            self._text_files_view.run_content_search(kw)
        else:
            self._text_files_view.clear_content_search()

    def _close_tf_content_bar(self):
        self._set_tf_content_bar_visible(False)
        self._tf_content_input.clear()
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()

    def _on_tf_content_status(self, keyword):
        if keyword:
            self._tf_content_status.setText(self.tr('Content: "{0}"').format(keyword))
            self._tf_content_btn.setText(self.tr("Clear Content"))
        else:
            self._tf_content_status.setText("")
            self._tf_content_btn.setText(self.tr("Search Content"))

    def _left_header(self) -> QWidget:
        # Single row: game/profile selectors, then the mod-action buttons.
        header = _HeaderBar(self._sync_header_compact)
        header.setObjectName("HeaderBar")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Game selector - no label; the game names make it self-evident. Items
        # are populated from the configured games (_populate_selectors); with
        # none configured the button reads "Add game".
        self._game_selector = SelectorButton(
            items=[],
            current=self.tr("Add game"),
            actions=self._game_actions(),
            on_select=self._on_game_changed,
            icon_provider=self._game_logo_icon,
            icon_px=self._ICON_PX,
        )
        self._game_selector.setFixedHeight(self._BTN_H)
        h.addWidget(self._game_selector)

        # Profile selector - "Profile:" prefix baked into the button text.
        # icon_px=16 pins the group badge to the menu's radio-indicator
        # geometry so icon rows align with radio rows.
        self._profile_selector = SelectorButton(
            items=["default"],
            current="default",
            prefix=self.tr("Profile: "),
            min_width=150,
            icon_px=16,
            actions=self._profile_actions(),
            on_select=self._on_profile_changed,
        )
        self._profile_selector.setFixedHeight(self._BTN_H)
        # Rebuild the pinned actions on every open (like the Wizard menu): the
        # "Open" submenu and Quick-configure entries are gated on live game
        # settings, so toggling e.g. profile-specific INI files via Quick
        # configure must make the Open submenu appear without a profile swap.
        self._profile_selector._menu.aboutToShow.connect(
            self._refresh_profile_actions)
        h.addWidget(self._profile_selector)

        # A new game/profile name is a different width - re-run the width
        # budget, since the bar itself isn't resized by it.
        for sel in (self._game_selector, self._profile_selector):
            sel.face_changed.connect(self._sync_header_compact)

        h.addWidget(self._group_sep())

        # Plain mod-action buttons. They drop to icon-only when the bar gets
        # too narrow for the labels (_sync_header_compact).
        self._action_buttons = []
        self._header_compact = False
        _handlers = {"Install Mod": self._on_install_mod,
                     # drop QPushButton.clicked's `bool checked` arg - it would
                     # otherwise land in _on_deploy's `silent` parameter.
                     "Deploy": lambda: self._on_deploy(), "Restore": self._on_restore}
        for label, disp, ico in [
            ("Install Mod", self.tr("Install Mod"), "install.png"),
            ("Deploy",      self.tr("Deploy"),      "deploy.png"),
            ("Restore",     self.tr("Restore"),     "restore.png"),
        ]:
            # `label` stays the canonical key (handler lookup + _full_label);
            # `disp` is the translated text shown on the button + tooltip.
            b = self._action_button(disp, ico)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label in _handlers:
                b.clicked.connect(_handlers[label])
                if label == "Deploy":
                    self._deploy_btn = b
                elif label == "Restore":
                    self._restore_btn = b
                elif label == "Install Mod":
                    self._install_btn = b
            self._action_buttons.append(b)
            h.addWidget(b)

        # Split menu buttons (placeholder menus - wired up in a later phase).
        # `label` stays the canonical key (used in == comparisons + as the
        # button's persistent id); `disp` is the translated button text. Menu
        # item labels are user-visible and translated directly.
        for label, disp, ico, items in [
            ("Proton", self.tr("Proton"), "proton.png", [
                (self.tr("Run winecfg"), self._proton_winecfg),
                (self.tr("Run winetricks"), self._proton_winetricks),
                (self.tr("Run an .exe in this prefix…"), self._proton_run_exe),
                None,
                (self.tr("Open wine registry"), self._proton_regedit),
                (self.tr("Wine DLL overrides"), self._proton_dll_overrides),
                (self.tr("Prefix health check…"), self._proton_health_check),
                None,
                (self.tr("Install VC++ Redistributable"), self._proton_install_vcredist),
                (self.tr("Install d3dcompiler_47"), self._proton_install_d3dcompiler),
                (self.tr("Install XACT audio (XAudio2)"), self._proton_install_xact),
                # Hidden unless the current game declares it
                (self.tr("Install LAV Filters (radio/music codecs)"),
                 self._proton_install_lavfilters, "lavfilters"),
                (self.tr(".NET runtime"), [
                    (self.tr(".NET {0}").format(v), (lambda v=v: self._proton_install_dotnet(v)))
                    for v in DOTNET_VERSIONS
                ]),
            ]),
            # Wizard's menu is dynamic - rebuilt per game on aboutToShow.
            ("Wizard", self.tr("Wizard"), "wizard.png", []),
            ("Nexus", self.tr("Nexus"), "nexus.png", [
                (self.tr("Open Nexus Mods"), self._open_nexus_browser_tab),
                (self.tr("Open game on nexus"), self._open_game_on_nexus),
                None,
                (self.tr("Login to Nexus"), [
                    (self.tr("Login via SSO"), self._nexus_login_sso),
                    (self.tr("Paste login code…"), self._nexus_paste_code),
                    (self.tr("Clear credentials"), self._nexus_clear_credentials),
                    (self._nxm_menu_label(), self._nexus_toggle_nxm),
                ]),
                (self.tr("Collections"), [
                    (self.tr("Browse collections…"), self._open_collections_tab),
                    (self.tr("Create collection…"), self._open_create_collection_tab),
                    (self.tr("My collections…"), self._open_my_collections_tab),
                    (self.tr("Open current collection"), self._open_current_collection),
                    (self.tr("Reset load order"), self._reset_collection_load_order),
                ]),
            ]),
            # Shown only for games declaring a thunderstore_community (see
            # _sync_thunderstore_button) - unlike Nexus, most games aren't on
            # Thunderstore at all, so a dead button would be noise.
            ("Thunderstore", self.tr("Thunderstore"), "Thunderstore.png", [
                (self.tr("Browse Thunderstore"),
                 self._open_thunderstore_browser_tab),
                (self.tr("Open game on Thunderstore"),
                 self._open_game_on_thunderstore),
                None,
                (self.tr("Identify installed mods"),
                 self._identify_thunderstore_mods),
            ]),
        ]:
            # Proton's logo is a mono glyph - tint it white like the Settings
            # icon so it stays visible. The others are full-colour logos.
            tint = _c(self._pal, "TEXT_MAIN") if label == "Proton" else None
            b = self._menu_action_button(disp, ico, items, tint=tint)
            b.setFixedHeight(self._BTN_H)
            b.setToolTip(disp)
            b._full_label = disp
            if label == "Wizard":
                self._wizard_btn = b
                b._menu.aboutToShow.connect(self._rebuild_wizard_menu)
            elif label == "Proton":
                # Static menu, but a couple of entries are game-specific -
                # re-evaluate their visibility each time it opens so game
                # switches are handled for free.
                b._menu.aboutToShow.connect(self._sync_proton_menu)
            elif label == "Thunderstore":
                self._thunderstore_btn = b
            elif label == "Nexus":
                self._nexus_btn = b
            self._action_buttons.append(b)
            h.addWidget(b)

        # Gate the Nexus / Thunderstore buttons on the current game's stores.
        # Done after the loop so both buttons exist; safe before the header is
        # shown (the sync tracks intent, not isVisible()).
        self._sync_thunderstore_button()

        h.addStretch(1)

        # Notifications - unread dot + dropdown history of recent toasts.
        self._notif_button = NotificationButton(
            self._notif_history, btn_h=self._BTN_H, icon_px=self._ICON_PX)
        h.addWidget(self._notif_button)

        # Settings - icon-only square button on the far right. Opens a Settings
        # tab scoped over the Plugins panel.
        self._settings_button = self._icon_square_button(
            "settings.png", tooltip=self.tr("Settings"), tint=_c(self._pal, "TEXT_MAIN"))
        self._settings_button.clicked.connect(self._open_settings_tab)
        h.addWidget(self._settings_button)

        self._left_header_widget = header
        return header

    def _game_logo_icon(self, name: str):
        """The game's square logo, for its row in the game menu and for the
        selector's face when the bar is too narrow to spell the name out.

        Loaded at 2x the display size so it stays crisp on HiDPI (QIcon.paint
        downscales cleanly) - same trick as the play bar's exe icons.
        """
        try:
            from Utils.game_helpers import _GAMES
            from gui_qt.add_game_view import _game_logo
            from PySide6.QtGui import QIcon
            game = _GAMES.get(name)
            pm = _game_logo(getattr(game, "game_id", "") or name,
                            self._ICON_PX * 2)
            if pm is not None and not pm.isNull():
                return QIcon(pm)
        except Exception:
            pass
        return None

    def _icon_square_button(self, icon_name: str, tooltip: str = "",
                            tint: str | None = None) -> QToolButton:
        """A compact square icon-only button (e.g. Settings) for the toolbar.

        *tint* recolours a mono glyph to a theme colour so it stays visible in
        both light and dark modes (the raw PNGs are white)."""
        b = QToolButton()
        b.setIcon(icon(icon_name, self._ICON_PX, color=tint))
        b.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setObjectName("IconButton")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(self._BTN_H, self._BTN_H)
        if tooltip:
            b.setToolTip(tooltip)
        return b

    # ---- narrow top bar: staged compaction ----------------------------------
    # The bar gives up width in stages, least destructive first: the profile
    # label is elided, then the game name drops to the game's logo, and only
    # then do the action buttons lose their labels (see _sync_header_compact).
    #
    # Extra width the bar must have BEYOND what a stage needs before that stage
    # comes back. Without this dead band the two states chase each other:
    # restoring the labels makes the bar want more room than it has, which
    # collapses them again on the very next resize event.
    _HEADER_HYST_PX = 24
    # Floor the profile selector is elided down to before the next stage starts.
    _HEADER_PROFILE_MIN_PX = 120

    def _measure_action_buttons(self) -> None:
        """Cache each action button's text+icon and icon-only widths (once)."""
        # Deferred to the first resize rather than done at build time: the theme
        # stylesheet (which supplies the paddings) is applied after the widgets
        # are created, so widths measured in _left_header would be unstyled.
        if getattr(self, "_action_btn_widths", False):
            return
        # The mode flips below re-lay-out the bar, so a resize landing mid-measure
        # would run _sync_header_compact against half-filled widths.
        self._measuring_buttons = True
        try:
            for b in self._action_buttons:
                b.ensurePolished()
                b._full_w = b.sizeHint().width()
            # Flip to icon-only just long enough to read the collapsed widths back.
            self._set_header_compact(True)
            for b in self._action_buttons:
                b._icon_w = b.sizeHint().width()
            self._set_header_compact(False)
            # Prime the selectors' text metrics while they're at full size -
            # they can't be measured once a label is elided or collapsed.
            self._game_selector.natural_width()
            self._profile_selector.natural_width()
            self._action_btn_widths = True
        finally:
            self._measuring_buttons = False

    def _set_header_compact(self, on: bool) -> None:
        """Show the action buttons as icon-only (*on*) or icon+label."""
        # The tooltips already carry the labels, so collapsed stays discoverable.
        self._header_compact = on
        for b in self._action_buttons:
            b.setToolButtonStyle(Qt.ToolButtonIconOnly if on
                                 else Qt.ToolButtonTextBesideIcon)
            # QSS trims the label padding (and the split arrow room) via this
            # property so the collapsed buttons are near-square, not just short.
            b.setProperty("compact", on)
            b.style().unpolish(b); b.style().polish(b)

    def _sync_header_compact(self) -> None:
        """Claw back the width the top bar is missing, one stage at a time:
        elide the profile label, then drop the game name to its logo, and only
        then collapse the action buttons to icon-only."""
        hdr = getattr(self, "_left_header_widget", None)
        if (hdr is None or not getattr(self, "_action_buttons", None)
                or getattr(self, "_measuring_buttons", False)
                # Each stage relays out the bar, which can re-enter through
                # face_changed - decide once, then apply.
                or getattr(self, "_syncing_header", False)):
            return
        self._syncing_header = True
        try:
            self._measure_action_buttons()
            gsel, psel = self._game_selector, self._profile_selector
            # What each stage is worth. The game selector only collapses when
            # the current game HAS a logo - a blank button says nothing.
            prof_room = max(0, psel.natural_width() - self._HEADER_PROFILE_MIN_PX)
            game_icon_w = gsel.icon_width()
            game_room = (max(0, gsel.natural_width() - game_icon_w)
                         if game_icon_w else 0)
            # Hidden buttons (the Thunderstore one on games without a
            # community) save nothing by collapsing, so counting their label
            # width here would inflate the deficit and trip the stages early.
            btn_room = sum(b._full_w - b._icon_w
                           for b in self._action_buttons if b.isVisible())
            # How much the bar is over budget with nothing collapsed: its
            # current hint plus everything the active stages already save.
            saved = ((gsel.natural_width() - gsel.current_width())
                     + (psel.natural_width() - psel.current_width())
                     + (btn_room if self._header_compact else 0))
            need = hdr.sizeHint().width() + saved - hdr.width()

            def _stage(active: bool, threshold: int) -> bool:
                """An all-or-nothing stage engages past *threshold* px of
                deficit and only lets go once we're clear of it by the dead
                band (_HEADER_HYST_PX)."""
                return need > (threshold - self._HEADER_HYST_PX if active
                               else threshold)

            game_on = bool(game_room) and _stage(gsel.is_icon_only(), prof_room)
            btns_on = _stage(self._header_compact,
                             prof_room + (game_room if game_on else 0))
            # Whatever the collapsed stages didn't cover comes out of the
            # profile label - so it stretches back out once they kick in.
            prof_take = max(0, min(prof_room,
                                   need - (game_room if game_on else 0)
                                   - (btn_room if btns_on else 0)))

            gsel.set_icon_only(game_on)
            psel.set_label_width(psel.natural_width() - prof_take
                                 if prof_take else None)
            if btns_on != self._header_compact:
                self._set_header_compact(btns_on)
        finally:
            self._syncing_header = False

    # ---- selector handlers -------------------------------------------------
    def _on_game_changed(self, name):
        if name == self._gs.game_name:
            return
        self._gs.set_game(name)
        # The Profile Settings tab is scoped to the previous game - close it.
        if self._tabs.has_key("profile_settings"):
            self._tabs.close_tab("profile_settings")
            self._profile_settings_view = None
        # The Profile Groups tab caches the previous game object - close it.
        if self._tabs.has_key("profile_groups"):
            self._tabs.close_tab("profile_groups")
            self._profile_groups_view = None
        # The Wine DLL overrides tab is game-scoped too - close it.
        if self._tabs.has_key("dll_overrides"):
            self._tabs.close_tab("dll_overrides")
            self._dll_overrides_view = None
        # The View Requirements tab reads the previous game's staging - close
        # it (its destroyed hook restores the conflict highlights).
        if self._tabs.has_key("view_requirements"):
            self._tabs.close_tab("view_requirements")
        # Wizard tool tabs are game-scoped - close them all.
        self._close_wizard_tabs()
        # The exe-settings tab is bound to the previous game's exe.
        self._close_exe_settings_tab()
        # Userlist tabs/bars hold the previous profile's userlist path.
        self._close_userlist_ui()
        # Retarget the Nexus / Collections browsers (if open) at the new game so
        # they show the new game's mods instead of holding a stale domain. A
        # missing/empty Nexus domain closes them (nothing to show).
        self._retarget_browsers_for_game()
        self._sync_thunderstore_button()
        # Reflect the new game's profiles + keep both game selectors in sync.
        profs = self._gs.profiles()
        if profs:
            self._set_profile_selector_items(profs, current=self._gs.profile)
        # The Open submenu depends on the new game's profile-specific settings.
        self._refresh_profile_actions()
        self._game_selector.set_current(name)
        # The 'Edit custom game…' entry depends on the active game.
        self._refresh_game_actions()
        self._refresh_play_selector()
        self._clear_search_boxes()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()

    def _retarget_browsers_for_game(self):
        """On game switch, point any open Nexus / Collections browser at the new
        game (or close them if the new game has no Nexus domain). Per-collection
        detail tabs are game-specific, so they're closed unconditionally."""
        game = self._gs.game
        domain = (getattr(game, "nexus_game_domain", "") or "") if game else ""

        # Close per-collection detail tabs (they belong to the previous game).
        # Swallow (a game switch must not abort) but leave a trace - a failure
        # here leaves stale tabs from the previous game.
        try:
            for key in [k for k in list(self._tabs._keys)
                        if k.startswith("collection_detail_")]:
                self._tabs.close_tab(key)
        except Exception as exc:
            from Utils.app_log import app_log
            app_log(f"game switch: closing collection tabs failed: {exc}")

        for attr, tab_key in (("_nexus_view", "nexus_browser"),
                              ("_collections_view", "collections")):
            view = getattr(self, attr, None)
            if view is None:
                continue
            if not domain:
                # New game can't show Nexus content - close the tab.
                if self._tabs.has_key(tab_key):
                    self._tabs.close_tab(tab_key)
                setattr(self, attr, None)
                continue
            try:
                view.set_game(game, domain)
            except Exception as exc:
                self._append_log(f"[nexus] retarget failed: {exc}")

        # Thunderstore keys on its own community, NOT the Nexus domain - a
        # Thunderstore-only game has no domain and vice versa, so folding this
        # into the loop above would close the wrong tab.
        community = ((getattr(game, "thunderstore_community", "") or "").strip()
                     if game else "")
        ts_view = getattr(self, "_thunderstore_view", None)
        if ts_view is not None:
            if not community:
                if self._tabs.has_key("thunderstore_browser"):
                    self._tabs.close_tab("thunderstore_browser")
                self._thunderstore_view = None
            else:
                try:
                    ts_view.set_game(game, community)
                except Exception as exc:
                    self._append_log(f"[thunderstore] retarget failed: {exc}")

    def _clear_search_boxes(self):
        """Reset both search boxes on a game/profile switch (Tk parity - a
        stale query silently filters the freshly-loaded lists)."""
        for attr, text_attr in (("_modlist_search", "_modlist_search_text"),
                                ("_plugins_search", "_plugin_search_text")):
            box = getattr(self, attr, None)
            if box is not None and box.text():
                box.clear()   # textChanged → debounced apply sees empty
            setattr(self, text_attr, "")

    def _on_profile_changed(self, name):
        if name == self._gs.profile:
            return
        from Utils import perftrace
        # End-to-end switch latency: the switch "feels done" only when the async
        # milestones land (meta → plugins → conflicts → final plugin pass), so
        # stamp t0 here and mark elapsed at each (see _mark_since_switch).
        if perftrace.is_enabled():
            import time
            self._switch_t0 = time.perf_counter()
            self._switch_conflicts_done = False
        with perftrace.span("switch.sync_total"):
            with perftrace.span("switch.set_profile"):
                self._gs.set_profile(name)
            self._profile_selector.set_current(name)
            # profile_ini_files / profile_saves are per-profile overrides - set_profile
            # reloaded them, so refresh the Open submenu for the new profile.
            self._refresh_profile_actions()
            # Userlist tabs/bars are profile-scoped (userlist.yaml lives there).
            self._close_userlist_ui()
            # Exe selection + exe args are per-profile.
            self._close_exe_settings_tab()
            with perftrace.span("switch.refresh_play_selector"):
                self._refresh_play_selector()
            self._clear_search_boxes()
            # Non-specific profiles share the mods folder: a mod added while
            # another profile was active is on disk but absent from this
            # profile's modlist.txt. Sync the file against the folder so the
            # new mod shows up on switch without a manual Refresh. This is
            # cheap (one iterdir + modlist.txt diff) and does NOT rescan the
            # shared index/filemap, which is unchanged across the switch.
            with perftrace.span("switch.sync_modlist_folder"):
                try:
                    from Utils.modlist import sync_modlist_with_mods_folder
                    ml = self._gs.modlist_path()
                    staging = self._gs.staging_dir()
                    if ml is not None and staging is not None:
                        sync_modlist_with_mods_folder(ml, staging)
                except Exception as exc:
                    print(f"[gui_qt] profile-switch modlist sync failed: {exc}",
                          flush=True)
            with perftrace.span("switch.reload_modlist(sync)"):
                self._reload_modlist()
            with perftrace.span("switch.reload_plugins(kickoff)"):
                if getattr(self, "_reload_had_entries", False):
                    # The conflict rebuild just queued by _reload_modlist ends
                    # in a plugin reload against the FRESH filemap. Loading now
                    # too would run the whole per-plugin pipeline twice - and
                    # the early pass resolves against the OLD profile's filemap
                    # (observed: 10 of 254 plugins listed until the rebuild
                    # corrected it). Clear the stale panel; the rebuild's
                    # reload populates it once, correctly.
                    self._clear_plugin_panel()
                else:
                    # Empty modlist → no conflict rebuild is coming; load the
                    # (vanilla-only) plugin list directly.
                    self._reload_plugins()
            self._update_deployed_profile_highlight()
        # Appended-collections section tracks the ACTIVE profile.
        view = getattr(self, "_collections_view", None)
        if view is not None:
            try:
                view.refresh_appended()
            except Exception:
                pass
        # Keep the Profile Settings ★ marker in sync if that tab is open.
        if self._tabs.has_key("profile_settings"):
            v = getattr(self, "_profile_settings_view", None)
            if v is not None:
                v.set_current_profile(name)
        if self._tabs.has_key("profile_groups"):
            v = getattr(self, "_profile_groups_view", None)
            if v is not None:
                v.set_current_profile(name)
        # Configure-Game tab tracks the active profile too - refresh its form +
        # tab title in place (no focus change) so the user doesn't have to
        # reopen it to configure a different profile.
        if self._tabs.has_key("configure_game"):
            v = getattr(self, "_configure_game_view", None)
            if v is not None:
                try:
                    v.refresh_for_profile()
                    self._tabs.set_tab_title(
                        "configure_game", self._configure_tab_title(self._gs.game))
                except Exception:
                    pass

    def _mark_since_switch(self, label: str) -> None:
        """Perf instrumentation (MM_PERFTRACE=1): record wall-clock elapsed
        since the last profile switch under *label*. The switch's perceived
        latency spans thread boundaries (sync UI work → meta worker → plugin
        worker → conflict/filemap worker → final plugin pass), which a single
        span can't measure - so each async milestone marks its own arrival
        time. No-op when perftrace is disabled or no switch is in flight."""
        t0 = getattr(self, "_switch_t0", None)
        if t0 is None:
            return
        import time
        from Utils import perftrace
        perftrace.mark(label, time.perf_counter() - t0)

    def _game_actions(self):
        """Build the pinned entries for the game selector. The 'Edit custom
        game…' entry only appears when the active game is a custom game whose
        definition is editable (i.e. not a repo handler with editable: false)."""
        actions = [
            (self.tr("Add game…"), lambda: self._on_game_action("add")),
            (self.tr("Configure game…"), lambda: self._on_game_action("configure")),
            (self.tr("Define custom game…"), lambda: self._on_game_action("custom")),
        ]
        game = getattr(self._gs, "game", None)
        if (game is not None
                and getattr(game, "is_custom", False)
                and getattr(game, "editable", False)):
            actions.append(
                (self.tr("Edit custom game…"), lambda: self._on_game_action("edit_custom")))
        # Repo handlers (downloaded from the Resources branch) carry version +
        # editable metadata; user-authored definitions don't. Only those can be
        # force-refreshed from the repo.
        defn = getattr(game, "_defn", None) if game is not None else None
        if (game is not None
                and getattr(game, "is_custom", False)
                and isinstance(defn, dict)
                and "version" in defn and "editable" in defn):
            actions.append(
                (self.tr("Force update handler"),
                 lambda: self._on_game_action("update_handler")))
        actions.append(
            (self.tr("Open"), [
                (self.tr("Game folder"),     lambda: self._open_game_dir("game")),
                (self.tr("Prefix folder"),   lambda: self._open_game_dir("prefix")),
                (self.tr("My Games folder"), lambda: self._open_game_dir("mygames")),
                (self.tr("AppData folder"),  lambda: self._open_game_dir("appdata")),
                (self.tr("Staging folder"),  lambda: self._open_game_dir("staging")),
                (self.tr("Profile folder"),  lambda: self._open_game_dir("profile")),
                (self.tr(".config folder"),  lambda: self._open_game_dir("config")),
            ]))
        return actions

    def _refresh_game_actions(self):
        """Rebuild the game-selector menu so the 'Edit custom game…' entry
        appears/disappears to match the active game."""
        if getattr(self, "_game_selector", None) is not None:
            self._game_selector.set_actions(self._game_actions())

    def _on_game_action(self, which):
        if which == "add":
            self._open_add_game_tab()
        elif which == "configure":
            game = self._gs.game
            if game is not None:
                self._open_configure_game_tab(game)
            else:
                self._append_log("[game] no active game to configure")
        elif which == "custom":
            self._open_custom_game_tab()
        elif which == "edit_custom":
            self._open_edit_custom_game_tab()
        elif which == "update_handler":
            self._force_update_handler()
        else:
            self._append_log(f"[game] {which} (not wired yet)")

    def _open_custom_game_tab(self):
        """Open the Define-Custom-Game form as a (detachable) tab. On save, chain
        to the Configure-Game tab so the user can set the install path/prefix
        (a fresh custom game has no path yet → not yet selectable)."""
        if self._tabs.has_key("custom_game"):
            self._tabs.focus_key("custom_game")
            return
        from gui_qt.custom_game_view import CustomGameView

        def _done(saved_defn, deleted):
            self._tabs.close_tab("custom_game")
            if saved_defn is None:
                return
            from Utils.game_helpers import _load_games, _GAMES
            names = _load_games()
            self._gs.game_names = names
            real_names = [n for n in names if n != "No games configured"]
            if real_names:
                self._game_selector.set_items(
                    real_names, current=self._gs.game_name)
            else:
                self._game_selector.set_items([], current=self.tr("Add game"))
            self._append_log(f"[game] custom game defined: {saved_defn['name']}")
            game = _GAMES.get(saved_defn["name"])
            if game is not None:
                # A freshly-defined custom game has no path yet - this is the
                # add flow, so Save should close the tab when done.
                self._open_configure_game_tab(game, from_add_game=True)

        view = CustomGameView(on_done=_done)
        self._tabs.open_tab(view, self.tr("Define custom game"), key="custom_game")

    def _open_edit_custom_game_tab(self):
        """Open the custom-game form pre-filled with the active game's definition
        so it can be edited in place. Only reachable for custom, editable games
        (see _game_actions). On save, refresh the game list + reload the game so
        any changed name/paths/routing take effect."""
        game = self._gs.game
        defn = getattr(game, "_defn", None) if game is not None else None
        if not defn:
            self._append_log("[game] no editable custom-game definition to edit")
            return
        if self._tabs.has_key("custom_game"):
            self._tabs.focus_key("custom_game")
            return
        from gui_qt.custom_game_view import CustomGameView

        prev_name = self._gs.game_name

        def _done(saved_defn, deleted):
            self._tabs.close_tab("custom_game")
            from Utils.game_helpers import _load_games
            names = _load_games()
            self._gs.game_names = names
            real_names = [n for n in names if n != "No games configured"]
            # A rename or delete may have changed which game we should show.
            target = None
            if saved_defn is not None and saved_defn["name"] in real_names:
                target = saved_defn["name"]
            elif prev_name in real_names:
                target = prev_name
            elif real_names:
                target = real_names[0]
            if target is not None:
                self._game_selector.set_items(real_names, current=target)
                # Re-select through the normal path so all views reload for the
                # (possibly renamed) game.
                self._gs.game_names = names
                self._gs.set_game(target)
                self._game_selector.set_current(target)
                self._refresh_game_actions()
                self._refresh_profile_actions()
                self._refresh_play_selector()
                self._reload_modlist()
                self._reload_plugins()
            else:
                self._game_selector.set_items([], current=self.tr("Add game"))
                self._refresh_game_actions()
            if deleted:
                self._append_log(f"[game] custom game deleted: {prev_name}")
            elif saved_defn is not None:
                self._append_log(f"[game] custom game edited: {saved_defn['name']}")

        view = CustomGameView(on_done=_done, existing=defn)
        self._tabs.open_tab(view, self.tr("Edit custom game"), key="custom_game")

    def _force_update_handler(self):
        """Re-download the active game's repo handler .json from the Resources
        branch right now (bypassing the sync cache) so the user doesn't have to
        wait for the next startup sync to pick up a repo fix."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import force_update_handler
        game = self._gs.game
        defn = getattr(game, "_defn", None) if game is not None else None
        if not isinstance(defn, dict):
            self._append_log("[game] no handler definition to update")
            return
        # Repo handlers are named <game_id>.json, but a local edit re-saves
        # under the game_id - try the on-disk name first, then the canonical.
        candidates = []
        src = defn.get("_source_file", "")
        if src:
            candidates.append(Path(src).name)
        gid_name = f"{game.game_id}.json"
        if gid_name not in candidates:
            candidates.append(gid_name)
        name = game.name
        self._notify(self.tr("Updating handler…"), "info")
        self._append_log(f"[game] force handler update: {name} ({candidates[0]})")
        force_update_handler(
            candidates,
            on_done=lambda status: safe_emit(
                self._handler_force_updated, name, status))

    def _on_handler_force_updated(self, name: str, status: str):
        """Force-update worker finished (UI thread). On a real update, reload
        the game registry and re-select the game through the normal path so the
        new definition takes effect everywhere (views, actions, play bar)."""
        if status == "failed":
            self._notify(
                self.tr("Handler update failed - check your connection."),
                "error")
            return
        if status == "missing":
            self._notify(
                self.tr("Handler not found on the Resources branch."), "error")
            return
        if status == "unchanged":
            self._notify(self.tr("Handler is already up to date."), "info")
            return
        from Utils.game_helpers import _load_games
        names = _load_games()
        self._gs.game_names = names
        real_names = [n for n in names if n != "No games configured"]
        # A repo rename may have changed the display name; fall back sanely.
        target = (name if name in real_names
                  else (real_names[0] if real_names else None))
        if target is not None:
            self._game_selector.set_items(real_names, current=target)
            self._gs.set_game(target)
            # set_game no-ops when the name didn't change (the common case) -
            # but _load_games() just rebuilt the game object, so its active
            # profile dir + per-profile path overrides must be re-applied.
            self._gs._apply_active_profile()
            self._game_selector.set_current(target)
            self._refresh_game_actions()
            self._refresh_profile_actions()
            self._refresh_play_selector()
            self._reload_modlist()
            self._reload_plugins()
        self._append_log(f"[game] handler updated from Resources: {name}")
        self._notify(self.tr("Handler updated."), "success")

    def _start_gh_sync(self):
        """Kick off background sync of custom handlers + Qt plugins from GitHub."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import (
            sync_custom_handlers, sync_plugins, sync_languages,
            sync_ludusavi_manifest,
        )
        # Handler downloads land on a worker thread; marshal the "refresh games"
        # signal to the UI thread. Plugin sync only touches the plugin cache
        # (discover_plugins) and picks up on the next tools-menu open - no UI
        # refresh needed here.
        sync_custom_handlers(on_changed=lambda: safe_emit(self._handlers_synced))
        sync_plugins()
        # UI translations (.qm) → config languages/ folder. New/updated ones
        # apply on next launch (Qt can't hot-swap installed translators safely).
        sync_languages(on_changed=lambda: safe_emit(self._languages_synced))
        # Ludusavi save-path data → config ludusavi/ folder. It reloads itself
        # in place, so refresh the Saves tab (and its per-game visibility) once
        # newer data lands.
        sync_ludusavi_manifest(on_changed=lambda: safe_emit(self._ludusavi_synced))

    def _sweep_deploy_trash_startup(self):
        """Remove leftover restore-trash dirs for every configured game.

        A restore renames the deploy dir to "<name>.mm_trash-<ns>" and deletes
        it in a background thread; a crash mid-delete leaves the trash behind.
        move_to_core/restore also sweep their own game, so this startup pass
        just covers games that aren't deployed again this session.  Runs on a
        daemon thread - it's pure cleanup and may touch slow drives.
        """
        import threading
        from Utils.game_helpers import _GAMES

        games = [g for g in _GAMES.values() if g.is_configured()]

        def _run():
            from Utils.deploy_standard import sweep_deploy_trash
            for game in games:
                try:
                    data = game.get_mod_data_path()
                    root = game.get_game_path()
                    if data is None or root is None:
                        continue
                    data = Path(data)
                    # Only standard-mode layouts (Data/ inside the game root)
                    # ever create trash siblings; skip game-root-mode games so
                    # we never scan outside the game install.
                    if data == Path(root):
                        continue
                    sweep_deploy_trash(data.parent)
                except Exception:
                    continue

        threading.Thread(target=_run, name="mm-trash-sweep", daemon=True).start()

    def _check_for_app_update(self, force_downgrade_prompt: bool = False,
                              force_fresh: bool = False):
        """Run in background: fetch latest version and prompt if newer.

        AppImage installs compare against GitHub releases and offer the
        auto-installer.  System installs (e.g. AUR) compare against the AUR
        package version and show instructions to update via the AUR helper.

        When *force_downgrade_prompt* is True (e.g. the user just toggled the
        pre-release channel off while running a beta), the AppImage/Flatpak
        branches will surface the latest stable even if it's older than the
        currently-running version, with downgrade-aware copy.

        When *force_fresh* is True (or implied by force_downgrade_prompt), the
        ETag-cache throttle is bypassed so a manual user action triggers an
        immediate re-check instead of waiting out the 1-hour throttle window.
        """
        import threading
        from gui_qt.safe_emit import safe_emit
        from version import __version__
        from Utils.ui_config import (
            load_allow_prerelease, load_update_notifications,
        )
        from Utils.version_check import (
            is_appimage, is_flatpak,
            _fetch_latest_version, _fetch_aur_version, _is_newer_version,
        )

        force_fresh = bool(force_fresh or force_downgrade_prompt)

        # Opted out of update notifications (overlay checkbox / Settings):
        # skip the AUTOMATIC startup check (no API call, no banner). User-
        # initiated re-checks (pre-release toggle → force_fresh) still run, so
        # channel switching keeps working while muted. The flatpak origin
        # tidy-up must still happen though - a fresh bundle install recreates
        # its no-enumerate origin remote and relies on startup to heal it.
        if not force_fresh and not load_update_notifications():
            if is_flatpak():
                def _polish_only():
                    from Utils.version_check import polish_flatpak_origin
                    polish_flatpak_origin()
                threading.Thread(target=_polish_only, daemon=True).start()
            return

        def _do_check():
            allow_pre = load_allow_prerelease()
            if is_appimage() or is_flatpak():
                mode = "appimage" if is_appimage() else "flatpak"
                if mode == "flatpak":
                    # Idempotent tidy-up of a bundle-created origin remote
                    # (enumerate + proper title) so Discover shows real target
                    # versions instead of the branch name. Cheap; daemon thread.
                    from Utils.version_check import polish_flatpak_origin
                    polish_flatpak_origin()
                result = _fetch_latest_version(
                    allow_prerelease=allow_pre, force=force_fresh)
                if result is None:
                    return
                latest, is_pre = result
                newer = _is_newer_version(__version__, latest)
                if newer or (force_downgrade_prompt and latest != __version__):
                    # GitHub Releases decides the *version strings*, but for a
                    # remote-tracked flatpak the *install* comes from our OSTree
                    # remote - which can lag Pages publishing or lack the beta
                    # branch entirely. Only show the banner when the remote can
                    # actually deliver; None (host unreachable) falls back to
                    # the GitHub-only decision.
                    if mode == "flatpak":
                        from Utils.version_check import (
                            flatpak_installed_from_remote,
                            flatpak_remote_update_ready,
                        )
                        if flatpak_installed_from_remote():
                            # Checkbox = channel = OSTree branch. Stables are
                            # published to BOTH branches (the beta branch's
                            # contract is "newest release, beta or stable"), so
                            # beta-channel users get new stables as ordinary
                            # beta-branch updates - no branch hopping needed.
                            branch = "beta" if allow_pre else "stable"
                            if flatpak_remote_update_ready(branch) is False:
                                return
                    safe_emit(self._app_update_found,
                              (__version__, latest, mode, is_pre, not newer))
            else:
                aur_ver = _fetch_aur_version(force=force_fresh)
                if aur_ver is None:
                    return
                if _is_newer_version(__version__, aur_ver):
                    safe_emit(self._app_update_found,
                              (__version__, aur_ver, "aur", False, False))

        threading.Thread(target=_do_check, daemon=True).start()

    def _on_app_update_found(self, payload):
        """Show the update banner over the modlist panel (UI thread)."""
        current, latest, mode, is_prerelease, is_downgrade = payload
        from gui_qt.update_overlay import UpdateOverlay

        # Dismiss any prior banner so re-checking (e.g. after toggling the
        # pre-release setting) doesn't stack multiple overlays.
        prior = getattr(self, "_update_overlay", None)
        if prior is not None:
            try:
                prior.close_overlay()
            except Exception:
                pass
            self._update_overlay = None

        def _on_update():
            if mode == "flatpak":
                from Utils.ui_config import load_allow_prerelease
                from Utils.version_check import (
                    flatpak_installed_from_remote, update_flatpak_from_remote,
                    run_flatpak_installer,
                )
                # Checkbox = channel = branch (stables are published to both
                # branches, so the beta branch always carries the newest
                # release - the banner's offer is deliverable either way).
                allow_pre = load_allow_prerelease()
                if flatpak_installed_from_remote():
                    status = update_flatpak_from_remote(
                        allow_prerelease=allow_pre)
                else:
                    status = ("launched" if run_flatpak_installer(latest)
                              else "unavailable")
                if status == "no-branch":
                    # The requested channel isn't on the remote yet (e.g. beta
                    # before the first beta release is published). The detached
                    # install would fail silently - tell the user instead.
                    channel = self.tr("beta") if allow_pre else self.tr("stable")
                    self._notify(self.tr(
                        "The {0} channel isn't published on the update remote "
                        "yet - try again after the next {0} release.").format(
                            channel), state="warning")
                    return
                if status != "launched":
                    # Host flatpak unreachable - fall back to the releases page
                    # rather than silently closing with nothing happening.
                    from Utils.xdg import open_url
                    from Utils.version_check import _APP_UPDATE_RELEASES_URL
                    open_url(_APP_UPDATE_RELEASES_URL)
                    return
            else:
                from Utils.version_check import run_installer
                run_installer(allow_prerelease=is_prerelease)
            # closeEvent handles the rest of the shutdown (NxmIPC etc.); the
            # installer waits 2s before replacing the running app.
            self.close()

        def _cleared(overlay):
            if getattr(self, "_update_overlay", None) is overlay:
                self._update_overlay = None

        self._update_overlay = UpdateOverlay(
            self._modlist_panel_stack, current, latest, mode=mode,
            is_prerelease=is_prerelease, is_downgrade=is_downgrade,
            on_update=_on_update, on_close=_cleared)

    def _prompt_ui_scale_restart(self):
        """Offer a self-restart after the UI scale changed (Settings). Qt reads
        QT_SCALE_FACTOR only at QApplication construction, so the new scale can
        only apply on a fresh launch. The value is already persisted by the
        caller; confirm, then restart on OK - on Cancel it applies next launch."""
        try:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change UI scale?"),
                self.tr("The UI scale change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            self._request_restart()

    def _prompt_theme_restart(self):
        """Offer a self-restart after the theme changed (Settings). The palette/
        QSS is applied once at startup, so a theme change only takes full effect
        on a fresh launch. The value is already persisted by the caller; confirm,
        then restart on OK - on Cancel it applies next launch."""
        try:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change theme?"),
                self.tr("The theme change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            self._request_restart()

    def _prompt_env_restart(self):
        """Offer a self-restart after the app's environment variables changed.
        They are applied by app_bootstrap before Qt starts, so only a fresh
        launch picks them up. The values are already persisted by the editor;
        on Cancel they simply apply at the next manual launch."""
        try:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to apply environment variables?"),
                self.tr("Environment variables are applied when Amethyst "
                        "starts. Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            self._request_restart()

    def _prompt_language_restart(self):
        """Offer a self-restart after the language changed (Settings picker).
        Alias for _apply_language's confirm flow - the choice is already
        persisted by the caller."""
        self._apply_language(None)

    def _apply_language(self, code: str):
        """Switch UI language (from the Settings/onboarding picker). A live
        in-place retranslate isn't feasible (the whole UI is built once), so we
        cleanly self-restart. The choice is already persisted by the caller;
        confirm first (a restart is disruptive) then restart on OK - on Cancel
        the new language simply applies at the next manual launch."""
        try:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self,
                self.tr("Restart to change language?"),
                self.tr("The language change takes effect after a restart. "
                        "Restart now?"),
                lambda ok: self._request_restart() if ok else None,
                confirm_label=self.tr("Restart now"),
                cancel_label=self.tr("Later"),
                danger=False,
            )
        except Exception:
            # If the overlay can't show for any reason, fall back to restarting.
            self._request_restart()

    def _request_restart(self):
        """Flag a self-restart and close the window. closeEvent runs the normal
        shutdown (IPC release, optional restore-on-close); run() then re-execs
        the process so everything rebuilds fresh (e.g. in the new language)."""
        global _RESTART_REQUESTED
        _RESTART_REQUESTED = True
        # Close on the next tick so the caller (a combo signal handler) returns
        # first and the UI isn't torn down mid-callback.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.close)

    def _sync_languages_now(self):
        """Manual language sync (from onboarding / Settings): force a fetch from
        the Resources branch, then refresh pickers via _languages_synced."""
        from gui_qt.safe_emit import safe_emit
        from Utils.gh_sync import sync_languages
        try:
            self._notify(self.tr("Syncing language files…"), "info")
        except Exception:
            pass
        sync_languages(
            on_changed=lambda: safe_emit(self._languages_synced), force=True)

    def _on_handlers_synced(self):
        """A background handler sync wrote new .json files - reload the registry
        so an open Add-Game picker (and the game selector) sees them, and pull
        down any missing custom-game banner images."""
        try:
            from Utils.game_helpers import _load_games, _GAMES
            _load_games()
        except Exception:
            return
        # Refresh an open Add-Game tab IN PLACE (don't close+reopen - that
        # deletes the view while its scan/image threads may still emit).
        view = self._tabs._keys.get("add_game") if hasattr(self._tabs, "_keys") else None
        if view is not None and hasattr(view, "refresh_games"):
            try:
                view.refresh_games(dict(_GAMES))
            except Exception:
                pass
        # Now that the definitions are on disk, download their banner images.
        self._download_custom_game_images(view)

    def _on_ludusavi_synced(self):
        """Newer Ludusavi save-path data landed - the module already reloaded
        itself, so re-evaluate which games get a Saves tab and rescan an open
        one (a game that had no entry before may have one now)."""
        try:
            self._apply_plugins_supported()
        except Exception:
            pass
        view = getattr(self, "_saves_view", None)
        if view is not None:
            view.mark_dirty()

    def _on_languages_synced(self):
        """A background/manual language sync wrote new/updated .qm files into the
        config languages/ folder. Refresh any open language picker (Settings tab
        or the onboarding page) so newly-available languages show up; the active
        language only fully changes on restart."""
        for view in (
            (self._tabs._keys.get("settings")
             if hasattr(self._tabs, "_keys") else None),
            getattr(self, "_onboarding_view", None),
        ):
            try:
                if view is not None and hasattr(view, "refresh_language_options"):
                    view.refresh_language_options()
            except Exception:
                pass
        try:
            self._notify(
                self.tr("Language files updated - restart to apply."), "info")
        except Exception:
            pass

    def _download_custom_game_images(self, view=None):
        """Background-download missing custom-game banner images. If *view* is an
        AddGameView, refresh each card's logo as its image lands."""
        try:
            from Games.Custom.custom_game import download_missing_custom_game_images
        except Exception:
            return
        cb = None
        if view is not None and hasattr(view, "on_image_downloaded"):
            cb = view.on_image_downloaded
        try:
            download_missing_custom_game_images(on_done=cb)
        except Exception:
            pass

    def _open_add_game_tab(self):
        """Open the Add Game card-grid picker as a (detachable) tab."""
        from gui_qt.add_game_view import AddGameView
        from Utils.game_helpers import _load_games, _GAMES
        _load_games()   # refresh registry (populates _GAMES with ALL games)
        page = AddGameView(dict(_GAMES),
                           on_select=self._on_add_game_select,
                           on_add=self._on_add_game_add)
        self._tabs.open_tab(page, self.tr("Add game"), key="add_game")
        # Pull down any custom-game banner images still missing on disk (e.g.
        # handlers synced on a previous run but their images never fetched).
        self._download_custom_game_images(page)

    def _ensure_nexus_api(self):
        """Build the shared NexusAPI from saved OAuth tokens (idempotent) and
        kick off a background validate() to learn the username. Returns the API
        or None (not logged in / connection failed). Reused by the footer and
        the Nexus browser tab so there's a single instance whose passively
        captured rate limits the footer can read."""
        if getattr(self, "_nexus_api", None) is not None:
            return self._nexus_api
        from Nexus.nexus_oauth import load_oauth_tokens, clear_oauth_tokens, OAuthRefreshError
        from Nexus.nexus_api import NexusAPI
        tokens = load_oauth_tokens()
        if tokens is None:
            return None
        try:
            self._nexus_api = NexusAPI.from_oauth(tokens)
        except OAuthRefreshError as exc:
            self._append_log(f"[nexus] api init failed: {exc}")
            # A dead (rotated-out / revoked) refresh token wedges every future
            # launch - clear it so the next attempt starts from a clean login.
            # A transient network failure leaves the token in place to retry.
            if exc.token_revoked:
                clear_oauth_tokens()
                self._append_log("[nexus] cleared expired login - please log in again")
                self._notify(
                    self.tr("Your Nexus session expired - please log in again "
                            "(Nexus ▸ Login to Nexus)."),
                    "warning",
                )
            return None
        except Exception as exc:
            self._append_log(f"[nexus] api init failed: {exc}")
            return None
        # Validate off-thread (one rate-limited call; result cached 5 min).
        import threading

        def _worker():
            name = None
            try:
                user = self._nexus_api.validate()
                name = user.name
                # Record premium status so the collection-install premium gate
                # has a last-known fallback if its own validate() errors (GH#278).
                try:
                    from Utils.ui_config import save_nexus_last_premium
                    save_nexus_last_premium(bool(getattr(user, "is_premium", False)))
                except Exception:
                    pass
            except Exception as exc:
                self._append_log(f"[nexus] validate failed: {exc}")
            self._nexus_validated.emit(name)
        threading.Thread(target=_worker, daemon=True).start()
        return self._nexus_api

    def _on_nexus_validated(self, name):
        """Worker reported the validated username (or None) - update the footer."""
        if name:
            self._append_log(f"[nexus] logged in as {name}")
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(name)

    def _open_onboarding_tab(self):
        """Open first-run onboarding as a fullscreen detachable tab (like the
        Nexus browser). Any dismissal - Skip, Add-a-Game, tab X, detach-close -
        persists onboarding_complete=1 via the view's destroyed handler."""
        if self._tabs.has_key("onboarding"):
            self._tabs.focus_key("onboarding")
            return
        from gui_qt.onboarding_view import OnboardingView
        view = OnboardingView(
            on_login=self._nexus_login_sso,
            on_add_game=self._open_add_game_tab,
            on_done=self._finish_onboarding,
            already_logged_in=self._ensure_nexus_api() is not None,
            on_language_change=self._apply_language,
            on_sync_languages=self._sync_languages_now,
        )
        self._onboarding_view = view
        # ANY dismissal (Skip, Add-Game, tab X, detach-close) tears the view
        # down → mark onboarding done + drop the ref.
        view.destroyed.connect(self._on_onboarding_destroyed)
        self._tabs.open_tab(view, self.tr("Welcome"), key="onboarding")

    def _finish_onboarding(self):
        """Called by the view's on_done (Skip / Add-a-Game). Just close the tab;
        the destroyed handler persists the flag."""
        self._tabs.close_tab("onboarding")

    def _on_onboarding_destroyed(self, *_):
        from Utils.ui_config import save_onboarding_complete
        try:
            save_onboarding_complete(True)
        except Exception:
            pass
        self._onboarding_view = None

    # ---- NXM protocol handling ("Download with Manager") -----------------

    def _handle_nxm_argv(self):
        """Check sys.argv for an nxm:// link and kick off a download once the
        window has finished building."""
        from PySide6.QtCore import QTimer
        from Nexus.nxm_handler import nxm_log, nxm_url_from_argv
        nxm_url = nxm_url_from_argv()
        if not nxm_url:
            return
        nxm_log("Fresh instance: processing NXM link after window build")
        QTimer.singleShot(500, lambda: self._process_nxm_link(nxm_url))

    def _handle_ror2mm_argv(self):
        """Check sys.argv for a ror2mm:// link and kick off a download once the
        window has finished building."""
        from PySide6.QtCore import QTimer
        from Nexus.nxm_handler import nxm_log
        from Thunderstore.ror2mm_handler import ror2mm_url_from_argv
        url = ror2mm_url_from_argv()
        if not url:
            return
        nxm_log("Fresh instance: processing ror2mm link after window build")
        QTimer.singleShot(500, lambda: self._process_ror2mm_link(url))

    def _receive_ror2mm(self, url: str):
        """UI thread: a ror2mm:// link arrived over IPC. Raise the window so
        the user sees the download start."""
        from Nexus.nxm_handler import nxm_log
        nxm_log("ror2mm link reached UI thread of running instance")
        self._append_log("[thunderstore] received install link from browser")
        # Re-assert our handler registration once per session for the same
        # reason the NXM path does: the click may have been routed here by a
        # stale .desktop belonging to a different install variant.
        if not getattr(self, "_ror2mm_reregistered", False):
            self._ror2mm_reregistered = True
            import threading

            def _rereg():
                from Thunderstore.ror2mm_handler import Ror2mmHandler
                try:
                    Ror2mmHandler.register()
                except Exception as exc:
                    nxm_log(f"ror2mm re-register after IPC receive failed: {exc}")

            threading.Thread(target=_rereg, daemon=True,
                             name="ror2mm-rereg").start()
        try:
            self.setWindowState(
                self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        self._process_ror2mm_link(url)

    def _process_ror2mm_link(self, url: str):
        """Handle a ror2mm:// link - download a Thunderstore package.

        Unlike nxm://, the link carries no game identifier, so the package
        installs into the CURRENTLY SELECTED game. That is a deliberate
        product decision: clicking install while on another game's profile is
        unlikely, and a wrong-profile install is harmless and reversible.
        No login/API key is needed - Thunderstore downloads are public.
        """
        from Nexus.nxm_handler import nxm_log
        from Thunderstore.ror2mm_handler import parse_ror2mm_url
        nxm_log(f"Processing ror2mm link: {url}")

        try:
            link = parse_ror2mm_url(url)
        except ValueError as exc:
            nxm_log(f"Bad ror2mm:// URL - {exc}")
            self._append_log(f"[thunderstore] bad ror2mm:// URL - {exc}")
            self._notify(self.tr("Received a malformed Thunderstore link."),
                         "warning")
            return

        game = self._gs.game
        if game is None or not game.is_configured():
            self._append_log(
                "[thunderstore] no configured game selected - "
                f"cannot install {link.full_name}")
            self._notify(
                self.tr("Select and configure a game before installing "
                        "Thunderstore mods."), "warning")
            return

        self._append_log(
            f"[thunderstore] resolving {link.full_name} "
            f"for '{self._gs.game_name}'…")
        self._notify(self.tr("Checking Thunderstore dependencies…"), "info")

        import threading

        def _resolve_worker():
            from Thunderstore.thunderstore_requirements import (
                filter_already_installed, resolve_dependencies)
            from Utils.mod_copy import resolve_target_staging
            from gui_qt.safe_emit import safe_emit

            # Resolve the transitive dependency graph. Thunderstore pins exact
            # versions on every package, so this is precise rather than the
            # best-effort matching the Nexus requirements code has to do.
            try:
                res = resolve_dependencies(link)
            except Exception as exc:
                self._op_log.emit(
                    f"[thunderstore] dependency resolution failed ({exc}) - "
                    "installing the requested mod only")
                res = None

            wanted = list(res.packages) if res is not None else []
            skipped = []
            if res is not None:
                for missing in res.failed:
                    self._op_log.emit(
                        f"[thunderstore] could not look up {missing} - skipped")
                if res.truncated:
                    self._op_log.emit(
                        "[thunderstore] dependency graph too large - "
                        "only the first packages were resolved")
                try:
                    staging = Path(resolve_target_staging(
                        self._gs.game, Path(self._gs.profile_dir())))
                    wanted, skipped = filter_already_installed(wanted, staging)
                except Exception as exc:
                    self._op_log.emit(
                        f"[thunderstore] could not check installed mods ({exc})"
                        " - installing everything")
            for pkg in skipped:
                self._op_log.emit(
                    f"[thunderstore] {pkg.package_id} already installed - skipped")

            safe_emit(self._ror2mm_resolved, (link, res, wanted))

        threading.Thread(target=_resolve_worker, daemon=True,
                         name="ror2mm-resolve").start()

    def _on_ror2mm_resolved(self, payload):
        """UI thread: the dependency graph is known - confirm, then download.

        Dependencies are never installed silently: when the graph pulls in
        anything beyond the requested mod the user gets a modal listing every
        package with a per-item checkbox, so installing extras is opt-out.
        """
        link, res, wanted = payload

        root = None
        deps = []
        for pkg in wanted:
            if pkg.is_root:
                root = pkg
            else:
                deps.append(pkg)

        if root is None or not deps:
            # Nothing extra to install (or resolution failed) - go straight to
            # the download with whatever we have.
            self._start_ror2mm_downloads(link, wanted)
            return

        conflicts = list(getattr(res, "conflicts", []) or [])

        def _decided(chosen):
            if chosen is None:
                self._append_log(
                    f"[thunderstore] install of {link.full_name} cancelled")
                self._notify(self.tr("Install cancelled."), "info")
                return
            dropped = len(deps) - (len(chosen) - 1)
            if dropped > 0:
                self._append_log(
                    f"[thunderstore] {dropped} dependenc"
                    f"{'y' if dropped == 1 else 'ies'} skipped by the user - "
                    "the mod may not work without them")
            self._start_ror2mm_downloads(link, chosen)

        from gui_qt.thunderstore_deps_overlay import ThunderstoreDepsOverlay
        ThunderstoreDepsOverlay.show_over(
            self, root=root, dependencies=deps, conflicts=conflicts,
            on_done=_decided)

    def _start_ror2mm_downloads(self, link, packages):
        """Download every package the user accepted, dependencies first."""
        self._append_log(
            f"[thunderstore] downloading {len(packages) or 1} package(s) "
            f"into '{self._gs.game_name}'…")
        self._notify(self.tr("Downloading mod from Thunderstore…"), "info")
        dl_key = self._new_dl_key()
        self._nexus_download_progress(dl_key, "", 0, 0)   # show popup immediately

        import threading

        def _worker():
            from Thunderstore.ror2mm_handler import Ror2mmLink
            from Thunderstore.thunderstore_download import download_package
            from Utils.config_paths import get_download_cache_dir_for_game
            from gui_qt.safe_emit import safe_emit

            dest = get_download_cache_dir_for_game(self._gs.game_name or "")
            downloads = []      # (result, Ror2mmLink, info-dict)

            wanted = list(packages)
            if not wanted:
                # Resolution failed entirely - fall back to the single package
                # the link asked for so a transient API error still installs.
                wanted = [None]

            total = len(wanted)
            for idx, pkg in enumerate(wanted, 1):
                if pkg is None:
                    sub_link, expected, info = link, 0, None
                else:
                    sub_link = Ror2mmLink(
                        namespace=pkg.namespace, name=pkg.name,
                        version=pkg.version, host=link.host)
                    expected = pkg.file_size
                    info = {
                        "full_version_name": pkg.full_name,
                        "download_url": pkg.download_url,
                        "description": pkg.description,
                        "website_url": pkg.website_url,
                        "icon_url": pkg.icon_url,
                        "size": pkg.file_size,
                        "dependencies": pkg.dependencies,
                    }
                label = f"{sub_link.full_name}.zip"
                if total > 1:
                    self._op_log.emit(
                        f"[thunderstore] downloading ({idx}/{total}) {label}")
                result = download_package(
                    sub_link, dest_dir=dest, expected_size=expected,
                    progress_cb=lambda d, t, _l=label: safe_emit(
                        self._req_install_prog, dl_key, _l, int(d), int(t)))
                downloads.append((result, sub_link, info))

            safe_emit(self._ror2mm_download_done, (downloads, link, dl_key))

        threading.Thread(target=_worker, daemon=True,
                         name="ror2mm-download").start()

    def _on_ror2mm_download_done(self, payload):
        """UI thread: the Thunderstore downloads finished - install them.

        *payload* carries every package in the resolved graph, ordered
        dependencies-first, so installing the list in order satisfies each
        mod's requirements before it lands.
        """
        downloads, link, dl_key = payload
        self._nexus_download_progress(dl_key, "", 0, -1)   # hide this card

        good = [(r, l, i) for (r, l, i) in downloads if r.success and r.file_path]
        for result, sub_link, _info in downloads:
            if not (result.success and result.file_path):
                self._append_log(
                    f"[thunderstore] download failed for {sub_link.full_name} "
                    f"- {result.error}")

        if not good:
            err = downloads[0][0].error if downloads else "no packages resolved"
            self._notify(
                self.tr("Thunderstore download failed - {0}").format(err), "error")
            return

        game = self._gs.game
        if game is None or not game.is_configured():
            self._append_log(
                f"[thunderstore] downloaded {len(good)} archive(s) - no configured "
                "game selected; install manually from Downloads.")
            self._notify(
                self.tr("Downloaded - no game selected; see Downloads tab."),
                "warning")
            return

        deps = len(good) - 1
        if deps > 0:
            self._append_log(
                f"[thunderstore] installing {link.full_name} "
                f"with {deps} dependenc{'y' if deps == 1 else 'ies'}")

        paths = [str(r.file_path) for (r, _l, _i) in good]
        # Stamp each archive onto the exact folder that archive installed. The
        # path→folder result map avoids guessing from the batch's successful
        # names when another dependency failed or was renamed.
        def _stamp(_ok, _total, _names, installed):
            records = [(str(result.file_path), sub_link, info)
                       for result, sub_link, info in good]
            self._stamp_thunderstore_install_results(records, installed)

        self._deliver_download(paths, on_all_done=_stamp)

    def _stamp_thunderstore_install_results(self, records, installed):
        """Stamp exact archive→folder install results with Thunderstore meta.

        ``records`` contains ``(archive_path, Ror2mmLink, version_info)``. A
        deferred interactive installer is retained until its final folder is
        reported by ``_on_wizard_finish_done``; failed archives are never
        guessed from another package's successful name.
        """
        installed = dict(installed or {})
        for archive_path, link, info in records:
            archive_path = str(archive_path)
            folder_name = installed.get(archive_path)
            if folder_name:
                self._stamp_thunderstore_meta(link, info, folder_name)
            elif archive_path in getattr(self, "_install_handoff_paths", set()):
                self._pending_thunderstore_meta[archive_path] = (link, info)
            else:
                self._append_log(
                    f"[thunderstore] {link.full_name} was not installed by this "
                    "batch - metadata not stamped.")

    def _install_thunderstore_entries(self, entries, game, profile_dir,
                                      control, callbacks):
        """Download + install a profile import's Thunderstore mods.

        Runs on the collection-install worker thread BEFORE the Nexus pipeline,
        for two reasons. install_local_bundle restores the exporter's
        modlist.txt and then drops every row whose folder is missing from disk,
        so the mods have to be staged by then or the authored load order loses
        them. And their priority keys have to be merged into the orchestrator's
        install_order, which it builds as it starts.

        No dependency resolution and no opt-out modal, unlike a ror2mm:// click:
        the manifest already lists every dependency as its own entry, pinned to
        the version the exporter had. Re-resolving could only pull in DIFFERENT
        versions than the profile being reproduced.

        Returns ``(order, installed, failures)`` - ``order`` is the
        ``(array_index, folder)`` pairs for what actually staged.
        """
        from Thunderstore.ror2mm_handler import Ror2mmLink
        from Thunderstore.thunderstore_download import (
            download_package, fetch_version_info)
        from Utils.config_paths import get_download_cache_dir_for_game
        from Utils.mod_copy import resolve_target_staging
        from Utils.mod_install import install_collection_archive

        log = callbacks.on_log if callbacks is not None else self._op_log.emit
        order: list = []
        failures: list = []
        installed = 0
        total = len(entries)
        dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
        try:
            staging_root = Path(resolve_target_staging(game, Path(profile_dir)))
        except Exception as exc:
            log(f"[thunderstore] no staging folder for the import: {exc}")
            return [], 0, [(e.get("full_name", "?"), str(exc)) for e in entries]

        for idx, entry in enumerate(entries, 1):
            if control is not None and control.stop.is_set():
                break
            full_name = entry.get("full_name") or entry.get("name") or "?"
            if callbacks is not None:
                callbacks.on_status(
                    f"Thunderstore {idx}/{total}: {full_name}")
                callbacks.on_progress(float(idx - 1) / max(total, 1))
            try:
                link = Ror2mmLink(namespace=entry["namespace"],
                                  name=entry["package"],
                                  version=entry["version"])
            except (ValueError, KeyError) as exc:
                log(f"[thunderstore] skipping {full_name} - bad package id ({exc})")
                failures.append((full_name, "bad package id"))
                continue

            result = download_package(
                link, dest_dir=dest,
                expected_size=int(entry.get("file_size") or 0),
                cancel=(control.stop if control is not None else None))
            if getattr(result, "cancelled", False):
                break
            if not (result.success and result.file_path):
                log(f"[thunderstore] download failed for {full_name} "
                    f"- {result.error}")
                failures.append((full_name, result.error or "download failed"))
                continue

            # Best effort: the manifest carries no description/dependency list,
            # and without them the update checker and a later dependency walk
            # lose fidelity. A failure here still installs the mod.
            info = None
            try:
                info = fetch_version_info(link)
            except Exception:
                info = None
            if not isinstance(info, dict):
                info = {"full_version_name": full_name,
                        "download_url": link.download_url,
                        "size": int(entry.get("file_size") or 0)}

            # prebuilt_meta stays None ON PURPOSE: synthesising a NexusModMeta
            # here would stamp a Nexus identity onto a Thunderstore mod and
            # drag it into the missing-requirements checker (which filters on
            # mod_id > 0). See _check_nexus_flags_after_install.
            folder = None
            try:
                folder = install_collection_archive(
                    str(result.file_path), game, Path(profile_dir),
                    log_fn=log,
                    preferred_name=(entry.get("logical")
                                    or entry.get("name") or ""),
                    prebuilt_meta=None,
                    skip_index_update=True,
                    cancel=(control.stop if control is not None else None))
            except Exception as exc:
                log(f"[thunderstore] install failed for {full_name} - {exc}")
            if not folder:
                failures.append((full_name, "install failed"))
                continue

            self._stamp_thunderstore_meta(link, info, folder,
                                          staging_root=staging_root)
            order.append((int(entry.get("array_index") or 0), folder))
            installed += 1

        if failures:
            log(f"[thunderstore] {len(failures)} package(s) did NOT install:")
            for name, why in failures:
                log(f"    • {name} - {why}")
        return order, installed, failures

    def _thunderstore_staging(self):
        """Staging root for the current profile, or None."""
        game = self._gs.game
        if game is None or not game.is_configured():
            return None
        try:
            from Utils.mod_copy import resolve_target_staging
            return Path(resolve_target_staging(
                game, Path(self._gs.profile_dir())))
        except Exception:
            return None

    def _open_thunderstore_version_tab(self, mod_name: str):
        """Open the Thunderstore version picker as a plugins-panel tab.

        Separate from the Nexus Change Version tab: that view is built around
        the Nexus API (per-file model, premium/free split, expiring links),
        none of which Thunderstore has.
        """
        from Thunderstore.thunderstore_meta import read_meta
        staging = self._thunderstore_staging()
        if staging is None:
            self._notify(self.tr("No configured game selected."), "warning")
            return
        try:
            meta = read_meta(staging / mod_name / "meta.ini")
        except Exception as exc:
            self._append_log(f"[thunderstore] could not read {mod_name}: {exc}")
            return
        if not meta.package_id:
            self._notify(
                self.tr("'{0}' isn't a Thunderstore mod.").format(mod_name),
                "warning")
            return

        if self._tabs.has_key("thunderstore_version"):
            self._tabs.close_tab("thunderstore_version")

        def _install(namespace: str, name: str, version: str):
            self._process_ror2mm_link(
                f"ror2mm://v1/install/thunderstore.io/"
                f"{namespace}/{name}/{version}/")

        from gui_qt.thunderstore_version_view import ThunderstoreVersionView
        view = ThunderstoreVersionView(
            mod_name, meta,
            install_fn=_install,
            on_close=self._close_thunderstore_version_tab,
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, self.tr("Change Version"), self._plugins_panel_stack,
            key="thunderstore_version")

    def _close_thunderstore_version_tab(self):
        """Close the Thunderstore version tab and refresh the modlist flags."""
        if self._tabs.has_key("thunderstore_version"):
            self._tabs.close_tab("thunderstore_version")
        if getattr(self, "_install_running", False):
            return
        self._refresh_modlist_flags()

    def _check_thunderstore_updates(self, names=None):
        """Thunderstore-only update check (right-click subset or all).

        Separate from the combined Check Updates so a Thunderstore mod can be
        checked without touching the Nexus/mod.io paths.
        """
        staging = self._thunderstore_staging()
        if staging is None:
            self._notify(self.tr("No configured game selected."), "warning")
            return
        subset = set(names) if names else None
        n = len(subset) if subset else self.tr("all")
        toast = self._notify(
            self.tr("Checking Thunderstore for updates ({0})…").format(n),
            "info", sticky=True)

        import threading

        def _worker():
            from Thunderstore.thunderstore_update_checker import (
                check_for_updates)
            from gui_qt.safe_emit import safe_emit
            try:
                res = check_for_updates(
                    staging, only_names=subset,
                    progress_cb=lambda m: self._op_log.emit(
                        f"[thunderstore] {m}"))
            except Exception as exc:
                self._op_log.emit(
                    f"[thunderstore] update check failed: {exc}")
                res = None
            safe_emit(self._ts_updates_ready, (res, toast, subset))

        threading.Thread(target=_worker, daemon=True,
                         name="ts-check-updates").start()

    def _on_thunderstore_updates_ready(self, payload):
        """UI thread: a Thunderstore-only update check finished."""
        res, toast, subset = payload

        def _finish(text, state):
            if toast is not None:
                toast.dismiss(text, state=state)
            else:
                self._notify(text, state)

        if res is None:
            _finish(self.tr("Thunderstore update check failed - see the log."),
                    "error")
            return
        updates = [u for u in res if getattr(u, "has_update", False)
                   and not getattr(u, "unknown", False)]
        unknown = [u for u in res if getattr(u, "unknown", False)]
        if updates:
            parts = [self.tr("{0} update(s) available").format(len(updates))]
            if unknown:
                parts.append(self.tr("{0} unknown").format(len(unknown)))
            _finish(", ".join(parts) + ".", "warning")
        elif unknown:
            _finish(self.tr("{0} package(s) could not be checked.")
                    .format(len(unknown)), "warning")
        else:
            _finish(self.tr("All Thunderstore mods are up to date."), "info")
        self._refresh_modlist_flags(subset)

    def _update_thunderstore_mod(self, mod_name: str):
        """Update-flag click on a Thunderstore mod → confirm, then reinstall it
        at the latest version.

        Downloads need no key, so the newest version is fetched and installed
        through the same ror2mm pipeline as a fresh install (which also
        resolves any dependencies the new version added).
        """
        from Thunderstore.thunderstore_meta import read_meta
        from Utils.mod_copy import resolve_target_staging

        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        try:
            staging = Path(resolve_target_staging(
                game, Path(self._gs.profile_dir())))
            meta = read_meta(staging / mod_name / "meta.ini")
        except Exception as exc:
            self._append_log(f"[thunderstore] could not read {mod_name}: {exc}")
            return
        if not meta.package_id:
            self._notify(self.tr("This mod has no Thunderstore metadata."),
                         "warning")
            return
        target = meta.latest_version or ""
        if not target:
            self._notify(
                self.tr("Run Check Updates first to find the latest version."),
                "info")
            return

        def _confirmed(ok):
            if not ok:
                return
            from Thunderstore.ror2mm_handler import Ror2mmLink
            link = Ror2mmLink(namespace=meta.namespace, name=meta.name,
                              version=target)
            self._append_log(
                f"[thunderstore] updating {meta.package_id} "
                f"{meta.version} → {target}")
            self._process_ror2mm_link(link.raw or
                                      f"ror2mm://v1/install/{link.host}/"
                                      f"{link.namespace}/{link.name}/{target}/")

        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Update mod"),
            self.tr("Update {0} from {1} to {2}?").format(
                meta.package_id, meta.version or "?", target),
            _confirmed, confirm_label=self.tr("Update"), danger=False)

    def _stamp_thunderstore_meta(self, link, info, installed_name=None,
                                 staging_root=None):
        """Write the [thunderstore] meta.ini section for a just-installed mod.

        *installed_name* is the folder reported for this archive by the install
        pipeline. It remains authoritative when a partial batch succeeds or the
        user renames a package during installation.

        *staging_root* overrides where that folder is looked up. Required by the
        profile-import pass: it stages into a profile the game has NOT been
        switched to yet, so the default (the ACTIVE profile's staging) would
        stamp a different profile's mod - or nothing at all.
        """
        from pathlib import Path

        from Thunderstore.thunderstore_meta import (
            build_meta_from_link, read_meta, write_meta)
        try:
            game = self._gs.game
            if game is None:
                return
            if staging_root is not None:
                staging = Path(staging_root)
            else:
                from Utils.mod_copy import resolve_target_staging
                staging = Path(resolve_target_staging(
                    game, Path(self._gs.profile_dir())))
            if not staging.is_dir():
                return

            meta = build_meta_from_link(link, info)
            # Resolve the community for the record. The link never carries it,
            # so this is informational only - it does NOT gate the install.
            if not meta.community:
                try:
                    from Thunderstore.thunderstore_download import resolve_communities
                    slugs = resolve_communities(link)
                    meta.community = slugs[0] if slugs else ""
                except Exception:
                    pass

            target = staging / str(installed_name or "")
            if not installed_name or not target.is_dir():
                self._append_log(
                    f"[thunderstore] installed folder for {link.full_name} not "
                    "found - meta.ini not stamped.")
                return

            meta_path = target / "meta.ini"
            existing = read_meta(meta_path)
            if existing.package_id and existing.package_id != meta.package_id:
                self._append_log(
                    f"[thunderstore] '{target.name}' already carries metadata "
                    f"for {existing.package_id} - not overwriting.")
                return
            write_meta(meta_path, meta)
            self._append_log(
                f"[thunderstore] stamped {meta.full_name} → {target.name}/meta.ini")
        except Exception as exc:
            self._append_log(f"[thunderstore] could not stamp meta.ini: {exc}")

    def _start_nxm_ipc(self):
        """Start the IPC server so this (running) instance receives NXM links
        handed off by later instances. The callback fires on a worker thread,
        so it emits _nxm_received to marshal onto the UI thread. A periodic
        self-heal re-binds our sockets if another instance or a /tmp cleaner
        removed them - otherwise every later 'Download with Mod Manager' click
        would open a new window instead of reaching us."""
        from PySide6.QtCore import QTimer
        from Nexus.nxm_handler import NxmIPC
        from gui_qt.safe_emit import safe_emit

        def _on_nxm(url: str):
            # One socket carries both schemes (the IPC payload is just a URL
            # string), so route by scheme here rather than standing up a
            # second server that would duplicate all the bind/heal logic.
            if (url or "").lower().startswith("ror2mm://"):
                safe_emit(self._ror2mm_received, url)
            else:
                safe_emit(self._nxm_received, url)

        NxmIPC.start_server(_on_nxm)

        def _heal():
            # Worker thread: ensure_bound probes sockets (blocking I/O) and is
            # internally locked against concurrent runs + shutdown.
            import threading
            threading.Thread(
                target=NxmIPC.ensure_bound, daemon=True, name="nxm-ipc-heal"
            ).start()

        self._nxm_ipc_timer = QTimer(self)
        self._nxm_ipc_timer.setInterval(30_000)
        self._nxm_ipc_timer.timeout.connect(_heal)
        self._nxm_ipc_timer.start()

    def _receive_nxm(self, nxm_url: str):
        """UI thread: handle an NXM link (from --nxm at startup or delivered via
        IPC from a second instance). Raise the window so the user sees it."""
        from Nexus.nxm_handler import nxm_log
        nxm_log("NXM link reached UI thread of running instance")
        self._append_log("[nexus] received NXM link from browser")
        # The click may have been routed here by a *different install
        # variant's* sender (stale .desktop registration). Re-register
        # ourselves - once per session, in the background (it shells out to
        # xdg-mime & co) - so the instance that actually handles downloads
        # owns the nxm:// handler and the next click launches this variant's
        # sender directly.
        if not getattr(self, "_nxm_reregistered", False):
            self._nxm_reregistered = True
            import threading

            def _rereg():
                from Nexus.nxm_handler import NxmHandler, nxm_log
                try:
                    NxmHandler.register()
                except Exception as exc:
                    nxm_log(f"Re-register after IPC receive failed: {exc}")

            threading.Thread(target=_rereg, daemon=True, name="nxm-rereg").start()
        try:
            self.setWindowState(
                self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        self._process_nxm_link(nxm_url)

    def _match_game_for_domain(self, game_domain: str):
        """Return the configured game that should receive *game_domain*.

        The current game wins when it explicitly accepts the domain. Otherwise
        prefer a game's primary domain before considering additional domains;
        this keeps a Skyrim link routed to Skyrim unless the user is already on
        a compatible game such as Enderal.
        """
        from Utils.game_helpers import _GAMES
        wanted = (game_domain or "").strip().lower()
        current = self._gs.game
        if (current is not None and current.is_configured()
                and current.accepts_nexus_domain(wanted)):
            return (self._gs.game_name, current)
        for name, game in _GAMES.items():
            primary = (getattr(game, "nexus_game_domain", "") or "").strip().lower()
            if primary == wanted and game.is_configured():
                return (name, game)
        for name, game in _GAMES.items():
            if game.is_configured() and game.accepts_nexus_domain(wanted):
                return (name, game)
        return None

    def _process_nxm_link(self, nxm_url: str):
        """Handle an nxm:// link - download a mod or open a collection."""
        from Nexus.nxm_handler import parse_nxm_url, nxm_log
        nxm_log(f"Processing NXM link: {nxm_url}")
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            nxm_log("NXM link ignored - not logged in to Nexus")
            self._append_log("[nexus] NXM link ignored - not logged in")
            return

        try:
            mod_link, coll_link = parse_nxm_url(nxm_url)
        except ValueError as exc:
            nxm_log(f"Bad nxm:// URL - {exc}")
            self._append_log(f"[nexus] bad nxm:// URL - {exc}")
            self._notify(self.tr("Received a malformed NXM link."), "warning")
            return

        if coll_link is not None:
            self._process_nxm_collection_link(coll_link)
            return

        link = mod_link
        # If the Nexus browser / Change Version tab is watching the download
        # folders for this mod (non-premium install), the user picked
        # 'Download with Mod Manager' instead - this flow installs it, so
        # stop the watch (double install).
        for _attr in ("_nexus_view", "_change_version_view"):
            _v = getattr(self, _attr, None)
            if _v is not None:
                try:
                    _v.cancel_manual_watch(link.mod_id, link.game_domain)
                except Exception:
                    pass
        # Same for a pending non-premium reinstall watch of this mod.
        try:
            self.cancel_app_manual_watch(link.mod_id, link.game_domain)
        except Exception:
            pass
        self._append_log(
            f"[nexus] downloading mod {link.mod_id} file {link.file_id} "
            f"from {link.game_domain}…")

        matched = self._match_game_for_domain(link.game_domain)
        if matched and matched[0] != self._gs.game_name:
            self._on_game_changed(matched[0])
            self._append_log(f"[nexus] switched to game '{matched[0]}'")

        self._notify(self.tr("Downloading mod from Nexus…"), "info")
        dl_key = self._new_dl_key()
        self._nexus_download_progress(dl_key, "", 0, 0)   # show popup immediately

        import threading

        def _worker():
            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game
            from gui_qt.safe_emit import safe_emit
            # Fetch mod + file info for accurate meta.ini (best-effort).
            mod_info = file_info = None
            try:
                mod_info, file_info = api.get_mod_and_file_info_graphql(
                    link.game_domain, link.mod_id, link.file_id)
            except Exception as exc:
                self._op_log.emit(
                    f"[nexus] could not fetch mod info ({exc}) - meta partial")
            dest_name = matched[0] if matched else (self._gs.game_name or "")
            dest = get_download_cache_dir_for_game(dest_name)
            dl_label = getattr(file_info, "file_name", "") or ""
            downloader = NexusDownloader(api, download_dir=dest)
            result = downloader.download_from_nxm(
                link, dest_dir=dest,
                known_file_name=dl_label,
                progress_cb=lambda d, t: safe_emit(
                    self._req_install_prog, dl_key, dl_label, int(d), int(t)))
            safe_emit(self._nxm_download_done,
                      (result, mod_info, file_info, dl_key))

        threading.Thread(target=_worker, daemon=True, name="nxm-download").start()

    def _on_nxm_download_done(self, payload):
        """UI thread: an NXM download finished. Install it (via the shared
        install pipeline) with a prebuilt meta from the link data."""
        result, mod_info, file_info, dl_key = payload
        self._nexus_download_progress(dl_key, "", 0, -1)   # hide this download's card
        if not (result.success and result.file_path):
            self._append_log(f"[nexus] download failed - {result.error}")
            self._notify(self.tr("Nexus download failed - {0}").format(result.error), "error")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._append_log(
                f"[nexus] downloaded {result.file_name} - no configured game "
                "selected; install manually from Downloads.")
            self._notify(self.tr("Downloaded - no game selected; see Downloads tab."),
                         "warning")
            return

        from Nexus.nexus_meta import build_meta_from_download
        meta = None
        try:
            meta = build_meta_from_download(
                game_domain=result.game_domain, mod_id=result.mod_id,
                file_id=result.file_id, archive_name=result.file_name,
                mod_info=mod_info, file_info=file_info)
        except Exception as exc:
            self._append_log(f"[nexus] could not build metadata: {exc}")

        path = str(result.file_path)
        metas = {path: meta} if meta is not None else None
        self._deliver_download([path], metas=metas)

    def _process_nxm_collection_link(self, coll_link):
        """Switch to the matching game and open the collection's detail tab."""
        self._append_log(
            f"[nexus] opening collection '{coll_link.slug}' "
            f"from {coll_link.game_domain}")
        matched = self._match_game_for_domain(coll_link.game_domain)
        if not matched:
            self._notify(
                self.tr("No configured game for Nexus domain '{0}'.").format(coll_link.game_domain), "warning")
            self._append_log(
                f"[nexus] no configured game for domain "
                f"'{coll_link.game_domain}' - cannot open collection")
            return
        if getattr(matched[1], "collections_disabled", False):
            self._notify(
                self.tr("Collections aren't supported for '{0}'.").format(matched[0]), "warning")
            return
        if matched[0] != self._gs.game_name:
            self._on_game_changed(matched[0])
            self._append_log(f"[nexus] switched to game '{matched[0]}'")

        from Nexus.nexus_api import NexusCollection
        collection = NexusCollection(
            slug=coll_link.slug, game_domain=coll_link.game_domain)
        revision = coll_link.revision_id or None
        self._open_collection_detail_tab(collection, revision_number=revision)

    def _open_game_on_nexus(self):
        """Open the current game's Nexus Mods page in the browser."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        from Utils.xdg import open_url
        open_url(f"https://www.nexusmods.com/{domain}",
                 log_fn=self._append_log)

    def _thunderstore_community(self) -> str:
        """The current game's Thunderstore community slug ("" if none)."""
        game = self._gs.game
        if game is None:
            return ""
        return (getattr(game, "thunderstore_community", "") or "").strip()

    def _open_game_on_thunderstore(self):
        """Open the current game's Thunderstore community page."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        community = self._thunderstore_community()
        if not community:
            self._notify(
                self.tr("'{0}' has no Thunderstore page.").format(game.name),
                "warning")
            return
        from Thunderstore.thunderstore_api import community_url
        from Utils.xdg import open_url
        open_url(community_url(community), log_fn=self._append_log)

    def _auto_identify_thunderstore(self, names):
        """Stamp Thunderstore metadata on mods just installed by any path.

        The ror2mm pipeline stamps its own installs, but the Downloads tab /
        Install Mod / drag-drop paths go straight through _install_paths and
        would otherwise leave a Thunderstore package with only a [General]
        section - no update checking, no flag, no Actions submenu.

        Cheap and silent: only folders carrying a manifest.json AND no existing
        [thunderstore] section are looked up, so a non-Thunderstore install
        costs one stat() and nothing else.
        """
        wanted = [str(n) for n in (names or []) if n]
        if not wanted:
            return
        community = self._thunderstore_community()
        if not community:
            return
        staging = self._thunderstore_staging()
        if staging is None:
            return

        import threading

        def _worker():
            from Thunderstore.ror2mm_handler import Ror2mmLink
            from Thunderstore.thunderstore_identify import (
                identify_mod, read_manifest, stamp_identified)
            from Thunderstore.thunderstore_meta import read_meta
            from Thunderstore.thunderstore_requirements import (
                filter_already_installed, resolve_dependencies)
            from gui_qt.safe_emit import safe_emit
            done = []
            missing: list = []      # ResolvedPackage, deepest-first
            seen_missing: set = set()
            for name in wanted:
                folder = staging / name
                try:
                    if not folder.is_dir():
                        continue
                    if read_meta(folder / "meta.ini").package_id:
                        continue        # already stamped (ror2mm install)
                    if read_manifest(folder) is None:
                        continue        # not a Thunderstore package
                    res = identify_mod(folder, community)
                    if not (res.matched and stamp_identified(folder, res)):
                        continue
                    version = res.manifest.version if res.manifest else ""
                    done.append((name, res.package_id, version))
                    # A hand-installed package brings no dependencies with it,
                    # so resolve its graph and collect whatever is absent. The
                    # mod itself is already on disk - only the gaps are offered.
                    if not version:
                        continue
                    graph = resolve_dependencies(Ror2mmLink(
                        namespace=res.namespace,
                        name=res.manifest.name, version=version))
                    todo, _have = filter_already_installed(
                        graph.packages, staging, keep_root=False)
                    for pkg in todo:
                        if pkg.is_root:
                            continue    # the mod we just installed
                        if pkg.package_id in seen_missing:
                            continue
                        seen_missing.add(pkg.package_id)
                        missing.append(pkg)
                except Exception as exc:
                    self._op_log.emit(
                        f"[thunderstore] could not identify '{name}': {exc}")
            if done or missing:
                safe_emit(self._ts_auto_identified, (done, missing))

        threading.Thread(target=_worker, daemon=True,
                         name="ts-auto-identify").start()

    def _on_thunderstore_auto_identified(self, payload):
        """UI thread: an install was recognised as a Thunderstore package.

        When the manifest declares dependencies that aren't installed, offer
        them through the SAME opt-out modal a ror2mm install uses - a manually
        installed package brings none of its requirements with it, so without
        this the mod silently doesn't work.
        """
        done, missing = payload
        for name, package_id, version in done or []:
            self._append_log(
                f"[thunderstore] recognised '{name}' as {package_id} {version}")
        if done:
            self._refresh_modlist_flags([n for n, _p, _v in done])
        if not missing:
            return

        for pkg in missing:
            self._append_log(
                f"[thunderstore] missing dependency: {pkg.full_name}")

        # The installed mod stands in as the modal's "requested" row: it is
        # already on disk, so only the dependencies get checkboxes.
        root_label = ", ".join(p for _n, p, _v in done) or self.tr("this mod")
        root = _InstalledRoot(root_label)
        from Thunderstore.ror2mm_handler import Ror2mmLink

        def _decided(chosen):
            if chosen is None:
                self._append_log(
                    "[thunderstore] dependency install declined")
                return
            picked = [p for p in chosen if not isinstance(p, _InstalledRoot)]
            if not picked:
                return
            self._append_log(
                f"[thunderstore] installing {len(picked)} missing "
                "dependenc" + ("y" if len(picked) == 1 else "ies"))
            # `picked` is non-empty, so the link is only a fallback for the
            # empty case - point it at the first package for a sane log line.
            first = picked[0]
            self._start_ror2mm_downloads(
                Ror2mmLink(namespace=first.namespace, name=first.name,
                           version=first.version), picked)

        from gui_qt.thunderstore_deps_overlay import ThunderstoreDepsOverlay
        ThunderstoreDepsOverlay.show_over(
            self, root=root, dependencies=missing, conflicts=[],
            on_done=_decided)

    def _identify_thunderstore_mods(self):
        """Adopt manually-installed Thunderstore mods.

        Thunderstore has no hash lookup (unlike Nexus's MD5 → mod/file id), but
        every package ships a manifest.json that survives installation. Reading
        it gives the exact name + version; the namespace it omits is resolved
        from the API. Stamping the result makes update checking, the update
        flag and the Thunderstore Actions submenu work for a hand-installed mod.
        """
        staging = self._thunderstore_staging()
        if staging is None:
            self._notify(self.tr("No configured game selected."), "warning")
            return
        community = self._thunderstore_community()
        toast = self._notify(self.tr("Identifying Thunderstore mods…"),
                             "info", sticky=True)

        import threading

        def _worker():
            from Thunderstore.thunderstore_identify import identify_all
            from gui_qt.safe_emit import safe_emit
            try:
                results = identify_all(
                    staging, community,
                    progress_cb=lambda m: self._op_log.emit(
                        f"[thunderstore] {m}"))
            except Exception as exc:
                self._op_log.emit(
                    f"[thunderstore] identify failed: {exc}")
                results = None
            safe_emit(self._ts_identify_ready, (results, toast))

        threading.Thread(target=_worker, daemon=True,
                         name="ts-identify").start()

    def _on_thunderstore_identify_ready(self, payload):
        """UI thread: the identify pass finished."""
        results, toast = payload

        def _finish(text, state):
            if toast is not None:
                toast.dismiss(text, state=state)
            else:
                self._notify(text, state)

        if results is None:
            _finish(self.tr("Identifying mods failed - see the log."), "error")
            return
        matched = [r for r in results if r.matched]
        missed = [r for r in results if not r.matched]
        for r in matched:
            note = (self.tr(" (several teams publish this name)")
                    if r.ambiguous else "")
            self._append_log(
                f"[thunderstore] '{r.mod_name}' → {r.package_id} "
                f"{r.manifest.version if r.manifest else ''}{note}")
        for r in missed:
            self._append_log(
                f"[thunderstore] '{r.mod_name}' not identified - {r.reason}")

        if not results:
            _finish(self.tr("No unidentified Thunderstore mods found."), "info")
        elif matched:
            self._refresh_modlist_flags()
            text = self.tr("Identified {0} mod(s).").format(len(matched))
            if missed:
                text += " " + self.tr("{0} could not be matched.").format(
                    len(missed))
            _finish(text, "success")
        else:
            _finish(self.tr("Could not identify any of the {0} mod(s) found.")
                    .format(len(results)), "warning")

    def _open_thunderstore_browser_tab(self):
        """Open the Thunderstore browser as a detachable tab.

        No login or API key needed - Thunderstore reads are public. The
        community guard is unreachable while the header button is hidden for
        unsupported games, but the menu action could be reached another way
        (a detached window, a future keybinding), so keep it.
        """
        if self._tabs.has_key("thunderstore_browser"):
            self._tabs.focus_key("thunderstore_browser")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        community = self._thunderstore_community()
        if not community:
            self._notify(
                self.tr("'{0}' has no Thunderstore page.").format(game.name),
                "warning")
            return

        def _install(namespace: str, name: str, version: str):
            # Route through the normal one-click pipeline so dependency
            # resolution + the opt-out modal + meta stamping all apply.
            self._process_ror2mm_link(
                f"ror2mm://v1/install/thunderstore.io/"
                f"{namespace}/{name}/{version}/")

        from gui_qt.thunderstore_browser_view import ThunderstoreBrowserView
        view = ThunderstoreBrowserView(community, game,
                                       install_fn=_install,
                                       log_fn=self._append_log)
        self._thunderstore_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_thunderstore_view", None))
        self._tabs.open_tab(view, self.tr("Thunderstore"),
                            key="thunderstore_browser")

    def _open_nexus_browser_tab(self):
        """Open the Nexus Mods browser as a detachable tab. Needs a configured
        game with a Nexus domain and existing OAuth tokens (login UI deferred)."""
        if self._tabs.has_key("nexus_browser"):
            self._tabs.focus_key("nexus_browser")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        # Reuse the shared API (built at startup); falls back to building it now
        # if startup couldn't (e.g. the user logged in afterwards).
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            self._append_log("[nexus] no OAuth tokens - login required")
            return
        from gui_qt.nexus_browser_view import NexusBrowserView
        view = NexusBrowserView(api, domain, game,
                                install_fn=self._deliver_download,
                                log_fn=self._append_log,
                                progress_fn=self._nexus_download_progress)
        self._nexus_view = view
        # Drop the reference when the tab/window is gone so we stop refreshing it.
        view.destroyed.connect(lambda *_: setattr(self, "_nexus_view", None))
        self._tabs.open_tab(view, self.tr("Nexus"), key="nexus_browser")

    def _open_collections_tab(self):
        """Open the Nexus Collections browser as a detachable tab (view-only -
        the install/detail flow is a separate feature). Same guards as the mods
        browser: a configured game with a Nexus domain + OAuth tokens."""
        if self._tabs.has_key("collections"):
            self._tabs.focus_key("collections")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            self._append_log("[nexus] no OAuth tokens - login required")
            return
        from gui_qt.collections_browser_view import CollectionsBrowserView
        view = CollectionsBrowserView(
            api, domain, game, log_fn=self._append_log,
            on_open_detail=self._open_collection_detail_tab,
            get_profile_dir=lambda: self._gs.profile_dir(),
            on_remove_appended=self._remove_appended_collection)
        self._collections_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_collections_view", None))
        self._tabs.open_tab(view, self.tr("Collections"), key="collections")

    def _remove_appended_collection(self, record):
        """Remove an appended collection from the active profile: every mod it
        installed (meta.ini fromCollection / manifest fileid ownership - other
        collections' and manual mods are never touched) plus its
        installed_collections/ record. Confirm → daemon worker (deploy mutex)
        → _appended_col_removed → reload."""
        import threading
        from Utils.installed_collections import resolve_owned_mod_names
        game = self._gs.game
        pdir = self._gs.profile_dir()
        if game is None or pdir is None:
            return
        if self._deploy_running:
            self._notify(self.tr("A deploy or removal is already running - try again when it finishes."),
                         "warning")
            return
        if self._col_install_running:
            self._notify(self.tr("A collection install is running - try again when it finishes."),
                         "warning")
            return
        # The tab can outlive a profile switch - only act on records that still
        # exist under the CURRENT profile.
        rec_path = record.get("path")
        try:
            valid = (rec_path is not None and Path(rec_path).is_file()
                     and Path(rec_path).parent.parent.resolve()
                     == Path(pdir).resolve())
        except Exception:
            valid = False
        if not valid:
            view = getattr(self, "_collections_view", None)
            if view is not None:
                view.refresh_appended()
            return
        name = str((record.get("card") or {}).get("name")
                   or record.get("slug") or "")
        names = resolve_owned_mod_names(game, pdir, record)
        if names:
            body = self.tr("Remove '{0}' and its {1} mod(s) from this profile?\n\n"
                           "Their files are deleted from the staging folder - "
                           "this cannot be undone.").format(name, len(names))
        else:
            body = self.tr("No installed mods from '{0}' were found in this "
                           "profile.\n\nRemove the appended-collection entry?"
                           ).format(name)

        def _confirmed(ok):
            if not ok or self._deploy_running:
                return
            self._deploy_running = True

            def _worker():
                from gui_qt.safe_emit import safe_emit
                from Utils.installed_collections import remove_appended_collection
                done_ok = True
                try:
                    remove_appended_collection(
                        game, pdir, record, names,
                        log_fn=lambda m: self._op_log.emit(f"[collection] {m}"))
                except Exception as exc:
                    done_ok = False
                    self._op_log.emit(f"[collection] remove appended failed: {exc}")
                finally:
                    self._deploy_running = False
                    safe_emit(self._appended_col_removed, name, done_ok)

            threading.Thread(target=_worker, daemon=True,
                             name="appended-col-remove").start()

        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(self, self.tr("Remove appended collection"),
                                 body, _confirmed,
                                 confirm_label=self.tr("Remove"))

    def _on_appended_col_removed(self, name, ok):
        """UI thread: appended-collection removal finished - reload + toast."""
        self._reload_modlist(rescan_index=True)
        self._reload_plugins()
        view = getattr(self, "_collections_view", None)
        if view is not None:
            try:
                view.refresh_appended()
            except Exception:
                pass
        if ok:
            self._notify(self.tr("Removed appended collection '{0}'.").format(name),
                         "success")
        else:
            self._notify(self.tr("Could not remove '{0}' - see the log.").format(name),
                         "error")

    def _open_current_collection(self):
        """Open the detail tab for the collection installed in the active profile.
        No-op (with a toast) unless the active profile is a collection profile."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pdir = self._gs.profile_dir()
        from Utils.game_helpers import get_collection_url_from_profile
        url = get_collection_url_from_profile(pdir) if pdir is not None else None
        if not url:
            self._notify(self.tr("The active profile isn't a collection profile."),
                         "warning")
            return
        from Utils.collection_manifest import parse_collection_url
        from Nexus.nexus_api import NexusCollection
        slug, url_domain, rev = parse_collection_url(url)
        if not slug:
            self._notify(self.tr("Couldn't read the collection from this profile."),
                         "warning")
            return
        domain = url_domain or getattr(game, "nexus_game_domain", "") or ""
        col = NexusCollection(slug=slug, name=slug, game_domain=domain)
        self._open_collection_detail_tab(col, revision_number=rev)

    def _open_collection_detail_tab(self, collection, revision_number=None):
        """Open a collection's detail panel as a NEW detachable tab (the
        collections browser tab stays open). Card View passes no revision (latest);
        the browser's 'Open Current' passes the installed revision."""
        # Key by slug when the id is 0 (Open Current builds a bare NexusCollection).
        key = f"collection_detail_{collection.id or collection.slug}"
        if revision_number is not None:
            key = f"{key}_r{revision_number}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        from gui_qt.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            revision_number=revision_number)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        title = f"Collection: {collection.name or collection.slug}"
        self._tabs.open_tab(view, title, key=key)
        # NXM / "Open Current" build a bare NexusCollection that only knows the
        # slug, so the title shows the id-like slug until the detail loads. Update
        # the tab caption once the real collection name resolves.
        view.title_resolved.connect(
            lambda name, k=key: self._tabs.set_tab_title(k, f"Collection: {name}"))

    # ---- Collection install (premium auto / non-premium manual) ----------
    def _install_collection(self, collection, detail_view, chosen, skipped,
                            intent="install"):
        """Start a collection install: check premium (premium → automatic
        downloads, non-premium → manual per-mod download prompts), create a new
        profile, then run the neutral orchestrator on a daemon thread with every
        callback marshaled through a Signal (UI-thread slots update the
        overlay). Deferred FOMOD/BAIN mods block the worker on a wizard via
        _col_fomod / _col_bain (same handshake as _make_exists_cb)."""
        import threading
        if self._col_install_running:
            self._notify(self.tr("A collection install is already running."), "warning")
            return
        # A collection install swaps the shared game to a fresh profile - don't
        # start one while a detached-wizard staging job is still touching the
        # shared game (the wrong-profile hazard).
        if getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._notify(self.tr("An install is finishing - try the collection "
                                 "again in a moment."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        from Utils import profile_export
        # Read the imported manifest BEFORE the Nexus gate: a Thunderstore-only
        # or fully-bundled import needs no account, and the Thunderstore-only
        # games have no Nexus domain to log in against (see manifest_needs_nexus).
        _local_manifest_early = getattr(detail_view, "_local_manifest", None)
        api = self._ensure_nexus_api()
        if api is None:
            if (_local_manifest_early is None
                    or profile_export.manifest_needs_nexus(_local_manifest_early)):
                self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."),
                             "warning")
                return
        # Latched ONCE for the whole run. Re-reading it later would let a toggle
        # mid-flight mix the two modes - worst case an Update that has already
        # removed the outgoing mods then downloads their replacements instead of
        # installing them, leaving the profile gutted.
        self._col_download_only = self._download_only_active()
        dl_path = getattr(detail_view, "download_link_path", "") or ""
        revision_number = getattr(detail_view, "_revision_number", None)
        if revision_number is None and hasattr(detail_view, "_resolved_viewing_revision"):
            revision_number = detail_view._resolved_viewing_revision()
        # True collection size (installed/uncompressed) shown in the detail header -
        # surfaced in the install overlay's aggregate label (the compressed download
        # total is much smaller and misrepresents the collection's real size).
        total_size = int(getattr(detail_view, "_total_size", 0) or 0)
        # Manifest rule: this collection must be installed as a NEW profile.
        recommend_new = bool(getattr(detail_view, "_recommend_new_profile", False))
        # Local-manifest import (Import profile): a parsed manifest to feed straight
        # into the orchestrator + a local .amethyst whose bundled mods/profile files
        # are extracted after install. Both empty for a normal Nexus collection.
        local_manifest = getattr(detail_view, "_local_manifest", None)
        bundle_zip = getattr(detail_view, "_bundle_zip_path", "") or ""
        # Nexus install: reuse the manifest the detail view already fetched
        # (Tk _collection_schema_cache parity). Only when its revision matches
        # the one being installed - otherwise the orchestrator downloads the
        # right one itself. Prevents a second CDN fetch at install time, whose
        # silent failure loses every FOMOD/BAIN auto-selection.
        if local_manifest is None:
            fetched = getattr(detail_view, "_fetched_manifest", None)
            fetched_rev = getattr(detail_view, "_fetched_manifest_rev", None)
            if fetched and (revision_number is None
                            or fetched_rev == revision_number):
                local_manifest = fetched
        mods = detail_view.install_mods(skipped)
        # Unticked optionals, taken from the FULL mod list (mods above already
        # excludes them) - the orchestrator removes these from an existing
        # profile on continue/append/update.
        skipped_mods = detail_view.skipped_optional_mods(skipped)
        # A Thunderstore-only import has no Nexus mods at all, but plenty to
        # install - the separate Thunderstore pass handles those.
        _ts_pending = bool(
            local_manifest is not None
            and profile_export.thunderstore_entries(local_manifest))
        if not mods and not _ts_pending:
            self._notify(self.tr("This collection has no installable mods."), "info")
            return
        slug = getattr(collection, "slug", "") or ""
        domain = (getattr(collection, "game_domain", "")
                  or getattr(game, "nexus_game_domain", "") or "")
        # Off-site mods (manual downloads) from the detail view's manifest -
        # remembered so the completion handler can remind the user about them.
        offsite = list(getattr(detail_view, "_offsite", None) or [])

        # Premium gate runs off-thread (validate() is rate-limited); on success it
        # creates the profile + starts the pipeline, all marshaled back to the UI.
        self._col_install_running = True
        self._notify(self.tr("Checking Nexus account…"), "info")

        def _premium_worker():
            from Utils.ui_config import (load_nexus_last_premium,
                                         save_nexus_last_premium)
            if api is None:
                # No account, and the gate above proved none is needed (a
                # Thunderstore-only / bundled import). There is nothing to
                # download from Nexus, so neither mode applies - go straight
                # through as "not premium" rather than calling validate() on
                # None. The manual-download producer is never reached: it only
                # runs for mods in `mods`, which is empty of Nexus entries.
                is_premium = False
            else:
                try:
                    user = api.validate()
                    is_premium = bool(getattr(user, "is_premium", False))
                    try:
                        save_nexus_last_premium(is_premium)
                    except Exception:
                        pass
                except Exception as exc:
                    # GH#278: a transient validate() failure (network hiccup,
                    # rate limit) must not silently demote a premium user to
                    # manual mode - fall back to the last validated status.
                    last = load_nexus_last_premium()
                    is_premium = bool(last)
                    self._op_log.emit(
                        f"[collection] premium check failed: {exc} - using "
                        f"last-known status "
                        f"({'premium' if is_premium else 'not premium'})")
            try:
                from Utils.ui_config import load_force_manual_install
                force_manual = bool(load_force_manual_install())
            except Exception:
                force_manual = False
            self._col_finished.emit(
                "_premium", {"ok": is_premium and not force_manual,
                             "manual": not (is_premium and not force_manual),
                             "collection": collection, "domain": domain,
                             "slug": slug, "dl_path": dl_path,
                             "revision": revision_number, "mods": mods,
                             "total_size": total_size,
                             "skipped": set(skipped),
                             "skipped_mods": skipped_mods,
                             "game": game, "api": api,
                             "recommend_new": recommend_new, "intent": intent,
                             "local_manifest": local_manifest,
                             "bundle_zip": bundle_zip,
                             "offsite": offsite})

        threading.Thread(target=_premium_worker, daemon=True,
                         name="col-premium").start()

    def _choose_collection_mode(self, info):
        """UI thread: show the New/Append (or Continue) mode overlay, then start
        the pipeline with the chosen mode. Mirrors Tk _continue_install_collection:
        if this exact collection+revision URL is already in a profile → Continue;
        else → New/Append."""
        from Utils.game_helpers import (
            find_profile_with_collection_url, _profiles_for_game)
        game = info["game"]; slug = info["slug"]; domain = info["domain"]
        rev = info["revision"]
        url = f"https://www.nexusmods.com/games/{domain}/collections/{slug}"
        if rev is not None:
            url += f"/revisions/{rev}"
        try:
            existing = find_profile_with_collection_url(game.name, url)
        except Exception:
            existing = None

        def _done(result):
            if result is None:
                self._col_install_finished()
                self._notify(self.tr("Collection install cancelled."), "info")
                return
            info["mode_result"] = result
            self._start_collection_pipeline(info)

        if existing:
            from gui_qt.collection_mode_overlay import ContinueOverlay
            ContinueOverlay.show_over(self, existing, _done)
        else:
            from gui_qt.collection_mode_overlay import ModeOverlay
            try:
                profiles = _profiles_for_game(game.name)
            except Exception:
                profiles = []
            # Profile Groups can't take a collection append - a group is a
            # merged view of its members; append into a member instead.
            try:
                from Utils.profile_groups import is_group
                _pr = game.get_profile_root() / "profiles"
                profiles = [p for p in profiles if not is_group(_pr / p)]
            except Exception:
                pass
            # Manifest rule (collectionConfig.recommendNewProfile) → disable Append.
            force_new = bool(info.get("recommend_new"))
            ModeOverlay.show_over(self, profiles, _done, force_new_profile=force_new)

    def _resume_collection_install(self, info):
        """UI thread: RESUME a paused install - clear the paused flag + registry,
        then continue into the profile that already claims this collection.
        Already-installed mods skip by file_id (Tk parity)."""
        game = info["game"]; slug = info["slug"]
        from Utils.game_helpers import find_profile_with_collection_slug
        from Utils.profile_state import write_collection_install_paused
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_finished()
            self._notify(self.tr("Could not find the paused profile."), "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        try:
            write_collection_install_paused(profile_dir, False)
        except Exception:
            pass
        try:
            from gui_qt.collection_detail_view import _PAUSED_COLLECTIONS
            _PAUSED_COLLECTIONS.discard(slug)
        except Exception:
            pass
        self._refresh_open_collection_buttons()
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _run_collection_update(self, info):
        """UI thread: UPDATE an installed collection to the viewed revision.
        Port of Tk _on_update_collection + _apply_collection_update: compute the
        diff, confirm via UpdateOverlay, remove stale/bundled/patched mods, stash
        an order-preserving update_context, then continue-install."""
        game = info["game"]; slug = info["slug"]; mods = info["mods"]
        from Utils.game_helpers import find_profile_with_collection_slug
        try:
            pname = find_profile_with_collection_slug(game.name, slug)
        except Exception:
            pname = None
        if not pname:
            self._col_install_finished()
            self._notify(self.tr("Could not find the installed collection profile."), "error")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname

        # Guard: the collection's profile must be the active one (cross-profile
        # removal is out of scope, matching Tk).
        active = getattr(game, "_active_profile_dir", None)
        if active is None or Path(active).resolve() != profile_dir.resolve():
            self._col_install_finished()
            self._notify(self.tr("Switch to profile '{0}' first, then Update.").format(pname),
                         "warning")
            return

        # Compute the diff (old cached manifest vs the new mod list).
        import json as _json
        from Utils.modlist import read_modlist
        from Utils.collection_diff import diff_collection
        from Utils.profile_state import read_collection_revision
        old_manifest = {}
        try:
            mf = profile_dir / "collection.json"
            if mf.is_file():
                old_manifest = _json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            old_manifest = {}
        modlist_path = profile_dir / "modlist.txt"
        try:
            installed_names_lower = {
                e.name.lower() for e in read_modlist(modlist_path)
                if not e.is_separator} if modlist_path.is_file() else set()
        except Exception:
            installed_names_lower = set()
        try:
            staging_path = game.get_effective_mod_staging_path()
        except Exception:
            staging_path = None
        try:
            diff = diff_collection(
                old_manifest=old_manifest, new_mods=mods,
                staging_path=Path(staging_path) if staging_path else Path("."),
                installed_names_lower=installed_names_lower,
                collection_slug=slug)
        except Exception as exc:
            self._col_install_finished()
            self._notify(self.tr("Could not compute update diff: {0}").format(exc), "error")
            return

        # Human labels for the update/add buckets via the new mod list.
        fid_to_name = {getattr(m, "file_id", 0): (getattr(m, "mod_name", "")
                       or f"file {getattr(m, 'file_id', 0)}") for m in mods}
        to_update_labels = [
            f"{old} → {fid_to_name.get(fid, fid)}"
            for old, fid in zip(diff.to_update_old, diff.to_update_new_fids)]
        to_add_labels = [fid_to_name.get(fid, str(fid))
                         for fid in diff.to_install_fids]

        from_rev = read_collection_revision(profile_dir)
        to_rev = info["revision"]

        def _done(apply_it):
            if not apply_it:
                self._col_install_finished()
                self._notify(self.tr("Collection update cancelled."), "info")
                return
            self._apply_collection_update(info, profile_dir, pname, slug, diff)

        from gui_qt.collection_update_overlay import UpdateOverlay
        UpdateOverlay.show_over(
            self, profile_name=pname, from_rev=from_rev, to_rev=to_rev,
            to_remove=list(diff.to_remove), to_update=to_update_labels,
            to_add=to_add_labels, orphans=list(diff.orphans), on_done=_done)

    def _apply_collection_update(self, info, profile_dir, pname, slug, diff):
        """UI thread: user confirmed - scan staging for this collection's
        bundled/patched mods in a worker (meta.ini per mod = too slow for the
        UI thread on big collections), then finish on the UI thread."""
        import threading
        from gui_qt.safe_emit import safe_emit
        game = info["game"]

        def worker():
            import configparser
            from Utils.modlist import read_modlist
            try:
                snapshot = list(read_modlist(profile_dir / "modlist.txt")) \
                    if (profile_dir / "modlist.txt").is_file() else []
            except Exception:
                snapshot = []

            # Bundled/patched mods for THIS collection are force-reinstalled
            # (their contents/patches may have changed) - port of Tk 1974-2001.
            slug_lower = (slug or "").strip().lower()
            bundled_or_patched: set[str] = set()
            try:
                staging_path = Path(game.get_effective_mod_staging_path())
            except Exception:
                staging_path = None
            if staging_path is not None and staging_path.is_dir() and slug_lower:
                for mod_dir in staging_path.iterdir():
                    if not mod_dir.is_dir():
                        continue
                    meta = mod_dir / "meta.ini"
                    if not meta.is_file():
                        continue
                    cp = configparser.ConfigParser()
                    try:
                        cp.read(meta, encoding="utf-8")
                    except Exception:
                        continue
                    if not cp.has_section("General"):
                        continue
                    if (cp["General"].get("fromCollection") or "").strip().lower() != slug_lower:
                        continue
                    if (cp["General"].getboolean("fromCollectionBundled", fallback=False)
                            or cp["General"].getboolean("fromCollectionPatched", fallback=False)):
                        bundled_or_patched.add(mod_dir.name.lower())
            safe_emit(self._col_update_scan_done,
                      {"info": info, "profile_dir": profile_dir, "pname": pname,
                       "diff": diff, "snapshot": snapshot,
                       "bundled_or_patched": bundled_or_patched})

        threading.Thread(target=worker, daemon=True,
                         name="col-update-scan").start()

    def _finish_collection_update(self, payload):
        """UI thread: remove stale + bundled/patched mods, build
        update_context, then continue-install."""
        from Utils import mod_remove
        info = payload["info"]; profile_dir = payload["profile_dir"]
        pname = payload["pname"]; diff = payload["diff"]
        snapshot = payload["snapshot"]
        game = info["game"]

        all_remove_lower = ({n.lower() for n in diff.removals}
                            | payload["bundled_or_patched"])
        removed_lower: set[str] = set()
        if all_remove_lower:
            to_remove_names = [e.name for e in snapshot
                               if not e.is_separator
                               and e.name.lower() in all_remove_lower]
            if to_remove_names:
                try:
                    mod_remove.remove_mods(
                        game, profile_dir, to_remove_names,
                        log_fn=lambda m: self._append_log(str(m)))
                    removed_lower = {n.lower() for n in to_remove_names}
                except Exception as exc:
                    self._col_install_finished()
                    self._notify(self.tr("Update failed during removal: {0}").format(exc), "error")
                    return

        filtered_snapshot = [
            e for e in snapshot
            if e.is_separator or e.name.lower() not in removed_lower]

        info["update_context"] = {"snapshot": filtered_snapshot, "schema_order": {}}
        info["mode_result"] = ("continue", pname, False, False)
        self._start_collection_pipeline(info)

    def _refresh_open_collection_buttons(self):
        """Refresh the Install/Update/Resume/Download button on every open
        collection detail tab, detached windows included."""
        try:
            from gui_qt.collection_detail_view import CollectionDetailView
            from PySide6.QtWidgets import QApplication
            seen = set()
            # A detached tab lives in its own top-level window, so findChildren
            # on the main window alone would miss it.
            for root in [self, *QApplication.topLevelWidgets()]:
                for view in root.findChildren(CollectionDetailView):
                    if id(view) in seen:
                        continue
                    seen.add(id(view))
                    try:
                        view._update_install_btn_state()
                    except Exception:
                        pass
        except Exception:
            pass

    def _start_collection_pipeline(self, info):
        """UI thread: premium confirmed - create the profile, open the overlay,
        and launch the orchestrator on a daemon thread."""
        import threading
        game = info["game"]; api = info["api"]; collection = info["collection"]
        slug = info["slug"]; domain = info["domain"]
        revision_number = info["revision"]; mods = info["mods"]
        skipped = info["skipped"]; dl_path = info["dl_path"]
        total_size = int(info.get("total_size", 0) or 0)
        self._col_install_slug = slug
        update_context = info.get("update_context")
        # Local-manifest import: feed the parsed manifest to the orchestrator (no
        # Nexus fetch) and remember the .amethyst so its bundled mods + profile
        # files are extracted once the Nexus mods finish installing.
        local_manifest = info.get("local_manifest")
        self._col_bundle_zip = info.get("bundle_zip") or ""
        self._col_offsite = list(info.get("offsite") or [])

        # Resolve the install mode chosen in the mode overlay (default: new).
        mode_result = info.get("mode_result") or ("new", None, False, False)
        mode, append_profile_name, ov_existing, skip_existing = mode_result
        # Mods whose optional was unticked - the orchestrator removes these from
        # an existing profile (append/continue). NB: must come from the full
        # detail-view list; *mods* already excludes them.
        skipped_mods = list(info.get("skipped_mods") or [])

        from Utils.game_helpers import _create_profile, _profiles_for_game
        from Utils.profile_state import write_collection_optional_skipped
        import re as _re

        # overwrite_existing is None for new/continue (fresh modlist write); a bool
        # for append (preserves existing load order via _append_reconcile_modlist).
        overwrite_existing = None
        skip_existing_arg = False

        # Download only: no profile is created, stamped or touched. _create_profile
        # is never called, so there is nothing for a cancel to delete either.
        dl_only = bool(getattr(self, "_col_download_only", False))
        if dl_only:
            profile_dir = None
            self._col_created_profile = False
            self._col_profile_name = ""
            update_context = None
        elif mode == "new":
            raw = collection.name or slug or "Collection"
            base = _re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_") or "Collection"
            profile_name = (f"{base}_Rev{revision_number}"[:64]
                            if revision_number is not None else base[:64])
            _existing = set(_profiles_for_game(game.name))
            _pn = profile_name
            _i = 2
            while _pn in _existing:
                _pn = f"{profile_name}_{_i}"[:64]
                _i += 1
            profile_name = _pn
            try:
                profile_dir = Path(_create_profile(
                    game.name, profile_name, profile_specific_mods=True))
            except Exception as exc:
                self._col_install_finished()
                self._notify(self.tr("Could not create profile: {0}").format(exc), "error")
                return
            # New/continue claim the collection: record URL + revision.
            self._stamp_collection_profile(
                profile_dir, domain, slug, revision_number, skipped)
        else:
            # append / continue → install into an existing profile.
            profile_dir = game.get_profile_root() / "profiles" / append_profile_name
            if not profile_dir.is_dir():
                self._col_install_finished()
                self._notify(self.tr("Profile '{0}' not found.").format(append_profile_name), "error")
                return
            if mode == "append":
                overwrite_existing = bool(ov_existing)
                skip_existing_arg = bool(skip_existing)
                # Append doesn't claim the collection URL; still record skipped set.
                try:
                    write_collection_optional_skipped(profile_dir, skipped)
                except Exception:
                    pass
            else:  # continue
                self._stamp_collection_profile(
                    profile_dir, domain, slug, revision_number, skipped)

        # GH#278: a cancelled install may delete the profile ONLY when this
        # run created it (new mode). Continue/append/resume/update target a
        # pre-existing profile that must survive a cancel.
        if not dl_only:
            self._col_created_profile = (mode == "new")
            self._col_profile_name = profile_dir.name

        # Card display fields for the appended-collections record
        # (installed_collections/<slug>.json - see Utils.installed_collections).
        append_card_info = None
        if overwrite_existing is not None:
            import dataclasses
            try:
                append_card_info = dataclasses.asdict(collection)
            except Exception:
                append_card_info = {
                    k: getattr(collection, k, None)
                    for k in ("id", "slug", "name", "summary", "user_name",
                              "endorsements", "total_downloads", "mod_count",
                              "tile_image_url", "game_domain")}
            if not append_card_info.get("game_domain"):
                append_card_info["game_domain"] = domain

        old_profile_dir = getattr(game, "_active_profile_dir", None)

        # Overlay + control. Manual (non-premium) installs get the per-mod
        # download-prompt card; premium gets the download/extract progress
        # overlay. Both expose the same slot surface to the _on_col_* handlers.
        from Utils.collection_install import CollectionInstallControl
        control = CollectionInstallControl()
        self._col_install_control = control
        manual_mode = bool(info.get("manual"))
        title = (f"Downloading collection: {collection.name or slug}" if dl_only
                 else f"Installing collection: {collection.name or slug}")
        # A download-only run has no profile to record a resume point in, so it
        # offers Cancel only (which keeps the archives) instead of a Pause that
        # could never be resumed.
        _on_pause = None if dl_only else self._on_col_pause_clicked
        if manual_mode:
            from gui_qt.collection_manual_overlay import CollectionManualOverlay
            self._col_install_overlay = CollectionManualOverlay.show_over(
                self, collection.name or slug,
                "" if dl_only else profile_dir.name, len(mods),
                control.manual_queue, on_pause=_on_pause,
                on_cancel=self._on_col_cancel_clicked)
        else:
            from gui_qt.collection_install_overlay import CollectionInstallOverlay
            from Utils.ui_config import load_download_speed_limit
            self._col_install_overlay = CollectionInstallOverlay.show_over(
                self, title, on_pause=_on_pause,
                on_cancel=self._on_col_cancel_clicked,
                limit_mbps=load_download_speed_limit(),
                on_limit_change=self._on_col_limit_changed)

        callbacks = self._build_collection_callbacks()
        # Thunderstore entries from an imported manifest - installed by their
        # own pass (see _install_thunderstore_entries), never by the Nexus
        # orchestrator. Empty for a normal Nexus collection.
        from Utils import profile_export as _pe_ts
        ts_entries = (_pe_ts.thunderstore_entries(local_manifest)
                      if isinstance(local_manifest, dict) else [])
        self._col_ts_installed = 0
        self._col_ts_failed = 0
        # Append mode (a share code imported into an existing profile) decides
        # what to reposition by diffing against the mods present BEFORE the run.
        # The Thunderstore pass registers its mods in modlist.txt as it stages
        # them, so that snapshot has to be taken here - reading it inside the
        # orchestrator would see them and treat them as the user's own mods,
        # leaving them wherever they landed instead of at their code position.
        ts_append_pre_existing = None
        if ts_entries and overwrite_existing is not None:
            try:
                from Utils.modlist import read_modlist as _rm_snap
                _ml = Path(profile_dir) / "modlist.txt"
                ts_append_pre_existing = {
                    e.name.lower() for e in _rm_snap(_ml)
                    if not e.is_separator} if _ml.is_file() else set()
            except Exception:
                ts_append_pre_existing = None

        def _worker():
            from Utils.collection_install import run_collection_install
            from Nexus.nexus_download import NexusDownloader
            from Utils.config_paths import get_download_cache_dir_for_game
            downloader = NexusDownloader(
                api, download_dir=get_download_cache_dir_for_game(game.name or ""))
            # Thunderstore mods first: the bundled modlist.txt restored later
            # drops rows whose folder isn't staged yet, and the orchestrator
            # needs their priorities as it builds install_order.
            ts_order = []
            if ts_entries and not dl_only:
                ts_order, ts_installed, ts_failed = \
                    self._install_thunderstore_entries(
                        ts_entries, game, profile_dir, control, callbacks)
                self._col_ts_installed = ts_installed
                self._col_ts_failed = len(ts_failed)
                if control.stop.is_set():
                    self._col_finished.emit(
                        "cancelled",
                        {"profile_dir": str(profile_dir) if profile_dir else ""})
                    return
            try:
                run_collection_install(
                    game=game, api=api, downloader=downloader, mods=mods,
                    download_link_path=dl_path, profile_dir=profile_dir,
                    old_profile_dir=old_profile_dir, collection_slug=slug,
                    collection_domain=info.get("domain") or "",
                    revision_number=revision_number, collection_total_size=total_size,
                    skipped_fids=skipped,
                    skipped_mods=skipped_mods, overwrite_existing=overwrite_existing,
                    skip_existing=skip_existing_arg, update_context=update_context,
                    collection_schema_cache=local_manifest,
                    manual_mode=manual_mode, download_only=dl_only,
                    append_card_info=append_card_info,
                    local_bundle_zip=info.get("bundle_zip") or "",
                    preinstalled_order=ts_order,
                    append_pre_existing=ts_append_pre_existing,
                    callbacks=callbacks, control=control)
            except Exception as exc:
                import traceback
                self._op_log.emit(f"[collection] install error: {exc}\n"
                                  f"{traceback.format_exc()}")
                self._col_finished.emit(
                    "cancelled",
                    {"profile_dir": str(profile_dir) if profile_dir else ""})

        threading.Thread(target=_worker, daemon=True, name="col-install").start()

    def _stamp_collection_profile(self, profile_dir, domain, slug, revision_number,
                                  skipped):
        """Record the collection URL + revision + skipped-optionals on a profile
        that claims this collection (new / continue modes)."""
        from Utils.game_helpers import save_collection_url_to_profile
        from Utils.profile_state import (
            write_collection_revision, write_collection_optional_skipped)
        try:
            url = f"https://www.nexusmods.com/games/{domain}/collections/{slug}"
            if revision_number is not None:
                url += f"/revisions/{revision_number}"
            save_collection_url_to_profile(profile_dir, url)
        except Exception as exc:
            self._append_log(f"[collection] could not save URL: {exc}")
        try:
            if revision_number is not None:
                write_collection_revision(profile_dir, revision_number)
            # Always write - an empty set clears a previously saved selection.
            write_collection_optional_skipped(profile_dir, set(skipped or ()))
        except Exception:
            pass

    def _build_collection_callbacks(self):
        """Build a CollectionInstallCallbacks whose every field is a single
        Signal.emit - NO callback touches a widget (all UI mutation happens in the
        connected UI-thread slots). Matches the thread-safety rule that cost a
        segfault before."""
        from Utils.collection_install import CollectionInstallCallbacks
        return CollectionInstallCallbacks(
            on_status=lambda m: self._col_status.emit(m),
            on_progress=lambda v: self._col_progress.emit(v),
            on_agg_download=lambda c, t, s: self._col_agg.emit(int(c), int(t), float(s)),
            on_display_total=lambda n: self._col_display_total.emit(int(n)),
            on_dl_mod_start=lambda f, n, s: self._col_dl.emit("start", (f, n, s)),
            on_dl_mod_update=lambda f, c, t: self._col_dl.emit("update", (f, c, t)),
            on_dl_mod_finish=lambda f: self._col_dl.emit("finish", f),
            on_extract_queue=lambda f, n: self._col_extract.emit("queue", (f, n)),
            on_extract_add=lambda f, n: self._col_extract.emit("add", (f, n)),
            on_extract_update=lambda f, c, t: self._col_extract.emit("update", (f, c, t)),
            on_extract_remove=lambda f: self._col_extract.emit("remove", f),
            on_row_installed=lambda f: self._col_row.emit(int(f)),
            on_manual_mod=lambda d: self._col_manual.emit(dict(d)),
            on_log=lambda m: self._op_log.emit(str(m)),
            on_done=lambda i, s, t, p: self._col_finished.emit("done", (i, s, t, p)),
            on_paused=lambda i, p: self._col_finished.emit("paused", (i, p)),
            # str(None) would be the truthy "None" - every download-only guard
            # downstream keys on profile_dir being falsy.
            on_cancelled=lambda pd: self._col_finished.emit(
                "cancelled", {"profile_dir": str(pd) if pd else ""}),
            resolve_fomod=self._make_col_fomod_cb(),
            resolve_bain=self._make_col_bain_cb(),
        )

    # ---- collection progress slots (UI thread) ---------------------------
    def _on_col_status(self, text):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_status(text)

    def _on_col_progress(self, value):
        pass   # overlay drives from the aggregate bytes; per-mod count unused here

    def _on_col_agg(self, cur, tot, mbps):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_agg(cur, tot, mbps)

    def _on_col_display_total(self, n):
        if self._col_install_overlay is not None:
            self._col_install_overlay.set_display_total(n)

    def _on_col_dl(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "start":
            ov.dl_start(*payload)
        elif verb == "update":
            ov.dl_update(*payload)
        elif verb == "finish":
            ov.dl_finish(payload)

    def _on_col_extract(self, verb, payload):
        ov = self._col_install_overlay
        if ov is None:
            return
        if verb == "queue":
            ov.extract_queue(*payload)
        elif verb == "add":
            ov.extract_add(*payload)
        elif verb == "update":
            ov.extract_update(*payload)
        elif verb == "remove":
            ov.extract_remove(payload)

    def _on_col_row(self, file_id):
        if self._col_install_overlay is not None:
            self._col_install_overlay.row_installed(file_id)

    def _on_col_manual(self, payload):
        """Manual-mode producer prompts the next mod (on_manual_mod)."""
        ov = self._col_install_overlay
        if ov is not None and hasattr(ov, "update_mod"):
            ov.update_mod(payload)

    # ---- deferred FOMOD / BAIN blocking handshake ------------------------
    def _make_col_fomod_cb(self):
        """resolve_fomod for the orchestrator: runs on the WORKER thread, asks the
        UI to open the FOMOD wizard, and BLOCKS until the user Finishes/Cancels."""
        import threading

        def _cb(config, base, name, installed, active, loose, saved=None):
            holder = {"result": None}
            ev = threading.Event()
            self._col_fomod.emit({"config": config, "base": base, "name": name,
                                  "installed": installed, "active": active,
                                  "loose": loose, "saved": saved,
                                  "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_fomod_ui(self, payload):
        """UI thread: open the FOMOD wizard as a tab; unblock the worker on
        finish/cancel/close. Only ONE deferred wizard is ever open (the deferred
        loop is sequential) so there's no nesting."""
        from gui_qt.fomod_wizard_view import FomodWizardView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the wizard is up - it covers the tab
        # and would otherwise block the deferred FOMOD picker (no work is running
        # while we wait for the user, so nothing to display).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_fomod_wizard")
            self._show_col_overlay()
            ev.set()

        _sel_path = None
        _game_name = getattr(getattr(self._gs, "game", None), "name", "")
        if _game_name:
            from Utils.config_paths import get_fomod_selections_path
            _sel_path = get_fomod_selections_path(_game_name, payload["name"])
        # Merge the live plugin-panel state (see the manual-install path below):
        # plugins.txt can lag the model, so a FOMOD `state="Active"` dep wouldn't
        # match until a save/sort. Union the panel's enabled/all sets.
        _c_inst = set(payload.get("installed") or set())
        _c_act = set(payload.get("active") or set())
        try:
            pm = getattr(self, "_plugin_model", None)
            if pm is not None:
                live_all = pm.all_lower()
                live_active = pm.enabled_lower()
                _c_inst |= live_all
                _c_act = (_c_act | live_active) - (live_all - live_active)
        except Exception:
            pass
        view = FomodWizardView(payload["config"], payload["base"], payload["name"],
                               on_finish=lambda sel: _finish_ev(sel),
                               on_cancel=lambda: _finish_ev(None),
                               saved_selections=payload.get("saved"),
                               selections_path=_sel_path,
                               installed_files=_c_inst,
                               active_files=_c_act,
                               loose_files=payload.get("loose"))
        # Closing the tab (or a stop request) counts as cancel so we never hang.
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, self.tr("Install: {0}").format(payload['name']),
                            key="col_fomod_wizard")

    def _make_col_bain_cb(self):
        import threading

        def _cb(subpkgs, root, name):
            holder = {"result": None}
            ev = threading.Event()
            self._col_bain.emit({"subpkgs": subpkgs, "root": root, "name": name,
                                 "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_col_bain_ui(self, payload):
        from gui_qt.bain_picker_view import BainPickerView
        holder, ev = payload["holder"], payload["event"]
        done = {"v": False}
        # Hide the install overlay while the BAIN picker is up (see FOMOD above).
        self._hide_col_overlay()

        def _finish_ev(result):
            if done["v"]:
                return
            done["v"] = True
            holder["result"] = result
            self._tabs.close_tab("col_bain_picker")
            self._show_col_overlay()
            ev.set()

        view = BainPickerView(payload["subpkgs"], payload["root"], payload["name"],
                              on_done=lambda r: _finish_ev(r))
        view.destroyed.connect(lambda *_: _finish_ev(None))
        self._tabs.open_tab(view, self.tr("Install: {0}").format(payload['name']),
                            key="col_bain_picker")

    def _hide_col_overlay(self):
        """Hide the collection install overlay (e.g. while a deferred FOMOD/BAIN
        wizard tab is up so it isn't covered)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.hide()
            except Exception:
                pass

    def _show_col_overlay(self):
        """Re-show the collection install overlay after a deferred wizard closes
        (no-op if the install already finished + dismissed the overlay)."""
        ov = self._col_install_overlay
        if ov is not None:
            try:
                ov.show()
                ov.raise_()
            except Exception:
                pass

    def _on_col_limit_changed(self, mbps: float):
        """Overlay speed-limit spinbox changed: apply to in-flight downloads
        immediately (global token bucket) and persist for next time."""
        from Utils import bandwidth_limit
        from Utils.ui_config import save_download_speed_limit
        bandwidth_limit.set_limit_mbps(mbps)
        try:
            save_download_speed_limit(mbps)
        except Exception:
            pass

    # ---- pause / cancel --------------------------------------------------
    def _on_col_pause_clicked(self):
        ctl = self._col_install_control
        if ctl is not None:
            ctl.pause.set()
            ctl.stop.set()

    def _on_col_cancel_clicked(self):
        from gui_qt.confirm_overlay import ConfirmOverlay

        def _done(confirmed):
            if not confirmed:
                return
            ctl = self._col_install_control
            if ctl is not None:
                ctl.cancel.set()
                ctl.pause.set()
                ctl.stop.set()
            if self._col_install_overlay is not None:
                self._col_install_overlay.set_status(self.tr("Cancelling…"))

        pname = getattr(self, "_col_profile_name", "") or ""
        if not pname:
            # Download only: no profile was ever created.
            title = self.tr("Cancel download?")
            msg = self.tr("This will stop the download. Archives already "
                          "downloaded are kept in the Downloads tab.")
            confirm = self.tr("Cancel Download")
        elif getattr(self, "_col_created_profile", False):
            title = self.tr("Cancel install?")
            msg = self.tr("This will stop the install and delete the new "
                          "profile '{0}'.").format(pname)
            confirm = self.tr("Cancel Install")
        else:
            title = self.tr("Cancel install?")
            msg = self.tr("This will stop the install. Profile '{0}' and its "
                          "already-installed mods will be kept.").format(pname)
            confirm = self.tr("Cancel Install")
        ConfirmOverlay.show_over(
            self, title, msg,
            _done, confirm_label=confirm,
            cancel_label=self.tr("Keep Going"))

    # ---- completion ------------------------------------------------------
    def _on_col_finished(self, kind, payload):
        """UI thread: handle the premium gate result AND the terminal states."""
        if kind == "_premium":
            if payload.get("manual"):
                # Non-premium (or [dev] force_manual_install): same pipeline,
                # but the manual overlay + sequential wait-for-download
                # producer replace the automatic download pool.
                self._notify(self.tr("Nexus Premium not detected - manual download "
                             "mode."), "info")
            # Download only: every intent collapses to "fetch every archive".
            # Not cosmetic - 'update' routes to _run_collection_update, which
            # REMOVES mods from a real profile, and 'resume' needs a paused one.
            if getattr(self, "_col_download_only", False):
                payload["mode_result"] = ("new", None, False, False)
                payload["update_context"] = None
                self._start_collection_pipeline(payload)
                return
            # Route by intent (identical for premium and manual).
            intent = payload.get("intent", "install")
            if intent == "update":
                self._run_collection_update(payload)
            elif intent == "resume":
                self._resume_collection_install(payload)
            else:
                # Ask the user how to install (New/Append/Continue) BEFORE
                # creating any profile.
                self._choose_collection_mode(payload)
            return

        # Terminal states. _col_install_running is NOT dropped here wholesale:
        # "cancelled" still has an async cleanup worker mutating the shared
        # game (profile delete, _active_profile_dir=None) and a bundle-import
        # "done" still extracts over the profile - each leaf releases the flag
        # via _col_install_finished() only once the install is truly over, so
        # auto-deploy stays suppressed for the whole run (premium AND manual).
        self._col_install_control = None
        ov = self._col_install_overlay

        if kind == "cancelled":
            import threading
            pd = payload.get("profile_dir") if isinstance(payload, dict) else None
            if ov is not None:
                ov.set_status(self.tr("Cancelling…"))
            game = self._gs.game
            # GH#278: only a profile this run created may be deleted.
            delete_profile = bool(getattr(self, "_col_created_profile", False))

            def _cleanup_worker():
                from Utils.collection_install import cleanup_cancelled_install
                from Utils.ui_config import load_clear_archive_after_install
                try:
                    cleanup_cancelled_install(
                        game, Path(pd) if pd else None,
                        delete_profile=delete_profile,
                        # No profile → download-only run: the cache holds
                        # everything it just fetched, so never wipe it.
                        clear_cache=(bool(load_clear_archive_after_install())
                                     if pd else False),
                        log_fn=lambda m: self._op_log.emit(str(m)))
                except Exception as exc:
                    self._op_log.emit(f"[collection] cancel cleanup failed: {exc}")
                self._col_finished.emit("_cancel_cleaned", {
                    "deleted": delete_profile,
                    "no_profile": not pd,
                    "profile": Path(pd).name if pd else ""})

            threading.Thread(target=_cleanup_worker, daemon=True,
                             name="col-cancel-cleanup").start()
            return

        if kind == "_cancel_cleaned":
            self._col_install_finished()
            if ov is not None:
                ov.dismiss()
                self._col_install_overlay = None
            if isinstance(payload, dict) and payload.get("no_profile"):
                # Download-only: the switch below would yank the active profile.
                if hasattr(self, "_downloads_view"):
                    self._downloads_view.mark_dirty()
                self._notify(self.tr("Collection download cancelled - the archives "
                                     "already fetched are in the Downloads tab."),
                             "info")
                return
            # cleanup_cancelled_install left game._active_profile_dir = None;
            # re-assert now because the switch below early-returns when the
            # target profile is already the active one.
            self._gs.reassert_active_profile()
            # Switch back to a sane profile + reload: the surviving target
            # profile when it was kept (continue/append/resume/update cancel),
            # else the default profile.
            kept = (isinstance(payload, dict) and not payload.get("deleted")
                    and payload.get("profile"))
            try:
                profs = self._gs.profiles()
                target = (kept if kept and kept in profs
                          else (profs[0] if profs else None))
                if target:
                    # A cancel is not "install finished" - the teardown's own
                    # reload/rebuild must not fire an auto-deploy. The flag is
                    # consumed by the next _maybe_auto_deploy.
                    self._auto_deploy_in_progress = True
                    self._on_profile_changed(target)
            except Exception:
                pass
            self._notify(self.tr("Collection install cancelled."), "info")
            return

        if kind == "paused":
            self._col_install_finished()
            installed, profile_name = payload
            if ov is not None:
                ov.finish(self.tr("Paused - {0} installed.").format(installed))
                QTimer.singleShot(1500, self._dismiss_col_overlay)
            # A pause is not "install finished" - the switch's reload/rebuild
            # must not auto-deploy a half-installed collection profile.
            self._auto_deploy_in_progress = True
            self._select_installed_collection_profile(profile_name)
            # Register the paused slug + refresh any open detail view so its
            # button flips to "Resume" (Tk parity - _PAUSED_INSTALLS).
            slug = getattr(self, "_col_install_slug", "") or ""
            if slug:
                try:
                    from gui_qt.collection_detail_view import _PAUSED_COLLECTIONS
                    _PAUSED_COLLECTIONS.add(slug)
                except Exception:
                    pass
            self._refresh_open_collection_buttons()
            self._notify(self.tr("Install paused - {0} mod(s) installed.").format(installed), "info")
            return

        # done
        installed, skipped_n, total, profile_name = payload
        # Thunderstore mods were installed by their own pass, so the
        # orchestrator never counted them - fold them in so the summary
        # describes the whole import rather than just its Nexus half.
        _ts_ok = int(getattr(self, "_col_ts_installed", 0) or 0)
        _ts_bad = int(getattr(self, "_col_ts_failed", 0) or 0)
        if _ts_ok or _ts_bad:
            installed += _ts_ok
            skipped_n += _ts_bad
            total += _ts_ok + _ts_bad
        self._col_ts_installed = 0
        self._col_ts_failed = 0
        if not profile_name:
            # Download only: nothing installed, no profile to select.
            self._col_install_finished()
            self._col_bundle_zip = ""
            if ov is not None:
                ov.finish(self.tr("Downloaded {0}/{1} - nothing installed.").format(
                    installed, total))
                QTimer.singleShot(1500, self._dismiss_col_overlay)
            if hasattr(self, "_downloads_view"):
                self._downloads_view.mark_dirty()
            msg = self.tr("Collection downloaded - {0}/{1} archive(s). Install them "
                          "from the Downloads tab.").format(installed, total)
            if skipped_n:
                self._notify(
                    msg + self.tr(" ({0} couldn't be downloaded - see the log)").format(
                        skipped_n), "warning")
            else:
                self._notify(msg, "success")
            self._show_offsite_reminder()
            return
        # Local-manifest import: extract bundled mods + profile files from the
        # .amethyst over the freshly-installed profile, then reload. Handled on a
        # worker (unzipping can be sizeable) → reload marshaled back via a Signal.
        bundle_zip = getattr(self, "_col_bundle_zip", "") or ""
        self._col_bundle_zip = ""
        if bundle_zip and Path(bundle_zip).is_file():
            # Flag stays held - the bundle worker still writes into the profile;
            # _on_import_bundle_done releases it.
            self._finish_import_bundle(bundle_zip, profile_name, ov,
                                       installed, total, skipped_n)
            return
        # Fully finished: auto-deploy is live again, so the switch's rebuild
        # may (deliberately) deploy the freshly-installed collection.
        self._col_install_finished()
        if ov is not None:
            ov.finish(self.tr("Done - {0}/{1} installed.").format(installed, total))
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        self._select_installed_collection_profile(profile_name)
        msg = self.tr("Collection installed - {0}/{1} mod(s)").format(installed, total)
        self._notify(msg + (self.tr(" ({0} skipped)").format(skipped_n) if skipped_n else ""), "success")
        self._show_offsite_reminder()

    # ---- Import profile: local-bundle extraction -------------------------
    def _finish_import_bundle(self, bundle_zip, profile_name, ov,
                              installed, total, skipped_n):
        """Extract a local .amethyst's bundled mods + profile files into the
        just-installed profile on a worker, then reload on the UI thread."""
        import threading
        game = self._gs.game
        if game is None:
            self._col_install_finished()
            self._select_installed_collection_profile(profile_name)
            return
        profile_dir = game.get_profile_root() / "profiles" / profile_name
        if ov is not None:
            ov.set_status(self.tr("Restoring bundled mods + profile files…"))

        def _worker():
            staged = []
            try:
                # Resolve the imported profile's own mods/overwrite dirs (it's a
                # profile_specific_mods profile → <profile_dir>/mods + /overwrite).
                # Set it active first so get_effective_*_path resolves to it.
                prev = getattr(game, "_active_profile_dir", None)
                try:
                    game.set_active_profile_dir(profile_dir)
                    game.load_paths()
                    mods_dir = game.get_effective_mod_staging_path()
                    overwrite_dir = game.get_effective_overwrite_path()
                finally:
                    game.set_active_profile_dir(prev)
                    game.load_paths()
                from Utils import profile_export
                staged = profile_export.install_local_bundle(
                    bundle_zip, profile_dir, mods_dir, overwrite_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)))
            except Exception as exc:
                self._op_log.emit(f"[import] bundle extraction failed: {exc}")
            # Bundled mods carry no Nexus file ID, so the install pipeline never
            # counts them - each extracted bundle folder is an installed mod.
            self._col_import_done.emit(
                (profile_name, int(installed) + len(staged or ()),
                 int(total), int(skipped_n)))

        threading.Thread(target=_worker, daemon=True, name="import-bundle").start()

    def _on_import_bundle_done(self, payload):
        self._col_install_finished()
        profile_name, installed, total, skipped_n = payload
        ov = self._col_install_overlay
        if ov is not None:
            ov.finish(self.tr("Imported - {0}/{1} installed.").format(installed, total))
            QTimer.singleShot(1500, self._dismiss_col_overlay)
        # Rebuild the mod index for the imported bundle mods, then reload.
        self._select_installed_collection_profile(profile_name, rescan_index=True)
        msg = self.tr("Profile imported - {0}/{1} mod(s)").format(installed, total)
        self._notify(msg + (self.tr(" ({0} skipped)").format(skipped_n) if skipped_n else ""),
                     "success")
        self._show_offsite_reminder()

    def _show_offsite_reminder(self):
        """Post-install reminder: the collection lists off-site mods that the
        installer can't download - the user must fetch + install them manually
        (links live in the detail view's yellow "Off-site mods" panel)."""
        offsite = self._col_offsite
        self._col_offsite = []
        if not offsite:
            return
        from gui_qt.confirm_overlay import ConfirmOverlay
        names = [name or url for name, url in offsite]
        shown = "\n".join(f"• {n}" for n in names[:8])
        if len(names) > 8:
            shown += "\n" + self.tr("…and {0} more").format(len(names) - 8)
        ConfirmOverlay.show_over(
            self, self.tr("Off-site mods to install"),
            self.tr("This collection includes {0} off-site mod(s) the installer "
                    "could not download:\n\n{1}\n\nDownload and install them "
                    "manually - the links are in the collection page's "
                    "\"Off-site mods\" panel.").format(len(offsite), shown),
            None, confirm_label=self.tr("OK"), cancel_label=None, danger=False,
            card_h=min(240 + 20 * min(len(names), 9), 460))

    def _dismiss_col_overlay(self):
        if self._col_install_overlay is not None:
            self._col_install_overlay.dismiss()
            self._col_install_overlay = None

    def _select_installed_collection_profile(self, profile_name, rescan_index=False):
        """Switch the profile selector to the freshly-installed collection profile
        and reload the panels + Open-Current. *rescan_index* forces a full mod-index
        rebuild (needed after an import extracts bundle mods straight to disk)."""
        try:
            profs = self._gs.profiles()
            self._set_profile_selector_items(profs, current=profile_name)
            self._gs.set_profile(profile_name)
            self._profile_selector.set_current(profile_name)
            # Rebuild pinned actions so "Remove current profile…" shows for the
            # freshly-created (removable) collection profile.
            self._refresh_profile_actions()
        except Exception as exc:
            self._append_log(f"[collection] profile switch failed: {exc}")
        self._reload_modlist(rescan_index=rescan_index)
        self._reload_plugins()
        try:
            self._update_deployed_profile_highlight()
        except Exception:
            pass

    # ---- Collections ▸ Reset load order ----------------------------------
    def _reset_collection_load_order(self):
        """Re-apply the active collection profile's intended load order from its
        manifest. No-op (with a toast) unless the active profile is a collection
        profile. Runs the file rewrites on a worker → toast + modlist reload."""
        if self._reset_running:
            self._notify(self.tr("A load-order reset is already running."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pdir = self._gs.profile_dir()
        from Utils.game_helpers import get_collection_url_from_profile
        url = get_collection_url_from_profile(pdir) if pdir is not None else None
        # Imported .amethyst profiles have no collection URL, but they DO have
        # the saved manifest + the Amethyst/ order snapshot - that pair is
        # enough to reset (off-site mods installed later land back in place).
        has_snapshot = (pdir is not None
                        and (pdir / "Amethyst" / "modlist.txt").is_file()
                        and (pdir / "collection.json").is_file())
        if not url and not has_snapshot:
            self._notify(self.tr("The active profile isn't a collection profile."),
                         "warning")
            return

        self._reset_running = True
        self._notify(self.tr("Resetting collection load order…"), "info")
        domain = getattr(game, "nexus_game_domain", "") or ""
        game_name = getattr(game, "name", "") or ""
        # An explicit collection URL domain is authoritative for fetching that
        # collection; the selected game's primary remains the legacy fallback.
        from Utils.collection_manifest import parse_collection_url
        slug, url_domain, rev_hint = parse_collection_url(url or "")
        domain = url_domain or domain

        import threading, json

        def worker():
            res = {"error": "unknown"}
            try:
                from Utils.collection_install import load_amethyst_reset_data
                from Utils.collection_manifest import load_collection_manifest
                from Utils.collection_reset import reset_collection_load_order
                _log = lambda m: self._op_log.emit(str(m))
                # Prefer the profile's already-saved collection.json (offline).
                manifest = {}
                saved = pdir / "collection.json"
                if saved.is_file():
                    try:
                        manifest = json.loads(saved.read_text(encoding="utf-8"))
                    except Exception:
                        manifest = {}
                if not manifest and slug:
                    api = self._ensure_nexus_api()
                    if api is not None:
                        (_n, _s, _c, _mods, dl_path,
                         revs, _card) = api.get_collection_detail(slug, domain)
                        rev = None
                        try:
                            pub = [int(r.get("revisionNumber") or 0)
                                   for r in (revs or [])
                                   if (r.get("revisionStatus") or "").lower()
                                   == "published"]
                            rev = max(pub) if pub else None
                        except Exception:
                            rev = None
                        manifest = load_collection_manifest(
                            api, game_name, slug, rev, dl_path,
                            log_fn=_log)
                if not manifest:
                    res = {"error": "no_manifest"}
                else:
                    reset_domain = (((manifest.get("info") or {})
                                     .get("domainName") or "").strip()
                                    or domain)
                    # Amethyst-authored collections carry the exact exported
                    # profile in the archive - cached copy first, redownload
                    # if it isn't cached anymore. None → manifest-based reset.
                    amethyst = None
                    try:
                        amethyst = load_amethyst_reset_data(
                            game, slug, profile_dir=pdir,
                            revision_hint=rev_hint, domain=reset_domain,
                            api_provider=self._ensure_nexus_api, log=_log)
                    except Exception as exc:
                        _log(f"Reset load order: Amethyst data unavailable "
                             f"({exc}) - using the manifest")
                    res = reset_collection_load_order(
                        pdir, manifest, log_fn=_log, game=game,
                        amethyst_state=amethyst)
            except Exception as exc:
                self._op_log.emit(f"Reset load order failed: {exc}")
                res = {"error": str(exc)}
            self._reset_done.emit(res)

        threading.Thread(target=worker, daemon=True,
                         name="collection-reset").start()

    def _on_reset_done(self, res):
        self._reset_running = False
        if not isinstance(res, dict) or res.get("error"):
            reason = (res or {}).get("error", "unknown") if isinstance(res, dict) else "unknown"
            self._notify(self.tr("Load order reset failed: {0}").format(reason), "warning")
            return
        _extra = ""
        if res.get("unordered"):
            _extra = (f", {res['unordered']} kept below."
                      if res.get("amethyst") else f", {res['unordered']} at top.")
        self._notify(
            f"Load order reset - {res.get('ordered', 0)} mods ordered{_extra or '.'}",
            "info")
        self._reload_modlist()

    # ---- Nexus login (header menu ▸ Login to Nexus) ------------------------
    # Thin wiring over the toolkit-neutral OAuth/NXM backend in src/Nexus/,
    # mirroring the Tk gui/nexus_settings_dialog.py. The OAuth client fires its
    # callbacks from a background thread, so they only emit `_oauth_event`
    # (queued → UI thread) and never touch widgets directly.

    def _nexus_login_sso(self):
        """Start the browser OAuth flow. Keeps the client on self so the
        'Paste login code' fallback can complete the same session."""
        from Nexus.nexus_oauth import NexusOAuthClient, CLIENT_ID
        if not CLIENT_ID:
            self._notify(self.tr("Nexus login is unavailable in this build."), "warning")
            return
        if self._oauth_client is not None and self._oauth_client.is_running:
            self._notify(self.tr("A Nexus login is already in progress."), "info")
            return
        self._oauth_client = NexusOAuthClient(
            on_token=lambda t: self._oauth_event.emit("token", t),
            on_error=lambda m: self._oauth_event.emit("error", m),
            on_status=lambda m: self._oauth_event.emit("status", m),
        )
        self._oauth_client.start()
        self._notify(self.tr("Opening browser to log in to Nexus Mods…"), "info")

    def _nexus_paste_code(self):
        """Fallback when the localhost redirect was blocked: paste the Base64
        code from the Nexus 'Having issues?' page into the live login session."""
        if self._oauth_client is None or not self._oauth_client.is_running:
            self._notify(self.tr("Start 'Login via SSO' first, then paste the code."),
                         "warning")
            return
        def _pasted(blob):
            if not blob or not blob.strip():
                return
            if self._oauth_client is None or not self._oauth_client.is_running:
                self._notify(self.tr("The login session has ended - start "
                             "'Login via SSO' again."), "warning")
                return
            ok2, msg = self._oauth_client.submit_manual_code(blob)
            self._notify(msg, "info" if ok2 else "warning")

        from gui_qt.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self, "Paste Nexus login code",
            "Paste the code from the Nexus 'Having issues?' page:", _pasted,
            ok_label=self.tr("Submit"))

    def _nexus_clear_credentials(self):
        """Forget the saved OAuth tokens + legacy API key."""
        from Nexus.nexus_oauth import clear_oauth_tokens
        from Nexus.nexus_api import clear_api_key
        clear_api_key()
        clear_oauth_tokens()
        self._nexus_api = None
        if hasattr(self, "_nexus_footer"):
            self._nexus_footer.set_username(None)
        self._notify(self.tr("Nexus credentials cleared."), "warning")
        self._append_log("[nexus] credentials cleared")

    def _nxm_menu_label(self) -> str:
        """Label for the NXM toggle, reflecting the handler's state at build
        time (the menu is built once at startup)."""
        from Nexus.nxm_handler import NxmHandler
        try:
            registered = NxmHandler.is_registered()
        except Exception:
            registered = False
        return "Unregister NXM handler" if registered else "Register NXM handler"

    def _nexus_toggle_nxm(self):
        """Register or unregister the nxm:// protocol handler (for Nexus
        'Download with Manager' links). Toasts the resulting state."""
        from Nexus.nxm_handler import NxmHandler
        try:
            if NxmHandler.is_registered():
                NxmHandler.unregister()
                self._notify(self.tr("NXM handler unregistered."), "warning")
                self._append_log("[nexus] NXM handler unregistered")
            elif NxmHandler.register():
                self._notify(self.tr("NXM handler registered."), "info")
                self._append_log("[nexus] NXM handler registered")
            else:
                self._notify(self.tr("Failed to register - xdg-mime not found?"), "error")
        except Exception as exc:
            self._notify(self.tr("NXM handler error: {0}").format(exc), "error")

    def _on_oauth_event(self, kind: str, payload):
        """OAuth client callbacks marshalled onto the UI thread."""
        if kind == "status":
            self._append_log(f"[nexus] {payload}")
        elif kind == "error":
            self._oauth_client = None
            self._notify(self.tr("Nexus login failed: {0}").format(payload), "error")
        elif kind == "token":
            # Tokens are already persisted by the client before this fires.
            self._oauth_client = None
            self._nexus_api = None
            self._ensure_nexus_api()   # rebuild api + kick the validate() worker
            self._notify(self.tr("Logged in to Nexus Mods."), "info")
            self._append_log("[nexus] OAuth login complete")
            # If onboarding is open, update its Nexus page (Skip → Next).
            ov = getattr(self, "_onboarding_view", None)
            if ov is not None:
                try:
                    ov.on_logged_in()
                except Exception:
                    pass

    # ---- Check for updates -------------------------------------------------
    # Reuses the toolkit-neutral Nexus.nexus_update_checker.check_for_updates,
    # which writes has_update / missing_requirements back to each mod's meta.ini
    # (save_results=True). The modlist re-reads those on reload and paints the
    # update + missing-requirement badges, so the handler just runs the check on
    # a worker thread and reloads.

    def _on_check_updates(self, names=None):
        """Check for mod updates + missing requirements. Runs the Nexus check
        and (for BG3, when a mod.io key is set) the mod.io check in parallel -
        mirroring the Tk _run_check_updates. *names* limits the check to a set
        of mod folder names (right-click subset); None = all."""
        if self._updates_running:
            self._notify(self.tr("An update check is already running."), "info")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return

        domain = getattr(game, "nexus_game_domain", "") or ""
        api = self._ensure_nexus_api() if domain else None
        have_nexus = domain and api is not None
        have_modio = _modio_key_present(game)
        modio_api_path_missing = _modio_api_path_missing(game)
        # Thunderstore needs no key or login at all - a profile of purely
        # Thunderstore mods must still be checkable.
        have_thunderstore = False
        try:
            from Thunderstore.thunderstore_update_checker import scan_installed
            _stg = self._gs.staging_dir()
            have_thunderstore = bool(_stg and scan_installed(Path(_stg)))
        except Exception:
            have_thunderstore = False

        # mod.io (BG3) and Thunderstore can run without a Nexus login. Only bail
        # for "needs Nexus login" when there is nothing else to fall back on.
        if not have_nexus and not have_modio and not have_thunderstore:
            if modio_api_path_missing:
                self._notify(
                    self.tr("Add the API path shown on mod.io's API Access page "
                            "using the mod.io API Key tool."), "warning")
            elif getattr(game, "game_id", "") == "baldurs_gate_3":
                self._notify(
                    self.tr("Log in to Nexus (Nexus ▸ Login) or set a mod.io API key "
                    "(mod.io API Key tool) to check for updates."), "warning")
            elif not domain:
                self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            else:
                self._notify(
                    self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                    "warning")
            return

        if modio_api_path_missing:
            self._notify(
                self.tr("mod.io update checking is disabled until its API path "
                        "is added in the mod.io API Key tool."), "warning")

        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return

        # Normalise the subset to a set (or None for "all").
        subset = set(names) if names else None
        self._updates_running = True
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText(self.tr("Checking…"))
        n = len(subset) if subset else "all"
        # Sticky toast: stays on screen until _on_updates_ready dismisses it,
        # so it doesn't vanish mid-check on the transient auto-dismiss timer.
        self._updates_toast = self._notify(
            self.tr("Checking for updates ({0})…").format(n), "info", sticky=True)

        import threading
        from Nexus.nexus_update_checker import check_for_updates

        def _worker():
            # Carry the checked subset (None = all) so _on_updates_ready can do a
            # scoped, filemap-free flag refresh instead of a full reload.
            out = {"nexus": None, "modio": [], "thunderstore": [],
                   "subset": subset}
            try:
                # Run the mod.io check (BG3) in parallel with the Nexus check -
                # they hit different APIs and write disjoint meta.ini keys.
                modio_box = {"results": []}

                def _modio_work():
                    modio_box["results"] = _check_modio_updates(
                        game, staging,
                        lambda m: self._op_log.emit(f"[modio] {m}"),
                        only_names=subset)

                modio_thread = None
                if have_modio:
                    modio_thread = threading.Thread(
                        target=_modio_work, daemon=True, name="check-modio")
                    modio_thread.start()

                # Thunderstore too - no key needed, and it writes only its own
                # [thunderstore] meta.ini section, so it is safe alongside both.
                ts_box = {"results": []}

                def _ts_work():
                    from Thunderstore.thunderstore_update_checker import (
                        check_for_updates as ts_check)
                    try:
                        ts_box["results"] = ts_check(
                            staging, only_names=subset,
                            progress_cb=lambda m: self._op_log.emit(
                                f"[thunderstore] {m}"))
                    except Exception as exc:
                        self._op_log.emit(
                            f"[thunderstore] update check failed: {exc}")

                ts_thread = threading.Thread(
                    target=_ts_work, daemon=True, name="check-thunderstore")
                ts_thread.start()

                if have_nexus:
                    try:
                        out["nexus"] = check_for_updates(
                            api, staging, game_domain=domain, save_results=True,
                            enabled_only=subset,
                            progress_cb=lambda m: self._op_log.emit(f"[nexus] {m}"),
                        )
                    except Exception as exc:
                        self._op_log.emit(f"[nexus] update check failed: {exc}")

                if modio_thread is not None:
                    modio_thread.join()
                    out["modio"] = modio_box["results"]
                ts_thread.join()
                out["thunderstore"] = ts_box["results"]
            except Exception as exc:
                self._append_log(f"update check failed: {exc}")
                self._updates_ready.emit(None)
                return
            self._updates_ready.emit(out)

        threading.Thread(target=_worker, daemon=True, name="check-updates").start()

    def _on_updates_ready(self, result):
        """Worker finished - re-enable the button, summarise, refresh the rows.
        The sticky "Checking…" toast is dismissed here (morphed into the final
        summary) so it never disappears while the check is still running."""
        self._updates_running = False
        btn = getattr(self, "_check_updates_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText(self.tr("Check Updates"))
        # Turn the sticky "Checking…" toast into the final summary (or drop it).
        toast = getattr(self, "_updates_toast", None)
        self._updates_toast = None

        def _finish(text: str, state: str):
            if toast is not None:
                toast.dismiss(text, state=state)
            else:
                self._notify(text, state)

        if result is None:
            _finish(self.tr("Update check failed - see the log."), "error")
            return

        nexus = result.get("nexus")
        modio = result.get("modio") or []
        parts = []
        if nexus is not None:
            updates, missing = nexus
            if updates:
                parts.append(f"Nexus: {len(updates)} update"
                             f"{'s' if len(updates) != 1 else ''}")
            if missing:
                parts.append(f"{len(missing)} missing requirement"
                             f"{'s' if len(missing) != 1 else ''}")
        modio_updates = [u for u in modio if not getattr(u, "unknown", False)]
        modio_unknown = [u for u in modio if getattr(u, "unknown", False)]
        if modio_updates:
            parts.append(f"mod.io: {len(modio_updates)} update"
                         f"{'s' if len(modio_updates) != 1 else ''}")
        if modio_unknown:
            parts.append(f"{len(modio_unknown)} mod.io version"
                         f"{'s' if len(modio_unknown) != 1 else ''} unknown")
        ts_all = result.get("thunderstore") or []
        ts_updates = [u for u in ts_all
                      if getattr(u, "has_update", False)
                      and not getattr(u, "unknown", False)]
        ts_unknown = [u for u in ts_all if getattr(u, "unknown", False)]
        if ts_updates:
            parts.append(f"Thunderstore: {len(ts_updates)} update"
                         f"{'s' if len(ts_updates) != 1 else ''}")
        if ts_unknown:
            parts.append(f"{len(ts_unknown)} Thunderstore package"
                         f"{'s' if len(ts_unknown) != 1 else ''} unknown")

        if parts:
            _finish(", ".join(parts) + ".", "warning")
        else:
            _finish(self.tr("All mods are up to date."), "info")
        # Check Updates only rewrote meta.ini flags (update / missing-reqs) -
        # the deployed file set is unchanged, so skip the full reload + filemap
        # rebuild and just re-read the affected flags (endorse/note parity).
        # subset is None for a full check (re-reads all flags) or the checked
        # names for a right-click subset; either way, no filemap rebuild.
        self._refresh_modlist_flags(result.get("subset"))
        # Offer to Quick Update everything the check just flagged. Nexus only -
        # Quick Update resolves and downloads through the Nexus API, so mod.io
        # updates (BG3) stay manual. Ignored updates never reach this list.
        if nexus is not None and nexus[0]:
            qu_names = [u.mod_name for u in nexus[0]]

            def _qu_confirmed(ok, ns=qu_names):
                if ok:
                    self._quick_update_mods(ns)

            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self, self.tr("Updates available"),
                self.tr("{0} mod(s) have an update available.\n\n"
                        "Run Quick Update on all of them now?").format(len(qu_names)),
                _qu_confirmed, confirm_label=self.tr("Quick Update"))

    # ---- Quick Update -----------------------------------------------------

    @staticmethod
    def _thunderstore_reinstall_record(meta, archive_path=""):
        """Build the link/API-info tuple needed to restamp an exact version."""
        from Thunderstore.ror2mm_handler import Ror2mmLink

        if not (meta.namespace and meta.name and meta.version):
            raise ValueError("namespace, name and version are required")
        link = Ror2mmLink(
            namespace=meta.namespace,
            name=meta.name,
            version=meta.version,
            community=meta.community or "",
        )
        info = {
            "full_version_name": meta.full_name or link.full_name,
            "download_url": meta.download_url or link.download_url,
            "description": meta.description or "",
            "website_url": meta.website_url or "",
            "icon_url": meta.icon_url or "",
            "size": int(meta.file_size or 0),
            "dependencies": meta.dependency_list(),
        }
        return str(archive_path), link, info

    def _notify_install_summary(self, ok: int, total: int, names) -> None:
        """Show the standard install result toast for callback-owned batches."""
        if total <= 0:
            return
        if ok == total and ok > 0:
            if ok == 1:
                self._notify(self.tr("Installed {0}").format(names[0]), "success")
            else:
                self._notify(self.tr("Installed {0} mods").format(ok), "success")
        elif ok > 0:
            self._notify(
                self.tr("Installed {0} of {1} mods - see log for failures.")
                .format(ok, total), "warning")
        else:
            self._notify(self.tr("Install failed - see log."), "error")

    def _reinstall_mods(self, mod_names):
        """Reinstall one or more mods from their recorded installation archives
        (Tk parity, gui/modlist_nexus_actions._reinstall_mod). Each mod's archive
        is located across the Downloads dir + configured caches + extra locations
        (Utils.download_locations / config_paths); mods with no on-disk archive
        are skipped with a log line. Resolved archives go through _install_paths
        with the mod's existing folder name forced (silent Replace-All, keeping
        the modlist position + endorsed flag), so no Mod-Already-Exists dialog."""
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        names = list(mod_names or [])
        if not names:
            return

        from gui_qt.modlist_menu import (
            _installation_archive, _read_mod_meta, _thunderstore_meta)
        preferred: dict[str, str] = {}   # archive path → forced folder name
        metas: dict[str, object] = {}    # archive path → reinstall metadata
        paths: list[str] = []
        redownload: list[tuple] = []     # (..., filename, installed_meta)
        ts_redownload: list[tuple] = []  # (folder name, Thunderstore meta)
        local_ts_records: list[tuple] = []
        missing: list[str] = []          # no archive or recorded store identity
        from Nexus.nexus_meta import (has_reinstall_carryover,
                                      merge_reinstall_metadata)
        for nm in names:
            meta = _read_mod_meta(self._modlist_view, nm)
            ts_meta = _thunderstore_meta(self._modlist_view, nm)
            arc = _installation_archive(self._modlist_view, nm)
            if arc is not None:
                archive_path = str(arc)
                paths.append(archive_path)
                preferred[archive_path] = nm
                if ts_meta is not None:
                    try:
                        local_ts_records.append(
                            self._thunderstore_reinstall_record(
                                ts_meta, archive_path))
                    except Exception as exc:
                        self._append_log(
                            f"[reinstall] {nm} - invalid Thunderstore metadata "
                            f"({exc}); section may not be restored.")
                # Only prebuild a meta when there is something to carry over
                # (identity / root_folder / collection ownership). EVERY
                # installed mod has a meta.ini, and a prebuilt meta makes
                # _write_install_meta skip its filename/MD5 Nexus lookup - so
                # passing a thin one through would leave a never-identified
                # mod unidentified forever.
                if has_reinstall_carryover(meta):
                    metas[archive_path] = merge_reinstall_metadata(None, meta)
                continue
            # Archive gone - fall back to a Nexus redownload if this mod carries
            # a modid + fileid (and a resolvable game domain) in its meta.ini.
            mod_id = int(getattr(meta, "mod_id", 0) or 0) if meta is not None else 0
            file_id = int(getattr(meta, "file_id", 0) or 0) if meta is not None else 0
            from Nexus.nexus_meta import normalise_game_domain
            domain = normalise_game_domain(
                getattr(meta, "game_domain", "") or "") if meta is not None else ""
            if not domain:
                domain = getattr(game, "nexus_game_domain", "") or ""
            if mod_id > 0 and file_id > 0 and domain:
                redownload.append((nm, domain, mod_id, file_id,
                                   getattr(meta, "installation_file", "") or "",
                                   meta, ts_meta))
            elif ts_meta is not None and ts_meta.namespace and ts_meta.name \
                    and ts_meta.version:
                ts_redownload.append((nm, ts_meta))
            else:
                missing.append(nm)
                self._append_log(f"[reinstall] {nm} - install archive not found and "
                                 "no Nexus or Thunderstore identity to redownload, "
                                 "skipped.")

        if not paths and not redownload and not ts_redownload:
            self._notify(self.tr("No install archive found for the selected mod(s)."),
                         "warning")
            return
        # With 'Download only' on, mods that still have their archive are
        # reinstalled from it; the rest are only redownloaded to the cache.
        redownload_count = len(redownload) + len(ts_redownload)
        _split = bool(redownload_count) and self._download_only_active()
        if missing or _split:
            if _split:
                msg = self.tr("Reinstalling {0} mod(s), redownloading {1}; "
                              "{2} skipped.").format(
                                  len(paths), redownload_count, len(missing))
            else:
                msg = self.tr("Reinstalling {0} mod(s); {1} skipped "
                              "(no archive found).").format(
                                  len(paths) + redownload_count, len(missing))
            self._notify(msg, "info")

        # Redownload-only reinstalls go through the Nexus path (premium download
        # or browser fallback). If some mods still have their archive, install
        # those now and redownload the rest afterwards.
        if paths:
            # clear_archives=False: reinstall CONSUMES an existing archive the
            # user kept - deleting it would make the next reinstall impossible.
            on_done = None
            if local_ts_records:
                def _restamp_local(ok, total, installed_names, installed):
                    self._stamp_thunderstore_install_results(
                        local_ts_records, installed)
                    sync_total = max(
                        0, total - int(getattr(self, "_install_handoffs", 0)))
                    self._notify_install_summary(
                        ok, sync_total, installed_names)

                on_done = _restamp_local
            self._install_paths(
                paths, metas=metas or None, preferred_names=preferred,
                on_all_done=on_done, clear_archives=False)
        if redownload:
            self._redownload_and_reinstall(redownload)
        if ts_redownload:
            self._redownload_thunderstore_mods(ts_redownload)

    def _redownload_thunderstore_mods(self, items):
        """Redownload exact installed Thunderstore versions and reinstall them.

        ``items`` is ``[(mod_folder_name, ThunderstoreModMeta), ...]``. Public
        Thunderstore downloads need no account or premium gate. Archives are
        retained in the cache and each install is forced back into its current
        folder, matching the Nexus reinstall behaviour.
        """
        jobs = []
        failed = []
        for mod_name, meta in items:
            try:
                _path, link, info = self._thunderstore_reinstall_record(meta)
            except Exception as exc:
                failed.append((mod_name, f"invalid metadata ({exc})"))
                continue
            dl_key = self._new_dl_key()
            label = f"{link.full_name}.zip"
            self._nexus_download_progress(dl_key, label, 0, 0)
            jobs.append((mod_name, link, info, dl_key, label))

        if not jobs:
            for name, reason in failed:
                self._append_log(
                    f"[thunderstore reinstall] {name}: {reason}")
            self._notify(
                self.tr("Reinstall: {0} mod(s) couldn't be redownloaded - "
                        "see the log.").format(len(failed)), "warning")
            return

        self._append_log(
            f"[thunderstore reinstall] redownloading {len(jobs)} package(s)…")
        self._notify(
            self.tr("Reinstall - redownloading {0} mod(s)…").format(len(jobs)),
            "info")

        import threading

        def _worker():
            from Thunderstore.thunderstore_download import download_package
            from Utils.config_paths import get_download_cache_dir_for_game
            from gui_qt.safe_emit import safe_emit

            game = self._gs.game
            dest = get_download_cache_dir_for_game(
                getattr(game, "name", "") or "")
            downloads = []
            worker_failed = list(failed)
            for mod_name, link, info, dl_key, label in jobs:
                try:
                    result = download_package(
                        link, dest_dir=dest,
                        expected_size=int(info.get("size") or 0),
                        progress_cb=lambda d, t, _key=dl_key, _label=label:
                            safe_emit(self._req_install_prog, _key, _label,
                                      int(d), int(t)))
                    if result.success and result.file_path:
                        downloads.append((mod_name, result, link, info))
                    else:
                        worker_failed.append(
                            (mod_name, result.error or "download failed"))
                except Exception as exc:
                    worker_failed.append((mod_name, f"download error ({exc})"))
                finally:
                    safe_emit(self._req_install_prog, dl_key, "", 0, -1)
            safe_emit(
                self._thunderstore_reinstall_downloaded,
                (downloads, worker_failed))

        threading.Thread(
            target=_worker, daemon=True,
            name="thunderstore-reinstall-download").start()

    def _on_thunderstore_reinstall_downloaded(self, payload):
        """Install completed Thunderstore redownloads on the UI thread."""
        downloads, failed = payload
        for name, reason in failed:
            self._append_log(f"[thunderstore reinstall] {name}: {reason}")

        if not downloads:
            if failed:
                self._notify(
                    self.tr("Reinstall: {0} mod(s) couldn't be redownloaded - "
                            "see the log.").format(len(failed)), "warning")
            return

        paths = [str(result.file_path)
                 for _name, result, _link, _info in downloads]
        preferred = {str(result.file_path): name
                     for name, result, _link, _info in downloads}
        records = [(str(result.file_path), link, info)
                   for _name, result, link, info in downloads]

        if failed:
            self._notify(
                self.tr("Redownloaded {0} mod(s); {1} failed - see the log.")
                .format(len(downloads), len(failed)), "warning")

        def _restamp(ok, total, installed_names, installed):
            self._stamp_thunderstore_install_results(records, installed)
            sync_total = max(
                0, total - int(getattr(self, "_install_handoffs", 0)))
            self._notify_install_summary(ok, sync_total, installed_names)

        queued = self._deliver_download(
            paths, preferred_names=preferred, on_all_done=_restamp,
            clear_archives=False, notify=False)
        if not queued:
            self._notify(
                self.tr("Redownloaded {0} mod(s) - reinstall them from the "
                        "Downloads tab.").format(len(downloads)), "success")

    def _redownload_and_reinstall(self, items):
        """Reinstall mods whose install archive is gone by redownloading the
        exact recorded file from Nexus (modid + fileid from meta.ini), then
        installing via _install_paths with the mod's folder name forced (silent
        Replace-All). Premium users get a direct download; non-premium users get
        each mod's Nexus files page opened in the browser (site 'Download with
        Mod Manager' flow). `items` = [(mod_name, domain, mod_id, file_id,
        filename, installed_meta, installed_thunderstore_meta), …]."""
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(
                self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                "warning")
            return
        game = self._gs.game
        is_premium = False
        try:
            is_premium = bool(api.validate().is_premium)
            if is_premium:
                # [dev] force_manual_install = true → exercise the manual
                # browser-download flow even on a premium account (same switch
                # the browser / Change Version tabs honour).
                from Utils.ui_config import load_force_manual_install
                if load_force_manual_install():
                    self._append_log("[reinstall] [dev] force_manual_install - "
                                     "using the manual browser-download flow.")
                    is_premium = False
        except Exception:
            pass

        if not is_premium:
            # Non-premium: same manual browser-download flow as the Nexus browser
            # and Change Version tabs - if the archive is already on disk install
            # it straight away, otherwise open the file's download page and watch
            # the download folders so it auto-installs when the browser download
            # lands (nxm:// 'Download with Mod Manager' also works and cancels
            # the watch to avoid a double install).
            self._start_reinstall_manual(items)
            return

        self._append_log(
            f"[reinstall] redownloading {len(items)} missing archive(s)…")
        self._notify(
            self.tr("Reinstall - redownloading {0} mod(s)…").format(len(items)),
            "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            dl_workers = 8

        import threading

        # One shared progress card for the whole batch (aggregate bytes).
        self._reinstall_dl_phase = self.tr(
            "Redownloading {0} mod(s)…").format(len(items))
        progress = {}                     # mod_name → [cur_bytes, total_bytes]
        for nm, *_ in items:
            progress[nm] = [0, 0]
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def _post_aggregate(force=False):
            import time as _time
            with progress_lock:
                now = _time.monotonic()
                if not force and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            self._reinstall_dl_progress.emit(cur, tot)

        _post_aggregate(force=True)

        def _download_all():
            import concurrent.futures as _cf
            from Nexus.nexus_download import NexusDownloader
            from Nexus.nexus_meta import (build_meta_from_download,
                                          merge_reinstall_metadata)
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            downloader = NexusDownloader(api, download_dir=dest)
            dl_items = []          # (mod_name, archive_path, prebuilt_meta)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                (mod_name, domain, mod_id, file_id, filename, installed_meta,
                 installed_ts_meta) = item
                try:
                    def _on_progress(cur, tot, _m=mod_name):
                        with progress_lock:
                            slot = progress[_m]
                            slot[0] = int(cur)
                            if tot:
                                slot[1] = int(tot)
                        _post_aggregate()

                    result = downloader.download_file(
                        game_domain=domain, mod_id=mod_id, file_id=file_id,
                        dest_dir=dest, known_file_name=filename,
                        progress_cb=_on_progress)
                    if not (result.success and result.file_path):
                        with lock:
                            failed.append((mod_name,
                                           f"download failed - {result.error}"))
                        return
                    with progress_lock:
                        slot = progress[mod_name]
                        slot[1] = slot[1] or slot[0]
                        slot[0] = slot[1]
                    _post_aggregate(force=True)
                    prebuilt = None
                    try:
                        mod_info = api.get_mod(domain, mod_id)
                    except Exception:
                        mod_info = None
                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=file_id,
                            archive_name=result.file_name, mod_info=mod_info)
                        prebuilt = merge_reinstall_metadata(prebuilt, installed_meta)
                    except Exception as exc:
                        self._op_log.emit(
                            f"[reinstall] Warning - could not build metadata: {exc}")
                    with lock:
                        dl_items.append((mod_name, str(result.file_path), prebuilt,
                                         installed_ts_meta))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, items))
            self._reinstall_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="reinstall-dl").start()

    # ---- non-premium reinstall: browser download + folder watch ------------
    def _start_reinstall_manual(self, items):
        """Reinstall missing archives for free users via the shared manual
        browser-download flow (parity with the Nexus browser / Change Version
        tabs). Fetches each file's REAL metadata first (name + size - the folder
        watcher matches archives by size ±1% + mod id, so a zero-size stub only
        matches on an exact name, which browser downloads rarely keep), then per
        mod: install immediately if the archive is already on disk, else open its
        download page and arm a folder watcher that auto-installs when the
        browser download arrives. `items` =
        [(mod_name, domain, mod_id, file_id, filename, installed_meta,
        installed_thunderstore_meta), …]."""
        import threading
        from gui_qt.safe_emit import safe_emit

        api = getattr(self, "_nexus_api", None)

        class _F:            # NexusModFile-like fallback if the fetch fails
            def __init__(self, file_id, file_name):
                self.file_id = file_id
                self.file_name = file_name
                self.name = file_name
                self.size_in_bytes = 0
                self.size_kb = 0

        def _prep():
            # One file-list fetch per mod (GraphQL modFiles - no REST cost),
            # shared when several items point at the same mod.
            files_cache: dict = {}
            enriched = []
            for (nm, domain, mod_id, file_id, filename, installed_meta,
                 installed_ts_meta) in items:
                f = None
                key = (domain, int(mod_id or 0))
                if api is not None:
                    try:
                        files = files_cache.get(key)
                        if files is None:
                            files = list(api.get_mod_files(domain, mod_id).files)
                            files_cache[key] = files
                        f = next((x for x in files
                                  if int(getattr(x, "file_id", 0) or 0)
                                  == int(file_id or 0)), None)
                    except Exception as exc:
                        files_cache.setdefault(key, [])
                        self._op_log.emit(
                            f"[reinstall] {nm} - could not fetch file info "
                            f"({exc}); archive detection may be less reliable.")
                if f is None:
                    f = _F(int(file_id or 0), filename or "")
                enriched.append((nm, domain, mod_id, file_id, f, installed_meta,
                                 installed_ts_meta))
            safe_emit(self._reinstall_manual_ready, enriched)

        threading.Thread(target=_prep, daemon=True,
                         name="reinstall-manual-prep").start()

    def _on_reinstall_manual_ready(self, enriched):
        """UI thread: file metadata resolved - run each mod through the shared
        start_manual_install flow (skip the browser when the archive is already
        downloaded, else open its download page + watch the download folders).
        `enriched` = [(mod_name, domain, mod_id, file_id, NexusModFile-like,
        installed_meta, installed_thunderstore_meta), …]."""
        from Nexus.manual_download_watch import start_manual_install
        from Nexus.nexus_meta import (has_reinstall_carryover,
                                      merge_reinstall_metadata)
        from Utils.xdg import open_url
        from gui_qt.safe_emit import safe_emit

        api = getattr(self, "_nexus_api", None)
        opened = 0
        for (nm, domain, mod_id, file_id, f, installed_meta,
             installed_ts_meta) in enriched:
            dl_key = self._new_dl_key()
            self._nexus_download_progress(dl_key, nm, 0, 0)  # show popup card

            # Helper callbacks run on the WATCHER thread - marshal via Signals.
            # (_op_log / _append_log are thread-safe.)
            def on_archive(path, meta, _file, _nm=nm, _domain=domain,
                           _mid=mod_id, _key=dl_key,
                           _installed=installed_meta,
                           _installed_ts=installed_ts_meta):
                if not self._claim_app_manual_watch(_mid, _domain, _key):
                    return
                # Merge only when there IS something to merge - the downstream
                # `meta is not None` guard must stay meaningful so a watcher
                # that built no meta still gets _write_install_meta's
                # filename/MD5 lookup instead of an empty prebuilt meta.
                if meta is not None or has_reinstall_carryover(_installed):
                    meta = merge_reinstall_metadata(meta, _installed)
                safe_emit(self._reinstall_manual_found,
                          (_nm, str(path), meta, _key, _installed_ts))

            def on_progress(done, total, _nm=nm, _key=dl_key):
                safe_emit(self._req_install_prog, _key, _nm, int(done), int(total))

            def on_timeout(_nm=nm, _domain=domain, _mid=mod_id, _key=dl_key):
                if not self._claim_app_manual_watch(_mid, _domain, _key):
                    return
                self._op_log.emit(
                    f"[reinstall] {_nm} - stopped waiting for a browser "
                    "download (nothing arrived; reinstall from Downloads once "
                    "downloaded).")
                # total<0 clears the card (on the UI thread via the Signal).
                safe_emit(self._req_install_prog, _key, "", 0, -1)

            self.cancel_app_manual_watch(mod_id, domain)  # re-trigger → fresh watch
            watcher, already = start_manual_install(
                api=api, game_domain=domain, mod_id=mod_id, files=[f],
                open_url_fn=lambda u: open_url(u, log_fn=self._append_log),
                log_fn=lambda m, _nm=nm: self._append_log(f"[reinstall] {_nm} - {m}"),
                log_label=nm,
                on_archive=on_archive, on_progress=on_progress,
                on_timeout=on_timeout)
            watch_key = self._app_manual_watch_key(domain, mod_id)
            self._app_manual_watchers[watch_key] = (watcher, dl_key)
            if not already:
                opened += 1

        if opened:
            tmpl = (self.tr("Premium required to redownload. Opened {0} download "
                            "page(s) - they'll land in the Downloads tab.")
                    if self._download_only_active() else
                    self.tr("Premium required to redownload. Opened {0} download "
                            "page(s) - they'll reinstall automatically once "
                            "downloaded."))
            self._notify(tmpl.format(opened), "info")

    @staticmethod
    def _app_manual_watch_key(game_domain, mod_id):
        from Nexus.nexus_meta import normalise_game_domain
        return normalise_game_domain(game_domain), int(mod_id or 0)

    def cancel_app_manual_watch(self, mod_id: int, game_domain: str = ""):
        """Stop a pending app-level browser-download watch (reinstall or
        missing-requirements install; no-op if none). Called on re-trigger,
        and by the nxm:// handler when a 'Download with Mod Manager' for the
        same mod arrives (that flow installs it, so the watch must not -
        double install)."""
        mid = int(mod_id or 0)
        if game_domain:
            keys = [self._app_manual_watch_key(game_domain, mid)]
        else:
            # Backwards-compatible internal cleanup when the caller has no
            # domain: stop every watch with this numeric ID.
            keys = [key for key in self._app_manual_watchers
                    if key[1] == mid]
        for key in keys:
            t = self._app_manual_watchers.pop(key, None)
            if t is None:
                continue
            watcher, dl_key = t
            watcher.stop()
            self._nexus_download_progress(dl_key, "", 0, -1)

    def _claim_app_manual_watch(self, mod_id, game_domain, dl_key) -> bool:
        """Claim completion of an app-level manual watch (watcher thread).
        False when the watch was already cancelled or replaced by a newer one
        for the same mod - the stale watcher must not emit its completion
        signals (double install / clobbering the new watch's progress card)."""
        watch_key = self._app_manual_watch_key(game_domain, mod_id)
        t = self._app_manual_watchers.pop(watch_key, None)
        if t is None:
            return False
        if t[1] != dl_key:
            self._app_manual_watchers[watch_key] = t  # newer watch owns slot
            return False
        return True

    def _on_reinstall_manual_found(self, payload):
        """UI thread: a non-premium reinstall's browser download landed (or an
        existing archive was found). Install with the folder name forced (silent
        Replace-All), keeping the archive for future reinstalls. *meta* was built
        on the watcher thread."""
        nm, archive, meta, dl_key, installed_ts_meta = payload
        self._nexus_download_progress(dl_key, "", 0, -1)   # clear the card
        if not archive:
            return
        self._append_log(f"[reinstall] {nm} - redownloaded {archive}")
        metas = {archive: meta} if meta is not None else None
        on_done = None
        if installed_ts_meta is not None:
            try:
                record = self._thunderstore_reinstall_record(
                    installed_ts_meta, archive)

                def _restamp(ok, total, installed_names, installed):
                    self._stamp_thunderstore_install_results(
                        [record], installed)
                    sync_total = max(
                        0, total - int(getattr(
                            self, "_install_handoffs", 0)))
                    self._notify_install_summary(
                        ok, sync_total, installed_names)

                on_done = _restamp
            except Exception as exc:
                self._append_log(
                    f"[reinstall] {nm} - invalid Thunderstore metadata "
                    f"({exc}); section may not be restored.")
        # clear_archives=False: keep the archive so it can be reinstalled again.
        self._deliver_download([archive], metas=metas,
                               preferred_names={archive: nm},
                               on_all_done=on_done, clear_archives=False)

    def _on_reinstall_dl_progress(self, cur: int, tot: int):
        """UI thread: drive the shared reinstall redownload progress card."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        self._progress_popup.set_progress(
            cur, tot, getattr(self, "_reinstall_dl_phase", None),
            title=self.tr("Reinstall"), bytes_mode=True, key="reinstall-dl")

    def _on_reinstall_downloaded(self, dl_items, failed):
        """UI thread: redownloads finished. Install the batch via _install_paths
        with the folder name forced per archive (silent Replace-All)."""
        if self._progress_popup is not None:
            self._progress_popup.clear(key="reinstall-dl")
        for name, reason in failed:
            self._append_log(f"[reinstall] {name}: {reason}")
        if not dl_items:
            if failed:
                self._notify(
                    self.tr("Reinstall: {0} mod(s) couldn't be redownloaded - "
                    "see the log.").format(len(failed)), "warning")
            return
        # New items also carry the installed Thunderstore section so a mod
        # mirrored on both stores keeps that metadata after the Nexus archive
        # replaces its folder. Accept legacy 3-tuples defensively.
        normalised = [tuple(item) + (None,) if len(item) == 3 else tuple(item)
                      for item in dl_items]
        paths = [p for _n, p, _m, _ts in normalised]
        metas = {p: m for _n, p, m, _ts in normalised if m is not None}
        preferred = {p: n for n, p, _m, _ts in normalised}
        ts_records = []
        for name, path, _meta, ts_meta in normalised:
            if ts_meta is None:
                continue
            try:
                ts_records.append(
                    self._thunderstore_reinstall_record(ts_meta, path))
            except Exception as exc:
                self._append_log(
                    f"[reinstall] {name} - invalid Thunderstore metadata "
                    f"({exc}); section may not be restored.")
        if failed:
            self._notify(
                self.tr("Redownloaded {0} mod(s); {1} failed - see the log.").format(
                    len(dl_items), len(failed)), "warning")
        # clear_archives=False: keep the freshly downloaded archive so the mod
        # can be reinstalled again without another download.
        # notify=False: the diverted path wants reinstall-specific wording.
        on_done = None
        if ts_records:
            def _restamp(ok, total, installed_names, installed):
                self._stamp_thunderstore_install_results(
                    ts_records, installed)
                sync_total = max(
                    0, total - int(getattr(self, "_install_handoffs", 0)))
                self._notify_install_summary(ok, sync_total, installed_names)

            on_done = _restamp
        queued = self._deliver_download(
            paths, metas=metas, preferred_names=preferred,
            on_all_done=on_done, clear_archives=False, notify=False)
        if not queued:
            self._notify(self.tr("Redownloaded {0} mod(s) - reinstall them from the "
                                 "Downloads tab.").format(len(dl_items)), "success")

    def _quick_update_mods(self, mod_names):
        """Auto-install the latest name-matched version for each update-flagged
        mod (Tk parity, gui/modlist_nexus_actions._quick_update_mods). Mods whose
        latest file isn't a name match are skipped - the user updates those via
        Change Version. Three phases, all off the UI thread: resolve the target
        file_ids (parallel) → download them (parallel, premium-gated) → install
        the whole batch via _install_paths with the folder name forced (silent
        Replace-All)."""
        if getattr(self, "_quick_updating", False):
            self._notify(self.tr("A Quick Update is already running."), "info")
            return
        if getattr(self, "_install_running", False):
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        targets = list(mod_names or [])
        if not targets:
            self._notify(self.tr("No mods with a pending update to quick-update."), "info")
            return

        self._quick_updating = True
        domain = getattr(game, "nexus_game_domain", "") or ""
        self._notify(self.tr("Quick Update - checking {0} mod(s)…").format(len(targets)), "info")
        self._append_log(f"[nexus] Quick Update - checking {len(targets)} mod(s)…")

        import threading

        def _resolve_worker():
            import concurrent.futures as _cf
            from Utils.quick_update import resolve_quick_update_target
            queue = []
            skipped = []   # (mod_name, reason)

            def _one(nm):
                return resolve_quick_update_target(api, staging, nm, domain)

            with _cf.ThreadPoolExecutor(max_workers=4) as pool:
                for nm, (status, payload) in zip(targets, pool.map(_one, targets)):
                    if status == "queued":
                        queue.append(payload)
                    else:
                        skipped.append((nm, payload))
                        self._op_log.emit(f"[nexus] {nm} - {payload}, skipped.")
            self._qu_resolved.emit(queue, skipped)

        threading.Thread(target=_resolve_worker, daemon=True,
                         name="quick-update-resolve").start()

    def _on_qu_resolved(self, queue, skipped):
        """UI thread: resolve finished. Nothing to update → summarise. Otherwise
        gate on premium and download every resolved file in parallel."""
        if not queue:
            self._qu_finish(0, [], skipped)
            return
        api = self._ensure_nexus_api()
        game = self._gs.game
        is_premium = False
        try:
            is_premium = bool(api.validate().is_premium)
        except Exception:
            pass
        if not is_premium:
            self._append_log("[nexus] Premium required for Quick Update direct "
                             "downloads.")
            self._qu_finish(
                0, [(it[0], "Premium required for direct download") for it in queue],
                skipped)
            return

        self._append_log(f"[nexus] Quick Update - downloading {len(queue)} mod(s)…")
        self._notify(self.tr("Quick Update - downloading {0} mod(s)…").format(len(queue)), "info")

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 8)))
        except Exception:
            dl_workers = 8

        import threading

        # One shared progress card for the whole batch (Tk parity: a per-download
        # popup per mod stacks up fast). Progress is the aggregate byte count
        # across all parallel downloads; totals are seeded from the Nexus file
        # sizes and corrected by each download's own reported total.
        self._qu_dl_phase = self.tr("Downloading {0} mod(s)…").format(len(queue))
        progress = {}                     # mod_name → [cur_bytes, total_bytes]
        for item in queue:
            fi = item[4]
            size = 0
            if fi is not None:
                size = (getattr(fi, "size_in_bytes", None)
                        or (getattr(fi, "size_kb", 0) or 0) * 1024 or 0)
            progress[item[0]] = [0, int(size)]
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def _post_aggregate(force=False):
            import time as _time
            with progress_lock:
                now = _time.monotonic()
                if not force and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            self._qu_dl_progress.emit(cur, tot)

        _post_aggregate(force=True)       # show the card before downloads start

        def _download_all():
            import concurrent.futures as _cf
            from Nexus.nexus_download import NexusDownloader
            from Nexus.nexus_meta import build_meta_from_download
            from Utils.config_paths import get_download_cache_dir_for_game
            dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
            downloader = NexusDownloader(api, download_dir=dest)
            dl_items = []          # (mod_name, archive_path, prebuilt_meta)
            failed = []            # (mod_name, reason)
            lock = threading.Lock()

            def _one(item):
                mod_name, game_domain, meta, file_id, file_info = item
                try:
                    try:
                        mod_info = api.get_mod(game_domain, meta.mod_id)
                    except Exception as exc:
                        with lock:
                            failed.append((mod_name, f"could not fetch mod info ({exc})"))
                        return
                    size = 0
                    if file_info is not None:
                        size = (getattr(file_info, "size_in_bytes", None)
                                or (getattr(file_info, "size_kb", 0) or 0) * 1024 or 0)

                    def _on_progress(cur, tot, _m=mod_name):
                        with progress_lock:
                            slot = progress[_m]
                            slot[0] = int(cur)
                            if tot:
                                slot[1] = int(tot)
                        _post_aggregate()

                    result = downloader.download_file(
                        game_domain=game_domain, mod_id=meta.mod_id,
                        file_id=file_id, dest_dir=dest,
                        known_file_name=getattr(file_info, "file_name", "") or "",
                        expected_size_bytes=int(size),
                        progress_cb=_on_progress)
                    if not (result.success and result.file_path):
                        with lock:
                            failed.append((mod_name,
                                           f"download failed - {result.error}"))
                        return
                    # Snap this mod's slice to done (its total may have been an
                    # estimate) so the shared bar never sits below the truth.
                    with progress_lock:
                        slot = progress[mod_name]
                        slot[1] = slot[1] or slot[0]
                        slot[0] = slot[1]
                    _post_aggregate(force=True)
                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=game_domain, mod_id=meta.mod_id,
                            file_id=file_id, archive_name=result.file_name,
                            mod_info=mod_info, file_info=file_info)
                        prebuilt.has_update = False
                    except Exception as exc:
                        self._op_log.emit(
                            f"[nexus] Warning - could not build metadata: {exc}")
                        prebuilt = None
                    with lock:
                        dl_items.append((mod_name, str(result.file_path), prebuilt))
                except Exception as exc:
                    with lock:
                        failed.append((mod_name, f"download error ({exc})"))

            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_one, queue))
            self._qu_skipped = skipped   # carried to _qu_finish via the installer
            self._qu_downloaded.emit(dl_items, failed)

        threading.Thread(target=_download_all, daemon=True,
                         name="quick-update-dl").start()

    def _on_qu_dl_progress(self, cur: int, tot: int):
        """UI thread: drive the shared Quick Update download card (aggregate
        bytes across every parallel download in the batch)."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        self._progress_popup.set_progress(
            cur, tot, getattr(self, "_qu_dl_phase", None),
            title=self.tr("Quick Update"), bytes_mode=True, key="qu-dl")

    def _on_qu_downloaded(self, dl_items, failed):
        """UI thread: every download finished. Install the batch via _install_paths
        with the folder name forced per archive (silent Replace-All), then finish."""
        if self._progress_popup is not None:
            self._progress_popup.clear(key="qu-dl")
        skipped = getattr(self, "_qu_skipped", [])
        if not dl_items:
            self._qu_finish(0, failed, skipped)
            return
        if self._download_only_active():
            # Not _deliver_download: _qu_finish only runs from on_all_done, so a
            # divert would leave _quick_updating set (blocking every later Quick
            # Update) and report "N install failed". No _reload_modlist either -
            # nothing changed on disk, so the update flags correctly stay set.
            for _n, p, _m in dl_items:
                self._append_log(f"[download-only] kept '{Path(p).name}' in the "
                                 f"cache - update install skipped.")
            if hasattr(self, "_downloads_view"):
                self._downloads_view.mark_dirty()
            self._quick_updating = False
            self._qu_skipped = []
            self._append_log(
                f"[nexus] Quick Update (download only) - {len(dl_items)} downloaded, "
                f"{len(skipped)} skipped (no name match), {len(failed)} failed.")
            for name, reason in failed:
                self._append_log(f"[nexus] Quick Update - {name}: {reason}")
            self._notify(self.tr("Quick Update: downloaded {0} update(s) - install "
                                 "them from the Downloads tab.").format(len(dl_items)),
                         "success")
            problems = len(skipped) + len(failed)
            if problems:
                self._notify(self.tr("Quick Update: {0} mod(s) couldn't be "
                                     "downloaded - see the log.").format(problems),
                             "warning")
            return

        paths = [p for _n, p, _m in dl_items]
        metas = {p: m for _n, p, m in dl_items if m is not None}
        preferred = {p: n for n, p, _m in dl_items}
        expected = len(dl_items)

        def _done(ok, total, names, _installed):
            # ok/total here are the archive install results; combine with the
            # download failures + resolve skips for the batch summary.
            more_failed = list(failed)
            if ok < expected:
                more_failed.append(
                    (f"{expected - ok} mod(s)", "install failed - see log"))
            self._qu_finish(ok, more_failed, skipped)

        self._install_paths(paths, metas=metas, preferred_names=preferred,
                            on_all_done=_done)

    def _qu_finish(self, updated, failed, skipped):
        """Log the batch summary + toast; warn about mods that couldn't be
        quick-updated. Re-checks flags happen via _install_paths' _reload_modlist;
        the resolve/download-only paths (nothing installed) reload here."""
        self._quick_updating = False
        self._qu_skipped = []
        if updated == 0:
            # No install ran → refresh flags ourselves (installed path already did).
            self._reload_modlist()
        self._append_log(
            f"[nexus] Quick Update - {updated} updated, "
            f"{len(skipped)} skipped (no name match), {len(failed)} failed.")
        for name, reason in failed:
            self._append_log(f"[nexus] Quick Update - {name}: {reason}")
        if updated:
            self._notify(self.tr("Quick Update: updated {0} mod(s)").format(updated), "success")
        problems = len(skipped) + len(failed)
        if not problems:
            if not updated:
                self._notify(self.tr("Quick Update: nothing to update."), "info")
            return
        parts = []
        if skipped:
            parts.append(f"{len(skipped)} had no name-matched file")
        if failed:
            parts.append(f"{len(failed)} failed to download or install")
        self._notify(
            f"Quick Update: {problems} mod(s) couldn't be updated - "
            + " and ".join(parts) + ". See the log; use Change Version manually.",
            "warning")

    # ---- Restore backup (plugins-panel-scoped overlay) --------------------

    def _open_restore_backup_tab(self):
        """Open the Restore backup list for the current profile as a tab that
        takes over the whole plugins panel. Triggered by the footer button."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        pname = self._gs.profile
        if not pname:
            self._notify(self.tr("No profile selected."), "warning")
            return
        if self._tabs.has_key("restore_backup"):
            self._tabs.focus_key("restore_backup")
            return
        profile_dir = game.get_profile_root() / "profiles" / pname
        from gui_qt.backup_restore_view import BackupRestoreView
        view = BackupRestoreView(
            profile_dir, pname,
            on_restored=self._on_backup_restored,
            on_close=self._close_restore_backup_tab,
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, self.tr("Restore backup"), self._plugins_panel_stack,
            key="restore_backup")

    def _on_backup_restored(self):
        """A backup was restored - sync the modlist with the mods folder so any
        mods in staging but absent from the restored modlist.txt show up, then
        refresh both panels (backups cover modlist AND plugins)."""
        from Utils.modlist import sync_modlist_with_mods_folder
        self._reassert_profile_paths()
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:  # noqa: BLE001
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        self._reload_modlist(rescan_index=True, preserve_overlays=True)
        self._reload_plugins()

    def _close_restore_backup_tab(self):
        if self._tabs.has_key("restore_backup"):
            self._tabs.close_tab("restore_backup")

    # ---- Change Version (plugins-panel-scoped overlay) --------------------

    def _open_change_version_tab(self, mod_name: str):
        """Open the Change Version picker for *mod_name* as a tab that takes over
        the whole plugins panel. Triggered by the update-flag click + the
        right-click 'Change Version' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        from Nexus.nexus_meta import read_meta
        meta = read_meta(staging / mod_name / "meta.ini")
        if int(getattr(meta, "mod_id", 0) or 0) <= 0:
            self._notify(self.tr("'{0}' isn't a Nexus mod.").format(mod_name), "warning")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return

        # Reuse one overlay: rebuild it for the new mod if already open.
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        from gui_qt.change_version_view import ChangeVersionView
        view = ChangeVersionView(
            api, game, mod_name, meta,
            install_fn=self._deliver_download,
            on_close=self._close_change_version_tab,
            log_fn=self._append_log,
            progress_fn=self._nexus_download_progress)
        self._change_version_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_change_version_view", None))
        self._tabs.open_scoped_tab(
            view, self.tr("Change Version"), self._plugins_panel_stack,
            key="change_version")

    def _close_change_version_tab(self):
        """Close the Change Version overlay + refresh modlist flags (an Ignore-
        Update toggle or an install may have changed meta.ini).

        When an install kicked off from this tab is still in flight, do nothing:
        the install's own completion path (_on_install_done) reloads the modlist
        + rebuilds the filemap once at the end. Reloading here as well caused a
        redundant refresh (+ filemap rebuild) BEFORE the "replace old mod?"
        dialog, on top of the necessary one after. For a plain close (Ignore-
        Update toggle only touched meta.ini flags) a light flag refresh is
        enough - no filemap rebuild needed."""
        if self._tabs.has_key("change_version"):
            self._tabs.close_tab("change_version")
        if getattr(self, "_install_running", False):
            return
        self._refresh_modlist_flags()

    def _sync_change_version_after_install(self, final_name: str):
        """Retarget the open Change Version tab at *final_name* once an install
        it kicked off has landed - the tab stays open across installs, so its
        title, Ignore-Update state and highlights must track the fresh meta.ini.
        Also the swap after 'Remove previous version' renames the target. Reads
        meta from the profile the install actually landed in (a group member's
        staging when a Profile Group is active - the group link may not exist
        yet at this point)."""
        view = getattr(self, "_change_version_view", None)
        if view is None:
            return
        game = self._gs.game
        pdir = getattr(self, "_install_profile_dir", None) or self._gs.profile_dir()
        if game is None or pdir is None:
            return
        try:
            from Utils.mod_copy import resolve_target_staging
            staging = Path(resolve_target_staging(game, Path(pdir)))
        except Exception:
            return
        if not (staging / final_name).is_dir():
            return
        from Nexus.nexus_meta import read_meta
        view.retarget(final_name, read_meta(staging / final_name / "meta.ini"))

    def _follow_selection_change_version(self):
        """Change Version tab follows the modlist selection: while the tab is
        visible, selecting a single Nexus mod retargets it. Debounced so
        keyboard-scrolling the list doesn't fire an API fetch per row."""
        view = getattr(self, "_change_version_view", None)
        if view is None or not view.isVisible() or view.busy():
            return
        t = getattr(self, "_chv_follow_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._retarget_change_version_to_selection)
            self._chv_follow_timer = t
        t.start(300)

    def _retarget_change_version_to_selection(self):
        view = getattr(self, "_change_version_view", None)
        if view is None or not view.isVisible() or view.busy():
            return
        rows = self._modlist_view.selectionModel().selectedRows()
        if len(rows) != 1:
            return
        e = self._modlist_model.entry(rows[0].row())
        if e.is_separator or e.name == view.current_mod_name():
            return
        staging = self._gs.staging_dir()
        if staging is None or not (staging / e.name).is_dir():
            return
        from Nexus.nexus_meta import read_meta
        meta = read_meta(staging / e.name / "meta.ini")
        if int(getattr(meta, "mod_id", 0) or 0) <= 0:
            return    # not a Nexus mod - keep showing the current one
        view.retarget(e.name, meta)

    # ---- Bundle options (plugins-panel-scoped overlay) --------------------

    def _open_bundle_tab(self, mod_name: str):
        """Open the Bundle Options selector for the RE/Fluffy bundle *mod_name*
        as a full (detachable) tab, like the Nexus browser. Triggered by the
        bundle-flag click + the right-click 'Bundle options…' item. On Save the
        new selection is materialised, the mod re-indexed and the filemap
        rebuilt."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        from Utils.re_bundle import read_bundle_spec, BUNDLE_LIB_DIR
        meta_path = staging / mod_name / "meta.ini"
        spec = read_bundle_spec(meta_path)
        if spec is None:
            self._notify(self.tr("'{0}' has no bundle configuration.").format(mod_name), "warning")
            return
        lib_dir = staging / mod_name / BUNDLE_LIB_DIR

        # Reuse one tab: rebuild it for the new mod if already open.
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        from gui_qt.bundle_options_view import BundleOptionsView
        view = BundleOptionsView(
            mod_name, spec, lib_dir,
            on_save=lambda s, m=mod_name, mp=meta_path:
                self._apply_bundle_selection(m, mp, s),
            on_close=self._close_bundle_tab,
            log_fn=self._append_log)
        self._bundle_options_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_bundle_options_view", None))
        self._tabs.open_tab(view, self.tr("Bundle: {0}").format(mod_name), key="bundle_options")

    def _apply_bundle_selection(self, mod_name, meta_path, new_spec):
        """Persist *new_spec*, re-materialise the bundle's selection, then close
        the tab and re-index + rebuild the filemap (Tk parity, gui/modlist_panel.
        _apply_bundle_selection). The rescan skips <mod>/.mm_bundle/ so only the
        materialised selection at the mod root is indexed/deployed."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        try:
            from Utils.re_bundle import write_bundle_spec, materialize_selection
            write_bundle_spec(meta_path, new_spec)
            materialize_selection(staging / mod_name, new_spec)
        except Exception as exc:
            self._append_log(f"Bundle options: apply failed - {exc}")
            self._notify(self.tr("Could not update bundle: {0}").format(exc), "error")
            return
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        # Full re-index (skips .mm_bundle/) then filemap rebuild - the Qt
        # equivalent of Tk's rescan_mods_in_index([mod]) + _rebuild_filemap.
        self._rebuild_conflicts_async(rescan_index=True)
        self._notify(self.tr("Updated bundle: {0}").format(mod_name), "info")

    def _close_bundle_tab(self):
        """Close the Bundle Options overlay + refresh the modlist."""
        if self._tabs.has_key("bundle_options"):
            self._tabs.close_tab("bundle_options")
        self._reload_modlist()

    # ---- Separator settings (plugins-panel-scoped overlay) ----------------

    def _open_sep_settings_tab(self, sep_name, current_color, current_deploy):
        """Open the Separator Settings picker (colour + deploy override) as a tab
        over the plugins panel. Triggered by the right-click 'Separator settings…'
        item. *sep_name* is the internal `..._separator` name (the storage key)."""
        from gui_qt.separator_settings_view import SeparatorSettingsView
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")
        view = SeparatorSettingsView(
            sep_name, current_color, current_deploy,
            on_save=lambda color, deploy: self._save_sep_settings(
                sep_name, color, deploy),
            on_close=self._close_sep_settings_tab)
        self._tabs.open_scoped_tab(
            view, self.tr("Separator Settings"), self._plugins_panel_stack,
            key="sep_settings")

    def _save_sep_settings(self, sep_name, color, deploy):
        """Persist a separator's colour + deploy override, repaint the modlist,
        and rebuild the filemap (a deploy-path change reroutes its mods)."""
        profile_dir = self._gs.profile_dir()
        # Colour + deploy override - model drives the repaint (the deploy badge
        # painted on the separator row); profile_state persists them.
        self._modlist_model.set_sep_color(sep_name, color)
        self._modlist_model.set_sep_deploy_info(sep_name, deploy)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                if color:
                    colors[sep_name] = color
                else:
                    colors.pop(sep_name, None)
                write_separator_colors(profile_dir, colors)

                paths = read_separator_deploy_paths(profile_dir)
                if deploy:
                    paths[sep_name] = deploy
                else:
                    paths.pop(sep_name, None)
                write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator settings save failed: {exc}",
                      flush=True)
        self._modlist_view.viewport().update()
        # Deploy paths feed the filemap → rebuild so the routing takes effect.
        self._rebuild_conflicts_async()

    def _close_sep_settings_tab(self):
        if self._tabs.has_key("sep_settings"):
            self._tabs.close_tab("sep_settings")

    def _on_separator_renamed(self, old_name, new_name):
        """Migrate a renamed separator's stored colour + deploy override from the
        old internal name to the new one, then persist + repaint."""
        profile_dir = self._gs.profile_dir()
        # Model colour/deploy dicts (keyed by internal name) follow the rename.
        c = self._modlist_model.sep_color(old_name)
        if c is not None:
            self._modlist_model.set_sep_color(old_name, None)
            self._modlist_model.set_sep_color(new_name, c)
        d = self._modlist_model.sep_deploy_info(old_name)
        if d:
            self._modlist_model.set_sep_deploy_info(old_name, None)
            self._modlist_model.set_sep_deploy_info(new_name, d)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                if old_name in colors:
                    colors[new_name] = colors.pop(old_name)
                    write_separator_colors(profile_dir, colors)
                paths = read_separator_deploy_paths(profile_dir)
                if old_name in paths:
                    paths[new_name] = paths.pop(old_name)
                    write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator rename migration failed: {exc}",
                      flush=True)
        self._modlist_view.viewport().update()

    def _on_separators_removed(self, names):
        """Drop stored colour + deploy override for removed separators."""
        profile_dir = self._gs.profile_dir()
        for nm in names:
            self._modlist_model.set_sep_color(nm, None)
            self._modlist_model.set_sep_deploy_info(nm, None)
        if profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_separator_colors, write_separator_colors,
                    read_separator_deploy_paths, write_separator_deploy_paths)
                colors = read_separator_colors(profile_dir)
                paths = read_separator_deploy_paths(profile_dir)
                changed_c = changed_p = False
                for nm in names:
                    changed_c |= colors.pop(nm, None) is not None
                    changed_p |= paths.pop(nm, None) is not None
                if changed_c:
                    write_separator_colors(profile_dir, colors)
                if changed_p:
                    write_separator_deploy_paths(profile_dir, paths)
            except Exception as exc:
                print(f"[gui_qt] separator removal cleanup failed: {exc}",
                      flush=True)

    def _open_missing_reqs_tab(self, target):
        """Open the Missing Requirements panel over the plugins panel. *target* is
        a single mod name (str) or a set of names (multi-select). Triggered by the
        ⚠ flag click + the right-click 'Missing Requirements' item."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        domain = getattr(game, "nexus_game_domain", "") or ""
        if not domain:
            self._notify(self.tr("'{0}' has no Nexus Mods page.").format(game.name), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        names = [target] if isinstance(target, str) else list(target or ())
        from Nexus.nexus_meta import normalise_game_domain, read_meta
        from gui_qt.modlist_data import (
            _apply_req_substitutions, _parse_missing_req_pairs)
        subs_cache: dict = {}
        # meta.ini keeps the FULL seeded requirement list on purpose (so a
        # requirement reappears if its mod is later removed) - filter out the
        # ones already installed here, exactly like the ⚠ flag pass does.
        # Without this, reopening the panel re-showed installed requirements.
        # Per-requirement IGNORED ids are NOT filtered - they stay visible in
        # the panel (with their Ignore box checked) so they can be un-ignored.
        installed_ids = self._installed_mod_ids()
        specs = []
        for name in names:
            meta = read_meta(staging / name / "meta.ini")
            mod_domain = (normalise_game_domain(
                getattr(meta, "game_domain", "") or "") or domain)
            raw = getattr(meta, "missing_requirements", "") or ""
            # Requirement substitutions (e.g. Nemesis → Pandora) are applied on
            # read too, so a meta.ini stamped before the rule existed still
            # offers the replacement rather than the mod we're steering away from.
            sub_dom = mod_domain.strip().lower()
            ids = {mid for mid, _ in _apply_req_substitutions(
                _parse_missing_req_pairs(raw), sub_dom, subs_cache)
                if (sub_dom, mid) not in installed_ids}
            if not ids:
                continue
            ignored_ids = {mid for mid, _ in _apply_req_substitutions(
                _parse_missing_req_pairs(
                    getattr(meta, "ignored_requirements", "") or ""),
                sub_dom, subs_cache)}
            specs.append({"mod_name": name,
                          "mod_id": int(getattr(meta, "mod_id", 0) or 0),
                          "domain": mod_domain,
                          "missing_ids": ids,
                          "ignored_ids": ignored_ids & ids})
        if not specs:
            self._notify(self.tr("No missing requirements."), "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return

        from Utils.profile_state import (
            read_ignored_missing_requirements, write_ignored_missing_requirements)
        pdir = self._gs.profile_dir()

        def _save_ignored(s):
            if pdir is not None:
                write_ignored_missing_requirements(pdir, set(s))

        ignored = (read_ignored_missing_requirements(pdir)
                   if pdir is not None else set())

        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        from gui_qt.missing_reqs_view import MissingReqsView
        view = MissingReqsView(
            api, game, specs, ignored, _save_ignored,
            on_close=self._close_missing_reqs_tab, log_fn=self._append_log,
            install_fn=self._install_nexus_mod_by_id,
            ignore_req_fn=self._set_req_ignored)
        self._missing_reqs_view = view
        view.destroyed.connect(
            lambda *_: setattr(self, "_missing_reqs_view", None))
        self._tabs.open_scoped_tab(
            view, self.tr("Missing Requirements"), self._plugins_panel_stack,
            key="missing_reqs")

    def _set_req_ignored(self, req_id: int, req_name: str, ignored: bool,
                         owner_names):
        """Per-requirement ignore from the Missing Requirements panel: add or
        remove *req_id* in the ignoredRequirements meta.ini field of each owning
        mod in *owner_names*, then re-run the light flag pass so the ⚠ flag
        updates immediately. Ignored ids stay in missingRequirements - the panel
        keeps listing them (checked) so they can be un-ignored later."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        from Nexus.nexus_meta import read_meta, write_meta
        from gui_qt.modlist_data import _parse_missing_req_pairs
        rid = int(req_id or 0)
        if rid <= 0:
            return
        changed = False
        for nm in owner_names or ():
            meta_path = staging / nm / "meta.ini"
            try:
                meta = read_meta(meta_path)
            except Exception:
                continue
            cur = dict(_parse_missing_req_pairs(
                getattr(meta, "ignored_requirements", "") or ""))
            if ignored:
                if rid in cur:
                    continue
                # ';' would corrupt the pair format (same rule the checkers use).
                cur[rid] = (req_name or "").replace(";", ",")
            else:
                if rid not in cur:
                    continue
                del cur[rid]
            meta.ignored_requirements = ";".join(
                f"{m}:{n}" for m, n in cur.items())
            try:
                write_meta(meta_path, meta)
                changed = True
            except Exception as exc:
                self._append_log(
                    f"[nexus] could not save ignored requirement for {nm}: {exc}")
        if changed:
            self._append_log(
                f"[nexus] {'ignored' if ignored else 'un-ignored'} requirement "
                f"'{req_name or rid}' for: {', '.join(owner_names)}")
            # Full pass (not a names= subset): the ⚠ two-pass needs installed
            # ids from ALL rows. Warm meta cache makes this cheap.
            self._refresh_modlist_flags()

    def _close_missing_reqs_tab(self):
        """Close the Missing Requirements panel. Only refresh flags when an Ignore
        toggle actually changed the ignored set (a plain open/close changes
        nothing) - and via the light flag path, not a full modlist reload."""
        view = getattr(self, "_missing_reqs_view", None)
        changed = bool(getattr(view, "ignored_changed", False))
        if self._tabs.has_key("missing_reqs"):
            self._tabs.close_tab("missing_reqs")
        if changed:
            # Re-read the on-disk ignored set the panel just wrote, then let the
            # light path recompute the ⚠ flag from it (no 500-file reload).
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile_state import read_ignored_missing_requirements
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                except Exception:
                    pass
            self._refresh_modlist_flags()

    # ---- View Requirements (plugins-panel-scoped tab) -----------------------
    def _req_tab_active(self) -> bool:
        """True only while the View Requirements tab is OPEN AND on screen.

        A scoped tab that's been switched away from (another tab in the same
        panel is showing) is hidden by the tab widget - so requirement
        highlights should give way to the normal conflict highlights until the
        user switches back. Visibility is the exact signal for that."""
        view = getattr(self, "_view_requirements_view", None)
        return (view is not None
                and self._tabs.has_key("view_requirements")
                and view.isVisible())

    def _open_view_requirements_tab(self, name):
        """Open the View Requirements panel over the plugins panel (right-click
        item). Offline - it reads the full requirements list the update checker
        stores in meta.ini. While open it follows the modlist selection and the
        modlist tints the related mods purple (requires) / blue (required by)
        instead of the green/red conflict highlights.

        Seeds from the current selection so several right-clicked mods pool
        their requirements from the start; falls back to the clicked mod when
        it isn't part of the selection."""
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."),
                         "warning")
            return
        seed = self._modlist_view.selected_mod_names()
        seed = sorted(seed) if name in seed else [name]
        if self._tabs.has_key("view_requirements"):
            self._tabs.focus_key("view_requirements")
            view = getattr(self, "_view_requirements_view", None)
            if view is not None:
                view.show_mods(seed)
            return
        from gui_qt.requirements_view import RequirementsView
        view = RequirementsView(
            staging_fn=self._gs.staging_dir,
            on_close=self._close_view_requirements_tab,
            on_data_changed=self._apply_requirement_highlights,
            on_focus_changed=self._on_view_requirements_focus_changed,
            on_view_missing=self._view_requirements_open_missing)
        self._view_requirements_view = view
        # destroyed fires on EVERY teardown path (✕ button, tab-bar close,
        # game change) - restore normal highlights from the one place.
        view.destroyed.connect(lambda *_: self._on_view_requirements_gone())
        self._tabs.open_scoped_tab(
            view, self.tr("View Requirements"), self._plugins_panel_stack,
            key="view_requirements")
        # The showEvent from open_scoped_tab flips on the requirement highlights
        # via _on_view_requirements_focus_changed(True).
        view.show_mods(seed)

    def _view_requirements_open_missing(self, names):
        """View Missing Requirements button → open the Missing Requirements
        panel for the current selection. _open_missing_reqs_tab filters to mods
        that actually have missing requirements and toasts if none do (it also
        replaces the View Requirements tab, since both scope the plugins
        panel)."""
        if names:
            self._open_missing_reqs_tab(set(names))

    def _close_view_requirements_tab(self):
        if self._tabs.has_key("view_requirements"):
            self._tabs.close_tab("view_requirements")

    def _on_view_requirements_focus_changed(self, visible: bool):
        """The tab was shown or hidden (tab switch within the plugins panel).
        Shown → suppress the per-rebuild conflict reapply and paint requirement
        tints. Hidden → un-suppress and restore conflict highlights for the
        current selection."""
        self._modlist_view.suppress_conflict_highlights = bool(visible)
        if visible:
            self._apply_requirement_highlights()
        else:
            try:
                self._on_mod_selection_changed()
            except Exception:
                pass

    def _on_view_requirements_gone(self):
        """Post-teardown: drop the ref and restore the conflict highlights for
        the current selection (a plain _on_mod_selection_changed pass replaces
        the requirement sets - set_highlights swaps all five sets at once).

        The view's hideEvent normally restores highlights first, but teardown
        paths that delete the widget without a hide (or where the ref is already
        gone) still need this backstop."""
        self._view_requirements_view = None
        self._modlist_view.suppress_conflict_highlights = False
        try:
            self._on_mod_selection_changed()
        except Exception:
            pass

    def _apply_requirement_highlights(self):
        """Push the View Requirements tab's related-mod sets into the modlist
        as the purple/blue row tints (replaces any conflict tint). No-op when
        the tab isn't on screen - a background repopulate must not steal the
        modlist tints from the conflict highlights."""
        if not self._req_tab_active():
            return
        view = self._view_requirements_view
        self._modlist_model.set_highlights(
            requires=view.installed_requires,
            required_by=view.installed_required_by)

    # ---- Show Conflicts (full detachable tab) ------------------------------
    def _open_show_conflicts_tab(self, mod_name: str):
        """Open the conflict-detail view for *mod_name* as a full tab. The
        file-level data is computed on a worker thread inside the view (from
        filemap.txt + modindex.bin + bsa_index.bin - the app's ConflictData is
        only mod-level)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        staging = self._gs.staging_dir()
        if staging is None:
            self._notify(self.tr("No mod staging folder for this profile."), "warning")
            return
        cd = getattr(self, "_conflict_data", None)
        beaten = set()
        if cd is not None:
            beaten = (set(cd.overrides.get(mod_name, set()))
                      | set(cd.bsa_overrides.get(mod_name, set())))
        strip_prefixes = (getattr(game, "mod_folder_strip_prefixes", set())
                          | getattr(game, "mod_folder_strip_prefixes_post", set()))
        # UE pak mounting is not plugin-driven - archive winners follow the
        # engine's (_P boost, basename) mount order instead (empty plugin
        # order + archive_name_ordering below select that path).
        if getattr(game, "archive_plugin_ordering", True):
            # Load order, NOT the panel's display order (a column sort must not
            # change which BSA wins).
            plugin_order = [r.name for r in self._plugin_model.natural_rows()
                            if getattr(r, "enabled", False)]
        else:
            plugin_order = []
        plugin_exts = frozenset(x.lower() for x in
                                (getattr(game, "plugin_extensions", []) or ()))
        from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
        from Utils.mod_files import conflict_root_context
        archive_exts = frozenset(
            getattr(game, "archive_extensions", frozenset()) or frozenset())
        ctx = {
            "staging_root": staging,
            "profile_dir": self._gs.profile_dir(),
            "filemap_path": staging.parent / "filemap.txt",
            "modindex_path": staging.parent / "modindex.bin",
            "bsa_index_path": staging.parent / "bsa_index.bin",
            "strip_prefixes": strip_prefixes,
            "beaten_mods": beaten,
            "archive_exts": archive_exts,
            "plugin_order": plugin_order,
            "plugin_exts": plugin_exts,
            # UE paks resolve by (_P boost, basename) mount order.
            "archive_name_ordering": bool(archive_exts & UE_ARCHIVE_EXTENSIONS),
            "root_ctx": conflict_root_context(game, self._gs.profile_dir()),
        }
        # Reuse one tab: rebuild it for the new mod if already open.
        if self._tabs.has_key("show_conflicts"):
            self._tabs.close_tab("show_conflicts")
        from gui_qt.show_conflicts_view import ShowConflictsView
        view = ShowConflictsView(
            mod_name, ctx,
            on_close=lambda: self._tabs.close_tab("show_conflicts"),
            log_fn=self._append_log)
        self._tabs.open_tab(view, self.tr("Conflicts: {0}").format(mod_name), key="show_conflicts")

    def _on_modlist_endorse(self, names, endorse: bool):
        """Endorse / abstain the given mods on Nexus (right-click). Runs on a
        worker thread via the shared API, updates each mod's meta.ini `endorsed`
        flag, then refreshes the modlist so the ★ flag icon updates. Mirrors Tk
        _vote_selected_mods."""
        import threading
        names = [n for n in (names or []) if n]
        if not names:
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        game = self._gs.game
        domain = getattr(game, "nexus_game_domain", "") or ""
        staging = self._gs.staging_dir()
        if staging is None or not domain:
            return
        msg = (self.tr("Endorsing {0} mod(s)…") if endorse
               else self.tr("Abstaining from {0} mod(s)…")).format(len(names))
        self._notify(msg, "info")

        def _worker():
            from Nexus.nexus_meta import normalise_game_domain, read_meta, write_meta
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = read_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    mod_domain = (normalise_game_domain(meta.game_domain)
                                  or domain)
                    if endorse:
                        api.endorse_mod(mod_domain, meta.mod_id, meta.version or "")
                    else:
                        api.abstain_mod(mod_domain, meta.mod_id, meta.version or "")
                    meta.endorsed = endorse
                    write_meta(meta_path, meta)
                    ok += 1
                except Exception as exc:
                    self._op_log.emit(f"Nexus: {'endorse' if endorse else 'abstain'} "
                                      f"failed for '{nm}': {exc}")
            self._endorse_done.emit({"ok": ok, "endorse": endorse,
                                     "names": list(names)})

        threading.Thread(target=_worker, daemon=True, name="endorse").start()

    def _on_endorse_done(self, payload):
        """UI thread: report the endorse/abstain result + refresh the ★ flag."""
        ok = payload.get("ok", 0)
        if ok:
            msg = (self.tr("Endorsed {0} mod(s).") if payload.get("endorse")
                   else self.tr("Abstained from {0} mod(s).")).format(ok)
            self._notify(msg, "success")
            self._refresh_modlist_flags(payload.get("names"))
        else:
            self._notify(self.tr("No mods were updated (already in that state or no "
                         "Nexus id)."), "info")

    def _on_modlist_track(self, names):
        """Start tracking the given mods on Nexus (right-click). Runs on a worker
        thread via the shared API, reading each mod's Nexus id from its meta.ini.
        Mirrors _on_modlist_endorse (track has no meta.ini flag to persist)."""
        import threading
        names = [n for n in (names or []) if n]
        if not names:
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus."), "warning")
            return
        game = self._gs.game
        domain = getattr(game, "nexus_game_domain", "") or ""
        staging = self._gs.staging_dir()
        if staging is None or not domain:
            return
        self._notify(self.tr("Tracking {0} mod(s)…").format(len(names)), "info")

        def _worker():
            from Nexus.nexus_meta import normalise_game_domain, read_meta
            ok = 0
            for nm in names:
                meta_path = staging / nm / "meta.ini"
                if not meta_path.is_file():
                    continue
                try:
                    meta = read_meta(meta_path)
                    if not meta.mod_id:
                        continue
                    mod_domain = (normalise_game_domain(meta.game_domain)
                                  or domain)
                    api.track_mod(mod_domain, meta.mod_id)
                    ok += 1
                except Exception as exc:
                    self._op_log.emit(f"Nexus: track failed for '{nm}': {exc}")
            self._track_done.emit({"ok": ok})

        threading.Thread(target=_worker, daemon=True, name="track").start()

    def _on_track_done(self, payload):
        """UI thread: report the track result."""
        ok = payload.get("ok", 0)
        if ok:
            self._notify(self.tr("Tracking {0} mod(s).").format(ok), "success")
        else:
            self._notify(self.tr("No mods were tracked (no Nexus id)."), "info")

    def _copy_mods_to_profile(self, names, enabled_map, target_profile, move):
        """Copy (or move) the given mods' staging folders into *target_profile*.
        Resolves collisions (single → Replace/Rename/Cancel overlay; multi → one
        Replace-or-skip prompt), copies on a worker thread, and - for a move -
        removes the sources here afterwards. Port of Tk _copy_mod(s)_to_profile."""
        from Utils import mod_copy
        game = self._gs.game
        src_staging = self._gs.staging_dir()
        src_profile_dir = self._gs.profile_dir()
        if game is None or src_staging is None or src_profile_dir is None:
            return
        try:
            target_profile_dir = game.get_profile_root() / "profiles" / target_profile
            target_staging = mod_copy.resolve_target_staging(game, target_profile_dir)
        except Exception as exc:
            self._notify(self.tr("Could not resolve target profile: {0}").format(exc), "error")
            return
        names = [n for n in names if n]
        if not names:
            return
        existing = [n for n in names
                    if mod_copy.mod_exists_in_profile(target_staging, n)]

        # plan[name] = dest_name (rename) or None (copy as-is / replace after wipe)
        def _launch(plan, replace_set):
            self._run_copy_to_profile(
                names, enabled_map, plan, replace_set, move,
                src_staging, src_profile_dir, target_staging, target_profile_dir,
                target_profile, game)

        if not existing:
            _launch({n: None for n in names}, set())
            return

        if len(names) == 1 and existing:
            # Single mod already there → Replace / Rename / Cancel.
            from gui_qt.mod_exists_overlay import ModExistsOverlay
            nm = names[0]

            def _resolved(action, _nm=nm):
                if not action or action == "cancel":
                    self._notify(self.tr("Copy cancelled."), "info")
                    return
                if action == "replace":
                    _launch({_nm: None}, {_nm})
                elif action.startswith("rename:"):
                    new = action.split(":", 1)[1].strip()
                    if new:
                        _launch({_nm: new}, set())

            ModExistsOverlay.show_over(
                self, nm, False, _resolved,
                suggestions=self._mod_name_suggestions(nm, src_staging))
        else:
            # Several already there → one Replace-or-skip prompt (Tk parity).
            from gui_qt.confirm_overlay import ConfirmOverlay

            def _resolved(replace):
                if replace:
                    _launch({n: None for n in names}, set(existing))
                else:
                    # Skip existing: copy only the non-colliding ones.
                    keep = [n for n in names if n not in existing]
                    if not keep:
                        self._notify(self.tr("All selected mods already exist there."), "info")
                        return
                    _launch({n: None for n in keep}, set())

            ConfirmOverlay.show_over(
                self, self.tr("Copy to profile"),
                self.tr("{0} of {1} mod(s) already exist in "
                        "'{2}'. Replace them? (Cancel skips those.)").format(
                            len(existing), len(names), target_profile),
                _resolved, confirm_label=self.tr("Replace"),
                cancel_label=self.tr("Skip"), danger=False)

    def _run_copy_to_profile(self, names, enabled_map, plan, replace_set, move,
                             src_staging, src_profile_dir, target_staging,
                             target_profile_dir, target_profile, game):
        """Worker: copy each planned mod, then (move) remove the sources."""
        import threading
        from pathlib import Path
        from Utils import mod_copy
        # Serialize: a second copy/move while one runs would write the same
        # target folders concurrently (install has the same guard).
        if getattr(self, "_copy_running", False):
            self._notify(self.tr("A copy/move is already in progress."), "info")
            return
        self._copy_running = True
        self._op_title = self.tr("Moving") if move else self.tr("Copying")
        self._ensure_feedback()
        self._notify(
            (self.tr("Moving {0} mod(s) to '{1}'…") if move
             else self.tr("Copying {0} mod(s) to '{1}'…"))
            .format(len(plan), target_profile), "info")
        total = len(plan)
        self._op_progress.emit(0, total, f"to '{target_profile}'")

        def _worker():
            import os as _os
            copied = []
            detached = []   # group→owning-member: files identical, no copy
            # `plan` is ordered highest-priority-first (row 0 = top of the source
            # modlist). Register the whole copied block in ONE prepend after the
            # loop - prepending each mod individually would reverse the group.
            registered = []  # (dest_name, enabled), top-first
            for done, (nm, dest_name) in enumerate(plan.items()):
                self._op_progress.emit(done, total, nm)
                try:
                    src_folder = Path(src_staging) / nm
                    dest_folder = Path(target_staging) / (dest_name or nm)
                    # Copying OUT of a group into the member that owns the mod:
                    # source (through the link) and destination are the SAME
                    # real folder. A Replace rmtree here would destroy the only
                    # copy; there is nothing to transfer - treat as done.
                    if dest_folder.exists() and _os.path.realpath(
                            src_folder) == _os.path.realpath(dest_folder):
                        copied.append(nm)
                        detached.append(nm)
                        self._op_log.emit(
                            f"'{nm}' already lives in the target profile "
                            f"(it owns the group's copy) - nothing to copy.")
                        continue
                    if nm in replace_set:
                        import shutil
                        shutil.rmtree(dest_folder, ignore_errors=True)
                    out = mod_copy.copy_mod_to_profile(
                        Path(src_staging), Path(src_profile_dir),
                        Path(target_staging), Path(target_profile_dir),
                        nm, enabled_map.get(nm, True), dest_name=dest_name,
                        game=game, register=False)
                    if out:
                        copied.append(nm)
                        registered.append((out, enabled_map.get(nm, True)))
                except Exception as exc:
                    self._op_log.emit(f"Copy to profile failed for '{nm}': {exc}")
            if registered:
                try:
                    mod_copy.register_mods_in_modlist(
                        Path(target_profile_dir) / "modlist.txt", registered)
                except Exception as exc:
                    self._op_log.emit(f"Copy to profile: modlist update failed: {exc}")
            self._op_progress.emit(total, total, "finishing")
            removed = False
            if move and copied:
                try:
                    from Utils.profile_groups import (is_group,
                                                      remove_mods_from_group)
                    _log_cb = lambda m: self._op_log.emit(str(m))  # noqa: E731
                    if is_group(Path(src_profile_dir)):
                        # Moving OUT of a group removes the mod from its
                        # owning member too - EXCEPT when the target IS the
                        # owner (detached): the member keeps its files and
                        # only the group entry/link goes. A locked member's
                        # mods are refused by remove_mods_from_group, which
                        # returns only what it actually removed.
                        full = [n for n in copied if n not in detached]
                        done = []
                        if full:
                            done += remove_mods_from_group(
                                game, Path(src_profile_dir), full,
                                log_fn=_log_cb)
                        if detached:
                            done += remove_mods_from_group(
                                game, Path(src_profile_dir), detached,
                                log_fn=_log_cb, delete_member_copies=False)
                        blocked = [n for n in copied if n not in done]
                        if blocked:
                            self._op_log.emit(
                                f"Kept in the group (locked profile): "
                                f"{', '.join(blocked)}")
                        copied = done
                    else:
                        from Utils.mod_remove import remove_mods
                        remove_mods(game, Path(src_profile_dir), copied,
                                    log_fn=_log_cb)
                    removed = True
                except Exception as exc:
                    self._op_log.emit(f"Move: could not remove sources: {exc}")
            self._copy_done.emit({"copied": len(copied), "total": len(plan),
                                  "move": move, "removed": removed,
                                  "target": target_profile,
                                  # names to drop from THIS profile's modlist
                                  # (remove_mods deliberately leaves modlist.txt
                                  # to the caller - mirror Tk _finish_copy_popup).
                                  "removed_names": list(copied) if removed else []})

        threading.Thread(target=_worker, daemon=True, name="copy-to-profile").start()

    def _on_copy_done(self, payload):
        """UI thread: report the copy/move result. For a move, drop the moved
        mods' rows from this profile's modlist (remove_mods left modlist.txt to
        us - mirrors Tk _finish_copy_popup calling _remove_selected_mods), which
        persists modlist.txt via the model, then reload."""
        self._copy_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        c = payload.get("copied", 0)
        self._notify(
            (self.tr("Moved {0}/{1} mod(s) to '{2}'.") if payload.get("move")
             else self.tr("Copied {0}/{1} mod(s) to '{2}'."))
            .format(c, payload.get('total', 0), payload.get('target', '')),
            "success" if c else "info")
        removed_names = set(payload.get("removed_names") or [])
        if removed_names:
            model = self._modlist_model
            # Remove by name, highest row first (indices shift as we delete).
            rows = [r for r in range(model.rowCount())
                    if (e := model.entry(r)) is not None
                    and not e.is_separator and e.name in removed_names]
            for r in sorted(rows, reverse=True):
                model.remove_row(r, save=False)
            if rows:
                model.save()  # single save → one filemap rebuild for the batch
        # Copy target may be a MEMBER of the active group - reconcile so the
        # new arrival shows up without a manual Refresh.
        reconciled = False
        try:
            from Utils.profile_groups import materialize_if_group
            reconciled = materialize_if_group(
                self._gs.game, self._gs.profile_dir(), log_fn=self._append_log)
        except Exception:
            pass
        if payload.get("removed") or reconciled:
            self._reload_modlist()

    # ---- install a Nexus mod by id (used by Missing Requirements cards) ----
    # Mirrors the Nexus browser's install flow: premium check → fetch files →
    # pick MAIN (or file chooser if several) → download → hand to _install_paths.
    # All worker stages hop back to the UI thread via Signals (NOT QThread).

    def _new_dl_key(self) -> str:
        """Unique tracking key for one download operation, so concurrent
        downloads can be told apart in the combined progress card."""
        self._dl_seq = getattr(self, "_dl_seq", 0) + 1
        return f"dl-{self._dl_seq}"

    def _nexus_download_progress(self, key: str, name: str,
                                 downloaded: int, total: int):
        """Drive the combined Nexus-download progress card. UI thread only.
        All concurrent downloads (each identified by *key*) aggregate into ONE
        card: the bar shows summed bytes across them. Finished downloads stay
        in the totals until the last one completes, so the bar never jumps
        backwards. total<0 marks *key* finished (done/failed/cancelled)."""
        self._ensure_feedback()
        if self._progress_popup is None:
            return
        dls = self._active_downloads
        if total < 0:
            e = dls.get(key)
            if e is not None and e["total"] > 0:
                e["done"] = e["total"]
                e["fin"] = True
            else:
                dls.pop(key, None)   # size never reported - just drop it
        else:
            e = dls.setdefault(key, {"fin": False})
            e["name"], e["done"], e["total"] = name, downloaded, total
        active = [e for e in dls.values() if not e["fin"]]
        if not active:
            dls.clear()
            self._progress_popup.clear(key="downloads")
            return
        if len(dls) == 1:
            nm = active[0].get("name") or ""
            phase = (self.tr("Downloading {0}…").format(nm)
                     if nm else self.tr("Downloading…"))
        else:
            phase = self.tr("Downloading {0} files ({1} remaining)…").format(
                len(dls), len(active))
        done = sum(e["done"] for e in dls.values() if e["total"] > 0)
        tot = sum(e["total"] for e in dls.values())
        if any(e["total"] <= 0 for e in active):
            done = tot = 0   # a size is still unknown - indeterminate bar
        self._progress_popup.set_progress(
            done, tot, phase, title=self.tr("Nexus Download"),
            bytes_mode=True, key="downloads")

    def _install_nexus_mod_by_id(self, mod_id: int, domain: str, name: str):
        if self._req_installing:
            self._notify(self.tr("An install is already in progress."), "info")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in to Nexus first."), "warning")
            return
        mod_id = int(mod_id or 0)
        if mod_id <= 0:
            self._notify(self.tr("That requirement has no Nexus mod page."), "warning")
            return
        self._req_installing = True
        ctx = {"mod_id": mod_id, "domain": domain, "name": name}
        self._append_log(f"[nexus] preparing install for {name}…")
        import threading

        def worker():
            files = None
            premium = False
            try:
                user = api.validate()
                premium = bool(getattr(user, "is_premium", False))
                if premium:
                    # [dev] force_manual_install = true → exercise the manual
                    # browser-download flow even on a premium account (same
                    # switch as the browser / Change Version / reinstall).
                    from Utils.ui_config import load_force_manual_install
                    if load_force_manual_install():
                        self._append_log("[nexus] [dev] force_manual_install - "
                                         "using the manual browser-download flow.")
                        premium = False
                # Fetch the file list for BOTH tiers: the chooser needs it, and
                # the non-premium folder watcher needs real names + sizes.
                resp = api.get_mod_files(domain, mod_id)
                files = list(resp.files)
            except Exception as exc:
                self._append_log(f"[nexus] install prep failed: {exc}")
                if not premium:
                    # No file list → no watch possible; fall back to the plain
                    # files page (site 'Download with Mod Manager' still works).
                    from Utils.xdg import open_url
                    open_url(f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                             f"?tab=files", log_fn=self._append_log)
                    self._append_log("[nexus] premium required for direct "
                                     "download - opened the files page (the "
                                     "download won't auto-install; use "
                                     "'Download with Mod Manager').")
            ctx["premium"] = premium
            self._req_install_files.emit(ctx, files)

        threading.Thread(target=worker, daemon=True, name="req-install-files").start()

    def _on_req_install_files(self, ctx, files):
        """UI thread: pick which file to install (chooser if >1 main/optional/misc),
        then route to the API download (premium) or the shared manual
        browser-download flow (non-premium / [dev] force_manual_install)."""
        if files is None:           # fetch error (fallback already handled)
            self._req_installing = False
            return
        from gui_qt.nexus_file_chooser import NexusFileChooser, installable_files
        picks = installable_files(files)
        if not picks:
            self._notify(self.tr("No downloadable files for that mod."), "warning")
            self._req_installing = False
            return
        start = (self._start_req_download if ctx.get("premium")
                 else self._start_req_manual)
        if len(picks) > 1:
            def _picked(chosen):
                if chosen is None:
                    self._req_installing = False
                    return
                start(ctx, chosen)

            NexusFileChooser.show_over(self, ctx["name"], picks, _picked)
        else:
            start(ctx, picks[0])

    def _start_req_download(self, ctx, f):
        domain, mod_id, name = ctx["domain"], ctx["mod_id"], ctx["name"]
        dl_label = f.file_name or name
        dl_key = self._new_dl_key()
        self._append_log(f"[nexus] downloading {dl_label}…")
        self._nexus_download_progress(dl_key, dl_label, 0, 0)   # show popup immediately

        class _Info:
            pass
        info = _Info()
        info.mod_id = mod_id
        info.domain_name = domain
        info.name = name
        import threading

        def worker():
            archive = meta = None
            try:
                from Nexus.nexus_download import NexusDownloader
                from Utils.config_paths import get_download_cache_dir_for_game
                from Nexus.nexus_meta import build_meta_from_download
                dest = get_download_cache_dir_for_game(
                    getattr(self._gs.game, "name", "") or "")
                size = (f.size_in_bytes or 0) or (f.size_kb * 1024)
                result = NexusDownloader(
                    self._nexus_api, download_dir=dest).download_file(
                    game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                    dest_dir=dest, known_file_name=f.file_name,
                    expected_size_bytes=size,
                    progress_cb=lambda d, t: self._req_install_prog.emit(
                        dl_key, dl_label, int(d), int(t)))
                if result.success and result.file_path is not None:
                    archive = str(result.file_path)
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain, mod_id=mod_id, file_id=f.file_id,
                            archive_name=result.file_name, mod_info=info,
                            file_info=f)
                    except Exception:
                        meta = None
                else:
                    self._append_log(f"[nexus] download failed: "
                                     f"{result.error or 'unknown error'}")
            except Exception as exc:
                self._append_log(f"[nexus] download error: {exc}")
            self._req_install_dl.emit(archive, meta, dl_key)

        threading.Thread(target=worker, daemon=True, name="req-install-dl").start()

    def _start_req_manual(self, ctx, f):
        """Non-premium (or [dev] force_manual_install) Missing-Requirements
        install via the shared start_manual_install flow: skip the browser when
        the archive is already downloaded, else open the file's download page +
        watch the download folders. 'Download with Mod Manager' still works -
        that nxm:// flow installs it and cancels this watch."""
        from Nexus.manual_download_watch import start_manual_install
        from Utils.xdg import open_url
        from gui_qt.safe_emit import safe_emit
        domain, mod_id, name = ctx["domain"], ctx["mod_id"], ctx["name"]
        dl_label = f.file_name or name
        dl_key = self._new_dl_key()
        self._nexus_download_progress(dl_key, dl_label, 0, 0)  # show popup card

        class _Info:            # requirement rows carry no NexusModInfo -
            pass                # minimal fallback for build_meta_from_download
        info = _Info()
        info.mod_id = mod_id
        info.domain_name = domain
        info.name = name

        # Helper callbacks run on the WATCHER thread - marshal via Signals.
        def on_archive(path, meta, _file):
            if not self._claim_app_manual_watch(mod_id, domain, dl_key):
                return
            safe_emit(self._req_install_dl, str(path), meta, dl_key)

        def on_progress(done, total):
            safe_emit(self._req_install_prog, dl_key, dl_label, int(done), int(total))

        def on_timeout():
            if not self._claim_app_manual_watch(mod_id, domain, dl_key):
                return
            self._op_log.emit(f"[nexus] stopped waiting for a browser download "
                              f"of '{dl_label}' (nothing arrived).")
            safe_emit(self._req_install_dl, None, None, dl_key)

        self.cancel_app_manual_watch(mod_id, domain)  # re-click → fresh watch
        watcher, _already = start_manual_install(
            api=self._nexus_api, game_domain=domain, mod_id=mod_id, files=[f],
            open_url_fn=lambda u: open_url(u, log_fn=self._append_log),
            log_fn=self._append_log, log_label=dl_label,
            mod_info_fallback=info,
            on_archive=on_archive, on_progress=on_progress,
            on_timeout=on_timeout)
        watch_key = self._app_manual_watch_key(domain, mod_id)
        self._app_manual_watchers[watch_key] = (watcher, dl_key)
        # The watch runs in the background - release the guard so the user can
        # install other missing requirements meanwhile (watches are per-mod).
        self._req_installing = False

    def _on_req_install_dl(self, archive, meta, dl_key):
        self._req_installing = False
        self._nexus_download_progress(dl_key, "", 0, -1)   # hide this download's card
        if not archive:
            return
        self._append_log(f"[nexus] downloaded → {archive}")
        self._deliver_download([archive],
                               {archive: meta} if meta is not None else None)

    def _on_modlist_flag_clicked(self, row: int, flag: int):
        """A flag icon in the modlist Flags column was clicked → its action
        (Tk parity, gui/modlist_panel ~3960): update→Change Version,
        modio-update→open mod.io page, missing→Missing Requirements,
        note→note editor, bundle→Bundle Options."""
        from gui_qt.modlist_data import (
            FLAG_UPDATE, FLAG_MISSING_REQS, FLAG_NOTE, FLAG_MODIO_UPDATE,
            FLAG_BUNDLE, FLAG_RERUN_FOMOD, FLAG_THUNDERSTORE_UPDATE)
        e = self._modlist_model.entry(row)
        if e is None or e.is_separator:
            return
        if flag == FLAG_UPDATE:
            self._open_change_version_tab(e.name)
        elif flag == FLAG_RERUN_FOMOD:
            # Re-run the FOMOD installer for this mod (its archive is re-extracted
            # and the wizard reopens). Reinstall rewrites fomodPendingDeps, so the
            # flag self-corrects once the now-relevant option is selected.
            self._reinstall_mods([e.name])
        elif flag == FLAG_MODIO_UPDATE:
            from gui_qt.modlist_menu import _open_on_modio
            _open_on_modio(self._modlist_view, e.name)
        elif flag == FLAG_THUNDERSTORE_UPDATE:
            self._update_thunderstore_mod(e.name)
        elif flag == FLAG_MISSING_REQS:
            self._open_missing_reqs_tab(e.name)
        elif flag == FLAG_NOTE:
            from gui_qt.modlist_menu import _open_note_editor
            _open_note_editor(self._modlist_view, [e.name])
        elif flag == FLAG_BUNDLE:
            self._open_bundle_tab(e.name)

    def _on_add_game_select(self, name: str):
        """A configured game was picked in the Add-Game view → switch to it and
        close the tab."""
        self._tabs.close_tab("add_game")
        if name in self._gs.game_names:
            self._on_game_changed(name)
            self._game_selector.set_current(name)
        self._append_log(f"[game] selected {name}")

    def _on_add_game_add(self, name: str):
        """An unconfigured game was picked → open the configure-game tab."""
        from Utils.game_helpers import _GAMES
        game = _GAMES.get(name)
        if game is None:
            self._append_log(f"[game] {name} not found in registry")
            return
        self._tabs.close_tab("add_game")
        self._open_configure_game_tab(game, from_add_game=True)

    def _open_configure_game_tab(self, game, from_add_game: bool = False):
        """Open the (live) Configure-Game view as a detachable tab.

        *from_add_game* is True when reached via the Add-Game picker (a brand-new
        instance). In that case Save closes the tab (the add flow is done); when
        reconfiguring an already-configured game, Save keeps the tab open so the
        user can keep tweaking (only Remove/Cancel close it)."""
        from gui_qt.configure_game_view import ConfigureGameView

        def _done(saved: bool, removed: bool):
            migrated = bool(getattr(page, "staging_migrated", False))
            if migrated:
                page.staging_migrated = False

            # Reconfigure saves keep the tab open; adding a game (or removing an
            # instance, or cancelling) closes it.
            if removed or not saved or from_add_game:
                self._tabs.close_tab("configure_game")
            if saved or removed:
                # Refresh the game registry + selector; switch to the game if it
                # is now configured, else fall back to the current/ first game.
                from Utils.game_helpers import _load_games
                names = _load_games()
                self._gs.game_names = names
                # _load_games returns the ["No games configured"] sentinel when
                # nothing is configured - don't surface that as a menu item;
                # show the "Add game" prompt instead.
                real_names = [n for n in names if n != "No games configured"]
                if real_names:
                    self._game_selector.set_items(
                        real_names, current=self._gs.game_name)
                else:
                    self._game_selector.set_items([], current=self.tr("Add game"))
                if saved and game.name in names:
                    self._on_game_changed(game.name)
                    self._game_selector.set_current(game.name)
                    # profile_ini_files / profile_saves may have just been
                    # toggled - refresh the Open submenu (a same-game save is a
                    # no-op for _on_game_changed).
                    self._refresh_profile_actions()
                elif removed:
                    self._append_log(f"[game] removed instance: {game.name}")
                    # Don't leave the manager pointed at the now-removed game -
                    # switch to the first remaining configured game, or fall
                    # back to the Add-Game picker if none are left.
                    remaining = [n for n in names
                                 if n != game.name and n != "No games configured"]
                    if remaining:
                        fallback = remaining[0]
                        self._on_game_changed(fallback)
                        self._game_selector.set_current(fallback)
                    else:
                        self._open_add_game_tab()

            # Staging files moved: re-sync the mods folder and rescan the index
            # against the NEW root (same work as the Refresh Modlist button).
            # Deferred so it runs after the game-switch reload above settles.
            if saved and not removed and migrated \
                    and self._gs.game is not None \
                    and self._gs.game.name == game.name:
                QTimer.singleShot(0, self._on_refresh_modlist)

            # Reconfigure kept the tab open - confirm the save inline and re-sync
            # its header (a per-profile override may have just been pinned).
            if saved and not removed and not from_add_game \
                    and self._tabs.has_key("configure_game"):
                v = getattr(self, "_configure_game_view", None)
                if v is not None:
                    try:
                        v.notify_saved()
                    except Exception:
                        pass

        page = ConfigureGameView(game, on_done=_done)
        self._configure_game_view = page
        self._tabs.open_tab(page, self._configure_tab_title(game),
                            key="configure_game")

    def _configure_tab_title(self, game) -> str:
        """Tab label for the configure view - includes the active profile name
        so the user can see which profile they're configuring."""
        verb = "Reconfigure" if game.is_configured() else "Add"
        prof = self._gs.profile
        if prof:
            return self.tr("{0} game - {1}").format(verb, prof)
        return self.tr("{0} game").format(verb)

    def _on_profile_action(self, which):
        if which == "add":
            if self._gs.game is None:
                self._append_log("[profile] no game selected.")
                return
            self._new_profile_bar.open_for()
        elif which == "remove":
            self._remove_current_profile()
        elif which == "settings":
            self._open_profile_settings_tab()
        elif which == "groups":
            self._open_profile_groups_tab()
        elif which == "export":
            self._open_export_profile_tab()
        elif which == "import":
            self._import_profile()
        elif which == "export_code":
            self._export_profile_code()
        elif which == "import_code":
            self._import_profile_code()
        else:
            self._append_log(f"[profile] {which} (not wired yet)")

    def _open_export_profile_tab(self):
        """Open the Export Profile panel scoped over the MODLIST panel (like Profile
        Settings): per-mod source/version/optional config + Export to a .amethyst."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._tabs.has_key("export_profile"):
            self._tabs.focus_key("export_profile")
            return
        api = self._ensure_nexus_api()   # optional - version/size fetch needs it
        from gui_qt.export_profile_view import ExportProfileView
        view = ExportProfileView(self, game, api, log_fn=self._append_log)
        self._export_profile_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Export Profile"), self._modlist_panel_stack,
            key="export_profile")

    def _open_create_collection_tab(self):
        """Open Create Collection (Nexus ▸ Collections) as a fullscreen tab:
        mod table on the left, collection info + Export/Upload panel on the
        right, backed by Utils.collection_export."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._tabs.has_key("create_collection"):
            self._tabs.focus_key("create_collection")
            return
        api = self._ensure_nexus_api()   # optional - version/size fetch needs it
        from gui_qt.create_collection_view import CreateCollectionView
        view = CreateCollectionView(self, game, api, log_fn=self._append_log)
        self._create_collection_view = view
        self._tabs.open_tab(view, self.tr("Create Collection"),
                            key="create_collection")

    def _open_my_collections_tab(self):
        """Open My Collections (Nexus ▸ Collections) as a fullscreen tab: the
        user's own collections with edit / changelog / publish / listing
        actions against the Nexus GraphQL API."""
        if self._tabs.has_key("my_collections"):
            self._tabs.focus_key("my_collections")
            return
        api = self._ensure_nexus_api()
        if api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ "
                                 "Login via SSO."), "warning")
            return
        from gui_qt.my_collections_view import MyCollectionsView
        view = MyCollectionsView(self, api, log_fn=self._append_log)
        self._my_collections_view = view
        self._tabs.open_tab(view, self.tr("My Collections"),
                            key="my_collections")

    def _import_profile(self):
        """Import a .amethyst / manifest: parse it, then reuse the collection detail
        + install pipeline (via CollectionDetailView with a local manifest) to build
        a new profile from it. Requires a configured game matching the manifest."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # No Nexus gate here: whether this import needs an account depends on
        # what is IN the manifest, which we only know once a file is picked.
        # _on_import_file_picked checks it (profile_export.manifest_needs_nexus).
        # Native picker via the XDG portal (portal_filechooser). The callback fires
        # on a WORKER thread - QTimer.singleShot(0, …) from there never fires (no
        # event loop on that thread), so marshal to the GUI thread with a Signal
        # (auto-queued to the receiver's thread).
        from Utils.portal_filechooser import pick_file
        pick_file(
            "Import profile",
            lambda p: self._import_file_picked.emit(p),
            filters=[("Amethyst Manifest (*.amethyst *.zip *.json)",
                      ["*.amethyst", "*.zip", "*.json"]),
                     ("All files", ["*"])])

    def _on_import_file_picked(self, picked):
        """GUI thread: continue the import once a file was chosen in the portal."""
        if not picked:
            return
        path = str(picked)
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        from Utils import profile_export
        try:
            manifest = profile_export.read_manifest(path)
        except Exception as exc:
            self._notify(self.tr("Could not read manifest: {0}").format(exc), "error")
            return
        if not isinstance(manifest, dict) or not manifest.get("mods"):
            self._notify(self.tr("That file doesn't look like an Amethyst manifest."),
                         "warning")
            return
        # Only a manifest with real Nexus entries needs an account: Thunderstore
        # downloads are public, and a Thunderstore-only game (Risk of Rain 2,
        # Inscryption) has no Nexus domain to log in against at all.
        if (profile_export.manifest_needs_nexus(manifest)
                and self._ensure_nexus_api() is None):
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        bundle_zip = path if Path(path).suffix.lower() in (".amethyst", ".zip") else ""
        self._open_manifest_import(manifest, Path(path).stem, bundle_zip=bundle_zip)

    def _open_manifest_import(self, manifest, source_stem, *, bundle_zip="",
                              allow_append=False):
        """Shared import continuation: validate the manifest's game domain against
        the selected game, then open the collection detail tab (which drives the
        install pipeline). Used by both file-import and code-import. *allow_append*
        permits appending into an existing profile (safe only when there is no
        bundle - i.e. a code import)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        from Utils import profile_export as _pe
        # A Thunderstore-only / fully-bundled manifest imports with no account:
        # see _on_import_file_picked. api stays None in that case and every
        # Nexus-touching step downstream is skipped along with it.
        api = self._ensure_nexus_api()
        if api is None and _pe.manifest_needs_nexus(manifest):
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ Login via SSO."),
                         "warning")
            return
        # The selected game may explicitly accept additional Nexus domains.
        man_domain = ((manifest.get("info") or {}).get("domainName") or "").strip()
        game_domain = (getattr(game, "nexus_game_domain", "") or "").strip()
        if man_domain and game_domain and not game.accepts_nexus_domain(man_domain):
            self._notify(
                self.tr("This profile targets '{0}', but the selected game is '{1}'. Switch games first, then import.").format(man_domain, game_domain), "warning")
            return

        # Build a bare NexusCollection + open the detail tab populated from the
        # manifest; the bundle zip (if a .amethyst) restores mods/profile after.
        from Nexus.nexus_api import NexusCollection
        name = (manifest.get("info") or {}).get("name") or source_stem
        collection = NexusCollection(
            id=0, slug=f"import_{source_stem}", name=name,
            game_domain=man_domain or game_domain)
        key = f"import_profile_{source_stem}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        from gui_qt.collection_detail_view import CollectionDetailView
        view = CollectionDetailView(
            api, collection, game, log_fn=self._append_log,
            local_manifest=manifest, bundle_zip=bundle_zip,
            allow_append=allow_append)
        view.set_install_handler(
            lambda chosen, skipped, intent="install": self._install_collection(
                collection, view, chosen, skipped, intent))
        self._tabs.open_tab(view, self.tr("Import: {0}").format(name), key=key)

    # ---- Share code: export / import a modlist as a text string -----------
    def _export_profile_code(self):
        """Build a compact share code for the active profile (mods with a
        modId + fileId, FOMOD/BAIN choices, load order, enabled state,
        separators) and show it with a Copy-to-clipboard overlay. The build
        runs on a worker thread (modlist + sidecar reads)."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # No API call is needed to build the code (file sizes come from each
        # mod's meta.ini, stamped at install time) - but the modlist read +
        # FOMOD/BAIN sidecar reads still run off the UI thread.
        import threading
        threading.Thread(
            target=self._export_code_worker, args=(game,),
            daemon=True, name="export-code").start()

    def _export_code_worker(self, game):
        try:
            from Utils import profile_export
            from Utils.modlist import read_modlist
            pd = getattr(game, "_active_profile_dir", None)
            modlist_path = (Path(pd) / "modlist.txt") if pd else None
            if not modlist_path or not modlist_path.is_file():
                self._export_code_ready.emit(None, 0)
                return
            # read_modlist returns index 0 = highest priority (top of modlist);
            # build_code_manifest expects exactly that order. Separators are
            # passed through - the manifest carries them as modlistSeparators.
            entries = read_modlist(modlist_path)
            try:
                from version import __version__ as app_version
            except Exception:
                app_version = ""
            profile_name = Path(pd).name if pd else None
            manifest = profile_export.build_code_manifest(
                entries, game, app_version, profile_name=profile_name)
            mods = manifest.get("mods") or []
            if not mods:
                self._export_code_ready.emit("", 0)
                return
            code = profile_export.encode_manifest(manifest)
            self._export_code_ready.emit(code, len(mods))
        except Exception as exc:
            self._append_log(f"[export-code] failed: {exc}")
            self._export_code_ready.emit(None, -1)

    def _on_export_code_ready(self, code, mod_count):
        if code is None:
            if mod_count == -1:
                self._notify(self.tr("Could not build share code."), "error")
            else:
                self._notify(self.tr("No active profile to export."), "warning")
            return
        if not code:
            self._notify(
                self.tr("No mods to share - a code carries Nexus mods with a "
                        "mod + file ID and Thunderstore mods."), "warning")
            return
        from gui_qt.share_code_overlay import ShareCodeExportOverlay
        ShareCodeExportOverlay(self.window(), code, mod_count)
        self._append_log(f"[export-code] built code for {mod_count} mod(s).")

    def _import_profile_code(self, code: str = ""):
        """Prompt for a pasted share code, decode it, then reuse the manifest
        import pipeline to build a new profile from it.

        *code* prefills the paste box - how a share code clicked in the wiki tab
        gets here. The overlay still opens either way, so its preview shows what
        the code holds before a profile is built from it."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # The Nexus gate moved into _got: whether an account is needed depends
        # on what the decoded code contains (_open_manifest_import re-checks).

        def _got(text):
            if not text:
                return
            from Utils import profile_export
            try:
                manifest = profile_export.decode_manifest(text)
            except Exception as exc:
                self._notify(self.tr("Could not read code: {0}").format(exc), "error")
                return
            # Name the imported profile "<source profile> - Code". The source name
            # is carried in the manifest's info.name; overwrite it with the "- Code"
            # form so both the collection/profile name and the import tab title use
            # it (build_code_manifest stores the bare name, so re-exporting an
            # imported profile won't stack "- Code - Code").
            info = manifest.setdefault("info", {})
            src_name = (info.get("name") or "Imported").strip()
            info["name"] = f"{src_name} - Code"
            # A code has no bundle, so appending into an existing profile is safe.
            self._open_manifest_import(
                manifest, f"{src_name}_code", bundle_zip="", allow_append=True)

        from gui_qt.share_code_overlay import ShareCodeImportOverlay
        ShareCodeImportOverlay(self.window(), _got, initial_code=code or "")

    def _open_profile_settings_tab(self):
        """Open the Profile Settings panel scoped over the MODLIST panel (like the
        Settings gear / image preview): profile rows with lock / rename / open /
        remove, while the plugins panel + the rest of the UI stay live."""
        if self._gs.game_name is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        if self._tabs.has_key("profile_settings"):
            self._tabs.focus_key("profile_settings")
            return
        view = self._make_profile_settings_view()
        self._profile_settings_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Profile Settings"), self._modlist_panel_stack,
            key="profile_settings")

    def _set_profile_selector_items(self, profs, current=None):
        """set_items on the profile selector: Profile Groups are badged with
        the collection icon and listed LAST, split from plain profiles by a
        separator, so merged profiles read as their own section."""
        profs = list(profs)
        # Always a REAL dict/set - set_items keeps the previous icon map when
        # passed None, so a deleted group's badge would stick to any later
        # profile reusing its name.
        icons: dict = {}
        seps: set = set()
        try:
            from Utils.profile_groups import list_groups
            g = self._gs.game
            if g is not None:
                groups = set(list_groups(g))
                if groups:
                    plain = [n for n in profs if n not in groups]
                    grouped = [n for n in profs if n in groups]
                    profs = plain + grouped
                    if plain and grouped:
                        seps = {grouped[0]}
                    from gui_qt.icons import icon
                    ico = icon("collection.png", 16)
                    icons = {n: ico for n in grouped}
        except Exception:
            icons = {}
            seps = set()
        self._profile_selector.set_items(profs, current=current,
                                         item_icons=icons,
                                         separator_before=seps)

    def _open_profile_groups_tab(self):
        """Open the Profile Groups panel scoped over the MODLIST panel (like
        Profile Settings): create/manage groups, reorder members, convert
        shared-pool profiles to profile-specific."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not getattr(game, "profile_groups_supported", True):
            self._notify(self.tr("Profile Groups aren't supported for this "
                                 "game."), "warning")
            return
        if self._tabs.has_key("profile_groups"):
            self._tabs.focus_key("profile_groups")
            return
        from gui_qt.profile_groups_view import ProfileGroupsView
        view = ProfileGroupsView(
            self, game, self._gs.profile,
            on_groups_changed=self._on_groups_changed,
            log_fn=self._append_log,
        )
        self._profile_groups_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Profile Groups"), self._modlist_panel_stack,
            key="profile_groups")

    def _on_groups_changed(self):
        """A group was created/edited/removed (or a profile converted). The
        profile list may have gained/lost entries, and if the ACTIVE profile
        is a group its modlist was just re-materialized - reload it."""
        self._set_profile_selector_items(self._gs.profiles(),
                                         current=self._gs.profile)
        self._refresh_profile_actions()
        try:
            from Utils.profile_groups import is_group
            if is_group(self._gs.profile_dir()):
                self._reload_modlist()
                self._reload_plugins()
        except Exception as exc:
            print(f"[gui_qt] group-change reload failed: {exc}", flush=True)

    def _make_profile_settings_view(self):
        """Build a ProfileSettingsView wired to the app's selector/reload
        callbacks. Used both by the scoped tab and the dropdown's Remove action."""
        from gui_qt.profile_settings_view import ProfileSettingsView
        return ProfileSettingsView(
            self,
            game_name=self._gs.game_name,
            current_profile=self._gs.profile,
            on_profile_renamed=self._on_profile_renamed,
            on_profile_removed=self._on_profile_removed,
            on_profiles_changed=self._on_profiles_lock_changed,
            log_fn=self._append_log,
        )

    def _current_profile_locked(self) -> bool:
        """True when the active profile is locked (or the original default) and so
        must not be removable from the dropdown. Mirrors the Profile Settings row
        gate without opening the tab."""
        if self._gs.game_name is None or self._gs.profile is None:
            return True
        try:
            view = self._make_profile_settings_view()
            return view._is_profile_locked(self._gs.profile)
        except Exception:
            return True

    def _remove_current_profile(self):
        """Remove the active profile via the same flow as Profile Settings'
        Remove button (confirm overlays, restore-if-deployed, delete, selector
        reload). Kept alive on self until the async remove worker finishes."""
        if self._gs.game_name is None or self._gs.profile is None:
            self._notify(self.tr("No profile selected."), "warning")
            return
        profile = self._gs.profile
        view = self._make_profile_settings_view()
        if view._is_profile_locked(profile):
            return
        # Retain a reference - the remove runs on a worker thread and emits back
        # into the (parentless) view; without this it could be collected mid-op.
        self._profile_remove_helper = view
        view._on_remove(profile)

    # -- Profile Settings callbacks (view → app: refresh selector + reload) --
    def _on_profiles_lock_changed(self):
        """A profile's lock toggled - the active profile is unchanged, so just
        refresh the selector list (Remove-eligibility, etc.)."""
        self._set_profile_selector_items(self._gs.profiles(),
                                         current=self._gs.profile)
        # Lock state feeds the "Remove current profile…" gate - rebuild the
        # pinned actions so unlocking the active profile reveals Remove (and
        # locking it hides Remove) without reopening the dropdown.
        self._refresh_profile_actions()

    def _on_profile_renamed(self, old: str, new: str):
        # A renamed profile may be a Profile Group member - update every
        # group's member list and retarget their links.
        try:
            from Utils.profile_groups import rename_profile_everywhere
            updated = rename_profile_everywhere(self._gs.game, old, new,
                                                log_fn=self._append_log)
            if updated:
                self._append_log(
                    f"Profile Groups updated for rename: {', '.join(updated)}")
        except Exception as exc:
            print(f"[gui_qt] group rename propagation failed: {exc}", flush=True)
        profs = self._gs.profiles()
        if self._gs.profile == old:
            # The active profile was renamed → switch GameState + reload.
            self._gs.set_profile(new)
            self._set_profile_selector_items(profs, current=new)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._set_profile_selector_items(profs, current=self._gs.profile)
        # The active profile (or its name) may have changed - rebuild the pinned
        # actions so Remove-eligibility tracks the current profile.
        self._refresh_profile_actions()
        self._update_deployed_profile_highlight()

    def _on_profile_removed(self, name: str):
        # Prune the deleted profile from every group's member list; affected
        # groups re-materialize (their entries/links for it are dropped).
        try:
            from Utils.profile_groups import remove_profile_everywhere
            affected = remove_profile_everywhere(self._gs.game, name,
                                                 log_fn=self._append_log)
            if affected:
                self._append_log(
                    f"Removed '{name}' from Profile Group(s): {', '.join(affected)}")
        except Exception as exc:
            print(f"[gui_qt] group remove propagation failed: {exc}", flush=True)
        profs = self._gs.profiles()
        if self._gs.profile == name:
            # The active profile was removed → fall back like the view did.
            target = "default" if "default" in profs else (
                profs[0] if profs else "default")
            self._gs.set_profile(target)
            self._set_profile_selector_items(profs, current=target)
            self._reload_modlist()
            self._reload_plugins()
        else:
            self._set_profile_selector_items(profs, current=self._gs.profile)
        # Fall-back to default (or any other switch) changes Remove-eligibility -
        # rebuild the pinned actions so Remove hides on the locked default.
        self._refresh_profile_actions()
        self._update_deployed_profile_highlight()
        # The removed profile may have been a group (or a group member) - the
        # Profile Groups tab lists both, so refresh it in place if open.
        if self._tabs.has_key("profile_groups"):
            v = getattr(self, "_profile_groups_view", None)
            if v is not None:
                try:
                    v.set_current_profile(self._gs.profile)
                    v._populate()
                except Exception:
                    pass

    def _on_new_profile_create(self, name: str, profile_specific_mods: bool):
        """Create a new profile for the active game and switch to it. Mirrors the
        Tk top_bar._on_add_profile flow: reject an existing name, create via the
        neutral _create_profile, repopulate the selector, select + reload."""
        from Utils.game_helpers import _create_profile, _profiles_for_game
        game_name = self._gs.game_name
        if not game_name:
            return
        existing = _profiles_for_game(game_name)
        if name in existing:
            self._notify(self.tr("Profile '{0}' already exists.").format(name), "error")
            # Re-open so the user can pick another name (fields reset).
            self._new_profile_bar.open_for()
            return
        try:
            _create_profile(game_name, name,
                            profile_specific_mods=profile_specific_mods)
        except Exception as exc:
            self._append_log(f"[profile] create failed: {exc}")
            self._notify(self.tr("Could not create profile: {0}").format(exc), "error")
            return
        # Success - make sure the bar is closed (the widget hides itself before
        # calling us, but close explicitly so the flow is robust to any caller).
        self._new_profile_bar.close()
        self._append_log(f"[profile] created '{name}'"
                         + (" (profile-specific mods)" if profile_specific_mods
                            else ""))
        # Refresh the profile selector, select the new profile, and load it.
        profs = self._gs.profiles()
        self._set_profile_selector_items(profs, current=name)
        self._gs.set_profile(name)
        self._profile_selector.set_current(name)
        # Rebuild the pinned actions so "Remove current profile…" appears now
        # that the active profile is a fresh (unlocked, removable) one.
        self._refresh_profile_actions()
        self._reload_modlist()
        self._reload_plugins()
        self._update_deployed_profile_highlight()
        self._notify(self.tr("Profile '{0}' created").format(name), "info")

    def _update_deployed_profile_highlight(self):
        """Green-highlight the deployed profile in the profile dropdown. Reads the
        same backend state the Tk app writes (game deploy-state JSON via
        get_deploy_active / get_last_deployed_profile)."""
        game = self._gs.game
        deployed = None
        try:
            if game is not None and game.get_deploy_active():
                deployed = game.get_last_deployed_profile()
        except Exception:
            deployed = None
        if hasattr(self, "_profile_selector"):
            self._profile_selector.set_highlighted_item(deployed)

    # ------------------------------------------------------------- play bar
    def _refresh_play_selector(self):
        """Repopulate the play-bar dropdown: the game + custom exes, restoring
        this profile's saved selection (Tk stores the label in profile_state)."""
        # Script extenders are deliberately NOT auto-listed any more - a loader
        # only shows up when the user added it themselves (Add custom EXE / from
        # game folder), where it launches exactly as before. The auto machinery
        # (exe_launch.detect_framework_exes, _play_auto_exe_names, hide_auto_exe,
        # the force-deploy branch in _do_play) is left intact.
        game = self._gs.game
        if game is None:
            self._play_exe_paths = {}
            self._play_auto_exe_names = set()
            self._play_exe_selector.set_items(["-"], current="-")
            return
        from Utils.exe_launch import load_custom_exes
        self._play_exe_paths = {}
        self._play_auto_exe_names = set()
        items = [game.name]
        for p in load_custom_exes(game):
            if p.name not in self._play_exe_paths and p.name != game.name:
                self._play_exe_paths[p.name] = p
                items.append(p.name)
        current = game.name
        pdir = self._gs.profile_dir()
        if pdir is not None:
            from Utils.profile_state import read_selected_exe
            saved = read_selected_exe(pdir)
            if saved in self._play_exe_paths:
                current = saved
        self._play_exe_selector.set_items(
            items, current=current, item_icons=self._build_exe_icon_map(game))
        self._update_play_btn_label(current)

    def _build_exe_icon_map(self, game) -> dict:
        """{label: QIcon} for the play-bar dropdown: the game's logo for the
        game entry, each custom exe's own extracted icon for the exe entries
        (falling back to the game logo when an exe has no embeddable icon).

        Icons are loaded at 2x the display size so the ~28px play-bar face + menu
        entries stay crisp on HiDPI (QIcon.paint downscales cleanly)."""
        px = 56
        icons: dict = {}
        game_logo = None
        try:
            from gui_qt.add_game_view import _game_logo
            pm = _game_logo(getattr(game, "game_id", "") or game.name, px)
            if pm is not None and not pm.isNull():
                from PySide6.QtGui import QIcon
                game_logo = QIcon(pm)
        except Exception:
            game_logo = None
        if game_logo is not None:
            icons[game.name] = game_logo
        from gui_qt.icons import exe_icon
        for name, path in self._play_exe_paths.items():
            ic = exe_icon(path, px)
            if ic is None:
                ic = game_logo
            if ic is not None:
                icons[name] = ic
        return icons

    def _update_play_btn_label(self, label: str):
        game = self._gs.game
        is_game = game is not None and label == game.name
        self._play_btn.setText(self.tr("▶  Play") if is_game else self.tr("▶  Run"))

    def _on_play_exe_selected(self, label):
        game = self._gs.game
        pdir = self._gs.profile_dir()
        if pdir is not None:
            from Utils.profile_state import write_selected_exe
            is_game = game is not None and label == game.name
            write_selected_exe(pdir, None if is_game else label)
        self._update_play_btn_label(label)

    def _on_add_custom_exe(self):
        if self._gs.game is None:
            self._notify(self.tr("No game selected."), "warning")
            return
        # Picker callback fires on the portal WORKER thread → marshal via Signal.
        from Utils.exe_launch import EXE_PICKER_FILTERS
        from Utils.portal_filechooser import pick_file
        pick_file("Select executable",
                  lambda p: self._custom_exe_picked.emit(p),
                  filters=EXE_PICKER_FILTERS)

    def _on_custom_exe_picked(self, path):
        game = self._gs.game
        if path is None or game is None:
            return
        from Utils.exe_launch import add_custom_exe
        add_custom_exe(game, path)
        self._refresh_play_selector()
        if path.name in self._play_exe_paths:
            self._play_exe_selector.set_current(path.name)
            self._on_play_exe_selected(path.name)

    def _on_add_exe_from_staging(self):
        game = self._gs.game
        if game is None or not hasattr(game, "get_mod_staging_path"):
            self._notify(self.tr("No game selected."), "warning")
            return
        from Utils.exe_launch import scan_staging_exes
        exes = scan_staging_exes(game)
        if not exes:
            self._notify(self.tr("No executables found in staging."), "info")
            return
        # Label each exe with a dim relative-path hint so duplicate basenames
        # from different mods are distinguishable in the picker.
        root = game.get_mod_staging_path().parent
        items = []
        for p in exes:
            try:
                hint = str(p.parent.relative_to(root))
            except ValueError:
                hint = str(p.parent)
            items.append((f"{p.name}  -  {hint}", p))
        from gui_qt.staging_exe_picker_overlay import StagingExePickerOverlay
        StagingExePickerOverlay.show_over(
            self.centralWidget() or self, items,
            on_done=self._on_staging_exes_picked)

    def _on_add_exe_from_game_folder(self):
        game = self._gs.game
        game_path = (game.get_game_path()
                     if game is not None and hasattr(game, "get_game_path")
                     else None)
        if game_path is None:
            self._notify(self.tr("No game folder configured."), "warning")
            return
        from Utils.exe_launch import scan_game_folder_exes
        exes = scan_game_folder_exes(game)
        if not exes:
            self._notify(self.tr("No executables found in the game folder."),
                         "info")
            return
        # Same dim relative-path hint as the staging picker; root-level exes
        # get no hint (relative parent is ".").
        items = []
        for p in exes:
            try:
                hint = str(p.parent.relative_to(game_path))
            except ValueError:
                hint = str(p.parent)
            items.append((p.name if hint == "." else f"{p.name}  -  {hint}", p))
        from gui_qt.staging_exe_picker_overlay import StagingExePickerOverlay
        StagingExePickerOverlay.show_over(
            self.centralWidget() or self, items,
            on_done=self._on_staging_exes_picked,
            title=self.tr("Add executable from game folder"),
            subtitle=self.tr(
                "Check the executables to add to the Run menu. These run "
                "from their location in the game folder - including files "
                "deployed there by mods."))

    def _on_staging_exes_picked(self, paths):
        game = self._gs.game
        if not paths or game is None:
            return
        from Utils.exe_launch import add_custom_exe
        for path in paths:
            add_custom_exe(game, path)
        self._refresh_play_selector()
        # Select the single added exe (mirror the custom-exe picker); leave the
        # current selection alone when several were added at once.
        if len(paths) == 1 and paths[0].name in self._play_exe_paths:
            self._play_exe_selector.set_current(paths[0].name)
            self._on_play_exe_selected(paths[0].name)

    def _on_play(self):
        # An exception escaping a Qt slot only prints to stderr - invisible in
        # a flatpak/AppImage GUI launch, so the button would "do nothing".
        # Surface it in the log + a toast instead.
        try:
            self._do_play()
        except Exception as exc:
            import traceback
            self._append_log(f"Play error: {exc!r}")
            for ln in traceback.format_exc().strip().splitlines():
                self._append_log(f"Play error:   {ln}")
            self._play_failed(f"{exc!r}")

    def _play_log(self, message: str):
        """Play-path log line. Also remembered as the failure reason: launch
        paths that give up do so with a log line and a bare return, so the last
        one logged is why nothing started."""
        self._append_log(message)
        from Utils import launch_report
        launch_report.note(message)

    def _start_play_toast(self, text: str):
        """Sticky toast raised the moment Play is pressed.

        Proton/Steam can take ten seconds to put a window on screen, and until
        then nothing in the UI changes - the button looked like it had swallowed
        the click. The toast stays up until the launch resolves (or the deploy
        that precedes it fails), so there is always something on screen saying
        the press was received.
        """
        self._end_play_toast()          # never stack two launches' toasts
        self._play_toast_handle = self._notify(text, "info", sticky=True)

    def _set_play_toast(self, text: str):
        """Retitle the live Play toast (deploy-before-launch → launching)."""
        handle = self._play_toast_handle
        if handle is not None:
            handle.set_text(text)

    def _end_play_toast(self, text: str = "", state: str = "success"):
        """Resolve the Play toast. UI thread only - worker threads emit
        `_play_toast_done` instead. With no *text* the toast just disappears."""
        handle, self._play_toast_handle = self._play_toast_handle, None
        if handle is not None:
            handle.dismiss(text or None, state if text else None)
        elif text:
            # The sticky toast is already gone - a watcher thread caught the
            # game dying seconds after it started. Say so anyway.
            self._notify(text, state)

    def _play_failed(self, detail: str = "", entry: str = ""):
        """Popup for a Play click that never got anything running.

        Thread-safe - the signal hop puts the card on the UI thread. The advice
        is to deploy and start from the launcher: Steam/Heroic/Lutris/Faugus
        own the prefix and runtime, so a launch our side can't complete
        usually still works from there, with the deployed mods active either
        way.
        """
        target = entry or (self._gs.game.name if self._gs.game is not None
                           else self.tr("the game"))
        body = self.tr(
            "Amethyst could not launch {0}.\n\n"
            "Press Deploy to apply your mods, then start the game from "
            "Steam, Heroic, Lutris or Faugus instead - the deployed mods "
            "stay active however the game is started.").format(target)
        if detail:
            body += "\n\n" + self.tr("Details: {0}").format(detail)
        self._play_toast_done.emit(
            self.tr("{0} did not launch").format(target), "error")
        self._warn_popup.emit(self.tr("The game did not launch"), body,
                              340 if detail else 280)

    def _do_play(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # The Play button is already disabled while a texture tool runs, but
        # Play can also auto-deploy first - and that deploy would be refused
        # mid-launch. Stop here with a clear reason instead.
        if self._tool_busy:
            self._notify(self.tr("{0} is running - launch again when it "
                                 "finishes.").format(self._tool_busy_label()),
                         "warning")
            return
        import threading
        from Utils import exe_launch
        label = self._play_exe_selector.current()
        exe_path = self._play_exe_paths.get(label)
        # First breadcrumb of every launch: proves the click reached the
        # handler and records which dropdown entry it resolved to.
        self._append_log(
            f"Play: pressed - game '{game.name}', entry '{label}'"
            + ("" if exe_path is None else f" ({exe_path})"))
        callback = game.play_button_callback
        if callback is not None:
            callback()
        self._start_play_toast(self.tr("Launching {0}…").format(label))
        if exe_path is not None:
            # If this exe belongs to a wizard tool (xEdit, BodySlide, Script
            # Merger, …), open the wizard instead of a bare Proton launch - the
            # wizard handles the install/deploy/prefix setup a raw launch skips.
            # Only divert when a Qt view exists for the wizard; otherwise fall
            # through and launch the exe normally.
            from Utils.plugin_loader import wizard_tool_for_exe
            tool = wizard_tool_for_exe(game, exe_path.name)
            if tool is not None:
                from wizards_qt import get_spec
                if get_spec(tool.dialog_class_path) is not None:
                    self._end_play_toast(
                        self.tr("Opening {0}…").format(label), "info")
                    self._open_wizard_tool(tool)
                    return
            # Custom exe → Proton in the game prefix (or per-exe override).
            is_auto = label in self._play_auto_exe_names
            can_deploy = hasattr(game, "deploy")
            if not exe_path.is_file() and not (is_auto and can_deploy):
                self._play_failed(
                    self.tr("Executable not found: {0}").format(exe_path),
                    entry=label)
                return
            target = (exe_launch.launch_jar if exe_launch.is_jar(exe_path)
                      else exe_launch.launch_exe_via_proton)

            def _launch_exe():
                run_path = exe_path
                if not run_path.is_file():
                    # Staged extender materialised by the deploy that just
                    # ran - re-resolve so on-disk casing wins.
                    from Utils.framework_detect import resolve_file_ci
                    gp = game.get_game_path() if hasattr(game, "get_game_path") else None
                    resolved = None
                    if gp is not None:
                        try:
                            resolved = resolve_file_ci(gp, run_path.relative_to(gp))
                        except ValueError:
                            resolved = None
                    if resolved is None:
                        self._play_failed(
                            self.tr("Executable not found: {0}").format(run_path),
                            entry=label)
                        return
                    run_path = resolved

                def _run(run_path=run_path):
                    # Worker-thread exceptions otherwise vanish to stderr, and
                    # most launch paths fail by logging and returning - the
                    # report catches both, plus a process that dies instantly.
                    from Utils import launch_report
                    with launch_report.report(
                            lambda d: self._play_failed(d, entry=label)) as rep:
                        try:
                            target(run_path, game, log_fn=self._play_log)
                        except Exception as exc:
                            self._append_log(f"Run EXE error: {exc!r}")
                            rep.mark_failed(f"{exc!r}")
                        else:
                            rep.finish()
                            if rep.spawned:
                                self._play_toast_done.emit(
                                    self.tr("{0} started").format(label),
                                    "success")
                threading.Thread(target=_run, daemon=True).start()

            # Auto-detected script extenders must run against the CURRENT
            # profile's files: deploy first when the exe is only staged (not
            # deployed yet) or when a different profile (or nothing) is the
            # one deployed. Otherwise the per-exe "deploy on run" setting
            # decides, exactly like a manual custom exe.
            force_deploy = False
            if is_auto and can_deploy:
                if not exe_path.is_file():
                    force_deploy = True
                else:
                    try:
                        deployed = (game.get_last_deployed_profile()
                                    if game.get_deploy_active() else None)
                    except Exception:
                        deployed = None
                    force_deploy = deployed != self._gs.profile

            # Deploy exactly like the Deploy button, then launch once the
            # (final, non-coalesced) deploy succeeds.
            if can_deploy and (force_deploy
                               or exe_launch.load_deploy_on_run(game, exe_path.name)):
                self._set_play_toast(
                    self.tr("Deploying, then launching {0}…").format(label))

                def _deploy_then_launch():
                    self._set_play_toast(
                        self.tr("Launching {0}…").format(label))
                    _launch_exe()

                self._post_deploy_action = _deploy_then_launch
                self._on_deploy()
            else:
                _launch_exe()
            return

        # Game entry → optional deploy first, then Steam/Heroic/Proton routing.
        def _launch():
            def _run():
                # Worker-thread exceptions otherwise vanish to stderr, and most
                # launch paths fail by logging and returning - the report
                # catches both, plus a process that dies instantly.
                from Utils import launch_report
                with launch_report.report(self._play_failed) as rep:
                    try:
                        exe_launch.launch_game(game, log_fn=self._play_log)
                    except Exception as exc:
                        self._append_log(f"Play error: {exc!r}")
                        rep.mark_failed(f"{exc!r}")
                    else:
                        rep.finish()
                        if rep.spawned:
                            self._play_toast_done.emit(
                                self.tr("{0} started").format(game.name),
                                "success")
            threading.Thread(target=_run, daemon=True).start()

        if exe_launch.load_deploy_before_launch(game) and hasattr(game, "deploy"):
            self._set_play_toast(
                self.tr("Deploying, then launching {0}…").format(game.name))

            def _deploy_then_launch():
                self._set_play_toast(
                    self.tr("Launching {0}…").format(game.name))
                _launch()

            self._post_deploy_action = _deploy_then_launch
            self._on_deploy()
        else:
            _launch()

    def _on_play_action(self, which):
        game = self._gs.game
        if game is None:
            self._append_log(f"[play] {which}: no game selected")
            return
        if which == "folder":
            if not hasattr(game, "get_mod_staging_path"):
                self._append_log("[play] could not determine the Applications folder")
                return
            from Utils.xdg import xdg_open
            apps_dir = game.get_mod_staging_path().parent / "Applications"
            try:
                apps_dir.mkdir(parents=True, exist_ok=True)
                xdg_open(apps_dir)
            except Exception as e:
                self._append_log(f"[play] could not open Applications folder: {e}")
        elif which == "settings":
            label = self._play_exe_selector.current()
            exe_path = self._play_exe_paths.get(label)
            if exe_path is None:
                self._open_launcher_settings(game)
            else:
                self._open_exe_settings_tab(exe_path)

    def _open_launcher_settings(self, game):
        """Borderless overlay with the game-launch settings (Tk: game-exe
        branch of the Configure dialog)."""
        from Utils import exe_launch
        from gui_qt.launcher_settings_overlay import LauncherSettingsOverlay
        exe_key = exe_launch.game_exe_key(game)

        def _done(mode, deploy, args, options):
            if mode is None:
                return
            exe_launch.save_launch_mode(game, exe_key, mode)
            exe_launch.save_deploy_before_launch(game, deploy)
            # Same keys the direct-launch path reads, so these apply to every
            # launch we make ourselves (Steam's own options are the fallback).
            exe_launch.save_exe_args(game, exe_key, args)
            exe_launch.save_launch_options(game, exe_key, options)
            self._append_log(f"[play] launch settings saved (via={mode}, "
                             f"deploy-before-launch={'on' if deploy else 'off'}"
                             f"{', args' if args else ''}"
                             f"{', options' if options else ''})")

        LauncherSettingsOverlay.show_over(
            self.centralWidget() or self,
            game_name=game.name,
            mode=exe_launch.load_launch_mode(game, exe_key),
            deploy=exe_launch.load_deploy_before_launch(game),
            args=exe_launch.load_exe_args(game, exe_key),
            options=exe_launch.load_launch_options(game, exe_key),
            on_done=_done,
        )

    def _open_exe_settings_tab(self, exe_path):
        """Per-exe settings as a plugins-panel-scoped tab (Tk: ExeConfigPanel
        overlay)."""
        game = self._gs.game
        if game is None:
            return
        if self._tabs.has_key("exe_settings"):
            self._tabs.close_tab("exe_settings")
            self._exe_settings_view = None
        from gui_qt.exe_settings_view import ExeSettingsView

        def _close(removed: bool):
            self._close_exe_settings_tab()
            if removed:
                # Drop a stale per-profile selection pointing at the removed exe.
                pdir = self._gs.profile_dir()
                if pdir is not None:
                    from Utils.profile_state import (read_selected_exe,
                                                     write_selected_exe)
                    if read_selected_exe(pdir) == exe_path.name:
                        write_selected_exe(pdir, None)
                self._refresh_play_selector()

        view = ExeSettingsView(
            game=game,
            exe_path=exe_path,
            on_close=_close,
            log_fn=self._append_log,
            is_auto=exe_path.name in self._play_auto_exe_names,
        )
        self._exe_settings_view = view
        self._tabs.open_scoped_tab(view, self.tr("Configure: {0}").format(exe_path.name),
                                   self._plugins_panel_stack, key="exe_settings")

    def _close_exe_settings_tab(self):
        if self._tabs.has_key("exe_settings"):
            self._tabs.close_tab("exe_settings")
        self._exe_settings_view = None

    # ------------------------------------------------------------- deploy/restore
    def _ensure_feedback(self):
        """Lazily create the progress popup stack + notifier (host = central
        widget). _progress_popup is a ProgressStack: default key "op" is the
        shared install/deploy card; downloads pass their own key so concurrent
        cards stack instead of clobbering each other."""
        if self._notifier is None:
            from gui_qt.notifications import ProgressStack, NotificationManager
            host = self.centralWidget() or self
            self._progress_popup = ProgressStack(host)
            self._notifier = NotificationManager(host)
            self._notifier.on_record = self._notif_history.add

    def _notify(self, text: str, state: str = "info", sticky: bool = False):
        """Show a toast. When *sticky* is True the toast stays on screen until
        its returned handle is dismissed (used for long-running operations like
        the update check) - otherwise it auto-dismisses after a few seconds."""
        self._ensure_feedback()
        self._notif_history.add(text, state)
        return self._notifier.notify(text, state, sticky=sticky)

    def _show_warn_popup(self, title: str, message: str, card_h):
        """ui_hooks.warn → OK-only message card (arrives via _warn_popup)."""
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_message(
            self, title, message,
            card_h=card_h if isinstance(card_h, int) else None)

    def _set_deploy_buttons_enabled(self, enabled: bool):
        for b in (getattr(self, "_deploy_btn", None), getattr(self, "_restore_btn", None),
                  getattr(self, "_play_btn", None)):
            if b is not None:
                b.setEnabled(enabled)

    @property
    def _col_install_running(self) -> bool:
        return getattr(self, "_col_install_flag", False)

    @_col_install_running.setter
    def _col_install_running(self, value):
        self._col_install_flag = bool(value)
        if value:
            self._set_deploy_buttons_enabled(False)
        elif not (getattr(self, "_deploy_running", False)
                  or getattr(self, "_install_running", False)
                  or self._tool_busy):
            self._set_deploy_buttons_enabled(True)

    # ---- external-tool lock (VRAMr / BENDr / ParallaxR) ------------------------
    @property
    def _tool_busy(self) -> bool:
        return bool(getattr(self, "_tool_locks", None))

    def _tool_busy_label(self) -> str:
        """Human name of a tool currently holding the lock (for toasts)."""
        locks = getattr(self, "_tool_locks", None) or {}
        return next(iter(locks.values()), "")

    def _set_tool_lock(self, key: str, label: str, held: bool):
        """Hold/release the deploy-restore lock for a long-running external
        tool. These read the deployed Data folder for many minutes, so a deploy
        or restore underneath them would pull the files out from under the tool
        (and a Play would launch a half-processed game).

        Keyed so concurrent tools nest correctly; releasing an unheld key is a
        no-op, and the buttons only come back once the last holder lets go.
        Called on the GUI thread."""
        locks = self._tool_locks
        if held:
            locks[key] = label
        else:
            locks.pop(key, None)
        if self._tool_busy:
            self._set_deploy_buttons_enabled(False)
        elif not (getattr(self, "_deploy_running", False)
                  or getattr(self, "_install_running", False)
                  or self._col_install_running):
            self._set_deploy_buttons_enabled(True)

    def _col_install_finished(self):
        """The collection install is truly over - including any async cancel
        cleanup or bundle extraction that outlives the pipeline's terminal
        signal. Only now may auto-deploy (and manual deploy/restore) run
        again; also drain a detached-wizard staging job that was held off
        while the install ran (see _run_staged_finish guard)."""
        self._col_install_running = False
        if self._staged_finish_queue:
            QTimer.singleShot(0, self._run_staged_finish)

    def _prompt_mewgenics_deploy(self, game):
        """Ask whether to generate a Steam launch command or repack the gpak,
        then act on the choice (Tk parity: gui/mewgenics_dialogs.py)."""
        from gui_qt.mewgenics_deploy_overlay import (
            MewgenicsDeployChoiceOverlay, MewgenicsLaunchCommandOverlay,
        )
        profile = self._gs.profile

        def _on_choice(result):
            if result is None:
                return
            if result == "steam":
                try:
                    launch_string, modpaths_file = \
                        game.get_modpaths_launch_string(profile)
                except Exception as exc:
                    self._notify(
                        self.tr("Could not build launch command: {0}")
                        .format(exc), "warning")
                    return
                MewgenicsLaunchCommandOverlay(
                    self.window(), launch_string, modpaths_file)
                return
            # result == "repack" -> run the normal deploy pipeline
            self._start_deploy(game, profile)

        MewgenicsDeployChoiceOverlay(self.window(), _on_choice)

    def _start_deploy(self, game, profile, silent: bool = False):
        """Run the deploy worker for *game* / *profile* without re-prompting
        (used by the Mewgenics 'repack' choice, which has already decided)."""
        if self._deploy_running:
            self._deploy_rerun_pending = True
            self._auto_deploy_in_progress = False
            return
        if getattr(self, "_install_running", False) or self._col_install_running:
            self._auto_deploy_in_progress = False
            if not silent:
                self._notify(self.tr("A mod install is in progress - deploy "
                                     "again when it finishes."), "warning")
            return
        # VRAMr/BENDr/ParallaxR read the deployed Data folder for the whole of
        # their run - deploying underneath them corrupts their output.
        if self._tool_busy:
            self._auto_deploy_in_progress = False
            if not silent:
                self._notify(self.tr("{0} is running - deploy again when it "
                                     "finishes.").format(self._tool_busy_label()),
                             "warning")
            return
        # A detached-wizard staging job mutates the shared game (staging root +
        # active profile) - a deploy started now could resolve the wrong profile
        # / race the staging tree. Defer until the staged queue drains; re-run
        # from _on_wizard_finish_done. Coalesce duplicate deferrals per game.
        if getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._auto_deploy_in_progress = False
            if not any(getattr(cb, "_kind", None) == "deploy"
                       for cb in self._pending_after_staged):
                def _later(g=game, p=profile, s=silent):
                    self._start_deploy(g, p, silent=s)
                _later._kind = "deploy"
                self._pending_after_staged.append(_later)
            return
        self._deploy_running = True
        self._op_silent = silent
        self._op_is_restore = False
        self._op_title = "Deploying"
        self._set_deploy_buttons_enabled(False)
        if not silent:
            self._ensure_feedback()
            self._notify(self.tr("Deploying {0}…").format(game.name), "info")
        rf_enabled = True

        # Tell handlers whether a manager-driven launch follows this deploy.
        # Our own launch routes pass the handler's default_launch_args, so a
        # "configure your launcher" warning is only useful when the user is
        # NOT about to press Play here (see BaseGame.deploy_launch_pending).
        try:
            game.deploy_launch_pending = self._post_deploy_action is not None
        except Exception:
            pass

        import threading

        def worker():
            from Utils.deploy_pipeline import run_deploy_pipeline
            ok = False
            warns = []
            try:
                ok = run_deploy_pipeline(
                    game, profile,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p),
                    root_folder_enabled=rf_enabled,
                    confirm_cet=self._make_confirm_cet_cb(game),
                    confirm_windows_fs=self._make_confirm_windows_fs_cb(game),
                    confirm_downgrade=self._make_confirm_downgrade_cb(
                        game, silent=silent),
                    do_backup=True,
                )
            except Exception as exc:
                self._op_log.emit(f"Deploy error: {exc}")
            finally:
                try:
                    warns = list(game.pop_deploy_warnings())
                except Exception:
                    warns = []
                # Scoped to this deploy: wizard/CLI deploys call the pipeline
                # directly and must not inherit the last GUI value.
                try:
                    game.deploy_launch_pending = False
                except Exception:
                    pass
                self._op_done.emit("deploy", bool(ok), warns)

        threading.Thread(target=worker, daemon=True).start()

    def _on_deploy(self, silent: bool = False):
        """Deploy the current game/profile. *silent* (used by auto-deploy)
        suppresses the progress popup + interim toast so rapid mod toggles
        don't flash the UI; log lines + the final success/warning toasts still
        surface (Tk parity: top_bar._run_deploy silent=)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._auto_deploy_in_progress = False
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not hasattr(game, "deploy"):
            self._auto_deploy_in_progress = False
            self._notify(self.tr("'{0}' does not support deployment.").format(game.name), "warning")
            return

        # A game exposing get_modpaths_launch_string (Mewgenics) offers a Steam
        # launch command as an alternative to repacking its archive (Tk parity:
        # top_bar._on_deploy). Only prompt for an explicit user deploy -
        # auto-deploy (silent) falls straight through to the repack path.
        if not silent and hasattr(game, "get_modpaths_launch_string"):
            self._prompt_mewgenics_deploy(game)
            return

        self._start_deploy(game, self._gs.profile, silent=silent)

    def _save_window_state(self):
        """Persist the window geometry (pos/size/maximized) and the modlist ║
        plugins splitter position to amethyst.ini [window]."""
        try:
            from Utils.ui_config import save_qt_window_state
            geo = bytes(self.saveGeometry().toBase64()).decode("ascii")
            split = getattr(self, "_body_split", None)
            save_qt_window_state(geo, list(split.sizes()) if split else None)
        except Exception as exc:
            print(f"[gui_qt] window-state save error: {exc}", flush=True)

    def _schedule_window_state_save(self):
        """(Re)start the debounce timer - one ini write ~1s after the last
        move/resize/splitter drag."""
        t = getattr(self, "_win_state_timer", None)
        if t is not None:
            t.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_window_state_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_window_state_save()

    def closeEvent(self, event):
        """On close, optionally restore every deployed game to vanilla (the
        'Restore on close' setting). Synchronous (the app is exiting) - mirrors
        the Tk gui.py shutdown path."""
        self._save_window_state()
        try:
            from Nexus.nxm_handler import NxmIPC
            NxmIPC.shutdown()      # release the IPC socket(s)
        except Exception:
            pass
        try:
            from Utils.ui_config import load_restore_on_close
            if load_restore_on_close():
                self._restore_all_on_close()
        except Exception as exc:
            print(f"[gui_qt] restore-on-close error: {exc}", flush=True)
        super().closeEvent(event)

    def _restore_all_on_close(self):
        """Restore every configured game that has an active deployment back to
        vanilla. Ported from gui.py `_restore_all_on_close`."""
        from gui_qt.game_state import _GAMES
        from Utils.deploy import restore_root_folder_for_game

        games = [g for g in _GAMES.values()
                 if g.is_configured() and g.get_deploy_active()
                 and getattr(g, "restore_on_close_eligible", True)]
        if not games:
            return
        log_fn = lambda m: print(f"[restore-on-close] {m}", flush=True)
        log_fn(f"restoring {len(games)} game(s)...")
        for game in games:
            try:
                game_root = game.get_game_path()
                last_deployed = game.get_last_deployed_profile()
                original_profile_dir = getattr(game, "_active_profile_dir", None)
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed)
                    # Reload so the last-deployed profile's path overrides drive
                    # the restore (it may target a different game folder).
                    game.load_paths()
                    game_root = game.get_game_path()
                try:
                    if hasattr(game, "restore"):
                        game.restore(log_fn=log_fn)
                    root_folder_dir = game.get_effective_root_folder_path()
                    if root_folder_dir.is_dir() and game_root:
                        restore_root_folder_for_game(
                            game, root_folder_dir=root_folder_dir,
                            game_root=game_root, log_fn=log_fn,
                        )
                    game.clear_deploy_active()
                finally:
                    if original_profile_dir is not None:
                        game.set_active_profile_dir(original_profile_dir)
                        game.load_paths()
            except Exception as e:
                log_fn(f"error for {game.name}: {e}")

    def _on_restore(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._deploy_running:
            self._notify(self.tr("A deploy is in progress - try again shortly."), "warning")
            return
        if getattr(self, "_install_running", False) or self._col_install_running:
            self._notify(self.tr("A mod install is in progress - try again "
                                 "when it finishes."), "warning")
            return
        # Restoring would strip the Data folder the tool is still reading.
        if self._tool_busy:
            self._notify(self.tr("{0} is running - restore again when it "
                                 "finishes.").format(self._tool_busy_label()),
                         "warning")
            return
        # Defer behind a detached-wizard staging job (shared game mutation) - it
        # re-runs once the staged queue drains (_on_wizard_finish_done). Coalesce
        # duplicate restore requests.
        if getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            if not any(getattr(cb, "_kind", None) == "restore"
                       for cb in self._pending_after_staged):
                def _later():
                    self._on_restore()
                _later._kind = "restore"
                self._pending_after_staged.append(_later)
            self._notify(self.tr("Restore queued - it will run after the "
                                 "current install finishes."), "info")
            return
        self._deploy_running = True
        self._op_is_restore = True
        self._op_title = "Restoring"
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        self._notify(self.tr("Restoring {0}…").format(game.name), "info")
        profile = self._gs.profile

        import threading
        from Utils.deploy import restore_root_folder_for_game

        def worker():
            ok = True
            try:
                from Utils.deploy_pipeline import check_paths_mounted
                err = check_paths_mounted(game)
                if err:
                    self._op_log.emit(f"Restore aborted: {err}")
                    ok = False
                else:
                    last = game.get_last_deployed_profile()
                    if last:
                        game.set_active_profile_dir(
                            game.get_profile_root() / "profiles" / last)
                        game.load_paths()
                    game_root = game.get_game_path()
                    if hasattr(game, "restore"):
                        game.restore(
                            log_fn=lambda m: self._op_log.emit(str(m)),
                            progress_fn=lambda d, t, p=None: self._op_progress.emit(d, t, p))
                    rf = game.get_effective_root_folder_path()
                    if rf.is_dir() and game_root:
                        restore_root_folder_for_game(
                            game, root_folder_dir=rf, game_root=game_root,
                            log_fn=lambda m: self._op_log.emit(str(m)),
                        )
            except Exception as exc:
                ok = False
                self._op_log.emit(f"Restore error: {exc}")
            finally:
                # Always restore the active profile dir to the selected profile.
                try:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / profile)
                    game.load_paths()
                    if ok and hasattr(game, "clear_deploy_active"):
                        game.clear_deploy_active()
                except Exception:
                    pass
                self._op_done.emit("restore", ok, [])

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_op_clear(self, delay_ms: int = 1200):
        """Hide the shared "op" progress card after *delay_ms*. Cancellable -
        if another operation emits progress before it fires, the pending clear
        is dropped so it can't hide the NEW operation's card (e.g. a queued
        install batch that starts right after the previous one finished)."""
        t = getattr(self, "_op_clear_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(
                lambda: self._progress_popup.clear()
                if self._progress_popup is not None else None)
            self._op_clear_timer = t
        t.start(delay_ms)

    def _on_op_progress(self, done: int, total: int, phase):
        if getattr(self, "_op_silent", False):
            return   # silent auto-deploy: no progress popup
        if self._progress_popup is not None:
            # A new/ongoing op is reporting - a clear scheduled by the previous
            # op's completion must not hide this op's card.
            t = getattr(self, "_op_clear_timer", None)
            if t is not None:
                t.stop()
            title = getattr(self, "_op_title", "Working")
            self._progress_popup.set_progress(done, total, phase, title=title)

    def _on_op_done(self, kind: str, success: bool, warnings):
        self._deploy_running = False
        self._op_silent = False
        if not (getattr(self, "_install_running", False)
                or self._col_install_running or self._tool_busy):
            self._set_deploy_buttons_enabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        # Any deploy (manual OR auto) is followed by a modlist reload → conflict
        # rebuild → _on_conflicts_ready. With auto_deploy on, that rebuild would
        # otherwise start a *fresh* auto-deploy every time (an endless loop, and
        # an extra deploy even after a manual Deploy press). Arm the guard so the
        # rebuild caused by THIS deploy's reload is swallowed. (Tk parity: the
        # gui.py entry path + top_bar._on_deploy_finished both set this flag.)
        game = self._gs.game
        if (kind == "deploy" and game is not None and game.is_configured()
                and getattr(game, "auto_deploy", False)):
            self._auto_deploy_in_progress = True
        # Refresh the modlist/conflicts + deployed-profile highlight after the op.
        self._reload_modlist()
        self._update_deployed_profile_highlight()
        self._refresh_framework_banner()
        # Deploy/restore adds/removes framework launchers (script extenders)
        # in the game root - reflect that in the play-bar dropdown.
        self._refresh_play_selector()
        game_name = self._gs.game.name if self._gs.game else self.tr("Game")
        if success:
            msg = (self.tr("{0} Deployed") if kind == "deploy"
                   else self.tr("{0} Restored")).format(game_name)
            self._notify(msg, "success")
        else:
            self._notify(self.tr("Deploy failed - see log.") if kind == "deploy"
                         else self.tr("Restore failed - see log."), "error")
        for w in (warnings or []):
            self._notify(w, "warning")
        # Drain any install batch that was queued behind this deploy/restore
        # (see _install_paths). Deferred so it starts after this handler +
        # the coalesced re-deploy check below settle. Skipped if a coalesced
        # re-deploy is about to run - the install stays queued until it too
        # finishes (the drain guards on _deploy_running/_install_running).
        if self._pending_install_batches and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have queued behind this deploy -
        # drain it once the deploy is fully settled (skipped if a coalesced
        # re-deploy is about to run; _run_staged_finish re-guards on
        # _deploy_running so it stays queued until that finishes too).
        if self._staged_finish_queue and not self._deploy_rerun_pending:
            QTimer.singleShot(0, self._run_staged_finish)
        # Coalesced re-deploy if mod state changed mid-deploy.
        if kind == "deploy" and self._deploy_rerun_pending:
            self._deploy_rerun_pending = False
            # A coalesced re-deploy already reflects a decided deploy - run it
            # directly (no fresh prompt/toast; matters for the Mewgenics choice).
            game = self._gs.game
            if game is not None and game.is_configured():
                QTimer.singleShot(
                    0, lambda g=game, p=self._gs.profile: self._start_deploy(g, p))
            else:
                # The re-deploy won't run (game gone/unconfigured) - the drains
                # skipped above on _deploy_rerun_pending would otherwise strand
                # the queues. Release them now.
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                if self._staged_finish_queue:
                    QTimer.singleShot(0, self._run_staged_finish)
        elif kind == "deploy":
            # Play-button deploy-before-launch: fire the pending launch only
            # after the FINAL (non-coalesced) deploy, and only on success.
            action, self._post_deploy_action = self._post_deploy_action, None
            if action is not None and success:
                action()
            elif action is not None:
                self._append_log(
                    "Play: deploy failed - the pending launch was cancelled.")
                # Otherwise the sticky "Deploying, then launching…" toast has
                # nothing left to resolve it and sits there for good.
                self._end_play_toast(
                    self.tr("Deploy failed - launch cancelled"), "error")
            # Wizard deploy steps: one-shot completion hooks (get the outcome
            # either way so the wizard can show failure and re-enable Deploy).
            hooks, self._deploy_done_hooks = self._deploy_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: deploy hook error: {exc}")
        elif kind == "restore":
            # Wizard restore steps (4GB patch / FO3 downgrade restore-first).
            hooks, self._restore_done_hooks = self._restore_done_hooks, []
            for h in hooks:
                try:
                    h(success)
                except Exception as exc:
                    self._append_log(f"Wizards: restore hook error: {exc}")

    # ----------------------------------------------------------------- install
    def _on_install_mod(self):
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if self._install_running:
            self._notify(self.tr("An install is already in progress."), "warning")
            return
        # The picker callback fires on the portal WORKER thread; QTimer.singleShot
        # from there never fires (no event loop on that thread), so marshal to the
        # GUI thread with a Signal (auto-queued to the receiver's thread).
        from Utils.portal_filechooser import pick_files
        pick_files("Select mod archive(s)",
                   lambda ps: self._install_files_picked.emit(ps))

    def _on_install_files_picked(self, paths):
        """GUI thread: start the install once archives were chosen in the portal."""
        if paths:
            self._install_paths([str(p) for p in paths], clear_archives=False)

    # ---- Proton tools ------------------------------------------------------
    def _proton_game(self):
        """Return the active configured game, or None (after notifying)."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return None
        return game

    def _open_folder_path(self, path, descr):
        """Open *path* in the file manager, or log why it can't be opened.
        Ported from gui/dialogs ProtonTools._open_folder."""
        from pathlib import Path
        if path is None:
            self._notify(self.tr("{0} is not configured for this game.").format(descr), "warning")
            return
        path = Path(path)
        if not path.is_dir():
            self._notify(self.tr("{0} not found ({1}).").format(descr, path), "warning")
            return
        from Utils.xdg import xdg_open
        try:
            xdg_open(str(path))
        except Exception as e:
            self._append_log(f"Open {descr} error: {e}")

    def _profile_actions(self):
        """Build the pinned action entries for the profile selector. An "Open"
        submenu is prepended only when the current game exposes profile-specific
        INI files and/or Saves *and* the matching option is toggled on."""
        open_items = []
        game = getattr(getattr(self, "_gs", None), "game", None)
        if game is not None and getattr(game, "is_configured", lambda: False)():
            if getattr(game, "profile_ini_files", False) \
                    and hasattr(game, "_profile_ini_dir"):
                open_items.append(
                    (self.tr("Profile INI files folder"),
                     lambda: self._open_profile_dir("ini")))
            if getattr(game, "profile_saves", False) \
                    and hasattr(game, "_profile_saves_dir"):
                open_items.append(
                    (self.tr("Profile saves folder"),
                     lambda: self._open_profile_dir("saves")))
        actions = []
        # Add new profile always comes first.
        add_opts = {"separator_after": True}
        # Remove current profile - hidden for locked (and default) profiles, so
        # the "Add" entry keeps the trailing separator when Remove is absent.
        can_remove = (self._gs.profile is not None
                      and not self._current_profile_locked())
        if can_remove:
            add_opts = {}
        actions.append(
            (self.tr("Add new profile…"), lambda: self._on_profile_action("add"),
             add_opts))
        if can_remove:
            actions.append(
                (self.tr("Remove current profile…"),
                 lambda: self._on_profile_action("remove"),
                 {"separator_after": True}))
        if open_items:
            actions.append((self.tr("Open"), open_items))
        quick = self._quick_configure_submenu(game)
        if quick:
            actions.append((self.tr("Quick configure"), quick))
        # Profile settings + groups, then the export/import group.
        actions.append(
            (self.tr("Profile settings…"),
             lambda: self._on_profile_action("settings"),
             {} if getattr(game, "profile_groups_supported", True)
             else {"separator_after": True}))
        if getattr(game, "profile_groups_supported", True):
            actions.append(
                (self.tr("Profile groups…"),
                 lambda: self._on_profile_action("groups"),
                 {"separator_after": True}))
        actions.extend([
            (self.tr("Export profile…"), lambda: self._on_profile_action("export")),
            (self.tr("Import profile…"), lambda: self._on_profile_action("import")),
            (self.tr("Export code…"), lambda: self._on_profile_action("export_code")),
            (self.tr("Import code…"), lambda: self._on_profile_action("import_code")),
        ])
        return actions

    def _quick_configure_submenu(self, game):
        """Build the profile dropdown's 'Quick configure' submenu: the active
        profile's Configure-view options as inline checkable/radio entries that
        flip live when clicked (like saving that one field would). Returns a list
        of SelectorButton action entries, or [] when the game has no options."""
        from Utils.quick_configure import build_quick_configure_options
        try:
            options = build_quick_configure_options(game)
        except Exception as e:
            self._append_log(f"[quick-configure] build failed: {e}")
            return []
        entries = []
        for opt in options:
            if opt["kind"] == "toggle":
                entries.append((
                    self.tr(opt["label"]),
                    lambda checked, act, o=opt: self._apply_quick_configure(
                        o, checked, act),
                    {"checkable": True, "checked": opt["value"], "keep_open": True}))
            else:  # choice → a nested submenu of radio entries
                gid = f"qc_{opt['key']}"
                sub = []
                for val, clabel in opt["choices"]:
                    sub.append((
                        self.tr(clabel),
                        lambda checked, act, o=opt, v=val: (
                            self._apply_quick_configure(o, v, act)
                            if checked else None),
                        {"checkable": True, "checked": val == opt["value"],
                         "group": gid, "keep_open": True, "value": val}))
                entries.append((self.tr(opt["label"]), sub))
        return entries

    def _apply_quick_configure(self, opt, value, action=None):
        """Apply one quick-configure option live to the active profile, then run
        the same post-save refresh the Configure view triggers. The menu stays
        open, so on failure we revert *action*'s check state in place rather than
        rebuilding the (still-shown) menu."""
        game = getattr(self._gs, "game", None)
        if game is None:
            return
        # Deploy method can't change while mods are deployed (would strand them).
        if opt["key"] == "deploy_mode":
            from Utils.quick_configure import deploy_mode_change_blocked
            if deploy_mode_change_blocked(game, value):
                self._notify(
                    self.tr("Cannot change the deploy method while mods are "
                            "deployed. Restore first."), "warning")
                self._revert_quick_configure(opt, action)
                return
        try:
            opt["apply"](value)
        except Exception as e:
            self._append_log(f"[quick-configure] apply {opt['key']} failed: {e}")
            self._notify(self.tr("Could not change {0}.").format(
                self.tr(opt["label"])), "warning")
            self._revert_quick_configure(opt, action)
            return
        self._append_log(f"[quick-configure] {opt['key']} = {value}")
        # Track the new value so a later revert (in this still-open menu) knows
        # what the checkbox/radio should snap back to.
        opt["value"] = value
        if opt.get("needs_reload"):
            # A path-changing option (e.g. game patch version) - reload the
            # game's paths, then refresh the lists that depend on them (mirrors
            # a same-game Configure save, which _on_game_changed no-ops).
            try:
                game.load_paths()
            except Exception:
                pass
            self._reload_modlist()
            self._reload_plugins()
        # Keep the Configure tab (if open) in sync. The profile menu itself is
        # NOT rebuilt here - it's still open; its action state already reflects
        # the click, and the next open rebuilds fresh from _profile_actions()
        # (via the selector's aboutToShow hook), so a toggle that adds/removes
        # the Open submenu takes effect the next time the dropdown is opened.
        v = getattr(self, "_configure_game_view", None)
        if v is not None and self._tabs.has_key("configure_game"):
            try:
                v.refresh_for_profile()
            except Exception:
                pass
        self._notify(self.tr("Setting saved."), "info")

    def _revert_quick_configure(self, opt, action):
        """Snap a quick-configure action back to the option's real (unchanged)
        value after a blocked/failed change, without closing or rebuilding the
        open menu."""
        if action is None:
            return
        try:
            if opt["kind"] == "toggle":
                action.setChecked(bool(opt["value"]))
                return
            # Radio: re-check the sibling whose stashed value matches the real
            # (pre-change) value; the shared QActionGroup unchecks the rest.
            group = action.actionGroup()
            peers = group.actions() if group is not None else [action]
            for peer in peers:
                if peer.data() == opt["value"]:
                    peer.setChecked(True)
                    return
            action.setChecked(False)
        except Exception:
            pass

    def _refresh_profile_actions(self):
        """Rebuild the profile selector's pinned actions so the Open submenu
        appears/disappears with the current game's profile-specific settings."""
        sel = getattr(self, "_profile_selector", None)
        if sel is not None:
            sel.set_actions(self._profile_actions())

    def _open_profile_dir(self, which: str):
        """Open the active profile's INI files or Saves folder."""
        from pathlib import Path
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # Keep the game's active profile dir in sync with the dropdown so the
        # folder resolves against the profile actually shown (a background worker
        # may have moved it).
        self._gs.reassert_active_profile()
        profile = self._gs.profile
        if which == "ini":
            getter = getattr(game, "_profile_ini_dir", None)
            descr = "profile INI files folder"
        elif which == "saves":
            getter = getattr(game, "_profile_saves_dir", None)
            descr = "profile saves folder"
        else:
            return
        path = getter(profile) if callable(getter) else None
        # The folder is created on demand when the setting is toggled on, but a
        # profile added afterwards may not have it yet - create it so the open
        # never fails on a valid, enabled setting.
        if path is not None:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        self._open_folder_path(path, descr)

    def _open_game_dir(self, which: str):
        """Open one of the game's on-disk folders (game selector ▸ Open ▸ …).
        Mirrors gui/dialogs ProtonTools folder openers."""
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # A background worker may have left the game's _active_profile_dir out of
        # sync with the selected profile - re-assert it so staging/profile folder
        # opens resolve against the profile the dropdown actually shows.
        self._gs.reassert_active_profile()
        if which == "game":
            self._open_folder_path(game.get_game_path(), "game folder")
        elif which == "prefix":
            self._open_folder_path(game.get_prefix_path(), "prefix folder")
        elif which == "mygames":
            getter = getattr(game, "_mygames_path", None)
            path = getter() if callable(getter) else None
            if path is None:
                prefix = game.get_prefix_path()
                if prefix is not None:
                    path = prefix / "drive_c/users/steamuser/Documents/My Games"
            self._open_folder_path(path, "My Games folder")
        elif which == "appdata":
            prefix = game.get_prefix_path()
            if prefix is None:
                self._open_folder_path(None, "AppData folder")
                return
            sub = getattr(game, "_APPDATA_SUBPATH", None)
            if sub is not None:
                self._open_folder_path(prefix / sub, "AppData folder")
            else:
                self._open_folder_path(
                    prefix / "drive_c/users/steamuser/AppData/Local",
                    "AppData folder")
        elif which == "staging":
            getter = getattr(game, "get_effective_mod_staging_path", None) \
                or getattr(game, "get_mod_staging_path", None)
            path = getter() if callable(getter) else None
            self._open_folder_path(path, "staging folder")
        elif which == "profile":
            path = getattr(game, "_active_profile_dir", None)
            if path is None:
                try:
                    path = game.get_profile_root() / "profiles"
                except Exception:
                    path = None
            self._open_folder_path(path, "profile folder")
        elif which == "config":
            try:
                from Utils.config_paths import get_config_dir
                path = get_config_dir()
            except Exception:
                path = None
            self._open_folder_path(path, ".config folder")

    def _proton_winecfg(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.proton_tools import launch_wine_tool
        launch_wine_tool(game, "winecfg", log_fn=self._append_log)

    def _proton_regedit(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.proton_tools import launch_wine_tool
        launch_wine_tool(game, "regedit", log_fn=self._append_log)

    def _proton_dll_overrides(self):
        """Open the Wine DLL overrides manager scoped over the MODLIST panel
        (like the Settings gear): a per-DLL load-order picker for this game's
        prefix. The plugins panel + rest of the UI stay live."""
        game = self._proton_game()
        if game is None:
            return
        if self._tabs.has_key("dll_overrides"):
            self._tabs.focus_key("dll_overrides")
            return
        from gui_qt.dll_overrides_view import DllOverridesView
        view = DllOverridesView(self, game, log_fn=self._append_log)
        self._dll_overrides_view = view
        self._tabs.open_scoped_tab(
            view, self.tr("Wine DLL overrides"), self._modlist_panel_stack,
            key="dll_overrides")

    def _proton_health_check(self):
        """Report what this game's prefix actually contains (deps, registry).

        Which rows appear is driven by the handler's auto_install_deps and
        synthesis_registry_name, so no game is special-cased here."""
        game = self._proton_game()
        if game is None:
            return
        from gui_qt.prefix_health_overlay import PrefixHealthOverlay
        PrefixHealthOverlay.show_over(self, game, window=self)

    def _proton_winetricks(self):
        game = self._proton_game()
        if game is None:
            return
        import threading
        from Utils.proton_tools import launch_winetricks
        self._notify(self.tr("Launching winetricks…"), "info")
        threading.Thread(
            target=lambda: launch_winetricks(
                game, log_fn=lambda m: self._op_log.emit(str(m))),
            daemon=True).start()

    def _proton_run_exe(self):
        game = self._proton_game()
        if game is None:
            return
        from Utils.portal_filechooser import pick_exe_file
        from Utils.proton_tools import launch_exe_in_prefix

        def _picked(exe_path):
            if exe_path is None:
                return
            launch_exe_in_prefix(game, exe_path, log_fn=self._append_log)

        pick_exe_file(self.tr("Select EXE to run in this prefix"), _picked)

    def _proton_install_vcredist(self):
        from Utils.proton_tools import install_vcredist
        self._run_proton_installer(
            self.tr("Installing VC++ Redistributable"),
            lambda plog: install_vcredist(self._gs.game, log_fn=plog))

    def _proton_install_d3dcompiler(self):
        from Utils.proton_tools import install_d3dcompiler_47
        self._run_proton_installer(
            self.tr("Installing d3dcompiler_47"),
            lambda plog: install_d3dcompiler_47(self._gs.game, log_fn=plog))

    def _proton_install_xact(self):
        from Utils.proton_tools import install_xact
        self._run_proton_installer(
            self.tr("Installing XACT audio (XAudio2)"),
            lambda plog: install_xact(self._gs.game, log_fn=plog))

    def _proton_install_lavfilters(self):
        from Utils.proton_tools import install_lavfilters
        self._run_proton_installer(
            self.tr("Installing LAV Filters"),
            lambda plog: install_lavfilters(self._gs.game, log_fn=plog))

    def _sync_proton_menu(self):
        """Show/hide the game-specific entries in the Proton menu.

        LAV Filters only fixes games that stream audio through DirectShow
        (Fallout 3 / New Vegas), so it is hidden elsewhere rather than offered
        as a no-op. The handler's auto_install_deps is the single source of
        truth - no game names are hardcoded here."""
        act = getattr(self, "_menu_actions", {}).get("lavfilters")
        if act is None:
            return
        game = self._gs.game
        deps = list(getattr(game, "auto_install_deps", []) or []) if game else []
        act.setVisible("lavfilters" in deps)

    def _sync_thunderstore_button(self):
        """Show each store's header button only for games that use that store.

        A game can be on Nexus, on Thunderstore, on both (Subnautica, Valheim)
        or - now that Risk of Rain 2 is supported - on Thunderstore alone. A
        button for a store the game isn't on is a dead end, so it is hidden
        rather than left to warn on click.

        The wanted state is tracked on the button (``_store_wanted``) instead of
        read back from ``isVisible()`` - during ``_left_header()`` the widget is
        not realised yet, so isVisible() is False for every button and an
        isVisible()-based early-out would skip the initial show."""
        game = self._gs.game
        wanted = {
            "_thunderstore_btn": bool(
                (getattr(game, "thunderstore_community", "") or "").strip()
                if game is not None else ""),
            "_nexus_btn": bool(
                (getattr(game, "nexus_game_domain", "") or "").strip()
                if game is not None else ""),
        }
        changed = False
        for attr, want in wanted.items():
            b = getattr(self, attr, None)
            if b is None or getattr(b, "_store_wanted", None) == want:
                continue
            b._store_wanted = want
            b.setVisible(want)
            changed = True
        # The bar just got wider or narrower - re-run the staged compaction
        # once for the whole batch (btn_room only counts visible buttons, so
        # the stage thresholds shift).
        if changed and getattr(self, "_action_btn_widths", False):
            self._sync_header_compact()

    def _proton_install_dotnet(self, version: str):
        from Utils.proton_tools import install_dotnet
        self._run_proton_installer(
            self.tr("Installing .NET {0}").format(version),
            lambda plog: install_dotnet(self._gs.game, version, log_fn=plog))

    def _run_proton_installer(self, title: str, worker_fn, on_done=None) -> bool:
        """Run a blocking Proton installer (*worker_fn(log_fn) -> bool*) on a
        worker thread, showing the indeterminate progress popup + a toast on
        completion. Serialized: refuses a second installer while one runs.

        Returns True when the installer was started, False when it was refused
        - callers that drive their own UI (the prefix health overlay) need to
        know the difference. *on_done(success)* is invoked from the completion
        slot, i.e. on the GUI thread, so it may touch widgets."""
        game = self._proton_game()
        if game is None:
            return False
        if self._proton_busy:
            self._notify(self.tr("A Proton installer is already running."), "warning")
            return False
        self._proton_done_cb = on_done
        self._proton_busy = True
        self._op_title = title
        self._ensure_feedback()
        self._notify(self.tr("{0}…").format(title), "info")
        self._op_progress.emit(0, 0, title)   # indeterminate (busy) bar

        import threading

        def _run():
            ok = False
            try:
                ok = bool(worker_fn(lambda m: self._op_log.emit(f"Proton Tools: {m}")))
            except Exception as exc:
                self._op_log.emit(f"Proton Tools error: {exc}")
            self._proton_done.emit(title, ok)

        threading.Thread(target=_run, daemon=True).start()
        return True

    def _on_proton_done(self, title: str, success: bool):
        self._proton_busy = False
        if self._progress_popup is not None:
            self._progress_popup.clear()
        if success:
            self._notify(self.tr("{0} - done.").format(title), "success")
        else:
            self._notify(self.tr("{0} - failed (see log).").format(title), "error")
        # Pop before calling: a callback that starts another installer must not
        # inherit this one's completion hook.
        cb, self._proton_done_cb = self._proton_done_cb, None
        if cb is not None:
            try:
                cb(success)
            except Exception as exc:
                self._append_log(f"Proton Tools: completion callback failed: {exc}")

    # ---- Wizard tools ------------------------------------------------------
    def _rebuild_wizard_menu(self):
        """(Re)populate the Wizard header menu for the current game. Runs on
        every aboutToShow: several games' wizard_tools do live filesystem
        checks (e.g. BodySlide only appears once its exe exists in staging),
        and this also handles game switches for free. Tools without a Qt view
        registered yet appear greyed out; EXCLUDED ones are dropped."""
        menu = getattr(self._wizard_btn, "_menu", None)
        if menu is None:
            return
        menu.clear()
        menu.setToolTipsVisible(True)
        game = self._gs.game
        if game is None:
            menu.addAction(self.tr("No game selected")).setEnabled(False)
            self._add_prefix_manager_action(menu)
            return
        from Utils.plugin_loader import get_all_wizard_tools
        from Utils.wizard_catalog import group_by_category
        from wizards_qt import EXCLUDED, get_spec
        try:
            tools = [t for t in get_all_wizard_tools(game)
                     if t.dialog_class_path not in EXCLUDED]
        except Exception as exc:
            self._append_log(f"Wizards: failed to list tools: {exc}")
            tools = []
        if not tools:
            menu.addAction(self.tr("No wizard tools for this game")).setEnabled(False)
            self._add_prefix_manager_action(menu, tools)
            return
        # Favourites submenu at the top - quick launch for pinned tools.
        from Utils.ui_config import load_favourite_wizards
        fav_ids = load_favourite_wizards()
        fav_tools = [t for t in tools if t.id in fav_ids]
        if fav_tools:
            fav_menu = menu.addMenu(self.tr("★ Favourites"))
            fav_menu.setToolTipsVisible(True)
            for tool in fav_tools:
                act = fav_menu.addAction(tool.label)
                if tool.description:
                    act.setToolTip(tool.description)
                if get_spec(tool.dialog_class_path) is None:
                    act.setEnabled(False)   # not ported to Qt yet
                else:
                    act.triggered.connect(
                        lambda _=False, t=tool: self._open_wizard_tool(t))
            menu.addSeparator()
        groups = group_by_category(tools)
        for cat, cat_tools in groups:
            target = menu if len(groups) == 1 else menu.addMenu(cat)
            target.setToolTipsVisible(True)
            for tool in cat_tools:
                act = target.addAction(tool.label)
                if tool.description:
                    act.setToolTip(tool.description)
                if get_spec(tool.dialog_class_path) is None:
                    act.setEnabled(False)   # not ported to Qt yet
                else:
                    act.triggered.connect(
                        lambda _=False, t=tool: self._open_wizard_tool(t))
        self._add_prefix_manager_action(menu, tools)

    def _add_prefix_manager_action(self, menu, tools=None):
        """Trailing 'Add Favourites…' + 'Manage Prefixes…' entries. The prefix
        manager is always available (it lists every game's tool prefixes, like
        the Tk wizard picker's button); 'Add Favourites…' is only shown when the
        current game has tools to favourite."""
        menu.addSeparator()
        if tools:
            fav = menu.addAction(self.tr("Add Favourites…"))
            fav.setToolTip(self.tr("Choose which wizard tools appear at the top of "
                           "this menu for quick access."))
            fav.triggered.connect(
                lambda _=False, t=list(tools): self._open_favourite_wizards(t))
        act = menu.addAction(self.tr("Manage Prefixes…"))
        act.setToolTip(self.tr("Browse every wizard-tool Wine prefix and delete them "
                       "to reclaim disk space."))
        act.triggered.connect(lambda _=False: self._open_prefix_manager())

    def _open_favourite_wizards(self, tools):
        """Open the borderless favourites picker for the given wizard *tools*.
        Saving replaces the global favourites set; the Wizard menu rebuilds its
        Favourites submenu on next open."""
        from gui_qt.favourite_wizards_overlay import FavouriteWizardsOverlay
        from Utils.ui_config import load_favourite_wizards, save_favourite_wizards
        # Only offer tools that are actually openable (ported to Qt).
        from wizards_qt import get_spec
        items = [(t.label, t.id) for t in tools
                 if get_spec(t.dialog_class_path) is not None]
        if not items:
            return

        def _done(chosen):
            if chosen is None:
                return
            try:
                save_favourite_wizards(chosen)
            except Exception as exc:
                self._append_log(f"Wizards: failed to save favourites: {exc}")

        FavouriteWizardsOverlay.show_over(
            self, items, load_favourite_wizards(), _done)

    def _open_prefix_manager(self):
        """Open the prefix manager as a plugins-panel-scoped tab."""
        if self._tabs.has_key("prefix_manager"):
            self._tabs.focus_key("prefix_manager")
            return
        from gui_qt.prefix_manager_view import PrefixManagerView
        game = self._gs.game
        view = PrefixManagerView(
            active_game_name=(game.name if game is not None else ""),
            on_close=lambda: self._close_wizard_tab("prefix_manager"),
            log_fn=self._append_log)
        self._tabs.open_scoped_tab(
            view, self.tr("Manage Prefixes"), self._plugins_panel_stack,
            key="prefix_manager")

    def _open_wizard_tool(self, tool, *, extra_kwargs=None, key_suffix="",
                          label=None):
        """Open a ported wizard tool as a panel-scoped tab (plugins panel for
        most tools; modlist panel for the wide ones that were full-width
        overlays in Tk), or as a full-UI tab when the spec says panel="full".
        Re-opening an already-open tool refocuses its tab.

        *extra_kwargs* / *key_suffix* / *label* open a PARAMETERISED instance of
        a tool (right-click ▸ Open in NIF Viewer scopes it to one mod), which
        needs its own tab key so it can't refocus a differently-scoped tab."""
        game = self._gs.game
        if game is None:
            return
        from wizards_qt import get_spec
        spec = get_spec(tool.dialog_class_path)
        if spec is None:
            return
        key = f"wizard:{tool.id}{key_suffix}"
        if self._tabs.has_key(key):
            self._tabs.focus_key(key)
            return
        # _full_width_overlay is a Tk-only hint (the registry's `panel` field
        # replaces it); the rest of extra is forwarded to the view.
        extra = {k: v for k, v in (tool.extra or {}).items()
                 if k != "_full_width_overlay"}
        extra.update(extra_kwargs or {})
        full = spec.panel == "full"
        stack = (self._modlist_panel_stack if spec.panel == "modlist"
                 else self._plugins_panel_stack)
        from wizards_qt import QtWizardContext
        ctx = QtWizardContext(
            profile_name=self._gs.profile or "default",
            run_deploy=self._wizard_run_deploy,
            run_restore=self._wizard_run_restore,
            refresh_modlist=self._on_refresh_modlist,
            refresh_plugins=self._wizard_refresh_plugins,
            import_manifest=lambda manifest, stem, bundle_zip:
                self._open_manifest_import(manifest, stem, bundle_zip=bundle_zip),
            current_profile=lambda: self._gs.profile or "default",
            nexus_api=self._ensure_nexus_api,
            open_log_tab=self._open_log_tab,
            set_tool_lock=self._set_tool_lock,
        )
        try:
            view = spec.view_factory(
                game, log_fn=self._append_log,
                on_close=lambda k=key: self._close_wizard_tab(k),
                ctx=ctx, **extra)
        except Exception as exc:
            self._append_log(f"Wizards: failed to open {tool.label}: {exc}")
            return
        title = label or tool.label
        if full:
            self._tabs.open_tab(view, title, key=key)
        else:
            self._tabs.open_scoped_tab(view, title, stack, key=key)

    def _open_nif_viewer_for_mod(self, mod_name: str):
        """Right-click ▸ Open in NIF Viewer: the mesh browser scoped to one
        mod's meshes. One tab per mod, but still under the wizard: key prefix
        so a game switch closes it with the rest."""
        game = self._gs.game
        if game is None or not mod_name:
            return
        from Utils.plugin_loader import get_builtin_wizard_tools_for_game
        tool = next((t for t in get_builtin_wizard_tools_for_game(game.game_id)
                     if t.id == "nif_viewer"), None)
        if tool is None:
            return
        # Tab titles sit in a shared bar - a 60-character mod name would push
        # every other tab off it, so the label (not the key) is capped.
        short = mod_name if len(mod_name) <= 28 else mod_name[:27] + "…"
        self._open_wizard_tool(
            tool, extra_kwargs={"mod": mod_name},
            key_suffix=f":mod:{mod_name}",
            label=self.tr("NIF Viewer - {0}").format(short))

    def _wizard_run_deploy(self, on_done) -> bool:
        """Start a deploy for a wizard step through the normal deploy path
        (mutex/coalesce + progress popup). *on_done(ok)* fires on the UI
        thread after the final deploy completes. Returns False when a deploy
        can't be started (unconfigured game or an install in progress)."""
        game = self._gs.game
        if game is None or not game.is_configured() or not hasattr(game, "deploy"):
            return False
        if getattr(self, "_install_running", False) or self._col_install_running:
            return False
        # _start_deploy refuses while a texture tool holds the lock - bail here
        # so the on_done hook isn't left dangling for the next deploy to fire.
        if self._tool_busy:
            return False
        self._deploy_done_hooks.append(on_done)
        self._on_deploy()
        return True

    def _wizard_run_restore(self, on_done) -> bool:
        """Start a modlist restore for a wizard step through the normal
        restore path (progress popup + last-deployed-profile handling).
        *on_done(ok)* fires on the UI thread after the restore completes.
        Returns False when a restore can't be started (unconfigured game or
        a deploy/restore/install already running)."""
        game = self._gs.game
        if game is None or not game.is_configured() or not hasattr(game, "restore"):
            return False
        if self._deploy_running:
            return False
        # Same lingering-hook hazard as _wizard_run_deploy: _on_restore
        # refuses while an install (or a texture tool) owns the shared game.
        if getattr(self, "_install_running", False) or self._col_install_running:
            return False
        if self._tool_busy:
            return False
        self._restore_done_hooks.append(on_done)
        self._on_restore()
        return True

    def _wizard_refresh_plugins(self):
        """Wizard hook: re-run LOOT to refresh plugin metadata without touching
        the load order (the xEdit wizards call this after a clean/edit session
        so dirty/message flags update). Deferred a beat so it runs after the
        refresh_modlist() reload the wizard also fires - the LOOT refresh reads
        the plugin model's rows, which that reload rebuilds."""
        game = self._gs.game
        if game is None or not game.is_configured():
            return
        if not getattr(game, "loot_sort_enabled", False):
            return
        # QTimer so it lands after the modlist refresh + plugin reload settle.
        QTimer.singleShot(0, self._on_refresh_plugins)

    def _close_wizard_tab(self, key: str):
        if self._tabs.has_key(key):
            self._tabs.close_tab(key)

    def _close_wizard_tabs(self):
        """Close every open wizard tab (game switch - they're game-scoped).
        The prefix manager closes too: its active-game highlight goes stale."""
        for key in [k for k in list(self._tabs._keys)
                    if k.startswith("wizard:") or k == "prefix_manager"]:
            self._tabs.close_tab(key)

    def _download_only_active(self) -> bool:
        """The 'Download only' setting."""
        from Utils.ui_config import load_download_only
        try:
            return bool(load_download_only())
        except Exception:
            return False

    def _deliver_download(self, paths, metas: dict | None = None,
                          *, notify: bool = True, **install_kw) -> bool:
        """Install archives the APP downloaded, or leave them cached; True = queued."""
        # The gate for nxm / Nexus browser / Change Version / requirements /
        # update+reinstall redownloads. Archives the USER supplied (Install Mod
        # button, Downloads tab, drag-drop) call _install_paths directly.
        if not paths:
            return False
        if not self._download_only_active():
            self._install_paths(list(paths), metas=metas, **install_kw)
            return True
        names = [Path(p).name for p in paths]
        for n in names:
            self._append_log(
                f"[download-only] kept '{n}' in the cache - install skipped.")
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        if notify:
            if len(names) == 1:
                self._notify(self.tr("Downloaded '{0}' - install it from the "
                                     "Downloads tab.").format(names[0]), "success")
            else:
                self._notify(self.tr("Downloaded {0} archives - install them from "
                                     "the Downloads tab.").format(len(names)),
                             "success")
        return False

    def _install_paths(self, paths: list[str], metas: dict | None = None,
                       previous_mod_name: str | None = None,
                       preferred_names: dict | None = None,
                       on_all_done=None, clear_archives: bool = True,
                       place: dict | None = None,
                       target_profile_dir: Path | None = None):
        """Queue + install a list of archive paths (shared by the Install Mod
        button and the Downloads tab). FOMODs pause for the wizard mid-queue.
        *metas* optionally maps an archive path → a prebuilt NexusModMeta (the
        Nexus browser supplies the real mod_id/file_id so meta.ini is correct).
        *previous_mod_name* - when set (Change Version updating an existing mod),
        and a single install lands under a DIFFERENT folder name, offer to remove
        that previous version (it inherits its modlist slot).
        *preferred_names* - optional archive-path → forced mod-folder name. Forces
        the install into that folder and SILENTLY replaces it (no Mod-Already-Exists
        dialog). Used by Quick Update, where the name match is already confirmed.
        *on_all_done* - optional ``(ok, total, names, installed)`` callback fired
        once the whole batch finishes. ``installed`` maps each successful archive
        path to its final mod-folder name, including user renames.
        *clear_archives* - False for archives the USER supplied (Install Mod
        button, Downloads tab, reinstall-from-archive): 'Clear archive after
        install' only applies to archives the app downloaded itself.
        *place* - optional modlist placement for the installed mods (drag-drop
        from the Downloads tab): {"anchor": name|None, "after": bool,
        "at_end": bool}. Applied in _on_install_done just before the reload
        (and per-wizard for detached FOMOD/BAIN handoffs).
        *target_profile_dir* - install into THIS profile instead of the active
        one, skipping the Profile Group member picker (set by the group update
        routing below when a batch is split across the members that own the
        mods being updated)."""
        if not paths:
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        # Queue behind a running install OR deploy OR a detached-wizard staging
        # job. All three mutate the SAME shared game object (_active_profile_dir +
        # load_paths → get_effective_*_path); a deploy swaps the active profile to
        # last_deployed/target mid-flight (deploy_pipeline), so an install started
        # concurrently can stage its mod + write modindex.bin into the WRONG
        # profile's tree - the mod then appears only after a Refresh (the
        # intermittent "manual install didn't take" bug). Serialize instead.
        # _on_install_done, _on_op_done (deploy) AND _on_wizard_finish_done (staged
        # finish) all drain the queue.
        if getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            self._pending_install_batches.append({
                "paths": list(paths), "metas": metas,
                "previous_mod_name": previous_mod_name,
                "preferred_names": preferred_names,
                "on_all_done": on_all_done,
                "clear_archives": clear_archives,
                "place": place,
                "target_profile_dir": target_profile_dir})
            _busy = self.tr("install") if getattr(self, "_install_running", False) \
                else (self.tr("deploy") if getattr(self, "_deploy_running", False)
                      else self.tr("install"))
            self._notify(
                self.tr("Install queued - {0} will install after the current "
                        "{1} finishes.").format(Path(paths[0]).name, _busy), "info")
            return
        # A just-finished background worker (deploy/restore/collection install)
        # may have left the game's _active_profile_dir out of sync with the
        # selected profile - re-assert it so staging + the mod index resolve
        # against the profile the dropdown shows (Tk parity: gui.py re-ran
        # load_paths before installing).
        self._gs.reassert_active_profile()
        profile_dir = self._gs.profile_dir()
        if profile_dir is None:
            self._notify(self.tr("No active profile."), "warning")
            return
        if target_profile_dir is not None:
            # Pre-routed (group update split, see _group_update_routes) -
            # no picker, install exactly where the caller said.
            self._start_install_batch(
                paths, game, Path(target_profile_dir), metas=metas,
                previous_mod_name=previous_mod_name,
                preferred_names=preferred_names, on_all_done=on_all_done,
                clear_archives=clear_archives, place=place)
            return
        # Updating mods that are ALREADY in an active Profile Group (Quick
        # Update / Change Version / Reinstall): each new version goes straight
        # back to the profile the mod came from - no member question. Kept out
        # of the picker's try/except so a failure there can never fall through
        # to a second, group-wide install of the same archives.
        try:
            from Utils.profile_groups import is_group as _is_group
            _in_group = _is_group(profile_dir)
        except Exception as exc:
            print(f"[gui_qt] group check failed: {exc}", flush=True)
            _in_group = False
        if _in_group:
            routes, unrouted = self._group_update_routes(
                profile_dir, paths, preferred_names, previous_mod_name)
            if routes:
                self._start_group_routed_installs(
                    game, routes, unrouted, metas=metas,
                    previous_mod_name=previous_mod_name,
                    preferred_names=preferred_names, on_all_done=on_all_done,
                    clear_archives=clear_archives, place=place)
                return
        # Installing a NEW mod while a Profile Group is active: ask which member
        # profile should own it, then install into that member.
        try:
            from Utils.profile_groups import get_members, is_group
            if is_group(profile_dir):
                def _queue_batch(note):
                    self._pending_install_batches.append({
                        "paths": list(paths), "metas": metas,
                        "previous_mod_name": previous_mod_name,
                        "preferred_names": preferred_names,
                        "on_all_done": on_all_done,
                        "clear_archives": clear_archives, "place": place,
                        "target_profile_dir": None})
                    self._notify(note, "info")

                # One picker at a time - a second batch queues and replays
                # (re-prompting) when the current work drains.
                if getattr(self, "_member_picker_open", False):
                    _queue_batch(self.tr(
                        "Install queued - waiting for the current member "
                        "choice."))
                    return
                members = [m for m in get_members(profile_dir)
                           if (profile_dir.parent / m).is_dir()]
                if not members:
                    self._notify(self.tr(
                        "This group has no member profiles - add one first."),
                        "warning")
                    return
                from gui_qt.list_picker_overlay import ListPickerOverlay

                def picked(member):
                    self._member_picker_open = False
                    if not member:
                        self._notify(self.tr("Install cancelled."), "info")
                        return
                    # The picker held no busy flag - a deploy/auto-deploy or
                    # another install may have started while it was open.
                    # Re-check and queue instead of racing it.
                    if getattr(self, "_install_running", False) \
                            or getattr(self, "_deploy_running", False) \
                            or getattr(self, "_staged_finish_running", False) \
                            or self._staged_finish_queue:
                        _queue_batch(self.tr(
                            "Install queued - it will run after the current "
                            "operation finishes."))
                        return
                    self._start_install_batch(
                        paths, game, profile_dir.parent / member,
                        metas=metas, previous_mod_name=previous_mod_name,
                        preferred_names=preferred_names,
                        on_all_done=on_all_done,
                        clear_archives=clear_archives, place=place)

                self._member_picker_open = True
                ListPickerOverlay(
                    self,
                    self.tr("Install into which member profile?\n"
                            "('{0}' is a profile group - the mod will be "
                            "installed there and appear in the group.)")
                    .format(profile_dir.name),
                    [(m, m) for m in members], picked,
                    select_label=self.tr("Install"))
                return
        except Exception as exc:
            self._member_picker_open = False
            print(f"[gui_qt] group install picker failed: {exc}", flush=True)
        self._start_install_batch(
            paths, game, profile_dir, metas=metas,
            previous_mod_name=previous_mod_name,
            preferred_names=preferred_names, on_all_done=on_all_done,
            clear_archives=clear_archives, place=place)

    def _group_update_routes(self, profile_dir, paths, preferred_names,
                             previous_mod_name):
        """Split an install batch by the profile that already owns each mod.

        Updating a mod that is already in an active Profile Group (Quick
        Update, Change Version, Reinstall) must land back in the member profile
        the mod came from - asking which member would both be noise and let the
        same mod fork into a second profile. Returns (routes, unrouted): each
        route is {dir, paths, preferred, prev} for one owning profile, and
        *unrouted* holds the archives with no owner (genuinely new mods, which
        still get the member picker)."""
        try:
            from Utils.profile_groups import entry_owner_profile
        except Exception as exc:
            print(f"[gui_qt] group update routing failed: {exc}", flush=True)
            return [], list(paths)
        prefer = dict(preferred_names or {})
        # Change Version passes the mod's name separately, for a single archive.
        prev_path = paths[0] if (previous_mod_name and len(paths) == 1) else None
        routes: dict[str, dict] = {}
        unrouted: list[str] = []
        for p in paths:
            entry = prefer.get(p) or (previous_mod_name if p == prev_path else None)
            try:
                owner = entry_owner_profile(profile_dir, entry) if entry else None
            except Exception:
                owner = None
            if owner is None:
                unrouted.append(p)
                continue
            target, folder = owner
            r = routes.setdefault(str(target), {
                "dir": target, "paths": [], "preferred": {}, "prev": None})
            r["paths"].append(p)
            if p in prefer:
                # A group entry can carry a different name than the member
                # folder it links to - install into the MEMBER's folder.
                r["preferred"][p] = folder
            if p == prev_path:
                r["prev"] = previous_mod_name
        return list(routes.values()), unrouted

    def _start_group_routed_installs(self, game, routes, unrouted, *, metas,
                                     previous_mod_name, preferred_names,
                                     on_all_done, clear_archives, place):
        """Run one install batch per owning profile (first now, the rest
        queued behind it - installs are serialized). Archives with no owner are
        queued as a plain batch so they re-enter _install_paths and get the
        member picker."""
        metas = dict(metas or {})
        aggregate_cb = None
        if on_all_done is not None:
            # A split batch reports once, with the whole batch's tally (Quick
            # Update's summary). The unrouted remainder can be cancelled at the
            # picker, so it never counts toward the aggregate.
            state = {"left": len(routes), "ok": 0, "total": 0,
                     "names": [], "installed": {}}

            def _aggregate(ok, total, names, installed):
                state["ok"] += int(ok or 0)
                state["total"] += int(total or 0)
                state["names"].extend(list(names or []))
                state["installed"].update(dict(installed or {}))
                state["left"] -= 1
                if state["left"] <= 0:
                    on_all_done(state["ok"], state["total"], state["names"],
                                state["installed"])

            aggregate_cb = _aggregate

        for i, r in enumerate(routes):
            sub_metas = {p: metas[p] for p in r["paths"] if p in metas}
            self._append_log(
                f"[install] {len(r['paths'])} update(s) → source profile "
                f"'{Path(r['dir']).name}'.")
            if i == 0:
                self._start_install_batch(
                    list(r["paths"]), game, Path(r["dir"]), metas=sub_metas,
                    previous_mod_name=r["prev"],
                    preferred_names=r["preferred"], on_all_done=aggregate_cb,
                    clear_archives=clear_archives, place=place)
                continue
            self._pending_install_batches.append({
                "paths": list(r["paths"]), "metas": sub_metas,
                "previous_mod_name": r["prev"],
                "preferred_names": r["preferred"],
                "on_all_done": aggregate_cb,
                "clear_archives": clear_archives, "place": place,
                "target_profile_dir": Path(r["dir"])})
        if unrouted:
            self._pending_install_batches.append({
                "paths": list(unrouted), "metas":
                    {p: metas[p] for p in unrouted if p in metas},
                "previous_mod_name": None,
                "preferred_names": {p: n for p, n in (preferred_names or {}).items()
                                    if p in unrouted},
                "on_all_done": None, "clear_archives": clear_archives,
                "place": place, "target_profile_dir": None})

    def _start_install_batch(self, paths, game, profile_dir, *, metas,
                             previous_mod_name, preferred_names, on_all_done,
                             clear_archives, place):
        """Arm the install queue against *profile_dir* (the active profile, or
        a group member chosen in the picker) and start the batch."""
        self._install_running = True
        self._op_is_restore = False
        self._op_title = "Installing"
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(False)
        # Deploy/Restore/Play mutate the same shared game object the install
        # worker is using - lock them until _on_install_done re-enables.
        self._set_deploy_buttons_enabled(False)
        self._ensure_feedback()
        # Process archives ONE AT A TIME so a FOMOD can pause for the wizard.
        self._install_queue = list(paths)
        self._install_total = len(paths)
        self._install_ok = []
        # Exact archive path → final installed folder. Post-install consumers
        # must not infer this association from an unordered success-name list.
        self._install_results: dict[str, str] = {}
        # Archives handed off to a detached FOMOD/BAIN wizard: they leave the
        # pipeline immediately and report their own outcome later, so they count
        # toward neither ok nor the failure tally (see _on_install_done summary).
        self._install_handoffs = 0
        self._install_handoff_paths = set()
        self._install_game = game
        self._install_profile_dir = profile_dir
        self._install_metas = dict(metas or {})
        self._install_prev_name = previous_mod_name
        self._install_preferred = dict(preferred_names or {})
        self._install_all_done_cb = on_all_done
        self._install_clear_archives = clear_archives
        self._install_place = place
        self._notify(self.tr("Installing {0} mod(s)…").format(len(paths)) if len(paths) > 1
                     else self.tr("Installing {0}…").format(Path(paths[0]).name), "info")
        # Multi-archive batch (Downloads tab multi-select, Nexus batches):
        # extract several archives at once like a collection install, deferring
        # FOMOD/BAIN wizards to a sequential phase at the end. Change Version
        # (previous_mod_name) is always a single archive; keep it sequential.
        if len(paths) > 1 and previous_mod_name is None:
            self._install_batch_parallel()
        else:
            self._install_next()

    def _make_need_prefix_cb(self):
        """Return an on_need_prefix(required, file_list, mod_name) callback for the
        install worker. It runs on the WORKER thread, so it asks the UI thread to
        show the Set-Prefix overlay (via _need_prefix) and BLOCKS on an Event
        until the user responds - mirroring the Tk non-main-thread prefix dialog.
        Returns the prefix str ("" = as-is), or None (cancel)."""
        import threading

        def _cb(required, file_list, mod_name):
            holder = {"result": None}
            ev = threading.Event()
            self._need_prefix.emit({
                "required": required, "file_list": file_list,
                "mod_name": mod_name, "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_need_prefix_ui(self, payload):
        """UI thread: show the Set-Prefix overlay; unblock the worker when done.
        The progress popup is hidden while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.set_prefix_overlay import SetPrefixOverlay

        def _done(result):
            payload["holder"]["result"] = result
            payload["event"].set()

        SetPrefixOverlay.show_over(
            self, payload["mod_name"], payload["required"],
            payload["file_list"], _done)

    def _make_exists_cb(self):
        """Return an on_exists(mod_name, conflict, prepared) callback for
        finish_install. Runs on the WORKER thread → shows the Mod-Already-Exists
        overlay on the UI thread and BLOCKS until the user picks (replace /
        rename:<n> / cancel), mirroring _make_need_prefix_cb."""
        import threading

        def _cb(mod_name, conflict=False, prepared=None):
            holder = {"result": "cancel"}
            ev = threading.Event()
            self._mod_exists.emit({
                "mod_name": mod_name, "conflict": bool(conflict),
                "suggestions": self._install_name_suggestions(mod_name, prepared),
                "holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _install_name_suggestions(self, mod_name: str, prepared):
        """Naming candidates for the Mod-Already-Exists rename field (GH#368).
        Worker-thread safe: pure file reads, no Qt. Never raises."""
        if prepared is None:
            return []
        try:
            from Utils.mod_name_utils import name_suggestions, sibling_version_name
            meta = getattr(prepared, "prebuilt_meta", None)
            archive = getattr(prepared, "archive", None)
            staging = self._gs.staging_dir()
            previous = ""
            if meta is not None and staging is not None:
                previous = sibling_version_name(staging, mod_name, meta)
            return name_suggestions(
                meta,
                installation_file=archive.name if archive is not None else "",
                previous_name=previous, exclude=mod_name)
        except Exception as exc:
            print(f"[gui_qt] install name suggestions failed: {exc}", flush=True)
            return []

    def _on_mod_exists_ui(self, payload):
        """UI thread: show the Mod-Already-Exists overlay; unblock the worker."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.mod_exists_overlay import ModExistsOverlay

        def _done(result):
            payload["holder"]["result"] = result or "cancel"
            payload["event"].set()

        ModExistsOverlay.show_over(
            self, payload["mod_name"], payload["conflict"], _done,
            suggestions=payload.get("suggestions") or [])

    def _make_confirm_cet_cb(self, game):
        """Return a confirm_cet() callback for run_deploy_pipeline. Runs on the
        deploy WORKER thread: if Cyberpunk 2077 is being deployed in symlink mode
        with CET staged, asks the UI thread to show the warning and BLOCKS on an
        Event until the user chooses. Returns True to proceed, False to cancel.
        No conflict → returns True immediately without prompting (Tk parity:
        gui.dialogs.confirm_cet_symlink)."""
        import threading
        from Utils.cet_check import cet_symlink_conflict

        def _cb() -> bool:
            try:
                if not cet_symlink_conflict(game):
                    return True
            except Exception:
                return True
            holder = {"result": True}
            ev = threading.Event()
            self._confirm_cet.emit({"holder": holder, "event": ev})
            ev.wait()
            return holder["result"]

        return _cb

    def _on_confirm_cet_ui(self, payload):
        """UI thread: show the CET-requires-Hardlink warning; unblock the worker.
        The progress popup is cleared while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.confirm_overlay import ConfirmOverlay

        def _done(ok):
            payload["holder"]["result"] = bool(ok)
            payload["event"].set()

        ConfirmOverlay.show_over(
            self,
            self.tr("Cyber Engine Tweaks requires Hardlink mode"),
            self.tr(
                "Cyber Engine Tweaks is enabled, but cyber_engine_tweaks.asi "
                "will be symlinked.\n\nCET will not load from a symlinked "
                "asi. This happens when the deploy mode is Symlink, or when it "
                "is Hardlink but the game folder and mod staging folder are on "
                "different drives (hardlinks fall back to symlinks across "
                "drives).\n\nUse Hardlink mode with both folders on the same "
                "drive for CET to work.\n\nDeploy anyway?"
            ),
            _done,
            confirm_label=self.tr("Deploy anyway"),
            cancel_label=self.tr("Cancel"),
            card_h=340,
        )

    def _make_confirm_windows_fs_cb(self, game):
        """Return a confirm_windows_fs() callback for run_deploy_pipeline.
        Runs on the deploy WORKER thread: if the mod staging folder or a
        deploy target sits on a Windows filesystem (NTFS/exFAT - weak write
        guarantees on Linux, the root cause of GH#307's 0-byte files), asks
        the UI thread to show the advisory and BLOCKS on an Event until the
        user chooses. Confirming persists the drive-layout fingerprint so the
        popup shows once per game (re-arming if the drives change)."""
        import threading
        from Utils.fs_check import windows_fs_targets, fs_ack_fingerprint
        from Utils.ui_config import get_fs_warning_ack

        def _cb() -> bool:
            try:
                hits = windows_fs_targets(game)
                if not hits:
                    return True
                fp = fs_ack_fingerprint(hits)
                if get_fs_warning_ack(game.name) == fp:
                    return True   # user already chose "Deploy anyway" for this layout
            except Exception:
                return True
            holder = {"result": True}
            ev = threading.Event()
            self._confirm_windows_fs.emit({
                "holder": holder, "event": ev,
                "hits": hits, "fp": fp, "game_name": game.name,
            })
            ev.wait()
            return holder["result"]

        return _cb

    def _make_confirm_downgrade_cb(self, game, silent: bool = False):
        """Return a confirm_downgrade() callback for run_deploy_pipeline.

        Runs on the deploy WORKER thread: if this is Fallout 3 still on the
        Anniversary exe (1.7.0.4), which FOSE cannot load, asks the UI thread
        to show the prompt and BLOCKS on an Event until the user chooses.
        Returns True to deploy anyway, False to cancel (the user chose to open
        the Downgrade wizard instead).

        Silent auto-deploys (mod toggles) never prompt - a blocking modal on
        every toggle would be intrusive, and the user gets the prompt on their
        next explicit Deploy.
        """
        import threading
        from Utils.fo3_version_check import ANNIVERSARY_VERSION, needs_downgrade
        from Utils.ui_config import get_fo3_downgrade_ack

        def _cb() -> bool:
            if silent:
                return True
            try:
                if not needs_downgrade(game):
                    return True
                if get_fo3_downgrade_ack(game.name) == ANNIVERSARY_VERSION:
                    return True   # user already chose "Deploy anyway" for this build
            except Exception:
                return True
            holder = {"result": True}
            ev = threading.Event()
            self._confirm_downgrade.emit({
                "holder": holder, "event": ev,
                "version": ANNIVERSARY_VERSION, "game": game,
                "game_name": game.name,
            })
            ev.wait()
            return holder["result"]

        return _cb

    def _on_confirm_downgrade_ui(self, payload):
        """UI thread: show the Fallout 3 Anniversary-Edition downgrade prompt;
        unblock the worker. Confirming opens the Downgrade wizard and cancels
        the deploy (the wizard redeploys when it closes); declining persists the
        acknowledgement so the prompt shows once per exe build."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.confirm_overlay import ConfirmOverlay

        def _done(open_wizard):
            if open_wizard:
                # Cancel this deploy - the wizard restores the modlist itself
                # and redeploys when it closes.
                payload["holder"]["result"] = False
                payload["event"].set()
                self._open_fo3_downgrade_wizard(payload["game"])
                return
            # "Deploy anyway": remember it so the prompt shows once per build;
            # it re-arms if a game update changes the exe version again.
            try:
                from Utils.ui_config import save_fo3_downgrade_ack
                save_fo3_downgrade_ack(payload["game_name"], payload["version"])
            except Exception:
                pass
            payload["holder"]["result"] = True
            payload["event"].set()

        ConfirmOverlay.show_over(
            self,
            self.tr("Fallout 3 needs downgrading"),
            self.tr(
                "Fallout3.exe is version {0} - the Anniversary Edition "
                "update.\n\nThe script extender (FOSE) does not work with this "
                "version, so mods that need it will not load, no matter how "
                "they are deployed.\n\nRun the Downgrade wizard to patch the "
                "game back to a version FOSE supports. Your modlist is "
                "restored before patching and redeployed afterwards."
            ).format(payload["version"]),
            _done,
            confirm_label=self.tr("Open Downgrade Wizard"),
            cancel_label=self.tr("Deploy anyway"),
            danger=False,
            card_h=340,
        )

    def _open_fo3_downgrade_wizard(self, game):
        """Open the Fallout 3 Downgrade wizard from the deploy prompt."""
        tool = None
        try:
            tool = next((t for t in game.wizard_tools
                         if t.dialog_class_path ==
                         "wizards.fallout_downgrade.FalloutDowngradeWizard"), None)
        except Exception:
            tool = None
        if tool is None:
            self._notify(self.tr("Could not open the Downgrade wizard - open it "
                                 "from the Tools tab."), "warning")
            return
        self._open_wizard_tool(tool)

    def _on_confirm_windows_fs_ui(self, payload):
        """UI thread: show the Windows-filesystem advisory; unblock the worker.
        The progress popup is cleared while the user decides (no work running)."""
        if self._progress_popup is not None:
            self._progress_popup.clear()
        from gui_qt.confirm_overlay import ConfirmOverlay

        lines = "\n".join(
            f"•  {label}:  {fs_label}  ({mnt})"
            for label, fs_label, mnt in payload["hits"])

        def _done(ok):
            if ok:
                # Remember the acknowledgement so the advisory shows once per
                # game; it re-arms if the folders move to different mounts.
                try:
                    from Utils.ui_config import save_fs_warning_ack
                    save_fs_warning_ack(payload["game_name"], payload["fp"])
                except Exception:
                    pass
            payload["holder"]["result"] = bool(ok)
            payload["event"].set()

        ConfirmOverlay.show_over(
            self,
            self.tr("Windows filesystem detected"),
            self.tr(
                "These folders are on a Windows filesystem:\n\n{0}\n\n"
                "NTFS and exFAT drives have weak write guarantees on Linux: "
                "an unclean unmount, power loss, or a dual-boot Windows with "
                "Fast Startup enabled can silently truncate files to 0 KB - "
                "including deployed mod files and your mod staging library.\n\n"
                "A Linux filesystem (ext4/btrfs) is recommended for both the "
                "game and the mod staging folder.\n\nIf you continue, this "
                "warning won't be shown again for {1} unless the drives "
                "change."
            ).format(lines, payload["game_name"]),
            _done,
            confirm_label=self.tr("Deploy anyway"),
            cancel_label=self.tr("Cancel"),
            card_h=400,
        )

    def _install_next(self):
        """Pop the next queued archive: prepare it on a worker; the prepared
        result comes back on _prepared_ready (UI thread)."""
        if not self._install_queue:
            self._install_done.emit(len(self._install_ok), self._install_total,
                                    self._install_ok)
            return
        path = self._install_queue.pop(0)
        self._install_current_path = str(path)
        idx = self._install_total - len(self._install_queue)
        self._op_log.emit(f"Installing ({idx}/{self._install_total}): {Path(path).name}")

        import threading

        meta = getattr(self, "_install_metas", {}).get(path)
        forced_name = getattr(self, "_install_preferred", {}).get(path, "")

        def worker():
            from Utils.mod_install import prepare_archive
            try:
                prepared = prepare_archive(
                    path, self._install_game, self._install_profile_dir,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    prebuilt_meta=meta,
                    preferred_name=forced_name,
                    on_need_prefix=self._make_need_prefix_cb())
            except Exception as exc:
                self._op_log.emit(f"Prepare error ({Path(path).name}): {exc}")
                prepared = None
            self._prepared_ready.emit(prepared)

        threading.Thread(target=worker, daemon=True).start()

    def _install_batch_parallel(self):
        """Phase 1 of a multi-archive install: prepare + install every
        NON-interactive archive on a small worker pool (collection-install
        style: max_extract_workers threads gated by the extraction memory
        budget). Archives that need a wizard (FOMOD with steps, BAIN) are
        deferred; _on_install_batch_stage_done then runs them one-by-one
        through the existing sequential wizard handshake. Blocking UI prompts
        (need-prefix, mod-already-exists) are serialised so only one overlay
        shows at a time."""
        import threading

        paths = list(self._install_queue)
        self._install_queue = []
        total = self._install_total
        ui_lock = threading.Lock()
        raw_prefix_cb = self._make_need_prefix_cb()
        raw_exists_cb = self._make_exists_cb()

        def _serialized(cb):
            def inner(*a, **k):
                with ui_lock:
                    return cb(*a, **k)
            return inner

        prefix_cb = _serialized(raw_prefix_cb)
        exists_cb = _serialized(raw_exists_cb)

        def driver():
            from concurrent.futures import ThreadPoolExecutor
            from Utils.mod_install import (prepare_archive, finish_install,
                                           _archive_lists_fomod_config)
            from Utils.extract_budget import (ExtractionMemoryBudget,
                                              get_uncompressed_size)
            try:
                from Utils.ui_config import load_collection_settings
                workers = int(load_collection_settings().get(
                    "max_extract_workers", 4))
            except Exception:
                workers = 4
            workers = max(1, min(workers, len(paths)))
            budget = ExtractionMemoryBudget(max_workers=workers)
            lock = threading.Lock()
            ok_items: list[tuple[str, str]] = []
            deferred: list = []
            counters = {"done": 0}

            def one(path):
                name_for_log = Path(path).name
                meta = self._install_metas.get(path)
                forced = self._install_preferred.get(path, "")
                est = 0
                acquired = False
                try:
                    self._op_log.emit(f"Installing: {name_for_log}")
                    # Archive listing already shows fomod/ModuleConfig.xml →
                    # defer NOW, skipping an extract that would be thrown away
                    # and repeated in the deferred phase (collection parity).
                    if _archive_lists_fomod_config(path):
                        self._op_log.emit(
                            f"FOMOD installer detected: {name_for_log} - "
                            "deferred to the end of the batch.")
                        with lock:
                            deferred.append(path)
                        return
                    est = get_uncompressed_size(path)
                    budget.acquire(est)
                    acquired = True
                    prepared = prepare_archive(
                        path, self._install_game, self._install_profile_dir,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        prebuilt_meta=meta, preferred_name=forced,
                        on_need_prefix=prefix_cb)
                    if prepared is None:
                        return
                    if (prepared.is_fomod() and prepared.fomod_has_steps()) \
                            or prepared.is_bain():
                        kind = "FOMOD" if prepared.is_fomod() else "BAIN"
                        self._op_log.emit(
                            f"{kind} installer detected: {prepared.mod_name} "
                            "- deferred to the end of the batch.")
                        prepared.cleanup()
                        with lock:
                            deferred.append(path)
                        return
                    if prepared.is_fomod():
                        # FOMOD with no install steps → nothing to choose;
                        # install headlessly (parity with _on_prepared_ready).
                        self._op_log.emit(
                            f"FOMOD has no install options: "
                            f"{prepared.mod_name} - installing required files.")
                    name = finish_install(
                        prepared, None,
                        log_fn=lambda m: self._op_log.emit(str(m)),
                        on_exists=None if forced else exists_cb)
                    if name:
                        self._maybe_clear_archive(prepared)
                        with lock:
                            ok_items.append((str(path), name))
                except Exception as exc:
                    self._op_log.emit(f"Install error ({name_for_log}): {exc}")
                finally:
                    if acquired:
                        budget.release(est)
                    with lock:
                        counters["done"] += 1
                        done = counters["done"]
                    self._op_progress.emit(done, total, "Installing")

            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(one, paths))
            self._install_batch_stage_done.emit(ok_items, deferred)

        threading.Thread(target=driver, daemon=True).start()

    def _on_install_batch_stage_done(self, items, deferred):
        """UI thread: the parallel phase of a batch install finished. Run the
        optional rename-after-install prompts for the phase-1 installs (one at
        a time, as the sequential path does), then push the deferred FOMOD/BAIN
        archives through the normal sequential queue - an empty deferred list
        goes straight to the batch summary via _install_next."""
        items = [(str(path), name) for path, name in (items or []) if name]
        deferred = list(deferred or [])

        def _proceed():
            if deferred:
                self._op_log.emit(
                    f"Installing {len(deferred)} deferred FOMOD/BAIN mod(s)…")
            self._install_queue = deferred
            self._install_next()

        try:
            from Utils.ui_config import load_rename_mod_after_install
            rename_on = load_rename_mod_after_install()
        except Exception:
            rename_on = False
        if not rename_on:
            for path, name in items:
                self._install_ok.append(name)
                self._install_results[path] = name
            _proceed()
            return

        def _chain(i):
            if i >= len(items):
                _proceed()
                return

            path, name = items[i]

            def _named(final, _path=path, _next=i + 1):
                self._install_ok.append(final)
                self._install_results[_path] = final
                _chain(_next)

            self._maybe_prompt_rename(name, _named)

        _chain(0)

    def _on_prepared_ready(self, prepared):
        if prepared is None:
            self._one_install_done.emit(None)
            return
        if prepared.is_fomod() and not prepared.fomod_has_steps():
            # FOMOD with no install steps (only requiredInstallFiles /
            # conditionalFileInstalls) → nothing for the user to choose. Install
            # headlessly with default selections instead of opening the wizard
            # on an empty step list (which crashed with IndexError).
            self._op_log.emit(
                f"FOMOD has no install options: {prepared.mod_name} - "
                "installing required files.")
            self._run_finish_install(prepared, None)
        elif prepared.is_fomod() or prepared.is_bain():
            # Interactive install (FOMOD wizard / BAIN picker): the user may take
            # a long time choosing, but that wait does NO staging work. Hand the
            # prepared archive off to a DETACHED, uniquely-keyed wizard tab so
            # several can be open at once and the install pipeline is free to
            # continue with the next queued archive. The pipeline treats this
            # item as handed off (not counted here - the detached wizard reports
            # its own outcome). Staging on finish is serialised separately.
            self._open_wizard_install(prepared, from_queue=True)
            # Release the pipeline slot: this item is no longer pending. Pass the
            # handoff sentinel so _on_one_install_done just advances the queue
            # without a rename prompt or an ok-count (the wizard owns those).
            self._install_handoffs = getattr(self, "_install_handoffs", 0) + 1
            archive = getattr(prepared, "archive", None)
            if archive is not None:
                self._install_handoff_paths.add(str(archive))
            self._one_install_done.emit(_WIZARD_HANDOFF)
        else:
            self._run_finish_install(prepared, None)

    def _open_wizard_install(self, prepared, from_queue: bool):
        """Open a DETACHED FOMOD/BAIN wizard for *prepared* in its own tab.

        Unlike the old single-wizard flow, this does NOT hold the install
        pipeline: the tab is uniquely keyed (fomod_wizard_N / bain_picker_N) so
        multiple wizards coexist, and on finish the staging step runs through
        _run_staged_finish (serialised against installs/deploys - the shared
        game._active_profile_dir wrong-profile guard). Each wizard reports its
        own toast + modlist reload; there is no shared _fomod_done flag.

        *from_queue* is True when the archive came off the install queue (so its
        source archive should honour 'clear archive after install' just like the
        pipeline would); the flag is captured for the deferred staging call."""
        self._wizard_seq += 1
        seq = self._wizard_seq
        clear_archives = getattr(self, "_install_clear_archives", True)
        # Capture the Change-Version 'previous mod name' for THIS handoff (a
        # single-archive Change Version can be a FOMOD): the pipeline's shared
        # _install_prev_name is one-shot and would be cleared by later items, so
        # bind it to the wizard now. Consumed once in _on_wizard_finish_done.
        prev_name = getattr(self, "_install_prev_name", None) if from_queue else None
        if prev_name is not None:
            self._install_prev_name = None
        # Likewise capture this batch's drop placement (drag-install from the
        # Downloads tab) - the shared _install_place belongs to the CURRENT
        # batch and may be gone/replaced by the time this wizard finishes.
        place = getattr(self, "_install_place", None) if from_queue else None
        done = {"v": False}   # per-wizard double-fire guard (finish/cancel/close)
        # NB: the progress popup is intentionally NOT cleared here (unlike the old
        # blocking flow) - the install pipeline keeps running behind this detached
        # tab, so the popup still reflects that real ongoing work and clears itself
        # when the pipeline finishes.

        if prepared.is_fomod():
            tab_key = f"fomod_wizard_{seq}"
            from gui_qt.fomod_wizard_view import FomodWizardView

            def _finish(selections):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._stage_wizard_finish(prepared, selections, None,
                                          clear_archives, prev_name, place)

            def _cancel():
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                self._op_log.emit(f"FOMOD install cancelled: {prepared.mod_name}")
                prepared.cleanup()
                self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")

            _sel_path = None
            _game_name = getattr(prepared.game, "name", "")
            if _game_name:
                from Utils.config_paths import get_fomod_selections_path
                _sel_path = get_fomod_selections_path(_game_name,
                                                      prepared.mod_name)
            _inst, _act, _loose = prepared.fomod_context
            # prepared.fomod_context was read from plugins.txt/loadorder.txt on the
            # worker thread. Those files can lag the LIVE plugin panel: a mod
            # enabled in the model isn't persisted to plugins.txt until the next
            # save (e.g. Sort Plugins), so a FOMOD `state="Active"` dep wouldn't
            # match and its conditional option wouldn't auto-select until the user
            # sorted. Merge the panel's live enabled/all sets so the wizard sees
            # the true current state without needing a sort first.
            try:
                pm = getattr(self, "_plugin_model", None)
                if pm is not None:
                    live_all = pm.all_lower()
                    live_active = pm.enabled_lower()
                    _inst = set(_inst) | live_all
                    # A plugin present in the panel but not enabled is genuinely
                    # inactive - only add the live-enabled ones to active, and drop
                    # any stale disk-active entry the panel now shows as disabled.
                    _act = (set(_act) | live_active) - (live_all - live_active)
            except Exception:
                pass
            self._append_log(
                f"[fomod] {prepared.mod_name}: {len(_inst)} installed / "
                f"{len(_act)} active plugin(s) for condition eval")
            view = FomodWizardView(prepared.fomod_config, prepared.fomod_base,
                                   prepared.mod_name, on_finish=_finish,
                                   on_cancel=_cancel,
                                   saved_selections=prepared.saved_fomod_selections,
                                   selections_path=_sel_path,
                                   installed_files=_inst, active_files=_act,
                                   loose_files=_loose)
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _cancel())
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)
        else:   # BAIN
            tab_key = f"bain_picker_{seq}"
            from gui_qt.bain_picker_view import BainPickerView

            def _bfinish(result):
                if done["v"]:
                    return
                done["v"] = True
                self._tabs.close_tab(tab_key)
                if result is None:
                    self._op_log.emit(
                        f"BAIN install cancelled: {prepared.mod_name}")
                    prepared.cleanup()
                    self._notify(self.tr("Install cancelled: {0}").format(prepared.mod_name), "info")
                    return
                self._stage_wizard_finish(prepared, None, result,
                                          clear_archives, prev_name, place)

            view = BainPickerView(
                prepared.bain_subpkgs, prepared.bain_root, prepared.mod_name,
                on_done=_bfinish,
                readme_text=getattr(prepared, "readme_text", None),
                saved_selections=getattr(prepared, "saved_bain_selections", None))
            # Closing the tab (× / detached-window close) cancels the install.
            view.destroyed.connect(lambda *_: _bfinish(None))
            self._tabs.open_tab(view, self.tr("Install: {0}").format(prepared.mod_name),
                                key=tab_key)

    def _run_finish_install(self, prepared, selections, bain_selections=None):
        import threading

        # Quick Update (forced folder name) auto-confirms Replace-All - pass
        # on_exists=None so finish_install silently replaces the existing folder
        # instead of raising the Mod-Already-Exists overlay (Tk parity).
        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._one_install_done.emit(name)

        threading.Thread(target=worker, daemon=True).start()

    # -- detached-wizard staging --------------------------------------------
    def _stage_wizard_finish(self, prepared, selections, bain_selections,
                             clear_archives, prev_name=None, place=None):
        """A detached FOMOD/BAIN wizard finished - enqueue its staging step.

        Staging (finish_install) mutates the shared game (get_effective_mod_
        staging_path reads the live active profile), so it MUST run one-at-a-time
        against installs AND deploys - the same wrong-profile guard _install_paths
        enforces. Wizards may finish in any order and while a pipeline install or
        a deploy is running, so we queue here and drain via _run_staged_finish.

        *prev_name* - a Change-Version 'previous mod name' captured for this
        wizard; if the install lands a differently-named mod, the remove-previous
        prompt fires from _on_wizard_finish_done."""
        self._staged_finish_queue.append({
            "prepared": prepared, "selections": selections,
            "bain_selections": bain_selections,
            "clear_archives": clear_archives, "prev_name": prev_name,
            "place": place})
        self._notify(self.tr("Installing {0}…").format(prepared.mod_name), "info")
        self._run_staged_finish()

    def _run_staged_finish(self):
        """Drain one queued detached-wizard staging job if nothing else is
        touching the shared game. Re-armed by _on_wizard_finish_done, and also by
        _on_install_done / _on_op_done when a blocking install/deploy finishes."""
        import threading

        if not self._staged_finish_queue \
                or self._staged_finish_running \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_col_install_running", False):
            return
        job = self._staged_finish_queue.pop(0)
        self._staged_finish_running = True
        prepared = job["prepared"]
        selections = job["selections"]
        bain_selections = job["bain_selections"]
        prev_name = job.get("prev_name")
        # Consumed by _on_wizard_finish_done; staged finishes are serialised
        # (_staged_finish_running), so a plain attribute is race-free.
        self._staged_finish_place = job.get("place")
        # Carry the clear-archive policy captured when this wizard opened (the
        # pipeline's _install_clear_archives may have changed for a later batch).
        self._install_clear_archives = job["clear_archives"]
        # A just-finished worker may have left _active_profile_dir out of sync;
        # re-assert it so get_effective_mod_staging_path resolves to the profile
        # the dropdown shows (parity with _install_paths).
        self._gs.reassert_active_profile()
        self._ensure_feedback()

        _forced = str(getattr(prepared, "archive", "")) in \
            getattr(self, "_install_preferred", {})
        exists_cb = None if _forced else self._make_exists_cb()

        def worker():
            from Utils.mod_install import finish_install
            try:
                name = finish_install(
                    prepared, selections,
                    log_fn=lambda m: self._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: self._op_progress.emit(d, t, ph),
                    on_exists=exists_cb,
                    bain_selections=bain_selections)
            except Exception as exc:
                self._op_log.emit(f"Install error ({prepared.mod_name}): {exc}")
                name = None
            if name:
                self._maybe_clear_archive(prepared)
            self._wizard_finish_done.emit(name, prepared, prev_name)

        threading.Thread(target=worker, daemon=True).start()

    def _on_wizard_finish_done(self, name, prepared, prev_name):
        """UI thread: a detached-wizard staging job finished. Report it (toast /
        optional rename prompt / remove-previous), reload, then drain the next
        queued wizard job."""
        self._staged_finish_running = False
        place = getattr(self, "_staged_finish_place", None)
        self._staged_finish_place = None
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()

        def _drain_next():
            # Another wizard may have finished while this one staged, and an
            # install/deploy may have queued behind the staging lock.
            self._run_staged_finish()
            # Only release the deferred install/deploy/restore queues once the
            # staged-finish queue is FULLY drained AND idle - a still-running or
            # still-queued staging job holds the shared game, so releasing now
            # would re-open the wrong-profile race we just guarded.
            if not self._staged_finish_running and not self._staged_finish_queue:
                if self._pending_install_batches:
                    QTimer.singleShot(0, self._drain_pending_installs)
                pending, self._pending_after_staged = self._pending_after_staged, []
                for cb in pending:
                    try:
                        cb()
                    except Exception as exc:
                        self._append_log(f"[install] deferred action error: {exc}")

        def _after_named(final):
            archive_path = str(getattr(prepared, "archive", "") or "")
            pending_ts = self._pending_thunderstore_meta.pop(
                archive_path, None)
            if pending_ts is not None:
                link, info = pending_ts
                self._stamp_thunderstore_meta(link, info, final)
            # A wizard install may have landed in a group MEMBER while the
            # group is active - reconcile before the reload (mirrors
            # _on_install_done) or the mod is invisible until a Refresh.
            try:
                from Utils.profile_groups import materialize_if_group
                materialize_if_group(self._gs.game, self._gs.profile_dir(),
                                     log_fn=self._append_log)
            except Exception as exc:
                print(f"[gui_qt] group reconcile after wizard failed: {exc}",
                      flush=True)
            # Drag-install placement captured when this wizard opened.
            if place:
                self._apply_install_placement(
                    [final], place,
                    profile_dir=getattr(prepared, "profile_dir", None))
            self._reload_modlist()
            # Plugins reload comes from _on_conflicts_ready after the rebuild -
            # an immediate reload prunes the fresh plugins.txt entry against the
            # stale filemap (see _on_install_done).
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            self._notify(self.tr("Installed {0}").format(final), "success")
            # Change Version tab stays open - refresh its highlights.
            if prev_name:
                self._sync_change_version_after_install(final)
            # Change Version landed a different-named version → offer to remove
            # the previous version (Tk parity with _finish_one_install).
            if prev_name and final != prev_name:
                self._maybe_prompt_remove_previous(prev_name, final)
            _drain_next()

        if name:
            self._maybe_prompt_rename(name, _after_named)
        else:
            archive_path = str(getattr(prepared, "archive", "") or "")
            self._pending_thunderstore_meta.pop(archive_path, None)
            self._reload_modlist()
            # See _after_named - only load directly when no rebuild is coming.
            if not getattr(self, "_reload_had_entries", False):
                self._reload_plugins()
            # Failed/cancelled staging (finish_install returned None): keep the
            # queue moving.
            _drain_next()

    def _maybe_clear_archive(self, prepared):
        """Delete the source archive after a successful install, honouring the
        'Clear archive after install' / 'Keep FOMOD archives' settings (Tk
        parity). Runs on the install worker thread; failures are non-fatal.
        Skipped for user-supplied archives (see _install_paths clear_archives)."""
        try:
            if not getattr(self, "_install_clear_archives", True):
                return
            from Utils.ui_config import (
                load_clear_archive_after_install, load_keep_fomod_archives)
            if not load_clear_archive_after_install():
                return
            if prepared.is_fomod() and load_keep_fomod_archives():
                return
            archive = getattr(prepared, "archive", None)
            if archive is None:
                return
            from Nexus.nexus_download import delete_archive_and_sidecar
            from pathlib import Path as _P
            delete_archive_and_sidecar(_P(archive))
            self._op_log.emit(f"Removed archive: {_P(archive).name}")
        except Exception as exc:
            self._op_log.emit(f"Archive cleanup skipped: {exc}")

    def _on_one_install_done(self, name):
        if name is _WIZARD_HANDOFF:
            # Interactive archive handed off to a detached wizard tab - it owns
            # its own outcome (toast/rename/reload on finish). Just advance the
            # queue so the next archive installs while the wizard sits open.
            self._install_next()
            return
        if name:
            # Optional post-install rename prompt (Tk parity). Modal, before the
            # next queued install - keeps one dialog at a time.
            self._maybe_prompt_rename(name, self._finish_one_install)
        else:
            self._install_next()   # continue the queue

    def _finish_one_install(self, name: str):
        """Tail of _on_one_install_done, run after the optional rename prompt
        resolves (*name* is the final mod name)."""
        self._install_ok.append(name)
        current_path = getattr(self, "_install_current_path", "")
        if current_path:
            self._install_results[str(current_path)] = name
        # Change Version landed a different-named version → offer to remove
        # the previous version (Tk parity). One-shot per queue.
        prev = getattr(self, "_install_prev_name", None)
        if prev:
            # The Change Version tab stays open - repoint it at the installed
            # mod so the installed-version highlight is accurate.
            self._sync_change_version_after_install(name)
        if prev and name != prev:
            self._install_prev_name = None   # don't re-prompt for later items
            self._maybe_prompt_remove_previous(prev, name)
        self._install_next()   # continue the queue

    def _prev_version_profile(self, old_name: str):
        """(profile dir, old folder name) a Change Version 'remove previous'
        must act on. Inside a Profile Group both versions really live in a
        MEMBER profile - the group's copy is only a link - so the swap happens
        there and materialize rebuilds the group."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return None, old_name
        try:
            from Utils.profile_groups import entry_owner_profile, is_group
            if is_group(pdir):
                owner = entry_owner_profile(pdir, old_name)
                if owner is not None:
                    return owner[0], owner[1]
        except Exception as exc:
            print(f"[gui_qt] group remove-previous lookup failed: {exc}",
                  flush=True)
        return pdir, old_name

    def _maybe_prompt_remove_previous(self, old_name: str, new_name: str):
        """Show the borderless 'Remove previous version?' overlay if both the old
        and new mods exist. Remove → the new mod inherits the old one's modlist
        position + enabled state, then the old mod is removed."""
        from Utils.mod_copy import resolve_target_staging
        game = self._gs.game
        old_dir, old_folder = self._prev_version_profile(old_name)
        new_dir = getattr(self, "_install_profile_dir", None) \
            or self._gs.profile_dir()
        if game is None or old_dir is None or new_dir is None:
            return
        # In a group these are two DIFFERENT profiles' staging roots (the new
        # version was just installed into a member; the group only links to it).
        try:
            old_staging = Path(resolve_target_staging(game, Path(old_dir)))
            new_staging = Path(resolve_target_staging(game, Path(new_dir)))
        except Exception:
            return
        if not (old_staging / old_folder).is_dir() \
                or not (new_staging / new_name).is_dir():
            return
        from gui_qt.remove_previous_overlay import RemovePreviousOverlay

        def _done(result):
            if result == "remove":
                # Resolved NOW, not when the user answers: the install's
                # group reconcile runs first and renames the group entry to
                # the new version in place (same identity, newer version), so
                # a late lookup of the old name would find nothing and fall
                # back to the group - where the old row no longer exists and
                # the new mod would be repositioned to the top.
                self._remove_previous_version(
                    old_name, new_name, profile_dir=old_dir,
                    old_folder=old_folder)

        RemovePreviousOverlay.show_over(self, old_name, new_name, _done)

    def _remove_previous_version(self, old_name: str, new_name: str, *,
                                 profile_dir: Path | None = None,
                                 old_folder: str | None = None):
        """New mod inherits old's modlist slot + enabled state; old is removed.
        In a Profile Group the files live in a MEMBER profile
        (*profile_dir*/*old_folder*, captured when the prompt was raised), so
        the removal goes through the group's own removal path - going through
        remove_mods there resolves staging against the ACTIVE profile and would
        unlink the farm entry while the member kept the mod."""
        try:
            from Utils.mod_remove import remove_mods
            from Utils.modlist import read_modlist, replace_mod_in_place
            if profile_dir is None:
                profile_dir, old_folder = self._prev_version_profile(old_name)
            game = self._gs.game
            pdir = Path(profile_dir) if profile_dir is not None else None
            old_folder = old_folder or old_name
            if pdir is None or game is None:
                return
            log = lambda m: self._append_log(f"[remove] {m}")   # noqa: E731
            active = self._gs.profile_dir()
            group_dir = None
            try:
                from Utils.profile_groups import is_group
                if active is not None and is_group(active) \
                        and Path(active) != pdir:
                    group_dir = Path(active)
            except Exception as exc:
                print(f"[gui_qt] remove-previous group check failed: {exc}",
                      flush=True)
            # New inherits old's enabled state + position in the profile that
            # owns the files, and the OLD row is dropped there (remove_mods
            # intentionally does NOT touch modlist.txt).
            replace_mod_in_place(pdir / "modlist.txt", old_folder, new_name)
            if group_dir is None:
                # Delete the old mod's files/plugins/index (NOT its modlist row
                # - already dropped above).
                remove_mods(game, pdir, [old_folder], log_fn=log)
            else:
                from Utils.profile_groups import (remove_member_mod,
                                                  remove_mods_from_group)
                gml = group_dir / "modlist.txt"
                listed = any(e.name == old_folder and not e.is_separator
                             for e in read_modlist(gml))
                if listed:
                    # Both versions are group entries: undeploy + drop the old
                    # one properly, and give the new one its slot. A locked
                    # member vetoes the removal - then the row stays too.
                    if remove_mods_from_group(game, group_dir, [old_folder],
                                              log_fn=log):
                        replace_mod_in_place(gml, old_folder, new_name)
                else:
                    # The group's reconcile already renamed its entry to the
                    # new version - only the member's stale copy is left.
                    remove_member_mod(game, pdir, old_folder, log_fn=log)
        except Exception as exc:
            self._append_log(f"[install] remove-previous failed: {exc}")
        # The swap happened member-side when a group is active - rebuild the
        # group so the reload sees the new version in the old one's slot
        # (same identity → the reconcile migrates the entry in place).
        try:
            from Utils.profile_groups import materialize_if_group
            materialize_if_group(self._gs.game, self._gs.profile_dir(),
                                 log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] group reconcile after remove-previous failed: "
                  f"{exc}", flush=True)
        self._reload_modlist()
        self._rebuild_conflicts_async()
        # The Change Version tab may still be open on the mod that was just
        # removed - swap it to the version that replaced it.
        self._sync_change_version_after_install(new_name)

    def _maybe_prompt_rename(self, name: str, on_done):
        """If 'Rename mod after install' is on, prompt for a new name and rename
        the mod (staging folder + index + modlist entry). Calls ``on_done`` with
        the final name (unchanged if the user cancels or the rename fails)."""
        try:
            from Utils.ui_config import load_rename_mod_after_install
            if not load_rename_mod_after_install():
                on_done(name)
                return
        except Exception:
            on_done(name)
            return

        def _named(new):
            if new is None or not new.strip():
                on_done(name)
                return
            renamed = self._rename_mod_on_disk(name, new.strip())
            on_done(renamed or name)

        from gui_qt.text_input_overlay import TextInputOverlay
        TextInputOverlay.show_over(
            self, "Rename mod", "New name for the installed mod:", _named,
            initial=name, ok_label=self.tr("Rename"),
            suggestions=self._mod_name_suggestions(name))

    def _mod_name_suggestions(self, name: str, staging=None):
        """Naming candidates for a staged mod: Nexus names, sibling version,
        cleaned + raw archive filename (GH#368). Never raises."""
        try:
            from Utils.mod_name_utils import suggest_names_for_staged_mod
            root = staging if staging is not None else self._gs.staging_dir()
            return suggest_names_for_staged_mod(root, name)
        except Exception as exc:
            print(f"[gui_qt] name suggestions failed for {name!r}: {exc}",
                  flush=True)
            return []

    def _rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Rename a mod: staging folder → new, modindex entry, modlist entry,
        then reload. Returns the sanitised new name on success, else None.
        Mirrors the Tk modlist_panel.rename_mod_by_name operation.

        If the target name is already taken, this shows the Mod-Already-Exists
        overlay (Replace All / Rename… / Cancel) rather than failing outright,
        and the rename completes asynchronously through that flow (so the return
        value is None - the folder is still under ``old_name`` at that point)."""
        from Utils.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(new_name)
        if not old_name or not new_name or old_name == new_name:
            return None
        # In a group the staging entry is a symlink and the folder name comes
        # from the OWNING MEMBER - renaming the link would just be fought by
        # the next reconcile. Rename it in the member profile instead.
        try:
            from Utils.profile_groups import is_group, owner_of
            pdir = self._gs.profile_dir()
            if pdir is not None and is_group(pdir):
                owner = owner_of(pdir, old_name)
                self._notify(self.tr(
                    "'{0}' belongs to the member profile '{1}' - switch to "
                    "that profile to rename it.").format(
                        old_name, owner[0] if owner else "?"), "warning")
                return None
        except Exception:
            pass
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        new_folder = staging / new_name
        if new_folder.exists():
            # Collision → let the user Replace the existing mod, pick another
            # name, or cancel (same overlay the installer uses).
            from gui_qt.mod_exists_overlay import ModExistsOverlay

            def _resolved(result):
                if not result or result == "cancel":
                    return
                if result == "replace":
                    self._replace_then_rename(old_name, new_name)
                elif result.startswith("rename:"):
                    self._rename_mod_on_disk(old_name, result[len("rename:"):])

            # Suggestions come from the mod BEING renamed, not the occupant.
            ModExistsOverlay.show_over(
                self, new_name, False, _resolved,
                suggestions=self._mod_name_suggestions(old_name))
            return None
        return self._do_rename_mod_on_disk(old_name, new_name)

    def _replace_then_rename(self, old_name: str, new_name: str) -> None:
        """Fully remove the existing mod occupying *new_name*, drop its modlist
        row, then rename *old_name* → *new_name* (used by the rename-collision
        Replace-All path)."""
        try:
            from Utils.mod_remove import remove_mods
            remove_mods(self._gs.game, self._gs.profile_dir(), [new_name],
                        log_fn=self._append_log)
        except Exception as exc:
            self._notify(self.tr("Replace failed: {0}").format(exc), "warning")
            return
        # Drop the replaced mod's modlist row so it isn't left dangling. Edit
        # the file, not self._modlist_model - see _do_rename_mod_on_disk: for a
        # rename-after-install the model is still the pre-install snapshot, and
        # remove_row would save that stale state over the real modlist.
        try:
            from Utils.modlist import read_modlist, write_modlist, modlist_lock
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with modlist_lock(ml_path):
                    entries = [e for e in read_modlist(ml_path)
                               if e.is_separator or e.name != new_name]
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] replace-then-rename modlist update failed: {exc}",
                  flush=True)
        self._do_rename_mod_on_disk(old_name, new_name)

    def _do_rename_mod_on_disk(self, old_name: str, new_name: str) -> str | None:
        """Perform the actual rename (staging folder + index + state + modlist),
        assuming *new_name* is free. Returns the new name on success."""
        staging = self._gs.staging_dir()
        if staging is None:
            return None
        old_folder = staging / old_name
        new_folder = staging / new_name
        try:
            if old_folder.is_dir():
                old_folder.rename(new_folder)
        except OSError as exc:
            self._notify(self.tr("Rename failed: {0}").format(exc), "warning")
            return None
        # Keep the persistent mod index in sync (avoids a full rescan).
        try:
            from Utils.filemap import rename_in_mod_index
            from Utils.ui_config import load_normalize_folder_case
            idx_path = staging.parent / "modindex.bin"
            rename_in_mod_index(idx_path, old_name, new_name,
                                normalize_folder_case=load_normalize_folder_case())
        except Exception:
            pass
        # Re-key the name-keyed per-mod state (strip prefixes, disabled plugins,
        # excluded files, notes) - Tk parity: _migrate_mod_name_state.
        try:
            from Utils.mod_rename import migrate_mod_state
            migrate_mod_state(self._gs.profile_dir(), old_name, new_name,
                              log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] rename state migration failed: {exc}", flush=True)
        # Rename in the FILE, not via self._modlist_model: the rename-after-
        # install prompt resolves before _on_install_done's reload, so the row
        # isn't in the model yet - editing it was a silent no-op and save()
        # then clobbered the just-installed entry (mods "vanishing until
        # Refresh"). The reload below catches the model up.
        try:
            from Utils.modlist import read_modlist, write_modlist, modlist_lock
            ml_path = self._gs.modlist_path()
            if ml_path is not None:
                with modlist_lock(ml_path):
                    entries = read_modlist(ml_path)
                    for e in entries:
                        if not e.is_separator and e.name == old_name:
                            e.name = new_name
                            break
                    else:
                        print(f"[gui_qt] rename: no modlist entry named "
                              f"{old_name!r} to rename.", flush=True)
                    write_modlist(ml_path, entries)
        except Exception as exc:
            print(f"[gui_qt] rename modlist update failed: {exc}", flush=True)
        self._reload_modlist()
        self._notify(self.tr("Renamed to '{0}'.").format(new_name), "info")
        return new_name

    def _on_install_done(self, ok: int, total: int, names):
        self._install_running = False
        # Drain any batch queued while this one ran (deferred so it starts
        # after this handler - and any on_all_done callback - completes).
        if self._pending_install_batches:
            QTimer.singleShot(0, self._drain_pending_installs)
        # A detached-wizard staging job may have finished (or been requested)
        # while this pipeline held the shared game - let it run now.
        if self._staged_finish_queue:
            QTimer.singleShot(0, self._run_staged_finish)
        if hasattr(self, "_install_btn"):
            self._install_btn.setEnabled(True)
        if not (self._deploy_running or self._col_install_running
                or self._tool_busy):
            self._set_deploy_buttons_enabled(True)
        if self._progress_popup is not None:
            self._schedule_op_clear(1200)
        # Drag-install from the Downloads tab: move the freshly installed mods
        # to the drop position BEFORE the reload so the modlist appears with
        # them already in place. Wizard handoffs position themselves later
        # (_on_wizard_finish_done) with the same captured placement.
        # Install landed in a group MEMBER while the group is active -
        # reconcile so the new mod's entry/link/index exist before the reload
        # (and before any drop-position placement references it).
        try:
            from Utils.profile_groups import materialize_if_group
            materialize_if_group(self._gs.game, self._gs.profile_dir(),
                                 log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] group reconcile after install failed: {exc}",
                  flush=True)
        place = getattr(self, "_install_place", None)
        self._install_place = None
        if place and names:
            self._apply_install_placement(list(names), place)
        # Adopt any Thunderstore mod installed outside the ror2mm pipeline
        # (Downloads tab, Install Mod button, drag-drop): those paths never
        # reach _stamp_thunderstore_meta, so without this a hand-installed
        # package gets only a [General] section and no update checking.
        self._auto_identify_thunderstore(names)
        self._reload_modlist()
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready, after the
        # conflict/filemap rebuild queued by _reload_modlist - NOT here. An
        # immediate reload reads the STALE filemap (the just-installed mod's
        # plugin isn't in it yet), so load_plugins' phantom prune deleted the
        # plugins.txt entry _add_plugins had just appended; the plugin then
        # appeared in the panel only via filemap recovery, never persisted
        # (observed: SkyUI_SE.esp pruned within the same second as its install).
        # Only load directly when the modlist is empty and no rebuild is coming.
        if not getattr(self, "_reload_had_entries", False):
            self._reload_plugins()
        # Re-flag Reinstall in the Downloads tab now that meta.ini changed.
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        # A failed Change Version install never reaches the per-item retarget -
        # don't leave the tab's "Installing…" notice up forever.
        chv = getattr(self, "_change_version_view", None)
        if chv is not None:
            chv.clear_install_status()
        # A batch owner (Quick Update) reports its own summary; skip the generic
        # install toast and hand control back to it.
        cb = getattr(self, "_install_all_done_cb", None)
        self._install_all_done_cb = None
        if cb is not None:
            installed = dict(getattr(self, "_install_results", {}) or {})
            cb(ok, total, names, installed)
            return
        # Archives handed off to a detached wizard aren't done yet - they report
        # their own toast on finish, so drop them from this batch's tally.
        handoffs = getattr(self, "_install_handoffs", 0)
        total = max(0, total - handoffs)
        if total == 0:
            # Nothing installed synchronously; every archive opened a wizard (or
            # there was nothing to summarise). Let the wizards speak for
            # themselves - no batch toast.
            return
        self._notify_install_summary(ok, total, names)

    def _apply_install_placement(self, names, place, profile_dir=None):
        """Reposition just-installed mods at the Downloads-tab drop point (see
        _install_paths *place*). Anchoring is by mod NAME - rows may have
        shifted since the drop; a vanished anchor leaves the mods at the top,
        the normal install position."""
        pd = profile_dir or getattr(self, "_install_profile_dir", None)
        # The drop anchor names a row in the VIEWED modlist. For an install
        # routed into a group member, that is the GROUP's list (the entries
        # exist there post-reconcile), not the member's.
        try:
            from Utils.profile_groups import is_group
            active = self._gs.profile_dir()
            if active is not None and is_group(active):
                pd = active
        except Exception:
            pass
        if pd is None:
            return
        try:
            from Utils.modlist import move_mods_to_anchor
            move_mods_to_anchor(Path(pd) / "modlist.txt", names,
                                anchor=place.get("anchor"),
                                after=bool(place.get("after")),
                                at_end=bool(place.get("at_end")))
        except Exception as exc:
            self._append_log(f"[install] drop placement skipped: {exc}")

    def _drain_pending_installs(self):
        """Start the next install batch queued while an earlier install OR
        deploy OR a detached-wizard staging job ran. If a callback/coalesced
        deploy already started work in the meantime, leave the queue for the next
        _on_install_done / _on_op_done / _on_wizard_finish_done to drain (all
        call here)."""
        if not self._pending_install_batches \
                or getattr(self, "_install_running", False) \
                or getattr(self, "_deploy_running", False) \
                or getattr(self, "_staged_finish_running", False) \
                or self._staged_finish_queue:
            return
        b = self._pending_install_batches.pop(0)
        self._install_paths(b["paths"], metas=b["metas"],
                            previous_mod_name=b["previous_mod_name"],
                            preferred_names=b["preferred_names"],
                            on_all_done=b["on_all_done"],
                            clear_archives=b.get("clear_archives", True),
                            place=b.get("place"),
                            target_profile_dir=b.get("target_profile_dir"))

    def _build_modlist(self) -> QWidget:
        self._modlist_model = ModListModel([])
        self._modlist_model.enabled_changed.connect(self._on_mods_enabled_changed)
        self._modlist_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._modlist_view = ModListView(self._modlist_model)
        # Drag-install: archives dropped from the Downloads tab install at the
        # drop position (drop slot → name-anchored placement).
        self._modlist_view.on_archives_dropped = self._on_archives_dropped
        # Search/filter hidden sets are row-indexed, so any structural change
        # (reorder/insert/remove) leaves them misaligned and must be recomputed
        # from the current entries - the view's own handler only re-applies the
        # STALE indices, so search would skip/mis-show rows until a full reload.
        # Connected AFTER the view so its cache-drop + re-span run first.
        # modelReset is handled by _reload_modlist's explicit reapply.
        for sig in (self._modlist_model.layoutChanged,
                    self._modlist_model.rowsMoved,
                    self._modlist_model.rowsInserted,
                    self._modlist_model.rowsRemoved):
            sig.connect(self._on_modlist_layout_changed)
        return self._modlist_view

    def _on_modlist_layout_changed(self, *_a):
        self._apply_modlist_search()
        self._apply_modlist_filters()

    def _on_archives_dropped(self, paths: list[str], slot: int):
        """Archives dragged from the Downloads tab were dropped on the modlist
        at display row *slot* - install them there."""
        self._install_paths(paths, clear_archives=False,
                            place=self._placement_from_slot(slot))

    def _placement_from_slot(self, slot: int) -> dict | None:
        """Translate a modlist display drop slot into a name-anchored placement
        for _install_paths (None = default top placement). The anchor is the
        first real entry at/below the slot: mod names are stable across the
        install (row indices are not - the batch prepends rows)."""
        m = self._modlist_model
        key, _asc = m.sort_state()
        if key not in (None, "priority"):
            # A non-priority column sort shows a permutation of the load order,
            # so a visual slot has no load-order meaning. Install normally.
            return None
        from gui_qt.modlist_model import _PINNED_NAMES, ROOT_FOLDER_NAME
        anchor = None
        at_end = False
        for r in range(max(0, slot), m.rowCount()):
            name = m.entry(r).name
            if name == ROOT_FOLDER_NAME:
                at_end = True
                break
            if name not in _PINNED_NAMES:
                anchor = name
                break
        if m.reverse_mode_active:
            # Reverse-priority display is the inverted file order: visually
            # above the anchor = AFTER it in modlist.txt. No anchor (dropped at
            # the visual bottom) = the natural top - the default placement.
            return {"anchor": anchor, "after": True} if anchor else None
        if anchor is not None:
            return {"anchor": anchor}
        return {"at_end": True} if at_end else None

    def _on_mods_enabled_changed(self, changes):
        """Mods were toggled (checkbox / Enable-Disable all / context menu) -
        mirror the change into plugins.txt + loadorder.txt (Tk parity:
        _sync_plugins_for_toggle) and refresh the Plugins tab."""
        from Utils.plugin_sync import sync_plugins_for_mods
        try:
            wrote = sync_plugins_for_mods(
                self._gs.game, self._gs.profile_dir(), self._gs.staging_dir(),
                list(changes or []), log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] plugin sync failed: {exc}", flush=True)
            return
        # NOTE: the plugin panel is reloaded from _on_conflicts_ready (after the
        # filemap rebuild that save()→on_saved() kicked off finishes), NOT here.
        # Reloading now would read a STALE filemap - the rebuild is still in
        # flight - so a just-enabled patcher mod's light copies wouldn't be the
        # resolved winners yet (ESL flags missing), and a just-disabled mod's
        # plugins couldn't be recovered from the fresh filemap. Tk parity:
        # gui.py _on_filemap_rebuilt refreshes the plugins tab after the rebuild.
        _ = wrote

    def _build_modlist_area(self) -> QWidget:
        """Modlist column: the list + its tool footer (buttons + search) stacked
        vertically, with a collapsible filter side panel docked on the left (Tk
        parity - the filter panel pushes the column right, not an overlay)."""
        # The list + footer share one vertical column. A hidden 'new profile'
        # bar sits at the very top (row 0, Tk parity) and appears when the user
        # picks 'Add new profile…'.
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        from gui_qt.new_profile_bar import NewProfileBar
        self._new_profile_bar = NewProfileBar(
            on_create=self._on_new_profile_create,
            on_cancel=lambda: None)
        cv.addWidget(self._new_profile_bar)
        cv.addWidget(self._build_modlist(), 1)
        cv.addWidget(self._modlist_footer())

        area = QWidget()
        h = QHBoxLayout(area)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self._modlist_filter_panel = self._build_modlist_filter_panel()
        self._modlist_filter_panel.setVisible(False)
        h.addWidget(self._modlist_filter_panel)
        # The Mod Files filter panel shares this window-left slot so it opens in
        # the same place as the modlist filters (just a different filter set).
        self._mod_files_filter_panel = self._build_mod_files_filter_panel()
        self._mod_files_filter_panel.setVisible(False)
        h.addWidget(self._mod_files_filter_panel)
        # The Data filter panel shares the same window-left slot.
        self._data_filter_panel = self._build_data_filter_panel()
        self._data_filter_panel.setVisible(False)
        h.addWidget(self._data_filter_panel)
        # The Downloads filter panel shares the slot too.
        self._downloads_filter_panel = self._build_downloads_filter_panel()
        self._downloads_filter_panel.setVisible(False)
        h.addWidget(self._downloads_filter_panel)
        # The Text Files filter panel shares the slot too.
        self._text_files_filter_panel = self._build_text_files_filter_panel()
        self._text_files_filter_panel.setVisible(False)
        h.addWidget(self._text_files_filter_panel)
        # The Saves filter panel shares the slot too.
        self._saves_filter_panel = self._build_saves_filter_panel()
        self._saves_filter_panel.setVisible(False)
        h.addWidget(self._saves_filter_panel)
        # The Plugins filter panel shares the slot too.
        self._plugin_filter_panel = self._build_plugin_filter_panel()
        self._plugin_filter_panel.setVisible(False)
        h.addWidget(self._plugin_filter_panel)
        h.addWidget(col, 1)
        self._install_tab_filter_menus()
        return area

    # -- header filter menus (the tabs' eye button) -------------------------
    def _install_tab_filter_menus(self):
        """Pin an eye button to the left of each simpler tab's column header,
        opening that tab's filters as a menu with one submenu per category.
        (The modlist + plugins views grow their own - theirs also carries the
        column show/hide list.) Every button drives the tab's filter side
        panel, so menu and panel are always the same state."""
        from gui_qt.mod_files_view import ModFilesView
        from gui_qt.data_view import DataView
        from gui_qt.saves_view import SavesView
        mf, tf, sv, dv, dl = (self._mod_files_view, self._text_files_view,
                              self._saves_view, self._data_view,
                              self._downloads_view)
        for view, panel, spec, dyn, sync in (
            (mf, self._mod_files_filter_panel, ModFilesView.filter_spec(),
             {"filetypes": mf.filetype_items},
             self._sync_mod_files_filter_list),
            (tf, self._text_files_filter_panel, tf.filter_spec(),
             {"filetypes": tf.filetype_items},
             self._sync_text_files_filter_list),
            (sv, self._saves_filter_panel, SavesView.filter_spec(),
             {"filetypes": sv.filetype_items},
             self._sync_saves_filter_list),
            (dv, self._data_filter_panel, DataView.filter_spec(),
             {"filetypes": dv.filetype_items},
             self._sync_data_filter_list),
            (dl, self._downloads_filter_panel, dl.filter_spec(),
             {"filetypes": dl.filetype_items, "locations": dl.location_items},
             self._sync_downloads_filter_list),
        ):
            self._install_filter_menu_button(view._tree, panel, spec, dyn, sync)

    def _install_filter_menu_button(self, tree, panel, spec, dyn_fns, sync):
        from gui_qt.filter_menu_button import FilterMenuButton, BTN_W
        # Paint the first column's label clear of the button; the button's
        # opaque background covers the strip that leaves bare.
        tree.header_left_pad = BTN_W
        btn = FilterMenuButton(tree)
        btn.sections_fn = lambda: self._filter_menu_sections(
            panel, spec, dyn_fns, sync)
        btn.on_toggle = lambda key, on: self._on_filter_menu_toggle(
            panel, key, on)
        btn.on_clear = panel.clear_all
        btn.any_active = panel.any_active
        return btn

    def _filter_menu_sections(self, panel, spec, dyn_fns, sync):
        """Resolve a filter spec into menu sections (one submenu per category),
        reading the live tri-states off the tab's filter panel."""
        from PySide6.QtCore import QCoreApplication
        # Spec labels/titles are canonical English registered for translation
        # under the FilterSidePanel context (see filter_panel._TR_MARKERS).
        tr = lambda s: QCoreApplication.translate("FilterSidePanel", s) if s else ""
        # Dynamic lists are only refreshed while the panel is open - force one
        # now so the menu (and the panel behind it) has the current items.
        sync(force=True)
        sections = []
        for sec in spec:
            if sec.get("type", "checks") == "dynamic":
                sec_id = sec["id"]
                fn = dyn_fns.get(sec_id)
                entries = [
                    ((sec_id, key),
                     f"{label}  ({count:,})" if count is not None else label,
                     panel.dynamic_state(sec_id, key))
                    for key, label, count in (fn() if fn else [])]
            else:
                entries = [((None, key), tr(label), panel.check_state(key))
                           for key, label, _enabled in sec.get("items", [])]
            sections.append({"title": tr(sec.get("title", "")),
                             "entries": entries})
        return sections

    def _panel_filters_active(self, attr: str) -> bool:
        """Whether the named filter side panel has anything applied (greys the
        column menus' "Clear all filters"). Tolerates the panel not built yet."""
        panel = getattr(self, attr, None)
        return bool(panel is not None and panel.any_active())

    def _clear_panel_filters(self, attr: str):
        panel = getattr(self, attr, None)
        if panel is not None:
            panel.clear_all()   # emits changed -> re-filter + footer tint

    def _on_filter_menu_toggle(self, panel, key, on: bool):
        """Apply a filter picked from a header menu by driving the tab's filter
        panel, so the panel stays in sync and the normal pipeline (re-filter +
        footer-button tint) runs."""
        sec_id, item = key
        state = 1 if on else 0
        if sec_id is None:
            panel.set_check(item, state)
        else:
            panel.set_dynamic_check(sec_id, item, state)

    def _build_text_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._text_files_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_text_files_filter_changed)
        panel.close_requested.connect(self._toggle_text_files_filters)
        self._text_files_view.filetypes_changed.connect(
            self._sync_text_files_filter_list)
        return panel

    def _toggle_text_files_filters(self):
        panel = self._text_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._saves_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_text_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_text_files_filter_changed(self, state: dict):
        self._text_files_view.apply_filter_state(state)
        self._sync_filters_btn("_tf_filters_btn")

    def _build_plugin_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist_filter import PLUGIN_STATUS_FILTERS
        items = [(key, label, True) for key, label in PLUGIN_STATUS_FILTERS]
        spec = [{"title": "By status", "type": "checks", "items": items}]
        panel = FilterSidePanel(spec, title=self.tr("Filters"))
        panel.changed.connect(self._on_plugin_filter_changed)
        panel.close_requested.connect(self._toggle_plugin_filters)
        self._plugin_filter_state: dict = {}
        return panel

    def _toggle_plugin_filters(self):
        panel = self._plugin_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._saves_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._apply_plugin_filters()
        else:
            panel.setVisible(False)

    def _on_plugin_filter_changed(self, state: dict):
        self._plugin_filter_state = state
        self._apply_plugin_filters()
        self._sync_filters_btn("_plugin_filters_btn")

    def _quick_plugin_filter_state(self, key: str) -> int:
        """Current tri-state for a plugin status filter (for the column-menu
        Filters submenu check marks). Reads the panel so it reflects edits."""
        panel = getattr(self, "_plugin_filter_panel", None)
        if panel is not None:
            return panel.check_state(key)
        return (getattr(self, "_plugin_filter_state", {}) or {}).get(key, 0)

    def _on_quick_plugin_filter(self, key: str, state: int):
        """Apply a plugin status filter chosen from the column menu by driving
        the Filters panel checkbox, so the panel and menu stay in sync and the
        normal filter pipeline (footer-button tint included) runs."""
        panel = getattr(self, "_plugin_filter_panel", None)
        if panel is not None:
            panel.set_check(key, state)   # emits changed -> _on_plugin_filter_changed
        else:   # panel not built yet - fall back to the state dict directly
            self._plugin_filter_state[key] = state
            self._apply_plugin_filters()

    def _on_plugin_layout_changed(self, *_a):
        if not hasattr(self, "_plugin_view"):
            return
        self._apply_plugin_search()
        self._apply_plugin_filters()

    def _apply_plugin_filters(self):
        """Push the filter-hidden row set to the plugin view (composes with the
        plugin search via the view's search/filter union)."""
        if not hasattr(self, "_plugin_view"):
            return
        from gui_qt.modlist_filter import plugin_filter_hidden_rows
        state = getattr(self, "_plugin_filter_state", None) or {}
        disabled_mf = self._disabled_plugin_files()
        hide = plugin_filter_hidden_rows(self._plugin_model._rows, state,
                                         disabled_mf=disabled_mf)
        self._plugin_view.set_filter_hidden(hide)

    def _disabled_plugin_files(self) -> set:
        """Plugin filenames (lower) disabled via the Mod Files tab, for the
        Plugins-tab 'Disabled plugins' filter union."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        try:
            from Utils.disabled_plugins import disabled_plugin_files
            return disabled_plugin_files(pdir, self._gs.game)
        except Exception:
            return set()

    def _sync_text_files_filter_list(self, *, force: bool = False):
        # force: the header filter menu needs the list even while the panel is
        # closed (it reads/drives the panel's checkboxes).
        if not force and not self._text_files_filter_panel.isVisible():
            return
        self._text_files_filter_panel.set_dynamic_items(
            "filetypes", self._text_files_view.filetype_items())

    def _build_saves_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.saves_view import SavesView
        panel = FilterSidePanel(SavesView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_saves_filter_changed)
        panel.close_requested.connect(self._toggle_saves_filters)
        self._saves_view.filetypes_changed.connect(self._sync_saves_filter_list)
        return panel

    def _toggle_saves_filters(self):
        panel = self._saves_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_saves_filter_list()
        else:
            panel.setVisible(False)

    def _on_saves_filter_changed(self, state: dict):
        self._saves_view.apply_filter_state(state)
        self._sync_filters_btn("_saves_filters_btn")

    def _sync_saves_filter_list(self, *, force: bool = False):
        if not force and not self._saves_filter_panel.isVisible():
            return
        self._saves_filter_panel.set_dynamic_items(
            "filetypes", self._saves_view.filetype_items())

    def _build_downloads_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        panel = FilterSidePanel(self._downloads_view.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_downloads_filter_changed)
        panel.close_requested.connect(self._toggle_downloads_filters)
        self._downloads_view.filetypes_changed.connect(
            self._sync_downloads_filter_list)
        return panel

    def _toggle_downloads_filters(self):
        panel = self._downloads_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._data_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._saves_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_downloads_filter_list()
        else:
            panel.setVisible(False)

    def _on_downloads_filter_changed(self, state: dict):
        self._downloads_view.apply_filter_state(state)
        self._sync_filters_btn("_dl_filters_btn")

    def _sync_downloads_filter_list(self, *, force: bool = False):
        if not force and not self._downloads_filter_panel.isVisible():
            return
        self._downloads_filter_panel.set_dynamic_items(
            "filetypes", self._downloads_view.filetype_items())
        self._downloads_filter_panel.set_dynamic_items(
            "locations", self._downloads_view.location_items())

    def _build_data_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.data_view import DataView
        panel = FilterSidePanel(DataView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_data_filter_changed)
        panel.close_requested.connect(self._toggle_data_filters)
        self._data_view.filetypes_changed.connect(self._sync_data_filter_list)
        return panel

    def _toggle_data_filters(self):
        """Open/close the Data filter panel in the window-left slot (does NOT hide
        the plugins column - the Data tree lives there)."""
        panel = self._data_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)
            self._mod_files_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._saves_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_data_filter_list()
        else:
            panel.setVisible(False)

    def _on_data_filter_changed(self, state: dict):
        self._data_view.apply_filter_state(state)
        self._sync_filters_btn("_data_filters_btn")

    def _sync_data_filter_list(self, *, force: bool = False):
        if not force and not self._data_filter_panel.isVisible():
            return
        self._data_filter_panel.set_dynamic_items(
            "filetypes", self._data_view.filetype_items())

    def _build_mod_files_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.mod_files_view import ModFilesView
        panel = FilterSidePanel(ModFilesView.filter_spec(), title=self.tr("Filters"))
        panel.changed.connect(self._on_mod_files_filter_changed)
        panel.close_requested.connect(self._toggle_mod_files_filters)
        # Keep the panel's file-type list + Pack/Unpack enablement in sync.
        self._mod_files_view.filetypes_changed.connect(
            self._sync_mod_files_filter_list)
        self._mod_files_view.mod_changed.connect(
            lambda _n: self._update_mf_footer_buttons())
        return panel

    def _toggle_mod_files_filters(self):
        """Open/close the Mod Files filter panel in the window-left slot. Unlike
        the modlist filter, this does NOT hide the plugins column - the Mod Files
        tree lives there, so hiding it would hide what you're filtering."""
        panel = self._mod_files_filter_panel
        show = not panel.isVisible()
        if show:
            self._modlist_filter_panel.setVisible(False)  # share the slot
            self._data_filter_panel.setVisible(False)
            self._downloads_filter_panel.setVisible(False)
            self._text_files_filter_panel.setVisible(False)
            self._saves_filter_panel.setVisible(False)
            self._plugin_filter_panel.setVisible(False)
            panel.setVisible(True)
            self._sync_mod_files_filter_list()
        else:
            panel.setVisible(False)

    def _on_mod_files_filter_changed(self, state: dict):
        self._mod_files_view.apply_filter_state(state)
        self._sync_filters_btn("_mf_filters_btn")

    def _sync_mod_files_filter_list(self, *, force: bool = False):
        if not force and not self._mod_files_filter_panel.isVisible():
            return
        self._mod_files_filter_panel.set_dynamic_items(
            "filetypes", self._mod_files_view.filetype_items())

    def _update_mf_footer_buttons(self):
        import Utils.bsa_pack_ops as ops
        pack_btn = getattr(self, "_mf_pack_btn", None)
        unpack_btn = getattr(self, "_mf_unpack_btn", None)
        if pack_btn is None or unpack_btn is None:
            return
        mv = self._mod_files_view
        # Reset first - it applies to every game, including ones we can't
        # pack for (which return early below).
        reset_btn = getattr(self, "_mf_reset_btn", None)
        if reset_btn is not None:
            reset_btn.setEnabled(mv.has_mod() and mv.has_changes())
        kind = ops.archive_kind_for_game(getattr(mv, "game", None))
        # Hide both buttons entirely on games we can't pack for (Tk parity).
        if kind is None:
            pack_btn.setVisible(False)
            unpack_btn.setVisible(False)
            return
        upper = kind.upper()
        pack_btn.setVisible(True)
        unpack_btn.setVisible(True)
        pack_btn.setText(self.tr("Pack {0}").format(upper))
        unpack_btn.setText(self.tr("Unpack {0}").format(upper))
        # Pack: any normal mod. Unpack: also needs a matching archive on disk.
        is_normal = mv.has_mod() and ops.is_packable_mod(getattr(mv, "_mod_name", None))
        pack_btn.setEnabled(is_normal)
        has_archive = False
        if is_normal:
            mod_dir = self._bsa_mod_dir()
            has_archive = mod_dir is not None and ops.mod_has_archive(mod_dir, kind)
        unpack_btn.setEnabled(has_archive)

    def _on_mf_expand_clicked(self):
        expanded = self._mod_files_view._toggle_expand_all()
        self._mf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))

    def _on_mf_reset_clicked(self):
        """Undo every Mod Files edit for the shown mod (files are untouched)."""
        mv = self._mod_files_view
        mod_name = getattr(mv, "_mod_name", None)
        if not mod_name or not mv.has_changes():
            return

        def _confirmed(ok):
            if not ok:
                return
            if getattr(mv, "_mod_name", None) != mod_name:
                return   # selection moved on while the confirm was up
            if mv.reset_mod():
                self._notify(
                    self.tr("Reset Mod Files changes for {0}").format(mod_name),
                    "info")
                self._update_mf_footer_buttons()

        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Reset Mod Files changes"),
            self.tr("Undo every Mod Files change for \"{0}\" - Top Level "
                    "promotions, Root folder tags and disabled files? The "
                    "mod's own files are not touched.").format(mod_name),
            _confirmed, confirm_label=self.tr("Reset"))

    # -- BSA / BA2 pack + unpack -------------------------------------------
    def _bsa_mod_dir(self):
        """Resolve the on-disk staging folder for the Mod Files tab's mod."""
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        if game is None or not mod_name:
            return None
        try:
            base = Path(game.get_effective_mod_staging_path())
        except Exception:
            return None
        d = base / mod_name
        return d if d.is_dir() else None

    def _bsa_plugin_exts(self):
        game = getattr(self._mod_files_view, "game", None)
        return getattr(game, "plugin_extensions", None) or (".esp", ".esm", ".esl")

    def _on_pack_bsa(self):
        import Utils.bsa_pack_ops as ops
        from gui_qt.bsa_pack_overlay import BsaPackOverlay

        if self._bsa_op_running:
            self._notify(self.tr("An archive operation is already running."), "warning")
            return
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        profile_dir = getattr(mv, "profile_dir", None)
        kind = ops.archive_kind_for_game(game)
        if kind is None or not ops.is_packable_mod(mod_name):
            return
        mod_dir = self._bsa_mod_dir()
        if mod_dir is None:
            self._notify(self.tr("Mod folder not found."), "warning")
            return
        if ops.is_profile_deployed(game, profile_dir):
            self._notify(
                self.tr("Profile is deployed - run Restore first, then pack the "
                "{0}.").format(kind.upper()), "warning")
            return

        plan = ops.plan_pack(game, mod_dir, mod_name, kind, self._bsa_plugin_exts())

        def on_done(opts):
            if opts is None:
                return
            self._start_pack_bsa(plan, opts)

        BsaPackOverlay.show_over(
            self, archive_name=plan.archive_path.name,
            existing=plan.existing_any, kind=kind, on_done=on_done)

    def _start_pack_bsa(self, plan, opts):
        import threading
        import Utils.bsa_pack_ops as ops
        from gui_qt.safe_emit import safe_emit

        mv = self._mod_files_view
        profile_dir = getattr(mv, "profile_dir", None)
        index_path = getattr(mv, "index_path", None)
        mod_name = plan.mod_name
        delete_loose = bool(opts.get("delete_loose"))
        split_textures = bool(opts.get("split_textures"))
        skip_winners = bool(opts.get("skip_winners"))

        excluded = ops.read_excluded_for_mod(profile_dir, mod_name)
        if skip_winners:
            # compute_skip_winners answers in index-key space; the archive
            # writers walk the mod folder, so translate back to raw paths or
            # the winners silently pack on any mod with a Top Level strip.
            import Utils.mod_files as _mf
            from Utils.profile_state import read_mod_strip_prefixes
            game = getattr(mv, "game", None)
            winners = ops.compute_skip_winners(
                index_path, profile_dir, mod_name,
                root_ctx=_mf.conflict_root_context(game, profile_dir))
            excluded |= _mf.index_keys_to_raw(
                self._bsa_mod_dir(), mod_name, winners,
                getattr(game, "mod_folder_strip_prefixes", None),
                read_mod_strip_prefixes(profile_dir) if profile_dir else None)
        excluded_now = frozenset(excluded)

        self._bsa_op_running = True
        self._op_title = f"Pack {plan.kind.upper()}"
        self._ensure_feedback()
        self._notify(self.tr("Packing {0}…").format(mod_name), "info")

        def worker():
            def progress(done, total, current):
                safe_emit(self._op_progress, done, total,
                          f"{done} / {total}  -  {current[-50:]}")
            try:
                res = ops.run_pack(
                    plan, excluded_keys=excluded_now,
                    split_textures=split_textures,
                    progress=progress, cancel=None)
            except ops.PackCancelled:
                safe_emit(self._bsa_op_done, {"kind": "pack", "cancelled": True})
                return
            except Exception as exc:
                safe_emit(self._bsa_op_done,
                          {"kind": "pack", "error": str(exc)})
                return
            safe_emit(self._bsa_op_done, {
                "kind": "pack", "plan": plan, "result": res,
                "delete_loose": delete_loose, "mod_name": mod_name,
            })

        threading.Thread(target=worker, daemon=True).start()

    def _on_unpack_bsa(self):
        import Utils.bsa_pack_ops as ops
        from gui_qt.bsa_unpack_overlay import BsaUnpackOverlay

        if self._bsa_op_running:
            self._notify(self.tr("An archive operation is already running."), "warning")
            return
        mv = self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        profile_dir = getattr(mv, "profile_dir", None)
        if ops.archive_kind_for_game(game) is None or not ops.is_packable_mod(mod_name):
            return
        mod_dir = self._bsa_mod_dir()
        if mod_dir is None:
            return
        if ops.is_profile_deployed(game, profile_dir):
            self._notify(
                self.tr("Profile is deployed - run Restore first, then unpack."),
                "warning")
            return

        def on_done(archives):
            if not archives:
                return
            self._start_unpack_bsa(mod_dir, archives)

        BsaUnpackOverlay.show_over(
            self, mod_name=mod_name, mod_dir=mod_dir,
            plugin_exts=self._bsa_plugin_exts(), on_done=on_done)

    def _start_unpack_bsa(self, mod_dir, archive_paths):
        import threading
        import Utils.bsa_pack_ops as ops
        from gui_qt.safe_emit import safe_emit

        mod_name = getattr(self._mod_files_view, "_mod_name", None)
        kind_upper = ops.unpack_kind_label(archive_paths)

        self._bsa_op_running = True
        self._op_title = f"Unpack {kind_upper}"
        self._ensure_feedback()
        self._notify(self.tr("Unpacking {0} archive(s)…").format(len(archive_paths)), "info")

        def worker():
            def progress(done, total, current):
                safe_emit(self._op_progress, done, total,
                          f"{done} / {total}  -  {current[-50:]}")
            try:
                count, written = ops.run_unpack(
                    archive_paths, mod_dir, progress=progress, cancel=None)
            except ops.UnpackCancelled:
                safe_emit(self._bsa_op_done, {"kind": "unpack", "cancelled": True})
                return
            except Exception as exc:
                safe_emit(self._bsa_op_done,
                          {"kind": "unpack", "error": str(exc)})
                return
            safe_emit(self._bsa_op_done, {
                "kind": "unpack", "mod_dir": mod_dir, "mod_name": mod_name,
                "archives": archive_paths, "count": count, "written": written,
            })

        threading.Thread(target=worker, daemon=True).start()

    def _on_bsa_op_done(self, info: dict):
        """UI-thread completion for pack/unpack workers (see _bsa_op_done)."""
        import Utils.bsa_pack_ops as ops

        self._bsa_op_running = False
        if self._progress_popup is not None:
            self._schedule_op_clear(800)

        if info.get("cancelled"):
            self._notify(self.tr("Cancelled."), "info")
            return
        err = info.get("error")
        if err:
            verb = "Pack" if info["kind"] == "pack" else "Unpack"
            self._notify(self.tr("{0} failed: {1}").format(verb, err), "error")
            return

        mv = self._mod_files_view
        profile_dir = getattr(mv, "profile_dir", None)
        mod_name = info.get("mod_name")

        if info["kind"] == "pack":
            plan = info["plan"]
            res = info["result"]
            if info["delete_loose"]:
                deleted = ops.delete_loose_files(plan.mod_dir, res.packed_keys)
                tail = f" ({deleted} loose file(s) deleted)" if deleted else ""
            else:
                ops.auto_disable_packed_files(profile_dir, mod_name, res.packed_keys)
                tail = " (packed files disabled)"
            parts = []
            if res.main_count:
                parts.append(f"{res.main_count} → {plan.archive_path.name}")
            if res.tex_count and plan.archive_textures_path is not None:
                parts.append(f"{res.tex_count} → {plan.archive_textures_path.name}")
            summary = "; ".join(parts) or "no files packed"
            stub = " + stub .esp" if plan.stub_plugin_path is not None else ""
            self._notify(self.tr("Packed {0}{1}{2}").format(summary, stub, tail), "success")
        else:  # unpack
            for ap in info["archives"]:
                try:
                    ap.unlink()
                except OSError:
                    pass
            stem = ops.shared_archive_stem(info["archives"])
            stub, is_ours = ops.stub_for_unpack(info["mod_dir"], stem)
            if is_ours:
                try:
                    stub.unlink()
                except OSError:
                    pass
            ops.clear_excluded_for_unpack(profile_dir, mod_name, info["written"])
            self._notify(
                self.tr("Unpacked {0} file(s) from "
                "{1} archive(s)").format(info['count'], len(info['archives'])), "success")

        # Rebuild conflicts/index (Qt equivalent of the Tk filemap rebuild) and
        # refresh the Mod Files tree so the new state shows.
        self._rebuild_conflicts_async(rescan_index=True)
        try:
            if mod_name is not None:
                mv.show_mod(mod_name)
        except Exception:
            pass
        self._update_mf_footer_buttons()

    def _build_modlist_filter_panel(self):
        from gui_qt.filter_panel import FilterSidePanel
        from gui_qt.modlist_filter import STATUS_FILTERS

        # Filters whose backing data the Qt side doesn't build yet - shown but
        # disabled (greyed) so the panel is complete and they light up later.
        # (FOMOD/BAIN come from meta.is_fomod/is_bain; conflicts, plugins, BSA,
        #  PBR, updates, categories, file types are all wired.)
        _UNWIRED = set()
        items = [(key, label, key not in _UNWIRED)
                 for key, label in STATUS_FILTERS]
        spec = [
            {"title": "By status", "type": "checks", "items": items},
            {"title": "By category", "type": "dynamic", "id": "categories"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
            {"title": "By author", "type": "dynamic", "id": "authors"},
        ]
        panel = FilterSidePanel(spec, title=self.tr("Filters"))
        panel.changed.connect(self._on_modlist_filter_changed)
        panel.close_requested.connect(self._toggle_modlist_filters)
        self._modlist_filter_state: dict = {}
        self._modlist_filter_data = None
        return panel

    def _toggle_modlist_filters(self):
        """Show/hide the modlist filter side panel (the Filters footer button).

        Mirrors Tk: opening the modlist filter auto-hides the plugins panel so
        the filter takes its space; closing restores the plugins panel only if
        it was visible when we opened."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        show = not panel.isVisible()
        right = getattr(self, "_right_col", None)
        if show:
            # The Mod Files / Data filters share this slot - close them first.
            mfp = getattr(self, "_mod_files_filter_panel", None)
            if mfp is not None and mfp.isVisible():
                self._toggle_mod_files_filters()
            dfp = getattr(self, "_data_filter_panel", None)
            if dfp is not None and dfp.isVisible():
                self._toggle_data_filters()
            dlfp = getattr(self, "_downloads_filter_panel", None)
            if dlfp is not None and dlfp.isVisible():
                self._toggle_downloads_filters()
            tffp = getattr(self, "_text_files_filter_panel", None)
            if tffp is not None and tffp.isVisible():
                self._toggle_text_files_filters()
            svfp = getattr(self, "_saves_filter_panel", None)
            if svfp is not None and svfp.isVisible():
                self._toggle_saves_filters()
            self._filter_plugins_was_visible = bool(
                right is not None and right.isVisible())
            panel.setVisible(True)
            if right is not None and self._filter_plugins_was_visible:
                right.setVisible(False)
            self._rebuild_filter_data()
        else:
            panel.setVisible(False)
            if right is not None and getattr(
                    self, "_filter_plugins_was_visible", False):
                right.setVisible(True)
            self._filter_plugins_was_visible = False

    # ---- footer button handlers -----------------------------------------
    def _on_toggle_collapse_all(self):
        """Expand all / Collapse all separators (toggles based on current state)."""
        m = self._modlist_model
        if not m.collapsible_separator_names():
            return
        collapse = not m.any_collapsed()   # if any expanded → collapse all
        self._modlist_view.set_all_collapsed(collapse)
        self._refresh_footer_toggle_labels()

    def _on_toggle_enable_all(self):
        """Enable all / Disable all toggleable mods."""
        m = self._modlist_model
        enable = not m.all_mods_enabled()
        m.set_all_enabled(enable)
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("All mods enabled") if enable else self.tr("All mods disabled"),
                     "info")

    def _refresh_footer_toggle_labels(self):
        """Keep the Expand/Collapse all + Enable/Disable all button text in sync
        with the list state (Tk parity)."""
        m = self._modlist_model
        eb = getattr(self, "_expand_all_btn", None)
        if eb is not None:
            has_seps = bool(m.collapsible_separator_names())
            eb.setText(self.tr("Expand all") if (not has_seps or m.any_collapsed())
                       else self.tr("Collapse all"))
        nb = getattr(self, "_enable_all_btn", None)
        if nb is not None:
            nb.setText(self.tr("Disable all") if m.all_mods_enabled() else self.tr("Enable all"))

    def _on_refresh_modlist(self):
        """Refresh: re-sync the mods folder, reload the modlist + plugins, and
        force a full index rescan (picks up files added/removed inside mods)."""
        from Utils.modlist import sync_modlist_with_mods_folder
        self._reassert_profile_paths()
        # Refresh doubles as the user's "reconcile my group now" button; must
        # run before the folder sync (which drops entries with missing dirs).
        try:
            from Utils.profile_groups import materialize_if_group
            materialize_if_group(self._gs.game, self._gs.profile_dir(),
                                 log_fn=self._append_log)
        except Exception as exc:
            print(f"[gui_qt] group reconcile on refresh failed: {exc}", flush=True)
        ml = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        if ml is not None and staging is not None:
            try:
                sync_modlist_with_mods_folder(ml, staging)
            except Exception as exc:
                print(f"[gui_qt] modlist sync failed: {exc}", flush=True)
        # Arm the mass phantom-prune offer: if the reload finds more stale
        # plugins.txt entries than the automatic prune's cap, an explicit
        # refresh asks the user instead of silently keeping them forever
        # (see _on_plugins_loaded / plugin_state SAFETY 3).
        self._offer_mass_prune = True
        self._reload_modlist(rescan_index=True, preserve_overlays=True)
        self._reload_plugins()
        self._refresh_footer_toggle_labels()
        self._notify(self.tr("Modlist refreshed"), "info")

    # ---- search boxes ----------------------------------------------------
    def _on_modlist_search(self, text: str):
        """Modlist search: hide rows whose mod name (or owning separator block)
        doesn't contain the query. Debounced to coalesce fast typing."""
        self._modlist_search_text = text
        t = getattr(self, "_modlist_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_modlist_search)
            self._modlist_search_timer = t
        t.start()

    def _modlist_search_tooltip(self) -> str:
        """Rich-text tooltip for the search icon: the available `!` filter tags
        plus the filetype/category forms. Grouped for readability."""
        rows = [
            (self.tr("Updates"),        "!update"),
            (self.tr("Winning conflicts"),         "!winning"),
            (self.tr("Losing conflicts"),          "!losing"),
            (self.tr("Winning & losing"),          "!partial"),
            (self.tr("Fully conflicted"),          "!full"),
            (self.tr("FOMOD installs"),            "!fomod"),
            (self.tr("BAIN installs"),             "!bain"),
            (self.tr("Missing requirements"),      "!missing"),
            (self.tr("Has notes"),                 "!notes"),
            (self.tr("Has plugins"),               "!plugins"),
            (self.tr("Has BSA/BA2 archives"),      "!bsa"),
            (self.tr("PGPatcher textures"),        "!pbr"),
            (self.tr("Enabled / disabled"),        "!enabled · !disabled"),
            (self.tr("By file type"),              "!.dds"),
            (self.tr("By category"),               "!patches"),
        ]
        items = "".join(
            f"<tr><td style='padding-right:10px'><code>{tag}</code></td>"
            f"<td>{label}</td></tr>"
            for label, tag in rows)
        head = self.tr("Filter the modlist with search tags "
                       "(combine them, and with text):")
        return f"<b>{head}</b><table>{items}</table>"

    def _modlist_token_search_active(self) -> bool:
        """True when the search box holds a `!token` filter - those key off the
        live FilterData, so callers must rebuild/reapply it like a panel filter."""
        return "!" in (getattr(self, "_modlist_search_text", "") or "")

    def _apply_modlist_search(self):
        from gui_qt.modlist_filter import search_hidden_rows
        text = getattr(self, "_modlist_search_text", "")
        entries = self._modlist_model._entries
        active = bool((text or "").strip())
        data = getattr(self, "_modlist_filter_data", None)
        self._modlist_view.set_search_hidden(
            search_hidden_rows(entries, text, data), active=active)

    def _on_plugin_search(self, text: str):
        """Plugins search: hide plugins whose name (or owning mod name) doesn't
        contain the query."""
        self._plugin_search_text = text
        t = getattr(self, "_plugin_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(120)
            t.timeout.connect(self._apply_plugin_search)
            self._plugin_search_timer = t
        t.start()

    def _apply_plugin_search(self):
        from gui_qt.modlist_filter import plugin_search_hidden_rows
        text = getattr(self, "_plugin_search_text", "")
        rows = self._plugin_model._rows
        owner = (self._conflict_data.plugin_owner
                 if getattr(self, "_conflict_data", None) else {})
        self._plugin_view.set_search_hidden(
            plugin_search_hidden_rows(rows, text, owner))

    def _on_modlist_filter_changed(self, state: dict):
        self._modlist_filter_state = state
        self._apply_modlist_filters()
        self._update_filters_btn_active()

    def _quick_modlist_filter_state(self, key: str) -> int:
        """Read the current tri-state for a status filter (for the column-menu
        quick-filter check marks). Reads the panel so it reflects panel edits."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            return panel.check_state(key)
        return (getattr(self, "_modlist_filter_state", {}) or {}).get(key, 0)

    def _on_quick_modlist_filter(self, key: str, state: int):
        """Apply a quick filter chosen from the column menu by driving the
        Filters panel checkbox, so the panel and menu stay in sync and the
        normal filter pipeline (footer-button tint included) runs."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is not None:
            panel.set_check(key, state)   # emits changed -> _on_modlist_filter_changed
        else:   # panel not built yet - fall back to the state dict directly
            self._modlist_filter_state[key] = state
            self._apply_modlist_filters()
            self._update_filters_btn_active()

    def _apply_modlist_filters(self):
        from gui_qt.modlist_filter import compute_hidden_rows
        state = getattr(self, "_modlist_filter_state", {}) or {}
        # Flatten the column sort across separator groups while separators are
        # hidden (must run before reading _entries - it may re-derive the sort).
        self._modlist_model.set_separators_hidden(
            state.get("filter_hide_separators") == 1)
        data = getattr(self, "_modlist_filter_data", None)
        if data is None:
            self._modlist_view.set_filter_hidden(set())
            return
        entries = self._modlist_model._entries
        hide = compute_hidden_rows(entries, state, data)
        self._modlist_view.set_filter_hidden(hide)

    def _update_filters_btn_active(self):
        """Tint the modlist Filters footer button when a filter is active."""
        self._sync_filters_btn("_modlist_filters_btn")

    def _sync_filters_btn(self, btn_attr: str):
        """Light a Filters footer button purple while that panel is filtered.

        "Filtered" covers both routes to a narrowed list: a checkbox set in the
        Filters side panel, and a non-empty search box - the button is the only
        persistent hint that the list isn't showing everything, so it has to
        follow both. Re-polish is skipped when the state hasn't changed."""
        btn = getattr(self, btn_attr, None)
        entry = self._FILTER_BTN_SOURCES.get(btn_attr)
        if btn is None or entry is None:
            return
        panel_attr, search_attr = entry
        panel = getattr(self, panel_attr, None)
        search = getattr(self, search_attr, None)
        active = bool(panel is not None and panel.any_active()) or bool(
            search is not None and search.text().strip())
        if bool(btn.property("active")) == active:
            return
        btn.setProperty("active", active)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _rebuild_filter_data(self):
        """(Re)build FilterData: the disk-derived parts (modindex read + BSA /
        plugin folder walks) run on a worker; _on_filter_data_ready assembles
        the FilterData and updates the panel on the UI thread. Runs after every
        conflict rebuild, so the scans must not block the UI."""
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        import threading
        staging = self._gs.staging_dir()
        staging_parent = staging.parent if staging is not None else None
        plugin_exts = getattr(self._gs.game, "plugin_extensions", None)
        self._filter_data_gen += 1
        gen = self._filter_data_gen

        def worker():
            payload = None
            if staging_parent is not None:
                from gui_qt.modlist_filter import (
                    build_index_data, build_mods_with_bsa, build_mods_with_plugins,
                )
                try:
                    counts, mod_ft, pbr = build_index_data(staging_parent)
                    payload = {
                        "filetype_counts": counts,
                        "mod_filetypes": mod_ft,
                        "mods_with_pbr": pbr,
                        "mods_with_bsa": build_mods_with_bsa(staging_parent),
                        "mods_with_plugins": build_mods_with_plugins(
                            staging_parent, plugin_exts),
                    }
                except Exception as exc:
                    print(f"[gui_qt] filter data scan failed: {exc}", flush=True)
            self._filter_data_ready.emit(gen, payload)

        threading.Thread(target=worker, daemon=True,
                         name="filter-data").start()

    def _on_filter_data_ready(self, gen: int, payload):
        """UI thread: merge the worker's disk-derived sets with the in-memory
        meta/conflict state into FilterData, repopulate the panel, reapply."""
        if gen != self._filter_data_gen:
            return   # superseded by a newer rebuild
        panel = getattr(self, "_modlist_filter_panel", None)
        if panel is None:
            return
        from gui_qt.modlist_filter import FilterData
        cd = getattr(self, "_conflict_data", None)

        data = FilterData()
        if cd is not None:
            data.conflict_codes = self._loose_backend_codes(cd)
            data.bsa_conflict_codes = self._bsa_backend_codes(cd)
        data.mods_with_updates = set(getattr(self, "_mod_updates", set()))
        data.missing_reqs = set(getattr(self, "_mod_missing_reqs", set()))
        data.ignored_missing_reqs = set(getattr(self, "_ignored_missing_reqs", frozenset()))
        data.category_names = dict(getattr(self, "_mod_categories", {}))
        data.author_names = dict(getattr(self, "_mod_authors", {}))
        data.fomod_mods = set(getattr(self, "_mod_fomod", set()))
        data.bain_mods = set(getattr(self, "_mod_bain", set()))
        data.modified_mf_mods = self._build_modified_mf_mods()
        data.disabled_plugins_mods = self._build_disabled_plugins_mods()
        data.notes_mods = {m for m, note in self._read_mod_notes().items()
                           if (note or "").strip()}
        # Overlay the "modified in Mod Files" eye flag in the modlist Flags column.
        self._modlist_model.set_modified_mf(data.modified_mf_mods)
        if payload:
            data.filetype_counts = payload["filetype_counts"]
            data.mod_filetypes = payload["mod_filetypes"]
            data.mods_with_pbr = payload["mods_with_pbr"]
            data.mods_with_bsa = payload["mods_with_bsa"]
            data.mods_with_plugins = payload["mods_with_plugins"]
        self._modlist_filter_data = data

        # Repopulate dynamic lists.
        cats = sorted({(c or "") for c in data.category_names.values()} | {""},
                      key=lambda c: ("(Uncategorized)" if c == "" else c).lower())
        panel.set_dynamic_items("categories", [
            (c, "(Uncategorized)" if c == "" else c, None) for c in cats])
        # Authors (Nexus uploader). Only offer names we actually know - an
        # "(Unknown)" bucket for un-stamped mods would just match everything not
        # yet looked up, which isn't a useful filter.
        auths = sorted({a for a in data.author_names.values() if a},
                       key=str.lower)
        panel.set_dynamic_items("authors", [(a, a, None) for a in auths])
        fts = sorted(data.filetype_counts.items(), key=lambda kv: kv[0])
        panel.set_dynamic_items("filetypes", [
            (ext, ext, count) for ext, count in fts])

        # Relabel / re-enable game-specific filters, then reapply.
        self._refresh_filter_game_specific()
        self._apply_modlist_filters()
        # Token searches (e.g. "!update") depend on the FilterData we just
        # (re)built - re-run so results reflect fresh conflict/update data.
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _build_modified_mf_mods(self) -> set:
        """Mods with Mod Files tab modifications - any excluded file, root-tagged
        file, OR a strip prefix (Tk `_mod_is_modified_in_mf`)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        try:
            from Utils.profile_state import (
                read_excluded_mod_files, read_mod_strip_prefixes,
                read_root_mod_files)
            for mod, keys in (read_excluded_mod_files(pdir, None) or {}).items():
                if keys:
                    out.add(mod)
            for mod, keys in (read_root_mod_files(pdir, None) or {}).items():
                if keys:
                    out.add(mod)
            for mod, prefixes in (read_mod_strip_prefixes(pdir, None) or {}).items():
                if any(p for p in prefixes):
                    out.add(mod)
        except Exception:
            pass
        return out

    def _build_rerun_fomod_mods(self) -> dict:
        """Mods that should show the rerun-FOMOD flag, mapped to the reason it
        fired - ``("pending"|"active", [plugin names])`` for the hover tooltip.
        Two conditions, both read from meta.ini for FOMOD mods only (read_meta
        is mtime-cached, so cheap) and evaluated against the currently-enabled
        plugins:

          • `fomodPendingDeps` (UNSELECTED options' deps): flag when an AND-clause
            becomes FULLY present - a patch you skipped is now relevant. On the
            FIRST evaluation after a (re)install (`fomodPendingBaselined` unset)
            clauses that ALREADY hold are pruned instead of fired: the wizard
            offered those patches against this very load order and the user
            declined them - an informed choice, not a change. Only a dep that
            appears AFTER install raises the flag.
          • `fomodActiveDeps` (SELECTED options' deps): flag when an AND-clause is
            NO LONGER fully present - a patch you installed is now orphaned (its
            required mod was removed/disabled). Gated on `fomodActiveDepsSeen`:
            only a clause we have OBSERVED satisfied can go on to be "orphaned".
            A clause that was never satisfied was never a real gate (older
            installers recorded merely-Recommended patterns too), so it can't
            fire the flag on a mod whose dep mod was never installed at all.

        See FLAG_RERUN_FOMOD."""
        if not hasattr(self, "_modlist_model") or not hasattr(self, "_plugin_model"):
            return {}
        staging = self._gs.staging_dir()
        if staging is None:
            return {}
        try:
            enabled = self._plugin_model.enabled_lower()
        except Exception:
            return {}
        # Narrow to FOMOD-installed mods when we know them (avoids reading meta for
        # every mod); fall back to all mods if the set isn't populated yet.
        candidates = getattr(self, "_mod_fomod", None) or self._modlist_model.mod_names()
        # A DISABLED mod isn't deployed, so "rerun to fix it" is meaningless - and
        # its own plugins leave the load order when disabled, which would break its
        # self-referential fomodActiveDeps and falsely raise the flag. Skip them.
        try:
            enabled_mods = self._modlist_model.enabled_mod_names()
            candidates = [m for m in candidates if m in enabled_mods]
        except Exception:
            pass
        out: dict[str, tuple[str, list[str]]] = {}
        try:
            from Nexus.nexus_meta import read_meta
        except Exception:
            return {}

        # Clause parsing/evaluation shared with the encode side
        # (fomod_installer) so the format and its evaluation can never drift.
        from Utils.fomod_installer import (
            FLAG_OPT_SEP, iter_option_conditions, option_met,
            option_has_present_member, satisfied_present_members,
            missing_present_members, prune_satisfied_conditions)

        for name in candidates:
            meta_path = staging / name / "meta.ini"
            try:
                meta = read_meta(meta_path)
            except Exception:
                continue
            # Baseline: first evaluation after a (re)install prunes clauses that
            # already hold - the wizard offered those patches against this load
            # order and the user declined them (see docstring).
            pending_raw = getattr(meta, "fomod_pending_deps", "") or ""
            if pending_raw and not getattr(meta, "fomod_pending_baselined",
                                           False):
                pending_raw = prune_satisfied_conditions(pending_raw, enabled)
                self._persist_pending_baseline(meta_path, pending_raw)
            # Pending: an UNSELECTED option's condition is now satisfiable → rerun
            # to pick up the newly-relevant patch. Require at least one PRESENT
            # member across the option (a non-">" literal) so a condition that only
            # needs something ABSENT - true almost always - doesn't fire constantly.
            triggers: list[str] = []
            for _cond, alts in iter_option_conditions(pending_raw):
                if option_met(alts, enabled) and option_has_present_member(alts):
                    for m in satisfied_present_members(alts, enabled):
                        if m not in triggers:
                            triggers.append(m)
            if triggers:
                out[name] = ("pending", triggers)
                continue
            # Active: a SELECTED option's condition is no longer satisfied (NONE of
            # its OR alternatives hold) → rerun to drop the now-orphaned patch. For
            # an OR option, having ONE of its plugins is still valid - don't fire.
            # Only clauses recorded in fomodActiveDepsSeen - ones we have actually
            # watched hold - can be "no longer" satisfied; anything else was never
            # a gate, so its absence is the status quo, not a change.
            active_raw = getattr(meta, "fomod_active_deps", "") or ""
            if not active_raw:
                continue
            seen_list = [c.strip() for c in (
                getattr(meta, "fomod_active_deps_seen", "") or ""
            ).split(FLAG_OPT_SEP) if c.strip()]
            seen = {c.lower() for c in seen_list}
            fire = False
            missing: list[str] = []
            newly_seen = False
            for cond, alts in iter_option_conditions(active_raw):
                if option_met(alts, enabled):
                    if cond.lower() not in seen:
                        seen.add(cond.lower())
                        seen_list.append(cond)
                        newly_seen = True
                elif cond.lower() in seen:
                    fire = True
                    for m in missing_present_members(alts, enabled):
                        if m not in missing:
                            missing.append(m)
            if newly_seen:
                self._persist_active_deps_seen(meta_path,
                                               FLAG_OPT_SEP.join(seen_list))
            if fire:
                out[name] = ("active", missing)
        return out

    def _persist_pending_baseline(self, meta_path, pruned: str) -> None:
        """Store the install-time-pruned fomodPendingDeps and mark the mod
        baselined (see _build_rerun_fomod_mods). One write for both keys."""
        try:
            from Nexus.nexus_meta import set_meta_keys
            set_meta_keys(meta_path, {
                "fomodPendingDeps": pruned or None,   # None removes the key
                "fomodPendingBaselined": "true",
            })
        except Exception:
            pass

    def _persist_active_deps_seen(self, meta_path, value: str) -> None:
        """Record which fomodActiveDeps clauses have been observed satisfied."""
        try:
            from Nexus.nexus_meta import set_meta_key
            set_meta_key(meta_path, "fomodActiveDepsSeen", value)
        except Exception:
            pass

    def _refresh_rerun_fomod_flags(self) -> None:
        """Recompute + push the rerun-FOMOD overlay onto the modlist model."""
        if hasattr(self, "_modlist_model"):
            self._modlist_model.set_rerun_fomod_mods(self._build_rerun_fomod_mods())

    def _build_disabled_plugins_mods(self) -> set:
        """Mods that own at least one disabled plugin - union of plugins disabled
        in plugins.txt and plugins disabled via the Mod Files tab (Tk-parity
        'Mods with disabled plugins' filter)."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return set()
        out: set[str] = set()
        # Mod Files "Disable" checkbox on a plugin file.
        try:
            from Utils.disabled_plugins import mods_with_disabled_plugins
            out |= mods_with_disabled_plugins(pdir, self._gs.game)
        except Exception:
            pass
        # plugins.txt-disabled plugins → their owning mod (via filemap owner map).
        try:
            cd = getattr(self, "_conflict_data", None)
            owner = dict(getattr(cd, "plugin_owner", {}) or {}) if cd else {}
            for r in getattr(self._plugin_model, "_rows", []) or []:
                if getattr(r, "vanilla", False) or getattr(r, "enabled", True):
                    continue
                mod = owner.get(getattr(r, "name", "").lower())
                if mod:
                    out.add(mod)
        except Exception:
            pass
        return out

    def _refresh_filter_game_specific(self):
        """Relabel BSA→BA2 + enable/disable PGPatcher per the active game."""
        panel = getattr(self, "_modlist_filter_panel", None)
        g = self._gs.game
        if panel is None:
            return
        archive_exts = getattr(g, "archive_extensions", None) if g else None
        if archive_exts and ".ba2" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BA2 archives"))
        elif archive_exts and ".pak" in archive_exts:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with PAK archives"))
        else:
            panel.set_check_label("filter_has_bsa", self.tr("Mods with BSA archives"))
        # PGPatcher (PBR) is Skyrim SE only.
        is_sse = bool(g and getattr(g, "nexus_game_domain", "")
                      == "skyrimspecialedition")
        panel.set_check_enabled("filter_has_pbr", is_sse)

    def _loose_backend_codes(self, cd) -> dict:
        """Map the display loose_codes back to backend CONFLICT_* codes the
        engine filters on (DISP 1/-1/2/3 → WINS/LOSES/PARTIAL/FULL)."""
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL)
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES,
             2: CONFLICT_PARTIAL, 3: CONFLICT_FULL}
        return {n: m[c] for n, c in (cd.loose_codes or {}).items() if c in m}

    def _bsa_backend_codes(self, cd) -> dict:
        """BSA codes from ConflictData are 1/-1/2/3 (win/lose/mixed/full).
        Map to backend codes for the engine (mixed→PARTIAL)."""
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL)
        m = {1: CONFLICT_WINS, -1: CONFLICT_LOSES, 2: CONFLICT_PARTIAL,
             3: CONFLICT_FULL}
        return {n: m[c] for n, c in (cd.bsa_codes or {}).items() if c in m}

    def _reassert_profile_paths(self):
        """Force game._active_profile_dir back in sync with GameState before
        resolving profile-derived paths (modlist/plugins/staging). Background
        workers (deploy pipeline, collection install/cancel-cleanup) swap the
        game object's active dir and can leave it stale or None - after which
        get_effective_mod_staging_path() silently falls back to the SHARED
        mods folder for profile-specific-mods profiles, and reload/sync/prune
        paths read (and destructively write) against the wrong profile.
        Skipped while a worker that legitimately owns that state is running -
        it re-establishes the right dir itself when it finishes. (getattr:
        the first _reload_modlist runs before the collection state is set.)"""
        if (getattr(self, "_deploy_running", False)
                or getattr(self, "_col_install_running", False)):
            return
        self._gs.reassert_active_profile()

    def _reload_modlist(self, rescan_index: bool = False,
                        preserve_overlays: bool = False):
        """Load the active game/profile's modlist + metadata into the model.
        rescan_index=True forces the conflict rebuild to rescan the index from
        disk (Refresh button).

        preserve_overlays=True keeps the current flag/conflict icons on screen
        until the async re-read lands (Refresh of the SAME game/profile - the
        overlays are keyed by mod name so they still render correctly). Leave it
        False for a game/profile switch, where the old overlays are stale and
        must be cleared immediately."""
        from Utils.modlist import read_modlist
        from gui_qt.modlist_data import read_meta_for_entries
        from Utils.perftrace import span

        self._reassert_profile_paths()

        ml_path = self._gs.modlist_path()
        staging = self._gs.staging_dir()
        with span("reload_modlist.read_modlist"):
            entries = (read_modlist(ml_path)
                       if (ml_path and ml_path.is_file()) else [])
        # Non-empty ⇒ this reload ends in a conflict rebuild (see bottom),
        # whose completion reloads the plugin panel. The profile-switch path
        # reads this to skip its own (redundant) immediate plugin reload.
        self._reload_had_entries = bool(entries)

        # Preserve the Mod Files tab's shown mod across a same-context refresh
        # (install/toggle/deploy/Refresh of the SAME game+profile): set_entries
        # clears the modlist selection, which would otherwise blank the tab. On a
        # game/profile SWITCH the shown mod may not exist, so we drop it.
        prev_context = (self._gs.game_name, self._gs.profile_dir())
        keep_mod_files = None
        if prev_context == getattr(self, "_modlist_context", None):
            mfv = getattr(self, "_mod_files_view", None)
            if mfv is not None and mfv.has_mod():
                keep_mod_files = mfv._mod_name
        self._modlist_context = prev_context

        self._mod_categories: dict[str, str] = {}
        self._mod_authors: dict[str, str] = {}
        self._mod_updates: set[str] = set()
        self._mod_fomod: set[str] = set()
        self._mod_bain: set[str] = set()
        self._mod_missing_reqs: set[str] = set()
        self._ignored_missing_reqs: frozenset[str] = frozenset()
        if entries and staging is not None:
            pdir = self._gs.profile_dir()
            if pdir is not None:
                try:
                    from Utils.profile_state import read_ignored_missing_requirements
                    self._ignored_missing_reqs = frozenset(
                        read_ignored_missing_requirements(pdir))
                except Exception:
                    self._ignored_missing_reqs = frozenset()

        # Entries go up immediately with blank meta so a game/profile switch
        # never blocks on disk; the per-mod meta.ini reads (one file per mod)
        # run on a worker → _modlist_meta_ready fills the columns/flags in.
        # The gen bump also drops a stale in-flight read on switch.
        self._modlist_meta_gen += 1
        meta_gen = self._modlist_meta_gen
        if not preserve_overlays:
            self._modlist_model._versions = {}
            self._modlist_model._installed = {}
            self._modlist_model._categories = {}
        with span("reload_modlist.set_entries"):
            self._modlist_model.set_entries(entries)
        if not preserve_overlays:
            self._modlist_model.set_flags({})
        with span("reload_modlist.read_notes"):
            self._modlist_model.set_notes(self._read_mod_notes())
        # (The meta worker itself starts at the END of this reload - see
        # below - so its disk work doesn't contend with the sync UI section.)
        if not preserve_overlays:
            self._modlist_model.set_conflicts({}, {}, {})   # clear stale; recomputed async
        # Persist edits back to this modlist; rebuild conflicts after each save
        # (pure reorders that can't change the filemap skip the rebuild).
        self._modlist_model.modlist_path = ml_path
        self._modlist_model.on_saved = self._on_modlist_saved
        self._modlist_view.staging_dir = staging
        self._modlist_view.profile_dir = self._gs.profile_dir()
        self._modlist_view.game = self._gs.game
        # Right-click "Check Updates" reaches the window through this callback.
        self._modlist_view.on_check_updates = self._on_check_updates
        # Change Version: right-click item + clicking the update flag icon.
        self._modlist_view.on_change_version = self._open_change_version_tab
        self._modlist_view.on_bundle_options = self._open_bundle_tab
        # Thunderstore Actions submenu (its own store, so its own callbacks).
        self._modlist_view.on_thunderstore_change_version = (
            self._open_thunderstore_version_tab)
        self._modlist_view.on_thunderstore_check_updates = (
            self._check_thunderstore_updates)
        self._modlist_view.on_flag_clicked = self._on_modlist_flag_clicked
        # Missing Requirements: right-click item + clicking the ⚠ flag icon.
        self._modlist_view.on_missing_reqs = self._open_missing_reqs_tab
        # View Requirements: right-click item (offline, reads meta.ini data).
        self._modlist_view.on_view_requirements = self._open_view_requirements_tab
        # Quick Update: right-click on update-flagged mods (premium direct DL).
        self._modlist_view.on_quick_update = self._quick_update_mods
        # Reinstall: use a retained archive, or redownload from Nexus/Thunderstore.
        self._modlist_view.on_reinstall = self._reinstall_mods
        # Show Conflicts: right-click item.
        self._modlist_view.on_show_conflicts = self._open_show_conflicts_tab
        # Open in NIF Viewer: right-click item on a Bethesda mod with meshes.
        self._modlist_view.on_open_nif_viewer = self._open_nif_viewer_for_mod
        # Mod removal strips plugins from plugins.txt/loadorder.txt - reload the
        # plugin panel so the removed plugins disappear without a manual refresh.
        self._modlist_view.on_mods_removed = self._on_mods_removed
        # Root-Folder toggle wrote meta.ini → refresh the root flag immediately,
        # then rescan those mods + rebuild filemap (the index caches strip-applied
        # vs verbatim paths, so it's now stale; the rebuild also refreshes the
        # filemap-derived root-rule/pre-RTX overlays).
        self._modlist_view.on_root_folder_changed = \
            lambda names: (self._refresh_modlist_flags(names),
                           self._rebuild_conflicts_async(rescan_index=True))
        # Endorse/Abstain: needs the shared Nexus API + a flag refresh.
        self._modlist_view.on_endorse = self._on_modlist_endorse
        # Track: needs the shared Nexus API (no meta.ini flag to persist).
        self._modlist_view.on_track = self._on_modlist_track
        # A saved note may change the note flag - light flag-only refresh.
        self._modlist_view.on_notes_changed = self._refresh_modlist_flags
        # Copy/Move to profile: worker copy + collision overlay + (move) remove.
        self._modlist_view.on_copy_to_profile = self._copy_mods_to_profile
        # Rename (context menu): folder + modindex + per-mod state migration.
        self._modlist_view.on_rename_mod = self._rename_mod_on_disk
        # Separator settings (colour + deploy override): open the scoped tab;
        # rename/remove migrate/drop the stored colour + deploy entries.
        self._modlist_view.on_separator_settings = self._open_sep_settings_tab
        self._modlist_view.on_separator_renamed = self._on_separator_renamed
        self._modlist_view.on_separators_removed = self._on_separators_removed
        # Enabling the Size column scans mod folder sizes on demand (Tk parity:
        # only walk the disk when Size is actually shown).
        self._modlist_view.on_sizes_requested = self._apply_modlist_sizes
        self._modlist_view.on_quick_filter = self._on_quick_modlist_filter
        self._modlist_view.quick_filter_state = self._quick_modlist_filter_state
        self._modlist_view.filters_active = lambda: self._panel_filters_active(
            "_modlist_filter_panel")
        self._modlist_view.on_clear_filters = lambda: self._clear_panel_filters(
            "_modlist_filter_panel")
        self._modlist_model._sizes = {}
        if not self._modlist_view.isColumnHidden(COL_SIZE):
            self._apply_modlist_sizes()
        with span("reload_modlist.load_separator_state"):
            self._modlist_view.load_separator_state()
        # Point the Mod Files tab at this game/profile (index next to filemap).
        if hasattr(self, "_mod_files_view"):
            idx = (staging.parent / "modindex.bin") if staging is not None else None
            self._mod_files_view.configure(
                self._gs.game, self._gs.profile_dir(), idx)
            # Re-show the previously selected mod on a same-context refresh (it
            # updates against the freshly-scanned files); blank otherwise. The
            # Overwrite / Root Folder boundary rows are synthetic (not in the raw
            # modlist) but always present, so they survive too.
            from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
            still_present = keep_mod_files is not None and (
                keep_mod_files in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                or any(e.name == keep_mod_files for e in entries))
            self._mod_files_view.show_mod(
                keep_mod_files if still_present else None)
        # Point the Data tab at this game/profile (filemap.txt + modindex.bin).
        if hasattr(self, "_data_view"):
            fm = (staging.parent / "filemap.txt") if staging is not None else None
            idx2 = (staging.parent / "modindex.bin") if staging is not None else None
            self._data_view.configure(
                self._gs.game, self._gs.profile_dir(), fm, idx2)
        # Point the Downloads tab at this game (game-name getter for cache dir).
        if hasattr(self, "_downloads_view"):
            self._downloads_view.configure(
                self._gs.game, lambda: self._gs.game_name)
        # Point the Overrides tab (BG3) at this game/profile.
        if hasattr(self, "_overrides_view"):
            self._overrides_view.configure(
                self._gs.profile_dir(), staging, ml_path)
        # Point the Text Files tab at this game/profile.
        if hasattr(self, "_text_files_view"):
            fm3 = (staging.parent / "filemap.txt") if staging is not None else None
            self._text_files_view.configure(
                self._gs.game, self._gs.profile_dir(), fm3, staging)
        # Point the Saves tab at this game/profile (saves can be per-profile).
        if hasattr(self, "_saves_view"):
            self._saves_view.configure(self._gs.game, self._gs.profile or "")
        self._refresh_footer_toggle_labels()
        # Re-apply an active search + filters against the fresh row indices.
        # set_entries fired modelReset, which dropped the view's applied-hidden
        # cache but left the OLD filter-hidden row indices in place - without
        # this reapply they'd hide the wrong (or all) rows after an install,
        # blanking the list until a manual Refresh. compute_hidden_rows re-indexes
        # off the current entry list; the async conflict/filter-data rebuild below
        # then reapplies once more with the freshly-scanned per-mod data.
        with span("reload_modlist.search+filters"):
            self._apply_modlist_search()
            self._apply_modlist_filters()
        self._refresh_modlist_stats()
        # File counts for the pinned Overwrite / Root Folder rows (async walk).
        self._refresh_boundary_counts(clear=not preserve_overlays)
        print(f"[gui_qt] modlist: {ml_path} ({len(entries)} entries)")

        # The Downloads tab reflects the active profile's STAGING folder (which
        # changes with the profile), so refresh it on every reload - even for an
        # empty profile, where the conflict rebuild below is skipped.
        if hasattr(self, "_data_view"):
            self._data_view.mark_dirty()
        if hasattr(self, "_downloads_view"):
            self._downloads_view.mark_dirty()
        if hasattr(self, "_text_files_view"):
            self._text_files_view.mark_dirty()
        if hasattr(self, "_overrides_view"):
            self._overrides_view.mark_dirty()
        # The Nexus browser (if open) shows Install/Reinstall per the active
        # profile's installed mods - refresh on every modlist reload (covers
        # profile/game change AND post-install).
        nv = getattr(self, "_nexus_view", None)
        if nv is not None:
            nv._game = self._gs.game
            nv.refresh_installed()
        tv = getattr(self, "_thunderstore_view", None)
        if tv is not None:
            tv._game = self._gs.game
            tv.refresh_installed()

        # Meta read + conflict rebuild, SEQUENCED. Both cold-read the same
        # per-mod meta.ini files (the meta columns here, the filemap's
        # root-flag collect inside the conflict build); run concurrently they
        # each parse ~the whole set and stretch each other under the GIL
        # (measured 4.0s + 3.1s concurrent vs ~0.4s + ~0.05s sequenced - the
        # second reader hits the read_meta mtime cache). So: meta worker
        # first, and _on_modlist_meta_ready chains the conflict rebuild.
        meta_started = False
        if entries and staging is not None:
            import threading
            ignored = self._ignored_missing_reqs
            pdir_meta = self._gs.profile_dir()
            is_bg3 = (getattr(self._gs.game, "game_id", "") == "baldurs_gate_3")
            meta_entries = list(entries)

            def meta_worker():
                try:
                    with span("modlist.meta_worker(read_meta)"):
                        payload = read_meta_for_entries(
                            meta_entries, staging, ignored,
                            profile_dir=pdir_meta, is_bg3=is_bg3)
                except Exception as exc:
                    print(f"[gui_qt] meta read failed: {exc}", flush=True)
                    payload = None   # still emit - the conflict rebuild chains
                self._modlist_meta_ready.emit(meta_gen, payload)

            self._conflicts_after_meta = (meta_gen, rescan_index)
            threading.Thread(target=meta_worker, daemon=True,
                             name="modlist-meta").start()
            meta_started = True

        if entries and not meta_started:
            # No meta worker (staging unresolved) → kick the rebuild directly.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and rescan_index:
            # Empty modlist but an explicit rescan was requested (Refresh /
            # install / toggle). The conflict rebuild is normally skipped when
            # there are no mods (nothing to conflict), but the [Overwrite]
            # pseudo-mod is INDEPENDENT of the real modlist: files can sit in
            # overwrite/ (and in modindex.bin's [Overwrite] entry) with zero
            # enabled mods. Skipping the rebuild here left a stale [Overwrite]
            # index entry + stale filemap.txt that no Refresh could clear once
            # the modlist was empty - the file kept showing in the Data tab and
            # kept deploying (as a dangling symlink). Run the rebuild so the
            # index and filemap are reconciled against the folder; it also
            # writes an empty filemap when the last mod is removed.
            # _on_conflicts_ready sets _switch_conflicts_done when the build
            # lands, so the switch-marker bookkeeping below is still covered.
            self._rebuild_conflicts_async(rescan_index=rescan_index)
        elif not entries and getattr(self, "_switch_t0", None) is not None:
            # Empty profile, no rescan (plain profile switch) → no conflict
            # rebuild will follow, so the first plugin pass IS the final one;
            # without this the switch marks would leak into the next unrelated
            # plugin reload.
            self._switch_conflicts_done = True

    def _on_modlist_meta_ready(self, gen, payload):
        """UI thread: apply the worker-read per-mod meta (see _reload_modlist).
        *payload* is None when the read failed - the apply is skipped but the
        chained conflict rebuild below still runs (it must not be lost)."""
        if gen != self._modlist_meta_gen:
            return   # superseded - the game/profile switched mid-read
        self._mark_since_switch("switch→modlist_meta_applied")  # i18n: skip - perftrace marker label
        from Utils.perftrace import span
        # Chained conflict/filemap rebuild (see _reload_modlist): kicked here,
        # AFTER the meta read, so its root-flag collect hits the warm read_meta
        # cache instead of racing the worker over the same 400+ ini files.
        pend = getattr(self, "_conflicts_after_meta", None)
        if pend is not None and pend[0] == gen:
            self._conflicts_after_meta = None
            self._rebuild_conflicts_async(rescan_index=pend[1])
        if payload is None:
            return
        (versions, installed, flags, categories, updates,
         fomod, bain, missing_reqs, descriptions, authors) = payload
        self._mod_categories = categories
        self._mod_authors = authors
        self._mod_updates = updates
        self._mod_fomod = fomod
        self._mod_bain = bain
        self._mod_missing_reqs = missing_reqs
        with span("on_modlist_meta_ready(apply)"):
            self._modlist_model.set_meta(versions, installed, categories,
                                         descriptions, authors)
            self._modlist_model.set_flags(flags)
        # Now that _mod_fomod + meta are current, refresh the rerun-FOMOD overlay
        # (picks up a just-installed/updated mod's pending deps without waiting for
        # the next plugin reload).
        self._refresh_rerun_fomod_flags()
        # Prune any installed requirements from an open Missing Requirements panel
        # (this is the path the panel's own Install button lands on).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # The meta-derived filter inputs (updates / categories / fomod / bain /
        # missing-reqs) were just refreshed on this thread. _reload_modlist
        # cleared them to empty sets before the worker ran, so any active filter
        # that keys off them (e.g. "mods with updates") was reapplied against the
        # stale FilterData and blanked the list until Refresh. Rebuild the filter
        # data now so _on_filter_data_ready merges the fresh sets and reapplies -
        # only when a filter is actually active (avoids needless disk scans).
        # A token search (e.g. "!updates") keys off the SAME meta-derived sets,
        # so it must rebuild too - otherwise updating a mod that a token matched
        # reapplies "!updates" against the just-cleared sets and blanks the list.
        panel = getattr(self, "_modlist_filter_panel", None)
        panel_active = panel is not None and panel.any_active()
        if panel_active or self._modlist_token_search_active():
            self._rebuild_filter_data()

    def _apply_modlist_sizes(self):
        """Scan mod folder sizes and push them to the model. Called on reload
        when the Size column is visible, and when the user enables Size from the
        column menu (the disk walk is skipped while Size stays hidden).

        The walk covers every mod folder, so it runs on a daemon worker -
        a generation counter drops a stale result (profile switched or a
        newer scan started mid-walk)."""
        staging = self._gs.staging_dir()
        if staging is None:
            return
        self._sizes_gen = getattr(self, "_sizes_gen", 0) + 1
        gen = self._sizes_gen
        # Snapshot the entry list - the user can reorder/rename while the
        # worker walks the disk.
        entries = list(self._modlist_model.natural_entries())

        from gui_qt.worker import run_in_worker, NO_EMIT

        def scan():
            from gui_qt.modlist_data import compute_sizes
            sizes, size_bytes = compute_sizes(entries, staging)
            return gen, sizes, size_bytes

        run_in_worker(scan, self._sizes_ready, name="modlist-sizes",
                      unpack=True, error_result=NO_EMIT)

    def _on_sizes_ready(self, gen, sizes, size_bytes):
        if gen != getattr(self, "_sizes_gen", 0):
            return   # superseded - a newer scan is in flight
        self._modlist_model.set_sizes(sizes, size_bytes)

    def _refresh_boundary_counts(self, clear: bool = False):
        """Count the files in the Overwrite / Root Folder folders and push them
        to the model, which paints them as the "(N)" on those separator rows.

        Both folders are usually small, but the game writes into overwrite/ at
        will, so the walk runs on a worker with a generation guard (a profile
        switch or a newer walk drops the stale result). *clear* blanks the old
        counts first - they belong to the game/profile we just left."""
        if clear:
            self._modlist_model.set_boundary_counts({})
        game = self._gs.game
        if game is None:
            return
        try:
            overwrite = Path(game.get_effective_overwrite_path())
            root_folder = Path(game.get_effective_root_folder_path())
        except Exception:
            return
        self._boundary_counts_gen = getattr(self, "_boundary_counts_gen", 0) + 1
        gen = self._boundary_counts_gen

        from gui_qt.worker import run_in_worker, NO_EMIT
        from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME

        def scan():
            from gui_qt.modlist_data import count_files_in
            return gen, {OVERWRITE_NAME: count_files_in(overwrite),
                         ROOT_FOLDER_NAME: count_files_in(root_folder)}

        run_in_worker(scan, self._boundary_counts_ready,
                      name="modlist-boundary-counts", unpack=True,
                      error_result=NO_EMIT)

    def _on_boundary_counts_ready(self, gen, counts):
        if gen != getattr(self, "_boundary_counts_gen", 0):
            return   # superseded - a newer walk is in flight
        self._modlist_model.set_boundary_counts(counts)
        self._modlist_view.viewport().update()

    def _refresh_modlist_stats(self):
        """Update the modlist footer count label: enabled / total mod counts.
        Both come straight from the model, so this is instant."""
        lbl = getattr(self, "_modlist_count", None)
        if lbl is None:
            return
        entries = [self._modlist_model.entry(r)
                   for r in range(self._modlist_model.rowCount())]
        mods = [e for e in entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        lbl.setText(f"{enabled} / {len(mods)}")
        lbl.setToolTip(self.tr("{0} enabled of {1} mods").format(enabled, len(mods)))

    def _installed_mod_ids(self) -> set[tuple[str, int]]:
        """Domain-qualified Nexus identities installed in the active profile."""
        ids: set[tuple[str, int]] = set()
        staging = self._gs.staging_dir()
        if staging is None:
            return ids
        game = self._gs.game
        fallback = (getattr(game, "nexus_game_domain", "") or "").strip().lower()
        from Nexus.nexus_meta import normalise_game_domain, read_meta
        for r in range(self._modlist_model.rowCount()):
            e = self._modlist_model.entry(r)
            if e is None or e.is_separator:
                continue
            meta_path = staging / e.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
                mid = int(getattr(meta, "mod_id", 0) or 0)
                domain = (normalise_game_domain(meta.game_domain) or fallback)
            except Exception:
                continue
            if mid > 0 and domain:
                ids.add((domain, mid))
        return ids

    def _on_mods_removed(self):
        """A mod was fully removed (single or multi right-click Remove): reload
        the plugins panel AND re-run the light flag pass. Removing a mod must
        resurface the ⚠ missing-requirement flag on mods that depended on it -
        the seeded list stays in meta.ini for exactly this, and the flag
        two-pass recomputes installed ids from the remaining rows. Must be a
        FULL flag pass (no names subset): a subset would compute installed ids
        from the subset alone."""
        self._reload_plugins()
        self._refresh_modlist_flags()

    def _refresh_modlist_flags(self, names=None):
        """Re-read the meta/profile-derived flags and push them into the model
        WITHOUT the full modlist reset (keeps selection + scroll). Call after any
        action that changes a flag (endorse, root toggle, note edit). When
        *names* is given, only those mods' metas are re-read and merged into the
        existing state (endorsing one mod shouldn't re-read 500 meta files).
        The filemap-derived overlays (pre-RTX / root-rule) refresh via the
        conflict-ready path instead."""
        from gui_qt.modlist_data import read_meta_for_entries
        staging = self._gs.staging_dir()
        if staging is None:
            return
        subset = set(names) if names else None
        entries = [e for r in range(self._modlist_model.rowCount())
                   if (e := self._modlist_model.entry(r)) is not None
                   and (subset is None or e.name in subset)]
        try:
            (_v, _i, flags, categories, updates, fomod, bain,
             missing_reqs, _desc, authors) = read_meta_for_entries(
                entries, staging, self._ignored_missing_reqs,
                profile_dir=self._gs.profile_dir(),
                is_bg3=(getattr(self._gs.game, "game_id", "") == "baldurs_gate_3"))
        except Exception:
            return
        if subset is None:
            (self._mod_categories, self._mod_authors, self._mod_updates,
             self._mod_fomod, self._mod_bain, self._mod_missing_reqs) = (
                categories, authors, updates, fomod, bain, missing_reqs)
        else:
            # Merge: clear the requested names first (a cleared flag won't
            # appear in the subset result), then overlay the fresh values.
            flags = {**{n: b for n, b in self._modlist_model._flags.items()
                        if n not in subset}, **flags}
            self._mod_categories = {**{n: c for n, c in self._mod_categories.items()
                                       if n not in subset}, **categories}
            self._mod_authors = {**{n: a for n, a in
                                    getattr(self, "_mod_authors", {}).items()
                                    if n not in subset}, **authors}
            for cur, fresh in ((self._mod_updates, updates),
                               (self._mod_fomod, fomod),
                               (self._mod_bain, bain),
                               (self._mod_missing_reqs, missing_reqs)):
                cur -= subset
                cur |= fresh
        self._modlist_model.set_flags(flags)
        # Re-point the model at the (possibly rebuilt) categories dict and
        # re-sort if the Category column drives the current sort.
        self._modlist_model._categories = self._mod_categories
        self._modlist_model._resort_if_key("category")
        self._modlist_model.set_notes(self._read_mod_notes())
        # If the Missing Requirements panel is open, drop cards for any
        # requirement that's now installed (works for any install path).
        view = getattr(self, "_missing_reqs_view", None)
        if view is not None:
            try:
                view.prune_installed(self._installed_mod_ids())
            except Exception:
                pass
        # This light path just mutated the meta-derived sets (updates / fomod /
        # bain / missing-reqs) in place. An active token search or panel filter
        # keys off the FilterData COPY of those sets, so refresh it and reapply -
        # otherwise a mod that just lost its !update flag lingers (or the list
        # goes stale) until a full Refresh.
        data = getattr(self, "_modlist_filter_data", None)
        if data is not None:
            data.mods_with_updates = set(self._mod_updates)
            data.fomod_mods = set(self._mod_fomod)
            data.bain_mods = set(self._mod_bain)
            data.missing_reqs = set(self._mod_missing_reqs)
            self._apply_modlist_filters()
        if self._modlist_token_search_active():
            self._apply_modlist_search()

    def _read_mod_notes(self) -> dict:
        """Per-mod note text for the active profile (Note flag tooltip). {} if
        none / no profile."""
        pdir = self._gs.profile_dir()
        if pdir is None:
            return {}
        try:
            from Utils.profile_state import read_mod_notes
            return read_mod_notes(pdir)
        except Exception:
            return {}

    def _refresh_plugin_stats(self):
        """Update the plugins footer count label: total plugins / non-ESL. All
        the data is in-memory on the plugin model, so this is instant."""
        lbl = getattr(self, "_plugin_count", None)
        if lbl is None:
            return
        from gui_qt.modlist_data import compute_plugin_stats
        s = compute_plugin_stats(self._plugin_model._rows)
        lbl.setText(self.tr("P:{0} / Non-ESL:{1}").format(
            s['total'], s['non_esl']))
        lbl.setToolTip(self.tr("{0} plugins ({1} ESL, {2} non-ESL)").format(
            s["total"], s["esl"], s["non_esl"]))

    def _reload_plugins(self):
        """Load the active game/profile's plugins into the Plugins tab.

        The disk work (per-plugin header reads for the ESL/master flags,
        master checks, filemap parse) is hundreds of file opens on a large
        load order, so it runs on a daemon worker → _plugins_loaded → the
        UI applies the rows. A generation counter drops results from a
        superseded reload (game/profile switched mid-read)."""
        import threading
        self._reassert_profile_paths()
        self._plugins_gen += 1
        gen = self._plugins_gen
        game, profile = self._gs.game, self._gs.profile
        ul_path = self._userlist_path()

        def worker():
            from gui_qt.plugin_state import (
                load_plugins, resolve_plugin_paths_for_game)
            from Utils.userlist import read_userlist_state
            from Utils.perftrace import span
            # Poll for supersession between the load's expensive phases: a
            # newer reload bumping the gen makes this one's result dead on
            # arrival (dropped in _on_plugins_loaded), so stop working on it.
            stale = lambda: gen != self._plugins_gen
            report = {}
            try:
                with span("plugins.load_plugins(worker)"):
                    rows = load_plugins(game, profile, cancelled=stale,
                                        report=report)
            except Exception as exc:
                print(f"[gui_qt] plugin load failed: {exc}", flush=True)
                rows = []
            if rows is None or stale():
                msg = (f"plugin load gen={gen} cancelled mid-build "
                       f"(superseded by gen={self._plugins_gen})")
                print(f"[plugin-diag] {msg}", flush=True)
                self._append_log(f"[rescan-diag] {msg}")
                return
            with span("plugins.resolve_paths(worker)"):
                paths = (resolve_plugin_paths_for_game(game)
                         if game is not None else {})
            try:
                state = read_userlist_state(ul_path)
            except Exception:
                state = None
            self._plugins_loaded.emit(gen, rows, paths, state, report)

        threading.Thread(target=worker, daemon=True,
                         name="plugins-reload").start()

    def _clear_plugin_panel(self):
        """Blank the plugin panel while a reload that will repopulate it is in
        flight (profile switch: the conflict rebuild's plugin reload is the
        authoritative one). Bumps the generation so any in-flight load from
        the previous profile is dropped/cancelled."""
        self._plugins_gen += 1
        # A pending Refresh cleanup offer belongs to the OLD profile.
        self._offer_mass_prune = False
        self._plugin_model.set_rows([], game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        self._plugin_view.set_master_highlight(set())
        self._plugin_view.refresh_missing_marker()
        self._plugin_view.refresh_cycle_marker()
        self._refresh_plugin_stats()

    def _on_plugins_loaded(self, gen, rows, paths, state, report=None):
        """UI thread: apply a finished plugin reload (see _reload_plugins)."""
        if gen != self._plugins_gen:
            msg = (f"plugins_loaded gen={gen} SUPERSEDED "
                   f"(current={self._plugins_gen}) - {len(rows)} row(s) DROPPED")
            print(f"[plugin-diag] {msg}", flush=True)
            self._append_log(f"[rescan-diag] {msg}")
            return   # superseded - a newer reload is in flight
        # Final link in the chain: how many plugin rows actually reached the
        # panel. If this is 0 while the index/filemap looked fine above, the
        # failure is in plugin discovery, not the filemap. GUI-visible so a
        # user can see + send it without an env var.
        self._append_log(f"[rescan-diag] plugins_loaded gen={gen} → applying "
                         f"{len(rows)} row(s) to the panel")
        import time as _time
        _apply_t0 = _time.perf_counter()
        self._plugin_model.set_rows(rows, game=self._gs.game,
                                    profile=self._gs.profile,
                                    profile_dir=self._gs.profile_dir())
        # Context menu reads these off the view (ESL path resolution + refresh).
        self._plugin_view.game = self._gs.game
        self._plugin_view.profile_dir = self._gs.profile_dir()
        self._plugin_view.on_plugins_changed = self._reload_plugins
        # The Saves details pane flags plugins a save was made with that are no
        # longer in the load order - reuse the list we just loaded.
        if hasattr(self, "_saves_view"):
            self._saves_view.set_known_plugins([r.name for r in rows])
        self._apply_plugin_search()
        # Persistent red marker-strip ticks for plugins with missing masters
        # (Tk parity) - recomputed from the freshly-loaded PF_MISSING flags.
        self._plugin_view.refresh_missing_marker()
        # Red marker-strip ticks for plugins whose userlist rules form a broken
        # cycle - recomputed from the freshly-loaded PF_UL_CYCLE flags.
        self._plugin_view.refresh_cycle_marker()
        # Resolved plugin paths (name.lower → on-disk path) power the master-
        # highlight on plugin selection; clear any stale master ticks.
        self._plugin_paths = paths
        self._plugin_view.set_master_highlight(set())
        # Row indices/flags changed - re-apply any active plugin filter.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()
        self._refresh_plugin_stats()
        self._refresh_framework_banner()
        self._apply_plugins_supported()
        # Rerun-FOMOD flag: an option's fileDependency plugin may have just become
        # enabled/disabled - recompute the overlay against the fresh load order.
        self._refresh_rerun_fomod_flags()
        # Userlist state → PF_USERLIST/PF_UL_CYCLE bits were already applied by
        # load_plugins; push the membership sets (context-menu predicates), the
        # group map (flags tooltip), and the userlist action callbacks.
        if state is None:
            from Utils.userlist import read_userlist_state
            state = read_userlist_state(self._userlist_path())
        self._userlist_state = state
        self._plugin_view.userlist_plugins = state.plugins
        self._plugin_view.userlist_cycles = state.cycle_plugins
        self._plugin_model.set_userlist_groups(state.group_map)
        self._plugin_view.on_userlist_add = self._on_userlist_add
        self._plugin_view.on_group_add = self._on_group_add
        self._plugin_view.on_userlist_remove = self._on_userlist_remove
        self._plugin_view.on_show_cycle = self._open_plugin_cycle_tab
        self._plugin_view.on_show_overlapping = self._on_show_overlapping_plugins
        self._plugin_view.on_quick_filter = self._on_quick_plugin_filter
        self._plugin_view.quick_filter_state = self._quick_plugin_filter_state
        self._plugin_view.filters_active = lambda: self._panel_filters_active(
            "_plugin_filter_panel")
        self._plugin_view.on_clear_filters = lambda: self._clear_panel_filters(
            "_plugin_filter_panel")
        print(f"[gui_qt] plugins: {len(rows)} entries")
        from Utils import perftrace
        perftrace.mark("on_plugins_loaded(apply)",
                       _time.perf_counter() - _apply_t0)
        # ESL-eligibility (filters) computes AFTER the rows are visible - a
        # cold libloot scan is seconds of GIL-hogging record parsing.
        self._start_esl_scan(gen, rows, paths)
        # Profile-switch milestones: the FIRST plugin pass runs against the old
        # filemap (fast feedback); the FINAL pass follows the conflict rebuild
        # and is the moment the switch is fully rendered.
        if getattr(self, "_switch_conflicts_done", False):
            self._mark_since_switch("switch→plugins_final_applied")  # i18n: skip - perftrace marker label
            self._switch_t0 = None
            self._switch_conflicts_done = False
        else:
            self._mark_since_switch("switch→plugins_first_applied")  # i18n: skip - perftrace marker label
        # Last render step of first load - the plugin panel is now fully
        # populated. Dismiss the startup splash here (dropped one event-loop
        # turn later so this final dataChanged pass actually paints first).
        if not self._splash_dismissed and self._splash is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._dismiss_splash)
        # Explicit Refresh Modlist: the automatic phantom-prune caps at
        # _PRUNE_MAX entries (a mass miss usually means a broken resolution),
        # so a listed-but-nowhere-on-disk load order above the cap - e.g.
        # another profile's plugins.txt copied in - survives every reload.
        # On a user-initiated refresh, offer the mass cleanup with a confirm
        # instead. One-shot: consumed by the first reload whose prune check
        # actually ran (filemap_ok), so a refresh whose direct reload got
        # superseded still offers from the conflicts-ready reload.
        if (getattr(self, "_offer_mass_prune", False)
                and report is not None and report.get("prune_checked")):
            self._offer_mass_prune = False
            phantoms = report.get("mass_prune") or []
            if phantoms:
                self._offer_phantom_cleanup(list(phantoms))

    def _offer_phantom_cleanup(self, names: list[str]):
        """Confirm-then-prune for a mass phantom load order (see the Refresh
        hook in _on_plugins_loaded). The confirm re-checks that the same
        game/profile is still active before writing: the overlay is modeless,
        and pruning these names from a DIFFERENT profile's plugins.txt (where
        they may be perfectly valid) would be data loss."""
        game, profile = self._gs.game, self._gs.profile
        if game is None or not profile:
            return

        def _confirmed(ok):
            if not ok:
                return
            if self._gs.game is not game or self._gs.profile != profile:
                return   # switched away while the confirm was up
            from gui_qt.plugin_state import prune_listed_plugins
            prune_listed_plugins(game, profile, names)
            self._notify(
                self.tr("Removed {0} stale plugin(s)").format(len(names)),
                "info")
            self._reload_plugins()

        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Remove stale plugins"),
            self.tr("{0} plugins listed in this profile have no file in its "
                    "mods, overwrite, or game folder - usually leftovers "
                    "from removed mods or another profile's load order. "
                    "Remove them from the load order? Mod files are not "
                    "touched.").format(len(names)),
            _confirmed, confirm_label=self.tr("Remove"), list_items=names)

    def _start_esl_scan(self, gen, rows, resolved):
        """Compute the ESL-safe/unsafe filter bits on a worker AFTER the plugin
        rows are applied. A cold libloot eligibility scan is seconds of
        full-record parsing that does not release the GIL - inside
        load_plugins it starved every other reload worker and the UI thread.
        Results are mtime-cached (plugin_state._ESL_ELIG_CACHE), so repeat
        loads resolve instantly."""
        game = self._gs.game
        if game is None or not getattr(game, "supports_esl_flag", False):
            return
        names = [r.name for r in rows]
        if not names:
            return
        import threading

        def worker():
            from gui_qt.plugin_state import compute_esl_eligibility
            from Utils.perftrace import span
            data_dir = (game.get_vanilla_plugins_path()
                        if hasattr(game, "get_vanilla_plugins_path") else None)
            with span("plugins.esl_eligibility(deferred)"):
                kinds = compute_esl_eligibility(names, resolved, data_dir, game)
            self._esl_elig_ready.emit(gen, kinds)

        threading.Thread(target=worker, daemon=True, name="esl-elig").start()

    def _on_esl_elig_ready(self, gen, kinds):
        """UI thread: merge the deferred ESL-eligibility bits into the plugin
        rows (see _start_esl_scan) and reapply any active plugin filter that
        keys off them."""
        if gen != self._plugins_gen or not kinds:
            return   # superseded - a newer plugin reload is in flight/applied
        from gui_qt.plugin_state import PF_ESL_SAFE, PF_ESL_UNSAFE
        from gui_qt.plugin_model import COL_FLAGS, PFlagsRole
        m = self._plugin_model
        changed = False
        for r in m._rows:
            bits = kinds.get(r.name.lower())
            if bits is None:
                continue
            nf = (r.flags & ~(PF_ESL_SAFE | PF_ESL_UNSAFE)) | bits
            if nf != r.flags:
                r.flags = nf
                changed = True
        if not changed:
            return
        m.dataChanged.emit(m.index(0, COL_FLAGS),
                           m.index(len(m._rows) - 1, COL_FLAGS),
                           [PFlagsRole, Qt.ToolTipRole])
        m.flags_changed()   # re-sort if the Flags column drives the display
        # The ESL-safe/unsafe filters read these bits - reapply if active.
        if getattr(self, "_plugin_filter_panel", None) is not None:
            self._apply_plugin_filters()

    # ---- LOOT userlist (groups / rules / cycle / flag) ---------------------
    def _userlist_path(self):
        """<active profile>/userlist.yaml, or None without a profile (matches
        the Tk _get_userlist_path and the Sort Plugins path resolution)."""
        pdir = self._gs.profile_dir()
        return (pdir / "userlist.yaml") if pdir else None

    def _refresh_userlist_flags(self):
        """Light refresh after a userlist.yaml edit: re-read the state and
        update each plugin row's PF_USERLIST/PF_UL_CYCLE bits + the menu sets +
        the tooltip group map, without a full plugin reload (Tk parity:
        _refresh_userlist_set + _predraw)."""
        from gui_qt.plugin_state import PF_USERLIST, PF_UL_CYCLE
        from gui_qt.plugin_model import COL_FLAGS, PFlagsRole
        from Utils.userlist import read_userlist_state
        state = read_userlist_state(self._userlist_path())
        self._userlist_state = state
        self._plugin_view.userlist_plugins = state.plugins
        self._plugin_view.userlist_cycles = state.cycle_plugins
        m = self._plugin_model
        m.set_userlist_groups(state.group_map)
        for r in m._rows:
            r.flags &= ~(PF_USERLIST | PF_UL_CYCLE)
            low = r.name.lower()
            if low in state.plugins:
                r.flags |= PF_USERLIST
                if low in state.cycle_plugins:
                    r.flags |= PF_UL_CYCLE
        if m._rows:
            m.dataChanged.emit(m.index(0, COL_FLAGS),
                               m.index(len(m._rows) - 1, COL_FLAGS),
                               [PFlagsRole, Qt.ToolTipRole])
            m.flags_changed()   # re-sort if the Flags column drives the display

    def _on_userlist_bar_saved(self, message: str):
        """An inline bar (Add to userlist / Add to group) wrote userlist.yaml."""
        self._notify(message, "success")
        self._refresh_userlist_flags()

    def _close_userlist_ui(self):
        """Close the userlist-scoped tabs + hide the inline bars - they hold
        the previous game/profile's userlist path."""
        for key in ("plugin_groups", "plugin_rules", "plugin_cycle"):
            if self._tabs.has_key(key):
                self._tabs.close_tab(key)
        self._plugin_rules_view = None
        self._plugin_cycle_view = None
        self._cycle_anchor = ""
        self._cycle_scope = frozenset()
        for bar in (getattr(self, "_ul_bar", None),
                    getattr(self, "_grp_bar", None)):
            if bar is not None:
                bar.cancel()

    def _open_plugin_groups_tab(self):
        """Footer 'Groups' button → LOOT Groups tab over the modlist panel."""
        ul_path = self._userlist_path()
        if ul_path is None:
            self._notify(self.tr("No active profile - cannot configure groups."),
                         "warning")
            return
        # Tk closes any existing overlay first so the view reflects the file.
        if self._tabs.has_key("plugin_groups"):
            self._tabs.close_tab("plugin_groups")
        from gui_qt.plugin_groups_view import PluginGroupsView
        view = PluginGroupsView(
            ul_path,
            on_close=lambda: self._tabs.close_tab("plugin_groups"),
            on_saved=self._refresh_userlist_flags,
        )
        self._tabs.open_scoped_tab(view, self.tr("LOOT Groups"),
                                   self._modlist_panel_stack,
                                   key="plugin_groups")

    def _open_plugin_rules_tab(self):
        """Footer 'Plugin Rules' button → per-plugin rules tab over the
        modlist panel. Follows the plugins-panel selection while open."""
        ul_path = self._userlist_path()
        if ul_path is None:
            self._notify(self.tr("No active profile - cannot configure plugin rules."),
                         "warning")
            return
        if self._tabs.has_key("plugin_rules"):
            self._tabs.close_tab("plugin_rules")
        plugin_names = [r.name for r in self._plugin_model.natural_rows()]
        sel_rows = self._plugin_view.selectionModel().selectedRows()
        sel_name = (self._plugin_model.row(sel_rows[0].row()).name
                    if sel_rows else "")
        from gui_qt.plugin_rules_view import PluginRulesView
        view = PluginRulesView(
            plugin_names, ul_path, selected_plugin=sel_name,
            on_close=lambda: self._tabs.close_tab("plugin_rules"),
            on_saved=self._refresh_userlist_flags,
        )
        self._plugin_rules_view = view
        self._tabs.open_scoped_tab(view, self.tr("LOOT Plugin Rules"),
                                   self._modlist_panel_stack,
                                   key="plugin_rules")

    def _open_plugin_cycle_tab(self, plugin_name: str):
        """'Show cycle…' / 'Show userlist rules…' → cycle tab over the modlist
        panel, pinned to the plugin's SCC (or its whole rule component)."""
        ul_path = self._userlist_path()
        if ul_path is None:
            return
        from Utils.userlist import parse_userlist, userlist_rule_component
        name_lower = plugin_name.lower()
        state = getattr(self, "_userlist_state", None)
        component = (state.cycle_components.get(name_lower)
                     if state is not None else None)
        if not component:
            data = (parse_userlist(ul_path) if ul_path.is_file()
                    else {"plugins": [], "groups": []})
            component = userlist_rule_component(data, name_lower)
        if not component:
            self._notify(self.tr("{0} has no userlist rules to display.").format(plugin_name),
                         "info")
            return
        if self._tabs.has_key("plugin_cycle"):
            self._tabs.close_tab("plugin_cycle")
        # Freeze the plugin set at open time. Subsequent flips keep showing
        # these plugins' rules even if the cycle is gone, so the user can
        # revert or adjust further (Tk parity).
        self._cycle_anchor = name_lower
        self._cycle_scope = component
        from gui_qt.plugin_cycle_view import PluginCycleView
        view = PluginCycleView(
            plugin_name,
            on_close=lambda: self._tabs.close_tab("plugin_cycle"),
            on_flip=self._on_flip_plugin_rule,
        )
        self._plugin_cycle_view = view
        self._tabs.open_scoped_tab(view, self.tr("Plugin Cycle"),
                                   self._modlist_panel_stack,
                                   key="plugin_cycle")
        self._refresh_cycle_tab_data()

    def _refresh_cycle_tab_data(self):
        """Push the pinned scope's current rules to the open cycle tab."""
        if not self._tabs.has_key("plugin_cycle"):
            return
        view = getattr(self, "_plugin_cycle_view", None)
        anchor = getattr(self, "_cycle_anchor", "")
        scope = getattr(self, "_cycle_scope", frozenset())
        if view is None or not anchor or not scope:
            return
        from Utils.userlist import parse_userlist, build_cycle_scope_data
        ul_path = self._userlist_path()
        data = (parse_userlist(ul_path) if (ul_path and ul_path.is_file())
                else {"plugins": [], "groups": []})
        display: dict[str, str] = {}
        for r in self._plugin_model._rows:
            display[r.name.lower()] = r.name
        for entry in data.get("plugins", []):
            raw = entry.get("name") or ""
            if raw:
                display.setdefault(raw.lower(), raw)
        info = build_cycle_scope_data(data, scope, display)
        view.update_cycle(
            starting_plugin=display.get(anchor, anchor),
            scope_plugins=scope,
            display_names=display,
            **info,
        )

    def _on_flip_plugin_rule(self, owner: str, field: str, target: str):
        """Cycle-tab Flip button: move target between the owner entry's
        after/before lists, save, refresh flag + tab."""
        from Utils.userlist import (parse_userlist, write_userlist,
                                    flip_plugin_rule)
        ul_path = self._userlist_path()
        if ul_path is None or not ul_path.is_file():
            self._notify(self.tr("userlist.yaml not found - cannot flip rule."),
                         "warning")
            return
        data = parse_userlist(ul_path)
        if not flip_plugin_rule(data, owner, field, target):
            self._notify(self.tr("Rule {0} '{1}' {2} not found in userlist.yaml.").format(owner, field, target), "warning")
            return
        write_userlist(ul_path, data)
        other = "before" if field == "after" else "after"
        self._notify(self.tr("Flipped: {0} now '{1}' {2}").format(owner, other, target), "success")
        self._refresh_userlist_flags()
        self._refresh_cycle_tab_data()

    # ---- userlist context-menu actions (plugin_menu callbacks) -------------
    def _on_userlist_add(self, plugin_name: str, row: int):
        """'Add to userlist…' - open the inline bar prefilled with the
        plugin's current load-order neighbours (Tk _add_plugin_to_userlist)."""
        if self._userlist_path() is None:
            self._notify(self.tr("No active profile - cannot edit userlist."), "warning")
            return
        # Neighbours come from the LOAD order - *row* is a display row, which a
        # column sort detaches from the load order.
        rows = self._plugin_model.natural_rows()
        pos = self._plugin_model.natural_index(plugin_name)
        if pos < 0:
            pos = row
        after_plugin = rows[pos - 1].name if pos > 0 else ""
        before_plugin = rows[pos + 1].name if pos + 1 < len(rows) else ""
        self._ul_bar.open_for(plugin_name, after_plugin, before_plugin)

    def _on_group_add(self, plugin_names: list):
        """'Add to group…' - open the inline group-assignment bar."""
        if self._userlist_path() is None:
            self._notify(self.tr("No active profile - cannot assign group."), "warning")
            return
        self._grp_bar.open_for(plugin_names)

    def _on_userlist_remove(self, plugin_names: list):
        """'Remove from userlist' - drop the plugins' entries and refresh."""
        from Utils.userlist import parse_userlist, write_userlist, remove_plugins
        ul_path = self._userlist_path()
        if ul_path is None:
            return
        data = parse_userlist(ul_path)
        remove_plugins(data, plugin_names)
        write_userlist(ul_path, data)
        self._notify(self.tr("Removed from userlist: {0} plugin(s)").format(len(plugin_names)),
                     "success")
        self._refresh_userlist_flags()

    # ---- Sort Plugins (LOOT) ----------------------------------------------
    def _on_refresh_plugins(self):
        """Run LOOT to refresh plugin metadata (messages/dirty/tags/requirements)
        WITHOUT reordering the load order - the ready handler skips the reorder
        step when the context is flagged as a refresh."""
        self._on_sort_plugins(refresh=True)

    def _on_sort_plugins(self, _checked=False, *, refresh=False):
        """LOOT-sort the load order on a worker thread (reuses the Tk backend
        LOOT/loot_sorter.sort_plugins). Result applied on the UI thread.

        When *refresh* is True, LOOT still evaluates the plugins (yielding the
        same metadata) but the resulting order is discarded - only the metadata
        is written and the panel reloaded."""
        from LOOT.loot_sorter import is_available, unavailable_reason
        if not is_available():
            self._notify(self.tr("LOOT library not available - cannot sort."), "warning")
            reason = unavailable_reason()
            if reason:
                from Utils.app_log import app_log
                app_log(reason)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not getattr(game, "loot_sort_enabled", False):
            self._notify(self.tr("LOOT sorting isn't supported for this game."), "warning")
            return
        rows = list(self._plugin_model.natural_rows())
        if not rows:
            self._notify(self.tr("No plugins to sort."), "warning")
            return
        if self._sort_running:
            self._notify(self.tr("A sort is already running."), "info")
            return

        # Locked plugins stay put; LOOT sorts the rest. (Qt model already carries
        # vanilla rows pinned at the top, so - unlike Tk - we don't inject them.)
        # Indices are LOAD-order positions, so look locks up by name.
        locked_indices = {i: r for i, r in enumerate(rows)
                          if self._plugin_model.is_locked_name(r.name)}
        unlocked = [r for i, r in enumerate(rows) if i not in locked_indices]
        plugin_names = [r.name for r in unlocked]
        enabled_set = {r.name for r in unlocked if r.enabled}

        pdir = self._gs.profile_dir()
        userlist_path = None
        try:
            prof_ul = (pdir / "userlist.yaml") if pdir else None
            if prof_ul and prof_ul.is_file():
                userlist_path = prof_ul
            else:
                from Utils.config_paths import get_loot_game_dir
                g_ul = get_loot_game_dir(game.game_id) / "userlist.yaml"
                userlist_path = g_ul if g_ul.is_file() else None
        except Exception:
            userlist_path = None

        include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
        # Snapshot what the apply step needs (no model access mid-flight).
        self._sort_ctx = {
            "rows": rows, "locked_indices": locked_indices,
            "plugin_names": plugin_names, "include_vanilla": include_vanilla,
            "profile_dir": pdir, "game_id": game.game_id,
            "game": game, "profile": self._gs.profile,
            "refresh": refresh,
        }

        kw = dict(
            plugin_names=plugin_names, enabled_set=enabled_set,
            game_name=getattr(game, "name", self._gs.game_name),
            game_path=game.get_game_path(),
            staging_root=game.get_effective_mod_staging_path(),
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_id=game.game_id,
            masterlist_url=getattr(game, "loot_masterlist_url", ""),
            masterlist_repo=getattr(game, "loot_masterlist_repo", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
            userlist_path=userlist_path,
        )

        self._sort_running = True
        self._plugin_sort_btn.setEnabled(False)
        if hasattr(self, "_plugin_refresh_btn"):
            self._plugin_refresh_btn.setEnabled(False)
        if refresh:
            self._notify(
                self.tr("Refreshing LOOT metadata for {0} plugins…").format(
                    len(plugin_names)), "info")
        else:
            self._notify(
                self.tr("Running LOOT on {0} plugins…").format(
                    len(plugin_names)), "info")

        from gui_qt.worker import run_in_worker

        def sort():
            from LOOT.loot_sorter import sort_plugins
            from Utils.app_log import app_log
            # Route libloot's status lines (and, via run_in_worker's exception
            # handler, cyclic-dependency / sort-failure messages) to the visible
            # log panel - Tk did this with self._log; the Qt port had dropped it
            # to a stdout print(), so users never saw why a sort failed.
            return sort_plugins(
                log_fn=lambda m: app_log(f"[loot] {m}"), **kw)

        run_in_worker(sort, self._sort_plugins_ready, name="loot-sort")

    def _on_sort_plugins_ready(self, result):
        self._sort_running = False
        if hasattr(self, "_plugin_sort_btn"):
            self._plugin_sort_btn.setEnabled(True)
        if hasattr(self, "_plugin_refresh_btn"):
            self._plugin_refresh_btn.setEnabled(True)
        ctx = getattr(self, "_sort_ctx", None) or {}
        self._sort_ctx = None
        is_refresh = bool(ctx.get("refresh"))
        if result is None:
            self._notify(
                (self.tr("LOOT refresh failed - see log.") if is_refresh
                 else self.tr("LOOT sort failed - see log.")), "error")
            return
        for w in getattr(result, "warnings", []) or []:
            self._append_log(f"[loot] warning: {w}")

        # Persist evaluated LOOT metadata so PF_LOOT/DIRTY/TAGS light up on reload.
        try:
            from LOOT.loot_sorter import write_loot_info
            write_loot_info(ctx.get("profile_dir"), result.plugin_info,
                            result.general_messages, game_id=ctx.get("game_id", ""))
        except Exception as exc:
            self._append_log(f"[loot] could not write loot.json: {exc}")

        # Refresh mode: metadata is written, load order untouched. Just reload
        # the panel so the new flags light up, then stop.
        if is_refresh:
            self._reload_plugins()
            self._notify(self.tr("Plugin metadata refreshed."), "success")
            return

        from gui_qt.plugin_state import apply_loot_sort, save_plugins
        new_rows, moved = apply_loot_sort(
            ctx.get("rows", []), ctx.get("locked_indices", {}),
            list(result.sorted_names), ctx.get("include_vanilla", False))

        game, profile = ctx.get("game"), ctx.get("profile")
        if game is not None and profile:
            self._plugin_model.set_natural_rows(new_rows)
            try:
                save_plugins(game, profile, new_rows)
            except Exception as exc:
                self._notify(self.tr("Failed to write load order: {0}").format(exc), "error")
                return
        # Reload (rebuilds rows + flags) and recompute BSA conflicts (a sort
        # changes plugin order → BSA winners follow plugin load order).
        self._reload_plugins()
        self._rebuild_conflicts_async()
        if moved == 0 and not ctx.get("locked_indices"):
            self._notify(self.tr("Load order is already sorted."), "info")
        else:
            self._notify(
                (self.tr("Sorted - 1 plugin moved.") if moved == 1
                 else self.tr("Sorted - {0} plugins moved.").format(moved)),
                "success")

    # ---- Show overlapping plugins (LOOT record overlap) -------------------
    def _on_show_overlapping_plugins(self, plugin_name: str):
        """Find plugins whose records overlap with *plugin_name* and highlight
        them in the plugins list (Tk parity: plugin_panel_loot
        _show_overlapping_plugins). A full libloot load is expensive, so it
        runs on a worker thread; the result is applied on the UI thread."""
        from LOOT.loot_sorter import is_available, unavailable_reason
        if not is_available():
            self._notify(self.tr("LOOT library not available - cannot check overlap."),
                         "warning")
            reason = unavailable_reason()
            if reason:
                from Utils.app_log import app_log
                app_log(reason)
            return
        game = self._gs.game
        if game is None or not game.is_configured():
            self._notify(self.tr("No configured game selected."), "warning")
            return
        if not getattr(game, "loot_sort_enabled", False):
            self._notify(self.tr("LOOT sorting isn't supported for this game."), "warning")
            return
        rows = list(self._plugin_model.natural_rows())
        if not rows:
            return
        if getattr(self, "_overlap_running", False):
            self._notify(self.tr("An overlap check is already running."), "info")
            return

        # Snapshot everything the worker needs - no model access mid-flight.
        plugin_names = [r.name for r in rows]
        kw = dict(
            target_plugin=plugin_name,
            plugin_names=plugin_names,
            game_name=getattr(game, "name", self._gs.game_name),
            game_path=game.get_game_path(),
            staging_root=game.get_effective_mod_staging_path(),
            game_type_attr=getattr(game, "loot_game_type", ""),
            game_data_dir=(game.get_vanilla_plugins_path()
                           if hasattr(game, "get_vanilla_plugins_path") else None),
        )

        self._overlap_running = True
        self._notify(self.tr("Checking record overlap for {0}…").format(plugin_name), "info")

        from gui_qt.worker import run_in_worker

        def check():
            from LOOT.loot_sorter import find_overlapping_plugins
            from Utils.app_log import app_log
            overlaps = find_overlapping_plugins(
                log_fn=lambda m: app_log(f"[loot] {m}"), **kw)
            return (plugin_name, overlaps)

        # error_result carries the target name with a None payload so the apply
        # step can report the failure against the right plugin.
        run_in_worker(check, self._overlap_ready, name="loot-overlap",
                      unpack=True, error_result=(plugin_name, None))

    def _on_overlap_ready(self, plugin_name: str, overlaps):
        self._overlap_running = False
        if overlaps is None:
            self._notify(self.tr("Overlap check failed - see log."), "error")
            return
        if not overlaps:
            self._notify(self.tr("{0}: no record overlap with other plugins.").format(plugin_name),
                         "info")
            self._plugin_view.set_master_highlight(set())
            return

        # Highlight the target + overlapping plugins (green tint + marker strip),
        # then scroll the first highlighted row into view (Tk parity).
        names_lower = {plugin_name.lower(), *(o.lower() for o in overlaps)}
        self._plugin_view.set_master_highlight(names_lower)
        m = self._plugin_model
        first = next((i for i in range(m.rowCount())
                      if m.row(i).name.lower() in names_lower), None)
        if first is not None:
            from gui_qt.plugin_model import COL_FLAGS
            from PySide6.QtWidgets import QAbstractItemView
            self._plugin_view.scrollTo(m.index(first, COL_FLAGS),
                                       QAbstractItemView.PositionAtCenter)
        self._append_log(
            f"[loot] {plugin_name} overlaps with {len(overlaps)} plugin(s): "
            + ", ".join(overlaps))
        self._notify(
            self.tr("{0} overlaps {1} plugin(s) - highlighted in list").format(
                plugin_name, len(overlaps)),
            "warning")

    def _plugins_supported(self) -> bool:
        """Whether the active game uses plugins (has plugin extensions). Games
        that return an empty ``plugin_extensions`` (e.g. non-Bethesda) don't
        track plugins, so the Plugins tab shows no column header/footer."""
        game = self._gs.game
        return bool(getattr(game, "plugin_extensions", None)) if game else True

    def _saves_supported(self) -> bool:
        """Whether the Saves tab has anything to show for the active game: the
        Ludusavi manifest knows it, or it keeps profile-specific saves. Games
        with server-side saves (Fallout 76, Darktide…) have neither."""
        game = self._gs.game
        if game is None:
            return False
        if getattr(game, "profile_saves", False):
            return True
        # A manual override is the escape hatch for games the manifest gets
        # wrong, so it has to be able to turn the tab back on by itself.
        try:
            if game.get_save_path_override():
                return True
        except Exception:
            pass
        from Utils.ludusavi_manifest import lookup
        try:
            steam_id = game.effective_steam_id() or game.steam_id
        except Exception:
            steam_id = getattr(game, "steam_id", "") or ""
        return lookup(str(steam_id or ""), getattr(game, "name", "") or "") is not None

    def _apply_plugins_supported(self):
        """Hide the plugins column header + footer for games without plugins.
        Called on game/profile change (via _reload_plugins)."""
        supported = self._plugins_supported()
        # Column header: hide for plugin-less games.
        self._plugin_view.setHeaderHidden(not supported)
        # Sub-tab strip: plugin-less games hide the Plugins tab entirely and
        # land on Downloads instead. Switching back to a plugin game restores
        # the tab (and returns to it if we auto-moved off it).
        was_hidden = getattr(self, "_plugins_tab_hidden", False)
        self._plugins_tab_hidden = not supported
        self._plugin_tab_labels[0].setVisible(supported)
        # The Overrides tab (BG3 override paks) replaces the Plugins tab for
        # games that declare it - the two are never visible together.
        overrides = bool(getattr(self._gs.game, "has_override_pak_tab", False))
        was_overrides = getattr(self, "_overrides_tab_shown", False)
        self._overrides_tab_shown = overrides
        self._plugin_tab_labels[self._OVERRIDES_TAB_IDX].setVisible(overrides)
        if not supported and self._plugin_stack.currentIndex() == 0:
            self._select_plugin_tab(self._OVERRIDES_TAB_IDX if overrides
                                    else self._DOWNLOADS_TAB_IDX)
        elif (supported and was_hidden
                and self._plugin_stack.currentIndex() in (
                    self._DOWNLOADS_TAB_IDX, self._OVERRIDES_TAB_IDX)):
            self._select_plugin_tab(0)
        elif (not overrides and was_overrides and
                self._plugin_stack.currentIndex() == self._OVERRIDES_TAB_IDX):
            # BG3 → another plugin-less game: leave the now-hidden tab.
            self._select_plugin_tab(self._DOWNLOADS_TAB_IDX)
        # The Saves tab only shows for games with somewhere to look - hidden
        # for titles the Ludusavi manifest doesn't know (server-side saves).
        saves = self._saves_supported()
        self._plugin_tab_labels[self._SAVES_TAB_IDX].setVisible(saves)
        if not saves and self._plugin_stack.currentIndex() == self._SAVES_TAB_IDX:
            self._select_plugin_tab(0)
        if not saves:
            # The Saves filter panel sits in the shared window-left slot - a
            # game without a Saves tab must not leave it docked there.
            svfp = getattr(self, "_saves_filter_panel", None)
            if svfp is not None and svfp.isVisible():
                svfp.setVisible(False)
        # Footer (page 0 of the swap stack = the plugin tools) is hidden only
        # while the Plugins sub-tab is active; other tabs keep their own footer.
        self._refresh_plugin_footer_visibility()

    def _refresh_plugin_footer_visibility(self):
        """Show the footer stack unless we're on the Plugins tab of a game that
        doesn't use plugins."""
        stack = getattr(self, "_plugin_footer_stack", None)
        if stack is None:
            return
        on_plugins_tab = self._plugin_stack.currentIndex() == 0
        stack.setVisible(self._plugins_supported() or not on_plugins_tab)

    def _refresh_framework_banner(self):
        """Re-detect modding frameworks and update the Plugins-tab banner. Called
        on game/profile change + after each filemap rebuild (same as Tk).

        The detect reads filemap.txt (+ mod index) - ~200 ms on a 100k-file
        modlist - so it runs on a worker thread and lands via
        _framework_statuses_ready. A generation counter drops results that a
        game/profile switch (or a newer refresh) has superseded."""
        if not hasattr(self, "_framework_banner"):
            return
        self._framework_gen += 1
        gen = self._framework_gen
        # Snapshot inputs on the UI thread - GameState can be swapped under a
        # worker (see the profile-desync incident).
        game = self._gs.game
        staging = self._gs.staging_dir()
        filemap_path = (staging.parent / "filemap.txt") if staging is not None else None
        modlist_path = self._gs.modlist_path()

        from gui_qt.worker import run_in_worker

        def detect():
            from Utils.framework_detect import detect_frameworks
            return gen, detect_frameworks(game, filemap_path, modlist_path,
                                          rf_toggle_enabled=True)

        run_in_worker(detect, self._framework_statuses_ready,
                      name="framework-detect", unpack=True,
                      error_result=(gen, []))

    def _on_framework_statuses(self, gen: int, statuses):
        if gen != self._framework_gen or not hasattr(self, "_framework_banner"):
            return
        self._framework_banner.set_statuses(statuses)
        # No play-bar refresh here: the dropdown no longer auto-lists script
        # extenders, so framework state can't change what it shows.

    def _recompute_bsa_conflicts_async(self):
        """A plugin toggle/reorder changed the plugin load order. BSAs load at
        their plugin's position, so BSA conflict winners may have changed - but
        the deployed file set (filemap.txt / modindex.bin), loose conflicts,
        plugin ownership and framework states are all unaffected. So recompute
        ONLY the BSA conflicts off-thread (re-reads the freshly-written
        loadorder.txt) and repaint just the BSA icons on the modlist - instead
        of the full _rebuild_conflicts_async, which rebuilds the filemap and
        reloads both the modlist and the Plugins panel. Tk parity: plugin
        toggle → recompute_bsa_conflicts (BSA-only), NOT a filemap rebuild."""
        import threading
        g = self._gs.game
        if g is None or not self._gs.profile:
            return
        gen = getattr(self, "_bsa_conflict_gen", 0) + 1
        self._bsa_conflict_gen = gen

        def worker():
            try:
                (bsa_codes, bsa_over, bsa_overby,
                 lob) = self._gs._build_bsa_conflicts(g, lambda _m: None)
            except Exception as exc:
                print(f"[gui_qt] BSA recompute failed: {exc}", flush=True)
                return
            # Plugin order decides BSA winners, hence whether a loose file
            # still overrides one - so the loose icons can change too. Re-merge
            # from the pristine base, not the already-merged copy (which would
            # accumulate upgrades across toggles).
            loose_codes = None
            cd = getattr(self, "_conflict_data", None)
            if cd is not None and cd.loose_codes_base is not None:
                loose_codes = self._gs._merge_loose_beats_bsa(
                    dict(cd.loose_codes_base), lob)
            self._bsa_conflicts_ready.emit(
                gen, bsa_codes, bsa_over, bsa_overby, loose_codes)

        threading.Thread(target=worker, daemon=True).start()

    def _on_bsa_conflicts_ready(self, gen, bsa_codes, bsa_over, bsa_overby,
                                loose_codes=None):
        if gen != getattr(self, "_bsa_conflict_gen", 0):
            return   # superseded by a newer plugin toggle
        # Keep the cached conflict data's BSA maps in sync so a later full
        # rebuild path / highlight lookup sees the current winners.
        if getattr(self, "_conflict_data", None) is not None:
            self._conflict_data.bsa_codes = bsa_codes
            self._conflict_data.bsa_overrides = bsa_over
            self._conflict_data.bsa_overridden_by = bsa_overby
            if loose_codes is not None:
                self._conflict_data.loose_codes = loose_codes
        if loose_codes is not None:
            # A changed BSA winner can add/remove a loose-beats-BSA win, so
            # repaint both icon sets together.
            self._modlist_model.set_conflicts(loose_codes,
                                              bsa_conflicts=bsa_codes)
        else:
            self._modlist_model.set_bsa_conflicts(bsa_codes)
        # Cross-panel highlights follow the BSA override maps (loose maps unchanged).
        loose_over = loose_overby = None
        if getattr(self, "_conflict_data", None) is not None:
            loose_over = self._conflict_data.overrides
            loose_overby = self._conflict_data.overridden_by
        self._modlist_view.set_conflict_maps(
            loose_over or {}, loose_overby or {}, bsa_over, bsa_overby)

    def _on_modlist_saved(self, edit_ctx=None):
        """modlist.txt was rewritten (every structural edit funnels here via
        model.save()). edit_ctx classifies the edit (see model.save):
          ("move", moved, crossed) - when the move provably can't have changed
            the filemap, skip EVERYTHING (worker + plugins reload + auto-deploy
            are all no-ops).
          ("toggle", changes) - when a disable provably can't change any
            conflict product, rebuild ONLY the filemap (the toggled mod's
            entries must still leave it) and auto-deploy; skip the scan.
          None/unknown → full rebuild."""
        if edit_ctx is not None:
            kind = edit_ctx[0]
            if kind == "move" and self._move_skips_rebuild(edit_ctx[1:]):
                return
            if kind == "toggle" and self._toggle_skips_conflict_scan(edit_ctx[1]):
                self._rebuild_filemap_light_async()
                return
        self._rebuild_conflicts_async()

    def _move_skips_rebuild(self, move_ctx) -> bool:
        """True when a reorder left every moved mod on the same side of all
        its conflict partners (loose AND archive), so no filemap winner,
        conflict code or override pair can have changed. Archive partners
        matter because orphan BSAs and UE paks rank by MOD priority - a move
        across an archive partner flips that winner (plugin-tied BSAs follow
        plugin order and are unaffected, but they're in the same maps, so
        including them just makes the check more conservative). Disabled mods
        aren't in the maps (empty partners). Sufficiency of the direct-pair
        maps: provider stacks are priority-ordered, so crossing ANY
        co-provider means first crossing a direct neighbour in that stack -
        which IS in the override maps. Conservative: any doubt (stale/missing
        conflict data, unknown mod) → full rebuild. AMM_MOVE_SKIP_REBUILD=0
        kills the fast path."""
        if os.environ.get("AMM_MOVE_SKIP_REBUILD") == "0":
            return False
        # Maps must describe the CURRENT modlist: _conflict_maps_current is
        # armed only when a build's result is accepted and dropped whenever a
        # rebuild is queued (a drag racing an in-flight toggle rebuild would
        # otherwise test against the pre-toggle maps). Skipped moves keep the
        # maps valid - that's exactly what this predicate proves.
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None:
            return False
        moved, crossed = move_ctx
        crossed_set = set(crossed)
        if moved and crossed_set:
            for m in moved:
                partners = ((data.overrides.get(m) or set())
                            | (data.overridden_by.get(m) or set())
                            | (data.bsa_overrides.get(m) or set())
                            | (data.bsa_overridden_by.get(m) or set()))
                if partners & crossed_set:
                    return False
        self._append_log(
            f"[filemap] move: no conflict crossing - rebuild skipped "
            f"(moved={len(moved)}, crossed={len(crossed_set)})")
        return True

    def _toggle_skips_conflict_scan(self, changes) -> bool:
        """True when a toggle batch is disable-only and every disabled mod has
        no loose-conflict partners, no plugin files, no BSA/BA2 archives, no
        framework-exe files and no loose file overriding an archive - then no
        ConflictData product changes: codes/override maps lose nothing (the
        mod's sets were empty), plugin_owner and the plugins panel keep the
        same winners, BSA conflicts follow plugin order + bsa_index
        (untouched), framework statuses can't flip (matching is by exe
        basename - the mod ships none). Only the filemap file set shrinks →
        _rebuild_filemap_light_async. loose_beats_bsa_mods is its own test -
        such a mod appears in none of the other sets yet still flips two icons
        (see ConflictData). Enables are never skipped: the maps can't prove a
        disabled mod won't CREATE conflicts. Capability sets come from the
        same accepted build as the maps, so the _conflict_maps_current guard
        covers them too. AMM_TOGGLE_LIGHT=0 kills the fast path."""
        if os.environ.get("AMM_TOGGLE_LIGHT") == "0":
            return False
        if not getattr(self, "_conflict_maps_current", False):
            return False
        data = self._conflict_data
        if data is None or not changes:
            return False
        for name, enabled in changes:
            if enabled:
                return False
            if name not in data.overrides and name not in data.overridden_by:
                return False   # unknown to the maps → treat as stale
            if data.overrides.get(name) or data.overridden_by.get(name):
                return False
            if (name in data.plugin_mods or name in data.bsa_mods
                    or name in data.framework_file_mods
                    or name in data.loose_beats_bsa_mods):
                return False
        self._append_log(
            f"[filemap] toggle: disable-only, no conflicts/plugins/BSAs - "
            f"conflict scan skipped ({len(changes)} mod(s), filemap-only)")
        return True

    def _rebuild_filemap_light_async(self):
        """Disable fast path: rebuild ONLY the filemap (the incremental delta
        removes the toggled mods' entries) - _toggle_skips_conflict_scan proved
        every conflict product unchanged, so skip the scan, the panel reloads
        and the conflict-map repaints. Shares the build lock + generation with
        _rebuild_conflicts_async so full rebuilds serialize/supersede normally;
        _conflict_maps_current deliberately stays armed (the maps still
        describe reality - that is exactly what the predicate proved)."""
        import threading
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()
        g = self._gs.game
        profile = self._gs.profile
        if g is None or not profile:
            return

        def worker():
            from Utils.perftrace import span
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    return   # superseded while waiting - the newer build covers us
                from Utils.deploy_pipeline import _build_filemap_for_game

                def _fm_log(m):
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")

                try:
                    with span("build_filemap(light)"):
                        _build_filemap_for_game(
                            g, profile, log_fn=_fm_log, rescan_index=False)
                except Exception as exc:
                    print(f"[gui_qt] light filemap rebuild failed: {exc}",
                          flush=True)
            self._filemap_light_done.emit(gen)

        threading.Thread(target=worker, daemon=True).start()

    def _on_filemap_light_done(self, gen: int):
        """UI thread: a filemap-only rebuild finished. Refresh only what the
        deployed file SET touches - conflict maps, plugins panel, filter data
        and framework banner are untouched by construction."""
        if gen != self._conflict_gen:
            return   # superseded by a full rebuild - its handler covers this
        from Utils.perftrace import span
        with span("on_filemap_light_done"):
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            self._refresh_modlist_stats()
            # Active filters may key on the enabled state - reapply from the
            # in-memory FilterData (no disk rescan needed).
            self._apply_modlist_filters()
        self._maybe_auto_deploy()

    def _rebuild_conflicts_async(self, rescan_index: bool = False):
        """Build the filemap off-thread; the worker emits _conflicts_ready
        (queued → UI thread). A generation counter drops results from a
        superseded reload (user switched game before the build finished).
        rescan_index=True forces a full disk rescan (Refresh button)."""
        import threading
        # The maps the move fast-path tests are about to be superseded.
        self._conflict_maps_current = False
        self._reassert_profile_paths()
        gen = getattr(self, "_conflict_gen", 0) + 1
        self._conflict_gen = gen
        # A requested full rescan must SURVIVE supersession. Previously
        # rescan_index lived only in this call's worker closure: if a Refresh's
        # rescan build (rescan_index=True) was superseded - e.g. by the enable
        # toggle the user does immediately after Refresh, which triggers a plain
        # rescan_index=False rebuild - the Refresh worker returned early WITHOUT
        # rescanning, and the superseding build reused the STALE index. The
        # just-added mod then never entered modindex.bin, so it dropped out of
        # filemap.txt (no conflicts / plugins / Data-tab) until some later full
        # rescan happened to win the race (or a different tool - e.g. the Tk
        # build, which always rescans - rewrote the index on disk). Latch the
        # request on the window so whichever build actually runs consumes it.
        if rescan_index:
            self._pending_rescan_index = True
        # Serialize the actual build: rapid triggers (e.g. a Mod Files edit while
        # a previous rescan is still running) otherwise run two rebuild_mod_index
        # writes concurrently and collide on modindex.bin.tmp. The generation
        # check still drops superseded RESULTS.
        if not hasattr(self, "_conflict_build_lock"):
            self._conflict_build_lock = threading.Lock()

        from Utils.perftrace import span

        def worker():
            # Diagnostics below go to BOTH stderr and the GUI log panel
            # (_append_log, thread-safe) so a normal user can see + send them
            # without relaunching with an env var. They fire once per conflict
            # build / supersession (not per-frame), so they're low-noise.
            with self._conflict_build_lock:
                if gen != self._conflict_gen:
                    msg = (f"conflict build gen={gen} SUPERSEDED "
                           f"(current={self._conflict_gen}) - skipped before "
                           f"build; requested rescan={rescan_index}, "
                           f"latch pending={getattr(self, '_pending_rescan_index', False)}")
                    print(f"[plugin-diag] {msg}", flush=True)
                    self._append_log(f"[rescan-diag] {msg}")
                    return   # a newer build was queued while we waited - skip
                # Honour a latched rescan request even if THIS build was queued
                # as rescan_index=False (it superseded a pending Refresh). The
                # flag is NOT cleared here - it's cleared in _on_conflicts_ready
                # only when this build's result is actually ACCEPTED. That way,
                # if this rescan is itself superseded mid-build (its result
                # dropped by the gen check), the request stays latched for the
                # next build instead of being lost.
                _latched = getattr(self, "_pending_rescan_index", False)
                do_rescan = rescan_index or _latched
                # Record what this gen's build did, so _on_conflicts_ready can
                # clear the latch only when a rescanning build is accepted.
                self._conflict_gen_did_rescan = (gen, do_rescan)
                msg = (f"build gen={gen}: requested rescan={rescan_index}, "
                       f"latched={_latched} → doing rescan={do_rescan}")
                print(f"[plugin-diag] {msg}", flush=True)
                self._append_log(f"[rescan-diag] {msg}")
                def _fm_log(m):
                    # Console for our own tracing + the GUI log panel so a user
                    # actually SEES filemap warnings (enabled-mod-not-indexed,
                    # symlink skips, name mismatches). _append_log is thread-safe
                    # (marshals to the UI thread) and classifies WARN severity.
                    # Escape surrogate bytes from on-disk names FIRST - printing
                    # them raw raises UnicodeEncodeError on strict-UTF-8 stdout,
                    # which aborted the index rescan this log_fn serves.
                    m = str(m)
                    try:
                        m.encode("utf-8")
                    except UnicodeEncodeError:
                        m = m.encode("utf-8", "backslashreplace").decode(
                            "utf-8", "replace")
                    print(f"[filemap] {m}", flush=True)
                    self._append_log(f"[filemap] {m}")
                with span(f"build_conflicts(rescan={do_rescan})"):
                    data = self._gs.build_conflicts(
                        log_fn=_fm_log,
                        rescan_index=do_rescan)
                # Decisive check: after the build, is every ENABLED modlist mod
                # actually present in the index the filemap was built from? An
                # enabled mod missing here is THE failure (no conflicts/plugins).
                self._log_enabled_not_indexed(gen)
            self._conflicts_ready.emit(gen, data)

        threading.Thread(target=worker, daemon=True).start()

    def _log_enabled_not_indexed(self, gen: int) -> None:
        """Diagnostic (off-thread): report any ENABLED modlist mod that is
        absent from modindex.bin after a build. An enabled mod missing from the
        index is the exact failure behind "no conflicts / no plugins / not in
        Data tab" - it isolates whether the cause is a stale index (rescan race:
        mod on disk + in modlist but not indexed) vs. a name/scan issue
        (near-match key) vs. all-good. Reads the index once per build."""
        try:
            from Utils.modlist import read_modlist
            from Utils.filemap import read_mod_index, OVERWRITE_NAME, ROOT_FOLDER_NAME
            staging = self._gs.staging_dir()
            ml = self._gs.modlist_path()
            if staging is None or ml is None or not ml.is_file():
                return
            index_path = staging.parent / "modindex.bin"
            index = read_mod_index(index_path) or {}
            # Surface the RESOLVED index path + any RIVAL modindex.bin at the
            # OTHER convention (shared <profile_root>/ vs profile-specific
            # <profile_dir>/). Two files means the staging-path resolution
            # flip-flopped (stale _active_profile_dir) and reads/writes are
            # hitting different indexes - the "two modindex.bin" bug.
            self._append_log(
                f"[rescan-diag] gen={gen}: index path = {index_path} "
                f"(exists={index_path.is_file()})")
            try:
                canon = index_path.resolve()
                pd = self._gs.profile_dir()
                stray = (pd / "modindex.bin") if pd is not None else None
                if (stray is not None and stray.is_file()
                        and stray.resolve() != canon):
                    self._append_log(
                        f"[rescan-diag] gen={gen}: WARNING - stray modindex.bin "
                        f"at {stray} describes the same shared mods folder as the "
                        f"canonical index ({index_path}); it is a legacy leftover "
                        f"and should be removed (a Refresh sweep removes it).")
            except Exception:
                pass
            enabled = [e.name for e in read_modlist(ml)
                       if e.enabled and not e.is_separator
                       and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]
            missing = []
            for name in enabled:
                if name in index:
                    continue
                on_disk = (staging / name).is_dir()
                near = [k for k in index if k.strip().casefold() == name.strip().casefold()]
                missing.append((name, on_disk, near))
            if not missing:
                self._append_log(
                    f"[rescan-diag] gen={gen}: OK - all {len(enabled)} enabled "
                    f"mod(s) present in modindex.bin ({len(index)} indexed)")
                return
            self._append_log(
                f"[rescan-diag] gen={gen}: {len(missing)} ENABLED mod(s) MISSING "
                f"from modindex.bin (index has {len(index)} mod(s)):")
            for name, on_disk, near in missing[:20]:
                if near:
                    reason = f"NAME MISMATCH - index has {near!r}"
                elif on_disk:
                    reason = ("folder EXISTS on disk but NOT indexed → STALE INDEX "
                              "(rescan race / rescan skipped)")
                else:
                    reason = "folder not found on disk"
                self._append_log(f"[rescan-diag]   {name!r}: {reason}")
        except Exception as exc:
            print(f"[plugin-diag] _log_enabled_not_indexed error: {exc}", flush=True)

    def _on_conflicts_ready(self, gen: int, data):
        if gen != self._conflict_gen:
            msg = (f"conflicts_ready gen={gen} SUPERSEDED "
                   f"(current={self._conflict_gen}) - result dropped, plugins "
                   f"NOT reloaded from this build")
            print(f"[plugin-diag] {msg}", flush=True)
            self._append_log(f"[rescan-diag] {msg}")
            return
        # This build's result is accepted. If it performed the latched full
        # rescan, clear the latch now (a fresh, correct modindex.bin is on disk).
        # A rescan that was superseded mid-build never reaches here, so its
        # latch survives for the next build - the request can't be lost.
        _did = getattr(self, "_conflict_gen_did_rescan", None)
        if _did is not None and _did[0] == gen and _did[1]:
            self._pending_rescan_index = False
        from Utils.perftrace import span
        # Profile-switch milestone: the conflict/filemap build (usually the
        # long pole) has landed; the reload_plugins below is the final pass.
        if getattr(self, "_switch_t0", None) is not None:
            self._mark_since_switch("switch→conflicts_applied")  # i18n: skip - perftrace marker label
            self._switch_conflicts_done = True
        with span("on_conflicts_ready"):
            self._conflict_data = data
            # These maps now describe the on-disk modlist - arm the move
            # fast-path (see _move_skips_rebuild). Only valid if no newer
            # rebuild was queued meanwhile; the gen check above ensures that.
            self._conflict_maps_current = True
            with span("model.set_filemap_results"):
                # Conflicts + the filemap-derived flag overlays (info=pre-RTX,
                # root=custom root rule) in one dataChanged pass.
                self._modlist_model.set_filemap_results(
                    data.loose_codes, data.bsa_codes,
                    getattr(data, "prertx_mods", set()),
                    getattr(data, "root_rule_mods", set()),
                    uuid_conflicts=getattr(data, "uuid_codes", None))
            # Cross-panel highlighting needs the override + owner maps.
            with span("view.set_conflict_maps"):
                self._modlist_view.set_conflict_maps(
                    data.overrides, data.overridden_by,
                    data.bsa_overrides, data.bsa_overridden_by)
            self._plugin_view.set_plugin_owner(data.plugin_owner)
            # Rebuild the filter data + repopulate the filter panel's dynamic lists,
            # then reapply whatever filters are currently active.
            with span("rebuild_filter_data"):
                self._rebuild_filter_data()
            # The deployed filemap changed → the Data tab is stale (rebuilds lazily).
            if hasattr(self, "_data_view"):
                self._data_view.mark_dirty()
            # A mod may have been added/removed → re-evaluate Install vs Reinstall.
            if hasattr(self, "_downloads_view"):
                self._downloads_view.mark_dirty()
            # The deployed file set changed → the Text Files list is stale.
            if hasattr(self, "_text_files_view"):
                self._text_files_view.mark_dirty()
            # A mod may have been removed/added → re-evaluate the Nexus browser's
            # Install/Reinstall buttons (remove goes through save()→conflict rebuild,
            # which is the only signal the Nexus tab gets for a removal).
            nv = getattr(self, "_nexus_view", None)
            if nv is not None:
                nv.refresh_installed()
            tv = getattr(self, "_thunderstore_view", None)
            if tv is not None:
                tv.refresh_installed()
            # The filemap (staged/deployed file set) changed → framework states may
            # have flipped (e.g. a framework mod toggled, deployed, or removed).
            # Precomputed on the conflict worker (detect_frameworks re-reads
            # filemap.txt - too slow here); bump the gen so an in-flight async
            # detect can't overwrite this fresher result. set_statuses is a
            # no-op when the rows are unchanged, so the repeated refreshes a
            # deploy/restore fires don't rebuild (and flicker) the banner.
            if hasattr(self, "_framework_banner"):
                self._framework_gen += 1
                self._framework_banner.set_statuses(data.framework_statuses)
            # A mod was toggled / added / removed → the footer counts + size are
            # stale (token-guarded, so overlapping walks coalesce).
            with span("refresh_modlist_stats"):
                self._refresh_modlist_stats()
            # A deploy/restore/install writes into overwrite/ (and Root_Folder)
            # without a modlist reload → re-count for the boundary rows' "(N)".
            self._refresh_boundary_counts()
            # The filemap is now fresh → reload the Plugins tab so ESL/master flags
            # resolve against the winning (e.g. patcher-provided light) copies and
            # plugins still deployed by another enabled mod are recovered. Tk parity:
            # gui.py _on_filemap_rebuilt calls _refresh_plugins_tab() here, AFTER the
            # rebuild - reloading earlier (on the toggle) races the stale filemap.
            with span("reload_plugins"):
                self._reload_plugins()
        self._maybe_auto_deploy()

    def _maybe_auto_deploy(self):
        """Auto deploy: if the game has auto_deploy enabled, deploy after every
        successful conflict/filemap rebuild (enable/disable/reorder/install) -
        but NOT when the rebuild was itself triggered by that auto-deploy
        (deploy → _reload_modlist → rebuild → here again), or we'd loop.
        Tk parity: gui.py _on_filemap_rebuilt. Called from _on_conflicts_ready
        AND _on_filemap_light_done (a disabled mod's files must still leave
        the game folder)."""
        if self._auto_deploy_in_progress:
            self._auto_deploy_in_progress = False
        else:
            game = self._gs.game
            if (game is not None and game.is_configured()
                    and getattr(game, "auto_deploy", False)
                    and hasattr(game, "deploy")
                    and not self._deploy_running
                    # Never auto-deploy while an install batch is mid-flight -
                    # the deploy swaps the shared game object's active profile
                    # and races the install worker's path resolution. The
                    # install's own _on_install_done reload re-triggers this.
                    and not getattr(self, "_install_running", False)
                    and not self._col_install_running
                    # Same hazard for a detached-wizard staging job in flight -
                    # _on_wizard_finish_done's reload re-triggers this.
                    and not getattr(self, "_staged_finish_running", False)
                    and not self._staged_finish_queue):
                self._auto_deploy_in_progress = True
                self._on_deploy(silent=True)

    # ----------------------------------------------------------------- right
    def _build_plugins(self) -> QWidget:
        return self._plugins_placeholder()

    def _play_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        # Exe selector stretches with the plugins panel: its left edge hugs the
        # splitter separator, so dragging the split resizes the dropdown while
        # Play + gear stay fixed at the right edge. Items = the game + manually
        # added custom exes, plus pickers fed by a staging scan (mod tools +
        # wizard tools installed under the profile) and a game-folder scan
        # (tools that must run from the game root, incl. deployed script
        # extenders - those are no longer listed automatically).
        self._play_exe_selector = SelectorButton(
            items=["-"],
            current="-",
            min_width=0,
            icon_px=28,   # bigger game/exe icon on the play-bar face + menu
            on_select=self._on_play_exe_selected,
            actions=[
                (self.tr("+ Add custom EXE…"), self._on_add_custom_exe),
                (self.tr("+ Add exe from staging…"), self._on_add_exe_from_staging),
                (self.tr("+ Add exe from game folder…"),
                 self._on_add_exe_from_game_folder),
            ],
        )
        self._play_exe_selector.setFixedHeight(self._BTN_H)
        self._play_exe_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        h.addWidget(self._play_exe_selector, 1)
        self._play_exe_paths: dict[str, Path] = {}
        self._play_auto_exe_names: set[str] = set()

        # ▶ Play - plain fixed-size button (label flips to "Run" for exes).
        self._play_btn = QPushButton(self.tr("▶  Play"))
        self._play_btn.setObjectName("PlayButton")
        self._play_btn.setFixedHeight(self._BTN_H)
        self._play_btn.setCursor(Qt.PointingHandCursor)
        self._play_btn.clicked.connect(self._on_play)
        h.addWidget(self._play_btn)

        # Exe menu - a hamburger (not a gear, to avoid reading as a second
        # settings button next to the header's gear). Its Settings action
        # depends on the dropdown selection (game → launcher settings overlay,
        # exe → per-exe settings tab).
        self._exe_settings_btn = SelectorButton(
            items=[],
            icon=hamburger_icon(self._ICON_PX, color=_c(self._pal, "TEXT_MAIN")),
            icon_px=self._ICON_PX,
            actions=[
                (self.tr("Settings"), lambda: self._on_play_action("settings")),
                (self.tr("Open application folder"), lambda: self._on_play_action("folder")),
            ],
        )
        self._exe_settings_btn.setFixedSize(self._BTN_H, self._BTN_H)
        h.addWidget(self._exe_settings_btn)
        return bar

    def _plugins_placeholder(self) -> QWidget:
        from PySide6.QtWidgets import QStackedWidget
        from gui_qt.plugin_model import PluginModel
        from gui_qt.plugin_view import PluginView

        frame = QFrame()
        frame.setObjectName("PlaceholderPane")
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Sub-tab strip - switches the stacked pages below.
        self._plugin_tab_names = [self.tr("Plugins"), self.tr("Mod Files"),
                                  self.tr("Text Files"), self.tr("Data"),
                                  self.tr("Downloads"), self.tr("Overrides"),
                                  self.tr("Saves")]
        self._plugin_stack = QStackedWidget()

        # Page 0: the real Plugins view, with a framework-status banner above the
        # columns (one colored row per framework the game declares).
        self._plugin_model = PluginModel()
        # A plugin reorder/toggle changes BSA load order (BSAs load at their
        # plugin's position), so recompute BSA conflicts when the order is saved.
        # BSA-ONLY: a plugin toggle doesn't touch the deployed file set, so the
        # filemap/modindex, loose conflicts and the Plugins panel itself are all
        # unaffected - recompute just the BSA winners (re-reads the freshly-
        # written loadorder.txt) instead of a full filemap rebuild + modlist/
        # plugins reload. Tk parity: recompute_bsa_conflicts.
        self._plugin_model.order_changed.connect(
            self._recompute_bsa_conflicts_async)
        self._plugin_model.save_failed.connect(
            lambda msg: self._notify(msg, "error"))
        self._plugin_view = PluginView(self._plugin_model)
        # Search/filter hidden sets are row-indexed, so a column sort (or any
        # reorder) leaves them misaligned - the view's own handler only
        # re-applies the STALE indices. Recompute both from the current display
        # order. modelReset is covered by _reload_plugins' explicit reapply.
        for sig in (self._plugin_model.layoutChanged,
                    self._plugin_model.rowsMoved,
                    self._plugin_model.rowsInserted,
                    self._plugin_model.rowsRemoved):
            sig.connect(self._on_plugin_layout_changed)
        from gui_qt.framework_banner import FrameworkBanner
        self._framework_banner = FrameworkBanner()
        self._plugin_stack.addWidget(self._plugin_view)
        # Page 1: the real Mod Files view.
        from gui_qt.mod_files_view import ModFilesView
        self._mod_files_view = ModFilesView()
        self._mod_files_view.changed.connect(self._on_mod_files_changed)
        self._mod_files_view.on_open_image = self._open_image_preview_tab
        self._mod_files_view.on_open_archive = self._open_bsa_preview_tab
        self._mod_files_view.on_open_nif = self._open_nif_preview_tab
        self._mod_files_view.on_open_text = self._open_text_editor_tab
        self._plugin_stack.addWidget(self._mod_files_view)
        # Page 2: the real Text Files view.
        from gui_qt.text_files_view import TextFilesView
        self._text_files_view = TextFilesView()
        self._text_files_view.on_open_file = self._open_text_editor_tab
        self._plugin_stack.addWidget(self._text_files_view)
        # Page 3: the real Data view.
        from gui_qt.data_view import DataView
        self._data_view = DataView()
        self._data_view.on_select_mod = self._on_data_select_mod
        self._plugin_stack.addWidget(self._data_view)
        # Page 4: the real Downloads view.
        from gui_qt.downloads_view import DownloadsView
        self._downloads_view = DownloadsView()
        self._downloads_view.on_install = \
            lambda paths: self._install_paths(paths, clear_archives=False)
        self._downloads_view.selection_changed.connect(
            self._update_downloads_footer)
        self._plugin_stack.addWidget(self._downloads_view)
        # Page 5: the BG3 Overrides view (override paks - tab shown only for
        # games with has_override_pak_tab; the label is repositioned below so
        # it renders where the hidden Plugins tab sits).
        from gui_qt.override_view import OverridesView
        self._overrides_view = OverridesView()
        self._overrides_view.changed.connect(self._on_mod_files_changed)
        # Pak row selected → orange its owning mod in the modlist + marker
        # strip (same anchor-highlight path the Plugins/Data tabs use).
        self._overrides_view.on_select_mod = self._on_data_select_mod
        self._plugin_stack.addWidget(self._overrides_view)
        # Page 6: the Saves view (Ludusavi-resolved save folders, read-only).
        from gui_qt.saves_view import SavesView
        self._saves_view = SavesView(log_fn=self._append_log)
        self._plugin_stack.addWidget(self._saves_view)
        self._TEXT_FILES_TAB_IDX = 2
        self._DATA_TAB_IDX = 3
        self._DOWNLOADS_TAB_IDX = 4
        self._OVERRIDES_TAB_IDX = 5
        self._SAVES_TAB_IDX = 6

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        # Centre the tab strip within the panel (stretch on both sides).
        tabs.addStretch(1)
        self._plugin_tab_labels = []
        for i, t in enumerate(self._plugin_tab_names):
            lbl = QLabel(t)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _e, idx=i: self._select_plugin_tab(idx)
            self._plugin_tab_labels.append(lbl)
        # Layout in display order: the Overrides label leads the strip (where
        # Plugins sits - the two are never visible together), and Saves sits
        # next to Text Files rather than at the end. List indices are unchanged
        # so the click closures, styling loop and stack pages keep their
        # numbering; only the visual order differs.
        for i in [self._OVERRIDES_TAB_IDX, 0, 1, self._TEXT_FILES_TAB_IDX,
                  self._SAVES_TAB_IDX, self._DATA_TAB_IDX,
                  self._DOWNLOADS_TAB_IDX]:
            tabs.addWidget(self._plugin_tab_labels[i])
        self._plugin_tab_labels[self._OVERRIDES_TAB_IDX].setVisible(False)
        tabs.addStretch(1)
        # Framework-status banner ABOVE the tabs so it's visible on every
        # sub-tab (one colored row per framework the game declares).
        v.addWidget(self._framework_banner)
        v.addLayout(tabs)
        v.addWidget(self._plugin_stack, 1)
        self._select_plugin_tab(0)
        return frame

    def _select_plugin_tab(self, idx: int):
        # Plugin-less games have no Plugins tab - route to the Overrides tab
        # (BG3) when it's shown, else to Downloads.
        if idx == 0 and getattr(self, "_plugins_tab_hidden", False):
            idx = (getattr(self, "_OVERRIDES_TAB_IDX", 5)
                   if getattr(self, "_overrides_tab_shown", False)
                   else getattr(self, "_DOWNLOADS_TAB_IDX", 4))
        self._plugin_stack.setCurrentIndex(idx)
        # Swap the column footer to match the active sub-tab. Footer pages:
        # 0 plugins / 1 Mod Files / 2 Data / 3 Downloads / 4 Text Files /
        # 5 Overrides.
        fstack = getattr(self, "_plugin_footer_stack", None)
        tf_idx = getattr(self, "_TEXT_FILES_TAB_IDX", 2)
        data_idx = getattr(self, "_DATA_TAB_IDX", 3)
        dl_idx = getattr(self, "_DOWNLOADS_TAB_IDX", 4)
        ov_idx = getattr(self, "_OVERRIDES_TAB_IDX", 5)
        sv_idx = getattr(self, "_SAVES_TAB_IDX", 6)
        if fstack is not None:
            fstack.setCurrentIndex(
                1 if idx == 1 else 2 if idx == data_idx
                else 3 if idx == dl_idx else 4 if idx == tf_idx
                else 5 if idx == ov_idx else 6 if idx == sv_idx else 0)
            # Plugin-less games hide the plugin tools footer on the Plugins tab.
            self._refresh_plugin_footer_visibility()
        # Deferred build: only (re)build a tab's contents when it's shown.
        dv = getattr(self, "_data_view", None)
        if dv is not None:
            dv.set_visible_tab(idx == data_idx)
        dlv = getattr(self, "_downloads_view", None)
        if dlv is not None:
            dlv.set_visible_tab(idx == dl_idx)
            if idx == dl_idx:
                self._update_downloads_footer()
        tfv = getattr(self, "_text_files_view", None)
        if tfv is not None:
            tfv.set_visible_tab(idx == tf_idx)
        ovv = getattr(self, "_overrides_view", None)
        if ovv is not None:
            ovv.set_visible_tab(idx == ov_idx)
        svv = getattr(self, "_saves_view", None)
        if svv is not None:
            svv.set_visible_tab(idx == sv_idx)
        for i, lbl in enumerate(self._plugin_tab_labels):
            sel = i == idx
            # Theme foreground (dim when unselected) so the tab strip reads on
            # the light panel too; selected tab gets the accent underline.
            col = _c(self._pal, "TEXT_MAIN" if sel else "TEXT_DIM")
            lbl.setStyleSheet(
                f"padding:4px 8px; color:{col};" + (
                    f"border-bottom:2px solid {_c(self._pal,'ACCENT')};"
                    if sel else ""))

    # --------------------------------------------------------------- widgets
    def _action_button(self, text: str, icon_name: str,
                       compact: bool = False,
                       tint: str | None = None) -> QToolButton:
        """Flat toolbar-style button with icon + label (mockup look).
        *compact* uses the smaller footer icon size. *tint* recolours a mono
        glyph to a theme colour (like the Settings icon)."""
        px = self._FOOT_ICON_PX if compact else self._ICON_PX
        b = QToolButton()
        b.setText(text)
        b.setIcon(icon(icon_name, px, color=tint))
        b.setIconSize(QSize(px, px))
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _populate_menu(self, menu: "QMenu", items: "list[tuple]") -> None:
        """Fill *menu* from *items*. Each entry is None (separator) or
        (label, callback) - where callback may be a list of (label, callback)
        pairs, which becomes a submenu. An optional third element is a stable
        key; the created QAction is recorded under it in ``_menu_actions`` so
        entries that come and go per game can be toggled later without having
        to match on their translated label."""
        for entry in items:
            if entry is None:
                menu.addSeparator()
                continue
            label, cb = entry[0], entry[1]
            key = entry[2] if len(entry) > 2 else None
            if isinstance(cb, list):
                sub = menu.addMenu(label)
                self._populate_menu(sub, cb)
                act = sub.menuAction()
            elif cb is not None:
                act = menu.addAction(label)
                act.triggered.connect(lambda _=False, c=cb: c())
            else:
                act = menu.addAction(label)   # inert label (placeholder)
            if key:
                if not hasattr(self, "_menu_actions"):
                    self._menu_actions = {}
                self._menu_actions[key] = act

    def _menu_action_button(self, text: str, icon_name: str,
                            items: "list[tuple]",
                            tint: str | None = None) -> QToolButton:
        """Like _action_button but a split button with a dropdown menu.
        *items* is a list of (label, callback|None); None inserts a separator.
        If the second element is a list of (label, callback) pairs it becomes a
        submenu instead. Highlights (button + arrow) while the menu is open via
        the `menuOpen` property, mirroring SelectorButton."""
        b = self._action_button(text, icon_name, tint=tint)
        b.setProperty("split", True)
        b.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(b)
        self._populate_menu(menu, items)
        b.setMenu(menu)
        # Open on press (not release), and set menuOpen BEFORE the synchronous
        # sunken repaint (SplitPressHighlighter) so body + arrow light together
        # instead of the arrow lagging one menu-build behind - mirrors
        # SelectorButton. Matters most for Wizard, whose menu rebuild on
        # aboutToShow probes the filesystem.
        b.pressed.connect(b.showMenu)
        b.installEventFilter(SplitPressHighlighter(b))

        def _set_open(on):
            b.setProperty("menuOpen", on)
            b.style().unpolish(b); b.style().polish(b)
        menu.aboutToShow.connect(lambda: _set_open(True))
        menu.aboutToHide.connect(lambda: _set_open(False))
        b._menu = menu
        return b

    def _search_icon_label(self, tooltip: str = "") -> QLabel:
        """A small magnifying-glass label placed just left of a search box, so
        every panel/tab search bar reads the same as the modlist footer."""
        lbl = QLabel()
        lbl.setPixmap(icon("search.png", 18).pixmap(18, 18))
        lbl.setAlignment(Qt.AlignCenter)
        if tooltip:
            lbl.setToolTip(tooltip)
        return lbl

    def _text_button(self, text: str, compact: bool = False) -> QToolButton:
        """Flat text-only button (same chrome as the action buttons)."""
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b.setObjectName("FooterButton" if compact else "ActionButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _color_button(self, text: str, color: str,
                      compact: bool = False,
                      active_color: str | None = None) -> QPushButton:
        """Solid colored button (plugin tools, matching the Tk app).

        *active_color* adds an ``[active="true"]`` fill so the button can light
        up in a second colour (the Filters footer buttons use it)."""
        pad = "4px 10px" if compact else "6px 14px"
        fs = "12px" if compact else "14px"
        fg = contrast_text(color)   # black or white - whichever reads on the fill
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        sheet = (
            f"QPushButton{{background:{color}; color:{fg}; border:none;"
            f" padding:{pad}; border-radius:4px; font-size:{fs};"
            f" font-weight:600;}}"
            f"QPushButton:hover{{background:{color};}}")
        if active_color:
            # The :hover rule above also matches while active, so the active
            # fill needs its own hover rule to win (equal specificity, later).
            afg = contrast_text(active_color)
            sheet += (
                f'QPushButton[active="true"]{{background:{active_color};'
                f' color:{afg};}}'
                f'QPushButton[active="true"]:hover{{background:{active_color};'
                f' color:{afg};}}')
        b.setStyleSheet(sheet)
        return b

    def _filters_button(self) -> QPushButton:
        """A footer "Filters" button: blue at rest, theme purple (BTN_PURPLE)
        while that panel has a filter set or its search box holds a query.
        _sync_filters_btn flips the `active` property that swaps the fill."""
        b = self._color_button(
            self.tr("Filters"), _c(self._pal, "BTN_INFO"), compact=True,
            active_color=_c(self._pal, "BTN_PURPLE"))
        b.setFixedHeight(self._FOOT_BTN_H)
        b.setProperty("active", False)
        return b

    def _equalize_button_widths(self, *buttons) -> None:
        """Give every button the width of the widest one so a row of related
        buttons (e.g. Pack/Unpack BSA) lines up regardless of label length."""
        w = max((b.sizeHint().width() for b in buttons), default=0)
        for b in buttons:
            b.setMinimumWidth(w)

    def _group_sep(self) -> QFrame:
        s = QFrame()
        s.setFrameShape(QFrame.VLine)
        s.setObjectName("GroupSep")
        s.setFixedWidth(2)
        return s

    # ------------------------------------------------------ log control bar
    def _build_log_bar(self) -> QWidget:
        """Fixed full-width control bar below the log splitter. The 'Log' button
        toggles the (drag-resizable) log text area above. Error/Warning/Clear
        controls only appear while the log is open."""
        bar = QWidget()
        bar.setObjectName("LogBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(12)

        self._log_toggle = self._text_button(self.tr("Log"), compact=True)
        self._log_toggle.clicked.connect(self._toggle_log)
        h.addWidget(self._log_toggle)

        # These only show when the log is open. The Error/Warning entries are
        # clickable toggles: clicking one filters the log to show ONLY lines of
        # that severity; clicking the active one again clears the filter and
        # shows everything again. Only one severity filter can be active at a
        # time. ``_log_filter`` is None (show all), "error", or "warning".
        self._log_filter = None
        self._errors_lbl = QLabel(self.tr("● Errors"))
        self._errors_lbl.setCursor(Qt.PointingHandCursor)
        self._errors_lbl.mousePressEvent = lambda e: self._toggle_log_filter("error")
        h.addWidget(self._errors_lbl)
        self._warnings_lbl = QLabel(self.tr("● Warnings"))
        self._warnings_lbl.setCursor(Qt.PointingHandCursor)
        self._warnings_lbl.mousePressEvent = lambda e: self._toggle_log_filter("warning")
        h.addWidget(self._warnings_lbl)
        self._refresh_log_filter_labels()
        self._open_log_tab_btn = self._text_button(self.tr("Open as tab"), compact=True)
        self._open_log_tab_btn.clicked.connect(self._open_log_tab)
        h.addWidget(self._open_log_tab_btn)
        self._clear_log_btn = self._text_button(self.tr("Clear Log"), compact=True)
        self._clear_log_btn.clicked.connect(self._clear_log)
        h.addWidget(self._clear_log_btn)
        self._open_logs_btn = self._text_button(self.tr("Open Log Folder"), compact=True)
        self._open_logs_btn.clicked.connect(self._open_logs_folder)
        h.addWidget(self._open_logs_btn)

        h.addStretch(1)

        # Wiki button - opens the project's GitHub wiki as a detachable tab,
        # fetched live so it always matches what is published on GitHub.
        self._wiki_btn = self._text_button(self.tr("Wiki"), compact=True)
        self._wiki_btn.clicked.connect(self._open_wiki_tab)
        h.addWidget(self._wiki_btn)

        # Changelog button - sits just left of the Nexus info; opens the
        # bundled Changelog.txt as a detachable tab.
        self._changelog_btn = self._text_button(self.tr("Changelog"), compact=True)
        self._changelog_btn.clicked.connect(self._open_changelog_tab)
        h.addWidget(self._changelog_btn)

        # GitHub / Ko-Fi / Endorse - colored buttons mirroring the Tk status
        # bar. GitHub + Ko-Fi open external links; Endorse endorses the AMM
        # Nexus page (site mod 1714) via the shared Nexus API.
        self._github_btn = self._text_button(self.tr("Github"), compact=True)
        self._github_btn.clicked.connect(self._open_github)
        h.addWidget(self._github_btn)

        self._kofi_btn = self._color_button(
            self.tr("Ko-Fi"), _c(self._pal, "BTN_PURPLE"), compact=True)
        self._kofi_btn.clicked.connect(self._open_kofi)
        h.addWidget(self._kofi_btn)

        self._endorse_amm_btn = self._color_button(
            self.tr("♥ Endorse AMM"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._endorse_amm_btn.clicked.connect(self._endorse_amm)
        h.addWidget(self._endorse_amm_btn)

        # Both are opt-out via UI settings; applied here and re-applied live
        # whenever the setting is toggled.
        self._apply_support_button_visibility()

        # Nexus username at the far right; hover shows API rate-limit usage.
        from gui_qt.nexus_footer import NexusFooterLabel
        self._nexus_footer = NexusFooterLabel(lambda: getattr(self, "_nexus_api", None))
        h.addWidget(self._nexus_footer)

        self._log_open_widgets = [self._errors_lbl, self._warnings_lbl,
                                  self._open_log_tab_btn, self._clear_log_btn,
                                  self._open_logs_btn]
        for w in self._log_open_widgets:
            w.setVisible(False)
        return bar

    def _log_is_open(self) -> bool:
        return len(self._vsplit.sizes()) > 1 and self._vsplit.sizes()[1] > 0

    def _toggle_log(self):
        if self._log_is_open():
            self._vsplit.setSizes([self._vsplit.height(), 0])     # collapse
        else:
            total = self._vsplit.height()
            self._vsplit.setSizes([total - 180, 180])             # open ~180px
            self._log_view.moveCursor(QTextCursor.End)            # scroll to newest
        self._sync_log_controls()

    def _sync_log_controls(self):
        """Error/Warning/Clear controls are visible only while the log has
        height - whether opened by the button or dragged open/closed (Tk feel)."""
        open_ = self._log_is_open()
        for w in self._log_open_widgets:
            w.setVisible(open_)

    @staticmethod
    def _classify_log_line(line: str) -> str:
        """Return the severity of a log line: 'error', 'warning' or ''.

        Free-form backend messages are classified by keyword so error lines can
        render red and warnings yellow."""
        low = line.lower()
        if any(k in low for k in
               ("error", "failed", "failure", "fatal", "exception",
                "traceback", "[err]", "could not", "cannot ", "can't")):
            return "error"
        if any(k in low for k in ("warn", "[warning]", "deprecat")):
            return "warning"
        return ""

    def _log_line_html(self, line: str, severity: str, timestamp: str = "") -> str:
        """Escaped, colour-tinted HTML for a single log line.

        *timestamp* (already formatted, e.g. ``2026-07-04 18:00:34``) is rendered
        as a dimmed prefix so each on-screen line shows when it was logged."""
        from html import escape
        text = escape(line) or "&nbsp;"
        prefix = ""
        if timestamp:
            dim = _c(self._pal, "TEXT_DIM")
            prefix = f'<span style="color:{dim};">[{escape(timestamp)}]</span>  '
        if severity == "error":
            color = _c(self._pal, "TEXT_ERR")
        elif severity == "warning":
            color = _c(self._pal, "TEXT_WARN")
        else:
            return f"<div>{prefix}{text}</div>"
        return f'<div>{prefix}<span style="color:{color};">{text}</span></div>'

    def _line_visible(self, severity: str) -> bool:
        """Whether a line of this severity passes the current Error/Warning
        filter. With no filter active every line shows; when a filter is active
        only lines of that severity show."""
        active = getattr(self, "_log_filter", None)
        if active is None:
            return True
        return severity == active

    def _init_log_file(self):
        """Create one on-disk log file per session (Tk status_bar parity).

        Lives at ``~/.config/AmethystModManager/logs/amethyst-<ts>.log`` and is
        appended to on every ``_append_log`` call so the file stays in sync with
        the on-screen log."""
        self._log_file = None
        try:
            from datetime import datetime
            from Utils.config_paths import get_logs_dir
            ts = datetime.now().strftime("%m-%d-%y-%H%M%S")
            self._log_file = get_logs_dir() / f"amethyst-{ts}.log"
        except Exception:
            self._log_file = None

    def _write_log_file(self, line: str, timestamp: str):
        """Append one already-stripped line to the session log file (best-effort).

        *timestamp* is the same value shown on-screen so file and panel agree."""
        log_file = getattr(self, "_log_file", None)
        if log_file is None:
            return
        try:
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{timestamp}]  {line}\n")
        except OSError:
            pass

    def _append_log(self, message: str):
        """Backend log_fn target - append a line to the log text area.

        Thread-safe: worker threads pass this as their ``log_fn`` (Nexus browser,
        collection detail/reset, installs …). Qt widgets may ONLY be touched on
        the GUI thread - touching ``_log_view`` from a worker is a data race that
        can segfault - so when called off-thread we marshal through the queued
        ``_op_log`` signal instead of writing the widget directly.

        Lines are also retained (with their severity) in ``_log_lines`` so the
        Error/Warning toggles can re-render a filtered view."""
        # Escape surrogate bytes from on-disk file names before the message
        # touches Qt / the log file - a lone surrogate can raise on encode in
        # any sink, and log calls must never be able to crash their caller.
        message = str(message)
        try:
            message.encode("utf-8")
        except UnicodeEncodeError:
            message = message.encode("utf-8", "backslashreplace").decode(
                "utf-8", "replace")
        try:
            from PySide6.QtCore import QThread
            if QThread.currentThread() is not self.thread():
                self._op_log.emit(message)
                return
        except Exception:
            pass
        line = message.rstrip("\n")
        severity = self._classify_log_line(line)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not hasattr(self, "_log_lines"):
            self._log_lines = []
        self._log_lines.append((line, severity, timestamp))
        # Persist to the session log file (before the display filter, so the file
        # captures everything - Tk status_bar parity).
        self._write_log_file(line, timestamp)
        if not self._line_visible(severity):
            return   # filtered out - retained but not shown
        html = self._log_line_html(line, severity, timestamp)
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.appendHtml(html)
            except Exception:
                pass

    def _render_log(self):
        """Re-render both log views from ``_log_lines`` honouring the current
        Error/Warning filter toggles."""
        lines = getattr(self, "_log_lines", [])
        html = "".join(
            self._log_line_html(text, sev, ts)
            for text, sev, ts in lines if self._line_visible(sev)
        )
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.clear()
                if html:
                    view.appendHtml(html)
                view.moveCursor(QTextCursor.End)
            except Exception:
                pass

    def _toggle_log_filter(self, which: str):
        """Filter the log to only *which* severity; clicking the active filter
        again clears it (shows everything). Only one filter at a time."""
        if getattr(self, "_log_filter", None) == which:
            self._log_filter = None   # clicking the active filter clears it
        else:
            self._log_filter = which
        self._refresh_log_filter_labels()
        self._render_log()

    def _refresh_log_filter_labels(self):
        """Colour the filter labels; highlight (bold) the active filter, dim the
        inactive one when a filter is engaged so it reads as an active toggle.

        Styles every Error/Warning label pair - the docked log bar's and, when
        the log is open as a tab, that tab's copies - so both read the same
        filter state."""
        active = getattr(self, "_log_filter", None)
        dim = _c(self._pal, "TEXT_DIM")
        err_c = _c(self._pal, "TEXT_ERR")
        warn_c = _c(self._pal, "TEXT_WARN")
        if active == "error":
            err_css, warn_css = f"color:{err_c}; font-weight:bold;", f"color:{dim};"
        elif active == "warning":
            err_css, warn_css = f"color:{dim};", f"color:{warn_c}; font-weight:bold;"
        else:
            err_css, warn_css = f"color:{err_c};", f"color:{warn_c};"
        pairs = [(self._errors_lbl, self._warnings_lbl)]
        tab_pair = getattr(self, "_log_tab_filter_lbls", None)
        if tab_pair is not None:
            pairs.append(tab_pair)
        for err_lbl, warn_lbl in pairs:
            try:
                err_lbl.setStyleSheet(err_css)
                warn_lbl.setStyleSheet(warn_css)
            except RuntimeError:      # label deleted with a closed tab
                pass

    def _clear_log(self):
        """Clear both the docked log view and the full-screen log tab (if open)."""
        self._log_lines = []
        for view in (self._log_view, getattr(self, "_log_tab_view", None)):
            if view is None:
                continue
            try:
                view.clear()
            except Exception:
                pass

    def _open_logs_folder(self):
        """Open the session logs directory in the system file manager (Tk parity)."""
        try:
            from Utils.config_paths import get_logs_dir
            from Utils.xdg import xdg_open
            logs_dir = get_logs_dir()
            logs_dir.mkdir(parents=True, exist_ok=True)
            xdg_open(logs_dir, log_fn=self._append_log)
        except Exception as exc:
            self._append_log(f"[log] could not open logs folder: {exc}")

    def _open_log_tab(self):
        """Open the log as a full-screen (detachable) tab. It mirrors the docked
        log view: new lines land in both, and Clear Log wipes both.

        The tab carries its own copy of the log bar's controls along its bottom
        edge (Errors/Warnings filters, Clear Log, Open Log Folder) so the log is
        fully usable without the docked bar in view."""
        if self._tabs.has_key("log"):
            self._tabs.focus_key("log")
            return
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setObjectName("LogView")
        self._log_tab_view = view
        # Seed with the existing (filtered, colour-tinted) log contents.
        for text, sev, ts in getattr(self, "_log_lines", []):
            if self._line_visible(sev):
                view.appendHtml(self._log_line_html(text, sev, ts))
        view.moveCursor(QTextCursor.End)
        col.addWidget(view, 1)

        bar = QWidget()
        bar.setObjectName("LogBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(12)
        errors_lbl = QLabel(self.tr("● Errors"))
        errors_lbl.setCursor(Qt.PointingHandCursor)
        errors_lbl.mousePressEvent = lambda e: self._toggle_log_filter("error")
        h.addWidget(errors_lbl)
        warnings_lbl = QLabel(self.tr("● Warnings"))
        warnings_lbl.setCursor(Qt.PointingHandCursor)
        warnings_lbl.mousePressEvent = lambda e: self._toggle_log_filter("warning")
        h.addWidget(warnings_lbl)
        self._log_tab_filter_lbls = (errors_lbl, warnings_lbl)
        self._refresh_log_filter_labels()
        clear_btn = self._text_button(self.tr("Clear Log"), compact=True)
        clear_btn.clicked.connect(self._clear_log)
        h.addWidget(clear_btn)
        open_btn = self._text_button(self.tr("Open Log Folder"), compact=True)
        open_btn.clicked.connect(self._open_logs_folder)
        h.addWidget(open_btn)
        h.addStretch(1)
        col.addWidget(bar)

        def _forget(*_):
            self._log_tab_view = None
            self._log_tab_filter_lbls = None
        view.destroyed.connect(_forget)
        self._tabs.open_tab(page, self.tr("Log"), key="log")

    def _find_changelog_file(self):
        """Locate the bundled Changelog.txt across the from-source and
        packaged (flatpak / AppImage) layouts.

        From source it lives at the repo root - a level above ``src/`` (which
        holds ``gui_qt``). In the flatpak/AppImage the tree is flattened, so it
        sits right beside ``gui_qt``. Check both, plus the current working dir
        (the launcher ``cd``s into the share dir before running)."""
        from pathlib import Path
        here = Path(__file__).resolve().parent  # .../gui_qt
        candidates = [
            here.parent / "Changelog.txt",         # flattened (flatpak/appimage)
            here.parent.parent / "Changelog.txt",  # from source (repo root)
            Path.cwd() / "Changelog.txt",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _open_wiki_tab(self):
        """Open the project's GitHub wiki as a detachable tab."""
        if self._tabs.has_key("wiki"):
            self._tabs.focus_key("wiki")
            return
        from gui_qt.wiki_view import WikiView
        view = WikiView()
        # Share codes printed in a wiki page are clickable - route one straight
        # into the same import flow as Profile ▸ Import code.
        view.import_code_requested.connect(self._import_profile_code)
        self._tabs.open_tab(view, self.tr("Wiki"), key="wiki")

    def _open_changelog_tab(self):
        """Open the bundled Changelog.txt as a read-only detachable tab."""
        if self._tabs.has_key("changelog"):
            self._tabs.focus_key("changelog")
            return
        path = self._find_changelog_file()
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = self.tr("Could not read the changelog:\n{0}").format(exc)
        else:
            text = self.tr("Changelog file not found.")
        view = QTextBrowser()
        view.setReadOnly(True)
        view.setObjectName("LogView")
        view.setOpenExternalLinks(True)
        # Render the changelog as Markdown (## headings, - bullets, etc.)
        # with a slightly larger base font for readability.
        font = view.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        view.setFont(font)
        view.setMarkdown(self._changelog_to_markdown(text))
        view.moveCursor(QTextCursor.Start)
        self._tabs.open_tab(view, self.tr("Changelog"), key="changelog")

    @staticmethod
    def _changelog_to_markdown(text):
        """Adapt the release-format Changelog.txt for Markdown rendering
        without editing the file itself.

        Version entries are written as ``- vX.Y.Z`` bullets (a format the
        release workflow depends on).  Turn those specific lines into level-1
        headings so they render as prominent titles, leaving every other line
        untouched.
        """
        import re
        version_re = re.compile(r"^\s*-\s*(v\d+(?:\.\d+)*)\s*$")
        out = []
        for line in text.splitlines():
            m = version_re.match(line)
            if m:
                # Blank line before the heading so Markdown doesn't fold it
                # into the preceding bullet list.
                if out and out[-1].strip():
                    out.append("")
                out.append("# {0}".format(m.group(1)))
            else:
                out.append(line)
        return "\n".join(out)

    # ------------------------------------------------------ social buttons
    def _apply_support_button_visibility(self):
        """Show/hide the Ko-Fi and Endorse buttons per the UI settings."""
        from Utils import ui_config as uc
        for attr, load_fn in (("_kofi_btn", uc.load_hide_kofi_button),
                              ("_endorse_amm_btn", uc.load_hide_endorse_button)):
            btn = getattr(self, attr, None)
            if btn is None:
                continue
            try:
                btn.setVisible(not bool(load_fn()))
            except Exception:
                btn.setVisible(True)

    def _open_github(self):
        from Utils.xdg import open_url
        open_url("https://github.com/ChrisDKN/Amethyst-Mod-Manager")

    def _open_kofi(self):
        from Utils.xdg import open_url
        open_url("https://ko-fi.com/chrisdkn")

    def _endorse_amm(self):
        """Endorse the Amethyst Mod Manager Nexus page (site mod 1714).

        If the user isn't logged in we just open the mod page. Otherwise we
        endorse on a worker thread and report the result. Nexus rejects an
        endorsement from anyone who has never downloaded the mod
        (``NOT_DOWNLOADED_MOD``) - something the Tk version didn't surface - so
        we detect that and tell them to download it from Nexus first."""
        _AMM_URL = "https://www.nexusmods.com/site/mods/1714"
        api = self._ensure_nexus_api()
        if api is None:
            from Utils.xdg import open_url
            self._notify(self.tr("Log in first (Nexus ▸ Login) - opening the "
                                 "AMM page so you can endorse it there."), "info")
            open_url(_AMM_URL)
            return

        import threading

        def _worker():
            import requests
            payload = {}
            try:
                result = api.endorse_mod("site", 1714)
            except requests.HTTPError as exc:
                try:
                    result = exc.response.json()
                except Exception:
                    payload = {"state": "error",
                               "message": self.tr("Endorse AMM failed - {0}").format(exc)}
                    self._amm_endorse_done.emit(payload)
                    return
            except Exception as exc:
                payload = {"state": "error",
                           "message": self.tr("Endorse AMM failed - {0}").format(exc)}
                self._amm_endorse_done.emit(payload)
                return

            status = (result.get("status") or "")
            message = (result.get("message") or "")
            if status == "Endorsed" or message == "IS_OWN_MOD":
                payload = {"state": "success",
                           "message": self.tr("Thank you for endorsing!")}
            elif message == "ALREADY_ENDORSED":
                payload = {"state": "info",
                           "message": self.tr("You've already endorsed - thank you!")}
            elif message == "NOT_DOWNLOADED_MOD":
                payload = {"state": "warning", "open_url": True,
                           "message": self.tr(
                               "Nexus only lets you endorse the app after you've "
                               "downloaded it at least once. Opening the AMM page "
                               "- please download it there first, then endorse.")}
            else:
                payload = {"state": "warning",
                           "message": self.tr("Endorse AMM: {0}").format(message or status)}
            self._amm_endorse_done.emit(payload)

        self._notify(self.tr("Endorsing Amethyst Mod Manager…"), "info")
        threading.Thread(target=_worker, daemon=True, name="endorse-amm").start()

    def _on_amm_endorse_done(self, payload):
        """UI thread: report the AMM endorse result. When Nexus reports the app
        was never downloaded, open the mod page so the user can grab it."""
        self._notify(payload.get("message", ""), payload.get("state", "info"))
        if payload.get("open_url"):
            from Utils.xdg import open_url
            open_url("https://www.nexusmods.com/site/mods/1714")


def _apply_app_identity(app) -> None:
    """Set the application/window icon and Wayland desktop-file association.

    Three things have to happen for the icon to show in the window title bar
    AND the taskbar across from-source, AppImage and Flatpak runs:

    1. QApplication.setWindowIcon() - the window's own icon. Works everywhere
       (including a plain from-source run that has no installed .desktop file)
       because we load the PNG straight off disk. This alone fixes the title
       bar; X11 taskbars also read it.
    2. setDesktopFileName() - on Wayland the compositor won't show a taskbar
       icon from the window itself; it looks up an INSTALLED .desktop file
       (by app_id) and uses that entry's Icon=. AppImage and Flatpak install
       such a file, so we point Qt at it. The basename differs per target.
    3. setApplicationName() - helps the X11 WM_CLASS / labels. (We avoid
       setApplicationDisplayName, which Qt would append to every window title.)
    """
    from pathlib import Path
    from PySide6.QtGui import QIcon

    app.setApplicationName("Amethyst Mod Manager")
    # NB: intentionally NOT calling setApplicationDisplayName - Qt auto-appends
    # " - {DisplayName}" to every setWindowTitle(), which duplicated the app
    # name in the title bar ("… - v2.0.0 - Amethyst Mod Manager").

    # Window icon: bundled Logo.png sits next to the other icons (src/icons/).
    # gui_qt/ is a sibling of icons/, so parent.parent/icons/Logo.png.
    logo = Path(__file__).resolve().parent.parent / "icons" / "Logo.png"
    if logo.is_file():
        ic = QIcon(str(logo))
        if not ic.isNull():
            app.setWindowIcon(ic)

    # Desktop-file name for the Wayland taskbar association. Flatpak installs
    # io.github.Amethyst.ModManager.desktop; the AppImage installs
    # mod-manager.desktop. Detect which we're in so the compositor finds the
    # matching installed entry (and its Icon=). From source there's no
    # installed .desktop, so this is a harmless no-op (the window icon above
    # still covers the title bar).
    #
    # NB: match FLATPAK_ID against OUR id, not just "is any FLATPAK_ID set" -
    # running from source inside another flatpak (e.g. a flatpak VS Code /
    # terminal) sets FLATPAK_ID to that host app, and /.flatpak-info exists
    # for any flatpak-sandboxed parent, so neither is a reliable "we are the
    # Amethyst flatpak" signal on its own.
    if os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager":
        app.setDesktopFileName("io.github.Amethyst.ModManager")
    elif os.environ.get("APPDIR") or os.environ.get("APPIMAGE"):
        app.setDesktopFileName("amethyst-mod-manager")
    else:
        app.setDesktopFileName("io.github.Amethyst.ModManager")

def run() -> int:
    import sys
    from PySide6.QtWidgets import QApplication
    from Nexus.nxm_handler import (
        NxmIPC, NxmHandler, nxm_log, nxm_url_from_argv, strip_nxm_argv)

    # The browser-spawned handoff process has no GUI, so nxm_log's file sink
    # (logs/nxm.log) is the only record of this launch - log it first thing.
    from Thunderstore.ror2mm_handler import (
        Ror2mmHandler, ror2mm_url_from_argv, strip_ror2mm_argv)

    nxm_url = nxm_url_from_argv()
    if nxm_url or "--nxm" in sys.argv:
        nxm_log(f"NXM launch: argv={sys.argv[1:]}")

    ror2mm_url = ror2mm_url_from_argv()
    if ror2mm_url or "--ror2mm" in sys.argv:
        nxm_log(f"ror2mm launch: argv={sys.argv[1:]}")

    # Single-instance: if launched with an nxm:// link and an instance is
    # already running, hand the link off over the IPC socket and exit - don't
    # build a second window. Done FIRST - before registration and the
    # QApplication - so the browser-spawned process is cheap, and so a stale
    # .desktop pointing at a different install variant can't re-assert its own
    # registration on every click while another variant is the one actually
    # running (the receiving instance re-registers itself instead, so the
    # install the user really uses ends up owning the handler).
    if nxm_url:
        if NxmIPC.send_to_running(nxm_url):
            nxm_log("NXM link handed off to running instance - exiting")
            return 0
        nxm_log("No running instance - continuing into full app launch")
    elif "--nxm" in sys.argv:
        nxm_log("--nxm flag present but no nxm:// URL in argv")

    # Same single-instance handoff for Thunderstore links - they ride the same
    # IPC socket (the payload is just a URL; the receiver routes by scheme).
    if ror2mm_url:
        if NxmIPC.send_to_running(ror2mm_url):
            nxm_log("ror2mm link handed off to running instance - exiting")
            return 0
        nxm_log("No running instance - continuing into full app launch")
    elif "--ror2mm" in sys.argv:
        nxm_log("--ror2mm flag present but no ror2mm:// URL in argv")

    # Register as the nxm:// handler on every full launch (idempotent) so
    # "Download with Manager" on Nexus routes here.
    try:
        NxmHandler.register()
    except Exception:
        import traceback
        nxm_log(f"NxmHandler.register() crashed:\n{traceback.format_exc()}")

    # Same for ror2mm:// so Thunderstore's "Install with Mod Manager" button
    # routes here. Independent try/except: a failure in one scheme's XDG
    # registration must not stop the other from being registered.
    try:
        Ror2mmHandler.register()
    except Exception:
        import traceback
        nxm_log(f"Ror2mmHandler.register() crashed:\n{traceback.format_exc()}")

    # Migrate/clean amethyst.ini BEFORE anything reads it (theme loader, GameState).
    # Wipes a pre-Qt ini (missing [meta] version=2) so everyone starts fresh.
    from Utils.ui_config import ensure_ini_version, load_language, load_ui_scale
    ensure_ini_version()

    # Apply the user's saved UI scale. Qt only reads QT_SCALE_FACTOR once, at
    # QApplication construction, so this must run before the QApplication below;
    # a scale change from Settings persists the value and self-re-execs (which
    # inherits this environment), so run() must be able to *update or clear*
    # QT_SCALE_FACTOR on every launch - not just set it once.
    #
    # We must not clobber a QT_SCALE_FACTOR the user set in their own shell, yet
    # we must fully own the one we set on a previous re-exec. A marker env var
    # (_AMM_OWNS_SCALE) distinguishes the two: if it's present, the current
    # QT_SCALE_FACTOR is ours from a prior launch and we may overwrite/clear it;
    # if QT_SCALE_FACTOR is present WITHOUT the marker, it's the user's - leave
    # it alone.
    import os as _os
    try:
        _scale = load_ui_scale()
        _ours = _os.environ.get("_AMM_OWNS_SCALE") == "1"
        _user_set = "QT_SCALE_FACTOR" in _os.environ and not _ours
        if not _user_set:
            if abs(_scale - 1.0) > 0.01:
                _os.environ["QT_SCALE_FACTOR"] = (
                    f"{_scale:.4f}".rstrip("0").rstrip("."))
                _os.environ["_AMM_OWNS_SCALE"] = "1"
            else:
                # 1.0 / auto→1.0: drop any scale we set on a previous launch so
                # Qt's native per-monitor DPI handling takes over again.
                _os.environ.pop("QT_SCALE_FACTOR", None)
                _os.environ.pop("_AMM_OWNS_SCALE", None)
    except Exception:
        pass

    # Share GL contexts so the nif viewport survives tab detach/re-pin
    # reparenting. Must be set before the QApplication exists.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    # We used to force QT_XCB_GL_INTEGRATION=xcb_egl here, to dodge a GLX/DRI3
    # swap that can hang forever in xcb_wait_for_special_event when the window
    # is resized mid-swap (dragging the NIF viewer's splitter froze the app).
    # That traded a viewer-only freeze for a startup-wide black window: under
    # Xwayland the EGL path rendered nothing at all, and since a QOpenGLWidget
    # composites the WHOLE window, the entire UI went black (GH#350 - the
    # reporter confirmed xcb_glx fixes it). Qt's default is used now; the swap
    # hang is handled where it happens instead, by holding off viewport repaints
    # mid-resize (see _Viewport.resizeEvent). Users can still opt in by exporting
    # QT_XCB_GL_INTEGRATION themselves - Qt reads it without our help.
    app = QApplication(sys.argv)
    # Before anything installs an event filter: an exception escaping a Qt
    # callback leaves CPython's error indicator set and kills the next widget
    # Qt-side construction with a bogus "returned NULL without setting an
    # exception" SystemError somewhere else entirely.
    from gui_qt.qt_callback_guard import install as _install_callback_guard
    _install_callback_guard()
    _apply_app_identity(app)
    # Install UI translators before any widget is built (Qt only translates
    # tr() calls made after the translator is installed). Language comes from
    # amethyst.ini, which ensure_ini_version() has just finished migrating.
    from gui_qt.i18n import install_translators
    install_translators(app, load_language())
    apply_theme(app)

    # Transparent, correctly-centred splash covering first load. Held open past
    # show() and dismissed by MainWindow on the first completed conflict rebuild
    # (with a watchdog fallback). Never let a splash failure block startup.
    _splash = None
    try:
        from gui_qt.splash import show_splash
        _splash = show_splash()
    except Exception:
        _splash = None

    win = MainWindow(app, splash=_splash)
    # Route stderr + uncaught tracebacks into the log panel now that the log
    # sink (set_app_log) is wired by MainWindow.__init__. Best-effort - a
    # failure here must never block startup.
    try:
        from Utils.stderr_capture import (
            install as _install_stderr_capture,
            install_faulthandler as _install_faulthandler,
        )
        _install_stderr_capture()
        # Native crashes (segfaults from Qt/C code) can't reach a Python hook -
        # faulthandler dumps a C-level traceback to a file in the logs dir so a
        # crashed user still has something to share.
        _install_faulthandler()
    except Exception:
        pass
    # Show the window immediately so its layout gets a real size (the deferred
    # singleShot(0) setup in __init__ reads live widget heights), but at zero
    # opacity so nothing is visibly rendered behind the splash while it loads.
    # _dismiss_splash restores opacity once the plugin panel - the last render
    # step - is populated, so the window appears fully drawn, not mid-render.
    # Nothing GL runs on this thread. The warmup that pre-empts the first-mesh
    # composition flash now waits on an out-of-process probe handed to a worker
    # thread, and only lands if it beats the window onto the screen; the inline
    # version of that probe aborted the process on GLX stacks that can't serve
    # the format (qFatal, uncatchable - it took the AppImage down under Xvfb).
    win._start_gl_warmup()
    if _splash is not None:
        win.setWindowOpacity(0.0)
    win.show()
    if _splash is not None:
        try:
            _splash.raise_()
        except Exception:
            pass
    # Listen for NXM links handed off by future instances (after the window is
    # up so the received-link handler has a live UI to drive).
    win._start_nxm_ipc()
    rc = app.exec()

    # A language change requests a clean self-restart so the whole UI rebuilds
    # in the new language (no partial live-retranslate). The window's closeEvent
    # has already run (IPC socket released, restore-on-close done); re-exec the
    # same interpreter + argv in place.
    if _RESTART_REQUESTED:
        # Drop one-shot Nexus and Thunderstore links from the relaunch argv so
        # neither install is reprocessed on the fresh start.
        argv = strip_ror2mm_argv(strip_nxm_argv(list(sys.argv)))
        try:
            os.execv(sys.executable, [sys.executable] + argv)
        except Exception:
            # If exec fails, fall through and just exit normally.
            pass
    return rc
