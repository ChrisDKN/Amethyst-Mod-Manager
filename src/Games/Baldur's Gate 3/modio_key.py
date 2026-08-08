"""
modio_key.py  (Baldur's Gate 3)

Secure storage for the user's read-only mod.io API key.  Mirrors the Nexus
key (``Nexus/nexus_api.py``): system keyring under the shared
``AmethystModManager`` service, with a machine-bound Fernet-encrypted
``modio_api_key.bin`` fallback when no keyring backend is available.
"""

from __future__ import annotations

from pathlib import Path

import keyring  # type: ignore

from Utils.app_log import app_log
from Utils.config_paths import get_config_dir

# Reuse the same keyring service as the Nexus key so all secrets live under
# one logical app entry; only the user/account differs.
_KEYRING_SERVICE = "AmethystModManager"
_KEYRING_USER = "modio_api_key"
_API_KEY_FILE = "modio_api_key.bin"


def _api_key_file_path() -> Path:
    return get_config_dir() / _API_KEY_FILE


def _keyring_ok() -> bool:
    """Reuse the Nexus keyring probe (set once at startup)."""
    try:
        from Nexus.nexus_oauth import _keyring_available
        return _keyring_available
    except Exception:
        return True  # assume available if we can't check


def _derive_key() -> bytes:
    """Derive a Fernet key from the machine ID (device-bound), matching Nexus."""
    import base64
    import hashlib
    machine_id = ""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                machine_id = f.read().strip()
            if machine_id:
                break
        except OSError:
            continue
    if not machine_id:
        machine_id = "fallback-no-machine-id"
    dk = hashlib.pbkdf2_hmac("sha256", machine_id.encode(),
                             b"AmethystModManager", 100_000)
    return base64.urlsafe_b64encode(dk)


def _load_key_file() -> str:
    p = _api_key_file_path()
    try:
        if not p.is_file():
            return ""
        from cryptography.fernet import Fernet
        import json as _json
        cipher = Fernet(_derive_key())
        data = _json.loads(cipher.decrypt(p.read_bytes()))
        return data.get("api_key", "").strip()
    except Exception:
        return ""


def _save_key_file(key: str) -> None:
    from cryptography.fernet import Fernet
    import json as _json
    import os as _os
    p = _api_key_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cipher = Fernet(_derive_key())
    p.write_bytes(cipher.encrypt(_json.dumps({"api_key": key}).encode()))
    _os.chmod(p, 0o600)


def _clear_key_file() -> None:
    p = _api_key_file_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def load_modio_key() -> str:
    """Return the saved mod.io API key, or "" if none is stored."""
    if not _keyring_ok():
        return _load_key_file()
    try:
        key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        return key.strip() if key else ""
    except UnicodeDecodeError as e:
        app_log(f"mod.io API key in keyring is corrupted ({e}); clearing.")
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass
        return ""
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for mod.io API key: {e} - using file fallback")
        return _load_key_file()


def save_modio_key(key: str) -> None:
    """Persist the mod.io API key to the keyring (or encrypted file fallback)."""
    key = key.strip()
    if not _keyring_ok():
        _save_key_file(key)
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key)
    except keyring.errors.KeyringError as e:
        app_log(f"Keyring unavailable for saving mod.io API key: {e} - using file fallback")
        _save_key_file(key)


def clear_modio_key() -> None:
    """Delete any stored mod.io API key from keyring and file."""
    _clear_key_file()
    if _keyring_ok():
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as e:
            app_log(f"Keyring unavailable when clearing mod.io API key: {e}")
