from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget


class PluginWizard(QWidget):
    def __init__(self, plugin, wizard, context_factory, on_close, log):
        super().__init__()
        from .plugin_ui import _Module
        self.context = context_factory(plugin)
        self.module = None
        self.view = None
        self.disposed = False
        self.log = log
        self.on_close = on_close
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            self.module = _Module(replace(plugin, entrypoint=wizard.entrypoint))
            snapshot = self.context.snapshot()
            snapshot.update(wizard_id=wizard.id, log=log)
            target = snapshot["game_id"], snapshot["profile"]
            self.view = self.module.module.create_wizard(snapshot, self, self.finish)
            if (not isinstance(self.view, QWidget) or self.view.isWindow()
                    or not self.isAncestorOf(self.view) or not callable(getattr(self.view, "dispose", None))):
                raise ValueError("create_wizard must return a child QWidget with dispose().")
            layout.addWidget(self.view)
            self.context.subscribe("context.changed", lambda event: self.finish()
                                   if (event["game_id"], event["profile"]) != target else None, scope="all")
        except BaseException:
            self.tab_closing()
            self.deleteLater()
            raise

    def finish(self, changed=False):
        if not self.disposed:
            QTimer.singleShot(0, self, self.on_close)

    def tab_closing(self):
        if self.disposed:
            return
        self.disposed = True
        self.context.dispose()
        try:
            if callable(getattr(self.view, "dispose", None)):
                self.view.dispose()
        except Exception as exc:
            self.log(f"Plugin wizard cleanup failed: {exc}")
        finally:
            if self.module is not None:
                self.module.retired = True
                self.module.unload()
