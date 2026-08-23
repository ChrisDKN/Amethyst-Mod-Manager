"""Native, in-manager detail page for a Nexus mod.

The Nexus browser already receives most of the information needed to render a
useful mod page.  This view presents that information without embedding the
Nexus website (and therefore without depending on Qt WebEngine or website
cookies).  The description and file list are refreshed from the authenticated
API when needed; the real website remains available through an explicit button.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import (
    QByteArray, QBuffer, QIODevice, Qt, QT_TRANSLATE_NOOP, Signal, QTimer, QUrl)
from PySide6.QtGui import QColor, QImage, QMovie, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHeaderView, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from gui_qt.nexus_mod_card import ThumbnailLoader, _fmt_count
from gui_qt.nexus_bbcode import nexus_bbcode_to_html
from gui_qt.theme_qt import _c, active_palette, contrast_text
from gui_qt.worker import run_in_worker
from Utils.collection_manifest import fmt_size


class _DescriptionBrowser(QTextBrowser):
    """QTextBrowser that fetches remote BBCode/HTML images asynchronously."""

    _image_ready = Signal(str, object)
    _IMAGE_MARGIN = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self._images: dict[str, QImage] = {}
        # URL -> (backing byte buffer, active movie). QMovie reads lazily, so
        # both objects must stay alive for as long as the description view.
        self._animations: dict[str, tuple[QBuffer, QMovie]] = {}
        self._requested: set[str] = set()
        self._placeholder = QImage(1, 1, QImage.Format_ARGB32)
        self._placeholder.fill(Qt.transparent)
        self._image_ready.connect(self._on_image_ready)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._rescale_images)

    def setHtml(self, html_text: str) -> None:
        self._requested.clear()
        super().setHtml(html_text)

    def loadResource(self, resource_type, url):
        if resource_type == QTextDocument.ImageResource:
            key = url.toString()
            if url.scheme().lower() in ("http", "https"):
                image = self._images.get(key)
                if image is not None:
                    return self._fit_image(image)
                if key not in self._requested:
                    self._requested.add(key)
                    self._request_image(key)
                return self._placeholder
        return super().loadResource(resource_type, url)

    def _request_image(self, url: str) -> None:
        def fetch():
            result = QImage()
            try:
                import requests
                from Utils.ca_bundle import resolve_ca_bundle
                response = requests.get(
                    url, timeout=15, verify=resolve_ca_bundle() or True,
                    headers={"User-Agent": "Amethyst Mod Manager"})
                if response.ok:
                    payload = bytes(response.content)
                    # QImage intentionally decodes only one frame. Preserve GIF
                    # bytes so QMovie can animate them on the GUI thread.
                    result = payload if payload.startswith(
                        (b"GIF87a", b"GIF89a")) else QImage.fromData(payload)
            except Exception:
                pass
            return url, result

        run_in_worker(
            fetch, self._image_ready, name="nexus-description-image",
            unpack=True, error_result=(url, QImage()))

    def _on_image_ready(self, url: str, image) -> None:
        if isinstance(image, (bytes, bytearray)):
            self._start_gif(url, bytes(image))
            return
        if image is None or image.isNull():
            return
        self._set_image_resource(url, image)

    def _start_gif(self, url: str, payload: bytes) -> None:
        if url in self._animations:
            return
        buffer = QBuffer(self)
        buffer.setData(QByteArray(payload))
        if not buffer.open(QIODevice.ReadOnly):
            return
        movie = QMovie(buffer, QByteArray(b"gif"), self)
        movie.setCacheMode(QMovie.CacheAll)
        if not movie.isValid():
            fallback = QImage.fromData(payload)
            if not fallback.isNull():
                self._set_image_resource(url, fallback)
            movie.deleteLater()
            buffer.deleteLater()
            return
        self._animations[url] = (buffer, movie)
        movie.frameChanged.connect(
            lambda _frame, key=url, active=movie: self._on_gif_frame(key, active))
        movie.start()

    def _on_gif_frame(self, url: str, movie: QMovie) -> None:
        image = movie.currentImage()
        if not image.isNull():
            self._set_image_resource(url, image)

    def _set_image_resource(self, url: str, image: QImage) -> None:
        self._images[url] = image
        self.document().addResource(
            QTextDocument.ImageResource, QUrl(url), self._fit_image(image))
        self.document().markContentsDirty(0, self.document().characterCount())
        self.viewport().update()

    def _fit_image(self, image: QImage) -> QImage:
        available = max(1, self.viewport().width() - self._IMAGE_MARGIN)
        if image.width() <= available:
            return image
        return image.scaledToWidth(available, Qt.SmoothTransformation)

    def _rescale_images(self) -> None:
        for url, image in self._images.items():
            self.document().addResource(
                QTextDocument.ImageResource, QUrl(url), self._fit_image(image))
        if self._images:
            self.document().markContentsDirty(0, self.document().characterCount())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._images:
            self._resize_timer.start()


def _date_text(iso_value: str = "", timestamp: int = 0) -> str:
    """Return a stable, human-readable date from either Nexus date shape."""
    try:
        if iso_value:
            dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        elif timestamp:
            dt = datetime.fromtimestamp(int(timestamp), timezone.utc)
        else:
            return ""
        return dt.strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError, OverflowError):
        return ""


def _file_size(file) -> int:
    """Return a NexusModFile's size in bytes, whichever API field supplied it."""
    raw = getattr(file, "size_in_bytes", None)
    if raw:
        return int(raw)
    return int(getattr(file, "size_kb", 0) or 0) * 1024


