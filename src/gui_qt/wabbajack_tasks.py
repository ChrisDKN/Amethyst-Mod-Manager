from __future__ import annotations

import configparser
import zipfile
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox

from gui_qt.safe_emit import safe_emit
from gui_qt.tri_state_checkbox import TriStateCheckBox
from gui_qt.wabbajack_setup import CappedComboBox


class SetupOptions(QWidget):
    changed = Signal()
    install_tool = Signal()
    picked = Signal(int, str, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0
        self._rows = {}
        self._values = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.picked.connect(self._picked)
        self.hide()

    def configure(self, package, options=None):
        with QSignalBlocker(self):
            self._configure(package, options)

    def _configure(self, package, options):
        from Utils.wabbajack.requirements import setup_tasks
        self._generation += 1
        self._rows.clear()
        self._values = dict(options or {})
        from Utils.wabbajack.post_install import display_supported
        display_available = bool(package and display_supported(package))
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        problem = ""
        try:
            tasks = setup_tasks(package) if package else []
        except (OSError, ValueError, KeyError, configparser.Error, zipfile.BadZipFile) as exc:
            tasks = []
            problem = str(exc)
            label = QLabel(problem, self)
            label.setWordWrap(True)
            self._layout.addWidget(label)
        for task in tasks:
            panel = QWidget(self)
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 6, 0, 6)
            title = QLabel(task.label, panel)
            title.setWordWrap(True)
            layout.addWidget(title)
            hint = QLabel(self.tr("Use the version required by the author. Output keeps its authored position in {0}.").format(task.mod), panel)
            hint.setWordWrap(True)
            layout.addWidget(hint)
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            mode = QComboBox(panel)
            mode.setMaxVisibleItems(15)
            if task.mpi_titles:
                mode.addItem(self.tr("Build from .mpi package"), "mpi")
            mode.addItem(self.tr("Import existing output mod"), "source")
            option = self._values.get(task.id, {})
            if option.get("source"):
                mode.setCurrentIndex(mode.findData("source"))
            form.addRow(self.tr("Method"), mode)
            fields = {}
            for key, label in (("mpi", "MPI package"), ("source", "Complete output mod")):
                if key == "mpi" and not task.mpi_titles:
                    continue
                row = QWidget(panel)
                buttons = QHBoxLayout(row)
                buttons.setContentsMargins(0, 0, 0, 0)
                edit = QLineEdit(str(option.get(key, "")), row)
                edit.setPlaceholderText(self.tr("Select the author-required version"))
                buttons.addWidget(edit, 1)
                browse = QPushButton(self.tr("Browse…"), row)
                browse.setObjectName("FormButton")
                browse.clicked.connect(lambda checked=False, task=task.id, key=key: self._browse(task, key))
                buttons.addWidget(browse)
                form.addRow(self.tr(label), row)
                fields[key] = (row, edit)
                edit.textChanged.connect(self.changed)
            layout.addLayout(form)
            self._layout.addWidget(panel)
            self._rows[task.id] = (task, panel, mode, form, fields)
            mode.currentIndexChanged.connect(lambda _, task=task.id: self._mode(task))
            self._mode(task.id)
        self._fo3_panel = QWidget(self)
        form = QFormLayout(self._fo3_panel)
        row = QHBoxLayout()
        self._fo3 = QLineEdit(str(self._values.get("fallout3", "")), self)
        self._fo3.textChanged.connect(self.changed)
        row.addWidget(self._fo3)
        browse = QPushButton(self.tr("Browse…"), self)
        browse.setObjectName("FormButton")
        browse.clicked.connect(lambda: self._browse("fallout3", "source"))
        row.addWidget(browse)
        form.addRow(self.tr("Original Fallout 3 game"), row)
        self._layout.addWidget(self._fo3_panel)
        button = QPushButton(self.tr("Install / update native MPI tool"), self)
        button.setObjectName("FormButton")
        button.clicked.connect(self.install_tool)
        self._layout.addWidget(button)
        self._tool = button
        settings = QWidget(self)
        form = QFormLayout(settings)
        self._store = QComboBox(settings)
        self._store.setMaxVisibleItems(15)
        for label, value in (("Detect from game installation", ""), ("Steam / GOG", "steam-gog"), ("Epic Games", "epic")):
            self._store.addItem(self.tr(label), value)
        self._store.setCurrentIndex(max(0, self._store.findData(self._values.get("store", ""))))
        form.addRow(self.tr("Root file variant"), self._store)
        from Utils.wabbajack.adapters import STORE_ROOT_FOLDERS
        store_available = bool(package and any(d.path.split("/")[0].casefold().strip("_ ") in STORE_ROOT_FOLDERS for d in package.directives))
        form.setRowVisible(self._store, store_available)
        self._store.currentIndexChanged.connect(self.changed)
        self._display = TriStateCheckBox(self.tr("Override"), settings, two_state=True)
        display = self._values.get("display")
        self._display.set_state(1 if display else 0)
        display_field = QWidget(settings)
        row = QHBoxLayout(display_field)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._display)
        self._width, self._height = QSpinBox(settings), QSpinBox(settings)
        for index, (field, value) in enumerate(zip((self._width, self._height), display or (1920, 1080))):
            field.setRange(320, 16384)
            field.setValue(value)
            field.setEnabled(bool(display))
            field.setMaximumWidth(90)
            field.valueChanged.connect(self.changed)
            if index:
                separator = QLabel("×", settings)
                separator.setEnabled(bool(display))
                row.addWidget(separator)
                self._display_separator = separator
            row.addWidget(field)
        row.addStretch(1)
        self._display.stateChanged.connect(self._display_changed)
        display_field.setToolTip(self.tr("Overrides the author's resolution in supported game INIs and display-tweak files. Leave off to keep their settings."))
        form.addRow(self.tr("Display resolution"), display_field)
        form.setRowVisible(display_field, display_available)
        textures = bool(package and any(d.kind == "TransformedTexture" for d in package.directives))
        self._texture_runtime = CappedComboBox(settings)
        self._texture_runtime.addItem(self.tr("Choose automatically"), "")
        texture = self._values.get("texture", {})
        if textures:
            from Utils.launchers.steam import list_installed_proton
            for proton in list_installed_proton():
                self._texture_runtime.addItem(proton.parent.name, str(proton.resolve()))
            saved = texture.get("proton", "")
            if saved and self._texture_runtime.findData(saved) < 0:
                self._texture_runtime.addItem(self.tr("Unavailable: {0}").format(Path(saved).parent.name), saved)
            self._texture_runtime.setCurrentIndex(max(0, self._texture_runtime.findData(saved)))
        self._texture_runtime.currentIndexChanged.connect(self.changed)
        form.addRow(self.tr("Texture tool Proton"), self._texture_runtime)
        form.setRowVisible(self._texture_runtime, textures)
        self._texture_mode = CappedComboBox(settings)
        self._texture_mode.addItem(self.tr("Automatic (GPU when available)"), "auto")
        self._texture_mode.addItem(self.tr("CPU only"), "cpu")
        self._texture_mode.setCurrentIndex(max(0, self._texture_mode.findData(texture.get("mode", "auto"))))
        self._texture_mode.currentIndexChanged.connect(self.changed)
        form.addRow(self.tr("Texture conversion"), self._texture_mode)
        form.setRowVisible(self._texture_mode, textures)
        self._layout.addWidget(settings)
        self._configurable = display_available or store_available or textures or bool(problem)
        self.set_profiles(package.profiles if package else [])

    def _display_changed(self, *_):
        enabled = self._display.state() == 1
        for field in (self._width, self._height):
            field.setEnabled(enabled)
        separator = getattr(self, "_display_separator", None)
        if separator is not None:
            separator.setEnabled(enabled)
        self.changed.emit()

    def _mode(self, task_id):
        _, _, mode, form, fields = self._rows[task_id]
        for key, (row, _) in fields.items():
            form.setRowVisible(row, mode.currentData() == key)
        self.changed.emit()

    def set_profiles(self, profiles):
        active = set(profiles)
        for task, panel, _, _, _ in self._rows.values():
            panel.setVisible(bool(active.intersection(task.profiles)))
        tasks = [task for task, _, _, _, _ in self._rows.values() if active.intersection(task.profiles)]
        self._active = {task.id for task in tasks}
        if hasattr(self, "_fo3_panel"):
            self._fo3_panel.setVisible(any(task.id.startswith("ttw:") for task in tasks))
            self._tool.setVisible(any(task.mpi_titles for task in tasks))
        self.setVisible(bool(tasks) or getattr(self, "_configurable", False))

    def values(self):
        values = dict(self._values)
        if hasattr(self, "_fo3"):
            values["fallout3"] = self._fo3.text().strip()
        if hasattr(self, "_display"):
            values["display"] = [self._width.value(), self._height.value()] if (self._display.state() == 1) else None
            values["store"] = self._store.currentData()
            values["texture"] = {"proton": self._texture_runtime.currentData(), "mode": self._texture_mode.currentData()}
        for task_id, (_, _, mode, _, fields) in self._rows.items():
            key = mode.currentData()
            values[task_id] = {key: fields[key][1].text().strip()}
        return values

    def _browse(self, task, key):
        from Utils.ui.portal import pick_file, pick_folder
        generation = self._generation
        def chosen(path):
            safe_emit(self.picked, generation, task, key, path)
        if key == "mpi":
            pick_file(self.tr("Select extracted MPI package"), chosen, filters=[("MPI packages", ["*.mpi"])])
        else:
            pick_folder(self.tr("Select original game" if task == "fallout3" else "Select complete output mod"), chosen)

    def _picked(self, generation, task, key, path):
        if generation != self._generation or not path:
            return
        if task == "fallout3":
            self._fo3.setText(str(Path(path)))
        elif task in self._rows:
            self._rows[task][4][key][1].setText(str(Path(path)))
