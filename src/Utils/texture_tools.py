"""
GUI-neutral gates + preset table for the VRAMr / BENDr / ParallaxR wizards.

The wrappers (wrappers/vramr.py, bendr.py, parallaxr.py) are already neutral;
this module just holds the install-detection helpers and VRAMr's preset table
so the Qt wizard view can share them with the Tk wizards.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class TextureToolCancelled(Exception):
    """Raised when a VRAMr/BENDr/ParallaxR run is stopped by the user.

    Distinct from a genuine failure so the wizard can report "Cancelled"
    instead of an error, and skip the partial output it leaves behind.
    """


# (key, label, description) - VRAMr's optimisation presets.
VRAMR_PRESETS = [
    ("hq",          "High Quality",  "2K / 2K / 1K / 1K  - 4K modlist downscaled to 2K"),
    ("quality",     "Quality",       "2K / 1K / 1K / 1K  - Balance of quality & savings"),
    ("optimum",     "Optimum",       "2K / 1K / 512 / 512 - Good starting point"),
    ("performance", "Performance",   "2K / 512 / 512 / 512 - Big gains, lower close-up"),
    ("vanilla",     "Vanilla",       "512 / 512 / 512 / 512 - Just run the game"),
]


def host_gpu_vendor_ids(sys_drm: Path = Path("/sys/class/drm")) -> tuple[str, ...]:
    """Return one PCI vendor ID per host DRM card, in card-number order.

    Reading sysfs avoids depending on ``lspci`` inside the AppImage/Flatpak.
    Connector entries (``card0-DP-1``) are excluded by the glob, and cards
    without a PCI-style ``device/vendor`` file are ignored.
    """
    vendors: list[str] = []
    try:
        vendor_files = sorted(sys_drm.glob("card[0-9]*/device/vendor"))
    except OSError:
        return ()
    for vendor_file in vendor_files:
        try:
            vendor = vendor_file.read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        if vendor:
            vendors.append(vendor)
    return tuple(vendors)


def apply_discrete_gpu_environment(
    env: dict[str, str],
    enabled: bool,
    *,
    vendor_ids: tuple[str, ...] | None = None,
) -> str:
    """Apply a hybrid-GPU selector to *env* and describe the result.

    Proton/DXVK consumes Vulkan device ordering from the environment.  NVIDIA
    PRIME has its own layer; Mesa uses ``DRI_PRIME``.  Appending ``!`` makes
    Mesa expose only the selected Vulkan device, so texconv's ``-gpu 0`` is
    deterministic instead of relying on DXGI enumeration order.
    """
    if not enabled:
        return "automatic GPU selection"

    vendors = host_gpu_vendor_ids() if vendor_ids is None else vendor_ids
    if len(vendors) < 2:
        return "discrete GPU requested, but no second host GPU was detected"

    if "0x10de" in vendors:  # NVIDIA
        env.pop("DRI_PRIME", None)
        env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
        env["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
        env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
        return "NVIDIA PRIME discrete GPU"

    env.pop("__NV_PRIME_RENDER_OFFLOAD", None)
    env.pop("__VK_LAYER_NV_optimus", None)
    env.pop("__GLX_VENDOR_LIBRARY_NAME", None)
    env["DRI_PRIME"] = "1!"
    return "Mesa discrete GPU (DRI_PRIME=1!)"


def kill_process_group(proc, log_fn=None) -> None:
    """Kill *proc* and every child it spawned, for a user cancel.

    Proton/Wine fan out into a tree (the Proton python script, wineserver, the
    tool itself), so killing only the direct child orphans the real workers -
    they keep chewing CPU and writing into the output folder. The subprocess is
    started with ``start_new_session=True`` so it leads its own process group
    and one ``killpg`` takes the whole tree down.

    SIGTERM first so wineserver can flush, then SIGKILL anything still up.
    """
    import os
    import signal
    import subprocess
    log = log_fn or (lambda _m: None)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError as exc:
            log(f"  stop: signal {sig} failed: {exc}")
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def applications_dir(game: "BaseGame", app_dir: str) -> Path:
    return game.get_mod_staging_path().parent / "Applications" / app_dir


def vramr_installed(game: "BaseGame") -> bool:
    app_dir = applications_dir(game, "VRAMr")
    return (app_dir / "VRAMr.exe").is_file() or (app_dir / "tools").is_dir()


def texture_tool_installed(game: "BaseGame", app_dir: str) -> bool:
    """BENDr / ParallaxR are 'installed' once their tools/ dir is present."""
    return (applications_dir(game, app_dir) / "tools").is_dir()
