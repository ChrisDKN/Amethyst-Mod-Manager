"""
thunderstore_update_checker.py
Check installed Thunderstore mods for newer package versions.

Workflow mirrors the Nexus / mod.io checkers:
  1. Scan staging for folders whose ``meta.ini`` has a ``[thunderstore]``
     section (see :mod:`Thunderstore.thunderstore_meta`).
  2. Ask the API for each package's newest version.
  3. Write ``latestVersion`` / ``hasUpdate`` back into the mod's own section and
     return the mods that have an update.

Only the ``[thunderstore]`` section is touched, so this can run in parallel
with the Nexus and mod.io checkers - they write disjoint keys of the same file.

No API key, no rate limit, and a package's whole version list comes back in one
request, so a check is cheap: one HTTP call per distinct package.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from Nexus.nexus_update_checker import _parse_version
from Thunderstore.thunderstore_download import fetch_json
from Thunderstore.thunderstore_meta import read_meta, write_meta
from Utils.app_log import app_log

ProgressCallback = Callable[[str], None]


@dataclass
class ThunderstoreUpdateInfo:
    """One mod's update status."""

    mod_name: str = ""              # staging folder name
    namespace: str = ""
    name: str = ""
    installed_version: str = ""
    latest_version: str = ""
    has_update: bool = False
    is_deprecated: bool = False     # package flagged deprecated upstream
    # True when the package could not be looked up (deleted, renamed, offline),
    # so the state is genuinely unknown rather than up to date.
    unknown: bool = False

    @property
    def package_id(self) -> str:
        return f"{self.namespace}-{self.name}"


def package_api_url(namespace: str, name: str,
                    host: str = "thunderstore.io") -> str:
    """Experimental package endpoint - carries ``latest`` + ``is_deprecated``."""
    return f"https://{host}/api/experimental/package/{namespace}/{name}/"


def _newest_version(namespace: str, name: str, host: str
                    ) -> tuple[str, bool, bool]:
    """Return ``(latest_version, is_deprecated, ok)`` for a package."""
    data = fetch_json(package_api_url(namespace, name, host))
    if not isinstance(data, dict):
        return "", False, False
    latest = (data.get("latest") or {}).get("version_number") or ""
    return latest, bool(data.get("is_deprecated")), bool(latest)


def scan_installed(staging_root: Path) -> list:
    """Every staging folder carrying Thunderstore metadata."""
    out: list = []
    staging_root = Path(staging_root)
    if not staging_root.is_dir():
        return out
    for folder in sorted(staging_root.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        meta_path = folder / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
        except Exception:
            continue
        if meta.package_id:
            out.append((folder, meta))
    return out


def check_for_updates(staging_root: Path, *,
                      host: str = "thunderstore.io",
                      only_names: Optional[Iterable[str]] = None,
                      save_results: bool = True,
                      progress_cb: Optional[ProgressCallback] = None,
                      ) -> list:
    """
    Check every Thunderstore mod in *staging_root* for a newer version.

    *only_names* limits the check to those staging folder names (the modlist's
    right-click subset); None checks everything. Returns the
    :class:`ThunderstoreUpdateInfo` entries that have an update, skipping any
    the user muted via ``ignoreUpdate`` / ``ignoredVersion``.
    """
    subset = set(only_names) if only_names else None
    installed = scan_installed(staging_root)
    if subset is not None:
        installed = [(f, m) for (f, m) in installed if f.name in subset]

    # One lookup per distinct package, even when several folders share it.
    looked_up: dict = {}
    results: list = []

    for folder, meta in installed:
        pkg_id = meta.package_id
        if progress_cb:
            progress_cb(f"checking {pkg_id}")

        if pkg_id not in looked_up:
            looked_up[pkg_id] = _newest_version(meta.namespace, meta.name, host)
        latest, deprecated, ok = looked_up[pkg_id]

        info = ThunderstoreUpdateInfo(
            mod_name=folder.name,
            namespace=meta.namespace,
            name=meta.name,
            installed_version=meta.version,
            latest_version=latest,
            is_deprecated=deprecated,
        )
        if not ok:
            info.unknown = True
            app_log(f"thunderstore: could not look up {pkg_id} - "
                    "update state unknown")
            results.append(info)
            continue

        cur = _parse_version(meta.version)
        new = _parse_version(latest)
        info.has_update = bool(cur is not None and new is not None and new > cur)

        # Respect the user's mute: either "ignore all updates for this mod" or
        # "ignore this specific version".
        muted = meta.ignore_update or (
            meta.ignored_version and meta.ignored_version == latest)

        if save_results:
            try:
                meta.latest_version = latest
                meta.has_update = info.has_update and not muted
                meta.is_deprecated = deprecated
                write_meta(folder / "meta.ini", meta)
            except Exception as exc:
                app_log(f"thunderstore: could not save update state for "
                        f"{folder.name}: {exc}")

        if info.has_update and not muted:
            results.append(info)

    updated = [r for r in results if r.has_update]
    app_log(f"thunderstore: checked {len(installed)} mod(s), "
            f"{len(updated)} update(s) available")
    return results


def set_ignore_update(meta_ini_path: Path, ignore: bool) -> None:
    """Mute (or unmute) all future update flags for one mod."""
    meta = read_meta(meta_ini_path)
    meta.ignore_update = ignore
    if not ignore:
        meta.ignored_version = ""
    meta.has_update = False if ignore else meta.has_update
    write_meta(meta_ini_path, meta)


def ignore_version(meta_ini_path: Path, version: str) -> None:
    """Mute updates only for *version* - a newer one flags again."""
    meta = read_meta(meta_ini_path)
    meta.ignored_version = version or ""
    meta.has_update = False
    write_meta(meta_ini_path, meta)
