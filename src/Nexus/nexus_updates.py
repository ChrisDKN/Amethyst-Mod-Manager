"""Shared Nexus update selection for checks, version lists and Quick Update."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from Utils.mods.versions import _normalize_match

_ACTIVE = {"MAIN", "UPDATE", "OPTIONAL", "MISCELLANEOUS"}
_SUPERSEDED = {"OLD_VERSION", "ARCHIVED", "REMOVED"}


@dataclass
class UpdateTarget:
    file: object = None
    has_update: bool = False
    automatic: bool = False
    source: str = ""
    installed_version: str = ""
    match_id: int = -1
    old_ids: set[int] = field(default_factory=set)
    error: str = ""


def _category(file):
    return (file.category_name or "").strip().upper()


def _position(row):
    try:
        value = Decimal(str(row["position"]))
        return value if value.is_finite() else None
    except (KeyError, TypeError, InvalidOperation):
        return None


class NexusUpdateResolver:
    def __init__(self, api, log=None):
        self.api = api
        self.log = log or (lambda _message: None)
        self._game_ids = {}
        self._versions = {}

    def prepare(self, domain, entries, *, history=False):
        """Batch installed identities and active candidates once per operation."""
        if domain not in self._game_ids:
            try:
                self._game_ids[domain] = self.api._resolve_game_id(domain)
            except Exception:
                self._game_ids[domain] = 0
        game_id = self._game_ids[domain]
        if not game_id:
            return
        wanted = set()
        for meta, files in entries:
            if meta.file_id > 0:
                wanted.add(meta.file_id)
            wanted.update(f.file_id for f in files if f.file_id > 0
                          and (history or _category(f) in _ACTIVE))
        pending = {fid for fid in wanted if (domain, fid) not in self._versions}
        if not pending:
            return
        try:
            rows = self.api.get_mod_file_versions_batch(
                [(game_id << 32) | fid for fid in sorted(pending)])
        except Exception as exc:
            self.log(f"  v3 update groups unavailable ({exc}); using file comparisons.")
            rows = {}
        for fid in pending:
            self._versions[domain, fid] = rows.get((game_id << 32) | fid)
        missing = sum(self._versions[domain, fid] is None for fid in pending)
        self.log(f"  v3 update groups: resolved {len(pending) - missing}/{len(pending)} file version(s).")

    def _record(self, domain, meta, file_id):
        row = self._versions.get((domain, file_id))
        try:
            expected_mod = (self._game_ids[domain] << 32) | meta.mod_id
            if (not row or int(row["mod_id"]) != expected_mod
                    or not row.get("mod_file_id") or _position(row) is None):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return row

    def resolve(self, domain, meta, files):
        if not files:
            return UpdateTarget(error="Nexus returned no files")
        self.prepare(domain, [(meta, files)])
        installed = next((f for f in files if f.file_id == meta.file_id), None)
        installed_version = ((installed.version or installed.mod_version)
                             if installed else "") or meta.version or ""
        result = self._chain(domain, meta, files, installed, installed_version)
        if result is None:
            result = self._fallback(meta, files, installed, installed_version)
        target = result.file
        if result.error:
            self.log(f"  {meta.mod_name}: could not check updates ({result.error}).")
        else:
            selected = f"file {target.file_id}, {target.version or target.mod_version or '?'}" if target else "no active successor"
            self.log(f"  {meta.mod_name}: {result.source} → {selected}; "
                     f"{'update available' if result.has_update else 'up to date'}.")
        return result

    def _chain(self, domain, meta, files, installed, installed_version):
        current = self._record(domain, meta, meta.file_id)
        if current is None:
            return None
        active = [f for f in files if _category(f) in _ACTIVE]
        records = {f.file_id: self._record(domain, meta, f.file_id) for f in active}
        if any(row is None for row in records.values()):
            return None
        group_id = str(current["mod_file_id"])
        candidates = [f for f in active if str(records[f.file_id]["mod_file_id"]) == group_id]
        current_position = _position(current)
        latest = max(candidates, key=lambda f: _position(records[f.file_id]), default=None)
        has_update = latest is not None and _position(records[latest.file_id]) > current_position
        if not has_update:
            latest = installed
        match_id = latest.file_id if latest else meta.file_id
        boundary = _position(records[latest.file_id]) if has_update else current_position
        old_ids = set()
        for f in files:
            row = self._record(domain, meta, f.file_id)
            if row and str(row["mod_file_id"]) == group_id and _position(row) < boundary:
                old_ids.add(f.file_id)
        return UpdateTarget(
            file=latest, has_update=has_update, automatic=True,
            source=f"v3 group {group_id}",
            installed_version=current.get("version") or installed_version,
            match_id=match_id, old_ids=old_ids)

    def _fallback(self, meta, files, installed, installed_version):
        from Nexus.nexus_update_checker import _parse_install_date, _version_is_newer

        category = ((meta.file_category or "").strip()
                    or (installed.category_name if installed else "")).upper()
        active = [f for f in files if _category(f) in _ACTIVE]
        candidates = ([f for f in active if _category(f) == category]
                      if category and category not in _SUPERSEDED else active)
        name = _normalize_match((installed.name or installed.file_name) if installed
                                else meta.nexus_file_name or meta.mod_name)
        matching = [f for f in candidates if name and _normalize_match(f.name) == name]
        automatic = bool(matching)
        if matching:
            candidates = matching
            source = "matching name"
        else:
            source = f"saved {category} category" if category and category not in _SUPERSEDED else "active file fallback"
        latest = max(candidates, key=lambda f: f.uploaded_timestamp, default=installed)
        if latest is None:
            return UpdateTarget(error="no matching active files", source=source)
        latest_version = latest.version or latest.mod_version or ""
        if latest.file_id == meta.file_id:
            has_update = False
        elif installed_version and latest_version:
            has_update = _version_is_newer(latest_version, installed_version)
        else:
            installed_date = _parse_install_date(meta)
            if installed_date is None or not latest.uploaded_timestamp:
                return UpdateTarget(error="no comparable version or upload date", source=source)
            has_update = datetime.fromtimestamp(latest.uploaded_timestamp, timezone.utc) > installed_date
        old_ids = {f.file_id for f in files if name and _normalize_match(f.name) == name
                   and f.file_id != latest.file_id and f.uploaded_timestamp <= latest.uploaded_timestamp}
        return UpdateTarget(
            file=latest, has_update=has_update, automatic=automatic, source=source,
            installed_version=installed_version, match_id=latest.file_id, old_ids=old_ids)
