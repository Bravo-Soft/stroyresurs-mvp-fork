"""Тесты D101 «Зависание пайплайна навсегда».

Критерий проверки из defects_kb (D101.verify): «симуляция висящего сокета -> компания
завершается по таймауту, прогон продолжается».

Сторож меряет ПРОСТОЙ (молчание лога), а не общее время компании, поэтому проверяем обе
стороны: молчащая компания бросается, а долго работающая (но подающая признаки жизни) — нет.

Покрыто:
  * молчащая компания бросается, прогон идёт дальше;
  * компания, работающая много дольше порога, НЕ режется, пока пишет в лог;
  * прогон продолжается, даже если отмена зависшей задачи не проходит;
  * нормальная компания и исключения ведут себя как раньше (сторож прозрачен);
  * брошенная urgent-задача не ставится на повтор;
  * семафор пула браузеров освобождается, даже если context.close() не возвращается;
  * recycle() прощает разрешения, удержанные брошенными задачами.
"""
import asyncio
import logging
import time

import pytest

import activity_heartbeat
from config import Config
from pipeline_orchestrator import PipelineOrchestrator
from web_crawler import BrowserPool

COMPANY = {'original_name': 'Тестовая компания', 'company_id': '1', 'website': 'https://a.ru/'}

IDLE_LIMIT = 1        # порог простоя в тестах, сек (в бою 1800)
BEAT_INTERVAL = 0.2   # как часто «живая» компания подаёт признаки жизни
BEATS = 15            # 3 с работы = 3x порога простоя


# ==================== Заглушки ====================

class FakeBrowserPool:
    def __init__(self):
        self.recycled = 0

    async def recycle(self):
        self.recycled += 1


class FakeMonitoringSystem:
    """Заглушка MonitoringSystem: process_company ведёт себя по сценарию."""

    def __init__(self, behaviour='ok'):
        self.behaviour = behaviour
        self.crawler = type('FakeCrawler', (), {'browser_pool': FakeBrowserPool()})()
        self.started = []
        self.cancelled = []

    async def process_company(self, company_data):
        self.started.append(company_data['original_name'])
        if self.behaviour == 'hang':
            try:
                await asyncio.sleep(3600)  # молчание: мёртвый браузер / висящий сокет
            except asyncio.CancelledError:
                self.cancelled.append(company_data['original_name'])
                raise
        elif self.behaviour == 'long_but_alive':
            # «Огромная компания»: работает много дольше порога простоя, но пишет в лог
            for _ in range(BEATS):
                await asyncio.sleep(BEAT_INTERVAL)
                logging.getLogger('crawler').info('Обработка страницы: https://a.ru/p')
        elif self.behaviour == 'raise':
            raise RuntimeError('нештатная ошибка компании')
        return {'status': 'success', 'company': company_data}


class StubbornMonitoringSystem(FakeMonitoringSystem):
    """Зависание, которое не поддаётся отмене (как cleanup Playwright на мёртвом браузере).

    Первую отмену проглатывает, вторую (при закрытии цикла) пропускает — иначе тест не завершился бы.
    """

    async def process_company(self, company_data):
        self.started.append(company_data['original_name'])
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.append(company_data['original_name'])
                if len(self.cancelled) >= 2:
                    raise


class FakeKafka:
    """Заглушка KafkaTaskManager: очередь пуста, статусы и повторы записываются."""

    def __init__(self):
        self.statuses = []
        self.retries = []

    async def get_urgent_task(self):
        return None

    async def send_task_status(self, task_id, status, message, progress=None):
        self.statuses.append((task_id, status))

    async def send_urgent_task(self, company_data, user_id):
        self.retries.append(company_data)


@pytest.fixture
def heartbeat():
    """Пульс на корневом логгере, как его ставит main.py (уровень INFO — как в бою)."""
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.INFO)
    hb = activity_heartbeat.install()
    hb.touch()
    yield hb
    root.setLevel(prev_level)
    root.removeHandler(hb)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch, heartbeat):
    config = Config()
    config.base_dir = str(tmp_path)                  # CheckpointManager создаёт здесь подпапку
    config.company_idle_timeout_seconds = IDLE_LIMIT
    monkeypatch.setattr(PipelineOrchestrator, 'WATCHDOG_POLL_SECONDS', 0.05)
    return PipelineOrchestrator(config, kafka_manager=None)


# ==================== Пульс активности ====================

def test_heartbeat_tracks_log_records(heartbeat):
    """Любая запись в лог обновляет пульс; без записей простой растёт."""
    time.sleep(0.15)
    assert heartbeat.idle_seconds() >= 0.15

    logging.getLogger('crawler').info('Обработка страницы: https://a.ru/p')
    assert heartbeat.idle_seconds() < 0.1


# ==================== Сторож зависаний ====================

def test_silent_company_is_abandoned(orchestrator):
    """Молчащая компания бросается, а не вешает прогон навсегда."""
    system = FakeMonitoringSystem('hang')

    started = time.monotonic()
    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))
    elapsed = time.monotonic() - started

    assert result is None, 'брошенная компания должна вернуть None'
    assert elapsed < 10, f'сторож не сработал вовремя: {elapsed:.1f} с'
    assert system.cancelled == ['Тестовая компания'], 'зависшая задача должна быть отменена'
    assert system.crawler.browser_pool.recycled == 1, 'пул браузеров должен быть пересоздан'


