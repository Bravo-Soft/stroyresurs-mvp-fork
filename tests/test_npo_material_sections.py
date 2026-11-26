import copy,json,re
from pathlib import Path
from bs4 import BeautifulSoup
import pytest
from visible_product_sections import restore_visible_product_sections

FIXTURES=Path(__file__).parent/'fixtures/npo_material_sections'
ROWS=json.loads((FIXTURES/'cases.json').read_text())
FIELDS=['description','specifications','applications','instructions_manuals','exploitation','storage','complectation','advantages']
def norm(text):return ''.join(re.findall(r'\w+',str(text).casefold().replace('ё','е')))
def load(row):return json.loads((FIXTURES/(row['id']+'.json')).read_text()),(FIXTURES/(row['id']+'.html')).read_text()


@pytest.mark.parametrize('row',ROWS,ids=lambda r:str(r['index']))
def test_eight_actual_products_keep_source_manufacturing_relationships(row):
    data,html=load(row);before=copy.deepcopy(data)
    restore_visible_product_sections(data,html,row['source_url'])
    text=' '.join(str(data['product'].get(f,''))for f in FIELDS)
    if row['index']==3:
        assert 'порош' in text  # Already retained in source packing/form; no shipping dump.
    else:
        assert norm(row['fact'])in norm(text),row['fact']
    assert data['product']['description']==before['product']['description']
    assert data['product'].get('additional_info')==before['product'].get('additional_info')
    from main import count_numeric_characteristics
    assert count_numeric_characteristics(data['product'])==count_numeric_characteristics(before['product'])
    once=copy.deepcopy(data);restore_visible_product_sections(data,html,row['source_url']);assert data==once


@pytest.mark.parametrize('missing', ['class','widget-type'])
def test_own_widget_with_language_signal_is_required(missing):
    row=ROWS[0];data,html=load(row);soup=BeautifulSoup(html,'lxml')
    for node in soup.select('.elementor-widget-text-editor'):
        if missing=='class':node['class']=[c for c in node.get('class',[])if c!='elementor-widget-text-editor']
        else:node['data-widget_type']='unrelated.default'
    before=copy.deepcopy(data);restore_visible_product_sections(data,str(soup),row['source_url']);assert data==before


def test_unknown_paragraph_hidden_nested_text_and_model_table_do_not_leak():
    row=ROWS[0];data,html=load(row);soup=BeautifulSoup(html,'lxml');widget=soup.select_one('.elementor-widget-text-editor')
    p=soup.new_tag('p');p.string='Случайные сведения777001';widget.append(p)
    table=soup.new_tag('table');table.append(BeautifulSoup('<tr><td><p>Чужая модель состоит из материала777002</p></td></tr>','html.parser'));widget.append(table)
    span=soup.new_tag('span',style='display:none');span.string='Невидимая модель состоит из777003';widget.p.append(span)
    outside=soup.new_tag('p');outside.string='Изготавливается чужой текст777004';soup.select_one('.hentry').append(outside)
    restore_visible_product_sections(data,str(soup),row['source_url'])
    text=' '.join(str(data['product'].get(f,''))for f in FIELDS)
    assert all(value not in text for value in ['777001','777002','777003','777004'])


def test_mixed_catalog_and_unisolated_family_url_are_noop():
    row=ROWS[0];data,_=load(row);before=copy.deepcopy(data)
    html=(FIXTURES/'mixed_catalog.html').read_text();url=(FIXTURES/'mixed_catalog_url.txt').read_text()
    from source_product_tables import source_family_catalog_reason
    assert source_family_catalog_reason(html,url)
    restore_visible_product_sections(data,html,url);assert data==before
    data,html=load(row);before=copy.deepcopy(data)
    restore_visible_product_sections(data,html,row['source_url']+'#source-family=Unrelated');assert data==before


def test_foreign_domain_and_hidden_product_wrapper_are_noop():
    row=ROWS[0];data,html=load(row);before=copy.deepcopy(data)
    restore_visible_product_sections(data,html,'https://example.org/products/item');assert data==before
    soup=BeautifulSoup(html,'lxml');soup.body['style']='visibility:hidden'
    restore_visible_product_sections(data,str(soup),row['source_url']);assert data==before


@pytest.mark.parametrize('heading,field',[('Применение','applications'),('Технология изготовления','specifications')])
def test_explicit_named_section_routes_own_paragraph_and_stops_at_other_caption(heading,field):
    row=ROWS[0];data,html=load(row);soup=BeautifulSoup(html,'lxml');widget=soup.select_one('.elementor-widget-text-editor');widget.clear()
    h=soup.new_tag('h3');h.string=heading;widget.append(h)
    p=soup.new_tag('p');p.string='Подтверждённый текст раздела777101';widget.append(p)
    h=soup.new_tag('h4');h.string='Таблица чужих моделей';widget.append(h)
    p=soup.new_tag('p');p.string='Неподтверждённый текст777102';widget.append(p)
    restore_visible_product_sections(data,str(soup),row['source_url'])
    assert '777101'in str(data['product'][field])
    assert '777102'not in ' '.join(str(data['product'].get(f,''))for f in FIELDS)
