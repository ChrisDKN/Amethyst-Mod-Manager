"""Headless checks for texture-tool discrete GPU selection.

Covers the host selector shared by VRAMr/BENDr/TexGen/DynDOLOD, forwarding
through the plain-Wine launcher, and the optional control on the Proton step.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_texture_gpu_selection.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="mm-texturegpu-cfg-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtWidgets import QApplication

from Utils import exe_launch
from Utils.texture_tools import apply_discrete_gpu_environment
from wizards_qt.proton_step import ProtonStepWidget

_app = QApplication.instance() or QApplication([])
_failures = []


def check(label, condition):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


print("host GPU selectors:")
env = {}
selection = apply_discrete_gpu_environment(
    env, True, vendor_ids=("0x8086", "0x1002"))
check("Mesa hybrid exposes only the discrete adapter",
      selection == "Mesa discrete GPU (DRI_PRIME=1!)"
      and env == {"DRI_PRIME": "1!"})

env = {"DRI_PRIME": "1!"}
selection = apply_discrete_gpu_environment(
    env, True, vendor_ids=("0x8086", "0x10de"))
check("NVIDIA hybrid uses PRIME and clears the Mesa selector",
      selection == "NVIDIA PRIME discrete GPU"
      and "DRI_PRIME" not in env
      and env.get("__VK_LAYER_NV_optimus") == "NVIDIA_only")

env = {"KEEP": "yes"}
selection = apply_discrete_gpu_environment(
    env, True, vendor_ids=("0x1002",))
check("single-GPU systems are left unchanged",
      selection.endswith("no second host GPU was detected")
      and env == {"KEEP": "yes"})


print("plain-Wine forwarding:")
captured = {}


def _fake_plain_wine(*_args, **kwargs):
    captured.update(kwargs)
    return 17


with patch.object(exe_launch, "run_tool_winetricks_style", _fake_plain_wine):
    rc = exe_launch.run_tool_logged(
        Path("/fake/proton"), Path("/fake/TexGenx64.exe"),
        {
            "AMM_WINETRICKS_STYLE": "1",
            "STEAM_COMPAT_DATA_PATH": "/fake/prefix",
            "DRI_PRIME": "1!",
        },
    )
forwarded = captured.get("extra_env", {})
check("launcher return code is preserved", rc == 17)
check("Mesa selector reaches the rebuilt Wine environment",
      forwarded.get("DRI_PRIME") == "1!")
check("stale NVIDIA selector is explicitly removed",
      "__VK_LAYER_NV_optimus" in forwarded
      and forwarded["__VK_LAYER_NV_optimus"] is None)


print("Proton step control:")


class _Game:
    name = "GPU Test"

    def get_prefix_path(self):
        return None


protons = [Path("/fake/Proton-Test/proton")]
with patch("Utils.steam_finder.list_installed_proton", return_value=protons), \
     patch("Utils.steam_finder.find_proton_for_game", return_value=None), \
     patch("Utils.steam_finder.game_steam_id", return_value=None):
    shown = ProtonStepWidget(
        _Game(), Path("/fake/TexGenx64.exe"), "TexGenx64.exe", "TexGen",
        lambda *_args: None, show_discrete_gpu=True)
    hidden = ProtonStepWidget(
        _Game(), Path("/fake/xLODGenx64.exe"), "xLODGenx64.exe", "xLODGen",
        lambda *_args: None)

check("TexGen can request the discrete-GPU control",
      shown._prefer_discrete_gpu_cb is not None)
check("control appears below the plain-Wine option",
      shown.layout().indexOf(shown._prefer_discrete_gpu_cb)
      > shown.layout().indexOf(shown._winetricks_chk))
check("control defaults off", not shown.prefer_discrete_gpu())
shown._prefer_discrete_gpu_cb.setChecked(True)
check("checked state is reported", shown.prefer_discrete_gpu())
check("other Proton-step tools do not gain the control",
      hidden._prefer_discrete_gpu_cb is None
      and not hidden.prefer_discrete_gpu())


print()
if _failures:
    print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
    sys.exit(1)
print("All texture GPU selection tests passed.")
