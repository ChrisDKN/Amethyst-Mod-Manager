"""Curated-profile wizard - installs a prebuilt .amethyst modlist hosted on the
GitHub ``Resources`` branch (e.g. "Install Viva New Vegas" for Fallout New
Vegas), reusing the normal Profile ▸ Import pipeline for the actual install.

Flow: intro (with an optional "also install Ultimate Edition ESM Fixes"
checkbox) → download the .amethyst into the curated-profiles cache and open
the Import tab via ctx.import_manifest → wait for the user to finish the
import there (the app switches to the new profile when it completes) →
optionally run the embedded ESM Fixes and BSA Decompressor wizards into that
new profile → optionally auto-apply the FNV 4GB patch → done.

The ESM Fixes / BSA Decompressor steps must come AFTER the import: the
curated profiles use profile-specific mods, so their outputs (registered into
the ACTIVE profile's effective mods dir) are only visible once the imported
profile is active. Both steps are parameterized (``esm_fixes_step`` /
``bsa_decompressor_step`` in WizardTool.extra) because their large outputs
cannot be bundled into the .amethyst, and each has an opt-out checkbox on
the intro page (the standalone wizards remain available for later).

The 4GB patch step (``fnv_4gb_step``) runs LAST: the ESM Fixes installer
checksum-checks FalloutNV.exe and may refuse a patched exe, so the exe is
patched only after the masters are done. For the same reason, entering the
ESM step first restores FalloutNV_backup.exe if the user had already patched
the exe before running this wizard (the final step re-patches it). The patch
applies automatically via Utils.bethesda.fnv4gb (already-patched / unrecognised
exes just report and continue - a failure never blocks finishing the wizard).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_INTRO, _PG_FETCH, _PG_WAIT, _PG_ESM, _PG_BSA, _PG_4GB, _PG_DONE = range(7)


class CuratedProfileView(WizardViewBase):
    """Guided install of a prebuilt .amethyst profile from the Resources branch."""

    _fetch_status_sig = Signal(str, str)
    _fetch_done_sig = Signal(object)      # Path | None
    _gb_status_sig = Signal(str, str)
    _gb_done_sig = Signal()
    _esm_prep_done_sig = Signal()
    _premium_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, profile_repo_path: str, display_name: str,
                 esm_fixes_step: bool = False,
                 bsa_decompressor_step: bool = False,
                 fnv_4gb_step: bool = False,
                 info_url: str = "", **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install {0} - {1}").format(
                             display_name, game.name))
        self._repo_path = profile_repo_path
        self._display_name = display_name
        self._esm_fixes_step = esm_fixes_step
        self._bsa_decompressor_step = bsa_decompressor_step
        self._fnv_4gb_step = fnv_4gb_step
        self._info_url = info_url
        self._gb_started = False
        self._gb_ok = False
        self._esm_prep_started = False
        self._bsa_view = None
        # Hands-free mode: premium accounts (checked during the fetch step)
        # auto-advance every successful step - including the embedded
        # ESM/BSA sub-wizards, whose archives download automatically.
        self._napi = None
        self._auto = False
        self._premium_checked = False
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(2000)
        self._wait_timer.timeout.connect(self._guard(self._wait_tick))
        self._bundle_path: "Path | None" = None
        self._manifest: dict | None = None
        # Profile at open - the import completing switches the active profile,
        # which is how the Continue gate spots an unfinished import.
        self._profile_at_open = getattr(ctx, "profile_name", None) or "default"
        self._continue_warned = False
        self._esm_view = None

        self._fetch_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._fetch_status, t, c)))
        self._fetch_done_sig.connect(self._guard(self._on_fetch_done))
        self._gb_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._gb_status, t, c)))
        self._gb_done_sig.connect(self._guard(self._on_gb_done))
        self._esm_prep_done_sig.connect(self._guard(self._enter_esm_step))
        self._premium_sig.connect(self._guard(self._on_premium_known))

        self._stack.addWidget(self._build_intro_page())   # 0
        self._stack.addWidget(self._build_fetch_page())   # 1
        self._stack.addWidget(self._build_wait_page())    # 2
        self._stack.addWidget(QWidget())                  # 3 (ESM, built lazily)
        self._stack.addWidget(QWidget())                  # 4 (BSA, built lazily)
        self._stack.addWidget(self._build_4gb_page())     # 5
        self._stack.addWidget(self._build_done_page())    # 6
        self._stack.setCurrentIndex(_PG_INTRO)

    # ---- page 0: intro + options --------------------------------------------------
    def _build_intro_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("Install the {0} modlist").format(self._display_name))
        self._make_note(lay, (
            self.tr("This wizard downloads the curated '{0}' profile and opens "
                    "the profile importer, which installs the modlist into a "
                    "NEW profile.\n\nThe mods are downloaded from Nexus Mods - "
                    "log in first (Nexus ▸ Login to Nexus) if you haven't.")
            .format(self._display_name)))
        if self._info_url:
            guide = QPushButton(self.tr("Open guide website"))
            guide.setCursor(Qt.PointingHandCursor)
            guide.clicked.connect(lambda: self._open_url(self._info_url))
            lay.addWidget(guide, 0, Qt.AlignHCenter)
        self._esm_chk = None
        if self._esm_fixes_step:
            lay.addSpacing(8)
            self._esm_chk = QCheckBox(
                self.tr("Also install Ultimate Edition ESM Fixes (recommended)"))
            self._esm_chk.setChecked(True)
            self._esm_chk.setCursor(Qt.PointingHandCursor)
            lay.addWidget(self._esm_chk, 0, Qt.AlignHCenter)
            self._make_note(lay, (
                self.tr("Patches the vanilla .esm masters with community "
                        "bugfixes after the modlist is installed. It is too "
                        "large to bundle, so it runs as an extra step - needs "
                        "the 'Ultimate Edition ESM Fixes Remastered' download "
                        "from Nexus.")))
        self._bsa_chk = None
        if self._bsa_decompressor_step:
            lay.addSpacing(8)
            self._bsa_chk = QCheckBox(
                self.tr("Also run the FNV BSA Decompressor (recommended)"))
            self._bsa_chk.setChecked(True)
            self._bsa_chk.setCursor(Qt.PointingHandCursor)
            lay.addWidget(self._bsa_chk, 0, Qt.AlignHCenter)
            self._make_note(lay, (
                self.tr("Rebuilds the vanilla BSA archives without compression "
                        "for faster loading, added as a mod after the modlist "
                        "is installed - needs the 'FNV BSA Decompressor' "
                        "download from Nexus. Can also be run later via its "
                        "own wizard.")))
        if self._fnv_4gb_step:
            lay.addSpacing(4)
            self._make_note(lay, (
                self.tr("The 4GB patch is applied to FalloutNV.exe as the "
                        "final step (original exe kept as a backup).")))
        lay.addStretch(1)
        start = self._accent_btn(self.tr("Start"))
        start.clicked.connect(self._start_fetch)
        lay.addWidget(start, 0, Qt.AlignHCenter)
        return page

    # ---- page 1: download the .amethyst --------------------------------------------
    def _build_fetch_page(self) -> QWidget:
        page, lay = self._step_page(
            self.tr("Step 1: Download the modlist profile"))
        self._make_note(lay, (
            self.tr("Downloading '{0}' from GitHub…").format(
                Path(self._repo_path).name)))
        self._fetch_status = self._make_status(lay)
        lay.addStretch(1)
        self._retry_btn = self._accent_btn(self.tr("Retry"))
        self._retry_btn.setVisible(False)
        self._retry_btn.clicked.connect(self._start_fetch)
        lay.addWidget(self._retry_btn, 0, Qt.AlignHCenter)
        return page

    def _start_fetch(self):
        self._stack.setCurrentIndex(_PG_FETCH)
        self._retry_btn.setVisible(False)
        self._set_status(self._fetch_status, self.tr("Contacting GitHub…"))
        self._check_premium()
        repo_path = self._repo_path

        def worker():
            from Utils.profiles.curated import download_curated_profile
            _wlog = lambda m: self._log(f"Curated Profile Wizard: {m}")
            try:
                path = download_curated_profile(repo_path, log_fn=_wlog)
                safe_emit(self._fetch_done_sig, path)
            except Exception as exc:
                _wlog(f"download error: {exc}")
                safe_emit(self._fetch_status_sig,
                          self.tr("Download failed: {0}").format(exc), RED)
                safe_emit(self._fetch_done_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="curated-profile-fetch").start()

    def _check_premium(self):
        """Resolve the shared Nexus API (GUI thread) and learn premium status
        off-thread - premium turns on hands-free auto-advance."""
        if self._premium_checked:
            return
        self._premium_checked = True
        api_fn = getattr(self._ctx, "nexus_api", None)
        if api_fn is not None:
            try:
                self._napi = api_fn()
            except Exception:
                self._napi = None
        api = self._napi
        if api is None:
            return

        def worker():
            try:
                if api.validate().is_premium:
                    safe_emit(self._premium_sig, True)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="curated-profile-premium").start()

    def _on_premium_known(self, premium: bool):
        self._auto = bool(premium)
        self._update_wait_auto_note()

    def _update_wait_auto_note(self):
        if self._auto and self._stack.currentIndex() == _PG_WAIT:
            self._set_status(self._wait_status,
                             self.tr("Premium account - the wizard continues "
                                     "automatically when the import "
                                     "completes."))

    def _on_fetch_done(self, path):
        if path is None:
            self._retry_btn.setVisible(True)
            return
        self._bundle_path = Path(path)
        try:
            from Utils.profiles.export import read_manifest
            self._manifest = read_manifest(self._bundle_path)
        except Exception as exc:
            self._set_status(self._fetch_status,
                             self.tr("Could not read manifest: {0}").format(exc),
                             RED)
            self._retry_btn.setVisible(True)
            return
        self._ran = True
        self._open_import_tab()
        self._stack.setCurrentIndex(_PG_WAIT)
        self._update_wait_auto_note()
        self._wait_timer.start()

    def _open_import_tab(self):
        import_manifest = getattr(self._ctx, "import_manifest", None)
        if import_manifest is None or self._manifest is None:
            self._set_status(self._wait_status,
                             self.tr("Import is unavailable here."), RED)
            return
        # The app validates the game domain + Nexus login and opens the Import
        # tab (collection detail + install pipeline); it notifies on failure.
        import_manifest(self._manifest, self._bundle_path.stem,
                        str(self._bundle_path))

    # ---- page 2: wait for the import to finish --------------------------------------
    def _build_wait_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Install the modlist"))
        self._make_note(lay, (
            self.tr("Finish the install in the Import tab: choose the profile "
                    "name and press Install. The mods are downloaded from "
                    "Nexus, which can take a while.\n\nWhen it completes, the "
                    "app switches to the new profile - then come back here and "
                    "press Continue.")))
        self._wait_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        reopen = QPushButton(self.tr("Reopen import tab"))
        reopen.setCursor(Qt.PointingHandCursor)
        reopen.clicked.connect(self._open_import_tab)
        rh.addWidget(reopen)
        cont = self._accent_btn(self.tr("Continue"))
        cont.clicked.connect(self._on_wait_continue)
        rh.addWidget(cont)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _current_profile(self) -> str:
        cur = getattr(self._ctx, "current_profile", None)
        return cur() if cur is not None else self._profile_at_open

    def _on_wait_continue(self):
        if (self._current_profile() == self._profile_at_open
                and not self._continue_warned):
            self._continue_warned = True
            self._set_status(self._wait_status,
                             self.tr("The active profile hasn't changed - the "
                                     "import doesn't look finished. Complete it "
                                     "in the Import tab first, or press "
                                     "Continue again to proceed anyway."), RED)
            return
        self._proceed_from_wait()

    def _wait_tick(self):
        """Hands-free mode: the import switching the active profile is the
        completion signal - continue without a Continue press."""
        if not self._auto or self._stack.currentIndex() != _PG_WAIT:
            return
        if self._current_profile() != self._profile_at_open:
            self._proceed_from_wait()

    def _proceed_from_wait(self):
        self._wait_timer.stop()
        if self._esm_chk is not None and self._esm_chk.isChecked():
            self._start_esm_prep()
        else:
            self._after_esm()

    # ---- ESM prep: un-4GB-patch the exe if the user patched it beforehand -----------
    def _start_esm_prep(self):
        """The ESM Fixes installer checksum-checks FalloutNV.exe and may refuse
        a 4GB-patched one. If the user already applied the patch before running
        this wizard, restore the original exe first - the final 4GB step
        re-patches it (which is why this only runs when that step is enabled)."""
        if self._esm_prep_started or not self._fnv_4gb_step:
            self._enter_esm_step()
            return
        self._esm_prep_started = True
        self._set_status(self._wait_status,
                         self.tr("Checking FalloutNV.exe…"))
        game_root = self._game.get_game_path()
        _wlog = lambda m: self._log(f"Curated Profile Wizard: {m}")

        def worker():
            from Utils.bethesda.fnv4gb import (
                BACKUP_NAME, EXE_NAME, inspect_exe, restore_backup,
            )
            try:
                if game_root is not None and game_root.is_dir():
                    info = inspect_exe(game_root)
                    if info["state"] == "patched":
                        if info["backup_exists"]:
                            restore_backup(game_root)
                            _wlog(f"{EXE_NAME} was already 4GB patched - "
                                  f"restored the original from {BACKUP_NAME} "
                                  "for the ESM Fixes installer (it is "
                                  "re-patched at the end).")
                        else:
                            _wlog(f"{EXE_NAME} is 4GB patched but "
                                  f"{BACKUP_NAME} is missing - cannot restore; "
                                  "the ESM Fixes installer may refuse to run. "
                                  "Verify game files to get a clean exe.")
            except Exception as exc:
                _wlog(f"pre-ESM exe check failed: {exc}")
            finally:
                safe_emit(self._esm_prep_done_sig)

        threading.Thread(target=worker, daemon=True,
                         name="curated-profile-esm-prep").start()

    # ---- embedded sub-wizard pages (ESM Fixes / BSA Decompressor) --------------------
    def _live_ctx(self):
        """Ctx for an embedded view, rebased onto the LIVE active profile
        (ctx.profile_name is frozen at open, before the import switched)."""
        if self._ctx is None:
            return None
        import dataclasses
        return dataclasses.replace(self._ctx,
                                   profile_name=self._current_profile())

    def _embed_step(self, idx: int, view) -> None:
        old = self._stack.widget(idx)
        self._stack.removeWidget(old)
        old.deleteLater()
        self._stack.insertWidget(idx, view)

    # ---- page 3: embedded ESM Fixes wizard ------------------------------------------
    def _enter_esm_step(self):
        # Built lazily so the embedded view captures the NEW profile (its ctor
        # syncs the game's active-profile context to ctx.profile_name).
        if self._esm_view is None:
            from wizards_qt.esm_fixes_view import ESMFixesView
            self._esm_view = ESMFixesView(
                self._game, log_fn=self._log,
                on_close=lambda: self._guard(self._on_esm_done)(),
                ctx=self._live_ctx(), show_header=False,
                auto_continue=self._auto)
            self._embed_step(_PG_ESM, self._esm_view)
        self._stack.setCurrentIndex(_PG_ESM)

    def _on_esm_done(self):
        self._after_esm()

    def _after_esm(self):
        if self._bsa_chk is not None and self._bsa_chk.isChecked():
            self._enter_bsa_step()
        else:
            self._after_bsa()

    # ---- page 4: embedded BSA Decompressor wizard -------------------------------------
    def _enter_bsa_step(self):
        if self._bsa_view is None:
            from wizards_qt.bsa_decompressor_view import BSADecompressorView
            self._bsa_view = BSADecompressorView(
                self._game, log_fn=self._log,
                on_close=lambda: self._guard(self._after_bsa)(),
                ctx=self._live_ctx(), show_header=False,
                auto_continue=self._auto)
            self._embed_step(_PG_BSA, self._bsa_view)
        self._stack.setCurrentIndex(_PG_BSA)

    def _after_bsa(self):
        if self._fnv_4gb_step:
            self._enter_4gb_step()
        else:
            self._stack.setCurrentIndex(_PG_DONE)

    # ---- page 5: apply the FNV 4GB patch ----------------------------------------------
    def _build_4gb_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Final step: Apply the 4GB Patch"))
        self._make_note(lay, (
            self.tr("FalloutNV.exe is patched so the game can use 4 GB of "
                    "memory and loads NVSE automatically at startup. The "
                    "original exe is kept as a backup (restorable via the "
                    "4GB Patch wizard).")))
        self._gb_status = self._make_status(lay)
        lay.addStretch(1)
        self._gb_continue_btn = self._accent_btn(self.tr("Continue"))
        self._gb_continue_btn.setEnabled(False)
        self._gb_continue_btn.clicked.connect(
            lambda: self._stack.setCurrentIndex(_PG_DONE))
        lay.addWidget(self._gb_continue_btn, 0, Qt.AlignHCenter)
        return page

    def _on_gb_done(self):
        self._gb_continue_btn.setEnabled(True)
        # Hands-free mode: success (or already patched) advances itself;
        # failures wait for the user.
        if self._auto and self._gb_ok:
            QTimer.singleShot(1200, self._guard(
                lambda: self._stack.setCurrentIndex(_PG_DONE)))

    def _enter_4gb_step(self):
        self._stack.setCurrentIndex(_PG_4GB)
        if self._gb_started:
            return
        self._gb_started = True
        game_root = self._game.get_game_path()
        _wlog = lambda m: self._log(f"Curated Profile Wizard: {m}")

        # Runs LAST on purpose: the ESM Fixes installer checksum-checks an
        # unpatched FalloutNV.exe. Failure never blocks finishing the wizard.
        def worker():
            from Utils.bethesda.fnv4gb import (
                BACKUP_NAME, EXE_NAME, apply_4gb_patch, inspect_exe,
            )
            try:
                if game_root is None or not game_root.is_dir():
                    safe_emit(self._gb_status_sig,
                              self.tr("Game path is not configured - skipping "
                                      "the 4GB patch."), RED)
                    return
                state = inspect_exe(game_root)["state"]
                if state == "patched":
                    self._gb_ok = True
                    safe_emit(self._gb_status_sig,
                              self.tr("{0} is already 4GB patched.")
                              .format(EXE_NAME), GREEN)
                    return
                if state == "missing":
                    safe_emit(self._gb_status_sig,
                              self.tr("{0} not found in the game folder - "
                                      "skipping the 4GB patch.")
                              .format(EXE_NAME), RED)
                    return
                if state != "patchable":
                    safe_emit(self._gb_status_sig,
                              self.tr("Unrecognised {0} version - skipping. "
                                      "Verify game files in Steam/Heroic, then "
                                      "run the 4GB Patch wizard manually.")
                              .format(EXE_NAME), RED)
                    return
                safe_emit(self._gb_status_sig,
                          self.tr("Patching {0}…").format(EXE_NAME), "")
                variant = apply_4gb_patch(game_root)
                self._gb_ok = True
                _wlog(f"patched {EXE_NAME} ({variant} version), original "
                      f"saved as {BACKUP_NAME}.")
                safe_emit(self._gb_status_sig,
                          self.tr("Patched {0} ({1} version) - original kept "
                                  "as {2}.").format(EXE_NAME, variant,
                                                    BACKUP_NAME), GREEN)
            except Exception as exc:
                _wlog(f"4GB patch failed: {exc}")
                safe_emit(self._gb_status_sig,
                          self.tr("Patch failed: {0} - you can run the 4GB "
                                  "Patch wizard manually later.").format(exc),
                          RED)
            finally:
                safe_emit(self._gb_done_sig)

        threading.Thread(target=worker, daemon=True,
                         name="curated-profile-4gb").start()

    # ---- page 6: done ----------------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("All done"))
        self._make_note(lay, (
            self.tr("The {0} profile is set up. Review the mod list, then "
                    "Deploy and play.").format(self._display_name)))
        lay.addStretch(1)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        lay.addWidget(done, 0, Qt.AlignHCenter)
        return page
