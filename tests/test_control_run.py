"""Контрольный прогон: штатное извлечение/карточки с явным тестовым транспортом."""
import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import zipfile
import pytest
from docx import Document
from site_profiles.control_run import execute,validate_manifest,LIMITS
from site_profiles.control_transport import NetworkPolicy,Unavailable,FreshTransport
from site_profiles.profile_queue import Queue,encode

HTML='<main><h1>Насос К1</h1><p>Масса 12 кг. Высота 20 мм. Мощность 3 кВт.</p></main>'
PRODUCT={'product':{'product_name':'Насос К1','specifications':{'Масса':'12 кг','Высота':'20 мм','Мощность':'3 кВт'}}}

class FakeTransport:
    def __init__(self,model,*,result=None):
        self.policy=NetworkPolicy({'factory.test','www.factory.test'},model['base_url'])
        self.result=copy.deepcopy(PRODUCT if result is None else result);self.fetched=[];self.requests=[]
        self.html=HTML;self.final=None;self.status=200;self.usage={'prompt_tokens':100,'completion_tokens':50}
    async def fetch(self,url,folder,page):
        self.fetched.append(url)
        return dict(html=self.html,final_url=self.final or url,method='test_transport',visibility='static_html_only')
    async def request(self,url,**kw):
        self.requests.append(kw['payload'])
        return self.status,{},json.dumps({'choices':[{'message':{'content':json.dumps(self.result,ensure_ascii=False)},'finish_reason':'stop'}],
                                        'usage':self.usage}).encode()

@pytest.fixture
def packet(tmp_path,monkeypatch):
    monkeypatch.setenv('CONTROL_MODEL_API_KEY','unit-secret-never-in-artifacts')
    profiles=tmp_path/'profiles';profiles.mkdir()
    (profiles/'factory.test.yaml').write_text('domain: factory.test\nextract:\n  markdown:\n    product_container: main\n')
    model=dict(base_url='http://127.0.0.1:4000/v1',name='gemma4-31b-nvfp4',key_env='CONTROL_MODEL_API_KEY')
    data=dict(version=1,company={'name':'Завод','url':'https://factory.test/'},model=model,
              pages=[dict(url='https://factory.test/p/'+s,split=s,reference={'selectors':['main'],'origin':'Ручной контроль'}) for s in ('control','holdout')])
    path=tmp_path/'manifest.json';path.write_text(encode(data))
    return path,profiles,data

def run(packet,tmp_path,transport=None,**kw):
    path,profiles,data=packet;path.write_text(encode(data))
    transport=transport or FakeTransport(data['model'])
    result=asyncio.run(execute(path,profiles,tmp_path/'output',transport=transport,require_isolation=False,**kw))
    return result,transport,json.loads((tmp_path/'output/metrics.json').read_text())

def test_full_chain_docx_source_binding_and_archive(packet,tmp_path):
    result,transport,metrics=run(packet,tmp_path)
    assert result['status']=='uncertain' and result['mode']=='injected_test_not_live'
    assert len(result['cards'])==2 and metrics['card_spec_counts']==[3,3] and metrics['median_specs_per_card']==3
    assert metrics['card_spec_counts_complete'] and metrics['model_usage']['calls']==2
    assert len({c['card_id'] for c in result['cards']})==2
    for card in result['cards']:
        doc=Document(tmp_path/'output'/card['path'])
        text=' '.join(p.text for p in doc.paragraphs)+' '.join(c.text for t in doc.tables for row in t.rows for c in row.cells)
        assert 'Насос К1' in text and '12 кг' in text and card['url'] in text
    with zipfile.ZipFile(tmp_path/'output/evidence.zip') as archive:
        assert '0000/request_0.json' in archive.namelist() and '0000/model_input.txt' in archive.namelist()
        assert not any(b'unit-secret' in archive.read(n) for n in archive.namelist())
    assert result['code_sha256'] and result['prompt_system_sha256']

