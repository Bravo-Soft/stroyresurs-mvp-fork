import copy
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent

from bitumex_source_sections import restore_bitumex_source_sections
from main import count_numeric_characteristics

FIXTURES = HERE / 'fixtures/bitumex_source_sections'
CASES = json.loads((FIXTURES / 'cases.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'])
def test_real_pages_idempotence_and_gate(case):
    data = copy.deepcopy(case['data'])
    html = (FIXTURES / (case['id'] + '.html')).read_text()
    before = count_numeric_characteristics(data.get('product', {}))
    restore_bitumex_source_sections(data, html, case['url'])
    assert count_numeric_characteristics(data.get('product', {})) == before
    once = copy.deepcopy(data)
    assert not restore_bitumex_source_sections(data, html, case['url'])
    assert data == once


def sample():
    case = next(c for c in CASES if c['id'] == '3905dbcaec6636a1')
    return copy.deepcopy(case['data']), (FIXTURES / (case['id'] + '.html')).read_text(), case['url']


@pytest.mark.parametrize('kind', ['host', 'trash', 'hidden_control', 'template', 'hidden_owner'])
def test_no_access_or_no_product_is_unchanged(kind):
    data, html, url = sample()
    if kind == 'host':
        url = url.replace('bitumex.org', 'other.example')
    elif kind == 'trash':
        data = {'error': 'Trash_418#'}
    elif kind == 'hidden_control':
        html = html.replace('id="tab-title-description"', 'hidden id="tab-title-description"')
    elif kind == 'template':
        html = '<template>' + html + '</template>'
    else:
        html = html.replace('class="woocommerce-tabs ', 'hidden class="woocommerce-tabs ')
    before = copy.deepcopy(data)
    assert not restore_bitumex_source_sections(data, html, url)
    assert data == before


def test_conditional_water_properties_preserve_context():
    data, html, url = sample()
    assert restore_bitumex_source_sections(data, html, url)
    value = data['product']['specifications']['Состав и свойства материала']
    assert 'более 30 % от количества материала' in value
    assert 'Характер набухания материала – равновесный' in value
    assert 'нейтральный рН и не способствует коррозии арматуры' in value


def test_hidden_descendant_is_not_a_source():
    data, html, url = sample()
    html = html.replace('Характер набухания материала – равновесный.',
                        '<span hidden>СЕКРЕТ 999</span>')
    restore_bitumex_source_sections(data, html, url)
    assert 'СЕКРЕТ 999' not in json.dumps(data, ensure_ascii=False)


def test_distinct_water_conditions_and_curing_are_retained():
    for identifier, field, facts in [
        ('cc4c0bc612a48675', 'specifications', ['более 500 % воды']),
        ('cc4c0bc612a48675', 'instructions_manuals', ['в отсутствие противодавления', 'на 100 %']),
        ('0f7557c37d8bf52d', 'instructions_manuals', ['Время полного отверждения', 'температуры окружающей среды и влажности воздуха']),
        ('5ed884f39c7b8f18', 'specifications', ['умеренно вязкая колерованная жидкость', 'жидкость коричневого оттенка']),
    ]:
        case = next(c for c in CASES if c['id'] == identifier)
        data = copy.deepcopy(case['data'])
        restore_bitumex_source_sections(data, (FIXTURES / (identifier + '.html')).read_text(), case['url'])
        text = json.dumps(data['product'][field], ensure_ascii=False)
        assert all(fact in text for fact in facts)


@pytest.mark.parametrize('identifier', ['73317d94f8942e53', '3f08dee6142a5cbe'])
def test_explicit_material_features_keep_pores_wetting_and_binding(identifier):
    case = next(c for c in CASES if c['id'] == identifier)
    data = copy.deepcopy(case['data'])
    restore_bitumex_source_sections(data, (FIXTURES / (identifier + '.html')).read_text(), case['url'])
    text = data['product']['advantages']
    assert 'проникать в поры' in text and 'смачивает поверхность' in text and 'связывает' in text


def test_visible_inline_powers_remain_powers():
    data, html, url = sample()
    html = html.replace('Характер набухания материала – равновесный.',
                        'Сопротивление 10<sup>17</sup> Ом. Плотность 1 г/см<sup>3</sup>.')
    restore_bitumex_source_sections(data, html, url)
    text = data['product']['specifications']['Состав и свойства материала']
    assert '10^{17}' in text and 'см^3' in text
    assert '10 17' not in text
