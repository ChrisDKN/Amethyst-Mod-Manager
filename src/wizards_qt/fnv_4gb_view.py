"""FNV 4GB Patch wizard - Qt port of wizards/fnv_4gb_patch.py.

Single page: hashes FalloutNV.exe (worker), reports its patch state, and
offers Apply (large-address-aware + NVSE-loader byte patches, original kept
as FalloutNV_backup.exe) / Restore Backup.  Patch core lives in
Utils/bethesda/fnv4gb.py (shared with the Tk wizard).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import button_qss
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bethesda.fnv4gb import BACKUP_NAME, EXE_NAME

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_AMBER = "#e0a06c"


class Fnv4GbView(WizardViewBase):
    """Apply or revert the FNV 4GB patch."""

    _exe_name = EXE_NAME
    _backup_name = BACKUP_NAME
    _thread_prefix = "fnv4gb"
    _log_prefix = "4GB patch wizard"

    _exe_status_sig = Signal(str, str)
    _backup_status_sig = Signal(str, str)
    _buttons_sig = Signal(bool, bool)     # (apply enabled, restore enabled)
    _refresh_sig = Signal()               # worker → re-run the state scan
    _redeploy_sig = Signal()              # worker → redeploy after restore-first apply

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("4GB Patch - {0}").format(game.name))
        self._game_root = game.get_game_path()
        self._busy = False

        self._exe_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._exe_status, t, c)))
        self._backup_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._backup_status, t, c)))
        self._buttons_sig.connect(self._guard(self._set_buttons))
        self._refresh_sig.connect(self._guard(self._refresh))
        self._redeploy_sig.connect(self._guard(self._start_redeploy))

        self._stack.addWidget(self._build_page())
        self._refresh()

    def _heading_text(self) -> str:
        return self.tr("Fallout New Vegas 4GB Patch")

    def _note_text(self) -> str:
        return self.tr(
            'Patches FalloutNV.exe so the game can use 4 GB of memory\n'
            'and loads NVSE automatically at startup.\n\n'
            'Under Proton this mostly silences in-game warnings from mods\n'
            'that check for the patch, but it is safe and recommended.\n\n'
            'While "Apply the 4GB patch automatically" is enabled in\n'
            'Configure Game (the default), deploy applies the patch and\n'
            'restore reverts it - disable that option to manage the patch\n'
            'manually here.\n\nThe original exe is kept as {0}.'
        ).format(self._backup_name)

    def _inspect_game_exe(self, game_root):
        from Utils.bethesda.fnv4gb import inspect_exe
        return inspect_exe(game_root)

    def _apply_game_patch(self, game_root):
        from Utils.bethesda.fnv4gb import apply_4gb_patch
        return apply_4gb_patch(game_root)

    def _restore_game_backup(self, game_root):
        from Utils.bethesda.fnv4gb import restore_backup
        restore_backup(game_root)

    def _unknown_status_text(self, info: dict) -> str:
        return self.tr(
            "Unrecognised {0} version.\nSHA-1: {1}\n"
            "It may already be modified. Verify game files in "
            "Steam/Heroic to get a clean exe, then try again."
        ).format(self._exe_name, info.get("hash"))

    def _patch_log_detail(self, result) -> str:
        return f" ({result} version)" if result else ""

    def _after_restore_log(self):
        if getattr(self._game, "auto_4gb_patch", False):
            self._log(
                f"{self._log_prefix}: note - automatic patching is enabled, "
                "so the next deploy will re-apply the patch (disable it in "
                "Configure Game to manage the patch manually)."
            )

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self._heading_text())
        self._make_note(lay, self._note_text())
        lay.addSpacing(8)
        self._exe_status = self._make_status(lay)
        self._backup_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._restore_btn = QPushButton(self.tr("Restore Backup"))
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setEnabled(False)
        self._restore_btn.setStyleSheet(
            button_qss("BTN_WARN_ORANGE", padding="8px 20px"))
        self._restore_btn.clicked.connect(self._on_restore)
        rh.addWidget(self._restore_btn)
        self._apply_btn = self._accent_btn(self.tr("Apply 4GB Patch"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        rh.addWidget(self._apply_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _set_buttons(self, apply_ok: bool, restore_ok: bool):
        self._apply_btn.setEnabled(apply_ok)
        self._restore_btn.setEnabled(restore_ok)

    # ---- state refresh (worker re-hashes the exe) -----------------------------
    def _refresh(self):
        game_root = self._game_root
        if game_root is None or not game_root.is_dir():
            self._set_status(self._exe_status,
                             self.tr("Game path is not configured."), RED)
            return
        self._set_status(self._exe_status,
                         self.tr("Checking {0}…").format(self._exe_name))
        self._set_buttons(False, False)

        def worker():
            try:
                info = self._inspect_game_exe(game_root)
            except Exception as exc:
                safe_emit(self._exe_status_sig, self.tr("Error reading exe: {0}").format(exc), RED)
                return
            state = info["state"]
            if state == "missing":
                safe_emit(self._exe_status_sig,
                          self.tr("{0} not found in the game folder.").format(self._exe_name), RED)
            elif state == "patched":
                safe_emit(self._exe_status_sig,
                          self.tr("{0} is already 4GB patched.").format(self._exe_name), GREEN)
            elif state == "patchable":
                variant = info.get("variant")
                if variant:
                    message = self.tr(
                        "Unpatched {0} detected ({1} version) - ready to patch."
                    ).format(self._exe_name, variant)
                else:
                    message = self.tr(
                        "Unpatched {0} detected - ready to patch."
                    ).format(self._exe_name)
                safe_emit(self._exe_status_sig, message, "")
            else:
                safe_emit(self._exe_status_sig, self._unknown_status_text(info),
                          _AMBER)
            if info["backup_exists"]:
                safe_emit(self._backup_status_sig,
                          self.tr("Backup found: {0}").format(self._backup_name), "")
            else:
                safe_emit(self._backup_status_sig, self.tr("No backup present."), "")
            safe_emit(self._buttons_sig,
                      state == "patchable", info["backup_exists"])

        threading.Thread(target=worker, daemon=True,
                         name=self._thread_prefix + "-scan").start()

    # ---- actions ----------------------------------------------------------------
    def _on_apply(self):
        if self._busy or self._game_root is None:
            return
        self._busy = True
        self._set_buttons(False, False)
        # Patching creates a backup in the game root. If a profile is deployed,
        # that file is absent from the deploy snapshot, so the
        # next restore would sweep it into overwrite/ as a runtime file -
        # restore the modlist first, patch the vanilla root, then redeploy.
        if getattr(self._game, "get_deploy_active", lambda: False)():
            self._log(f"{self._log_prefix}: modlist is deployed - restoring "
                      "before patching (redeploys afterwards).")

            def _restore_failed():
                self._busy = False
                self._refresh()

            if not self._run_ctx_restore(
                    self._exe_status,
                    lambda: self._start_apply(redeploy=True),
                    _restore_failed):
                self._busy = False
            return
        self._start_apply()

    def _start_apply(self, redeploy: bool = False):
        self._set_status(self._exe_status,
                         self.tr("Patching {0}…").format(self._exe_name))
        game_root = self._game_root

        def worker():
            try:
                result = self._apply_game_patch(game_root)
                detail = self._patch_log_detail(result)
                self._log(f"{self._log_prefix}: patched {self._exe_name}{detail}, "
                          f"original saved as {self._backup_name}.")
            except Exception as exc:
                self._log(f"{self._log_prefix}: patch failed: {exc}")
                safe_emit(self._exe_status_sig, self.tr("Patch failed: {0}").format(exc), RED)
            finally:
                self._busy = False
                # Redeploy (successful or not) puts the restored modlist back;
                # the new deploy snapshot then records the backup exe so later
                # restores leave it in the game root.
                if redeploy:
                    safe_emit(self._redeploy_sig)
                else:
                    safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True,
                         name=self._thread_prefix + "-apply").start()

    def _start_redeploy(self):
        if not self._run_ctx_deploy(self._exe_status, self._refresh, self._refresh):
            self._refresh()

    def _on_restore(self):
        if self._busy or self._game_root is None:
            return
        self._busy = True
        self._set_buttons(False, False)
        self._set_status(
            self._exe_status,
            self.tr("Restoring original {0}…").format(self._exe_name))
        game_root = self._game_root

        def worker():
            try:
                self._restore_game_backup(game_root)
                self._log(f"{self._log_prefix}: restored {self._exe_name} "
                          f"from {self._backup_name}.")
                self._after_restore_log()
            except Exception as exc:
                self._log(f"{self._log_prefix}: restore failed: {exc}")
                safe_emit(self._exe_status_sig, self.tr("Restore failed: {0}").format(exc), RED)
            finally:
                self._busy = False
                safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True,
                         name=self._thread_prefix + "-restore").start()
