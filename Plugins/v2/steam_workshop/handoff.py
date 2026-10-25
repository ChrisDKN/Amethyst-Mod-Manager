import json
from pathlib import Path
import time
import uuid

from Utils.atomic_write import write_atomic_text
from .download import parse_item_id

_ROOT = Path(__file__).parent


def queue_operation(context, action, *, items=(), requirements=None):
    operation = {
        'id': uuid.uuid4().hex, 'created': time.time(), 'action': action,
        'game_id': context['game_id'], 'profile': context['profile'],
        'items': [item.__dict__ for item in items], 'requirements': dict(requirements or {}),
    }
    write_atomic_text(_ROOT / 'operation.json', json.dumps(operation))
    context['open_plugin']()


def take_operation(context):
    path = _ROOT / 'operation.json'
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError('Invalid Workshop action. Select the items again in the browser.')
        operation = json.loads(path.read_text())
        if not isinstance(operation, dict) or operation.get('action') not in {'download', 'subscribe', 'unsubscribe', 'login', 'forget'}:
            raise ValueError('Invalid Workshop action. Select the items again in the browser.')
        if (operation.get('game_id') != context['game_id'] or operation.get('profile') != context['profile']
                or not isinstance(operation.get('created'), (float, int))
                or not 0 <= time.time() - operation['created'] < 300):
            raise ValueError('Game, profile, or pending action changed. Select the items again in the browser.')
        items, requirements = operation.get('items'), operation.get('requirements')
        if not isinstance(items, list) or len(items) > 200 or not isinstance(requirements, dict) or len(requirements) > 200:
            raise ValueError('Invalid Workshop item list.')
        for item in items:
            if (not isinstance(item, dict) or set(item) != {'app_id', 'item_id', 'title', 'size', 'updated'}
                    or not isinstance(item['title'], str) or len(item['title']) > 4096
                    or not isinstance(item['app_id'], str) or not item['app_id'].isascii()
                    or not item['app_id'].isdecimal() or not 0 < int(item['app_id']) < 2**32
                    or not isinstance(item['item_id'], str)
                    or parse_item_id(item['item_id']) != item['item_id']
                    or any(type(item[key]) is not int or item[key] < 0 for key in ('size', 'updated'))):
                raise ValueError('Invalid Workshop item. Select it again in the browser.')
        for item_id, name in requirements.items():
            if parse_item_id(item_id) != item_id or not isinstance(name, str) or not name or len(name) > 255:
                raise ValueError('Invalid Workshop requirement.')
        return operation
    except (OSError, ValueError, TypeError) as exc:
        return {'action': 'invalid', 'message': str(exc)}
    finally:
        path.unlink(missing_ok=True)


def save_result(operation, message):
    record = {key: operation[key] for key in ('id', 'game_id', 'profile')}
    record.update(message=str(message), updated=time.time_ns())
    write_atomic_text(_ROOT / 'operation-result.json', json.dumps(record))


def result():
    try:
        record = json.loads((_ROOT / 'operation-result.json').read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}
