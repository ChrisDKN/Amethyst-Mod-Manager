"""NIF Viewer - browse every mesh in the profile and the vanilla game, and
preview it in 3D.

Left: a unified meshes/… tree from Utils.mesh_catalog - every copy from mods,
the data folder and BSA/BA2 archives, contested paths expanding into one row
per copy with the game's winner tinted green. Right: the gui_qt.nif_preview
viewport, fed raw bytes. Scans and tree builds run off-thread behind a
generation counter.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QSplitter, QTreeView, QVBoxLayout, QWidget,
)

from gui_qt.eliding_label import ElidingLabel
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.nif_preview import ASSET_PREFIXES, NifPreview
from gui_qt.path_tree import (
    Node, PathTreeDelegate, PathTreeModel, build_tree,
)
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, button_qss, _c
from gui_qt.nif_texture_sources import TextureSourceController
from gui_qt.worker import LatestWorker
from Utils.mesh_catalog import (
    DATA_ARCHIVE, DATA_LOOSE, DEFAULT_PREFIX, MOD_ARCHIVE, MOD_LOOSE,
    build_catalog, read_entry, source_label,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# The catalogue's own prefix plus what the viewport needs to resolve textures,
# so ONE AssetResolver serves both the listing and the 3D preview.
BROWSE_PREFIXES = ASSET_PREFIXES + (DEFAULT_PREFIX,)

_SRC_ALL, _SRC_MODS, _SRC_VANILLA = "all", "mods", "vanilla"
# Paths a mod-scoped open expands automatically (see _on_tree_ready).
_AUTO_EXPAND_MAX = 400
_MOD_KINDS = (MOD_LOOSE, MOD_ARCHIVE)
_VANILLA_KINDS = (DATA_LOOSE, DATA_ARCHIVE)


class NifViewerView(QWidget):
    """Full-screen mesh browser + 3D preview."""

    _scan_done = Signal(int, object)      # (generation, [MeshEntry])
    _tree_ready = Signal(int, object, int)  # (generation, root Node, path count)
    # (generation, bytes|None, entry, (tex_override, keep_view))
    _mesh_ready = Signal(int, object, object, object)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 mod: str = "", **_extra):
        super().__init__()
        self.setObjectName("NifViewerView")
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        # Opened from a mod's right-click menu: scan includes the mod even when
        # it is disabled, and the list starts filtered to what it provides.
        self._mod = (mod or "").strip()
        self._closing = False
        self._gen = 0
        self._tree_gen = 0
        self._open_gen = 0
        self._entries: list = []
        self._resolver = None
        self._current_entry = None
        self._current_data = None
        self._restore = None
        self._tree_jobs = LatestWorker("nif-catalog-tree")
        self._mesh_reads = LatestWorker("nif-mesh-read")

        profile = ""
        if ctx is not None:
            profile = getattr(ctx, "profile_name", "") or ""
        self._staging, self._profile_dir, self._modlist, self._data = \
            _paths_for(game, profile)

        pal = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("HeaderBar")
        # FlowLayout: the search box and filters wrap instead of setting a
        # floor under the tab's width.
        hb = FlowLayout(bar, spacing=8)
        hb.setContentsMargins(12, 8, 8, 8)
        enable_height_for_width(bar)

        title = (self.tr("NIF Viewer - {0} ▸ {1}").format(game.name, self._mod)
                 if self._mod else
                 self.tr("NIF Viewer - {0}").format(game.name))
        head = ElidingLabel(title, max_width=340)
        head.setStyleSheet(f"color:{_c(pal,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(head)

        self._count = QLabel(self.tr("Scanning…"))
        self._count.setStyleSheet(f"color:{_c(pal,'TEXT_DIM')};")
        hb.addWidget(self._count)
        hb.add_stretch()

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search meshes and mods…"))
        self._search.setToolTip(self.tr(
            "Match a mesh path, or the name of a mod or archive that provides one"))
        self._search.setClearButtonEnabled(True)
        # Fixed, not minimum: FlowLayout places an item at its sizeHint width,
        # and a minimum here would be a floor under the bar too.
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(lambda _t: self._debounce.start())
        hb.addWidget(self._search)

        self._src_box = QComboBox()
        self._src_box.setToolTip(self.tr("Limit the list to one side of the setup"))
        for label, key in ((self.tr("All sources"), _SRC_ALL),
                           (self.tr("Mods only"), _SRC_MODS),
                           (self.tr("Vanilla only"), _SRC_VANILLA)):
            self._src_box.addItem(label, key)
        self._src_box.activated.connect(lambda _i: self._rebuild_tree())
        hb.addWidget(self._src_box)

        self._only_overridden = QCheckBox(self.tr("Only overridden"))
        self._only_overridden.setToolTip(self.tr(
            "Show only meshes provided by more than one source"))
        self._only_overridden.clicked.connect(lambda _on: self._rebuild_tree())
        hb.addWidget(self._only_overridden)

        # Scoped open: on by default, but unticking it opens the whole
        # catalogue rather than forcing a second tab from the Wizard menu.
        self._only_mod = QCheckBox(self.tr("Only this mod"))
        self._only_mod.setToolTip(self.tr(
            "Show only meshes {0} provides, alongside the copies they "
            "compete with").format(self._mod))
        self._only_mod.setChecked(True)
        self._only_mod.clicked.connect(lambda _on: self._rebuild_tree())
        self._only_mod.setVisible(bool(self._mod))
        if self._mod:
            hb.addWidget(self._only_mod)

        self._refresh_btn = QPushButton(self.tr("⟳ Refresh"))
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.setToolTip(self.tr(
            "Re-scan the profile after changing the modlist or load order"))
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

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)

        tools = QWidget()
        tl = FlowLayout(tools, spacing=6)
        tl.setContentsMargins(8, 4, 8, 4)
        enable_height_for_width(tools)
        self._expand_btn = QPushButton(self.tr("Expand All"))
        self._expand_btn.setCursor(Qt.PointingHandCursor)
        self._expand_btn.clicked.connect(self._expand_all)
        tl.addWidget(self._expand_btn)
        collapse_btn = QPushButton(self.tr("Collapse All"))
        collapse_btn.setCursor(Qt.PointingHandCursor)
        collapse_btn.clicked.connect(self._tree_collapse_all)
        tl.addWidget(collapse_btn)
        lv.addWidget(tools)

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
        # A QTreeView's own minimumSizeHint is generous; without this the pane
        # cannot be dragged narrow and the splitter clips its contents instead.
        self._tree.setMinimumWidth(120)
        # We draw our own arrow, so a name-column click toggles expand.
        self._tree.clicked.connect(self._on_clicked)
        self._tree.expanded.connect(lambda *_: self._tree.viewport().update())
        self._tree.collapsed.connect(lambda *_: self._tree.viewport().update())
        lv.addWidget(self._tree, 1)
        split.addWidget(left)

        self._preview = NifPreview(None, self.tr("Select a mesh"),
                                   resolver=None, log_fn=self._log)
        self._preview.setMinimumWidth(160)
        self._tex_sources = TextureSourceController(self._preview, self._log)
        split.addWidget(self._preview)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 900])
        v.addWidget(split, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._rebuild_tree)

        self._scan_done.connect(self._guard(self._on_scan_done))
        self._tree_ready.connect(self._guard(self._on_tree_ready))
        self._mesh_ready.connect(self._guard(self._on_mesh_ready))

        self._start_scan()

    # ---- teardown ---------------------------------------------------------
    def _guard(self, fn):
        return lambda *a: None if self._closing else fn(*a)

    def _refresh(self):
        """Re-scan after the modlist or load order changed.

        The catalogue and the shown mesh are dropped; the process-wide caches
        beneath (AssetResolver's winner map, the per-archive indexes) key on
        file mtimes, so an untouched profile re-scans cheaply.
        """
        if self._closing:
            return
        self._restore = (self._current_entry.rel_key
                         if self._current_entry is not None else None)
        self._current_entry = None
        self._current_data = None
        self._open_gen += 1
        self._mesh_reads.discard_pending()
        self._tex_sources.cancel()
        self._preview.cancel_load()
        self._log("NIF Viewer: refreshing from the profile…")
        self._start_scan()

    def _restore_selection(self):
        """Re-open the mesh that was on screen before a refresh, by PATH.

        Not by entry: the point of refreshing is that the winning copy may
        have changed, and the user wants whichever one now wins that path.
        """
        want, self._restore = self._restore, None
        if want is None:
            return
        match = next((e for e in self._entries
                      if e.rel_key == want and e.wins), None)
        if match is None:
            match = next((e for e in self._entries if e.rel_key == want), None)
        if match is None:
            self._log("NIF Viewer: the mesh that was open is no longer in the "
                      "profile")
            return
        self._open_entry(match)

    def _finish(self):
        if self._closing:
            return
        self._closing = True
        self._gen += 1            # abandon any in-flight scan
        self._tree_gen += 1
        self._open_gen += 1
        self._tree_jobs.discard_pending()
        self._mesh_reads.discard_pending()
        self._tex_sources.cancel()
        self._preview.cancel_load()
        self._on_close_cb()

    def event(self, e):
        # close_tab deleteLater()s THIS host; the embedded preview never gets
        # its own DeferredDelete, so relay it - GL textures must be freed while
        # their context lives (orphaned ones segfault in GC: stale context
        # deref in QOpenGLTexture's destructor).
        if e.type() == QEvent.DeferredDelete:
            try:
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
            self._log(f"NIF Viewer: could not build the asset resolver: {exc}")
            self._resolver = None
        self._tex_sources.configure(self._staging, self._modlist, self._data,
                                    self._resolver)

        def worker():
            try:
                entries = build_catalog(
                    self._resolver, self._staging, self._modlist, self._data,
                    extra_mods=(self._mod,) if self._mod else (),
                    cancel=lambda: gen != self._gen)
            except Exception as exc:                     # noqa: BLE001
                self._log(f"NIF Viewer: scan failed: {exc}")
                entries = []
            safe_emit(self._scan_done, gen, entries)

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_done(self, gen: int, entries: list):
        if gen != self._gen:
            return
        self._entries = entries
        self._rebuild_tree()

    # ---- tree -------------------------------------------------------------
    def _rebuild_tree(self):
        """Filter the cached catalogue and rebuild the tree off the UI thread."""
        self._debounce.stop()
        self._tree_gen += 1
        gen = self._tree_gen
        query = self._search.text().strip().lower()
        source = self._src_box.currentData() or _SRC_ALL
        overridden_only = self._only_overridden.isChecked()
        mod = self._mod if self._mod and self._only_mod.isChecked() else ""
        entries = list(self._entries)

        def worker():
            try:
                root, count = _build_catalog_tree(
                    entries, query, source, overridden_only, mod)
            except Exception as exc:                     # noqa: BLE001
                self._log(f"NIF Viewer: could not build the tree: {exc}")
                root, count = Node("", is_dir=True), 0
            safe_emit(self._tree_ready, gen, root, count)

        self._tree_jobs.submit(worker)

    def _on_tree_ready(self, gen: int, root, count: int):
        if gen != self._tree_gen:
            return
        self._model.set_root(root)
        self._count.setText(self.tr("{0} meshes").format(f"{count:,}"))
        # A scoped open lands on one mod's handful of meshes: expand so they
        # are on screen instead of hidden under a collapsed 'meshes' root.
        # (expandAll is ~1 s on the full 22.7k-path vanilla tree - hence the cap.)
        if self._mod and 0 < count <= _AUTO_EXPAND_MAX:
            self._tree.expandAll()
        if self._restore is not None:
            self._restore_selection()

    def _expand_all(self):
        # ~1 s on a full vanilla Skyrim tree (22.7k paths) - busy cursor so an
        # explicit button press does not read as a hang.
        self._with_wait_cursor(self._tree.expandAll)

    def _tree_collapse_all(self):
        self._with_wait_cursor(self._tree.collapseAll)

    @staticmethod
    def _with_wait_cursor(fn):
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            fn()
        finally:
            QApplication.restoreOverrideCursor()

    def _on_clicked(self, index):
        node = self._model.node(index)
        if node is None:
            return
        # A contested mesh row both expands (revealing its other copies) and
        # previews the winning one - the two are not in conflict.
        if self._model.rowCount(index) > 0:
            self._tree.setExpanded(index, not self._tree.isExpanded(index))
        if node.payload is not None:
            self._open_entry(node.payload)

    # ---- preview ----------------------------------------------------------
    def _open_entry(self, entry, tex_override=None, keep_view=False,
                    texture_reload=False):
        """Read THIS copy (not the winner) off-thread and hand it to the view."""
        self._open_gen += 1
        gen = self._open_gen
        # Same mesh path = a comparison: hold the camera so both versions land
        # in the same pose. Only a different mesh reframes.
        previous = self._current_entry
        keep = keep_view or (previous is not None
                             and previous.rel_key == entry.rel_key)
        self._current_entry = entry
        if not texture_reload:
            # Re-arm on user-driven opens only. The token is the COPY, not the
            # path: two mods' versions of one mesh can name different textures.
            self._tex_sources.arm(
                entry,
                lambda ov, e=entry: self._open_entry(
                    e, tex_override=ov, keep_view=True, texture_reload=True))
        # The archive/loose read below may take a moment.  Invalidate a prior
        # parse now so it cannot finish under this newly-selected mesh's title.
        self._preview.cancel_load()
        self._preview.set_title(_title_for(entry), self.tr("Reading…"))

        # Texture-source changes rebuild the same mesh.  Its NIF bytes are
        # immutable for the lifetime of this catalog, so avoid decompressing
        # the archive member again.
        if (texture_reload and self._current_data is not None
                and self._current_data[0] == entry):
            self._on_mesh_ready(gen, self._current_data[1], entry,
                                (tex_override, keep))
            return

        def worker():
            data = read_entry(entry, self._staging, self._data)
            safe_emit(self._mesh_ready, gen, data, entry, (tex_override, keep))

        self._mesh_reads.submit(worker)

    def _on_mesh_ready(self, gen: int, data, entry, load_opts=None):
        tex_override, keep_view = load_opts or (None, False)
        if gen != self._open_gen:
            return
        title = _title_for(entry)
        if not data:
            status = self.tr("could not be read")
            self._preview.set_title(title, status)
            self._preview.clear(status)
            self._log(f"NIF Viewer: could not read {entry.rel_key}")
            return
        self._current_data = (entry, data)
        archives = None
        archive = entry.archive
        if archive is not None:
            # The archive's own folder often holds a sibling '- Textures.ba2'
            # that the resolver cannot see when the mod is not installed.
            try:
                from Utils.archive_lookup import ArchiveLookup, find_archives
                archives = ArchiveLookup(find_archives([Path(archive).parent]),
                                         keep_prefix=ASSET_PREFIXES)
            except Exception:                            # noqa: BLE001
                archives = None
        # Plugin TXST overrides: the copy's own mod folder first, then the
        # data folder (its big vanilla masters are size-capped away).
        dirs = []
        if entry.mod and self._staging is not None:
            dirs.append(Path(self._staging) / entry.mod)
        elif entry.archive is not None:
            dirs.append(Path(entry.archive).parent)
        if self._data is not None:
            dirs.append(Path(self._data))
        self._preview.set_nif_data(data, title, self._resolver, archives,
                                   tex_override, keep_view,
                                   mesh_rel=entry.rel_key, plugin_dirs=dirs)


# ---- helpers ---------------------------------------------------------------

def _paths_for(game, profile: str):
    """(staging, profile_dir, modlist, data_dir) - same shape as game_state."""
    staging = profile_dir = modlist = data = None
    try:
        p = game.get_effective_mod_staging_path()
        staging = p if p and Path(p).is_dir() else None
    except Exception:                                    # noqa: BLE001
        staging = None
    try:
        if profile:
            profile_dir = game.get_profile_root() / "profiles" / profile
            modlist = profile_dir / "modlist.txt"
    except Exception:                                    # noqa: BLE001
        profile_dir = modlist = None
    try:
        d = game.get_mod_data_path()
        data = Path(d) if d and Path(d).is_dir() else None
    except Exception:                                    # noqa: BLE001
        data = None
    return staging, profile_dir, modlist, data


def _resolver_for(staging, modlist, profile_dir, data, game):
    """One resolver for both the winner flags and the viewport's textures."""
    if staging is None:
        return None
    from Utils.asset_resolver import AssetResolver
    return AssetResolver(staging_dir=staging, modlist_path=modlist,
                         profile_dir=profile_dir, data_dir=data, game=game,
                         keep_prefix=BROWSE_PREFIXES)


def _title_for(entry) -> str:
    return f"{entry.name}  ·  {source_label(entry)}"


def _keep(entry, source: str) -> bool:
    if source == _SRC_MODS:
        return entry.kind in _MOD_KINDS
    if source == _SRC_VANILLA:
        return entry.kind in _VANILLA_KINDS
    return True


def _build_catalog_tree(entries: list, query: str, source: str,
                        overridden_only: bool,
                        mod: str = "") -> tuple[Node, int]:
    """Unified meshes/… tree; contested paths expand into one row per copy."""
    groups: dict[str, list] = {}
    for e in entries:
        if not _keep(e, source):
            continue
        groups.setdefault(e.rel_key, []).append(e)

    if mod:
        # Same rule as the provider search: keep the WHOLE group so the mod's
        # meshes appear next to whatever they override or lose to.
        groups = {k: g for k, g in groups.items()
                  if any(e.mod == mod for e in g)}
    if query:
        # Match the path OR any provider, and keep the WHOLE group - a mod-name
        # search is only useful next to the copies that mod competes with.
        groups = {k: g for k, g in groups.items()
                  if query in k
                  or any(query in source_label(e).lower() for e in g)}
    if overridden_only:
        groups = {k: g for k, g in groups.items() if len(g) > 1}

    root, files = build_tree(sorted(groups))
    for key, group in groups.items():
        leaf = files.get(key)
        if leaf is None:
            continue
        winner = next((e for e in group if e.wins), group[0])
        leaf.payload = winner
        if len(group) == 1:
            leaf.source = source_label(winner)
            continue
        # Contested: green on the row that wins, one dim child per copy. The
        # +N says the copies are there before anyone expands the row.
        leaf.source = f"{source_label(winner)}  +{len(group) - 1}"
        leaf.code = 1
        for e in group:
            child = Node(source_label(e), is_dir=False, parent=leaf,
                         payload=e)
            child.code = 1 if e.wins else 0
            leaf.children.append(child)
    return root, len(groups)


__all__ = ["NifViewerView", "BROWSE_PREFIXES"]
