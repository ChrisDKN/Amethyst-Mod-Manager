from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout,
                               QLineEdit, QPushButton, QSpinBox, QWidget)

from amethyst_plugins.i18n import translate


class SettingField(QWidget):
    def __init__(self, plugin, setting, value, parent):
        super().__init__(parent)
        self.setting = setting
        kind = setting["type"]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if kind == "boolean":
            self.control = QCheckBox(self)
        elif kind == "choice":
            self.control = QComboBox(self)
            for choice in setting["choices"]:
                self.control.addItem(translate(plugin, choice), choice)
        elif kind in ("integer", "number"):
            self.control = QSpinBox(self) if kind == "integer" else QDoubleSpinBox(self)
            bound = 2147483647 if kind == "integer" else 1e12
            if kind == "number":
                self.control.setDecimals(12)
            self.control.setRange(setting.get("minimum", -bound), setting.get("maximum", bound))
        else:
            self.control = QLineEdit(self)
            self.control.setMaxLength(setting.get("max_length", 4096))
        layout.addWidget(self.control, 1)
        if kind in ("file", "directory"):
            browse = QPushButton(self.tr("Browse…"), self)
            browse.clicked.connect(self.browse)
            layout.addWidget(browse)
        if setting.get("description"):
            self.setToolTip(translate(plugin, setting["description"]))
        self.set_value(value)

    def set_value(self, value):
        kind = self.setting["type"]
        if kind == "boolean":
            self.control.setChecked(value)
        elif kind == "choice":
            self.control.setCurrentIndex(self.control.findData(value))
        elif kind in ("integer", "number"):
            self.control.setValue(value)
        else:
            self.control.setText(value)

    def value(self):
        kind = self.setting["type"]
        if kind == "boolean":
            return self.control.isChecked()
        if kind == "choice":
            return self.control.currentData()
        if kind in ("integer", "number"):
            return self.control.value()
        return self.control.text()

    def browse(self):
        from gui_qt.dialogs.portal_picker import pick_file, pick_folder
        picker = pick_file if self.setting["type"] == "file" else pick_folder
        picker(self, self.tr("Select path"), lambda path: self.set_value(str(path)) if path else None)
