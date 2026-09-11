"""Реплей контрольных URL досье P04 через категоризатор текущего дерева.

Печатает таблицу «код | URL | якорь | роль, приоритет» и пишет JSON-снимок для
сравнения «до/после» правки.

Запуск: python tests/tools/replay_control_urls.py [снимок.json]
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from config import Config          # noqa: E402
from web_crawler import URLCategorizer   # noqa: E402


def main() -> int:
    cases = json.load(open(os.path.join(_HERE, 'p04_control_urls.json'), encoding='utf-8'))
    categorizer = URLCategorizer(Config())
    categorizer.set_profile(None)
    snapshot = []
    for case in cases:
        # base_locale — то, что crawl_site вычисляет по рабочему URL компании (P04 U1)
        categorizer.set_base_locale(case.get('base_locale'))
        role, priority = categorizer.categorize_url(case['url'], case.get('text', ''))
        snapshot.append({**case, 'role': role, 'priority': priority})
        print(f"{case['code']:<6} {case['unit']:<3} {role:<11} {priority:<2} "
              f"{case['url']}  [{case.get('text', '')}]")
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w', encoding='utf-8') as out:
            json.dump(snapshot, out, ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
