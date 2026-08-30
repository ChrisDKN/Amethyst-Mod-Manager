"""Profile Settings - a modlist-scoped tab listing every profile for the active
game with per-row management. Qt port of the Tk ``gui/profile_settings_overlay.py``
(``ProfileSettingsOverlay``), MINUS the "Steam Cmd" button.

Each row: a lock toggle (disabled for the default profile), the profile name (with
``(default)`` / ``★`` markers), and Rename / Open / Remove buttons. Rename opens an
inline bar under the row; Remove restores the game first if the profile is deployed,
asks a second time if the profile has its own mods, then deletes the folder. All the
persistence reuses the neutral ``Utils.profile_state`` helpers - no backend rewrite.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFrame, QScrollArea,
)

from gui_qt.theme_qt import active_palette, _c, close_button, contrast_text
from gui_qt.icons import icon
from gui_qt.safe_emit import safe_emit
from gui_qt.i18n import profile_display, is_reserved_profile_name
from Utils.profile_state import (
    read_profile_settings, merge_profile_settings, profile_uses_specific_mods,
)
from Utils.xdg import xdg_open


class _LockBox(QWidget):
    """A small bordered checkbox-style box that shows lock.png when locked, drawn
    directly (mirrors the modlist delegate's lock cell - QToolButton.setIcon does
    not render reliably inside a styled small button). Click toggles when enabled."""

    _BOX = 18

    def __init__(self, locked: bool, enabled: bool, on_click):
        super().__init__()
        self._locked = locked
        self._on_click = on_click
        self.setFixedSize(self._BOX, self._BOX)
        self.setEnabled(enabled)
        if enabled:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip(self.tr("Locked profiles can't be removed"))

    def paintEvent(self, _event):
        p = active_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor(_c(p, "BORDER")), 1))
        # Locked → filled (BG_DEEP) so the gold lock reads; else transparent box.
        painter.setBrush(QColor(_c(p, "BG_DEEP")) if self._locked
                         else Qt.NoBrush)
        painter.drawRoundedRect(rect, 3, 3)
        if self._locked:
            lico = icon("lock.png", self._BOX - 4)
            if not lico.isNull():
                lico.paint(painter, self.rect().adjusted(2, 2, -2, -2))
        painter.end()

    def mouseReleaseEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton \
                and self.rect().contains(event.pos()):
            self._on_click()
        super().mouseReleaseEvent(event)


class ProfileSettingsView(QWidget):
    """Hosted as a modlist-scoped tab. Callbacks let the app refresh the profile
    selector + reload when the active profile is renamed / removed."""

    # (profile_name, ok) from the remove worker → UI thread.
    _remove_finished = Signal(str, bool)

    def __init__(self, window, game_name: str, current_profile: str,
                 on_profile_renamed=None, on_profile_removed=None,
                 on_profiles_changed=None, log_fn=None):
        super().__init__()
        self._window = window
        self._game_name = game_name
        self._current_profile = current_profile
        self._on_profile_renamed = on_profile_renamed or (lambda o, n: None)
        self._on_profile_removed = on_profile_removed or (lambda n: None)
        self._on_profiles_changed = on_profiles_changed or (lambda: None)
        self._log = log_fn or (lambda _m: None)

        self._rename_row: QFrame | None = None
        self._rename_target: str | None = None
        self._rename_edit: QLineEdit | None = None

        self.setObjectName("ProfileSettingsView")
        self._remove_finished.connect(self._on_remove_finished)
        self._build()
        self._populate_list()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #ProfileSettingsView {{ background: {c('BG_DEEP')}; }}
        #PSTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #PSTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        QScrollArea {{ background: {c('BG_DEEP')}; border: none; }}
        #PSBody {{ background: {c('BG_DEEP')}; }}
        #ProfileRow {{ background: {c('BG_PANEL')}; }}
        #ProfileRow[alt="true"] {{ background: {c('BG_DEEP')}; }}
        #RenameBar {{ background: {c('BG_HEADER')}; }}
        #DangerButton {{ background: {c('BTN_DANGER')}; color: {contrast_text(c('BTN_DANGER'))}; border: none;
                         border-radius: 4px; padding: 4px 12px; font-size: 12px;
                         font-weight: 600; }}
        #DangerButton:hover {{ background: {c('BTN_DANGER_HOV')}; }}
        #DangerButton:disabled {{ background: {c('BTN_GREY')}; color: {c('TEXT_DIM')}; }}
        """

    def _build(self):
        p = active_palette()
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title bar with a Close button (the scoped tab's × also dismisses it,
        # but a Close button matches the other overlays/panels).
        bar = QWidget(); bar.setObjectName("PSTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Profile Settings")); title.setObjectName("PSTitle")
        hb.addWidget(title); hb.addStretch(1)
        close = close_button(pal=p)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll
        body = QWidget(); body.setObjectName("PSBody")
        self._rows_layout = QVBoxLayout(body)
        self._rows_layout.setContentsMargins(8, 8, 8, 8)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    # -- profile helpers (neutral, ported from the Tk overlay) --------------
    def _get_profile_dir(self, profile: str) -> Path:
        from Utils.game_helpers import _GAMES
        game = _GAMES.get(self._game_name)
        if game is not None:
            return game.get_profile_root() / "profiles" / profile
        from Utils.config_paths import get_profiles_dir
        return get_profiles_dir() / self._game_name / "profiles" / profile

    def _profiles(self) -> list[str]:
        from Utils.game_helpers import _profiles_for_game
        return _profiles_for_game(self._game_name)

    def _is_original_default(self, profile: str) -> bool:
        if profile == "default":
            return True
        return self._is_original_default_dir(self._get_profile_dir(profile))

    def _is_original_default_dir(self, profile_dir: Path) -> bool:
        try:
            return bool(read_profile_settings(profile_dir, None)
                        .get("original_default", False))
        except Exception:
            return False

    def _is_profile_locked(self, profile: str) -> bool:
        if self._is_original_default(profile):
            return True
        try:
            return bool(read_profile_settings(self._get_profile_dir(profile), None)
                        .get("profile_locked", False))
        except Exception:
            return False

    def _mark_original_default(self, profile_dir: Path):
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            merge_profile_settings(profile_dir, {"original_default": True})
        except Exception:
            pass

    # -- list ---------------------------------------------------------------
    def _populate_list(self):
        # Tear down existing rows (leak-safe - Qt won't auto-destroy like Tk).
        # Keep the trailing stretch (the last layout item).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rename_row = None
        self._rename_target = None
        self._rename_edit = None
        for i, profile in enumerate(self._profiles()):
            self._rows_layout.insertWidget(i, self._build_row(profile, i))

    def _build_row(self, profile: str, i: int) -> QFrame:
        p = active_palette()
        is_default = self._is_original_default(profile)
        is_active = profile == self._current_profile
        is_locked = self._is_profile_locked(profile)

        row = QFrame()
        row.setObjectName("ProfileRow")
        row.setProperty("alt", "true" if i % 2 else "false")
        row.setFixedHeight(44)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 4, 10, 4)
        rl.setSpacing(8)

        # (1) Lock toggle - bordered box, gold lock.png when locked (default
        # profile can't be toggled). Drawn directly for reliable icon rendering.
        lock = _LockBox(is_locked, not is_default,
                        lambda pr=profile: self._toggle_lock(pr))
        rl.addWidget(lock)

        # (2) Name label. profile is the FOLDER NAME (the row's key for every
        # button below); only the shown text is translated.
        text = profile_display(profile)
        if is_default:
            text += self.tr("  (default)")
        if is_active:
            text += "  ★"
        name = QLabel(text)
        if is_active:
            name.setStyleSheet(f"color:{_c(p,'ACCENT')}; font-weight:bold;")
        else:
            name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        rl.addWidget(name, 1)

        # (3) Buttons - Rename / Open / Remove (NO Steam Cmd).
        rename = QPushButton(self.tr("Rename"))
        rename.setObjectName("FormButton")
        rename.setCursor(Qt.PointingHandCursor)
        rename.clicked.connect(lambda _=False, pr=profile, rw=row:
                               self._show_rename(pr, rw))
        rl.addWidget(rename)

        openb = QPushButton(self.tr("Open"))
        openb.setObjectName("FormButton")
        openb.setCursor(Qt.PointingHandCursor)
        openb.clicked.connect(lambda _=False, pr=profile:
                              self._open_profile_folder(pr))
        rl.addWidget(openb)

        remove = QPushButton(self.tr("Remove"))
        remove.setObjectName("DangerButton")
        remove.setEnabled(not is_default and not is_locked)
        if remove.isEnabled():
            remove.setCursor(Qt.PointingHandCursor)
        remove.clicked.connect(lambda _=False, pr=profile: self._on_remove(pr))
        rl.addWidget(remove)

        return row

    # -- lock ---------------------------------------------------------------
    def _toggle_lock(self, profile: str):
        new_locked = not self._is_profile_locked(profile)
        try:
            merge_profile_settings(self._get_profile_dir(profile),
                                   {"profile_locked": new_locked})
        except Exception as e:
            self._log(f"Could not save lock state: {e}")
            return
        self._populate_list()
        self._on_profiles_changed()

    # -- open ---------------------------------------------------------------
    def _open_profile_folder(self, profile: str):
        folder = self._get_profile_dir(profile)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        xdg_open(folder, log_fn=self._log)

    # -- rename -------------------------------------------------------------
    def _show_rename(self, profile: str, row: QFrame):
        if self._rename_row is not None:
            self._rename_row.setParent(None)
            self._rename_row.deleteLater()
            self._rename_row = None
        self._rename_target = profile

        p = active_palette()
        bar = QFrame()
        bar.setObjectName("RenameBar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 6, 12, 6)
        hb.setSpacing(6)
        lbl = QLabel(self.tr("Rename '{0}' to:").format(profile_display(profile)))
        lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        hb.addWidget(lbl)
        # Seeded with the FOLDER name, not the display name - what the user
        # edits here becomes the new folder.
        edit = QLineEdit(profile)
        edit.setFixedWidth(200)
        edit.selectAll()
        edit.returnPressed.connect(self._do_rename)
        # Escape cancels (mirror new_profile_bar).
        edit.installEventFilter(self)
        hb.addWidget(edit)
        self._rename_edit = edit
        ok = QPushButton(self.tr("OK"))
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._do_rename)
        hb.addWidget(ok)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._cancel_rename)
        hb.addWidget(cancel)
        hb.addStretch(1)

        idx = self._rows_layout.indexOf(row)
        self._rows_layout.insertWidget(idx + 1, bar)
        self._rename_row = bar
        edit.setFocus()
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(bar))

    def _cancel_rename(self):
        if self._rename_row is not None:
            self._rename_row.setParent(None)
            self._rename_row.deleteLater()
            self._rename_row = None
        self._rename_target = None
        self._rename_edit = None

    def _do_rename(self):
        if self._rename_edit is None or self._rename_target is None:
            return
        old_name = self._rename_target
        new_name = self._rename_edit.text().strip()

        if not new_name:
            self._log("Profile name cannot be empty.")
            return
        if new_name == old_name:
            self._cancel_rename()
            return
        # Reject the folder name AND its translation: a profile literally named
        # "Standard" under German would draw identically to the default one.
        if is_reserved_profile_name(new_name):
            self._log("Cannot rename to 'default'.")
            return
        if new_name in self._profiles():
            self._log(f"Profile '{new_name}' already exists.")
            return

        profile_dir = self._get_profile_dir(old_name)
        new_dir = profile_dir.parent / new_name
        was_original_default = (old_name == "default"
                                or self._is_original_default_dir(profile_dir))
        try:
            profile_dir.rename(new_dir)
        except OSError as e:
            self._log(f"Rename failed: {e}")
            return

        if was_original_default:
            self._mark_original_default(new_dir)

        self._log(f"Profile '{old_name}' renamed to '{new_name}'.")
        if old_name == self._current_profile:
            self._current_profile = new_name

        self._cancel_rename()
        self._populate_list()
        self._on_profile_renamed(old_name, new_name)

    def _overlay_host(self):
        """Top-level window to host the confirm overlays. When this view lives in
        a scoped tab, ``self.window()`` is the app window; when it's built headless
        for the profile dropdown's Remove action (never shown), fall back to the
        app window directly so the overlay attaches to a visible widget."""
        w = self.window()
        if w is self or not w.isVisible():
            return self._window
        return w

    # -- remove -------------------------------------------------------------
    def _on_remove(self, profile: str):
        if self._is_original_default(profile) or self._is_profile_locked(profile):
            return
        from gui_qt.confirm_overlay import ConfirmOverlay
        from Utils.game_helpers import _GAMES
        game = _GAMES.get(self._game_name)

        # Deleting a MEMBER of a group that is currently deployed would leave
        # the game full of dangling links - require a restore first.
        try:
            from Utils.profile_groups import is_group, member_of_groups
            target_is_group = is_group(self._get_profile_dir(profile))
            if game is not None and not target_is_group:
                groups = member_of_groups(game, profile)
                if groups and game.get_deploy_active() \
                        and game.get_last_deployed_profile() in groups:
                    self._notify(self.tr(
                        "'{0}' is a member of the deployed group '{1}' - "
                        "restore the game first, then remove it.").format(
                            profile, game.get_last_deployed_profile()),
                        "warning")
                    return
        except Exception:
            target_is_group = False

        def after_first(ok: bool):
            if not ok:
                return
            profile_dir = self._get_profile_dir(profile)
            is_deployed = bool(
                game is not None and game.is_configured()
                and game.get_deploy_active()
                and game.get_last_deployed_profile() == profile)

            def proceed():
                self._start_remove_worker(profile, is_deployed)

            if target_is_group:
                # A group's mods/ holds only symlinks - deleting it never
                # touches member profiles' files, so no scary second confirm.
                ConfirmOverlay.show_over(
                    self._overlay_host(), "Remove Group",
                    f"'{profile}' is a profile group.\n\n"
                    "Only the group itself is removed - its member profiles "
                    "and their mods are untouched. Continue?",
                    on_done=lambda ok2: proceed() if ok2 else None,
                    confirm_label=self.tr("Remove"))
            elif profile_dir.is_dir() and profile_uses_specific_mods(profile_dir):
                ConfirmOverlay.show_over(
                    self._overlay_host(), "Remove Profile",
                    f"The '{profile}' profile has profile-specific mods.\n\n"
                    "Removing it will permanently delete its installed mods and "
                    "modlist. Continue?",
                    on_done=lambda ok2: proceed() if ok2 else None,
                    confirm_label=self.tr("Remove"))
            else:
                proceed()

        ConfirmOverlay.show_over(
            self._overlay_host(), "Remove Profile",
            f"Are you sure you want to remove the '{profile}' profile?\n\n"
            "The game will be restored first if this profile is deployed.",
            on_done=after_first, confirm_label=self.tr("Remove"))

    def _start_remove_worker(self, profile: str, is_deployed: bool):
        win = self._window
        # Coordinate with the app's deploy/restore mutex.
        if getattr(win, "_deploy_running", False):
            self._notify(self.tr("A deploy is in progress - try again shortly."), "warning")
            return
        profile_dir = self._get_profile_dir(profile)
        if is_deployed:
            win._deploy_running = True
            win._op_is_restore = True
            win._op_title = "Restoring"
            try:
                win._set_deploy_buttons_enabled(False)
                win._ensure_feedback()
            except Exception:
                pass

        from Utils.game_helpers import _GAMES
        game = _GAMES.get(self._game_name)

        def worker():
            ok = True
            try:
                if is_deployed and game is not None:
                    self._restore_before_remove(game, profile_dir)
                if profile_dir.is_dir():
                    shutil.rmtree(profile_dir)
            except Exception as exc:
                ok = False
                self._log(f"Remove failed: {exc}")
            finally:
                win._deploy_running = False
                safe_emit(self._remove_finished, profile, ok)

        threading.Thread(target=worker, daemon=True, name="profile-remove").start()

    def _restore_before_remove(self, game, profile_dir: Path):
        """Restore the deployed game folder for *profile_dir* before deleting it.
        Mirrors the app's _on_restore sequence (borrows the window's op signals
        for the progress popup)."""
        win = self._window
        from Utils.deploy import restore_root_folder_for_game
        # Remember the profile we're actually on so the finally block restores to
        # it - restoring to None (default) would desync the game object from the
        # selected profile and make later path-derived opens resolve wrong.
        prev_profile_dir = getattr(game, "_active_profile_dir", None)
        game.set_active_profile_dir(profile_dir)
        game.load_paths()
        try:
            game_root = game.get_game_path()
            if hasattr(game, "restore"):
                game.restore(
                    log_fn=lambda m: win._op_log.emit(str(m)),
                    progress_fn=lambda d, t, ph=None: win._op_progress.emit(d, t, ph))
            rf = game.get_effective_root_folder_path()
            if rf.is_dir() and game_root:
                restore_root_folder_for_game(
                    game, root_folder_dir=rf, game_root=game_root,
                    log_fn=lambda m: win._op_log.emit(str(m)),
                )
        finally:
            game.set_active_profile_dir(prev_profile_dir)
            game.load_paths()

    def _on_remove_finished(self, profile: str, ok: bool):
        win = self._window
        try:
            win._set_deploy_buttons_enabled(True)
        except Exception:
            pass
        popup = getattr(win, "_progress_popup", None)
        if popup is not None:
            QTimer.singleShot(1200, popup.clear)
        if ok:
            self._log(f"Profile '{profile}' removed.")
            if profile == self._current_profile:
                self._current_profile = "default"
        self._populate_list()
        if ok:
            self._on_profile_removed(profile)
            self._notify(self.tr("Profile '{0}' removed").format(profile), "info")

    # -- misc ---------------------------------------------------------------
    def _close(self):
        """Dismiss the panel - routes through the window's tab manager so the
        scoped tab tears down (modlist panel stack resets to page 0). Falls back
        to hiding if the tab manager isn't reachable (e.g. detached)."""
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("profile_settings")
                if getattr(self._window, "_profile_settings_view", None) is self:
                    self._window._profile_settings_view = None
                return
            except Exception:
                pass
        self.hide()

    def set_current_profile(self, name: str):
        """Keep the ★ marker in sync if the top selector changes while open."""
        if name != self._current_profile:
            self._current_profile = name
            self._populate_list()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)

    def eventFilter(self, obj, event):
        # Escape in the rename field cancels the inline bar.
        from PySide6.QtCore import QEvent
        if (obj is self._rename_edit and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Escape):
            self._cancel_rename()
            return True
        return super().eventFilter(obj, event)
