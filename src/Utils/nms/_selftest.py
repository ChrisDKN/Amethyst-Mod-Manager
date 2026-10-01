"""Focused checks for No Man's Sky GCMODSETTINGS.MXML handling.

Run directly from the source tree::

    python3 src/Utils/nms/_selftest.py

Covers parsing and rebuilding the game's exact file format, the mod-order
mapping, preservation of hand-installed entries, and the handler's
backup/restore cycle.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch


_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils.nms.gcmodsettings import (  # noqa: E402
    build_gcmodsettings_xml,
    disable_all_mods,
    entry_name,
    mod_entries,
    new_entry,
    parse_gcmodsettings,
)
from Utils.mods.modlist import ModEntry  # noqa: E402
from Utils.nms.gcmodsettings import resolve_mod_order, write_gcmodsettings  # noqa: E402

_CRLF = "\r\n"


def _entry_lines(index: int, name: str, enabled: str) -> list[str]:
    return [
        f'\t\t<Property name="Data" value="GcModSettingsInfo" _index="{index}">',
        f'\t\t\t<Property name="Name" value="{name}" />',
        '\t\t\t<Property name="Author" value="" />',
        '\t\t\t<Property name="ID" value="0" />',
        '\t\t\t<Property name="AuthorID" value="0" />',
        '\t\t\t<Property name="LastUpdated" value="0" />',
        f'\t\t\t<Property name="ModPriority" value="{index}" />',
        f'\t\t\t<Property name="Enabled" value="{enabled}" />',
        f'\t\t\t<Property name="EnabledVR" value="{enabled}" />',
        '\t\t\t<Property name="Dependencies" />',
        '\t\t</Property>',
    ]


def _game_file(entries: list[tuple[str, str]]) -> bytes:
    """Bytes exactly as NMS writes GCMODSETTINGS.MXML."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Data template="GcModSettings">',
        '\t<Property name="DisableAllMods" value="false" />',
    ]
    if entries:
        lines.append('\t<Property name="Data">')
        for i, (name, enabled) in enumerate(entries):
            lines.extend(_entry_lines(i, name, enabled))
        lines.append('\t</Property>')
    else:
        lines.append('\t<Property name="Data" />')
    lines.append('</Data>')
    # The game ends the file at </Data> with no trailing newline.
    return _CRLF.join(lines).encode("utf-8-sig")


_POPULATED = _game_file([
    ("CORVETTE OPERATIONS CONSOLE - WITH TELEPORT", "true"),
    ("BETTER RECHARGE ORDER", "true"),
    ("REMOVE INTRO LOGOS", "false"),
])
_EMPTY = _game_file([])


def _roundtrip(raw: bytes) -> bytes:
    root = parse_gcmodsettings(raw.decode("utf-8-sig"))
    assert root is not None
    xml = build_gcmodsettings_xml(mod_entries(root), disable_all_mods(root))
    return xml.encode("utf-8-sig")


def test_roundtrip_populated_file_is_byte_exact() -> None:
    assert _roundtrip(_POPULATED) == _POPULATED


def test_roundtrip_empty_file_is_byte_exact() -> None:
    assert _roundtrip(_EMPTY) == _EMPTY


def test_parse_reads_names_in_priority_order() -> None:
    root = parse_gcmodsettings(_POPULATED.decode("utf-8-sig"))
    assert [entry_name(e) for e in mod_entries(root)] == [
        "CORVETTE OPERATIONS CONSOLE - WITH TELEPORT",
        "BETTER RECHARGE ORDER",
        "REMOVE INTRO LOGOS",
    ]


def test_parse_rejects_garbage_and_foreign_documents() -> None:
    assert parse_gcmodsettings("not xml at all") is None
    assert parse_gcmodsettings('<Data template="GcUserSettingsData" />') is None


def test_new_entry_matches_game_template() -> None:
    xml = build_gcmodsettings_xml([new_entry("Better Recharge Order")])
    assert xml == _CRLF.join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Data template="GcModSettings">',
        '\t<Property name="DisableAllMods" value="false" />',
        '\t<Property name="Data">',
        *_entry_lines(0, "BETTER RECHARGE ORDER", "true"),
        '\t</Property>',
        '</Data>',
    ])