_FILE_CATEGORY_ORDER = {
    "MAIN": 0,
    "MISCELLANEOUS": 1,
    "OPTIONAL": 2,
    "UPDATE": 3,
    "OLD_VERSION": 4,
    "ARCHIVED": 5,
}


# lupdate only extracts QT_TRANSLATE_NOOP when the literal is spelled out at
# the call, so the canonical labels below are listed here one by one. The dict
# stays English (it is the display fallback); translation happens where the
# label is rendered, via self.tr(_category_label(...)).
_TR_MARKERS = (
    QT_TRANSLATE_NOOP("NexusModDetailView", "Main files"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Miscellaneous files"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Optional files"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Update files"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Old versions"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Archived files"),
    QT_TRANSLATE_NOOP("NexusModDetailView", "Other files"),
)


def _category_label(category: str) -> str:
    category = (category or "OTHER").upper()
    labels = {
        "MAIN": "Main files",
        "MISCELLANEOUS": "Miscellaneous files",
        "OPTIONAL": "Optional files",
        "UPDATE": "Update files",
        "OLD_VERSION": "Old versions",
        "ARCHIVED": "Archived files",
        "OTHER": "Other files",
    }
    return labels.get(category, category.replace("_", " ").title())


class NexusModDetailView(QWidget):
    """A native detail page shown over :class:`NexusBrowserView`."""

    back_requested = Signal()
    _info_ready = Signal(object, str)
    _files_ready = Signal(object, str)

    _BANNER_W = 420
    _BANNER_H = 220

    def __init__(self, api, entry, *, domain: str, on_install=None,
                 on_install_file=None, installed_file_ids=None,
                 is_installed: bool = False, download_only: bool = False,
                 log_fn=None, parent=None):
        super().__init__(parent)
        self._api = api
        self._entry = entry
        self._domain = (getattr(entry, "domain_name", "") or domain or "").strip()
        self._on_install = on_install
        self._on_install_file = on_install_file
        self._installed_file_ids = {
            int(value) for value in (installed_file_ids or ()) if int(value or 0) > 0}
        self._file_buttons: dict[int, QPushButton] = {}
        self._log = log_fn or (lambda _message: None)
        self._download_only = bool(download_only)
        self._installed = bool(is_installed)
        self._watching = False
        self._current_picture = ""
        self._description_source = ""
        self._expanded_spoilers: set[int] = set()

        self.setObjectName("NexusModDetailView")
        self.setAutoFillBackground(True)
        self._build()
        self._info_ready.connect(self._on_info_ready)
        self._files_ready.connect(self._on_files_ready)

        self._thumbs = ThumbnailLoader(
            self, crop_w=self._BANNER_W, crop_h=self._BANNER_H)
        self._thumbs.loaded.connect(self._on_thumbnail)

        self._set_info(entry)
        self._load_remote_data()

    def _build(self) -> None:
        p = active_palette()
        self.setStyleSheet(
            f"#NexusModDetailView{{background:{_c(p, 'BG_MAIN')};}}"
            f"QTabWidget::pane{{background:{_c(p, 'BG_PANEL')};"
            f"border:1px solid {_c(p, 'BORDER')};}}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("HeaderBar")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(8)

        back = QToolButton()
        back.setText(self.tr("← Back to mods"))
        back.setObjectName("ActionButton")
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(self.back_requested)
        hl.addWidget(back)

        self._header_title = QLabel()
        self._header_title.setStyleSheet(
            f"font-size:15px; font-weight:600; color:{_c(p, 'TEXT_MAIN')};")
        hl.addWidget(self._header_title, 1)

        external = QToolButton()
        external.setText(self.tr("Open on Nexus ↗"))
        external.setObjectName("ActionButton")
        external.setCursor(Qt.PointingHandCursor)
        external.clicked.connect(self._open_on_nexus)
        hl.addWidget(external)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("NexusModDetailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            f"#NexusModDetailScroll{{background:{_c(p, 'BG_MAIN')};}}"
            f"#NexusModDetailScroll > QWidget > QWidget{{background:{_c(p, 'BG_MAIN')};}}")
        body = QWidget()
        body.setObjectName("NexusModDetailBody")
        body.setStyleSheet(
            f"#NexusModDetailBody{{background:{_c(p, 'BG_MAIN')};}}")
        bv = QVBoxLayout(body)
        bv.setContentsMargins(18, 16, 18, 16)
        bv.setSpacing(14)

        intro = QSplitter(Qt.Horizontal)
        intro.setChildrenCollapsible(False)
        self._image = QLabel(self.tr("Loading image…"))
        self._image.setFixedSize(self._BANNER_W, self._BANNER_H)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setStyleSheet(
            f"background:{_c(p, 'BG_DEEP')}; color:{_c(p, 'TEXT_DIM')};"
            f"border:1px solid {_c(p, 'BORDER')}; border-radius:6px;")
        intro.addWidget(self._image)

        summary_box = QWidget()
        sv = QVBoxLayout(summary_box)
        sv.setContentsMargins(14, 2, 4, 2)
        sv.setSpacing(7)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet(
            f"font-size:22px; font-weight:650; color:{_c(p, 'TEXT_MAIN')};")
        sv.addWidget(self._title)
        self._byline = QLabel()
        self._byline.setStyleSheet(f"color:{_c(p, 'ACCENT')}; font-size:12px;")
        sv.addWidget(self._byline)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._summary.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')}; font-size:13px;")
        sv.addWidget(self._summary, 1)
        self._facts = QLabel()
        self._facts.setWordWrap(True)
        self._facts.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:12px;")
        sv.addWidget(self._facts)

        self._install_btn = QPushButton()
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(self._install)
        sv.addWidget(self._install_btn)
        self._apply_install_style()
        intro.addWidget(summary_box)
        intro.setStretchFactor(0, 0)
        intro.setStretchFactor(1, 1)
        intro.setSizes([self._BANNER_W, 700])
        bv.addWidget(intro)

        tabs = QTabWidget()
        self._description = _DescriptionBrowser()
        self._description.setOpenLinks(False)
        self._description.setOpenExternalLinks(False)
        self._description.setStyleSheet(
            f"QTextBrowser{{background:{_c(p, 'BG_DEEP')};"
            f"color:{_c(p, 'TEXT_MAIN')}; border:1px solid {_c(p, 'BORDER')};"
            "padding:6px;}")
        self._description.anchorClicked.connect(self._open_link)
        self._description.document().setDefaultStyleSheet(
            f"body{{color:{_c(p, 'TEXT_MAIN')}; font-size:13px;}}"
            f"a{{color:{_c(p, 'ACCENT')};}}"
            f"h1,h2,h3{{color:{_c(p, 'TEXT_MAIN')};}}"
            f"blockquote{{color:{_c(p, 'TEXT_DIM')}; border-left:3px solid {_c(p, 'BORDER')};"
            "padding-left:8px;}}")
        tabs.addTab(self._description, self.tr("Description"))

        files_page = QWidget()
        fv = QVBoxLayout(files_page)
        fv.setContentsMargins(6, 8, 6, 6)
        self._files_status = QLabel(self.tr("Loading files…"))
        self._files_status.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        fv.addWidget(self._files_status)
        self._files = QTableWidget(0, 5)
        self._files.setHorizontalHeaderLabels([
            self.tr("Name"), self.tr("Version"), self.tr("Size"),
            self.tr("Uploaded"), "",
        ])
        self._files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._files.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._files.setAlternatingRowColors(True)
        self._files.setSortingEnabled(False)
        self._files.verticalHeader().setVisible(False)
        self._files.setStyleSheet(
            f"QTableWidget{{background:{_c(p, 'BG_DEEP')};"
            f"alternate-background-color:{_c(p, 'BG_ROW_ALT')};"
            f"color:{_c(p, 'TEXT_MAIN')}; border:1px solid {_c(p, 'BORDER')};}}"
            f"QTableWidget::item:selected{{background:{_c(p, 'BG_SELECT')};}}"
            f"QHeaderView::section{{background:{_c(p, 'BG_HEADER')};"
            f"color:{_c(p, 'TEXT_MAIN')}; border:1px solid {_c(p, 'BORDER')};"
            "padding:4px;}")
        fh = self._files.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            fh.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        fv.addWidget(self._files, 1)
        tabs.addTab(files_page, self.tr("Files"))
        tabs.setMinimumHeight(360)
        bv.addWidget(tabs, 1)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _set_info(self, info) -> None:
        name = getattr(info, "name", "") or self.tr("Mod {0}").format(
            getattr(info, "mod_id", 0))
        self._header_title.setText(name)
        self._title.setText(name)
        self._summary.setText(getattr(info, "summary", "") or self.tr("No summary provided."))

        author = (getattr(info, "uploaded_by", "") or
                  getattr(info, "author", "") or self.tr("Unknown author"))
        category = getattr(info, "category_name", "") or ""
        self._byline.setText(
            self.tr("by {0}  •  {1}").format(author, category) if category
            else self.tr("by {0}").format(author))

        created = _date_text(getattr(info, "created_at", ""),
                             getattr(info, "created_timestamp", 0))
        updated = _date_text(getattr(info, "updated_at", ""),
                             getattr(info, "updated_timestamp", 0))
        facts = [
            self.tr("Version: {0}").format(getattr(info, "version", "") or "—"),
            self.tr("Endorsements: {0}").format(
                _fmt_count(getattr(info, "endorsement_count", 0))),
            self.tr("Downloads: {0}").format(
                _fmt_count(getattr(info, "downloads_total", 0))),
        ]
        if created:
            facts.append(self.tr("Uploaded: {0}").format(created))
        if updated:
            facts.append(self.tr("Updated: {0}").format(updated))
        self._facts.setText("  •  ".join(facts))

        description = (getattr(info, "description", "") or "").strip()
        if description:
            if description != self._description_source:
                self._description_source = description
                self._expanded_spoilers.clear()
            self._render_description()
        elif not self._description.toPlainText().strip():
            self._description.setHtml(
                f"<p><i>{self.tr('Loading description…')}</i></p>")

        picture = (getattr(info, "picture_url", "") or "").strip()
        if picture and picture != self._current_picture:
            self._current_picture = picture
            self._thumbs.request(getattr(info, "mod_id", 0), picture)
        elif not picture and not self._current_picture:
            self._image.setText(self.tr("No image available"))

    def _load_remote_data(self) -> None:
        domain = self._domain
        mod_id = int(getattr(self._entry, "mod_id", 0) or 0)
        # Browse results include the description. Tracked/endorsed batch rows do
        # not, so only spend a REST request when the page actually needs it.
        if not (getattr(self._entry, "description", "") or "").strip():
            run_in_worker(
                lambda: (self._api.get_mod(domain, mod_id), ""),
                self._info_ready, name="nexus-mod-detail",
                unpack=True, error_result=(None, self.tr("Description unavailable.")))
        run_in_worker(
            lambda: (list(self._api.get_mod_files(domain, mod_id).files), ""),
            self._files_ready, name="nexus-mod-detail-files",
            unpack=True, error_result=([], self.tr("Could not load the file list.")))

    def _on_info_ready(self, info, error: str) -> None:
        if info is not None:
            # The REST detail endpoint supplies the long description but omits
            # a few values present on the GraphQL browse card. Keep those card
            # values instead of replacing useful metadata with zero/blank.
            for field in (
                    "downloads_total", "endorsement_count", "uploaded_by",
                    "uploader_id", "category_name", "created_at", "updated_at",
                    "created_timestamp", "updated_timestamp", "file_size_kb"):
                if not getattr(info, field, None):
                    try:
                        setattr(info, field, getattr(self._entry, field, None))
                    except Exception:
                        pass
            self._set_info(info)
        elif error:
            self._description.setHtml(f"<p><i>{error}</i></p>")

    def _on_files_ready(self, files, error: str) -> None:
        files = list(files or [])
        grouped: dict[str, list] = {}
        for file in files:
            category = (getattr(file, "category_name", "") or "OTHER").upper()
            grouped.setdefault(category, []).append(file)
        categories = sorted(
            grouped, key=lambda category: (
                _FILE_CATEGORY_ORDER.get(category, 99), _category_label(category)))
        for category_files in grouped.values():
            category_files.sort(
                key=lambda file: -(getattr(file, "uploaded_timestamp", 0) or 0))

        self._file_buttons.clear()
        self._files.clearSpans()
        self._files.setRowCount(len(files) + len(categories))
        p = active_palette()
        row = 0
        for category in categories:
            category_files = grouped[category]
            heading = QTableWidgetItem(
                self.tr("{0} ({1})").format(
                    self.tr(_category_label(category)), len(category_files)))
            font = heading.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() + 0.5)
            heading.setFont(font)
            heading.setForeground(QColor(_c(p, "TEXT_ON_ACCENT")))
            heading.setBackground(QColor(_c(p, "ACCENT")))
            heading.setFlags(Qt.ItemIsEnabled)
            self._files.setItem(row, 0, heading)
            self._files.setSpan(row, 0, 1, 5)
            self._files.setRowHeight(row, 32)
            row += 1

            for file in category_files:
                uploaded = _date_text(
                    timestamp=getattr(file, "uploaded_timestamp", 0))
                values = [
                    getattr(file, "name", "") or
                    getattr(file, "file_name", "") or "—",
                    getattr(file, "version", "") or "—",
                    fmt_size(_file_size(file)) if _file_size(file) else "—",
                    uploaded or "—",
                ]
                description = (getattr(file, "description", "") or "").strip()
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if description:
                        item.setToolTip(description)
                    self._files.setItem(row, column, item)
                button = QPushButton()
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(
                    lambda _checked=False, selected=file: self._install_file(selected))
                button.setEnabled(self._on_install_file is not None)
                self._files.setCellWidget(row, 4, button)
                self._file_buttons[int(getattr(file, "file_id", 0) or 0)] = button
                self._apply_file_button_style(button, file)
                self._files.setRowHeight(row, 34)
                row += 1
        if error:
            self._files_status.setText(error)
        else:
            self._files_status.setText(
                self.tr("{0} file(s)").format(len(files)))

    def _on_thumbnail(self, mod_id: int, pixmap) -> None:
        if mod_id != int(getattr(self._entry, "mod_id", 0) or 0):
            return
        self._image.setText("")
        self._image.setPixmap(pixmap)

    def _mod_url(self) -> str:
        return f"https://www.nexusmods.com/{self._domain}/mods/{self._entry.mod_id}"

    def _open_on_nexus(self) -> None:
        from Utils.xdg import open_url
        open_url(self._mod_url(), log_fn=self._log)

    def _open_link(self, url: QUrl) -> None:
        target = url.toString()
        if target.lower().startswith("nexus-spoiler:"):
            try:
                spoiler_id = int(target.split(":", 1)[1])
            except (TypeError, ValueError):
                return
            if spoiler_id in self._expanded_spoilers:
                self._expanded_spoilers.remove(spoiler_id)
            else:
                self._expanded_spoilers.add(spoiler_id)
            # A raw scrollbar value is not stable when collapsing content
            # changes the document's total height. Keep the clicked toggle at
            # the same viewport Y coordinate instead.
            before_y = self._anchor_viewport_y(target)
            old_position = self._description.verticalScrollBar().value()
            self._render_description()

            def restore_position():
                bar = self._description.verticalScrollBar()
                after_y = self._anchor_viewport_y(target)
                if before_y is not None and after_y is not None:
                    bar.setValue(max(0, min(
                        bar.maximum(), bar.value() + after_y - before_y)))
                else:
                    bar.setValue(min(old_position, bar.maximum()))

            QTimer.singleShot(0, restore_position)
            return
        if url.scheme().lower() not in ("http", "https"):
            self._log(f"Nexus: ignored unsupported description link: {url.toString()}")
            return
        from Utils.xdg import open_url
        open_url(url.toString(), log_fn=self._log)

    def _render_description(self) -> None:
        if not self._description_source:
            return
        self._description.setHtml(nexus_bbcode_to_html(
            self._description_source, self._expanded_spoilers))

    def _anchor_viewport_y(self, href: str):
        """Viewport Y coordinate of the first text fragment for *href*."""
        block = self._description.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    char_format = fragment.charFormat()
                    if char_format.isAnchor() and char_format.anchorHref() == href:
                        cursor = QTextCursor(self._description.document())
                        cursor.setPosition(fragment.position())
                        return self._description.cursorRect(cursor).top()
                iterator += 1
            block = block.next()
        return None

    def _install(self) -> None:
        if self._on_install is not None:
            self._on_install(self._entry)

    def _install_file(self, file) -> None:
        if self._on_install_file is not None:
            self._on_install_file(self._entry, file)

    def set_installed(self, installed: bool) -> None:
        self._installed = bool(installed)
        self._apply_install_style()

    @property
    def mod_id(self) -> int:
        return int(getattr(self._entry, "mod_id", 0) or 0)

    def set_download_only(self, download_only: bool) -> None:
        self._download_only = bool(download_only)
        self._apply_install_style()
        self._refresh_file_button_styles()

    def set_installed_files(self, file_ids) -> None:
        self._installed_file_ids = {
            int(value) for value in (file_ids or ()) if int(value or 0) > 0}
        self._refresh_file_button_styles()

    def set_watching(self, watching: bool) -> None:
        self._watching = bool(watching)
        self._apply_install_style()
        self._refresh_file_button_styles()

    def _refresh_file_button_styles(self) -> None:
        for file_id, button in self._file_buttons.items():
            self._apply_file_button_style(button, None, file_id=file_id)

    def _apply_file_button_style(self, button: QPushButton, file=None,
                                 *, file_id: int = 0) -> None:
        p = active_palette()
        file_id = int(file_id or getattr(file, "file_id", 0) or 0)
        if self._watching:
            colour = hover = _c(p, "BTN_DANGER")
            text = self.tr("Cancel")
        elif file_id in self._installed_file_ids:
            colour = _c(p, "BTN_WARN")
            hover = _c(p, "BTN_WARN_HOV")
            text = self.tr("Redownload") if self._download_only else self.tr("Reinstall")
        else:
            colour = _c(p, "BTN_SUCCESS")
            hover = _c(p, "BTN_SUCCESS_HOV")
            text = self.tr("Download") if self._download_only else self.tr("Install")
        button.setText(text)
        button.setStyleSheet(
            f"QPushButton{{background:{colour}; color:{contrast_text(colour)};"
            "font-weight:600; border:none; border-radius:4px; padding:4px 10px;}"
            f"QPushButton:hover{{background:{hover};}}")

    def _apply_install_style(self) -> None:
        p = active_palette()
        if self._watching:
            colour = hover = _c(p, "BTN_DANGER")
            text = self.tr("Cancel")
        elif self._installed:
            colour = _c(p, "BTN_WARN")
            hover = _c(p, "BTN_WARN_HOV")
            text = self.tr("Redownload") if self._download_only else self.tr("Reinstall")
        else:
            colour = _c(p, "BTN_SUCCESS")
            hover = _c(p, "BTN_SUCCESS_HOV")
            text = self.tr("Download") if self._download_only else self.tr("Install")
        self._install_btn.setText(text)
        self._install_btn.setStyleSheet(
            f"QPushButton{{background:{colour}; color:{contrast_text(colour)};"
            "font-weight:600; border:none; border-radius:4px; padding:7px 14px;}"
            f"QPushButton:hover{{background:{hover};}}")
