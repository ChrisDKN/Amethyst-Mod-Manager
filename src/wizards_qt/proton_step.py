"""Reusable "Choose Proton Version" wizard step - Qt port of the Tk
ProtonPrefixStepMixin's step UI (wizards/_proton_prefix.py).

Lets the user pick a Proton version and a prefix placement for a wizard tool:
isolated (prefix_<Proton>/ next to the exe, default), shared
(wine_prefixes/shared_<Proton>/), or the game's own prefix. The pick persists
per-exe (shared with the Mod Files exe launcher and the Tk wizards) via
Utils.exe_launch. Includes the optional env-vars entry and the
double-click-to-confirm Delete Prefix button. Texture-tool callers can also
request a hybrid-system discrete-GPU selector.

Embed one per wizard view; `on_continue(proton_name, prefix_mode)` fires after
the choices are saved.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QComboBox, QLineEdit,
)

from gui_qt.help_marker import help_mark_qss, make_help_marker, tip_text
from gui_qt.theme_qt import (active_palette, _c, button_qss, ok_text,
                             err_text, warn_text)
from gui_qt.safe_emit import safe_emit
from Utils.exe_launch import (
    PREFIX_MODE_GAME, PREFIX_MODE_ISOLATED, PREFIX_MODE_SHARED,
    load_prefix_mode, load_proton_override, load_tool_launch_args,
    load_tool_launch_env, load_winetricks_style, save_prefix_mode,
    save_proton_override, save_tool_launch_args, save_tool_launch_env,
    save_winetricks_style, shared_prefix_dir,
    load_wizard_always_use_settings, save_wizard_always_use_settings,
    load_wizard_prefer_discrete_gpu, save_wizard_prefer_discrete_gpu,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame



class ProtonStepWidget(QWidget):
    """Choose Proton version + prefix placement for a wizard tool."""

    # (ok, message) from the delete-prefix worker → UI thread.
    _delete_done = Signal(bool, str)

    def __init__(self, game: "BaseGame", exe: Path,
                 tool_exe_name: str, tool_display_name: str,
                 on_continue, log_fn=None, *,
                 allow_game_prefix: bool = True,
                 isolated_prefix_dir_fn=None,
                 title: str | None = None,
                 deps_note: str | None = None,
                 default_prefix_mode: str | None = None,
                 show_launch_args: bool = False,
                 default_launch_args: str = "",
                 show_discrete_gpu: bool = False,
                 wizard_id: str = "",
                 wizard_label: str = "",
                 wizard_label_args: tuple = ()):
        super().__init__()
        if title is None:
            title = self.tr("Choose Proton Version")
        if deps_note is None:
            deps_note = self.tr("Each version gets its own prefix; "
                                "dependencies are installed into it "
                                "automatically on the next step.")
        self._game = game
        self._exe = exe
        self._tool_exe_name = tool_exe_name
        self._tool_display_name = tool_display_name
        self._on_continue = on_continue
        self._log = log_fn or (lambda _m: None)
        self._allow_game_prefix = allow_game_prefix
        self._show_launch_args = show_launch_args
        self._default_launch_args = default_launch_args
        self._wizard_id = wizard_id
        self._wizard_label = wizard_label
        self._wizard_label_args = tuple(wizard_label_args or ())
        self._remembered = load_wizard_always_use_settings(game, wizard_id)
        self._auto_skip_pending = False
        self._args_entry = None
        self._prefer_discrete_gpu_cb: QCheckBox | None = None
        # Hosts whose exe sits somewhere a prefix shouldn't go (e.g. Creation
        # Kit in the game root) relocate the isolated prefix; the Delete
        # button must target the same dir (mirrors Tk _isolated_prefix_dir).
        self._isolated_prefix_dir_fn = (
            isolated_prefix_dir_fn
            or (lambda name: self._exe.parent / f"prefix_{name}"))
        self._confirm_delete = False

        self._delete_done.connect(self._on_delete_done)

        p = active_palette()
        self.setStyleSheet(help_mark_qss(p))
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(6)

        def add_help_control(control, help_text: str):
            control.setToolTip(tip_text(help_text))
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(control)
            row.addWidget(make_help_marker(help_text))
            row.addStretch(1)
            v.addWidget(holder)

        def add_heading(text: str, help_text: str):
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addStretch(1)
            label = QLabel(text)
            label.setStyleSheet(
                f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            label.setToolTip(tip_text(help_text))
            row.addWidget(label)
            row.addWidget(make_help_marker(help_text))
            row.addStretch(1)
            v.addWidget(holder)

        intro_help = (
            self.tr("{0} runs in its own Wine prefix, stored next to "
                    "its exe and separate from the game's prefix, so you can "
                    "pick any Proton version without affecting the game.")
            .format(tool_display_name)
            + "\n\n" + deps_note
        )
        add_heading(title, intro_help)

        from Utils.steam_finder import list_installed_proton
        self._versions = [s.parent.name for s in list_installed_proton()]
        if not self._versions:
            err = QLabel(self.tr("No Proton versions were found.\n\n"
                         "Install a Proton version in Steam (or with "
                         "Heroic's Wine Manager), then reopen this wizard."))
            err.setAlignment(Qt.AlignHCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            v.addWidget(err)
            v.addStretch(1)
            return

        dim = f"color:{_c(p,'TEXT_DIM')};"
        # ---- prefix mode checkboxes ----
        saved_mode = load_prefix_mode(game, tool_exe_name)
        saved_proton = load_proton_override(game, tool_exe_name)
        mode = saved_mode
        # Apply a tool-specific default only before this exe has any saved
        # Proton choice. After the first Continue, the user's selection wins.
        if (default_prefix_mode is not None
                and saved_proton is None):
            mode = default_prefix_mode
        game_pfx_ok = self._game_prefix_available()
        self._remember_warning = ""
        if self._remembered:
            if saved_mode == PREFIX_MODE_GAME:
                if not (allow_game_prefix and game_pfx_ok):
                    self._remember_warning = self.tr(
                        "The saved game prefix is unavailable. Choose another "
                        "prefix setting.")
            elif not saved_proton:
                self._remember_warning = self.tr(
                    "The saved Proton selection is incomplete. Choose a "
                    "Proton version.")
            elif self._match_installed_version(saved_proton) is None:
                self._remember_warning = self.tr(
                    "The saved Proton version '{0}' is no longer installed. "
                    "Choose another version.").format(saved_proton)
        if mode == PREFIX_MODE_GAME and not (allow_game_prefix and game_pfx_ok):
            mode = PREFIX_MODE_ISOLATED

        if self._remember_warning:
            warning = QLabel(self._remember_warning)
            warning.setAlignment(Qt.AlignHCenter)
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color:{warn_text()};")
            v.addWidget(warning)

        self._shared_chk = QCheckBox(self.tr("Use shared prefix"))
        self._shared_chk.setChecked(mode == PREFIX_MODE_SHARED)
        self._shared_chk.toggled.connect(self._on_shared_toggle)
        add_help_control(self._shared_chk, self.tr(
            "Reuse one prefix (per Proton version) shared by every wizard "
            "tool, kept in the app config folder instead of next to the exe."))

        self._game_chk = None
        if allow_game_prefix and game_pfx_ok:
            self._game_chk = QCheckBox(self.tr("Use game prefix"))
            self._game_chk.setChecked(mode == PREFIX_MODE_GAME)
            self._game_chk.toggled.connect(self._on_game_pfx_toggle)
            add_help_control(self._game_chk, self.tr(
                "Run inside the game's own prefix. No new prefix is created "
                "and the Proton version follows the game's Steam setting."))

        # ---- winetricks-style launch ----
        self._winetricks_chk = QCheckBox(
            self.tr("Launch with plain Wine (winetricks-style)"))
        self._winetricks_chk.setChecked(
            load_winetricks_style(game, tool_exe_name))
        add_help_control(self._winetricks_chk, self.tr(
            "Run this tool with plain Wine against the selected prefix instead "
            "of starting a Proton session."))

        if show_discrete_gpu:
            self._prefer_discrete_gpu_cb = QCheckBox(
                self.tr("Prefer discrete GPU (hybrid systems)"))
            self._prefer_discrete_gpu_cb.setChecked(
                load_wizard_prefer_discrete_gpu(game, wizard_id))
            add_help_control(self._prefer_discrete_gpu_cb, self.tr(
                "Expose the discrete GPU as adapter 0 for texconv. This may "
                "use more power and falls back to the CPU if unavailable."))

        # ---- proton picker row + delete ----
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 4); rh.setSpacing(8)
        rh.addStretch(1)
        self._proton_combo = QComboBox()
        self._proton_combo.addItems(self._versions)
        self._proton_combo.setMinimumWidth(280)
        self._proton_combo.setCurrentText(self._initial_version())
        self._proton_combo.currentTextChanged.connect(
            lambda _t: self._update_prefix_delete_state())
        rh.addWidget(self._proton_combo)
        self._delete_btn = QPushButton(self.tr("Delete Prefix"))
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.clicked.connect(self._on_delete_prefix)
        rh.addWidget(self._delete_btn)
        rh.addStretch(1)
        v.addWidget(row)

        self._prefix_status = QLabel("")
        self._prefix_status.setAlignment(Qt.AlignHCenter)
        self._prefix_status.setWordWrap(True)
        self._prefix_status.setStyleSheet(dim)
        v.addWidget(self._prefix_status)

        # ---- launch arguments ----
        if self._show_launch_args:
            v.addSpacing(8)
            add_heading(self.tr("Launch Arguments (optional)"), self.tr(
                "Extra command-line arguments appended when the tool "
                "launches. Saved next to the exe and reapplied on every run."))
            self._args_entry = QLineEdit()
            if self._default_launch_args:
                self._args_entry.setPlaceholderText(self._default_launch_args)
            saved_args = load_tool_launch_args(exe)
            self._args_entry.setText(saved_args or self._default_launch_args)
            v.addWidget(self._args_entry)

        # ---- env vars ----
        v.addSpacing(8)
        add_heading(self.tr("Environment Variables (optional)"), self.tr(
            "Space-separated KEY=VALUE pairs applied when the tool launches. "
            "Saved next to the exe and reapplied on every run."))
        self._env_entry = QLineEdit()
        self._env_entry.setPlaceholderText(
            self.tr("e.g. PROTON_USE_WINED3D=1 WINEDLLOVERRIDES=dinput8=n,b"))
        self._env_entry.setText(load_tool_launch_env(exe))
        v.addWidget(self._env_entry)

        v.addStretch(1)
        self._always_use_chk = QCheckBox(
            self.tr("Always use these settings"))
        self._always_use_chk.setChecked(self._remembered)
        add_help_control(self._always_use_chk, self.tr(
            "Skip this Proton step on future runs and reuse the saved values. "
            "Reset it from Wizard > Wizard Settings."))
        cont = QPushButton(self.tr("Continue"))
        cont.setCursor(Qt.PointingHandCursor)
        cont.setStyleSheet(button_qss("BTN_INFO"))
        cont.clicked.connect(self._on_chosen)
        v.addWidget(cont, 0, Qt.AlignHCenter)

        self._update_proton_row_state()
        self._auto_skip_pending = bool(
            self._remembered and not self._remember_warning)

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        if not self._auto_skip_pending:
            return
        self._auto_skip_pending = False
        self.setVisible(False)
        QTimer.singleShot(0, self._auto_continue)

    def _auto_continue(self):
        try:
            self._on_chosen()
        except Exception as exc:
            self.setVisible(True)
            self._log(
                f"{self._tool_display_name} Wizard: could not reuse saved "
                f"settings: {exc}")

    # ---- defaults / state ---------------------------------------------------
    def _match_installed_version(self, saved: str) -> str | None:
        if not saved:
            return None
        low = saved.lower()
        for version in self._versions:
            if version.lower() == low:
                return version
        for version in self._versions:
            if version.lower().startswith(low):
                return version
        return None

    def _initial_version(self) -> str:
        """Saved per-exe override, else the game's own Proton, else first."""
        from Utils.steam_finder import find_proton_for_game, game_steam_id
        saved = load_proton_override(self._game, self._tool_exe_name) or ""
        if not saved:
            steam_id = game_steam_id(self._game)
            script = find_proton_for_game(steam_id) if steam_id else None
            if script is not None:
                saved = script.parent.name
        matched = self._match_installed_version(saved)
        if matched is not None:
            return matched
        return self._versions[0]

    def _game_prefix_available(self) -> bool:
        try:
            pfx = (self._game.get_prefix_path()
                   if hasattr(self._game, "get_prefix_path") else None)
            return pfx is not None and Path(pfx).is_dir()
        except Exception:
            return False

    def _current_prefix_mode(self) -> str:
        if self._game_chk is not None and self._game_chk.isChecked():
            return PREFIX_MODE_GAME
        if self._shared_chk.isChecked():
            return PREFIX_MODE_SHARED
        return PREFIX_MODE_ISOLATED

    def prefer_discrete_gpu(self) -> bool:
        """Whether the optional hybrid-GPU selector is enabled."""
        return bool(
            self._prefer_discrete_gpu_cb is not None
            and self._prefer_discrete_gpu_cb.isChecked()
        )

    def _on_shared_toggle(self, on: bool):
        if on and self._game_chk is not None:
            self._game_chk.setChecked(False)
        self._update_proton_row_state()

    def _on_game_pfx_toggle(self, on: bool):
        if on:
            self._shared_chk.setChecked(False)
        self._update_proton_row_state()

    def _update_proton_row_state(self):
        """The game prefix has its own fixed Proton; grey the picker out then."""
        use_game = self._game_chk is not None and self._game_chk.isChecked()
        self._proton_combo.setEnabled(not use_game)
        if use_game:
            self._delete_btn.setEnabled(False)
            self._prefix_status.setText(
                self.tr("Using the game's existing prefix - Proton version follows "
                "the game's Steam setting and no new prefix is created."))
            self._prefix_status.setStyleSheet(
                f"color:{_c(active_palette(),'TEXT_DIM')};")
        else:
            self._update_prefix_delete_state()

    def _on_chosen(self):
        mode = self._current_prefix_mode()
        name = self._proton_combo.currentText()
        save_proton_override(self._game, self._tool_exe_name, name)
        save_prefix_mode(self._game, self._tool_exe_name, mode)
        wt = self._winetricks_chk.isChecked()
        save_winetricks_style(self._game, self._tool_exe_name, wt)
        if wt:
            self._log(f"{self._tool_display_name} Wizard: winetricks-style "
                      "launch enabled (plain Wine, no Proton session).")
        try:
            save_tool_launch_env(self._exe, self._env_entry.text().strip())
        except Exception:
            pass
        if self._args_entry is not None:
            try:
                save_tool_launch_args(self._exe, self._args_entry.text().strip())
            except Exception:
                pass
        if self._prefer_discrete_gpu_cb is not None:
            save_wizard_prefer_discrete_gpu(
                self._game, self._wizard_id,
                self._prefer_discrete_gpu_cb.isChecked())
        save_wizard_always_use_settings(
            self._game, self._wizard_id,
            self._always_use_chk.isChecked(),
            label=self._wizard_label,
            label_args=self._wizard_label_args)
        if mode == PREFIX_MODE_GAME:
            self._log(f"{self._tool_display_name} Wizard: using the game's own prefix.")
        elif mode == PREFIX_MODE_SHARED:
            self._log(f"{self._tool_display_name} Wizard: using {name} "
                      "with a shared prefix in the app config folder.")
        else:
            self._log(f"{self._tool_display_name} Wizard: using {name} "
                      "with an isolated prefix next to the exe.")
        self._on_continue(name, mode)

    # ---- Delete Prefix ------------------------------------------------------
    def _selected_prefix_dir(self) -> Path | None:
        name = self._proton_combo.currentText().strip()
        if not name:
            return None
        if self._shared_chk.isChecked():
            return shared_prefix_dir(name)
        return self._isolated_prefix_dir_fn(name)

    def _set_prefix_status(self, text: str, color: str | None = None):
        c = color or _c(active_palette(), "TEXT_DIM")
        self._prefix_status.setStyleSheet(f"color:{c};")
        self._prefix_status.setText(text)

    def _update_prefix_delete_state(self):
        self._confirm_delete = False
        d = self._selected_prefix_dir()
        exists = d is not None and d.is_dir()
        self._delete_btn.setText(self.tr("Delete Prefix"))
        self._delete_btn.setStyleSheet("")
        self._delete_btn.setEnabled(exists)
        self._set_prefix_status(
            self.tr("A prefix already exists for this version. Delete it if "
            "{0}\nhas issues - it is recreated "
            "automatically on the next step.").format(self._tool_display_name)
            if exists else "")

    def _on_delete_prefix(self):
        d = self._selected_prefix_dir()
        if d is None or not d.is_dir():
            self._update_prefix_delete_state()
            return
        if not self._confirm_delete:
            self._confirm_delete = True
            self._delete_btn.setText(self.tr("Confirm Delete"))
            self._delete_btn.setStyleSheet(button_qss("BTN_DANGER", padding="0px"))
            self._set_prefix_status(self.tr("Click again to delete '{0}'.").format(d.name))
            return
        self._confirm_delete = False
        self._delete_btn.setEnabled(False)
        self._delete_btn.setText(self.tr("Deleting…"))
        self._set_prefix_status(self.tr("Deleting '{0}'…").format(d.name))

        def worker(target=d):
            import shutil
            try:
                # Safety: only delete recognised tool-prefix dirs.
                if not (target.name.startswith("prefix_")
                        or target.name.startswith("shared_")
                        or target.name.startswith("creationkit_")):
                    raise RuntimeError(
                        f"refusing to delete non-prefix dir: {target}")
                shutil.rmtree(target)
            except Exception as exc:
                safe_emit(self._delete_done, False, str(exc))
                return
            safe_emit(self._delete_done, True, str(target))

        threading.Thread(target=worker, daemon=True,
                         name="wizard-prefix-delete").start()

    def _on_delete_done(self, ok: bool, msg: str):
        if ok:
            self._log(f"{self._tool_display_name} Wizard: deleted prefix {msg}")
            self._set_prefix_status(
                self.tr("Prefix deleted - a fresh one is created on the next step."),
                ok_text())
        else:
            self._log(f"{self._tool_display_name} Wizard: prefix delete error: {msg}")
            self._set_prefix_status(self.tr("Could not delete prefix: {0}").format(msg), err_text())
        d = self._selected_prefix_dir()
        self._delete_btn.setText(self.tr("Delete Prefix"))
        self._delete_btn.setStyleSheet("")
        self._delete_btn.setEnabled(d is not None and d.is_dir())
