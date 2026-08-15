"""Configure-Game view - Qt port of gui/add_game_dialog.ReconfigureGamePanel.

Opens as a (detachable) tab. Reads/writes LIVE game config via the toolkit-neutral
backend setters/getters on BaseGame (set_game_path / set_prefix_path /
set_deploy_mode / set_staging_path / set_*; auto_deploy/archive_invalidation/
prefix_numbering attrs). Game-dependent options are gated by hasattr(), exactly
like the Tk panel. Auto-detection (Steam/Heroic) runs on a worker thread and is
delivered to the UI thread via Qt signals.

Handles both cases in one view: "Add" framing when the game is unconfigured,
"Reconfigure" + Remove/Clean when configured.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QRadioButton, QCheckBox, QButtonGroup,
    QComboBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.add_game_view import _game_logo
from gui_qt.safe_emit import safe_emit
from gui_qt.worker import run_in_worker, NO_EMIT
# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.deploy import LinkMode

# Left column width - the image panel and the options panel share it.
_LEFT_COL_W = 240
_LOGO_SQ = 200


def _heroic_app_names(game) -> list[str]:
    names = list(getattr(game, "heroic_app_names", []) or [])
    if not names and getattr(game, "name", None):
        try:
            import json
            from Utils.config_paths import get_game_config_path
            p = get_game_config_path(game.name)
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                saved = (data.get("heroic_app_name", "") or "").strip()
                if saved:
                    names = [saved]
        except Exception:
            pass
    return names


def _lutris_available(game) -> bool:
    """True when a Lutris install exists and the game has an exe to match
    against it - Lutris detection is keyed off the exe name, so any such game
    may have a Lutris prefix."""
    if not getattr(game, "exe_name", None):
        return False
    try:
        from Utils.lutris_finder import find_lutris_roots
        return bool(find_lutris_roots())
    except Exception:
        return False


def _faugus_available(game) -> bool:
    """True when a Faugus install exists and the game has an exe to match
    against it (same exe-name-keyed detection as Lutris)."""
    if not getattr(game, "exe_name", None):
        return False
    try:
        from Utils.faugus_finder import find_faugus_roots
        return bool(find_faugus_roots())
    except Exception:
        return False


class _ScanSignals(QObject):
    # Scan results carry everything the worker discovered so the worker thread
    # never writes view attributes directly (the slots run on the GUI thread).
    game_found = Signal(object, bool)
    # ^ (candidates: list of {source, path, prefix, id} dicts - one per
    #    launcher the game was detected on, in detection-priority order -
    #    and auto_apply: False for the candidates-only rescan that fills the
    #    launcher dropdown of an already-configured game)
    drive_scan_found = Signal(object)       # (path|None) - full-drive Scan button
    prefix_found = Signal(object, object, object)
    # ^ (path|None, lutris_slug|None, faugus_gameid|None)
    # Browse (portal) picks - fired from the portal WORKER thread, so they must
    # be marshalled to the GUI thread via a Signal before touching any widget.
    game_picked = Signal(object)            # (path|None)
    prefix_picked = Signal(object)          # (path|None)
    staging_picked = Signal(object)         # (path|None)
    saves_picked = Signal(object)           # (path|None)
    # Remove-instance / clean-game-folder workers → GUI thread. Both do heavy
    # disk work (restore + rmtree / full game-dir scan) that used to freeze
    # the UI when run in the click handler.
    remove_done = Signal()
    clean_done = Signal(object, object)     # (removed count|None, error|None)
    # Staging migration (old root scan + move) → GUI thread.
    staging_scanned = Signal(object, object, object, object)  # (old, new, files, size)
    staging_progress = Signal(int, int, str)                  # (done, total, message)
    staging_move_done = Signal(int, int, int)                 # (moved, skipped, failed)


class ConfigureGameView(QWidget):
    """*on_done(saved: bool, removed: bool)* is called after Save/Remove so the
    window can refresh the game list and close the tab."""

    def __init__(self, game, on_done, parent=None):
        super().__init__(parent)
        self._game = game
        self._on_done = on_done or (lambda saved, removed: None)
        self._p = active_palette()

        self._found_path: Path | None = None
        self._found_prefix: Path | None = None
        self._found_lutris_slug: str | None = None
        self._found_heroic_app: str | None = None
        self._found_faugus_gameid: str | None = None
        # Dropdown choices when the game is installed via more than one
        # launcher: {source, path, prefix, id} dicts, aligned with the combo.
        self._install_choices: list[dict] = []
        self._custom_staging: Path | None = None
        self._custom_saves: Path | None = None

        # Closing the tab deleteLater()'s the view while scan workers may still
        # be running; the guards drop late slot runs so they never touch
        # destroyed widgets (mirrors wizards_qt._view_base).
        self._closing = False
        self.destroyed.connect(lambda *_: setattr(self, "_closing", True))

        self._sig = _ScanSignals()
        g = self._guard
        self._sig.game_found.connect(g(self._on_game_found))
        self._sig.drive_scan_found.connect(g(self._on_drive_scan_found))
        self._sig.prefix_found.connect(g(self._on_prefix_found))
        self._sig.game_picked.connect(g(self._on_game_picked))
        self._sig.prefix_picked.connect(g(self._on_prefix_picked))
        self._sig.staging_picked.connect(g(self._on_staging_picked))
        self._sig.saves_picked.connect(g(self._on_saves_picked))
        self._sig.remove_done.connect(g(self._on_remove_finished))
        self._sig.clean_done.connect(g(self._on_clean_finished))
        self._sig.staging_scanned.connect(g(self._on_staging_scanned))
        self._sig.staging_progress.connect(g(self._on_staging_progress))
        self._sig.staging_move_done.connect(g(self._on_staging_move_done))
        self._staging_popup = None
        self._destructive_busy = False
        self.staging_migrated = False

        self._build()
        self._prepopulate()

    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    # ---- styling helpers --------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size:14px; font-weight:600; color:{self._c('TEXT_SEP')};")
        return lbl

    def _panel(self, title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
        """A bordered card panel. Returns (frame, inner vbox). If *title* is given
        a section header is added as the first row."""
        frame = QFrame()
        frame.setObjectName("ConfigPanel")
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)
        if title:
            v.addWidget(self._section_header(title))
        return frame, v

    def _status(self, text: str, tone: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{self._c(tone)};")
        return lbl

    def _path_edit(self) -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("PathEdit")
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); e.setFont(f)
        return e

    def _small_btn(self, text, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("FormButton")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # ---- build ------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        configured = self._game.is_configured()

        # Title bar.
        header = QWidget(); header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header); hb.setContentsMargins(12, 8, 12, 8)
        verb = "Reconfigure" if configured else "Add"
        self._title_lbl = QLabel(
            self.tr("{0} Game - {1}").format(verb, self._game.name))
        self._title_lbl.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(self._title_lbl)
        hb.addStretch(1)
        self._scope_lbl = QLabel()
        self._scope_lbl.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        hb.addWidget(self._scope_lbl)
        self._unpin_btn = self._small_btn(
            self.tr("Use Shared Settings"), self._on_clear_overrides)
        self._unpin_btn.setToolTip(self.tr(
            "This profile has its own saved paths/options. Remove them so "
            "it follows the shared (default profile) settings again."))
        hb.addWidget(self._unpin_btn)
        self._refresh_scope_header()
        outer.addWidget(header)

        # Body - four distinct panels in a 2×2 grid: (top-left) image,
        # (bottom-left) options, (right, spanning both rows) path entries.
        body = QWidget(); body.setObjectName("FormBody")
        grid = QGridLayout(body)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        outer.addWidget(body, 1)

        image_panel = self._build_image_panel()
        options_panel = self._build_options_panel()
        paths_panel = self._build_paths_panel()

        # Left column (image + options) is a fixed narrow width; the paths panel
        # takes all remaining width. The options panel gets the taller share so
        # its scroll area has room.
        grid.addWidget(image_panel, 0, 0)
        grid.addWidget(options_panel, 1, 0)
        grid.addWidget(paths_panel, 0, 1, 2, 1)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 1)

        # --- Button bar ---
        bar = QWidget(); bar.setObjectName("BottomBar")
        bb = QHBoxLayout(bar); bb.setContentsMargins(12, 8, 12, 8)
        if configured:
            self._remove_btn = QPushButton(self.tr("Remove Instance"))
            self._remove_btn.setObjectName("DangerButton")
            self._remove_btn.setCursor(Qt.PointingHandCursor)
            self._remove_btn.clicked.connect(self._on_remove)
            bb.addWidget(self._remove_btn)
            self._clean_btn = QPushButton(self.tr("Clean Game Folder"))
            self._clean_btn.setObjectName("DangerButton")
            self._clean_btn.setCursor(Qt.PointingHandCursor)
            self._clean_btn.clicked.connect(self._on_clean)
            bb.addWidget(self._clean_btn)
        bb.addStretch(1)
        if configured:
            reset = self._small_btn(self.tr("Reset Locations"), self._reset_locations)
            bb.addWidget(reset)
        self._save_btn = QPushButton(self.tr("Save"))
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        bb.addWidget(self._save_btn)
        cancel = self._small_btn(self.tr("Cancel"), lambda: self._on_done(False, False))
        bb.addWidget(cancel)
        outer.addWidget(bar)

    def _divider(self) -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.HLine)
        f.setStyleSheet(f"color:{self._c('BORDER')};")
        return f

    # ---- panel builders ---------------------------------------------------
    def _build_image_panel(self) -> QFrame:
        """Top-left panel - the game's square logo (same source as Add-Game)."""
        frame, v = self._panel()
        frame.setFixedWidth(_LEFT_COL_W)
        v.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(_LOGO_SQ, _LOGO_SQ)
        game_id = (getattr(self._game, "game_id", None)
                   or self._game.name.lower().replace(" ", "_"))
        pm = _game_logo(game_id, _LOGO_SQ)
        if pm is not None:
            logo.setPixmap(pm)
        else:
            logo.setText("?")
            logo.setStyleSheet(
                f"color:{self._c('TEXT_DIM')}; font-size:48px; font-weight:bold;")
        v.addWidget(logo, 0, Qt.AlignHCenter)

        name = QLabel(self._game.name)
        name.setAlignment(Qt.AlignCenter)
        name.setWordWrap(True)
        name.setStyleSheet("font-size:14px; font-weight:600;")
        v.addWidget(name)
        return frame

    def _build_paths_panel(self) -> QFrame:
        """Right panel - the three location entries (install / prefix / staging)."""
        frame, v = self._panel()
        g = self._game

        # --- Game install folder ---
        v.addWidget(self._section_header(self.tr("Game Installation Folder")))
        self._game_status = self._status(self.tr("Scanning Steam libraries…"), "TEXT_WARN")
        v.addWidget(self._game_status)
        # Launcher picker - hidden unless the scan detects the game in more
        # than one place (e.g. both a Heroic and a Lutris install).
        self._install_row = QWidget()
        pick = QHBoxLayout(self._install_row)
        pick.setContentsMargins(0, 0, 0, 0)
        pick.setSpacing(6)
        pick.addWidget(QLabel(self.tr("Detected installs:")))
        self._install_combo = QComboBox()
        self._install_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._install_combo.setMinimumContentsLength(24)
        self._install_combo.activated.connect(self._guard(self._on_install_combo))
        pick.addWidget(self._install_combo, 1)
        self._install_row.hide()
        v.addWidget(self._install_row)
        self._game_edit = self._path_edit()
        self._game_edit.editingFinished.connect(self._on_game_typed)
        v.addWidget(self._game_edit)
        row = QHBoxLayout()
        row.addWidget(self._small_btn(self.tr("Browse manually…"), self._browse_game))
        self._game_open = self._small_btn(self.tr("Open"), lambda: self._open_path(self._found_path))
        row.addWidget(self._game_open)
        self._scan_btn = self._small_btn(self.tr("Scan"), self._start_drive_scan)
        row.addWidget(self._scan_btn)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self._divider())

        # --- Proton prefix ---
        v.addWidget(self._section_header(self.tr("Proton Prefix (compatdata/pfx)")))
        has_prefix_src = bool(getattr(g, "steam_id", None)
                              or _heroic_app_names(g)
                              or _lutris_available(g)
                              or _faugus_available(g))
        self._prefix_status = self._status(
            self.tr("Scanning for prefix…") if has_prefix_src
            else self.tr("No launcher ID - prefix not applicable."),
            "TEXT_WARN" if has_prefix_src else "TEXT_DIM")
        v.addWidget(self._prefix_status)
        self._prefix_edit = self._path_edit()
        self._prefix_edit.setEnabled(has_prefix_src)
        self._prefix_edit.editingFinished.connect(self._on_prefix_typed)
        v.addWidget(self._prefix_edit)
        row = QHBoxLayout()
        self._prefix_browse = self._small_btn(self.tr("Browse manually…"), self._browse_prefix)
        self._prefix_browse.setEnabled(has_prefix_src)
        row.addWidget(self._prefix_browse)
        self._prefix_open = self._small_btn(self.tr("Open"), lambda: self._open_path(self._found_prefix))
        row.addWidget(self._prefix_open)
        row.addStretch(1)
        v.addLayout(row)
        self._has_prefix_src = has_prefix_src
        v.addWidget(self._divider())

        # --- Mod staging folder ---
        v.addWidget(self._section_header(self.tr("Mod Staging Folder")))
        self._staging_status = self._status(self.tr("Default location will be used."), "TEXT_DIM")
        v.addWidget(self._staging_status)
        self._staging_edit = self._path_edit()
        self._staging_edit.editingFinished.connect(self._on_staging_typed)
        v.addWidget(self._staging_edit)
        row = QHBoxLayout()
        row.addWidget(self._small_btn(self.tr("Browse manually…"), self._browse_staging))
        row.addWidget(self._small_btn(self.tr("Open"), lambda: self._open_path(
            Path(self._staging_edit.text()) if self._staging_edit.text() else None)))
        row.addWidget(self._small_btn(self.tr("Reset to default"), self._reset_staging))
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self._divider())

        # --- Saves folder override -------------------------------------------
        # The Saves tab normally locates saves from the Ludusavi manifest. Games
        # it does not cover, or covers with a Windows-only path while running a
        # native Linux build, need to be told where to look.
        v.addWidget(self._section_header(self.tr("Saves Folder (optional)")))
        self._saves_status = self._status(
            self.tr("Detected automatically."), "TEXT_DIM")
        v.addWidget(self._saves_status)
        self._saves_edit = self._path_edit()
        self._saves_edit.editingFinished.connect(self._on_saves_typed)
        v.addWidget(self._saves_edit)
        row = QHBoxLayout()
        row.addWidget(self._small_btn(self.tr("Browse manually…"), self._browse_saves))
        row.addWidget(self._small_btn(self.tr("Open"), lambda: self._open_path(
            Path(self._saves_edit.text()) if self._saves_edit.text() else None)))
        row.addWidget(self._small_btn(self.tr("Clear"), self._clear_saves))
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return frame

    def _steam_manifest_path(self) -> Path | None:
        """This game's Steam appmanifest, or None when it isn't a Steam install."""
        try:
            from Utils.steam_finder import game_steam_id, steam_appmanifest_path
            sid = game_steam_id(self._game)
            if not sid:
                return None
            gp = self._found_path
            if gp is None and self._game.is_configured():
                gp = self._game.get_game_path()
            return steam_appmanifest_path(sid, gp)
        except Exception:
            return None

    def _build_options_panel(self) -> QFrame:
        """Bottom-left panel - deploy method + game-dependent options, in an
        independently-scrolling list so many options never blow out the frame."""
        frame, v = self._panel(self.tr("Options"))
        frame.setFixedWidth(_LEFT_COL_W)
        v.setContentsMargins(14, 12, 8, 12)   # tighter right for the scrollbar

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(scroll, 1)
        inner = QWidget(); inner.setObjectName("OptionsList")
        ov = QVBoxLayout(inner)
        ov.setContentsMargins(0, 0, 6, 0)
        ov.setSpacing(6)
        scroll.setWidget(inner)

        # --- Deploy method ---
        ov.addWidget(self._section_header(self.tr("Deploy Method")))
        rec = getattr(self._game, "default_deploy_mode", "symlink")
        self._deploy_group = QButtonGroup(self)
        self._rb_symlink = QRadioButton(
            self.tr("Symlink (Recommended)") if rec == "symlink" else self.tr("Symlink"))
        self._rb_hardlink = QRadioButton(
            self.tr("Hardlink (Recommended)") if rec == "hardlink" else self.tr("Hardlink"))
        self._deploy_group.addButton(self._rb_symlink)
        self._deploy_group.addButton(self._rb_hardlink)
        ov.addWidget(self._rb_symlink)
        ov.addWidget(self._rb_hardlink)
        ov.addWidget(self._divider())

        # hasattr-gated option checkboxes (mirrors the Tk panel).
        self._opt_checks: dict[str, QCheckBox] = {}

        def add_check(key: str, text: str, gate: bool, tip: str = ""):
            if not gate:
                return
            # Pair a bare checkbox indicator with a wrapping label so long option
            # text reflows inside the narrow column (a plain QCheckBox can't wrap).
            roww = QWidget()
            rl = QHBoxLayout(roww)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(8)
            cb = QCheckBox()
            cb.setFixedWidth(18)
            rl.addWidget(cb, 0, Qt.AlignTop)
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setCursor(Qt.PointingHandCursor)
            # Click the label to toggle the box.
            lbl.mousePressEvent = lambda _e, box=cb: box.toggle()
            rl.addWidget(lbl, 1)
            if tip:
                cb.setToolTip(tip)
                lbl.setToolTip(tip)
            ov.addWidget(roww)
            self._opt_checks[key] = cb

        add_check("script_extender_swap",
                  self.tr("Swap launcher with script extender on deploy"),
                  hasattr(self._game, "script_extender_swap"))
        add_check("auto_4gb_patch",
                  self.tr("Apply the 4GB patch automatically (deploy patches "
                          "the exe, restore reverts it)"),
                  hasattr(self._game, "set_auto_4gb_patch"))
        add_check("auto_deploy",
                  self.tr("Auto deploy (deploy automatically on enable/disable/reorder)"),
                  True)
        add_check("archive_invalidation",
                  self.tr("Automatic archive invalidation (prefer loose files over BSAs)"),
                  hasattr(self._game, "archive_invalidation_enabled"))
        add_check("case_alias_links",
                  self.tr("Create case-alias symlinks on deploy (Faster load times)"),
                  bool(getattr(self._game, "case_alias_dirs", None)))
        add_check("profile_ini_files",
                  self.tr("Use profile-specific INI files"),
                  hasattr(self._game, "profile_ini_files"))
        add_check("profile_saves",
                  self.tr("Use profile-specific saves"),
                  hasattr(self._game, "profile_saves")
                  and getattr(self._game, "supports_profile_saves", True))
        add_check("prefix_numbering",
                  self.tr("Prepend load-order numbers to mod folders"),
                  hasattr(self._game, "prefix_numbering"))
        add_check("manage_load_order_in_dfu",
                  self.tr("Manage load order in DFU"),
                  hasattr(self._game, "set_manage_load_order_in_dfu"))
        # Steam-only: gated on an appmanifest actually existing for this game,
        # so it stays hidden for Heroic/GOG/Lutris installs that have none.
        add_check("disable_steam_updates",
                  self.tr("Disable Steam updates"),
                  self._steam_manifest_path() is not None,
                  tip=self.tr("Marks Steam's appmanifest file read-only so an "
                              "update cannot be applied, keeping mods working "
                              "against the current build.\n\n"
                              "Steam may still download the update and show a "
                              "disk write error. Verifying the game's files in "
                              "Steam clears this, re-enabling updates."))

        # BG3 patch-version radios.
        self._patch_group = None
        if hasattr(self._game, "get_patch_version"):
            ov.addWidget(self._divider())
            ov.addWidget(self._section_header(self.tr("Game Patch Version")))
            self._patch_group = QButtonGroup(self)
            self._patch_buttons = {}
            for val in (8, 7, 6):
                rb = QRadioButton(self.tr("Patch {0}").format(val))
                self._patch_group.addButton(rb)
                self._patch_buttons[val] = rb
                ov.addWidget(rb)

        # plugins.txt filename casing - only for games that read a plugins.txt.
        self._plugins_txt_group = None
        if (getattr(self._game, "uses_plugins_txt", False)
                and hasattr(self._game, "set_plugins_txt_filename")):
            ov.addWidget(self._divider())
            ov.addWidget(self._section_header(self.tr("Plugins file name")))
            self._plugins_txt_group = QButtonGroup(self)
            self._plugins_txt_buttons = {}
            for fname in ("plugins.txt", "Plugins.txt"):
                rb = QRadioButton(fname)
                self._plugins_txt_group.addButton(rb)
                self._plugins_txt_buttons[fname] = rb
                ov.addWidget(rb)

        ov.addStretch(1)
        return frame

    # ---- prepopulate ------------------------------------------------------
    def _prepopulate(self):
        g = self._game
        if g.is_configured():
            gp = g.get_game_path()
            if gp:
                self._set_game(Path(gp), configured=True)
            pfx = g.get_prefix_path() if hasattr(g, "get_prefix_path") else None
            if pfx and Path(pfx).is_dir():
                self._set_prefix(Path(pfx), configured=True)
            elif self._has_prefix_src:
                self._start_prefix_scan()
            # deploy mode
            if hasattr(g, "get_deploy_mode"):
                mode = g.get_deploy_mode()
                if mode == LinkMode.HARDLINK:
                    self._rb_hardlink.setChecked(True)
                else:
                    self._rb_symlink.setChecked(True)
            else:
                self._rb_symlink.setChecked(True)
            # staging
            if getattr(g, "_staging_path", None) is not None:
                self._custom_staging = g._staging_path
                self._staging_edit.setText(str(g._staging_path))
                self._staging_status.setText(self.tr("Custom staging folder configured."))
            else:
                self._staging_edit.setText(str(g.get_mod_staging_path()))
            saves_override = (g.get_save_path_override()
                              if hasattr(g, "get_save_path_override") else None)
            if saves_override:
                self._custom_saves = Path(saves_override)
                self._saves_edit.setText(str(saves_override))
                self._saves_status.setText(self.tr("Custom saves folder configured."))
                self._saves_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")
            # option values
            self._set_check("script_extender_swap",
                            getattr(g, "script_extender_swap", True))
            self._set_check("auto_4gb_patch", getattr(g, "auto_4gb_patch", True))
            self._set_check("auto_deploy", getattr(g, "auto_deploy", False))
            self._set_check("archive_invalidation",
                            getattr(g, "archive_invalidation", True))
            self._set_check("case_alias_links", getattr(g, "case_alias_links", True))
            self._set_check("profile_ini_files", getattr(g, "profile_ini_files", False))
            self._set_check("profile_saves", getattr(g, "profile_saves", False))
            self._set_check("prefix_numbering", getattr(g, "prefix_numbering", True))
            self._set_check("manage_load_order_in_dfu",
                            getattr(g, "manage_load_order_in_dfu", False))
            self._refresh_steam_updates_check()
            if self._patch_group is not None and hasattr(g, "get_patch_version"):
                rb = self._patch_buttons.get(int(g.get_patch_version()))
                if rb:
                    rb.setChecked(True)
            self._select_plugins_txt_default()
            self._save_btn.setEnabled(True)
            # Candidates-only rescan: fills the launcher dropdown so the user
            # can switch to another detected install without touching the
            # configured path until they pick one.
            self._start_game_scan(auto_apply=False)
        else:
            self._rb_symlink.setChecked(rec_is_symlink := (
                getattr(g, "default_deploy_mode", "symlink") == "symlink"))
            if not rec_is_symlink:
                self._rb_hardlink.setChecked(True)
            # Fresh-game option defaults (mirror the Tk BooleanVar initials in
            # add_game_dialog: script_extender_swap/archive_invalidation start ON,
            # the rest OFF except prefix_numbering).
            self._set_check("script_extender_swap", True)
            self._set_check("auto_4gb_patch", True)
            self._set_check("auto_deploy", False)
            self._set_check("archive_invalidation", True)
            self._set_check("case_alias_links", True)
            self._set_check("profile_ini_files", False)
            self._set_check("profile_saves", False)
            self._set_check("prefix_numbering", True)
            self._set_check("manage_load_order_in_dfu", False)
            self._refresh_steam_updates_check()
            if self._patch_group is not None and hasattr(g, "get_patch_version"):
                rb = self._patch_buttons.get(int(g.get_patch_version()))
                if rb:
                    rb.setChecked(True)
            self._select_plugins_txt_default()
            self._seed_default_staging(g)
            self._start_game_scan()

    def _set_check(self, key, value):
        cb = self._opt_checks.get(key)
        if cb is not None:
            cb.setChecked(bool(value))

    def _apply_steam_updates_setting(self):
        """Write the Steam-updates choice through to the appmanifest's mode.

        A chmod failure never blocks the save - the rest of the settings are
        still valid - so it is logged and the box re-synced to what's real.
        """
        from Utils.app_log import app_log
        from Utils.steam_finder import game_steam_id, set_steam_updates_disabled
        want = self._opt_checks["disable_steam_updates"].isChecked()
        try:
            sid = game_steam_id(self._game)
            gp = self._found_path or (self._game.get_game_path()
                                      if self._game.is_configured() else None)
            ok = set_steam_updates_disabled(sid, want, gp)
        except Exception as e:
            app_log(f"[Configure Game] Steam update lock failed: {e}")
            ok = False
        if not ok:
            app_log("[Configure Game] Could not change the Steam update lock "
                    f"for app {game_steam_id(self._game)!r}.")
        self._refresh_steam_updates_check()

    def _refresh_steam_updates_check(self):
        """Seed the Steam-updates box from the manifest's actual permissions.

        This option is filesystem state, not saved config - the user may have
        set it by hand, or Steam's "verify files" may have cleared it since we
        last looked, so the disk is the only source of truth.
        """
        if "disable_steam_updates" not in self._opt_checks:
            return
        try:
            from Utils.steam_finder import game_steam_id, steam_updates_disabled
            sid = game_steam_id(self._game)
            gp = self._found_path
            if gp is None and self._game.is_configured():
                gp = self._game.get_game_path()
            self._set_check("disable_steam_updates",
                            steam_updates_disabled(sid, gp))
        except Exception:
            self._set_check("disable_steam_updates", False)

    # ---- per-profile overrides ---------------------------------------------
    def _overridable_keys(self) -> list[str]:
        """Every profile_settings key this game may pin as a per-profile
        override (paths + deploy mode + overridable options)."""
        g = self._game
        keys = ["game_path", "prefix_path", "deploy_mode"]
        keys += list(getattr(g, "profile_overridable_paths_extras", ()) or ())
        keys += list(getattr(g, "profile_overridable_settings", ()) or ())
        return keys

    def _profile_has_overrides(self) -> bool:
        try:
            from Utils.profile_state import read_profile_settings
            pset = read_profile_settings(self._profile_dir)
        except Exception:
            return False
        return any(k in pset for k in self._overridable_keys())

    def _on_clear_overrides(self):
        """Un-pin this profile's saved overrides so it follows the default
        profile's settings again (an improvement over Tk, where a pinned
        override could never be released)."""
        g = self._game
        # Clearing a game/prefix override changes the effective paths, which
        # would strand deployed files - same guard as saving a path change.
        if g.is_configured() and g.get_deploy_active():
            self._game_status.setText(
                self.tr("Cannot reset to shared settings while mods are deployed. "
                        "Restore the game first."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
            return
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Use Shared Settings?"),
            self.tr("Remove this profile's own paths and options so it follows "
                    "the shared (default profile) settings again?\n\n"
                    "The default profile's settings are not affected."),
            lambda ok: self._clear_overrides() if ok else None,
            confirm_label=self.tr("Reset"), cancel_label=self.tr("Cancel"),
            danger=True)

    def _clear_overrides(self):
        from Utils.profile_state import merge_profile_settings
        try:
            merge_profile_settings(
                self._profile_dir, {k: None for k in self._overridable_keys()})
        except Exception as exc:
            print(f"[gui_qt] clear profile overrides failed: {exc}", flush=True)
            return
        try:
            self._game.load_paths()
        except Exception:
            pass
        # Re-fill the form from the now-shared values.
        self._found_path = None
        self._found_prefix = None
        self._prepopulate()
        self._refresh_scope_header()
        self._game_status.setText(
            self.tr("Profile now follows the shared (default profile) settings."))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    def _refresh_scope_header(self):
        """Sync the scope label + un-pin button to the game's active profile.
        Records ``self._profile_dir`` (None for the default profile)."""
        active = getattr(self._game, "_active_profile_dir", None)
        self._profile_dir = (active if active is not None
                             and active.name != "default" else None)
        if self._profile_dir is not None:
            self._scope_lbl.setText(self.tr(
                "Settings saved to profile: {0} (this profile only)"
            ).format(active.name))
        else:
            self._scope_lbl.setText(self.tr("Editing shared settings (default profile)"))
        self._unpin_btn.setVisible(
            self._profile_dir is not None and self._profile_has_overrides())

    def refresh_for_profile(self):
        """Re-read the game's paths/options for the now-active profile and
        re-fill the form, without stealing focus. Called by the app when the
        user switches profiles while this tab is open, so it always reflects
        the profile being configured. Assumes GameState has already switched
        the game's active profile dir (and run load_paths)."""
        self._found_path = None
        self._found_prefix = None
        self._prepopulate()
        self._refresh_scope_header()

    def notify_saved(self):
        """Called by the app after a Save that keeps the tab open (Reconfigure).
        Re-sync the header (an override may have just been pinned/cleared) and
        confirm the save inline, since the tab no longer closes to signal it."""
        self._refresh_scope_header()
        self._game_status.setText(self.tr("Settings saved."))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    def _select_plugins_txt_default(self):
        """Tick the radio matching the game's current plugins.txt filename."""
        if self._plugins_txt_group is None:
            return
        current = getattr(self._game, "plugins_txt_filename", "plugins.txt")
        rb = self._plugins_txt_buttons.get(current)
        if rb is None:
            # Fall back to a case-insensitive match, then to lowercase default.
            for fname, button in self._plugins_txt_buttons.items():
                if fname.lower() == str(current).lower():
                    rb = button
                    break
            else:
                rb = self._plugins_txt_buttons.get("plugins.txt")
        if rb is not None:
            rb.setChecked(True)

    # ---- setters / status -------------------------------------------------
    def _exe_names(self):
        """The game's main exe plus any configured alternatives (non-empty)."""
        g = self._game
        names = [getattr(g, "exe_name", None)] + list(
            getattr(g, "exe_name_alts", []) or [])
        return [e for e in names if e]

    def _exe_present_in(self, folder: Path) -> bool | None:
        """Is any of the game's exes locatable under *folder*?

        exe_name may be a bare filename (directly inside the game root) or a
        relative subpath (e.g. "bin/bg3.exe"); both are matched
        case-insensitively to handle Proton/Linux casing. Returns None when the
        game has no exe name configured (nothing to validate against).
        """
        exe_names = self._exe_names()
        if not exe_names:
            return None
        for exe in exe_names:
            parts = exe.lower().replace("\\", "/").split("/")
            cur = folder
            ok = True
            for part in parts:
                match = None
                try:
                    for entry in cur.iterdir():
                        if entry.name.lower() == part:
                            match = entry
                            break
                except (PermissionError, FileNotFoundError, NotADirectoryError):
                    ok = False
                    break
                if match is None:
                    ok = False
                    break
                cur = match
            if ok and cur.is_file():
                return True
        return False

    def _set_game(self, path: Path, configured=False, source="steam"):
        self._found_path = path
        self._game_edit.setText(str(path))
        # Steam/Heroic/Lutris/Faugus library detection already verified the exe
        # lives here, so trust those sources. For manual browse / drive-scan / typed
        # paths the folder is whatever the user picked - verify the exe is
        # actually inside and warn (rather than silently claiming "Found") if
        # it isn't.
        if source in ("steam", "heroic", "lutris", "faugus") or configured:
            if configured:
                msg, tone = "Game already configured. You can update the path below.", "TEXT_OK"
            elif source == "heroic":
                msg, tone = "Found via Heroic Games Launcher.", "TEXT_OK"
            elif source == "lutris":
                msg, tone = self.tr("Found via Lutris."), "TEXT_OK"
            elif source == "faugus":
                msg, tone = self.tr("Found via Faugus Launcher."), "TEXT_OK"
            else:
                msg, tone = "Found via Steam libraries.", "TEXT_OK"
        else:
            present = self._exe_present_in(path)
            if present is False:
                names = ", ".join(self._exe_names())
                msg = self.tr("Executable ({0}) not found in this folder - "
                              "double-check the path.").format(names)
                tone = "TEXT_ERR"
            elif present is None:
                msg, tone = "Folder selected.", "TEXT_OK"
            else:
                msg, tone = "Executable found.", "TEXT_OK"
        self._game_status.setText(msg)
        self._game_status.setStyleSheet(f"color:{self._c(tone)};")
        self._save_btn.setEnabled(True)
        self._sync_install_combo(path)

    def _set_prefix(self, path: Path, configured=False):
        self._found_prefix = path
        self._prefix_edit.setText(str(path))
        msg = ("Prefix already configured. You can update the path below."
               if configured else "Found via Steam compatdata.")
        self._prefix_status.setText(msg)
        self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    # ---- typed-path handlers ----------------------------------------------
    def _on_game_typed(self):
        text = self._game_edit.text().strip()
        if text:
            path = Path(text)
            self._found_path = path
            present = self._exe_present_in(path)
            if present is False:
                names = ", ".join(self._exe_names())
                self._game_status.setText(
                    self.tr("Executable ({0}) not found in this folder - "
                            "double-check the path.").format(names))
                self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
            elif present is True:
                self._game_status.setText(self.tr("Executable found."))
                self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")
            self._save_btn.setEnabled(True)
            self._sync_install_combo(path)

    def _on_prefix_typed(self):
        text = self._prefix_edit.text().strip()
        self._found_prefix = Path(text) if text else None

    def _on_staging_typed(self):
        text = self._staging_edit.text().strip()
        self._custom_staging = Path(text) if text else None

    def _on_saves_typed(self):
        text = self._saves_edit.text().strip()
        self._custom_saves = Path(text) if text else None

    # ---- browse / open ----------------------------------------------------
    def _browse_game(self):
        # pick_folder's callback fires on the portal WORKER thread - marshal to
        # the GUI thread via a Signal before touching any widget (see the note
        # on _ScanSignals). Calling _set_game here directly would segfault Qt.
        from Utils.portal_filechooser import pick_folder
        pick_folder("Select game install folder",
                    lambda path: self._sig.game_picked.emit(path))

    def _on_game_picked(self, path):
        if path:
            self._set_game(Path(path), source="manual")

    def _browse_prefix(self):
        from Utils.portal_filechooser import pick_folder
        pick_folder("Select Proton/Wine prefix (pfx)",
                    lambda path: self._sig.prefix_picked.emit(path))

    def _on_prefix_picked(self, path):
        if path:
            self._set_prefix(Path(path))

    def _browse_staging(self):
        from Utils.portal_filechooser import pick_folder
        pick_folder("Select mod staging folder",
                    lambda path: self._sig.staging_picked.emit(path))

    def _browse_saves(self):
        # Same worker-thread caveat as _browse_game - marshal via the signal.
        from Utils.portal_filechooser import pick_folder
        pick_folder("Select saves folder",
                    lambda path: self._sig.saves_picked.emit(path))

    def _on_saves_picked(self, path):
        if not path:
            return
        self._custom_saves = Path(path)
        self._saves_edit.setText(str(path))
        self._saves_status.setText(self.tr("Custom saves folder selected."))
        self._saves_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    def _clear_saves(self):
        self._custom_saves = None
        self._saves_edit.clear()
        self._saves_status.setText(self.tr("Detected automatically."))
        self._saves_status.setStyleSheet(f"color:{self._c('TEXT_DIM')};")

    def _on_staging_picked(self, path):
        if not path:
            return
        self._custom_staging = Path(path)
        self._staging_edit.setText(str(path))
        self._staging_status.setText(self.tr("Custom staging folder selected."))
        self._staging_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")

    def _open_path(self, path):
        if path and Path(path).exists():
            import subprocess
            try:
                subprocess.Popen(["xdg-open", str(path)])
            except Exception:
                pass

    def _seed_default_staging(self, g):
        """Seed the staging field + _custom_staging with the preferred default.

        Priority: the user's Settings default_staging_path (if set), else the
        built-in ~/Games/Amethyst/<game> - a per-game *root* that keeps mods
        beside the game install on the same filesystem (hardlink-friendly),
        avoiding the Flatpak ~/.var/app cross-mount that forced symlink deploys.
        We set _custom_staging (the value save persists) - not just the display
        text - so the seeded default actually takes effect on save.
        """
        try:
            from Utils.ui_config import load_default_staging_path
            from Utils.config_paths import get_default_game_staging_root
            user_root = (load_default_staging_path() or "").strip()
            root = Path(user_root) / g.name if user_root \
                else get_default_game_staging_root(g.name)
        except Exception:
            self._custom_staging = None
            self._staging_edit.setText(str(g.get_mod_staging_path()))
            return
        # _staging_path is the game-root; the backend appends "mods" for the
        # staging path and puts profiles/overwrite/filemap alongside it.  The
        # field shows the *root* to match the browse/pick/typed convention
        # (_on_staging_picked / _on_staging_typed store the field text verbatim
        # as the root), so the seeded value round-trips through a manual edit.
        self._custom_staging = root
        self._staging_edit.setText(str(root))

    def _reset_staging(self):
        # Reset re-seeds the preferred default rather than clearing to the legacy
        # <config>/Profiles location (which the screenshot showed sticking).
        self._seed_default_staging(self._game)
        self._staging_status.setText(self.tr("Default location will be used."))
        self._staging_status.setStyleSheet(f"color:{self._c('TEXT_DIM')};")

    def _reset_locations(self):
        self._found_path = None
        self._found_prefix = None
        self._game_edit.clear()
        self._prefix_edit.clear()
        self._start_game_scan()

    # ---- auto-detection (worker thread → signals) -------------------------
    def _start_game_scan(self, auto_apply=True):
        """Scan every launcher for the game. With *auto_apply* the first hit
        is written into the fields (fresh-game flow); without it the results
        only feed the launcher dropdown (configured-game flow)."""
        if auto_apply:
            self._game_status.setText(self.tr("Scanning Steam libraries…"))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        threading.Thread(target=self._game_scan_worker, args=(auto_apply,),
                         daemon=True).start()

    def _game_scan_worker(self, auto_apply=True):
        from Utils.app_log import app_log
        g = self._game
        # One candidate per launcher the game is detected on, in the same
        # priority order the old first-hit scan used (Heroic > Lutris >
        # Faugus > Steam), so candidates[0] is what that scan would have
        # returned.
        candidates: list[dict] = []

        def _add(source, path, prefix, launcher_id):
            candidates.append({"source": source, "path": path,
                               "prefix": prefix, "id": launcher_id})

        game_name = getattr(g, "name", repr(g))
        app_log(f"[Configure Game] Auto-detecting: {game_name}")
        try:
            from Utils.steam_finder import (
                find_steam_libraries, find_game_by_steam_id, find_game_in_libraries)
            from Utils.heroic_finder import (
                find_heroic_game_info_by_app_names, find_heroic_game_info_by_exe)
            exe_names = [getattr(g, "exe_name", None)] + list(
                getattr(g, "exe_name_alts", []) or [])
            exe_names = [e for e in exe_names if e]
            heroic_names = _heroic_app_names(g)
            if heroic_names:
                # Declared app names are authoritative: generic launcher names
                # collide across games (FalloutLauncher.exe ships with both
                # Fallout 3 GOTY and classic Fallout on GOG), so the exe scan
                # can resolve to the wrong title and is skipped entirely.
                app_log(f"[Configure Game] Checking Heroic app names: {heroic_names}")
                info = find_heroic_game_info_by_app_names(heroic_names)
                if info:
                    _add("heroic", info[0], info[1], info[2])
                    app_log(f"[Configure Game] Found via Heroic app name "
                            f"({info[2]}): {info[0]}")
            else:
                app_log(f"[Configure Game] Checking Heroic (exe names: {exe_names})")
                for exe in exe_names:
                    info = find_heroic_game_info_by_exe(exe)
                    if info:
                        _add("heroic", info[0], info[1], info[2])
                        app_log(f"[Configure Game] Found via Heroic exe scan ({exe}): {info[0]}")
                        break
            from Utils.lutris_finder import find_lutris_game_info_by_exe
            app_log(f"[Configure Game] Checking Lutris (exe names: {exe_names})")
            for exe in exe_names:
                info = find_lutris_game_info_by_exe(exe)
                if info:
                    _add("lutris", info[0], info[1], info[2])
                    app_log(f"[Configure Game] Found via Lutris ({exe}): {info[0]}")
                    break
            from Utils.faugus_finder import find_faugus_game_info_by_exe
            app_log(f"[Configure Game] Checking Faugus (exe names: {exe_names})")
            for exe in exe_names:
                info = find_faugus_game_info_by_exe(exe)
                if info:
                    _add("faugus", info[0], info[1], info[2])
                    app_log(f"[Configure Game] Found via Faugus ({exe}): {info[0]}")
                    break
            libs = find_steam_libraries()
            app_log(f"[Configure Game] Steam libraries found: "
                    f"{libs if libs else 'none'}")
            found = None
            sid = getattr(g, "steam_id", None)
            if sid:
                app_log(f"[Configure Game] Checking Steam manifest "
                        f"(app ID: {sid}, exes: {exe_names})")
                for exe in exe_names:
                    found = find_game_by_steam_id(libs, sid, exe)
                    if found:
                        app_log(f"[Configure Game] Found via Steam manifest "
                                f"({exe}): {found}")
                        break
            else:
                app_log("[Configure Game] No Steam app ID configured for this game")
            if not found:
                app_log("[Configure Game] Falling back to exe scan across Steam libraries")
                for exe in exe_names:
                    found = find_game_in_libraries(libs, exe)
                    if found:
                        app_log(f"[Configure Game] Found via Steam exe scan "
                                f"({exe}): {found}")
                        break
                else:
                    app_log(f"[Configure Game] Not found via Steam exe scan "
                            f"(tried: {exe_names})")
            if found:
                # No prefix here: the Steam compatdata lookup needs the game
                # path to pick the right library, so it runs as a follow-up
                # scan once this candidate is applied.
                _add("steam", found, None, None)
        except Exception as exc:
            import traceback
            app_log(f"[Configure Game] Scan failed: {exc}\n{traceback.format_exc()}")
        if not candidates:
            app_log(f"[Configure Game] Game location not auto-detected for: {game_name}")
        safe_emit(self._sig.game_found, candidates, auto_apply)

    def _on_game_found(self, candidates, auto_apply):
        self._populate_install_choices(candidates)
        if not auto_apply:
            return
        if candidates:
            self._apply_install_choice(candidates[0])
        elif getattr(self._game, "auto_drive_scan", False):
            # Store-less games (manual downloads) can never be found by the
            # library scan - the drive scan IS their auto-detection.
            self._start_drive_scan()
        else:
            self._game_status.setText(
                self.tr("Not found automatically. Browse manually to locate the game folder."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")

    # ---- launcher dropdown -------------------------------------------------
    def _populate_install_choices(self, candidates):
        """Fill the launcher dropdown with every detected install, plus the
        current path when detection didn't produce it. Shown only when that
        leaves the user an actual choice (>= 2 entries)."""
        choices = []
        cur = self._found_path
        if cur is not None and not any(Path(c["path"]) == Path(cur)
                                       for c in candidates):
            choices.append({"source": "current", "path": cur,
                            "prefix": self._found_prefix, "id": None})
        choices.extend(candidates)
        self._install_choices = choices
        names = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris",
                 "faugus": "Faugus"}
        combo = self._install_combo
        combo.clear()
        for i, c in enumerate(choices):
            label = (self.tr("Current: {0}").format(c["path"])
                     if c["source"] == "current"
                     else f"{names.get(c['source'], c['source'])} - {c['path']}")
            combo.addItem(label)
            combo.setItemData(i, str(c["path"]), Qt.ToolTipRole)
        self._install_row.setVisible(len(choices) >= 2)
        self._sync_install_combo(cur)

    def _sync_install_combo(self, path):
        """Point the dropdown at the choice matching *path* (or nothing)."""
        combo = getattr(self, "_install_combo", None)
        if combo is None or not self._install_choices:
            return
        for i, c in enumerate(self._install_choices):
            if path is not None and Path(c["path"]) == Path(path):
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(-1)

    def _on_install_combo(self, index):
        if 0 <= index < len(self._install_choices):
            self._apply_install_choice(self._install_choices[index])

    def _apply_install_choice(self, c):
        """Write one scan candidate into the path fields + launcher-id state.
        The ids are mutually exclusive - switching launchers must not leak the
        previous selection's id into the save."""
        source = c["source"]
        self._found_heroic_app = c["id"] if source == "heroic" else None
        self._found_lutris_slug = c["id"] if source == "lutris" else None
        self._found_faugus_gameid = c["id"] if source == "faugus" else None
        if source == "current":
            self._set_game(Path(c["path"]), configured=True)
        else:
            self._set_game(Path(c["path"]), source=source)
        if c["prefix"] is not None:
            self._set_prefix(Path(c["prefix"]), configured=(source == "current"))
        elif self._has_prefix_src:
            self._start_prefix_scan()

    # ---- full-drive Scan button -------------------------------------------
    def _start_drive_scan(self):
        """The Scan button: walk every mounted drive for the game exe.

        Distinct from the automatic Steam/Heroic library detection that runs on
        open - this catches non-Steam / GOG / manually-installed copies the
        library scan can't see (Tk parity: the Tk Scan button did the same
        all-drives walk, not a Steam re-scan)."""
        g = self._game
        exe_names = [getattr(g, "exe_name", None)] + list(
            getattr(g, "exe_name_alts", []) or [])
        exe_names = [e for e in exe_names if e]
        if not exe_names:
            self._game_status.setText(
                self.tr("No executable name configured for this game."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
            return
        self._game_status.setText(self.tr("Scanning all drives…"))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        self._scan_btn.setEnabled(False)
        threading.Thread(target=self._drive_scan_worker, args=(exe_names,),
                         daemon=True, name="drive-scan").start()

    def _drive_scan_worker(self, exe_names):
        from Utils.app_log import app_log
        found = None
        try:
            from Utils.steam_finder import scan_drives_for_exe
            app_log(f"[Configure Game] Scanning all drives for: {exe_names}")
            found = scan_drives_for_exe(exe_names)
            app_log(f"[Configure Game] Drive scan result: {found or 'not found'}")
        except Exception as exc:
            import traceback
            app_log(f"[Configure Game] Drive scan failed: {exc}\n"
                    f"{traceback.format_exc()}")
            found = None
        safe_emit(self._sig.drive_scan_found, found)

    def _on_drive_scan_found(self, found):
        self._scan_btn.setEnabled(True)
        if found:
            self._set_game(Path(found), source="manual")
            self._game_status.setText(self.tr("Found via drive scan."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")
        else:
            self._game_status.setText(
                self.tr("Game executable not found on any drive."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")

    def _start_prefix_scan(self):
        self._prefix_status.setText(self.tr("Scanning for Proton prefix…"))
        self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        # Read the game path here, on the main thread, so the Steam lookup can
        # tell which library owns the app (a stale compatdata in another library
        # would otherwise win).
        try:
            game_path = self._found_path or (self._game.get_game_path()
                                             if self._game else None)
        except Exception:
            game_path = None
        threading.Thread(target=self._prefix_scan_worker, args=(game_path,),
                         daemon=True).start()

    def _prefix_scan_worker(self, game_path=None):
        g = self._game
        found = None
        lutris_slug = None
        faugus_gameid = None
        try:
            from Utils.steam_finder import find_prefix
            from Utils.heroic_finder import find_heroic_prefix
            sid = getattr(g, "steam_id", None)
            ids = [sid] + [str(s) for s in getattr(g, "alt_steam_ids", []) or [] if s]
            for s in [x for x in ids if x]:
                found = find_prefix(s, game_path)
                if found:
                    break
            if not found and _heroic_app_names(g):
                found = find_heroic_prefix(_heroic_app_names(g))
            exe_names = [getattr(g, "exe_name", None)] + list(
                getattr(g, "exe_name_alts", []) or [])
            exe_names = [e for e in exe_names if e]
            if not found:
                from Utils.lutris_finder import find_lutris_game_info_by_exe
                for exe in exe_names:
                    info = find_lutris_game_info_by_exe(exe)
                    if info and info[1] is not None:
                        found = info[1]
                        lutris_slug = info[2]
                        break
            if not found:
                from Utils.faugus_finder import find_faugus_game_info_by_exe
                for exe in exe_names:
                    info = find_faugus_game_info_by_exe(exe)
                    if info and info[1] is not None:
                        found = info[1]
                        faugus_gameid = info[2]
                        break
        except Exception:
            found = None
        safe_emit(self._sig.prefix_found, found, lutris_slug, faugus_gameid)

    def _on_prefix_found(self, found, lutris_slug, faugus_gameid):
        if lutris_slug:
            self._found_lutris_slug = lutris_slug
        if faugus_gameid:
            self._found_faugus_gameid = faugus_gameid
        if found:
            self._set_prefix(Path(found))
        else:
            self._prefix_status.setText(
                self.tr("Prefix not found automatically. Not needed if game is Linux native."))
            self._prefix_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")

    # ---- save (live write) ------------------------------------------------
    def _on_save(self):
        g = self._game
        if self._found_path is None and self._game_edit.text().strip():
            self._found_path = Path(self._game_edit.text().strip())
        if self._found_path is None:
            self._game_status.setText(self.tr("Set the game installation folder first."))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
            return

        # Flatpak: a path outside the sandbox's filesystem grants looks like a
        # typo (it simply doesn't exist in here) - tell the user what it
        # actually is and how to grant access before letting them save a
        # config that can never work.
        from Utils.sandbox_paths import flatpak_blocked_path_hint
        for candidate, status in (
            (self._found_path, self._game_status),
            (self._staging_edit.text().strip() or None, self._staging_status),
        ):
            hint = flatpak_blocked_path_hint(candidate) if candidate else None
            if hint:
                status.setText(self.tr(
                    "This path is not visible inside the Flatpak sandbox. "
                    "Grant access in Flatseal or run: {0}").format(hint))
                status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
                return

        # Block path changes while deployed (would strand deployed files).
        if g.is_configured() and g.get_deploy_active():
            def _changed(old, new):
                if old and new:
                    try:
                        return Path(old).resolve() != Path(new).resolve()
                    except Exception:
                        return str(old) != str(new)
                return bool(old) != bool(new)
            if _changed(g.get_game_path(), self._found_path) or (
                    self._found_prefix is not None
                    and _changed(g.get_prefix_path(), self._found_prefix)):
                self._game_status.setText(
                    self.tr("Cannot change the game/prefix path while mods are deployed. "
                    "Restore the game first."))
                self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
                return

        mode = (LinkMode.HARDLINK if self._rb_hardlink.isChecked()
                else LinkMode.SYMLINK)

        # Capture the staging root currently on disk, before any setters mutate
        # it - needed to offer a migration if the staging location changed.
        old_profile_root: Path | None = None
        try:
            if g.is_configured():
                old_profile_root = g.get_profile_root()
        except Exception:
            old_profile_root = None

        # -- Hard-link cross-device validation --------------------------------
        # Hardlinks can't span filesystems. Apply the pending paths so the game
        # can resolve its deploy targets (game dir, and for BG3/Sims 4/etc the
        # Proton prefix or native data dir), then block the save if any target
        # is on a different drive than the staging folder (Tk parity:
        # add_game_dialog save-time check). Setters persist, but so does the
        # save we're about to do - an invalid mode is never written.
        if mode == LinkMode.HARDLINK:
            g.set_game_path(self._found_path)
            if self._found_prefix is not None and hasattr(g, "set_prefix_path"):
                g.set_prefix_path(self._found_prefix)
            if hasattr(g, "set_staging_path"):
                g.set_staging_path(self._custom_staging)
            from Utils.hardlink_check import hardlink_device_mismatches
            mismatched = hardlink_device_mismatches(g)
            if mismatched:
                names = " and ".join(mismatched)
                self._game_status.setText(self.tr(
                    "Cannot use hardlinks: the staging folder and {0} are on "
                    "different drives or filesystems. Switch to Symlink instead."
                ).format(names))
                self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
                return
        # ---------------------------------------------------------------------

        # Persist via the backend setters (live write to paths.json / overrides).
        g.set_game_path(self._found_path)
        if self._found_prefix is not None:
            g.set_prefix_path(self._found_prefix)
        if self._found_lutris_slug and hasattr(g, "set_lutris_slug"):
            g.set_lutris_slug(self._found_lutris_slug)
        if self._found_heroic_app and hasattr(g, "set_heroic_app_name"):
            g.set_heroic_app_name(self._found_heroic_app)
        if self._found_faugus_gameid and hasattr(g, "set_faugus_gameid"):
            g.set_faugus_gameid(self._found_faugus_gameid)
        if hasattr(g, "set_deploy_mode"):
            g.set_deploy_mode(mode)
        if hasattr(g, "set_staging_path"):
            g.set_staging_path(self._custom_staging)
        if hasattr(g, "set_save_path_override"):
            g.set_save_path_override(self._custom_saves)
        if hasattr(g, "set_script_extender_swap") and "script_extender_swap" in self._opt_checks:
            g.set_script_extender_swap(self._opt_checks["script_extender_swap"].isChecked())
        if hasattr(g, "set_auto_4gb_patch") and "auto_4gb_patch" in self._opt_checks:
            g.set_auto_4gb_patch(self._opt_checks["auto_4gb_patch"].isChecked())
        if "auto_deploy" in self._opt_checks:
            g.auto_deploy = self._opt_checks["auto_deploy"].isChecked()
        if "archive_invalidation" in self._opt_checks:
            g.archive_invalidation = self._opt_checks["archive_invalidation"].isChecked()
        if "case_alias_links" in self._opt_checks:
            g.case_alias_links = self._opt_checks["case_alias_links"].isChecked()
        if hasattr(g, "set_profile_ini_files") and "profile_ini_files" in self._opt_checks:
            g.set_profile_ini_files(self._opt_checks["profile_ini_files"].isChecked())
        if hasattr(g, "set_profile_saves") and "profile_saves" in self._opt_checks:
            g.set_profile_saves(self._opt_checks["profile_saves"].isChecked())
        if hasattr(g, "prefix_numbering") and "prefix_numbering" in self._opt_checks:
            g.prefix_numbering = self._opt_checks["prefix_numbering"].isChecked()
        if (hasattr(g, "set_manage_load_order_in_dfu")
                and "manage_load_order_in_dfu" in self._opt_checks):
            g.set_manage_load_order_in_dfu(
                self._opt_checks["manage_load_order_in_dfu"].isChecked())
        # Not persisted config: the read-only bit on Steam's appmanifest IS the
        # setting, so this applies it to disk rather than storing a flag.
        if "disable_steam_updates" in self._opt_checks:
            self._apply_steam_updates_setting()
        if hasattr(g, "set_patch_version") and self._patch_group is not None:
            for val, rb in self._patch_buttons.items():
                if rb.isChecked():
                    g.set_patch_version(val)
                    break
        if (self._plugins_txt_group is not None
                and hasattr(g, "set_plugins_txt_filename")):
            for fname, rb in self._plugins_txt_buttons.items():
                if rb.isChecked():
                    g.set_plugins_txt_filename(fname)
                    break

        # If the staging root moved and the old root has content, offer to
        # migrate the existing mods/profiles/overwrite tree before finalizing.
        new_profile_root: Path | None = None
        try:
            new_profile_root = g.get_profile_root()
        except Exception:
            new_profile_root = None
        from Utils.staging_migrate import staging_move_needed
        if staging_move_needed(old_profile_root, new_profile_root):
            self._start_staging_scan(old_profile_root, new_profile_root)
            return

        self._finalize_save()

    def _finalize_save(self):
        # Ensure the profile structure exists (mods/profiles/overwrite + default).
        try:
            from Utils.profile_structure import create_profile_structure
            create_profile_structure(self._game)
        except Exception as exc:
            print(f"[gui_qt] profile structure create failed: {exc}", flush=True)

        # Silently install this game's prefix dependencies (vcredist /
        # d3dcompiler_47) in the background, exactly like the Tk add dialog did.
        self._install_prefix_deps()

        self._on_done(True, False)

    # ---- staging migration --------------------------------------------------
    def _start_staging_scan(self, old_root: Path, new_root: Path):
        """Staging root changed - size up the old tree off-thread, then offer
        to move it. The new path is already saved, so Skip just leaves the old
        files behind (Tk parity)."""
        self._save_btn.setEnabled(False)
        self._game_status.setText(self.tr("Checking existing staging files…"))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        sig = self._sig

        def scan():
            from Utils.staging_migrate import collect_staging_files
            files, size = collect_staging_files(old_root)
            return old_root, new_root, files, size

        run_in_worker(scan, sig.staging_scanned, name="staging-scan",
                      unpack=True, error_result=NO_EMIT)

    def _on_staging_scanned(self, old_root, new_root, files, size):
        if not files:
            self._finalize_save()
            return
        from Utils.prefix_manager import fmt_size
        from gui_qt.confirm_overlay import ConfirmOverlay
        body = (f"The staging location for {self._game.name} has changed.\n\n"
                f"Move {fmt_size(size)} of mods, profiles and overwrite "
                f"files from\n{old_root}\nto\n{new_root}?\n\n"
                "Existing items at the destination are kept; only items not "
                "already present at the new location are moved.")
        ConfirmOverlay.show_over(
            self, "Move Mod Staging Files?", body,
            lambda ok: (self._run_staging_move(old_root, new_root, files)
                        if ok else self._finalize_save()),
            confirm_label=self.tr("Move"), cancel_label=self.tr("Skip"),
            danger=False, card_h=340)

    def _run_staging_move(self, old_root, new_root, files):
        from gui_qt.notifications import ProgressPopup
        self._game_status.setText(self.tr("Moving staging files…"))
        self._staging_popup = ProgressPopup(self.window())
        self._staging_popup.set_progress(0, len(files), phase=str(old_root),
                                         title=self.tr("Moving Mod Staging Files"))
        sig = self._sig
        game_name = self._game.name

        def _prog(done, total, msg):
            if done == total or done % 20 == 0:
                safe_emit(sig.staging_progress, done, total, msg)

        def worker():
            from Utils.app_log import app_log
            from Utils.staging_migrate import migrate_staging_files
            moved, skipped, failed = migrate_staging_files(
                old_root, new_root, files, progress_cb=_prog, log_fn=app_log)
            app_log(f"{game_name}: moved {moved} staging file(s) to {new_root}"
                    + (f", skipped {skipped}" if skipped else "")
                    + (f", failed {failed}" if failed else ""))
            safe_emit(sig.staging_move_done, moved, skipped, failed)

        threading.Thread(target=worker, daemon=True,
                         name="staging-migrate").start()

    def _on_staging_progress(self, done, total, msg):
        if self._staging_popup is not None:
            self._staging_popup.set_progress(done, total, phase=msg)

    def _on_staging_move_done(self, moved, skipped, failed):
        if self._staging_popup is not None:
            self._staging_popup.clear()
            self._staging_popup.deleteLater()
            self._staging_popup = None
        if moved:
            self.staging_migrated = True
        self._finalize_save()

    def _install_prefix_deps(self) -> None:
        """Silently install this game's prefix dependencies in the background.

        Two mechanisms, both skipped when no Proton prefix is available:
          * ``auto_install_deps`` - vcredist / d3dcompiler_47 via the same
            installers the Proton Tools menu uses (preferred; see base_game).
          * ``winetricks_components`` - legacy winetricks verbs.

        Progress is reported via ``Utils.app_log.app_log`` (thread-safe; wired
        into the Qt log panel by gui_qt.glue), so this worker touches no widgets.
        """
        game = self._game
        prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
        if not (prefix and Path(prefix).is_dir()):
            return
        prefix = Path(prefix)

        deps = list(getattr(game, "auto_install_deps", []))
        components = list(getattr(game, "winetricks_components", []))
        if not deps and not components:
            return

        def _worker():
            from Utils.app_log import app_log
            from Utils.protontricks import (
                D3D_DEP_KEY,
                VCREDIST_DEP_KEY,
                _install_via_winetricks,
                build_proton_env_for_game,
                install_d3dcompiler_47,
                install_vcredist,
                is_dep_installed,
                winetricks_verb_dep_key,
            )
            from Utils.proton_tools import install_lavfilters
            from Utils.steam_finder import game_steam_id

            _proton: tuple = ()

            def _ensure_proton():
                nonlocal _proton
                if not _proton:
                    _proton = build_proton_env_for_game(game)
                return _proton

            installed: list[str] = []
            skipped: list[str] = []
            failed: list[str] = []

            app_log(f"{game.name}: checking prefix dependencies …")

            for dep in deps:
                if dep == "vcredist":
                    if is_dep_installed(prefix, VCREDIST_DEP_KEY):
                        app_log(f"{game.name}: VC++ Redistributable already installed - skipping.")
                        skipped.append("vcredist")
                        continue
                    proton_script, env = _ensure_proton()
                    if proton_script is None:
                        app_log(f"{game.name}: skipping vcredist - no Proton prefix available.")
                        skipped.append("vcredist")
                        continue
                    app_log(f"{game.name}: auto-installing VC++ Redistributable …")
                    ok = install_vcredist(proton_script, env, log_fn=app_log, prefix_path=prefix)
                    (installed if ok else failed).append("vcredist")
                elif dep == "d3dcompiler_47":
                    if is_dep_installed(prefix, D3D_DEP_KEY):
                        app_log(f"{game.name}: d3dcompiler_47 already installed - skipping.")
                        skipped.append("d3dcompiler_47")
                        continue
                    app_log(f"{game.name}: auto-installing d3dcompiler_47 …")
                    ok = install_d3dcompiler_47(
                        game_steam_id(game), log_fn=app_log, prefix_path=prefix)
                    (installed if ok else failed).append("d3dcompiler_47")
                elif dep == "lavfilters":
                    # Same installer the Proton Tools menu entry uses, so a
                    # manual install and this one share the skip marker.
                    if is_dep_installed(prefix, winetricks_verb_dep_key("lavfilters")):
                        app_log(f"{game.name}: LAV Filters already installed - skipping.")
                        skipped.append("lavfilters")
                        continue
                    app_log(f"{game.name}: auto-installing LAV Filters (radio/music codecs) …")
                    ok = install_lavfilters(game, log_fn=app_log)
                    (installed if ok else failed).append("lavfilters")
                else:
                    app_log(f"{game.name}: unknown auto_install dep '{dep}' - skipping.")
                    skipped.append(dep)

            for comp in components:
                app_log(f"{game.name}: installing {comp} via winetricks …")
                if _install_via_winetricks(prefix, comp, app_log):
                    installed.append(comp)
                else:
                    app_log(f"{game.name}: {comp} install failed (see log above).")
                    failed.append(comp)

            summary = []
            if installed:
                summary.append(f"installed {', '.join(installed)}")
            if skipped:
                summary.append(f"skipped {', '.join(skipped)}")
            if failed:
                summary.append(f"FAILED {', '.join(failed)}")
            app_log(
                f"{game.name}: prefix dependency setup done"
                + (f" - {'; '.join(summary)}." if summary else ".")
            )

        threading.Thread(target=_worker, daemon=True,
                         name="install-prefix-deps").start()

    # ---- destructive actions ----------------------------------------------
    def _confirm(self, title, text, on_yes):
        """Borderless in-app confirm; runs *on_yes* only on confirmation."""
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(self, title, text,
                                 lambda ok: on_yes() if ok else None,
                                 confirm_label=self.tr("Yes"))

    def _on_remove(self):
        g = self._game
        msg = self.tr(
            "Remove the instance configuration for {0}?\n\n"
            "Deleted: game config + generated caches; the game is restored to "
            "vanilla.\nKept: your mods, profiles, and overwrite folders.\n\n"
            "This cannot be undone.").format(g.name)
        self._confirm(self.tr("Remove Instance - {0}").format(g.name), msg,
                      self._do_remove)

    def _do_remove(self):
        """Restore the game to vanilla + drop config/caches on a daemon worker
        (a big restore + rmtree freezes the UI for many seconds otherwise)."""
        if self._destructive_busy:
            return
        self._destructive_busy = True
        self._set_destructive_enabled(False)
        self._game_status.setText(self.tr("Removing instance…"))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        g = self._game
        sig = self._sig

        def worker():
            from Utils.config_paths import get_game_config_path
            profile_root = g.get_profile_root()
            paths_json = get_game_config_path(g.name)
            try:
                if hasattr(g, "restore"):
                    g.restore()
            except Exception:
                pass
            try:
                from Utils.deploy import restore_root_folder_for_game
                rf = profile_root / "Root_Folder"
                game_root = g.get_game_path()
                if rf.is_dir() and game_root:
                    restore_root_folder_for_game(
                        g, root_folder_dir=rf, game_root=game_root,
                    )
            except Exception:
                pass
            keep = {"mods", "profiles", "overwrite"}
            if profile_root.is_dir():
                for child in profile_root.iterdir():
                    if child.name in keep:
                        continue
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            cfg_dir = paths_json.parent
            if cfg_dir.is_dir():
                shutil.rmtree(cfg_dir, ignore_errors=True)
            safe_emit(sig.remove_done)

        threading.Thread(target=worker, daemon=True,
                         name="remove-instance").start()

    def _on_remove_finished(self):
        self._destructive_busy = False
        self._on_done(False, True)

    def _set_destructive_enabled(self, on: bool) -> None:
        for b in (getattr(self, "_remove_btn", None),
                  getattr(self, "_clean_btn", None)):
            if b is not None:
                b.setEnabled(on)

    def _on_clean(self):
        g = self._game
        game_path = g.get_game_path()
        if not game_path:
            return
        target = game_path
        if hasattr(g, "get_mod_data_path"):
            dp = g.get_mod_data_path()
            if dp and dp != game_path:
                target = dp
        if not target or not Path(target).is_dir():
            return
        msg = self.tr(
            "Scan {0} and remove leftover deployed mod files (hardlinks/"
            "symlinks/copies) that weren't restored?\n\nVanilla game files are "
            "kept. This cannot be undone.").format(target)
        self._confirm(self.tr("Clean Game Folder - {0}").format(g.name), msg,
                      lambda: self._do_clean(target))

    def _do_clean(self, target):
        """Scan the game folder for leftover deployed files on a daemon worker
        (the scan walks the whole install - a long freeze on the GUI thread)."""
        if self._destructive_busy:
            return
        self._destructive_busy = True
        self._set_destructive_enabled(False)
        self._game_status.setText(self.tr("Cleaning game folder…"))
        self._game_status.setStyleSheet(f"color:{self._c('TEXT_WARN')};")
        g = self._game
        sig = self._sig

        def worker():
            try:
                from Utils.deploy import (
                    remove_deployed_files, restore_filemap_from_root)
                tgt = Path(target)
                removed = 0
                if hasattr(g, "get_effective_filemap_path"):
                    try:
                        fm = g.get_effective_filemap_path()
                        removed += restore_filemap_from_root(
                            fm, tgt, move_runtime_files=False)
                    except Exception:
                        pass
                removed += remove_deployed_files(tgt)
                if hasattr(g, "post_clean_game_folder"):
                    try:
                        g.post_clean_game_folder()
                    except Exception:
                        pass
                safe_emit(sig.clean_done, removed, None)
            except Exception as exc:
                safe_emit(sig.clean_done, None, str(exc))

        threading.Thread(target=worker, daemon=True,
                         name="clean-game-folder").start()

    def _on_clean_finished(self, removed, error):
        self._destructive_busy = False
        self._set_destructive_enabled(True)
        if error is not None:
            self._game_status.setText(self.tr("Clean failed: {0}").format(error))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
        else:
            self._game_status.setText(
                self.tr("Clean complete - {0} deployed file(s) removed.").format(removed))
            self._game_status.setStyleSheet(f"color:{self._c('TEXT_OK')};")
