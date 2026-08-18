"""Merge several mods' regulation.bin into one, for FROMSOFTWARE games.

Elden Ring stores every param table in a single ``regulation.bin``, so any two
mods that ship one conflict totally - me3 serves exactly one and silently drops
the rest, however unrelated their edits are.  Load order cannot help; the file
has to be merged.

This automates what the community does by hand in DSMapStudio: diff each mod's
regulation against vanilla, then replay every mod's edits onto one base file in
mod-list order.  The work is done by DSMSPortable (DSMapStudio's command-line
half), and the result is written back as a normal mod.

Three steps, matching every other tool wizard: install the tool into
Profiles/<game>/Applications/, let the user pick a Proton version and prefix
placement, then run.  The Proton step matters here - DSMSPortable needs a .NET 6
desktop runtime, and installing that into the *game's* prefix would pollute it,
so the default isolated prefix keeps it beside the exe.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import AMBER, GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_INSTALL, _PG_PROTON, _PG_RUN = 0, 1, 2

# Name of the mod the merge result is written into.
_OUTPUT_MOD = "Merged Regulation"


class RegulationMergeView(WizardViewBase):
    """Install DSMSPortable, choose a prefix, merge the regulation mods."""

    _log_sig = Signal(str)
    _status_sig = Signal(str, str)
    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Merge regulation.bin - {0}").format(
                             game.name))
        from Utils import dsms_portable as dsms
        self._proton_name = ""
        self._prefix_mode = ""
        self._sources: list = []
        self._exe = dsms.find_dsms(game)

        self._log_sig.connect(self._guard(self._append_log))
        self._status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))

        self._stack.addWidget(self._build_install_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page())

        self._refresh_sources()
        self._stack.setCurrentIndex(
            _PG_PROTON if self._exe is not None else _PG_INSTALL)
        if self._exe is not None:
            self._goto_step(_PG_PROTON)

    # ---- pages -----------------------------------------------------------------

    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Install DSMSPortable"))
        self._make_note(lay, self.tr(
            "Only one regulation.bin can be active, so mods that ship one "
            "override each other completely - even when they change unrelated "
            "things. Merging needs DSMSPortable (about 40 MB), which is "
            "installed into this game's Applications folder."))
        self._dl_status = self._make_status(lay)
        lay.addSpacing(8)
        self._install_btn = self._accent_btn(self.tr("Download and install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        lay.addStretch(1)
        return page

    def _build_run_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Merge"))
        self._make_note(lay, self.tr(
            "Every enabled mod's param edits are combined into one "
            "regulation.bin, written as the '{0}' mod. Enable it afterwards "
            "and disable the mods it replaces.").format(_OUTPUT_MOD))
        self._run_status = self._make_status(lay)

        p = active_palette()
        list_lbl = QLabel(self.tr("Mods contributing param edits "
                                  "(highest priority first):"))
        list_lbl.setStyleSheet(self._dim)
        lay.addWidget(list_lbl)
        self._list = QPlainTextEdit()
        self._list.setReadOnly(True)
        self._list.setMaximumHeight(96)
        self._list.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none;}}")
        lay.addWidget(self._list)

        self._merge_btn = self._accent_btn(self.tr("Merge into one mod"))
        self._merge_btn.clicked.connect(self._do_merge)
        lay.addWidget(self._merge_btn, 0, Qt.AlignHCenter)

        log_lbl = QLabel(self.tr("Log:"))
        log_lbl.setStyleSheet(self._dim)
        lay.addWidget(log_lbl)
        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none;}}")
        lay.addWidget(self._log_box, 1)

        self._done_btn = self._green_btn()
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    # ---- step flow -------------------------------------------------------------

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_PROTON:
            from Utils import dsms_portable as dsms
            self._enter_proton(
                self._exe, dsms.EXE_NAME, "DSMSPortable",
                self._on_proton_chosen,
                title=self.tr("Step 2: Choose Proton Version"),
                missing_text=self.tr(
                    "{0} was not found.\nReopen this wizard and let it "
                    "install DSMSPortable first.").format(dsms.EXE_NAME))
        elif idx == _PG_RUN:
            self._refresh_sources()

    def _start_install(self):
        from Utils import dsms_portable as dsms
        self._install_btn.setEnabled(False)

        def worker():
            ok = False
            try:
                ok = dsms.install_dsms(
                    self._game,
                    log_fn=lambda m: safe_emit(self._dl_status_sig, m, ""))
            except Exception as exc:
                safe_emit(self._dl_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
            safe_emit(self._dl_done_sig, ok)

        threading.Thread(target=worker, daemon=True, name="dsms-install").start()

    def _on_dl_done(self, ok: bool):
        from Utils import dsms_portable as dsms
        if ok:
            self._exe = dsms.find_dsms(self._game)
            self._goto_step(_PG_PROTON)
        else:
            self._install_btn.setEnabled(True)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- sources ---------------------------------------------------------------

    def _enabled_mod_dirs(self) -> list:
        """Enabled mods for the active profile, in mod-list order."""
        from Utils.modlist import read_modlist
        game = self._game
        staging = game.get_effective_mod_staging_path()
        profile = (getattr(self._ctx, "profile_name", None)
                   or game.get_last_active_profile() or "default")
        profile_dir = game.get_profile_root() / "profiles" / profile
        return [(e.name, staging / e.name)
                for e in read_modlist(profile_dir / "modlist.txt")
                if e.enabled and not e.is_separator]

    def _refresh_sources(self):
        from Utils import dsms_portable as dsms
        try:
            self._sources = dsms.find_regulation_sources(self._enabled_mod_dirs())
        except Exception as exc:
            self._set_status(self._run_status,
                             self.tr("Could not read the mod list: {0}").format(exc),
                             RED)
            return

        lines = []
        for src in self._sources:
            bits = []
            if src.regulation is not None:
                bits.append("regulation.bin")
            if src.csvs:
                bits.append(self.tr("{0} CSV(s)").format(len(src.csvs)))
            lines.append(f"  {src.name}  -  {', '.join(bits)}")
        self._list.setPlainText("\n".join(lines) or self.tr("  (none)"))

        n = len(self._sources)
        self._merge_btn.setEnabled(n > 1)
        if n > 1:
            self._set_status(self._run_status, self.tr(
                "{0} mods ship param edits - only one would survive without "
                "merging.").format(n), RED)
        elif n == 1:
            self._set_status(self._run_status, self.tr(
                "Only one mod ships param edits, so nothing conflicts."), GREEN)
        else:
            self._set_status(self._run_status,
                             self.tr("No enabled mod ships param edits."), "")

    def _append_log(self, msg: str):
        self._log_box.appendPlainText(msg)
        try:
            self._log(f"Regulation merge: {msg}")
        except Exception:
            pass

    # ---- merge -----------------------------------------------------------------

    def _do_merge(self):
        self._merge_btn.setEnabled(False)
        self._set_status(self._run_status, self.tr("Merging ..."), "")
        threading.Thread(target=self._worker, daemon=True,
                         name="regulation-merge").start()

    def _run_step(self, proton_script, exe, env, args, label: str,
                  out_lines: "list[str] | None" = None) -> bool:
        """One DSMSPortable invocation through the chosen prefix.

        *out_lines*, when given, receives the tool's output so the caller can
        inspect it (param versions, per-table export failures).
        """
        from Utils.exe_launch import run_tool_logged
        safe_emit(self._log_sig, f"$ {label}")
        lines: list[str] = out_lines if out_lines is not None else []

        def _log(msg: str) -> None:
            lines.append(msg)

        rc = run_tool_logged(proton_script, exe, env, _log,
                             extra_args=args, cwd=exe.parent,
                             label=f"DSMSPortable ({label})")
        # A bad argument makes it print its whole help text; surface only the
        # line that says what went wrong.
        bad = [ln for ln in lines if "Invalid switch" in ln]
        if bad:
            for ln in bad:
                safe_emit(self._log_sig, f"  {ln.strip()}")
            safe_emit(self._log_sig,
                      "  (DSMSPortable rejected an argument; help text hidden)")
            return False
        for ln in lines:
            if ln.strip():
                safe_emit(self._log_sig, f"  {ln.rstrip()}")
        if rc != 0:
            safe_emit(self._log_sig, f"  exited with code {rc}")
            if rc == 7:
                # "Timed out loading param files".  ParamBank's loader task is
                # queued without ever being observed, so ANY exception it throws
                # is swallowed and the load simply never completes - every cause
                # looks like this one timeout.  The usual culprit is a
                # regulation SoulsFormats refuses to decompress.
                safe_emit(self._log_sig, self.tr(
                    "  (DSMSPortable could not read a regulation.bin. Its "
                    "loader hides the real error and reports a timeout.)"))
            return False
        return True

    def _worker(self):
        from Utils import dsms_portable as dsms
        from Utils.exe_launch import resolve_tool_prefix, shutdown_prefix_wineserver

        game, exe = self._game, self._exe
        log = lambda m: safe_emit(self._log_sig, m)  # noqa: E731
        compat_data = proton_script = None
        try:
            if exe is None:
                raise RuntimeError(self.tr("DSMSPortable is not installed."))

            exe_dir = getattr(game, "get_exe_dir", lambda: None)() \
                or game.get_game_path()
            vanilla = exe_dir / dsms.REGULATION_NAME if exe_dir else None
            if vanilla is None or not vanilla.is_file():
                raise RuntimeError(self.tr(
                    "Could not find the game's own regulation.bin - restore "
                    "before merging so the vanilla file is in place."))

            # DSMSPortable needs the game's Oodle compressor beside it to write
            # a compressed regulation (its own installer copies it too).
            oodle = exe_dir / dsms.OODLE_DLL
            if oodle.is_file() and not (exe.parent / dsms.OODLE_DLL).is_file():
                shutil.copy2(oodle, exe.parent / dsms.OODLE_DLL)
                log(f"copied {dsms.OODLE_DLL} next to DSMSPortable.")

            log(f"preparing the {self._prefix_mode or 'isolated'} prefix ...")
            resolved = resolve_tool_prefix(exe, game, self._proton_name,
                                           self._prefix_mode, log_fn=log)
            if not resolved:
                raise RuntimeError(self.tr(
                    "Could not prepare the Proton prefix for DSMSPortable."))
            proton_script, compat_data, env = resolved

            # .NET 6 desktop runtime goes into the prefix the user picked, not
            # blindly into the game's own prefix.
            prefix = Path(compat_data) / "pfx" if compat_data else None
            from Utils.proton_tools import install_dotnet_runtime
            log("installing the .NET 6 desktop runtime into that prefix ...")
            install_dotnet_runtime("6", proton_script, env, prefix, log_fn=log)
            plan = dsms.plan_merge(self._sources)
            if not plan.is_useful:
                raise RuntimeError(self.tr("Nothing to merge."))
            log(f"merging {len(plan.sources)} mod(s), lowest priority first: "
                + ", ".join(s.name for s in plan.sources))

            with tempfile.TemporaryDirectory(prefix="amm-regmerge-") as td:
                work = Path(td)
                base = work / dsms.REGULATION_NAME
                shutil.copy2(vanilla, base)
                # Normally a no-op - the game's own file is level 21 - but the
                # "vanilla" file is whatever is in the game folder, which may
                # itself have been replaced by a mod at some point.
                dsms.normalize_zstd_level(base, log_fn=log)

                # The gamepath for the diff step holds the VANILLA regulation, so
                # every mod is read as "primary" against it and the `modified`
                # filter yields that mod's own edits.  Oodle goes alongside
                # because a modern regulation is DCX-compressed.
                diff_game = work / "diff_gamepath"
                diff_game.mkdir(parents=True, exist_ok=True)
                shutil.copy2(vanilla, diff_game / dsms.REGULATION_NAME)
                dsms.normalize_zstd_level(diff_game / dsms.REGULATION_NAME,
                                          log_fn=log)
                if oodle.is_file():
                    shutil.copy2(oodle, diff_game / dsms.OODLE_DLL)
                # FindGamepath() only accepts an Elden Ring gamepath that holds
                # EldenRing.exe, and exits 3 ("Could not find game directory")
                # otherwise.  Nothing ever reads the file - for ER the gamepath
                # is used solely for this check, regulation.bin and the optional
                # Oodle copy - so an empty placeholder is enough.  It also keeps
                # DSMSPortable off the branch that writes a gamepath.txt into
                # its own folder, which would leak into later runs.
                (diff_game / dsms.GAME_EXE_NAME).touch()

                # Exporting per param means naming them; DSMSPortable ships the
                # full list as its Paramdex defs.
                names = dsms.param_names(exe)
                if not names:
                    raise RuntimeError(self.tr(
                        "DSMSPortable is missing its param definitions."))

                # 1. Recover each bare regulation's edits as CSV rows.
                csvs: list[Path] = []
                mismatched: list[tuple[str, str, str]] = []
                missing_tables: dict[str, list[str]] = {}
                for n, src in enumerate(plan.needs_diff):
                    out_dir = work / f"diff_{n}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    # Stage the mod's regulation somewhere clean first, so
                    # DSMSPortable never writes scratch files into the user's
                    # mod folder.
                    src_dir = work / f"src_{n}"
                    src_dir.mkdir(parents=True, exist_ok=True)
                    src_copy = src_dir / dsms.REGULATION_NAME
                    shutil.copy2(src.regulation, src_copy)
                    # Mod tools repack regulation.bin at ZSTD level 15, but
                    # SoulsFormats asserts the level byte is 21 and throws
                    # otherwise - inside an unobserved task, so it surfaces only
                    # as a load that never finishes (exit 7).  Patch the scratch
                    # copy, never the user's staged mod.
                    dsms.normalize_zstd_level(src_copy, log_fn=log)
                    if oodle.is_file():
                        shutil.copy2(oodle, src_dir / dsms.OODLE_DLL)
                    args = dsms.diff_command(exe, diff_game, src_copy,
                                             out_dir=out_dir, params=names,
                                             prefix=prefix)[1:]
                    step_out: list[str] = []
                    if not self._run_step(proton_script, exe, env, args,
                                          f"diff {src.name}", step_out):
                        raise RuntimeError(self.tr(
                            "Could not diff '{0}'.").format(src.name))

                    # A regulation built for an older patch has a different
                    # param layout. Rows replayed across that gap can land in
                    # the wrong fields, and tables the older file lacks cannot
                    # be read at all - both silent. Record it for the summary
                    # rather than pretending the merge is clean.
                    game_ver, mod_ver = dsms.parse_param_versions(step_out)
                    if game_ver and mod_ver and game_ver != mod_ver:
                        mod_s = dsms.describe_param_version(mod_ver)
                        game_s = dsms.describe_param_version(game_ver)
                        mismatched.append((src.name, mod_s, game_s))
                        log(f"  ! '{src.name}' targets game {mod_s} "
                            f"({mod_ver}) but this install is {game_s} "
                            f"({game_ver}).")
                    absent = dsms.missing_param_tables(step_out)
                    if absent:
                        missing_tables[src.name] = absent
                        log(f"  ! '{src.name}': {len(absent)} param table(s) "
                            "could not be read - any edits in them are lost.")
                    # An unmodified table exports an empty CSV (header only);
                    # keeping those would just slow the merge down.
                    found = [p for p in sorted(out_dir.rglob("*.csv"))
                             if p.is_file() and len(
                                 p.read_text(encoding="utf-8",
                                             errors="replace").splitlines()) > 1]
                    if not found:
                        # Treating "nothing" as success would quietly drop the
                        # mod and hand back a half-merged file.
                        raise RuntimeError(self.tr(
                            "Diffing '{0}' produced no edits - its regulation "
                            "may be for a different game version.").format(
                                src.name))
                    log(f"  '{src.name}': {len(found)} changed param table(s).")
                    csvs.extend(found)

                # 2. Replay every edit onto the vanilla base, in order.  The
                # exported rows go last so a mod's own regulation outranks the
                # CSVs it happens to ship; both lists are already
                # lowest-priority-first, and ProcessCSV applies them in the
                # order given, deriving each param name from the file name (so
                # the per-mod diff_<n>/ folders keep same-named exports apart).
                merged_dir = work / "merged"
                merged_dir.mkdir(parents=True, exist_ok=True)
                args = dsms.merge_command(exe, base, game_path=exe_dir,
                                          out_path=merged_dir,
                                          csvs=[*plan.csvs, *csvs],
                                          prefix=prefix)[1:]
                if not self._run_step(proton_script, exe, env, args,
                                      "apply merged edits"):
                    raise RuntimeError(self.tr("The merge step failed."))

                merged = merged_dir / dsms.REGULATION_NAME
                result = merged if merged.is_file() else base
                if not result.is_file():
                    raise RuntimeError(self.tr("No merged regulation was produced."))

                # 3. Stage the result as a normal mod.
                dest = game.get_effective_mod_staging_path() / _OUTPUT_MOD
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result, dest / dsms.REGULATION_NAME)
                log(f"merged regulation written to {dest}")

            self._ran = True
            if mismatched or missing_tables:
                # The merge completed, but replaying edits across a param
                # layout change can put values in the wrong fields. Say so
                # plainly - a quietly wrong regulation is worse than a failure,
                # because it only shows up in-game.
                log("")
                log("WARNING - this merge may not be correct:")
                for mod_name, mod_ver, game_ver in mismatched:
                    log(f"  * '{mod_name}' was built for Elden Ring {mod_ver}, "
                        f"but this install is {game_ver}.")
                for mod_name, tables in missing_tables.items():
                    shown = ", ".join(tables[:4])
                    more = f" (+{len(tables) - 4} more)" if len(tables) > 4 else ""
                    log(f"  * '{mod_name}': could not read {shown}{more}.")
                log("Update those mods to versions built for your game patch, "
                    "or test carefully before relying on the result.")
                names_ = ", ".join(sorted(
                    {m for m, _, _ in mismatched} | set(missing_tables)))
                safe_emit(self._status_sig, self.tr(
                    "Merged into '{0}', but {1} may be for a different game "
                    "version - see the log.").format(_OUTPUT_MOD, names_),
                    AMBER)
            else:
                safe_emit(self._status_sig, self.tr(
                    "Merged into '{0}'. Enable it, disable the mods it "
                    "replaces, then deploy.").format(_OUTPUT_MOD), GREEN)
        except Exception as exc:
            log(f"error: {exc}")
            safe_emit(self._status_sig,
                      self.tr("Merge failed: {0}").format(exc), RED)
        finally:
            # Proton leaves sidecar processes attached to a tool prefix; leaving
            # them running would keep the prefix locked for the next run.
            if compat_data and proton_script:
                try:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=log)
                except Exception:
                    pass
