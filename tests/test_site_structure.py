"""Обнаружение структуры на изолированном сайте; сеть/продовые записи запрещены."""
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from config import Config
from fetch_outcome import FetchOutcome
from web_crawler import WebCrawler
from site_profiles import profile_from_dict
from site_profiles.structure import StructureDiscovery, DiscoveryLimits, html_shape, url_shape
from site_profiles.discover_structure import SavedCorpus, execute, new_output

BASE='https://factory.test/'
PROFILE={'domain':'factory.test','sections':{'catalog_roots':['/catalog/'],
        'product_url_patterns':['^/catalog/[^/]+/[^/]+/?$'],
        'contacts_urls':['/contacts/'],'distributor_urls':['/contacts/dealers/']},
         'crawl':{'exclude_paths':['/private/']}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*a,**kw):raise AssertionError('Сеть/производственный пайплайн запрещены')
    monkeypatch.setattr('socket.socket.connect',fail)
    monkeypatch.setattr(WebCrawler,'crawl_site',fail)
    monkeypatch.setattr(WebCrawler,'_download_site_files',fail)


@pytest.fixture
def crawler(tmp_path):
    config=Config();config.base_dir=str(tmp_path);config.graph_db_enable=False
    c=WebCrawler(config);c.current_base_url=BASE
    c.profile,errors=profile_from_dict(PROFILE);assert not errors
    c.url_categorizer.set_profile(c.profile)
    return c


def data_fetch(data,calls):
    async def fetch(url,role):
        calls.append(url)
        if url not in data:return FetchOutcome(url,reason='not_in_saved_corpus',retryable=False)
        html=data[url]
        return html if isinstance(html,FetchOutcome) else FetchOutcome(url,url,'saved',200,'ok',False,content=html)
    return fetch


def run(crawler,data,limits=None,**kwargs):
    calls=[];d=StructureDiscovery(crawler,BASE,limits)
    report=asyncio.run(d.run(data_fetch(data,calls),sitemap=False,**kwargs))
    return report,calls


def by_url(report):return {r['url']:r for r in report['urls']}


def test_catalog_base_pagination_and_query_variants(crawler):
    data={BASE:'<a href="/catalog/">Каталог</a>',
          BASE+'catalog/':'<base href="/catalog/"><a href="pumps/p1/">Насос</a><a rel="next" href="?page=2">2</a>',
          BASE+'catalog/?page=2':'<a href="/catalog/pumps/p2/?variant=red">Товар</a><a href="/catalog/pumps/p2/?variant=blue">Товар</a>',
          BASE+'catalog/pumps/p1/':'<main><h1>Насос</h1><table><tr><td>12 кг</td></tr></table></main>',
          BASE+'catalog/pumps/p2/?variant=red':'<main>Красный</main>',BASE+'catalog/pumps/p2/?variant=blue':'<main>Синий</main>'}
    report,calls=run(crawler,data)
    assert set(calls)==set(data)
    rows=by_url(report)
    assert rows[BASE+'catalog/']['role']=='category'
    assert rows[BASE+'catalog/pumps/p1/']['role']=='product'
    assert {'kind':'pagination','parent':BASE+'catalog/'} in rows[BASE+'catalog/?page=2']['sources']
    assert report['coverage']['cards']=='not_run'


def test_exclusions_files_and_external_kept_but_not_fetched(crawler):
    report,calls=run(crawler,{BASE:'<a href="/private/a">Секрет</a><a href="/manual.pdf">PDF</a><a href="https://outside.test/p">Внешняя</a>'})
    rows=by_url(report);assert calls==[BASE]
    assert rows[BASE+'private/a']['reason']=='url_excluded'
    assert rows[BASE+'manual.pdf']['reason']=='document_not_fetched'
    assert rows['https://outside.test/p']['reason']=='out_of_scope'
    assert all(r['key'] in report['sample'] for r in rows.values())


def test_redirect_to_excluded_or_outside_rejected(crawler):
    for final,reason in [(BASE+'private/p','redirect_url_excluded'),('https://outside.test/','redirect_out_of_scope')]:
        report,_=run(crawler,{BASE:FetchOutcome(BASE,final,'saved',200,'ok',False,content='<a href="/secret">x</a>')})
        assert report['urls'][0]['reason']==reason
        assert len(report['urls'])==1


