"""xEdit wizard (SSEEdit / FO4Edit / FNVEdit / … + QuickAutoClean) — Qt port
of wizards/sseedit.py.

One parametrised view serves every Bethesda game via the WizardTool.extra
kwargs (xedit_exe / nexus_url / app_dir / display_name), with SSEEdit
defaults for Skyrim SE's plain registrations; ``qac=True`` runs the
QuickAutoClean build instead and shows the LOOT dirty-plugins list on the
run page.

Steps (plugins-panel-scoped tab; 1-3 are skipped when the exe is already
extracted under Profiles/<game>/Applications/<app_dir>/):
  1. Open the Nexus download page (manual download only).
  2. Locate the archive in ~/Downloads (Try Again / Browse).
  3. Extract to Applications/<app_dir>/ and flatten wrapper dirs.
  4. Deploy the modlist (auto-starts through QtWizardContext.run_deploy).
  5. Choose Proton version + prefix placement (shared ProtonStepWidget).
  6. Run <xEdit>.exe via Proton with -d:<game>/Data after prefix prep
     (registry seed, plugins.txt + My Games links, viewsettings seed, WinXP
     compat flag).  When the tool exits: wineserver shutdown → finalize
     pending ``<plugin>.save.<timestamp>`` renames → restore_after_xedit
     (full un-deploy so edited plugins land back in their mod folders) →
     close + modlist refresh.

All blocking work happens on daemon threads with Signals marshalling back to
the UI thread; the portal file picker's callback also fires on a worker
thread and is marshalled via Signal.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStackedWidget,
)

from gui_qt.theme_qt import active_palette, _c, button_qss, ok_text, err_text
from gui_qt.safe_emit import safe_emit
from Utils.xedit_tools import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files&file_id=495506"
_NEXUS_FILE_ID = 495506       # SSEEdit main file, pairs with _NEXUS_URL
_EXE_NAME = "SSEEdit.exe"

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(6)


class XEditView(QWidget):
    """Install (if needed), deploy and run an xEdit build for a Bethesda game."""

    _locate_status_sig = Signal(str, str)
    _extract_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _picked_sig = Signal(object)          # portal picker → UI thread
    _extract_done_sig = Signal(bool)
    _run_started_sig = Signal()           # xEdit launched → lock close
    _run_finished_sig = Signal()          # xEdit exited + restore done → close
    _run_error_sig = Signal()             # launch/run failed → unlock close
    _qac_all_started_sig = Signal()       # QAC-All loop began → lock close + mark ran
    _qac_all_aborted_sig = Signal()       # QAC-All bailed pre-launch → re-offer chooser
    _auto_dl_status_sig = Signal(str, str)   # auto-fetch → download-page status
    _auto_dl_gate_sig = Signal(bool)         # auto-fetch → manual Next enable
    _auto_dl_archive_sig = Signal(object)    # auto-fetch → finished archive

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, xedit_exe: str | None = None, nexus_url: str | None = None,
                 nexus_file_id: int | None = None,
                 app_dir: str | None = None, display_name: str | None = None,
                 qac: bool = False, discord: bool = False,
                 discord_mode: str | None = None, **_extra):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._qac = qac
        self._discord = discord
        # Game-mode arg the multi-game Discord launcher needs (e.g. "SSE",
        # "FO4", "SF1") — it refuses to start without one.
        self._discord_mode = discord_mode

        # Resolve per-game config from kwargs, falling back to SSEEdit
        # defaults (mirrors the Tk wizard's __init__).
        base_exe = xedit_exe or _EXE_NAME
        # The Discord build has no separate QuickAutoClean exe — QAC
        # is the same launcher with a ``-quickautoclean`` command-line arg (added
        # in _start_run).  The Nexus builds ship a distinct <build>QuickAutoClean.exe.
        if qac and not discord and base_exe.lower().endswith(".exe"):
            self._exe_name = base_exe[: -len(".exe")] + "QuickAutoClean.exe"
        else:
            self._exe_name = base_exe
        self._nexus_url = nexus_url or _NEXUS_URL
        # The pinned file id must pair with the URL it belongs to — only fall
        # back to the SSEEdit default when the URL is also the default. The
        # QAC build ships inside the same archive, so it shares the id;
        # Discord builds have no Nexus file at all.
        if nexus_file_id is None and nexus_url is None:
            nexus_file_id = _NEXUS_FILE_ID
        self._nexus_file_id = 0 if discord else int(nexus_file_id or 0)
        self._app_dir = app_dir or base_exe.removesuffix(".exe")
        short = display_name or base_exe.removesuffix(".exe")
        self._xedit_name = short  # build name w/o QAC, e.g. "FO4Edit"
        self._name = short + (" QAC" if qac else "")
        # Discord archives are named "xEdit <version>.7z" regardless of the
        # launcher inside, so match on the generic "xedit" keyword.
        self._archive_keyword = "xedit" if discord else self._xedit_name.lower()

        self._exe = tool_exe_path(game, self._exe_name, self._app_dir)
        self._archive_path: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""
        self._ran = False
        self._tool_running = False
        self._closing = False
        self._auto_fetch_cancel = threading.Event()
        self._auto_fetch_started = False
        self._dl_status = None
        self._dl_next_btn = None

        def _guard(fn):
            return lambda *a: None if self._closing else fn(*a)

        self._locate_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._locate_status, t, c)))
        self._extract_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._extract_status, t, c)))
        self._run_status_sig.connect(
            _guard(lambda t, c: self._set_status(self._run_status, t, c)))
        self._picked_sig.connect(_guard(self._on_picked))
        self._extract_done_sig.connect(_guard(self._on_extract_done))
        self._run_started_sig.connect(_guard(self._on_run_started))
        self._run_finished_sig.connect(_guard(self._on_run_finished))
        self._run_error_sig.connect(_guard(self._on_run_error))
        self._qac_all_started_sig.connect(_guard(self._on_qac_all_started))
        self._qac_all_aborted_sig.connect(_guard(self._on_qac_all_aborted))
        self._auto_dl_status_sig.connect(_guard(self._on_auto_dl_status))
        self._auto_dl_gate_sig.connect(_guard(self._on_auto_dl_gate))
        self._auto_dl_archive_sig.connect(_guard(self._on_auto_dl_archive))

        self.setObjectName("XEditView")
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
        title = QLabel(self.tr("Run {0} — {1}").format(self._name, self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            button_qss("BTN_DANGER", padding="5px 12px"))
        close.clicked.connect(self._finish)
        self._close_btn = close
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        self._stack.addWidget(self._build_step_download())  # 0
        self._stack.addWidget(self._build_step_locate())    # 1
        self._stack.addWidget(self._build_step_extract())   # 2
        self._stack.addWidget(self._build_step_deploy())    # 3
        self._stack.addWidget(self._build_step_proton())    # 4
        self._stack.addWidget(self._build_step_run())       # 5

        # Already extracted → straight to the deploy step (Tk behaviour).
        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._start_auto_fetch()

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

    # ---- step 1: download -----------------------------------------------------
    def _build_step_download(self) -> QWidget:
        if self._discord:
            return self._build_step_download_discord()
        page, lay = self._step_page(self.tr("Step 1: Download {0}").format(self._xedit_name))
        note = QLabel(
            self.tr('Click the button below to open the {0} page on Nexus Mods.\n\nDownload the archive manually (do NOT use the Mod Manager download button), then click Next.').format(self._xedit_name))
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
        lay.addSpacing(8)
        self._dl_status = self._make_status(lay)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_LOCATE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        self._dl_next_btn = nxt
        return page

    def _build_step_download_discord(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download xEdit (Discord version)"))
        note = QLabel(self.tr(
            'The latest official xEdit is now released through the xEdit '
            'Discord — a single multi-game download that is NOT on Nexus Mods.\n\n'
            'To get it:\n'
            '  1. Find and join the xEdit Discord server (search for it '
            'yourself — we do not link it here as the invite can change).\n'
            '  2. Download the latest xEdit archive (e.g. "xEdit 4.1.5q.7z") '
            'from the #xedit-builds channel.\n'
            '  3. Leave the archive in your Downloads folder and click Next.\n\n'
            'It contains xFOEdit (Fallout), xSFEdit (Starfield) and xTESEdit '
            '(Elder Scrolls); the wizard runs the one for this game '
            'automatically.'))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)

        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_LOCATE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _open_download_page(self):
        from Utils.xdg import open_url
        open_url(self._nexus_url)

    # ---- hands-free archive fetch (premium download / folder watch) --------------
    def _start_auto_fetch(self):
        if (self._auto_fetch_started or self._closing
                or not self._nexus_file_id):
            return
        api = None
        api_fn = getattr(self._ctx, "nexus_api", None)
        if api_fn is not None:
            try:
                api = api_fn()      # GUI thread — the app's shared resolver
            except Exception:
                api = None
        from wizards_qt._view_base import arm_nexus_auto_fetch
        self._auto_fetch_started = arm_nexus_auto_fetch(
            api=api, url=self._nexus_url, file_id=self._nexus_file_id,
            keywords=[self._archive_keyword], label=self._xedit_name,
            cancel=self._auto_fetch_cancel,
            status_cb=lambda t, c: safe_emit(self._auto_dl_status_sig, t, c),
            gate_cb=lambda on: safe_emit(self._auto_dl_gate_sig, on),
            archive_cb=lambda p: safe_emit(self._auto_dl_archive_sig, p),
            log_fn=lambda m: self._log(f"{self._name} Wizard: {m}"))

    def _on_auto_dl_status(self, text: str, color: str):
        if self._dl_status is not None:
            self._set_status(self._dl_status, text, color)

    def _on_auto_dl_gate(self, enabled: bool):
        if self._dl_next_btn is not None:
            self._dl_next_btn.setEnabled(enabled)

    def _on_auto_dl_archive(self, path):
        self._on_auto_dl_gate(True)
        path = Path(path)
        # Only auto-advance while still on the download/locate steps — a user
        # who already located an archive manually keeps their choice.
        if self._stack.currentIndex() not in (_PG_DOWNLOAD, _PG_LOCATE):
            return
        if not path.is_file():
            return
        self._archive_path = path
        if self._dl_status is not None:
            self._set_status(self._dl_status,
                             self.tr("Downloaded: {0}").format(path.name),
                             ok_text())
        self._goto_step(_PG_EXTRACT)

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
        from Utils.wizard_archives import find_archive, get_downloads_dir
        found = find_archive(get_downloads_dir(), [self._archive_keyword])
        if found:
            self._archive_path = found
            self._set_status(self._locate_status, self.tr("Found: {0}").format(found.name), ok_text())
            # Fire only while still on the locate page — the hands-free fetch
            # may have advanced the wizard while this timer was pending.
            QTimer.singleShot(
                300, lambda: None
                if self._closing or self._stack.currentIndex() != _PG_LOCATE
                else self._goto_step(_PG_EXTRACT))
        else:
            self._archive_path = None
            self._set_status(
                self._locate_status,
                self.tr('{0} archive not found in Downloads.\nMake sure you downloaded it, then press Try Again,\nor use Browse to select it manually.').format(self._xedit_name),
                err_text())

    def _browse_archive(self):
        from Utils.portal_filechooser import pick_file
        # Portal callback fires on a WORKER thread — marshal via Signal.
        pick_file(self.tr("Select the {0} archive").format(self._xedit_name),
                  lambda *a: safe_emit(self._picked_sig, *a))

    def _on_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._locate_status,
                             self.tr("Selected: {0}").format(self._archive_path.name), ok_text())
            QTimer.singleShot(
                300, lambda: None
                if self._closing or self._stack.currentIndex() != _PG_LOCATE
                else self._goto_step(_PG_EXTRACT))

    # ---- step 3: extract --------------------------------------------------------
    def _build_step_extract(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Extract {0}").format(self._xedit_name))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        return page

    def _start_extract(self):
        archive, game = self._archive_path, self._game
        exe_name, app_dir = self._exe_name, self._app_dir

        def worker():
            from Utils.wizard_archives import extract_archive
            from Utils.xedit_tools import applications_dir, flatten_subdirs
            try:
                if archive is None or not archive.is_file():
                    raise RuntimeError(self.tr("Archive not found."))
                dest = applications_dir(game, app_dir)
                dest.mkdir(parents=True, exist_ok=True)

                safe_emit(self._extract_status_sig,
                          self.tr("Extracting {0}…").format(archive.name), "")
                self._log(f"{self._name} Wizard: extracting {archive.name} → {dest}")

                paths = extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"{self._name} Wizard: extracted {file_count} file(s).")

                flatten_subdirs(dest, exe_name)

                exe = dest / exe_name
                if not exe.is_file():
                    raise RuntimeError(self.tr(
                        "{0} not found after extraction.\n"
                        "Check that the archive contains {0}.").format(exe_name))
                self._exe = exe

                safe_emit(self._extract_status_sig,
                    self.tr("Extracted {0} file(s).").format(file_count),
                    ok_text())
                safe_emit(self._extract_done_sig, True)
            except Exception as exc:
                safe_emit(self._extract_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                self._log(f"{self._name} Wizard: extract error: {exc}")
                safe_emit(self._extract_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="xedit-extract").start()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- step 4: deploy ---------------------------------------------------------
    def _build_step_deploy(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Deploy Modlist"))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip"))
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(lambda: self._goto_step(_PG_PROTON))
        lay.addWidget(self._skip_btn, 0, Qt.AlignHCenter)
        return page

    def _start_deploy(self):
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            self._set_status(self._deploy_status,
                             self.tr("Deploy is unavailable here — Skip to continue."),
                             err_text())
            return
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
                                 self.tr("Deploy failed — see log."), err_text())

        if not run_deploy(_done):
            self._set_status(self._deploy_status,
                             self.tr("Could not start deploy — see log."), err_text())

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
                self.tr('{0} was not found.\nPlease restart the wizard and install {1} first.').format(self._exe_name, self._xedit_name))
            err.setAlignment(Qt.AlignCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            lay.addWidget(err)
            return
        from wizards_qt.proton_step import ProtonStepWidget
        lay.addWidget(ProtonStepWidget(
            self._game, self._exe, self._exe_name, self._name,
            on_continue=self._on_proton_chosen,
            log_fn=self._log,
            title=self.tr("Step 5: Choose Proton Version"),
            show_launch_args=True,
        ))

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- step 6: run ----------------------------------------------------------------
    def _build_step_run(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 6: Run {0}").format(self._name))
        self._run_page_lay = lay
        self._dirty_box_added = False
        lay.addStretch(1)
        self._run_status = self._make_status(lay)

        self._qac_choice_row = QWidget()
        crow = QHBoxLayout(self._qac_choice_row)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(8)
        self._qac_launch_btn = self._accent_btn(self.tr("Launch {0}").format(self._name))
        self._qac_launch_btn.clicked.connect(self._launch_qac_interactive)
        crow.addWidget(self._qac_launch_btn)
        self._qac_all_btn = QPushButton(self.tr("QAC All"))
        self._qac_all_btn.setCursor(Qt.PointingHandCursor)
        self._qac_all_btn.setStyleSheet(button_qss("BTN_SUCCESS"))
        self._qac_all_btn.clicked.connect(self._start_qac_all)
        crow.addWidget(self._qac_all_btn)
        self._qac_choice_row.setVisible(False)
        lay.addWidget(self._qac_choice_row, 0, Qt.AlignHCenter)
        return page

    def _build_dirty_plugins_panel(self):
        """QAC only: list the LOOT-flagged dirty plugins above the run status
        so the user can see what needs cleaning without closing the wizard."""
        from Utils.xedit_tools import collect_dirty_plugins
        dirty = collect_dirty_plugins(self._game)
        if not dirty or self._dirty_box_added:
            return
        self._dirty_box_added = True
        p = active_palette()

        head = QLabel(self.tr("Plugins needing cleaning ({0}):").format(len(dirty)))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")

        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(4, 2, 4, 2)
        iv.setSpacing(2)
        for name, summary in dirty:
            nm = QLabel(name)
            nm.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            iv.addWidget(nm)
            sm = QLabel(summary)
            sm.setStyleSheet(self._dim)
            sm.setContentsMargins(12, 0, 0, 4)
            sm.setWordWrap(True)
            iv.addWidget(sm)
        iv.addStretch(1)

        box = QScrollArea()
        box.setWidgetResizable(True)
        box.setWidget(inner)
        box.setFrameShape(QScrollArea.NoFrame)

        # Drop the placeholder stretch so it doesn't split the free vertical
        # space with the box — then the box (stretch 1) gets the full height.
        for i in range(self._run_page_lay.count()):
            item = self._run_page_lay.itemAt(i)
            if item is not None and item.widget() is None and item.spacerItem() is not None:
                self._run_page_lay.takeAt(i)
                break

        # Insert the header + list just under the step title (index 0), above
        # the run status, so the list absorbs the free height.
        self._run_page_lay.insertWidget(1, head)
        self._run_page_lay.insertWidget(2, box, 1)

    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(self._exe_name), err_text())
            return
        self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name), "")
        name = self._name
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import (
                load_tool_launch_args, parse_launch_args, resolve_tool_prefix,
                run_tool_logged, shutdown_prefix_wineserver,
            )
            from Utils.wine_paths import to_wine_path
            from Utils.xedit_tools import (
                finalize_xedit_saves, prepare_xedit_prefix, restore_after_xedit,
            )
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                        self.tr("Could not find Proton '{0}' — "
                        "check that it is installed in Steam.").format(
                            proton_name), err_text())
                    return
                proton_script, compat_data, env = result

                game_path = game.get_game_path()
                if game_path is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Game path not configured."), err_text())
                    return

                pfx = compat_data / "pfx"
                data_arg = f'-d:{to_wine_path(game_path / "Data", pfx)}'
                extra_args = [data_arg]
                # The Discord build's QuickAutoClean is the same launcher with a
                # -quickautoclean switch (the Nexus builds ship a separate exe).
                if self._qac and self._discord:
                    extra_args.insert(0, "-quickautoclean")
                # The multi-game Discord launcher needs a game-mode arg (e.g.
                # -SSE / -FO4 / -SF1) to pick the game — it errors out without it.
                if self._discord and self._discord_mode:
                    extra_args.insert(0, f"-{self._discord_mode}")
                # User-supplied extra launch arguments (Step 5 field) go last.
                user_args = parse_launch_args(load_tool_launch_args(exe))
                if user_args:
                    extra_args.extend(user_args)

                # Registry seed + plugins.txt / My Games links + viewsettings
                # seed + WinXP compat flag (see Utils.xedit_tools).
                #
                # The viewsettings filename xEdit reads is keyed to the GAME
                # mode, not the launcher exe.  The Nexus builds are per-game so
                # their build name already encodes it (SSEEdit -> Plugins.sse…),
                # but the multi-game Discord launcher (xTESEdit/xFOEdit/xSFEdit)
                # picks the mode from its -<MODE> arg, so seed the file for that
                # mode instead of the generic "xEdit" display name (which would
                # write a useless Plugins.xviewsettings and leave the first-run
                # popups un-suppressed).  Feed a synthetic "<mode>Edit" name so
                # xedit_settings_ext yields the right extension (SSE -> sse,
                # FO4 -> fo4, TES5 -> tes5, SF1 -> sf1, Enderal -> enderal, …).
                seed_name = self._xedit_name
                if self._discord and self._discord_mode:
                    seed_name = f"{self._discord_mode}Edit"
                prepare_xedit_prefix(
                    game, compat_data, proton_script, env,
                    xedit_name=seed_name, exe=exe, log_fn=_wlog)

                self._log(f"{name} Wizard: launching {exe} via Proton with "
                          f"{' '.join(extra_args)}")
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                extra_args=extra_args, label=name)

                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)

                # xEdit can write the cleaned plugin to a temp and queue the
                # rename "on shutdown"; the wineserver is down now, so
                # finalise any temp that slipped through before the restore.
                n = finalize_xedit_saves(game_path / "Data", log_fn=_wlog)
                if n:
                    self._log(f"{name} Wizard: finalised {n} pending xEdit save(s).")

                # Move any edited plugin back into its mod folder while the
                # modindex still knows it — BEFORE the close-refresh rescans
                # staging (the core QAC-plugins-land-in-overwrite fix).
                restore_after_xedit(game, name, log_fn=self._log)
                self._log(f"{name} Wizard: {name} closed.")

                safe_emit(self._run_status_sig,
                          self.tr("{0} finished.").format(name), ok_text())
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: launch error: {exc}")
                safe_emit(self._run_error_sig)

        threading.Thread(target=worker, daemon=True, name="xedit-run").start()

    def _on_run_started(self):
        self._ran = True
        self._tool_running = True
        self._close_btn.setEnabled(False)
        self._close_btn.setToolTip(
            self.tr("{0} is running — close it to continue.").format(self._name))
        self._set_status(
            self._run_status,
            self.tr('{0} is running.\nWhen you close it, your changes are restored automatically.').format(self._name),
            ok_text())

    def _on_run_finished(self):
        # xEdit exited and restore_after_xedit already ran on the worker —
        # unlock and close/refresh without any user action.
        self._tool_running = False
        self._finish()

    def _on_run_error(self):
        # Launch/run failed after the close lock engaged — unlock so the user
        # can read the error status and close the wizard manually.
        self._tool_running = False
        self._close_btn.setEnabled(True)
        self._close_btn.setToolTip("")

    def tab_close_blocked(self) -> bool:
        """Veto hook for the tab bar's ✕: no closing while xEdit runs."""
        return self._tool_running

    # ---- QAC batch clean ----------------------------------------------------
    def _enter_qac_run_choices(self):
        """QAC run page: let the user launch the tool interactively (pick a
        plugin each time) or clean every dirty plugin in one pass. Nothing
        launches until they choose."""
        from Utils.xedit_tools import collect_dirty_plugins
        dirty = collect_dirty_plugins(self._game)
        n = len(dirty)
        if n:
            self._qac_all_btn.setText(self.tr("QAC All ({0})").format(n))
            self._qac_all_btn.setEnabled(True)
            self._set_status(
                self._run_status,
                self.tr('Launch {0} to clean plugins one at a time, or QAC All '
                        'to clean all {1} flagged plugin(s) automatically.').format(
                            self._name, n),
                "")
        else:
            # No LOOT-flagged dirty plugins — QAC All has nothing to do, so only
            # offer the interactive launch (LOOT may simply never have run).
            self._qac_all_btn.setEnabled(False)
            self._set_status(
                self._run_status,
                self.tr('No plugins are flagged as dirty. Launch {0} to inspect '
                        'or clean manually.').format(self._name),
                "")
        self._qac_launch_btn.setEnabled(True)
        self._qac_choice_row.setVisible(True)

    def _launch_qac_interactive(self):
        # User picked the manual flow: hide the chooser and run the tool the
        # classic way (module-selection dialog, one plugin at a time).
        self._qac_choice_row.setVisible(False)
        self._start_run()

    def _start_qac_all(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(self._exe_name), err_text())
            return
        from Utils.xedit_tools import collect_dirty_plugins
        plugins = [name for name, _ in collect_dirty_plugins(game)]
        if not plugins:
            self._set_status(self._run_status,
                             self.tr("No plugins need cleaning."), ok_text())
            return

        # Lock the chooser so the batch can't be double-started or the
        # interactive launch fired mid-run (both drive the same prefix).
        self._qac_launch_btn.setEnabled(False)
        self._qac_all_btn.setEnabled(False)
        self._qac_choice_row.setVisible(False)
        self._set_status(self._run_status,
                         self.tr("Preparing to clean {0} plugin(s)…").format(len(plugins)), "")

        name = self._name
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import (
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )
            from Utils.wine_paths import to_wine_path
            from Utils.xedit_tools import (
                finalize_xedit_saves, prepare_xedit_prefix, restore_after_xedit,
            )
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                        self.tr("Could not find Proton '{0}' — "
                        "check that it is installed in Steam.").format(
                            proton_name), err_text())
                    safe_emit(self._qac_all_aborted_sig)
                    return
                proton_script, compat_data, env = result

                game_path = game.get_game_path()
                if game_path is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Game path not configured."), err_text())
                    safe_emit(self._qac_all_aborted_sig)
                    return

                pfx = compat_data / "pfx"
                data_arg = f'-d:{to_wine_path(game_path / "Data", pfx)}'
                # QuickAutoClean CLI: -autoload skips the module-selection
                # dialog, -autoexit closes the tool when the clean finishes, and
                # the plugin filename (positional) is the one to clean. The QAC
                # exe already forces IKnowWhatImDoing + autosave, so no prompt.
                base_args = [data_arg, "-autoload", "-autoexit"]
                # The Discord build's QuickAutoClean is the same launcher with a
                # -quickautoclean switch (the Nexus builds ship a separate exe).
                if self._qac and self._discord:
                    base_args.insert(0, "-quickautoclean")
                # The multi-game Discord launcher needs a game-mode arg to pick
                # the game (e.g. -SSE / -FO4 / -SF1) or it errors out.
                if self._discord and self._discord_mode:
                    base_args.insert(0, f"-{self._discord_mode}")

                seed_name = self._xedit_name
                if self._discord and self._discord_mode:
                    seed_name = f"{self._discord_mode}Edit"
                prepare_xedit_prefix(
                    game, compat_data, proton_script, env,
                    xedit_name=seed_name, exe=exe, log_fn=_wlog)

                safe_emit(self._qac_all_started_sig)
                total = len(plugins)
                data_dir = game_path / "Data"
                cleaned = 0
                for i, plugin in enumerate(plugins, 1):
                    if self._closing:
                        break
                    safe_emit(self._run_status_sig,
                              self.tr("Cleaning {0} of {1}: {2}…").format(
                                  i, total, plugin), "")
                    self._log(f"{name} Wizard: QAC All — cleaning {plugin} "
                              f"({i}/{total})")
                    run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                    extra_args=base_args + [plugin],
                                    label=f"{name} [{plugin}]")
                    # Finalise this plugin's <name>.save.<ts> temp before the
                    # next launch reloads Data/ (QAC queues the rename to run on
                    # shutdown, but we relaunch into the same prefix).
                    finalize_xedit_saves(data_dir, log_fn=_wlog)
                    cleaned += 1

                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)
                # Catch any straggler temp the per-plugin pass missed.
                finalize_xedit_saves(data_dir, log_fn=_wlog)

                # Un-deploy so every cleaned plugin lands back in its mod folder
                # before the close-refresh rescans staging (same fix the
                # interactive flow relies on).
                restore_after_xedit(game, name, log_fn=self._log)
                self._log(f"{name} Wizard: QAC All cleaned {cleaned} plugin(s).")

                safe_emit(self._run_status_sig,
                          self.tr("QAC All finished — cleaned {0} plugin(s).").format(
                              cleaned), ok_text())
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("QAC All error: {0}").format(exc), err_text())
                self._log(f"{name} Wizard: QAC All error: {exc}")
                safe_emit(self._run_error_sig)

        threading.Thread(target=worker, daemon=True, name="xedit-qac-all").start()

    def _on_qac_all_started(self):
        # The batch drives the same prefix as the interactive run, so it takes
        # the same close lock: closing mid-batch would rescan staging before
        # restore_after_xedit. _run_finished_sig unlocks and auto-closes.
        self._ran = True
        self._tool_running = True
        self._close_btn.setEnabled(False)
        self._close_btn.setToolTip(
            self.tr("{0} is cleaning plugins — please wait.").format(self._name))

    def _on_qac_all_aborted(self):
        # The batch bailed before launching anything (no Proton / no game path),
        # so nothing was cleaned and no lock was taken — put the chooser back
        # rather than stranding the user on a hidden row. The error status the
        # worker already emitted stays on screen.
        self._qac_launch_btn.setEnabled(True)
        self._qac_all_btn.setEnabled(True)
        self._qac_choice_row.setVisible(True)

    # ---- shared -------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._set_status(self._locate_status,
                             self.tr("Searching Downloads folder…"), "")
            self._scan_downloads()
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status, self.tr("Extracting…"), "")
            self._start_extract()
        elif idx == _PG_DEPLOY:
            self._start_deploy()
        elif idx == _PG_PROTON:
            self._enter_proton()
        elif idx == _PG_RUN:
            if self._qac:
                self._build_dirty_plugins_panel()
                self._enter_qac_run_choices()
            else:
                self._start_run()

    def _finish(self):
        # ✕ and the auto-close after the tool exits both land here.
        # Idempotent; in-flight daemon workers finish harmlessly (their late
        # signals are dropped by the _closing guards). While xEdit runs the ✕
        # is disabled, but guard anyway (e.g. programmatic close attempts) —
        # closing mid-run would rescan staging before restore_after_xedit.
        if self._closing or self._tool_running:
            return
        self._closing = True
        self._auto_fetch_cancel.set()
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        do_refresh_plugins = self._ran and getattr(
            self._ctx, "refresh_plugins", None)
        self._on_close_cb()
        if do_refresh:
            # The post-run restore un-deployed the game and may have moved
            # cleaned plugins back into their mod folders — re-sync + reload
            # panels (mirrors the Tk wizard's _reload_mod_panel; the Qt
            # refresh path also rescans the mod index, which the QAC flow
            # depends on).
            do_refresh()
        if do_refresh_plugins:
            do_refresh_plugins()
