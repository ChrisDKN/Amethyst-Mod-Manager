"""Borderless in-window overlays for the profile share-code feature.

``ShareCodeExportOverlay`` - shows a generated share code in a read-only, word-
wrapped text box with a "Copy to clipboard" button (the code is copied to the
clipboard automatically on open too).

``ShareCodeImportOverlay`` - a multi-line paste box; ``on_done(code)`` on Import
or ``on_done(None)`` on Cancel / Esc / backdrop click.

Both are child overlays via gui_qt/overlay_base.py, sharing a small local base
with the title/sub/text-area builders.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
)

from gui_qt.custom_game_view import deploy_type_display
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, close_button, _c


class _CodeOverlayBase(OverlayBase):
    CARD_W = 560
    CARD_H = 320
    MIN_W = 380
    MIN_H = 200
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, on_done=None):
        super().__init__(host, on_done=on_done)
        _card, self._v = self._make_card("ShareCodeCard")

    def _p(self):
        return active_palette()

    def _title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_c(self._p(),'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        return lbl

    def _sub(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{_c(self._p(),'TEXT_DIM')}; font-size:13px;")
        lbl.setWordWrap(True)
        return lbl

    def _text_area(self, read_only: bool) -> QPlainTextEdit:
        p = self._p()
        area = QPlainTextEdit()
        area.setReadOnly(read_only)
        area.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        area.setStyleSheet(
            f"QPlainTextEdit {{ background:{_c(p,'BG_DEEP')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:5px; padding:6px; font-family:monospace; }}")
        return area


class ShareCodeExportOverlay(_CodeOverlayBase):
    """Show a generated share code with a Copy-to-clipboard button. The code is
    also copied to the clipboard automatically on open.

    A code for a big modlist is tens of kilobytes, which chat apps handle badly,
    so "Get link" uploads it to a paste host and swaps the box for a short URL.
    That upload is always an explicit click - it publishes the profile and mod
    names to a third-party server - and if it fails the raw code is still sitting
    on the clipboard, so the overlay can never leave the user with nothing."""

    CARD_H = 360

    # Worker → main thread. The upload runs off the UI thread; Qt widgets may
    # only be touched from the main thread, so the result comes back by signal.
    _upload_done = Signal(str, str)   # (url, error) - exactly one is non-empty

    def __init__(self, host: QWidget, code: str, mod_count: int, on_copy=None):
        super().__init__(host)
        self._code = code
        self._on_copy = on_copy
        self._url = ""
        self._uploading = False

        self._v.addWidget(self._title(self.tr("Export code")))
        noun = "mod" if mod_count == 1 else "mods"
        self._v.addWidget(self._sub(self.tr(
            "Share this code with someone to send them your load order "
            "({0} {1}). They can add it with Import code.").format(mod_count, noun)))

        self._area = self._text_area(read_only=True)
        self._area.setPlainText(code)
        self._v.addWidget(self._area, 1)

        # Upload row: what it does, where it goes, and how long it lasts. The
        # host is named up front so the click is an informed one.
        from Utils.profile_export import PASTE_HOST, RETENTION_NOTE
        self._status = self._sub(self.tr(
            "Too long to paste? Get a short link instead - this "
            "uploads the code to {0}, where anyone with the link can read it. "
            "The link stops working {1}."
        ).format(PASTE_HOST, RETENTION_NOTE))
        self._v.addWidget(self._status)

        up = QHBoxLayout()
        up.addStretch(1)
        self._link_btn = QPushButton(self.tr("Get link"))
        self._link_btn.setObjectName("FormButton")
        self._link_btn.setCursor(Qt.PointingHandCursor)
        self._link_btn.clicked.connect(self._get_link)
        up.addWidget(self._link_btn)
        self._v.addLayout(up)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = close_button(self.tr("Close"))
        close.clicked.connect(lambda: self._finish(None))
        bar.addWidget(close)
        self._copy_btn = QPushButton(self.tr("Copy to clipboard"))
        self._copy_btn.setObjectName("PrimaryButton")
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy)
        bar.addWidget(self._copy_btn)
        self._v.addLayout(bar)

        self._upload_done.connect(self._on_upload_done)

        self._present()
        self._copy()   # auto-copy on open

    def _copy(self):
        """Copy whatever the box currently holds - the code, or the link once
        one has been generated."""
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self._url or self._code)
        self._copy_btn.setText(self.tr("Copied ✓"))
        if callable(self._on_copy):
            self._on_copy()

    def _get_link(self):
        if self._uploading:
            return
        self._uploading = True
        self._link_btn.setEnabled(False)
        self._status.setText(self.tr("Uploading…"))
        code = self._code

        def _work():
            try:
                from Utils.profile_export import upload_code
                url = upload_code(code)
            except Exception as exc:
                self._safe_emit(self._upload_done, "", str(exc))
                return
            self._safe_emit(self._upload_done, url, "")

        threading.Thread(target=_work, daemon=True).start()

    def _safe_emit(self, signal, *args):
        """Emit from the worker thread, ignoring a C++-deleted overlay (the user
        can close this before the upload returns)."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _on_upload_done(self, url: str, error: str):
        self._uploading = False
        if error:
            # The raw code is still in the box and on the clipboard - the user
            # loses nothing, so this is a note rather than a failure.
            self._link_btn.setEnabled(True)
            self._status.setText(self.tr(
                "Could not get a link ({0}). The code above still works - copy "
                "and send that instead.").format(error))
            return
        self._url = url
        self._area.setPlainText(url)
        self._link_btn.setText(self.tr("Link ready"))
        self._link_btn.setEnabled(False)
        self._status.setText(self.tr(
            "Anyone with this link can import your load order. Paste it into "
            "Import code."))
        self._copy_btn.setText(self.tr("Copy to clipboard"))
        self._copy()   # put the link on the clipboard, replacing the raw code


