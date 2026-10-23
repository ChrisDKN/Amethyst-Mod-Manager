from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import re


def entries(manifest):
    from amethyst_plugins.downloads import source_uri
    rows = []
    for index, mod in enumerate(manifest.get("mods", [])):
        source = mod.get("source") or {}
        if str(source.get("type") or "").strip().lower() != "plugin":
            continue
        request = {key: source.get(key) for key in ("plugin_id", "provider_id", "source")}
        if any(not isinstance(request[key], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", request[key]) for key in ("plugin_id", "provider_id")):
            raise ValueError("A collection provider source requires valid plugin and provider IDs.")
        source_uri(request["source"])
        if (not isinstance(source.get("identity"), str) or not 1 <= len(source["identity"]) <= 1000
                or not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                or type(source.get("fileSize")) is not int or source["fileSize"] <= 0):
            raise ValueError("Provider collection sources must pin identity, SHA-256 and fileSize.")
        rows.append({"array_index": index, "name": str(mod.get("name") or ""), "request": request,
                     "identity": source["identity"], "sha256": source["sha256"], "size_bytes": source["fileSize"],
                     "root_folder": bool(mod.get("rootFolder", False))})
    return rows


def acquire(manifest, game, profile, control, callbacks, ownership=None, *, download_only=False, skipped=()):
    from amethyst_plugins.dependencies import graph
    from amethyst_plugins.downloads import resolve, transfer
    from Utils.mods.install import install_collection_archive
    rows = [row for row in entries(manifest) if row["name"] not in skipped]
    if not rows:
        return [], []
    def key(request):
        return tuple(request[field] for field in ("plugin_id", "provider_id", "source"))
    pins = {key(row["request"]): row for row in rows}
    if len(pins) != len(rows):
        raise ValueError("A collection cannot include duplicate provider sources.")
    cancel = control.stop if control is not None else None
    log = callbacks.on_log
    ordered = graph(game, [row["request"] for row in rows], log, cancel)
    if any(key(request) not in pins for request in ordered):
        raise ValueError("Every provider dependency must have its own pinned collection entry.")
    plans = []
    for request in ordered:
        row = pins[key(request)]
        plan = resolve(game, **request, log=log, control=cancel)
        if plan.descriptor["identity"] != row["identity"] or plan.descriptor["size_bytes"] != row["size_bytes"]:
            raise ValueError(f"The collection artifact changed: {row['name']}")
        if plan.descriptor["sha256"] and plan.descriptor["sha256"] != row["sha256"]:
            raise ValueError(f"The collection checksum changed: {row['name']}")
        plans.append((row, replace(plan, descriptor={**plan.descriptor, "sha256": row["sha256"]})))
    order, failures = [], []
    for row, plan in plans:
        callbacks.on_status(f"Provider: {row['name']}")
        result = transfer(game, plan, log, lambda done, total, phase=None: None, cancel)
        if result["provider"]["sha256"] != row["sha256"]:
            raise ValueError(f"Provider archive checksum mismatch: {row['name']}")
        if download_only:
            continue
        folder = install_collection_archive(result["archive"]["path"], game, Path(profile),
                    log_fn=log, preferred_name=row["name"], cancel=cancel, skip_index_update=False)
        if not folder:
            failures.append(f"Provider mod failed to install: {row['name']}")
            break
        from Utils.mods.copy import resolve_target_staging
        from Nexus.nexus_meta import set_meta_keys
        set_meta_keys(Path(resolve_target_staging(game, Path(profile))) / folder / "meta.ini",
                      {"rootFolder": "true" if row["root_folder"] else "false"})
        if ownership is not None:
            ownership.track(folder)
        order.append((row["array_index"], folder))
    return order, failures
