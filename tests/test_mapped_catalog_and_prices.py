import asyncio
from types import SimpleNamespace
from pathlib import Path

from config import Config
from main import MonitoringSystem
from site_profiles import profile_from_dict
from web_crawler import WebCrawler, URLCategorizer, FileDownloadManager
from test_crawl_perimeter_files import Response, Session

LINK = 'https://elconnect.ru/production/price/general/7332/'
FINAL = 'https://elconnect.ru/content/elconnect/pics/price/elconnect_common-industrial_price.xls'
HEADER = bytes.fromhex('D0 CF 11 E0 A1 B1 1A E1')


def test_no_second_pass_for_declared_product_lists():
    profile, errors = profile_from_dict({'domain': 'pspcom.ru', 'extract': {'second_pass': False}})
    assert not errors and not profile.extract.second_pass
    assert profile_from_dict({'domain':'example.org'})[0].extract.second_pass
    system = SimpleNamespace(config=Config(), crawler=SimpleNamespace(profile=profile))
    assert asyncio.run(MonitoringSystem._second_pass_product_extraction(system, [{'category':'category'}], {}, None, 'unused')) == []


def test_menu_prices_follow_profile_links_and_validate_bytes(tmp_path, monkeypatch):
    profile, errors = profile_from_dict({'domain':'elconnect.ru','sections':{'price_list_urls':[LINK]}})
    assert not errors
    crawler = WebCrawler.__new__(WebCrawler)
    crawler.profile = profile
    crawler.current_base_url = 'https://elconnect.ru/'
    crawler.host_throttle = crawler.processing_tracker = crawler.metrics_collector = None
    crawler.url_categorizer = URLCategorizer()
    crawler.url_categorizer.set_profile(profile)
    config = Config()
    config.base_dir = str(tmp_path)
    manager = crawler.file_download_manager = FileDownloadManager(crawler,config)
    assert crawler._is_company_file(LINK)
    assert crawler.url_categorizer.categorize_url(LINK)[0]=='excluded'
    assert not crawler._is_company_file(LINK.replace('7332','9999'))
    session = Session({LINK:Response(LINK,302,{'Location': FINAL}),FINAL:Response(FINAL,headers={'Content-Type':'application/vnd.ms-excel'},body=b'error')})
    class Client:
        async def __aenter__(self):return session
        async def __aexit__(self,*exc):pass
    monkeypatch.setattr('web_crawler.aiohttp.ClientSession',lambda **kw:Client())
    monkeypatch.setattr('web_crawler.aiohttp.TCPConnector',lambda **kw:None)
    directory=tmp_path/'Price_lists'
    directory.mkdir()
    html=f'<a href="{LINK}">Скачать</a>'
    async def exercise():
        for content,expected in [(b'error',False),(HEADER+b'xls',True)]:
            session.responses[FINAL].body=content
            await crawler._download_explicit_profile_documents('https://elconnect.ru/',html,{'price_lists_dir':str(directory)})
            file=directory/Path(FINAL).name
            assert file.exists()==expected
            if expected:assert file.read_bytes()==content
    asyncio.run(exercise())
    assert len(session.calls)==4


def test_one_saved_page_can_supply_dealers_and_company():
    url='https://zavoduniversal.ru/about/contacts/'
    profile, errors=profile_from_dict({'domain':'zavoduniversal.ru','sections':{'distributor_urls':['/about/contacts/'],'contacts_urls':['/about/contacts/']}})
    assert not errors
    contact={'metadata':{'url':url,'category':'contacts'}}
    class Storage:
        async def get_company_files(self,name,category):
            return [] if category=='distributor' else [contact,contact,{'metadata':{'category':'contacts','url':url+'moscow/'}}]
    system=SimpleNamespace(crawler=SimpleNamespace(profile=profile,temp_storage=Storage()))
    assert asyncio.run(MonitoringSystem._distributor_source_pages(system,'Завод'))==[contact]


def test_mixed_file_index_disables_whole_page_download():
    profile,errors=profile_from_dict({'domain':'factory.test','sections':{'file_index_paths':['/company/docs/'],'documents_urls':['/company/']}})
    assert not errors
    cat=URLCategorizer()
    cat.set_profile(profile)
    url='https://factory.test/company/docs'
    assert cat.categorize_url(url)[0]=='other'
    assert cat.profile_document_section(url) is None
    assert cat.profile_document_section(url+'/manuals')=='documents'


def test_tss_nested_slider_keeps_product_content():
    from bs4 import BeautifulSoup
    from Profile_Markdown.tss_ru import clean

    soup = BeautifulSoup('<div>SDG 5500EHA</div><div class="sliderdop-a"><div>Аналог</div></div>', 'lxml')
    clean(soup)
    assert soup.get_text(strip=True) == 'SDG 5500EHA'
