# report_service.py
import asyncio
import time
import os
from datetime import datetime
from copy import copy
from contextlib import ExitStack
from collections import defaultdict
from report_queue import ReportQueue, company_lock, company_ready, data_version
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
        self.queue = ReportQueue(config.bot_dir)
        self.path_formatter = PathFormatter()
        
        # Настройка конфигурации для отчетов
        from graph_reports.config import ReportConfig
        self.report_config = ReportConfig(base_dir=config.base_dir)
        self.report_config.output_dir = str(config.reports_dir)
        self.report_config.graph_db_api_url = config.graph_db_api_url
        self.report_config.graph_db_request_timeout = config.graph_db_request_timeout

        # Инициализируем интегратор
        self.monitoring_integrator = MonitoringIntegrator(config)

    async def sync_report_dates(self):
        # Журнал выдачи сохраняет результат даже при недоступности Graph DB.
        try:
            with company_lock(self.config.bot_dir, "__report_date_sync__"):
                groups = defaultdict(list)
                for item in self.queue.unsynced():
                    groups[item['id']].append(item)
                for request_id, items in groups.items():
                    for item in items:
                        if not await self.api_client.confirm_report_version(
                                item['company_id'], item['report_date'], item['version']):
                            return
                    self.queue.mark_synced(request_id)
        except BlockingIOError:
            return
        except Exception:
            log.exception("Подтверждения выдачи сохранены локально; синхронизация будет повторена")

    def _pending(self, company_id):
        root = Path(os.getenv('GRAPH_DB_PENDING_DIR', str(Path(self.config.base_dir) / 'GraphDB_Pending')))
        return (root / (str(company_id) + '.json')).exists()

    async def prepare_report(self, limit, user_id, event_key, company_ids=None):
        """Резервируем компании и собираем неизменяемый архив отдельного запроса."""
        await self.sync_report_dates()
        async with GraphDBClient(self.report_config) as client:
            companies = await client.get_all_companies_data()
        wanted = set(map(str, company_ids)) if company_ids is not None else None
        available = []
        for company in companies:
            cid = str(company.get('id', ''))
            if not cid or (wanted is not None and cid not in wanted) or self._pending(cid) or not company_ready(self.config.bot_dir, cid):
                continue
            try:
                with company_lock(self.config.bot_dir, cid):
                    available.append(company)
            except BlockingIOError:
                continue
        batch = self.queue.reserve(available, limit, user_id, event_key, CompanyRanker.rank_companies)
        if batch is None:
            raise ValueError("Нет доступных компаний для нового отчёта: данные уже выданы или обрабатываются")
        try:
            with ExitStack() as locks:
                for company in sorted(batch['companies'], key=lambda c: str(c['id'])):
                    locks.enter_context(company_lock(self.config.bot_dir, str(company['id'])))
                async with GraphDBClient(self.report_config) as client:
                    current = {str(c['id']): c for c in await client.get_all_companies_data()}
                for company in batch['companies']:
                    cid = str(company['id'])
                    if (cid not in current or self._pending(cid) or not company_ready(self.config.bot_dir, cid)
                            or data_version(current[cid]) != data_version(company)):
                        raise ValueError("Данные изменились во время выбора; повторите запрос")
                config = copy(self.config)
                config.reports_dir = Path(self.config.reports_dir) / batch['id']
                config.archive_temp_dir = Path(self.config.archive_temp_dir) / batch['id']
                config.reports_dir.mkdir(parents=True, exist_ok=True)
                config.archive_temp_dir.mkdir(parents=True, exist_ok=True)
                ids = [str(c['id']) for c in batch['companies']]
                success, message = await self.generate_reports_for_companies(
                    ids, companies=batch['companies'], previous=batch['previous'], output_config=config)
                if not success:
                    raise ValueError(message)
                archiver = ReportArchiveBuilder(config, self.api_client)
                info = archiver.extract_companies_from_general_report()
                if set(info) != set(ids):
                    raise ValueError("Состав общего отчёта не совпадает с резервом")
                folders = archiver.find_company_folders(set(ids))
                directory, stats = archiver.create_archive_structure(info, folders)
                if (directory is None or stats['errors'] or stats['skipped_no_folder']
                        or stats['skipped_empty']):
                    raise ValueError("Не удалось собрать полный комплект файлов компаний")
                archive, stats = archiver.create_zip_archive(directory)
                if not archive or not stats['success']:
                    raise ValueError("Не удалось создать архив")
            # После копирования pipeline может обновлять исходные файлы.
            # Архив и сохранённый снимок уже принадлежат выбранной версии.
            date = datetime.now().strftime('%d_%m_%Y')
            destination = f"{str(user_id).zfill(4)}_{batch['id']}"
            success, message, url = await self.api_client.upload_archive(archive, date, destination)
            if not success or not url:
                raise ValueError(message)
            batch['download_url'] = url
            batch['archive_path'] = str(archive)
            return batch
        except BaseException:
            self.queue.release(batch['id'], 'Сборка или загрузка архива не завершена')
            raise

    async def confirm_delivery(self, batch, message_id):
        self.queue.delivered(batch['id'], message_id)
        await self.sync_report_dates()

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
    
    async def generate_reports_for_companies(self, company_ids: List[str], *, companies=None, previous=None, output_config=None) -> Tuple[bool, str]:
        """
        Генерация отчетов для указанных компаний
        
        Args:
            company_ids: Список ID компаний
            
        Returns:
            Tuple[bool, str]: (успех, сообщение)
        """
        try:
            log.info(f"Генерация отчетов для {len(company_ids)} компаний")
            
            output_config = output_config or self.config
            async with GraphDBClient(self.report_config) as client:
                if companies is None:
                    all_companies = await client.get_all_companies_data()
                    companies = [c for c in all_companies if str(c.get("id")) in set(map(str, company_ids))]
                filtered_companies = companies
                if not filtered_companies:
                    return False, "Не найдено компаний с указанными ID"
                previous = previous or {}
                old_data = []
                history = {}
                for company in filtered_companies:
                    cid = str(company["id"])
                    if cid in previous:
                        old_data.append(previous[cid])
                    elif company.get("report_date"):
                        date = company["report_date"]
                        if date not in history:
                            history[date] = {str(c["id"]): c for c in
                                            await client.get_companies_data_on_date(date)}
                        if cid not in history[date]:
                            raise ValueError(f"Не найдено состояние компании {cid} на дату предыдущего отчёта")
                        old_data.append(history[date][cid])

                # Анализируем изменения
                data_comparator = DataComparator()
                changes_analysis = data_comparator.analyze_company_changes(old_data, filtered_companies)
                
                # Генерируем отчеты
                general_generator = GeneralReportGenerator(self.path_formatter)
                detailed_generator = DetailedReportGenerator(self.path_formatter)
                
                general_report_data = general_generator.generate_report(
                    filtered_companies, changes_analysis, None
                )
                
                detailed_report_data = detailed_generator.generate_report(
                    filtered_companies, changes_analysis, None
                )
                
                # Сохраняем отчеты в Excel
                excel_writer = ExcelWriter(self.report_config)
                
                general_report_path = excel_writer.write_general_report(
                    general_report_data,
                    output_path=output_config.reports_dir / output_config.general_report_name
                )
                
                detailed_report_path = excel_writer.write_detailed_report(
                    detailed_report_data,
                    output_path=output_config.reports_dir / output_config.detailed_report_name
                )
                
                log.info(f"Отчеты сгенерированы: {general_report_path}, {detailed_report_path}")
                return True, "Отчеты успешно сгенерированы"
                
        except Exception as e:
            log.error(f"Ошибка генерации отчетов: {e}")
            return False, f"Ошибка генерации отчетов: {str(e)}"
    
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