DOMAIN = 'ventland.ru'

import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter, sup_tag_to_caret

def clean(soup: BeautifulSoup):
    """Удаляет специфичные для ventland.ru блоки."""
    selectors = [
        'table.head', 'table.head2', 'table.bot1',
        '.top_index', '.top_index_top', '.reg',
        '.nav_3', '.nav_2', '.nav_p', '.nav_5', '.nav_5_1', '.nav_5_2',
        '.tab_5_3_c', '.tab_5', '.bread_1', '.bread_2'
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()
    # В ventland.ru не удаляем table.head3 (там контент)

def extract(html: str, base_url: str = '') -> str | None:
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    main = soup.select_one('.main_2') or soup.select_one('td.main_2')
    if not main:
        return None

    for unwanted in main.select('.tab_5_3_c, .tab_5, .print, .pdf, .zakaz, .reg'):
        unwanted.decompose()

    def process_element(el):
        if getattr(el, 'name', None) is None:
            return str(el).strip()
        name = el.name.lower()
        if name == 'br':
            return '  \n'
        if name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(name[1])
            text = el.get_text(separator=' ', strip=True)
            return f'\n{"#" * level} {text}\n\n' if text else ''
        if name in ['b', 'strong']:
            text = el.get_text(separator=' ', strip=True)
            return f'**{text}**' if text else ''
        if name in ['em', 'i']:
            inner = ''.join(process_element(child) for child in el.children)
            return f'*{inner}*' if inner else ''
        if name == 'sup':
            return sup_tag_to_caret(el)
        if name == 'p':
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
            return converter.convert_table(el, '')
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
    for child in main.children:
        if getattr(child, 'name', None) is None and not str(child).strip():
            continue
        parts.append(process_element(child))
    markdown = ''.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() if markdown.strip() else None