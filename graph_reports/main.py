# main.py 1.0.0
"""
Генератор отчетов об изменениях на сайтах компаний производителей
"""
import asyncio
import logging
import argparse
import sys
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

# Настройка путей для импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import ReportConfig
from graph_db_client import GraphDBClient
from data_comparator import DataComparator, ChangeType
from path_formatter import PathFormatter
from report_generators import GeneralReportGenerator, DetailedReportGenerator
from excel_writer import ExcelWriter

config = ReportConfig()

# Настройка логирования
logs_dir = config.logs_dir    
os.makedirs(logs_dir, exist_ok=True)
log_file_path = os.path.join(logs_dir, "report_generator.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler()
    ],
    force=True
)

log = logging.getLogger("main")

class ReportGeneratorApp:
    """Основной класс приложения для генерации отчетов"""
    
    def __init__(self, config: ReportConfig):
        self.config = config
        self.graph_db_client = None
        self.data_comparator = DataComparator()
        self.path_formatter = PathFormatter()
        self.general_report_generator = GeneralReportGenerator(self.path_formatter)
        self.detailed_report_generator = DetailedReportGenerator(self.path_formatter)
        self.excel_writer = ExcelWriter(config)
    
    async def generate_reports(self, report_date: Optional[str] = None, 
                             company_ids: Optional[List[str]] = None):
        """Генерация отчетов"""
        log.info(f"Начинаем генерацию отчетов. Дата отчета: {report_date}")
        
        try:
            # Инициализация клиента Graph DB
            async with GraphDBClient(self.config) as client:
                self.graph_db_client = client
                
                # Получаем данные
                log.info("Получаем данные из Graph DB...")
                
                old_data = []
                new_data = []
                
                if report_date:
                    # Получаем данные на дату отчета (Было)
                    log.info(f"Получаем данные на дату {report_date} (Было)...")
                    old_data = await client.get_companies_data_on_date(report_date)
                    
                    # Получаем текущие данные (Стало)
                    log.info("Получаем текущие данные (Стало)...")
                    new_data = await client.get_all_companies_data()
                else:
                    # Если даты нет, берем все текущие данные
                    log.info("Дата отчета не указана, получаем текущие данные...")
                    new_data = await client.get_all_companies_data()
                
                log.info(f"Получено {len(old_data)} компаний в 'Было', {len(new_data)} компаний в 'Стало'")
                
                # Если указаны конкретные компании, фильтруем данные
                if company_ids:
                    log.info(f"Фильтруем данные по {len(company_ids)} компаниям...")
                    old_data = [c for c in old_data if c.get("id") in company_ids]
                    new_data = [c for c in new_data if c.get("id") in company_ids]
                
                # Анализируем изменения
                log.info("Анализируем изменения...")
                changes_analysis = self.data_comparator.analyze_company_changes(old_data, new_data)
                
                # Генерируем общий отчет (теперь 13 столбцов)
                log.info("Формируем общий отчет...")
                general_report_data = self.general_report_generator.generate_report(
                    new_data, changes_analysis, report_date
                )
                
                # Генерируем детальный отчет
                log.info("Формируем детальный отчет...")
                detailed_report_data = self.detailed_report_generator.generate_report(
                    new_data, changes_analysis, report_date
                )
                
                # Сохраняем отчеты в Excel
                log.info("Сохраняем отчеты в Excel...")
                
                # Общий отчет
                general_report_path = self.excel_writer.write_general_report(
                    general_report_data,
                    output_path=os.path.join(self.config.output_dir, self.config.general_report_filename)
                )
                
                # Детальный отчет
                detailed_report_path = self.excel_writer.write_detailed_report(
                    detailed_report_data,
                    output_path=os.path.join(self.config.output_dir, self.config.detailed_report_filename)
                )
                
                log.info(f"Отчеты успешно сгенерированы:")
                log.info(f"  - Общий отчет (13 столбцов): {general_report_path}")
                log.info(f"  - Детальный отчет: {detailed_report_path}")
                
                # Выводим статистику
                self._print_statistics(old_data, new_data, changes_analysis)
                
                return {
                    "success": True,
                    "general_report": general_report_path,
                    "detailed_report": detailed_report_path,
                    "statistics": {
                        "old_companies_count": len(old_data),
                        "new_companies_count": len(new_data),
                        "analyzed_companies": len(changes_analysis)
                    }
                }
                
        except Exception as e:
            log.error(f"Ошибка при генерации отчетов: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def _print_statistics(self, old_data: List[Dict[str, Any]], 
                         new_data: List[Dict[str, Any]],
                         changes_analysis: Dict[str, Any]):
        """Вывод статистики"""
        print("\n" + "="*60)
        print("СТАТИСТИКА ФОРМИРОВАНИЯ ОТЧЕТОВ")
        print("="*60)
        
        # Подсчет типов изменений
        change_types = {}
        for company_id, analysis in changes_analysis.items():
            status = analysis.get("status")
            if isinstance(status, ChangeType):
                change_types[status.value] = change_types.get(status.value, 0) + 1
            elif isinstance(status, str):
                change_types[status] = change_types.get(status, 0) + 1
        
        print(f"Компаний в 'Было': {len(old_data)}")
        print(f"Компаний в 'Стало': {len(new_data)}")
        print(f"Проанализировано компаний: {len(changes_analysis)}")
        print("\nСтатусы компаний:")
        
        for change_type, count in change_types.items():
            print(f"  - {change_type}: {count}")
        
        # Подсчет продуктов, поставщиков и файлов
        total_products = sum(len(c.get("products", [])) for c in new_data)
        total_suppliers = sum(len(c.get("suppliers", [])) for c in new_data)
        total_company_files = sum(len(c.get("files", [])) for c in new_data)
        
        print(f"\nВсего продуктов: {total_products}")
        print(f"Всего поставщиков: {total_suppliers}")
        print(f"Всего файлов компаний: {total_company_files}")
        print("="*60 + "\n")


