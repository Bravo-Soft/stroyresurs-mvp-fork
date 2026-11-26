"""Пустые строки RTF-карточки товара — по 5 пробелов (требование заказчика 2026-09-29).

Загрузчик заказчика схлопывает пустые строки карточки: пропадают и пустые абзацы-разделители между
блоками, и пустые строки внутри многострочного текстового блока («Инструкции по применению» и т. п.).
Строку из 5 пробелов он пустой не считает. Ячейки таблиц — не строки, их не трогаем.
"""
import asyncio

from docx import Document
from docx.oxml.ns import qn

from docx_generator import DOCXGenerator

FIVE = " " * 5

_PRODUCT = {
    "product_name": "Смесь штукатурная (Завод, ООО)",
    "tu": "ТУ 5745-001-12345678-2020",
    "description": "Смесь для выравнивания стен.",
    "specifications": {"Прочность, МПа": "10",
                       "Марка 1": {"Цвет": "белый", "Вес, кг": ""},
                       "Марка 2": {"Цвет": "серый", "Вес, кг": "25"}},
    "applications": "Внутренние работы.",
    "instructions_manuals": "Подготовить основание.\n\nНанести смесь.\n   \nВыровнять.",
}


def _card(tmp_path):
    gen = DOCXGenerator()

    async def _no_rtf(docx_path):  # в контейнере есть LibreOffice: без подмены DOCX был бы удалён после RTF
        return None
    gen._convert_to_rtf = _no_rtf

    data = {"product_id": "t1", "source_url": "https://example.ru/p/1", "ai_raw_text": {"product": _PRODUCT}}
    return Document(asyncio.run(gen.generate_product_card(data, {}, str(tmp_path))))


def test_every_empty_line_holds_five_spaces(tmp_path):
    doc = _card(tmp_path)
    # раскладка карточки прежняя (те же пустые строки на тех же местах) — меняется только их содержимое
    assert [p.text for p in doc.paragraphs] == [
        FIVE,                                  # пустая строка в начале карточки (перед наименованием)
        "Смесь штукатурная",
        "ТУ 5745-001-12345678-2020",
        FIVE,
        "Смесь для выравнивания стен.",
        FIVE,
        "Технические характеристики",
        FIVE,                                  # между заголовком и таблицей
        FIVE,                                  # между двумя таблицами характеристик
        FIVE,                                  # после раздела
        "Область применения",
        FIVE,
        "Внутренние работы.",
        FIVE,
        "Инструкции по применению",
        FIVE,
        f"Подготовить основание.\n{FIVE}\nНанести смесь.\n{FIVE}\nВыровнять.",  # пустые строки внутри блока
        FIVE,
        FIVE,                                  # вторая пустая строка перед URL
        "URL страницы: https://example.ru/p/1",
    ]


def test_spaces_marked_preserve_in_docx(tmp_path):
    # без xml:space="preserve" Word и LibreOffice отбрасывают пробелы на краях текста — строка снова пустая
    doc = _card(tmp_path)
    spaces = [t for t in doc.element.body.iter(qn("w:t")) if t.text == FIVE]
    assert len(spaces) == 13  # 11 пустых абзацев + 2 пустые строки внутри блока
    assert all(t.get(qn("xml:space")) == "preserve" for t in spaces)


def test_table_cells_untouched(tmp_path):
    doc = _card(tmp_path)
    assert ["Вес, кг", "", "25"] in [[c.text for c in r.cells] for r in doc.tables[1].rows]
