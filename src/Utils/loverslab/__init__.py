"""LoversLab authentication and downloads."""

from urllib.parse import urlparse


def is_loverslab_url(url):
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        return (parsed.scheme in {"http", "https"} and not parsed.username
                and not parsed.password and parsed.port in {None, 80, 443}
                and (host == "loverslab.com" or host.endswith(".loverslab.com")))
    except ValueError:
        return False
