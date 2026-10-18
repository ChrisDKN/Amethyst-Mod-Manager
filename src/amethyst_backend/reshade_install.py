from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _destination_state, _selected_game, _token


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("ReShade installation cancelled.")


def _digest(path, cancel):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _checkpoint(cancel)
            value.update(chunk)
    return value.hexdigest()


def _source(raw, kind, cancel):
    from Utils.wizards.reshade import (
        MAX_DLL_BYTES, MAX_SHADER_TREE_BYTES, OPTIONAL_SHADER_PACKS,
        RESHADE_BASE_URL, SHADER_BASE_URL,
    )

    path = Path(raw).expanduser()
    suffix = ".dll" if kind == "reshade_dll" else ".zip"
    if not path.is_absolute() or path.suffix.lower() != suffix:
        raise JobError("invalid_reshade_source", "Choose an absolute acquired ReShade file path.")
    sidecar = path.with_suffix(suffix + ".reshade.json")
    limit = MAX_DLL_BYTES if kind == "reshade_dll" else MAX_SHADER_TREE_BYTES
    try:
        info, meta_info = path.lstat(), sidecar.lstat()
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit
                or not stat.S_ISREG(meta_info.st_mode) or not 0 < meta_info.st_size <= 65536):
            raise ValueError("The acquired file or identity is unsafe or too large.")
        before = (_token(path), _token(sidecar))
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        digest = _digest(path, cancel)
        if before != (_token(path), _token(sidecar)):
            raise ValueError("The acquired file changed during inspection.")
        if (not isinstance(metadata, dict) or metadata.get("version") != 1
                or metadata.get("kind") != kind or metadata.get("sha256") != digest
                or metadata.get("size_bytes") != info.st_size):
            raise ValueError("The acquired file does not match its identity.")
        if kind == "reshade_dll":
            version = metadata.get("reshade_version")
            architecture = metadata.get("architecture")
            if (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
                    or architecture not in (32, 64) or isinstance(architecture, bool)
                    or metadata.get("source_url") != f"{RESHADE_BASE_URL}ReShade_Setup_{version}_Addon.exe"):
                raise ValueError("The DLL identity is not an official full add-on choice.")
        else:
            indices = metadata.get("pack_indices")
            if (metadata.get("base_url") != SHADER_BASE_URL or not isinstance(indices, list)
                    or any(type(index) is not int or not 0 <= index < len(OPTIONAL_SHADER_PACKS)
                           for index in indices) or indices != sorted(set(indices))
                    or metadata.get("packs") != [
                        {"index": index, "label": OPTIONAL_SHADER_PACKS[index][0],
                         "url": OPTIONAL_SHADER_PACKS[index][1],
                         "subfolder": OPTIONAL_SHADER_PACKS[index][2]}
                        for index in indices]):
                raise ValueError("The shader identity has invalid pack choices.")
        return path, metadata, before
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise JobError("reshade_source_mismatch", "The acquired ReShade file or identity is unavailable or changed.",
                       {"path": str(path), "reason": str(exc)}) from exc


