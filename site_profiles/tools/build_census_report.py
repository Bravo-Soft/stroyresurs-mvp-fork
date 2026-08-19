#!/usr/bin/env python3
"""Отчёт заказчику по итогам переписи: сайт -> можем/не можем отдать карточки,
в чём проблема и как устраняем. Структура колонок = таблица заказчика
(«Статус после полной обработки», «Группа доработки Сервиса», «Количество товаров»,
«Ошибка…», «Комментарии»).

Вход: Base/profile_metrics/<домен>/*.json (последний на домен) + profiles_drafts/<домен>.yaml.
Выход: XLSX (+CSV) со строкой на домен.

Запуск:
  python site_profiles/tools/build_census_report.py \
      [--metrics DIR] [--drafts DIR] [--out census_report.xlsx]
"""
import argparse
import glob
import json
import sys
from pathlib import Path

MVP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MVP))

import yaml  # noqa: E402


def latest_metrics(metrics_dir: Path) -> dict:
    """{домен: последний metrics-json}."""
    result = {}
    for dom_dir in sorted(metrics_dir.iterdir()) if metrics_dir.is_dir() else []:
        files = sorted(glob.glob(str(dom_dir / '*.json')))
        if files:
            try:
                result[dom_dir.name] = json.load(open(files[-1], encoding='utf-8'))
            except Exception:
                pass
    return result


def classify(m: dict, draft: dict) -> dict:
    """Вердикт по сайту: статус, группа доработки, причина, план устранения."""
    output = m.get('output') or {}
    cards = output.get('cards_generated') or 0
    found = output.get('products_found') or 0
    pages = m.get('pages_crawled') or 0
    fail_rate = m.get('fetch_fail_rate') or 0
    challenge = m.get('challenge_rate') or 0
    render = (draft.get('crawl') or {}).get('render')
    antibot = (draft.get('crawl') or {}).get('antibot')
    confidence = draft.get('confidence')

    problems, fixes = [], []
    if antibot and antibot != 'none' or challenge > 0.2:
        problems.append(f'антибот-защита (challenge_rate={challenge})')
        fixes.append('профиль: antibot=impersonate/warmup (обход curl_cffi+прогрев)')
    if pages <= 3:
        problems.append(f'краулер собрал {pages} стр. (SPA/JS-рендер или блокировка)')
        fixes.append('профиль: render=browser (принудительный Playwright)')
    elif render == 'browser' and fail_rate > 0.3:
        problems.append(f'высокая доля сбоев фетча ({fail_rate})')
        fixes.append('профиль: render=browser + пер-хостовые задержки (load.page_delay_ms)')
    if found > 0 and cards == 0:
        problems.append(f'{found} товарных стр. найдено, 0 карточек '
                        f'(LLM классифицирует как не-товар / нет ТХ)')
        fixes.append('REVIEW: проверить product_url_antipatterns и полноту markdown '
                     '(возможно ТХ в AJAX-вкладках -> ajax_tabs)')
    elif cards and found > 3 * cards:
        problems.append(f'категоризация URL шумит: {found} «товарных» стр. -> {cards} карточек')
        fixes.append('профиль: product_url_antipatterns по census_evidence.top_urls')
    for alert in (m.get('alerts') or []):
        if 'секция' in alert:
            problems.append(alert)
            fixes.append('профиль: заполнить contacts_urls/distributor_urls (страница есть, '
                         'но не распознана) либо подтвердить отсутствие раздела')

    if pages == 0:
        status, group = 'Сайт недоступен', 'Краулер/доступность'
    elif cards == 0:
        status = 'Обработан, карточки НЕ получены'
        group = ('SPA/JS' if pages <= 3 else
                 'Антибот' if (antibot and antibot != 'none') else 'Извлечение')
    else:
        status = 'Обработан, карточки получены'
        group = ('Категоризация URL' if found > 3 * cards else
                 'Антибот' if (antibot and antibot != 'none') else '—')

    can_deliver = ('Да' if cards > 0 and not problems else
                   'Да, с оговорками' if cards > 0 else 'Нет')
    return {'status': status, 'group': group, 'cards': cards,
            'can_deliver': can_deliver,
            'problems': '; '.join(problems) or '—',
            'fixes': '; '.join(dict.fromkeys(fixes)) or '—',
            'confidence': confidence}


def build_rows(metrics_dir: Path, drafts_dir: Path, product_cap: int = None):
    rows = []
    for domain, m in latest_metrics(metrics_dir).items():
        draft_path = drafts_dir / f'{domain}.yaml'
        draft = {}
        if draft_path.exists():
            try:
                draft = yaml.safe_load(draft_path.read_text(encoding='utf-8')) or {}
            except Exception:
                pass
        verdict = classify(m, draft)
        cards = verdict['cards']
        cards_str = str(cards)
        if product_cap and (m.get('product_pages') or 0) >= product_cap:
            cards_str = f'{cards}+ (лимит переписи {product_cap} товарных стр.)'
        rows.append({
            'Website': f'https://{domain}/',
            'Наименование': m.get('company_name') or '',
            'Статус после полной обработки': verdict['status'],
            'Группа доработки Сервиса': verdict['group'],
            'Количество товаров': cards_str,
            'Можем отдать карточки': verdict['can_deliver'],
            'Ошибка/проблема': verdict['problems'],
            'План устранения': verdict['fixes'],
            'Комментарии': (f'confidence профиля {verdict["confidence"]}; '
                            f'страниц {m.get("pages_crawled")}, '
                            f'товарных {m.get("product_pages")}'),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metrics', default='/ai/projects/stroy-resurs/mvp_directories/Base/profile_metrics')
    parser.add_argument('--drafts', default='/ai/projects/stroy-resurs/mvp_directories/profiles_drafts')
    parser.add_argument('--out', default='census_report.xlsx')
    parser.add_argument('--product-cap', type=int, default=None,
                        help='лимит товарных страниц прогона: пометить сайты, упёршиеся в кап')
    args = parser.parse_args()

    rows = build_rows(Path(args.metrics), Path(args.drafts), args.product_cap)
    if not rows:
        print('Нет метрик — отчёт пуст')
        return 1

    import pandas as pd
    df = pd.DataFrame(rows)
    out = Path(args.out)
    if out.suffix == '.xlsx':
        df.to_excel(out, index=False)
    df.to_csv(out.with_suffix('.csv'), index=False)
    print(f'Отчёт: {len(rows)} сайтов -> {out} (+ .csv)')
    print(df[['Website', 'Статус после полной обработки', 'Группа доработки Сервиса',
              'Количество товаров', 'Можем отдать карточки']].to_string(index=False, max_colwidth=42))
    return 0


if __name__ == '__main__':
    sys.exit(main())
