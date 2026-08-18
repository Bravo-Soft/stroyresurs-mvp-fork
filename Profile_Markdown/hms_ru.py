DOMAIN = 'hms.ru'

import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import sys
import os
# Добавляем родительскую директорию в путь для импорта MarkdownConverter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter, sup_tag_to_caret

def clean(soup: BeautifulSoup):
    """Удаляет специфичные для hms.ru блоки."""
    selectors = [
        'header', 'footer', '.breadcrumbs', '.catalog-menu',
        '.content-menu', '.sidebar', '.search', '.footer-menu',
        '.footer-contacts', '.footer-bottom', '.table_details div[align="right"]',
        '.zakaz', '.print', '.pdf'
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()
    # Дополнительно скрытые блоки (уже удаляются общим clean_noise, но оставим на всякий случай)
    for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        tag.decompose()

def extract(html: str, base_url: str = '') -> str | None:
    """
    Специализированное извлечение для страниц товаров hms.ru.
    Возвращает Markdown или None, если не удалось.
    """
    soup = BeautifulSoup(html, 'lxml')
    # Применяем свою очистку
    clean(soup)

    # Поиск основного контейнера
    main_container = (
        soup.select_one('.catalog-element .item_details') or
        soup.select_one('.catalog-element') or
        soup.select_one('.item_details') or
        soup.select_one('#work_text .item_details') or
        soup.select_one('.catalog-detail__content') or
        soup.select_one('.catalog-detail')
    )
    if not main_container:
        return None

    # Удаляем лишнее внутри контейнера
    for unwanted in main_container.select('.table_details div[align="right"], .zakaz, .print, .pdf, .item_details .table_details > div[align="right"]'):
        unwanted.decompose()
    for hidden in main_container.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        hidden.decompose()

    def process_element(el):
        if getattr(el, 'name', None) is None:
            text = str(el).strip()
            return text if text else ''
        name = el.name.lower()
        if name == 'br':
            return '  \n'
        if name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(name[1])
            text = el.get_text(separator=' ', strip=True)
            return f'\n{"#" * level} {text}\n\n' if text else ''
        if name in ['b', 'strong']:
            text = el.get_text(separator=' ', strip=True)
            if not text:
                return ''
            next_name = getattr(el.next_sibling, 'name', None)
            if text.isupper() or next_name in ['table', 'ul', 'ol']:
                return f'\n**{text}**\n\n'
            return f'**{text}**'
        if name in ['em', 'i']:
            inner = ''.join(process_element(child) for child in el.children)
            return f'*{inner}*' if inner else ''
        if name == 'sup':
            return sup_tag_to_caret(el)
        if name in ['p', 'div']:
            inner = ''.join(process_element(child) for child in el.children)
            return f'\n{inner.strip()}\n\n' if inner.strip() else ''
        if name in ['ul', 'ol']:
            items = []
            for li in el.find_all('li', recursive=False):
                li_text = ''.join(process_element(child) for child in li.children).strip()
                if li_text:
                    items.append(f'- {li_text}')
            return '\n'.join(items) + '\n\n' if items else ''
        if name == 'table':
            converter = MarkdownConverter()
            try:
                return converter.convert_table(el, '')
            except Exception:
                return ''
        if name == 'img':
            src = el.get('src') or el.get('data-src') or ''
            alt = el.get('alt', '')
            if src and base_url:
                src = urljoin(base_url, src)
            return f'![{alt}]({src})\n\n' if src else ''
        if name == 'a':
            href = el.get('href', '').strip()
            text = el.get_text(strip=True)
            if href and base_url:
                href = urljoin(base_url, href)
            if href:
                return f'[{text}]({href})' if text else f'<{href}>'
            return text
        return ''.join(process_element(child) for child in el.children)

    parts = []
    for child in main_container.children:
        if getattr(child, 'name', None) is None and not str(child).strip():
            continue
        try:
            parts.append(process_element(child))
        except Exception as e:
            continue
    markdown = ''.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown).strip()
    return markdown if markdown else None