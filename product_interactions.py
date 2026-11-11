"""Публичные товарные окна Tilda и связанные панели характеристик."""
import re
from copy import deepcopy
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup
from inline_products import inline_product_key, isolate_inline_product

PRODUCT_PANEL_SELECTOR = ('[data-toggle="collapse"], [data-bs-toggle="collapse"], '
                          '[aria-expanded][aria-controls], [data-toggle="tab"], [data-bs-toggle="tab"], '
                          '[role="tab"][aria-controls], [data-toggle="modal"], '
                          '[data-bs-toggle="modal"], [aria-haspopup="dialog"]')
PRODUCT_PANEL_LABEL = re.compile(
    r'характеристик|параметр|описани|документ|сертификат|инструкци|'
    r'размер|specification|technical|description|document|download', re.I)


def tilda_product_key(url):
    """Идентификатор блока обязателен: lid повторяются в разных блоках каталога."""
    match = re.fullmatch(r'!/tproduct/(\d+)-(\d+)', urlparse(url).fragment)
    return match.groups() if match else None


def product_state_key(url):
    """Товарное состояние страницы: Tilda либо подтверждённый профиль лендинга."""
    return urlparse(url).fragment if tilda_product_key(url) else inline_product_key(url)


def isolate_product_state(html, url):
    if tilda_product_key(url):
        return isolate_tilda_product(html, url)
    return isolate_inline_product(html, url)


def tilda_product_links(soup, url):
    """Восстанавливает адреса только опубликованных плиток с #prodpopup."""
    links = set()
    for card in soup.select('[data-product-lid]'):
        if not card.select_one('a[href="#prodpopup"]'):
            continue
        rec = card.find_parent(id=re.compile(r'^rec\d+$'))
        lid = card.get('data-product-lid', '')
        if rec is None or not re.fullmatch(r'\d+', lid):
            continue
        fragment = f'!/tproduct/{rec["id"][3:]}-{lid}'
        links.add(urlunparse(urlparse(url)._replace(fragment=fragment)))
    return sorted(links)


def isolate_tilda_product(html, url):
    """Выделяет ровно одно опубликованное окно; отсутствие данных не заменяет каталогом."""
    key = tilda_product_key(url)
    if not key:
        return html
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('main[data-tilda-product]'):
        return html if soup.main.get('data-tilda-product') == '-'.join(key) else None
    rec = soup.find(id='rec' + key[0])
    product = rec.select_one(f'.t-popup [data-product-lid="{key[1]}"]') if rec else None
    if product is None:
        return None
    name = product.select_one('.js-product-name')
    description = product.select_one('.js-product-descr, [class*="__descr"]')
    if name is None or not name.get_text(strip=True) or not panel_populated(description):
        return None
    result = BeautifulSoup('<html><head></head><body></body></html>', 'html.parser')
    # base нужен для файлов/изображений, title каталога заменяется именем товара.
    if soup.find('base', href=True):
        result.head.append(deepcopy(soup.find('base', href=True)))
    title = result.new_tag('title')
    title.string = name.get_text(' ', strip=True)
    result.head.append(title)
    product = deepcopy(product)
    product.name = 'main'
    product.attrs = {'data-tilda-product': '-'.join(key)}
    product.select_one('.js-product-name').name = 'h1'
    result.body.append(product)
    return str(result)


def panel_target(control):
    target = control.get('aria-controls')
    if target:
        return target if not re.search(r'\s', target) else None
    ref = control.get('data-bs-target') or control.get('data-target') or control.get('href') or ''
    return ref[1:] if ref.startswith('#') and len(ref) > 1 else None


def panel_populated(panel, label=''):
    if panel is None:
        return False
    content = deepcopy(panel)
    for noise in content.select('script, style, button'):
        noise.decompose()
    text = content.get_text(' ', strip=True)
    if label and text.casefold().startswith(label.casefold()):
        text = text[len(label):].strip()
    loading = re.fullmatch(r'(?:загрузка|загружается|loading|please wait)[\s.…!]*', text, re.I)
    return bool((text and not loading) or content.select_one('img[src], a[href]:not([href^="#"])'))


