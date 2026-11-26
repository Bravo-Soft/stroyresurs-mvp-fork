"""Версии повторного извлечения Tegola, окон Tilda и товаров лендингов."""
import re
from urllib.parse import urlparse
from product_interactions import tilda_product_key
from inline_products import inline_product_key
from profnastil_configurator import configurator_slug

NUMERIC_GATE_REVISION = 'range-endpoints-v1'
TEGOLA_EXTRACTION_REVISION = 'tegola-v3-public-variants-20261030'


def product_extraction_revision(url):
    if configurator_slug(url):
        return "profnastil-public-options-v1-20261126"
    parsed = urlparse(url or '')
    if (parsed.hostname or '').removeprefix('www.') == 'alumil-yug.ru':
        return 'alumil-visible-thermal-conditions-v2-20261118'
    if (parsed.hostname or '').removeprefix('www.') == 'v-kirpich.ru' and parsed.path.startswith('/catalog/'):
        return 'vkirpich-public-delivery-mix-v4-20261118'
    if parsed.hostname == 'products.metalloinvest.com' and parsed.path.startswith('/catalog/'):
        return 'metalloinvest-complete-public-matrices-v7-20261118'
    if ((parsed.hostname or '').removeprefix('www.') == 'konord.com' and parsed.path.startswith('/catalog/')):
        return 'konord-public-series-description-v1-20261118'
    if ((parsed.hostname or '').removeprefix('www.') == 'carlisle-llc.ru'
            and parsed.path == '/catalog/geomembrane/description.html'):
        return 'public-product-image-text-carlisle-v7-20261118'
    if (((parsed.hostname or '').removeprefix('www.') == 'rvul.ru'
            and parsed.path == '/vse-produkty/lenta-samokleyuschayasya-las-las-a/')
            or ((parsed.hostname or '').removeprefix('www.') == 'carlisle-llc.ru'
                and parsed.path == '/catalog/geomembrane/description.html')):
        return 'public-product-image-text-v3-20261117'
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
