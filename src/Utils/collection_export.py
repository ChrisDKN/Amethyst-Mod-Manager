"""
collection_export.py
Build a Vortex/Nexus-compatible collection archive (.7z) from export rows.

The manifest format mirrors real Vortex collections (see a published
collection.json): top-level ``info`` / ``mods`` / ``modRules`` /
``plugins`` / ``pluginRules`` / ``collectionConfig``. The archive layout is
``collection.json`` + ``bundled/<archive>`` for bundle-source mods. Our own
collection installer consumes exactly this shape, so exports round-trip.

Rows come from ``profile_export.load_rows`` plus the per-row UI flags
(source / direct_url / optional / fomod_export). All functions are
toolkit-free; the Qt view drives them from a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
import zlib
from pathlib import Path

from Utils.config_paths import get_download_cache_dir

try:
    import bsdiff4
except Exception:       # optional dependency - file-edit patches need it
    bsdiff4 = None

# Sub-phase labels for progress_cb(done, total, phase). done/total count mods
# during the build phase and bytes during packing.
PHASE_META = "meta"
PHASE_HASH = "hash"
PHASE_BUNDLE = "bundle"
PHASE_PATCH = "patch"
PHASE_PACK = "pack"

UPDATE_POLICIES = ("exact", "prefer", "latest")

# Nexus's own limits for a collection name (Vortex enforces the same client
# side: collections/constants.ts + StartPage.validateCollectionName).
COLLECTION_NAME_MIN = 3
COLLECTION_NAME_MAX = 36


def read_profile_manifest(profile_dir) -> dict:
    """The collection manifest an install saved to ``<profile>/collection.json``.

    Present whenever the profile was built by installing a collection (ours or
    one authored in Vortex), so a re-upload can recover the per-mod authoring
    settings instead of starting from defaults.
    """
    if not profile_dir:
        return {}
    path = Path(profile_dir) / "collection.json"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def seed_rows_from_manifest(rows, manifest: dict) -> int:
    """Apply a manifest's per-mod authoring settings onto export *rows*.

    Matches on (modId, fileId) first - the identity an install actually
    preserves - then falls back to the mod's name. Returns how many rows were
    seeded. Callers apply this BEFORE any saved export settings so the user's
    own saved choices still win.
    """
    mods = manifest.get("mods") or []
    if not mods:
        return 0

    by_ids: dict = {}
    by_name: dict = {}
    for mod in mods:
        source = mod.get("source") or {}
        mod_id = int(source.get("modId") or 0)
        file_id = int(source.get("fileId") or 0)
        if mod_id and file_id:
            by_ids[(mod_id, file_id)] = mod
        for key in (mod.get("name"), source.get("logicalFilename"),
                    source.get("fileExpression")):
            key = (key or "").strip().lower()
            if key:
                by_name.setdefault(key, mod)

    seeded = 0
    for row in rows:
        mod = by_ids.get((int(row.get("mod_id") or 0),
                          int(row.get("file_id") or 0)))
        if mod is None:
            mod = by_name.get((row.get("name") or "").strip().lower())
        if mod is None:
            continue

        source = mod.get("source") or {}
        try:
            row["phase"] = int(mod.get("phase") or 0)
        except (TypeError, ValueError):
            row["phase"] = 0
        policy = source.get("updatePolicy")
        if policy in UPDATE_POLICIES:
            row["update_policy"] = policy
        source_type = (source.get("type") or "").lower()
        if source.get("bundle") or source_type == "bundle":
            row["source"] = "bundle"
        elif source_type in ("direct", "browse", "manual"):
            row["source"] = source_type
            row["direct_url"] = source.get("url", "") or ""
            row["source_instructions"] = source.get("instructions", "") or ""
        # A mod that shipped patches was authored with "save edits" on.
        if mod.get("patches"):
            row["save_edits"] = True
        seeded += 1
    return seeded


def validate_collection_name(name: str) -> str:
    """Return "" when *name* is acceptable, else a human-readable reason."""
    text = (name or "").strip()
    if len(text) < COLLECTION_NAME_MIN:
        return (f"A collection name needs at least {COLLECTION_NAME_MIN} "
                "characters.")
    if len(text) > COLLECTION_NAME_MAX:
        return (f"A collection name can be at most {COLLECTION_NAME_MAX} "
                "characters.")
    if not all(ch.isalnum() or ch in " -" for ch in text):
        return ("A collection name may only contain letters, numbers, spaces "
                "and hyphens.")
    return ""

# Binary-patch limits, mirroring Vortex (collections/constants.ts): a patch may
# not exceed 20% of the source file's size once the ~130-byte bsdiff header is
# discounted, else the edit is better shipped as a bundled mod.
MAX_PATCH_RATIO = 0.2
PATCH_OVERHEAD = 130
# bsdiff holds both files plus suffix-array state in memory; skip anything
# where that would be unreasonable (BSAs, big textures).
MAX_PATCH_SOURCE_BYTES = 256 * 1024 * 1024

# Executables that never carry the game's version.
_VERSION_EXE_SKIP = re.compile(
    r"^(unins|setup|launcher|crash|dxsetup|vc_?redist|ue4prereq|dotnet)",
    re.IGNORECASE)


def detect_game_version(game) -> str:
    """Best-effort installed-game version from top-level exe version resources."""
    from Utils.exe_icon import extract_exe_version
    try:
        root = game.get_game_path() if game else None
        if not root or not Path(root).is_dir():
            return ""
        exes = [p for p in Path(root).glob("*.exe")
                if not _VERSION_EXE_SKIP.match(p.name)]
        # The main binary is almost always the largest top-level exe.
        exes.sort(key=lambda p: p.stat().st_size, reverse=True)
        for exe in exes[:5]:
            version = extract_exe_version(exe)
            if version and version != "0.0.0.0":
                return version
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _short_tag(name: str) -> str:
    """Deterministic 10-char tag for a mod (stable across re-exports)."""
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:10]


def _read_row_meta(staging_root, name: str):
    """Return the NexusModMeta for a staged mod, or None."""
    if not staging_root:
        return None
    meta_path = Path(staging_root) / name / "meta.ini"
    if not meta_path.is_file():
        return None
    try:
        from Nexus.nexus_meta import read_meta
        return read_meta(meta_path)
    except Exception:
        return None


# {(dir, mtime_ns): {file_id: archive path}} — the download cache's .fileid
# sidecars, memoized so a whole export scans each folder at most once.
_fileid_index_cache: dict = {}


def _fileid_archive_index(directory: Path) -> dict:
    """Map Nexus file id → archive path using the cache's ``.fileid`` sidecars."""
    try:
        key = (str(directory), directory.stat().st_mtime_ns)
    except OSError:
        return {}
    cached = _fileid_index_cache.get(key)
    if cached is not None:
        return cached
    index: dict = {}
    try:
        for sidecar in directory.glob("*.fileid"):
            try:
                file_id = int(sidecar.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            archive = sidecar.with_suffix("")      # drop the .fileid suffix
            if file_id and archive.is_file():
                index[file_id] = archive
    except OSError:
        return {}
    _fileid_index_cache[key] = index
    return index


def _cached_archive(meta, game_name: str = "") -> "Path | None":
    """The mod's original archive in the download cache, or None.

    Downloads land in a per-game subfolder (download_cache/<game>/), with the
    cache root kept as a fallback for legacy layouts. The filename recorded in
    meta.ini can be stale — Nexus changed its download naming, or the file was
    re-downloaded under a new name — so fall back to matching the mod's Nexus
    file id against the ``.fileid`` sidecars written next to each download.
    """
    if meta is None:
        return None
    root = get_download_cache_dir()
    dirs = ([root / game_name] if game_name else []) + [root]

    fname = (getattr(meta, "installation_file", "") or "").strip()
    if fname:
        for directory in dirs:
            path = directory / fname
            if path.is_file():
                return path

    file_id = int(getattr(meta, "file_id", 0) or 0)
    if file_id:
        for directory in dirs:
            hit = _fileid_archive_index(directory).get(file_id)
            if hit is not None:
                return hit
    return None


def _archive_md5(archive: Path) -> str:
    """MD5 of an archive - served from the download-cache md5 cache when possible."""
    try:
        from Nexus.nexus_download import _md5_cache_get, _md5_cache_put
    except Exception:
        _md5_cache_get = _md5_cache_put = None
    if _md5_cache_get is not None:
        cached = _md5_cache_get(archive)
        if cached:
            return cached
    md5 = hashlib.md5()
    try:
        with open(archive, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                md5.update(chunk)
    except OSError:
        return ""
    digest = md5.hexdigest()
    if _md5_cache_put is not None:
        try:
            _md5_cache_put(archive, digest)
        except Exception:
            pass
    return digest


# ---------------------------------------------------------------------------
# FOMOD choices - Amethyst sidecar -> Vortex "options" shape
# ---------------------------------------------------------------------------

_FOMOD_XML_RE = re.compile(r"(^|/)fomod/moduleconfig\.xml$", re.IGNORECASE)


def _module_config_bytes(archive: Path) -> "bytes | None":
    """Read fomod/ModuleConfig.xml straight out of a .zip/.7z/.rar, or None."""
    suffix = archive.suffix.lower()
    xml_bytes = None
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if _FOMOD_XML_RE.search(name):
                        xml_bytes = zf.read(name)
                        break
        elif suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(str(archive), mode="r") as arc:
                target = next((n for n in arc.getnames()
                               if _FOMOD_XML_RE.search(n)), None)
                if target:
                    arc.reset()
                    with tempfile.TemporaryDirectory() as td:
                        arc.extract(path=td, targets=[target])
                        fp = Path(td) / target
                        if fp.is_file():
                            xml_bytes = fp.read_bytes()
        else:  # .rar and friends - only if rarfile is importable
            try:
                import rarfile
                with rarfile.RarFile(str(archive)) as rf:
                    for name in rf.namelist():
                        if _FOMOD_XML_RE.search(name.replace("\\", "/")):
                            xml_bytes = rf.read(name)
                            break
            except Exception:
                return None
    except Exception:
        return None
    return xml_bytes or None


def _extract_module_config_to(archive: Path, dest: Path) -> bool:
    """Save the archive's ModuleConfig.xml to *dest*; False when unavailable."""
    xml_bytes = _module_config_bytes(archive)
    if not xml_bytes:
        return False
    try:
        dest.write_bytes(xml_bytes)
        return True
    except OSError:
        return False


def _module_config_from_archive(archive: Path):
    """Parse fomod/ModuleConfig.xml straight out of an archive, or None."""
    from Utils.fomod_parser import parse_module_config
    xml_bytes = _module_config_bytes(archive)
    if not xml_bytes:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            tmp.write(xml_bytes)
            tmp_path = tmp.name
        try:
            return parse_module_config(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except Exception:
        return None


def _fomod_options(selections: dict, config) -> "list | None":
    """Convert saved selections {step: {group: [plugins]}} to Vortex options."""
    steps = getattr(config, "steps", None) or []
    if not steps:
        return None

    def _step_for(key: str):
        try:
            idx = int(key)
            if 0 <= idx < len(steps):
                return steps[idx]
        except (TypeError, ValueError):
            pass
        return next((s for s in steps if s.name == key), None)

    def _sort_key(key: str):
        try:
            return (0, int(key))
        except (TypeError, ValueError):
            return (1, key)

    options: list = []
    for key in sorted(selections, key=_sort_key):
        step = _step_for(key)
        if step is None:
            return None
        groups_out: list = []
        for group_name, plugin_names in (selections[key] or {}).items():
            group = next((g for g in step.groups if g.name == group_name), None)
            if group is None:
                return None
            ordered = [p.name for p in group.plugins]
            choices = []
            for pname in plugin_names:
                try:
                    idx = ordered.index(pname)
                except ValueError:
                    return None
                choices.append({"name": pname, "idx": idx})
            groups_out.append({"name": group_name, "choices": choices})
        options.append({"name": step.name, "groups": groups_out})
    return options


def _module_config_for_mod(mod_name: str, profile_dir, archive):
    """The mod's parsed FOMOD config, preferring the profile's own copy.

    Installs mirror ``ModuleConfig.xml`` to ``<profile>/fomod/<mod>.xml``, so
    the usual path needs no archive at all. Mods installed before that existed
    fall back to reading the archive — and the copy is then back-filled so the
    next export works even if the archive is later deleted or renamed.
    """
    from Utils.fomod_parser import parse_module_config

    saved = (Path(profile_dir) / "fomod" / f"{mod_name}.xml"
             if profile_dir else None)
    if saved is not None and saved.is_file():
        try:
            return parse_module_config(str(saved))
        except Exception:
            pass          # corrupt copy — fall through to the archive

    if archive is None:
        return None
    config = _module_config_from_archive(archive)
    if config is not None and saved is not None:
        try:
            saved.parent.mkdir(parents=True, exist_ok=True)
            _extract_module_config_to(archive, saved)
        except Exception:
            pass          # best-effort cache; the export still succeeds
    return config


def _load_fomod_selections(row: dict, game_name: str, profile_dir) -> "dict | None":
    """Read the profile-local (preferred) or global FOMOD sidecar for a row."""
    from Utils.config_paths import get_fomod_selections_path
    candidates = []
    if profile_dir:
        candidates.append(Path(profile_dir) / "fomod" / f"{row['name']}.json")
    if game_name:
        candidates.append(get_fomod_selections_path(game_name, row["name"]))
    for path in candidates:
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Mod rules from filemap conflicts
# ---------------------------------------------------------------------------

_FILENAME_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _bundled_folder_name(mod_name: str, version: str = "") -> str:
    """Vortex's bundled-folder name: ``Bundled - <mod name> (v<version>)``.

    Matches ``modToCollection.ts``'s
    ``Bundled - ${sanitizeFilename(renderModName(mod, {version: true}))}`` so
    the layout is identical to a Vortex-authored collection.
    """
    label = (mod_name or "mod").strip()
    version = (version or "").strip()
    if version:
        label = f"{label} (v{version})"
    safe = _FILENAME_ILLEGAL_RE.sub("", f"Bundled - {label}")
    # Trailing dots/spaces are illegal on Windows, where these get extracted.
    return safe.rstrip(". ") or "Bundled - mod"


def _crc32_hex(data: bytes) -> str:
    """8-char uppercase CRC32 - the format collection.json patch maps use."""
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"


def _extract_archive_to(archive: Path, dest: Path) -> bool:
    """Extract a mod archive into *dest*; False when the format is unsupported."""
    suffix = archive.suffix.lower()
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        elif suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(str(archive), mode="r") as arc:
                arc.extractall(path=str(dest))
        elif suffix == ".rar":
            import rarfile
            with rarfile.RarFile(str(archive)) as rf:
                rf.extractall(str(dest))
        else:
            return False
    except Exception:
        return False
    return True


def _scan_mod_patches(mod_dir: Path, archive: Path, out_dir: Path,
                      mod_label: str, warnings: list) -> dict:
    """Diff a staged mod against its original archive; write ``<rel>.diff``.

    Returns ``{relative_path: source_crc}`` for every file whose staged content
    differs from the archive's - the manifest's ``patches`` map. Installers
    re-download the untouched mod and apply these patches, so only files that
    exist in the archive can be covered.
    """
    if bsdiff4 is None:
        warnings.append(
            f"'{mod_label}': file edits skipped - bsdiff4 is not installed.")
        return {}

    import shutil

    tmp_root = Path(tempfile.mkdtemp(prefix="amethyst_colpatch_"))
    try:
        if not _extract_archive_to(archive, tmp_root):
            warnings.append(
                f"'{mod_label}': file edits skipped - could not read the "
                f"original archive ({archive.name}).")
            return {}

        # Index the archive by normalised relative path, plus a basename map so
        # installer-relocated files (FOMOD option folders, flattening) still
        # resolve.
        by_path: dict = {}
        by_base: dict = {}
        for src in tmp_root.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(tmp_root).as_posix().lower()
            by_path[rel] = src
            by_base.setdefault(src.name.lower(), []).append(src)

        patches: dict = {}
        for staged in sorted(mod_dir.rglob("*")):
            if not staged.is_file():
                continue
            rel = staged.relative_to(mod_dir).as_posix()
            rel_lower = rel.lower()
            # Installer bookkeeping, not mod content.
            if rel_lower == "meta.ini" or rel_lower.startswith("fomod/"):
                continue

            src = by_path.get(rel_lower)
            if src is None:
                # The installer may have stripped a leading folder.
                tail = [p for k, p in by_path.items()
                        if k.endswith("/" + rel_lower)]
                if len(tail) == 1:
                    src = tail[0]
            if src is None:
                candidates = by_base.get(Path(rel_lower).name, [])
                if len(candidates) == 1:
                    src = candidates[0]
            if src is None:
                continue        # added by the user; can't be expressed as a patch

            try:
                src_bytes = src.read_bytes()
                dst_bytes = staged.read_bytes()
            except OSError:
                continue
            if src_bytes == dst_bytes:
                continue

            if len(src_bytes) > MAX_PATCH_SOURCE_BYTES:
                warnings.append(
                    f"'{mod_label}': '{rel}' was edited but is too large to "
                    "patch - users will get the unmodified file.")
                continue

            try:
                diff = bsdiff4.diff(src_bytes, dst_bytes)
            except Exception as exc:
                warnings.append(f"'{mod_label}': could not diff '{rel}' ({exc}).")
                continue

            # An oversized patch means the file was effectively replaced -
            # bundling the mod is the right answer, not shipping a huge diff.
            if len(diff) - PATCH_OVERHEAD > len(src_bytes) * MAX_PATCH_RATIO:
                warnings.append(
                    f"'{mod_label}': edits to '{rel}' are too extensive to ship "
                    "as a patch - users will get the unmodified file.")
                continue

            diff_path = out_dir / (rel + ".diff")
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_bytes(diff)
            patches[rel] = _crc32_hex(src_bytes)

        return patches
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _ini_tweak_jobs(game_name: str, profile_dir,
                    warnings: list) -> "list[tuple[Path, str]]":
    """Archive jobs turning ``<profile>/ini files/*.ini`` into ``INI Tweaks/``.

    Each profile INI whose name is a valid game INI target becomes
    ``INI Tweaks/Profile Settings [<Target>].ini`` - the exact layout both our
    installer (collection_ini_tweaks) and Vortex merge from.
    """
    from Utils.collection_ini_tweaks import GAME_INI_TARGETS

    ini_dir = Path(profile_dir) / "ini files" if profile_dir else None
    if not ini_dir or not ini_dir.is_dir():
        return []
    ini_files = sorted(p for p in ini_dir.glob("*.ini") if p.is_file())
    if not ini_files:
        return []

    targets = GAME_INI_TARGETS.get(game_name)
    if not targets:
        warnings.append(
            "Profile INI files were not included - this game has no known "
            "INI tweak targets.")
        return []
    by_lower = {t.lower(): t for t in targets}

    jobs: list = []
    for src in ini_files:
        target = by_lower.get(src.name.lower())
        if target is None:
            warnings.append(
                f"'{src.name}' skipped - not a recognised game INI for this "
                "game.")
            continue
        stem = target[:-len(".ini")]
        jobs.append((src, f"INI Tweaks/Profile Settings [{stem}].ini"))
    return jobs


def _conflict_mod_rules(ordered_names: list, refs: dict, staging_root,
                        index_path=None) -> list:
    """Emit ``after`` rules so loose-file conflicts resolve exactly as they do
    in the modlist panel.

    *ordered_names* is LOWEST-priority-first — the order the export rows carry,
    which mirrors the modlist read bottom-up (the panel's top entry, the one
    that wins conflicts, comes last). For every file more than one mod
    provides, the winner is the one furthest down this list; it must load
    AFTER the mods it overwrites.

    Rule direction matches Vortex and our own installer's topo sort
    (``collection_reset._topo_sort_collection``): ``source after reference``
    means *source* wins. Only consecutive pairs in each conflict chain are
    emitted — the transitive order follows — which keeps the rule count near
    the number of real conflicts rather than their square.
    """
    if index_path is None:
        if not staging_root:
            return []
        index_path = Path(staging_root).parent / "modindex.bin"
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(Path(index_path)) or {}
    except Exception:
        index = {}
    if not index:
        return []

    # Higher number = higher modlist priority = wins the file.
    priority = {name: i for i, name in enumerate(ordered_names)}
    providers: dict = {}   # (namespace, rel_key) -> [mod names]
    for name in ordered_names:
        entry = index.get(name)
        if not entry:
            continue
        normal_files = entry[0] or {}
        root_files = (entry[1] if len(entry) > 1 else None) or {}
        # Root-deployed files live in a different target tree, so a shared
        # relative path there is not a conflict with a normal-deploy file.
        for rel_key in normal_files:
            providers.setdefault(("data", rel_key), []).append(name)
        for rel_key in root_files:
            providers.setdefault(("root", rel_key), []).append(name)

    rules: list = []
    seen: set = set()
    for mods in providers.values():
        if len(mods) < 2:
            continue
        # Winner (highest priority) first, then each mod it overwrites.
        mods.sort(key=lambda n: priority[n], reverse=True)
        for winner, loser in zip(mods, mods[1:]):
            pair = (winner, loser)
            if pair in seen or winner not in refs or loser not in refs:
                continue
            seen.add(pair)
            rules.append({
                "type": "after",
                "source": refs[winner],
                "reference": refs[loser],
            })
    return rules


# ---------------------------------------------------------------------------
# Plugins + LOOT rules (Bethesda-family profiles)
# ---------------------------------------------------------------------------

def _load_order_block(entries: list) -> "list | None":
    """The explicit mod load order, for games whose ORDER IS the mod order.

    Bethesda-style games express load order through plugins (plugins.txt +
    LOOT rules), so their manifests carry ``plugins``/``pluginRules`` instead.
    Games without plugin files — Baldur's Gate 3 and its .pak order being the
    motivating case — have nothing but the mod order itself, and conflict-
    derived ``modRules`` barely cover it because each mod ships a uniquely
    named archive that never collides on a loose path.

    ``loadOrder[0]`` is the FIRST mod to load, i.e. the lowest priority /
    bottom of the modlist — the same order the export rows are already in, and
    the contract ``collection_reset._resolve_collection_priorities`` consumes
    (it prefers this block over topo-sorting whenever entries carry a fileId).
    Entries also carry Vortex's file-based-load-order fields (id / name /
    enabled) so its BG3 extension can restore the order too.

    *entries* — ``(display_name, file_id)`` in export-row order.
    """
    out: list = []
    for name, file_id in entries:
        entry: dict = {"id": name, "name": name, "enabled": True}
        if file_id:
            entry["fileId"] = int(file_id)
        out.append(entry)
    # Without at least one fileId the block carries no information our
    # installer can act on, so leave it out entirely.
    if not any(e.get("fileId") for e in out):
        return None
    return out


def _plugins_block(profile_dir) -> "list | None":
    """The manifest plugins list from the profile's plugins.txt, or None."""
    path = Path(profile_dir) / "plugins.txt" if profile_dir else None
    if not path or not path.is_file():
        return None
    try:
        from Utils.plugins import read_plugins
        entries = read_plugins(path)
    except Exception:
        return None
    return [{"name": e.name.lower(), "enabled": bool(e.enabled)}
            for e in entries if getattr(e, "name", "")]


def _plugin_rules_block(profile_dir, known_plugins=None) -> "dict | None":
    """The profile's LOOT ``userlist.yaml`` as the manifest's ``pluginRules``.

    This is the exact inverse of what installing a collection does: our
    installer (``collection_reset._apply_collection_groups``) merges
    ``pluginRules`` back into the profile's userlist.yaml, honouring a plugin's
    ``group``, ``after`` and ``before``, plus the group definitions and their
    own ``after`` ordering. Everything those two sides share round-trips.

    *known_plugins* — lowercase names of the plugins the collection actually
    ships. Rules owned by anything else are dropped: they would reference
    plugins the installer never sees (Vortex's gamebryo generator filters the
    same way). Group definitions are always kept, since a rule may place a
    shipped plugin into a group defined by an absent one.
    """
    path = Path(profile_dir) / "userlist.yaml" if profile_dir else None
    if not path or not path.is_file():
        return None
    try:
        from Utils.userlist import parse_userlist
        data = parse_userlist(path)
    except Exception:
        return None

    def _names(items) -> list:
        out = []
        for it in items or []:
            name = it.get("name") if isinstance(it, dict) else it
            if name:
                out.append(str(name).lower())
        return out

    plugins_out = []
    for entry in data.get("plugins", []):
        name = (entry.get("name") or "").lower()
        if not name:
            continue
        if known_plugins is not None and name not in known_plugins:
            continue
        rule: dict = {"name": name}
        for field in ("after", "before"):
            values = _names(entry.get(field))
            if values:
                rule[field] = values
        if entry.get("group"):
            rule["group"] = entry["group"]
        if len(rule) > 1:
            plugins_out.append(rule)

    groups_out = []
    for grp in data.get("groups", []):
        gname = grp.get("name") if isinstance(grp, dict) else None
        if not gname:
            continue
        gout: dict = {"name": gname}
        after = [g for g in (grp.get("after") or []) if g]
        if after:
            gout["after"] = after
        groups_out.append(gout)

    if not plugins_out and not groups_out:
        return None
    rules: dict = {"plugins": plugins_out}
    if groups_out:
        rules["groups"] = groups_out
    return rules


# ---------------------------------------------------------------------------
# Manifest build
# ---------------------------------------------------------------------------

def build_collection_manifest(rows, game, info: dict, *,
                              progress_cb=None) -> "tuple[dict, list, list]":
    """Build (manifest, bundle_jobs, warnings) from export rows for *game*.

    *info* supplies the manifest info block: name, author, authorUrl,
    description, installInstructions, gameVersions (list). bundle_jobs is a
    list of (src_path, arcname) for pack_collection; temp zips created for
    bundle mods without a cached archive land in the download cache and are
    removed by pack_collection after writing.
    """
    progress_cb = progress_cb or (lambda *_a: None)
    staging_root = game.get_effective_mod_staging_path() if game else None
    profile_dir = getattr(game, "_active_profile_dir", None) if game else None
    game_name = getattr(game, "name", "") or ""
    game_domain = getattr(game, "nexus_game_domain", "") or ""

    warnings: list = []
    mods: list = []
    bundle_jobs: list = []
    refs: dict = {}            # mod name -> modRules reference dict
    ordered_names: list = []   # exported, enabled mod names (priority order)
    load_order_entries: list = []   # (manifest name, file_id), same order

    active = [r for r in rows if r.get("source") != "ignore"]
    skipped_disabled = [r["name"] for r in active if r.get("enabled") is False]
    if skipped_disabled:
        warnings.append(
            f"{len(skipped_disabled)} disabled mod(s) were left out of the "
            "collection (Nexus collections have no disabled state).")
        active = [r for r in active if r.get("enabled") is not False]

    tmp_dir = None
    patch_root = None
    total = len(active)
    for i, row in enumerate(active):
        name = row["name"]
        progress_cb(i, total, PHASE_META)
        meta = _read_row_meta(staging_root, name)
        archive = _cached_archive(meta, game_name)
        version = row.get("version") or (getattr(meta, "version", "") or "")
        logical = (getattr(meta, "nexus_file_name", "") or "").strip() or name
        author = (getattr(meta, "author", "") or "").strip()
        tag = _short_tag(name)
        row_source = row.get("source", "nexus")
        # Mods downloaded from another Nexus game domain (cross-game files,
        # "site" tools) keep their own domain in meta.
        mod_domain = (getattr(meta, "game_domain", "") or "").strip() or game_domain

        # Bundling ships the mod's files inside the collection. For anything
        # with a Nexus page that is redistributing someone else's work, so it
        # is downgraded to a normal Nexus download regardless of what the row
        # (or a stale saved setting / seeded manifest) asked for. Local changes
        # still travel as binary patches via "save_edits".
        if row_source == "bundle" and row.get("mod_id") and row.get("file_id"):
            warnings.append(
                f"'{name}': can't be bundled because it is available on Nexus "
                "- exported as a normal Nexus download instead. Enable Edits "
                "to include your local changes.")
            row_source = "nexus"

        policy = row.get("update_policy") or "exact"
        if policy not in UPDATE_POLICIES or row_source == "bundle":
            policy = "exact"

        md5 = ""
        file_size = row.get("size_bytes") or (getattr(meta, "file_size", 0) or 0)

        if row_source == "bundle":
            progress_cb(i, total, PHASE_BUNDLE)
            # Bundled mods ship as a FOLDER of loose files under bundled/,
            # exactly as Vortex writes them (modToCollection.ts copies the
            # staging folder's contents to bundled/<generatedName>/). Installers
            # — ours included — look for that directory by fileExpression and
            # skip the mod entirely if it isn't one, so a packed archive here
            # would silently fail to install.
            mod_dir = Path(staging_root) / name if staging_root else None
            if not mod_dir or not mod_dir.is_dir():
                warnings.append(f"'{name}': staged files not found - skipped.")
                continue
            bundle_folder = _bundled_folder_name(name, version)
            file_size = 0
            member_count = 0
            for fp in sorted(mod_dir.rglob("*")):
                # meta.ini is our own bookkeeping; the installer writes a fresh
                # one for the bundled mod on the way in.
                if not fp.is_file() or fp.name == "meta.ini":
                    continue
                rel = fp.relative_to(mod_dir).as_posix()
                bundle_jobs.append((fp, f"bundled/{bundle_folder}/{rel}"))
                try:
                    file_size += fp.stat().st_size
                except OSError:
                    pass
                member_count += 1
            if not member_count:
                warnings.append(f"'{name}': no files to bundle - skipped.")
                continue
            # No md5 for bundled files: installers re-compress them, so a hash
            # taken here would never match (Vortex omits it for the same
            # reason - modToCollection.ts).
            md5 = ""
            source: dict = {
                "type": "bundle",
                "fileSize": file_size,
                "logicalFilename": f"{bundle_folder}.7z",
                "updatePolicy": "exact",
                "tag": tag,
                "fileExpression": bundle_folder,
            }
            file_expr = bundle_folder
        else:
            if archive is not None:
                progress_cb(i, total, PHASE_HASH)
                md5 = _archive_md5(archive)
                if not file_size:
                    file_size = archive.stat().st_size
            if row_source == "direct":
                source = {"type": "direct", "url": row.get("direct_url", "")}
            elif row_source in ("browse", "manual"):
                # No automatic download: the installer sends the user to a web
                # page (browse) or shows instructions (manual).
                source = {"type": row_source}
                if row.get("direct_url"):
                    source["url"] = row["direct_url"]
                if row.get("source_instructions"):
                    source["instructions"] = row["source_instructions"]
            else:
                source = {
                    "type": "nexus",
                    "modId": row["mod_id"],
                    "fileId": row["file_id"],
                    "logicalFilename": logical,
                }
            if md5:
                source["md5"] = md5
            if file_size:
                source["fileSize"] = file_size
            if row_source not in ("browse", "manual"):
                source["updatePolicy"] = policy
            source["tag"] = tag
            file_expr = Path(archive.name).stem if archive is not None else logical

        mod_entry: dict = {
            "name": logical if row_source == "nexus" else name,
            "version": version or "",
            "optional": bool(row.get("optional")),
            "domainName": mod_domain,
            "source": source,
            "details": {
                "category": row.get("category_name") or "",
                "type": "dinput" if row.get("root_folder") else "",
            },
            "phase": int(row.get("phase") or 0),
        }
        if author:
            mod_entry["author"] = author

        # FOMOD choices - only exportable when the original archive still has
        # the installer config we can map names/indices from. BAIN has no
        # Vortex equivalent.
        if row.get("has_fomod") and row.get("fomod_export", True):
            if row.get("has_bain"):
                warnings.append(
                    f"'{name}': BAIN installer choices can't be represented "
                    "in a Nexus collection - exported without choices.")
            else:
                selections = _load_fomod_selections(row, game_name, profile_dir)
                options = None
                if selections:
                    config = _module_config_for_mod(name, profile_dir, archive)
                    if config is not None:
                        options = _fomod_options(selections, config)
                if options:
                    mod_entry["choices"] = {"options": options}
                elif selections:
                    warnings.append(
                        f"'{name}': FOMOD choices could not be recovered - no "
                        "installer config saved in the profile and the original "
                        "archive is missing or changed. Reinstalling the mod "
                        "records it; installers will run interactively.")

        # Local file edits → binary patches applied over the pristine download.
        # Bundled mods already ship the edited files themselves.
        if row.get("save_edits") and row_source != "bundle":
            progress_cb(i, total, PHASE_PATCH)
            mod_dir = Path(staging_root) / name if staging_root else None
            if archive is None:
                warnings.append(
                    f"'{name}': file edits skipped - the original archive is "
                    "not in the download cache.")
            elif mod_dir and mod_dir.is_dir():
                if patch_root is None:
                    patch_root = Path(tempfile.mkdtemp(
                        prefix="amethyst_colexport_",
                        dir=str(get_download_cache_dir())))
                mod_patch_dir = patch_root / mod_entry["name"]
                found = _scan_mod_patches(mod_dir, archive, mod_patch_dir,
                                          name, warnings)
                if found:
                    mod_entry["patches"] = found
                    for diff in sorted(mod_patch_dir.rglob("*.diff")):
                        rel = diff.relative_to(mod_patch_dir).as_posix()
                        bundle_jobs.append(
                            (diff, f"patches/{mod_entry['name']}/{rel}"))

        # Rule references: non-exact policies must not pin the current file
        # (the installed file may legitimately be newer).
        if policy != "exact":
            refs[name] = {"versionMatch": "*", "logicalFileName": logical}
        else:
            refs[name] = {
                "fileExpression": file_expr,
                "versionMatch": version or "*",
                "logicalFileName": logical,
                **({"fileMD5": md5} if md5 else {}),
            }
        ordered_names.append(name)
        load_order_entries.append((mod_entry["name"], row.get("file_id") or 0))
        mods.append(mod_entry)

    manifest: dict = {
        "info": {
            "author": info.get("author", ""),
            "authorUrl": info.get("authorUrl", ""),
            "name": info.get("name", ""),
            "description": info.get("description", ""),
            "installInstructions": info.get("installInstructions", ""),
            "domainName": game_domain,
            "gameVersions": list(info.get("gameVersions") or []),
        },
        "mods": mods,
        "modRules": (_conflict_mod_rules(ordered_names, refs, staging_root)
                     if staging_root else []),
        "collectionConfig": {
            "recommendNewProfile": bool(info.get("recommendNewProfile", True)),
            "excludePluginRules": bool(info.get("excludePluginRules", False)),
        },
    }

    plugins = _plugins_block(profile_dir)
    if plugins is not None:
        manifest["plugins"] = plugins
        plugin_rules = _plugin_rules_block(
            profile_dir, {p["name"] for p in plugins})
        if plugin_rules is not None:
            manifest["pluginRules"] = plugin_rules
    elif not (getattr(game, "plugin_extensions", None) or []):
        # No plugin files at all (BG3's .pak order, and games like it): the mod
        # order IS the load order, so ship it explicitly rather than hoping
        # file-conflict rules happen to describe it.
        load_order = _load_order_block(load_order_entries)
        if load_order is not None:
            manifest["loadOrder"] = load_order

    if info.get("includeIniTweaks"):
        bundle_jobs.extend(_ini_tweak_jobs(game_name, profile_dir, warnings))

    return manifest, bundle_jobs, warnings


# ---------------------------------------------------------------------------
# Archive write
# ---------------------------------------------------------------------------

def pack_collection(out_path, manifest: dict, bundle_jobs, *,
                    progress_cb=None) -> Path:
    """Write collection.json + bundled files into a .7z; returns the final path."""
    import py7zr

    out_path = Path(out_path)
    if out_path.suffix.lower() != ".7z":
        out_path = out_path.with_suffix(".7z")
    progress_cb = progress_cb or (lambda *_a: None)

    payload = json.dumps(manifest, indent=1).encode("utf-8")
    sizes = [len(payload)]
    for src, _arc in bundle_jobs:
        try:
            sizes.append(Path(src).stat().st_size)
        except OSError:
            sizes.append(0)
    total = sum(sizes)
    done = 0
    progress_cb(0, total, PHASE_PACK)

    tmp_dirs = set()
    # Write the manifest through a real temp file rather than writestr():
    # py7zr stores writestr() entries without permission bits, which extract
    # back as mode 000 and break any installer that re-reads collection.json.
    with tempfile.TemporaryDirectory() as td:
        cj_path = Path(td) / "collection.json"
        cj_path.write_bytes(payload)
        with py7zr.SevenZipFile(str(out_path), mode="w") as arc:
            arc.write(str(cj_path), "collection.json")
            done += sizes[0]
            progress_cb(done, total, PHASE_PACK)
            for (src, arcname), size in zip(bundle_jobs, sizes[1:]):
                src = Path(src)
                arc.write(str(src), arcname)
                # Scratch bundles sit directly in the temp root; patch diffs
                # nest under <root>/<mod>/…, so walk up to find the root.
                for parent in src.parents:
                    if parent.name.startswith("amethyst_colexport_"):
                        tmp_dirs.add(parent)
                        break
                done += size
                progress_cb(done, total, PHASE_PACK)

    # Sweep scratch zips created for bundle mods without a cached archive.
    import shutil
    for d in tmp_dirs:
        shutil.rmtree(d, ignore_errors=True)

    return out_path


def export_collection(out_path, rows, game, info: dict, *,
                      progress_cb=None) -> "tuple[Path, list]":
    """Build the manifest and write the archive; returns (final_path, warnings)."""
    manifest, bundle_jobs, warnings = build_collection_manifest(
        rows, game, info, progress_cb=progress_cb)
    if not manifest["mods"]:
        raise ValueError("No exportable mods (all ignored, disabled, or missing).")
    final = pack_collection(out_path, manifest, bundle_jobs,
                            progress_cb=progress_cb)
    return final, warnings
