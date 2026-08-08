"""BodySlide / Outfit Studio wizard - Qt port of wizards/bodyslide.py.

Both tools are installed as regular mods (the WizardTool only registers when
the exe is staged); the deployed copy runs from the game's Data folder.
Flow: deploy (with an output-mod-name entry; the output-capture mod +
Config.xml redirect are applied BEFORE the deploy so the mod lands in the
filemap - the Tk version did this via run_deploy_pipeline's on_pre_filemap
hook) → Proton (prefix anchored to the STAGED exe, never inside Data) → run
the deployed exe with the Xalia helper disabled (wxWidgets crash) and the
Bethesda registry key seeded.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bodyslide_tools import sanitize_output_name
from Utils.wizard_gates import find_staged_exe

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Prefer the "x64" exe when present: that is the 5.7.x build, whose preview
# panel renders correctly under Proton. 5.8+ dropped the x64 suffix and its
# preview renders black on Wine/Mesa, so it is only the fallback.
_TOOLS = {
    "bodyslide":    ("BodySlide",
                     ("BodySlide x64.exe", "BodySlide.exe"),
                     "BodySlide_files"),
    "outfitstudio": ("Outfit Studio",
                     ("OutfitStudio x64.exe", "OutfitStudio.exe"),
                     "OutfitStudio_files"),
}

_PG_DEPLOY, _PG_PROTON, _PG_RUN = range(3)


class BodySlideView(WizardViewBase):
    """Deploy mods and run BodySlide / Outfit Studio from the Data folder."""

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "bodyslide", **_extra):
        self._name, self._exe_names, self._output_default = _TOOLS[tool]
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("{0} - {1}").format(self._name, game.name))
        # The prefix is anchored to the staged exe (prefix_* dirs in staging
        # are excluded from filemap scans); the deployed copy is what runs.
        self._exe = find_staged_exe(game, self._exe_names)
        self._output_mod_name = self._output_default
        self._proton_name = ""
        self._prefix_mode = ""

        self._stack.addWidget(self._build_bs_deploy_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 3: Run {0}").format(self._name)))
        self._goto_step(_PG_DEPLOY)

    def _build_bs_deploy_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Deploy Modlist"))
        self._make_note(lay, (
            self.tr('{0} must be run from the deployed Data folder.\n\nDeploy your modlist first, then click Run.').format(self._name)))

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

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_PROTON:
            self._enter_proton(
                self._exe,
                self._exe.name if self._exe is not None else self._exe_names[0],
                self._name, self._on_proton_chosen,
                title=self.tr("Step 2: Choose Proton Version"),
                missing_text=self.tr("{0} was not found in your mod staging "
                             "folder.\n\nInstall {0} as a mod, then "
                             "reopen this wizard.").format(self._name))
        elif idx == _PG_RUN:
            self._set_status(self._run_status, self.tr("Launching {0}…").format(self._name))
            self._start_run()

    def _capture_output_mod_name(self):
        self._output_mod_name = sanitize_output_name(
            self._output_name_entry.text(), self._output_default)

    def _profile(self) -> str:
        return getattr(self._ctx, "profile_name", None) or "default"

    def _skip_deploy(self):
        self._capture_output_mod_name()
        self._goto_step(_PG_PROTON)

    def _start_bs_deploy(self):
        self._capture_output_mod_name()
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        # Materialize the output-capture mod (modlist entry + Config.xml
        # OutputDataPath) BEFORE the deploy so the filemap picks it up -
        # replaces the Tk run_deploy_pipeline(on_pre_filemap=) hook.
        from Utils.bodyslide_tools import apply_output_redirect
        try:
            apply_output_redirect(
                self._game, self._output_mod_name, self._profile(),
                post_deploy=False, tool_label=self._name, log_fn=self._log)
            self._ran = True   # modlist gained the output mod - refresh on close
        except Exception as exc:
            self._log(f"{self._name} Wizard: output redirect failed: {exc}")

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status,
                                    lambda: self._goto_step(_PG_PROTON),
                                    _re_enable):
            _re_enable()

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        from Utils.bodyslide_tools import find_deployed_exe
        game = self._game
        deployed = find_deployed_exe(game, self._exe_names)
        if deployed is None:
            self._set_status(
                self._run_status,
                self.tr('{0} was not found in the deployed Data folder.\n\nDeploy your modlist first, then reopen this wizard.').format(self._name), RED)
            return
        staged_exe = self._exe
        name = self._name
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        output_mod_name, profile = self._output_mod_name, self._profile()

        def worker():
            import subprocess
            from Utils.bodyslide_tools import apply_output_redirect
            from Utils.exe_launch import (
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )
            from Utils.steam_finder import proton_run_command
            _wlog = lambda m: self._log(f"{name} Wizard: {m}")
            gl_log = None
            try:
                # Re-apply in case the user skipped deploy, and patch the
                # deployed copy directly when deploy mode produced an
                # independent file.
                try:
                    apply_output_redirect(
                        game, output_mod_name, profile,
                        post_deploy=True, tool_label=name, log_fn=self._log)
                except Exception as exc:
                    _wlog(f"output redirect failed: {exc}")

                result = resolve_tool_prefix(
                    staged_exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Could not find Proton '{0}' - "
                              "check that it is installed in Steam.").format(proton_name), RED)
                    return
                proton_script, compat_data, env = result

                # Proton's Xalia UI-automation helper destabilises BodySlide /
                # Outfit Studio (wxWidgets): it floods the app with
                # window-handle queries and crashes when the Preview child
                # window opens.  Proton 10 renamed the knob, so set both.
                env["PROTON_DISABLE_XALIA"] = "1"
                env["PROTON_USE_XALIA"] = "0"

                # The x64 builds autofill the Data folder from the Bethesda
                # Softworks registry key - seed it (idempotent).
                try:
                    from Utils.bethesda_registry import maybe_register_for_game
                    maybe_register_for_game(
                        prefix_dir=compat_data, proton_script=proton_script,
                        env=env, game=game, log_fn=_wlog)
                except Exception as exc:
                    _wlog(f"registry write skipped: {exc}")

                # Optional GL diagnostics (MM_BODYSLIDE_GLLOG=1) for the
                # black-preview issue.
                if os.environ.get("MM_BODYSLIDE_GLLOG"):
                    env["WINEDEBUG"] = "+wgl,+opengl,fixme-all"
                    env["MESA_DEBUG"] = "1"
                    env.setdefault("LIBGL_DEBUG", "verbose")
                    try:
                        log_path = compat_data / f"gl_trace_{deployed.name}.log"
                        gl_log = open(log_path, "w", encoding="utf-8")
                        _wlog(f"GL trace → {log_path}")
                    except OSError as exc:
                        _wlog(f"could not open GL trace log: {exc}")
                        gl_log = None

                _wlog(f"launching {deployed} via Proton (cwd={deployed.parent})")
                safe_emit(self._run_status_sig,
                          self.tr("{0} is running.\nClose it when you are done, "
                          "then click Done.").format(name), GREEN)
                safe_emit(self._run_started_sig)
                if gl_log is not None:
                    # GL trace mode: keep the raw file redirect (verbose OpenGL
                    # channels the log-panel stream would flood).
                    proc = subprocess.Popen(
                        # runinprefix - same verb as the normal
                        # run_tool_logged path (no steam.exe shim).
                        proton_run_command(proton_script, "runinprefix",
                                           str(deployed), env=env),
                        env=env,
                        cwd=str(deployed.parent),
                        stdout=gl_log,
                        stderr=gl_log,
                    )
                    proc.wait()
                else:
                    run_tool_logged(proton_script, deployed, env,
                                    log_fn=_wlog, label=name)
                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)
                _wlog(f"{deployed.name} closed.")
                safe_emit(self._run_status_sig, self.tr("{0} finished.").format(name), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"{name} Wizard: launch error: {exc}")
            finally:
                if gl_log is not None:
                    try:
                        gl_log.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True, name="bodyslide-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
