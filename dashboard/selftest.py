# dashboard/selftest.py 1.0.0
# Сквозная проверка API Диспетчерской (спецификация §4) на живом сервисе.
#
# Проверяет контракт, а не «страница открылась»: каждый эндпоинт §4, коды ошибок
# из §9 (401 без токена, 409 на повторный старт, 404 на несуществующую компанию,
# 422 на некорректные данные) и fail-open поведение §2.
#
# Запуск (панель уже поднята):
#   py -3.10 dashboard/selftest.py --url http://127.0.0.1:5510 --token <DASHBOARD_TOKEN>
# Разрушающие проверки (старт/стоп эмулятора, очистка) — только с --write:
#   py -3.10 dashboard/selftest.py --token <...> --write
#
# Только стандартная библиотека.

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASSED = []
FAILED = []


def call(base, path, token, method='GET', body=None, timeout=30):
    """(код, данные). Сетевая ошибка -> код 0."""
    url = base.rstrip('/') + path
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8', errors='replace')
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, {'raw': raw[:200]}   # страница / отдаёт HTML
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {'detail': raw[:300]}
    except Exception as e:
        return 0, {'detail': f'{type(e).__name__}: {e}'}


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append((name, detail))
    print(f"  {'OK  ' if condition else 'СБОЙ'}  {name}" + (f"  — {detail}" if detail else ''))
    return condition


def expect(name, base, path, token, want, method='GET', body=None):
    code, data = call(base, path, token, method, body)
    ok = code == want
    detail = f'ожидался {want}, получен {code}'
    if not ok and isinstance(data, dict):
        detail += f": {str(data.get('detail'))[:160]}"
    check(name, ok, '' if ok else detail)
    return data if ok else None


