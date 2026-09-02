"""Content-based detection of UE4SS blueprint ("logic") mods.

UE4SS's BPModLoaderMod enumerates the hardcoded ``Content/Paks/LogicMods``
directory and, for every ``.pak`` it finds, derives an asset path of
``/Game/Mods/<pak-filename-stem>/ModActor`` which it then loads from the asset
registry and spawns via ``World:SpawnActor``. It never mounts anything - the
engine's own recursive scan of ``Content/Paks`` does that.

The consequence is an asymmetry that makes this detection worth doing:

* A blueprint mod dropped in ``~mods`` still mounts, and its assets are still
  loadable, but nothing ever spawns the ModActor because the spawn list comes
  exclusively from the ``LogicMods`` listing. The mod silently does nothing -
  no crash, no error, not even a log line.
* An asset replacer dropped in ``LogicMods`` still works, because replacers
  win by colliding with vanilla asset paths and mounting alone is enough.

So a blueprint mod's assets live at a *new* path (``/Game/Mods/<Name>/``) that
no vanilla asset references, and that new path is exactly what we can look for
inside the archive: a file named ``ModActor`` directly under ``Mods/<Name>/``.

Detection is advisory and fail-safe. A negative or inconclusive result (an
encrypted index, an unparseable archive, an I/O error) means "no opinion", and
the caller keeps whatever destination its normal routing rules produce.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Iterable, Sequence

from Utils.unreal.archives import read_ue_archive_file_list

# Archive members that belong to one logical mod and must move together. An
# IoStore mod ships all three sharing a filename stem; splitting them (pak in
# LogicMods, bulk data in ~mods) would break the mod outright.
GROUP_EXTENSIONS = (".pak", ".utoc", ".ucas")
# .ucas holds only bulk data - no names - so it is never worth opening.
PROBE_EXTENSIONS = (".pak", ".utoc")

# ``Mods/<Name>/<something>ModActor<something>`` anywhere in the archive.
# read_ue_archive_file_list lowercases and forward-slashes its output, and
# folds the pak mount point in, so a blueprint mod packaged from the editor
# as "../../../Project/Content/Mods/MyMod/ModActor.uasset" arrives here as
# "project/content/mods/mymod/modactor.uasset".
_MODACTOR_RX = re.compile(r"(?:^|/)mods/([^/]+)/[^/]*modactor")

# Author-shipped signal that outranks detection entirely.
_LOGICMODS_SEGMENT = "logicmods"

# (abs_path, mtime_ns, size) → (has_marker, matched_mod_name)
# Only the verdict is cached, not the archive's full path list, which can run
# to thousands of entries per pak.
_marker_cache: dict[tuple[str, int, int], tuple[bool, str | None]] = {}
_marker_cache_lock = threading.Lock()


def _split_group_ext(name_lower: str) -> str | None:
    """Return the GROUP_EXTENSIONS suffix of *name_lower*, or None."""
    for ext in GROUP_EXTENSIONS:
        if name_lower.endswith(ext) and len(name_lower) > len(ext):
            return ext
    return None


def has_logicmods_segment(rel: str) -> bool:
    """True when any path segment is ``LogicMods`` (any casing).

    An explicit LogicMods folder is the mod author saying what this is, so it
    outranks anything we can infer from the contents.
    """
    return _LOGICMODS_SEGMENT in rel.replace("\\", "/").lower().split("/")


def stem_groups(staged_rels: Iterable[str]) -> dict[tuple[str, str], list[str]]:
    """Group archive paths by (parent dir, filename stem), both lowercased.

    Same-stem ``.pak``/``.utoc``/``.ucas`` files in one folder are one mod and
    are returned as one group so they can be routed as a unit. Non-archive
    paths are ignored.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for rel in staged_rels:
        norm = rel.replace("\\", "/")
        name_lower = norm.rsplit("/", 1)[-1].lower()
        ext = _split_group_ext(name_lower)
        if ext is None:
            continue
        parent = norm.rsplit("/", 1)[0].lower() if "/" in norm else ""
        stem = name_lower[: -len(ext)]
        groups.setdefault((parent, stem), []).append(rel)
    return groups


def _archive_marker(path: Path) -> tuple[bool, str | None]:
    """Return (contains_modactor, mod_name) for one archive, cached by stat."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (False, None)

    with _marker_cache_lock:
        hit = _marker_cache.get(key)
    if hit is not None:
        return hit

    result: tuple[bool, str | None] = (False, None)
    for entry in read_ue_archive_file_list(path):
        m = _MODACTOR_RX.search(entry)
        if m is not None:
            result = (True, m.group(1))
            break

    with _marker_cache_lock:
        _marker_cache[key] = result
    return result


def is_logic_mod(
    mod_dir: Path,
    members: Sequence[str],
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """True when this stem group is a UE4SS blueprint mod.

    Probes the group's ``.pak`` and ``.utoc`` members - an IoStore mod's ``.pak``
    is often a stub with no index while the ModActor lives in the ``.utoc``, so
    either member vouching for the group is enough.

    BPModLoaderMod builds its asset path from the *pak filename*, so a group
    whose ModActor folder name doesn't match the archive stem won't actually
    load in-game. It is still reported as a logic mod: it belongs in LogicMods
    where the user can see and rename it, rather than silently vanishing into
    ``~mods``. The mismatch is logged when a *log_fn* is supplied.
    """
    for rel in members:
        norm = rel.replace("\\", "/")
        name_lower = norm.rsplit("/", 1)[-1].lower()
        if not name_lower.endswith(PROBE_EXTENSIONS):
            continue
        found, mod_name = _archive_marker(mod_dir / norm)
        if not found:
            continue
        stem = name_lower.rsplit(".", 1)[0]
        if log_fn is not None and mod_name is not None and mod_name != stem:
            log_fn(
                f"LogicMods detect: '{norm}' contains Mods/{mod_name}/ModActor "
                f"but the archive stem is '{stem}' - UE4SS builds the asset "
                f"path from the filename, so this mod will not load until the "
                f"file is renamed to '{mod_name}{name_lower[len(stem):]}'."
            )
        return True
    return False


def logic_mod_entries(
    staging_root: Path | str | None,
    entries: Sequence[tuple[str, str]],
    log_fn: Callable[[str], None] | None = None,
) -> set[int]:
    """Indices into *entries* that belong to detected blueprint mods.

    *entries* is the ``[(staged_rel, mod_name), ...]`` list the UE5 routing
    pipeline works on, where ``staged_rel`` is the raw on-disk path relative to
    ``<staging_root>/<mod_name>``.

    Groups already living under a ``LogicMods`` folder are skipped - they need
    no help and the author's intent wins. Everything else that positively
    matches is returned; anything unreadable or unmatched is simply absent, so
    the caller's existing rules still decide.
    """
    if staging_root is None:
        return set()
    root = Path(staging_root)

    # Index positions per mod so a group's verdict maps back to entry indices.
    by_mod: dict[str, list[tuple[int, str]]] = {}
    for i, (staged_rel, mod_name) in enumerate(entries):
        by_mod.setdefault(mod_name, []).append((i, staged_rel))

    claimed: set[int] = set()
    for mod_name, items in by_mod.items():
        index_of = {rel: i for i, rel in items}
        groups = stem_groups(rel for _i, rel in items)
        if not groups:
            continue
        mod_dir = root / mod_name
        for members in groups.values():
            if any(has_logicmods_segment(m) for m in members):
                continue
            if not is_logic_mod(mod_dir, members, log_fn=log_fn):
                continue
            for m in members:
                idx = index_of.get(m)
                if idx is not None:
                    claimed.add(idx)
    return claimed
