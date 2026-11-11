"""Структура сложных таблиц четырёх производителей без изменения промпта LLM.

Значения хранятся в specifications. _table_layouts содержит только подписи и
пути к этим значениям и не входит в контракт GraphDBDataFormatter.
"""
from collections import Counter
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


_TABLE_SITES = {'t-sila.ru', 'uralgidromash.ru', 'sverdmash.ru',
                'vmp-holding.ru', 'vmp-anticor.ru'}


class TableStructureError(ValueError):
    """Распознанная таблица не может быть перенесена без потери данных."""


def is_table_site(url):
    return (urlparse(url).hostname or '').lower().removeprefix('www.') in _TABLE_SITES


def cell_text(cell, converter):
    """Блоки и br разделяются переносом; отступы HTML не становятся данными."""
    parts = []

    def walk(node):
        for child in node.children:
            name = getattr(child, 'name', None)
            if name is None:
                parts.append(re.sub(r'\s+', ' ', str(child)))
            elif name == 'sup':
                parts.append(converter.convert_sup(child, ''))
            elif name == 'br':
                parts.append('\n')
            else:
                block = name in {'p', 'div', 'li'}
                if block:
                    parts.append('\n')
                walk(child)
                if block:
                    parts.append('\n')

    walk(cell)
    return '\n'.join(line.strip() for line in ''.join(parts).splitlines() if line.strip())


def _grid(table, converter):
    """Только собственные строки таблицы; ширина учитывает продолжающийся rowspan."""
    rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
    grid = [[] for _ in rows]
    for i, row in enumerate(rows):
        j = 0
        for cell in row.find_all(['td', 'th'], recursive=False):
            while j < len(grid[i]) and grid[i][j] is not None:
                j += 1
            width = max(1, converter._span(cell, 'colspan'))
            height = max(1, converter._span(cell, 'rowspan'))
            for r in range(i, min(i + height, len(rows))):
                grid[r].extend([None] * max(0, j + width - len(grid[r])))
                for c in range(j, j + width):
                    if grid[r][c] is not None:
                        raise TableStructureError('Пересекающиеся объединённые ячейки')
                    grid[r][c] = cell
            j += width
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([None] * (width - len(row)))
    return rows, grid


