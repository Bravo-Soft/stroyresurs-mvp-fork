"""Явно именованные собственные условия из проверенных товарных DOM."""
import re
from copy import deepcopy
import unicodedata
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
from product_interactions import _control_available, panel_section


# Подписи этих публичных пиктограмм проверены по снимку НФЛ 13.11.2026.
# Остальные изображения (чертежи, логотипы, условные знаки) не интерпретируем.
_NFL_ICONS = {
    '50hz.png': ('Частота сети', '50 Гц'),
    '380.png': ('Номинальное напряжение', '380 В'),
    '220_1.png': ('Номинальное напряжение', '220 В'),
    '095.png': ('Коэффициент мощности cos φ', '0,95'),
    'U1.png': ('Климатическое исполнение', 'У1'),
    'IP65.png': ('Степень защиты', 'IP65'),
    '12_year.png': ('Срок службы', '12 лет'),
    'LED.png': ('Источник света', 'LED'),
}


def visible_nfl_icons(soup, page_url):
    if (urlsplit(page_url or '').hostname or '').removeprefix('www.') != 'nppnfl.ru':
        return {}
    facts = {}
    for node in soup.select('#tab-description img[src]'):
        parsed = urlsplit(urljoin(page_url, node['src']))
        path = parsed.path
        if (_visible(node) and (parsed.hostname or '').removeprefix('www.') == 'nppnfl.ru'
                and path.startswith('/image/catalog/znak/')):
            fact = _NFL_ICONS.get(path.rsplit('/', 1)[-1])
            if fact:
                facts[fact[0]] = fact[1]
    return facts


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
    existing = [p.lstrip('-• ') for line in previous.splitlines()
                for p in [line, *_sentences(line)]]
    if _norm(fact) in {_norm(p) for p in existing}:
        return False
    product[field] = '\n'.join(p for p in (previous, '- ' + fact) if p)
    return True


def _visible(node):
    return (node is not None and _control_available(node)
            and not node.find_parent(['script', 'template', 'noscript', 'nav', 'footer', 'aside']))


def visible_ryazan_images(soup, page_url):
    """Only rendered CSS product photos; do not promote metadata/lazy URLs."""
    parsed = urlsplit(page_url)
    if (parsed.hostname or '').removeprefix('www.') != 'ryazanbrick.ru' or '/tproduct/' not in parsed.path:
        return []
    images = []
    for node in soup.select('.js-store-product .js-product-img'):
        if not _visible(node) or any(n.get('aria-hidden') == 'true'
                for n in [node, *node.parents] if getattr(n, 'attrs', None) is not None):
            continue
        match = re.search(r'background-image\s*:\s*url\([\s\'"]*(.*?)[\s\'"]*\)', node.get('style', ''), re.I)
        if not match:
            continue
        value = urljoin(page_url, match[1])
        target = urlsplit(value)
        if (target.scheme in ('http', 'https') and target.hostname == 'static.tildacdn.com'
                and re.search(r'\.(?:jpe?g|png|webp)$', target.path, re.I) and value not in images):
            images.append(value)
    return images


def _public_text(node):
    node = deepcopy(node)
    for child in list(node.select('script, style, template, noscript, [hidden], [style]')):
        if child.name is not None and (child.name in ('script', 'style', 'template', 'noscript')
                                       or not _control_available(child)):
            child.decompose()
    return node.get_text(' ', strip=True)


def _owned_description(product, soup, selectors):
    """Полные собственные абзацы с проверенных публичных панелей.

    Сохраняет условия и отрицания; не восстанавливает отрицательный ответ
    модели, не дополняет числовые ТХ и не переносит денежные суммы в DOCX.
    """
    values=[]
    for root in soup.select(selectors):
        if not _visible(root): continue
        own=deepcopy(root)
        for node in list(own.select('table,nav,form,button,input,select,textarea')):
            if node.name is not None:node.decompose()
        for node in list(own.select('p,li')):
            if node.name is not None and re.search(r'\d[\d\s.,]*\s*(?:₽|руб|евро|доллар)',_public_text(node),re.I):node.decompose()
        value=_public_text(own).replace('\xad','')
        if value and value not in values:values.append(value)
    if not values:return False
    value='\n\n'.join(values)
    changed=product.get('description')!=value
    product['description']=value
    return changed


def _polikor_powder_tables(soup):
    """Три опубликованные группы; общая ячейка примесей не делится на четыре."""
    result = {}
    groups = {'Марка микропорошка': 'Корундовый порошок шлифовальный',
              'Заполнитель': 'Заполнитель', 'Мертель': 'Мертель'}
    for table in soup.select('#content-tab4 table'):
        if not _visible(table):
            continue
        rows = table.find_all('tr')
        if len(rows) < 3:
            continue
        heading = rows[0].find_all('td', recursive=False)
        if not heading or _public_text(heading[-1]) not in groups:
            continue
        group = groups[_public_text(heading[-1])]
        marks = [_public_text(c) for c in rows[1].find_all('td', recursive=False)]
        grades = {mark: {} for mark in marks}
        for index, row in enumerate(rows[2:], 2):
            cells = row.find_all('td', recursive=False)
            if len(cells) < 3 or not _visible(row):
                continue
            label, unit = map(_public_text, cells[:2])
            for mark, cell in zip(marks, cells[2:]):
                value = _public_text(cell)
                if value == '-':
                    continue
                if cell.get('rowspan') == '4' and mark == 'КП':
                    labels = [_public_text(r.find('td')) for r in rows[index:index + 4]]
                    key = ' / '.join(labels) + ' (общая ячейка таблицы), ' + unit
                    grades[mark][key] = value
                elif label.startswith(('Распределение зерна по фракциям:', 'Зерновой состав:')):
                    labels = [_public_text(p) for p in cells[0].find_all('p')]
                    units = [_public_text(p) for p in cells[1].find_all('p')]
                    values = [_public_text(p) for p in cell.find_all('p')]
                    prefix = label.split(':', 1)[0] + ': '
                    if labels[0] == 'Зерновой состав:':
                        labels = labels[1:]
                    if len(labels) == len(values):
                        for i, (part, val) in enumerate(zip(labels, values)):
                            if val != '-':
                                key = prefix + part.removeprefix(prefix).strip() + ', ' + (units[i] if len(units) == len(labels) else unit)
                                grades[mark][key] = val
                else:
                    grades[mark][label + ', ' + unit] = value
        result[group] = grades
    return result if set(result) == set(groups.values()) else {}


def _restore_metalloinvest(ai_data, html, page_url):
    """Literal public matrices; no PDF, inferred composition or model-error override."""
    from product_interactions import preserve_product_panels
    from site_profiles.markdown_validation import html_table
    soup = BeautifulSoup(html, 'html.parser')
    preserve_product_panels(soup, page_url)
    root = soup.select_one('#catalog-offer-id')
    if root is None:
        return False
    product = ai_data['product']
    # Replace lossy model matrices with complete source matrices; scalar fields
    # (dimensions selected by the model from the public configurator) remain.
    specs = {k: v for k, v in (product.get('specifications') or {}).items()
             if not isinstance(v, (dict, list))}
    layouts = []
    manufacturer = ''
    def text(node):
        return re.sub(r'\s+', ' ', _public_text(node)).strip()
    def pair(key, value):
        nonlocal manufacturer
        if not value:
            return
        if key == 'Производитель':
            manufacturer = value
        if key.startswith(('Отрасль применения', 'Область применения')):
            _append(product, 'applications', value)
        elif key.startswith('Минимальный объем заказа'):
            _append(product, 'complectation', key + ': ' + value)
        elif key.startswith(('Способ', 'Условия хранения', 'Срок хранения')) or (key == 'Примечание' and 'ГБЖ' in value):
            _append(product, 'storage', key + ': ' + value)
        else:
            specs[key] = value
    for number, table in enumerate(root.select('table'), 1):
        if not _visible(table) or table.find('table'):
            continue
        rows = table.find_all('tr')
        thead = table.find('thead', recursive=False)
        raw = [r.find_all(['td','th'], recursive=False) for r in rows]
        if thead is None and all(len(cells)==2 for cells in raw):
            for cells in raw:
                pair(text(cells[0]), text(cells[1]))
            continue
        clone = deepcopy(table)
        if thead is not None:
            # Some public header cells use td. Their membership in thead,
            # rather than their tag name, defines the published header.
            for cell in clone.select('thead td'):
                cell.name = 'th'
            first_body = clone.select_one('tbody tr')
            if first_body is not None:
                cells = first_body.find_all(['td','th'],recursive=False)
                if cells and not text(cells[0]) and any('Повышенная' in text(c) for c in cells):
                    for c in cells:c.name='th'
                    clone.thead.append(first_body.extract())
            data=html_table(clone)
            headers, values = data['headers'], data['rows']
            title=data['title']
        else:
            # Variant matrix: rows before explicit "Продукция" are metadata.
            index=next((i for i,c in enumerate(raw) if c and text(c[0])=='Продукция'),None)
            if index is None:
                continue
            for cells in raw[:index]:
                if len(cells)==2:pair(text(cells[0]),text(cells[1]))
            for row in list(clone.select('tr'))[:index]:row.decompose()
            first=clone.find('tr')
            for c in first.find_all(['td','th'],recursive=False):c.name='th'
            data=html_table(clone);headers,values=data['headers'],data['rows'];title=''
        if not values or len(headers)<2:
            continue
        # All-empty overflow columns arise from published colspan values;
        # retain every nonempty cell and its corresponding header.
        keep=[j for j in range(len(headers)) if headers[j] or any(j<len(r) and r[j] for r in values)]
        headers=[' / '.join(p.strip() for p in headers[j].split(' / ') if p.strip()) for j in keep];values=[[r[j] if j<len(r) else '' for j in keep] for r in values]
        if not title:
            panel=table.find_parent(attrs={'data-product-panel':True})
            modal=table.find_parent(class_='modal')
            collapse=table.find_parent(class_='collapse')
            if collapse and collapse.get('id'):
                ctrl=root.select_one('[data-target="#'+collapse['id']+'"], a[href="#'+collapse['id']+'"]')
                title='Химический состав — '+text(ctrl) if ctrl else ''
            if not title and (panel or modal):
                heading=(panel or modal).find(['h2','h3','h4','h5'])
                if heading:title=text(heading)
            if not title and 'марка стали' in headers[0].casefold():
                title='Химический состав'
            if not title:
                heading=table.find_previous(['h2','h3','h4','h5'])
                title=text(heading) if heading else 'Характеристики'
        note_rows=[row for row in values if len(row[0])>50 and all(not v or v==row[0] for v in row[1:])]
        data['notes'] += [row[0] for row in note_rows]
        values=[row for row in values if row not in note_rows]
        # Zero-height empty HTML rows may repeat the preceding rowspan cells.
        values=[row for i,row in enumerate(values) if not i or row!=values[i-1]]
        path=title+' / таблица '+str(number)
        # Labels may repeat; storage keys are unique while displayed labels
        # preserve the literal complete multilevel source headers.
        columns=[]
        for i,label in enumerate(headers[1:],1):
            key=label or 'Столбец '+str(i)
            if key in columns:key+=' / столбец '+str(i)
            columns.append(key)
        records={}
        for i,row in enumerate(values,1):
            key=row[0] or 'Строка '+str(i)
            if key in records:key+=' / строка '+str(i)
            records[key]=dict(zip(columns,row[1:]))
        if title.startswith('Заказ, доставка, хранение'):
            for label,record in records.items():
                for column,value in record.items():
                    if value:_append(product,'storage',column+' — '+label+': '+value)
            continue
        specs[path]=records
        layouts.append(dict(path=path,title=title,row_header=headers[0] or 'Характеристика',columns=columns,
                            labels=headers[1:],column_axis='Опубликованные параметры',notes=data['notes']))
    # The public radio labels are offered alternatives, not a fabricated
    # combination or a single selected variant. Ignore raw input attributes.
    for group in root.select('.form-group'):
        heading=group.select_one('.form-group-title')
        if heading is None or not _visible(group):
            continue
        heading=deepcopy(heading)
        for tip in heading.select('.tooltip-mark,[data-public-tooltip-text]'):tip.decompose()
        label=text(heading)
        options=list(dict.fromkeys(text(n) for n in group.select('.btn-group label')
                                  if n.select_one('input[type="radio"]') and _visible(n) and text(n)))
        if label and options:
            specs['Варианты «'+label+'» в публичном конфигураторе']='; '.join(options)
    for tip in root.select('[data-public-tooltip-text]'):
        if _visible(tip) and text(tip):_append(product,'compatibility',text(tip))
    length_control=root.select_one('a[href="#modal-table"]')
    length_panel=soup.select_one('#modal-table')
    if length_control is not None and _visible(length_control) and length_panel is not None:
        definitions=[]
        for row in length_panel.select('.table-body .table-row'):
            cells=row.select('.table-col')
            if len(cells)==2:definitions.append(text(cells[0])+': '+text(cells[1]))
        if definitions:specs['Обозначения видов длины']='; '.join(definitions)
    heading=root.find('h1')
    intro=heading.find_next_sibling(class_='text-border-top') if heading else None
    if intro is not None and _visible(intro):
        paragraphs=[text(n) for n in intro.find_all('p',recursive=False) if _visible(n) and text(n)]
        if paragraphs:product['description']='\n\n'.join(paragraphs)
    product['specifications']=specs
    ai_data['_table_layouts']=layouts
    if manufacturer:
        product['manufacturer']=manufacturer
        name=product.get('product_name','')
        if name:
            # Company supplied for the job may be another group enterprise.
            # The source's explicit manufacturer takes precedence.
            name=re.sub(r'\s*\([^()]*\)\s*$', '',name)
            product['product_name']=name+' ('+manufacturer+')'
    images=[]
    for anchor in root.select('a.slide-img.js-gallery-link[href]'):
        if _visible(anchor) and 'background-image' in anchor.get('style',''):
            url=urljoin(page_url,anchor['href'])
            if urlsplit(url).hostname=='products.metalloinvest.com' and re.search(r'\.(?:jpe?g|png|webp)$',url,re.I):images.append(url)
    if images:product['images']=list(dict.fromkeys(images))
    for heading in root.select('h4'):
        if text(heading)=='Упаковка и маркировка':
            block=heading.find_next_sibling()
            if block:
                for row in block.select('.table__row'):
                    label=row.select_one('.table__left');right=row.select_one('.table__right .table-text > div')
                    if not label or not right:continue
                    label_text=text(label)
                    for item in right.select('li') or [right]:
                        _append(product,'complectation',label_text+': '+text(item))
    for node in root.select('i,em'):
        val=text(node)
        if val.startswith('Окончательные размеры и характеристики согласовываются'):_append(product,'compatibility',val)
    return True


