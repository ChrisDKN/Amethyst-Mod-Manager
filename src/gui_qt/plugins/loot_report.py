"""LOOT report sub-tab for the plugins panel.

Renders the parts of the cached ``<profile>/loot.json`` that the Plugins tab
has no room for: the masterlist-wide general messages (which otherwise only
flash past in the log while a sort runs) and the per-plugin download
locations the masterlist knows about. Everything shown here comes from the
last sort or metadata refresh - the view never talks to libloot itself, so
it stays cheap to rebuild on every plugin reload.
"""

from __future__ import annotations

import html
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextBrowser

from gui_qt.appearance.theme import (
    active_palette, bind_theme, _c, err_text, ok_text, warn_text,
)

_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(\s*(https?://[^\s)]+)\s*\)", re.IGNORECASE)
_WEB_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1")
_CODE_RE = re.compile(r"`([^`]+)`")
_FILENAME_RE = re.compile(r'^Filename\(["\'](.+?)["\']\)$')


def _valid_web_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _trim_url(url: str) -> str:
    """Remove prose/Markdown punctuation captured after a bare web URL."""
    url = url.rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    changed = True
    while changed and url:
        changed = False
        for close, opening in pairs:
            if url.endswith(close) and url.count(close) > url.count(opening):
                url = url[:-1]
                changed = True
    return url


def _anchor(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def markdown_to_html(text: str) -> str:
    """Render the small Markdown subset LOOT messages use as inline HTML.

    Handles ``[label](url)`` links, bare URLs, ``**bold**`` and ``code``.
    Everything else is escaped verbatim; newlines become ``<br>``.
    """
    pieces: list[str] = []
    pos = 0
    for match in _MARKDOWN_LINK_RE.finditer(text):
        pieces.append(_plain_to_html(text[pos:match.start()]))
        label, url = match.group(1), _trim_url(match.group(2))
        pieces.append(_anchor(url, label) if _valid_web_url(url)
                      else html.escape(match.group(0)))
        pos = match.end()
    pieces.append(_plain_to_html(text[pos:]))
    return "".join(pieces)


def _plain_to_html(text: str) -> str:
    """Escape *text*, linkifying bare URLs and applying bold/code spans."""
    pieces: list[str] = []
    pos = 0
    for match in _WEB_URL_RE.finditer(text):
        pieces.append(_inline_to_html(text[pos:match.start()]))
        url = _trim_url(match.group(0))
        trailing = match.group(0)[len(url):]
        pieces.append(_anchor(url, url) if _valid_web_url(url)
                      else html.escape(url))
        pieces.append(html.escape(trailing))
        pos = match.end()
    pieces.append(_inline_to_html(text[pos:]))
    return "".join(pieces)


def _inline_to_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _EMPHASIS_RE.sub(r"<b>\2</b>", escaped)
    return escaped.replace("\n", "<br>")


class LootReportView(QWidget):
    """The LOOT sub-tab. configure() once, then mark_dirty()/refresh()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.game = None
        self.profile_dir: Path | None = None
        self._dirty = True
        self._is_visible = False
        self._pal = active_palette()
        self._build()
        bind_theme(self)

    # -- construction ------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self._summary = QLabel()
        self._summary.setObjectName("LootReportSummary")
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self._summary)
        self._browser = QTextBrowser()
        self._browser.setObjectName("LootReportBrowser")
        self._browser.setOpenLinks(False)
        self._browser.setOpenExternalLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        v.addWidget(self._browser, 1)

    def refresh_theme(self, palette: dict) -> None:
        self._pal = palette
        self._summary.setStyleSheet(
            f"color:{_c(palette, 'TEXT_DIM')}; padding:2px 4px;")
        self._browser.setStyleSheet(
            f"QTextBrowser {{ background:{_c(palette, 'BG_DEEP')};"
            f" color:{_c(palette, 'TEXT_MAIN')};"
            f" border:1px solid {_c(palette, 'BORDER')}; border-radius:4px;"
            f" padding:6px; }}")
        if not self._dirty:
            self._render()

    # -- context -----------------------------------------------------------
    def configure(self, game, profile_dir):
        self.game = game
        self.profile_dir = profile_dir
        self.mark_dirty()

    def set_visible_tab(self, visible: bool):
        self._is_visible = visible
        if visible and self._dirty:
            self.refresh()

    def mark_dirty(self):
        self._dirty = True
        if self._is_visible:
            self.refresh()

    def refresh(self):
        self._dirty = False
        self._render()

    # -- rendering ---------------------------------------------------------
    def _load(self) -> dict:
        if self.profile_dir is None:
            return {}
        try:
            from LOOT.loot_sorter import read_loot_info
            data = read_loot_info(Path(self.profile_dir))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _render(self):
        data = self._load()
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
        general = data.get("general_messages")
        if not isinstance(general, list):
            general = []

        if not data:
            self._summary.setText(self.tr(
                "LOOT has not run for this profile yet. Use Sort Plugins or "
                "Refresh to generate a report."))
            self._browser.setHtml("")
            return

        generated = data.get("generated_at")
        when = ""
        if isinstance(generated, (int, float)) and generated > 0:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated))
        parts = []
        if when:
            parts.append(self.tr("Last LOOT run: {0}").format(when))
        parts.append(self.tr("{0} plugin(s) with metadata").format(len(plugins)))
        self._summary.setText("  ·  ".join(parts))

        pal = self._pal
        dim = _c(pal, "TEXT_DIM")
        self._severity = {
            "error": (err_text(pal), self.tr("Error")),
            "warn": (warn_text(pal), self.tr("Warning")),
            "say": (dim, self.tr("Note")),
        }

        # First pass: render every plugin and gather what the summary needs,
        # so the summary can link down to the plugin headers.
        # (anchor, owner, text) per message, keyed by severity.
        messages: dict[str, list[tuple[str, str, str]]] = {
            "error": [], "warn": [], "say": []}
        dirty: list[tuple[str, str]] = []           # (anchor, plugin)
        general_label = self.tr("General")
        for message in self._valid_messages(general):
            messages[message["type"]].append(
                ("general", general_label, message["text"]))
        plugin_html: list[str] = []
        for index, (name, info) in enumerate(plugins.items()):
            if not isinstance(info, dict):
                continue
            sections = self._plugin_sections(info)
            if not sections:
                continue
            anchor = f"plugin-{index}"
            plugin_html.append(
                f"<a name='{anchor}'></a><h3>{html.escape(str(name))}</h3>")
            for title, body in sections:
                plugin_html.append(f"<h4>{html.escape(title)}</h4>{body}")
            for message in self._valid_messages(info.get("messages")):
                messages[message["type"]].append((anchor, str(name), message["text"]))
            if any(isinstance(entry, dict) for entry in (info.get("dirty") or [])):
                dirty.append((anchor, str(name)))

        out = [
            f"<style>a {{ color:{_c(pal, 'LINK_BLUE')}; text-decoration:none; }}"
            f" h2 {{ color:{_c(pal, 'ACCENT')}; font-size:12pt; margin:10px 0 4px 0; }}"
            f" h3 {{ color:{_c(pal, 'TEXT_MAIN')}; font-size:11pt; margin:14px 0 2px 0;"
            f" border-bottom:1px solid {_c(pal, 'BORDER')}; }}"
            f" h4 {{ color:{dim}; font-size:9pt; font-weight:bold;"
            f" margin:6px 0 2px 0; }}"
            f" code {{ background:{_c(pal, 'BG_PANEL')}; }}"
            f" td {{ padding:1px 6px; vertical-align:top; }}"
            f" ul {{ margin:0 0 0 14px; }}</style>",
            f"<a name='top'></a><h2>{html.escape(self.tr('Summary'))}</h2>",
            self._summary_html(messages, dirty),
            f"<a name='general'></a>"
            f"<h2>{html.escape(self.tr('General messages'))}</h2>",
        ]
        general_html = self._messages_html(general)
        if general_html:
            out.append(general_html)
        else:
            out.append(self._empty_note(
                self.tr("The masterlist reported no general messages.")))

        out.append(f"<h2>{html.escape(self.tr('Plugins'))}</h2>")
        if plugin_html:
            out.extend(plugin_html)
        else:
            out.append(self._empty_note(
                self.tr("The masterlist has no metadata for the loaded plugins.")))
        self._browser.setHtml("".join(out))
        self._browser.verticalScrollBar().setValue(0)

    def _summary_html(self, messages, dirty) -> str:
        """Counts plus one linked line per message and dirty plugin.

        Errors and warnings come first; dirty plugins and LOOT's plain notes
        follow, since the notes often carry useful hints (patches, settings,
        known issues) that would otherwise be buried in the plugin list.
        """
        pal = self._pal
        dim = _c(pal, "TEXT_DIM")
        errors, warnings, notes = messages["error"], messages["warn"], messages["say"]
        if not (errors or warnings or dirty or notes):
            return (f"<p style='color:{ok_text(pal)};'>"
                    f"{html.escape(self.tr('No messages or dirty plugins reported.'))}"
                    "</p>")
        counts = []
        if errors:
            counts.append(f"<span style='color:{err_text(pal)};'><b>"
                          f"{html.escape(self.tr('{0} error(s)').format(len(errors)))}</b></span>")
        if warnings:
            counts.append(f"<span style='color:{warn_text(pal)};'><b>"
                          f"{html.escape(self.tr('{0} warning(s)').format(len(warnings)))}</b></span>")
        if dirty:
            counts.append(f"<b>{html.escape(self.tr('{0} dirty plugin(s)').format(len(dirty)))}</b>")
        if notes:
            counts.append(f"<span style='color:{dim};'><b>"
                          f"{html.escape(self.tr('{0} note(s)').format(len(notes)))}</b></span>")
        if not (errors or warnings or dirty):
            counts.insert(0, f"<span style='color:{ok_text(pal)};'>"
                          f"{html.escape(self.tr('No errors, warnings or dirty plugins.'))}</span>")
        out = ["<p>" + " · ".join(counts) + "</p>"]

        def listing(title, colour, rows):
            if not rows:
                return
            out.append(f"<h4 style='color:{colour};'>{html.escape(title)}</h4><ul>")
            for anchor, owner, text in rows:
                line = f"<a href='#{anchor}'>{html.escape(owner)}</a>"
                if text:
                    line += f" <span style='color:{dim};'>–</span> {markdown_to_html(text)}"
                out.append(f"<li>{line}</li>")
            out.append("</ul>")

        listing(self.tr("Errors"), err_text(pal), errors)
        listing(self.tr("Warnings"), warn_text(pal), warnings)
        listing(self.tr("Dirty plugins"), _c(pal, "TEXT_MAIN"),
                [(anchor, name, "") for anchor, name in dirty])
        listing(self.tr("Notes"), dim, notes)
        return "".join(out)

    def _valid_messages(self, messages) -> list[dict]:
        out = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            kind = message.get("type")
            out.append({"type": kind if kind in self._severity else "say",
                        "text": text})
        return out

    def _empty_note(self, text: str) -> str:
        return (f"<p style='color:{_c(self._pal, 'TEXT_DIM')};'>"
                f"{html.escape(text)}</p>")

    def _messages_html(self, messages) -> str:
        rows = []
        for message in self._valid_messages(messages):
            colour, label = self._severity[message["type"]]
            rows.append(
                f"<tr><td style='color:{colour}; white-space:nowrap;'>"
                f"<b>{html.escape(label)}</b></td>"
                f"<td>{markdown_to_html(message['text'])}</td></tr>")
        return f"<table cellspacing='0'>{''.join(rows)}</table>" if rows else ""

    def _plugin_sections(self, info: dict) -> list[tuple[str, str]]:
        """One ``(subheader, html)`` pair per non-empty loot.json field."""
        sections = []
        messages = self._messages_html(info.get("messages"))
        if messages:
            sections.append((self.tr("Messages"), messages))
        requirements = self._file_list_html(info.get("requirements"))
        if requirements:
            sections.append((self.tr("Requirements"), requirements))
        incompatibilities = self._file_list_html(info.get("incompatibilities"))
        if incompatibilities:
            sections.append((self.tr("Incompatibilities"), incompatibilities))
        locations = self._locations_html(info.get("locations"))
        if locations:
            sections.append((self.tr("Locations"), locations))
        dirty = self._cleaning_html(info.get("dirty"))
        if dirty:
            sections.append((self.tr("Dirty edits"), dirty))
        clean = self._cleaning_html(info.get("clean"))
        if clean:
            sections.append((self.tr("Verified clean"), clean))
        tags = self._tags_html(info.get("tags"))
        if tags:
            sections.append((self.tr("Bash Tags"), tags))
        return sections

    @staticmethod
    def _file_label(raw: str) -> str:
        match = _FILENAME_RE.match(raw)
        return match.group(1) if match else raw

    def _file_list_html(self, files) -> str:
        items = []
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            name = self._file_label(str(entry.get("name") or ""))
            display = self._file_label(str(entry.get("display_name") or "")) or name
            if not display:
                continue
            line = markdown_to_html(display)
            if name and display != name:
                line += f" <span style='color:{_c(self._pal, 'TEXT_DIM')};'>({html.escape(name)})</span>"
            detail = entry.get("detail")
            if isinstance(detail, str) and detail.strip():
                line += f"<br>{markdown_to_html(detail)}"
            items.append(f"<li>{line}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""

    def _locations_html(self, locations) -> str:
        links = []
        seen: set[str] = set()
        for location in locations or []:
            if not isinstance(location, dict):
                continue
            url = _trim_url(str(location.get("url") or "").strip())
            if not _valid_web_url(url) or url in seen:
                continue
            seen.add(url)
            label = str(location.get("name") or "").strip()
            if not label or label == url:
                # The masterlist left the location unnamed (the writer stores
                # the URL as the name): show it without the scheme.
                label = url.split("://", 1)[-1].rstrip("/")
            links.append(f"<li>{_anchor(url, label)}</li>")
        return f"<ul>{''.join(links)}</ul>" if links else ""

    def _cleaning_html(self, entries) -> str:
        items = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            counts = []
            if entry.get("itm"):
                counts.append(self.tr("{0} ITM").format(entry["itm"]))
            if entry.get("udr"):
                counts.append(self.tr("{0} UDR").format(entry["udr"]))
            if entry.get("nav"):
                counts.append(self.tr("{0} deleted navmesh").format(entry["nav"]))
            line = html.escape(", ".join(counts)) if counts else ""
            crc = entry.get("crc")
            if isinstance(crc, int) and crc:
                crc_text = html.escape(self.tr("CRC {0}").format(f"{crc:08X}"))
                line = f"{line} · {crc_text}" if line else crc_text
            utility = entry.get("utility")
            if isinstance(utility, str) and utility.strip():
                tool = markdown_to_html(utility)
                line += " · " + html.escape(self.tr("clean with")) + f" {tool}"
            detail = entry.get("detail")
            if isinstance(detail, str) and detail.strip():
                line += f"<br>{markdown_to_html(detail)}"
            if line:
                items.append(f"<li>{line}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""

    def _tags_html(self, tags) -> str:
        if not isinstance(tags, dict):
            return ""
        rows = []
        for key, label, prefix in (("current", self.tr("Current"), ""),
                                   ("add", self.tr("Suggested (add)"), "+"),
                                   ("remove", self.tr("Suggested (remove)"), "-")):
            values = [str(v) for v in (tags.get(key) or []) if str(v)]
            if values:
                rows.append(
                    f"<tr><td style='color:{_c(self._pal, 'TEXT_DIM')};"
                    f" white-space:nowrap;'>{html.escape(label)}</td>"
                    f"<td>{html.escape(', '.join(prefix + v for v in values))}</td></tr>")
        return f"<table cellspacing='0'>{''.join(rows)}</table>" if rows else ""

    # -- links -------------------------------------------------------------
    def _on_anchor_clicked(self, url: QUrl):
        target = url.toString()
        if target.startswith("#"):
            # Summary entries jump to the plugin's own header.
            self._browser.scrollToAnchor(url.fragment())
            return
        if not _valid_web_url(target):
            return
        from Utils.environment.xdg import open_url
        open_url(target)
