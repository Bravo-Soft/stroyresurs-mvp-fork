DOMAIN = '//xn----7sbegqnkyhbtn.xn--p1ai/'

from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def clean(soup: BeautifulSoup):
    """Удаляет специфичные для agroskon блоки."""
    selectors = [
        'header', 'footer', '.burger', '#mobile-mnu',
        '.search-holder', '.theme-modal', '.breadcrumbs',
        '.price-list', '.info-wrapper .info-name', '.phones__holder',
        '.email__holder', '.dev', '.rights', '.footer-wrapper .menu-holder',
        '.bottom-wrapper .privacy-holder'
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()

# extract отсутствует – общий метод после очистки