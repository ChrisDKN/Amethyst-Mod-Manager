"""Environment-variable editor - a modlist-panel-scoped tab.

Opened from Settings ▸ Advanced ("Edit environment variables…", app.py
`_open_env_vars_tab`) via `open_scoped_tab(..., key="env_vars")`, the same
mechanism as the Settings tab itself.

Lets a user pin environment variables that Amethyst applies to its OWN process
at startup - the kill switches and diagnostic flags that otherwise need a
terminal launch. The dropdown lists the variables the app understands (with a
description and its usual values); "Add custom variable" covers anything else,
including plain shell vars a user wants every tool launch to inherit.

Variables that can only work before the process starts (LD_PRELOAD, PYTHONPATH,
APPDIR …) are rejected - see `Utils.app_env`, which owns the catalogue, the
validation and the startup apply. Everything persists to amethyst.ini on every
change, like the rest of Settings; nothing takes effect until the next launch,
hence the explicit Restart button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QFrame,
    QLabel, QCheckBox, QComboBox, QLineEdit, QPushButton, QGroupBox,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.help_marker import tip_text, make_help_marker, help_mark_qss
from gui_qt.wheel_guard import no_wheel
from Utils import app_env
from Utils import ui_config as uc


class _EnvRow(QWidget):
    """One variable: [enabled] NAME = [value] [?] [✕].

    A known variable shows its name as a label and offers its usual values in an
    editable combo; a custom one gets an editable name field and a plain value
    box. Any edit calls back into the view's ``_on_changed`` so the whole list
    is re-serialised.
    """

    def __init__(self, view: "EnvVarsView", name: str, value: str,
                 enabled: bool):
        super().__init__()
        self._view = view
        self._spec = app_env.KNOWN_BY_NAME.get(name)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.enabled = QCheckBox()
        self.enabled.setChecked(bool(enabled))
        self.enabled.setToolTip(view.tr("Apply this variable at startup"))
        self.enabled.toggled.connect(lambda _v: self._view._on_changed())
        row.addWidget(self.enabled)

        # --- name ---
        self.name_edit: QLineEdit | None = None
        if self._spec is not None:
            lbl = QLabel(name)
            lbl.setObjectName("VarName")
            lbl.setMinimumWidth(230)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(lbl)
            self._name_widget = lbl
            self._name = name
        else:
            self.name_edit = QLineEdit(name)
            self.name_edit.setPlaceholderText("VARIABLE_NAME")
            self.name_edit.setMinimumWidth(230)
            self.name_edit.textChanged.connect(lambda _t: self._view._on_changed())
            row.addWidget(self.name_edit)
            self._name_widget = self.name_edit
            self._name = name

        row.addWidget(QLabel("="))

        # --- value ---
        values = (self._spec or {}).get("values") or []
        if values:
            self.value_combo = QComboBox()
            self.value_combo.setEditable(True)      # any other value still allowed
            self.value_combo.addItems([str(v) for v in values])
            self.value_combo.setCurrentText(value)
            self.value_combo.currentTextChanged.connect(
                lambda _t: self._view._on_changed())
            no_wheel(self.value_combo)
            self.value_widget = self.value_combo
        else:
            self.value_combo = None
            self.value_widget = QLineEdit(value)
            self.value_widget.setPlaceholderText(view.tr("value"))
            self.value_widget.textChanged.connect(lambda _t: self._view._on_changed())
        row.addWidget(self.value_widget, 1)

        if self._spec is not None:
            summary = self._spec.get("summary", "")
            self.setToolTip(tip_text(summary))
            row.addWidget(make_help_marker(summary))

        remove = QPushButton("✕")
        remove.setFixedWidth(30)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip(view.tr("Remove this variable"))
        remove.clicked.connect(lambda: self._view._remove_row(self))
        row.addWidget(remove)

    # ---- state ------------------------------------------------------------
    def var_name(self) -> str:
        if self.name_edit is not None:
            return self.name_edit.text().strip()
        return self._name

    def var_value(self) -> str:
        if self.value_combo is not None:
            return self.value_combo.currentText()
        return self.value_widget.text()

    def to_entry(self) -> dict:
        return {
            "name": self.var_name(),
            "value": self.var_value(),
            "enabled": self.enabled.isChecked(),
        }

    def mark_invalid(self, reason: str) -> None:
        """Colour the name field/label of a row that startup will skip, and put
        the reason in its tooltip. A known variable keeps its description
        tooltip when it's fine."""
        w = self._name_widget
        w.setProperty("invalid", "true" if reason else "false")
        if reason:
            w.setToolTip(tip_text(reason))
        elif self._spec is not None:
            w.setToolTip(tip_text(self._spec.get("summary", "")))
        else:
            w.setToolTip("")
        # Re-polish so the property selector in the stylesheet re-applies.
        w.style().unpolish(w)
        w.style().polish(w)


