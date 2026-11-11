"""Детерминированные правила блоков карточки после извлечения моделью."""
import re
import unicodedata


_PALLET = re.compile(r'\b(?:поддон\w*|паллет\w*|палл?ет\w*)\b', re.I)
_QUANTITY = re.compile(r'количеств\w*|кол[. -]?во|(?:упаков|рулон|короб|мешк|штук|шт\b)', re.I)
_LINK = re.compile(r'(?<!!)\[([^\]\n]+)\]\(([^\s)]+)(?:\s+"[^\"]*")?\)')
_DOCUMENT = re.compile(r'\.(?:pdf|docx?|xlsx?|rtf)(?:[?#]|$)|/(?:documentfile|documents)/', re.I)
_DOCUMENT_TITLE = re.compile(r'^(?:инструкци\w*|рекомендаци\w*|техническ\w+\s+лист|альбом\b|технологическ\w+\s+регламент|сертификат\w*|паспорт\b)', re.I)


def _norm(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    return re.sub(r'[^\w]+', ' ', text).strip()


def is_pallet_quantity(label):
    """Количество на паллете — комплектация независимо от исходного блока."""
    return bool(_PALLET.search(str(label)) and _QUANTITY.search(str(label)))


def _fact_key(text):
    """Равенство полных фактов сохраняет знак, диапазон, единицы и контекст."""
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    text = re.sub(r'^[\s•]+|^[-*]\s+', '', text)
    text = text.replace('–', '-').replace('—', '-')
    text = re.sub(r'\s+', ' ', text).strip().rstrip('.;')
    # Единица из заголовка относится к голому значению: «Срок службы, лет: 30».
    # Контекст до подписи и уточнения после величины сохраняются полностью.
    text = re.sub(
        r'^(.*(?:срок (?:службы|эксплуатации|хранения)|срок годности)),\s*'
        r'(лет|год|года|месяцев|месяца|месяц|суток|сутки|часов|часа|час|ч)\s*:\s*'
        r'([+-]?\d+(?:[.,]\d+)?)$', r'\1: \3 \2', text)
    return re.sub(r'\s*:\s*', ' ', text)


def _profile_field(label):
    label = _fact_key(label)
    if re.match(r'^(?:(?:гарантийный|допустимый|максимальный) )?'
                r'(?:срок (?:хранения|годности)|условия хранения|температура хранения)\b', label):
        return 'storage'
    if re.match(r'^(?:(?:гарантийный|средний|расчетный|минимальный|максимальный) )?'
                r'срок (?:службы|эксплуатации)\b|^температура окружающ\w* среды\b', label):
        return 'exploitation'
    return None


def _route_explicit_fields(product):
    """Только однозначные подписи; свободный остаток классифицирует модель."""
    def walk(obj, parents=()):
        if not isinstance(obj, dict):
            return
        for key in list(obj):
            value = obj[key]
            if isinstance(value, dict):
                walk(value, (*parents, str(key)))
                continue
            if not isinstance(value, (str, int, float)):
                continue
            field = _profile_field(key)
            if field:
                if _append_line(product, field, ': '.join((*parents, str(key), str(value)))):
                    del obj[key]
            elif not parents and _fact_key(key) in ('цвет', 'цветовая гамма'):
                colors = product.get('colors') or []
                if any(_fact_key(value) == _fact_key(c) for c in colors):
                    del obj[key]
    walk(product.get('specifications'))
    value = product.get('exploitation')
    if isinstance(value, str):
        keep = []
        for line in value.splitlines():
            # Не переносим неоднозначные совместные условия хранения и эксплуатации.
            storage_line = (_profile_field(line) == 'storage'
                            or re.match(r'^(?:хранить|хранят|хранение)\b', line.lstrip('-• '), re.I))
            if (storage_line and not re.search(r'эксплуатаци', line, re.I)
                    and _append_line(product, 'storage', line.lstrip('-• '))):
                pass
            else:
                keep.append(line)
        product['exploitation'] = '\n'.join(keep)
    additional = product.get('additional_info')
    if isinstance(additional, dict) and isinstance(additional.get('text'), str):
        keep = []
        for line in additional['text'].splitlines():
            label, sep, value = line.partition(':')
            field = _profile_field(label) if sep else None
            if field and _append_line(product, field, line):
                pass
            else:
                keep.append(line)
        additional['text'] = '\n'.join(keep)


def _append_line(product, field, text):
    previous = product.get(field) or ''
    if not isinstance(previous, str):
        return False
    lines = previous.splitlines()
    # Два самостоятельных срока могут находиться в одной строке через «;».
    # Сравниваем целый входной факт с целой строкой/фрагментом, не с подстрокой.
    existing = {_fact_key(fragment) for line in lines
                for fragment in (line, *line.split(';'))}
    if _fact_key(text) not in existing:
        product[field] = '\n'.join([*lines, '- ' + text]).strip()
    return True


def _strip_document_titles(value, source_text):
    titles = []
    for match in _LINK.finditer(source_text):
        title, url = match.groups()
        if _DOCUMENT.search(url) or _DOCUMENT_TITLE.match(title):
            titles.append(_norm(title))
    if not titles:
        return value

    def is_title(fragment):
        fragment = _norm(_LINK.sub(r'\1', fragment))
        if fragment in titles:
            return True
        # Модель иногда сокращает длинное название файла до первого предложения.
        return (len(fragment) >= 35 and bool(_DOCUMENT_TITLE.match(fragment))
                and any(title.startswith(fragment + ' ') for title in titles))

    result = []
    for line in value.splitlines():
        if is_title(line):
            continue
        # В смешанной строке сохраняем реальные действия после названия документа.
        fragments = re.split(r';\s*', line)
        kept = [part for part in fragments if not is_title(part)]
        if kept:
            result.append('; '.join(kept))
    return '\n'.join(result).strip()


def normalize_product_fields(ai_data, source_text=''):
    """Правит положительный ответ на месте; не принимает отклонённые страницы."""
    product = ai_data.get('product') if isinstance(ai_data, dict) else None
    if not isinstance(product, dict) or ai_data.get('error'):
        return

    def relocate(obj, parents=()):
        if isinstance(obj, dict):
            for key in list(obj):
                value = obj[key]
                if is_pallet_quantity(key) and isinstance(value, (str, int, float)):
                    label = ': '.join((*parents, str(key)))
                    _append_line(product, 'complectation', f'{label}: {value}')
                    del obj[key]
                elif isinstance(value, (dict, list)):
                    relocate(value, (*parents, str(key)))
        elif isinstance(obj, list):
            for item in obj:
                relocate(item, parents)

    _route_explicit_fields(product)
    relocate(product.get('specifications'))
    for field in ('description', 'applications', 'advantages', 'instructions_manuals',
                  'exploitation', 'storage', 'security_measures', 'compatibility'):
        value = product.get(field)
        if not isinstance(value, str):
            continue
        kept = []
        for line in value.splitlines():
            label, sep, number = line.lstrip('-• ').partition(':')
            if sep and is_pallet_quantity(label) and re.fullmatch(r'[\d\s.,+/<>=–—-]+(?:шт\.?|уп\.?|рулон\w*)?', number.strip()):
                _append_line(product, 'complectation', line.lstrip('-• '))
            else:
                kept.append(line)
        product[field] = '\n'.join(kept)
    instructions = product.get('instructions_manuals')
    if isinstance(instructions, str):
        product['instructions_manuals'] = _strip_document_titles(instructions, source_text)
    colors = product.get('colors')
    if isinstance(colors, list):
        seen = set()
        product['colors'] = []
        for color in colors:
            key = _norm(color)
            if key and key not in seen:
                seen.add(key)
                product['colors'].append(color)

    # Цвета уже представлены отдельным блоком. Повтор одной карточки под видом
    # единственного варианта не добавляет сведений.
    variants = product.get('variants')
    if isinstance(variants, list):
        color_names = {_norm(c) for c in (product.get('colors') or [])}
        model_names = {_norm(product.get('product_name', '')),
                       _norm(re.sub(r'\s+\([^()]+\)\s*$', '', product.get('product_name') or ''))}
        def redundant(item):
            if not isinstance(item, dict):
                return False
            populated = {k: v for k, v in item.items() if v not in (None, '', [], {}) and k != 'id'}
            return not populated or all(
                (k == 'Цвет' and _norm(v) in color_names) or
                (k == 'Модель' and _norm(v) in model_names)
                for k, v in populated.items())
        product['variants'] = [v for v in variants if not redundant(v)]
