"""Тесты офлайн-метрик markdown M1–M8 (site_profiles/tools/md_metrics.py).

Синтетические фикстуры на каждую метрику (сеть и корпус не нужны) + один тест
по сохранённому корпусу (Тизол): проверяет форму отчёта по компании.
"""
import json

import pytest

from site_profiles.tools import md_metrics as MD

URL = 'https://md-metrics.test/catalog/p1'      # домена нет ни в профилях, ни в адаптерах


def _page(body_html: str) -> str:
    return f'<html><head><title>Тест</title></head><body>{body_html}</body></html>'


# ==================== M1 container_text_share ====================

def test_m1_крошечный_article_даёт_малую_долю():
    """Первое звено цепочки (article) почти пустое — доля от body мала (механика D273)."""
    html = _page('<article>Хлебные крошки</article>'
                 '<div>' + 'Описание товара с характеристиками. ' * 40 + '</div>')
    share, selector, body_len = MD.container_share(html, URL)
    assert selector == 'article'
    assert body_len > 1000
    assert share < 0.15


def test_m1_без_контейнеров_берётся_body():
    """Ни одного селектора цепочки — контейнер = body, доля 1.0."""
    share, selector, _ = MD.container_share(_page('<div><p>Текст товара</p></div>'), URL)
    assert selector == 'body'
    assert share == 1.0


# ==================== M2 spec_lines ====================

def test_m2_считает_ключ_значение_и_строки_таблицы():
    lines = ['# Плита',
             'Толщина: 50 мм',            # ключ: значение с числом
             'Цвет: белый',               # числа нет — не считается
             'Подробнее: https://site.ru/p/12',   # голая ссылка — не считается
             '| Параметр | Значение |',   # шапка без числа — не считается
             '| --- | --- |',             # разделитель — не считается
             '| Ширина | 1200 мм |',
             '| Длина | 600 мм |']
    assert MD._spec_lines(lines) == 3


# ==================== M3 table_retention ====================

def test_m3_таблица_из_html_доходит_до_markdown():
    html = _page('<main><h1>Плита</h1><table><tr><th>Параметр</th><th>Значение</th></tr>'
                 '<tr><td>Толщина</td><td>50 мм</td></tr></table></main>')
    record, _ = MD.page_metrics(html, URL)
    assert record['has_table_html'] and record['has_table_md']


def test_m3_таблица_вне_контейнера_теряется():
    """Таблица лежит вне выбранного контейнера — в markdown её нет (потеря M3)."""
    html = _page('<main><h1>Плита</h1><p>Описание</p></main>'
                 '<table><tr><td>Толщина</td><td>50 мм</td></tr></table>')
    record, _ = MD.page_metrics(html, URL)
    assert record['has_table_html'] and not record['has_table_md']


# ==================== M4 short_md_rate ====================

def test_m4_короткий_markdown():
    short, _ = MD.page_metrics(_page('<main>Плита</main>'), URL)
    long_page, _ = MD.page_metrics(_page('<main>' + 'Описание товара. ' * 40 + '</main>'), URL)
    assert short['short_md'] and not long_page['short_md']


# ==================== M5 tab_coverage ====================

def test_m5_скрытая_панель_вкладки_не_дошла_до_markdown():
    html = _page('<main><h1>Плита</h1>'
                 '<div class="tab-pane" style="display:none">'
                 '<p>Характеристики из неактивной вкладки</p></div></main>')
    record, _ = MD.page_metrics(html, URL)
    assert record['tab_panels'] == 1
    assert record['tab_panels_covered'] == 0


def test_m5_видимая_панель_вкладки_дошла():
    html = _page('<main><h1>Плита</h1>'
                 '<div class="tab-pane"><p>Характеристики из активной вкладки</p></div></main>')
    record, _ = MD.page_metrics(html, URL)
    assert record['tab_panels'] == 1
    assert record['tab_panels_covered'] == 1


# ==================== M6 glued_cell_rate ====================

def test_m6_склейка_в_ячейках():
    lines = ['| Параметр | Значение |', '| --- | --- |',
             '| ТолщинаПлиты | 50 мм |',      # склейка [а-яё][А-ЯЁ]
             '| Ширина | 1200миллиметров |',  # склейка \\d[А-Яа-я]{3,}
             '| Длина | 600 мм |']
    total, glued = MD._cell_stats(lines)
    assert (total, glued) == (8, 2)          # шапка тоже ячейки, разделитель — нет


# ==================== M7 footnote_retention ====================

