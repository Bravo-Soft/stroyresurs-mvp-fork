import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from docx import Document

from main import CompanyStatistics, MonitoringSystem
from product_extraction_revision import product_extraction_revision
from profnastil_configurator import FENCE, GUIDE, SALE, SHEET, restore_configurator
from test_crawl_refresh_resume import build_system, crawler


def site_url(slug):
    return 'https://profnastilspb.ru/product/' + slug + '/'


def test_scoped_revision_and_rejections():
    for slug in [FENCE, GUIDE, SALE, SHEET]:
        assert product_extraction_revision(site_url(slug))
    for url in [site_url(FENCE + '-2'), site_url(FENCE).replace('profnastilspb.ru', 'example.org')]:
        assert product_extraction_revision(url) is None
    html = '<h1>Ограждение «жалюзи»</h1>'
    for result in [None, {'error': 'Trash_418#'}, {'error': 'Trash_418#', 'source_rejection': 'catalog'}]:
        before = repr(result)
        restore_configurator(result, html, site_url(FENCE))
        assert repr(result) == before


@pytest.mark.parametrize('reverse', [False, True])
def test_update_archived_configurators(corpus_dir, crawler, tmp_path, reverse):
    folder = corpus_dir / 'Профиль_ПК_ООО' / 'Product_pages'
    # Снимки до исправления. Более поздний обход может обновить их метаданные.
    snapshots = {
        GUIDE: '*5695379daa9897ea_2f8d857b2d04a859.html',
        SALE: '*7d62d20fa0a24fb9_11531e9331200f5f.html',
        SHEET: '*8deb7e28d85b931f_fc8b4f18a7dcd5a1.html',
        FENCE: '*2cedb83b076c7afb_7273cf4f8dc907af.html',
    }
    sources = {slug: next(folder.glob(pattern), None) for slug, pattern in snapshots.items()}
    if not all(sources.values()):
        pytest.skip('Нет снимков четырёх проверенных конфигураторов')
    company = {'company_id': '896707455', 'original_name': 'Профиль ПК, ООО', 'actual_name': 'Профиль ПК'}

    async def run():
        system = build_system(crawler, tmp_path)
        system._get_product_folder_path = MonitoringSystem._get_product_folder_path.__get__(system)
        sale_product = {'product': {'product_name': 'Профиль направляющий для гипсокартона 60х27 (Профиль ПК)',
                                    'specifications': {'Ширина': '60 мм', 'Высота': '27 мм'}}}
        sheet_product = {'product': {'product_name': 'Профнастил С8-1150 (Профиль ПК)',
                                     'specifications': {'Ширина рабочая': '1150 мм', 'Высота': '8 мм'}}}
        keys = [FENCE, SHEET, SALE, GUIDE]
        if reverse:
            keys.reverse()
        await system._prepare_company_crawl(company)
        pages, ids = {}, {}
        for slug in keys:
            url = site_url(slug)
            path = sources[slug]
            norm = crawler.url_normalizer.normalize_url(url)
            saved, _, _ = await crawler.temp_storage.save_cleaned_html(
                url, norm, path.read_text(), company['original_name'], category='product')
            sidecar = Path(saved).with_suffix('.json')
            meta = json.loads(sidecar.read_text())
            meta['checkpoints']['ai_processed'] = True
            if slug in (SALE, SHEET):
                meta['product_result'] = {'product_id': 'stale', 'source_url': url}
            sidecar.write_text(json.dumps(meta))
            pages[slug] = {'file_path': saved, 'normalized_url': norm, 'category': 'product'}
            previous = sheet_product if slug == SHEET else sale_product
            ai = {'error': 'Trash_418#'} if slug == FENCE else json.loads(json.dumps(previous))
            system.ai_client.extract_product_info = AsyncMock(return_value=(ai, {'input_tokens': 0, 'output_tokens': 0}))
            result = await system._process_single_page_with_checkpoint(
                pages[slug], company, CompanyStatistics(company), str(tmp_path))
            assert result is not None
            system.ai_client.extract_product_info.assert_awaited_once()
            ids[slug] = result['product_id']
            product = result['ai_raw_text']['product']
            md = system._save_markdown_file.await_args.args[-1]
            card = next((Path(tmp_path) / 'Products' / Path(result['product_folder']).name).glob('*.docx'))
            document = Document(card)
            body = ' '.join(document.element.body.itertext())
            expected = {FENCE: ['0,5 мм', '1,5 / 1,8 / 2 м', '1 / 1,5 / 2 / 2,5 / 3 м'],
                        GUIDE: ['0,5 / 0,6 / 0,7 мм'], SALE: ['0,5 / 0,7 мм', '3000 мм'],
                        SHEET: ['1200 мм']}[slug]
            for value in expected:
                assert value in md and value in body and value in product['specifications'].values()
            if slug == FENCE:
                assert product['price'] == '2296.41' and len(product['colors']) == 13
            assert json.loads(sidecar.read_text())['product_extraction_revision'] == product_extraction_revision(url)
        assert len(set(ids.values())) == 4
        assert ids[GUIDE] == 'pr_40a740ee82d759a69d11904afc42'
        await system._prepare_company_crawl(company)
        system.ai_client.extract_product_info = AsyncMock(side_effect=AssertionError('Повторный вызов модели'))
        for slug in keys:
            result = await system._process_single_page_with_checkpoint(
                pages[slug], company, CompanyStatistics(company), str(tmp_path))
            assert result and result['product_id'] == ids[slug]
        system.ai_client.extract_product_info.assert_not_awaited()
    asyncio.run(run())
