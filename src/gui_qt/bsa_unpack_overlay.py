"""Borderless archive-unpack picker overlay (Qt).

Qt counterpart of the Tk ``gui/bsa_unpack_overlay.py``. Lists each plugin in the
selected mod together with every sibling archive (``Foo.bsa``, ``Foo - Main.ba2``,
``Foo - Textures.ba2``) that auto-loads with it; clicking a row's Unpack extracts
that whole group in one go. Archives with no matching plugin get a trailing
"(no matching plugin)" group.

Grouping/size/count come from ``Utils.bsa_pack_ops.collect_unpack_groups`` (shared
with Tk). ``on_done(list[Path])`` is called with the chosen group's archives; the
overlay closes itself. Dimmed child overlay via gui_qt/overlay_base.py with a
scroll body (like ``download_locations_overlay.py``).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea,
)

import Utils.bsa_pack_ops as ops
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, close_button, _c


class BsaUnpackOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 520
    MIN_W = 400
    MIN_H = 300

    def __init__(self, host: QWidget, *, mod_name: str, mod_dir: Path,
                 plugin_exts, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        groups = ops.collect_unpack_groups(mod_dir, plugin_exts)
        all_archives = [a for g in groups for a in g.archives]
        kind_label = ops.unpack_kind_label(all_archives) if all_archives else "Archive"

        _card, v = self._make_card("UnpackCard", margins=(18, 16, 18, 14))

        title_lbl = QLabel(self.tr("Unpack {0} - {1}").format(kind_label, mod_name))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        # -- scrollable group list -----------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 6, 0)
        bl.setSpacing(6)
        if not groups:
            empty = QLabel(self.tr("No archive files in this mod folder."))
            empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
            bl.addWidget(empty)
        else:
            for g in groups:
                bl.addWidget(self._group_row(g, p))
        bl.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        hint = QLabel(
            self.tr("Unpacking extracts every archive under the selected plugin into "
            "this mod's folder, deletes those archives, removes the plugin if "
            "it was a generated stub, and re-enables the unpacked files in the "
            "Mod Files tab."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = close_button(self.tr("Close"), pal=p)
        close.clicked.connect(lambda: self._finish(None))
        bar.addWidget(close)
        v.addLayout(bar)

        self._present()

    def _group_row(self, g: ops.UnpackGroup, p) -> QWidget:
        row = QFrame()
        row.setObjectName("UnpackRow")
        row.setStyleSheet(
            f"#UnpackRow {{ background:{_c(p,'BG_DEEP')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(10)

        info = QVBoxLayout()
        info.setSpacing(2)
        name = QLabel(g.label)
        name_col = _c(p, "TEXT_DIM") if g.is_orphan else _c(p, "TEXT_MAIN")
        name.setStyleSheet(f"color:{name_col}; font-weight:600;")
        name.setWordWrap(True)
        info.addWidget(name)
        for a in g.archives:
            sub = QLabel(self.tr("  • {0}").format(a.name))
            sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            sub.setWordWrap(True)
            info.addWidget(sub)
        size_mb = g.total_bytes / (1024 * 1024)
        if g.total_files >= 0:
            totals = f"{g.total_files} file(s) - {size_mb:.1f} MiB"
        else:
            totals = f"unreadable - {size_mb:.1f} MiB"
        totals_lbl = QLabel(totals)
        totals_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        info.addWidget(totals_lbl)
        h.addLayout(info, 1)

        btn = QPushButton(self.tr("Unpack"))
        btn.setObjectName("PrimaryButton")
        btn.setCursor(Qt.PointingHandCursor)
        archives = list(g.archives)
        btn.clicked.connect(lambda: self._finish(archives))
        h.addWidget(btn, 0, Qt.AlignTop)
        return row

    @classmethod
    def show_over(cls, host, *, mod_name, mod_dir, plugin_exts, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mod_name=mod_name, mod_dir=mod_dir,
                   plugin_exts=plugin_exts, on_done=on_done)
