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
except Exception:       # optional dependency — file-edit patches need it
    bsdiff4 = None

# Sub-phase labels for progress_cb(done, total, phase). done/total count mods
# during the build phase and bytes during packing.
PHASE_META = "meta"
PHASE_HASH = "hash"
PHASE_BUNDLE = "bundle"
PHASE_PATCH = "patch"
PHASE_PACK = "pack"

UPDATE_POLICIES = ("exact", "prefer", "latest")

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


def _cached_archive(meta, game_name: str = "") -> "Path | None":
    """The mod's original archive in the download cache, or None.

    Downloads land in a per-game subfolder (download_cache/<game>/), with the
    cache root kept as a fallback for legacy layouts.
    """
    fname = (getattr(meta, "installation_file", "") or "").strip() if meta else ""
    if not fname:
        return None
    root = get_download_cache_dir()
    candidates = ([root / game_name / fname] if game_name else []) + [root / fname]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _archive_md5(archive: Path) -> str:
    """MD5 of an archive — served from the download-cache md5 cache when possible."""
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
# FOMOD choices — Amethyst sidecar -> Vortex "options" shape
# ---------------------------------------------------------------------------

_FOMOD_XML_RE = re.compile(r"(^|/)fomod/moduleconfig\.xml$", re.IGNORECASE)


def _module_config_from_archive(archive: Path):
    """Parse fomod/ModuleConfig.xml straight out of a .zip/.7z archive, or None."""
    from Utils.fomod_parser import parse_module_config
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
        else:  # .rar and friends — only if rarfile is importable
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

