"""Thunderstore dependency confirmation overlay.

Shown before a ror2mm:// install that pulls in dependencies, so the user can
see what else is about to land and opt out of any of it. The requested mod is
always installed and is listed as a fixed row; each dependency gets its own
checkbox (all ticked by default - that is the working configuration the mod
author pinned).

``on_done`` receives the list of ``ResolvedPackage`` objects to install
(dependencies-first order preserved, requested mod last), or None if the user
cancelled the whole install.

Version conflicts are surfaced here rather than silently resolved: when the
graph pinned one package at several versions the row says so, because a user
deselecting a dependency that something else needs should know what they are
doing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QScrollArea, QFrame,
)

from Utils.downloads.core import fmt_size
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ThunderstoreDepsOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 520
    MIN_W = 400
    MIN_H = 300
    ESC_RESULT = None          # Esc / backdrop click = cancel the install

    def __init__(self, host: QWidget, *, root, dependencies,
                 conflicts=None, on_done=None):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        self._root = root
        self._deps = list(dependencies or [])
        self._boxes: dict = {}          # package_id → QCheckBox

        _card, v = self._make_card("TsDepsCard")

        title = QLabel(self.tr("Install dependencies?"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        count = len(self._deps)
        body = QLabel(
            self.tr("{0} needs {1} other mod(s). They will be installed first.")
            .format(getattr(root, "full_name", ""), count))
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 6, 0)
        il.setSpacing(6)

        # The requested mod - always installed, so no checkbox.
        il.addWidget(self._row(p, getattr(root, "full_name", ""),
                               getattr(root, "file_size", 0),
                               self.tr("requested"), fixed=True))

        conflict_map = {c[0]: c for c in (conflicts or [])}
        for pkg in self._deps:
            note = ""
            conflict = conflict_map.get(pkg.package_id)
            if conflict:
                _pid, versions, chosen = conflict
                note = self.tr("required at {0} - installing {1}").format(
                    ", ".join(versions), chosen)
            row, box = self._dep_row(p, pkg, note)
            self._boxes[pkg.package_id] = box
            il.addWidget(row)

        il.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        v.addWidget(self._total_lbl)
        self._update_total()

        bar = QHBoxLayout()
        none_btn = QPushButton(self.tr("Skip all"))
        none_btn.setObjectName("FormButton")
        none_btn.setCursor(Qt.PointingHandCursor)
        none_btn.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(none_btn)
        all_btn = QPushButton(self.tr("Select all"))
        all_btn.setObjectName("FormButton")
        all_btn.setCursor(Qt.PointingHandCursor)
        all_btn.clicked.connect(lambda: self._set_all(True))
        bar.addWidget(all_btn)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        install = QPushButton(self.tr("Install"))
        install.setObjectName("PrimaryButton")
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self._accept)
        bar.addWidget(install)
        v.addLayout(bar)

        self._present()

    # -- rows ---------------------------------------------------------------
    def _row(self, p, text: str, size: int, note: str, fixed: bool = False):
        frame = QFrame()
        frame.setObjectName("TsDepRow")
        frame.setStyleSheet(
            f"#TsDepRow {{ background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        h = QHBoxLayout(frame)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(8)
        name = QLabel(text)
        name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        h.addWidget(name, 1)
        if note:
            tag = QLabel(note)
            tag.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            h.addWidget(tag)
        if size:
            sz = QLabel(fmt_size(size))
            sz.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            h.addWidget(sz)
        return frame

    def _dep_row(self, p, pkg, note: str):
        frame = QFrame()
        frame.setObjectName("TsDepRow")
        frame.setStyleSheet(
            f"#TsDepRow {{ background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        h = QHBoxLayout(frame)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(8)
        box = QCheckBox(pkg.full_name)
        box.setChecked(True)
        box.setCursor(Qt.PointingHandCursor)
        box.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        box.toggled.connect(self._update_total)
        h.addWidget(box, 1)
        if note:
            tag = QLabel(note)
            tag.setStyleSheet(f"color:{_c(p,'TEXT_WARN')}; font-size:11px;")
            tag.setToolTip(note)
            h.addWidget(tag)
        if pkg.file_size:
            sz = QLabel(fmt_size(pkg.file_size))
            sz.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
            h.addWidget(sz)
        return frame, box

    # -- behaviour ----------------------------------------------------------
    def _set_all(self, checked: bool):
        for box in self._boxes.values():
            box.setChecked(checked)
        self._update_total()

    def _selected(self) -> list:
        return [pkg for pkg in self._deps
                if self._boxes[pkg.package_id].isChecked()]

    def _update_total(self, *_a):
        chosen = self._selected()
        total = sum(p.file_size or 0 for p in chosen)
        total += getattr(self._root, "file_size", 0) or 0
        self._total_lbl.setText(
            self.tr("{0} of {1} dependencies selected - {2} to download")
            .format(len(chosen), len(self._deps), fmt_size(total)))

    def _accept(self):
        # Dependencies keep their resolved (deepest-first) order; the requested
        # mod installs last so its requirements are already in place.
        self._finish(self._selected() + [self._root])

    @classmethod
    def show_over(cls, host, *, root, dependencies, conflicts=None, on_done=None):
        top = host.window() if host is not None else None
        return cls(top or host, root=root, dependencies=dependencies,
                   conflicts=conflicts, on_done=on_done)
