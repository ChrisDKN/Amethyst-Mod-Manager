"""Wine DLL Overrides - a modlist-scoped tab to manage per-game Wine DLL load
orders. Qt port of the Tk ``gui/wine_dll_overrides_panel.py``, with an
improvement: the Tk panel hardcodes ``native,builtin`` for every DLL, whereas
this offers a PER-DLL load-order picker (native / builtin / native,builtin /
builtin,native / disabled).

The persistence reuses the neutral ``Utils.wine_dll_config`` +
``Utils.deploy_wine_dll`` helpers unchanged - those already write whatever value
string they're given, so per-order flexibility is a pure UI change. Overrides are
saved to config and applied to the prefix's ``user.reg`` on "Save & Apply".
"""

from __future__ import annotations

import re
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button, contrast_text
from gui_qt.wheel_guard import no_wheel
from Utils.wine_dll_config import (
    load_wine_dll_overrides, save_wine_dll_overrides,
)

# Wine DLL load orders (base_game.py wine_dll_overrides docstring). Most common
# first so a new DLL defaults to index 0.
LOAD_ORDERS = ["native,builtin", "builtin,native", "native", "builtin", "disabled"]
DEFAULT_ORDER = "native,builtin"          # new-DLL default (Tk parity)
_NAME_RE = re.compile(r"[a-z0-9_.-]+")    # valid DLL name (Tk _on_add)


