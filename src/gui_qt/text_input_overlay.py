"""Generic borderless in-window text-input overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, prompt, a line edit and Cancel / OK buttons. ``on_done(text)`` on
confirm, ``on_done(None)`` on cancel / Esc / backdrop click. Replaces the
native ``QInputDialog.getText`` / ``getInt`` prompts; pass a ``QIntValidator``
for numeric input, or ``suggestions=[(text, label), …]`` to add a ▾ button that
drops down pre-filled answers (mod-rename candidates — see GH#368).
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


def tr_name_source(label: str) -> str:
    """Translate a mod_name_utils suggestion source label (Utils is untranslated)."""
    _t = QCoreApplication.translate
    return {
        "Nexus file name": _t("NameSuggestions", "Nexus file name"),
        "Nexus mod name": _t("NameSuggestions", "Nexus mod name"),
        "Previously installed": _t("NameSuggestions", "Previously installed"),
        "Cleaned filename": _t("NameSuggestions", "Cleaned filename"),
        "Alternative": _t("NameSuggestions", "Alternative"),
        "Original filename": _t("NameSuggestions", "Original filename"),
    }.get(label, label)


def make_suggestion_button(on_click) -> QPushButton:
    """The ▾ button next to a name field — arrow.png tinted like a QComboBox."""
    from gui_qt.icons import icon
    from PySide6.QtCore import QSize
    btn = QPushButton()
    btn.setObjectName("FormButton")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIcon(icon("arrow.png", 12, _c(active_palette(), "DROPDOWN_ARROW")))
    btn.setIconSize(QSize(12, 12))
    # #FormButton carries 14px of side padding — in a 30px-wide button that
    # leaves no room for the glyph, so drop it for this icon-only button.
    btn.setStyleSheet("#FormButton { padding: 0px; }")
    btn.setFixedWidth(30)
    btn.setToolTip(QCoreApplication.translate("NameSuggestions",
                                              "Suggested names"))
    btn.clicked.connect(lambda: on_click(btn))
    return btn


def normalise_suggestions(suggestions) -> list[tuple[str, str]]:
    """Accept ``[name, …]`` or ``[(name, source_label), …]``; return pairs."""
    return [(s, "") if isinstance(s, str) else (s[0], s[1])
            for s in (suggestions or [])]


def fill_suggestion_menu(menu: QMenu, suggestions, apply_fn) -> None:
    """Populate *menu* with the suggestion rows, each calling ``apply_fn(name)``."""
    for text, raw_label in suggestions:
        label = tr_name_source(raw_label)
        act = menu.addAction(f"{text}    ({label})" if label else text)
        act.triggered.connect(
            lambda _checked=False, t=text: apply_fn(t))


class TextInputOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 190
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, prompt: str, on_done,
                 initial: str = "", ok_label: str = "OK", validator=None,
                 suggestions=None):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        self._suggestions = normalise_suggestions(suggestions)

        _card, v = self._make_card("TextInputCard")

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        prompt_lbl = QLabel(prompt)
        prompt_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        prompt_lbl.setWordWrap(True)
        v.addWidget(prompt_lbl)

        self._edit = QLineEdit()
        if validator is not None:
            self._edit.setValidator(validator)
        self._edit.setText(initial)
        self._edit.selectAll()
        self._edit.returnPressed.connect(self._confirm)
        if self._suggestions:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(self._edit, 1)
            row.addWidget(make_suggestion_button(self._show_suggestions))
            v.addLayout(row)
        else:
            v.addWidget(self._edit)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        ok = QPushButton(ok_label)
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, prompt, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, prompt, on_done, **kw)

    # -- internals ----------------------------------------------------------
    def _show_suggestions(self, anchor):
        menu = QMenu(self)
        fill_suggestion_menu(menu, self._suggestions, self._apply_suggestion)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _apply_suggestion(self, text: str):
        self._edit.setText(text)
        self._edit.setFocus()
        self._edit.selectAll()

    def _confirm(self):
        self._finish(self._edit.text())
