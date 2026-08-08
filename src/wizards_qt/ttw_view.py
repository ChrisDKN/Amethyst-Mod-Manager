"""Tale of Two Wastelands installer wizard - Qt port of wizards/ttw.py.

Installs TTW on Fallout New Vegas via the native Linux MPI installer (no
Proton).  Flow: download the binary → confirm FNV/FO3 paths + pick the .mpi
→ restore the game to vanilla → run the installer with a live log → register
the output as the 'Tale of Two Wastelands' mod, set up its profile INIs +
FalloutCustom.ini and seed its recommended Nexus requirements.  When TTW is
already built it offers a fast setup-only re-apply.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.ttw_tools import (
    GITHUB_REPO_URL, MODPUB_URL, OUTPUT_NAME,
    find_fo3_install, find_ttw_installer, ttw_mod_dir,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_ALREADY, _PG_PATHS, _PG_RUN = range(4)


class TTWView(WizardViewBase):
    """Install Tale of Two Wastelands via the native Linux MPI installer."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)
    _paths_picked_sig = Signal(str, object)   # (attr, path)
    _run_status_sig2 = Signal(str, str)
    _run_log_sig = Signal(str)
    _run_done_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install Tale of Two Wastelands - {0}").format(game.name))
        self._exe = find_ttw_installer(game)
        self._mpi_path: "Path | None" = None
        self._fo3_path: "Path | None" = find_fo3_install()
        self._fnv_path: "Path | None" = game.get_game_path()
        self._force_rebuild = False

        profile = getattr(self._ctx, "profile_name", None) or "default"
        self._profile = profile
        from Utils.ttw_tools import sync_active_profile
        sync_active_profile(game, profile)

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))
        self._paths_picked_sig.connect(self._guard(self._on_path_picked))
        self._run_status_sig2.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._run_log_sig.connect(self._guard(self._append_run_log))
        self._run_done_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))

        self._stack.addWidget(self._build_download_page())   # 0
        self._stack.addWidget(self._build_already_page())    # 1
        self._stack.addWidget(self._build_paths_page())      # 2
        self._stack.addWidget(self._build_ttw_run_page())    # 3

        self._route_initial()

    def _route_initial(self):
        # Already built → offer the skip; else installer present → paths;
        # else download.
        if not self._force_rebuild and ttw_mod_dir(self._game) is not None:
            self._stack.setCurrentIndex(_PG_ALREADY)
        elif find_ttw_installer(self._game) is not None:
            self._goto_step(_PG_PATHS)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    # ---- page 0: download ------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Install the TTW MPI Installer"))
        self._make_note(lay, (
            self.tr("The native Linux TTW installer will be downloaded from GitHub\n"
            "and placed in this game's Applications folder.\n\n"
            "Click Install to begin.")))
        self._make_note(lay, self.tr("Installer by SulfurNitride (TTW_Linux_Installer)"))
        gh = QPushButton(self.tr("View on GitHub"))
        gh.setCursor(Qt.PointingHandCursor)
        gh.clicked.connect(lambda: self._open_url(GITHUB_REPO_URL))
        lay.addWidget(gh, 0, Qt.AlignHCenter)
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        self._install_btn = self._accent_btn(self.tr("Install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        return page

    def _start_install(self):
        self._install_btn.setEnabled(False)
        self._set_status(self._dl_status, self.tr("Contacting GitHub…"))
        game = self._game

        def worker():
            from Utils.ttw_tools import download_installer
            _wlog = lambda m: self._log(f"TTW Wizard: {m}")
            try:
                self._exe = download_installer(
                    game,
                    status_fn=lambda m: safe_emit(self._dl_status_sig, m, ""),
                    log_fn=_wlog)
                safe_emit(self._dl_status_sig,
                          self.tr("Installer ready."), GREEN)
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                safe_emit(self._dl_status_sig,
                          self.tr("Install error: {0}").format(exc), RED)
                _wlog(f"install error: {exc}")
                safe_emit(self._dl_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="ttw-install").start()

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PATHS)
        else:
            self._install_btn.setEnabled(True)

    # ---- page 1: already installed ---------------------------------------------
    def _build_already_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Tale of Two Wastelands is already installed"))
        note = QLabel(
            self.tr("The '{0}' mod is already in your mod list, so the ~18 GB build can be skipped.\n\n• Re-apply setup only - re-runs the profile INI + FalloutCustom.ini setup without rebuilding (fast).\n\n• Rebuild from scratch - restores to vanilla and runs the full installer again (needs the .mpi + both games).").format(OUTPUT_NAME))
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        lay.addStretch(1)
        reapply = self._accent_btn(self.tr("Re-apply setup only"))
        reapply.clicked.connect(self._start_setup_only)
        lay.addWidget(reapply, 0, Qt.AlignHCenter)
        rebuild = QPushButton(self.tr("Rebuild from scratch"))
        rebuild.setCursor(Qt.PointingHandCursor)
        rebuild.clicked.connect(self._rebuild_from_scratch)
        lay.addWidget(rebuild, 0, Qt.AlignHCenter)
        return page

    def _rebuild_from_scratch(self):
        self._force_rebuild = True
        if find_ttw_installer(self._game) is not None:
            self._goto_step(_PG_PATHS)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _start_setup_only(self):
        self._goto_step(_PG_RUN)
        self._set_status(self._run_status, self.tr("Re-applying TTW setup…"))
        threading.Thread(target=lambda: self._post_install_setup(rebuilt=False),
                         daemon=True, name="ttw-setup").start()

    # ---- page 2: paths ------------------------------------------------------------
    def _build_paths_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Game folders & TTW package"))
        self._make_note(lay, (
            self.tr("TTW merges assets from both Fallout 3 and Fallout New Vegas, so "
            "both games must be installed. Confirm the folders below, then "
            "select the TTW .mpi package.\n\n"
            "Get the latest TTW .mpi from mod.pub (free account required) - "
            "extract the download and the .mpi is inside.")))
        modpub = QPushButton(self.tr("Open mod.pub TTW page"))
        modpub.setCursor(Qt.PointingHandCursor)
        modpub.clicked.connect(lambda: self._open_url(MODPUB_URL))
        lay.addWidget(modpub, 0, Qt.AlignHCenter)

        self._fnv_label = self._path_row(
            lay, self.tr("Fallout New Vegas:"), self._fnv_path,
            lambda: self._browse_folder(
                "fnv", self.tr("Select the Fallout New Vegas folder")))
        self._fo3_label = self._path_row(
            lay, self.tr("Fallout 3:"), self._fo3_path,
            lambda: self._browse_folder(
                "fo3", self.tr("Select the Fallout 3 folder")))
        self._mpi_label = self._path_row(
            lay, self.tr("TTW .mpi package:"), self._mpi_path, self._browse_mpi,
            browse_text=self.tr("Choose .mpi…"))

        self._paths_status = self._make_status(lay)
        lay.addStretch(1)
        cont = self._accent_btn(self.tr("Continue"))
        cont.clicked.connect(self._validate_and_run)
        lay.addWidget(cont, 0, Qt.AlignHCenter)
        return page

    def _path_row(self, lay, label, value, browse_cmd, browse_text=None):
        if browse_text is None:
            browse_text = self.tr("Browse…")
        p = active_palette()
        row = QWidget()
        row.setStyleSheet(f"background:{_c(p,'BG_PANEL')}; border-radius:6px;")
        rl = QVBoxLayout(row); rl.setContentsMargins(8, 4, 8, 4); rl.setSpacing(2)
        header = QWidget()
        hh = QHBoxLayout(header); hh.setContentsMargins(0, 0, 0, 0)
        title = QLabel(label)
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hh.addWidget(title)
        hh.addStretch(1)
        browse = QPushButton(browse_text)
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(browse_cmd)
        hh.addWidget(browse)
        rl.addWidget(header)
        val = QLabel(str(value) if value else self.tr("- not set -"))
        val.setWordWrap(True)
        val.setStyleSheet(self._dim if value else f"color:{RED};")
        rl.addWidget(val)
        lay.addWidget(row)
        return val

    def _browse_folder(self, attr: str, title: str):
        from Utils.portal_filechooser import pick_folder
        pick_folder(title,
                    lambda p: safe_emit(self._paths_picked_sig, attr, p))

    def _browse_mpi(self):
        from Utils.portal_filechooser import pick_file
        pick_file(self.tr("Select the TTW .mpi package"),
                  lambda p: safe_emit(self._paths_picked_sig, "mpi", p),
                  filters=[(self.tr("TTW Package (*.mpi)"), ["*.mpi"]),
                           (self.tr("All files"), ["*"])])

    def _on_path_picked(self, attr: str, path):
        if path is None:
            return
        p = Path(path)
        label = {"fnv": self._fnv_label, "fo3": self._fo3_label,
                 "mpi": self._mpi_label}[attr]
        setattr(self, f"_{attr}_path", p)
        label.setText(str(p))
        label.setStyleSheet(self._dim)

    def _validate_and_run(self):
        if self._mpi_path is None or not self._mpi_path.is_file():
            self._set_status(self._paths_status,
                             self.tr("Please select the TTW .mpi package."), RED)
            return
        if self._fnv_path is None or not self._fnv_path.is_dir():
            self._set_status(self._paths_status,
                             self.tr("Fallout New Vegas folder is not set."), RED)
            return
        if self._fo3_path is None or not self._fo3_path.is_dir():
            self._set_status(self._paths_status,
                             self.tr("Fallout 3 folder is not set. TTW requires "
                             "Fallout 3 to be installed."), RED)
            return
        self._goto_step(_PG_RUN)
        self._set_status(self._run_status, self.tr("Starting…"))
        threading.Thread(target=self._do_run, daemon=True,
                         name="ttw-build").start()

    # ---- page 3: run (restore → build → register) --------------------------------
    def _build_ttw_run_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Building Tale of Two Wastelands"))
        self._make_note(lay, (
            self.tr("The game is first restored to a vanilla state, then the installer\nmerges Fallout 3 and Fallout New Vegas assets. This produces ~18 GB of output and can take a long while - please leave it running.\nOutput is written directly into your mod list as the '{0}' mod.").format(OUTPUT_NAME)))
        self._run_status = self._make_status(lay)
        p = active_palette()
        self._run_output = QPlainTextEdit()
        self._run_output.setReadOnly(True)
        self._run_output.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none;}}")
        lay.addWidget(self._run_output, 1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _append_run_log(self, text: str):
        self._run_output.appendPlainText(text)

    def _do_run(self):
        import subprocess
        from Utils.ttw_tools import (
            FO3_REQUIRED_ESMS, OUTPUT_NAME as _ON, fnv_required_esms,
            missing_vanilla_esms, register_output, restore_to_vanilla,
        )
        game = self._game
        exe = self._exe
        if exe is None or not exe.is_file():
            safe_emit(self._run_status_sig2,
                      self.tr("Installer binary is missing. Restart the wizard "
                      "and let it install first."), RED)
            safe_emit(self._run_done_sig)
            return

        def _rlog(m):
            self._log(f"TTW Wizard: {m}")
            safe_emit(self._run_log_sig, str(m))

        safe_emit(self._run_status_sig2,
                  self.tr("Restoring game to vanilla…"), "")
        safe_emit(self._run_log_sig,
                  self.tr("Restoring game to a vanilla state before install…"))
        ok, fnv_root = restore_to_vanilla(game, self._profile, log_fn=_rlog)
        if not ok:
            safe_emit(self._run_status_sig2,
                      self.tr("Restore failed - see the log. Fix the issue (or "
                      "restore manually via the Restore button) and retry."),
                      RED)
            safe_emit(self._run_done_sig)
            return
        if fnv_root is not None:
            self._fnv_path = fnv_root

        staging = game.get_effective_mod_staging_path()
        if staging is None:
            safe_emit(self._run_status_sig2,
                      self.tr("Mod staging path is not configured."), RED)
            safe_emit(self._run_done_sig)
            return
        dest = staging / _ON

        fnv_missing = missing_vanilla_esms(self._fnv_path,
                                           fnv_required_esms(game))
        fo3_missing = missing_vanilla_esms(self._fo3_path, FO3_REQUIRED_ESMS)
        if fnv_missing or fo3_missing:
            parts = []
            if fnv_missing:
                parts.append(self.tr("Fallout New Vegas: {0}").format(
                    ", ".join(fnv_missing)))
            if fo3_missing:
                parts.append(self.tr("Fallout 3: {0}").format(
                    ", ".join(fo3_missing)))
            detail = "\n".join(parts)
            _rlog(f"missing vanilla esms after restore - {detail}")
            safe_emit(self._run_log_sig,
                      self.tr("ERROR: missing vanilla plugin files:\n{0}").format(
                          detail))
            safe_emit(self._run_status_sig2,
                      self.tr("Missing vanilla plugin files even after restoring "
                      "to vanilla - these were never backed up.\nIn Steam, "
                      "right-click each game → Properties → Installed Files → "
                      "Verify integrity of game files, then retry.\n\n{0}")
                      .format(detail), RED)
            safe_emit(self._run_done_sig)
            return

        cmd = [str(exe), "install", "--mpi", str(self._mpi_path),
               "--fo3", str(self._fo3_path), "--fnv", str(self._fnv_path),
               "--dest", str(dest)]
        self._log("TTW Wizard: running " + " ".join(cmd))
        safe_emit(self._run_status_sig2,
                  self.tr("Installing… (see log below)"), "")
        activity_re = re.compile(
            r"\b(Building ready BSA|Extracting|Patching|Cleaning up)[^\r\n]*")

        try:
            dest.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                cmd, cwd=str(exe.parent), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as exc:
            safe_emit(self._run_status_sig2,
                      self.tr("Launch error: {0}").format(exc), RED)
            self._log(f"TTW Wizard: launch error: {exc}")
            safe_emit(self._run_done_sig)
            return

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self._log(f"TTW: {line}")
                safe_emit(self._run_log_sig, line)
                m = activity_re.search(line)
                if m:
                    safe_emit(self._run_status_sig2, m.group(0).strip() + "…",
                              "")
        except Exception as exc:
            self._log(f"TTW Wizard: error reading installer output: {exc}")

        rc = proc.wait()
        if rc != 0:
            safe_emit(self._run_status_sig2,
                      self.tr("Installer exited with error (code {0}). See the "
                      "log for details.").format(rc), RED)
            self._log(f"TTW Wizard: installer exited with code {rc}.")
            safe_emit(self._run_done_sig)
            return

        safe_emit(self._run_status_sig2,
                  self.tr("Install complete - registering mod…"), GREEN)
        self._log("TTW Wizard: install complete.")
        try:
            register_output(game, dest, log_fn=_rlog)
        except Exception as exc:
            safe_emit(self._run_status_sig2,
                      self.tr("Install finished but registering the mod failed: "
                      "{0}").format(exc), RED)
            self._log(f"TTW Wizard: register error: {exc}")
            safe_emit(self._run_done_sig)
            return
        self._ran = True
        self._post_install_setup(rebuilt=True)

    def _post_install_setup(self, *, rebuilt: bool):
        from Utils.ttw_tools import (
            OUTPUT_NAME as _ON, seed_required_mods, sync_active_profile,
        )
        game = self._game
        sync_active_profile(game, self._profile)

        def _ilog(m):
            self._log(f"TTW Wizard: {m}")
            safe_emit(self._run_log_sig, str(m))

        setup = getattr(game, "setup_ttw_custom_ini", None)
        if callable(setup):
            try:
                safe_emit(self._run_log_sig,
                          self.tr("Setting up profile INIs + FalloutCustom.ini "
                          "for TTW…"))
                setup(self._profile, log_fn=_ilog)
            except Exception as exc:
                _ilog(f"FalloutCustom.ini setup failed: {exc}")

        try:
            seed_required_mods(game, log_fn=_ilog)
            safe_emit(self._run_log_sig,
                      self.tr("Recommended Nexus mods are flagged on the TTW mod "
                      "via the 'missing requirements' marker (installed ones "
                      "are hidden automatically)."))
        except Exception as exc:
            _ilog(f"Seeding requirements failed: {exc}")

        self._ran = True
        done_msg = (
            self.tr("Done! '{0}' was added to your mod list. Enable it and "
                    "deploy.").format(_ON)
            if rebuilt else
            self.tr("Setup re-applied for the existing '{0}' mod. Enable it and "
                    "deploy.").format(_ON))
        safe_emit(self._run_status_sig2,
                  done_msg + self.tr("\n\nTTW needs several supporting mods "
                  "(script extender plugins, patches, etc.). These are flagged "
                  "on the TTW mod via the red 'missing requirements' marker - "
                  "click it to install them, then deploy."), GREEN)
        safe_emit(self._run_done_sig)

    # ---- routing helper ------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
