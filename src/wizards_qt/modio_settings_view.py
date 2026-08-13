"""mod.io API access wizard (Baldur's Gate 3).

Paste the assigned API path and free read-only key, test them and store them
(system keyring / encrypted file). The mod.io logic lives in the BG3
game folder (Games/Baldur's Gate 3/modio_*.py); that folder isn't
importable by dotted path (space in the name), so modules load by file path.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_KEY_URL = "https://mod.io/me/access"
_GREEN_OK = "#5fb95f"
_RED_ERR = "#d65c5c"


def _load_bg3_modio(stem: str):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path."""
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent
               / "Games" / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class ModioSettingsView(WizardViewBase):
    """Enter, test and store the mod.io API path and key."""

    _save_done_sig = Signal(str, str, bool, str)  # (key, API path, ok, err)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx, title=self.tr("mod.io API Key"))
        self._busy = False
        self._modio_key = _load_bg3_modio("modio_key")

        self._save_done_sig.connect(self._guard(self._save_done))
        self._stack.addWidget(self._build_page())

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("mod.io API Key"))
        self._make_note(lay, (
            self.tr("Paste your mod.io API path and read-only API key to enable update checks\n"
            "for Baldur's Gate 3 mods installed manually from mod.io.\n\n"
            "Both values are shown on the linked API Access page. The key is\n"
            "read-only and stored securely (system keyring,\n"
            "or an encrypted file when no keyring is available).")))

        link = QPushButton(self.tr("Get my API key (mod.io)"))
        link.setCursor(Qt.PointingHandCursor)
        link.clicked.connect(lambda: self._open_url(_KEY_URL))
        lay.addWidget(link, 0, Qt.AlignHCenter)

        self._api_path_entry = QLineEdit()
        self._api_path_entry.setPlaceholderText(
            self.tr("API path (for example https://u-123.modapi.io/v1)"))
        self._api_path_entry.setMinimumWidth(480)

        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("mod.io API key"))
        self._entry.setMinimumWidth(480)
        try:
            existing, existing_path = self._modio_key.load_modio_credentials()
        except Exception:
            existing, existing_path = "", ""
        if existing_path:
            self._api_path_entry.setText(existing_path)
        if existing:
            self._entry.setText(existing)
        lay.addWidget(self._api_path_entry, 0, Qt.AlignHCenter)
        lay.addWidget(self._entry, 0, Qt.AlignHCenter)

        self._status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        clear = QPushButton(self.tr("Clear key"))
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self._on_clear)
        rh.addWidget(clear)
        self._save_btn = self._accent_btn(self.tr("Test && Save"))
        self._save_btn.clicked.connect(self._on_save)
        rh.addWidget(self._save_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _set_result(self, text: str, ok: "bool | None" = None):
        color = ""
        if ok is True:
            color = _GREEN_OK
        elif ok is False:
            color = _RED_ERR
        self._set_status(self._status, text, color)

    # ---- actions ----------------------------------------------------------------
    def _on_save(self):
        if self._busy:
            return
        key = self._entry.text().strip()
        api_path = self._api_path_entry.text().strip()
        if not key or not api_path:
            self._set_result(self.tr("Enter the API path and key first."), ok=False)
            return
        self._busy = True
        self._save_btn.setEnabled(False)
        self._set_result(self.tr("Testing key…"))

        def worker():
            ok = False
            err = ""
            normalized_path = api_path
            try:
                modio_api = _load_bg3_modio("modio_api")
                normalized_path = modio_api.normalize_api_path(api_path)
                ok = modio_api.ModioAPI(key, normalized_path).test_key()
            except Exception as e:
                err = str(e)
            safe_emit(self._save_done_sig, key, normalized_path, ok, err)

        threading.Thread(target=worker, daemon=True, name="modio-test").start()

    def _save_done(self, key: str, api_path: str, ok: bool, err: str):
        self._busy = False
        self._save_btn.setEnabled(True)
        if not ok:
            msg = (self.tr("Key rejected by mod.io.") if not err
                   else self.tr("Key test failed: {0}").format(err))
            self._set_result(msg, ok=False)
            return
        try:
            self._modio_key.save_modio_credentials(key, api_path)
            self._set_result(
                self.tr("Key saved. mod.io update checks are now enabled."),
                ok=True)
            self._log("mod.io: API key saved.")
        except Exception as e:
            self._set_result(self.tr("Could not save key: {0}").format(e), ok=False)

    def _on_clear(self):
        try:
            self._modio_key.clear_modio_key()
            self._api_path_entry.clear()
            self._entry.clear()
            self._set_result(self.tr("Key cleared."))
            self._log("mod.io: API key cleared.")
        except Exception as e:
            self._set_result(self.tr("Could not clear key: {0}").format(e), ok=False)
