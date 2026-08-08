"""UI translation loading (Qt native i18n).

The app uses Qt's own translation system rather than gettext: user-facing
strings are wrapped in ``self.tr("...")`` (or ``QCoreApplication.translate``),
extracted with ``pyside6-lupdate`` into ``translations/amethyst_<code>.ts``,
translated (Qt Linguist or any XML editor), then compiled to ``.qm`` with
``pyside6-lrelease``. See ``tools/i18n_update.sh`` for the extract/compile step.

Translation files are loaded from TWO locations, config-folder-wins:

1. ``~/.config/AmethystModManager/languages/`` - the user config folder. This
   is where translations synced from the Resources branch (see
   ``Utils.gh_sync.sync_languages``) land, and where a user can drop their own
   ``amethyst_<code>.qm`` to add or override a language WITHOUT an app update.
2. The built-in ``translations/`` source folder - ships English (the source
   language needs no file) and any languages bundled with the app.

When a language exists in both, the config-folder copy wins, so fixes shipped
via Resources take effect without a full app release.

At startup :func:`install_translators` picks the language from ``amethyst.ini``
(``[ui] language``; empty = follow the system locale), loads our ``.qm`` plus
Qt's own bundled ``qtbase_<code>.qm`` (so standard dialog buttons - OK/Cancel/
etc. - are localised too), and installs both on the app.

The translators are kept alive on the QApplication because Qt does not retain
a reference to an installed QTranslator; letting one get garbage-collected
silently drops its translations.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLocale, QTranslator, QLibraryInfo

# Built-in translations/ sits next to the icons/ dir at the src/ root.
TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"


def _config_languages_dir() -> "Path | None":
    """The user config languages/ dir (synced + user-added .qm), or None if it
    can't be resolved (never let a config-path error break startup)."""
    try:
        from Utils.config_paths import get_languages_dir
        return get_languages_dir()
    except Exception:
        return None


def _qm_search_dirs() -> list[Path]:
    """Directories to look for amethyst_<code>.qm in, HIGHEST priority first:
    the config folder (synced/user) then the built-in source folder."""
    dirs: list[Path] = []
    cfg = _config_languages_dir()
    if cfg is not None:
        dirs.append(cfg)
    dirs.append(TRANSLATIONS_DIR)
    return dirs

# Compiled-translation filenames look like "amethyst_<code>.qm".
_QM_PREFIX = "amethyst_"
_QM_SUFFIX = ".qm"

# Display names for codes QLocale can't name nicely (or where we want an
# override). QLocale.nativeLanguageName() handles the common cases; this is a
# fallback map, not the source of truth for which languages exist. Keyed by the
# exact <code> in the .qm filename (so pt and pt_BR can differ).
_DISPLAY_OVERRIDES: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "pt": "Português (Portugal)",
    "pt_BR": "Português (Brasil)",
    "pt_PT": "Português (Portugal)",
}


def _title(s: str) -> str:
    s = (s or "").strip()
    return s[:1].upper() + s[1:] if s else s


def _display_name(code: str) -> str:
    """Human-readable, in-language name for a locale code (e.g. de -> Deutsch).

    For region variants (a code with an underscore, e.g. pt_BR) the native
    territory is appended so they read distinctly in the picker
    ("Português (Brasil)" vs "Português (Portugal)").
    """
    if code in _DISPLAY_OVERRIDES:
        return _DISPLAY_OVERRIDES[code]
    loc = QLocale(code)
    name = _title(loc.nativeLanguageName()
                  or QLocale.languageToString(loc.language()))
    if not name:
        return code
    # If the code carries a region (pt_BR), disambiguate with the territory.
    if "_" in code or "-" in code:
        territory = (loc.nativeTerritoryName() or "").strip()
        if territory:
            return f"{name} ({territory})"
    return name


def available_languages() -> list[tuple[str, str]]:
    """Return [(display, code), ...] for the language picker.

    The list is derived from the .qm files present in the config languages/
    folder AND the built-in translations/ folder: every compiled
    ``amethyst_<code>.qm`` becomes a selectable entry, so adding a translation
    (sync from Resources, or drop the .qm in the config folder) makes it appear
    with NO code change. "System default" ("" = follow the OS locale) and
    "English" (the source language, always available) are prepended.
    """
    out: list[tuple[str, str]] = [
        ("System default", ""),
        ("English", "en"),
    ]
    seen = {"", "en"}
    codes = []
    for d in _qm_search_dirs():
        if not d.is_dir():
            continue
        for qm in d.glob(f"{_QM_PREFIX}*{_QM_SUFFIX}"):
            code = qm.stem[len(_QM_PREFIX):]
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    for code in sorted(codes, key=_display_name):
        out.append((_display_name(code), code))
    return out


# Backwards-compatible module-level list. Prefer available_languages() at call
# time so newly-added .qm files show up; this snapshot is taken at import.
AVAILABLE_LANGUAGES: list[tuple[str, str]] = available_languages()


def _resolve_code(code: str) -> str:
    """Return the locale code to actually load.

    Empty ``code`` means follow the system; we take the two-letter language of
    the system UI locale (e.g. "de_DE" -> "de") so region variants share one
    translation file.
    """
    code = (code or "").strip()
    if code:
        return code
    return QLocale.system().name().split("_")[0]


def install_translators(app, code: str) -> list[QTranslator]:
    """Load and install our + Qt's translators for ``code`` onto ``app``.

    ``code`` is the configured language ("" = system locale). Returns the list
    of installed translators; they are also stashed on ``app`` to keep them
    alive. Missing .qm files are skipped silently (falls back to source
    strings), so this is safe to call before any translation exists.
    """
    lang = _resolve_code(code)
    installed: list[QTranslator] = []

    # English (or a resolved "en") is our source language - nothing to load.
    if lang and lang != "en":
        # Try each search dir in priority order (config folder first) and use
        # the first that has this language's .qm.
        app_tr = QTranslator(app)
        for d in _qm_search_dirs():
            if app_tr.load(f"{_QM_PREFIX}{lang}", str(d)):
                app.installTranslator(app_tr)
                installed.append(app_tr)
                break

        # Qt's own strings (dialog buttons, etc.). Shipped with PySide6.
        qt_dir = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        qt_tr = QTranslator(app)
        if qt_tr.load(f"qtbase_{lang}", qt_dir):
            app.installTranslator(qt_tr)
            installed.append(qt_tr)

    # Keep references alive - Qt does not retain installed translators.
    app._i18n_translators = installed
    return installed
