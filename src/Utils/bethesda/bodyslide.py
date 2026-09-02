"""
GUI-neutral core of the BodySlide / Outfit Studio wizards.

Moved out of wizards/bodyslide.py (which imports customtkinter) so the Qt
wizard view can share it: deployed-exe discovery, the output-capture mod,
and the Config.xml updates (OutputDataPath / GameDataPath / TargetGame /
ProjectPath) that point BodySlide at the deployed Data folder and the
output mod.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame


def _noop(_msg: str) -> None:
    pass


def as_names(exe_name) -> tuple[str, ...]:
    """Accept a single name or an iterable of candidate names."""
    if isinstance(exe_name, str):
        return (exe_name,)
    return tuple(exe_name)


def find_deployed_exe(game: "BaseGame", exe_name) -> Path | None:
    """Search the deployed Data directory for exe_name (launch time, after deploy)."""
    from Utils.vfs import effective_tool_data_root
    try:
        data_path = effective_tool_data_root(game)
    except RuntimeError:
        return None
    if not data_path.is_dir():
        return None
    fallback = None
    for name in as_names(exe_name):
        for candidate in data_path.rglob(name):
            if not candidate.is_file():
                continue
            # Stale copies (e.g. leftovers captured into Overwrite) can deploy
            # alongside the real install; without res/xrc next to the exe,
            # BodySlide dies with "Failed to load Setup.xrc file!".
            if (candidate.parent / "res" / "xrc").is_dir():
                return candidate
            if fallback is None:
                fallback = candidate
    return fallback


def wine_z_path(p: Path) -> str:
    """BodySlide-style Wine path: Z:\\…\\ with a trailing backslash."""
    s = str(p).replace("/", "\\")
    if not s.startswith("\\"):
        s = "\\" + s
    return "Z:" + s + "\\"


def sanitize_output_name(raw: str, default: str) -> str:
    """Strip filesystem-hostile characters; blank falls back to *default*."""
    name = re.sub(r'[\\/:*?"<>|]', "", raw or "").strip(" .")
    return name or default


def ensure_output_mod(game: "BaseGame", profile: str, mod_name: str) -> Path:
    """Create the empty output-capture mod folder and enable it in the
    profile's modlist (prepended) when missing."""
    staging = game.get_effective_mod_staging_path()
    mod_dir = staging / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    modlist_path = game.get_profile_root() / "profiles" / profile / "modlist.txt"
    if modlist_path.is_file():
        from Utils.mods.modlist import prepend_mod, read_modlist
        entries = read_modlist(modlist_path)
        if not any(e.name == mod_name for e in entries):
            prepend_mod(modlist_path, mod_name, enabled=True)

    return mod_dir


def config_xml_path(base: Path) -> Path | None:
    direct = base / "CalienteTools" / "BodySlide" / "Config.xml"
    if direct.is_file():
        return direct
    for cand in base.rglob("Config.xml"):
        if cand.is_file() and cand.parent.name.lower() == "bodyslide":
            return cand
    return None


def slider_data_root(base: Path) -> Path | None:
    """Find the folder that holds SliderSets/SliderGroups/ShapeData.

    BodySlide 5.8+ resolves these relative to <ProjectPath> (the exe dir
    when empty). When the exe and the slider data live in different folders
    (exe under Tools/BodySlide, data under <mod>/BodySlide), the lists come
    up empty. We locate the data folder so the caller can pin ProjectPath.
    """
    direct = base / "CalienteTools" / "BodySlide"
    if (direct / "SliderSets").is_dir():
        return direct
    for cand in base.rglob("SliderSets"):
        if cand.is_dir():
            return cand.parent
    return None


# Maps a game's synthesis_registry_name to its BodySlide GameDataPaths child
# tag and TargetGame index (see the comment at the top of Config.xml).
BODYSLIDE_GAMES = {
    "Fallout3":                ("Fallout3", 0),
    "FalloutNewVegas":         ("FalloutNewVegas", 1),
    "Skyrim":                  ("Skyrim", 2),
    "Fallout4":                ("Fallout4", 3),
    "Skyrim Special Edition":  ("SkyrimSpecialEdition", 4),
    "Fallout 4 VR":            ("Fallout4VR", 5),
    "Skyrim VR":               ("SkyrimVR", 6),
}


