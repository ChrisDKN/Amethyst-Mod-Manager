"""Quick-configure option descriptors - a thin, GUI-free mirror of the option
set the Configure-Game view exposes, so the profile dropdown can offer a
"Quick Configure" submenu that flips a single option live for the active profile
(exactly like saving that one field in the Configure view would).

The descriptors are built by inspecting the game with the same presence checks
the Configure view uses (``hasattr`` gates on setters/attributes), so any
game-specific option the Configure view surfaces (e.g. BG3's patch version,
plugins.txt casing) is picked up here automatically without duplicating the
game-by-game knowledge.

Each descriptor is a dict:
  key      - stable identifier (matches the Configure view keys)
  label    - human-readable option name
  kind     - "toggle" (bool) or "choice" (one-of)
  value    - the current value (bool for toggle; the current choice key)
  choices  - for "choice": list of (choice_key, choice_label)
  apply    - callable taking the new value and writing it live to the game
  needs_reload - True if changing it should trigger the app's post-configure
                 refresh (paths/profile-dir dependent, e.g. profile INI/saves).

No Qt imports here - this module stays importable in headless tests.
"""

from __future__ import annotations

from typing import Any

from Utils.deploy import LinkMode


def _toggle_attr(game, attr: str, default: bool):
    """A toggle backed by a plain boolean attribute on the game (no setter)."""
    def apply(val: bool):
        setattr(game, attr, bool(val))
    return bool(getattr(game, attr, default)), apply


def build_quick_configure_options(game) -> list[dict[str, Any]]:
    """Return the quick-configure descriptors for *game*'s active profile.

    Only options the game actually supports are included (same gating as the
    Configure view). Returns an empty list for an unconfigured game."""
    if game is None or not getattr(game, "is_configured", lambda: False)():
        return []

    opts: list[dict[str, Any]] = []

    def add_toggle(key, label, value, apply, *, needs_reload=False):
        opts.append({"key": key, "label": label, "kind": "toggle",
                     "value": bool(value), "apply": apply,
                     "needs_reload": needs_reload})

    def add_choice(key, label, value, choices, apply, *, needs_reload=False):
        opts.append({"key": key, "label": label, "kind": "choice",
                     "value": value, "choices": list(choices), "apply": apply,
                     "needs_reload": needs_reload})

    # --- Deploy method (Symlink / Hardlink) ---------------------------------
    if hasattr(game, "set_deploy_mode") and hasattr(game, "get_deploy_mode"):
        cur = (LinkMode.HARDLINK if game.get_deploy_mode() == LinkMode.HARDLINK
               else LinkMode.SYMLINK)
        rec = getattr(game, "default_deploy_mode", "symlink")
        add_choice(
            "deploy_mode", "Deploy Method",
            "hardlink" if cur == LinkMode.HARDLINK else "symlink",
            [("symlink",
              "Symlink (Recommended)" if rec == "symlink" else "Symlink"),
             ("hardlink",
              "Hardlink (Recommended)" if rec == "hardlink" else "Hardlink")],
            lambda v: game.set_deploy_mode(
                LinkMode.HARDLINK if v == "hardlink" else LinkMode.SYMLINK))

    # --- Boolean option toggles (mirror the Configure view gating) ----------
    if hasattr(game, "set_script_extender_swap"):
        add_toggle(
            "script_extender_swap",
            "Swap launcher with script extender on deploy",
            getattr(game, "script_extender_swap", True),
            lambda v: game.set_script_extender_swap(v))

    if hasattr(game, "set_auto_4gb_patch"):
        add_toggle(
            "auto_4gb_patch",
            "Apply the 4GB patch automatically on deploy",
            getattr(game, "auto_4gb_patch", True),
            lambda v: game.set_auto_4gb_patch(v))

    val, apply = _toggle_attr(game, "auto_deploy", False)
    add_toggle("auto_deploy",
               "Auto deploy (on enable/disable/reorder)", val, apply)

    if hasattr(game, "archive_invalidation_enabled"):
        val, apply = _toggle_attr(game, "archive_invalidation", True)
        add_toggle("archive_invalidation",
                   "Automatic archive invalidation (prefer loose files over BSAs)",
                   val, apply)

    if getattr(game, "case_alias_dirs", None):
        val, apply = _toggle_attr(game, "case_alias_links", True)
        add_toggle("case_alias_links",
                   "Create case-alias symlinks on deploy (Faster Load times)",
                   val, apply)

    # profile_ini_files / profile_saves only manage per-profile INI/save
    # symlinks at deploy time - they don't change mod staging, the deploy
    # target, or the plugin list, so no reload is needed (matches the full
    # Configure view, whose same-game save no-ops for these).
    if hasattr(game, "set_profile_ini_files") and hasattr(game, "profile_ini_files"):
        add_toggle("profile_ini_files", "Use profile-specific INI files",
                   getattr(game, "profile_ini_files", False),
                   lambda v: game.set_profile_ini_files(v))

    if (hasattr(game, "set_profile_saves") and hasattr(game, "profile_saves")
            and getattr(game, "supports_profile_saves", True)):
        add_toggle("profile_saves", "Use profile-specific saves",
                   getattr(game, "profile_saves", False),
                   lambda v: game.set_profile_saves(v))

    if hasattr(game, "prefix_numbering"):
        val, apply = _toggle_attr(game, "prefix_numbering", True)
        add_toggle("prefix_numbering",
                   "Prepend load-order numbers to mod folders", val, apply)

    if hasattr(game, "set_manage_load_order_in_dfu"):
        add_toggle(
            "manage_load_order_in_dfu",
            "Manage load order in DFU",
            getattr(game, "manage_load_order_in_dfu", False),
            lambda v: game.set_manage_load_order_in_dfu(v))

    # --- Game patch version (BG3-style) -------------------------------------
    if hasattr(game, "get_patch_version") and hasattr(game, "set_patch_version"):
        try:
            cur = int(game.get_patch_version())
        except Exception:
            cur = None
        add_choice(
            "patch_version", "Game Patch Version", cur,
            [(8, "Patch 8"), (7, "Patch 7"), (6, "Patch 6")],
            lambda v: game.set_patch_version(int(v)), needs_reload=True)

    # --- plugins.txt filename casing ----------------------------------------
    if (getattr(game, "uses_plugins_txt", False)
            and hasattr(game, "set_plugins_txt_filename")):
        cur = getattr(game, "plugins_txt_filename", "plugins.txt")
        add_choice(
            "plugins_txt_filename", "Plugins file name", cur,
            [("plugins.txt", "plugins.txt"), ("Plugins.txt", "Plugins.txt")],
            lambda v: game.set_plugins_txt_filename(v))

    return opts


def deploy_mode_change_blocked(game, new_value: str) -> bool:
    """True if switching the deploy method to *new_value* must be refused
    because mods are currently deployed (would strand the deployed files - the
    same guard the Configure view applies to path/deploy-mode saves)."""
    try:
        if not (game.is_configured() and game.get_deploy_active()):
            return False
    except Exception:
        return False
    cur = "hardlink" if game.get_deploy_mode() == LinkMode.HARDLINK else "symlink"
    return new_value != cur
