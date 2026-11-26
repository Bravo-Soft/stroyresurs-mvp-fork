"""Правила блоков, воспроизведённые по аудиту Tegola, и защита полезного текста."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from product_quality import normalize_product_fields

CASES = json.loads((Path(__file__).parent / 'fixtures/tegola_quality/document_titles.json').read_text())


@pytest.mark.parametrize('case', CASES)
def test_audited_document_titles_are_not_application_instructions(case):
    data = {'product': {'instructions_manuals': case['instructions']}}
    normalize_product_fields(data, case['source'])
    assert data['product']['instructions_manuals'] == ''


@pytest.mark.parametrize('label', ['Количество в поддоне', 'Количество упаковок на паллете, шт.',
                                 'Количество рулонов на паллете', 'Коробок на поддоне, шт.'])
def test_pallet_quantities_move_with_variant_context_and_are_idempotent(label):
    data = {'product': {'specifications': {'Модель А': {label: '40', 'Толщина, мм': '2'}},
                        'complectation': '- Упаковка: пленка', 'colors': ['коричневый', 'Коричневый']}}
    normalize_product_fields(data)
    p = data['product']
    assert p['specifications'] == {'Модель А': {'Толщина, мм': '2'}}
    assert f'Модель А: {label}: 40' in p['complectation']
    assert 'Упаковка: пленка' in p['complectation']
    assert p['colors'] == ['коричневый']
    once = deepcopy(data)
    normalize_product_fields(data)
    assert data == once


def test_actual_instructions_after_document_title_survive():
    title = 'Инструкция по монтажу мембраны'
    data = {'product': {'instructions_manuals': '- ' + title + '; Укладывать с нахлестом 100 мм.\nТемпература применения: 0/+45'}}
    normalize_product_fields(data, f'[{title}](https://example.org/instruction.pdf)')
    assert data['product']['instructions_manuals'] == 'Укладывать с нахлестом 100 мм.\nТемпература применения: 0/+45'


def test_title_word_does_not_remove_real_instruction_or_invent_pdf_contents():
    data = {'product': {'instructions_manuals': 'Инструкция: нанести два слоя кистью.\nНе укладывать на влажное основание.',
                        'specifications': {'Грузоподъемность поддона, кг': '1000'}}}
    original = deepcopy(data)
    normalize_product_fields(data, '[Инструкция по монтажу](https://example.org/manual.pdf)')
    assert data == original


def test_rejected_result_never_becomes_product():
    data = {'error': 'Trash_418#'}
    normalize_product_fields(data, 'Длина: 10. Ширина: 2. Толщина: 3.')
    assert data == {'error': 'Trash_418#'}


def test_single_model_pipeline_applies_rules_before_returning_json():
    import asyncio
    from ai_integration import AITunnelClient
    title = 'Инструкция по устройству плоских крыш'
    response = {'product': {'specifications': {'Количество рулонов на паллете': '30', 'Толщина': '3 мм'},
                            'instructions_manuals': '- ' + title + '\nНанести кистью.'}}
    client = object.__new__(AITunnelClient)
    client.model = 'gemma4-31b-nvfp4'
    async def request(data):
        return {'choices': [{'message': {'content': json.dumps(response)}}]}, {'input_tokens': 1, 'output_tokens': 1}
    client._make_request_with_retry = request
    result, _ = asyncio.run(client.extract_product_info(f'[{title}](https://example.org/file.pdf)', 'Tegola', 'https://example.org'))
    assert result['product']['specifications'] == {'Толщина': '3 мм'}
    assert result['product']['complectation'] == '- Количество рулонов на паллете: 30'
    assert result['product']['instructions_manuals'] == 'Нанести кистью.'
