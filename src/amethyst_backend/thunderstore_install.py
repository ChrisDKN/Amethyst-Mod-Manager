from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .jobs import JobCancelled, JobError, _install_download


def _check(cancel, report=None):
    if cancel.is_set():
        raise JobCancelled("Thunderstore operation cancelled between safe steps.", {"report": report or {}})


def _state(game, profile):
    from Thunderstore.thunderstore_meta import read_meta
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.modlist import read_modlist
    from Utils.profiles.groups import is_group

    if profile.is_symlink() or not profile.is_dir() or is_group(profile):
        raise JobError("profile_group_requires_member", "Install into an available member profile.")
    staging = Path(resolve_target_staging(game, profile))
    if staging.is_symlink() or not staging.is_dir():
        raise JobError("unsafe_staging_path", "The staging folder is unavailable or unsafe.")
    files = []
    paths_file = getattr(game, "_paths_file", None)
    if paths_file:
        files.append(Path(paths_file))
    for member in sorted(profile.parent.iterdir()):
        if member.is_dir():
            files.extend(member / name for name in (
                "modlist.txt", "profile_state.json", "profile_settings.json"))
    installed = {}
    folders = []
    for folder in sorted(staging.iterdir()):
        if not folder.is_dir():
            continue
        folders.append(folder.name)
        path = folder / "meta.ini"
        files.append(path)
        if folder.is_symlink() or path.is_symlink():
            continue
        meta = read_meta(path)
        if meta.package_id:
            installed.setdefault(meta.package_id, []).append({"name": folder.name, "version": meta.version})
    rows = {entry.name: entry.enabled for entry in read_modlist(profile / "modlist.txt")
            if not entry.is_separator and not entry.is_group_header}
    digest = hashlib.sha256(json.dumps({
        "profile": str(profile.resolve()), "staging": str(staging.resolve()), "folders": folders,
        "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  if path.is_file() else None for path in files},
    }, sort_keys=True).encode()).hexdigest()
    return digest, installed, rows


def _ordered(packages):
    from Nexus.nexus_update_checker import _parse_version
    from Thunderstore.thunderstore_meta import parse_full_name

    by_id = {pkg.package_id: pkg for pkg in packages}
    ordered, visited, visiting = [], set(), set()
    def visit(pkg):
        if pkg.package_id in visiting:
            raise JobError("thunderstore_dependency_cycle", "The selected packages contain a dependency cycle.")
        if pkg.package_id in visited:
            return
        visiting.add(pkg.package_id)
        for dependency in pkg.dependencies:
            namespace, name, version = parse_full_name(dependency)
            target = by_id.get(f"{namespace}-{name}")
            have, want = _parse_version(target.version) if target else None, _parse_version(version)
            if target is None or have is None or want is None or have < want:
                raise JobError("thunderstore_dependencies_unavailable", "The resolved graph does not satisfy every dependency.",
                               {"dependency": dependency})
            visit(target)
        visiting.remove(pkg.package_id)
        visited.add(pkg.package_id)
        ordered.append(pkg)
    for pkg in packages:
        visit(pkg)
    return ordered


