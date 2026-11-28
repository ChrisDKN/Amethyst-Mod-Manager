"""Overlay for linking a hand-installed mod to its source page.

A dimmed child overlay (see gui_qt/dialogs/base.py) that previews the source
and its versions before stamping the provider identity. All parsing, network
and metadata writes run on a worker thread via ``run_in_worker``; only the
two object Signals arrive back on the UI thread.

``on_done`` receives the core's ``LinkResult`` on success, or ``None`` on
cancel / Esc / an unrecovered error.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox,
)

from gui_qt.dialogs.base import OverlayBase
from gui_qt.dialogs.confirm import ConfirmOverlay
from gui_qt.appearance.theme import active_palette, _c
from gui_qt.runtime.worker import run_in_worker
from Utils.app_log import app_log
from Utils.mods.source_link import (
    SourceLinkError, apply_link, read_source_link, resolve_link, verify_choice,
)


class SourceLinkOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 340
    MIN_H = 260
    CLICK_OUTSIDE_CANCELS = True

    resolve_ready = Signal(object)   # ResolvedLink | Exception
    apply_ready = Signal(object)     # LinkResult | SourceLinkError | Exception
    verify_ready = Signal(object)    # (seq, (verification, local_version)) | Exception

    def __init__(self, host: QWidget, *, game, profile, staging, mod,
                 api=None, on_done=None, origin_provider=None):
        super().__init__(host, on_done=on_done)
        self._game = game
        self._profile = Path(profile) if profile is not None else None
        self._mod = mod
        self._api = api
        self._mod_dir = (Path(staging) / mod) if staging is not None else None
        self._origin_provider = origin_provider
        self._origin = self._current_origin()
        self._resolved = None
        self._confirm = None
        self._linking = False
        self._verified = False
        self._verify_seq = 0

        self.resolve_ready.connect(self._on_resolve)
        self.apply_ready.connect(self._on_apply)
        self.verify_ready.connect(self._on_verify)

        p = active_palette()
        _card, v = self._make_card("SourceLinkCard")

        title = QLabel(self.tr("Link mod to source"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._edit = QLineEdit()
        if self._mod_dir is not None:
            self._edit.setText(
                read_source_link(self._mod_dir / "meta.ini").get("url", ""))
        self._edit.setPlaceholderText(self.tr("Source URL"))
        self._edit.returnPressed.connect(self._resolve)
        row.addWidget(self._edit, 1)
        self._resolve_btn = QPushButton(self.tr("Resolve"))
        self._resolve_btn.setObjectName("FormButton")
        self._resolve_btn.setCursor(Qt.PointingHandCursor)
        self._resolve_btn.clicked.connect(self._resolve)
        row.addWidget(self._resolve_btn)
        v.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        self._source = QLabel("")
        self._source.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        self._source.setWordWrap(True)
        v.addWidget(self._source)

        self._verification = QLabel("")
        self._verification.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:12px;")
        self._verification.setWordWrap(True)
        v.addWidget(self._verification)

        version_row = QHBoxLayout()
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(6)
        version_lbl = QLabel(self.tr("Version"))
        version_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        version_row.addWidget(version_lbl)
        self._combo = QComboBox()
        self._combo.setEnabled(False)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        version_row.addWidget(self._combo, 1)
        v.addLayout(version_row)

        self._warning = QLabel("")
        self._warning.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:12px;")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        v.addWidget(self._warning)
        self._anyway = QCheckBox(self.tr("Link anyway"))
        self._anyway.setVisible(False)
        self._anyway.toggled.connect(self._update_link_enabled)
        v.addWidget(self._anyway)

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._link_btn = QPushButton(self.tr("Link"))
        self._link_btn.setObjectName("PrimaryButton")
        self._link_btn.setCursor(Qt.PointingHandCursor)
        self._link_btn.setEnabled(False)
        self._link_btn.clicked.connect(self._apply)
        bar.addWidget(self._link_btn)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, *, game, profile, staging, mod, api=None,
                  on_done=None, origin_provider=None):
        top = host.window() if host is not None else None
        return cls(top or host, game=game, profile=profile, staging=staging,
                   mod=mod, api=api, on_done=on_done,
                   origin_provider=origin_provider)

    def _current_origin(self):
        if self._origin_provider is not None:
            return self._origin_provider()
        return (getattr(self._game, "name", ""), str(self._profile or ""))

    def _origin_changed(self) -> bool:
        return self._origin_provider is not None and self._current_origin() != self._origin

    def keyPressEvent(self, event):
        # A nested confirmation owns the keyboard while it is open. If focus
        # has not moved off this overlay yet, forward the key to it instead of
        # letting Esc tear down the whole link overlay.
        if self._confirm is not None:
            self._confirm.keyPressEvent(event)
            return
        # Never discard a completed (or in-flight) write on Esc.
        if self._linking:
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # Same guard for a backdrop click while the stamp is running.
        if self._linking:
            return
        super().mousePressEvent(event)

    def _stale(self) -> bool:
        if self._origin_changed():
            self._status.setText(self.tr("Game or profile changed. Start linking again."))
            self._resolved = None
            self._verified = False
            self._link_btn.setEnabled(False)
            return True
        return False

    # -- worker launches ----------------------------------------------------
    def _resolve(self):
        if self._stale():
            return
        url = self._edit.text().strip()
        if not url or self._mod_dir is None:
            self._status.setText(self.tr("Enter a source URL first."))
            return
        self._status.setText(self.tr("Resolving…"))
        self._resolved = None
        self._verified = False
        self._link_btn.setEnabled(False)

        def _work():
            try:
                return resolve_link(url, self._game, self._mod_dir, api=self._api)
            except Exception as exc:                     # noqa: BLE001
                return exc

        run_in_worker(_work, self.resolve_ready, name="source-link-resolve")

    def _apply(self, confirm_replace: bool = False):
        if self._stale():
            return
        if self._resolved is None or self._profile is None or self._mod_dir is None:
            return
        choice_id = self._combo.currentData()
        if not choice_id:
            return
        self._status.setText(self.tr("Linking…"))
        self._linking = True
        self._link_btn.setEnabled(False)

        def _work():
            try:
                return apply_link(
                    self._game, self._profile, self._mod_dir, self._resolved,
                    choice_id, api=self._api, confirm_replace=confirm_replace,
                    log=app_log)
            except Exception as exc:                     # noqa: BLE001
                return exc

        run_in_worker(_work, self.apply_ready, name="source-link-apply")

    # -- UI-thread result handlers -----------------------------------------
    def _on_resolve(self, result):
        self._linking = False
        if self._stale():
            return
        if isinstance(result, Exception):
            self._status.setText(getattr(result, "message", None) or str(result))
            return
        self._resolved = result
        self._combo.blockSignals(True)
        self._combo.clear()
        for choice in result.versions:
            self._combo.addItem(choice.label, choice.id)
        recommended = next(
            (i for i, choice in enumerate(result.versions) if choice.recommended),
            0)
        if result.versions:
            self._combo.setCurrentIndex(recommended)
        self._combo.setEnabled(bool(result.versions))
        self._combo.blockSignals(False)
        self._show_verification(result.verification, result.local_version,
                                result.warning)

    def _on_combo_changed(self, *_args):
        """A different version must be verified before Link is allowed again."""
        if (self._resolved is None or self._confirm is not None
                or self._linking):
            return
        choice_id = self._combo.currentData()
        if not choice_id:
            return
        self._verified = False
        self._link_btn.setEnabled(False)
        self._status.setText(self.tr("Verifying…"))
        self._verify_seq += 1
        seq = self._verify_seq

        def _work():
            try:
                return (seq, verify_choice(
                    self._game, self._mod_dir, self._resolved, choice_id,
                    api=self._api))
            except Exception as exc:                     # noqa: BLE001
                return (seq, exc)

        run_in_worker(_work, self.verify_ready, name="source-link-verify")

    def _on_verify(self, payload):
        seq, result = payload
        if seq != self._verify_seq or self._resolved is None or self._stale():
            return
        if isinstance(result, Exception):
            self._status.setText(getattr(result, "message", None) or str(result))
            return
        self._show_verification(result[0], result[1], "")

    def _show_verification(self, verification, local_version, warning=""):
        """Update the banner and the explicit-confirmation gate."""
        self._source.setText(self._source_line())
        verification = verification or "none"
        banner = self.tr("Verification: {0}").format(verification)
        if local_version:
            banner += "  ·  " + self.tr("Local version: {0}").format(local_version)
        self._verification.setText(banner)

        # A local version that doesn't verify against the chosen source is a
        # real mismatch (a fresh, unlinked mod has no local version at all).
        # Require an explicit "Link anyway" acknowledgement before stamping.
        mismatch = verification == "none" and bool(local_version)
        self._warning.setVisible(mismatch)
        self._anyway.setVisible(mismatch)
        if mismatch:
            self._anyway.setChecked(False)
            self._warning.setText(
                self.tr("The installed version does not match this source."))
            self._status.setText("")
        elif warning:
            self._status.setText(warning)
        else:
            self._status.setText(self.tr("Ready to link."))
        self._verified = True
        self._update_link_enabled()

    def _source_line(self) -> str:
        if self._resolved is None:
            return ""
        candidate = self._resolved.candidate
        parts = [candidate.provider]
        if candidate.name:
            parts.append(candidate.name)
        version = self._combo.currentText()
        if version:
            parts.append(version)
        return "  ·  ".join(parts)

    def _on_apply(self, result):
        self._linking = False
        if self._stale():
            # The write may have landed in another game/profile; never deliver
            # the stale result to that context.
            return
        if isinstance(result, SourceLinkError) and result.code == "source_conflict":
            def _decided(yes):
                self._confirm = None
                if yes:
                    self._apply(confirm_replace=True)
                else:
                    self._status.setText(self.tr("Link cancelled."))
                    self._update_link_enabled()

            confirm = ConfirmOverlay.show_over(
                self,
                self.tr("Replace existing source?"),
                result.message,
                _decided,
                confirm_label=self.tr("Replace"),
                danger=False)
            self._confirm = confirm
            # Clear the guard when the confirm is destroyed by any path, not
            # just through _decided, so Esc cannot leave it dangling.
            confirm.destroyed.connect(lambda *_: setattr(self, "_confirm", None))
            confirm.setFocusPolicy(Qt.StrongFocus)
            confirm.setFocus()
            return
        if isinstance(result, Exception):
            self._status.setText(getattr(result, "message", None) or str(result))
            self._update_link_enabled()
            return
        self._finish(result)

    def _update_link_enabled(self, *_args):
        ready = self._resolved is not None and self._verified and not self._linking
        if self._warning.isVisible() and not self._anyway.isChecked():
            ready = False
        self._link_btn.setEnabled(ready)
