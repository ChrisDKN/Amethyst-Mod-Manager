from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


MAX_RECORD_BYTES = 16 * 1024 * 1024
_TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}
_SECRETS = {'api_key', 'access_token', 'refresh_token', 'password', 'authorization',
            'client_secret', 'nxm_url', 'nxm_link', 'signed_url', 'tokens'}


def _redacted(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if str(key).lower() in _SECRETS else _redacted(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
        def clean(match):
            try:
                url = urlsplit(match.group())
                return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, '', ''))
            except ValueError:
                return '[redacted URL]'
        return re.sub(r'(?:https?|nxm)://[^\s<>"\x27]+', clean, value)
    return value


class JobHistory:
    def __init__(self, directory: Path | None = None, *, retention: int = 500):
        from Utils.config_paths import get_config_dir
        from Utils.processes.leases import boot_identity, process_identity

        self.directory = directory or Path(os.environ.get('AMETHYST_JOB_HISTORY_DIR') or
                                           get_config_dir() / 'headless' / 'jobs')
        self.error = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
        except OSError as exc:
            self.error = str(exc)
        self.retention = retention
        self.owner = {'pid': os.getpid(), 'process_start': process_identity(os.getpid()),
                      'boot_id': boot_identity(), 'session': uuid.uuid4().hex}

    def save(self, snapshot: dict) -> bool:
        from Utils.atomic_write import atomic_writer
        try:
            record = {'version': 1, 'owner': self.owner, 'job': _redacted(snapshot)}
            record['job']['history_persisted'] = True
            raw = json.dumps(record, ensure_ascii=True)
            if len(raw) > MAX_RECORD_BYTES:
                record['job']['result'] = None
                record['job']['result_omitted'] = True
                if record['job']['error']:
                    record['job']['error']['details'] = {'omitted': True}
                raw = json.dumps(record, ensure_ascii=True)
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError('Job history record exceeds its storage limit')
            path = self.directory / (snapshot['id'] + '.json')
            with atomic_writer(path) as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.error = None
            if snapshot['state'] in _TERMINAL:
                self._prune()
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)
            return False

    def get(self, job_id: str) -> dict:
        from Utils.processes.leases import boot_identity, process_identity

        if not re.fullmatch('[0-9a-f]{32}', job_id):
            raise KeyError(job_id)
        path = self.directory / (job_id + '.json')
        if not path.is_file():
            raise KeyError(job_id)
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError('Unsafe job history record')
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or not isinstance(record.get('job'), dict)
                or not isinstance(record.get('owner'), dict)):
            raise ValueError('Invalid job history record')
        job, owner = record['job'], record['owner']
        if (record['version'] != 1 or job['id'] != job_id
                or not isinstance(job.get('kind'), str) or not isinstance(job.get('created_at'), str)
                or not isinstance(job.get('state'), str)
                or (job.get('game') is not None and not isinstance(job['game'], dict))
                or not isinstance(owner.get('pid'), int)):
            raise ValueError('Invalid job history record')
        live = (owner['boot_id'] == boot_identity() and owner['process_start'] is not None
                and process_identity(owner['pid']) == owner['process_start'])
        if job['state'] not in _TERMINAL and not live:
            job = {**job, 'state': 'interrupted', 'cancellable': False, 'pausable': False,
                   'paused': False, 'error': {'name': 'outcome_unknown',
                    'message': 'The owning process ended before recording an outcome.',
                    'details': {'last_recorded_state': job['state']}}}
        return {'job': job, 'owner_active': live, 'owner_session': owner['session']}

    def list(self, *, game=None, recovery_only=False) -> dict:
        records, unavailable = [], []
        for path in self.directory.glob('*.json'):
            try:
                record = self.get(path.stem)
                job = record['job']
                if game is not None and (job.get('game') or {}).get('id') != game:
                    continue
                if recovery_only and job['state'] not in {'failed', 'cancelled', 'interrupted'}:
                    continue
                record['recovery_methods'] = self.recovery_methods(job)
                records.append(record)
            except (KeyError, OSError, ValueError, TypeError):
                unavailable.append(path.stem)
        records.sort(key=lambda record: (record['job']['created_at'], record['job']['id']), reverse=True)
        return {'records': records, 'unavailable': unavailable, 'storage_error': self.error}

    @staticmethod
    def recovery_methods(job):
        kind = job['kind']
        if kind.startswith('collections.'):
            return ['collections.installed.list', 'collections.resume.start']
        if kind.startswith('wabbajack.'):
            return ['wabbajack.list', 'wabbajack.preflight.start', 'wabbajack.install.start']
        if kind in {'nexus.download', 'downloads.interrupted.resume'}:
            return ['downloads.interrupted.list', 'downloads.interrupted.resume.start']
        if kind == 'launch':
            return ['launch.active.list', 'games.get']
        return ['games.get', 'profiles.list', 'mods.list', 'downloads.list'] if job.get('game') else []

    def _prune(self):
        records = self.list()['records']
        for record in records[self.retention:]:
            if record['job']['state'] in _TERMINAL:
                (self.directory / (record['job']['id'] + '.json')).unlink(missing_ok=True)