class ShareCodeImportOverlay(_CodeOverlayBase):
    """A multi-line paste box for importing a share code. ``on_done(code)`` on
    Import, ``on_done(None)`` on Cancel / Esc / backdrop click.

    *initial_code* prefills the box - used when the code came from somewhere in
    the app (a code clicked in the wiki tab) rather than the user's clipboard.
    The overlay is still shown, so the preview says what is about to be
    imported before anything is built.

    A link to a hosted code is accepted too: pasting one swaps the Import button
    for Fetch, which downloads the code and drops it into the box, from where the
    normal preview-then-import path takes over."""

    # Worker → main thread; see the note on ShareCodeExportOverlay.
    _fetch_done = Signal(str, str)   # (code, error) - exactly one is non-empty

    def __init__(self, host: QWidget, on_done, initial_code: str = ""):
        super().__init__(host, on_done=on_done)
        self._fetching = False

        self._v.addWidget(self._title(self.tr("Import code")))
        self._v.addWidget(self._sub(self.tr(
            "Paste a share code below to build a new profile from someone "
            "else's load order. A link to a code works too.")))

        self._area = self._text_area(read_only=False)
        self._area.setPlaceholderText("AMMCODE1:…")  # i18n: skip - share-code format token
        # Prefill BEFORE connecting textChanged: _update_preview drives the
        # primary button, which does not exist yet. The explicit call at the end
        # of __init__ previews the prefilled code once everything is built.
        if initial_code:
            self._area.setPlainText(initial_code)
        self._v.addWidget(self._area, 1)

        # Live preview of the decoded code - profile / game / mod count / size /
        # export date - so the user sees what they're importing before committing.
        self._preview = self._sub("")
        self._v.addWidget(self._preview)
        self._area.textChanged.connect(self._update_preview)

        # Offer to paste the clipboard contents in one tap - pointless once the
        # box is already filled in.
        cb = QGuiApplication.clipboard()
        clip = cb.text() if cb is not None else ""
        bar = QHBoxLayout()
        # Offer the clipboard for a code OR a link - both are things a recipient
        # is handed to paste in here.
        from Utils.profile_export import is_code_url
        clip_ok = bool(clip) and (clip.strip().startswith("AMMCODE")
                                  or is_code_url(clip))
        if clip_ok and not initial_code:
            paste = QPushButton(self.tr("Paste from clipboard"))
            paste.setObjectName("FormButton")
            paste.setCursor(Qt.PointingHandCursor)
            paste.clicked.connect(lambda: self._area.setPlainText(clip))
            bar.addWidget(paste)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._ok = QPushButton(self.tr("Import"))
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._ok.clicked.connect(self._confirm)
        bar.addWidget(self._ok)
        self._v.addLayout(bar)

        self._fetch_done.connect(self._on_fetch_done)

        self._present()
        self._area.setFocus()
        self._update_preview()   # initial_code may already fill the box

    def _update_preview(self):
        text = self._area.toPlainText().strip()
        if not text:
            self._preview.setText("")
            self._set_fetch_mode(False)
            return
        # A link can't be decoded - it has to be downloaded first. Flip the
        # primary button to Fetch and wait for the click.
        from Utils.profile_export import is_code_url
        if is_code_url(text):
            self._set_fetch_mode(True)
            self._preview.setText(self.tr(
                "A link - press Fetch to download the code from it."))
            return
        self._set_fetch_mode(False)
        try:
            from Utils.profile_export import decode_manifest
            manifest = decode_manifest(text)
        except Exception:
            self._preview.setText(self.tr("Not a valid share code."))
            return
        info = manifest.get("info") or {}
        mods = manifest.get("mods") or []
        parts = []
        name = (info.get("name") or "").strip()
        if name:
            parts.append(name)
        game = (info.get("gameName") or info.get("domainName") or "").strip()
        if game:
            parts.append(game)
        noun = "mod" if len(mods) == 1 else "mods"
        counts = f"{len(mods)} {noun}"
        total = int(info.get("totalSize") or 0)
        if total:
            from Utils.collection_manifest import fmt_size
            counts += self.tr(", ~{0} to download").format(fmt_size(total))
        parts.append(counts)
        exported = (info.get("exported") or "")[:10]
        if exported:
            parts.append(self.tr("exported {0}").format(exported))
        self._preview.setText("  -  ".join(parts))

    def _set_fetch_mode(self, on: bool):
        """Swap the primary button between Fetch (box holds a link) and Import
        (box holds a code)."""
        self._fetch_mode = on
        self._ok.setText(self.tr("Fetch") if on else self.tr("Import"))

    def _confirm(self):
        text = self._area.toPlainText().strip()
        if not text or self._fetching:
            return
        if getattr(self, "_fetch_mode", False):
            self._start_fetch(text)
            return
        self._finish(text)

    def _start_fetch(self, url: str):
        self._fetching = True
        self._ok.setEnabled(False)
        self._preview.setText(self.tr("Downloading…"))

        def _work():
            try:
                from Utils.profile_export import fetch_code_url
                code = fetch_code_url(url)
            except Exception as exc:
                self._safe_emit(self._fetch_done, "", str(exc))
                return
            self._safe_emit(self._fetch_done, code, "")

        threading.Thread(target=_work, daemon=True).start()

    def _safe_emit(self, signal, *args):
        """Emit from the worker thread, ignoring a C++-deleted overlay."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _on_fetch_done(self, code: str, error: str):
        self._fetching = False
        self._ok.setEnabled(True)
        if error:
            self._preview.setText(error)
            return
        # Dropping the code in fires textChanged → _update_preview, which leaves
        # fetch mode and shows the real modlist preview.
        self._area.setPlainText(code)


class CustomGameExportOverlay(_CodeOverlayBase):
    """Show a generated custom-game share code with a Copy-to-clipboard button.
    The code is also copied to the clipboard automatically on open."""

    def __init__(self, host: QWidget, code: str, game_name: str = "", on_copy=None):
        super().__init__(host)
        self._code = code
        self._on_copy = on_copy

        self._v.addWidget(self._title(self.tr("Export game")))
        if game_name:
            sub = self.tr(
                "Share this code to send someone your \"{0}\" custom game setup. "
                "They can add it with Import code in Define Custom Game."
            ).format(game_name)
        else:
            sub = self.tr(
                "Share this code to send someone this custom game setup. They can "
                "add it with Import code in Define Custom Game.")
        self._v.addWidget(self._sub(sub))

        self._area = self._text_area(read_only=True)
        self._area.setPlainText(code)
        self._v.addWidget(self._area, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = close_button(self.tr("Close"))
        close.clicked.connect(lambda: self._finish(None))
        bar.addWidget(close)
        self._copy_btn = QPushButton(self.tr("Copy to clipboard"))
        self._copy_btn.setObjectName("PrimaryButton")
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy)
        bar.addWidget(self._copy_btn)
        self._v.addLayout(bar)

        self._present()
        self._copy()   # auto-copy on open

    def _copy(self):
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self._code)
        self._copy_btn.setText(self.tr("Copied ✓"))
        if callable(self._on_copy):
            self._on_copy()


class CustomGameImportOverlay(_CodeOverlayBase):
    """A multi-line paste box for importing a custom-game share code.
    ``on_done(defn)`` on Import with the decoded definition dict, or
    ``on_done(None)`` on Cancel / Esc / backdrop click."""

    def __init__(self, host: QWidget, on_done):
        super().__init__(host, on_done=on_done)
        self._defn = None

        self._v.addWidget(self._title(self.tr("Import game")))
        self._v.addWidget(self._sub(self.tr(
            "Paste a share code below to prefill the form from another custom "
            "game's setup. You still need to give it a unique name.")))

        self._area = self._text_area(read_only=False)
        self._area.setPlaceholderText("AMMGAME1:…")  # i18n: skip - share-code format token
        self._v.addWidget(self._area, 1)

        self._preview = self._sub("")
        self._v.addWidget(self._preview)
        self._area.textChanged.connect(self._update_preview)

        cb = QGuiApplication.clipboard()
        clip = cb.text() if cb is not None else ""
        bar = QHBoxLayout()
        if clip and clip.strip().startswith("AMMGAME"):
            paste = QPushButton(self.tr("Paste from clipboard"))
            paste.setObjectName("FormButton")
            paste.setCursor(Qt.PointingHandCursor)
            paste.clicked.connect(lambda: self._area.setPlainText(clip))
            bar.addWidget(paste)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._ok = QPushButton(self.tr("Import"))
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._ok.clicked.connect(self._confirm)
        self._ok.setEnabled(False)
        bar.addWidget(self._ok)
        self._v.addLayout(bar)

        self._present()
        self._area.setFocus()

    def _update_preview(self):
        text = self._area.toPlainText().strip()
        self._defn = None
        if not text:
            self._preview.setText("")
            self._ok.setEnabled(False)
            return
        try:
            from Games.Custom.custom_game import decode_custom_game_definition
            defn = decode_custom_game_definition(text)
        except Exception:
            self._preview.setText(self.tr("Not a valid game code."))
            self._ok.setEnabled(False)
            return
        self._defn = defn
        self._ok.setEnabled(True)
        parts = []
        name = (defn.get("name") or "").strip()
        if name:
            parts.append(name)
        exe = (defn.get("exe_name") or "").strip()
        if exe:
            parts.append(exe)
        dep = deploy_type_display(defn.get("deploy_type"))
        if dep:
            parts.append(self.tr("{0} deploy").format(dep))
        self._preview.setText("  -  ".join(parts))

    def _confirm(self):
        if self._defn is not None:
            self._finish(self._defn)