def _gazosilicat_tables(product, html, url):
    soup = BeautifulSoup(html, 'lxml')
    physical = soup.select_one('.product-characteristics__table table')
    packing = soup.select_one('.product-characteristics table.responsive-table-new')
    if physical is None or packing is None:
        return False
    properties = {}
    for row in physical.select('tr'):
        cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'], recursive=False)]
        if len(cells) != 4:
            continue
        if not properties:
            if cells[0] != 'Марка по средней плотности' or not all(re.fullmatch(r'D[0-9]+', g) for g in cells[1:]):
                return False
            properties = {g: {cells[0]: g} for g in cells[1:]}
        else:
            for record, val in zip(properties.values(), cells[1:], strict=True):
                record[cells[0]] = val
    rows = packing.select('tr')
    if not properties or len(rows) < 3:
        return False
    heads = [c.get_text(' ', strip=True) for c in rows[0].find_all(['th','td'], recursive=False)]
    units = [c.get_text(' ', strip=True) for c in rows[1].find_all(['th','td'], recursive=False)]
    if len(heads) != 6 or len(units) != 2 or 'брутто' not in heads[-1] or heads[4] != 'Кол-во':
        return False
    headers = heads[1:4] + [heads[4]+' '+unit for unit in units] + heads[5:]
    packing_data = {}
    for row in rows[2:]:
        values = [c.get_text(' ', strip=True) for c in row.select('td.responsive-table-new__number')]
        if len(values) != 6:
            return False
        label = ' / '.join(values[:4])
        if label in packing_data:
            return False
        packing_data[label] = dict(zip(headers, values, strict=True))
    _owned_description(product, soup, '.product-characteristics .text-characteristic__text')
    if product.get('description'):
        product['description'] = product['description'].replace('°', ' град. ')
    product['specifications'] = {'Физико-технические свойства': properties, heads[0]: packing_data}
    pictures = []
    for img in soup.select('#product-blocks-slider img[src]'):
        addr = urljoin(url, img['src'])
        if urlsplit(addr).hostname == urlsplit(url).hostname and addr not in pictures:
            pictures.append(addr)
    if pictures:
        product['images'] = pictures
    return True


def _demidovo_plywood(product, html, url):
    from inline_products import isolate_inline_product
    isolated = isolate_inline_product(html, url)
    if not isolated:
        return False
    soup = BeautifulSoup(isolated, 'lxml')
    tables = soup.select('main[data-source-family] table.sertif')
    if len(tables) != 1:
        return False
    specs = {}
    images = []
    for row in tables[0].select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 2:
            return False
        key, value = [c.get_text(' ', strip=True) for c in cells]
        if key == 'ФОТО':
            for img in cells[1].select('img[src]'):
                image = urljoin(url, img['src'])
                if urlsplit(image).hostname == urlsplit(url).hostname and image not in images:
                    images.append(image)
        elif key and value:
            specs[key] = specs[key] + '; ' + value if key in specs else value
    if not specs or 'Толщина, мм' not in specs:
        return False
    product['specifications'] = specs
    product['images'] = images
    return True


def _fubag_details(product, html, url):
    soup = BeautifulSoup(html, 'lxml')
    rows = soup.select('.ordered-block.char .product-details-card__spec-row')
    facts = {}
    for row in rows:
        label, datum = row.select_one('.product-details-card__spec-name'), row.select_one('.product-details-card__spec-value')
        if not label or not datum or not _visible(row):
            continue
        key, val = _public_text(label), _public_text(datum)
        if key in facts or re.search(r'цен[аы]|стоимость', key, re.I):
            return False
        if key and val:
            facts[key.replace('°', ' град. ')] = val.replace('°', ' град. ')
    if not facts:
        return False
    product['specifications'] = facts
    photos = []
    for item in soup.select('.product-container .gallery__item--thumb[data-big]:has(img[src])'):
        photo = urljoin(url, item['data-big'])
        if urlsplit(photo).hostname == urlsplit(url).hostname and urlsplit(photo).path.startswith('/upload/') and photo not in photos:
            photos.append(photo)
    if photos:
        product['images'] = photos
    _owned_description(product, soup, '.ordered-block.desc .content')
    return True


def _intersigma_pump_tables(product, html):
    soup = BeautifulSoup(html, 'lxml')
    tables = soup.select('body > table')
    if len(tables) != 3:
        return False
    facts = {}
    for table in tables:
        title = next((_public_text(p) for p in table.find_previous_siblings('p') if _public_text(p)), '')
        rows = [[_public_text(td) for td in tr.find_all(['td','th'], recursive=False)] for tr in table.select('tr')]
        if not title or not rows or rows[0][:2] != ['Размер', ''] or any(len(row) != len(rows[0]) for row in rows):
            return False
        values = {}
        for number, (label, unit, *cells) in enumerate(rows[1:], 1):
            key = ', '.join(v for v in (label, unit) if v)
            if key in values:
                key += f' (строка {number})'
            values[key] = {size: val for size,val in zip(rows[0][2:], cells, strict=True) if val}
        facts[title] = values
    product['specifications'] = facts
    paragraphs = [_public_text(p).replace('°', ' град. ').replace('\xad', '') for p in soup.select('body > p')
                  if _visible(p) and _public_text(p) and not p.find('a')]
    product['description'] = '\n\n'.join(paragraphs)
    return True


def _omsk_materials(product, html, url):
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('main .wpb-content-wrapper')
    if main is None:
        return False
    tables = main.select('table')
    if len(tables) != 1:
        return False
    def cell_text(cell):
        copied = deepcopy(cell)
        for sup in copied.select('sup'):
            sup.replace_with('^' + sup.get_text(strip=True))
        return re.sub(r'\s+', ' ', copied.get_text(strip=False)).strip().replace('°', ' град. ')
    matrix = [[cell_text(c) for c in row.find_all(['td','th'], recursive=False)] for row in tables[0].select('tr')]
    if len(matrix) < 2 or len(matrix[0]) < 3 or any(len(row) != len(matrix[0]) for row in matrix):
        return False
    groups = {}
    photos = []
    prices = []
    for heading in main.select('h3'):
        name = heading.get_text(' ', strip=True)
        if not re.fullmatch(r'ГБ-\d+|ИЗОР|ИЗУР', name):
            continue
        column = heading.find_parent(class_='wpb_column')
        lines = column.get_text('\n', strip=True).splitlines() if column else []
        if len(lines) < 3 or name in groups or lines[0] != name:
            return False
        amount = re.fullmatch(r'([\d ,]+)\s*[рР]/шт', lines[1])
        if not amount:
            return False
        prices.append(float(amount[1].replace(' ', '').replace(',', '.')))
        groups[name] = ' / '.join(lines[2:]).replace('³', '^3')
        for img in column.select('img[src]'):
            photo = urljoin(url,img['src'])
            if urlsplit(photo).hostname == urlsplit(url).hostname and photo not in photos:
                photos.append(photo)
    if not groups or not photos:
        return False
    values = {}
    for row in matrix[1:]:
        if not row[0] or row[0] in values:
            return False
        values[row[0]] = dict(zip(matrix[0][1:],row[1:],strict=True))
    product['specifications'] = {matrix[0][0]:values, 'Исполнения и упаковка':groups}
    product['price'] = f'{min(prices):.2f}'
    product['images'] = photos
    return True


def _abac_applications(product, html):
    soup = BeautifulSoup(html, 'lxml')
    table = soup.select_one('#descript .tables table')
    if table is None:
        return False
    rows = [[cell.get_text(' ', strip=True) for cell in row.find_all('td', recursive=False)]
            for row in table.select('tr')]
    if (len(rows) < 3 or rows[0][:2] != ['Типовое применение', 'Коалесцентные фильтры']
            or len(rows[0]) != 3 or rows[1] != ['Твердые частицы', 'Масла']
            or any(len(row) != 4 for row in rows[2:])):
        return False
    condition = table.parent.find('p', recursive=False)
    if any('(*)' in val for row in rows for val in row) and not condition:
        return False
    values = [f'- {sector}: {rows[0][1]} {filters}, {rows[0][2]}, '
              f'{rows[1][0]} {particles}, {rows[1][1]} {oil}.'
              for sector, filters, particles, oil in rows[2:]]
    if condition:
        values.append(condition.get_text(' ', strip=True))
    product['applications'] = '\n'.join(values).replace('(*)', '(1)')
    return True


def _rothenberger_properties(product, html):
    soup = BeautifulSoup(html, 'lxml')
    tables = soup.select('table.props_list:has(td.char_value)')
    if len(tables) != 1 or not isinstance(product.get('specifications'), dict):
        return False
    properties = {}
    for row in tables[0].select('tr'):
        key, value = row.select_one('td.char_name'), row.select_one('td.char_value')
        if not key or not value:
            return False
        label = key.get_text(' ', strip=True)
        datum = value.get_text(' ', strip=True)
        if not datum or label in properties or re.search(r'цен[аы]|стоимость', label, re.I):
            return False
        properties[label] = datum
    product['specifications'].update(properties)
    return bool(properties)


def _haier_specs(product, html, url):
    soup = BeautifulSoup(html, 'lxml')
    tabs = soup.select_one('.details__tabs:has(button[data-tabs-btn="tab_description"]):has(button[data-tabs-btn="tab_attribute"])')
    if not tabs or not soup.select_one('.sku #product h1'):
        return False
    table = tabs.select_one('[data-tabs-content="tab_description"] table.table-desc-haier')
    if not table:
        return False
    def value(node):
        from product_utils import apply_cp1251_replacements
        text = node.get_text(' ', strip=True)
        return apply_cp1251_replacements(text).replace('°', '').replace('м²', 'м^2').replace('м³', 'м^3')

    facts = {}
    seen = set()
    section = ''
    for row in table.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) == 1 and cells[0].find('h3'):
            section = value(cells[0])
            continue
        if len(cells) != 2 or not section or 'td-name-haier' not in cells[0].get('class', []):
            return False
        key, datum = map(value, cells)
        if not key or not datum or key in facts.setdefault(section, {}):
            return False
        facts[section][key] = datum
        seen.add((key, datum))
    if not facts:
        return False
    for row in tabs.select('.details__attributes .details-spec__item'):
        label, field = row.select_one('dt'), row.select_one('dd')
        if label and field and (value(label), value(field)) not in seen:
            facts.setdefault('Параметры вкладки характеристик', {})[value(label)] = value(field)
    product['specifications'] = facts
    blocks = ['Внутренний блок', 'Наружный блок']
    if all(block in facts for block in blocks) and not re.search(
            r'комплект(?:ация| поставки)', tabs.get_text(' ', strip=True), re.I):
        product['complectation'] = '\n'.join('- ' + block for block in blocks)
    gallery = []
    for link in soup.select('.sku__gallery a.sku__slides-item[href]:has(img)'):
        image = urljoin(url, link['href'])
        target = urlsplit(image)
        if target.scheme in ('http','https') and target.hostname == urlsplit(url).hostname and target.path.startswith('/image/') and image not in gallery:
            gallery.append(image)
    if gallery:
        product['images'] = gallery
    model_code = soup.select_one('.sku__panel .sku__id-value')
    if model_code:
        product['article'] = value(model_code)
    summary = tabs.select('[data-tabs-content="tab_description"] .div-desc-haier li')
    if summary:
        product['advantages'] = '\n'.join('- ' + value(item) for item in summary)
    return True


def _volson_model_columns(product, html):
    from product_tables import _grid, cell_text
    from text_extractor import MarkdownConverter
    converter = MarkdownConverter()
    soup = BeautifulSoup(html, 'lxml')
    tables = soup.select('div.technicals table')
    if not tables:
        return False
    def text(cell):
        return cell_text(cell, converter).replace('°', '').strip() if cell else ''
    _, grid = _grid(tables[0], converter)
    if not grid or text(grid[0][0]).casefold() != 'модель':
        return False
    names = [text(cell).replace('\n', ' ') for cell in grid[0][1:]]
    if not all(names) or len(set(names)) != len(names):
        return False
    specs = {name: {} for name in names}
    for row in grid[1:]:
        label = text(row[0])
        if not label or any(cell is None for cell in row):
            return False
        for name, cell in zip(names, row[1:], strict=True):
            if label in specs[name]:
                return False
            specs[name][label] = text(cell)
    if len(tables) == 2:
        _, rows = _grid(tables[1], converter)
        if len(rows) < 3 or [text(cell) for cell in rows[1][2:]] != names:
            return False
        matrix = {}
        for row in rows[2:]:
            height, area, *values = [text(cell) for cell in row]
            if not height or height in matrix or not area or not all(values):
                return False
            matrix[height] = {text(rows[0][1]): area, **dict(zip(names, values, strict=True))}
        specs[text(rows[0][0])] = matrix
    elif len(tables) != 1:
        return False
    changed = product.get('specifications') != specs
    product['specifications'] = specs
    return changed


