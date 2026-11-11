"""Технические DOCX, явно привязанные к товару МЛКЗ, до числового гейта."""
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup


class TechnicalSourceError(ValueError):
    """Источник нельзя прочитать: товар следует повторить, а не отвергать."""


def technical_source_urls(html, page_url):
    if (urlsplit(page_url).hostname or '').lower() not in ('mlkz.by', 'www.mlkz.by'):
        return []
    soup = BeautifulSoup(html, 'lxml')
    item = soup.select_one('#k2Container.itemView')
    if item is None or item.select_one('h1.itemTitle') is None:
        return []
    urls = []
    for link in item.select('.itemAttachments a[href]'):
        if link.get_text(' ', strip=True).casefold() != 'технические характеристики':
            continue
        url = urljoin(page_url, link['href'])
        parts = urlsplit(url)
        if (parts.scheme in ('http', 'https')
                and parts.hostname == urlsplit(page_url).hostname
                and '/download/' in parts.path and url not in urls):
            urls.append(url)
    return urls


def docx_markdown(path):
    """Сохраняет абзацы и границы ячеек; не включает коды полей Word."""
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    try:
        with ZipFile(path) as source:
            xml = source.read('word/document.xml')
        root = ET.fromstring(xml)
    except (OSError, BadZipFile, KeyError, ET.ParseError) as exc:
        raise TechnicalSourceError(f'Нечитаемый технический DOCX: {path}') from exc
    def paragraph_text(paragraph):
        parts = []
        for element in paragraph.iter():
            if element.tag == '{' + ns['w'] + '}t':
                parts.append(element.text or '')
            elif element.tag == '{' + ns['w'] + '}sym':
                symbol = element.get('{' + ns['w'] + '}char', '').upper()
                font = element.get('{' + ns['w'] + '}font', '')
                if font != 'Symbol' or symbol not in ('F0B0', 'F0B1'):
                    raise TechnicalSourceError(f'Неизвестный символ DOCX: {font}/{symbol}')
                parts.append({'F0B0': '°', 'F0B1': '±'}[symbol])
        return ''.join(parts)

    body = root.find('w:body', ns)
    blocks = []
    if body is not None:
        for block in body:
            if block.tag.endswith('}p'):
                text = paragraph_text(block)
                if text.strip():
                    blocks.append(text.strip())
            elif block.tag.endswith('}tbl'):
                for row in block.findall('w:tr', ns):
                    cells = [' '.join(paragraph_text(p)
                                      for p in cell.findall('.//w:p', ns))
                             for cell in row.findall('w:tc', ns)]
                    blocks.append(' | '.join(cells))
    text = '\n\n'.join(blocks)
    if not text.strip():
        raise TechnicalSourceError(f'Пустой технический DOCX: {path}')
    return text


async def technical_sources_markdown(html, page_url, downloader, company_dir):
    blocks = []
    for url in technical_source_urls(html, page_url):
        path = await downloader._download_aiohttp(
            url, str(Path(downloader.config.base_dir) / 'technical_sources' / Path(company_dir).name), 'product_page_file')
        if not path:
            raise TechnicalSourceError(f'Не загружен технический источник: {url}')
        blocks.append(f'## Технические характеристики из документа\n\nИсточник: {url}\n\n{docx_markdown(path)}')
    return '\n\n'.join(blocks)
