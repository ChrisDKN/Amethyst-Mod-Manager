"""In-window overlay shown after a Change Version install lands as a NEW mod
(different folder name): offer to remove the previous version. Qt equivalent of
the Tk ``_prompt_remove_previous_version`` CTkAlert - a dimmed child overlay
(see gui_qt/overlay_base.py).

`on_done(result)` is called with:
    "remove"  - delete the old mod; the new one inherits its modlist slot + state
    "keep"    - leave both mods
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class RemovePreviousOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 260
    MIN_H = 200
    ESC_RESULT = "keep"

    def __init__(self, host: QWidget, old_name: str, new_name: str, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("RemovePrevCard")

        title = QLabel(self.tr("Remove previous version?"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        body = QLabel(
            self.tr("'{0}' was installed as a new mod (different folder name) because it did not replace '{1}'.\n\nRemove the previous version '{2}'? The new mod will take its position in the modlist.\n\nChoose Keep if this is an optional/alternative variant rather than a replacement.").format(new_name, old_name, old_name))
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        keep = QPushButton(self.tr("Keep"))
        keep.setObjectName("FormButton")        # neutral, like other buttons
        keep.setCursor(Qt.PointingHandCursor)
        keep.clicked.connect(lambda: self._finish("keep"))
        bar.addWidget(keep)
        remove = QPushButton(self.tr("Remove"))
        remove.setObjectName("PrimaryButton")   # accent primary action
        remove.setCursor(Qt.PointingHandCursor)
        remove.clicked.connect(lambda: self._finish("remove"))
        bar.addWidget(remove)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, old_name, new_name, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, old_name, new_name, on_done)
