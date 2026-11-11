"""Таблицы проверенных DOM: значения копируются из HTML, минуя генерацию LLM.

Модуль не принимает решение о товарности. Каждое семейство сохраняет свою
шапку, строки и сноски; неизвестная/неоднозначная структура не уплощается.
"""
from collections import Counter
import re
import unicodedata
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from product_tables import _grid, cell_text, TableStructureError


DOM_SITES = {'bsdbel.ru', 'ventexpress.ru', 'nzmk22.ru', 'npo-stekloplastic.ru',
             'xotpipe.ru', 'landshaft.ru', 'hms-livgidromash.ru'}

HMS_TABLES = '#catalogElement .props_list, #marki-harakteristiki-nasosa .marki-table'


def _host(url):
    return (urlparse(url).hostname or '').lower().removeprefix('www.')


def _ui_available(element, soup, url, seen=()):
    """Скрытый блок допустим только при явном управлении вкладкой/аккордеоном.

    Это проверка опубликованного DOM, не замена живой визуальной приёмке.
    Данные script/template и недоступные скрытые заготовки не являются UI.
    """
    if id(element) in seen:
        return False
    seen = (*seen, id(element))
    for node in [element, *element.parents]:
        if getattr(node, 'name', None) in {'script', 'style', 'template', 'noscript'}:
            return False
        if not hasattr(node, 'get'):
            continue
        style = re.sub(r'\s+', '', node.get('style', '')).casefold()
        hidden = (node.has_attr('hidden') or node.get('aria-hidden') == 'true'
                  or 'display:none' in style or 'visibility:hidden' in style)
        if not hidden:
            continue
        identifier = node.get('id')
        controlled = bool(identifier and any(
            (control.get('href') == '#' + identifier
             or control.get('data-target') == '#' + identifier
             or control.get('data-bs-target') == '#' + identifier
             or identifier in control.get('aria-controls', '').split())
            and _ui_available(control, soup, url, seen)
            for control in soup.select('a, button, [role="tab"], .elementor-tab-title')))
        if not controlled:
            # Тот же строгий контракт опубликованного слайдера, что и
            # Markdown-путь; локальный импорт избегает цикла с inline_products.
            from product_interactions import public_slider_panels
            controlled = any(node is panel for panel in public_slider_panels(soup, url))
        if not controlled:
            return False
    return True


def _label(cell, converter):
    return cell_text(cell, converter).replace('\n', ' ') if cell else ''


def _unique(labels):
    counts = Counter(labels)
    return [label + (f' / столбец {i + 1}' if counts[label] > 1 else '')
            for i, label in enumerate(labels)]


def _paragraph_slots(cell, converter):
    if cell is None:
        return []
    container = cell.select_one('.td-content') or cell
    paragraphs = container.find_all('p', recursive=False)
    return [cell_text(paragraph, converter) for paragraph in paragraphs]


def _conditional_rows(row, converter):
    """Пустой p сохраняет позицию условия; не сдвигаем числа между строками."""
    labels = _paragraph_slots(row[0], converter)
    if len(labels) < 2 or not all(re.match(r'^[—–-]\s*(?:при|к)\b', label) for label in labels):
        return None
    columns = [labels]
    for cell in row[1:]:
        slots = _paragraph_slots(cell, converter)
        if not cell or not cell_text(cell, converter):
            slots = [''] * len(labels)
        if len(slots) != len(labels):
            return None  # Источник не доказывает соответствие отдельных строк.
        columns.append(slots)
    return [[column[i] for column in columns] for i in range(len(labels))]


