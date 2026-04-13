"""
Downloads panel — scans the user's Downloads folder for archive files
(.zip, .7z, .rar, .tar.gz, .tar) and displays them in a canvas list.

Right-clicking an entry triggers the standard mod-install flow as if the
user had clicked "Install Mod" and selected that file manually.

Users can add extra scan locations via the Locations button; paths are
saved to ~/.config/AmethystModManager/download_locations.json.
"""

import os
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional, Set

import customtkinter as ctk
from gui.ctk_components import CTkPopupMenu
from gui.download_locations_overlay import load_extra_download_locations
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_HOVER,
    BG_SELECT,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    TEXT_SEP,
    BORDER,
    FONT_NORMAL,
    FONT_SMALL,
    FONT_HEADER,
    scaled,
)

ROW_H     = scaled(40)
BTN_COL_W = scaled(90)   # px reserved on the right for the Install button
SIZE_COL_W = scaled(70)  # px reserved for the file-size text (left of button col)
NAME_PAD_L = scaled(8)   # left padding for the filename text
NAME_PAD_R = scaled(8)   # gap between filename text and size column

_POOL_SIZE = 40  # pre-allocated canvas slots (covers ~40 visible rows)

# Archive extensions we care about (lowercase, with dot)
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}

from gui.text_utils import truncate_text_tk_call as _truncate_text_cached


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


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"