@pytest.mark.parametrize('result,expected',[
    ({'error':'Trash_418#'},'rejected'),
    ({'product':{'product_name':'Насос','specifications':{'Масса':'12 кг'}}},'rejected'),
    ({'product':{'product_name':'Насос','specifications':{}}},'rejected')])
def test_rejections_keep_evidence_no_fake_counts(packet,tmp_path,result,expected):
    report,transport,m=run(packet,tmp_path,FakeTransport(packet[2]['model'],result=result))
    assert all(p['state']==expected for p in report['pages'])
    assert m['card_spec_counts']==[] and m['median_specs_per_card'] is None
    assert report['status']=='uncertain' and 'zero_cards_requires_review' in report['uncertainties']

@pytest.mark.parametrize('expected,result',[('product',{'error':'Trash_418#'}),('not_product',PRODUCT)])
def test_known_expected_outcome_is_fail(packet,tmp_path,expected,result):
    for p in packet[2]['pages']:p['expected']=expected
    report,_,_=run(packet,tmp_path,FakeTransport(packet[2]['model'],result=result))
    assert report['status']=='fail' and not report['cards']

def test_alias_dedup_before_model(packet,tmp_path):
    t=FakeTransport(packet[2]['model']);t.final='https://factory.test/shared'
    report,t,m=run(packet,tmp_path,t)
    assert len(t.requests)==1 and len(report['cards'])==1
    assert report['pages'][1]['state']=='duplicate'

def test_model_error_no_retry(packet,tmp_path):
    t=FakeTransport(packet[2]['model']);t.status=503
    report,t,m=run(packet,tmp_path,t)
    assert report['status']=='uncertain' and len(t.requests)==2  # По одной попытке на страницу.
    assert not report['cards'] and m['model_usage']['usage_estimated']

def test_call_budget(packet,tmp_path):
    packet[2]['limits']={'max_calls':1}
    report,t,m=run(packet,tmp_path)
    assert len(t.requests)==1 and m['model_usage']['calls']==1
    assert report['pages'][1]['state']=='uncertain'

def test_token_budget_preflight_no_model(packet,tmp_path):
    packet[2]['limits']={'max_tokens':10}
    report,t,m=run(packet,tmp_path)
    assert not t.requests and not report['cards']

def test_missing_key_no_fetch(packet,tmp_path,monkeypatch):
    monkeypatch.delenv('CONTROL_MODEL_API_KEY')
    report,t,m=run(packet,tmp_path)
    assert not t.fetched and all(p['state']=='not_run' for p in report['pages'])
    assert report['status']=='uncertain'

def test_readonly_required_without_mount(packet,tmp_path):
    path,profiles,data=packet;t=FakeTransport(data['model'])
    report=asyncio.run(execute(path,profiles,tmp_path/'output',transport=t))
    assert not t.fetched and any('read-only' in g for g in report['uncertainties'])

def test_card_failure_not_counted(packet,tmp_path,monkeypatch):
    async def fail(*args,**kw):return ''
    monkeypatch.setattr('docx_generator.DOCXGenerator.generate_product_card',fail)
    report,t,m=run(packet,tmp_path)
    assert report['status']=='fail' and m['card_spec_counts']==[]
    assert m['product_cards']==0

def test_profile_exclusion_before_fetch(packet,tmp_path):
    (packet[1]/'factory.test.yaml').write_text('domain: factory.test\ncrawl:\n  exclude_paths: ["/p/"]\n')
    report,t,m=run(packet,tmp_path)
    assert not t.fetched and report['status']=='fail'

def test_profile_removal_is_not_reference(packet,tmp_path):
    (packet[1]/'factory.test.yaml').write_text('domain: factory.test\nextract:\n  markdown:\n    product_container: main\n    remove_selectors: [p]\n')
    report,t,m=run(packet,tmp_path)
    assert not t.requests and report['status']=='fail'

