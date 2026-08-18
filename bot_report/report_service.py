# report_service.py
import asyncio
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

from graph_reports.graph_db_client import GraphDBClient
from graph_reports.data_comparator import DataComparator
from graph_reports.report_generators import GeneralReportGenerator, DetailedReportGenerator
from graph_reports.excel_writer import ExcelWriter
from graph_reports.path_formatter import PathFormatter
from graph_reports.company_ranker import CompanyRanker
from graph_reports.archive_builder import ReportArchiveBuilder
from bot_report.monitoring_integrator import MonitoringIntegrator

log = logging.getLogger("report_service")

class ReportService:
    """Сервис для формирования отчетов и архивов"""
    
    def __init__(self, config, api_client):
        self.config = config
        self.api_client = api_client
        self.path_formatter = PathFormatter()
        
        # Настройка конфигурации для отчетов
        from graph_reports.config import ReportConfig
        self.report_config = ReportConfig()
        self.report_config.output_dir = str(config.reports_dir)
        self.report_config.graph_db_api_url = config.graph_db_api_url
        self.report_config.graph_db_request_timeout = config.graph_db_request_timeout

        # Инициализируем интегратор
        self.monitoring_integrator = MonitoringIntegrator(config)

    async def get_companies_from_graph_db(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Получение компаний из Graph DB с сортировкой по дате
        
        Args:
            limit: Ограничение количества компаний
            
        Returns:
            List[Dict]: Список компаний
        """
        try:
            async with GraphDBClient(self.report_config) as client:
                # Получаем все компании
                companies = await client.get_all_companies_data()
                
                # Сортируем по дате в правильном порядке
                sorted_companies = CompanyRanker.rank_companies(companies, len(companies))
                
                # Применяем лимит если задан
                if limit and limit > 0:
                    sorted_companies = sorted_companies[:limit]
                
                log.info(f"Получено {len(sorted_companies)} компаний из Graph DB")
                return sorted_companies
                
        except Exception as e:
            log.error(f"Ошибка получения компаний из Graph DB: {e}")
            raise
    
    def _sort_companies_by_date(self, companies: List[Dict]) -> List[Dict]:
        """Сортировка компаний по дате в правильном порядке"""
        # Используем CompanyRanker для правильной сортировки
        return CompanyRanker.rank_companies(companies, len(companies))
    
    async def get_ranked_companies(self, limit: int) -> List[Dict]:
        """
        Получение ранжированных компаний в правильном порядке:
        1. Компании только с upload_date (без report_date) - самые старые
        2. Компании с report_date
        3. Компании без дат
        
        Args:
            limit: Количество компаний
            
        Returns:
            List[Dict]: Список ранжированных компаний
        """
        try:
            # Получаем все компании из Graph DB
            async with GraphDBClient(self.report_config) as client:
                companies = await client.get_all_companies_data()
                
                # Ранжируем компании в правильном порядке
                ranked_companies = CompanyRanker.rank_companies(companies, limit)
                
                log.info(f"Ранжировано {len(ranked_companies)} компаний (запрошено: {limit})")
                
                # Логируем распределение по группам для отладки
                if ranked_companies:
                    with_upload_only = len([c for c in ranked_companies if c.get("upload_date") and not c.get("report_date")])
                    with_report = len([c for c in ranked_companies if c.get("report_date")])
                    without_dates = len([c for c in ranked_companies if not c.get("upload_date") and not c.get("report_date")])
                    
                    log.info(f"Распределение в отчете: upload_only={with_upload_only}, report_date={with_report}, no_dates={without_dates}")
                
                return ranked_companies
                
        except Exception as e:
            log.error(f"Ошибка получения ранжированных компаний: {e}")
            raise
    
    async def generate_reports_for_companies(self, company_ids: List[str]) -> Tuple[bool, str]:
        """
        Генерация отчетов для указанных компаний
        
        Args:
            company_ids: Список ID компаний
            
        Returns:
            Tuple[bool, str]: (успех, сообщение)
        """
        try:
            log.info(f"Генерация отчетов для {len(company_ids)} компаний")
            
            # Получаем данные компаний из Graph DB
            async with GraphDBClient(self.report_config) as client:
                # Получаем все компании
                all_companies = await client.get_all_companies_data()
                
                # Фильтруем по указанным ID
                filtered_companies = [c for c in all_companies if c.get("id") in company_ids]
                
                if not filtered_companies:
                    return False, "Не найдено компаний с указанными ID"
                
                # Получаем старые данные (на вчерашнюю дату для сравнения)
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
                old_data = await client.get_companies_data_on_date(yesterday)
                
                # Анализируем изменения
                data_comparator = DataComparator()
                changes_analysis = data_comparator.analyze_company_changes(old_data, filtered_companies)
                
                # Генерируем отчеты
                general_generator = GeneralReportGenerator(self.path_formatter)
                detailed_generator = DetailedReportGenerator(self.path_formatter)
                
                general_report_data = general_generator.generate_report(
                    filtered_companies, changes_analysis, yesterday
                )
                
                detailed_report_data = detailed_generator.generate_report(
                    filtered_companies, changes_analysis, yesterday
                )
                
                # Сохраняем отчеты в Excel
                excel_writer = ExcelWriter(self.report_config)
                
                general_report_path = excel_writer.write_general_report(
                    general_report_data,
                    output_path=self.config.reports_dir / self.config.general_report_name
                )
                
                detailed_report_path = excel_writer.write_detailed_report(
                    detailed_report_data,
                    output_path=self.config.reports_dir / self.config.detailed_report_name
                )
                
                log.info(f"Отчеты сгенерированы: {general_report_path}, {detailed_report_path}")
                return True, "Отчеты успешно сгенерированы"
                
        except Exception as e:
            log.error(f"Ошибка генерации отчетов: {e}")
            return False, f"Ошибка генерации отчетов: {str(e)}"
    
    async def generate_ranked_reports(self, limit: int) -> Tuple[bool, str, List[str]]:
        """
        Генерация отчетов для ранжированных компаний
        
        Args:
            limit: Количество компаний для отчета
            
        Returns:
            Tuple[bool, str, List[str]]: (успех, сообщение, список ID компаний)
        """
        try:
            log.info(f"Генерация отчетов для {limit} ранжированных компаний")
            
            # Получаем ранжированные компании
            ranked_companies = await self.get_ranked_companies(limit)
            
            if not ranked_companies:
                return False, "Не удалось получить ранжированные компании", []
            
            # Получаем ID компаний
            company_ids = [c.get("id") for c in ranked_companies if c.get("id")]
            
            # Генерируем отчеты для этих компаний
            success, message = await self.generate_reports_for_companies(company_ids)
            
            if success:
                log.info(f"Отчеты для {len(company_ids)} ранжированных компаний сгенерированы")
                return True, f"Отчеты для {len(company_ids)} компаний успешно сгенерированы", company_ids
            else:
                return False, message, []
                
        except Exception as e:
            log.error(f"Ошибка генерации ранжированных отчетов: {e}")
            return False, f"Ошибка генерации ранжированных отчетов: {str(e)}", []
    
    async def create_archive_for_user(self, user_id: str, date_str: str) -> Tuple[bool, str, Optional[Path]]:
        """
        Создание архива для пользователя
        
        Args:
            user_id: ID пользователя
            date_str: Дата в формате ДД_ММ_ГГГГ
            
        Returns:
            Tuple[bool, str, Optional[Path]]: (успех, сообщение, путь к архиву)
        """
        try:
            # Создаем архиватор с использованием ApiClient
            archiver = ReportArchiveBuilder(self.config, self.api_client)
            
            # Создаем и загружаем архив
            result = await archiver.build_and_upload_archive(date_str, user_id)
            
            if result.get('success'):
                archive_path = Path(result.get('archive_path', ''))
                if archive_path.exists():
                    return True, "Архив успешно создан", archive_path
                else:
                    return False, "Архив создан, но файл не найден", None
            else:
                error_msg = result.get('error', 'Неизвестная ошибка')
                return False, f"Ошибка создания архива: {error_msg}", None
                
        except Exception as e:
            log.error(f"Ошибка создания архива: {e}")
            return False, f"Ошибка создания архива: {str(e)}", None
    
    async def process_new_company(self, company_data: Dict) -> Tuple[bool, str, Optional[str]]:
        """
        Реальная обработка новой компании через основной пайплайн
        
        Args:
            company_data: Данные компании
            
        Returns:
            Tuple[bool, str, Optional[str]]: (успех, сообщение, company_id)
        """
        try:
            log.info(f"Обработка новой компании: {company_data.get('Наименование')}")            
            
            start_time = time.time()
            
            success, message, details = await self.monitoring_integrator.process_single_company(
                company_data
            )
            
            processing_time = time.time() - start_time
            
            if not success:
                return False, message, None
            
            # 2. Даем время Graph DB обновиться
            log.info("Ожидание обновления Graph DB...")
            await asyncio.sleep(5)
            
            # 3. Генерируем отчет для этой компании
            company_id = company_data.get('id производителя')
            report_success, report_message = await self.generate_reports_for_companies([company_id])
            
            if report_success:
                final_message = (
                    f"✅ Компания '{company_data.get('Наименование')}' успешно обработана.\n"
                    f"📊 Результаты:\n"
                    f"   • Товаров добавлено: {details.get('products_count', 0)}\n"
                    f"   • Дистрибьюторов: {details.get('distributors_count', 0)}\n"
                    f"   • Файлов скачано: {details.get('files_downloaded', 0)}\n"
                    f"   • Время обработки: {processing_time:.1f} секунд\n"
                    f"   • Добавлена в Graph DB: {'Да' if details.get('graph_db_uploaded') else 'Нет'}\n"
                    f"   • Отчет сгенерирован: Да"
                )
                
                return True, final_message, company_id
            else:
                # Компания обработана, но отчет не сгенерирован
                return False, f"Компания обработана, но отчет не сгенерирован: {report_message}", company_id
                
        except Exception as e:
            log.error(f"Ошибка обработки компании: {e}", exc_info=True)
            return False, f"Ошибка обработки компании: {str(e)}", None