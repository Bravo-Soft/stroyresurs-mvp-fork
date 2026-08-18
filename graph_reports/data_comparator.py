# data_comparator.py 1.0.0
import re
import os
import logging
from typing import Dict, List, Any, Optional, Tuple
from enum import Enum
from datetime import datetime

log = logging.getLogger("data_comparator")

class ChangeType(Enum):
    """Типы изменений"""
    NEW_DATA = "Новые данные"
    DATA_NOT_FOUND = "Данные не найдены"
    EDITING = "Редактирование"
    SITE_NOT_WORKING = "Ошибка, сайт не работает"
    NO_CHANGES = "Нет изменений"
    NEW_COMPANY = "Новая компания"
    ALREADY_REPORTED = "Уже была в отчёте"

class DataComparator:
    """Сравнение данных для определения изменений"""
    
    def __init__(self):
        self.date_format = "%Y-%m-%d %H:%M:%S"
        self.date_only_format = "%Y-%m-%d"
    
    def _parse_date(self, date_str: Any) -> Optional[datetime]:
        """Парсинг даты из строки"""
        if not date_str:
            return None
        
        if isinstance(date_str, datetime):
            return date_str
        
        try:
            date_str = str(date_str).strip().strip('"').strip("'")
            
            for fmt in [self.date_format, "%Y-%m-%d", "%d.%m.%Y %H:%M:%S"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            
            return None
        except Exception as e:
            log.debug(f"Ошибка парсинга даты '{date_str}': {e}")
            return None
    
    def safe_str(self, value: Any) -> str:
        """Безопасное преобразование в строку"""
        if value is None:
            return ""
        if isinstance(value, list):
            if value:
                return str(value[0]) if isinstance(value[0], (str, int, float)) else str(value)
            return ""
        return str(value)
    
    def normalize_value(self, value: Any) -> str:
        """Нормализация значения для сравнения"""
        if value is None:
            return ""
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        if isinstance(value, dict):
            return "; ".join(f"{k}:{v}" for k, v in value.items() if not isinstance(v, (dict, list)))
        return str(value)
    
    def compare_strings(self, old_value: str, new_value: str) -> bool:
        """Сравнение строковых значений"""
        old_norm = self.normalize_value(old_value).strip()
        new_norm = self.normalize_value(new_value).strip()
        return old_norm == new_norm
    
    def _get_company_status(self, new_company: Dict[str, Any]) -> str:
        """
        Определение статуса компании на основе дат
        
        Логика:
        1. Только upload_date, нет report_date → "NEW_COMPANY"
        2. Есть upload_date и report_date, нет update_date → "ALREADY_REPORTED"
        3. Есть все три даты:
           - report_date < update_date → "NEEDS_COMPARISON"
           - report_date >= update_date → "ALREADY_REPORTED"
        """
        
        upload_date = self._parse_date(new_company.get("upload_date"))
        report_date = self._parse_date(new_company.get("report_date"))
        update_date = self._parse_date(new_company.get("update_date"))
        
        # Сценарий 1: Только upload_date, нет report_date
        if upload_date and not report_date:
            return "NEW_COMPANY"
        
        # Сценарий 2: Есть upload_date и report_date, но нет update_date
        if upload_date and report_date and not update_date:
            return "ALREADY_REPORTED"
        
        # Сценарий 3: Есть все три даты
        if upload_date and report_date and update_date:
            if report_date < update_date:
                return "NEEDS_COMPARISON"
            else:
                return "ALREADY_REPORTED"
        
        return "UNKNOWN"
    
    def compare_companies(self, old_company: Dict[str, Any], 
                         new_company: Dict[str, Any]) -> Tuple[ChangeType, Dict[str, Any], Dict[str, Any]]:
        """Сравнение данных компании с учетом новой логики статусов"""
        
        if not new_company:
            return ChangeType.DATA_NOT_FOUND, {}, {}
        
        # Определяем статус компании на основе дат
        status_code = self._get_company_status(new_company)
        
        if status_code == "NEW_COMPANY":
            return ChangeType.NEW_COMPANY, {}, new_company
        
        elif status_code == "ALREADY_REPORTED":
            return ChangeType.ALREADY_REPORTED, {}, new_company
        
        elif status_code == "NEEDS_COMPARISON":
            if not old_company:
                return ChangeType.EDITING, {}, new_company
            
            changes_old = {}
            changes_new = {}
            
            compare_fields = [
                "наименование компании",
                "реквизиты",
                "телефон",
                "описание",
                "адрес",
                "website",
                "E-mail"
            ]
            
            has_changes = False
            
            for field in compare_fields:
                old_val = old_company.get(field)
                new_val = new_company.get(field)
                
                if not self.compare_strings(old_val, new_val):
                    has_changes = True
                    changes_old[field] = old_val
                    changes_new[field] = new_val
            
            if has_changes:
                return ChangeType.EDITING, changes_old, changes_new
            else:
                return ChangeType.NO_CHANGES, {}, {}
        
        else:
            return ChangeType.NO_CHANGES, {}, {}
    
    def compare_suppliers(self, old_supplier: Dict[str, Any], 
                         new_supplier: Dict[str, Any]) -> Tuple[ChangeType, Dict[str, Any], Dict[str, Any]]:
        """Сравнение данных поставщика"""
        if not old_supplier and new_supplier:
            return ChangeType.NEW_DATA, {}, new_supplier
        
        if old_supplier and not new_supplier:
            return ChangeType.DATA_NOT_FOUND, old_supplier, {}
        
        if not old_supplier and not new_supplier:
            return ChangeType.NO_CHANGES, {}, {}
        
        changes_old = {}
        changes_new = {}
        
        compare_fields = [
            "наименование",
            "регион",
            "адрес",
            "телефон",
            "url",
            "url страницы"
        ]
        
        has_changes = False
        
        for field in compare_fields:
            old_val = old_supplier.get(field)
            new_val = new_supplier.get(field)
            
            if not self.compare_strings(old_val, new_val):
                has_changes = True
                changes_old[field] = old_val
                changes_new[field] = new_val
        
        if has_changes:
            return ChangeType.EDITING, changes_old, changes_new
        else:
            return ChangeType.NO_CHANGES, {}, {}
    
    def compare_files(self, old_file: Dict[str, Any], new_file: Dict[str, Any]) -> Tuple[ChangeType, Dict[str, Any], Dict[str, Any]]:
        """Сравнение данных файла по download_date"""
        if not old_file and new_file:
            return ChangeType.NEW_DATA, {}, new_file
        
        if old_file and not new_file:
            return ChangeType.DATA_NOT_FOUND, old_file, {}
        
        if not old_file and not new_file:
            return ChangeType.NO_CHANGES, {}, {}
        
        old_date = old_file.get("download_date")
        new_date = new_file.get("download_date")
        
        if old_date == new_date:
            return ChangeType.NO_CHANGES, {}, {}
        else:
            return ChangeType.EDITING, old_file, new_file
    
    def _analyze_files(self, old_files: List[Dict[str, Any]], 
                      new_files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Анализ изменений файлов (компании или продукта)"""
        analysis = {}
        
        old_files_dict = {f.get("id"): f for f in old_files}
        new_files_dict = {f.get("id"): f for f in new_files}
        
        all_file_ids = set(list(old_files_dict.keys()) + list(new_files_dict.keys()))
        
        for file_id in all_file_ids:
            old_file = old_files_dict.get(file_id)
            new_file = new_files_dict.get(file_id)
            
            if old_file and not new_file:
                status = ChangeType.DATA_NOT_FOUND
            elif not old_file and new_file:
                status = ChangeType.NEW_DATA
            else:
                status, _, _ = self.compare_files(old_file, new_file)
            
            analysis[file_id] = {
                "status": status,
                "old_file": old_file,
                "new_file": new_file
            }
        
        return analysis
    
    def analyze_company_changes(self, old_data: List[Dict[str, Any]], 
                               new_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Анализ изменений по компаниям с новой логикой статусов"""
        results = {}
        
        old_companies = {c.get("id"): c for c in old_data}
        new_companies = {c.get("id"): c for c in new_data}
        
        all_company_ids = set(list(old_companies.keys()) + list(new_companies.keys()))
        
        for company_id in all_company_ids:
            old_company = old_companies.get(company_id)
            new_company = new_companies.get(company_id)
            
            # Определяем статус компании с помощью новой логики
            status, changes_old, changes_new = self.compare_companies(old_company, new_company)
            
            # Ключевое изменение: передаем статус компании в анализ сущностей
            is_new_company = (status == ChangeType.NEW_COMPANY)
            
            # Анализируем поставщиков
            suppliers_analysis = self._analyze_suppliers_with_company_status(
                old_company.get("suppliers", []) if old_company else [],
                new_company.get("suppliers", []) if new_company else [],
                is_new_company
            )
            
            # Анализируем продукты
            products_analysis = self._analyze_products_with_company_status(
                old_company.get("products", []) if old_company else [],
                new_company.get("products", []) if new_company else [],
                is_new_company
            )
            
            # Анализируем файлы компании
            company_files_analysis = self._analyze_files_with_company_status(
                old_company.get("files", []) if old_company else [],
                new_company.get("files", []) if new_company else [],
                is_new_company
            )
            
            results[company_id] = {
                "status": status,
                "old_company": old_company,
                "new_company": new_company,
                "suppliers": suppliers_analysis,
                "products": products_analysis,
                "company_files": company_files_analysis
            }
        
        return results
    
    def _analyze_suppliers_with_company_status(self, old_suppliers: List[Dict[str, Any]], 
                                             new_suppliers: List[Dict[str, Any]],
                                             is_new_company: bool) -> Dict[str, Any]:
        """Анализ изменений поставщиков с учетом статуса компании"""
        analysis = {}
        
        old_suppliers_dict = {s.get("id"): s for s in old_suppliers}
        new_suppliers_dict = {s.get("id"): s for s in new_suppliers}
        
        all_supplier_ids = set(list(old_suppliers_dict.keys()) + list(new_suppliers_dict.keys()))
        
        for supplier_id in all_supplier_ids:
            old_supplier = old_suppliers_dict.get(supplier_id)
            new_supplier = new_suppliers_dict.get(supplier_id)
            
            if is_new_company:
                # Для новой компании ВСЕ поставщики = "Новые данные"
                status = ChangeType.NEW_DATA
            else:
                if old_supplier and not new_supplier:
                    status = ChangeType.DATA_NOT_FOUND
                elif not old_supplier and new_supplier:
                    status = ChangeType.NEW_DATA
                else:
                    status, _, _ = self.compare_suppliers(old_supplier, new_supplier)
            
            analysis[supplier_id] = {
                "status": status,
                "old_data": old_supplier,
                "new_data": new_supplier
            }
        
        return analysis
    
    def _analyze_products_with_company_status(self, old_products: List[Dict[str, Any]], 
                                            new_products: List[Dict[str, Any]],
                                            is_new_company: bool) -> Dict[str, Any]:
        """Анализ изменений продуктов с учетом статуса компании"""
        analysis = {}
        
        old_products_dict = {p.get("id"): p for p in old_products}
        new_products_dict = {p.get("id"): p for p in new_products}
        
        all_product_ids = set(list(old_products_dict.keys()) + list(new_products_dict.keys()))
        
        for product_id in all_product_ids:
            old_product = old_products_dict.get(product_id)
            new_product = new_products_dict.get(product_id)
            
            if is_new_company:
                # Для новой компании ВСЕ продукты = "Новые данные"
                status = ChangeType.NEW_DATA
            else:
                if old_product and not new_product:
                    status = ChangeType.DATA_NOT_FOUND
                elif not old_product and new_product:
                    status = ChangeType.NEW_DATA
                else:
                    old_name = self.safe_str(old_product.get("product_name", ""))
                    new_name = self.safe_str(new_product.get("product_name", ""))
                    
                    if old_name != new_name:
                        status = ChangeType.EDITING
                    else:
                        status = ChangeType.NO_CHANGES
            
            # Анализируем файлы продукта
            product_files_analysis = self._analyze_files_with_company_status(
                old_product.get("files", []) if old_product else [],
                new_product.get("files", []) if new_product else [],
                is_new_company
            )
            
            analysis[product_id] = {
                "status": status,
                "old_product": old_product,
                "new_product": new_product,
                "files": product_files_analysis
            }
        
        return analysis
    
    def _analyze_files_with_company_status(self, old_files: List[Dict[str, Any]], 
                                         new_files: List[Dict[str, Any]],
                                         is_new_company: bool) -> Dict[str, Any]:
        """Анализ изменений файлов с учетом статуса компании"""
        analysis = {}
        
        if is_new_company:
            # Для новой компании ВСЕ файлы = "Новые данные"
            for file_data in new_files:
                file_id = file_data.get("id")
                if file_id:
                    analysis[file_id] = {
                        "status": ChangeType.NEW_DATA,
                        "old_file": None,
                        "new_file": file_data
                    }
        else:
            old_files_dict = {f.get("id"): f for f in old_files}
            new_files_dict = {f.get("id"): f for f in new_files}
            
            all_file_ids = set(list(old_files_dict.keys()) + list(new_files_dict.keys()))
            
            for file_id in all_file_ids:
                old_file = old_files_dict.get(file_id)
                new_file = new_files_dict.get(file_id)
                
                if old_file and not new_file:
                    status = ChangeType.DATA_NOT_FOUND
                elif not old_file and new_file:
                    status = ChangeType.NEW_DATA
                else:
                    status, _, _ = self.compare_files(old_file, new_file)
                
                analysis[file_id] = {
                    "status": status,
                    "old_file": old_file,
                    "new_file": new_file
                }
        
        return analysis