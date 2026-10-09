# dashboard/supervisor.py 1.0.0
# Супервизор пайплайна (спецификация «Диспетчерская» §5).
#
# Диспетчерская ВЛАДЕЕТ процессом пайплайна: main.py запускается её подпроцессом,
# а не руками через docker exec. Здесь же живут инварианты §5:
#   * ровно один экземпляр пайплайна (файловый lock + проверка живости PID);
#   * мягкая остановка — команда в logs/runs/control.json, оркестратор читает её
#     МЕЖДУ компаниями, снимает чекпоинт и выходит; жёсткая (force) — снятие дерева
#     процессов, всегда помечается в журнале;
#   * рестарт с resume=true продолжает с чекпоинта, а не с 1-й строки ситлиста;
#   * сторож по простою: нет прогресса дольше COMPANY_IDLE_TIMEOUT_SECONDS ->
#     статус «завис», алерт в Пачку и, если включён авторестарт, мягкий перезапуск.
#
# Только стандартная библиотека.

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import journal
import settings_store
from common import (MVP_DIR, atomic_write_json, cfg_value, logs_dir, read_json,
                    read_status, runs_dir)

log = logging.getLogger("dashboard.supervisor")

# Пороги по умолчанию
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800      # порог простоя пайплайна (config.company_idle_timeout_seconds)
DEFAULT_WATCHDOG_GRACE_SECONDS = 300     # запас панели сверх порога пайплайна
WATCHDOG_INTERVAL_SECONDS = 60           # период опроса heartbeat
_TRUE = ('true', '1', 'yes', 'on')

# Состояние подпроцесса, запущенного ЭТИМ экземпляром панели.
# После рестарта панели остаётся только PID-файл — процесс виден как managed=False.
# exit_handled — завершение этого popen уже отражено в журнале (защита от дублей).
_PIPELINE = {'popen': None, 'started_at': None, 'params': None, 'exit_handled': False}
_LOCK = threading.Lock()

# Состояние сторожа: не спамим алертами по одному и тому же зависанию
_WATCH = {'alerted_run': None, 'autorestarted_run': None, 'last_check': None,
          'last_event': None}


def _p(name):
    return os.path.join(runs_dir(), name)


def pid_file():
    return _p('pipeline.pid')


def lock_file():
    return _p('pipeline.lock')


def stdout_log():
    return _p('pipeline_stdout.log')


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _int_setting(attr, env_name, default):
    """Целое из окружения (attr=None) или из окружения/config.py (cfg_value)."""
    raw = os.environ.get(env_name) if attr is None else cfg_value(attr, env_name, default)
    try:
        return int(raw if raw not in (None, '') else default)
    except (TypeError, ValueError):
        return default


def idle_timeout_seconds():
    """Порог «завис» для панели.

    Пульс status.json (run_recorder) обновляется по записям в лог — ровно тем же
    признаком, по которому сторож D101 внутри пайплайна меряет простой компании
    (config.company_idle_timeout_seconds). Сторож пайплайна бросает зависшую компанию
    сам и пишет об этом в лог, поэтому панель ждёт тот же порог плюс запас
    WATCHDOG_GRACE_SECONDS: её статус «завис» означает зависание всего процесса
    (замёрзший event loop), а не одной компании.
    """
    base = _int_setting('company_idle_timeout_seconds', 'COMPANY_IDLE_TIMEOUT_SECONDS',
                        DEFAULT_IDLE_TIMEOUT_SECONDS)
    grace = _int_setting(None, 'WATCHDOG_GRACE_SECONDS', DEFAULT_WATCHDOG_GRACE_SECONDS)
    return max(60, base + max(0, grace))


# ------------------------------ живость процесса ------------------------------