def test_build_renumbers_index_and_priority() -> None:
    root = parse_gcmodsettings(_POPULATED.decode("utf-8-sig"))
    reordered = list(reversed(mod_entries(root)))
    rebuilt = parse_gcmodsettings(build_gcmodsettings_xml(reordered))
    for i, entry in enumerate(mod_entries(rebuilt)):
        assert entry.get("_index") == str(i)
        prio = [p for p in entry if p.get("name") == "ModPriority"][0]
        assert prio.get("value") == str(i)
    assert entry_name(mod_entries(rebuilt)[0]) == "REMOVE INTRO LOGOS"


def test_special_characters_are_escaped() -> None:
    xml = build_gcmodsettings_xml([new_entry('Guns & "Roses" <v2>')])
    root = parse_gcmodsettings(xml)
    assert root is not None
    assert entry_name(mod_entries(root)[0]) == 'GUNS & "ROSES" <V2>'
    assert "&amp;" in xml and "&quot;" in xml and "&lt;" in xml


def _mods(*names: str) -> list[ModEntry]:
    return [ModEntry(name=n, enabled=True, locked=False) for n in names]


def _written_names(path: Path) -> list[tuple[str, str]]:
    root = parse_gcmodsettings(path.read_text(encoding="utf-8-sig"))
    assert root is not None
    out = []
    for e in mod_entries(root):
        enabled = [p for p in e if p.get("name") == "Enabled"][0].get("value")
        out.append((entry_name(e), enabled))
    return out


def _write_modlist(profile: Path, lines: list[str]) -> Path:
    modlist = profile / "modlist.txt"
    modlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return modlist


def test_order_follows_modlist_top_first() -> None:
    owners = {"Beta": {"ModB"}, "Alpha": {"ModA"}, "Gamma": {"ModB"}}
    assert resolve_mod_order(_mods("ModB", "ModA"), owners) == [
        "Beta", "Gamma", "Alpha"]


def test_shared_folder_goes_to_highest_priority_mod() -> None:
    owners = {"Shared": {"ModA", "ModB"}, "Solo": {"ModA"}}
    assert resolve_mod_order(_mods("ModB", "ModA"), owners) == [
        "Shared", "Solo"]


def test_overwrite_outranks_every_mod() -> None:
    owners = {"FromOverwrite": {"[Overwrite]"}, "Alpha": {"ModA"}}
    assert resolve_mod_order(_mods("[Overwrite]", "ModA"), owners) == [
        "FromOverwrite", "Alpha"]


def test_unlisted_owner_folders_are_appended_not_dropped() -> None:
    owners = {"Alpha": {"ModA"}, "Stray": {"NotInModlist"}}
    assert resolve_mod_order(_mods("ModA"), owners) == ["Alpha", "Stray"]


def test_write_uses_modlist_and_ignores_disabled_and_separators() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, [
            "-Visuals_separator", "+ModB", "-ModOff", "+ModA"])
        settings = tmp / "GCMODSETTINGS.MXML"
        count = write_gcmodsettings(
            settings, modlist,
            {"Alpha": {"ModA"}, "Beta": {"ModB"}, "Off": {"ModOff"}})
        assert count == 3
        assert _written_names(settings) == [
            ("BETA", "true"), ("ALPHA", "true"), ("OFF", "true")]
        assert settings.read_bytes().startswith(b"\xef\xbb\xbf<?xml")


def test_preserved_entries_follow_managed_with_flags_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModA"])
        original = tmp / "original.mxml"
        original.write_bytes(_game_file([
            ("HAND ONE", "true"), ("GONE FOLDER", "true"),
            ("HAND TWO", "false")]))
        settings = tmp / "GCMODSETTINGS.MXML"
        write_gcmodsettings(
            settings, modlist, {"Alpha": {"ModA"}},
            preserved_settings=original,
            unmanaged_folders={"Hand One", "Hand Two"})
        assert _written_names(settings) == [
            ("ALPHA", "true"), ("HAND ONE", "true"), ("HAND TWO", "false")]


def test_case_insensitive_match_reuses_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModA"])
        original = tmp / "original.mxml"
        original.write_bytes(_game_file([("MYMOD", "false")]))
        settings = tmp / "GCMODSETTINGS.MXML"
        write_gcmodsettings(
            settings, modlist, {"MyMod": {"ModA"}},
            preserved_settings=original, unmanaged_folders=set())
        assert _written_names(settings) == [("MYMOD", "true")]


