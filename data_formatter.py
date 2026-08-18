# data_formatter.py 1.0.0
import logging
from typing import Dict, Any, Union, List

log = logging.getLogger("data_formatter")

class DataFormatter:
    """Утилиты для форматирования сложных структур данных"""
    
    @staticmethod
    def format_nested_dict(data: Dict[str, Any], indent_level: int = 0, 
                          max_level: int = 5) -> str:
        """
        Рекурсивное форматирование вложенного словаря в читаемый текст
        с отступами табуляцией
        """
        if not data or indent_level >= max_level:
            return ""
        
        result_lines = []
        indent = "\t" * indent_level
        
        for key, value in data.items():
            if value is None:
                continue
            
            if isinstance(value, dict):
                # Вложенный словарь
                result_lines.append(f"{indent}{key}:")
                nested_text = DataFormatter.format_nested_dict(value, indent_level + 1, max_level)
                if nested_text:
                    result_lines.append(nested_text)
            
            elif isinstance(value, list):
                # Список
                result_lines.append(f"{indent}{key}:")
                list_indent = "\t" * (indent_level + 1)
                for item in value:
                    if item is not None:
                        result_lines.append(f"{list_indent}• {item}")
            
            elif isinstance(value, str) and DataFormatter._is_text_description(value):
                # Текстовое описание
                result_lines.append(f"{indent}{key}:")
                # Убираем лишние пробелы в тексте
                cleaned_text = ' '.join(value.split())
                result_lines.append(f"{indent}\t{cleaned_text}")
            
            else:
                # Простое значение
                result_lines.append(f"{indent}{key}: {value}")
        
        return "\n".join(result_lines)
    
    @staticmethod
    def _is_text_description(text: str) -> bool:
        """Определяет, является ли текст описанием (предложением)"""
        text = text.strip()
        if len(text) < 20:
            return False
        
        # Признаки предложения
        sentence_indicators = [
            ' и ', ' что ', ' который ', ' где ', ' когда ',
            ' потому что ', ' если ', ' хотя ', ' однако ',
            ' также ', ' кроме ', ' поэтому '
        ]
        
        for indicator in sentence_indicators:
            if indicator in text.lower():
                return True
        
        # Заканчивается точкой и содержит пробелы
        if text.endswith(('.', '!', '?')) and ' ' in text:
            return True
        
        return False
    
    @staticmethod
    def flatten_specifications(specs: Dict[str, Any]) -> Dict[str, str]:
        """
        Преобразует вложенные спецификации в плоский словарь,
        где значения преобразованы в строки
        """
        flat_specs = {}
        
        def process_dict(data: Dict[str, Any], prefix: str = ""):
            for key, value in data.items():
                full_key = f"{prefix}{key}" if prefix else key
                
                if isinstance(value, dict):
                    process_dict(value, f"{full_key} > ")
                elif isinstance(value, list):
                    flat_specs[full_key] = "; ".join(str(item) for item in value if item is not None)
                elif value is not None:
                    flat_specs[full_key] = str(value)
        
        if isinstance(specs, dict):
            process_dict(specs)
        
        return flat_specs