def _read_pid():
    try:
        with open(pid_file(), 'r', encoding='utf-8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    """Жив ли процесс. На Windows — только WinAPI."""
    if pid is None:
        return False
    if os.name == 'nt':
        # ВАЖНО: os.kill(pid, 0) на Windows УБИВАЕТ процесс — проверка только через WinAPI
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid):
    """Жёсткая остановка: дерево процессов (пайплайн держит браузеры Playwright)."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)


# ------------------------------ singleton-lock --------------------------------

class AlreadyRunning(RuntimeError):
    """Экземпляр пайплайна уже работает (наружу — HTTP 409)."""


def _acquire_lock(pid_placeholder='starting'):
    """Занять файловый lock. Протухший lock мёртвого процесса переиспользуется."""
    path = lock_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = read_json(path) or {}
        holder_pid = holder.get('pid')
        if isinstance(holder_pid, int) and pid_alive(holder_pid):
            raise AlreadyRunning(f"Пайплайн уже запущен (PID {holder_pid})")
        log.warning(f"Найден протухший lock (PID {holder_pid}) — переиспользуется")
        os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        import json
        json.dump({'pid': pid_placeholder, 'acquired_at': datetime.now().isoformat()},
                  f, ensure_ascii=False)
    return path


def _write_lock_pid(pid):
    import json
    tmp = lock_file() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'pid': pid, 'acquired_at': datetime.now().isoformat()}, f,
                  ensure_ascii=False)
    os.replace(tmp, lock_file())


def _release_lock():
    try:
        os.remove(lock_file())
    except OSError:
        pass


# ------------------------------ статус процесса -------------------------------

def process_status():
    """Состояние процесса пайплайна: running / exit_code / кем управляется."""
    popen = _PIPELINE['popen']
    if popen is not None:
        exit_code = popen.poll()
        base = {'pid': popen.pid, 'managed': True,
                'started_at': _PIPELINE['started_at'],
                'params': _PIPELINE['params']}
        if exit_code is None:
            return {'running': True, **base}
        if not _PIPELINE['exit_handled']:
            _PIPELINE['exit_handled'] = True
            _release_lock()
            _on_process_exit(popen.pid, exit_code)
        return {'running': False, 'exit_code': exit_code, **base}
    pid = _read_pid()
    if pid is not None and pid_alive(pid):
        # процесс пережил рестарт панели: жив, но Popen-хендла у нас нет
        return {'running': True, 'pid': pid, 'managed': False}
    if pid is not None and _run_active():
        # процесс из PID-файла умер без нас (падение, перезагрузка хоста, kill извне):
        # прогон в status.json всё ещё «идёт» — финализируем; код выхода неизвестен
        _release_lock()
        _on_process_exit(pid, None)
    return {'running': False}


_ACTIVE_STATES = ('running', 'processing_regular', 'processing_urgent')


def _run_active():
    status = read_status()
    return bool(status and status.get('state') in _ACTIVE_STATES)


def _on_process_exit(pid, exit_code):
    """Процесс завершился не по команде панели.

    Единственное место, где панель наблюдает за процессом, — process_status(); его
    зовут и UI (раз в 10 с), и сторож (раз в 60 с), поэтому смерть замечается и без
    открытого экрана. Прогон, который run_recorder не успел закрыть, помечается
    прерванным (иначе он навсегда оставался бы running, а его компания — «в работе»),
    факт и код выхода уходят в журнал, о падении — алерт в Пачку.
    """
    aborted_run = _finalize_killed_run(aborted_by='pipeline-exited')
    crashed = aborted_run is not None or exit_code not in (None, 0)
    details = {'pid': pid, 'exit_code': exit_code, 'aborted_run': aborted_run}
    if crashed:
        text = (f"Стройресурс: пайплайн завершился сам (PID {pid}, код выхода {exit_code}).\n"
                f"Прогон {aborted_run or 'уже закрыт'} — подробности в pipeline_stdout.log.")
        alert_sent, alert_detail = (False, 'отключено')
        if _env_flag('WATCHDOG_ALERT_PACHCA', default=True):
            alert_sent, alert_detail = _pachca_alert(text)
        details.update(alert_sent=alert_sent, alert_detail=alert_detail)
        log.error(f"Пайплайн завершился сам: PID {pid}, код выхода {exit_code}, "
                  f"прогон {aborted_run} помечен прерванным (алерт: {alert_detail})")
    else:
        log.info(f"Пайплайн завершился: PID {pid}, код выхода {exit_code}")
    journal.append('pipeline.exited', source='supervisor',
                   result='crash' if crashed else 'ok', details=details)


def pipeline_state():
    """Сводное состояние для §4 GET /status: running | stopped | stalled."""
    proc = process_status()
    status = read_status()
    heartbeat_age = None
    if status and status.get('updated_at'):
        try:
            updated = datetime.fromisoformat(status['updated_at'])
            heartbeat_age = round((datetime.now() - updated).total_seconds(), 1)
        except (TypeError, ValueError):
            heartbeat_age = None

    active_states = ('running', 'processing_regular', 'processing_urgent')
    run_active = bool(status and status.get('state') in active_states)

    if not proc['running']:
        state = 'stopped'
    elif (run_active and heartbeat_age is not None
            and heartbeat_age > idle_timeout_seconds()):
        state = 'stalled'
    else:
        state = 'running'

    progress = None
    if status:
        total = status.get('companies_total')
        done = status.get('companies_done')
        if isinstance(total, int) and isinstance(done, int):
            progress = {'done': done, 'total': total,
                        'percent': round(done * 100.0 / total, 1) if total else None}

    return {
        'state': state,
        'process': proc,
        'run_id': (status or {}).get('run_id'),
        'run_state': (status or {}).get('state'),
        'current_company': (status or {}).get('current_company'),
        'kind': (status or {}).get('kind'),
        'progress': progress,
        'heartbeat_age_seconds': heartbeat_age,
        'idle_timeout_seconds': idle_timeout_seconds(),
        'watchdog': dict(_WATCH),
        'restart': dict(_RESTART),
        'checked_at': datetime.now().isoformat(),
    }


# ------------------------------ управление ------------------------------------

def _pipeline_argv(mode):
    """Что запускает кнопка «Старт».

    PIPELINE_CMD позволяет подменить main.py на эмулятор (dashboard/fake_pipeline.py)
    для репетиции сценариев стоп/рестарт/зависание без краула и LLM.
    """
    script = os.environ.get('PIPELINE_CMD', 'main.py').strip() or 'main.py'
    argv = [sys.executable]
    argv.extend(script.split())
    if mode == 'urgent-only':
        argv.append('--urgent-only')
    return argv


def start(mode='full', resume=True, company_ids=None, issued_by='ui'):
    """Запуск прогона. 409, если экземпляр уже работает (§4)."""
    if mode not in ('full', 'urgent-only'):
        raise ValueError(f"Неизвестный режим: {mode}")
    with _LOCK:
        state = process_status()
        if state['running']:
            raise AlreadyRunning(f"Пайплайн уже запущен (PID {state['pid']})")
        _acquire_lock()
        try:
            env = settings_store.env_for_subprocess()
            if company_ids:
                env['COMPANY_IDS'] = ','.join(company_ids)
            else:
                env.pop('COMPANY_IDS', None)
            env['PIPELINE_MODE'] = mode
            # resume=False -> начать прогон с начала списка, чекпоинт не восстанавливать
            if resume:
                env.pop('PIPELINE_IGNORE_CHECKPOINT', None)
            else:
                env['PIPELINE_IGNORE_CHECKPOINT'] = '1'

            os.makedirs(runs_dir(), exist_ok=True)
            # Устаревшая команда stop от прошлого прогона не должна убить новый
            _clear_control()

            argv = _pipeline_argv(mode)
            with open(stdout_log(), 'ab') as out:
                header = (f"\n===== ЗАПУСК {datetime.now().isoformat()} | режим {mode} | "
                          f"resume={resume} | компаний: "
                          f"{'все' if not company_ids else len(company_ids)} =====\n")
                out.write(header.encode('utf-8'))
                popen = subprocess.Popen(argv, cwd=MVP_DIR, env=env,
                                         stdout=out, stderr=subprocess.STDOUT)
            params = {'mode': mode, 'resume': resume, 'company_ids': company_ids,
                      'argv': argv[1:]}
            _PIPELINE.update(popen=popen, started_at=datetime.now().isoformat(),
                             params=params, exit_handled=False)
            _write_lock_pid(popen.pid)
            with open(pid_file(), 'w', encoding='utf-8') as f:
                f.write(str(popen.pid))
            _WATCH.update(alerted_run=None, autorestarted_run=None)
        except Exception:
            _release_lock()
            raise
    log.info(f"Пайплайн запущен: PID {popen.pid}, режим {mode}, resume={resume}")
    journal.append('pipeline.start', source=issued_by,
                   details={'pid': popen.pid, **params})
    return {'ok': True, 'pid': popen.pid, **params}


def _clear_control():
    try:
        import pipeline_control
        pipeline_control.clear_command(logs_dir())
    except Exception as e:
        log.warning(f"Не удалось очистить команду управления: {e}")


def _finalize_killed_run(aborted_by='dispatcher-force-stop'):
    """Пометить прогон прерванным после жёсткой остановки или смерти процесса.

    Процесс снят SIGKILL/taskkill либо умер сам — run_recorder не успел выполнить
    run_finished(), и прогон навсегда остался бы в состоянии running: экран «Журнал»
    показывал бы живым то, чего нет. Дописываем финализацию за него.
    """
    try:
        status = read_status()
        if not status or status.get('state') in ('finished', 'aborted'):
            return None
        run_id = status.get('run_id')
        now = datetime.now().isoformat()
        status.update(state='aborted', current_company=None, updated_at=now)
        atomic_write_json(os.path.join(runs_dir(), 'status.json'), status)
        if run_id:
            run_path = os.path.join(runs_dir(), run_id, 'run.json')
            run = read_json(run_path)
            if run and run.get('state') not in ('finished', 'aborted'):
                run.update(state='aborted', finished_at=now, updated_at=now,
                           aborted_by=aborted_by)
                atomic_write_json(run_path, run)
        return run_id
    except Exception as e:
        log.warning(f"Не удалось финализировать прерванный прогон: {e}")
        return None


class AlreadyStopped(RuntimeError):
    """Пайплайн не запущен (наружу — HTTP 409)."""


def stop(force=False, issued_by='ui'):
    """Остановка: мягкая (по умолчанию) или жёсткая. 409, если не запущен."""
    state = process_status()
    if not state['running']:
        raise AlreadyStopped('Пайплайн не запущен')
    if force:
        # завершение по нашей команде — не «смерть», pipeline.exited не пишем
        _PIPELINE['exit_handled'] = True
        _terminate_pid(state['pid'])
        _release_lock()
        aborted_run = _finalize_killed_run()
        log.info(f"Пайплайн остановлен жёстко: PID {state['pid']}")
        journal.append('pipeline.stop', source=issued_by, result='force',
                       details={'pid': state['pid'], 'force': True,
                                'aborted_run': aborted_run})
        return {'ok': True, 'force': True, 'pid': state['pid'],
                'aborted_run': aborted_run,
                'detail': 'Процесс завершён немедленно. '
                          'Незавершённая компания будет переобработана.'}
    import pipeline_control
    pipeline_control.write_command(logs_dir(), 'stop', issued_by=issued_by)
    log.info("Отправлена команда мягкой остановки (control.json)")
    journal.append('pipeline.stop', source=issued_by,
                   details={'pid': state['pid'], 'force': False})
    return {'ok': True, 'force': False, 'pid': state['pid'],
            'detail': 'Пайплайн остановится после текущей компании и сохранит чекпоинт.'}


def wait_stopped(timeout_seconds):
    """Дождаться завершения процесса. True — завершился, False — таймаут."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not process_status()['running']:
            return True
        time.sleep(1.0)
    return not process_status()['running']


