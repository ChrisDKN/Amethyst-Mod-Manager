from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class WizardSettingsOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 460
    MIN_W = 340
    MIN_H = 260
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, entries, on_reset):
        super().__init__(host)
        self._on_reset = on_reset
        self._rows: dict[tuple[str, str], QWidget] = {}
        p = active_palette()

        _card, outer = self._make_card(
            "_WizardSettingsCard", margins=(16, 14, 16, 14))

        title = QLabel(self.tr("Wizard Settings"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        outer.addWidget(title)

        intro = QLabel(self.tr(
            "These wizard tools skip their Proton settings step. Reset a tool "
            "to show the step again; its saved values are kept."))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget {"
            " background: transparent; }")
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 4, 0, 4)
        self._list.setSpacing(5)
        self._empty = QLabel(self.tr("No wizard tools are using saved settings."))
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        self._list.addWidget(self._empty, 1)
        self._list.addStretch(1)
        scroll.setWidget(self._list_host)
        outer.addWidget(scroll, 1)

        for entry in entries:
            self._add_entry(p, entry)
        self._sync_empty_state()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda: self._finish(None))
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self._present()
        close.setFocus()

    def _add_entry(self, p, entry):
        game_name = str(entry["game_name"])
        wizard_id = str(entry["wizard_id"])
        key = (game_name, wizard_id)

        row = QFrame()
        row.setObjectName("WizardSettingRow")
        row.setStyleSheet(
            f"#WizardSettingRow {{ background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        labels = QWidget()
        label_layout = QVBoxLayout(labels)
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_layout.setSpacing(1)
        tool = QLabel(str(entry["label"]))
        tool.setWordWrap(True)
        tool.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        label_layout.addWidget(tool)
        game = QLabel(game_name)
        game.setWordWrap(True)
        game.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        label_layout.addWidget(game)
        layout.addWidget(labels, 1)

        reset = QPushButton(self.tr("Reset"))
        reset.setObjectName("FormButton")
        reset.setCursor(Qt.PointingHandCursor)
        reset.setToolTip(self.tr(
            "Show this wizard's Proton settings step the next time it runs."))
        reset.clicked.connect(
            lambda _=False, k=key: self._reset_entry(k))
        layout.addWidget(reset)

        self._rows[key] = row
        self._list.insertWidget(self._list.count() - 1, row)

    def _reset_entry(self, key: tuple[str, str]):
        if not self._on_reset(*key):
            return
        row = self._rows.pop(key, None)
        if row is not None:
            self._list.removeWidget(row)
            row.deleteLater()
        self._sync_empty_state()

    def _sync_empty_state(self):
        self._empty.setVisible(not self._rows)

    @classmethod
    def show_over(cls, host, entries, on_reset):
        top = host.window() if host is not None else None
        return cls(top or host, entries, on_reset)