@pytest.mark.parametrize('url',['http://factory.test:4242/api/companies/add','file:///tmp/a','https://outside.test/x','https://factory.test/a.pdf','https://u:p@factory.test/'])
def test_site_perimeter(url):
    with pytest.raises(ValueError):NetworkPolicy({'factory.test'},'http://127.0.0.1:4000/v1').check_url(url)

@pytest.mark.parametrize('base',['http://127.0.0.1:4242/api','http://host:9092','ftp://host'])
def test_model_cannot_be_graph(base):
    policy=NetworkPolicy({'factory.test'},base)
    with pytest.raises(ValueError):policy.check_url(policy.model_url,True)

def test_private_site_dns_denied(monkeypatch):
    async def check():
        async def lookup(*args,**kwargs):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',lookup)
        policy=NetworkPolicy({'factory.test'},'http://127.0.0.1:4000/v1')
        with pytest.raises(ValueError):await policy.resolve('https://factory.test/')
    asyncio.run(check())

def test_redirect_perimeter_checked_before_next_request(tmp_path):
    p=NetworkPolicy({'factory.test'},'http://127.0.0.1:4000/v1');t=FreshTransport(p,LIMITS);calls=[]
    async def request(url,**kw):calls.append(url);return 302,{'Location':'http://127.0.0.1:4242/api/companies/add'},b''
    t.request=request
    with pytest.raises(ValueError):asyncio.run(t.fetch('https://factory.test/',tmp_path,{}))
    assert len(calls)==1

def test_source_windows_captured_after_actual_truncation(packet,tmp_path):
    t=FakeTransport(packet[2]['model']);t.html='<main><p>'+('текст '*18000)+'</p><p>Масса 12 кг. Высота 20 мм. Мощность 3 кВт.</p></main>'
    report,t,m=run(packet,tmp_path,t)
    assert all(row['state']=='failed' and row['reason']=='model_input_loss' for row in report['pages'])
    assert len((tmp_path/'output/0000/model_input.txt').read_text())==100000
    assert not report['cards']

def test_queue_control_slots_and_snapshot(packet,tmp_path):
    manifest,profiles,data=packet
    job=tmp_path/'job.json';job.write_text(encode(dict(version=1,url=data['company']['url'],profiles=str(profiles),stages=[dict(kind='control',manifest=str(manifest))])))
    q=Queue.create(tmp_path/'queue')
    with pytest.raises(ValueError,match='model_slots'):q.enqueue(job)
    q.close()
    q=Queue.create(tmp_path/'queue2',dict(model_slots=1))
    jid=q.enqueue(job);a=q.claim();assert a['job']==jid
    with q.dispatcher_lock():q.recover()
    assert q.status()['jobs'][0]['status']=='review' and q.claim() is None
    assert 'automatic_model_retry' in (q.root/a['folder']/'output'/next((q.root/a['folder']/'output').iterdir()).name).read_text()
    q.close()

def test_sibling_headings_and_approved_lambda_preserved():
    from site_profiles.markdown_validation import check_markdown
    html='<main><h1>Изделие</h1><h2>Описание</h2><p>λ10: 0,038 Вт/(м*К)</p><h2>Применение</h2><p>Кровля</p></main>'
    md='# Изделие\n## **Описание**\nлямбда10: 0,038 Вт/(м*К)\n## **Применение**\nКровля'
    assert check_markdown(html,md,source_selectors=['main'])['status']=='pass'
    assert check_markdown(html,md.replace('## **Применение**','Применение'),source_selectors=['main'])['status']=='fail'

def test_same_contact_url_different_roles_are_both_processed(packet,tmp_path):
    packet[2]['pages']=[dict(url='https://factory.test/contacts',role=role,split='control',reference={'selectors':['main'],'origin':'Контактная страница'}) for role in ('company','distributor')]
    t=FakeTransport(packet[2]['model'],result={'suppliers':[{'company_name':'Дилер','address':'Москва'}]})
    report,t,m=run(packet,tmp_path,t)
    assert [p['state'] for p in report['pages']]==['data_extracted','data_extracted']
    assert len(t.requests)==2 and m['card_spec_counts']==[]


