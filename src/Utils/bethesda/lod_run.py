from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from .tool_run import ToolRunCancelled, ToolRunError


def executable(game, choice):
    from .xedit_run import executable as xedit_executable

    try:
        return xedit_executable(game, {**choice, "optional_64bit_exe": None})
    except ToolRunError as exc:
        if exc.name == "xedit_executable_missing":
            raise ToolRunError("lod_executable_missing", "Install the selected LOD executable first.", exc.details) from exc
        raise


def saved_options(game, choice, tool_id):
    from .xedit_run import saved_options as xedit_options
    from Utils.executables.launch import load_wizard_prefer_discrete_gpu

    settings = xedit_options(game, {**choice, "optional_64bit_exe": None}, False)
    settings.pop("use_64bit")
    settings["prefer_discrete_gpu"] = load_wizard_prefer_discrete_gpu(game, tool_id)
    return settings


def output_mod_name(staging, profile, name):
    from Utils.atomic_write import filename_limit
    from Utils.mods.modlist import read_modlist

    occupied = {row.name.casefold() for row in read_modlist(profile / "modlist.txt")}
    if staging.is_dir():
        occupied.update(path.name.casefold() for path in staging.iterdir())
    limit = filename_limit(staging)
    number = 1
    while True:
        suffix = f" ({number})" if number > 1 else ""
        base = name
        while len(os.fsencode(base + suffix)) > limit:
            base = base[:-1]
        candidate = base + suffix
        if candidate.casefold() not in occupied:
            return candidate
        number += 1


def validate_output_path(game, choice, output, prefix, *, direct_output=False):
    from Utils.wizards.applications import installation_target

    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ToolRunError("lod_output_unavailable", "Choose a new absolute LOD output folder.")
    protected = [game.get_game_path(), installation_target(game, choice), Path(prefix["compat_data"])]
    if direct_output:
        if output.parent.resolve() != Path(game.get_effective_mod_staging_path()).resolve():
            raise ToolRunError("unsafe_lod_output", "Generate LOD into a new mod folder in staging.")
    else:
        protected.extend([game.get_profile_root(), game.get_effective_mod_staging_path(),
                          game.get_effective_overwrite_path(), game.get_effective_root_folder_path()])
    for root in protected:
        if root is not None and output.resolve().is_relative_to(Path(root).resolve()):
            message = ("Mod staging must be outside the game, application and Wine prefix folders." if direct_output
                       else "Generate LOD outside game, profile, staging and prefix folders.")
            raise ToolRunError("unsafe_lod_output", message)
    for parent in output.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ToolRunError("unsafe_lod_output", "The LOD work folder has a link or file as a parent.")


def arguments(game, choice, output, compat_data, launch_args="", *, direct_output=False):
    from Utils.executables.arguments import XLODGEN_GAME_FLAGS
    from Utils.executables.launch import parse_launch_args
    from Utils.wine.paths import to_wine_path
    from Utils.wine.registry import normalize_pfx

    extra = parse_launch_args(launch_args)
    reserved = {"-d", "-o", "-p", "-m", *XLODGEN_GAME_FLAGS.values()}
    if any(arg.casefold().split(":", 1)[0].split("=", 1)[0] in reserved for arg in extra):
        raise ToolRunError("lod_argument_override", "LOD paths and game mode are supplied by the manager.")
    pfx = normalize_pfx(compat_data)
    data_path = to_wine_path(game.get_game_path() / "Data", pfx).rstrip("\\") + "\\"
    output_path = to_wine_path(output if direct_output else output / "files", pfx).rstrip("\\") + "\\"
    return [f'-d:{data_path}', f'-o:{output_path}', choice["game_flag"], *extra]


def sync_output_presets(exe, choice, output, prefix, *, log_fn):
    from Utils.atomic_write import write_atomic
    from Utils.wine.paths import to_wine_path

    if choice["tool"] not in {"texgen", "dyndolod"}:
        return
    config = exe.parent / "Edit Scripts" / "DynDOLOD"
    presets = config / "Presets"
    for parent in (config.parent, config, presets):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ToolRunError("unsafe_lod_preset", "The LOD preset folder is a link or file.", {"path": str(parent)})
    mode = choice["game_flag"].lstrip("-").upper()
    texgen = choice["tool"] == "texgen"
    stem = f"DynDOLOD_{mode}" + ("_TexGen" if texgen else "")
    defaults = {presets / f"{stem}.ini", presets / f"{stem}_Default.ini"}
    paths = set(defaults)
    if presets.is_dir():
        for path in presets.rglob("*"):
            name = path.name.casefold()
            if (path.suffix.casefold() == ".ini" and name.startswith(f"dyndolod_{mode.casefold()}_")
                    and ("_texgen" in name) == texgen):
                paths.add(path)
    paths.add(config / (f"TexGen_{mode}.ini" if texgen else f"DynDOLOD_{mode}.ini"))
    if texgen:
        paths.add(config / "TexGen.ini")
    wine_path = to_wine_path(output, Path(prefix["prefix_path"])).rstrip("\\") + "\\"
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(config.resolve()):
            raise ToolRunError("unsafe_lod_preset", "The LOD preset is a link or outside its application folder.", {"path": str(path)})
        if not path.exists():
            continue
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            encoding = "cp1252"
            text = raw.decode(encoding)
        pattern = re.compile(r"^([ \t]*OutputPath[ \t]*=[ \t]*)[^\r\n]*", re.IGNORECASE | re.MULTILINE)
        updated = pattern.sub(lambda match: match[1] + wine_path, text)
        if updated != text:
            try:
                encoded = updated.encode(encoding)
            except UnicodeEncodeError:
                encoded = updated.encode("utf-8-sig")
            write_atomic(path, encoded)
            log_fn(f"{choice['label']}: preset output updated: {path}")
    log_fn(f"{choice['label']}: generating output in {output} ({wine_path})")


