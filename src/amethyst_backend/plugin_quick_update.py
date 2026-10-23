from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .jobs import JobCancelled, JobError


def plan(game, profile, mods, log):
    from .quick_update import _selection
    from .mod_properties import get_properties
    from amethyst_plugins.repositories import catalogue, installed, source_key, update_state
    names, selection_revision = _selection(profile, mods)
    items, skipped = [], []
    for name in names:
        properties = get_properties(game, profile, name, ignore_download_history=True)
        record = installed(properties["path"])
        if record is None:
            skipped.append({"mod": name, "reason": "not_plugin_source"})
            continue
        try:
            catalog = catalogue(game, record, log)
        except ValueError as exc:
            log(f"Provider unavailable for {name}: {exc}")
            skipped.append({"mod": name, "reason": "provider_unavailable"})
            continue
        candidate = next((row for row in catalog["versions"] if row["id"] == catalog["recommended"]), None)
        if candidate is None or candidate["id"] == record["identity"]:
            skipped.append({"mod": name, "reason": "up_to_date"})
            continue
        if update_state(Path(properties["path"])).get("ignore_update"):
            skipped.append({"mod": name, "reason": "ignored"})
            continue
        items.append({"mod": name, "source": source_key(record), "revision": properties["revision"],
                      "catalogue_revision": catalog["catalogue_revision"], "candidate": candidate, "installed": record})
    result = {"source": "plugins", "profile": profile.name, "selection_revision": selection_revision,
              "items": items, "skipped": skipped}
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def runner(game, profile, mods, revision=None, options=None, control=None):
    def run(log, progress):
        from Utils.operations import acquire_game_operation
        from Utils.deployment.locking import coordinated_game_mutation
        with (coordinated_game_mutation(game, "mods.quick_update") if revision is not None else acquire_game_operation(game, "mods.quick_update")):
            reviewed = plan(game, profile, mods, log)
            if revision is None:
                return reviewed
            if revision != reviewed["revision"]:
                raise JobError("stale_plugin_update", "The update plan changed; review it again.", {"plan": reviewed})
            selected = {row["mod"] for row in reviewed["items"] + reviewed["skipped"]}
            if set(options.get("per_mod", {})) - selected:
                raise JobError("invalid_plugin_update_options", "Per-mod options must target the selected mods.")
            results = []
            for index, row in enumerate(reviewed["items"]):
                if control.is_set():
                    raise JobCancelled("Provider update cancelled.", {"items": results})
                progress(index, len(reviewed["items"]), row["mod"])
                try:
                    if options.get("download_only"):
                        from amethyst_plugins.downloads import resolve, transfer
                        record = row["installed"]
                        resolved = resolve(game, record["plugin_id"], record["provider_id"], row["candidate"]["source"], log, control)
                        if resolved.descriptor["identity"] != row["candidate"]["id"]:
                            raise JobError("plugin_version_mismatch", "The provider returned another version.")
                        result = transfer(game, resolved, log, progress, control)
                    else:
                        from .mod_versions import change_runner
                        per_mod = {**options["installer_options"], **options.get("per_mod", {}).get(row["mod"], {}),
                                   "catalogue_revision": row["catalogue_revision"]}
                        result = change_runner(None, game, profile, row["mod"], row["revision"], None, per_mod,
                            source=row["source"], version=row["candidate"]["id"], operation_locked=True,
                            ignore_download_history=True)(log, progress)
                    results.append({"mod": row["mod"], "state": "succeeded", "result": result})
                except Exception as exc:
                    from Utils.operations import OperationCancelled
                    if isinstance(exc, OperationCancelled):
                        raise JobCancelled("Provider update cancelled.", {"items": results,
                            "remaining": [item["mod"] for item in reviewed["items"][index:]]}) from exc
                    results.append({"mod": row["mod"], "state": "failed", "error": {
                        "name": getattr(exc, "name", "plugin_update_failed"), "message": str(exc), "details": getattr(exc, "details", {})}})
                    raise JobError("plugin_update_failed", "Provider update stopped after a failure.", {
                        "items": results, "remaining": [item["mod"] for item in reviewed["items"][index + 1:]]}) from exc
            return {"ok": True, "items": results, "skipped": reviewed["skipped"]}
    return run