class DllOverridesView(QWidget):
    """Hosted as a modlist-scoped tab. Edits an in-memory dict of {dll: order}
    and writes it (config + prefix user.reg) on Save & Apply."""

    # (n_applied, n_removed, ok) from the apply worker → UI thread.
    _apply_finished = Signal(int, int, bool)

    def __init__(self, window, game, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._log = log_fn or (lambda _m: None)

        # Load initial overrides exactly like the Tk panel: handler defaults with
        # the user's stored config on top (stored wins on value conflicts).
        handler = {}
        try:
            handler = dict(getattr(game, "wine_dll_overrides", {}) or {})
        except Exception:
            handler = {}
        stored = load_wine_dll_overrides(getattr(game, "name", "") or "")
        self._overrides: dict[str, str] = {**handler, **stored}
        self._initial_dlls: set[str] = set(self._overrides.keys())
        self._add_edit: QLineEdit | None = None

        self.setObjectName("DllOverridesView")
        self._apply_finished.connect(self._on_apply_finished)
        self._build()
        self._populate_list()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #DllOverridesView {{ background: {c('BG_DEEP')}; }}
        #DllTitleBar {{ background: {c('BG_HEADER')};
                        border-bottom: 1px solid {c('BORDER')}; }}
        #DllTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        QScrollArea {{ background: {c('BG_DEEP')}; border: none; }}
        #DllBody {{ background: {c('BG_DEEP')}; }}
        #DllRow {{ background: {c('BG_PANEL')}; }}
        #DllRow[alt="true"] {{ background: {c('BG_DEEP')}; }}
        #DllAddBar {{ background: {c('BG_HEADER')};
                      border-top: 1px solid {c('BORDER')}; }}
        #DllSaveBar {{ background: {c('BG_HEADER')};
                       border-top: 1px solid {c('BORDER')}; }}
        #DllEmpty {{ color: {c('TEXT_DIM')}; font-style: italic; }}
        #DllHint {{ color: {c('TEXT_DIM')}; }}
        #DllName {{ color: {c('TEXT_MAIN')}; }}
        #DangerButton {{ background: {c('BTN_DANGER')}; color: {contrast_text(c('BTN_DANGER'))}; border: none;
                         border-radius: 4px; padding: 2px 10px; font-size: 13px;
                         font-weight: 600; }}
        #DangerButton:hover {{ background: {c('BTN_DANGER_HOV')}; }}
        """

    def _build(self):
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title bar + Close (the scoped tab's × also dismisses it).
        bar = QWidget(); bar.setObjectName("DllTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        gname = getattr(self._game, "name", "") or ""
        title = QLabel(self.tr("Wine DLL Overrides - {0}").format(gname))
        title.setObjectName("DllTitle")
        hb.addWidget(title); hb.addStretch(1)
        close = danger_close_button()
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        # Scrollable row list.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll
        body = QWidget(); body.setObjectName("DllBody")
        self._rows_layout = QVBoxLayout(body)
        self._rows_layout.setContentsMargins(8, 8, 8, 8)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Add-a-DLL bar.
        addbar = QWidget(); addbar.setObjectName("DllAddBar")
        ab = QHBoxLayout(addbar); ab.setContentsMargins(12, 8, 12, 8)
        ab.setSpacing(8)
        self._add_edit = QLineEdit()
        self._add_edit.setPlaceholderText(self.tr("DLL name (e.g. winhttp)"))
        self._add_edit.setFixedWidth(240)
        self._add_edit.returnPressed.connect(self._on_add)
        ab.addWidget(self._add_edit)
        addb = QPushButton(self.tr("+ Add"))
        addb.setObjectName("FormButton")
        addb.setCursor(Qt.PointingHandCursor)
        addb.clicked.connect(self._on_add)
        ab.addWidget(addb)
        hint = QLabel(self.tr("New DLLs default to native,builtin"))
        hint.setObjectName("DllHint")
        ab.addWidget(hint)
        ab.addStretch(1)
        root.addWidget(addbar)

        # Save bar.
        savebar = QWidget(); savebar.setObjectName("DllSaveBar")
        sb = QHBoxLayout(savebar); sb.setContentsMargins(12, 8, 12, 8)
        sb.addStretch(1)
        save = QPushButton(self.tr("Save & Apply"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._on_save)
        sb.addWidget(save)
        root.addWidget(savebar)

    # -- list ---------------------------------------------------------------
    def _populate_list(self):
        # Leak-safe teardown (Qt won't auto-destroy like Tk). Keep the trailing
        # stretch (last item).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not self._overrides:
            empty = QLabel(self.tr("No DLL overrides configured."))
            empty.setObjectName("DllEmpty")
            empty.setAlignment(Qt.AlignCenter)
            self._rows_layout.insertWidget(0, empty)
            return
        for i, (dll, value) in enumerate(sorted(self._overrides.items())):
            self._rows_layout.insertWidget(i, self._build_row(dll, value, i))

    def _build_row(self, dll: str, value: str, i: int) -> QFrame:
        row = QFrame()
        row.setObjectName("DllRow")
        row.setProperty("alt", "true" if i % 2 else "false")
        row.setFixedHeight(38)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 4, 10, 4)
        rl.setSpacing(8)

        name = QLabel(dll)
        name.setObjectName("DllName")
        rl.addWidget(name, 1)

        combo = QComboBox()
        combo.addItems(LOAD_ORDERS)
        try:
            combo.setCurrentIndex(LOAD_ORDERS.index(value))
        except ValueError:
            # Preserve an unknown/legacy stored value rather than reset it.
            combo.addItem(value)
            combo.setCurrentIndex(combo.count() - 1)
        combo.setFixedWidth(150)
        no_wheel(combo)
        # Connect AFTER setting the index so the initial set doesn't fire.
        combo.currentTextChanged.connect(
            lambda text, d=dll: self._overrides.__setitem__(d, text))
        rl.addWidget(combo)

        remove = QPushButton("✕")
        remove.setObjectName("DangerButton")
        remove.setFixedWidth(30)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip(self.tr("Remove '{0}'").format(dll))
        remove.clicked.connect(lambda _=False, d=dll: self._on_remove(d))
        rl.addWidget(remove)

        return row

    # -- add / remove -------------------------------------------------------
    def _on_add(self):
        raw = (self._add_edit.text() if self._add_edit else "").strip().lower()
        if not raw:
            return
        if not _NAME_RE.fullmatch(raw):
            self._log("Wine DLL Overrides: invalid DLL name - only letters, "
                      "digits, underscores, dots and hyphens are allowed.")
            self._notify(self.tr("Invalid DLL name."), "warning")
            return
        if raw in self._overrides:
            self._log(f"Wine DLL Overrides: '{raw}' is already in the list.")
            self._notify(self.tr("'{0}' is already in the list.").format(raw), "warning")
            return
        self._overrides[raw] = DEFAULT_ORDER
        if self._add_edit is not None:
            self._add_edit.clear()
        self._populate_list()

    def _on_remove(self, dll: str):
        self._overrides.pop(dll, None)
        self._populate_list()

    # -- save ---------------------------------------------------------------
    def _on_save(self):
        game = self._game
        name = getattr(game, "name", "") or ""
        removed = self._initial_dlls - set(self._overrides.keys())

        # Persist config synchronously (fast JSON write).
        try:
            save_wine_dll_overrides(name, self._overrides)
        except Exception as exc:
            self._notify(self.tr("Failed to save overrides: {0}").format(exc), "warning")
            return
        self._log(f"Wine DLL Overrides: saved {len(self._overrides)} "
                  f"override(s) for {name}.")

        prefix = None
        try:
            prefix = game.get_prefix_path()
        except Exception:
            prefix = None
        if prefix is None or not prefix.is_dir():
            self._log("Wine DLL Overrides: no Proton prefix configured - "
                      "overrides saved but not applied.")
            self._notify(self.tr("Overrides saved (no prefix to apply to)."), "info")
            self._initial_dlls = set(self._overrides.keys())
            return

        # Apply/remove on a daemon worker (edits user.reg - never block the UI).
        overrides_copy = dict(self._overrides)
        removed_copy = set(removed)

        def worker():
            ok = True
            try:
                from Utils.deploy import (
                    apply_wine_dll_overrides, remove_wine_dll_overrides)
                if removed_copy:
                    self._log(f"Wine DLL Overrides: removing "
                              f"{len(removed_copy)} override(s) from prefix ...")
                    remove_wine_dll_overrides(prefix, removed_copy,
                                              log_fn=self._log)
                if overrides_copy:
                    apply_wine_dll_overrides(prefix, overrides_copy,
                                             log_fn=self._log)
            except Exception as exc:
                ok = False
                self._log(f"Wine DLL Overrides: apply failed: {exc}")
            finally:
                try:
                    self._apply_finished.emit(
                        len(overrides_copy), len(removed_copy), ok)
                except RuntimeError:
                    pass

        threading.Thread(target=worker, daemon=True,
                         name="wine-dll-apply").start()

    def _on_apply_finished(self, n_applied: int, n_removed: int, ok: bool):
        if ok:
            # Re-snapshot so a second Save computes the removed-delta correctly.
            self._initial_dlls = set(self._overrides.keys())
            self._log("Wine DLL Overrides: applied to Proton prefix.")
            self._notify(self.tr("Applied {0} override(s) to the prefix.").format(n_applied),
                         "info")
        else:
            self._notify(self.tr("Failed to apply overrides to the prefix."), "warning")

    # -- misc ---------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("dll_overrides")
                if getattr(self._window, "_dll_overrides_view", None) is self:
                    self._window._dll_overrides_view = None
                return
            except Exception:
                pass
        self.hide()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)
