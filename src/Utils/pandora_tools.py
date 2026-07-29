"""
Toolkit-neutral Pandora Behaviour Engine+ logic.

Ported from wizards/pandora.py so both the Tk wizard and the Qt wizard view
can share it (and so game files can gate the wizard on find_pandora_exe
without importing GUI code).

Pandora ships as a regular mod, so its exe lives under the mod staging folder.
It runs in a wizard-tool Wine prefix (see exe_launch.resolve_tool_prefix) with
the .NET 10 desktop runtime installed into that prefix.

install_net10 / run_pandora are blocking — call them from a worker thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from Utils.protontricks import dotnet_dep_key as _dotnet_dep_key

EXE_NAME = "Pandora Behaviour Engine+.exe"

# .NET 10 install now runs through Utils.proton_tools.install_dotnet_runtime
# (single source of truth for URL/filename/exit-code handling).
NET10_DEP_KEY = _dotnet_dep_key("10")


def _noop(_msg: str) -> None:
    pass


def find_pandora_exe(game: "BaseGame") -> Path | None:
    """Search the mod staging directory for Pandora Behaviour Engine+.exe.

    Uses the memory-cached modindex (with a disk-walk fallback) so gating the
    wizard stays fast on large modlists — see Utils.wizard_gates.find_staged_exe.
    """
    from Utils.wizard_gates import find_staged_exe
    return find_staged_exe(game, EXE_NAME)


def net10_installed(compat_data: Path) -> bool:
    """True when .NET 10 is already marked installed in this prefix."""
    from Utils.protontricks import is_dep_installed
    return is_dep_installed(Path(compat_data) / "pfx", NET10_DEP_KEY)


def install_net10(proton_script: Path, compat_data: Path, env: dict,
                  log_fn=_noop, status_fn=_noop) -> None:
    """Download (cached) + silently install the .NET 10 desktop runtime into
    the prefix, then mark the dep key. Raises on failure. Blocking.

    *status_fn* receives short user-facing progress strings; *log_fn* the
    detailed log lines.
    """
    from Utils.proton_tools import install_dotnet_runtime

    ok = install_dotnet_runtime(
        "10", proton_script, env, Path(compat_data) / "pfx",
        log_fn=log_fn, status_fn=status_fn, dep_key=NET10_DEP_KEY)
    if not ok:
        raise RuntimeError("Pandora .NET 10 install failed (see log).")


def run_pandora(exe: Path, game: "BaseGame", proton_script: Path,
                compat_data: Path, env: dict,
                log_fn=_noop, on_started=None) -> int:
    """Launch Pandora via Proton and wait for it to exit. Returns the exit
    code (stderr is logged). Blocking — call from a worker thread.

    *on_started* fires once the process has spawned (the UI can enable its
    Done button while Pandora runs). Shuts the prefix wineserver down after
    exit. When the winetricks-style marker is in *env* (Proton-step checkbox,
    via resolve_tool_prefix) the launch bypasses the proton script and runs
    plain Wine the way winetricks does — handled here rather than through
    run_tool_logged's delegation because the rebuilt environment must get
    Pandora's .NET/renderer tweaks re-applied; prefix prep (registry seed,
    Settings.json) still happens the same way.
    """
    from Utils.bethesda_registry import maybe_register_for_game
    from Utils.exe_args_builder import _bootstrap_pandora_settings
    from Utils.exe_launch import shutdown_prefix_wineserver
    from Utils.wine_paths import to_wine_path

    game_path = game.get_game_path()
    if game_path is None:
        raise RuntimeError("Game path not configured.")
    staging = game.get_effective_mod_staging_path()

    # Seed the Bethesda registry key in the fresh prefix so tools that look
    # the game path up via the registry keep working (idempotent).
    maybe_register_for_game(
        prefix_dir=compat_data,
        proton_script=proton_script,
        env=env,
        game=game,
        log_fn=log_fn,
    )

    # The output folder (<staging>/Pandora_output) is configured by rewriting
    # Pandora's Settings.json inside the prefix — newer Pandora builds ignore
    # the --output: CLI flag.
    _bootstrap_pandora_settings(
        getattr(game, "game_id", None),
        game_path,
        staging,
        compat_data,
        log_fn,
    )

    pfx = Path(compat_data) / "pfx"
    game_arg = f"--tesv:{to_wine_path(game_path, pfx)}"

    env = dict(env)
    # Unset .NET environment variables that can prevent Pandora from launching
    # when the host has a .NET runtime installed (e.g. via Bottles/MO2).
    env.pop("DOTNET_ROOT", None)
    env.pop("DOTNET_BUNDLE_EXTRACT_BASE_DIR", None)
    # WPF rendering over DXVK produces a double title bar / frame glitch in
    # Proton. Forcing the WineD3D GDI renderer bypasses the Vulkan path
    # entirely and gives a single, properly-decorated window.
    # PROTON_USE_WINED3D is required — WINE_D3D_CONFIG only takes effect when
    # WineD3D (not DXVK) is actually handling the d3d calls.
    env["PROTON_USE_WINED3D"] = "1"
    env["WINE_D3D_CONFIG"] = "renderer=gdi"

    from Utils.exe_launch import run_tool_logged, run_tool_winetricks_style
    log_fn(f"launching {exe} via Proton")
    log_fn(
        "  env: "
        f"PROTON_USE_WINED3D={env.get('PROTON_USE_WINED3D', '<unset>')} "
        f"WINE_D3D_CONFIG={env.get('WINE_D3D_CONFIG', '<unset>')} "
        f"STEAM_COMPAT_DATA_PATH={env.get('STEAM_COMPAT_DATA_PATH', '<unset>')}"
    )
    if on_started is not None:
        on_started()
    if env.get("AMM_WINETRICKS_STYLE") == "1":
        # The helper rebuilds env from the desktop environment, so re-apply
        # the launch-critical tweaks: drop the host .NET vars and keep the
        # WineD3D GDI renderer (bare wine has no DXVK session, so wined3d
        # handles d3d and WINE_D3D_CONFIG applies directly).
        rc = run_tool_winetricks_style(
            proton_script, exe, compat_data, log_fn=log_fn,
            extra_args=[game_arg] if game_arg else None,
            extra_env={
                "DOTNET_ROOT": None,
                "DOTNET_BUNDLE_EXTRACT_BASE_DIR": None,
                "WINE_D3D_CONFIG": "renderer=gdi",
            },
            label="Pandora")
    else:
        rc = run_tool_logged(proton_script, exe, env, log_fn=log_fn,
                             extra_args=[game_arg] if game_arg else None,
                             label="Pandora")
    shutdown_prefix_wineserver(proton_script, compat_data, log_fn=log_fn)
    log_fn(f"Pandora exited (code {rc}).")
    return rc
