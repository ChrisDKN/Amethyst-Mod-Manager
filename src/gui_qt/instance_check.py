from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QMessageBox

from Utils.processes.instances import running_instances, terminate_instances


def _tr(text):
    return QCoreApplication.translate("InstanceCheck", text)


def _work(callback, label, splash):
    if splash is not None:
        splash.set_message(label)
    loop = QEventLoop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(callback)
        timer = QTimer(loop)
        timer.timeout.connect(lambda: loop.quit() if future.done() else None)
        timer.start(50)
        loop.exec()
        timer.stop()
        return future.result()


def _show_warning(box, splash):
    if splash is not None:
        splash.hide()
    try:
        box.exec()
    finally:
        if splash is not None:
            splash.show()


def check_running_instances(splash=None) -> bool:
    while True:
        try:
            instances = _work(running_instances, _tr("Checking instances…"), splash)
        except Exception as exc:
            box = QMessageBox(QMessageBox.Warning, _tr("Instance check failed"),
                              _tr("Could not check whether another Amethyst instance is running."))
            box.setDetailedText(str(exc))
            proceed = box.addButton(_tr("Continue anyway"), QMessageBox.AcceptRole)
            cancel = box.addButton(_tr("Cancel launch"), QMessageBox.RejectRole)
            box.setDefaultButton(cancel)
            box.setEscapeButton(cancel)
            _show_warning(box, splash)
            return box.clickedButton() is proceed
        if not instances:
            return True

        box = QMessageBox(QMessageBox.Warning, _tr("Amethyst is already running"),
                          _tr("Other Amethyst instances are running, possibly in the background. "
                              "They can prevent this instance from refreshing or deploying mods."))
        box.setInformativeText(_tr(
            "Terminating them can lose unsaved changes and interrupt downloads, installs or "
            "deployments. Unresponsive instances will be force killed.\n\n"
            "Processes: {0}").format(", ".join(str(item.pid) for item in instances)))
        terminate = box.addButton(_tr("Terminate other instances"), QMessageBox.DestructiveRole)
        proceed = box.addButton(_tr("Continue anyway"), QMessageBox.AcceptRole)
        cancel = box.addButton(_tr("Cancel launch"), QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        _show_warning(box, splash)
        if box.clickedButton() is proceed:
            return True
        if box.clickedButton() is not terminate:
            return False
        try:
            _work(lambda: terminate_instances(instances),
                  _tr("Stopping instances…"), splash)
        except Exception as exc:
            box = QMessageBox(QMessageBox.Warning, _tr("Could not terminate all instances"),
                              _tr("Some Amethyst instances could not be stopped. "
                                  "Close them manually or try again."))
            box.setDetailedText(str(exc))
            _show_warning(box, splash)
