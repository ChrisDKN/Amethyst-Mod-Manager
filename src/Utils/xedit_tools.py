"""
GUI-neutral helpers for the xEdit-family wizard tools (SSEEdit / FO4Edit /
FNVEdit / … + QuickAutoClean, TexGen, DynDOLOD, xLODGen).

Moved out of wizards/sseedit.py and wizards/dyndolod.py (which import
customtkinter) so the Qt wizard views can share them. Prefix resolution and
the plugins.txt / My Games links live in Utils.exe_launch; archive
download/locate/extract lives in Utils.wizard_archives.
"""

from __future__ import annotations

import hashlib
import os
import re as _re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.atomic_write import write_atomic_text

if TYPE_CHECKING:
    from Games.base_game import BaseGame


def _noop(_msg: str) -> None:
    pass


# xEdit / QuickAutoClean can save the cleaned plugin to a temp file and queue
# the rename to the real name "on shutdown" (e.g.
# ``AlternatePerspective.esp.save.2026_06_19_00_38_14`` -> ``…esp``).  Matches
# ``<plugin>.save.<timestamp>`` so finalize_xedit_saves can complete the rename.
XEDIT_SAVE_TEMP_RE = _re.compile(r"^(?P<base>.+)\.save\.[0-9_]+$", _re.IGNORECASE)
_PLUGIN_EXTS = (".esp", ".esm", ".esl")


@dataclass
class XEditVFSSession:
    """Private plugin baseline used while xEdit runs in a VFS view."""

    data_dir: Path
    staging_root: Path
    overwrite_dir: Path
    filemap_path: Path
    strip_prefixes: set[str]
    baseline: dict[str, tuple[str, str]]


def _plugin_digest(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=20)
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plugin_files(data_dir: Path) -> list[Path]:
    try:
        return [
            entry for entry in data_dir.iterdir()
            if entry.is_file()
            and entry.name.lower().endswith(_PLUGIN_EXTS)
        ]
    except OSError:
        return []


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.amm-xedit-", dir=destination.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp, follow_symlinks=True)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def begin_xedit_vfs_session(game: "BaseGame", log_fn=None) -> "XEditVFSSession | None":
    """Privatise deployed plugins and remember their contents before xEdit.

    Shadow views are assembled with hardlinks for speed. xEdit is specifically
    expected to modify existing files, so each top-level plugin is replaced by
    a private copy first; an in-place save can then never mutate a vanilla or
    staging inode. Returns ``None`` for an ordinary physical deployment.
    """
    _log = log_fn or _noop
    from Utils.vfs import effective_shadow_data_root, manifest_path

    if not manifest_path(game).is_file():
        return None
    data_dir = effective_shadow_data_root(game)
    baseline: dict[str, tuple[str, str]] = {}
    plugins = _plugin_files(data_dir)
    for plugin in plugins:
        baseline[plugin.name.lower()] = (plugin.name, _plugin_digest(plugin))
    for plugin in plugins:
        _copy_file_atomic(plugin, plugin)
    if plugins:
        _log(
            f"xEdit VFS: privatised {len(plugins)} deployed plugin(s) "
            "before launch."
        )
    return XEditVFSSession(
        data_dir=data_dir,
        staging_root=Path(game.get_effective_mod_staging_path()),
        overwrite_dir=Path(game.get_effective_overwrite_path()),
        filemap_path=Path(game.get_effective_filemap_path()),
        strip_prefixes={
            str(prefix).lower()
            for prefix in (getattr(game, "mod_folder_strip_prefixes", None)
                           or {"data"})
        },
        baseline=baseline,
    )


