"""Отпечаток содержимого страницы; исходный HTML остаётся неизменным."""
import hashlib
import json
import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

COMPARISON_VERSION = 2
_TOKEN_NAME = re.compile(r'^(?:csrf[-_]?token|csrfmiddlewaretoken|_token|sessid|bitrix_sessid|'
                         r'__requestverificationtoken|g-recaptcha-response|h-captcha-response)$', re.I)
# Атрибуты оформления/состояния браузера не являются данными компании.
_INLINE_TOKEN = re.compile(
    r"""((?:["'](?:bitrix_sessid|sessid|csrf[-_]?token|csrfmiddlewaretoken)["'])\s*:\s*)(["'])(?:\\.|(?!\2).)*\2""",
    re.I,
)
_NOISE_ATTRS = {'id', 'class', 'style', 'nonce', 'integrity', 'crossorigin',
                'aria-expanded', 'aria-selected', 'aria-describedby', 'tabindex', 'data-reactid',
                'data-react-checksum', 'data-v-app'}


def _bitrix_service_data(match):
    """Меняем только известные служебные поля JSON-вызова Bitrix BX.message."""
    try:
        data = json.loads(match.group(2))
    except ValueError:
        return match.group(0)
    if isinstance(data, dict) and 'bitrix_sessid' in data and 'SERVER_TIME' in data:
        data['SERVER_TIME'] = '<server-time>'
    return match.group(1) + json.dumps(data, sort_keys=True, ensure_ascii=False) + match.group(3)


_BITRIX_MESSAGE = re.compile(r'(\(window\.BX\|\|top\.BX\)\.message\()(\{[^;]*?\})(\);)')
# Проверенные bootstrap счётчиков/чата Ридана (03.11.2026), точное совпадение
# кода с нормализацией пробелов. Эталоны: tests/fixtures/ridan_tracking_bootstraps.json.
# Приписанный JS или новые данные не совпадут и останутся значимыми.
_TRACKING_BOOTSTRAPS = {
    '883bcee757a759e11cda312890041f7a83134c734418e3691a51aaae95003299',
    '635fc9decbd50c3eea393aea64895cf20782c4d939965fcf84993e66dd9fb10d',
    '10c016b619fdd76dd6499b8123567cb17e48abfc03bf1008610f46423ca09718',
    '184de89d109f656628f193d9bd77a3956acb2fd748b63bcf71ffc8864f18901f',
}
_TRACKING_URL = re.compile(r'^(?:https?:)?//(?:mc\.yandex\.ru/(?:metrika/|watch/)|'
                           r'top-fwz1\.mail\.ru/counter\?|www\.googletagmanager\.com/ns\.html\?)')


