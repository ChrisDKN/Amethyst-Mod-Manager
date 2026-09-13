from __future__ import annotations

import json
import re
import threading
import unicodedata
from contextlib import contextmanager
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth
from Utils.wabbajack.diagnostics import emit, url_host
from Utils.wabbajack.http import DownloadUnavailable, download_http
from . import is_loverslab_url

_BASE = "https://www.loverslab.com"
_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _file_id(url):
    match = re.search(r"/files/file/(\d+)", url)
    return match[1] if match else ""


def _resource_id(url):
    match = re.search(r"[?&]r=(\d+)(?=&|#|$)", unescape(url))
    return match[1] if match else ""


def _https(url):
    parsed = urlparse(url)
    return urlunparse(parsed._replace(scheme="https", netloc=parsed.hostname or ""))


def download_url(url, resource=""):
    url = unescape(url).strip()
    if not is_loverslab_url(url):
        raise DownloadUnavailable("The LoversLab source URL is invalid. Use Select File for the required archive.")
    parsed = urlparse(_https(url))
    if not _file_id(parsed.path):
        return urlunparse(parsed._replace(fragment=""))
    resource = resource or _resource_id(url)
    if parsed.query and not re.match(r"(?:do|r|confirm|t|csrfKey)=", parsed.query):
        parsed = parsed._replace(path=f"/files/file/{_file_id(parsed.path)}/")
    query = {"do": "download"}
    if resource:
        query["r"] = resource
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def _name(value, *, strip_duplicate=False):
    value = unicodedata.normalize("NFKC", unquote(unescape(value))).casefold().strip()
    if strip_duplicate:
        value = re.sub(r" \(\d+\)(?=\.[^.]+$|$)", "", value)
    return " ".join(value.split())


def _join(base, url):
    try:
        return urljoin(base, url)
    except ValueError:
        return ""


def _select(entries, expected, resource=""):
    entries = list(dict.fromkeys(entries))
    if resource:
        matches = [entry for entry in entries if _resource_id(entry[1]) == resource]
    else:
        matches = [entry for entry in entries if _name(entry[0]) == _name(expected)]
        if not matches:
            matches = [entry for entry in entries if _name(entry[0]) == _name(expected, strip_duplicate=True)]
        if not matches and len(entries) == 1:
            matches = entries
    urls = list(dict.fromkeys(url for _, url in matches))
    if len(urls) == 1:
        return urls[0]
    raise DownloadUnavailable("The exact LoversLab file could not be selected. Download the required archive from the page and use Select File.")


class _Element:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def elements(self, tag=None):
        pending = list(reversed(self.children))
        while pending:
            node = pending.pop()
            if isinstance(node, _Element):
                if tag is None or node.tag == tag:
                    yield node
                pending.extend(reversed(node.children))

    def text(self):
        pieces, pending = [], list(reversed(self.children))
        while pending:
            node = pending.pop()
            if isinstance(node, str):
                pieces.append(node)
            else:
                pending.extend(reversed(node.children))
        return "".join(pieces).strip()

    def has_class(self, name):
        return name in (self.attrs.get("class") or "").split()


