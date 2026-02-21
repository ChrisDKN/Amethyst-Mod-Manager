"""
Downloads panel — scans the user's Downloads folder for archive files
(.zip, .7z, .tar.gz, .tar) and displays them in a canvas list.

Right-clicking an entry triggers the standard mod-install flow as if the
user had clicked "Install Mod" and selected that file manually.
"""

import os
import tkinter as tk
from pathlib import Path

# ---------------------------------------------------------------------------
# Theme constants (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_ROW_ALT = "#303030"
BG_HOVER   = "#094771"
BG_SELECT  = "#0f5fa3"
ACCENT     = "#0078d4"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_SEP   = "#b0b0b0"
BORDER     = "#444444"

FONT_NORMAL = ("Segoe UI", 12)
FONT_SMALL  = ("Segoe UI", 10)
FONT_HEADER = ("Segoe UI", 11, "bold")

ROW_H     = 40
BTN_COL_W = 90   # px reserved on the right for the Install button
SIZE_COL_W = 70  # px reserved for the file-size text (left of button col)
NAME_PAD_L = 8   # left padding for the filename text
NAME_PAD_R = 8   # gap between filename text and size column

# Archive extensions we care about (lowercase, with dot)
_ARCHIVE_EXTS = {".zip", ".7z", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}


def _is_archive(name: str) -> bool:
    """Return True if *name* looks like a supported archive file."""
    low = name.lower()
    for ext in _ARCHIVE_EXTS:
        if low.endswith(ext):
            return True
    return False


def _get_downloads_dir() -> Path:
    """Return the user's Downloads directory."""
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


def _truncate_text(widget, text: str, font, max_px: int) -> str:
    """Return *text* truncated with '…' so it fits within *max_px* pixels."""
    if widget.tk.call("font", "measure", font, text) <= max_px:
        return text
    ellipsis = "…"
    ellipsis_w = widget.tk.call("font", "measure", font, ellipsis)
    while text and widget.tk.call("font", "measure", font, text) + ellipsis_w > max_px:
        text = text[:-1]
    return text + ellipsis


