"""Qt icon loading - reuses the existing PNG assets in src/icons/.

The Tk app loads these via gui.theme.load_icon (→ CTkImage). The Qt app loads
the same files into QIcon. Icons are cached by (name, size).
"""

from __future__ import annotations

from pathlib import Path
from PySide6.QtGui import QIcon, QPixmap, QTransform, QPainter, QColor
from PySide6.QtCore import QSize, Qt

# src/icons/ - same dir the Tk app uses (gui/ is a sibling of icons/).
_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"

_cache: dict[tuple[str, int], QIcon] = {}


def icon(name: str, size: int = 18, color: str | None = None) -> QIcon:
    """Return a QIcon for icons/<name> scaled to *size* px (square).

    When *color* is given the opaque pixels are recoloured to it (alpha shape
    preserved), so a mono glyph like the gear can follow the theme foreground
    and stay visible in both light and dark modes.

    Missing files yield an empty QIcon (button shows text only).
    """
    key = (f"{name}#{color or ''}", size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    path = _ICONS_DIR / name
    if not path.is_file():
        ic = QIcon()
    else:
        pm = QPixmap(str(path))
        if not pm.isNull():
            pm = pm.scaled(QSize(size, size), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
            if color:
                tinted = QPixmap(pm.size())
                tinted.fill(Qt.transparent)
                p = QPainter(tinted)
                p.drawPixmap(0, 0, pm)          # original (for its alpha shape)
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(tinted.rect(), QColor(color))
                p.end()
                pm = tinted
        ic = QIcon(pm)
    _cache[key] = ic
    return ic


_exe_icon_cache: dict[tuple[str, float], "QIcon | None"] = {}


def exe_icon(path, size: int = 18) -> "QIcon | None":
    """Return the QIcon embedded in a Windows .exe, or None.

    Parses the PE icon resource (Utils.exe_icon) into .ico bytes and loads them
    via QImage so the exe entries in the play-bar dropdown carry their own icon.
    Cached by (path, mtime); None is cached too so a bad exe isn't reparsed.
    """
    from pathlib import Path as _Path
    p = _Path(path)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key in _exe_icon_cache:
        return _exe_icon_cache[key]
    ic: "QIcon | None" = None
    try:
        from Utils.exe_icon import extract_exe_icon
        from PySide6.QtGui import QImage
        blob = extract_exe_icon(p)
        if blob:
            img = QImage.fromData(blob, "ICO")
            if not img.isNull():
                pm = QPixmap.fromImage(img).scaled(
                    QSize(size, size), Qt.KeepAspectRatio,
                    Qt.SmoothTransformation)
                if not pm.isNull():
                    ic = QIcon(pm)
    except Exception:
        ic = None
    _exe_icon_cache[key] = ic
    return ic


def hamburger_icon(size: int = 18, color: str = "#ffffff") -> QIcon:
    """Return a drawn 3-bar 'hamburger' menu QIcon tinted to *color*.

    No PNG asset exists for this; drawing it keeps the glyph crisp at any size
    and theme-tintable. Used for the play-bar exe-menu button so it reads as a
    menu of run-exe actions rather than a second settings gear."""
    key = (f"__hamburger#{color}", size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, False)
    thickness = max(1, round(size / 9))
    inset = round(size * 0.22)
    width = size - inset * 2
    col = QColor(color)
    # Three evenly spaced bars at ~28% / 50% / 72% of the height.
    for frac in (0.28, 0.5, 0.72):
        y = round(size * frac - thickness / 2)
        p.fillRect(inset, y, width, thickness, col)
    p.end()
    ic = QIcon(pm)
    _cache[key] = ic
    return ic


def icon_rotated(name: str, degrees: int, size: int = 18,
                 color: str | None = None) -> QIcon:
    """Return a QIcon for icons/<name> rotated *degrees* clockwise, scaled to
    *size* px, optionally recoloured to *color* (tints the opaque pixels while
    keeping the alpha shape). Used e.g. for up/down chevrons from arrow.png."""
    key = (f"{name}@{degrees}#{color or ''}", size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    path = _ICONS_DIR / name
    if not path.is_file():
        ic = QIcon()
    else:
        pm = QPixmap(str(path))
        if not pm.isNull():
            if degrees:
                pm = pm.transformed(QTransform().rotate(degrees),
                                    Qt.SmoothTransformation)
            pm = pm.scaled(QSize(size, size), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
            if color:
                tinted = QPixmap(pm.size())
                tinted.fill(Qt.transparent)
                p = QPainter(tinted)
                p.drawPixmap(0, 0, pm)          # original (for its alpha shape)
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(tinted.rect(), QColor(color))
                p.end()
                pm = tinted
        ic = QIcon(pm)
    _cache[key] = ic
    return ic
