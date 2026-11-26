import json
from pathlib import Path
from bs4 import BeautifulSoup
from text_extractor import html_to_markdown, _site_product_container
from inline_products import isolate_inline_product

W = Path(__file__).resolve().parents[2]
JOB = next(j for j in json.loads((W / 'replay_jobs.json').read_text()) if j['url'].endswith('/vmp-roving/'))
HTML = Path(JOB['html']).read_text()


def test_vmp_current_product_does_not_include_footer_catalog_and_forms():
    md = html_to_markdown(HTML, JOB['url'])
    assert 'ВМП' in md or 'Высокомодульный' in md
    assert 'Гибкие элементы термических компенсаторов' not in md
    assert 'Отправляя данную форму' not in md
    assert 'Политикой обработки персональных данных' not in md
    assert len(md) < 5000


def test_product_route_does_not_apply_to_contacts_or_company_or_foreign_domain():
    soup = BeautifulSoup(HTML, 'lxml')
    for url in ['https://npo-stekloplastic.ru/contacts/', 'https://npo-stekloplastic.ru/', 'https://example.org/products/item']:
        assert _site_product_container(soup, url) is None


def test_isolated_npo_families_remain_main_without_hentry():
    for job in json.loads((W / 'npo_family_jobs.json').read_text()):
        html = isolate_inline_product(Path(job['html']).read_text(), job['url'])
        assert html is not None
        soup = BeautifulSoup(html, 'lxml')
        assert soup.main is not None and soup.select_one('.di-page-contents.hentry') is None
        assert _site_product_container(soup, job['url']) is None
        md = html_to_markdown(html, job['url'])
        assert soup.h1.get_text(' ', strip=True) in md


def test_company_and_distributor_paths_do_not_call_product_container(monkeypatch):
    import text_extractor
    def forbidden(*args):
        raise AssertionError('product container used for company/distributor')
    monkeypatch.setattr(text_extractor, '_site_product_container', forbidden)
    html = '<html><head><title>Компания</title></head><body><h1>Компания</h1><main><p>' + ('Сведения о компании. ' * 30) + '</p></main></body></html>'
    for page_type in ['company', 'distributor']:
        md = html_to_markdown(html, 'https://www.zavodmel.ru/products/example/', page_type=page_type)
        assert 'Компания' in md
