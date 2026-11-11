#!/usr/bin/env python3
"""Одноразовый генератор профилей mvp/profiles/*.yaml из существующего кода (шаг 2 фазы 0).

Источники:
  - адаптеры Profile_Markdown/ (через text_extractor._get_adapters): tier=custom + флаги;
  - настройки обхода из существующих profiles/*.yaml;
  - ручные дополнения MANUAL (хардкоды из бизнес-логики, карта разделов aerobel из D98).

Скрипт детерминирован и перезапускаем: перезаписывает <домен>.yaml целиком.
В конце печатает кросс-отчёт покрытия настроек YAML и известных хардкодов.
"""
import sys
from pathlib import Path

MVP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MVP))

import yaml  # noqa: E402

import text_extractor  # noqa: E402
from site_profiles import profile_from_dict  # noqa: E402

PROFILED_AT = '2026-08-18'

# Ручные дополнения: то, что нельзя вывести из кода автоматически.
# Ключ — домен; значения глубоко сливаются в сгенерированный профиль.
MANUAL = {
    'hms.ru': {
        'crawl': {'subdomain_collapse': ['*.hms.ru']},
        'notes': ['Поддомены *.hms.ru схлопываются в hms.ru (перенос хардкода '
                  'text_extractor._normalize_domain)'],
    },
    'chelaz.ru': {
        'extract': {'streaming': False},
        'notes': ['D85: типоразмеры/URL-алиасы схлопываются ПОСЛЕ полного краула '
                  '(main.py collapse_chelaz_size_variants) — стриминг выключен'],
    },
    'aerobel.ru': {
        'sections': {
            'catalog_roots': ['/catalog/'],
            'distributor_urls': ['/cooperation/'],
            'contacts_urls': ['/contacts/', '/company/'],
        },
        'notes': ['D98: товары в /catalog/, дилеры /cooperation/ (~700 записей), '
                  'контакты /contacts/ /company/ (раздел /about/ намеренно не добавлен '
                  'в contacts_urls: его поддерево жгло бюджет краула, см. D98)'],
    },
    'tizol.com': {
        'crawl': {'tab_panel_selector': '.product-page__section-content'},
        'notes': ['D01: характеристики в AJAX-вкладках, склейка в один HTML; '
                  'панель вкладки (Vue) — .product-page__section-content'],
    },
    'betar.ru': {
        'notes': ['D82: Bitrix product_offers.php — таблица исполнений дотягивается '
                  'AJAX-запросом и вклеивается до markdown'],
    },
    'pspcom.ru': {
        'notes': ['D69: /catalog/build/ — девелоперское направление, не товары'],
    },
    'pktmt.ru': {
        'notes': ['D89: /practice/ — статьи «Применение», не товары; товары в /produktsiya/',
                  'Контент только на www (non-www 301->404) — канонизация К www'],
    },
    # Профили-заметки: домен зашит в бизнес-логику, поведение остаётся в коде до фазы 2.
    'place-start.ru': {
        'notes': ['Домен зашит в глобальный exclude_url_patterns (web_crawler.py, D58-блок): '
                  'ссылки на него не краулятся ни с одного сайта'],
    },
    'viran.ru': {
        'notes': ['Домен зашит в глобальный exclude_url_patterns (web_crawler.py): '
                  'ссылки на него не краулятся ни с одного сайта'],
    },
    'actey.com': {
        'notes': ['Спец-обработка относительных путей assets/, files/, download/ '
                  '(web_crawler.py, поиск файлов)'],
    },
}


