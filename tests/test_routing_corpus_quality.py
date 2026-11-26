"""Корпус подтверждённых дублей + защита единиц, знаков и контекста."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from product_quality import normalize_product_fields, _fact_key

CORPUS=json.loads((Path(__file__).parent/'fixtures/audit_routing/corpus.json').read_text())
CHECKS=CORPUS['checks']
PRODUCTS=CORPUS['products']

@pytest.mark.parametrize('check',[c for c in CHECKS if c['kind']=='color_exact'],ids=lambda c:c['name'])
def test_corpus_13_flat_colors(check):
    p=deepcopy(PRODUCTS[check['source_url'].rstrip('/')]);colors=deepcopy(p['colors'])
    normalize_product_fields({'product':p})
    assert 'Цвет' not in p['specifications']
    assert p['colors']==colors

@pytest.mark.parametrize('check',[c for c in CHECKS if c['kind'].startswith('life_')],ids=lambda c:c['name']+c['spec_label'])
def test_corpus_24_lifetime_duplicates(check):
    p=deepcopy(PRODUCTS[check['source_url'].rstrip('/')])
    normalize_product_fields({'product':p})
    expected=_fact_key(check['spec_label']+': '+check['value'])
    facts=[_fact_key(fragment) for line in p['exploitation'].splitlines() for fragment in line.split(';')]
    assert facts.count(expected)==1
    assert check['spec_path'][-1] not in p['specifications']
    once=deepcopy(p);normalize_product_fields({'product':p});assert p==once

@pytest.mark.parametrize('check',[c for c in CHECKS if c['kind']=='storage_review'],ids=lambda c:c['name'])
def test_corpus_10_storage_conditions_preserved(check):
    p=deepcopy(PRODUCTS[check['source_url'].rstrip('/')])
    normalize_product_fields({'product':p})
    assert _fact_key(check['exploitation_line']) in {_fact_key(line) for line in p['storage'].splitlines()}
    assert check['exploitation_line'] not in p['exploitation'].splitlines()
    once=deepcopy(p);normalize_product_fields({'product':p});assert p==once


def test_different_values_guarantee_and_after_opening_remain_separate():
    p={'specifications':{'Срок службы, лет':'30','Гарантийный срок службы, лет':'5'},
       'exploitation':'Срок службы: 30 лет; Гарантийный срок службы: 5 лет',
       'storage':'После вскрытия использовать в течение 72 часов'}
    normalize_product_fields({'product':p})
    assert p['exploitation']=='Срок службы: 30 лет; Гарантийный срок службы: 5 лет'
    assert p['storage']=='После вскрытия использовать в течение 72 часов'


def test_nested_context_not_deduplicated_against_whole_product():
    p={'specifications':{'Двигатель':{'Срок службы, лет':'30'}},'exploitation':'Срок службы: 30 лет'}
    normalize_product_fields({'product':p})
    assert 'Двигатель: Срок службы, лет: 30' in p['exploitation']
    assert p['exploitation'].startswith('Срок службы: 30 лет')

@pytest.mark.parametrize('label,value', [('Срок службы при температуре +5 C','30 лет'),('Срок службы, лет','не менее 30'),('Срок службы','30 лет при +5 C')])
def test_qualifications_are_never_merged(label,value):
    p={'specifications':{label:value},'exploitation':'Срок службы: 30 лет'}
    normalize_product_fields({'product':p})
    assert f'{label}: {value}' in p['exploitation']


def test_numeric_sign_and_units_survive():
    assert _fact_key('Температура: +5 C')!=_fact_key('Температура: -5 C')
    assert _fact_key('Срок службы, лет: 30')==_fact_key('Срок службы: 30 лет')
    assert _fact_key('Срок службы, лет: 30')!=_fact_key('Срок службы: 30 месяцев')


def test_unknown_additional_advertising_and_mixed_storage_stay():
    p={'specifications':{'Гарантированная прочность':'50 МПа'},
       'additional_info':{'text':'Преимущества гарантии: лучшие условия\nРеклама срока службы: покупайте сейчас'},
       'exploitation':'Условия хранения и эксплуатации: от +5 до +30 C'}
    original=deepcopy(p)
    normalize_product_fields({'product':p})
    assert p['specifications']==original['specifications']
    assert p['additional_info']==original['additional_info']
    assert p['exploitation']==original['exploitation']


def test_invalid_destination_never_drops_fact():
    p={'specifications':{'Срок службы':'30 лет'},'exploitation':{'неожиданная':'схема'}}
    normalize_product_fields({'product':p})
    assert p['specifications']=={'Срок службы':'30 лет'}
