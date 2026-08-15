"""In-app viewer for the project's GitHub wiki, fetched live.

A detachable tab: page list on the left, rendered markdown on the right. Pages
and images come from :mod:`Utils.wiki_sync`, so editing the wiki on GitHub is
reflected here on the next fetch - nothing is bundled with the app. The list
mirrors the wiki's own ``_Sidebar.md``, group headings included, so the app's
navigation is edited on GitHub alongside the pages.

Remote images need a QTextBrowser subclass: Qt never fetches ``https://``
image resources itself, so :class:`_WikiBrowser` returns a placeholder from
``loadResource`` and swaps in the real bitmap once a worker has downloaded it.
Images are also scaled to the viewport width, since wiki screenshots record
their full pixel size and would otherwise force horizontal scrolling.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize, QUrl, QTimer
from PySide6.QtGui import (
    QColor, QDesktopServices, QImage, QTextCursor, QTextDocument,
    QTextFrameFormat, QTextLength, QTextTable,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QSplitter, QTextBrowser, QToolButton,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, bind_theme, _c
from gui_qt.worker import run_in_worker
from Utils import wiki_sync

#: Slug carried on each page-list row (absent on the sidebar's group headers).
_SLUG_ROLE = Qt.UserRole

#: Horizontal room left for the scrollbar/margins when scaling an image.
_IMAGE_MARGIN = 28

#: Extra height above a sidebar group header, to set its group apart.
_HEADER_GAP = 10


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

    def reset_images(self, *, drop_cached: bool = False) -> None:
        """Forget which images were requested (called when the page changes).

        *drop_cached* also discards the decoded bitmaps, so an explicit
        Refresh re-fetches artwork that was replaced at the same URL.
        """
        self._requested.clear()
        if drop_cached:
            self._originals.clear()

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

    # A profile share code printed in the page was clicked. Whoever opened the
    # tab connects this to the manager's import-code flow. Plain "#", not "#:":
    # lupdate reads a "#:" comment as a note to translators.
    import_code_requested = Signal(str)

    _rows_ready = Signal(object)
    _page_ready = Signal(str, object)
    _image_ready = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closing = False
        self._current_slug: Optional[str] = None
        self._page_text: dict[str, str] = {}
        self._awaiting_refresh = False
        self._share_codes: list[str] = []
        self._pal = active_palette()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        self._list = QListWidget()
        self._list.setObjectName("WikiPageList")
        self._list.setMinimumWidth(170)
        self._list.setStyleSheet("#WikiPageList::item{padding:2px 6px;}")
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
        self._status = QLabel("")
        self._status.setStyleSheet(
            "color:{0};".format(_c(self._pal, "TEXT_DIM")))
        head.addWidget(self._status)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(8000)
        self._status_timer.timeout.connect(lambda: self._status.setText(""))
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

        self._rows_ready.connect(self._on_rows_ready)
        self._page_ready.connect(self._on_page_ready)
        self._image_ready.connect(self._on_image_ready)

        bind_theme(self, roles={
            "TONE_BLUE", "ACCENT", "BORDER", "BG_HEADER", "BG_ROW_ALT",
        })

        self._set_message(self.tr("Loading the wiki…"))
        self._start_list_fetch(force=False)

    def refresh_theme(self, p: dict) -> None:
        self._pal = p
        key = "TONE_BLUE" if p.get("TONE_BLUE") else "ACCENT"
        for i in range(self._list.count()):
            item = self._list.item(i)
            if not item.data(_SLUG_ROLE) and item.flags() != Qt.NoItemFlags:
                item.setForeground(QColor(_c(p, key)))
        self._style_tables()
        self._view.viewport().update()

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
        run_in_worker(lambda: wiki_sync.nav_rows(force=force),
                      self._rows_ready, name="wiki-pages", error_result=None)

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

    def _on_rows_ready(self, rows) -> None:
        if self._closing:
            return
        if not rows:
            self._set_message(self.tr(
                "Could not reach the wiki.\n\nCheck your connection and press "
                "Refresh - pages you have already opened stay readable offline."))
            return
        self._list.blockSignals(True)
        self._list.clear()
        for kind, label, slug in rows:
            if kind == wiki_sync.SIDEBAR_PAGE:
                item = QListWidgetItem(label)
                item.setData(_SLUG_ROLE, slug)
            else:
                if self._list.count():
                    self._list.addItem(self._spacer_item())
                item = self._header_item(
                    self.tr("Other pages")
                    if kind == wiki_sync.SIDEBAR_OTHER else label)
            self._list.addItem(item)
        self._list.blockSignals(False)
        # Restore the page being read across a Refresh; otherwise open the
        # first page in the sidebar, skipping any group header above it.
        row = self._first_page_row()
        if self._current_slug:
            for i in range(self._list.count()):
                if self._list.item(i).data(_SLUG_ROLE) == self._current_slug:
                    row = i
                    break
        if row >= 0:
            self._list.setCurrentRow(row)

    def _header_item(self, label: str) -> QListWidgetItem:
        """Build a sidebar group header: bold, tinted and not selectable."""
        item = QListWidgetItem(label)
        item.setFlags(Qt.ItemIsEnabled)   # enabled, so it keeps its colour
        font = self._list.font()
        font.setBold(True)
        item.setFont(font)
        # TONE_BLUE, not TEXT_DIM: a dimmed header reads as a disabled page,
        # while the tint reads as a label. It is a light blue in every built-in
        # theme (cyan under cyberpunk, deeper on the light ones so it keeps its
        # contrast on white); ACCENT covers a custom theme that omits it.
        key = "TONE_BLUE" if self._pal.get("TONE_BLUE") else "ACCENT"
        item.setForeground(QColor(_c(self._pal, key)))
        return item

    @staticmethod
    def _spacer_item() -> QListWidgetItem:
        """Blank, inert row setting one sidebar group apart from the last.

        A size hint rather than a taller header: an item's own hint has to
        supply the width too, and an empty row is the one place where getting
        that width wrong cannot clip anything.
        """
        item = QListWidgetItem("")
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(QSize(0, _HEADER_GAP))
        return item

    def _first_page_row(self) -> int:
        """Row of the first selectable page, or -1 if the list holds none."""
        for i in range(self._list.count()):
            if self._list.item(i).data(_SLUG_ROLE):
                return i
        return -1

    def _on_page_ready(self, slug, text) -> None:
        if self._closing or slug != self._current_slug:
            return   # a different page was selected while this one downloaded
        if not text:
            self._set_message(self.tr(
                "Could not load “{0}”.\n\nPress Refresh to try again."
            ).format(slug))
            return
        was_refresh = self._awaiting_refresh
        self._awaiting_refresh = False
        unchanged = was_refresh and self._page_text.get(slug) == text
        self._page_text[slug] = text
        self._view.reset_images(drop_cached=was_refresh)
        # QTextEdit.setMarkdown takes no dialect argument - it always uses
        # MarkdownDialectGitHub, which is what the wiki is authored in.
        codes: list[str] = []
        self._view.setMarkdown(
            wiki_sync.to_display_markdown(text, share_codes=codes))
        self._share_codes = codes
        self._space_blocks()
        self._style_tables()
        self._view.moveCursor(QTextCursor.Start)
        self._view.verticalScrollBar().setValue(0)
        if was_refresh:
            # GitHub serves wiki markdown through a CDN with a five-minute TTL,
            # so a page edited moments ago can come back unchanged however hard
            # we ask. Say so rather than let Refresh look broken.
            self._set_status(self.tr(
                "No change yet - GitHub caches wiki pages for up to 5 minutes."
            ) if unchanged else self.tr("Updated."))
        if self._share_codes:
            # Said last so it wins over a Refresh note: the codes are the one
            # thing on a page that does something when clicked.
            self._set_status(self.tr("Click an import code to load it."))

    def _space_blocks(self) -> None:
        """Add paragraph spacing to the rendered page.

        Qt's markdown importer gives every block zero margins, so prose comes
        out visually cramped compared to how the same page reads on GitHub.
        Table cells are left tight: the padding set in :meth:`_style_tables`
        does that job, and prose margins on top of it make rows tower.
        """
        doc = self._view.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        marks: list[int] = []
        block = doc.begin()
        while block.isValid():
            fmt = block.blockFormat()
            cur.setPosition(block.position())
            if block.text().startswith(wiki_sync.CENTER_MARK):
                fmt.setAlignment(Qt.AlignHCenter)
                marks.append(block.position())
            if cur.currentTable() is not None:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(0)
            elif fmt.headingLevel():
                fmt.setTopMargin(14)
                fmt.setBottomMargin(4)
            elif block.textList() is not None:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(3)
            else:
                fmt.setTopMargin(0)
                fmt.setBottomMargin(10)
            cur.setBlockFormat(fmt)
            block = block.next()
        # Drop the centring marks last, back to front, so the positions
        # collected above stay valid as the document shrinks.
        for pos in reversed(marks):
            cur.setPosition(pos)
            cur.deleteChar()
        cur.endEditBlock()

    def _style_tables(self) -> None:
        """Give every table on the page a readable, consistent look.

        Qt's importer leaves tables in two different states - markdown pipe
        tables get a 1px *outset* (3D-looking) border and 4px padding, while a
        raw ``<table>`` block gets no border and no padding at all, so its
        columns run together. Both are restyled here into one flat, banded
        table that fills the page width.
        """
        doc = self._view.document()
        border = QColor(_c(self._pal, "BORDER"))
        header_bg = QColor(_c(self._pal, "BG_HEADER"))
        band_bg = QColor(_c(self._pal, "BG_ROW_ALT"))
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        for table in self._iter_tables(doc.rootFrame()):
            fmt = table.format()
            fmt.setBorder(1)
            fmt.setBorderStyle(QTextFrameFormat.BorderStyle_Solid)
            fmt.setBorderBrush(border)
            fmt.setBorderCollapse(True)
            fmt.setCellPadding(6)
            fmt.setCellSpacing(0)
            fmt.setHeaderRowCount(1)
            fmt.setWidth(QTextLength(QTextLength.PercentageLength, 100))
            table.setFormat(fmt)
            for row in range(table.rows()):
                if row and row % 2 == 0:
                    shade = band_bg
                elif row:
                    continue
                else:
                    shade = header_bg
                for col in range(table.columns()):
                    cell = table.cellAt(row, col)
                    cell_fmt = cell.format()
                    cell_fmt.setBackground(shade)
                    cell.setFormat(cell_fmt)
        cur.endEditBlock()

    @classmethod
    def _iter_tables(cls, frame):
        """Yield every QTextTable under *frame*, nested ones included."""
        for child in frame.childFrames():
            if isinstance(child, QTextTable):
                yield child
            yield from cls._iter_tables(child)

    def _on_image_ready(self, url, image) -> None:
        if self._closing or image is None:
            return
        self._view.set_image(url, image)

    # -------------------------------------------------------------- navigation

    def _on_row_changed(self, current, _previous) -> None:
        if current is None:
            return
        slug = current.data(_SLUG_ROLE)
        if not slug:
            return   # a group header, not a page
        if slug != self._current_slug:
            self._awaiting_refresh = False   # a Refresh result is no longer wanted
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
        self._awaiting_refresh = False
        self._current_slug = slug
        self._title.setText(slug.replace("-", " "))
        self._set_message(self.tr("Loading…"))
        self._start_page_fetch(slug, force=False)

    def _on_link(self, url: QUrl) -> None:
        """Follow wiki-internal links in place; send everything else to a browser."""
        if url.scheme() == wiki_sync.SHARE_CODE_SCHEME:
            self._request_import(url)
            return
        href = url.toString()
        slug = wiki_sync.resolve_link(href)
        if slug:
            self._load_slug(slug)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _request_import(self, url: QUrl) -> None:
        """Hand the clicked share code to the manager's import-code flow.

        The link only carries the code's index on the page - the code itself is
        a kilobyte of base64 - so a stale link (one clicked after the page was
        re-rendered) is simply ignored rather than importing the wrong thing.
        """
        try:
            code = self._share_codes[int(url.path())]
        except (ValueError, IndexError):
            return
        self.import_code_requested.emit(code)

    def _refresh(self) -> None:
        self._set_status(self.tr("Refreshing…"))
        self._start_list_fetch(force=True)
        if self._current_slug:
            self._awaiting_refresh = True
            self._start_page_fetch(self._current_slug, force=True)

    def _set_status(self, text: str) -> None:
        """Show a short-lived note next to the Refresh button."""
        self._status.setText(text)
        self._status_timer.start()

    def _open_in_browser(self) -> None:
        slug = self._current_slug or "Home"
        QDesktopServices.openUrl(QUrl(wiki_sync.page_web_url(slug)))

    # ------------------------------------------------------------------ helpers

    def _set_message(self, text: str) -> None:
        self._share_codes = []
        self._view.setMarkdown(text)

    def closeEvent(self, event):
        self._closing = True
        super().closeEvent(event)
