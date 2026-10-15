import subprocess
from pathlib import Path


EXE_NAME = "SkyrimTogether.exe"
RELATIVE_EXE = Path("Data/SkyrimTogetherReborn") / EXE_NAME


def is_launcher(game, exe: Path) -> bool:
    return (getattr(game, "game_id", "") == "skyrim_se"
            and Path(exe).name.casefold() == EXE_NAME.casefold())


def find_launcher(game) -> Path | None:
    if getattr(game, "game_id", "") != "skyrim_se":
        return None
    root = game.get_game_path()
    if root is None:
        return None
    from Utils.filegraph.service import active_snapshot, source_path
    snapshot = active_snapshot(game)
    winner = snapshot.winner_by_suffix(EXE_NAME)
    if (winner is not None and winner.target == "game"
            and is_launcher(game, Path(winner.destination))):
        if source_path(game, winner.mod_name, winner.source_rel).is_file():
            return Path(root) / winner.destination
    if snapshot.raw_files_by_basename((EXE_NAME.casefold(),)):
        return None
    from Utils.executables.launch import resolve_deployed_exe
    return resolve_deployed_exe(game, Path(root) / RELATIVE_EXE)


def resolve_launcher(game, selected: Path) -> Path:
    from Utils.executables.launch import resolve_deployed_exe
    candidate = find_launcher(game)
    if candidate is not None:
        deployed = resolve_deployed_exe(game, candidate)
        if deployed is None:
            raise RuntimeError("Deploy Skyrim Together before launching it.")
        return deployed
    staging = game.get_effective_mod_staging_path()
    if staging is not None and selected.is_relative_to(staging):
        raise RuntimeError("Enable and deploy Skyrim Together in the active profile first.")
    return selected


def launch_args(game, args: list[str]) -> list[str]:
    if "--exePath" in args:
        return args
    from Utils.executables.launch import resolve_deployed_exe
    root = game.get_game_path()
    target = resolve_deployed_exe(game, Path(root) / "SkyrimSE.exe") if root else None
    if target is None:
        raise RuntimeError("SkyrimSE.exe was not found in the active game folder.")
    # Keep the logical path: resolving symlinks would bypass the deployed view.
    return ["--exePath", "Z:" + str(target).replace("/", "\\"), *args]


def prepare_compatibility(proton_script, env, *, exe_name=EXE_NAME,
                          umu_bin=None, log_fn):
    args = ["reg", "add", rf"HKCU\Software\Wine\AppDefaults\{exe_name}",
            "/v", "Version", "/t", "REG_SZ", "/d", "win10", "/f"]
    if umu_bin is not None:
        from Utils.launchers.lutris import umu_run_command
        command = umu_run_command(umu_bin, args[0], env=env) + args[1:]
    else:
        from Utils.launchers.steam import proton_run_command
        command = proton_run_command(proton_script, "run", *args, env=env)
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=60)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(f"Could not set Skyrim Together Windows 10 compatibility: {detail}")
    log_fn("Skyrim Together: Windows 10 compatibility set for its launcher.")