def persist_xedit_vfs_changes(
    game: "BaseGame", session: "XEditVFSSession | None", log_fn=None,
) -> int:
    """Persist plugins changed in a private xEdit view into profile storage.

    A deployed mod's plugin is copied back to its owning staging folder and
    tagged exactly like physical restore's xEdit rescue. New or vanilla-owned
    plugins go to ``[Overwrite]`` so the real game installation stays intact.
    The baseline is refreshed, allowing QAC All to persist after every pass.
    """
    if session is None:
        return 0
    _log = log_fn or _noop
    current: dict[str, tuple[str, str, Path]] = {}
    for plugin in _plugin_files(session.data_dir):
        current[plugin.name.lower()] = (
            plugin.name, _plugin_digest(plugin), plugin)

    owners: dict[str, str] = {}
    try:
        with session.filemap_path.open(
                encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                rel, sep, mod_name = line.rstrip("\n").partition("\t")
                if sep and "/" not in rel.replace("\\", "/"):
                    owners[rel.lower()] = mod_name
    except OSError:
        pass

    from Utils.deploy_shared import _OVERWRITE_NAME, _get_staging_source_path
    from Utils.deploy_standard import _tag_mod_xedit_modified

    changed = 0
    index_cache: dict[Path, dict] = {}
    for key, (name, digest, source) in current.items():
        old = session.baseline.get(key)
        if old is not None and old[1] == digest:
            continue
        if source.stat().st_size == 0:
            _log(f"xEdit VFS: refusing to persist empty plugin {name}.")
            continue

        owner = owners.get(key)
        if owner and owner != _OVERWRITE_NAME:
            mod_root = session.staging_root / owner
            destination = _get_staging_source_path(
                mod_root, name, session.strip_prefixes,
                index_cache=index_cache)
            if destination is None:
                destination = mod_root / name
            _copy_file_atomic(source, destination)
            _tag_mod_xedit_modified(mod_root, name)
            _log(f"xEdit VFS: saved {name} back to '{owner}'.")
        else:
            destination = session.overwrite_dir / name
            _copy_file_atomic(source, destination)
            _log(f"xEdit VFS: saved {name} to [Overwrite].")
        changed += 1

    session.baseline = {
        key: (name, digest) for key, (name, digest, _path) in current.items()
    }
    return changed


def finalize_xedit_saves(data_dir: Path, log_fn=None) -> int:
    """Complete any pending xEdit ``<plugin>.save.<timestamp>`` renames in
    *data_dir* so the cleaned plugin sits at its real name before we rebuild the
    filemap/index.  Returns the number of temps finalised.

    Only acts on the top level of Data/ (where xEdit writes plugins).  If both a
    temp and the base name exist, the temp wins (it is the freshly-saved copy)
    and replaces the base via ``os.replace`` (atomic, clobbers a stale symlink).
    """
    _log = log_fn or _noop
    finalised = 0
    try:
        entries = list(data_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        m = XEDIT_SAVE_TEMP_RE.match(entry.name)
        if m is None:
            continue
        # Only finalise temps for actual plugins; ignore unrelated ".save." names.
        base_name = m.group("base")
        if not base_name.lower().endswith((".esp", ".esm", ".esl")):
            continue
        base_path = entry.with_name(base_name)
        try:
            os.replace(str(entry), str(base_path))
            finalised += 1
            _log(f"Finalised xEdit save: {entry.name} -> {base_name}")
        except OSError as exc:
            _log(f"WARN: could not finalise xEdit save {entry.name}: {exc}")
    return finalised


def applications_dir(game: "BaseGame", app_dir: str) -> Path:
    """Profiles/<game>/Applications/<app_dir>/ - where wizard tools extract."""
    return game.get_mod_staging_path().parent / "Applications" / app_dir


def tool_exe_path(game: "BaseGame", exe_name: str, app_dir: str) -> Path | None:
    p = applications_dir(game, app_dir) / exe_name
    return p if p.is_file() else None


def flatten_subdirs(dest: Path, exe_name: str) -> None:
    """Collapse single-subdir wrappers until exe_name is at the top level."""
    while True:
        all_entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
        subdirs = [e for e in all_entries if e.is_dir()]
        if len(subdirs) == 1 and not (dest / exe_name).is_file():
            wrapper = subdirs[0]
            tmp = dest.parent / (dest.name + "_flatten_tmp")
            wrapper.rename(tmp)
            for item in tmp.iterdir():
                shutil.move(str(item), str(dest / item.name))
            tmp.rmdir()
        else:
            break


def set_winxp_compat(prefix_path: Path, exe: Path, log_fn=None) -> None:
    """Set the Wine per-app Windows version for *exe* to Windows XP.

    This writes the same entry that winecfg writes when you select an
    application and change its Windows Version to "Windows XP":
        HKCU\\Software\\Wine\\AppDefaults\\<exe.name>  "Version"="winxp"
    in user.reg.
    """
    import time as _time

    _log = log_fn or _noop

    # Accept either pfx/ directly or its compatdata parent
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping WinXP version flag.")
        return

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return

    section_header = f"[Software\\\\Wine\\\\AppDefaults\\\\{exe.name}]"
    lines = text.splitlines(keepends=True)

    section_start: int | None = None
    section_end: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    _filetime_hex = format(int((_time.time() + 11644473600) * 1e7), "x")
    entry_line = '"Version"="winxp"\n'

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_filetime_hex}\n")
        lines.append(f"#time={_filetime_hex}\n")
        lines.append(entry_line)
        _log(f"SSEEdit: set Windows version to WinXP for {exe.name}.")
    else:
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        lines[section_start] = f"{section_header} {_filetime_hex}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break

        found = False
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith('"version"='):
                if kline.strip() != entry_line.strip():
                    key_lines[j] = entry_line
                    _log(f"SSEEdit: updated Windows version to WinXP for {exe.name}.")
                found = True
                break
        if not found:
            key_lines.append(entry_line)
            _log(f"SSEEdit: set Windows version to WinXP for {exe.name}.")

        lines[body_start:body_end] = key_lines

    try:
        write_atomic_text(user_reg, "".join(lines))
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")


def xedit_settings_ext(xedit_name: str) -> str:
    """Derive the xEdit viewsettings extension from the build name.

    xEdit names its per-game settings file ``Plugins.<mode>viewsettings``
    where ``<mode>`` is the build name minus the trailing ``Edit``, lowercased:
      FO4Edit  -> fo4   (Plugins.fo4viewsettings)
      SSEEdit  -> sse   (Plugins.sseviewsettings)
      TES4Edit -> tes4  (Plugins.tes4viewsettings)
      SF1Edit  -> sf1   (Plugins.sf1viewsettings)
    """
    name = xedit_name
    if name.lower().endswith("edit"):
        name = name[: -len("edit")]
    return name.lower()


def seed_xedit_viewsettings(game: "BaseGame", pfx: Path, xedit_name: str, log_fn=None) -> None:
    """Pre-create the xEdit viewsettings file so the first-run messages
    ("What's New" + developer message) never appear in a fresh tool prefix.

    A fresh prefix has no ``Plugins.<mode>viewsettings`` next to the game's
    AppData/Local data dir, so xEdit shows its nag dialogs every time the
    prefix is recreated. Seeding the gate keys suppresses them:

      [Options]          ShowTip=0            - no Tip of the Day on startup
      [WhatsNew]         Version=<very high>  - newer than any running build
      [DeveloperMessage] LastShownOn=<far-future Delphi date serial>

    ``LastShownOn`` is a Delphi ``TDateTime`` integer (days since 1899-12-30);
    xEdit re-shows the message when it is older than today, so we write a
    date well in the future. Skips if a settings file already exists (the
    user's real layout/preferences must win).
    """
    _log = log_fn or _noop

    subpath = getattr(game, "_APPDATA_SUBPATH", None)
    if subpath is None:
        return
    data_dir = pfx / subpath
    ext = xedit_settings_ext(xedit_name)
    settings_file = data_dir / f"Plugins.{ext}viewsettings"

    if settings_file.exists():
        return  # real settings already present - don't clobber

    # Far-future Delphi date serial (1899-12-30 epoch) so the developer
    # message stays dismissed: 2099-01-01 -> 72686.
    last_shown = 72686
    content = (
        "[Options]\r\n"
        "ShowTip=0\r\n"
        "\r\n"
        "[WhatsNew]\r\n"
        "Version=99999999\r\n"
        "\r\n"
        "[DeveloperMessage]\r\n"
        f"LastShownOn={last_shown}\r\n"
        "Version=99999999\r\n"
    )
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        write_atomic_text(settings_file, content)
        _log(f"seeded {settings_file.name} to suppress first-run messages")
    except OSError as exc:
        _log(f"could not seed {settings_file.name}: {exc}")


def restore_after_xedit(game: "BaseGame", display_name: str, log_fn=None) -> None:
    """Fully un-deploy the game (Data/ + Root_Folder) after xEdit closes,
    moving any plugin xEdit edited in Data/ back into its owning mod folder
    BEFORE the panel refresh rescans staging.

    This mirrors the Restore button: ``game.restore()`` undoes the Data/
    deploy, then ``restore_root_folder()`` removes the root-deployed files
    from the game directory.  Earlier this only ran ``game.restore()``,
    leaving root-folder deployed files behind in the game install.

    QuickAutoClean backs the original plugin up into ``Data/<tool> Backups/``
    - which, under symlink-mode deploy, consumes the staging copy (the Data
    entry was a symlink into staging) - and writes the cleaned plugin as a
    fresh regular file in Data/.  If we let the wizard-close reload
    rescan staging now, ``rebuild_mod_index`` finds the mod folder missing
    its plugin and drops it from the index; the next Restore then can't
    recognise the cleaned file and buries it in overwrite/.

    Running ``restore()`` first walks Data/ while the index still knows the
    plugin, so its orphan-rescue moves the cleaned file back into the mod
    folder.  The rescan that follows then sees the restored plugin and keeps
    it.  No-op for games without ``restore``; failures are logged, not
    raised (a normal redeploy/restore can still recover).
    """
    _log = log_fn or _noop
    if not hasattr(game, "restore"):
        return
    name = display_name
    # Restore against the last-deployed profile so the cleaned file lands in
    # the right mod staging folder (mirrors the Deploy/Restore button flow).
    saved_profile_dir = getattr(game, "_active_profile_dir", None)
    restored_ok = False
    try:
        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / last_deployed
            )
            # Reload so the last-deployed profile's path overrides apply.
            game.load_paths()
        try:
            game.restore(log_fn=lambda m: _log(f"{name} Wizard: {m}"))
            # Restore Root_Folder too, so root-deployed files are removed
            # from the game directory exactly like the Restore button does
            # (game.restore only handles the Data/ deploy).
            from Utils.deploy import restore_root_folder_for_game
            root_folder_dir = game.get_effective_root_folder_path()
            game_root = game.get_game_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder_for_game(
                    game,
                    root_folder_dir=root_folder_dir,
                    game_root=game_root,
                    log_fn=lambda m: _log(f"{name} Wizard: {m}"),
                )
            restored_ok = True
        except RuntimeError as exc:
            _log(f"{name} Wizard: restore skipped: {exc}")
    except Exception as exc:
        _log(f"{name} Wizard: post-edit restore failed: {exc}")
    finally:
        # Leave the active profile exactly as we found it so the panel reload
        # rebuilds the profile the user actually has selected.
        if saved_profile_dir is not None:
            try:
                game.set_active_profile_dir(saved_profile_dir)
                game.load_paths()
            except Exception:
                pass
    # The game is no longer deployed - drop the deploy-active flag so the
    # profile dropdown loses its green "deployed" highlight (mirrors what the
    # Restore button does).  The caller refreshes the UI afterwards.
    if restored_ok:
        try:
            game.clear_deploy_active()
        except Exception:
            pass