def test_managed_folder_is_never_duplicated_as_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModA"])
        original = tmp / "original.mxml"
        original.write_bytes(_game_file([("ALPHA", "true")]))
        settings = tmp / "GCMODSETTINGS.MXML"
        write_gcmodsettings(
            settings, modlist, {"Alpha": {"ModA"}},
            preserved_settings=original, unmanaged_folders={"Alpha"})
        assert _written_names(settings) == [("ALPHA", "true")]


def test_unparseable_original_is_warned_and_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModA"])
        original = tmp / "original.mxml"
        original.write_text("<<< corrupted", encoding="utf-8")
        settings = tmp / "GCMODSETTINGS.MXML"
        logs: list[str] = []
        write_gcmodsettings(
            settings, modlist, {"Alpha": {"ModA"}}, log_fn=logs.append,
            preserved_settings=original, unmanaged_folders={"Hand"})
        assert _written_names(settings) == [("ALPHA", "true")]
        assert any("WARNING" in line for line in logs)


def test_disable_all_mods_is_carried_over() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModA"])
        original = tmp / "original.mxml"
        original.write_bytes(_game_file([]).replace(
            b'"DisableAllMods" value="false"', b'"DisableAllMods" value="true"'))
        settings = tmp / "GCMODSETTINGS.MXML"
        write_gcmodsettings(settings, modlist, {"Alpha": {"ModA"}},
                            preserved_settings=original)
        root = parse_gcmodsettings(settings.read_text(encoding="utf-8-sig"))
        assert disable_all_mods(root) == "true"


_HANDLER_PATH = _SRC_ROOT / "Games" / "No Man's Sky" / "no_mans_sky.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "Games._nms_selftest_handler", str(_HANDLER_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings_file(game: Path) -> Path:
    path = game / "Binaries" / "SETTINGS" / "GCMODSETTINGS.MXML"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_backup_then_restore_puts_original_back_exactly() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        profile.mkdir()
        settings = _settings_file(tmp / "game")
        settings.write_bytes(_POPULATED)
        backup = nms._backup_settings(profile, settings, lambda _m: None)
        assert backup is not None and backup.read_bytes() == _POPULATED
        settings.write_bytes(_EMPTY)          # what a deploy would write
        nms._record_generated_settings(profile, settings)
        assert nms._restore_settings(profile, settings, lambda _m: None)
        assert settings.read_bytes() == _POPULATED
        assert not (profile / nms._SETTINGS_STATE).exists()
        assert not (profile / nms._SETTINGS_BACKUP).exists()


def test_restore_removes_generated_file_when_there_was_no_original() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        profile.mkdir()
        settings = _settings_file(tmp / "game")
        assert nms._backup_settings(profile, settings, lambda _m: None) is None
        settings.write_bytes(_EMPTY)
        nms._record_generated_settings(profile, settings)
        assert nms._restore_settings(profile, settings, lambda _m: None)
        assert not settings.exists()


def test_second_backup_reuses_original() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        profile.mkdir()
        settings = _settings_file(tmp / "game")
        settings.write_bytes(_POPULATED)
        nms._backup_settings(profile, settings, lambda _m: None)
        settings.write_bytes(_EMPTY)          # generated by the first deploy
        backup = nms._backup_settings(profile, settings, lambda _m: None)
        assert backup is not None and backup.read_bytes() == _POPULATED


def test_runtime_modified_file_is_kept_as_recovery_copy() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        profile.mkdir()
        settings = _settings_file(tmp / "game")
        settings.write_bytes(_POPULATED)
        nms._backup_settings(profile, settings, lambda _m: None)
        settings.write_bytes(_EMPTY)
        nms._record_generated_settings(profile, settings)
        runtime = _game_file([("CHANGED IN GAME", "false")])
        settings.write_bytes(runtime)         # the game edited it
        assert nms._restore_settings(profile, settings, lambda _m: None)
        assert settings.read_bytes() == _POPULATED
        assert (profile / "nms_gcmodsettings_runtime.mxml").read_bytes() == runtime


def test_tampered_backup_is_refused() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        profile = tmp / "profile"
        profile.mkdir()
        settings = _settings_file(tmp / "game")
        settings.write_bytes(_POPULATED)
        nms._backup_settings(profile, settings, lambda _m: None)
        (profile / nms._SETTINGS_BACKUP).write_bytes(b"tampered")
        logs: list[str] = []
        assert not nms._restore_settings(profile, settings, logs.append)
        assert any("integrity" in line for line in logs)
        state = json.loads((profile / nms._SETTINGS_STATE).read_text())
        assert state["had_original"] is True


