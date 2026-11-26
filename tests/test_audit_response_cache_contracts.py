"""Повторяемость обрыва, независимость артефактов и адресные ревизии кэша."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import ai_integration
from ai_integration import AITunnelClient
from product_extraction_revision import product_extraction_revision

DOMAINS=['npo-stekloplastic.ru','nzmk22.ru','bsdbel.ru','ventexpress.ru',
         'zavodmel.ru','quality-pro.ru','specbolt.ru','xotpipe.ru','ecosgroup.com',
         'landshaft.ru','mlkz.by','st-paint.ru','bitumex.org','nizhegorodsetkabel.ru','bkmz.ru']

@pytest.mark.parametrize('content', ['{"product":{"product_name":"Насос","specifications":{"Масса":"40 кг"}}}', '{"error":"Trash_418#"}', '{"product":'])
def test_length_is_retryable_even_when_content_is_valid_json(content):
    client=AITunnelClient('https://example.invalid/v1','test',model='gemma4-31b-nvfp4')
    tokens={'input_tokens':11,'output_tokens':7}
    client._make_request_with_retry=AsyncMock(return_value=({'choices':[{'message':{'content':content},'finish_reason':'length'}]},tokens))
    client._save_sgr_error_response=Mock()
    result,usage=asyncio.run(client._sgr_single('Видимая страница','Завод','https://example.invalid'))
    assert result is None and usage==tokens
    client._save_sgr_error_response.assert_called_once_with('single_length',content,0)


def test_sgr_errors_in_same_second_are_distinct_artifacts(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai_integration.time,'time',lambda:1793984400)
    client=AITunnelClient('https://example.invalid/v1','test')
    client._save_sgr_error_response('single_length','first page',0)
    client._save_sgr_error_response('single_length','second page',0)
    paths=list((tmp_path/'logs').glob('sgr_error_single_length_*.json'))
    assert len(paths)==2
    assert {json.loads(p.read_text())['content'] for p in paths}=={'first page','second page'}

@pytest.mark.parametrize('domain',DOMAINS)
def test_revision_scope_accepts_exact_15_hosts_and_www(domain):
    assert product_extraction_revision('https://'+domain+'/catalog/item')=='visible-html-quality-v1-20261106'
    assert product_extraction_revision('http://www.'+domain+'/catalog/item')=='visible-html-quality-v1-20261106'
    assert product_extraction_revision('https://'+domain+'.example.org/catalog/item') is None

@pytest.mark.parametrize('url,expected',[
    ('https://example.ru/catalog/item',None),
    ('https://tegola.ru/product/item','tegola-v3-public-variants-20261030'),
    ('https://tegola.ru/catalog/item',None),
    ('https://vitgroup.ru/catalog#!/tproduct/123-456','tilda-product-popup-v1-20261102'),
    ('https://fzlomov.ru/#product-1','inline-products-v1-20261102'),
])
def test_other_domains_keep_existing_revision_contract(url,expected):
    assert product_extraction_revision(url)==expected

@pytest.mark.parametrize('domain,extra_call',[(d,True) for d in DOMAINS]+[('example.ru',False),('factory.ru',False)])
def test_legacy_cache_reextracts_only_for_15_domains_then_current_cache_reuses(domain,extra_call):
    import main
    from types import SimpleNamespace
    async def run():
        url='https://'+domain+'/catalog/cache-probe'
        product={'product_id':'one','source_url':url,'name':'Насос'}
        metadata={'url':url,'checkpoints':{'ai_processed':True},'product_result':product}
        storage=SimpleNamespace(load_cleaned_html=AsyncMock(return_value={'html_content':'<p>Видимая страница</p>','metadata':metadata}),save_product_result=AsyncMock())
        system=main.MonitoringSystem.__new__(main.MonitoringSystem)
        system.crawler=SimpleNamespace(temp_storage=storage,_product_dedup_key=lambda url:url)
        system.statistics=main.Statistics()
        system._processing_urls_lock=asyncio.Lock()
        system._processing_product_ids_lock=asyncio.Condition()
        for attr in ['_global_processed_urls','_processing_urls','_global_processed_product_ids','_processing_product_ids','_global_processed_slug_keys']:
            setattr(system,attr,set())
        system.process_product_page=AsyncMock(return_value=product)
        page={'normalized_url':url,'file_path':'saved.html','category':'product'}
        company={'original_name':'Завод'}
        result=await system._process_single_page_with_checkpoint(page,company,main.CompanyStatistics(company),'.')
        assert result and system.process_product_page.await_count==int(extra_call)
        # Версия, сохранённая после успешного извлечения, разрешает повторный cache hit.
        metadata['product_extraction_revision']=product_extraction_revision(url)
        system._global_processed_urls.clear();system._global_processed_product_ids.clear()
        result=await system._process_single_page_with_checkpoint(page,company,main.CompanyStatistics(company),'.')
        assert result and system.process_product_page.await_count==int(extra_call)
    asyncio.run(run())


def test_main_product_pipeline_does_not_read_linked_docx(tmp_path,monkeypatch):
    import main
    import product_technical_sources
    from config import Config
    from web_crawler import WebCrawler
    from test_http_https_product_dedup import system_stub,product_ai
    probe=AsyncMock(side_effect=AssertionError('Attached DOCX must not supply product facts'))
    monkeypatch.setattr(product_technical_sources,'technical_sources_markdown',probe)
    html='<div id="k2Container" class="itemView"><h1 class="itemTitle">Эмаль</h1><p>Видимая карточка</p><div class="itemAttachments"><a href="/production/item/download/35">Технические характеристики</a></div></div>'
    cfg=Config();cfg.base_dir=str(tmp_path);crawler=WebCrawler(cfg)
    system=system_stub(crawler,tmp_path,monkeypatch,[product_ai('Эмаль')])
    company={'company_id':'1','original_name':'Завод'}
    url='http://mlkz.by/production/item'
    result=asyncio.run(main.MonitoringSystem.process_product_page(system,html,company,main.CompanyStatistics(company),{'url':url,'normalized_url':url,'storage_path':url,'category':'product'},str(tmp_path)))
    assert result is not None
    probe.assert_not_awaited()
    system.ai_client.extract_product_info.assert_awaited_once()


def test_current_visible_html_cache_not_invalidated_by_docx_attachment():
    import main
    from types import SimpleNamespace
    async def run():
        url='http://mlkz.by/production/item'
        product={'product_id':'one','source_url':url,'name':'Эмаль'}
        metadata={'url':url,'checkpoints':{'ai_processed':True},'product_result':product,
                  'product_extraction_revision':product_extraction_revision(url)}
        html='<div id="k2Container" class="itemView"><h1 class="itemTitle">Эмаль</h1><div class="itemAttachments"><a href="/production/item/download/35">Технические характеристики</a></div></div>'
        system=main.MonitoringSystem.__new__(main.MonitoringSystem)
        system.crawler=SimpleNamespace(temp_storage=SimpleNamespace(load_cleaned_html=AsyncMock(return_value={'html_content':html,'metadata':metadata})),_product_dedup_key=lambda url:url)
        system.statistics=main.Statistics()
        system._processing_urls_lock=asyncio.Lock();system._processing_product_ids_lock=asyncio.Condition()
        for attr in ['_global_processed_urls','_processing_urls','_global_processed_product_ids','_processing_product_ids','_global_processed_slug_keys']:
            setattr(system,attr,set())
        system.process_product_page=AsyncMock(side_effect=AssertionError('Current visible HTML should reuse cached product'))
        company={'original_name':'Завод'}
        result=await system._process_single_page_with_checkpoint({'normalized_url':url,'file_path':'saved.html','category':'product'},company,main.CompanyStatistics(company),'.')
        assert result['product_id']=='one'
        system.process_product_page.assert_not_awaited()
    asyncio.run(run())
