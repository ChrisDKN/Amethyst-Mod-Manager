"""In-app viewer for the project's GitHub wiki, fetched live.

A detachable tab: page list on the left, rendered markdown on the right. Pages
and images come from :mod:`Utils.wiki_sync`, so editing the wiki on GitHub is
reflected here on the next fetch — nothing is bundled with the app.

Remote images need a QTextBrowser subclass: Qt never fetches ``https://``
image resources itself, so :class:`_WikiBrowser` returns a placeholder from
``loadResource`` and swaps in the real bitmap once a worker has downloaded it.
Images are also scaled to the viewport width, since wiki screenshots record
their full pixel size and would otherwise force horizontal scrolling.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QImage, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSplitter, QTextBrowser, QToolButton,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.worker import run_in_worker
from Utils import wiki_sync

#: Slug carried on each page-list row.
_SLUG_ROLE = Qt.UserRole

#: Horizontal room left for the scrollbar/margins when scaling an image.
_IMAGE_MARGIN = 28


class _WikiBrowser(QTextBrowser):
    """QTextBrowser that resolves remote wiki images through a supplied fetcher."""

    def __init__(self, request_image, parent=None):
        super().__init__(parent)
        self._request_image = request_image
        self._originals: dict[str, QImage] = {}
        self._requested: set[str] = set()
        self._placeholder = QImage(1, 1, QImage.Format_ARGB32)
        self._placeholder.fill(Qt.transparent)
        self._rescale_timer = QTimer(self)
        self._rescale_timer.setSingleShot(True)
        self._rescale_timer.setInterval(150)
        self._rescale_timer.timeout.connect(self._rescale_all)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)

    def reset_images(self) -> None:
        """Forget which images were requested (called when the page changes)."""
        self._requested.clear()

    def loadResource(self, type_, url):
        if type_ == QTextDocument.ImageResource:
            key = url.toString()
            if key.startswith("http"):
                img = self._originals.get(key)
                if img is not None:
                    return self._fit(img)
                if key not in self._requested:
                    self._requested.add(key)
                    self._request_image(key)
                return self._placeholder
        return super().loadResource(type_, url)

    def set_image(self, url: str, image: QImage) -> None:
        """Install a downloaded image and re-lay out the page around it."""
        if image is None or image.isNull():
            return
        self._originals[url] = image
        # QTextDocument caches whatever loadResource returned (the placeholder),
        # so the resource has to be replaced explicitly before marking dirty.
        doc = self.document()
        doc.addResource(QTextDocument.ImageResource, QUrl(url), self._fit(image))
        doc.markContentsDirty(0, doc.characterCount())

    def _fit(self, image: QImage) -> QImage:
        """Scale *image* down to the viewport width, leaving smaller ones alone."""
        avail = max(1, self.viewport().width() - _IMAGE_MARGIN)
        if image.width() <= avail:
            return image
        return image.scaledToWidth(avail, Qt.SmoothTransformation)

    def _rescale_all(self) -> None:
        if not self._originals:
            return
        doc = self.document()
        for url, img in self._originals.items():
            doc.addResource(QTextDocument.ImageResource, QUrl(url), self._fit(img))
        doc.markContentsDirty(0, doc.characterCount())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Debounced: a drag-resize would otherwise rescale every image per pixel.
        if self._originals:
            self._rescale_timer.start()


class WikiView(QWidget):
    """The wiki tab: page list plus the rendered page."""

    _pages_ready = Signal(object)
    _page_ready = Signal(str, object)
    _image_ready = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closing = False
        self._current_slug: Optional[str] = None
        self._pal = active_palette()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        self._list = QListWidget()
        self._list.setObjectName("WikiPageList")
        self._list.setMinimumWidth(170)
        self._list.currentItemChanged.connect(self._on_row_changed)
        split.addWidget(self._list)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 6, 8, 8)
        rv.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._title = QLabel("")
        self._title.setStyleSheet(
            "font-size:15px; font-weight:600; color:{0};".format(
                _c(self._pal, "TEXT_MAIN")))
        head.addWidget(self._title)
        head.addStretch(1)
        self._refresh_btn = self._flat_button(self.tr("Refresh"))
        self._refresh_btn.clicked.connect(self._refresh)
        head.addWidget(self._refresh_btn)
        self._browser_btn = self._flat_button(self.tr("Open in Browser"))
        self._browser_btn.clicked.connect(self._open_in_browser)
        head.addWidget(self._browser_btn)
        rv.addLayout(head)

        # Deliberately NOT objectName "LogView" (the changelog tab's style):
        # that rule forces font-family: monospace, which reads badly for wiki
        # prose and would override anything set with setFont.
        self._view = _WikiBrowser(self._start_image_fetch)
        self._view.setObjectName("WikiBrowser")
        self._view.setStyleSheet(
            "#WikiBrowser{{background:{0}; color:{1}; border:1px solid {2};"
            " border-radius:4px; padding:8px;}}".format(
                _c(self._pal, "BG_DEEP"), _c(self._pal, "TEXT_MAIN"),
                _c(self._pal, "BORDER")))
        font = self._view.font()
        font.setPointSizeF(font.pointSizeF() + 1)
        self._view.setFont(font)
        self._view.anchorClicked.connect(self._on_link)
        rv.addWidget(self._view, 1)

        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 780])

        self._pages_ready.connect(self._on_pages_ready)
        self._page_ready.connect(self._on_page_ready)
        self._image_ready.connect(self._on_image_ready)

        self._set_message(self.tr("Loading the wiki…"))
        self._start_list_fetch(force=False)

    @staticmethod
    def _flat_button(text: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextOnly)
        b.setObjectName("FooterButton")
        b.setCursor(Qt.PointingHandCursor)
        return b

    # ---------------------------------------------------------------- fetching

    def _start_list_fetch(self, *, force: bool) -> None:
        run_in_worker(lambda: wiki_sync.list_pages(force=force),
                      self._pages_ready, name="wiki-pages", error_result=None)

    def _start_page_fetch(self, slug: str, *, force: bool) -> None:
        run_in_worker(lambda: (slug, wiki_sync.fetch_page(slug, force=force)),
                      self._page_ready, name="wiki-page",
                      error_result=(slug, None), unpack=True)

    def _start_image_fetch(self, url: str) -> None:
        def _load():
            data = wiki_sync.fetch_image(url)
            if not data:
                return (url, None)
            img = QImage()
            return (url, img if img.loadFromData(data) else None)

        run_in_worker(_load, self._image_ready, name="wiki-image",
                      error_result=(url, None), unpack=True)

    # ----------------------------------------------------------------- results

    def _on_pages_ready(self, pages) -> None:
        if self._closing:
            return
        if not pages:
            self._set_message(self.tr(
                "Could not reach the wiki.\n\nCheck your connection and press "
                "Refresh — pages you have already opened stay readable offline."))
            return
        self._list.blockSignals(True)
        self._list.clear()
        for slug, title in pages:
            item = QListWidgetItem(title)
            item.setData(_SLUG_ROLE, slug)
            self._list.addItem(item)
        self._list.blockSignals(False)
        # Restore the page being read across a Refresh; otherwise start at Home.
        row = 0
        if self._current_slug:
            for i in range(self._list.count()):
                if self._list.item(i).data(_SLUG_ROLE) == self._current_slug:
                    row = i
                    break
        self._list.setCurrentRow(row)

    def _on_page_ready(self, slug, text) -> None:
        if self._closing or slug != self._current_slug:
            return   # a different page was selected while this one downloaded
        if not text:
            self._set_message(self.tr(
                "Could not load “{0}”.\n\nPress Refresh to try again."
            ).format(slug))
            return
        self._view.reset_images()
        # QTextEdit.setMarkdown takes no dialect argument — it always uses
        # MarkdownDialectGitHub, which is what the wiki is authored in.
        self._view.setMarkdown(wiki_sync.to_display_markdown(text))
        self._space_blocks()
        self._view.moveCursor(QTextCursor.Start)
        self._view.verticalScrollBar().setValue(0)

    def _space_blocks(self) -> None:
        """Add paragraph spacing to the rendered page.

        Qt's markdown importer gives every block zero margins, so prose comes
        out visually cramped compared to how the same page reads on GitHub.
        """
        doc = self._view.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            fmt = block.blockFormat()
            if fmt.headingLevel():
                fmt.setTopMargin(14)
                fmt.setBottomMargin(4)
            elif block.textList() is not None:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(3)
            else:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(10)
            cur.setPosition(block.position())
            cur.setBlockFormat(fmt)
            block = block.next()
        cur.endEditBlock()

    def _on_image_ready(self, url, image) -> None:
        if self._closing or image is None:
            return
        self._view.set_image(url, image)

    # -------------------------------------------------------------- navigation

    def _on_row_changed(self, current, _previous) -> None:
        if current is None:
            return
        slug = current.data(_SLUG_ROLE)
        self._current_slug = slug
        self._title.setText(current.text())
        self._set_message(self.tr("Loading…"))
        self._start_page_fetch(slug, force=False)

    def _load_slug(self, slug: str) -> None:
        """Show a page by slug, syncing the list selection when it is listed."""
        for i in range(self._list.count()):
            if self._list.item(i).data(_SLUG_ROLE) == slug:
                self._list.setCurrentRow(i)
                return
        self._current_slug = slug
        self._title.setText(slug.replace("-", " "))
        self._set_message(self.tr("Loading…"))
        self._start_page_fetch(slug, force=False)

    def _on_link(self, url: QUrl) -> None:
        """Follow wiki-internal links in place; send everything else to a browser."""
        href = url.toString()
        slug = wiki_sync.resolve_link(href)
        if slug:
            self._load_slug(slug)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _refresh(self) -> None:
        self._start_list_fetch(force=True)
        if self._current_slug:
            self._start_page_fetch(self._current_slug, force=True)

    def _open_in_browser(self) -> None:
        slug = self._current_slug or "Home"
        QDesktopServices.openUrl(QUrl(wiki_sync.page_web_url(slug)))

    # ------------------------------------------------------------------ helpers

    def _set_message(self, text: str) -> None:
        self._view.setMarkdown(text)

    def closeEvent(self, event):
        self._closing = True
        super().closeEvent(event)