class DownloadsPanel:
    """
    Canvas-based panel that lists archive files found in ~/Downloads.

    Uses pool-based virtual rendering: a fixed number of canvas items are
    pre-allocated and repositioned/reconfigured on scroll rather than
    being destroyed and recreated.

    This is *not* a standalone widget — it builds its widgets inside an
    existing parent frame (the "Downloads" tab of PluginPanel).
    """

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn=None,
        install_fn=None,
        on_open_locations: Optional[Callable[[], None]] = None,
        get_installed_filenames: Optional[Callable[[], Set[str]]] = None,
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._install_fn = install_fn or (lambda path: None)
        self._on_open_locations = on_open_locations or (lambda: None)
        self._get_installed_filenames = get_installed_filenames or (lambda: set())

        # Data: list of (Path, size_str) — size cached at scan time
        self._files: list[tuple[Path, str]] = []
        self._sel_idx: int = -1
        self._canvas_w: int = 400
        self._resize_after_id: str | None = None
        self._context_menu: CTkPopupMenu | None = None

        # Pool state
        self._pool_bg: list[int] = []
        self._pool_name: list[int] = []
        self._pool_size: list[int] = []
        self._pool_slot: list[int] = []  # data index mapped to each slot, -1 = free
        self._pool_btns: list[tk.Button] = []
        self._pool_btn_ids: list[int] = []  # canvas window item ids

        self._build(parent_tab)
        self._create_pool()
        self.refresh()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=scaled(28))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        ctk.CTkButton(
            toolbar, text="\u21ba Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self.refresh,
        ).pack(side="left", padx=8, pady=2)
        ctk.CTkButton(
            toolbar, text="Locations", width=85, height=26,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=self._on_open_locations,
        ).pack(side="left", padx=(0, 8), pady=2)

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
            highlightthickness=0, yscrollincrement=scaled(20), takefocus=0,
        )
        self._vsb = tk.Scrollbar(
            canvas_frame, orient="vertical", command=self._canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
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
    # Pool creation
    # ------------------------------------------------------------------

    def _create_pool(self):
        """Pre-allocate canvas items for virtual rendering."""
        c = self._canvas
        OFF = -ROW_H * 2  # off-screen parking position

        for _ in range(_POOL_SIZE):
            bg = c.create_rectangle(0, OFF, 0, OFF, fill=BG_DEEP, outline="", state="hidden")
            name_id = c.create_text(NAME_PAD_L, OFF, text="", anchor="w",
                                    font=FONT_NORMAL, fill=TEXT_MAIN, state="hidden")
            size_id = c.create_text(0, OFF, text="", anchor="e",
                                    font=FONT_SMALL, fill=TEXT_DIM, state="hidden")

            btn = tk.Button(
                c, text="Install",
                bg="#2d7a2d", fg="#ffffff",
                activebackground="#3a9e3a", activeforeground="#ffffff",
                relief="flat", font=FONT_SMALL, bd=0,
                cursor="hand2", highlightthickness=0,
            )
            btn.bind("<Button-4>",   lambda e: self._scroll(-3))
            btn.bind("<Button-5>",   lambda e: self._scroll(3))
            btn.bind("<MouseWheel>", self._on_mousewheel)
            btn_win = c.create_window(0, OFF, window=btn,
                                      width=BTN_COL_W - 10, height=ROW_H - 10,
                                      state="hidden")

            self._pool_bg.append(bg)
            self._pool_name.append(name_id)
            self._pool_size.append(size_id)
            self._pool_slot.append(-1)
            self._pool_btns.append(btn)
            self._pool_btn_ids.append(btn_win)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _get_scan_dirs(self) -> list[Path]:
        """Return all directories to scan: default Downloads + user-added locations."""
        dirs: list[Path] = [_get_downloads_dir()]
        seen: set[Path] = {dirs[0].resolve()}
        for p in load_extra_download_locations():
            path = Path(p).expanduser().resolve()
            if path.is_dir() and path not in seen:
                dirs.append(path)
                seen.add(path)
        return dirs

    def refresh(self):
        """Scan Downloads + extra locations for archive files and rebuild everything."""
        scan_dirs = self._get_scan_dirs()
        primary = scan_dirs[0]
        self._dir_label.configure(
            text=str(primary) + (" +" + str(len(scan_dirs) - 1) + " more" if len(scan_dirs) > 1 else "")
        )

        raw_files: list[tuple[Path, float, int]] = []
        for dl_dir in scan_dirs:
            if not dl_dir.is_dir():
                continue
            for entry in dl_dir.iterdir():
                if entry.is_file() and _is_archive(entry.name):
                    try:
                        st = entry.stat()
                        raw_files.append((entry, st.st_mtime, st.st_size))
                    except OSError:
                        pass

        # Sort by modification time (newest first)
        raw_files.sort(key=lambda t: t[1], reverse=True)
        # Cache size strings at scan time — no stat() during render
        installed_filenames = self._get_installed_filenames()
        self._files = [(p, _fmt_size(sz)) for p, _mt, sz in raw_files]
        self._installed_filenames = installed_filenames
        self._sel_idx = -1

        # Reset all pool slots
        for s in range(_POOL_SIZE):
            if s < len(self._pool_slot):
                self._pool_slot[s] = -1
                self._canvas.itemconfigure(self._pool_bg[s], state="hidden")
                self._canvas.itemconfigure(self._pool_name[s], state="hidden")
                self._canvas.itemconfigure(self._pool_size[s], state="hidden")
                self._canvas.itemconfigure(self._pool_btn_ids[s], state="hidden")

        total_h = len(self._files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

        count = len(self._files)
        self._log(f"Downloads: found {count} archive(s) in {len(scan_dirs)} location(s)")

    # ------------------------------------------------------------------
    # Pool-based virtual rendering
    # ------------------------------------------------------------------

    def _redraw(self):
        """Reconfigure pool slots to show only the visible viewport rows."""
        n = len(self._files)
        if not n:
            return

        c = self._canvas
        canvas_top = int(c.canvasy(0))
        canvas_h = max(c.winfo_height(), 1)
        first_row = max(0, canvas_top // ROW_H)
        last_row = min(n, (canvas_top + canvas_h) // ROW_H + 2)
        wanted = set(range(first_row, last_row))

        cw = self._canvas_w
        btn_left = cw - BTN_COL_W
        size_right = btn_left - NAME_PAD_R
        name_max_px = max(size_right - SIZE_COL_W - NAME_PAD_L - NAME_PAD_R, 20)
        btn_center_x = cw - BTN_COL_W // 2
        tk_call = c.tk.call

        # Pass 1: identify which slots are still showing wanted rows, free the rest
        showing: dict[int, int] = {}
        free: list[int] = []
        for s in range(_POOL_SIZE):
            di = self._pool_slot[s]
            if di != -1 and di in wanted:
                showing[di] = s
            else:
                if di != -1:
                    c.itemconfigure(self._pool_bg[s], state="hidden")
                    c.itemconfigure(self._pool_name[s], state="hidden")
                    c.itemconfigure(self._pool_size[s], state="hidden")
                    c.itemconfigure(self._pool_btn_ids[s], state="hidden")
                    self._pool_slot[s] = -1
                free.append(s)

        # Pass 2: reposition already-showing slots and assign free slots to new rows
        fi = 0
        for di in range(first_row, last_row):
            y0 = di * ROW_H
            y1 = y0 + ROW_H
            yc = y0 + ROW_H // 2

            if di in showing:
                # Already visible — just update position and hover bg
                s = showing[di]
                c.coords(self._pool_bg[s], 0, y0, cw, y1)
                c.coords(self._pool_name[s], NAME_PAD_L, yc)
                c.coords(self._pool_size[s], size_right, yc)
                c.coords(self._pool_btn_ids[s], btn_center_x, yc)
                # Update hover highlight
                if di == self._sel_idx:
                    bg = BG_HOVER
                elif di % 2 == 0:
                    bg = BG_ROW
                else:
                    bg = BG_ROW_ALT
                c.itemconfigure(self._pool_bg[s], fill=bg)
                continue

            if fi >= len(free):
                break
            s = free[fi]; fi += 1

            fpath, size_str = self._files[di]
            self._pool_slot[s] = di

            # Background
            if di == self._sel_idx:
                bg = BG_HOVER
            elif di % 2 == 0:
                bg = BG_ROW
            else:
                bg = BG_ROW_ALT

            c.coords(self._pool_bg[s], 0, y0, cw, y1)
            c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

            # File name (cached truncation)
            name = _truncate_text_cached(tk_call, fpath.name, FONT_NORMAL, name_max_px)
            c.coords(self._pool_name[s], NAME_PAD_L, yc)
            c.itemconfigure(self._pool_name[s], text=name, state="normal")

            # File size (pre-computed at scan time)
            c.coords(self._pool_size[s], size_right, yc)
            c.itemconfigure(self._pool_size[s], text=size_str, state="normal")

            # Install button
            is_installed = fpath.name in self._installed_filenames
            btn = self._pool_btns[s]
            btn.configure(
                text="Reinstall" if is_installed else "Install",
                bg="#c37800" if is_installed else "#2d7a2d",
                activebackground="#e28b00" if is_installed else "#3a9e3a",
                command=lambda p=fpath: self._on_install(p),
            )
            c.coords(self._pool_btn_ids[s], btn_center_x, yc)
            c.itemconfigure(self._pool_btn_ids[s], state="normal")

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")
        self._redraw()

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 3)

    def _on_resize(self, event):
        new_w = event.width
        if new_w == self._canvas_w:
            return
        self._canvas_w = new_w
        if self._resize_after_id:
            self._canvas.after_cancel(self._resize_after_id)
        self._resize_after_id = self._canvas.after(150, self._apply_resize)

    def _apply_resize(self):
        self._resize_after_id = None
        # Invalidate all pool slots so _redraw reconfigures positions
        for s in range(_POOL_SIZE):
            self._pool_slot[s] = -1
            self._canvas.itemconfigure(self._pool_bg[s], state="hidden")
            self._canvas.itemconfigure(self._pool_name[s], state="hidden")
            self._canvas.itemconfigure(self._pool_size[s], state="hidden")
            self._canvas.itemconfigure(self._pool_btn_ids[s], state="hidden")
        total_h = len(self._files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

    # ------------------------------------------------------------------
    # Hover highlight
    # ------------------------------------------------------------------

    def _on_motion(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        new_idx = idx if 0 <= idx < len(self._files) else -1
        if new_idx != self._sel_idx:
            self._sel_idx = new_idx
            self._redraw()

    def _on_leave(self, _event):
        if self._sel_idx != -1:
            self._sel_idx = -1
            self._redraw()

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def _on_install(self, fpath: Path):
        self._log(f"Installing {fpath.name} \u2026")
        self._install_fn(str(fpath))

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _on_right_click(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        if idx < 0 or idx >= len(self._files):
            return

        fpath = self._files[idx][0]
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Install Mod", lambda: self._on_install(fpath))
        menu.popup(event.x_root, event.y_root)