def parse_arguments():
    """Парсинг аргументов командной строки"""
    parser = argparse.ArgumentParser(
        description="Генератор отчетов об изменениях на сайтах компаний производителей"
    )
    
    parser.add_argument(
        "--report-date",
        type=str,
        help="Дата отчета в формате YYYY-MM-DD HH:MM:SS"
    )
    
    parser.add_argument(
        "--company-ids",
        type=str,
        help="ID компаний через запятую (опционально)"
    )
    
    parser.add_argument(
        "--config-file",
        type=str,
        default="config.yaml",
        help="Путь к файлу конфигурации"
    )
    
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Директория для сохранения отчетов"
    )
    
    parser.add_argument(
        "--test",
        action="store_true",
        help="Запустить тестовый режим с мок-данными"
    )
    
    return parser.parse_args()


async def main():
    """Основная функция"""
    args = parse_arguments()    
       
    # Парсинг ID компаний
    company_ids = None
    if args.company_ids:
        company_ids = [cid.strip() for cid in args.company_ids.split(",") if cid.strip()]
        log.info(f"Будут обработаны компании: {company_ids}")
    
    # Создание конфигурации
    config = ReportConfig()
    
    if args.output_dir:
        config.output_dir = args.output_dir
    
    # Создание и запуск приложения
    app = ReportGeneratorApp(config)
    
    result = await app.generate_reports(
        report_date=args.report_date,
        company_ids=company_ids
    )
    
    if result["success"]:
        log.info("Генерация отчетов завершена успешно!")
        sys.exit(0)
    else:
        log.error(f"Ошибка при генерации отчетов: {result.get('error')}")
        sys.exit(1)
    
    # Тестируем формирование отчетов
    config = ReportConfig()
    path_formatter = PathFormatter()
    data_comparator = DataComparator()
    general_generator = GeneralReportGenerator(path_formatter)
    detailed_generator = DetailedReportGenerator(path_formatter)
    excel_writer = ExcelWriter(config)
    
    # Создаем тестовые данные
    old_data = []  # Пустые старые данные (все будет новым)
    new_data = [test_company]
    
    # Анализируем изменения
    changes_analysis = data_comparator.analyze_company_changes(old_data, new_data)
    
    # Генерируем отчеты
    general_report = general_generator.generate_report(new_data, changes_analysis, None)
    detailed_report = detailed_generator.generate_report(new_data, changes_analysis, None)
    
    # Проверяем структуру отчетов
    print(f"Общий отчет: {len(general_report[0])} столбцов")
    print(f"Детальный отчет: {len(detailed_report[0])} столбцов")
    
    # Сохраняем
    general_path = excel_writer.write_general_report(general_report, "test_general.xlsx")
    detailed_path = excel_writer.write_detailed_report(detailed_report, "test_detailed.xlsx")
    
    print(f"Тестовые отчеты созданы:")
    print(f"  - Общий (13 столбцов): {general_path}")
    print(f"  - Детальный: {detailed_path}")
    
    return True


if __name__ == "__main__":
    asyncio.run(main())