def describe_source_table(table, converter, host):
    """Разворачивает rowspan/colspan, сохраняя позиции повторных заголовков."""
    if table.find('table'):
        return None
    rows, grid = _grid(table, converter)
    if len(rows) < 2 or not grid or len(grid[0]) < 2:
        return None
    width = len(grid[0])
    title, notes = '', []
    start = 0
    first = rows[0].find_all(['td', 'th'], recursive=False)
    if len(first) == 1 and converter._span(first[0], 'colspan') >= width:
        title = _label(first[0], converter)
        start = 1
    if start >= len(rows):
        return None
    # НЗМК: марки в горизонтальной строке, свойства идут по вертикали.
    if host == 'nzmk22.ru' and 'table-product' in table.get('class', []):
        name_row = grid[start]
        property_headers = sum(bool(re.match(r'^(?:Об[ъь]ем|Класс бетона|Масса|Габаритные размеры)\b',
                                             _label(cell, converter), re.I))
                               for cell in name_row[1:] if cell)
        if (re.fullmatch(r'Марка изделия', _label(name_row[0], converter), re.I)
                and property_headers < 2):
            labels, values, notes = [], [], []
            offset = converter._span(name_row[0], 'colspan')
            model_columns, model_cells = [], set()
            for column in range(offset, width):
                cell = name_row[column]
                if cell is not None and _label(cell, converter) and id(cell) not in model_cells:
                    model_columns.append(column)
                    model_cells.add(id(cell))
            names = [cell_text(name_row[column], converter) for column in model_columns]
            section = ''
            for tr, row in zip(rows[start + 1:], grid[start + 1:]):
                own = tr.find_all(['td', 'th'], recursive=False)
                if len(own) == 1 and converter._span(own[0], 'colspan') >= width:
                    section = cell_text(own[0], converter)
                    notes.append(section)
                    continue
                pieces = list(dict.fromkeys(_label(c, converter) for c in row[:offset] if c))
                label = ': '.join(pieces)
                if not label:
                    continue
                if section and re.fullmatch(r'[A-Za-zА-Яа-я]', label):
                    label = section + ': ' + label
                else:
                    section = ''
                labels.append(label)
                values.append([cell_text(row[column], converter) if row[column] else ''
                               for column in model_columns])
            if names and values and all(len(v) == len(names) for v in values):
                return dict(title=title, headers=['Марка изделия'] + _unique(labels),
                            rows=[[name] + [v[i] for v in values] for i, name in enumerate(names)], notes=notes)
            raise TableStructureError('Неоднозначное соответствие марок и колонок НЗМК')
    depth = start + 1
    # Явная thead, либо шапка с rowspan/colspan (BSD, НЗМК).
    thead = table.find('thead', recursive=False)
    if thead:
        depth = max(depth, len(thead.find_all('tr', recursive=False)))
    else:
        for i in range(start, depth):
            depth = max(depth, *(i + converter._span(c, 'rowspan')
                                for c in rows[i].find_all(['td', 'th'], recursive=False)))
        if any(converter._span(c, 'colspan') > 1 for c in rows[start].find_all(['td','th'], recursive=False)):
            depth = max(depth, start + 2)
    if depth >= len(rows):
        return None
    while depth < len(rows) and not any(_label(cell, converter) for cell in grid[depth]):
        depth += 1
    if depth >= len(rows):
        return None
    if (host == 'npo-stekloplastic.ru' and grid[depth][0] is not None
            and not _label(grid[depth][0], converter)
            and converter._span(grid[depth][0], 'rowspan') > 1
            and any(_label(cell, converter) for cell in grid[depth][1:])):
        depth += converter._span(grid[depth][0], 'rowspan')
    if depth >= len(rows):
        return None
    group_subheader = (host == 'npo-stekloplastic.ru' and _label(grid[depth][0], converter) == 'Группа')
    if group_subheader:
        depth += 1
    # Elementor помещает буквенные исполнения под общей шапкой в первую
    # строку tbody. Пустой левый угол и явные буквенные коды — продолжение
    # шапки, а не физический параметр с пустым названием.
    if (depth < len(rows) and host == 'npo-stekloplastic.ru' and thead
            and not _label(grid[depth][0], converter)):
        names = [_label(cell, converter) for cell in grid[depth][1:]]
        short_names = all(re.fullmatch(r'[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9-]{0,8}', name)
                          for name in names)
        grouped_names = (any(converter._span(cell, 'colspan') > 1
                             for cell in rows[start].find_all(['td', 'th'], recursive=False))
                         and all(re.fullmatch(r'[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9()\s,./+−–—-]*', name)
                                 for name in names))
        if short_names or grouped_names:
            depth += 1
    if depth >= len(rows):
        return None
    headers = []
    for col in range(width):
        path, seen = [], set()
        for row in grid[start:depth]:
            cell = row[col]
            if cell is not None and id(cell) not in seen:
                seen.add(id(cell))
                text = _label(cell, converter)
                if group_subheader and row is grid[depth - 1]:
                    text = ('Группа ' + text) if col else ''
                if text and text not in path:
                    path.append(text)
        if len(path) > 1 and path[0].casefold() in {'значение', 'значение показателя'}:
            path = path[1:]
        headers.append(': '.join(path) or f'Столбец {col + 1}')
    headers = _unique(headers)
    body, body_cells = [], []
    for tr, row in zip(rows[depth:], grid[depth:]):
        own = tr.find_all(['td', 'th'], recursive=False)
        if len(own) == 1 and (converter._span(own[0], 'colspan') >= width
                              or (converter._span(own[0], 'colspan') > 1
                                  and cell_text(own[0], converter).startswith('*'))):
            notes.append(cell_text(own[0], converter))
            continue
        values = [cell_text(c, converter) if c else '' for c in row]
        if any(values):
            logical = _conditional_rows(row, converter) if host == 'npo-stekloplastic.ru' else None
            body.extend(logical or [values])
            body_cells.extend([row] * len(logical) if logical else [row])
    if not body:
        return None
    # Ошибочный colspan в Elementor создаёт пустые проекции без собственных
    # ячеек данных. Одинаковые значения в разных td остаются разными колонками.
    if host in {'npo-stekloplastic.ru', 'nzmk22.ru'}:
        active = []
        for i in range(width):
            if i and not any(row[i] is not None for row in body_cells):
                continue
            if any(grid[depth - 1][i] is grid[depth - 1][j]
                   and all(row[i] is row[j] for row in body_cells) for j in active):
                continue
            active.append(i)
        headers = _unique([re.sub(r' / столбец \d+$', '', headers[i]) for i in active])
        body = [[row[i] for i in active] for row in body]
        width = len(active)
    # Elementor: две колонки «параметр → значение», первая строка в tbody
    # является характеристикой, а не шапкой.
    if ((host == 'npo-stekloplastic.ru' and title and width == 2)
            or (host == 'xotpipe.ru' and not thead and width == 2)
            or (host == 'hms-livgidromash.ru' and 'props_list' in table.get('class', []) and width == 2)):
        body = []
        for row in grid[start:]:
            selected = [row[i] for i in active] if host == 'npo-stekloplastic.ru' else row
            values = [cell_text(c, converter) if c else '' for c in selected]
            logical = _conditional_rows(selected, converter) if host == 'npo-stekloplastic.ru' else None
            body.extend(logical or [values])
        headers = ['Характеристика', 'Значение']
    # BSD публикует марки и соответствующие расходы металла в абзацах
    # одной физической строки. Делим только равные по длине колонки;
    # общие многострочные поля (В30 / М400) остаются общими.
    if host == 'bsdbel.ru' and headers[0] == 'Марка изделия':
        expanded = []
        for row in body:
            names = row[0].splitlines()
            if len(names) > 1:
                columns = [value.splitlines() for value in row]
                expanded.extend([[parts[i] if len(parts) == len(names) else row[c]
                                  for c, parts in enumerate(columns)]
                                 for i in range(len(names))])
            else:
                expanded.append(row)
        body = expanded
    return dict(title=title, headers=headers, rows=body, notes=notes)