def restore_source_detail_fields(ai_data, html, page_url):
    """Не выводит additional_info и не принимает отрицательные ответы модели."""
    if not isinstance(ai_data, dict) or ai_data.get('error'):
        return False
    product = ai_data.get('product')
    if not isinstance(product, dict):
        return False
    host = (urlsplit(page_url or '').hostname or '').lower().removeprefix('www.')
    if host == 'po-korf.ru' and urlsplit(page_url).path.startswith('/equipment/'):
        soup = BeautifulSoup(html, 'lxml')
        images = []
        for node in soup.select('.equipment-catalog__single-image__block[data-full]'):
            path = node['data-full']
            if (_visible(node) and path.startswith('/uploads/images/products/')
                    and f'url({path})' in node.get('style','')):
                images.append(urljoin(page_url,path))
        if images:
            product['images'] = list(dict.fromkeys(images))
            return True
        return False
    if host == 'fingersmix.ru' and urlsplit(page_url).path.startswith('/production/'):
        soup = BeautifulSoup(html, 'lxml')
        for table in soup.select('table'):
            rows = table.find_all('tr')
            if len(rows) < 2 or not _visible(table):
                continue
            def fields(row):
                return [' '.join(_public_text(c).split()).replace('°', '')
                        for c in row.find_all(['td', 'th'], recursive=False)]
            headers = fields(rows[0])
            if (len(headers) < 4 or headers[1] != 'Наименование показателей'
                    or not headers[2].startswith('Нормативные показатели') or headers[3] != 'Фактические результаты'):
                continue
            from text_extractor import sup_tag_to_caret
            for superscript in table.select('sup'):
                superscript.replace_with(sup_tag_to_caret(superscript))
            specs = {}
            for row in rows[1:]:
                cells = fields(row)
                if not any(cells):
                    continue
                if len(cells) < 4 or not cells[1] or any(cells[4:]):
                    return False
                label = cells[1] if cells[1] not in specs else f'{cells[0]} {cells[1]}'
                specs[label] = {headers[i]: re.sub(r'(?<=\d),\s+(?=\d)', ',', cells[i])
                                for i in (2,3) if cells[i]}
            if specs:
                for key in ('Состав','Норматив'):
                    if key in product.get('specifications', {}):
                        specs[key] = product['specifications'][key]
                product['specifications'] = specs
                for panel in soup.select('.newsitem_text dl > dd, dl.tabs > dd'):
                    term = panel.find_previous_sibling('dt')
                    if term is not None and _visible(term):
                        text = ' '.join(_public_text(panel).split())
                        if text and text != 'В разработке...':
                            _append(product,'instructions_manuals', _public_text(term).rstrip(':') + ': ' + text)
                return True
        return False
    if host == 'vacma.ru' and '/vakuumnye-nasosy/' in urlsplit(page_url).path:
        soup = BeautifulSoup(html, 'html.parser')
        photos = [urljoin(page_url, node['src']) for node in soup.select(
            '.bx-catalog-element .product-item-detail-slider-images-container .product-item-detail-slider-image img[src]')
            if node['src'].startswith('/upload/iblock/') and _visible(node)]
        from site_profiles.markdown_validation import dom_text
        panel = soup.select_one('.bx-catalog-element #char')
        # The description contains the full multi-motor table. The selected SKU
        # tab can contain a shorter copy for one motor and a hidden duplicate.
        tables = soup.select('.bx-catalog-element [id$="_description"] table')
        if not tables and panel:
            tables = [table for table in panel.select('table') if not table.find_parent(class_='dn')]
        changed = False
        if len(tables) == 1:
            rows = tables[0].select('tr')
            head = rows[0].find_all(['td','th'],recursive=False) if rows else []
            if len(head) == 2 and dom_text(head[0]) == 'Наименование параметра и размера' and 'Значение для насоса' in dom_text(head[1]):
                specs = {}
                for label in panel.select('span.psk273'):
                    value = label.find_next_sibling('span')
                    if value and dom_text(value):
                        specs[dom_text(label).rstrip(': ')] = dom_text(value).replace('°','')
                for row in rows[1:]:
                    cells = row.find_all(['td','th'],recursive=False)
                    if len(cells) == 1 and cells[0].get('colspan') == '2' and 'ПРИМЕЧАНИЕ:' in dom_text(cells[0]):
                        note = dom_text(cells[0]).replace('°','')
                        specs['Примечание к параметрам измерения'] = note
                        changed = _append(product,'exploitation',note) or changed
                        continue
                    if len(cells) != 2:
                        raise ValueError('Изменилась структура паспортной таблицы насоса')
                    pair = []
                    for cell in cells:
                        body = deepcopy(cell)
                        for br in body.select('br'):
                            br.replace_with(' | ')
                        pair.append([v.strip().replace('°','') for v in dom_text(body).split('|') if v.strip()])
                    names, values = pair
                    if not names or not values:
                        continue
                    if len(names) == len(values)+1 and all(n.startswith('-') for n in names[1:]):
                        specs[names[0].rstrip(': ')] = dict(zip([n.lstrip('- ') for n in names[1:]],values,strict=True))
                    else:
                        specs[' '.join(names)] = ' / '.join(values)
                changed = product.get('specifications') != specs or changed
                product['specifications'] = specs
        if photos:
            return_value = list(dict.fromkeys(photos))
            changed = product.get('images') != return_value or changed
            product['images'] = return_value
            return changed
        return changed
    if host == 'bescom.ru' and re.fullmatch(r'/pages/prod\d+\.php', urlsplit(page_url).path):
        soup = BeautifulSoup(html, 'html.parser')
        paragraphs = [node for node in soup.select('body > div[id="text"] > p')
                      if _visible(node) and not node.find('table') and len(_public_text(node).split()) > 3]
        if paragraphs:
            description = '\n\n'.join(' '.join(_public_text(node).split()) for node in paragraphs)
            changed = product.get('description') != description
            product['description'] = description
            return changed
        return False
    if host == 'baltur-russia.ru' and '/produkt/' in urlsplit(page_url).path:
        soup = BeautifulSoup(html, 'html.parser')
        specs = {}
        for key in soup.select('#nav-tehhar .tex-har > .file-col.col-lg-8'):
            value = key.find_next_sibling()
            if value and 'col-lg-4' in value.get('class', []) and _visible(key) and _visible(value):
                specs[_public_text(key).replace('°', '')] = _public_text(value).replace('°', '')
        if not specs:
            return False
        changed = specs != product.get('specifications')
        product['specifications'] = specs
        kit = product.get('complectation')
        if isinstance(kit, str):
            cleaned = '\n'.join(line for line in kit.splitlines()
                                if not re.search(r'вес (?:без|с) упаковк', line, re.I))
            if cleaned != kit:
                product['complectation'] = cleaned
                changed = True
        tab = soup.select_one('a[href="#nav-home"], [data-target="#nav-home"]')
        if _control_available(tab):
            for item in soup.select('#nav-home .tex-har ul > li'):
                if _visible(item):
                    changed = _append(product, 'description', _public_text(item)) or changed
        return changed
    if host == 'zubr.ru':
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        for row in soup.select('.detail-container-grey-white table.articles-table tr'):
            cells = row.find_all('td', recursive=False)
            if (len(cells) == 2 and _visible(row) and
                    _norm(_public_text(cells[0])) == 'масса в упаковке, кг'):
                value = _public_text(cells[1])
                specs = product.get('specifications')
                if isinstance(specs, dict) and value and specs.get(_public_text(cells[0])) != value:
                    specs[_public_text(cells[0])] = value
                    changed = True
        thumbs = soup.select('.detail-page-product .slide-inner-content.js-full-window img[src]')
        originals = soup.select('.full-window-wrapper .js-slider-full-window .image-wrapper img[src]')
        filenames = {urlsplit(node['src']).path.rsplit('/', 1)[-1] for node in thumbs}
        images = list(dict.fromkeys(urljoin(page_url, node['src']) for node in originals
                     if urlsplit(node['src']).path.rsplit('/', 1)[-1] in filenames
                     and node['src'].startswith('/upload/iblock/')))
        if images and len({urlsplit(u).path.rsplit('/',1)[-1] for u in images}) == len(filenames):
            if images != product.get('images'):
                product['images'] = images
                changed = True
        return changed
    if host == 'volson.ru':
        return _volson_model_columns(product, html)
    if host == 'istra-vniieto.ru' and urlsplit(page_url).fragment.startswith('source-family='):
        from inline_products import isolate_inline_product
        scoped = isolate_inline_product(html, page_url)
        if not scoped:
            return False
        soup = BeautifulSoup(scoped, 'lxml')
        root = soup.select_one('main[data-source-family]')
        if not root:
            return False
        specs = {}
        price = ''
        for row in root.select('table tr'):
            label, value = row.th.get_text(' ', strip=True), row.td.get_text(' ', strip=True)
            if label == 'Паспорт':
                continue
            if label == 'Цена (руб.)':
                price = value
            else:
                specs[label.replace('°', '')] = value
        product['specifications'] = specs
        product['price'] = price
        product['images'] = [urljoin(page_url, n['src']) for n in root.select('img[src]')]
        _owned_description(product, soup, 'main > p:not(:has(a)), main > ul')
        return True
    if host == 'ladogaltdspb.ru':
        soup = BeautifulSoup(html, 'lxml')
        return _owned_description(product, soup, '[data-elementor-type="wp-page"] .elementor-widget-text-editor .elementor-widget-container > p')
    if host == 'dealanenergo.ru':
        soup = BeautifulSoup(html, 'lxml')
        changed = _owned_description(product, soup, '#b88 .b88__tabsContent .tinymce > p:not(:has(a)), #b88 .b88__tabsContent .tinymce > ul')
        for row in soup.select('#b88 .b88__paramRow'):
            cells = row.select(':scope > .b88__param')
            if len(cells) == 2 and _visible(row) and _public_text(cells[0]) == 'Температура эксплуатации':
                value = _public_text(cells[1]).replace('°', '')
                changed = _append(product, 'exploitation', 'Температура эксплуатации: ' + value) or changed
        return changed
    if host == 'omskblok.ru' and urlsplit(page_url).path in ('/varmit/', '/warm-brick/'):
        return _omsk_materials(product, html, page_url)
    if host == 'intersigma.ru' and urlsplit(page_url).path == '/ruch.htm':
        return _intersigma_pump_tables(product, html)
    if host == 'fubag.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        return _fubag_details(product, html, page_url)
    if host == 'abac-russia.com':
        return _abac_applications(product, html)
    if host == 'rothenberger.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        return _rothenberger_properties(product, html)
    if host == 'demidovo.ru' and urlsplit(page_url).fragment.startswith('source-family='):
        return _demidovo_plywood(product, html, page_url)
    if host == 'gazosilicat.by' and urlsplit(page_url).path.rstrip('/') == '/products/blocks':
        return _gazosilicat_tables(product, html, page_url)
    if host == 'ru-hr.com' and urlsplit(page_url).path.startswith('/konditsioner-haier-'):
        return _haier_specs(product, html, page_url)
    if host == 'oookalita.ru':
        soup = BeautifulSoup(html, 'html.parser')
        packaging = set()
        for node in soup.select('#allrecords > .t-rec:has(h1) .tn-atom'):
            if not _visible(node):
                continue
            value = _public_text(node)
            if (re.search(r'уложен[аы]? на поддон\w* по \d+\s+шт', value, re.I)
                    and not re.search(r'₽|руб|цен[аы]|стоимост', value, re.I)):
                packaging.add(value)
        if len(packaging) == 1:
            value = packaging.pop()
            if product.get('complectation') != value:
                product['complectation'] = value
                return True
        return False
    if host=='vemz.net' and urlsplit(page_url).path.startswith('/product/'):
        from site_profiles.markdown_validation import html_table
        from site_profiles import get_resolver
        from product_interactions import preserve_product_panels
        soup=BeautifulSoup(html,'html.parser')
        preserve_product_panels(soup,page_url)
        profile=get_resolver().resolve(page_url)
        if profile:
            for selector in profile.extract.markdown.remove_selectors:
                for node in soup.select(selector):
                    if node.name is not None:node.decompose()
        root=soup.select_one('.product-wrapper')
        if root is None:return False
        changed=_owned_description(product,soup,'.product-wrapper .content-text')
        labelled={};field=None
        labels={'назначение':'applications','назначение и область применения':'applications',
                'условия эксплуатации':'exploitation','комплект поставки':'complectation',
                'условия хранения':'storage'}
        for content in root.select('.content-text'):
            field=None
            for node in content.find_all(recursive=False):
                value=_public_text(node) if _visible(node) else ''
                key=_norm(value)
                if key in labels:
                    field=labels[key];labelled.setdefault(field,[]);continue
                if node.find('table') or node.name=='table' or key in ('конструкция','технические характеристики'):
                    field=None;continue
                if field and value:labelled[field].append(value)
        for field,values in labelled.items():
            if values:product[field]='\n\n'.join(values);changed=True
        if labelled.get('exploitation') and not labelled.get('storage'):
            # На этих страницах нет самостоятельных условий хранения.
            # Модель переносила сюда температуру из «Условий эксплуатации».
            product['storage']=''
        layouts=[];specs={}
        for number,table in enumerate(root.select('table'),1):
            if not _visible(table) or table.find('table'):continue
            data=html_table(table);headers=data['headers'];rows=data['rows']
            if not data['header_rows'] and len(rows)>1:headers,rows=rows[0],rows[1:]
            if len(rows)<2 or len(headers)<2:
                # Версточная таблица с самостоятельным текстом размеров:
                # её содержимое сохраняется, даже если следующая таблица матричная.
                for row in rows:
                    for value in dict.fromkeys(v for v in row if v):changed=_append(product,'description',value) or changed
                continue
            if len(headers)==3 and headers[0]==headers[1]:
                rows=[[r[0]+(' / '+r[1] if r[1] and r[1]!=r[0] else ''),r[2]] for r in rows]
                headers=[headers[0],headers[2]]
            columns=[(label or 'Значение')+' / столбец '+str(i) for i,label in enumerate(headers[1:],1)]
            records={}
            for i,row in enumerate(rows,1):
                key=row[0] or 'Строка '+str(i)
                if key in records:key+=' / строка '+str(i)
                records[key]=dict(zip(columns,row[1:]))
            path='Опубликованная таблица '+str(number)
            specs[path]=records
            layouts.append(dict(path=path,title='',row_header=headers[0] or 'Параметр',columns=columns,labels=headers[1:],column_axis='',notes=data['notes']))
        if specs:
            # Повторяющиеся шапки сохраняют отдельные колонки и связи значений.
            product['specifications']=specs;ai_data['_table_layouts']=layouts;changed=True
        if urlsplit(page_url).path.rstrip('/')=='/product/2ktp-pk-do-630-kva':
            # Диапазон 25...1000 кВА описывает семейство КТП в общем абзаце,
            # не мощность конкретного исполнения до 630 кВА.
            for key in list(product.get('specifications',{})):
                if _norm(key)=='мощность' and '1000' in str(product['specifications'][key]):
                    del product['specifications'][key]
        return changed
    if host=='ryazanbrick.ru' and '/tproduct/' in urlsplit(page_url).path:
        soup=BeautifulSoup(html,'html.parser');changed=False
        images=visible_ryazan_images(soup,page_url)
        if images:
            changed=product.get('images')!=images
            product['images']=images
        specs=product.setdefault('specifications',{})
        for option in soup.select('.js-store-product .js-product-edition-option'):
            title=option.select_one('.js-product-edition-option-name')
            values=[_public_text(n) for n in option.select('.t-product__option-variants_custom .t-product__option-title') if _visible(n)]
            if title and _visible(title) and values:
                label=_public_text(title);value='; '.join(dict.fromkeys(values))
                if label=='Назначение':product['applications']=value
                elif label=='Цвет':product['colors']=list(dict.fromkeys(values))
                else:specs[label]=value
                changed=True
        for node in soup.select('.js-store-prod-text p, .js-store-prod-text'):
            text=_public_text(node) if _visible(node) else ''
            match=re.search(r'Сезонная акция.*',text)
            if match:
                product['storage']=match[0]
                if 'Зимнее хранение' in str(product.get('exploitation','')):product['exploitation']=''
                break
        return changed
    if host=='oookalita.ru' and urlsplit(page_url).path.strip('/'):
        soup=BeautifulSoup(html,'html.parser');values=[]
        from text_extractor import sup_tag_to_caret
        for node in soup.select('.tn-atom sup'):
            node.replace_with(sup_tag_to_caret(node))
        for node in soup.select('.tn-atom'):
            value=_public_text(node)
            if _visible(node) and re.search(r'уложен[аы].*поддон',value,re.I) and not re.search(r'\d\s*руб',value,re.I):
                if value not in values:values.append(value)
        if values:
            value='\n\n'.join(values);changed=product.get('complectation')!=value
            product['complectation']=value
            return changed
        return False
    if host=='emufta.com':
        soup=BeautifulSoup(html,'html.parser')
        return _owned_description(product,soup,'.faq-accordion__body')
    if host=='knauf.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        soup=BeautifulSoup(html,'html.parser')
        # Эти блоки раскрываются пользовательскими контролами expandable.
        # Сохраняем ограничения применения и условия числовых показателей.
        from text_extractor import sup_tag_to_caret
        for node in soup.select('#PRODUCT_DESCRIPTION sup, #PRODUCT_APPLICATION sup, #PRODUCT_CHARACTERISTICS sup'):
            node.replace_with(sup_tag_to_caret(node))
        for node in soup.select('#PRODUCT_CHARACTERISTICS span[style]'):
            style=re.sub(r'\s+','',node.get('style','')).lower()
            if node.get_text(strip=True)=='0' and 'position:relative' in style and re.search(r'top:-\d',style):
                node.replace_with('')
        return _owned_description(product,soup,'#PRODUCT_DESCRIPTION_DATA, #PRODUCT_APPLICATION .expandable__inner, #PRODUCT_CHARACTERISTICS .expandable__inner')
    if host=='thermona-rus.ru' and '/tov_' in urlsplit(page_url).path:
        soup=BeautifulSoup(html,'html.parser');heading=soup.find('h1')
        return _append(product,'description',_public_text(heading)) if _visible(heading) else False
    if host=='aksa.com.ru' and urlsplit(page_url).path.startswith('/product/'):
        soup=BeautifulSoup(html,'html.parser');changed=False
        for paragraph in soup.select('.product-card-info__optimal .app-article p'):
            if _visible(paragraph):
                text = ' '.join(_public_text(paragraph).split())
                if re.search(r'\bATS\b',text) and 'при использовании' in text.casefold():
                    changed = _append(product,'compatibility',text) or changed
        for option in soup.select('.app-dropdown-item button[data-stroy-public-option="1"]'):
            if not _visible(option):continue
            own=deepcopy(option)
            for price in own.select('.product-card-top-info__options-price'):price.decompose()
            value=_public_text(own)
            if value:changed=_append(product,'compatibility','Дополнительная опция, приобретается отдельно: '+value) or changed
        return changed
    if host in ('albanydoorsystems.ru','swan-nn.ru','bastion-group.com','bkmz.ru','santexprom.ru','nayada.ru'):
        soup=BeautifulSoup(html,'html.parser')
        selectors={
            'albanydoorsystems.ru':'.element__content .tabs__content',
            'swan-nn.ru':'.product__main-text',
            'bastion-group.com':'#description,#technology',
            'bkmz.ru':'.card .card-desc',
            'santexprom.ru':'#tab-1',
            'nayada.ru':'main .b-about__desc, main .b-seoCommonText__rightText',
        }
        changed=_owned_description(product,soup,selectors[host])
        if host=='swan-nn.ru':
            for node in soup.select('.product-params__item'):
                if _visible(node) and 'грубо сравнивать' in _public_text(node).replace('\xad',''):
                    changed=_append(product,'description',_public_text(node).replace('\xad','')) or changed
        if host=='bastion-group.com' and urlsplit(page_url).path=='/materials/etalon-bt':
            for table in soup.select('#characteristics table'):
                if not _visible(table):continue
                lines=[' | '.join(_public_text(c) for c in row.find_all(['td','th'],recursive=False)) for row in table.select('tr') if _visible(row)]
                for line in lines:
                    if line:changed=_append(product,'description',line) or changed
        return changed
    if host=='apriz.ru' and urlsplit(page_url).path.rstrip('/')=='/products/agroperlit':
        soup=BeautifulSoup(html,'html.parser')
        for table in soup.select('td.leftbg > div table'):
            rows=table.find_all('tr')
            if not rows or 'значение для марки' not in _norm(_public_text(rows[0])):continue
            if len(rows)<2 or not _visible(rows[1]):continue
            cells=rows[1].find_all(['td','th'],recursive=False)
            if len(cells) in (1,2) and _public_text(cells[-1]):
                return _append(product,'description','Марка: '+_public_text(cells[-1]))
        return False
    if host == 'ec-smesi.ru' and urlsplit(page_url).path.startswith('/product/'):
        soup = BeautifulSoup(html, 'html.parser')
        panel = soup.select_one('.rs-product #tab-description article')
        control = soup.select_one('[href="#tab-description"], [data-bs-target="#tab-description"]')
        if panel is None or not _control_available(control):
            return False
        fields = {}; field = 'description'; label = ''
        for node in panel.find_all(recursive=False):
            if not _visible(node):
                continue
            value = _public_text(node)
            if not value:
                continue
            if node.name in ('h2', 'h3', 'h4'):
                label = value
                title = _norm(value)
                if 'назначени' in title: field = 'applications'
                elif any(word in title for word in ('подготовка', 'приготовлен', 'применени')): field = 'instructions_manuals'
                elif 'предосторож' in title: field = 'security_measures'
                elif 'хранени' in title or 'срок годности' in title: field = 'storage'
                elif 'фасовка' in title or 'упаковка' in title: field = 'complectation'
                else: field = 'description'
                fields.setdefault(field, []).append(label)
            elif node.name in ('p', 'ul', 'ol'):
                # Условие измерения относится ко всем ТХ, а не к фасовке.
                target = 'description' if re.search(r'характеристик.*(?:действительн|температур)', value, re.I) else field
                fields.setdefault(target, []).append(value)
        changed = False
        for field, values in fields.items():
            value = '\n\n'.join(values)
            if field == 'description':
                changed = _append(product, field, value) or changed
            else:
                changed = product.get(field) != value or changed
                product[field] = value
        specs_control = soup.select_one('[href="#tab-ud_property"], [data-bs-target="#tab-ud_property"]')
        if _control_available(specs_control):
            for note in soup.select('.rs-product #tab-ud_property article p'):
                value = _public_text(note)
                if _visible(note) and re.search(r'характеристик.*(?:действительн|температур)', value, re.I):
                    changed = _append(product, 'description', value) or changed
        return changed
    if host == 'npl38080.ru' and urlsplit(page_url).path.startswith('/products/'):
        soup = BeautifulSoup(html, 'html.parser'); changed = False
        literal_fields={}
        for row in soup.select('.b-product-detail table tr'):
            cells = row.find_all(['td', 'th'], recursive=False)
            if len(cells) != 2 or not all(_visible(cell) for cell in cells):
                continue
            label, value = (_public_text(cell) for cell in cells)
            field={'состав':'description','особенности':'description','назначение':'applications',
                   'условия эксплуатации':'exploitation','срок эксплуатации':'exploitation','гарантийный срок хранения до использования':'storage',
                   'вид тары':'complectation'}.get(_norm(label))
            if field and value:literal_fields.setdefault(field,[]).append(value)
            if _norm(label) in ('минимальный заказ', 'минимальная партия', 'дополнительная информация') and value:
                # Срок производства/объём заказа не являются числовыми ТХ.
                own_label='Минимальный заказ' if _norm(label)=='минимальная партия' else label
                value=re.sub(r'^Минимальный заказ\s*[–—:-]\s*','',value)
                changed = _append(product, 'description', own_label + ': ' + value) or changed
        for field,values in literal_fields.items():
            if field=='description':
                # Полные состав/особенности заменяют сокращённый пересказ.
                # Прочие явно восстановленные коммерческие условия сохраняются.
                extras=[line for line in str(product.get('description','')).splitlines()
                        if line.lstrip('- ').startswith(('Минимальный заказ:', 'Дополнительная информация:'))]
                product[field]='\n\n'.join(list(dict.fromkeys(values))+extras)
            else:product[field]='\n\n'.join(dict.fromkeys(values))
            changed=True
        specs=product.get('specifications')
        if isinstance(specs,dict):
            for key in list(specs):
                normalized=_norm(key)
                # Размер кабельной проходки и климат эксплуатации — условия
                # применения состава; они уже сохранены в собственных полях.
                if normalized in ('назначение','условия эксплуатации','срок эксплуатации','температурный режим'):
                    del specs[key];changed=True
        # Публичная инструкция бывает абзацами либо одноколоночной таблицей
        # с ненумерованной шапкой. Сохраняем весь собственный блок.
        instructions=[node.get_text('\n',strip=True)
                      for node in soup.select('.b-product-detail .product-detail__instruction')
                      if _visible(node) and _public_text(node)]
        if instructions:
            product['instructions_manuals']='\n\n'.join(instructions);changed=True
            for text in instructions:
                for line in text.splitlines():
                    if re.search(r'необходимо хранить',line,re.I):
                        _append(product,'storage',re.sub(r'^\d+\.\s*','',line))
        for table in soup.select('.b-product-detail table'):
            rows = table.find_all('tr')
            if not rows or not _visible(table):
                continue
            headers = [_public_text(c) for c in rows[0].find_all('th', recursive=False)]
            if len(headers) != 3 or not _norm(headers[0]).startswith('показатель огнестойкости'):
                continue
            lines = []
            for row in rows[1:]:
                cells = row.find_all('td', recursive=False)
                if len(cells) != 3 or not all(_visible(c) for c in cells):
                    continue
                values = [_public_text(c) for c in cells]
                if all(values):
                    lines.append('; '.join(h + ': ' + v for h, v in zip(headers, values)))
                elif values[0].startswith('*') and not any(values[1:]):
                    lines.append(values[0])
            for line in lines:
                changed = _append(product, 'description', line) or changed
        return changed
    if host == 'kbpribor.ru' and urlsplit(page_url).path.startswith('/tovar/'):
        soup = BeautifulSoup(html, 'html.parser')
        owners = soup.select('.product__body')
        if len(owners) != 1 or not _visible(soup.find('h1')):
            return False
        # Собственные абзацы содержат условия тестирования и настройки,
        # которые модель сокращала либо отправляла в невыводимый additional_info.
        paragraphs = []
        for node in owners[0].select(':scope > p:not(.pkeys__more)'):
            if not _visible(node):
                continue
            value = _public_text(node)
            if node.find('a') and re.fullmatch(r'[←\s]*Ко всем товарам', value, re.I):
                continue
            if value and not re.search(r'₽|\bруб(?:\.|лей|ля)?\b|\bцен[аыу]\b', value, re.I):
                paragraphs.append(value)
        if not paragraphs:
            return False
        description = '\n\n'.join(paragraphs)
        changed = product.get('description') != description
        product['description'] = description
        for listing in owners[0].select(':scope > ul.product__list'):
            heading = listing.find_previous_sibling(['h2', 'h3'])
            if heading is None or 'преимуществ' not in _norm(_public_text(heading)) or not _visible(listing):
                continue
            values = [_public_text(li) for li in listing.find_all('li', recursive=False) if _visible(li)]
            advantages = '\n'.join('- ' + value for value in values if value)
            if advantages:
                changed = product.get('advantages') != advantages or changed
                product['advantages'] = advantages
        return changed
    if host == 'v-kirpich.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        specs = product.setdefault('specifications', {})
        packing=[]
        for row in soup.select('.element__values.tab-2 .element__value'):
            label = row.select_one('.element__value-title')
            value = row.select_one('.element__value-item')
            if label is None or value is None:
                continue
            key, val = _public_text(label).strip().rstrip(':'), _public_text(value).strip()
            if key == 'Маркировка кирпича' and val:
                # Asterisk here denotes dimensions, not Markdown emphasis.
                specs[key] = val.replace('*', '×')
                changed = True
            if key == 'Размер поддона' and val:
                normalized = re.sub(r'(\d+(?:[.,]\d+)?)\s*м\s*\*\s*(\d+(?:[.,]\d+)?)\s*м', r'\1×\2 м', val).replace('*', ' × ')
                packing.append(key+': '+normalized)
            if key.startswith('Количество') and val:
                packing.append(key+': '+val)
        if packing:
            product['complectation']='\n'.join('- '+v for v in packing)
            changed=True
        # Public delivery tab is common to several types: preserve its terms,
        # and select only the loading row matching this product's visible type.
        panel=soup.select_one('.element__values.tab-3')
        if panel is not None:
            copy=deepcopy(panel)
            for unwanted in copy.select('table,img,script,style,a[href^="tel:"],a[href^="mailto:"]'):
                unwanted.decompose()
            terms='\n'.join(re.sub(r'\s+',' ',v).strip() for v in copy.get_text('\n',strip=True).splitlines())
            if terms:
                product['storage']='Общие условия доставки и оплаты, опубликованные на странице товара:\n'+terms
                changed=True
            h1=soup.find('h1');name=_norm(_public_text(h1)) if h1 else ''
            wall='утолщенная лицевая стенка' in _norm(str(specs))
            for table in panel.select('table'):
                for row in table.select('tr'):
                    cells=row.find_all(['td','th'],recursive=False)
                    if not cells:continue
                    label=_public_text(cells[0]);kind=_norm(label)
                    if 'пустотелый одинарный' not in kind:continue
                    if len(cells)==5:
                        value=(label+': КАМАЗ — '+_public_text(cells[1])+' поддонов, '+_public_text(cells[2])+' шт.; '
                               'КАМАЗ с прицепом — '+_public_text(cells[3])+' поддонов, '+_public_text(cells[4])+' шт.')
                    elif len(cells)==2 and ('утолщенной лицевой стенкой' in kind)==wall:
                        value='Максимальная вагонная норма ('+label+'): '+_public_text(cells[1])
                    else:continue
                    changed=_append(product,'storage',value) or changed
        description=soup.select_one('.element__values.tab-1')
        if description:
            text=description.get_text('\n',strip=True)
            if 'При ведении кладки следует пользоваться' in text:
                start=text.index('При ведении кладки следует пользоваться')
                product['description']=re.sub(r'\s+',' ',text[:start]).strip()
                product['instructions_manuals']=text[start:].strip()
            else:
                product['description']=re.sub(r'\s+',' ',text).strip()
            changed=True
        # Literal screen-colour caveat belongs to use/purchase instructions.
        for node in soup.find_all(['p','div']):
            if len(node.find_all(['p','div'])) or not _visible(node):continue
            text=_public_text(node)
            if text.startswith('Обращаем Ваше внимание, что цвет продукции на экране монитора'):
                changed=_append(product,'instructions_manuals',text) or changed
        for node in soup.select('.price_info'):
            text = _public_text(node)
            if _visible(node) and text == 'Покупка кирпича осуществляется поддонами':
                changed = _append(product, 'complectation', text) or changed
        return changed
    if host == 'konord.com' and urlsplit(page_url).path.startswith('/catalog/'):
        soup = BeautifulSoup(html, 'html.parser')
        panel = soup.select_one('#description')
        control = soup.select_one('#description-tab[href="#description"]')
        if panel is not None and _visible(control) and not panel.select_one('form,input,textarea'):
            # Сам источник описывает линейку (включая её диапазоны), тогда как
            # таблица даёт параметры конкретной модели. Не смешиваем их.
            panel.attrs.pop('hidden', None)
            panel.attrs.pop('style', None)
            text = _public_text(panel)
            if text:
                product['description'] = 'Опубликованное описание линейки на странице товара: '+re.sub(r'\s+', ' ', text)
                return True
        return False
    if host in ('doorhan.ru', 'moscow.doorhan.ru'):
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        panel = soup.select_one('.product-page .tabs__content-item[data-content="complectation"]')
        control = soup.select_one('.product-page .tabs__item[data-tab="complectation"]')
        if panel and _visible(control):
            equipment = []
            for switch in panel.select('.tabs__switcher-item[data-switcher]'):
                label = _public_text(switch)
                slider = panel.find(attrs={'data-slider': switch['data-switcher']})
                if label not in ('Базовая', 'Дополнительная') or not slider or not _visible(switch):
                    continue
                for item in slider.select('.tabs__equipment-slider-item'):
                    texts = [_public_text(n) for n in item.select('.tabs__equipment-slider-heading, .tabs__equipment-slider-text') if _visible(n)]
                    value = ' '.join(v for v in dict.fromkeys(texts) if v)
                    if value and not re.search(r'\d[\d\s,.]*\s*(?:руб|₽)', value, re.I):
                        equipment.append(label + ': ' + value)
            if equipment:
                product['complectation'] = '\n'.join('- ' + v for v in dict.fromkeys(equipment))
                changed = True
        for row in soup.select('.product-page .catalog-section-tech__table tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) == 3 and _visible(row) and _public_text(cells[2]):
                changed = _append(product, 'exploitation', _public_text(cells[0]) + ': ' + _public_text(cells[2])) or changed
        for node in soup.select('.product-page .main-description__text b'):
            if _visible(node) and _public_text(node).strip('. ') == 'В эти ворота мы можем вставить калитку':
                changed = _append(product, 'complectation', 'Дополнительно: в ворота может быть встроена калитка.') or changed
        for node in soup.select('.product-page .main-description__text'):
            if _visible(node):
                for sentence in re.split(r'(?<=[.!?])\s+', _public_text(node)):
                    if sentence.startswith(('В жесткий алюминиевый каркас ', 'Конструкция ворот предусматривает ')):
                        changed = _append(product, 'description', sentence) or changed
        for node in soup.select('.product-page .measurement__text'):
            if _visible(node):
                value = _public_text(node).replace(', как показано на рисунке', '')
                changed = _append(product, 'instructions_manuals', value) or changed
        return changed
    if host == 'carlisle-llc.ru' and urlsplit(page_url).path == '/catalog/geomembrane/description.html':
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        for block in soup.select('#midCol .sppb-addon-text-block .sppb-addon-content'):
            paragraphs=[_public_text(p) for p in block.find_all('p',recursive=False) if _visible(p)]
            if paragraphs and paragraphs[0].startswith('Бутилкаучуковая геомембрана'):
                product['description']='\n\n'.join(paragraphs)
                changed=True
        for block in soup.select('#midCol .sppb-addon-blocknumber'):
            title, body = block.select_one('.sppb-addon-title'), block.select_one('.sppb-media-body')
            if not _visible(title) or not _visible(body):
                continue
            if _public_text(title) == 'Монтаж пленки для водоемов':
                product['instructions_manuals'] = _public_text(body)
                changed = True
            if _public_text(title) == 'Уникальные характеристики EPDM PondLiner':
                product['advantages']='\n'.join('- '+_public_text(p).rstrip(';') for p in body.select('li') if _visible(p))
                changed=True
            for item in body.select('li'):
                value = _public_text(item).rstrip(';')
                if _visible(item) and value.casefold().startswith('гарантия на материал '):
                    changed = _append(product, 'exploitation', value) or changed
        return changed
    if host == 'rvul.ru':
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        # Адреса публичной товарной галереи копируются буквально: модель
        # может потерять один символ в длинном имени каталога изображения.
        gallery = []
        for img in soup.select('#overview .product-view-slider__item img'):
            if not _visible(img):
                continue
            raw = img.get('data-src') or img.get('src') or ''
            value = urljoin(page_url, raw)
            parsed = urlsplit(value)
            if (parsed.scheme in ('http', 'https') and parsed.path.startswith('/upload/')
                    and re.search(r'\.(?:png|jpe?g|webp|gif|avif)$', parsed.path, re.I)
                    and value not in gallery):
                gallery.append(value)
        if gallery:
            changed = product.get('images') != gallery
            product['images'] = gallery
        description = soup.select_one('#overview .product-overview__description')
        if description and _visible(description) and _public_text(description):
            product['description'] = _public_text(description)
            changed = True
        for label in soup.select('main .text-block__col > b'):
            value = label.find_next_sibling()
            if (_visible(label) and _public_text(label) == 'Состав' and value
                    and value.name in ('div', 'p') and _visible(value) and _public_text(value)):
                product.setdefault('specifications', {})['Состав'] = _public_text(value)
                changed = True
        supplies = soup.select_one('#supplies')
        if supplies and _visible(supplies) and 'Рекомендуем использовать' in _public_text(supplies):
            names = [_public_text(node) for node in supplies.select('.product-card__title') if _visible(node)]
            existing = re.sub(r'Рекомендуется использовать совместно:\s*', '', product.get('compatibility') or '', flags=re.I)
            known = {_norm(part.strip(' -*')) for part in re.split(r'[\n;]', existing)}
            missing = [name for name in dict.fromkeys(names) if _norm(name) not in known]
            if missing:
                changed = _append(product, 'compatibility', 'Рекомендуется использовать совместно: ' + '; '.join(missing)) or changed
        return changed
    if host == 'hagerrussia.ru':
        soup = BeautifulSoup(html, 'html.parser')
        changed = False
        for row in soup.select('#content .item-specification .haracter'):
            label, value = row.select_one('.attr-name'), row.select_one('.attr-text')
            if _visible(row) and label and value:
                key = _public_text(label)
                fact = re.sub(r'^\s*-\s+', '', _public_text(value))
                if key.startswith('Срок службы'):
                    changed = _append(product, 'exploitation', key + ': ' + fact) or changed
                elif key and fact:
                    product.setdefault('specifications', {})[key] = fact
                    changed = True
        for node in soup.select('#content [itemprop="description"]'):
            value = _public_text(node)
            if (_visible(node) and value
                    and not re.search(r'\d[\d\s,.]*\s*(?:руб|₽|RUB|USD|EUR)', value, re.I)):
                # Собственное описание сохраняет условия и принцип работы,
                # которые сокращались моделью при пересказе.
                product['description'] = value.replace('Оптимальная цена.', '').strip()
                changed = True
            if _visible(node) and 'серии шкафов Volta и Golf' in value:
                for sentence in _sentences(value):
                    if 'максимально допустимому номинальному току' in sentence or 'серии шкафов Volta и Golf' in sentence:
                        changed = _append(product, 'compatibility', sentence) or changed
        if isinstance(product.get('advantages'), str):
            product['advantages'] = re.sub(r'(?:^|\n)\s*[-•]?\s*Оптимальная цена[.;]?\s*(?=\n|$)', '', product['advantages']).strip()
        return changed
    if host == 'alumil-yug.ru':
        facts = {
            '/S650_alumil_razdvizhnaya_dver.html': ('Имеются дополнительные скрытые профили для водоотлива', 'description'),
            '/M19800.html': ('Имеется возможность изготовления распашных дверей', 'description'),
            '/S67_aluminievie_okna_alumil.html': ('Система сертифицирована немецким Институтом IFT Rosenheim', 'description'),
            '/MD9660_alumil_aluminievie_dvei.html': ('Возможность выбора между механизмами разного типа', 'compatibility'),
        }
        expected = facts.get(urlsplit(page_url).path)
        changed = False
        if expected:
            soup = BeautifulSoup(html, 'html.parser')
            for node in soup.select('#text1 .wpmd > div'):
                value = _public_text(node).lstrip('-/ \u00a0')
                if _visible(node) and value.startswith(expected[0]):
                    changed = _append(product, expected[1], value) or changed
        # Preserve source conditions as conditions of Uw, rather than an
        # independent glass/system parameter that inflates the numeric gate.
        soup = BeautifulSoup(html, 'html.parser')
        paragraphs = [re.sub(r'\s+', ' ', _public_text(node)).lstrip('-/ ')
                      for node in soup.select('#text1 .wpmd > div') if _visible(node)]
        for index, value in enumerate(paragraphs):
            if ('Uw=' in value and 'для размеров' in value and 'Ug=' not in value
                    and index+1 < len(paragraphs) and 'Ug=' in paragraphs[index+1]):
                value += ' ' + paragraphs[index+1]
            if ('уровень теплоизоляции' in value and 'Uw=' in value
                    and 'для размеров' in value and 'Ug=' in value):
                specifications = product.setdefault('specifications', {})
                for key in list(specifications):
                    if re.search(r'\bU[wg]\b', key, re.I):
                        del specifications[key]
                specifications['Теплоизоляция Uw'] = value[value.index('Uw='):]
                changed = True
            if value.startswith(('Для помещений таких, как автомобильные салоны',
                                 'Для помещений  таких, как автомобильные салоны',
                                 'При использовании особых петель',
                                 'Роликовый механизм может устанавливаться',
                                 'Доступны дополнительные скрытые профили',
                                 'Теплая панель',
                                 'Сертифицирована институтами',
                                 'Система сертифицирована')):
                changed = _append(product, 'description', value) or changed
        return changed
    if host not in ('xotpipe.ru', 'ecosgroup.com', 'ventexpress.ru', 'bitumex.org', '2vv.cz', '1marka.ru', 'ufapolimer.ru', 'pergamin.ru', 'nppnfl.ru', 'stabiterm.ru', 'ecovod.ru', 'parad.by', 'polikor.net', 'ascold.ru', 'belcolor.ru', 'gbi-grand.ru', 'bksm.net', 'recon-rec.ru', 'monoplus.ru', 'cteed.ru', 'products.metalloinvest.com', 'gidrol.ru', 'sibno.ru', 'sk-stroiservis.ru', 'ecodin.ru'):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    changed = False
    if host == 'ecodin.ru':
        options = []
        for control in soup.select('input[type="checkbox"][name="services[]"][id]'):
            label = soup.find('label', attrs={'for': control['id']})
            if not _visible(label):
                continue
            value = re.sub(r'\s*\(\+\s*[\d\s.,]+\s*₽\s*\)', '', _public_text(label)).strip()
            if value:
                options.append(value)
        if options:
            product['complectation'] = 'Дополнительно по выбору: ' + '; '.join(dict.fromkeys(options))
            changed = True
        for node in soup.select('.badge'):
            value = _public_text(node)
            if _visible(node) and re.fullmatch(r'\d+\s*[-–]\s*\d+\s+человек', value):
                product.setdefault('specifications', {})['Рекомендуемое число пользователей'] = value
                changed = True
        return changed
    if host == 'sk-stroiservis.ru':
        control = soup.select_one('div.product a[href="#tab-description"]')
        panel = soup.select_one('div.product #tab-description')
        if (_visible(control) and _norm(_public_text(control)) == 'описание'
                and panel is not None and not panel.select_one('input, textarea, form')
                and _visible(panel.parent)):
            panel.replace_with(panel_section(soup, panel, 'Описание', 'tab-description'))
        for row in soup.select('div.product #tab-description tr, div.product section[data-product-panel="tab-description"] tr'):
            cells = row.find_all(['td', 'th'], recursive=False)
            if len(cells) != 2 or not _visible(row):
                continue
            label, value = map(_public_text, cells)
            if label == 'Цвет' and value.casefold() != 'гранит':
                # Опубликованный список описания полнее текущего селектора заказа.
                product['colors'] = [v.strip() for v in value.split(',') if v.strip()]
                changed = True
            if label == 'Покраска цена' and re.fullmatch(r'\d+\s*₽\([^()]+\)', value):
                colors = value.split('(', 1)[1].rstrip(')')
                changed = _append(product, 'compatibility', 'Покраска: ' + colors) or changed
        for node in soup.select('div.product .product-details__deliveri-info_title'):
            if _visible(node) and 'Отгрузка производится кратно целому поддону!' in _public_text(node):
                changed = _append(product, 'complectation', 'Отгрузка производится кратно целому поддону!') or changed
        return changed
    if host == 'products.metalloinvest.com':
        return _restore_metalloinvest(ai_data, html, page_url)
    if host == 'sibno.ru':
        if urlsplit(page_url).path == '/products/25/':
            for node in soup.select('#content-holder p'):
                value = _public_text(node)
                if _visible(node) and value.startswith('Блок может содержать полный набор'):
                    changed = _append(product, 'complectation', value) or changed
            for node in soup.select('#content-holder h2'):
                if _visible(node) and _public_text(node) == 'БД-50х21':
                    changed = _append(product, 'description', 'На странице также представлен блок дросселирования БД-50х21.') or changed
        if urlsplit(page_url).path == '/products/57/':
            values = []
            label = 'Масса пульта выносного, не более, кг'
            for row in soup.select('#content-holder tr'):
                cells = row.find_all(['td', 'th'], recursive=False)
                if len(cells) == 2 and _visible(row) and _norm(_public_text(cells[0])) == _norm(label):
                    values.append(_public_text(cells[1]))
            if len(set(values)) > 1:
                specs = product.setdefault('specifications', {})
                for key in list(specs):
                    if _norm(key).startswith('масса пульта выносного'):
                        specs.pop(key)
                specs[label] = 'В таблице источника указаны два значения: ' + ' и '.join(dict.fromkeys(values))
                changed = True
        for node in soup.select('#content-holder p'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if 'полости для обогрева плашек' in sentence:
                    changed = _append(product, 'advantages', sentence) or changed
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/unimast-cv.html':
        for table in soup.select('#box_wrapper table'):
            if not _visible(table) or 'Температурный интервал эксплуатации' not in _public_text(table):
                continue
            for row in table.select('tr'):
                cells = row.find_all(['td', 'th'], recursive=False)
                if len(cells) == 5 and 'ЦВ' in _public_text(cells[0]):
                    product['specifications'] = {'Температурный интервал эксплуатации, C':
                        'от ' + _public_text(cells[2]) + ' до ' + _public_text(cells[3])}
                    product['colors'] = [v.strip().rstrip('.') for v in _public_text(cells[1]).split(',')]
                    changed = True
        for node in soup.select('#box_wrapper p'):
            if _visible(node) and 'При введении алюминиевой пудры теплостойкость мастики повышается' in _public_text(node):
                changed = _append(product, 'exploitation', _public_text(node).lstrip('* ')) or changed
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/zhidkaya-rezina-unimast.html':
        variants = []
        for table in soup.select('#box_wrapper .text table'):
            if not _visible(table) or 'Области применения' not in _public_text(table):
                continue
            for row in table.select('tr'):
                cells = row.find_all('td', recursive=False)
                if len(cells) == 5:
                    label = _public_text(cells[0])
                    match = re.match(r'«(ЦВ|У|П|Б)»', label)
                    if match:
                        variants.append({'Модель': 'Унимаст-' + match[1], 'Цвет': _public_text(cells[1]),
                                         'Температурный интервал эксплуатации, C': 'от ' + _public_text(cells[2]) + ' до ' + _public_text(cells[3]),
                                         'Область применения': _public_text(cells[4])})
        if len(variants) == 4:
            product['variants'] = variants
            changed = True
        for row in soup.select('#box_wrapper .text table tr'):
            cells = row.find_all('td', recursive=False)
            if _visible(row) and (len(cells) == 5 or (len(cells) == 2 and cells[1].get('colspan') == '4')):
                label = _public_text(cells[0])
                values = [_public_text(c) for c in cells[1:]]
                if len(set(values)) == 1 and values[0].startswith(('Не должно быть', 'Отсутствие признаков')):
                    product.setdefault('specifications', {})[label + ' (все марки)'] = values[0]
                    changed = True
        for node in soup.select('#box_wrapper .text p'):
            value = _public_text(node)
            if not _visible(node):
                continue
            if 'При введении алюминиевой пудры теплостойкость мастики повышается' in value:
                changed = _append(product, 'exploitation', value.lstrip('* ')) or changed
            if value.startswith(('Работы по оклейке стеклотканью', 'Мастика при температуре 0', 'Гидроизоляция Вашей кровли выполнена')):
                changed = _append(product, 'instructions_manuals', value) or changed
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/lenta-gidroizolacionnaya-s-folgoi.html':
        for node in soup.select('#box_wrapper .text p, #box_wrapper .text li'):
            value = _public_text(node)
            match = re.match(r'Ширина ролика по заказу (от \d+ до \d+ мм \(шаг \d+ мм\))', value)
            if _visible(node) and match:
                specs = product.setdefault('specifications', {})
                for key in list(specs):
                    if key.startswith('Ширина ролика'):
                        del specs[key]
                specs['Ширина ролика по заказу'] = match[1]
                changed = True
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/elon-super-l.html':
        for node in soup.select('#box_wrapper .text p'):
            value = _public_text(node)
            if not _visible(node):
                continue
            if value.startswith('На основе проведенных экспериментов производительность труда'):
                # Следующее предложение сравнивает стоимость и не входит в карточку.
                changed = _append(product, 'advantages', value.split('Стоимость', 1)[0].strip()) or changed
            if value.startswith(('В зависимости от области применения', 'Примечание * Условная прочность', 'По согласованию с потребителем допускается')):
                changed = _append(product, 'description', value) or changed
            if value.startswith(('Работы по устройству и ремонту кровли', 'На основание из бетона', 'Праймер наносится',
                                 'При устройстве и ремонте кровли', 'Укладку рулонного материала начинают',
                                 'Материал "Элон-Супер-Л" приклеивается', 'В соединительных швах мембраны',
                                 'После завершения наклейки материала')):
                changed = _append(product, 'instructions_manuals', value) or changed
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/krovlelon-a.html':
        for node in soup.select('#box_wrapper .text p'):
            value = _public_text(node)
            if _visible(node) and 'фактические значения по результатам сертификационных испытаний' in value:
                changed = _append(product, 'description', value) or changed
            if _visible(node) and value.startswith(('Клеевой метод прикрепления', 'Технология выполнения конструктива', '* Приплавление', '** Наплавление', 'Механическое крепление Кровлелона', 'Балластная система используется', 'Инверсионная кровля представляет')):
                changed = _append(product, 'instructions_manuals', value) or changed
        for table in soup.select('#box_wrapper .text table'):
            if not _visible(table) or 'Выработка при устройстве кровель' not in _public_text(table):
                continue
            for row in table.select('tr'):
                cells = row.find_all('td', recursive=False)
                if len(cells) == 3:
                    changed = _append(product, 'advantages', _public_text(cells[0]) + ': ' + _public_text(cells[2])) or changed
        return changed
    if host == 'gidrol.ru' and urlsplit(page_url).path == '/elon-super.html':
        for node in soup.select('#box_wrapper p'):
            if not _visible(node):
                continue
            for sentence in _sentences(_public_text(node)):
                if any(term in sentence for term in ('электронно-лучевой', 'Допускается использование других типов основ',
                                                       'рельефный отличительный рисунок', 'Допускается изготовление Элона других размеров')):
                    changed = _append(product, 'description', sentence) or changed
            if _public_text(node).startswith(('Клеевая система наиболее часто', 'Механическое крепление целесообразно', 'Балластная и инверсионная система')):
                changed = _append(product, 'instructions_manuals', _public_text(node)) or changed
        # Условия испытания в двух строках, colspan в HTML неодинаковый.
        specs = product.get('specifications', {})
        for row in soup.select('#box_wrapper tr'):
            if _visible(row) and 'фактические значения по результатам сертификационных испытаний' in _public_text(row).lower():
                changed = _append(product, 'description', _public_text(row)) or changed
            cells = row.find_all(['td', 'th'], recursive=False)
            if len(cells) != 3 or not _visible(row):
                continue
            label = _public_text(cells[0])
            if label.startswith('при давлении не менее'):
                for grade, cell in zip(('Элон-Супер 1', 'Элон-Супер 2'), cells[1:]):
                    if isinstance(specs.get(grade), dict):
                        specs[grade]['Водонепроницаемость: ' + label] = _public_text(cell)
                        changed = True
        for node in soup.select('#box_wrapper p'):
            value = _public_text(node)
            if _visible(node) and value.startswith('* Фактические'):
                changed = _append(product, 'description', value.lstrip('* ')) or changed
        return changed
    if host == 'cteed.ru':
        for root in soup.select('div[id^="xr_mvp_"]:not(.xr_mvpo)'):
            if urlsplit(page_url).path == '/ac-konk-elem.htm':
                for node in root.select('div.xr_txt'):
                    value = _public_text(node)
                    if not _visible(node) or not value.startswith('Коньковый элемент '):
                        continue
                    title = _public_text(node.find('span'))
                    if 'Производится в цветах, соответствующих цветам кровельного покрытия' in value:
                        fact = title + ': производится в цветах, соответствующих цветам кровельного покрытия.'
                        if isinstance(product.get('compatibility'), dict):
                            product['compatibility'][title + ' — цвета'] = fact
                            changed = True
                        else:
                            changed = _append(product, 'compatibility', fact) or changed
                    if 'Shangle Ridge совместно с вентиляционным коньком' in value:
                        fact = 'Размеры Shangle Ridge дают возможность использования совместно с вентиляционным коньком.'
                        if isinstance(product.get('compatibility'), dict):
                            product['compatibility']['Shangle Ridge — вентиляционный конёк'] = fact
                            changed = True
                        else:
                            changed = _append(product, 'compatibility', fact) or changed
            if urlsplit(page_url).path in ('/s-ind-shangle.htm', '/s-grand-manor-shangle.htm'):
                wind = re.search(r'(\d+)-ти летнюю гарантию\s+сопротивления ветру\s+до\s+(\d+)\s+км/ч', _public_text(root))
                if wind and _visible(root):
                    product.setdefault('specifications', {})['Ветроустойчивость'] = 'до ' + wind[2] + ' км/ч (гарантия ' + wind[1] + ' лет)'
                    changed = True
            for node in root.select('span.xr_tl'):
                if _visible(node) and _public_text(node).startswith('Температура эксплуатации'):
                    if 'температура эксплуатации' not in _norm(product.get('exploitation', '')):
                        changed = _append(product, 'exploitation', _public_text(node)) or changed
            for node in root.select('div.xr_txt'):
                value = _public_text(node)
                path = urlsplit(page_url).path
                if (_visible(node) and not node.has_attr('onmousemove') and not node.find('a')
                        and path in ('/s-ct20.htm', '/s-ind-shangle.htm', '/s-grand-manor-shangle.htm')):
                    if len(value) > 130 and 'CertainTeed' in value and not value.startswith('Модельный ряд') and '•' not in value:
                        changed = _append(product, 'description', value) or changed
                    if value.startswith('Особенности:') and 'Стандарты:' in value:
                        instructions, standards = value.split('Стандарты:', 1)
                        changed = _append(product, 'instructions_manuals', instructions.removeprefix('Особенности:').strip()) or changed
                        changed = _append(product, 'tu', standards.strip()) or changed
                if (_visible(node) and path == '/ac-start-polosa.htm'
                        and value.startswith('LeakBarrier') and 'Применение' in value):
                    changed = _append(product, 'description', value.split('Применение', 1)[0].strip()) or changed
                if _visible(node):
                    for sentence in _sentences(_public_text(node)):
                        if sentence.startswith('Для закрытия коньков крыш требуется специальный конек Shangle Ridge'):
                            if isinstance(product.get('compatibility'), dict):
                                product['compatibility']['Конек'] = sentence
                                changed = True
                            else:
                                changed = _append(product, 'compatibility', sentence) or changed
                if _visible(node) and urlsplit(page_url).path in ('/ac-start-polosa.htm', '/ac-naled.htm'):
                    instructions = []
                    collecting = False
                    for line in node.select('span.xr_tl'):
                        text = _public_text(line)
                        if text in ('Монтаж', 'Установка'):
                            collecting = True
                            continue
                        if collecting and text in ('Свойства', 'Свойства и преимущества', 'Характеристики', 'Гарантия'):
                            break
                        if collecting and _visible(line):
                            instructions.append(text)
                    if instructions:
                        product['instructions_manuals'] = ' '.join(instructions).replace('•', '\n-').strip()
                        changed = True
                    if _public_text(node).startswith('Изоляция от наледи укладывается в ендовы'):
                        changed = _append(product, 'applications', _public_text(node)) or changed
                if _visible(node) and _public_text(node).startswith('Для всех серий мягкой черепицы CertainTeed, за исключением линий'):
                    value = _public_text(node)
                    if isinstance(product.get('compatibility'), dict):
                        changed = product['compatibility'].get('Общие условия выбора') != value or changed
                        product['compatibility']['Общие условия выбора'] = value
                    else:
                        changed = _append(product, 'compatibility', value) or changed
        return changed
    if host == 'recon-rec.ru' and urlsplit(page_url).path.startswith('/veimix-vep-'):
        # Подписанный публичный раздел: сохраняем готовность к применению и
        # возможность доработки технологии с заказчиком без пересказа моделью.
        for label in soup.select('.industify_fn_without_sidebar_page .inner p strong'):
            if _norm(_public_text(label)) != _norm('Технология приготовления и применения') or not _visible(label):
                continue
            paragraphs = []
            for node in label.parent.find_next_siblings():
                if node.name == 'p' and node.find('strong'):
                    break
                if node.name == 'p' and _visible(node):
                    paragraphs.append(re.sub(r'\s+', ' ', _public_text(node)).strip())
            if paragraphs:
                value = '\n\n'.join(paragraphs)
                changed = product.get('instructions_manuals') != value or changed
                product['instructions_manuals'] = value
        for row in soup.select('.industify_fn_without_sidebar_page .inner tr'):
            cells = row.find_all(['th', 'td'], recursive=False)
            if len(cells) == 4 and _visible(row) and _norm(_public_text(cells[1])) == 'температура хранения':
                changed = _append(product, 'storage', 'Температура хранения: ' + _public_text(cells[3]) + ' ' + _public_text(cells[2])) or changed
        return changed
    if host == 'monoplus.ru' and urlsplit(page_url).path.startswith('/produkciya/'):
        for node in soup.select('.column__center-indent p'):
            if _visible(node):
                value = _public_text(node).lstrip('* ')
                if value == 'Возможна нарезка плит по размерам заказчика.':
                    changed = _append(product, 'advantages', value) or changed
        return changed
    if host == 'ascold.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        specs = product.get('specifications')
        if isinstance(specs, dict):
            for row in soup.select('.product-info__row'):
                label = row.select_one('.product-info__label')
                value = row.select_one('.product-info__value')
                if not _visible(label) or not _visible(value):
                    continue
                key, text = _public_text(label), _public_text(value)
                if key == 'Обозначение по чертежу' and text:
                    changed = specs.get(key) != text or changed
                    specs[key] = text
    elif host == 'belcolor.ru' and urlsplit(page_url).path.startswith('/catalog/'):
        # Явные подписанные разделы полного описания без пересказа условий.
        fields = {'СОСТАВ': 'description', 'ХАРАКТЕРИСТИКА': 'description',
                  'НАЗНАЧЕНИЕ': 'applications', 'СПОСОБ ПРИМЕНЕНИЯ': 'instructions_manuals',
                  'ХРАНЕНИЕ': 'storage', 'МЕРЫ ПРЕДОСТОРОЖНОСТИ': 'security_measures'}
        values = {}
        for root in soup.select('.bx_item_description'):
            if not _visible(root):
                continue
            for label in root.find_all('b', recursive=False):
                field = fields.get(_public_text(label))
                if not field or not _visible(label):
                    continue
                parts = []
                for node in label.next_siblings:
                    if getattr(node, 'name', None) == 'b':
                        break
                    if getattr(node, 'name', None):
                        if _visible(node):
                            parts.append(_public_text(node))
                    else:
                        parts.append(str(node))
                value = re.sub(r'\s+', ' ', ' '.join(parts)).strip().lstrip('. ')
                if value:
                    values.setdefault(field, []).append(value)
        for field, paragraphs in values.items():
            value = '\n\n'.join(paragraphs)
            changed = product.get(field) != value or changed
            product[field] = value
    elif host == 'gbi-grand.ru' and urlsplit(page_url).path.startswith('/product/'):
        for caption in soup.select('.text_after_items table caption'):
            if _visible(caption):
                changed = _append(product, 'description', _public_text(caption)) or changed
        for node in soup.select('.text_after_items p'):
            if _visible(node):
                text = _public_text(node)
                if 'П-образную форму' in text and 'бетонными плитами' in text:
                    changed = _append(product, 'compatibility', text) or changed
                if 'сложного грунта' in text and 'сейсмически опасных' in text:
                    changed = _append(product, 'advantages', text) or changed
    if host in ('ascold.ru', 'belcolor.ru', 'gbi-grand.ru'):
        return changed
    if host in ('ecovod.ru', 'nppnfl.ru', 'bksm.net'):
        selector, attr = (('main .detail-text img[src], main .carousel-item img[src]', 'src')
                          if host == 'ecovod.ru' else ('.MagicToolboxContainer a[href]', 'href'))
        if host == 'bksm.net':
            selector, attr = '#gallery .products-item-images a[href]', 'href'
        gallery = []
        for node in soup.select(selector):
            if not _visible(node):
                continue
            value = urljoin(page_url, node[attr]); parsed = urlsplit(value)
            if (parsed.scheme in ('https', 'http') and (parsed.hostname or '').removeprefix('www.') == host
                    and re.search(r'\.(?:png|jpe?g|webp|gif)$', parsed.path, re.I)
                    and (host != 'nppnfl.ru' or '/original/' in parsed.path) and value not in gallery):
                gallery.append(value)
        if gallery:
            changed = product.get('images') != gallery
            product['images'] = gallery
    if host == 'bksm.net':
        return changed
    if host == 'ecovod.ru' and urlsplit(page_url).path.rstrip('/') == '/barabannye-filtry':
        # Условия и регламент ТО не должны исчезать при сокращении моделью.
        fields = {
            'Режимы работы установок БФ': 'exploitation',
            'Основные требования к монтажу и эксплуатации барабанных фильтров': 'instructions_manuals',
            'Требования к транспортировке и хранению': 'storage',
            'Техническое обслуживание': 'exploitation',
            'Заводские гарантии': 'exploitation',
        }
        for heading in soup.select('main .detail-text h2'):
            field = fields.get(_public_text(heading))
            if not field or not _visible(heading):
                continue
            for node in heading.find_next_siblings():
                if node.name == 'h2':
                    break
                if node.name in ('p', 'ul', 'ol') and _visible(node):
                    changed = _append(product, field, _public_text(node)) or changed
        from product_tables import _grid, TableStructureError
        from text_extractor import MarkdownConverter
        converter = MarkdownConverter()
        specs = product.get('specifications')
        if isinstance(specs, dict):
            for table in soup.select('main .detail-text table'):
                if not _visible(table):
                    continue
                try:
                    _, grid = _grid(table, converter)
                except TableStructureError:
                    continue
                rows = [[converter._cell_to_text(cell) if cell is not None and _visible(cell) else '' for cell in row] for row in grid]
                if (len(rows) < 4 or len(rows[0]) != 10 or
                        rows[0][0] != 'Содержание частиц/температура воды' or
                        not rows[0][1].startswith('Прозор сетки') or
                        not all(re.fullmatch(r'\d+', model) for model in rows[1][2:])):
                    continue
                conditions = {}
                for row in rows[3:]:
                    if len(row) != 10 or not row[0] or not row[1]:
                        continue
                    key = row[0] + '; ' + rows[0][1] + ': ' + row[1]
                    conditions[key] = {model: value + ' м^3/час' for model, value in zip(rows[1][2:], row[2:]) if value}
                if conditions:
                    changed = specs.get('Производительность по условиям фильтрации') != conditions or changed
                    specs['Производительность по условиям фильтрации'] = conditions
    elif host == 'parad.by':
        paragraphs = [_public_text(node) for node in soup.select('.user_product_card .user_product_col_1 > p') if _visible(node)]
        if paragraphs:
            description = '\n\n'.join(paragraphs)
            changed = product.get('description') != description or changed
            product['description'] = description
        label = soup.select_one('.user_card_of_goods h2')
        if _visible(label):
            text = _public_text(label)
            changed = _append(product, 'description', 'Обозначение производителя: ' + text) or changed
    elif host == 'stabiterm.ru':
        if not re.fullmatch(r'/stabiterm-\d{3}(?:-[^/]+)?/?', urlsplit(page_url).path):
            return False
        for node in soup.select('article .et_pb_text_inner p, article .et_pb_tab_content p, article .et_pb_blurb_description p'):
            if not _visible(node):
                continue
            text = _public_text(node)
            if 'сертификат' in text and 'ТР ЕАЭС' in text:
                changed = _append(product, 'security_measures', text) or changed
            if text.startswith('Время высыхания до степени 3'):
                changed = _append(product, 'instructions_manuals', text) or changed
            if 'Относится к конструктивной огнезащите согласно требованиям' in text:
                # Номера пунктов содержат точки, поэтому сохраняем абзац целиком.
                changed = _append(product, 'description', text) or changed
    elif host == 'nppnfl.ru':
        for key, value in visible_nfl_icons(soup, page_url).items():
            if key == 'Срок службы':
                changed = _append(product, 'exploitation', key + ': ' + value) or changed
            elif isinstance(product.get('specifications'), dict):
                changed = product['specifications'].get(key) != value or changed
                product['specifications'][key] = value
        panel = soup.select_one('#tab-description')
        if _visible(panel) and 'Цена указана без ламп.' in _public_text(panel):
            old = product.get('complectation') or ''
            if isinstance(old, str):
                product['complectation'] = re.sub(r'Цена указана без ламп\.?', 'Лампы в комплект поставки не включены.', old)
                changed = _append(product, 'complectation', 'Лампы в комплект поставки не включены.') or changed
    elif host == 'polikor.net':
        if urlsplit(page_url).path == '/katalog/gorelochnye-kamni/gorelochnye-kamni-gmg.html':
            tables = [table for table in soup.select('#sp-component table') if _visible(table)]
            note = next((_public_text(p) for p in soup.select('#sp-component p, #sp-component span')
                         if _visible(p) and _public_text(p).startswith('Температура начала размягчения материала не равна')), '')
            if len(tables) == 2 and note:
                own = {}
                for row in tables[0].select('tr'):
                    cells = row.find_all('td', recursive=False)
                    if len(cells) == 2:
                        key, value = map(_public_text, cells)
                        own[key] = value
                # Не переносить свойства предлагаемых материалов в ТХ готового
                # камня. Срабатывает только для явно ненормированных размеров.
                if own.get('Габаритные размеры') == 'По чертежу выбранного исполнения' and own.get('Масса') == 'По технической документации':
                    burners = own.pop('Марки горелок', '')
                    product['specifications'] = own
                    product['dimensions'] = ''
                    product['weight'] = ''
                    changed = True
                    if burners:
                        _append(product, 'compatibility', 'Марки горелок: ' + burners)
                    for row in tables[1].select('tr'):
                        cells = row.find_all('td', recursive=False)
                        if len(cells) == 2:
                            _append(product, 'compatibility', _public_text(cells[0]) + ': температура начала размягчения ' + _public_text(cells[1]))
                    _append(product, 'compatibility', note)
        if urlsplit(page_url).path == '/katalog/korundovye-poroshki.html':
            tables = _polikor_powder_tables(soup)
            if tables:
                product['specifications'] = tables
                changed = True
            for node in soup.select('#k2Container li'):
                value = _public_text(node)
                if _visible(node) and value.startswith('степень расширения раствора'):
                    changed = _append(product, 'advantages', value) or changed
            for node in soup.select('#k2Container p'):
                value = _public_text(node)
                if (_visible(node) and value.startswith('Огнеупорные муллитокорундовые смеси')
                        and '(мертели)' in value and 'не имеющие срока годности' in value):
                    changed = _append(product, 'description', value) or changed
                    if not product.get('storage') or (isinstance(product.get('storage'), str) and _norm(product['storage']) == 'не имеют срока годности'):
                        product['storage'] = 'Огнеупорные муллитокорундовые смеси (мертели) не имеют срока годности.'
                        changed = True
                if _visible(node) and value.startswith('Заполнители предназначены для изготовления'):
                    changed = _append(product, 'applications', value) or changed
                if _visible(node) and value.startswith('Заполнитель – неформованный огнеупорный материал'):
                    changed = _append(product, 'description', value) or changed
            specifications = product.get('specifications', {})
            for row in soup.select('#content-tab4 tr'):
                if not _visible(row):
                    continue
                cells = row.find_all('td', recursive=False)
                if not cells:
                    continue
                text = _public_text(row)
                if len(cells) == 1 and ('техническим требованиям' in text or 'пластификатором ЛСТ' in text):
                    changed = _append(product, 'advantages', text) or changed
                if text.startswith('Распределение зерна по фракциям:') and len(cells) == 5:
                    labels = [_public_text(p) for p in cells[0].find_all('p')]
                    for mark, cell in zip(('К', 'К-1'), cells[2:4]):
                        grade = specifications.get('Корундовый порошок шлифовальный', {}).get(mark)
                        values = [_public_text(p) for p in cell.find_all('p')]
                        if isinstance(grade, dict) and len(labels) == len(values) == 4:
                            for key in list(grade):
                                if key.startswith('Распределение зерна по фракциям:'):
                                    grade.pop(key)
                            for label, value in zip(labels, values):
                                grade['Распределение зерна по фракциям: ' + label.removeprefix('Распределение зерна по фракциям:').strip() + ', %'] = value
                            changed = True
                if text.startswith('Зерновой состав:') and len(cells) == 4:
                    labels = [_public_text(p) for p in cells[0].find_all('p')][1:]
                    units = [_public_text(p) for p in cells[1].find_all('p')]
                    for mark, cell in zip(('ММК-85', 'ММК-72'), cells[2:]):
                        grade = specifications.get('Мертель', {}).get(mark)
                        values = [_public_text(p) for p in cell.find_all('p')]
                        if isinstance(grade, dict) and len(labels) == len(units) == len(values) == 3:
                            for key in list(grade):
                                if key.startswith('Зерновой состав:'):
                                    grade.pop(key)
                            for label, unit, value in zip(labels, units, values):
                                if value != '-':
                                    grade['Зерновой состав: ' + label + ', ' + unit] = value
                            changed = True
            grade = product.get('specifications', {}).get('Корундовый порошок шлифовальный', {}).get('КП')
            for cell in soup.select('#k2Container td[rowspan="4"]'):
                first = cell.parent.find_all('td', recursive=False)
                if not _visible(cell) or not first or 'Na2O' not in _public_text(first[0]) or not isinstance(grade, dict):
                    continue
                keys = [key for key in grade if key.startswith('Содержание примесей:') and any(x in key for x in ('Na2O', 'CaO', 'MgO', 'SiO2'))]
                if len(keys) == 4:
                    value = _public_text(cell)
                    for key in keys:
                        grade.pop(key)
                    grade['Содержание примесей: щелочь в пересчете на Na2O, CaO, MgO, SiO2 (общая ячейка таблицы), %, не более'] = value
                    changed = True
        for node in soup.select('#k2Container span'):
            if not _visible(node):
                continue
            value = _public_text(node)
            if value == 'Изготовление продукции по индивидуальным размерам':
                changed = _append(product, 'advantages', value) or changed
            elif value == 'Индивидуальный подбор огнеупорного материала':
                changed = _append(product, 'advantages', value) or changed
        if urlsplit(page_url).path == '/catalog/tigli-i-chashi/tigli-dlya-svintsa.html':
            paragraphs = [_public_text(node) for node in soup.select('#k2Container #content-tab1 p') if _visible(node)]
            if paragraphs:
                description = '\n\n'.join(paragraphs)
                changed = product.get('description') != description or changed
                product['description'] = description
    elif host == 'ufapolimer.ru':
        root = soup.select_one('#primary .type-page')
        path = urlsplit(page_url).path.rstrip('/')
        if root is None or path not in ('/29-2', '/plastikovye-emkosti/bunkery-plastikovye/bunker-obemom-1400-l-dlya-zhidkostej-kod-b1400'):
            return False
        for node in root.find_all('p'):
            if not _visible(node):
                continue
            text = _public_text(node)
            if text.startswith(('Трубы футерованные фторопластом', 'Бункер на')):
                changed = _append(product, 'description', text) or changed
            if path == '/29-2' and text.startswith('Фторопласт — полимерный материал'):
                changed = _append(product, 'description', text) or changed
            if text.startswith('К бункеру прилагается следующая документация:'):
                changed = _append(product, 'complectation', text) or changed
            for sentence in _sentences(text):
                sentence = re.sub(r'^ПРИМЕЧАНИЕ:\s*', '', sentence)
                if re.match(r'^(?:Допускается футеровка труб длиной|Трубы длиной более|По согласованию с потребителем допускается выпуск)', sentence):
                    changed = _append(product, 'complectation', sentence) or changed
                if sentence.startswith('На бункере может быть установлен'):
                    # Не оставляем безусловную комплектацию рядом с опубликованной опцией.
                    old = product.get('complectation') or ''
                    if isinstance(old, str):
                        lines = [line for line in old.splitlines() if not re.search(r'шаровый кран|транспортная заглушка', line, re.I) or _norm(line.lstrip('- ')) == _norm(sentence)]
                        product['complectation'] = '\n'.join(lines)
                        changed = product['complectation'] != old or changed
                    changed = _append(product, 'complectation', sentence) or changed
                if sentence.startswith('Бункеры могут отгружаться'):
                    changed = _append(product, 'complectation', sentence) or changed
    elif host == 'pergamin.ru':
        root = soup.select_one('.main .product-tabs')
        if root is None or not soup.select_one('.main .product-price'):
            return False
        for node in root.select('#description p'):
            if _visible(node):
                text = _public_text(node)
                if text.startswith('Мастика холодная МБР-Х-65') and 'растворителя' in text:
                    changed = product.get('description') != text or changed
                    product['description'] = text
        from text_extractor import MarkdownConverter
        converter = MarkdownConverter()
        specs = product.get('specifications')
        if not isinstance(specs, dict):
            return False
        for table in root.find_all('table'):
            if not _visible(table):
                continue
            rows = [row.find_all(['td', 'th'], recursive=False) for row in table.find_all('tr')]
            if not rows or len(rows[0]) not in (3, 4):
                continue
            headers = [converter._cell_to_text(c) for c in rows[0]]
            if headers[1].casefold() != 'единица измерения':
                continue
            for cells in rows[1:]:
                if len(cells) != len(headers) or not all(_visible(c) for c in cells):
                    continue
                label, unit, *values = [converter._cell_to_text(c) for c in cells]
                for index, value in enumerate(values, 2):
                    if not label or not value:
                        continue
                    if label.casefold() in ('тара', 'фасовка'):
                        changed = _append(product, 'complectation', value + (' ' + unit if unit and unit != 'Шт.' else '')) or changed
                        continue
                    target = specs if len(headers) == 3 else specs.setdefault(headers[index], {})
                    if not isinstance(target, dict):
                        continue
                    key = label + (', ' + unit if unit else '')
                    for old in list(target):
                        if _norm(old).startswith(_norm(label)):
                            del target[old]
                    target[key] = value
                    changed = True
    elif host == 'xotpipe.ru':
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
    elif host == '1marka.ru':
        panel = soup.select_one('#prod-detail-chars')
        control = soup.select_one('.tab-link[data-target="#prod-detail-chars"]')
        if panel is None or not _visible(control):
            return False
        # Вкладка доступна через публичный переключатель. Снимаем скрытие только
        # с её корня; скрытые строки/вложенные служебные узлы остаются запрещены.
        panel = deepcopy(panel)
        panel.attrs.pop('hidden', None)
        panel.attrs.pop('style', None)
        packaging = []
        for row in panel.select('.prod-detail-chars__row'):
            label = row.select_one('.prod-detail-chars__label')
            value = row.select_one('.prod-detail-chars__value')
            if not _visible(label) or not _visible(value):
                continue
            text = _public_text(value)
            key = _public_text(label)
            if re.search(r'упаков', key, re.I) and 'без упаковки' not in key.casefold() and text:
                packaging.append(f'{key}: {text}')
            elif _norm(key) in ('рама', 'ножки', 'слив-перелив', 'подголовник', 'ручки',
                                'гидромассаж', 'аэромассаж', 'подводная подсветка', 'хромотерапия') and text:
                changed = _append(product, 'compatibility', f'{key}: {text}') or changed
            elif _norm(key) in ('слив в центре ванны', 'сиденье-полочка', 'для двоих', 'количество человек') and text:
                specs = product.get('specifications')
                if isinstance(specs, dict):
                    changed = specs.get(key) != text or changed
                    specs[key] = text
            if (_norm(_public_text(label)) == 'дополнительная информация'
                    and re.fullmatch(r'регулируемая высота \d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)? см', text, re.I)):
                changed = _append(product, 'exploitation', text) or changed
        if packaging and isinstance(product.get('complectation', ''), str):
            # Явные строки таблицы имеют приоритет над пропуском/пересказом модели.
            old = product.get('complectation') or ''
            keep = [line for line in old.splitlines() if not re.match(
                r'^[-• ]*(?:длина|ширина|глубина|высота|об[ъь]ем|вес|масса|материал)\s+(?:в\s+)?упаков', line, re.I)]
            product['complectation'] = '\n'.join(keep + ['- ' + item for item in packaging])
            changed = product['complectation'] != old or changed
        description = soup.select_one('#prod-detail-text')
        if description is not None and _visible(soup.select_one('.tab-link[data-target="#prod-detail-text"]')):
            description = deepcopy(description)
            description.attrs.pop('hidden', None)
            description.attrs.pop('style', None)
            for node in description.find_all('p'):
                if not _visible(node):
                    continue
                text = _public_text(node)
                if re.search(r'ножки регулируются по высоте', text, re.I):
                    changed = _append(product, 'exploitation', text) or changed
                elif re.search(r'креплениям.+фиксируют ванну к стене', text, re.I):
                    changed = _append(product, 'security_measures', text) or changed
                elif text.startswith('Рамы, которые мы производим,'):
                    # Общие сведения об отдельно приобретаемой раме не становятся ТХ ванны.
                    text = text.replace('Рамы, которые мы производим,', 'Рамы производителя', 1)
                    changed = _append(product, 'compatibility', text) or changed
                    specs = product.get('specifications', {})
                    if isinstance(specs, dict):
                        for key in list(specs):
                            if re.search(r'запас прочности рамы', key, re.I):
                                specs.pop(key)
                                changed = True
        gallery = []
        for img in soup.select('.prod-detail__main-row .swiper-slide img'):
            if not _visible(img):
                continue
            for attr in ('src', 'data-src'):
                value = urljoin(page_url, img.get(attr, '')) if img.get(attr) else ''
                if urlsplit(value).scheme in ('https', 'http') and value not in gallery:
                    gallery.append(value)
        if gallery and isinstance(product.get('images', []), list):
            changed = gallery != product.get('images') or changed
            product['images'] = gallery
    elif host == '2vv.cz':
        owners = soup.select('.tm-product-body')
        if len(owners) != 1 or not _visible(soup.find('h1')):
            return False
        specs = product.get('specifications')
        if not isinstance(specs, dict):
            return False
        # В публичном описании есть ограничения монтажа, которые модель
        # нестабильно сокращала. Сохраняем собственные видимые абзацы целиком.
        paragraphs = [_public_text(node) for node in owners[0].find_all('p') if _visible(node)]
        description = '\n\n'.join(text for text in paragraphs if text)
        if description:
            changed = product.get('description') != description or changed
            product['description'] = description
        # Оригиналы из публичной галереи: модель иногда заменяла латинскую x
        # на кириллическую х в URL. Превью видеороликов не являются фотографиями.
        gallery = []
        for link in owners[0].select('a[data-type="image"][href]'):
            if not _visible(link) or not _visible(link.find('img')):
                continue
            value = urljoin(page_url, link['href'])
            parsed = urlsplit(value)
            if (parsed.scheme in ('http', 'https') and re.search(r'\.(?:png|jpe?g|webp|gif|avif|svg)$', parsed.path, re.I)
                    and value not in gallery):
                gallery.append(value)
        if gallery and isinstance(product.get('images', []), list):
            changed = gallery != product.get('images') or changed
            product['images'] = gallery
        # Сохраняем опубликованное исполнение корпуса, которое модель теряла
        # в скрытом additional_info. Числовой гейт не считает «Конструкцию» ТХ.
        for node in owners[0].find_all('p'):
            if not _visible(node):
                continue
            paragraph = _public_text(node)
            # Назначение должно сохранять ограничение однозонной вентиляции,
            # а не только перечисленные моделью типы помещений.
            if re.search(r'\bmonozonní větrání\b', paragraph, re.I):
                changed = _append(product, 'applications', paragraph) or changed
            for sentence in _sentences(paragraph):
                if re.search(r'\bAirGENIO\b', sentence, re.I) and re.search(r'\bBMS\b', sentence):
                    compatibility = product.get('compatibility')
                    if isinstance(compatibility, dict):
                        changed = compatibility.get('Система управления') != sentence or changed
                        compatibility['Система управления'] = sentence
                    else:
                        changed = _append(product, 'compatibility', sentence) or changed
                russian = (re.match(r'^Визуальная часть установки\b', sentence, re.I)
                           and re.search(r'часть корпуса.+оцинкованн', sentence, re.I))
                czech = (re.match(r'^Pohledová část jednotky\b', sentence, re.I)
                         and re.search(r'část pláště.+pozink', sentence, re.I))
                if russian or czech:
                    changed = _append(specs, 'Конструкция', sentence) or changed
                    colors = product.get('colors')
                    if isinstance(colors, list):
                        # Покрытие корпуса не является отдельным цветовым вариантом.
                        keep = [c for c in colors if not re.match(
                            r'^(?:pozink|оцинк|galvani[sz])', str(c).strip(), re.I)]
                        changed = changed or keep != colors
                        product['colors'] = keep
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
