"""Тесты скачивания общих документов компании (4 блока профиля ->
папки Certificates/Documents/Instructions/Price_lists)."""
import asyncio
import types

import pytest

from config import Config
from site_profiles import profile_from_dict
from site_profiles.resolver import DEFAULT_PROFILES_DIR, ProfileResolver
from web_crawler import FileDownloadManager, URLCategorizer


@pytest.fixture
def categorizer():
    return URLCategorizer(Config())


@pytest.fixture
def manager():
    crawler_stub = types.SimpleNamespace(profile=None, host_throttle=None,
                                         processing_tracker=None, url_categorizer=None)
    return FileDownloadManager(crawler_stub, Config())


def _profile_with_blocks():
    profile, errors = profile_from_dict({'domain': 'a.ru', 'sections': {
        'certificates_urls': ['/docs/certificates/'],
        'documents_urls': ['/docs/'],
        'instructions_urls': ['/docs/manuals/'],
        'price_list_urls': ['/price/'],
    }})
    assert errors == []
    return profile


# ==================== profile_document_section ====================

def test_document_section_mapping(categorizer):
    categorizer.set_profile(_profile_with_blocks())
    sec = categorizer.profile_document_section
    # самый длинный префикс побеждает: /docs/certificates/ вложен в /docs/
    assert sec('https://a.ru/docs/certificates/gost.pdf') == 'certificates'
    assert sec('https://a.ru/docs/manuals/install/') == 'instructions'
    assert sec('https://a.ru/docs/passport.html') == 'documents'
    assert sec('https://a.ru/price/2026/') == 'price_list'
    assert sec('https://a.ru/catalog/item-1/') is None


def test_document_section_without_profile(categorizer):
    assert categorizer.profile_document_section('https://a.ru/docs/') is None
    categorizer.set_profile(None)
    assert categorizer.profile_document_section('https://a.ru/docs/') is None


def test_belcolor_document_sections(categorizer):
    profile = ProfileResolver(DEFAULT_PROFILES_DIR).resolve('https://www.belcolor.ru/')
    categorizer.set_profile(profile)
    sec = categorizer.profile_document_section
    assert sec('https://www.belcolor.ru/services/deklaratsii-sootvetstviya/') == 'certificates'
    assert sec('https://www.belcolor.ru/services/svidetelstva-o-gosregistratsii/x/') == 'certificates'
    assert sec('https://www.belcolor.ru/services/pasporta-bezopasnosti/') == 'documents'
    assert sec('https://www.belcolor.ru/services/') == 'documents'
    assert sec('https://www.belcolor.ru/contacts/') is None


# ==================== extract_document_links ====================

HTML = '''<html><body>
<a href="/files/passport.pdf">Паспорт</a>
<a href="doc2.docx">Декларация</a>
<a href="https://cdn.a.ru/price.xlsx#sheet1">Прайс</a>
<a href="/files/passport.pdf">дубль</a>
<a href="/catalog/item.html">не файл</a>
<a href="/download/page/">паттерн без расширения — не берём</a>
</body></html>'''


def test_extract_document_links(manager):
    links = manager.extract_document_links(HTML, 'https://a.ru/docs/')
    assert links == ['https://a.ru/files/passport.pdf',
                     'https://a.ru/docs/doc2.docx',
                     'https://cdn.a.ru/price.xlsx']


# ==================== download_document_files ====================

def test_download_document_files(manager, tmp_path, monkeypatch):
    downloaded = []

    async def fake_download(file_url, save_dir, page_category=None):
        downloaded.append(file_url)
        return f'{save_dir}/{file_url.rsplit("/", 1)[-1]}'

    monkeypatch.setattr(manager, '_download_aiohttp', fake_download)
    save_dir = tmp_path / 'Certificates'

    saved = asyncio.run(manager.download_document_files(HTML, 'https://a.ru/docs/', str(save_dir)))
    assert len(saved) == 3
    assert save_dir.is_dir()
    # повторный вызов: кэш скачанных гасит дубли
    saved2 = asyncio.run(manager.download_document_files(HTML, 'https://a.ru/docs/', str(save_dir)))
    assert saved2 == []
    assert len(downloaded) == 3


def test_download_document_files_empty_page(manager, tmp_path):
    saved = asyncio.run(manager.download_document_files(
        '<html><body>без файлов</body></html>', 'https://a.ru/docs/', str(tmp_path / 'x')))
    assert saved == []
    assert not (tmp_path / 'x').exists()  # папка не создаётся впустую
