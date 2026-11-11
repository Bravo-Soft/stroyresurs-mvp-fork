"""Версии повторного извлечения Tegola, окон Tilda и товаров лендингов."""
import re
from urllib.parse import urlparse
from product_interactions import tilda_product_key
from inline_products import inline_product_key

TEGOLA_EXTRACTION_REVISION = 'tegola-v3-public-variants-20261030'


def product_extraction_revision(url):
    parsed = urlparse(url or '')
    if ((parsed.hostname or '').lower().removeprefix('www.') == 'hms-livgidromash.ru'
            and parsed.path.startswith('/catalog/')):
        return 'hms-maincontent-v1-20261110'
    if (parsed.hostname or '').lower().removeprefix('www.') in {
            'npo-stekloplastic.ru', 'nzmk22.ru', 'bsdbel.ru', 'ventexpress.ru',
            'zavodmel.ru', 'quality-pro.ru', 'specbolt.ru', 'xotpipe.ru',
            'ecosgroup.com', 'landshaft.ru', 'mlkz.by', 'st-paint.ru',
            'bitumex.org', 'nizhegorodsetkabel.ru', 'bkmz.ru'}:
        return 'visible-html-quality-v1-20261106'
    if tilda_product_key(url or ''):
        return 'tilda-product-popup-v1-20261102'
    if inline_product_key(url or ''):
        return 'inline-products-v1-20261102'
    parsed = urlparse(url or '')
    if parsed.hostname in ('tegola.ru', 'www.tegola.ru') and re.fullmatch(r'/product/[^/]+/?', parsed.path):
        return TEGOLA_EXTRACTION_REVISION
    return None
