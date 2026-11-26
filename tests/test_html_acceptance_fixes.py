"""Живые отклонения: точечный шум не скрывает данные контактов и дилеров."""
import asyncio

import pytest

from html_comparison import html_comparison_hash as fingerprint
from contact_interactions import collect_contact_panels, needs_contact_interaction
from config import Config


def bitrix(time, **extra):
    import json
    data = {'SERVER_TIME': time, 'bitrix_sessid': 'token', **extra}
    return '<script>(window.BX||top.BX).message(' + json.dumps(data) + ');</script>'


def test_bitrix_server_time_only_is_ignored():
    assert fingerprint(bitrix(100)) == fingerprint(bitrix(200))
    assert fingerprint(bitrix(100, phone='123')) != fingerprint(bitrix(200, phone='456'))


@pytest.mark.parametrize('html', [
    '<script>window.data={"SERVER_TIME":100};</script>',
    '<script type="application/ld+json">{"SERVER_TIME":100}</script>',
    '<p>Дата обновления: 100</p>',
    '<script>(window.BX||top.BX).message({"SERVER_TIME":100});</script>',
])
def test_unknown_time_and_business_data_remain_significant(html):
    assert fingerprint(html) != fingerprint(html.replace('100', '200'))


def test_captcha_image_changes_but_document_images_do_not_disappear():
    assert fingerprint('<img src="/captcha/flat?one">') == fingerprint('<img src="/captcha/flat?two">')
    assert fingerprint('<img src="/files/one.png">') != fingerprint('<img src="/files/two.png">')


def test_tilda_preloader_timer_is_ignored_only_on_record():
    html = '<div data-record-type="756" data-preloader-timeout="10">Адрес: Москва</div>'
    assert fingerprint(html) == fingerprint(html.replace('"10"', '"20"'))
    assert fingerprint(html) != fingerprint(html.replace('Москва', 'Тула'))
    assert fingerprint('<div data-preloader-timeout="10">') != fingerprint('<div data-preloader-timeout="20">')


def test_arbitrary_button_is_not_a_dynamic_tab():
    assert 'button' not in Config().dynamic_tab_selectors


class StaticPage:
    url = 'https://example.test/contacts'

    def __init__(self, html):
        self.html = html

    async def content(self):
        return self.html

    async def query_selector_all(self, selector):
        raise AssertionError('Заполненные/несвязанные панели не требуют клика')


@pytest.mark.parametrize('html', [
    '<button>Заказать</button><p>Адрес</p>',
    '<button role="tab" aria-controls="a">Москва</button><div id="a" hidden>Телефон 123</div>',
    '<nav><button role="tab" aria-controls="a">Каталог</button></nav><div id="a"></div>',
    '<button role="tab" aria-controls="a">Заявка</button><div id="a"><form><input></form></div>',
])
def test_filled_panels_and_forms_do_not_trigger_clicks(html):
    assert not needs_contact_interaction(html, Config().dynamic_tab_selectors)
    result = asyncio.run(collect_contact_panels(StaticPage(html), Config().dynamic_tab_selectors))
    from bs4 import BeautifulSoup
    assert BeautifulSoup(result, 'lxml').get_text() == BeautifulSoup(html, 'lxml').get_text()


def test_empty_related_panel_requires_interaction():
    assert needs_contact_interaction('<button role="tab" aria-controls="a">Москва</button><div id="a"></div>', Config().dynamic_tab_selectors)


def test_real_dealer_change_is_not_ignored():
    before = '<section hidden><h2>Дилеры</h2><p>Москва, тел. 123</p></section>'
    assert fingerprint(before) != fingerprint(before.replace('</section>', '<p>Адлер, тел. 456</p></section>'))


def test_tracking_iframe_is_noise_but_map_is_significant():
    html = '<p>Контакты</p>'
    assert fingerprint(html) == fingerprint(html + '<iframe src="https://mc.yandex.ru/metrika/match.html"></iframe>')
    assert fingerprint(html) != fingerprint(html + '<iframe src="https://maps.example.test/map"></iframe>')


