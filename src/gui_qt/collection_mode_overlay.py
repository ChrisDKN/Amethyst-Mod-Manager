"""Collection installation mode overlays."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QRadioButton, QCheckBox, QComboBox,
    QButtonGroup,
)

from gui_qt.overlay_base import OverlayBase
from Utils.collections.options import CollectionInstallOptions
from gui_qt.i18n import profile_display
from gui_qt.theme_qt import active_palette, _c


class _BaseModeOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 300
    MIN_W = 360
    MIN_H = 220

    def __init__(self, host, on_done):
        super().__init__(host, on_done=on_done)
        self._p = active_palette()
        _card, self._v = self._make_card("_ModeCard", margins=(20, 16, 20, 16))

    def _c(self, k):
        return _c(self._p, k)


class ModeOverlay(_BaseModeOverlay):
    CARD_H = 320

    def __init__(self, host, profiles, on_done, force_new_profile: bool = False):
        super().__init__(host, on_done)
        self._profiles = list(profiles or [])
        self._force_new = bool(force_new_profile)
        self._build()
        self._present()

    @classmethod
    def show_over(cls, host, profiles, on_done, force_new_profile: bool = False):
        top = host.window() if host is not None else None
        return cls(top or host, profiles, on_done, force_new_profile=force_new_profile)

    def _build(self):
        v = self._v

        title = QLabel(self.tr("Install Collection"), self._card)
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        sub = QLabel(self.tr("How would you like to install this collection?"), self._card)
        sub.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        self._group = QButtonGroup(self._card)
        self._new_radio = QRadioButton(self.tr("Create a new profile"), self._card)
        self._new_radio.setChecked(True)
        self._group.addButton(self._new_radio)
        v.addWidget(self._new_radio)

        # When the manifest requires a new profile, the Append section is omitted
        # entirely (Tk parity) - only the note is shown.
        self._append_radio = None
        self._profile_combo = None
        self._overwrite_cb = None
        self._skip_cb = None

        if self._force_new:
            note = QLabel(
                self.tr("This collection requires a new profile and cannot be "
                "appended to an existing one."), self._card)
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
            v.addWidget(note)
        else:
            self._append_radio = QRadioButton(
                self.tr("Append to existing profile"), self._card)
            self._group.addButton(self._append_radio)
            v.addWidget(self._append_radio)
            # Append controls (indented) - enabled only when Append is selected.
            self._profile_combo = QComboBox(self._card)
            # Show the translated name, carry the FOLDER name as item data - the
            # pick is read back with currentData() and used as a profile name.
            if self._profiles:
                for _p in self._profiles:
                    self._profile_combo.addItem(profile_display(_p), _p)
            else:
                self._profile_combo.addItem(self.tr("(no profiles)"), None)
            v.addWidget(self._profile_combo)
            self._overwrite_cb = QCheckBox(self.tr("Overwrite existing mods"), self._card)
            v.addWidget(self._overwrite_cb)
            self._skip_cb = QCheckBox(self.tr("Skip already installed mods"), self._card)
            v.addWidget(self._skip_cb)
            self._new_radio.toggled.connect(self._sync_append_state)
            self._append_radio.toggled.connect(self._sync_append_state)
            self._sync_append_state()

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        install = QPushButton(self.tr("Install"), self._card)
        install.setObjectName("PrimaryButton")
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self._on_install)
        bar.addWidget(install)
        v.addLayout(bar)

    def _sync_append_state(self):
        if self._append_radio is None:
            return
        is_append = self._append_radio.isChecked()
        has_profiles = bool(self._profiles)
        for w in (self._profile_combo, self._overwrite_cb, self._skip_cb):
            w.setEnabled(is_append and has_profiles)

    def _on_install(self):
        # A forced-new collection has no Append section - only a new profile.
        if self._force_new or self._append_radio is None \
                or self._new_radio.isChecked() \
                or not self._append_radio.isChecked():
            self._finish(CollectionInstallOptions())
            return
        # Append
        if not self._profiles:
            return
        profile = self._profile_combo.currentData()
        if not profile:
            return
        self._finish(CollectionInstallOptions(
            mode="append", target=profile, overwrite_existing=self._overwrite_cb.isChecked(),
            skip_existing=self._skip_cb.isChecked()))


class ContinueOverlay(_BaseModeOverlay):
    CARD_H = 220

    def __init__(self, host, profile_name, on_done):
        super().__init__(host, on_done)
        self._profile_name = profile_name
        self._build()
        self._present()

    @classmethod
    def show_over(cls, host, profile_name, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, profile_name, on_done)

    def _build(self):
        v = self._v
        v.setSpacing(10)

        title = QLabel(self.tr("Continue Collection Install"), self._card)
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        msg = QLabel(
            self.tr("This collection is already installed in profile '{0}'.").format(self._profile_name), self._card)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:13px;")
        v.addWidget(msg)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        cont = QPushButton(self.tr("Continue Install"), self._card)
        cont.setObjectName("PrimaryButton")
        cont.setCursor(Qt.PointingHandCursor)
        cont.clicked.connect(
            lambda: self._finish(CollectionInstallOptions(mode="continue", target=self._profile_name)))
        bar.addWidget(cont)
        v.addLayout(bar)
