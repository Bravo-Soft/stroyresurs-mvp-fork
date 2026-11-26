"""Эталонные пары заданы вручную; конвертер не создаёт ожидания проверяющего."""
import json
from pathlib import Path

import pytest
from site_profiles.markdown_validation import check_markdown, canonical, inventory

HTML='''<html><body><nav><a href="/">Главная страница</a></nav><main>
<h1>Насос А</h1><p>Расход: 12,5 м³/ч; давление: 2 МПа.</p>
<table><caption>Параметры моделей</caption><tr><th>Модель</th><th>Масса, кг</th><th>Мощность, кВт</th></tr>
<tr><td>А1</td><td>10</td><td>3</td></tr><tr><td>Б2</td><td>20</td><td>5</td></tr></table>
<p class="note">* При температуре 20 °C.</p><button>Добавить в корзину</button></main></body></html>'''
MD='''# Насос А

Расход: 12.5 м^3/ч; давление: 2 МПа.

### Параметры моделей

| Модель | Масса, кг | Мощность, кВт |
| --- | --- | --- |
| А1 | 10 | 3 |
| Б2 | 20 | 5 |

\\* При температуре 20 C.
'''


def check(md=MD,html=HTML,**kw):return check_markdown(html,md,source_selectors=['main'],**kw)
def codes(report):return {f['code'] for f in report['findings']}


def test_manual_golden_pair():
    r=check();assert r['status']=='pass',r
    assert r['counts']['source_tables']==1
    assert r['model_input']['status']=='uncertain'


@pytest.mark.parametrize('old,new,code',[
 ('12.5','13.5','number_mismatch'),('12.5','125','number_mismatch'),
 ('2 МПа','2 кПа','unit_mismatch'),('м^3/ч','м^2/ч','unit_mismatch'),
 ('Расход: 12.5 м^3/ч; давление: 2 МПа.','', 'number_mismatch'),
 ('\\* При температуре 20 C.','', 'note_missing_or_changed'),
 ('| А1 | 10 | 3 |','| А1 | 20 | 3 |','table_row_relationship_lost'),
 ('| Б2 | 20 | 5 |','', 'table_row_relationship_lost'),
 ('Масса, кг','Масса, г','table_headers_or_layout_changed'),
])
def test_lost_and_changed_facts(old,new,code):
    r=check(MD.replace(old,new));assert r['status']=='fail' and code in codes(r),r


def test_same_global_numbers_swapped_between_models_fail():
    changed=MD.replace('| А1 | 10 | 3 |','| А1 | 20 | 5 |').replace('| Б2 | 20 | 5 |','| Б2 | 10 | 3 |')
    r=check(changed)
    assert 'table_row_relationship_lost' in codes(r)
    assert len([f for f in r['findings'] if f['code']=='table_row_relationship_lost'])==2


def test_numbers_copied_outside_table_do_not_mask_loss():
    changed=MD.replace('| А1 | 10 | 3 |','')+'\nВсе значения: А1 10 3\n'
    assert 'table_row_relationship_lost' in codes(check(changed))


def test_column_reordering_with_values_is_allowed():
    changed=MD.replace('| Модель | Масса, кг | Мощность, кВт |','| Модель | Мощность, кВт | Масса, кг |').replace('| А1 | 10 | 3 |','| А1 | 3 | 10 |').replace('| Б2 | 20 | 5 |','| Б2 | 5 | 20 |')
    assert check(changed)['status']=='pass'


def test_row_reordering_is_allowed():
    changed=MD.replace('| А1 | 10 | 3 |\n| Б2 | 20 | 5 |','| Б2 | 20 | 5 |\n| А1 | 10 | 3 |')
    assert check(changed)['status']=='pass'


def test_table_dropped_even_when_all_values_in_text():
    md='# Насос А\nРасход: 12.5 м^3/ч; давление: 2 МПа.\nМодель Масса, кг Мощность, кВт А1 10 3 Б2 20 5'
    assert 'table_missing' in codes(check(md))