def describe_table(table, converter):
    """Распознаём явную группу двигателя либо матрицу сушки по температурам."""
    if table.find('table'):
        return None
    first = table.find('tr')
    if first is None:
        return None
    cells = first.find_all(['td', 'th'], recursive=False)
    groups = [cell_text(c, converter).replace('\n', ' ') for c in cells]
    motor = any('электродвигател' in g.lower() and converter._span(c, 'colspan') > 1
                for g, c in zip(groups, cells))
    drying = (any('температур' in g.lower() and converter._span(c, 'colspan') > 1
                  for g, c in zip(groups, cells))
              and bool(groups) and 'степень' in groups[0].lower())
    if not (motor or drying):
        return None

    rows, grid = _grid(table, converter)
    width = len(grid[0])
    depth = 2
    r = 0
    while r < min(depth, len(rows)):
        depth = max(depth, *(r + max(1, converter._span(c, 'rowspan'))
                            for c in rows[r].find_all(['td', 'th'], recursive=False)))
        r += 1
    if depth >= len(rows):
        raise TableStructureError('У таблицы нет строк данных')

    headers, sections, labels = [], [], []
    for col in range(width):
        path, seen = [], set()
        for row in grid[:depth]:
            cell = row[col]
            if cell is not None and id(cell) not in seen:
                seen.add(id(cell))
                value = cell_text(cell, converter).replace('\n', ' ')
                if value and value not in path:
                    path.append(value)
        if drying:
            section = ''
            label = path[-1] if path else ''
        else:
            section = 'Электродвигатель' if any('электродвигател' in p.lower() for p in path) else 'Насос'
            top = grid[0][col]
            top_text = cell_text(top, converter).replace('\n', ' ') if top is not None else ''
            if (top is not None and converter._span(top, 'colspan') > 1
                    and re.search(r'электродвигател|насос', top_text, re.I)):
                path = [p for p in path if p != top_text]
            label = ': '.join(path)
            if 'агрегат' in label.lower():
                section = 'Агрегат'
        headers.append(': '.join(path) if drying else f'{section}: {label}')
        labels.append(label)
        sections.append(section)
    body, body_cells, notes, title = [], [], [], ''
    for tr, row in zip(rows[depth:], grid[depth:]):
        own = tr.find_all(['td', 'th'], recursive=False)
        if len(own) == 1 and converter._span(own[0], 'colspan') >= width:
            text = cell_text(own[0], converter)
            if not body and not text.startswith('*'):
                title = text
            elif text:
                notes.append(text)
            continue
        values = [cell_text(c, converter) if c is not None else '' for c in row]
        if not any(values):
            continue
        # Параллельная английская шапка Уралгидромаша не является режимом.
        if values[0].strip().lower() in {'type', 'designation'}:
            continue
        if not values[0].strip():
            raise TableStructureError('Строка без идентификатора')
        body.append(values)
        body_cells.append(row)

    # 50ВО: несколько логических строк в абзацах одной физической строки.
    if motor and len(body) == 1:
        parts = [v.splitlines() or [''] for v in body[0]]
        count = len(parts[0])
        if count > 1 and all(len(p) in (1, count) for p in parts):
            body = [[p[i] if len(p) > 1 else p[0] for p in parts] for i in range(count)]
            body_cells *= count
    if not body:
        raise TableStructureError('Пустая распознанная таблица')
    # Ошибочный colspan иногда создаёт пустые хвостовые столбцы (Техносила).
    # Частично заполненные столбцы сохраняем целиком.
    active = [i for i in range(width) if i == 0 or any(row[i] for row in body)]
    kept = []
    for i in active:
        # Проекции ОДНОЙ colspan-ячейки под ОДНИМ заголовком не являются
        # разными исходными значениями. Отдельные td, даже равные, сохраняем.
        if any(headers[i] == headers[j]
               and grid[depth - 1][i] is grid[depth - 1][j]
               and all(row[i] is None or row[i] is row[j] for row in body_cells)
               for j in kept):
            continue
        kept.append(i)
    active = kept
    duplicate = Counter(headers[i] for i in active)
    for i in active:
        if duplicate[headers[i]] > 1:
            same = [j for j in active if headers[j] == headers[i]]
            if not all(grid[depth - 1][j] is grid[depth - 1][i] for j in same):
                raise TableStructureError('Неоднозначные заголовки столбцов')
    # Один исходный заголовок на две различные колонки не даёт права их слить
    # или придумать значение подписи. Оставляем явную исходную позицию.
    for i in active:
        if duplicate[headers[i]] > 1:
            headers[i] += f' / столбец {i + 1}'
            labels[i] += f' / столбец {i + 1}'
    headers = [headers[i] for i in active]
    labels = [labels[i] for i in active]
    sections = [sections[i] for i in active]
    body = [[row[i] for i in active] for row in body]
    if any(not label for label in labels):
        raise TableStructureError('Столбец данных без заголовка')
    return {'headers': headers, 'labels': labels, 'sections': sections, 'rows': body,
            'title': title, 'notes': notes, 'drying': drying}


def _md_cell(value):
    return value.replace('|', r'\|').replace('\n', '<br>') or ' '


def structured_table_markdown(table, converter):
    data = describe_table(table, converter)
    if data is None:
        return None
    lines = []
    if data['title']:
        lines.append('### ' + data['title'].replace('\n', ' '))
    lines.append('| ' + ' | '.join(_md_cell(x) for x in data['headers']) + ' |')
    lines.append('| ' + ' | '.join('---' for _ in data['headers']) + ' |')
    lines.extend('| ' + ' | '.join(_md_cell(x) for x in row) + ' |' for row in data['rows'])
    lines.extend('\n' + note for note in data['notes'])
    return '\n\n' + '\n'.join(lines) + '\n\n'


def _norm(value):
    return re.sub(r'\s+', ' ', str(value).replace('ё', 'е').replace('Ё', 'Е')).strip().lower()


