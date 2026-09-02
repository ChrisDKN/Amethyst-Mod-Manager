"""Public paste transport for sharing text and fetching it back.

Used by two features: the profile share code (a code for a big modlist is too
long to paste) and the log panel (a log is far too long to paste into
a bug report). Both hand the user a short URL instead.

paste.c-net.org is the host: no API key (nothing to ship in the binary or leak),
a plain POST-the-body API, and a 50 MB ceiling that a multi-megabyte log fits
inside comfortably.

NOTE on host choice - two were rejected after live testing, don't "fix" this
back to either:

* dpaste.ORG's documented ``/api/`` endpoint answers 405 to a POST (stale docs).
* dpaste.COM works, but its ToS caps storage at 10 MB PER IP and blocks
  offending IPs for 2 days. A share code is ~4 KB, but a log is 35 KB - 1.5 MB,
  so a user filing a few bug reports could exhaust the quota and be blocked
  from the host entirely. Testing this tripped exactly that block.

paste.c-net.org: 50 MB per file, 180-day expiry that RESETS on each access
(so a linked log stays alive while anyone is still reading it), and no
documented rate limit. Verified by posting and re-fetching a 1.5 MB payload
byte-for-byte. Re-check with a live POST before switching hosts.

Every upload here is user-initiated from a button, never automatic: it publishes
whatever is uploaded to a third-party server where anyone with the link can read
it. Callers are responsible for telling the user that before they click.
"""

from __future__ import annotations

import re

PASTE_HOST = "paste.c-net.org"
_PASTE_API = f"https://{PASTE_HOST}/"

_USER_AGENT = "Amethyst-Mod-Manager"

#: How long an upload survives, for UI copy. The host expires files after 180
#: days and resets that clock on every access - there is no per-upload expiry
#: to choose, so the UI states the policy instead of offering a dropdown.
RETENTION_NOTE = "180 days after it was last opened"

#: Matches a URL we are willing to fetch from: any http(s) host, since a user
#: may well re-host a code themselves. Fetches are size-capped and the body
#: still has to parse downstream, so a wrong link fails harmlessly.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

#: Ceiling on a fetched body. Anything larger is not something we posted.
_MAX_FETCH_BYTES = 8 * 1024 * 1024

#: Well under the host's 50 MB limit - a log past this is pathological, and
#: trimming keeps the upload quick on a slow connection. Content past it keeps
#: its TAIL (see upload_text).
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def is_url(text: str) -> bool:
    """True when *text* looks like a link rather than literal content."""
    return bool(_URL_RE.match((text or "").strip()))


def _ssl_context():
    """Reuse the app's resolved CA bundle - the packaged builds need it."""
    try:
        from Utils.github.cache import _get_ssl_context
        return _get_ssl_context()
    except Exception:
        return None


def upload_text(text: str, *, timeout: float = 60.0) -> str:
    """Upload *text* to the paste host and return the resulting URL.

    Content past :data:`MAX_UPLOAD_BYTES` keeps its TAIL - for a log the newest
    lines are the ones worth reading. Raises ``RuntimeError`` with a user-facing
    message on failure; callers fall back to whatever they already had locally.
    """
    import urllib.error
    import urllib.request

    if not (text or "").strip():
        raise RuntimeError("Nothing to upload.")

    payload = text
    encoded = payload.encode("utf-8", "replace")
    if len(encoded) > MAX_UPLOAD_BYTES:
        # Cut on a character boundary, then on a line boundary so the paste
        # doesn't open mid-line, and say what was dropped.
        clipped = encoded[-MAX_UPLOAD_BYTES:].decode("utf-8", "ignore")
        nl = clipped.find("\n")
        if nl != -1:
            clipped = clipped[nl + 1:]
        dropped = len(encoded) - len(clipped.encode("utf-8", "replace"))
        payload = (f"[... {dropped} earlier bytes omitted - upload size limit "
                   f"...]\n{clipped}")

    # This host takes the body verbatim - no form encoding, no field names.
    body = payload.encode("utf-8", "replace")
    req = urllib.request.Request(
        _PASTE_API, data=body, method="POST",
        headers={"User-Agent": _USER_AGENT,
                 "Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            url = resp.read(2048).decode("utf-8", "replace").strip().strip('"')
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(
                f"{PASTE_HOST} is rate-limiting - wait a minute and try again."
            ) from exc
        if exc.code == 413:
            raise RuntimeError(f"{PASTE_HOST} rejected it as too large.") from exc
        raise RuntimeError(
            f"{PASTE_HOST} refused the upload (HTTP {exc.code}).") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach {PASTE_HOST}: {exc}") from exc
    if not url.lower().startswith("http"):
        raise RuntimeError(f"{PASTE_HOST} returned an unexpected response.")
    return url


def fetch_text(url: str, *, timeout: float = 20.0) -> str:
    """Download the text behind *url*.

    paste.c-net.org already serves the raw body at the bare URL, so nothing is
    rewritten; other hosts are fetched as given. The body is size-capped.
    """
    import urllib.error
    import urllib.request

    text = (url or "").strip()
    if not text:
        raise RuntimeError("No link to open.")

    req = urllib.request.Request(text, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            raw = resp.read(_MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(
                "The paste host is rate-limiting - wait a minute and try again."
            ) from exc
        if exc.code in (404, 410):
            raise RuntimeError(
                "That link no longer exists - it may have expired.") from exc
        raise RuntimeError(f"Link returned HTTP {exc.code}.") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not open the link: {exc}") from exc
    if len(raw) > _MAX_FETCH_BYTES:
        raise RuntimeError("That link is too large.")
    return raw.decode("utf-8", "replace").strip()
