"""Qt view: Install MSCLoader (mod loader for My Summer Car).

Port of the Tk ``msc_mscloader`` plugin.  Same shape as the SRML wizard but with
an extra step that writes ``MSCFolder.txt`` (the game path) into the game root
before running ``MSCLInstaller.exe``; that file is removed again on cleanup.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QT_TRANSLATE_NOOP, Signal

from gui_qt.runtime.safe_emit import safe_emit
from wizards_qt._mod_loader_installer_view import ModLoaderInstallerView
from wizards_qt._view_base import GREEN, RED


class MSCLoaderView(ModLoaderInstallerView):
    TOOL_LABEL = "MSCLoader"
    NEXUS_URL = "https://www.nexusmods.com/mysummercar/mods/147"
    ARCHIVE_KEYWORDS = ["mscloader_msc"]
    INSTALLER_EXE = "MSCLInstaller.exe"
    PICK_TITLE = QT_TRANSLATE_NOOP("MSCLoaderView", "Select the MSCLoader archive")

    _mscfolder_status_sig = Signal(str, str)
    _mscfolder_next_sig = Signal()

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx, **_extra)
        self._mscfolder_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._mscfolder_status, t, c)))
        self._mscfolder_next_sig.connect(
            self._guard(lambda: self._goto_named("run")))

    # -- extra step: write MSCFolder.txt -----------------------------------------
    def _has_extra_step(self) -> bool:
        return True

    def _build_extra_page(self):
        page, lay = self._step_page(self.tr("Step 4: Create MSCFolder.txt"))
        self._mscfolder_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _run_extra_step(self):
        self._set_status(self._mscfolder_status, self.tr("Writing MSCFolder.txt…"))
        threading.Thread(target=self._do_mscfolder, daemon=True,
                         name="mscloader-mscfolder").start()

    def _do_mscfolder(self):
        try:
            game_path = self._game_root
            if game_path is None:
                raise RuntimeError(self.tr("Game path not configured."))
            (game_path / "MSCFolder.txt").write_text(str(game_path),
                                                     encoding="utf-8")
            self._log(f"MSCLoader Wizard: wrote {game_path / 'MSCFolder.txt'}")
            safe_emit(self._mscfolder_status_sig,
                      self.tr("Created MSCFolder.txt → {0}").format(game_path), GREEN)
            safe_emit(self._mscfolder_next_sig)
        except Exception as exc:
            safe_emit(self._mscfolder_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"MSCLoader Wizard: MSCFolder.txt error: {exc}")

    # -- cleanup: also remove MSCFolder.txt --------------------------------------
    def _extra_cleanup(self) -> int:
        game_path = self._game_root
        if game_path is None:
            return 0
        txt = game_path / "MSCFolder.txt"
        if txt.is_file():
            try:
                txt.unlink()
                return 1
            except OSError:
                pass
        return 0
