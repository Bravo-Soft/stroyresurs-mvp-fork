import copy,json,re
from pathlib import Path
from bs4 import BeautifulSoup
import pytest
from visible_product_sections import restore_visible_product_sections

FIXTURES=Path(__file__).parent/'fixtures/product_sections'
ROWS=json.loads((FIXTURES/'cases.json').read_text())
FIELDS=['specifications','description','applications','instructions_manuals','exploitation','storage','advantages','complectation','security_measures','compatibility']
def norm(x):return re.sub(r'\s+',' ',str(x).casefold().replace('ё','е')).strip()

def load(row):
    data=json.loads((FIXTURES/(row['id']+'.json')).read_text())
    return copy.deepcopy(data),(FIXTURES/(row['id']+'.html')).read_text()


@pytest.mark.parametrize('row',ROWS,ids=lambda r:str(r['index']))
def test_actual_visible_audit_fact_is_retained_without_adding_gate_specs(row):
    data,html=load(row);before=copy.deepcopy(data)
    restore_visible_product_sections(data,html,row['source_url'])
    text=' '.join(str(data['product'].get(f,''))for f in FIELDS)
    assert norm(row['fact']).rstrip('.')in norm(text),row['fact']
    assert data['product'].get('description')==before['product'].get('description')
    for key,value in before['product'].get('specifications',{}).items():
        if key!='Конструкция':assert data['product']['specifications'][key]==value
    from main import count_numeric_characteristics
    assert count_numeric_characteristics(data['product'])==count_numeric_characteristics(before['product'])
    assert data['product'].get('additional_info')==before['product'].get('additional_info')
    once=copy.deepcopy(data);restore_visible_product_sections(data,html,row['source_url']);assert data==once


@pytest.mark.parametrize('url',['https://example.org/catalog/product','https://bkmz.ru/contacts/'])
def test_unrelated_domain_or_nonproduct_path_noop(url):
    data,html=load(ROWS[0]);before=copy.deepcopy(data)
    restore_visible_product_sections(data,html,url);assert data==before


def test_inactive_or_hidden_control_does_not_authorize_description():
    for attr in ['hidden','style']:
        data,html=load(ROWS[0]);before=copy.deepcopy(data);soup=BeautifulSoup(html,'lxml');control=soup.select_one('.tabs_caption li.active')
        if attr=='hidden':control['hidden']=''
        else:control['style']='visibility:hidden'
        restore_visible_product_sections(data,str(soup),ROWS[0]['source_url']);assert data==before
    data,html=load(ROWS[0]);before=copy.deepcopy(data);html=html.replace('tabs_good','unrelated-tabs')
    restore_visible_product_sections(data,html,ROWS[0]['source_url']);assert data==before


def test_hidden_nested_paragraph_and_diagnostic_dump_never_copied():
    data,html=load(ROWS[0]);soup=BeautifulSoup(html,'lxml');node=soup.new_tag('p',style='display:none');node.string='Не выводить999999';soup.select_one('.tabs_content.active .text').append(node)
    data['product']['additional_info']={'text':'Не выводить888888'}
    restore_visible_product_sections(data,str(soup),ROWS[0]['source_url'])
    text=' '.join(str(data['product'].get(f,''))for f in FIELDS)
    assert '999999'not in text and'888888'not in text


def test_valve_installation_quantity_is_instruction_not_technical_spec():
    row=ROWS[-1];data,html=load(row);restore_visible_product_sections(data,html,row['source_url'])
    assert norm(row['fact'])in norm(data['product']['instructions_manuals'])


def test_rejected_model_result_is_not_promoted():
    _,html=load(ROWS[0]);data={'error':'Trash_418#'}
    restore_visible_product_sections(data,html,ROWS[0]['source_url']);assert data=={'error':'Trash_418#'}


@pytest.mark.parametrize('tag', ['template', 'script', 'noscript', 'span'])
def test_nested_invisible_text_inside_visible_paragraph_is_not_copied(tag):
    data,html=load(ROWS[0]);soup=BeautifulSoup(html,'lxml');node=soup.new_tag(tag);node.string='Невидимая конструкция777777'
    if tag=='span':node['style']='visibility:hidden'
    soup.select_one('.tabs_content.active .text p').append(node)
    restore_visible_product_sections(data,str(soup),ROWS[0]['source_url'])
    assert '777777'not in ' '.join(str(data['product'].get(f,''))for f in FIELDS)


@pytest.mark.parametrize('wrapper_type', ['template','script','noscript','body-hidden','body-display','html-visibility'])
def test_entire_product_in_invisible_wrapper_is_not_a_source(wrapper_type):
    data,html=load(ROWS[0]);before=copy.deepcopy(data);soup=BeautifulSoup(html,'lxml')
    if wrapper_type in ['template','script','noscript']:
        wrapper=soup.new_tag(wrapper_type);soup.select_one('div.main').wrap(wrapper)
    elif wrapper_type=='body-hidden':soup.body['hidden']=''
    elif wrapper_type=='body-display':soup.body['style']='display:none'
    else:soup.html['style']='visibility:hidden'
    restore_visible_product_sections(data,str(soup),ROWS[0]['source_url']);assert data==before
