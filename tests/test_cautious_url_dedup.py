"""Различия адресов сохраняются до очереди и payload; шум убирается только по доказанным правилам."""
import asyncio
from collections import deque

import pytest
from config import Config
from web_crawler import WebCrawler, PageContent
from test_http_https_product_dedup import system_stub, product_ai, extract, COMPANY
from graph_db_uploader import GraphDBDataFormatter
import main

@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = WebCrawler(cfg)
    c.current_base_url = 'https://factory.ru/'
    return c

@pytest.mark.parametrize('param', ['type','filter','version','format','region','view','key','auth','token',
    'sid','sessionid','source','ref','from','date','time','redirect','cache','sort','order','display',
    'orderby','set_filter','filter_model','p','ID'])
def test_semantic_parameters_remain_distinct(crawler,param):
    urls=[f'https://factory.ru/catalog/pump?{param}={v}' for v in ('A','B')]
    norm=crawler.url_normalizer.normalize_url
    assert norm(urls[0]) != norm(urls[1])
    assert crawler._scheme_dedup_key(urls[0]) != crawler._scheme_dedup_key(urls[1])
    assert crawler._product_dedup_key(urls[0]) != crawler._product_dedup_key(urls[1])

@pytest.mark.parametrize('left,right', [
 ('/catalog/pumps/standard','/catalog/motors/standard'),
 ('/ru/catalog/pump','/catalog/pump'),
 ('/catalog/Pump','/catalog/pump'),
 ('/catalog/pump;variant=1','/catalog/pump;variant=2'),
 ('/catalog/pump?x=','/catalog/pump'),
 ('/catalog/pump?x=A&x=B','/catalog/pump?x=B&x=A'),
 ('/catalog/®-pump','/catalog/'),
 ('/catalog/pump?brand=®&id=2','/catalog/pump?brand=®&id=3'),
])
def test_full_paths_and_query_values_are_identity(crawler,left,right):
    a,b=('https://factory.ru'+p for p in (left,right))
    assert crawler.url_normalizer.normalize_url(a)!=crawler.url_normalizer.normalize_url(b)
    assert crawler._product_dedup_key(a)!=crawler._product_dedup_key(b)

@pytest.mark.parametrize('urls', [
 ['https://factory.ru:8443/catalog/pump','https://factory.ru:8080/catalog/pump'],
 ['https://factory.ru/catalog/pump','https://other.ru/catalog/pump'],
 ['https://factory.ru/catalog/pump','https://sub.factory.ru/catalog/pump'],
 ['http://[::1]:8080/catalog/pump','http://[::1]:8443/catalog/pump'],
])
def test_host_port_and_holding_scope_are_not_product_equivalence(crawler,urls):
    crawler.config.equivalent_domains={'factory.ru':['other.ru']}
    assert len({crawler.url_normalizer.normalize_url(u) for u in urls})==2
    assert len({crawler._product_dedup_key(u) for u in urls})==2

@pytest.mark.parametrize('tracking', ['utm_source','utm_medium','utm_campaign','fbclid','gclid','msclkid','yclid'])
def test_tracking_deduplicates_without_removing_variant(crawler,tracking):
    a='https://factory.ru/catalog/pump?type=2'
    b=a+'&'+tracking+'=visit'
    assert crawler.url_normalizer.normalize_url(a)==crawler.url_normalizer.normalize_url(b)
    assert crawler._product_dedup_key(a)==crawler._product_dedup_key(b)
    assert crawler._product_dedup_key(a)!=crawler._product_dedup_key(b.replace('type=2','type=3'))


def test_query_order_www_scheme_and_fragment_contract(crawler):
    a='http://www.factory.ru/catalog/pump/?b=2&a=1#drawing'
    b='https://factory.ru/catalog/pump?a=1&b=2'
    assert crawler._product_dedup_key(a)==crawler._product_dedup_key(b)


def test_ru_exception_is_limited_to_verified_site_and_product_paths(crawler):
    n=crawler.url_normalizer.normalize_url
    path='/katalog-produktsii/item/osevyie-i-diagonalnyie-tipa--2/'
    assert n('https://uralgidromash.ru/ru'+path)==n('https://uralgidromash.ru'+path)
    assert n('https://factory.ru/ru'+path)!=n('https://factory.ru'+path)
    assert n('https://uralgidromash.ru/ru/contacts')!=n('https://uralgidromash.ru/contacts')


def test_poligon_sort_rule_is_limited_to_verified_listing(crawler):
    n=crawler.url_normalizer.normalize_url
    listing='https://poligonspb.ru/catalog/stabilizatory-napryazheniya/blokrkf'
    assert n(listing+'?display=list&orderBy=price&sortBy=asc')==n(listing)
    for url in ['https://poligonspb.ru/product/kf-3mk',listing+'/new-model',listing.replace('poligonspb','another')]:
        assert n(url+'?display=list')!=n(url+'?display=other')

@pytest.mark.parametrize('marker',['®ion=','%C2%AEion='])
def test_aerobel_region_repair_preserves_unrelated_data(crawler,marker):
    n=crawler.url_normalizer.normalize_url
    base='https://aerobel.ru/catalog/pump'
    assert n(base+'?region=belgorod')==n(base+'?region=kursk')==n(base)
    assert n(base+marker+'kursk&type=2')==n(base+'?type=2')
    assert n(base+'?region=belgorod'+marker+'kursk&id=2')==n(base+'?id=2')
    assert n(base+'?brand=®&id=2')!=n(base+'?brand=®&id=3')
    foreign=base.replace('aerobel','factory')
    assert n(foreign+'?region=kursk')!=n(foreign+'?region=belgorod')
    assert n(foreign+marker+'kursk')!=n(foreign)