def test_named_navigation_slots_ignore_order_but_keep_links():
    a = '<li data-nav-slot="service"><a href="/service">Сервис</a></li>'
    b = '<li data-nav-slot="catalog"><a href="/catalog">Каталог</a></li>'
    def menu(items):
        return '<header><ul>' + items + '</ul></header>'
    assert fingerprint(menu(a+b)) == fingerprint(menu(b+a))
    assert fingerprint(menu(a+b)) != fingerprint(menu((a+b).replace('/service', '/new-service')))


def test_tilda_proxy_geometry_and_map_references_are_noise():
    html = '<div data-artboard-recid="123" data-artboard-proxy-max-height="5" aria-describedby="random1">Телефон 123</div>'
    assert fingerprint(html) == fingerprint(html.replace('height="5"', 'height="6"').replace('random1', 'random2'))
    assert fingerprint(html) != fingerprint(html.replace('Телефон 123', 'Телефон 456'))


def test_empty_map_slot_is_noise_but_populated_slot_is_not():
    html = '<p>Москва</p>'
    assert fingerprint(html) == fingerprint(html + '<div data-view="SlotView.mimic"></div>')
    assert fingerprint(html) != fingerprint(html + '<div data-view="SlotView.mimic">Адлер</div>')


@pytest.mark.parametrize('page_type', ['company', 'distributor'])
def test_hidden_contact_panels_survive_markdown(page_type):
    from text_extractor import html_to_markdown
    html = '''<html><body><main><h1>Контакты</h1>
    <button role="tab" aria-controls="a">Москва</button>
    <div style="display:none"><div id="a" style="display:none">Дилер Сигма, телефон +74951234567</div></div>
    </main></body></html>'''
    merged = asyncio.run(collect_contact_panels(StaticPage(html), Config().dynamic_tab_selectors))
    markdown = html_to_markdown(merged, url='https://example.test/contacts', page_type=page_type)
    assert 'Сигма' in markdown and '+74951234567' in markdown


def test_known_tracking_bootstraps_only_are_ignored():
    import json
    from pathlib import Path
    scripts = json.loads((Path(__file__).parent / 'fixtures/ridan_tracking_bootstraps.json').read_text())
    for script in scripts:
        assert fingerprint('<p>Контакты</p>') == fingerprint('<p>Контакты</p><script>'+script+'</script>')
        assert fingerprint('<p>Контакты</p>') != fingerprint('<p>Контакты</p><script>'+script+';window.contacts={phone:123};</script>')


def test_tracking_noscript_does_not_remove_company_fallback():
    base = '<p>Контакты</p>'
    assert fingerprint(base) == fingerprint(base+'<noscript><img src="https://mc.yandex.ru/watch/123"></noscript>')
    assert fingerprint(base) != fingerprint(base+'<noscript>Телефон 123<img src="https://mc.yandex.ru/watch/123"></noscript>')


def test_jivo_ui_is_ignored_but_contacts_remain():
    base = '<p>Телефон 123</p>'
    assert fingerprint(base) == fingerprint(base+'<div><iframe name="jivo_container"></iframe></div><jdiv>Напишите нам</jdiv>')
    assert fingerprint(base) != fingerprint('<p>Телефон 456</p><jdiv>Напишите нам</jdiv>')
    assert fingerprint(base) == fingerprint(base+'<iframe src="//widgets-04.livetex.me/js/iframe.26.html"></iframe>')
    assert fingerprint(base) != fingerprint(base+'<iframe src="https://company.test/contacts"></iframe>')


def test_map_controls_and_empty_canvas_are_noise_but_dealer_balloon_is_not():
    base = '<script>window.map={lat:56.1,phone:123};</script><ymaps class="ymaps-2-1-79-balloon">Дилер Сигма</ymaps>'
    noise = '<ymaps class="ymaps-2-1-79-gotoymaps">Открыть маршрут</ymaps><ymaps class="ymaps-2-1-79-ground-pane"><canvas width="100"></canvas></ymaps>'
    assert fingerprint(base) == fingerprint(base+noise)
    assert fingerprint(base) != fingerprint(base.replace('lat:56.1','lat:55.2'))
    assert fingerprint(base) != fingerprint(base.replace('Сигма','Дельта'))


