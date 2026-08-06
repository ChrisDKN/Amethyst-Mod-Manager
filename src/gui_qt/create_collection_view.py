"""Create Collection - a modlist-scoped tab that packages the current profile
into a Vortex/Nexus-compatible collection ``.7z``. Subclasses ExportProfileView
for the per-mod source/version/optional/fomod table; adds the collection info
form (name, author, description, install instructions) and swaps the export
path for ``Utils.collection_export`` (collection.json + bundled files).

The output installs in Vortex and in Amethyst's own collection installer, and
can be uploaded straight to Nexus as a new collection or as a new revision of
one the user already owns (including collections authored in Vortex). Uploads
land as drafts; publishing happens in the My Collections tab.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QT_TRANSLATE_NOOP
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QCheckBox, QRadioButton, QButtonGroup,
    QFrame, QTableWidgetItem, QComboBox,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.export_profile_view import (
    ExportProfileView, _CardOverlay, _card_title, _card_button_bar,
)
from Utils import profile_export

_COL_PHASE = 5
_COL_POLICY = 6
_COL_EDITS = 7

# Per-mod update policy cycle: what the installer fetches when the mod has a
# newer file than the one the author pinned.
_POLICY_ORDER = ("exact", "prefer", "latest")
# Short cell labels - the full wording lives in the picker overlay.
_POLICY_LABELS = {
    "exact": QT_TRANSLATE_NOOP("CreateCollectionView", "Exact"),
    "prefer": QT_TRANSLATE_NOOP("CreateCollectionView", "Prefer"),
    "latest": QT_TRANSLATE_NOOP("CreateCollectionView", "Latest"),
}

# Profile-local record of the collection this profile was uploaded to, so
# subsequent uploads become revisions of the same collection.
_UPLOAD_RECORD = "uploaded_collection.json"


class _PolicyOverlay(_CardOverlay):
    """Borderless in-window overlay: pick a mod's update policy.
    ``on_pick(policy)`` fires on Apply."""

    CARD_W = 480
    CARD_H = 320

    def __init__(self, host, mod_name, current_policy, on_pick):
        super().__init__(host)
        self._on_pick = on_pick
        self._body.addWidget(
            _card_title(self.tr("Update policy - {0}").format(mod_name)))
        sub = QLabel(self.tr("What installers download when a newer file exists:"))
        sub.setObjectName("CardSub")
        sub.setWordWrap(True)
        self._body.addWidget(sub)

        self._group = QButtonGroup(self)
        self._radios: dict[str, QRadioButton] = {}
        for value, label, desc in (
            ("exact", self.tr("Exact only"),
             self.tr("Always install this exact file")),
            ("prefer", self.tr("Prefer exact"),
             self.tr("This file while it exists, otherwise the newest")),
            ("latest", self.tr("Latest"),
             self.tr("Always install the newest file")),
        ):
            rb = QRadioButton(self.tr("{0}   - {1}").format(label, desc))
            rb.setChecked(value == current_policy)
            self._group.addButton(rb)
            self._radios[value] = rb
            row = QFrame()
            row.setObjectName("CardOption")
            row_l = QVBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(rb)
            self._body.addWidget(row)

        self._body.addStretch(1)
        self._body.addLayout(
            _card_button_bar(self, self.tr("Apply"), self._apply))
        self._show_over()

    def _apply(self):
        picked = next((v for v, rb in self._radios.items() if rb.isChecked()),
                      "exact")
        cb = self._on_pick
        self._finish()
        if cb:
            cb(picked)

    def _cancel(self):
        self._finish()


class CreateCollectionView(ExportProfileView):
    """Export the active profile as a Nexus-collection .7z (Vortex-compatible)."""

    _TAB_KEY = "create_collection"
    _COLUMN_STATE_SECTION = "qt_columns_create_collection"

    # (ok, message, url) from the upload worker → UI thread.
    _upload_done = Signal(bool, str, str)
    # detected game version from the background probe → UI thread.
    _game_version_ready = Signal(str)
    # validated Nexus username for the author autofill → UI thread.
    _author_ready = Signal(str)
    # the user's own collections, for the upload-target picker → UI thread.
    _targets_ready = Signal(object)

    def __init__(self, window, game, api, log_fn=None):
        self._pending_info: dict = {}
        # The name we last auto-filled from the upload target, so switching
        # targets can replace it while never clobbering a hand-typed name.
        self._autofilled_name = ""
        super().__init__(window, game, api, log_fn=log_fn)
        self._upload_done.connect(self._on_upload_done)
        self._game_version_ready.connect(self._on_game_version_ready)
        self._author_ready.connect(self._on_author_ready)
        self._targets_ready.connect(self._on_targets_ready)
        threading.Thread(target=self._autofill_worker,
                         daemon=True, name="collection-autofill").start()

    def _autofill_worker(self):
        from Utils.collection_export import detect_game_version
        version = detect_game_version(self._game)
        if version:
            safe_emit(self._game_version_ready, version)
        if self._api is None:
            return
        try:
            user = self._api.validate()
            name = getattr(user, "name", "") or ""
        except Exception:
            name = ""
        if name:
            safe_emit(self._author_ready, name)
        try:
            mine = self._api.get_my_collections()
        except Exception:
            mine = []
        safe_emit(self._targets_ready, mine)

    def _on_game_version_ready(self, version: str):
        if not self._info_versions.text().strip():
            self._info_versions.setText(version)

    def _on_author_ready(self, name: str):
        if not self._info_author.text().strip():
            self._info_author.setText(name)

    def _on_targets_ready(self, collections):
        """Fill the upload-target picker and preselect this profile's collection.

        Priority: the profile's own upload record, then the collection URL an
        install stamped on the profile (which is how a re-installed profile - or
        one built from a Vortex-authored collection - is recognised)."""
        domain = (getattr(self._game, "nexus_game_domain", "") or "").lower()
        self._targets = [c for c in (collections or [])
                         if not domain or not c.game_domain
                         or c.game_domain.lower() == domain]
        self._target_box.blockSignals(True)
        self._target_box.clear()
        self._target_box.addItem(self.tr("Create new collection"), 0)
        for col in self._targets:
            latest = (col.latest_published_revision
                      or (col.draft_revision.revision_number
                          if col.draft_revision else 0))
            label = (self.tr("{0}  (rev {1})").format(col.name or col.slug, latest)
                     if latest else (col.name or col.slug))
            self._target_box.addItem(label, col.id)
        self._target_box.blockSignals(False)

        record = self._read_upload_record()
        wanted_id = int(record.get("collection_id") or 0)
        matched = None
        if wanted_id:
            matched = next((c for c in self._targets if c.id == wanted_id), None)
        if matched is None:
            slug = self._profile_collection_slug()
            if slug:
                matched = next((c for c in self._targets
                                if c.slug.lower() == slug.lower()), None)
                if matched is not None:
                    self._log(f"[collection] profile came from '{matched.slug}' "
                              "- preselected it as the upload target")
        if matched is not None:
            idx = self._target_box.findData(matched.id)
            if idx >= 0:
                self._target_box.setCurrentIndex(idx)
        self._on_target_changed()

    def _profile_collection_slug(self) -> str:
        """The slug of the collection this profile was installed from, if any."""
        pd = self._profile_dir()
        if pd is None:
            return ""
        try:
            from Utils.game_helpers import get_collection_url_from_profile
            from Utils.collection_manifest import parse_collection_url
            url = get_collection_url_from_profile(pd)
            if not url:
                return ""
            slug, _domain, _rev = parse_collection_url(url)
            return slug or ""
        except Exception:
            return ""

    def _selected_target(self):
        """(collection_id, slug, name) for the chosen upload target."""
        cid = int(self._target_box.currentData() or 0)
        if not cid:
            return 0, "", ""
        col = next((c for c in self._targets if c.id == cid), None)
        if col is None:
            return 0, "", ""
        return col.id, col.slug, col.name

    def _on_target_changed(self, *_args):
        cid, _slug, name = self._selected_target()
        if cid:
            self._upload_btn.setText(self.tr("Upload revision"))
            self._upload_btn.setToolTip(
                self.tr("Uploads a new draft revision of '{0}'.").format(name))
        else:
            self._upload_btn.setText(self.tr("Upload to Nexus"))
            self._upload_btn.setToolTip(
                self.tr("Creates a new draft collection on your account."))

        # Match the name to the chosen target - a revision upload sends this
        # name through editCollection, so a stale one would rename the
        # collection. Anything the user typed themselves is left alone.
        current = self._info_name.text().strip()
        if not current or current == self._autofilled_name:
            self._info_name.setText(name)
            self._autofilled_name = name

    # -- construction -------------------------------------------------------
    def _build(self):
        # Keep the parent's "ExportProfileView" objectName - the inherited QSS
        # background selector keys off it.
        super()._build()
        title = self.findChild(QLabel, "EPTitle")
        if title is not None:
            title.setText(self.tr("Create Collection"))

        # The toolbar's Export… button moves to the side panel's action row.
        root = self.layout()
        tb = root.itemAt(1).widget()
        for btn in tb.findChildren(QPushButton):
            if btn.objectName() == "PrimaryButton":
                btn.hide()

        # ---- side panel: info fields on top, action buttons at the bottom --
        p = active_palette()
        panel = QWidget()
        panel.setObjectName("CCSidePanel")
        panel.setFixedWidth(320)
        panel.setStyleSheet(
            f"#CCSidePanel {{ background: {_c(p, 'BG_HEADER')};"
            f" border-left: 1px solid {_c(p, 'BORDER')}; }}"
            f"#CCSidePanel QLabel {{ color: {_c(p, 'TEXT_MAIN')}; }}")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(4)

        def _line(placeholder: str) -> QLineEdit:
            w = QLineEdit()
            w.setPlaceholderText(placeholder)
            return w

        self._DEFAULT_COL_WIDTHS = dict(
            ExportProfileView._DEFAULT_COL_WIDTHS,
            **{"Phase": 70, "Update": 100, "Edits": 60})
        # Content floors for the extra columns: the phase spinbox is 56px
        # fixed, and Update must fit the widest policy label button.
        fm = self.fontMetrics()
        widest_policy = max(fm.horizontalAdvance(self.tr(lbl))
                            for lbl in _POLICY_LABELS.values())
        self._MIN_COL_WIDTHS = dict(
            ExportProfileView._MIN_COL_WIDTHS,
            **{"Phase": 72, "Update": widest_policy + 36, "Edits": 58})

        self._info_name = _line(self.tr("e.g. My Survival Overhaul"))
        self._info_author = _line(self.tr("Your Nexus username"))
        self._info_versions = _line(
            self.tr("Game version(s), comma separated - optional"))
        self._info_desc = QPlainTextEdit()
        self._info_desc.setPlaceholderText(self.tr("Short description - optional"))
        self._info_desc.setFixedHeight(72)
        self._info_instructions = QPlainTextEdit()
        self._info_instructions.setPlaceholderText(
            self.tr("Install instructions shown before install - optional"))
        self._info_instructions.setFixedHeight(72)

        # Upload target: a new collection, or a new revision of one the user
        # already owns (filled in once the account's collections load).
        self._targets: list = []
        self._target_box = QComboBox()
        self._target_box.addItem(self.tr("Create new collection"), 0)
        self._target_box.currentIndexChanged.connect(self._on_target_changed)

        for label, field in (
            (self.tr("Upload target"), self._target_box),
            (self.tr("Collection name"), self._info_name),
            (self.tr("Author"), self._info_author),
            (self.tr("Game versions"), self._info_versions),
            (self.tr("Description"), self._info_desc),
            (self.tr("Install instructions"), self._info_instructions),
        ):
            pv.addWidget(QLabel(label))
            pv.addWidget(field)
            pv.addSpacing(4)

        self._recommend_profile_chk = QCheckBox(
            self.tr("Suggest a new profile on install"))
        self._recommend_profile_chk.setChecked(True)
        self._recommend_profile_chk.setToolTip(self.tr(
            "When enabled, installers (Amethyst and Vortex) recommend "
            "installing this collection into a fresh profile."))
        self._adult_chk = QCheckBox(self.tr("Contains adult content"))
        self._adult_chk.setToolTip(self.tr(
            "Flags the collection as adult on Nexus."))
        self._exclude_plugin_rules_chk = QCheckBox(
            self.tr("Exclude plugin rules"))
        self._exclude_plugin_rules_chk.setToolTip(self.tr(
            "Installers skip this collection's LOOT plugin rules."))
        self._ini_tweaks_chk = QCheckBox(self.tr("Include profile INI files"))
        self._ini_tweaks_chk.setToolTip(self.tr(
            "Ships the profile's game INI files (ini files/) as INI tweaks "
            "that installers merge into the player's settings."))
        ini_count = len(self._profile_ini_candidates())
        if ini_count:
            self._ini_tweaks_chk.setChecked(True)
            self._ini_tweaks_chk.setText(
                self.tr("Include profile INI files ({0})").format(ini_count))
        else:
            self._ini_tweaks_chk.setChecked(False)
            self._ini_tweaks_chk.setEnabled(False)
            self._ini_tweaks_chk.setToolTip(self.tr(
                "No game INI files found in this profile's 'ini files' folder."))
        pv.addSpacing(4)
        for chk in (self._recommend_profile_chk, self._adult_chk,
                    self._exclude_plugin_rules_chk, self._ini_tweaks_chk):
            pv.addWidget(chk)
        pv.addStretch(1)

        # Action row (bottom of the panel): Export to file + Upload.
        self._export_btn = QPushButton(self.tr("Export…"))
        self._export_btn.setObjectName("FormButton")
        self._export_btn.setCursor(Qt.PointingHandCursor)
        self._export_btn.clicked.connect(self._on_export)
        self._upload_btn = QPushButton(self.tr("Upload to Nexus"))
        self._upload_btn.setObjectName("PrimaryButton")
        self._upload_btn.setCursor(Qt.PointingHandCursor)
        self._upload_btn.clicked.connect(self._on_upload)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self._export_btn, 1)
        actions.addWidget(self._upload_btn, 1)
        pv.addLayout(actions)

        # Extra columns - Phase: install phasing (0 = first wave; higher phases
        # install after previous ones are deployed, e.g. frameworks first).
        # Update: what installers fetch when a newer file exists.
        t = self._table
        t.setColumnCount(8)
        t.setHorizontalHeaderLabels(
            [self.tr("Mod Name"), self.tr("Source"), self.tr("Preferred Version"),
             self.tr("Fomod"), self.tr("Optional"), self.tr("Phase"),
             self.tr("Update"), self.tr("Edits")])
        # Re-wire the header now the column set is final (sortable + resizable,
        # widths/sort persisted under this view's own section).
        self._init_header()

        # ---- restructure: [toolbar + table] on the left, panel on the right.
        # Parent root layout is title bar (0), toolbar (1), table (2).
        root.removeWidget(tb)
        root.removeWidget(t)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(tb)
        lv.addWidget(t, 1)
        content = QWidget()
        ch = QHBoxLayout(content)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        ch.addWidget(left, 1)
        ch.addWidget(panel)
        root.addWidget(content, 1)

    # -- phase column -------------------------------------------------------
    def _rebuild_table(self):
        super()._rebuild_table()
        t = self._table
        pos = {id(r): i for i, r in enumerate(self._all_rows)}
        for i, row in enumerate(self._rows):
            data_idx = pos[id(row)]
            spin = QSpinBox()
            spin.setRange(0, 99)
            spin.setValue(int(row.get("phase") or 0))
            spin.setFixedWidth(56)
            spin.setAlignment(Qt.AlignCenter)
            spin.valueChanged.connect(
                lambda val, di=data_idx: self._set_phase(di, val))
            wrap = QWidget()
            lay = QHBoxLayout(wrap)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setAlignment(Qt.AlignCenter)
            lay.addWidget(spin)
            t.setCellWidget(i, _COL_PHASE, wrap)

            policy = row.get("update_policy", "exact")
            pol_btn = QPushButton(self.tr(_POLICY_LABELS.get(policy, "Exact")))
            pol_btn.setCursor(Qt.PointingHandCursor)
            # Sized for the widest label so no cell ever clips its text.
            pol_btn.setMinimumWidth(self._policy_btn_width(pol_btn))
            pol_btn.setToolTip(self.tr(
                "What installers download when a newer file exists."))
            pol_btn.clicked.connect(
                lambda _=False, di=data_idx, b=pol_btn: self._pick_policy(di, b))
            t.setCellWidget(i, _COL_POLICY, pol_btn)

            # Bundled mods ship their files as-is, so edits are already in.
            if row.get("source") == "bundle":
                dash = QTableWidgetItem("-")
                dash.setFlags(Qt.ItemIsEnabled)
                dash.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, _COL_EDITS, dash)
            else:
                edits_chk = self._center_checkbox(
                    row.get("save_edits", False),
                    lambda ch, di=data_idx: self._set_save_edits(di, ch))
                edits_chk.setToolTip(self.tr(
                    "Ship your local changes to this mod's files as binary "
                    "patches, so users get the mod exactly as you have it."))
                t.setCellWidget(i, _COL_EDITS, edits_chk)

    def _set_phase(self, data_idx: int, value: int):
        self._all_rows[data_idx]["phase"] = int(value)

    def _set_save_edits(self, data_idx: int, checked: bool):
        self._all_rows[data_idx]["save_edits"] = bool(checked)

    def _bundle_blocked_reason(self, row: dict) -> str:
        """Mods users can download themselves must not be bundled.

        Bundling ships the files inside the collection, so doing it to a mod
        that has a Nexus page is redistributing someone else's work — Nexus's
        own guidance is that anything qualifying as a "mod" belongs on a mod
        page and gets *linked* from the collection. Bundling stays available
        for generated output, config files and unpublished work, and local
        changes to a Nexus mod ride along as patches via the Edits column.
        """
        if row.get("mod_id") and row.get("file_id"):
            return self.tr(
                "Not allowed: this mod is on Nexus, so users download it "
                "themselves. Use the Edits column to ship your changes.")
        return ""

    def _autosave_settings(self):
        """Persist the per-mod table on every export/upload.

        Revisions are the normal case for a collection, so the phases, update
        policies and edit flags must survive closing the tab without the user
        having to remember to press Save settings. ``_auto_load_latest`` picks
        the newest file in the folder, so this reuses one fixed name rather
        than piling up timestamped saves."""
        ws_dir = self._workshop_dir()
        if not ws_dir or not self._all_rows:
            return
        try:
            ws_dir.mkdir(parents=True, exist_ok=True)
            profile_export.write_settings(
                ws_dir / "workshop_autosave.json", self._all_rows)
        except Exception as exc:
            self._log(f"[collection] could not autosave settings: {exc}")

    # -- seeding ------------------------------------------------------------
    def _seed_rows(self):
        """Recover per-mod authoring settings from the manifest an install left
        in the profile, so a re-installed (or Vortex-authored) collection keeps
        its phases, update policies, sources and file-edit flags."""
        from Utils.collection_export import (
            read_profile_manifest, seed_rows_from_manifest)
        manifest = read_profile_manifest(self._profile_dir())
        if not manifest:
            return
        seeded = seed_rows_from_manifest(self._all_rows, manifest)
        if seeded:
            self._log(f"[collection] seeded {seeded} mod(s) from the "
                      "profile's collection manifest")

    def _sort_key(self, col: int, row: dict):
        if col == _COL_PHASE:
            return int(row.get("phase") or 0)
        if col == _COL_POLICY:
            policy = row.get("update_policy", "exact")
            return (_POLICY_ORDER.index(policy)
                    if policy in _POLICY_ORDER else 0)
        if col == _COL_EDITS:
            return bool(row.get("save_edits"))
        return super()._sort_key(col, row)

    def _policy_btn_width(self, btn: QPushButton) -> int:
        """Width that fits the longest policy label plus button padding."""
        fm = btn.fontMetrics()
        widest = max(fm.horizontalAdvance(self.tr(lbl))
                     for lbl in _POLICY_LABELS.values())
        return widest + 28

    def _pick_policy(self, data_idx: int, btn: QPushButton):
        row = self._all_rows[data_idx]

        def _picked(policy):
            if policy not in _POLICY_ORDER:
                return
            row["update_policy"] = policy
            btn.setText(self.tr(_POLICY_LABELS[policy]))

        _PolicyOverlay(self.window(), row["name"],
                       row.get("update_policy", "exact"), _picked)

    def _profile_ini_candidates(self) -> list:
        """Profile 'ini files' entries that are valid INI-tweak targets."""
        from Utils.collection_ini_tweaks import GAME_INI_TARGETS
        pd = self._profile_dir()
        targets = GAME_INI_TARGETS.get(getattr(self._game, "name", "") or "")
        if not pd or not targets:
            return []
        ini_dir = pd / "ini files"
        if not ini_dir.is_dir():
            return []
        allowed = {t.lower() for t in targets}
        return [p for p in sorted(ini_dir.glob("*.ini"))
                if p.name.lower() in allowed]

    def _info_error(self, info: dict) -> "str | None":
        """Collection-info validation shared by export and upload."""
        from Utils.collection_export import validate_collection_name
        if not info.get("author"):
            return self.tr("An author name is required.")
        problem = validate_collection_name(info.get("name", ""))
        return problem or None

    def _rows_error(self) -> "str | None":
        """Row-level validation shared by export and upload; None when OK."""
        if not self._all_rows:
            return self.tr("This profile has no mods to put in a collection.")
        missing = profile_export.nexus_missing_file_ids(self._all_rows)
        if missing:
            return self.tr(
                "{0} Nexus mod(s) are missing a File ID and must be set "
                "first.").format(len(missing))
        no_url = [r["name"] for r in self._all_rows
                  if r.get("source") == "browse" and not r.get("direct_url")]
        if no_url:
            return self.tr(
                "Browse-source mod(s) need a URL: {0}").format(
                    ", ".join(no_url[:3]))
        bare = [r["name"] for r in self._all_rows
                if r.get("source") == "manual"
                and not r.get("direct_url")
                and not r.get("source_instructions")]
        if bare:
            return self.tr(
                "Manual-source mod(s) need instructions or a URL: {0}").format(
                    ", ".join(bare[:3]))
        return None

    # -- export -------------------------------------------------------------
    def _collect_info(self) -> dict:
        """Snapshot the info form on the UI thread for the export worker."""
        versions = [v.strip() for v in self._info_versions.text().split(",")
                    if v.strip()]
        return {
            "name": self._info_name.text().strip(),
            "author": self._info_author.text().strip(),
            "authorUrl": "",
            "description": self._info_desc.toPlainText().strip(),
            "installInstructions": self._info_instructions.toPlainText().strip(),
            "gameVersions": versions,
            "recommendNewProfile": self._recommend_profile_chk.isChecked(),
            "excludePluginRules": self._exclude_plugin_rules_chk.isChecked(),
            "adultContent": self._adult_chk.isChecked(),
            "includeIniTweaks": self._ini_tweaks_chk.isChecked(),
        }

    def _on_export(self):
        if not self._all_rows:
            self._notify(self.tr("No mods to export."), "warning")
            return
        info = self._collect_info()
        err = self._info_error(info) or self._rows_error()
        if err:
            self._notify(err, "warning")
            return
        self._pending_info = info
        self._autosave_settings()

        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in info["name"]).strip()
        default_name = f"{safe_name or 'collection'}.7z"
        from Utils.portal_filechooser import pick_save_file
        pick_save_file(
            self.tr("Export Collection"),
            lambda path: safe_emit(self._save_path_picked, path),
            current_name=default_name,
            filters=[(self.tr("Nexus Collection (*.7z)"), ["*.7z"]),
                     (self.tr("All files"), ["*"])])

    def _package_worker(self, out_path: str):
        """Worker thread: build the collection manifest and write the .7z."""
        try:
            from Utils import collection_export
            final, warnings = collection_export.export_collection(
                out_path, self._all_rows, self._game, self._pending_info,
                progress_cb=self._make_collection_progress_cb())
            for w in warnings:
                self._log(f"[collection-export] {w}")
            msg = str(final)
            if warnings:
                msg += "\n" + self.tr("{0} warning(s) - see log.").format(
                    len(warnings))
            safe_emit(self._export_done, True, msg)
        except Exception as exc:
            safe_emit(self._export_done, False, str(exc))

    def _make_collection_progress_cb(self):
        """Throttled worker→UI progress bridge for the collection phases."""
        import time
        from Utils import collection_export as ce
        last_emit = [0.0]
        phases = {
            ce.PHASE_META: self.tr("Reading mod metadata…"),
            ce.PHASE_HASH: self.tr("Hashing archives…"),
            ce.PHASE_BUNDLE: self.tr("Packing bundled mods…"),
            ce.PHASE_PATCH: self.tr("Diffing edited files…"),
            ce.PHASE_PACK: self.tr("Writing collection archive…"),
        }

        def _cb(done: int, total: int, phase: str):
            now = time.monotonic()
            if done < total and now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            safe_emit(self._export_progress, done, total,
                      phases.get(phase, self.tr("Exporting…")))

        return _cb

    def _on_export_progress(self, done, total, phase: str):
        done, total = int(done), int(total)
        popup = self._progress_popup()
        if popup is None:
            return
        popup.set_progress(done, total, phase,
                           title=self.tr("Exporting collection"),
                           bytes_mode=False, key="collection-export")

    def _on_export_done(self, ok: bool, message: str):
        popup = self._progress_popup()
        if popup is not None:
            if ok:
                QTimer.singleShot(900, lambda: popup.clear(key="collection-export"))
            else:
                popup.clear(key="collection-export")
        if ok:
            self._notify(self.tr("Collection exported to {0}").format(message),
                         "info")
            self._log(f"[collection-export] wrote {message}")
        else:
            self._notify(self.tr("Export failed: {0}").format(message), "error")
            self._log(f"[collection-export] failed: {message}")

    # -- upload -------------------------------------------------------------
    def _upload_record_path(self) -> "Path | None":
        pd = self._profile_dir()
        return (pd / _UPLOAD_RECORD) if pd else None

    def _read_upload_record(self) -> dict:
        path = self._upload_record_path()
        if path and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_upload_record(self, record: dict):
        path = self._upload_record_path()
        if path is None:
            return
        try:
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            self._log(f"[collection-upload] record write failed: {exc}")

    def _on_upload(self):
        if not self._all_rows:
            self._notify(self.tr("No mods to upload."), "warning")
            return
        if self._api is None:
            self._notify(self.tr("Log in first: Nexus ▸ Login to Nexus ▸ "
                                 "Login via SSO."), "warning")
            return
        info = self._collect_info()
        err = self._info_error(info) or self._rows_error()
        if err:
            self._notify(err, "warning")
            return
        self._pending_info = info

        collection_id, target_slug, target_name = self._selected_target()
        if not collection_id:
            # No explicit target picked - fall back to this profile's own
            # upload record (set by a previous upload from here).
            record = self._read_upload_record()
            collection_id = int(record.get("collection_id") or 0)
            target_slug = record.get("slug", "") or ""
            target_name = record.get("name", "") or ""
        if collection_id:
            body = self.tr(
                "Upload a new revision of '{0}' (collection #{1})?\n\n"
                "The revision is created as a draft - publish it from "
                "Nexus ▸ Collections ▸ My collections when ready. If that "
                "collection was deleted, a new draft collection is created "
                "instead.").format(target_name or info["name"], collection_id)
        else:
            body = self.tr(
                "Create a new draft collection '{0}' on your Nexus account?\n\n"
                "Nothing is published: the draft is only visible to you until "
                "you publish it from Nexus ▸ Collections ▸ My collections."
            ).format(info["name"])

        from gui_qt.confirm_overlay import ConfirmOverlay

        def _confirmed(ok: bool):
            if not ok:
                return
            self._autosave_settings()
            self._on_export_progress(0, 0, self.tr("Preparing upload…"))
            threading.Thread(
                target=self._upload_worker, args=(collection_id, target_slug),
                daemon=True, name="collection-upload").start()

        ConfirmOverlay(self.window(), self.tr("Upload collection"), body,
                       _confirmed, confirm_label=self.tr("Upload"),
                       cancel_label=self.tr("Cancel"), danger=False)

    def _upload_worker(self, collection_id: int, target_slug: str = ""):
        """Worker thread: pack to a scratch .7z, PUT it, run the mutation."""
        import shutil
        import tempfile

        tmp_dir = None
        try:
            from Utils import collection_export
            from Utils.config_paths import get_download_cache_dir

            # The target collection may have been discarded/deleted on the
            # website since it was recorded - revising it would be rejected
            # ("not allowed to create_or_update_revision"). Fall back to
            # creating a fresh draft.
            if collection_id:
                slug = target_slug or self._read_upload_record().get("slug") or ""
                status = self._api.get_collection_status(slug) if slug else "missing"
                if status != "ok":
                    self._log(f"[collection-upload] target collection "
                              f"#{collection_id} ({slug}) is {status} - "
                              "creating a new draft instead")
                    collection_id = 0

            tmp_dir = Path(tempfile.mkdtemp(
                prefix="amethyst_colupload_", dir=str(get_download_cache_dir())))
            manifest, bundle_jobs, warnings = \
                collection_export.build_collection_manifest(
                    self._all_rows, self._game, self._pending_info,
                    progress_cb=self._make_collection_progress_cb())
            if not manifest["mods"]:
                raise ValueError(
                    "No uploadable mods (all ignored, disabled, or missing).")
            for w in warnings:
                self._log(f"[collection-upload] {w}")
            archive = collection_export.pack_collection(
                tmp_dir / "collection.7z", manifest, bundle_jobs,
                progress_cb=self._make_collection_progress_cb())

            up = self._api.get_collection_upload_url()
            if not up:
                raise RuntimeError(
                    "Nexus did not provide an upload URL (see log).")

            def _put_progress(done, total):
                safe_emit(self._export_progress, done, total,
                          self.tr("Uploading archive…"))

            if not self._api.upload_collection_archive(
                    up["url"], archive, progress_cb=_put_progress):
                raise RuntimeError("Archive upload failed (see log).")

            safe_emit(self._export_progress, 0, 0,
                      self.tr("Registering collection…"))
            adult = bool(self._pending_info.get("adultContent"))
            if collection_id:
                result = self._api.create_collection_revision(
                    collection_id, up["uuid"], manifest, adult_content=adult)
            else:
                result = self._api.create_collection(up["uuid"], manifest,
                                                     adult_content=adult)
            if not result:
                raise RuntimeError(
                    "Nexus rejected the collection (see log).")

            col = result.get("collection") or {}
            slug = col.get("slug") or ""
            new_id = int(result.get("collectionId")
                         or col.get("id") or collection_id or 0)
            rev = result.get("revision") or {}
            domain = getattr(self._game, "nexus_game_domain", "") or ""
            self._write_upload_record({
                "collection_id": new_id,
                "slug": slug,
                "domain": domain,
                "name": self._pending_info.get("name", ""),
                "last_revision": int(rev.get("revisionNumber") or 0),
                "updated": datetime.now(timezone.utc).isoformat(),
            })
            url = (f"https://next.nexusmods.com/{domain}/collections/{slug}"
                   if slug and domain else "")
            safe_emit(self._upload_done, True, slug or str(new_id), url)
        except Exception as exc:
            safe_emit(self._upload_done, False, str(exc), "")
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _on_upload_done(self, ok: bool, message: str, url: str):
        popup = self._progress_popup()
        if popup is not None:
            if ok:
                QTimer.singleShot(900, lambda: popup.clear(key="collection-export"))
            else:
                popup.clear(key="collection-export")
        if ok:
            self._notify(self.tr("Uploaded as a draft revision - publish it "
                                 "from Nexus ▸ Collections ▸ My collections."),
                         "info")
            self._log(f"[collection-upload] uploaded: {message} {url}")
            if url:
                QDesktopServices.openUrl(QUrl(url))
        else:
            self._notify(self.tr("Upload failed: {0}").format(message), "error")
            self._log(f"[collection-upload] failed: {message}")

    # -- misc ---------------------------------------------------------------
    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            try:
                tabs.close_tab(self._TAB_KEY)
                if getattr(self._window, "_create_collection_view", None) is self:
                    self._window._create_collection_view = None
                return
            except Exception:
                pass
        self.hide()
