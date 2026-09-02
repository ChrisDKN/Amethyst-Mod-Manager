"""Modlist delegate - paints rows for the QTreeView.

Graduates the spike's painting onto the multi-column model:
  - separator rows: full-width band + bold label
  - Name column: conflict strip, checkbox, lock glyph, elided name
  - other columns: plain text via the base delegate

Colours come from the active palette so themes carry over.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import Qt, QRect, QSize, QEvent, QT_TRANSLATE_NOOP
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPen, QBrush, QLinearGradient,
)
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QToolTip

from gui_qt.theme_qt import bind_theme, _c, qc, qc_contrast
from gui_qt.icons import icon
from gui_qt.tooltips import wrap_tooltip
from gui_qt.modlist_model import (
    EntryRole, ConflictRole, BsaConflictRole, UuidConflictRole, FlagsRole,
    HighlightRole,
    COL_NAME, COL_FLAGS, COL_CONFLICTS, COL_VERSION, COL_PRIORITY,
)
from gui_qt.modlist_data import (
    FLAG_UPDATE, FLAG_ENDORSED, FLAG_ROOT, FLAG_MODIFIED_MF, FLAG_MISSING_REQS,
    FLAG_COLLECTION_BUNDLED, FLAG_COLLECTION_PATCHED, FLAG_NOTE, FLAG_XEDIT,
    FLAG_BUNDLE, FLAG_MODIO_UPDATE, FLAG_PRERTX, FLAG_ROOT_RULE,
    FLAG_RERUN_FOMOD, FLAG_THUNDERSTORE_UPDATE,
)

# Flag bit → icon filename, painted left-to-right in the Flags column, in the
# SAME order as the Tk modlist (gui/modlist_panel.py ~2878): note, bundle,
# missing-reqs, update, modio-update, endorsed, info (pre-RTX OR collection -
# mutually exclusive, handled in _flag_icons), modified-MF, xEdit, root.
_FLAG_ICONS = [
    (FLAG_NOTE, "note.png"),
    (FLAG_BUNDLE, "bundle_settings.png"),
    (FLAG_MISSING_REQS, "warning.png"),
    (FLAG_RERUN_FOMOD, "rerun_fomod.png"),
    (FLAG_UPDATE, "update.png"),
    (FLAG_MODIO_UPDATE, "update_modio.png"),
    (FLAG_THUNDERSTORE_UPDATE, "update_thunderstore.png"),
    (FLAG_ENDORSED, "endorsed.png"),
    # info.png: pre-RTX OR collection bundled/patched - only ONE ever paints (see
    # _flag_icons). The hover tooltip distinguishes which.
    (FLAG_PRERTX, "info.png"),
    (FLAG_COLLECTION_BUNDLED, "info.png"),
    (FLAG_COLLECTION_PATCHED, "info.png"),
    (FLAG_MODIFIED_MF, "eye2_white.png"),
    (FLAG_XEDIT, "brush.png"),
    # root.png: meta root_folder OR a custom root-routing rule (same icon).
    (FLAG_ROOT, "root.png"),
    (FLAG_ROOT_RULE, "root.png"),
]

_MONO_FLAG_ICONS = {"bundle_settings.png", "root.png", "eye2_white.png"}

# The info-icon flags, in precedence order (Tk: pre-RTX wins, else collection).
_INFO_FLAGS = (FLAG_PRERTX, FLAG_COLLECTION_BUNDLED, FLAG_COLLECTION_PATCHED)
# The root-icon flags - only one root.png ever paints.
_ROOT_FLAGS = (FLAG_ROOT, FLAG_ROOT_RULE)
_CLICKABLE_FLAG_BITS = frozenset({
    FLAG_NOTE,
    FLAG_BUNDLE,
    FLAG_MISSING_REQS,
    FLAG_RERUN_FOMOD,
    FLAG_UPDATE,
    FLAG_MODIO_UPDATE,
    FLAG_THUNDERSTORE_UPDATE,
})
_ALIGN_CENTER = Qt.AlignVCenter | Qt.AlignHCenter
_ALIGN_LEFT = Qt.AlignVCenter | Qt.AlignLeft

# Flag bit → hover tooltip text (verbatim from the Tk modlist, ~5114). The two
# root sources have DISTINCT text (meta root_folder vs a custom routing rule);
# the note/xedit/missing tips are per-mod dynamic (see _flag_tip in the delegate).
# Wrapped in self.tr() at show time (see _flag_tip); registered here for lupdate.
_FLAG_TIPS = {
    FLAG_NOTE: QT_TRANSLATE_NOOP("ModRowDelegate", "Note"),
    FLAG_BUNDLE: QT_TRANSLATE_NOOP("ModRowDelegate", "Click here to open bundle settings"),
    FLAG_MISSING_REQS: QT_TRANSLATE_NOOP("ModRowDelegate", "Missing requirements"),
    FLAG_RERUN_FOMOD: QT_TRANSLATE_NOOP("ModRowDelegate", "A FOMOD patch option's plugin is now installed - click to re-run the FOMOD installer"),
    FLAG_UPDATE: QT_TRANSLATE_NOOP("ModRowDelegate", "Update available on Nexus Mods"),
    FLAG_MODIO_UPDATE: QT_TRANSLATE_NOOP("ModRowDelegate", "Update available on mod.io"),
    FLAG_THUNDERSTORE_UPDATE: QT_TRANSLATE_NOOP("ModRowDelegate", "Update available on Thunderstore"),
    FLAG_ENDORSED: QT_TRANSLATE_NOOP("ModRowDelegate", "Endorsed"),
    FLAG_PRERTX: QT_TRANSLATE_NOOP("ModRowDelegate", "Pre-RTX mod"),
    FLAG_COLLECTION_BUNDLED: QT_TRANSLATE_NOOP("ModRowDelegate", "This mod is a collection bundled mod"),
    FLAG_COLLECTION_PATCHED: QT_TRANSLATE_NOOP("ModRowDelegate", "This mod has diff patches applied by the collection install"),
    FLAG_MODIFIED_MF: QT_TRANSLATE_NOOP("ModRowDelegate", "Modified in Mod Files tab"),
    FLAG_XEDIT: QT_TRANSLATE_NOOP("ModRowDelegate", "Contains a plugin modified in xEdit"),
    FLAG_ROOT: QT_TRANSLATE_NOOP("ModRowDelegate", "This mod is sent to the root folder"),
    FLAG_ROOT_RULE: QT_TRANSLATE_NOOP("ModRowDelegate", "This mod contains files that route to the game root"),
}


def _note_to_tooltip_html(note: str) -> str:
    """Render a Markdown note as HTML for a rich-text tooltip.

    Qt's QToolTip auto-detects rich text, so returning HTML makes headings,
    bold/italic, lists and links render. QTextDocument.setMarkdown does the
    conversion with no external dependency (GitHub dialect → tables, strikethrough,
    task lists). Falls back to the plain (HTML-escaped) text if anything goes
    wrong."""
    try:
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setMarkdown(note, QTextDocument.MarkdownDialectGitHub)
        return doc.toHtml()
    except Exception:
        import html
        return f'<div style="white-space:pre-wrap;">{html.escape(note)}</div>'

# Conflict code → icon (lightning), painted in the Conflicts column (Tk parity).
_CONFLICT_ICONS = {
    1: "conflict-winner.png",
    -1: "conflict-loser.png",
    2: "conflict-mixed.png",
    3: "conflict-redundant.png",   # FULL - fully overridden / redundant
}

# BSA/BA2 archive conflict gets its own icon set (drawn right of the loose one).
_BSA_CONFLICT_ICONS = {
    1: "archive-conflict-winner.png",
    -1: "archive-conflict-loser.png",
    2: "archive-conflict-mixed.png",
    3: "archive-conflict-redundant.png",   # FULL - every archive file overridden
}

# BG3 module-UUID conflicts: two mods shipping the same .pak module under
# different file names. Own icon set - "partial" means a mod with several paks
# won some UUIDs and lost others. There is no redundant/FULL variant: a mod that
# loses every one of its paks is still just a loser.
_UUID_CONFLICT_ICONS = {
    1: "UUID_Winner.png",
    -1: "UUID_Loser.png",
    2: "UUID_Partial.png",
    3: "UUID_Loser.png",
}

# Conflict code → hover tooltip (verbatim from the Tk modlist, ~5217). Loose-file
# and BSA conflicts each get their own text; static so lupdate can extract them
# (wrapped in self.tr() at show time - see _conflict_tip).
_LOOSE_CONFLICT_TIPS = {
    1:  QT_TRANSLATE_NOOP("ModRowDelegate", "Loose file conflict - Winning"),
    -1: QT_TRANSLATE_NOOP("ModRowDelegate", "Loose file conflict - Losing"),
    2:  QT_TRANSLATE_NOOP("ModRowDelegate", "Loose file conflict - Partial"),
    3:  QT_TRANSLATE_NOOP("ModRowDelegate", "Loose file conflict - Full"),
}
_BSA_CONFLICT_TIPS = {
    1:  QT_TRANSLATE_NOOP("ModRowDelegate", "Archive conflict - Winning"),
    -1: QT_TRANSLATE_NOOP("ModRowDelegate", "Archive conflict - Losing"),
    2:  QT_TRANSLATE_NOOP("ModRowDelegate", "Archive conflict - Partial"),
    3:  QT_TRANSLATE_NOOP("ModRowDelegate", "Archive conflict - Full"),
}
_UUID_CONFLICT_TIPS = {
    c: QT_TRANSLATE_NOOP("ModRowDelegate", "UUID Conflict")
    for c in (1, -1, 2, 3)
}

def _lin(c: float) -> float:
    """sRGB channel -> linear light. Module level on purpose: pyside6-lupdate
    loses class context for the REST OF THE FILE when a def sits inside a block
    (try/if/for) in a MODULE-LEVEL function - self.tr() strings then extract
    with an empty <name/> context and can never be translated. (The same
    nesting inside a method is fine - the class context is already open.)"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _contrasting_text_color(hex_bg: str) -> str:
    """'#111111' or '#eeeeee' based on the luminance of *hex_bg* so separator
    text stays readable on a custom colour. Inlined from gui.theme (which pulls
    in customtkinter/tkinter - unavailable in the Qt app)."""
    try:
        hex_bg = hex_bg.lstrip("#")
        r, g, b = (int(hex_bg[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
        return "#111111" if lum > 0.179 else "#eeeeee"
    except Exception:
        return "#eeeeee"

_SEP_GRAD_LIGHT = 110
_SEP_GRAD_DARK = 107

def _sep_gradient(base: QColor, r) -> QLinearGradient:
    """Top-to-bottom gradient derived from *base*, spanning the row rect *r*.

    Dark bands lighten toward the bottom and light bands darken, so the effect
    stays visible whichever end of the luminance range the colour sits at."""
    g = QLinearGradient(0, r.top(), 0, r.bottom())
    if base.lightness() < 128:
        g.setColorAt(0.0, base.darker(_SEP_GRAD_DARK))
        g.setColorAt(1.0, base.lighter(_SEP_GRAD_LIGHT))
    else:
        g.setColorAt(0.0, base.lighter(_SEP_GRAD_LIGHT))
        g.setColorAt(1.0, base.darker(_SEP_GRAD_DARK))
    return g


# Target contrast ratio of the strike line against the band behind it. ~2.1
# reads clearly at 1px without competing with the label (which sits at 5-12).
_SEP_RULE_CONTRAST = 1.5
_SEP_RULE_ALPHA_MIN = 60
_SEP_RULE_ALPHA_MAX = 160


def _rel_luminance(c: QColor) -> float:
    """WCAG relative luminance of *c* (sRGB, 0.0-1.0)."""
    def _lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * _lin(c.redF()) + 0.7152 * _lin(c.greenF())
            + 0.0722 * _lin(c.blueF()))


def _contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio between two opaque colours (1.0-21.0)."""
    l1, l2 = sorted((_rel_luminance(a), _rel_luminance(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


@lru_cache(maxsize=64)
def _sep_rule_alpha(text_rgb: int, band_rgb: int) -> int:
    """Alpha that puts *text_rgb* over *band_rgb* at _SEP_RULE_CONTRAST.

    Solved rather than fixed: themes vary a lot in how strongly TEXT_SEP reads
    against BG_SEP (5.2 in light, 11.8 in adwaita), so one flat alpha leaves the
    weaker ones washed out. Keying on the achieved contrast instead makes the
    line equally visible everywhere, custom separator colours included. Cached
    because paint() runs per visible cell."""
    text, band = QColor.fromRgb(text_rgb), QColor.fromRgb(band_rgb)
    # Monotonic in alpha, so a plain scan at integer precision is enough.
    for a in range(_SEP_RULE_ALPHA_MIN, _SEP_RULE_ALPHA_MAX + 1):
        f = a / 255.0
        mixed = QColor.fromRgbF(
            text.redF() * f + band.redF() * (1 - f),
            text.greenF() * f + band.greenF() * (1 - f),
            text.blueF() * f + band.blueF() * (1 - f))
        if _contrast_ratio(mixed, band) >= _SEP_RULE_CONTRAST:
            return a
    return _SEP_RULE_ALPHA_MAX


def _sep_rule_pen(text_color: QColor, band: QColor) -> QPen:
    """1px pen for a separator's strike line, derived from its *text_color*.

    BORDER is tuned for panel edges against the deep window background, so on
    the lighter BG_SEP band it lands at a 1.0-1.7 contrast ratio - invisible in
    every theme. The separator's own text colour is guaranteed to read against
    the band it sits on (including user-picked custom colours, where it comes
    from _contrasting_text_color), so the rule borrows it at the alpha that
    hits _SEP_RULE_CONTRAST: clearly visible, still subordinate to the label."""
    c = QColor(text_color)
    c.setAlpha(_sep_rule_alpha(QColor(text_color).rgb(), QColor(band).rgb()))
    return QPen(c, 1)


# Row metrics - ~10% larger than the Tk baseline (30px) for readability.
ROW_H = 33
SEP_H = 33
CHECK_BOX = 17
ICON_SZ = 20        # flag / conflict / arrow / lock icon size
FONT_PX = 14        # row text size


class ModRowDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        bind_theme(self, roles={
            "BG_ROW", "BG_ROW_ALT", "BG_ROW_HOVER", "BG_SELECT", "BG_SEP",
            "BG_DEEP", "TEXT_MAIN", "TEXT_DIM", "TEXT_ON_ACCENT",
            "TEXT_SEP", "TEXT_WARN", "TEXT_ERR_BRIGHT", "TEXT_OK_BRIGHT",
            "BORDER", "CHECK_FILL", "DROPDOWN_ARROW", "LINK_BLUE",
            "CONFLICT_HL_WIN", "CONFLICT_HL_LOSE", "CONFLICT_HL_ANCHOR",
            "REQ_HL_REQUIRES", "REQ_HL_REQUIRED_BY", "OVERWRITE_SEP_BG",
            "OVERWRITE_SEP_FG", "ROOT_SEP_BG", "ROOT_SEP_FG",
        })
        # Shared row/label fonts - paint() runs per visible cell, so build
        # these once instead of allocating a QFont per call.
        self.f_row = QFont()
        self.f_row.setPixelSize(FONT_PX)
        self.f_bold = QFont()
        self.f_bold.setBold(True)
        self.f_bold.setPixelSize(FONT_PX)
        self.fm_row = QFontMetrics(self.f_row)
        self._name_widths: dict[str, int] = {}

    def refresh_theme(self, p: dict) -> None:
        self.c_sep_bg = qc(p, "BG_SEP")
        self.c_sep_text = qc(p, "TEXT_SEP")
        self.c_row = qc(p, "BG_ROW")
        self.c_row_alt = qc(p, "BG_ROW_ALT")
        self.c_sel = qc(p, "BG_SELECT")
        self.c_hover = qc(p, "BG_ROW_HOVER")
        self.c_text = qc(p, "TEXT_MAIN")
        self.c_text_dim = qc(p, "TEXT_DIM")
        self.c_text_on_sel = qc(p, "TEXT_ON_ACCENT")
        self.c_tick = qc_contrast(p, "CHECK_FILL")   # tick reads on the checkbox fill
        self.c_border = qc(p, "BORDER")
        self.c_arrow = _c(p, "DROPDOWN_ARROW")   # separator collapse arrow tint
        self.c_flag_tint = "#" + _c(p, "TEXT_MAIN").lstrip("#")
        self.c_lock = qc(p, "TEXT_WARN")
        self.c_win = qc(p, "TEXT_OK_BRIGHT")
        self.c_lose = qc(p, "TEXT_ERR_BRIGHT")
        self.c_check = qc(p, "CHECK_FILL")   # checkbox fill when enabled
        self.c_check_off = qc(p, "BG_DEEP")   # checkbox fill when disabled
        self.c_overwrite_bg = qc(p, "OVERWRITE_SEP_BG")  # Overwrite band
        self.c_root_bg = qc(p, "ROOT_SEP_BG")            # Root Folder band
        # Cross-panel highlight row tints (palette-driven; seeded from Tk colours).
        self.c_hl_higher = qc(p, "CONFLICT_HL_WIN")    # selection beats this mod (green)
        self.c_hl_lower = qc(p, "CONFLICT_HL_LOSE")    # this mod beats selection (red)
        self.c_hl_anchor = qc(p, "CONFLICT_HL_ANCHOR") # plugin-selected mod (orange)
        self.c_hl_requires = qc(p, "REQ_HL_REQUIRES")        # selection requires this mod (purple)
        self.c_hl_required_by = qc(p, "REQ_HL_REQUIRED_BY")  # this mod requires selection (blue)
        self.c_root_text = qc(p, "ROOT_SEP_FG")
        self.c_overwrite_text = qc(p, "OVERWRITE_SEP_FG")
        self.c_badge = qc(p, "LINK_BLUE")   # separator deploy-path badge
        parent = self.parent()
        if parent is not None:
            try:
                parent.viewport().update()
            except AttributeError:
                parent.update()

    def sizeHint(self, opt, index):
        e = index.data(EntryRole)
        h = SEP_H if (e and e.is_separator) else ROW_H
        return QSize(opt.rect.width(), h)

    def paint(self, p, opt, index):
        e = index.model().entry(index.row())
        if e is None:
            super().paint(p, opt, index)
            return
        r = opt.rect
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        # Separator: paint a full band only on the name column; blank elsewhere
        # so the band reads as one strip across the row. A collapsed separator
        # whose child mod is a conflict partner is tinted green/red/orange.
        if e.is_separator:
            from gui_qt.modlist_model import OVERWRITE_NAME, ROOT_FOLDER_NAME
            from gui_qt.modlist_sort import DIVIDER_NAME
            if e.name == DIVIDER_NAME:
                # Reverse-priority float divider: a thin dashed centred line on
                # a plain row background, no controls (Tk BOUNDARY_NAME row).
                p.fillRect(r, self.c_row)
                # Sits on the plain row background, so it keys off TEXT_DIM
                # rather than the separator band's TEXT_SEP.
                pen = _sep_rule_pen(self.c_text_dim, self.c_row)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                cy = r.center().y()
                p.drawLine(r.left() + 8, cy, r.right() - 8, cy)
                p.restore()
                return
            sep_hl = index.data(HighlightRole) or 0
            selected = bool(opt.state & QStyle.State_Selected)
            # A custom colour applies only to plain separators, and only when no
            # selection / cross-panel highlight band overrides it (Tk parity).
            custom = index.model().sep_color(e.name)
            sep_text = None
            # The band actually filled below - the strike line contrasts
            # against this, not against the default BG_SEP.
            if selected:
                sep_band = self.c_sel
                p.fillRect(r, sep_band)
            elif sep_hl == 2:
                sep_band = self.c_hl_anchor
                p.fillRect(r, sep_band)
            elif sep_hl == 3:
                sep_band = self.c_hl_requires
                p.fillRect(r, sep_band)
            elif sep_hl == -3:
                sep_band = self.c_hl_required_by
                p.fillRect(r, sep_band)
            elif sep_hl == 1:
                sep_band = self.c_hl_higher
                p.fillRect(r, sep_band)
            elif sep_hl == -1:
                sep_band = self.c_hl_lower
                p.fillRect(r, sep_band)
            elif e.name == OVERWRITE_NAME:
                sep_band = self.c_overwrite_bg
                p.fillRect(r, _sep_gradient(sep_band, r))
            elif e.name == ROOT_FOLDER_NAME:
                sep_band = self.c_root_bg
                p.fillRect(r, _sep_gradient(sep_band, r))
            elif custom:
                sep_band = QColor(custom)
                p.fillRect(r, _sep_gradient(sep_band, r))
                sep_text = QColor(_contrasting_text_color(custom))
            else:
                sep_band = self.c_sep_bg
                p.fillRect(r, _sep_gradient(sep_band, r))
            if index.column() == COL_NAME:
                self._paint_separator(p, r, e, index, sep_text, sep_band)
            p.restore()
            return

        # Row background. Selection wins, then cross-panel highlight tint
        # (green/red/orange), then hover, then the zebra base.
        selected = bool(opt.state & QStyle.State_Selected)
        hl = index.data(HighlightRole) or 0
        highlighted = False
        if selected:
            p.fillRect(r, self.c_sel)
        elif hl == 2:
            p.fillRect(r, self.c_hl_anchor); highlighted = True
        elif hl == 3:
            p.fillRect(r, self.c_hl_requires); highlighted = True
        elif hl == -3:
            p.fillRect(r, self.c_hl_required_by); highlighted = True
        elif hl == 1:
            p.fillRect(r, self.c_hl_higher); highlighted = True
        elif hl == -1:
            p.fillRect(r, self.c_hl_lower); highlighted = True
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(r, self.c_hover)
        else:
            p.fillRect(r, self.c_row_alt if index.row() % 2 else self.c_row)

        text_color = (self.c_text_on_sel if (selected or highlighted)
                      else (self.c_text if e.enabled else self.c_text_dim))

        if index.column() == COL_NAME:
            self._paint_name(p, r, e, index, text_color)
        elif index.column() == COL_FLAGS:
            self._paint_icons(p, r, self._flag_icons(index.data(FlagsRole) or 0))
        elif index.column() == COL_CONFLICTS:
            self._paint_conflicts(p, r, index.data(ConflictRole) or 0,
                                  index.data(BsaConflictRole) or 0,
                                  index.data(UuidConflictRole) or 0)
        else:
            # Plain columns (Installed/Version/Priority): centred to match the
            # centred headers + the icon columns.
            val = index.data(Qt.DisplayRole) or ""
            p.setPen(text_color)
            p.setFont(self.f_row)
            pad = QRect(r.left() + 6, r.top(), r.width() - 12, r.height())
            p.drawText(pad, _ALIGN_CENTER, str(val))

        p.restore()

    # Geometry shared by paint + editorEvent so the hit areas match exactly.
    ARROW_SZ = ICON_SZ
    LOCK_SZ = ICON_SZ
    PAD = 10

    def _arrow_rect(self, r):
        y = r.top() + (r.height() - self.ARROW_SZ) // 2
        return QRect(r.left() + self.PAD, y, self.ARROW_SZ, self.ARROW_SZ)

    def _lock_rect(self, r):
        y = r.top() + (r.height() - self.LOCK_SZ) // 2
        return QRect(r.right() - self.PAD - self.LOCK_SZ, y,
                     self.LOCK_SZ, self.LOCK_SZ)

    def _col_rect(self, col, r):
        """Sub-rect of a (full-width, spanned) separator row aligned with a
        given column, so separator content sits under the right header."""
        view = self.parent()
        try:
            x = view.columnViewportPosition(col)
            w = view.columnWidth(col)
            return QRect(x, r.top(), w, r.height())
        except Exception:
            return r

    def _paint_separator(self, p, r, e, index, text_color=None, band=None):
        model = index.model()
        text_color = text_color or self.c_sep_text
        band = band if band is not None else self.c_sep_bg
        # Boundary separators (Overwrite / Root Folder) are pinned + not
        # collapsible/lockable: just a centred name + strikethrough, no controls.
        from gui_qt.modlist_model import (_BOUNDARY_NAMES, ROOT_FOLDER_NAME,
                                          OVERWRITE_NAME)
        if e.name in _BOUNDARY_NAMES:
            p.setFont(self.f_bold)
            cy = r.center().y()
            nr = self._col_rect(COL_NAME, r)
            # These rows own a folder, not a block of mods - the "(N)" counts
            # the files in it (blank until the async walk lands).
            n = (model.boundary_file_count(e.name)
                 if hasattr(model, "boundary_file_count") else None)
            label = e.display_name if n is None else f"{e.display_name}   ({n})"
            tw = p.fontMetrics().horizontalAdvance(label)
            cx = nr.center().x(); gap = tw // 2 + 12
            txt = (self.c_overwrite_text if e.name == OVERWRITE_NAME
                   else self.c_root_text if e.name == ROOT_FOLDER_NAME
                   else self.c_sep_text)
            p.setPen(_sep_rule_pen(txt, band))
            p.drawLine(r.left() + 6, cy, cx - gap, cy)
            p.drawLine(cx + gap, cy, r.right() - 6, cy)
            p.setPen(txt)
            p.drawText(nr, Qt.AlignVCenter | Qt.AlignHCenter, label)
            return

        name = e.display_name
        collapsed = model.is_collapsed(name)
        locked = model.is_sep_locked(name)
        block = model.sep_block_rows(index.row())
        deploy = model.sep_deploy_info(e.name) if hasattr(model, "sep_deploy_info") else {}
        has_badge = bool(deploy.get("path") or deploy.get("raw"))

        name_rect = self._col_rect(COL_NAME, r)
        p.setFont(self.f_bold)
        label = f"{name}   ({len(block)})"
        tw = p.fontMetrics().horizontalAdvance(label)
        cx = name_rect.center().x()
        cy = r.center().y()

        # Collapse arrow - right.png when collapsed, arrow.png when expanded.
        a = self._arrow_rect(r)
        ico = icon("right.png" if collapsed else "arrow.png", self.ARROW_SZ,
                   color=self.c_arrow)
        if not ico.isNull():
            ico.paint(p, a)

        if has_badge:
            # Custom deploy override: left-aligned name after the arrow, with
            # the "⇒ path" badge flowing right of it (Tk parity - no lines).
            tx = a.right() + 8
            p.setPen(text_color)
            p.drawText(QRect(tx, r.top(), tw, r.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, label)
            self._paint_deploy_badge(p, r, deploy, tx + tw + 10, collapsed)
        else:
            # Strikethrough line across the row, broken around the centred name
            # (Tk-style - makes separators easy to distinguish). The left line
            # starts just past the collapse arrow so it doesn't run under it.
            p.setPen(_sep_rule_pen(text_color, band))
            gap = tw // 2 + 12
            left_start = a.right() + 8
            if left_start < cx - gap:
                p.drawLine(left_start, cy, cx - gap, cy)
            p.drawLine(cx + gap, cy, r.right() - 6, cy)

            # Centred name + "(N)" count over the Mod Name column.
            p.setPen(text_color)
            p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignHCenter, label)

        # Grouped flags/conflicts when collapsed - each under its own column.
        # Plus the priority range of the hidden mods in the Priority column
        # (Tk parity - e.g. "0 - 20", dim + centred).
        if collapsed:
            self._paint_grouped_icons(p, r, model, block)
            prio_text = (model.sep_block_priority_range(index.row())
                         if hasattr(model, "sep_block_priority_range") else "")
            if prio_text:
                p.setFont(self.f_row)
                p.setPen(self.c_text_dim)
                # Keep the range clear of the lock box on the far right - clamp
                # the column rect so it never runs under the lock icon.
                pr = self._col_rect(COL_PRIORITY, r)
                lock_left = self._lock_rect(r).left() - 8
                if pr.right() > lock_left:
                    pr.setRight(lock_left)
                p.drawText(pr, Qt.AlignVCenter | Qt.AlignHCenter, prio_text)

        # Lock checkbox on the far right - always drawn so it reads as a
        # clickable control. Empty box when unlocked; the (gold) lock.png on a
        # neutral fill when locked (lock.png is gold, so the fill stays neutral).
        lk = self._lock_rect(r)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check_off))
        p.drawRoundedRect(lk, 3, 3)
        if locked:
            lico = icon("lock.png", self.LOCK_SZ - 2)
            if not lico.isNull():
                lico.paint(p, lk.adjusted(1, 1, -1, -1))

    def _paint_deploy_badge(self, p, r, deploy, x, collapsed):
        """Paint the custom-deploy badge ("⇒ ~/path  [raw]", or "[raw deploy]"
        when only the raw flag is set) starting at *x*, in the link-blue tone.
        Elided to stop before the lock box - and, when the separator is
        collapsed, before the Flags column so the grouped icons stay clear."""
        path = deploy.get("path", "")
        if path:
            try:
                from pathlib import Path
                dp = Path(path)
                home = Path.home()
                if dp.is_relative_to(home):
                    path = "~/" + str(dp.relative_to(home))
            except Exception:
                pass
            text = f"⇒ {path}  [raw]" if deploy.get("raw") else f"⇒ {path}"
        else:
            text = "[raw deploy]"
        right = self._lock_rect(r).left() - 8
        if collapsed:
            right = min(right, self._col_rect(COL_NAME, r).right() - 6)
        if right - x < 16:
            return
        p.setFont(self.f_row)
        elided = p.fontMetrics().elidedText(text, Qt.ElideMiddle, right - x)
        p.setPen(self.c_badge)
        p.drawText(QRect(x, r.top(), right - x, r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_grouped_icons(self, p, r, model, block):
        """Collapsed-separator summary: union of the block's flag icons painted
        in the Flags column, and its conflict icons in the Conflicts column -
        each kept under the relevant header."""
        bits, conflicts, bsa_conflicts, uuid_conflicts = \
            model.sep_block_summary(block)

        def _summarise(codes, icons):
            # Both winners + losers (or any mixed) → one mixed icon, else lone.
            # A fully-redundant (3) member is shown with its own icon if present,
            # else folds into the "loses" bucket.
            if 2 in codes or (1 in codes and -1 in codes):
                return [icons[2]]
            if 3 in codes and 3 in icons:
                return [icons[3]]
            if 1 in codes:
                return [icons[1]]
            if -1 in codes or 3 in codes:
                return [icons[-1]]
            return []

        conflict_icons = (_summarise(conflicts, _CONFLICT_ICONS)
                          + _summarise(bsa_conflicts, _BSA_CONFLICT_ICONS)
                          + _summarise(uuid_conflicts, _UUID_CONFLICT_ICONS))
        self._paint_icons(p, self._col_rect(COL_FLAGS, r), self._flag_icons(bits))
        self._paint_icons(p, self._col_rect(COL_CONFLICTS, r), conflict_icons)

    def _paint_name(self, p, r, e, index, text_color):
        x = r.left()

        # Checkbox (accent fill + white tick when enabled; hollow when not).
        box = QRect(x + 10, r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check if e.enabled else self.c_check_off))
        p.drawRoundedRect(box, 3, 3)
        if e.enabled:
            p.setPen(QPen(self.c_tick, 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        tx = box.right() + 10

        # Lock glyph.
        if e.locked:
            p.setPen(self.c_lock)
            p.drawText(QRect(tx, r.top(), 16, r.height()),
                       Qt.AlignVCenter, "\U0001F512")
            tx += 18

        # Name (elided).
        p.setPen(text_color)
        p.setFont(self.f_row)
        name_rect = QRect(tx, r.top(), r.right() - tx - 6, r.height())
        name = e.display_name
        text_width = self._name_widths.get(name)
        if text_width is None:
            text_width = self.fm_row.horizontalAdvance(name)
            self._name_widths[name] = text_width
        shown = (name if text_width <= name_rect.width()
                 else self.fm_row.elidedText(name, Qt.ElideRight,
                                             name_rect.width()))
        p.drawText(name_rect, _ALIGN_LEFT, shown)

    def _paint_conflicts(self, p, r, loose, bsa, uuid=0):
        """Conflicts cell: loose-file conflict icon on the left, BSA/BA2 archive
        conflict icon next (Tk parity), then the BG3 module-UUID icon. Any may be
        absent; a lone icon is centred, several are laid out around the centre."""
        names = self._conflict_icon_names(loose, bsa, uuid)
        if names:
            self._paint_icons(p, r, names)

    @staticmethod
    def _conflict_icon_names(loose, bsa, uuid):
        """Icon file names for the Conflicts cell, in paint order. Single source
        of truth for painting, hit-testing and tooltips - they must agree."""
        return [n for n in (_CONFLICT_ICONS.get(loose),
                            _BSA_CONFLICT_ICONS.get(bsa),
                            _UUID_CONFLICT_ICONS.get(uuid)) if n]

    @staticmethod
    def _effective_flag_bits(bits):
        """Collapse the mutually-exclusive icon groups: only ONE info.png (pre-RTX
        wins over collection bundled/patched) and only ONE root.png ever paint -
        matching Tk. Returns the active FLAG_ICONS entries after the collapse."""
        # Info group: keep the first present in precedence order, drop the rest.
        info_keep = next((f for f in _INFO_FLAGS if bits & f), 0)
        root_keep = next((f for f in _ROOT_FLAGS if bits & f), 0)
        out = []
        for bit, name in _FLAG_ICONS:
            if bit in _INFO_FLAGS and bit != info_keep:
                continue
            if bit in _ROOT_FLAGS and bit != root_keep:
                continue
            if bits & bit:
                out.append((bit, name))
        return out

    def _flag_icons(self, bits):
        """Icon names for the Flags cell. Mono white glyphs get the theme's
        text colour appended as a "#rrggbb" tint so they stay visible on light
        themes (_paint_icons splits it back off)."""
        return [name + self.c_flag_tint if name in _MONO_FLAG_ICONS else name
                for _bit, name in self._effective_flag_bits(bits)]

    def _hit_flag_bit(self, pos, r, bits):
        """Which FLAG_* bit's icon (if any) is under *pos* within the Flags cell
        rect *r*. Recomputes the same left-to-right centred geometry as
        _paint_icons so a click lands on the icon the user sees."""
        active = self._effective_flag_bits(bits)
        if not active:
            return 0
        sz, gap = ICON_SZ, 3
        total = len(active) * sz + (len(active) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        # Walk the collapsed ACTIVE bits in paint order so the hit maps to the
        # correct flag even when several share an icon (info / root).
        for bit, _name in active:
            if QRect(x, y, sz, sz).contains(pos):
                return bit
            x += sz + gap
        return 0

    def _hit_clickable_flag_bit(self, pos, r, bits):
        hit = self._hit_flag_bit(pos, r, bits)
        return hit if hit in _CLICKABLE_FLAG_BITS else 0

    def _hit_conflict_icon(self, pos, r, index):
        """True if *pos* lands on a conflict icon in the Conflicts cell rect *r*.
        Recomputes the same centred geometry as _paint_conflicts/_paint_icons so
        the hit area matches the painted icons."""
        names = self._conflict_icon_names(index.data(ConflictRole) or 0,
                                          index.data(BsaConflictRole) or 0,
                                          index.data(UuidConflictRole) or 0)
        if not names:
            return False
        sz, gap = ICON_SZ, 3
        total = len(names) * sz + (len(names) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        for _name in names:
            if QRect(x, y, sz, sz).contains(pos):
                return True
            x += sz + gap
        return False

    def _conflict_tip(self, pos, r, index):
        """Tooltip for the hovered conflict icon (Tk parity - the loose-file icon
        is drawn left, the BSA/BA2 icon right; each gets its own text). Returns
        None when *pos* is not over a conflict icon."""
        loose = index.data(ConflictRole) or 0
        bsa = index.data(BsaConflictRole) or 0
        uuid = index.data(UuidConflictRole) or 0
        # Rebuild the same left-to-right layout as _paint_icons, tagging each
        # slot with the text it should show. Full strings kept static (not
        # composed) so lupdate can extract them.
        slots = []
        if _CONFLICT_ICONS.get(loose):
            slots.append(_LOOSE_CONFLICT_TIPS.get(loose))
        if _BSA_CONFLICT_ICONS.get(bsa):
            slots.append(_BSA_CONFLICT_TIPS.get(bsa))
        if _UUID_CONFLICT_ICONS.get(uuid):
            slots.append(_UUID_CONFLICT_TIPS.get(uuid))
        if not slots:
            return None
        sz, gap = ICON_SZ, 3
        total = len(slots) * sz + (len(slots) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        for tip in slots:
            if tip and QRect(x, y, sz, sz).contains(pos):
                return self.tr(tip)
            x += sz + gap
        return None

    def _flag_tip(self, hit, index):
        """Tooltip for the hovered flag *hit*. The Note flag shows the actual
        note text rendered from Markdown (Tk parity + rich text), the
        rerun-FOMOD flag names the plugins that triggered it; everything else
        uses the static _FLAG_TIPS."""
        if hit == FLAG_NOTE:
            try:
                model = index.model()
                name = index.data(EntryRole).name
                note = model.note_for(name) if hasattr(model, "note_for") else ""
                if note:
                    if len(note) > 500:
                        note = note[:500].rstrip() + "…"
                    return _note_to_tooltip_html(note)
            except Exception:
                pass
        if hit == FLAG_RERUN_FOMOD:
            try:
                model = index.model()
                name = index.data(EntryRole).name
                reason = (model.rerun_fomod_reason(name)
                          if hasattr(model, "rerun_fomod_reason") else None)
                if reason and reason[1]:
                    kind, plugins = reason
                    names = ", ".join(plugins[:6]) + \
                        ("…" if len(plugins) > 6 else "")
                    if kind == "active":
                        return self.tr(
                            "A plugin an installed FOMOD option requires left "
                            "the load order ({0}) - click to re-run the FOMOD "
                            "installer").format(names)
                    return self.tr(
                        "A FOMOD option you didn't select is now relevant "
                        "({0} is in the load order) - click to re-run the "
                        "FOMOD installer").format(names)
            except Exception:
                pass
        tip = _FLAG_TIPS.get(hit)
        return self.tr(tip) if tip else None

    def helpEvent(self, event, view, opt, index):
        """Show the per-flag tooltip when hovering a flag icon (Tk parity -
        distinguishes e.g. collection bundled vs patched)."""
        try:
            if event.type() == QEvent.ToolTip and index.isValid():
                if index.column() == COL_FLAGS:
                    bits = index.data(FlagsRole) or 0
                    if bits:
                        hit = self._hit_flag_bit(event.pos(), opt.rect, bits)
                        tip = self._flag_tip(hit, index)
                        if tip:
                            # Pass the flags-cell rect so Qt hides the tooltip as
                            # soon as the cursor leaves the cell (instead of
                            # keeping it up for its full length-based timeout).
                            QToolTip.showText(
                                event.globalPos(), wrap_tooltip(tip), view,
                                opt.rect)
                            return True
                    QToolTip.hideText()
                elif index.column() == COL_CONFLICTS:
                    tip = self._conflict_tip(event.pos(), opt.rect, index)
                    if tip:
                        QToolTip.showText(
                            event.globalPos(), wrap_tooltip(tip), view,
                            opt.rect)
                        return True
                    QToolTip.hideText()
        except Exception:
            pass
        return super().helpEvent(event, view, opt, index)

    def _paint_icons(self, p, r, names):
        """Paint a horizontally-centred row of icons (Flags + Conflicts cells,
        and the collapsed-separator summary) so they line up under the column."""
        if not names:
            return
        sz, gap = ICON_SZ, 3
        total = len(names) * sz + (len(names) - 1) * gap
        x = r.left() + max(6, (r.width() - total) // 2)
        y = r.top() + (r.height() - sz) // 2
        for name in names:
            # A "file.png#rrggbb" name carries a tint colour (e.g. the bundle
            # flag recolours its glyph white to read on any row). Re-attach the
            # "#" - QColor requires it, and a bare "ffffff" is invalid (→ black).
            fname, sep, hexpart = name.partition("#")
            color = ("#" + hexpart) if sep else None
            ic = icon(fname, sz, color)
            if not ic.isNull():
                ic.paint(p, QRect(x, y, sz, sz))
            x += sz + gap
            if x > r.right() - sz:
                break

    def _hit_centered_text(self, pos, rect, index):
        text = str(index.data(Qt.DisplayRole) or "")
        if not text:
            return False
        width = min(self.fm_row.horizontalAdvance(text),
                    max(0, rect.width() - 12))
        hit = QRect(0, 0, width,
                    self.fm_row.height())
        hit.moveCenter(rect.center())
        return hit.contains(pos)

    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        if index.column() not in (COL_NAME, COL_FLAGS, COL_CONFLICTS,
                                  COL_VERSION, COL_PRIORITY):
            return False
        pos = event.position().toPoint()
        e = model.entry(index.row())

        if index.column() == COL_VERSION:
            if (event.button() != Qt.LeftButton or e.is_separator
                    or not self._hit_centered_text(pos, opt.rect, index)):
                return False
            from gui_qt.modlist_menu import _set_version
            _set_version(self.parent(), model, index.row())
            return True

        if index.column() == COL_PRIORITY:
            if (event.button() != Qt.LeftButton or e.is_separator
                    or e.locked
                    or not self._hit_centered_text(pos, opt.rect, index)):
                return False
            from gui_qt.modlist_menu import _set_priority
            _set_priority(self.parent(), model, index.row())
            return True

        # Only flags backed by an action consume the click.
        if index.column() == COL_FLAGS:
            if e.is_separator:
                return False
            bits = model.data(index, FlagsRole) or 0
            hit = self._hit_clickable_flag_bit(pos, opt.rect, bits)
            if hit:
                view = self.parent()
                cb = getattr(view, "on_flag_clicked", None)
                if callable(cb):
                    cb(index.row(), hit)
                    return True
            return False

        # Conflicts cell: a click on a conflict icon opens the Show Conflicts
        # tab for the mod (same tab as the right-click menu item).
        if index.column() == COL_CONFLICTS:
            if e.is_separator:
                return False
            if self._hit_conflict_icon(pos, opt.rect, index):
                view = self.parent()
                cb = getattr(view, "on_show_conflicts", None)
                if callable(cb):
                    cb(e.name)
                    return True
            return False

        if e.is_separator:
            # Real separators are never selectable: the view consumes their
            # press (mousePressEvent) and does the collapse/lock toggle on
            # release (_handle_separator_click), so it never reaches here. The
            # _arrow_rect/_lock_rect geometry still lives on this delegate and
            # is reused by that handler.
            return False

        # Mod row: checkbox area toggles enabled.
        box = QRect(opt.rect.left() + 6, opt.rect.top(), 26, opt.rect.height())
        if box.contains(pos):
            model.toggle(index.row())
            return True
        return False
