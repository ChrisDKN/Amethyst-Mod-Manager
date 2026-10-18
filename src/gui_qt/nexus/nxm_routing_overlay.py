from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from Nexus.nxm_routing import AMETHYST_HANDLER, discover_handlers, load_routes, read_handler, save_route
from gui_qt.dialogs.base import OverlayBase
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.appearance.theme import active_palette, _c


class NxmRoutingOverlay(OverlayBase):
    CARD_W = 580
    CARD_H = 380
    MIN_H = 300
    _file_picked = Signal(object)

    def __init__(self, host, on_done=None, *, domain="", error=""):
        super().__init__(host, on_done=on_done)
        self._incoming = bool(domain)
        _, layout = self._make_card("NxmRoutingCard")
        title = QLabel(self.tr("Nexus link forwarding"))
        title.setStyleSheet(f"color:{_c(active_palette(), 'TEXT_MAIN')}; font-size:16px; font-weight:600;")
        layout.addWidget(title)
        intro = QLabel(self.tr(
            "Choose which mod manager receives this game's Nexus links. "
            "The choice is remembered. Amethyst uses the matching configured game, "
            "or the current game for links from other domains."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(QLabel(self.tr("Nexus game domain")))
        self._domain = QComboBox()
        self._domain.setEditable(True)
        self._domain.lineEdit().setPlaceholderText("helldivers2")
        self._domain.setEnabled(not self._incoming)
        layout.addWidget(self._domain)
        layout.addWidget(QLabel(self.tr("Open this game's Nexus links with")))
        self._handler = QComboBox()
        self._handler.addItem(self.tr("Choose a mod manager…"), "")
        self._handler.addItem("Amethyst Mod Manager", AMETHYST_HANDLER)
        for handler in discover_handlers():
            self._handler.addItem(f"{handler.name} ({handler.path.name})", str(handler.path))
        layout.addWidget(self._handler)
        browse = QPushButton(self.tr("Browse for an NXM launcher…"))
        browse.setObjectName("FormButton")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)
        self._status = QLabel(error or self.tr(
            "If your manager is missing, register it for Nexus links or select its .desktop NXM launcher."))
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        layout.addStretch(1)
        bar = QHBoxLayout()
        self._remove = QPushButton(self.tr("Remove saved choice"))
        self._remove.setObjectName("FormButton")
        self._remove.clicked.connect(self._forget)
        bar.addWidget(self._remove)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel") if self._incoming else self.tr("Close"))
        cancel.setObjectName("FormButton")
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save and open") if self._incoming else self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        bar.addWidget(save)
        layout.addLayout(bar)
        self._file_picked.connect(self._picked)
        try:
            self._routes = load_routes()
        except (OSError, ValueError) as exc:
            self._routes = {}
            self._status.setText(str(exc))
        self._domain.addItems(sorted(self._routes))
        self._domain.setCurrentText(domain)
        self._domain.currentTextChanged.connect(self._select_domain)
        self._select_domain(domain)
        self._present()

    def _select_domain(self, domain):
        desktop = self._routes.get(domain.strip().lower(), "")
        if desktop and self._handler.findData(desktop) < 0:
            self._handler.addItem(desktop, desktop)
        self._handler.setCurrentIndex(max(0, self._handler.findData(desktop)))
        self._remove.setEnabled(bool(desktop))

    def _browse(self):
        from Utils.ui.portal import pick_file
        pick_file(self.tr("Select the mod manager's NXM launcher"),
                  lambda path: safe_emit(self._file_picked, path),
                  filters=[(self.tr("Desktop launchers"), ["*.desktop"])])

    def _picked(self, path):
        if not path or self._done:
            return
        try:
            handler = read_handler(path)
        except Exception as exc:
            self._status.setText(str(exc))
            return
        desktop = str(handler.path)
        if self._handler.findData(desktop) < 0:
            self._handler.addItem(handler.name, desktop)
        self._handler.setCurrentIndex(self._handler.findData(desktop))

    def _save(self):
        desktop = self._handler.currentData()
        if not desktop:
            self._status.setText(self.tr("Choose a mod manager first."))
            return
        domain = self._domain.currentText().strip().lower()
        try:
            save_route(domain, desktop)
        except Exception as exc:
            self._status.setText(str(exc))
            return
        if self._incoming:
            self._finish(desktop)
        else:
            self._routes[domain] = desktop
            if self._domain.findText(domain) < 0:
                self._domain.addItem(domain)
            self._remove.setEnabled(True)
            self._status.setText(self.tr("Saved forwarding choice for {0}.").format(domain))

    def _forget(self):
        domain = self._domain.currentText().strip().lower()
        try:
            save_route(domain, None)
        except Exception as exc:
            self._status.setText(str(exc))
            return
        self._routes.pop(domain, None)
        self._select_domain(domain)
        self._status.setText(self.tr("Removed forwarding choice for {0}.").format(domain))
