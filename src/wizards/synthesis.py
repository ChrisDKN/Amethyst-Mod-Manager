"""
wizards/synthesis.py
Wizard for running Mutagen's Synthesis patcher in a dedicated Wine prefix.

Synthesis needs .NET 9 SDK + .NET 10 Desktop Runtime + registry tweaks to
work under Wine. Rather than pollute the game's real Proton prefix with
those heavy changes, we give Synthesis its own prefix next to its exe and
point it at the real Skyrim SE install via a registry entry so it can
discover the game.

Flow
----
1. Download the latest Synthesis release from GitHub (zip) and extract it.
2. Let the user pick a Proton version.
3. Run setup_synthesis_prefix() in a worker thread (per-step markers skip
   already-applied work on subsequent runs).
4. Symlink the active profile's plugins.txt into the prefix AppData dir
   where Bethesda games expect it, then launch Synthesis.exe via the
   Proton-bundled `wine` binary (NOT `proton run`, which targets Steam
   compatdata prefixes).
"""

from __future__ import annotations

import configparser
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.config_paths import get_game_config_dir
from Utils.steam_finder import list_installed_proton
from Utils.synthesis_setup import setup_synthesis_prefix
from wizards.script_extender import _extract_archive, _fetch_latest_github_asset

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)


_APP_DIR_NAME = "Synthesis"
_EXE_NAME = "Synthesis.exe"
_GITHUB_API = "https://api.github.com/repos/Mutagen-Modding/Synthesis/releases/latest"
_INI_SECTION = "synthesis"
_INI_PROTON_KEY = "proton"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _synthesis_dir(game: "BaseGame") -> Path:
    """Return Profiles/<game>/Applications/Synthesis/ (holds Synthesis.exe)."""
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR_NAME


def _synthesis_prefix_parent(game: "BaseGame") -> Path:
    """Return the compatdata-style parent dir that contains ``pfx/``.

    Keeping the prefix in its own subfolder (``<synthesis_dir>/prefix/pfx``)
    means the entire Wine environment can be deleted without touching
    Synthesis.exe / its config, and matches Proton's expected
    ``STEAM_COMPAT_DATA_PATH`` layout (``<path>/pfx``).
    """
    return _synthesis_dir(game) / "prefix"


def _synthesis_pfx(game: "BaseGame") -> Path:
    return _synthesis_prefix_parent(game) / "pfx"


def _synthesis_exe(game: "BaseGame") -> Path:
    return _synthesis_dir(game) / _EXE_NAME


def _settings_path(game: "BaseGame") -> Path:
    return get_game_config_dir(game.name) / "synthesis.ini"


def _load_saved_proton(game: "BaseGame") -> str:
    ini = _settings_path(game)
    if not ini.is_file():
        return ""
    parser = configparser.ConfigParser()
    try:
        parser.read(ini)
    except configparser.Error:
        return ""
    return parser.get(_INI_SECTION, _INI_PROTON_KEY, fallback="")


def _save_proton(game: "BaseGame", proton_name: str) -> None:
    ini = _settings_path(game)
    parser = configparser.ConfigParser()
    if ini.is_file():
        try:
            parser.read(ini)
        except configparser.Error:
            parser = configparser.ConfigParser()
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_PROTON_KEY] = proton_name
    with ini.open("w") as f:
        parser.write(f)


def _plugins_appdata_targets(game: "BaseGame", pfx: Path) -> list[Path]:
    """Return all AppData plugins.txt paths Synthesis might read.

    Bethesda games expose both the Steam-variant AppData dir and (when
    defined) a GOG-variant dir. We link plugins.txt into both so Synthesis
    finds the current load order regardless of which build the user has
    installed.
    """
    targets: list[Path] = []
    for attr in ("_APPDATA_SUBPATH", "_APPDATA_SUBPATH_GOG"):
        subpath = getattr(game, attr, None)
        if subpath is not None:
            targets.append(pfx / subpath / "plugins.txt")
    return targets


def _active_profile_plugins_source(game: "BaseGame", profile: str) -> Path:
    return game.get_profile_root() / "profiles" / profile / "plugins.txt"


# ============================================================================
# Wizard dialog
# ============================================================================

