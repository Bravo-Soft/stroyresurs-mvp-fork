"""Права диспетчерской проверяются настоящими HTTP-запросами без сети."""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


@pytest.fixture
def dashboard(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'dashboard'))
    spec = importlib.util.spec_from_file_location('dashboard_roles_app', root / 'dashboard/app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('DASHBOARD_TOKEN', 'admin-secret')
    monkeypatch.setenv('DASHBOARD_IT_TOKEN', 'it-secret')
    return module


def request(dashboard, method, path, token='it-secret', **kwargs):
    async def send():
        headers = {} if token is None else {b'authorization': ('Bearer ' + token).encode('utf-8')}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app),
                                    base_url='http://test') as client:
            return await client.request(method, '/api/v1' + path, headers=headers, **kwargs)
    return asyncio.run(send())


@pytest.mark.parametrize('token,role,manage', [('admin-secret', 'admin', True),
                                              ('it-secret', 'it', False)])
def test_identity_permissions(dashboard, token, role, manage):
    response = request(dashboard, 'GET', '/auth/me', token)
    assert response.status_code == 200
    identity = response.json()
    assert identity['role'] == role
    assert identity['role_label']
    assert identity['permissions'] == {'manage_profiles': manage, 'delete_data': manage,
                                        'control_pipeline': manage}
    assert token not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('token', [None, '', 'wrong', 'admin-secre', 'it-secret-extra'])
def test_invalid_credentials(dashboard, token):
    assert request(dashboard, 'GET', '/auth/me', token).status_code == 401


@pytest.mark.parametrize('admin,it', [('', 'it-secret'), ('same', 'same'), ('секрет', 'секрет')])
def test_bad_configuration_fails_closed(dashboard, monkeypatch, admin, it):
    monkeypatch.setenv('DASHBOARD_TOKEN', admin)
    monkeypatch.setenv('DASHBOARD_IT_TOKEN', it)
    for token in (None, admin, it, 'wrong'):
        assert request(dashboard, 'GET', '/auth/me', token).status_code == 503


def test_it_token_optional_and_utf8(dashboard, monkeypatch):
    monkeypatch.delenv('DASHBOARD_IT_TOKEN')
    assert request(dashboard, 'GET', '/auth/me', 'it-secret').status_code == 401
    monkeypatch.setenv('DASHBOARD_TOKEN', 'админ🔑')
    monkeypatch.setenv('DASHBOARD_IT_TOKEN', 'инженер🔑')
    for token, role in [('админ🔑', 'admin'), ('инженер🔑', 'it')]:
        assert dashboard.require_token('Bearer ' + token) == role


FORBIDDEN = [
    ('POST', '/companies/123/purge', {'sources': ['temp'], 'confirm_company_id': '123'}),
    ('POST', '/companies/123/purge/dry-run', {'sources': ['temp']}),
    ('POST', '/profiles/example.com/validate', {'text': 'domain: example.com'}),
    ('PUT', '/profiles/example.com', {'text': 'domain: example.com'}),
    ('DELETE', '/profiles/example.com', {'confirm_domain': 'example.com'}),
    ('POST', '/profiles/example.com/accept-draft', {}),
    ('POST', '/pipeline/start', {}),
    ('POST', '/pipeline/stop', {}),
    ('POST', '/pipeline/restart', {}),
    ('POST', '/companies/123/crawl', {}),
]


@pytest.mark.parametrize('method,path,payload', FORBIDDEN)
@pytest.mark.parametrize('malformed', [False, True])
def test_it_forbidden_operations_have_no_side_effects(dashboard, monkeypatch, method, path,
                                                      payload, malformed):
    guards = []
    for target, names in [(dashboard.profiles_store, ['read', 'validate', 'save', 'delete', 'accept_draft']),
                          (dashboard.purge_mod, ['dry_run', 'execute']),
                          (dashboard.companies_mod, ['find_in_site_list', 'last_result', 'company_payload', 'send_urgent_task']),
                          (dashboard.journal, ['append']),
                          (dashboard.supervisor, ['start', 'stop', 'restart_async', 'process_status'])]:
        for name in names:
            guard = Mock(side_effect=AssertionError('запрещённое действие вызвало ' + name))
            monkeypatch.setattr(target, name, guard)
            guards.append(guard)
    response = request(dashboard, method, path, json=[] if malformed else payload)
    assert response.status_code == 403, response.text
    for guard in guards:
        guard.assert_not_called()


