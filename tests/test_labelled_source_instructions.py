import copy
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from labelled_source_instructions import restore_labelled_instructions

FIXTURES = Path(__file__).parent / 'fixtures' / 'stpaint_instructions'
URL = 'https://www.st-paint.ru/pu-1357'
HTML = (FIXTURES / 'pu1357.html').read_text()
DATA = json.loads((FIXTURES / 'pu1357_model.json').read_text())


def test_actual_both_application_assertions_curing_and_potlife():
    data = copy.deepcopy(DATA)
    before = copy.deepcopy(data)
    restore_labelled_instructions(data, HTML, URL)
    p = data['product']
    assert 'По разделу «Нанесение эмали»' in p['instructions_manuals']
    assert 'при температуре не ниже –1°С' in p['instructions_manuals']
    assert 'По разделу «Нанесение краски»: Наносят грунт-эмаль двумя слоями при температуре не ниже 0°С.' in p['instructions_manuals']
    assert 'Минимальная температура для отверждения: 0°C.' in p['instructions_manuals']
    assert 'температура для отверждения' not in p['exploitation']
    assert '12 лет' in p['exploitation'] and '5 лет' in p['exploitation']
    assert p['storage'] == ''
    assert p['specifications']['Время жизни смеси в таре'].startswith('Не менее 8 часов')
    assert 'при +20°С не менее 8 часов' in p['specifications']['Время жизни смеси в таре']
    assert len(p['specifications']) == len(before['product']['specifications'])
    for key in before['product']:
        if key not in ('instructions_manuals', 'exploitation', 'storage', 'specifications'):
            assert p[key] == before['product'][key]
    once = copy.deepcopy(data)
    restore_labelled_instructions(data, HTML, URL)
    assert data == once


@pytest.mark.parametrize('url', ['https://example.org/pu-1357', 'https://st-paint.ru.evil.test/product'])
def test_other_domain_never_applies(url):
    data = copy.deepcopy(DATA)
    restore_labelled_instructions(data, HTML, url)
    assert data == DATA


def test_wrong_page_class_or_heading_or_hidden_control_does_not_apply():
    for html in [HTML.replace('ulockd-service-details', 'other-page'),
                 HTML.replace('id="description-tab"', 'style="visibility:hidden" id="description-tab"')]:
        data = copy.deepcopy(DATA)
        restore_labelled_instructions(data, html, URL)
        assert data == DATA
    soup = BeautifulSoup(HTML, 'lxml')
    for h in soup.select('#description h2, #description h3, #description h4'):
        h.string = 'Случайные сведения'
    data = copy.deepcopy(DATA)
    restore_labelled_instructions(data, str(soup), URL)
    assert data == DATA


def test_storage_only_exact_source_fact_removed_other_information_retained():
    data = copy.deepcopy(DATA)
    data['product']['storage'] += '\n- состав остается пригодным к использованию в течение 8 часов при температуре +30 C с момента приготовления\n- хранить в сухом месте'
    restore_labelled_instructions(data, HTML, URL)
    assert '+30 C' in data['product']['storage']
    assert 'хранить в сухом месте' in data['product']['storage']
    assert '+20 C' not in data['product']['storage']


def test_rejected_answer_never_promoted():
    data = {'error': 'Trash_418#'}
    restore_labelled_instructions(data, HTML, URL)
    assert data == {'error': 'Trash_418#'}


def test_values_come_from_source_not_product_url_or_constants():
    soup = BeautifulSoup(HTML, 'lxml')
    for node in soup.find_all(string=True):
        node.replace_with(str(node).replace('–1°С', '–7°С').replace('0°С.', '+2°С.').replace('8 часов', '9 часов').replace('+20°С', '+23°С'))
    html = str(soup)
    data = {'product': {'instructions_manuals': '', 'storage': '', 'exploitation': '', 'specifications': {}}}
    restore_labelled_instructions(data, html, URL.rsplit('/', 1)[0] + '/different-product')
    p = data['product']
    assert '–7°С' in p['instructions_manuals'] and '+2°С' in p['instructions_manuals']
    assert '9 часов' in str(p['specifications']) and '+23°С' in str(p['specifications'])