def _fake_game(root: Path):
    return SimpleNamespace(get_game_path=lambda: root,
                           get_prefix_path=lambda: None)


def _deploy_entry(dest: str, mod: str, target: str = "game"):
    return SimpleNamespace(target=target, destination=dest, mod_name=mod)


def test_deployed_folders_map_to_owning_mods() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mods_dir = root / "GAMEDATA" / "MODS"
        owners = nms.deployed_nms_folders(_fake_game(root), mods_dir, [
            _deploy_entry("GAMEDATA/MODS/Alpha/a.MBIN", "ModA"),
            _deploy_entry("GAMEDATA/MODS/Alpha/sub/b.MBIN", "ModB"),
            _deploy_entry("gamedata/mods/Beta/c.lua", "ModB"),
            _deploy_entry("Binaries/SETTINGS/other.txt", "ModC"),
            _deploy_entry("GAMEDATA/MODS/Gamma/d.MBIN", "ModD", target="prefix"),
        ])
        assert owners == {"Alpha": {"ModA", "ModB"}, "Beta": {"ModB"}}


def test_loose_files_in_mods_root_are_ignored() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owners = nms.deployed_nms_folders(
            _fake_game(root), root / "GAMEDATA" / "MODS",
            [_deploy_entry("GAMEDATA/MODS/readme.txt", "ModA")])
        assert owners == {}


def test_folder_case_variants_merge_into_first_spelling() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owners = nms.deployed_nms_folders(
            _fake_game(root), root / "GAMEDATA" / "MODS", [
                _deploy_entry("GAMEDATA/MODS/MyMod/a", "ModA"),
                _deploy_entry("GAMEDATA/MODS/MYMOD/b", "ModB")])
        assert owners == {"MyMod": {"ModA", "ModB"}}


def test_unmanaged_folders_come_from_core_backup_when_present() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        gamedata = Path(tmp) / "GAMEDATA"
        (gamedata / "MODS" / "Alpha").mkdir(parents=True)
        (gamedata / "MODS" / "HandInMods").mkdir()
        (gamedata / "MODS_Core" / "HandOne").mkdir(parents=True)
        (gamedata / "MODS_Core" / "alpha").mkdir()
        (gamedata / "MODS_Core" / "loose.txt").write_text("x")
        result = nms.unmanaged_nms_folders(
            gamedata / "MODS", {"Alpha": {"ModA"}})
        assert result == {"HandOne"}


def test_unmanaged_folders_fall_back_to_mods_dir_without_core() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        mods = Path(tmp) / "GAMEDATA" / "MODS"
        (mods / "HandOne").mkdir(parents=True)
        (mods / "Alpha").mkdir()
        assert nms.unmanaged_nms_folders(mods, {"Alpha": {"ModA"}}) == {"HandOne"}


def test_handler_identity_matches_the_custom_definition() -> None:
    nms = _load_handler()
    d = nms.NMS_DEFINITION
    assert d["name"] == "No Man's Sky"
    assert d["game_id"] == "No_Man_s_Sky"
    assert d["deploy_type"] == "standard"
    assert d["mod_data_path"] == "GAMEDATA/MODS"
    assert d["nexus_game_domain"] == "nomanssky"
    assert d["steam_id"] == "275850"
    assert issubclass(nms.NoMansSky, nms.StandardCustomGame)
    for name in ("deploy", "restore", "post_clean_game_folder"):
        assert name in vars(nms.NoMansSky), name


def _make_handler(nms, tmp: Path, *, vfs: bool = False,
                  disable_all: bool = False):
    """A NoMansSky instance wired to temp dirs, bypassing load_paths()."""
    game_root = tmp / "game"
    (game_root / "GAMEDATA" / "MODS").mkdir(parents=True, exist_ok=True)
    profile_root = tmp / "profiles_root"
    profile_dir = profile_root / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "modlist.txt").write_text("+ModA\n", encoding="utf-8")
    game = nms.NoMansSky.__new__(nms.NoMansSky)
    game._defn = dict(nms.NMS_DEFINITION)
    game._game_path = game_root
    game._prefix_path = None
    game._staging_path = None
    game._active_profile_dir = profile_dir
    game.get_profile_root = lambda: profile_root
    game.get_effective_root_folder_path = lambda: tmp / "Root_Folder"
    game._disable_all_mods = lambda: disable_all   # launch toggle, no config IO
    vfs_patch = patch.object(nms.NoMansSky, "vfs_launch_enabled",
                             new_callable=PropertyMock, return_value=vfs)
    return game, profile_dir, vfs_patch


