# data_extractor.py 1.0.0
from bs4 import BeautifulSoup
from typing import Dict, Any, List

class DataExtractor:
    """
    Извлекает структурированные данные со страницы, используя селекторы из профиля.
    """

    @staticmethod
    def extract(html: str, page_type: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        """
        page_type: 'product', 'contacts', 'distributor', 'files'
        """
        soup = BeautifulSoup(html, 'html.parser')
        result = {}

        if page_type == 'product':
            extractors = profile.get('extractors', {}).get('product_page', {})
            fields = extractors.get('fields', {})
            for field_name, selectors in fields.items():
                css_selectors = selectors.get('css', [])
                for css in css_selectors:
                    elem = soup.select_one(css)
                    if elem:
                        result[field_name] = elem.get_text(strip=True)
                        break

            # Специальная обработка для файлов
            file_links = []
            file_css = fields.get('files', {}).get('css', [])
            for css in file_css:
                for a in soup.select(css):
                    href = a.get('href')
                    if href:
                        file_links.append(href)
            result['files'] = file_links

        elif page_type == 'contacts':
            extractors = profile.get('extractors', {}).get('contacts_page', {})
            fields = extractors.get('fields', {})
            for field_name, selectors in fields.items():
                css_selectors = selectors.get('css', [])
                for css in css_selectors:
                    elem = soup.select_one(css)
                    if elem:
                        result[field_name] = elem.get_text(strip=True)
                        break

        elif page_type == 'distributor':
            extractors = profile.get('extractors', {}).get('dealer_page', {})
            container_selector = extractors.get('container')
            item_selector = extractors.get('item_selector')
            fields = extractors.get('fields', {})
            if container_selector and item_selector:
                container = soup.select_one(container_selector)
                if container:
                    items = container.select(item_selector)
                    dealers = []
                    for item in items:
                        dealer = {}
                        for field_name, sel in fields.items():
                            css = sel.get('css', [])
                            for c in css:
                                elem = item.select_one(c)
                                if elem:
                                    dealer[field_name] = elem.get_text(strip=True)
                                    break
                        dealers.append(dealer)
                    result['dealers'] = dealers

        elif page_type == 'files':
            file_extensions = profile.get('discovery_rules', {}).get('file_extensions', ['.pdf', '.doc', '.xls'])
            file_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                if any(href.lower().endswith(ext) for ext in file_extensions):
                    file_links.append({
                        'url': href,
                        'text': a.get_text(strip=True)
                    })
            result['files'] = file_links

        return result