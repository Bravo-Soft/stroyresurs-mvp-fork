"""Эмулятор pipeline для локальной проверки Диспетчерской.

Не импортирует краулер, Kafka и LLM. Он создаёт те же telemetry-файлы, что и
настоящий ``main.py``, и уважает команду мягкой остановки между компаниями.
"""

import logging
import os
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
MVP_DIR = os.path.dirname(HERE)
if MVP_DIR not in sys.path:
    sys.path.insert(0, MVP_DIR)

import pipeline_control
from run_recorder import RunRecorder


def _flag(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    log = logging.getLogger('dashboard.fake_pipeline')
    logs_dir = os.environ.get('LOGS_DIR') or os.path.join(MVP_DIR, 'logs')
    selected = pipeline_control.company_ids_from_env()
    total = len(selected) if selected is not None else max(1, _flag('FAKE_COMPANIES', 5))
    seconds = max(0.1, float(os.environ.get('FAKE_SECONDS_PER_COMPANY', '6')))
    zero_every = _flag('FAKE_ZERO_EVERY')
    fail_every = _flag('FAKE_FAIL_EVERY')
    hang_after = _flag('FAKE_HANG_AFTER')
    recorder = RunRecorder(logs_dir)
    recorder.start(os.environ.get('PIPELINE_MODE', 'full'), total, selected, resume=True)
    recorder.install_heartbeat()
    stopped = False
    try:
        for index in range(total):
            company_id = selected[index] if selected is not None else f'demo-{index + 1}'
            company = {
                'company_id': company_id,
                'original_name': f'Демо-компания {index + 1}',
                'website': f'https://demo-{index + 1}.example.test',
            }
            recorder.company_started(company, index=index + 1, total=total)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if hang_after and index + 1 > hang_after:
                    # Нужен именно неподвижный status.json для проверки watchdog.
                    time.sleep(max(1.0, seconds))
                    break
                log.info('Демо: обрабатывается %s', company['original_name'])
                time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
            failed = bool(fail_every and (index + 1) % fail_every == 0)
            products = 0 if (zero_every and (index + 1) % zero_every == 0) else index + 1
            result = {
                'status': 'error' if failed else 'success',
                'errors': ['Демонстрационная ошибка'] if failed else [],
                'company_statistics': {
                    'products_processed': products,
                    'products_found': products,
                    'product_pages_processed': products * 2,
                    'cards_generated': products,
                    'errors': ['Демонстрационная ошибка'] if failed else [],
                },
            }
            recorder.company_finished(company, result)
            if pipeline_control.stop_requested(logs_dir):
                stopped = True
                log.info('Демо: получена команда мягкой остановки')
                break
    finally:
        recorder.finish(aborted=stopped, aborted_by='stopped_by_admin' if stopped else None)
        recorder.uninstall_heartbeat()


if __name__ == '__main__':
    main()