def _alpha_entries():
    return [_deploy_entry("GAMEDATA/MODS/Alpha/a.MBIN", "ModA")]


def _patched_filegraph(entries_fn):
    import Utils.filegraph.deploy as fg
    return (patch.object(fg, "current", lambda: object()),
            patch.object(fg, "entries", lambda **_kw: entries_fn()))


def test_physical_deploy_writes_settings_then_restore_puts_original_back() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, profile_dir, vfs_patch = _make_handler(nms, tmp)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)
        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None), \
                patch.object(nms.StandardCustomGame, "restore", lambda *a, **k: None):
            game.deploy(log_fn=lambda _m: None)
            assert _written_names(settings) == [("ALPHA", "true")]
            state = json.loads((profile_dir / nms._SETTINGS_STATE).read_text())
            assert state["generated_sha256"]
            assert game.pop_deploy_warnings() == []
            game.restore(log_fn=lambda _m: None)
        assert settings.read_bytes() == _POPULATED


def test_settings_write_failure_becomes_deploy_warning() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(nms, tmp)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)

        def _boom():
            raise RuntimeError("plan unavailable")

        cur, ent = _patched_filegraph(_boom)
        logs: list[str] = []
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None):
            game.deploy(log_fn=logs.append)
        assert settings.read_bytes() == _POPULATED
        assert any("GCMODSETTINGS" in w for w in game.pop_deploy_warnings())
        assert any("WARN" in line for line in logs)


def test_vfs_deploy_writes_into_view_and_leaves_real_game_untouched() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, profile_dir, vfs_patch = _make_handler(nms, tmp, vfs=True)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)
        view = tmp / "view"
        view_settings = view / "Binaries" / "SETTINGS" / "GCMODSETTINGS.MXML"
        view_settings.parent.mkdir(parents=True)
        os.link(settings, view_settings)      # the view hardlinks the game root

        def _fake_vfs_deploy(self, log_fn=None, **_kw):
            self._vfs_post_view_build(view_root=view, profile="default",
                                      filemap=None, staging=None, log_fn=log_fn)

        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", _fake_vfs_deploy):
            game.deploy(log_fn=lambda _m: None)
        assert settings.read_bytes() == _POPULATED
        assert _written_names(view_settings) == [("ALPHA", "true")]
        assert not (profile_dir / nms._SETTINGS_STATE).exists()


def test_root_folder_copy_skips_generation_with_warning() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(nms, tmp)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)
        stale = tmp / "Root_Folder" / "Binaries" / "SETTINGS" / "gcmodsettings.mxml"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(_EMPTY)
        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None):
            game.deploy(log_fn=lambda _m: None)
        assert settings.read_bytes() == _POPULATED
        assert any("Root_Folder" in w for w in game.pop_deploy_warnings())


def test_corrupt_original_raises_deploy_warning() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(nms, tmp)
        settings = _settings_file(game._game_path)
        settings.write_text("<<< corrupted", encoding="utf-8")
        cur, ent = _patched_filegraph(_alpha_entries)
        logs: list[str] = []
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None):
            game.deploy(log_fn=logs.append)
        assert _written_names(settings) == [("ALPHA", "true")]
        warnings = game.pop_deploy_warnings()
        assert len(warnings) == 1 and "could not be read" in warnings[0]
        assert any(f"WARNING: {warnings[0]}" in line for line in logs)


def _written_disable_all(path: Path) -> str:
    root = parse_gcmodsettings(path.read_text(encoding="utf-8-sig"))
    assert root is not None
    return disable_all_mods(root)


def test_disable_all_mods_launch_toggle_is_declared() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        game, _profile_dir, _vfs = _make_handler(nms, Path(tmp))
        toggles = game.launch_toggles
        assert [t.key for t in toggles] == ["disable_all_mods"]
        assert toggles[0].default is False
        assert "Disable all mods" in toggles[0].label


def test_disable_all_toggle_on_is_written_on_physical_deploy() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(nms, tmp, disable_all=True)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)
        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None):
            game.deploy(log_fn=lambda _m: None)
        assert _written_disable_all(settings) == "true"
        assert _written_names(settings) == [("ALPHA", "true")]


