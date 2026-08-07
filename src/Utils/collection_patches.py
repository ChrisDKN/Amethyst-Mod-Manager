"""Apply BSDIFF40 binary patches shipped with a Nexus collection archive.

The archive's ``patches/`` folder contains, per mod:

    patches/<mod-name>/<file-relative-to-mod>.diff

Each ``.diff`` is a BSDIFF40 patch produced by Vortex. ``collection.json``
lists, per mod, a ``patches`` map of ``{relative_path: expected_source_crc32}``.
We only apply a patch when the source file's CRC32 matches the expected value
(otherwise the user has a different version of the underlying mod than the
curator built the patch against).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import bsdiff4  # type: ignore
    _BSDIFF_AVAILABLE = True
except Exception:  # pragma: no cover
    bsdiff4 = None  # type: ignore
    _BSDIFF_AVAILABLE = False


@dataclass
class PatchResult:
    applied: int = 0
    crc_mismatch: int = 0
    missing_diff: int = 0
    missing_target: int = 0
    failed: int = 0


def _crc32_hex(data: bytes) -> str:
    """Match Vortex's format: 8-char uppercase hex of unsigned CRC32."""
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"


def _tag_mod_as_patched(
    mod_dir: Path, slug: str, revision: "str | None",
) -> None:
    """Set ``[General] fromCollectionPatched=true`` (and revision) in the
    mod's ``meta.ini``. Preserves all other fields. Idempotent."""
    import configparser
    meta = mod_dir / "meta.ini"
    cp = configparser.ConfigParser()
    if meta.is_file():
        try:
            cp.read(meta, encoding="utf-8")
        except Exception:
            cp = configparser.ConfigParser()
    if not cp.has_section("General"):
        cp.add_section("General")
    cp["General"]["fromCollectionPatched"] = "true"
    if slug and not cp["General"].get("fromCollection"):
        cp["General"]["fromCollection"] = slug
    if revision:
        cp["General"]["fromCollectionRevision"] = revision
    try:
        with open(meta, "w", encoding="utf-8") as fh:
            cp.write(fh)
    except Exception:
        pass


def apply_collection_patches(
    archive_root: Path,
    collection_schema: dict,
    staging_path: Path,
    mod_folder_for: Callable[[dict], str | None],
    log_fn: Callable[[str], None] | None = None,
    collection_slug: str = "",
    collection_revision: "str | None" = None,
) -> PatchResult:
    """Apply every patch listed in ``collection_schema`` whose CRC matches.

    ``archive_root`` is the directory containing the extracted ``patches/``
    folder (typically the same dir ``collection.json`` lives in).

    ``mod_folder_for(schema_entry)`` maps a mod entry from
    ``collection_schema['mods']`` to its installed folder name under
    ``staging_path``. Return ``None`` to skip that entry.
    """
    log = log_fn or (lambda *_a: None)
    result = PatchResult()
    patched_mods: set[str] = set()  # installed-folder names we already tagged

    if not _BSDIFF_AVAILABLE:
        log("Collection patches: bsdiff4 not installed — patches skipped")
        return result

    patches_root = archive_root / "patches"
    if not patches_root.is_dir():
        return result

    for mod_entry in collection_schema.get("mods", []):
        patches: dict = mod_entry.get("patches") or {}
        if not patches:
            continue

        installed_folder = mod_folder_for(mod_entry)
        if not installed_folder:
            continue

        # Patch sources live under the mod's ``name``, its
        # ``source.fileExpression``, or — for our own exports — the SANITIZED
        # name: collection_export strips characters that are illegal in
        # archive members (':', '?', …), so a mod named "X: SE" ships its
        # diffs under "X SE" and the raw-name lookup alone would miss every
        # one of them. Empty components must be skipped, not joined:
        # ``patches_root / ""`` is ``patches_root`` itself, which always
        # exists and would swallow the lookup (nexus sources carry no
        # fileExpression, so this was the common case).
        name = mod_entry.get("name") or ""
        candidates = [name,
                      (mod_entry.get("source") or {}).get("fileExpression") or ""]
        if name:
            from Utils.collection_export import _safe_archive_component
            safe = _safe_archive_component(name)
            if safe != name:
                candidates.append(safe)
        archive_subdir = next(
            (patches_root / c for c in candidates
             if c and (patches_root / c).is_dir()), None)
        if archive_subdir is None:
            for rel, _crc in patches.items():
                log(f"Collection patches: no patch folder for '{mod_entry.get('name')}' "
                    f"(looking for {rel}.diff)")
                result.missing_diff += 1
            continue

        mod_dir = staging_path / installed_folder

        for rel_path, expected_crc in patches.items():
            # collection.json stores Windows-style separators; normalise so the
            # path resolves on Linux too.
            rel_norm = rel_path.replace("\\", "/")
            target_file = mod_dir / rel_norm
            diff_file = archive_subdir / f"{rel_norm}.diff"

            if not diff_file.is_file():
                log(f"Collection patches: missing diff {diff_file}")
                result.missing_diff += 1
                continue
            if not target_file.is_file():
                log(f"Collection patches: target file not in staging: {target_file}")
                result.missing_target += 1
                continue

            tmp = target_file.with_suffix(target_file.suffix + ".patched")
            try:
                src = target_file.read_bytes()
                actual_crc = _crc32_hex(src)
                if actual_crc.upper() != str(expected_crc).upper():
                    log(f"Collection patches: CRC mismatch for {rel_path} "
                        f"(expected {expected_crc}, got {actual_crc}) — skipping")
                    result.crc_mismatch += 1
                    continue

                patched = bsdiff4.patch(src, diff_file.read_bytes())
                tmp.write_bytes(patched)
                tmp.replace(target_file)
                result.applied += 1
                patched_mods.add(installed_folder)
                log(f"Collection patches: applied {rel_path} → {installed_folder}")
            except Exception as exc:
                log(f"Collection patches: failed to patch {rel_path} in "
                    f"'{installed_folder}': {exc}")
                result.failed += 1
            finally:
                # Clean up partial/stray .patched if the atomic replace didn't
                # consume it (write error, bsdiff failure, etc.).
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    # Tag every mod that received at least one patch so update runs can
    # identify and refresh them from the new collection archive.
    if collection_slug and patched_mods:
        for folder in patched_mods:
            _tag_mod_as_patched(staging_path / folder, collection_slug, collection_revision)

    return result
