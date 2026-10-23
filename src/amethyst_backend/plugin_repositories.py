from __future__ import annotations

from pathlib import Path

from .jobs import JobError, _validated_archive_path


def version_catalogue(game, context, log):
    from amethyst_plugins.repositories import catalogue
    catalog = catalogue(game, context["plugin_source"], log)
    return {**catalog, "revision": context["properties"]["revision"],
            "mod": context["requested_mod"], "profile": context["requested_profile"].name,
            "ownership": context["properties"]["affected_profiles"]}


def retained_archive(game, profile, artifact, archive_id=None, downloaded=None):
    from Utils.downloads.inventory import build_inventory
    from .download_archives import _token
    inventory = build_inventory(game, profile)
    matches = [item for item in inventory["archives"] if all((item.get("provider") or {}).get(key) == artifact.get(key)
               for key in ("plugin_id", "provider_id", "identity", "sha256"))]
    if archive_id is not None:
        selected = next((item for item in matches if item["id"] == archive_id), None)
    elif downloaded is not None:
        selected = next((item for item in matches if Path(item["path"]) == Path(downloaded)), None)
    else:
        selected = matches[0] if len(matches) == 1 else None
    if selected is None:
        raise JobError("plugin_archive_unavailable", "Choose a retained archive matching this provider artifact.")
    path = _validated_archive_path(inventory, selected)
    paths = [path, path.with_suffix(path.suffix + ".plugin.json")]
    return selected, paths, [_token(item, required=True) for item in paths]


def change_version(game, profile, mod, revision, context, version, options, log, progress):
    from amethyst_plugins.downloads import resolve, transfer
    from amethyst_plugins.repositories import installed
    from Nexus.nexus_meta import merge_reinstall_metadata
    from .mod_versions import _apply_change
    catalog = version_catalogue(game, context, log)
    if options.get("catalogue_revision") != catalog["catalogue_revision"]:
        raise JobError("stale_plugin_catalogue", "The provider catalogue changed; review its versions again.")
    candidate = next((row for row in catalog["versions"] if row["id"] == version), None)
    if candidate is None:
        raise JobError("mod_version_not_found", "The selected provider version is no longer available.")
    record = context["plugin_source"]
    plan = resolve(game, record["plugin_id"], record["provider_id"], candidate["source"], log, None)
    if plan.descriptor["identity"] != version or plan.descriptor["metadata"].get("version") != candidate["version"]:
        raise JobError("plugin_version_mismatch", "The resolver artifact does not match the selected catalogue version.")
    def dependencies():
        from amethyst_plugins.dependencies import graph, missing
        root = {"plugin_id": record["plugin_id"], "provider_id": record["provider_id"], "source": candidate["source"]}
        required = graph(game, [root], log)[:-1] + candidate.get("dependencies", [])
        absent = missing(game, context["target_profile"], required, log)
        if absent:
            raise JobError("plugin_dependencies_required", "Install or enable the required provider dependencies first.", {"dependencies": absent})
    dependencies()
    from Utils.downloads.inventory import build_inventory
    candidates = [item for item in build_inventory(game, profile)["archives"] if all(
        (item.get("provider") or {}).get(key) == value for key, value in {
            "plugin_id": record["plugin_id"], "provider_id": record["provider_id"],
            "identity": version}.items())
        and item["size_bytes"] == plan.descriptor["size_bytes"]
        and (not plan.descriptor["sha256"] or item["provider"]["sha256"] == plan.descriptor["sha256"])]
    if options.get("archive_id"):
        candidates = [item for item in candidates if item["id"] == options["archive_id"]]
        if not candidates:
            raise JobError("plugin_archive_unavailable", "The selected archive does not match this provider version.")
    if len(candidates) == 1:
        from amethyst_plugins.downloads import read_source
        artifact = read_source(candidates[0]["path"], verify=True)
        archive = retained_archive(game, profile, artifact, candidates[0]["id"])
    else:
        downloaded = transfer(game, plan, log, progress, None)
        artifact = downloaded["provider"]
        archive = retained_archive(game, profile, artifact, downloaded=downloaded["archive"]["path"])
    metadata = merge_reinstall_metadata(None, context["nexus_meta"])
    metadata.version = candidate["version"]
    metadata.installation_file = archive[0]["name"]
    def verify(_actual):
        if installed(context["destination"] / "meta.ini") != artifact:
            raise JobError("plugin_version_metadata_failed", "The installed provider identity could not be verified.")
    return _apply_change(game, profile, mod, revision, options, context, metadata, archive,
                         lambda archive_id: retained_archive(game, profile, artifact, archive_id),
                         verify, {"version_id": version}, log, progress, verify_dependencies=dependencies)