def test_caption_and_notes_remain_attached():
    md=MD.replace('\\* При температуре 20 C.','### Другие условия\n\\* При температуре 20 C.')
    assert check(md)['status']=='fail'


def test_ui_and_hidden_leaks():
    html=HTML.replace('</main>','<div hidden>Скрытый служебный код 9988</div></main>')
    r=check(MD+'\nДобавить в корзину\nГлавная страница\nСкрытый служебный код 9988\n',html)
    assert {'interface_leak','hidden_content_leak'}<=codes(r)


def test_ui_label_also_used_in_content_is_not_automatically_noise():
    html='<main><h1>Заказать</h1><p>Заказать</p><button>Заказать</button></main>'
    assert check('# Заказать\nЗаказать',html)['status']=='pass'


def test_unlinked_hidden_panel_is_uncertain_not_required_source():
    html='<main><h1>Насос</h1><div role="tabpanel" hidden><p>Масса 90 кг</p></div></main>'
    r=check('# Насос',html)
    assert r['status']=='uncertain' and not r['findings']


def test_public_hidden_tab_is_required():
    html='<main><h1>Насос</h1><button aria-controls="spec">Характеристики</button><section id="spec" role="tabpanel" hidden><p>Масса: 90 кг</p></section></main>'
    assert check('# Насос',html)['status']=='fail'
    assert check('# Насос\nМасса: 90 кг',html)['status']=='pass'


def test_inferred_scope_never_silently_passes():
    r=check_markdown('<main><p>Размер 10 мм</p></main>','Размер 10 мм')
    assert r['status']=='uncertain' and 'source_scope_inferred' in r['uncertainties']


def test_missing_explicit_source_is_uncertain():
    r=check_markdown('<main>Размер 10 мм</main>','Размер 10 мм',source_selectors=['#missing'])
    assert r['status']=='uncertain'


def test_required_source_outside_extractor_container_checked():
    html='<main><h1>Насос</h1></main><section id="spec"><p>Масса: 50 кг</p></section>'
    r=check_markdown(html,'# Насос',source_selectors=['main','#spec'])
    assert r['status']=='fail'


def test_explicit_reference_exclusion_has_reason():
    html='<main><h1>Насос</h1><p class="offer">Акция только сегодня</p></main>'
    r=check('# Насос',html,exclusions=[{'selector':'.offer','reason':'Промо-блок, не описание товара'}])
    assert r['status']=='pass' and len(r['source']['excluded'])==1
    with pytest.raises(ValueError):check('# Насос',html,exclusions=[{'selector':'.offer','reason':''}])


def test_actual_model_input_truncation_detected():
    r=check(model_input=MD[:MD.index('| Б2')])
    assert r['status']=='fail'
    assert 'table_row_relationship_lost' in codes(r['model_input'])


@pytest.mark.parametrize('source,target',[
 ('12,5 м³/ч','12.5 м^3/ч'),('1\u00a0234 кг','1234 кг'),('−5 °С','-5 C'),
 ('10×20 мм','10х20 мм'),('2 МПа','2МПа'),('250 кг/м3','250 кг/м^3'),
])
def test_equivalent_numeric_spelling(source,target):
    assert canonical(source)==canonical(target)


@pytest.mark.parametrize('source,target',[('−5 C','5 C'),('0,5 мм','5 мм'),('2 МПа','2 кПа'),('м³','м²'),('10±2 мм','10 мм')])
def test_important_numeric_distinctions(source,target):
    assert canonical(source)!=canonical(target)


def test_rowspan_and_flattened_multilevel_headers():
    html='<main><h1>Насос</h1><table><tr><th rowspan="2">Модель</th><th colspan="2">Двигатель</th></tr><tr><th>Мощность, кВт</th><th>Масса, кг</th></tr><tr><td>A</td><td>2</td><td>3</td></tr></table></main>'
    md='# Насос\n| Модель | Двигатель / Мощность, кВт | Двигатель / Масса, кг |\n| --- | --- | --- |\n| A | 2 | 3 |'
    assert check(md,html)['status']=='pass'
    assert check(md.replace('| A | 2 | 3 |','| A | 3 | 2 |'),html)['status']=='fail'


