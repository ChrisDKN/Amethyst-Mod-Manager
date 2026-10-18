from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from .tool_run import ToolRunError


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


def new_output_path():
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return home.resolve() / "AmethystModManager" / "lod-runs" / uuid.uuid4().hex


def validate_output_path(game, choice, output, prefix):
    from Utils.wizards.applications import installation_target

    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ToolRunError("lod_output_unavailable", "Choose a new absolute LOD work folder.")
    protected = [game.get_game_path(), game.get_profile_root(), game.get_effective_mod_staging_path(),
                 game.get_effective_overwrite_path(), game.get_effective_root_folder_path(),
                 installation_target(game, choice), Path(prefix["compat_data"])]
    for root in protected:
        if root is not None and output.resolve().is_relative_to(Path(root).resolve()):
            raise ToolRunError("unsafe_lod_output", "Generate LOD outside game, profile, staging and prefix folders.")
    for parent in output.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ToolRunError("unsafe_lod_output", "The LOD work folder has a link or file as a parent.")


def arguments(game, choice, output, compat_data, launch_args=""):
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
    output_path = to_wine_path(output / "files", pfx).rstrip("\\") + "\\"
    return [f'-d:{data_path}', f'-o:{output_path}', choice["game_flag"], *extra]


def import_output(game, choice, output, mod_name, *, log_fn, checkpoint):
    from Utils.mods.install_as_mod import index_installed_mod
    from Utils.plugins.sync import sync_plugins_for_mods
    from Utils.wizards.payload import PreparedPayload, _install_mod, archive_token, tree_identity

    root = output / "files"
    if root.is_symlink() or not root.is_dir() or output.is_symlink():
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
            if any(part.endswith((".", " ")) or ":" in part or "\\" in part for part in relative.parts):
                raise ToolRunError("unsafe_lod_output", "Generated output has an unsafe Windows path.")
            files.append(relative)
    if not files:
        raise ToolRunError("lod_output_empty", "The LOD tool produced no files; the previous mod is retained.")
    paths = [str(path).casefold() for path in files]
    if len(paths) != len(set(paths)):
        raise ToolRunError("unsafe_lod_output", "Generated output has paths differing only in case.")
    source_identity = tree_identity(root, checkpoint)
    marker = output / "generation.json"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": choice["tool"], "mod": mod_name}))

    class GeneratedPayload(PreparedPayload):
        def check_archive(self):
            super().check_archive()
            if tree_identity(self.root, checkpoint) != source_identity:
                raise ToolRunError("lod_output_changed", "The generated output changed during import.")

    payload = GeneratedPayload(marker, root, tuple(sorted(files)), archive_token(marker))
    target = Path(game.get_effective_mod_staging_path()).resolve() / mod_name
    profile = game._active_profile_dir
    _install_mod(payload, game, target, mod_name, profile / "modlist.txt", log_fn, checkpoint,
                 record_source=False, root_folder=False)
    try:
        index_installed_mod(game, mod_name, log_fn=log_fn, strict=True)
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


def run_lod(game, choice, exe, settings, output, mod_name, *, cancel=None, prefix=None,
            before_write=lambda: None, before_import=lambda: None, import_checkpoint=lambda: None, **kwargs):
    from .tool_run import run_bethesda_tool
    from .xedit_run import preview_prefix
    from Utils.wizards.textures import apply_discrete_gpu_environment, apply_texture_dxvk_environment

    log = kwargs.get("log_fn", lambda _m: None)
    prefix = prefix or preview_prefix(game, exe, settings["proton_name"], settings["prefix_mode"])
    output_identity = None

    def prepare():
        nonlocal output_identity
        before_write()
        validate_output_path(game, choice, output, prefix)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        value = output.stat()
        output_identity = value.st_dev, value.st_ino
        (output / "files").mkdir()

    def environment(env):
        if choice["tool"] in ("texgen", "dyndolod"):
            apply_texture_dxvk_environment(env)
            log(f"GPU: {apply_discrete_gpu_environment(env, settings['prefer_discrete_gpu'])}")

    def complete(_result):
        value = output.lstat()
        if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != output_identity:
            raise ToolRunError("lod_output_changed", "The LOD work folder changed during the run.")
        before_import()
        return import_output(game, choice, output, mod_name, log_fn=log, checkpoint=import_checkpoint)

    try:
        return run_bethesda_tool(game, choice, exe, settings["proton_name"], settings["prefix_mode"],
                                family="lod", cancel=cancel, prefix=prefix, before_write=prepare,
                                launch_args=settings["launch_args"], launch_env=settings["launch_env"],
                                winetricks_style=settings["winetricks_style"],
                                resources=(f"lod-output:{output.resolve()}",),
                                build_arguments=lambda compat: arguments(game, choice, output, compat, settings["launch_args"]),
                                prepare_environment=environment, complete=complete, **kwargs)
    except Exception as exc:
        if hasattr(exc, "details"):
            exc.details.update(output_path=str(output), output_retained=output_identity is not None and output.exists())
        raise
