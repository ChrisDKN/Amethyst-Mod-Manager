"""
Filter side-panel mixin for ModListPanel.

Builds the inline filter widget (column 0, hidden until toggled), wires its
checkboxes to ModListPanel filter state, and handles open/close transitions.

State fields (e.g. self._filter_show_disabled, self._filter_categories) live
on the host panel — this mixin only reads/writes them via the names below.
"""

import tkinter as tk
import customtkinter as ctk

from gui.theme import (
    ACCENT, ACCENT_HOV,
    BG_HEADER, BG_HOVER, BG_PANEL,
    BORDER,
    TEXT_DIM, TEXT_MAIN,
    scaled,
)
import gui.theme as _theme
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.tri_state_checkbox import TriStateCheckBox, STATE_OFF

# Keys that should remain plain on/off (no exclude state).
_PLAIN_TOGGLES = frozenset({"filter_hide_separators"})


# (var_key, label, host_state_attr) — single source of truth for the
# checkbox grid. var_key is the BooleanVar key in self._fsp_vars *and*
# the dict key emitted by _on_filter_panel_change; host_state_attr is
# the live field on ModListPanel that _open / _apply read and write.
_FILTER_CHECKBOXES: tuple[tuple[str, str, str], ...] = (
    ("filter_show_disabled",        "Disabled mods",                  "_filter_show_disabled"),
    ("filter_show_enabled",         "Enabled mods",                   "_filter_show_enabled"),
    ("filter_hide_separators",      "Hide separators",                "_filter_hide_separators"),
    ("filter_winning",              "Winning conflicts",              "_filter_conflict_winning"),
    ("filter_losing",               "Losing conflicts",               "_filter_conflict_losing"),
    ("filter_partial",              "Winning & losing conflicts",     "_filter_conflict_partial"),
    ("filter_full",                 "Fully conflicted mods",          "_filter_conflict_full"),
    ("filter_missing_reqs",         "Missing requirements",           "_filter_missing_reqs"),
    ("filter_has_disabled_plugins", "Mods with disabled plugins",     "_filter_has_disabled_plugins"),
    ("filter_has_plugins",          "Mods with plugins",              "_filter_has_plugins"),
    ("filter_has_disabled_files",   "Mods modified in Mod Files tab", "_filter_has_disabled_files"),
    ("filter_has_updates",          "Mods with updates",              "_filter_has_updates"),
    ("filter_has_notes",            "Mods with notes",                "_filter_has_notes"),
    ("filter_fomod_only",           "FOMOD mods",                     "_filter_fomod_only"),
    ("filter_bain_only",            "BAIN mods",                      "_filter_bain_only"),
    ("filter_has_bsa",              "Mods with BSA archives",         "_filter_has_bsa"),
    ("filter_has_pbr",              "PGPatcher mods",                 "_filter_has_pbr"),
)

# Filters that only apply to a specific game. Maps var_key → predicate on the
# active game; the checkbox is hidden (and its state cleared) when the active
# game doesn't match. PGPatcher (parallax/complex material/PBR) is Skyrim SE only.
_GAME_SPECIFIC_FILTERS = {
    "filter_has_pbr": lambda game: getattr(game, "nexus_game_domain", "") == "skyrimspecialedition",
}


