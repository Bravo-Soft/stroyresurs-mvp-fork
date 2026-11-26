"""Цветовые исполнения с одинаковым ответом модели и строгий числовой гейт."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from product_id_manager import ProductIDManager, restore_source_variant_name

NAME = 'Мембрана Safety PLAST Ceramic АПП ЭКП 4мм (TEGOLA)'
COMPANY = {'original_name': 'Tegola Canadese, Италия', 'company_id': '925700928'}


def source(color='Brown', slug='brown-mix'):
    slug = 'safety-plast-ceramic-app-ekp-4mm-' + slug
    p = {'title': 'Safety PLAST Ceramic (' + color + ' mix)', 'slug': slug,
         'variants': [{'title': 'Сейфити ПЛАСТ Керамик (' + color + ' mix)',
                       'color': {'title': color}}],
         'tab_features': [{'title': k, 'value': v} for k, v in
                          [('Длина рулона, м', '8'), ('Ширина рулона, м', '1'), ('Толщина, мм', '4')]]}
    state = json.dumps({'data': [{'product': p}]}, ensure_ascii=False)
    return '<script>window.__NUXT__=(function(){return ' + state + ';}());</script>', 'https://tegola.ru/product/' + slug


def ai():
    return {'product': {'product_name': NAME, 'specifications': {
        'Длина рулона, м': '8', 'Ширина рулона, м': '1', 'Толщина, мм': '4'}}}


def test_distinct_source_colors_get_distinct_stable_names_and_ids():
    manager = ProductIDManager(); names = []; ids = []
    for color, slug in [('Brown', 'brown-mix'), ('Light Grey', 'light-grey-mix')]:
        html, url = source(color, slug); data = ai()
        restore_source_variant_name(data, html, url)
        name = data['product']['product_name']
        assert name.endswith('(' + color + ' mix) (TEGOLA)')
        once = deepcopy(data)
        restore_source_variant_name(data, html, url)
        assert data == once
        names.append(name); ids.append(manager.generate_product_id_from_name_bag(name, COMPANY['original_name']))
    assert len(set(names)) == len(set(ids)) == 2
    assert ids[0] == manager.generate_product_id_from_name_bag(names[0], COMPANY['original_name'])


@pytest.mark.parametrize('kind', ['ordinary', 'existing_qualifier', 'foreign_host', 'wrong_slug'])
def test_unrelated_and_already_distinguished_names_keep_legacy_id(kind):
    html, url = source(); data = ai()
    if kind == 'ordinary':html = html.replace('Brown mix', 'Brown finish')
    elif kind == 'existing_qualifier':data['product']['product_name'] = NAME.replace(' (TEGOLA)', ' (Brown mix) (TEGOLA)')
    elif kind == 'foreign_host':url = url.replace('tegola.ru', 'factory.ru')
    else:url += '-other'
    original = deepcopy(data); manager = ProductIDManager()
    old_id = manager.generate_product_id_from_name_bag(data['product']['product_name'], COMPANY['original_name'])
    restore_source_variant_name(data, html, url)
    assert data == original
    assert manager.generate_product_id_from_name_bag(data['product']['product_name'], COMPANY['original_name']) == old_id


def test_pipeline_preserves_two_color_pages_and_deduplicates_same_url(tmp_path, monkeypatch):
    import main
    from test_http_https_product_dedup import system_stub
    from web_crawler import WebCrawler
    from config import Config
    cfg = Config(); cfg.base_dir = str(tmp_path); crawler = WebCrawler(cfg)
    stub = system_stub(crawler, tmp_path, monkeypatch, [ai(), ai(), ai()])
    folders = []
    def folder(company_dir, product_id, name):
        path = main.MonitoringSystem._get_product_folder_path(stub, company_dir, product_id, name)
        folders.append(path[0]); return path
    stub._get_product_folder_path = Mock(side_effect=folder)
    stats = main.CompanyStatistics(COMPANY)
    async def run():
        result = []
        for color, slug in [('Brown', 'brown-mix'), ('Light Grey', 'light-grey-mix'), ('Light Grey', 'light-grey-mix')]:
            html, url = source(color, slug)
            result.append(await main.MonitoringSystem.process_product_page(stub, html, COMPANY, stats,
                          {'url': url, 'normalized_url': url, 'storage_path': url, 'category': 'product'}, str(tmp_path)))
        return result
    results = asyncio.run(run()); products = [x for x in results if x]
    assert len(products) == 2 and results[-1] is None
    assert len({p['product_id'] for p in products}) == 2
    assert len({p['source_url'] for p in products}) == 2
    assert len(set(folders)) == 2
    assert stub.docx_generator.generate_product_card.await_count == 2


@pytest.mark.parametrize('specifications,expected', [
    ({'Длина': '3', 'Типоразмер': '90/125', 'Гарантия': '50 лет'}, 2),
    ({'ГОСТ': '123-2020', 'Количество в упаковке': '50', 'Цвет': 'RAL 8017', 'Артикул': '123'}, 0),
    ({'Размер, мм': '1205x415', 'Толщина, мм': '0.5'}, 3),
    ({'Габариты, мм': '1180 / 1280', 'Ширина, мм': '1280'}, 2),
    ({'Размеры': '12×12×123'}, 3),
    ({'Покрытие': 'Без покрытия / Zn / Zn 275', 'Типоразмер': '90 / 125'}, 1),
    ({'Рабочая температура изделия': '-20..80 °C', 'Температура окружающего воздуха при монтаже': '5 °C'}, 2),
])
def test_gate_counts_properties_not_codes_packaging_or_guarantee(specifications, expected):
    from main import count_numeric_characteristics
    assert count_numeric_characteristics({'specifications': specifications}) == expected


@pytest.mark.parametrize('specifications,expected', [
    ({'Модель A': {'Длина': '100 мм', 'Ширина': '20 мм', 'Высота': '5 мм'}}, 3),
    ({'Срок полного отверждения': '24 ч', 'Монтажный размер': '1205x415'}, 3),
    ({'Срок хранения': '12 мес', 'Срок службы': '50 лет'}, 0),
    ({'Длина': ['10', '20', '30']}, 1),
    ({'Размер': ['10x20', '30x40']}, 2),
    ({'Морозостойкость': 'F3000'}, 1),
    ({'Упаковка': {'Длина': '100 мм', 'Ширина': '20 мм', 'Высота': '5 мм'}}, 0),
])
def test_gate_nested_models_and_parameter_lists(specifications, expected):
    from main import count_numeric_characteristics
    assert count_numeric_characteristics({'specifications': specifications}) == expected


@pytest.mark.parametrize('first_trash', [False, True])
def test_tegola_legacy_cache_reextracts_once_then_current_revision_reuses(tmp_path, first_trash):
    from unittest.mock import AsyncMock
    import main
    from config import Config
    from web_crawler import WebCrawler
    from test_crawl_refresh_resume import build_system, save, COMPANY as STORED_COMPANY
    from product_extraction_revision import TEGOLA_EXTRACTION_REVISION
    async def run():
        cfg = Config(); cfg.base_dir = str(tmp_path); crawler = WebCrawler(cfg)
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': STORED_COMPANY}
        url = 'https://tegola.ru/product/revision-test'
        path, _, metadata = await save(crawler, url=url)
        page = {'normalized_url': metadata['normalized_url'], 'file_path': path, 'category': 'product'}
        if first_trash:
            system.ai_client.extract_product_info.return_value = ({'error': 'Trash_418#'}, {'input_tokens': 1, 'output_tokens': 1})
        await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        # Симулируем положительный/отрицательный кэш до внедрения новой версии.
        mp = Path(path).with_suffix('.json'); meta = json.loads(mp.read_text()); meta.pop('product_extraction_revision', None); mp.write_text(json.dumps(meta))
        await system._prepare_company_crawl(company)
        system.ai_client.extract_product_info.return_value = (ai(), {'input_tokens': 1, 'output_tokens': 1})
        result = await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        assert result is not None
        assert system.ai_client.extract_product_info.await_count == 2
        assert json.loads(mp.read_text())['product_extraction_revision'] == TEGOLA_EXTRACTION_REVISION
        await system._prepare_company_crawl(company)
        result = await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        assert result is not None and system.ai_client.extract_product_info.await_count == 2
    asyncio.run(run())


def test_tegola_rejected_new_revision_does_not_resurrect_old_product(tmp_path):
    import main
    from config import Config
    from web_crawler import WebCrawler
    from test_crawl_refresh_resume import build_system, save, COMPANY as STORED_COMPANY
    async def run():
        cfg = Config(); cfg.base_dir = str(tmp_path); crawler = WebCrawler(cfg)
        system = build_system(crawler, tmp_path); company = {'company_id': '1', 'original_name': STORED_COMPANY}
        path, _, metadata = await save(crawler, url='https://tegola.ru/product/revision-test')
        page = {'normalized_url': metadata['normalized_url'], 'file_path': path, 'category': 'product'}
        assert await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        mp = Path(path).with_suffix('.json'); meta = json.loads(mp.read_text()); meta.pop('product_extraction_revision', None); mp.write_text(json.dumps(meta))
        await system._prepare_company_crawl(company)
        system.ai_client.extract_product_info.return_value = ({'error': 'Trash_418#'}, {'input_tokens': 1, 'output_tokens': 1})
        assert await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path)) is None
        assert 'product_result' not in json.loads(mp.read_text())
        await system._prepare_company_crawl(company)
        assert await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path)) is None
        assert system.ai_client.extract_product_info.await_count == 2
    asyncio.run(run())
