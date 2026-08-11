"""
thunderstore_identify.py
Identify manually-installed Thunderstore mods and stamp their metadata.

Thunderstore has **no hash lookup** - unlike Nexus, where an archive's MD5
resolves to a mod/file id. Verified 2026-08-11 across all three API
generations (v1, experimental, cyberstorm) and the OpenAPI schema: no
``md5``/``sha``/``checksum`` field on any package or version, and no
lookup-by-hash route. ``PackageVersion`` carries only ``file_size``.

What Thunderstore *does* give us is better: every package ships a
``manifest.json`` at the root of its zip, and that file survives installation.
It carries the exact ``name``, ``version_number`` and pinned ``dependencies``.

The one thing the manifest omits is the **namespace** (the publishing team), so
that is resolved from the API by searching the community for the name. Package
names are not unique across teams - 12.2% of Lethal Company packages share a
name with another team's (measured) - so :func:`resolve_namespace` disambiguates
by checking which candidate actually published the manifest's exact version,
falling back to the most-downloaded one.

Flow: read manifest -> resolve namespace -> stamp ``[thunderstore]`` meta.ini,
after which update checking, the update flag and the actions submenu all work
for a mod the user installed by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from Utils.app_log import app_log

# A Thunderstore manifest is tiny; anything larger is not one.
_MAX_MANIFEST_BYTES = 256 * 1024


@dataclass
class ManifestInfo:
    """The parts of a package's manifest.json we can use."""

    name: str = ""
    version: str = ""
    description: str = ""
    website_url: str = ""
    dependencies: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.name)


@dataclass
class IdentifyResult:
    """Outcome of identifying one staging folder."""

    mod_name: str = ""            # staging folder name
    manifest: Optional[ManifestInfo] = None
    namespace: str = ""
    community: str = ""
    matched: bool = False         # namespace resolved
    ambiguous: bool = False       # several teams publish this name
    candidates: list = field(default_factory=list)   # namespaces considered
    reason: str = ""              # why it failed, for the log

    @property
    def package_id(self) -> str:
        if self.namespace and self.manifest:
            return f"{self.namespace}-{self.manifest.name}"
        return ""


def read_manifest(mod_dir: Path) -> Optional[ManifestInfo]:
    """Parse ``manifest.json`` from an installed mod folder, or None.

    Looks at the folder root first, then one level down - some packages wrap
    their payload in a subfolder, and the installer's strip-prefix rules can
    leave the manifest either place.
    """
    mod_dir = Path(mod_dir)
    candidates = [mod_dir / "manifest.json"]
    try:
        candidates += [p / "manifest.json" for p in mod_dir.iterdir()
                       if p.is_dir()]
    except OSError:
        pass

    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
                continue
            # Thunderstore manifests are frequently UTF-8 *with BOM*; plain
            # utf-8 decoding chokes on the leading ﻿.
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = (data.get("name") or "").strip()
        if not name:
            continue
        deps = data.get("dependencies")
        return ManifestInfo(
            name=name,
            version=(data.get("version_number") or "").strip(),
            description=(data.get("description") or "").strip(),
            website_url=(data.get("website_url") or "").strip(),
            dependencies=[str(d) for d in deps] if isinstance(deps, list) else [],
        )
    return None


def _package_has_version(namespace: str, name: str, version: str) -> bool:
    """Whether *namespace* published exactly this version of *name*."""
    if not version:
        return False
    from Thunderstore.thunderstore_api import fetch_versions
    for v in fetch_versions(namespace, name):
        if isinstance(v, dict) and (v.get("version_number") or "") == version:
            return True
    return False


def resolve_namespace(community: str, name: str, version: str = "",
                      ) -> tuple[str, list]:
    """
    Find which team publishes *name* in *community*.

    Returns ``(namespace, candidates)``; namespace is "" when nothing matched.
    Package names are NOT unique across teams, so when several publish the same
    name the one that actually released *version* wins, and failing that the
    most-downloaded one (the search endpoint already ranks by relevance, and the
    popular package is overwhelmingly the one a user installed).
    """
    from Thunderstore.thunderstore_api import fetch_listing

    rows, _count = fetch_listing(community, query=name)
    exact = [r for r in rows if r.name.lower() == name.lower()]
    if not exact:
        return "", []
    candidates = [r.namespace for r in exact]
    if len(exact) == 1:
        return exact[0].namespace, candidates

    # Ambiguous: prefer whoever actually shipped this exact version.
    if version:
        for r in exact:
            if _package_has_version(r.namespace, r.name, version):
                return r.namespace, candidates
    # Otherwise the most-downloaded package of that name.
    best = max(exact, key=lambda r: r.download_count or 0)
    return best.namespace, candidates