def _nzmk_table_notes(table, data):
    """Явные технические абзацы после собственной таблицы до следующей секции."""
    owners = list(dict.fromkeys(row[0] for row in data['rows'] if row[0]))
    notes, normative, tu_refs, conditions = [], [], [], []
    for sibling in table.next_siblings:
        name = getattr(sibling, 'name', None)
        if name in {'table', 'h1', 'h2', 'h3', 'h4', 'h5'}:
            break
        if name != 'p':
            continue
        text = sibling.get_text(' ', strip=True)
        if not re.match(r'^(?:Расч[её]тная нагрузка|Конструкции балок|Балка|Изготовление балки|Армирование|При транспортировке)', text):
            continue
        scoped = ', '.join(owners) + ': ' + text
        references = re.findall(r'\bТУ\s+\d[\d.\s–—-]*', text)
        if references:
            tu_refs.extend(reference.strip() for reference in references)
        if re.search(r'изготавливается\s+(?:по|согласно)', text, re.I) and not references:
            normative.append(scoped)
        else:
            notes.append(scoped)
        condition = re.search(r'эксплуатац[а-я]+\s+(.+)', text, re.I)
        if condition and len(condition[1]) >= 20:
            conditions.append((owners, condition[1]))
    return dict(data, notes=data['notes'] + notes, _normative_lines=normative,
                _tu_refs=tu_refs, _scoped_conditions=conditions)


def _common_nzmk_purpose(soup, url):
    """Общее назначение лежит в том же опубликованном slider group, до состояний."""
    if _host(url) != 'nzmk22.ru':
        return []
    from product_interactions import public_slider_panels
    panels = public_slider_panels(soup, url)
    control = soup.select_one('.breams-slider .slider21')
    if not panels or control is None:
        return []
    for parent in control.parents:
        if parent.find('h2', recursive=False) is None:
            continue
        if not all(any(ancestor is parent for ancestor in panel.parents) for panel in panels):
            continue
        purposes = []
        for child in parent.children:
            if getattr(child, 'name', None) is None:
                continue
            if child is control or child.select_one('.breams-slider .slider21') is control:
                break
            if (child.name == 'p' and _ui_available(child, soup, url)
                    and re.match(r'^(?:Предназначен[аоы]?|Назначение)\b', child.get_text(' ', strip=True), re.I)):
                purposes.append(child)
        return purposes
    return []


def _npo_caption_context(table, caption, soup, url):
    """Own h2 + соседние sections до своей table, не за предыдущую table."""
    section = table.find_parent('section', class_='elementor-top-section')
    if section is None:
        return []
    context = []
    brand = _semantic_label(re.sub(r'^Технические характеристики\s+', '', caption, flags=re.I))
    for sibling in section.previous_siblings:
        if getattr(sibling, 'name', None) != 'section':
            continue
        if sibling.select_one('table.eael-data-table'):
            break
        if not _ui_available(sibling, soup, url):
            continue
        context.insert(0, sibling)
        heading = sibling.select_one('h1,h2,h3,h4')
        if heading:
            tokens = [_semantic_label(token) for token in heading.get_text(' ', strip=True).split()]
            if any(token == brand or (len(token) >= 3 and re.fullmatch(re.escape(token) + r'\d+', brand))
                   for token in tokens if token):
                return context
            break
    return []


def extract_source_tables(html, url):
    """Извлекает только таблицы известных товарных областей DOM."""
    host = _host(url)
    prefixes = {'bsdbel.ru': '/catalog/', 'ventexpress.ru': '/produkciya/',
                'nzmk22.ru': '/products/', 'npo-stekloplastic.ru': '/products/',
                'xotpipe.ru': '/product/', 'landshaft.ru': '/product/',
                'hms-livgidromash.ru': '/catalog/'}
    if host not in DOM_SITES or not urlparse(url).path.startswith(prefixes[host]):
        return []
    from text_extractor import MarkdownConverter
    converter = MarkdownConverter()
    soup = BeautifulSoup(html, 'lxml')
    selectors = {'nzmk22.ru': 'table.table-product',
                 'npo-stekloplastic.ru': 'table.eael-data-table',
                 'hms-livgidromash.ru': HMS_TABLES}
    tables = soup.select(selectors.get(host, 'table'))
    result = []
    for index, table in enumerate(tables):
        if not _ui_available(table, soup, url):
            continue
        data = describe_source_table(table, converter, host)
        if data:
            if host == 'hms-livgidromash.ru':
                # В том числе &tilde; после разбора HTML; знак сохраняется в CP1251.
                data['headers'] = [value.replace('˜', '~') for value in data['headers']]
                data['rows'] = [[value.replace('˜', '~') for value in row] for row in data['rows']]
            previous = table.find_previous(['h1', 'h2', 'h3', 'h4'])
            if not data['title'] or data['title'] == 'Характеристика изделия':
                data['title'] = (previous.get_text(' ', strip=True) if previous else '') or f'Таблица {index + 1}'
            if host == 'hms-livgidromash.ru' and 'props_list' in table.get('class', []):
                data['title'] = 'Характеристики изделия'
            data['source_index'] = index
            if host == 'nzmk22.ru':
                data = _nzmk_table_notes(table, data)
            result.append(data)
    if host == 'xotpipe.ru':
        modifications = []
        for heading in soup.select('.fusion_builder_column_inner h3'):
            name = heading.get_text(' ', strip=True)
            block = heading.find_parent(class_='fusion_builder_column_inner')
            if block and name and _ui_available(block, soup, url) and re.search(r'Модификация', block.get_text(' ', strip=True), re.I):
                texts = [p.get_text(' ', strip=True) for p in block.select('h4, .fusion-text, .fusion-li-item-content')
                         if _ui_available(p, soup, url)]
                description = '\n'.join(text for text in texts if text)
                if description:
                    modifications.append([name, description])
        if modifications:
            result.append(dict(title='Модификации', headers=['Модификация', 'Описание'],
                               rows=modifications, notes=[], source_index=-1))
    return result


