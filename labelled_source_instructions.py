"""Явные условия подготовки и нанесения из пользовательских разделов st-paint.ru."""
import re
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def _key(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    text = re.sub(r'^\s*[-•*]\s*', '', text)
    text = text.replace('–', '-').replace('—', '-').replace('−', '-')
    text = re.sub(r'\s*°\s*[cс]', ' c', text)
    text = re.sub(r'(?<=\d)\s*[cс](?=\W|$)', ' c', text)
    return re.sub(r'\s+', ' ', text).strip().rstrip('.;')


def _append(product, field, line):
    current = product.get(field, '')
    if not isinstance(current, str):
        return False
    if _key(line) not in {_key(x) for x in current.splitlines()}:
        product[field] = (current.rstrip() + '\n- ' + line).lstrip('\n')
    return True


def _remove_exact(product, field, source_facts):
    current = product.get(field)
    if isinstance(current, str):
        product[field] = '\n'.join(line for line in current.splitlines()
                                   if _key(line) not in {_key(s) for s in source_facts})


def restore_labelled_instructions(ai_data, html, page_url):
    """Не меняет отклонённый ответ и не выбирает между противоречиями страницы."""
    if (urlsplit(page_url).hostname or '').lower() not in ('st-paint.ru', 'www.st-paint.ru'):
        return
    product = ai_data.get('product') if isinstance(ai_data, dict) and not ai_data.get('error') else None
    if not isinstance(product, dict):
        return
    soup = BeautifulSoup(html, 'lxml')
    card = soup.select_one('section.ulockd-service-details')
    if card is None or card.find('h1') is None or card.select_one('#description.tab-pane') is None:
        return
    control = card.select_one('[href="#description"][data-toggle="tab"], [role="tab"][aria-controls="description"]')
    from product_interactions import _control_available, panel_section
    if not _control_available(control):
        return
    original = card.select_one('#description.tab-pane')
    panel = panel_section(soup, original, control.get_text(' ', strip=True), 'description')
    original.replace_with(panel)
    facts = []
    heading = ''
    for node in panel.find_all(['h2', 'h3', 'h4', 'p']):
        if not _control_available(node):
            continue
        text = node.get_text(' ', strip=True)
        if node.name != 'p':
            heading = text
            continue
        if not re.search(r'подготовк.*смес|приготовлен.*краск|нанесен|окрашиван|отвержден', heading, re.I):
            continue
        for sentence in re.split(r'(?<=[.!?])\s+', text):
            kind = None
            if re.match(r'Минимальная температура для отверждения\s*:', sentence, re.I):
                kind = 'curing'
            elif re.search(r'(?:нанос|наносят|окрашиван).*температур', sentence, re.I):
                kind = 'application'
            elif (re.match(r'Жизнеспособность\b', sentence, re.I) and re.search(r'смешанн|смес', sentence, re.I)
                  or re.match(r'Состав остается пригодным к использованию\b', sentence, re.I)
                  and re.search(r'с момента приготовления', sentence, re.I)):
                kind = 'potlife'
            if kind and re.search(r'\d', sentence):
                facts.append((kind, heading, sentence))
    curing = []
    potlife = []
    for kind, heading, sentence in facts:
        if kind in ('application', 'curing'):
            if _append(product, 'instructions_manuals', f'По разделу «{heading}»: {sentence}') and kind == 'curing':
                curing.append(sentence)
        else:
            potlife.append((heading, sentence))
    _remove_exact(product, 'exploitation', curing)
    if not potlife:
        return
    specs = product.get('specifications')
    if not isinstance(specs, dict):
        return
    keys = [key for key in specs if re.search(r'жизнеспособност|время жизни|пригодност.*смес', str(key), re.I)]
    if len(keys) > 1:
        return  # Не объединяем разные модельные ТХ без однозначной связи.
    key = keys[0] if keys else 'Жизнеспособность приготовленной смеси'
    value = specs.get(key, '')
    if not isinstance(value, str):
        return
    for heading, sentence in potlife:
        quote = f'По разделу «{heading}»: {sentence}'
        if _key(quote) not in _key(value):
            value = (value.rstrip() + '\n' + quote).lstrip('\n')
    specs[key] = value
    _remove_exact(product, 'storage', [sentence for _, sentence in potlife])
