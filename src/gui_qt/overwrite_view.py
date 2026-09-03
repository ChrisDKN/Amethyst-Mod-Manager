"""Qt Overwrite manager tab - browse the Overwrite folder and clear it out.

Opened from the Overwrite row's context menu (modlist_menu `_manage_overwrite`)
as a modlist-panel-scoped tab. The Overwrite folder is where the game and its
tools drop files at runtime; this tab is the place to sort that pile into real
mods instead of opening a file manager.

The tree is the shared read-only `path_tree` (same look as Mod Files / Text
Files), extended with multi-selection. Selected files/folders can be:

  * moved into an existing mod (staging/<mod>/<same relative path>)
  * moved into a brand-new empty mod (created here, then inserted in the
    modlist just below Overwrite through a host callback)
  * deleted

Every action is destructive on disk, so each one goes through ConfirmOverlay
first. After any change `changed` fires with the mods whose staging folders were
written to, so the host can re-catalogue them and reload - the moved files now
belong to a real mod and their conflict standing changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QLabel, QLineEdit,
    QToolButton, QAbstractItemView, QMenu,
)

from Utils.filegraph.paths import EXCLUDE_NAMES, is_macos_junk
from gui_qt.confirm_overlay import ConfirmOverlay
from gui_qt.flow_layout import FlowLayout, enable_height_for_width
from gui_qt.icons import icon
from gui_qt.theme_qt import close_button
from gui_qt.list_picker_overlay import ListPickerOverlay
from gui_qt.modlist_model import NEW_MOD_VERSION
from gui_qt.path_tree import (
    Node, sort_tree, PathTreeModel, PathTreeDelegate,
)
from gui_qt.text_input_overlay import TextInputOverlay


def _is_hidden_name(name: str) -> bool:
    """True for manager-internal bookkeeping and OS junk.

    EXCLUDE_NAMES is the catalog's own ignore list (.mm_overwrite_log.txt,
    meta.ini, .mm_merge_inventory.xml), so hiding it here makes the tree show
    exactly what actually deploys. Moving or deleting the log would also break
    the boundary row's "Log" entry, which reads it from this folder.
    """
    return name in EXCLUDE_NAMES or is_macos_junk(name)


def _list_overwrite_tree(root: Path) -> tuple[list[str], list[str]]:
    """(file paths, empty-folder paths) under *root*, relative and posix.

    Empty folders are collected separately because the normal path-tree builder
    only creates a folder node on the way to a file. A leftover empty tree would
    otherwise be invisible, and it is exactly the kind of junk a user opens
    this tab to select and delete.

    A folder holding nothing but hidden entries counts as empty here, so it
    still shows up as a deletable leftover rather than vanishing.
    """
    files: list[str] = []
    empty_dirs: list[str] = []
    try:
        for p in root.rglob("*"):
            try:
                if p.is_symlink() or p.is_file():
                    if _is_hidden_name(p.name):
                        continue
                    files.append(p.relative_to(root).as_posix())
                elif p.is_dir():
                    if is_macos_junk(p.name):
                        continue
                    if not any(c for c in p.iterdir()
                               if not _is_hidden_name(c.name)):
                        empty_dirs.append(p.relative_to(root).as_posix())
            except OSError:
                continue
    except OSError:
        pass
    return files, empty_dirs


def _build_overwrite_tree(files: list[str], empty_dirs: list[str]) -> Node:
    """Build a tree without treating a literal POSIX backslash as a separator."""
    root = Node("", is_dir=True)
    folders: dict[str, Node] = {}

    def _folder(parts: list[str]) -> Node:
        parent = root
        path = ""
        for segment in parts:
            path = f"{path}/{segment}" if path else segment
            node = folders.get(path)
            if node is None:
                node = Node(segment, is_dir=True, parent=parent, payload=path)
                parent.children.append(node)
                folders[path] = node
            parent = node
        return parent

    for rel in files:
        parts = rel.split("/")
        parent = _folder(parts[:-1])
        parent.children.append(
            Node(parts[-1], is_dir=False, parent=parent, payload=rel))
    for rel in empty_dirs:
        _folder(rel.split("/"))
    sort_tree(root)
    return root


def _entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _safe_child(root: Path, rel: str) -> Path | None:
    """Return root/rel only when it is lexically and physically contained."""
    relative = Path(rel)
    if (not relative.parts or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)):
        return None
    candidate = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=False)
        resolved_parent = candidate.parent.resolve(strict=False)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _safe_mod_root(staging: Path, name: str) -> Path | None:
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1 or path.name != name:
        return None
    return _safe_child(staging, name)


# Footer buttons match the app's compact footer chrome (_text_button /
# _FOOT_BTN_H in app.py) - this view builds its own footer, so it repeats the
# two lines rather than reaching into the window for them.
_FOOT_BTN_H = 28


def _foot_button(text: str) -> QToolButton:
    """Flat compact footer button, styled like the Mod Files footer's."""
    b = QToolButton()
    b.setText(text)
    b.setToolButtonStyle(Qt.ToolButtonTextOnly)
    b.setObjectName("FooterButton")
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedHeight(_FOOT_BTN_H)
    return b


