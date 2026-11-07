from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot, QCoreApplication

from gui_qt.dialogs.confirm import ConfirmOverlay
from gui_qt.runtime.worker import run_in_worker


def _tr(text):
    return QCoreApplication.translate("DeploymentRepair", text)


class _DeletedProfileRepair(QObject):
    finished = Signal(object)

    def __init__(self, host, game, repair, on_done):
        super().__init__(host)
        self.host = host
        self.game = game
        self.repair = repair
        self.on_done = on_done
        self.finished.connect(self._finished)
        self.loading = None
        mode = repair["mode"]
        body = _tr(
            "The previously deployed profile '{0}' is missing and Restore "
            "could not recover it. Recover the original folder from Trash or "
            "backup if possible. Deleted mods and saves cannot be recreated.\n\n"
        ).format(repair["profile"])
        if mode == "SYMLINK":
            body += _tr(
                "Amethyst can use Clean Game Folder to remove remaining mod "
                "symlinks and restore surviving vanilla backups, then reset "
                "the deployment state. Missing original-file backups cannot "
                "be recovered; the game may still need repairing.")
        elif mode == "VFS":
            body += _tr(
                "The VFS view was stored inside the deleted profile. Amethyst "
                "can reset the deployment state once it confirms no VFS "
                "deployment remains. No game files will be removed.")
        else:
            body += _tr(
                "The previous deployment used hardlinks or copies. Mod files "
                "may now be orphaned in the game folder. Resetting deployment "
                "state will not remove them. If orphaned files remain, delete "
                "the game installation folder and reinstall the game before "
                "deploying again.")
        ConfirmOverlay.show_over(
            host, _tr("Repair deployment - {0}").format(game.name), body, self._confirmed,
            confirm_label=(_tr("Clean and reset") if mode == "SYMLINK"
                           else _tr("Reset deployment state")),
            scrollable=True, card_h=380)

    def _confirmed(self, accepted):
        if not accepted:
            self._release()
            return
        from gui_qt.dialogs.loading import LoadingOverlay
        self.loading = LoadingOverlay(self.host.window())
        self.loading.set_text(_tr("Repairing deployment state…"))
        self.loading.show_over()
        self.host.window().setEnabled(False)

        def worker():
            from Utils.app_log import app_log
            from Utils.deployment.pipeline import repair_deleted_profile
            try:
                removed = repair_deleted_profile(
                    self.game, self.repair, log_fn=app_log)
                return removed, ""
            except Exception as exc:
                app_log(f"Deleted-profile repair failed: {exc}")
                return 0, str(exc)

        run_in_worker(worker, self.finished, name="repair-deleted-profile",
                      error_result=(0, _tr("Unexpected repair failure.")))

    @Slot(object)
    def _finished(self, result):
        self.host.window().setEnabled(True)
        self.loading.hide_overlay()
        self.loading.deleteLater()
        _removed, error = result
        if error:
            title = _tr("Profile repair failed")
            body = error + _tr("\n\nThe deployment record has been kept.")
        else:
            title = _tr("Deployment state repaired")
            if self.repair["mode"] in ("HARDLINK", "COPY"):
                body = _tr(
                    "The deployment state has been reset. Orphaned mod files "
                    "may remain in the game folder. If so, delete the game "
                    "installation folder and reinstall before deploying again.")
            elif self.repair["mode"] == "SYMLINK":
                body = _tr(
                    "Game folder cleanup completed and the deployment state "
                    "has been reset. Missing original "
                    "files cannot be recreated; repair the game installation "
                    "if needed before deploying again.")
            else:
                body = _tr(
                    "The deployment state has been reset. No game files "
                    "were changed. You can deploy your selected profile again.")
            if self.on_done is not None:
                self.on_done()
        ConfirmOverlay.show_message(self.host, title, body,
                                    scrollable=True, card_h=320)
        self._release()

    def _release(self):
        self.host._deleted_profile_repair_dialog = None
        self.deleteLater()


def offer_deleted_profile_repair(host, game, *, repair=None, on_done=None) -> bool:
    from Utils.deployment.pipeline import deleted_profile_repair

    if getattr(host, "_deleted_profile_repair_dialog", None) is not None:
        return True
    repair = repair or deleted_profile_repair(game)
    if repair is None:
        return False
    host._deleted_profile_repair_dialog = _DeletedProfileRepair(
        host, game, repair, on_done)
    return True
