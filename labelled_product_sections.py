"""Собственные озаглавленные разделы устройства из проверенного DOM ЭКОС."""
import re
from copy import deepcopy
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from product_interactions import _control_available


def _norm(value):
    return re.sub(r'\s+', ' ', str(value)).casefold().strip()


def _public_text(node):
    node = deepcopy(node)
    for child in list(node.select('script, style, template, noscript, [hidden], [style]')):
        if child.name is not None and (child.name in ('script', 'style', 'template', 'noscript')
                                       or not _control_available(child)):
            child.decompose()
    return node.get_text(' ', strip=True)


def restore_labelled_product_sections(ai_data, html, page_url):
    """Только положительный ответ; произвольные p и additional_info не читаются."""
    host = (urlsplit(page_url or '').hostname or '').lower().removeprefix('www.')
    if host != 'ecosgroup.com' or not isinstance(ai_data, dict) or ai_data.get('error'):
        return False
    product = ai_data.get('product')
    if not isinstance(product, dict) or not isinstance(product.get('specifications'), dict):
        return False
    specs = product['specifications']
    previous = specs.get('Конструкция', '')
    if not isinstance(previous, str):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main.page.products-list-page')
    if main is None or main.select_one('#isDetailPage h1.textbanner__title') is None:
        return False
    parts = []
    for heading in main.select('h2.textproductstype__title, h3.contentblock__title'):
        label = _norm(_public_text(heading))
        if label not in ('принцип работы', 'конструктивные особенности'):
            continue
        if (not _control_available(heading)
                or heading.find_parent(['script', 'template', 'noscript', 'nav', 'footer', 'aside'])):
            continue
        content = heading.find_next_sibling()
        required_class = 'textproductstype__descr' if label == 'принцип работы' else 'content__text'
        if (content is None or required_class not in content.get('class', [])
                or not _control_available(content)):
            continue
        paragraphs = [_public_text(p) for p in content.find_all(['p', 'li'])
                      if _control_available(p) and not p.find_parent(['script', 'template', 'noscript', 'table'])]
        text = '\n'.join(p for p in paragraphs if p)
        if text:
            parts.append(_public_text(heading) + ':\n' + text)
    if not parts:
        return False
    for part in parts:
        if _norm(part) not in _norm(previous):
            previous = '\n'.join(v for v in (previous, part) if v)
    if previous == specs.get('Конструкция', ''):
        return False
    specs['Конструкция'] = previous
    return True