def restart(mode='full', company_ids=None, wait_seconds=900, force_after_wait=False,
            issued_by='ui'):
    """stop + start с resume=true — продолжение с чекпоинта (§4, §5)."""
    state = process_status()
    stopped_details = None
    if state['running']:
        stopped_details = stop(force=False, issued_by=issued_by)
        if not wait_stopped(wait_seconds):
            if not force_after_wait:
                raise TimeoutError(
                    f"Пайплайн не завершился за {wait_seconds} с — он дозавершает компанию. "
                    f"Повторите позже или используйте жёсткую остановку.")
            stop(force=True, issued_by=issued_by)
            wait_stopped(30)
    started = start(mode=mode, resume=True, company_ids=company_ids, issued_by=issued_by)
    journal.append('pipeline.restart', source=issued_by,
                   details={'stopped': bool(stopped_details), 'started_pid': started['pid'],
                            'mode': mode})
    return {'ok': True, 'stopped': stopped_details, 'started': started}


# Фоновый перезапуск: мягкая остановка дозавершает текущую компанию (это могут быть
# десятки минут), поэтому HTTP-запрос не ждёт — ход виден в GET /status.
_RESTART = {'pending': False, 'requested_at': None, 'params': None,
            'finished_at': None, 'error': None, 'started_pid': None}