def _output_files(root, checkpoint, *, direct_output=False):
    if root.is_symlink() or not root.is_dir():
        raise ToolRunError("unsafe_lod_output", "The generated output folder is a link or unavailable.")
    files = []
    seen = set()
    for current, directories, names in os.walk(root, followlinks=False):
        checkpoint()
        for name in directories + names:
            path = Path(current) / name
            folded = str(path.relative_to(root)).casefold()
            if folded in seen:
                raise ToolRunError("unsafe_lod_output", "Generated output has paths differing only in case.")
            seen.add(folded)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ToolRunError("unsafe_lod_output", "Generated output contains links or special files.")
            relative = path.relative_to(root)
            if direct_output and relative == Path("meta.ini"):
                continue
            if any(part.endswith((".", " ")) or ":" in part or "\\" in part for part in relative.parts):
                raise ToolRunError("unsafe_lod_output", "Generated output has an unsafe Windows path.")
            files.append(relative)
    if not files:
        raise ToolRunError("lod_output_empty", "No generated files were found in the selected output folder.",
                           {"reason": f"Expected output in {root}. Check the tool's Output Path after loading presets, and exit without zipping."})
    paths = [str(path).casefold() for path in files]
    if len(paths) != len(set(paths)):
        raise ToolRunError("unsafe_lod_output", "Generated output has paths differing only in case.")
    return files


def import_output(game, choice, output, mod_name, *, log_fn, checkpoint, progress_fn=lambda *_: None):
    from Utils.mods.install_as_mod import index_installed_mod
    from Utils.plugins.sync import sync_plugins_for_mods
    from Utils.wizards.payload import PreparedPayload, _install_mod, archive_token, tree_identity

    progress_fn(0, 1, "validate_lod_output")
    if output.is_symlink():
        raise ToolRunError("unsafe_lod_output", "The generated output folder is a link.")
    root = output / "files"
    files = _output_files(root, checkpoint)
    source_identity = tree_identity(root, checkpoint)
    staging = Path(game.get_effective_mod_staging_path()).resolve()
    profile = game._active_profile_dir
    mod_name = output_mod_name(staging, profile, mod_name)
    marker = output / "generation.json"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": choice["tool"], "mod": mod_name}))

    class GeneratedPayload(PreparedPayload):
        def check_archive(self):
            super().check_archive()
            if tree_identity(self.root, checkpoint) != source_identity:
                raise ToolRunError("lod_output_changed", "The generated output changed during import.")

    payload = GeneratedPayload(marker, root, tuple(sorted(files)), archive_token(marker))
    target = staging / mod_name
    log_fn(f"{choice['label']}: importing generated output as '{mod_name}'.")
    progress_fn(0, 1, "import_lod_output")
    _install_mod(payload, game, target, mod_name, profile / "modlist.txt", log_fn, checkpoint,
                 record_source=False, root_folder=False, replace_existing=False)
    try:
        progress_fn(0, 1, "index_lod_output")
        index_installed_mod(game, mod_name, log_fn=log_fn, strict=True)
        progress_fn(0, 1, "sync_lod_plugins")
        sync_plugins_for_mods(game, profile, target.parent, [(mod_name, True)], log_fn=log_fn)
    except Exception as exc:
        raise ToolRunError("lod_output_sync_failed", "The output mod is installed but indexing or plugin synchronization failed.",
                           {"mod": mod_name, "path": str(target), "reason": str(exc), "state_persisted": True}) from exc
    retained = False
    try:
        shutil.rmtree(output)
    except OSError as exc:
        retained = True
        log_fn(f"LOD output imported; work folder retained at {output}: {exc}")
    return {"output_mod": {"name": mod_name, "path": str(target), "enabled": True},
            "output_path": str(output), "output_retained": retained, "file_count": len(files),
            "needs_deployment": True}


