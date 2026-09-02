"""Qt view: Install Special K as a managed mod (NieR: Automata).

Downloads the latest SpecialK.7z from GitHub, extracts it, and builds a
root-flagged mod whose only payload is ``SpecialK64.dll`` renamed to the
selected proxy DLL name (default ``dxgi.dll``).

Special K ships as bare 32/64-bit DLLs with no installer: local injection works
by dropping the DLL next to the game exe under the name of a system API the
game already imports, so the game loads Special K first and it chains on to the
real system DLL.  NieR: Automata is a 64-bit D3D11 title, so ``SpecialK64.dll``
is the correct half of the archive and ``dxgi.dll`` the usual proxy name.

The payload is assembled by hand rather than through
``wizard_archives.install_archive_payload`` because that extracts an archive
verbatim - here only one of the two DLLs is wanted, under a different name.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GITHUB_API = "https://api.github.com/repos/SpecialKO/SpecialK/releases/latest"
_ARCHIVE_KEYWORDS = ["specialk"]
_SOURCE_DLL = "SpecialK64.dll"
_MOD_NAME = "Special K"

# Proxy names Special K supports for local injection. dxgi is the right default
# for a D3D11/D3D12 title; the others are here for games that don't import dxgi
# directly (older D3D9 titles, or where another mod already owns dxgi.dll).
_PROXY_NAMES = ["dxgi.dll", "d3d11.dll", "d3d9.dll", "dinput8.dll", "winmm.dll"]

_PG_DOWNLOAD, _PG_INSTALL = range(2)


class SpecialKView(WizardViewBase):
    """Download Special K and install it as a root-flagged managed mod."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)          # 0-100, or -1 for indeterminate
    _dl_next_sig = Signal()
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install Special K - {0}").format(game.name))

        self._installed_mode = ""
        self._proxy_name = _PROXY_NAMES[0]

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(self._guard(self._on_dl_progress))
        self._dl_next_sig.connect(self._guard(
            lambda: self._dl_next_btn.setEnabled(True)))
        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))

        self._stack.addWidget(self._build_download_page())
        self._stack.addWidget(self._build_install_page())
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

        threading.Thread(target=self._do_fetch_and_download, daemon=True,
                         name="specialk-download").start()

    # ---- options box -------------------------------------------------------------
    def _build_options_box(self) -> QWidget:
        p = active_palette()
        box = QFrame()
        box.setStyleSheet(
            f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        bv = QVBoxLayout(box)
        bv.setContentsMargins(12, 10, 12, 10)
        bv.setSpacing(4)

        head = QLabel(self.tr("Proxy DLL name"))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        bv.addWidget(head)
        self._proxy_combo = QComboBox()
        self._proxy_combo.addItems(_PROXY_NAMES)
        bv.addWidget(self._proxy_combo)
        note = QLabel(self.tr(
            "{0} is renamed to this. dxgi.dll is correct for NieR: Automata; "
            "pick another only if a different mod already uses that name.")
            .format(_SOURCE_DLL))
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        bv.addWidget(note)

        dest = QLabel(self.tr("Install destination"))
        dest.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        bv.addWidget(dest)
        self._mode_group = QButtonGroup(self)
        for val, label in (
                ("mod", self.tr("As a managed mod (root-flagged)")),
                ("root", self.tr("Root_Folder (staging)")),
                ("game", self.tr("Game folder (restores to vanilla first)"))):
            rb = QRadioButton(label)
            rb.setProperty("mode", val)
            if val == "mod":
                rb.setChecked(True)
            self._mode_group.addButton(rb)
            bv.addWidget(rb)
        return box

    def _selected_mode(self) -> str:
        btn = self._mode_group.checkedButton()
        return btn.property("mode") if btn is not None else "mod"

    # ---- page 1: download --------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download Special K"))
        self._dl_status = self._make_status(lay)
        self._set_status(self._dl_status,
                         self.tr("Checking for the latest Special K release…"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)     # indeterminate until first progress
        lay.addWidget(self._dl_bar)
        lay.addWidget(self._build_options_box())
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_archive_dialog)
        rh.addWidget(browse)
        self._dl_next_btn = self._accent_btn(self.tr("Next →"))
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.clicked.connect(self._goto_install)
        rh.addWidget(self._dl_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _on_dl_progress(self, pct: int):
        if pct < 0:
            self._dl_bar.setRange(0, 0)
            return
        if self._dl_bar.maximum() == 0:
            self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)

    def _do_fetch_and_download(self):
        from Utils.ca_bundle import download_file
        from Utils.wizards.archives import (
            fetch_latest_github_asset, get_downloads_dir,
        )
        try:
            safe_emit(self._dl_status_sig,
                      self.tr("Fetching latest Special K release from GitHub…"), "")
            tag, url = fetch_latest_github_asset(_GITHUB_API, _ARCHIVE_KEYWORDS)
            filename = url.split("/")[-1]
            dest = get_downloads_dir() / filename
            safe_emit(self._dl_status_sig,
                      self.tr("Downloading Special K {0}…").format(tag), "")
            self._log(f"Special K Wizard: downloading {url} → {dest}")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100, block_num * block_size * 100 / total_size)
                    safe_emit(self._dl_progress_sig, int(pct))

            download_file(url, dest, reporthook=hook)
            safe_emit(self._dl_progress_sig, 100)
            self._archive_path = dest
            self._log(f"Special K Wizard: downloaded {filename}")
            safe_emit(self._dl_status_sig,
                      self.tr("Downloaded Special K {0}: {1}\n"
                      "Check the options below, then click Next.")
                      .format(tag, filename), GREEN)
            safe_emit(self._dl_next_sig)
        except Exception as exc:
            self._log(f"Special K Wizard: download error: {exc}")
            safe_emit(self._dl_progress_sig, -1)
            safe_emit(self._dl_status_sig,
                      self.tr("Download failed: {0}\n\n"
                      "Use Browse to select a manually downloaded archive.")
                      .format(exc), RED)
            safe_emit(self._dl_next_sig)

    def _browse_archive_dialog(self):
        from Utils.ui.portal import pick_file
        pick_file(self.tr("Select the Special K archive"),
                  lambda p: safe_emit(self._picked_sig, p))

    def _on_picked(self, path):
        """Base portal-picker override - this view has no locate page, so the
        pick lands here (the base connection stays guarded)."""
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._dl_status,
                             self.tr("Selected: {0}\n"
                             "Check the options below, then click Next.")
                             .format(Path(path).name), GREEN)
            self._dl_next_btn.setEnabled(True)

    # ---- page 2: install ---------------------------------------------------------
    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Install Special K"))
        self._run_status = self._make_status(lay)
        self._install_bar = QProgressBar()
        self._install_bar.setRange(0, 0)
        self._install_bar.setTextVisible(False)
        lay.addWidget(self._install_bar)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_install(self):
        if self._archive_path is None or not Path(self._archive_path).is_file():
            return
        self._installed_mode = self._selected_mode()
        self._proxy_name = self._proxy_combo.currentText()
        self._stack.setCurrentIndex(_PG_INSTALL)
        self._install_bar.setVisible(True)
        if self._installed_mode == "game":
            # Writing into the live game folder must happen on vanilla files,
            # so revert through the app's restore machinery (deploy mutex +
            # progress popup) and abort when it can't run - a swap that
            # post-dates the snapshot gets swept into overwrite/ on the next
            # restore.
            self._run_ctx_restore(self._run_status,
                                  on_ok=self._start_install,
                                  on_fail=self._on_restore_failed)
            return
        self._start_install()

    def _start_install(self):
        self._set_status(self._run_status, self.tr("Installing Special K…"))
        threading.Thread(target=self._do_install, daemon=True,
                         name="specialk-install").start()

    def _on_restore_failed(self):
        self._install_bar.setVisible(False)
        self._done_btn.setEnabled(True)

    def _extract_source_dll(self) -> "tuple[Path, Path]":
        """Extract the archive to a temp dir and locate SpecialK64.dll inside it.

        Returns (dll_path, temp_root). The caller must remove *temp_root* - not
        the DLL's own parent, which is only the same directory while the
        release stays a flat pair of DLLs.
        """
        from Utils.wizards.archives import extract_to_dir

        tmp = Path(tempfile.mkdtemp(prefix="specialk-"))
        try:
            extract_to_dir(self._archive_path, tmp)
            # The release is a flat pair of DLLs, but match case-insensitively
            # and recursively so a re-packed or wrapper-foldered archive works.
            for cand in tmp.rglob("*"):
                if cand.is_file() and cand.name.lower() == _SOURCE_DLL.lower():
                    return cand, tmp
            raise RuntimeError(
                f"{_SOURCE_DLL} was not found inside {self._archive_path.name}.")
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _do_install(self):
        mode = self._installed_mode
        proxy = self._proxy_name
        tmp_root: "Path | None" = None
        try:
            safe_emit(self._install_status_sig,
                      self.tr("Extracting {0}…").format(_SOURCE_DLL), "")
            src, tmp_root = self._extract_source_dll()

            dest_dir, dest_label, mod_name = self._resolve_destination(mode)
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / proxy
            shutil.copy2(src, target)
            self._log(f"Special K Wizard: installed {_SOURCE_DLL} as "
                      f"{target} ({dest_label}).")

            if mode == "mod" and mod_name is not None:
                self._register_mod(mod_name)

            extra = ("" if mode == "game" else
                     self.tr("\n\nDeploy your mods to activate it."))
            safe_emit(self._install_status_sig,
                      self.tr("Special K installed successfully!\n"
                      "{0} installed as {1} into the {2}.{3}\n\n"
                      "Click Done to close.")
                      .format(_SOURCE_DLL, proxy, dest_label, extra), GREEN)
            safe_emit(self._install_done_sig, True)
        except Exception as exc:
            safe_emit(self._install_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"Special K Wizard error: {exc}")
            safe_emit(self._install_done_sig, False)
        finally:
            if tmp_root is not None:
                shutil.rmtree(tmp_root, ignore_errors=True)

    def _resolve_destination(self, mode: str) -> "tuple[Path, str, str | None]":
        """Return (dest_dir, dest_label, mod_name-or-None) for *mode*."""
        if mode == "mod":
            staging = self._game.get_effective_mod_staging_path()
            if staging is None:
                raise RuntimeError("Mod staging path is not configured.")
            mod_dir = staging / _MOD_NAME
            # Replace any previous install so a proxy-name change doesn't leave
            # the old DLL behind as a second copy.
            if mod_dir.exists():
                shutil.rmtree(mod_dir, ignore_errors=True)
            return mod_dir, f"mod folder ({_MOD_NAME})", _MOD_NAME
        if mode == "root":
            return (self._game.get_effective_root_folder_path(),
                    "Root_Folder (staging)", None)
        game_path = self._game.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")
        return game_path, "game folder", None

    def _register_mod(self, mod_name: str) -> None:
        """Write meta.ini + modlist entry and index the mod so it deploys."""
        from Utils.mods.install_as_mod import (
            index_installed_mod, register_as_mod_neutral,
        )
        register_as_mod_neutral(
            self._game, mod_name, self._archive_path,
            log_fn=self._log, root_folder=True)
        # Files are on disk now - index them or the next deploy emits nothing.
        index_installed_mod(self._game, mod_name, log_fn=self._log)

    def _on_install_done(self, ok: bool):
        self._install_bar.setVisible(False)
        self._done_btn.setEnabled(True)
        # A managed-mod install changed modlist.txt - the base _finish()
        # reloads it on the GUI thread when _ran is set.
        if ok and self._installed_mode == "mod":
            self._ran = True
