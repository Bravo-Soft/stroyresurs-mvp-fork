"""Приоритет YAML и сохранение очистки при смене пути извлечения."""
from types import SimpleNamespace

import pytest

import text_extractor as te
import universal_extractor as ue
from site_profiles import profile_from_dict
from site_profiles.profile_metrics import RunMetricsCollector

URL = 'https://profile-test.example/product'
TEXT = 'Описание изделия и технические характеристики. ' * 12
HTML = ('<html><body><article><h1>Чужой товар</h1><p>' + TEXT * 2 + '</p></article>'
        '<section id="selected"><h2>Выбранный товар</h2><p>Масса 12 кг</p>'
        '<table><tr><th>Размер</th><td>150 мм</td></tr></table>'
        '<p class="remove">ЗАПРЕЩЕННЫЙ БЛОК 999</p></section></body></html>')


@pytest.fixture
def configure(monkeypatch):
    def setup(markdown=None, **extract):
        profile, errors = profile_from_dict({'domain':'profile-test.example',
                                           'extract':dict(markdown=markdown or {}, **extract)})
        assert not errors
        monkeypatch.setattr(te, '_resolve_profile', lambda url: profile)
        return profile
    te.set_census_sink(None)
    yield setup
    te.set_census_sink(None)


@pytest.mark.parametrize('page_type,key', [('', 'product_container'), ('company','company_container'),
                                          ('distributor','distributor_container')])
def test_explicit_container_wins_even_if_short(configure, page_type, key):
    configure({key:'#selected', 'remove_selectors':['.remove']})
    md = te.html_to_markdown(HTML, URL, page_type)
    assert 'Выбранный товар' in md and '12 кг' in md and '150 мм' in md
    assert 'Чужой товар' not in md and 'ЗАПРЕЩЕННЫЙ' not in md


@pytest.mark.parametrize('css_class', ['modal', 'overlay'])
def test_explicit_container_survives_product_noise_heuristics(configure, css_class):
    configure({'product_container':'.'+css_class, 'remove_selectors':['.remove']})
    md = te.html_to_markdown(HTML.replace('id="selected"',f'class="{css_class}"'), URL)
    assert '12 кг' in md and 'Чужой товар' not in md


def test_container_precedes_custom_adapter(configure, monkeypatch):
    configure({'product_container':'#selected'}, tier='custom', custom_module='test')
    monkeypatch.setattr(te, '_get_adapter_by_module', lambda name: SimpleNamespace(extract=lambda *a: 'Чужой адаптер'))
    assert '12 кг' in te.html_to_markdown(HTML, URL)


def test_profile_cleaning_survives_universal(configure):
    configure({'remove_selectors':['.remove']})
    html = '<html><body><article><h1>Товар</h1><p>'+TEXT+'</p><div class="remove">ЗАПРЕЩЕННЫЙ БЛОК 999</div></article></body></html>'
    assert 'ЗАПРЕЩЕННЫЙ' not in te.html_to_markdown(html, URL)


def test_adapter_cleaning_survives_fallback(configure, monkeypatch):
    configure(tier='custom', custom_module='test')
    def clean(soup):
        for tag in soup.select('.remove'): tag.decompose()
    monkeypatch.setattr(te, '_get_adapter_by_module', lambda name: SimpleNamespace(clean=clean, extract=lambda *a:None))
    html = '<article><h1>Товар</h1><p>'+TEXT+'</p><div class="remove">ЗАПРЕЩЕННЫЙ БЛОК 999</div></article>'
    assert 'ЗАПРЕЩЕННЫЙ' not in te.html_to_markdown(html, URL)


def test_raw_adapter_receives_profile_filtered_html_and_keeps_scripts(configure, monkeypatch):
    configure({'remove_selectors':['.remove']}, tier='custom', custom_module='test')
    received=[]
    def extract(html, url):
        received.append(html)
        return 'Товар 12 кг'
    monkeypatch.setattr(te, '_get_adapter_by_module', lambda name: SimpleNamespace(NEEDS_RAW_HTML=True, extract=extract))
    te.html_to_markdown(HTML.replace('</body>','<script>visible_widget_data</script></body>'), URL)
    assert 'ЗАПРЕЩЕННЫЙ' not in received[0]
    assert 'visible_widget_data' in received[0]


def test_keep_hidden_tabs_survives_universal(configure):
    configure(keep_hidden_tabs=True)
    html='<article><h1>Товар</h1><p>'+TEXT+'</p><div style="display:none">Публичная вкладка: масса 123 кг</div></article>'
    assert '123 кг' in te.html_to_markdown(html, URL)


