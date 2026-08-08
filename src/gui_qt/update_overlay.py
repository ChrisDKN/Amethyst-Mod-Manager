"""App-update banner card (Tk parity: gui.py `_show_update_overlay`).

A centered card floated over the modlist panel with NO dimmed backdrop -
the app stays fully usable behind it (unlike ``ConfirmOverlay``). The card
is the whole widget: it is parented to the host panel and re-centered on
host resizes via an event filter, so clicks outside the card land on the
panel as normal.

*mode* is one of ``"appimage"``, ``"flatpak"``, or ``"aur"``.
*is_downgrade* swaps the copy when the offered version is older than the
running one (e.g. user opted out of the pre-release channel while running
a beta).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.version_check import _APP_UPDATE_RELEASES_URL, _AUR_PACKAGE_URL


class UpdateOverlay(QFrame):

    def __init__(self, host, current: str, latest: str, *,
                 mode: str = "appimage",
                 is_prerelease: bool = False,
                 is_downgrade: bool = False,
                 on_update=None,
                 on_close=None):
        super().__init__(host)
        self._host = host
        self._on_close = on_close
        p = active_palette()

        self.setObjectName("UpdateCard")
        self.setStyleSheet(
            f"#UpdateCard {{ background:{_c(p,'BG_DEEP')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        if mode == "aur":
            msg = self.tr(
                "A new version of Amethyst Mod Manager is available on the AUR.\n\n"
                "Current: {0}\n"
                "AUR:     {1}\n\n"
                "Update via your AUR helper, e.g.\n"
                "  yay -Syu amethyst-mod-manager").format(current, latest)
        elif is_downgrade:
            offered_label = self.tr("Pre-release") if is_prerelease else self.tr("Stable")
            msg = self.tr(
                "You're running a pre-release. Switch to the latest {0} build?\n\n"
                "Current:     {1}\n"
                "{2}: {3}\n\n"
                "This will downgrade your installation.").format(
                    offered_label.lower(), current, offered_label, latest)
        else:
            msg = self.tr(
                "A new version of Amethyst Mod Manager is available.\n\n"
                "Current: {0}\n"
                "Latest:  {1}").format(current, latest)

        body = QLabel(msg)
        body.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-size:13px;")
        body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        v.addWidget(body)

        # Opt-out (all modes): ticking suppresses the startup update banner.
        # Saved immediately - no confirm needed; re-enable via Settings, and a
        # manual pre-release toggle still forces a check regardless.
        mute = QCheckBox(self.tr("Don't notify me about new versions"))
        mute.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")

        def _mute_toggled(checked: bool):
            from Utils.ui_config import save_update_notifications
            try:
                save_update_notifications(not checked)
            except Exception:
                pass

        mute.toggled.connect(_mute_toggled)
        v.addWidget(mute)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        if mode in ("appimage", "flatpak"):
            def _do_update():
                self.close_overlay()
                if on_update is not None:
                    on_update()

            upd = QPushButton(self.tr("Switch to stable") if is_downgrade
                              else self.tr("Update via installer"))
            upd.setObjectName("PrimaryButton")
            upd.setCursor(Qt.PointingHandCursor)
            upd.clicked.connect(_do_update)
            bar.addWidget(upd)

        if mode == "aur":
            aur = QPushButton(self.tr("Open AUR page"))
            aur.setObjectName("PrimaryButton")
            aur.setCursor(Qt.PointingHandCursor)
            aur.clicked.connect(lambda: self._open_and_close(_AUR_PACKAGE_URL))
            bar.addWidget(aur)
        else:
            # AppImage & Flatpak both now have a primary "Update" button above,
            # so the releases-page link is secondary for each.
            rel = QPushButton(self.tr("Open releases page"))
            rel.setObjectName("FormButton")
            rel.setCursor(Qt.PointingHandCursor)
            rel.clicked.connect(lambda: self._open_and_close(_APP_UPDATE_RELEASES_URL))
            bar.addWidget(rel)

        later = QPushButton(self.tr("Later"))
        later.setObjectName("FormButton")
        later.setCursor(Qt.PointingHandCursor)
        later.clicked.connect(self.close_overlay)
        bar.addWidget(later)

        bar.addStretch(1)
        v.addLayout(bar)

        host.installEventFilter(self)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()

    # -- internals ----------------------------------------------------------
    def _open_and_close(self, url: str):
        from Utils.xdg import open_url
        open_url(url)
        self.close_overlay()

    def close_overlay(self):
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        cb = self._on_close
        self._on_close = None
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(self)

    def _reposition(self):
        self.adjustSize()
        self.move((self._host.width() - self.width()) // 2,
                  (self._host.height() - self.height()) // 2)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return False
