"""MGE XE wizard (Morrowind) - Qt port of Games/Morrowind/mgexe_wizard.py.

MGE XE bundles MWSE. Two install paths, auto-detected from the archive name:
  * Installer - archive contains MGEXE-<version>-installer.exe; extracted to
    the game root, then run via the game's Proton prefix. The installer .exe
    writes into the live game folder when run, so it always installs there.
  * Manual    - loose files (d3d8.dll, MGEXEgui.exe, mge3/, MWSE-Update.exe …).
    The user picks a destination: the game's Root_Folder staging, or a managed
    root-flagged mod (registered in the modlist + indexed so it deploys).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QRadioButton,
    QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/morrowind/mods/41102?tab=files&file_id=1000048202"
_NEXUS_FILE_ID = 1000048202     # "MGE XE Manual Install" main file
_KEYWORDS_COMMON = ["mge"]
_INSTALLER_EXE_PREFIX = "MGEXE"
_MOD_FALLBACK_NAME = "MGE XE"

_PG_DOWNLOAD, _PG_LOCATE, _PG_DEST, _PG_INSTALL = range(4)


class MGEXEView(WizardViewBase):
    """Download and install MGE XE."""

    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install MGE XE - {0}").format(game.name))
        self._game_root = game.get_game_path()
        self._is_installer = False
        self._dest_mode = "root"          # manual variant: "root" | "mod"

        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._install_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download MGE XE"),
            self.tr("Click the button below to open the MGE XE download page on "
                    "Nexus Mods.\n\nDownload either the Installer or the Manual "
                    "Install archive, then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive"), with_next=True))
        self._stack.addWidget(self._build_dest_page())
        # last page: install (status + Done)
        page, lay = self._step_page(self.tr("Install MGE XE"))
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        self._stack.setCurrentIndex(_PG_DOWNLOAD)
        self._nexus_auto_fetch(
            url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
            keywords=_KEYWORDS_COMMON,
            # The label is the product name, substituted into already-translated
            # shells like "Downloading {0} from Nexus…" - it must not translate.
            label="MGE XE",  # i18n: skip - product name
            pages=(_PG_DOWNLOAD, _PG_LOCATE),
            on_archive=self._on_archive_ready)

    # ---- destination page (manual variant only) -----------------------------
    def _build_dest_page(self) -> QWidget:
        p = active_palette()
        page, lay = self._step_page(self.tr("Step 3: Choose Destination"))
        self._make_note(
            lay,
            self.tr("Choose where to install the MGE XE files. Installing as a "
                    "managed mod lets you toggle and reorder it like any other "
                    "mod; the Root_Folder staging deploys the files straight to "
                    "the game root."))

        box = QFrame()
        box.setStyleSheet(f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        bv = QVBoxLayout(box); bv.setContentsMargins(12, 10, 12, 10); bv.setSpacing(4)
        head = QLabel(self.tr("Install destination"))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        bv.addWidget(head)
        self._dest_group = QButtonGroup(self)
        for val, label in (("root", self.tr("Root_Folder (staging)")),
                           ("mod", self.tr("As a managed mod (root-flagged)"))):
            rb = QRadioButton(label)
            rb.setProperty("dest", val)
            if val == self._dest_mode:
                rb.setChecked(True)
            self._dest_group.addButton(rb)
            rb.toggled.connect(self._sync_mod_name_state)
            bv.addWidget(rb)

        mod_row = QWidget()
        mh = QHBoxLayout(mod_row); mh.setContentsMargins(0, 4, 0, 0); mh.setSpacing(8)
        self._mod_name_lbl = QLabel(self.tr("Mod name"))
        self._mod_name_lbl.setStyleSheet(self._dim)
        mh.addWidget(self._mod_name_lbl)
        self._mod_name_edit = QLineEdit(_MOD_FALLBACK_NAME)
        mh.addWidget(self._mod_name_edit, 1)
        bv.addWidget(mod_row)
        lay.addWidget(box)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Install →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_INSTALL))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _selected_dest(self) -> str:
        btn = self._dest_group.checkedButton()
        return btn.property("dest") if btn is not None else "root"

    def _sync_mod_name_state(self, *_):
        is_mod = self._selected_dest() == "mod"
        self._mod_name_lbl.setEnabled(is_mod)
        self._mod_name_edit.setEnabled(is_mod)

    # ---- navigation ---------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _KEYWORDS_COMMON, self.tr("Select the MGE XE archive"),
                self.tr("MGE XE archive not found in Downloads.\n"
                        "Make sure you downloaded it, then press Try Again,\n"
                        "or use Browse to select it manually."),
                self._on_archive_ready)
        elif idx == _PG_DEST:
            self._sync_mod_name_state()
        elif idx == _PG_INSTALL:
            if self._is_installer:
                self._set_status(self._install_status,
                                 self.tr("Extracting archive to game folder…"))
            else:
                self._dest_mode = self._selected_dest()
                self._set_status(self._install_status,
                                 self.tr("Extracting archive…"))
            threading.Thread(target=self._do_install, daemon=True,
                             name="mgexe-install").start()

    def _on_archive_ready(self, path):
        self._is_installer = "installer" in Path(path).name.lower()
        # The installer .exe must run in the live game folder, so it skips the
        # destination choice; the manual (loose-files) variant offers it.
        self._goto_step(_PG_INSTALL if self._is_installer else _PG_DEST)

    # ---- workers ------------------------------------------------------------
    def _do_install(self):
        if self._is_installer:
            self._do_installer()
        else:
            self._do_manual()

    def _do_installer(self):
        from Utils.wizards.archives import extract_archive
        try:
            if self._game_root is None:
                raise RuntimeError(self.tr("Game path is not configured."))
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError(self.tr("Archive not found."))

            safe_emit(self._install_status_sig,
                      self.tr("Extracting archive to game folder…"), "")
            self._log(f"MGE XE Wizard: extracting {archive.name} → {self._game_root}")
            paths = extract_archive(archive, self._game_root)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"MGE XE Wizard: extracted {file_count} file(s).")
            self._ran = True

            try:
                archive.unlink()
                self._log(f"MGE XE Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"MGE XE Wizard: could not delete archive: {exc}")

            installer_exe = next(
                (p for p in self._game_root.iterdir()
                 if p.is_file()
                 and p.name.upper().startswith(_INSTALLER_EXE_PREFIX)
                 and p.suffix.lower() == ".exe"), None)
            if installer_exe is None:
                raise RuntimeError(self.tr(
                    "Installer exe not found in game folder after extraction.\n"
                    "Expected a file starting with '{0}' (.exe).")
                    .format(_INSTALLER_EXE_PREFIX))
            safe_emit(self._install_status_sig,
                      self.tr("Running {0} via Proton…\n"
                              "Follow the installer steps, then come back and "
                              "click Done.").format(installer_exe.name), "")
            self._run_exe(installer_exe)
            self._log("MGE XE Wizard: installer completed.")
            safe_emit(self._install_status_sig,
                      self.tr("MGE XE installer finished.\n\n"
                              "Click Done to close."),
                      GREEN)
            safe_emit(self._install_done_sig)
        except Exception as exc:
            safe_emit(self._install_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"MGE XE Wizard error: {exc}")
            safe_emit(self._install_done_sig)

    def _do_manual(self):
        from Utils.wizards.archives import install_archive_payload
        try:
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError(self.tr("Archive not found."))
            mode = self._dest_mode
            if mode == "mod":
                name = self._mod_name_edit.text().strip() or _MOD_FALLBACK_NAME
                safe_emit(self._install_status_sig,
                          self.tr("Installing MGE XE as mod '{0}'…")
                          .format(name), "")
            else:
                safe_emit(self._install_status_sig,
                          self.tr("Extracting archive to Root_Folder…"), "")

            dest_label, file_count, mod_name = install_archive_payload(
                self._game, archive, mode,
                mod_fallback_name=_MOD_FALLBACK_NAME,
                log_fn=lambda m: self._log(f"MGE XE Wizard: {m}"))
            self._ran = True

            if mode == "mod":
                msg = self.tr("MGE XE installed as mod '{0}'.\n"
                              "{1} file(s) staged.\n\n"
                              "Deploy to apply it.\n\nClick Done to close.") \
                    .format(mod_name, file_count)
            else:
                msg = self.tr("MGE XE installed to Root_Folder!\n"
                              "{0} file(s) extracted.\n\n"
                              "Deploy to apply it.\n\nClick Done to close.") \
                    .format(file_count)
            safe_emit(self._install_status_sig, msg, GREEN)
            safe_emit(self._install_done_sig)
        except Exception as exc:
            safe_emit(self._install_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"MGE XE Wizard error: {exc}")
            safe_emit(self._install_done_sig)

    def _on_install_done(self):
        self._done_btn.setEnabled(True)
        # A managed-mod install changed modlist.txt - reload it now on the GUI
        # thread so the new mod shows without waiting for Done.
        if not self._is_installer and self._dest_mode == "mod":
            refresh = getattr(self._ctx, "refresh_modlist", None)
            if refresh is not None:
                refresh()

    def _run_exe(self, exe: Path):
        import subprocess
        from Utils.executables.launch import (
            get_game_prefix_env, shutdown_prefix_wineserver,
        )
        from Utils.launchers.steam import proton_run_command
        result = get_game_prefix_env(
            self._game, log_fn=lambda m: self._log(f"MGE XE Wizard: {m}"),
            allow_runner_fallback=True)
        if result is None:
            raise RuntimeError(
                self.tr("Could not determine Proton version for this game."))
        proton_script, compat_data, env = result
        self._log(f"MGE XE Wizard: launching {exe} via Proton")
        try:
            proc = subprocess.Popen(
                # runinprefix: skips the steam.exe shim so Steam doesn't show the
                # game as "Running" while the tool is open.
                proton_run_command(proton_script, "runinprefix", str(exe), env=env),
                env=env,
                cwd=str(self._game_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            proc.wait()
            if proc.returncode != 0:
                stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    self.tr("{0} exited with code {1}.\n{2}")
                    .format(exe.name, proc.returncode, stderr))
        finally:
            # Proton sidecars keep the GAME prefix's wineserver alive after the
            # tool exits, which blocks Steam from launching the game. In
            # finally: a crashed tool is when the leak is most likely.
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"MGE XE Wizard: {m}"))
