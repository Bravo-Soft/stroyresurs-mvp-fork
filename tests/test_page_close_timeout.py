"""Закрытие страницы браузера под таймаутом (зависание termal.biz, стенд 28.09.2026).

Дамп цепочек await на стенде: три задачи `_process_page_parallel` стояли в
`DynamicContentExtractor.extract` → `finally` → `page.close()` → Playwright `send`,
ответ от браузера не приходил; главный цикл обхода ждал их в `gather` до таймаута
компании (на обоих деревьях — базовом 9659d42 и step0-1). Контекст и браузер уже
закрывались под `BrowserPool.CLOSE_TIMEOUT` (D101), страница — нет.

Покрыто:
  * `BrowserPool.close_page` возвращается по таймауту, если `page.close()` висит;
  * штатное закрытие проходит без ожидания таймаута;
  * `DynamicContentExtractor.extract` с зависшим `page.close()` отдаёт контент,
    возвращает контекст в пул и освобождает семафор.
"""
import asyncio
import time

from config import Config
from web_crawler import BrowserPool, DynamicContentExtractor

HANG_TIMEOUT = 0.05   # потолок закрытия в тестах, сек (в бою CLOSE_TIMEOUT = 30)


# ==================== Заглушки ====================

class HangingPage:
    """Страница, у которой close() не возвращается никогда (зависший рендерер)."""

    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        await asyncio.Event().wait()

    async def goto(self, url, **kwargs):
        return None

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def query_selector_all(self, selector):
        return []          # ни вкладок, ни аккордеонов -> ветка page.content()

    async def content(self):
        return '<html><body>контент</body></html>'


class ClosingPage(HangingPage):
    async def close(self):
        self.close_calls += 1


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class FakePool(BrowserPool):
    """Настоящие close_page/семафор, без Playwright."""

    def __init__(self, page):
        super().__init__(max_concurrent_contexts=1)
        self.CLOSE_TIMEOUT = HANG_TIMEOUT
        self.page = page
        self.returned = 0

    async def get_browser(self):
        await self._context_semaphore.acquire()
        return FakeContext(self.page)

    async def return_browser(self, context):
        self.returned += 1
        self._context_semaphore.release()


# ==================== Тесты ====================

def test_close_page_returns_after_timeout_when_close_hangs():
    page = HangingPage()
    pool = FakePool(page)
    started = time.monotonic()
    asyncio.run(pool.close_page(page))
    assert page.close_calls == 1
    assert time.monotonic() - started < 2, 'close_page должен вернуться по таймауту, а не ждать вечно'


def test_close_page_normal_close_is_immediate():
    page = ClosingPage()
    pool = FakePool(page)
    asyncio.run(pool.close_page(page))
    assert page.close_calls == 1


def test_extract_finishes_and_releases_pool_when_page_close_hangs():
    page = HangingPage()
    pool = FakePool(page)
    extractor = DynamicContentExtractor(pool, Config())

    async def run():
        return await asyncio.wait_for(extractor.extract('https://termal.test/dealers/'), timeout=5)

    result = asyncio.run(run())
    assert result == '<html><body>контент</body></html>'
    assert page.close_calls == 1
    assert pool.returned == 1, 'контекст должен вернуться в пул и после зависшего close()'
    assert not pool._context_semaphore.locked(), 'семафор пула должен быть свободен'
