"""Явно именованные собственные условия из проверенных товарных DOM."""
import re
from copy import deepcopy
import unicodedata
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from product_interactions import _control_available


def _norm(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    text = re.sub(r'\s*[°º˚ᵒ]\s*[cс]', ' c', text)
    return re.sub(r'\s+', ' ', text).strip().rstrip('.;')


def _sentences(text):
    return re.split(r'(?<=[.!?])\s+(?=[А-ЯЁA-Z])', text)


def _append(product, field, fact):
    previous = product.get(field) or ''
    if not isinstance(previous, str):
        return False
    existing = [p.lstrip('-• ') for line in previous.splitlines() for p in _sentences(line)]
    if _norm(fact) in {_norm(p) for p in existing}:
        return False
    product[field] = '\n'.join(p for p in (previous, '- ' + fact) if p)
    return True


def _visible(node):
    return (node is not None and _control_available(node)
            and not node.find_parent(['script', 'template', 'noscript', 'nav', 'footer', 'aside']))


def _public_text(node):
    node = deepcopy(node)
    for child in list(node.select('script, style, template, noscript, [hidden], [style]')):
        if child.name is not None and (child.name in ('script', 'style', 'template', 'noscript')
                                       or not _control_available(child)):
            child.decompose()
    return node.get_text(' ', strip=True)


def restore_source_detail_fields(ai_data, html, page_url):
    """Не выводит additional_info и не принимает отрицательные ответы модели."""
    if not isinstance(ai_data, dict) or ai_data.get('error'):
        return False
    product = ai_data.get('product')
    if not isinstance(product, dict):
        return False
    host = (urlsplit(page_url or '').hostname or '').lower().removeprefix('www.')
    if host not in ('xotpipe.ru', 'ecosgroup.com', 'ventexpress.ru', 'bitumex.org'):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    changed = False
    if host == 'xotpipe.ru':
        owners = [node for node in soup.select('div.product.type-product')
                  if _visible(node.select_one('h1.product_title'))]
        if len(owners) != 1:
            return False
        for node in owners[0].select('.summary .woocommerce-product-details__short-description p'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if re.match(r'^Кольцо имеет лапки\b', sentence, re.I):
                    specs = product.get('specifications')
                    if not isinstance(specs, dict):
                        continue
                    holder = {'Конструкция': specs.get('Конструкция', '')}
                    if _append(holder, 'Конструкция', sentence):
                        specs['Конструкция'] = holder['Конструкция']
                        changed = True
                elif re.match(r'^Рекомендуемая температура (?:теплоносителя|острого пара в трубах)\b', sentence, re.I):
                    changed = _append(product, 'exploitation', sentence) or changed
    elif host == 'bitumex.org':
        main = soup.select_one('body.single-product main.main')
        if (not urlsplit(page_url).path.startswith('/product/') or main is None
                or not _visible(main.select_one('.page-header h1'))):
            return False
        descriptions = main.select('.summary .woocommerce-product-details__short-description')
        if len(descriptions) != 1:
            return False
        for node in descriptions[0].find_all('p'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if re.match(r'^Материал производится под заказ\b', sentence, re.I):
                    changed = _append(product, 'complectation', sentence) or changed
    elif host == 'ventexpress.ru':
        owners = soup.select('div.production-detail')
        if (not urlsplit(page_url).path.startswith('/produkciya/') or len(owners) != 1
                or not _visible(soup.find('h1'))):
            return False
        for node in owners[0].find_all('p'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if (re.match(r'^Изготовление вед[её]тся\b', sentence, re.I)
                        and re.search(r'индивидуальн\w* заказ\w*', sentence, re.I)):
                    changed = _append(product, 'complectation', sentence) or changed
    else:
        main = soup.select_one('main.page.products-list-page')
        if main is None or not _visible(main.select_one('#isDetailPage h1.textbanner__title')):
            return False
        # Собственное короткое назначение опубликовано перед разделами устройства.
        for node in main.select('section.products-advantages .products-advantages__descr'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if re.search(r'\bпредназначен[аоы]?\s+для\s+транспортировки\b', sentence, re.I):
                    changed = _append(product, 'applications', sentence) or changed
        for heading in main.select('h3.contentblock__title'):
            if not _visible(heading) or _norm(heading.get_text(' ', strip=True)) != 'экономическая эффективность':
                continue
            content = heading.find_next_sibling()
            if content is None or 'content__text' not in content.get('class', []) or not _visible(content):
                continue
            for p in content.find_all('p'):
                text = _public_text(p)
                if _visible(p) and re.match(r'^Осадок,\s*обезвоженный до влажности\b', text, re.I):
                    changed = _append(product, 'exploitation', text) or changed
    return changed