def compact_source_tables(html, url):
    """Для LLM оставляет шапку и одну строку; полный HTML остаётся источником.

    Использовать только вместе с восстановлением таблиц после классификации.
    Объём ответа не зависит от числа исполнений в исходной таблице.
    """
    tables = extract_source_tables(html, url)
    if not tables:
        return html
    soup = BeautifulSoup(html, 'lxml')
    host = _host(url)
    selector = {'nzmk22.ru': 'table.table-product', 'npo-stekloplastic.ru': 'table.eael-data-table',
                'hms-livgidromash.ru': HMS_TABLES}.get(host, 'table')
    source = soup.select(selector)
    for data in tables:
        if (data['source_index'] < 0 or len(data['rows']) <= 20
                or len(data['headers']) < 3
                or data['headers'] == ['Характеристика', 'Значение']
                or _model_property_table(data, url) is not data):
            continue
        table = soup.new_tag('table')
        for values in [data['headers'], data['rows'][0]]:
            row = soup.new_tag('tr')
            for value in values:
                cell = soup.new_tag('td')
                cell.string = value
                row.append(cell)
            table.append(row)
        note = soup.new_tag('p')
        note.string = '\n'.join(data['notes']) + f" Полная исходная таблица «{data['title']}»: {len(data['rows'])} строк. Значения сохраняются программно; не разворачивать таблицу в JSON."
        source[data['source_index']].insert_before(note)
        source[data['source_index']].replace_with(table)
    return str(soup)


def _model_property_table(data, url):
    """Матрица Elementor: имя модели становится контейнером, не ключом ТХ.

    Единицы остаются частью подписи физического параметра. Числа в именах
    моделей и колонках единиц не превращаются в самостоятельные значения.
    Исходный extract_source_tables возвращает исходную ориентацию для сверки.
    """
    if _host(url) != 'npo-stekloplastic.ru':
        return data
    headers = data['headers']
    normalize = lambda value: re.sub(r'[^а-яa-z0-9]+', '', value.casefold())
    first = normalize(headers[0])
    unit_columns = [i for i, header in enumerate(headers[1:], 1)
                    if normalize(header) in {'едизм', 'едизмерения', 'единицаизмерения', 'единицыизмерения'}]
    generic_value = lambda value: bool(re.fullmatch(r'значение(?:показателя)?(?:столбец\d+)?', normalize(value)))
    generic_pair = len(headers) == 2 and generic_value(headers[1])
    parameter_axis = (generic_pair or first in {'наименованиепоказателей', 'показатель', 'характеристика', 'столбец1'}
                      or bool(unit_columns)
                      or (first == 'наименованиепоказателя'
                          and all(',' not in header and not re.search(r'[σεσ%]', header)
                                  for header in headers[1:]))
                      or (first == 'наименование'
                          and all(re.match(r'Показатель для .+ типа:', header)
                                  for header in headers[1:])))
    if not parameter_axis:
        return data
    model_columns = [i for i in range(1, len(headers)) if i not in unit_columns]
    if not model_columns:
        raise TableStructureError('В матрице параметров нет модели или значения')
    properties, source_rows, section = [], [], ''
    for number, row in enumerate(data['rows'], 1):
        if row[0] and not any(row[i] for i in model_columns):
            section = row[0]
            continue
        label = row[0] or f'Строка {number}'
        condition = (re.fullmatch(r'[+−-]?\s*\d+(?:[.,]\d+)?\s*[°º]?\s*[CСКK]', label)
                     or re.match(r'^[—–-]\s*(?:при|к)\b', label)
                     or re.fullmatch(r'(?:[A-Z][a-z]?\d*[²³]?)+(?:[-+](?:ион)?)?(?:,\s*%)?', label))
        if section and condition:
            stem, separator, unit = section.rpartition(',')
            label = (stem + ': ' + label + (', ' + unit.strip() if not _property_signature(label)[1] else '')
                     if separator and _unit(unit) else section + ': ' + label)
        else:
            section = ''
        source_rows.append(row)
        units = [row[i] for i in unit_columns if row[i]]
        properties.append(label + (', ' + ', '.join(units) if units else ''))
    properties = _unique(properties)
    if generic_pair:
        return dict(data, headers=['Характеристика', 'Значение'],
                    rows=[[label, row[1]] for label, row in zip(properties, source_rows)])
    return dict(data, headers=['Модель / исполнение'] + properties,
                rows=[[headers[i]] + [row[i] for row in source_rows] for i in model_columns])


def _semantic_label(value):
    """Только типографика: части, условия и нормативные ссылки остаются."""
    text = unicodedata.normalize('NFKC', str(value).replace('º', '°').replace('˚', '°'))
    text = re.sub(r'(?<=\d)\s*°?\s*[CС]\b', 'c', text, flags=re.I)
    text = text.casefold().replace('ё', 'е')
    text = re.sub(r'(?<=\d)\s*[×xх*]\s*(?=\d)', 'х', text)
    return re.sub(r'[^а-яa-z0-9]+', '', text)


def _unit(value):
    value = value.replace('º', '°').replace('˚', '°')
    value = re.sub(r'(?<![A-Za-zА-Яа-я])°?[CС](?![A-Za-zА-Яа-я])', '°с', value)
    text = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е').strip()
    text = re.sub(r'\^(?=[+−-]?\d)', '', text)
    text = re.sub(r'\s*[xх]\s*', '*', text)
    text = re.sub(r'\s+', '', text).replace('·', '*').replace('•', '*').replace('×', '*')
    atoms = r'ом|кгс|мкм|км|мм|см|мг|кг|кн|гпа|мпа|кпа|квт|кдж|суток|сут|час|сек|мин|об|шт|mm|cm|kg|mpa|kpa|pa|м|г|т|н|па|вт|дж|с|ч|к|m|g|n|w|s|h|k|%'
    remainder = re.sub(atoms, '', text)
    if remainder == text or re.search(r'[^0-9^/*().°+−-]', remainder):
        return None
    return text.rstrip('.')


