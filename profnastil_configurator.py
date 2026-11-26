"""Параметры четырёх проверенных публичных конфигураторов ПК «Профиль»."""
from decimal import Decimal
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

GUIDE = 'profil-60h27-dlya-gipsokartona-napravlyayushhie-dlya-gipsokartona'
SALE = GUIDE + '-2'
FENCE = 'ograzhdenie-zhalyuzi-ograzhdenie-zhalyuzi'
SHEET = 's8-1150-profnastil'
FIELDS = {
    GUIDE: ('pa_tolshhina',),
    SALE: ('pa_tolshhina', 'pa_razmer'),
    FENCE: ('pa_tolshhina', 'pa_vysota', 'pa_dlinasekczii'),
    SHEET: ('pa_shirinapolnaya',),
}
LABELS = {
    'pa_tolshhina': ('Толщина металла', 'мм'),
    'pa_vysota': ('Высота', 'м'),
    'pa_dlinasekczii': ('Длина секции', 'м'),
    'pa_shirinapolnaya': ('Ширина полная', 'мм'),
    'pa_razmer': ('Размер', 'мм'),
}


def configurator_slug(url):
    parsed = urlsplit(url or '')
    if (parsed.hostname or '').removeprefix('www.') == 'profnastilspb.ru':
        slug = parsed.path.rstrip('/').removeprefix('/product/')
        if parsed.path.startswith('/product/') and slug in FIELDS:
            return slug
    return None


def public_options(form, attribute):
    """Vue отдаёт либо уже отрисованные переключатели, либо их шаблон и модель формы."""
    props = form.select_one('.jb-product__props')
    if not props or props.has_attr('hidden'):
        return []
    if attribute == 'pa_czvet':
        controls = props.select('select#pa_czvet option[value]:not([value=""])')
    else:
        controls = props.select(f'.jb-radio__option input[name="attribute_{attribute}"]')
    if controls:
        return [item['value'] for item in controls if item.get('value')]
    template = props.select_one('.props__item[v-for] .jb-radio__option[v-for]')
    if attribute == 'pa_czvet' and not props.select_one('select#pa_czvet option[v-for]'):
        return []
    field = form.select_one('input#jbVariationsObj[value]')
    if not (template and 'getAllValuesForAttribute(attributeName)' in template['v-for'] and field):
        return []
    try:
        data = json.loads(field['value'])
        if str(data['product_id']) != form.get('data-product_id'):
            return []
        values = data['attributes'][attribute]
        options = values.values() if isinstance(values, dict) else values
        if not isinstance(values, (dict, list)):
            return []
        offered = {r['attributes'].get(attribute) for r in data['variations_map'] if isinstance(r, dict)}
        return [v for v in options if isinstance(v, str) and v in offered]
    except (KeyError, TypeError, AttributeError, ValueError):
        return []


def configuration(soup, slug):
    canonical = soup.select_one('link[rel~=canonical][href]')
    if not canonical or configurator_slug(canonical['href']) != slug:
        return None, {}
    form = soup.select_one('form.variations_form[data-product_id]')
    if not form or not form.select_one('h1.product_title'):
        return None, {}
    specs = {}
    for attribute in FIELDS[slug]:
        values = public_options(form, attribute)
        if values and all(re.fullmatch(r'\d+(?:[_,.]\d+)?', v) for v in values):
            label, unit = LABELS[attribute]
            numbers = [v.replace('_', ',').replace('.', ',') for v in dict.fromkeys(values)]
            specs[label] = ' / '.join(numbers) + ' ' + unit
    return form, specs


def preserve_configurator_markdown(soup):
    canonical = soup.select_one('link[rel~=canonical][href]')
    slug = configurator_slug(canonical['href']) if canonical else None
    if not slug:
        return
    form, specs = configuration(soup, slug)
    tabs = form.select_one('.jb-product__tabs') if form else None
    if tabs and specs and not tabs.select_one('.configurator-options'):
        block = soup.new_tag('div', attrs={'class': 'configurator-options'})
        for label, value in specs.items():
            line = soup.new_tag('p')
            line.string = f'{label}: {value}'
            block.append(line)
        tabs.insert(0, block)


def restore_configurator(result, html, url, manufacturer=''):
    """До классификации восстанавливает проверяемое предложение только по его форме."""
    slug = configurator_slug(url)
    if not slug or not isinstance(result, dict) or result.get('source_rejection'):
        return
    product = result.get('product')
    rejected_fence = slug == FENCE and result == {'error': 'Trash_418#'}
    if not isinstance(product, dict) and not rejected_fence:
        return
    soup = BeautifulSoup(html, 'lxml')
    form, specs = configuration(soup, slug)
    if not specs:
        return
    if rejected_fence:
        description = form.select_one('.jb-product__tabs #description .tabs__text-wrapper')
        if not description or not description.select_one('p') or len(specs) != len(FIELDS[FENCE]):
            return
        title = form.select_one('h1.product_title').get_text(' ', strip=True)
        purpose = form.select_one('.jb-product__tabs #purpose')
        product = {
            'tu': '', 'price': '', 'article': '', 'variants': [],
            'complectation': '', 'compatibility': '', 'advantages': '', 'instructions_manuals': '',
            'exploitation': '', 'storage': '', 'security_measures': '', 'dimensions': '',
            'weight': '', 'additional_info': {'text': ''},
            'product_name': f'{title} ({manufacturer})' if manufacturer else title,
            'description': '\n'.join(p.get_text(' ', strip=True) for p in description.select('p, li')),
            'applications': purpose.get_text(' ', strip=True) if purpose else '',
            'colors': [],
            'images': list(dict.fromkeys(a['href'] for a in form.select('.jb-product__gallery a.gallery__img[href]'))),
        }
        result.clear()
        result['product'] = product
    if slug == FENCE:
        colors = [v.upper().replace('RAL-', 'RAL ', 1) for v in public_options(form, 'pa_czvet')
                  if re.fullmatch(r'ral-\d{4}(?:-\d{4})?', v)]
        price = ''
        field = form.select_one('input#jbVariationsObj[value]')
        if field and form.select_one('.jb-product__calc .prices-item'):
            try:
                model = json.loads(field['value'])
                rows = model['variations_map'] if str(model['product_id']) == form.get('data-product_id') else []
                prices = [str(r.get('sale_price') or r.get('regular_price') or '') for r in rows
                          if isinstance(r, dict) and r.get('is_in_stock') is True]
                amounts = [Decimal(v) for v in prices if re.fullmatch(r'[0-9]+(?:[.][0-9]+)?', v)]
                available = [v for v in amounts if v > 0]
                if available:
                    price = format(min(available), '.2f')
            except (ValueError, TypeError, KeyError):
                pass
        if colors:
            product['colors'] = colors
        if price:
            product['price'] = price
    existing = product.get('specifications')
    if not isinstance(existing, dict):
        existing = {}
    product['specifications'] = {**existing, **specs}
    if slug == SALE:
        crumbs = form.select('.jb-breadcrumbs__container a')
        name = product.get('product_name')
        if isinstance(name, str) and name.strip() and 'распродажа' not in name.casefold() and any(
                a.get_text(strip=True).casefold() == 'распродажа' for a in crumbs):
            product['product_name'] = name + ' (распродажа)'
