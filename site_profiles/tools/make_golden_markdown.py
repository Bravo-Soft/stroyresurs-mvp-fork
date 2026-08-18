#!/usr/bin/env python3
"""Генерация golden-эталонов markdown с ТЕКУЩЕГО кода (запускается ДО врезки роутера).

Выход:
  tests/golden/index.json            — список страниц (источник, url, page_type, файл эталона);
  tests/golden/<Компания>/<кат>/*.md — эталонный markdown (вывод html_to_markdown);
  tests/golden/dispatch_map.json     — карта диспетчеризации адаптеров Profile_Markdown
                                       (домен -> модуль + флаги) для теста эквивалентности роутера.

Запуск: python site_profiles/tools/make_golden_markdown.py [--corpus PATH] [--per-cat N]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

MVP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MVP))

import text_extractor  # noqa: E402

# Папка корпуса -> page_type, который main.py передаёт в html_to_markdown.
CATEGORIES = {
    'Product_pages': '',
    'Company_pages': 'company',
    'Distributor_pages': 'distributor',
    'Other_pages': '',
}


def build_dispatch_map() -> dict:
    """Снимок текущей диспетчеризации адаптеров: домен -> модуль + флаги/методы."""
    result = {}
    for domain, module in sorted(text_extractor._get_adapters().items()):
        result[domain] = {
            'module': module.__name__.split('.')[-1],
            'keep_hidden_tabs': bool(getattr(module, 'KEEP_HIDDEN_TABS', False)),
            'needs_raw_html': bool(getattr(module, 'NEEDS_RAW_HTML', False)),
            'clean_in_universal': bool(getattr(module, 'CLEAN_IN_UNIVERSAL', False)),
            'has_clean': callable(getattr(module, 'clean', None)),
            'has_extract': callable(getattr(module, 'extract', None)),
            'has_extract_company': callable(getattr(module, 'extract_company', None)),
            'has_extract_distributor': callable(getattr(module, 'extract_distributor', None)),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', default='/ai/projects/stroy-resurs/mvp_directories/Base/temp_html_storage')
    parser.add_argument('--out', default=str(MVP / 'tests' / 'golden'))
    parser.add_argument('--per-cat', type=int, default=25,
                        help='максимум страниц на категорию на компанию (детерминированно: сортировка имён)')
    args = parser.parse_args()

    corpus = Path(args.corpus)
    out = Path(args.out)
    index = []

    for company_dir in sorted(corpus.iterdir()) if corpus.is_dir() else []:
        if not company_dir.is_dir():
            continue
        for cat, page_type in CATEGORIES.items():
            cat_dir = company_dir / cat
            if not cat_dir.is_dir():
                continue
            for html_path in sorted(cat_dir.glob('*.html'))[:args.per_cat]:
                url = ''
                meta_path = html_path.with_suffix('.json')
                if meta_path.exists():
                    try:
                        url = json.loads(meta_path.read_text(encoding='utf-8')).get('url', '')
                    except Exception:
                        pass
                html = html_path.read_text(encoding='utf-8', errors='replace')
                markdown = text_extractor.html_to_markdown(html, url, page_type)
                digest = hashlib.sha1(html_path.name.encode()).hexdigest()[:16]
                rel = f'{company_dir.name}/{cat}/{digest}.md'
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(markdown, encoding='utf-8')
                index.append({'golden': rel, 'source': str(html_path), 'url': url, 'page_type': page_type})

    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.json').write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'dispatch_map.json').write_text(
        json.dumps(build_dispatch_map(), ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'golden: {len(index)} страниц, адаптеров в dispatch_map: '
          f'{len(build_dispatch_map())} -> {out}')


if __name__ == '__main__':
    main()
