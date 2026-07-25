"""Manage Profile Groups — a modlist-scoped tab (same hosting pattern as
ProfileSettingsView) for creating/renaming/removing Profile Groups and editing
their ordered member-profile list.

A Profile Group is a named, reusable combination of profiles that deploy
together as one merged virtual profile (see Utils/profile_groups.py). This
view only manages *composition* (name, member order); the merged mod list
itself is regenerated automatically before every deploy and whenever the
group becomes the active selection — there is nothing to edit here about
individual mods.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFrame, QScrollArea, QCheckBox,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button, contrast_text
from Utils.profile_groups import (
    GroupValidationError, add_member, create_group, get_members, list_groups,
    move_member, remove_member,
)
from Utils.profile_state import profile_uses_specific_mods
from Utils.profile_groups import is_group as _is_group


class ProfileGroupsView(QWidget):
    """Hosted as a modlist-scoped tab. Callbacks let the app refresh the
    profile selector when a group is created/renamed/removed."""

    def __init__(self, window, game_name: str, on_groups_changed=None, log_fn=None):
        super().__init__()
        self._window = window
        self._game_name = game_name
        self._on_groups_changed = on_groups_changed or (lambda: None)
        self._log = log_fn or (lambda _m: None)

        self._expanded: set[str] = set()
        self._create_open = False
        self._create_members: dict[str, bool] = {}

        self.setObjectName("ProfileGroupsView")
        self._build()
        self._populate()

    # -- game/profile helpers ------------------------------------------------
    def _game(self):
        from Utils.game_helpers import _GAMES
        return _GAMES.get(self._game_name)

    def _profile_dir(self, name: str) -> Path:
        game = self._game()
        from Utils.config_paths import get_profiles_dir
        root = (game.get_profile_root() if game is not None
                else get_profiles_dir() / self._game_name)
        return root / "profiles" / name

    def _eligible_members(self) -> list[str]:
        """Profiles that can be a group member: not a group themselves, and not
        profile-specific-mods (which have their own private staging pool)."""
        from Utils.game_helpers import _profiles_for_game
        out = []
        for name in _profiles_for_game(self._game_name):
            d = self._profile_dir(name)
            if _is_group(d):
                continue
            if profile_uses_specific_mods(d):
                continue
            out.append(name)
        return out

    def _overlay_host(self):
        w = self.window()
        if w is self or not w.isVisible():
            return self._window
        return w

    # -- construction ---------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #ProfileGroupsView {{ background: {c('BG_DEEP')}; }}
        #PGTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #PGTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        QScrollArea {{ background: {c('BG_DEEP')}; border: none; }}
        #PGBody {{ background: {c('BG_DEEP')}; }}
        #GroupRow {{ background: {c('BG_PANEL')}; }}
        #GroupRow[alt="true"] {{ background: {c('BG_DEEP')}; }}
        #MemberPanel {{ background: {c('BG_HEADER')}; border-radius: 4px; }}
        #DangerButton {{ background: {c('BTN_DANGER')}; color: {contrast_text(c('BTN_DANGER'))}; border: none;
                         border-radius: 4px; padding: 4px 12px; font-size: 12px;
                         font-weight: 600; }}
        #DangerButton:hover {{ background: {c('BTN_DANGER_HOV')}; }}
        """

    def _build(self):
        p = active_palette()
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget(); bar.setObjectName("PGTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Manage Profile Groups")); title.setObjectName("PGTitle")
        hb.addWidget(title); hb.addStretch(1)
        new_btn = QPushButton(self.tr("+ New Group"))
        new_btn.setObjectName("PrimaryButton")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._toggle_create)
        hb.addWidget(new_btn)
        close = danger_close_button(pal=p)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll
        body = QWidget(); body.setObjectName("PGBody")
        self._rows_layout = QVBoxLayout(body)
        self._rows_layout.setContentsMargins(8, 8, 8, 8)
        self._rows_layout.setSpacing(6)
        self._rows_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    # -- list ------------------------------------------------------------------
    def _clear_rows(self):
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _populate(self):
        self._clear_rows()
        game = self._game()
        groups = list_groups(game) if game is not None else []

        i = 0
        if self._create_open:
            self._rows_layout.insertWidget(i, self._build_create_panel())
            i += 1

        if not groups:
            empty = QLabel(self.tr(
                "No Profile Groups yet. A Profile Group combines several "
                "profiles (e.g. \"QoL\" + \"Decor\") into one deployable, "
                "merged mod list — click \"+ New Group\" to create one."))
            empty.setWordWrap(True)
            p = active_palette()
            empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding: 12px;")
            self._rows_layout.insertWidget(i, empty)
            i += 1

        for idx, name in enumerate(groups):
            self._rows_layout.insertWidget(i, self._build_group_row(name, idx))
            i += 1
            if name in self._expanded:
                self._rows_layout.insertWidget(i, self._build_member_panel(name))
                i += 1

    def _build_group_row(self, name: str, i: int) -> QFrame:
        p = active_palette()
        members = get_members(self._profile_dir(name))

        row = QFrame()
        row.setObjectName("GroupRow")
        row.setProperty("alt", "true" if i % 2 else "false")
        row.setFixedHeight(44)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 4, 10, 4)
        rl.setSpacing(8)

        label = QLabel(f"{name}  ({len(members)} profile{'s' if len(members) != 1 else ''})")
        label.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        rl.addWidget(label, 1)

        edit = QPushButton(self.tr("Hide members") if name in self._expanded
                            else self.tr("Edit members"))
        edit.setObjectName("FormButton")
        edit.setCursor(Qt.PointingHandCursor)
        edit.clicked.connect(lambda _=False, n=name: self._toggle_expand(n))
        rl.addWidget(edit)

        remove = QPushButton(self.tr("Remove"))
        remove.setObjectName("DangerButton")
        remove.setCursor(Qt.PointingHandCursor)
        remove.clicked.connect(lambda _=False, n=name: self._on_remove_group(n))
        rl.addWidget(remove)

        return row

    def _toggle_expand(self, name: str):
        if name in self._expanded:
            self._expanded.discard(name)
        else:
            self._expanded.add(name)
        self._populate()

    # -- member editing ---------------------------------------------------------
    def _build_member_panel(self, name: str) -> QFrame:
        p = active_palette()
        panel = QFrame()
        panel.setObjectName("MemberPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 8, 12, 8)
        v.setSpacing(4)

        hint = QLabel(self.tr(
            "Top = highest priority. When two member profiles both change the "
            "same file, the one closer to the top wins."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size: 11px;")
        v.addWidget(hint)

        members = get_members(self._profile_dir(name))
        for idx, member in enumerate(members):
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel(f"{idx + 1}. {member}")
            lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            row.addWidget(lbl, 1)

            up = QPushButton("▲")
            up.setFixedWidth(28)
            up.setEnabled(idx > 0)
            up.setCursor(Qt.PointingHandCursor)
            up.clicked.connect(lambda _=False, n=name, m=member, ix=idx:
                               self._move(n, m, ix - 1))
            row.addWidget(up)

            down = QPushButton("▼")
            down.setFixedWidth(28)
            down.setEnabled(idx < len(members) - 1)
            down.setCursor(Qt.PointingHandCursor)
            down.clicked.connect(lambda _=False, n=name, m=member, ix=idx:
                                 self._move(n, m, ix + 1))
            row.addWidget(down)

            rm = QPushButton(self.tr("Remove"))
            rm.setObjectName("FormButton")
            rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(lambda _=False, n=name, m=member: self._remove_member(n, m))
            row.addWidget(rm)

            v.addLayout(row)

        # Add-member row: eligible profiles not already in this group.
        current = set(members)
        addable = [n for n in self._eligible_members() if n not in current and n != name]
        if addable:
            add_row = QHBoxLayout()
            add_row.setSpacing(6)
            from PySide6.QtWidgets import QComboBox
            combo = QComboBox()
            combo.addItems(addable)
            add_row.addWidget(combo, 1)
            add_btn = QPushButton(self.tr("+ Add"))
            add_btn.setObjectName("FormButton")
            add_btn.setCursor(Qt.PointingHandCursor)
            add_btn.clicked.connect(
                lambda _=False, n=name, cb=combo: self._add_member(n, cb.currentText()))
            add_row.addWidget(add_btn)
            v.addLayout(add_row)
        elif not members:
            none_lbl = QLabel(self.tr("No eligible profiles to add — create another "
                                       "profile first (profile-specific-mods profiles "
                                       "and other groups can't be members)."))
            none_lbl.setWordWrap(True)
            none_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            v.addWidget(none_lbl)

        return panel

    def _move(self, group_name: str, member: str, new_index: int):
        move_member(self._profile_dir(group_name), member, new_index)
        self._populate()
        self._on_groups_changed()

    def _remove_member(self, group_name: str, member: str):
        remove_member(self._profile_dir(group_name), member)
        self._log(f"Removed '{member}' from group '{group_name}'.")
        self._populate()
        self._on_groups_changed()

    def _add_member(self, group_name: str, member: str):
        if not member:
            return
        try:
            add_member(self._game(), self._profile_dir(group_name), member)
        except GroupValidationError as e:
            self._notify(str(e), "warning")
            return
        self._log(f"Added '{member}' to group '{group_name}'.")
        self._populate()
        self._on_groups_changed()

    # -- create -----------------------------------------------------------------
    def _toggle_create(self):
        self._create_open = not self._create_open
        if self._create_open:
            self._create_members = {n: False for n in self._eligible_members()}
            self._create_check_order: list[str] = []
        self._populate()

    def _build_create_panel(self) -> QFrame:
        p = active_palette()
        panel = QFrame()
        panel.setObjectName("MemberPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 10, 12, 10)
        v.setSpacing(6)

        title = QLabel(self.tr("New Profile Group"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        v.addWidget(title)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(self.tr("Name:")))
        name_edit = QLineEdit()
        name_edit.setFixedWidth(220)
        self._create_name_edit = name_edit
        name_row.addWidget(name_edit)
        name_row.addStretch(1)
        v.addLayout(name_row)

        members_lbl = QLabel(self.tr(
            "Members — check in priority order: the FIRST one you check wins "
            "any conflict between the profiles (you can still reorder later "
            "via \"Edit members\"):"))
        members_lbl.setWordWrap(True)
        members_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        v.addWidget(members_lbl)

        order_preview = QLabel("")
        order_preview.setWordWrap(True)
        order_preview.setStyleSheet(f"color:{_c(p,'ACCENT')}; font-size: 11px;")
        self._create_order_preview = order_preview
        v.addWidget(order_preview)

        eligible = self._eligible_members()
        if not eligible:
            none_lbl = QLabel(self.tr("No eligible profiles yet — create at least one "
                                       "regular profile first."))
            none_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            v.addWidget(none_lbl)
        self._create_checks: dict[str, QCheckBox] = {}
        for name in eligible:
            cb = QCheckBox(name)
            cb.setChecked(self._create_members.get(name, False))
            cb.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            cb.toggled.connect(lambda checked, n=name: self._on_create_check_toggled(n, checked))
            self._create_checks[name] = cb
            v.addWidget(cb)
        self._update_create_order_preview()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._toggle_create)
        btn_row.addWidget(cancel)
        create = QPushButton(self.tr("Create"))
        create.setObjectName("PrimaryButton")
        create.setCursor(Qt.PointingHandCursor)
        create.clicked.connect(self._do_create)
        btn_row.addWidget(create)
        v.addLayout(btn_row)

        return panel

    def _on_create_check_toggled(self, name: str, checked: bool):
        order = getattr(self, "_create_check_order", [])
        if checked:
            if name not in order:
                order.append(name)
        else:
            if name in order:
                order.remove(name)
        self._create_check_order = order
        self._update_create_order_preview()

    def _update_create_order_preview(self):
        lbl = getattr(self, "_create_order_preview", None)
        if lbl is None:
            return
        order = getattr(self, "_create_check_order", [])
        if not order:
            lbl.setText(self.tr("Priority order: (none checked yet)"))
        else:
            lbl.setText(self.tr("Priority order (highest first): {0}").format(
                "  >  ".join(order)))

    def _do_create(self):
        name = (self._create_name_edit.text() or "").strip()
        if not name:
            self._notify(self.tr("Group name cannot be empty."), "warning")
            return
        # Priority order follows the order profiles were checked in (first
        # checked = highest priority), NOT dict/alphabetical order — a
        # silent alphabetical default previously meant the group's actual
        # conflict-resolution order didn't match what the user chose.
        members = [n for n in getattr(self, "_create_check_order", [])
                   if self._create_checks[n].isChecked()]
        if not members:
            self._notify(self.tr("Select at least one member profile."), "warning")
            return
        game = self._game()
        try:
            create_group(game, name, members)
        except GroupValidationError as e:
            self._notify(str(e), "warning")
            return
        self._log(f"Created Profile Group '{name}' ({len(members)} member(s)).")
        self._create_open = False
        self._populate()
        self._on_groups_changed()

    # -- remove -------------------------------------------------------------
    def _on_remove_group(self, name: str):
        from gui_qt.confirm_overlay import ConfirmOverlay

        def after(ok: bool):
            if not ok:
                return
            import shutil
            try:
                shutil.rmtree(self._profile_dir(name))
            except OSError as e:
                self._log(f"Remove group failed: {e}")
                return
            self._expanded.discard(name)
            self._log(f"Profile Group '{name}' removed.")
            self._populate()
            self._on_groups_changed()

        ConfirmOverlay.show_over(
            self._overlay_host(), "Remove Profile Group",
            f"Are you sure you want to remove the Profile Group '{name}'?\n\n"
            "This deletes only the merged view — its member profiles and "
            "their mods are not affected.",
            on_done=after, confirm_label=self.tr("Remove"))

    # -- misc ---------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab("profile_groups")
                if getattr(self._window, "_profile_groups_view", None) is self:
                    self._window._profile_groups_view = None
                return
            except Exception:
                pass
        self.hide()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)
