"""Независимое сравнение исходного DOM и Markdown. Не вызывает конвертер/LLM."""
import hashlib
import re
from collections import Counter
from copy import deepcopy
from html import unescape

from bs4 import BeautifulSoup, NavigableString, Comment

VERSION = 26
_NUMBER = re.compile(r'(?<![\w^])[-+]?\d+(?:\.\d+)?(?:[eе][-+]?\d+)?(?!\w)', re.I)
_UNIT = re.compile(r'(?<![а-яa-z])(?:кг|мг|г|мм|см|км|м|мпа|кпа|па|квт|вт|кв|в|а|ом|гц|л|мин|ч|с|сек|бар|н)(?:\^[-+]?\d+)?(?:\s*/\s*(?:м|см|мм|ч|с|мин)(?:\^[-+]?\d+)?)?(?![а-яa-z])|%|°|(?<=\d )c\b', re.I)
_SUP = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺', '0123456789-+')
_SUB = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')
_HEADINGS = re.compile(r'^h[1-6]$')
_UI = ('nav, header, [role="navigation"], [role="menu"], [role="menubar"], [role="button"], '
       'form, button, input, select, textarea, .menu, .navbar, .pagination, .pager, '
       '.breadcrumb, .breadcrumbs, .cookie-banner, .cookie-consent')


def dom_text(node):
    """Читает текст с индексами, не используя MarkdownConverter или его очистку."""
    parts = []
    def visit(tag):
        if isinstance(tag, Comment): return
        if isinstance(tag, NavigableString):
            parts.append(str(tag)); return
        if tag.name in ('script','style','noscript','template'): return
        if tag.name == 'span' and tag.get_text(strip=True) == '0':
            style = re.sub(r'\s+', '', tag.get('style', '')).lower()
            following = tag.find_next(string=lambda value: value.strip() not in ('', '0'))
            if 'position:relative' in style and re.search(r'top:-\d', style) and following and following.strip() in ('C', 'С'):
                return  # Надстрочный ноль перед C — знак градуса, не числовой разряд.
        if tag.name in ('sup','sub'):
            value = tag.get_text('',strip=True)
            if tag.name == 'sup' and value in ('®','™',''):
                parts.append(value)
            else:
                parts.append(('^' if tag.name=='sup' else '_') + value)
            return
        if tag.name == 'br': parts.append(' '); return
        block = tag.name in ('p','div','li','dt','dd','td','th','h1','h2','h3','h4','section')
        if block: parts.append(' ')
        for child in tag.children: visit(child)
        if block: parts.append(' ')
    visit(node)
    return re.sub(r'\s+', ' ', ''.join(parts)).strip()


