"""Install-name patterns editor - a modlist-panel-scoped tab.

Opened from Settings ▸ General ("Edit custom install-name rules…", app.py
`_open_install_name_patterns_tab`) via `open_scoped_tab(..., key="install_name_patterns")`,
the same modlist-scoped mechanism as the Settings tab itself.

Lets a user author ordered search/replace regex rules that are applied to a
downloaded archive's filename stem *before* the built-in Nexus/mod.io name
parsers (see `Utils.mods.names._apply_custom_patterns`). This is the escape
hatch for when Nexus changes its download-name format again: rather than shipping
a code change, a user adds a rule here.

Each rule is `re.sub(search, replace, stem)`. Rules persist to amethyst.ini
through `Utils.ui.config.load/save_install_name_patterns` on every change - no
Save button, matching the rest of the Qt Settings tab. A live "Test" box shows
the result of running the current rules against a sample filename so a user can
verify a regex before relying on it.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame,
    QLabel, QCheckBox, QLineEdit, QPushButton, QGroupBox,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.ui import config as uc


# Generic placeholder for the Test box - a made-up mod name in the current Nexus
# "<name> <id> <version> <timestamp> <slug>" format, so it exercises the default
# rules without naming a real mod. (Fields: name / id / version / timestamp / slug.)
_EXAMPLE_FILENAME = "Example Mod Name 100000 1 2026-01-01T00-00Z AbCdEf01g"


class _RuleRow(QWidget):
    """One editable rule: [enabled] [search] → [replace] [✕].

    Any edit calls back into the parent view's ``_on_rule_changed`` so the
    whole list is re-serialised and the live test re-run.
    """

    def __init__(self, parent_view, rule: dict):
        super().__init__()
        self._view = parent_view
        # id/label mark a built-in default row: it keeps its id so the whole set
        # round-trips, and gets a name label + a per-row reset button.
        self._id = str(rule.get("id", "")) or None
        self._label = str(rule.get("label", "")) or None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        if self._label:
            cap = QLabel(self._label)
            cap.setObjectName("RuleLabel")
            outer.addWidget(cap)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        outer.addLayout(row)

        self.enabled = QCheckBox()
        self.enabled.setChecked(bool(rule.get("enabled", True)))
        self.enabled.setToolTip(parent_view.tr("Enable this rule"))
        self.enabled.toggled.connect(lambda _v: self._view._on_rule_changed())
        row.addWidget(self.enabled)

        self.search = QLineEdit(str(rule.get("search", "")))
        self.search.setPlaceholderText(parent_view.tr("Search regex"))
        self.search.textChanged.connect(lambda _t: self._view._on_rule_changed())
        row.addWidget(self.search, 3)

        arrow = QLabel("→")
        row.addWidget(arrow)

        self.replace = QLineEdit(str(rule.get("replace", "")))
        self.replace.setPlaceholderText(parent_view.tr(r"Replacement (e.g. \1)"))
        self.replace.textChanged.connect(lambda _t: self._view._on_rule_changed())
        row.addWidget(self.replace, 2)

        # Per-row reset - only for built-in default rows (those with a known id).
        if self._id and parent_view._has_default(self._id):
            reset = QPushButton("↺")
            reset.setFixedWidth(30)
            reset.setCursor(Qt.PointingHandCursor)
            reset.setToolTip(parent_view.tr("Reset this rule to its default"))
            reset.clicked.connect(lambda: self._view._reset_row(self))
            row.addWidget(reset)

        remove = QPushButton("✕")
        remove.setFixedWidth(30)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip(parent_view.tr("Remove this rule"))
        remove.clicked.connect(lambda: self._view._remove_row(self))
        row.addWidget(remove)

    def rule_id(self) -> str | None:
        return self._id

    def set_values(self, search: str, replace: str, enabled: bool) -> None:
        self.search.setText(search)
        self.replace.setText(replace)
        self.enabled.setChecked(enabled)

    def to_rule(self) -> dict:
        r = {
            "search": self.search.text(),
            "replace": self.replace.text(),
            "enabled": self.enabled.isChecked(),
        }
        if self._id:
            r["id"] = self._id
        if self._label:
            r["label"] = self._label
        return r


class InstallNamePatternsView(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._pal = active_palette()
        self.setObjectName("InstallNamePatternsView")
        self._rows: list[_RuleRow] = []
        # {id: default rule dict} for the built-in rules, used for per-row and
        # global reset.
        try:
            from Utils.mods.names import default_install_name_rules
            self._defaults = default_install_name_rules()
        except Exception:
            self._defaults = []
        self._defaults_by_id = {d["id"]: d for d in self._defaults if d.get("id")}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(16, 14, 16, 18)
        self._v.setSpacing(14)

        self.setStyleSheet(self._qss())

        title = QLabel(self.tr("Install-name rules"))
        f = title.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True)
        title.setFont(f)
        self._v.addWidget(title)

        intro = QLabel(self.tr(
            "Rules are applied in order to a downloaded archive's filename "
            "(without extension) to work out the mod name. The built-in rules "
            "for the known Nexus / mod.io download formats are shown below and "
            "can be edited or reset to their defaults; add your own to adapt to "
            "a new format without waiting for an update. Each rule runs a "
            "regular-expression search/replace; use \\1, \\2 … to keep captured "
            "groups."))
        intro.setObjectName("Help")
        intro.setWordWrap(True)
        self._v.addWidget(intro)

        # --- rules list ----------------------------------------------------
        self._rules_box = QGroupBox(self.tr("Rules"))
        self._rules_v = QVBoxLayout(self._rules_box)
        self._rules_v.setContentsMargins(8, 8, 8, 8)
        self._rules_v.setSpacing(6)
        self._v.addWidget(self._rules_box)

        # Section headers separating the shipped defaults from user-added rules,
        # so a blank custom row doesn't look like it belongs to the mod.io row
        # above it. They are inserted by _relayout() and hidden when their
        # section is empty.
        self._builtin_hdr = QLabel(self.tr("Built-in rules"))
        self._builtin_hdr.setObjectName("SectionHdr")
        self._custom_hdr = QLabel(self.tr("Custom rules"))
        self._custom_hdr.setObjectName("SectionHdr")

        self._empty_lbl = QLabel(self.tr("No rules yet - add one below."))
        self._empty_lbl.setObjectName("Help")
        self._rules_v.addWidget(self._empty_lbl)

        add_btn = QPushButton(self.tr("Add rule"))
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_blank_row)
        restore_btn = QPushButton(self.tr("Restore defaults"))
        restore_btn.setCursor(Qt.PointingHandCursor)
        restore_btn.setToolTip(self.tr(
            "Reset the built-in rules to their defaults and re-add any that "
            "were removed. Your own custom rules are kept."))
        restore_btn.clicked.connect(self._restore_defaults)
        add_row = QHBoxLayout()
        add_row.addWidget(add_btn)
        add_row.addWidget(restore_btn)
        add_row.addStretch(1)
        self._v.addLayout(add_row)

        # --- live test -----------------------------------------------------
        test_box = QGroupBox(self.tr("Test"))
        test_v = QVBoxLayout(test_box)
        test_v.setContentsMargins(8, 8, 8, 8)
        test_v.setSpacing(6)
        test_help = QLabel(self.tr(
            "Paste a downloaded filename to see the resulting mod name."))
        test_help.setObjectName("Help")
        test_help.setWordWrap(True)
        test_v.addWidget(test_help)
        self._test_in = QLineEdit()
        self._test_in.setText(_EXAMPLE_FILENAME)
        self._test_in.setPlaceholderText(_EXAMPLE_FILENAME)
        self._test_in.textChanged.connect(lambda _t: self._update_test())
        test_v.addWidget(self._test_in)
        self._test_out = QLabel("")
        self._test_out.setObjectName("TestOut")
        self._test_out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._test_out.setWordWrap(True)
        test_v.addWidget(self._test_out)
        self._v.addWidget(test_box)

        self._v.addStretch(1)

        # Load saved rules
        for rule in uc.load_install_name_patterns():
            self._append_row(rule)
        self._refresh_empty()
        self._update_test()

    # ---- styling ----------------------------------------------------------
    def _qss(self) -> str:
        c = lambda k: _c(self._pal, k)
        return f"""
        QGroupBox {{
            border: 1px solid {c('BORDER')};
            border-radius: 6px;
            margin-top: 10px;
            padding: 10px 12px 12px 12px;
            background: {c('BG_PANEL')};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px; padding: 0 5px;
            color: {c('TEXT_MAIN')};
            font-weight: bold;
        }}
        QLabel#Help {{ color: {c('TEXT_DIM')}; }}
        QLabel#RuleLabel {{ color: {c('TEXT_DIM')}; font-size: 11px; padding-top: 4px; }}
        QLabel#SectionHdr {{
            color: {c('TEXT_MAIN')}; font-weight: bold;
            padding: 6px 0 2px 0;
            border-bottom: 1px solid {c('BORDER')};
        }}
        QLabel#TestOut {{ color: {c('TEXT_MAIN')}; padding: 4px 0; }}
        """

    # ---- row management ---------------------------------------------------
    def _is_builtin(self, row: "_RuleRow") -> bool:
        rid = row.rule_id()
        return bool(rid and self._has_default(rid))

    def _append_row(self, rule: dict) -> None:
        row = _RuleRow(self, rule)
        self._rows.append(row)
        self._rules_v.addWidget(row)

    def _add_blank_row(self) -> None:
        self._append_row({"search": "", "replace": "", "enabled": True})
        self._relayout()
        self._on_rule_changed()

    def _remove_row(self, row: _RuleRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._relayout()
        self._on_rule_changed()

    def _relayout(self) -> None:
        """Re-order the rules list as [Built-in header] built-in rows /
        [Custom header] custom rows, showing each header only when its section
        is non-empty. Detach every managed widget first, then re-add in order -
        cheap for the handful of rows involved."""
        for w in (self._builtin_hdr, self._custom_hdr, self._empty_lbl, *self._rows):
            self._rules_v.removeWidget(w)
        builtins = [r for r in self._rows if self._is_builtin(r)]
        customs = [r for r in self._rows if not self._is_builtin(r)]

        if builtins:
            self._rules_v.addWidget(self._builtin_hdr)
            self._builtin_hdr.setVisible(True)
            for r in builtins:
                self._rules_v.addWidget(r)
        else:
            self._builtin_hdr.setVisible(False)

        # Show the "Custom rules" header only when a custom row exists, so a new
        # custom row lands under its own heading (not under the mod.io row) -
        # but no empty header hangs when there are none.
        if customs:
            self._rules_v.addWidget(self._custom_hdr)
            self._custom_hdr.setVisible(True)
            for r in customs:
                self._rules_v.addWidget(r)
        else:
            self._custom_hdr.setVisible(False)

        self._rules_v.addWidget(self._empty_lbl)
        self._empty_lbl.setVisible(not self._rows)

    def _refresh_empty(self) -> None:
        self._relayout()

    # ---- defaults / reset -------------------------------------------------
    def _has_default(self, rule_id: str) -> bool:
        """True if *rule_id* names a built-in default (so it can be reset)."""
        return rule_id in self._defaults_by_id

    def _reset_row(self, row: _RuleRow) -> None:
        """Restore a single built-in row's search/replace/enabled to its default."""
        d = self._defaults_by_id.get(row.rule_id() or "")
        if not d:
            return
        row.set_values(str(d.get("search", "")), str(d.get("replace", "")),
                       bool(d.get("enabled", True)))
        self._on_rule_changed()

    def _restore_defaults(self) -> None:
        """Reset every built-in row to its default, re-add any removed defaults
        (in their canonical order, before custom rules), and keep custom rules."""
        # Custom rows = those without a known default id; preserve their order.
        custom = [r.to_rule() for r in self._rows
                  if not (r.rule_id() and self._has_default(r.rule_id()))]
        rebuilt = [dict(d) for d in self._defaults] + custom
        # Rebuild the whole list from scratch.
        for r in list(self._rows):
            r.setParent(None)
            r.deleteLater()
        self._rows.clear()
        for rule in rebuilt:
            self._append_row(rule)
        self._refresh_empty()
        self._on_rule_changed()

    # ---- persistence + test ----------------------------------------------
    def _on_rule_changed(self) -> None:
        """Serialise every row and persist. Rules with an empty search are kept
        in the UI (so the user can finish typing) but save_* drops them."""
        rules = [r.to_rule() for r in self._rows]
        try:
            uc.save_install_name_patterns(rules)
        except Exception as exc:
            self._notify(self.tr("Failed to save rules: {0}").format(exc), "warning")
        self._update_test()

    def _update_test(self) -> None:
        """Preview the name the installer would derive from the test filename.

        Runs the REAL pipeline (``mod_name_utils._suggest_mod_names``), not just
        the visible regex rows - so it accounts for the duplicate-download suffix
        strip ("(2)"), the built-in heuristic parsers, and the fact that edits
        are already saved (each change persists before this runs). This keeps the
        preview honest: what it shows is what an install produces."""
        stem = self._test_in.text().strip()
        # Strip a trailing extension if the user pasted a full filename, so the
        # test mirrors what the installer feeds in (the stem, no extension).
        for ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        if not stem:
            self._test_out.setText("")
            return
        try:
            from Utils.mods.names import (
                _suggest_mod_names, sanitize_mod_folder_name)
            suggestions = _suggest_mod_names(stem)
            result = (suggestions[0] if suggestions else stem) or stem
            result = sanitize_mod_folder_name(result) or result
        except Exception:
            result = stem
        # Flag any row whose regex won't compile so the user knows it's ignored.
        has_bad = False
        for row in self._rows:
            if not row.enabled.isChecked():
                continue
            search = row.search.text()
            if not search:
                continue
            try:
                re.compile(search)
            except re.error:
                has_bad = True
        if has_bad:
            self._test_out.setText(
                self.tr("Result: {0}   (a rule has an invalid regex - skipped)")
                .format(result))
        else:
            self._test_out.setText(self.tr("Result: {0}").format(result))

    # ---- notify -----------------------------------------------------------
    def _notify(self, text: str, state: str = "info"):
        win = self._window
        if win is not None and hasattr(win, "_notify"):
            try:
                win._notify(text, state)
                return
            except Exception:
                pass
        print(f"[install-name-patterns] {text}")