def _record_keys(rows, headers):
    names = [next((value for header, value in zip(headers[1:], row[1:])
                   if re.search(r'Марка насоса|Обозначение', header) and value.strip() not in ('', '-')), row[0])
             if row[0].strip() in ('-', '—') else row[0] for row in rows]
    counts = Counter(_norm(name) for name in names)
    keys, used = [], set()
    for i, (row, name) in enumerate(zip(rows, names)):
        key = name
        if counts[_norm(name)] > 1:
            motor = next((v for h, v in zip(headers[1:], row[1:])
                          if h.startswith('Электродвигатель:') and re.search(r': (Тип|Марка)$', h)), '')
            if motor:
                key += ' / ' + motor.replace('\n', ' / ')
        if key in used:
            speed = next((f'{h}: {v}' for h, v in zip(headers[1:], row[1:])
                          if re.search(r'част.*вращ', h, re.I) and v), '')
            if speed:
                key += ' / ' + speed.replace('\n', ' / ')
        while key in used:
            key += f' / строка {i + 1}'
        used.add(key)
        keys.append(key)
    return keys


def _flatten_record(obj, prefix=''):
    pairs = []
    for key, value in obj.items():
        name = f'{prefix} → {key}' if prefix else key
        if isinstance(value, dict):
            pairs.extend(_flatten_record(value, name) or [(name, '')])
        else:
            pairs.append((name, value))
    return pairs


def take_record_lists(specs, prefix=''):
    """Списки объектов — строки таблицы; списки скаляров остаются перечнями."""
    remaining, tables = {}, []
    for key, value in specs.items():
        title = f'{prefix} / {key}' if prefix else key
        if isinstance(value, list) and len(value) > 1 and all(isinstance(v, dict) for v in value):
            records = {}
            for i, item in enumerate(value, 1):
                attrs = dict(_flatten_record(item))
                motor = next((str(v) for k, v in attrs.items()
                              if re.search(r'(?:Электродвигатель: Марка|Марка электродвигателя)$', k)), '')
                name = f'{key} / {motor}' if motor else f'{key} / комплектация {i}'
                while name in records:
                    name += f' / комплектация {i}'
                records[name] = attrs
            columns = list(dict.fromkeys(k for attrs in records.values() for k in attrs))
            sections = {}
            for column in columns:
                group, sep, label = column.partition(': ')
                section = group if sep and group in ('Насос', 'Насосы', 'Электродвигатель', 'Электродвигатели') else ''
                sections.setdefault(section, []).append(column)
            for section, keys in sections.items():
                tables.append((records, {'title': f'{title} / {section}' if section else title,
                                         'row_header': 'Модель / комплектация', 'columns': keys,
                                         'labels': [k.partition(': ')[2] if section else k for k in keys]}))
        elif isinstance(value, dict):
            nested, extracted = take_record_lists(value, title)
            if nested or not extracted:
                remaining[key] = nested
            tables.extend(extracted)
        else:
            remaining[key] = value
    return remaining, tables


