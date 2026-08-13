"""Environment forwarding for commands escaped from our Flatpak sandbox.

``flatpak-spawn --host`` starts with the desktop session's host environment,
not the environment of the calling sandbox.  Most callers build a child env
from ``os.environ.copy()`` and then add Proton/Wine variables.  Comparing that
dict with ``os.environ`` catches newly-added values, but loses two important
classes of variables:

* Steam/Proton variables inherited by Amethyst (for example when Gaming Mode
  launched it), and
* arbitrary variables applied at startup through Settings -> Environment.

Forward the runtime families unconditionally and use app_env's marker for the
user-configured names.  Deliberately do not forward the complete sandbox env:
its PATH, XDG directories and loader paths describe the Flatpak runtime and
are invalid for a host-side Proton process.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


_APP_ENV_MARKER = "_AMM_ENV_KEYS"

_ALWAYS_FORWARD_EXACT = frozenset({
    "SteamAppId",
    "SteamGameId",
    "SteamOverlayGameId",
    "GAMEID",
    "PROTONPATH",
    "WINEPREFIX",
    "WINEDLLOVERRIDES",
    "WINEDEBUG",
    "DRI_PRIME",
    "MANGOHUD",
})

_ALWAYS_FORWARD_PREFIXES = (
    "STEAM_COMPAT_",
    "PROTON_",
    "WINE",
    "DXVK_",
    "VKD3D_",
    "MANGOHUD_",
    "GAMESCOPE_",
    "AMD_VULKAN_",
    "__GL_",
    "__NV_",
)


def flatpak_forward_env_args(
    env: "Mapping[str, str] | None",
    *,
    baseline: "Mapping[str, str] | None" = None,
) -> list[str]:
    """Return ``--env=KEY=VALUE`` arguments for ``flatpak-spawn --host``.

    Values changed relative to *baseline* are explicit launch overrides and
    are always forwarded.  Runtime variables and names recorded in
    ``_AMM_ENV_KEYS`` are forwarded even when they already existed in the
    sandbox environment.  This is the distinction the old plain env-diff
    implementation could not make.
    """
    if not env:
        return []
    base = os.environ if baseline is None else baseline
    configured = {
        name for name in env.get(_APP_ENV_MARKER, "").split(",") if name
    }
    out: list[str] = []
    for key, value in env.items():
        if (base.get(key) != value
                or key in configured
                or key in _ALWAYS_FORWARD_EXACT
                or key.startswith(_ALWAYS_FORWARD_PREFIXES)):
            out.append(f"--env={key}={value}")
    return out