@pytest.mark.parametrize('pair', [
 ('/catalog/pumps/standard','/catalog/motors/standard'),
 ('/catalog/pump?type=2','/catalog/pump?type=3'),
 ('/catalog/pump?filter_model=2','/catalog/pump?filter_model=3'),
 ('/ru/catalog/pump','/catalog/pump'),
])
@pytest.mark.parametrize('source',['links','sitemap'])
def test_queue_keeps_distinct_products(crawler,pair,source):
    async def run():
        urls=['https://factory.ru'+path for path in pair]
        for url in urls:
            assert crawler.url_categorizer.categorize_url(url)[0] != 'excluded'
        if source=='links':
            admitted=await asyncio.gather(*(crawler._should_add_to_queue_parallel(u,1,'product') for u in urls))
            assert admitted==[True,True]
        else:
            queue=deque()
            rows=[(u,crawler.url_normalizer.normalize_url(u),'product',9) for u in urls]
            await crawler._add_sitemap_urls_to_queue(queue,rows,crawler.current_base_url)
            assert len(queue)==2
        assert len(crawler.product_urls)==2
    asyncio.run(run())

@pytest.mark.parametrize('parallel',[False,True])
@pytest.mark.parametrize('pair',[
 ('https://factory.ru/catalog/pumps/standard','https://factory.ru/catalog/motors/standard'),
 ('https://factory.ru/catalog/pump?type=2','https://factory.ru/catalog/pump?type=3'),
 ('https://factory.ru/catalog/pump','https://other.ru/products/pump'),
])
def test_distinct_products_reach_cards_and_graph(crawler,tmp_path,monkeypatch,parallel,pair):
    crawler.config.equivalent_domains={'factory.ru':['other.ru']}
    stub=system_stub(crawler,tmp_path,monkeypatch,[product_ai('Насос А'),product_ai('Двигатель Б')])
    stats=main.CompanyStatistics(COMPANY)
    async def run():
        if parallel:return await asyncio.gather(*(extract(stub,stats,u) for u in pair))
        return [await extract(stub,stats,u) for u in pair]
    products=asyncio.run(run())
    assert all(products) and len({p['product_id'] for p in products})==2
    assert stub.docx_generator.generate_product_card.await_count==2
    payload=GraphDBDataFormatter.format_product_data(COMPANY['company_id'],COMPANY['original_name'],products)
    assert {p['source_url'] for p in payload['companies'][0]['products']}==set(pair)
    assert not stub._processing_product_ids and not stub._processing_slug_keys


def test_redirect_aliases_deduplicate_at_final_address(crawler,tmp_path,monkeypatch):
    stub=system_stub(crawler,tmp_path,monkeypatch,[product_ai('Насос А'),product_ai('Насос другое имя')])
    stats=main.CompanyStatistics(COMPANY)
    html='<html><head><link rel="canonical" href="/wrong"/></head><body>'+'Товар '*100+'</body></html>'
    async def run():
        final='https://factory.ru/catalog/pump/'
        docs=await asyncio.gather(*(crawler._parse_content(PageContent(html,u,final),u)
            for u in ['https://factory.ru/old','https://factory.ru/another']))
        assert all(doc.url==final for doc in docs)
        return await asyncio.gather(*(extract(stub,stats,doc.url) for doc in docs))
    products=asyncio.run(run())
    assert sum(p is not None for p in products)==1
    assert stub.docx_generator.generate_product_card.await_count==1

@pytest.mark.parametrize('left,right', [('%E0','%E1'),('%FF','%FE'),('%D0','%D1')])
def test_legacy_query_bytes_are_lossless(crawler,left,right):
    a='https://factory.ru/catalog/pump?filter='+left
    b='https://factory.ru/catalog/pump?filter='+right
    assert crawler.url_normalizer.normalize_url(a)==a
    assert crawler.url_normalizer.normalize_url(b)==b
    assert crawler._product_dedup_key(a)!=crawler._product_dedup_key(b)

@pytest.mark.parametrize('field,value', [
 ('ignored_query_params','region'),('query_noise_paths',[1]),
 ('strip_ru_prefix_paths',False),('repair_region_entity','yes')])
def test_invalid_identity_profile_is_rejected(field,value):
    from site_profiles import profile_from_dict
    _,errors=profile_from_dict({'domain':'factory.ru','crawl':{field:value}})
    assert errors


def test_identity_profile_roundtrip_and_no_cross_site_leak(crawler):
    from site_profiles import get_resolver, profile_from_dict
    from dataclasses import asdict
    resolver=get_resolver()
    for domain in ['aerobel.ru','uralgidromash.ru','poligonspb.ru']:
        profile=resolver.resolve(domain)
        restored,errors=profile_from_dict(asdict(profile))
        assert not errors and restored==profile
    n=crawler.url_normalizer.normalize_url
    for _ in range(2):
        assert n('https://aerobel.ru/catalog/x?region=1')==n('https://aerobel.ru/catalog/x?region=2')
        assert n('https://factory.ru/catalog/x?region=1')!=n('https://factory.ru/catalog/x?region=2')
