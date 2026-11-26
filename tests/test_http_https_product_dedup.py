"""Схемные дубли: очередь, архивные страницы и атомарное извлечение товара."""
import asyncio
from collections import defaultdict, deque
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse

import pytest

import main
from config import Config
from graph_db_uploader import GraphDBDataFormatter
from product_id_manager import ProductIDManager
from web_crawler import WebCrawler


ARCHIVE = Path('/ai/projects/stroy-resurs/codex_work/http - https')
COMPANIES = (sorted(p.name for p in ARCHIVE.iterdir() if p.is_dir())
             if ARCHIVE.is_dir() else ['archive_unavailable'])
COMPANY = {'original_name': 'Тестовый завод, ООО', 'company_id': 'test'}


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    return WebCrawler(config)


def product_ai(name):
    return {'product': {'product_name': name,
                        'specifications': {'Мощность': '10 кВт',
                                           'Масса': '20 кг', 'Длина': '30 мм'}}}


def system_stub(crawler, tmp_path, monkeypatch, responses):
    """Реальные ключи, ID и обработчик; внешние вызовы заменены сохранёнными ответами."""
    crawler.temp_storage.save_ai_text = AsyncMock(return_value='saved_ai.txt')
    crawler.temp_storage.mark_as_ai_processed = AsyncMock()
    monkeypatch.setattr(main, 'html_to_markdown', Mock(return_value='Текст товара'))
    return SimpleNamespace(
        config=crawler.config, crawler=crawler,
        processing_tracker=SimpleNamespace(clean_span=lambda name: nullcontext()),
        ai_client=SimpleNamespace(extract_product_info=AsyncMock(side_effect=[
            (ai, {'input_tokens': 1, 'output_tokens': 1}) for ai in responses])),
        statistics=SimpleNamespace(total_input_tokens=0, total_output_tokens=0,
                                   total_products_skipped_duplicate=0),
        product_id_manager=ProductIDManager(),
        docx_generator=SimpleNamespace(generate_product_card=AsyncMock(return_value='card.rtf')),
        _save_markdown_file=AsyncMock(),
        _download_product_files=AsyncMock(return_value=[]),
        _download_images_from_ai_text=AsyncMock(return_value=[]),
        _get_product_folder_path=Mock(return_value=(str(tmp_path), 'Products/test')),
        _processing_product_ids_lock=asyncio.Condition(),
        _processing_product_ids=set(), _global_processed_product_ids=set(),
        _processing_slug_keys=set(), _global_processed_slug_keys=set(),
    )


async def extract(stub, stats, url, company=COMPANY):
    metadata = {'url': url, 'normalized_url': stub.crawler.url_normalizer.normalize_url(url),
                'storage_path': url, 'category': 'product'}
    return await main.MonitoringSystem.process_product_page(
        stub, '<html>Товар</html>', company, stats, metadata, 'company')


@pytest.mark.parametrize('path', [
    '/catalog/12/', '/catalog/a/', '/catalog/product/', '/catalog/', '/',
    '/catalog/element.php?id=110', '/catalog/pump/?variant=2',
    '/catalog/product/;variant=2',
])
def test_every_product_url_has_scheme_independent_key(crawler, path):
    key = crawler._product_dedup_key('http://factory.ru' + path)
    assert key is not None
    assert key == crawler._product_dedup_key('https://factory.ru' + path)


@pytest.mark.parametrize('left,right', [
    ('/catalog/element.php?id=110', '/catalog/element.php?id=111'),
    ('/catalog/pump/?variant=2', '/catalog/pump/?variant=3'),
    ('/catalog/pump/?type=2', '/catalog/pump/?type=3'),
    ('/catalog/pump/;variant=2', '/catalog/pump/;variant=3'),
    ('/catalog/a/product/', '/catalog/b/product/'),
    ('/catalog/12/', '/catalog/13/'),
    ('/catalog/A/?id=1', '/catalog/a/?id=1'),
])
def test_different_products_do_not_merge(crawler, left, right):
    first = crawler._product_dedup_key('http://factory.ru' + left)
    second = crawler._product_dedup_key('https://factory.ru' + right)
    assert first and second and first != second


def test_fallback_keeps_host_and_port(crawler):
    urls = ['http://factory.ru/catalog/product/',
            'https://other.ru/catalog/product/',
            'https://factory.ru:8080/catalog/product/',
            'https://factory.ru:8443/catalog/product/']
    keys = [crawler._product_dedup_key(url) for url in urls]
    assert all(keys) and len(set(keys)) == len(urls)


@pytest.mark.parametrize('url', ['', '/catalog/product/', 'mailto:factory.ru',
                                 'ftp://factory.ru/catalog/product/'])
def test_missing_or_non_http_url_has_no_fallback_key(crawler, url):
    assert crawler._product_dedup_key(url) is None


def test_descriptive_slug_does_not_merge_holding_products(crawler):
    crawler.config.equivalent_domains = {'factory.ru': ['other.ru']}
    assert crawler._product_dedup_key('https://factory.ru/catalog/pump-abc/') != (
        crawler._product_dedup_key('http://other.ru/products/pump-abc/'))


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('path', ['/catalog/12/', '/catalog/product/',
                                  '/catalog/element.php?id=110', '/catalog/pump-abc/'])