def test_html_group_stable_text_rows_distinguishes_template():
    a='<main><h1>А</h1><table><tr><td>12</td></tr></table></main>'
    b='<main><h1>Б</h1><table><tr><td>14</td></tr><tr><td>18</td></tr></table></main>'
    c='<main><h1>А</h1><dl><dt>Вес</dt><dd>12</dd></dl></main>'
    assert html_shape(BeautifulSoup(a,'html.parser'))==html_shape(BeautifulSoup(b,'html.parser'))
    assert html_shape(BeautifulSoup(a,'html.parser'))!=html_shape(BeautifulSoup(c,'html.parser'))


def test_diverse_sample_rare_html_negative_and_missing(crawler):
    data={BASE:''.join(f'<a href="/catalog/pumps/p{i}/">Насос</a>' for i in range(10))+'<a href="/contacts/">Контакты</a>'}
    for i in range(10):data[BASE+f'catalog/pumps/p{i}/']='<main><table><tr><td>12</td></tr></table></main>' if i<9 else '<main><dl><dt>Вес</dt><dd>12</dd></dl></main>'
    report,_=run(crawler,data,DiscoveryLimits(sample=6))
    rows=by_url(report)
    assert rows[BASE+'catalog/pumps/p9/']['key'] in report['sample']
    assert rows[BASE+'contacts/']['key'] in report['sample']
    assert any(g['html_shape'] is not None for g in report['groups'])
    assert report['coverage']['status']=='partial'


def test_mandatory_holdout_separated_and_no_link_leak(crawler):
    data={BASE:'<p>Главная</p>',BASE+'control/':'<h1>Контроль</h1>',BASE+'holdout/':'<a href="/leak/">Нельзя учить</a>'}
    report,_=run(crawler,data,DiscoveryLimits(sample=1),controls=[BASE+'control/'],holdouts=[BASE+'holdout/'])
    rows=by_url(report);assert BASE+'leak/' not in rows
    assert rows[BASE+'control/']['forced'] and not rows[BASE+'control/']['independently_discovered']
    assert rows[BASE+'holdout/']['sample_split']=='holdout'
    assert rows[BASE+'holdout/']['key'] in report['sample']
    assert 'mandatory_sample_overflow' in report['coverage']['gaps']


def test_control_also_discovered_is_marked(crawler):
    report,_=run(crawler,{BASE:'<a href="/control/">Товар</a>',BASE+'control/':'ok'},controls=[BASE+'control/'])
    assert by_url(report)[BASE+'control/']['independently_discovered']


def test_holdout_overlap_refused(crawler):
    with pytest.raises(ValueError):run(crawler,{},controls=[BASE],holdouts=[BASE])


@pytest.mark.parametrize('limits,reason',[(DiscoveryLimits(pages=1),'page_limit'),(DiscoveryLimits(depth=0),'traversal_limit'),(DiscoveryLimits(urls=1),'url_limit')])
def test_caps_are_visible(crawler,limits,reason):
    report,calls=run(crawler,{BASE:'<a href="/catalog/pumps/a/">А</a>'},limits)
    assert reason in report['coverage']['gaps'];assert len(calls)==1


def test_failed_fetch_and_dynamic_not_success(crawler):
    report,_=run(crawler,{BASE:'<button>Показать ещё</button><a href="/contacts/">Контакты</a>',BASE+'contacts/':FetchOutcome(BASE+'contacts/',reason='challenge')})
    assert 'dynamic_not_exhausted' in report['coverage']['gaps']
    assert 'fetch_failures' in report['coverage']['gaps']
    assert by_url(report)[BASE+'contacts/']['reason']=='challenge'


def test_tilda_states_distinct(crawler):
    page='<div id="rec55"><div data-product-lid="1"><a href="#prodpopup">Товар</a></div><div data-product-lid="2"><a href="#prodpopup">Товар</a></div></div>'
    report,calls=run(crawler,{BASE:page})
    assert BASE+'#!/tproduct/55-1' in calls and BASE+'#!/tproduct/55-2' in calls
    assert len({r['key'] for r in report['urls']})==len(report['urls'])


def test_contact_distributor_roles_and_v2_page_rule(crawler):
    p=dict(PROFILE,schema_version=2,extract={'page_rules':[{'name':'contact','match':{'page_types':['distributor'],'selector':'main'},'extract':{}}]})
    crawler.profile,errors=profile_from_dict(p);assert not errors;crawler.url_categorizer.set_profile(crawler.profile)
    report,_=run(crawler,{BASE:'<a href="/contacts/dealers/">Дилеры</a>',BASE+'contacts/dealers/':'<main>Наши дилеры</main>'})
    row=by_url(report)[BASE+'contacts/dealers/']
    assert set(row['roles'])=={'contacts','distributor'} and row['page_rule']=='contact'