class _Page(HTMLParser):
    def __init__(self, html, base):
        super().__init__(convert_charrefs=True)
        self.root = _Element("")
        self.stack = [self.root]
        self.base, self.html = base, html
        self.feed(html)
        self.links = [(node, _join(base, node.attrs["href"]))
                      for node in self.root.elements("a") if node.attrs.get("href")]
        self.links = [(node, url) for node, url in self.links if url]
        self.inputs = {node.attrs.get("name"): node.attrs.get("value") or ""
                       for node in self.root.elements("input")}
        self.title = next((node.text() for node in self.root.elements("title")), "")
        self.csrf = self.inputs.get("csrfKey", "")
        if not self.csrf:
            match = re.search(r'''["']?csrfKey["']?\s*[:=]\s*["']([^"']+)["']''', html)
            self.csrf = match[1] if match else ""

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            if len(self.stack) >= 256:
                raise DownloadUnavailable("LoversLab returned an unsupported page. Use a browser to download this file.")
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    @property
    def authenticated(self):
        return any(is_loverslab_url(url) and (
            urlparse(url).path.rstrip("/").endswith("/logout")
            or parse_qs(urlparse(url).query).get("do") == ["logout"])
                   for _, url in self.links)

    @property
    def login_required(self):
        return not self.authenticated and ("auth" in self.inputs and "password" in self.inputs
            or "elSignIn_submit" in self.html or "you must be logged in" in self.html.casefold())

    @property
    def browser_required(self):
        return ("just a moment" in self.title.casefold() or "cf-chl-" in self.html
                or "g-recaptcha-response" in self.inputs or "h-captcha-response" in self.inputs)

    def entries(self):
        entries = []
        for item in self.root.elements("li"):
            if not item.has_class("ipsDataItem"):
                continue
            title = next((node for node in item.elements("h4") if node.has_class("ipsDataItem_title")), None)
            if title is None:
                continue
            name = next((node.text() for node in title.elements("span")), title.text())
            for link in item.elements("a"):
                if link.attrs.get("data-action") == "download" and link.attrs.get("href"):
                    url = _join(self.base, link.attrs["href"])
                    if url:
                        entries.append((name, url))
        if entries:
            return entries
        for node, url in self.links:
            if node.has_class("ipsAttachLink") and "attachment.php" in urlparse(url).path:
                entries.append((node.text(), url))
            elif node.attrs.get("data-action") == "download" and (
                    _resource_id(url) or parse_qs(urlparse(url).query).get("confirm") == ["1"]):
                entries.append((node.text(), url))
        return entries

    def next_page(self, source, resource):
        candidates = [url for node, url in self.links
                      if parse_qs(urlparse(url).query).get("do") == ["download"]
                      and ("download" in node.text().casefold() or node.attrs.get("data-action") == "download")]
        for node in self.root.elements():
            if node.tag == "link" and node.attrs.get("rel") == "canonical":
                candidates.append(_join(self.base, node.attrs.get("href") or ""))
            elif node.tag == "meta" and node.attrs.get("property") == "og:url":
                candidates.append(_join(self.base, node.attrs.get("content") or ""))
        for match in re.finditer(r'''"(?:downloadUrl|content_url)"\s*:\s*("(?:\\.|[^"\\])*")''', self.html):
            try:
                candidates.append(json.loads(match[1]))
            except ValueError:
                pass
        file_id = _file_id(source)
        if file_id:
            title = self.title.rsplit(" - LoversLab", 1)[0].rsplit(" - ", 1)[0]
            slug = re.sub(r'''[\s/\\()\[\]{}<>"'`:;,.!?&|_+=*#@^~]+''', "-", title.casefold()).strip("-")
            if slug:
                candidates.append(f"{_BASE}/files/file/{file_id}-{slug}/")
        for url in candidates:
            if is_loverslab_url(url) and _file_id(url) == file_id and file_id:
                normalized = download_url(url, resource)
                if normalized != source:
                    return normalized
        return ""

    def mega_links(self):
        entries = [(node.text(), url) for node, url in self.links if _is_mega(url)]
        if not entries:
            for url in re.findall(r'''https://mega\.(?:nz|co\.nz)/[^\s"'<>]+''', self.html):
                entries.append(("", unescape(url)))
        return entries


def _is_mega(url):
    try:
        return urlparse(url).hostname in {"mega.nz", "www.mega.nz", "mega.co.nz"}
    except ValueError:
        return False


class _MegaRedirect(Exception):
    def __init__(self, url):
        super().__init__("LoversLab linked to Mega.")
        self.url = url


class _SessionExpired(Exception):
    pass


