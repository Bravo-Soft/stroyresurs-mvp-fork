"""Офлайн-реплей ролей categorize_url по корпусу прошлого прогона.

Прогоняет TSV из dump_corpus_urls.py через URLCategorizer(Config()) БЕЗ профиля
сайта (как в досье пакета P04) и пишет JSON-снимок: распределение ролей, роль
каждого URL и число товарных URL по компаниям. Снимки двух ревизий кода
сравниваются скриптом diff_corpus_roles.py.

Запуск: python tests/tools/replay_corpus_roles.py <корпус.tsv> <снимок.json>
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import Config          # noqa: E402
from web_crawler import URLCategorizer   # noqa: E402


def main() -> int:
    tsv_path, out_path = sys.argv[1], sys.argv[2]
    categorizer = URLCategorizer(Config())
    categorizer.set_profile(None)

    roles = {}
    dist = Counter()
    per_company_products = Counter()
    per_company_total = Counter()
    with open(tsv_path, 'r', encoding='utf-8') as fh:
        for line in fh:
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 4:
                continue
            company, _folder, _category, url = parts
            try:
                role, priority = categorizer.categorize_url(url)
            except Exception as e:      # реплей не должен падать на битом URL
                role, priority = f'ERROR:{type(e).__name__}', -1
            roles[url] = [role, priority, company]
            dist[role] += 1
            per_company_total[company] += 1
            if role == 'product':
                per_company_products[company] += 1

    snapshot = {
        'urls_total': len(roles),
        'distribution': dict(sorted(dist.items())),
        'roles': roles,
        'per_company_products': dict(per_company_products),
        'per_company_total': dict(per_company_total),
    }
    with open(out_path, 'w', encoding='utf-8') as out:
        json.dump(snapshot, out, ensure_ascii=False)
    print(f"URL: {len(roles)}; распределение: {dict(sorted(dist.items()))}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
