from copy import deepcopy
import json
from pathlib import Path
import pytest

HERE=Path(__file__).parent/'fixtures/audit_quality_source_fields'
from quality_source_fields import restore_quality_source_fields
from main import count_numeric_characteristics
CASES=json.loads((HERE/'cases37.json').read_text())

@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_all_37_real_ui_sources(case):
    ai=deepcopy(case['ai']);before=deepcopy(ai);html=(HERE/(case['id']+'.html')).read_text()
    changed=restore_quality_source_fields(ai,html,case['url'])
    if case['is_assembly']:
        assert not changed and ai==before
        yield_value = next(v for k, v in ai['product']['specifications'].items() if k.startswith('Выход клея'))
        assert count_numeric_characteristics(ai['product']) == (4 if yield_value == '10-11 м' else 3)
    elif 'error' in before:
        assert not changed and ai==before
    elif changed:
        specs=ai['product']['specifications']
        assert isinstance(specs['Расход'],str)
        assert 'воды' not in specs['Расход']
        assert 'литр' in ai['product']['instructions_manuals']
        assert count_numeric_characteristics(ai['product']) == 2
        assert not any('стандартный рулон' in k.casefold() for k in specs)
    else:
        assert count_numeric_characteristics(ai['product'])<3
    once=deepcopy(ai);restore_quality_source_fields(ai,html,case['url']);assert ai==once


def wallpaper():
    c=next(c for c in CASES if c['id']=='19836c7e0bb30988')
    return deepcopy(c['ai']),(HERE/(c['id']+'.html')).read_text(),c['url']


def test_other_host_is_unchanged():
    ai,html,url=wallpaper();before=deepcopy(ai)
    assert not restore_quality_source_fields(ai,html,url.replace('quality-pro.ru','other.example'))
    assert ai==before


def test_additional_honest_properties_not_discarded():
    ai,html,url=wallpaper();ai['product']['specifications'].update({'Плотность':'1000 кг/м^3','Адгезия':'2 МПа'})
    assert restore_quality_source_fields(ai,html,url)
    assert count_numeric_characteristics(ai['product']) == 4


def test_independent_models_are_not_globally_collapsed():
    ai,html,url=wallpaper();table={'Модель А':{'Расход':'10'},'Модель Б':{'Расход':'20'},'Модель В':{'Расход':'30'}}
    ai['product']['specifications']['Другие исполнения']=deepcopy(table)
    restore_quality_source_fields(ai,html,url)
    assert ai['product']['specifications']['Другие исполнения']==table


def test_public_ui_required_not_a_hidden_template():
    ai,html,url=wallpaper();before=deepcopy(ai)
    assert not restore_quality_source_fields(ai,'<template>'+html+'</template>',url)
    assert ai==before


def test_unknown_markup_not_guessed():
    ai,_,url=wallpaper();before=deepcopy(ai)
    assert not restore_quality_source_fields(ai,'<p>расход клея зависит от их типа</p>',url)
    assert ai==before


def test_preserve_complete_hyphenated_context_and_both_roll_conditions():
    c=next(c for c in CASES if c['id']=='c9e8bd7da9fb5ff7');ai=deepcopy(c['ai'])
    restore_quality_source_fields(ai,(HERE/(c['id']+'.html')).read_text(),c['url'])
    value=ai['product']['specifications']['Расход']
    assert '3D-рисунком, грубоволокнистые, пенообои)' in value
    assert 'Стандартный рулон обоев: 10,05 × 0,53 м' in value
    assert 'Стандартный рулон флизелиновых обоев: 25,0 × 1,06 м' in value


def test_hidden_public_control_cannot_authorize_source_panel():
    ai,html,url=wallpaper();before=deepcopy(ai)
    html=html.replace('class="t-store__tabs__item-button', 'hidden class="t-store__tabs__item-button')
    assert not restore_quality_source_fields(ai,html,url)
    assert ai==before


@pytest.mark.parametrize('tag,attrs',[('span',{'hidden':''}),('span',{'style':'display:none'}),('template',{})])
def test_nested_invisible_quality_rows_not_copied(tag,attrs):
    from bs4 import BeautifulSoup
    a,h,u=wallpaper();s=BeautifulSoup(h,'html.parser')
    panel=next(p for p in s.select('.t-store__tabs__content') if 'расход клея зависит' in p.get_text().casefold())
    invisible=s.new_tag(tag,attrs=attrs)
    invisible.append(BeautifulSoup('<li>HIDDEN_LI_SENTINEL — 999 литров воды на упаковку, хватает на 999 рулонов</li>','html.parser').li)
    panel.append(invisible)
    restore_quality_source_fields(a,str(s),u)
    assert 'HIDDEN_LI_SENTINEL' not in json.dumps(a['product'],ensure_ascii=False)
    assert '999' not in a['product']['specifications']['Расход']


def test_nested_hidden_title_does_not_authorize_quality_panel():
    a,h,u=wallpaper();before=deepcopy(a)
    h=h.replace('>Область применения<','><span hidden>Область применения</span><')
    assert not restore_quality_source_fields(a,h,u)
    assert a==before


def test_published_collapsed_quality_panel_remains_available():
    a,h,u=wallpaper()
    h=h.replace('class="t-store__tabs__content ', 'hidden style="display:none" class="t-store__tabs__content ')
    assert restore_quality_source_fields(a,h,u)
    assert count_numeric_characteristics(a['product']) == 2
