"""Собственные активные разделы товаров НСК/БКМЗ без сжатия исходных абзацев."""
import re
from copy import deepcopy
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

_FIELDS = {'описание': 'construction', 'конструкция': 'construction',
           'состав': 'construction', 'технология изготовления': 'construction',
           'применение': 'applications', 'назначение': 'applications',
           'монтаж': 'instructions_manuals', 'установка': 'instructions_manuals',
           'инструкция по монтажу': 'instructions_manuals',
           'условия хранения': 'storage', 'условия эксплуатации': 'exploitation'}


def _norm(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    return re.sub(r'\s+', ' ', text).strip().strip('-• ').rstrip('.;:')


_INVISIBLE = ['script', 'style', 'template', 'noscript', 'form', 'pre']


def _available(node):
    from product_interactions import _control_available
    return (node is not None and node.name not in _INVISIBLE
            and node.find_parent(_INVISIBLE) is None and _control_available(node))


def _text(node):
    copy = deepcopy(node)
    for hidden in copy.select(','.join(_INVISIBLE)):
        hidden.decompose()
    for child in list(copy.find_all(True)):
        if getattr(child, 'attrs', None) is not None and not _available(child):
            child.decompose()
    indices = copy.find_all(['sup', 'sub'])
    if indices:
        from text_extractor import MarkdownConverter
        converter = MarkdownConverter()
        for index in indices:
            index.replace_with(converter.convert(str(index)))
        copy.smooth()  # Склеиваем H + sub(3) + PO, не добавляя пробелы get_text.
    return ' '.join(copy.get_text(' ', strip=True).split())


def _body(soup, host, page_url):
    heading = soup.find('h1')
    if heading is None:
        return None
    if host == 'npo-stekloplastic.ru':
        root = soup.select_one('.di-page-contents.hentry')
        if root is None or root not in heading.parents or not _available(root):
            return None
        from source_product_tables import source_family_catalog_reason
        if source_family_catalog_reason(str(soup), page_url):
            return None
        return root
    if host == 'nizhegorodsetkabel.ru':
        root = soup.select_one('div.main')
        if root is None or root not in heading.parents:
            return None
        tabs = root.select('.tabs.tabs_good')
        if len(tabs) != 1:
            return None
        controls = tabs[0].select('ul.tabs_caption > li.active')
        panels = tabs[0].select('div.tabs_content.active')
        if len(controls) != 1 or len(panels) != 1:
            return None
        control, panel = controls[0], panels[0]
        content = panel.select_one('div.text')
    else:
        roots = [p for p in heading.parents if 'card' in (p.get('class', []) or [])]
        if not roots:
            return None
        tabs = roots[0].select('.card-tabs')
        if len(tabs) != 1:
            return None
        controls = tabs[0].select('.card-tabs-nav > .card-tabs-nav-item.active')
        panels = tabs[0].select('.card-tabs-content.active')
        if len(controls) != 1 or len(panels) != 1:
            return None
        control, panel = controls[0], panels[0]
        content = panel.select_one('.card-desc')
    if (_norm(_text(control)) != 'описание'
            or content is None or not _available(control) or not _available(content)):
        return None
    return content


def restore_visible_product_sections(ai_data, html, page_url):
    """Добавляет только видимые собственные p; не читает additional_info и документы."""
    parts = urlsplit(page_url)
    host = (parts.hostname or '').lower().removeprefix('www.')
    prefixes = {'nizhegorodsetkabel.ru': '/catalog/', 'bkmz.ru': '/catalog/',
                'npo-stekloplastic.ru': '/products/'}
    if host not in prefixes or not parts.path.startswith(prefixes[host]):
        return
    if parts.fragment.startswith('source-family='):
        return  # Состав отдельного изолированного семейства восстанавливает его parser.

    product = ai_data.get('product') if isinstance(ai_data, dict) and not ai_data.get('error') else None
    if not isinstance(product, dict):
        return
    soup = BeautifulSoup(html, 'lxml')
    content = _body(soup, host, page_url)
    if content is None:
        return
    field, label = 'construction', 'Описание'
    if host == 'npo-stekloplastic.ru':
        label = _text(content.find('h1'))
    named_section = False
    for node in content.find_all(['h2', 'h3', 'h4', 'b', 'strong', 'p']):
        if not _available(node):
            continue
        text = _text(node)
        mapped = _FIELDS.get(_norm(text))
        if host == 'npo-stekloplastic.ru':
            if node.find_parent('table') is not None:
                continue
            if node.name in ('h2', 'h3', 'h4'):
                named_section = bool(mapped)
                if mapped:
                    field, label = mapped, text.rstrip(':')
                continue
            widget = node.find_parent(class_='elementor-widget-text-editor')
            if widget is None or widget.get('data-widget_type') != 'text-editor.default':
                continue
            if mapped:
                named_section = True
            elif not named_section:
                # Неподписанный случайный p не является технологией/составом.
                label = _text(content.find('h1'))
                if re.search(r'предназначен\w*\s+для|применя[ею]тся|использу[ею]тся\s+(?:для|в качестве)', text, re.I):
                    field = 'applications'
                elif re.search(r'изготавлива|вырабатыва|получа[ею]т|получается|состоит|состоящ|представля\w*\s+собой', text, re.I):
                    field = 'construction'
                else:
                    continue
        if mapped:
            field, label = mapped, text.rstrip(':')
            continue
        if node.name != 'p' or not text:
            continue
        # Список ссылок на документы не является содержимым инструкции.
        if node.find('a') and _norm(text) == _norm(' '.join(a.get_text(' ', strip=True) for a in node.find_all('a'))):
            continue
        fragments = [(field, text)]
        if host == 'bkmz.ru':
            sentences = re.split(r'(?<=[.!?])\s+', text)
            install = [sentence for sentence in sentences
                       if re.match(r'^На каждый кот[её]л устанавливается\b', sentence, re.I)]
            if install:
                rest = ' '.join(sentence for sentence in sentences if sentence not in install)
                fragments = [(field, rest), ('instructions_manuals', ' '.join(install))]
        for target, fragment in fragments:
            obj = product
            if target == 'construction':
                obj = product.get('specifications')
                if not isinstance(obj, dict):
                    continue
                target = ('Состав и свойства материала' if host == 'npo-stekloplastic.ru' else 'Конструкция')
            current = obj.get(target, '')
            if not fragment or not isinstance(current, str) or _norm(fragment) in _norm(current):
                continue
            obj[target] = (current.rstrip() + f'\n\n{label}: {fragment}').lstrip('\n')