_RESTART_MAX_WAIT_SECONDS = 6 * 3600


def restart_state():
    return dict(_RESTART)


def restart_async(mode='full', company_ids=None, issued_by='ui'):
    """Поставить перезапуск в фон: stop(soft) -> ожидание -> start(resume=True)."""
    if mode not in ('full', 'urgent-only'):
        raise ValueError(f"Неизвестный режим: {mode}")
    if _RESTART['pending']:
        raise AlreadyRunning('Перезапуск уже выполняется')
    if not process_status()['running']:
        # нечего останавливать — просто стартуем
        started = start(mode=mode, resume=True, company_ids=company_ids, issued_by=issued_by)
        journal.append('pipeline.restart', source=issued_by,
                       details={'stopped': False, 'started_pid': started['pid'], 'mode': mode})
        return {'ok': True, 'pending': False, 'started': started}

    params = {'mode': mode, 'company_ids': company_ids}
    _RESTART.update(pending=True, requested_at=datetime.now().isoformat(),
                    params=params, finished_at=None, error=None, started_pid=None)

    def _worker():
        try:
            stop(force=False, issued_by=issued_by)
            if not wait_stopped(_RESTART_MAX_WAIT_SECONDS):
                raise TimeoutError('Пайплайн не завершился за отведённое время')
            started = start(mode=mode, resume=True, company_ids=company_ids,
                            issued_by=issued_by)
            _RESTART['started_pid'] = started['pid']
            journal.append('pipeline.restart', source=issued_by,
                           details={'stopped': True, 'started_pid': started['pid'],
                                    'mode': mode})
        except Exception as e:
            _RESTART['error'] = str(e)
            log.error(f"Перезапуск не удался: {e}")
            journal.append('pipeline.restart', source=issued_by, result='error',
                           details={'error': str(e), 'mode': mode})
        finally:
            _RESTART.update(pending=False, finished_at=datetime.now().isoformat())

    threading.Thread(target=_worker, name='dispatcher-restart', daemon=True).start()
    return {'ok': True, 'pending': True,
            'detail': ('Отправлена мягкая остановка. Пайплайн дозавершит текущую компанию, '
                       'после чего запустится заново с чекпоинта. '
                       'Ход перезапуска — в GET /status.')}


