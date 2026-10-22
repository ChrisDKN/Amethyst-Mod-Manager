from __future__ import annotations

from pathlib import Path

from .jobs import JobError


def local_archive(value):
    from .download_archives import _token
    from Utils.downloads.inventory import _opaque_id

    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise JobError("unsafe_archive", "Use an absolute path to a regular archive file.")
    _token(path, required=True)
    return {"id": _opaque_id("archive", path), "name": path.name, "path": str(path)}


def validate_options(params):
    from .service import ServiceError

    result = {}
    if "content_prefix" in params:
        value = params["content_prefix"]
        if (not isinstance(value, str) or len(value) > 4096 or "\\" in value
                or ":" in value or any(ord(c) < 32 for c in value)
                or value and any(p in {"", ".", ".."} for p in value.split("/"))):
            raise ServiceError(-32602, "invalid_params", "content_prefix must be an archive-relative directory or an empty string.")
        result["content_prefix"] = value
    for key in ("multi_mod_selected",):
        if key in params:
            value = params[key]
            if (not isinstance(value, list) or not 1 <= len(value) <= 4096
                    or any(not isinstance(name, str) or not name for name in value)
                    or len(set(value)) != len(value)):
                raise ServiceError(-32602, "invalid_params", f"{key} must list unique mod names.")
            result[key] = value
    if "multi_mod_names" in params:
        value = params["multi_mod_names"]
        if not isinstance(value, dict) or len(value) > 4096:
            raise ServiceError(-32602, "invalid_params", "multi_mod_names must map source names to destination names.")
        result["multi_mod_names"] = {}
        from Utils.mods.install import validate_mod_folder_name
        for key, name in value.items():
            try:
                result["multi_mod_names"][key] = validate_mod_folder_name(name)
            except (ValueError, TypeError) as exc:
                raise ServiceError(-32602, "invalid_params", "Invalid multi-mod destination name.") from exc
    if "multi_mod_policies" in params:
        value = params["multi_mod_policies"]
        if (not isinstance(value, dict) or len(value) > 4096
                or any(not isinstance(key, str) or not key or policy not in ("fail", "replace", "skip")
                       for key, policy in value.items())):
            raise ServiceError(-32602, "invalid_params", "multi_mod_policies must map source names to fail, replace or skip.")
        result["multi_mod_policies"] = value
    return result


def select_multi(prepared, options, reinstall=None):
    selected = options.get("multi_mod_selected")
    names = options.get("multi_mod_names", {})
    policies = options.get("multi_mod_policies", {})
    source = None
    if reinstall is not None:
        from Utils.mods.copy import resolve_target_staging
        from Utils.mods.metadata import read_meta_ini
        path = Path(resolve_target_staging(prepared.game, prepared.profile_dir)) / reinstall["target_mod"] / "meta.ini"
        _signature, parser, error = read_meta_ini(path)
        source = parser.get("General", "multiModSource", fallback=None)
    if prepared.is_bundle():
        from Utils.re_engine.bundle import detect_multi_mod
        multi = detect_multi_mod(prepared.bundle_root)
        if multi and (selected is not None or names or policies or
                      source is not None):
            prepared.multi_mods = multi
            prepared.bundle_layout = None
    if not prepared.is_multi_mod():
        if selected is not None or names or policies:
            raise JobError("installer_selection_mismatch", "Multi-mod choices require a multi-mod archive.")
        return
    available = dict(prepared.multi_mods)
    if reinstall is not None:
        source = source or reinstall["target_mod"]
        selected = [source]
        names = {source: reinstall["target_mod"]}
    if (set(selected or ()) | set(names) | set(policies)) - set(available):
        raise JobError("installer_selection_invalid", "Unknown multi-mod selection.", {"available": list(available)})
    prepared._multi_sources = {names.get(name, name): name for name in available}
    prepared._multi_policies = {names.get(name, name): policy for name, policy in policies.items()}
    prepared.multi_mods = [(names.get(name, name), path) for name, path in prepared.multi_mods
                          if (selected is None or name in selected) and policies.get(name) != "skip"]
    if not prepared.multi_mods:
        raise JobError("installer_selection_invalid", "Select at least one mod to install.")
    prepared._multi_explicit = selected is not None or bool(names or policies)


def layout_resolver(prefix=None, *, supplied=False, requests=None):
    def resolve(required, files, name):
        if supplied:
            return prefix
        if requests is not None:
            return requests.layout(required, files, name)
        raise JobError("installer_layout_required", "The archive needs a content directory; inspect it and supply content_prefix.",
                       {"mod": name, "required_directories": sorted(required),
                        "files": [destination for _source, destination, _folder in files[:100]]})
    return resolve
