from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .jobs import JobError


def forwarding_snapshot():
    from Nexus.nxm_routing import load_routes
    try:
        routes = load_routes()
    except Exception:
        raise JobError("invalid_nxm_routes", "Could not read Nexus link forwarding settings.") from None
    revision = hashlib.sha256(json.dumps(routes, sort_keys=True).encode()).hexdigest()
    return {"revision": revision, "routes": routes}


def forwarding_handlers():
    from Nexus.nxm_routing import AMETHYST_HANDLER, discover_handlers
    return {"handlers": [{"id": AMETHYST_HANDLER, "name": "Amethyst Mod Manager"},
                         *[{"id": str(item.path), "name": item.name} for item in discover_handlers()]]}


def validate_route(domain, handler):
    from Nexus.nxm_routing import AMETHYST_HANDLER, read_handler
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", domain):
        raise JobError("invalid_nxm_route", "Supply a lowercase Nexus game domain.")
    if handler is not None and (not isinstance(handler, str) or len(handler) > 4096
                                or any(ord(char) < 32 for char in handler) or not handler):
        raise JobError("invalid_nxm_route", "Supply amethyst, an absolute desktop launcher path, or null.")
    if handler not in (None, AMETHYST_HANDLER):
        if not Path(handler).is_absolute():
            raise JobError("invalid_nxm_route", "Supply an absolute desktop launcher path.")
        try:
            handler = str(read_handler(Path(handler)).path)
        except Exception:
            raise JobError("invalid_nxm_route", "The selected desktop launcher cannot handle Nexus links directly.") from None
    return domain, handler


def set_forwarding(revision, domain, handler, log):
    from Nexus.nxm_routing import save_route
    from Utils.operations import acquire_resource_operation
    from Utils.config_paths import get_config_dir
    path = get_config_dir() / "nxm_routes.json"
    with acquire_resource_operation("nxm.routes:" + str(path.absolute()), "nexus.forwarding.set",
                                    owner="headless:nexus.forwarding.set"):
        domain, handler = validate_route(domain, handler)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise JobError("unsafe_nxm_routes", "The forwarding settings must be a regular file.")
        current = forwarding_snapshot()
        if current["revision"] != revision:
            raise JobError("nxm_routes_changed", "Forwarding settings changed; read them again.")
        if current["routes"].get(domain) == handler:
            return {"changed": False, "forwarding": current}
        try:
            save_route(domain, handler)
        except Exception:
            raise JobError("nxm_routes_save_failed", "Could not save Nexus link forwarding settings.") from None
        log("Saved Nexus link forwarding settings.")
        return {"changed": True, "forwarding": forwarding_snapshot()}


def loverslab_status():
    from Utils.loverslab.credentials import load_loverslab_credentials
    try:
        credentials = load_loverslab_credentials()
    except Exception:
        raise JobError("loverslab_credentials_unavailable", "Could not read saved LoversLab credentials.") from None
    return {"configured": credentials is not None}


def validate_loverslab(email, password):
    if (not isinstance(email, str) or not email.strip() or len(email) > 512
            or any(ord(char) < 32 for char in email)
            or not isinstance(password, str) or not password or len(password) > 4096
            or any(char in password for char in "\x00\r\n")):
        raise JobError("invalid_loverslab_credentials", "Supply a nonempty email and password.")
    return email.strip(), password


def save_loverslab(email, password, _log, progress):
    from Utils.loverslab.client import LoversLabClient
    from Utils.loverslab.credentials import (LoversLabCredentials, save_loverslab_credentials,
                                            load_loverslab_credentials)
    from Utils.operations import acquire_resource_operation
    with acquire_resource_operation("loverslab.credentials", "loverslab.credentials.set",
                                    owner="headless:loverslab.credentials.set"):
        email, password = validate_loverslab(email, password)
        progress(0, 2, "verify_loverslab_credentials")
        try:
            client = LoversLabClient(LoversLabCredentials(email, password))
            try:
                client.login()
            finally:
                client.close()
        except Exception:
            raise JobError("loverslab_verification_failed", "Could not verify LoversLab credentials. Check the login and connection; site security checks may require a browser.") from None
        progress(1, 2, "save_loverslab_credentials")
        try:
            save_loverslab_credentials(email, password)
            if load_loverslab_credentials() != LoversLabCredentials(email, password):
                raise ValueError
        except Exception:
            raise JobError("loverslab_save_failed", "Could not securely save LoversLab credentials.") from None
        progress(2, 2, "save_loverslab_credentials")
        return {"configured": True}


def clear_loverslab(_log, progress):
    from Utils.loverslab.credentials import clear_loverslab_credentials
    from Utils.operations import acquire_resource_operation
    with acquire_resource_operation("loverslab.credentials", "loverslab.credentials.clear",
                                    owner="headless:loverslab.credentials.clear"):
        progress(0, 1, "clear_loverslab_credentials")
        try:
            clear_loverslab_credentials()
            if loverslab_status()["configured"]:
                raise ValueError
        except Exception:
            raise JobError("loverslab_clear_failed", "Could not clear saved LoversLab credentials.") from None
        progress(1, 1, "clear_loverslab_credentials")
        return {"configured": False}
