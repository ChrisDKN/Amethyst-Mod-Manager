"""Fallout 3 / New Vegas BSA Decompressor wizard."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bsa.decompressor import (
    decompressor_mod_dir, find_decompressor_archive, find_extracted_mpi,
    get_config, normalize_output_files, packages_dir,
)
from Utils.bethesda.ttw import find_ttw_installer

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_ALREADY, _PG_SOURCE, _PG_RUN = range(4)

_ARCHIVE_SUFFIXES = (".7z", ".zip", ".rar", ".tar", ".tar.gz", ".tar.bz2",
                     ".tar.xz", ".tgz")


class BSADecompressorView(WizardViewBase):
    """Decompress vanilla BSAs via the native Linux MPI installer."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)
    _paths_picked_sig = Signal(str, object)   # (attr, path)
    _detect_status_sig = Signal(str, str)
    _mpi_ready_sig = Signal(object)           # Path | None
    _run_status_sig2 = Signal(str, str)
    _run_log_sig = Signal(str)
    _run_done_sig = Signal()
    _auto_kick_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 show_header: bool = True, auto_continue: bool = False,
                 **_extra):
        self._config = get_config(game)
        self._is_fallout3 = self._config.game_arg == "--fo3"
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("BSA Decompressor - {0}").format(game.name),
                         show_header=show_header)
        # auto_continue: hands-free mode (curated-profile wizard, premium) -
        # every successful step advances itself; failures still stop.
        self._auto_continue = bool(auto_continue)
        self._exe = find_ttw_installer(game)
        self._mpi_path: "Path | None" = None
        self._game_path: "Path | None" = game.get_game_path()
        self._force_rebuild = False
        self._detect_started = False
        self._napi = None
        self._auto_fetch_started = False
        self._auto_fetch_cancel = threading.Event()

        profile = getattr(self._ctx, "profile_name", None) or "default"
        self._profile = profile
        from Utils.bethesda.ttw import sync_active_profile
        sync_active_profile(game, profile)

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))
        self._paths_picked_sig.connect(self._guard(self._on_path_picked))
        self._detect_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._source_status, t, c)))
        self._mpi_ready_sig.connect(self._guard(self._on_mpi_ready))
        self._run_status_sig2.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._run_log_sig.connect(self._guard(self._append_run_log))
        self._run_done_sig.connect(self._guard(self._on_run_done))
        self._auto_kick_sig.connect(self._guard(self._start_auto_fetch))

        self._stack.addWidget(self._build_download_page())   # 0
        self._stack.addWidget(self._build_already_page())    # 1
        self._stack.addWidget(self._build_source_page())     # 2
        self._stack.addWidget(self._build_run_page_bsa())    # 3

        self._route_initial()

    def _route_initial(self):
        # Already built → offer the skip; else installer present → source;
        # else download.
        try:
            normalize_output_files(
                self._game,
                log_fn=lambda m: self._log(
                    f"BSA Decompressor Wizard: {m}"),
                config=self._config)
        except Exception as exc:
            self._log(f"BSA Decompressor Wizard: output repair error: {exc}")
        if (not self._force_rebuild
                and decompressor_mod_dir(self._game, self._config) is not None):
            self._stack.setCurrentIndex(_PG_ALREADY)
            if self._auto_continue:
                QTimer.singleShot(600, self._guard(self._finish))
        elif find_ttw_installer(self._game) is not None:
            self._goto_source()
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            if self._auto_continue:
                QTimer.singleShot(300, self._guard(
                    lambda: self._install_btn.isEnabled()
                    and self._start_install()))

    def _on_run_done(self):
        self._done_btn.setEnabled(True)
        # Hands-free mode: a successful run (_ran set) closes itself so the
        # host wizard advances; failures wait for the user.
        if self._auto_continue and self._ran:
            QTimer.singleShot(1500, self._guard(self._finish))

    # ---- page 0: download installer ---------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Install the MPI Installer"))
        self._make_note(lay, (
            self.tr("The native Linux MPI installer (also used for Tale of Two "
            "Wastelands) will be downloaded from GitHub\n"
            "and placed in this game's Applications folder.\n\n"
            "Click Install to begin.")))
        self._make_note(lay, self.tr("Installer by SulfurNitride (TTW_Linux_Installer)"))
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
            from Utils.bethesda.ttw import download_installer
            _wlog = lambda m: self._log(f"BSA Decompressor Wizard: {m}")
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

        threading.Thread(target=worker, daemon=True,
                         name="bsadecomp-install").start()

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_source()
        else:
            self._install_btn.setEnabled(True)

    # ---- page 1: already installed ------------------------------------------------
    def _build_already_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("The BSA Decompressor output is already installed"))
        note = QLabel(
            self.tr("The '{0}' mod is already in your mod list - there is "
                    "nothing to re-apply, so you can simply close this wizard."
                    "\n\nRebuild from scratch restores the game to vanilla and "
                    "runs the decompressor again (needs the .mpi package).")
            .format(self._config.output_name))
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        rebuild = self._accent_btn(self.tr("Rebuild from scratch"))
        rebuild.clicked.connect(self._rebuild_from_scratch)
        rh.addWidget(rebuild)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        rh.addWidget(done)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _rebuild_from_scratch(self):
        self._force_rebuild = True
        if find_ttw_installer(self._game) is not None:
            self._goto_source()
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    # ---- page 2: game path + .mpi package -----------------------------------------
    def _build_source_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Game folder & package"))
        if self._is_fallout3:
            note = self.tr(
                "The BSA Decompressor rebuilds the vanilla BSA archives "
                "without compression for faster loading, and the result is "
                "added as a mod.\n\nDownload the 'FO3 BSA Decompressor' main "
                "file from Nexus - the .mpi package inside the archive is "
                "detected automatically.")
            game_label = self.tr("Fallout 3:")
            folder_title = self.tr("Select the Fallout 3 folder")
        else:
            note = self.tr(
                "The BSA Decompressor rebuilds the vanilla BSA archives "
                "without compression for faster loading, and the result is "
                "added as a mod.\n\nDownload the 'FNV BSA Decompressor' main "
                "file from Nexus - the .mpi package inside the archive is "
                "detected automatically.")
            game_label = self.tr("Fallout New Vegas:")
            folder_title = self.tr("Select the Fallout New Vegas folder")
        self._make_note(lay, note)
        nexus = QPushButton(self.tr("Open Nexus page"))
        nexus.setCursor(Qt.PointingHandCursor)
        nexus.clicked.connect(lambda: self._open_url(self._config.nexus_url))
        lay.addWidget(nexus, 0, Qt.AlignHCenter)

        self._game_label = self._path_row(
            lay, game_label, self._game_path,
            lambda: self._browse_folder("game", folder_title))
        self._mpi_label = self._path_row(
            lay, self.tr("BSA Decompressor package:"), self._mpi_path,
            self._browse_mpi, browse_text=self.tr("Choose file…"))

        self._source_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        redetect = QPushButton(self.tr("Detect again"))
        redetect.setCursor(Qt.PointingHandCursor)
        redetect.clicked.connect(self._start_detect)
        rh.addWidget(redetect)
        cont = self._accent_btn(self.tr("Continue"))
        cont.clicked.connect(self._validate_and_run)
        rh.addWidget(cont)
        rh.addStretch(1)
        lay.addWidget(row)
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

    def _finish(self):
        self._auto_fetch_cancel.set()
        super()._finish()

    def _goto_source(self):
        self._stack.setCurrentIndex(_PG_SOURCE)
        # Resolve the shared Nexus API here (GUI thread) for the hands-free
        # fetch that kicks in when the archive isn't found.
        if self._napi is None:
            api_fn = getattr(self._ctx, "nexus_api", None)
            if api_fn is not None:
                try:
                    self._napi = api_fn()
                except Exception:
                    self._napi = None
        if not self._detect_started:
            self._detect_started = True
            self._start_detect()

    def _start_detect(self):
        if self._mpi_path is not None and self._mpi_path.is_file():
            return
        self._set_status(self._source_status,
                         self.tr("Looking for the BSA Decompressor download…"))
        game = self._game

        def worker():
            from Utils.bsa.decompressor import extract_mpi_from_archive
            _wlog = lambda m: self._log(f"BSA Decompressor Wizard: {m}")
            try:
                mpi = find_extracted_mpi(game, self._config)
                if mpi is not None:
                    _wlog(f"reusing previously extracted {mpi.name}")
                    safe_emit(self._detect_status_sig,
                              self.tr("Using previously extracted package."),
                              GREEN)
                    safe_emit(self._mpi_ready_sig, mpi)
                    return
                archive = find_decompressor_archive(self._config)
                if archive is None:
                    safe_emit(self._detect_status_sig,
                              self.tr("Archive not found in your download "
                              "folders - download it from Nexus, then click "
                              "Detect again (or Choose file…)."), RED)
                    safe_emit(self._mpi_ready_sig, None)
                    safe_emit(self._auto_kick_sig)
                    return
                _wlog(f"auto-detected {archive}")
                safe_emit(self._detect_status_sig,
                          self.tr("Extracting the .mpi package from {0}…")
                          .format(archive.name), "")
                mpi = extract_mpi_from_archive(archive, packages_dir(game),
                                               log_fn=_wlog)
                safe_emit(self._detect_status_sig,
                          self.tr("Auto-detected from {0}.").format(archive.name),
                          GREEN)
                safe_emit(self._mpi_ready_sig, mpi)
            except Exception as exc:
                _wlog(f"auto-detect error: {exc}")
                safe_emit(self._detect_status_sig,
                          self.tr("Auto-detect failed: {0}").format(exc), RED)
                safe_emit(self._mpi_ready_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="bsadecomp-detect").start()

    def _on_mpi_ready(self, mpi):
        if mpi is None:
            return
        self._auto_fetch_cancel.set()    # package secured - stop the fetch
        self._mpi_path = Path(mpi)
        self._mpi_label.setText(str(self._mpi_path))
        self._mpi_label.setStyleSheet(self._dim)
        if self._auto_continue:
            QTimer.singleShot(600, self._guard(self._maybe_auto_run))

    def _maybe_auto_run(self):
        """Hands-free mode: run as soon as both inputs are ready (only while
        still on the source page, so a repeat detect can't double-start)."""
        if (self._stack.currentIndex() == _PG_SOURCE
                and self._mpi_path is not None and self._mpi_path.is_file()
                and self._game_path is not None and self._game_path.is_dir()):
            self._validate_and_run()

    # ---- hands-free archive fetch (premium download / folder watch) ---------------
    def _start_auto_fetch(self):
        if (self._auto_fetch_started or self._mpi_path is not None
                or self._closing):
            return
        self._auto_fetch_started = True
        from Utils.downloads.mpi import start_auto_fetch
        _wlog = lambda m: self._log(f"BSA Decompressor Wizard: {m}")
        last_pct = [-1]
        config = self._config

        def _progress(done, total):
            if total <= 0:
                return
            pct = min(100, int(done * 100 / total))
            if pct == last_pct[0]:
                return
            last_pct[0] = pct
            safe_emit(self._detect_status_sig,
                      self.tr("Downloading the BSA Decompressor package "
                              "from Nexus… {0}%").format(pct), "")

        start_auto_fetch(
            api=self._napi,
            game_domain=config.nexus_game_domain,
            mod_id=config.nexus_mod_id,
            file_id=config.nexus_file_id,
            find_archive_fn=lambda: find_decompressor_archive(config),
            on_archive=lambda p: safe_emit(self._paths_picked_sig, "mpi", p),
            cancel=self._auto_fetch_cancel,
            label=config.output_name,
            on_download_started=lambda: safe_emit(
                self._detect_status_sig,
                self.tr("Premium account - downloading the BSA Decompressor "
                        "package from Nexus…"), ""),
            on_progress=_progress,
            on_waiting=lambda: safe_emit(
                self._detect_status_sig,
                self.tr("Archive not found - download it from Nexus (button "
                        "above). It will be picked up automatically as soon "
                        "as the download finishes."), ""),
            log_fn=_wlog)

    def _browse_folder(self, attr: str, title: str):
        from Utils.ui.portal import pick_folder
        pick_folder(title,
                    lambda p: safe_emit(self._paths_picked_sig, attr, p))

    def _browse_mpi(self):
        from Utils.ui.portal import pick_file
        pick_file(self.tr("Select the BSA Decompressor .mpi or its archive"),
                  lambda p: safe_emit(self._paths_picked_sig, "mpi", p),
                  filters=[(self.tr("MPI package or archive"),
                            ["*.mpi", "*.7z", "*.zip", "*.rar"]),
                           (self.tr("All files"), ["*"])])

    def _on_path_picked(self, attr: str, path):
        if path is None:
            return
        p = Path(path)
        if attr == "game":
            self._game_path = p
            self._game_label.setText(str(p))
            self._game_label.setStyleSheet(self._dim)
            return
        # Package row: a picked archive routes through the same extractor.
        if p.name.lower().endswith(_ARCHIVE_SUFFIXES):
            self._extract_picked_archive(p)
        else:
            self._on_mpi_ready(p)
            self._set_status(self._source_status,
                             self.tr("Selected: {0}").format(p.name), GREEN)

    def _extract_picked_archive(self, archive: Path):
        self._set_status(self._source_status,
                         self.tr("Extracting the .mpi package from {0}…")
                         .format(archive.name))
        game = self._game

        def worker():
            from Utils.bsa.decompressor import extract_mpi_from_archive
            _wlog = lambda m: self._log(f"BSA Decompressor Wizard: {m}")
            try:
                mpi = extract_mpi_from_archive(archive, packages_dir(game),
                                               log_fn=_wlog)
                safe_emit(self._detect_status_sig,
                          self.tr("Using the .mpi from {0}.").format(archive.name),
                          GREEN)
                safe_emit(self._mpi_ready_sig, mpi)
            except Exception as exc:
                _wlog(f"extract error: {exc}")
                safe_emit(self._detect_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                safe_emit(self._mpi_ready_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="bsadecomp-extract").start()

    def _validate_and_run(self):
        if self._mpi_path is None or not self._mpi_path.is_file():
            self._set_status(self._source_status,
                             self.tr("Please select the BSA Decompressor "
                             ".mpi package (or its downloaded archive)."), RED)
            return
        if self._game_path is None or not self._game_path.is_dir():
            if self._is_fallout3:
                message = self.tr("Fallout 3 folder is not set.")
            else:
                message = self.tr("Fallout New Vegas folder is not set.")
            self._set_status(self._source_status,
                             message, RED)
            return
        self._goto_step(_PG_RUN)
        self._set_status(self._run_status, self.tr("Starting…"))
        threading.Thread(target=self._do_run, daemon=True,
                         name="bsadecomp-build").start()

    # ---- page 3: run (restore → build → register) -------------------------------
    def _build_run_page_bsa(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Decompressing BSA archives"))
        self._make_note(lay, (
            self.tr("The game is first restored to a vanilla state, then the "
            "installer rebuilds the vanilla BSA archives without compression. "
            "This can take a while - please leave it running.\nOutput is "
            "written directly into your mod list as the '{0}' mod.")
            .format(self._config.output_name)))
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
        from Utils.bsa.decompressor import register_output
        from Utils.bethesda.ttw import (
            fnv_required_esms, missing_vanilla_esms, restore_to_vanilla,
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
            self._log(f"BSA Decompressor Wizard: {m}")
            safe_emit(self._run_log_sig, str(m))

        safe_emit(self._run_status_sig2,
                  self.tr("Restoring game to vanilla…"), "")
        safe_emit(self._run_log_sig,
                  self.tr("Restoring game to a vanilla state before install…"))
        ok, game_root = restore_to_vanilla(game, self._profile, log_fn=_rlog)
        if not ok:
            safe_emit(self._run_status_sig2,
                      self.tr("Restore failed - see the log. Fix the issue (or "
                      "restore manually via the Restore button) and retry."),
                      RED)
            safe_emit(self._run_done_sig)
            return
        if game_root is not None:
            self._game_path = game_root

        staging = game.get_effective_mod_staging_path()
        if staging is None:
            safe_emit(self._run_status_sig2,
                      self.tr("Mod staging path is not configured."), RED)
            safe_emit(self._run_done_sig)
            return
        dest = staging / self._config.output_name

        required = (list(self._config.required_data_files)
                    or fnv_required_esms(game))
        missing = missing_vanilla_esms(self._game_path, required)
        if missing:
            detail = ", ".join(missing)
            if self._is_fallout3:
                _rlog(f"missing vanilla game files after restore - {detail}")
                log_message = self.tr(
                    "ERROR: missing vanilla game files:\n{0}").format(detail)
                status_message = self.tr(
                    "Missing vanilla game files even after restoring to "
                    "vanilla - these were never backed up.\nIn Steam, "
                    "right-click the game → Properties → Installed Files → "
                    "Verify integrity of game files, then retry.\n\n{0}")
            else:
                _rlog(f"missing vanilla esms after restore - {detail}")
                log_message = self.tr(
                    "ERROR: missing vanilla plugin files:\n{0}").format(detail)
                status_message = self.tr(
                    "Missing vanilla plugin files even after restoring to "
                    "vanilla - these were never backed up.\nIn Steam, "
                    "right-click the game → Properties → Installed Files → "
                    "Verify integrity of game files, then retry.\n\n{0}")
            safe_emit(self._run_log_sig, log_message)
            safe_emit(self._run_status_sig2,
                      status_message.format(detail), RED)
            safe_emit(self._run_done_sig)
            return

        cmd = [str(exe), "install", "--mpi", str(self._mpi_path),
               self._config.game_arg, str(self._game_path),
               "--dest", str(dest)]
        self._log("BSA Decompressor Wizard: running " + " ".join(cmd))
        safe_emit(self._run_status_sig2,
                  self.tr("Decompressing… (see log below)"), "")
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
            self._log(f"BSA Decompressor Wizard: launch error: {exc}")
            safe_emit(self._run_done_sig)
            return

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self._log(f"BSA Decompressor: {line}")
                safe_emit(self._run_log_sig, line)
                m = activity_re.search(line)
                if m:
                    safe_emit(self._run_status_sig2, m.group(0).strip() + "…",
                              "")
        except Exception as exc:
            self._log(f"BSA Decompressor Wizard: error reading installer "
                      f"output: {exc}")

        rc = proc.wait()
        if rc != 0:
            safe_emit(self._run_status_sig2,
                      self.tr("Installer exited with error (code {0}). See the "
                      "log for details.").format(rc), RED)
            self._log(f"BSA Decompressor Wizard: installer exited with code {rc}.")
            safe_emit(self._run_done_sig)
            return

        safe_emit(self._run_status_sig2,
                  self.tr("Build complete - registering mod…"), GREEN)
        self._log("BSA Decompressor Wizard: build complete.")
        try:
            register_output(game, log_fn=_rlog, config=self._config)
        except Exception as exc:
            safe_emit(self._run_status_sig2,
                      self.tr("Build finished but registering the mod failed: "
                      "{0}").format(exc), RED)
            self._log(f"BSA Decompressor Wizard: register error: {exc}")
            safe_emit(self._run_done_sig)
            return
        from Utils.bethesda.ttw import remove_cached_mpi
        remove_cached_mpi(game, self._mpi_path, log_fn=_rlog)
        self._ran = True
        safe_emit(self._run_status_sig2,
                  self.tr("Done! '{0}' was added to your mod list. Enable it "
                  "and deploy.").format(self._config.output_name), GREEN)
        safe_emit(self._run_done_sig)

    # ---- routing helper -----------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