def _control_available(control):
    """Не раскрываем панель через явно скрытый элемент или его предка."""
    if control is None:
        return False
    return not any(
        node.has_attr('hidden')
        or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', node.get('style', ''), re.I)
        for node in [control, *control.parents] if getattr(node, 'attrs', None) is not None)


def product_panel_controls(soup):
    for control in soup.select(PRODUCT_PANEL_SELECTOR):
        if control.find_parent(['nav', 'footer', 'aside', 'form']):
            continue
        # Недоступный пользователю переключатель не подтверждает доступ к данным.
        if not _control_available(control):
            continue
        label = control.get_text(' ', strip=True)
        target = panel_target(control)
        panel = soup.find(id=target) if target else None
        if panel is not None and panel.select_one('input, textarea, form'):
            continue
        if target and PRODUCT_PANEL_LABEL.search(label):
            yield control, label, target


def panel_section(soup, panel, label, target):
    section = soup.new_tag('section', attrs={'data-product-panel': target, 'data-tab': label})
    first_heading = panel.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    same_heading = (first_heading is not None
                    and ' '.join(first_heading.get_text(' ', strip=True).casefold().split())
                    == ' '.join(label.casefold().split()))
    if not same_heading:
        heading = soup.new_tag('h2')
        heading.string = label
        section.append(heading)
    for child in list(panel.contents):
        section.append(child.extract())
    # Это уже содержимое выбранной панели, modal-body здесь не рекламный шум.
    for child in section.select('[class]'):
        child['class'] = [c for c in child.get('class', [])
                          if not re.match(r'^(modal|popup|overlay)(?:[-_]|$)', c, re.I)]
    return section


def public_slider_panels(soup, page_url):
    """Пять HTML-состояний опубликованного ползунка длины НЗМК."""
    if (urlparse(page_url).hostname or '').lower() not in ('nzmk22.ru', 'www.nzmk22.ru'):
        return []
    if not _control_available(soup.select_one('.breams-slider .slider21')):
        return []
    script = '\n'.join(tag.get_text() for tag in soup.find_all('script'))
    # Связь селектора, значения ползунка и показываемой панели есть в JS.
    if ('$(".slider21").slider({ max: 4' not in script
            or '$(".slider-data-containers .slider-data-container-"+value).show()' not in script):
        return []
    result = []
    for index in range(5):
        panels = soup.select(f'.slider-data-containers .slider-data-container-{index}')
        if len(panels) != 1:
            return []
        result.append(panels[0])
    return result


def preserve_public_slider_panels(soup, page_url):
    changed = False
    for panel in public_slider_panels(soup, page_url):
        style = panel.get('style', '')
        clean_style = re.sub(r'display\s*:\s*none\s*;?', '', style, flags=re.I)
        if clean_style != style:
            panel['style'] = clean_style
            changed = True
    return changed