def finish_staged_output(game, output, mod_name, *, log_fn, checkpoint, progress_fn):
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral
    from Utils.mods.modlist import ensure_mod_preserving_position, read_modlist
    from Utils.plugins.sync import sync_plugins_for_mods

    progress_fn(0, 1, "validate_lod_output")
    files = _output_files(output, checkpoint, direct_output=True)
    modlist = game._active_profile_dir / "modlist.txt"
    row = next((row for row in read_modlist(modlist) if row.name == mod_name), None)
    if row is None or row.enabled:
        raise ToolRunError("lod_output_changed", "The unfinished output mod's state changed during generation.")
    try:
        register_as_mod_neutral(game, mod_name, modlist_path=modlist, log_fn=log_fn, root_folder=False, enabled=False)
        progress_fn(0, 1, "index_lod_output")
        index_installed_mod(game, mod_name, log_fn=log_fn, strict=True)
        checkpoint()
        progress_fn(0, 1, "sync_lod_plugins")
        sync_plugins_for_mods(game, game._active_profile_dir, output.parent, [(mod_name, True)], log_fn=log_fn)
        ensure_mod_preserving_position(modlist, mod_name, enabled=True, preserve_existing_state=False)
    except ToolRunCancelled:
        raise
    except Exception as exc:
        raise ToolRunError("lod_output_sync_failed", "Generated output was retained disabled because indexing or registration failed.",
                           {"mod": mod_name, "path": str(output), "reason": str(exc), "state_persisted": True}) from exc
    log_fn(f"LOD generation finished; enabled '{mod_name}' at {output}.")
    return {"output_mod": {"name": mod_name, "path": str(output), "enabled": True},
            "output_path": str(output), "output_retained": True, "direct_output": True,
            "file_count": len(files), "needs_deployment": True}


def run_lod(game, choice, exe, settings, output, mod_name, *, cancel=None, prefix=None,
            before_write=lambda: None, before_import=lambda: None, import_checkpoint=lambda: None,
            direct_output=False, **kwargs):
    from .tool_run import run_bethesda_tool
    from .xedit_run import preview_prefix
    from Utils.wizards.textures import apply_discrete_gpu_environment, apply_texture_dxvk_environment

    log = kwargs.get("log_fn", lambda _m: None)
    prefix = prefix or preview_prefix(game, exe, settings["proton_name"], settings["prefix_mode"])
    output_identity = None

    def prepare():
        nonlocal output_identity
        before_write()
        validate_output_path(game, choice, output, prefix, direct_output=direct_output)
        if direct_output and output.name != mod_name:
            raise ToolRunError("unsafe_lod_output", "The output folder must match the new mod name.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        value = output.stat()
        output_identity = value.st_dev, value.st_ino
        if direct_output:
            from Utils.mods.modlist import ensure_mod_preserving_position
            ensure_mod_preserving_position(game._active_profile_dir / "modlist.txt", mod_name,
                                           enabled=False, preserve_existing_state=False)
            log(f"{choice['label']}: reserved disabled output mod '{mod_name}' at {output}.")
        else:
            (output / "files").mkdir()

    def environment(env):
        sync_output_presets(exe, choice, output if direct_output else output / "files", prefix, log_fn=log)
        if choice["tool"] in ("texgen", "dyndolod"):
            apply_texture_dxvk_environment(env)
            log(f"GPU: {apply_discrete_gpu_environment(env, settings['prefer_discrete_gpu'])}")

    def complete(_result):
        value = output.lstat()
        if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != output_identity:
            raise ToolRunError("lod_output_changed", "The LOD work folder changed during the run.")
        before_import()
        if direct_output:
            return finish_staged_output(game, output, mod_name, log_fn=log, checkpoint=import_checkpoint,
                                        progress_fn=kwargs.get("progress_fn", lambda *_: None))
        return import_output(game, choice, output, mod_name, log_fn=log, checkpoint=import_checkpoint,
                             progress_fn=kwargs.get("progress_fn", lambda *_: None))

    try:
        result = run_bethesda_tool(game, choice, exe, settings["proton_name"], settings["prefix_mode"],
                                family="lod", cancel=cancel, prefix=prefix, before_write=prepare,
                                launch_args=settings["launch_args"], launch_env=settings["launch_env"],
                                winetricks_style=settings["winetricks_style"],
                                resources=(f"lod-output:{output.resolve()}", f"lod-application:{exe.parent.resolve()}"),
                                build_arguments=lambda compat: arguments(game, choice, output, compat, settings["launch_args"],
                                                                         direct_output=direct_output),
                                prepare_environment=environment, complete=complete, **kwargs)
        return {**result, "direct_output": direct_output}
    except Exception as exc:
        if not hasattr(exc, "details"):
            exc.details = {}
        exc.details.update(output_path=str(output), output_retained=output_identity is not None and output.exists(),
                           direct_output=direct_output)
        if output_identity is not None:
            exc.details["state_persisted"] = True
        raise
