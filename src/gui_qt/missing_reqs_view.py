"""Missing Requirements panel - a vertical list of cards (one per missing
requirement) showing the required mod's name + its notes, with a View link.
Opens as a plugins-panel-scoped tab (covers the whole plugins panel), like the
Change Version overlay.

The notes/description aren't in meta.ini (which only stores `modId:name` pairs),
so they're fetched from `api.get_mod_requirements(domain, mod_id)` on a daemon
thread (a Signal marshals the result back - never a QThread). For multiple mods
the requirements are aggregated and deduped by mod_id.

No keyword categorisation (the Tk Required/Optional/Other split was unreliable -
dropped per the user).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QScrollArea, QFrame,
)

from gui_qt.theme_qt import active_palette, _c, danger_close_button, button_qss
from gui_qt.safe_emit import safe_emit


class _ElidedLabel(QLabel):
    """A QLabel that never grows past *max_width*, eliding its text with an
    ellipsis instead of wrapping or forcing the panel wider. The full text is
    kept so the tooltip / re-elide-on-resize can use it."""

    def __init__(self, text="", max_width=360):
        super().__init__()
        self._full = text
        self.setMaximumWidth(max_width)
        self._apply_elide()

    def setText(self, text):  # noqa: N802 (Qt override)
        self._full = text
        self._apply_elide()

    def _apply_elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, self.width()))

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._apply_elide()


class _ReqCard(QFrame):
    """A single missing-requirement card: title (required mod name) + notes +
    per-requirement Ignore checkbox + View (opens the Nexus page) + Install
    (downloads & installs the mod)."""

    def __init__(self, p, req, url, is_external, on_view, on_install,
                 ignored=False, on_ignore=None):
        super().__init__()
        self.setObjectName("ReqCard")
        self.setStyleSheet(
            f"#ReqCard{{background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px;}}")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(10)

        col = QVBoxLayout(); col.setContentsMargins(0, 0, 0, 0); col.setSpacing(3)
        name = req.mod_name or self.tr("Mod {0}").format(req.mod_id)
        if is_external:
            name += "  " + self.tr("(External)")
        title = QLabel(name)
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        title.setWordWrap(True)
        col.addWidget(title)

        notes = (req.notes or "").strip() or self.tr("No description provided.")
        desc = QLabel(notes)
        desc.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        desc.setWordWrap(True)
        col.addWidget(desc)
        h.addLayout(col, 1)

        # Per-requirement ignore: suppresses the ⚠ flag for THIS requirement
        # only (stored in the owning mods' meta.ini, so it survives reinstalls);
        # the card stays listed so it can be un-ignored.
        if on_ignore is not None and int(getattr(req, "mod_id", 0) or 0) > 0:
            ign = QCheckBox(self.tr("Ignore"))
            ign.setCursor(Qt.PointingHandCursor)
            ign.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            ign.setToolTip(self.tr(
                "Don't count this requirement towards the missing-requirements "
                "flag. It stays listed here so you can un-ignore it."))
            ign.setChecked(bool(ignored))
            ign.toggled.connect(lambda st, r=req: on_ignore(r, bool(st)))
            h.addWidget(ign, 0, Qt.AlignTop)

        view = QPushButton(self.tr("View"))
        view.setCursor(Qt.PointingHandCursor)
        view.setStyleSheet(button_qss("BTN_GREY", padding="5px 14px"))
        view.setEnabled(bool(url))
        view.clicked.connect(lambda _=False, u=url: on_view(u))
        h.addWidget(view, 0, Qt.AlignTop)

        # Install is only meaningful for Nexus-hosted requirements (external
        # ones have no mod page to download from).
        self._install_btn = None
        if not is_external:
            inst = QPushButton(self._install_label())
            inst.setCursor(Qt.PointingHandCursor)
            inst.setStyleSheet(button_qss("BTN_SUCCESS", padding="5px 14px"))
            inst.clicked.connect(lambda _=False, r=req: on_install(r))
            h.addWidget(inst, 0, Qt.AlignTop)
            self._install_btn = inst

    def _install_label(self) -> str:
        """Install, or Download while 'Download only' is on."""
        from Utils.ui_config import load_download_only
        try:
            dl_only = bool(load_download_only())
        except Exception:
            dl_only = False
        return self.tr("Download") if dl_only else self.tr("Install")

    def refresh_install_label(self):
        if self._install_btn is not None:
            self._install_btn.setText(self._install_label())


class MissingReqsView(QWidget):
    """Scoped-tab body listing a mod's (or several mods') missing requirements."""

    # (reqs | None, error_msg) from the fetch worker → UI thread.
    _reqs_ready = Signal(object, object)

    def __init__(self, api, game, mods, ignored_set, save_ignored_fn,
                 on_close, log_fn=None, install_fn=None, ignore_req_fn=None):
        super().__init__()
        self._api = api
        self._game = game
        # mods: list of {"mod_name","mod_id","domain","missing_ids": set[int]}.
        self._mods = list(mods or ())
        self._ignored_set = set(ignored_set or ())
        # True once an Ignore toggle changes the ignored set, so the window can
        # skip the flag refresh on close when nothing actually changed.
        self.ignored_changed = False
        self._save_ignored_fn = save_ignored_fn or (lambda s: None)
        self._on_close = on_close or (lambda: None)
        self._log = log_fn or (lambda _m: None)
        # install_fn(mod_id, domain, name) - runs the full premium→files→download
        # →install flow (provided by the window). None = install disabled.
        self._install_fn = install_fn
        # ignore_req_fn(req_id, req_name, ignored, owner_names) - persists a
        # per-requirement ignore into the owning mods' meta.ini (provided by
        # the window). None = no per-requirement Ignore checkboxes.
        self._ignore_req_fn = ignore_req_fn
        # mod_id → card widget, so cards can be pruned once installed.
        self._cards: dict[int, _ReqCard] = {}

        self.setObjectName("MissingReqsView")
        self._reqs_ready.connect(self._on_reqs_ready)
        self._build()
        self._start_fetch()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Ignore requirements + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        full = self.tr("Missing requirements - {0}").format(self._title_text())
        title = _ElidedLabel(full, max_width=360)
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        # A long mod name must not be allowed to force the whole panel wide - the
        # label caps its own width and elides the overflow (full name in tooltip).
        title.setToolTip(full)
        hb.addWidget(title)
        hb.addStretch(1)

        self._ignore_cb = QCheckBox(self.tr("Ignore requirements"))
        self._ignore_cb.setToolTip(
            self.tr("Stop flagging the selected mod(s) for missing requirements."))
        self._ignore_cb.setChecked(self._all_ignored())
        self._ignore_cb.toggled.connect(self._on_ignore_toggled)
        hb.addWidget(self._ignore_cb)

        close = danger_close_button(pal=p)
        close.clicked.connect(lambda: self._on_close())
        hb.addWidget(close)
        v.addWidget(bar)

        # Scrollable card list.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._cards_host = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(12, 10, 12, 10)
        self._cards_layout.setSpacing(8)
        self._status = QLabel(self.tr("Loading requirements…"))
        self._status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        self._cards_layout.addWidget(self._status)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(self._cards_host)
        v.addWidget(self._scroll, 1)

    def _title_text(self) -> str:
        if len(self._mods) == 1:
            return self._mods[0].get("mod_name", "")
        return f"{len(self._mods)} mods"

    # ---- ignore -----------------------------------------------------------
    def _mod_names(self) -> list[str]:
        return [m.get("mod_name", "") for m in self._mods if m.get("mod_name")]

    def _all_ignored(self) -> bool:
        names = self._mod_names()
        return bool(names) and all(n in self._ignored_set for n in names)

    def _on_ignore_toggled(self, state):
        before = set(self._ignored_set)
        for n in self._mod_names():
            if state:
                self._ignored_set.add(n)
            else:
                self._ignored_set.discard(n)
        if self._ignored_set != before:
            self.ignored_changed = True
        try:
            self._save_ignored_fn(self._ignored_set)
        except Exception as exc:
            self._log(f"Nexus: could not save ignored requirements - {exc}")

    # ---- fetch ------------------------------------------------------------
    def _start_fetch(self):
        mods = list(self._mods)

        def worker():
            out = []
            seen: set[int] = set()
            errors: list[str] = []
            try:
                for m in mods:
                    mod_id = int(m.get("mod_id", 0) or 0)
                    domain = m.get("domain", "") or ""
                    want = set(m.get("missing_ids") or set())
                    # Resolve via the owning mod's requirement graph (carries the
                    # requirement notes) when the mod is Nexus-hosted.
                    if mod_id > 0:
                        try:
                            reqs = self._api.get_mod_requirements(domain, mod_id)
                        except Exception as e:
                            errors.append(f"{m.get('mod_name','?')}: {e}")
                            reqs = []
                        for r in reqs:
                            if r.mod_id in want and r.mod_id not in seen:
                                seen.add(r.mod_id)
                                out.append(r)
                    # Any required ids the graph didn't cover - including every id
                    # when the owning mod is local (mod_id<=0, e.g. TTW's seeded
                    # requirements) - are resolved one mod at a time.
                    for rid in want:
                        if rid in seen:
                            continue
                        req = self._resolve_single(domain, rid, errors)
                        if req is not None:
                            seen.add(rid)
                            out.append(req)
            except Exception as e:
                safe_emit(self._reqs_ready, None, str(e))
                return
            err = ("; ".join(errors) if errors and not out else None)
            safe_emit(self._reqs_ready, out, err)

        threading.Thread(target=worker, daemon=True, name="missing-reqs-fetch").start()

    def _resolve_single(self, domain, mod_id, errors):
        """Build a NexusModRequirement for a single required *mod_id* by fetching
        the mod directly. Used for locally-seeded requirements (e.g. the TTW
        installer) where there's no owning-mod requirement graph to read notes
        from - so the mod's summary stands in for the requirement notes."""
        from Nexus.nexus_api import NexusModRequirement
        try:
            info = self._api.get_mod(domain, mod_id)
        except Exception as e:
            errors.append(f"mod {mod_id}: {e}")
            return None
        return NexusModRequirement(
            mod_id=mod_id,
            mod_name=getattr(info, "name", "") or f"Mod {mod_id}",
            game_domain=domain,
            notes=getattr(info, "summary", "") or "",
        )

    def _on_reqs_ready(self, reqs, error):
        if error is not None and not reqs:
            self._status.setText(self.tr("Could not load requirements: {0}").format(error))
            return
        if not reqs:
            self._status.setText(self.tr("No missing requirements found."))
            return
        self._status.setVisible(False)
        p = active_palette()
        # Insert cards before the trailing stretch, keeping a mod_id → card map
        # so cards can be pruned once their requirement is installed.
        insert_at = self._cards_layout.count() - 1
        for r in reqs:
            is_external = bool(getattr(r, "is_external", False))
            url = self._req_url(r, is_external)
            card = _ReqCard(
                p, r, url, is_external, self._open_url, self._install_req,
                ignored=self._req_ignored(int(r.mod_id or 0)),
                on_ignore=(self._toggle_req_ignored
                           if self._ignore_req_fn is not None else None))
            self._cards_layout.insertWidget(insert_at, card)
            insert_at += 1
            self._cards[int(r.mod_id)] = card

    def refresh_install_labels(self):
        """Re-read 'Download only' and relabel the per-card buttons."""
        for card in list(self._cards.values()):
            try:
                card.refresh_install_label()
            except RuntimeError:
                pass        # card already destroyed

    def prune_installed(self, installed_ids):
        """Remove the cards for any requirement whose mod_id is now installed
        (called after the modlist flags refresh, so this works no matter how the
        requirement got installed - panel Install button, manual, NXM, …).
        Shows the empty-state text once every card is gone."""
        installed = {int(i) for i in installed_ids or ()}
        for mid in [m for m in self._cards if m in installed]:
            card = self._cards.pop(mid)
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        if not self._cards:
            self._status.setText(self.tr("No missing requirements found."))
            self._status.setVisible(True)

    def _domain(self) -> str:
        return getattr(self._game, "nexus_game_domain", "") or (
            self._mods[0].get("domain", "") if self._mods else "")

    def _req_url(self, req, is_external: bool) -> str:
        """The link the View button opens. The GraphQL `url` is usually empty for
        Nexus-hosted requirements (only externals carry it), so fall back to the
        mod page built from the domain + mod_id (mirrors the Tk panel).

        NB `req.game_domain` is the GraphQL `gameId` - a NUMERIC id (e.g. 1704),
        not a domain slug - so it can't go in the URL. Use the view's real domain
        slug (the game's `nexus_game_domain`); only same-game reqs are supported."""
        if req.url:
            return req.url
        if is_external:
            return ""   # external w/ no url → nothing to open
        domain = self._domain()
        if domain and req.mod_id:
            return f"https://www.nexusmods.com/{domain}/mods/{req.mod_id}"
        return ""

    # ---- actions ----------------------------------------------------------
    def _open_url(self, url):
        if not url:
            return
        try:
            from Utils.xdg import open_url
            open_url(url)
        except Exception:
            pass

    def _install_req(self, req):
        """Hand the required mod off to the window's Nexus-install flow
        (premium → file pick → download → install). Use the view's real domain
        slug - `req.game_domain` is a numeric gameId, not a slug."""
        if self._install_fn is None:
            return
        self._install_fn(req.mod_id, self._domain(),
                         req.mod_name or f"Mod {req.mod_id}")

    # ---- per-requirement ignore --------------------------------------------
    def _req_owners(self, req_id: int) -> list:
        """The panel mods whose missing list contains *req_id* (a deduped card
        can belong to several owning mods)."""
        return [m for m in self._mods
                if req_id in (m.get("missing_ids") or set())]

    def _req_ignored(self, req_id: int) -> bool:
        """Checked state for a card: ignored in EVERY owning mod that lists it."""
        owners = self._req_owners(req_id)
        return bool(owners) and all(
            req_id in (m.get("ignored_ids") or set()) for m in owners)

    def _toggle_req_ignored(self, req, state: bool):
        """Persist a per-requirement ignore for every owning mod in the panel
        (the window writes meta.ini + refreshes the ⚠ flags), and mirror it in
        the local specs so the checked state stays consistent."""
        rid = int(getattr(req, "mod_id", 0) or 0)
        if rid <= 0 or self._ignore_req_fn is None:
            return
        owners = self._req_owners(rid)
        names = [m.get("mod_name", "") for m in owners if m.get("mod_name")]
        if not names:
            return
        for m in owners:
            s = m.setdefault("ignored_ids", set())
            (s.add if state else s.discard)(rid)
        try:
            self._ignore_req_fn(rid, req.mod_name or "", bool(state), names)
        except Exception as exc:
            self._log(f"Nexus: could not save the ignored requirement - {exc}")
