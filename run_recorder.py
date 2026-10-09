"""Файловая телеметрия одного запуска пайплайна для Диспетчерской.

Регистратор не зависит от FastAPI и тяжёлых модулей краулера. Он пишет только
атомарные JSON/JSONL-файлы в ``logs/runs``; при ошибке телеметрии сам пайплайн
продолжает работу.
"""

import json
import logging
import os
import statistics
import threading
import time
from datetime import datetime


MODULES = ('crawler', 'text', 'ai', 'cards', 'files', 'pdf', 'graph')


def _now():
    return datetime.now().isoformat(timespec='seconds')


class _LogHeartbeat(logging.Handler):
    def __init__(self, recorder):
        super().__init__(level=logging.INFO)
        self.recorder = recorder

    def emit(self, record):
        if record.name.startswith('run_recorder'):
            return
        self.recorder.heartbeat()


class RunRecorder:
    """Записывает состояние прогона и результаты компаний.

    Обновление status.json от логов ограничено разом в две секунды, чтобы
    подробное логирование краулера не превращалось в тысячи операций rename.
    """

    HEARTBEAT_WRITE_SECONDS = 2.0

    def __init__(self, logs_dir, run_id=None):
        if not logs_dir:
            raise ValueError('Не задан logs_dir для регистратора прогона')
        self.logs_dir = logs_dir
        self.runs_dir = os.path.join(logs_dir, 'runs')
        self.run_id = run_id or 'run_' + datetime.now().strftime('%Y%m%d_%H%M%S')
        self.run_dir = os.path.join(self.runs_dir, self.run_id)
        self.status_path = os.path.join(self.runs_dir, 'status.json')
        self.run_path = os.path.join(self.run_dir, 'run.json')
        self.companies_path = os.path.join(self.run_dir, 'companies.jsonl')
        self._lock = threading.RLock()
        self._status = None
        self._run = None
        self._company_started = {}
        self._last_heartbeat_write = 0.0
        self._handler = None

    @property
    def started(self):
        return self._run is not None

    @staticmethod
    def _write_json(path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def _flush(self):
        if self._status is not None:
            self._write_json(self.status_path, self._status)
        if self._run is not None:
            self._write_json(self.run_path, self._run)

    def start(self, mode, total_companies=None, selected_company_ids=None, resume=True):
        with self._lock:
            os.makedirs(self.run_dir, exist_ok=True)
            timestamp = _now()
            self._status = {
                'run_id': self.run_id,
                'state': 'running',
                'mode': mode,
                'started_at': timestamp,
                'updated_at': timestamp,
                'current_company': None,
                'companies': {'total': total_companies, 'done': 0, 'success': 0, 'failed': 0},
            }
            self._run = {
                'run_id': self.run_id,
                'state': 'running',
                'mode': mode,
                'resume': bool(resume),
                'selected_company_ids': selected_company_ids,
                'started_at': timestamp,
                'updated_at': timestamp,
                'companies': dict(self._status['companies']),
                'pages': {},
                'tokens': {},
                'cost': {},
                'perf': {'modules': {}},
            }
            self._flush()
        return self.run_id

    def install_heartbeat(self):
        """Связать heartbeat с корневым логгером после настройки logging."""
        if self._handler is None:
            self._handler = _LogHeartbeat(self)
            logging.getLogger().addHandler(self._handler)

    def uninstall_heartbeat(self):
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

    def heartbeat(self, force=False):
        with self._lock:
            if not self._status or self._status.get('state') != 'running':
                return
            current = time.monotonic()
            if not force and current - self._last_heartbeat_write < self.HEARTBEAT_WRITE_SECONDS:
                return
            self._last_heartbeat_write = current
            self._status['updated_at'] = _now()
            self._write_json(self.status_path, self._status)

    def company_started(self, company, index=None, total=None, kind='regular'):
        with self._lock:
            if not self._status:
                return
            company_id = str((company or {}).get('company_id') or '')
            started = time.monotonic()
            self._company_started[company_id] = started
            current = {
                'company_id': company_id,
                'name': (company or {}).get('original_name'),
                'website': (company or {}).get('website'),
                'kind': kind,
                'index': index,
                'total': total,
                'started_at': _now(),
            }
            self._status.update(state='processing_' + kind, current_company=current, updated_at=_now())
            if total is not None:
                self._status['companies']['total'] = total
                self._run['companies']['total'] = total
            self._flush()

    @staticmethod
    def _company_statistics(result):
        value = (result or {}).get('company_statistics') or {}
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _modules(tracker, company_id):
        if tracker is None or not hasattr(tracker, 'company_summary'):
            return {}
        result = {}
        for module in MODULES:
            try:
                summary = tracker.company_summary(company_id, module)
            except Exception:
                continue
            if summary.get('count') or summary.get('retry'):
                result[module] = {key: round(value, 3) if isinstance(value, float) else value
                                  for key, value in summary.items()}
        return result

    def company_finished(self, company, result, tracker=None, kind='regular'):
        with self._lock:
            if not self._status:
                return
            company = company or {}
            company_id = str(company.get('company_id') or '')
            started_at = self._company_started.pop(company_id, None)
            duration = (round(time.monotonic() - started_at, 3)
                        if started_at is not None else None)
            statistics_data = self._company_statistics(result)
            errors = list((result or {}).get('errors') or statistics_data.get('errors') or [])
            status = (result or {}).get('status') or 'error'
            record = {
                'ts': _now(),
                'company_id': company_id,
                'name': company.get('original_name'),
                'website': company.get('website'),
                'kind': kind,
                'status': status,
                'duration_seconds': duration,
                'statistics': statistics_data,
                'modules': self._modules(tracker, company_id),
                'errors': errors,
            }
            with open(self.companies_path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + '\n')
            counts = self._status['companies']
            counts['done'] = int(counts.get('done') or 0) + 1
            if status == 'success':
                counts['success'] = int(counts.get('success') or 0) + 1
            else:
                counts['failed'] = int(counts.get('failed') or 0) + 1
            self._run['companies'] = dict(counts)
            self._status.update(state='running', current_company=None, updated_at=_now())
            self._run['updated_at'] = self._status['updated_at']
            self._flush()
            return record

    @staticmethod
    def _getattr_number(value, name):
        value = getattr(value, name, 0) if value is not None else 0
        return value if isinstance(value, (int, float)) else 0

    def finish(self, statistics_data=None, tracker=None, aborted=False, aborted_by=None):
        with self._lock:
            if not self._status or not self._run:
                return
            state = 'aborted' if aborted else 'finished'
            timestamp = _now()
            stats = statistics_data
            self._run['pages'] = {
                'total': self._getattr_number(stats, 'pages_processed'),
                'product': self._getattr_number(stats, 'product_pages_processed'),
                'company': self._getattr_number(stats, 'company_pages_processed'),
                'distributor': self._getattr_number(stats, 'distributor_pages_processed'),
            }
            self._run['tokens'] = {
                'input': self._getattr_number(stats, 'total_input_tokens'),
                'output': self._getattr_number(stats, 'total_output_tokens'),
                'embedding': self._getattr_number(stats, 'total_embedding_tokens'),
            }
            self._run['cost'] = {'total': self._getattr_number(stats, 'get_total_cost')}
            if stats is not None and hasattr(stats, 'get_total_cost'):
                try:
                    self._run['cost']['total'] = stats.get_total_cost()
                except Exception:
                    pass
            perf_modules = {}
            if tracker is not None and hasattr(tracker, 'global_summary'):
                for module in MODULES:
                    try:
                        summary = tracker.global_summary(module)
                    except Exception:
                        continue
                    if summary.get('count') or summary.get('retry'):
                        perf_modules[module] = {
                            key: round(value, 3) if isinstance(value, float) else value
                            for key, value in summary.items()
                        }
            durations = []
            try:
                with open(self.companies_path, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        row = json.loads(line)
                        if isinstance(row.get('duration_seconds'), (int, float)):
                            durations.append(row['duration_seconds'])
            except (OSError, json.JSONDecodeError):
                pass
            self._run['perf'] = {
                'modules': perf_modules,
                'median_company_seconds': round(statistics.median(durations), 3) if durations else None,
            }
            self._status.update(state=state, current_company=None, updated_at=timestamp,
                                finished_at=timestamp)
            self._run.update(state=state, updated_at=timestamp, finished_at=timestamp,
                             companies=dict(self._status['companies']))
            if aborted_by:
                self._run['aborted_by'] = aborted_by
                self._status['aborted_by'] = aborted_by
            self._flush()
        self.uninstall_heartbeat()
