"""Состав товара — в блоке «Технические характеристики» карточки (требование заказчика 2026-09-29).

Раньше промпт извлечения запрещал ключ «Состав» в specifications и отправлял его в description или
additional_info — а additional_info карточка не выводит вовсе, так что состав пропадал из карточки.
"""
import asyncio

from docx import Document

from ai_integration import AITunnelClient
from docx_generator import DOCXGenerator

_C = AITunnelClient(api_url="http://unused.local", api_key="unused")


def test_prompt_puts_composition_into_specifications():
    for system in (_C._formatter_system_prompt(), _C._single_system_prompt()):
        assert "Состав (ингредиенты) → description или additional_info" not in system
        assert "СОСТАВ ТОВАРА → specifications" in system
        # «состав комплекта» (поставляемые предметы) по-прежнему идёт в комплектацию
        assert "состав комплекта → complectation" in system
    # маршрутизация секций REASONER — в user-промпте (и однопромптового, и 2-вызовного пути)
    user = _C._single_user_prompt("ТЕКСТ", "Завод, ООО", "https://example.ru")
    assert "→ specifications (в spec_items: ключ как в источнике, иначе «Состав»; значение дословно)" in user


def test_card_shows_composition_in_specifications_table(tmp_path):
    gen = DOCXGenerator()

    async def _no_rtf(docx_path):  # в контейнере есть LibreOffice: без подмены DOCX был бы удалён после RTF
        return None
    gen._convert_to_rtf = _no_rtf

    composition = "Гипс, древесная стружка, остаточная влага"
    ai = {"product": {"product_name": "Плита гипсостружечная (Декор-1, ООО)",
                      "description": "Плита для облицовки стен и перегородок.",
                      "specifications": {"Плотность (кг/м^3)": "1250", "Состав": composition}}}
    path = asyncio.run(gen.generate_product_card({"product_id": "t1", "ai_raw_text": ai}, {}, str(tmp_path)))

    doc = Document(path)
    texts = [p.text for p in doc.paragraphs]
    assert "Технические характеристики" in texts
    assert not any(composition in t for t in texts)  # не в описании и не отдельным абзацем
    rows = [[c.text for c in r.cells] for r in doc.tables[0].rows]
    assert ["Состав", "Гипс; древесная стружка; остаточная влага"] in rows
