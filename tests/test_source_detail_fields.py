from copy import deepcopy
from pathlib import Path
import json
import pytest
W=Path(__file__).parent/'fixtures/audit_source_detail_fields'
from source_detail_fields import restore_source_detail_fields
from main import count_numeric_characteristics
C=json.loads((W/'cases10.json').read_text())

@pytest.mark.parametrize('c',C,ids=lambda c:c['id'])
def test_seven_real_sources_and_idempotence(c):
 a=deepcopy(c['ai']);before=deepcopy(a);h=(W/(c['id']+'.html')).read_text()
 changed=restore_source_detail_fields(a,h,c['url'])
 assert changed==(c['index']!=33)
 assert a['product']['description']==before['product']['description']
 assert a['product'].get('additional_info')==before['product'].get('additional_info')
 assert count_numeric_characteristics(a['product'])==count_numeric_characteristics(before['product'])
 if c['index'] in [35,36]:assert 'внутренней и наружной частям' in a['product']['specifications']['Конструкция']
 if c['index'] in [37,38]:assert 'наружной части и направленные внутрь' in a['product']['specifications']['Конструкция']
 if c['index']==40:assert '700 °C' in a['product']['exploitation']
 if c['index']==58:assert 'сыпучих, вязких, влажных, липких и волокнистых' in a['product']['applications'] and '45 градусов' in a['product']['applications']
 if c['index']==28:assert 'индивидуальным заказам' in a['product']['complectation'] and 'эскизов' in a['product']['complectation']
 if c['index']==65:assert 'Материал производится под заказ.' in a['product']['complectation']
 if c['index']==56:assert '75-80 %' in a['product']['exploitation'] and 'после специальной подготовки' in a['product']['exploitation']
 once=deepcopy(a);restore_source_detail_fields(a,h,c['url']);assert a==once


def sample(index=35):
 c=next(c for c in C if c['index']==index)
 return deepcopy(c['ai']),(W/(c['id']+'.html')).read_text(),c['url']


def test_unrelated_host_noop():
 a,h,u=sample();b=deepcopy(a);assert not restore_source_detail_fields(a,h,u.replace('xotpipe.ru','other.example'));assert a==b

@pytest.mark.parametrize('index',[35,56])
def test_negative_model_never_overridden(index):
 _,h,u=sample(index);a={'error':'Trash_418#'};assert not restore_source_detail_fields(a,h,u);assert a=={'error':'Trash_418#'}


def test_source_must_have_current_own_container():
 a,_,u=sample();b=deepcopy(a);assert not restore_source_detail_fields(a,'<p>Кольцо имеет лапки, направленные наружу.</p>',u);assert a==b


def test_hidden_own_data_not_accepted():
 a,h,u=sample();b=deepcopy(a);h='<div hidden>'+h+'</div>'
 assert not restore_source_detail_fields(a,h,u);assert a==b


def test_hidden_ecos_heading_not_accepted():
 a,h,u=sample(56);b=deepcopy(a);h=h.replace('class="contentblock__title"','hidden class="contentblock__title"')
 assert not restore_source_detail_fields(a,h,u);assert a==b


def test_related_product_ambiguous_container_not_accepted():
 a,h,u=sample();b=deepcopy(a);assert not restore_source_detail_fields(a,h+h,u);assert a==b


def test_source_value_signs_and_decimals_are_not_hardcoded():
 a,h,u=sample(40);h=h.replace('700 °C','-9,5 °C')
 restore_source_detail_fields(a,h,u);assert '-9,5 °C' in a['product']['exploitation'];assert '700' not in a['product']['exploitation']


def test_no_full_description_or_additional_dump():
 a,h,u=sample();a['product']['additional_info']={'text':'DO_NOT_COPY_ADDITIONAL'}
 h=h.replace('Кольцо имеет лапки,','UNRELATED_SOURCE_SENTENCE. Кольцо имеет лапки,')
 restore_source_detail_fields(a,h,u);value=a['product']['specifications']['Конструкция']
 assert 'UNRELATED_SOURCE_SENTENCE' not in value and 'DO_NOT_COPY_ADDITIONAL' not in value


@pytest.mark.parametrize('index',[28,65])
def test_hidden_inner_source_statement_not_copied(index):
 a,h,u=sample(index);b=deepcopy(a)
 phrase='Изготовление ведется' if index==28 else 'Материал производится под заказ.'
 h=h.replace(phrase,'<span hidden>'+phrase+'</span>')
 assert not restore_source_detail_fields(a,h,u);assert a==b
