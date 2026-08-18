DOMAIN = 'betonn.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для betonn.ru (сайт завода ЖБИ «Промбетон», г. Нижний Новгород,
# самописная CMS). Полезный контент лежит в <main id="cnt">:
#   - h1.page-header          — заголовок товара
#   - .prdt_imgs              — фото товара
#   - #prdt_desc / table.prdt_props — характеристики (таблица)
#   - #prdt_delivery          — условия доставки
#   - #prdt_dop               — дополнительное описание
# Всё остальное — шапка, меню, боковая колонка-каталог, хлебные крошки,
# «С этим товаром покупают», «Наши преимущества», форма заявки,
# кнопка «Купить», «Поделиться», контакты менеджера, footer, модальные окна — шум.
NOISE_SELECTORS = [
    # шапка / логотип / главное меню / поиск / корзина
    'header', '.hdr', '#hdr',
    # подвал
    'footer', '#ftr', '#ftr_ndr', '#ftr_top', '#ftr_inner', '.ftr-fixed',
    # боковая колонка с каталогом
    'aside',
    # навигация (главное меню, мобильное меню, боковое меню)
    'nav',
    # хлебные крошки (блок перед <main>)
    '#cnt_bfr',
    # cookie-уведомление и маска мобильного меню
    '.cookie_popup', '.c-mask',
    # выбор города / модальное окно выбора города
    '.cities_accept', '.modal', '.modalsearch',
    # badge reCAPTCHA
    '#recaptcha_badge',
    # «С этим товаром покупают»
    '.pohozie_tovari', '#plus_pohozie_tovari',
    # «Наши преимущества»
    '.prdt_advantages',
    # форма «Нужна консультация?» — отдельный блок вне карточки товара
    '.mdl_fos', '#fos',
    # кнопки «Купить», «Поделиться», блок контакта менеджера — коммерческий шум
    '.prdt_buy', '.prdt_buy_buttons', '.podelitsa', '.manager-info',
    # навигация по вкладкам («Характеристики / Доставка / Дополнительно»)
    # сами вкладки оставляем, но второй .prdt_row (только ul вкладок, без содержимого) убираем
    # NOTE: используем точечный decompose в clean(), а не CSS-селектор, чтобы не зацепить первый .prdt_row
    # служебные теги
    # ВАЖНО: <form> НЕ удаляем здесь — вся карточка товара (#prdt_desc, .prdt_imgs,
    # .tab-content) обёрнута в один <form> внутри #product_detail. Удаление form
    # уничтожило бы весь полезный контент. Формы-шум (#fos) удалены выше по id.
    'script', 'style', 'noscript', 'iframe', 'svg',
]


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для betonn.ru блоки шума: шапку, меню, боковую колонку,
    хлебные крошки, «С этим товаром покупают», «Наши преимущества», форму заявки,
    кнопки «Купить»/«Поделиться», контакт менеджера, footer, модальные окна, скрипты."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()

    # Второй .prdt_row — только навигация вкладок («Характеристики / Доставка / Дополнительно»)
    # без контента: убираем его отдельно, чтобы не тронуть первый .prdt_row с фото и ценой
    prdt_rows = soup.select('.prdt_row')
    for row in prdt_rows:
        # Навигационный ряд не содержит таб-контент и не является .prdt_row.row
        if 'row' not in (row.get('class') or []) and row.select_one('.tab-content') is None:
            # Это чистый навигационный div — удаляем
            if not row.select_one('table, img, #prdt_desc, #prdt_delivery, #prdt_dop, .prdt_imgs'):
                row.decompose()


def extract(html: str, base_url: str = '') -> str | None:
    """Карточка товара betonn.ru: заголовок + фото + таблица характеристик + доставка + описание.

    Структура страницы (самописная CMS):
      <main id="cnt">
        <h1 class="page-header">…</h1>
        <div class="shop_prdt catalog_main" id="shop_prdt">
          <div class="prdt_dtl shk-item" id="product_detail">
            <!-- .prdt_row.row: два столбца — фото (.prdt_imgs) и цена+кнопки (.prdt_info) -->
            <!-- .prdt_row: вкладки-навигация (убирается в clean) -->
            <!-- .tab-content: #prdt_desc (характеристики+таблица), #prdt_delivery, #prdt_dop -->
          </div>
        </div>
        <div class="pohozie_tovari"> ... </div>   <!-- убирается в clean -->
        <div class="prdt_advantages"> ... </div>  <!-- убирается в clean -->
        <div class="mdl_fos"> ... </div>           <!-- убирается в clean -->
      </main>

    Возвращает None только если на странице отсутствует #cnt (тогда отрабатывает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    cnt = soup.select_one('#cnt')
    if not cnt:
        # Запасной вариант — весь основной контент без боковой колонки
        cnt = soup.select_one('#cnt_wrap') or soup.select_one('#sect_main')
    if not cnt:
        return None

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    parts = []

    # Заголовок товара
    h1 = cnt.find('h1')
    if h1:
        title_text = h1.get_text(strip=True)
        if title_text:
            parts.append(f'# {title_text}')
        h1.decompose()  # убираем из дерева, чтобы не дублировать ниже

    # Изображение товара
    prdt_imgs = cnt.select_one('.prdt_imgs')
    if prdt_imgs:
        imgs_md = conv.convert(str(prdt_imgs)).strip()
        if imgs_md:
            parts.append(imgs_md)

    # Характеристики: #prdt_desc содержит h5 + table.prdt_props
    prdt_desc = cnt.select_one('#prdt_desc')
    if prdt_desc:
        desc_md = conv.convert(str(prdt_desc)).strip()
        if desc_md:
            parts.append(desc_md)

    # Доставка: #prdt_delivery
    prdt_delivery = cnt.select_one('#prdt_delivery')
    if prdt_delivery:
        del_md = conv.convert(str(prdt_delivery)).strip()
        if del_md:
            parts.append(del_md)

    # Дополнительное описание: #prdt_dop
    prdt_dop = cnt.select_one('#prdt_dop')
    if prdt_dop:
        dop_md = conv.convert(str(prdt_dop)).strip()
        if dop_md:
            parts.append(dop_md)

    # Описание товара со сферами применения: div.prdt_desc.shop_desc (КЛАСС, не id!) —
    # вкладка «Описание» («Широкое применение этих элементов…»). Адаптер брал только
    # #prdt_desc (id, таблица характеристик), и описание/применение терялись (D55, ФБС 9.3.6-Т).
    for shop_desc in cnt.select('div.prdt_desc.shop_desc'):
        if shop_desc.get('id') == 'prdt_desc':
            continue  # таблица характеристик уже добавлена выше
        sd_md = conv.convert(str(shop_desc)).strip()
        if sd_md:
            parts.append(sd_md)

    # Если ничего кроме заголовка не нашли — конвертируем весь #cnt целиком
    if len(parts) <= 1:
        fallback_md = conv.convert(str(cnt)).strip()
        if fallback_md:
            parts = [fallback_md]

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
