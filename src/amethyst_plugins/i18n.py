from __future__ import annotations

from Utils import json_limits as json
import logging
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from string import Formatter


def language_code():
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance()
    code = getattr(app, "_amethyst_language", None)
    if code is None:
        code = os.environ.get("AMETHYST_PLUGIN_LANGUAGE")
    if code is None:
        code = _configured_language()
    return code


@lru_cache(maxsize=1)
def _configured_language():
    from gui_qt.i18n import _resolve_code
    from Utils.ui.config import load_language
    return _resolve_code(load_language())


def _fields(text):
    fields = Counter()
    for _, name, spec, conversion in Formatter().parse(text):
        if name is not None:
            fields[(name, spec, conversion)] += 1
            fields.update(_fields(spec))
    return fields


@lru_cache(maxsize=128)
def _catalog(path, modified, size):
    try:
        if size > 4 * 1024 * 1024:
            raise ValueError("Translation catalogue exceeds 4 MiB.")
        data = json.read_json(path, max_bytes=4 * 1024 * 1024)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        result = {}
        for source, translated in data.items():
            if not isinstance(translated, str) or not translated:
                continue
            try:
                if _fields(source) == _fields(translated):
                    result[source] = translated
            except ValueError:
                continue
        return result
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        logging.getLogger(__name__).warning("Could not load plugin translations %s: %s", path, exc)
        return {}


class Translator:
    def __init__(self, directory, language=None):
        self.directory = Path(directory).resolve()
        self.language = language_code() if language is None else language

    def __call__(self, source, *args, **kwargs):
        code = self.language.replace("-", "_")
        codes = ["en"]
        if re.fullmatch(r"[A-Za-z]{2,3}(?:_[A-Za-z0-9]{2,8})*", code):
            parts = code.split("_")
            codes.extend("_".join(parts[:i]) for i in range(1, len(parts) + 1))
        translated = source
        for code in dict.fromkeys(codes):
            path = (self.directory / "translations" / f"{code}.json").resolve()
            if not path.is_relative_to(self.directory):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            translated = _catalog(path, stat.st_mtime_ns, stat.st_size).get(source, translated)
        return translated.format(*args, **kwargs) if args or kwargs else translated


def translate(plugin, source):
    from .catalog import plugin_directory
    return Translator(plugin_directory(plugin))(source)