def test_pipeline_emits_one_product_despite_different_ai_names(
        crawler, tmp_path, monkeypatch, parallel, path):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    # Поддерживаем пересечение задач после резервирования ключа.
    async def download(*args, **kwargs):
        await asyncio.sleep(0)
        return []
    stub._download_product_files.side_effect = download

    async def run():
        urls = ['http://factory.ru' + path, 'https://factory.ru' + path]
        if parallel:
            return await asyncio.gather(*(extract(stub, stats, url) for url in urls))
        return [await extract(stub, stats, url) for url in urls]

    products = [p for p in asyncio.run(run()) if p]
    assert len(products) == 1
    product = products[0]
    assert product['source_url'] == 'http://factory.ru' + path
    assert product['metadata']['url'] == product['source_url']
    assert product['ai_raw_text'] == product_ai('Насос А')
    assert product['product_id'] == stub.product_id_manager.generate_product_id_from_name_bag(
        'Насос А', COMPANY['original_name'])
    payload = GraphDBDataFormatter.format_product_data('test', COMPANY['original_name'], products)
    assert len(payload['companies'][0]['products']) == 1
    assert payload['companies'][0]['products'][0]['source_url'] == product['source_url']
    assert stats.products_processed == stats.cards_generated == 1
    assert stats.products_skipped_duplicate == 1 and stats.products_failed == 0
    assert stub._processing_slug_keys == stub._processing_product_ids == set()
    assert stub._download_product_files.await_count == 1
    assert stub.docx_generator.generate_product_card.await_count == 1


def test_pipeline_keeps_query_product_variants(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai('Насос А'), product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    async def run():
        return [await extract(stub, stats, 'http://factory.ru/element.php?id=110'),
                await extract(stub, stats, 'https://factory.ru/element.php?id=111')]
    assert all(asyncio.run(run()))
    assert stats.products_processed == 2 and stats.products_skipped_duplicate == 0


@pytest.mark.parametrize('failed_ai', [None, {'error': 'Trash_418#'},
                                      {'product': {'product_name': 'Насос А', 'specifications': {}}}])
def test_failed_version_does_not_block_valid_version(
        crawler, tmp_path, monkeypatch, failed_ai):
    stub = system_stub(crawler, tmp_path, monkeypatch, [failed_ai, product_ai('Насос Б')])
    stats = main.CompanyStatistics(COMPANY)
    async def run():
        assert await extract(stub, stats, 'http://factory.ru/catalog/12/') is None
        assert stub._global_processed_slug_keys == stub._processing_slug_keys == set()
        assert await extract(stub, stats, 'https://factory.ru/catalog/12/') is not None
    asyncio.run(run())
    assert stats.products_processed == 1 and stats.products_skipped_duplicate == 0


def test_failure_after_key_reservation_releases_key(crawler, tmp_path, monkeypatch):
    stub = system_stub(crawler, tmp_path, monkeypatch,
                       [product_ai('Насос А'), product_ai('Насос Б')])
    stub._download_product_files.side_effect = [RuntimeError('Тестовая ошибка'), []]
    stats = main.CompanyStatistics(COMPANY)
    async def run():
        assert await extract(stub, stats, 'http://factory.ru/catalog/12/') is None
        assert stub._global_processed_slug_keys == stub._processing_slug_keys == set()
        assert await extract(stub, stats, 'https://factory.ru/catalog/12/') is not None
    asyncio.run(run())
    assert stats.products_processed == stats.products_failed == 1
    assert stub._processing_product_ids == stub._processing_slug_keys == set()


@pytest.mark.parametrize('company', COMPANIES)
@pytest.mark.skipif(not ARCHIVE.is_dir(), reason='Архив примеров http/https недоступен')
def test_archive_scheme_pairs_have_product_keys(crawler, company):
    groups = defaultdict(list)
    for path in (ARCHIVE / company / 'Product_pages').glob('*.json'):
        metadata = json.loads(path.read_text(encoding='utf-8'))
        parsed = urlparse(metadata['normalized_url'])
        groups[parsed._replace(scheme='').geturl()].append(metadata['url'])
    pairs = [urls for urls in groups.values()
             if len({urlparse(url).scheme for url in urls}) > 1]
    assert pairs, 'В примерах компании должны быть пары http/https'
    for urls in pairs:
        keys = [crawler._product_dedup_key(url) for url in urls]
        assert all(keys) and len(set(keys)) == 1, urls


def test_sitemap_and_link_queue_share_scheme_guard(crawler):
    http = 'http://factory.ru/catalog/12/'
    https = http.replace('http:', 'https:')
    queue = deque()
    async def run():
        rows = [(url, crawler.url_normalizer.normalize_url(url), 'product', 9)
                for url in [http, https]]
        await crawler._add_sitemap_urls_to_queue(queue, rows, 'http://factory.ru/')
        assert not await crawler._should_add_to_queue_parallel(https, 1, 'product')
    asyncio.run(run())
    assert len(queue) == len(crawler.product_urls) == 1


def test_link_queue_atomic_scheme_guard(crawler):
    async def run():
        return await asyncio.gather(*(
            crawler._should_add_to_queue_parallel(url, 1, 'product')
            for url in ['http://factory.ru/catalog/12/', 'https://factory.ru/catalog/12/']))
    assert asyncio.run(run()).count(True) == 1
    assert len(crawler.product_urls) == 1
