"""Миграция storage identity: фактический URL важнее старого lossy ключа."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from config import Config
from temp_storage_manager import TempStorageManager
from web_crawler import WebCrawler

COMPANY = 'Тест совместимости'
BASE = 'https://example.ru/catalog'
TYPE2 = BASE + '?type=2'
TYPE3 = BASE + '?type=3'


def legacy_record(storage, actual, old_key=BASE, *, content='legacy html', stamp='2026-10-01', run='old'):
    folder = storage._get_company_subdir(COMPANY, 'product')
    digest = hashlib.sha256(content.encode()).hexdigest()
    html = folder / (storage._generate_filename(old_key, digest) + '.html')
    html.write_text(content)
    metadata = {'url': actual, 'normalized_url': old_key, 'category': 'product',
                'content_hash': digest, 'saved_at': stamp, 'crawl_run_id': run,
                'storage_path': str(html), 'checkpoints': {'html_saved': True, 'ai_processed': True},
                'product_result': {'product_id': actual}}
    html.with_suffix('.json').write_text(json.dumps(metadata))
    return html


def test_legacy_lookup_matches_actual_url(tmp_path):
    storage = TempStorageManager(str(tmp_path), config=Config())
    old = legacy_record(storage, TYPE2)
    async def run():
        assert await storage._find_existing_files(TYPE2, COMPANY, 'product') == [old]
        assert await storage._find_existing_files(TYPE3, COMPANY, 'product') == []
    asyncio.run(run())


def test_latest_keeps_distinct_actual_urls_with_same_old_key(tmp_path):
    storage = TempStorageManager(str(tmp_path), config=Config())
    legacy_record(storage, TYPE2, content='type2', stamp='2026-10-01')
    legacy_record(storage, TYPE3, content='type3', stamp='2026-10-02')
    files = asyncio.run(storage.get_company_files(COMPANY))
    assert {item['metadata']['url'] for item in files} == {TYPE2, TYPE3}
    assert {item['metadata']['normalized_url'] for item in files} == {TYPE2, TYPE3}


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = WebCrawler(cfg)
    c.current_base_url = 'https://example.ru/'
    c.temp_storage = TempStorageManager(str(tmp_path / 'compat'), config=cfg,
                                       url_normalizer=c.url_normalizer.normalize_url)
    return c


@pytest.mark.parametrize('actual,other,lossy', [
    (TYPE2, TYPE3, BASE),
    (BASE + '?filter=a', BASE + '?filter=b', BASE),
    (BASE + '?version=2', BASE + '?version=3', BASE),
    (BASE + '?format=2', BASE + '?format=3', BASE),
    (BASE + '?region=2', BASE + '?region=3', BASE),
    ('https://example.ru/ru/catalog', BASE, BASE),
    ('https://example.ru/Catalog', BASE, BASE),
    ('https://example.ru:8443/catalog', BASE, BASE),
])
def test_shared_normalizer_does_not_reuse_legacy_lossy_alias(crawler, actual, other, lossy):
    old = legacy_record(crawler.temp_storage, actual, old_key=lossy)
    normalize = crawler.url_normalizer.normalize_url
    async def run():
        assert normalize(actual) != normalize(other)
        assert await crawler.temp_storage._find_existing_files(normalize(actual), COMPANY, 'product') == [old]
        assert await crawler.temp_storage._find_existing_files(normalize(other), COMPANY, 'product') == []
    asyncio.run(run())


def test_refresh_reuses_only_matching_actual_legacy_html_and_ai(crawler):
    old = legacy_record(crawler.temp_storage, TYPE2)
    old_name = old.name
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        path, is_new, meta = await crawler.temp_storage.save_cleaned_html(
            TYPE2, crawler.url_normalizer.normalize_url(TYPE2), 'legacy html', COMPANY, category='product')
        assert path == str(old) and not is_new
        assert Path(path).name == old_name
        assert meta['checkpoints']['ai_processed']
        assert meta['product_result']['product_id'] == TYPE2
        assert meta['crawl_run_id'] == 'current'
        other, is_new, other_meta = await crawler.temp_storage.save_cleaned_html(
            TYPE3, crawler.url_normalizer.normalize_url(TYPE3), 'legacy html', COMPANY, category='product')
        assert other != path and is_new
        assert not other_meta['checkpoints']['ai_processed']
        assert old.exists() and len(list(old.parent.glob('*.html'))) == 2
    asyncio.run(run())


def test_reading_legacy_metadata_projects_identity_without_rewrite(crawler):
    old = legacy_record(crawler.temp_storage, TYPE2, run='current')
    before = old.with_suffix('.json').read_bytes()
    async def run():
        crawler.temp_storage.crawl_run_id = 'current'
        files = await crawler.temp_storage.get_company_files(COMPANY)
        assert files[0]['metadata']['normalized_url'] == crawler.url_normalizer.normalize_url(TYPE2)
        loaded = await crawler.temp_storage.load_cleaned_html(str(old))
        assert loaded['metadata']['normalized_url'] == crawler.url_normalizer.normalize_url(TYPE2)
    asyncio.run(run())
    assert old.with_suffix('.json').read_bytes() == before


def test_resume_projects_old_key_and_never_claims_another_query(crawler):
    old = legacy_record(crawler.temp_storage, TYPE2, run='same')
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='same', resume=True)
        assert crawler.url_normalizer.normalize_url(TYPE2) in crawler._resume_pages
        assert crawler.url_normalizer.normalize_url(TYPE3) not in crawler._resume_pages
        assert crawler.url_normalizer.normalize_url(BASE) not in crawler._resume_pages
        assert crawler._resume_pages[crawler.url_normalizer.normalize_url(TYPE2)]['html_path'] == str(old)
    asyncio.run(run())


def test_latest_and_current_run_use_actual_identity(crawler):
    first = legacy_record(crawler.temp_storage, TYPE2, content='type2-old', stamp='2026-10-01', run='old')
    latest = legacy_record(crawler.temp_storage, TYPE2, content='type2-new', stamp='2026-10-03', run='current')
    third = legacy_record(crawler.temp_storage, TYPE3, content='type3', stamp='2026-10-02', run='current')
    async def run():
        crawler.temp_storage.crawl_run_id = 'current'
        files = await crawler.temp_storage.get_company_files(COMPANY)
        assert {item['html_path'] for item in files} == {str(latest), str(third)}
        assert await crawler.temp_storage._find_existing_files(crawler.url_normalizer.normalize_url(TYPE2), COMPANY, 'product') == [latest]
        assert first.exists()
    asyncio.run(run())


def test_legacy_without_actual_url_never_guesses_lost_query(tmp_path):
    storage = TempStorageManager(str(tmp_path), config=Config())
    old = legacy_record(storage, None)
    async def run():
        assert await storage._find_existing_files(BASE, COMPANY, 'product') == [old]
        assert await storage._find_existing_files(TYPE2, COMPANY, 'product') == []
    asyncio.run(run())


def test_lazy_index_reused_for_repeated_saves_and_new_records(crawler, monkeypatch):
    old = legacy_record(crawler.temp_storage, TYPE2)
    original_glob = Path.glob
    scans = []
    def counted_glob(path, pattern, *args, **kwargs):
        if path == old.parent and pattern == '*.json':
            scans.append(pattern)
        return original_glob(path, pattern, *args, **kwargs)
    monkeypatch.setattr(Path, 'glob', counted_glob)
    async def run():
        for address in (TYPE2, TYPE3, TYPE2, TYPE3):
            await crawler.temp_storage.save_cleaned_html(address, crawler.url_normalizer.normalize_url(address),
                                                         'legacy html', COMPANY, category='product')
        assert scans == ['*.json']
        assert len(await crawler.temp_storage._find_existing_files(crawler.url_normalizer.normalize_url(TYPE3), COMPANY, 'product')) == 1
    asyncio.run(run())


def test_reset_index_sees_external_legacy_file(crawler):
    async def run():
        assert await crawler.temp_storage._find_existing_files(crawler.url_normalizer.normalize_url(TYPE2), COMPANY, 'product') == []
        old = legacy_record(crawler.temp_storage, TYPE2)
        crawler.temp_storage.reset_url_index()
        assert await crawler.temp_storage._find_existing_files(crawler.url_normalizer.normalize_url(TYPE2), COMPANY, 'product') == [old]
    asyncio.run(run())


def test_parallel_saves_reuse_one_legacy_path(crawler):
    old = legacy_record(crawler.temp_storage, TYPE2)
    async def run():
        results = await asyncio.gather(*(crawler.temp_storage.save_cleaned_html(
            TYPE2, crawler.url_normalizer.normalize_url(TYPE2), 'legacy html', COMPANY, category='product')
            for _ in range(5)))
        assert {result[0] for result in results} == {str(old)}
        assert not any(result[1] for result in results)
        assert len(list(old.parent.glob('*.html'))) == 1
    asyncio.run(run())


def test_changed_legacy_version_keeps_archive_and_uses_new_identity(crawler):
    old = legacy_record(crawler.temp_storage, TYPE2)
    old_html = old.read_bytes()
    old_json = old.with_suffix('.json').read_bytes()
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        newer, is_new, metadata = await crawler.temp_storage.save_cleaned_html(
            TYPE2, crawler.url_normalizer.normalize_url(TYPE2), 'changed html', COMPANY, category='product')
        assert is_new and newer != str(old)
        assert not metadata['checkpoints']['ai_processed']
        assert await crawler.temp_storage._find_existing_files(crawler.url_normalizer.normalize_url(TYPE2), COMPANY, 'product') == [Path(newer)]
        assert old.read_bytes() == old_html
        assert old.with_suffix('.json').read_bytes() == old_json
    asyncio.run(run())


def test_index_reset_applies_changed_normalizer(tmp_path):
    storage = TempStorageManager(str(tmp_path), config=Config(), url_normalizer=lambda address: address)
    old = legacy_record(storage, TYPE2)
    async def run():
        assert await storage._find_existing_files(TYPE2, COMPANY, 'product') == [old]
        storage.url_normalizer = lambda address: address.replace('/catalog', '/profile-catalog')
        storage.reset_url_index()
        assert await storage._find_existing_files(TYPE2, COMPANY, 'product') == [old]
        assert storage._url_index[old.parent].get(TYPE2.replace('/catalog', '/profile-catalog')) == {old}
    asyncio.run(run())
