from __future__ import annotations

from copy import deepcopy

from .methods import REGISTRY, contracts

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_CONTRACTS = contracts()

_BOOL = set('accept_game_version_mismatch accept_known_issues allow_patch_warnings allow_unverified_source active_only all append apply apply_order cached_only check_proton clear_archives collapsed configured_only confirm create_mod deploy download_only enabled force hidden ignored include_adult include_manifest include_overwrite include_root_folder include_saves manual_interactive migrate_staging move open_browser overwrite overwrite_existing prefer_discrete_gpu preserve_conflicts profile_specific_mods refresh replace_existing replace_output root_folder root_folder_enabled save_settings skip skip_existing use_64bit winetricks_style'.split())
_INTEGER = set('after created_since_days file_id limit mod_id offset protocol_version request_id uploader_id version_index wait_ms'.split())
_ARRAY = set('archive_ids bain_selected bundle_selected categories checks dependencies entries exclusions expected_members fixes items members mods overwrite_excluded pack_indices paths plugins profiles requirement_ids reset rules skipped_optional_file_ids sources'.split())
_OBJECT = set('archives changes client filters fomod_selections game_roots groups manual_archives options overrides per_mod renames settings setup_options'.split())
_DYNAMIC = {'expected_value', 'value', 'position'}
_JOB = {
    'type': 'object',
    'required': ['id', 'kind', 'state', 'created_at', 'cancellable', 'pausable', 'event_sequence'],
    'properties': {
        'id': {'type': 'string'}, 'kind': {'type': 'string'},
        'state': {'enum': ['queued', 'running', 'waiting_for_input', 'succeeded', 'failed', 'cancelled', 'interrupted']},
        'game': {'type': ['object', 'null']}, 'profile': {'type': ['string', 'null']},
        'options': {'type': 'object'}, 'created_at': {'type': 'string'},
        'started_at': {'type': ['string', 'null']}, 'finished_at': {'type': ['string', 'null']},
        'progress': {'type': ['object', 'null']}, 'result': {'type': ['object', 'null']},
        'error': {'type': ['object', 'null']}, 'cancellable': {'type': 'boolean'},
        'pausable': {'type': 'boolean'}, 'paused': {'type': 'boolean'},
        'cancel_requested': {'type': 'boolean'}, 'event_sequence': {'type': 'integer'},
        'history_persisted': {'type': 'boolean'},
        'requires_client': {'type': 'boolean'}, 'waiting_for': {'type': ['object', 'null']},
    },
}
_ERROR = {
    '$schema': DIALECT, 'type': 'object', 'required': ['code', 'message', 'data'],
    'properties': {
        'code': {'type': 'integer'}, 'message': {'type': 'string'},
        'data': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string'}, 'details': {'type': 'object'},
        }},
    },
}


