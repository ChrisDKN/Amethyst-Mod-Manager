"""Post-deploy overlay offering a Steam launch command.

Games whose mods are served by an external loader (Elden Ring via me3) cannot
be started from Steam normally - Steam would run the vanilla exe and the loader
would never be injected, which looks like a successful launch but is completely
unmodded.  Pressing Play in the manager works, but people expect their library
to work too, especially in Steam's Big Picture / Game Mode on a Deck.

The fix is a Launch Option that runs our CLI instead, which deploys and then
execs the loader.  This overlay shows that command after a deploy and copies it
to the clipboard, the same shape as the Mewgenics one
(gui_qt/mewgenics_deploy_overlay.py).

Driven by ``BaseGame.get_steam_launch_string()``, so any future handler with a
native launch command gets this for free.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QCheckBox,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class SteamLaunchCommandOverlay(OverlayBase):
    """Show the Steam launch command for a loader-based game.

    ``on_done`` receives True when the user ticked "don't show this again".
    """

    CARD_W = 620
    CARD_H = 400
    MIN_W = 400
    MIN_H = 300
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, game_name: str, launch_string: str,
                 on_done=None):
        super().__init__(host, on_done=on_done)
        self._launch_string = launch_string
        p = active_palette()
        _card, v = self._make_card("SteamLaunchCard")

        title = QLabel(self.tr("{0} - launching from Steam").format(game_name))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        sub = QLabel(self.tr(
            "Your mods are loaded by an external mod loader, so starting "
            "{0} from Steam normally would run it unmodded.\n\n"
            "Press Play here in the manager, or paste this into Steam "
            "(right-click {0} → Properties → General → Launch Options) to "
            "launch it modded from your library:").format(game_name))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        sub.setWordWrap(True)
        v.addWidget(sub)

        self._area = QPlainTextEdit()
        self._area.setReadOnly(True)
        self._area.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._area.setStyleSheet(
            f"QPlainTextEdit {{ background:{_c(p,'BG_DEEP')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:5px; padding:6px; font-family:monospace; }}")
        self._area.setPlainText(launch_string)
        v.addWidget(self._area, 1)

        note = QLabel(self.tr(
            "Set this once. It always deploys and launches whichever profile "
            "you last deployed here, so switching profiles needs no change in "
            "Steam."))
        note.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        note.setWordWrap(True)
        v.addWidget(note)

        self._hide_chk = QCheckBox(self.tr("Don't show this again"))
        self._hide_chk.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(self._hide_chk)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._close)
        bar.addWidget(close)
        self._copy_btn = QPushButton(self.tr("Copy to clipboard"))
        self._copy_btn.setObjectName("PrimaryButton")
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy)
        bar.addWidget(self._copy_btn)
        v.addLayout(bar)

        self._present()
        self._copy()   # auto-copy on open

    def _copy(self):
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self._launch_string)
            self._copy_btn.setText(self.tr("Copied ✓"))
        else:
            self._copy_btn.setText(self.tr("Copy failed - copy it manually"))

    def _close(self):
        self._finish(bool(self._hide_chk.isChecked()))
