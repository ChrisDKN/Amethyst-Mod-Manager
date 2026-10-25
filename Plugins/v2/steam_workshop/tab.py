import copy
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtWidgets import (
    QLabel, QSizePolicy,
    QVBoxLayout, QWidget,
)

from Utils.games.registry import _GAMES
from Utils.profiles.state import read_only_profile_state

from .browser import WorkshopBrowserView
from .download import saved_account
from .handoff import queue_operation, result
from .helper import helper_path
from Utils.ui.config import load_download_only
from .support import INSTALL_EXCLUSIONS, workshop_app_id


class Browser(WorkshopBrowserView):
    def __init__(self, game, **kwargs):
        self.disposed = False
        super().__init__(game, **kwargs)

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self._cancel.set()
        self._action_cancel.set()
        self._client.close()
        self._stop_subscription_check()
        self._stop_subscribed_listing()
        self._close_details()
        QCoreApplication.instance().aboutToQuit.disconnect(self._client.close)
        QCoreApplication.instance().aboutToQuit.disconnect(self._stop_subscription_check)
        QCoreApplication.instance().aboutToQuit.disconnect(self._stop_subscribed_listing)


class WorkshopTab(QWidget):
    def __init__(self, context, parent):
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.context = {}
        self.scope = None
        self.browser = self.game = self.profile = None
        self.disposed = False
        self.last_result = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.account = self.sign_in = self.forget = None
        self.last_download_mode = bool(load_download_only())
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        outer.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._refresh)
        self.set_context(context)
        self.timer.start()

    def _log(self, message):
        if not self.disposed:
            self.status.setText(str(message))

    def _clear_browser(self):
        if self.browser is not None:
            self.browser.dispose()
            self.body.removeWidget(self.browser)
            self.browser.hide()
            self.browser.deleteLater()
            self.browser = None
        self.account = self.sign_in = self.forget = None

    def set_context(self, context):
        if self.disposed:
            return
        self.context = dict(context)
        scope = (context['game_id'], context['profile'])
        if self.scope == scope:
            self._refresh()
            return
        self.scope = scope
        self.last_result = None
        self._clear_browser()
        self.game = self.profile = None
        try:
            game = copy.copy(next(game for game in _GAMES.values() if game.game_id == context['game_id']))
            profile_name = context['profile']
            if not profile_name or Path(profile_name).name != profile_name or profile_name in {'.', '..'}:
                raise ValueError('Select a profile to browse Workshop.')
            profile = Path(game.get_profile_root()) / 'profiles' / profile_name
            if not profile.is_dir() or profile.is_symlink():
                raise ValueError('The selected profile is no longer available.')
            game.set_active_profile_dir(profile)
            with read_only_profile_state():
                game.load_paths(read_only=True)
            if not workshop_app_id(game):
                raise ValueError('The selected game does not have a Steam App ID.')
            helper_path()
            self.game, self.profile = game, profile
            self.browser = Browser(game, parent=self, toolbar_fn=self._account_controls,
                                   context_fn=lambda: (self.game, self.profile),
                                   install_fn=self._download, subscribe_fn=self._subscribe, log_fn=self._log)
            self.body.addWidget(self.browser, 1)
            self._log(INSTALL_EXCLUSIONS.get(workshop_app_id(game), 'Browse anonymously. Sign in when you download a mod.'))
        except Exception as exc:
            self._log(str(exc) or 'The selected game is no longer available.')
        self._refresh()

    def _account_controls(self, browser, row):
        self.account = QLabel()
        self.account.setTextFormat(Qt.PlainText)
        self.account.setMaximumWidth(180)
        row.addWidget(self.account)
        self.sign_in = browser._button(row, 'Sign in to Steam', lambda: self._operation('login'))
        self.forget = browser._button(row, 'Remove saved login', lambda: self._operation('forget'))

    def _operation(self, action, **kwargs):
        if self.disposed or not self.isEnabled() or self.browser is None:
            return
        try:
            queue_operation(self.context, action, **kwargs)
        except Exception as exc:
            self._log(str(exc))

    def _download(self, items, context, *, enable_requirements=None):
        if context != (self.game, self.profile):
            self._log('Game or profile changed. Select the mods again.')
            return
        self._operation('download', items=items, requirements=enable_requirements)

    def _subscribe(self, items, context, *, unsubscribe=False):
        if context != (self.game, self.profile):
            self._log('Game or profile changed. Select the mods again.')
            return
        self._operation('unsubscribe' if unsubscribe else 'subscribe', items=items)

    def _refresh(self):
        if self.disposed or not self.isEnabled():
            return
        if self.account is not None:
            account = saved_account()
            text = f'Steam: {account}' if account else 'Steam: not connected'
            self.account.setText(self.account.fontMetrics().elidedText(text, Qt.ElideRight, 180))
            self.account.setToolTip(text)
            self.sign_in.setEnabled(self.browser is not None)
            self.forget.setEnabled(self.browser is not None and bool(account))
        if self.browser is not None:
            self.browser.refresh_subscriptions()
        mode = bool(load_download_only())
        if mode != self.last_download_mode:
            self.last_download_mode = mode
            if self.browser is not None:
                self.browser.refresh_install_labels()
        record = result()
        token = (record.get('id'), record.get('updated'))
        if token != self.last_result and (record.get('game_id'), record.get('profile')) == self.scope:
            self.last_result = token
            self._log(record.get('message', ''))
            if self.browser is not None:
                self.browser.refresh_subscriptions(force=True)
                self.browser.refresh_installed()

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.timer.stop()
        self._clear_browser()
        self.context = {}
