import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from Utils.app_log import app_log
from Utils.games.registry import _GAMES

from .client import WorkshopClient
from .helper import helper_path
from .preferences import preferences
from .support import fetch_support, workshop_app_id


class WorkshopSupport(QObject):
    ready = Signal(str, str, object, str)

    def __init__(self, context):
        super().__init__()
        self.context = dict(context)
        self.closed = False
        self.pending = {}
        self.reported = set()
        self.ready.connect(self._received)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self._ensure)
        self.timer.start()
        QTimer.singleShot(0, self, self._ensure)

    def set_context(self, context):
        self.context = dict(context)
        self._ensure()

    def _publish(self, game_id, supported, record=None):
        data = preferences()
        visibility = data.setdefault('workshop_support', {})
        records = data.setdefault('workshop_support_checks', {})
        if visibility.get(game_id) is supported and (record is None or records.get(game_id) == record):
            return
        visibility[game_id] = supported
        if record is not None:
            records[game_id] = record
        self.context['save_data'](data)
        self.context['data'] = data

    def _ensure(self):
        if self.closed:
            return
        game_id = self.context.get('game_id', '')
        if not game_id:
            return
        game = next((game for game in _GAMES.values() if game.game_id == game_id), None)
        app_id = workshop_app_id(game)
        if not app_id:
            self._publish(game_id, False)
            return
        record = self.context.get('data', {}).get('workshop_support_checks', {}).get(game_id, {})
        if record.get('app_id') == app_id and time.time() < record.get('expires', 0):
            self._publish(game_id, record.get('supported') is True)
            return
        self._publish(game_id, False)
        key = (game_id, app_id)
        if key in self.pending:
            return
        cancel, client = threading.Event(), WorkshopClient()
        self.pending[key] = cancel, client
        signal = self.ready
        def worker():
            supported, error = None, ''
            try:
                helper_path()
                supported = fetch_support(app_id, client, cancel)
            except Exception as exc:
                error = str(exc)
            finally:
                client.close()
            if not cancel.is_set():
                try:
                    signal.emit(game_id, app_id, supported, error)
                except RuntimeError:
                    pass
        threading.Thread(target=worker, daemon=True, name='WorkshopSupport').start()

    def _received(self, game_id, app_id, supported, error):
        self.pending.pop((game_id, app_id), None)
        if self.closed:
            return
        game = next((game for game in _GAMES.values() if game.game_id == game_id), None)
        if workshop_app_id(game) != app_id:
            return
        record = {'app_id': app_id, 'supported': supported is True,
                  'expires': time.time() + (300 if supported is None else 86400)}
        self._publish(game_id, supported is True, record)
        if error and error not in self.reported:
            self.reported.add(error)
            app_log(f'[workshop] Could not confirm support for {app_id}: {error}')

    def dispose(self):
        self.closed = True
        self.timer.stop()
        for cancel, client in self.pending.values():
            cancel.set()
            client.close()
        self.pending.clear()
