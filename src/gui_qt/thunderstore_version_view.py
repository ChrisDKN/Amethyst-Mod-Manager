"""Thunderstore Change Version panel - pick any published version of a mod.

A plugins-panel-scoped tab (like the Nexus Change Version overlay), listing
every version of the installed package newest-first with its release date,
download count and size, plus an Install button per row.

Far simpler than the Nexus equivalent: Thunderstore has no per-file model
(one package version = one zip), no premium/free split and no expiring
tokens, so there is no file chooser, no manual-download watch and no login.
The whole version list arrives in a single unauthenticated request.

The install itself is delegated back to the host via *install_fn*, which
reuses the normal ror2mm pipeline - so switching version also resolves any
dependencies that version introduced.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame,
)

from Utils.downloads.core import fmt_size
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, close_button


class ThunderstoreVersionView(QWidget):
    """Version picker for one installed Thunderstore mod."""

    _versions_ready = Signal(object)

    def __init__(self, mod_name: str, meta, install_fn, on_close,
                 log_fn=None):
        super().__init__()
        self._mod_name = mod_name
        self._meta = meta
        self._install_fn = install_fn or (lambda ns, name, version: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)

        p = active_palette()
        self.setStyleSheet(f"background:{_c(p,'BG_PANEL')};")
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(self.tr("Change Version - {0}").format(meta.package_id))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        head.addWidget(title)
        head.addStretch(1)
        close = close_button(self.tr("Close"))
        close.clicked.connect(lambda: self._on_close())
        head.addWidget(close)
        root.addLayout(head)

        self._status = QLabel(self.tr("Loading versions…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        root.addWidget(self._status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._body_l = QVBoxLayout(self._body)
        self._body_l.setContentsMargins(0, 0, 6, 0)
        self._body_l.setSpacing(6)
        self._body_l.addStretch(1)
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

        self._versions_ready.connect(self._populate)
        self._fetch()

    # -- data ---------------------------------------------------------------
    def _fetch(self):
        """Load the version list on a daemon thread (never a QThread)."""
        meta = self._meta

        def _work():
            from Thunderstore.thunderstore_api import fetch_versions
            safe_emit(self._versions_ready,
                      fetch_versions(meta.namespace, meta.name))

        threading.Thread(target=_work, daemon=True,
                         name="ts-versions").start()

    def _populate(self, data):
        p = active_palette()
        if not isinstance(data, list) or not data:
            self._status.setText(
                self.tr("Could not load versions for {0}.")
                .format(self._meta.package_id))
            return

        # Newest first. The API returns them unordered, so sort on the parsed
        # version rather than the string (1001 must beat 999).
        from Nexus.nexus_update_checker import _parse_version
        rows = sorted(data,
                      key=lambda v: (_parse_version(v.get("version_number", ""))
                                     or ()),
                      reverse=True)
        installed = (self._meta.version or "").strip()
        self._status.setText(
            self.tr("{0} version(s) - installed: {1}")
            .format(len(rows), installed or self.tr("unknown")))

        for v in rows:
            self._body_l.insertWidget(self._body_l.count() - 1,
                                      self._row(p, v, installed))

    def _row(self, p, v: dict, installed: str) -> QWidget:
        ver = str(v.get("version_number") or "")
        is_current = ver == installed

        frame = QFrame()
        frame.setObjectName("TsVerRow")
        frame.setStyleSheet(
            f"#TsVerRow {{ background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p, 'ACCENT' if is_current else 'BORDER')};"
            f" border-radius:4px; }}")
        h = QHBoxLayout(frame)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(10)

        name = QLabel(ver)
        name.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-size:13px; font-weight:600;")
        h.addWidget(name)

        if is_current:
            tag = QLabel(self.tr("installed"))
            tag.setStyleSheet(f"color:{_c(p,'TEXT_OK')}; font-size:11px;")
            h.addWidget(tag)

        date = str(v.get("datetime_created") or "")[:10]
        if date:
            d = QLabel(date)
            d.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            h.addWidget(d)

        h.addStretch(1)

        dl = v.get("download_count")
        if dl:
            c = QLabel(self.tr("{0} downloads").format(f"{int(dl):,}"))
            c.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            h.addWidget(c)

        btn = QPushButton(self.tr("Reinstall") if is_current
                          else self.tr("Install"))
        btn.setObjectName("FormButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, x=ver: self._install(x))
        h.addWidget(btn)
        return frame

    def _install(self, version: str):
        if not version:
            return
        self._log(f"[thunderstore] switching {self._meta.package_id} "
                  f"{self._meta.version or '?'} → {version}")
        self._install_fn(self._meta.namespace, self._meta.name, version)