def _property_signature(label):
    label = unicodedata.normalize('NFKC', str(label).replace('º', '°').replace('˚', '°'))
    unit = None
    # Обособленная единица в подписи не удаляет часть/условие свойства.
    for match in list(re.finditer(r',\s*([^,]+)', label))[::-1]:
        candidate = _unit(match[1])
        if candidate:
            unit = candidate
            label = label[:match.start()] + label[match.end():]
            break
    return _semantic_label(label), unit


def _value_unit(value):
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    for position in re.finditer(r'(?<![A-Za-zА-Яа-яё])(?=[A-Za-zА-Яа-яё°%])', text):
        if re.search(r'\d', text[:position.start()]):
            unit = _unit(text[position.start():])
            if unit:
                return unit
    return None


def _money_column(label):
    return bool(re.search(r'цен[аы]|стоимост|(?:\bруб\.?|₽|\bRUB\b)', str(label), re.I))


def _first_column_property(data):
    return bool(re.match(r'^(?:Размер|Габарит|Диаметр|Длина|Ширина|Высота|Толщина|Масса|Вес)\b',
                         data['headers'][0], re.I))


def _separate_source_metadata(product, tables):
    """ТУ — в tu, прочие ссылки — в нормативный блок, пояснения — в notes."""
    technical = []
    normative = {'нд', 'нтд', 'норматив', 'нормативныйдокумент',
                 'проектнаядокция', 'проектнаядокументация'}
    for data in tables:
        removed, notes, routed = set(), list(data['notes']), {}
        for i, label in enumerate(data['headers'][1:], 1):
            kind = _semantic_label(label)
            if kind not in normative | {'примечание', 'примечания'}:
                continue
            removed.add(i)
            rows = [(row[0], row[i]) for row in data['rows'] if row[i].strip() not in {'', '-', '—'}]
            values = list(dict.fromkeys(value for _, value in rows))
            routed[kind] = [_semantic_label(value) for value in values]
            lines = values if len(values) == 1 else [name + ': ' + value for name, value in rows]
            if kind in normative:
                tu_rows = [(name, value) for name, value in rows
                           if re.match(r'^\s*ТУ(?=\s|\d)', value, re.I)
                           and not re.search(r'\b(?:ГОСТ|СП|СТО|СНиП|серия)\b', value, re.I)]
                other_rows = [(name, value) for name, value in rows if (name, value) not in tu_rows]
                tu_values = list(dict.fromkeys(value for _, value in tu_rows))
                tu_lines = (tu_values if len(tu_values) == 1 and len(tu_rows) == len(data['rows'])
                            else [name + ': ' + value for name, value in tu_rows])
                previous = product.get('tu') or ''
                if isinstance(previous, str):
                    for line in tu_lines:
                        if _semantic_label(line) not in _semantic_label(previous):
                            previous = (previous + '\n' + line).strip()
                    product['tu'] = previous
                other_values = list(dict.fromkeys(value for _, value in other_rows))
                if other_rows:
                    normative_lines = (other_values if len(other_values) == 1 and len(other_rows) == len(data['rows'])
                                       else [name + ': ' + value for name, value in other_rows])
                    data = dict(data, _normative_lines=data.get('_normative_lines', []) + normative_lines)
            else:
                notes.extend(line for line in lines if line not in notes)
        keep = [i for i in range(len(data['headers'])) if i not in removed]
        technical.append(dict(data, headers=[data['headers'][i] for i in keep],
                              rows=[[row[i] for i in keep] for row in data['rows']], notes=notes,
                              _routed_values=routed))
    return technical


def _separate_source_prices(product, tables):
    """Цена сохраняет исходную строку модели и условия количества вне ТХ."""
    price_lines = []
    technical = []
    for data in tables:
        monetary = [i for i, label in enumerate(data['headers']) if i and _money_column(label)]
        if not monetary:
            technical.append(data)
            continue
        for row in data['rows']:
            values = [f"{data['headers'][i]}: {row[i]}" for i in monetary if row[i]]
            if values:
                price_lines.append(f"{data['title']} / {data['headers'][0]}: {row[0]}; " + '; '.join(values))
        keep = [i for i in range(len(data['headers'])) if i not in monetary]
        technical.append(dict(data, headers=[data['headers'][i] for i in keep],
                              rows=[[row[i] for i in keep] for row in data['rows']]))
    if price_lines:
        previous = product.get('price') or ''
        if isinstance(previous, str):
            if re.fullmatch(r'[\d\s.,]+', previous):
                previous = ''  # Уточняем одиночную цену полным опубликованным соответствием.
            lines = previous.splitlines()
            seen = {_semantic_label(line) for line in lines}
            for line in price_lines:
                if _semantic_label(line) not in seen:
                    lines.append(line)
                    seen.add(_semantic_label(line))
            product['price'] = '\n'.join(lines)
    return technical