def test_sitemap_index_uses_existing_parser_and_records_exclusions(crawler):
    d=StructureDiscovery(crawler,BASE)
    resources={BASE+'robots.txt':b'Sitemap: https://factory.test/index.xml',
      BASE+'index.xml':b'<sitemapindex><sitemap><loc>/products.xml</loc></sitemap></sitemapindex>',
      BASE+'products.xml':b'<urlset><url><loc>/catalog/pumps/p1/</loc></url><url><loc>/private/a</loc></url><url><loc>/en/catalog/p2/</loc></url></urlset>'}
    async def request(url,method='GET'):return (200 if url in resources else 404),url,{},resources.get(url,b'')
    crawler.sitemap_parser._request=request
    report=asyncio.run(d.run(data_fetch({BASE:'home',BASE+'catalog/pumps/p1/':'<main>Насос</main>'},[])))
    rows=by_url(report)
    assert rows[BASE+'catalog/pumps/p1/']['state']=='loaded'
    assert rows[BASE+'private/a']['state']=='excluded'
    assert rows[BASE+'en/catalog/p2/']['state']=='excluded'
    assert any(s['kind']=='sitemap' and s['parent']==BASE+'products.xml' for s in rows[BASE+'private/a']['sources'])
    assert not crawler.sitemap_parser.discovery_sink


@pytest.mark.parametrize('body',[ '<urlset><url><loc>https://factory.test/catalog/p1</loc></url><url><loc>https://factory.test/catalog/p2</loc></url></urlset>', '<html><a href="/a">a</a><a href="/b">b</a></html>',BASE+'a\n'+BASE+'b'])
def test_sitemap_scan_limit_reported(crawler,body):
    events=[];crawler.sitemap_parser.discovery_sink=lambda event,**data:events.append((event,data))
    crawler.config.sitemap_max_scan_urls=1
    async def request(url,method='GET'):return 200,url,{},body.encode()
    crawler.sitemap_parser._request=request
    asyncio.run(crawler.sitemap_parser.parse_sitemap(BASE+'sitemap.xml'))
    assert any(e=='limit' and d['reason']=='scan_limit' for e,d in events)


def test_same_inputs_deterministic(crawler):
    data={BASE:'<a href="/catalog/b/p2/">2</a><a href="/catalog/a/p1/">1</a>',BASE+'catalog/a/p1/':'<main>A</main>',BASE+'catalog/b/p2/':'<main>B</main>'}
    a,_=run(crawler,data);crawler.url_categorizer.set_profile(crawler.profile);b,_=run(crawler,data)
    assert a==b


def test_timeout_is_reported(crawler):
    async def slow(url,role):await asyncio.sleep(10)
    d=StructureDiscovery(crawler,BASE,DiscoveryLimits(seconds=1))
    report=asyncio.run(d.run(slow,sitemap=False))
    assert report['urls'][0]['reason']=='timeout'


@pytest.mark.parametrize('field,value',[('pages',0),('urls',-1),('depth',-1),('seconds',False),('sample',0)])
def test_bad_limits(field,value):
    with pytest.raises(ValueError):DiscoveryLimits(**{field:value})


def test_shapes_keep_query_names_not_values():
    assert url_shape(BASE+'catalog/p/a?variant=red')==url_shape(BASE+'catalog/p/b?variant=blue')
    assert url_shape(BASE+'catalog/p/a?page=2')!=url_shape(BASE+'catalog/p/a?p=2')


def test_cli_saved_isolated_no_original_changes(tmp_path,monkeypatch):
    import site_profiles.discover_structure as cli
    monkeypatch.setattr(cli,'workspace',lambda:tmp_path)
    profiles=tmp_path/'profiles';profiles.mkdir()
    (profiles/'factory.test.yaml').write_text('domain: factory.test\n')
    html=tmp_path/'saved.html';html.write_text('<main><a href="/manual.pdf">Файл</a></main>')
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'pages':{BASE:{'path':'saved.html'}}}))
    args=SimpleNamespace(url=BASE,profiles=str(profiles),output=str(tmp_path/'run'),manifest=str(manifest),
                         live=False,pages=5,urls=30,depth=3,seconds=10,sample=5,control=[],holdout=[],no_sitemap=True)
    output=asyncio.run(execute(args))
    report=json.loads((output/'structure.json').read_text())
    assert report['isolation']['graph_enabled'] is False
    assert report['mode']=='saved' and report['coverage']['live']=='not_checked'
    assert html.read_text()=='<main><a href="/manual.pdf">Файл</a></main>'
    assert len(list((output/'html').glob('*.html')))==1
    with pytest.raises(ValueError):new_output(output)
    with pytest.raises(ValueError):new_output(tmp_path.parent/'outside')


