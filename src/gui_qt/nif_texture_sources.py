"""
nif_texture_sources.py
Drives a NifPreview's "preview with another mod's textures" picker.

Lists every provider of the mesh's texture paths, fills the preview's combo,
and re-renders through a byte override on pick. Shared by the NIF Viewer tab
and the Mod Files preview.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from gui_qt.safe_emit import safe_emit
from gui_qt.worker import LatestWorker
from Utils.assets.catalog import MeshEntry, find_copies, read_entry, source_label

__all__ = ["TextureSourceController", "group_by_source", "make_override"]


def group_by_source(copies: dict) -> tuple[dict, int]:
    """{origin label: {rel_key: MeshEntry}}, winner's source first."""
    by_source: dict[str, dict] = {}
    covered = 0
    for key, entries in copies.items():
        if entries:
            covered += 1
        for e in entries:
            by_source.setdefault(source_label(e), {})[key] = e
    return by_source, covered


def make_override(chosen: dict | None, staging, data_dir):
    """rel -> bytes for the paths *chosen* provides; other paths return None
    and fall through to normal resolution."""
    if not chosen:
        return None
    from Utils.assets.resolver import DirCache, normalise
    dirs = DirCache()
    representative = next(iter(chosen.values()), None)

    def override(rel: str):
        key = normalise(rel)
        entry = chosen.get(key)
        if entry is None and not key.startswith(("textures/", "materials/")):
            return None
        if entry is None and representative is not None:
            # A chosen material file can reveal texture paths that were not in
            # the original winning material.  Keep resolving those later paths
            # from the same physical source instead of falling back and mixing
            # providers again.
            source_path = None
            if representative.source_path is not None:
                source = representative.source_path
                parts = representative.rel_key.split("/")
                suffix = tuple(p.lower() for p in source.parts[-len(parts):])
                if suffix == tuple(parts):
                    root = source.parents[len(parts) - 1]
                    source_path = dirs.resolve(root, key) or root / key
            entry = MeshEntry(key, representative.kind,
                              mod=representative.mod,
                              archive=representative.archive,
                              source_path=source_path)
        # Runs on the parse worker; read_entry is plain file/archive I/O.
        return (read_entry(entry, staging, data_dir, dirs=dirs)
                if entry is not None else None)

    return override


class TextureSourceController(QObject):
    """Fills a NifPreview's texture-source picker and applies the choice."""

    _sources_ready = Signal(int, object)      # (generation, {rel_key: [copies]})

    def __init__(self, preview, log_fn=None, parent=None):
        super().__init__(parent or preview)
        self._preview = preview
        self._log = log_fn or (lambda _m: None)
        self._gen = 0
        self._token = None            # identifies the mesh on show
        self._scanned = None          # token whose sources are already listed
        self._reload = None           # reload(override) -> re-render this mesh
        self._by_source: dict = {}
        self._source_jobs = LatestWorker("nif-texture-sources")
        self._staging = self._modlist = self._data = self._resolver = None

        self._sources_ready.connect(self._on_sources_ready)
        preview.textures_seen.connect(self._on_textures_seen)
        preview.texture_source_changed.connect(self._on_pick)

    def configure(self, staging, modlist_path, data_dir, resolver):
        """Where to look; call whenever the game/profile behind the host moves."""
        self._staging = staging
        self._modlist = modlist_path
        self._data = data_dir
        self._resolver = resolver

    def cancel(self):
        """Invalidate pending provider scans when the hosting preview closes."""
        self._gen += 1
        self._source_jobs.discard_pending()
        self._token = self._scanned = self._reload = None
        self._by_source = {}
        self._preview.set_texture_sources([])

    def arm(self, token, reload_fn):
        """Reset sources for a new load; picks call reload_fn(override)."""
        self.cancel()
        self._token = token
        self._reload = reload_fn

    def _on_textures_seen(self, paths):
        """The mesh asked for these textures - list who else provides them."""
        if not paths or self._token is None:
            return
        # A reload with an override reports the same paths again; don't rebuild
        # the picker underneath the choice just made.
        if self._scanned == self._token:
            return
        self._scanned = self._token
        gen = self._gen
        keys = list(paths)
        staging, modlist, data = self._staging, self._modlist, self._data
        resolver = self._resolver

        # The resolver's keep_prefix keys the archive TOC cache; reusing it
        # means the vanilla scan reads warm indexes instead of re-parsing.
        prefix = getattr(resolver, "keep_prefix", "") or ("textures/",
                                                          "materials/")

        def worker():
            try:
                copies = find_copies(keys, resolver, staging, modlist, data,
                                     keep_prefix=prefix)
            except Exception as exc:                     # noqa: BLE001
                self._log(f"NIF preview: texture lookup failed: {exc}")
                copies = {}
            safe_emit(self._sources_ready, gen, copies)

        self._source_jobs.submit(worker)

    def _on_sources_ready(self, gen: int, copies: dict):
        if gen != self._gen:
            return
        self._by_source, total = group_by_source(copies)
        if len(self._by_source) < 2:
            # A single provider for everything - nothing to swap to.
            self._preview.set_texture_sources([])
            return
        items = [(self.tr("Textures: as the game loads"), None)]
        for label, entries in self._by_source.items():
            items.append((self.tr("Textures: {0} ({1}/{2})").format(
                label, len(entries), total), label))
        self._preview.set_texture_sources(items, current=None)

    def _on_pick(self, label):
        """Re-render the current mesh with the picked source's textures."""
        if self._reload is None:
            return
        chosen = self._by_source.get(label) if label else None
        self._reload(make_override(chosen, self._staging, self._data))
