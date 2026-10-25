from datetime import datetime, timezone
import threading
import time

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from Utils.app_log import app_log
from Utils.games.registry import _GAMES
from gui_qt.runtime.safe_emit import safe_emit

from .client import WorkshopClient
from .download import DownloadCancelled, saved_account, state_root
from .handoff import queue_operation, result
from .preferences import preferences
from .support import workshop_app_id


class SubscribedMods(QObject):
    ready = Signal(object, object, str)

    def __init__(self, context):
        super().__init__()
        self.context = dict(context)
        self.closed = False
        self.scope = None
        self.entries = {}
        self.client = None
        self.cancel = threading.Event()
        self.checked = 0
        self.result_token = None
        self.reported = set()
        self.ready.connect(self._received)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._ensure)
        self.timer.start()
        QCoreApplication.instance().aboutToQuit.connect(self._stop)
        QTimer.singleShot(0, self, self._ensure)

    def set_context(self, context):
        self.context = dict(context)
        self._ensure()

    def _publish(self, rows):
        publish = self.context.get('set_modlist_rows')
        if publish:
            publish('subscribed', rows, game_id=self.context.get('game_id', ''),
                    profile=self.context.get('profile', ''), on_action=self._action)

    def _stop(self):
        self.cancel.set()
        if self.client is not None:
            self.client.close()
        self.client = None

    def _ensure(self):
        if self.closed or not self.context.get('set_modlist_rows'):
            return
        game_id, profile = self.context.get('game_id', ''), self.context.get('profile', '')
        game = next((g for g in _GAMES.values() if g.game_id == game_id), None)
        app_id = workshop_app_id(game)
        supported = preferences().get('workshop_support', {}).get(game_id) is True
        if not app_id or not supported or not profile:
            self._stop()
            self.entries.clear()
            self.scope = None
            self._publish(None)
            return
        account = saved_account()
        try:
            configs = tuple(sorted((str(p), p.stat().st_mtime_ns)
                for p in (state_root() / 'account').rglob('account.config') if p.is_file()))
        except OSError:
            configs = ()
        scope = (game_id, profile, app_id, account, configs)
        if scope != self.scope:
            self._stop()
            self.scope = scope
            self.checked = 0
            self.entries.clear()
            self._publish([])
        record = result()
        token = (record.get('id'), record.get('updated'))
        if token != self.result_token and (record.get('game_id'), record.get('profile')) == scope[:2]:
            self.result_token = token
            self.checked = 0
        if not account or self.client is not None or time.monotonic() - self.checked < 60:
            return
        cancel = self.cancel = threading.Event()
        client = self.client = WorkshopClient(account=account)
        self.checked = time.monotonic()
        signal = self.ready
        def worker():
            entries, error = {}, ''
            try:
                for page in range(1, 101):
                    batch, total, _ = client.listing(app_id, subscribed=True, page=page, page_size=100, cancel=cancel)
                    entries.update((entry.package_id, entry) for entry in batch)
                    if page * 100 >= total:
                        break
                else:
                    raise ValueError('The subscribed list exceeds 10000 mods.')
            except DownloadCancelled:
                return
            except Exception as exc:
                error = str(exc)
            finally:
                client.close()
            if not cancel.is_set():
                safe_emit(signal, (cancel, scope), entries, error)
        threading.Thread(target=worker, daemon=True, name='WorkshopModlistSubscriptions').start()

    def _received(self, token, entries, error):
        cancel, scope = token
        if self.closed or cancel is not self.cancel or cancel.is_set() or scope != self.scope:
            return
        self.client = None
        if error:
            self.entries.clear()
            self._publish([])
            if error not in self.reported:
                self.reported.add(error)
                app_log(f'[workshop] Subscription list: {error}')
            return
        self.entries = entries
        from Utils.downloads.core import fmt_size
        rows = []
        for entry in entries.values():
            item = entry.item
            updated = datetime.fromtimestamp(item.updated, timezone.utc).strftime('%Y-%m-%d') if item.updated else ''
            rows.append({'id': item.item_id, 'name': item.title,
                'tooltip': f'Steam subscription · managed by {scope[3]}\n{item.url}\n'
                           'Read only in Amethyst; shared across profiles. Enable in-game where required.',
                'values': {'category': 'Steam Workshop', 'size': fmt_size(item.size), 'updated': updated}})
        self._publish(rows)

    def _action(self, action):
        if self.closed or (action['game_id'], action['profile']) != (self.context.get('game_id'), self.context.get('profile')):
            return
        if self.scope is None or saved_account() != self.scope[3]:
            self._ensure()
            return
        kind = action['action_id']
        if kind == 'refresh':
            self._stop()
            self.checked = 0
            self._ensure()
        elif kind == 'login':
            queue_operation(self.context, 'login')
        else:
            entries = [self.entries[item] for item in action['ids'] if item in self.entries]
            if not entries:
                return
            if kind == 'unsubscribe':
                if len(entries) > 200:
                    raise ValueError('Select at most 200 Workshop mods to unsubscribe at once.')
                queue_operation(self.context, 'unsubscribe', items=[entry.item for entry in entries])
            elif kind == 'open':
                from Utils.environment.xdg import open_url
                for entry in entries[:10]:
                    open_url(entry.item.url)
            elif kind == 'copy':
                QCoreApplication.instance().clipboard().setText('\n'.join(entry.item.url for entry in entries))

    def dispose(self):
        self.closed = True
        self.timer.stop()
        self._stop()
        QCoreApplication.instance().aboutToQuit.disconnect(self._stop)
