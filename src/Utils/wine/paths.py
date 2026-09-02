"""Wine/Proton path conversion - toolkit-neutral.

Converting a Linux absolute path into the ``Z:\\`` drive path that a Wine /
Proton prefix sees. Pure string/symlink logic, shared by the GUI and backend
(exe-args builder, wizards).
"""

from __future__ import annotations

from pathlib import Path


def to_wine_path(linux_path: Path | str, prefix: Path | None = None) -> str:
    r"""Convert a Linux absolute path to a Proton/Wine Z:\ path.

    If *prefix* is the Wine pfx directory (containing dosdevices/), the Z:
    symlink target is resolved first.  This handles prefixes where Z: points
    to a UUID mount (e.g. /mnt/c3edc2f9-.../`) rather than / - without this,
    paths on that drive would be double-prefixed (Z:\mnt\uuid\...).
    """
    if prefix is not None:
        z_link = Path(prefix) / "dosdevices" / "z:"
        if z_link.is_symlink():
            z_target = z_link.resolve()
            try:
                rel = Path(linux_path).resolve().relative_to(z_target)
                return "Z:\\" + str(rel).replace("/", "\\")
            except ValueError:
                pass
    return "Z:" + str(linux_path).replace("/", "\\")
