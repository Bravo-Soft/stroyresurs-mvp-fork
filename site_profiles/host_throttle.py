"""Пер-хостовый троттлинг запросов (crawl.load.page_delay_ms / file_delay_ms профиля).

Задержка = минимальный интервал между ПОСЛЕДОВАТЕЛЬНЫМИ запросами к одному хосту.
delay_ms <= 0 -> no-op (текущее поведение без профиля). Каналы 'pages' и 'files'
независимы: скачивание файлов не блокирует краул страниц и наоборот.
rate_limiter.py не переиспользуется намеренно: он глобальный (окно под LLM),
а здесь нужен пер-хостовый межзапросный интервал.
"""
import asyncio
import time


class HostThrottle:
    def __init__(self):
        self._locks = {}   # (channel, host) -> asyncio.Lock
        self._last = {}    # (channel, host) -> monotonic-время последнего запроса

    async def acquire(self, host: str, delay_ms, channel: str = 'pages'):
        """Ждёт, пока с последнего запроса к host пройдёт delay_ms миллисекунд."""
        if not delay_ms or delay_ms <= 0 or not host:
            return
        key = (channel, host)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait = self._last.get(key, 0.0) + delay_ms / 1000.0 - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[key] = time.monotonic()
