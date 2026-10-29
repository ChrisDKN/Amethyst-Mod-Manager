from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit

from gui_qt.appearance.theme import _c
from gui_qt.appearance.icons import icon
from gui_qt.widgets.flow_layout import FlowLayout


class PluginPanelControls:
    def _plugins_footer(self) -> QWidget:
        """Plugin tools + search, under the plugins."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4, center=True)
        _made = {}
        self._plugin_footer_btns: list = []
        for label, disp in [
            ("Sort Plugins", self.tr("Sort Plugins")),
            ("Refresh Plugins", self.tr("Refresh Plugins")),
            ("Sync", self.tr("Sync")),
            ("Groups", self.tr("Groups")),
            ("Plugin Rules", self.tr("Plugin Rules")),
            ("LOOT", self.tr("LOOT")),
        ]:
            if label == "Sort Plugins":
                b = self._color_button(disp, _c(self._pal, "ACCENT"), compact=True)
            elif label == "LOOT":
                b = self._action_button("", "loot.png", compact=True)
                b.setToolButtonStyle(Qt.ToolButtonIconOnly)
                b.setFixedSize(self._FOOT_BTN_H, self._FOOT_BTN_H)
                px = self._FOOT_BTN_H - 4
                b.setIcon(icon("loot.png", px))
                b.setIconSize(QSize(px, px))
                b.setStyleSheet("QToolButton#FooterButton { padding: 1px; }")
                b.setAccessibleName(disp)
            else:
                b = self._text_button(disp, compact=True)
            b.setFixedHeight(self._FOOT_BTN_H)
            btns.addWidget(b)
            _made[label] = b
            self._plugin_footer_btns.append(b)
        v.addLayout(btns)
        self._register_plugin_footer("plugins_footer", btns)
        self._plugin_sort_btn = _made["Sort Plugins"]
        self._plugin_refresh_btn = _made["Refresh Plugins"]
        self._plugin_loot_btn = _made["LOOT"]
        self._plugin_loot_btn.setToolTip(self.tr("Show LOOT diagnostics"))
        self._plugin_sync_btn = _made["Sync"]
        self._plugin_sync_btn.setToolTip(
            self.tr("Match plugin load order to modlist priority"))
        self._plugin_groups_btn = _made["Groups"]
        self._plugin_rules_btn = _made["Plugin Rules"]
        self._plugin_filters_btn = self._filters_button()
        self._plugin_footer_btns.append(self._plugin_filters_btn)
        self._plugin_sort_btn.clicked.connect(self._on_sort_plugins)
        self._plugin_refresh_btn.clicked.connect(self._on_refresh_plugins)
        self._plugin_loot_btn.clicked.connect(self._open_loot_report)
        self._plugin_sync_btn.clicked.connect(self._on_sync_plugins)
        self._plugin_groups_btn.clicked.connect(self._open_plugin_groups_tab)
        self._plugin_rules_btn.clicked.connect(self._open_plugin_rules_tab)
        self._plugin_filters_btn.clicked.connect(self._toggle_plugin_filters)

        # Search row: an active / non-ESL count label (accent outline) to the left
        # of a full-width search box, mirroring the modlist footer.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)

        count = QLabel("…")
        count.setObjectName("PluginCountLabel")
        count.setAlignment(Qt.AlignCenter)
        count.setStyleSheet(
            "QLabel#PluginCountLabel {"
            f" color: {_c(self._pal, 'TEXT_MAIN')};"
            f" border: 1px solid {_c(self._pal, 'ACCENT')};"
            " border-radius: 4px; padding: 2px 8px; }")
        self._plugin_count = count
        search_row.addWidget(count)
        search_row.addWidget(self._plugin_filters_btn)
        search_row.addWidget(self._search_icon_label())

        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search plugins…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._on_plugin_search)
        self._plugins_search = search
        search.textChanged.connect(
            lambda _t, a="_plugin_filters_btn": self._sync_filters_btn(a))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        return bar


    def _mod_files_footer(self) -> QWidget:
        """Pack/Unpack BSA + Filters buttons + search, shown under the plugins
        column when the Mod Files sub-tab is active (replaces the plugin tools)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4, center=True)
        self._mf_pack_btn = self._text_button(
            self.tr("Pack BSA"), compact=True)
        self._mf_pack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_pack_btn.setEnabled(False)
        self._mf_pack_btn.clicked.connect(self._on_pack_bsa)
        self._mf_unpack_btn = self._text_button(
            self.tr("Unpack BSA"), compact=True)
        self._mf_unpack_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_unpack_btn.setEnabled(False)
        self._mf_unpack_btn.clicked.connect(self._on_unpack_bsa)
        self._mf_filters_btn = self._filters_button()
        self._mf_filters_btn.clicked.connect(self._toggle_mod_files_filters)
        self._mf_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._mf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_expand_btn.clicked.connect(self._on_mf_expand_clicked)
        self._mf_open_btn = self._text_button(self.tr("Open"), compact=True)
        self._mf_open_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_open_btn.setEnabled(False)
        self._mf_open_btn.clicked.connect(self._on_mf_open_clicked)
        self._mf_reset_btn = self._text_button(self.tr("Reset"), compact=True)
        self._mf_reset_btn.setFixedHeight(self._FOOT_BTN_H)
        self._mf_reset_btn.setEnabled(False)
        self._mf_reset_btn.setToolTip(
            self.tr("Undo this mod's Mod Files changes (Top Level, Root, "
                    "Disable). Mod files themselves are not touched."))
        self._mf_reset_btn.clicked.connect(self._on_mf_reset_clicked)
        self._equalize_button_widths(self._mf_pack_btn, self._mf_unpack_btn)
        btns.addWidget(self._mf_expand_btn)
        btns.addWidget(self._mf_open_btn)
        btns.addWidget(self._mf_reset_btn)
        btns.addWidget(self._mf_pack_btn)
        btns.addWidget(self._mf_unpack_btn)
        v.addLayout(btns)
        self._register_plugin_footer("mod_files_footer", btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._mf_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(
            lambda t: self._mod_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._mod_files_search = search
        search.textChanged.connect(
            lambda _t, a="_mf_filters_btn": self._sync_filters_btn(a))
        return bar


    def _data_footer(self) -> QWidget:
        """Filters + Expand-all + search, shown under the plugins column when the
        Data sub-tab is active (no Pack/Unpack - Data is read-only)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4, center=True)
        self._data_filters_btn = self._filters_button()
        self._data_filters_btn.clicked.connect(self._toggle_data_filters)
        self._data_expand_btn = self._text_button(self.tr("⊞ Expand all"), compact=True)
        self._data_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_expand_btn.clicked.connect(self._on_data_expand_clicked)
        btns.addWidget(self._data_expand_btn)
        self._data_blacklist_btn = self._text_button(self.tr("Blacklist"), compact=True)
        self._data_blacklist_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_blacklist_btn.clicked.connect(self._open_data_blacklist)
        btns.addWidget(self._data_blacklist_btn)
        self._data_routing_btn = self._text_button(self.tr("Routing Rules"), compact=True)
        self._data_routing_btn.setFixedHeight(self._FOOT_BTN_H)
        self._data_routing_btn.clicked.connect(self._open_data_routing_rules)
        btns.addWidget(self._data_routing_btn)
        v.addLayout(btns)
        self._register_plugin_footer("data_footer", btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._data_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._data_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._data_search = search
        search.textChanged.connect(
            lambda _t, a="_data_filters_btn": self._sync_filters_btn(a))
        return bar


    def _on_data_expand_clicked(self):
        expanded = self._data_view._toggle_expand_all()
        self._data_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                      else self.tr("⊞ Expand all"))


    def _overrides_footer(self) -> QWidget:
        """Refresh, shown under the plugins column when the Overrides sub-tab
        is active (BG3 override paks)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)
        btns = FlowLayout(spacing=4, center=True)
        refresh = self._text_button(self.tr("↻ Refresh"), compact=True)
        refresh.setFixedHeight(self._FOOT_BTN_H)
        refresh.clicked.connect(lambda: self._overrides_view.mark_dirty())
        btns.addWidget(refresh)
        v.addLayout(btns)
        self._register_plugin_footer("overrides_footer", btns)
        return bar


    def _saves_footer(self) -> QWidget:
        """Refresh / Open folder + a location summary, Filters and search, shown
        under the plugins column when the Saves sub-tab is active."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)
        btns = FlowLayout(spacing=4, center=True)
        refresh = self._text_button(self.tr("↻ Refresh"), compact=True)
        refresh.setFixedHeight(self._FOOT_BTN_H)
        refresh.clicked.connect(lambda: self._saves_view.mark_dirty())
        btns.addWidget(refresh)
        self._saves_open_btn = self._text_button(
            self.tr("Open folder"), compact=True)
        self._saves_open_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_open_btn.setEnabled(False)
        self._saves_open_btn.clicked.connect(lambda: self._saves_view.open_folder())
        btns.addWidget(self._saves_open_btn)
        # Export packs the save folder into a zip; Import puts one back (after
        # moving the current contents aside).
        self._saves_export_btn = self._text_button(
            self.tr("Export"), compact=True)
        self._saves_export_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_export_btn.setEnabled(False)
        self._saves_export_btn.clicked.connect(lambda: self._saves_view.start_export())
        btns.addWidget(self._saves_export_btn)
        self._saves_import_btn = self._text_button(
            self.tr("Import"), compact=True)
        self._saves_import_btn.setFixedHeight(self._FOOT_BTN_H)
        self._saves_import_btn.setEnabled(False)
        self._saves_import_btn.clicked.connect(lambda: self._saves_view.start_import())
        btns.addWidget(self._saves_import_btn)
        self._saves_status = QLabel("")
        self._saves_status.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._saves_status)
        v.addLayout(btns)
        self._register_plugin_footer("saves_footer", btns)

        self._saves_filters_btn = self._filters_button()
        self._saves_filters_btn.clicked.connect(self._toggle_saves_filters)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._saves_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search saves… (try !.ess)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._saves_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._saves_search = search
        search.textChanged.connect(
            lambda _t, a="_saves_filters_btn": self._sync_filters_btn(a))

        self._saves_view.status_changed.connect(self._saves_status.setText)
        # Open folder falls back to the save location itself, and Export/Import
        # need a single unambiguous folder - so all three follow the view's
        # state rather than the selection alone (and never run mid-transfer).
        for sig in (self._saves_view.selection_changed,
                    self._saves_view.busy_changed):
            sig.connect(self._sync_saves_buttons)
        self._saves_view.notify.connect(self._on_saves_notify)
        return bar


    def _sync_saves_buttons(self):
        """Sync the Saves footer buttons to what the tab can currently do."""
        can = self._saves_view.can_transfer()
        self._saves_export_btn.setEnabled(can)
        self._saves_import_btn.setEnabled(can)
        self._saves_open_btn.setEnabled(self._saves_view.can_open())


    def _on_saves_notify(self, message: str, kind: str):
        """Progress/result line from a save export or import."""
        self._saves_status.setText(message)
        if kind in ("success", "error"):
            self._notify(message, kind)


    def _downloads_footer(self) -> QWidget:
        """Downloads actions, locations, filters, and search."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        btns = FlowLayout(spacing=4, center=True)
        self._dl_install_btn = self._color_button(
            self.tr("Install Selected"), _c(self._pal, "ACCENT"), compact=True)
        self._dl_install_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_install_btn.setEnabled(False)
        self._dl_install_btn.clicked.connect(
            lambda: self._downloads_view.install_selected())
        self._dl_move_btn = self._text_button(
            self.tr("Move Selected"), compact=True)
        self._dl_move_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_move_btn.setEnabled(False)
        self._dl_move_btn.clicked.connect(lambda: self._on_downloads_move())
        self._dl_hide_btn = self._text_button(
            self.tr("Hide Selected"), compact=True)
        self._dl_hide_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_hide_btn.setEnabled(False)
        self._dl_hide_btn.clicked.connect(lambda: self._on_downloads_toggle_hidden())
        self._dl_remove_btn = self._color_button(
            self.tr("Remove Selected"), _c(self._pal, "BTN_DANGER"), compact=True)
        self._dl_remove_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_remove_btn.setEnabled(False)
        self._dl_remove_btn.clicked.connect(lambda: self._on_downloads_remove())
        self._dl_locations_btn = self._text_button(
            self.tr("Locations"), compact=True)
        self._dl_locations_btn.setFixedHeight(self._FOOT_BTN_H)
        self._dl_locations_btn.clicked.connect(self._on_downloads_locations)
        self._dl_filters_btn = self._filters_button()
        self._dl_filters_btn.clicked.connect(self._toggle_downloads_filters)
        self._equalize_button_widths(
            self._dl_install_btn, self._dl_move_btn, self._dl_hide_btn,
            self._dl_remove_btn)
        btns.addWidget(self._dl_install_btn)
        btns.addWidget(self._dl_move_btn)
        btns.addWidget(self._dl_hide_btn)
        btns.addWidget(self._dl_remove_btn)
        btns.addWidget(self._dl_locations_btn)
        v.addLayout(btns)
        self._register_plugin_footer("downloads_footer", btns)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._dl_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search downloads…"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._downloads_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._dl_search = search
        search.textChanged.connect(
            lambda _t, a="_dl_filters_btn": self._sync_filters_btn(a))
        return bar


    def _update_downloads_footer(self):
        n = self._downloads_view.checked_count()
        hide_label = (self.tr("Unhide Selected")
                      if n and self._downloads_view.selected_all_hidden()
                      else self.tr("Hide Selected"))
        for attr, label in (("_dl_install_btn", self.tr("Install Selected")),
                            ("_dl_move_btn", self.tr("Move Selected")),
                            ("_dl_hide_btn", hide_label),
                            ("_dl_remove_btn", self.tr("Remove Selected"))):
            b = getattr(self, attr, None)
            if b is not None:
                b.setEnabled(n > 0)
                b.setText(self.tr("{0} ({1})").format(label, n) if n else label)
        stack = getattr(self, "_plugin_footer_stack", None)
        if stack is not None:
            stack.clamp_to_current()


    def _text_files_footer(self) -> QWidget:
        """Search Content / Filters + search, shown under the plugins column when
        the Text Files sub-tab is active (read-only - no Pack/Install)."""
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # Inline content-search popup (hidden until "Search Content" is clicked).
        self._tf_content_bar = QWidget()
        cbl = QHBoxLayout(self._tf_content_bar)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(4)
        lbl = QLabel(self.tr("Find in files:"))
        lbl.setStyleSheet(f"color:{_c(self._pal,'TEXT_DIM')};")
        cbl.addWidget(lbl)
        self._tf_content_input = QLineEdit()
        self._tf_content_input.setPlaceholderText(self.tr("Text to search for…"))
        self._tf_content_input.setClearButtonEnabled(True)
        self._tf_content_input.returnPressed.connect(self._run_tf_content_search)
        cbl.addWidget(self._tf_content_input, 1)
        go = self._color_button(
            self.tr("Search"), _c(self._pal, "ACCENT"), compact=True)
        go.setFixedHeight(self._FOOT_BTN_H)
        go.clicked.connect(self._run_tf_content_search)
        cbl.addWidget(go)
        close = self._text_button("✕", compact=True)
        close.setFixedHeight(self._FOOT_BTN_H)
        close.clicked.connect(self._close_tf_content_bar)
        cbl.addWidget(close)
        self._tf_content_bar.setVisible(False)
        v.addWidget(self._tf_content_bar)

        btns = FlowLayout(spacing=4, center=True)
        self._tf_content_btn = self._text_button(
            self.tr("Search Content"), compact=True)
        self._tf_content_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_content_btn.clicked.connect(self._on_text_files_content_search)
        self._tf_filters_btn = self._filters_button()
        self._tf_filters_btn.clicked.connect(self._toggle_text_files_filters)
        self._tf_expand_btn = self._text_button(
            self.tr("⊞ Expand all"), compact=True)
        self._tf_expand_btn.setFixedHeight(self._FOOT_BTN_H)
        self._tf_expand_btn.clicked.connect(self._on_tf_expand_clicked)
        btns.addWidget(self._tf_expand_btn)
        btns.addWidget(self._tf_content_btn)
        # A dim status label showing the active content-search keyword.
        self._tf_content_status = QLabel("")
        self._tf_content_status.setStyleSheet(
            f"color:{_c(self._pal,'TEXT_DIM')};")
        btns.addWidget(self._tf_content_status)
        v.addLayout(btns)
        self._register_plugin_footer("text_files_footer", btns)
        self._text_files_view.content_status_changed.connect(
            self._on_tf_content_status)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._tf_filters_btn)
        search_row.addWidget(self._search_icon_label())
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Search files… (try !.dds)"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(lambda t: self._text_files_view._on_search(t))
        search_row.addWidget(search, 1)
        v.addLayout(search_row)
        self._tf_search = search
        search.textChanged.connect(
            lambda _t, a="_tf_filters_btn": self._sync_filters_btn(a))
        return bar


    def _set_tf_content_bar_visible(self, visible: bool):
        """Show/hide the inline find-in-files bar, then re-clamp the footer
        stack: its max height is pinned to the current page's height (see
        _CurrentPageStack), so the page growing a row would otherwise be
        squashed into the old clamped height."""
        self._tf_content_bar.setVisible(visible)
        stack = getattr(self, "_plugin_footer_stack", None)
        if stack is not None:
            stack.clamp_to_current()


    def _on_text_files_content_search(self):
        """Toggle the inline content-search bar. When a search is active, this
        clears it; otherwise it opens the input bar above the footer + focuses it."""
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()
            self._tf_content_input.clear()
            self._set_tf_content_bar_visible(False)
            return
        showing = not self._tf_content_bar.isVisible()
        self._set_tf_content_bar_visible(showing)
        if showing:
            self._tf_content_input.setFocus()
            self._tf_content_input.selectAll()


    def _on_tf_expand_clicked(self):
        expanded = self._text_files_view._toggle_expand_all()
        self._tf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))


    def _run_tf_content_search(self):
        kw = self._tf_content_input.text().strip()
        if kw:
            self._text_files_view.run_content_search(kw)
        else:
            self._text_files_view.clear_content_search()


    def _close_tf_content_bar(self):
        self._set_tf_content_bar_visible(False)
        self._tf_content_input.clear()
        if self._text_files_view._content_keyword:
            self._text_files_view.clear_content_search()


    def _on_tf_content_status(self, keyword):
        if keyword:
            self._tf_content_status.setText(self.tr('Content: "{0}"').format(keyword))
            self._tf_content_btn.setText(self.tr("Clear Content"))
        else:
            self._tf_content_status.setText("")
            self._tf_content_btn.setText(self.tr("Search Content"))
        self._tf_content_btn.setProperty("active", bool(keyword))
        self._tf_content_btn.style().unpolish(self._tf_content_btn)
        self._tf_content_btn.style().polish(self._tf_content_btn)


    def _update_mf_footer_buttons(self):
        import Utils.bsa.pack as ops
        pack_btn = getattr(self, "_mf_pack_btn", None)
        unpack_btn = getattr(self, "_mf_unpack_btn", None)
        if pack_btn is None or unpack_btn is None:
            return
        mv = self._mod_files_view
        open_btn = getattr(self, "_mf_open_btn", None)
        if open_btn is not None:
            mod_dir = mv._mod_root_dir() if mv.has_mod() else None
            open_btn.setEnabled(mod_dir is not None and mod_dir.is_dir())
        # Reset first - it applies to every game, including ones we can't
        # pack for (which return early below).
        reset_btn = getattr(self, "_mf_reset_btn", None)
        if reset_btn is not None:
            reset_btn.setEnabled(mv.has_mod() and mv.has_changes() and not self._bsa_op_running)
        kind = ops.archive_kind_for_game(getattr(mv, "game", None))
        # Hide both buttons entirely on games we can't pack for (Tk parity).
        if kind is None:
            pack_btn.setVisible(False)
            unpack_btn.setVisible(False)
            return
        upper = kind.upper()
        pack_btn.setVisible(True)
        unpack_btn.setVisible(True)
        pack_btn.setText(self.tr("Pack {0}").format(upper))
        unpack_btn.setText(self.tr("Unpack {0}").format(upper))
        # Pack: any normal mod. Unpack: also needs a matching archive on disk.
        is_normal = (not self._bsa_op_running and mv.has_mod()
                     and ops.is_packable_mod(getattr(mv, "_mod_name", None)))
        pack_btn.setEnabled(is_normal)
        has_archive = False
        if is_normal:
            mod_dir = self._bsa_mod_dir()
            has_archive = mod_dir is not None and ops.mod_has_archive(mod_dir, kind)
        unpack_btn.setEnabled(has_archive)


    def _on_mf_expand_clicked(self):
        expanded = self._mod_files_view._toggle_expand_all()
        self._mf_expand_btn.setText(self.tr("⊟ Collapse all") if expanded
                                    else self.tr("⊞ Expand all"))


    def _on_mf_open_clicked(self):
        self._open_folder_path(
            self._mod_files_view._mod_root_dir(), self.tr("File location"))


    def _bsa_mod_dir(self, view=None):
        """Resolve the on-disk staging folder for the Mod Files tab's mod."""
        mv = view or self._mod_files_view
        game = getattr(mv, "game", None)
        mod_name = getattr(mv, "_mod_name", None)
        if game is None or not mod_name:
            return None
        try:
            base = Path(game.get_effective_mod_staging_path())
        except Exception:
            return None
        d = base / mod_name
        return d if d.is_dir() else None


    def _bsa_plugin_exts(self, view=None):
        game = getattr(view or self._mod_files_view, "game", None)
        return getattr(game, "plugin_extensions", None) or (".esp", ".esm", ".esl")


    def _refresh_plugin_stats(self):
        """Update the plugins footer count label: active plugins / non-ESL. All
        the data is in-memory on the plugin model, so this is instant."""
        lbl = getattr(self, "_plugin_count", None)
        if lbl is None:
            return
        from gui_qt.modlist.data import compute_plugin_stats
        s = compute_plugin_stats(self._plugin_model._rows)
        lbl.setText(self.tr("P:{0} / Non-ESL:{1}").format(
            s['total'], s['non_esl']))
        lbl.setToolTip(self.tr("{0} plugins ({1} ESL, {2} non-ESL)").format(
            s["total"], s["esl"], s["non_esl"]))

