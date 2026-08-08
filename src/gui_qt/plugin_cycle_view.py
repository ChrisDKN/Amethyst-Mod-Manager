"""Plugin Cycle view - view and resolve a broken cycle in userlist.yaml.

Qt port of the Tk gui/plugin_cycle_overlay.py PluginCycleOverlay (1:1).
Opens as a modlist-panel-scoped tab when the user right-clicks a plugin with a
red userlist dot and picks 'Show cycle…' (or 'Show userlist rules…' - both open
this view, Tk parity). Once open, the view is pinned to the set of plugins that
formed the cycle at open time. Each plugin rule connecting any two of those
plugins gets a Flip button so the user can iteratively resolve (and, if needed,
revert) the cycle in-place. Group rules are informational.

A status banner at the top turns red while a cycle is still present among the
pinned plugins and green once it has been resolved.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button

_PAL = active_palette()
STATUS_BROKEN_BG = _c(_PAL, "PLUGIN_CYCLE_ERR_BG")
STATUS_BROKEN_FG = _c(_PAL, "PLUGIN_CYCLE_ERR_FG")
STATUS_OK_BG = _c(_PAL, "PLUGIN_CYCLE_OK_BG")
STATUS_OK_FG = _c(_PAL, "PLUGIN_CYCLE_OK_FG")

# Background for rule rows whose flip (on its own) would resolve every cycle
# currently present in the scope. Chosen to read as a warm highlight against
# the normal BG_ROW / BG_ROW_ALT palette without mimicking the red error tone.
FIXABLE_ROW_BG = _c(_PAL, "PLUGIN_CYCLE_WARN_BG")
FIXABLE_ROW_FG = _c(_PAL, "PLUGIN_CYCLE_WARN_FG")

# Per-keyword colors so "before" and "after" read as opposites at a glance.
BEFORE_FG = _c(_PAL, "PLUGIN_CYCLE_ANCHOR")
AFTER_FG = _c(_PAL, "PLUGIN_CYCLE_LINK")


class PluginCycleView(QWidget):
    """Modlist-scoped tab body describing one userlist.yaml cycle, with Flip
    actions for plugin rules so the user can iteratively resolve (or revert)
    the cycle in place."""

    def __init__(
        self,
        starting_plugin: str,
        on_close: Optional[Callable[[], None]] = None,
        on_flip: Optional[Callable[[str, str, str], None]] = None,
    ):
        super().__init__()
        self._starting = starting_plugin
        self._on_close = on_close or (lambda: None)
        self._on_flip = on_flip

        # Populated by update_cycle(); default to empty state so _build works
        # before data arrives.
        self._plugins: list[str] = []
        self._edges: dict[tuple[str, str], list[dict]] = {}
        self._cyclic_edges: set[tuple[str, str]] = set()
        self._fixable_reasons: set[tuple[str, str, str]] = set()
        self._display: dict[str, str] = {}
        self._is_broken: bool = True

        self.setObjectName("PluginCycleView")
        self._build()

    # ------------------------------------------------------------------
    # Data updates
    # ------------------------------------------------------------------

    def update_cycle(
        self,
        starting_plugin: str,
        scope_plugins: frozenset[str],
        scope_edges: dict[tuple[str, str], list[dict]],
        cyclic_edges: set[tuple[str, str]],
        fixable_reasons: set[tuple[str, str, str]],
        display_names: dict[str, str],
        is_broken: bool,
    ) -> None:
        """Replace the view's data and repaint.

        `scope_plugins`    - plugins pinned to this view (the original SCC).
        `scope_edges`      - every rule between scope plugins (cyclic or not).
        `cyclic_edges`     - subset of scope_edges whose endpoints are still in
                             the same SCC right now.
        `fixable_reasons`  - set of reason ids (owner_lower, field, target_lower)
                             whose single flip would resolve every current cycle.
        `is_broken`        - True if the scope currently contains any cycle.
        """
        self._starting = starting_plugin
        self._plugins = sorted(scope_plugins)
        self._edges = scope_edges
        self._cyclic_edges = cyclic_edges
        self._fixable_reasons = fixable_reasons
        self._display = display_names
        self._is_broken = is_broken
        self._repaint_title()
        self._repaint_status()
        self._repaint_plugins()
        self._repaint_rules()

    def _disp(self, name_lower: str) -> str:
        return self._display.get(name_lower, name_lower)

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

        self.setStyleSheet(f"""
            #PluginCycleView {{ background:{self._c_bg_deep}; }}
            QLabel {{ color:{self._c_text}; }}
            QListWidget {{ background:{self._c_bg_panel}; color:{self._c_text};
                           border:1px solid {self._c_border}; }}
            QListWidget::item:selected {{ background:{self._c_accent};
                                          color:white; }}
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
        self._title_label = QLabel("")
        self._title_label.setStyleSheet(
            f"color:{self._c_text}; font-weight:bold;")
        tb.addWidget(self._title_label, 1)
        close_btn = danger_close_button(pal=p)
        close_btn.clicked.connect(self._do_close)
        tb.addWidget(close_btn)
        root.addWidget(toolbar)

        # ---- status banner (red while broken, green when resolved) ----
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"background:{STATUS_BROKEN_BG}; color:{STATUS_BROKEN_FG};"
            " font-weight:bold; padding:8px 12px;")
        banner_row = QWidget()
        br = QHBoxLayout(banner_row)
        br.setContentsMargins(12, 10, 12, 0)
        br.addWidget(self._status_label, 1)
        root.addWidget(banner_row)

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

        self._repaint_title()
        self._repaint_status()

    def _build_plugins_panel(self) -> QWidget:
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QLabel(self.tr("Plugins in cycle"))
        hdr.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(hdr)

        self._plugins_list = QListWidget()
        v.addWidget(self._plugins_list, 1)
        return left

    def _build_rules_panel(self) -> QWidget:
        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QLabel(self.tr("Rules between these plugins"))
        hdr.setStyleSheet(f"color:{self._c_text}; font-weight:bold;")
        v.addWidget(hdr)

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
        return right

    # ------------------------------------------------------------------
    # Repaint
    # ------------------------------------------------------------------

    def _repaint_title(self):
        n = len(self._plugins)
        self._title_label.setText(
            (self.tr("Userlist rules (1 plugin) - anchor: {0}").format(self._starting)
             if n == 1
             else self.tr("Userlist rules ({0} plugins) - anchor: {1}")
             .format(n, self._starting)))

    def _repaint_status(self):
        if self._is_broken:
            bg, fg = STATUS_BROKEN_BG, STATUS_BROKEN_FG
            text = "Status: BROKEN - these plugins still form a cycle."
        else:
            bg, fg = STATUS_OK_BG, STATUS_OK_FG
            text = "Status: OK - no cycle among these plugins."
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"background:{bg}; color:{fg}; font-weight:bold; padding:8px 12px;")

    def _repaint_plugins(self):
        self._plugins_list.clear()
        for p in self._plugins:
            self._plugins_list.addItem(self._disp(p))

    def _repaint_rules(self):
        while self._rules_layout.count() > 1:
            item = self._rules_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # Flatten: one row per reason (not one row per edge). Each rule is a
        # single line showing the rule text + a flip/info control on the right.
        flat: list[tuple[tuple[str, str], dict]] = []
        for edge, reasons in self._edges.items():
            for reason in reasons:
                flat.append((edge, reason))
        flat.sort(key=lambda it: (
            self._disp(it[0][0]).lower(),
            self._disp(it[0][1]).lower(),
            0 if it[1].get("kind") == "plugin" else 1,
        ))

        if not flat:
            empty = QLabel(self.tr("No rules between these plugins."))
            empty.setStyleSheet(f"color:{self._c_text_dim}; padding:12px;")
            self._rules_layout.insertWidget(0, empty)
            return

        for i, (edge, reason) in enumerate(flat):
            rid = reason.get("id")
            fixable = rid is not None and rid in self._fixable_reasons
            if fixable:
                row_bg = FIXABLE_ROW_BG
                text_fg = FIXABLE_ROW_FG
            else:
                row_bg = self._c_bg_row if i % 2 == 0 else self._c_bg_row_alt
                text_fg = self._c_text

            row = QWidget()
            row.setStyleSheet(f"background:{row_bg};")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            h.setSpacing(6)

            tokens = QWidget()
            tokens.setStyleSheet(f"background:{row_bg};")
            th = QHBoxLayout(tokens)
            th.setContentsMargins(0, 0, 0, 0)
            th.setSpacing(0)
            self._build_rule_tokens(th, text_fg, reason)
            h.addWidget(tokens, 1)

            if reason.get("kind") == "plugin" and self._on_flip is not None:
                owner = reason.get("owner", "")
                field = reason.get("field", "")
                target = reason.get("target", "")
                if owner and target and field in ("after", "before"):
                    flip = QPushButton(self.tr("Flip rule"))
                    flip.setFixedSize(90, 22)
                    flip.setCursor(Qt.PointingHandCursor)
                    flip_fg = FIXABLE_ROW_FG if fixable else self._c_accent
                    flip.setStyleSheet(
                        f"QPushButton {{ background:{self._c_bg_header};"
                        f" color:{flip_fg}; border:none; border-radius:4px; }}"
                        f"QPushButton:hover {{ background:{self._c_bg_hover}; }}")
                    flip.clicked.connect(
                        lambda _=False, o=owner, f=field, t=target:
                        self._on_flip(o, f, t))
                    h.addWidget(flip)
            elif reason.get("kind") == "group":
                note = QLabel(self.tr("(group rule - edit via Groups overlay)"))
                note.setStyleSheet(f"color:{self._c_text_dim};")
                h.addWidget(note)

            self._rules_layout.insertWidget(i, row)

    def _build_rule_tokens(self, layout: QHBoxLayout, text_fg: str,
                           reason: dict) -> None:
        """Render a rule as inline tokens so 'before' and 'after' can be
        colored distinctly. Falls back to the plain text for anything we
        don't recognise."""
        def _lbl(text: str, color: str, bold: bool = False) -> QLabel:
            lbl = QLabel(text)
            weight = " font-weight:bold;" if bold else ""
            lbl.setStyleSheet(f"color:{color};{weight}")
            return lbl

        kind = reason.get("kind")
        if kind == "plugin":
            owner = reason.get("owner", "")
            field = reason.get("field", "")
            target = reason.get("target", "")
            if owner and target and field in ("after", "before"):
                kw_fg = AFTER_FG if field == "after" else BEFORE_FG
                layout.addWidget(_lbl(owner, text_fg))
                layout.addWidget(_lbl(f"  {field}  ", kw_fg, bold=True))
                layout.addWidget(_lbl(target, text_fg))
                layout.addStretch(1)
                return
        if kind == "group":
            # Group-rule text mentions "after" once; highlight that keyword too.
            text = reason.get("text", "")
            marker = " after "
            if marker in text:
                left, right = text.split(marker, 1)
                layout.addWidget(_lbl(left, self._c_text_dim))
                lbl = _lbl(" after ", AFTER_FG, bold=True)  # i18n: skip - LOOT rule match token
                layout.addWidget(lbl)
                layout.addWidget(_lbl(right, self._c_text_dim))
                layout.addStretch(1)
                return
        # Fallback - flat text.
        layout.addWidget(_lbl(reason.get("text", ""), text_fg))
        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _do_close(self):
        self._on_close()
