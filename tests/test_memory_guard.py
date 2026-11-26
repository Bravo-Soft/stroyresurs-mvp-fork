"""Тесты сторожа памяти.

Прогон 26.08-01.09 убил OOM-киллер на 685-й компании из 1452: контейнер дорос до
23.6 ГиБ при лимите 24 ГиБ, а сторож не сработал НИ РАЗУ за весь лог. Причина —
он мерил psutil.Process().memory_info().rss, то есть только питон, тогда как под
mem_limit попадают и процессы Chromium/Playwright.

Покрыто:
  * из потребления вычитается страничный кэш (он вытесняется без OOM);
  * shmem внутри file не вычитается (tmpfs не вытесняется);
  * без cgroup-файлов — откат на RSS процесса, прежнее поведение;
  * РЕГРЕССИЯ: память в Chromium (RSS питона мал) — сторож обязан её увидеть;
  * пул браузеров пересоздаётся на границе компаний при пробитом пороге;
  * ниже порога пул не трогаем.
"""
import asyncio

import psutil
import pytest

import web_crawler
from config import Config
from pipeline_orchestrator import PipelineOrchestrator

GB = 1024 ** 3
MB = 1024 ** 2


def fake_cgroup(tmp_path, monkeypatch, current, file_bytes=0, shmem=0):
    """Подкладывает cgroup v2 файлы, как их видит код внутри контейнера."""
    (tmp_path / 'memory.current').write_text(str(int(current)))
    (tmp_path / 'memory.stat').write_text(
        f"anon {int(current - file_bytes)}\nfile {int(file_bytes)}\nshmem {int(shmem)}\nslab 0\n")
    monkeypatch.setattr(web_crawler, '_CGROUP_MEMORY_CURRENT', str(tmp_path / 'memory.current'))
    monkeypatch.setattr(web_crawler, '_CGROUP_MEMORY_STAT', str(tmp_path / 'memory.stat'))


# ==================== Измерение ====================

def test_page_cache_is_excluded(tmp_path, monkeypatch):
    """Страничный кэш вытесняется без OOM — под порог он попадать не должен."""
    fake_cgroup(tmp_path, monkeypatch, current=10 * GB, file_bytes=4 * GB)
    assert web_crawler.read_memory_usage_mb() == pytest.approx(6 * GB / MB)


def test_shmem_is_not_excluded(tmp_path, monkeypatch):
    """shmem учтён внутри file, но tmpfs/shm не вытесняется — возвращаем его обратно."""
    fake_cgroup(tmp_path, monkeypatch, current=10 * GB, file_bytes=4 * GB, shmem=1 * GB)
    assert web_crawler.read_memory_usage_mb() == pytest.approx(7 * GB / MB)


def test_falls_back_to_process_rss_without_cgroup(monkeypatch):
    """Вне контейнера (или на cgroup v1) файлов нет — работаем как раньше."""
    monkeypatch.setattr(web_crawler, '_CGROUP_MEMORY_CURRENT', '/nonexistent/memory.current')
    monkeypatch.setattr(web_crawler, '_CGROUP_MEMORY_STAT', '/nonexistent/memory.stat')
    rss_mb = psutil.Process().memory_info().rss / MB
    assert web_crawler.read_memory_usage_mb() == pytest.approx(rss_mb, abs=50)


def test_chromium_memory_is_visible(tmp_path, monkeypatch):
    """Регрессия: память держит Chromium, RSS питона мал — сторож обязан сработать.

    Ровно этот случай старый код и проглядел: контейнер 23.6 ГиБ при пороге 20000 МБ.
    """
    fake_cgroup(tmp_path, monkeypatch, current=23.6 * GB, file_bytes=1 * GB)
    threshold = Config().memory_cleanup_threshold_mb

    assert psutil.Process().memory_info().rss / MB < threshold   # питон сам по себе мал
    assert web_crawler.read_memory_usage_mb() > threshold         # но порог пробит


# ==================== Пересоздание пула на границе компаний ====================

class FakeBrowserPool:
    def __init__(self):
        self.recycled = 0

    async def recycle(self):
        self.recycled += 1


class FakeMonitoringSystem:
    def __init__(self):
        self.crawler = type('FakeCrawler', (), {'browser_pool': FakeBrowserPool()})()


@pytest.fixture
def orchestrator(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)          # CheckpointManager создаёт здесь подпапку
    return PipelineOrchestrator(config, kafka_manager=None)


def test_pool_recycled_above_threshold(orchestrator, tmp_path, monkeypatch):
    """Порог пробит — пул пересоздаётся: только это освобождает память Chromium."""
    fake_cgroup(tmp_path, monkeypatch, current=23.6 * GB)
    system = FakeMonitoringSystem()

    asyncio.run(orchestrator._recycle_browser_pool_if_memory_high(system))

    assert system.crawler.browser_pool.recycled == 1


def test_pool_untouched_below_threshold(orchestrator, tmp_path, monkeypatch):
    """Память в норме — браузер не трогаем, прогон идёт как раньше."""
    fake_cgroup(tmp_path, monkeypatch, current=2 * GB)
    system = FakeMonitoringSystem()

    asyncio.run(orchestrator._recycle_browser_pool_if_memory_high(system))

    assert system.crawler.browser_pool.recycled == 0
