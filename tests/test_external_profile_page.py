from domain_equivalency import url_in_scope
from site_profiles import profile_from_dict
from web_crawler import URLCategorizer, WebCrawler

ROOT = 'https://brand.test/'
CONTACT = 'https://group.test/contacts/hotline.php'
PRICE = 'https://price.brand.test/'
REGION = PRICE + 'region.html'


def test_exact_external_pages_have_roles_without_aliasing_the_host():
    profile, errors = profile_from_dict({
        'domain': 'brand.test',
        'crawl': {'external_page_urls': [CONTACT, PRICE, REGION]},
        'sections': {'contacts_urls': [CONTACT], 'price_list_urls': [PRICE, REGION]},
    })
    assert not errors
    crawler = WebCrawler.__new__(WebCrawler)
    crawler.current_base_url = ROOT
    crawler.profile = profile
    router = URLCategorizer()
    router.set_profile(profile)
    for url, role in [(CONTACT, 'contacts'), (PRICE, 'price_list'), (REGION, 'price_list')]:
        assert crawler._is_allowed_url(url)
        assert crawler._is_allowed_url(url + '#text')
        assert router.categorize_url(url)[0] == role
        assert not crawler._is_allowed_url(url, resource='sitemap')
        assert not crawler._is_allowed_url(url, resource='file')
        assert not crawler._is_allowed_url(url + '?extra=1')
    assert router.profile_document_section(REGION) == 'price_list'
    assert router.profile_document_section(ROOT) is None
    assert router.categorize_url(ROOT)[0] == 'main_page'
    assert not crawler._is_allowed_url(CONTACT + '/child')
    assert not crawler._is_allowed_url(CONTACT.replace('group.test', 'other.test'))
    assert not url_in_scope(CONTACT, 'https://other.test', profile_domains=['brand.test'], external_page_urls=[CONTACT])
    router.set_profile(None)
    assert router.profile_document_section(REGION) is None