def bodyslide_game(game: "BaseGame") -> "tuple[str, int] | None":
    return BODYSLIDE_GAMES.get(getattr(game, "synthesis_registry_name", None))


def set_gamedatapaths_child(text: str, tag: str, value: str) -> str:
    """Set <tag> inside the <GameDataPaths> block (creating it if absent)."""
    block = re.search(r"<GameDataPaths>(.*?)</GameDataPaths>", text, flags=re.DOTALL)
    if not block:
        return text
    inner = block.group(1)
    new_child = f"<{tag}>{value}</{tag}>"
    child_pat = rf"<{tag}>.*?</{tag}>"
    if re.search(child_pat, inner, flags=re.DOTALL):
        new_inner = re.sub(child_pat, lambda _m: new_child, inner, count=1, flags=re.DOTALL)
    else:
        new_inner = inner + f"        {new_child}\n    "
    return text[: block.start(1)] + new_inner + text[block.end(1):]


def set_config_tag(text: str, tag: str, value: str) -> str:
    new_tag = f"<{tag}>{value}</{tag}>"
    pattern = rf"<{tag}>.*?</{tag}>"
    if re.search(pattern, text, flags=re.DOTALL):
        return re.sub(pattern, lambda _m: new_tag, text, count=1, flags=re.DOTALL)
    return text.replace("</Config>", f"    {new_tag}\n</Config>", 1)


def update_output_path_in_config(game: "BaseGame", config_path: Path,
                                 output_dir: Path) -> bool:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False

    updated = set_config_tag(text, "OutputDataPath", wine_z_path(output_dir))

    data_path = game.get_mod_data_path()
    if data_path is not None:
        wine_data = wine_z_path(data_path)
        updated = set_config_tag(updated, "GameDataPath", wine_data)

        mapping = bodyslide_game(game)
        if mapping is not None:
            tag, target = mapping
            updated = set_gamedatapaths_child(updated, tag, wine_data)
            updated = set_config_tag(updated, "TargetGame", str(target))

        # BodySlide 5.8+ scans <ProjectPath>/SliderSets (falling back to the
        # exe dir when empty). The exe and the slider data are usually in
        # different folders here, so pin ProjectPath at the data folder or
        # every list shows up empty.
        slider_root = slider_data_root(data_path)
        if slider_root is not None:
            updated = set_config_tag(
                updated, "ProjectPath", wine_z_path(slider_root).rstrip("\\"))

    if updated == text:
        return True
    try:
        config_path.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


def apply_output_redirect(game: "BaseGame", output_mod_name: str, profile: str,
                          *, post_deploy: bool, tool_label: str = "BodySlide",
                          log_fn: Callable[[str], None] = _noop) -> None:
    """Materialize the output-capture mod (enable it in the modlist + point
    Config.xml's OutputDataPath at it).  Run BEFORE the deploy so the mod is
    in the filemap; run again with *post_deploy* to also patch the deployed
    Config.xml copy when deploy mode produced an independent file."""
    try:
        output_mod = ensure_output_mod(game, profile, output_mod_name)
    except OSError as exc:
        log_fn(f"{tool_label} Wizard: could not create '{output_mod_name}': {exc}")
        return

    staging = game.get_effective_mod_staging_path()
    source_cfg = None
    for sub in staging.iterdir() if staging.is_dir() else []:
        if not sub.is_dir():
            continue
        cand = config_xml_path(sub)
        if cand is not None:
            source_cfg = cand
            break

    if source_cfg is not None:
        if update_output_path_in_config(game, source_cfg, output_mod):
            log_fn(f"{tool_label} Wizard: set OutputDataPath → "
                   f"{wine_z_path(output_mod)} (source)")

    if post_deploy:
        from Utils.vfs import effective_tool_data_root
        try:
            data_path = effective_tool_data_root(game)
        except RuntimeError:
            data_path = None
        if data_path is not None and data_path.is_dir():
            deployed_cfg = config_xml_path(data_path)
            if deployed_cfg is not None and (
                source_cfg is None
                or deployed_cfg.resolve() != source_cfg.resolve()
            ):
                update_output_path_in_config(game, deployed_cfg, output_mod)
