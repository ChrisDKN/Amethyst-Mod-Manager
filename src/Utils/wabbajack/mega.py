from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
from urllib.parse import urlparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from .http import DownloadUnavailable, download_http
from .paths import WabbajackError


def _decode(value):
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise DownloadUnavailable("The MEGA link contains an invalid file key. Obtain the complete download link from the author.") from exc


def _file_link(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"mega.nz", "mega.co.nz", "www.mega.nz", "www.mega.co.nz"}:
        raise DownloadUnavailable("The MEGA download link is invalid. Use Select File for the required archive.")
    match = re.fullmatch(r"/file/([A-Za-z0-9_-]+)(?:/)?", parsed.path)
    if match:
        handle, encoded = match[1], parsed.fragment
    else:
        legacy = re.fullmatch(r"!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)", parsed.fragment)
        if not legacy or parsed.path not in {"", "/"}:
            raise DownloadUnavailable("This MEGA link needs browser access. Open it and select the exact required file; automatic downloads support public file links with a decryption key.")
        handle, encoded = legacy[1], legacy[2]
    key = _decode(encoded)
    if len(_decode(handle)) != 6 or len(key) != 32:
        raise DownloadUnavailable("The MEGA file link or decryption key is incomplete. Obtain the full link from the author.")
    return handle, key


def _xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def _cipher(key, mode):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    return Cipher(algorithms.AES(key), mode)


def _decryptor(key, offset):
    from cryptography.hazmat.primitives.ciphers import modes
    counter = key[16:24] + (offset // 16).to_bytes(8, "big")
    decoder = _cipher(_xor(key[:16], key[16:]), modes.CTR(counter)).decryptor()
    if offset % 16:
        decoder.update(bytes(offset % 16))
    return decoder.update


def _verify_mac(path, key, stop):
    from cryptography.hazmat.primitives.ciphers import modes
    aes_key = _xor(key[:16], key[16:])
    aggregate = bytes(16)
    chunk_size = 128 * 1024
    combine = _cipher(aes_key, modes.ECB()).encryptor()
    with path.open("rb") as source:
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation stopped")
            chunk = source.read(chunk_size)
            if not chunk:
                break
            chunk += bytes(-len(chunk) % 16)
            cipher = _cipher(aes_key, modes.CBC(key[16:24] * 2)).encryptor()
            mac = (cipher.update(chunk) + cipher.finalize())[-16:]
            aggregate = combine.update(_xor(aggregate, mac))
            chunk_size = min(chunk_size + 128 * 1024, 1024 * 1024)
    actual = _xor(aggregate[:4], aggregate[4:8]) + _xor(aggregate[8:12], aggregate[12:])
    if not hmac.compare_digest(actual, key[24:32]):
        raise WabbajackError("MEGA file authentication failed. The downloaded file is damaged or the link has the wrong key.")


def _ticket(session, handle, key, size, stop):
    from cryptography.hazmat.primitives.ciphers import modes
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")
    with session.post("https://g.api.mega.co.nz/cs", params={"id": secrets.randbits(32)},
                      json=[{"a": "g", "g": 1, "p": handle, "ssl": 2}], timeout=(20, 60),
                      verify=resolve_ca_bundle() or True) as response:
        response.raise_for_status()
        try:
            data = response.json()
            data = data[0] if isinstance(data, list) and len(data) == 1 else data
        except ValueError as exc:
            raise WabbajackError("MEGA returned an invalid download response") from exc
    error = data if isinstance(data, int) else data.get("e", 0) if isinstance(data, dict) else None
    if error:
        reason = {-9: "The file is no longer available.", -11: "Access to this file is restricted.",
                  -16: "The file is blocked or has been removed.", -17: "The transfer quota has been reached."}.get(error)
        if reason:
            raise DownloadUnavailable("MEGA: " + reason + " Open the download page for details. For a quota limit, wait or use your own MEGA account in the browser, then resume.")
        raise WabbajackError(f"MEGA could not provide a download (error {error})")
    if not isinstance(data, dict) or not isinstance(data.get("g"), str):
        raise WabbajackError("MEGA did not provide a public file download")
    if data.get("s") != size:
        raise DownloadUnavailable("MEGA's file size differs from the modlist. Obtain the exact required version from the author.")
    url = data["g"]
    if urlparse(url).scheme != "https":
        raise WabbajackError("MEGA did not provide a secure download link")
    attributes = _decode(data.get("at", ""))
    if not attributes or len(attributes) % 16 or len(attributes) > 64 * 1024:
        raise WabbajackError("MEGA returned invalid file metadata")
    decoder = _cipher(_xor(key[:16], key[16:]), modes.CBC(bytes(16))).decryptor()
    plain = (decoder.update(attributes) + decoder.finalize()).rstrip(b"\0")
    try:
        if not plain.startswith(b'MEGA{"') or not isinstance(json.loads(plain[4:]).get("n"), str):
            raise ValueError("Invalid file attributes")
    except (ValueError, AttributeError) as exc:
        raise DownloadUnavailable("The MEGA key does not unlock this file. Obtain the complete link from the author or use Select File.") from exc
    return url


def download_mega(url, target, *, size, expected, stop=None, progress=None):
    handle, key = _file_link(url)
    with requests.Session() as session:
        def open_response(headers):
            ticket = _ticket(session, handle, key, size, stop)
            response = session.get(ticket, headers=headers, stream=True, timeout=(20, 60),
                                   verify=resolve_ca_bundle() or True)
            if response.status_code == 509:
                response.close()
                raise DownloadUnavailable("MEGA's transfer quota has been reached. Open the file in your browser to use your own account, or wait for the quota to reset and resume.")
            return response
        return download_http(url, target, size=size, expected=expected, stop=stop, progress=progress,
                             open_response=open_response, transform=lambda offset: _decryptor(key, offset),
                             validate=lambda path: _verify_mac(path, key, stop))