def restore_deterministic_tables(ai_data, html, url):
    """Добавляет полные таблицы в штатные specifications и _table_layouts."""
    product = ai_data.get('product')
    if not isinstance(product, dict):
        return []
    soup = BeautifulSoup(html, 'lxml')
    purposes = (soup.select('main[data-source-family] p[data-source-common-purpose]')
                if _host(url) == 'nzmk22.ru' else [])
    if purposes:
        source_text = '\n'.join(node.get_text(' ', strip=True) for node in purposes if _ui_available(node, soup, url))
        previous = product.get('applications') or ''
        heading = soup.find('h1')
        title = heading.get_text(' ', strip=True) if heading else ''
        if source_text and isinstance(previous, str):
            if not previous or _semantic_label(previous) in _semantic_label(title) or _semantic_label(previous) in _semantic_label(source_text):
                product['applications'] = source_text
            elif _semantic_label(source_text) not in _semantic_label(previous):
                product['applications'] = previous + '\n' + source_text
    tables = [_model_property_table(data, url) for data in extract_source_tables(html, url)]
    if not tables:
        return []
    conditions = {}
    all_owners = {row[0] for data in tables for row in data['rows']}
    for data in tables:
        previous_tu = product.get('tu') or ''
        if isinstance(previous_tu, str):
            for reference in data.get('_tu_refs', []):
                if _semantic_label(reference) not in _semantic_label(previous_tu):
                    previous_tu = (previous_tu + '\n' + reference).strip()
            product['tu'] = previous_tu
        for owners, text in data.get('_scoped_conditions', []):
            conditions.setdefault(_semantic_label(text), set()).update(owners)
    for field in ('description', 'exploitation'):
        text = product.get(field)
        if not isinstance(text, str):
            continue
        lines = []
        for line in text.splitlines():
            normalized = _semantic_label(line)
            for condition, owners in conditions.items():
                if (owners != all_owners and condition in normalized
                        and not any(_semantic_label(owner) in normalized for owner in all_owners)):
                    line = ', '.join(sorted(owners)) + ': ' + line
                    break
            lines.append(line)
        product[field] = '\n'.join(lines)
    tables = _separate_source_metadata(product, tables)
    tables = _separate_source_prices(product, tables)
    specs = product.setdefault('specifications', {})
    if not isinstance(specs, dict):
        raise TableStructureError('specifications должен быть объектом')
    norm = _semantic_label
    source = [data for data in tables if data['source_index'] >= 0
              and (len(data['headers']) > 1 or _first_column_property(data))]
    aliases = {}
    for data in source:
        if data['headers'] == ['Характеристика', 'Значение']:
            continue
        for row in data['rows']:
            name = row[0]
            short = name.rsplit(': ', 1)[-1]
            candidates = [name, re.sub(r':\s*Группа\s+', ': ', name), short]
            tokens = short.split()
            if len(tokens) > 1 and all(re.fullmatch(r'[A-Za-zА-Яа-я]\S*\d\S*', token) for token in tokens):
                candidates.extend(tokens)  # Два явных кода в одной исходной ячейке.
            for alias in candidates:
                aliases.setdefault(norm(alias), set()).add(norm(name))
    models = {alias for alias, owners in aliases.items() if alias and len(owners) == 1}
    routed = {}
    for data in tables:
        for key, values in data.get('_routed_values', {}).items():
            routed.setdefault(key, set()).update(values)
    identity_labels = {norm(data['headers'][0]) for data in source
                       if not _first_column_property(data)
                       and data['headers'] != ['Характеристика', 'Значение']}
    paths = {norm(f"{data['title']} / исходная таблица {i}") for i, data in enumerate(tables, 1)}
    properties = {}
    for data in source:
        labels = ([row[0] for row in data['rows']] if data['headers'] == ['Характеристика', 'Значение']
                  else data['headers'][1:])
        if _first_column_property(data):
            labels = [data['headers'][0], *labels]
        for label in labels:
            name, unit = _property_signature(label)
            properties.setdefault(name, set()).add(unit)
    generic = {norm(name) for name in ('Характеристики', 'Технические характеристики',
                                      'Параметры', 'Технические параметры')}

    def exact_cell(field, value):
        # Оператор можно перенести между подписью и значением, но не менять.
        if isinstance(value, (dict, list)):
            return None
        operators = re.findall(r'не\s+(?:более|менее)', str(field) + ' ' + str(value), re.I)
        operators = {re.sub(r'\s+', ' ', op.casefold()) for op in operators}
        if len(operators) > 1:
            return None
        clean_field = re.sub(r'не\s+(?:более|менее)', '', str(field), flags=re.I).strip(' ,')
        clean_value = re.sub(r'не\s+(?:более|менее)', '', str(value), flags=re.I)
        name, unit = _property_signature(clean_field)
        clean_value = unicodedata.normalize('NFKC', clean_value).casefold()
        clean_value = re.sub(r'\s+', '', clean_value).replace('−', '-').replace('—', '-')
        return name, unit, next(iter(operators), ''), clean_value

    source_cells, column_aliases = set(), {}
    for data in source:
        if data['headers'] == ['Характеристика', 'Значение']:
            source_cells.update(exact_cell(*row) for row in data['rows'])
            continue
        for row in data['rows']:
            cells = {exact_cell(label, value) for label, value in zip(data['headers'][1:], row[1:])}
            source_cells.update(cells)
            column = re.fullmatch(r'(.+) / столбец (\d+)', row[0])
            if column:
                alias = norm(column[1] + '_' + str(int(column[2]) - 1))
                column_aliases[alias] = cells

    def redundant(field, value):
        if exact_cell(field, value) in source_cells:
            return True
        name, declared_unit = _property_signature(field)
        if name not in properties:
            return False
        units = properties[name]
        actual_unit = declared_unit or _value_unit(value)
        return actual_unit in units or units == {None}

    def model_remainder(obj, prefix=('', None)):
        if not isinstance(obj, dict):
            return obj
        result = {}
        for key, value in obj.items():
            base, unit = _property_signature(key)
            signature = prefix if norm(key) in generic else (prefix[0] + base, unit or prefix[1])
            if isinstance(value, dict):
                cleaned = model_remainder(value, signature)
                if cleaned or not value:
                    result[key] = cleaned
                continue
            if (not prefix[0] and norm(key) in routed and not isinstance(value, list)
                    and (not value or norm(value) in routed[norm(key)])):
                continue
            units = properties.get(signature[0])
            actual_unit = signature[1] or _value_unit(value)
            if units and (actual_unit in units or units == {None}
                          or (actual_unit is None and len(units) == 1)):
                continue  # Только source-owned physical field, не весь model object.
            result[key] = value
        return result

    def remaining(obj, unowned=True):
        if not isinstance(obj, dict):
            return obj
        result = {}
        for key, value in obj.items():
            key_name = norm(key)
            if key_name in paths:
                continue
            if key_name in column_aliases and isinstance(value, dict):
                cells = column_aliases[key_name]
                physical = [(label, val) for label, val in value.items()
                            if not (norm(label) in {'нд', 'нтд', 'норматив', 'нормативныйдокумент'}
                                    and not isinstance(val, (dict, list))
                                    and any(norm(val) in values for values in routed.values()))]
                if len(physical) >= 3 and all(exact_cell(label, val) in cells for label, val in physical):
                    continue  # AI-суффикс _N точно совпал с одноимённым source-столбцом.
            if key_name in models and not isinstance(value, (dict, list)) and norm(value) == key_name:
                continue  # Повтор обозначения/размера в качестве собственного значения.
            if key_name in models and isinstance(value, dict):
                cleaned = model_remainder(value)
                if cleaned:
                    result[key] = cleaned
                continue
            if (key_name in identity_labels and not isinstance(value, (dict, list))
                    and norm(value) in models):
                continue
            if (unowned and key_name in routed and not isinstance(value, (dict, list))
                    and norm(value) in routed[key_name]):
                continue
            # Не переносим совпадение подписи между разными частями/условиями.
            if unowned and not isinstance(value, (dict, list)) and redundant(key, value):
                continue
            cleaned = remaining(value, key_name in generic)
            if cleaned or not value:
                result[key] = cleaned
        return result

    specs = remaining(specs)
    product['specifications'] = specs
    for number, data in enumerate(tables, 1):
        if data.get('_normative_lines'):
            specs[f"Нормативные документы / {data['title']} / таблица {number}"] = '\n'.join(data['_normative_lines'])
    notes = [norm(note) for data in tables for note in data['notes'] if note]
    if isinstance(product.get('variants'), list) and notes:
        product['variants'] = [variant for variant in product['variants']
                               if not any(note in norm(variant) for note in notes)]
    layouts = []
    for number, data in enumerate(tables, 1):
        if data['source_index'] < 0 and data['title'] == 'Модификации':
            variants = product.setdefault('variants', [])
            if isinstance(variants, list):
                source_names = {row[0] for row in data['rows']}
                variants[:] = [v for v in variants if not isinstance(v, dict)
                               or not any(str(value) in source_names for value in v.values())]
                variants.extend({'Исполнение': row[0], 'Описание': row[1]} for row in data['rows'])
            continue
        if len(data['headers']) == 1 and not _first_column_property(data):
            continue
        path = f"{data['title']} / исходная таблица {number}"
        if _host(url) == 'hms-livgidromash.ru':
            path = data['title']
        if data['headers'] == ['Характеристика', 'Значение']:
            specs[path] = dict(zip(_unique([row[0] for row in data['rows']]),
                                   [row[1] for row in data['rows']]))
            continue
        records = {}
        for i, values in enumerate(data['rows'], 1):
            name = values[0] or f'Строка {i}'
            if name in records:
                name += f' / строка {i}'
            records[name] = dict(zip(data['headers'][1:], values[1:]))
            if _first_column_property(data):
                records[name][data['headers'][0]] = values[0]
        specs[path] = records
        row_header = data['headers'][0]
        columns = data['headers'][1:]
        if not columns and len(records) > 1:
            records = {f'Строка {i}': attrs for i, attrs in enumerate(records.values(), 1)}
            specs[path] = records
            row_header = 'Строка'
            columns = [data['headers'][0]]
        layouts.append(dict(path=path, title=data['title'], row_header=row_header,
                            columns=columns, labels=columns, notes=data['notes']))
    if layouts:
        paths = {layout['path'] for layout in layouts}
        ai_data['_table_layouts'] = [layout for layout in ai_data.get('_table_layouts', [])
                                     if layout.get('path') not in paths] + layouts
    return layouts


