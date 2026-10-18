from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QCheckBox,
    QLabel, QPushButton,
)

from Utils.collections.options import CollectionInstallOptions
from Utils.collections.grouping import available_name, matching_profiles, pending_group, profile_path
from Utils.profiles import groups
from Utils.profiles.state import profile_uses_specific_mods, read_profile_settings, read_collection_install_paused
from gui_qt.i18n import profile_display


class _SetupCombo(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class CollectionSetup(QWidget):
    changed = Signal()
    convert_requested = Signal(str)

    def __init__(self, game, collection, parent=None):
        super().__init__(parent)
        self._game = game
        self._collection = collection
        self._refreshing = False
        self._pending_loaded = None
        self._suggested_name = ""
        self._suggested_key = None
        self._conversion_profile = ""
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._form = QFormLayout()
        self._form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        layout.addLayout(self._form)
        self.mode = _SetupCombo(self)
        self.mode.addItem(self.tr("Create a new profile"), "new")
        self.mode.addItem(self.tr("Append to existing profile"), "append")
        self.mode.addItem(self.tr("Group with"), "group")
        self.mode.model().item(2).setEnabled(bool(getattr(game, "profile_groups_supported", True)))
        self._form.addRow(self.tr("Install mode"), self.mode)
        self.target = _SetupCombo(self)
        self._form.addRow(self.tr("Target"), self.target)
        self.reuse = _SetupCombo(self)
        self._form.addRow(self.tr("Collection profile"), self.reuse)
        self.group_name = QLineEdit(self)
        self.group_name.setMaxLength(64)
        self._form.addRow(self.tr("Group name"), self.group_name)
        self.overwrite = QCheckBox(self.tr("Overwrite existing mods"), self)
        self.skip = QCheckBox(self.tr("Skip already installed mods"), self)
        layout.addWidget(self.overwrite)
        layout.addWidget(self.skip)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.convert = QPushButton(self.tr("Convert to profile-specific mods…"), self)
        self.convert.setObjectName("FormButton")
        self.convert.clicked.connect(lambda: self.convert_requested.emit(self._conversion_profile))
        layout.addWidget(self.convert)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.target.currentIndexChanged.connect(self._sync)
        self.reuse.currentIndexChanged.connect(self._sync)
        self.group_name.textEdited.connect(self._sync)
        self.overwrite.toggled.connect(self.changed)
        self.skip.toggled.connect(self.changed)
        self._profiles = []
        self._force_new = False
        self._sync()

    def refresh(self, domain, revision, force_new=False):
        from Utils.games.registry import _profiles_for_game
        self._refreshing = True
        self._force_new = force_new
        current_mode = self.mode.currentData()
        append_index = self.mode.findData("append")
        if force_new and append_index >= 0:
            self.mode.removeItem(append_index)
        elif not force_new and append_index < 0:
            self.mode.insertItem(1, self.tr("Append to existing profile"), "append")
        restored = self.mode.findData("new" if force_new and current_mode == "append" else current_mode)
        self.mode.setCurrentIndex(max(0, restored))
        self._profiles = []
        for name in _profiles_for_game(self._game.name):
            try:
                profile_path(self._game, name)
            except ValueError:
                continue
            self._profiles.append(name)
        self._fill_targets()
        old = self.reuse.currentData()
        self.reuse.clear()
        names = matching_profiles(self._game, domain, self._collection.slug, revision)
        for name in names:
            self.reuse.addItem(profile_display(name), name)
        if not names:
            self.reuse.addItem(self.tr("Create a new collection profile"), "")
        if old in names:
            self.reuse.setCurrentIndex(self.reuse.findData(old))
        key = (domain, self._collection.slug, revision)
        if self._pending_loaded != key:
            self._pending_loaded = key
            for name in names:
                pending = pending_group(profile_path(self._game, name))
                if pending:
                    self.mode.setCurrentIndex(self.mode.findData("group"))
                    self._fill_targets()
                    self.target.setCurrentIndex(self.target.findData(pending.target))
                    self.reuse.setCurrentIndex(self.reuse.findData(name))
                    self._suggested_name = ""
                    self.group_name.setText(pending.group_name)
                    break
        self._refreshing = False
        self._sync()

    def _fill_targets(self):
        old = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        grouping = self.mode.currentData() == "group"
        for name in self._profiles:
            try:
                path = profile_path(self._game, name)
            except ValueError:
                continue
            is_group = groups.is_group(path)
            if is_group and not grouping:
                continue
            label = (self.tr("Group: {0}") if is_group else self.tr("Profile: {0}")).format(profile_display(name))
            self.target.addItem(label, name)
        if self.target.findData(old) >= 0:
            self.target.setCurrentIndex(self.target.findData(old))
        self.target.blockSignals(False)

    def _mode_changed(self):
        if self._refreshing:
            return
        self._fill_targets()
        self._sync()

    def options(self) -> CollectionInstallOptions:
        name = self.target.currentData() or ""
        return CollectionInstallOptions(
            mode=self.mode.currentData(), target=name,
            group_name=self.group_name.text().strip(),
            reuse_profile=self.reuse.currentData() or "",
            target_is_group=bool(name and groups.is_group(profile_path(self._game, name))),
            overwrite_existing=self.overwrite.isChecked(), skip_existing=self.skip.isChecked())

    def _sync(self, *_args):
        if self._refreshing:
            return
        options = self.options()
        grouping = options.mode == "group"
        append = options.mode == "append"
        self._form.setRowVisible(self.target, append or grouping)
        self._form.setRowVisible(self.reuse, grouping)
        self._form.setRowVisible(self.group_name, grouping and not options.target_is_group)
        self.overwrite.setVisible(append)
        self.skip.setVisible(append)
        if grouping and not options.target_is_group:
            key = (options.target, options.reuse_profile)
            if (self._suggested_key is None
                    or (options.group_name == self._suggested_name
                        and key != self._suggested_key)):
                existing = next((name for name in self._profiles
                                 if options.reuse_profile and groups.get_members(profile_path(self._game, name))
                                 == [options.reuse_profile, options.target]), "")
                self._suggested_name = existing or available_name(
                    self._game, f"{options.target} + {self._collection.name or self._collection.slug}")
                self._suggested_key = key
                self.group_name.setText(self._suggested_name)
        self._conversion_profile = ""
        if grouping:
            for name in (options.target, options.reuse_profile):
                if name and not profile_uses_specific_mods(profile_path(self._game, name)):
                    self._conversion_profile = name
                    break
        self.convert.setVisible(bool(self._conversion_profile))
        self.convert.setEnabled(not self._busy)
        if self._conversion_profile:
            hint = self.tr("Convert '{0}' before grouping. Its mods will be stored in its own profile.").format(profile_display(self._conversion_profile))
        elif grouping:
            hint = self.tr("The collection keeps its own profile and gets highest member priority. Newer duplicate mod versions still win.")
            if options.reuse_profile:
                settings = read_profile_settings(profile_path(self._game, options.reuse_profile), None)
                report = settings.get("collection_install_report") or {}
                if read_collection_install_paused(profile_path(self._game, options.reuse_profile)) or report.get("missing_required") or report.get("failed_stages"):
                    hint += " " + self.tr("Complete this collection's installation before it is added to the group.")
                else:
                    hint += " " + self.tr("The existing collection profile will be reused.")
        elif self._force_new:
            hint = self.tr("This collection requires its own profile. It can also be combined through Group with.")
        else:
            hint = ""
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.changed.emit()

    def valid(self) -> bool:
        options = self.options()
        if self._busy:
            return False
        if options.mode == "new":
            return True
        if not options.target:
            return False
        if options.mode == "append":
            return not self._force_new
        return (bool(getattr(self._game, "profile_groups_supported", True))
                and not self._conversion_profile and options.target != options.reuse_profile
                and (options.target_is_group or bool(self.group_name.text().strip())))

    def set_busy(self, busy):
        self._busy = busy
        self.setEnabled(not busy)
        self._sync()
