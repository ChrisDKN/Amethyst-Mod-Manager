from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton,
    QFrame, QSizePolicy, QComboBox, QPlainTextEdit,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.tooltips import escaped_tooltip
from Utils.collections.manifest import fmt_size


class CappedComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaxVisibleItems(15)

    def showPopup(self):
        super().showPopup()
        view = self.view()
        popup = view.window()
        rows = min(15, self.count())
        height = sum(max(view.sizeHintForRow(row), self.fontMetrics().height() + 8) for row in range(rows)) + 8
        screen = self.screen().availableGeometry()
        height = min(height, screen.height() - 20)
        popup.setFixedHeight(height)
        below = self.mapToGlobal(self.rect().bottomLeft())
        top = self.mapToGlobal(self.rect().topLeft())
        y = below.y() if below.y() + height <= screen.bottom() else top.y() - height
        popup.move(max(screen.left(), min(popup.x(), screen.right() - popup.width())), max(screen.top(), y))
        view.scrollTo(view.currentIndex())


class CheckRow(QFrame):
    """One requirement check rendered as a severity-striped row."""

    TONES = {"error": ("×", "TEXT_ERR"), "manual": ("↗", "TEXT_WARN"),
             "warning": ("!", "TEXT_WARN"), "pass": ("✓", "TEXT_MAIN")}

    def __init__(self, check, parent=None):
        super().__init__(parent)
        self._check = check
        self._expanded = False
        palette = active_palette()
        mark, tone = self.TONES.get(check.status, ("·", "TEXT_DIM"))
        colour = _c(palette, tone)
        self.setObjectName("CheckRow")
        blocking = check.status == "error"
        tint = f"background:{_c(palette, 'BG_ROW')};" if blocking else ""
        self.setStyleSheet(f"#CheckRow {{ {tint} border-bottom:1px solid {_c(palette, 'BORDER_FAINT')}; }}")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        stripe = QFrame(self)
        stripe.setFixedWidth(3)
        stripe.setStyleSheet(f"background:{colour if check.status != 'pass' else _c(palette, 'BORDER')};")
        outer.addWidget(stripe)
        body = QHBoxLayout()
        body.setContentsMargins(9, 8, 10, 9)
        body.setSpacing(9)
        outer.addLayout(body, 1)
        glyph = QLabel(mark, self)
        glyph.setFixedWidth(12)
        glyph.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        glyph.setStyleSheet(f"color:{colour}; font-weight:600;")
        body.addWidget(glyph)
        text = QVBoxLayout()
        text.setSpacing(2)
        body.addLayout(text, 1)
        count = f"  ({len(check.items):,})" if check.items else ""
        name = QLabel(check.name + count, self)
        name.setTextFormat(Qt.PlainText)
        name.setWordWrap(True)
        name.setStyleSheet(f"color:{colour}; font-weight:600;")
        text.addWidget(name)
        detail = QLabel(check.detail, self)
        detail.setTextFormat(Qt.PlainText)
        detail.setWordWrap(True)
        detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(detail)
        resolution = check.resolution
        if not resolution and check.status != "pass":
            from Utils.wabbajack.checks import make_check
            resolution = make_check(check.status, check.name, check.detail).resolution
        if resolution and check.status != "pass":
            advice = QLabel(resolution, self)
            advice.setTextFormat(Qt.PlainText)
            advice.setWordWrap(True)
            advice.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            advice.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
            text.addWidget(advice)
        if check.items:
            self._toggle = QToolButton(self)
            self._toggle.setCursor(Qt.PointingHandCursor)
            self._toggle.setStyleSheet(f"color:{_c(palette, 'ACCENT')}; padding:1px 0; border:none;")
            self._toggle.clicked.connect(self._toggle_items)
            text.addWidget(self._toggle, 0, Qt.AlignLeft)
            self._files = QPlainTextEdit(self)
            self._files.setReadOnly(True)
            self._files.setPlainText("\n".join(check.items))
            self._files.setMaximumHeight(120)
            self._files.hide()
            text.addWidget(self._files)
            self._sync_toggle()
        status = {"error": self.tr("Blocking: resolve before installing."),
                  "manual": self.tr("Needs your input: follow the download or setup instructions."),
                  "warning": self.tr("To review: read before continuing; this does not block installation."),
                  "pass": self.tr("Passed: this check is ready to proceed.")}.get(check.status, "")
        explanation = check.explanation
        if not explanation:
            from Utils.wabbajack.checks import make_check
            explanation = make_check(check.status, check.name, check.detail).explanation
        tooltip = escaped_tooltip("\n\n".join(piece for piece in (
            check.name, status,
            self.tr("What this means") + "\n" + explanation if explanation else "",
            self.tr("Details") + "\n" + check.detail) if piece))
        for widget in (name, detail):
            widget.setToolTip(tooltip)

    def _sync_toggle(self):
        total = len(self._check.items)
        self._toggle.setText(self.tr("Hide affected files") if self._expanded
                             else (self.tr("Show 1 affected file") if total == 1
                                   else self.tr("Show {0} affected files").format(total)))

    def _toggle_items(self):
        self._expanded = not self._expanded
        self._files.setVisible(self._expanded)
        self._sync_toggle()