def deep_merge(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        elif isinstance(value, list) and isinstance(dst.get(key), list):
            dst[key] = dst[key] + [x for x in value if x not in dst[key]]
        else:
            dst[key] = value
    return dst


def build_profiles() -> dict:
    """Возвращает {домен: dict-профиль} из адаптеров + существующих YAML + MANUAL."""
    profiles = {}

    def get(domain):
        return profiles.setdefault(domain, {
            'domain': domain,
            'profile_version': 1,
            'profiled_at': PROFILED_AT,
            'source': 'manual',
            'confidence': 1.0,
            'notes': [],
        })

    # 1. Адаптеры Profile_Markdown -> tier=custom + флаги.
    from site_profiles import normalize_domain
    for domain, module in sorted(text_extractor._get_adapters().items()):
        module_name = module.__name__.split('.')[-1]
        clean_domain = normalize_domain(domain)
        if clean_domain != domain:
            # Ключ диспетчера не совпадает с netloc (agroskon: DOMAIN со слэшами) —
            # старый код такой адаптер НИКОГДА не применял. Миграция честная:
            # generic-поведение + заметка; включение адаптера — решение фазы 2.
            profile = get(clean_domain)
            profile['notes'].append(
                f'Адаптер Profile_Markdown/{module_name}.py существует, но в старом '
                f'диспетчере не матчился (DOMAIN={domain!r}) — фактическое поведение '
                f'generic; включение tier=custom — осознанное решение фазы 2')
            continue
        profile = get(domain)
        extract = profile.setdefault('extract', {})
        extract['tier'] = 'custom'
        extract['custom_module'] = module_name
        for attr, key in (('KEEP_HIDDEN_TABS', 'keep_hidden_tabs'),
                          ('NEEDS_RAW_HTML', 'needs_raw_html'),
                          ('CLEAN_IN_UNIVERSAL', 'clean_in_universal')):
            if getattr(module, attr, False):
                extract[key] = True
        profile['notes'].append(f'Адаптер markdown: Profile_Markdown/{extract["custom_module"]}.py')

    # 2. Сохраняем настройки существующих профилей после удаления дублей из Config.
    for path in sorted((MVP / 'profiles').glob('*.yaml')):
        if not path.name.startswith('_'):
            data = yaml.safe_load(path.read_text(encoding='utf-8'))
            deep_merge(get(data['domain']), data)

    # 3. Ручные дополнения.
    for domain, extra in MANUAL.items():
        deep_merge(get(domain), extra)

    return profiles


def coverage_report(profiles: dict):
    """Кросс-отчёт: каждый параметр обхода YAML и известный хардкод покрыт профилем."""
    checks = []
    for path in sorted((MVP / 'profiles').glob('*.yaml')):
        if path.name.startswith('_'):
            continue
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        domain = data['domain']
        for key, value in data.get('crawl', {}).items():
            checks.append((f'{domain}: crawl.{key}',
                           profiles.get(domain, {}).get('crawl', {}).get(key) == value))
    from site_profiles import normalize_domain
    for domain, module in text_extractor._get_adapters().items():
        module_name = module.__name__.split('.')[-1]
        clean_domain = normalize_domain(domain)
        if clean_domain != domain:
            # мёртвый ключ диспетчера: покрытие = профиль с заметкой (без tier=custom)
            notes = profiles.get(clean_domain, {}).get('notes', [])
            checks.append((f'адаптер {module_name} (мёртвый ключ {domain!r}) -> заметка',
                           any(module_name in n for n in notes)))
        else:
            checks.append((f'адаптер {module_name} -> {domain}',
                           profiles.get(domain, {}).get('extract', {}).get('tier') == 'custom'))
    for domain in ('hms.ru', 'chelaz.ru', 'place-start.ru', 'viran.ru', 'actey.com'):
        checks.append((f'хардкод {domain}', domain in profiles))
    return checks


def main():
    out_dir = MVP / 'profiles'
    out_dir.mkdir(exist_ok=True)
    profiles = build_profiles()

    written = 0
    for domain, data in sorted(profiles.items()):
        _, errors = profile_from_dict(data)
        if errors:
            print(f'ОШИБКА: профиль {domain} невалиден, не записан: {errors}')
            continue
        path = out_dir / f'{domain}.yaml'
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
            encoding='utf-8')
        written += 1

    print(f'Записано профилей: {written} -> {out_dir}')
    print('\nКросс-отчёт покрытия:')
    all_ok = True
    for name, ok in coverage_report(profiles):
        print(f'  [{"OK" if ok else "НЕТ"}] {name}')
        all_ok = all_ok and ok
    print('\nИтог:', 'полное покрытие' if all_ok else 'ЕСТЬ НЕПОКРЫТЫЕ ЭЛЕМЕНТЫ')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
