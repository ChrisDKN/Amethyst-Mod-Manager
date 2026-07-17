#!/usr/bin/env python3
"""Translation Manager — a small PySide6 GUI over the i18n tooling.

A thin front-end that drives the sibling scripts in tools/i18n/ as subprocesses
so all the real logic stays in the tested scripts:

  * refresh_translations.sh   — merge new strings + machine-translate
  * libretranslate_server.sh  — start/stop the local LibreTranslate server
  * i18n_deepl.py / i18n_libre.py — the actual translation backends

Panels:
  1. Folder + language selection — pick the .ts dir, tick which languages.
  2. Per-language status table — strings vs the English base, unfinished count.
  3. Backend picker + DeepL quota — DeepL / LibreTranslate / Auto + live usage.
  4. LibreTranslate server controls — Start / Stop / Status.
  5. Run + live log.

Launch:  ./tools/i18n/translation_manager.sh   (from the repo root)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QFileDialog, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QRadioButton, QButtonGroup, QCheckBox, QGroupBox,
    QHeaderView, QMessageBox,
)

REPO = Path(__file__).resolve().parents[2]
I18N_DIR = Path(__file__).resolve().parent   # tools/i18n/ — the sibling scripts
EN_TS = REPO / "src" / "translations" / "amethyst_en.ts"
LT_URL = os.environ.get("AMM_LT_URL", "http://127.0.0.1:5000").rstrip("/")

# The app's shipped languages (must match src/translations / the tooling maps).
LANGS = ["fr", "de", "es", "it", "pt", "pt_BR", "ru", "pl", "zh", "ja",
         "nl", "cs"]
LANG_NAMES = {
    "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "pt_BR": "Portuguese (BR)", "ru": "Russian",
    "pl": "Polish", "zh": "Chinese", "ja": "Japanese", "nl": "Dutch",
    "cs": "Czech",
}


def _count_ts(path: Path) -> tuple[int, int]:
    """(source count, unfinished count) for a .ts file, or (0,0) if unreadable."""
    try:
        root = ET.parse(path).getroot()
        n = unf = 0
        for m in root.iter("message"):
            n += 1
            t = m.find("translation")
            if t is not None and t.get("type") == "unfinished":
                unf += 1
        return n, unf
    except Exception:
        return 0, 0


def _deepl_usage() -> "tuple[int, int] | None":
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not key:
        return None
    host = ("https://api-free.deepl.com/v2/usage" if key.endswith(":fx")
            else "https://api.deepl.com/v2/usage")
    try:
        req = urllib.request.Request(
            host, headers={"Authorization": f"DeepL-Auth-Key {key}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        return d["character_count"], d["character_limit"]
    except Exception:
        return None


def _libre_up() -> bool:
    try:
        urllib.request.urlopen(f"{LT_URL}/languages", timeout=3)
        return True
    except Exception:
        return False


class TranslationManager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Amethyst — Translation Manager")
        self.resize(880, 720)
        self._proc: QProcess | None = None
        self._lang_checks: dict[str, QCheckBox] = {}
        self._build()
        self._refresh_status()
        self._refresh_backends()
        # Poll the LibreTranslate server + DeepL quota periodically.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_backends)
        self._poll.start(5000)

    # ---- layout -----------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)

        # --- Folder picker ---
        fbox = QGroupBox("Translations folder")
        fl = QHBoxLayout(fbox)
        self._dir_edit = QLineEdit(str(REPO / "src" / "translations"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self._pull_btn = QPushButton("⭳ Pull from Resources")
        self._pull_btn.setToolTip(
            "Download the current language .ts files from the Resources branch "
            "on GitHub into this folder (so you can refresh without switching "
            "branches). English is skipped — it's built locally.")
        self._pull_btn.clicked.connect(self._pull_from_resources)
        reload_btn = QPushButton("Reload status")
        reload_btn.clicked.connect(self._refresh_status)
        fl.addWidget(self._dir_edit, 1)
        fl.addWidget(browse)
        fl.addWidget(self._pull_btn)
        fl.addWidget(reload_btn)
        v.addWidget(fbox)

        # --- Status table ---
        sbox = QGroupBox("Languages")
        sl = QVBoxLayout(sbox)
        self._table = QTableWidget(len(LANGS), 5)
        self._table.setHorizontalHeaderLabels(
            ["", "Language", "Strings", "Unfinished", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        for r, code in enumerate(LANGS):
            cb = QCheckBox()
            cb.setChecked(True)
            self._lang_checks[code] = cb
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 0, 0); hl.addWidget(cb)
            self._table.setCellWidget(r, 0, holder)
            self._table.setItem(r, 1, QTableWidgetItem(
                f"{LANG_NAMES.get(code, code)}  ({code})"))
            for c in (2, 3, 4):
                self._table.setItem(r, c, QTableWidgetItem("—"))
        sl.addWidget(self._table)
        selrow = QHBoxLayout()
        allbtn = QPushButton("Select all"); allbtn.clicked.connect(
            lambda: self._set_all_langs(True))
        nonebtn = QPushButton("Select none"); nonebtn.clicked.connect(
            lambda: self._set_all_langs(False))
        outdbtn = QPushButton("Only out-of-date"); outdbtn.clicked.connect(
            self._select_outdated)
        selrow.addWidget(allbtn); selrow.addWidget(nonebtn)
        selrow.addWidget(outdbtn); selrow.addStretch(1)
        self._en_lbl = QLabel("English base: —")
        selrow.addWidget(self._en_lbl)
        sl.addLayout(selrow)
        v.addWidget(sbox, 1)

        # --- Backend + server row ---
        row = QHBoxLayout()

        bbox = QGroupBox("Translation backend")
        bl = QVBoxLayout(bbox)
        self._bg = QButtonGroup(self)
        self._rb_auto = QRadioButton("Auto (DeepL if quota, else LibreTranslate)")
        self._rb_deepl = QRadioButton("DeepL")
        self._rb_libre = QRadioButton("LibreTranslate")
        self._rb_auto.setChecked(True)
        for rb in (self._rb_auto, self._rb_deepl, self._rb_libre):
            self._bg.addButton(rb); bl.addWidget(rb)
        self._deepl_lbl = QLabel("DeepL: —")
        self._deepl_lbl.setStyleSheet("color:#888;")
        bl.addWidget(self._deepl_lbl)
        row.addWidget(bbox, 1)

        lbox = QGroupBox("LibreTranslate server")
        ll = QVBoxLayout(lbox)
        self._lt_lbl = QLabel("checking…")
        ll.addWidget(self._lt_lbl)
        btnrow = QHBoxLayout()
        self._lt_start = QPushButton("Start")
        self._lt_start.clicked.connect(self._start_server)
        self._lt_stop = QPushButton("Stop")
        self._lt_stop.clicked.connect(self._stop_server)
        btnrow.addWidget(self._lt_start); btnrow.addWidget(self._lt_stop)
        ll.addLayout(btnrow)
        note = QLabel("First start downloads models (slow once; cached after).")
        note.setStyleSheet("color:#888; font-size:11px;"); note.setWordWrap(True)
        ll.addWidget(note)
        row.addWidget(lbox, 1)
        v.addLayout(row)

        # --- Run + log ---
        runrow = QHBoxLayout()
        self._audit_btn = QPushButton("Check for unwrapped strings")
        self._audit_btn.setToolTip(
            "Scan gui_qt/ + wizards_qt/ for user-facing strings NOT wrapped in "
            "tr() — those won't be translatable. Run after adding new UI text.")
        self._audit_btn.clicked.connect(self._run_audit)
        self._run_btn = QPushButton("▶  Refresh translations")
        self._run_btn.setStyleSheet(
            "QPushButton{background:#3a7a3a; color:#fff; font-weight:600;"
            " padding:8px 16px; border-radius:4px;}"
            "QPushButton:disabled{background:#555;}")
        self._run_btn.clicked.connect(self._run_refresh)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel)
        runrow.addWidget(self._audit_btn)
        runrow.addWidget(self._run_btn, 1)
        runrow.addWidget(self._cancel_btn)
        v.addLayout(runrow)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "background:#1e1e1e; color:#ddd; font-family:monospace;"
            " font-size:12px;")
        v.addWidget(self._log, 1)

    # ---- helpers ----------------------------------------------------------
    def _browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "Pick the translations folder", self._dir_edit.text())
        if d:
            self._dir_edit.setText(d)
            self._refresh_status()

    def _set_all_langs(self, on: bool):
        for cb in self._lang_checks.values():
            cb.setChecked(on)

    def _select_outdated(self):
        en_n, _ = _count_ts(EN_TS)
        d = Path(self._dir_edit.text())
        for code, cb in self._lang_checks.items():
            n, unf = _count_ts(d / f"amethyst_{code}.ts")
            cb.setChecked(n != en_n or unf > 0 or n == 0)

    def _refresh_status(self):
        en_n, _ = _count_ts(EN_TS)
        self._en_lbl.setText(f"English base: {en_n} strings")
        d = Path(self._dir_edit.text())
        for r, code in enumerate(LANGS):
            f = d / f"amethyst_{code}.ts"
            n, unf = _count_ts(f)
            self._table.item(r, 2).setText(str(n) if n else "—")
            self._table.item(r, 3).setText(str(unf))
            if not f.is_file():
                status, color = "missing", "#c66"
            elif n != en_n:
                status, color = f"behind ({en_n - n} new)", "#e0a040"
            elif unf > 0:
                status, color = f"{unf} untranslated", "#e0a040"
            else:
                status, color = "up to date", "#6bc76b"
            it = self._table.item(r, 4)
            it.setText(status)
            it.setForeground(Qt.GlobalColor.white if color == "#6bc76b"
                             else Qt.GlobalColor.white)
            it.setData(Qt.ForegroundRole, None)
            from PySide6.QtGui import QColor
            it.setForeground(QColor(color))

    def _refresh_backends(self):
        # DeepL quota.
        usage = _deepl_usage()
        if usage is None:
            self._deepl_lbl.setText("DeepL: no key / unreachable")
            self._deepl_lbl.setStyleSheet("color:#888;")
        else:
            used, lim = usage
            pct = used * 100 // lim if lim else 0
            room = used < lim
            self._deepl_lbl.setText(
                f"DeepL: {used:,}/{lim:,} chars ({pct}%)"
                + ("" if room else "  — EXHAUSTED"))
            self._deepl_lbl.setStyleSheet(
                f"color:{'#6bc76b' if room else '#c66'};")
        # LibreTranslate server.
        if _libre_up():
            self._lt_lbl.setText("● running")
            self._lt_lbl.setStyleSheet("color:#6bc76b;")
            self._lt_start.setEnabled(False); self._lt_stop.setEnabled(True)
        else:
            self._lt_lbl.setText("○ not running")
            self._lt_lbl.setStyleSheet("color:#888;")
            self._lt_start.setEnabled(True); self._lt_stop.setEnabled(False)

    def _selected_langs(self) -> list[str]:
        return [c for c, cb in self._lang_checks.items() if cb.isChecked()]

    # ---- subprocess driving ----------------------------------------------
    def _log_line(self, text: str):
        self._log.appendPlainText(text.rstrip("\n"))
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _run(self, argv: list[str], env_extra: dict | None = None,
             on_done=None):
        """Run a command, streaming stdout+stderr into the log."""
        if self._proc is not None:
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        from PySide6.QtCore import QProcessEnvironment
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.setWorkingDirectory(str(REPO))
        env = QProcessEnvironment.systemEnvironment()
        for k, val in (env_extra or {}).items():
            env.insert(k, val)
        self._proc.setProcessEnvironment(env)
        self._proc.readyReadStandardOutput.connect(
            lambda: self._log_line(bytes(
                self._proc.readAllStandardOutput()).decode("utf-8", "replace")))

        def _finished(code, _status):
            self._log_line(f"\n[exit {code}]")
            self._proc = None
            self._run_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._refresh_status()
            self._refresh_backends()
            if on_done:
                on_done(code)

        self._proc.finished.connect(_finished)
        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._log_line("$ " + " ".join(argv))
        self._proc.start(argv[0], argv[1:])

    def _cancel(self):
        if self._proc is not None:
            self._proc.kill()

    def _start_server(self):
        self._log_line("Starting LibreTranslate server (may download models)…")
        self._run(["bash", str(I18N_DIR / "libretranslate_server.sh"), "start"]
                  + self._server_langs())

    def _server_langs(self) -> list[str]:
        # Let the script use its default set (all shipped langs).
        return []

    def _stop_server(self):
        self._run(["bash", str(I18N_DIR / "libretranslate_server.sh"), "stop"])

    def _run_audit(self):
        """Scan gui_qt/ + wizards_qt/ for user-facing strings NOT wrapped in
        tr(). Uses i18n_wrap.find_sites() in-process; 'wrappable' sites are the
        real misses (they can be auto-wrapped). Streams a report to the log."""
        self._log.clear()
        self._log_line("Scanning gui_qt/ + wizards_qt/ for unwrapped strings…\n")
        try:
            import importlib
            sys.path.insert(0, str(I18N_DIR))
            wrap = importlib.import_module("i18n_wrap")
        except Exception as e:
            self._log_line(f"[error] could not load i18n_wrap: {e}")
            return
        import ast
        import re as _re
        files = sorted(
            list((REPO / "src" / "gui_qt").glob("*.py"))
            + list((REPO / "src" / "wizards_qt").glob("*.py")))
        total_wrap = 0       # auto-wrappable misses (self.tr can be applied)
        total_manual = 0     # need a hand fix (ternary/f-string/no-self/literal)
        flagged = 0
        for f in files:
            try:
                src = f.read_text(encoding="utf-8")
                tree = ast.parse(src)
                wrapped, skipped = wrap.find_sites(tree, src)
                literals = wrap.find_literal_sites(tree, src)
            except Exception:
                continue
            src_lines = src.splitlines()
            real = [s for s in wrapped
                    if self._is_real_miss(s, src_lines)]
            manual = [s for s in (list(skipped) + list(literals))
                      if self._is_real_miss(s, src_lines)]
            if real or manual:
                flagged += 1
                total_wrap += len(real)
                total_manual += len(manual)
                rel = f.relative_to(REPO)
                self._log_line(
                    f"⚠ {rel}: {len(real)} auto-wrappable, "
                    f"{len(manual)} manual")
                for s in (real + manual)[:12]:
                    tag = "" if s in real else f"  [{s.reason}]"
                    line = src_lines[s.lineno - 1].strip()
                    self._log_line(f"    L{s.lineno}: {line[:72]}{tag}")
                if len(real) + len(manual) > 12:
                    self._log_line(
                        f"    …and {len(real) + len(manual) - 12} more")
        self._log_line("")
        if total_wrap == 0 and total_manual == 0:
            self._log_line("✓ No unwrapped user-facing strings found. "
                           "Everything is translatable.")
        else:
            self._log_line(
                f"Found {total_wrap} auto-wrappable + {total_manual} "
                f"manual string(s) in {flagged} file(s).")
            if total_wrap:
                self._log_line(
                    "Auto-wrap the first group with:  "
                    "./tools/i18n/i18n_wrap.py <file> --apply")
            if total_manual:
                self._log_line(
                    "Manual group = ternary / complex f-string / no-self / "
                    "UI literal (list/dict). These need a hand-written tr() "
                    "template or a QT_TRANSLATE_NOOP registration.")

    def _pull_from_resources(self):
        """Download the language .ts from the Resources branch into the current
        folder (so the refresh can run without switching branches)."""
        d = self._dir_edit.text().strip()
        if not d:
            QMessageBox.warning(self, "No folder", "Pick a destination folder.")
            return
        if Path(d).resolve() == (REPO / "src" / "translations").resolve():
            r = QMessageBox.question(
                self, "Pull into src/translations?",
                "This will download the Resources .ts into src/translations/, "
                "overwriting anything there. Continue?")
            if r != QMessageBox.Yes:
                return
        self._log.clear()
        self._run([sys.executable,
                   str(I18N_DIR / "pull_from_resources.py"), d])

    @staticmethod
    def _is_real_miss(site, src_lines) -> bool:
        """Filter i18n_wrap false positives so the audit only reports genuine
        unwrapped user-facing text. Drops:
          * the literal it wants to wrap being a short lowercase CODE
            (dim/ok/err/warn/… — status kinds, not UI text; from _set_status's
            dual signature), or a number/percent/glyph-only string;
          * a site on a line that already contains self.tr( for the same arg
            (the flagged token is a sibling arg, not missed text)."""
        import re as _re
        repl = getattr(site, "replacement", "")
        m = _re.search(r'tr\((["\'])(.*?)\1\)', repl)
        if m:
            text = m.group(2)
        else:
            # Report-only sites (ternary / f-string / no-self / UI literal) carry
            # no replacement — pull the first string literal off the source line
            # so the code-word/glyph filters below still apply.
            line = src_lines[site.lineno - 1] if site.lineno <= len(src_lines) else ""
            lm = _re.search(r'(["\'])(.*?)\1', line)
            text = lm.group(2) if lm else ""
        # short lowercase code word (dim, ok, err, warn, info, success, …)
        if text and _re.fullmatch(r"[a-z_]{1,8}", text):
            return False
        # no translatable letters (numbers, %, glyphs, placeholders only)
        if text and not any(ch.isalpha() for ch in _re.sub(r"\{\d+\}", "", text)):
            return False
        return True

    def _run_refresh(self):
        langs = self._selected_langs()
        if not langs:
            QMessageBox.information(self, "No languages",
                                    "Tick at least one language to refresh.")
            return
        d = self._dir_edit.text().strip()
        if not Path(d).is_dir():
            QMessageBox.warning(self, "Bad folder",
                                f"Not a directory:\n{d}")
            return
        env = {}
        if self._rb_deepl.isChecked():
            env["AMM_MT_BACKEND"] = "deepl"
        elif self._rb_libre.isChecked():
            env["AMM_MT_BACKEND"] = "libre"
        # Auto → let the script decide (no override).
        self._log.clear()
        self._run(["bash", str(I18N_DIR / "refresh_translations.sh"), d] + langs,
                  env_extra=env)


def main() -> int:
    app = QApplication(sys.argv)
    w = TranslationManager()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
