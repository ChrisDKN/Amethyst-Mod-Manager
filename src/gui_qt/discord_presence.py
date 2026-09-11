"""Optional Discord Rich Presence over the desktop client's local socket."""

import json
import os
import struct
import time
import uuid

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalSocket


APPLICATION_ID = "1547793579118825532"
_RETRY_MS = 15000
_MAX_FRAME = 64 * 1024


def _socket_paths():
    if os.name == "nt":
        return [f"discord-ipc-{i}" for i in range(10)]
    roots = [os.environ.get(key) for key in
             ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")]
    roots.append("/tmp")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        roots.extend(os.path.join(runtime, "app", app_id) for app_id in (
            "com.discordapp.Discord", "com.discordapp.DiscordCanary",
            "com.discordapp.DiscordPTB", "dev.vencord.Vesktop"))
    return [os.path.join(root, f"discord-ipc-{i}")
            for root in dict.fromkeys(roots) if root for i in range(10)]


class DiscordPresence(QObject):
    def __init__(self, game_name, parent=None, *, application_id=APPLICATION_ID):
        super().__init__(parent)
        self._game_name = game_name
        self._application_id = application_id
        self._enabled = False
        self._socket = None
        self._paths = []
        self._buffer = bytearray()
        self._ready = False
        self._sent = None
        self._next_update = 0.0
        self._pending_nonce = None
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._update)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._connect)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(5000)
        self._timeout.timeout.connect(self._failed)

    def set_enabled(self, enabled):
        enabled = bool(enabled and self._application_id)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self._poll.start()
            self._retry.start(0)
        else:
            self.stop()

    def stop(self):
        self._enabled = False
        self._poll.stop()
        self._retry.stop()
        if self._ready:
            self._send_activity(None)
            if self._socket is not None:
                self._socket.flush()
        self._drop_socket()
        self._paths.clear()

    def _drop_socket(self):
        self._timeout.stop()
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.blockSignals(True)
            sock.abort()
            sock.deleteLater()
        self._buffer.clear()
        self._ready = False
        self._sent = None
        self._pending_nonce = None
        self._next_update = 0.0

    def _connect(self):
        if not self._enabled:
            return
        if not self._paths:
            self._paths = _socket_paths()
        sock = QLocalSocket(self)
        self._socket = sock
        sock.setReadBufferSize(_MAX_FRAME + 8)
        sock.connected.connect(self._handshake)
        sock.readyRead.connect(self._read)
        sock.errorOccurred.connect(self._failed)
        sock.disconnected.connect(self._failed)
        self._timeout.start()
        sock.connectToServer(self._paths.pop(0))

    def _failed(self, *_):
        was_ready = self._ready
        self._drop_socket()
        if was_ready:
            self._paths.clear()
        if self._enabled:
            self._retry.start(0 if self._paths else _RETRY_MS)

    def _write(self, opcode, payload):
        if self._socket is not None:
            self._socket.write(struct.pack("<II", opcode, len(payload)) + payload)

    def _send(self, opcode, payload):
        self._write(opcode, json.dumps(payload, separators=(",", ":")).encode())

    def _handshake(self):
        self._send(0, {"v": 1, "client_id": self._application_id})

    def _read(self):
        self._buffer.extend(bytes(self._socket.readAll()))
        while len(self._buffer) >= 8:
            opcode, size = struct.unpack_from("<II", self._buffer)
            if size > _MAX_FRAME:
                self._failed()
                return
            if len(self._buffer) < 8 + size:
                return
            payload = bytes(self._buffer[8:8 + size])
            del self._buffer[:8 + size]
            if opcode == 3:
                self._write(4, payload)
                continue
            if opcode == 4:
                continue
            if opcode != 1:
                self._failed()
                return
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeError):
                self._failed()
                return
            if not isinstance(data, dict) or data.get("evt") == "ERROR":
                self._failed()
                return
            if data.get("cmd") == "DISPATCH" and data.get("evt") == "READY":
                self._timeout.stop()
                self._ready = True
                self._update()
            elif self._pending_nonce and data.get("nonce") == self._pending_nonce:
                self._pending_nonce = None
                self._timeout.stop()

    def _send_activity(self, activity):
        self._pending_nonce = uuid.uuid4().hex
        self._send(1, {"cmd": "SET_ACTIVITY", "nonce": self._pending_nonce,
                       "args": {"pid": os.getpid(), "activity": activity}})
        self._timeout.start()

    def _update(self):
        if not self._enabled or not self._ready or self._pending_nonce:
            return
        name = self._game_name()
        details = (("Modding " + name).encode("utf-8")[:128]
                   .decode("utf-8", errors="ignore")) if name else ""
        if details == self._sent:
            return
        if details and time.monotonic() < self._next_update:
            return
        self._send_activity({"details": details} if details else None)
        self._sent = details
        self._next_update = time.monotonic() + 15
