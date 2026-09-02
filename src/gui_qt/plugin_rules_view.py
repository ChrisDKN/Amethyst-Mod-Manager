"""LOOT Plugin Rules view - configure per-plugin before/after rules in userlist.yaml.

Qt port of the Tk gui/loot_plugin_rules_overlay.py LootPluginRulesOverlay (1:1).
Opens as a modlist-panel-scoped tab so the plugins panel stays visible - the
selected plugin comes from the plugins panel (the app forwards selection changes
via set_selected_plugin, mirroring Tk's _on_plugin_row_selected_cb).

Left pane:  all plugins (except the selected one), filterable, draggable onto
            the right pane.
Right pane: rules for the selected plugin - each dropped plugin becomes a rule
            row with a before/after toggle and a remove button.

Tk semantics kept exactly: drop defaults to "after" and skips self/duplicates;
every drop / toggle / remove saves immediately (merge-preserving the plugin's
existing userlist entry, dropping it when nothing meaningful remains).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QMimeData, Signal
from PySide6.QtGui import QDrag, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, close_button
from Utils.plugins.userlist import (
    parse_userlist, write_userlist, save_plugin_rules_merged,
)

_MIME = "application/x-amethyst-plugin-name"


class _DragPluginList(QListWidget):
    """Plugin list that drags the plugin name with an accent-coloured ghost
    (Tk parity: the drag ghost Toplevel label)."""

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        name = item.text()
        mime = QMimeData()
        mime.setText(name)
        mime.setData(_MIME, name.encode("utf-8"))

        p = active_palette()
        accent = QColor(_c(p, "ACCENT"))
        f = QFont()
        f.setPixelSize(12)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(name) + 16
        h = fm.height() + 6
        pm = QPixmap(w, h)
        pm.fill(accent)
        painter = QPainter(pm)
        painter.setFont(f)
        painter.setPen(QColor(_c(p, "TEXT_ON_ACCENT")))
        painter.drawText(pm.rect(), Qt.AlignCenter, name)
        painter.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pm)
        drag.exec(Qt.CopyAction)


class _RulesDropArea(QScrollArea):
    """Drop target for plugin names; highlights its border while hovered
    (Tk parity: _update_drop_highlight)."""

    dropped = Signal(str)

    def __init__(self, border: str, accent: str, bg: str):
        super().__init__()
        self._border = border
        self._accent = accent
        self._bg = bg
        self.setAcceptDrops(True)
        self._set_border(border)

    def _set_border(self, color: str):
        self.setStyleSheet(
            f"QScrollArea {{ background:{self._bg}; border:1px solid {color}; }}")

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_MIME):
            self._set_border(self._accent)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(_MIME):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_border(self._border)

    def dropEvent(self, event):
        self._set_border(self._border)
        if event.mimeData().hasFormat(_MIME):
            name = bytes(event.mimeData().data(_MIME)).decode("utf-8")
            if name:
                self.dropped.emit(name)
            event.acceptProposedAction()


class PluginRulesView(QWidget):
    """Modlist-scoped tab body for managing per-plugin LOOT before/after rules."""

    def __init__(
        self,
        plugin_names: list[str],
        userlist_path: Path,
        selected_plugin: str = "",
        on_close: Optional[Callable[[], None]] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self._plugin_names = list(plugin_names)
        self._ul_path = userlist_path
        self._on_close = on_close or (lambda: None)
        self._on_saved = on_saved or (lambda: None)

        self._selected_plugin: str = selected_plugin
        # rules: list of [rel, target] - mutable so toggle can update in-place
        self._rules: list[list[str]] = []

        self.setObjectName("PluginRulesView")
        self._build()

        if self._selected_plugin:
            self._load_rules_for(self._selected_plugin)
            self._repaint_rules()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        p = active_palette()
        self._c_bg_deep = _c(p, "BG_DEEP")
        self._c_bg_header = _c(p, "BG_HEADER")
        self._c_bg_panel = _c(p, "BG_PANEL")
        self._c_bg_hover = _c(p, "BG_HOVER")
        self._c_bg_row = _c(p, "BG_ROW")
        self._c_bg_row_alt = _c(p, "BG_ROW_ALT")
        self._c_border = _c(p, "BORDER")
        self._c_text = _c(p, "TEXT_MAIN")
        self._c_text_dim = _c(p, "TEXT_DIM")
        self._c_accent = _c(p, "ACCENT")
        self._c_danger = _c(p, "BTN_DANGER")

        self.setStyleSheet(f"""
            #PluginRulesView {{ background:{self._c_bg_deep}; }}
            QLabel {{ color:{self._c_text}; }}
            QLineEdit {{ background:{self._c_bg_panel}; color:{self._c_text};
                         border:1px solid {self._c_border}; border-radius:4px;
                         padding:4px; }}
            QListWidget {{ background:{self._c_bg_panel}; color:{self._c_text};
                           border:1px solid {self._c_border}; }}
            QListWidget::item:selected {{ background:{self._c_accent};
                                          color:{_c(p, 'TEXT_ON_ACCENT')}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- toolbar ----
        toolbar = QWidget()
        toolbar.setFixedHeight(42)
        toolbar.setStyleSheet(f"background:{self._c_bg_header};")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(12, 0, 12, 0)
        title = QLabel(self.tr("LOOT Plugin Rules - Select a plugin on the plugins panel"))
        title.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        tb.addWidget(title, 1)
        close_btn = close_button(pal=p)
        close_btn.clicked.connect(self._do_close)
        tb.addWidget(close_btn)
        root.addWidget(toolbar)

        # ---- body: Plugins (left) | divider | Rules (right), 1:2 ----
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(8)

        bl.addWidget(self._build_plugins_panel(), 1)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background:{self._c_border}; border:none;")
        bl.addWidget(divider)

        bl.addWidget(self._build_rules_panel(), 2)
        root.addWidget(body, 1)

    def _build_plugins_panel(self) -> QWidget:
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QLabel(self.tr("Plugins  -  drag onto rules pane"))
        hdr.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(hdr)

        self._plugins_list = _DragPluginList()
        self._plugins_list.setDragEnabled(True)
        v.addWidget(self._plugins_list, 1)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self._search_edit = QLineEdit()
        self._search_edit.textChanged.connect(self._on_search_change)
        search_row.addWidget(self._search_edit, 1)
        filter_lbl = QLabel(self.tr("Filter"))
        filter_lbl.setStyleSheet(f"color:{self._c_text_dim};")
        search_row.addWidget(filter_lbl)
        v.addLayout(search_row)

        self._repaint_plugins()
        return left

    def _build_rules_panel(self) -> QWidget:
        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        plugin_label = self._selected_plugin or self.tr("- no plugin selected -")
        self._rules_title = QLabel(self.tr("Rules for: {0}").format(plugin_label))
        self._rules_title.setStyleSheet(
            f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(self._rules_title)

        self._rules_scroll = _RulesDropArea(
            self._c_border, self._c_accent, self._c_bg_panel)
        self._rules_scroll.setWidgetResizable(True)
        self._rules_scroll.dropped.connect(self._drop_plugin)
        self._rules_container = QWidget()
        self._rules_container.setStyleSheet(f"background:{self._c_bg_panel};")
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(0)
        self._rules_layout.addStretch(1)
        self._rules_scroll.setWidget(self._rules_container)
        v.addWidget(self._rules_scroll, 1)

        self._repaint_rules()
        return right

    # ------------------------------------------------------------------
    # Selection (forwarded from the plugins panel)
    # ------------------------------------------------------------------

    def set_selected_plugin(self, name: str):
        """Called by the app when the user clicks a plugin row."""
        if name == self._selected_plugin:
            return
        self._selected_plugin = name
        self._rules_title.setText(self.tr("Rules for: {0}").format(name))
        self._load_rules_for(name)
        self._repaint_plugins(self._search_edit.text())
        self._repaint_rules()

    # ------------------------------------------------------------------
    # Plugins list
    # ------------------------------------------------------------------

    def _repaint_plugins(self, filter_text: str = ""):
        self._plugins_list.clear()
        ft = filter_text.lower()
        for n in self._plugin_names:
            if ft in n.lower() and n.lower() != self._selected_plugin.lower():
                self._plugins_list.addItem(n)

    def _on_search_change(self, text: str):
        self._repaint_plugins(text)

    # ------------------------------------------------------------------
    # Drop → add rule
    # ------------------------------------------------------------------

    def _drop_plugin(self, name: str):
        if not self._selected_plugin:
            return
        if name.lower() == self._selected_plugin.lower():
            return
        # Default to "after"; skip if already present (any rel)
        for _rel, target in self._rules:
            if target.lower() == name.lower():
                return
        self._rules.append(["after", name])
        self._repaint_rules()
        self._save_current()

    # ------------------------------------------------------------------
    # Rules pane
    # ------------------------------------------------------------------

    def _load_rules_for(self, plugin_name: str):
        self._rules = []
        if not self._ul_path.is_file():
            return
        data = parse_userlist(self._ul_path)
        for entry in data.get("plugins", []):
            if entry.get("name", "").lower() == plugin_name.lower():
                for t in entry.get("after", []):
                    self._rules.append(["after", t])
                for t in entry.get("before", []):
                    self._rules.append(["before", t])
                break

    def _repaint_rules(self):
        while self._rules_layout.count() > 1:
            item = self._rules_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._selected_plugin:
            empty = QLabel(self.tr("No plugin selected.\n"
                           "Right-click a plugin and choose 'Plugin Rules'."))
            empty.setStyleSheet(f"color:{self._c_text_dim}; padding:12px;")
            self._rules_layout.insertWidget(0, empty)
            return

        if not self._rules:
            empty = QLabel(self.tr("No rules yet.\n"
                           "Drag a plugin from the left pane to add a rule."))
            empty.setStyleSheet(f"color:{self._c_text_dim}; padding:12px;")
            self._rules_layout.insertWidget(0, empty)
            return

        for i, rule in enumerate(self._rules):
            rel, target = rule
            row_bg = self._c_bg_row if i % 2 == 0 else self._c_bg_row_alt
            row = QWidget()
            row.setStyleSheet(f"background:{row_bg};")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 4, 6, 4)
            h.setSpacing(6)

            rel_btn = QPushButton(rel)
            rel_btn.setFixedSize(80, 28)
            rel_btn.setCursor(Qt.PointingHandCursor)
            rel_btn.setStyleSheet(
                f"QPushButton {{ background:{self._c_bg_header};"
                f" color:{self._c_accent}; border:none; border-radius:4px; }}"
                f"QPushButton:hover {{ background:{self._c_bg_hover}; }}")
            rel_btn.clicked.connect(lambda _=False, r=rule: self._toggle_rule(r))
            h.addWidget(rel_btn)

            lbl = QLabel(target)
            lbl.setStyleSheet(f"color:{self._c_text};")
            h.addWidget(lbl, 1)

            rm = QPushButton("✕")
            rm.setFixedSize(26, 22)
            rm.setCursor(Qt.PointingHandCursor)
            rm.setStyleSheet(
                f"QPushButton {{ background:{self._c_bg_deep};"
                f" color:{self._c_text_dim}; border:none; border-radius:4px; }}"
                f"QPushButton:hover {{ background:{self._c_danger}; }}")
            rm.clicked.connect(lambda _=False, idx=i: self._remove_rule(idx))
            h.addWidget(rm)

            self._rules_layout.insertWidget(i, row)

    def _toggle_rule(self, rule: list[str]):
        rule[0] = "before" if rule[0] == "after" else "after"
        self._repaint_rules()
        self._save_current()

    def _remove_rule(self, idx: int):
        if 0 <= idx < len(self._rules):
            self._rules.pop(idx)
            self._repaint_rules()
            self._save_current()

    # ------------------------------------------------------------------
    # Save / Close
    # ------------------------------------------------------------------

    def _save_current(self):
        if not self._selected_plugin:
            return
        data = (parse_userlist(self._ul_path) if self._ul_path.is_file()
                else {"plugins": [], "groups": []})
        save_plugin_rules_merged(data, self._selected_plugin, self._rules)
        self._ul_path.parent.mkdir(parents=True, exist_ok=True)
        write_userlist(self._ul_path, data)
        self._on_saved()

    def _do_close(self):
        self._on_close()
