"""
LOOT Plugin Rules overlay — configure per-plugin before/after rules in userlist.yaml.

Selected plugin comes from the plugin panel (passed in as selected_plugin).
Left pane: all plugins, filterable, draggable onto the right pane.
Right pane: rules for the selected plugin — each dropped plugin becomes a rule row
            with a before/after toggle and a remove button.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_SEP,
    BORDER,
    FONT_BOLD,
    FONT_FAMILY,
    FONT_SMALL,
    font_sized_px,
    TEXT_DIM,
    TEXT_MAIN,
)


class LootPluginRulesOverlay(tk.Frame):
    """
    Overlay for managing per-plugin LOOT before/after rules in userlist.yaml.
    Placed over the modlist panel when the user clicks 'Plugin Rules'.

    Left pane:  all plugins, filterable. Drag a plugin onto the right pane to add a rule.
    Right pane: rules for the selected plugin. Each rule row has a before/after toggle.
    """

    def __init__(
        self,
        parent: tk.Widget,
        plugin_names: list[str],
        userlist_path: Path,
        parse_userlist: Callable[[Path], dict],
        write_userlist: Callable[[Path, dict], None],
        selected_plugin: str = "",
        on_close: Optional[Callable[[], None]] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._plugin_names = list(plugin_names)
        self._ul_path = userlist_path
        self._parse_userlist = parse_userlist
        self._write_userlist = write_userlist
        self._on_close = on_close
        self._on_saved = on_saved

        self._selected_plugin: str = selected_plugin
        # rules: list of [rel, target] — mutable so toggle can update in-place
        self._rules: list[list[str]] = []

        # Drag state
        self._drag_ghost: tk.Toplevel | None = None
        self._drag_name: str = ""

        self._build()

        if self._selected_plugin:
            self._load_rules_for(self._selected_plugin)
            self._repaint_rules()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=42)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="LOOT Plugin Rules - Select a plugin on the plugins panel",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Body
        body = tk.Frame(self, bg=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(2, weight=2)
        body.grid_rowconfigure(0, weight=1)

        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=1, sticky="ns", padx=8)

        self._build_plugins_panel(body)
        self._build_rules_panel(body)

    def _build_plugins_panel(self, body: tk.Frame):
        left = tk.Frame(body, bg=BG_DEEP)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        tk.Label(left, text="Plugins  —  drag onto rules pane", font=FONT_BOLD,
                 fg=TEXT_MAIN, bg=BG_DEEP, anchor="w").grid(
            row=0, column=0, sticky="ew", pady=(0, 6))

        list_frame = tk.Frame(left, bg=BG_PANEL, highlightthickness=1,
                              highlightbackground=BORDER)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        self._plugins_listbox = tk.Listbox(
            list_frame,
            bg=BG_PANEL, fg=TEXT_MAIN, selectbackground=ACCENT,
            selectforeground="white", activestyle="none",
            relief="flat", bd=0, highlightthickness=0,
            font=font_sized_px(FONT_FAMILY, 11),
            exportselection=False,
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical",
                           command=self._plugins_listbox.yview,
                           bg=BG_SEP, troughcolor=BG_DEEP,
                           activebackground=ACCENT, highlightthickness=0, bd=0)
        self._plugins_listbox.configure(yscrollcommand=vsb.set)
        self._plugins_listbox.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._plugins_listbox.bind("<Button-4>",
                                   lambda e: self._plugins_listbox.yview_scroll(-3, "units"))
        self._plugins_listbox.bind("<Button-5>",
                                   lambda e: self._plugins_listbox.yview_scroll(3, "units"))

        # Drag bindings
        self._plugins_listbox.bind("<ButtonPress-1>", self._on_drag_start)
        self._plugins_listbox.bind("<B1-Motion>", self._on_drag_motion)
        self._plugins_listbox.bind("<ButtonRelease-1>", self._on_drag_release)

        # Search bar
        search_frame = tk.Frame(left, bg=BG_DEEP)
        search_frame.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        search_frame.grid_columnconfigure(0, weight=1)
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search_change)
        tk.Entry(
            search_frame, textvariable=self._search_var,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=font_sized_px(FONT_FAMILY, 11),
            highlightthickness=1, highlightbackground=BORDER,
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(search_frame, text="Filter", font=FONT_SMALL,
                 fg=TEXT_DIM, bg=BG_DEEP).grid(row=0, column=1, padx=(6, 0))

        self._repaint_plugins()

    def _build_rules_panel(self, body: tk.Frame):
        right = tk.Frame(body, bg=BG_DEEP)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        plugin_label = self._selected_plugin or "— no plugin selected —"
        self._rules_title = tk.Label(
            right, text=f"Rules for: {plugin_label}",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_DEEP, anchor="w")
        self._rules_title.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # Drop zone / rules list
        rules_outer = tk.Frame(right, bg=BG_PANEL, highlightthickness=1,
                               highlightbackground=BORDER)
        rules_outer.grid(row=1, column=0, sticky="nsew")
        rules_outer.grid_rowconfigure(0, weight=1)
        rules_outer.grid_columnconfigure(0, weight=1)

        self._rules_canvas = tk.Canvas(rules_outer, bg=BG_PANEL, bd=0,
                                       highlightthickness=0, yscrollincrement=1)
        rules_vsb = tk.Scrollbar(rules_outer, orient="vertical",
                                 command=self._rules_canvas.yview,
                                 bg=BG_SEP, troughcolor=BG_DEEP,
                                 activebackground=ACCENT, highlightthickness=0, bd=0)
        self._rules_canvas.configure(yscrollcommand=rules_vsb.set)
        self._rules_canvas.grid(row=0, column=0, sticky="nsew")
        rules_vsb.grid(row=0, column=1, sticky="ns")
        self._rules_canvas.bind("<Button-4>",
                                lambda e: self._rules_canvas.yview_scroll(-3, "units"))
        self._rules_canvas.bind("<Button-5>",
                                lambda e: self._rules_canvas.yview_scroll(3, "units"))

        self._rules_inner = tk.Frame(self._rules_canvas, bg=BG_PANEL)
        self._rules_inner_id = self._rules_canvas.create_window((0, 0), window=self._rules_inner, anchor="nw")
        self._rules_inner.bind("<Configure>", lambda e: self._rules_canvas.configure(
            scrollregion=self._rules_canvas.bbox("all")))
        self._rules_canvas.bind("<Configure>", lambda e: self._rules_canvas.itemconfigure(
            self._rules_inner_id, width=e.width))

        # Accept drops on the canvas and inner frame
        for w in (self._rules_canvas, self._rules_inner, rules_outer):
            w.bind("<ButtonRelease-1>", self._on_drop_on_rules)

    def set_selected_plugin(self, name: str):
        """Called by plugin_panel when the user clicks a plugin row."""
        if name == self._selected_plugin:
            return
        self._selected_plugin = name
        self._rules_title.configure(text=f"Rules for: {name}")
        self._load_rules_for(name)
        self._repaint_plugins(self._search_var.get())
        self._repaint_rules()

    # ------------------------------------------------------------------
    # Plugins list
    # ------------------------------------------------------------------

    def _repaint_plugins(self, filter_text: str = ""):
        self._plugins_listbox.delete(0, tk.END)
        ft = filter_text.lower()
        self._displayed_plugins: list[str] = [
            n for n in self._plugin_names
            if ft in n.lower() and n.lower() != self._selected_plugin.lower()
        ]
        for name in self._displayed_plugins:
            self._plugins_listbox.insert(tk.END, name)

    def _on_search_change(self, *_):
        self._repaint_plugins(self._search_var.get())

    # ------------------------------------------------------------------
    # Drag
    # ------------------------------------------------------------------

    def _on_drag_start(self, event):
        idx = self._plugins_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._displayed_plugins):
            self._drag_name = ""
            return
        self._plugins_listbox.selection_clear(0, tk.END)
        self._plugins_listbox.selection_set(idx)
        self._drag_name = self._displayed_plugins[idx]

    def _on_drag_motion(self, event):
        if not self._drag_name:
            return
        rx = event.x_root
        ry = event.y_root
        if self._drag_ghost is None:
            self._drag_ghost = tk.Toplevel(self)
            self._drag_ghost.overrideredirect(True)
            self._drag_ghost.attributes("-topmost", True)
            self._drag_ghost.configure(bg=ACCENT)
            tk.Label(
                self._drag_ghost, text=self._drag_name,
                font=font_sized_px(FONT_FAMILY, 10), fg="white", bg=ACCENT,
                padx=8, pady=3,
            ).pack()
        self._drag_ghost.geometry(f"+{rx + 12}+{ry + 4}")

        # Highlight drop zone if hovering over it
        self._update_drop_highlight(rx, ry)

    def _on_drag_release(self, event):
        self._destroy_ghost()
        if not self._drag_name:
            return
        # Check if released over the rules pane
        name = self._drag_name
        self._drag_name = ""  # Clear before drop to prevent double-fire from _on_drop_on_rules
        rx, ry = event.x_root, event.y_root
        if self._is_over_rules_pane(rx, ry):
            self._drop_plugin(name)
        self._clear_drop_highlight()

    def _on_drop_on_rules(self, event):
        # Handles the case where ButtonRelease fires on the rules widgets
        if self._drag_name:
            name = self._drag_name
            self._drag_name = ""
            self._drop_plugin(name)
        self._destroy_ghost()
        self._clear_drop_highlight()

    def _destroy_ghost(self):
        if self._drag_ghost:
            self._drag_ghost.destroy()
            self._drag_ghost = None

    def _is_over_rules_pane(self, rx: int, ry: int) -> bool:
        w = self._rules_canvas
        try:
            wx, wy = w.winfo_rootx(), w.winfo_rooty()
            ww, wh = w.winfo_width(), w.winfo_height()
            return wx <= rx <= wx + ww and wy <= ry <= wy + wh
        except Exception:
            return False

    def _update_drop_highlight(self, rx: int, ry: int):
        over = self._is_over_rules_pane(rx, ry)
        color = ACCENT if over else BORDER
        try:
            self._rules_canvas.master.configure(highlightbackground=color)
        except Exception:
            pass

    def _clear_drop_highlight(self):
        try:
            self._rules_canvas.master.configure(highlightbackground=BORDER)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Drop → add rule
    # ------------------------------------------------------------------

    def _drop_plugin(self, name: str):
        if not self._selected_plugin:
            return
        if name.lower() == self._selected_plugin.lower():
            return
        # Default to "after"; skip if already present (any rel)
        for _rel, target in self._rules:
            if target.lower() == name.lower():
                return
        self._rules.append(["after", name])
        self._repaint_rules()
        self._save_current()

    # ------------------------------------------------------------------
    # Rules pane
    # ------------------------------------------------------------------

    def _load_rules_for(self, plugin_name: str):
        self._rules = []
        if not self._ul_path.is_file():
            return
        data = self._parse_userlist(self._ul_path)
        for entry in data.get("plugins", []):
            if entry.get("name", "").lower() == plugin_name.lower():
                for t in entry.get("after", []):
                    self._rules.append(["after", t])
                for t in entry.get("before", []):
                    self._rules.append(["before", t])
                break

    def _repaint_rules(self):
        for w in self._rules_inner.winfo_children():
            w.destroy()

        if not self._selected_plugin:
            tk.Label(
                self._rules_inner,
                text="No plugin selected.\nRight-click a plugin and choose 'Plugin Rules'.",
                font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL, justify="left",
            ).pack(padx=12, pady=12)
            return

        if not self._rules:
            tk.Label(
                self._rules_inner,
                text="No rules yet.\nDrag a plugin from the left pane to add a rule.",
                font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL, justify="left",
            ).pack(padx=12, pady=12)
            return

        for i, rule in enumerate(self._rules):
            rel, target = rule
            row_bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT
            row = tk.Frame(self._rules_inner, bg=row_bg)
            row.pack(fill="x")

            # Before/after toggle button
            def _toggle(r=rule, row_idx=i):
                r[0] = "before" if r[0] == "after" else "after"
                self._repaint_rules()
                self._save_current()

            rel_btn = ctk.CTkButton(
                row, text=rel, width=80, height=28,
                fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=ACCENT,
                font=FONT_SMALL, command=_toggle,
            )
            rel_btn.pack(side="left", padx=(10, 6), pady=4)

            tk.Label(row, text=target, font=FONT_SMALL, fg=TEXT_MAIN, bg=row_bg,
                     anchor="w").pack(side="left", pady=4, fill="x", expand=True)

            ctk.CTkButton(
                row, text="✕", width=26, height=22,
                fg_color=BG_DEEP, hover_color="#6b3333", text_color=TEXT_DIM,
                font=FONT_SMALL, command=lambda idx=i: self._remove_rule(idx),
            ).pack(side="right", padx=(4, 6), pady=3)

    def _remove_rule(self, idx: int):
        if 0 <= idx < len(self._rules):
            self._rules.pop(idx)
            self._repaint_rules()
            self._save_current()

    # ------------------------------------------------------------------
    # Save / Close
    # ------------------------------------------------------------------

    def _save_current(self):
        if not self._selected_plugin:
            return

        data = self._parse_userlist(self._ul_path) if self._ul_path.is_file() else {"plugins": [], "groups": []}

        plugin_name = self._selected_plugin
        existing = next(
            (e for e in data["plugins"] if e.get("name", "").lower() == plugin_name.lower()),
            {},
        )
        data["plugins"] = [
            e for e in data["plugins"]
            if e.get("name", "").lower() != plugin_name.lower()
        ]

        after_list = [t for rel, t in self._rules if rel == "after"]
        before_list = [t for rel, t in self._rules if rel == "before"]

        if after_list or before_list or existing:
            # Merge into the existing entry to preserve extra fields (dirty, tag, etc.)
            entry = dict(existing)
            entry["name"] = plugin_name
            if not entry.get("group"):
                entry["group"] = "default"
            # Replace rule lists (clear old ones if now empty)
            if after_list:
                entry["after"] = after_list
            else:
                entry.pop("after", None)
            if before_list:
                entry["before"] = before_list
            else:
                entry.pop("before", None)
            # Only keep the entry if it has rules or a non-default group or extra fields
            has_content = (after_list or before_list
                          or entry.get("group", "default") != "default"
                          or any(k not in ("name", "group", "after", "before") for k in entry))
            if has_content:
                data["plugins"].append(entry)

        self._ul_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_userlist(self._ul_path, data)

        if self._on_saved:
            self._on_saved()

    def _do_close(self):
        if self._on_close:
            self._on_close()
        self.destroy()
