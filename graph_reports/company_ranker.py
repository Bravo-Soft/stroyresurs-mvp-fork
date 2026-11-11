# company_ranker.py 1.0.0
"""
Модуль для ранжирования компаний по датам
"""
import logging
import os
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

log = logging.getLogger("company_ranker")

class CompanyRanker:
    """Класс для ранжирования компаний по датам"""
    
    def __init__(self):
        self.date_format = "%Y-%m-%d %H:%M:%S"
        self.date_only_format = "%Y-%m-%d"
    
    @staticmethod
    def _parse_date(date_str: Any) -> Optional[datetime]:
        """Парсинг даты из строки (статическая версия)"""
        if not date_str:
            return None
        
        if isinstance(date_str, datetime):
            return date_str
        
        try:
            date_str = str(date_str).strip().strip('"').strip("'")
            
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            
            return None
        except Exception as e:
            log.debug(f"Ошибка парсинга даты '{date_str}': {e}")
            return None
    
    @staticmethod
    def get_company_date_for_sorting(company: Dict[str, Any]) -> Optional[datetime]:
        """
        Получение даты компании для сортировки
        
        Приоритет:
        1. update_date (если есть)
        2. upload_date (если нет update_date)
        
        Returns:
            datetime или None если даты нет
        """
        try:
            # Сначала пробуем получить update_date
            update_date = company.get("update_date")
            if update_date:
                if isinstance(update_date, datetime):
                    return update_date
                elif isinstance(update_date, str):
                    # Убираем лишние пробелы и кавычки
                    date_str = str(update_date).strip().strip('"').strip("'")
                    # Пробуем разные форматы
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S"]:
                        try:
                            return datetime.strptime(date_str, fmt)
                        except ValueError:
                            continue
            
            # Если update_date нет или невалидна, пробуем upload_date
            upload_date = company.get("upload_date")
            if upload_date:
                if isinstance(upload_date, datetime):
                    return upload_date
                elif isinstance(upload_date, str):
                    date_str = str(upload_date).strip().strip('"').strip("'")
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S"]:
                        try:
                            return datetime.strptime(date_str, fmt)
                        except ValueError:
                            continue
            
            return None
            
        except Exception as e:
            log.error(f"Ошибка получения даты компании: {e}")
            return None
    
    @staticmethod
    def get_company_date_status(company: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
        """
        Получение даты и статуса компании
        
        Returns:
            Tuple[дата для сортировки, тип даты]
        """
        update_date = company.get("update_date")
        upload_date = company.get("upload_date")
        
        if update_date:
            return CompanyRanker.get_company_date_for_sorting(company), "update_date"
        elif upload_date:
            return CompanyRanker.get_company_date_for_sorting(company), "upload_date"
        else:
            return None, "no_date"
    
    @staticmethod
    def rank_companies(companies: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """Общая очередь: первый отчёт по загрузке, повторный — по обновлению."""
        new, updated = [], []
        for company in companies:
            if company.get("data_update_in_progress") in (True, "true", "True"):
                continue
            report = company.get("report_date")
            upload = CompanyRanker._parse_date(company.get("upload_date"))
            update = CompanyRanker._parse_date(company.get("update_date"))
            if not report or not str(report).strip():
                new.append((company, upload))
            elif company.get("report_version") and company.get("data_version"):
                if company["report_version"] != company["data_version"]:
                    updated.append((company, update))
            else:
                report_dt = CompanyRanker._parse_date(report)
                if report_dt and update and update > report_dt:
                    updated.append((company, update))
        key = lambda item: (item[1] is None, item[1] or datetime.max, str(item[0].get("id", "")))
        new.sort(key=key)
        updated.sort(key=key)
        result = [company for company, _ in new + updated]
        return result[:limit] if limit > 0 else result

    @staticmethod
    def get_company_status_for_report(company: Dict[str, Any]) -> str:
        """
        Определение статуса компании для отчета
        
        Returns:
            Статус компании
        """
        upload_date = company.get("upload_date")
        report_date = company.get("report_date")
        update_date = company.get("update_date")
        
        if not report_date:
            # Нет report_date - новая компания
            return "new_company"
        elif report_date and not update_date:
            # Есть report_date, но нет update_date - уже была в отчете
            return "already_reported"
        elif report_date and update_date:
            # Есть обе даты - нужно проверить
            return "needs_analysis"
        else:
            # Компания без дат (не должно быть)
            return "unknown"
    
    def determine_company_status(self, company: Dict[str, Any]) -> str:
        """
        Определение статуса компании по новой логике
        
        Returns:
            Статус компании
        """
        upload_date = self._parse_date(company.get("upload_date"))
        report_date = self._parse_date(company.get("report_date"))
        update_date = self._parse_date(company.get("update_date"))
        
        # Сценарий 1: Только upload_date, нет report_date
        if upload_date and not report_date:
            return "Новая компания"
        
        # Сценарий 2: Есть upload_date и report_date, но нет update_date
        if upload_date and report_date and not update_date:
            return "Уже была в отчёте"
        
        # Сценарий 3: Есть все три даты
        if upload_date and report_date and update_date:
            if report_date < update_date:
                # Нужно проверить изменения
                return "Требуется сравнение"
            else:
                # report_date >= update_date
                return "Уже была в отчёте"
        
        # Неизвестный статус
        return "Неизвестно"