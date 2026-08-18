DOMAIN = 'tizol.com'

import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter

def clean(soup: BeautifulSoup):
    """Удаляет специфичные для tizol.com блоки."""
    selectors = [
        '.preloader', '.product-page__application-list', '.product-page__application-box',
        '.breadcrumbs', '.product-page__similar-products', '.product-page__dealers-box',
        '.dealers-list', '.update-warning', '#jivo-iframe-container', 'jdiv',
        '.product-page__gallery-top', '.product-page__gallery-thumbs', '.product-page__action-box',
        '.product-page__action-box--mobile', '.product-page__sections-box',
        'script', 'noscript', 'style', 'iframe', '.swiper-container', '.product-page__slider-btn',
        '.product-page__product-gallery', '.product-card', '.products-list-wrap'
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()

def extract(html: str, base_url: str = '') -> str | None:
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # Заголовок h1
    h1_tag = soup.find('h1', class_='product-page__title')
    title_text = ""
    if h1_tag:
        title_text = h1_tag.get_text(strip=True)
    elif soup.title and soup.title.string:
        title_text = soup.title.string.strip()

    def get_slot_content(slot_name: str) -> str:
        selector = f'template[v-slot\\:{slot_name}]'
        slot = soup.select_one(selector)
        if not slot:
            return ''
        inner_html = ''.join(str(child) for child in slot.contents if child.name or (isinstance(child, str) and child.strip()))
        if not inner_html.strip():
            return ''
        inner_soup = BeautifulSoup(inner_html, 'html.parser')
        for img in inner_soup.find_all('img'):
            src = img.get('src', '')
            if not src or '100х97.png' in src or 'чб.png' in src:
                img.decompose()
        conv = MarkdownConverter()
        md = conv.convert(str(inner_soup))
        return md.strip()

    content_parts = []
    if title_text:
        content_parts.append(f"# {title_text}\n")

    body_parts = []
    slot_names = ['description-text', 'product-specifics', 'description', 'specifics', 'application', 'package', 'certifications']
    for slot_name in slot_names:
        slot_content = get_slot_content(slot_name)
        if slot_content:
            body_parts.append(slot_content)

    # Фолбэк: в сохранённом HTML Vue-слотов (<template v-slot>) нет — страница отрендерена сервером.
    # Запускаем, когда слоты не дали ТЕЛА (проверяем body_parts, а не content_parts — там уже есть заголовок).
    # Берём основной контейнер product-page__inner, убрав заголовок (он уже добавлен), галерею,
    # кнопки и служебные изображения-бейджи.
    if not body_parts:
        product_inner = soup.select_one('div.product-page__inner')
        if product_inner:
            for unwanted in product_inner.select(
                '.product-page__title, .product-page__title--mobile, '
                '.product-page__gallery-top, .product-page__gallery-thumbs, .product-page__action-box, '
                '.product-page__action-box--mobile, .product-page__sections-box, .product-card, '
                '.product-page__product-gallery'
            ):
                unwanted.decompose()
            for img in product_inner.find_all('img'):
                src = img.get('src', '')
                if not src or '100х97.png' in src or 'чб.png' in src:
                    img.decompose()
            conv = MarkdownConverter()
            md = conv.convert(str(product_inner))
            if md.strip():
                body_parts.append(md.strip())

    content_parts.extend(body_parts)

    if not content_parts:
        return None

    markdown = "\n\n".join(content_parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip()