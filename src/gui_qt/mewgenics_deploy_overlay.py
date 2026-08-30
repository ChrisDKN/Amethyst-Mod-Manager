"""Borderless in-window overlays for the Mewgenics deploy flow.

Mewgenics can be modded two ways (Tk parity - see gui/mewgenics_dialogs.py):

  * ``MewgenicsDeployChoiceOverlay`` - asks whether to generate a Steam launch
    command (safer, no repack) or to repack ``resources.gpak`` in place.
    ``on_done("steam" | "repack" | None)``.

  * ``MewgenicsLaunchCommandOverlay`` - shows the generated ``-modpaths`` launch
    string in a read-only box with a Copy-to-clipboard button (auto-copied on
    open) and, when a script was written, its path.

Both are child overlays via gui_qt/overlay_base.py.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, close_button, _c


class MewgenicsDeployChoiceOverlay(OverlayBase):
    """Choose Steam launch command or repack the gpak. ``on_done`` receives
    ``"steam"``, ``"repack"``, or ``None`` (cancel / Esc)."""

    CARD_W = 480
    CARD_H = 260
    MIN_H = 220

    def __init__(self, host: QWidget, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("MewgenicsChoiceCard")

        title = QLabel(self.tr("Mewgenics - Deploy method"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        v.addWidget(self._choice(
            self.tr("Steam launch command  (Safer / Recommended)"),
            self.tr("Generates a launch script for Steam. Set it once in "
                    "Launch Options (no repack)."),
            "steam"))
        v.addWidget(self._choice(
            self.tr("Repack gpak  (No command needed / not recommended)"),
            self.tr("Unpack resources.gpak, merge mods, repack."),
            "repack"))
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        v.addLayout(bar)

        self._present()

    def _choice(self, label: str, desc: str, result: str) -> QWidget:
        p = active_palette()
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        btn = QPushButton(label)
        btn.setObjectName("FormButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet("text-align:left; padding:8px 12px;")
        btn.clicked.connect(lambda: self._finish(result))
        col.addWidget(btn)
        sub = QLabel(desc)
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        sub.setWordWrap(True)
        col.addWidget(sub)
        return box


class MewgenicsLaunchCommandOverlay(OverlayBase):
    """Show the ``-modpaths`` launch string with a Copy button. The command is
    also copied to the clipboard automatically on open."""

    CARD_W = 560
    CARD_H = 340
    MIN_W = 380
    MIN_H = 240
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, launch_string: str, modpaths_file=None):
        super().__init__(host)
        self._launch_string = launch_string
        p = active_palette()
        _card, v = self._make_card("MewgenicsLaunchCard")

        title = QLabel(self.tr("Mewgenics - Steam / Lutris launch command"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        sub = QLabel(self.tr(
            "Paste this into Steam Launch Options (Properties → General):"))
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

        if modpaths_file is not None:
            path_lbl = QLabel(self.tr(
                "Script written to:\n{0}\n\nUpdate this whenever you change "
                "your mod list.").format(modpaths_file))
            path_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            path_lbl.setWordWrap(True)
            v.addWidget(path_lbl)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = close_button(self.tr("Close"), pal=p)
        close.clicked.connect(lambda: self._finish(None))
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