def source_catalog_families(html, url):
    """Отдельные товарные секции смешанного каталога, явно озаглавленные DOM.

    Семейства одной многотабличной страницы с одинаковым заголовком остаются
    вместе. Текстовый фрагмент стабилен при перестановке таблиц на странице.
    """
    from copy import deepcopy
    from urllib.parse import quote, urlunparse
    host = _host(url)
    prefixes = {'bsdbel.ru': '/catalog/', 'ventexpress.ru': '/produkciya/',
                'nzmk22.ru': '/products/', 'npo-stekloplastic.ru': '/products/'}
    if host not in prefixes or not urlparse(url).path.startswith(prefixes[host]):
        return []
    soup = BeautifulSoup(html, 'lxml')
    if soup.select_one('main[data-source-family]'):
        return []
    if host == 'npo-stekloplastic.ru':
        # Четыре опубликованных Elementor-таблицы имеют собственные
        # именные caption в полной первой строке thead. Общие тексты каталога
        # и ссылки на документы не переносятся в отдельный товар.
        candidates = []
        for table in soup.select('table.eael-data-table'):
            if not _ui_available(table, soup, url):
                continue
            first = table.select_one('thead > tr')
            cells = first.find_all(['th', 'td'], recursive=False) if first else []
            if len(cells) != 1 or int(cells[0].get('colspan') or 1) < 2:
                continue
            title = cells[0].get_text(' ', strip=True)
            if not re.fullmatch(r'Технические характеристики\s+\S.+', title, re.I):
                continue
            candidates.append((title, table))
        if len(candidates) < 2 or len({title for title, _ in candidates}) != len(candidates):
            return []
        families = []
        for title, table in candidates:
            key = 'source-family=' + quote(title, safe='')
            result = BeautifulSoup('<html><head></head><body><main></main></body></html>', 'lxml')
            result.main['data-source-family'] = key
            context = _npo_caption_context(table, title, soup, url)
            if context:
                for section in context:
                    copied = deepcopy(section)
                    for noise in copied.select('script, style, form, button'):
                        noise.decompose()
                    result.main.append(copied)
                result.main.find(['h1', 'h2', 'h3', 'h4']).name = 'h1'
            else:
                heading = result.new_tag('h1'); heading.string = title; result.main.append(heading)
            result.main.append(deepcopy(table))
            families.append(dict(url=urlunparse(urlparse(url)._replace(fragment=key)),
                                 title=title, html=str(result)))
        return families
    groups = {}
    for table in soup.find_all('table'):
        if not _ui_available(table, soup, url):
            continue
        if host == 'nzmk22.ru' and 'table-product' not in table.get('class', []):
            continue
        heading = table.find_previous(['h1', 'h2', 'h3', 'h4'])
        if heading is None:
            continue
        name = heading.get_text(' ', strip=True)
        groups.setdefault(name, []).append((heading, table))
    # НЗМК описывает виды товарного бетона заголовками h5, без таблиц.
    text_groups = []
    if host == 'nzmk22.ru' and not groups:
        for heading in soup.select('.span9 > h5'):
            if not _ui_available(heading, soup, url):
                continue
            blocks = [heading]
            for sibling in heading.next_siblings:
                if getattr(sibling, 'name', None) in {'h1', 'h2', 'h3', 'h4', 'h5'}:
                    break
                if getattr(sibling, 'name', None) is not None:
                    blocks.append(sibling)
            text_groups.append((heading.get_text(' ', strip=True), blocks))
    if len(groups) < 2 and len(text_groups) < 2:
        return []
    if text_groups:
        families = []
        for name, blocks in text_groups:
            key = 'source-family=' + quote(name, safe='')
            result = BeautifulSoup('<html><head></head><body><main></main></body></html>', 'lxml')
            result.main['data-source-family'] = key
            for block in blocks:
                result.main.append(deepcopy(block))
            result.main.h5.name = 'h1'
            families.append(dict(url=urlunparse(urlparse(url)._replace(fragment=key)),
                                 title=name, html=str(result)))
        return families
    families = []
    for name, items in groups.items():
        key = 'source-family=' + quote(name, safe='')
        result = BeautifulSoup('<html><head></head><body><main></main></body></html>', 'lxml')
        result.main['data-source-family'] = key
        title = result.new_tag('h1'); title.string = name; result.main.append(title)
        if host == 'nzmk22.ru' and all(table.find_parent(class_='slider-data-container') for _, table in items):
            for purpose in _common_nzmk_purpose(soup, url):
                copied = deepcopy(purpose)
                copied['data-source-common-purpose'] = 'true'
                result.main.append(copied)
        headings = {id(h) for h, _ in items}
        # Только блоки, принадлежащие этой секции, вне самих таблиц.
        for block in soup.find_all(['p', 'img', 'table']):
            if block.find_parent(['table', 'p']):
                continue
            previous = block.find_previous(['h1', 'h2', 'h3', 'h4'])
            if previous is not None and id(previous) in headings and _ui_available(block, soup, url):
                result.main.append(deepcopy(block))
        families.append(dict(url=urlunparse(urlparse(url)._replace(fragment=key)),
                             title=name, html=str(result)))
    return families