def _crc32_hex(data: bytes) -> str:
    """8-char uppercase CRC32 — the format collection.json patch maps use."""
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
    differs from the archive's — the manifest's ``patches`` map. Installers
    re-download the untouched mod and apply these patches, so only files that
    exist in the archive can be covered.
    """
    if bsdiff4 is None:
        warnings.append(
            f"'{mod_label}': file edits skipped — bsdiff4 is not installed.")
        return {}

    import shutil

    tmp_root = Path(tempfile.mkdtemp(prefix="amethyst_colpatch_"))
    try:
        if not _extract_archive_to(archive, tmp_root):
            warnings.append(
                f"'{mod_label}': file edits skipped — could not read the "
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
                    "patch — users will get the unmodified file.")
                continue

            try:
                diff = bsdiff4.diff(src_bytes, dst_bytes)
            except Exception as exc:
                warnings.append(f"'{mod_label}': could not diff '{rel}' ({exc}).")
                continue

            # An oversized patch means the file was effectively replaced —
            # bundling the mod is the right answer, not shipping a huge diff.
            if len(diff) - PATCH_OVERHEAD > len(src_bytes) * MAX_PATCH_RATIO:
                warnings.append(
                    f"'{mod_label}': edits to '{rel}' are too extensive to ship "
                    "as a patch — users will get the unmodified file.")
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
    ``INI Tweaks/Profile Settings [<Target>].ini`` — the exact layout both our
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
            "Profile INI files were not included — this game has no known "
            "INI tweak targets.")
        return []
    by_lower = {t.lower(): t for t in targets}

    jobs: list = []
    for src in ini_files:
        target = by_lower.get(src.name.lower())
        if target is None:
            warnings.append(
                f"'{src.name}' skipped — not a recognised game INI for this "
                "game.")
            continue
        stem = target[:-len(".ini")]
        jobs.append((src, f"INI Tweaks/Profile Settings [{stem}].ini"))
    return jobs


def _conflict_mod_rules(ordered_names: list, refs: dict, staging_root) -> list:
    """Emit 'after' rules between consecutive providers of each conflicting file."""
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(Path(staging_root).parent / "modindex.bin") or {}
    except Exception:
        index = {}
    if not index:
        return []

    priority = {name: i for i, name in enumerate(ordered_names)}
    providers: dict = {}   # rel_key -> [mod names]
    for name in ordered_names:
        entry = index.get(name)
        if not entry:
            continue
        normal_files = entry[0] or {}
        for rel_key in normal_files:
            providers.setdefault(rel_key, []).append(name)

    rules: list = []
    seen: set = set()
    for mods in providers.values():
        if len(mods) < 2:
            continue
        # ordered_names is highest-priority-first, so mods[i] beats mods[i+1]
        # and must load AFTER it to win the overwrite in Vortex.
        mods.sort(key=lambda n: priority[n])
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


def _plugin_rules_block(profile_dir) -> "dict | None":
    """LOOT userlist.yaml after-rules/groups as manifest pluginRules, or None."""
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
        rule: dict = {"name": name}
        after = _names(entry.get("after"))
        if after:
            rule["after"] = after
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
        policy = row.get("update_policy") or "exact"
        if policy not in UPDATE_POLICIES or row_source == "bundle":
            policy = "exact"

        md5 = ""
        file_size = row.get("size_bytes") or (getattr(meta, "file_size", 0) or 0)

        if row_source == "bundle":
            progress_cb(i, total, PHASE_BUNDLE)
            if archive is not None:
                bundle_name = archive.name
                bundle_path = archive
            else:
                # No cached archive — zip the staged folder into a scratch file.
                mod_dir = Path(staging_root) / name if staging_root else None
                if not mod_dir or not mod_dir.is_dir():
                    warnings.append(f"'{name}': staged files not found — skipped.")
                    continue
                if tmp_dir is None:
                    tmp_dir = Path(tempfile.mkdtemp(
                        prefix="amethyst_colexport_",
                        dir=str(get_download_cache_dir())))
                bundle_name = f"{name}.zip"
                bundle_path = tmp_dir / bundle_name
                with zipfile.ZipFile(bundle_path, "w",
                                     compression=zipfile.ZIP_DEFLATED) as zf:
                    for fp in sorted(mod_dir.rglob("*")):
                        if fp.is_file() and fp.name != "meta.ini":
                            zf.write(fp, fp.relative_to(mod_dir).as_posix())
            file_size = bundle_path.stat().st_size
            bundle_jobs.append((bundle_path, f"bundled/{bundle_name}"))
            # No md5 for bundled files: installers re-compress them, so a hash
            # taken here would never match (Vortex omits it for the same
            # reason — modToCollection.ts).
            md5 = ""
            source: dict = {
                "type": "bundle",
                "fileExpression": bundle_name,
                "fileSize": file_size,
                "updatePolicy": "exact",
                "tag": tag,
            }
            file_expr = bundle_name
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

        # FOMOD choices — only exportable when the original archive still has
        # the installer config we can map names/indices from. BAIN has no
        # Vortex equivalent.
        if row.get("has_fomod") and row.get("fomod_export", True):
            if row.get("has_bain"):
                warnings.append(
                    f"'{name}': BAIN installer choices can't be represented "
                    "in a Nexus collection — exported without choices.")
            else:
                selections = _load_fomod_selections(row, game_name, profile_dir)
                options = None
                if selections:
                    config = (_module_config_from_archive(archive)
                              if archive is not None else None)
                    if config is not None:
                        options = _fomod_options(selections, config)
                if options:
                    mod_entry["choices"] = {"options": options}
                else:
                    warnings.append(
                        f"'{name}': FOMOD choices could not be recovered "
                        "(original archive missing or changed) — installers "
                        "will run interactively.")

        # Local file edits → binary patches applied over the pristine download.
        # Bundled mods already ship the edited files themselves.
        if row.get("save_edits") and row_source != "bundle":
            progress_cb(i, total, PHASE_PATCH)
            mod_dir = Path(staging_root) / name if staging_root else None
            if archive is None:
                warnings.append(
                    f"'{name}': file edits skipped — the original archive is "
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
        plugin_rules = _plugin_rules_block(profile_dir)
        if plugin_rules is not None:
            manifest["pluginRules"] = plugin_rules

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
