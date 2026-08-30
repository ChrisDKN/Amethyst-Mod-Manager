"""Overlay for uploading the session log to a paste host.

A log is the thing a user is asked for when reporting a bug, and it is far too
long to paste into a GitHub issue or a Discord message. This uploads it and
hands back a short URL.

Unlike a share code, a log is NOT something the user authored deliberately: it
carries file paths (so, usually, their username), game install locations and
whatever the session happened to do. So this overlay is a confirmation step -
it states what is about to be published and where, shows the size, and offers a
scrubbing option, before any bytes leave the machine.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit, QCheckBox,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, close_button, _c


class LogUploadOverlay(OverlayBase):
    """Confirm-then-upload the log, ending on a copyable URL.

    ``on_done(url)`` fires with the resulting URL, or ``None`` if the user
    cancelled. The overlay stays open after a successful upload so the link can
    be read and copied.
    """

    CARD_W = 560
    CARD_H = 340
    MIN_W = 380
    MIN_H = 220
    CLICK_OUTSIDE_CANCELS = True

    # Worker → main thread. Qt widgets may only be touched on the GUI thread.
    _upload_done = Signal(str, str)   # (url, error) - exactly one is non-empty

    def __init__(self, host: QWidget, log_text: str, on_done=None):
        super().__init__(host, on_done=on_done)
        self._log_text = log_text or ""
        self._url = ""
        self._uploading = False
        _card, self._v = self._make_card("LogUploadCard")

        p = active_palette()
        title = QLabel(self.tr("Upload log"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        self._v.addWidget(title)

        from Utils.paste_upload import (
            PASTE_HOST, RETENTION_NOTE, MAX_UPLOAD_BYTES)
        size = len(self._log_text.encode("utf-8", "replace"))
        lines = self._log_text.count("\n") + 1 if self._log_text else 0
        detail = self.tr(
            "This uploads your session log ({0} lines, {1}) to {2}, where "
            "anyone with the link can read it. Logs contain file paths, which "
            "usually include your username. The link stops working {3}."
        ).format(lines, _fmt_size(size), PASTE_HOST, RETENTION_NOTE)
        if size > MAX_UPLOAD_BYTES:
            detail += " " + self.tr(
                "Only the most recent {0} will be uploaded."
            ).format(_fmt_size(MAX_UPLOAD_BYTES))
        self._sub = QLabel(detail)
        self._sub.setWordWrap(True)
        self._sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        self._v.addWidget(self._sub)

        # Result box - empty until an upload succeeds, then holds the URL.
        self._area = QPlainTextEdit()
        self._area.setReadOnly(True)
        self._area.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._area.setPlaceholderText(
            self.tr("The link will appear here once the log is uploaded."))
        self._area.setStyleSheet(
            f"QPlainTextEdit {{ background:{_c(p,'BG_DEEP')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
            f" border-radius:5px; padding:6px; font-family:monospace; }}")
        self._v.addWidget(self._area, 1)

        self._scrub = QCheckBox(self.tr("Replace my username with \"user\""))
        self._scrub.setChecked(True)
        self._scrub.setCursor(Qt.PointingHandCursor)
        self._scrub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        self._v.addWidget(self._scrub)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._cancel = close_button(self.tr("Cancel"), pal=p)
        self._cancel.clicked.connect(lambda: self._finish(self._url or None))
        bar.addWidget(self._cancel)
        self._ok = QPushButton(self.tr("Upload"))
        self._ok.setObjectName("PrimaryButton")
        self._ok.setCursor(Qt.PointingHandCursor)
        self._ok.clicked.connect(self._upload)
        bar.addWidget(self._ok)
        self._v.addLayout(bar)

        self._upload_done.connect(self._on_upload_done)
        self._present()

    # -- upload ---------------------------------------------------------------
    def _upload(self):
        if self._uploading:
            return
        if self._url:            # already uploaded - button is "Copy link"
            self._copy()
            return
        self._uploading = True
        self._ok.setEnabled(False)
        self._scrub.setEnabled(False)
        self._sub.setText(self.tr("Uploading…"))
        text = _scrub_home(self._log_text) if self._scrub.isChecked() \
            else self._log_text

        def _work():
            try:
                from Utils.paste_upload import upload_text
                url = upload_text(text)
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
        self._ok.setEnabled(True)
        if error:
            # Nothing was published and the log is still on disk - a note, not
            # a failure the user has to recover from.
            self._scrub.setEnabled(True)
            self._sub.setText(self.tr(
                "Could not upload ({0}). The log is still saved locally - use "
                "Open Log Folder to attach the file instead.").format(error))
            return
        self._url = url
        self._area.setPlainText(url)
        self._sub.setText(self.tr(
            "Uploaded. Anyone with this link can read the log."))
        self._ok.setText(self.tr("Copy link"))
        self._cancel.setText(self.tr("Close"))
        self._copy()

    def _copy(self):
        cb = QGuiApplication.clipboard()
        if cb is not None and self._url:
            cb.setText(self._url)
        self._ok.setText(self.tr("Copied ✓"))


def _scrub_home(text: str) -> str:
    """Replace the user's home directory and bare username with placeholders.

    Best-effort privacy pass, not a guarantee - a log can name a user in ways
    this can't see. The checkbox wording promises only the username swap.
    """
    import os
    import re
    out = text or ""
    home = os.path.expanduser("~")
    user = os.path.basename(home)
    if home and home != "/":
        out = out.replace(home, "/home/user")
    if user and len(user) > 2:
        # Word-boundary so a username that is a common substring doesn't
        # shred unrelated words.
        out = re.sub(rf"\b{re.escape(user)}\b", "user", out)
    return out


def _fmt_size(n: int) -> str:
    """Bytes → a short human string ("847 KB")."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