def _preserve_product_panels_once(soup, page_url=''):
    """Переносит связанные панели из modal/hidden в основной товарный контейнер.

    Вызывается до удаления шума. Формы заказа и несвязанные окна не раскрываются.
    """
    slider_changed = preserve_public_slider_panels(soup, page_url)
    root = (soup.select_one('[itemtype*="Product"]') or soup.find('article')
            or soup.find('main') or soup.select_one('.content') or soup.body or soup)
    # .content может оказаться скрытым меню, а не карточкой. Перенос туда
    # уничтожает таблицу при последующей чистке меню. Привязываем контейнер
    # к заголовку и всем явно связанным панелям текущей страницы.
    controls = list(product_panel_controls(soup))
    panels = [panel for _, _, target in controls if (panel := soup.find(id=target)) is not None]
    snapshots = list(soup.select('section[data-product-panel]'))
    heading = soup.find('h1')
    if (heading is not None and (panels or snapshots)
            and root is not heading and root not in heading.parents):
        required = [heading] + [control for control, _, _ in controls]
        for ancestor in heading.parents:
            if all(node is ancestor or ancestor in node.parents for node in required):
                root = ancestor
                break
    changed = slider_changed
    for snapshot in snapshots:
        if root not in snapshot.parents:
            root.append(snapshot.extract())
            changed = True
    for control, label, target in controls:
        if control.find_parent(['nav', 'footer', 'aside', 'form']):
            continue
        panel = soup.find(id=target)
        if not panel_populated(panel, label) or panel is root or root in panel.descendants:
            continue
        if panel.select_one('input, textarea, form'):
            continue
        # Снимок браузера уже содержит панель, в том числе при закрытом аккордеоне.
        if soup.find('section', attrs={'data-product-panel': target}):
            continue
        section = panel_section(soup, panel, label, target)
        ancestors = []
        for ancestor in panel.parents:
            if ancestor is root:
                break
            ancestors.append(ancestor)
        unsafe_parent = any(
            ancestor.has_attr('hidden')
            or re.search(r'display\s*:\s*none', ancestor.get('style', ''), re.I)
            or any(re.match(r'^(modal|popup|overlay|menu|nav)(?:[-_]|$)', cls, re.I)
                   for cls in ancestor.get('class', []))
            for ancestor in ancestors)
        if root in panel.parents and not unsafe_parent:
            # Уже внутри карточки: сохраняем место, иначе адаптер/CMS может
            # выбрать прежний контейнер и потерять перенесённые наружу вкладки.
            panel.replace_with(section)
        else:
            panel.extract()
            root.append(section)
        changed = True
    return changed


def preserve_product_panels(soup, page_url=''):
    """Раскрывает связанные панели снаружи внутрь, не открывая скрытых соседей."""
    changed = False
    while _preserve_product_panels_once(soup, page_url):
        changed = True
    return changed


async def expand_product_panels(page):
    """Снимает каждую AJAX-панель до следующего клика (аккордеоны могут её удалить)."""
    soup = BeautifulSoup(await page.content(), 'html.parser')
    pending = {target for _, label, target in product_panel_controls(soup)
               if not panel_populated(soup.find(id=target), label)}
    if not pending:
        return []
    sections = []
    for control in await page.query_selector_all(PRODUCT_PANEL_SELECTOR):
        if not await control.is_visible():
            continue
        label = (await control.inner_text()).strip()
        attrs = {key: await control.get_attribute(key) for key in
                 ('aria-controls', 'data-bs-target', 'data-target', 'href')}
        target = panel_target(attrs)
        if target not in pending or not PRODUCT_PANEL_LABEL.search(label):
            continue
        original_url = page.url
        await control.click(timeout=5000)
        if urlparse(page.url)._replace(fragment='') != urlparse(original_url)._replace(fragment=''):
            raise ValueError('Панель характеристик изменила адрес документа')
        await page.wait_for_function("""({id, label}) => {
            const el = document.getElementById(id);
            if (!el) return false;
            const copy = el.cloneNode(true);
            copy.querySelectorAll('script, style, button').forEach(node => node.remove());
            let text = copy.textContent.trim();
            if (text.toLowerCase().startsWith(label.toLowerCase())) text = text.slice(label.length).trim();
            return (text && !/^(загрузка|загружается|loading|please wait)[\\s.…!]*$/i.test(text))
                || !!copy.querySelector('a[href]:not([href^="#"]), img[src]');
        }""", arg={'id': target, 'label': label}, timeout=5000)
        if urlparse(page.url)._replace(fragment='') != urlparse(original_url)._replace(fragment=''):
            raise ValueError('Панель характеристик изменила адрес документа')
        current = BeautifulSoup(await page.content(), 'html.parser')
        panel = current.find(id=target)
        if not panel_populated(panel, label):
            raise ValueError('Панель характеристик осталась пустой')
        section = panel_section(current, panel, label, target)
        sections.append(str(section))
        pending.remove(target)
        # Диалог может закрывать следующие кнопки. Закрываем только открытый target.
        modal = await control.get_attribute('data-bs-toggle') or await control.get_attribute('data-toggle')
        if modal == 'modal' or await control.get_attribute('aria-haspopup') == 'dialog':
            await page.keyboard.press('Escape')
    if pending:
        raise ValueError('Не удалось открыть все панели характеристик')
    return sections