def source_family_catalog_reason(html, url):
    """Известный DOM содержит самостоятельные товарные блоки, не один product.

    Виртуальный адрес сначала изолирует вызывающая сторона. Родительский
    смешанный каталог не становится карточкой даже при ошибочном AI product.
    """
    prefixes = {'bsdbel.ru': '/catalog/', 'ventexpress.ru': '/produkciya/',
                'nzmk22.ru': '/products/', 'npo-stekloplastic.ru': '/products/'}
    if _host(url) not in prefixes or not urlparse(url).path.startswith(prefixes[_host(url)]):
        return None
    if urlparse(url).fragment.startswith('source-family='):
        return None
    soup = BeautifulSoup(html, 'lxml')
    if soup.select_one('main[data-source-family]'):
        return None
    # В опубликованном Elementor-каталоге ссылки на готовые изделия и
    # сравнительная таблица базовых материалов не образуют один товар.
    if _host(url) == 'npo-stekloplastic.ru':
        base = urlparse(url)
        root = soup.select_one('.di-page-contents .elementor, [data-elementor-type="wp-page"]') or soup.new_tag('div')
        children = {}
        for heading in root.select('h2, h3'):
            for link in heading.find_all('a', href=True):
                target = urlparse(urljoin(url, link['href']))
                prefix = base.path.rstrip('/') + '/'
                suffix = target.path.removeprefix(prefix).strip('/')
                if (_host(target.geturl()) == _host(url)
                        and target.path.startswith(prefix) and suffix and '/' not in suffix
                        and _ui_available(heading, soup, url)):
                    children[target.path.rstrip('/')] = heading.get_text(' ', strip=True)
        comparative = any('сравнительные характеристики' in re.sub(r'\s+', ' ', text).casefold()
                          for text in root.stripped_strings)
        comparison = any(len(table.select('thead th')) >= 3
                         and not table.select_one('thead th').get_text(' ', strip=True)
                         and _ui_available(table, soup, url)
                         for table in root.find_all('table'))
        if len(children) >= 2 and comparative and comparison:
            return 'Каталог дочерних изделий со сравнением базовых материалов: ' + '; '.join(children.values())
    families = source_catalog_families(html, url)
    if len(families) < 2:
        return None
    return 'Самостоятельные товарные блоки: ' + '; '.join(family['title'] for family in families)
