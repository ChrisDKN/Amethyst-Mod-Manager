"""
fomod_dialog.py
Step-by-step FOMOD installer wizard as a modal CustomTkinter Toplevel.
"""

from __future__ import annotations

import os
import tkinter as tk
import customtkinter as ctk
from typing import Optional
from PIL import Image as PilImage

from Utils.fomod_parser import ModuleConfig, InstallStep, Group, Plugin
from Utils.fomod_installer import (
    get_visible_steps,
    get_default_selections,
    update_flags,
    validate_selections,
    resolve_plugin_type,
)

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BG_SEP,
    BG_HOVER,
    BG_SELECT,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    TEXT_SEP,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
    FONT_HEADER,
    FONT_SEP,
)


# ---------------------------------------------------------------------------
# FomodDialog
# ---------------------------------------------------------------------------

class FomodDialog(ctk.CTkToplevel):
    """
    Modal FOMOD installer wizard.

    Usage:
        dialog = FomodDialog(parent, config, mod_root)
        parent.wait_window(dialog)
        result = dialog.result  # dict or None

    result is None if cancelled, or
        {step_name: {group_name: [plugin_name, ...]}} if finished.
    """

    DIALOG_WIDTH  = 940
    DIALOG_HEIGHT = 640
    IMAGE_WIDTH   = 300
    IMAGE_HEIGHT  = 210

    def __init__(self, parent: ctk.CTk, config: ModuleConfig,
                 mod_root: str,
                 installed_files: set[str] | None = None,
                 saved_selections: dict[str, dict[str, list[str]]] | None = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"FOMOD Installer — {config.name or 'Mod'}")
        self.geometry(f"{self.DIALOG_WIDTH}x{self.DIALOG_HEIGHT}")
        self.resizable(True, True)
        self.minsize(700, 500)

        # State
        self._config        = config
        self._mod_root      = mod_root
        self._installed     = installed_files or set()
        self._flag_state: dict[str, str] = {}
        self._all_selections: dict[str, dict[str, list[str]]] = {}
        self._saved_selections = saved_selections or {}
        self._visible_steps: list[InstallStep] = []
        self._current_idx   = 0
        # Keeps {group_name: {"vars": ..., "type": group_type, "plugins": [Plugin, ...]}}
        self._group_widgets: dict[str, dict] = {}
        # Prevent CTkImage GC
        self._current_image: Optional[ctk.CTkImage] = None
        self._current_image_path: Optional[str] = None
        self.result: Optional[dict] = None

        # Make modal (deferred so window is viewable before grab)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._build_ui()
        self._refresh_visible_steps()
        if self._visible_steps:
            self._load_step(0)
        else:
            # No steps — treat as instant finish
            self._on_finish()

    def _make_modal(self):
        """Grab input focus once the window is viewable."""
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        self._create_indicator_images()
        self._build_title_bar()
        self._build_content_area()
        self._build_button_bar()

    def _create_indicator_images(self):
        """Pre-render radio and checkbox indicator images at a visible size."""
        size = 18
        accent = ACCENT
        bg = BG_DEEP
        border_col = BORDER

        # --- Radio: off = empty circle, on = filled circle ---
        radio_off = PilImage.new("RGBA", (size, size), (0, 0, 0, 0))
        radio_on = PilImage.new("RGBA", (size, size), (0, 0, 0, 0))
        from PIL import ImageDraw
        d = ImageDraw.Draw(radio_off)
        d.ellipse([1, 1, size - 2, size - 2], outline=border_col, width=2)
        d = ImageDraw.Draw(radio_on)
        d.ellipse([1, 1, size - 2, size - 2], outline=accent, width=2)
        d.ellipse([4, 4, size - 5, size - 5], fill=accent)

        # --- Check: off = empty box, on = filled box with check ---
        check_off = PilImage.new("RGBA", (size, size), (0, 0, 0, 0))
        check_on = PilImage.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(check_off)
        d.rounded_rectangle([1, 1, size - 2, size - 2], radius=3, outline=border_col, width=2)
        d = ImageDraw.Draw(check_on)
        d.rounded_rectangle([1, 1, size - 2, size - 2], radius=3, fill=accent, outline=accent, width=2)
        # Checkmark
        d.line([(4, 9), (7, 13), (13, 5)], fill="white", width=2)

        # Convert to tk PhotoImages (keep references to prevent GC)
        self._radio_off = tk.PhotoImage(data=self._pil_to_png_bytes(radio_off))
        self._radio_on = tk.PhotoImage(data=self._pil_to_png_bytes(radio_on))
        self._check_off = tk.PhotoImage(data=self._pil_to_png_bytes(check_off))
        self._check_on = tk.PhotoImage(data=self._pil_to_png_bytes(check_on))

    @staticmethod
    def _pil_to_png_bytes(img: PilImage.Image) -> bytes:
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _build_title_bar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_columnconfigure(1, weight=0)

        self._mod_name_label = ctk.CTkLabel(
            bar, text=self._config.name,
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        )
        self._mod_name_label.grid(row=0, column=0, sticky="w", padx=12, pady=8)

        self._progress_label = ctk.CTkLabel(
            bar, text="", font=FONT_SMALL, text_color=TEXT_DIM, anchor="e"
        )
        self._progress_label.grid(row=0, column=1, sticky="e", padx=12, pady=8)

    def _build_content_area(self):
        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=0, minsize=310)
        content.grid_columnconfigure(1, weight=0, minsize=1)
        content.grid_columnconfigure(2, weight=1)
        content.grid_rowconfigure(0, weight=1)

        # --- Left panel: image + description ---
        left = ctk.CTkFrame(content, fg_color=BG_PANEL, corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self._image_label = ctk.CTkLabel(
            left, text="", fg_color=BG_DEEP,
            width=self.IMAGE_WIDTH, height=self.IMAGE_HEIGHT,
            cursor="hand2"
        )
        self._image_label.grid(row=0, column=0, sticky="ew")
        self._image_label.bind("<Button-1>", self._on_image_click)

        self._desc_box = ctk.CTkTextbox(
            left, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            font=FONT_NORMAL, state="disabled",
            wrap="word", corner_radius=0
        )
        self._desc_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        # --- Separator ---
        ctk.CTkFrame(content, fg_color=BORDER, width=1, corner_radius=0).grid(
            row=0, column=1, sticky="ns"
        )

        # --- Right panel: scrollable options ---
        self._options_scroll = ctk.CTkScrollableFrame(
            content, fg_color=BG_DEEP, corner_radius=0,
            scrollbar_button_color=BG_PANEL,
            scrollbar_button_hover_color=ACCENT
        )
        self._options_scroll.grid(row=0, column=2, sticky="nsew")
        self._options_scroll.grid_columnconfigure(0, weight=1)

        canvas = self._options_scroll._parent_canvas
        canvas.bind("<Button-4>", lambda e: canvas.yview("scroll", -1, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview("scroll",  1, "units"))

        # Bind scroll on the inner frame so all children inherit it
        inner = self._options_scroll._parent_frame
        inner.bind("<Button-4>", lambda e: canvas.yview("scroll", -1, "units"))
        inner.bind("<Button-5>", lambda e: canvas.yview("scroll",  1, "units"))

    def _build_button_bar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=50)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)

        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        self._cancel_btn = ctk.CTkButton(
            bar, text="Cancel", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_cancel
        )
        self._cancel_btn.pack(side="right", padx=(4, 12), pady=10)

        self._next_btn = ctk.CTkButton(
            bar, text="Next", width=100, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="white", command=self._on_next
        )
        self._next_btn.pack(side="right", padx=4, pady=10)

        self._back_btn = ctk.CTkButton(
            bar, text="Back", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, state="disabled",
            command=self._on_back
        )
        self._back_btn.pack(side="right", padx=4, pady=10)

        self._validation_label = ctk.CTkLabel(
            bar, text="", font=FONT_SMALL, text_color="#e06c75"
        )
        self._validation_label.pack(side="left", padx=12)

    # ------------------------------------------------------------------
    # Step rendering
    # ------------------------------------------------------------------

    def _refresh_visible_steps(self):
        self._visible_steps = get_visible_steps(
            self._config, self._flag_state, self._installed
        )

    def _load_step(self, idx: int):
        # Freeze layout during bulk widget creation
        inner = self._options_scroll._parent_frame
        inner.grid_propagate(False)

        self._clear_options_panel()
        step = self._visible_steps[idx]
        self._group_widgets = {}

        # Restore or compute default selections for this step.
        # Priority: current session > saved from previous install > computed defaults.
        # Saved selections are merged with auto-detected defaults so that newly
        # installed mods still get their compatibility patches auto-selected.
        existing = self._all_selections.get(step.name)
        if existing is None:
            defaults = get_default_selections(step, self._flag_state, self._installed)
            saved = self._saved_selections.get(step.name)
            if saved is not None:
                existing = dict(saved)
                # Merge: add auto-detected defaults for groups where saved was empty
                for group_name, default_plugins in defaults.items():
                    if not existing.get(group_name) and default_plugins:
                        existing[group_name] = default_plugins
            else:
                existing = defaults

        row_idx = 0

        # Step name header
        header = ctk.CTkLabel(
            self._options_scroll,
            text=step.name,
            font=FONT_SEP, text_color=TEXT_SEP,
            fg_color="transparent", anchor="w"
        )
        header.grid(row=row_idx, column=0, sticky="ew", padx=12, pady=(10, 4))
        row_idx += 1

        # Separator line
        ctk.CTkFrame(
            self._options_scroll, fg_color=BORDER, height=1, corner_radius=0
        ).grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(0, 8))
        row_idx += 1

        # Render each group
        for group in step.groups:
            group_selections = existing.get(group.name, [])
            row_idx = self._render_group(group, row_idx, group_selections)

        # Unfreeze and perform a single layout pass
        inner.grid_propagate(True)

        self._update_progress()

        # Show description/image for first selected plugin in the step
        first_plugin = self._first_selected_plugin(step, existing)
        if first_plugin:
            self._update_description_and_image(first_plugin)
        else:
            self._clear_left_panel()

        # Bind scroll on all new children in one pass
        self._bind_scroll_children()

    def _bind_scroll_children(self):
        """Bind mouse-wheel scroll on all current children of the options panel."""
        canvas = self._options_scroll._parent_canvas
        scroll_up = lambda e: canvas.yview("scroll", -1, "units")
        scroll_dn = lambda e: canvas.yview("scroll",  1, "units")
        # Use a stack instead of recursion for speed
        stack = list(self._options_scroll.winfo_children())
        while stack:
            w = stack.pop()
            w.bind("<Button-4>", scroll_up)
            w.bind("<Button-5>", scroll_dn)
            stack.extend(w.winfo_children())

    def _clear_options_panel(self):
        for widget in self._options_scroll.winfo_children():
            widget.destroy()
        self._group_widgets = {}

    def _render_group(self, group: Group, start_row: int,
                      existing_selections: list[str]) -> int:
        """
        Render one group into _options_scroll starting at start_row.
        Returns the next available row index.
        """
        row = start_row
        selected_set = set(existing_selections)

        # Group label
        ctk.CTkLabel(
            self._options_scroll,
            text=group.name,
            font=FONT_HEADER, text_color=TEXT_MAIN,
            fg_color=BG_HEADER, anchor="w", corner_radius=4
        ).grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 2), ipady=4)
        row += 1

        gtype = group.group_type
        plugins = group.plugins

        # Common style kwargs for lightweight tk radio/check widgets
        _base_style = dict(
            bg=BG_DEEP, fg=TEXT_MAIN, activebackground=BG_DEEP,
            activeforeground=TEXT_MAIN, selectcolor=BG_DEEP,
            font=FONT_NORMAL, bd=0, highlightthickness=0, anchor="w",
            indicatoron=False, compound="left", padx=4, pady=2,
        )
        _radio_style = {**_base_style, "image": self._radio_off, "selectimage": self._radio_on}
        _check_style = {**_base_style, "image": self._check_off, "selectimage": self._check_on}

        if gtype in ("SelectExactlyOne", "SelectAtMostOne"):
            # Radio buttons — one shared IntVar per group
            # Value -1 = nothing selected (allowed for SelectAtMostOne)
            sel_idx = -1
            for i, p in enumerate(plugins):
                if p.name in selected_set:
                    sel_idx = i
                    break

            radio_var = tk.IntVar(value=sel_idx)

            if gtype == "SelectAtMostOne":
                # "None" option
                rb = tk.Radiobutton(
                    self._options_scroll,
                    text=" None", variable=radio_var, value=-1,
                    command=lambda: self._on_radio_change(group.name, radio_var, plugins),
                    **{**_radio_style, "fg": TEXT_DIM},
                )
                rb.grid(row=row, column=0, sticky="w", padx=24, pady=2)
                rb.bind("<Enter>", lambda _e: self._clear_left_panel())
                row += 1

            for i, plugin in enumerate(plugins):
                rb = tk.Radiobutton(
                    self._options_scroll,
                    text=f" {plugin.name}", variable=radio_var, value=i,
                    command=lambda p=plugin, v=radio_var: self._on_radio_change(
                        group.name, v, plugins
                    ),
                    **_radio_style,
                )
                rb.grid(row=row, column=0, sticky="w", padx=24, pady=2)
                rb.bind("<Enter>", lambda _e, p=plugin: self._update_description_and_image(p))
                row += 1

            self._group_widgets[group.name] = {
                "type": gtype,
                "var": radio_var,
                "plugins": plugins,
            }

        elif gtype in ("SelectAtLeastOne", "SelectAny"):
            # Checkboxes — one BooleanVar per plugin
            check_vars: list[tk.BooleanVar] = []
            for plugin in plugins:
                var = tk.BooleanVar(value=(plugin.name in selected_set))
                cb = tk.Checkbutton(
                    self._options_scroll,
                    text=f" {plugin.name}", variable=var,
                    command=lambda p=plugin, v=var: self._on_check_change(
                        group.name, p, v
                    ),
                    **_check_style,
                )
                cb.grid(row=row, column=0, sticky="w", padx=24, pady=2)
                cb.bind("<Enter>", lambda _e, p=plugin: self._update_description_and_image(p))
                check_vars.append(var)
                row += 1

            self._group_widgets[group.name] = {
                "type": gtype,
                "vars": check_vars,
                "plugins": plugins,
            }

        elif gtype == "SelectAll":
            # Non-interactive — always selected
            for plugin in plugins:
                lbl = tk.Label(
                    self._options_scroll,
                    text=f"  {plugin.name}",
                    bg=BG_DEEP, fg=TEXT_DIM, font=FONT_NORMAL,
                    anchor="w",
                )
                lbl.grid(row=row, column=0, sticky="ew", padx=24, pady=2)
                lbl.bind("<Enter>", lambda _e, p=plugin: self._update_description_and_image(p))
                row += 1

            self._group_widgets[group.name] = {
                "type": gtype,
                "plugins": plugins,
            }

        # Spacing between groups
        ctk.CTkFrame(
            self._options_scroll, fg_color="transparent", height=6
        ).grid(row=row, column=0)
        row += 1

        return row

    # ------------------------------------------------------------------
    # Selection change callbacks
    # ------------------------------------------------------------------

    def _on_radio_change(self, group_name: str, var: tk.IntVar,
                         plugins: list[Plugin]):
        idx = var.get()
        if 0 <= idx < len(plugins):
            self._update_description_and_image(plugins[idx])
        else:
            self._clear_left_panel()
        self._validation_label.configure(text="")

    def _on_check_change(self, group_name: str, plugin: Plugin,
                         var: tk.BooleanVar):
        if var.get():
            self._update_description_and_image(plugin)
        self._validation_label.configure(text="")

    # ------------------------------------------------------------------
    # Left panel: image + description
    # ------------------------------------------------------------------

    def _on_image_click(self, _event=None):
        if self._current_image_path:
            self._show_lightbox(self._current_image_path)

    def _show_lightbox(self, full_path: str):
        """Open a resizable window showing the image at its natural size."""
        try:
            pil_img = PilImage.open(full_path)
        except Exception:
            return

        # Fit to 45% of screen
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        max_w = int(screen_w * 0.45)
        max_h = int(screen_h * 0.45)
        orig_w, orig_h = pil_img.size
        scale = min(max_w / orig_w, max_h / orig_h, 1.0)
        win_w = max(1, int(orig_w * scale))
        win_h = max(1, int(orig_h * scale))

        win = ctk.CTkToplevel(self)
        win.title(os.path.basename(full_path))
        win.geometry(f"{win_w}x{win_h}")
        win.resizable(True, True)
        win.transient(self)
        win.after(100, lambda: win.grab_set())

        # Close on click or Escape
        win.bind("<Escape>", lambda _e: win.destroy())

        img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                           size=(win_w, win_h))
        lbl = ctk.CTkLabel(win, text="", image=img, fg_color=BG_DEEP,
                           cursor="hand2")
        lbl.pack(fill="both", expand=True)
        lbl.bind("<Button-1>", lambda _e: win.destroy())

        # Keep reference so GC doesn't collect it
        win._img = img

    def _clear_image(self):
        """Detach any image from the label before releasing CTkImage references."""
        try:
            self._image_label._label.configure(image="")
        except Exception:
            pass
        self._current_image = None
        self._current_image_path = None

    def _update_description_and_image(self, plugin: Plugin):
        # Description
        self._desc_box.configure(state="normal")
        self._desc_box.delete("1.0", "end")
        self._desc_box.insert("end", plugin.description or "")
        self._desc_box.configure(state="disabled")

        # Image
        img: Optional[ctk.CTkImage] = None
        if plugin.image_os_path:
            img = self._load_image(plugin.image_os_path)

        self._clear_image()
        if img:
            self._current_image = img
            self._current_image_path = os.path.join(self._mod_root, plugin.image_os_path)
            self._image_label.configure(image=img, text="")
            self._image_label.grid()
        else:
            self._current_image_path = None
            self._image_label.configure(text="")
            self._image_label.grid_remove()

    def _clear_left_panel(self):
        self._clear_image()
        self._image_label.configure(text="")
        self._image_label.grid_remove()
        self._desc_box.configure(state="normal")
        self._desc_box.delete("1.0", "end")
        self._desc_box.configure(state="disabled")

    def _load_image(self, image_os_path: str) -> Optional[ctk.CTkImage]:
        """
        Load an image from mod_root/image_os_path.
        Returns a CTkImage scaled to fit the display area, or None on failure.
        Supports any format PIL can read (PNG, DDS, JPG, BMP, etc.).
        """
        full_path = os.path.join(self._mod_root, image_os_path)
        if not os.path.isfile(full_path):
            return None
        try:
            pil_img = PilImage.open(full_path)
            # Compute display size preserving aspect ratio
            orig_w, orig_h = pil_img.size
            scale = min(self.IMAGE_WIDTH / orig_w, self.IMAGE_HEIGHT / orig_h, 1.0)
            display_w = max(1, int(orig_w * scale))
            display_h = max(1, int(orig_h * scale))
            return ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                size=(display_w, display_h))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _get_current_selections(self) -> dict[str, list[str]]:
        """Read current widget state → {group_name: [plugin_name, ...]}"""
        result: dict[str, list[str]] = {}
        for group_name, widget_info in self._group_widgets.items():
            gtype = widget_info["type"]
            plugins: list[Plugin] = widget_info["plugins"]

            if gtype in ("SelectExactlyOne", "SelectAtMostOne"):
                idx = widget_info["var"].get()
                if 0 <= idx < len(plugins):
                    result[group_name] = [plugins[idx].name]
                else:
                    result[group_name] = []

            elif gtype in ("SelectAtLeastOne", "SelectAny"):
                selected = [
                    p.name for p, v in zip(plugins, widget_info["vars"])
                    if v.get()
                ]
                result[group_name] = selected

            elif gtype == "SelectAll":
                result[group_name] = [p.name for p in plugins]

        return result

    def _save_step_selections(self):
        if not self._visible_steps:
            return
        step = self._visible_steps[self._current_idx]
        self._all_selections[step.name] = self._get_current_selections()

    def _first_selected_plugin(self, step: InstallStep,
                               selections: dict[str, list[str]]) -> Optional[Plugin]:
        """Return the first plugin that is selected in the step, for the left panel."""
        for group in step.groups:
            sel = set(selections.get(group.name, []))
            for plugin in group.plugins:
                if plugin.name in sel:
                    return plugin
        # Nothing selected — return first plugin of first group with plugins
        for group in step.groups:
            if group.plugins:
                return group.plugins[0]
        return None

    # ------------------------------------------------------------------
    # Progress bar
    # ------------------------------------------------------------------

    def _update_progress(self):
        total = len(self._visible_steps)
        current = self._current_idx + 1
        self._progress_label.configure(text=f"Step {current} of {total}")
        self._back_btn.configure(
            state="normal" if self._current_idx > 0 else "disabled"
        )
        is_last = self._current_idx >= total - 1
        self._next_btn.configure(text="Finish" if is_last else "Next")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_back(self):
        if self._current_idx <= 0:
            return
        self._save_step_selections()
        self._validation_label.configure(text="")
        self._current_idx -= 1
        self._load_step(self._current_idx)

    def _on_next(self):
        self._save_step_selections()

        step = self._visible_steps[self._current_idx]
        sels = self._all_selections.get(step.name, {})
        errors = validate_selections(step, sels)
        if errors:
            self._validation_label.configure(text=errors[0])
            return
        self._validation_label.configure(text="")

        # Apply flags from completed step
        self._flag_state = update_flags(step, sels, self._flag_state)
        # Re-evaluate visible steps (flag changes may affect visibility)
        self._refresh_visible_steps()

        if self._current_idx >= len(self._visible_steps) - 1:
            self._on_finish()
        else:
            self._current_idx += 1
            self._load_step(self._current_idx)

    def _on_finish(self):
        self.result = dict(self._all_selections)
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()
