DOMAIN = 'alfapol.ru'

from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def clean(soup: BeautifulSoup):
    """Удаляет специфичные для alfapol.ru блоки."""
    selectors = [
        'header', 'footer', '.product-subslider',
        '.product-similar', '.product-recent',
        '.breadcrumbs', '.header__catalog-menu',
        '.product-examples', '.areas-materials__activity',
        '.product-hero__slider-remote', '.product-hero__slider-buttons',
        '.product-hero__slider-pagination'
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()
    # Дополнительные специфичные элементы
    for bc in soup.select('.breadcrumbs')[1:]:
        bc.decompose()
    for btn in soup.select('.areas-materials__slide-btns'):
        btn.decompose()

# Для alfapol нет специального extract, только очистка. После очистки общий метод справится.
# extract не определён – значит, адаптер только чистит, а конвертацию делает общий поток.