def html_comparison_hash(html: str) -> str:
    """Сохраняем тексты, таблицы, ссылки, data-атрибуты и встроенные данные.

    Не вырезаем скрытые панели, даты, числа, произвольные JS и карты: там могут
    находиться характеристики, контакты и координаты. Удаление шума намеренно
    ограничено служебной разметкой, а не регулярным выражением по всему HTML.
    """
    soup = BeautifulSoup(html, 'lxml')
    # Маска пустого поля телефона — интерфейс формы, не телефон компании.
    for node in soup.select('form [data-field-type="ph"]'):
        if not any(el.get('value') for el in node.select('input[type="tel"], input[name="Phone"]')):
            node.clear()
            node.attrs.pop('data-init-mask', None)
            node.attrs.pop('data-input-ready', None)
    # Tilda дорисовывает эти подписи после AJAX. Нулевой остаток уже выражен
    # в data-product-inv и остаётся значимым вместе с остальными данными товара.
    for card in soup.select('[data-product-uid][data-product-inv="0"]'):
        for node in card.select('.js-store-prod-sold-out, a[href="#popup:out-of-stock"]'):
            if node.get_text(' ', strip=True) in ('Нет в наличии', 'Купить под заказ'):
                node.decompose()
    for node in list(soup.find_all('jdiv')):
        if node.parent:
            node.decompose()
    for node in soup.select('iframe[name="jivo_container"]'):
        parent = node.parent
        node.decompose()
        if parent.name == 'div' and not parent.get_text(strip=True) and not parent.find(True):
            parent.decompose()
    # Только элементы управления маршрутом; балуны, подписи дилеров и данные карт
    # остаются. Координаты в JS/iframe не вырезаются.
    for node in list(soup.find_all('ymaps')):
        if (node.parent and node.get_text(strip=True).isdigit()
                and any(re.search(r'islands_cluster[A-Za-z]*Icon', cls) for cls in node.get('class', []))):
            # Число сгруппированных значков зависит от масштаба/анимации карты.
            node.decompose()
        elif node.parent and any(re.match(r'ymaps-[\d-]+-(?:gotoymaps|routerPoints-pane|copyrights-pane|controls-pane)$', cls)
                               for cls in node.get('class', [])):
            node.decompose()
        elif node.parent and not node.get_text(strip=True) and any(
                re.match(r'ymaps-[\d-]+-ground-pane$', cls) for cls in node.get('class', [])):
            # Растровая/Canvas-подложка карты, без подписей и маркеров компании.
            node.decompose()
    for node in soup.find_all('canvas'):
        if (node.find_parent('ymaps') and not node.get_text(strip=True)
                and set(node.attrs) <= _NOISE_ATTRS | {'width', 'height'}):
            node.decompose()
    for node in reversed(soup.find_all('ymaps')):
        if (not node.get_text(strip=True) and not node.find(True)
                and set(node.attrs) <= _NOISE_ATTRS | {'unselectable'}):
            node.decompose()
    for node in soup.find_all('noscript'):
        resources = node.select('img[src], iframe[src]')
        if resources and not node.get_text(strip=True) and all(_TRACKING_URL.match(el['src']) for el in resources):
            node.decompose()
    for node in soup.select('[data-view="SlotView.mimic"], div[data-nosnippet]'):
        if not node.contents:
            node.decompose()
    for node in soup.select('iframe[src]'):
        if re.match(r'^(?:https?:)?//(?:mc\.yandex\.ru/metrika/|widgets-\d+\.livetex\.me/js/iframe\.)', node['src']):
            node.decompose()
    # Адаптивное меню переставляет именованные слоты при загрузке шрифтов.
    # Содержимое и ссылки каждого пункта остаются в сравнении.
    for parent in soup.select('nav ul, header ul'):
        slots = parent.find_all('li', recursive=False) if parent.find('li', attrs={'data-nav-slot': True}, recursive=False) else []
        for original, ordered in zip(slots, sorted((node.__copy__() for node in slots),
                                                  key=lambda node: node.get_text(' ', strip=True))):
            original.replace_with(ordered)
    # Меняется адрес картинки проверки пользователя, а не данные компании.
    for node in soup.select('img[src]'):
        if re.search(r'(?:^|/)captcha(?:/|\?)', node.get('src', ''), re.I):
            node['src'] = '<captcha>'
        original = node.get('data-original', '')
        if (original.startswith('https://static.tildacdn.com/')
                and node['src'].replace('/-/empty/', '/') == original):
            node['src'] = original
    for node in soup.find_all(['style', 'script', 'link', 'input', 'meta']):
        if (node.name == 'style'
                or (node.name == 'script' and node.get('src'))
                or (node.name == 'script' and hashlib.sha256(re.sub(r'\s+', ' ', node.get_text()).strip().encode()).hexdigest() in _TRACKING_BOOTSTRAPS)
                or (node.name == 'link' and 'stylesheet' in node.get('rel', []))
                or (node.name == 'meta' and (node.has_attr('charset')
                    or node.get('http-equiv', '').lower() == 'content-type'))
                or (node.name in {'input', 'meta'} and _TOKEN_NAME.fullmatch(node.get('name', '')))):
            node.decompose()
    parts = []
    for node in soup.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, Tag):
            attrs = {}
            for key, value in sorted(node.attrs.items()):
                if key in _NOISE_ATTRS or key.startswith('data-v-'):
                    continue
                if key == 'data-preloader-timeout' and node.has_attr('data-record-type'):
                    continue
                if key == 'data-animate-start-time' and node.has_attr('data-animate-style'):
                    continue
                if key == 'data-tracking-consent':
                    continue
                if key == 'disabled' and node.name == 'input' and (
                        node.has_attr('data-search-input-value') or node.find_parent(id='distributorFilter')):
                    continue
                if key == 'rel' and node.name == 'a':
                    value = [item for item in value if item not in ('noopener', 'noreferrer')]
                    if not value:
                        continue
                if key.startswith('data-artboard-proxy-') and node.has_attr('data-artboard-recid'):
                    continue
                attrs[key] = value
            parts.append(['tag', len(list(node.parents)), node.name, attrs])
        elif isinstance(node, NavigableString):
            value = str(node)
            if node.parent.name == 'script':
                value = _BITRIX_MESSAGE.sub(_bitrix_service_data, value)
                value = _INLINE_TOKEN.sub(lambda m: m.group(1) + '"<service-token>"', value)
                # JSON переставляет ключи без изменения данных; JS не исполняем.
                try:
                    value = json.dumps(json.loads(value), sort_keys=True, ensure_ascii=False)
                except (ValueError, TypeError):
                    value = value.strip()
            else:
                value = re.sub(r'\s+', ' ', value).strip()
            if value:
                parts.append(['text', len(list(node.parents)), value])
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