def test_m7_сноска_рядом_с_таблицей_сохранена():
    lines = ['| Параметр | Значение |', '| --- | --- |', '| Толщина* | 50 мм |',
             '', '\\* при температуре 20 C']
    assert MD._footnote_stats(lines) == (1, 1)


def test_m7_сноска_потеряна():
    lines = ['| Параметр | Значение |', '| --- | --- |', '| Толщина* | 50 мм |',
             '', '## Другой раздел', '', 'Текст']
    assert MD._footnote_stats(lines) == (1, 0)


def test_m7_жирная_разметка_не_маркер():
    """`**Итого**` — это bold markdownify, а не сноска: таблица не считается помеченной."""
    lines = ['| Параметр | Значение |', '| --- | --- |', '| **Итого** | 50 мм |']
    assert MD._footnote_stats(lines) == (0, 0)


# ==================== M8 boilerplate_share ====================

def test_m8_одинаковые_страницы_целиком_боилерплейт():
    page = ['Каталог продукции', 'Контакты завода', 'Доставка по России']
    assert MD._boilerplate_share([page, page, page]) == 1.0


def test_m8_уникальные_страницы_без_боилерплейта():
    pages = [['Плита ППЖ-200', 'Толщина 50 мм'],
             ['Мат прошивной МП-75', 'Толщина 60 мм'],
             ['Цилиндр Ц-100', 'Толщина 70 мм']]
    assert MD._boilerplate_share(pages) == 0.0


def test_m8_меньше_трёх_страниц_не_считается():
    assert MD._boilerplate_share([['Каталог'], ['Каталог']]) is None


# ==================== агрегация и вердикты ====================

def test_вердикты_по_порогам():
    assert MD._verdict('M1', 0.4) == 'ok'
    assert MD._verdict('M1', 0.2) == 'warn'
    assert MD._verdict('M1', 0.14) == 'red'
    assert MD._verdict('M2', 2) == 'red'          # красный по порогу «<=2»
    assert MD._verdict('M4', 0.4) == 'red'        # обратное направление
    assert MD._verdict('M4', 0.05) == 'ok'
    assert MD._verdict('M8', None) == 'n/a'


def test_aggregate_медианы_и_доли():
    details = [
        {'container_share': 0.5, 'spec_lines': 10, 'has_table_html': True, 'has_table_md': True,
         'short_md': False, 'tab_panels': 2, 'tab_panels_covered': 2, 'cells_total': 10,
         'cells_glued': 0, 'tables_marked': 1, 'tables_footnoted': 1},
        {'container_share': 0.1, 'spec_lines': 2, 'has_table_html': True, 'has_table_md': False,
         'short_md': True, 'tab_panels': 2, 'tab_panels_covered': 0, 'cells_total': 10,
         'cells_glued': 2, 'tables_marked': 1, 'tables_footnoted': 0},
    ]
    metrics, verdict = MD.aggregate(details, [])
    assert metrics['M1'] == 0.3 and metrics['M2'] == 6
    assert metrics['M3'] == 0.5 and metrics['M4'] == 0.5
    assert metrics['M5'] == 0.5 and metrics['M6'] == 0.1
    assert metrics['M7'] == 0.5 and metrics['M8'] is None
    assert verdict['M3'] == 'red' and verdict['M8'] == 'n/a'


# ==================== индекс корпуса ====================

def test_индекс_корпуса_домен_к_папкам(tmp_path):
    pages = tmp_path / 'Компания_ООО' / 'Product_pages'
    pages.mkdir(parents=True)
    (pages / 'page.json').write_text(json.dumps({'url': 'https://www.Example.RU/catalog/p1'}),
                                     encoding='utf-8')
    index_path = tmp_path / 'index.json'
    index = MD.load_index(tmp_path, index_path)
    assert index['domains']['example.ru'] == ['Компания_ООО']
    assert index_path.exists()
    assert MD.load_index(tmp_path, index_path)['built_at'] == index['built_at']   # взят кэш


# ==================== корпус ====================

def test_метрики_компании_по_корпусу(corpus_dir):
    """Тизол: 3 товарные страницы из сохранённого корпуса — форма отчёта и домен."""
    company_dir = corpus_dir / 'Тизол_ОАО'
    if not (company_dir / 'Product_pages').is_dir():
        pytest.skip('в корпусе нет товарных страниц Тизола')
    result = MD.company_metrics(company_dir, 3)
    assert result['domain'] == 'tizol.com'
    assert result['pages_used'] == 3
    assert result['errors'] == []
    assert set(result['metrics']) == set(MD.THRESHOLDS)
    assert set(result['verdict']) == set(MD.THRESHOLDS)
    assert all(d['md_len'] > 0 and d['url'] for d in result['details'])
