"""Выгрузка URL корпуса прошлого прогона в TSV для офлайн-реплея категоризатора.

Читает метаданные сохранённых страниц (mvp_directories/Base/temp_html_storage/
<Компания>/{Product_pages,Company_pages,Distributor_pages,Other_pages}/*.json) и
пишет строки «компания<TAB>папка<TAB>категория<TAB>url». Корпус read-only.

Запуск: python tests/tools/dump_corpus_urls.py <корень корпуса> <выходной TSV>
"""
import json
import os
import sys

FOLDERS = ('Product_pages', 'Company_pages', 'Distributor_pages', 'Other_pages')


def main() -> int:
    root = sys.argv[1]
    out_path = sys.argv[2]
    total = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        for company in sorted(os.listdir(root)):
            company_dir = os.path.join(root, company)
            if not os.path.isdir(company_dir):
                continue
            for folder in FOLDERS:
                folder_dir = os.path.join(company_dir, folder)
                if not os.path.isdir(folder_dir):
                    continue
                for name in sorted(os.listdir(folder_dir)):
                    if not name.endswith('.json'):
                        continue
                    try:
                        with open(os.path.join(folder_dir, name), 'r', encoding='utf-8') as fh:
                            meta = json.load(fh)
                    except Exception:
                        continue
                    url = (meta.get('url') or '').strip()
                    if not url:
                        continue
                    category = meta.get('category') or ''
                    out.write(f"{company}\t{folder}\t{category}\t{url}\n")
                    total += 1
    print(f"URL выгружено: {total}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
