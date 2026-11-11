"""Собственные текстовые разделы опубликованной вкладки Bitumex."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from product_interactions import _control_available, panel_section


def _key(text):
    return re.sub(r'\s+', ' ', text).strip().casefold().rstrip(':.')


# Только явные подписи страницы; неизвестная подпись прекращает раздел.
_FIELDS = {
    'области применения': 'applications', 'область применения': 'applications',
    'свойства и преимущества': 'advantages', 'свойства продукта': 'advantages',
    'преимущества': 'advantages', 'особенности материала': 'advantages',
    'применение': 'instructions_manuals',
    'свойства материала': 'advantages', 'производство работ': 'instructions_manuals',
    'подготовка поверхности': 'instructions_manuals', 'грунтование': 'instructions_manuals',
    'рекомендации по использованию': 'instructions_manuals',
    'подготовка материала к работе': 'instructions_manuals',
    'использование материала': 'instructions_manuals',
    'перед началом работ': 'instructions_manuals',
    'способ подготовки поверхности': 'instructions_manuals',
    'нанесение': 'instructions_manuals', 'прочие указания': 'instructions_manuals',
    'очистку инструмента': 'instructions_manuals',
    'приготовление компонента а': 'instructions_manuals',
    'приготовление компонента б': 'instructions_manuals',
    'особенности применения': 'instructions_manuals',
    'площадная инъекция': 'instructions_manuals',
    'температурные швы и трещины': 'instructions_manuals',
    'с использованием однокомпонентного насоса': 'instructions_manuals',
    'меры безопасности': 'security_measures', 'меры предосторожности': 'security_measures',
    'указания по безопасности': 'security_measures', 'утилизация': 'security_measures',
    'хранение': 'storage', 'упаковка': 'complectation',
    'транспортировка': 'storage', 'гарантийный срок хранения': 'storage',
    'условия транспортировки и хранения': 'mixed_storage',
}


def restore_bitumex_source_sections(ai_data, html, page_url):
    """Не принимает товар и не использует остаток модели или содержимое файлов.

    Сохраняет контекст исходного раздела целиком: условия набухания в тексте
    не заменяются похожим числом из таблицы. Эти текстовые разделы не дают
    дополнительных баллов числовому гейту.
    """
    parsed = urlsplit(page_url or '')
    if ((parsed.hostname or '').lower().removeprefix('www.') != 'bitumex.org'
            or not parsed.path.startswith('/product/') or not isinstance(ai_data, dict)
            or ai_data.get('error')):
        return False
    product = ai_data.get('product')
    if not isinstance(product, dict) or not isinstance(product.get('specifications'), dict):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    owner = soup.select_one('body.single-product main.main')
    panel = soup.select_one('#tab-description.woocommerce-Tabs-panel--description')
    control = soup.select_one('#tab-title-description a[href="#tab-description"]')
    if (owner is None or panel is None or owner.select_one('.page-header h1') is None
            or not _control_available(control)
            or owner not in panel.parents or owner not in control.parents
            or panel.find_parent(['template', 'script', 'noscript', 'footer', 'aside'])):
        return False
    # Скрытой может быть только сама доступная вкладка, не чужой контейнер.
    if not _control_available(panel.parent):
        return False
    public = panel_section(soup, panel, 'Описание', 'tab-description')
    fields, intro = {}, []
    context = 'intro'
    for node in public.find_all(['p', 'ul', 'ol', 'h2', 'h3', 'h4'], recursive=False):
        if not _control_available(node):
            continue
        if node.name.startswith('h'):
            if node.get_text(' ', strip=True) != 'Описание':
                context = _FIELDS.get(_key(node.get_text(' ', strip=True)))
            continue
        # Внутренняя скрытая заготовка не становится видимой вместе с вкладкой.
        for hidden in list(node.select('script, style, template, noscript, [hidden], [style]')):
            if hidden.name in ('script', 'style', 'template', 'noscript') or not _control_available(hidden):
                hidden.decompose()
        indices = node.find_all(['sup', 'sub'])
        if indices:
            from text_extractor import MarkdownConverter
            converter = MarkdownConverter()
            for index in indices:
                index.replace_with(converter.convert(str(index)))
            node.smooth()
        text = node.get_text(' ', strip=True)
        if not text or (node.find('a') and text == node.find('a').get_text(' ', strip=True)):
            continue
        bold = node.find(['strong', 'b'])
        if bold is not None:
            heading = bold.get_text(' ', strip=True)
            label = _key(heading)
            if label in _FIELDS:
                context = _FIELDS[label]
            elif len(heading) < 85 and text == heading:
                # Не присоединяем упаковку/расход/неизвестный раздел к инструкции.
                context = None
        if context == 'mixed_storage':
            # В общей секции сайта отдельно подписаны состав, комплект и срок.
            if re.search(r'двухкомпонентный состав\s*\(', text, re.I):
                intro.append(text)
            elif re.match(r'Комплектность:', text, re.I):
                fields.setdefault('complectation', []).append(text)
            else:
                fields.setdefault('storage', []).append(text)
        elif context == 'intro':
            intro.append(text)
        elif context:
            fields.setdefault(context, []).append(text)
    changed = False
    if intro:
        # Это собственное описание состава и поведения материала, не весь body.
        # Краткое description модели сохраняет контракт трёх предложений.
        key = 'Состав и свойства материала'
        value = '\n'.join(intro)
        if product['specifications'].get(key) != value:
            product['specifications'][key] = value
            changed = True
    for field, parts in fields.items():
        current = product.get(field, '')
        if not isinstance(current, str):
            continue
        # Дополняем только исходными полными блоками; неизвестные сведения
        # модели не стираются, совпадающий исходный блок повторно не добавляется.
        for part in parts:
            if _key(part) not in _key(current):
                current = '\n'.join(filter(None, (current, part)))
        if current != product.get(field, ''):
            product[field] = current
            changed = True
    return changed
