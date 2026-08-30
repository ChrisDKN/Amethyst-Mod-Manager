"""Engine Fixes config editor - Qt port of wizards/engine_fixes.py.

A modlist-panel-scoped tab: a scrollable grid form of every EngineFixes.toml
key with a typed value control (bool → two radios, else line edit) and a dim
description.  Save renders the values into the managed 'EngineFixes toml' mod
(schema/parse/render in Utils/engine_fixes_config.py); Reset restores the
built-in defaults.  No per-key enable checkbox (toml keys are always present).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui_qt.theme_qt import active_palette, close_button, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
import Utils.engine_fixes_config as cfg

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class EngineFixesView(WizardViewBase):
    """Edit the Engine Fixes toml as a managed mod."""

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Engine Fixes - {0}").format(game.name))
        self._rows: dict = {}   # (section,key) -> (getter()->str, setter(str))
        self._stack.addWidget(self._build_form())

    def _build_form(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        values, source = cfg.load_initial_values(self._game)

        head = QLabel(self.tr("Editing values from {0}. Save writes the managed mod '{1}'.").format(source, cfg.MOD_NAME))
        head.setWordWrap(True)
        head.setStyleSheet(self._dim)
        outer.addWidget(head)

        self._status = QLabel("")
        self._status.setStyleSheet(self._dim)
        outer.addWidget(self._status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(4, 4, 4, 4)
        col.setSpacing(12)

        card_lay = None
        last_section = None
        for s in cfg.SCHEMA:
            first_in_card = s.section != last_section
            if first_in_card:
                card, card_lay = self._section_card(s.section)
                col.addWidget(card)
                last_section = s.section

            value = values.get(s.id, s.default)
            widget, getter, setter = self._value_control(s)
            setter(value)
            self._setting_row(card_lay, s, widget, divider=not first_in_card)
            self._rows[s.id] = (getter, setter)

        col.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        bar = QWidget()
        bh = QHBoxLayout(bar); bh.setContentsMargins(0, 4, 0, 0); bh.setSpacing(8)
        bh.addStretch(1)
        close = close_button(self.tr("Close"))
        close.clicked.connect(self._finish)
        bh.addWidget(close)
        reset = QPushButton(self.tr("Reset to defaults"))
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._on_reset)
        bh.addWidget(reset)
        save = self._accent_btn(self.tr("Save"))
        save.clicked.connect(self._on_save)
        bh.addWidget(save)
        outer.addWidget(bar)
        return page

    def _section_card(self, section: str):
        """Return (card_frame, body_layout) for a named settings section."""
        p = active_palette()
        card = QFrame()
        card.setStyleSheet(
            f"QFrame{{background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px;}}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)

        title = QLabel(self.tr("[{0}]").format(section))
        title.setStyleSheet("color:#2d8fd0; font-weight:700; border:none;")
        lay.addWidget(title)
        return card, lay

    def _setting_row(self, card_lay: QVBoxLayout, s, control: QWidget,
                     *, divider: bool = False):
        """Add one setting (name + control on the right, dim description below)
        to a section card.  When *divider* is set, a thin separator line
        precedes the row so it is visually distinct from the setting above."""
        p = active_palette()
        if divider:
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setFixedHeight(1)
            line.setStyleSheet(f"background:{_c(p,'BORDER')}; border:none;")
            card_lay.addWidget(line)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        name = QLabel(s.key)
        name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; border:none;")
        top.addWidget(name, 0, Qt.AlignVCenter)

        top.addStretch(1)
        top.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)
        card_lay.addLayout(top)

        desc = QLabel(s.desc)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"{self._dim} border:none;")
        desc.setContentsMargins(0, 0, 0, 2)
        card_lay.addWidget(desc)

    def _value_control(self, s):
        if s.kind == "bool":
            w = QWidget()
            w.setStyleSheet("border:none;")
            h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(12)
            grp = QButtonGroup(w)
            rb_t = QRadioButton(self.tr("true"))
            rb_f = QRadioButton(self.tr("false"))
            grp.addButton(rb_t); grp.addButton(rb_f)
            h.addWidget(rb_t); h.addWidget(rb_f)
            return (w,
                    lambda: "true" if rb_t.isChecked() else "false",
                    lambda v: (rb_t if str(v).lower() == "true"
                               else rb_f).setChecked(True))
        le = QLineEdit()
        le.setFixedWidth(200)
        return (le, lambda: le.text(), lambda v: le.setText(str(v)))

    def _collect_values(self):
        return {ident: getter() for ident, (getter, _s) in self._rows.items()}

    def _on_reset(self):
        defaults = cfg.schema_defaults()
        for ident, (_getter, setter) in self._rows.items():
            setter(defaults.get(ident, ""))
        self._status.setStyleSheet(self._dim)
        self._status.setText(self.tr("Form reset to built-in defaults (not yet saved)."))

    def _on_save(self):
        values = self._collect_values()
        try:
            target = cfg.save_config(self._game, values)
        except OSError as exc:
            self._status.setStyleSheet(f"color:{RED};")
            self._status.setText(self.tr("Save failed: {0}").format(exc))
            self._log(f"Engine Fixes wizard: save failed: {exc}")
            return
        self._log(f"Engine Fixes wizard: wrote {target}")
        self._status.setStyleSheet(f"color:{GREEN};")
        self._status.setText(self.tr("Saved to {0}/{1}.").format(cfg.MOD_NAME, cfg.REL_TOML_PATH))
        self._ran = True
        if getattr(self._ctx, "refresh_modlist", None):
            self._ctx.refresh_modlist()
