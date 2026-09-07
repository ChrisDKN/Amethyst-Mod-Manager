from __future__ import annotations

from Utils.bethesda.oblivion4gb import BACKUP_NAME, EXE_NAME
from wizards_qt.fnv_4gb_view import Fnv4GbView


class Oblivion4GbView(Fnv4GbView):
    _exe_name = EXE_NAME
    _backup_name = BACKUP_NAME
    _thread_prefix = "oblivion4gb"
    _log_prefix = "Oblivion 4GB patch wizard"

    def _heading_text(self) -> str:
        return self.tr("Oblivion 4GB Patch")

    def _note_text(self) -> str:
        return self.tr(
            "Patches Oblivion.exe so the 32-bit game can use up to 4 GB of "
            "memory on a 64-bit system.\n\nThe patch is applied natively and "
            "does not require Wine or an external patcher. It does not install "
            "or load OBSE.\n\nThe original exe is kept as {0}."
        ).format(self._backup_name)

    def _inspect_game_exe(self, game_root):
        from Utils.bethesda.oblivion4gb import inspect_exe
        return inspect_exe(game_root)

    def _apply_game_patch(self, game_root):
        from Utils.bethesda.oblivion4gb import apply_4gb_patch
        return apply_4gb_patch(game_root)

    def _restore_game_backup(self, game_root):
        from Utils.bethesda.oblivion4gb import restore_backup
        restore_backup(game_root)

    def _unknown_status_text(self, info: dict) -> str:
        detail = info.get("error") or self.tr("The PE header could not be read.")
        return self.tr(
            "{0} is not a supported Windows executable.\n{1}\n"
            "Verify the game files in Steam and try again."
        ).format(self._exe_name, detail)

    def _patch_log_detail(self, _result) -> str:
        return ""

    def _after_restore_log(self):
        pass
