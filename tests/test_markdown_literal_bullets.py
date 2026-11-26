import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from site_profiles.markdown_validation import check_markdown

HTML='<main><div>Описание.<br>- Фасад: регулировка 3D;<br>- Высота 36 см.</div></main>'
MD='Описание.\n- Фасад: регулировка 3D;\n- Высота 36 см.'

def test_literal_bullets_preserve_complete_paragraph():
 assert check_markdown(HTML,MD,source_selectors=['main'])['status']=='pass'

def test_changed_number_still_fails():
 assert check_markdown(HTML,MD.replace('36','35'),source_selectors=['main'])['status']=='fail'

def test_missing_line_still_fails():
 assert check_markdown(HTML,'Описание.\n- Высота 36 см.',source_selectors=['main'])['status']=='fail'

def test_list_interface_leak_still_fails():
 html='<main><p>Высота 36 см</p><button>Купить</button></main>'
 assert check_markdown(html,'Высота 36 см\n- Купить',source_selectors=['main'])['status']=='fail'


def test_invisible_separators_are_not_lost_facts():
 html='<main><p>Корпус оставлен \u200b\u200bв оцинкованном исполнении. Высота 598 мм.</p></main>'
 assert check_markdown(html,'Корпус оставлен в оцинкованном исполнении. Высота 598 мм.',source_selectors=['main'])['status']=='pass'
 assert check_markdown(html,'Корпус оставлен в оцинкованном исполнении. Высота 599 мм.',source_selectors=['main'])['status']=='fail'
