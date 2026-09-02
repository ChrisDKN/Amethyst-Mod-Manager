"""LOOT Groups view - configure LOOT groups and group ordering rules.

Qt port of the Tk gui/loot_groups_overlay.py LootGroupsOverlay (1:1 behaviour).
Opens as a modlist-panel-scoped tab (Tk places it over the modlist panel).

Groups and rules are stored in the active profile's userlist.yaml alongside
plugin entries. Rules define load order relationships between groups, e.g.
"groupA loads after groupB".

Tk semantics kept exactly:
- "default" group always present (first) and can't be removed.
- Adding a group is only persisted on Save or the next rule operation.
- Add/remove rule saves immediately; removing a group rewrites plugin entries
  immediately (rules-bearing plugins → 'default', bare ones dropped).
- Add rule normalises "before" (A before B → B after A), skips duplicates and
  a direct reverse rule (would be an instant cycle).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QComboBox, QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, close_button, button_qss
from gui_qt.wheel_guard import no_wheel
from Utils.plugins.userlist import (
    DEFAULT_GROUP, parse_userlist, write_userlist, remove_group,
)


class PluginGroupsView(QWidget):
    """Modlist-scoped tab body for managing LOOT groups + group rules."""

    def __init__(
        self,
        userlist_path: Path,
        on_close: Optional[Callable[[], None]] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self._ul_path = userlist_path
        self._on_close = on_close or (lambda: None)
        self._on_saved = on_saved or (lambda: None)

        data = (parse_userlist(userlist_path) if userlist_path.is_file()
                else {"plugins": [], "groups": []})
        # groups: list of {"name": str, "after": [str]}
        self._groups: list[str] = [g["name"] for g in data.get("groups", [])
                                   if g.get("name")]
        if DEFAULT_GROUP not in self._groups:
            self._groups.insert(0, DEFAULT_GROUP)

        # rules: list of (group_a, "after"|"before", group_b) - stored as "after".
        self._rules: list[tuple[str, str, str]] = []
        for g in data.get("groups", []):
            name = g.get("name", "")
            for after_grp in g.get("after", []):
                self._rules.append((name, "after", after_grp))

        self.setObjectName("PluginGroupsView")
        self._build()

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
        self._c_danger = _c(p, "BTN_DANGER")
        self._c_accent = _c(p, "ACCENT")
        self._c_accent_hov = _c(p, "ACCENT_HOV")
        self._c_on_accent = _c(p, "TEXT_ON_ACCENT")

        self.setStyleSheet(f"""
            #PluginGroupsView {{ background:{self._c_bg_deep}; }}
            QLabel {{ color:{self._c_text}; }}
            QLineEdit {{ background:{self._c_bg_panel}; color:{self._c_text};
                         border:1px solid {self._c_border}; border-radius:4px;
                         padding:4px; }}
            QListWidget {{ background:{self._c_bg_panel}; color:{self._c_text};
                           border:1px solid {self._c_border}; }}
            QListWidget::item:selected {{ background:{self._c_accent};
                                          color:{self._c_on_accent}; }}
            QComboBox {{ background:{self._c_bg_header}; color:{self._c_text};
                         border:1px solid {self._c_border}; border-radius:4px;
                         padding:3px 6px; }}
            QComboBox QAbstractItemView {{ background:{self._c_bg_panel};
                                           color:{self._c_text}; }}
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
        title = QLabel(self.tr("LOOT Groups - Right click plugins to add them to groups"))
        title.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        tb.addWidget(title, 1)

        save_btn = QPushButton(self.tr("Save"))
        save_btn.setFixedSize(80, 30)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(
            f"QPushButton {{ background:{self._c_accent}; color:{self._c_on_accent};"
            f" border:none; border-radius:4px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{self._c_accent_hov}; }}")
        save_btn.clicked.connect(self._do_save)
        tb.addWidget(save_btn)

        close_btn = close_button(pal=p)
        close_btn.clicked.connect(self._do_close)
        tb.addWidget(close_btn)
        root.addWidget(toolbar)

        # ---- body: Groups (left) | divider | Rules (right), 1:2 ----
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(8)

        bl.addWidget(self._build_groups_panel(), 1)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)
        divider.setStyleSheet(f"background:{self._c_border}; border:none;")
        bl.addWidget(divider)

        bl.addWidget(self._build_rules_panel(), 2)
        root.addWidget(body, 1)

    def _build_groups_panel(self) -> QWidget:
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QLabel(self.tr("Groups"))
        hdr.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(hdr)

        self._groups_list = QListWidget()
        v.addWidget(self._groups_list, 1)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._new_group_edit = QLineEdit()
        self._new_group_edit.returnPressed.connect(self._add_group)
        add_row.addWidget(self._new_group_edit, 1)
        add_btn = QPushButton(self.tr("Add"))
        add_btn.setFixedSize(60, 28)
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="0px"))
        add_btn.clicked.connect(self._add_group)
        add_row.addWidget(add_btn)
        v.addLayout(add_row)

        remove_btn = QPushButton(self.tr("Remove Selected"))
        remove_btn.setFixedSize(140, 28)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setStyleSheet(
            f"QPushButton {{ background:{self._c_bg_header}; color:{self._c_text};"
            f" border:none; border-radius:4px; }}"
            f"QPushButton:hover {{ background:{self._c_bg_hover}; }}")
        remove_btn.clicked.connect(self._remove_group)
        v.addWidget(remove_btn, 0, Qt.AlignLeft)

        self._repaint_groups()
        return left

    def _build_rules_panel(self) -> QWidget:
        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QLabel(self.tr("Group Rules"))
        hdr.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(hdr)

        # New rule row.
        add_lbl = QLabel(self.tr("Add rule:"))
        add_lbl.setStyleSheet(f"color:{self._c_text_dim};")
        v.addWidget(add_lbl)

        rule_row = QHBoxLayout()
        rule_row.setSpacing(4)
        self._rule_a_combo = QComboBox()
        rule_row.addWidget(self._rule_a_combo, 1)
        self._rule_rel_combo = QComboBox()
        self._rule_rel_combo.addItems(["after", "before"])
        self._rule_rel_combo.setFixedWidth(90)
        rule_row.addWidget(self._rule_rel_combo)
        self._rule_b_combo = QComboBox()
        rule_row.addWidget(self._rule_b_combo, 1)
        no_wheel(self._rule_a_combo, self._rule_rel_combo, self._rule_b_combo)
        add_rule_btn = QPushButton(self.tr("Add Rule"))
        add_rule_btn.setFixedSize(100, 28)
        add_rule_btn.setCursor(Qt.PointingHandCursor)
        add_rule_btn.setStyleSheet(button_qss("BTN_SUCCESS", padding="0px"))
        add_rule_btn.clicked.connect(self._add_rule)
        rule_row.addWidget(add_rule_btn)
        v.addLayout(rule_row)
        self._refresh_rule_menus()

        # Rules list (scrollable rows).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ background:{self._c_bg_panel};"
            f" border:1px solid {self._c_border}; }}")
        self._rules_container = QWidget()
        self._rules_container.setStyleSheet(f"background:{self._c_bg_panel};")
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(0)
        self._rules_layout.addStretch(1)
        scroll.setWidget(self._rules_container)
        v.addWidget(scroll, 1)

        self._repaint_rules()
        return right

    # ------------------------------------------------------------------
    # Groups management
    # ------------------------------------------------------------------

    def _repaint_groups(self):
        self._groups_list.clear()
        for g in self._groups:
            self._groups_list.addItem(g)

    def _add_group(self):
        name = self._new_group_edit.text().strip()
        if not name or name in self._groups:
            return
        self._groups.append(name)
        self._new_group_edit.clear()
        self._repaint_groups()
        self._refresh_rule_menus()

    def _remove_group(self):
        row = self._groups_list.currentRow()
        if row < 0 or row >= len(self._groups):
            return
        name = self._groups[row]
        if name == DEFAULT_GROUP:
            return  # don't remove default
        self._groups.pop(row)
        # Remove group ordering rules referencing this group
        self._rules = [r for r in self._rules if r[0] != name and r[2] != name]
        # Update plugins in userlist that were in this group
        if self._ul_path.is_file():
            data = parse_userlist(self._ul_path)
            # Only rewrite if any plugin actually references the removed group
            if remove_group(data, name):
                write_userlist(self._ul_path, data)
        self._repaint_groups()
        self._refresh_rule_menus()
        self._repaint_rules()

    def _refresh_rule_menus(self):
        vals = self._groups or [DEFAULT_GROUP]
        for combo in (self._rule_a_combo, self._rule_b_combo):
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(vals)
            combo.setCurrentIndex(vals.index(cur) if cur in vals else 0)
            combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Rules management
    # ------------------------------------------------------------------

    def _repaint_rules(self):
        # Clear all rows (keep the trailing stretch).
        while self._rules_layout.count() > 1:
            item = self._rules_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._rules:
            empty = QLabel(self.tr("No rules defined."))
            empty.setStyleSheet(f"color:{self._c_text_dim}; padding:12px;")
            self._rules_layout.insertWidget(0, empty)
            return

        for i, (a, rel, b) in enumerate(self._rules):
            row_bg = self._c_bg_row if i % 2 == 0 else self._c_bg_row_alt
            row = QWidget()
            row.setStyleSheet(f"background:{row_bg};")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 4, 6, 4)
            h.setSpacing(6)

            lbl_a = QLabel(a)
            lbl_a.setStyleSheet(f"color:{self._c_text};")
            h.addWidget(lbl_a)
            lbl_rel = QLabel(rel)
            lbl_rel.setStyleSheet(f"color:{self._c_accent};")
            h.addWidget(lbl_rel)
            lbl_b = QLabel(b)
            lbl_b.setStyleSheet(f"color:{self._c_text};")
            h.addWidget(lbl_b)
            h.addStretch(1)

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

    def _add_rule(self):
        a = self._rule_a_combo.currentText().strip()
        rel = self._rule_rel_combo.currentText().strip()
        b = self._rule_b_combo.currentText().strip()
        if not a or not b or a == b:
            return
        # Normalise: "before" means b loads after a → store as (b, "after", a)
        if rel == "before":
            a, b = b, a
            rel = "after"
        if (a, rel, b) in self._rules:
            return
        # Check for reverse rule which would create a cycle
        if (b, "after", a) in self._rules:
            return
        self._rules.append((a, rel, b))
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
        data = (parse_userlist(self._ul_path) if self._ul_path.is_file()
                else {"plugins": [], "groups": []})

        group_after: dict[str, list[str]] = {g: [] for g in self._groups}
        for a, _rel, b in self._rules:
            if a in group_after:
                if b not in group_after[a]:
                    group_after[a].append(b)
            else:
                group_after[a] = [b]

        new_groups = []
        for g in self._groups:
            if g == DEFAULT_GROUP and not group_after.get(g):
                continue
            entry: dict = {"name": g}
            if group_after.get(g):
                entry["after"] = group_after[g]
            new_groups.append(entry)

        data["groups"] = new_groups
        self._ul_path.parent.mkdir(parents=True, exist_ok=True)
        write_userlist(self._ul_path, data)
        self._on_saved()

    def _do_save(self):
        self._save_current()
        self._do_close()

    def _do_close(self):
        self._on_close()