def _parameter(method, name):
    if name == 'interactive':
        return {'type': 'boolean', 'default': False}
    if method.startswith('installers.requests.') and name == 'request_id':
        return {'type': 'string', 'minLength': 1}
    if method.startswith('installers.') and name in {'step', 'group'}:
        return {'type': 'integer', 'minimum': 0, 'maximum': 100000}
    if name == 'answer':
        return {'type': 'object'}
    if name == 'content_prefix':
        return {'type': 'string', 'maxLength': 4096}
    if name == 'multi_mod_selected':
        return {'type': 'array', 'minItems': 1, 'maxItems': 4096, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}}
    if name in {'multi_mod_names', 'multi_mod_policies'}:
        if name == 'multi_mod_policies':
            return {'type': 'object', 'maxProperties': 4096, 'additionalProperties': {'enum': ['fail', 'replace', 'skip']}}
        return {'type': 'object', 'maxProperties': 4096, 'additionalProperties': {'type': 'string', 'minLength': 1}}
    if method.startswith('installers.sessions.') and name in {'step', 'group'}:
        return {'type': 'integer', 'minimum': 0, 'maximum': 100000}
    if method == 'thunderstore.install.start' and name == 'dependencies':
        return {'type': 'array', 'maxItems': 256, 'uniqueItems': True,
                'items': {'type': 'string', 'minLength': 1, 'maxLength': 255}}
    if method == 'thunderstore.install.start' and name == 'installer_policy':
        return {'enum': ['reject', 'defaults'], 'default': 'reject'}
    if method.startswith('games.custom.'):
        if name == 'definition':
            from Utils.games.custom_definitions import definition_schema
            return definition_schema()
        if name == 'revision':
            return {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}

    if method.startswith('manager_plugins.download.') or method == 'manager_plugins.providers.list':
        if name in {'plugin_id', 'provider_id'}:
            return {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9_.-]{0,99}$'}
        if name == 'source':
            return {'type': 'string', 'minLength': 1, 'maxLength': 16384}
        if name == 'revision':
            return {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    if name == 'installer_revision':
        return {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    if method.startswith('manager_plugins.metadata.'):
        if name == 'plugin_id':
            return {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9_.-]{0,99}$'}
        if name == 'mods':
            return {'type': 'array', 'maxItems': 4096, 'uniqueItems': True, 'items': {'type': 'string'}}
        if name == 'revision':
            return {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
        if name == 'changes':
            record = {'type': ['object', 'null'], 'required': ['value'], 'additionalProperties': False,
                      'properties': {'value': {'type': 'object'}, 'portable': {'type': 'boolean', 'default': False}}}
            return {'type': 'object', 'minProperties': 1, 'additionalProperties': False,
                    'properties': {'profile': record, 'mods': {'type': 'object', 'maxProperties': 4096, 'additionalProperties': record}}}
    if method == 'settings.set.start' and name == 'changes':
        from .application_settings import _specs
        return {'type': 'object', 'minProperties': 1, 'additionalProperties': False,
                'properties': {key: spec['schema'] for key, spec in _specs().items()}}
    if method == 'nexus.forwarding.set.start' and name == 'handler':
        return {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 4096}
    if method.startswith('settings.') and name == 'revision':
        return {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    if method == 'loverslab.credentials.set.start':
        return {'type': 'string', 'minLength': 1, 'maxLength': 512 if name == 'email' else 4096}
    if method == 'thunderstore.packages.search.start':
        if name == 'include_deprecated':
            return {'type': 'boolean'}
        if name == 'page':
            return {'type': 'integer', 'minimum': 0}
        if name in {'included_categories', 'excluded_categories'}:
            return {'type': 'array', 'items': {'type': 'string'}}
    if method == 'mods.files.edit.start':
        if name == 'reset':
            return {'type': 'boolean'}
        if name == 'strip_prefixes':
            return {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4096, 'uniqueItems': True}
        if name == 'changes':
            return {'type': 'array', 'minItems': 1, 'maxItems': 4096, 'items': {
                'type': 'object', 'required': ['path'], 'additionalProperties': False,
                'properties': {'path': {'type': 'string'}, 'enabled': {'type': 'boolean'}, 'root': {'type': 'boolean'}},
                'anyOf': [{'required': ['enabled']}, {'required': ['root']}],
            }}
    if method.startswith('profiles.backups.') and name == 'kept':
        return {'type': 'boolean'}
    if method.startswith('profiles.export.'):
        if name == 'changes':
            return {'type': 'array', 'maxItems': 4096, 'items': {'type': 'object'}}
        if name == 'settings':
            return {'type': 'string', 'default': 'latest'}
    if method in {'mods.set_enabled.start', 'plugins.set_enabled.start'} and name == 'changes':
        return {'type': 'array', 'items': {
            'type': 'object', 'required': ['name', 'enabled'], 'additionalProperties': False,
            'properties': {'name': {'type': 'string'}, 'enabled': {'type': 'boolean'}},
        }}
    if name in _DYNAMIC:
        return {'description': 'The accepted shape depends on the selected operation or game.'}
    if name in {'revision', 'target_revision'}:
        return {'type': ['string', 'integer', 'null']}
    if method == 'plugins.userlist.edit.start' and name in {'after', 'before'}:
        return {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4096}
    if name in _BOOL:
        return {'type': 'boolean'}
    if name in _INTEGER:
        result = {'type': 'integer'}
        if name in {'after', 'offset', 'version_index', 'wait_ms'}:
            result['minimum'] = 0
        if name == 'wait_ms':
            result['maximum'] = 30000
        if name == 'protocol_version':
            result['type'] = ['integer', 'null']
        return result
    if name in _ARRAY:
        return {'type': 'array'}
    if name in _OBJECT:
        return {'type': ['object', 'null']} if name == 'client' else {'type': 'object'}
    if name in {'prefix_path', 'staging_path', 'save_path_override', 'appimage_path', 'launcher_id',
                'deploy_mode', 'runtime_mode'}:
        return {'type': ['string', 'null']}
    if name == 'installer_policy':
        return {'enum': ['reject', 'defaults', 'verbatim'], 'default': 'reject'}
    if name == 'on_error':
        return {'enum': ['stop', 'continue'], 'default': 'stop'}
    return {'type': 'string'}


def _params(method):
    from .batch import METHODS
    from .service import SNAPSHOT_METHODS

    required, optional, _ = _CONTRACTS[method]
    names = (required + ' ' + optional).split()
    schema = {'type': 'object', 'properties': {name: _parameter(method, name) for name in names},
              'required': required.split(), 'additionalProperties': False}
    if method in {'downloads.install.start', 'downloads.inspect.start', 'installers.sessions.open.start'}:
        schema['oneOf'] = [{'required': ['archive_id']}, {'required': ['archive_path']}]
    if method in SNAPSHOT_METHODS:
        schema['properties']['allow_cached'] = {'type': 'boolean', 'default': True}
    if method in METHODS:
        child = _params(METHODS[method])
        if 'profile' in child['required']:
            schema['required'].append('profile')
        child['required'] = [key for key in child['required'] if key not in {'game', 'profile'}]
        for key in ('game', 'profile'):
            child['properties'].pop(key, None)
        schema['properties']['items'] = {'type': 'array', 'minItems': 1, 'maxItems': 256, 'items': child}
    if method == 'plugins.userlist.edit.start':
        from .plugin_userlist import ACTIONS
        common = {'game', 'profile', 'revision', 'action'}
        schema['oneOf'] = []
        for action, (needed, allowed) in ACTIONS.items():
            names = common | needed | allowed
            schema['oneOf'].append({
                'properties': {**{name: {} for name in names}, 'action': {'const': action}},
                'required': sorted(common | needed), 'additionalProperties': False,
            })
    if method == 'mods.version.change.start':
        schema['oneOf'] = [
            {'properties': {'source': {'const': 'thunderstore'}}, 'required': ['source', 'version']},
            {'properties': {'source': {'enum': ['nexus', 'modio']}}, 'required': ['file_id']},
            {'properties': {'source': {'pattern': '^plugin:[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}$'}},
             'required': ['source', 'version', 'catalogue_revision']},
        ]
    if method == 'collections.import.start':
        schema['oneOf'] = [{'required': ['path']}, {'required': ['code']}]
    return schema


def _result(method):
    schema = {'$schema': DIALECT, 'type': 'object'}
    job_methods = {'jobs.get', 'jobs.cancel', 'jobs.pause', 'jobs.resume', 'jobs.history.get'}
    if (method.endswith('.start') and method != 'nexus.auth.login.start') or method in job_methods:
        schema.update(required=['job'], properties={'job': {'$ref': '#/$defs/job'}},
                      **{'$defs': {'job': deepcopy(_JOB)}})
        if method == 'nexus.download.manual.start':
            schema['required'].append('browser_url')
            schema['properties']['browser_url'] = {'type': 'string', 'format': 'uri'}
    elif method == 'jobs.list':
        schema.update(required=['jobs'], properties={'jobs': {'type': 'array', 'items': {'$ref': '#/$defs/job'}}},
                      **{'$defs': {'job': deepcopy(_JOB)}})
    elif method in {'jobs.history.list', 'jobs.recovery.list'}:
        schema.update(required=['records', 'total', 'offset', 'limit'], properties={
            'records': {'type': 'array', 'items': {'type': 'object', 'required': ['job'],
                        'properties': {'job': {'$ref': '#/$defs/job'}}}},
            'total': {'type': 'integer'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'},
        }, **{'$defs': {'job': deepcopy(_JOB)}})
    elif method in {'jobs.events', 'jobs.history.events'}:
        schema.update(required=['job_id', 'events', 'next_sequence', 'complete'], properties={
            'job_id': {'type': 'string'}, 'events': {'type': 'array', 'items': {'type': 'object'}},
            'next_sequence': {'type': 'integer'}, 'oldest_sequence': {'type': 'integer'},
            'truncated': {'type': 'boolean'}, 'complete': {'type': 'boolean'},
        })
    elif method in {'launch.get', 'launch.stop'}:
        schema.update(required=['launch'], properties={'launch': {'type': ['object', 'null']}})
    else:
        roots = {
            'system.handshake': {'protocol': 'object', 'application': 'object', 'capabilities': 'object'},
            'system.ping': {'ok': 'boolean', 'protocol_version': 'integer', 'application_version': 'string'},
            'system.schema.get': {'schema_version': 'integer', 'methods': 'array'},
            'games.list': {'games': 'array'}, 'games.get': {'game': 'object'},
            'installers.sessions.get': {'session': 'object'},
            'installers.sessions.saved.list': {'sessions': 'array'},
            'installers.requests.get': {'requests': 'array'},
            'installers.requests.update': {'request': 'object'},
            'installers.requests.submit': {'accepted': 'boolean', 'request_id': 'string'},
            'installers.sessions.update': {'session': 'object'},
            'installers.sessions.image.get': {'image': 'object'},
            'installers.sessions.close': {'closed': 'boolean', 'session_id': 'string'},
            'games.custom.schema.get': {'definition_schema': 'object'},
            'games.custom.templates.list': {'templates': 'array'},
            'games.custom.list': {'games': 'array'},
            'games.custom.get': {'definition': 'object', 'revision': 'string', 'source_key': 'string'},
            'games.custom.validate': {'definition': 'object'},
            'games.custom.code.import': {'definition': 'object'},
            'games.custom.code.export': {'code': 'string'},
            'games.custom.save.plan': {'definition': 'object', 'revision': 'string', 'changes': 'object'},
            'games.custom.remove.plan': {'definition': 'object', 'revision': 'string'},
            'appearance.themes.list': {'themes': 'array'},
            'appearance.get': {'theme_id': 'string'},
            'appearance.set': {'theme_id': 'string'},
            'settings.get': {'revision': 'string', 'settings': 'object', 'definitions': 'object',
                             'shortcut_defaults': 'object', 'effective_shortcuts': 'object', 'dismissed_notices': 'integer'},
            'nexus.forwarding.get': {'revision': 'string', 'routes': 'object'},
            'nexus.forwarding.handlers.list': {'handlers': 'array'},
            'loverslab.credentials.get': {'credentials': 'object'},
            'profiles.list': {'profiles': 'array'}, 'mods.list': {'mods': 'array'},
            'manager_plugins.metadata.get': {'game': 'object', 'profile': 'string', 'plugin_id': 'string',
                                             'revision': 'string', 'metadata': ['object', 'null'], 'mods': 'object'},
            'launch.active.list': {'launches': 'array'},
        }.get(method, {})
        if roots:
            schema.update(required=list(roots), properties={key: {'type': value} for key, value in roots.items()})
    return schema


def catalogue(method=None):
    selected = [method] if method is not None else list(REGISTRY)
    records = []
    for name in selected:
        params = {'$schema': DIALECT, **_params(name)}
        records.append({
            'name': name, 'description': _CONTRACTS[name][2],
            'mutating': REGISTRY[name].mutating,
            'params': params, 'result': _result(name), 'error': deepcopy(_ERROR),
            'validation_scope': 'parameter_names_and_common_types',
        })
    return {'schema_version': 1, 'dialect': DIALECT, 'methods': records}
