import json

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class JsonProcess(QObject):
    notification = Signal(str, object)
    server_request = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, label, parent):
        super().__init__(parent)
        self.label = label
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read_errors)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.buffer = bytearray()
        self.diagnostics = ""
        self.pending = {}
        self.sequence = 0
        self.closing = False

    @property
    def running(self):
        return self.process.state() != QProcess.NotRunning

    def start(self, program, arguments, *, cwd=None, environment=None):
        self.closing = False
        self.buffer.clear()
        self.diagnostics = ""
        if cwd:
            self.process.setWorkingDirectory(str(cwd))
        if environment is not None:
            self.process.setProcessEnvironment(environment)
        self.process.start(program, arguments)

    def send(self, message):
        if not self.running or self.closing:
            return False
        return self.process.write((json.dumps(message, ensure_ascii=True) + "\n").encode()) >= 0

    def request(self, method, params, callback):
        self.sequence += 1
        request_id = self.sequence
        self.pending[request_id] = callback
        if not self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}):
            self.pending.pop(request_id, None)
            callback(None, {"message": f"{self.label} is not connected."})

    def respond(self, request_id, result):
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _read(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Expected an object")
            except (ValueError, UnicodeError):
                self.failed.emit(f"{self.label} returned invalid protocol data.")
                self.close()
                return
            if "method" in message:
                if "id" in message:
                    self.server_request.emit(message)
                else:
                    self.notification.emit(message["method"], message.get("params") or {})
            else:
                callback = self.pending.pop(message.get("id"), None)
                if callback:
                    callback(message.get("result"), message.get("error"))

    def _read_errors(self):
        self.diagnostics = (self.diagnostics + bytes(
            self.process.readAllStandardError()).decode("utf-8", "replace"))[-8000:]

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self._fail_pending()
            self.failed.emit(f"Could not start {self.label}: {self.process.errorString()}")
            self.stopped.emit()

    def _fail_pending(self):
        callbacks, self.pending = self.pending, {}
        for callback in callbacks.values():
            callback(None, {"message": f"{self.label} disconnected; the operation result is unknown."})

    def _finished(self, *_):
        self._read()
        self._read_errors()
        self._fail_pending()
        if not self.closing:
            detail = self.diagnostics.strip()[-1500:]
            self.failed.emit(f"{self.label} exited. Close Workshop and reopen it to reconnect.\n{detail}")
        self.stopped.emit()

    def close(self, *, terminate=False):
        self.closing = True
        self.process.closeWriteChannel()
        if terminate and self.running:
            self.process.terminate()
            QTimer.singleShot(2000, self, self._kill_if_running)

    def _kill_if_running(self):
        if self.running:
            self.process.kill()


