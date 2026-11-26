"""Проверка переключения прав в настоящем JavaScript диспетчерской."""
import pathlib
import re
import shutil
import subprocess

import pytest


def test_role_ui_permissions_and_logout():
    if not shutil.which('node'):
        pytest.skip('Node.js недоступен')
    html = (pathlib.Path(__file__).parents[1] / 'dashboard/static/index.html').read_text()
    script = re.search(r'<script>(.*?)</script>', html, re.S).group(1)
    harness = r'''
const assert = require('assert');
const nodes = new Map();
const permissions = ['manage_profiles', 'delete_data', 'control_pipeline'].map(permission => ({dataset: {permission}}));
let closed = false;
global.localStorage = {getItem: () => '', removeItem: () => {}, setItem: () => {}};
global.document = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {innerHTML: '', value: '', addEventListener() {}});
    return nodes.get(id);
  },
  querySelectorAll(selector) {
    if (selector === '[data-permission]') return permissions;
    if (selector === 'dialog[open]') return [{close() {closed = true;}}];
    return [];
  }
};
'''
    checks = r'''
IDENTITY = {role: 'it', role_label: 'ИТ-специалист', permissions: {
  manage_profiles: false, delete_data: false, control_pipeline: false
}};
applyPermissions();
assert.deepStrictEqual(permissions.map(x => x.hidden), [true, true, true]);
assert.strictEqual(el('pf-text').readOnly, true);
renderProfileEditor({exists: true, valid: true, errors: [], domain: 'example.org', text: 'domain: example.org', draft: {exists: true}});
assert.ok(el('profile-editor').innerHTML.includes('data-permission="manage_profiles"'));
assert.strictEqual(el('pf-text').readOnly, true);
assert.ok(el('profile-editor').innerHTML.includes('Профиль доступен только для чтения'));
assert.ok(el('profile-editor').innerHTML.includes('data-permission="manage_profiles" class="ghost" onclick="checkProfile()"'));
IDENTITY = {role: 'admin', role_label: 'Администратор', permissions: {
  manage_profiles: true, delete_data: true, control_pipeline: true
}};
applyPermissions();
assert.deepStrictEqual(permissions.map(x => x.hidden), [false, false, false]);
assert.strictEqual(el('pf-text').readOnly, false);
el('card-body').innerHTML = 'старый редактор';
PROFILE_CTX = {domain: 'example.org'};
logout();
assert.strictEqual(IDENTITY, null);
assert.strictEqual(PROFILE_CTX, null);
assert.strictEqual(el('card-body').innerHTML, '');
assert.strictEqual(el('app').hidden, true);
assert.strictEqual(el('pf-text').readOnly, true);
assert.ok(closed);
assert.deepStrictEqual(permissions.map(x => x.hidden), [true, true, true]);
(async () => {
  const pending = [];
  global.fetch = () => new Promise(resolve => pending.push(resolve));
  let boots = 0;
  boot = () => { boots++; };
  el('token-input').value = 'admin-token';
  const first = login();
  el('token-input').value = 'it-token';
  const second = login();
  pending[1]({status: 200, ok: true, text: async () => JSON.stringify({role: 'it', role_label: 'ИТ-специалист', permissions: {}})});
  await second;
  pending[0]({status: 200, ok: true, text: async () => JSON.stringify({role: 'admin', permissions: {manage_profiles: true}})});
  await first;
  assert.strictEqual(TOKEN, 'it-token');
  assert.strictEqual(IDENTITY.role, 'it');
  assert.strictEqual(boots, 1);
  assert.strictEqual(el('login-msg').innerHTML, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', harness + script + checks], check=True, capture_output=True, text=True)
