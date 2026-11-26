"""Номера пунктов в исходных абзацах не должны теряться в проверяющем парсере."""
import pytest
from site_profiles.markdown_validation import check_markdown


@pytest.mark.parametrize('marker', ['1.', '2)'])
def test_literal_paragraph_number_preserved(marker):
    html = f'<main><p>{marker} Акрил — прочный, толщина 5 мм.</p></main>'
    report = check_markdown(html, f'{marker} Акрил — прочный, толщина 5 мм.', source_selectors=['main'])
    assert report['status'] == 'pass', report


def test_html_ordered_list_still_matches_markdown():
    html = '<main><ol><li>Давление 2 МПа.</li><li>Масса 5 кг.</li></ol></main>'
    report = check_markdown(html, '1. Давление 2 МПа.\n2. Масса 5 кг.', source_selectors=['main'])
    assert report['status'] == 'pass', report


@pytest.mark.parametrize('markdown', ['1. Давление 3 МПа.', '1. Давление 2 кПа.', '3. Давление 2 МПа.'])
def test_real_number_unit_and_literal_number_changes_fail(markdown):
    html = '<main><p>1. Давление 2 МПа.</p></main>'
    report = check_markdown(html, markdown, source_selectors=['main'])
    assert report['status'] == 'fail', report


def test_phone_link_with_parenthesized_destination():
    html = '<main><p><a href="tel:+7 (343) 305-71-84">+7 (343) 305-71-84</a></p></main>'
    md = '[+7 (343) 305-71-84](tel:+7 (343) 305-71-84)'
    assert check_markdown(html, md, source_selectors=['main'])['status'] == 'pass'
    changed = md.replace('305-71-84]', '305-71-99]')
    assert check_markdown(html, changed, source_selectors=['main'])['status'] == 'fail'


@pytest.mark.parametrize('level', [5, 6])
def test_wrapped_sibling_small_headings_not_duplicated(level):
    html = f'<main><h1>Условия</h1><div><h{level}>Доставка по России</h{level}></div><div><h{level}>Возврат за 24 часа</h{level}></div></main>'
    md = '# Условия\n\n' + '#' * level + ' Доставка по России\n\n' + '#' * level + ' Возврат за 24 часа'
    assert check_markdown(html, md, source_selectors=['main'])['status'] == 'pass'
    assert check_markdown(html, md.replace('24', '48'), source_selectors=['main'])['status'] == 'fail'


def test_unit_exponent_not_joined_to_following_price():
    html = '<main><p>760₽ /м2 895₽</p></main>'
    md = '760₽\n/м^2\n895₽'
    assert check_markdown(html, md, source_selectors=['main'])['status'] == 'pass'
    assert check_markdown(html, md.replace('^2', '^3'), source_selectors=['main'])['status'] == 'fail'