def test_empty_tilda_phone_mask_is_not_a_company_phone_change():
    before = '<form><div data-field-type="ph"><input name="Phone" type="tel" value=""></div></form>'
    after = '<form><div data-field-type="ph" data-init-mask="yes" data-input-ready="true"><span>+7</span><input name="Phone" type="tel" value=""></div></form>'
    assert fingerprint(before) == fingerprint(after)
    assert fingerprint(before) != fingerprint(after.replace('value=""', 'value="123"'))
    assert fingerprint('<a href="tel:123">Телефон</a>') != fingerprint('<a href="tel:456">Телефон</a>')


def test_animation_clock_does_not_hide_product_values():
    html = '<div data-animate-style="fadeinup" data-animate-start-time="100" data-product-inv="5">Ванна</div>'
    assert fingerprint(html) == fingerprint(html.replace('time="100"', 'time="200"'))
    assert fingerprint(html) != fingerprint(html.replace('inv="5"', 'inv="6"'))


def test_lazy_tilda_placeholder_keeps_actual_image_identity():
    original = 'https://static.tildacdn.com/stor123/image.jpg'
    before = '<img data-original="'+original+'" src="'+original.replace('/image.jpg','/-/empty/image.jpg')+'">'
    after = '<img data-original="'+original+'" src="'+original+'">'
    assert fingerprint(before) == fingerprint(after)
    assert fingerprint(after) != fingerprint(after.replace('image.jpg', 'changed.jpg'))


def test_empty_map_shell_is_noise_but_map_data_are_preserved():
    base = '<p>Адрес</p>'
    shell = '<ymaps class="ymaps-2-1-79-map"><ymaps class="ymaps-2-1-79-events-pane" unselectable="on"></ymaps><ymaps class="ymaps-2-1-79-copyrights-pane">Условия использования</ymaps></ymaps>'
    assert fingerprint(base) == fingerprint(base+shell)
    assert fingerprint(base) != fingerprint(base+'<ymaps data-coordinates="55,37"></ymaps>')
    assert fingerprint(base) != fingerprint(base+'<ymaps>Дилер Сигма, телефон 123</ymaps>')
    assert fingerprint(base) == fingerprint(base+'<ymaps><canvas width="320" height="320"></canvas></ymaps>')
    assert fingerprint(base) != fingerprint(base+'<ymaps><canvas data-coordinates="55,37"></canvas></ymaps>')
    assert fingerprint(base) == fingerprint(base+'<ymaps><ymaps class="islands_clusterMediumIcon">25</ymaps></ymaps>')
    assert fingerprint(base) != fingerprint(base+'<ymaps class="islands_clusterMediumIcon">Дилер 25</ymaps>')


def test_filter_disabled_state_is_noise_but_selected_region_is_not():
    field = '<input data-search-input-value="" name="regionId" value="41">'
    assert fingerprint(field) == fingerprint(field.replace('>', ' disabled>'))
    assert fingerprint(field) != fingerprint(field.replace('41', '42'))


def test_tilda_stock_label_deduplicates_only_explicit_zero_stock():
    base = '<div data-product-uid="123" data-product-inv="0">Ванна</div>'
    label = '<div class="js-store-prod-sold-out">Нет в наличии</div><a href="#popup:out-of-stock">Купить под заказ</a>'
    rendered = base.replace('</div>', label+'</div>')
    assert fingerprint(base) == fingerprint(rendered)
    assert fingerprint(base) != fingerprint(base.replace('inv="0"', 'inv="1"'))
    assert fingerprint(base) != fingerprint(rendered.replace('Нет в наличии', 'Доставка через 25 дней'))
    assert fingerprint('<p>Ванна</p>') != fingerprint('<p>Ванна</p>'+label)
