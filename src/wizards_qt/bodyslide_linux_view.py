"""Native-Linux BodySlide / Outfit Studio wizard.

Runs the ChrisDKN portable-tarball fork on the host instead of the Windows
build under Proton, so there is no prefix step and nothing to configure: the
fork reads BSOS_TARGET_GAME / BSOS_GAME_DATA_PATH / BSOS_OUTPUT_DATA_PATH /
BSOS_APPDIR and those win over its stored config on every launch
(Utils/bodyslide_linux).

Flow: install-or-update the build (shared across games) → deploy, with an
output-mod-name entry so builds land in the mod list → run.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import err_text, ok_text
from wizards_qt._view_base import WizardViewBase
from Utils.bodyslide_linux import TOOLS

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_INSTALL, _PG_DEPLOY, _PG_RUN = range(3)


class BodySlideLinuxView(WizardViewBase):
    """Download the Linux build, deploy, and run it against the game."""

    _inst_status_sig = Signal(str, str)
    _inst_progress_sig = Signal(int)
    _inst_latest_sig = Signal(object)     # (tag, url) | None
    _inst_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "bodyslide", **_extra):
        self._name, self._program, self._output_default = TOOLS[tool]
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("{0} (Linux) — {1}").format(
                             self._name, game.name))
        self._output_mod_name = self._output_default
        self._latest: tuple[str, str] | None = None
        self._installing = False

        self._inst_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._inst_status, t, c)))
        self._inst_progress_sig.connect(self._guard(self._on_inst_progress))
        self._inst_latest_sig.connect(self._guard(self._on_latest))
        self._inst_done_sig.connect(self._guard(self._on_install_done))

        self._stack.addWidget(self._build_install_page())
        self._stack.addWidget(self._build_bs_deploy_page())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 3: Run {0}").format(self._name)))
        self._goto_step(_PG_INSTALL)

    def _log_tool(self, msg: str) -> None:
        self._log(f"{self._name} (Linux) Wizard: {msg}")

    def _profile(self) -> str:
        return getattr(self._ctx, "profile_name", None) or "default"

    # ---- step 1: install / update -------------------------------------------------
    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Install {0} for Linux")
                                    .format(self._name))
        self._make_note(lay, self.tr(
            "A native Linux build of BodySlide and Outfit Studio, shared by "
            "every game.\n\nNo Proton prefix is used — the game, its Data "
            "folder and the output folder are passed to the tool directly."))

        self._inst_status = self._make_status(lay)
        self._inst_bar = QProgressBar()
        self._inst_bar.setRange(0, 100)
        self._inst_bar.setTextVisible(True)
        self._inst_bar.setVisible(False)
        lay.addWidget(self._inst_bar)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._inst_btn = self._orange_btn(self.tr("Download"))
        self._inst_btn.setEnabled(False)
        self._inst_btn.clicked.connect(self._start_install)
        rh.addWidget(self._inst_btn)
        self._inst_next_btn = self._accent_btn(self.tr("Next →"))
        self._inst_next_btn.setEnabled(False)
        self._inst_next_btn.clicked.connect(
            lambda: self._goto_step(_PG_DEPLOY))
        rh.addWidget(self._inst_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _enter_install(self):
        from Utils.bodyslide_linux import installed_version

        have = installed_version()
        if have:
            self._inst_next_btn.setEnabled(True)
            self._set_status(self._inst_status,
                             self.tr("Installed: {0}. Checking for updates…")
                             .format(have))
        else:
            self._set_status(self._inst_status,
                             self.tr("Not installed. Checking GitHub for the "
                                     "latest release…"))

        def worker():
            from Utils.bodyslide_linux import fetch_latest_release
            try:
                tag, url = fetch_latest_release()
                safe_emit(self._inst_latest_sig, (tag, url))
            except Exception as exc:
                self._log_tool(f"release check failed: {exc}")
                safe_emit(self._inst_latest_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="bodyslide-linux-check").start()

    def _on_latest(self, latest):
        from Utils.bodyslide_linux import installed_version

        have = installed_version()
        if latest is None:
            # Offline / API failure: an existing install still works, so only
            # block when there is nothing to fall back on.
            if have:
                self._set_status(
                    self._inst_status,
                    self.tr("Installed: {0}. Could not reach GitHub to check "
                            "for updates — see log.").format(have))
            else:
                self._set_status(
                    self._inst_status,
                    self.tr("Could not reach GitHub to fetch the release — "
                            "see log."), err_text())
            return

        tag, _url = latest
        self._latest = latest
        self._inst_btn.setEnabled(True)
        if not have:
            self._inst_btn.setText(self.tr("Download {0}").format(tag))
            self._set_status(self._inst_status,
                             self.tr("Latest release: {0}.").format(tag))
        elif have != tag:
            self._inst_btn.setText(self.tr("Update to {0}").format(tag))
            self._set_status(self._inst_status,
                             self.tr("Installed: {0} — {1} is available.")
                             .format(have, tag))
        else:
            self._inst_btn.setText(self.tr("Reinstall {0}").format(tag))
            self._set_status(self._inst_status,
                             self.tr("Installed: {0} (up to date).").format(tag),
                             ok_text())

    def _start_install(self):
        if self._latest is None or self._installing:
            return
        self._installing = True
        tag, url = self._latest
        self._inst_btn.setEnabled(False)
        self._inst_next_btn.setEnabled(False)
        self._inst_bar.setValue(0)
        self._inst_bar.setVisible(True)
        self._set_status(self._inst_status,
                         self.tr("Downloading {0}…").format(tag))

        def worker():
            from Utils.bodyslide_linux import install_release
            last = [-1]

            def hook(block_num, block_size, total_size):
                if total_size <= 0:
                    return
                pct = min(100, int(block_num * block_size * 100 / total_size))
                if pct == last[0]:
                    return
                last[0] = pct
                safe_emit(self._inst_progress_sig, pct)
                if pct == 100:
                    # Unpacking a ~170 MB tree takes a beat; without this the
                    # bar sits full and the wizard looks hung.
                    safe_emit(self._inst_status_sig, self.tr("Extracting…"), "")

            try:
                self._log_tool(f"downloading {tag} from {url}")
                install_release(url, tag, reporthook=hook,
                                log_fn=self._log_tool)
                safe_emit(self._inst_progress_sig, 100)
                safe_emit(self._inst_status_sig,
                          self.tr("Installed {0}.").format(tag), ok_text())
                safe_emit(self._inst_done_sig, True)
            except Exception as exc:
                self._log_tool(f"download error: {exc}")
                safe_emit(self._inst_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                safe_emit(self._inst_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="bodyslide-linux-dl").start()

    def _on_inst_progress(self, pct: int):
        self._inst_bar.setValue(pct)

    def _on_install_done(self, ok: bool):
        from Utils.bodyslide_linux import is_installed

        self._installing = False
        self._inst_bar.setVisible(False)
        self._inst_btn.setEnabled(True)
        if ok:
            self._goto_step(_PG_DEPLOY)
        else:
            # A failed update leaves the previous install intact — let the
            # user carry on with it rather than trapping them on this page.
            self._inst_next_btn.setEnabled(is_installed())

    # ---- step 2: deploy -----------------------------------------------------------
    def _build_bs_deploy_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Deploy Modlist"))
        self._make_note(lay, self.tr(
            "{0} reads its sliders and shapes from the deployed Data folder, "
            "so deploy your modlist first.\n\nBuilt meshes are written to the "
            "output mod below, which is added to your mod list."
        ).format(self._name))

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 4, 0, 4); rh.setSpacing(8)
        rh.addStretch(1)
        lbl = QLabel(self.tr("Output mod name:"))
        lbl.setStyleSheet(self._dim)
        rh.addWidget(lbl)
        self._output_name_entry = QLineEdit()
        self._output_name_entry.setPlaceholderText(self._output_default)
        self._output_name_entry.setMinimumWidth(220)
        rh.addWidget(self._output_name_entry)
        rh.addStretch(1)
        lay.addWidget(row)

        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        brow = QWidget()
        bh = QHBoxLayout(brow); bh.setContentsMargins(0, 8, 0, 0); bh.setSpacing(8)
        bh.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(self._skip_deploy)
        bh.addWidget(self._deploy_skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_bs_deploy)
        bh.addWidget(self._deploy_btn)
        bh.addStretch(1)
        lay.addWidget(brow)
        return page

    def _capture_output_mod_name(self):
        from Utils.bodyslide_tools import ensure_output_mod, sanitize_output_name

        self._output_mod_name = sanitize_output_name(
            self._output_name_entry.text(), self._output_default)
        # Create + enable the mod before the deploy so a build that happens
        # this session already has a registered home in the load order.
        try:
            ensure_output_mod(self._game, self._profile(),
                              self._output_mod_name)
            self._ran = True       # modlist gained a mod — refresh on close
        except OSError as exc:
            self._log_tool(f"could not create '{self._output_mod_name}': {exc}")

    def _skip_deploy(self):
        self._capture_output_mod_name()
        self._goto_step(_PG_RUN)

    def _start_bs_deploy(self):
        self._capture_output_mod_name()
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status,
                                    lambda: self._goto_step(_PG_RUN),
                                    _re_enable):
            _re_enable()

    # ---- step 3: run --------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_INSTALL:
            self._enter_install()
        elif idx == _PG_RUN:
            self._set_status(self._run_status,
                             self.tr("Launching {0}…").format(self._name))
            self._start_run()

    def _start_run(self):
        from Utils.bodyslide_linux import is_installed

        if not is_installed():
            self._set_status(
                self._run_status,
                self.tr("{0} is not installed.\n\nGo back and download it "
                        "first.").format(self._name), err_text())
            return

        game, name, program = self._game, self._name, self._program
        profile, output_mod_name = self._profile(), self._output_mod_name
        output_dir = game.get_effective_mod_staging_path() / output_mod_name

        def worker():
            from Utils.bodyslide_linux import build_env, run_logged
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                env = build_env(game, profile, output_dir,
                                log_fn=self._log_tool)
                self._warn_if_no_slider_data(game)
                self._log_tool(f"game data: {env.get('BSOS_GAME_DATA_PATH')}")
                self._log_tool(f"output:    {env.get('BSOS_OUTPUT_DATA_PATH')}")
                self._log_tool(f"target:    {env.get('BSOS_TARGET_GAME')}")

                safe_emit(self._run_status_sig,
                          self.tr("{0} is running.\nClose it when you are "
                                  "done, then click Done.").format(name), "")
                safe_emit(self._run_started_sig)
                run_logged(program, env, log_fn=self._log_tool, label=name)
                self._log_tool(f"{name} closed.")
                safe_emit(self._run_status_sig,
                          self.tr("{0} finished.").format(name), ok_text())
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), err_text())
                self._log_tool(f"launch error: {exc}")

        threading.Thread(target=worker, daemon=True,
                         name="bodyslide-linux-run").start()

    def _warn_if_no_slider_data(self, game):
        """Log when the deployed slider data isn't where the tool looks.

        The fork resolves its project folder to <GameData>/CalienteTools/
        BodySlide or <GameData>/Tools/BodySlide and there is no environment
        override for it, so slider data deployed anywhere else shows up as an
        empty outfit list with no other clue.
        """
        from Utils.bodyslide_tools import slider_data_root

        data_path = game.get_mod_data_path()
        if data_path is None or not data_path.is_dir():
            self._log_tool("no deployed Data folder — deploy your modlist "
                           "before building.")
            return
        found = slider_data_root(data_path)
        if found is None:
            self._log_tool("no SliderSets folder in the deployed Data folder "
                           "— the outfit list will be empty until a BodySlide "
                           "mod (CBBE, BHUNP, …) is deployed.")
            return
        try:
            rel = found.relative_to(data_path).as_posix().lower()
        except ValueError:
            rel = ""
        if rel not in ("calientetools/bodyslide", "tools/bodyslide"):
            self._log_tool(
                f"WARNING: slider data is at {found}, but the tool only looks "
                "in <Data>/CalienteTools/BodySlide and <Data>/Tools/BodySlide. "
                "The outfit list will be empty.")

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
