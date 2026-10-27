"""Create Collection - a modlist-scoped tab that packages the current profile
into a Vortex/Nexus-compatible collection ``.7z``. Subclasses ExportProfileView
for the per-mod source/version/optional/fomod table; adds the collection info
form (name, author, description, install instructions) and swaps the export
path for ``Utils.collections.export`` (collection.json + bundled files).

The output installs in Vortex and in Amethyst's own collection installer, and
can be uploaded straight to Nexus as a new collection or as a new revision of
one the user already owns (including collections authored in Vortex). Uploads
land as drafts; publishing happens in the My Collections tab.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QT_TRANSLATE_NOOP
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QCheckBox, QRadioButton, QButtonGroup,
    QFrame, QComboBox,
)

from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.appearance.theme import active_palette, _c
from gui_qt.profiles.export import (
    ExportProfileView, _CardOverlay, _card_title, _card_button_bar,
    _CHECK_COL_NAME,
)
from Utils.profiles import export as profile_export

# Base columns are check/name/source/version/fomod/optional (0-5); these three
# are appended, so they shift with any change to the base set.
_COL_PHASE = 6
_COL_POLICY = 7
_COL_EDITS = 8

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


class _NoScrollSpinBox(QSpinBox):
    """A spin box that ignores the wheel instead of changing value.

    Phase boxes sit in table cells, so a wheel event over one is the user
    scrolling the mod list, not aiming at that box - and silently renumbering a
    phase while scrolling past is the kind of edit nobody notices until the
    collection installs in the wrong order. Ignoring the event lets it bubble
    to the table viewport, which scrolls as if the box weren't there.
    ``StrongFocus`` drops the default ``WheelFocus`` so scrolling can't pull
    keyboard focus into a cell either; clicking and tabbing still work.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class _NoScrollComboBox(QComboBox):
    """A combo box that ignores the wheel. See :class:`_NoScrollSpinBox`.

    The upload target is the one setting where a stray scroll does real damage:
    it decides WHICH collection a revision is pushed to.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


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
    # Phase/Update slot in before the parent's Edits column.
    _EDITS_COL = _COL_EDITS

    # (ok, message, url) from the upload worker → UI thread.
    _upload_done = Signal(bool, str, str)
    # detected game version from the background probe → UI thread.
    _game_version_ready = Signal(str)
    # validated Nexus username for the author autofill → UI thread.
    _author_ready = Signal(str)
    # the user's own collections, for the upload-target picker → UI thread.
    _targets_ready = Signal(object)
    _warnings_ready = Signal(object)
    _collection_path_ready = Signal(int, object)

    # A Nexus collection has no disabled state - build_collection_manifest
    # drops disabled rows outright - so default them to "ignore" and let the
    # table say so, instead of listing them as exportable and warning later.
    _PRESERVE_VERSION_PINS = True
    _IGNORE_DISABLED_ROWS = True
    _SUPPORTS_SAVES_EXPORT = False
    # Collection FOMOD choices are mapped through the archive's
    # ModuleConfig.xml when no profile copy exists - include that in the
    # missing-archive preflight.
    _ARCHIVE_PREFLIGHT_FOMOD = True
    # A Nexus collection manifest has no way to express a Thunderstore package,
    # so the source is neither offered nor auto-detected here - see _seed_rows,
    # which resets any row load_rows detected before the defaults run.
    _ALLOWED_SOURCES = ("nexus", "direct", "browse", "manual", "bundle",
                        "ignore")

    def __init__(self, window, game, api, log_fn=None):
        self._pending_info: dict = {}
        self._busy = False
        self._closed = False
        self._operation_id = 0
        self._operation_kind = ""
        self._registration_started = False
        self._stage = "idle"
        self._operation_rows = []
        self._retry_upload = None
        self._warning_request = None
        self._last_upload_record = None
        self._draft_target = None
        self._draft_fingerprint = None
        # The name we last auto-filled from the upload target, so switching
        # targets can replace it while never clobbering a hand-typed name.
        self._autofilled_name = ""
        staging = game.get_effective_mod_staging_path()
        game = copy.copy(game)
        game.get_effective_mod_staging_path = lambda: staging
        super().__init__(window, game, api, log_fn=log_fn)
        self._upload_done.connect(self._on_upload_done)
        self._game_version_ready.connect(self._on_game_version_ready)
        self._author_ready.connect(self._on_author_ready)
        self._targets_ready.connect(self._on_targets_ready)
        self._warnings_ready.connect(self._on_warnings_ready)
        self._collection_path_ready.connect(self._on_collection_path)
        self._load_draft()
        self._draft_timer = QTimer(self)
        self._draft_timer.setInterval(2000)
        self._draft_timer.timeout.connect(self._autosave_settings)
        self._draft_timer.start()
        threading.Thread(target=self._autofill_worker,
                         daemon=True, name="collection-autofill").start()

    def _autofill_worker(self):
        from Utils.collections.export import detect_game_version
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
        if self._last_upload_record and not any(
                c.id == self._last_upload_record["collection_id"] for c in (collections or [])):
            from Nexus.nexus_api import MyCollection
            record = self._last_upload_record
            collections = list(collections or []) + [MyCollection(
                id=record["collection_id"], slug=record["slug"], name=record["name"],
                game_domain=record["domain"])]
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
        wanted_id = (self._draft_target if self._draft_target is not None
                     else int(record.get("collection_id") or 0))
        matched = None
        if wanted_id:
            matched = next((c for c in self._targets if c.id == wanted_id), None)
        if matched is None and self._draft_target is None:
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
            from Utils.games.registry import get_collection_url_from_profile
            from Utils.collections.manifest import parse_collection_url
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
            profile = self._profile_dir()
            title.setText(self.tr("Create Collection — {0} / {1}").format(
                self._game.name, profile.name if profile else ""))

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
            **{"Phase": 70, "Update": 100})
        # Content floors for the extra columns: the phase spinbox is 56px
        # fixed, and Update must fit the widest policy label button.
        fm = self.fontMetrics()
        widest_policy = max(fm.horizontalAdvance(self.tr(lbl))
                            for lbl in _POLICY_LABELS.values())
        self._MIN_COL_WIDTHS = dict(
            ExportProfileView._MIN_COL_WIDTHS,
            **{"Phase": 72, "Update": widest_policy + 36})

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
        self._target_box = _NoScrollComboBox()
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
            # OFF by default: these are whole INI files, so they carry the
            # author's resolution, gamma, shadow distance and adapter settings
            # and would overwrite the installing user's machine-specific values.
            self._ini_tweaks_chk.setChecked(False)
            self._ini_tweaks_chk.setText(
                self.tr("Include profile INI files ({0})").format(ini_count))
            self._ini_tweaks_chk.setToolTip(self.tr(
                "Ships the profile's game INI files as INI tweaks. These are "
                "complete files, so they also carry display settings specific "
                "to your machine (resolution, gamma, shadow distance) and "
                "will overwrite the installing user's."))
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
        self._cancel_btn = QPushButton(self.tr("Cancel operation"))
        self._cancel_btn.clicked.connect(self._cancel_operation)
        self._cancel_btn.hide()
        pv.addWidget(self._cancel_btn)
        self._retry_btn = QPushButton(self.tr("Retry previous upload"))
        self._retry_btn.setToolTip(self.tr("Reuse the prepared collection from the failed upload."))
        self._retry_btn.clicked.connect(self._retry_previous_upload)
        self._retry_btn.hide()
        pv.addWidget(self._retry_btn)

        # Extra columns - Phase: install phasing (0 = first wave; higher phases
        # install after previous ones are deployed, e.g. frameworks first).
        # Update: what installers fetch when a newer file exists.
        t = self._table
        t.setColumnCount(9)
        t.setHorizontalHeaderLabels(
            [_CHECK_COL_NAME,
             self.tr("Mod Name"), self.tr("Source"), self.tr("Preferred Version"),
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
    def _create_row_cells(self, vi: int) -> dict:
        w = super()._create_row_cells(vi)
        di = lambda: w["data_idx"]
        spin = _NoScrollSpinBox()
        spin.setRange(0, 99)
        spin.setFixedWidth(56)
        spin.setAlignment(Qt.AlignCenter)
        spin.valueChanged.connect(lambda val: self._set_phase(di(), val))
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        lay.addWidget(spin)
        self._attach_cell(w, _COL_PHASE, wrap)

        pol_btn = QPushButton()
        pol_btn.setCursor(Qt.PointingHandCursor)
        # Sized for the widest label so no cell ever clips its text.
        pol_btn.setMinimumWidth(self._policy_btn_width(pol_btn))
        pol_btn.setToolTip(self.tr(
            "What installers download when a newer file exists."))
        pol_btn.clicked.connect(lambda _=False: self._pick_policy(di()))
        self._attach_cell(w, _COL_POLICY, pol_btn)

        w["phase_spin"] = spin
        w["policy_btn"] = pol_btn
        return w

    def _update_row_cells(self, vi: int, row: dict):
        super()._update_row_cells(vi, row)
        w = self._row_pool[vi]
        spin = w["phase_spin"]
        spin.blockSignals(True)
        spin.setValue(int(row.get("phase") or 0))
        spin.blockSignals(False)
        policy = row.get("update_policy", "exact")
        w["policy_btn"].setText(self.tr(_POLICY_LABELS.get(policy, "Exact")))

    def _set_phase(self, data_idx: int, value: int):
        self._all_rows[data_idx]["phase"] = int(value)

    def _bundle_blocked_reason(self, row: dict) -> str:
        """Mods users can download themselves must not be bundled.

        Bundling ships the files inside the collection, so doing it to a mod
        that has a Nexus page is redistributing someone else's work - Nexus's
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

    def _snapshot_rows(self) -> list:
        """A detached copy of the export rows for a worker thread.

        The table stays interactive during an export/upload, so the worker
        must not read the live dicts the UI is still mutating."""
        return [dict(r) for r in self._all_rows]

    def _workshop_dir(self):
        """Collection settings live in their own folder.

        ``ExportProfileView._auto_load_latest`` loads the NEWEST json in this
        directory, so sharing it with the .amethyst export would make each
        feature clobber the other's auto-loaded settings."""
        pd = self._profile_dir()
        return (pd / "workshop" / "collection") if pd else None

    def _draft_path(self):
        pd = self._profile_dir()
        return pd / "workshop" / "collection_draft.json" if pd else None

    def _load_draft(self):
        path = self._draft_path()
        try:
            draft = json.loads(path.read_text(encoding="utf-8")) if path else {}
            info = draft.get("info") or {}
            for key, widget in (("name", self._info_name), ("author", self._info_author)):
                if key in info:
                    widget.setText(str(info[key]))
            if "gameVersions" in info:
                self._info_versions.setText(", ".join(info["gameVersions"]))
            for key, widget in (("description", self._info_desc),
                                ("installInstructions", self._info_instructions)):
                if key in info:
                    widget.setPlainText(str(info[key]))
            for key, widget in (("recommendNewProfile", self._recommend_profile_chk),
                                ("adultContent", self._adult_chk),
                                ("excludePluginRules", self._exclude_plugin_rules_chk),
                                ("includeIniTweaks", self._ini_tweaks_chk)):
                if key in info:
                    widget.setChecked(bool(info[key]))
            self._draft_target = draft.get("target_id")
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _autosave_settings(self):
        ws_dir = self._workshop_dir()
        if not ws_dir:
            return
        info = self._collect_info()
        target = self._selected_target()[0]
        if self._targets:
            self._draft_target = target
        draft = {"info": info, "target_id": self._draft_target}
        keys = ("name", "optional", "source", "direct_url", "file_id", "mod_id",
                "version", "ver_label", "size_bytes", "game_domain", "phase",
                "update_policy", "source_instructions", "save_edits", "fomod_export")
        fingerprint = json.dumps([draft, [{k: r.get(k) for k in keys}
                                         for r in self._all_rows]], sort_keys=True)
        if fingerprint == self._draft_fingerprint:
            return
        try:
            ws_dir.mkdir(parents=True, exist_ok=True)
            profile_export.write_settings(ws_dir / "workshop_autosave.json", self._all_rows)
            path = self._draft_path()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(draft, indent=2), encoding="utf-8")
            temporary.replace(path)
            self._draft_fingerprint = fingerprint
        except OSError as exc:
            self._log(f"[collection] could not autosave draft: {exc}")

    # -- seeding ------------------------------------------------------------

    def _seed_rows(self):
        """Recover per-mod authoring settings from the manifest an install left
        in the profile, so a re-installed (or Vortex-authored) collection keeps
        its phases, update policies, sources and file-edit flags."""
        from Utils.collections.export import (
            read_profile_manifest, seed_info_from_manifest,
            seed_rows_from_manifest)
        # Undo load_rows' Thunderstore detection: this format cannot express a
        # Thunderstore package. Back on "nexus" these rows go through exactly
        # the path they took before Thunderstore support existed -
        # apply_source_defaults (which runs after this) bundles the ones with
        # no Nexus ids, and the rest are caught by nexus_missing_file_ids.
        for row in self._all_rows:
            if row.get("source") in {"thunderstore", "plugin"}:
                row["source"] = "nexus"
        manifest = read_profile_manifest(self._profile_dir())
        if not manifest:
            return
        seeded = seed_rows_from_manifest(self._all_rows, manifest)
        if seeded:
            self._log(f"[collection] seeded {seeded} mod(s) from the "
                      "profile's collection manifest")
        self._seed_info(seed_info_from_manifest(manifest))

    def _seed_info(self, info: dict):
        """Pre-fill the collection-info panel from the installed manifest.

        Runs during construction, before the user can type, so an empty text
        field means "untouched" - it is still checked so a later caller can't
        silently overwrite real input.
        """
        if not info:
            return
        filled = []
        for key, widget in (("description", self._info_desc),
                            ("installInstructions", self._info_instructions)):
            if key in info and not widget.toPlainText().strip():
                widget.setPlainText(info[key])
                filled.append(key)
        for key, chk in (
                ("recommendNewProfile", self._recommend_profile_chk),
                ("excludePluginRules", self._exclude_plugin_rules_chk)):
            if key in info:
                chk.setChecked(info[key])
                filled.append(key)
        if filled:
            self._log("[collection] seeded collection info from the profile's "
                      f"manifest: {', '.join(filled)}")

    def _sort_key(self, col: int, row: dict):
        if col == _COL_PHASE:
            return int(row.get("phase") or 0)
        if col == _COL_POLICY:
            policy = row.get("update_policy", "exact")
            return (_POLICY_ORDER.index(policy)
                    if policy in _POLICY_ORDER else 0)
        return super()._sort_key(col, row)

    def _policy_btn_width(self, btn: QPushButton) -> int:
        """Width that fits the longest policy label plus button padding."""
        fm = btn.fontMetrics()
        widest = max(fm.horizontalAdvance(self.tr(lbl))
                     for lbl in _POLICY_LABELS.values())
        return widest + 28

    def _pick_policy(self, data_idx: int):
        row = self._all_rows[data_idx]

        def _picked(policy):
            if policy not in _POLICY_ORDER:
                return
            row["update_policy"] = policy
            self._refresh_visible_row(data_idx)

        _PolicyOverlay(self.window(), row["name"],
                       row.get("update_policy", "exact"), _picked)

    def _profile_ini_candidates(self) -> list:
        """Profile 'ini files' entries that are valid INI-tweak targets."""
        from Utils.collections.ini_tweaks import GAME_INI_TARGETS
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
        from Utils.collections.export import validate_collection_name
        if not info.get("author"):
            return self.tr("An author name is required.")
        problem = validate_collection_name(info.get("name", ""))
        return problem or None

    def _rows_error(self) -> "str | None":
        """Row-level validation shared by export and upload; None when OK."""
        if not self._all_rows:
            return self.tr("This profile has no mods to put in a collection.")
        active = [r for r in self._all_rows
                  if r.get("source") != "ignore" and r.get("enabled") is not False]
        missing = profile_export.nexus_missing_file_ids(active)
        if missing:
            return self.tr(
                "{0} Nexus mod(s) are missing a File ID and must be set "
                "first.").format(len(missing))
        no_url = [r["name"] for r in active
                  if r.get("source") in ("browse", "direct") and not r.get("direct_url")]
        if no_url:
            return self.tr(
                "Download-source mod(s) need a URL: {0}").format(
                    ", ".join(no_url[:3]))
        bare = [r["name"] for r in active
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
        if self._busy:
            return
        info = self._collect_info()
        err = self._info_error(info) or self._rows_error()
        if err:
            self._notify(err, "warning")
            return
        self._begin_operation("export", info)
        self._archive_preflight(lambda: self._open_collection_picker(info))

    def _open_collection_picker(self, info: dict):
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in info["name"]).strip()
        default_name = f"{safe_name or 'collection'}.7z"
        from Utils.ui.portal import pick_save_file
        self._stage = "picker"
        token = self._operation_id
        pick_save_file(
            self.tr("Export Collection"),
            lambda path: safe_emit(self._collection_path_ready, token, path),
            current_name=default_name,
            filters=[(self.tr("Nexus Collection (*.7z)"), ["*.7z"]),
                     (self.tr("All files"), ["*"])])

    def _begin_operation(self, kind, info):
        self._operation_id += 1
        self._operation_kind = kind
        self._registration_started = False
        self._operation_rows = copy.deepcopy(self._snapshot_rows())
        self._pending_info = dict(info)
        self._export_cancel = threading.Event()
        self._stage = "preflight"
        self._set_busy(True)
        self._autosave_settings()

    def _set_busy(self, busy: bool):
        self._busy = busy
        for btn in (self._export_btn, self._upload_btn):
            btn.setEnabled(not busy)
        self._cancel_btn.setVisible(busy)
        self._cancel_btn.setEnabled(busy)
        self._retry_btn.setVisible(not busy and self._retry_upload is not None)
        if not busy:
            self._stage = "idle"
            self._operation_kind = ""

    def _cancel_operation(self):
        if not self._busy or self._registration_started:
            return
        self._export_cancel.set()
        if self._warning_request:
            self._warning_request["event"].set()
        self._cancel_btn.setEnabled(False)
        if self._stage != "working":
            self._operation_id += 1
            self._set_busy(False)
        else:
            self._on_export_progress(0, 0, self.tr("Cancelling…"))

    def _check_cancel(self):
        profile_export._check_cancel(self._export_cancel)

    def _archive_preflight(self, proceed):
        token = self._operation_id
        rows, game = self._operation_rows, self._game
        proceed._collection_token = token
        def worker():
            try:
                from Utils.collections.export import missing_archive_report
                report = missing_archive_report(rows, game, check_fomod=True, include_disabled=False)
            except Exception as exc:
                report = exc
            safe_emit(self._archive_check_done, report, proceed)
        threading.Thread(target=worker, daemon=True, name="collection-preflight").start()

    def _on_archive_check_done(self, report, proceed):
        token = getattr(proceed, "_collection_token", None)
        if token != self._operation_id or not self._busy or self._closed:
            return
        if isinstance(report, Exception):
            self._set_busy(False)
            self._notify(str(report), "error")
            return
        def done(ok):
            if token != self._operation_id or not self._busy:
                return
            if ok:
                proceed()
            else:
                self._set_busy(False)
        if not report:
            done(True)
            return
        from gui_qt.installers.missing_archives import MissingArchivesOverlay
        MissingArchivesOverlay.show_over(self.window(), report, api=self._api,
            game_name=self._game.name, log_fn=self._log, on_done=done)

    def _on_collection_path(self, token, path):
        if token == self._operation_id and not self._closed:
            self._on_save_path_picked(path)

    def _on_save_path_picked(self, path):
        if not self._busy or self._operation_kind != "export" or self._stage != "picker":
            return
        if not path:
            self._set_busy(False)
            return
        self._stage = "working"
        threading.Thread(target=self._package_worker,
                         args=(str(path), self._operation_rows),
                         daemon=True, name="collection-export").start()

    def _review_warnings(self, warnings):
        if not warnings:
            return
        request = {"token": self._operation_id, "warnings": list(warnings),
                   "event": threading.Event(), "accepted": False}
        safe_emit(self._warnings_ready, request)
        while not request["event"].wait(0.1):
            self._check_cancel()
        self._check_cancel()
        if not request["accepted"]:
            raise profile_export.ExportCancelled()

    def _on_warnings_ready(self, request):
        if self._closed or request["token"] != self._operation_id or self._export_cancel.is_set():
            request["event"].set()
            return
        self._warning_request = request
        from gui_qt.dialogs.confirm import ConfirmOverlay
        def done(ok):
            request["accepted"] = ok
            request["event"].set()
            if self._warning_request is request:
                self._warning_request = None
        ConfirmOverlay(self.window(), self.tr("Review collection warnings"),
            self.tr("Some content or settings could not be included. Continue with this collection?"),
            done, list_items=request["warnings"], confirm_label=self.tr("Continue"),
            cancel_label=self.tr("Cancel"), danger=False)

    def _build_package(self, out_path, rows, info):
        from Utils.collections import export as ce
        scratch = []
        try:
            manifest, jobs, warnings = ce.build_collection_manifest(
                rows, self._game, info, progress_cb=self._make_collection_progress_cb(),
                scratch_out=scratch, check_cancel=self._check_cancel)
            if not manifest["mods"]:
                raise ValueError("No exportable mods (all ignored, disabled, or missing).")
            for warning in warnings:
                self._log(f"[collection] {warning}")
            self._review_warnings(warnings)
            path = ce.pack_collection(out_path, manifest, jobs,
                progress_cb=self._make_collection_progress_cb(), log_fn=self._log,
                check_cancel=self._check_cancel)
            return path, manifest, warnings
        finally:
            ce.cleanup_scratch(scratch)

    def _package_worker(self, out_path: str, rows=None):
        try:
            final, _, warnings = self._build_package(out_path, rows, self._pending_info)
            message = str(final)
            if warnings:
                message += "\n" + self.tr("{0} warning(s) - see log.").format(len(warnings))
            safe_emit(self._export_done, True, message)
        except profile_export.ExportCancelled:
            safe_emit(self._export_done, False, "")
        except Exception as exc:
            safe_emit(self._export_done, False, str(exc))

    def _make_collection_progress_cb(self):
        """Throttled worker→UI progress bridge for the collection phases."""
        import time
        from Utils.collections import export as ce
        last_emit = [0.0]
        phases = {
            ce.PHASE_META: self.tr("Reading mod metadata…"),
            ce.PHASE_HASH: self.tr("Hashing archives…"),
            ce.PHASE_BUNDLE: self.tr("Packing bundled mods…"),
            ce.PHASE_PATCH: self.tr("Diffing edited files…"),
            ce.PHASE_PACK: self.tr("Writing collection archive…"),
        }

        def _cb(done: int, total: int, phase: str):
            self._check_cancel()
            now = time.monotonic()
            if done < total and now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            safe_emit(self._export_progress, done, total,
                      phases.get(phase, self.tr("Exporting…")))

        return _cb

    def _byte_phase_labels(self) -> set:
        """Phase labels whose counters are BYTES rather than mod counts.

        The build phases count mods; packing and uploading count bytes, so the
        card has to switch units or a 500 MB upload reads as a raw integer.
        Matched on the label because that is all the progress signal carries -
        both sides translate the same source string, so it stays correct."""
        return {self.tr("Writing collection archive…"),
                self.tr("Uploading archive…")}

    def _on_export_progress(self, done, total, phase: str):
        done, total = int(done), int(total)
        if phase == self.tr("Registering collection…"):
            self._cancel_btn.setEnabled(False)
        popup = self._progress_popup()
        if popup is None:
            return
        popup.set_progress(done, total, phase,
                           title=self.tr("Exporting collection"),
                           bytes_mode=(total > 0
                                       and phase in self._byte_phase_labels()),
                           key="collection-export")

    def _on_export_done(self, ok: bool, message: str):
        self._export_active = False
        self._set_busy(False)
        popup = self._progress_popup()
        if popup is not None:
            popup.clear(key="collection-export")
        if ok:
            self._notify(self.tr("Collection exported to {0}").format(message),
                         "info")
            self._log(f"[collection-export] wrote {message}")
        elif message:
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
        if self._busy:
            return
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
        self._begin_operation("upload", info)

        collection_id, target_slug, target_name = self._selected_target()
        if not collection_id and not self._targets:
            # Fall back to this profile's upload record ONLY while the target
            # picker is empty (fetch failed / offline), so revision continuity
            # survives without the account list. Once the picker has targets,
            # a live recorded collection is in it and preselected - so "Create
            # new collection" is an explicit user choice. Overriding it with
            # the record would not just force a revision: the revision path
            # sends the typed name through editCollection and would RENAME the
            # old collection.
            record = self._read_upload_record()
            collection_id = int(record.get("collection_id") or 0)
            target_slug = record.get("slug", "") or ""
            target_name = record.get("name", "") or ""
        self._archive_preflight(
            lambda: self._confirm_upload(info, collection_id, target_slug,
                                         target_name))

    def _confirm_upload(self, info: dict, collection_id: int,
                        target_slug: str, target_name: str):
        """Second half of :meth:`_on_upload`, resumed after the
        missing-archive preflight."""
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

        from gui_qt.dialogs.confirm import ConfirmOverlay

        self._stage = "confirm"
        token = self._operation_id
        def _confirmed(ok: bool):
            if token != self._operation_id or not self._busy or self._stage != "confirm":
                return
            if not ok:
                self._set_busy(False)
                return
            self._discard_retry()
            self._stage = "working"
            self._on_export_progress(0, 0, self.tr("Preparing upload…"))
            threading.Thread(
                target=self._upload_worker,
                args=(collection_id, target_slug, self._operation_rows,
                      dict(info)),
                daemon=True, name="collection-upload").start()

        ConfirmOverlay(self.window(), self.tr("Upload collection"), body,
                       _confirmed, confirm_label=self.tr("Upload"),
                       cancel_label=self.tr("Cancel"), danger=False)

    def _discard_retry(self):
        import shutil
        previous, self._retry_upload = self._retry_upload, None
        if previous:
            shutil.rmtree(previous["directory"], ignore_errors=True)
        self._retry_btn.hide()

    def _retry_previous_upload(self):
        if self._busy or not self._retry_upload:
            return
        package = self._retry_upload
        if not package["archive"].is_file():
            self._discard_retry()
            return
        self._begin_operation("upload", package["info"])
        self._retry_upload = None
        self._stage = "working"
        threading.Thread(target=self._upload_worker,
            args=(package["collection_id"], package["slug"], [], package["info"], package),
            daemon=True, name="collection-upload-retry").start()

    def _upload_worker(self, collection_id: int, target_slug: str = "",
                       rows=None, info=None, package=None):
        import shutil
        import tempfile
        import time
        from Utils.config_paths import get_download_cache_dir

        info = dict(info if info is not None else self._pending_info)
        directory = package["directory"] if package else None
        retain = False
        transfer_started = False
        result_message = (False, "", "")
        slug = target_slug
        try:
            self._check_cancel()
            if collection_id:
                status = self._api.get_collection_status(slug, collection_id)
                if status in ("discarded", "missing"):
                    collection_id, slug = 0, ""
            self._check_cancel()
            if package is None:
                directory = Path(tempfile.mkdtemp(prefix="amethyst_colupload_",
                                                 dir=str(get_download_cache_dir())))
                archive, manifest, warnings = self._build_package(
                    directory / "collection.7z", rows, info)
                package = {"directory": directory, "archive": archive,
                           "manifest": manifest, "warnings": warnings, "info": info}
            package.update(collection_id=collection_id, slug=slug)
            manifest, warnings = package["manifest"], package["warnings"]
            last_emit = [0.0]
            def progress(done, total):
                now = time.monotonic()
                if done < total and now - last_emit[0] < 0.1:
                    return
                last_emit[0] = now
                safe_emit(self._export_progress, done, total, self.tr("Uploading archive…"))
            phases = {"hash": self.tr("Hashing archive…"),
                      "upload": self.tr("Uploading archive…"),
                      "finalise": self.tr("Finalising upload…"),
                      "wait": self.tr("Waiting for Nexus…")}
            def phase(name):
                safe_emit(self._export_progress, 0, 0, phases[name])
            transfer_started = True
            upload_id = self._api.upload_collection_archive(
                package["archive"], progress_cb=progress,
                check_cancel=self._check_cancel, phase_cb=phase)
            transfer_started = False
            self._check_cancel()
            safe_emit(self._export_progress, 0, 0, self.tr("Registering collection…"))
            adult = bool(info.get("adultContent"))
            self._registration_started = True
            if collection_id:
                result = self._api.create_collection_revision(
                    collection_id, upload_id, manifest, adult_content=adult)
            else:
                result = self._api.create_collection(upload_id, manifest, adult_content=adult)
            if not result:
                raise RuntimeError("Nexus rejected the collection (see log).")
            col = result.get("collection") or {}
            slug = col.get("slug") or slug
            new_id = int(result.get("collectionId") or col.get("id") or collection_id or 0)
            domain = getattr(self._game, "nexus_game_domain", "") or ""
            record = {"collection_id": new_id, "slug": slug, "domain": domain,
                      "name": info.get("name", ""),
                      "last_revision": int((result.get("revision") or {}).get("revisionNumber") or 0),
                      "updated": datetime.now(timezone.utc).isoformat()}
            self._write_upload_record(record)
            self._last_upload_record = record
            self._pending_warnings = len(warnings)
            url = (f"https://next.nexusmods.com/{domain}/collections/{slug}"
                   if slug and domain else "")
            result_message = (True, slug or str(new_id), url)
        except profile_export.ExportCancelled:
            pass
        except Exception as exc:
            retain = transfer_started and package is not None and not self._closed
            if retain:
                self._retry_upload = package
            result_message = (False, str(exc), "")
        finally:
            if self._closed:
                retain = False
                if self._retry_upload is package:
                    self._retry_upload = None
            if directory is not None and not retain:
                shutil.rmtree(directory, ignore_errors=True)
        safe_emit(self._upload_done, *result_message)

    def _on_upload_done(self, ok: bool, message: str, url: str):
        self._set_busy(False)
        popup = self._progress_popup()
        if popup is not None:
            popup.clear(key="collection-export")
        if ok:
            record = self._last_upload_record
            if record:
                from Nexus.nexus_api import MyCollection, MyCollectionRevision
                self._draft_target = record["collection_id"]
                targets = [c for c in self._targets if c.id != self._draft_target]
                targets.append(MyCollection(id=record["collection_id"], slug=record["slug"],
                    name=record["name"], game_domain=record["domain"],
                    revisions=[MyCollectionRevision(revision_number=record["last_revision"])]))
                self._on_targets_ready(targets)
                self._autosave_settings()
            warned = getattr(self, "_pending_warnings", 0)
            text = self.tr("Uploaded as a draft revision - publish it "
                           "from Nexus ▸ Collections ▸ My collections.")
            if warned:
                text += "  " + self.tr("{0} warning(s) - see log.").format(warned)
            self._notify(text, "warning" if warned else "info")
            self._log(f"[collection-upload] uploaded: {message} {url}")
            if url:
                QDesktopServices.openUrl(QUrl(url))
        elif message:
            if self._retry_upload:
                message += " " + self.tr("The prepared collection is kept for Retry previous upload.")
            self._notify(self.tr("Upload failed: {0}").format(message), "error")
            self._log(f"[collection-upload] failed: {message}")

    # -- misc ---------------------------------------------------------------
    def tab_close_blocked(self):
        if self._busy:
            self._notify(self.tr("Cancel the collection operation before closing this tab."), "warning")
        return self._busy

    def tab_closing(self):
        self._autosave_settings()
        self._draft_timer.stop()
        self._closed = True
        if self._export_cancel is not None:
            self._export_cancel.set()
        if self._warning_request:
            self._warning_request["event"].set()
        if self._retry_upload:
            self._discard_retry()

    def _close(self):
        if self.tab_close_blocked():
            return
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
