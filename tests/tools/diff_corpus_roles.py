"""Диф двух снимков ролей (replay_corpus_roles.py): что изменилось после правки.

Печатает: распределение до/после, число сменивших роль URL по направлениям
(пара «было -> стало»), топ-N примеров в каждую сторону и компании, у которых
число товарных URL изменилось более чем на заданный процент.

Запуск: python tests/tools/diff_corpus_roles.py <до.json> <после.json> [топ] [порог_%]
"""
import json
import sys
from collections import Counter, defaultdict


def main() -> int:
    before = json.load(open(sys.argv[1], encoding='utf-8'))
    after = json.load(open(sys.argv[2], encoding='utf-8'))
    top = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    threshold = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0

    print(f"URL всего: до {before['urls_total']}, после {after['urls_total']}")
    print("Распределение ролей:")
    keys = sorted(set(before['distribution']) | set(after['distribution']))
    for key in keys:
        b = before['distribution'].get(key, 0)
        a = after['distribution'].get(key, 0)
        print(f"  {key:<12} {b:>7} -> {a:>7}  ({a - b:+d})")

    transitions = Counter()
    examples = defaultdict(list)
    for url, (role_b, _prio_b, _c) in before['roles'].items():
        entry = after['roles'].get(url)
        if entry is None:
            continue
        role_a = entry[0]
        if role_a != role_b:
            transitions[(role_b, role_a)] += 1
            if len(examples[(role_b, role_a)]) < top:
                examples[(role_b, role_a)].append(url)

    changed = sum(transitions.values())
    print(f"\nСменили роль: {changed} URL ({changed * 100.0 / max(before['urls_total'], 1):.2f} %)")
    for (role_b, role_a), count in transitions.most_common():
        print(f"\n  {role_b} -> {role_a}: {count}")
        for url in examples[(role_b, role_a)]:
            print(f"    {url}")

    print(f"\nКомпании с изменением числа товарных URL более чем на {threshold:.0f} %:")
    grew, shrank = [], []
    companies = set(before['per_company_products']) | set(after['per_company_products'])
    for company in companies:
        b = before['per_company_products'].get(company, 0)
        a = after['per_company_products'].get(company, 0)
        if b == a:
            continue
        base = max(b, 1)
        delta_pct = (a - b) * 100.0 / base
        if delta_pct > threshold:
            grew.append((delta_pct, company, b, a))
        elif delta_pct < -threshold:
            shrank.append((delta_pct, company, b, a))
    print(f"  выросло: {len(grew)}, упало: {len(shrank)}")
    for label, rows in (('выросло', sorted(grew, reverse=True)), ('упало', sorted(shrank))):
        for delta_pct, company, b, a in rows[:top]:
            print(f"    [{label}] {company}: {b} -> {a} ({delta_pct:+.0f} %)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
