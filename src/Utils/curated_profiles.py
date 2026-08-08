"""
curated_profiles.py
Download prebuilt (curated) .amethyst profiles from the Amethyst-Mod-Manager
``Resources`` branch on GitHub - e.g. ``Profiles/FalloutNV/Viva_New_Vegas.amethyst``
for the "Install Viva New Vegas" wizard.

GUI-neutral: the Qt curated-profile wizard calls download_curated_profile() on a
worker thread, then hands the parsed manifest to the app's normal Import-profile
pipeline (which re-reads the bundle zip at the END of the install, so the file
is kept in a persistent cache dir - never a tempfile).
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from Utils.config_paths import get_config_dir

RAW_BASE = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/Resources/"


def cache_dir() -> Path:
    """Return the persistent cache dir for downloaded curated profiles.

    Result: ~/.config/AmethystModManager/curated_profiles/
    """
    d = get_config_dir() / "curated_profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_curated_profile(repo_path: str, log_fn=None) -> Path:
    """Download the .amethyst at *repo_path* (relative to the Resources branch
    root) into the curated-profiles cache and validate it parses as an Amethyst
    manifest with mods. Returns the downloaded file's path; raises on download
    or validation failure."""
    from Utils.ca_bundle import download_file
    from Utils.profile_export import read_manifest

    log = log_fn or (lambda _m: None)
    url = RAW_BASE + urllib.parse.quote(repo_path.lstrip("/"))
    dest = cache_dir() / Path(repo_path).name
    log(f"Curated profile: downloading {url}")
    download_file(url, dest, timeout=60)
    manifest = read_manifest(dest)
    if not isinstance(manifest, dict) or not manifest.get("mods"):
        raise ValueError(f"{dest.name} does not look like an Amethyst manifest.")
    log(f"Curated profile: saved {dest} ({len(manifest['mods'])} mod(s)).")
    return dest
