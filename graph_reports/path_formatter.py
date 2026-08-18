# path_formatter 1.1.0
import re
import os
import sys
from typing import Dict, Any
import logging

from folder_renamer import FolderRenamer

# product_utils лежит в корне mvp — добавляем его в sys.path для standalone-запуска graph_reports
_MVP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MVP_ROOT not in sys.path:
    sys.path.insert(0, _MVP_ROOT)
from product_utils import sanitize_filename, sanitize_company_name

log = logging.getLogger("path_formatter")


class PathFormatter:
    """Форматирование путей к файлам с учетом переименования папок"""

    # Подпапки корня компании, которые архиватор складывает внутрь «Файлы производителя»
    MANUFACTURER_DIRS = {
        'Certificates', 'Documents', 'Instructions', 'Price_lists', 'CompanyFiles',
        'Сертификаты', 'Документация', 'Инструкции', 'Прайс-листы', 'Файлы компании',
    }

    def __init__(self):
        self.folder_renamer = FolderRenamer()

    def normalize_company_name(self, company_name: Any) -> str:
        """Нормализация имени компании — тот же санитайзер, которым пайплайн именует папки компаний"""
        if not company_name:
            return ""

        if isinstance(company_name, list):
            name = str(company_name[0]) if company_name else ""
        elif isinstance(company_name, str):
            name = company_name
        else:
            name = str(company_name)

        return sanitize_company_name(name.strip())

    def format_company_folder_path(self, company_id: str, company_name: str) -> str:
        """Корень компании в архиве: <ID>_<имя> — формула совпадает с папкой компании в Documents"""
        normalized_name = self.normalize_company_name(company_name)
        company_id_str = str(company_id).strip() if company_id is not None else ""
        # pandas/json могут отдать числовой ID как '123.0' — приводим к целому виду
        if company_id_str.endswith('.0') and company_id_str[:-2].isdigit():
            company_id_str = company_id_str[:-2]
        if company_id_str and company_id_str.lower() not in ('none', 'nan'):
            return f"{company_id_str}_{normalized_name}"
        return normalized_name

    def format_product_folder_path(self, company_folder: str, product: Dict[str, Any]) -> str:
        """Формирование пути к папке продукта"""
        product_name = product.get("product_name", "")
        product_id = product.get("id", "")

        if not product_id:
            return ""

        # Папку товара в Documents создаёт sanitize_filename — используем его же
        normalized_product_name = sanitize_filename(product_name)
        product_folder_name = normalized_product_name

        # Собираем полный путь
        return f"{company_folder}/Товары/{product_folder_name}"

    def format_product_files_folder_path(self, company_folder: str, product: Dict[str, Any]) -> str:
        """Формирование пути к папке файлов продукта"""
        product_folder = self.format_product_folder_path(company_folder, product)
        return f"{product_folder}/Файлы товара"

    def extract_filename_from_path(self, path: str) -> str:
        if not path:
            return ""

        # Универсальная обработка путей для Windows и Linux
        path = path.replace("\\", "/")
        filename = os.path.basename(path)
        return filename

    def extract_folder_path_without_filename(self, file_path: str) -> str:
        if not file_path:
            return ""

        # Универсальная обработка путей для Windows и Linux
        file_path = file_path.replace("\\", "/")
        
        # Отрезаем имя файла
        folder = os.path.dirname(file_path)
        return folder

    def get_product_card_path(self, company_folder: str, product: Dict[str, Any]) -> str:
        """Формирование пути к карточке товара с учетом переименования"""
        product_folder = product.get("product_folder", "")

        if not product_folder:
            product_folder = self.format_product_folder_path(company_folder, product)

        # Универсальная нормализация разделителей
        product_folder = product_folder.replace("\\", "/")

        renamed_path = self.folder_renamer.rename_path(product_folder)
        return renamed_path.rstrip("/")

    def get_product_files_path(self, company_folder: str, product: Dict[str, Any]) -> str:
        """Формирование пути к файлам товара с учетом переименования"""
        product_folder = self.format_product_files_folder_path(company_folder, product)
        
        # Универсальная нормализация разделителей
        product_folder = product_folder.replace("\\", "/")

        renamed_path = self.folder_renamer.rename_path(product_folder)
        return renamed_path.rstrip("/")
        
    def get_full_archive_path(self, company_folder: str, relative_path: str) -> str:
        """
        Формирование полного пути в архиве (кроссплатформенно)
        """
        if not relative_path:
            return company_folder

        # Универсальная нормализация разделителей
        relative_path = relative_path.replace("\\", "/")
        company_folder = company_folder.replace("\\", "/")

        # Если путь уже без company_id — просто приклеиваем
        if "/" not in relative_path:
            # Создаем путь с универсальными разделителями
            full_path = f"{company_folder}/{relative_path}"
            # Берем только директорию (без имени файла)
            folder_only = os.path.dirname(full_path) if "." in os.path.basename(full_path) else full_path
        else:
            # Убираем company_id, если он есть
            parts = relative_path.split("/", 1)
            if len(parts) == 2:
                rest = parts[1]
            else:
                rest = relative_path

            # Файлы из корневых подпапок компании архиватор кладёт внутрь «Файлы производителя»
            first_segment = rest.split("/", 1)[0]
            if first_segment in self.MANUFACTURER_DIRS:
                rest = f"Файлы производителя/{rest}"

            full_path = f"{company_folder}/{rest}"
            
            # Определяем, является ли последняя часть именем файла (есть расширение)
            # или это папка (для товаров и их файловых папок)
            if "." in os.path.basename(full_path):
                # Это файл - берем директорию
                folder_only = os.path.dirname(full_path)
            else:
                # Это папка - оставляем как есть
                folder_only = full_path

        # Еще раз нормализуем разделители для переименователя
        folder_only = folder_only.replace("\\", "/")
        
        # Применяем переименование папок
        renamed_path = self.folder_renamer.rename_path(folder_only)
        
        # Возвращаем путь с правильными разделителями для системы
        if os.name == 'nt':  # Windows
            return renamed_path.replace("/", "\\").rstrip("\\")
        else:  # Linux/Mac
            return renamed_path.rstrip("/")
