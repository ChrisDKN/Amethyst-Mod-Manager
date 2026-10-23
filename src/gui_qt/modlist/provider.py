from __future__ import annotations

from copy import copy
import threading
import time

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton

from gui_qt.dialogs.base import OverlayBase


class ProviderOverlay(OverlayBase):
    CARD_W = 760
    CARD_H = 620
    completed = Signal(object, object)
    message = Signal(str)

    def __init__(self, host, plugin, game, profile, on_done, *, mod=None, source=None, provider_id=None, mods=None):
        super().__init__(host, on_done=on_done)
        self.game, self.profile, self.plugin = copy(game), profile, plugin
        self.mod, self.source, self.provider_id = mod, source, provider_id
        self.mods = mods
        self.busy, self.changed, self.closing = False, False, False
        self.stop = threading.Event()
        self.catalog = None
        self.plan = None
        card, layout = self._make_card("ProviderCard")
        title = QLabel(f"{plugin.name} · {mod or game.name}", card)
        title.setTextFormat(Qt.PlainText)
        layout.addWidget(title)
        self.status = QLabel(self.tr("Loading provider details…"), card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.versions = QComboBox(card)
        layout.addWidget(self.versions)
        self.actions = QComboBox(card)
        layout.addWidget(self.actions)
        self.output = QPlainTextEdit(card)
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(2000)
        layout.addWidget(self.output, 1)
        buttons = QHBoxLayout()
        self.install = QPushButton(self.tr("Install selected version") if mod else self.tr("Download and install"), card)
        self.install.clicked.connect(self._install)
        buttons.addWidget(self.install)
        self.action = QPushButton(self.tr("Apply selected action"), card)
        self.action.clicked.connect(self._action)
        buttons.addWidget(self.action)
        self.page = QPushButton(self.tr("Open website"), card)
        self.page.clicked.connect(self._page)
        buttons.addWidget(self.page)
        close = QPushButton(self.tr("Close"), card)
        close.clicked.connect(self._finish)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.message.connect(self.output.appendPlainText)
        self.completed.connect(self._completed)
        self._present()
        self._run(self._load, self._loaded)

    def _wait(self, service, method, params):
        from amethyst_plugins.client import PluginError
        response = service.call(method, params)
        if "job" not in response:
            return response
        job = response["job"]
        previous = None
        while job["state"] not in {"succeeded", "failed", "cancelled"}:
            if self.stop.is_set() and job.get("cancellable"):
                service.call("jobs.cancel", {"job_id": job["id"]})
            time.sleep(0.15)
            job = service.call("jobs.get", {"job_id": job["id"]})["job"]
            progress = job.get("progress")
            if progress and progress != previous:
                previous = progress
                label = progress.get("message") or progress.get("phase") or job["kind"]
                if progress.get("total"):
                    label += f" ({progress.get('done', 0)}/{progress['total']})"
                self.message.emit(label)
        if job["state"] != "succeeded":
            error = job.get("error") or {}
            raise PluginError(error.get("message", "Operation cancelled."), error)
        return job.get("result") or {}

    def _run(self, function, callback):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        self.install.setEnabled(False)
        self.action.setEnabled(False)
        def work():
            from amethyst_backend.service import AmethystService
            service = None
            try:
                service = AmethystService({self.game.game_id: self.game})
                result = function(service)
            except Exception as exc:
                result = exc
            finally:
                if service is not None:
                    try:
                        service.close()
                    except Exception as exc:
                        result = exc
            from gui_qt.runtime.safe_emit import safe_emit
            safe_emit(self.completed, callback, result)
        threading.Thread(target=work, daemon=True, name="provider-panel").start()

    def _completed(self, callback, result):
        self.busy = False
        if self.closing:
            super()._finish(self.changed)
            return
        if isinstance(result, Exception):
            self.status.setText(str(result))
            self.output.appendPlainText(str(result))
        else:
            callback(result)
        self.install.setEnabled(not self.busy and (self.catalog is not None or self.plan is not None))
        self.action.setEnabled(not self.busy and self.catalog is not None and bool(self.catalog["actions"]))

    def _load(self, service):
        if self.mods is not None:
            return self._wait(service, "mods.quick_update.plan.start", {"game": self.game.game_id,
                              "profile": self.profile, "source": "plugins", "mods": self.mods})
        if self.mod:
            from amethyst_plugins.repositories import installed, source_key
            properties = self._wait(service, "mods.properties.get", {"game": self.game.game_id, "profile": self.profile, "mod": self.mod})["properties"]
            record = installed(properties["path"])
            if record is None:
                raise ValueError("This mod has no provider identity.")
            return self._wait(service, "mods.versions.get.start", {"game": self.game.game_id, "profile": self.profile,
                              "mod": self.mod, "source": source_key(record)})
        roots = [{"plugin_id": self.plugin.id, "provider_id": self.provider_id, "source": self.source}]
        graph = self._wait(service, "manager_plugins.dependencies.get.start", {"game": self.game.game_id, "items": roots})
        plans = [{"request": row, "artifact": self._wait(service, "manager_plugins.download.resolve.start", {"game": self.game.game_id, **row})}
                 for row in graph["items"]]
        return {"roots": roots, "dependency_revision": graph["revision"], "items": plans}

    def _loaded(self, result):
        self.versions.clear()
        self.actions.clear()
        if self.mods is not None:
            self.plan = result
            self.versions.hide()
            self.actions.hide()
            self.action.hide()
            self.page.hide()
            self.install.setText(self.tr("Update listed mods"))
            self.status.setText(self.tr("Review the proposed provider updates below."))
            self.output.clear()
            for row in result["items"]:
                self.output.appendPlainText(f"{row['mod']}: {row['candidate']['version']}")
            for row in result["skipped"]:
                self.output.appendPlainText(f"{row['mod']}: {row['reason']}")
        elif self.mod:
            self.catalog = result
            for row in result["versions"]:
                self.versions.addItem(row.get("label") or row["version"], row["id"])
            recommended = self.versions.findData(result["recommended"])
            if recommended >= 0:
                self.versions.setCurrentIndex(recommended)
            for row in result["actions"]:
                self.actions.addItem(row["label"], row["id"])
            self.status.setText(self.tr("Choose a version or repository action. Installation replaces the selected mod."))
        else:
            self.plan = result
            self.versions.hide()
            self.actions.hide()
            self.action.hide()
            self.page.hide()
            self.status.setText(self.tr("Review the downloads and dependencies below, then choose Download and install."))
            for row in result["items"]:
                artifact = row["artifact"]
                self.output.appendPlainText(f"{artifact['provider_label']}: {artifact['file_name']} ({artifact['size_bytes']} bytes)")

    def _install(self):
        if self.mod and not self.versions.currentData():
            return
        self.changed = True
        if self.mods is not None:
            params = {"game": self.game.game_id, "profile": self.profile, "source": "plugins",
                      "mods": self.mods, "revision": self.plan["revision"], "confirm": True}
            self._run(lambda service: self._wait(service, "mods.quick_update.start", params), self._installed)
        elif self.mod:
            params = {"game": self.game.game_id, "profile": self.profile, "mod": self.mod, "source": self.catalog["source"],
                      "revision": self.catalog["revision"], "catalogue_revision": self.catalog["catalogue_revision"],
                      "version": self.versions.currentData(), "confirm": True}
            self._run(lambda service: self._wait(service, "mods.version.change.start", params), self._installed)
        else:
            self._run(self._install_sources, self._installed)

    def _install_sources(self, service):
        from amethyst_plugins.client import provider_install_steps
        from amethyst_plugins.workflows import arguments
        results = []
        for step in provider_install_steps(self.game.game_id, self.profile, self.plan):
            if self.stop.is_set():
                raise ValueError("Provider installation cancelled; completed operations were retained.")
            result = self._wait(service, step["method"], arguments(step["params"], results))
            results.append({"result": result})
        return {"ok": True}

    def _installed(self, _result):
        self.status.setText(self.tr("Operation completed."))
        if self.mod:
            self._run(self._load, self._loaded)
        else:
            self.plan = None

    def _action(self):
        if self.catalog is None or not self.actions.currentData():
            return
        self.changed = True
        params = {"game": self.game.game_id, "profile": self.profile, "mod": self.mod,
                  "source": self.catalog["source"], "revision": self.catalog["revision"],
                  "catalogue_revision": self.catalog["catalogue_revision"], "action_id": self.actions.currentData(), "confirm": True}
        self._run(lambda service: self._wait(service, "manager_plugins.action.start", params), self._installed)

    def _page(self):
        if self.catalog and self.catalog.get("url"):
            from Utils.environment.xdg import open_url
            open_url(self.catalog["url"])

    def _finish(self, result=None):
        if self.busy:
            self.closing = True
            self.stop.set()
            self.status.setText(self.tr("Waiting for the active operation to finish…"))
        else:
            super()._finish(self.changed)
