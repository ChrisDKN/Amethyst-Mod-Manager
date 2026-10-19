from __future__ import annotations

import json


class ThemeService:
    def __init__(self, context):
        self.selection = None
        self.set_context(context)

    def set_context(self, context):
        data = context["data"]
        selection = (context["game_id"], json.dumps(data, sort_keys=True))
        if selection == self.selection:
            return
        self.selection = selection
        if not context["game_id"] or not data.get("enabled", True):
            return
        api = context["api_call"]
        available = {theme["id"] for theme in api("appearance.themes.list")["themes"]}
        mappings = data.get("games", {})
        if not isinstance(mappings, dict):
            mappings = {}
        fallback = data.get("fallback", "amethyst")
        if not isinstance(fallback, str) or fallback not in available:
            fallback = "amethyst"
        theme_id = mappings.get(context["game_id"], fallback)
        if not isinstance(theme_id, str) or theme_id not in available:
            theme_id = fallback
        if not data:
            return
        if api("appearance.get")["theme_id"] != theme_id:
            api("appearance.set", {"theme_id": theme_id, "confirm": True})

    def dispose(self):
        self.selection = None


def create_service(context):
    return ThemeService(context)


def create_view(context, parent, on_close):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                                   QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget)

    class ThemeView(QWidget):
        def __init__(self):
            super().__init__(parent)
            self.data = context["data"]
            self.themes = context["api_call"]("appearance.themes.list")["themes"]
            layout = QVBoxLayout(self)
            description = QLabel("Automatically apply each game's theme when you select it, "
                                 "including when Amethyst starts.", self)
            description.setWordWrap(True)
            layout.addWidget(description)
            self.enabled = QCheckBox("Enable automatic theme switching", self)
            self.enabled.setChecked(bool(self.data.get("enabled", True)))
            layout.addWidget(self.enabled)
            options = QWidget(self)
            options_layout = QVBoxLayout(options)
            options_layout.setContentsMargins(0, 0, 0, 0)
            fallback_form = QFormLayout()
            current = context["api_call"]("appearance.get")["theme_id"]
            self.fallback = self.theme_combo(self.data.get("fallback", current))
            fallback_form.addRow("Fallback theme", self.fallback)
            options_layout.addLayout(fallback_form)
            hint = QLabel("The fallback is used for unassigned games and themes that are no longer available.", options)
            hint.setWordWrap(True)
            options_layout.addWidget(hint)
            scroll = QScrollArea(options)
            scroll.setWidgetResizable(True)
            contents = QWidget(scroll)
            form = QFormLayout(contents)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            self.fields = {}
            mappings = self.data.get("games", {})
            self.mappings = dict(mappings) if isinstance(mappings, dict) else {}
            for game in context["configured_games"]:
                label = QLabel(game["name"], contents)
                label.setTextFormat(Qt.PlainText)
                label.setWordWrap(True)
                combo = self.theme_combo(self.mappings.get(game["id"], ""), inherit=True)
                label.setBuddy(combo)
                form.addRow(label, combo)
                self.fields[game["id"]] = combo
            scroll.setWidget(contents)
            options_layout.addWidget(scroll, 1)
            layout.addWidget(options, 1)
            options.setEnabled(self.enabled.isChecked())
            self.enabled.toggled.connect(options.setEnabled)
            self.status = QLabel(self)
            self.status.setTextFormat(Qt.PlainText)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            buttons = QHBoxLayout()
            buttons.addStretch()
            cancel = QPushButton("Cancel", self)
            cancel.clicked.connect(self.request_close)
            buttons.addWidget(cancel)
            save = QPushButton("Save", self)
            save.clicked.connect(self.save)
            buttons.addWidget(save)
            layout.addLayout(buttons)

        def theme_combo(self, selected, inherit=False):
            combo = QComboBox(self)
            if inherit:
                combo.addItem("Use fallback theme", "")
            for theme in self.themes:
                combo.addItem(theme["name"], theme["id"])
            index = combo.findData(selected)
            if index < 0 and isinstance(selected, str) and selected:
                combo.addItem(f"Unavailable: {selected} (uses fallback)", selected)
                index = combo.count() - 1
            combo.setCurrentIndex(max(0, index))
            return combo

        def save(self):
            for game_id, combo in self.fields.items():
                theme_id = combo.currentData()
                if theme_id:
                    self.mappings[game_id] = theme_id
                else:
                    self.mappings.pop(game_id, None)
            data = {**self.data, "enabled": self.enabled.isChecked(),
                    "fallback": self.fallback.currentData(), "games": self.mappings}
            try:
                context["save_data"](data)
            except Exception as exc:
                self.status.setText(f"Could not save theme settings: {exc}")
                return
            on_close(False)

        def request_close(self):
            on_close(False)

    return ThemeView()