class RequirementsSummary(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._show_passed = False
        self._placeholder = self.tr("Check requirements to verify game files, available space and runtime requirements. Review the results before installing.")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        heading.addWidget(self.summary, 1)
        self._passed = QToolButton(self)
        self._passed.setCheckable(True)
        self._passed.setCursor(Qt.PointingHandCursor)
        self._passed.toggled.connect(self._passed_toggled)
        heading.addWidget(self._passed)
        layout.addLayout(heading)
        self._message = QLabel(self)
        self._message.setTextFormat(Qt.PlainText)
        self._message.setWordWrap(True)
        self._message.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._message)
        self._list = QWidget(self)
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        layout.addWidget(self._list)
        self._list.hide()
        self.clear()

    def clear(self):
        self.setPlainText(self._placeholder)

    def _clear_rows(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def setPlainText(self, text):
        self._rows = []
        self._clear_rows()
        self._list.hide()
        self._passed.hide()
        self.summary.hide()
        self._message.setText(text)
        self._message.show()

    def _passed_toggled(self, checked):
        self._show_passed = checked
        self._render()

    def show_report(self, report):
        self._rows = list(report.checks)
        errors = sum(c.status == "error" for c in self._rows)
        manual = sum(c.status == "manual" for c in self._rows)
        warnings = sum(c.status == "warning" for c in self._rows)
        passed = sum(c.status == "pass" for c in self._rows)
        pieces = []
        if errors:
            pieces.append(self.tr("{0} blocking").format(errors))
        if manual:
            pieces.append(self.tr("1 needs your input") if manual == 1 else self.tr("{0} need your input").format(manual))
        if warnings:
            pieces.append(self.tr("{0} to review").format(warnings))
        self.summary.setText(" · ".join(pieces) or self.tr("Requirements passed"))
        palette = active_palette()
        tone = "TEXT_ERR" if errors else "TEXT_WARN" if manual or warnings else "TEXT_MAIN"
        self.summary.setStyleSheet(f"color:{_c(palette, tone)}; font-weight:600;")
        self.summary.show()
        self._passed.setText(self.tr("Passed ({0})").format(passed))
        self._passed.setVisible(bool(passed))
        self._passed.blockSignals(True)
        self._passed.setChecked(not pieces)
        self._show_passed = not pieces
        self._passed.blockSignals(False)
        self._message.hide()
        self._list.show()
        self._render()

    def _render(self):
        order = {"error": 0, "manual": 1, "warning": 2, "pass": 3}
        rows = sorted((c for c in self._rows if c.status != "pass" or self._show_passed),
                      key=lambda c: order.get(c.status, 2))
        self._clear_rows()
        for check in rows:
            self._list_layout.addWidget(CheckRow(check, self._list))


class PlanBar(QFrame):
    """Proportional bar showing cached / automatic / manual shares of the download."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(9)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        palette = active_palette()
        self.setStyleSheet(f"background:{_c(palette, 'BG_ROW')}; border-radius:4px;")
        self._segments = {}
        for key in ("ready", "automatic", "manual"):
            segment = QFrame(self)
            segment.setStyleSheet("background:transparent;")
            layout.addWidget(segment, 0)
            self._segments[key] = segment
        layout.addStretch(1)
        self._layout = layout

    def set_shares(self, shares, colours):
        total = sum(shares.values())
        for key, segment in self._segments.items():
            share = shares.get(key, 0)
            weight = int(round(share * 1000 / total)) if total else 0
            self._layout.setStretch(list(self._segments).index(key), weight)
            segment.setStyleSheet(f"background:{colours[key]};" if weight else "background:transparent;")
        self._layout.setStretch(3, 0 if total else 1)


class AcquisitionSummary(QWidget):
    KEYS = (("ready", "Already cached", "TEXT_OK_BRIGHT"),
            ("automatic", "Downloads automatically", "ACCENT"),
            ("manual", "Needs your input", "TEXT_WARN"))

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        palette = active_palette()
        self._bar = PlanBar(self)
        layout.addWidget(self._bar)
        self._rows = {}
        legend = QVBoxLayout()
        legend.setSpacing(5)
        layout.addLayout(legend)
        for key, title, tone in self.KEYS:
            row = QHBoxLayout()
            row.setSpacing(9)
            swatch = QFrame(self)
            swatch.setFixedSize(9, 9)
            swatch.setStyleSheet(f"background:{self._tone(tone)}; border-radius:2px;")
            row.addWidget(swatch, 0, Qt.AlignVCenter)
            label = QLabel(self.tr(title), self)
            label.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
            row.addWidget(label, 1)
            count = QLabel("—", self)
            count.setStyleSheet(f"color:{_c(palette, 'TEXT_FAINT')};")
            row.addWidget(count, 0, Qt.AlignRight)
            size = QLabel("—", self)
            size.setMinimumWidth(70)
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            size.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600;")
            row.addWidget(size, 0)
            legend.addLayout(row)
            self._rows[key] = (count, size, swatch)
        total_row = QHBoxLayout()
        total_row.setSpacing(9)
        self._total_label = QLabel(self.tr("Transfers over the network"), self)
        self._total_label.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        total_row.addWidget(self._total_label, 1)
        self._total = QLabel("—", self)
        self._total.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:700; font-size:15px;")
        total_row.addWidget(self._total, 0, Qt.AlignRight)
        layout.addLayout(total_row)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        layout.addWidget(self.note)
        self.clear()

    @staticmethod
    def _tone(tone):
        return _c(active_palette(), tone)

    def clear(self):
        for count, size, _ in self._rows.values():
            count.setText("—")
            size.setText("—")
        self._bar.set_shares({}, {key: self._tone(tone) for key, _, tone in self.KEYS})
        self._total.setText("—")
        self._total_label.setText(self.tr("Not checked"))
        self.note.setText(self.tr("Check requirements to verify cached files and Nexus access, then review what still needs downloading."))

    def show_report(self, request, report):
        from Utils.wabbajack.hosts import automatic_source
        if report.required_archives is None:
            self.clear()
            return
        totals = {key: [0, 0] for key, _, _ in self.KEYS}
        missing_game = 0
        for key in report.required_archives:
            archive = request.package.archives[key]
            if key in report.cached or key in report.game_files or key in report.prepared_game_files:
                category = "ready"
            elif archive.kind == "GameFileSource":
                missing_game += 1
                continue
            elif automatic_source(archive, request.premium):
                category = "automatic"
            else:
                category = "manual"
            totals[category][0] += 1
            totals[category][1] += archive.size
        colours = {key: self._tone(tone) for key, _, tone in self.KEYS}
        self._bar.set_shares({key: totals[key][1] for key in totals}, colours)
        for key, (count, size) in ((key, totals[key]) for key, _, _ in self.KEYS):
            count_label, size_label, _ = self._rows[key]
            size_label.setText(fmt_size(size) if size else self.tr("0 B"))
            count_label.setText(
                self.tr("1 archive") if count == 1
                else self.tr("{0} archives").format(f"{count:,}"))
        network = totals["automatic"][1] + totals["manual"][1]
        self._total_label.setText(self.tr("Transfers over the network"))
        self._total.setText(fmt_size(network) if network else self.tr("Nothing to download"))
        if missing_game:
            missing = self.tr("1 required game file is missing or differs.") if missing_game == 1 else self.tr("{0} required game files are missing or differ.").format(missing_game)
            self.note.setText(missing + " " + self.tr("Resolve the listed requirements before downloading."))
        elif totals["manual"][0]:
            self.note.setText(self.tr("Browser downloads and Select File use the normal installer prompts. Automatic downloads continue while you respond."))
        elif not report.download_bytes:
            self.note.setText(self.tr("No archive downloads needed. Verified local content will be reused."))
        else:
            self.note.setText(self.tr("Verified cache and game files are reused. Downloads follow your existing speed and concurrency settings."))
