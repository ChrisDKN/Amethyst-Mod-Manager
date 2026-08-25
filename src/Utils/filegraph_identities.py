"""Exclusive-identity helpers used during candidate derivation."""

from __future__ import annotations

import os
import re


_MULTIPART_RE = re.compile(r"^(.*)_\d+\.pak$")


def is_multipart_pak(relative_key: str, mod_files: dict) -> bool:
    match = _MULTIPART_RE.match(relative_key)
    if match is None:
        return False
    base = match.group(1) + ".pak"
    return any(key == base or key.endswith("/" + base) for key in mod_files)


def bg3_uuid_conflicts_enabled() -> bool:
    return os.environ.get(
        "AMM_BG3_PAK_UUID_CONFLICTS", "1").strip().lower() not in {
            "0", "false", "no",
        }