class DownloadsPanel:
    """
    Canvas-based panel that lists archive files found in ~/Downloads.

    This is *not* a standalone widget — it builds its widgets inside an
    existing parent frame (the "Downloads" tab of PluginPanel).
    """

    def __init__(self, parent_tab: tk.Widget, log_fn=None, install_fn=None):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._install_fn = install_fn or (lambda path: None)

        self._files: list[Path] = []
        self._sel_idx: int = -1
        self._canvas_w: int = 400
        # One persistent button per file row — never destroyed except on refresh
        self._btn_widgets: list[tk.Button] = []

        self._build(parent_tab)
        self.refresh()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        tk.Button(
            toolbar, text="↺ Refresh",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=FONT_SMALL,
            bd=0, cursor="hand2",
            command=self.refresh,
        ).pack(side="left", padx=8, pady=2)

        self._dir_label = tk.Label(
            toolbar, text="", anchor="w",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        )
        self._dir_label.pack(side="left", padx=4)

        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_frame, bg=BG_DEEP, bd=0,
            highlightthickness=0, yscrollincrement=1, takefocus=0,
        )
        self._vsb = tk.Scrollbar(
            canvas_frame, orient="vertical", command=self._canvas.yview,
        )
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._canvas.bind("<Configure>",       self._on_resize)
        self._canvas.bind("<Button-4>",        lambda e: self._scroll(-3))
        self._canvas.bind("<Button-5>",        lambda e: self._scroll(3))
        self._canvas.bind("<MouseWheel>",      self._on_mousewheel)
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Leave>",           self._on_leave)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def refresh(self):
        """Scan ~/Downloads for archive files and rebuild everything."""
        dl_dir = _get_downloads_dir()
        self._dir_label.configure(text=str(dl_dir))

        self._files = []
        if dl_dir.is_dir():
            for entry in sorted(dl_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if entry.is_file() and _is_archive(entry.name):
                    self._files.append(entry)

        self._sel_idx = -1
        self._rebuild_buttons()
        self._repaint()

        count = len(self._files)
        self._log(f"Downloads: found {count} archive(s) in {dl_dir}")

    # ------------------------------------------------------------------
    # Button management  (only recreated when the file list changes)
    # ------------------------------------------------------------------

    def _rebuild_buttons(self):
        """Destroy all existing Install buttons and create one per file."""
        for btn in self._btn_widgets:
            btn.destroy()
        self._btn_widgets.clear()

        for fpath in self._files:
            btn = tk.Button(
                self._canvas,
                text="Install",
                bg="#2d7a2d",
                fg="#ffffff",
                activebackground="#3a9e3a",
                activeforeground="#ffffff",
                relief="flat",
                font=FONT_SMALL,
                bd=0,
                cursor="hand2",
                command=lambda p=fpath: self._on_install(p),
            )
            self._btn_widgets.append(btn)

        # Place them at their initial canvas positions
        self._place_buttons()

    def _place_buttons(self):
        """Move every button to the correct canvas-coordinate position."""
        cw = self._canvas_w
        btn_center_x = cw - BTN_COL_W // 2

        for row, btn in enumerate(self._btn_widgets):
            y_center = row * ROW_H + ROW_H // 2
            self._canvas.create_window(
                btn_center_x, y_center,
                window=btn,
                width=BTN_COL_W - 10,
                height=ROW_H - 10,
                tags=f"btn{row}",
            )

    # ------------------------------------------------------------------
    # Drawing  (backgrounds + text only — buttons are untouched)
    # ------------------------------------------------------------------

    def _repaint(self):
        """Redraw row backgrounds and text without touching button widgets."""
        self._canvas.delete("bg")
        self._canvas.delete("txt")

        cw = self._canvas_w
        files = self._files
        total_h = len(files) * ROW_H

        canvas_top = int(self._canvas.canvasy(0))
        canvas_bottom = canvas_top + self._canvas.winfo_height()

        # Column x-coordinates
        btn_left    = cw - BTN_COL_W
        size_right  = btn_left - NAME_PAD_R
        size_left   = size_right - SIZE_COL_W
        name_max_px = size_left - NAME_PAD_L - NAME_PAD_R

        for row, fpath in enumerate(files):
            y_top = row * ROW_H
            y_bot = y_top + ROW_H

            if y_bot < canvas_top or y_top > canvas_bottom:
                continue

            # Row background
            if row == self._sel_idx:
                bg = BG_HOVER
            elif row % 2 == 0:
                bg = BG_ROW
            else:
                bg = BG_ROW_ALT

            self._canvas.create_rectangle(
                0, y_top, cw, y_bot, fill=bg, outline="", tags="bg",
            )

            # File name — clipped to available width
            name = _truncate_text(self._canvas, fpath.name, FONT_NORMAL, max(name_max_px, 20))
            self._canvas.create_text(
                NAME_PAD_L, y_top + ROW_H // 2,
                text=name, anchor="w",
                font=FONT_NORMAL, fill=TEXT_MAIN, tags="txt",
            )

            # File size
            try:
                size_str = self._fmt_size(fpath.stat().st_size)
            except OSError:
                size_str = ""
            self._canvas.create_text(
                size_right, y_top + ROW_H // 2,
                text=size_str, anchor="e",
                font=FONT_SMALL, fill=TEXT_DIM, tags="txt",
            )

        self._canvas.configure(scrollregion=(0, 0, cw, max(total_h, 1)))
        # Keep buttons visually on top of the freshly drawn backgrounds
        self._canvas.tag_raise("all")

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")
        self._repaint()

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 3)

    def _on_resize(self, event):
        new_w = event.width
        if new_w == self._canvas_w:
            return
        self._canvas_w = new_w
        # Buttons must be repositioned when the width changes
        self._canvas.delete("all")
        self._place_buttons()
        self._repaint()

    # ------------------------------------------------------------------
    # Hover highlight
    # ------------------------------------------------------------------

    def _on_motion(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        new_idx = idx if 0 <= idx < len(self._files) else -1
        if new_idx != self._sel_idx:
            self._sel_idx = new_idx
            self._repaint()

    def _on_leave(self, _event):
        if self._sel_idx != -1:
            self._sel_idx = -1
            self._repaint()

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def _on_install(self, fpath: Path):
        self._log(f"Installing {fpath.name} …")
        self._install_fn(str(fpath))

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _on_right_click(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        if idx < 0 or idx >= len(self._files):
            return

        fpath = self._files[idx]
        menu = tk.Menu(self._canvas, tearoff=0,
                       bg=BG_PANEL, fg=TEXT_MAIN,
                       activebackground=ACCENT,
                       activeforeground=TEXT_MAIN,
                       font=FONT_SMALL)
        menu.add_command(label="Install Mod", command=lambda: self._on_install(fpath))
        menu.tk_popup(event.x_root, event.y_root)
