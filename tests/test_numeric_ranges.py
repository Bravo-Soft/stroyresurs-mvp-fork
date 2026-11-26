"""Числовой порог применяется к явным диапазонам разрешённых параметров."""
import pytest

from product_preparation import count_numeric_characteristics


def test_uralux_visible_specifications():
    product = {'specifications': {
        'Расход': '250-300 г\\м² (три слоя)',
        'Время высыхания': 'при температуре 20 °С -6 ч. (для каждого слоя)',
        'Гарантийный срок': '12–24 мес.',
        'Фасовка': {'Масса': '1–5 кг'},
    }}
    assert count_numeric_characteristics(product) == 3


@pytest.mark.parametrize('value', [
    '0,25–0,30 мм', 'от 0.25 до 0.30 мм', 'от −20 до +50 C', '-30..-5 C',
    '-20 — -5 °C', '5…8 мм', '5 мм – 8 мм', 'от 5 мм до 8 мм', 'от 20 см до 1 м', '2-4 ч при 20 C',
    'при температуре 20 C — 4–6 ч', 'нагрев 10–20, охлаждение 8–12',
])
def test_two_range_endpoints(value):
    assert count_numeric_characteristics({'specifications': {'Параметр': value}}) == 2


@pytest.mark.parametrize('value', [
    'до 300 г/м²', 'от 250 г/м²', '250 / 300', '250, 300 или 350', '250 ± 30',
    '6 ч при температуре от 18 до 20 C', '6 ч (при 18–20 °C)', '250 г/м² (2–3 слоя)',
    'при 20 °C -6 ч', '4 ч по ГОСТ 1234-2020', '2026-11-24', 'ПФ-115-20',
    ['120 г/м² по дереву', '150 г/м² по бетону'],
])
def test_scalar_list_or_condition(value):
    assert count_numeric_characteristics({'specifications': {'Параметр': value}}) == 1


def test_ranges_in_exclusions_axes_and_legacy_fields():
    assert count_numeric_characteristics({'specifications': {
        'Гарантия': {'Срок': '1–3 года'}, 'Фасовка': '1-3 кг', 'Цена': '7–9 руб.',
        'Норматив': 'ГОСТ 1234-2020', 'Срок хранения': '1–2 года'}}) == 0
    assert count_numeric_characteristics({'dimensions': '10–20×30×40 мм', 'weight': '1–2 кг'}) == 6
    assert count_numeric_characteristics({'dimensions': '40 мм × 50 мм'}) == 2
    assert count_numeric_characteristics({'specifications': {'Модель А': {'Расход': ['100–120', '100–120']}}}) == 2


@pytest.mark.parametrize('dimension', ['450-450', '900-700'])
def test_ambiguous_bracket_size_is_not_an_interval(dimension):
    assert count_numeric_characteristics({'specifications': {'Размер, мм': dimension}}) == 1


def test_packaging_mass_range_is_not_a_product_property():
    assert count_numeric_characteristics({'specifications': {'Масса одной паковки ровинга, кг': '1 — 15'}}) == 0


@pytest.mark.parametrize('domain', ['mlkz.by', 'another.example'])
def test_previous_rejection_is_rechecked_once(domain, tmp_path):
    import asyncio
    import json
    from pathlib import Path

    import main
    from config import Config
    from product_extraction_revision import NUMERIC_GATE_REVISION
    from test_crawl_refresh_resume import build_system, save, COMPANY
    from web_crawler import WebCrawler

    async def run():
        config = Config()
        config.base_dir = str(tmp_path)
        crawler = WebCrawler(config)
        system = build_system(crawler, tmp_path)
        company = {'company_id': '896932339', 'original_name': COMPANY}
        stats = main.CompanyStatistics(company)
        path, _, metadata = await save(crawler, url=f'https://{domain}/product/coat')
        page = {'normalized_url': metadata['normalized_url'], 'file_path': path, 'category': 'product'}
        model = system.ai_client.extract_product_info
        usage = {'input_tokens': 1, 'output_tokens': 1}
        model.return_value = ({'error': 'Trash_418#'}, usage)
        assert await system._process_single_page_with_checkpoint(page, company, stats, str(tmp_path)) is None
        meta_path = Path(path).with_suffix('.json')
        saved = json.loads(meta_path.read_text())
        assert saved.pop('numeric_gate_revision') == NUMERIC_GATE_REVISION
        meta_path.write_text(json.dumps(saved))
        for _ in range(2):
            await system._prepare_company_crawl(company)
            assert await system._process_single_page_with_checkpoint(page, company, stats, str(tmp_path)) is None
        assert model.await_count == 2
        assert json.loads(meta_path.read_text())['numeric_gate_revision'] == NUMERIC_GATE_REVISION

    asyncio.run(run())
