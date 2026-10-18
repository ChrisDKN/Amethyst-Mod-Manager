"""Qt installation and generation views for the shared LOD workflows."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QStackedWidget,
)

from gui_qt.appearance.theme import (
    active_palette, _c, button_qss, close_button, ok_text, err_text,
)
from gui_qt.runtime.safe_emit import safe_emit
from Utils.bethesda.xedit import tool_exe_path
from Utils.bethesda.lod import (
    NEXUS_URL as _NEXUS_URL, XLODGEN_GITHUB_API as _XLODGEN_GITHUB_API,
    TOOLS as _TOOLS, options,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_PG_DL_MANUAL, _PG_DL_AUTO, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(7)


class DynDOLODView(QWidget):
    """Install (if needed), deploy and run TexGen / DynDOLOD / xLODGen."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)        # percent 0-100
    _dl_done_sig = Signal(bool)
    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # tool launched → enable Done
    _run_finished_sig = Signal()
    _run_failed_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "texgen", **_extra):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx

        (self._name, self._exe_name, self._output_dir, self._app_dir,
         self._archive_kw, self._auto_dl) = _TOOLS[tool]
        self._tool_id = tool
        self._choice = options(game, tool)
        self._install_cancel = threading.Event()

        self._exe = tool_exe_path(game, self._exe_name, self._app_dir)
        self._archive_path: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefer_discrete_gpu = False
        self._proton_step = None
        self._ran = False
        self._run_complete = False
        self._closing = False

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        self._dl_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(_guard(self._on_dl_progress))
        self._dl_done_sig.connect(_guard(self._on_dl_done))
        self._locate_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(_guard(self._on_picked))
        self._extract_done_sig.connect(_guard(self._on_extract_done))
        self._run_started_sig.connect(_guard(self._on_run_started))
        self._run_finished_sig.connect(_guard(self._finish))
        self._run_failed_sig.connect(_guard(lambda: self._done_btn.setEnabled(True)))

        self.setObjectName("DynDOLODView")
        self._build()

    # ---- layout -------------------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("{0} - {1}").format(self._name, self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        if self._exe is not None:
            upgrade = self._accent_btn(self.tr("Update Tool"))
            upgrade.clicked.connect(lambda: self._goto_step(
                _PG_DL_AUTO if self._auto_dl else _PG_DL_MANUAL))
            hb.addWidget(upgrade)
        close = close_button(self.tr("✕ Close"), pal=p)
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)
        if self._exe is not None:
            self._stack.currentChanged.connect(
                lambda index: upgrade.setVisible(index == _PG_DEPLOY))

        self._stack.addWidget(self._build_step_dl_manual())  # 0
        self._stack.addWidget(self._build_step_dl_auto())    # 1
        self._stack.addWidget(self._build_step_locate())     # 2
        self._stack.addWidget(self._build_step_extract())    # 3
        self._stack.addWidget(self._build_step_deploy())     # 4
        self._stack.addWidget(self._build_step_proton())     # 5
        self._stack.addWidget(self._build_step_run())        # 6

        # Already extracted → straight to the deploy step (Tk behaviour).
        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
        elif self._auto_dl:
            self._goto_step(_PG_DL_AUTO)
        else:
            self._stack.setCurrentIndex(_PG_DL_MANUAL)

    def _step_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return page, lay

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    def _accent_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            button_qss("BTN_INFO"))
        return b

    # ---- step 1a: manual download (TexGen / DynDOLOD) ---------------------------
    def _build_step_dl_manual(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download DynDOLOD"))
        note = QLabel(
            self.tr("Click the button below to open the DynDOLOD page on Nexus "
            "Mods.\n\nDownload the archive manually (do NOT use the Mod "
            "Manager download button), then click Next."))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        lay.addSpacing(8)

        open_btn = QPushButton(self.tr("Open Download Page"))
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            button_qss("BTN_WARN"))
        open_btn.clicked.connect(self._open_download_page)
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_LOCATE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_download_page(self):
        from Utils.environment.xdg import open_url
        open_url(_NEXUS_URL)

    # ---- step 1b: auto download (xLODGen) ---------------------------------------
    def _build_step_dl_auto(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download {0}").format(self._name))
        self._dl_status = self._make_status(lay)
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(0)
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)
        lay.addStretch(1)
        return page

    def _start_auto_download(self):
        game = self._game
        name = self._name

        def worker():
            import tempfile
            from Utils.ca_bundle import download_file
            from Utils.wizards.archives import fetch_latest_github_asset
            from Utils.wizards.applications import install_application_archive
            tmp_path = None
            try:
                self._check_install()
                safe_emit(self._dl_status_sig,
                    self.tr("Fetching latest release from GitHub…"), "")
                tag, dl_url = fetch_latest_github_asset(
                    _XLODGEN_GITHUB_API, ["xlodgen"])
                self._check_install()
                safe_emit(self._dl_status_sig, self.tr("Downloading {0}…").format(tag), "")
                self._log(f"{name} Wizard: downloading {tag} from {dl_url}")

                suffix = Path(dl_url).suffix or ".7z"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                def _reporthook(block_num, block_size, total_size):
                    self._check_install()
                    if total_size > 0:
                        pct = min(100, block_num * block_size * 100 / total_size)
                        safe_emit(self._dl_progress_sig, int(pct))

                download_file(dl_url, tmp_path, reporthook=_reporthook)
                safe_emit(self._dl_progress_sig, 100)
                self._log(f"{name} Wizard: download complete, extracting…")
                safe_emit(self._dl_status_sig, self.tr("Extracting…"), "")

                self._exe, file_count = install_application_archive(
                    game, tmp_path, self._choice, log_fn=self._log,
                    checkpoint=self._check_install, family="lod")

                self._log(f"{name} Wizard: extracted {file_count} file(s).")
                safe_emit(self._dl_status_sig,
                    self.tr("Downloaded and extracted {0}.").format(tag), ok_text())
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                safe_emit(self._dl_status_sig, self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: download error: {exc}")
                safe_emit(self._dl_done_sig, False)
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        threading.Thread(target=worker, daemon=True,
                         name="dyndolod-download").start()

    def _on_dl_progress(self, pct: int):
        self._dl_bar.setValue(pct)

    def _on_dl_done(self, ok: bool):
        if ok:
            QTimer.singleShot(
                500, lambda: None if self._closing
                else self._goto_step(_PG_DEPLOY))

    # ---- step 2: locate ---------------------------------------------------------
    def _build_step_locate(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Locate the Archive"))
        self._locate_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_archive)
        rh.addWidget(browse)
        retry = QPushButton(self.tr("Try Again"))
        retry.setCursor(Qt.PointingHandCursor)
        retry.clicked.connect(self._scan_downloads)
        rh.addWidget(retry)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _scan_downloads(self):
        from Utils.wizards.archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), [self._archive_kw])
        if found:
            self._archive_path = found
            self._set_status(self._locate_status, self.tr("Found: {0}").format(found.name), ok_text())
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))
        else:
            self._archive_path = None
            self._set_status(
                self._locate_status,
                self.tr("DynDOLOD archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually."),
                err_text())

    def _browse_archive(self):
        from Utils.ui.portal import pick_file
        # Portal callback fires on a WORKER thread - marshal via Signal.
        pick_file(self.tr("Select the DynDOLOD archive"), lambda *a: safe_emit(self._picked_sig, *a))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._locate_status,
                             self.tr("Selected: {0}").format(self._archive_path.name), ok_text())
            QTimer.singleShot(
                300, lambda: None if self._closing
                else self._goto_step(_PG_EXTRACT))

    # ---- step 3: extract --------------------------------------------------------
    def _build_step_extract(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Extract DynDOLOD"))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _start_extract(self):
        archive, game = self._archive_path, self._game
        name = self._name

        def worker():
            from Utils.wizards.applications import install_application_archive, installation_target
            try:
                self._check_install()
                if archive is None or not archive.is_file():
                    raise RuntimeError(self.tr("Archive not found."))
                dest = installation_target(game, self._choice)

                safe_emit(self._extract_status_sig, self.tr("Extracting {0}…").format(archive.name), "")
                self._log(f"{name} Wizard: extracting {archive.name} → {dest}")

                self._exe, file_count = install_application_archive(
                    game, archive, self._choice, log_fn=self._log,
                    checkpoint=self._check_install, family="lod")
                self._log(f"{name} Wizard: extracted {file_count} file(s).")

                safe_emit(self._extract_status_sig,
                    self.tr("Extracted {0} file(s).").format(file_count), ok_text())
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig, self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="dyndolod-extract").start()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- step 4: deploy ---------------------------------------------------------
    def _build_step_deploy(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Deploy Modlist"))
        note = QLabel(
            self.tr("Disable or remove the previous '{0}' output mod before deploying. "
                    "The new output is imported after generation succeeds.").format(self._output_dir))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        self._replace_output = QCheckBox(self.tr("Replace the disabled output after generation succeeds"))
        lay.addWidget(self._replace_output)
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip"))
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(lambda: self._goto_step(_PG_PROTON))
        rh.addWidget(self._skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_deploy)
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _start_deploy(self):
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(self._deploy_status,
                             self.tr("Deploy is unavailable here."), err_text())
            return
        self._deploy_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        self._set_status(self._deploy_status, self.tr("Deploying…"), "")

        def _done(ok: bool):
            # Fired on the UI thread by the app's deploy completion handler.
            if self._closing:
                return
            if ok:
                self._set_status(self._deploy_status, self.tr("Deploy complete."), ok_text())
                self._goto_step(_PG_PROTON)
            else:
                self._set_status(self._deploy_status,
                                 self.tr("Deploy failed - see log."), err_text())
                self._deploy_btn.setEnabled(True)
                self._skip_btn.setEnabled(True)

        if not run_deploy(_done):
            self._set_status(self._deploy_status,
                             self.tr("Could not start deploy - see log."), err_text())
            self._deploy_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)

    # ---- step 5: proton -----------------------------------------------------------
    def _build_step_proton(self) -> QWidget:
        self._proton_holder = QWidget()
        QVBoxLayout(self._proton_holder).setContentsMargins(0, 0, 0, 0)
        return self._proton_holder

    def _enter_proton(self):
        # Built lazily on entry: the exe may only exist after the extract step.
        lay = self._proton_holder.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._exe is None:
            err = QLabel(
                self.tr('{0} was not found.\nPlease restart the wizard and install {1} first.').format(self._exe_name, self._app_dir))
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        self._proton_step = ProtonStepWidget(
            self._game, self._exe, self._exe_name, self._name,
            on_continue=self._on_proton_chosen,
            log_fn=self._log,
            title=self.tr("Step 5: Choose Proton Version"),
            show_discrete_gpu=self._tool_id in ("texgen", "dyndolod"),
            wizard_id=getattr(self._ctx, "wizard_tool_id", ""),
            wizard_label=getattr(self._ctx, "wizard_tool_label", ""),
            wizard_label_args=getattr(
                self._ctx, "wizard_tool_label_args", ()),
        )
        lay.addWidget(self._proton_step)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._prefer_discrete_gpu = bool(
            self._proton_step is not None
            and self._proton_step.prefer_discrete_gpu()
        )
        self._goto_step(_PG_RUN)

    # ---- step 6: run ----------------------------------------------------------------
    def _build_step_run(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 6: Run {0}").format(self._name))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setEnabled(False)
        self._done_btn.setStyleSheet(
            button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(self._exe_name), err_text())
            return
        self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name), "")
        name = self._name
        request = {"proton_name": self._proton_name, "prefix_mode": self._prefix_mode,
                   "prefer_discrete_gpu": self._prefer_discrete_gpu, "deploy": False,
                   "replace_output": self._replace_output.isChecked()}
        tool_id = getattr(self._ctx, "wizard_tool_id", "") or f"run_{self._tool_id}"

        def worker():
            from Utils.bethesda.lod_run import new_output_path
            from amethyst_backend.lod_run import plan_runner, run_runner
            from amethyst_backend.tools import catalogue

            try:
                profile = game._active_profile_dir
                request["output_path"] = str(new_output_path())
                revision = catalogue(game, profile)["revision"]
                plan = plan_runner(game, profile, tool_id, revision, self._choice,
                                   request, self._install_cancel)(self._log, lambda *_: None)
                run_runner(game, profile, tool_id, revision, self._choice, request,
                           plan["revision"], self._install_cancel,
                           on_started=lambda: safe_emit(self._run_started_sig))(self._log, lambda *_: None)
                self._run_complete = True
                safe_emit(self._run_status_sig, self.tr("{0} finished.").format(name), ok_text())
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                details = getattr(exc, "details", {})
                if details.get("output_retained"):
                    self._log(f"{name}: generated output retained at {details['output_path']}")
                self._log(f"{name} Wizard: launch error: {exc}")
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), err_text())
                safe_emit(self._run_failed_sig)

        threading.Thread(target=worker, daemon=True, name="dyndolod-run").start()

    def _on_run_started(self):
        self._ran = True
        self._set_status(
            self._run_status,
            self.tr('{0} is running.\nClose the tool when generation finishes; its output will then be imported.').format(self._name),
            ok_text())
        self._done_btn.setEnabled(False)

    # ---- shared -------------------------------------------------------------
    def _check_install(self):
        if self._install_cancel.is_set():
            raise InterruptedError("LOD tool installation cancelled.")

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DL_AUTO:
            self._set_status(self._dl_status,
                             self.tr("Fetching latest release from GitHub…"), "")
            self._start_auto_download()
        elif idx == _PG_LOCATE:
            self._set_status(self._locate_status,
                             self.tr("Searching Downloads folder…"), "")
            self._scan_downloads()
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status, self.tr("Extracting…"), "")
            self._start_extract()
        elif idx == _PG_PROTON:
            self._enter_proton()
        elif idx == _PG_RUN:
            self._start_run()

    def _finish(self):
        if self._closing:
            return
        self._closing = True
        self._install_cancel.set()
        do_refresh = self._run_complete and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            do_refresh()