def checkpoint_info():
    """С какого места продолжится рестарт (Base/processing_checkpoint.json)."""
    base_dir = cfg_value('base_dir', 'BASE_DIR')
    name = cfg_value('checkpoint_file', 'CHECKPOINT_FILE', 'processing_checkpoint.json')
    if not base_dir:
        return {'exists': False, 'detail': 'base_dir неизвестен (нет config.py и env BASE_DIR)'}
    data = read_json(os.path.join(base_dir, name))
    if not data:
        return {'exists': False}
    return {'exists': True,
            'company': (data.get('current_company') or {}).get('original_name'),
            'company_id': (data.get('current_company') or {}).get('company_id'),
            'stage': data.get('current_stage'),
            'timestamp': data.get('timestamp'),
            'progress': data.get('progress'),
            'processed_urls_count': len(data.get('processed_urls') or [])}


# ------------------------------ сторож по простою -----------------------------

def _pachca_alert(text):
    """Алерт в Пачку через её API. Только stdlib; отсутствие токена — не ошибка."""
    token = os.environ.get('PACHKA_ACCESS_TOKEN', '').strip()
    admin_id = os.environ.get('PACHKA_ADMIN_ID', '').strip()
    if not token or not admin_id:
        return False, 'PACHKA_ACCESS_TOKEN / PACHKA_ADMIN_ID не заданы'
    import json as _json
    payload = _json.dumps({'message': {'entity_type': 'discussion',
                                       'entity_id': int(admin_id),
                                       'content': text}}).encode('utf-8')
    request = urllib.request.Request(
        'https://api.pachca.com/api/shared/v1/messages', data=payload,
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return True, f'HTTP {response.status}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)


def watchdog_tick():
    """Один проход сторожа. Возвращает описание сработки или None."""
    _WATCH['last_check'] = datetime.now().isoformat()
    try:
        state = pipeline_state()
    except Exception as e:
        log.warning(f"Сторож: не удалось получить состояние ({e})")
        return None
    if state['state'] != 'stalled':
        return None

    run_id = state.get('run_id')
    if _WATCH['alerted_run'] == run_id:
        return None  # об этом зависании уже сообщили

    company = (state.get('current_company') or {}).get('name') or 'неизвестна'
    age_minutes = round((state.get('heartbeat_age_seconds') or 0) / 60)
    text = (f"Стройресурс: пайплайн завис.\n"
            f"Прогон {run_id}, компания «{company}», "
            f"нет прогресса {age_minutes} мин "
            f"(порог {idle_timeout_seconds() // 60} мин).")
    _WATCH['alerted_run'] = run_id

    alert_sent, alert_detail = (False, 'отключено')
    if _env_flag('WATCHDOG_ALERT_PACHCA', default=True):
        alert_sent, alert_detail = _pachca_alert(text)
    log.warning(f"Сторож: зависание прогона {run_id} (алерт: {alert_detail})")

    event = {'ts': datetime.now().isoformat(), 'run_id': run_id,
             'company': company, 'heartbeat_age_seconds': state.get('heartbeat_age_seconds'),
             'alert_sent': alert_sent, 'alert_detail': alert_detail,
             'autorestarted': False}

    if _env_flag('WATCHDOG_AUTORESTART') and _WATCH['autorestarted_run'] != run_id:
        _WATCH['autorestarted_run'] = run_id
        try:
            restart(mode='full', wait_seconds=120, force_after_wait=True,
                    issued_by='watchdog')
            event['autorestarted'] = True
        except Exception as e:
            event['autorestart_error'] = str(e)
            log.error(f"Сторож: авторестарт не удался: {e}")

    _WATCH['last_event'] = event
    journal.append('watchdog.stalled', source='watchdog', details=event,
                   result='autorestart' if event['autorestarted'] else 'alert')
    return event