def test_long_running_company_is_not_cut(orchestrator):
    """Главное свойство: компания, работающая много дольше порога простоя, НЕ режется,
    пока подаёт признаки жизни (у заказчика такие сайты обрабатываются сутками)."""
    system = FakeMonitoringSystem('long_but_alive')

    started = time.monotonic()
    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))
    elapsed = time.monotonic() - started

    assert elapsed > IDLE_LIMIT * 2, f'компания отработала слишком быстро, тест ничего не проверил: {elapsed:.1f} с'
    assert result == {'status': 'success', 'company': COMPANY}, 'живая компания не должна быть брошена'
    assert system.crawler.browser_pool.recycled == 0


def test_run_continues_when_cancel_does_not_work(orchestrator, monkeypatch):
    """Прогон продолжается, даже если зависшая задача игнорирует отмену."""
    monkeypatch.setattr(PipelineOrchestrator, 'CANCEL_GRACE_SECONDS', 0.1)
    system = StubbornMonitoringSystem()

    started = time.monotonic()
    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 10, f'ожидание отмены не ограничено: {elapsed:.1f} с'
    assert system.crawler.browser_pool.recycled == 1


def test_normal_company_is_untouched(orchestrator):
    """Уложившаяся компания проходит сквозь сторож без изменений."""
    system = FakeMonitoringSystem('ok')

    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))

    assert result == {'status': 'success', 'company': COMPANY}
    assert system.crawler.browser_pool.recycled == 0, 'пул пересоздавать незачем'


def test_exception_propagates_as_before(orchestrator):
    """Исключение компании пробрасывается наверх (его ловит цикл оркестратора)."""
    system = FakeMonitoringSystem('raise')

    with pytest.raises(RuntimeError, match='нештатная ошибка компании'):
        asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))

    assert system.crawler.browser_pool.recycled == 0


def test_watchdog_off_when_heartbeat_not_installed(orchestrator, heartbeat):
    """Fail-open: без подключённого пульса простой рос бы вечно и сторож зарубил бы
    любую живую компанию — в этом случае сторож обязан выключиться, а не сработать."""
    logging.getLogger().removeHandler(heartbeat)
    assert not heartbeat.is_installed()
    system = FakeMonitoringSystem('long_but_alive')

    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))

    assert result == {'status': 'success', 'company': COMPANY}
    assert system.crawler.browser_pool.recycled == 0


def test_zero_disables_watchdog(orchestrator):
    """company_idle_timeout_seconds=0 — прежнее поведение без обёртки в задачу."""
    orchestrator.config.company_idle_timeout_seconds = 0
    system = FakeMonitoringSystem('ok')

    result = asyncio.run(orchestrator._process_company_watchdogged(system, COMPANY))

    assert result == {'status': 'success', 'company': COMPANY}


def test_abandoned_urgent_task_is_not_retried(orchestrator):
    """Брошенная urgent-задача не ставится на повтор: повтор снова упёрся бы в сторож."""
    from kafka_manager import TaskMessage, TaskType

    kafka = FakeKafka()
    orchestrator.kafka_manager = kafka
    system = FakeMonitoringSystem('hang')
    task = TaskMessage(task_id='t1', task_type=TaskType.URGENT, company_data=COMPANY, user_id=1)

    asyncio.run(orchestrator._process_all_urgent_tasks(system, task))

    assert kafka.retries == [], 'зависшая urgent-задача не должна ставиться на повтор'
    assert kafka.statuses[-1][1].value == 'failed', 'статус задачи должен быть FAILED'
    assert system.crawler.browser_pool.recycled == 1


# ==================== Живучесть пула браузеров ====================

class HangingContext:
    async def close(self):
        await asyncio.sleep(3600)  # мёртвый браузер не отвечает никогда


class FakeBrowser:
    async def new_context(self, **kwargs):
        return HangingContext()

    async def close(self):
        pass


def test_semaphore_released_when_context_close_hangs(monkeypatch):
    """Механизм зависания: context.close() не возвращается -> раньше семафор пула
    оставался захваченным и следующий get_browser() ждал вечно."""
    monkeypatch.setattr(BrowserPool, 'CLOSE_TIMEOUT', 0.05)

    async def run():
        pool = BrowserPool(max_concurrent_contexts=1)
        pool._browser = FakeBrowser()

        context = await pool.get_browser()
        # wait_for — страховка: до фикса return_browser не возвращался вовсе (тест бы завис)
        await asyncio.wait_for(pool.return_browser(context), timeout=5)

        # Единственное разрешение должно вернуться в пул
        return await asyncio.wait_for(pool.get_browser(), timeout=1)

    assert asyncio.run(run()) is not None


def test_recycle_forgives_leaked_permits(monkeypatch):
    """recycle() возвращает пул в рабочее состояние, даже когда все разрешения
    удерживают брошенные задачи."""
    monkeypatch.setattr(BrowserPool, 'CLOSE_TIMEOUT', 0.05)

    async def run():
        pool = BrowserPool(max_concurrent_contexts=2)
        pool._browser = FakeBrowser()

        # Брошенные задачи удерживают оба разрешения
        await pool._context_semaphore.acquire()
        await pool._context_semaphore.acquire()
        assert pool._context_semaphore.locked()

        async def fake_initialize():
            pool._browser = FakeBrowser()

        monkeypatch.setattr(pool, 'initialize', fake_initialize)
        await pool.recycle()

        return await asyncio.wait_for(pool.get_browser(), timeout=1)

    assert asyncio.run(run()) is not None