@pytest.mark.parametrize('page_type,key', [('', 'product_container'), ('company','company_container'), ('distributor','distributor_container')])
def test_missing_container_records_fallback(configure, page_type, key, caplog):
    configure({key:'#missing', 'remove_selectors':['.remove']})
    metrics=RunMetricsCollector('profile-test.example');te.set_census_sink(metrics.sink)
    with caplog.at_level('INFO'):
        md=te.html_to_markdown(HTML, URL, page_type)
    events=metrics.to_dict().get('markdown_fallbacks', [])
    assert any(e['reason']=='profile_container_missing' and e['url']==URL and e['page_type']==page_type for e in events)
    assert 'profile_container_missing' in caplog.text
    assert 'ЗАПРЕЩЕННЫЙ' not in md
    assert sum(metrics.extraction_paths.values())==1


@pytest.mark.parametrize('result,reason', [(None,'universal_empty'), ('коротко','universal_too_short')])
def test_universal_rejection_has_reason(configure, monkeypatch, result, reason):
    configure()
    monkeypatch.setattr(ue,'extract_product_markdown',lambda *a,**kw:result)
    m=RunMetricsCollector('profile-test.example');te.set_census_sink(m.sink)
    md=te.html_to_markdown('<article><h1>Товар</h1><p>'+TEXT+'</p></article>', URL)
    assert TEXT.strip() in md
    assert any(e['reason']==reason for e in m.to_dict().get('markdown_fallbacks', []))


def test_universal_exception_has_reason(configure, monkeypatch):
    configure()
    def fail(*a, **kw): raise ValueError('Ошибка теста')
    monkeypatch.setattr(ue,'extract_product_markdown',fail)
    m=RunMetricsCollector('profile-test.example');te.set_census_sink(m.sink)
    assert '12 кг' in te.html_to_markdown('<article><p>12 кг</p></article>', URL)
    assert any(e['reason']=='universal_error' for e in m.to_dict().get('markdown_fallbacks', []))


def test_failing_metrics_does_not_break_output(configure, monkeypatch):
    configure({'product_container':'#missing'})
    def fail(*a, **kw): raise RuntimeError('sink unavailable')
    te.set_census_sink(fail)
    assert te.html_to_markdown(HTML,URL)


def test_adapter_product_exception_is_not_silently_hidden(configure, monkeypatch):
    configure(tier='custom', custom_module='test')
    def fail(*args): raise ValueError('Ошибка адаптера')
    monkeypatch.setattr(te,'_get_adapter_by_module',lambda name:SimpleNamespace(extract=fail))
    with pytest.raises(ValueError,match='Ошибка адаптера'): te.html_to_markdown(HTML,URL)


def test_scoped_hidden_descendants_and_scripts_are_not_visible(configure):
    configure({'product_container':'#selected'})
    html='<div id="selected">12 кг<div style="display:none"><span style="display:none">СЕКРЕТ</span></div><script>СЕКРЕТ</script></div>'
    md=te.html_to_markdown(html, URL)
    assert '12 кг' in md and 'СЕКРЕТ' not in md


def test_scoped_max_len_is_enforced(configure):
    configure({'product_container':'#selected','max_len':30})
    assert len(te.html_to_markdown(HTML,URL)) <= 30


@pytest.mark.parametrize('page_type', ['company','distributor'])
def test_special_adapter_failure_is_recorded(configure, monkeypatch, page_type):
    configure(tier='custom',custom_module='test')
    def fail(*a):raise ValueError('test adapter error')
    monkeypatch.setattr(te,'_get_adapter_by_module',lambda name:SimpleNamespace(**{'extract_'+page_type:fail}))
    m=RunMetricsCollector('profile-test.example');te.set_census_sink(m.sink)
    md=te.html_to_markdown('<main>Контакты +7 812 123-45-67</main>',URL,page_type)
    assert 'Контакты' in md
    assert any(e['reason']=='adapter_special_error' for e in m.to_dict()['markdown_fallbacks'])


def test_adapter_empty_record_is_serializable(configure, monkeypatch):
    import json
    configure(tier='custom',custom_module='test')
    monkeypatch.setattr(te,'_get_adapter_by_module',lambda name:SimpleNamespace(extract=lambda *a:None))
    m=RunMetricsCollector('profile-test.example');te.set_census_sink(m.sink)
    te.html_to_markdown(HTML,URL)
    data=json.loads(json.dumps(m.to_dict()))
    assert any(e['reason']=='adapter_empty' and e['source']=='adapter' for e in data['markdown_fallbacks'])
    assert sum(m.extraction_paths.values()) == 1


def test_adapter_clean_runs_once_when_falling_back(configure, monkeypatch):
    configure(tier='custom',custom_module='test',clean_in_universal=True)
    calls=[]
    def clean(soup): calls.append(1)
    monkeypatch.setattr(te,'_get_adapter_by_module',lambda name:SimpleNamespace(clean=clean,extract=lambda *a:None))
    te.html_to_markdown(HTML,URL)
    assert calls==[1]
