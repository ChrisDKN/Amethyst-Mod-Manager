from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QLabel, QWidget

from amethyst_plugins.i18n import translate
from gui_qt.dialogs.base import OverlayBase


class QtPluginOverlay(OverlayBase):
    CARD_W = 900
    CARD_H = 800

    def __init__(self, host, plugin, game, profile, on_done):
        super().__init__(host, on_done=on_done)
        self._module_name = f"_amethyst_plugin_{uuid.uuid4().hex}"
        self.view = None
        self.changed = False
        self._close_after_operation = False
        self._plugin_context = None
        card, layout = self._make_card("QtPluginCard")
        title = QLabel(translate(plugin, plugin.name), card)
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        try:
            from amethyst_plugins.catalog import load_settings
            source = Path(__file__).resolve().parents[2]
            manuals = [source / "docs/headless.md", source.parent / "docs/headless.md"]
            manuals.extend(Path(prefix) / "share/doc/Amethyst-Mod-Manager/headless.md"
                           for prefix in (sys.prefix, "/app", "/usr/local", "/usr"))
            context = {
                "api_version": 1, "plugin_id": plugin.id,
                "game": game.name if game else "", "game_id": game.game_id if game else "", "profile": profile,
                "settings": load_settings(plugin),
                "headless_command": [sys.executable, str(source / "cli.py"), "headless", "--stdio"],
                "headless_documentation": next((str(p) for p in manuals if p.is_file()), None),
            }
            self._plugin_context = host._plugin_ui.services.context(plugin)
            snapshot = self._plugin_context.snapshot()
            context.update({key: snapshot[key] for key in (
                "plugin_dir", "settings_path", "data_path", "language",
                "available_methods",
                "host_capabilities",
                "apply_async", "apply_batch_async",
                "configured_games", "data", "save_data", "api_call", "read_async", "inventory", "subscribe", "selection", "notify", "dismiss_notification")})
            spec = importlib.util.spec_from_file_location(
                self._module_name, plugin.entrypoint,
                submodule_search_locations=[str(plugin.entrypoint.parent)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[self._module_name] = module
            spec.loader.exec_module(module)
            self.view = module.create_view(context, card, self._closed)
            if (not isinstance(self.view, QWidget) or self.view.isWindow()
                    or not card.isAncestorOf(self.view)
                    or not callable(getattr(self.view, "request_close", None))):
                raise ValueError("Qt plugins must return a child QWidget with request_close().")
            layout.addWidget(self.view, 1)
            preferred_size = getattr(self.view, "preferred_size", None)
            if preferred_size is not None:
                if not isinstance(preferred_size, QSize) or preferred_size.isEmpty():
                    raise ValueError("Qt plugin preferred_size must be a positive QSize.")
                self._card_w = preferred_size.width()
                self._card_h = preferred_size.height()
            self._escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
            self._escape.setContext(Qt.WidgetWithChildrenShortcut)
            self._escape.activated.connect(self._finish)
            self._present()
            self.view.setFocus()
        except Exception:
            self._done = True
            self._host.removeEventFilter(self)
            self._unload()
            self.deleteLater()
            raise

    def _unload(self):
        if self._plugin_context is not None:
            self._plugin_context.dispose()
        for name in list(sys.modules):
            if name == self._module_name or name.startswith(self._module_name + "."):
                del sys.modules[name]

    def _closed(self, changed=False):
        QTimer.singleShot(0, self, lambda: self._complete(bool(changed)))

    def _complete(self, changed):
        if self._done:
            return
        operations = getattr(self._host._plugin_ui, "operations", None)
        request = operations.current if operations is not None else None
        if (request is not None and request.owner() is self._plugin_context
                and request.overlay is not None and not request.overlay._done):
            self.changed |= bool(changed)
            self._close_after_operation = True
            operations.cancel(request)
            return
        self._unload()
        super()._finish(changed or self.changed)

    def _finish(self, result=None):
        if not self._done and self.view is not None:
            self.view.request_close()

    def closeEvent(self, event):
        event.ignore()
        self._finish()
