from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit


def normalize_source(url, branch="", folder="", enabled=True):
    if not all(isinstance(value, str) for value in (url, branch, folder)) or type(enabled) is not bool:
        raise ValueError("Invalid plugin repository configuration.")
    parsed = urlsplit(url.strip())
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTPS GitHub repository URL, with branch and folder in separate fields.")
    repository = parsed.path.strip("/")
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub repository URL such as https://github.com/owner/repository.")
    if repository.split("/")[1] in (".", ".."):
        raise ValueError("Invalid GitHub repository name.")
    branch, folder = branch.strip(), folder.strip()
    if branch and (branch.startswith("-") or any(ord(c) < 32 for c in branch)):
        raise ValueError("Invalid repository branch.")
    if folder and ("\\" in folder or any(p in ("", ".", "..") for p in folder.split("/"))):
        raise ValueError("Use a repository-relative folder without leading or trailing slashes.")
    identity = (repository.lower(), branch, folder)
    sid = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    if identity == ("chrisdkn/amethyst-mod-manager", "Resources", "Plugins/v2"):
        sid = "builtin"
    return {"id": sid, "url": f"https://github.com/{repository}",
            "repository": repository, "branch": branch, "folder": folder, "enabled": enabled}


DEFAULT_SOURCE = normalize_source(
    "https://github.com/ChrisDKN/Amethyst-Mod-Manager", "Resources", "Plugins/v2")


def read_source(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid plugin repository configuration.")
    source = normalize_source(data["url"], data.get("branch", ""),
                              data.get("folder", ""), data.get("enabled", True))
    if data.get("id", source["id"]) != source["id"]:
        raise ValueError("Plugin repository identity does not match its configuration.")
    return source


def api_url(source):
    return f"https://api.github.com/repos/{source['repository']}"


def raw_url(source):
    return f"https://raw.githubusercontent.com/{source['repository']}"
