from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth
from Utils.loverslab import is_loverslab_url
from .http import DownloadUnavailable, download_http
from .paths import WabbajackError
from .diagnostics import emit, url_host

_AUTOMATIC = {"Http", "HTTP", "WabbajackCDN", "GoogleDrive", "Mega", "MediaFire", "ModDB"}
_DRIVE_HOSTS = {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}


def source_url(archive):
    state = archive.state
    for key in ("Url", "URL", "FullURL", "IPS4Url"):
        if state.get(key):
            return str(state[key])
    if archive.kind == "GoogleDrive" and state.get("Id"):
        return "https://drive.google.com/file/d/" + str(state["Id"]) + "/view"
    return ""


def automatic_source(archive, premium=False, *, loverslab_available=False):
    if is_loverslab_url(source_url(archive)):
        return loverslab_available
    if archive.kind == "Mega":
        from .mega import _file_link
        try:
            _file_link(source_url(archive))
        except WabbajackError:
            return False
    if archive.kind == "GoogleDrive":
        try:
            _drive_id(archive)
        except WabbajackError:
            return False
    return archive.kind in _AUTOMATIC or (archive.kind == "Nexus" and premium)


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")


def _get(session, url, headers, stop, log=None, label="host"):
    _check_stop(stop)
    if urlparse(url).scheme not in {"http", "https"}:
        raise DownloadUnavailable("The host did not provide an HTTP download link. Use the download page or Select File.")
    emit(log, "host.request", resolver=label, host=url_host(url),
         header_names=sorted(headers))
    response = session.get(url, headers=headers, stream=True, timeout=(20, 60),
                           verify=resolve_ca_bundle() or True)
    emit(log, "host.response", resolver=label, status=response.status_code,
         final_host=url_host(getattr(response, "url", url)),
         content_type=response.headers.get("Content-Type", ""))
    return response


def _html(response):
    return response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}


def _page(response, stop):
    body = bytearray()
    for chunk in response.iter_content(64 * 1024):
        _check_stop(stop)
        bandwidth.throttle(len(chunk), stop)
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise DownloadUnavailable("The host returned an unexpected download page. Open it in your browser to download the file.")
    page = _DownloadPage()
    page.feed(body.decode(response.encoding or "utf-8", errors="replace"))
    return page


class _DownloadPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self.form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.form = (attrs, {})
            self.forms.append(self.form)
        elif tag == "input" and self.form is not None and attrs.get("name"):
            self.form[1][attrs["name"]] = attrs.get("value", "")
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None


def _drive_id(archive):
    value = str(archive.state.get("Id") or "")
    if not value:
        url = urlparse(source_url(archive))
        if url.hostname not in _DRIVE_HOSTS:
            raise DownloadUnavailable("The Google Drive file link is missing. Use Select File for the required archive.")
        match = re.search(r"/file/d/([\w-]+)", url.path)
        value = match[1] if match else parse_qs(url.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise DownloadUnavailable("The Google Drive file ID is invalid. Obtain the exact archive from the author and use Select File.")
    return value


def _drive_response(session, archive, headers, stop, log=None):
    file_id = _drive_id(archive)
    query = {"id": file_id, "export": "download", "confirm": "t"}
    resource = parse_qs(urlparse(source_url(archive)).query).get("resourcekey")
    if resource:
        query["resourcekey"] = resource[0]
    url = "https://drive.usercontent.google.com/download?" + urlencode(query)
    visited = set()
    for attempt in range(4):
        if url in visited:
            break
        visited.add(url)
        response = _get(session, url, headers, stop, log, "google-drive")
        if not _html(response):
            emit(log, "host.google_drive.direct", archive=archive.name,
                 attempt=attempt + 1)
            return response
        with response:
            response.raise_for_status()
            page = _page(response, stop)
            base = response.url
        confirmation = ""
        for attrs, fields in page.forms:
            action = urlparse(urljoin(base, attrs.get("action") or base))
            if (action.scheme == "https" and action.hostname in _DRIVE_HOSTS
                    and action.path in {"/download", "/uc"} and attrs.get("method", "get").lower() == "get"
                    and (fields.get("id") == file_id or "download" in attrs.get("id", "").lower())):
                params = {**dict((k, v[-1]) for k, v in parse_qs(action.query).items()), **query, **fields}
                if params.get("id") != file_id:
                    continue
                confirmation = urlunparse(action._replace(query=urlencode(params)))
                break
        if not confirmation:
            warning = next((cookie.value for cookie in session.cookies if cookie.name.startswith("download_warning_")), "")
            if warning:
                confirmation = "https://drive.google.com/uc?" + urlencode({**query, "confirm": warning})
        if not confirmation:
            break
        emit(log, "host.google_drive.confirmation", archive=archive.name,
             attempt=attempt + 1, next_host=url_host(confirmation))
        url = confirmation
    raise DownloadUnavailable("Google Drive requires browser access or has reached a download limit. Open the page, sign in if requested, and download the exact file. If a quota message appears, wait for it to reset and resume.")


def _web_response(session, archive, headers, stop, log=None):
    url = source_url(archive)
    kind = archive.kind
    expected_host = "mediafire.com" if kind == "MediaFire" else "moddb.com"
    host = urlparse(url).hostname or ""
    if host != expected_host and not host.endswith("." + expected_host):
        raise DownloadUnavailable(f"The {kind} source link is invalid. Use the author's download page or Select File.")
    visited = set()
    for attempt in range(4):
        if url in visited:
            break
        visited.add(url)
        response = _get(session, url, headers, stop, log, kind.casefold())
        if not _html(response):
            emit(log, "host.web.direct", archive=archive.name, kind=kind,
                 attempt=attempt + 1)
            return response
        with response:
            response.raise_for_status()
            page = _page(response, stop)
            base = response.url
        candidates = []
        for attrs in page.links:
            link = urljoin(base, attrs["href"])
            parsed = urlparse(link)
            hostname = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or (hostname != expected_host and not hostname.endswith("." + expected_host)):
                continue
            if kind == "MediaFire":
                match = attrs.get("id") == "downloadButton" or attrs.get("aria-label") == "Download file" or hostname.startswith("download")
            else:
                match = attrs.get("id") == "downloadon" or parsed.path.startswith("/downloads/mirror/")
            if match and link not in visited:
                candidates.append(link)
        if not candidates:
            break
        emit(log, "host.web.link_found", archive=archive.name, kind=kind,
             attempt=attempt + 1, candidates=len(candidates),
             next_host=url_host(candidates[0]))
        url = candidates[0]
    raise DownloadUnavailable(f"{kind} did not provide a direct download. Open the page to complete any sign-in or browser checks, then download the exact file.")


def download_host(archive, target, *, stop=None, progress=None, log=None):
    emit(log, "host.download.started", archive=archive.name, kind=archive.kind,
         source_host=url_host(source_url(archive)), target=target)
    if archive.kind == "Mega":
        from .mega import download_mega
        return download_mega(source_url(archive), target, size=archive.size, expected=archive.key,
                             stop=stop, progress=progress, log=log)
    with requests.Session() as session:
        resolver = _drive_response if archive.kind == "GoogleDrive" else _web_response
        return download_http(source_url(archive), target, size=archive.size, expected=archive.key,
                             stop=stop, progress=progress,
                             open_response=lambda headers: resolver(session, archive, headers, stop, log),
                             log=log)
