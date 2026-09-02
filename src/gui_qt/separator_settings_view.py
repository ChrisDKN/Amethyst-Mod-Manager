"""Separator Settings overlay - per-separator colour + deployment override.

Opens as a plugins-panel-scoped tab (covers the whole plugins panel, like Change
Version). Qt port of the Tk gui/dialogs.py SepSettingsPanel, merged with the
SepColorPanel colour picker: the separator colour is edited here instead of via a
separate "Change separator color" menu item.

Persistence is neutral (Utils.profiles.state read/write helpers), keyed by the
separator's internal `..._separator` name - the same shape the Tk app writes, so
existing data round-trips and the deploy pipeline (Utils.deployment.shared) picks the
paths up unchanged.

on_save(color: str | None, deploy: dict | None) is called on Save:
  color  - "#rrggbb" or None (reset to default / no colour)
  deploy - {"path","raw","mode","merge"} or None (no override)
on_close() is called on Cancel, Save, or the ✕ button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QCheckBox, QRadioButton, QButtonGroup, QFrame,
)

from gui_qt.theme_qt import active_palette, _c, close_button


def _hline(color: str) -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{color}; border:none;")
    return f


class SeparatorSettingsView(QWidget):
    """Scoped-tab body for editing one separator's colour + deploy override."""

    # pick_folder's callback fires on the portal WORKER thread; marshal the
    # result to the GUI thread via this Signal before touching any widget.
    _folder_picked = Signal(object)

    def __init__(self, sep_name: str, current_color: str | None,
                 current_deploy: dict | None, on_save, on_close):
        super().__init__()
        self._folder_picked.connect(self._on_folder_picked)
        self._sep_name = sep_name
        self._color: str | None = current_color or None
        self._deploy = dict(current_deploy or {})
        self._on_save = on_save or (lambda color, deploy: None)
        self._on_close = on_close or (lambda: None)

        self.setObjectName("SeparatorSettingsView")
        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        p = active_palette()
        bg_panel = _c(p, "BG_PANEL")
        bg_header = _c(p, "BG_HEADER")
        bg_deep = _c(p, "BG_DEEP")
        text_main = _c(p, "TEXT_MAIN")
        text_sep = _c(p, "TEXT_SEP")
        text_dim = _c(p, "TEXT_DIM")
        border = _c(p, "BORDER")
        accent = _c(p, "ACCENT")

        self.setStyleSheet(f"""
            #SeparatorSettingsView {{ background:{bg_panel}; }}
            QLabel#SectionLabel {{ color:{text_sep}; font-weight:bold; }}
            QLabel#HelpLabel {{ color:{text_dim}; }}
            QLineEdit {{ background:{bg_deep}; color:{text_main};
                         border:1px solid {border}; border-radius:4px; padding:4px; }}
            QCheckBox, QRadioButton {{ color:{text_main}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- title bar ----
        title_bar = QWidget()
        title_bar.setFixedHeight(36)
        title_bar.setStyleSheet(f"background:{bg_header};")
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(12, 0, 4, 0)
        title = QLabel(self.tr("Separator Settings - {0}").format(self._display_name()))
        title.setStyleSheet(f"color:{text_main}; font-weight:bold;")
        tb.addWidget(title, 1)
        close_btn = close_button(pal=p)
        close_btn.clicked.connect(self._on_close)
        tb.addWidget(close_btn)
        root.addWidget(title_bar)
        root.addWidget(_hline(border))

        # ---- scrollable content ----
        content = QWidget()
        c = QVBoxLayout(content)
        c.setContentsMargins(16, 16, 16, 16)
        c.setSpacing(4)

        # Colour section (merged from the old "Change separator color").
        c.addWidget(self._section_label(self.tr("Separator Color")))
        color_row = QHBoxLayout()
        color_row.setSpacing(6)
        self._swatch = QFrame()
        self._swatch.setFixedSize(48, 28)
        color_row.addWidget(self._swatch)
        self._hex = QLineEdit()
        self._hex.setPlaceholderText(self.tr("#rrggbb"))
        self._hex.setMaximumWidth(120)
        self._hex.editingFinished.connect(self._on_hex_typed)
        color_row.addWidget(self._hex)
        choose = QPushButton(self.tr("Choose colour…"))
        choose.setCursor(Qt.PointingHandCursor)
        choose.clicked.connect(self._on_choose_colour)
        color_row.addWidget(choose)
        reset = QPushButton(self.tr("Reset to default"))
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(self._on_reset_colour)
        color_row.addWidget(reset)
        color_row.addStretch(1)
        c.addLayout(color_row)
        c.addWidget(self._help_label(
            self.tr("Custom background colour for this separator row. "
            "Reset uses the theme default.")))
        self._sync_colour_ui()

        c.addSpacing(12)
        c.addWidget(_hline(border))
        c.addSpacing(12)

        # Deployment location.
        c.addWidget(self._section_label(self.tr("Deployment Location")))
        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self._path = QLineEdit(self._deploy.get("path", "") or "")
        path_row.addWidget(self._path, 1)
        browse = QPushButton(self.tr("Browse"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._on_browse)
        path_row.addWidget(browse)
        clear = QPushButton(self.tr("Clear"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(lambda: self._path.setText(""))
        path_row.addWidget(clear)
        c.addLayout(path_row)
        c.addWidget(self._help_label(
            self.tr("Deploy this separator's mods here instead of the game directory.")))

        c.addSpacing(12)
        c.addWidget(_hline(border))
        c.addSpacing(12)

        # Ignore deployment rules.
        self._raw = QCheckBox(self.tr("Ignore deployment rules"))
        self._raw.setChecked(bool(self._deploy.get("raw", False)))
        c.addWidget(self._raw)
        c.addWidget(self._help_label(self.tr("Skip routing rules; deploy files as-is.")))

        c.addSpacing(12)
        c.addWidget(_hline(border))
        c.addSpacing(12)

        # File transfer method.
        c.addWidget(self._section_label(self.tr("File Transfer Method")))
        self._mode_group = QButtonGroup(self)
        mode = (self._deploy.get("mode", "") or "").strip().lower()
        if mode not in ("hardlink", "symlink"):
            mode = "default"
        self._mode_buttons: dict[str, QRadioButton] = {}
        for val, lbl in (
            ("default", "Default (use global setting)"),
            ("hardlink", "Hardlink"),
            ("symlink", "Symlink"),
        ):
            rb = QRadioButton(lbl)
            rb.setChecked(val == mode)
            self._mode_group.addButton(rb)
            self._mode_buttons[val] = rb
            c.addWidget(rb)
        c.addWidget(self._help_label(
            self.tr("Override the global deploy mode. Hardlink falls back to symlink if "
            "unsupported.")))

        c.addSpacing(12)
        c.addWidget(_hline(border))
        c.addSpacing(12)

        # Merge folders.
        self._merge = QCheckBox(self.tr("Merge folders with target"))
        self._merge.setChecked(bool(self._deploy.get("merge", False)))
        c.addWidget(self._merge)
        c.addWidget(self._help_label(
            self.tr("Merge mod folders into existing ones instead of replacing them.")))

        c.addStretch(1)
        root.addWidget(content, 1)

        # ---- button bar ----
        root.addWidget(_hline(border))
        bar = QWidget()
        bar.setStyleSheet(f"background:{bg_panel};")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._on_close)
        bl.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setCursor(Qt.PointingHandCursor)
        save.setStyleSheet(
            f"QPushButton {{ background:{accent}; color:{_c(p,'TEXT_ON_ACCENT')};"
            f" border:none; border-radius:4px; padding:6px 16px; font-weight:bold; }}")
        save.clicked.connect(self._on_save_click)
        bl.addWidget(save)
        root.addWidget(bar)

    # ------------------------------------------------------------------
    def _display_name(self) -> str:
        suffix = "_separator"
        return self._sep_name[:-len(suffix)] if self._sep_name.endswith(suffix) \
            else self._sep_name

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SectionLabel")
        return lbl

    def _help_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("HelpLabel")
        lbl.setWordWrap(True)
        return lbl

    # ---- colour handling -------------------------------------------------
    def _sync_colour_ui(self):
        """Repaint the swatch + hex field from self._color."""
        p = active_palette()
        if self._color:
            self._swatch.setStyleSheet(
                f"background:{self._color};"
                f" border:1px solid {_c(p, 'BORDER_FAINT')};")
            if self._hex.text().strip().lower() != self._color.lower():
                self._hex.setText(self._color)
        else:
            self._swatch.setStyleSheet(
                f"background:{_c(p, 'BG_SEP')};"
                f" border:1px dashed {_c(p, 'TEXT_DIM')};")
            self._hex.clear()

    def _on_choose_colour(self):
        initial = QColor(self._color) if self._color else QColor(
            _c(active_palette(), "BG_SEP"))

        def _picked(chosen):
            if chosen is not None and chosen.isValid():
                self._color = chosen.name()  # "#rrggbb"
                self._sync_colour_ui()

        from gui_qt.color_picker_overlay import ColorPickerOverlay
        ColorPickerOverlay.show_over(self, "Separator colour", initial,
                                     _picked)

    def _on_reset_colour(self):
        self._color = None
        self._sync_colour_ui()

    def _on_hex_typed(self):
        raw = self._hex.text().strip().lstrip("#")
        if len(raw) == 6:
            try:
                int(raw, 16)
                self._color = "#" + raw.lower()
            except ValueError:
                pass
        elif not raw:
            self._color = None
        self._sync_colour_ui()

    # ---- deploy handling -------------------------------------------------
    def _on_browse(self):
        from Utils.ui.portal import pick_folder
        pick_folder("Select deployment directory",
                    lambda chosen: self._folder_picked.emit(chosen))

    def _on_folder_picked(self, chosen):
        if chosen is not None:
            self._path.setText(str(chosen))

    def _current_deploy(self) -> dict | None:
        path = self._path.text().strip()
        raw = self._raw.isChecked()
        merge = self._merge.isChecked()
        mode = ""
        for val, rb in self._mode_buttons.items():
            if rb.isChecked():
                mode = "" if val == "default" else val
                break
        if path or raw or mode or merge:
            return {"path": path, "raw": raw, "mode": mode, "merge": merge}
        return None

    # ---- save / cancel ---------------------------------------------------
    def _on_save_click(self):
        self._on_save(self._color, self._current_deploy())
        self._on_close()