def test_rowspan_data_and_duplicate_values():
    html='<main><table><tr><th>Модель</th><th>Режим</th><th>Вес</th></tr><tr><td rowspan="2">A</td><td>1</td><td>10</td></tr><tr><td>2</td><td>10</td></tr></table></main>'
    md='| Модель | Режим | Вес |\n| --- | --- | --- |\n| A | 1 | 10 |\n| A | 2 | 10 |'
    assert check(md,html)['status']=='pass'
    assert check(md.replace('| A | 2 | 10 |',''),html)['status']=='fail'


def test_invalid_nested_table_is_uncertain():
    r=check('1','<main><table><tr><td><table><tr><td>1</td></tr></table></td></tr></table></main>')
    assert r['status']=='uncertain'


def test_escaped_pipe_and_line_break_in_cells():
    html='<main><table><tr><th>Марка</th><th>Размер</th></tr><tr><td>A | B</td><td>10 мм<br>20 мм</td></tr></table></main>'
    md='| Марка | Размер |\n| --- | --- |\n| A \\| B | 10 мм<br>20 мм |'
    assert check(md,html)['status']=='pass'


def test_table_footnote_moved_to_paragraph_allowed():
    html='<main><table><caption>Насос</caption><tr><th>Модель</th><th>Вес</th></tr><tr><td>A*</td><td>10 кг</td></tr><tr><td colspan="2">* При 20 C</td></tr></table></main>'
    md='### Насос\n| Модель | Вес |\n| --- | --- |\n| A* | 10 кг |\n\n\\* При 20 C'
    assert check(md,html)['status']=='pass'
    assert check(md.replace('A*','A'),html)['status']=='fail'


def test_separate_tables_do_not_cover_each_other():
    html='<main><table><caption>Режим А</caption><tr><th>Вес</th></tr><tr><td>10 кг</td></tr></table><table><caption>Режим Б</caption><tr><th>Вес</th></tr><tr><td>10 кг</td></tr></table></main>'
    md='### Режим Б\n| Вес |\n| --- |\n| 10 кг |'
    assert 'table_missing' in codes(check(md,html))


def test_public_panel_does_not_expose_hidden_service_descendants():
    html='<main><button aria-controls="panel">ТХ</button><div id="panel" hidden><p>Вес: 12 кг</p><p hidden>Сервисный код 987</p></div></main>'
    r=check('Вес: 12 кг',html)
    assert r['status']=='pass',r
    assert check('Вес: 12 кг\nСервисный код 987',html)['status']=='fail'


def test_visual_graph_cannot_be_declared_checked():
    r=check('# Насос','<main><h1>Насос</h1><canvas></canvas></main>')
    assert r['status']=='uncertain' and 'visual_content_unchecked' in r['uncertainties']


def test_table_cell_diagnostics_include_column_and_value():
    r=check(MD.replace('| А1 | 10 | 3 |','| А1 | 100 | 3 |'))
    issue=next(f for f in r['findings'] if f['code']=='table_row_relationship_lost')
    assert issue['cells'][0]==dict(column=2,header='Масса, кг',expected='10',actual='100',change='number_changed')


def make_manifest(tmp_path,actual=True):
    (tmp_path/'source.html').write_text(HTML);(tmp_path/'md.txt').write_text(MD)
    pages=[]
    for url,split in [('https://test.example/a','control'),('https://test.example/b','holdout')]:
        page=dict(url=url,html='source.html',markdown='md.txt',split=split,
                  reference=dict(selectors=['main'],origin='Ручной независимый эталон теста'))
        if actual: page['model_input']='md.txt'
        pages.append(page)
    path=tmp_path/'manifest.json';path.write_text(json.dumps(dict(version=1,pages=pages)))
    return path


