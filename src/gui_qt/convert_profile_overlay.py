"""ConvertProfileOverlay — Profile Settings' "Convert…" action: turn a
standard (shared mod pool) profile into a profile-specific one, or back,
either in place or by copying the chosen mods into a brand-new profile.

Backed by ``Utils.profile_convert``; see that module's docstring for the
actual file-copy mechanics. This overlay only collects the user's choices —
which mods, in place vs. a new profile, and (for a new profile) its name and
kind — matching the existing borderless in-window overlay pattern (see
``gui_qt/collection_mode_overlay.py``).

``CLICK_OUTSIDE_CANCELS`` stays at the base class default (False): this card
has enough fields (a mod checklist, radios, a name field) that discarding it
on a stray click — e.g. one intended to switch to a different tab, which
still lands on this topmost dimmed backdrop first — would silently throw away
real work. Only the explicit Cancel button (or Esc) dismisses it.

``on_done(result)`` is called with a dict or ``None`` (cancelled):
  {"scope": [mod_name, ...], "mode": "in_place"}
  {"scope": [mod_name, ...], "mode": "new", "new_name": str, "new_specific": bool}
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QButtonGroup,
    QCheckBox, QListWidget, QListWidgetItem,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ConvertProfileOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 540
    MIN_W = 360
    MIN_H = 380

    def __init__(self, host, profile_name: str, currently_specific: bool,
                mod_entries, on_done, *, allow_in_place: bool = True):
        super().__init__(host, on_done=on_done)
        self._p = active_palette()
        self._profile_name = profile_name
        self._currently_specific = currently_specific
        self._mod_entries = list(mod_entries)
        self._enabled_snapshot = {name: enabled for name, enabled in self._mod_entries}
        self._allow_in_place = allow_in_place
        _card, self._v = self._make_card("_ConvertCard", margins=(20, 16, 20, 16))
        self._build()
        self._present()

    @classmethod
    def show_over(cls, host, profile_name, currently_specific, mod_entries, on_done,
                  *, allow_in_place: bool = True):
        top = host.window() if host is not None else None
        return cls(top or host, profile_name, currently_specific, mod_entries, on_done,
                   allow_in_place=allow_in_place)

    def _c(self, k):
        return _c(self._p, k)

    def _build(self):
        v = self._v
        direction = (self.tr("profile-specific → standard") if self._currently_specific
                    else self.tr("standard → profile-specific"))
        title = QLabel(
            self.tr("Convert '{0}' ({1})").format(self._profile_name, direction),
            self._card)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:15px;")
        v.addWidget(title)

        hint = QLabel(
            self.tr("Choose which mods to include, then whether to convert "
                    "this profile in place or copy them into a brand-new "
                    "profile. Mods left unchecked are never touched or "
                    "removed from the shared pool. Disabled mods start "
                    "unchecked — use \"Check all\" to include them too."),
            self._card)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        v.addWidget(hint)

        self._list = QListWidget(self._card)
        for name, enabled in sorted(self._mod_entries, key=lambda t: t[0].lower()):
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
            self._list.addItem(it)
        v.addWidget(self._list, 1)

        check_bar = QHBoxLayout()
        all_btn = QPushButton(self.tr("Check all"), self._card)
        all_btn.setObjectName("FormButton")
        all_btn.setCursor(Qt.PointingHandCursor)
        all_btn.clicked.connect(lambda: self._set_all_checked(True))
        check_bar.addWidget(all_btn)
        none_btn = QPushButton(self.tr("Check none"), self._card)
        none_btn.setObjectName("FormButton")
        none_btn.setCursor(Qt.PointingHandCursor)
        none_btn.clicked.connect(lambda: self._set_all_checked(False))
        check_bar.addWidget(none_btn)
        restore_btn = QPushButton(self.tr("Restore enabled"), self._card)
        restore_btn.setObjectName("FormButton")
        restore_btn.setCursor(Qt.PointingHandCursor)
        restore_btn.setToolTip(
            self.tr("Reset the checklist back to exactly this profile's "
                    "currently-enabled mods."))
        restore_btn.clicked.connect(self._restore_enabled)
        check_bar.addWidget(restore_btn)
        check_bar.addStretch(1)
        v.addLayout(check_bar)

        self._mode_group = QButtonGroup(self._card)
        self._in_place_radio = QRadioButton(
            self.tr("Convert this profile in place"), self._card)
        self._in_place_radio.setEnabled(self._allow_in_place)
        if not self._allow_in_place:
            self._in_place_radio.setToolTip(
                self.tr("The default profile can't be converted in place — "
                        "copy it to a new profile instead."))
        self._mode_group.addButton(self._in_place_radio)
        v.addWidget(self._in_place_radio)

        self._new_radio = QRadioButton(self.tr("Copy to a new profile"), self._card)
        self._mode_group.addButton(self._new_radio)
        v.addWidget(self._new_radio)

        new_row = QHBoxLayout()
        self._name_edit = QLineEdit(self._card)
        self._name_edit.setPlaceholderText(self.tr("New profile name"))
        new_row.addWidget(self._name_edit, 1)
        self._new_specific_chk = QCheckBox(self.tr("Use Profile Specific Mods"), self._card)
        self._new_specific_chk.setChecked(not self._currently_specific)
        new_row.addWidget(self._new_specific_chk)
        v.addLayout(new_row)

        self._in_place_radio.toggled.connect(self._sync_new_row)
        self._new_radio.toggled.connect(self._sync_new_row)
        # Default to whichever mode is actually available; setChecked() only
        # fires toggled() when the state actually flips, so sync explicitly
        # afterward too — this must land right no matter the signal timing.
        (self._in_place_radio if self._allow_in_place else self._new_radio).setChecked(True)
        self._sync_new_row()

        v.addStretch(1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        go = QPushButton(self.tr("Convert"), self._card)
        go.setObjectName("PrimaryButton")
        go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(self._on_convert)
        bar.addWidget(go)
        v.addLayout(bar)

    def _sync_new_row(self):
        is_new = self._new_radio.isChecked()
        self._name_edit.setEnabled(is_new)
        self._new_specific_chk.setEnabled(is_new)

    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def _restore_enabled(self):
        for i in range(self._list.count()):
            it = self._list.item(i)
            it.setCheckState(
                Qt.Checked if self._enabled_snapshot.get(it.text(), False) else Qt.Unchecked)

    def _checked_names(self) -> list:
        return [self._list.item(i).text() for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.Checked]

    def _on_convert(self):
        names = self._checked_names()
        if not names:
            return
        if self._new_radio.isChecked():
            new_name = self._name_edit.text().strip()
            if not new_name:
                return
            self._finish({"scope": names, "mode": "new", "new_name": new_name,
                         "new_specific": self._new_specific_chk.isChecked()})
        elif self._allow_in_place:
            self._finish({"scope": names, "mode": "in_place"})