class SynthesisWizard(ctk.CTkFrame):
    """Multi-step wizard: download → proton → setup prefix → launch."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda msg: None)

        self._proton_candidates: list[Path] = []
        self._selected_proton: Path | None = None
        self._plugins_symlinks: list[Path] = []

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Run Synthesis \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        # If Synthesis.exe already exists, skip straight to proton selection.
        if _synthesis_exe(self._game).is_file():
            self._show_step_proton()
        else:
            self._show_step_download()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply():
            widget = getattr(self, attr, None)
            if widget is not None and widget.winfo_exists():
                widget.configure(text=text, text_color=color)
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _append_log(self, msg: str):
        box = getattr(self, "_log_box", None)
        if box is None:
            self._log(msg)
            return

        def _apply():
            try:
                box.configure(state="normal")
                box.insert("end", msg + "\n")
                box.see("end")
                box.configure(state="disabled")
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass
        self._log(msg)

    # ------------------------------------------------------------------
    # Step 1 — download
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download Synthesis",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Fetching latest release from GitHub \u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._dl_status.pack(pady=(0, 16))

        self._dl_progress = ctk.CTkProgressBar(self._body, width=400, mode="indeterminate")
        self._dl_progress.pack(pady=(0, 16))
        self._dl_progress.start()

        threading.Thread(target=self._do_download, daemon=True).start()

    def _do_download(self):
        try:
            self._set_label("_dl_status", "Fetching latest release from GitHub \u2026")
            tag, url = _fetch_latest_github_asset(_GITHUB_API, ["synthesis"])
            self._set_label("_dl_status", f"Downloading Synthesis {tag} \u2026")
            self._log(f"Synthesis: downloading {url}")

            import tempfile
            tmpdir = Path(tempfile.mkdtemp(prefix="synthesis_dl_"))
            archive = tmpdir / url.split("/")[-1]

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(block_num * block_size / total_size, 1.0)
                    try:
                        self.after(0, lambda p=pct: (
                            self._dl_progress.configure(mode="determinate"),
                            self._dl_progress.set(p),
                        ))
                    except Exception:
                        pass

            urllib.request.urlretrieve(url, archive, reporthook=_reporthook)

            dest = _synthesis_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)
            self._set_label("_dl_status", f"Extracting Synthesis {tag} \u2026")
            _extract_archive(archive, dest)

            try:
                archive.unlink()
                archive.parent.rmdir()
            except OSError:
                pass

            if not _synthesis_exe(self._game).is_file():
                raise RuntimeError(
                    f"{_EXE_NAME} not found after extraction — "
                    "the release asset layout may have changed."
                )

            self._set_label("_dl_status", f"Installed Synthesis {tag}.", color="#6bc76b")
            self.after(0, lambda: self._dl_progress.stop())
            self.after(500, self._show_step_proton)

        except Exception as exc:
            self._log(f"Synthesis: download error: {exc}")
            self._set_label("_dl_status", f"Download failed: {exc}", color="#e06c6c")
            try:
                self.after(0, lambda: self._dl_progress.stop())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Step 2 — Proton selection
    # ------------------------------------------------------------------

    def _show_step_proton(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Select Proton Version",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))
        ctk.CTkLabel(
            self._body,
            text=(
                "Synthesis will run in its own Wine prefix next to Synthesis.exe.\n"
                "Pick a Proton version to create that prefix with."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 12))

        self._proton_candidates = list_installed_proton()
        if not self._proton_candidates:
            ctk.CTkLabel(
                self._body,
                text="No Proton installations found.\n"
                     "Install Proton (e.g. GE-Proton) via Steam and try again.",
                font=FONT_NORMAL, text_color="#e06c6c", justify="center",
            ).pack(pady=16)
            return

        saved = _load_saved_proton(self._game)
        preselect = self._proton_candidates[0]
        for p in self._proton_candidates:
            if p.parent.name == saved:
                preselect = p
                break

        scroll = ctk.CTkScrollableFrame(self._body, fg_color="transparent", height=240)
        scroll.pack(fill="x", pady=(0, 12))

        self._proton_var = ctk.StringVar(value=str(preselect))
        for script in self._proton_candidates:
            row = ctk.CTkFrame(scroll, fg_color=BG_PANEL, corner_radius=6)
            row.pack(fill="x", pady=4)
            ctk.CTkRadioButton(
                row, text=script.parent.name, variable=self._proton_var,
                value=str(script),
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).pack(side="left", padx=12, pady=10)

        btn = ctk.CTkButton(
            self._body, text="Continue \u2192", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_proton_chosen,
        )
        btn.pack(side="bottom", pady=(8, 0))

    def _on_proton_chosen(self):
        choice = self._proton_var.get()
        if not choice:
            return
        self._selected_proton = Path(choice)
        _save_proton(self._game, self._selected_proton.parent.name)
        self._show_step_setup()

    # ------------------------------------------------------------------
    # Step 3 — prefix setup
    # ------------------------------------------------------------------

    def _show_step_setup(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Prepare Prefix",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        self._setup_status = ctk.CTkLabel(
            self._body, text="Preparing \u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        )
        self._setup_status.pack(pady=(0, 8))

        self._log_box = ctk.CTkTextbox(
            self._body, width=540, height=220, font=FONT_SMALL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER, border_width=1,
        )
        self._log_box.pack(pady=(0, 12))
        self._log_box.configure(state="disabled")

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._launch_btn = ctk.CTkButton(
            btn_frame, text="Launch Synthesis", width=180, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_launch, state="disabled",
        )
        self._launch_btn.pack(side="right", padx=(8, 0))

        threading.Thread(target=self._do_setup, daemon=True).start()

    def _do_setup(self):
        game_path = self._game.get_game_path()
        if game_path is None:
            self._append_log("Game path is not configured; aborting.")
            self._set_label("_setup_status", "Game path not configured.", color="#e06c6c")
            return
        if self._selected_proton is None:
            self._append_log("No Proton selected; aborting.")
            return

        synthesis_dir = _synthesis_dir(self._game)
        self._append_log(f"Synthesis dir: {synthesis_dir}")
        self._append_log(f"Proton: {self._selected_proton.parent.name}")
        self._append_log(f"Game path: {game_path}")
        self._append_log("")

        try:
            ok = setup_synthesis_prefix(
                synthesis_dir=synthesis_dir,
                proton_script=self._selected_proton,
                game_path=Path(game_path),
                log_fn=self._append_log,
                prefix_parent=_synthesis_prefix_parent(self._game),
                registry_game_name=getattr(
                    self._game, "synthesis_registry_name", "Skyrim Special Edition",
                ),
            )
        except Exception as exc:
            self._append_log(f"Prefix setup raised: {exc}")
            ok = False

        if ok:
            self._set_label("_setup_status", "Prefix ready. Click Launch Synthesis.", color="#6bc76b")
        else:
            self._set_label(
                "_setup_status",
                "Setup completed with errors — launch may still work.",
                color="#e0a06c",
            )
        try:
            self.after(0, lambda: self._launch_btn.configure(state="normal"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 4 — launch
    # ------------------------------------------------------------------

    def _current_profile(self) -> str:
        """Return the profile the user has selected in the topbar, else the last-active one."""
        try:
            return self.winfo_toplevel()._topbar._profile_var.get() or "default"
        except Exception:
            return self._game.get_last_active_profile()

    def _symlink_plugins(self) -> None:
        """Symlink the active profile's plugins.txt into every AppData variant.

        Links both Steam and GOG AppData dirs so Synthesis reads the correct
        load order regardless of which distribution the game came from.
        """
        pfx = _synthesis_pfx(self._game)
        targets = _plugins_appdata_targets(self._game, pfx)
        if not targets:
            self._append_log("Skipping plugins.txt link (game has no AppData subpath).")
            return
        profile = self._current_profile()
        source = _active_profile_plugins_source(self._game, profile)
        if not source.is_file():
            self._append_log(f"plugins.txt source not found: {source}")
            return
        self._append_log(f"Using profile: {profile}")
        self._plugins_symlinks = []
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(source)
                self._plugins_symlinks.append(target)
                self._append_log(f"Linked plugins.txt \u2192 {target}")
            except OSError as exc:
                self._append_log(f"Failed to link plugins.txt at {target}: {exc}")

    def _remove_plugins_symlink(self) -> None:
        for link in getattr(self, "_plugins_symlinks", []):
            try:
                if link.is_symlink():
                    link.unlink()
                    self._log(f"Synthesis: removed plugins.txt symlink {link}")
            except OSError:
                pass
        self._plugins_symlinks = []

    def _on_launch(self):
        if self._selected_proton is None:
            return
        self._launch_btn.configure(state="disabled", text="Running \u2026")
        self._symlink_plugins()
        threading.Thread(target=self._do_launch, daemon=True).start()

    def _deploy_active_profile(self) -> bool:
        """Run restore + filemap rebuild + deploy for the active profile.

        Mirrors the Run-EXE deploy flow in plugin_panel so the game's Data
        folder reflects the current modlist before Synthesis scans it.
        Returns True if deploy completed without an exception.
        """
        from Utils.filemap import build_filemap
        from Utils.deploy import (
            LinkMode, deploy_root_folder, restore_root_folder,
            load_per_mod_strip_prefixes,
        )
        from Utils.profile_state import read_excluded_mod_files
        from Utils.wine_dll_config import deploy_game_wine_dll_overrides

        game = self._game
        profile = self._current_profile()
        game_root = game.get_game_path()
        self._append_log(f"Deploying profile '{profile}' before launch \u2026")

        try:
            # Activate the last-deployed profile for restore so rescued
            # runtime files land in *that* profile's overwrite/ (critical
            # for profile_specific_mods profiles). Then switch to the
            # target profile for deploy.
            profile_root = game.get_profile_root()
            last_deployed = game.get_last_deployed_profile()
            if last_deployed:
                game.set_active_profile_dir(
                    profile_root / "profiles" / last_deployed
                )

            if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                try:
                    game.restore(log_fn=self._append_log)
                except RuntimeError:
                    pass

            restore_rf_dir = game.get_effective_root_folder_path()
            if restore_rf_dir.is_dir() and game_root:
                restore_root_folder(restore_rf_dir, game_root, log_fn=self._append_log)

            game.set_active_profile_dir(
                profile_root / "profiles" / profile
            )

            staging = game.get_effective_mod_staging_path()
            modlist_path = profile_root / "profiles" / profile / "modlist.txt"
            filemap_out = game.get_effective_filemap_path()
            if modlist_path.is_file():
                try:
                    _exc_raw = read_excluded_mod_files(modlist_path.parent, None)
                    _exc = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
                    build_filemap(
                        modlist_path, staging, filemap_out,
                        strip_prefixes=game.mod_folder_strip_prefixes or None,
                        per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                        allowed_extensions=game.mod_install_extensions or None,
                        root_deploy_folders=game.mod_root_deploy_folders or None,
                        excluded_mod_files=_exc,
                        conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                        exclude_dirs=getattr(game, "filemap_exclude_dirs", None) or None,
                    )
                except Exception as fm_err:
                    self._append_log(f"Filemap rebuild warning: {fm_err}")

            deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
            game.deploy(log_fn=self._append_log, profile=profile, mode=deploy_mode)
            game.save_last_deployed_profile(profile)

            _pfx = game.get_prefix_path()
            if _pfx and _pfx.is_dir():
                deploy_game_wine_dll_overrides(
                    game.name, _pfx, game.wine_dll_overrides, log_fn=self._append_log,
                )

            target_rf_dir = game.get_effective_root_folder_path()
            rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
            if rf_allowed and target_rf_dir.is_dir() and game_root:
                deploy_root_folder(target_rf_dir, game_root, mode=deploy_mode, log_fn=self._append_log)

            if hasattr(game, "swap_launcher"):
                game.swap_launcher(self._append_log)

            self._append_log("Deploy complete.")
            return True
        except Exception as exc:
            self._append_log(f"Deploy error: {exc}")
            return False

    def _do_launch(self):
        synthesis_dir = _synthesis_dir(self._game)
        exe = synthesis_dir / _EXE_NAME
        proton_script = self._selected_proton  # .../<ProtonVer>/proton
        if not exe.is_file():
            self._append_log(f"Synthesis.exe missing at {exe}")
            return
        if not proton_script.is_file():
            self._append_log(f"Proton script missing at {proton_script}")
            return

        self._deploy_active_profile()

        # Run via `proton run`. This gives us the Steam Linux Runtime sniper
        # container, which ships libicuuc/libicuin (needed by Wine's icu.dll
        # stub) and everything else Wine expects. Without it, .NET 9 WPF
        # crashes with ``Cannot get symbol u_charsToUChars from libicuuc``.
        # STEAM_COMPAT_DATA_PATH is Proton's "compatdata" dir — it expects
        # <path>/pfx/ underneath, so we point it at our prefix parent.
        import os
        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(_synthesis_prefix_parent(self._game))
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(Path.home() / ".local" / "share" / "Steam")
        # Bind-mount the synthesis working dir into the sniper container so
        # `dotnet build` under Flatpak can write obj/bin/ and read Synthesis'
        # Data/ cache. Without this, Flatpak's sandbox + Proton's sniper
        # combine to hide parts of the tree inside the container and MSBuild
        # exits 1 with truncated output ("SynthesisBuildFailure: Unknown (1)").
        env["STEAM_COMPAT_INSTALL_PATH"] = str(synthesis_dir)
        env["WINEDEBUG"] = "-all"
        env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
        # NuGet: skip online CRL/OCSP checks (Wine's WinHTTP can't reach them
        # reliably, which amplifies the signature-expiry failures Mutagen's
        # 2020-era deps already trigger).
        env["NUGET_CERT_REVOCATION_MODE"] = "offline"

        self._append_log(f"Launching {exe} via {proton_script.parent.name} \u2026")
        try:
            log_path = synthesis_dir / "synthesis.log"
            with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
                proc = subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe)],
                    env=env,
                    cwd=str(synthesis_dir),
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                )
                proc.wait()
            self._append_log(f"Synthesis closed. Output log: {log_path}")
        except Exception as exc:
            self._append_log(f"Launch error: {exc}")
        finally:
            self._remove_plugins_symlink()
            try:
                self.after(0, lambda: self._launch_btn.configure(
                    state="normal", text="Launch Synthesis",
                ))
            except Exception:
                pass