class ModListFilterPanelMixin:
    """Adds the filter side panel to ModListPanel.

    Host must define filter state attrs listed in _FILTER_CHECKBOXES (plus
    self._filter_categories), self._category_names, self._filter_btn,
    self._invalidate_derived_caches(), and self._redraw().
    """

    def _build_filter_side_panel(self):
        """Build the inline filter side panel (column 0, initially hidden)."""
        self._filter_panel_open = False

        # 300 was too narrow at 1.25x–1.5x scale (labels got truncated).
        # CTk scales frame width; pass the unscaled design value.
        panel = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0,
                             width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._filter_side_panel = panel

        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.TK_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_filter_side_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.TK_FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)

        tk.Label(
            scroll_frame, text="By status",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(2, 4))

        self._fsp_vars: dict[str, tk.IntVar] = {}
        self._fsp_checkboxes: dict[str, ctk.CTkCheckBox] = {}
        for key, label, _attr in _FILTER_CHECKBOXES:
            var = tk.IntVar(value=0)
            self._fsp_vars[key] = var
            common = dict(
                text=label,
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            )
            if key in _PLAIN_TOGGLES:
                cb = ctk.CTkCheckBox(scroll_frame, onvalue=1, offvalue=0, **common)
            else:
                cb = TriStateCheckBox(scroll_frame, **common)
            cb.pack(anchor="w", fill="x", pady=3)
            self._fsp_checkboxes[key] = cb

        self._fsp_category_label = tk.Label(
            scroll_frame, text="By category",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        )
        self._fsp_category_label.pack(anchor="w", pady=(10, 4))
        self._fsp_category_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._fsp_category_frame.pack(anchor="w", fill="x", pady=(2, 0))
        self._fsp_category_vars: dict[str, tk.IntVar] = {}

        tk.Label(
            scroll_frame, text="By file type",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(10, 4))
        self._fsp_filetype_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._fsp_filetype_frame.pack(anchor="w", fill="x", pady=(2, 0))
        self._fsp_filetype_vars: dict[str, tk.IntVar] = {}

        self._filter_scroll_frame = scroll_frame
        self._bind_filter_panel_scroll()

    def _bind_filter_panel_scroll(self) -> None:
        """Bind mouse wheel to the filter panel's scroll frame (Linux Button-4/5, Windows MouseWheel)."""
        scroll_frame = getattr(self, "_filter_scroll_frame", None)
        if not scroll_frame or not hasattr(scroll_frame, "_parent_canvas"):
            return

        # Slower than the app-wide 3 units/notch — the filter panel is short
        # enough that 3 flies past entries.
        step = 2

        def _on_wheel(evt):
            num = getattr(evt, "num", None)
            delta = getattr(evt, "delta", 0) or 0
            if num == 4 or delta > 0:
                scroll_frame._parent_canvas.yview_scroll(-step, "units")
            elif num == 5 or delta < 0:
                scroll_frame._parent_canvas.yview_scroll(step, "units")
            # Stop Tk from propagating the wheel event to ancestor widgets that
            # also have _on_wheel bound — without this, a notch on a deeply
            # nested checkbox scrolls once per ancestor (looks like acceleration
            # as the list nests deeper near the bottom).
            return "break"

        # On Tk >= 8.7 CTkScrollableFrame's own <MouseWheel> handler scrolls
        # at the app-wide 3-unit step. Override it on this instance so the
        # filter panel uses our slower step.
        if LEGACY_WHEEL_REDUNDANT:
            import sys as _sys

            def _slow_mouse_wheel_all(self_sf, event, _step=step):
                check_fn = getattr(self_sf, "_check_if_valid_scroll", None) or getattr(self_sf, "check_if_master_is_canvas", None)
                if check_fn and not check_fn(event.widget):
                    return
                delta = getattr(event, "delta", 0) or 0
                if delta == 0:
                    return
                if _sys.platform.startswith("win"):
                    units = -int(delta / 8)
                elif _sys.platform == "darwin":
                    units = -delta
                else:
                    units = -_step if delta > 0 else _step
                if self_sf._shift_pressed:
                    if self_sf._parent_canvas.xview() != (0.0, 1.0):
                        self_sf._parent_canvas.xview("scroll", units, "units")
                else:
                    if self_sf._parent_canvas.yview() != (0.0, 1.0):
                        self_sf._parent_canvas.yview("scroll", units, "units")

            import types as _types
            scroll_frame._mouse_wheel_all = _types.MethodType(
                _slow_mouse_wheel_all, scroll_frame,
            )

        _legacy = None if LEGACY_WHEEL_REDUNDANT else _on_wheel

        def _bind_recursive(w):
            if _legacy is not None:
                w.bind("<Button-4>", _legacy)
                w.bind("<Button-5>", _legacy)
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(scroll_frame)

    def _refresh_filter_category_list(self) -> None:
        """Populate category checkboxes from current _category_names. Call when opening filter panel."""
        for w in self._fsp_category_frame.winfo_children():
            w.destroy()
        self._fsp_category_vars.clear()
        categories = sorted(
            set(self._category_names.values()) | {""},
            key=lambda c: ("(Uncategorized)" if c == "" else c).lower(),
        )
        cat_excludes = getattr(self, "_filter_categories_exclude", frozenset())
        for cat in categories:
            label = "(Uncategorized)" if cat == "" else cat
            if cat in self._filter_categories:
                init = 1
            elif cat in cat_excludes:
                init = 2
            else:
                init = 0
            var = tk.IntVar(value=init)
            self._fsp_category_vars[cat] = var
            TriStateCheckBox(
                self._fsp_category_frame,
                text=label,
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", pady=2)

        self._bind_filter_panel_scroll()

    def _refresh_filter_filetype_list(self) -> None:
        """Populate filetype checkboxes from the persisted mod index.

        Called when opening the filter panel so the list always reflects
        what's currently installed. Sorted by file count (desc), then ext (asc).
        """
        for w in self._fsp_filetype_frame.winfo_children():
            w.destroy()
        self._fsp_filetype_vars.clear()
        counts = self._get_filetype_counts()
        if not counts:
            ctk.CTkLabel(
                self._fsp_filetype_frame,
                text="(no mods indexed)",
                font=_theme.FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).pack(anchor="w", pady=2)
            self._bind_filter_panel_scroll()
            return
        ordered = sorted(counts.items(), key=lambda kv: kv[0])
        ft_excludes = getattr(self, "_filter_filetypes_exclude", frozenset())
        for ext, count in ordered:
            if ext in self._filter_filetypes:
                init = 1
            elif ext in ft_excludes:
                init = 2
            else:
                init = 0
            var = tk.IntVar(value=init)
            self._fsp_filetype_vars[ext] = var
            TriStateCheckBox(
                self._fsp_filetype_frame,
                text=f"{ext}  ({count:,})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", pady=2)

        self._bind_filter_panel_scroll()

    def _clear_all_filters(self):
        for key, cb in self._fsp_checkboxes.items():
            if isinstance(cb, TriStateCheckBox):
                cb.set_state(STATE_OFF)
            else:
                self._fsp_vars[key].set(0)
        for v in self._fsp_category_vars.values():
            v.set(0)
        for v in self._fsp_filetype_vars.values():
            v.set(0)
        # Force category/filetype checkbox widgets to redraw their visuals.
        self._refresh_filter_category_list()
        self._refresh_filter_filetype_list()
        self._apply_modlist_filters({
            "filter_categories": frozenset(),
            "filter_categories_exclude": frozenset(),
            "filter_filetypes": frozenset(),
            "filter_filetypes_exclude": frozenset(),
        })

    def _on_filter_panel_change(self):
        state = {k: v.get() for k, v in self._fsp_vars.items()}
        state["filter_categories"] = frozenset(
            c for c, v in self._fsp_category_vars.items() if v.get() == 1
        )
        state["filter_categories_exclude"] = frozenset(
            c for c, v in self._fsp_category_vars.items() if v.get() == 2
        )
        state["filter_filetypes"] = frozenset(
            ext for ext, v in self._fsp_filetype_vars.items() if v.get() == 1
        )
        state["filter_filetypes_exclude"] = frozenset(
            ext for ext, v in self._fsp_filetype_vars.items() if v.get() == 2
        )
        self._apply_modlist_filters(state)

    def _on_open_filters(self):
        if getattr(self, "_filter_panel_open", False):
            self._close_filter_side_panel()
        else:
            self._open_filter_side_panel()

    def _open_filter_side_panel(self):
        # Close plugin/data filter if open (they share the same column).
        plugin_panel = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if plugin_panel is not None and getattr(plugin_panel, "_plugin_filter_panel_open", False):
            plugin_panel._close_plugin_filter_panel()
        if plugin_panel is not None and getattr(plugin_panel, "_data_filter_panel_open", False):
            plugin_panel._close_data_filter_panel()
        if plugin_panel is not None and getattr(plugin_panel, "_ini_filter_panel_open", False):
            plugin_panel._close_ini_filter_panel()
        if plugin_panel is not None and getattr(plugin_panel, "_mf_filter_panel_open", False):
            plugin_panel._close_mf_filter_panel()
        # Remember plugin panel state so _close restores only if it was visible.
        app = self.winfo_toplevel()
        self._filter_plugin_panel_was_visible = bool(getattr(app, "_plugin_panel_visible", False))
        self._filter_panel_open = True
        # Use scaled minsize so the panel isn't squeezed at higher UI scale.
        self.grid_columnconfigure(0, minsize=scaled(380))
        self._filter_side_panel.grid()
        for key, _label, attr in _FILTER_CHECKBOXES:
            cb = self._fsp_checkboxes[key]
            val = int(getattr(self, attr) or 0)
            if isinstance(cb, TriStateCheckBox):
                cb.set_state(val)
            else:
                self._fsp_vars[key].set(1 if val else 0)
        self._refresh_archive_filter_label()
        self._refresh_game_specific_filters()
        self._refresh_filter_category_list()
        self._refresh_filter_filetype_list()
        self._update_filter_btn_color()
        # Defer the plugin-panel hide so the filter panel paints first; the
        # modlist reflow that follows is the slow part.
        if self._filter_plugin_panel_was_visible and hasattr(app, "_toggle_plugin_panel"):
            self.after_idle(app._toggle_plugin_panel)

    def _refresh_game_specific_filters(self) -> None:
        """Show/hide game-specific filter checkboxes based on the active game.
        Hidden checkboxes also have their state cleared so a stale filter from
        a previous game can't keep hiding rows."""
        game = getattr(self, "_game", None)
        for key, predicate in _GAME_SPECIFIC_FILTERS.items():
            cb = getattr(self, "_fsp_checkboxes", {}).get(key)
            if cb is None:
                continue
            show = bool(game is not None and predicate(game))
            if show:
                if not cb.winfo_ismapped():
                    # Keep it within the "By status" group, above the category list.
                    before = getattr(self, "_fsp_category_label", None)
                    if before is not None and before.winfo_exists():
                        cb.pack(anchor="w", fill="x", pady=3, before=before)
                    else:
                        cb.pack(anchor="w", fill="x", pady=3)
            else:
                # Clear any active state, then hide the widget.
                attr = next((a for k, _l, a in _FILTER_CHECKBOXES if k == key), None)
                if attr is not None and int(getattr(self, attr, 0) or 0):
                    if isinstance(cb, TriStateCheckBox):
                        cb.set_state(STATE_OFF)
                    else:
                        self._fsp_vars[key].set(0)
                    self._on_filter_panel_change()
                cb.pack_forget()

    def _refresh_archive_filter_label(self) -> None:
        """Relabel the 'has archives' checkbox to match the active game's
        archive type — 'BSA archives' for older Bethesda titles, 'BA2
        archives' for Fallout 4 / Starfield. Falls back to 'BSA' for
        games without archive support."""
        cb = getattr(self, "_fsp_checkboxes", {}).get("filter_has_bsa")
        if cb is None:
            return
        archive_exts = getattr(self._game, "archive_extensions", None) if getattr(self, "_game", None) else None
        if archive_exts and ".ba2" in archive_exts:
            label = "Mods with BA2 archives"
        else:
            label = "Mods with BSA archives"
        try:
            cb.configure(text=label)
        except Exception:
            pass

    def _close_filter_side_panel(self):
        self._filter_panel_open = False
        self._filter_side_panel.grid_remove()
        self.grid_columnconfigure(0, minsize=0)
        self._update_filter_btn_color()
        # Restore the plugins panel only if it was visible when we opened.
        app = self.winfo_toplevel()
        if getattr(self, "_filter_plugin_panel_was_visible", False) \
                and not getattr(app, "_plugin_panel_visible", True) \
                and hasattr(app, "_toggle_plugin_panel"):
            app._toggle_plugin_panel()
        self._filter_plugin_panel_was_visible = False

    def _apply_modlist_filters(self, state: dict):
        """Apply filter state from the side panel and redraw."""
        for key, _label, attr in _FILTER_CHECKBOXES:
            setattr(self, attr, int(state.get(key, 0) or 0))
        self._filter_categories = state.get("filter_categories") or frozenset()
        self._filter_categories_exclude = state.get("filter_categories_exclude") or frozenset()
        self._filter_filetypes = state.get("filter_filetypes") or frozenset()
        self._filter_filetypes_exclude = state.get("filter_filetypes_exclude") or frozenset()
        self._update_filter_btn_color()
        self._invalidate_derived_caches()
        self._redraw()

    def _any_modlist_filters_active(self) -> bool:
        if any(getattr(self, attr) for _key, _label, attr in _FILTER_CHECKBOXES):
            return True
        if self._filter_categories or self._filter_filetypes:
            return True
        if getattr(self, "_filter_categories_exclude", None) or getattr(self, "_filter_filetypes_exclude", None):
            return True
        return False

    def _update_filter_btn_color(self) -> None:
        btn = getattr(self, "_filter_btn", None)
        if btn is None:
            return
        if self._any_modlist_filters_active():
            btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)
        else:
            btn.configure(fg_color=BG_HEADER, hover_color=BG_HOVER)
