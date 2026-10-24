from __future__ import annotations

import queue
import threading
import time
import weakref
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from Nexus.nexus_meta import normalise_game_domain, read_meta
from gui_qt.previews.image_view import ImageView
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.widgets.card_helpers import ThumbnailLoader
from gui_qt.widgets.eliding_label import ElidingLabel


class Fetcher(QObject):
    completed = Signal(object, object, str)

    def __init__(self):
        super().__init__()
        self.pending = queue.Queue()
        self.stopped = threading.Event()
        self.cache = OrderedDict()
        self.thread = None

    def request(self, key, callback):
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            self.cache.move_to_end(key)
            callback(cached[1], "")
            return lambda: None
        cancelled = threading.Event()
        self.pending.put((key, cancelled, callback))
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="nexus-mod-info")
            self.thread.start()
        return cancelled.set

    def run(self):
        from Nexus.session import NexusSession

        while not self.stopped.is_set():
            request = self.pending.get()
            if request is None:
                return
            key, cancelled, callback = request
            if cancelled.is_set():
                continue
            result, error = {}, ""
            session = NexusSession()
            try:
                api = session.require_api()
                domain, mod_id = key
                info = api.graphql_mod_info_batch([(domain, mod_id)]).get(mod_id)
                if info is None:
                    raise LookupError("Nexus info unavailable")
                result = {
                    "name": info.name, "description": info.summary,
                    "version": info.version, "picture_url": info.picture_url,
                    "downloads": info.downloads_total,
                    "endorsements": info.endorsement_count,
                }
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if getattr(exc, "name", "").startswith("nexus_auth") or status == 401:
                    error = "Reconnect Nexus Mods in Amethyst Settings, then select the mod again."
                elif getattr(exc, "name", "") == "nexus_not_authenticated":
                    error = "Connect Nexus Mods in Amethyst Settings, then select the mod again."
                elif status in (403, 404):
                    error = "This mod is hidden, deleted, or unavailable to your Nexus account."
                elif status == 429:
                    error = "Nexus rate limit reached. Select the mod again later."
                else:
                    error = "Could not load Nexus info. Check your connection and select the mod again."
            finally:
                session.close()
            if not cancelled.is_set() and not self.stopped.is_set():
                try:
                    self.completed.emit((key, cancelled, callback), result, error)
                except RuntimeError:
                    return

    def deliver(self, request, result, error):
        key, cancelled, callback = request
        if cancelled.is_set() or self.stopped.is_set():
            return
        if result and not error:
            self.cache[key] = (time.monotonic(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        callback(result, error)

    def dispose(self):
        self.stopped.set()
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break
        self.pending.put(None)


class Description(QTextBrowser):
    def loadResource(self, kind, url):
        return None


class ModImage(QLabel):
    clicked = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._source_pixmap = None
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def setPixmap(self, pixmap):
        self._source_pixmap = pixmap
        self.fit_image()

    def clear(self):
        self._source_pixmap = None
        super().clear()

    def fit_image(self):
        if self._source_pixmap is None or self._source_pixmap.isNull():
            return
        ratio = self.devicePixelRatioF()
        bounds = self.contentsRect().size() * ratio
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        pixmap = self._source_pixmap.scaled(bounds, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pixmap.setDevicePixelRatio(ratio)
        super().setPixmap(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_image()

    def set_available(self, available):
        self.available = available
        self.setCursor(Qt.PointingHandCursor if available else Qt.ArrowCursor)
        self.setToolTip("Open image in a viewer tab" if available else "")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and getattr(self, "available", False):
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.LeftButton and getattr(self, "available", False)
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class ImageTab(ImageView):
    loaded = Signal(object, str)

    def __init__(self, url, preview=None):
        super().__init__(pixmap=preview)
        self.status = QLabel("Loading full-size image…", self)
        self.status.setWordWrap(True)
        self.layout().insertWidget(0, self.status)
        self.loaded.connect(self.show_image)
        self.stopped = threading.Event()
        stopped, loaded = self.stopped, self.loaded
        self.destroyed.connect(lambda *_: stopped.set())

        def fetch():
            import requests
            from Utils.ca_bundle import resolve_ca_bundle

            image, error = QImage(), ""
            try:
                with requests.get(url, timeout=15, verify=resolve_ca_bundle() or True) as response:
                    response.raise_for_status()
                    image = QImage.fromData(response.content)
                if image.isNull():
                    error = "This image could not be displayed."
            except Exception:
                error = "Could not load the full-size image."
            if not stopped.is_set():
                safe_emit(loaded, image, error)

        threading.Thread(target=fetch, daemon=True, name="nexus-mod-image").start()

    def show_image(self, image, error):
        if self.stopped.is_set():
            return
        self.status.setText(error)
        self.status.setVisible(bool(error))
        if not image.isNull():
            self._canvas.set_image(QPixmap.fromImage(image))

    def tab_closing(self):
        self.stopped.set()


class InfoPanel(QWidget):
    def __init__(self, service, source_id, parent=None):
        super().__init__(parent)
        self.service = service
        self.source_id = source_id
        self.identity = None
        self.generation = 0
        self.cancel = lambda: None
        self.image_url = ""
        self.description_source = ""
        self.setObjectName("NexusModInfoPanel")
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(5)
        header = QHBoxLayout()
        self.title = ElidingLabel("Nexus mod info")
        self.title.setTextFormat(Qt.PlainText)
        header.addWidget(self.title, 1)
        layout.addLayout(header)
        content = QHBoxLayout()
        self.image = ModImage("No image")
        self.image.set_available(False)
        self.image.clicked.connect(self.open_image)
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setWordWrap(True)
        content.addWidget(self.image, 1)
        details = QWidget()
        details.setMinimumSize(0, 0)
        details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        facts = QVBoxLayout(details)
        facts.setContentsMargins(0, 0, 0, 0)
        facts.setSpacing(5)
        self.stats = QLabel()
        self.stats.setTextFormat(Qt.PlainText)
        self.stats.setWordWrap(True)
        self.stats.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        facts.addWidget(self.stats)
        facts.addWidget(self.message)
        self.description = Description()
        self.description.setMinimumSize(0, 0)
        self.description.setOpenLinks(False)
        self.description.setOpenExternalLinks(False)
        facts.addWidget(self.description, 1)
        content.addWidget(details, 1)
        layout.addLayout(content, 1)
        self.thumbnails = ThumbnailLoader(self, crop_w=768, crop_h=512, fit=True)
        self.thumbnails.loaded.connect(self.thumbnail_ready)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(250)
        self.debounce.timeout.connect(self.fetch)
        self.meta = None
        self.refresh()

    def refresh(self, *, force=False):
        if not self.service.active:
            return
        context = self.service.context
        if not context.get("game_id") or not context.get("settings", {}).get("enabled", True):
            self.dispose()
            self.identity = None
            return
        snapshot = context["selection"]("modlist", source_id=self.source_id)
        items = snapshot.get("items") or []
        current = snapshot.get("current")
        item = current if current in items else next(iter(items), None)
        identity = (context.get("game_id"), context.get("profile"),
                    item.get("type") if item else None, item.get("path") if item else None)
        if identity == self.identity and not force:
            return
        self.identity = identity
        self.cancel()
        self.debounce.stop()
        self.generation += 1
        self.image_url = self.description_source = ""
        self.image.clear()
        self.image.setText("No image")
        self.image.set_available(False)
        self.thumbnails.discard_pending()
        self.meta = None
        self.title.setText("Nexus mod info")
        self.stats.setText("Downloads: —   Endorsements: —   Version: —")
        self.description.clear()
        self.message.show()
        if not item or item.get("type") != "mod" or not item.get("path"):
            self.message.setText("Select a mod to see its Nexus info.")
            return
        self.title.setText(item.get("display_name") or item["name"])
        try:
            self.meta = read_meta(Path(item["path"]) / "meta.ini")
        except (OSError, ValueError):
            self.message.setText("Could not read this mod's metadata.")
            return
        self.stats.setText(f"Downloads: —   Endorsements: —   Version: {self.meta.version or '—'} (installed)")
        self.description.setPlainText(self.meta.description or "No description saved locally.")
        domain = normalise_game_domain(self.meta.game_domain)
        if self.meta.mod_id <= 0 or not domain:
            self.message.setText("This mod has no linked Nexus page.")
            return
        self.key = (domain, self.meta.mod_id)
        self.message.setText("Loading Nexus info…")
        self.debounce.start()

    def fetch(self):
        generation = self.generation

        def ready(result, error):
            if self.service.active and isValid(self) and generation == self.generation:
                self.show_result(result, error)

        self.cancel = self.service.fetcher.request(self.key, ready)

    def show_result(self, result, error):
        self.message.setText(error)
        self.message.setVisible(bool(error))
        if not result:
            return
        self.title.setText(result.get("name") or self.title.text())
        downloads = result.get("downloads")
        endorsements = result.get("endorsements")
        count = lambda value: f"{int(value):,}" if value is not None else "—"
        version = result.get("version") or "—"
        installed = self.meta.version or "—"
        self.stats.setText(
            f"Downloads: {count(downloads)}   Endorsements: {count(endorsements)}\n"
            f"Version: {version} (Nexus)   Installed: {installed}")
        self.description_source = (result.get("description") or self.meta.description or "").strip()
        self.description.setPlainText(self.description_source or "No description available.")
        self.description.verticalScrollBar().setValue(0)
        self.image_url = result.get("picture_url") or ""
        self.image.setText("Image unavailable" if self.image_url else "No image")
        available = self.image_url.startswith(("https://", "http://"))
        self.image.set_available(available)
        if available:
            self.thumbnails.request(self.generation, self.image_url)

    def open_image(self):
        if self.image.available and self.service.active:
            self.service.open_image(
                self.key, self.image_url, self.title.text(),
                self.thumbnails.cached(self.image_url))

    def thumbnail_ready(self, _request_id, _pixmap):
        if self.service.active and self.image_url:
            pixmap = self.thumbnails.cached(self.image_url)
            if pixmap is not None:
                self.image.setPixmap(pixmap)

    def dispose(self):
        self.generation += 1
        self.debounce.stop()
        self.cancel()
        self.thumbnails.discard_pending()


def containing_layout(layout, widget):
    if layout is None:
        return None
    if layout.indexOf(widget) >= 0:
        return layout
    for index in range(layout.count()):
        nested = layout.itemAt(index).layout()
        found = containing_layout(nested, widget) if nested is not None else None
        if found is not None:
            return found
    return None


class Service:
    def __init__(self, context):
        self.context = context
        self.active = True
        self.attachments = []
        self.image_tabs = []
        self.fetcher = Fetcher()
        self.fetcher.completed.connect(self.fetcher.deliver)
        self.unsubscribe = context["subscribe"](
            ["selection.changed", "mods.changed", "context.changed"], self.on_event)
        self.timer = QTimer()
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.attach)
        self.timer.start()
        self.attach()

    def attach(self):
        from gui_qt.modlist.view import ModListView

        self.attachments = [entry for entry in self.attachments if isValid(entry[1])]
        enabled = bool(self.context.get("game_id")) and self.context.get("settings", {}).get("enabled", True)
        if not enabled:
            for _, _, panel, _ in self.attachments:
                if not panel.isHidden():
                    panel.dispose()
                    panel.identity = None
                    panel.hide()
            return
        for view in QApplication.allWidgets():
            if not isinstance(view, ModListView) or any(ref() is view for ref, *_ in self.attachments):
                continue
            parent = view.parentWidget()
            layout = containing_layout(parent.layout(), view) if parent is not None else None
            binding = getattr(view, "_amethyst_selection_binding", None)
            if layout is None or binding is None:
                continue
            splitter = QSplitter(Qt.Vertical, parent)
            splitter.setObjectName("NexusModInfoSplitter")
            splitter.setHandleWidth(6)
            splitter.setChildrenCollapsible(False)
            layout.replaceWidget(view, splitter)
            splitter.addWidget(view)
            panel = InfoPanel(self, binding[2], splitter)
            splitter.addWidget(panel)
            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, True)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 0)
            splitter.setSizes([max(200, view.height() - 230), 230])
            self.attachments.append((weakref.ref(view), splitter, panel, layout))
        for _, splitter, panel, _ in self.attachments:
            panel.setVisible(enabled)

    def set_context(self, context):
        self.context = context
        self.attach()
        for _, _, panel, _ in self.attachments:
            panel.refresh(force=True)

    def on_event(self, event):
        if event["type"] == "selection.changed" and event.get("data", {}).get("location") != "modlist":
            return
        self.attach()
        for _, _, panel, _ in self.attachments:
            panel.refresh(force=event["type"] != "selection.changed")

    def open_image(self, mod_key, url, title, preview):
        for ref, *_ in self.attachments:
            source = ref()
            if source is None or not isValid(source):
                continue
            tabs = getattr(source.window(), "_tabs", None)
            if tabs is None:
                continue
            key = f"nexus-info-image:{mod_key[0]}/{mod_key[1]}"
            if tabs.has_key(key):
                tabs.focus_key(key)
                return
            viewer = ImageTab(url, preview)
            tabs.open_tab(viewer, title, key=key)
            self.image_tabs = [(ref, tab_key) for ref, tab_key in self.image_tabs
                               if ref() is not None and isValid(ref()) and ref().has_key(tab_key)]
            self.image_tabs.append((weakref.ref(tabs), key))
            return

    def dispose(self):
        self.active = False
        self.timer.stop()
        self.unsubscribe()
        self.fetcher.dispose()
        for ref, key in self.image_tabs:
            tabs = ref()
            if tabs is not None and isValid(tabs):
                tabs.close_tab(key)
        self.image_tabs.clear()
        for ref, splitter, panel, layout in self.attachments:
            if not isValid(splitter):
                continue
            panel.dispose()
            view = ref()
            if view is not None and isValid(view):
                layout.replaceWidget(splitter, view)
                view.setParent(layout.parentWidget())
                view.show()
            splitter.hide()
            splitter.deleteLater()
        self.attachments.clear()


def create_service(context):
    return Service(context)


def create_view(context, parent, on_close):
    class SettingsView(QWidget):
        preferred_size = (520, 240)

        def request_close(self):
            on_close(False)

    view = SettingsView(parent)
    layout = QVBoxLayout(view)
    label = QLabel(
        "Nexus mod info appears beneath each modlist. Select a Nexus-linked mod "
        "to see its image, short description, downloads, endorsements and version.\n\n"
        "Connect Nexus Mods in Amethyst Settings. Drag the divider above the "
        "panel to resize it, or drag it all the way down to hide it. "
        "Drag it back up to show the panel again. Click the mod image to open "
        "it in an image viewer tab.")
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    layout.addWidget(label)
    close = QPushButton("Close")
    close.clicked.connect(view.request_close)
    layout.addWidget(close, 0, Qt.AlignRight)
    return view
