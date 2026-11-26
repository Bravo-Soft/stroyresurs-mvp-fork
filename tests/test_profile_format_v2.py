"""Контракт формата v2: загрузка, выбор шаблона, содержимое и совместимость."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from bs4 import BeautifulSoup

import text_extractor as te
from site_profiles import ProfileResolver, profile_from_dict
from site_profiles.format_v2 import compose, load_yaml, merge, select_page, ProfileRuleError
from site_profiles.profile_metrics import RunMetricsCollector
from site_profiles.census_collector import CensusCollector

URL='https://test.example/catalog/item'
BASE={'domain':'test.example','schema_version':2,'profile_version':1}
RULE={'name':'product','priority':10,'match':{'page_types':['product'],'path_regex':'^/catalog/','selector':'.card'},
      'extract':{'markdown':{'product_container':'.card','content_blocks':[{'name':'specs','selector':'.specs','required':True}], 'remove_selectors':['.noise']}}}
HTML='<article>Чужой товар</article><div class="card"><h1>Насос</h1><div class="noise">МУСОР</div></div><section class="specs">Подача 12 м3/ч <b>Напор 25 м</b></section>'


@pytest.fixture(autouse=True)
def clear_sink():
    te.set_census_sink(None)
    yield
    te.set_census_sink(None)


def profile(data):
    p,e=profile_from_dict(data)
    assert not e,e
    return p


def setup(monkeypatch,data):
    p=profile(data);monkeypatch.setattr(te,'_resolve_profile',lambda url:p)
    return p


def test_runtime_rule_multiple_blocks_and_metrics(monkeypatch):
    p=setup(monkeypatch,dict(BASE,extract={'page_rules':[RULE]}))
    metrics=RunMetricsCollector('test.example');te.set_census_sink(metrics.sink)
    out=te.html_to_markdown(HTML,URL)
    assert 'Насос' in out and '12 м^3/ч' in out and '25 м' in out
    assert 'МУСОР' not in out and 'Чужой товар' not in out
    assert metrics.to_dict()['profile_rules'][0]['selected_rule']=='product'
    assert p.extract.markdown.product_container is None


@pytest.mark.parametrize('page_type',['company','distributor'])
def test_role_specific_rule(monkeypatch,page_type):
    rule=copy.deepcopy(RULE);rule['match']={'page_types':[page_type]}
    rule['extract']['markdown']={page_type+'_container':'.specs'}
    setup(monkeypatch,dict(BASE,extract={'page_rules':[rule]}))
    out=te.html_to_markdown(HTML,URL,page_type)
    assert '25 м' in out and 'Насос' not in out


def test_no_rule_keeps_site_settings(monkeypatch):
    setup(monkeypatch,dict(BASE,extract={'markdown':{'product_container':'.specs'},'page_rules':[RULE]}))
    out=te.html_to_markdown(HTML,'https://test.example/other')
    assert '25 м' in out and 'Насос' not in out


@pytest.mark.parametrize('mutation',[lambda s:s.replace('class="specs"','class="absent"'),
                                    lambda s:s.replace('class="specs"','class="specs" hidden'),
                                    lambda s:s.replace('class="specs"','class="specs" style="display:none"')])
def test_missing_or_hidden_required_block_fails(monkeypatch,mutation):
    setup(monkeypatch,dict(BASE,extract={'page_rules':[RULE]}))
    with pytest.raises(ProfileRuleError,match='обязательный'):te.html_to_markdown(mutation(HTML),URL)


def test_removal_of_required_block_is_detected(monkeypatch):
    rule=copy.deepcopy(RULE);rule['extract']['markdown']['remove_selectors']=['.specs']
    setup(monkeypatch,dict(BASE,extract={'page_rules':[rule]}))
    with pytest.raises(ProfileRuleError):te.html_to_markdown(HTML,URL)


def test_blocks_order_optional_and_nested_dedup(monkeypatch):
    blocks=[{'name':'inside','selector':'b'},{'name':'specs','selector':'.specs'},
            {'name':'missing','selector':'.none'}, {'name':'card','selector':'.card'}]
    setup(monkeypatch,dict(BASE,extract={'markdown':{'content_blocks':blocks,'remove_selectors':['.noise']}}))
    out=te.html_to_markdown(HTML,URL)
    assert out.count('25 м')==1 and out.index('25 м')<out.index('Насос')


def test_overlap_same_priority_fails(monkeypatch):
    other=copy.deepcopy(RULE);other['name']='other'
    setup(monkeypatch,dict(BASE,extract={'page_rules':[RULE,other]}))
    with pytest.raises(ProfileRuleError,match='Неоднозначные'):te.html_to_markdown(HTML,URL)


def test_higher_priority_wins():
    other=copy.deepcopy(RULE);other['name']='other';other['priority']=20
    p=profile(dict(BASE,extract={'page_rules':[RULE,other]}))
    assert select_page(p,URL,'',BeautifulSoup(HTML,'html.parser')).diagnostics['selected_rule']=='other'


def test_rule_adapter_without_container(monkeypatch):
    from types import SimpleNamespace
    rule={'name':'adapter','match':{'page_types':['product']},'extract':{'tier':'custom','custom_module':'areopag_spb_ru'}}
    setup(monkeypatch,dict(BASE,extract={'page_rules':[rule]}))
    monkeypatch.setattr(te,'_get_adapter_by_module',lambda name:SimpleNamespace(extract=lambda html,url:'Адаптер 123'))
    assert 'Адаптер 123' in te.html_to_markdown(HTML,URL)


def rules_file(tmp_path):
    rules={'schema_version':2,'version':1,'defaults':{'extract':{'keep_hidden_tabs':True,'markdown':{'remove_selectors':['.global']}}},
           'groups':{'pumps':{'version':3,'settings':{'extract':{'markdown':{'product_container':'.card','remove_selectors':['.group']}}}}}}
    (tmp_path/'_rules.yaml').write_text(yaml.safe_dump(rules))
    return rules


def inherited():
    return dict(BASE,extends={'rules_version':1,'group':'pumps','group_version':3},
                extract={'keep_hidden_tabs':False,'markdown':{'remove_selectors':[]}})


def test_merge_explicit_false_empty_and_null(tmp_path):
    rules_file(tmp_path)
    data,info=compose(inherited(),tmp_path)
    p=profile(data)
    assert not p.extract.keep_hidden_tabs and p.extract.markdown.remove_selectors==[]
    assert p.extract.markdown.product_container=='.card'
    assert info['origins']['extract.markdown.product_container']=='group:pumps'
    assert info['origins']['extract.markdown.remove_selectors']=='site'
    assert merge({'x':1},{'x':None})=={'x':None}


def test_resolver_explain_reload_and_dependency_hash(tmp_path):
    rules=rules_file(tmp_path)
    (tmp_path/'test.example.yaml').write_text(yaml.safe_dump(inherited()))
    resolver=ProfileResolver(tmp_path)
    before=resolver.explain(URL)
    assert not before['load_errors']
    assert resolver.reload_if_changed() is False
    rules['groups']['pumps']['settings']['extract']['markdown']['product_container']='#changed'
    (tmp_path/'_rules.yaml').write_text(yaml.safe_dump(rules))
    assert resolver.reload_if_changed()
    after=resolver.explain(URL)
    assert before['profile']['diagnostics']['effective_sha256']!=after['profile']['diagnostics']['effective_sha256']
    assert after['profile']['extract']['markdown']['product_container']=='#changed'


@pytest.mark.parametrize('key,value',[('rules_version',2),('group_version',4),('group','absent'),('rules_version',True)])
def test_wrong_reference_is_rejected(tmp_path,key,value):
    rules_file(tmp_path);data=inherited();data['extends'][key]=value
    with pytest.raises(ProfileRuleError):compose(data,tmp_path)


def test_duplicate_yaml_and_cycle_rejected():
    with pytest.raises(ProfileRuleError):load_yaml('domain: a\ndomain: b')
    with pytest.raises((ValueError,yaml.YAMLError)):load_yaml('x: &x {y: *x}')


@pytest.mark.parametrize('extract',[
    {'markdown':{'product_container':'['}},
    {'markdown':{'remove_selectors':['[']}},
    {'markdown':{'max_len':0}},
    {'markdown':{'min_len':20,'max_len':10}},
    {'markdown':{'content_blocks':[{'name':'a','selector':'['}]}},
    {'markdown':{'content_blocks':[{'name':'a','selector':None}]}},
    {'markdown':{'content_blocks':[{'name':'a','selector':'b','required':'yes'}]}},
    {'markdown':{'content_blocks':[{'name':'a','selector':'b'},{'name':'a','selector':'i'}]}},
    {'tier':'custom','custom_module':'../invalid'},
    {'tier':'custom','custom_module':'missing_adapter_abc'},
    {'page_rules':[dict(RULE,priority=True)]},
    {'page_rules':[dict(RULE,match={'path_regex':'['})]},
    {'page_rules':[dict(RULE,match={'selector':None})]},
    {'page_rules':[dict(RULE,match={'page_types':['unknown']})]},
    {'page_rules':[dict(RULE,extract={'page_rules':[]})]},
    {'page_rules':[RULE,RULE]},
    {'page_rules':'bad'},
    {'markdown':{'content_blocks':'bad'}},
])
def test_invalid_v2_extract(extract):
    _,errors=profile_from_dict(dict(BASE,extract=extract));assert errors


@pytest.mark.parametrize('data',[
    dict(BASE,schema_version=3),dict(BASE,schema_version=True),dict(BASE,profile_version=None),
    dict(BASE,sections={'product_url_patterns':['[']}),dict(BASE,crawl={'contact_ready_selector':'['}),
    dict(BASE,extends=[]),dict(BASE,unexpected=True),
    {'domain':'test.example','extract':{'page_rules':[RULE]}},
])
def test_invalid_schema(data):
    assert profile_from_dict(data)[1]


def test_census_preserves_raw_v2_settings_and_false(tmp_path):
    rules_file(tmp_path);raw=inherited()
    effective,info=compose(raw,tmp_path);p=profile(effective);p.diagnostics=info
    draft=CensusCollector(RunMetricsCollector('test.example'),p).build_draft()
    assert draft['extends']==raw['extends'] and draft['extract']==raw['extract']
    assert draft['schema_version']==2 and draft['profile_version']==2


def test_legacy_profiles_all_load():
    resolver=ProfileResolver(Path(__file__).resolve().parents[1]/'profiles')
    assert len(resolver.known_domains())>=36
    assert not resolver.load_errors


def test_bad_v2_reference_does_not_become_generic(tmp_path,monkeypatch):
    (tmp_path/'test.example.yaml').write_text(yaml.safe_dump(inherited()))
    resolver=ProfileResolver(tmp_path)
    import site_profiles
    monkeypatch.setattr(site_profiles,'get_resolver',lambda:resolver)
    with pytest.raises(ProfileRuleError):te.html_to_markdown(HTML,URL)


def test_rule_origin_is_recorded():
    p=profile(dict(BASE,extract={'page_rules':[RULE]}))
    chosen=select_page(p,URL,'',BeautifulSoup(HTML,'html.parser'))
    assert chosen.diagnostics['origins']['extract.markdown.product_container']=='page_rule:product'


def test_dashboard_uses_shared_rules_validation(tmp_path,monkeypatch):
    from dashboard import profiles_store
    monkeypatch.setattr(profiles_store,'profiles_dir',lambda:str(tmp_path))
    rules_file(tmp_path)
    assert profiles_store.validate('test.example',yaml.safe_dump(inherited()))==[]
    bad=inherited();bad['extends']['group_version']=4
    assert profiles_store.validate('test.example',yaml.safe_dump(bad))
    assert profiles_store.validate('test.example','domain: test.example\ndomain: test.example')


def test_census_writes_v2_without_losing_group_reference(tmp_path,monkeypatch):
    from site_profiles import get_resolver, reset_resolver
    reset_resolver();rules_file(tmp_path)
    (tmp_path/'test.example.yaml').write_text(yaml.safe_dump(inherited()))
    resolver=get_resolver(tmp_path)
    collector=CensusCollector(RunMetricsCollector('test.example'),resolver.resolve(URL))
    try:
        written=collector.write_draft(str(tmp_path/'drafts'))
        assert written
        assert yaml.safe_load(Path(written).read_text())['extends']==inherited()['extends']
    finally:reset_resolver()


@pytest.mark.parametrize('key',['schema_version','"schema_version"',"'schema_version'"])
def test_invalid_versioned_yaml_blocks_generic(tmp_path,monkeypatch,key):
    import site_profiles
    (tmp_path/'test.example.yaml').write_text(key+': 2\ndomain: test.example\ndomain: test.example')
    monkeypatch.setattr(site_profiles,'get_resolver',lambda:ProfileResolver(tmp_path))
    with pytest.raises(ProfileRuleError):te.html_to_markdown(HTML,URL)


def test_selected_rule_can_reset_nullable_container(monkeypatch):
    rule={'name':'reset','match':{'page_types':['product']},'extract':{'markdown':{'product_container':None,'content_blocks':[{'name':'spec','selector':'.specs'}]}}}
    setup(monkeypatch,dict(BASE,extract={'markdown':{'product_container':'.card'},'page_rules':[rule]}))
    out=te.html_to_markdown(HTML,URL)
    assert '25 м' in out and 'Насос' not in out


def test_empty_dictionary_does_not_erase_origins(tmp_path):
    rules_file(tmp_path)
    data=inherited();data['extract']={'markdown':{}}
    effective,info=compose(data,tmp_path)
    assert effective['extract']['markdown']['product_container']=='.card'
    assert info['origins']['extract.markdown.product_container']=='group:pumps'


@pytest.mark.parametrize('url',['https://alias.example/p','https://region.test.example/p'])
def test_broken_v2_aliases_do_not_become_generic(tmp_path,url):
    data=inherited();data['crawl']={'equivalent_domains':['alias.example'],'subdomain_collapse':['*.test.example']}
    (tmp_path/'test.example.yaml').write_text(yaml.safe_dump(data))
    with pytest.raises(ProfileRuleError):ProfileResolver(tmp_path).resolve(url)


def test_exact_profile_precedes_broken_alias(tmp_path):
    data=inherited();data['crawl']={'equivalent_domains':['alias.example']}
    (tmp_path/'test.example.yaml').write_text(yaml.safe_dump(data))
    (tmp_path/'alias.example.yaml').write_text(yaml.safe_dump(dict(BASE,domain='alias.example')))
    assert ProfileResolver(tmp_path).resolve('https://alias.example').domain=='alias.example'