@pytest.mark.parametrize('token', ['admin-secret', 'it-secret'])
@pytest.mark.parametrize('method,path,target,name,payload,status', [
    ('POST', '/bot/restart', 'bot_control', 'restart_async', None, 202),
    ('POST', '/pipeline/start', 'supervisor', 'start', {}, 200),
    ('POST', '/pipeline/stop', 'supervisor', 'stop', {}, 200),
    ('POST', '/pipeline/restart', 'supervisor', 'restart_async', {}, 200),
])
def test_pipeline_and_bot_operations_allowed(dashboard, monkeypatch, token, method, path,
                                            target, name, payload, status):
    action = Mock(return_value={'ok': True})
    monkeypatch.setattr(getattr(dashboard, target), name, action)
    response = request(dashboard, method, path, token, **({} if payload is None else {'json': payload}))
    expected = 403 if token == 'it-secret' and '/pipeline/' in path else status
    assert response.status_code == expected, response.text
    if expected == 403:
        action.assert_not_called()
    else:
        action.assert_called_once()


@pytest.mark.parametrize('token', ['admin-secret', 'it-secret'])
def test_company_crawl_allowed(dashboard, monkeypatch, token):
    monkeypatch.setattr(dashboard.companies_mod, 'company_payload', Mock(return_value={'original_name': 'Тест'}))
    monkeypatch.setattr(dashboard.supervisor, 'process_status', Mock(return_value={'running': True}))
    action = AsyncMock(return_value='task-1')
    monkeypatch.setattr(dashboard.companies_mod, 'send_urgent_task', action)
    monkeypatch.setattr(dashboard.journal, 'append', Mock())
    response = request(dashboard, 'POST', '/companies/123/crawl', token)
    assert response.status_code == (200 if token == 'admin-secret' else 403), response.text
    if token == 'admin-secret':
        action.assert_awaited_once()
    else:
        action.assert_not_awaited()


@pytest.mark.parametrize('token', ['admin-secret', 'it-secret'])
@pytest.mark.parametrize('name', ['DASHBOARD_TOKEN', 'DASHBOARD_IT_TOKEN'])
def test_settings_cannot_rotate_role_tokens(dashboard, monkeypatch, token, name):
    writes = Mock(side_effect=AssertionError('роль нельзя менять через настройки'))
    monkeypatch.setattr(dashboard.settings_store, 'read_overrides', Mock(return_value={}))
    monkeypatch.setattr(dashboard.settings_store, '_journal_view', Mock(return_value={}))
    monkeypatch.setattr(dashboard.settings_store, '_write_overrides', writes)
    response = request(dashboard, 'PUT', '/settings', token, json={'values': {name: 'changed'}})
    assert response.status_code == 422, response.text
    writes.assert_not_called()


@pytest.mark.parametrize('token', ['admin-secret', 'it-secret'])
def test_settings_updates_allowed(dashboard, monkeypatch, token):
    action = Mock(return_value={'LOG_LEVEL': {'before': 'INFO', 'after': 'DEBUG'}})
    monkeypatch.setattr(dashboard.settings_store, 'save', action)
    monkeypatch.setattr(dashboard.settings_store, 'read_all', Mock(return_value={'items': []}))
    monkeypatch.setattr(dashboard.journal, 'append', Mock())
    response = request(dashboard, 'PUT', '/settings', token, json={'values': {'LOG_LEVEL': 'DEBUG'}})
    assert response.status_code == 200, response.text
    action.assert_called_once_with({'LOG_LEVEL': 'DEBUG'}, issued_by='ui')


@pytest.mark.parametrize('path,target,name,result', [
    ('/status', 'supervisor', 'pipeline_state', {'running': False}),
    ('/settings', 'settings_store', 'read_all', {'items': []}),
    ('/profiles/example.com', 'profiles_store', 'read', {'exists': True}),
    ('/companies', 'companies_mod', 'read_site_list', []),
    ('/checkpoint', 'supervisor', 'checkpoint_info', {}),
    ('/bot/status', 'bot_control', 'status', {'running': False}),
])
def test_it_read_access(dashboard, monkeypatch, path, target, name, result):
    read = Mock(return_value=result)
    monkeypatch.setattr(getattr(dashboard, target), name, read)
    response = request(dashboard, 'GET', path)
    assert response.status_code == 200, response.text
    assert response.json() == result
    read.assert_called_once()