class EnvVarsView(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._pal = active_palette()
        self.setObjectName("EnvVarsView")
        self._rows: list[_EnvRow] = []
        self._dirty = False

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

        title = QLabel(self.tr("Environment variables"))
        f = title.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True)
        title.setFont(f)
        self._v.addWidget(title)

        intro = QLabel(self.tr(
            "Variables set here are applied to Amethyst itself every time it "
            "starts, so you don't have to launch it from a terminal to use one. "
            "Pick a variable Amethyst understands from the dropdown, or add any "
            "other one by hand - those are passed on to the tools and games "
            "Amethyst launches too.\n\n"
            "Changes take effect on the next launch. If a variable ever stops "
            "the app from starting, launch it once with "
            "AMM_NO_ENV_OVERRIDES=1 to skip them all and fix it here."))
        intro.setObjectName("Help")
        intro.setWordWrap(True)
        self._v.addWidget(intro)

        # --- variable list -------------------------------------------------
        self._vars_box = QGroupBox(self.tr("Variables"))
        self._vars_v = QVBoxLayout(self._vars_box)
        self._vars_v.setContentsMargins(8, 8, 8, 8)
        self._vars_v.setSpacing(6)
        self._v.addWidget(self._vars_box)

        self._empty_lbl = QLabel(self.tr("No variables set - add one below."))
        self._empty_lbl.setObjectName("Help")
        self._vars_v.addWidget(self._empty_lbl)

        # --- add row -------------------------------------------------------
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self._known_combo = QComboBox()
        self._known_combo.setMinimumWidth(260)
        no_wheel(self._known_combo)
        add_row.addWidget(self._known_combo)
        self._add_btn = QPushButton(self.tr("Add"))
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.clicked.connect(self._add_known)
        add_row.addWidget(self._add_btn)
        custom_btn = QPushButton(self.tr("Add custom variable"))
        custom_btn.setCursor(Qt.PointingHandCursor)
        custom_btn.setToolTip(self.tr(
            "Add a variable Amethyst doesn't know about - anything your system "
            "or a launched tool reads."))
        custom_btn.clicked.connect(self._add_custom)
        add_row.addWidget(custom_btn)
        add_row.addStretch(1)
        self._v.addLayout(add_row)

        # Description of whatever the dropdown currently shows, so a user can
        # read what a variable does before adding it.
        self._known_help = QLabel("")
        self._known_help.setObjectName("Help")
        self._known_help.setWordWrap(True)
        self._v.addWidget(self._known_help)
        self._known_combo.currentIndexChanged.connect(
            lambda _i: self._update_known_help())

        # --- restart -------------------------------------------------------
        restart_row = QHBoxLayout()
        restart_row.setSpacing(8)
        self._restart_note = QLabel(self.tr(
            "Changes take effect after a restart."))
        self._restart_note.setObjectName("RestartNote")
        self._restart_note.setVisible(False)
        self._restart_btn = QPushButton(self.tr("Restart now"))
        self._restart_btn.setCursor(Qt.PointingHandCursor)
        self._restart_btn.setVisible(False)
        self._restart_btn.clicked.connect(self._restart)
        restart_row.addWidget(self._restart_note)
        restart_row.addWidget(self._restart_btn)
        restart_row.addStretch(1)
        self._v.addLayout(restart_row)

        self._v.addStretch(1)

        for entry in uc.load_app_env_vars():
            self._append_row(entry["name"], entry["value"], entry["enabled"])
        self._refresh_known_combo()
        self._relayout()
        self._validate()

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
        {help_mark_qss(self._pal)}
        QLabel#Help {{ color: {c('TEXT_DIM')}; }}
        QLabel#VarName {{ color: {c('TEXT_MAIN')}; font-weight: bold; }}
        QLabel#VarName[invalid="true"] {{ color: {c('TEXT_ERR')}; }}
        QLabel#RestartNote {{ color: {c('TEXT_WARN')}; }}
        QLineEdit[invalid="true"] {{ border: 1px solid {c('TEXT_ERR')}; }}
        """

    # ---- rows -------------------------------------------------------------
    def _append_row(self, name: str, value: str, enabled: bool) -> _EnvRow:
        row = _EnvRow(self, name, value, enabled)
        self._rows.append(row)
        self._vars_v.addWidget(row)
        return row

    def _remove_row(self, row: _EnvRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._relayout()
        self._refresh_known_combo()
        self._on_changed()

    def _relayout(self) -> None:
        """Keep the empty-state label last and hide it once a row exists."""
        self._vars_v.removeWidget(self._empty_lbl)
        self._vars_v.addWidget(self._empty_lbl)
        self._empty_lbl.setVisible(not self._rows)

    def _add_known(self) -> None:
        name = self._known_combo.currentData()
        if not name:
            return
        spec = app_env.KNOWN_BY_NAME.get(name, {})
        self._append_row(name, str(spec.get("default", "1")), True)
        self._relayout()
        self._refresh_known_combo()
        self._on_changed()

    def _add_custom(self) -> None:
        self._append_row("", "", True)
        self._relayout()
        self._on_changed()
        # Put the cursor in the new name box so it can be typed straight away.
        row = self._rows[-1]
        if row.name_edit is not None:
            row.name_edit.setFocus()

    # ---- known-variable dropdown ------------------------------------------
    def _refresh_known_combo(self) -> None:
        """Rebuild the dropdown with the supported variables not already added,
        grouped by area. Disabled (with a note) once every one is in the list."""
        present = {r.var_name() for r in self._rows}
        self._known_combo.blockSignals(True)
        self._known_combo.clear()
        last_group = None
        count = 0
        for spec in app_env.KNOWN_VARS:
            if spec["name"] in present:
                continue
            group = spec.get("group", "")
            if last_group is not None and group != last_group:
                self._known_combo.insertSeparator(self._known_combo.count())
            last_group = group
            self._known_combo.addItem(f"{spec['name']}  ({group})", spec["name"])
            self._known_combo.setItemData(
                self._known_combo.count() - 1,
                tip_text(spec.get("summary", "")), Qt.ToolTipRole)
            count += 1
        if count == 0:
            self._known_combo.addItem(
                self.tr("All supported variables are already listed"), None)
        self._known_combo.blockSignals(False)
        self._known_combo.setEnabled(count > 0)
        self._add_btn.setEnabled(count > 0)
        self._update_known_help()

    def _update_known_help(self) -> None:
        name = self._known_combo.currentData()
        spec = app_env.KNOWN_BY_NAME.get(name or "")
        self._known_help.setText(spec.get("summary", "") if spec else "")

    # ---- persistence ------------------------------------------------------
    def _on_changed(self) -> None:
        entries = [r.to_entry() for r in self._rows]
        try:
            uc.save_app_env_vars(entries)
        except Exception as exc:
            self._notify(
                self.tr("Failed to save environment variables: {0}").format(exc),
                "warning")
            return
        self._dirty = True
        self._restart_note.setVisible(True)
        self._restart_btn.setVisible(True)
        self._validate()

    def _validate(self) -> None:
        """Flag rows startup will skip: an unusable/blocked name, a name that a
        later row repeats (the environment holds one value per name), or a value
        the environment can't store."""
        seen: dict[str, int] = {}
        for i, row in enumerate(self._rows):
            name = row.var_name()
            reason = app_env.validate_name(name) if name else ""
            if not reason:
                reason = app_env.validate_value(row.var_value())
            if not reason and name in seen:
                reason = f"{name} is listed twice - only the last one applies."
            if name:
                seen[name] = i
            row.mark_invalid(reason)

    # ---- restart ----------------------------------------------------------
    def _restart(self) -> None:
        prompt = getattr(self._window, "_prompt_env_restart", None)
        if callable(prompt):
            prompt()
            return
        request = getattr(self._window, "_request_restart", None)
        if callable(request):
            request()

    # ---- notify -----------------------------------------------------------
    def _notify(self, text: str, state: str = "info"):
        win = self._window
        if win is not None and hasattr(win, "_notify"):
            try:
                win._notify(text, state)
                return
            except Exception:
                pass
        print(f"[env-vars] {text}")