def main():
    ap = argparse.ArgumentParser(description='Проверка API Диспетчерской')
    ap.add_argument('--url', default='http://127.0.0.1:5510')
    ap.add_argument('--token', required=True)
    ap.add_argument('--write', action='store_true',
                    help='включить проверки, меняющие состояние (старт/стоп пайплайна)')
    ap.add_argument('--purge-company', default=None,
                    help='id компании для проверки dry-run очистки (без удаления)')
    args = ap.parse_args()
    base, token = args.url, args.token

    print('\n=== 1. Авторизация (§9) ===')
    code, _ = call(base, '/api/v1/status', None)
    check('без заголовка Authorization -> 401', code == 401, f'получен {code}')
    code, _ = call(base, '/api/v1/status', 'wrong-token-000')
    check('с неверным токеном -> 401', code == 401, f'получен {code}')
    code, data = call(base, '/', None)
    check('страница / отдаётся без токена', code == 200, f'получен {code}')
    check('страница / — это HTML консоли',
          bool(data and '<!doctype html' in str(data.get('raw', '')).lower()))

    print('\n=== 2. Наблюдение (§4) ===')
    status = expect('GET /status', base, '/api/v1/status', token, 200)
    if status:
        check('status.state — одно из running/stopped/stalled',
              status.get('state') in ('running', 'stopped', 'stalled'),
              f"получено {status.get('state')}")
        check('status содержит process и heartbeat',
              'process' in status and 'heartbeat_age_seconds' in status)
    expect('GET /runs', base, '/api/v1/runs', token, 200)
    expect('GET /logs/tail', base, '/api/v1/logs/tail?lines=20', token, 200)
    summary = expect('GET /metrics/summary', base, '/api/v1/metrics/summary', token, 200)
    if summary is not None:
        check('metrics/summary работает и без прогонов (fail-open §2)',
              'available' in summary)
    expect('GET /metrics/companies', base, '/api/v1/metrics/companies', token, 200)
    expect('GET /errors', base, '/api/v1/errors', token, 200)
    expect('GET /checkpoint', base, '/api/v1/checkpoint', token, 200)
    expect('GET /journal', base, '/api/v1/journal?limit=5', token, 200)
    health = expect('GET /health', base, '/api/v1/health', token, 200)
    if health:
        check('health не падает при недоступных сервисах (§2)',
              set(health.get('services', {})) == {'graph_db', 'bot', 'kafka', 'llm'},
              str(list(health.get('services', {}))))
    expect('GET /runs/{кривой id} -> 400', base, '/api/v1/runs/not-a-run-id', token, 400)
    expect('GET /runs/{несуществующий} -> 404', base,
           '/api/v1/runs/run_19700101_000000', token, 404)

    print('\n=== 3. Ситлист и карточка компании (§3) ===')
    companies = expect('GET /companies', base, '/api/v1/companies', token, 200)
    sample_id = card = None
    if companies and companies.get('companies'):
        sample_id = companies['companies'][0]['company_id']
        card = expect(f'GET /companies/{sample_id}', base,
                      f'/api/v1/companies/{sample_id}', token, 200)
        if card:
            check('карточка содержит блоки graph и profile',
                  'graph' in card and 'profile' in card)
            check('недоступный backend не ломает карточку (§2)',
                  isinstance(card.get('graph'), dict) and 'available' in card['graph'])
    else:
        print('  ПРОПУСК  ситлист пуст или недоступен — карточка не проверяется')
    expect('GET /companies/{несуществующий} -> 404', base,
           '/api/v1/companies/no_such_company_00', token, 404)

    print('\n=== 3.1. Профили сайтов (§3) ===')
    domain = ((card or {}).get('profile') or {}).get('domain')
    if domain:
        check('карточка компании отдаёт domain и draft_exists профиля',
              'draft_exists' in card['profile'])
        profile = expect(f'GET /profiles/{domain}', base, f'/api/v1/profiles/{domain}', token, 200)
        if profile:
            check('профиль содержит text, version, valid, schema_hint, draft',
                  {'text', 'version', 'valid', 'schema_hint', 'draft'} <= set(profile))
        expect(f'POST /profiles/{domain}/validate с битым YAML -> 422', base,
               f'/api/v1/profiles/{domain}/validate', token, 422, 'POST',
               {'text': 'domain: [unclosed'})
    else:
        print('  ПРОПУСК  нет компании с доменом — профиль не проверяется')
    expect('GET /profiles/_schema -> 400 (служебный файл)', base,
           '/api/v1/profiles/_schema', token, 400)
    # «..%2F» отсекает сам роутер (404); до валидатора доходит «..» внутри сегмента
    expect('GET /profiles/a..b.ru -> 400 (обход пути)', base,
           '/api/v1/profiles/a..b.ru', token, 400)

    print('\n=== 4. Настройки (§6) ===')
    settings = expect('GET /settings', base, '/api/v1/settings', token, 200)
    if settings:
        items = settings.get('items', [])
        secrets_leaked = [i['name'] for i in items if i['type'] == 'secret' and i.get('value')]
        check('секреты не выводятся (§9)', not secrets_leaked, str(secrets_leaked))
        check('в белом списке есть параметры и он сгруппирован',
              len(items) > 10 and len({i['group'] for i in items}) >= 4,
              f"{len(items)} параметров, {len({i['group'] for i in items})} групп")
        check('есть блок «нет в этой версии»',
              isinstance(settings.get('not_in_this_build'), list))
    expect('PUT /settings с параметром вне списка -> 422', base, '/api/v1/settings', token,
           422, 'PUT', {'values': {'NO_SUCH_SETTING': '1'}})
    expect('PUT /settings с нечисловым int -> 422', base, '/api/v1/settings', token,
           422, 'PUT', {'values': {'MAX_CONCURRENT_PAGES': 'not-a-number'}})
    expect('PUT /settings упразднённого VECTOR_DB_ENABLE -> 422 (вне списка)', base,
           '/api/v1/settings', token, 422, 'PUT', {'values': {'VECTOR_DB_ENABLE': 'true'}})
    if settings:
        items = settings.get('items', [])
        check('у каждого параметра есть описание',
              all((i.get('note') or '').strip() for i in items),
              str([i['name'] for i in items if not (i.get('note') or '').strip()]))
        names = {i['name'] for i in items}
        check('AI_TUNNEL_* и VECTOR_DB_* убраны из белого списка',
              not [n for n in names if n.startswith(('AI_TUNNEL_', 'VECTOR_DB_'))])
        check('есть LLM_REQUEST_DELAY_SECONDS, MAX_PAGES_PER_SITE, MAX_PRODUCT_PAGES_PER_SITE',
              {'LLM_REQUEST_DELAY_SECONDS', 'MAX_PAGES_PER_SITE',
               'MAX_PRODUCT_PAGES_PER_SITE'} <= names)

    print('\n=== 5. Очистка: dry-run без удаления (§7) ===')
    target = args.purge_company or sample_id
    if target:
        dry = expect(f'POST /companies/{target}/purge/dry-run', base,
                     f'/api/v1/companies/{target}/purge/dry-run', token, 200, 'POST',
                     {'sources': ['temp', 'documents', 'graph']})
        if dry:
            check('dry-run вернул счётчики по источникам',
                  set(dry.get('sources', {})) == {'temp', 'documents', 'graph'})
            check('показано, что очистка не трогает (§7)',
                  bool((dry.get('protected') or {}).get('detail')))
            check('graph помечен как неподдерживаемый, пока нет эндпоинта backend',
                  (dry['sources']['graph'].get('supported') is False))
        expect('purge с неизвестным источником -> 422', base,
               f'/api/v1/companies/{target}/purge/dry-run', token, 422, 'POST',
               {'sources': ['everything']})
        expect('purge без подтверждения id -> 409', base,
               f'/api/v1/companies/{target}/purge', token, 409, 'POST',
               {'sources': ['temp'], 'confirm_company_id': 'не-тот-id'})
    else:
        print('  ПРОПУСК  нет компании для проверки очистки')
    expect('purge несуществующей компании -> 404', base,
           '/api/v1/companies/no_such_company_00/purge/dry-run', token, 404, 'POST',
           {'sources': ['temp']})

    print('\n=== 6. Управление (§4, §5) ===')
    running = bool((status or {}).get('process', {}).get('running'))
    expect('start с неизвестным режимом -> 422', base, '/api/v1/pipeline/start', token,
           422, 'POST', {'mode': 'no-such-mode'})
    if not args.write:
        print('  ПРОПУСК  разрушающие проверки выключены (добавьте --write)')
    elif running:
        print('  ПРОПУСК  пайплайн уже запущен — не вмешиваемся')
    else:
        expect('stop незапущенного -> 409', base, '/api/v1/pipeline/stop', token,
               409, 'POST', {'force': False})
        started = expect('POST /pipeline/start', base, '/api/v1/pipeline/start', token,
                         200, 'POST', {'mode': 'full', 'resume': True})
        if started:
            time.sleep(2)
            expect('повторный start -> 409 (singleton §5)', base, '/api/v1/pipeline/start',
                   token, 409, 'POST', {'mode': 'full', 'resume': True})
            state = call(base, '/api/v1/status', token)[1] or {}
            check('после старта статус running',
                  state.get('state') == 'running', str(state.get('state')))
            stopped = expect('POST /pipeline/stop (мягкая)', base, '/api/v1/pipeline/stop',
                             token, 200, 'POST', {'force': False})
            if stopped:
                deadline = time.time() + 60
                while time.time() < deadline:
                    if not (call(base, '/api/v1/status', token)[1] or {}) \
                            .get('process', {}).get('running'):
                        break
                    time.sleep(2)
                final = call(base, '/api/v1/status', token)[1] or {}
                if final.get('process', {}).get('running'):
                    call(base, '/api/v1/pipeline/stop', token, 'POST', {'force': True})
                    check('мягкая остановка отработала за 60 с', False,
                          'не завершился — процесс снят жёстко')
                else:
                    check('мягкая остановка завершила процесс', True)
        journal = call(base, '/api/v1/journal?limit=20', token)[1] or {}
        actions = [e['action'] for e in journal.get('entries', [])]
        check('действия записаны в журнал (§9)',
              'pipeline.start' in actions and 'pipeline.stop' in actions, str(actions[:6]))

    print('\n' + '=' * 62)
    print(f'ПРОЙДЕНО: {len(PASSED)}    СБОЕВ: {len(FAILED)}')
    if FAILED:
        print('\nСбои:')
        for name, detail in FAILED:
            print(f'  - {name}: {detail}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
