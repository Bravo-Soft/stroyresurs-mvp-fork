"""Повторная URL-версия ждёт владельца ключа и восстанавливает товар при его сбое."""
import asyncio
from unittest.mock import Mock

import pytest

from test_http_https_product_dedup import COMPANY, crawler, extract, main, product_ai, system_stub


async def start_pair(stub, stats, entered, urls):
    first = asyncio.create_task(extract(stub, stats, urls[0]))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(extract(stub, stats, urls[1]))
    await asyncio.sleep(0)
    return first, second, not second.done()


def assert_released(stub):
    assert not stub._processing_product_ids and not stub._processing_slug_keys
    assert not stub._processing_product_ids_lock.locked()


@pytest.mark.parametrize('path', [
    '/catalog/12/', '/catalog/product/', '/element.php?id=110', '/catalog/pump-abc/',
])
@pytest.mark.parametrize('same_name', [False, True])
@pytest.mark.parametrize('first_scheme', ['http', 'https'])
def test_failed_owner_hands_product_to_waiter(
        crawler, tmp_path, monkeypatch, path, same_name, first_scheme):
    stub = system_stub(crawler, tmp_path, monkeypatch, [
        product_ai('Насос А'), product_ai('Насос А' if same_name else 'Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    second_scheme = 'https' if first_scheme == 'http' else 'http'
    urls = [f'{first_scheme}://factory.ru{path}', f'{second_scheme}://factory.ru{path}']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(url, *args, **kwargs):
            if url == urls[0]:
                entered.set()
                await release.wait()
                raise RuntimeError('Сбой первой версии после резервирования ключа')
            return []

        stub._download_product_files.side_effect = download
        first, second, waited = await start_pair(stub, stats, entered, urls)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        return results, waited

    results, waited = asyncio.run(run())
    assert results[0] is None and results[1] is not None
    assert waited and results[1]['source_url'] == urls[1]
    assert stats.products_processed == stats.cards_generated == stats.products_failed == 1
    assert stats.products_skipped_duplicate == 0
    assert stub.docx_generator.generate_product_card.await_count == 1
    assert_released(stub)


@pytest.mark.parametrize('same_name', [False, True])
def test_successful_owner_is_the_only_product(crawler, tmp_path, monkeypatch, same_name):
    stub = system_stub(crawler, tmp_path, monkeypatch, [
        product_ai('Насос А'), product_ai('Насос А' if same_name else 'Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        stub._download_product_files.side_effect = download
        first, second, waited = await start_pair(stub, stats, entered, urls)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        return results, waited

    results, waited = asyncio.run(run())
    assert results[0] is not None and results[1] is None and waited
    assert results[0]['source_url'] == urls[0]
    assert stats.products_processed == stats.cards_generated == stats.products_skipped_duplicate == 1
    assert stats.products_failed == 0
    assert stub._download_product_files.await_count == 1
    assert stub.docx_generator.generate_product_card.await_count == 1
    assert_released(stub)


@pytest.mark.parametrize('same_name', [False, True])
@pytest.mark.parametrize('cancel_count', [1, 2])
def test_cancelled_owner_releases_waiter(crawler, tmp_path, monkeypatch, same_name, cancel_count):
    stub = system_stub(crawler, tmp_path, monkeypatch, [
        product_ai('Насос А'), product_ai('Насос А' if same_name else 'Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered = asyncio.Event()

        async def download(url, *args, **kwargs):
            if url == urls[0]:
                entered.set()
                await asyncio.Event().wait()
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        for _ in range(cancel_count):
            first.cancel()
        return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)

    results = asyncio.run(run())
    assert isinstance(results[0], asyncio.CancelledError) and isinstance(results[1], dict)
    assert results[1]['source_url'] == urls[1]
    assert stats.products_processed == stats.cards_generated == 1
    assert stats.products_skipped_duplicate == stats.products_failed == 0
    assert_released(stub)


def test_cancelled_waiter_does_not_release_owner(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        second.cancel()
        cancellation = await asyncio.gather(second, return_exceptions=True)
        occupied = bool(stub._processing_product_ids and stub._processing_slug_keys)
        release.set()
        result = await asyncio.wait_for(first, 1)
        return result, cancellation[0], occupied

    product, cancellation, occupied = asyncio.run(run())
    assert isinstance(cancellation, asyncio.CancelledError) and occupied
    assert product['source_url'] == urls[0]
    assert stub.crawler.temp_storage.mark_as_ai_processed.await_count == 1
    assert_released(stub)


def test_all_cancelled_versions_release_keys(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered = asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        first.cancel()
        second.cancel()
        return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)

    results = asyncio.run(run())
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not stub._global_processed_product_ids and not stub._global_processed_slug_keys
    assert stub.crawler.temp_storage.mark_as_ai_processed.await_count == 0
    assert_released(stub)


def test_multiple_failures_allow_the_last_version(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai(name) for name in ('Насос А', 'Насос Б', 'Насос В')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/pump-abc/', 'https://factory.ru/catalog/pump-abc/',
            'https://factory.ru/catalog/pump-abc/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0

        async def download(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
            if attempts < 3:
                raise RuntimeError('Сбой версии')
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        third = asyncio.create_task(extract(stub, stats, urls[2]))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, second, third), 1)

    products = [product for product in asyncio.run(run()) if product]
    assert len(products) == 1 and stats.products_failed == 2
    assert stats.products_skipped_duplicate == 0
    assert stats.products_processed == stats.cards_generated == 1
    assert_released(stub)


def test_unrelated_product_does_not_wait_for_duplicate(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai(name) for name in ('Насос А', 'Насос Б', 'Насос В')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(url, *args, **kwargs):
            if url == urls[0]:
                entered.set()
                await release.wait()
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        try:
            unrelated = await asyncio.wait_for(extract(stub, stats, 'http://factory.ru/other/99/'), 1)
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        return unrelated, results

    unrelated, results = asyncio.run(run())
    assert unrelated['source_url'] == 'http://factory.ru/other/99/'
    assert results[0] is not None and results[1] is None
    assert stats.products_processed == stats.cards_generated == 2
    assert_released(stub)


def test_metadata_failure_does_not_commit_product(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def mark_processed(path, *args, **kwargs):
            if path == urls[0]:
                entered.set()
                await release.wait()
                raise RuntimeError('Сбой записи метаданных')

        stub.crawler.temp_storage.mark_as_ai_processed.side_effect = mark_processed
        first, second, waited = await start_pair(stub, stats, entered, urls)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        return results, waited

    results, waited = asyncio.run(run())
    assert results[0] is None and results[1] is not None and waited
    assert results[1]['source_url'] == urls[1]
    assert len(stub._global_processed_product_ids) == len(stub._global_processed_slug_keys) == 1
    assert stats.products_processed == stats.products_failed == stats.cards_generated == 1
    assert_released(stub)


def test_same_id_without_url_key_can_retry(crawler, tmp_path, monkeypatch):
    crawler._product_dedup_key = Mock(return_value=None)
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос А')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(url, *args, **kwargs):
            if url == urls[0]:
                entered.set()
                await release.wait()
                raise RuntimeError('Сбой первой версии')
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, second), 1)

    results = asyncio.run(run())
    assert results[0] is None and results[1] is not None
    assert not stub._global_processed_slug_keys
    assert_released(stub)


def test_card_failure_keeps_existing_product_semantics(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос Б')])
    stub.docx_generator.generate_product_card.side_effect = RuntimeError('Сбой карточки')
    stats = main.CompanyStatistics(COMPANY)

    async def run():
        return await asyncio.gather(
            extract(stub, stats, 'http://factory.ru/catalog/12/'),
            extract(stub, stats, 'https://factory.ru/catalog/12/'))

    products = [product for product in asyncio.run(run()) if product]
    assert len(products) == 1 and stats.products_processed == stats.products_skipped_duplicate == 1
    assert stats.products_failed == stats.cards_generated == 0
    assert stub.docx_generator.generate_product_card.await_count == 1
    assert_released(stub)


def test_cancel_during_card_generation_releases_waiter(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch, [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']

    async def run():
        entered = asyncio.Event()

        async def generate(product, *args, **kwargs):
            if product['source_url'] == urls[0]:
                entered.set()
                await asyncio.Event().wait()
            return 'card.docx'

        stub.docx_generator.generate_product_card.side_effect = generate
        first, second, _ = await start_pair(stub, stats, entered, urls)
        first.cancel()
        return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)

    results = asyncio.run(run())
    assert isinstance(results[0], asyncio.CancelledError) and isinstance(results[1], dict)
    assert results[1]['source_url'] == urls[1]
    assert stats.products_processed == stats.cards_generated == 1
    assert_released(stub)


def test_success_wakes_all_duplicate_waiters(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai(f'Насос серия {i}') for i in range(5)])
    stats = main.CompanyStatistics(COMPANY)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        stub._download_product_files.side_effect = download
        first = asyncio.create_task(extract(stub, stats, 'http://factory.ru/catalog/pump-abc/'))
        await asyncio.wait_for(entered.wait(), 1)
        others = [asyncio.create_task(extract(stub, stats, 'https://factory.ru/catalog/pump-abc/'))
                  for i in range(4)]
        await asyncio.sleep(0)
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, *others), 1)

    results = asyncio.run(run())
    assert results[0] is not None and all(result is None for result in results[1:])
    assert stats.products_processed == stats.cards_generated == 1
    assert stats.products_skipped_duplicate == 4 and stats.products_failed == 0
    assert_released(stub)


@pytest.mark.parametrize('cached_key', ['product_id', 'url'])
def test_completed_duplicate_does_not_wait_for_other_owner(
        crawler, tmp_path, monkeypatch, cached_key):
    stub = system_stub(crawler, tmp_path, monkeypatch, [
        product_ai('Насос А'), product_ai('Насос Б' if cached_key == 'product_id' else 'Насос А')])
    stats = main.CompanyStatistics(COMPANY)
    urls = ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']
    if cached_key == 'product_id':
        stub._global_processed_product_ids.add(stub.product_id_manager.generate_product_id_from_name_bag(
            'Насос Б', COMPANY['original_name']))
    else:
        urls[0] = 'http://factory.ru/other/99/'
        stub._global_processed_slug_keys.add(crawler._product_dedup_key(urls[1]))

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        stub._download_product_files.side_effect = download
        first, second, _ = await start_pair(stub, stats, entered, urls)
        try:
            duplicate = await asyncio.wait_for(second, 1)
        finally:
            release.set()
        owner = await asyncio.wait_for(first, 1)
        return duplicate, owner

    duplicate, owner = asyncio.run(run())
    assert duplicate is None and owner is not None
    assert stats.products_processed == stats.products_skipped_duplicate == 1
    assert stats.products_failed == 0
    assert_released(stub)