def test_batch_saved_inputs_and_reports(tmp_path):
    from site_profiles.check_markdown import run
    manifest=make_manifest(tmp_path);out=tmp_path/'out'
    r=run(manifest,out)
    assert r['status']=='pass' and r['counts']=={'pass':2}
    assert (out/'pages.tsv').exists() and (out/'0001/model_input.txt').read_text()==MD
    assert (tmp_path/'source.html').read_text()==HTML
    with pytest.raises(ValueError):run(manifest,out)


def test_batch_missing_capture_uncertain_and_simulated_truncation(tmp_path):
    from site_profiles.check_markdown import run
    manifest=make_manifest(tmp_path,actual=False)
    r=run(manifest,tmp_path/'out');assert r['status']=='uncertain'
    r=run(manifest,tmp_path/'short',model_char_limit=40);assert r['status']=='fail'
    detail=json.loads((tmp_path/'short/0000/result.json').read_text())
    assert detail['model_input_preview']['kind']=='simulation_not_capture'


def test_batch_generation_does_not_hide_profile_removal(tmp_path,monkeypatch):
    from site_profiles.check_markdown import run
    import socket
    def forbid(*a,**kw):raise AssertionError('Сеть запрещена')
    monkeypatch.setattr(socket.socket,'connect',forbid)
    manifest=make_manifest(tmp_path);data=json.loads(manifest.read_text())
    for p in data['pages']:p.pop('markdown')
    manifest.write_text(json.dumps(data))
    profiles=tmp_path/'profiles';profiles.mkdir()
    (profiles/'test.example.yaml').write_text('domain: test.example\nextract:\n  markdown:\n    product_container: main\n    remove_selectors: [table]\n')
    r=run(manifest,tmp_path/'out',profiles)
    assert r['status']=='fail' and all(p['status']=='fail' for p in r['pages'])
    detail=json.loads((tmp_path/'out/0000/result.json').read_text())
    assert 'table_missing' in codes(detail)


def test_batch_failing_input_does_not_skip_other_pages(tmp_path):
    from site_profiles.check_markdown import run
    manifest=make_manifest(tmp_path);data=json.loads(manifest.read_text());data['pages'][0]['html']='missing.html';manifest.write_text(json.dumps(data))
    r=run(manifest,tmp_path/'out')
    assert r['counts']=={'error':1,'pass':1}


def test_discovery_sample_and_holdout_coverage_remain_open(tmp_path):
    from site_profiles.check_markdown import run
    manifest=make_manifest(tmp_path);data=json.loads(manifest.read_text())
    data['pages'][1]['url']=data['pages'][0]['url']
    structure=dict(urls=[dict(key='missing',url='https://test.example/missing')],sample=['missing'],coverage=dict(gaps=['dynamic_not_exhausted']))
    (tmp_path/'structure.json').write_text(json.dumps(structure));data['structure_report']='structure.json';manifest.write_text(json.dumps(data))
    r=run(manifest,tmp_path/'out');assert r['status']=='uncertain'
    assert 'holdout_overlaps_control' in r['uncertainties']
    assert any(isinstance(g,dict) and g['code']=='discovery_sample_missing' for g in r['uncertainties'])


def test_output_cannot_escape_codex_work(tmp_path):
    from site_profiles.check_markdown import new_output
    (tmp_path/'redirect').symlink_to('/tmp',target_is_directory=True)
    with pytest.raises(ValueError):new_output(tmp_path/'redirect/new_markdown_audit')


def test_markdown_reference_links_and_bold_are_not_facts():
    html='<main><p>Размер: 12 мм. Серия Альфа</p></main>'
    md='**Размер**: 12 мм. Серия [Альфа](https://test.example/987)'
    assert check(md,html)['status']=='pass'


def test_colspan_overlap_not_silently_accepted():
    html='<main><table><tr><td>A</td><td rowspan="2">B</td></tr><tr><td colspan="2">C</td></tr></table></main>'
    assert check('A B C',html)['status']=='uncertain'