@pytest.mark.parametrize('method,path,payload', FORBIDDEN[:6])
def test_admin_profile_and_purge_operations(dashboard, monkeypatch, method, path, payload):
    monkeypatch.setattr(dashboard.journal, 'append', Mock())
    monkeypatch.setattr(dashboard.companies_mod, 'find_in_site_list', Mock(return_value={}))
    monkeypatch.setattr(dashboard.profiles_store, 'read', Mock(return_value={
        'exists': True, 'draft': {'exists': True}}))
    result = {'created': False, 'file': 'example.yaml', 'bytes': 10, 'version': 'v1',
              'changed_keys': [], 'confidence': 1, 'profile_version': 'v0', 'sources': {}}
    if '/purge' in path:
        target = dashboard.purge_mod
        name = 'dry_run' if path.endswith('/dry-run') else 'execute'
    else:
        target = dashboard.profiles_store
        name = {'PUT': 'save', 'DELETE': 'delete'}.get(method)
        name = name or ('validate' if path.endswith('/validate') else 'accept_draft')
    action = Mock(return_value=[] if name == 'validate' else result.copy())
    monkeypatch.setattr(target, name, action)
    response = request(dashboard, method, path, 'admin-secret', json=payload)
    assert response.status_code == 200, response.text
    action.assert_called_once()


def test_it_can_read_journal_and_bot_logs(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard.journal, 'read', Mock(return_value=[{'action': 'bot.restart'}]))
    monkeypatch.setattr(dashboard.metrics, 'log_tail', Mock(return_value={'lines': ['Готово']}))
    assert request(dashboard, 'GET', '/journal').json() == {'entries': [{'action': 'bot.restart'}]}
    for source in ('bot', 'bot-rebuild'):
        response = request(dashboard, 'GET', '/logs/tail?source=' + source)
        assert response.status_code == 200
        assert response.json() == {'lines': ['Готово']}
        dashboard.metrics.log_tail.assert_called_with(lines=200, source=source)


def test_new_write_route_is_admin_only_by_default(dashboard):
    action = Mock()

    @dashboard.api.post('/future-operation')
    async def future_operation():
        action()
        return {'ok': True}

    dashboard.app.include_router(dashboard.api)
    assert request(dashboard, 'POST', '/future-operation').status_code == 403
    action.assert_not_called()
    assert request(dashboard, 'POST', '/future-operation', 'admin-secret').status_code == 200
    action.assert_called_once()


@pytest.mark.parametrize('name', [
    'PIPELINE_CMD', 'LIBREOFFICE_PATH', 'WATCHDOG_AUTORESTART',
    'WATCHDOG_GRACE_SECONDS', 'COMPANY_IDLE_TIMEOUT_SECONDS',
    'LOGS_DIR', 'PROFILES_DIR', 'PROFILES_DRAFTS_DIR',
])
def test_it_cannot_bypass_permissions_through_settings(dashboard, monkeypatch, name):
    save = Mock(return_value={})
    monkeypatch.setattr(dashboard.settings_store, 'save', save)
    monkeypatch.setattr(dashboard.journal, 'append', Mock())
    monkeypatch.setattr(dashboard.settings_store, 'read_all', Mock(return_value={
        'items': [{'name': name, 'locked': False}, {'name': 'OLLAMA_MODEL', 'locked': False}]}))
    values = {name: 'changed', 'OLLAMA_MODEL': 'ordinary-setting'}
    # Смешанный запрос целиком отклоняется до записи даже разрешённого параметра.
    assert request(dashboard, 'PUT', '/settings', json={'values': values}).status_code == 403
    save.assert_not_called()
    it_settings = request(dashboard, 'GET', '/settings').json()['items']
    assert it_settings[0]['locked'] is True
    assert it_settings[1]['locked'] is False
    admin_settings = request(dashboard, 'GET', '/settings', 'admin-secret').json()['items']
    assert admin_settings[0]['locked'] is False
    response = request(dashboard, 'PUT', '/settings', 'admin-secret', json={'values': values})
    assert response.status_code == 200
    save.assert_called_once_with(values, issued_by='ui')