def canonical(text):
    """Только эквивалентная запись; значения/единицы и знаки не пересчитываются."""
    text = re.sub(r'\s+', ' ', unescape(str(text)))
    text = text.replace('\u200b', '').replace('\ufeff', '')  # Невидимые разделители, удаляемые конвертером.
    text = text.replace('σ','сигма').replace('δ','дельта').replace('λ','лямбда').replace('Ø','d').replace('ø','d').replace('φ','d').replace('ϕ','d').replace('⌀','d')  # Утверждённые замены product_utils для CP1251.
    text = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)',lambda m:'^'+m[0].translate(_SUP),text)
    text = re.sub(r'([₀₁₂₃₄₅₆₇₈₉]+)',lambda m:'_'+m[0].translate(_SUB),text)
    text = re.sub(r'\^\{([+-]?\d+)\}', r'^\1', text)
    text = text.replace('−','-').replace('–','-').replace('—','-').replace('℃','°C').replace('℉','°F')
    text = text.replace('Ü', 'U')  # Утверждённая замена для Windows-1251 в product_utils.
    text = text.replace('÷', '-')  # Утверждённая замена исходного конвертера CP1251.
    text = text.replace('^TM', '™')  # Надстрочный товарный знак, не степень единицы.
    if re.fullmatch(r'\s*_{3,}\s*', text): return ''  # Декоративная линия без данных.
    text = re.sub(r'<br\s*/?>', ' ', text, flags=re.I)  # Литерал CMS вместо переноса строки.
    text = text.replace('≤','<=').replace('≥','>=')
    # Те же значения дробей в обычной записи, без округления/пересчёта единиц.
    fractions = {'¼':'1/4', '½':'1/2', '¾':'3/4', '⅓':'1/3', '⅔':'2/3',
                 '⅛':'1/8', '⅜':'3/8', '⅝':'5/8', '⅞':'7/8'}
    text = re.sub(r'(?<=\d)(?=[¼½¾⅓⅔⅛⅜⅝⅞])', ' ', text)
    text = ''.join(fractions.get(char, char) for char in text)
    text = re.sub(r'(?<=\d),(?=\d)', '.', text)
    text = re.sub(r'(?<![\w^])\d{1,3}(?:[ \t]\d{3})+(?!\d)',
                  lambda m:re.sub(r'[ \t]', '', m[0]), text)
    text = re.sub(r'(?<=\d)\s*[×xXх*·]\s*(?=\d)', '×', text)
    text = re.sub(r'(?<=\S)\s+[×х]\s+(?=\d)', ' × ', text)
    text = re.sub(r'(?<![\w])([A-Za-zА-Яа-я])\s*[×xXх*·]\s*(?=\d)', r'\1×', text)
    text = re.sub(r'(?<=[а-яА-Яa-zA-Z])[·∙](?=[а-яА-Яa-zA-Z])', ' х ', text)
    text = re.sub(r'(?<=\d)[°˚]\s+(?=до\s+[+-]?\d+(?:\.\d+)?\s*[°˚]?[CcСс]\b)', ' ', text)
    text = re.sub(r'(?:[°˚º⁰ᵒᴼ◦\u030a]|\^[0oо])\s*[CcСс]\b', ' C', text)
    text = re.sub(r'(?<=\d)\s*[CcСс][°˚º\u030a](?![a-zA-Zа-яА-Я])', ' C', text)
    text = re.sub(r'[°˚º⁰ᵒᴼ◦\u030a]\s*[Ff]\b', ' F', text)
    text = re.sub(r'[°˚º⁰ᵒᴼ◦\u030a]\s*[KК]\b', ' K', text)
    # Заказчик запрещает символ градуса также у углов и диапазонов без C.
    text = re.sub(r'[°˚ºᵒᴼ◦\u030a]', '', text)
    text = re.sub(r'(?<=[a-zA-Zа-яА-Я])×(?=[a-zA-Zа-яА-Я])', 'х', text)
    text = re.sub(r'\bК\b', 'K', text)
    text = text.replace('″', '"')
    text = re.sub(r'(?<=\d)\s*[СC]\b', ' C', text)
    text = re.sub(r'\b(мм|см|дм|м|mm|cm|dm|m)\^?([23])\b',r'\1^\2',text)
    text = re.sub(r'(\d)(?=[а-яА-Яa-zA-Z%°])',r'\1 ',text)
    text = re.sub(r'^/\s*', '/', text)
    text = re.sub(r'\s*@\s*', '@', text)
    text = text.casefold().replace('ё','е')
    text = re.sub(r'(?<=[a-zа-я])_(?=[a-zа-я0-9]\b)', '', text)
    text = re.sub(r'(?<!\d)\.|\.(?!\d)', ' ', text)
    # Сохраняем *, ^, _, ±, /, <, >, знаки и единицы; пунктуация не является фактом.
    text = re.sub(r'[,:;()\[\]{}"«»“”]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip(' .')


def markdown_text(text):
    destination = r'\((?:[^()]|\([^()]*\))*\)'
    text = re.sub(r'!\[([^]]*)\]' + destination, '', text)
    text = re.sub(r'\[([^]]*)\]' + destination, r'\1', text)
    text = re.sub(r'\[([^]]+)\]\[[^]]*\]', r'\1', text)
    # Вложенные strong/em дают несколько пар маркеров вокруг одной фразы.
    text = re.sub(r'(?<![\\\w])(\*{2,})(?=\S)((?:\\.|[^*\n])+?)\1', r'\2', text)
    text = re.sub(r'(?<!\w)__(.+?)__(?!\w)', r'\1', text)
    text = re.sub(r'(?<![\\\w])\*(?=\S)([^*\n]+)(?<=\S)(?<!\\)\*(?!\w)',r'\1',text)
    text = re.sub(r'(?<![\\\w])_(?=\S)([^_\n]+?)(?<=\S)(?<!\\)_(?!\w)',r'\1',text)
    text = re.sub(r'(?<!\\)`([^`]+)(?<!\\)`',r'\1',text)
    text = re.sub(r'\\([\\|*_[\]<>`])',r'\1',text)
    text = re.sub(r'<br\s*/?>',' ',text,flags=re.I)
    text = re.sub(r'<(?:sup|sub)>(.*?)</(?:sup|sub)>',lambda m:('^' if m[0].startswith('<sup') else '_')+m[1],text)
    # CommonMark autolink — видимый URL, а не HTML-тег.
    text = re.sub(r'<((?:https?://|mailto:|www\.)[^<>\s]+)>', r'\1', text)
    text = re.sub(r'</?[a-zA-Z][^>]*>', '', text)
    return text


def location(node):
    if node.get('id'): return '#'+node['id']
    parts=[]
    for tag in [node,*node.parents]:
        if tag.name in ('[document]',None): break
        siblings=[p for p in tag.previous_siblings if getattr(p,'name',None)==tag.name]
        parts.append(f'{tag.name}:nth-of-type({len(siblings)+1})')
        if len(parts)==5: break
    return ' > '.join(reversed(parts))


def context(node):
    headings={}
    own_level=int(node.name[1]) if _HEADINGS.match(node.name or '') else None
    ancestors={id(p) for p in node.parents}
    for prev in node.find_all_previous(_HEADINGS):
        if node.find_parent('footer') is not None and prev.find_parent('footer') is not node.find_parent('footer'):
            continue
        # Заголовок внутри ячейки не распространяет контекст на последующий футер.
        if prev.find_parent('table') is not None and prev.find_parent('table') is not node.find_parent('table'):
            continue
        level=int(prev.name[1])
        section=prev.find_parent('section')
        if level>1 and section is not None and id(section) not in ancestors:
            continue
        if own_level is not None and level>=own_level: continue
        # Заголовок отдельного наполненного блока (например, «Цена») не задаёт
        # контекст соседней панели описания. Обёртка только над заголовком
        # сохраняет его область, как и заголовок всей страницы.
        heading_only = all(isinstance(s, Comment) or not str(s).strip() or s.find_parent(_HEADINGS)
                           for s in prev.parent.find_all(string=True))
        if level > 1 and id(prev.parent) not in ancestors and not heading_only and section is None:
            continue
        if not headings or level<min(headings): headings[level]=dom_text(prev)
        if level==1: break
    return [headings[k] for k in sorted(headings)]


def hidden(node):
    return any(t.has_attr('hidden') or t.get('aria-hidden')=='true'
               or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',t.get('style',''),re.I)
               or set(t.get('class',[])) & {'hidden','d-none','visually-hidden'}
               for t in [node,*node.parents] if getattr(t,'attrs',None) is not None)


def inaccessible(node, public):
    """Публичность панели не делает доступными её собственные скрытые дочерние узлы."""
    for tag in [node,*node.parents]:
        if getattr(tag,'attrs',None) is None or id(tag) in public: continue
        if (tag.has_attr('hidden') or tag.get('aria-hidden')=='true'
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',tag.get('style',''),re.I)
                or set(tag.get('class',[])) & {'hidden','d-none','visually-hidden'}): return True
    return False


def inventory(html, selectors=None, public_selectors=(), exclusions=()):
    """Эталон до профильных удалений. Исключение требует явного обоснования."""
    soup=BeautifulSoup(html,'html.parser'); gaps=[]; ui=[]; hidden_text=[]
    selected = list(selectors or [])
    if not selected:
        selected=['body'] if soup.body else ['html'] if soup.html else [':root']
        gaps.append('source_scope_inferred')
    # Публичные панели должны иметь видимый управляющий элемент.
    public=set()
    for control in soup.select('[aria-controls], [data-target], [data-bs-target], a[href^="#"]'):
        if hidden(control): continue
        target=control.get('aria-controls') or control.get('data-target') or control.get('data-bs-target') or control.get('href','')
        panel=soup.find(id=target.lstrip('#'))
        if panel is not None and control.get_text(strip=True): public.add(id(panel))
    for selector in public_selectors:
        nodes=soup.select(selector)
        if not nodes: gaps.append('public_selector_missing:'+selector)
        public.update(id(n) for n in nodes)
    for node in soup.select('[role="tabpanel"], .tab-pane, .accordion-body'):
        if hidden(node) and id(node) not in public:
            gaps.append('panel_availability_unproven:'+location(node))
    roots=[]
    for selector in selected:
        nodes=soup.select(selector)
        if not nodes: gaps.append('source_selector_missing:'+selector)
        roots.extend(nodes)
    ids={id(n) for n in roots}
    roots=[n for n in roots if not any(id(p) in ids for p in n.parents)]
    ignored=[]
    for spec in exclusions:
        if not isinstance(spec,dict) or set(spec)!={'selector','reason'} or not spec['reason'].strip():
            raise ValueError('Исключение эталона требует selector и непустой reason')
        for node in soup.select(spec['selector']):
            ignored.append(dict(path=location(node),reason=spec['reason'],text=dom_text(node)))
            node.attrs['data-md-audit-excluded']='1'
    for node in soup.find_all(True):
        if node.name in ('script','style','template','noscript'): continue
        if node.get('data-md-audit-excluded'): continue
        if inaccessible(node,public):
            if node.find_parent(lambda p:inaccessible(p,public)) is None:
                hidden_text.append(dict(text=dom_text(node),path=location(node)))
            node.attrs['data-md-audit-hidden']='1'
    # Явно выбранная область компании может включать контакты в шапке.
    header_contacts=[]
    for contact in soup.select('header a[href^="tel:"], header a[href^="mailto:"], header a[href^="https://yandex.ru/maps/"]'):
        if not inaccessible(contact,public) and any(root is contact or root in contact.parents for root in roots):
            header_contacts.append(dict(kind='text',text=dom_text(contact),context=[],path=location(contact)))
    # UI инвентаризируется по исходному DOM; затем удаляется из эталона.
    for node in soup.select(_UI):
        if node.name=='button' and id(node) in public and node.get('data-stroy-public-option')=='1':
            continue  # Явно проверенная открытая товарная опция с ценой.
        if node.name == 'form' and id(node) in public:
            # Проверяющий явно подтвердил публичную форму текущего товара:
            # её цены/параметры — содержимое, а поля ввода и кнопки всё ещё UI.
            continue
        if node.name == 'header' and (node.find_parent(['main', 'article']) or any(root is node or root in node.parents for root in roots if root.name not in ('body','html','[document]'))):
            continue
        if not hidden(node):
            labels=[node] if node.name in ('button','input','select') else node.find_all(['a','button','label']) or [node]
            for label in labels:
                value=dom_text(label)
                if value: ui.append(dict(text=value,path=location(label)))
        node.attrs['data-md-audit-ui']='1'
    if 'source_scope_inferred' in gaps:
        for node in soup.select('footer'): node.attrs['data-md-audit-ui']='1'
    # Полноту содержимого вне явной независимой области проверяет оператор.
    root_paths=[location(r) for r in roots]
    combined=BeautifulSoup('<section></section>','html.parser')
    for root in roots:
        if root.attrs is not None: combined.section.append(deepcopy(root))
    for node in list(combined.select('script,style,noscript,template,[data-md-audit-excluded],[data-md-audit-ui],[data-md-audit-hidden]')):
        if node.attrs is not None: node.decompose()
    tables=[]; blocks=header_contacts
    for table in combined.find_all('table'):
        if table.find_parent('table'): continue
        try: tables.append(html_table(table))
        except ValueError as exc: gaps.append('unsupported_table:'+str(exc))
    if combined.select_one('canvas, svg text, img[alt*="график"], img[alt*="диаграмм"]'):
        gaps.append('visual_content_unchecked')
    used=set()
    for term in combined.find_all('dt'):
        values=[]
        for sibling in term.next_siblings:
            if getattr(sibling,'name',None)=='dt': break
            if getattr(sibling,'name',None)=='dd': values.append(dom_text(sibling))
        if values:
            blocks.append(dict(kind='definition',text=dom_text(term).rstrip(':').rstrip()+': '+' '.join(values),
                               context=context(term),path=location(term)))
        else: gaps.append('definition_without_value:'+location(term))
    for node in combined.find_all(['h1','h2','h3','h4','h5','h6','p','li','div','section']):
        if node.find_parent(['dt','dd']): continue
        if node.find_parent('table') or node.find('table'): continue
        # Берём нижние содержательные блоки, не дублируя вложенные абзацы.
        if node.find(['p','li','dt','dd','div','section','h1','h2','h3','h4','h5','h6']): continue
        value=dom_text(node)
        if not value: continue
        key=id(node)
        if key in used: continue
        used.add(key)
        kind='heading' if _HEADINGS.match(node.name) else 'note' if (
            re.match(r'^(?:примечани|сноск|note\b|[*†‡]|\^\d)',value,re.I)
            or set(node.get('class',[])) & {'note','footnote','notes'}) else 'text'
        scope=context(node)
        if kind=='note':
            previous=node.find_previous('table')
            if previous is not None and previous.find('caption'):
                scope=context(previous)+[dom_text(previous.find('caption'))]
        blocks.append(dict(kind=kind,text=value,context=scope,path=location(node)))
    for table in tables:
        for note in table.get('notes',[]):
            blocks.append(dict(kind='note',text=note,context=table['context'],path=table['path']))
    # Текст непосредственно в main/article/span также нельзя потерять молча.
    inline_used=set()
    for node in combined.find_all(string=True):
        if isinstance(node,Comment) or not str(node).strip(): continue
        if id(node) in inline_used: continue
        if node.find_parent(['table','p','li','dt','dd','h1','h2','h3','h4','h5','h6']): continue
        if any(id(p) in used for p in node.parents): continue
        value=str(node)
        # Прямой текст перед li/table может содержать индексы без p-обёртки.
        # Не отделяем степень от величины и единицы в независимом эталоне.
        following=node.next_sibling
        while following is not None and (isinstance(following,NavigableString) or getattr(following,'name',None) in ('sup','sub')):
            value += str(following) if isinstance(following,NavigableString) else dom_text(following)
            if isinstance(following,NavigableString): inline_used.add(id(following))
            else: inline_used.update(id(s) for s in following.find_all(string=True))
            following=following.next_sibling
        value=value.strip()
        if value: blocks.append(dict(kind='text',text=value,context=context(node),path=location(node.parent)))
    if not blocks and not tables: gaps.append('empty_source_inventory')
    # Название доступной непустой панели может стать заголовком её таблицы
    # в Markdown. Это не утечка кнопки; пустая/посторонняя панель не подходит.
    panel_labels=[]
    for control in soup.select('[aria-controls]'):
        panel=soup.find(id=control.get('aria-controls'))
        if (hidden(control) or panel is None or id(panel) not in public
                or not any(root is panel or root in panel.parents for root in roots)):
            continue
        if panel.find('table') and not panel.get('data-md-audit-excluded'):
            panel_labels.append(dom_text(control))
    return dict(blocks=blocks,tables=tables,ui=ui,hidden=hidden_text,excluded=ignored,panel_labels=panel_labels,
                selectors=selected,roots=root_paths,gaps=sorted(set(gaps)))


def html_table(table):
    if table.find('table'): raise ValueError('вложенная таблица')
    rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
    occupied={}; grids=[]; header_count=0
    for i,row in enumerate(rows):
        cells=row.find_all(['td','th'],recursive=False); col=0
        if i==header_count and cells and all(c.name=='th' for c in cells): header_count+=1
        for cell in cells:
            while (i,col) in occupied: col+=1
            spans=[]
            for attr in ('rowspan','colspan'):
                value=str(cell.get(attr,1))
                if not value.isdigit() or not 1<=int(value)<=1000: raise ValueError('невалидный '+attr)
                spans.append(int(value))
            height,width=spans
            # HTML rowspan ограничен своей группой строк; thead не может
            # занимать ячейки tbody, даже при избыточном атрибуте источника.
            group=row.find_parent(['thead','tbody','tfoot'])
            if group is not None and group.find_parent('table') is table:
                remaining=sum(1 for other in rows[i:] if other.find_parent(['thead','tbody','tfoot']) is group)
                height=min(height,remaining)
            if i+height>len(rows): raise ValueError('rowspan выходит за таблицу')
            if len(occupied)+height*width>100000: raise ValueError('лимит ячеек')
            for r in range(i,i+height):
                for c in range(col,col+width):
                    if (r,c) in occupied: raise ValueError('перекрытие объединённых ячеек')
                    occupied[r,c]=cell
            col+=width
    width=max((c for r,c in occupied),default=-1)+1
    if len(rows)*width > 100000: raise ValueError('лимит ячеек')
    # Незаполненные позиции HTML-таблицы отображаются пустыми, а не содержат
    # неизвестное значение. Явные rowspan/colspan и пересечения проверены выше.
    empty_factory = BeautifulSoup('', 'html.parser')
    for i in range(len(rows)):
        for j in range(width):
            if (i,j) not in occupied: occupied[i,j] = empty_factory.new_tag('td')
        grids.append([dom_text(occupied[i,j]) for j in range(width)])
    # Таблицы без th с текстовой первой строкой — распространённый HTML.
    if header_count==0 and grids and all(not re.search(r'\d',s) for s in grids[0]): header_count=1
    if (header_count == 1 and width == 4 and len(grids) > 2
            and [canonical(cell) for cell in grids[0]] == ['типоразмер насоса'] + ['тип электродвигателя'] * 3
            and [canonical(cell) for cell in grids[1]] == ['типоразмер насоса', 'марка', 'мощность квт', 'напряжение в']
            and occupied[0,0] is occupied[1,0]
            and occupied[0,1] is occupied[0,2] is occupied[0,3]):
        header_count = 2
    title_rows=int(width>1 and bool(grids) and len({id(occupied[0,j]) for j in range(width)})==1)
    if title_rows and header_count<=1 and len(grids)>1 and all(not re.search(r'\d',s) for s in grids[1]):
        header_count=2
    header_count=max(header_count,title_rows)
    headers=[]
    for j in range(width):
        parts=[];seen=set()
        for i in range(title_rows,header_count):
            cell=occupied[i,j]
            if id(cell) not in seen: parts.append(dom_text(cell));seen.add(id(cell))
        headers.append(' / '.join(parts))
    notes=[];data=[]
    for row in grids[header_count:]:
        if row and len(set(row))==1 and re.match(r'^(?:примечани|note\b|[*†‡]|\^\d)',row[0],re.I):
            notes.append(row[0])
        else:
            row_index=header_count+len(data)+len(notes)
            if width>1 and len({id(occupied[row_index,j]) for j in range(width)})==1:
                row=[row[0]]+['']*(width-1)
            data.append(row)
    caption=table.find('caption',recursive=False)
    return dict(headers=headers,rows=data,all_rows=grids[title_rows:header_count]+data,notes=notes,
                title=grids[0][0] if title_rows else '',
                context=context(table)+([dom_text(caption)] if caption else []),path=location(table),
                header_rows=header_count)


def split_cells(line):
    cells=[];part=[];escaped=False;code=False
    for char in line.strip().strip('|'):
        if char=='\\' and not escaped: escaped=True;part.append(char);continue
        if char=='`' and not escaped: code=not code
        if char=='|' and not escaped and not code:
            cells.append(markdown_text(''.join(part)).strip());part=[]
        else: part.append(char)
        escaped=False
    cells.append(markdown_text(''.join(part)).strip())
    return cells


def parse_markdown(markdown):
    # Выделение может охватывать абзац с br; номера строк сохраняются.
    markdown=re.sub(r'(?<![\\\w])(\*{2,})(?=\S)((?:\\.|[^*])+?)\1', r'\2', markdown)
    lines=markdown.splitlines(); blocks=[];tables=[]; headings={};i=0;gaps=[]
    while i<len(lines):
        line=lines[i].strip()
        if line.startswith('```') or line.startswith('~~~'): gaps.append('fenced_markdown_requires_review')
        if re.search(r'<table\b',line,re.I):
            start=i+1;fragment=line
            while not re.search(r'</table\s*>',fragment,re.I) and i+1<len(lines):
                i+=1;fragment+='\n'+lines[i]
            try:
                node=BeautifulSoup(fragment,'html.parser').find('table')
                table=html_table(node)
                table.update(line=start,context=list(headings.values())+table['context'])
                tables.append(table)
                blocks.extend(dict(text=note,context=table['context'],line=start) for note in table['notes'])
            except (ValueError,AttributeError) as exc:
                gaps.append('unsupported_markdown_html_table:'+str(exc))
            i+=1;continue
        heading=re.match(r'^(?:>\s*)*(?:[-+*]\s+)?(#{1,6})\s+(.+)',line)
        if heading:
            level=len(heading[1]);headings={k:v for k,v in headings.items() if k<level}
            value=markdown_text(heading[2]);headings[level]=value
            blocks.append(dict(kind='heading',text=value,context=list(headings.values()),line=i+1));i+=1;continue
        if i+1<len(lines) and '|' in line:
            separator=split_cells(lines[i+1])
            if separator and all(re.fullmatch(r':?-{3,}:?',c.strip()) for c in separator):
                header=split_cells(line);start=i+1;rows=[];i+=2
                while i<len(lines) and '|' in lines[i] and lines[i].strip():
                    rows.append(split_cells(lines[i]));i+=1
                if any(len(r)!=len(header) for r in rows) or len(separator)!=len(header):
                    gaps.append('ragged_markdown_table:'+str(start))
                tables.append(dict(headers=header,rows=rows,all_rows=[header]+rows,
                                   context=list(headings.values()),line=start));continue
        if line:
            # Номер может быть буквальным текстом HTML-абзаца, а не счётчиком ol.
            # Сохраняем его: содержимое обычного li по-прежнему совпадает как подстрока.
            text=re.sub(r'^\s*(?:[-+*]\s+|>\s*)','',line)
            blocks.append(dict(text=markdown_text(text),literal_text=markdown_text(line),
                               bullet_text=markdown_text(re.sub(r'^[-+*]\s+', '• ', line)),
                               context=list(headings.values()),line=i+1))
        i+=1
    return dict(blocks=blocks,tables=tables,gaps=sorted(set(gaps)))


def _ctx(values): return [canonical(v).replace(' / ',' ') for v in values if canonical(v)]


def _same_context(source,target):
    a,b=_ctx(source),_ctx(target)
    return not a or all(x in b or any(_contains(x,v) for v in b) for x in a)


def _contains(expected,actual):
    a,b=canonical(expected),canonical(actual)
    return bool(a) and (' '+a+' ') in (' '+b+' ')


def _column_header(value):
    label = canonical(value).replace(' / ', ' ')
    if label == 'насос типоразмер насоса':
        return 'типоразмер насоса'
    if label in ('тип электродвигателя марка', 'тип электродвигателя мощность квт',
                 'тип электродвигателя напряжение в'):
        return label.replace('тип электродвигателя ', 'электродвигатель ', 1)
    return label


def compare(source,markdown,stage='markdown'):
    target=parse_markdown(markdown);findings=[];gaps=list(source['gaps'])+target['gaps'];used_tables=set()
    def issue(code,expected,actual='',**where):
        severity='review' if 'source_scope_inferred' in source['gaps'] and code not in (
            'interface_leak','hidden_content_leak') else 'error'
        findings.append(dict(code=code,severity=severity,stage=stage,expected=expected,actual=actual,**where))
    for block in source['blocks']:
        if not canonical(block['text']):continue
        candidates=[b for b in target['blocks'] if _same_context(block['context'],b['context'])]
        if block['kind']=='heading':candidates=[b for b in candidates if b.get('kind')=='heading']
        if block['kind']=='note':
            candidates += [dict(text=' '.join(row),context=t['context'],line=t['line'])
                           for t in target['tables'] if _same_context(block['context'],t['context'])
                           for row in t['all_rows']]
        # Заголовок проверяется в самом заголовке; переносы строк внутри абзаца допустимы.
        joined=' '.join(b['text'] for b in candidates)
        # Дефис перед строкой может быть буквальным текстом HTML с <br>,
        # а не маркером списка. Проверяем оба точных представления, без удаления чисел.
        literal=' '.join(b.get('literal_text',b['text']) for b in candidates)
        bullets=' '.join(b.get('bullet_text',b['text']) for b in candidates)
        if block['kind'] == 'definition':
            # markdownify emits the Markdown Extra delimiter on the next line, even
            # when a term already ends with a literal colon. Keep its value in order.
            definitions = re.sub(r'\s*:\s*:\s+', ': ', joined)
            if _contains(block['text'], definitions):
                continue
        if any(_contains(block['text'], b['text']) for b in candidates) or _contains(block['text'],joined) or _contains(block['text'],literal) or _contains(block['text'],bullets): continue
        expected=canonical(block['text']);words=set(re.findall(r'[а-яa-z]+',expected))
        best=max(candidates,key=lambda b:len(words & set(re.findall(r'[а-яa-z]+',canonical(b['text'])))),default=None)
        actual=best['text'] if best else ''
        missing_numbers=Counter(_NUMBER.findall(expected))-Counter(_NUMBER.findall(canonical(actual)))
        missing_units=Counter(_UNIT.findall(expected))-Counter(_UNIT.findall(canonical(actual)))
        if block['kind']=='note': code='note_missing_or_changed'
        elif missing_numbers: code='number_mismatch'
        elif missing_units: code='unit_mismatch'
        else: code='block_missing_or_changed'
        issue(code,block['text'],actual,path=block['path'],context=block['context'],
              target_line=best['line'] if best else None,missing_numbers=list(missing_numbers.elements()),
              missing_units=list(missing_units.elements()))
    for table in source['tables']:
        # Таблица без видимых ячеек не несёт данных; непустые проверяются полностью.
        if not any(cell.strip() for row in table['all_rows'] for cell in row):
            continue
        candidates=[(i,t) for i,t in enumerate(target['tables']) if i not in used_tables
                    and _same_context(table['context'],t['context'])]
        if not candidates:
            issue('table_missing',table['all_rows'],path=table['path'],context=table['context']);continue
        def overlap(candidate):
            a=Counter(canonical(c) for row in table['all_rows'] for c in row)
            b=Counter(canonical(c) for row in candidate['all_rows'] for c in row)
            return sum((a&b).values())
        ranked=sorted(candidates,key=lambda item:(-overlap(item[1]),item[0]))
        index,matched=ranked[0];used_tables.add(index)
        title=table.get('title')
        if title:
            banner=[canonical(h) for h in matched['headers'] if canonical(h)]
            if banner and set(banner)=={canonical(title)} and matched['rows']:
                matched=dict(matched,headers=matched['rows'][0],rows=matched['rows'][1:],all_rows=matched['rows'])
            elif canonical(title) not in _ctx(matched['context']) and canonical(title)!=canonical(matched.get('title','')):
                issue('table_title_missing',title,path=table['path'],target_line=matched['line'])
        if (len(ranked)>1 and overlap(ranked[0][1])==overlap(ranked[1][1])
                and [[canonical(c) for c in row] for row in ranked[0][1]['all_rows']]
                != [[canonical(c) for c in row] for row in ranked[1][1]['all_rows']]):
            gaps.append('ambiguous_table_match:'+table['path'])
        # Привязка столбца к полной цепочке заголовков; перестановка столбцов допустима.
        mapping=[]
        for col,header in enumerate(table['headers']):
            expected=_column_header(header)
            hits=[j for j,h in enumerate(matched['headers']) if _column_header(h)==expected]
            if not expected: hits=[col] if col<len(matched['headers']) else []
            if len(hits)!=1 or hits[0] in mapping:
                mapping=[];break
            mapping.append(hits[0])
        if not mapping:
            # Многострочная шапка может быть оставлена несколькими строками GFM.
            src=Counter(tuple(canonical(c) for c in row) for row in table['all_rows'])
            dst=Counter(tuple(canonical(c) for c in row) for row in matched['all_rows'])
            if not (src-dst): continue
            issue('table_headers_or_layout_changed',table['headers'],matched['headers'],path=table['path'],target_line=matched['line'])
            continue
        actual_rows=matched['rows'] if table['header_rows'] > int(bool(title)) else matched['all_rows']
        actual=Counter(tuple(canonical(row[j]) if j<len(row) else '' for j in mapping) for row in actual_rows)
        for row_index,row in enumerate(table['rows']):
            values=tuple(canonical(c) for c in row)
            if actual[values]: actual[values]-=1;continue
            closest=max(actual_rows,key=lambda r:sum(j<len(r) and canonical(r[j])==values[k] for k,j in enumerate(mapping)),default=[])
            cells=[]
            for col,j in enumerate(mapping):
                value=closest[j] if j<len(closest) else ''
                if canonical(value)==values[col]: continue
                change='number_changed' if Counter(_NUMBER.findall(values[col]))!=Counter(_NUMBER.findall(canonical(value))) else (
                    'unit_changed' if Counter(_UNIT.findall(values[col]))!=Counter(_UNIT.findall(canonical(value))) else 'text_changed')
                cells.append(dict(column=col+1,header=table['headers'][col],expected=row[col],actual=value,change=change))
            issue('table_row_relationship_lost',row,closest,path=table['path'],row=row_index+table['header_rows']+1,
                  headers=table['headers'],context=table['context'],target_line=matched['line'],cells=cells)
    content=' '.join(b['text'] for b in source['blocks'])+' '+' '.join(c for t in source['tables'] for r in t['all_rows'] for c in r)
    md_visible=' '.join(b['text'] for b in target['blocks'])+' '+' '.join(c for t in target['tables'] for r in t['all_rows'] for c in r)
    ui_candidates=target['blocks']+[dict(text=c,line=t['line']) for t in target['tables'] for row in t['all_rows'] for c in row]
    for kind,items in [('interface_leak',source['ui']),('hidden_content_leak',source['hidden'])]:
        seen=set()
        for item in items:
            label=item['text'];key=canonical(label)
            if not key or key in seen or _contains(label,content): continue
            if (kind=='interface_leak' and label in source.get('panel_labels',[])
                    and any(b.get('kind')=='heading' and canonical(b['text'])==key for b in target['blocks'])):
                continue
            seen.add(key)
            match=next((b for b in ui_candidates if canonical(b['text'])==key),None)
            if match or (len(key.split())>=3 and _contains(label,md_visible)):
                issue(kind,label,match['text'] if match else label,path=item['path'],target_line=match['line'] if match else None)
    return dict(status='fail' if any(f['severity']=='error' for f in findings) else 'uncertain' if gaps or findings else 'pass',stage=stage,
                findings=findings,uncertainties=sorted(set(gaps)),
                counts=dict(source_blocks=len(source['blocks']),source_tables=len(source['tables']),
                            markdown_blocks=len(target['blocks']),markdown_tables=len(target['tables'])),
                markdown_sha256=hashlib.sha256(markdown.encode()).hexdigest())


def check_markdown(html,markdown,*,url='',source_selectors=None,public_selectors=(),exclusions=(),model_input=None):
    from urllib.parse import urlsplit
    reference_html = html
    if (urlsplit(url).hostname or '').removeprefix('www.') == 'gorgaz.ru' and urlsplit(url).path.startswith('/catalog/show/'):
        from source_product_tables import unwrap_irvis_layout
        soup = BeautifulSoup(html, 'lxml')
        unwrap_irvis_layout(soup)
        reference_html = str(soup)
    if urlsplit(url).hostname == 'istra-vniieto.ru' and urlsplit(url).fragment.startswith('source-family='):
        from inline_products import isolate_inline_product
        reference_html = isolate_inline_product(html, url) or ''
    source=inventory(reference_html,source_selectors,public_selectors,exclusions)
    result=compare(source,markdown)
    result.update(checker_version=VERSION,url=url,html_sha256=hashlib.sha256(html.encode()).hexdigest(),source=source)
    if model_input is not None:
        result['model_input']=compare(source,model_input,'model_input')
        if result['model_input']['status']=='fail': result['status']='fail'
        elif result['model_input']['status']=='uncertain' and result['status']=='pass': result['status']='uncertain'
    else: result['model_input']=dict(status='uncertain',reason='actual_model_input_not_provided')
    result['scope']='HTML→Markdown; проверка карточек и живая видимость не выполнялись'
    return result
