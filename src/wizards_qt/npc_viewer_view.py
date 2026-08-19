"""View NPCs - browse every NPC with a baked FaceGen head and see the face in 3D.

The mesh side is the NIF Viewer's: Utils.mesh_catalog finds every copy of every
FaceGeom head and flags the one the game loads, and gui_qt.nif_preview renders
it. What this adds is IDENTITY - Utils.npc_catalog joins those meshes to their
NPC_ records, so the list reads "Nazeem" rather than "00013BBF.nif", and a
contested head shows which overhaul actually wins.

A flat sorted list, not a path tree: users look for a person, not a folder.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QSplitter, QTreeView, QVBoxLayout, QWidget,
)

from gui_qt.eliding_label import ElidingLabel
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.image_export_overlay import ImageExportOverlay
from gui_qt.nif_preview import ASSET_PREFIXES, NifPreview
from gui_qt.nif_texture_sources import TextureSourceController
from gui_qt.path_tree import Node, PathTreeDelegate, PathTreeModel
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, button_qss, _c
from gui_qt.worker import LatestWorker
from Utils.mesh_catalog import (
    DATA_ARCHIVE, DATA_LOOSE, MOD_ARCHIVE, MOD_LOOSE, read_entry,
)
from Utils.npc_catalog import build_npc_catalog, npc_label

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# One AssetResolver serves the head listing, the viewport's textures AND the
# body meshes. "meshes/" rather than just the FaceGeom subtree: an actor's
# body, hands, feet and skeleton all live elsewhere under it, and a resolver
# that does not index them reports every body part as missing.
BROWSE_PREFIXES = ASSET_PREFIXES + ("meshes/",)

# Where the camera sits when an NPC opens or the view is reset. Yaw +90 faces
# the actor (Skyrim heads look down +Y); the shallow pitch reads as just above
# eye level rather than a floor shot. LOOK is the look-at height as a fraction
# of the figure: measured against a real assembled body, 0.56 is the highest
# that still keeps the FEET in frame - 0.66 crops them - and it puts the head
# ~85% up the view instead of centring on the waist.
NPC_HOME_YAW = math.radians(90.0)
NPC_HOME_PITCH = math.radians(8.0)
NPC_HOME_LOOK = 0.56

_SRC_ALL, _SRC_MODS, _SRC_VANILLA = "all", "mods", "vanilla"
_MOD_KINDS = (MOD_LOOSE, MOD_ARCHIVE)
_VANILLA_KINDS = (DATA_LOOSE, DATA_ARCHIVE)


class NpcViewerView(QWidget):
    """Full-screen NPC browser + 3D head preview."""

    _scan_done = Signal(int, object)         # (generation, [NpcEntry])
    _list_ready = Signal(int, object, int)   # (generation, root Node, count)
    # (generation, bytes|None, NpcEntry, (tex_override, keep_view))
    _mesh_ready = Signal(int, object, object, object)
    _save_path_picked = Signal(object)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 mod: str = "", **_extra):
        super().__init__()
        self.setObjectName("NpcViewerView")
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._mod = (mod or "").strip()
        self._closing = False
        self._gen = 0
        self._list_gen = 0
        self._open_gen = 0
        self._npcs: list = []
        self._resolver = None
        self._records = None
        self._records_gen = -1
        # The catalogue worker preloads the complete record stack while the
        # user scans the list. An immediate click shares that one operation
        # instead of starting a second walk over the game's master plugins.
        self._records_lock = threading.Lock()
        # Lazy per-mod archive lookups, for profiles with no BSA index.
        self._mod_archives: dict = {}
        self._archive_mod_list = None
        self._archive_owner: dict = {}
        self._current = None
        self._current_data = None
        self._pending_export = None
        self._restore = None
        self._list_jobs = LatestWorker("npc-list")
        self._mesh_reads = LatestWorker("npc-mesh-read")

        profile = ""
        if ctx is not None:
            profile = getattr(ctx, "profile_name", "") or ""
        from wizards_qt.nif_viewer_view import _paths_for
        self._staging, self._profile_dir, self._modlist, self._data = \
            _paths_for(game, profile)

        pal = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("HeaderBar")
        hb = FlowLayout(bar, spacing=8)
        hb.setContentsMargins(12, 8, 8, 8)
        enable_height_for_width(bar)

        title = (self.tr("View NPCs - {0} ▸ {1}").format(game.name, self._mod)
                 if self._mod else
                 self.tr("View NPCs - {0}").format(game.name))
        head = ElidingLabel(title, max_width=340)
        head.setStyleSheet(f"color:{_c(pal,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(head)

        self._count = QLabel(self.tr("Scanning…"))
        self._count.setStyleSheet(f"color:{_c(pal,'TEXT_DIM')};")
        hb.addWidget(self._count)
        hb.add_stretch()

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search NPCs and mods…"))
        self._search.setToolTip(self.tr(
            "Match an NPC name, editor id, FormID, or the mod providing the face"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(lambda _t: self._debounce.start())
        hb.addWidget(self._search)

        self._src_box = QComboBox()
        self._src_box.setToolTip(self.tr("Limit the list to one side of the setup"))
        for label, key in ((self.tr("All sources"), _SRC_ALL),
                           (self.tr("Mods only"), _SRC_MODS),
                           (self.tr("Vanilla only"), _SRC_VANILLA)):
            self._src_box.addItem(label, key)
        self._src_box.activated.connect(lambda _i: self._rebuild_list())
        hb.addWidget(self._src_box)

        self._whole_body = QCheckBox(self.tr("Whole body"))
        self._whole_body.setToolTip(self.tr(
            "Show the NPC's body, hands and feet with the head, posed on the "
            "race's skeleton"))
        self._whole_body.setChecked(True)
        self._whole_body.clicked.connect(self._reload_current)
        hb.addWidget(self._whole_body)

        self._outfit = QCheckBox(self.tr("Outfit"))
        self._outfit.setToolTip(self.tr(
            "Dress the NPC in its default outfit. NPCs that equip from their "
            "inventory instead have none, and show bare."))
        self._outfit.setChecked(True)
        self._outfit.clicked.connect(self._reload_current)
        hb.addWidget(self._outfit)

        self._only_overridden = QCheckBox(self.tr("Only overridden"))
        self._only_overridden.setToolTip(self.tr(
            "Show only NPCs whose face is provided by more than one mod"))
        self._only_overridden.clicked.connect(lambda _on: self._rebuild_list())
        hb.addWidget(self._only_overridden)

        self._only_mod = QCheckBox(self.tr("Only this mod"))
        self._only_mod.setToolTip(self.tr(
            "Show only NPCs {0} provides a face for, alongside the versions "
            "they compete with").format(self._mod))
        self._only_mod.setChecked(True)
        self._only_mod.clicked.connect(lambda _on: self._rebuild_list())
        self._only_mod.setVisible(bool(self._mod))
        if self._mod:
            hb.addWidget(self._only_mod)

        self._save_btn = QPushButton(self.tr("Save as image…"))
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setToolTip(self.tr(
            "Save four turntable angles and a face close-up as one PNG image, "
            "on a background you pick"))
        self._save_btn.setStyleSheet(
            button_qss("BTN_INFO", pal=pal, padding="5px 12px"))
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._choose_image_path)
        hb.addWidget(self._save_btn)

        self._refresh_btn = QPushButton(self.tr("⟳ Refresh"))
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.setToolTip(self.tr(
            "Re-read the profile after changing the modlist or load order"))
        self._refresh_btn.setStyleSheet(
            button_qss("BTN_NEUTRAL", pal=pal, padding="5px 12px"))
        self._refresh_btn.clicked.connect(self._refresh)
        hb.addWidget(self._refresh_btn)

        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(button_qss("BTN_DANGER", pal=pal, padding="5px 12px"))
        close.clicked.connect(self._finish)
        hb.addWidget(close)
        v.addWidget(bar)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        self._model = PathTreeModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree.setItemDelegate(PathTreeDelegate(self._tree))
        self._tree.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._tree.setMinimumWidth(120)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        split.addWidget(self._tree)

        self._preview = NifPreview(None, self.tr("Select an NPC"),
                                   resolver=None, log_fn=self._log)
        self._preview.setMinimumWidth(160)
        # Open on the actor's FACE, not a mesh-browser 3/4 view: yaw +90 looks
        # a Skyrim head in the eyes (they face +Y, verified over 119/120
        # vanilla FaceGen meshes), a shallow downward pitch reads as eye level
        # rather than a floor shot, and looking two-thirds up the figure puts
        # the head in the upper frame instead of centring on the waist.
        self._preview.set_home_view(NPC_HOME_YAW, NPC_HOME_PITCH,
                                    NPC_HOME_LOOK)
        self._tex_sources = TextureSourceController(self._preview, self._log)

        # A fixed front-on still of the face, so the NPC stays identifiable
        # while the main view is orbited away from them. A child of the
        # preview so it tracks that pane rather than the whole tab.
        self._portrait = QLabel(self._preview)
        self._portrait.setObjectName("NpcPortrait")
        self._portrait.setStyleSheet(
            f"background:{_c(pal,'BG_DARK')};"
            f"border:1px solid {_c(pal,'BORDER')};")
        self._portrait.setAlignment(Qt.AlignCenter)
        self._portrait.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._portrait.hide()
        self._preview.portrait_ready.connect(self._guard(self._on_portrait))
        self._preview.scene_ready.connect(self._guard(self._on_scene_ready))
        self._preview.installEventFilter(self)
        split.addWidget(self._preview)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 900])
        v.addWidget(split, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._rebuild_list)

        # Keep feedback's delayed reset inside this widget's lifetime.  A
        # static QTimer.singleShot retains its Python callback after a tab has
        # deleteLater()d the view, at which point touching _save_btn raises
        # "Internal C++ object already deleted".
        self._save_feedback_timer = QTimer(self)
        self._save_feedback_timer.setSingleShot(True)
        self._save_feedback_timer.setInterval(1600)
        self._save_feedback_timer.timeout.connect(
            self._guard(self._restore_save_feedback))

        self._scan_done.connect(self._guard(self._on_scan_done))
        self._list_ready.connect(self._guard(self._on_list_ready))
        self._mesh_ready.connect(self._guard(self._on_mesh_ready))
        self._save_path_picked.connect(self._guard(self._on_save_path_picked))

        self._start_scan()

    # ---- teardown ---------------------------------------------------------
    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    def _refresh(self):
        """Re-read the profile after the modlist or load order changed.

        Everything derived from the profile is dropped: the record stack, the
        lazily indexed mod archives and the shown mesh. The process-wide caches
        underneath (AssetResolver's winner map, npc_body's per-plugin parses)
        key on file mtimes, so they re-read only what actually changed and a
        refresh over an untouched profile stays cheap.
        """
        if self._closing:
            return
        self._records = None
        self._records_gen = -1
        self._mod_archives = {}
        self._archive_mod_list = None
        self._archive_owner = {}
        self._current_data = None
        # Who was on screen, so the refresh lands back on them rather than
        # dumping the user at the top of a 4000-row list. Kept by IDENTITY,
        # not by entry: the mod providing the winning face may have changed,
        # which is the whole reason for refreshing.
        self._restore = ((self._current.plugin, self._current.formid)
                         if self._current is not None else None)
        self._current = None
        # In-flight reads belong to the old profile state.
        self._open_gen += 1
        self._mesh_reads.discard_pending()
        self._tex_sources.cancel()
        self._preview.cancel_load()
        self._portrait.hide()
        self._save_btn.setEnabled(False)
        self._log("View NPCs: refreshing from the profile…")
        self._start_scan()

    def _restore_selection(self):
        """Re-open the NPC that was on screen before a refresh, if still there."""
        want, self._restore = self._restore, None
        if want is None:
            return
        match = next((n for n in self._npcs
                      if (n.plugin, n.formid) == want and n.wins), None)
        if match is None:
            self._log("View NPCs: the NPC that was open is no longer in the "
                      "profile")
            return
        # NPC rows are the top level of the list, so no recursion is needed.
        for row in range(self._model.rowCount()):
            index = self._model.index(row, 0)
            node = self._model.node(index)
            if node is not None and node.payload is match:
                self._tree.setCurrentIndex(index)
                self._tree.scrollTo(index)
                break
        self._open_npc(match)

    def _begin_close(self):
        if self._closing:
            return False
        self._closing = True
        timer = getattr(self, "_save_feedback_timer", None)
        if timer is not None:
            timer.stop()
        self._pending_export = None
        self._gen += 1
        self._list_gen += 1
        self._open_gen += 1
        self._list_jobs.discard_pending()
        self._mesh_reads.discard_pending()
        self._tex_sources.cancel()
        self._preview.cancel_load()
        return True

    def _finish(self):
        if not self._begin_close():
            return
        self._on_close_cb()

    def tab_closing(self):
        """Let a tab-bar close cancel this view's asynchronous work."""
        self._begin_close()

    def event(self, e):
        # The tab's close deleteLater()s THIS host; the embedded preview never
        # gets its own DeferredDelete, so relay it - GL textures must be freed
        # while their context lives or their destructors deref a dead one.
        if e.type() == QEvent.DeferredDelete:
            try:
                self._begin_close()
                self._preview.cancel_load()
                self._preview._view.release_gl()
            except Exception:                            # noqa: BLE001
                pass
        return super().event(e)

    # ---- scan -------------------------------------------------------------
    def _start_scan(self):
        self._gen += 1
        gen = self._gen
        self._count.setText(self.tr("Scanning…"))
        try:
            self._resolver = _resolver_for(
                self._staging, self._modlist, self._profile_dir, self._data,
                self._game)
        except Exception as exc:                         # noqa: BLE001
            self._log(f"View NPCs: could not build the asset resolver: {exc}")
            self._resolver = None
        self._tex_sources.configure(self._staging, self._modlist, self._data,
                                    self._resolver)

        def worker():
            try:
                npcs = build_npc_catalog(
                    self._resolver, self._staging, self._modlist, self._data,
                    extra_mods=(self._mod,) if self._mod else (),
                    string_languages=_string_languages_for(self._game),
                    cancel=lambda: gen != self._gen)
            except Exception as exc:                     # noqa: BLE001
                self._log(f"View NPCs: scan failed: {exc}")
                npcs = []
            safe_emit(self._scan_done, gen, npcs)
            # Publish the list first, then prepare every body/outfit record in
            # the time the user spends choosing an NPC. Warming only the core
            # master still left DLC and patch plugins on the first-click path.
            if gen == self._gen:
                try:
                    import time
                    started = time.monotonic()
                    self._body_records()
                    if gen == self._gen:
                        self._log("View NPCs: body and outfit records ready "
                                  f"({(time.monotonic() - started) * 1000:.0f}ms)")
                except Exception as exc:                 # noqa: BLE001
                    self._log(f"View NPCs: body-record warm-up failed: {exc!r}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_done(self, gen: int, npcs: list):
        if gen != self._gen:
            return
        self._npcs = npcs
        self._rebuild_list()

    # ---- list -------------------------------------------------------------
    def _rebuild_list(self):
        """Filter the cached catalogue and rebuild the list off the UI thread."""
        self._debounce.stop()
        self._list_gen += 1
        gen = self._list_gen
        query = self._search.text().strip().lower()
        source = self._src_box.currentData() or _SRC_ALL
        overridden_only = self._only_overridden.isChecked()
        mod = self._mod if self._mod and self._only_mod.isChecked() else ""
        npcs = list(self._npcs)

        def worker():
            try:
                root, count = _build_npc_list(
                    npcs, query, source, overridden_only, mod)
            except Exception as exc:                     # noqa: BLE001
                self._log(f"View NPCs: could not build the list: {exc}")
                root, count = Node("", is_dir=True), 0
            safe_emit(self._list_ready, gen, root, count)

        self._list_jobs.submit(worker)

    def _on_list_ready(self, gen: int, root, count: int):
        if gen != self._list_gen:
            return
        self._model.set_root(root)
        self._count.setText(self.tr("{0} NPCs").format(f"{count:,}"))
        if self._restore is not None:
            self._restore_selection()

    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is None:
            return
        # A contested face both expands (revealing the other versions) and
        # previews the winning one.
        if self._model.rowCount(index) > 0:
            self._tree.setExpanded(index, not self._tree.isExpanded(index))
        if node.payload is not None:
            self._open_npc(node.payload)

    # ---- preview ----------------------------------------------------------
    def _open_npc(self, npc, tex_override=None, keep_view=False,
                  texture_reload=False):
        """Read THIS copy of the head off-thread and hand it to the viewport."""
        self._open_gen += 1
        gen = self._open_gen
        previous = self._current
        # Same face = a comparison between two mods' versions: hold the camera
        # so both land in the same pose. A different NPC reframes.
        keep = keep_view or (previous is not None
                             and previous.rel_key == npc.rel_key)
        self._current = npc
        if not texture_reload:
            self._tex_sources.arm(
                npc.entry,
                lambda ov, n=npc: self._open_npc(
                    n, tex_override=ov, keep_view=True, texture_reload=True))
        self._preview.cancel_load()
        self._save_btn.setEnabled(False)
        # The old face must go now: a portrait left up during the read would
        # label the incoming NPC with the previous one's likeness.
        if not keep_view:
            self._portrait.hide()
        self._preview.set_title(_title_for(npc), self.tr("Reading…"))

        if (texture_reload and self._current_data is not None
                and self._current_data[0] == npc):
            cached_body = (self._current_data[2]
                           if len(self._current_data) > 2 else None)
            self._on_mesh_ready(gen, self._current_data[1], npc,
                                (tex_override, keep, cached_body))
            return

        entry = npc.entry
        whole = self._whole_body.isChecked()
        outfit = self._outfit.isChecked()

        def worker():
            data = read_entry(entry, self._staging, self._data)
            # FO4 can override a face entirely through NPC_/HDPT records, with
            # no replacement FaceGeom file.  Appearance therefore has to be
            # resolved even for the head-only view.
            body = self._read_body(npc, outfit, whole=whole)
            safe_emit(self._mesh_ready, gen, data, npc,
                      (tex_override, keep, body))

        self._mesh_reads.submit(worker)

    # ---- portrait ---------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self._preview and event.type() == QEvent.Resize:
            self._place_portrait()
        return super().eventFilter(obj, event)

    def _on_portrait(self, image):
        if image is None or image.isNull():
            self._portrait.hide()
            return
        self._portrait.setPixmap(QPixmap.fromImage(image))
        self._portrait.resize(image.width() + 2, image.height() + 2)
        self._portrait.show()
        self._portrait.raise_()
        self._place_portrait()

    def _place_portrait(self):
        if self._portrait.isHidden():
            return
        margin = 12
        pane = self._preview.size()
        self._portrait.move(
            max(0, pane.width() - self._portrait.width() - margin),
            max(0, pane.height() - self._portrait.height() - margin))

    # ---- image export ----------------------------------------------------
    def _on_scene_ready(self, ready: bool):
        if self._pending_export is None:
            self._save_btn.setEnabled(bool(ready) and self._current is not None)

    def _choose_image_path(self):
        """Ask for the export backdrop, then capture and pick a destination."""
        if self._pending_export is not None or self._current is None:
            return
        self._save_btn.setEnabled(False)
        self._save_btn.setText(self.tr("Choosing background…"))
        ImageExportOverlay(
            self.window(), self._guard(self._on_background_chosen),
            current=self._preview.background_key(),
            labels={"light": self.tr("Light"), "grey": self.tr("Grey"),
                    "dark": self.tr("Dark"), "black": self.tr("Black"),
                    "green": self.tr("Green screen")})

    def _on_background_chosen(self, choice):
        """Capture as chosen, then ask where to write the PNG.

        *choice* is ``(background, face_only)`` from the overlay, or None when
        cancelled. Capturing before opening the asynchronous chooser keeps the
        image tied to the NPC the user clicked, even if they browse elsewhere
        while the chooser is open.
        """
        if choice is None:                           # cancelled / Esc
            self._save_reset()
            return
        background, face_only = choice
        # The overlay outlives a profile refresh or a tab close, so re-check
        # rather than trust the state that opened it.
        if self._current is None or not self._preview.can_capture():
            self._save_reset()
            return
        self._save_btn.setText(self.tr("Capturing…"))
        self._log(f"View NPCs: capturing "
                  f"{'face portrait' if face_only else 'turntable sheet'}"
                  f" on {background} background")
        image = (self._preview.capture_face_image(background) if face_only
                 else self._preview.capture_turntable_sheet(background))
        if image is None or image.isNull():
            self._log("View NPCs: image capture failed")
            self._save_feedback(self.tr("Capture failed"))
            return
        self._log(f"View NPCs: captured {image.width()}x{image.height()}")
        self._pending_export = image
        self._save_btn.setText(self.tr("Choose location…"))
        from Utils.portal_filechooser import pick_save_file
        pick_save_file(
            self.tr("Save NPC image"),
            lambda path: safe_emit(self._save_path_picked, path),
            current_name=_image_name_for(self._current, face_only),
            filters=[(self.tr("PNG images (*.png)"), ["*.png"]),
                     (self.tr("All files"), ["*"])])

    def _on_save_path_picked(self, path):
        image, self._pending_export = self._pending_export, None
        if image is None:
            self._save_feedback(self.tr("Save as image…"))
            return
        if not path:
            self._save_feedback(self.tr("Save as image…"))
            return
        dest = Path(path)
        if dest.suffix.lower() != ".png":
            dest = dest.with_name(dest.name + ".png")
        try:
            saved = image.save(str(dest), "PNG")
        except Exception as exc:                         # noqa: BLE001
            saved = False
            self._log(f"View NPCs: could not save image: {exc!r}")
        if saved:
            self._log(f"View NPCs: image saved to {dest}")
            self._save_feedback(self.tr("Saved!"))
        else:
            self._log(f"View NPCs: could not save image to {dest}")
            self._save_feedback(self.tr("Save failed"))

    def _save_reset(self):
        """Straight back to the resting state - no message, no 1.6s wait.

        Cancelling the background picker is not an outcome worth reporting, so
        the button must not sit disabled under a stale caption afterwards.
        """
        if self._closing:
            return
        self._save_btn.setText(self.tr("Save as image…"))
        self._save_btn.setEnabled(
            self._current is not None and self._preview.can_capture())

    def _save_feedback(self, text: str):
        if self._closing:
            return
        self._save_btn.setText(text)
        self._save_feedback_timer.start()

    def _restore_save_feedback(self):
        if self._pending_export is None:
            self._save_reset()

    def _reload_current(self, *_a):
        """Re-open the shown NPC after the whole-body toggle changed."""
        if self._current is not None:
            self._open_npc(self._current, keep_view=True)

    def _body_records(self):
        """The profile's assembly records, read once and reused.

        The WHOLE load order, not just the face's mod: a patch plugin can point
        an NPC's outfit at another mod's armour while shipping no meshes at
        all, and nothing narrower would ever see it.
        """
        with self._records_lock:
            if self._records is None or self._records_gen != self._gen:
                from Utils.npc_body import load_order_records
                gen = self._gen
                records = load_order_records(
                    self._profile_dir, self._staging, self._data,
                    cancel=lambda: gen != self._gen) or []
                # A refresh can invalidate the profile while the background
                # parse is running. Never publish that old stack into the new
                # generation; its warm-up will acquire this lock next.
                if gen != self._gen:
                    return []
                self._records = records
                self._records_gen = gen
                self._log(f"View NPCs: read {len(records)} plugin(s) "
                          f"for body and outfit records")
            return self._records

    def _from_mod_archives(self, rel: str):
        """Read a mesh from an enabled mod's own BSA, in modlist priority.

        The resolver's archive map is built from `bsa_index.bin` beside the
        staging folder. A profile-group staging that has never been scanned has
        no such file, so EVERY mod BSA is invisible and an armour shipped
        packed (Remiel's whole outfit) reads as missing while its loose
        siblings resolve fine. Archives are indexed lazily, highest-priority
        mod first, and the per-archive index is cached process-wide - only mods
        that actually ship one are touched, and only until the mesh is found.
        """
        if self._staging is None:
            return None
        from Utils.archive_lookup import ArchiveLookup, find_archives
        for mod in self._archive_mods():
            look = self._mod_archives.get(mod)
            if look is None:
                try:
                    look = ArchiveLookup(find_archives([Path(self._staging) / mod]),
                                         keep_prefix=BROWSE_PREFIXES)
                except Exception:                        # noqa: BLE001
                    look = False
                self._mod_archives[mod] = look
            if not look:
                continue
            try:
                blob = look.read(rel)
            except Exception:                            # noqa: BLE001
                blob = None
            if blob:
                self._archive_owner[rel] = mod
                self._log(f"View NPCs: {rel.rsplit('/', 1)[-1]} read from "
                          f"{mod}'s archive (not in the profile's BSA index)")
                return blob
        return None

    def _archive_mods(self) -> list:
        """Enabled mods that ship an archive, highest priority first."""
        if self._archive_mod_list is None:
            mods = []
            try:
                from Utils.modlist import read_modlist
                for e in read_modlist(Path(self._modlist)):
                    if e.is_separator or not e.enabled:
                        continue
                    d = Path(self._staging) / e.name
                    try:
                        if any(p.suffix.lower() in (".bsa", ".ba2")
                               for p in d.iterdir()):
                            mods.append(e.name)
                    except OSError:
                        continue
            except Exception:                            # noqa: BLE001
                mods = []
            self._archive_mod_list = mods
        return self._archive_mod_list

    def _part_plugin_dirs(self, rel: str) -> list:
        """Plugin folders that can retexture THIS part, its own mod first.

        Armour routinely bakes a dead texture path into the mesh and swaps in
        the real one through a TXST record in its OWN plugin - the Lustmord
        cape points at a cuirass texture that does not exist and would render
        white clay. The face's plugins cannot answer for the armour's mesh, so
        the mod that actually provides each part is looked up and used.
        """
        dirs = []
        mod = self._archive_owner.get(rel, "")
        if not mod and self._resolver is not None:
            try:
                mod = (self._resolver.loose_winners().get(rel)
                       or self._resolver.archive_winners().get(rel) or "")
            except Exception:                            # noqa: BLE001
                mod = ""
        if mod and self._staging is not None:
            dirs.append(Path(self._staging) / mod)
        if self._data is not None:
            dirs.append(Path(self._data))
        return dirs

    def _read_body(self, npc, outfit: bool = True, whole: bool = True):
        """Resolve runtime face changes and optional body off the UI thread."""
        try:
            from Utils.npc_body import resolve_body, resolve_face, scope_records
            all_records = self._body_records()
            if not all_records:
                return None
            # The body follows the head ON SCREEN, not the one the game would
            # load: showing a vanilla face on the winning replacer's tanned
            # body leaves a seam at the neck.
            baseline = scope_records(
                all_records, getattr(npc.entry, "mod", ""))
            if not baseline:
                return None

            # A winning FO4 asset can still be the vanilla NIF while a later
            # ESP changes the runtime head.  Non-winning comparison rows stay
            # scoped to the record state that generated their selected NIF.
            game_id = str(getattr(self._game, "game_id", "") or "").lower()
            runtime_face = game_id in ("fallout4", "fallout4vr") and npc.wins
            records = all_records if runtime_face else baseline
            got = resolve_body(
                npc.plugin, npc.formid, records,
                outfit=outfit if whole else False)
            face = (resolve_face(npc.plugin, npc.formid, records, baseline)
                    if runtime_face else {})

            assembly = {
                "parts": [], "skeleton": None,
                "skin_tint": got.get("skin_tint"),
                "hide_hair": bool(got.get("hide_hair")) if whole else False,
                "head_parts": [], "eye_textures": face.get("eye_textures", ()),
                "face_morph": None,
                "face_skin_tint": face.get("face_skin_tint"),
                "record_mods": face.get("record_mods", []),
            }

            for part in face.get("hair", ()):
                blob = self._resolver.read(part.rel) if self._resolver else None
                if not blob:
                    blob = self._from_mod_archives(part.rel)
                if blob:
                    assembly["head_parts"].append(
                        (blob, part.rel, part.textures,
                         self._part_plugin_dirs(part.rel)))
                else:
                    self._log(f"View NPCs: head-part mesh not found: {part.rel}")
            tri_rel = face.get("chargen_tri", "")
            morphs = face.get("morphs", {})
            if tri_rel and morphs:
                tri = self._resolver.read(tri_rel) if self._resolver else None
                if not tri:
                    tri = self._from_mod_archives(tri_rel)
                if tri:
                    assembly["face_morph"] = (tri, morphs, tri_rel)
                else:
                    self._log(f"View NPCs: chargen TRI not found: {tri_rel}")

            if not whole:
                return assembly
            if not got["parts"]:
                self._log(f"View NPCs: no body records for {npc_label(npc)} "
                          f"- showing the head alone")
                return assembly
            parts = []
            weight = got.get("weight", 1.0)
            for part in got["parts"]:
                blob = self._resolver.read(part.rel) if self._resolver else None
                if not blob:
                    blob = self._from_mod_archives(part.rel)
                if blob:
                    low_blob = None
                    if weight < 0.999 and part.rel.lower().endswith("_1.nif"):
                        low_rel = part.rel[:-6] + "_0.nif"
                        low_blob = (self._resolver.read(low_rel)
                                    if self._resolver else None)
                    parts.append((blob, part.rel, part.attach,
                                  low_blob, weight, part.textures,
                                  self._part_plugin_dirs(part.rel)))
                else:
                    self._log(f"View NPCs: body mesh not found: {part.rel}")
            assembly["parts"] = parts
            skeleton = None
            for rel in got["skeleton"]:
                skeleton = self._resolver.read(rel) if self._resolver else None
                if skeleton:
                    break
            if not skeleton:
                # Without one the parts cannot be placed relative to each
                # other, so showing them would be worse than not.
                self._log("View NPCs: no skeleton found - showing the head alone")
                assembly["parts"] = []
                return assembly
            assembly["skeleton"] = skeleton
            return assembly
        except Exception as exc:                         # noqa: BLE001
            self._log(f"View NPCs: body assembly failed: {exc!r}")
            return None

    def _on_mesh_ready(self, gen: int, data, npc, load_opts=None):
        tex_override, keep_view, body = _load_options(load_opts)
        if gen != self._open_gen:
            return
        title = _title_for(npc)
        if not data:
            status = self.tr("could not be read")
            self._preview.set_title(title, status)
            self._preview.clear(status)
            self._log(f"View NPCs: could not read {npc.rel_key}")
            return
        # Texture-source changes rebuild the render but must not re-read and
        # reassemble the actor. Retain the body beside the head bytes so that
        # reload path can pass the complete three-part payload back here.
        self._current_data = (npc, data, body)
        entry = npc.entry
        archives = _entry_archives(entry, self._staging)
        # Hair colour and TXST overrides both need the plugins: the face's own
        # mod first, then the data folder holding the masters.
        dirs = []
        selected_roots = []
        if entry.mod and self._staging is not None:
            mod_dir = Path(self._staging) / entry.mod
            dirs.append(mod_dir)
            selected_roots.append(mod_dir)
        elif entry.archive is not None:
            dirs.append(Path(entry.archive).parent)
        # An ESP-only FO4 replacer has no mesh entry of its own, so the row's
        # source remains the vanilla archive.  Its plugin directory still has
        # to lead the hair-colour/TXST lookup.
        if isinstance(body, dict) and self._staging is not None:
            record_dirs = [Path(self._staging) / mod
                           for mod in body.get("record_mods", ())]
            dirs = [d for d in record_dirs if d not in dirs] + dirs
        if self._data is not None:
            dirs.append(Path(self._data))
        if isinstance(body, dict):
            parts = body.get("parts")
            skeleton = body.get("skeleton")
            skin_tint = body.get("skin_tint")
            hide_hair = bool(body.get("hide_hair"))
            head_parts = body.get("head_parts")
            eye_textures = body.get("eye_textures")
            face_morph = body.get("face_morph")
            face_skin_tint = body.get("face_skin_tint")
        else:
            # Compatibility with in-flight payloads created before a hot UI
            # reload, and with callers using the historical tuple.
            parts, skeleton, skin_tint, hide_hair = (
                body if body else (None, None, None, False))
            head_parts = eye_textures = face_morph = None
            face_skin_tint = None
        self._preview.set_nif_data(data, title, self._resolver, archives,
                                   tex_override, keep_view,
                                   mesh_rel=entry.rel_key, plugin_dirs=dirs,
                                   parts=parts, skeleton=skeleton,
                                   skin_tint=skin_tint,
                                   hide_hair=hide_hair,
                                   head_parts=head_parts,
                                   eye_textures=eye_textures,
                                   face_morph=face_morph,
                                   face_skin_tint=face_skin_tint,
                                   selected_roots=selected_roots)


# ---- helpers ---------------------------------------------------------------

def _entry_archives(entry, staging):
    """Archives to search beside a head's own copy, or None.

    A replacer commonly ships a LOOSE FaceGeom head plus a private BSA of its
    textures. An imported or very large profile may leave that BSA out of the
    generated global archive index, so the mod's own folder is swept as a
    bounded fallback. A head read FROM an archive gets its siblings the same
    way - a '- Textures.ba2' next to it is invisible to the resolver when the
    mod is not installed.
    """
    from Utils.archive_lookup import ArchiveLookup, find_archives
    root = None
    if getattr(entry, "mod", "") and staging is not None:
        root = Path(staging) / entry.mod
    elif getattr(entry, "archive", None) is not None:
        root = Path(entry.archive).parent
    if root is None:
        return None
    try:
        return ArchiveLookup(find_archives([root]), keep_prefix=ASSET_PREFIXES)
    except Exception:                                    # noqa: BLE001
        return None


def _load_options(load_opts):
    """Normalise old/new mesh-ready payloads to (override, keep, body)."""
    if not load_opts:
        return None, False, None
    tex_override, keep_view, *rest = load_opts
    return tex_override, keep_view, rest[0] if rest else None


def _string_languages_for(game) -> tuple[str, ...]:
    """String-table suffixes used by this game, preferred first.

    Skyrim names its English tables ``*_english.strings``; Fallout 4 uses
    ``*_en.strings``. Keeping the other spelling as a fallback also covers
    mods authored with the opposite convention.
    """
    game_id = str(getattr(game, "game_id", "") or "").lower()
    if game_id in ("fallout4", "fallout4vr"):
        return "en", "english"
    return "english", "en"


def _resolver_for(staging, modlist, profile_dir, data, game):
    """One resolver for both the winner flags and the viewport's textures.

    Built with the FaceGeom prefix in keep_prefix from the start - the NIF
    Viewer's own helper keeps a meshes/ prefix that would not cover these.
    """
    if staging is None:
        return None
    from Utils.asset_resolver import AssetResolver
    return AssetResolver(staging_dir=staging, modlist_path=modlist,
                         profile_dir=profile_dir, data_dir=data, game=game,
                         keep_prefix=BROWSE_PREFIXES)


def _title_for(npc) -> str:
    """'Nazeem  ·  0013BBF  ·  Skyrim - Meshes0.bsa'."""
    return f"{npc_label(npc)}  ·  {npc.formid_hex}  ·  {npc.source}"


def _image_name_for(npc, face_only: bool = False) -> str:
    """A portable, identifiable default name for an exported NPC image."""
    label = npc_label(npc).strip() or "NPC"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    safe = "_".join(part for part in safe.split("_") if part).strip("-_")
    kind = "portrait" if face_only else "reference"
    return f"{safe or 'NPC'}_{npc.formid_hex}_{kind}.png"


def _keep(npc, source: str) -> bool:
    if source == _SRC_MODS:
        return npc.entry.kind in _MOD_KINDS
    if source == _SRC_VANILLA:
        return npc.entry.kind in _VANILLA_KINDS
    return True


def _matches(npc, query: str) -> bool:
    """Name, editor id, FormID or providing mod - whichever the user typed."""
    return (query in npc_label(npc).lower()
            or query in npc.edid.lower()
            or query in npc.formid_hex.lower()
            or query in npc.plugin.lower()
            or query in npc.source.lower())


def _build_npc_list(npcs: list, query: str, source: str,
                    overridden_only: bool, mod: str = "") -> tuple[Node, int]:
    """A flat NPC list; a contested face expands into one row per version."""
    groups: dict[str, list] = {}
    for n in npcs:
        if not _keep(n, source):
            continue
        groups.setdefault(n.rel_key, []).append(n)

    if mod:
        # Keep the WHOLE group so the mod's faces sit next to what they beat.
        groups = {k: g for k, g in groups.items()
                  if any(n.entry.mod == mod for n in g)}
    if query:
        groups = {k: g for k, g in groups.items()
                  if any(_matches(n, query) for n in g)}
    if overridden_only:
        groups = {k: g for k, g in groups.items() if len(g) > 1}

    root = Node("", is_dir=True)
    # Named NPCs first, then editor ids, then the bare FormIDs nobody can read:
    # opening on a screenful of '000807' hides the answer the user came for.
    ordered = sorted(groups.items(), key=lambda kv: _sort_key(_winner(kv[1])))
    for _key, group in ordered:
        winner = _winner(group)
        leaf = Node(npc_label(winner), is_dir=False, parent=root,
                    payload=winner)
        leaf.source = (winner.source if len(group) == 1
                       else f"{winner.source}  +{len(group) - 1}")
        if len(group) > 1:
            leaf.code = 1
            for n in group:
                child = Node(n.source, is_dir=False, parent=leaf, payload=n)
                child.code = 1 if n.wins else 0
                leaf.children.append(child)
        root.children.append(leaf)
    return root, len(ordered)


def _winner(group: list):
    return next((n for n in group if n.wins), group[0])


def _sort_key(npc):
    """(rank, label) - 0 named, 1 editor id only, 2 nothing but a FormID."""
    if npc.name:
        rank = 0
    elif npc.edid:
        rank = 1
    else:
        rank = 2
    return rank, npc_label(npc).lower(), npc.rel_key


__all__ = ["NpcViewerView", "BROWSE_PREFIXES"]
