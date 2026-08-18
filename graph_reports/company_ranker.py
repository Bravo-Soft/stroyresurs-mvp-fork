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
        """
        Ранжирование компаний по правильному приоритету:
        
        1. Новые компании (без report_date) - ВЫСШИЙ ПРИОРИТЕТ
        1.1. С update_date (самые свежие)
        1.2. Без update_date, но с upload_date
        1.3. Без дат
        
        2. Компании, требующие сравнения (есть report_date и update_date, где report_date < update_date)
        2.1. С update_date (самые свежие)
        2.2. Без update_date, но с upload_date
        
        3. Компании, уже бывшие в отчете (есть report_date, но нет update_date ИЛИ report_date >= update_date)
        """
        if not companies:
            return []
        
        # Инициализируем группы в правильном порядке приоритета
        group1_companies = []  # Новые компании (без report_date) - ВЫСШИЙ ПРИОРИТЕТ
        group2_companies = []  # Требуют сравнения (report_date < update_date)
        group3_companies = []  # Уже были в отчете (report_date >= update_date или нет update_date)
        
        for company in companies:
            report_date = company.get("report_date")
            update_date = company.get("update_date")
            upload_date = company.get("upload_date")
            
            # Определяем статус компании
            has_report_date = bool(report_date and str(report_date).strip())
            has_update_date = bool(update_date and str(update_date).strip())
            
            if not has_report_date:
                # 1. Новая компания (без report_date) - ВЫСШИЙ ПРИОРИТЕТ
                sort_date = CompanyRanker.get_company_date_for_sorting(company)
                group1_companies.append((company, sort_date))
            elif has_report_date and has_update_date:
                # Проверяем, требует ли сравнения
                try:
                    report_dt = CompanyRanker._parse_date(str(report_date))
                    update_dt = CompanyRanker._parse_date(str(update_date))
                    
                    if report_dt and update_dt and report_dt < update_dt:
                        # 2. Требует сравнения
                        sort_date = update_dt
                        group2_companies.append((company, sort_date))
                    else:
                        # 3. Уже была в отчете
                        sort_date = CompanyRanker.get_company_date_for_sorting(company)
                        group3_companies.append((company, sort_date))
                except:
                    # В случае ошибки парсинга
                    sort_date = CompanyRanker.get_company_date_for_sorting(company)
                    group3_companies.append((company, sort_date))
            else:
                # 3. Уже была в отчете (есть report_date, но нет update_date)
                sort_date = CompanyRanker.get_company_date_for_sorting(company)
                group3_companies.append((company, sort_date))
        
        # Сортируем группы по дате (самые свежие первые)
        group1_companies.sort(key=lambda x: x[1] if x[1] else datetime.min, reverse=True)
        group2_companies.sort(key=lambda x: x[1] if x[1] else datetime.min, reverse=True)
        group3_companies.sort(key=lambda x: x[1] if x[1] else datetime.min, reverse=True)
        
        # Формируем итоговый список в правильном порядке приоритета
        result_companies = []
        
        # 1. Сначала новые компании (самые свежие без report_date)
        for company, _ in group1_companies:
            result_companies.append(company)
        
        # 2. Затем компании, требующие сравнения
        for company, _ in group2_companies:
            result_companies.append(company)
        
        # 3. В конце компании, уже бывшие в отчете
        for company, _ in group3_companies:
            result_companies.append(company)
        
        # Применяем лимит
        if limit > 0:
            result_companies = result_companies[:limit]
        
        log.info(f"Ранжировано компаний: {len(result_companies)} из {len(companies)} (лимит: {limit})")
        log.info(f"Группы: новые={len(group1_companies)}, требуют сравнения={len(group2_companies)}, уже в отчете={len(group3_companies)}")
        
        return result_companies
    
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