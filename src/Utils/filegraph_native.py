"""Required native-extension loader and MessagePack wire helpers."""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import msgpack

from Utils.filegraph_models import FileGraphUnavailable


API_VERSION = 11
SCHEMA_VERSION = 9
ENGINE_REVISION = 1
RULES_REVISION = 7
_native: ModuleType | None = None


def _development_artifacts() -> tuple[Path, ...]:
    """Source-tree builds accepted for development, never a legacy fallback."""
    repo = Path(__file__).resolve().parents[2]
    configured = os.environ.get("AMETHYST_FILEGRAPH_EXTENSION")
    values = [Path(configured)] if configured else []
    values.append(repo / "src" / "amethyst_filegraph.abi3.so")
    return tuple(values)


def require_native() -> ModuleType:
    global _native
    if _native is not None:
        return _native
    error: BaseException | None = None
    module = None
    configured = os.environ.get("AMETHYST_FILEGRAPH_EXTENSION")
    if configured:
        try:
            spec = importlib.util.spec_from_file_location(
                "amethyst_filegraph", Path(configured))
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
        except BaseException as exc:
            error = exc
            module = None
    if module is None:
        try:
            module = importlib.import_module("amethyst_filegraph")
        except BaseException as exc:
            error = exc
            module = None
    if module is None:
        for artifact in _development_artifacts():
            if configured and artifact == Path(configured):
                continue
            if not artifact.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    "amethyst_filegraph", artifact)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                break
            except BaseException as exc:
                error = exc
                module = None
    if module is None:
        detail = f" ({error})" if error else ""
        raise FileGraphUnavailable(
            "Amethyst's required filegraph component could not be loaded"
            f"{detail}. Reinstall the application package. Developers can run "
            "`python native/amethyst_filegraph/build_extension.py`."
        ) from error
    found = int(module.api_version())
    if found != API_VERSION:
        raise FileGraphUnavailable(
            f"The installed filegraph API is version {found}, but this Amethyst "
            f"build requires version {API_VERSION}. Reinstall the application "
            "so its Python and native components match."
        )
    found_schema = int(module.schema_version())
    if found_schema != SCHEMA_VERSION:
        raise FileGraphUnavailable(
            f"The installed filegraph schema is version {found_schema}, but "
            f"this Amethyst build requires version {SCHEMA_VERSION}. "
            "Reinstall the application so its Python and native components "
            "match."
        )
    _native = module
    return module


def pack(value: Any) -> bytes:
    return msgpack.packb(value, use_bin_type=True)


def unpack(value: bytes | bytearray | memoryview) -> Any:
    return msgpack.unpackb(value, raw=False, strict_map_key=False)
