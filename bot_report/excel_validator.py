# excel_validator.py
import pandas as pd
from typing import Dict, List, Tuple, Optional
import logging
from pathlib import Path

log = logging.getLogger("excel_validator")

class ExcelValidator:
    """Валидатор Excel файлов для добавления компаний"""
    
    # Поддерживаемые варианты названий колонок
    COLUMN_MAPPING = {
        'ID производителя': ['ID производителя', 'ID производителя', 'id', 'ID'],
        'Наименование': ['Наименование', 'Название', 'Наименование компании', 'Company Name'],
        'Website': ['Website', 'Website', 'Сайт', 'URL', 'website', 'url']
    }
    
    REQUIRED_COLUMNS = ['ID производителя', 'Наименование', 'Website']
    OPTIONAL_COLUMNS = ['Адрес', 'Телефон, ограничение 4 шт', 'E-mail', 
                       'Регион производства', 'Информация']
    
    def validate_file(self, file_path: Path) -> Tuple[bool, str, Optional[pd.DataFrame]]:
        """
        Валидация Excel файла с данными компаний
        
        Returns:
            Tuple[bool, str, Optional[DataFrame]]: 
            (успех, сообщение об ошибке/успехе, DataFrame с данными)
        """
        try:
            # Проверка существования файла
            if not file_path.exists():
                return False, "Файл не найден", None
            
            # Проверка размера файла
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            if file_size_mb > 10:  # 10 MB лимит
                return False, f"Файл слишком большой ({file_size_mb:.1f} MB). Максимальный размер: 10 MB", None
            
            # Чтение Excel файла
            try:
                df = pd.read_excel(file_path)
            except Exception as e:
                return False, f"Ошибка чтения Excel файла: {str(e)}", None
            
            # Проверка и нормализация названий колонок
            df_standard = df.copy()
            renamed_columns = {}
            
            # Проверяем наличие обязательных колонок (в любом из вариантов названий)
            for required_col, possible_names in self.COLUMN_MAPPING.items():
                found = False
                for possible_name in possible_names:
                    if possible_name in df_standard.columns:
                        renamed_columns[possible_name] = required_col
                        found = True
                        break
                
                if not found:
                    return False, f"Отсутствует обязательная колонка: {required_col}. Возможные названия: {', '.join(possible_names)}", None
            
            # Переименовываем колонки в стандартные названия
            if renamed_columns:
                df_standard = df_standard.rename(columns=renamed_columns)
            
            # Проверка пустых строк в обязательных колонках
            required_data = df_standard[self.REQUIRED_COLUMNS]
            empty_rows = required_data.isna().any(axis=1)
            
            if empty_rows.any():
                empty_indices = df_standard[empty_rows].index.tolist()
                return False, f"Пустые значения в обязательных колонках в строках: {', '.join(map(str, empty_indices))}", None
            
            # Проверка уникальности ID
            id_col = 'ID производителя'
            duplicate_ids = df_standard[df_standard.duplicated(subset=[id_col], keep=False)]
            
            if not duplicate_ids.empty:
                dup_ids = duplicate_ids[id_col].unique().tolist()
                return False, f"Найдены дубликаты ID: {', '.join(map(str, dup_ids))}", None
            
            # Проверка формата URL
            url_errors = []
            for idx, row in df_standard.iterrows():
                url = str(row['Website']).strip()
                if url and not (url.startswith('http://') or url.startswith('https://')):
                    url_errors.append(f"Строка {idx}: URL должен начинаться с http:// или https://")
            
            if url_errors:
                return False, "\n".join(url_errors[:5]), None  # Ограничиваем количество ошибок
            
            # Очистка данных
            df_clean = df_standard.copy()
            
            # Преобразование ID в строки
            df_clean['ID производителя'] = df_clean['ID производителя'].astype(str).str.strip()
            df_clean['Наименование'] = df_clean['Наименование'].astype(str).str.strip()
            df_clean['Website'] = df_clean['Website'].astype(str).str.strip()
            
            # Обработка опциональных колонок
            for col in self.OPTIONAL_COLUMNS:
                if col in df_clean.columns:
                    df_clean[col] = df_clean[col].fillna('').astype(str).str.strip()
            
            log.info(f"Файл успешно валидирован: {len(df_clean)} компаний")
            return True, f"Файл успешно валидирован. Найдено {len(df_clean)} компаний", df_clean
            
        except Exception as e:
            log.error(f"Ошибка валидации файла: {e}")
            return False, f"Внутренняя ошибка при валидации: {str(e)}", None
    
    def extract_company_data(self, df: pd.DataFrame) -> List[Dict]:
        """Извлечение данных компаний из DataFrame"""
        companies = []
        
        for _, row in df.iterrows():
            company = {
                'ID производителя': str(row['ID производителя']),
                'Наименование': str(row['Наименование']),
                'Website': str(row['Website'])
            }
            
            # Добавляем опциональные поля если они есть
            for col in self.OPTIONAL_COLUMNS:
                if col in row:
                    company[col] = str(row[col]) if pd.notna(row[col]) else ''
            
            companies.append(company)
        
        return companies