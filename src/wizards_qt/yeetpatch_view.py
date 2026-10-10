from __future__ import annotations

import threading
from copy import copy

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from Utils.wizards.yeetpatch import find_game_exe, run_update
from wizards_qt._view_base import GREEN, RED, WizardViewBase


class YeetPatchView(WizardViewBase):
    _status_sig = Signal(str)
    _done_sig = Signal(int, str)

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Update Game (YeetPatch)"))
        self._busy = False
        self._lock_key = f"yeetpatch-{id(self)}"
        self._status_sig.connect(self._guard(self._on_status))
        self._done_sig.connect(self._guard(self._on_done))
        page, lay = self._step_page(self.tr("Update Voices of the Void"))
        self._make_note(lay, self.tr(
            "Download the latest Linux YeetPatch and run it against VotV.exe.\n\n"
            "Close the game before continuing. Deployed mods will be restored "
            "first; deploy them again after updating.\n\n"
            "Follow the prompts in the terminal window. YeetPatch requires "
            "curl, jq, xxhsum, sha256sum and 7z on your system."))
        self._status = self._make_status(lay)
        lay.addStretch(1)
        self._run_btn = self._accent_btn(self.tr("Download and Run YeetPatch"))
        self._run_btn.clicked.connect(self._start)
        lay.addWidget(self._run_btn)
        self._stack.addWidget(page)
        try:
            self._make_note(lay, str(find_game_exe(game)))
        except FileNotFoundError as exc:
            self._set_status(self._status, str(exc), RED)
            self._run_btn.setEnabled(False)

    def _unlock(self):
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook:
            hook(self._lock_key, "YeetPatch", False)
        self._busy = False
        self._lock_close(False)
        self._run_btn.setEnabled(True)

    def _start(self):
        if self._busy:
            return
        self._busy = True
        self._lock_close(True)
        self._run_btn.setEnabled(False)
        self._update_game = copy(self._game)
        if self._update_game.get_deploy_active():
            if not self._run_ctx_restore(self._status, self._run, self._unlock):
                self._unlock()
            return
        self._run()

    def _run(self):
        game = self._update_game
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook:
            hook(self._lock_key, "YeetPatch", True)

        def worker():
            try:
                code = run_update(game, self._log,
                                  lambda state: safe_emit(self._status_sig, state))
                safe_emit(self._done_sig, code, "")
            except Exception as exc:
                self._log(f"YeetPatch: {exc}")
                safe_emit(self._done_sig, -1, str(exc))

        threading.Thread(target=worker, daemon=True, name="yeetpatch-update").start()

    def _on_status(self, state):
        if state == "download":
            text = self.tr("Downloading and extracting YeetPatch…")
        else:
            text = self.tr("Follow the prompts in the terminal, then press Enter to close it.")
        self._set_status(self._status, text)

    def _on_done(self, code, error):
        self._unlock()
        self._ran = True
        self._log(f"YeetPatch: finished with exit code {code}.")
        if code == 0:
            self._set_status(self._status, self.tr(
                "YeetPatch finished. Check the terminal result to confirm whether "
                "an update was applied. Deploy your mods when ready."), GREEN)
        else:
            self._set_status(self._status, self.tr("YeetPatch failed: {0}").format(
                error or self.tr("exit code {0}").format(code)), RED)
