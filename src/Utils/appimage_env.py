"""
Utils/appimage_env.py
The canonical list of AppImage/bundle-injected environment variables, and the
scrub function that removes them from a child-process environment.

Inside the AppImage, the sharun runtime (and our own launcher wrapper) exports
vars that point into the transient /tmp/.mount_* FUSE mount - loader paths,
GTK/GIO module caches, CA-bundle paths, our private FONTCONFIG_FILE, … A HOST
child that inherits them loads the bundle's libraries instead of its own
(crashes, TLS failures), or keeps using paths that dangle the moment the
AppImage exits. sharun's anylinux.so execve hook scrubs SOME of these
automatically, but it is disabled on some build hosts (ANYLINUX_LIB=0) and
doesn't know about vars our own wrapper adds - so the Python side must not
rely on it.

History: three modules (protontricks, xdg, smapi_installer) each grew their
own hand-maintained copy of this list, and they drifted - a var added to one
was silently missing from the others. This module is now the single source of
truth; the per-module wrappers keep their differing POLICIES (no-op outside
the AppImage vs. always-strip) but share the list and the strip logic.

Prefix semantics: an env var is dropped when its name STARTS WITH an entry
below - so "FONTCONFIG_" covers FONTCONFIG_FILE / _PATH / _SYSROOT and
"SHARUN_" covers every sharun knob.
"""

from __future__ import annotations

import os

# Union of the historical protontricks/xdg/smapi lists. Grouped by origin.
APPIMAGE_ENV_PREFIXES: tuple[str, ...] = (
    # AppImage runtime identity / paths
    "APPDIR", "APPIMAGE", "OWD", "URUNTIME", "ARG0", "ARGV0",
    "SHARUN_",
    # Loader
    "LD_LIBRARY_PATH", "LD_PRELOAD", "GCONV_PATH",
    # Bundled Python
    "PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    # Our own launcher/bootstrap
    "MOD_MANAGER_GAMES",     # app_bootstrap points this at $APPDIR/.../Games
    "FONTCONFIG_",           # wrapper's private fonts.conf - a host child
                             # reading it would write ITS fontconfig version's
                             # caches into our private cache dir, re-creating
                             # the shared-cache poisoning crash in reverse
    # TLS / CA bundles (dangle after the mount goes away)
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    # GTK / GLib module caches (host file manager/browser would load ours)
    "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE",
    "GIO_MODULE_DIR", "GIO_LAUNCH_DESKTOP", "GSETTINGS_SCHEMA_DIR",
    "GTK_PATH", "GTK_IM_MODULE_FILE",
    # Qt plugin paths (a host Qt app would load OUR Qt plugins → version clash)
    "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
    # Misc bundled-data pointers
    "TERMINFO", "LIBTHAI_DICTDIR", "PERLLIB", "PERL5LIB",
    "XDG_DATA_DIRS_APPIMAGE",
)

# List-style vars: keep host entries, drop bundle-pointing fragments.
_LIST_VARS = ("PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS")


def in_appimage() -> bool:
    """True when this process runs inside the AppImage bundle."""
    return bool(os.environ.get("APPDIR") or os.environ.get("APPIMAGE"))


def strip_appimage_vars(env: dict) -> dict:
    """Return a copy of *env* with bundle-injected vars removed.

    Whole vars matching ``APPIMAGE_ENV_PREFIXES`` are dropped outright;
    list-style vars (PATH, XDG_DATA_DIRS, XDG_CONFIG_DIRS) keep their host
    entries but lose any ``/tmp/.mount_*`` / ``$APPDIR`` fragments - otherwise
    a host child would still find the bundle's own python3/tools on PATH and
    re-pollute itself.

    Unconditional (no AppImage gate): this also defends against STALE bundle
    env inherited from a shell the user opened out of a previous AppImage run.
    Callers that must be a no-op outside the AppImage gate on
    :func:`in_appimage` themselves.
    """
    appdir = ""
    if os.environ.get("APPDIR"):
        appdir = os.path.realpath(os.environ["APPDIR"])

    out = {
        k: v for k, v in env.items()
        if not any(k.startswith(p) for p in APPIMAGE_ENV_PREFIXES)
    }

    def _bundled_entry(entry: str) -> bool:
        if not entry:
            return True
        if entry.startswith("/tmp/.mount_"):
            return True
        return bool(appdir) and os.path.realpath(entry).startswith(appdir)

    for k in _LIST_VARS:
        if k not in out:
            continue
        cleaned = os.pathsep.join(
            p for p in out[k].split(os.pathsep) if not _bundled_entry(p))
        if cleaned:
            out[k] = cleaned
        else:
            out.pop(k, None)
    return out
