"""Условия единого расхода обойного клея из опубликованных вкладок Quality."""
import re
from copy import deepcopy
import unicodedata
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from product_interactions import _control_available


def _norm(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text))).casefold().strip().rstrip('.;')


def _public_node(node, reveal_root=False):
    node = deepcopy(node)
    if reveal_root:
        # Саму вкладку разрешает её доступный опубликованный переключатель.
        node.attrs.pop('hidden', None)
        node.attrs.pop('aria-hidden', None)
        node['style'] = re.sub(r'display\s*:\s*none|visibility\s*:\s*hidden', '',
                               node.get('style', ''), flags=re.I)
    for child in list(node.select('script, style, template, noscript, [hidden], [style]')):
        if child.name is not None and (child.name in ('script', 'style', 'template', 'noscript')
                                       or not _control_available(child)):
            child.decompose()
    return node


def _append(product, field, line):
    previous = product.get(field) or ''
    if _norm(line) not in {_norm(v.lstrip('-• ')) for v in previous.splitlines()}:
        product[field] = '\n'.join([previous, '- ' + line]).strip()


def restore_quality_source_fields(ai_data, html, page_url):
    """Правит только положительный ответ; параметры других товаров не затрагивает."""
    host = (urlsplit(page_url or '').hostname or '').lower().removeprefix('www.')
    if host != 'quality-pro.ru' or not isinstance(ai_data, dict) or ai_data.get('error'):
        return False
    product = ai_data.get('product')
    if not isinstance(product, dict) or not isinstance(product.get('specifications'), dict):
        return False
    if any(not isinstance(product.get(field) or '', str) for field in ('instructions_manuals', 'complectation')):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    panels = []
    for item in soup.select('.t-store__tabs__item'):
        title = item.select_one('.t-store__tabs__item-button .t-store__tabs__item-title')
        panel = item.select_one('.t-store__tabs__content')
        if (title is not None and panel is not None
                and _control_available(title)
                and _norm(_public_node(title).get_text(' ', strip=True)) == 'область применения'
                and not any(getattr(p, 'name', '') in ('script', 'template', 'noscript') for p in item.parents)
                and re.search(r'расход клея зависит от их типа',
                              _public_node(panel, reveal_root=True).get_text(' ', strip=True), re.I)):
            panels.append(_public_node(panel, reveal_root=True))
    if len(panels) != 1:
        return False
    panel = panels[0]
    source = panel.get_text(' ', strip=True)
    coverage, dilution = [], []
    preparation = re.search(
        r'Для этого (на одну упаковку клея используется .+?литр\w* воды,? '
        r'что позволяет обработать поверхность площадью ([^.]+?м[²2]))[.]', source, re.I)
    if preparation:
        coverage.append('Предварительная обработка стен: ' + preparation.group(2) + ' на одну упаковку')
        dilution.append('Предварительная обработка стен: ' + preparation.group(1))
    for li in panel.select('li'):
        line = li.get_text(' ', strip=True)
        match = re.fullmatch(r'(.+?)\s*[—–-]\s*(\d+(?:[.,]\d+)?\s*литр\w* воды на упаковку),?\s*хватает на (.+)', line, re.I)
        if not match:
            continue
        context, water, amount = match.groups()
        coverage.append(context + ': ' + amount.rstrip('.') + ' на упаковку')
        dilution.append(context + ': ' + water)
    if len(coverage) < 3 or len(dilution) != len(coverage):
        return False
    rolls = re.findall(r'Стандартный рулон(?: флизелиновых)? обоев:\s*\d+(?:[.,]\d+)?'
                       r'\s*[×хx]\s*\d+(?:[.,]\d+)?\s*м\b', source, re.I)
    for roll in rolls:
        coverage.append(roll + ' (условие расчета расхода)')
    specs = product['specifications']

    def clean(obj):
        for label in list(obj):
            value = obj[label]
            if re.match(r'^расход(?:\s|\(|$)', str(label), re.I):
                del obj[label]
            elif re.search(r'стандартн\w* рулон|(?:длина|ширина).*рулон.*обоев', str(label), re.I):
                del obj[label]
            elif re.fullmatch(r'фасовка|масса упаковки|вес упаковки', str(label).strip(), re.I) and isinstance(value, (str, int, float)):
                _append(product, 'complectation', str(label) + ': ' + str(value))
                del obj[label]
    clean(specs)
    specs['Расход'] = '; '.join(coverage)
    for line in dilution:
        _append(product, 'instructions_manuals', line)
    return True