def test_disable_all_toggle_on_is_written_into_vfs_view() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(
            nms, tmp, vfs=True, disable_all=True)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED)
        view_settings = tmp / "view" / "Binaries" / "SETTINGS" / "GCMODSETTINGS.MXML"

        def _fake_vfs_deploy(self, log_fn=None, **_kw):
            self._vfs_post_view_build(view_root=tmp / "view", profile="default",
                                      filemap=None, staging=None, log_fn=log_fn)

        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", _fake_vfs_deploy):
            game.deploy(log_fn=lambda _m: None)
        assert _written_disable_all(view_settings) == "true"
        assert settings.read_bytes() == _POPULATED


def test_disable_all_toggle_off_overrides_original_true() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        game, _profile_dir, vfs_patch = _make_handler(nms, tmp, disable_all=False)
        settings = _settings_file(game._game_path)
        settings.write_bytes(_POPULATED.replace(
            b'"DisableAllMods" value="false"', b'"DisableAllMods" value="true"'))
        cur, ent = _patched_filegraph(_alpha_entries)
        with vfs_patch, cur, ent, \
                patch.object(nms.StandardCustomGame, "deploy", lambda *a, **k: None):
            game.deploy(log_fn=lambda _m: None)
        assert _written_disable_all(settings) == "false"


def test_builtin_handler_is_not_a_custom_game() -> None:
    nms = _load_handler()
    with tempfile.TemporaryDirectory() as tmp:
        game, _profile_dir, _vfs = _make_handler(nms, Path(tmp))
        assert game.is_custom is False


def test_priority_log_line_states_which_end_wins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        modlist = _write_modlist(tmp, ["+ModB", "+ModA"])
        logs: list[str] = []
        write_gcmodsettings(tmp / "GCMODSETTINGS.MXML", modlist,
                            {"Alpha": {"ModA"}, "Beta": {"ModB"}},
                            log_fn=logs.append)
        assert "  Mod priority (ModPriority 0 wins): BETA, ALPHA" in logs


def main() -> None:
    test_roundtrip_populated_file_is_byte_exact()
    test_roundtrip_empty_file_is_byte_exact()
    test_parse_reads_names_in_priority_order()
    test_parse_rejects_garbage_and_foreign_documents()
    test_new_entry_matches_game_template()
    test_build_renumbers_index_and_priority()
    test_special_characters_are_escaped()
    test_order_follows_modlist_top_first()
    test_shared_folder_goes_to_highest_priority_mod()
    test_overwrite_outranks_every_mod()
    test_unlisted_owner_folders_are_appended_not_dropped()
    test_write_uses_modlist_and_ignores_disabled_and_separators()
    test_preserved_entries_follow_managed_with_flags_kept()
    test_case_insensitive_match_reuses_entry()
    test_managed_folder_is_never_duplicated_as_preserved()
    test_unparseable_original_is_warned_and_skipped()
    test_disable_all_mods_is_carried_over()
    test_priority_log_line_states_which_end_wins()
    test_backup_then_restore_puts_original_back_exactly()
    test_restore_removes_generated_file_when_there_was_no_original()
    test_second_backup_reuses_original()
    test_runtime_modified_file_is_kept_as_recovery_copy()
    test_tampered_backup_is_refused()
    test_deployed_folders_map_to_owning_mods()
    test_loose_files_in_mods_root_are_ignored()
    test_folder_case_variants_merge_into_first_spelling()
    test_unmanaged_folders_come_from_core_backup_when_present()
    test_unmanaged_folders_fall_back_to_mods_dir_without_core()
    test_handler_identity_matches_the_custom_definition()
    for check in (
        test_physical_deploy_writes_settings_then_restore_puts_original_back,
        test_settings_write_failure_becomes_deploy_warning,
        test_vfs_deploy_writes_into_view_and_leaves_real_game_untouched,
        test_root_folder_copy_skips_generation_with_warning,
        test_builtin_handler_is_not_a_custom_game,
        test_corrupt_original_raises_deploy_warning,
        test_disable_all_mods_launch_toggle_is_declared,
        test_disable_all_toggle_on_is_written_on_physical_deploy,
        test_disable_all_toggle_on_is_written_into_vfs_view,
        test_disable_all_toggle_off_overrides_original_true,
    ):
        try:
            check()
            print(f"ok    {check.__name__}")
        except Exception as exc:
            print(f"FAIL  {check.__name__}: {type(exc).__name__}: {exc}")
            raise
    print("ok  gcmodsettings format, order, backup and handler")


if __name__ == "__main__":
    main()