def test_output_symlink_escape_refused(tmp_path,monkeypatch):
    import site_profiles.discover_structure as cli
    root=tmp_path/'codex_work';root.mkdir();outside=tmp_path/'prod';outside.mkdir()
    (root/'redirect').symlink_to(outside,target_is_directory=True)
    monkeypatch.setattr(cli,'workspace',lambda:root)
    with pytest.raises(ValueError):new_output(root/'redirect'/'run')
    assert not list(outside.iterdir())


def test_previous_zero_cards_retained_with_provenance(crawler):
    d=StructureDiscovery(crawler,BASE,card_outcomes={BASE:{'count':0,'source':'saved_run/report.json'}})
    report=asyncio.run(d.run(data_fetch({BASE:'<main>Насос</main>'},[]),sitemap=False))
    assert 'zero_cards_previous_run' in report['urls'][0]['features']
    assert report['urls'][0]['previous_card_outcome']['source']=='saved_run/report.json'
    assert report['coverage']['cards']=='not_run'


def test_all_returned_dynamic_states_reveal_links(crawler):
    result=FetchOutcome(BASE,BASE,'dynamic',200,'ok',False,content=[
        '<main><a href="/catalog/pumps/p1/">1</a></main>',
        '<main><a href="/catalog/valves/v1/">2</a></main>'])
    report,calls=run(crawler,{BASE:result})
    assert BASE+'catalog/pumps/p1/' in calls and BASE+'catalog/valves/v1/' in calls
    assert by_url(report)[BASE]['extra_states'][0]['html_group']
    assert 'multiple_states_need_review' in report['coverage']['gaps']


def test_mandatory_urls_cannot_silently_overflow(crawler):
    with pytest.raises(ValueError):
        run(crawler,{},DiscoveryLimits(urls=1),controls=[BASE+'required/'])


def test_data_attributes_and_catalog_grid(crawler):
    report,calls=run(crawler,{BASE:'<div data-href="/catalog/pumps/a/">Насос</div><div onclick="location.href=\'/catalog/pumps/b/\'">Насос</div>'})
    assert BASE+'catalog/pumps/a/' in calls and BASE+'catalog/pumps/b/' in calls


def test_sitemap_observer_does_not_change_parser_results(crawler):
    body='<urlset><url><loc>/catalog/pumps/p1/</loc></url><url><loc>/private/a</loc></url></urlset>'
    async def request(url,method='GET'):return 200,url,{},body.encode()
    parser=crawler.sitemap_parser;parser._request=request
    before=asyncio.run(parser.parse_sitemap(BASE+'sitemap.xml'))
    asyncio.run(parser.reset_run());events=[];parser.discovery_sink=lambda e,**d:events.append((e,d))
    after=asyncio.run(parser.parse_sitemap(BASE+'sitemap.xml'))
    assert before==after and len(events)==2


def test_runner_blocks_exclusions_on_transport_redirect(tmp_path):
    import yaml
    import site_profiles.discover_structure as cli
    profiles=tmp_path/'profiles';profiles.mkdir()
    (profiles/'factory.test.yaml').write_text(yaml.safe_dump(PROFILE))
    output=tmp_path/'run';output.mkdir()
    old=cli.resolver_module._resolver
    try:
        c=cli.make_crawler(output,profiles,BASE,DiscoveryLimits())
        assert c._is_allowed_url(BASE+'catalog/pumps/p1/')
        assert not c._is_allowed_url(BASE+'private/a')
        assert not c._is_allowed_final_url(BASE,BASE+'private/a')
        assert not c.config.graph_db_enable
        assert Path(c.config.base_dir).is_relative_to(output)
    finally:cli.resolver_module._resolver=old


def test_zero_card_evidence_needs_source(crawler):
    with pytest.raises(ValueError):StructureDiscovery(crawler,BASE,card_outcomes={BASE:{'count':0}})


def test_no_holdout_is_explicit_gap(crawler):
    report,_=run(crawler,{BASE:'<main>Описание</main>'})
    assert 'holdout_not_provided' in report['coverage']['gaps']


def test_flat_product_urls_form_template():
    assert url_shape(BASE+'pump-a.html')==url_shape(BASE+'pump-b.html')
    assert url_shape(BASE+'123')==url_shape(BASE+'456')
