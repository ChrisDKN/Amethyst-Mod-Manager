"""Profile Groups - a modlist-scoped tab for creating and managing groups
(named, ordered combinations of profiles that deploy together as one merged
profile - see Utils/profiles/groups.py).

Layout: a list of existing groups (expandable member panel with priority
reorder / add / remove), then a create panel: group name + an ordered member
checklist where CHECK ORDER = priority order (a live preview shows it).
Shared-pool profiles are listed disabled with an inline "Convert…" action
(Utils/profiles/convert.py) since group members must be profile-specific.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.i18n import profile_display
from gui_qt.theme_qt import (
    _c, active_palette, bind_theme_icon, contrast_text, close_button,
)
from Utils.profiles import groups as pg
from Utils.profiles.groups import GroupValidationError
from Utils.profiles.state import profile_uses_specific_mods


class ProfileGroupsView(QWidget):
    """Hosted as a modlist-scoped tab (key "profile_groups")."""

    # (profile_name, ok) from the convert worker → UI thread.
    _convert_finished = Signal(str, bool)

    def __init__(self, window, game, current_profile: str,
                 on_groups_changed=None, log_fn=None):
        super().__init__()
        self._window = window
        self._game = game
        self._current_profile = current_profile
        self._on_groups_changed = on_groups_changed or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        self._expanded: str | None = None
        self._create_order: list[str] = []
        self._converting: str | None = None

        self.setObjectName("ProfileGroupsView")
        self._convert_finished.connect(self._on_convert_finished)
        self._build()
        self._populate()

    # -- construction -------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)  # noqa: E731
        return f"""
        #ProfileGroupsView {{ background: {c('BG_DEEP')}; }}
        #PGTitleBar {{ background: {c('BG_HEADER')};
                       border-bottom: 1px solid {c('BORDER')}; }}
        #PGTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        QScrollArea {{ background: {c('BG_DEEP')}; border: none; }}
        #PGBody {{ background: {c('BG_DEEP')}; }}
        #GroupRow {{ background: {c('BG_PANEL')}; border-radius: 4px; }}
        #MemberPanel {{ background: {c('BG_HEADER')}; border-radius: 4px; }}
        #PGSection {{ color: {c('TEXT_DIM')}; font-weight: 600; }}
        #PGHint {{ color: {c('TEXT_DIM')}; font-size: 12px; }}
        #DangerButton {{ background: {c('BTN_DANGER')};
                         color: {contrast_text(c('BTN_DANGER'))}; border: none;
                         border-radius: 4px; padding: 4px 12px; font-size: 12px;
                         font-weight: 600; }}
        #DangerButton:hover {{ background: {c('BTN_DANGER_HOV')}; }}
        #DangerButton:disabled {{ background: {c('BTN_GREY')};
                                  color: {c('TEXT_DIM')}; }}
        """

    def _build(self):
        p = active_palette()
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget(); bar.setObjectName("PGTitleBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Profile Groups")); title.setObjectName("PGTitle")
        hb.addWidget(title); hb.addStretch(1)
        close = close_button(pal=p)
        close.clicked.connect(self._close)
        hb.addWidget(close)
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._scroll = scroll
        body = QWidget(); body.setObjectName("PGBody")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(10, 10, 10, 10)
        self._body_layout.setSpacing(6)
        self._body_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    # -- data helpers -------------------------------------------------------
    def _profiles_dir(self) -> Path:
        return self._game.get_profile_root() / "profiles"

    def _profile_names(self) -> list[str]:
        from Utils.games.registry import _profiles_for_game
        return _profiles_for_game(self._game.name)

    def _groups(self) -> list[str]:
        try:
            return pg.list_groups(self._game)
        except Exception:
            return []

    def _eligible_members(self) -> list[str]:
        """Profile-specific, non-group profiles - valid group members."""
        out = []
        for name in self._profile_names():
            d = self._profiles_dir() / name
            if not d.is_dir() or pg.is_group(d):
                continue
            if profile_uses_specific_mods(d):
                out.append(name)
        return out

    def _shared_pool_profiles(self) -> list[str]:
        out = []
        for name in self._profile_names():
            d = self._profiles_dir() / name
            if d.is_dir() and not pg.is_group(d) \
                    and not profile_uses_specific_mods(d):
                out.append(name)
        return out

    # -- list ---------------------------------------------------------------
    def _populate(self):
        while self._body_layout.count() > 1:
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        p = active_palette()
        idx = 0

        groups = self._groups()
        if groups:
            lbl = QLabel(self.tr("Groups")); lbl.setObjectName("PGSection")
            self._body_layout.insertWidget(idx, lbl); idx += 1
            for name in groups:
                self._body_layout.insertWidget(idx, self._build_group_row(name))
                idx += 1
                if self._expanded == name:
                    self._body_layout.insertWidget(
                        idx, self._build_member_panel(name))
                    idx += 1
        else:
            hint = QLabel(self.tr(
                "No profile groups yet. A group combines several profiles "
                "and deploys them together as one merged profile."))
            hint.setObjectName("PGHint"); hint.setWordWrap(True)
            self._body_layout.insertWidget(idx, hint); idx += 1

        self._body_layout.insertWidget(idx, self._build_create_panel())
        idx += 1

        shared = self._shared_pool_profiles()
        if shared:
            lbl = QLabel(self.tr("Not eligible (shared mod pool)"))
            lbl.setObjectName("PGSection")
            self._body_layout.insertWidget(idx, lbl); idx += 1
            hint = QLabel(self.tr(
                "Group members need profile-specific mods so the group only "
                "sees mods deliberately added to them. Convert copies a "
                "profile's mods into its own folder (hardlinked where "
                "possible - no extra disk on the same filesystem); the "
                "shared pool and other profiles are untouched."))
            hint.setObjectName("PGHint"); hint.setWordWrap(True)
            self._body_layout.insertWidget(idx, hint); idx += 1
            for name in shared:
                self._body_layout.insertWidget(
                    idx, self._build_shared_row(name, p))
                idx += 1

    def _build_group_row(self, name: str) -> QFrame:
        p = active_palette()
        row = QFrame(); row.setObjectName("GroupRow")
        rl = QHBoxLayout(row); rl.setContentsMargins(10, 6, 10, 6); rl.setSpacing(8)
        members = pg.get_members(self._profiles_dir() / name)
        text = name
        if name == self._current_profile:
            text += "  ★"
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_c(p, 'ACCENT' if name == self._current_profile else 'TEXT_MAIN')};"
            f" font-weight: 600;")
        rl.addWidget(lbl)
        count = QLabel(self.tr("{0} member(s)").format(len(members)))
        count.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        rl.addWidget(count)
        rl.addStretch(1)

        expand = QPushButton(self.tr("Hide members") if self._expanded == name
                             else self.tr("Members"))
        expand.setObjectName("FormButton")
        expand.setCursor(Qt.PointingHandCursor)
        expand.clicked.connect(lambda _=False, n=name: self._toggle_expand(n))
        rl.addWidget(expand)

        remove = QPushButton(self.tr("Remove"))
        remove.setObjectName("DangerButton")
        remove.setCursor(Qt.PointingHandCursor)
        remove.clicked.connect(lambda _=False, n=name: self._remove_group(n))
        rl.addWidget(remove)
        return row

    def _build_member_panel(self, group_name: str) -> QFrame:
        p = active_palette()
        panel = QFrame(); panel.setObjectName("MemberPanel")
        pl = QVBoxLayout(panel); pl.setContentsMargins(16, 8, 10, 8); pl.setSpacing(4)
        gdir = self._profiles_dir() / group_name
        members = pg.get_members(gdir)

        for i, member in enumerate(members):
            row = QHBoxLayout(); row.setSpacing(6)
            num = QLabel(f"{i + 1}.")
            num.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; min-width: 18px;")
            row.addWidget(num)
            missing = not (self._profiles_dir() / member).is_dir()
            lbl = QLabel(profile_display(member)
                         + (self.tr("  (missing)") if missing else ""))
            lbl.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')};")
            row.addWidget(lbl, 1)
            # Theme-tinted arrow.png chevrons (the glyph font has no ▲/▼ here).
            up = QPushButton(); up.setFixedSize(26, 24)
            bind_theme_icon(up, "arrow.png", 12, "TEXT_MAIN", degrees=180)
            up.setToolTip(self.tr("Move up"))
            up.setEnabled(i > 0)
            up.clicked.connect(lambda _=False, g=group_name, m=member, j=i:
                               self._move_member(g, m, j - 1))
            row.addWidget(up)
            down = QPushButton(); down.setFixedSize(26, 24)
            bind_theme_icon(down, "arrow.png", 12, "TEXT_MAIN", degrees=0)
            down.setToolTip(self.tr("Move down"))
            down.setEnabled(i < len(members) - 1)
            down.clicked.connect(lambda _=False, g=group_name, m=member, j=i:
                                 self._move_member(g, m, j + 1))
            row.addWidget(down)
            rm = QPushButton(self.tr("Remove"))
            rm.setObjectName("FormButton")
            rm.clicked.connect(lambda _=False, g=group_name, m=member:
                               self._remove_member(g, m))
            row.addWidget(rm)
            pl.addLayout(row)

        hint = QLabel(self.tr("1 = highest priority (its mods win conflicts)"))
        hint.setObjectName("PGHint")
        pl.addWidget(hint)

        addable = [m for m in self._eligible_members() if m not in members]
        if addable:
            row = QHBoxLayout(); row.setSpacing(6)
            combo = QComboBox()
            # Folder name as item data - _add_member takes a real profile name.
            for _m in addable:
                combo.addItem(profile_display(_m), _m)
            row.addWidget(combo, 1)
            add = QPushButton(self.tr("+ Add member"))
            add.setObjectName("FormButton")
            add.setCursor(Qt.PointingHandCursor)
            add.clicked.connect(lambda _=False, g=group_name, cb=combo:
                                self._add_member(g, cb.currentData()))
            row.addWidget(add)
            pl.addLayout(row)
        return panel

    def _build_create_panel(self) -> QFrame:
        p = active_palette()
        panel = QFrame(); panel.setObjectName("GroupRow")
        pl = QVBoxLayout(panel); pl.setContentsMargins(10, 8, 10, 8); pl.setSpacing(6)
        lbl = QLabel(self.tr("New group")); lbl.setObjectName("PGSection")
        pl.addWidget(lbl)

        row = QHBoxLayout(); row.setSpacing(6)
        row.addWidget(QLabel(self.tr("Name:")))
        self._create_name_edit = QLineEdit()
        self._create_name_edit.setFixedWidth(220)
        row.addWidget(self._create_name_edit)
        row.addStretch(1)
        pl.addLayout(row)

        eligible = self._eligible_members()
        self._create_checks: dict[str, QCheckBox] = {}
        self._create_order = [n for n in self._create_order if n in eligible]
        if not eligible:
            hint = QLabel(self.tr(
                "No eligible member profiles yet - create a profile with "
                "profile-specific mods, or convert one below."))
            hint.setObjectName("PGHint"); hint.setWordWrap(True)
            pl.addWidget(hint)
        for name in eligible:
            cb = QCheckBox(profile_display(name))
            cb.setChecked(name in self._create_order)
            cb.toggled.connect(lambda on, n=name: self._on_create_check(n, on))
            self._create_checks[name] = cb
            pl.addWidget(cb)

        self._create_order_preview = QLabel("")
        self._create_order_preview.setObjectName("PGHint")
        self._create_order_preview.setWordWrap(True)
        pl.addWidget(self._create_order_preview)
        self._update_order_preview()

        # Overwrite/Root_Folder merge choice: profiles with runtime files get
        # an include-checkbox (default on). Applied to checked MEMBERS only.
        self._ow_checks: dict[str, QCheckBox] = {}
        try:
            contributors = pg.runtime_dir_contributors(
                self._profiles_dir(), eligible)
        except Exception:
            contributors = []
        if contributors:
            lbl2 = QLabel(self.tr("Merge overwrite / Root Folder files from:"))
            lbl2.setObjectName("PGSection")
            pl.addWidget(lbl2)
            hint2 = QLabel(self.tr(
                "These profiles have runtime-generated files (overwrite / "
                "Root Folder). Checked profiles' files are copied into the "
                "group; conflicts use the higher-priority member's copy."))
            hint2.setObjectName("PGHint"); hint2.setWordWrap(True)
            pl.addWidget(hint2)
            for name in contributors:
                cb = QCheckBox(profile_display(name))
                cb.setChecked(True)
                self._ow_checks[name] = cb
                pl.addWidget(cb)

        row = QHBoxLayout(); row.addStretch(1)
        create = QPushButton(self.tr("Create group"))
        create.setObjectName("PrimaryButton")
        create.setCursor(Qt.PointingHandCursor)
        create.clicked.connect(self._create_group)
        row.addWidget(create)
        pl.addLayout(row)
        return panel

    def _build_shared_row(self, name: str, p) -> QFrame:
        row = QFrame(); row.setObjectName("GroupRow")
        rl = QHBoxLayout(row); rl.setContentsMargins(10, 4, 10, 4); rl.setSpacing(8)
        lbl = QLabel(profile_display(name))
        lbl.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')};")
        rl.addWidget(lbl, 1)
        conv = QPushButton(self.tr("Converting…") if self._converting == name
                           else self.tr("Convert to profile-specific…"))
        conv.setObjectName("FormButton")
        conv.setEnabled(self._converting is None)
        if conv.isEnabled():
            conv.setCursor(Qt.PointingHandCursor)
        conv.clicked.connect(lambda _=False, n=name: self._convert_profile(n))
        rl.addWidget(conv)
        return row

    # -- create-order tracking ---------------------------------------------
    def _on_create_check(self, name: str, on: bool):
        if on and name not in self._create_order:
            self._create_order.append(name)
        elif not on and name in self._create_order:
            self._create_order.remove(name)
        self._update_order_preview()

    def _update_order_preview(self):
        if self._create_order:
            order = "  →  ".join(f"{i + 1}. {n}"
                                 for i, n in enumerate(self._create_order))
            self._create_order_preview.setText(
                self.tr("Priority (check order): {0}").format(order))
        else:
            self._create_order_preview.setText(
                self.tr("Check member profiles in priority order "
                        "(first checked = highest priority)."))

    # -- actions ------------------------------------------------------------
    def _edits_blocked(self, group_name: str) -> bool:
        """Refuse group mutations while a deploy runs or when the group is the
        DEPLOYED profile (dropping links under a live symlink deploy leaves
        the game dir dangling)."""
        if getattr(self._window, "_deploy_running", False) \
                or getattr(self._window, "_install_running", False):
            self._notify(self.tr("An install or deploy is in progress - try "
                                 "again shortly."), "warning")
            return True
        try:
            if self._game.get_deploy_active() \
                    and self._game.get_last_deployed_profile() == group_name:
                self._notify(self.tr(
                    "'{0}' is currently deployed - restore the game first, "
                    "then edit the group.").format(group_name), "warning")
                return True
        except Exception:
            pass
        return False

    def _toggle_expand(self, name: str):
        self._expanded = None if self._expanded == name else name
        self._populate()

    def _create_group(self):
        name = self._create_name_edit.text().strip()
        if not name:
            self._notify(self.tr("Enter a group name."), "warning")
            return
        if not self._create_order:
            self._notify(self.tr("Check at least one member profile."), "warning")
            return
        # Several members with a same-named profile-specific INI: ask whose
        # copy the group inherits before creating (the group then owns its
        # copies - they're copied, not linked).
        try:
            contributors, conflicts = pg.profile_ini_conflicts(
                self._game, self._profiles_dir(), list(self._create_order))
        except Exception:
            contributors, conflicts = [], []
        if conflicts and len(contributors) > 1:
            from gui_qt.list_picker_overlay import ListPickerOverlay

            def picked(member):
                if not member:
                    self._notify(self.tr("Group creation cancelled."), "info")
                    return
                self._do_create_group(name, ini_source=member)

            shown = ", ".join(conflicts[:4]) + ("…" if len(conflicts) > 4 else "")
            ListPickerOverlay(
                self._overlay_host(),
                self.tr("Several members have profile-specific INIs with the "
                        "same name ({0}).\nWhich profile's INI files should "
                        "the group use for those?").format(shown),
                [(m, m) for m in contributors], picked,
                select_label=self.tr("Use these INIs"))
            return
        self._do_create_group(name)

    def _do_create_group(self, name: str, ini_source: "str | None" = None):
        # Unchecked "merge overwrite from" boxes → exclusions (members only).
        excluded = [m for m in self._create_order
                    if m in self._ow_checks
                    and not self._ow_checks[m].isChecked()]
        try:
            pg.create_group(self._game, name, list(self._create_order),
                            ini_source=ini_source,
                            overwrite_excluded=excluded or None,
                            log_fn=self._log)
        except GroupValidationError as exc:
            self._notify(str(exc), "warning")
            return
        except Exception as exc:
            self._notify(self.tr("Could not create group: {0}").format(exc),
                         "error")
            return
        self._log(f"Profile Group '{name}' created "
                  f"({', '.join(self._create_order)}).")
        self._create_order = []
        self._create_name_edit.clear()
        self._expanded = name
        self._populate()
        self._on_groups_changed()

    def _remove_group(self, name: str):
        from gui_qt.confirm_overlay import ConfirmOverlay

        def done(ok: bool):
            if not ok:
                return
            if self._expanded == name:
                self._expanded = None
            # Delegate to the Profile Settings removal worker: it restores
            # the game first when this group is the deployed profile, shows
            # the progress popup, and its finish callback runs the app's
            # _on_profile_removed (active-profile fallback + selector), which
            # also repopulates this tab.
            view = self._window._make_profile_settings_view()
            self._window._profile_remove_helper = view   # keep alive (async)
            is_deployed = False
            try:
                is_deployed = bool(
                    self._game.get_deploy_active()
                    and self._game.get_last_deployed_profile() == name)
            except Exception:
                pass
            view._start_remove_worker(name, is_deployed)

        ConfirmOverlay.show_over(
            self._overlay_host(), self.tr("Remove Group"),
            self.tr("Remove the profile group '{0}'?\n\nOnly the group itself "
                    "is deleted - its member profiles and their mods are "
                    "untouched. The game will be restored first if this group "
                    "is deployed.").format(name),
            on_done=done, confirm_label=self.tr("Remove"))

    def _add_member(self, group: str, member: str):
        if not member or self._edits_blocked(group):
            return
        try:
            pg.add_member(self._game, self._profiles_dir() / group, member,
                          log_fn=self._log)
        except GroupValidationError as exc:
            self._notify(str(exc), "warning")
            return
        self._populate()
        self._on_groups_changed()

    def _remove_member(self, group: str, member: str):
        if self._edits_blocked(group):
            return
        try:
            pg.remove_member(self._game, self._profiles_dir() / group, member,
                             log_fn=self._log)
        except Exception as exc:
            self._notify(str(exc), "warning")
            return
        self._populate()
        self._on_groups_changed()

    def _move_member(self, group: str, member: str, new_index: int):
        if self._edits_blocked(group):
            return
        try:
            pg.move_member(self._game, self._profiles_dir() / group, member,
                           new_index, log_fn=self._log)
        except Exception as exc:
            self._notify(str(exc), "warning")
            return
        self._populate()
        self._on_groups_changed()

    # -- convert ------------------------------------------------------------
    def _convert_profile(self, name: str):
        if self._converting is not None:
            return
        # The convert worker copies the staging tree the deploy/install flows
        # read - refuse when busy, and hold _deploy_running for the duration
        # so auto-deploy/installs queue instead of racing a half-copied tree.
        if getattr(self._window, "_deploy_running", False) \
                or getattr(self._window, "_install_running", False):
            self._notify(self.tr("An install or deploy is in progress - try "
                                 "again shortly."), "warning")
            return
        from gui_qt.confirm_overlay import ConfirmOverlay

        def done(ok: bool):
            if not ok:
                return
            self._converting = name
            self._window._deploy_running = True
            self._populate()
            pdir = self._profiles_dir() / name
            game = self._game

            def worker():
                ok2 = True
                try:
                    from Utils.profiles.convert import convert_profile_to_specific
                    convert_profile_to_specific(game, pdir, log_fn=self._log)
                except Exception as exc:
                    ok2 = False
                    self._log(f"Convert failed: {exc}")
                safe_emit(self._convert_finished, name, ok2)

            threading.Thread(target=worker, daemon=True,
                             name="profile-convert").start()

        ConfirmOverlay.show_over(
            self._overlay_host(), self.tr("Convert Profile"),
            self.tr("Convert '{0}' to profile-specific mods?\n\nIts listed "
                    "mods are copied into the profile's own mods folder "
                    "(hardlinked where possible). The shared pool and other "
                    "profiles are not changed.").format(name),
            on_done=done, confirm_label=self.tr("Convert"))

    def _on_convert_finished(self, name: str, ok: bool):
        self._converting = None
        self._window._deploy_running = False
        self._populate()
        if ok:
            self._notify(self.tr("Profile '{0}' converted - it can now join "
                                 "groups.").format(name), "info")
            if name == self._current_profile:
                # The active profile's staging location changed - reload.
                try:
                    self._window._gs.reassert_active_profile()
                    self._window._reload_modlist()
                except Exception:
                    pass
            self._on_groups_changed()
        else:
            self._notify(self.tr("Convert of '{0}' failed - see the log.")
                         .format(name), "error")

    # -- misc ---------------------------------------------------------------
    def _overlay_host(self):
        w = self.window()
        if w is self or not w.isVisible():
            return self._window
        return w

    def set_current_profile(self, name: str):
        if name != self._current_profile:
            self._current_profile = name
            self._populate()

    def _notify(self, text: str, state: str = "info"):
        n = getattr(self._window, "_notify", None)
        if callable(n):
            n(text, state)
        else:
            self._log(text)

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
