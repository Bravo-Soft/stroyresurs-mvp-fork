"""Обновление: технический шум не сбрасывает AI, полезные изменения сохраняются."""
import asyncio
import hashlib
from pathlib import Path

import pytest
from temp_storage_manager import TempStorageManager

BASE = '''<html><head><title>Контакты</title></head><body>
<h1>Завод</h1><p>Москва, ул. Первая, 1</p><a href="tel:+74951234567">Телефон</a>
<a href="mailto:a@test.ru">Почта</a><a href="/files/a.pdf">Документ</a>
<div hidden>Скрытая вкладка: 10 мм</div><img src="/a.jpg" alt="Схема">
<script type="application/ld+json">{"telephone":"123","name":"Завод"}</script>
<script>window.__NUXT__={width:10};</script>
</body></html>'''


@pytest.mark.parametrize('extra1,extra2', [
    ('<!-- request 123 -->', '<!-- request 456 -->'),
    ('<style>.x{color:red}</style>', '<style>.x{color:blue}</style>'),
    ('<script src="/app.js?v=1"></script>', '<script src="/app.js?v=2"></script>'),
    ('<input type="hidden" name="csrf_token" value="111">', '<input type="hidden" name="csrf_token" value="222">'),
    ('<input type="hidden" name="sessid" value="111">', '<input type="hidden" name="sessid" value="222">'),
    ('<meta name="csrf-token" content="111">', '<meta name="csrf-token" content="222">'),
    ('<div id="random1" class="active" style="left:1px">Адрес</div>', '<div id="random2" class="ready" style="left:2px">Адрес</div>'),
    ('<script nonce="one">window.contacts={phone:123}</script>', '<script nonce="two">window.contacts={phone:123}</script>'),
])
def test_noise_preserves_storage_and_ai(tmp_path, extra1, extra2):
    async def run():
        manager = TempStorageManager(str(tmp_path/'html'), max_size_mb=10)
        url = 'https://factory.test/contacts/'
        a = BASE.replace('</body>', extra1+'</body>')
        b = BASE.replace('</body>', extra2+'</body>')
        path, new, meta = await manager.save_cleaned_html(url, url, a, 'Завод', category='contacts')
        assert new
        meta['checkpoints']['ai_processed'] = True
        await manager._write_metadata(Path(path).with_suffix('.json'), meta)
        manager.crawl_run_id = 'second'
        result, new, meta = await manager.save_cleaned_html(url, url, b, 'Завод', category='contacts')
        assert not new
        assert result == path and meta['checkpoints']['ai_processed']
        assert meta['crawl_run_id'] == 'second'
        assert Path(path).read_text() == a
        assert meta['content_hash'] == hashlib.sha256(a.encode()).hexdigest()
    asyncio.run(run())


@pytest.mark.parametrize('old,new', [
    ('Первая, 1', 'Первая, 2'), ('+74951234567', '+74951234568'),
    ('a@test.ru', 'b@test.ru'), ('/files/a.pdf', '/files/b.pdf'),
    ('10 мм', '11 мм'), ('/a.jpg', '/b.jpg'), ('"telephone":"123"', '"telephone":"124"'),
    ('width:10', 'width:20'), ('Завод</h1>', 'Фабрика</h1>'),
])
@pytest.mark.parametrize('category', ['contacts', 'product'])
def test_business_changes_create_version(tmp_path, old, new, category):
    async def run():
        manager = TempStorageManager(str(tmp_path/'html'), max_size_mb=10)
        url = 'https://factory.test/page'
        first, _, _ = await manager.save_cleaned_html(url, url, BASE, 'Завод', category=category)
        second, changed, meta = await manager.save_cleaned_html(url, url, BASE.replace(old,new), 'Завод', category=category)
        assert changed and second != first
        assert Path(first).exists() and not meta['checkpoints']['ai_processed']
        assert len(meta['change_history']) == 1
    asyncio.run(run())


def test_legacy_raw_hash_migrates_without_false_change(tmp_path):
    async def run():
        manager = TempStorageManager(str(tmp_path/'html'), max_size_mb=10)
        url = 'https://factory.test/contacts'
        first, _, meta = await manager.save_cleaned_html(url,url,BASE+'<!-- old -->','Завод',category='contacts')
        meta.pop('comparison_hash',None)
        meta.pop('comparison_version',None)
        meta['checkpoints']['ai_processed'] = True
        await manager._write_metadata(Path(first).with_suffix('.json'),meta)
        manager = TempStorageManager(str(tmp_path/'html'), max_size_mb=10)
        second, changed, meta = await manager.save_cleaned_html(url,url,BASE+'<!-- new -->','Завод',category='contacts')
        assert not changed and second == first and meta['checkpoints']['ai_processed']
        assert meta['comparison_version'] and meta['comparison_hash']
    asyncio.run(run())


def test_json_order_and_whitespace_are_not_changes():
    from html_comparison import html_comparison_hash as fingerprint
    assert fingerprint(BASE)==fingerprint(BASE.replace('{"telephone":"123","name":"Завод"}', '{ "name": "Завод", "telephone": "123" }'))
    assert fingerprint('<p>Москва   улица</p>')==fingerprint('<p>Москва\nулица</p>')


def test_table_hierarchy_and_significant_attributes_are_changes():
    from html_comparison import html_comparison_hash as fingerprint
    assert fingerprint('<div><b>А</b>Б</div>')!=fingerprint('<div><b>АБ</b></div>')
    for attribute in ['href','src','data-product','itemprop','content','colspan','rowspan','onclick']:
        assert fingerprint(f'<div {attribute}="1">Значение</div>')!=fingerprint(f'<div {attribute}="2">Значение</div>')


def test_dates_and_map_coordinates_are_not_noise():
    from html_comparison import html_comparison_hash as fingerprint
    for a,b in [('2026-11-02','2026-11-03'), ('<script>map([55,37])</script>','<script>map([56,37])</script>'),
                ('<iframe src="https://map.test/?ll=55,37"></iframe>','<iframe src="https://map.test/?ll=56,37"></iframe>')]:
        assert fingerprint(a)!=fingerprint(b)


@pytest.mark.parametrize('left,right', [
    ('<meta charset="UTF-8">','<meta charset="utf-8">'),
    ('<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">',
     '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'),
])
def test_decoded_text_does_not_depend_on_encoding_declaration(left,right):
    from html_comparison import html_comparison_hash as fingerprint
    assert fingerprint('<html><head>'+left+'</head><body>Завод</body></html>')==fingerprint('<html><head>'+right+'</head><body>Завод</body></html>')


@pytest.mark.parametrize('template',[
    '<script>BX.message({"bitrix_sessid":"TOKEN","phone":"123"});</script>',
    '<script type="application/json">{"csrfToken":"TOKEN","name":"Завод"}</script>',
])
def test_inline_security_token_is_not_business_data(template):
    from html_comparison import html_comparison_hash as fingerprint
    assert fingerprint(template.replace('TOKEN','abc'))==fingerprint(template.replace('TOKEN','def'))
    assert fingerprint(template)!=fingerprint(template.replace('123','124').replace('Завод','Фабрика'))