class OverwriteView(QWidget):
    """Self-contained Overwrite manager tab. Call configure() then reload()."""

    # Mods whose staging folder changed on disk ([] for a pure delete).
    changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game = None
        self.staging_dir: Path | None = None
        self._root_path: Path | None = None
        self.context_id = None
        self._search = ""
        self._search_exts: frozenset[str] = frozenset()
        self._files: list[str] = []
        self._empty_dirs: list[str] = []
        # Host-installed: () -> list[str] of existing mod names, priority order.
        self.mod_names_fn = None
        # Host-installed: every reserved mod/separator display and storage name.
        self.all_names_fn = None
        # Host-installed: persist a newly-created mod row; returns success.
        self.add_new_mod_fn = None
        # Host-installed: verifies this view still describes the active context.
        self.context_valid_fn = None
        # Host-installed: called by the header's Close button.
        self.on_close = lambda: None
        self._skip_next_show_reload = False
        self._build()

    # -- context ------------------------------------------------------------
    def configure(self, game, staging_dir, context_id=None, *, refresh=True):
        """Point the tab at a game's Overwrite folder."""
        old_context = (self.game, self.staging_dir, self._root_path,
                       self.context_id)
        self.game = game
        self.staging_dir = Path(staging_dir) if staging_dir else None
        self._root_path = None
        self.context_id = context_id
        if game is not None:
            try:
                self._root_path = Path(game.get_effective_overwrite_path())
            except Exception:
                self._root_path = None
        new_context = (self.game, self.staging_dir, self._root_path,
                       self.context_id)
        if refresh or new_context != old_context:
            self.reload()
            if not self.isVisible():
                self._skip_next_show_reload = True

    # -- construction -------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header: caption + Close (the tab's own chrome), matching the other
        # scoped views.
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 4, 8, 4)
        self._label = QLabel(self.tr("Overwrite"))
        self._label.setObjectName("HeaderCaption")
        bl.addWidget(self._label)
        bl.addStretch(1)
        close = close_button()
        close.clicked.connect(lambda: self.on_close())
        bl.addWidget(close)
        v.addWidget(bar)

        self._model = PathTreeModel(self)
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        # path_tree's delegate draws its own arrow + indent.
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.setItemDelegate(PathTreeDelegate(self._tree, self))
        v.addWidget(self._tree, 1)

        # Footer: a wrapping button row, then the search row underneath -
        # the same shape as the app's Mod Files / Data footers. FlowLayout so
        # longer translated labels reflow instead of clipping.
        foot = QWidget()
        foot.setObjectName("HeaderBar")
        fv = QVBoxLayout(foot)
        fv.setContentsMargins(8, 6, 8, 6)
        fv.setSpacing(6)

        btns = FlowLayout(spacing=4)
        self._btn_expand = _foot_button(self.tr("⊞ Expand all"))
        self._btn_expand.clicked.connect(self._on_expand_clicked)
        btns.addWidget(self._btn_expand)
        self._btn_mod = _foot_button(self.tr("Move to mod…"))
        self._btn_mod.clicked.connect(self._move_to_existing)
        btns.addWidget(self._btn_mod)
        self._btn_new = _foot_button(self.tr("Move to new mod…"))
        self._btn_new.clicked.connect(self._move_to_new)
        btns.addWidget(self._btn_new)
        self._btn_del = _foot_button(self.tr("Delete"))
        self._btn_del.clicked.connect(self._delete_selected)
        btns.addWidget(self._btn_del)
        fv.addLayout(btns)
        # FlowLayout only reports its wrapped height correctly once the host
        # reports height-for-width at its real width.
        enable_height_for_width(foot)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_icon = QLabel()
        search_icon.setPixmap(icon("search.png", 18).pixmap(18, 18))
        search_icon.setAlignment(Qt.AlignCenter)
        search_row.addWidget(search_icon)
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText(
            self.tr("Search files… (try !.dds)"))
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search)
        search_row.addWidget(self._search_box, 1)
        fv.addLayout(search_row)
        v.addWidget(foot)

        self._tree.selectionModel().selectionChanged.connect(
            lambda *_a: self._sync_buttons())
        self._sync_buttons()

    def showEvent(self, event):
        """Re-read the folder every time the tab is shown.

        Opening the tab already reloads (app._open_overwrite_tab calls
        configure), but a scoped tab stays alive when the user switches to
        another tab: coming back only calls setCurrentWidget, so without this
        the tree would still show the folder as it was, while the game or a
        tool has been writing into Overwrite the whole time. This is why the
        view has no Refresh button - reshowing it IS the refresh.
        """
        super().showEvent(event)
        if self._skip_next_show_reload:
            self._skip_next_show_reload = False
        else:
            self.reload()

    # -- population ---------------------------------------------------------
    def reload(self):
        """Re-read the Overwrite folder from disk, then rebuild the tree.

        The scan is cached in _files/_empty_dirs so typing in the search box
        re-filters without re-walking the folder (Overwrite can hold thousands
        of generated files - Grass Cache, Pandora output, PGPatcher textures).
        """
        self._files = []
        self._empty_dirs = []
        if self._root_path is not None and self._root_path.is_dir():
            self._files, self._empty_dirs = _list_overwrite_tree(self._root_path)
        self._repopulate()

    def _repopulate(self):
        """Rebuild the tree from the cached scan, honouring the search filter."""
        files, empty_dirs = self._apply_search()
        root = _build_overwrite_tree(files, empty_dirs)
        self._model.set_root(root)
        # A search is only useful with its hits on screen - a match five levels
        # down is invisible in a collapsed tree. set_root collapsed everything,
        # so the button label has to follow whichever state we land in.
        searching = bool(self._search or self._search_exts)
        if searching:
            self._tree.expandAll()
        self._set_expand_label(searching)
        self._update_label(len(files))
        self._sync_buttons()

    def _apply_search(self) -> tuple[list[str], list[str]]:
        """(files, empty_dirs) narrowed to the current query.

        A path matches when the needle is in any part of it, so typing a folder
        name keeps everything under that folder; an ``!.ext`` token filters
        files by extension. An empty folder can only match on its name - it has
        no extension to test - so it drops out entirely once a type token is
        given.
        """
        needle, exts = self._search, self._search_exts
        if not needle and not exts:
            return self._files, self._empty_dirs
        files = []
        for rel in self._files:
            if exts and not rel.lower().endswith(tuple(exts)):
                continue
            if needle and needle not in rel.lower():
                continue
            files.append(rel)
        empty_dirs = ([] if exts else
                      [d for d in self._empty_dirs
                       if not needle or needle in d.lower()])
        return files, empty_dirs

    def _update_label(self, shown: int):
        total = len(self._files)
        if self._root_path is None:
            self._label.setText(self.tr("Overwrite (no game selected)"))
        elif not self._files and not self._empty_dirs:
            self._label.setText(self.tr("Overwrite - empty"))
        elif shown != total:
            self._label.setText(
                self.tr("Overwrite - {0} of {1} file(s)").format(shown, total))
        else:
            self._label.setText(
                self.tr("Overwrite - {0} file(s)").format(total))

    def _on_search(self, text: str):
        """Debounced so fast typing rebuilds the tree once, not per keystroke."""
        from Utils.text.search import parse_file_query
        needle, self._search_exts = parse_file_query(text)
        self._search = needle
        t = getattr(self, "_search_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(150)
            t.timeout.connect(self._repopulate)
            self._search_timer = t
        t.start()

    def _toggle_expand_all(self) -> bool:
        """Toggle expand/collapse all; returns True if now expanded. Mirrors
        ModFilesView._toggle_expand_all - the first top-level row's state is
        what the label tracks."""
        first = self._model.index(0, 0) if self._model.rowCount() else None
        expanded = bool(first is not None and self._tree.isExpanded(first))
        if expanded:
            self._tree.collapseAll()
            return False
        self._tree.expandAll()
        return True

    def _on_expand_clicked(self):
        self._set_expand_label(self._toggle_expand_all())

    def _set_expand_label(self, expanded: bool):
        self._btn_expand.setText(self.tr("⊟ Collapse all") if expanded
                                 else self.tr("⊞ Expand all"))

    def _sync_buttons(self):
        has_sel = bool(self._selected_nodes())
        staging_ok = self.staging_dir is not None
        self._btn_mod.setEnabled(has_sel and staging_ok)
        self._btn_new.setEnabled(
            has_sel and staging_ok and callable(self.add_new_mod_fn))
        self._btn_del.setEnabled(has_sel)

    # -- selection ----------------------------------------------------------
    def _selected_nodes(self) -> list[Node]:
        nodes = []
        for idx in self._tree.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            node = self._model.node(idx)
            if node is not None and node.parent is not None:
                nodes.append(node)
        return nodes

    def _selected_rel_paths(self) -> list[str]:
        """Top-level relative paths of the selection, with descendants of an
        also-selected folder dropped - moving a folder already carries its
        children, and listing both would move the same file twice.

        While a search is active a selected folder is expanded to just its
        MATCHED files instead: the tree is showing a filtered subset, and
        moving the folder itself would silently take the hidden files with it.
        """
        paths = sorted({str(n.payload) for n in self._selected_nodes()
                        if n.payload})
        out: list[str] = []
        for path in paths:
            if any(path != q and path.startswith(q + "/") for q in paths):
                continue
            out.append(path)
        if not (self._search or self._search_exts):
            return out
        shown, shown_empty = self._apply_search()
        shown_set = set(shown)
        empty_set = set(shown_empty)
        expanded: list[str] = []
        for path in out:
            if path in shown_set:            # a matched file
                expanded.append(path)
                continue
            if path in empty_set:            # an exact matched empty folder
                expanded.append(path)
                continue
            prefix = path + "/"
            expanded.extend(f for f in shown if f.startswith(prefix))
            expanded.extend(d for d in shown_empty if d.startswith(prefix))
        return sorted(set(expanded))

    # -- context menu -------------------------------------------------------
    def _on_context_menu(self, pos):
        idx = self._tree.indexAt(pos)
        if idx.isValid() and not self._tree.selectionModel().isSelected(idx):
            self._tree.setCurrentIndex(idx)
        if not self._selected_nodes():
            return
        menu = QMenu(self._tree)
        staging_ok = self.staging_dir is not None
        if staging_ok:
            a = menu.addAction(self.tr("Move to mod…"))
            a.triggered.connect(self._move_to_existing)
            a = menu.addAction(self.tr("Move to new mod…"))
            a.triggered.connect(self._move_to_new)
            menu.addSeparator()
        a = menu.addAction(self.tr("Delete"))
        a.triggered.connect(self._delete_selected)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # -- actions ------------------------------------------------------------
    def _context_is_current(self) -> bool:
        if not callable(self.context_valid_fn):
            return True
        try:
            return bool(self.context_valid_fn(self.context_id))
        except Exception:
            return False

    def _require_current_context(self, expected=None) -> bool:
        if ((expected is None or expected == self.context_id)
                and self._context_is_current()):
            return True
        ConfirmOverlay.show_message(
            self, self.tr("Profile changed"),
            self.tr("The active game or profile changed. Reopen Overwrite "
                    "before modifying files."))
        return False

    def _move_to_existing(self):
        if not self._require_current_context():
            return
        expected_context = self.context_id
        rels = self._selected_rel_paths()
        if not rels or self.staging_dir is None:
            return
        names = list(self.mod_names_fn()) if callable(self.mod_names_fn) else []
        if not names:
            ConfirmOverlay.show_message(
                self, self.tr("Move to mod"),
                self.tr("There are no mods to move these files into."))
            return
        ListPickerOverlay.show_over(
            self, self.tr("Move {0} item(s) to which mod?").format(len(rels)),
            [(n, n) for n in names],
            lambda name: self._do_move(
                rels, name, expected_context=expected_context) if name else None,
            select_label=self.tr("Move"))

    def _move_to_new(self):
        if not self._require_current_context():
            return
        expected_context = self.context_id
        rels = self._selected_rel_paths()
        if (not rels or self.staging_dir is None
                or not callable(self.add_new_mod_fn)):
            return

        def _named(raw_name):
            raw_name = (raw_name or "").strip()
            if (not raw_name
                    or not self._require_current_context(expected_context)):
                return
            from Utils.mods.names import sanitize_mod_folder_name
            name = sanitize_mod_folder_name(raw_name)
            if name.casefold().endswith("_separator"):
                ConfirmOverlay.show_message(
                    self, self.tr("Invalid mod name"),
                    self.tr("Mod names cannot end with '_separator'."))
                return
            mod_dir = _safe_mod_root(self.staging_dir, name)
            if mod_dir is None:
                ConfirmOverlay.show_message(
                    self, self.tr("Invalid mod name"),
                    self.tr("That name cannot be used for a mod folder."))
                return
            names = (self.all_names_fn() if callable(self.all_names_fn)
                     else (self.mod_names_fn() if callable(self.mod_names_fn)
                           else []))
            existing = {str(value).casefold() for value in names}
            try:
                existing.update(
                    p.name.casefold() for p in self.staging_dir.iterdir())
            except OSError:
                pass
            if name.casefold() in existing or _entry_exists(mod_dir):
                ConfirmOverlay.show_message(
                    self, self.tr("Name conflict"),
                    self.tr("A mod named '{0}' already exists.").format(name))
                return
            try:
                from datetime import datetime
                mod_dir.mkdir(parents=True, exist_ok=False)
                installed = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                (mod_dir / "meta.ini").write_text(
                    f"[General]\ninstalled={installed}\n"
                    f"version={NEW_MOD_VERSION}\n", encoding="utf-8")
            except (OSError, ValueError) as exc:
                try:
                    if mod_dir.is_dir():
                        shutil.rmtree(mod_dir)
                except OSError:
                    pass
                ConfirmOverlay.show_message(
                    self, self.tr("Move to new mod"),
                    self.tr("Could not create the mod folder:\n{0}").format(exc))
                return
            try:
                added = bool(self.add_new_mod_fn(name))
            except Exception:
                added = False
            if not added:
                try:
                    shutil.rmtree(mod_dir)
                except OSError:
                    pass
                ConfirmOverlay.show_message(
                    self, self.tr("Move to new mod"),
                    self.tr("The mod could not be added to the modlist. "
                            "No files were moved."))
                return
            self._do_move(
                rels, name, confirm=False,
                expected_context=expected_context,
                refresh_even_if_unchanged=True)

        TextInputOverlay.show_over(
            self, self.tr("Move to new mod"), self.tr("Mod name:"), _named,
            ok_label=self.tr("Create"))

    def _move_relative(self, rel: str, dest_root: Path
                       ) -> tuple[bool, list[str]]:
        src = (_safe_child(self._root_path, rel)
               if self._root_path is not None else None)
        dst = _safe_child(dest_root, rel)
        if src is None or dst is None:
            return False, [f"{rel}: path is outside its managed folder"]
        if not _entry_exists(src):
            return False, [f"{rel}: source no longer exists"]

        src_is_dir = src.is_dir() and not src.is_symlink()
        dst_exists = _entry_exists(dst)
        dst_is_dir = dst_exists and dst.is_dir() and not dst.is_symlink()

        if src_is_dir and dst_is_dir:
            try:
                children = list(src.iterdir())
            except OSError as exc:
                return False, [f"{rel}: {exc}"]
            moved = False
            failed: list[str] = []
            for child in children:
                child_rel = f"{rel}/{child.name}"
                child_moved, child_failed = self._move_relative(
                    child_rel, dest_root)
                moved = moved or child_moved
                failed.extend(child_failed)
            try:
                src.rmdir()
                moved = True
            except OSError:
                pass
            return moved, failed

        if src_is_dir and dst_exists:
            return False, [
                f"{rel}: target is not a directory; not replaced"]

        if not src_is_dir and dst_is_dir:
            try:
                if any(dst.iterdir()):
                    return False, [
                        f"{rel}: target is a non-empty directory; not replaced"]
                dst.rmdir()
            except OSError as exc:
                return False, [f"{rel}: {exc}"]
        elif dst_exists:
            try:
                # Overwrite an exact file/symlink collision atomically. In
                # particular, leave the existing target intact if the move is
                # rejected (permissions, cross-device, etc.). Overwrite and
                # staging normally share a filesystem; the fallback below is
                # for custom layouts where they do not.
                src.replace(dst)
                return True, []
            except OSError as exc:
                import errno
                if exc.errno != errno.EXDEV:
                    return False, [f"{rel}: {exc}"]
                tmp = None
                try:
                    from uuid import uuid4
                    tmp = dst.parent / f".amethyst-move-{uuid4().hex}"
                    if src.is_symlink():
                        tmp.symlink_to(src.readlink(),
                                       target_is_directory=src.is_dir())
                    else:
                        shutil.copy2(src, tmp)
                    tmp.replace(dst)
                except (OSError, ValueError, shutil.Error) as copy_exc:
                    try:
                        if tmp is not None and _entry_exists(tmp):
                            tmp.unlink()
                    except OSError:
                        pass
                    return False, [f"{rel}: {copy_exc}"]
                try:
                    src.unlink()
                except OSError as unlink_exc:
                    return True, [
                        f"{rel}: copied, but source could not be removed: "
                        f"{unlink_exc}"]
                return True, []

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True, []
        except (OSError, ValueError, shutil.Error) as exc:
            return False, [f"{rel}: {exc}"]

    def _do_move(self, rels: list[str], mod_name: str, confirm: bool = True,
                 *, expected_context=None,
                 refresh_even_if_unchanged: bool = False):
        if expected_context is None:
            expected_context = self.context_id
        if (self._root_path is None or self.staging_dir is None
                or not self._require_current_context(expected_context)):
            return
        dest_root = _safe_mod_root(self.staging_dir, mod_name)
        if dest_root is None:
            ConfirmOverlay.show_message(
                self, self.tr("Move to mod"),
                self.tr("The selected mod has an unsafe folder name."))
            return

        def _go(ok=True):
            if not ok or not self._require_current_context(expected_context):
                return
            moved, failed = 0, []
            for rel in rels:
                changed, errors = self._move_relative(rel, dest_root)
                if changed:
                    moved += 1
                failed.extend(errors)
            self._prune_empty_dirs()
            self.reload()
            if moved or refresh_even_if_unchanged:
                self.changed.emit([mod_name])
            if failed:
                ConfirmOverlay.show_message(
                    self, self.tr("Move to mod"),
                    self.tr("Moved {0}, failed {1}:\n{2}").format(
                        moved, len(failed), "\n".join(failed[:10])))

        if confirm:
            ConfirmOverlay.show_over(
                self, self.tr("Move to mod"),
                self.tr("Move {0} item(s) from Overwrite into '{1}'?").format(
                    len(rels), mod_name),
                _go, confirm_label=self.tr("Move"), danger=False)
        else:
            _go(True)

    def _delete_selected(self):
        if not self._require_current_context():
            return
        expected_context = self.context_id
        rels = self._selected_rel_paths()
        if not rels or self._root_path is None:
            return

        def _go(ok=True):
            if not ok or not self._require_current_context(expected_context):
                return
            deleted, failed = 0, []
            for rel in rels:
                target = _safe_child(self._root_path, rel)
                if target is None:
                    failed.append(
                        f"{rel}: path is outside the Overwrite folder")
                    continue
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    deleted += 1
                except OSError as exc:
                    failed.append(f"{rel}: {exc}")
            self._prune_empty_dirs()
            self.reload()
            if deleted:
                self.changed.emit([])
            if failed:
                ConfirmOverlay.show_message(
                    self, self.tr("Delete"),
                    self.tr("Deleted {0}, failed {1}:\n{2}").format(
                        deleted, len(failed), "\n".join(failed[:10])))

        ConfirmOverlay.show_over(
            self, self.tr("Delete from Overwrite"),
            self.tr("Permanently delete {0} item(s) from the Overwrite "
                    "folder?\n\nThis cannot be undone.").format(len(rels)),
            _go, confirm_label=self.tr("Delete"), danger=True)

    def _prune_empty_dirs(self):
        """Drop folders a move/delete emptied out. The Overwrite root itself
        always stays - the deploy engine expects it to exist."""
        root = self._root_path
        if root is None or not root.is_dir():
            return
        try:
            dirs = sorted((p for p in root.rglob("*")
                           if p.is_dir() and not p.is_symlink()),
                          key=lambda p: len(p.parts), reverse=True)
        except OSError:
            return
        for d in dirs:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                continue
