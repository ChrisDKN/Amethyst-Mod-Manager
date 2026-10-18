from dataclasses import asdict
from pathlib import Path

from .jobs import JobError


def runner(game, profile, mod, options, *, apply):
    def run(log, progress):
        from Nexus.nexus_meta import read_meta, merge_reinstall_metadata, normalise_game_domain
        from Utils.deployment.locking import coordinated_game_mutation
        from .mod_properties import get_properties
        from .mod_versions import _context, _selected_target, _archive, _apply_change

        with coordinated_game_mutation(game, "mods.rollback" if apply else "mods.rollback.get"):
            properties = get_properties(game, profile, mod)
            if apply and properties["revision"] != options["revision"]:
                raise JobError("stale_mod_version", "The installed mod changed; inspect rollback again.")
            previous = read_meta(Path(properties["path"])).previous_version
            if previous is None or not previous.valid:
                if apply:
                    raise JobError("mod_rollback_unavailable", "This mod has no recorded previous version.")
                return {"available": False, "mod": mod, "profile": profile.name, "revision": properties["revision"]}
            context = _context(game, profile, mod, properties["revision"], previous.source)
            context.update(game=game, operation="rollback")
            _selected_target(game, context)
            if properties["routing_scan_errors"]:
                raise JobError("mod_version_routing_unavailable", "Could not inspect every affected profile.",
                               {"profiles": properties["routing_scan_errors"]})
            fresh, verify_dependencies = None, None
            if previous.source == "nexus":
                def archive_reader(archive_id):
                    return _archive(game, profile, previous.game_domain, previous.mod_id, previous.file_id, archive_id)
                metadata = merge_reinstall_metadata(previous.nexus_meta(), context["nexus_meta"])
                def verify_source(actual):
                    if (normalise_game_domain(actual.game_domain), actual.mod_id, actual.file_id) != (
                            normalise_game_domain(previous.game_domain), previous.mod_id, previous.file_id):
                        raise JobError("mod_version_metadata_failed", "The restored Nexus identity did not match the recorded version.")
            else:
                from .thunderstore import package_link, retained_archive, dependency_report
                link = package_link(previous.namespace, previous.name, previous.version, context["community"])
                fresh = previous.thunderstore_meta()
                def archive_reader(archive_id):
                    return retained_archive(game, profile, link, previous.file_size, archive_id)
                metadata = merge_reinstall_metadata(None, context["nexus_meta"])
                def verify_source(_actual):
                    return None
                def verify_dependencies():
                    missing = dependency_report(game, context, fresh)
                    if missing and options.get("dependency_policy", "reject") == "reject":
                        raise JobError("thunderstore_dependencies_required", "The previous version needs unavailable dependencies.",
                                       {"dependencies": missing})
            metadata.version = previous.version or metadata.version
            archive = archive_reader(options.get("archive_id"))
            if previous.source == "thunderstore" and archive is not None:
                import json
                import zipfile
                from Thunderstore.thunderstore_meta import build_meta_from_link
                with zipfile.ZipFile(archive[1][0]) as bundle:
                    member = next(item for item in bundle.infolist() if item.filename.lower() == "manifest.json")
                    manifest = json.loads(bundle.read(member))
                dependencies = manifest.get("dependencies")
                if not isinstance(dependencies, list) or any(not isinstance(value, str) or not value for value in dependencies):
                    raise JobError("thunderstore_dependencies_unavailable", "The cached archive has invalid dependency metadata.")
                fresh = build_meta_from_link(link, {**manifest, "size": previous.file_size})
            result = {"available": True, "mod": mod, "profile": profile.name, "revision": properties["revision"],
                      "previous": asdict(previous), "archive_available": archive is not None,
                      "archive": {"id": archive[0]["id"], "name": archive[0]["name"]} if archive else None,
                      "preserves_current_name": True}
            if not apply:
                return result
            if archive is None:
                raise JobError("mod_rollback_archive_missing", "Download the recorded previous archive before rolling back.", result)
            metadata.installation_file = archive[0]["name"]
            if verify_dependencies is not None:
                verify_dependencies()
            result = _apply_change(game, profile, mod, properties["revision"], options, context, metadata, archive,
                                   archive_reader, verify_source, {"operation": "rollback", "previous": asdict(previous)},
                                   log, progress, thunderstore_meta=fresh, verify_dependencies=verify_dependencies)
            result["rollback"] = True
            return result
    return run