def collect_dirty_plugins(game: "BaseGame") -> "list[tuple[str, str]]":
    """Return [(plugin_name, summary), ...] for plugins LOOT flags as dirty.

    Reads ``loot.json`` from the active profile dir (the same data the
    Plugins panel uses for its brush icon), so QuickAutoClean users can see
    which plugins need cleaning without closing the wizard to read the panel
    underneath it.  Empty list if LOOT has never run or nothing is dirty.
    """
    profile_dir = getattr(game, "_active_profile_dir", None)
    if profile_dir is None:
        return []
    try:
        from LOOT.loot_sorter import read_loot_info
        data = read_loot_info(profile_dir)
    except Exception:
        return []
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    version = data.get("version", 1) if isinstance(data, dict) else 1
    if version < 2 or not isinstance(plugins, dict):
        # v1 stored only a raw message list with no CRC-matched dirty data.
        return []
    out: "list[tuple[str, str]]" = []
    for name, info in plugins.items():
        if not isinstance(info, dict):
            continue
        dirty = info.get("dirty") or []
        if not dirty:
            continue
        parts: list[str] = []
        for d in dirty:
            if not isinstance(d, dict):
                continue
            bits = []
            if d.get("itm"):
                bits.append(f"{d['itm']} ITM")
            if d.get("udr"):
                bits.append(f"{d['udr']} UDR")
            if d.get("nav"):
                bits.append(f"{d['nav']} deleted navmesh")
            if bits:
                parts.append(", ".join(bits))
        summary = "; ".join(parts) if parts else "needs cleaning"
        out.append((name, summary))
    out.sort(key=lambda t: t[0].lower())
    return out


