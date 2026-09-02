"""VRAMr / BENDr / ParallaxR wizards - Qt port of wizards/vramr.py and
wizards/bendr_parallaxr.py.

All three share one shape: manual Nexus download → locate → extract to
Applications/<tool>/ (verified by its tools/ dir) → deploy → run the neutral
wrapper (wrappers/vramr.py / bendr.py / parallaxr.py) against the deployed
Data folder, writing output as a staging mod.  VRAMr adds a preset radio
group; the others run straight away.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QT_TRANSLATE_NOOP, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QRadioButton,
    QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.wizards.textures import (
    VRAMR_PRESETS, TextureToolCancelled, texture_tool_installed,
    vramr_installed,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# tool id → (title, nexus_url, app_dir, archive keyword, output dir, run desc,
#            has_presets)
_TOOLS = {
    "vramr": (
        "VRAMr",
        "https://www.nexusmods.com/skyrimspecialedition/mods/90557?tab=files",
        "VRAMr", "vramr", "VRAMr",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Select an optimisation preset, then click Run."),
        True),
    "bendr": (
        "BENDr",
        "https://www.nexusmods.com/skyrimspecialedition/mods/121578?tab=files",
        "BENDr", "bendr", "BENDr",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Processes normal maps: BSA extract → filter → "
                          "parallax prep → bend normals → BC7 compress"),
        False),
    "parallaxr": (
        "ParallaxR",
        "https://www.nexusmods.com/skyrimspecialedition/mods/124711?tab=files",
        "ParallaxR", "parallaxr", "ParallaxR",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Processes parallax textures: BSA extract → filter "
                          "pairs → height maps → output QC"),
        False),
}

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_RUN = range(5)


def _discard_output(output_dir, name: str, log_fn) -> None:
    """Delete a stopped run's half-written output folder.

    These tools rebuild output_dir from scratch on every run, so whatever is
    there belongs to the run that was just cancelled - it is a partial texture
    set that would deploy as a broken mod if left in staging."""
    import shutil
    try:
        if output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)
            log_fn(f"{name}: discarded partial output at {output_dir}")
    except Exception as exc:
        log_fn(f"{name}: could not remove partial output: {exc}")


def _release_on_main(set_lock) -> None:
    """Call ``set_lock(False)`` on the GUI thread from a worker thread.

    Falls back to a direct call if there is no QApplication to hop through -
    leaving the lock held would be far worse than a cross-thread release of
    what is only a dict write plus a setEnabled."""
    try:
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app, lambda: set_lock(False))
            return
    except Exception:
        pass
    set_lock(False)


class TextureToolView(WizardViewBase):
    """Download, install, deploy and run VRAMr / BENDr / ParallaxR."""

    _run_reenable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "vramr", **_extra):
        (self._name, self._nexus_url, self._app_dir, self._archive_kw,
         self._output_dir, self._run_desc, self._has_presets) = _TOOLS[tool]
        self._tool_id = tool
        self._prefer_discrete_gpu_cb: QCheckBox | None = None
        # Set by Stop (and by closing the tab mid-run); the wrappers poll it
        # between/inside steps and kill the running tool's process group.
        self._cancel_evt = threading.Event()
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run {0} - {1}").format(self._name, game.name))
        self._preset = "optimum"
        self._run_reenable_sig.connect(self._guard(self._on_run_reenable))

        self._installed = (vramr_installed(game) if tool == "vramr"
                           else texture_tool_installed(game, self._app_dir))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download {0}").format(self._name),
            self.tr("Click the button below to open the {0} page on Nexus "
            "Mods, then download the archive.\n\nOnce downloaded, click Next.")
            .format(self._name),
            self._nexus_url,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract {0}").format(self._name)))
        self._stack.addWidget(self._build_deploy_page(
            self.tr("Step 4: Deploy Modlist"),
            self.tr("{0} reads your mods from the deployed Data folder.\n\n"
            "Deploy your modlist first, then click Run.").format(self._name),
            lambda: self._goto_step(_PG_RUN)))
        self._stack.addWidget(self._build_run_page())

        if self._installed:
            self._goto_step(_PG_DEPLOY)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _build_run_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 5: Run {0}").format(self._name))
        self._make_note(lay, self.tr(self._run_desc))

        if self._has_presets:
            p = active_palette()
            box = QWidget()
            box.setStyleSheet(
                f"background:{_c(p,'BG_PANEL')}; border-radius:6px;")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(12, 8, 12, 8)
            self._preset_group = QButtonGroup(self)
            for key, label, desc in VRAMR_PRESETS:
                row = QWidget()
                rh = QHBoxLayout(row); rh.setContentsMargins(0, 2, 0, 2)
                rb = QRadioButton(label)
                rb.setProperty("preset_key", key)
                if key == "optimum":
                    rb.setChecked(True)
                self._preset_group.addButton(rb)
                rh.addWidget(rb)
                d = QLabel(desc)
                d.setStyleSheet(self._dim)
                rh.addWidget(d)
                rh.addStretch(1)
                bl.addWidget(row)
            lay.addWidget(box)

        if self._tool_id in ("vramr", "bendr"):
            self._prefer_discrete_gpu_cb = QCheckBox(
                self.tr("Prefer discrete GPU (hybrid systems)"))
            self._prefer_discrete_gpu_cb.setToolTip(self.tr(
                "Runs the texture encoder through Proton/DXVK and exposes the "
                "discrete GPU as adapter 0. This may use more power."))
            lay.addWidget(self._prefer_discrete_gpu_cb)

            gpu_note = QLabel(self.tr(
                "VRAMr and BENDr use texconv's GPU BC7 encoder when "
                "DirectCompute is available; otherwise texconv falls back "
                "to its CPU encoder."))
            gpu_note.setWordWrap(True)
            gpu_note.setStyleSheet(self._dim)
            lay.addWidget(gpu_note)

        staging = self._game.get_effective_mod_staging_path()
        out_lbl = QLabel(self.tr("Output: {0}").format(staging / self._output_dir))
        out_lbl.setWordWrap(True)
        out_lbl.setStyleSheet(self._dim)
        lay.addWidget(out_lbl)

        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        # Run and Stop share a row so the page doesn't reflow when Stop
        # appears; Stop is hidden until a run is actually in flight.
        btn_row = QWidget()
        brh = QHBoxLayout(btn_row)
        brh.setContentsMargins(0, 0, 0, 0)
        brh.setSpacing(8)
        brh.addStretch(1)
        self._run_btn = self._accent_btn(self.tr("▶  Run {0}").format(self._name))
        self._run_btn.clicked.connect(self._start_run)
        brh.addWidget(self._run_btn)
        self._stop_btn = self._red_btn(self.tr("■  Stop"))
        self._stop_btn.setToolTip(self.tr(
            "Stop the running tool and discard its partial output."))
        self._stop_btn.clicked.connect(self._request_stop)
        self._stop_btn.setVisible(False)
        brh.addWidget(self._stop_btn)
        brh.addStretch(1)
        lay.addWidget(btn_row)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                [self._archive_kw],
                self.tr("Select the {0} archive").format(self._name),
                self.tr("{0} archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.").format(self._name),
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(
                self._app_dir, "", self._name, marker="tools")

    def _on_extract_done(self, ok: bool):
        if ok:
            self._installed = True
            self._goto_step(_PG_DEPLOY)

    # ---- run ----------------------------------------------------------------------
    def _selected_preset(self) -> str:
        if not self._has_presets:
            return ""
        btn = self._preset_group.checkedButton()
        return btn.property("preset_key") if btn is not None else "optimum"

    def _prefer_discrete_gpu(self) -> bool:
        return bool(
            self._prefer_discrete_gpu_cb is not None
            and self._prefer_discrete_gpu_cb.isChecked()
        )

    def _tool_lock_setter(self):
        """Return a ``(held: bool) -> None`` callable that holds/releases this
        tool's app lock. Call it on the GUI thread.

        It closes over the ctx hook and plain strings ONLY - never over ``self``
        - because the worker's release must still land if the user closed the
        tab mid-run. Routing that release through a Signal on this view would
        lose it to safe_emit once the view is deleted, stranding the lock and
        disabling Deploy/Restore for the rest of the session."""
        hook = getattr(self._ctx, "set_tool_lock", None)
        key, label = f"texture_tool:{self._tool_id}", self._name
        log = self._log
        if hook is None:
            return lambda _held: None

        def _set(held: bool):
            try:
                hook(key, label, held)
            except Exception as exc:
                log(f"{label} Wizard: tool lock failed: {exc}")
        return _set

    def _request_stop(self):
        """Stop pressed: signal the worker and let it unwind.

        The button is disabled rather than hidden so the click can't repeat
        while the current step's process group is being torn down - the worker
        clears the run state when it actually finishes."""
        if self._cancel_evt.is_set():
            return
        self._cancel_evt.set()
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText(self.tr("Stopping…"))
        self._set_status(self._run_status,
                         self.tr("Stopping {0}…").format(self._name))
        self._log(f"{self._name} Wizard: stop requested by user")

    def _open_log_tab(self):
        """Surface the Log tab so the run's output is visible (Tk parity: the
        old wizards popped their own log window). Our log_fn already writes
        there, so this only has to bring it into view."""
        hook = getattr(self._ctx, "open_log_tab", None)
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:
            self._log(f"{self._name} Wizard: could not open log tab: {exc}")

    def _start_run(self):
        game = self._game
        if not self._installed:
            self._set_status(self._run_status,
                             self.tr("{0} not found. Please restart the wizard.").format(self._name), RED)
            return
        from Utils.vfs import effective_tool_data_root
        try:
            game_data_dir = effective_tool_data_root(game)
        except RuntimeError:
            game_data_dir = None
        if game_data_dir is None or not game_data_dir.is_dir():
            self._set_status(self._run_status,
                             self.tr("Game Data folder not found. Deploy first."), RED)
            return

        self._run_btn.setEnabled(False)
        # Fresh token per run - a previous cancelled run left the old one set.
        self._cancel_evt = threading.Event()
        cancel_evt = self._cancel_evt
        self._stop_btn.setEnabled(True)
        self._stop_btn.setText(self.tr("■  Stop"))
        self._stop_btn.setVisible(True)
        preset = self._selected_preset()
        prefer_discrete_gpu = self._prefer_discrete_gpu()
        from Utils.wizards.textures import applications_dir
        bat_dir = applications_dir(game, self._app_dir)
        staging = game.get_effective_mod_staging_path()
        output_dir = staging / self._output_dir
        name, tool_id = self._name, self._app_dir.lower()

        if preset:
            run_msg = self.tr("Running {0} ({1})… This may take a while.").format(
                name, preset)
        else:
            run_msg = self.tr("Running {0}… This may take a while.").format(name)
        self._set_status(self._run_status, run_msg)
        # Lock Deploy/Restore/Play BEFORE the worker starts: the tool reads the
        # deployed Data folder for its whole run, and a deploy or restore
        # underneath it would swap those files mid-pass.
        set_lock = self._tool_lock_setter()
        set_lock(True)
        self._open_log_tab()
        self._log(f"{name} Wizard: starting pipeline…")

        def worker():
            _wlog = lambda m: self._log(str(m))
            try:
                if tool_id == "vramr":
                    from wrappers.vramr import run_vramr
                    run_vramr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                              output_dir=output_dir, preset=preset,
                              prefer_discrete_gpu=prefer_discrete_gpu,
                              log_fn=_wlog, cancel_evt=cancel_evt)
                elif tool_id == "bendr":
                    from wrappers.bendr import run_bendr
                    run_bendr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                              output_dir=output_dir,
                              prefer_discrete_gpu=prefer_discrete_gpu,
                              log_fn=_wlog, cancel_evt=cancel_evt)
                else:
                    from wrappers.parallaxr import run_parallaxr
                    run_parallaxr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                                  output_dir=output_dir, log_fn=_wlog,
                                  cancel_evt=cancel_evt)
                safe_emit(self._run_status_sig,
                          self.tr("{0} complete! Output is ready as a mod.")
                          .format(name), GREEN)
                # marks _ran + enables Done (no auto-close - let the user read
                # the result and click Done).
                safe_emit(self._run_started_sig)
            except TextureToolCancelled:
                # User-requested stop, not a failure. The partial output is a
                # half-processed texture set that would deploy as a broken mod,
                # so bin it rather than leaving it in staging.
                _discard_output(output_dir, name, _wlog)
                safe_emit(self._run_status_sig,
                          self.tr("{0} stopped. Partial output discarded.")
                          .format(name), RED)
                self._log(f"{name} Wizard: stopped by user")
                safe_emit(self._run_reenable_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                self._log(f"{name} Wizard: error: {exc}")
                safe_emit(self._run_reenable_sig)
            finally:
                # Must always run - a lock left held disables Deploy/Restore
                # for the rest of the session. The context object is the
                # QApplication, NOT this view: the receiver-less singleShot
                # overload builds its timer in the *calling* (worker) thread,
                # which has no event loop, and a view context would be gone if
                # the tab was closed mid-run. Either way the release is lost.
                _release_on_main(set_lock)

        threading.Thread(target=worker, daemon=True,
                         name=f"{tool_id}-run").start()

    def _finish(self):
        """Closing the tab (✕ / Done) also stops a run in flight.

        Without this the tool would keep grinding invisibly with no way to
        reach the Stop button. The worker unwinds on its own: it discards the
        partial output and releases the deploy lock from its finally, both of
        which survive this view's destruction."""
        if not self._closing and not self._cancel_evt.is_set():
            self._cancel_evt.set()
        super()._finish()

    def _on_run_reenable(self):
        """Run ended without producing output (error or user stop) - offer Run
        again and retire the Stop button."""
        self._run_btn.setEnabled(True)
        self._stop_btn.setVisible(False)
        self._stop_btn.setEnabled(True)
        self._stop_btn.setText(self.tr("■  Stop"))

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
        # Completed successfully - there is nothing left to stop.
        self._stop_btn.setVisible(False)
