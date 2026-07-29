"""Reusable "Choose Proton Version" wizard step — Qt port of the Tk
ProtonPrefixStepMixin's step UI (wizards/_proton_prefix.py).

Lets the user pick a Proton version and a prefix placement for a wizard tool:
isolated (prefix_<Proton>/ next to the exe, default), shared
(wine_prefixes/shared_<Proton>/), or the game's own prefix. The pick persists
per-exe (shared with the Mod Files exe launcher and the Tk wizards) via
Utils.exe_launch. Includes the optional env-vars entry and the
double-click-to-confirm Delete Prefix button.

Embed one per wizard view; `on_continue(proton_name, prefix_mode)` fires after
the choices are saved.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QComboBox, QLineEdit,
)

from gui_qt.theme_qt import active_palette, _c, button_qss, ok_text, err_text
from gui_qt.safe_emit import safe_emit
from Utils.exe_launch import (
    PREFIX_MODE_GAME, PREFIX_MODE_ISOLATED, PREFIX_MODE_SHARED,
    load_prefix_mode, load_proton_override, load_tool_launch_args,
    load_tool_launch_env, load_winetricks_style, save_prefix_mode,
    save_proton_override, save_tool_launch_args, save_tool_launch_env,
    save_winetricks_style, shared_prefix_dir,
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
                 show_launch_args: bool = False,
                 default_launch_args: str = ""):
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
        self._args_entry = None
        # Hosts whose exe sits somewhere a prefix shouldn't go (e.g. Creation
        # Kit in the game root) relocate the isolated prefix; the Delete
        # button must target the same dir (mirrors Tk _isolated_prefix_dir).
        self._isolated_prefix_dir_fn = (
            isolated_prefix_dir_fn
            or (lambda name: self._exe.parent / f"prefix_{name}"))
        self._confirm_delete = False

        self._delete_done.connect(self._on_delete_done)

        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(6)

        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        v.addWidget(head)

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

        desc = QLabel(
            self.tr("{0} runs in its own Wine prefix, stored next to "
            "its exe and separate from the game's prefix, so you can pick any "
            "Proton version without affecting the game.\n\n").format(
                tool_display_name) + deps_note)
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignHCenter)
        desc.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        v.addWidget(desc)
        v.addSpacing(6)

        # ---- prefix mode checkboxes ----
        mode = load_prefix_mode(game, tool_exe_name)
        game_pfx_ok = self._game_prefix_available()
        if mode == PREFIX_MODE_GAME and not (allow_game_prefix and game_pfx_ok):
            mode = PREFIX_MODE_ISOLATED

        dim = f"color:{_c(p,'TEXT_DIM')};"
        self._shared_chk = QCheckBox(self.tr("Use shared prefix"))
        self._shared_chk.setChecked(mode == PREFIX_MODE_SHARED)
        self._shared_chk.toggled.connect(self._on_shared_toggle)
        v.addWidget(self._shared_chk)
        shared_note = QLabel(
            self.tr("Reuse one prefix (per Proton version) shared by every wizard "
            "tool, kept in the app config folder instead of next to the exe."))
        shared_note.setWordWrap(True)
        shared_note.setStyleSheet(dim)
        shared_note.setContentsMargins(26, 0, 0, 6)
        v.addWidget(shared_note)

        self._game_chk = None
        if allow_game_prefix and game_pfx_ok:
            self._game_chk = QCheckBox(self.tr("Use game prefix"))
            self._game_chk.setChecked(mode == PREFIX_MODE_GAME)
            self._game_chk.toggled.connect(self._on_game_pfx_toggle)
            v.addWidget(self._game_chk)
            game_note = QLabel(
                self.tr("Run inside the game's own prefix. No new prefix is created "
                "and the Proton version follows the game's Steam setting."))
            game_note.setWordWrap(True)
            game_note.setStyleSheet(dim)
            game_note.setContentsMargins(26, 0, 0, 0)
            v.addWidget(game_note)

        # ---- winetricks-style launch ----
        self._winetricks_chk = QCheckBox(
            self.tr("Launch with plain Wine (winetricks-style)"))
        self._winetricks_chk.setChecked(
            load_winetricks_style(game, tool_exe_name))
        v.addWidget(self._winetricks_chk)
        wt_note = QLabel(
            self.tr("Use Winetricks style launch"))
        wt_note.setWordWrap(True)
        wt_note.setStyleSheet(dim)
        wt_note.setContentsMargins(26, 0, 0, 6)
        v.addWidget(wt_note)

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
            args_head = QLabel(self.tr("Launch Arguments (optional)"))
            args_head.setAlignment(Qt.AlignHCenter)
            args_head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
            v.addWidget(args_head)
            args_note = QLabel(
                self.tr("Extra command-line arguments appended when the tool "
                "launches. Saved next to the exe and reapplied on every run."))
            args_note.setWordWrap(True)
            args_note.setAlignment(Qt.AlignHCenter)
            args_note.setStyleSheet(dim)
            v.addWidget(args_note)
            self._args_entry = QLineEdit()
            if self._default_launch_args:
                self._args_entry.setPlaceholderText(self._default_launch_args)
            saved_args = load_tool_launch_args(exe)
            self._args_entry.setText(saved_args or self._default_launch_args)
            v.addWidget(self._args_entry)

        # ---- env vars ----
        v.addSpacing(8)
        env_head = QLabel(self.tr("Environment Variables (optional)"))
        env_head.setAlignment(Qt.AlignHCenter)
        env_head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        v.addWidget(env_head)
        env_note = QLabel(
            self.tr("Space-separated KEY=VALUE pairs applied when the tool launches. "
            "Saved next to the exe and reapplied on every run."))
        env_note.setWordWrap(True)
        env_note.setAlignment(Qt.AlignHCenter)
        env_note.setStyleSheet(dim)
        v.addWidget(env_note)
        self._env_entry = QLineEdit()
        self._env_entry.setPlaceholderText(
            self.tr("e.g. PROTON_USE_WINED3D=1 WINEDLLOVERRIDES=dinput8=n,b"))
        self._env_entry.setText(load_tool_launch_env(exe))
        v.addWidget(self._env_entry)

        v.addStretch(1)
        cont = QPushButton(self.tr("Continue"))
        cont.setCursor(Qt.PointingHandCursor)
        cont.setStyleSheet(button_qss("BTN_INFO"))
        cont.clicked.connect(self._on_chosen)
        v.addWidget(cont, 0, Qt.AlignHCenter)

        self._update_proton_row_state()

    # ---- defaults / state ---------------------------------------------------
    def _initial_version(self) -> str:
        """Saved per-exe override, else the game's own Proton, else first."""
        from Utils.steam_finder import find_proton_for_game, game_steam_id
        saved = load_proton_override(self._game, self._tool_exe_name) or ""
        if not saved:
            steam_id = game_steam_id(self._game)
            script = find_proton_for_game(steam_id) if steam_id else None
            if script is not None:
                saved = script.parent.name
        if saved in self._versions:
            return saved
        if saved:
            low = saved.lower()
            for vname in self._versions:
                if vname.lower().startswith(low):
                    return vname
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
                self.tr("Using the game's existing prefix — Proton version follows "
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
            "{0}\nhas issues — it is recreated "
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
                self.tr("Prefix deleted — a fresh one is created on the next step."),
                ok_text())
        else:
            self._log(f"{self._tool_display_name} Wizard: prefix delete error: {msg}")
            self._set_prefix_status(self.tr("Could not delete prefix: {0}").format(msg), err_text())
        d = self._selected_prefix_dir()
        self._delete_btn.setText(self.tr("Delete Prefix"))
        self._delete_btn.setStyleSheet("")
        self._delete_btn.setEnabled(d is not None and d.is_dir())
