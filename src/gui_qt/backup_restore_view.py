"""Restore backup overlay - lists a profile's backups (snapshots of
modlist.txt / plugins.txt / state JSON) so the user can restore one, mark it
"kept", create a fresh backup, or remove one.

The list is split into two sections: user-made backups (created via the
New backup button, or automated ones marked Keep - never pruned, no limit)
and automated backups (created before every deploy, pruned to the newest 20).

Opens as a plugins-panel-scoped tab (covers the whole plugins panel while the
modlist stays live). Qt port of the Tk gui/backup_restore_dialog.py; reuses the
neutral backup logic in Utils.profiles.backup verbatim.

Backup operations are fast local file copies, so everything runs synchronously
on the UI thread - no worker/Signal marshalling needed.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QAbstractItemView,
)

from gui_qt.theme_qt import active_palette, _c, close_button, button_qss
from gui_qt.text_input_overlay import TextInputOverlay
from gui_qt.confirm_overlay import ConfirmOverlay
from Utils.profiles.backup import (
    create_backup, list_backups, restore_backup, backup_stats, delete_backup,
    is_backup_kept, set_backup_kept, get_backup_label, set_backup_label,
    is_backup_manual, is_backup_user_made,
)

# Human-friendly weekday + date + time, e.g. "Fri 04 Jul 2026 · 14:30".
_CARD_DATE_FMT = "%a %d %b %Y  ·  %H:%M"


class BackupRestoreView(QWidget):
    """Scoped-tab body listing profile backups with restore / keep / create / remove."""

    def __init__(self, profile_dir: Path, profile_name: str = "default",
                 on_restored=None, on_close=None, log_fn=None):
        super().__init__()
        self._profile_dir = Path(profile_dir)
        self._profile_name = profile_name
        self._on_restored = on_restored or (lambda: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        # Per-row payload: (datetime, backup_dir) for backup rows, None for
        # section headers / placeholder rows.
        self._row_backups: list = []

        self.setObjectName("BackupRestoreView")
        self._build()
        self._reload_list()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Restore backup - {0}").format(self._profile_name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Instruction line.
        info = QLabel(
            self.tr("Select a backup to restore the modlist and plugins for this profile."))
        info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; padding:8px 12px 4px 12px;")
        v.addWidget(info)

        # Backup list - rows carry a rich card widget (see _make_card).
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setSpacing(6)
        self._list.setStyleSheet(
            f"QListWidget {{ background:{_c(p,'BG_DEEP')}; border:none; padding:6px; }}"
            "QListWidget::item { border:none; }"
        )
        self._list.itemSelectionChanged.connect(self._on_selection)
        v.addWidget(self._list, 1)

        # Button row: New backup (left) | Restore, Keep, Rename, Remove (right).
        # Colours are palette-driven so custom themes restyle them.
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(12, 8, 12, 12); rh.setSpacing(8)
        self._new_btn = self._colored_button(self.tr("New backup"), "BTN_SUCCESS")
        self._new_btn.clicked.connect(self._on_create)
        rh.addWidget(self._new_btn)
        rh.addStretch(1)
        self._restore_btn = self._colored_button(self.tr("Restore"), "BTN_WARN_ORANGE")
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore)
        rh.addWidget(self._restore_btn)
        self._keep_btn = self._colored_button(self.tr("Keep"), "BTN_PURPLE")
        self._keep_btn.setEnabled(False)
        self._keep_btn.clicked.connect(self._on_keep)
        rh.addWidget(self._keep_btn)
        self._rename_btn = self._colored_button(self.tr("Rename"), "BTN_INFO")
        self._rename_btn.setEnabled(False)
        self._rename_btn.clicked.connect(self._on_rename)
        rh.addWidget(self._rename_btn)
        self._remove_btn = self._colored_button(self.tr("Remove"), "BTN_DANGER")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._on_remove)
        rh.addWidget(self._remove_btn)
        v.addWidget(row)

    @staticmethod
    def _colored_button(text: str, key: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(button_qss(key, hover_key=key + "_HOV",
                                   padding="6px 14px"))
        return b

    # ---- data -------------------------------------------------------------
    def _reload_list(self):
        all_backups = list_backups(self._profile_dir)
        user = [(dt, b) for dt, b in all_backups if is_backup_user_made(b)]
        auto = [(dt, b) for dt, b in all_backups if not is_backup_user_made(b)]
        self._row_backups = []
        self._list.clear()
        self._add_section(
            self.tr("User backups"), user,
            self.tr("No user backups. Use New backup, or Keep an automated one."))
        self._add_section(
            self.tr("Automated backups"), auto,
            self.tr("No automated backups yet. One is created every time you deploy."))
        self._on_selection()

    def _add_section(self, title: str, backups: list, empty_text: str):
        p = active_palette()
        header = QLabel(title)
        header.setStyleSheet(
            f"color:{_c(p,'TEXT_DIM')}; font-weight:600; font-size:11px;"
            " padding:8px 4px 2px 4px; text-transform:uppercase;")
        self._add_widget_row(header)
        if not backups:
            placeholder = QLabel(empty_text)
            placeholder.setStyleSheet(
                f"color:{_c(p,'TEXT_DIM')}; font-size:11px; padding:2px 8px 6px 8px;")
            self._add_widget_row(placeholder)
            return
        for dt, bdir in backups:
            item = QListWidgetItem()
            card = self._make_card(dt, bdir)
            item.setSizeHint(card.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, card)
            self._row_backups.append((dt, bdir))

    def _add_widget_row(self, widget):
        """Add a non-selectable decoration row (section header / placeholder)."""
        item = QListWidgetItem()
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)
        self._row_backups.append(None)

    def _make_card(self, dt, bdir) -> QWidget:
        """Build a summary card for one backup: date + mod/plugin counts."""
        p = active_palette()
        kept = is_backup_kept(bdir)
        stats = backup_stats(bdir)

        card = QWidget()
        accent = _c(p, 'ACCENT') if is_backup_user_made(bdir) else _c(p, 'BORDER')
        card.setStyleSheet(
            f"QWidget#bcard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')};"
            f" border-left:3px solid {accent}; border-radius:6px; }}"
        )
        card.setObjectName("bcard")
        g = QGridLayout(card)
        g.setContentsMargins(12, 8, 12, 8)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(2)

        # Row 0: title (label if set, else date) + optional "Kept" badge.
        label = get_backup_label(bdir)
        date_str = dt.strftime(_CARD_DATE_FMT)
        title = QLabel(label or date_str)
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        g.addWidget(title, 0, 0)
        if kept:
            badge = QLabel(self.tr("Kept"))
            badge.setStyleSheet(
                f"color:{_c(p,'TEXT_ON_ACCENT')}; background:{_c(p,'ACCENT')};"
                " border-radius:4px; padding:1px 8px; font-size:10px; font-weight:600;")
            g.addWidget(badge, 0, 1, Qt.AlignRight)
        g.setColumnStretch(0, 1)

        # Row 1: date subline - only when a label has taken the title's place.
        row = 1
        if label:
            sub = QLabel(date_str)
            sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            g.addWidget(sub, row, 0, 1, 2)
            row += 1

        # Next row: stats summary line.
        mods = self.tr("{0} mods ({1} enabled)").format(
            stats["mods_total"], stats["mods_enabled"])
        parts = [mods, self.tr("{0} plugins").format(stats["plugins"])]
        if stats["separators"]:
            parts.append(self.tr("{0} separators").format(stats["separators"]))
        stat = QLabel("   •   ".join(parts))
        stat.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        g.addWidget(stat, row, 0, 1, 2)
        return card

    def _selected_backup(self):
        """Return (datetime, backup_dir) for the selected row, or None."""
        row = self._list.currentRow()
        if 0 <= row < len(self._row_backups):
            return self._row_backups[row]
        return None

    def _select_backup(self, bdir: Path):
        """Re-select a backup by folder after the list was rebuilt."""
        for row, payload in enumerate(self._row_backups):
            if payload is not None and payload[1] == bdir:
                self._list.setCurrentRow(row)
                return

    # ---- handlers ---------------------------------------------------------
    def _on_selection(self):
        sel = self._selected_backup()
        self._restore_btn.setEnabled(sel is not None)
        self._rename_btn.setEnabled(sel is not None)
        self._remove_btn.setEnabled(sel is not None)
        if sel is not None and not is_backup_manual(sel[1]):
            # Keep toggles an automated backup into/out of the user section.
            # Manual backups are user-made by definition, so Keep is moot.
            self._keep_btn.setEnabled(True)
            self._keep_btn.setText(
                self.tr("Unkeep") if is_backup_kept(sel[1]) else self.tr("Keep"))
        else:
            self._keep_btn.setEnabled(False)
            self._keep_btn.setText(self.tr("Keep"))

    def _on_create(self):
        try:
            create_backup(self._profile_dir, log_fn=self._log, manual=True)
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the tab
            self._log(f"[backup] create failed: {exc}")
        self._reload_list()

    def _on_keep(self):
        sel = self._selected_backup()
        if sel is None:
            return
        _dt, bdir = sel
        set_backup_kept(bdir, not is_backup_kept(bdir))
        self._reload_list()
        self._select_backup(bdir)

    def _on_rename(self):
        sel = self._selected_backup()
        if sel is None:
            return
        _dt, bdir = sel

        def _done(text):
            if text is None:
                return
            set_backup_label(bdir, text)
            self._reload_list()
            self._select_backup(bdir)

        TextInputOverlay.show_over(
            self,
            self.tr("Rename backup"),
            self.tr("Enter a name for this backup (leave blank to use the date)."),
            _done,
            initial=get_backup_label(bdir),
            ok_label=self.tr("Rename"),
        )

    def _on_remove(self):
        sel = self._selected_backup()
        if sel is None:
            return
        dt, bdir = sel
        name = get_backup_label(bdir) or dt.strftime(_CARD_DATE_FMT)

        def _done(ok):
            if not ok:
                return
            try:
                delete_backup(bdir)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[backup] remove failed: {exc}")
            self._reload_list()

        ConfirmOverlay.show_over(
            self,
            self.tr("Remove backup"),
            self.tr("Remove backup \"{0}\"? This cannot be undone.").format(name),
            _done,
            confirm_label=self.tr("Remove"),
        )

    def _on_restore(self):
        sel = self._selected_backup()
        if sel is None:
            return
        _dt, backup_dir = sel
        try:
            restore_backup(self._profile_dir, backup_dir)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[backup] restore failed: {exc}")
            return
        self._on_restored()
        self._on_close()
