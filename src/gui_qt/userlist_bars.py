"""Inline userlist edit bars for the plugins panel.

Qt port of the two hidden inline panels at the bottom of the Tk plugins tab
(gui/plugin_panel.py rows 5/6 + gui/plugin_panel_userlist_cycle.py handlers):

- UserlistBar - 'Add to userlist…': After/Before entry fields (|-separated),
  prefilled from the plugin's current load-order neighbours by the caller.
- GroupBar - 'Add to group…': group combo (from userlist.yaml, 'default'
  first), single or multi plugin assignment.

Both bars are hidden by default; the plugins context menu opens them. Save
parses + mutates + writes userlist.yaml directly (mirroring the Tk mixin) and
reports back via on_saved(message) so the app can toast + refresh flags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QFrame,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.userlist import (
    DEFAULT_GROUP, parse_userlist, write_userlist,
    set_plugin_rules, set_plugin_group,
)


def _parse_pipe_list(val: str) -> list[str]:
    return [p.strip() for p in val.split("|") if p.strip()]


class _BarBase(QWidget):
    """Shared chrome: top border line, header bg, Save/Cancel button row."""

    def __init__(self, get_userlist_path: Callable[[], Optional[Path]],
                 on_saved: Callable[[str], None]):
        super().__init__()
        self._get_userlist_path = get_userlist_path
        self._on_saved = on_saved

        p = active_palette()
        self._c_bg_header = _c(p, "BG_HEADER")
        self._c_bg_deep = _c(p, "BG_DEEP")
        self._c_bg_hover = _c(p, "BG_HOVER")
        self._c_bg_panel = _c(p, "BG_PANEL")
        self._c_border = _c(p, "BORDER")
        self._c_text = _c(p, "TEXT_MAIN")
        self._c_text_dim = _c(p, "TEXT_DIM")
        self._c_accent = _c(p, "ACCENT")
        self._c_accent_hov = _c(p, "ACCENT_HOV")
        self._c_on_accent = _c(p, "TEXT_ON_ACCENT")

        self.setStyleSheet(f"""
            QLabel {{ color:{self._c_text_dim}; }}
            QLineEdit {{ background:{self._c_bg_deep}; color:{self._c_text};
                         border:1px solid {self._c_border}; border-radius:4px;
                         padding:3px; }}
            QComboBox {{ background:{self._c_bg_deep}; color:{self._c_text};
                         border:1px solid {self._c_border}; border-radius:4px;
                         padding:3px 6px; }}
            QComboBox QAbstractItemView {{ background:{self._c_bg_panel};
                                           color:{self._c_text}; }}
        """)
        self.setAutoFillBackground(True)
        self.hide()

    def _chrome(self) -> QVBoxLayout:
        """Top border line + padded inner layout on a header-coloured bar."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{self._c_border}; border:none;")
        outer.addWidget(line)
        body = QWidget()
        body.setStyleSheet(f"background:{self._c_bg_header};")
        outer.addWidget(body)
        inner = QVBoxLayout(body)
        inner.setContentsMargins(8, 6, 8, 6)
        inner.setSpacing(4)
        return inner

    def _button_row(self, name_label: QLabel, on_save, on_cancel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(name_label, 1)
        save = QPushButton(self.tr("Save"))
        save.setFixedSize(70, 24)
        save.setCursor(Qt.PointingHandCursor)
        save.setStyleSheet(
            f"QPushButton {{ background:{self._c_accent};"
            f" color:{self._c_on_accent}; border:none; border-radius:4px; }}"
            f"QPushButton:hover {{ background:{self._c_accent_hov}; }}")
        save.clicked.connect(on_save)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setFixedSize(70, 24)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setStyleSheet(
            f"QPushButton {{ background:{self._c_bg_deep};"
            f" color:{self._c_text}; border:none; border-radius:4px; }}"
            f"QPushButton:hover {{ background:{self._c_bg_hover}; }}")
        cancel.clicked.connect(on_cancel)
        row.addWidget(save)
        row.addWidget(cancel)
        return row


class UserlistBar(_BarBase):
    """Inline 'Add to userlist' panel: After / Before |-separated entries."""

    def __init__(self, get_userlist_path, on_saved):
        super().__init__(get_userlist_path, on_saved)
        self._plugin: str = ""

        inner = self._chrome()
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        grid.addWidget(QLabel(self.tr("After:")), 0, 0, Qt.AlignLeft)
        self._after_edit = QLineEdit()
        grid.addWidget(self._after_edit, 0, 1)
        grid.addWidget(QLabel(self.tr("Before:")), 1, 0, Qt.AlignLeft)
        self._before_edit = QLineEdit()
        grid.addWidget(self._before_edit, 1, 1)
        hint = QLabel(self.tr("Separate multiple plugins with  |"))
        grid.addWidget(hint, 2, 1, Qt.AlignLeft)
        grid.setColumnStretch(1, 1)
        inner.addLayout(grid)

        self._name_label = QLabel("")
        inner.addLayout(self._button_row(self._name_label,
                                         self._save, self.cancel))

    def open_for(self, plugin_name: str, after_prefill: str,
                 before_prefill: str):
        """Show the bar prefilled for this plugin (Tk _add_plugin_to_userlist -
        the caller derives the prefills from the current load order position)."""
        self._plugin = plugin_name
        self._after_edit.setText(after_prefill)
        self._before_edit.setText(before_prefill)
        self._name_label.setText(plugin_name)
        self.show()

    def cancel(self):
        self._plugin = ""
        self.hide()

    def _save(self):
        plugin_name = self._plugin
        ul_path = self._get_userlist_path()
        if not plugin_name or ul_path is None:
            self.cancel()
            return
        data = parse_userlist(ul_path)
        set_plugin_rules(data, plugin_name,
                         after=_parse_pipe_list(self._after_edit.text()),
                         before=_parse_pipe_list(self._before_edit.text()))
        ul_path.parent.mkdir(parents=True, exist_ok=True)
        write_userlist(ul_path, data)
        self._on_saved(f"Userlist updated: {plugin_name}")
        self.cancel()


class GroupBar(_BarBase):
    """Inline group-assignment panel for one or more plugins."""

    def __init__(self, get_userlist_path, on_saved):
        super().__init__(get_userlist_path, on_saved)
        self._plugins: list[str] = []

        inner = self._chrome()
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel(self.tr("Group:")))
        self._group_combo = QComboBox()
        row.addWidget(self._group_combo, 1)
        inner.addLayout(row)

        self._name_label = QLabel("")
        inner.addLayout(self._button_row(self._name_label,
                                         self._save, self.cancel))

    def open_for(self, plugin_names: list[str]):
        """Show the bar for these plugins with the current group preselected
        (Tk _add_plugins_to_group)."""
        ul_path = self._get_userlist_path()
        if ul_path is None or not plugin_names:
            return
        data = (parse_userlist(ul_path) if ul_path.is_file()
                else {"plugins": [], "groups": []})
        groups = [g["name"] for g in data.get("groups", []) if g.get("name")]
        if DEFAULT_GROUP not in groups:
            groups.insert(0, DEFAULT_GROUP)

        # Use current group of first plugin as default selection
        first = next(
            (e for e in data["plugins"]
             if e.get("name", "").lower() == plugin_names[0].lower()),
            {},
        )
        current_group = first.get("group", DEFAULT_GROUP)

        self._group_combo.clear()
        self._group_combo.addItems(groups)
        self._group_combo.setCurrentIndex(
            groups.index(current_group) if current_group in groups else 0)
        label = (plugin_names[0] if len(plugin_names) == 1
                 else f"{len(plugin_names)} plugins")
        self._name_label.setText(label)
        self._plugins = list(plugin_names)
        self.show()

    def cancel(self):
        self._plugins = []
        self.hide()

    def _save(self):
        plugin_names = self._plugins
        ul_path = self._get_userlist_path()
        if not plugin_names or ul_path is None:
            self.cancel()
            return
        group = self._group_combo.currentText()
        data = (parse_userlist(ul_path) if ul_path.is_file()
                else {"plugins": [], "groups": []})
        set_plugin_group(data, plugin_names, group)
        ul_path.parent.mkdir(parents=True, exist_ok=True)
        write_userlist(ul_path, data)
        self._on_saved(f"Group assigned: {len(plugin_names)} plugin(s) → {group}")
        self.cancel()