def prepare_xedit_prefix(
    game: "BaseGame",
    compat_data: Path,
    proton_script: Path,
    env: dict,
    *,
    xedit_name: "str | None" = None,
    exe: "Path | None" = None,
    log_fn=None,
) -> None:
    """Prepare a tool prefix for an xEdit-based tool, pre-launch.

    Seeds the game's Installed Path into the prefix registry (xEdit reads it
    from HKLM), links the deployed profile's plugins.txt into the prefix
    AppData and the game prefix's My Games folder (xEdit fatals without the
    game INI).  When *xedit_name*/*exe* are given (full xEdit / QAC, not the
    DynDOLOD tools) it also seeds the viewsettings file to suppress the
    first-run nags and sets the per-app WinXP compat flag.

    Blocking (the registry seed runs wine); call from a worker thread.
    """
    _log = log_fn or _noop
    pfx = compat_data / "pfx"

    from Utils.bethesda_registry import maybe_register_for_game
    maybe_register_for_game(
        prefix_dir=compat_data,
        proton_script=proton_script,
        env=env,
        game=game,
        log_fn=_log,
    )

    from Utils.exe_launch import link_mygames, link_plugins_txt
    link_plugins_txt(game, pfx, _log)
    link_mygames(game, pfx, _log)

    if xedit_name and exe is not None:
        seed_xedit_viewsettings(game, pfx, xedit_name, log_fn=_log)
        set_winxp_compat(compat_data, exe, log_fn=_log)
