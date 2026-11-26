from pathlib import Path
from copy import deepcopy
import json
import pytest
W=Path(__file__).parent/'fixtures/audit_labelled_sections'
from labelled_product_sections import restore_labelled_product_sections
C=json.loads((W/'cases8.json').read_text())

@pytest.mark.parametrize('c',C,ids=lambda c:c['id'])
def test_real_ecos_sections_preserve_description_and_models(c):
 a=deepcopy(c['ai']);before=deepcopy(a);html=(W/(c['id']+'.html')).read_text()
 changed=restore_labelled_product_sections(a,html,c['url'])
 assert changed==(c['id']!='9f5fb2e9de174d9e')
 assert a['product']['description']==before['product']['description']
 assert {k:v for k,v in a['product']['specifications'].items() if k!='Конструкция'}==before['product']['specifications']
 once=deepcopy(a);restore_labelled_product_sections(a,html,c['url']);assert a==once


def sample():
 c=C[0];return deepcopy(c['ai']),(W/(c['id']+'.html')).read_text(),c['url']


def test_other_host_noop():
 a,h,u=sample();b=deepcopy(a);assert not restore_labelled_product_sections(a,h,u.replace('ecosgroup.com','other.example'));assert a==b


def test_error_not_overridden():
 _,h,u=sample();a={'error':'Trash_418#'};assert not restore_labelled_product_sections(a,h,u);assert a=={'error':'Trash_418#'}


def test_unlabelled_paragraph_not_copied():
 a,h,u=sample();h=h.replace('</main>','<p>UNLABELLED_UNIQUE_SENTINEL</p></main>')
 restore_labelled_product_sections(a,h,u);assert 'UNLABELLED_UNIQUE_SENTINEL' not in json.dumps(a)


def test_hidden_section_not_copied():
 a,h,u=sample();h=h.replace('class="textproductstype__title"','hidden class="textproductstype__title"');before=deepcopy(a)
 assert not restore_labelled_product_sections(a,h,u);assert a==before


def test_labels_outside_own_main_not_copied():
 a,_,u=sample();before=deepcopy(a);h='<h2 class="textproductstype__title">Принцип работы</h2><div class="textproductstype__descr"><p>OTHER_PRODUCT</p></div>'
 assert not restore_labelled_product_sections(a,h,u);assert a==before


def test_ss_brushes_and_isr_fat_collection_preserved():
 for id,words in [('5dab248e634f55d4',['перфорированного полотна','специальными щётками']),('b1e117a7226420e2',['жиры и пену','приёмный лоток'])]:
  c=next(c for c in C if c['id']==id);a=deepcopy(c['ai']);restore_labelled_product_sections(a,(W/(id+'.html')).read_text(),c['url'])
  assert all(v in a['product']['specifications']['Конструкция'] for v in words)


def test_centrifuge_keeps_angle_qualification():
 c=next(c for c in C if c['id']=='6bb1e6a8e6ec76d3');a=deepcopy(c['ai'])
 restore_labelled_product_sections(a,(W/(c['id']+'.html')).read_text(),c['url'])
 value=a['product']['specifications']['Конструкция']
 assert '20°' in value and 'когда не требуется максимальная сухость осадка' in value and 'без потери производительности' in value


@pytest.mark.parametrize('markup',['<span hidden>HIDDEN_CONSTRUCTION_999</span>', '<span style="display:none">HIDDEN_CONSTRUCTION_999</span>', '<template><p>HIDDEN_CONSTRUCTION_999</p></template>'])
def test_nested_invisible_construction_not_copied(markup):
 a,h,u=sample()
 h=h.replace('Илоскреб HEPRI',markup+'Илоскреб HEPRI',1)
 restore_labelled_product_sections(a,h,u)
 assert 'HIDDEN_CONSTRUCTION_999' not in a['product']['specifications']['Конструкция']


def test_hidden_construction_paragraph_not_copied():
 a,h,u=sample()
 from bs4 import BeautifulSoup
 s=BeautifulSoup(h,'html.parser');p=s.new_tag('p',attrs={'hidden':''});p.string='HIDDEN_PARAGRAPH_999'
 s.select_one('.textproductstype__descr').append(p)
 restore_labelled_product_sections(a,str(s),u)
 assert 'HIDDEN_PARAGRAPH_999' not in a['product']['specifications']['Конструкция']


def test_hidden_label_does_not_authorize_ecos_section():
 a,h,u=sample();before=deepcopy(a)
 h=h.replace('Принцип работы','<span hidden>Принцип работы</span>')
 assert not restore_labelled_product_sections(a,h,u)
 assert a==before