class ThunderstorePlans:
    maximum = 8
    lifetime = 1800

    def __init__(self):
        self._plans = {}

    def close(self):
        self._plans.clear()

    def plan(self, game, profile, link, log, progress, cancel):
        from Nexus.nexus_update_checker import _parse_version
        from Thunderstore.thunderstore_download import resolve_communities
        from Thunderstore.thunderstore_requirements import resolve_dependencies
        from Utils.operations import acquire_game_operation

        _check(cancel)
        if link.community not in resolve_communities(link):
            raise JobError("thunderstore_community_mismatch", "The package is not listed for this game's community.")
        def resolving(full_name):
            _check(cancel)
            progress(0, 1, f"Resolving {full_name}")
        resolved = resolve_dependencies(link, community=link.community, progress_cb=resolving)
        _check(cancel)
        if resolved.failed or not resolved.packages or not any(pkg.is_root for pkg in resolved.packages):
            raise JobError("thunderstore_dependencies_unavailable", "The dependency graph could not be resolved.",
                           {"failed": resolved.failed})
        if len(resolved.packages) > 256:
            raise JobError("thunderstore_dependency_limit", "The dependency plan exceeds 256 packages.")
        with acquire_game_operation(game, "thunderstore.install.plan"):
            state, installed, enabled = _state(game, profile)
            packages = []
            for pkg in _ordered(resolved.packages):
                from .thunderstore import package_link
                package_link(pkg.namespace, pkg.name, pkg.version, link.community)
                if pkg.file_size <= 0:
                    raise JobError("thunderstore_size_unavailable", "A package has no verifiable archive size.")
                matches = installed.get(pkg.package_id, [])
                if len(matches) > 1:
                    raise JobError("thunderstore_installation_ambiguous", "Several mod folders identify the same package.",
                                   {"package": pkg.package_id, "mods": matches})
                existing = matches[0] if matches else None
                action = "replace" if existing else "install"
                if existing and not pkg.is_root:
                    have, want = _parse_version(existing["version"]), _parse_version(pkg.version)
                    if have is not None and want is not None and have >= want and existing["name"] in enabled:
                        action = "satisfied" if enabled[existing["name"]] else "enable"
                packages.append({
                    "id": pkg.package_id, "namespace": pkg.namespace, "package": pkg.name,
                    "version": pkg.version, "full_name": pkg.full_name,
                    "size_bytes": pkg.file_size, "dependencies": list(pkg.dependencies),
                    "root": pkg.is_root, "action": action,
                    "mod_name": existing["name"] if existing else pkg.full_name,
                    "installed": existing, "enable": True,
                })
            now = time.monotonic()
            self._plans = {key: plan for key, plan in self._plans.items() if now - plan["created"] < self.lifetime}
            if len(self._plans) >= self.maximum:
                self._plans.pop(next(iter(self._plans)))
            plan_id = uuid.uuid4().hex
            from Utils.mods.copy import resolve_target_staging
            from Utils.profiles.state import profile_uses_specific_mods
            public = {"id": plan_id, "profile": profile.name, "community": link.community,
                      "staging_path": str(resolve_target_staging(game, profile)),
                      "shared_staging": not profile_uses_specific_mods(profile),
                      "packages": packages, "conflicts": resolved.conflicts,
                      "download_bytes": sum(pkg["size_bytes"] for pkg in packages if pkg["action"] in {"install", "replace"}),
                      "expires_after_seconds": self.lifetime}
            revision = hashlib.sha256(json.dumps([state, public], sort_keys=True).encode()).hexdigest()
            public["revision"] = revision
            self._plans[plan_id] = {"game": game, "profile": profile, "state": state,
                                    "public": public, "created": now}
            log(f"Resolved {len(packages)} Thunderstore packages in installation order.")
            progress(1, 1, "Dependency plan ready")
            return {"plan": public}

    def get(self, plan_id, revision, game, profile):
        plan = self._plans.get(plan_id)
        if plan is None or time.monotonic() - plan["created"] > self.lifetime:
            self._plans.pop(plan_id, None)
            raise JobError("thunderstore_plan_not_found", "The dependency plan expired; create a new plan.")
        if plan["game"] is not game or plan["profile"] != profile or plan["public"]["revision"] != revision:
            raise JobError("stale_thunderstore_plan", "The selected dependency plan does not match this request.")
        return plan

    def install(self, game, profile, plan_id, revision, dependencies, options, log, progress, cancel):
        from Utils.downloads.inventory import build_inventory
        from Utils.mods.state import set_mods_enabled
        from Utils.operations import OperationCancelled, acquire_game_operation
        from .thunderstore import download, package_link, version_metadata
        from Thunderstore.thunderstore_download import resolve_communities

        plan = self.get(plan_id, revision, game, profile)
        public = plan["public"]
        available = {pkg["id"] for pkg in public["packages"] if not pkg["root"]}
        if dependencies is None:
            selected = available
        else:
            selected = set(dependencies)
            if selected - available:
                raise JobError("thunderstore_dependency_invalid", "The selection contains packages outside the dependency plan.")
        wanted = [pkg for pkg in public["packages"] if pkg["root"] or pkg["id"] in selected]
        report = {"installed": [], "enabled": [], "satisfied": [],
                  "skipped_dependencies": sorted(available - selected), "remaining": [pkg["id"] for pkg in wanted]}
        with acquire_game_operation(game, "thunderstore.install"):
            _check(cancel, report)
            if _state(game, profile)[0] != plan["state"]:
                raise JobError("stale_thunderstore_plan", "The profile or staging library changed; create a new plan.")
            replacements = [pkg["mod_name"] for pkg in wanted if pkg["action"] == "replace"]
            if replacements and options["if_exists"] != "replace":
                raise JobError("mod_already_exists", "The plan replaces existing mods; explicitly allow replacement.",
                               {"mods": replacements})
            metadata_by_package = {}
            for pkg in wanted:
                _check(cancel, report)
                if pkg["action"] not in {"install", "replace"}:
                    continue
                link = package_link(pkg["namespace"], pkg["package"], pkg["version"], public["community"])
                if link.community not in resolve_communities(link):
                    raise JobError("thunderstore_community_mismatch", "A dependency is not listed for this game's community.")
                metadata = version_metadata(link)
                if metadata.file_size != pkg["size_bytes"] or metadata.dependency_list() != pkg["dependencies"]:
                    raise JobError("stale_thunderstore_plan", "Package metadata changed; create a new plan.", {"package": pkg["id"]})
                metadata_by_package[pkg["id"]] = metadata
            try:
                for index, pkg in enumerate(wanted):
                    _check(cancel, report)
                    progress(index, len(wanted), f"Installing {pkg['full_name']}")
                    if pkg["action"] == "satisfied":
                        report["satisfied"].append(pkg["id"])
                    elif pkg["action"] in {"install", "replace"}:
                        link = package_link(pkg["namespace"], pkg["package"], pkg["version"], public["community"])
                        path = download(game, link, metadata_by_package[pkg["id"]], log, progress, cancel)
                        _check(cancel, report)
                        archive = next((item for item in build_inventory(game, profile)["archives"]
                                        if Path(item["path"]).resolve() == path.resolve()), None)
                        if archive is None:
                            raise JobError("archive_not_found", "The downloaded Thunderstore archive is unavailable.")
                        root_options = options if pkg["root"] else {"installer_policy": "reject", "if_exists": options["if_exists"]}
                        job = SimpleNamespace(id=uuid.uuid4().hex, kind="downloads.install", game=game,
                                              profile=profile.name, options={**root_options, "archive_id": archive["id"],
                                                                             "name": pkg["mod_name"]})
                        def verify(prepared, _path):
                            if prepared.is_multi_mod():
                                raise JobError("installer_unsupported", "A Thunderstore package must install as one managed mod.")
                        installed = _install_download(job, log, progress, operation_locked=True, verify_prepared=verify)
                        report["installed"].append({"package": pkg["id"], "result": installed})
                    if pkg["action"] != "satisfied":
                        set_mods_enabled(game, profile, [(pkg["mod_name"], True)], log_fn=log,
                                         progress_fn=progress, operation_locked=True)
                        report["enabled"].append(pkg["id"])
                    report["remaining"].remove(pkg["id"])
                progress(len(wanted), len(wanted), "Thunderstore installation complete")
                return {"ok": True, "profile": profile.name, **report}
            except JobCancelled as exc:
                exc.details["report"] = report
                raise
            except OperationCancelled as exc:
                raise JobCancelled("Thunderstore download cancelled; completed packages were retained.", {"report": report}) from exc
            except Exception as exc:
                raise JobError("thunderstore_install_failed", "Thunderstore installation stopped; completed packages were retained.",
                               {"report": report, "cause": {"name": getattr(exc, "name", "operation_failed"),
                                                            "message": str(exc), "details": getattr(exc, "details", {})}}) from exc
            finally:
                self._plans.pop(plan_id, None)
