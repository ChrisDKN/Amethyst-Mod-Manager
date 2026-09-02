"""onboarding_view.py - first-run onboarding, shown as a fullscreen detachable
tab (like the Nexus browser). Qt port of gui/onboarding_panel.py.

Pages:
  0 - Welcome        (Next button)
  1 - Nexus login    (optional, skippable; Skip becomes Next after login)
  2 - Configure paths + Add a game (opens the game picker)

The OAuth flow itself lives in the app (MainWindow._nexus_login_sso /
_on_oauth_event). This view just fires `on_login()` and reacts to the app
calling `on_logged_in()` back on it - no OAuth client is owned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QStackedWidget, QFrame, QComboBox,
)

from gui_qt.theme_qt import active_palette, _c, button_qss
from gui_qt.safe_emit import safe_emit
from gui_qt.wheel_guard import no_wheel
from Utils.config_paths import get_default_staging_root, get_config_dir
from Utils.ui import config as uc
from Utils.ui.config import (
    load_default_staging_path, save_default_staging_path,
    load_download_cache_path, save_download_cache_path,
)
from Utils.environment.xdg import open_url

_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"
_WIKI_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/wiki"
_TOTAL_PAGES = 3


def _logo(name: str, size: int) -> QPixmap | None:
    path = _ICONS_DIR / name
    if not path.is_file():
        return None
    pm = QPixmap(str(path))
    if pm.isNull():
        return None
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class OnboardingView(QWidget):
    """First-run onboarding. Callbacks (mirroring the Tk panel):
      * on_login()     - start the browser OAuth flow (app._nexus_login_sso)
      * on_add_game()  - open the Add-Game picker (app._open_add_game_tab)
      * on_done()      - dismiss onboarding (app._finish_onboarding)

    *already_logged_in* skips the Nexus page (Tk parity).
    """

    # Portal picker callbacks fire on a worker thread - marshal the result back
    # onto the GUI thread before touching the line edits.
    _folder_picked = Signal(str, str)   # (which, path)

    def __init__(
        self,
        on_login: Optional[Callable] = None,
        on_add_game: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        already_logged_in: bool = False,
        on_language_change: Optional[Callable] = None,
        on_sync_languages: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._on_login = on_login or (lambda: None)
        self._on_add_game = on_add_game or (lambda: None)
        self._on_done = on_done or (lambda: None)
        # on_language_change(code) - app re-installs translators + retranslates;
        # on_sync_languages() - app pulls latest .qm from Resources.
        self._on_language_change = on_language_change or (lambda _c: None)
        self._on_sync_languages = on_sync_languages or (lambda: None)
        self._already_logged_in = already_logged_in
        self._logged_in = already_logged_in
        self._lang_combo: QComboBox | None = None

        self._page = 0
        self._pal = active_palette()
        self._nexus_status: QLabel | None = None
        self._sso_btn: QPushButton | None = None
        self._staging_edit: QLineEdit | None = None
        self._cache_edit: QLineEdit | None = None

        self._folder_picked.connect(self._on_folder_picked)
        self._build()
        self._show_page(0)

    # ------------------------------------------------------------------ build
    def _build(self):
        pal = self._pal
        self.setStyleSheet(f"background: {_c(pal, 'BG_DEEP')};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- header --
        header = QFrame()
        header.setFixedHeight(48)
        header.setStyleSheet(f"background: {_c(pal, 'BG_HEADER')};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        title = QLabel(self.tr("Welcome to Amethyst Mod Manager"))
        title.setStyleSheet(
            f"color: {_c(pal, 'TEXT_MAIN')}; font-size: 15px; font-weight: 600;")
        hl.addWidget(title)
        hl.addStretch(1)
        self._step_label = QLabel(self.tr("Step 1 of {0}").format(_TOTAL_PAGES))
        self._step_label.setStyleSheet(
            f"color: {_c(pal, 'TEXT_DIM')}; font-size: 12px;")
        hl.addWidget(self._step_label)
        root.addWidget(header)

        # -- content stack --
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_page_welcome())   # 0
        self._stack.addWidget(self._build_page_nexus())      # 1
        self._stack.addWidget(self._build_page_add_game())   # 2
        root.addWidget(self._stack, 1)

        # -- footer --
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {_c(pal, 'BORDER')};")
        root.addWidget(sep)

        footer = QFrame()
        footer.setFixedHeight(52)
        footer.setStyleSheet(f"background: {_c(pal, 'BG_HEADER')};")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(12, 10, 12, 10)

        self._prev_btn = QPushButton(self.tr("← Back"))
        self._prev_btn.setFixedWidth(100)
        self._prev_btn.setStyleSheet(self._neutral_btn_qss())
        self._prev_btn.clicked.connect(self._on_prev_btn)
        fl.addWidget(self._prev_btn)
        fl.addStretch(1)

        self._footer_btn = QPushButton(self.tr("Next →"))
        self._footer_btn.setFixedWidth(120)
        self._footer_btn.clicked.connect(self._on_footer_btn)
        fl.addWidget(self._footer_btn)
        root.addWidget(footer)

    # ------------------------------------------------------------------ styles
    def _accent_btn_qss(self) -> str:
        pal = self._pal
        return (
            f"QPushButton {{ background: {_c(pal, 'ACCENT')};"
            f" color: {_c(pal, 'TEXT_ON_ACCENT')}; border: none;"
            f" border-radius: 4px; padding: 6px 12px; font-weight: 600; }}"
            f" QPushButton:hover {{ background: {_c(pal, 'ACCENT_HOV')}; }}")

    def _neutral_btn_qss(self) -> str:
        pal = self._pal
        return (
            f"QPushButton {{ background: {_c(pal, 'BG_PANEL')};"
            f" color: {_c(pal, 'TEXT_DIM')}; border: 1px solid {_c(pal, 'BORDER')};"
            f" border-radius: 4px; padding: 6px 12px; }}"
            f" QPushButton:hover {{ background: {_c(pal, 'BG_ROW_HOVER')}; }}")

    def _orange_btn_qss(self) -> str:
        return button_qss("BTN_WARN", padding="8px 16px")

    def _card(self) -> "tuple[QWidget, QFrame]":
        """A centered panel card matching the Tk inner frame. Returns
        (outer container to add to the stack, inner card to fill with content)."""
        pal = self._pal
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(60, 40, 60, 40)
        card = QFrame()
        card.setStyleSheet(
            f"background: {_c(pal, 'BG_PANEL')}; border-radius: 10px;")
        ol.addWidget(card)
        return outer, card

    # ------------------------------------------------------------- page 0 welcome
    def _build_page_welcome(self) -> QWidget:
        pal = self._pal
        outer, card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(40, 30, 40, 30)
        v.setAlignment(Qt.AlignCenter)

        pm = _logo("Logo.png", 120)
        if pm is not None:
            img = QLabel()
            img.setPixmap(pm)
            img.setAlignment(Qt.AlignCenter)
            v.addWidget(img, 0, Qt.AlignCenter)
            v.addSpacing(20)

        heading = QLabel(self.tr("Welcome to Amethyst Mod Manager"))
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet(
            f"color: {_c(pal, 'TEXT_MAIN')}; font-size: 16px; font-weight: 600;")
        v.addWidget(heading)
        v.addSpacing(10)

        body = QLabel(self.tr("See the wiki for guides on how to use the Manager"))
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setStyleSheet(f"color: {_c(pal, 'TEXT_DIM')}; font-size: 13px;")
        v.addWidget(body)
        v.addSpacing(20)

        # Language picker + sync - so users can switch language before going on.
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(0, 0, 0, 0)
        lang_row.addStretch(1)
        self._lang_label = QLabel(self.tr("Language:"))
        self._lang_label.setStyleSheet(f"color: {_c(pal, 'TEXT_DIM')};")
        lang_row.addWidget(self._lang_label)
        self._lang_combo = QComboBox()
        no_wheel(self._lang_combo)
        self._populate_language_combo()
        self._lang_combo.currentIndexChanged.connect(self._on_lang_selected)
        lang_row.addWidget(self._lang_combo)
        self._lang_sync_btn = QPushButton(self.tr("Sync language files"))
        self._lang_sync_btn.setCursor(Qt.PointingHandCursor)
        self._lang_sync_btn.setStyleSheet(self._neutral_btn_qss())
        self._lang_sync_btn.clicked.connect(lambda: self._on_sync_languages())
        lang_row.addWidget(self._lang_sync_btn)
        lang_row.addStretch(1)
        lang_holder = QWidget(); lang_holder.setLayout(lang_row)
        v.addWidget(lang_holder)
        v.addSpacing(20)

        wiki = QPushButton(self.tr("Open Wiki"))
        wiki.setFixedWidth(160)
        wiki.setStyleSheet(self._orange_btn_qss())
        wiki.clicked.connect(lambda: open_url(_WIKI_URL))
        v.addWidget(wiki, 0, Qt.AlignCenter)
        return outer

    def _populate_language_combo(self):
        """(Re)fill the welcome-page language combo from available_languages(),
        storing each locale code as item-data and preserving the selection."""
        combo = self._lang_combo
        if combo is None:
            return
        from gui_qt.i18n import available_languages
        combo.blockSignals(True)
        current = uc.load_language()
        combo.clear()
        sel = 0
        for i, (disp, code) in enumerate(available_languages()):
            combo.addItem(disp, userData=code)
            if code == current:
                sel = i
        combo.setCurrentIndex(sel)
        combo.blockSignals(False)

    def _on_lang_selected(self, idx: int):
        """User changed the onboarding language: persist + ask the app to apply
        it immediately (re-install translators + retranslate)."""
        if self._lang_combo is None:
            return
        code = self._lang_combo.itemData(idx) or ""
        uc.save_language(code)
        self._on_language_change(code)

    def refresh_language_options(self):
        """A background/manual sync added .qm files - refresh the picker."""
        self._populate_language_combo()

    # -------------------------------------------------------------- page 1 nexus
    def _build_page_nexus(self) -> QWidget:
        pal = self._pal
        outer, card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(40, 30, 40, 30)
        v.setAlignment(Qt.AlignCenter)

        pm = _logo("nexus.png", 80)
        if pm is not None:
            img = QLabel()
            img.setPixmap(pm)
            img.setAlignment(Qt.AlignCenter)
            v.addWidget(img, 0, Qt.AlignCenter)
            v.addSpacing(16)

        heading = QLabel(self.tr("Connect to Nexus Mods"))
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet(
            f"color: {_c(pal, 'TEXT_MAIN')}; font-size: 16px; font-weight: 600;")
        v.addWidget(heading)
        v.addSpacing(8)

        desc = QLabel(
            self.tr("Logging in lets you browse and download mods directly within the app.\n"
            "You can skip this and connect later from the Nexus button in the toolbar."))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        desc.setStyleSheet(f"color: {_c(pal, 'TEXT_DIM')}; font-size: 13px;")
        v.addWidget(desc)
        v.addSpacing(24)

        self._sso_btn = QPushButton(self.tr("Log in via Nexus Mods"))
        self._sso_btn.setFixedWidth(220)
        self._sso_btn.setStyleSheet(self._orange_btn_qss())
        self._sso_btn.clicked.connect(self._on_sso_login)
        v.addWidget(self._sso_btn, 0, Qt.AlignCenter)
        v.addSpacing(8)

        self._nexus_status = QLabel("")
        self._nexus_status.setWordWrap(True)
        self._nexus_status.setAlignment(Qt.AlignCenter)
        self._nexus_status.setStyleSheet(
            f"color: {_c(pal, 'TEXT_DIM')}; font-size: 12px;")
        v.addWidget(self._nexus_status)
        return outer

    def _on_sso_login(self):
        if self._sso_btn is not None:
            self._sso_btn.setEnabled(False)
            self._sso_btn.setText(self.tr("Waiting for browser..."))
        self._set_nexus_status(
            self.tr("Browser login started - complete it in your browser."),
            _c(self._pal, "TEXT_DIM"))
        self._on_login()

    def on_logged_in(self):
        """Called by the app when OAuth completes while onboarding is open."""
        self._logged_in = True
        if self._sso_btn is not None:
            self._sso_btn.setEnabled(True)
            self._sso_btn.setText(self.tr("Log in via Nexus Mods"))
        self._set_nexus_status(
            self.tr("✓ Logged in to Nexus Mods!"), _c(self._pal, "TEXT_OK_BRIGHT"))
        # Upgrade the footer from Skip → Next if we're on the Nexus page.
        if self._page == 1:
            self._apply_footer_style()

    def _set_nexus_status(self, text: str, color: str):
        if self._nexus_status is not None:
            self._nexus_status.setText(text)
            self._nexus_status.setStyleSheet(f"color: {color}; font-size: 12px;")

    # ---------------------------------------------------------- page 2 add game
    def _build_page_add_game(self) -> QWidget:
        pal = self._pal
        outer, card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(40, 30, 40, 30)
        v.setAlignment(Qt.AlignCenter)

        # -- Default mod staging folder --
        v.addWidget(self._section_title(self.tr("Default Mod Staging Folder")))
        # Display-only: new games default to ~/Games/Amethyst/<game>.  Nothing
        # is created here - the folder appears when the first game is saved.
        v.addWidget(self._hint(self.tr("Default: {0}").format(
            get_default_staging_root() / self.tr("<game name>"))))
        self._staging_edit = QLineEdit(load_default_staging_path())
        self._staging_edit.setPlaceholderText(self.tr("Leave blank to use the default"))
        v.addLayout(self._folder_row(self._staging_edit, "staging"))
        v.addWidget(self._hint(
            self.tr("When set, new games will use <this>/<game name> as their\n"
            "mod staging folder. You can change this later in Settings.")))
        v.addSpacing(16)

        # -- Download cache folder --
        v.addWidget(self._section_title(self.tr("Download Cache Folder")))
        v.addWidget(self._hint(self.tr("Default: {0}").format(get_config_dir() / 'download_cache')))
        self._cache_edit = QLineEdit(load_download_cache_path())
        self._cache_edit.setPlaceholderText(self.tr("Leave blank to use the default"))
        v.addLayout(self._folder_row(self._cache_edit, "cache"))
        v.addWidget(self._hint(
            self.tr("Where downloaded mod archives are stored.\n"
            "Each game gets its own subfolder.")))
        v.addSpacing(24)

        # -- Add first game --
        v.addWidget(self._section_title(self.tr("Add Your First Game")))
        v.addSpacing(8)
        add_row = QHBoxLayout()
        add_row.setAlignment(Qt.AlignCenter)
        lbl = QLabel(self.tr("Select a game to manage."))
        lbl.setStyleSheet(f"color: {_c(pal, 'TEXT_DIM')}; font-size: 13px;")
        add_row.addWidget(lbl)
        add_row.addSpacing(12)
        add_btn = QPushButton(self.tr("Add a Game"))
        add_btn.setFixedWidth(160)
        add_btn.setStyleSheet(self._accent_btn_qss())
        add_btn.clicked.connect(self._on_add_game_clicked)
        add_row.addWidget(add_btn)
        v.addLayout(add_row)
        return outer

    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {_c(self._pal, 'TEXT_MAIN')}; font-size: 14px; font-weight: 600;")
        return lbl

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color: {_c(self._pal, 'TEXT_DIM')}; font-size: 11px;")
        return lbl

    def _folder_row(self, edit: QLineEdit, which: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        edit.setFixedWidth(340)
        row.addWidget(edit)
        browse = QPushButton(self.tr("Browse"))
        browse.setFixedWidth(80)
        browse.setStyleSheet(self._neutral_btn_qss())
        browse.clicked.connect(lambda: self._browse(which))
        row.addWidget(browse)
        clear = QPushButton(self.tr("Clear"))
        clear.setFixedWidth(60)
        clear.setStyleSheet(self._neutral_btn_qss())
        clear.clicked.connect(lambda: edit.setText(""))
        row.addWidget(clear)
        return row

    def _browse(self, which: str):
        from Utils.ui.portal import pick_folder
        title = ("Select Default Mod Staging Folder" if which == "staging"
                 else "Select Download Cache Folder")

        def _on_chosen(chosen):
            # Fires on a worker thread - marshal to the GUI thread via a Signal.
            if chosen:
                safe_emit(self._folder_picked, which, str(chosen))

        pick_folder(title, _on_chosen)

    def _on_folder_picked(self, which: str, path: str):
        if which == "staging" and self._staging_edit is not None:
            self._staging_edit.setText(path)
        elif which == "cache" and self._cache_edit is not None:
            self._cache_edit.setText(path)

    def _on_add_game_clicked(self):
        self._save_paths()
        self._on_done()          # persists onboarding_complete + closes the tab
        self._on_add_game()      # open the Add-Game picker

    def _save_paths(self):
        try:
            if self._staging_edit is not None:
                save_default_staging_path(self._staging_edit.text())
        except Exception:
            pass
        try:
            if self._cache_edit is not None:
                save_download_cache_path(self._cache_edit.text())
        except Exception:
            pass

    # ------------------------------------------------------------------ nav
    def _show_page(self, page: int):
        self._page = page
        self._stack.setCurrentIndex(page)
        self._step_label.setText(self.tr("Step {0} of {1}").format(page + 1, _TOTAL_PAGES))
        self._prev_btn.setVisible(page != 0)
        self._apply_footer_style()

    def _apply_footer_style(self):
        """Footer is 'Next →' (accent) on welcome + logged-in Nexus; 'Skip'
        (neutral) on the not-logged-in Nexus page and the last page."""
        page = self._page
        if page == 0 or (page == 1 and self._logged_in):
            self._footer_btn.setText(self.tr("Next →"))
            self._footer_btn.setStyleSheet(self._accent_btn_qss())
        else:
            self._footer_btn.setText(self.tr("Skip"))
            self._footer_btn.setStyleSheet(self._neutral_btn_qss())

    def _on_prev_btn(self):
        if self._page > 0:
            target = self._page - 1
            # Skip the Nexus page going backwards if already logged in (Tk parity).
            if target == 1 and self._already_logged_in:
                target = 0
            self._show_page(target)

    def _on_footer_btn(self):
        if self._page == 0:
            self._show_page(2 if self._already_logged_in else 1)
        elif self._page == 1:
            self._show_page(2)
        else:
            # Skip on the last page - dismiss without adding a game.
            self._save_paths()
            self._on_done()
