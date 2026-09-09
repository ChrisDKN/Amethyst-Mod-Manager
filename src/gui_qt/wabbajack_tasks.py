from __future__ import annotations

import configparser
import zipfile
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox, QFrame

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
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
        self._layout.setSpacing(10)
        self.picked.connect(self._picked)
        self.hide()

    def _section(self, title):
        panel = QFrame(self)
        panel.setObjectName("WabbajackSetupSection")
        palette = active_palette()
        panel.setStyleSheet(
            f"QFrame#WabbajackSetupSection {{ background:{_c(palette, 'BG_ROW')}; "
            f"border:1px solid {_c(palette, 'BORDER_FAINT')}; border-radius:5px; }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 9, 10, 10)
        layout.setSpacing(7)
        heading = QLabel(title, panel)
        heading.setWordWrap(True)
        heading.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600; border:none; background:transparent;")
        layout.addWidget(heading)
        return panel, layout

    def _category(self, title):
        label = QLabel(title, self)
        label.setStyleSheet(f"color:{_c(active_palette(), 'TEXT_DIM')}; font-weight:600;")
        self._layout.addWidget(label)
        return label

    def configure(self, package, options=None, *, game=None):
        with QSignalBlocker(self):
            self._configure(package, options, game)

    def _configure(self, package, options, game):
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
        has_ttw = any(task.id.startswith("ttw:") for task in tasks)
        self._tasks_heading = self._category(self.tr("Additional setup"))
        for task in tasks:
            panel, layout = self._section(task.label)
            hint = QLabel(self.tr("Use the version required by the author. Output keeps its authored position in {0}.").format(task.mod), panel)
            hint.setWordWrap(True)
            layout.addWidget(hint)
            if task.id.startswith("fo3-bsa:"):
                hint = QLabel(self.tr("Run the Fallout 3 BSA Decompressor wizard, then import its complete output mod here, or select the author's .mpi package."), panel)
                hint.setWordWrap(True)
                layout.addWidget(hint)
            if task.id.startswith("ttw:"):
                package_page = QPushButton(self.tr("Open mod.pub TTW page"), panel)
                package_page.setObjectName("FormButton")
                package_page.clicked.connect(self._open_ttw_page)
                layout.addWidget(package_page)
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            mode = QComboBox(panel)
            mode.setMaxVisibleItems(15)
            if task.mpi_titles:
                mode.addItem(self.tr("Build from .mpi package"), "mpi")
            mode.addItem(self.tr("Import existing output mod"), "source")
            if task.id.startswith("yupttw:"):
                mode.addItem(self.tr("Import output archive"), "archive")
            option = self._values.get(task.id, {})
            if game is not None and task.id.startswith("fo3-bsa:") and task.id not in self._values:
                from Utils.bsa.decompressor import FO3_CONFIG, decompressor_mod_dir
                output = decompressor_mod_dir(game, FO3_CONFIG)
                if output is not None:
                    option = {"source": str(output)}
            if task.id.startswith("yupttw:"):
                selected = "source" if "source" in option else "archive"
                mode.setCurrentIndex(mode.findData(selected))
            elif option.get("source"):
                mode.setCurrentIndex(mode.findData("source"))
            form.addRow(self.tr("Method"), mode)
            fields = {}
            field_defs = [("mpi", "MPI package"), ("source", "Complete output mod")]
            if task.id.startswith("yupttw:"):
                field_defs.append(("archive", "Output archive"))
            for key, label in field_defs:
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
                if key == "mpi" and task.id.startswith("fo3-bsa:"):
                    download = QPushButton(self.tr("Download package…"), row)
                    download.setObjectName("FormButton")
                    download.setToolTip(self.tr("Download the FO3 BSA Decompressor archive from Nexus Mods, extract it, then browse to the .mpi file."))
                    download.clicked.connect(self._open_fo3_bsa_page)
                    buttons.addWidget(download)
                form.addRow(self.tr(label), row)
                fields[key] = (row, edit)
                edit.textChanged.connect(self.changed)
            layout.addLayout(form)
            self._layout.addWidget(panel)
            self._rows[task.id] = (task, panel, mode, form, fields)
            mode.currentIndexChanged.connect(lambda _, task=task.id: self._mode(task))
            self._mode(task.id)
        self._fo3_panel, section = self._section(self.tr("Source games"))
        form = QFormLayout()
        section.addLayout(form)
        row = QHBoxLayout()
        fo3_path = str(self._values.get("fallout3", ""))
        if not fo3_path and has_ttw:
            from Utils.bethesda.ttw import find_fo3_install
            detected = find_fo3_install()
            fo3_path = str(detected) if detected else ""
        self._fo3 = QLineEdit(fo3_path, self)
        self._fo3.setPlaceholderText(self.tr("Detected automatically when installed through Steam"))
        self._fo3.textChanged.connect(self.changed)
        row.addWidget(self._fo3)
        browse = QPushButton(self.tr("Browse…"), self)
        browse.setObjectName("FormButton")
        browse.clicked.connect(lambda: self._browse("fallout3", "source"))
        row.addWidget(browse)
        form.addRow(self.tr("Original Fallout 3 game"), row)
        self._layout.addWidget(self._fo3_panel)
        self._tool, section = self._section(self.tr("Setup tools"))
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(self.tr("Install / update native MPI tool"), self._tool)
        button.setObjectName("FormButton")
        button.clicked.connect(self.install_tool)
        tools.addWidget(button, 1)
        page = QPushButton(self.tr("MPI installer on GitHub"), self._tool)
        page.setObjectName("FormButton")
        page.clicked.connect(self._open_mpi_tool_page)
        tools.addWidget(page)
        section.addLayout(tools)
        self._layout.addWidget(self._tool)
        settings, section = self._section(self.tr("Compatibility and display"))
        self._settings_panel = settings
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        section.addLayout(form)
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
        settings_available = display_available or store_available or textures
        settings.setVisible(settings_available)
        self._configurable = settings_available or bool(problem)
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
        if hasattr(self, "_tasks_heading"):
            self._tasks_heading.setVisible(bool(tasks))
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
        elif key == "archive":
            pick_file(self.tr("Select output archive"), chosen,
                      filters=[("Mod archives", ["*.zip", "*.7z", "*.rar", "*.tar", "*.tar.gz", "*.tar.bz2", "*.tar.xz"])])
        else:
            pick_folder(self.tr("Select original game" if task == "fallout3" else "Select complete output mod"), chosen)

    def _open_ttw_page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from Utils.bethesda.ttw import MODPUB_URL
        QDesktopServices.openUrl(QUrl(MODPUB_URL))

    def _open_fo3_bsa_page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from Utils.bsa.decompressor import FO3_CONFIG
        QDesktopServices.openUrl(QUrl(FO3_CONFIG.nexus_url))

    def _open_mpi_tool_page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from Utils.bethesda.ttw import GITHUB_BUILDS_URL
        QDesktopServices.openUrl(QUrl(GITHUB_BUILDS_URL))

    def _picked(self, generation, task, key, path):
        if generation != self._generation or not path:
            return
        if task == "fallout3":
            self._fo3.setText(str(Path(path)))
        elif task in self._rows:
            self._rows[task][4][key][1].setText(str(Path(path)))
