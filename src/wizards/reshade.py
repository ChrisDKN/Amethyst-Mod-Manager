"""
ReShade installation wizard.

Multi-step dialog that walks the user through:
  1. Downloading the latest ReShade installer from reshade.me and extracting
     the DLL (the installer is a self-extracting zip).
  2. Installing d3dcompiler_47 into the game's Proton prefix via protontricks.
  3. Copying all ReShade files to the game folder (or Root_Folder staging):
       - <reshade_dll>       (e.g. dxgi.dll)
       - ReShade.ini         (bundled, uses relative shader paths)
       - ReShadePreset.ini   (empty preset)
       - reshade-shaders/    (bundled Shaders + Textures)
     and applying the Wine DLL override to the Proton prefix.
"""

from __future__ import annotations

import io
import re
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.protontricks import install_d3dcompiler_47, protontricks_available
from Utils.deploy import apply_wine_dll_overrides

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# ---------------------------------------------------------------------------
# Theme constants (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_OK    = "#6bc76b"
TEXT_WARN  = "#e5a04a"
TEXT_ERR   = "#e06c6c"
BORDER     = "#444444"

FONT_NORMAL = ("Segoe UI", 14)
FONT_BOLD   = ("Segoe UI", 14, "bold")
FONT_SMALL  = ("Segoe UI", 12)

_RESHADE_BASE_URL = "https://reshade.me/downloads/"
_RESHADE_HOME_URL = "https://reshade.me/"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_latest_reshade_url() -> tuple[str, str]:
    """Return (download_url, version_string) for the latest ReShade release.

    Raises RuntimeError if the version cannot be determined.
    """
    req = urllib.request.Request(
        _RESHADE_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Match e.g. "ReShade_Setup_6.7.3.exe" (not the _Addon variant)
    match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe(?!"[^"]*Addon)', html)
    if not match:
        # Fallback: accept any non-Addon exe
        match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe', html)
    if not match:
        raise RuntimeError("Could not find ReShade download link on reshade.me.")

    version = match.group(1)
    url = f"{_RESHADE_BASE_URL}ReShade_Setup_{version}.exe"
    return url, version


def _download_and_extract_reshade_dll(dest_dir: Path, arch: int = 64) -> Path:
    """Download the latest ReShade installer and extract the DLL to *dest_dir*.

    *arch* selects ``ReShade64.dll`` (default) or ``ReShade32.dll`` for
    32-bit games (Fallout 3/NV, Oblivion, Skyrim classic, etc.).

    The installer .exe is a self-extracting zip; Python's zipfile can read it
    by seeking past the PE stub.

    Returns the path to the extracted DLL.
    Raises RuntimeError on any failure.
    """
    url, version = _fetch_latest_reshade_url()

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()

    want = f"reshade{arch}.dll"
    fallback = "reshade32.dll" if arch == 64 else "reshade64.dll"

    buf = io.BytesIO(data)
    try:
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            dll_name = next((n for n in names if n.lower() == want), None)
            if dll_name is None:
                dll_name = next((n for n in names if n.lower() == fallback), None)
            if dll_name is None:
                raise RuntimeError(
                    f"ReShade installer did not contain {want} or {fallback}. "
                    f"Found: {names}"
                )
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / dll_name
            with zf.open(dll_name) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ReShade installer is not a valid zip archive: {exc}") from exc

    return out_path


# Always downloaded — the official ReShade shader set.
_SHADER_BASE_URL = "https://github.com/crosire/reshade-shaders/archive/refs/heads/slim.zip"
_SHADER_BASE_SUBFOLDER = None  # has Shaders/ and Textures/ at root

