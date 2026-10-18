from collections import deque
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer

from Nexus.nxm_handler import nxm_log
from Nexus.nxm_routing import AMETHYST_HANDLER, launch_command, load_routes
from gui_qt.nexus.nxm_routing_overlay import NxmRoutingOverlay


class NxmRouter(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.queue = deque()
        self.busy = False
        self.forwarded = {}

    def route(self, url, domain):
        from Utils.games.registry import _GAMES
        now = time.monotonic()
        self.forwarded = {u: stamp for u, stamp in self.forwarded.items() if now - stamp < 30}
        try:
            desktop = load_routes().get(domain)
        except (OSError, ValueError) as exc:
            self.window._notify(self.tr("Could not read Nexus forwarding settings: {0}").format(exc), "error")
            return True
        if desktop != AMETHYST_HANDLER and url in self.forwarded:
            self.window._notify(self.tr("This Nexus link returned to Amethyst. Choose a different NXM launcher."), "warning")
            return True
        if not desktop and any(game.accepts_nexus_domain(domain) for game in _GAMES.values()):
            return False
        if (url, domain) not in self.queue:
            self.queue.append((url, domain))
        if not self.busy:
            self._next()
        return True

    def _next(self):
        if not self.queue:
            self.busy = False
            return
        self.busy = True
        url, domain = self.queue[0]
        try:
            desktop = load_routes().get(domain)
        except (OSError, ValueError) as exc:
            self._choose(str(exc))
            return
        if desktop:
            self._launch(desktop)
        else:
            self._choose()

    def _choose(self, error=""):
        domain = self.queue[0][1]

        def chosen(desktop):
            if desktop:
                self._launch(desktop)
            else:
                self._finish()

        NxmRoutingOverlay(self.window, chosen, domain=domain, error=error)

    def _finish(self):
        self.queue.popleft()
        QTimer.singleShot(0, self, self._next)

    def _launch(self, desktop):
        url, domain = self.queue[0]
        if desktop == AMETHYST_HANDLER:
            self.forwarded.pop(url, None)
            self._finish()
            self.window._process_nxm_link(url, use_amethyst=True)
            return
        try:
            command, env, handler = launch_command(desktop)
        except Exception as exc:
            self._choose(str(exc))
            return
        process = QProcess(self)
        environment = QProcessEnvironment()
        for key, value in env.items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory("/")
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        timer = QTimer(process)
        timer.setSingleShot(True)
        completed = False

        def done(error=""):
            nonlocal completed
            if completed:
                return
            completed = True
            timer.stop()
            if error:
                self.forwarded.pop(url, None)
                nxm_log(f"NXM forwarding failed for {domain}: {error}")
                self._choose(error)
            else:
                nxm_log(f"Forwarded NXM link for {domain} to {handler.name}")
                self.window._notify(self.tr("Sent Nexus link to {0}.").format(handler.name), "info")
                self._finish()
            process.deleteLater()

        def timeout():
            process.kill()
            done(self.tr("The NXM launcher did not respond. Check whether the other manager opened before retrying."))

        process.errorOccurred.connect(lambda _error: done(self.tr("Could not start the NXM launcher: {0}").format(process.errorString())))
        process.finished.connect(lambda code, status: done(
            "" if code == 0 and status == QProcess.NormalExit else
            self.tr("The NXM launcher failed. Check that the other manager is installed, or choose another launcher.")))
        timer.timeout.connect(timeout)
        self.forwarded[url] = time.monotonic()
        timer.start(10000)
        process.start(command[0], [*command[1:], url])
