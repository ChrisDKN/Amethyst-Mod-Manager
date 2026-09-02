"""BAIN sub-package picker - Qt port of gui/bain_dialog.py (1:1 parity).

Unlike the FOMOD wizard there are no steps/groups/conditions - just a checklist
of sub-packages to merge. Each sub-package is a distinct card (accent side-bar,
checkbox, bold display name, dim raw folder name) that recolours live to show
which selected packages win/lose shared files:

  * green - the package contributes ≥1 file (or has no files);
  * red   - every one of its files is provided by a LATER selected package, so it
            is fully overridden and installs nothing. A ⬆ "Promote" button on red
            cards unchecks the later packages overriding it so it wins.

An optional README pane sits to the left. Buttons: Select All / Select None
(left) · Cancel / Install (right).

On Install it calls ``on_done({"selected": [name, ...]})``; Cancel / close calls
``on_done(None)`` (mirrors the neutral ``resolve_bain`` contract). Default check
state = each sub-package's ``default_selected`` (00-prefixed core packages), or a
restored saved selection when provided.

All widgets are built ONCE with real parents (no per-item unparented widgets that
could flash as blank top-level windows - see the collection install-overlay fix);
recompute recolours by re-applying stylesheets, never by create/destroy.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QScrollArea, QFrame, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.mods.bain import BainSubPackage


class BainPickerView(QWidget):
    def __init__(self, subpackages: "list[BainSubPackage]", mod_root: str,
                 mod_name: str, on_done, *, readme_text: str | None = None,
                 saved_selections: dict | None = None, parent=None):
        super().__init__(parent)
        self._subpackages = list(subpackages or [])
        self._mod_root = mod_root
        self._mod_name = (mod_name or "").strip()
        self._on_done = on_done or (lambda _r: None)
        self._done = False
        self._p = active_palette()

        # Per-package checkbox + card widget bundle, keyed by pkg.name.
        self._boxes: dict[str, QCheckBox] = {}
        self._cards: dict[str, dict] = {}
        # Last-computed conflict state per package ("win"/"lose"/"neutral").
        self._states: dict[str, str] = {}

        # Per-package set of relative file keys (rel, lower-cased) so we can show
        # which selected packages win/lose shared files live - matches how
        # conflicts resolve on a case-insensitive game install. The prepare /
        # install worker pre-fills file_keys (scan_subpackage_files) so this
        # normally costs nothing; the walk is only a fallback.
        self._pkg_files: dict[str, set[str]] = {
            pkg.name: (pkg.file_keys
                       if getattr(pkg, "file_keys", None) is not None
                       else self._scan_pkg_files(pkg.path))
            for pkg in self._subpackages
        }

        saved = None
        if saved_selections and isinstance(saved_selections.get("selected"), list):
            saved = set(saved_selections["selected"])

        self._build(readme_text, saved)
        self._recompute_conflicts()

    def _c(self, k):
        return _c(self._p, k)

    @staticmethod
    def _scan_pkg_files(root: str) -> set[str]:
        """Return the set of file paths under *root*, relative and lower-cased."""
        out: set[str] = set()
        try:
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    out.add(rel.replace("\\", "/").lower())
        except OSError:
            pass
        return out

    # ------------------------------------------------------------------ UI
    def _build(self, readme_text, saved):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel(
            self.tr("{0} - BAIN package - choose sub-packages to install")
            .format(self._mod_name)
            if self._mod_name
            else self.tr("BAIN package - choose sub-packages to install"))
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:15px;")
        root.addWidget(title)

        header = QLabel(
            self.tr("Sub-packages ({0}) - tick to install · "
            "green = files used · red = fully overridden by a later package")
            .format(len(self._subpackages)))
        header.setWordWrap(True)
        header.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        root.addWidget(header)

        # Content: optional readme pane on the left, checklist on the right.
        content = QHBoxLayout()
        content.setSpacing(10)
        has_readme = bool(readme_text and readme_text.strip())
        if has_readme:
            left = QVBoxLayout()
            left.setSpacing(4)
            rlbl = QLabel(self.tr("Package readme"))
            rlbl.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:11px;")
            left.addWidget(rlbl)
            ro = QTextEdit()
            ro.setReadOnly(True)
            ro.setPlainText(readme_text)
            ro.setStyleSheet(
                f"QTextEdit {{ background:{self._c('BG_LIST')};"
                f" color:{self._c('TEXT_MAIN')};"
                f" border:1px solid {self._c('BORDER')}; border-radius:4px; }}")
            left.addWidget(ro, 1)
            content.addLayout(left, 1)

        # Checklist (scrollable) - build the card list.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QFrame()
        body.setObjectName("_BainBody")
        body.setStyleSheet(
            f"#_BainBody {{ background:{self._c('BG_PANEL')};"
            f" border:1px solid {self._c('BORDER')}; border-radius:6px; }}")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(6)
        for pkg in self._subpackages:
            checked = (pkg.name in saved) if saved is not None else pkg.default_selected
            blay.addWidget(self._make_card(pkg, bool(checked)))
        blay.addStretch(1)
        scroll.setWidget(body)
        content.addWidget(scroll, 2)
        root.addLayout(content, 1)

        # Button bar: Select All / None (left) · Cancel / Install (right).
        bar = QHBoxLayout()
        sel_all = QPushButton(self.tr("Select All"))
        sel_all.setObjectName("FormButton")
        sel_all.setCursor(Qt.PointingHandCursor)
        sel_all.clicked.connect(lambda: self._set_all(True))
        bar.addWidget(sel_all)
        sel_none = QPushButton(self.tr("Select None"))
        sel_none.setObjectName("FormButton")
        sel_none.setCursor(Qt.PointingHandCursor)
        sel_none.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(sel_none)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._cancel)
        bar.addWidget(cancel)
        ok = QPushButton(self.tr("Install"))
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._ok)
        bar.addWidget(ok)
        root.addLayout(bar)

    def _make_card(self, pkg: "BainSubPackage", checked: bool) -> QFrame:
        card = QFrame()
        card.setObjectName("_BainCard")
        clay = QHBoxLayout(card)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)

        # Accent side-bar (left edge).
        sidebar = QFrame()
        sidebar.setFixedWidth(4)
        clay.addWidget(sidebar)

        inner = QHBoxLayout()
        inner.setContentsMargins(10, 6, 8, 6)
        inner.setSpacing(10)

        cb = QCheckBox()
        cb.setChecked(checked)
        cb.setCursor(Qt.PointingHandCursor)
        cb.toggled.connect(self._recompute_conflicts)
        inner.addWidget(cb, 0, Qt.AlignVCenter)
        self._boxes[pkg.name] = cb

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        name_lbl = QLabel(pkg.display_name or pkg.name)
        name_lbl.setCursor(Qt.PointingHandCursor)
        sub_lbl = QLabel(pkg.name)
        sub_lbl.setCursor(Qt.PointingHandCursor)
        text_col.addWidget(name_lbl)
        text_col.addWidget(sub_lbl)
        inner.addLayout(text_col, 1)

        # Click name/sub label toggles the checkbox (Tk parity).
        for lbl in (name_lbl, sub_lbl):
            lbl.mousePressEvent = lambda _e, c=cb: c.toggle()

        # "Promote to winner" button (right) - shown only on red cards.
        promote = QPushButton("⬆")
        promote.setFixedSize(30, 30)
        promote.setCursor(Qt.PointingHandCursor)
        promote.setToolTip(self.tr("Use this package - turn off the later packages "
                           "overriding its files"))
        promote.clicked.connect(lambda _=False, n=pkg.name: self._promote_package(n))
        promote.hide()
        inner.addWidget(promote, 0, Qt.AlignVCenter)

        clay.addLayout(inner, 1)

        self._cards[pkg.name] = {
            "card": card, "sidebar": sidebar,
            "name": name_lbl, "sub": sub_lbl, "promote": promote,
        }
        return card

    # ------------------------------------------------------ conflict colouring
    def _recompute_conflicts(self):
        """Recolour each card based on the live selection. Install order = order of
        self._subpackages (already name-sorted, matching resolve_bain_files); a
        later selected package overrides an earlier one for any shared file."""
        selected_order = [p.name for p in self._subpackages
                          if self._boxes[p.name].isChecked()]

        # winners[key] = name of the LAST selected package providing that file.
        winners: dict[str, str] = {}
        for name in selected_order:
            for key in self._pkg_files.get(name, ()):
                winners[key] = name

        for pkg in self._subpackages:
            name = pkg.name
            checked = self._boxes[name].isChecked()
            if not checked:
                state = "neutral"
            else:
                files = self._pkg_files.get(name, set())
                if not files:
                    state = "win"  # nothing to override; treat as contributing
                elif any(winners.get(k) == name for k in files):
                    state = "win"
                else:
                    state = "lose"
            self._apply_card_state(name, state)

    def _overriders_of(self, name: str) -> list[str]:
        """Checked packages installed AFTER *name* that provide any of its files."""
        files = self._pkg_files.get(name, set())
        if not files:
            return []
        names = [p.name for p in self._subpackages]
        try:
            idx = names.index(name)
        except ValueError:
            return []
        out = []
        for later in names[idx + 1:]:
            if self._boxes[later].isChecked() and (self._pkg_files.get(later, set()) & files):
                out.append(later)
        return out

    def _promote_package(self, name: str):
        """Uncheck the later packages overriding *name* so it wins its files."""
        for overrider in self._overriders_of(name):
            self._boxes[overrider].setChecked(False)
        self._recompute_conflicts()

    def _apply_card_state(self, name: str, state: str):
        self._states[name] = state
        w = self._cards.get(name)
        if not w:
            return
        if state == "win":
            card_bg, bar_col = self._c('BG_GREEN_ROW'), self._c('TONE_GREEN')
            name_col = sub_col = self._c('BG_GREEN_TEXT')
        elif state == "lose":
            card_bg, bar_col = self._c('BG_RED_DEEP'), self._c('TONE_RED')
            name_col = sub_col = self._c('BG_RED_TEXT')
        else:  # neutral
            card_bg, bar_col = self._c('BG_CARD'), self._c('ACCENT')
            name_col, sub_col = self._c('TEXT_MAIN'), self._c('TEXT_DIM')
        w["card"].setStyleSheet(
            f"#_BainCard {{ background:{card_bg};"
            f" border:1px solid {self._c('BORDER')}; border-radius:6px; }}")
        w["sidebar"].setStyleSheet(f"background:{bar_col}; border-radius:0px;")
        w["name"].setStyleSheet(
            f"color:{name_col}; font-weight:600; font-size:13px;"
            " background:transparent;")
        w["sub"].setStyleSheet(
            f"color:{sub_col}; font-size:11px; background:transparent;")
        w["promote"].setVisible(state == "lose")

    # ------------------------------------------------------------ actions
    def _set_all(self, value: bool):
        for cb in self._boxes.values():
            cb.blockSignals(True)
            cb.setChecked(value)
            cb.blockSignals(False)
        self._recompute_conflicts()

    def _ok(self):
        if self._done:
            return
        self._done = True
        selected = [p.name for p in self._subpackages
                    if self._boxes[p.name].isChecked()]
        self._on_done({"selected": selected})

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._on_done(None)
