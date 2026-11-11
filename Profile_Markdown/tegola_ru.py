"""Карточки Tegola: данные вкладок из сериализованного состояния Nuxt.

Парсер принимает только литералы, ссылки на параметры и присваивания полей.
Код JavaScript, вызовы функций и выражения не исполняются.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

DOMAIN = 'tegola.ru'
NEEDS_RAW_HTML = True
_TOKEN = re.compile(r'\s*("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_$][\w$]*|[{}\[\](),:;.=!])')
_IDENTIFIER = re.compile(r'[A-Za-z_$][\w$]*\Z')


class _Parser:
    """Ограниченная грамматика сериализатора Nuxt, без интерпретатора JS."""

    def __init__(self, tokens, env=None):
        self.tokens = tokens
        self.pos = 0
        self.env = env or {}

    def take(self, expected=None):
        if self.pos >= len(self.tokens):
            raise ValueError('Неожиданный конец состояния Nuxt')
        token = self.tokens[self.pos]
        self.pos += 1
        if expected is not None and token != expected:
            raise ValueError('Неподдерживаемая грамматика Nuxt')
        return token

    def value(self, depth=0):
        if depth > 100:
            raise ValueError('Слишком глубокое состояние Nuxt')
        token = self.take()
        if token == '{':
            result = {}
            while self.tokens[self.pos] != '}':
                key = self.take()
                if key.startswith('"'):
                    key = json.loads(key)
                elif not _IDENTIFIER.fullmatch(key):
                    raise ValueError('Недопустимый ключ')
                self.take(':')
                result[key] = self.value(depth + 1)
                if self.tokens[self.pos] != ',':
                    break
                self.take(',')
            self.take('}')
            return result
        if token == '[':
            result = []
            while self.tokens[self.pos] != ']':
                result.append(self.value(depth + 1))
                if self.tokens[self.pos] != ',':
                    break
                self.take(',')
            self.take(']')
            return result
        if token.startswith('"'):
            return json.loads(token)
        if token in ('null', 'true', 'false'):
            return {'null': None, 'true': True, 'false': False}[token]
        if token == 'void':
            self.take('0')
            return None
        if token == '!':
            number = self.take()
            if number not in ('0', '1'):
                raise ValueError('Недопустимый литерал')
            return number == '0'
        if token[0].isdigit() or token.startswith('-'):
            return json.loads(token)
        if token in self.env:
            return self.env[token]
        raise ValueError('Неизвестная ссылка Nuxt: ' + token)


def _state(source):
    tokens = []
    pos = 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if not match:
            if not source[pos:].strip():
                break
            raise ValueError('Недопустимый токен')
        tokens.append(match.group(1))
        pos = match.end()
    p = _Parser(tokens)
    for token in ('window', '.', '__NUXT__', '=', '(', 'function', '('):
        p.take(token)
    names = []
    while tokens[p.pos] != ')':
        name = p.take()
        if not _IDENTIFIER.fullmatch(name) or name in names:
            raise ValueError('Недопустимый параметр')
        names.append(name)
        if tokens[p.pos] != ',':
            break
        p.take(',')
    p.take(')')
    p.take('{')
    body_start = p.pos
    nesting = 1
    while nesting:
        token = p.take()
        nesting += (token == '{') - (token == '}')
    body_end = p.pos - 1
    # Nuxt: (function(a,...){...}(...));
    p.take('(')
    args = []
    while tokens[p.pos] != ')':
        args.append(p.value())
        if tokens[p.pos] != ',':
            break
        p.take(',')
    for token in (')', ')', ';'):
        p.take(token)
    if p.pos != len(tokens) or len(args) != len(names):
        raise ValueError('Неполное состояние Nuxt')
    body = _Parser(tokens[body_start:body_end], dict(zip(names, args)))
    while body.pos < len(body.tokens):
        name = body.take()
        if name == 'return':
            result = body.value()
            if body.pos < len(body.tokens) and body.tokens[body.pos] == ';':
                body.take(';')
            if body.pos != len(body.tokens):
                raise ValueError('Код после return')
            return result
        target = body.env.get(name)
        if not isinstance(target, dict):
            raise ValueError('Присваивание не объекту')
        body.take('.')
        key = body.take()
        if not _IDENTIFIER.fullmatch(key):
            raise ValueError('Недопустимое поле')
        body.take('=')
        target[key] = body.value()
        body.take(';')
    raise ValueError('Нет return')


def _text(value):
    if not isinstance(value, str):
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for tag in soup.select('script, style, iframe'):
        tag.decompose()
    return soup.get_text(' ', strip=True)


def _url(value, base_url):
    if not isinstance(value, str) or not value.strip():
        return ''
    result = urljoin(base_url, value.strip())
    return result if urlparse(result).scheme in ('http', 'https') else ''


def _product(html, base_url):
    parsed = urlparse(base_url)
    if parsed.hostname not in ('tegola.ru', 'www.tegola.ru') or not re.fullmatch(r'/product/[^/]+/?', parsed.path):
        return None
    try:
        soup = BeautifulSoup(html, 'lxml')
        script = next((tag.get_text() for tag in soup.find_all('script')
                       if tag.get_text().lstrip().startswith('window.__NUXT__=')), None)
        if not script:
            return None
        state = _state(script)
        product = state['data'][0]['product']
        if not isinstance(product, dict) or product.get('slug') != parsed.path.rstrip('/').split('/')[-1]:
            return None
        return product if _text(product.get('title')) else None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
        return None


def _content(value):
    """Текстовые блоки текущего товара, с сохранением строк таблиц и списков."""
    if isinstance(value, dict):
        return '\n\n'.join(filter(None, (_content(value.get(k)) for k in ('title', 'description', 'content'))))
    if not isinstance(value, str):
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for tag in soup.select('script, style, iframe'):
        tag.decompose()
    return markdownify(str(soup), heading_style='ATX').strip()


def _color(variant):
    color = variant.get('color')
    text = _text(color.get('title')) if isinstance(color, dict) else _text(color)
    return '' if text.casefold() in ('цвет', 'color') else text


def extract(html, base_url=''):
    """Возвращает только текущий товар; неизвестный формат отдаёт общему пути."""
    product = _product(html, base_url)
    if product is None:
        return None
    try:
        title = _text(product.get('title'))
        parts = ['# ' + title]
        for field in ('description', 'tab_description', 'info_section', 'seo_title_block', 'seo_text_block'):
            text = _content(product.get(field))
            if text and text not in parts:
                parts.append(text)
        features = []
        for feature in product.get('tab_features') or []:
            label, value = _text(feature.get('title')), _text(feature.get('value'))
            if label and value:
                features.append(f'- {label}: {value}')
        if features:
            parts.append('## Характеристики\n' + '\n'.join(features))
        variants = product.get('variants') or []
        colors = []
        images = []
        for variant in variants:
            color = _color(variant)
            if color and color.casefold() not in {c.casefold() for c in colors}:
                colors.append(color)
            image = _url(variant.get('image'), base_url)
            if image and image not in images:
                images.append(image)
        if colors:
            parts.append('## Цвета\n' + '\n'.join('- ' + color for color in colors))
        if product.get('advantages_show'):
            advantages = []
            for item in product.get('advantages') or []:
                label, text = _text(item.get('title')), _text(item.get('description'))
                value = ': '.join(dict.fromkeys(v for v in (label, text) if v))
                if value:
                    advantages.append('- ' + value)
            if advantages:
                parts.append('## Преимущества\n' + '\n'.join(dict.fromkeys(advantages)))
        for field, heading in (('tab_documents', 'Инструкции и технические листы'),
                               ('tab_nodes', 'Сертификаты'),
                               ('tab_node_exts', 'Полезные материалы')):
            documents = []
            for item in product.get(field) or []:
                link = _url(item.get('link') or item.get('file'), base_url)
                label = _text(item.get('title'))
                if link and label:
                    documents.append(f'- [{label}]({link})')
            if documents:
                parts.append('## ' + heading + '\n' + '\n'.join(dict.fromkeys(documents)))
        main_image = _url(product.get('image'), base_url)
        if main_image and main_image not in images:
            images.insert(0, main_image)
        if images:
            parts.append('## Изображения\n' + '\n'.join(f'![{title}]({link})' for link in images))
        return '\n\n'.join(parts)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
        return None


def _label_key(value):
    text = value.casefold().replace('ё', 'е').replace('²', '2').replace('³', '3')
    text = text.replace('м.кв.', 'м2').replace('м.кв', 'м2').replace('м.куб.', 'м3')
    return re.sub(r'[^а-яa-z0-9]', '', text)


def _put_line(product, field, label, value):
    """Заменяем только строку с тем же именем, не угадываем смысл прозы."""
    lines = str(product.get(field) or '').splitlines()
    key = _label_key(label)
    lines = [line for line in lines if _label_key(line.lstrip('- ').split(':', 1)[0]) != key]
    lines.append(f'- {label}: {value}')
    product[field] = '\n'.join(lines)


def _value_key(value):
    text = value.casefold().replace('м.кв.', 'м2').replace('²', '2').replace('³', '3')
    text = text.replace(',', '.').replace('×', 'x').replace('х', 'x')
    # Десятичный разделитель значим: 0,5 и 05 — разные исходные значения.
    return re.sub(r'[^а-яa-z0-9.]', '', text)


def _features(source):
    rows = list(source.get('tab_features') or [])
    # Принимаем только явную таблицу «Характеристика — Значение», не числовую
    # прозу, не таблицу цен/исполнений и не характеристики материалов схемы.
    for field in ('description', 'tab_description', 'info_section', 'seo_text_block'):
        value = source.get(field)
        if isinstance(value, dict):
            value = '\n'.join(v for k, v in value.items() if k in ('description', 'content') and isinstance(v, str))
        if not isinstance(value, str):
            continue
        for table in BeautifulSoup(value, 'html.parser').find_all('table'):
            cells = [[_text(str(c)) for c in tr.find_all(['td', 'th'], recursive=False)] for tr in table.find_all('tr')]
            if not cells or [_label_key(c) for c in cells[0]] != ['характеристика', 'значение']:
                continue
            for row in cells[1:]:
                if len(row) != 2 or not all(row):
                    continue
                label, text = row
                for existing in rows:
                    old_label = _text(existing.get('title'))
                    if _label_key(old_label.split(',')[0]) != _label_key(label.split(',')[0]):
                        continue
                    old_value = _text(existing.get('value'))
                    unit = old_label.split(',', 1)[1] if ',' in old_label else ''
                    if _value_key(old_value + unit) == _value_key(text) or _value_key(old_value) == _value_key(text):
                        break
                else:
                    rows.append({'title': label, 'value': text})
    return rows


def _remove_private_variant_data(product, source, markdown):
    """Не выпускаем служебные исполнения, даже если они остались в ответе LLM."""
    norm = lambda value: re.sub(r'[^а-яa-z0-9]+', ' ', str(value).casefold().replace('ё', 'е')).strip()
    public_text = re.sub(r'!?\[([^]\n]*)\]\([^\n)]*\)', r'\1', markdown)
    public = ' ' + norm(public_text) + ' '
    visible = lambda value: bool(norm(value)) and (' ' + norm(value) + ' ') in public
    source_titles = {norm(v.get('title')) for v in source.get('variants') or [] if v.get('title')}
    private_titles = [norm(v.get('title')) for v in source.get('variants') or []
                      if v.get('title') and not visible(v['title'])]
    def public_sku(sku):
        return re.search(r'\b(?:артикул(?: товара)?|sku|код товара)\s+' + re.escape(norm(sku)) + r'\b', public)
    private_skus = [str(v['sku']) for v in source.get('variants') or []
                    if v.get('sku') and not public_sku(v['sku'])]

    def clean_specs(obj):
        if not isinstance(obj, dict):
            return
        for key in list(obj):
            value = obj[key]
            key_text = ' ' + norm(key) + ' '
            private_key = any(' ' + title + ' ' in key_text for title in private_titles)
            private_article = ('артикул' in norm(key) or norm(key) == 'sku') and any(
                re.search(r'(?<!\d)' + re.escape(sku) + r'(?!\d)', str(value)) for sku in private_skus)
            invented_heading = norm(key) == 'марка' and norm(value) == 'исполнения' and not visible(value)
            empty_variant = norm(key) in source_titles and isinstance(value, dict) and not any(value.values())
            if private_key or private_article or invented_heading or empty_variant:
                del obj[key]
            elif isinstance(value, dict):
                clean_specs(value)
                if not value:
                    del obj[key]
    clean_specs(product.get('specifications'))
    if str(product.get('article') or '') in private_skus:
        product['article'] = ''

    def public_fields(item):
        if not isinstance(item, dict):
            return {}
        result = {}
        for key, value in item.items():
            if isinstance(value, dict):
                nested = public_fields(value)
                if nested:
                    result[key] = nested
            elif value not in (None, ''):
                text = ' ' + norm(value) + ' '
                private_title = any(' ' + title + ' ' in text for title in private_titles)
                private_sku = any(re.search(r'(?<!\d)' + re.escape(sku) + r'(?!\d)', str(value)) for sku in private_skus)
                repeats_heading = key == 'Модель' and norm(value) == norm(source.get('title', ''))
                if not private_title and not private_sku and not repeats_heading:
                    result[key] = value
        return result
    variants = product.get('variants')
    if isinstance(variants, list):
        product['variants'] = [cleaned for item in variants if (cleaned := public_fields(item))]


def restore_source_product(ai_data, html, url):
    """Восстанавливает точные поля Nuxt после положительного решения LLM.

    Не меняет товарность и имя, не создаёт товары из исполнений. Нижняя проза
    остаётся входом LLM: численные свойства из рекламного текста не угадываются.
    """
    if not isinstance(ai_data, dict) or not isinstance(ai_data.get('product'), dict) or ai_data.get('is_product') is False:
        return []
    source = _product(html, url)
    if source is None:
        return []
    product = ai_data['product']
    specs = product.setdefault('specifications', {})
    if not isinstance(specs, dict):
        raise ValueError('specifications должен быть объектом')
    restored = []
    features = {}
    for feature in _features(source):
        label, value = _text(feature.get('title')), _text(feature.get('value'))
        if not label or not value:
            continue
        values = features.setdefault(label, [])
        if value not in values:
            values.append(value)
    for label, values in features.items():
        # В источнике встречаются разные строки с одним заголовком. Не теряем
        # первое значение и не переименовываем ошибочный заголовок догадкой.
        value = '\n'.join(values)
        key = _label_key(label)
        for existing in list(specs):
            if _label_key(existing) == key:
                del specs[existing]
        if re.search(r'количеств.*(?:палл[её]т|поддон)', label, re.I):
            target = 'complectation'
        elif re.search(r'гаранти', label, re.I):
            target = 'exploitation'
        elif re.search(r'совместим', label, re.I) or label in (
                'Дополнительные материалы для полной гидроизоляции',
                'Рекомендуемый диаметр греющего кабеля', 'Рекомендуемый плиточный клей'):
            target = 'compatibility'
        elif label in ('Шпатель', 'Температура применения, °C', 'Тип применения',
                       'Возможность монтажа на сплошной настил'):
            target = 'instructions_manuals'
        else:
            target = 'specifications'
        if target == 'specifications':
            specs[label] = value
        else:
            _put_line(product, target, label, value)
        restored.append({'field': target, 'key': label, 'value': value})
    # В Nuxt variants — складские записи, а не публичная таблица выбора.
    # Восстанавливаем только подписи цветов; SKU/названия/цены не публикуем.
    colors = []
    for variant in source.get('variants') or []:
        color = _color(variant)
        if color and color.casefold() not in {c.casefold() for c in colors}:
            colors.append(color)
    if colors:
        product['colors'] = colors
    _remove_private_variant_data(product, source, extract(html, url) or '')
    if source.get('advantages_show'):
        lines = str(product.get('advantages') or '').splitlines()
        known = {_label_key(line.lstrip('- ')) for line in lines}
        for item in source.get('advantages') or []:
            title, text = _text(item.get('title')), _text(item.get('description'))
            line = ': '.join(dict.fromkeys(v for v in (title, text) if v))
            if line and _label_key(line) not in known:
                lines.append('- ' + line)
                known.add(_label_key(line))
        if lines:
            product['advantages'] = '\n'.join(lines)
    return restored
