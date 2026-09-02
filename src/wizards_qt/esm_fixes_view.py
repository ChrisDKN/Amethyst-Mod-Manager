"""Ultimate Edition ESM Fixes wizard - patches the vanilla .esm masters with
community bugfixes via the same native Linux MPI installer the TTW wizard
uses (no Proton).

Flow: download the binary (if missing) → confirm the FNV path + the
'Ultimate Edition ESM Fixes Remastered' .mpi package (auto-detected from the
Downloads folder(s) and extracted from the Nexus archive automatically) →
restore the game to vanilla → run the installer with a live log → register
the output (the six patched masters) as a mod.
"""

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
from Utils.bethesda.esm_fixes import (
    NEXUS_URL, OUTPUT_NAME, esm_fixes_mod_dir, find_esm_fixes_archive,
    find_extracted_mpi, packages_dir,
)
from Utils.bethesda.ttw import find_ttw_installer

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_ALREADY, _PG_SOURCE, _PG_RUN = range(4)

_ARCHIVE_SUFFIXES = (".7z", ".zip", ".rar", ".tar", ".tar.gz", ".tar.bz2",
                     ".tar.xz", ".tgz")


class ESMFixesView(WizardViewBase):
    """Patch the vanilla FNV masters via the native Linux MPI installer."""

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
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Ultimate Edition ESM Fixes - {0}").format(game.name),
                         show_header=show_header)
        # auto_continue: hands-free mode (curated-profile wizard, premium) -
        # every successful step advances itself; failures still stop.
        self._auto_continue = bool(auto_continue)
        self._exe = find_ttw_installer(game)
        self._mpi_path: "Path | None" = None
        self._fnv_path: "Path | None" = game.get_game_path()
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
        self._stack.addWidget(self._build_run_page_esm())    # 3

        self._route_initial()

    def _route_initial(self):
        # Already built → offer the skip; else installer present → source;
        # else download.
        if not self._force_rebuild and esm_fixes_mod_dir(self._game) is not None:
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
            _wlog = lambda m: self._log(f"ESM Fixes Wizard: {m}")
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
                         name="esmfixes-install").start()

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_source()
        else:
            self._install_btn.setEnabled(True)

    # ---- page 1: already installed ------------------------------------------------
    def _build_already_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("The ESM Fixes output is already installed"))
        note = QLabel(
            self.tr("The '{0}' mod is already in your mod list - there is "
                    "nothing to re-apply, so you can simply close this wizard."
                    "\n\nRebuild from scratch restores the game to vanilla and "
                    "runs the patcher again (needs the .mpi package).")
            .format(OUTPUT_NAME))
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

    # ---- page 2: FNV path + .mpi package --------------------------------------------
    def _build_source_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Game folder & package"))
        self._make_note(lay, (
            self.tr("Ultimate Edition ESM Fixes patches the vanilla .esm "
            "masters (FalloutNV + all DLC) with community bugfixes, and the "
            "result is added as a mod.\n\nDownload the 'Ultimate Edition ESM "
            "Fixes Remastered' main file from Nexus - the .mpi package inside "
            "the archive is detected automatically.")))
        nexus = QPushButton(self.tr("Open Nexus page"))
        nexus.setCursor(Qt.PointingHandCursor)
        nexus.clicked.connect(lambda: self._open_url(NEXUS_URL))
        lay.addWidget(nexus, 0, Qt.AlignHCenter)

        self._fnv_label = self._path_row(
            lay, self.tr("Fallout New Vegas:"), self._fnv_path,
            lambda: self._browse_folder(
                "fnv", self.tr("Select the Fallout New Vegas folder")))
        self._mpi_label = self._path_row(
            lay, self.tr("ESM Fixes package:"), self._mpi_path,
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
                         self.tr("Looking for the ESM Fixes download…"))
        game = self._game

        def worker():
            from Utils.bethesda.esm_fixes import extract_mpi_from_archive
            _wlog = lambda m: self._log(f"ESM Fixes Wizard: {m}")
            try:
                mpi = find_extracted_mpi(game)
                if mpi is not None:
                    _wlog(f"reusing previously extracted {mpi.name}")
                    safe_emit(self._detect_status_sig,
                              self.tr("Using previously extracted package."),
                              GREEN)
                    safe_emit(self._mpi_ready_sig, mpi)
                    return
                archive = find_esm_fixes_archive()
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
                         name="esmfixes-detect").start()

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
                and self._fnv_path is not None and self._fnv_path.is_dir()):
            self._validate_and_run()

    # ---- hands-free archive fetch (premium download / folder watch) ---------------
    def _start_auto_fetch(self):
        if (self._auto_fetch_started or self._mpi_path is not None
                or self._closing):
            return
        self._auto_fetch_started = True
        from Utils.bethesda.esm_fixes import (
            NEXUS_FILE_ID, NEXUS_GAME_DOMAIN, NEXUS_MOD_ID,
        )
        from Utils.downloads.mpi import start_auto_fetch
        _wlog = lambda m: self._log(f"ESM Fixes Wizard: {m}")
        last_pct = [-1]

        def _progress(done, total):
            if total <= 0:
                return
            pct = min(100, int(done * 100 / total))
            if pct == last_pct[0]:
                return
            last_pct[0] = pct
            safe_emit(self._detect_status_sig,
                      self.tr("Downloading the ESM Fixes package from "
                              "Nexus… {0}%").format(pct), "")

        start_auto_fetch(
            api=self._napi,
            game_domain=NEXUS_GAME_DOMAIN,
            mod_id=NEXUS_MOD_ID,
            file_id=NEXUS_FILE_ID,
            find_archive_fn=find_esm_fixes_archive,
            on_archive=lambda p: safe_emit(self._paths_picked_sig, "mpi", p),
            cancel=self._auto_fetch_cancel,
            label="Ultimate Edition ESM Fixes",  # i18n: skip — mod name, used in log lines
            on_download_started=lambda: safe_emit(
                self._detect_status_sig,
                self.tr("Premium account - downloading the ESM Fixes "
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
        pick_file(self.tr("Select the ESM Fixes .mpi or its archive"),
                  lambda p: safe_emit(self._paths_picked_sig, "mpi", p),
                  filters=[(self.tr("MPI package or archive"),
                            ["*.mpi", "*.7z", "*.zip", "*.rar"]),
                           (self.tr("All files"), ["*"])])

    def _on_path_picked(self, attr: str, path):
        if path is None:
            return
        p = Path(path)
        if attr == "fnv":
            self._fnv_path = p
            self._fnv_label.setText(str(p))
            self._fnv_label.setStyleSheet(self._dim)
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
            from Utils.bethesda.esm_fixes import extract_mpi_from_archive
            _wlog = lambda m: self._log(f"ESM Fixes Wizard: {m}")
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
                         name="esmfixes-extract").start()

    def _validate_and_run(self):
        if self._mpi_path is None or not self._mpi_path.is_file():
            self._set_status(self._source_status,
                             self.tr("Please select the ESM Fixes .mpi "
                             "package (or its downloaded archive)."), RED)
            return
        if self._fnv_path is None or not self._fnv_path.is_dir():
            self._set_status(self._source_status,
                             self.tr("Fallout New Vegas folder is not set."), RED)
            return
        self._goto_step(_PG_RUN)
        self._set_status(self._run_status, self.tr("Starting…"))
        threading.Thread(target=self._do_run, daemon=True,
                         name="esmfixes-build").start()

    # ---- page 3: run (restore → patch → register) -------------------------------
    def _build_run_page_esm(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Patching the vanilla masters"))
        self._make_note(lay, (
            self.tr("The game is first restored to a vanilla state, then the "
            "installer patches the vanilla .esm masters with the community "
            "bugfixes.\nOutput is written directly into your mod list as the "
            "'{0}' mod.").format(OUTPUT_NAME)))
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
        from Utils.bethesda.esm_fixes import register_output
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
            self._log(f"ESM Fixes Wizard: {m}")
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
        dest = staging / OUTPUT_NAME

        fnv_missing = missing_vanilla_esms(self._fnv_path,
                                           fnv_required_esms(game))
        if fnv_missing:
            detail = ", ".join(fnv_missing)
            _rlog(f"missing vanilla esms after restore - {detail}")
            safe_emit(self._run_log_sig,
                      self.tr("ERROR: missing vanilla plugin files:\n{0}").format(
                          detail))
            safe_emit(self._run_status_sig2,
                      self.tr("Missing vanilla plugin files even after restoring "
                      "to vanilla - these were never backed up.\nIn Steam, "
                      "right-click the game → Properties → Installed Files → "
                      "Verify integrity of game files, then retry.\n\n{0}")
                      .format(detail), RED)
            safe_emit(self._run_done_sig)
            return

        # The package checksum-checks FalloutNV.exe; restore does not revert
        # the 4GB patch (it keeps its own FalloutNV_backup.exe), so warn.
        try:
            from Utils.bethesda.fnv4gb import inspect_exe
            if inspect_exe(self._fnv_path).get("state") == "patched":
                safe_emit(self._run_log_sig,
                          self.tr("WARNING: FalloutNV.exe is 4GB-patched. The "
                          "installer verifies the game exe and may refuse to "
                          "run - if it fails below, restore the original exe "
                          "via the 4GB Patch wizard, run this again, then "
                          "re-apply the 4GB patch."))
        except Exception:
            pass

        cmd = [str(exe), "install", "--mpi", str(self._mpi_path),
               "--fnv", str(self._fnv_path), "--dest", str(dest)]
        self._log("ESM Fixes Wizard: running " + " ".join(cmd))
        safe_emit(self._run_status_sig2,
                  self.tr("Patching… (see log below)"), "")
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
            self._log(f"ESM Fixes Wizard: launch error: {exc}")
            safe_emit(self._run_done_sig)
            return

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self._log(f"ESM Fixes: {line}")
                safe_emit(self._run_log_sig, line)
                m = activity_re.search(line)
                if m:
                    safe_emit(self._run_status_sig2, m.group(0).strip() + "…",
                              "")
        except Exception as exc:
            self._log(f"ESM Fixes Wizard: error reading installer output: {exc}")

        rc = proc.wait()
        if rc != 0:
            safe_emit(self._run_status_sig2,
                      self.tr("Installer exited with error (code {0}). See the "
                      "log for details.").format(rc), RED)
            self._log(f"ESM Fixes Wizard: installer exited with code {rc}.")
            safe_emit(self._run_done_sig)
            return

        safe_emit(self._run_status_sig2,
                  self.tr("Patching complete - registering mod…"), GREEN)
        self._log("ESM Fixes Wizard: patching complete.")
        try:
            register_output(game, log_fn=_rlog)
        except Exception as exc:
            safe_emit(self._run_status_sig2,
                      self.tr("Patching finished but registering the mod "
                      "failed: {0}").format(exc), RED)
            self._log(f"ESM Fixes Wizard: register error: {exc}")
            safe_emit(self._run_done_sig)
            return
        self._ran = True
        safe_emit(self._run_status_sig2,
                  self.tr("Done! '{0}' was added to your mod list. Enable it "
                  "and deploy.").format(OUTPUT_NAME), GREEN)
        safe_emit(self._run_done_sig)

    # ---- routing helper -----------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
