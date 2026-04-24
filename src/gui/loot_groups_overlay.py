"""
LOOT Groups overlay — configure LOOT groups and group ordering rules.

Groups and rules are stored in the active profile's userlist.yaml alongside
plugin entries. Rules define load order relationships between groups, e.g.
"groupA loads after groupB".
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

import gui.theme as _theme
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BORDER,
    FONT_BOLD,
    FONT_NORMAL,
    FONT_FAMILY,
    FONT_SMALL,
    font_sized_px,
    TEXT_DIM,
    TEXT_MAIN,
)


_DEFAULT_GROUP = "default"


class LootGroupsOverlay(tk.Frame):
    """
    Overlay for managing LOOT groups and group ordering rules.
    Placed over the plugin panel when the user clicks 'Groups'.

    Reads/writes the groups section of the active profile's userlist.yaml
    via the parse/write helpers passed in from plugin_panel.
    """

    def __init__(
        self,
        parent: tk.Widget,
        userlist_path: Path,
        parse_userlist: Callable[[Path], dict],
        write_userlist: Callable[[Path, dict], None],
        on_close: Optional[Callable[[], None]] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._ul_path = userlist_path
        self._parse_userlist = parse_userlist
        self._write_userlist = write_userlist
        self._on_close = on_close
        self._on_saved = on_saved

        data = self._parse_userlist(userlist_path) if userlist_path.is_file() else {"plugins": [], "groups": []}
        # groups: list of {"name": str, "after": [str]}
        self._groups: list[str] = [g["name"] for g in data.get("groups", []) if g.get("name")]
        if _DEFAULT_GROUP not in self._groups:
            self._groups.insert(0, _DEFAULT_GROUP)

        # rules: list of (group_a, "after"|"before", group_b)
        self._rules: list[tuple[str, str, str]] = []
        for g in data.get("groups", []):
            name = g.get("name", "")
            for after_grp in g.get("after", []):
                self._rules.append((name, "after", after_grp))

        self._build()

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
            toolbar, text="LOOT Groups - Right click plugins to add them to groups",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        ctk.CTkButton(
            toolbar, text="Save", width=80, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            font=FONT_BOLD, command=self._do_save,
        ).pack(side="right", padx=(0, 4), pady=5)

        # Body: two columns — Groups (left) | Rules (right)
        body = tk.Frame(self, bg=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(2, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # Divider
        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=1, sticky="ns", padx=8)

        self._build_groups_panel(body)
        self._build_rules_panel(body)

    def _build_groups_panel(self, body: tk.Frame):
        left = tk.Frame(body, bg=BG_DEEP)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        tk.Label(left, text="Groups", font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_DEEP,
                 anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # List
        list_frame = tk.Frame(left, bg=BG_PANEL, highlightthickness=1,
                              highlightbackground=BORDER)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        self._groups_listbox = tk.Listbox(
            list_frame,
            bg=BG_PANEL, fg=TEXT_MAIN, selectbackground=ACCENT,
            selectforeground="white", activestyle="none",
            relief="flat", bd=0, highlightthickness=0,
            font=font_sized_px(FONT_FAMILY, 11),
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical",
                           command=self._groups_listbox.yview,
                           bg=_theme.BG_SEP, troughcolor=BG_DEEP,
                           activebackground=ACCENT, highlightthickness=0, bd=0)
        self._groups_listbox.configure(yscrollcommand=vsb.set)
        self._groups_listbox.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Add row
        add_row = tk.Frame(left, bg=BG_DEEP)
        add_row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        add_row.grid_columnconfigure(0, weight=1)

        self._new_group_var = tk.StringVar()
        tk.Entry(
            add_row, textvariable=self._new_group_var,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=font_sized_px(FONT_FAMILY, 11),
            highlightthickness=1, highlightbackground=BORDER,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            add_row, text="Add", width=60, height=28,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_SMALL, command=self._add_group,
        ).grid(row=0, column=1)

        ctk.CTkButton(
            left, text="Remove Selected", width=140, height=28,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=FONT_SMALL, command=self._remove_group,
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))

        self._repaint_groups()

    def _build_rules_panel(self, body: tk.Frame):
        right = tk.Frame(body, bg=BG_DEEP)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)

        tk.Label(right, text="Group Rules", font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_DEEP,
                 anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # New rule row (top)
        new_rule_frame = tk.Frame(right, bg=BG_DEEP)
        new_rule_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        new_rule_frame.grid_columnconfigure(0, weight=1)
        new_rule_frame.grid_columnconfigure(2, weight=1)

        tk.Label(new_rule_frame, text="Add rule:", font=FONT_SMALL,
                 fg=TEXT_DIM, bg=BG_DEEP).grid(row=0, column=0, columnspan=3,
                                               sticky="w", pady=(0, 4))

        self._rule_a_var = tk.StringVar()
        self._rule_rel_var = tk.StringVar(value="after")
        self._rule_b_var = tk.StringVar()

        _om_kw = dict(
            font=FONT_SMALL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
            text_color=TEXT_MAIN, height=28,
        )

        self._rule_a_menu = ctk.CTkOptionMenu(
            new_rule_frame, variable=self._rule_a_var,
            values=self._groups or [_DEFAULT_GROUP], **_om_kw,
        )
        self._rule_a_menu.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        self._rule_rel_menu = ctk.CTkOptionMenu(
            new_rule_frame, variable=self._rule_rel_var,
            values=["after", "before"],
            width=90, **_om_kw,
        )
        self._rule_rel_menu.grid(row=1, column=1, padx=(0, 4))

        self._rule_b_menu = ctk.CTkOptionMenu(
            new_rule_frame, variable=self._rule_b_var,
            values=self._groups or [_DEFAULT_GROUP], **_om_kw,
        )
        self._rule_b_menu.grid(row=1, column=2, sticky="ew")

        ctk.CTkButton(
            new_rule_frame, text="Add Rule", width=100, height=28,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_SMALL, command=self._add_rule,
        ).grid(row=1, column=3, padx=(8, 0))

        # Rules list
        rules_frame = tk.Frame(right, bg=BG_PANEL, highlightthickness=1,
                               highlightbackground=BORDER)
        rules_frame.grid(row=2, column=0, sticky="nsew")
        rules_frame.grid_rowconfigure(0, weight=1)
        rules_frame.grid_columnconfigure(0, weight=1)

        self._rules_canvas = tk.Canvas(rules_frame, bg=BG_PANEL, bd=0,
                                       highlightthickness=0, yscrollincrement=1)
        rules_vsb = tk.Scrollbar(rules_frame, orient="vertical",
                                 command=self._rules_canvas.yview,
                                 bg=_theme.BG_SEP, troughcolor=BG_DEEP,
                                 activebackground=ACCENT, highlightthickness=0, bd=0)
        self._rules_canvas.configure(yscrollcommand=rules_vsb.set)
        self._rules_canvas.grid(row=0, column=0, sticky="nsew")
        rules_vsb.grid(row=0, column=1, sticky="ns")
        if not LEGACY_WHEEL_REDUNDANT:
            self._rules_canvas.bind("<Button-4>",
                                    lambda e: self._rules_canvas.yview_scroll(-3, "units"))
            self._rules_canvas.bind("<Button-5>",
                                    lambda e: self._rules_canvas.yview_scroll(3, "units"))

        self._rules_inner = tk.Frame(self._rules_canvas, bg=BG_PANEL)
        self._rules_canvas.create_window((0, 0), window=self._rules_inner, anchor="nw")
        self._rules_inner.bind("<Configure>", lambda e: self._rules_canvas.configure(
            scrollregion=self._rules_canvas.bbox("all")))

        self._repaint_rules()

    # ------------------------------------------------------------------
    # Groups management
    # ------------------------------------------------------------------

    def _repaint_groups(self):
        self._groups_listbox.delete(0, tk.END)
        for g in self._groups:
            self._groups_listbox.insert(tk.END, g)

    def _add_group(self):
        name = self._new_group_var.get().strip()
        if not name or name in self._groups:
            return
        self._groups.append(name)
        self._new_group_var.set("")
        self._repaint_groups()
        self._refresh_rule_menus()

    def _remove_group(self):
        sel = self._groups_listbox.curselection()
        if not sel:
            return
        name = self._groups[sel[0]]
        if name == _DEFAULT_GROUP:
            return  # don't remove default
        self._groups.pop(sel[0])
        # Remove group ordering rules referencing this group
        self._rules = [r for r in self._rules if r[0] != name and r[2] != name]
        # Update plugins in userlist that were in this group
        if self._ul_path.is_file():
            data = self._parse_userlist(self._ul_path)
            # Only rewrite if any plugin actually references the removed group
            if any(e.get("group", "") == name for e in data.get("plugins", [])):
                new_plugins = []
                for entry in data.get("plugins", []):
                    if entry.get("group", "") == name:
                        has_rules = entry.get("before") or entry.get("after")
                        if has_rules:
                            entry["group"] = _DEFAULT_GROUP
                            new_plugins.append(entry)
                        # else: drop the entry entirely
                    else:
                        new_plugins.append(entry)
                data["plugins"] = new_plugins
                self._write_userlist(self._ul_path, data)
        self._repaint_groups()
        self._refresh_rule_menus()
        self._repaint_rules()

    def _refresh_rule_menus(self):
        vals = self._groups or [_DEFAULT_GROUP]
        self._rule_a_menu.configure(values=vals)
        self._rule_b_menu.configure(values=vals)
        if self._rule_a_var.get() not in vals:
            self._rule_a_var.set(vals[0])
        if self._rule_b_var.get() not in vals:
            self._rule_b_var.set(vals[0])

    # ------------------------------------------------------------------
    # Rules management
    # ------------------------------------------------------------------

    def _repaint_rules(self):
        for w in self._rules_inner.winfo_children():
            w.destroy()

        if not self._rules:
            tk.Label(
                self._rules_inner, text="No rules defined.",
                font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            ).pack(padx=12, pady=12)
            return

        for i, (a, rel, b) in enumerate(self._rules):
            row_bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT
            row = tk.Frame(self._rules_inner, bg=row_bg)
            row.pack(fill="x")
            row.grid_columnconfigure(1, weight=1)

            tk.Label(row, text=a, font=FONT_SMALL, fg=TEXT_MAIN, bg=row_bg,
                     anchor="w").pack(side="left", padx=(10, 6), pady=4)
            tk.Label(row, text=rel, font=FONT_SMALL, fg=ACCENT, bg=row_bg,
                     anchor="w").pack(side="left", padx=(0, 6), pady=4)
            tk.Label(row, text=b, font=FONT_SMALL, fg=TEXT_MAIN, bg=row_bg,
                     anchor="w").pack(side="left", pady=4)

            ctk.CTkButton(
                row, text="✕", width=26, height=22,
                fg_color=BG_DEEP, hover_color="#6b3333", text_color=TEXT_DIM,
                font=FONT_SMALL, command=lambda idx=i: self._remove_rule(idx),
            ).pack(side="right", padx=(4, 6), pady=3)

    def _add_rule(self):
        a = self._rule_a_var.get().strip()
        rel = self._rule_rel_var.get().strip()
        b = self._rule_b_var.get().strip()
        if not a or not b or a == b:
            return
        # Normalise: "before" means b loads after a → store as (b, "after", a)
        if rel == "before":
            a, b = b, a
            rel = "after"
        if (a, rel, b) in self._rules:
            return
        # Check for reverse rule which would create a cycle
        if (b, "after", a) in self._rules:
            return
        self._rules.append((a, rel, b))
        self._repaint_rules()
        self._save_current()

    def _remove_rule(self, idx: int):
        if 0 <= idx < len(self._rules):
            self._rules.pop(idx)
            self._repaint_rules()
            self._save_current()

    # ------------------------------------------------------------------
    # Save / Close
    # ------------------------------------------------------------------

    def _save_current(self):
        data = self._parse_userlist(self._ul_path) if self._ul_path.is_file() else {"plugins": [], "groups": []}

        group_after: dict[str, list[str]] = {g: [] for g in self._groups}
        for a, _rel, b in self._rules:
            if a in group_after:
                if b not in group_after[a]:
                    group_after[a].append(b)
            else:
                group_after[a] = [b]

        new_groups = []
        for g in self._groups:
            if g == _DEFAULT_GROUP and not group_after.get(g):
                continue
            entry: dict = {"name": g}
            if group_after.get(g):
                entry["after"] = group_after[g]
            new_groups.append(entry)

        data["groups"] = new_groups
        self._ul_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_userlist(self._ul_path, data)

        if self._on_saved:
            self._on_saved()

    def _do_save(self):
        self._save_current()
        self._do_close()

    def _do_close(self):
        if self._on_close:
            self._on_close()
        self.destroy()
