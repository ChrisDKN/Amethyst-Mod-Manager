from __future__ import annotations

import json
import os
import time
from pathlib import Path


def process_identity(pid: int) -> str | None:
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def boot_identity() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def save_launch(directory: Path, launch_id: str, game, resources, identity) -> Path:
    from Utils.atomic_write import write_atomic_text

    path = directory / f'launch-{launch_id}.json'
    record = {
        'id': launch_id, 'operation': 'launch', 'pid': os.getpid(),
        'game_id': str(getattr(game, 'game_id', '')),
        'game_name': str(getattr(game, 'name', '')),
        'resources': list(resources), 'boot_id': boot_identity(), **identity,
    }
    write_atomic_text(path, json.dumps(record, ensure_ascii=True))
    path.chmod(0o600)
    return path


def active_launches(directory: Path, resources=None, *, prune=False) -> list[dict]:
    from .game import matching_pids, POLL_SECONDS

    found = []
    for path in sorted(directory.glob('launch-*.json')):
        try:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ValueError('Unsafe launch record')
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or not isinstance(record.get('resources'), list):
                raise ValueError('Invalid launch record')
            if resources is not None and not set(resources).intersection(record['resources']):
                continue
            active = False
            if record['boot_id'] == boot_identity():
                matched = matching_pids(record['markers'])
                roots = any(process_identity(int(pid)) == token
                            for pid, token in record['roots'].items())
                active = matched is None or bool(matched) or roots
                if not active and time.time() < record['startup_deadline']:
                    active = True
                if not active:
                    # Allow launcher handoffs the same missing-process grace as the monitor.
                    time.sleep(POLL_SECONDS)
                    matched = matching_pids(record['markers'])
                    active = matched is None or bool(matched)
            if active:
                found.append({key: record[key] for key in
                              ('id', 'operation', 'pid', 'game_id', 'game_name', 'resources')})
            elif prune:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            found.append({'operation': 'launch', 'record': str(path),
                          'inspection_error': 'launch_state_unavailable'})
    return found