def restore_source_tables(ai_data, html, url):
    """После решения LLM о товарности заменяем распознанные таблицы данными HTML.

    Остальные характеристики и поля товара остаются прежними. При ошибке
    структура не сохраняется как успешная: вызывающий код повторит страницу.
    """
    if not is_table_site(url) or not isinstance(ai_data.get('product'), dict):
        return []
    from text_extractor import MarkdownConverter, clean_noise
    converter = MarkdownConverter()
    soup = clean_noise(BeautifulSoup(html, 'lxml'), url)
    tables = [d for t in soup.find_all('table') if (d := describe_table(t, converter)) is not None]
    product = ai_data['product']
    specs = product.get('specifications', {})
    if not isinstance(specs, dict):
        raise TableStructureError('specifications должен быть объектом')
    bases = {_norm(name) for t in tables for row in t['rows']
             for i, value in enumerate(row)
             if (i == 0 or re.search(r'Марка насоса|Обозначение', t['labels'][i]))
             and value.strip() not in ('', '-')
             for name in (value, value.replace('\n', ' / '))}

    def owner(key):
        text = _norm(key)
        if re.match(r'^насосы?(?:\s+[^:]+)?:', text):
            return 'Насос'
        if re.search(r'электродвигател', text):
            return 'Электродвигатель'
        if re.search(r'агрегат', text):
            return 'Агрегат'
        if (text in ('насос', 'насосы', 'характеристики насоса')
                or '(насос)' in text or 'характеристика насоса' in text):
            return 'Насос'
        return None

    def alias(key):
        text = re.sub(r'^(?:насосы?(?:\s+[^:]+)?|электродвигатели?|электродвигатель|агрегат):\s*', '', _norm(key))
        text = re.sub(r' / столбец \d+$', '', text)
        text = re.sub(r'\b(?:насоса|электродвигателя|агрегата)\b', '', text)
        return re.sub(r'[\s:()]', '', text)

    def value_key(value):
        return re.sub(r'\s+', '', str(value)).replace('×', 'х').replace('x', 'х').replace('–', '-')

    known, source_values = {}, set()
    unowned = any(t['drying'] or 'Насос' in t['sections'][1:] for t in tables)
    for table in tables:
        for i, header in enumerate(table['headers'][1:], 1):
            values = {value_key(row[i]) for row in table['rows']}
            source_values.update(values)
            for label in {header, table['labels'][i]}:
                known.setdefault(alias(label), []).append((table['sections'][i], values))

    def remove_fields(obj, context=None):
        if isinstance(obj, list):
            # Списки скаляров не являются строками параметров.
            if not all(isinstance(v, dict) for v in obj):
                return obj
            return [item for v in obj if (item := remove_fields(v, context))]
        if not isinstance(obj, dict):
            return obj
        result = {}
        for field, value in obj.items():
            field_owner = owner(field) or context
            if isinstance(value, (dict, list)):
                cleaned = remove_fields(value, field_owner)
                if cleaned or not value:
                    result[field] = cleaned
                continue
            matches = known.get(alias(field), [])
            if not matches:
                # LLM нумерует проекции одной ячейки с ошибочным colspan.
                # Удаляем копию только при явной группе и совпадении с HTML.
                unnumbered = re.sub(r'\s+\(\d+\)$', '', field)
                if unnumbered != field and field_owner is not None:
                    matches = [(group, values) for group, values in known.get(alias(unnumbered), [])
                               if value_key(value) in values]
            if any((field_owner is not None and field_owner == group)
                   or (field_owner is None and unowned and value_key(value) in values)
                   for group, values in matches):
                continue
            result[field] = value
        return result

    def remove_old(obj, context=None):
        if not isinstance(obj, dict):
            return obj
        result = {}
        for key, value in obj.items():
            norm = _norm(key)
            if any(norm == base or norm.startswith(base + ' (') or norm.startswith(base + ' /')
                   for base in bases):
                cleaned = remove_fields(value, context or owner(key))
                if isinstance(cleaned, (dict, list)):
                    if cleaned:
                        result[key] = cleaned
                elif value_key(cleaned) not in source_values:
                    result[key] = cleaned
                continue
            cleaned = remove_old(value, owner(key) or context)
            if cleaned != {} or value == {}:
                result[key] = cleaned
        return result

    updated = remove_old(specs)
    layouts = []
    for table_index, data in enumerate(tables):
        keys = _record_keys(data['rows'], data['headers'])
        sections = [''] if data['drying'] else list(dict.fromkeys(data['sections'][1:]))
        for section in sections:
            indices = [i for i in range(1, len(data['headers'])) if data['sections'][i] == section]
            title = (data['title'] or 'Сушка покрытия') if data['drying'] else {
                'Насос': 'Характеристики насоса', 'Электродвигатель': 'Характеристики электродвигателя',
                'Агрегат': 'Параметры агрегата'}[section]
            path = title
            while path in updated:
                path += f' / таблица {table_index + 1}'
            columns = [data['headers'][i] for i in indices]
            updated[path] = {key: {data['headers'][i]: row[i] for i in indices}
                             for key, row in zip(keys, data['rows'])}
            layouts.append({'path': path, 'title': title, 'row_header': data['labels'][0],
                            'columns': columns, 'labels': [data['labels'][i] for i in indices],
                            'column_axis': data['headers'][1].rsplit(': ', 1)[0] if data['drying'] else '',
                            'notes': [note for note in data['notes']
                                      if not any(_norm(note).lstrip('* ') in _norm(value)
                                                 for value in product.values() if isinstance(value, str))]
                            if section == sections[0] else []})
    updated, record_tables = take_record_lists(updated)
    for records, layout in record_tables:
        path = layout['title']
        while path in updated:
            path += ' / таблица'
        updated[path] = {name: {column: attrs.get(column, '') for column in layout['columns']}
                         for name, attrs in records.items()}
        layouts.append(dict(layout, path=path))
    if layouts:
        product['specifications'] = updated
        ai_data['_table_layouts'] = layouts
    return layouts