def _materialize(root, dll, bundle, dll_name, mode, metadata, cancel):
    from Utils.mods.names import sanitize_mod_folder_name
    from Utils.wizards.reshade import BUNDLED_INI_PATH, MAX_SHADER_FILE_BYTES, MAX_SHADER_TREE_BYTES

    preset_source = metadata.get("preset_name")
    preset_hash = metadata.get("preset_sha256")
    if (preset_source is None) != (preset_hash is None):
        raise JobError("reshade_source_mismatch", "The shader bundle has inconsistent preset metadata.")
    if preset_source is not None and (not isinstance(preset_source, str)
            or Path(preset_source).name != preset_source or not preset_source.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", preset_hash)):
        raise JobError("reshade_source_mismatch", "The shader bundle preset identity is invalid.")
    preset_name = (sanitize_mod_folder_name(Path(preset_source).stem) + ".ini"
                   if preset_source is not None else "ReShadePreset.ini")
    if preset_name.casefold() == "reshade.ini":
        raise JobError("invalid_reshade_preset", "The preset name conflicts with ReShade.ini.")

    files = []
    total = 0
    names = set()
    with zipfile.ZipFile(bundle) as archive:
        for entry in archive.infolist():
            _checkpoint(cancel)
            if entry.is_dir():
                continue
            relative = Path(entry.filename)
            mode_bits = (entry.external_attr >> 16) & 0o170000
            if (relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename
                    or mode_bits not in (0, stat.S_IFREG)
                    or entry.file_size > MAX_SHADER_FILE_BYTES
                    or (relative.parts[0] != "reshade-shaders" and relative != Path("preset.ini"))
                    or len(relative.parts) < 2 and relative != Path("preset.ini")):
                raise JobError("unsafe_reshade_bundle", "The shader bundle contains an unsafe entry.",
                               {"entry": entry.filename})
            key = relative.as_posix().casefold()
            if key in names:
                raise JobError("unsafe_reshade_bundle", "The shader bundle contains duplicate paths.")
            names.add(key)
            total += entry.file_size
            if total > MAX_SHADER_TREE_BYTES or len(names) > 50000:
                raise JobError("unsafe_reshade_bundle", "The shader bundle exceeds install limits.")
            output = root / (preset_name if relative == Path("preset.ini") else relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, output.open("xb") as target:
                written = 0
                while chunk := source.read(1024 * 1024):
                    _checkpoint(cancel)
                    written += len(chunk)
                    if written > entry.file_size:
                        raise JobError("unsafe_reshade_bundle", "A shader entry exceeded its declared size.")
                    target.write(chunk)
            if written != entry.file_size:
                raise JobError("unsafe_reshade_bundle", "A shader entry was incomplete.")
            files.append(output.relative_to(root))
    if not any(item.parts[0] == "reshade-shaders" for item in files):
        raise JobError("empty_reshade_shaders", "The bundle has no shader files.")
    if len(files) != metadata.get("file_count"):
        raise JobError("reshade_source_mismatch", "The shader bundle file count changed.")
    preset = root / preset_name
    if preset_source is not None:
        if not preset.is_file() or _digest(preset, cancel) != preset_hash:
            raise JobError("reshade_source_mismatch", "The bundled preset does not match its identity.")
    elif "preset.ini" in names:
        raise JobError("reshade_source_mismatch", "The shader bundle has an undeclared preset.")
    else:
        preset.touch()
        files.append(Path(preset_name))
    if not BUNDLED_INI_PATH.is_file():
        raise JobError("reshade_configuration_missing", "The bundled ReShade.ini is unavailable.")
    ini = root / "ReShade.ini"
    shutil.copy2(BUNDLED_INI_PATH, ini)
    if preset_name != "ReShadePreset.ini":
        contents = ini.read_text(encoding="utf-8")
        default = "PresetPath=.\\ReShadePreset.ini"
        if default not in contents:
            raise JobError("reshade_configuration_missing", "The bundled ReShade.ini has no default preset path.")
        ini.write_text(contents.replace(default, "PresetPath=.\\" + preset_name, 1), encoding="utf-8")
    files.append(Path("ReShade.ini"))
    shutil.copy2(dll, root / dll_name)
    files.append(Path(dll_name))
    if mode != "game":
        (root / "ReShade.log").touch()
        files.append(Path("ReShade.log"))
    return tuple(sorted(files)), preset_name


@contextmanager
def _prepared(game, profile, dll_path, shader_path, dll_name, mode, cancel):
    from Utils.wizards.payload import PreparedPayload, archive_token
    from Utils.wizards.reshade import API_CHOICES

    if dll_name not in {name for _label, name in API_CHOICES}:
        raise JobError("invalid_reshade_dll", "Choose a ReShade proxy DLL from the options API.")
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the game before installing ReShade.")
    game_path = selected.get_game_path()
    subdir = selected.reshade_install_subdir(game_path)
    subdir = Path(subdir) if subdir is not None else Path(".")
    if subdir.is_absolute() or any(part in ("..", "") for part in subdir.parts):
        raise JobError("invalid_reshade_destination", "The game's ReShade install folder is unsafe.")
    dll, dll_meta, dll_state = _source(dll_path, "reshade_dll", cancel)
    bundle, shader_meta, shader_state = _source(shader_path, "reshade_shaders", cancel)
    with tempfile.TemporaryDirectory(prefix="amethyst-reshade-install-") as raw:
        root = Path(raw)
        install_root = root / subdir
        install_root.mkdir(parents=True, exist_ok=True)
        files, preset_name = _materialize(install_root, dll, bundle, dll_name, mode,
                                           shader_meta, cancel)
        if (dll_state != (_token(dll), _token(dll.with_suffix(".dll.reshade.json")))
                or shader_state != (_token(bundle), _token(bundle.with_suffix(".zip.reshade.json")))):
            raise JobError("reshade_source_changed", "An acquired ReShade file changed during preparation.")
        payload = PreparedPayload(dll, root, tuple(sorted(subdir / path for path in files)),
                                  archive_token(dll), replace_trees=(subdir / "reshade-shaders",),
                                  keep_existing=tuple(subdir / name for name in (
                                      (["ReShadePreset.ini"] if shader_meta.get("preset_name") is None else [])
                                      + (["ReShade.log"] if mode != "game" else []))))
        yield selected, payload, bundle, dll_meta, shader_meta, dll_state, shader_state, preset_name


def _plan(selected, payload, bundle, dll_meta, shader_meta, dll_state, shader_state,
          preset_name, profile, mode, mod_name, dll_name, cancel):
    target, label, name, records, overwritten, existing = _destination_state(
        selected, payload, mode, cancel, 0, mod_name_override=mod_name)
    if bundle.is_relative_to(target):
        raise JobError("reshade_source_in_destination", "Move the shader bundle outside the destination.")
    source = {"dll": {"path": str(payload.archive), "identity": dll_state,
                       "sha256": dll_meta["sha256"], "architecture": dll_meta["architecture"]},
              "shaders": {"path": str(bundle), "identity": shader_state,
                          "sha256": shader_meta["sha256"],
                          "missing_effects": shader_meta.get("missing_effects", [])}}
    snapshot = {
        "game": str(selected.game_id), "profile": profile.name, "mode": mode,
        "mod_name": name, "dll_name": dll_name, "preset_name": preset_name,
        "source": source, "destination": str(target), "files": [str(p) for p in payload.files],
        "replace_trees": [str(path) for path in payload.replace_trees],
        "keep_existing": [str(path) for path in payload.keep_existing],
        "configuration_state": {
            "reshade_ini_sha256": hashlib.sha256(next(
                (payload.root / path).read_bytes() for path in payload.files
                if path.name == "ReShade.ini")).hexdigest(),
            "paths": _token(selected._paths_file),
            "profile": _token(profile / "profile_state.json"),
            "legacy_profile": _token(profile / "profile_settings.json"),
        }, "destination_state": records,
    }
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {
        "revision": revision, "game": str(selected.game_id), "profile": profile.name,
        "mode": mode, "dll_name": dll_name, "preset_name": preset_name,
        "sources": source,
        "destination": {"path": str(target), "label": label, "mod_name": name,
                        "exists": target.exists(), "existing_entries": existing,
                        "overwrites": overwritten[:100], "overwrite_count": len(overwritten),
                        "replaces_trees": [str(path) for path in payload.replace_trees],
                        "preserves": [str(path) for path in payload.keep_existing if mode != "mod"
                                      and records.get(str(target / path)) is not None
                                      and stat.S_ISREG(records[str(target / path)][2])]},
        "file_count": len(payload.files), "restore_before_install": mode == "game",
        "wine_override": f"{Path(dll_name).stem}=native,builtin",
        "shader_bundle_retained": True, "dll_retained": True,
    }


def inspect_runner(game, profile, dll_path, shader_path, dll_name, mode, mod_name, cancel):
    def run(log, progress):
        from Utils.wizards.payload import PayloadInstallError

        progress(0, 1, "inspect_reshade")
        try:
            with _prepared(game, profile, dll_path, shader_path, dll_name, mode, cancel) as parts:
                result = _plan(*parts, profile, mode, mod_name, dll_name, cancel)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise JobError("reshade_inspection_failed", "Could not inspect ReShade installation.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, "inspect_reshade")
        return result
    return run


def install_runner(game, profile, dll_path, shader_path, dll_name, mode,
                   mod_name, revision, replace_existing, cancel):
    def run(log, progress):
        from Utils.wizards.payload import PayloadInstallError, install_prepared_payload

        restore_started = False

        def checkpoint():
            if cancel.is_set():
                details = {"restore_attempted": True, "state_persisted": True} if restore_started else {}
                raise JobCancelled("ReShade installation cancelled.", details)

        def mark_restore():
            nonlocal restore_started
            restore_started = True

        progress(0, 1, "prepare_reshade")
        try:
            with _prepared(game, profile, dll_path, shader_path, dll_name, mode, cancel) as parts:
                selected, payload, bundle, _dll_meta, _shader_meta, dll_state, shader_state, _preset = parts
                configuration_paths = (selected._paths_file, profile / "profile_state.json",
                                       profile / "profile_settings.json")
                configuration_state = tuple(_token(path) for path in configuration_paths)

                def source_checkpoint():
                    checkpoint()
                    if (dll_state != (_token(payload.archive), _token(payload.archive.with_suffix(".dll.reshade.json")))
                            or shader_state != (_token(bundle), _token(bundle.with_suffix(".zip.reshade.json")))):
                        raise JobError("reshade_source_changed", "An acquired ReShade file changed; inspect again.")
                    if configuration_state != tuple(_token(path) for path in configuration_paths):
                        raise JobError("stale_reshade_plan", "Game or profile configuration changed during installation.")

                def verify_plan():
                    source_checkpoint()
                    current = _plan(*parts, profile, mode, mod_name, dll_name, cancel)
                    if current["revision"] != revision:
                        raise JobError("stale_reshade_plan", "The ReShade source or destination changed; inspect again.")
                    if mode == "mod" and current["destination"]["exists"] and not replace_existing:
                        raise JobError("reshade_mod_already_exists", "Confirm replacement of the existing managed mod.")
                    return current

                current = verify_plan()
                progress(0, 1, "install_reshade")
                synced_groups = []

                def sync_groups():
                    if mode != "mod":
                        return
                    from Utils.profiles.groups import materialize_group, member_of_groups

                    failures = []
                    try:
                        group_names = member_of_groups(game, profile.name)
                    except Exception as exc:
                        group_names = []
                        failures.append({"profile": None, "reason": str(exc)})
                    for group_name in group_names:
                        group_dir = profile.parent / group_name
                        try:
                            if group_dir.is_symlink() or not group_dir.is_dir():
                                raise OSError("The profile group is unavailable or unsafe.")
                            materialize_group(_selected_game(game, group_dir), group_dir, log_fn=log)
                            synced_groups.append(group_name)
                        except Exception as exc:
                            failures.append({"profile": group_name, "reason": str(exc)})
                    if failures:
                        raise JobError("profile_group_refresh_failed",
                                       "ReShade was installed but a profile group could not be refreshed.",
                                       {"mod": current["destination"]["mod_name"],
                                        "synced_groups": synced_groups, "failures": failures,
                                        "state_persisted": True})

                label, count, name = install_prepared_payload(
                    selected, payload, mode, mod_fallback_name="ReShade",
                    mod_name_override=mod_name, modlist_path=profile / "modlist.txt",
                    restore_first=True, delete_archive=False, log_fn=log,
                    checkpoint=source_checkpoint, before_write=verify_plan,
                    on_restore_start=mark_restore, after_write=sync_groups, strict_index=True,
                    record_source=False)
        except PayloadInstallError as exc:
            if restore_started:
                exc.details.update(restore_attempted=True, state_persisted=True)
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled) as exc:
            if restore_started:
                exc.details.update(restore_attempted=True, state_persisted=True)
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise JobError("reshade_install_failed", "Could not install ReShade.",
                           {"reason": str(exc), "restore_attempted": restore_started,
                            "state_persisted": restore_started}) from exc
        progress(1, 1, "install_reshade")
        return {"ok": True, "game": str(game.game_id), "profile": profile.name,
                "destination": {"path": current["destination"]["path"],
                                "label": label, "mod_name": name,
                                "replaced_trees": current["destination"]["replaces_trees"],
                                "preserved": current["destination"]["preserves"]},
                "file_count": count, "restore_attempted": mode == "game",
                "wine_override": current["wine_override"],
                "synced_groups": synced_groups,
                "dll_retained": True, "shader_bundle_retained": True}
    return run
