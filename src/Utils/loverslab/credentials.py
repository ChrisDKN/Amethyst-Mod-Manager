from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

import keyring

from Utils.config_paths import get_config_dir

_SERVICE = "AmethystModManager"
_ACCOUNT = "loverslab_credentials"
_FILE = "loverslab_credentials.bin"
_lock = threading.RLock()


class CredentialStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoversLabCredentials:
    email: str = field(repr=False)
    password: str = field(repr=False)


def _keyring_ok():
    from Nexus.nexus_api import _keyring_ok as available
    return available()


def _cipher():
    from cryptography.fernet import Fernet
    from Nexus.nexus_api import _derive_key
    return Fernet(_derive_key())


def _decode(payload):
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError
    email, password = data.get("email"), data.get("password")
    if not isinstance(email, str) or not email.strip() or not isinstance(password, str) or not password:
        raise ValueError
    return LoversLabCredentials(email.strip(), password)


def _payload(credentials):
    return json.dumps({"email": credentials.email, "password": credentials.password},
                      separators=(",", ":"))


def _write_record(mode, credentials=None):
    from Nexus.nexus_oauth import _write_owner_only
    record = {"version": 1, "mode": mode}
    if credentials is not None:
        record["credentials"] = _cipher().encrypt(_payload(credentials).encode()).decode("ascii")
    _write_owner_only(get_config_dir() / _FILE, json.dumps(record).encode())


def load_loverslab_credentials() -> LoversLabCredentials | None:
    with _lock:
        try:
            record = json.loads((get_config_dir() / _FILE).read_bytes())
        except FileNotFoundError:
            return None
        except Exception:
            raise CredentialStorageError("Saved LoversLab credentials could not be read. Clear them and sign in again.") from None
        try:
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError
            mode = record.get("mode")
            if mode == "cleared":
                return None
            if mode == "file":
                return _decode(_cipher().decrypt(record["credentials"].encode("ascii")))
            if mode != "keyring":
                raise ValueError
        except Exception:
            raise CredentialStorageError("Saved LoversLab credentials are unreadable. Clear them and sign in again.") from None
        try:
            if not _keyring_ok():
                raise RuntimeError
            payload = keyring.get_password(_SERVICE, _ACCOUNT)
            if not payload:
                raise RuntimeError
            return _decode(payload)
        except Exception:
            raise CredentialStorageError("LoversLab credentials are unavailable in the keyring. Unlock it or sign in again.") from None


def save_loverslab_credentials(email: str, password: str) -> None:
    credentials = LoversLabCredentials(email.strip(), password)
    if not credentials.email or not credentials.password:
        raise CredentialStorageError("Enter your LoversLab email and password first.")
    with _lock:
        try:
            _write_record("file", credentials)
        except Exception:
            raise CredentialStorageError("Could not save LoversLab credentials securely.") from None
        try:
            if not _keyring_ok():
                return
            keyring.set_password(_SERVICE, _ACCOUNT, _payload(credentials))
        except Exception:
            return
        try:
            _write_record("keyring")
        except Exception:
            # The new encrypted write-ahead record remains authoritative.
            pass


def clear_loverslab_credentials() -> None:
    with _lock:
        try:
            _write_record("cleared")
        except Exception:
            raise CredentialStorageError("Could not clear saved LoversLab credentials.") from None
        try:
            if _keyring_ok():
                keyring.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            # The cleared record also covers an inaccessible keyring.
            pass
