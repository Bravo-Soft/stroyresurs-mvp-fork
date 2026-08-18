DOMAIN = 'tss.ru'

import re
from bs4 import BeautifulSoup, Tag
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для tss.ru (собственная CMS на базе Bitrix).
# Полезное лежит в .new_with: заголовок (.bloc_title_new), галерея (.galari_new),
# блок характеристик (.product-information с .haractiris_new) и описание (.opisanie_tovar).
# Всё остальное — меню, шапка, подвал, хлебные крошки, слайдеры «похожих»,
# форма заказа, всплывающие попапы — шум.
NOISE_SELECTORS = [
    # Шапка / навигация / мобильное меню
    '.header-new-slader-block', '.header-new-blocks-mobi', '.mobi_search_block',
    '.menu_mobi_top_new', '.catalog_menu_blcok', '.pustota_scrol',
    '.header-new-blocks', '.headers_catalog_menu', '.catalog_menu',
    # Корзина / оверлей
    '#layer_cart', '.layer_cart_overlay',
    # Хлебные крошки
    '.breadcrumb',
    # Подвал
    '.block_foter_link', '.foter_mobi_dop', '.footer-new',
    # Попапы форм (обратный звонок, заказ, отзыв, запчасти)
    '.form-fancybox',
    # Telegram-баннер
    '.block_telegram_bg',
    # Вспомогательные элементы навигации
    '#back-to-top', '#toTop', '#preloader',
    '.url_strani', '.region_none',
    # Блок «Варианты исполнения» (слайдер карточек-двойников)
    '.slide_element_dop',
    # Блоки «Аналоги по мощности», «Запчасти», «Расходные материалы», видеообзор
    '.slide_element2',
    # Слайдеры похожих товаров (динамически сгенерированные классы sliderdop-*)
    # обрабатываются отдельно в clean() через prefix-поиск
    # Карточки товаров внутри слайдеров
    '.block_tovar',
    # Навигация по вкладкам (сами ссылки-якоря, не контент)
    '.tab_blocks',
    # Блок загрузки PDF / каталогов
    '.link_kp_block_new',
    # Правая колонка: цена, кнопки «купить», «в закладки», «сравнить»
    '.pb-right-column',
    # Блок мобильной серии (дубль заголовка/таблицы)
    '.mobi_seria_tabli', '.galari_mobi_slade',
    # Уведомление «Артикул скопирован»
    '.copu_wspl',
    # «Примеры реализованных задач» и прочие маркетинговые блоки ниже карточки
    '.new_projects_second_block',
    # Стандартные служебные теги
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для tss.ru блоки (шапка, меню, подвал, слайдеры, формы, попапы)."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()

    # Удаляем динамические классы sliderdop-SLADER_* (слайдеры похожих/аналогов)
    for tag in soup.find_all('div'):
        cls_list = tag.get('class') or []
        if any(c.startswith('sliderdop-') for c in cls_list):
            tag.decompose()

    # Удаляем разделители .border_dop внутри строк характеристик
    # (пустые div-спейсеры между ключом и значением — в Markdown они лишние)
    for tag in soup.select('.border_dop'):
        tag.decompose()


def _spec_row(row_tag: Tag):
    """Извлекает пару (параметр, значение) из тега .haractiris_new.

    Структура после удаления .border_dop:
      <div class="haractiris_new">
        <div>Серия</div>
        <div>SDG</div>
      </div>
    Игнорируем NavigableString (пробельные узлы) — берём только Tag-потомков первого уровня.
    Возвращает None, если данные не удалось извлечь.
    """
    tag_children = [c for c in row_tag.children if isinstance(c, Tag)]
    if len(tag_children) < 2:
        return None
    key = tag_children[0].get_text(strip=True)
    val = tag_children[-1].get_text(strip=True)
    if not key or not val:
        return None
    return key.replace('|', '\\|'), val.replace('|', '\\|')


def _build_spec_table(container) -> str:
    """Конвертирует блоки .haractiris_new в Markdown-таблицу характеристик.

    Структура DOM (после clean):
      .product-information
        .block_fon_harakterist
          .block_det_harakterist
            .product-features-tab-content
              .block_det_harakterist2
                p.one_chars_item_title_element  <- название группы
                .blockharakter
                  .haractiris_new  <- строки: [ключ][значение]

    Заголовки групп (.one_chars_item_title_element) — предшествующие сиблинги .blockharakter,
    поэтому ищем их через обход предыдущих братьев.
    """
    parts = []
    table_started = False

    for bh in container.select('.blockharakter'):
        # Ищем заголовок группы — ближайший предшествующий Tag-сиблинг с нужным классом.
        # Два варианта разметки tss.ru: p.one_chars_item_title_element (старый) и
        # div.name_zagl_tabl > a (новый: «Двигатель Cummins KTA50-G16B», «Генератор TSS-SA-1600»).
        # Без заголовков секций одинаково названные параметры разных частей товара
        # (Мощность номинальная общая 1600 vs двигателя 1760) схлопывались при извлечении (D54).
        section_title = ''
        for prev in bh.previous_siblings:
            if not isinstance(prev, Tag):
                continue
            cls_prev = prev.get('class') or []
            if 'one_chars_item_title_element' in cls_prev or 'name_zagl_tabl' in cls_prev:
                section_title = prev.get_text(strip=True)
            break  # Берём только непосредственного предшественника-тега

        rows = bh.select('.haractiris_new')
        if not rows:
            continue

        if section_title:
            if parts:
                parts.append('')
            parts.append(f'### {section_title}')
            parts.append('')
            table_started = False

        if not table_started or section_title:
            parts.append('| Параметр | Значение |')
            parts.append('| --- | --- |')
            table_started = True

        for row in rows:
            pair = _spec_row(row)
            if pair:
                parts.append(f'| {pair[0]} | {pair[1]} |')

    # Фоллбэк: плоский список .haractiris_new (если структура нестандартная)
    if not parts:
        rows = container.select('.haractiris_new')
        if rows:
            parts.append('| Параметр | Значение |')
            parts.append('| --- | --- |')
            for row in rows:
                pair = _spec_row(row)
                if pair:
                    parts.append(f'| {pair[0]} | {pair[1]} |')

    return '\n'.join(parts).strip()


def extract(html: str, base_url: str = '') -> str | None:
    """Специализированное извлечение для карточек товаров tss.ru.

    Структура страницы (Bitrix/собственная CMS):
    - .new_with — главный блок карточки товара
      - .bloc_title_new — заголовок H1
      - .primary_block — галерея фото + правая колонка с ценой (правую удаляем)
        - .galari_new — изображения товара (сохраняем)
      - .product-information — характеристики + описание
        - .haractiris_new — строки таблицы характеристик (конвертируем в Markdown-таблицу)
        - .opisanie_tovar — текстовое описание

    Возвращает None, если структура не распознана (тогда отработает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # Берём первый основной блок карточки товара
    nw = soup.select_one('.new_with')
    if not nw:
        # Фоллбэк: общий контейнер страницы
        nw = soup.select_one('#center_column')
        if not nw:
            return None

    parts = []

    # --- Заголовок H1 ---
    h1 = nw.find('h1')
    if h1:
        title_text = h1.get_text(strip=True)
        if title_text:
            parts.append(f'# {title_text}')

    # --- Изображения товара (.galari_new) ---
    gallery = nw.select_one('.galari_new')
    if gallery:
        # Оставляем только главное фото (#image-block) — миниатюры-дубли убираем
        for thumb_block in gallery.select('#views_block'):
            thumb_block.decompose()
        for nav_btn in gallery.select('#el_prev, #el_next'):
            nav_btn.decompose()
        conv_img = MarkdownConverter(heading_style='ATX', bullets='-')
        img_md = conv_img.convert(str(gallery)).strip()
        img_md = re.sub(r'\n{2,}', '\n', img_md).strip()
        if img_md:
            parts.append(img_md)

    # --- Таблица технических характеристик ---
    prod_info = nw.select_one('.product-information')
    if prod_info:
        spec_table = _build_spec_table(prod_info)
        if spec_table:
            parts.append('## Характеристики')
            parts.append(spec_table)

        # Рекомендуемые расходники (.name_zagl_tabl) — полезный контент
        rec_links = []
        for zagl in prod_info.select('.name_zagl_tabl'):
            link = zagl.find('a')
            if link:
                text = link.get_text(strip=True)
                href = link.get('href', '')
                if text:
                    if href and not href.startswith('javascript'):
                        rec_links.append(f'- {text}: {href}')
                    else:
                        rec_links.append(f'- {text}')
        if rec_links:
            parts.append('\n'.join(rec_links))

        # --- Описание товара (.opisanie_tovar) ---
        desc = prod_info.select_one('.opisanie_tovar')
        if desc:
            conv_desc = MarkdownConverter(heading_style='ATX', bullets='-')
            desc_md = conv_desc.convert(str(desc)).strip()
            desc_md = re.sub(r'\n{3,}', '\n\n', desc_md).strip()
            if desc_md:
                parts.append('## Описание')
                parts.append(desc_md)

    if not parts:
        return None

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
