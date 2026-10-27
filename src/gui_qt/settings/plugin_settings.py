from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QWidget)

from amethyst_plugins.catalog import load_settings, reset_settings, save_settings, settings_error
from amethyst_plugins.i18n import translate
from gui_qt.dialogs.base import OverlayBase
from .plugin_fields import SettingField


class PluginSettingsOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 540

    def __init__(self, host, plugin, on_done=None):
        super().__init__(host, on_done=on_done)
        self.plugin = plugin
        card, layout = self._make_card("PluginSettingsCard")
        title = QLabel(self.tr("{0} settings").format(translate(plugin, plugin.name)), card)
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFormAlignment(Qt.AlignTop)
        saved = load_settings(plugin)
        self.fields = {}
        for setting in plugin.settings:
            field = SettingField(plugin, setting, saved[setting["key"]], content)
            label = QLabel(translate(plugin, setting["label"]), content)
            label.setTextFormat(Qt.PlainText)
            form.addRow(label, field)
            self.fields[setting["key"]] = field
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.status = QLabel(card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setText(self.tr("Icon changes take effect after restarting Amethyst.")
                            if plugin.icon_overrides else
                            self.tr("Settings are used on the plugin's next run or refresh."))
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        defaults = QPushButton(self.tr("Restore defaults"), card)
        defaults.clicked.connect(self._defaults)
        buttons.addWidget(defaults)
        buttons.addStretch()
        cancel = QPushButton(self.tr("Cancel"), card)
        cancel.clicked.connect(lambda: self._finish(False))
        buttons.addWidget(cancel)
        self.save_button = QPushButton(self.tr("Save"), card)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        problem = settings_error(plugin)
        if problem:
            self.status.setText(self.tr("Plugin settings could not be read: {0}").format(problem))
            self.save_button.setEnabled(False)
            self.recover_button = QPushButton(self.tr("Reset damaged settings"), card)
            self.recover_button.clicked.connect(self._recover)
            buttons.insertWidget(1, self.recover_button)
        layout.addLayout(buttons)
        self._present()

    def _defaults(self):
        for setting in self.plugin.settings:
            field = self.fields[setting["key"]]
            field.set_value(setting["default"])

    def _save(self):
        values = {key: field.value() for key, field in self.fields.items()}
        try:
            save_settings(self.plugin, values)
        except (OSError, ValueError) as exc:
            self.status.setText(self.tr("Could not save plugin settings: {0}").format(exc))
            return
        self._finish(True)

    def _recover(self):
        try:
            backup = reset_settings(self.plugin)
        except (OSError, ValueError) as exc:
            self.status.setText(self.tr("Could not reset plugin settings: {0}").format(exc))
            return
        self._defaults()
        self.recover_button.setEnabled(False)
        self.save_button.setEnabled(True)
        self.status.setText(self.tr("Settings reset. Original settings saved to {0}.").format(backup)
                            if backup else self.tr("Settings reset."))
