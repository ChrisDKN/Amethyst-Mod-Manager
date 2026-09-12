from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class WabbajackIssuesOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 470
    ESC_RESULT = False

    def __init__(self, host, name, issues, on_done):
        super().__init__(host.window(), on_done=on_done)
        palette = active_palette()
        _, layout = self._make_card("WabbajackIssuesCard")

        title = QLabel(self.tr("Confirmed broken modlist"))
        title.setWordWrap(True)
        title.setStyleSheet(f"color:{_c(palette, 'TEXT_WARN')}; font-size:16px; font-weight:600;")
        layout.addWidget(title)

        scroll = QScrollArea(self._card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget(scroll)
        details = QVBoxLayout(content)
        details.setContentsMargins(0, 0, 8, 0)
        details.setSpacing(12)

        def add_text(text):
            label = QLabel(text, content)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')};")
            details.addWidget(label)

        add_text(self.tr("{0} has a confirmed installation issue. Installation may fail unless it has been resolved.").format(name))
        for issue in issues:
            add_text(self.tr("Confirmed on {0}").format(issue.confirmed_on))
            add_text(self.tr(issue.reason))
            add_text(self.tr(issue.recovery))
            for label, url in issue.links:
                link = QPushButton(self.tr(label), content)
                link.setObjectName("FormButton")
                link.setCursor(Qt.PointingHandCursor)
                link.clicked.connect(lambda _checked=False, url=url: self._open_link(url))
                details.addWidget(link, 0, Qt.AlignLeft)
        details.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.clicked.connect(lambda: self._finish(False))
        buttons.addWidget(cancel)
        proceed = QPushButton(self.tr("Continue anyway"), self._card)
        proceed.setObjectName("PrimaryButton")
        proceed.clicked.connect(lambda: self._finish(True))
        buttons.addWidget(proceed)
        layout.addLayout(buttons)
        self._present()
        cancel.setFocus(Qt.OtherFocusReason)

    @staticmethod
    def _open_link(url):
        from Utils.environment.xdg import open_url
        open_url(url)

    def dismiss(self):
        self._finish(False)
