"""Относительные адреса Keddy разрешаются по HTML base, а не по /cat/."""
import asyncio
from pathlib import Path
from urllib.parse import urljoin

import pytest
from bs4 import BeautifulSoup

from config import Config
from web_crawler import WebCrawler


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    return WebCrawler(cfg)


def links(crawler, html, url='https://pechi-keddy.ru/cat/'):
    return asyncio.run(crawler._extract_additional_links(
        BeautifulSoup(html, 'html.parser'), url))


def test_saved_keddy_catalog(crawler):
    html = (Path(__file__).parent / 'fixtures/recrawl/keddy_catalog_20261023.html').read_text()
    soup = BeautifulSoup(html, 'html.parser')
    expected = {urljoin('https://pechi-keddy.ru/', a['href'])
                for a in soup.select('a.mn-card__title[href]')}
    assert len(expected) == 24
    actual = {u for u, _, _ in links(crawler, html)}
    assert expected <= actual
    assert 'https://pechi-keddy.ru/cat/?page=2' in actual
    assert 'https://pechi-keddy.ru/cat/?page=3' in actual
    assert 'https://pechi-keddy.ru/contacts.html' in actual
    assert not any('/cat/cat/' in u or '/cat/pechi-kaminy/' in u for u in actual)


@pytest.mark.parametrize(('base', 'expected'), [
    ('<base href="https://pechi-keddy.ru/">', 'https://pechi-keddy.ru/product.html'),
    ('<base href="/store/">', 'https://pechi-keddy.ru/store/product.html'),
    ('<base href="../">', 'https://pechi-keddy.ru/product.html'),
    ('<base href="//pechi-keddy.ru/store/">', 'https://pechi-keddy.ru/store/product.html'),
    ('<base target="_blank"><base href="/store/">', 'https://pechi-keddy.ru/store/product.html'),
    ('<base href="/first/"><base href="/second/">', 'https://pechi-keddy.ru/first/product.html'),
    ('<base href=""><base href="/second/">', 'https://pechi-keddy.ru/cat/product.html'),
    ('<base href=" javascript:alert(1)">', 'https://pechi-keddy.ru/cat/product.html'),
    ('<base href="data:text/html,foo">', 'https://pechi-keddy.ru/cat/product.html'),
    ('<base href="https://[broken">', 'https://pechi-keddy.ru/cat/product.html'),
    ('', 'https://pechi-keddy.ru/cat/product.html'),
])
def test_html_base_resolution(crawler, base, expected):
    actual = {u for u, _, _ in links(crawler, base + '<a href="product.html">Товар</a>')}
    assert expected in actual


def test_grid_and_onclick_use_html_base(crawler):
    html = ('<base href="https://pechi-keddy.ru/">'
            '<div class="product-card"><a href="products/a.html">Товар</a>'
            '<span data-href="products/b.html">Товар</span></div>')
    soup = BeautifulSoup(html, 'html.parser')
    grid = crawler.product_grid_parser.extract_product_links(
        soup, 'https://pechi-keddy.ru/', 'https://pechi-keddy.ru/cat/')
    expected = {'https://pechi-keddy.ru/products/a.html', 'https://pechi-keddy.ru/products/b.html'}
    assert {u for u, _, _ in grid} == expected
    assert expected <= {u for u, _, _ in links(crawler, html)}


def test_files_use_html_base(crawler):
    soup = BeautifulSoup('<base href="https://pechi-keddy.ru/">'
                         '<a href="manual.pdf">Инструкция</a>', 'html.parser')
    assert crawler._extract_file_links_from_soup(soup, 'https://pechi-keddy.ru/cat/') == [
        'https://pechi-keddy.ru/manual.pdf']


def test_canonical_does_not_replace_document_url(crawler):
    html = '<link rel="canonical" href="https://pechi-keddy.ru/"><a href="product.html">Товар</a>'
    assert 'https://pechi-keddy.ru/cat/product.html' in {u for u, _, _ in links(crawler, html)}


def test_keddy_profile_classifies_saved_details(crawler):
    from site_profiles.resolver import ProfileResolver
    profile = ProfileResolver(Path(__file__).parents[1] / 'profiles').resolve('https://pechi-keddy.ru/')
    assert profile is not None
    crawler.url_categorizer.set_profile(profile)
    html = (Path(__file__).parent / 'fixtures/recrawl/keddy_catalog_20261023.html').read_text()
    actual = links(crawler, html)
    details = [(u, role) for u, role, _ in actual if '/pechi-kaminy/keddy/' in u]
    assert len(details) == 24
    assert all(role == 'product' for _, role in details)
    assert crawler.url_categorizer.categorize_url('https://pechi-keddy.ru/cat/?page=2')[0] == 'category'
    assert crawler.url_categorizer.categorize_url('https://pechi-keddy.ru/cat/')[0] == 'category'
