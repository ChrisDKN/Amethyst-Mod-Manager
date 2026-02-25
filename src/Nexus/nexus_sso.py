"""
nexus_sso.py
Nexus Mods Single Sign-On (SSO) integration.

Implements the SSO flow described at:
  https://github.com/Nexus-Mods/sso-integration-demo

Flow
----
1. Connect to wss://sso.nexusmods.com via WebSocket.
2. Send a request with a unique UUID and protocol version 2.
3. Open the user's browser to the Nexus authorisation page.
4. Wait for the API key to arrive over the WebSocket.
5. Store the key and close the connection.

The `application_slug` must be registered with Nexus Mods staff.
Update APPLICATION_SLUG once you receive your registered slug.

Usage
-----
    from Nexus.nexus_sso import NexusSSOClient

    def on_key(api_key: str):
        print("Got API key:", api_key)

    client = NexusSSOClient(on_api_key=on_key, on_error=print)
    client.start()   # non-blocking, runs in a background thread
    # ... later ...
    client.cancel()  # if the user cancels
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
import webbrowser
from typing import Callable, Optional

import websocket  # websocket-client

from version import __version__

log = logging.getLogger(__name__)

SSO_WEBSOCKET_URL = "wss://sso.nexusmods.com"
SSO_AUTH_URL = "https://www.nexusmods.com/sso"

#To replace
APPLICATION_SLUG = ""
APPLICATION_VERSION = __version__

# How long to wait before retrying a dropped connection (seconds)
_RECONNECT_DELAY = 5.0

# Maximum time to wait for the user to authorise (seconds, 0 = no limit)
_TIMEOUT = 300  # 5 minutes


class NexusSSOError(Exception):
    """Raised when the SSO flow fails."""


class NexusSSOClient:
    """
    Background SSO client that obtains a Nexus API key via browser authorisation.

    Parameters
    ----------
    on_api_key : callable(str)
        Called (from a background thread) when the API key is received.
    on_error : callable(str)
        Called (from a background thread) on unrecoverable errors.
    on_status : callable(str), optional
        Called with human-readable status messages for the UI.
    application_slug : str, optional
        Registered application slug. Defaults to APPLICATION_SLUG.
    """

    def __init__(
        self,
        on_api_key: Callable[[str], None],
        on_error: Callable[[str], None],
        on_status: Optional[Callable[[str], None]] = None,
        application_slug: str = APPLICATION_SLUG,
    ):
        self._on_api_key = on_api_key
        self._on_error = on_error
        self._on_status = on_status or (lambda _: None)
        self._slug = application_slug

        self._uuid: str = str(uuid.uuid4())
        self._connection_token: Optional[str] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False
        self._browser_opened = False
        self._got_key = False

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Begin the SSO flow in a background thread."""
        self._cancelled = False
        self._got_key = False
        self._browser_opened = False
        self._on_status("Connecting to Nexus Mods SSO...")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="nexus-sso")
        self._thread.start()

    def cancel(self) -> None:
        """Abort the SSO flow."""
        self._cancelled = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        """Main loop: connect → handshake → wait for key."""
        try:
            self._ws = websocket.WebSocketApp(
                SSO_WEBSOCKET_URL,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_ws_error,
                on_close=self._on_close,
            )
            # run_forever blocks until the socket is closed
            self._ws.run_forever(
                ping_interval=30,
                ping_timeout=10,
            )
        except Exception as exc:
            if not self._cancelled:
                log.exception("SSO connection failed")
                self._on_error(f"SSO connection failed: {exc}")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        """WebSocket connected — send the SSO handshake."""
        log.info("SSO WebSocket connected")
        self._on_status("Connected — sending authorisation request...")

        payload = {
            "id": self._uuid,
            "appid": self._slug,               # registered application id
            "token": self._connection_token,   # None on first connect
            "protocol": 2,
        }
        ws.send(json.dumps(payload))

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        """Handle messages from the SSO server."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("SSO: unparseable message: %s", raw[:200])
            return

        if not msg.get("success"):
            error = msg.get("error", "Unknown SSO error")
            log.error("SSO error: %s", error)
            self._on_error(f"SSO error: {error}")
            ws.close()
            return

        data = msg.get("data", {})

        # -- Connection token (store for reconnects) --
        if "connection_token" in data:
            self._connection_token = data["connection_token"]
            log.debug("SSO connection token received")

            # Now open the browser for the user to authorise
            if not self._browser_opened:
                self._browser_opened = True
                auth_url = (
                    f"{SSO_AUTH_URL}?id={self._uuid}"
                    f"&application={self._slug}"
                )
                self._on_status(
                    "Opening browser — please authorise the app on Nexus Mods..."
                )
                log.info("Opening SSO auth URL: %s", auth_url)
                webbrowser.open(auth_url)

        # -- API key received! --
        if "api_key" in data:
            api_key = data["api_key"]
            self._got_key = True
            log.info("SSO: API key received successfully")
            self._on_status("API key received!")
            self._on_api_key(api_key)
            ws.close()

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        """Handle WebSocket-level errors."""
        if self._cancelled:
            return
        log.warning("SSO WebSocket error: %s", error)

    def _on_close(self, ws: websocket.WebSocketApp, close_status: int | None,
                  close_msg: str | None) -> None:
        """Handle connection close — reconnect if we haven't got the key yet."""
        if self._cancelled or self._got_key:
            return

        log.info("SSO connection closed (status=%s), will reconnect in %.0fs",
                 close_status, _RECONNECT_DELAY)
        self._on_status("Connection lost — reconnecting...")

        import time
        time.sleep(_RECONNECT_DELAY)

        if not self._cancelled:
            self._run()  # reconnect with the same uuid + token
