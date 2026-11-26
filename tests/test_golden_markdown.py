"""Регрессия «байт-в-байт»: вывод html_to_markdown по корпусу не меняется.

Эталоны генерирует site_profiles/tools/make_golden_markdown.py с кода ДО врезки
роутера профилей; после врезки этот тест обязан оставаться зелёным (главный гейт
шага 3 фазы 0 внедрения профилирования).
"""
import json
from pathlib import Path

import pytest

import text_extractor

GOLDEN_DIR = Path(__file__).parent / 'golden'


def _load_index():
    idx = GOLDEN_DIR / 'index.json'
    if not idx.exists():
        return []
    return json.loads(idx.read_text(encoding='utf-8'))

_INDEX = _load_index()


@pytest.mark.skipif(not _INDEX, reason='эталоны не сгенерированы (make_golden_markdown.py)')
@pytest.mark.parametrize('entry', _INDEX, ids=[e['golden'] for e in _INDEX])
def test_markdown_matches_golden(entry):
    source = Path(entry['source'])
    if not source.exists():
        pytest.skip(f'исходник корпуса недоступен: {source}')
    html = source.read_text(encoding='utf-8', errors='replace')
    markdown = text_extractor.html_to_markdown(html, entry['url'], entry['page_type'])
    golden = (GOLDEN_DIR / entry['golden']).read_text(encoding='utf-8')
    assert markdown == golden, f'markdown разошёлся с эталоном: {entry["golden"]}'
