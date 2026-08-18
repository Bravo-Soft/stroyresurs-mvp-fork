# folder_renamer.py 1.0.0
import os
"""
Единый модуль переименования папок для согласованности между отчетами и архивом
"""

class FolderRenamer:
    """Класс для переименования папок согласно business logic"""
    
    # Словарь переименования папок в корне компании
    ROOT_FOLDER_TRANSLATIONS = {
        'Products': 'Товары',
        'Certificates': 'Сертификаты',
        'Documents': 'Документация',
        'Instructions': 'Инструкции',
        'Price_lists': 'Прайс-листы',
        'CompanyFiles': 'Файлы компании',
    }
    
    # Перевод для папок внутри Products/Товары
    PRODUCT_SUBFOLDER_TRANSLATIONS = {
        'Documents': 'Файлы товара',
        'Images': 'Изображения товара'
    }
    
    @staticmethod
    def rename_path(path: str) -> str:
        """
        Переименование всех папок в пути
        """
        if not path or not isinstance(path, str):
            return path
        
        # Универсальная нормализация разделителей
        path = path.replace("\\", "/")
        
        # Разбиваем путь на части
        parts = path.split("/")
        renamed_parts = []
        
        # Определяем контекст для каждой части
        for i, part in enumerate(parts):
            # Пропускаем пустые части (могут появиться из-за двойных слешей)
            if not part:
                continue
                
            # Проверяем, находимся ли мы внутри Products/Товары
            is_in_products = False
            for prev_part in parts[:i]:
                if prev_part in ['Products', 'Товары']:
                    is_in_products = True
                    break
            
            # Переименовываем папку
            renamed_part = part
            
            if is_in_products and part in FolderRenamer.PRODUCT_SUBFOLDER_TRANSLATIONS:
                renamed_part = FolderRenamer.PRODUCT_SUBFOLDER_TRANSLATIONS[part]
            elif part in FolderRenamer.ROOT_FOLDER_TRANSLATIONS:
                renamed_part = FolderRenamer.ROOT_FOLDER_TRANSLATIONS[part]
            
            renamed_parts.append(renamed_part)
        
        # Возвращаем путь с разделителями в зависимости от ОС
        result = "/".join(renamed_parts)
        if os.name == 'nt':  # Windows
            return result.replace("/", "\\")
        else:  # Linux/Mac
            return result
    
    @staticmethod
    def extract_company_name_from_folder(folder_name: str) -> str:
        """
        Извлечение названия компании из имени папки (удаление ID)
        """
        if not folder_name:
            return ""
        
        # Удаляем ID до первого подчеркивания
        if '_' in folder_name:
            parts = folder_name.split('_', 1)
            if len(parts) > 1:
                return parts[1]
        
        return folder_name