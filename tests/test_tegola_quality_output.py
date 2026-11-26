"""Потери нулевой границы температуры и WEBP с расширением JPG."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from docx import Document
from PIL import Image

from docx_generator import DOCXGenerator
from file_converter import FileConverter


@pytest.mark.parametrize('name', ['Лента Бутилен Дуо Брико Р', 'Лента герметизирующая Бутилен'])
def test_butylen_application_zero_boundary_reaches_docx(tmp_path, name):
    gen = DOCXGenerator()
    async def no_office(path):
        return None
    gen._convert_to_rtf = no_office
    product = {'product_name': name, 'specifications': {'Длина, м': '20', 'Ширина, м': '0.02', 'Толщина, мм': '0.8'},
               'instructions_manuals': 'Температура применения, C: 0/+45',
               'additional_info': 'ДИАГНОСТИЧЕСКИЙ МУСОР'}
    data = {'product_id': 'butylen', 'source_url': 'https://tegola.ru/product/butilen', 'ai_raw_text': {'product': product}}
    path = asyncio.run(gen.generate_product_card(data, {}, str(tmp_path)))
    doc = Document(path)
    texts = [p.text for p in doc.paragraphs]
    assert 'Инструкции по применению' in texts
    assert 'Температура применения, C: 0/+45' in texts
    assert all('ДИАГНОСТИЧЕСКИЙ МУСОР' not in t for t in texts)


@pytest.mark.parametrize('value', ['Температура применения, C: 0/+45', 'Слой 0 мм недопустим, нанести 2 мм',
                                  'При температуре 0 C применять кисть', 'Температура применения: -10/+45 C'])
def test_zero_in_meaningful_text_does_not_hide_section(value):
    doc = Document()
    DOCXGenerator()._add_generic_field(doc, 'Инструкции по применению', value)
    assert value in [p.text for p in doc.paragraphs]


@pytest.mark.parametrize('value', ['0', '0.0', '0,0 кг', '0 кг'])
def test_standalone_zero_placeholder_still_omitted(value):
    doc = Document()
    DOCXGenerator()._add_generic_field(doc, 'Вес', value)
    assert not doc.paragraphs


def converter(tmp_path, office=sys.executable):
    return FileConverter(str(tmp_path), libreoffice_path=office, delete_originals=False, delete_empty_folders=False)


@pytest.mark.parametrize('suffix,mode', [('.jpg', 'RGB'), ('.JPG', 'RGBA'), ('.webp', 'RGB')])
def test_webp_content_converts_without_office(tmp_path, monkeypatch, suffix, mode):
    source = tmp_path / ('Маренго_RR23' + suffix)
    Image.new(mode, (17, 23), (20, 80, 150, 100) if mode == 'RGBA' else (20, 80, 150)).save(source, 'WEBP')
    before = source.read_bytes()
    conv = converter(tmp_path)
    run = Mock(side_effect=AssertionError('WEBP не должен обращаться к LibreOffice'))
    monkeypatch.setattr('file_converter.subprocess.run', run)
    result = conv.convert_single_file(source)
    assert result.success, result.error
    pdf = source.with_suffix('.pdf')
    assert pdf.read_bytes().startswith(b'%PDF-')
    assert source.read_bytes() == before
    run.assert_not_called()


def test_webp_without_installed_office(tmp_path):
    source = tmp_path / 'Коричневый.jpg'
    Image.new('RGB', (10, 10), 'brown').save(source, 'WEBP')
    conv = converter(tmp_path, str(tmp_path / 'no-soffice'))
    assert conv.convert_single_file(source).success


def test_existing_pdf_is_not_overwritten(tmp_path, monkeypatch):
    source = tmp_path / 'Маренго.jpg'
    Image.new('RGB', (10, 10), 'blue').save(source, 'WEBP')
    pdf = source.with_suffix('.pdf')
    pdf.write_bytes(b'existing-pdf')
    monkeypatch.setattr('file_converter.subprocess.run', Mock(side_effect=AssertionError('Не вызывать LO')))
    assert converter(tmp_path).convert_single_file(source).success
    assert pdf.read_bytes() == b'existing-pdf'


@pytest.mark.parametrize('suffix', ['.jpg', '.png', '.docx'])
def test_other_formats_keep_office_route(tmp_path, monkeypatch, suffix):
    source = tmp_path / ('ordinary' + suffix)
    if suffix == '.docx':
        Document().save(source)
    else:
        Image.new('RGB', (10, 10), 'red').save(source)
    pdf = source.with_suffix('.pdf')
    def office(*args, **kwargs):
        pdf.write_bytes(b'%PDF-office')
        return Mock(stderr='')
    run = Mock(side_effect=office)
    monkeypatch.setattr('file_converter.subprocess.run', run)
    assert converter(tmp_path).convert_single_file(source).success
    run.assert_called_once()
    assert source.exists()


def test_corrupt_webp_reports_failure_and_keeps_source(tmp_path, monkeypatch):
    source = tmp_path / 'broken.jpg'
    source.write_bytes(b'RIFF\x00\x00\x00\x00WEBPbroken')
    monkeypatch.setattr('file_converter.subprocess.run', Mock(side_effect=RuntimeError('invalid input')))
    result = converter(tmp_path).convert_single_file(source)
    assert not result.success
    assert source.exists()
    assert not source.with_suffix('.pdf').exists()


@pytest.mark.parametrize('record_index', range(4))
def test_four_saved_mislabelled_webps(tmp_path, monkeypatch, record_index):
    import json
    review_path = next((root / 'codex_work/audit_runs/tegola_20261030/conversion_review.json'
                        for root in Path(__file__).resolve().parents
                        if (root / 'codex_work/audit_runs/tegola_20261030/conversion_review.json').is_file()), None)
    if review_path is None:
        pytest.skip('Сохраненный корпус четырех WEBP для аудита недоступен')
    review = json.loads(review_path.read_text())
    saved = Path(review[record_index]['remaining_files'][0]['file'])
    if not saved.is_file():
        pytest.skip('Сохраненный WEBP из корпуса аудита недоступен')
    source = tmp_path / saved.name
    source.write_bytes(saved.read_bytes())
    with Image.open(source) as image:
        assert image.format == 'WEBP'
        assert image.size == (652, 652)
    monkeypatch.setattr('file_converter.subprocess.run', Mock(side_effect=AssertionError('Не вызывать LO')))
    assert converter(tmp_path).convert_single_file(source).success
    assert source.with_suffix('.pdf').read_bytes().startswith(b'%PDF-')
    assert source.read_bytes() == saved.read_bytes()


def test_absent_pillow_keeps_office_conversion_available(tmp_path, monkeypatch):
    source = tmp_path / 'photo.jpg'
    Image.new('RGB', (10, 10), 'blue').save(source)
    pdf = source.with_suffix('.pdf')
    monkeypatch.setattr('file_converter.Image', None)
    def office(*args, **kwargs):
        pdf.write_bytes(b'%PDF-office')
        return Mock(stderr='')
    monkeypatch.setattr('file_converter.subprocess.run', office)
    assert converter(tmp_path).convert_single_file(source).success


def test_no_pillow_or_office_still_reports_missing_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr('file_converter.Image', None)
    with pytest.raises(FileNotFoundError, match='LibreOffice'):
        converter(tmp_path, str(tmp_path / 'no-soffice'))
