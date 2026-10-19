from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit


def normalize_repository(url, branch="", folder="", enabled=True):
    if not all(isinstance(value, str) for value in (url, branch, folder)) or type(enabled) is not bool:
        raise ValueError("Invalid repository configuration.")
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
    return {"id": sid, "url": f"https://github.com/{repository}",
            "repository": repository, "branch": branch, "folder": folder, "enabled": enabled}

