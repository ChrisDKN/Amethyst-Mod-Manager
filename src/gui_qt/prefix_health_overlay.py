"""Prefix Health Check - a modal report of what a game's Proton prefix contains.

Opened from the Proton Tools menu. Every row comes from ``Utils.prefix_health``,
which reads the prefix itself rather than trusting Amethyst's
``amethyst_deps.json`` marker, so prefixes provisioned by hand report honestly.
Which rows appear is driven entirely by the game handler's ``auto_install_deps``
and ``synthesis_registry_name`` - no game is named here.

Failing rows that something can repair get a Fix button, which runs the very
same installer the Proton menu entry uses (via the window's
``_run_proton_installer``, so the two share the busy guard) and then re-runs the
whole check.

Modal via OverlayBase, not QDialog: gaming mode opens top-level windows behind
the app. That also means the main window's progress popup is invisible under
this overlay, hence the in-card status line and busy bar.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import (
    _c, active_palette, button_qss, contrast_text, close_button,
    err_text, ok_text,
)
from Utils.prefix_health import HealthStatus

_GLYPH = {
    HealthStatus.OK: "✔",
    HealthStatus.MISSING: "✖",
    HealthStatus.WARN: "!",
    HealthStatus.UNKNOWN: "?",
}


def _fix_game_registry(game, log_fn) -> bool:
    """Re-register the game's install path in the prefix (Bethesda tools).

    Headless equivalent of the Register Game Path wizard: the marker is cleared
    first so a manual fix always rewrites, even when the recorded path matches.
    """
    from Utils.bethesda_registry import _marker_path, register_bethesda_game_path
    from Utils.proton_prefix import resolve_compat_data
    from Utils.proton_tools import resolve_proton_env

    registry_name = getattr(game, "synthesis_registry_name", None)
    if not registry_name:
        log_fn("This game does not use the Bethesda Softworks registry key.")
        return False
    prefix = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if not prefix:
        log_fn("No Proton prefix is configured for this game.")
        return False
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if not game_path:
        log_fn("The game install path is not configured.")
        return False

    proton_script, env = resolve_proton_env(game, log_fn)
    if proton_script is None:
        log_fn("No Proton could be resolved for this prefix.")
        return False
    env = dict(env or {})
    env.setdefault("WINEDEBUG", "-all")

    # register_bethesda_game_path takes the compat-data dir (the parent of
    # pfx/), NOT the prefix - and for Heroic/Lutris/Faugus that is the prefix
    # itself, so resolve it rather than assuming .parent.
    compat_data = resolve_compat_data(Path(prefix))
    try:
        _marker_path(compat_data, registry_name).unlink()
    except OSError:
        pass
    return register_bethesda_game_path(
        prefix_dir=compat_data, proton_script=proton_script, env=env,
        game_path=Path(game_path), registry_game_name=registry_name,
        log_fn=log_fn)


def _install_vcredist(game, log_fn) -> bool:
    from Utils.proton_tools import install_vcredist
    return install_vcredist(game, log_fn=log_fn)


def _install_d3dcompiler(game, log_fn) -> bool:
    from Utils.proton_tools import install_d3dcompiler_47
    return install_d3dcompiler_47(game, log_fn=log_fn)


def _install_lavfilters(game, log_fn) -> bool:
    from Utils.proton_tools import repair_lavfilters
    return repair_lavfilters(game, log_fn=log_fn)


def _install_dotnet(version: str):
    def _install(game, log_fn) -> bool:
        from Utils.proton_tools import install_dotnet
        return install_dotnet(game, version, log_fn=log_fn)
    return _install


from Utils.protontricks import WINETRICKS_VERB_DEPS as _WINETRICKS_VERB_DEPS
from Utils.proton_tools import DOTNET_VERSIONS as _DOTNET_VERSIONS

def _install_winetricks_verb(verb: str):
    """Fix handler for a component that is just a winetricks verb."""
    def _install(game, log_fn) -> bool:
        from Utils.protontricks import install_winetricks_verb
        return install_winetricks_verb(game, verb, log_fn=log_fn)
    return _install


# fix_token -> (installer, progress title key). One entry per fixable component.
_FIX_INSTALLERS = {
    "vcredist": _install_vcredist,
    "d3dcompiler_47": _install_d3dcompiler,
    "lavfilters": _install_lavfilters,
    "game_registry": _fix_game_registry,
    **{f"dotnet{_v}": _install_dotnet(_v) for _v in _DOTNET_VERSIONS},
    **{_v: _install_winetricks_verb(_v) for _v in _WINETRICKS_VERB_DEPS},
}


class PrefixHealthOverlay(OverlayBase):
    """Modal prefix report with per-row Fix buttons."""

    CARD_W = 760
    CARD_H = 580
    MIN_W = 460
    MIN_H = 340
    CLICK_OUTSIDE_CANCELS = False

    _scan_done = Signal(object)          # list[HealthCheck]
    _status_sig = Signal(str)

    def __init__(self, host, game, window=None, on_done=None):
        super().__init__(host, on_done=on_done)
        self._game = game
        self._window = window
        self._fixing = False
        self._fix_buttons: dict[str, QPushButton] = {}
        # (check_id, fix_token, label) for every row Fix All should run, in
        # display order. Rebuilt on each render.
        self._fixable: list[tuple[str, str, str]] = []
        self._rows_layout: QVBoxLayout | None = None

        self._scan_done.connect(self._render)
        self._status_sig.connect(self._set_status)
        self._build()
        self._present()
        self._rescan()

    @classmethod
    def show_over(cls, host, game, window=None):
        """Open the overlay over *host*'s top-level window."""
        top = host.window() if host is not None else None
        return cls(top or host, game, window=window if window is not None else host)

    # -- logging -------------------------------------------------------------
    def _log(self, message: str) -> None:
        """Send a line to the main log panel (thread-safe both ways)."""
        win = self._window
        if win is None:
            return
        try:
            safe_emit(win._op_log, f"Prefix health: {message}")  # i18n: skip — log line
        except Exception:
            pass

    # -- construction --------------------------------------------------------
    def _qss(self) -> str:
        p = active_palette()
        c = lambda k: _c(p, k)
        return f"""
        #HealthTitle {{ color: {c('TEXT_MAIN')}; font-weight: 600; font-size: 15px; }}
        #HealthSub {{ color: {c('TEXT_DIM')}; font-size: 11px; }}
        #HealthStatusLine {{ color: {c('TEXT_DIM')}; }}
        QScrollArea {{ background: transparent; border: none; }}
        #HealthBody {{ background: {c('BG_DEEP')}; }}
        #HealthRow {{ background: {c('BG_PANEL')}; border-radius: 4px; }}
        #HealthRow[alt="true"] {{ background: {c('BG_DEEP')}; }}
        #HealthLabel {{ color: {c('TEXT_MAIN')}; font-weight: 600; }}
        #HealthDetail {{ color: {c('TEXT_DIM')}; font-size: 11px; }}
        #HealthGlyph {{ font-size: 15px; font-weight: 700; }}
        #DangerButton {{ background: {c('BTN_DANGER')};
                         color: {contrast_text(c('BTN_DANGER'))}; border: none;
                         border-radius: 4px; padding: 2px 10px; font-size: 13px;
                         font-weight: 600; }}
        #DangerButton:hover {{ background: {c('BTN_DANGER_HOV')}; }}
        """

    def _build(self):
        card, v = self._make_card("PrefixHealthCard", extra_qss=self._qss())

        gname = getattr(self._game, "name", "") or ""
        title = QLabel(self.tr("Prefix health check - {0}").format(gname))
        title.setObjectName("HealthTitle")
        v.addWidget(title)

        self._sub = QLabel("")
        self._sub.setObjectName("HealthSub")
        self._sub.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._sub.setWordWrap(True)
        v.addWidget(self._sub)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("HealthBody")
        self._rows_layout = QVBoxLayout(body)
        self._rows_layout.setContentsMargins(0, 4, 0, 4)
        self._rows_layout.setSpacing(3)
        self._rows_layout.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        self._status = QLabel("")
        self._status.setObjectName("HealthStatusLine")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)                 # indeterminate
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.hide()
        v.addWidget(self._bar)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._recheck = QPushButton(self.tr("Re-check"))
        self._recheck.setObjectName("FormButton")
        self._recheck.setCursor(Qt.PointingHandCursor)
        self._recheck.clicked.connect(self._rescan)
        bar.addWidget(self._recheck)

        self._fix_all = QPushButton(self.tr("Fix All"))
        self._fix_all.setObjectName("FormButton")
        self._fix_all.setCursor(Qt.PointingHandCursor)
        self._fix_all.clicked.connect(self._on_fix_all)
        self._fix_all.setVisible(False)          # shown once a scan finds work
        bar.addWidget(self._fix_all)

        bar.addStretch(1)
        self._close_btn = close_button()
        self._close_btn.clicked.connect(lambda: self._finish(None))
        bar.addWidget(self._close_btn)
        v.addLayout(bar)

    # -- rendering -----------------------------------------------------------
    def _clear_rows(self):
        if self._rows_layout is None:
            return
        # Leave the trailing stretch in place.
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._fix_buttons.clear()
        self._fixable.clear()

    def _make_row(self, check, alt: bool) -> QWidget:
        p = active_palette()
        colour = {
            HealthStatus.OK: ok_text(p),
            HealthStatus.MISSING: err_text(p),
            HealthStatus.WARN: _c(p, "TEXT_WARN_BRIGHT"),
            HealthStatus.UNKNOWN: _c(p, "TEXT_DIM"),
        }.get(check.status, _c(p, "TEXT_DIM"))

        row = QFrame()
        row.setObjectName("HealthRow")
        row.setProperty("alt", "true" if alt else "false")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(10)

        glyph = QLabel(_GLYPH.get(check.status, "?"))
        glyph.setObjectName("HealthGlyph")
        glyph.setStyleSheet(f"#HealthGlyph {{ color: {colour}; }}")
        glyph.setFixedWidth(16)
        glyph.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        h.addWidget(glyph)

        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(self._label_for(check))
        name.setObjectName("HealthLabel")
        col.addWidget(name)
        if check.detail:
            detail = QLabel(check.detail)
            detail.setObjectName("HealthDetail")
            detail.setWordWrap(True)
            col.addWidget(detail)
        h.addLayout(col, 1)

        if (check.fix_token and check.status in
                (HealthStatus.MISSING, HealthStatus.WARN)
                and check.fix_token in _FIX_INSTALLERS):
            btn = QPushButton(self.tr("Fix"))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(button_qss("BTN_WARN", hover_key="BTN_WARN_HOV",
                                         padding="4px 14px"))
            btn.clicked.connect(
                lambda _=False, cid=check.check_id, tok=check.fix_token,
                lbl=self._label_for(check): self._on_fix(cid, tok, lbl))
            self._fix_buttons[check.check_id] = btn
            self._fixable.append(
                (check.check_id, check.fix_token, self._label_for(check)))
            h.addWidget(btn, 0, Qt.AlignTop)

        return row

    def _label_for(self, check) -> str:
        """Localised row label; falls back to the util's English one."""
        for version in _DOTNET_VERSIONS:
            if check.check_id == f"dotnet{version}":
                return self.tr(".NET {0} Desktop Runtime").format(version)
        return {
            "prefix_exists": self.tr("Proton prefix"),
            "prefix_structure": self.tr("Prefix structure"),
            "runner_bound": self.tr("Prefix runner"),
            "steam_first_launch": self.tr("Steam first-launch setup"),
            "game_inis": self.tr("Game INI files"),
            "proton_bound": self.tr("Proton build"),
            "proton_downgrade": self.tr("Proton / prefix version"),
            "vcredist": self.tr("VC++ Redistributable (x64)"),
            "d3dcompiler_47": self.tr("d3dcompiler_47 (shader compiler)"),
            "lavfilters": self.tr("LAV Filters (DirectShow codecs)"),
            "game_registry": self.tr("Game path in prefix registry"),
            "d3dx9": self.tr("d3dx9 (all legacy DirectX 9 runtimes)"),
            "d3dx10": self.tr("d3dx10 (all legacy DirectX 10 runtimes)"),
            "quartz": self.tr("quartz (DirectShow runtime)"),
            "dx8vb": self.tr("dx8vb (DirectX 8 Visual Basic runtime)"),
            "dxvk": self.tr("DXVK (Direct3D → Vulkan)"),
        }.get(check.check_id, check.label)

    def _render(self, checks):
        if self._done:
            return
        self._clear_rows()
        for i, check in enumerate(checks or []):
            self._rows_layout.insertWidget(i, self._make_row(check, alt=bool(i % 2)))

        prefix = ""
        try:
            got = self._game.get_prefix_path()
            prefix = str(got) if got else ""
        except Exception:
            prefix = ""
        self._sub.setText(prefix or self.tr("No prefix configured"))

        # Only worth its own button when it would batch more than one Fix.
        self._fix_all.setVisible(len(self._fixable) > 1)

        bad = sum(1 for c in (checks or [])
                  if c.status in (HealthStatus.MISSING, HealthStatus.WARN))
        self._set_busy(False, self.tr("Everything looks healthy.") if not bad
                       else self.tr("{0} item(s) need attention.").format(bad))

    # -- busy / status -------------------------------------------------------
    def _set_status(self, text: str):
        if not self._done:
            self._status.setText(text)

    def _set_busy(self, busy: bool, message: str = "", *, fixing: bool = False):
        """Disable the controls while work runs.

        *fixing* marks an actual install, which is what Esc must not interrupt;
        a plain rescan is read-only and safe to walk away from, so it leaves
        the Close button live.
        """
        self._fixing = busy and fixing
        self._bar.setVisible(busy)
        self._recheck.setEnabled(not busy)
        self._fix_all.setEnabled(not busy)
        self._close_btn.setEnabled(not self._fixing)
        for btn in self._fix_buttons.values():
            btn.setEnabled(not busy)
        if message:
            self._status.setText(message)

    # -- scanning ------------------------------------------------------------
    def _rescan(self):
        if self._done:
            return
        self._set_busy(True, self.tr("Checking prefix…"))

        def _worker():
            proton_script = None
            try:
                from Utils.proton_tools import resolve_proton_env
                proton_script, _env = resolve_proton_env(self._game, self._log)
            except Exception as exc:
                self._log(f"could not resolve Proton: {exc}")
            try:
                from Utils.prefix_health import run_prefix_health
                checks = run_prefix_health(self._game, proton_script=proton_script,
                                           check_proton=True)
            except Exception as exc:
                from Utils.prefix_health import HealthCheck
                self._log(f"check failed: {exc}")
                checks = [HealthCheck("error", HealthStatus.UNKNOWN,
                                      "Health check", str(exc))]
            safe_emit(self._scan_done, checks)

        threading.Thread(target=_worker, daemon=True,
                         name="prefix-health-scan").start()

    # -- fixing --------------------------------------------------------------
    def _on_fix(self, check_id: str, fix_token: str, label: str):
        if self._done or self._fixing:
            return
        installer = _FIX_INSTALLERS.get(fix_token)
        win = self._window
        if installer is None or win is None:
            return
        title = self.tr("Fixing {0}").format(label)
        self._set_busy(True, self.tr("Fixing {0}… (details in the log)").format(label),
                       fixing=True)
        started = win._run_proton_installer(
            title,
            lambda plog: installer(self._game, plog),
            on_done=lambda ok: self._on_fix_done(ok))
        if not started:
            self._set_busy(
                False,
                self.tr("Another Proton installer is running - try again shortly."))

    def _on_fix_all(self):
        """Run every fixable row's installer back-to-back in ONE Proton job.

        _run_proton_installer is serialized and refuses a second installer
        while one runs, so this cannot be a loop over _on_fix - the whole
        sequence has to live inside a single worker.
        """
        if self._done or self._fixing:
            return
        win = self._window
        if win is None:
            return

        # Snapshot now: _rescan() replaces the rows this list came from.
        targets = [(cid, tok, lbl) for cid, tok, lbl in self._fixable]
        if not targets:
            return

        self._set_busy(True, self.tr("Fixing {0} item(s)… (details in the log)")
                       .format(len(targets)), fixing=True)

        def _run_all(plog) -> bool:
            done = 0
            for _cid, tok, lbl in targets:
                installer = _FIX_INSTALLERS.get(tok)
                if installer is None:
                    continue
                plog(f"--- {lbl} ---")
                try:
                    ok = installer(self._game, plog)
                except Exception as exc:            # keep going: one bad verb
                    plog(f"{lbl}: {exc}")           # must not strand the rest
                    ok = False
                if ok:
                    done += 1
                else:
                    plog(f"{lbl}: failed - continuing with the remaining items.")
            plog(f"Fix All finished: {done}/{len(targets)} succeeded.")
            return done > 0

        started = win._run_proton_installer(
            self.tr("Fixing {0} prefix item(s)").format(len(targets)),
            _run_all,
            on_done=lambda ok: self._on_fix_done(ok))
        if not started:
            self._set_busy(
                False,
                self.tr("Another Proton installer is running - try again shortly."))

    def _on_fix_done(self, ok: bool):
        if self._done:
            return
        # Re-run the whole check rather than trusting the installer's verdict:
        # the prefix is the source of truth. _rescan() takes over the busy
        # state (as a scan, not a fix, so Close comes back).
        self._rescan()

    # -- events --------------------------------------------------------------
    def keyPressEvent(self, event):
        # Dismissing mid-fix would throw away the rescan; the installer itself
        # is capped (PREFIX_INSTALLER_TIMEOUT_S), so this cannot wedge.
        if event.key() == Qt.Key_Escape and self._fixing:
            self._set_status(
                self.tr("A fix is running - please wait for it to finish."))
            return
        super().keyPressEvent(event)
