DOMAIN = 'cooltech.ru'

# Неактивные вкладки (Преимущества, Тех. данные, Размеры…) на cooltech свёрстаны как
# display:none. Просим text_extractor НЕ вырезать их универсальным display:none-стриппингом —
# это полезный контент, а не шум; лишнее (чертёж) убирает _strip_drawing ниже.
KEEP_HIDDEN_TABS = True

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для cooltech.ru (самописная CMS «Алма»).
# Полезный контент лежит в .catalog_el внутри <main>:
#   .cel_main_block        — h1, главное фото, вводное описание (.full_in_right)
#   .cel_bottom            — расширенное описание товара
#   .cel_content_block     — вкладки с характеристиками, преимуществами, таблицами
# Всё остальное — шапка, меню, хлебные крошки, слайдер партнёров, подвал — шум.
NOISE_SELECTORS = [
    # Шапка, логотип-баннер, навигация
    'header', 'footer', 'nav', '.small_header',
    'aside', '.left_block', '.left_menu',
    # Горизонтальное меню и скрипты под него
    '.menu_cont_hor',
    # Слайдер логотипов партнёров
    '.slider_logo_cont', '.slider_logo',
    # Хлебные крошки
    '.breadcrumbs',
    # Слайдер миниатюр фотографий (оставляем только главный снимок)
    '.cel_main_block_addon_images',
    # Блок цены, складских остатков, бонусов и кнопок заказа
    '.cel_price_block', '.cel_controle_cont', '.cel_bonus', '.cel_ostatok',
    # Форма-заявка «заполните опросный лист» (не контент, а CTA-форма)
    '.specblock',
    # Навигация «Вернуться»
    '.ni_nav',
    # Вкладки-навигация (заголовки вкладок без содержимого)
    'ul.tabs',
    # Служебные теги
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def _is_drawing_legend_table(table) -> bool:
    """Таблица-ЛЕГЕНДА ЧЕРТЕЖА: заголовки — одиночные буквы (A, B, C, D…), которые
    подписаны выносками ТОЛЬКО на чертеже. Без чертежа такая таблица бессмысленна.
    Признак: >=3 заголовочных ячейки — ровно одна заглавная буква (лат./кир.).
    Обычные таблицы (Артикул/Наименование, Тип/Q кВт/Габариты, аналоги COOLTECH/Mycom)
    самодостаточны и под правило НЕ попадают."""
    row = table.find('tr')
    if not row:
        return False
    heads = [td.get_text(' ', strip=True) for td in row.find_all(['td', 'th'])]
    return sum(1 for h in heads if re.fullmatch(r'[A-ZА-Я]', h or '')) >= 3


def _strip_drawing(soup: BeautifulSoup):
    """Упраздняет данные, ОТНОСЯЩИЕСЯ К ЧЕРТЕЖУ (чертёж в карточку товара не выводится):
      · таблицу-легенду с буквенными обозначениями A/B/C… (см. _is_drawing_legend_table);
      · блок «Чертеж»/«Чертёж» (заголовок + следующая за ним картинка).
    Такие блоки на cooltech.ru лежат во вкладке «Размеры» (div.box) — если легенда/«Чертеж»
    внутри такого box, удаляем весь box целиком; иначе — точечно таблицу/заголовок+картинку.
    Полезные вкладки (Характеристики, Преимущества, Тех. данные) не затрагиваются."""
    targets = []
    for table in soup.find_all('table'):
        if _is_drawing_legend_table(table):
            targets.append(table.find_parent(class_='box') or table)
    for h in soup.find_all(re.compile(r'^h[1-6]$')):
        if re.fullmatch(r'Черт[её]ж\.?', h.get_text(' ', strip=True), re.IGNORECASE):
            box = h.find_parent(class_='box')
            if box is not None:
                targets.append(box)
            else:
                targets.append(h)
                sib = h.find_next_sibling()
                if sib is not None:
                    targets.append(sib)
    seen = set()
    for t in targets:
        if t is None or id(t) in seen:
            continue
        seen.add(id(t))
        try:
            t.decompose()
        except Exception:
            pass


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для cooltech.ru блоки-шум.

    Сохраняет: h1, главное фото (.jsMainImage), вводное описание (.full_in_right),
    расширенное описание (.cel_bottom), вкладочный контент (.tabs_content) с
    таблицами характеристик и списками преимуществ.
    """
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()

    # Выноски-цитаты (.quote_big) — информативный контент, оставляем.
    # Пустые блоки .clear — технические распорки, убираем.
    for tag in soup.select('.clear'):
        tag.decompose()

    # Данные, относящиеся к чертежу (легенда A/B/C… + блок «Чертеж») — упраздняем:
    # чертёж в карточку не идёт, а таблица размеров без него нечитаема.
    _strip_drawing(soup)


def extract(html: str, base_url: str = '') -> str | None:
    """Извлекает карточку товара cooltech.ru в Markdown.

    Структура страницы после clean():
      <main>
        .catalog_el
          .cel_main_block      — h1 + главное фото + вводный текст
          .cel_bottom          — расширенное описание
          .cel_content_block   — .tabs_content с характеристиками и таблицами

    Возвращает None, если блок .catalog_el не найден
    (тогда отрабатывает общий конвертер html_to_markdown).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    catalog_el = soup.select_one('.catalog_el')
    if not catalog_el:
        # Запасной вариант: берём весь <main> или весь документ
        fallback = soup.find('main') or soup
        conv = MarkdownConverter(heading_style='ATX', bullets='-')
        md = conv.convert(str(fallback)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md.strip() or None

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    parts = []

    # --- Главный блок: h1 + главное фото + вводный текст ---
    cel_main = catalog_el.select_one('.cel_main_block')
    if cel_main:
        md = conv.convert(str(cel_main)).strip()
        if md:
            parts.append(md)

    # --- Расширенное описание (если есть) ---
    cel_bottom = catalog_el.select_one('.cel_bottom')
    if cel_bottom:
        md = conv.convert(str(cel_bottom)).strip()
        if md:
            parts.append(md)

    # --- Вкладочный контент: характеристики, таблицы, преимущества ---
    tabs_content = catalog_el.select_one('.tabs_content')
    if tabs_content:
        md = conv.convert(str(tabs_content)).strip()
        if md:
            parts.append(md)

    if not parts:
        return None

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