def identify_mod(mod_dir: Path, community: str) -> IdentifyResult:
    """Identify one installed mod folder from its manifest + the API."""
    mod_dir = Path(mod_dir)
    res = IdentifyResult(mod_name=mod_dir.name, community=community)

    manifest = read_manifest(mod_dir)
    if manifest is None:
        res.reason = "no manifest.json"
        return res
    res.manifest = manifest

    # The folder is often still named "{namespace}-{name}-{version}" (the
    # archive name), which gives the namespace for free - no API call needed.
    from Thunderstore.thunderstore_meta import parse_full_name
    ns, nm, _ver = parse_full_name(mod_dir.name)
    if ns and nm.lower() == manifest.name.lower():
        res.namespace = ns
        res.matched = True
        res.candidates = [ns]
        return res

    if not community:
        res.reason = "no community for this game"
        return res

    namespace, candidates = resolve_namespace(
        community, manifest.name, manifest.version)
    res.candidates = candidates
    if not namespace:
        res.reason = f"'{manifest.name}' not found on {community}"
        return res
    res.namespace = namespace
    res.matched = True
    res.ambiguous = len(candidates) > 1
    return res


def stamp_identified(mod_dir: Path, result: IdentifyResult) -> bool:
    """Write the ``[thunderstore]`` section for an identified mod.

    Never overwrites a mod that already carries Thunderstore metadata.
    Returns True when a stamp was written.
    """
    from Thunderstore.thunderstore_meta import (
        ThunderstoreModMeta, read_meta, write_meta)

    if not (result.matched and result.manifest):
        return False
    meta_path = Path(mod_dir) / "meta.ini"
    existing = read_meta(meta_path)
    if existing.package_id:
        return False        # already identified - leave it alone

    m = result.manifest
    meta = ThunderstoreModMeta(
        community=result.community,
        namespace=result.namespace,
        name=m.name,
        version=m.version,
        full_name=f"{result.namespace}-{m.name}-{m.version}"
        if m.version else "",
        description=m.description,
        website_url=m.website_url,
        dependencies=", ".join(m.dependencies),
    )
    write_meta(meta_path, meta)
    app_log(f"thunderstore: identified '{Path(mod_dir).name}' as "
            f"{meta.package_id} {m.version}")
    return True


def scan_unidentified(staging_root: Path) -> list:
    """Staging folders that have a Thunderstore manifest but no metadata yet."""
    out: list = []
    staging_root = Path(staging_root)
    if not staging_root.is_dir():
        return out
    from Thunderstore.thunderstore_meta import read_meta
    for folder in sorted(staging_root.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            if read_meta(folder / "meta.ini").package_id:
                continue        # already has Thunderstore metadata
        except Exception:
            pass
        if read_manifest(folder) is not None:
            out.append(folder)
    return out


def identify_all(staging_root: Path, community: str, *,
                 save: bool = True,
                 progress_cb: Optional[Callable[[str], None]] = None,
                 ) -> list:
    """
    Identify every unstamped Thunderstore mod under *staging_root*.

    Returns the :class:`IdentifyResult` list (matched and unmatched), so the
    caller can report both what was adopted and what could not be resolved.
    """
    results: list = []
    for folder in scan_unidentified(staging_root):
        if progress_cb:
            progress_cb(f"identifying {folder.name}")
        res = identify_mod(folder, community)
        if res.matched and save:
            try:
                stamp_identified(folder, res)
            except Exception as exc:
                res.matched = False
                res.reason = f"could not stamp: {exc}"
        results.append(res)
    matched = [r for r in results if r.matched]
    app_log(f"thunderstore: identified {len(matched)} of {len(results)} "
            "unstamped mod(s)")
    return results