def test_definition_values_cannot_be_swapped():
    html='<main><h1>Насос</h1><dl><dt>Масса</dt><dd>10 кг</dd><dt>Нагрузка</dt><dd>20 кг</dd></dl></main>'
    assert check('# Насос\nМасса\n10 кг\nНагрузка\n20 кг',html)['status']=='pass'
    assert check('# Насос\nМасса\n20 кг\nНагрузка\n10 кг',html)['status']=='fail'


@pytest.mark.parametrize('md',[
 '| Насосы серии А | |\n| --- | --- |\n| Модель | Масса |\n| А1 | 10 кг |',
 '### Насосы серии А\n| Модель | Масса |\n| --- | --- |\n| А1 | 10 кг |',
])
def test_full_width_table_title_can_be_heading_or_banner(md):
    html='<main><table><tr><th colspan="2">Насосы серии А</th></tr><tr><th>Модель</th><th>Масса</th></tr><tr><td>А1</td><td>10 кг</td></tr></table></main>'
    assert check(md,html)['status']=='pass'


def test_steel_grade_is_not_dimension():
    assert canonical('20Х13')!=canonical('20х13')


def test_inequality_operator_preserved():
    assert canonical('≤ 0,5 мм')==canonical('<= 0.5 мм')
    assert canonical('≤ 0,5 мм')!=canonical('>= 0.5 мм')


def test_raw_html_table_in_markdown_is_a_table():
    table='<table><caption>Насос</caption><tr><th>Модель</th><th>Вес</th></tr><tr><td>A</td><td>10 кг</td></tr></table>'
    assert check(table,'<main>'+table+'</main>')['status']=='pass'


def test_note_can_remain_in_table():
    html='<main><table><caption>Насос</caption><tr><th>Модель</th><th>Вес</th></tr><tr><td>A*</td><td>10 кг</td></tr><tr><td colspan="2">* При 20 C</td></tr></table></main>'
    md='### Насос\n| Модель | Вес |\n| --- | --- |\n| A* | 10 кг |\n| \\* При 20 C | |'
    assert check(md,html)['status']=='pass'


def test_interface_in_table_cells_is_detected():
    html='<main><table><tr><th>Товар</th><th>Действие</th></tr><tr><td>A</td><td><button>Купить</button></td></tr></table></main>'
    md='| Товар | Действие |\n| --- | --- |\n| A | Купить |'
    assert 'interface_leak' in codes(check(md,html))


def test_inferred_reference_does_not_claim_confirmed_data_loss():
    r=check_markdown('<main><p>Масса: 12 кг</p></main>','Масса: 13 кг')
    assert r['status']=='uncertain'
    assert r['findings'][0]['severity']=='review'
    assert check('Масса: 13 кг','<main><p>Масса: 12 кг</p></main>')['status']=='fail'


def test_actual_100000_character_cut_loses_tail_facts():
    description='Описание материала. '*5100
    html='<main><p>'+description+'</p><p>Плотность: 1250 кг/м³</p></main>'
    md=description+'\nПлотность: 1250 кг/м^3'
    r=check(md,html,model_input=md[:100000])
    assert not r['findings'] and r['model_input']['status']=='fail'
    assert any('1250' in str(f['expected']) for f in r['model_input']['findings'])


@pytest.mark.parametrize('source,target',[('20ºС','20 C'),('20˚С','20 C'),('20⁰С','20 C'),('20℃','20 C'),('32℉','32 F')])
def test_temperature_glyphs_from_existing_contract(source,target):
    assert canonical(source)==canonical(target)


def test_italic_text_is_not_a_footnote_marker():
    assert check('*Важно*: масса 10 кг','<main><p><em>Важно</em>: масса 10 кг</p></main>')['status']=='pass'


def test_model_heading_prefix_does_not_match_other_model():
    html='<main><h1>Насос А</h1><table><tr><th>Вес</th></tr><tr><td>10 кг</td></tr></table></main>'
    md='# Насос А\n# Насос А2\n| Вес |\n| --- |\n| 10 кг |'
    assert 'table_missing' in codes(check(md,html))