class LoversLabClient:
    def __init__(self, credentials, *, stop=None, log=None):
        self._credentials = credentials
        self.stop = stop if stop is not None else threading.Event()
        self.log = log
        self._session = None
        self._public_session = None
        self._authenticated = False
        self._login_error = ""
        self._renewed = False
        self._lock = threading.Lock()

    def _check_stop(self):
        if self.stop.is_set():
            raise InterruptedError("LoversLab operation stopped")

    @contextmanager
    def _operation(self):
        while not self._lock.acquire(timeout=0.2):
            self._check_stop()
        try:
            self._check_stop()
            yield
        finally:
            self._lock.release()

    def _get_session(self, authenticated):
        attr = "_session" if authenticated else "_public_session"
        session = getattr(self, attr)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": _AGENT})
            session.verify = resolve_ca_bundle() or True
            setattr(self, attr, session)
        return session

    def _request(self, url, *, headers=None, data=None, login=False):
        method = "POST" if data is not None else "GET"
        for _ in range(10):
            self._check_stop()
            try:
                parsed = urlparse(url)
            except ValueError:
                raise DownloadUnavailable("LoversLab supplied an invalid download link. Use Select File.") from None
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise DownloadUnavailable("LoversLab supplied an unsupported download link. Use Select File.")
            loverslab = is_loverslab_url(url)
            if login and not loverslab:
                raise DownloadUnavailable("LoversLab login requires browser interaction.")
            if loverslab:
                url = _https(url)
            elif data is not None:
                raise DownloadUnavailable("LoversLab login redirected outside the site. Use a browser to sign in.")
            if _is_mega(url):
                raise _MegaRedirect(url)
            session = self._get_session(loverslab)
            try:
                response = session.request(method, url, data=data, headers=headers,
                                           stream=True, allow_redirects=False, timeout=(20, 60))
            except requests.RequestException:
                raise DownloadUnavailable("Could not reach LoversLab or its download server. Check your connection or download the file manually.") from None
            emit(self.log, "loverslab.response", host=url_host(url), status=response.status_code)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                break
            url = urljoin(url, location)
            if response.status_code in {301, 302, 303}:
                method, data = "GET", None
        raise DownloadUnavailable("LoversLab returned too many redirects. Open the download page in a browser.")

    def _read_page(self, response):
        body = bytearray()
        try:
            for chunk in response.iter_content(64 * 1024):
                self._check_stop()
                bandwidth.throttle(len(chunk), self.stop)
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise DownloadUnavailable("LoversLab returned an unexpected page. Download the file in a browser.")
        except requests.RequestException:
            raise DownloadUnavailable("Could not read the LoversLab page. Retry or download the file in a browser.") from None
        try:
            return _Page(body.decode(response.encoding or "utf-8", errors="replace"), response.url)
        except (ValueError, LookupError):
            raise DownloadUnavailable("LoversLab returned an unsupported page. Download the file in a browser.") from None

    def _login(self):
        self._check_stop()
        if self._login_error:
            raise DownloadUnavailable(self._login_error)
        if self._authenticated:
            return
        try:
            with self._request(_BASE + "/login/", login=True) as response:
                page = self._read_page(response)
                if response.status_code != 200 or page.browser_required or not page.csrf:
                    raise DownloadUnavailable("LoversLab login requires browser interaction. Complete any site checks and download manually.")
            fields = {"csrfKey": page.csrf, "auth": self._credentials.email,
                      "password": self._credentials.password, "remember_me": "1",
                      "_processLogin": "usernamepassword"}
            with self._request(_BASE + "/login/", data=fields, login=True) as response:
                page = self._read_page(response)
                lower = page.html.casefold()
                if "your account has been locked" in lower or "you have been banned" in lower:
                    raise DownloadUnavailable("The LoversLab account is locked or banned. Check the account in a browser.")
                if response.status_code != 200 or page.browser_required or not page.authenticated:
                    raise DownloadUnavailable("LoversLab could not verify the login. Check your email and password, or complete any security checks in a browser.")
            self._authenticated = True
            emit(self.log, "loverslab.login.completed")
        except DownloadUnavailable as exc:
            self._login_error = str(exc)
            self._authenticated = False
            emit(self.log, "loverslab.login.failed")
            raise

    def login(self):
        with self._operation():
            self._login()

    def _resolve(self, source, expected_name, headers):
        resource = _resource_id(source)
        url = download_url(source)
        visited = set()
        for _ in range(8):
            self._check_stop()
            if url in visited:
                break
            visited.add(url)
            response = self._request(url, headers=headers)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                if response.status_code in {200, 206, 416}:
                    return response
                status = response.status_code
                response.close()
                if status in {401, 403} and is_loverslab_url(url):
                    raise _SessionExpired
                raise DownloadUnavailable("The LoversLab file is unavailable or the server is limiting downloads. Open the page in a browser.")
            with response:
                page = self._read_page(response)
                if page.browser_required:
                    raise DownloadUnavailable("LoversLab requires a browser security check. Download the required file manually.")
                if page.login_required or urlparse(response.url).path.rstrip("/") == "/login":
                    raise _SessionExpired
                if response.status_code != 200:
                    raise DownloadUnavailable("LoversLab denied the download or has reached a download limit. Open the page in a browser.")
            entries = page.entries()
            if entries:
                url = _select(entries, expected_name, resource)
                if is_loverslab_url(url) and page.csrf and _resource_id(url):
                    parsed = urlparse(url)
                    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                    query["csrfKey"] = page.csrf
                    url = urlunparse(parsed._replace(query=urlencode(query)))
                continue
            mega = page.mega_links()
            if mega:
                raise _MegaRedirect(_select(mega, expected_name))
            next_page = page.next_page(url, resource)
            if next_page and next_page not in visited:
                url = next_page
                continue
            if resource and page.csrf and _file_id(url):
                parsed = urlparse(url)
                query = {"do": "download", "r": resource, "confirm": "1", "t": "1", "csrfKey": page.csrf}
                url = urlunparse(parsed._replace(query=urlencode(query)))
                continue
            break
        raise DownloadUnavailable("LoversLab did not provide the required file. Open the page, download the exact archive, and use Select File.")

    def download(self, archive, target, *, progress=None):
        from Utils.wabbajack.hosts import source_url
        from Utils.wabbajack.mega import download_mega
        with self._operation():
            self._login()
            source = source_url(archive)

            def open_response(headers):
                try:
                    return self._resolve(source, archive.name, headers)
                except _SessionExpired:
                    self._authenticated = False
                    if self._renewed:
                        self._login_error = "The LoversLab session expired again. Sign in in a browser and download manually."
                        raise DownloadUnavailable(self._login_error) from None
                    self._renewed = True
                    self._login()
                    try:
                        return self._resolve(source, archive.name, headers)
                    except _SessionExpired:
                        self._authenticated = False
                        self._login_error = "LoversLab still requires a browser login. Download the required file manually."
                        raise DownloadUnavailable(self._login_error) from None

            try:
                return download_http(source, target, size=archive.size, expected=archive.key,
                                     stop=self.stop, progress=progress, open_response=open_response, log=self.log)
            except _MegaRedirect as redirect:
                return download_mega(redirect.url, target, size=archive.size, expected=archive.key,
                                     stop=self.stop, progress=progress, log=self.log)

    def close(self):
        for session in (self._session, self._public_session):
            if session is not None:
                session.close()
        self._session = self._public_session = None
        self._authenticated = False