# Optional shader packs shown as checkboxes in the wizard.
# Each entry: (label, url, subfolder)
#   subfolder=None  → extract Shaders/ and Textures/ from repo root
#   subfolder="xyz" → strip repo root then extract only the "xyz/" subtree
_OPTIONAL_SHADER_PACKS: list[tuple[str, str, "str | None"]] = [
    ("SweetFX",         "https://github.com/CeeJayDK/SweetFX/archive/refs/heads/master.zip",            None),
    ("qUINT",           "https://github.com/martymcmodding/qUINT/archive/refs/heads/master.zip",         "Shaders"),
    ("iMMERSE",         "https://github.com/martymcmodding/iMMERSE/archive/refs/heads/main.zip",         None),
    ("METEOR",          "https://github.com/martymcmodding/METEOR/archive/refs/heads/main.zip",          None),
    ("AstrayFX",        "https://github.com/BlueSkyDefender/AstrayFX/archive/refs/heads/master.zip",     None),
    ("Depth3D",         "https://github.com/BlueSkyDefender/Depth3D/archive/refs/heads/master.zip",      None),
    ("FXShaders",       "https://github.com/luluco250/FXShaders/archive/refs/heads/master.zip",          None),
    ("Pirate Shaders",  "https://github.com/Heathen/Pirate-Shaders/archive/refs/heads/master.zip",       "reshade-shaders"),
    ("OtisFX",          "https://github.com/FransBouma/OtisFX/archive/refs/heads/master.zip",            None),
]


def _extract_zip_into(data: bytes, dest: Path, subfolder: "str | None") -> None:
    """Extract a GitHub repo zip into *dest*.

    *subfolder* — if None, extract the repo root content directly into *dest*
    (preserving subdirectories).  If a string, only extract files that live
    inside that subfolder of the repo and place them into *dest/subfolder/*.
    """
    _KEEP = {"Shaders", "Textures"}

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            top = next(
                (n.split("/")[0] for n in zf.namelist() if "/" in n),
                None,
            )
            if top is None:
                raise RuntimeError("Unexpected zip layout (no top-level folder).")

            for member in zf.namelist():
                # Strip the repo top-level folder prefix
                if not member.startswith(top + "/"):
                    continue
                rel = member[len(top) + 1:]
                if not rel:
                    continue

                if subfolder:
                    # Only extract files inside the named subfolder
                    if not rel.startswith(subfolder + "/"):
                        continue
                    target = dest / rel
                else:
                    # Extract Shaders/ and Textures/ only, skip loose files/dirs
                    first_seg = rel.split("/")[0]
                    if first_seg not in _KEEP:
                        continue
                    target = dest / rel

                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Shader zip is not a valid archive: {exc}") from exc


