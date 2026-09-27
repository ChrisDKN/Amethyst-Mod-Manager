"""Edit saved Wine DLL overrides and show the game's current prefix overrides."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, close_button, contrast_text
from gui_qt.wheel_guard import no_wheel
from Utils.wine.dll_config import (
    _merge_overrides, load_wine_dll_overrides, load_removed_wine_dll_overrides,
    read_prefix_wine_dll_overrides, save_wine_dll_overrides,
)
from Utils.wine.registry import normalize_pfx

# Wine DLL load orders (base_game.py wine_dll_overrides docstring). Most common
# first so a new DLL defaults to index 0.
LOAD_ORDERS = ["native,builtin", "builtin,native", "native", "builtin", ""]
DEFAULT_ORDER = "native,builtin"          # new-DLL default (Tk parity)
_NAME_RE = re.compile(r"\*?[a-z0-9_][a-z0-9_.-]*")


class DllOverridesView(QWidget):
    """Hosted as a modlist-scoped tab. Edits an in-memory dict of {dll: order}
    and writes it (config + prefix user.reg) on Save & Apply."""

    _apply_finished = Signal(int, str)

    def __init__(self, window, game, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._log = log_fn or (lambda _m: None)

        self._overrides: dict[str, str] = {}
        self._edited: dict[str, str] = {}
        self._deleted: set[str] = set()
        self._prefix_overrides: dict[str, str] = {}
        self._stored_overrides: dict[str, str] = {}
        self._source_stamp = None
        self._applying = False
        self._add_edit: QLineEdit | None = None

        self.setObjectName("DllOverridesView")
        self._apply_finished.connect(self._on_apply_finished)
        self._build()
        self.refresh()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_visible)
        self._refresh_timer.start()

    def _prefix_path(self) -> Path | None:
        try:
            prefix = self._game.get_prefix_path()
            return normalize_pfx(Path(prefix)) if prefix else None
        except Exception:
            return None

    def refresh(self):
        if self._applying:
            return
        name = getattr(self._game, "name", "") or ""
        prefix = self._prefix_path()
        try:
            handler = _merge_overrides(dict(getattr(self._game, "wine_dll_overrides", {}) or {}))
        except Exception:
            handler = {}
        stored = load_wine_dll_overrides(name)
        removed = load_removed_wine_dll_overrides(name)
        registry_stamp = None
        if prefix is not None:
            try:
                stat = (prefix / "user.reg").stat()
                registry_stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except OSError:
                pass
        stamp = (prefix, registry_stamp, handler, stored, removed)
        if stamp == self._source_stamp:
            return
        self._source_stamp = stamp
        self._stored_overrides = stored
        self._prefix_overrides = read_prefix_wine_dll_overrides(prefix)
        configured = {dll: mode for dll, mode in _merge_overrides(handler, stored).items()
                      if dll not in removed}
        overrides = _merge_overrides(configured, self._prefix_overrides, self._edited)
        self._overrides = {dll: mode for dll, mode in overrides.items()
                           if dll not in self._deleted}
        self._populate_list()

    def _refresh_visible(self):
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def tab_closing(self):
        self._refresh_timer.stop()

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
        close = close_button()
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        hint = QLabel(self.tr("Prefix overrides refresh automatically. Saved overrides and game defaults are also shown. Removing an entry prevents deployment from adding it again."))
        hint.setObjectName("DllHint")
        hint.setWordWrap(True)
        hint.setContentsMargins(12, 8, 12, 8)
        root.addWidget(hint)

        # Scrollable row list.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll
        body = QWidget(); body.setObjectName("DllBody")
        self._editor_body = body
        self._rows_layout = QVBoxLayout(body)
        self._rows_layout.setContentsMargins(8, 8, 8, 8)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Add-a-DLL bar.
        addbar = QWidget(); addbar.setObjectName("DllAddBar")
        self._add_bar = addbar
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
        self._save_button = save
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

        source = QLabel(self.tr("Unsaved") if dll in self._edited else
                        self.tr("Prefix") if dll in self._prefix_overrides else
                        self.tr("Saved") if dll in self._stored_overrides else
                        self.tr("Game default"))
        source.setObjectName("DllHint")
        rl.addWidget(source)

        combo = QComboBox()
        for mode in LOAD_ORDERS:
            combo.addItem(mode or self.tr("disabled"), mode)
        if value in LOAD_ORDERS:
            combo.setCurrentIndex(LOAD_ORDERS.index(value))
        else:
            combo.addItem(value, value)
            combo.setCurrentIndex(combo.count() - 1)
        combo.setFixedWidth(150)
        no_wheel(combo)
        # Connect AFTER setting the index so the initial set doesn't fire.
        combo.currentIndexChanged.connect(
            lambda _index, d=dll, cb=combo, label=source:
                self._on_order_changed(d, cb.currentData(), label))
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
    def _on_order_changed(self, dll: str, mode: str, source: QLabel):
        self._overrides[dll] = mode
        self._edited[dll] = mode
        source.setText(self.tr("Unsaved"))

    def _on_add(self):
        raw = (self._add_edit.text() if self._add_edit else "").strip().lower()
        if raw.endswith(".dll"):
            raw = raw[:-4]
        if not raw:
            return
        if not _NAME_RE.fullmatch(raw):
            self._log("Wine DLL Overrides: invalid DLL name - only letters, "
                      "digits, underscores, dots, hyphens and a leading * are allowed.")
            self._notify(self.tr("Invalid DLL name."), "warning")
            return
        if raw in self._overrides:
            self._log(f"Wine DLL Overrides: '{raw}' is already in the list.")
            self._notify(self.tr("'{0}' is already in the list.").format(raw), "warning")
            return
        self._overrides[raw] = DEFAULT_ORDER
        self._edited[raw] = DEFAULT_ORDER
        self._deleted.discard(raw)
        if self._add_edit is not None:
            self._add_edit.clear()
        self._populate_list()

    def _on_remove(self, dll: str):
        self._overrides.pop(dll, None)
        self._edited.pop(dll, None)
        self._deleted.add(dll)
        self._populate_list()

    # -- save ---------------------------------------------------------------
    def _on_save(self):
        if self._applying:
            return
        self.refresh()
        game = self._game
        name = getattr(game, "name", "") or ""
        removed = set(self._deleted)
        stored = _merge_overrides(self._stored_overrides, self._edited)
        stored = {dll: mode for dll, mode in stored.items() if dll not in removed}
        suppressed = (load_removed_wine_dll_overrides(name) | removed) - self._edited.keys()

        # Persist config synchronously (fast JSON write).
        try:
            save_wine_dll_overrides(name, stored, removed=suppressed)
        except Exception as exc:
            self._notify(self.tr("Failed to save overrides: {0}").format(exc), "warning")
            return
        self._log(f"Wine DLL Overrides: saved {len(stored)} "
                  f"override(s) for {name}.")

        prefix = self._prefix_path()
        if prefix is None or not prefix.is_dir():
            self._log("Wine DLL Overrides: no Proton prefix configured - "
                      "overrides saved but not applied.")
            self._notify(self.tr("Overrides saved (no prefix to apply to)."), "info")
            self._finish_saved()
            return

        # Apply/remove on a daemon worker (edits user.reg - never block the UI).
        overrides_copy = {dll: mode for dll, mode in self._overrides.items()
                          if dll not in suppressed}
        removed_copy = set(suppressed)
        self._set_applying(True)

        def worker():
            error = ""
            try:
                from Utils.processes.game import matching_pids, prefix_markers
                from Utils.deployment import (
                    apply_wine_dll_overrides, remove_wine_dll_overrides)
                pids = matching_pids(prefix_markers(prefix))
                if pids is None:
                    raise RuntimeError(self.tr("Could not check whether this prefix is in use."))
                if pids:
                    raise RuntimeError(self.tr("Close the game and tools using this prefix, then apply again."))
                if removed_copy:
                    self._log(f"Wine DLL Overrides: removing "
                              f"{len(removed_copy)} override(s) from prefix ...")
                    if not remove_wine_dll_overrides(prefix, removed_copy,
                                                    log_fn=self._log):
                        raise OSError(self.tr("Could not update the prefix registry. See the log for details."))
                if overrides_copy:
                    if not apply_wine_dll_overrides(prefix, overrides_copy,
                                                   log_fn=self._log):
                        raise OSError(self.tr("Could not update the prefix registry. See the log for details."))
            except Exception as exc:
                error = str(exc)
                self._log(f"Wine DLL Overrides: apply failed: {exc}")
            finally:
                try:
                    self._apply_finished.emit(
                        len(overrides_copy), error)
                except RuntimeError:
                    pass

        threading.Thread(target=worker, daemon=True,
                         name="wine-dll-apply").start()

    def _set_applying(self, applying: bool):
        self._applying = applying
        self._editor_body.setEnabled(not applying)
        self._add_bar.setEnabled(not applying)
        self._save_button.setEnabled(not applying)

    def _finish_saved(self):
        self._edited.clear()
        self._deleted.clear()
        self._source_stamp = None
        self.refresh()

    def _on_apply_finished(self, n_applied: int, error: str):
        self._set_applying(False)
        if not error:
            self._finish_saved()
            self._log("Wine DLL Overrides: applied to Proton prefix.")
            self._notify(self.tr("Applied {0} override(s) to the prefix.").format(n_applied),
                         "info")
        else:
            self._notify(self.tr("Overrides saved, but could not be applied: {0}").format(error), "warning")

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