def test_holdout_same_url_not_independent(packet):
    packet[2]['pages'][1]['url']=packet[2]['pages'][0]['url']
    packet[2]['pages'][1]['role']='company'
    with pytest.raises(ValueError,match='holdout'):validate_manifest(packet[2])


def test_missing_usage_is_explicit_estimate(packet,tmp_path):
    t=FakeTransport(packet[2]['model']);t.usage={}
    report,t,m=run(packet,tmp_path,t)
    assert m['model_usage']['usage_estimated'] and 'model_usage_estimated' in report['uncertainties']


def test_wrong_structure_sample_remains_open(packet,tmp_path):
    structure=tmp_path/'structure.json';structure.write_text(encode({'urls':[{'key':'x','url':'https://factory.test/missing'}], 'sample':['x'],'coverage':{'gaps':['page_limit']}}))
    packet[2]['structure_report']=str(structure)
    report,_,_=run(packet,tmp_path)
    assert 'discovery_sample_missing' in report['uncertainties'] and 'discovery:page_limit' in report['uncertainties']


def test_resource_requests_not_subject_to_page_classifier(tmp_path):
    policy=NetworkPolicy({'factory.test'},'http://127.0.0.1:4000/v1')
    policy.check_url('https://factory.test/assets/app.js')
    transport=FreshTransport(policy,LIMITS)
    def deny(url):raise ValueError('Страница исключена')
    transport.page_check=deny
    with pytest.raises(ValueError,match='исключена'):asyncio.run(transport.request('https://factory.test/private'))


def test_partial_control_receipt_no_paid_retry_and_tamper_detection(packet,tmp_path):
    path,profiles,data=packet;job=tmp_path/'job.json'
    job.write_text(encode({'version':1,'url':data['company']['url'],'profiles':str(profiles),'stages':[{'kind':'control','manifest':str(path)}]}))
    q=Queue.create(tmp_path/'queue',dict(model_slots=1))
    q.enqueue(job);a=q.claim();folder=q.root/a['folder'];(folder/'output').mkdir(parents=True)
    (folder/'output/model_request.json').write_text('{"model":"already_sent"}')
    with q.dispatcher_lock():q.recover()
    assert q.status()['jobs'][0]['status']=='review' and len(q.status()['attempts'])==1
    assert q.receipt(a)['status']=='uncertain'
    receipt=q.receipt(a);(folder/receipt['report']).write_text('искажение')
    with pytest.raises(ValueError):q.receipt(a)
    q.close()


def test_numeric_gate_unchanged_from_prior_source():
    # Независимое сравнение всех контрольных примеров с сохранённой старой реализацией.
    import ast,re
    from product_preparation import count_numeric_characteristics
    from typing import Any,Dict
    path=Path(__file__).resolve().parents[2]/'before/main.py'
    if not path.exists():pytest.skip('Парный снимок хранится только в рабочем пакете задачи')
    tree=ast.parse(path.read_text());nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_count_value_numeric','count_numeric_characteristics')]
    namespace={'re':re,'Any':Any,'Dict':Dict};exec(compile(ast.Module(body=nodes,type_ignores=[]),'old_gate','exec'),namespace)
    cases=[PRODUCT['product'],{'specifications':{'ГОСТ':'123','Упаковка':'12 шт','Размер':'12х34х56 мм','Мощность':'1,2 кВт'}},
           {'specifications':{'A':{'Масса':'12 кг','Толщина':['1','2']},'B':{'Высота':'10 мм'}},'weight':'3 кг'},
           {'specifications':{},'dimensions':'4x5x6 мм'},{}]
    for value in cases:assert count_numeric_characteristics(value)==namespace['count_numeric_characteristics'](value)