def _download_and_extract_shaders(
    dest_dir: Path,
    optional_packs: "list[tuple[str, str, str | None]]",
) -> Path:
    """Download the base shader repo plus any selected optional packs and merge
    them all into *dest_dir*/reshade-shaders/.

    *optional_packs* is a subset of :data:`_OPTIONAL_SHADER_PACKS` — the
    entries the user ticked.  Downloads run in parallel.

    Returns the path to the ``reshade-shaders/`` folder.
    Raises RuntimeError on any failure.
    """
    out = dest_dir / "reshade-shaders"
    out.mkdir(parents=True, exist_ok=True)

    to_fetch = [(_SHADER_BASE_URL, _SHADER_BASE_SUBFOLDER)] + [
        (url, sub) for (_, url, sub) in optional_packs
    ]

    errors: list[str] = []
    results: list[tuple[bytes, "str | None"] | None] = [None] * len(to_fetch)

    def _fetch(idx: int, url: str, subfolder: "str | None") -> None:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                results[idx] = (resp.read(), subfolder)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    threads = [
        threading.Thread(target=_fetch, args=(i, url, sub), daemon=True)
        for i, (url, sub) in enumerate(to_fetch)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RuntimeError("Shader download failed:\n" + "\n".join(errors))

    for entry in results:
        if entry is not None:
            data, subfolder = entry
            _extract_zip_into(data, out, subfolder)

    return out


# ============================================================================
# Wizard
# ============================================================================

class ReShadeWizard(ctk.CTkFrame):
    """Three-step wizard: download ReShade, install d3dcompiler_47, deploy files."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        reshade_dll: str = "dxgi.dll",
        reshade_arch: int = 64,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda _: None)
        self._reshade_dll = reshade_dll          # e.g. "dxgi.dll"
        self._reshade_arch = reshade_arch        # 32 or 64

        # DLL stem used for the Wine override key, e.g. "dxgi"
        self._override_key = Path(reshade_dll).stem

        self._extracted_dll: Path | None = None      # path to ReShade DLL in download cache
        self._extracted_shaders: Path | None = None  # path to reshade-shaders/ in download cache

        # Optional shader pack checkboxes — populated in step 1
        self._shader_pack_vars: list[ctk.BooleanVar] = [
            ctk.BooleanVar(value=False) for _ in _OPTIONAL_SHADER_PACKS
        ]

        self._install_to_root_folder = ctk.BooleanVar(value=False)

        # --- Title bar ---
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Install ReShade \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_shaders()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_cancel(self):
        self._cleanup_tmp()
        self._on_close_cb()

    def _cleanup_tmp(self):
        pass  # downloads are kept in config download_cache for reuse

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Shader pack selection
    # ------------------------------------------------------------------

    def _show_step_shaders(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Select Shader Packs",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            self._body,
            text="The official ReShade shaders are always included.\nSelect any additional packs to download:",
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, pady=(0, 12))

        for i, (label, _url, _sub) in enumerate(_OPTIONAL_SHADER_PACKS):
            ctk.CTkCheckBox(
                scroll, text=label,
                variable=self._shader_pack_vars[i],
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, checkmark_color="white",
            ).pack(anchor="w", padx=12, pady=4)

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_download,
        ).pack(side="bottom")

    # ------------------------------------------------------------------
    # Step 2 — Download ReShade
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Download ReShade",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body,
            text="Fetching latest ReShade version\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._dl_status.pack(pady=(0, 16))

        self._progress = ctk.CTkProgressBar(self._body, mode="indeterminate", width=340)
        self._progress.pack(pady=(0, 16))
        self._progress.start()

        self._dl_next_btn = ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_d3dcompiler, state="disabled",
        )
        self._dl_next_btn.pack(side="bottom")

        threading.Thread(target=self._do_download, daemon=True).start()

    def _do_download(self):
        try:
            self._set_dl_status("Downloading ReShade and shaders\u2026")
            from Utils.config_paths import get_config_dir
            tmp_path = get_config_dir() / "download_cache" / "reshade"
            tmp_path.mkdir(parents=True, exist_ok=True)

            dll_exc: list[Exception] = []
            shaders_exc: list[Exception] = []
            dll_result: list[Path] = []
            shaders_result: list[Path] = []

            arch = self._reshade_arch

            def _get_dll():
                try:
                    dll_result.append(_download_and_extract_reshade_dll(tmp_path, arch))
                except Exception as e:
                    dll_exc.append(e)

            selected_packs = [
                pack for pack, var in zip(_OPTIONAL_SHADER_PACKS, self._shader_pack_vars)
                if var.get()
            ]

            def _get_shaders():
                try:
                    shaders_result.append(_download_and_extract_shaders(tmp_path, selected_packs))
                except Exception as e:
                    shaders_exc.append(e)

            t1 = threading.Thread(target=_get_dll, daemon=True)
            t2 = threading.Thread(target=_get_shaders, daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()

            if dll_exc:
                raise RuntimeError(f"ReShade DLL: {dll_exc[0]}")
            if shaders_exc:
                raise RuntimeError(f"Shaders: {shaders_exc[0]}")

            self._extracted_dll = dll_result[0]
            self._extracted_shaders = shaders_result[0]
            self._log(f"ReShade wizard: downloaded {self._extracted_dll.name} and shaders.")
            self._set_dl_status("Downloaded ReShade and shaders successfully.", color=TEXT_OK)
            self.after(0, lambda: [
                self._progress.stop(),
                self._progress.pack_forget(),
                self._dl_next_btn.configure(state="normal"),
            ])
        except Exception as exc:
            self._log(f"ReShade wizard: download failed: {exc}")
            self._set_dl_status(f"Download failed:\n{exc}\n\nCheck your internet connection and try again.", color=TEXT_ERR)
            self.after(0, lambda: [
                self._progress.stop(),
                self._progress.pack_forget(),
                self._dl_next_btn.configure(state="normal", text="Retry \u21ba",
                                             command=self._show_step_download),
            ])

    def _set_dl_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._dl_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 2 — Install d3dcompiler_47
    # ------------------------------------------------------------------

    def _show_step_d3dcompiler(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Install d3dcompiler_47",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        steam_id = str(getattr(self._game, "steam_id", "") or "")
        has_steam_id = bool(steam_id)
        has_protontricks = protontricks_available()

        if not has_steam_id:
            info = (
                "This game has no Steam ID configured — d3dcompiler_47 cannot be\n"
                "installed automatically. Install it manually via protontricks\n"
                "or winecfg before running the game with ReShade."
            )
            color = TEXT_WARN
        elif not has_protontricks:
            info = (
                "protontricks is not installed.\n\n"
                "Install it via the Discover store or Flathub, then re-open\n"
                "this wizard — or install d3dcompiler_47 manually via winecfg."
            )
            color = TEXT_WARN
        else:
            info = (
                f"protontricks will install d3dcompiler_47 into the Proton\n"
                f"prefix for this game (App ID: {steam_id}).\n\n"
                "This may take up to a minute."
            )
            color = TEXT_DIM

        ctk.CTkLabel(
            self._body, text=info,
            font=FONT_NORMAL, text_color=color, justify="center", wraplength=460,
        ).pack(pady=(0, 16))

        self._d3d_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=460,
        )
        self._d3d_status.pack(pady=(0, 8))

        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))

        skip_btn = ctk.CTkButton(
            btn_row, text="Skip", width=100, height=36,
            font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._show_step_install,
        )
        skip_btn.pack(side="left", padx=(0, 8))

        self._d3d_install_btn = ctk.CTkButton(
            btn_row, text="Install d3dcompiler_47", width=200, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._do_install_d3dcompiler,
            state="normal" if (has_steam_id and has_protontricks) else "disabled",
        )
        self._d3d_install_btn.pack(side="left")

    def _do_install_d3dcompiler(self):
        self._d3d_install_btn.configure(state="disabled", text="Installing\u2026")
        steam_id = str(getattr(self._game, "steam_id", "") or "")

        prefix = getattr(self._game, "_prefix_path", None)

        def _run():
            ok = install_d3dcompiler_47(
                steam_id,
                log_fn=lambda msg: self._set_d3d_status(msg),
                prefix_path=prefix,
            )
            color = TEXT_OK if ok else TEXT_ERR
            self._set_d3d_status(
                "d3dcompiler_47 installed successfully.\nClick Next to continue." if ok
                else "Install failed — you can Skip and install it manually.",
                color=color,
            )
            self.after(0, lambda: self._d3d_install_btn.configure(
                state="normal",
                text="Next \u2192" if ok else "Retry",
                fg_color=("#2d7a2d" if ok else ACCENT),
                hover_color=("#3a9e3a" if ok else ACCENT_HOV),
                command=self._show_step_install if ok else self._do_install_d3dcompiler,
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _set_d3d_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._d3d_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 3 — Install files
    # ------------------------------------------------------------------

    def _show_step_install(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Install ReShade",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        info = (
            f"ReShade will be installed as  {self._reshade_dll}\n"
            f"and the Wine DLL override  {self._override_key}=native,builtin\n"
            f"will be written to the Proton prefix."
        )
        ctk.CTkLabel(
            self._body, text=info,
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 12))

        ctk.CTkCheckBox(
            self._body,
            text="Install to Root_Folder (staging) instead of game folder",
            variable=self._install_to_root_folder,
            font=FONT_SMALL, text_color=TEXT_DIM,
            fg_color=ACCENT, hover_color=ACCENT_HOV, checkmark_color="white",
        ).pack(pady=(0, 16))

        self._install_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=460,
        )
        self._install_status.pack(pady=(0, 8))

        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))

        self._done_btn = ctk.CTkButton(
            btn_row, text="Done", width=100, height=36,
            font=FONT_BOLD, fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._finish, state="disabled",
        )
        self._done_btn.pack(side="right", padx=(8, 0))

        self._do_install_btn = ctk.CTkButton(
            btn_row, text="Install", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._do_install,
        )
        self._do_install_btn.pack(side="right")

    def _do_install(self):
        self._do_install_btn.configure(state="disabled", text="Installing\u2026")

        def _run():
            try:
                use_root_folder = self._install_to_root_folder.get()

                # Derive exe subdir from the game handler (e.g. "bin/x64" for Cyberpunk)
                game_exe = getattr(self._game, "exe_name", "") or ""
                exe_subdir = Path(game_exe).parent if game_exe else Path(".")
                if exe_subdir == Path("."):
                    exe_subdir = None

                if use_root_folder:
                    base_dir = self._game.get_effective_root_folder_path()
                    base_dir.mkdir(parents=True, exist_ok=True)
                    dest_label = "Root_Folder (staging)"
                else:
                    base_dir = self._game.get_game_path()
                    if base_dir is None:
                        raise RuntimeError("Game path is not configured.")
                    dest_label = "game folder"

                dest_dir = base_dir / exe_subdir if exe_subdir else base_dir
                dest_dir.mkdir(parents=True, exist_ok=True)

                dll_src = self._extracted_dll
                if dll_src is None or not dll_src.is_file():
                    raise RuntimeError("ReShade DLL not found — please restart the wizard.")

                # 1. Copy the ReShade DLL renamed to the game's override name
                shutil.copy2(str(dll_src), str(dest_dir / self._reshade_dll))
                self._log(f"ReShade wizard: copied {dll_src.name} → {self._reshade_dll}")

                # 2. Copy bundled ReShade.ini and create blank ReShadePreset.ini
                src_ini = Path(__file__).parent / "ReShade.ini"
                if src_ini.is_file():
                    shutil.copy2(str(src_ini), str(dest_dir / "ReShade.ini"))
                    self._log("ReShade wizard: copied ReShade.ini")
                (dest_dir / "ReShadePreset.ini").touch()
                self._log("ReShade wizard: created ReShadePreset.ini")

                # 3. Copy reshade-shaders/ directly into dest_dir
                shaders_src = self._extracted_shaders
                if shaders_src is None or not shaders_src.is_dir():
                    raise RuntimeError("Shader files not found — please restart the wizard.")
                shaders_dest = dest_dir / "reshade-shaders"
                if shaders_dest.exists():
                    shutil.rmtree(str(shaders_dest))
                shutil.copytree(str(shaders_src), str(shaders_dest))
                self._log("ReShade wizard: copied reshade-shaders/")

                # 4. Apply Wine DLL override to the Proton prefix
                prefix = getattr(self._game, "_prefix_path", None)
                if prefix and Path(prefix).is_dir():
                    apply_wine_dll_overrides(
                        Path(prefix),
                        {self._override_key: "native,builtin"},
                        log_fn=self._log,
                    )
                    self._log(f"ReShade wizard: applied Wine override {self._override_key}=native,builtin")
                    override_note = f"\u2713 Wine override {self._override_key}=native,builtin applied."
                else:
                    override_note = (
                        f"\u26a0 Could not apply Wine override automatically.\n"
                        f"Add to Steam launch options:\n"
                        f'WINEDLLOVERRIDES="{self._override_key}=native,builtin" %command%'
                    )

                self._set_install_status(
                    f"\u2713 ReShade installed to {dest_label}.\n"
                    f"{override_note}\n\n"
                    "Click Done to close.",
                    color=TEXT_OK,
                )
                self._log("ReShade wizard: installation complete.")
                self._cleanup_tmp()
                self.after(0, lambda: self._done_btn.configure(state="normal"))

            except Exception as exc:
                self._log(f"ReShade wizard error: {exc}")
                self._set_install_status(f"Error: {exc}", color=TEXT_ERR)
                self.after(0, lambda: self._do_install_btn.configure(
                    state="normal", text="Retry",
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _set_install_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._install_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def _finish(self):
        self._cleanup_tmp()
        self._log("ReShade wizard: closed.")
        self._on_close_cb()
