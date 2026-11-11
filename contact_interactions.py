"""Полный снимок связанных панелей контактов без кликов по меню и формам."""
from collections import Counter
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from product_interactions import panel_populated, panel_target


def _panel_plan(base, selectors):
    selector = ', '.join(selectors)
    controls = []
    for control in base.select(selector) if selector else []:
        target = panel_target(control)
        panel = base.find(id=target) if target else None
        if (panel is None or control.find_parent(['nav', 'header', 'footer', 'form'])
                or panel.select_one('form, input, textarea')
                or control.get('type') == 'submit'):
            continue
        controls.append((target, control.get_text(' ', strip=True)))
    counts = Counter(target for target, _ in set(controls))
    pending = {(target, label) for target, label in controls
               if counts[target] > 1 or not panel_populated(base.find(id=target), label)}
    return controls, counts, pending


def needs_contact_interaction(html, selectors):
    return bool(_panel_plan(BeautifulSoup(html, 'lxml'), selectors)[2])


async def collect_contact_panels(page, selectors):
    """Заполненные панели сохраняем сразу, AJAX-панели снимаем до следующего клика.

    Одна панель может обслуживать несколько городов: такие переключатели
    проверяются все, а их содержимое сохраняется с устойчивой подписью.
    """
    base = BeautifulSoup(await page.content(), 'lxml')
    selector = ', '.join(selectors)
    controls, counts, pending = _panel_plan(base, selectors)
    for target, label in controls:
        panel = base.find(id=target)
        if counts[target] == 1 and panel_populated(panel, label):
            panel['data-contact-panel'] = target
            panel['data-tab'] = label
    if not pending:
        return str(base)

    snapshots = {}
    original_url = urlparse(page.url)._replace(fragment='')
    # CSS union возвращает DOM-элемент один раз, даже при совпадении селекторов.
    for control in await page.query_selector_all(selector):
        attrs = {name: await control.get_attribute(name) for name in
                 ('aria-controls', 'data-bs-target', 'data-target', 'href')}
        target = panel_target(attrs)
        label = ' '.join((await control.inner_text()).split())
        key = (target, label)
        if key not in pending or not await control.is_visible():
            continue
        if (counts[target] > 1 and await control.get_attribute('aria-selected') == 'true'
                and panel_populated(base.find(id=target), label)):
            snapshots[key] = BeautifulSoup(await page.content(), 'lxml').find(id=target)
            pending.remove(key)
            continue
        previous = await page.evaluate("""id => {
            const el = document.getElementById(id);
            return el ? el.textContent.trim() : '';
        }""", target) if counts[target] > 1 else None
        await control.click(timeout=5000)
        await page.wait_for_function("""({target, label, previous}) => {
            const el = document.getElementById(target);
            if (!el) return false;
            if (previous !== null && el.textContent.trim() === previous) return false;
            const copy = el.cloneNode(true);
            copy.querySelectorAll('script,style,button').forEach(n => n.remove());
            let text = copy.textContent.trim();
            if (text.startsWith(label)) text = text.slice(label.length).trim();
            return (text && !/^(загрузка|загружается|loading|please wait)[\\s.…!]*$/i.test(text))
                || !!copy.querySelector('a[href]:not([href^="#"]),img[src]');
        }""", arg={'target': target, 'label': label, 'previous': previous}, timeout=5000)
        if urlparse(page.url)._replace(fragment='') != original_url:
            raise ValueError('Переключатель контактов изменил адрес документа')
        current = BeautifulSoup(await page.content(), 'lxml')
        panel = current.find(id=target)
        if not panel_populated(panel, label):
            raise ValueError('Панель контактов осталась пустой')
        snapshots[key] = panel
        pending.remove(key)
    if pending:
        raise ValueError('Не загружены все связанные панели контактов')

    for target in sorted({target for target, _ in snapshots}):
        original = base.find(id=target)
        original.clear()
        for (panel_id, label), panel in sorted(snapshots.items()):
            if panel_id != target:
                continue
            section = base.new_tag('section', attrs={'data-contact-panel': target})
            heading = base.new_tag('h2')
            heading.string = label
            section.append(heading)
            for child in list(panel.contents):
                section.append(child.extract())
            original.append(section)
    return str(base)


def preserve_contact_panels(soup):
    """Перед очисткой переносим подтверждённые панели из скрытых контейнеров."""
    root = soup.find('main') or soup.find('article') or soup.body or soup
    for panel in list(soup.select('[data-contact-panel]')):
        if panel is root or root in panel.descendants or panel.find_parent(attrs={'data-contact-panel': True}):
            continue
        section = soup.new_tag('section', attrs={'data-contact-panel': panel['data-contact-panel']})
        if panel.get('data-tab'):
            heading = soup.new_tag('h2')
            heading.string = panel['data-tab']
            section.append(heading)
        for child in list(panel.contents):
            section.append(child.extract())
        panel.extract()
        root.append(section)
