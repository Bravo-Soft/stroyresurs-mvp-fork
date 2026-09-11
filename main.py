# main.py 1.1.0
import asyncio
import logging
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import os
import re
import json
import time
import traceback
import aiofiles
from urllib.parse import urlparse
from config import Config
from web_crawler import WebCrawler  
from ai_integration import AITunnelClient
from docx_generator import DOCXGenerator
from xlsx_generator import XLSXGenerator
from product_utils import extract_product_name_from_ai_text, sanitize_company_name, sanitize_filename
from checkpoint_manager import CheckpointManager
from rate_limiter import RateLimiter
from performance_monitor import PerformanceMonitor
from product_id_manager import ProductIDManager
from file_id_manager import FileIDManager
from distributor_id_manager import DistributorIDManager
from graph_db_uploader import GraphDBUploader, GraphDBFatalError
from file_converter import FileConverter 
from kafka_manager import TaskMessage, KafkaTaskManager, TaskType, TaskStatus
from pipeline_orchestrator import PipelineOrchestrator
import activity_heartbeat
# from site_profiler import SiteProfiler
from text_extractor import html_to_markdown
from processing_time_tracker import ProcessingTimeTracker

log = logging.getLogger("main")


def is_excluded_product_name(product_name: str, patterns: List[str]) -> bool:
    """Слова-исключения (N7): True, если название товара совпадает (по целому слову,
    регистронезависимо) с одним из шаблонов-исключений заказчика (Губка, Марля, Мыло, Салфетка…)."""
    if not product_name or not patterns:
        return False
    name_lower = str(product_name).lower()
    for pat in patterns:
        pat = str(pat).strip().lower()
        if pat and re.search(r'\b' + re.escape(pat) + r'\b', name_lower):
            return True
    return False


def _count_value_numeric(value: Any) -> int:
    """Сколько ЧИСЛОВЫХ характеристик несёт ОДНО значение.
    Габариты вида AAA×BBB×CCC считаются по числу осей (12×12×123 → 3, 390×90×188 → 3,
    60×60 → 2); прочее значение с числом → 1; значение без числа → 0."""
    s = str(value)
    if not re.search(r'\d', s):
        return 0
    # размеры: 2+ числовых компонента, разделённых ×/x/х/X/Х/*
    parts = re.split(r'\s*[×xхXХ*]\s*', s)
    nums = [p for p in parts if re.search(r'\d', p)]
    if len(nums) >= 2:
        return len(nums)
    return 1


def count_numeric_characteristics(product: Dict[str, Any]) -> int:
    """ГЕЙТ качества (решение пользователя, июнь-2026): число ЧИСЛОВЫХ тех.характеристик товара.
    В итоговую обработку (карточка товара + отправка в БД) идут ТОЛЬКО товары с >=3 такими
    характеристиками — параметрический поиск в системе завязан именно на числовые значения
    характеристик. Обходит specifications (с вложенными словарями/списками) + поля dimensions/weight.
    Габариты вида AAA×BBB×CCC засчитываются по числу осей (12×12×123 → 3)."""
    if not isinstance(product, dict):
        return 0
    count = 0

    def walk(obj):
        nonlocal count
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif v is not None and str(v).strip():
                    count += _count_value_numeric(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)

    walk(product.get('specifications', {}))
    for fld in ('dimensions', 'weight'):
        val = product.get(fld)
        if val:
            count += _count_value_numeric(val)
    return count


def collapse_chelaz_size_variants(pages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """D85 (типоразмеры + URL-алиасы chelaz.ru, вариант A): в каталоге /katalog/.../<модель>/
    сайт держит общую карточку-диапазон <модель>.html И (а) по отдельной странице на каждый
    типоразмер (<модель>-dnXX.html и прочие схемы того же каталога), И (б) алиас-URL самого
    каталога /<модель>/, который 301-редиректит на <модель>.html. Обе группы — дубли одной
    модели: общая карточка уже содержит полную таблицу всех размеров + цену + описание, поэтому
    размерные страницы — строгое подмножество. Оставляем ОДНУ общую карточку модели, а размерные
    страницы и алиас-каталог того же семейства отбрасываем ДО извлечения (LLM по ним не гоняем).
    Fallback: нет общей .html, но есть алиас-каталог — держим алиас (та же общая по 301);
    нет никакой общей (только размерные) — оставляем всё (данные не теряем).
    Гейт: только category=='product' на chelaz.ru; на прочих сайтах и непродуктовых страницах
    это no-op (структура «1 каталог = 1 модель» проверена именно для chelaz).
    Возвращает (оставленные_страницы, отброшенные_страницы)."""
    families: Dict[str, Dict[str, list]] = {}
    passthrough: List[Dict[str, Any]] = []
    for p in pages:
        parsed = urlparse(p.get('url') or '')
        if p.get('category') != 'product' or 'chelaz.ru' not in parsed.netloc.lower():
            passthrough.append(p)
            continue
        m = re.match(r'(.*)/([^/]+)\.html?$', parsed.path, re.IGNORECASE)
        if m:
            famdir, leaf = m.group(1).lower(), m.group(2).lower()
            dirname = famdir.rsplit('/', 1)[-1]
            fam = families.setdefault(famdir, {'general': [], 'children': [], 'diralias': []})
            (fam['general'] if leaf == dirname else fam['children']).append(p)
        else:
            sp = re.sub(r'/+$', '', parsed.path).lower()  # алиас-каталог /<модель>/ -> 301 на <модель>.html
            if not sp:
                passthrough.append(p)
                continue
            families.setdefault(sp, {'general': [], 'children': [], 'diralias': []})['diralias'].append(p)
    kept: List[Dict[str, Any]] = list(passthrough)
    dropped: List[Dict[str, Any]] = []
    for fam in families.values():
        if fam['general']:                        # каноничная общая .html-карточка модели
            kept.append(fam['general'][0])
            dropped.extend(fam['general'][1:])    # страховка: общая .html уникальна
            dropped.extend(fam['children'])       # размерные дубли
            dropped.extend(fam['diralias'])       # алиас-каталог того же семейства (301 на ту же общую)
        elif fam['diralias'] and fam['children']:  # общей .html нет, но алиас-каталог отдаёт её по 301
            kept.append(fam['diralias'][0])
            dropped.extend(fam['diralias'][1:])
            dropped.extend(fam['children'])
        else:                                     # только размерные (fallback) либо одиночная страница/алиас
            kept.extend(fam['general'])
            kept.extend(fam['children'])
            kept.extend(fam['diralias'])
    return kept, dropped


class CompanyStatistics:
    """Статистика обработки для отдельной компании"""
    
    def __init__(self, company_data: Dict[str, str]):
        self.company_data = company_data
        self.start_time = time.time()
        self.end_time = None
        
        self.products_found = 0
        self.products_processed = 0
        self.products_failed = 0
        self.products_added_to_db = 0
        self.cards_generated = 0
        
        self.files_found = 0
        self.files_downloaded = 0
        self.files_failed = 0
        # Разбивка обработанных страниц по категориям
        self.product_pages_processed = 0
        self.company_pages_processed = 0
        self.distributor_pages_processed = 0

        self.distributors_found = 0
        self.distributors_processed = 0
        self.distributors_added_to_db = 0
        
        self.input_tokens = 0
        self.output_tokens = 0
        self.embedding_tokens = 0
        
        # Graph DB статистика
        self.graph_db_uploaded = False
        self.graph_db_products_sent = 0
        self.graph_db_distributors_sent = 0
        self.graph_db_files_sent = 0
        self.graph_db_errors = []
        
        # Статистика дубликатов
        self.urls_skipped_duplicate = 0
        self.products_skipped_duplicate = 0
        # Слова-исключения (N7) и гейт качества (товары с <3 числовых характеристик, в БД не идут)
        self.products_skipped_excluded = 0
        self.products_below_min_specs = 0
        
        # Статистика конвертации файлов
        self.file_conversion_enabled = False
        self.file_conversion_completed = False
        self.file_conversion_errors = []
        self.files_converted = 0
        self.files_conversion_failed = 0
        
        self.errors = []
    
    def finish(self):
        self.end_time = time.time()
    
    def get_processing_time(self):
        if self.end_time:
            seconds = self.end_time - self.start_time
        else:
            seconds = time.time() - self.start_time
        
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}ч {minutes}м {secs:.1f}с"
    
    def to_dict(self):
        return {
            'products_found': self.products_found,
            'products_processed': self.products_processed,
            'products_failed': self.products_failed,
            'products_added_to_db': self.products_added_to_db,
            'cards_generated': self.cards_generated,
            'files_found': self.files_found,
            'files_downloaded': self.files_downloaded,
            'files_failed': self.files_failed,
            'product_pages_processed': self.product_pages_processed,
            'company_pages_processed': self.company_pages_processed,
            'distributor_pages_processed': self.distributor_pages_processed,
            'distributors_found': self.distributors_found,
            'distributors_processed': self.distributors_processed,
            'distributors_added_to_db': self.distributors_added_to_db,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'embedding_tokens': self.embedding_tokens,
            'graph_db_uploaded': self.graph_db_uploaded,
            'graph_db_products_sent': self.graph_db_products_sent,
            'graph_db_distributors_sent': self.graph_db_distributors_sent,
            'graph_db_files_sent': self.graph_db_files_sent,
            'graph_db_errors': self.graph_db_errors,
            'urls_skipped_duplicate': self.urls_skipped_duplicate,
            'products_skipped_duplicate': self.products_skipped_duplicate,
            'products_skipped_excluded': self.products_skipped_excluded,
            'products_below_min_specs': self.products_below_min_specs,
            'file_conversion_enabled': self.file_conversion_enabled,
            'file_conversion_completed': self.file_conversion_completed,
            'file_conversion_errors': self.file_conversion_errors,
            'files_converted': self.files_converted,
            'files_conversion_failed': self.files_conversion_failed,
            'processing_time': self.get_processing_time(),
            'errors': self.errors
        }

class Statistics:
    """Класс для сбора статистики выполнения"""
    
    def __init__(self):
        self.start_time = time.time()
        
        self.pages_processed = 0
        self.companies_processed = 0
        self.companies_success = 0
        self.companies_failed = 0
        
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_embedding_tokens = 0
        # Разбивка обработанных страниц по категориям
        self.product_pages_processed = 0
        self.company_pages_processed = 0
        self.distributor_pages_processed = 0
        # Файлы (агрегировано по компаниям)
        self.total_files_found = 0
        self.total_files_downloaded = 0
        self.total_files_failed = 0
        
        # Graph DB статистика
        self.graph_db_companies_sent = 0
        self.graph_db_products_sent = 0
        self.graph_db_distributors_sent = 0
        self.graph_db_files_sent = 0
        self.graph_db_successful_sends = 0
        self.graph_db_failed_sends = 0
        
        # Статистика дубликатов
        self.total_urls_skipped_duplicate = 0
        self.total_products_skipped_duplicate = 0
        
        self.price_per_million_input_tokens: float = 13.5
        self.price_per_million_output_tokens: float = 108.0
        self.price_per_million_embedding_tokens: float = 3.6
    
    def get_execution_time(self):
        seconds = time.time() - self.start_time
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}ч {minutes}м {secs:.1f}с"
    
    def get_tokens_cost(self):
        input_cost = (self.total_input_tokens / 1_000_000) * self.price_per_million_input_tokens
        output_cost = (self.total_output_tokens / 1_000_000) * self.price_per_million_output_tokens
        return round(input_cost + output_cost, 2)
    
    def get_embedding_cost(self):
        return round((self.total_embedding_tokens / 1_000_000) * self.price_per_million_embedding_tokens, 2)
    
    def get_total_cost(self):
        return round(self.get_tokens_cost() + self.get_embedding_cost(), 2)
    
    def print_statistics(self):
        stats = f"""
============================================================
СТАТИСТИКА ВЫПОЛНЕНИЯ
============================================================
Общее время выполнения: {self.get_execution_time()}
Обработано компаний: {self.companies_processed}
- Успешно: {self.companies_success}
- С ошибками: {self.companies_failed}
Обработано страниц: {self.pages_processed}
----------------------------------------
ДУБЛИКАТЫ (ПРОПУЩЕНО):
URL пропущено из-за дубликатов: {self.total_urls_skipped_duplicate}
Товаров пропущено из-за дубликатов: {self.total_products_skipped_duplicate}
----------------------------------------
ИСПОЛЬЗОВАНИЕ LLM:
Всего входных токенов: {self.total_input_tokens}
Всего выходных токенов: {self.total_output_tokens}
Общее количество токенов: {self.total_input_tokens + self.total_output_tokens}
Всего эмбеддингов (токенов): {self.total_embedding_tokens}
----------------------------------------
GRAPH DB ОТПРАВКА:
Компаний отправлено: {self.graph_db_companies_sent}
Продуктов отправлено: {self.graph_db_products_sent}
Дистрибьюторов отправлено: {self.graph_db_distributors_sent}
Файлов отправлено: {self.graph_db_files_sent}
Успешных отправок: {self.graph_db_successful_sends}
Неудачных отправок: {self.graph_db_failed_sends}
----------------------------------------
СТОИМОСТЬ:
Общая стоимость токенов: {self.get_tokens_cost()}₽
Общая стоимость эмбеддингов: {self.get_embedding_cost()}₽
Итоговая стоимость: {self.get_total_cost()}₽
============================================================
"""
        print(stats)
        return stats

class DirectoryManager:
    """Менеджер для управления структурой директорий"""
    
    def __init__(self, config: Config):
        self.config = config
        self._created_dirs = set()
    
    def create_company_directory_structure(self, company_dir: str) -> Dict[str, str]:
        """Создает унифицированную структуру папок компании"""
        directories = {
            'company_root': company_dir,
            'products_dir': os.path.join(company_dir, "Products"),
            'certificates_dir': os.path.join(company_dir, "Certificates"),
            'documents_dir': os.path.join(company_dir, "Documents"), 
            'instructions_dir': os.path.join(company_dir, "Instructions"),
            'price_lists_dir': os.path.join(company_dir, "Price_lists"),
        }
        
        for dir_path in directories.values():
            if not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
                log.info(f"Создана директория: {dir_path}")
        
        self._created_dirs.add(company_dir)
        return directories
    
    def ensure_product_subdirectories(self, product_folder: str) -> Dict[str, str]:
        """Создает подпапки для товара только при необходимости"""
        structure = {
            'product_root': product_folder,
            'images_dir': os.path.join(product_folder, "Images"),
            'documents_dir': os.path.join(product_folder, "Documents"),
        }
        
        # Создаем подпапки только если они еще не существуют
        for dir_path in [structure['images_dir'], structure['documents_dir']]:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
                log.debug(f"Создана подпапка товара: {dir_path}")
        
        return structure
    
    def validate_directory_structure(self, company_dir: str) -> bool:
        """Проверяет корректность структуры директорий"""
        expected_dirs = ['Products', 'Certificates', 'Documents', 'Instructions', 'Price_lists']
        
        for expected_dir in expected_dirs:
            dir_path = os.path.join(company_dir, expected_dir)
            if not os.path.exists(dir_path):
                log.warning(f"Отсутствует ожидаемая директория: {dir_path}")
                return False
        
        # Проверяем структуру товаров
        products_dir = os.path.join(company_dir, "Products")
        if os.path.exists(products_dir):
            for item in os.listdir(products_dir):
                item_path = os.path.join(products_dir, item)
                if os.path.isdir(item_path):
                    # Проверяем, что нет вложенных папок Products
                    if "Products" in os.listdir(item_path):
                        log.error(f"Обнаружена лишняя папка Products в: {item_path}")
                        return False
                        
        return True

class MonitoringSystem:
    """Версия системы мониторинга с интеграцией временного хранилища и Graph DB"""
    
    def __init__(self, config: Config, kafka_manager: KafkaTaskManager):
        self.config = config
        self.kafka_manager = kafka_manager
        self._kafka_initialized = True
        self.statistics = Statistics()
        # Тайминги «чистой обработки» по модулям (лист «Статистика по времени обработки»)
        self.processing_tracker = ProcessingTimeTracker()
        self.statistics.processing_tracker = self.processing_tracker

        # Инициализация rate limiter для 100 запросов в 10 секунд        
        self.rate_limiter = RateLimiter(config.rate_limit_requests_per_10s)

        # Инициализация мониторинга производительности        
        self.performance_monitor = PerformanceMonitor()

        self.ai_client = AITunnelClient(config.ollama_url, config.ollama_api_key, rate_limiter=self.rate_limiter,
            max_concurrent_requests=config.max_concurrent_llm_requests, performance_monitor=self.performance_monitor,
            model=config.ollama_model, processing_tracker=self.processing_tracker
        )
        if config.vector_db_enable:
            from llama_index_integration import ProductIndexManager  # ленивый импорт: chromadb нужен только при включённой вектор-БД
            self.index_manager = ProductIndexManager(config)
        else:
            self.index_manager = None
        self.docx_generator = DOCXGenerator()
        self.xlsx_generator = XLSXGenerator(self.config) 
        
        self.crawler = WebCrawler(config, processing_tracker=self.processing_tracker)
        self.checkpoint_manager = CheckpointManager(config)
        self._global_processed_urls = set()
        
        self.unavailable_sites_file = os.path.join(config.reports_dir, "unavailable_sites.json")
        self.unavailable_sites = self._load_unavailable_sites()

        # Атомарные блокировки для предотвращения гонок
        self._processing_urls = set()
        self._processing_urls_lock = asyncio.Lock()
        self._processing_product_ids = set()
        self._processing_product_ids_lock = asyncio.Lock()
        # Лок сериализации чекпоинтов из воркеров потоковой обработки
        self._stream_ckpt_lock = asyncio.Lock()
        
        self._last_checkpoint_pages = 0
        self.ensure_directories()

        # Менеджеры ID
        self.product_id_manager = ProductIDManager()
        self.file_id_manager = FileIDManager()
        self.distributor_id_manager = DistributorIDManager()
        
        # Кэши для быстрой проверки дубликатов
        self._global_processed_product_ids = set()
        # D59 (C): дедуп кросс-доменных/путь-версий одного товара по слагу — НА СТАДИИ
        # УСПЕШНОГО извлечения (не на crawl), чтобы провалившаяся версия (soft-404 на
        # основном домене холдинга) не блокировала рабочую со вторичного домена. Ключ —
        # crawler._product_dedup_key(source_url) = <группа доменов>|<слаг>.
        self._global_processed_slug_keys = set()
        self._processing_slug_keys = set()
        self._global_processed_file_ids = set()
        self._global_processed_distributor_ids = set()

        # Менеджер директорий
        self.directory_manager = DirectoryManager(config)
        
        # Кэш информации о файлах для Graph DB
        self._file_info_cache = {}

        # Инициализация Graph DB Uploader
        if self.config.graph_db_enable:
            self.graph_db_uploader = GraphDBUploader(config, processing_tracker=self.processing_tracker)
            log.info("Graph DB Uploader инициализирован")
        else:
            self.graph_db_uploader = None
            log.info("Graph DB Uploader отключен в конфигурации")        

    async def initialize(self):        
        """Инициализация краулера"""
        await self.crawler.initialize()
        log.info("Менеджер Kafka инициализирован")
                

    async def close(self):
        """Закрытие ресурсов краулера"""
        self._stop_requested = True
        await self.crawler.close()

    def ensure_directories(self):
        os.makedirs(self.config.base_dir, exist_ok=True)
        os.makedirs(self.config.reports_dir, exist_ok=True)
        os.makedirs(self.config.vector_db_path, exist_ok=True)
        os.makedirs(self.config.documents_dir, exist_ok=True)
        os.makedirs(self.config.product_cards_dir, exist_ok=True)

    def read_company_list(self) -> List[Dict[str, str]]:
        try:
            df = pd.read_excel(self.config.excel_path)
            companies = []
            
            for _, row in df.iterrows():
                if 'Website' in row and pd.notna(row['Website']):
                    original_name = ''
                    for col in ['Наименование', 'Название']:
                        if col in row and pd.notna(row[col]):
                            original_name = row[col]
                            break
                    
                    manufacturer_id = ''
                    for col in ['ID производителя', 'id производителя']:
                        if col in row and pd.notna(row[col]):
                            manufacturer_id = str(row[col])
                            break
                
                    if not manufacturer_id:
                        manufacturer_id = f"company_{len(companies)}"

                    safe_name = sanitize_company_name(original_name)
                    
                    companies.append({
                        'original_name': original_name,
                        'safe_name': safe_name,
                        'website': row['Website'],
                        'folder_name': safe_name,
                        'company_id': manufacturer_id,
                        'generated_company_id': f"company_{len(companies)}"
                    })
            
            return companies
        except Exception as e:
            log.error(f"Ошибка чтения Excel: {e}")
            return []
    
    def _load_unavailable_sites(self) -> List[Dict]:
        """Загрузка ранее сохраненных недоступных сайтов"""
        try:
            if os.path.exists(self.unavailable_sites_file):
                with open(self.unavailable_sites_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            log.warning(f"Ошибка загрузки файла недоступных сайтов: {e}")
        return []
    
    async def save_unavailable_site(self, company_data: Dict[str, str], 
                               error_msg: str,
                               http_status: Optional[int] = None,
                               error_type: Optional[str] = None,
                               response_headers: Optional[Dict] = None,
                               company_stats: Optional[CompanyStatistics] = None):
        """Сохранение информации о недоступном сайте с детальной информацией об ошибке"""
        try:
            unavailable_site_info = {
                'company_name': company_data['original_name'],
                'safe_name': company_data['safe_name'],
                'website': company_data['website'],
                'company_id': company_data['company_id'],
                'error_message': error_msg,
                'http_status': http_status,
                'error_type': error_type or 'unknown',
                'response_headers': response_headers,
                'server_error': None,
                'timestamp': datetime.now().isoformat(),
                'attempt_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            # Извлекаем информацию об ошибке сервера из сообщения
            if http_status:
                unavailable_site_info['server_error'] = f"HTTP {http_status}"
            
            # Попробуем извлечь детали ошибки из сообщения
            if error_msg:
                # Ищем стандартные HTTP ошибки
                http_error_patterns = [
                    r'HTTP\s+(\d{3})',
                    r'Status\s+code\s+(\d{3})',
                    r'(\d{3})\s+[A-Za-z\s]+error'
                ]
                
                for pattern in http_error_patterns:
                    match = re.search(pattern, error_msg, re.IGNORECASE)
                    if match:
                        unavailable_site_info['server_error'] = f"HTTP {match.group(1)}"
                        break
                
                # Ищем другие типы ошибок
                if 'timeout' in error_msg.lower():
                    unavailable_site_info['error_type'] = 'timeout'
                elif 'connection refused' in error_msg.lower() or 'connection reset' in error_msg.lower():
                    unavailable_site_info['error_type'] = 'connection_error'
                elif 'ssl' in error_msg.lower():
                    unavailable_site_info['error_type'] = 'ssl_error'
                elif 'dns' in error_msg.lower():
                    unavailable_site_info['error_type'] = 'dns_error'
            
            if company_stats:
                unavailable_site_info.update({
                    'processing_time': company_stats.get_processing_time(),
                    'statistics': company_stats.to_dict()
                })
            
            # Добавляем в список, проверяя на дубликаты
            existing_sites = [site for site in self.unavailable_sites 
                            if site['website'] == company_data['website']]
            
            if not existing_sites:
                self.unavailable_sites.append(unavailable_site_info)
            else:
                # Обновляем существующую запись
                for i, site in enumerate(self.unavailable_sites):
                    if site['website'] == company_data['website']:
                        self.unavailable_sites[i] = unavailable_site_info
                        break
            
            # Сохраняем в файл
            self._save_unavailable_sites_to_file()
            
            log.info(f"Информация о недоступном сайте сохранена: {company_data['original_name']} - {error_msg}")
            
        except Exception as e:
            log.error(f"Ошибка сохранения информации о недоступном сайте: {e}")
    
    def _save_unavailable_sites_to_file(self):
        """Сохранение списка недоступных сайтов в файл"""
        try:
            # Создаем директорию reports_dir если не существует
            os.makedirs(os.path.dirname(self.unavailable_sites_file), exist_ok=True)
            
            with open(self.unavailable_sites_file, 'w', encoding='utf-8') as f:
                json.dump(self.unavailable_sites, f, ensure_ascii=False, indent=2)
                
            log.info(f"Файл недоступных сайтов сохранен: {self.unavailable_sites_file}")
            
        except Exception as e:
            log.error(f"Ошибка сохранения файла недоступных сайтов: {e}")

    async def full_site_crawling(self, company_data: Dict[str, str], company_dir: str) -> Dict[str, Any]:
        log.info(f"Начинаем полный краулинг сайта: {company_data['website']}")
        
        try:
            crawl_result = await self.crawler.crawl_site(
                company_data['website'],
                company_data['original_name'],
                company_dir
            )
            
            if 'error' in crawl_result:
                return {
                    'status': 'crawling_error',
                    'error': crawl_result['error'],
                    'products': [],
                    'downloaded_files': []
                }
            
            self.statistics.pages_processed += crawl_result.get('total_pages', 0)
            
            return {
                'status': 'success',
                'crawl_data': crawl_result,
                'products_count': len(crawl_result.get('products', [])),
                'files_count': len(crawl_result.get('downloaded_files', [])),
                'stored_pages': crawl_result.get('stored_pages', [])
            }
            
        except Exception as e:
            log.error(f"Ошибка краулинга сайта {company_data['website']}: {e}")
            return {
                'status': 'error',
                'error': str(e),
                'products': [],
                'downloaded_files': []
            }  
          
    async def resume_pending_graphdb_uploads(self) -> bool:
        """Восстанавливает отправку отложенных данных в Graph DB."""
        if not self.config.graph_db_enable or not self.graph_db_uploader:
            log.info("Graph DB отключён, пропускаем восстановление")
            return True
        log.info("=== НАЧАЛО ВОССТАНОВЛЕНИЯ ОТЛОЖЕННЫХ ОТПРАВОК В GRAPH DB ===")
        try:
            result = await self.graph_db_uploader.resume_all_pending_uploads()
            if result:
                log.info("=== ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО УСПЕШНО ===")
            else:
                log.critical("=== ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО С ОШИБКАМИ (пайплайн будет остановлен) ===")
            return result
        except Exception as e:
            log.error(f"Неожиданная ошибка при восстановлении: {e}")
            return False

    async def process_stored_pages_with_checkpoints(self, stored_pages: List[Dict], company_data: Dict[str, str], 
                                              company_stats: CompanyStatistics, company_dir: str) -> List[Dict[str, Any]]:
        """Обработка страниц с промежуточными чекпоинтами"""
        structured_products = []
        processed_count = 0
        
        # Разбиваем на батчи для чекпоинтов
        batch_size = self.config.checkpoint_frequency_pages
        
        for i in range(0, len(stored_pages), batch_size):
            batch = stored_pages[i:i + batch_size]
            log.info(f"Обработка батча {i//batch_size + 1}/{(len(stored_pages)-1)//batch_size + 1}, размер: {len(batch)}")
            
            # Параллельная обработка страниц в батче
            tasks = []
            for page in batch:
                task = self._process_single_page_with_checkpoint(page, company_data, company_stats, company_dir)
                tasks.append(task)
            
            # Ожидаем завершения всех задач в батче
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Обрабатываем результаты батча
            batch_products = []
            for result in batch_results:
                if isinstance(result, Exception):
                    log.error(f"Ошибка обработки страницы в батче: {result}")
                    company_stats.products_failed += 1
                elif result is not None:
                    batch_products.append(result)
            
            structured_products.extend(batch_products)
            
            # ЧЕКПОИНТ для батча
            if batch_products or (i + batch_size) >= len(stored_pages):
                await self.checkpoint_manager.save_checkpoint(
                    company_data=company_data,
                    processed_urls=self._global_processed_urls,
                    stage=f"processing_batch_{i//batch_size}",
                    progress={
                        "status": "processing_batch",
                        "total_pages": len(stored_pages),
                        "processed_pages": min(i + batch_size, len(stored_pages)),
                        "current_batch": i // batch_size,
                        "total_batches": (len(stored_pages) + batch_size - 1) // batch_size,
                        "products_in_batch": len(batch_products),
                        "total_products": len(structured_products)
                    },
                    company_stats=company_stats.to_dict()
                )
        
        return structured_products
    
    async def _process_single_page_with_checkpoint(self, page: Dict, company_data: Dict[str, str], 
                                             company_stats: CompanyStatistics, company_dir: str) -> Optional[Dict[str, Any]]:
        """Обработка одной страницы с атомарными проверками для предотвращения дублирования"""
        normalized_url = page.get('normalized_url', '')
        
        if not normalized_url:
            log.warning(f"Страница без normalized_url: {page.get('url', 'unknown')}")
            return None
        
        log.debug(f"Начало обработки URL: {normalized_url}")
        log.debug(f"Текущие URL в обработке: {len(self._processing_urls)}")
        log.debug(f"Обработанные URL: {len(self._global_processed_urls)}")
        
        # Атомарная проверка URL для предотвращения гонок
        async with self._processing_urls_lock:
            if normalized_url in self._global_processed_urls:
                log.debug(f"URL уже обработан (глобально): {normalized_url}")
                company_stats.urls_skipped_duplicate += 1
                self.statistics.total_urls_skipped_duplicate += 1 
                return None
            if normalized_url in self._processing_urls:
                log.debug(f"URL уже в обработке: {normalized_url}")
                company_stats.urls_skipped_duplicate += 1
                self.statistics.total_urls_skipped_duplicate += 1
                return None
            self._processing_urls.add(normalized_url)
        
        try:
            # Проверяем в хранилище
            is_processed = await self.crawler.temp_storage.is_page_processed(
                normalized_url, company_data['original_name'], page['category']
            )
            if is_processed:
                log.info(f"Страница уже обработана в хранилище, пропускаем: {normalized_url}")
                return None

            # Загружаем HTML из временного хранилища
            storage_result = await self.crawler.temp_storage.load_cleaned_html(page['file_path'])
            if not storage_result:
                return None
            
            html_content = storage_result['html_content']
            metadata = storage_result['metadata']
            
            # Обработка продуктовых страниц
            if page['category'] == 'product':
                product_data = await self.process_product_page(html_content, company_data, company_stats, metadata, company_dir)
                if product_data:
                    # Успешная обработка - добавляем URL в глобальный кэш
                    async with self._processing_urls_lock:
                        self._global_processed_urls.add(normalized_url)
                        self._processing_urls.remove(normalized_url)
                    
                    log.debug(f"Успешно обработан URL: {normalized_url}")
                    return product_data
                else:
                    # Если обработка не удалась, все равно помечаем как обработанную
                    async with self._processing_urls_lock:
                        self._processing_urls.remove(normalized_url)
                    
                    log.warning(f"Не удалось обработать продукт для URL: {normalized_url}")
                    return None
            
            # Для других типов страниц просто помечаем как обработанные
            async with self._processing_urls_lock:
                self._global_processed_urls.add(normalized_url)
                self._processing_urls.remove(normalized_url)
            
            return None
                
        except Exception as e:
            log.error(f"Ошибка обработки страницы {page.get('url')}: {e}")
            # При ошибке очищаем флаг обработки
            async with self._processing_urls_lock:
                self._processing_urls.discard(normalized_url)
            return None

    # === Потоковая обработка страниц (конвейер внутри компании) ===

    def _actual_name_cache_path(self, company_data: Dict[str, str]) -> str:
        """Путь к кэшу актуального наименования компании (переживает прогоны)"""
        return os.path.join(self.config.base_dir, 'ActualNames', f"{company_data['company_id']}.json")

    def _load_cached_actual_name(self, company_data: Dict[str, str]) -> Optional[str]:
        """Читает actual_name, сохранённый в прошлом прогоне (None, если кэша нет)"""
        try:
            path = self._actual_name_cache_path(company_data)
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            name = (data.get('actual_name') or '').strip()
            return name or None
        except Exception as e:
            log.warning(f"Не удалось прочитать кэш actual_name для {company_data.get('original_name')}: {e}")
            return None

    def _save_actual_name_cache(self, company_data: Dict[str, str], actual_name: str) -> None:
        """Сохраняет actual_name для потоковой обработки следующего прогона"""
        try:
            if not actual_name or not actual_name.strip():
                return
            path = self._actual_name_cache_path(company_data)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({
                    'company_id': company_data['company_id'],
                    'original_name': company_data['original_name'],
                    'actual_name': actual_name.strip(),
                    'saved_at': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"Не удалось сохранить кэш actual_name для {company_data.get('original_name')}: {e}")

    def _setup_profiling(self, company_data: Dict[str, str]) -> None:
        """Профилирование: резолв профиля сайта + телеметрия прогона (метрики, census).
        Fail-open: любая ошибка телеметрии логируется и не влияет на пайплайн."""
        self._profile_metrics = None
        self._company_profile = None
        try:
            from site_profiles import get_resolver
            from site_profiles.profile_metrics import RunMetricsCollector
            from text_extractor import set_census_sink
            # В контейнере config.profiles_dir существует; вне его (dev) резолвер
            # возьмёт свой дефолт — mvp/profiles рядом с пакетом site_profiles.
            profiles_dir = (self.config.profiles_dir
                            if os.path.isdir(self.config.profiles_dir) else None)
            profile = get_resolver(profiles_dir).resolve(company_data['website'])
            domain = profile.domain
            if profile.is_default():
                log.info(f"Профиль сайта для {domain}: отсутствует — generic-поведение")
            else:
                self._company_profile = profile
                log.info(f"Профиль сайта: {domain} v{profile.profile_version} "
                         f"(source={profile.source}, tier={profile.extract.tier})")
            metrics = RunMetricsCollector(
                domain, company_data.get('original_name', ''),
                baseline=(profile.baseline if self._company_profile is not None else {}))
            if (self._company_profile is not None
                    and profile.extract.markdown.min_len is not None):
                metrics.min_len = profile.extract.markdown.min_len
            # Эффективные лимиты прогона — baseline сравним только при их совпадении
            limits = profile.crawl.limits
            metrics.limits = {
                'pages': limits.pages or self.config.max_pages_per_site,
                'product_pages': limits.product_pages or self.config.max_product_pages_per_site,
            }
            self._profile_metrics = metrics
            self.crawler.metrics_collector = metrics
            set_census_sink(metrics.sink)
        except Exception as e:
            log.warning(f"Профилирование: телеметрия не инициализирована: {e}")

    def _finalize_profiling(self, crawl_result, company_stats: 'CompanyStatistics') -> None:
        """Финализация телеметрии компании: JSON метрик (Base/profile_metrics/) и,
        в режиме переписи (config.census_enabled), черновик профиля в profiles_drafts/."""
        metrics = getattr(self, '_profile_metrics', None)
        if metrics is None:
            return
        try:
            from text_extractor import set_census_sink
            set_census_sink(None)
            self.crawler.metrics_collector = None
            if isinstance(crawl_result, dict):
                metrics.record_pages(crawl_result.get('stored_pages'))
            metrics.record_output(vars(company_stats))
            metrics.write(self.config.profile_metrics_dir)
            if getattr(self.config, 'census_enabled', False):
                from site_profiles.census_collector import CensusCollector
                CensusCollector(metrics, self._company_profile).write_draft(
                    self.config.profiles_drafts_dir)
        except Exception as e:
            log.warning(f"Профилирование: финализация телеметрии не удалась: {e}")
        finally:
            self._profile_metrics = None

    def _resolve_streaming_mode(self, company_data: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        """Гейт потоковой обработки: (use_streaming, cached_actual_name).
        Стриминг включается только при известном из прошлого прогона actual_name
        (иначе product_id нестабилен) и вне chelaz.ru (D85 требует полного списка
        страниц до извлечения)."""
        if not self.config.pipeline_streaming_enabled:
            return False, None
        cached_actual_name = self._load_cached_actual_name(company_data)
        # Профиль сайта: extract.streaming=false — принудительный батч
        # (декларативная замена хардкода chelaz.ru; хардкод ниже остаётся fallback'ом)
        profile = getattr(self, '_company_profile', None)
        if profile is not None and profile.extract.streaming is False:
            log.info(f"Потоковая обработка отключена профилем сайта {profile.domain}, батч-режим")
            return False, cached_actual_name
        site_domain = urlparse(company_data['website']).netloc.lower()
        if 'chelaz.ru' in site_domain:
            log.info("Потоковая обработка отключена для chelaz.ru (D85), батч-режим")
            return False, cached_actual_name
        if not cached_actual_name:
            log.info("Потоковая обработка отключена: нет кэша actual_name (первый прогон компании), батч-режим")
            return False, None
        return True, cached_actual_name

    async def _page_stream_worker(self, page_queue: asyncio.Queue, company_data: Dict[str, str],
                                  company_stats: CompanyStatistics, company_dir: str,
                                  out_products: List[Dict[str, Any]]) -> None:
        """Воркер конвейера: обрабатывает товарные страницы по мере их сохранения краулером.
        Непродуктовые страницы пропускает (их, как раньше, помечает финальный батч-проход).
        None в очереди — сигнал завершения."""
        while True:
            page = await page_queue.get()
            try:
                if page is None:
                    return
                if page.get('category') != 'product':
                    continue
                try:
                    product = await self._process_single_page_with_checkpoint(
                        page, company_data, company_stats, company_dir)
                except Exception as e:
                    log.error(f"Ошибка потоковой обработки страницы {page.get('url')}: {e}")
                    company_stats.products_failed += 1
                    continue
                if product is not None:
                    out_products.append(product)
                    if len(out_products) % self.config.checkpoint_frequency_pages == 0:
                        async with self._stream_ckpt_lock:
                            await self.checkpoint_manager.save_checkpoint(
                                company_data=company_data,
                                processed_urls=self._global_processed_urls,
                                stage="streaming_pages",
                                progress={
                                    "status": "streaming",
                                    "products_streamed": len(out_products)
                                },
                                company_stats=company_stats.to_dict()
                            )
            finally:
                page_queue.task_done()

    # Внутри класса MonitoringSystem, метод process_product_page

    async def process_product_page(self, html_content: str, company_data: Dict[str, str],
                                company_stats: CompanyStatistics, metadata: Dict, 
                                company_dir: str) -> Optional[Dict[str, Any]]:
        """Обработка продуктовой страницы с JSON-данными"""
        product_id = None
        # Флаг владения product_id в _processing_product_ids: снимать флаг в finally
        # имеет право только задача, которая его поставила (иначе задача-дубликат
        # стирает флаг у работающей задачи и та падает на remove() с KeyError)
        product_id_acquired = False
        slug_key = None            # D59 (C): ключ склейки версий товара по слагу
        slug_key_acquired = False  # владелец slug_key в _processing_slug_keys (как product_id)
        try:
            current_time = datetime.now().isoformat()
            normalized_url = metadata.get('normalized_url', '')

            # Имя производителя для извлечения: в потоковом режиме закреплено на весь прогон
            # (product_extraction_name из кэша actual_name прошлого прогона) — иначе товары,
            # обработанные во время краула, и товары «метлы» получили бы разные суффиксы
            # «(производитель)» в имени и, как следствие, разные product_id
            manufacturer_name = (company_data.get('product_extraction_name')
                                 or company_data.get('actual_name', company_data['original_name']))

            with self.processing_tracker.clean_span('text'):
                markdown_content = html_to_markdown(html_content, url=metadata.get('url', ''))
            await self._save_markdown_file(
                company_data['original_name'],
                'product',
                normalized_url,
                markdown_content
            )
            
            # Получаем базовый домен для формирования абсолютных ссылок
            base_domain = metadata.get('url', '')
            if base_domain:                
                parsed_url = urlparse(base_domain)
                base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"

            # Добавляем метаданные в промпт
            enhanced_text = f"""
            Производитель: {manufacturer_name}
            URL страницы: {metadata.get('url', '')}

            {markdown_content}                
            """
            
            # Получаем JSON данные от AI
            with self.processing_tracker.clean_span('ai'):
                ai_data, token_usage = await self.ai_client.extract_product_info(
                enhanced_text,
                manufacturer_name,
                base_domain
            )                                
            
            text_type = "product"
            is_product = False
            
            if ai_data is None:
                log.warning(f"Сетевая ошибка или нечитаемый ответ AI, страница будет повторена "
                            f"(не помечается обработанной): {metadata.get('url', '')}")
                company_stats.products_failed += 1
                return None
            
            # Проверяем наличие ошибки Trash_418#
            if isinstance(ai_data, dict) and ai_data.get("error") == "Trash_418#":
                log.info(f"AI вернул ошибку Trash_418#, сохраняем в Not_products: {metadata.get('url', '')}")
                text_type = "not_product"
                is_product = False
            # Гейт качества: товар идёт в карточку и БД ТОЛЬКО при наличии >=3 ЧИСЛОВЫХ
            # тех.характеристик (параметрический поиск завязан на числовые значения характеристик;
            # габариты AAA×BBB×CCC считаются по числу осей). Пустой specifications — тоже не товар.
            elif isinstance(ai_data, dict) and "product" in ai_data:
                product_data_ai = ai_data.get("product") or {}
                specifications = product_data_ai.get("specifications", {})
                numeric_specs = count_numeric_characteristics(product_data_ai)
                if specifications == {}:
                    log.info(f"Пустой specifications, сохраняем в Not_products: {metadata.get('url', '')}")
                    text_type = "not_product"
                    is_product = False
                elif numeric_specs < 3:
                    company_stats.products_below_min_specs += 1
                    log.info(f"Менее 3 числовых тех.характеристик (numeric_specs={numeric_specs}), "
                             f"сохраняем в Not_products: {metadata.get('url', '')}")
                    text_type = "not_product"
                    is_product = False
                else:
                    is_product = True
                # Телеметрия профилирования (W1): avg_specs_per_card. Fail-open внутри record_specs.
                metrics = getattr(self, '_profile_metrics', None)
                if metrics is not None:
                    metrics.record_specs(numeric_specs, is_product)
            else:
                # Неизвестный формат, сохраняем в Not_products
                log.warning(f"Неизвестный формат AI данных, сохраняем в Not_products: {metadata.get('url', '')}")
                text_type = "not_product"
                is_product = False
            
            # Обновляем статистику токенов
            company_stats.input_tokens += token_usage['input_tokens']
            company_stats.output_tokens += token_usage['output_tokens']
            self.statistics.total_input_tokens += token_usage['input_tokens']
            self.statistics.total_output_tokens += token_usage['output_tokens']                
            
            # Сохраняем AI данные (даже для not_product)
            ai_text_path = await self.crawler.temp_storage.save_ai_text(
                company_name=company_data['original_name'],
                url=metadata.get('url', ''),
                normalized_url=metadata.get('normalized_url', ''),
                ai_text=ai_data,
                text_type=text_type,
                metadata={
                    'company_id': company_data['company_id'],
                    'input_tokens': token_usage['input_tokens'],
                    'output_tokens': token_usage['output_tokens'],
                    'source_timestamp': metadata.get('timestamp'),
                    'processing_timestamp': current_time,
                    'is_product': is_product,
                    'actual_company_name': manufacturer_name
                }
            )                    
            
            # Если не товар, возвращаем None
            if not is_product:
                log.info(f"Страница не является товаром, сохранена в 'not_product': {metadata.get('url', '')}")
                await self.crawler.temp_storage.mark_as_ai_processed(
                    metadata.get('storage_path', ''), 
                    ai_text_path
                )
                return None
            
            log.info(f"Страница является товаром, обрабатываем: {metadata.get('url', '')}")                  
            
            # Извлекаем название продукта из JSON данных
            product_name = (ai_data.get('product') or {}).get('product_name', '')
            if not product_name or product_name == "Неизвестный товар":
                # Резервный метод извлечения имени (но если нет - пропускаем)
                product_name = extract_product_name_from_ai_text(str(ai_data))
                if not product_name or product_name == "Неизвестный товар":
                    log.warning(f"Не удалось извлечь название товара для URL {metadata.get('url', '')}, пропускаем")
                    await self.crawler.temp_storage.mark_as_ai_processed(
                        metadata.get('storage_path', ''), 
                        ai_text_path
                    )
                    company_stats.products_failed += 1
                    return None
            
            # Слова-исключения (N7): товары, названия которых заказчик просил не собирать.
            excl_patterns = getattr(self.config, 'excluded_product_name_patterns', None) or []
            if is_excluded_product_name(product_name, excl_patterns):
                log.info(f"Товар в списке исключений ('{product_name}'), пропускаем: {metadata.get('url','')}")
                company_stats.products_skipped_excluded += 1
                await self.crawler.temp_storage.mark_as_ai_processed(
                    metadata.get('storage_path', ''), ai_text_path)
                return None

            # ГЕНЕРАЦИЯ PRODUCT_ID ПО «МЕШКУ СЛОВ» ИМЕНИ (стабилен к перестановке слов/кавычкам между прогонами)
            try:
                product_id = self.product_id_manager.generate_product_id_from_name_bag(
                    product_name,
                    company_data['original_name']   # используем original_name из Excel
                )
            except ValueError as e:
                log.error(f"Ошибка генерации product_id для имени '{product_name}': {e}, пропускаем товар")
                await self.crawler.temp_storage.mark_as_ai_processed(
                    metadata.get('storage_path', ''), 
                    ai_text_path
                )
                company_stats.products_failed += 1
                return None
            
            # АТОМАРНАЯ проверка дубликатов product_id ПОСЛЕ генерации
            async with self._processing_product_ids_lock:
                if product_id in self._global_processed_product_ids:
                    log.info(f"Товар уже обработан (глобально), пропускаем: {product_id}")
                    company_stats.products_skipped_duplicate += 1
                    self.statistics.total_products_skipped_duplicate += 1
                    await self.crawler.temp_storage.mark_as_ai_processed(
                        metadata.get('storage_path', ''), 
                        ai_text_path
                    )
                    return None
                if product_id in self._processing_product_ids:
                    log.debug(f"Товар уже в обработке: {product_id}")
                    company_stats.products_skipped_duplicate += 1
                    self.statistics.total_products_skipped_duplicate += 1
                    await self.crawler.temp_storage.mark_as_ai_processed(
                        metadata.get('storage_path', ''),
                        ai_text_path
                    )
                    return None
                # D59 (C): кросс-доменные/путь-версии одного товара дают РАЗНЫЕ имена (значит
                # разные product_id — проверки выше их не ловят). Склеиваем по слагу. Точка
                # достигается только для УСПЕШНО извлечённого товара (Trash_418#/soft-404
                # возвращаются раньше), поэтому провалившаяся версия ключ не занимает и не
                # блокирует рабочую версию со вторичного домена холдинга.
                slug_key = self.crawler._product_dedup_key(metadata.get('url', '')) if self.crawler else None
                if slug_key and (slug_key in self._global_processed_slug_keys
                                 or slug_key in self._processing_slug_keys):
                    log.info(f"D59 (C): дубль товара по слагу ({slug_key}), пропускаем URL: {metadata.get('url','')}")
                    company_stats.products_skipped_duplicate += 1
                    self.statistics.total_products_skipped_duplicate += 1
                    await self.crawler.temp_storage.mark_as_ai_processed(
                        metadata.get('storage_path', ''),
                        ai_text_path
                    )
                    return None
                self._processing_product_ids.add(product_id)
                product_id_acquired = True
                if slug_key:
                    self._processing_slug_keys.add(slug_key)
                    slug_key_acquired = True

            try:
                # Извлекаем ссылки на изображения из JSON
                image_links = []
                images_data = (ai_data.get('product') or {}).get('images', [])
                if images_data:
                    image_links = [(img, '') for img in images_data]
                
                downloaded_images_from_ai = []
                if image_links and product_name and product_name != "Неизвестный товар":                    
                    images_to_download = image_links[:20]            
                    log.info(f"Найдено {len(image_links)} изображений в AI данных, скачиваем {len(images_to_download)}")

                    product_folder_abs, product_folder_rel = self._get_product_folder_path(company_dir, product_id, product_name)

                    downloaded_images_from_ai = await self._download_images_from_ai_text(
                        images_to_download, product_name, company_data, product_folder_abs, product_id)
                else:
                    log.info(f"Пропускаем скачивание изображений: image_links={len(image_links)}, product_name={product_name}")

                downloaded_files_with_ids = []
                if product_name and product_name != "Неизвестный товар":
                    if 'product_folder_abs' not in locals():
                        product_folder_abs, product_folder_rel = self._get_product_folder_path(company_dir, product_id, product_name)
                    
                    downloaded_files = await self._download_product_files(
                        metadata.get('url', ''), product_name, company_data, product_folder_abs, company_dir, product_id, company_stats=company_stats,
                        storage_path=metadata.get('storage_path', ''))
                    downloaded_files_with_ids = downloaded_files

                if 'product_folder_rel' not in locals():
                    _, product_folder_rel = self._get_product_folder_path(company_dir, product_id, product_name)
                manufacturer = manufacturer_name
                product_data = {
                    'product_id': product_id, 
                    'name': product_name,                       
                    'ai_raw_text': ai_data, 
                    'source_url': metadata.get('url', ''),
                    'product_folder': product_folder_rel,  
                    'manufacturer': manufacturer, 
                    'original_manufacturer': company_data['original_name'],
                    'timestamp': current_time,
                    'metadata': metadata,
                    'downloaded_files': downloaded_files_with_ids, 
                    'images': downloaded_images_from_ai,
                    'file_ids': [f.get('id') for f in downloaded_files_with_ids],
                    'files': downloaded_files_with_ids
                }

                # Добавляем в глобальный кэш product_id
                async with self._processing_product_ids_lock:
                    self._global_processed_product_ids.add(product_id)
                    self._processing_product_ids.remove(product_id)
                    # D59 (C): фиксируем слаг как обработанный ТОЛЬКО здесь (успех), чтобы
                    # следующие URL-версии того же товара схлопнулись в одну карточку.
                    if slug_key_acquired:
                        self._global_processed_slug_keys.add(slug_key)
                        self._processing_slug_keys.discard(slug_key)
                        slug_key_acquired = False

                # Помечаем страницу как обработанную
                await self.crawler.temp_storage.mark_as_ai_processed(
                    metadata.get('storage_path', ''), 
                    ai_text_path
                )

                # Генерируем карточку товара                
                try: 
                    if 'product_folder_abs' not in locals():
                        product_folder_abs, _ = self._get_product_folder_path(company_dir, product_id, product_name)
                                        
                    with self.processing_tracker.clean_span('cards'):
                        card_path = await self.docx_generator.generate_product_card(product_data, company_data, product_folder_abs)
                    if card_path:
                        company_stats.cards_generated += 1
                except Exception as e:
                    log.error(f"Ошибка генерации карточки товара: {e}")
                    # Продолжаем обработку даже при ошибке генерации карточки
            
                company_stats.products_processed += 1
                return product_data
                
            finally:
                # В любом случае удаляем product_id из processing
                async with self._processing_product_ids_lock:
                    self._processing_product_ids.discard(product_id)

        except Exception as e:
            log.error(f"Ошибка обработки продуктовой страницы: {e}")
            company_stats.products_failed += 1
            return None

        finally:
            # Очищаем флаг обработки ТОЛЬКО если его поставила эта задача (владелец);
            # задача-дубликат флаг не ставила и снимать его не должна
            if product_id and product_id_acquired:
                async with self._processing_product_ids_lock:
                    self._processing_product_ids.discard(product_id)
            # D59 (C): если слаг остался в processing (провал ДО фиксации в global) —
            # снимаем, чтобы другая URL-версия товара могла быть обработана (не терять товар).
            if slug_key_acquired:
                async with self._processing_product_ids_lock:
                    self._processing_slug_keys.discard(slug_key)

    async def load_existing_ids_from_index(self):
        """Загрузка существующих ID из векторной БД"""
        if not self.index_manager:
            return
        try:
            # Получаем все существующие товары из индекса
            all_products = await self.index_manager.get_all_products_metadata()
            
            product_ids = set()
            file_ids = set()
            distributor_ids = set()
            
            for product_meta in all_products:
                # Извлекаем product_id
                if 'product_id' in product_meta:
                    product_ids.add(product_meta['product_id'])
                
                # Извлекаем file_ids из метаданных товара
                if 'file_ids' in product_meta:
                    file_ids.update(product_meta['file_ids'])

                # Извлекаем distributor_id если это дистрибьютор
                if 'distributor_id' in product_meta and product_meta.get('document_type') == 'distributor':
                    distributor_ids.add(product_meta['distributor_id'])
            
            # Загружаем в менеджеры
            self.product_id_manager.load_existing_ids(product_ids)
            self.file_id_manager.load_existing_ids(file_ids)
            self.distributor_id_manager.load_existing_ids(distributor_ids)
            
            # Обновляем глобальные кэши
            self._global_processed_product_ids.update(product_ids)
            self._global_processed_file_ids.update(file_ids)
            self._global_processed_distributor_ids.update(distributor_ids)
            
            log.info(f"Загружено {len(product_ids)} product_id, {len(file_ids)} file_id и {len(distributor_ids)} distributor_id из индекса")
            
        except Exception as e:
            log.error(f"Ошибка загрузки существующих ID: {e}")    

    async def _save_markdown_file(self, company_name: str, category: str, normalized_url: str, markdown: str) -> str:
        """Сохраняет Markdown файл в структуру temp_html_storage/Markdown/<category>_pages/"""
        base_dir = self.crawler.temp_storage.base_dir
        company_dir = os.path.join(base_dir, sanitize_filename(company_name))
        markdown_base_dir = os.path.join(company_dir, "Markdown")

        # Определяем подпапку в зависимости от категории
        if category == 'product':
            subdir = os.path.join(markdown_base_dir, "Product_pages")
        elif category == 'distributor':
            subdir = os.path.join(markdown_base_dir, "Distributor_pages")
        elif category in ['company', 'contacts', 'main_page']:
            subdir = os.path.join(markdown_base_dir, "Company_pages")
        else:
            subdir = os.path.join(markdown_base_dir, "Other_pages")

        os.makedirs(subdir, exist_ok=True)

        safe_name = sanitize_filename(normalized_url)
        if len(safe_name) > 200:
            safe_name = safe_name[:200]
        file_name = f"{safe_name}.md"
        file_path = os.path.join(subdir, file_name)

        # Если файл уже существует, добавляем суффикс
        counter = 1
        original_path = file_path
        while os.path.exists(file_path):
            name, ext = os.path.splitext(original_path)
            file_path = f"{name}_{counter}{ext}"
            counter += 1

        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(markdown)

        log.debug(f"Сохранен Markdown файл: {file_path}")
        return file_path

    async def _download_images_from_ai_text(self, image_links: List[Tuple[str, str]], product_name: str, 
                  company_data: Dict[str, str], product_folder: str, product_id: str) -> List[str]:
        """Скачивание изображений из ссылок, извлеченных из AI текста"""
        try:            
            if isinstance(product_folder, tuple):
                log.warning(f"product_folder передан как кортеж в _download_images_from_ai_text, берем первый элемент")
                product_folder = product_folder[0]
            
            # Создаем подпапки только при необходимости
            dir_structure = self.directory_manager.ensure_product_subdirectories(product_folder)
            product_images_dir = dir_structure['images_dir']
            
            downloaded_images = []
            
            for image_url, description in image_links:
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        image_path = await self.crawler.file_download_manager.download_image_with_size_check(
                    image_url, product_images_dir, 'ai_image'
                )
                        if image_path:
                            downloaded_images.append(image_path)
                            log.info(f"Скачано изображение из AI текста: {image_path} (описание: {description})")
                            break  
                        else:
                            log.warning(f"Не удалось скачать изображение {image_url} (попытка {attempt + 1}/{max_retries})")
                    except Exception as e:
                        log.warning(f"Ошибка скачивания изображения {image_url} (попытка {attempt + 1}/{max_retries}): {e}")
                        if attempt == max_retries - 1:  
                            log.error(f"Не удалось скачать изображение {image_url} после {max_retries} попыток")
                        else:
                            await asyncio.sleep(1) 
                        continue
            
            log.info(f"Скачано {len(downloaded_images)} изображений из AI текста для товара {product_name}")
            return downloaded_images
            
        except Exception as e:
            log.error(f"Ошибка скачивания изображений из AI текста: {e}")
            return []

    async def _download_product_files(self, page_url: str, product_name: str,
              company_data: Dict[str, str], product_folder: str, company_dir: str,
              product_id: str, company_stats=None, storage_path: str = '') -> List[Dict[str, Any]]:
        """Скачивание файлов из ссылок, извлеченных из AI текста"""
        try:            
            if isinstance(product_folder, tuple):
                log.warning(f"product_folder передан как кортеж в _download_product_files, берем первый элемент")
                product_folder = product_folder[0]
            
            # Создаем подпапки только при необходимости
            dir_structure = self.directory_manager.ensure_product_subdirectories(product_folder)
            product_files_dir = dir_structure['documents_dir']
            
            downloaded_files = []

            # D72: ссылки на файлы извлекаем из уже сохранённого при обходе HTML. Повторная
            # загрузка страницы на медленных/антибот-сайтах (pspcom.ru) таймаутила в Playwright
            # ДО извлечения ссылок => files_found=0. extract_file_links_from_page сам делает
            # фолбэк на сетевую загрузку, если сохранённого HTML нет или в нём нет файловых ссылок.
            file_urls = await self.crawler.extract_file_links_from_page(page_url, storage_path=storage_path)
            
            # Фильтруем только ссылки на файлы
            file_links = []
            for file_url in file_urls:
                if self.crawler.file_download_manager.is_downloadable_file(file_url):
                    file_links.append(file_url)
            
            # Скачиваем файлы
            dl_ok = 0
            dl_fail = 0
            for file_url in file_links[:30]:  # Ограничиваем количество файлов
                file_handled = False
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        file_path = await self.crawler.file_download_manager._download_aiohttp(
                            file_url, product_files_dir, 'product_page_file'
                        )
                        if file_path and os.path.exists(file_path):
                            file_info = await self.file_id_manager.get_file_info(file_path, product_id)                            
                            
                            if not file_info.get('exists', False):
                                log.warning(f"Не удалось получить информацию о файле: {file_path}")
                                continue
                            
                            file_id = file_info['id']
                            
                            # Проверяем дубликаты
                            if file_id in self._global_processed_file_ids:
                                # Используем кэшированную информацию, если есть
                                if file_id in self._file_info_cache:
                                    cached_info = self._file_info_cache[file_id].copy()
                                    # Форматируем путь для Graph DB
                                    cached_info['path'] = self.file_id_manager.get_relative_path(
                                        cached_info['path'], company_dir
                                    )
                                    downloaded_files.append(cached_info)
                                    log.info(f"Файл уже обработан, используем кэш: {file_id}")
                                else:
                                    log.info(f"Файл уже обработан, но нет в кэше: {file_id}")
                                file_handled = True
                                continue
                            
                            # Форматируем путь для Graph DB (относительно папки компании)
                            file_info['path'] = os.path.relpath(file_info['path'], self.config.documents_dir)
                            
                            # Удаляем служебные поля
                            if 'exists' in file_info:
                                del file_info['exists']
                            if 'error' in file_info:
                                del file_info['error']
                            
                            # Кэшируем информацию о файле
                            self._file_info_cache[file_id] = file_info.copy()
                            
                            # Сохраняем в список скачанных файлов
                            downloaded_files.append(file_info)
                            
                            # Добавляем в глобальные кэши
                            self._global_processed_file_ids.add(file_id)
                            dl_ok += 1
                            file_handled = True
                            
                            log.info(f"Скачан файл с продуктовой страницы: {file_path}")
                            break
                        else:
                            log.warning(f"Не удалось скачать файл {file_url} (попытка {attempt + 1}/{max_retries})")
                    except Exception as e:
                        log.warning(f"Ошибка скачивания файла {file_url} (попытка {attempt + 1}/{max_retries}): {e}")
                        if attempt == max_retries - 1:
                            log.error(f"Не удалось скачать файл {file_url} после {max_retries} попыток")
                        else:
                            await asyncio.sleep(1)
                        continue
            
                if not file_handled:
                    dl_fail += 1
            if company_stats is not None:
                company_stats.files_downloaded += dl_ok
                company_stats.files_failed += dl_fail
                company_stats.files_found += dl_ok + dl_fail
                self.statistics.total_files_downloaded += dl_ok
                self.statistics.total_files_failed += dl_fail
                self.statistics.total_files_found += dl_ok + dl_fail
            log.info(f"Скачано {len(downloaded_files)} файлов с продуктовой страницы для товара {product_name}")
            return downloaded_files
            
        except Exception as e:
            log.error(f"Ошибка скачивания файлов с продуктовой страницы: {e}")
            return []
        
    def _get_product_folder_path(self, company_dir: str, product_id: str, product_name: str) -> Tuple[str, str]:
        """Получение пути к папке продукта без создания подпапок"""
        safe_product_name = sanitize_filename(product_name)
        if safe_product_name == "null" or not safe_product_name:
            safe_product_name = "unknown_product"
        
        # Формируем папку в формате: ID_Наименование_товара
        folder_name = safe_product_name
        product_folder = os.path.join(company_dir, "Products", folder_name)
        product_folder_rel = os.path.relpath(product_folder, self.config.documents_dir)
        
        # Создаем только основную папку товара
        if not os.path.exists(product_folder):
            os.makedirs(product_folder, exist_ok=True)
            log.info(f"Создана папка товара: {product_folder}")
        
        return product_folder, product_folder_rel
        
    def _get_domain_dirs(self, url: str, company_data: Dict[str, str], company_dir: str) -> Dict[str, str]:
        """Получение директорий для домена"""
        return self.crawler.create_site_directories(url, company_data['original_name'], company_dir)    
    
    async def save_company_info(self, company_info_pages: List[Dict], company_data: Dict[str, str], company_dir: str):
        """Сохранение информации о компании"""
        try:
            company_info_dir = os.path.join(company_dir, "Company_Info")
            os.makedirs(company_info_dir, exist_ok=True)
            
            for info in company_info_pages:
                safe_filename = sanitize_company_name(f"company_info_{info['category']}_{hash(info['source_url'])}.txt")
                file_path = os.path.join(company_info_dir, safe_filename)
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"URL: {info['source_url']}\n")
                    f.write(f"Категория: {info['category']}\n")
                    f.write(f"Дата обработки: {info['timestamp']}\n")
                    f.write("\n" + "="*50 + "\n")
                    f.write(info['ai_raw_text'])
            
            log.info(f"Сохранена информация о компании: {len(company_info_pages)} файлов")
            
        except Exception as e:
            log.error(f"Ошибка сохранения информации о компании: {e}")
    
    async def add_products_to_index(self, structured_products: List[Dict[str, Any]], 
                                                     company_data: Dict[str, str], 
                                                     company_stats: CompanyStatistics):
        """Добавление товаров в индекс (без генерации карточек)"""
        if not structured_products:
            return
        
        # Векторная БД / эмбеддинги упразднены (config.vector_db_enable=False), стадия отключена
        if not self.index_manager:
            return

        # Добавляем в векторный индекс
        embedding_tokens = await self.index_manager.add_products_to_index(
            structured_products, company_data['company_id']
        )
        company_stats.embedding_tokens += embedding_tokens
        self.statistics.total_embedding_tokens += embedding_tokens
        company_stats.products_added_to_db = len(structured_products)
        
        log.info(f"Добавлено {len(structured_products)} товаров в базу данных")

    async def process_distributor_pages(self, company_data: Dict[str, str], company_dir: str, 
                                  company_stats: CompanyStatistics) -> List[Dict[str, Any]]:
        log.info(f"Начинаем обработку страниц дистрибьюторов для компании: {company_data['original_name']}")
    
        try:
            # Получаем все страницы дистрибьюторов из временного хранилища
            distributor_pages = await self.crawler.temp_storage.get_company_files(
                company_data['original_name'], 
                category='distributor'
            )
            
            if not distributor_pages:
                log.info(f"Не найдено страниц дистрибьюторов для {company_data['original_name']}")
                return []
            
            log.info(f"Найдено {len(distributor_pages)} страниц дистрибьюторов")
            
            # Объединяем HTML контент всех страниц дистрибьюторов
            combined_markdown = await self._combine_distributor_content(company_data['original_name'], distributor_pages)
            
            if not combined_markdown:
                log.error("Не удалось объединить контент дистрибьюторов")
                return []
            
            # Отправляем объединенный HTML в AI для обработки
            log.info("Отправка объединенного HTML дистрибьюторов в AI для обработки")
            ai_distributors_data, token_usage = await self.ai_client.extract_distributor_info(
                    combined_markdown, company_data['company_id'], company_data['actual_name'],
                    company_dir=company_dir
            )
            
            if not ai_distributors_data:
                log.error("Не удалось получить информацию о дистрибьюторах через AI")
                return []
            
            # Обновляем статистику токенов
            company_stats.input_tokens += token_usage['input_tokens']
            company_stats.output_tokens += token_usage['output_tokens']
            self.statistics.total_input_tokens += token_usage['input_tokens']
            self.statistics.total_output_tokens += token_usage['output_tokens']
            
            # Сохраняем AI текст дистрибьюторов
            await self.crawler.temp_storage.save_ai_text(
                company_name=company_data['original_name'],
                url="distributor_combined_content",
                normalized_url="distributor_combined_content",
                ai_text=ai_distributors_data,
                text_type="distributor_info",
                metadata={
                    'company_id': company_data['company_id'],
                    'input_tokens': token_usage['input_tokens'],
                    'output_tokens': token_usage['output_tokens'],
                    'total_pages_processed': len(distributor_pages),
                    'processing_timestamp': datetime.now().isoformat()
                }
            )
            
            # Парсим структурированные данные дистрибьюторов из AI данных
            structured_distributors = self._parse_distributors_from_ai_data(ai_distributors_data, company_data)
            
            if not structured_distributors:
                log.warning("Не удалось извлечь структурированные данные дистрибьюторов из AI данных")
                return []
            
            # Добавляем дистрибьюторов в индекс (если векторная БД включена)
            embedding_tokens = 0
            if self.index_manager:
                log.info(f"Добавление {len(structured_distributors)} дистрибьюторов в векторную БД")
                embedding_tokens = await self.index_manager.add_distributors_to_index(
                    structured_distributors, company_data['company_id']
                )
            
            # Обновляем статистику
            company_stats.embedding_tokens += embedding_tokens
            self.statistics.total_embedding_tokens += embedding_tokens
            company_stats.distributors_found = len(structured_distributors)
            company_stats.distributors_processed = len(structured_distributors)
            if self.index_manager:
                company_stats.distributors_added_to_db = len(structured_distributors)
            
            log.info(f"Успешно обработано {len(structured_distributors)} дистрибьюторов")
            return structured_distributors
            
        except Exception as e:
            log.error(f"Ошибка обработки страниц дистрибьюторов: {e}")
            log.error(traceback.format_exc())
            return []

    async def _combine_distributor_content(self, company_name: str, distributor_pages: List[Dict[str, Any]]) -> str:
        """Объединение контента страниц дистрибьюторов с конвертацией в Markdown"""
        combined_content = []
        for page in distributor_pages:
            try:
                storage_result = await self.crawler.temp_storage.load_cleaned_html(page['html_path'])
                if storage_result and storage_result['html_content']:
                    metadata = storage_result['metadata']
                    url = metadata.get('url', 'unknown_url')
                    normalized_url = metadata.get('normalized_url', '')
                    html = storage_result['html_content']
                    markdown = html_to_markdown(html, url=url, page_type='distributor')
                    # Сохраняем Markdown
                    await self._save_markdown_file(company_name, 'distributor', normalized_url, markdown)

                    file_header = f"\n<!-- === Начало файла: {url} === -->\n"
                    file_footer = f"\n<!-- === Конец файла: {url} === -->\n"
                    combined_content.append(file_header + markdown + file_footer)
            except Exception as e:
                log.warning(f"Ошибка загрузки страницы дистрибьютора {page.get('html_path', '')}: {e}")
                continue
        return "\n\n".join(combined_content)

    @staticmethod
    def _dedup_distributors_by_name(suppliers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Схлопывает дубли дилеров по НАИМЕНОВАНИЮ, оставляя запись с самым ПОЛНЫМ
        адресом. Причина: региональные версии страницы (напр. Белгород/Курск у
        aerobel.ru) отдают одного дилера дважды с разной полнотой адреса
        («Россия, Белгородская область, Ракитянский р-н…» vs «Ракитянский р-н…»),
        а generate_distributor_id(имя+адрес) даёт разные id → дубль в графе (7→14).
        Полнота адреса = длина строки. Недостающие контакты (телефон/почта/URL/
        график/регион) добираем из отброшенного дубля, чтобы не терять данные."""
        by_name: Dict[str, Dict[str, Any]] = {}
        fill_fields = ('Телефон', 'Эл. почта', 'URL', 'URL страницы', 'График работы', 'Регион')
        for s in suppliers:
            if not (isinstance(s, dict) and (s.get('Наименование') or '').strip()):
                continue
            key = ' '.join(s['Наименование'].split()).lower()
            prev = by_name.get(key)
            if prev is None:
                by_name[key] = dict(s)
                continue
            cur_addr = (s.get('Адрес') or '').strip()
            prev_addr = (prev.get('Адрес') or '').strip()
            base, extra = (dict(s), prev) if len(cur_addr) > len(prev_addr) else (prev, s)
            for f in fill_fields:
                if not (base.get(f) or '').strip() and (extra.get(f) or '').strip():
                    base[f] = extra[f]
            by_name[key] = base
        return list(by_name.values())

    def _parse_distributors_from_ai_data(self, ai_data, company_data: Dict[str, str]) -> List[Dict[str, Any]]:
        """Парсинг структурированных данных дистрибьюторов из AI данных"""
        distributors = []

        try:
            # Обрабатываем разные форматы входных данных
            data = None
            if isinstance(ai_data, str):
                try:
                    data = json.loads(ai_data)
                except json.JSONDecodeError:
                    log.error(f"Ошибка парсинга JSON строки: {ai_data[:200]}...")
                    return []
            elif isinstance(ai_data, dict):
                data = ai_data
            else:
                log.error(f"Неожиданный тип данных для дистрибьюторов: {type(ai_data)}")
                return []
            
            if isinstance(data, dict) and 'suppliers' in data:
                # Схлопываем регион-дубли одного дилера (7→14 у aerobel), оставляя
                # самый полный адрес и добирая недостающие контакты.
                suppliers = self._dedup_distributors_by_name(data['suppliers'])
                for supplier in suppliers:
                    if isinstance(supplier, dict) and supplier.get('Наименование'):
                        # Генерируем ID дистрибьютора
                        distributor_id = self.distributor_id_manager.generate_distributor_id(
                            supplier['Наименование'],
                            supplier.get('Адрес', '')
                        )
                        
                        # Проверяем дубликаты
                        if distributor_id not in self._global_processed_distributor_ids:
                            manufacturer = company_data.get('actual_name', company_data['original_name'])
                            distributor_data = {
                                'distributor_id': distributor_id,
                                'name': supplier.get('Наименование', ''),
                                'region': supplier.get('Регион', ''),
                                'address': supplier.get('Адрес', ''),
                                'phone': supplier.get('Телефон', ''),
                                'business_hours': supplier.get('График работы', ''),
                                'email': supplier.get('Эл. почта', ''),
                                'url': supplier.get('URL', ''),
                                'page_url': supplier.get('URL страницы'),
                                'manufacturer': manufacturer,
                                'original_manufacturer': company_data['original_name'],
                                'company_id': company_data['company_id'],
                                'timestamp': datetime.now().isoformat()
                            }
                            distributors.append(distributor_data)
                            self._global_processed_distributor_ids.add(distributor_id)
            
            log.info(f"Извлечено {len(distributors)} дистрибьюторов из AI данных")
            
        except json.JSONDecodeError as e:
            log.error(f"Ошибка парсинга JSON дистрибьюторов: {e}")
        except Exception as e:
            log.error(f"Ошибка обработки данных дистрибьюторов: {e}")
        
        return distributors    

    async def _trim_global_url_cache(self):
        """Очистка глобального кэша URL для экономии памяти.
        Вызывать после завершения обработки компании, когда чекпоинт уже неактуален.
        """
        MAX_GLOBAL_URLS = 20000   # храним не более 20k URL
        if len(self._global_processed_urls) > MAX_GLOBAL_URLS:
            # Превращаем set в список, берём последние MAX_GLOBAL_URLS
            as_list = list(self._global_processed_urls)
            self._global_processed_urls = set(as_list[-MAX_GLOBAL_URLS:])
            log.info(f"Очищен _global_processed_urls: было {len(as_list)}, стало {len(self._global_processed_urls)}")

    async def _mark_company_for_graph_db_retry(self, company_data: Dict[str, str], error_msg: str):
        """Помечает компанию как требующую повторной отправки в Graph DB."""
        pending_file = os.path.join(self.config.graph_db_pending_dir, "pending_graph_db_upload.json")
        try:
            pending = []
            if os.path.exists(pending_file):
                async with aiofiles.open(pending_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content:
                        pending = json.loads(content)
            
            # Проверяем, нет ли уже записи
            existing = next((item for item in pending if item['company_id'] == company_data['company_id']), None)
            if not existing:
                pending.append({
                    'company_id': company_data['company_id'],
                    'company_name': company_data['original_name'],
                    'company_data': company_data,
                    'error': error_msg,
                    'timestamp': datetime.now().isoformat(),
                    'retry_count': 0
                })
            else:
                existing['retry_count'] += 1
                existing['error'] = error_msg
                existing['timestamp'] = datetime.now().isoformat()
            
            async with aiofiles.open(pending_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(pending, ensure_ascii=False, indent=2))
            
            log.info(f"Компания {company_data['original_name']} добавлена в список ожидающих повторной отправки в Graph DB")
        except Exception as e:
            log.error(f"Ошибка при сохранении компании в pending список: {e}")

    async def process_company(self, company_data: Dict[str, str], force_restart: bool = False) -> Dict[str, Any]:
        log.info(f"Обработка компании: {company_data['original_name']}")
        
        # # Определяем домен для профиля
        # parsed = urlparse(company_data['website'])
        # domain = parsed.netloc

        # # Загружаем или создаём профиль
        # profile = self.profiler.load_profile(domain)
        # if not profile or force_restart:
        #     log.info(f"Профиль для {domain} не найден или запрошено обновление. Запускаем разведывательный краулинг.")
        #     discovery_crawler = WebCrawler(self.config)
        #     await discovery_crawler.initialize()
        #     try:
        #         samples = await discovery_crawler.crawl_for_profiling(
        #             company_data['website'],
        #             company_data['original_name'],
        #             max_pages=self.config.discovery_max_pages
        #         )
        #     finally:
        #         await discovery_crawler.close()

        #     if samples:
        #         profile = await self.profiler.generate_profile(domain, samples)
        #     else:
        #         log.warning(f"Не удалось собрать образцы для {domain}, используем базовый профиль.")
        #         profile = self.profiler._default_profile(domain)
        # else:
        #     log.info(f"Загружен профиль для {domain} от {profile.get('metadata', {}).get('generated_at')}")

        # # Передаём профиль в основной краулер
        # self.crawler.set_profile(profile)
        
        company_dir = os.path.join(
            self.config.documents_dir,
            f"{company_data['company_id']}_{company_data['safe_name']}"
        )
        
        is_update = os.path.exists(company_dir)

        # Создаем структуру папок компании через DirectoryManager
        company_dirs = self.directory_manager.create_company_directory_structure(company_dir)
        
        # Валидируем структуру
        if not self.directory_manager.validate_directory_structure(company_dir):
            log.warning(f"Обнаружены проблемы со структурой директорий для компании {company_data['original_name']}")

        # Очищаем кэши для текущей компании (но сохраняем глобальные кэши)
        processed_urls_at_start = len(self._global_processed_urls)
        processed_product_ids_at_start = len(self._global_processed_product_ids)

        # Сбрасываем состояние краулера и загружаем обработанные URL из хранилища
        await self.crawler.reset_state(company_data['original_name'])

        company_stats = CompanyStatistics(company_data)
        self.processing_tracker.set_current_company(company_data.get('company_id'))

        # Профилирование: профиль сайта + телеметрия прогона (метрики/census)
        self._setup_profiling(company_data)
        
        if is_update:
            log.info(f"Режим ОБНОВЛЕНИЯ для компании: {company_data['original_name']}")
            company_stats.update_mode = True
        else:
            log.info(f"Режим ПЕРВИЧНОЙ ЗАГРУЗКИ для компании: {company_data['original_name']}")
        
        result = {
            'company': company_data,
            'processed_at': datetime.now().isoformat(),
            'status': 'success',
            'crawling_result': None,
            'structured_products': [],
            'structured_distributors': [],
            'company_statistics': company_stats.to_dict(),
            'errors': []
        }

        # Потоковая обработка (конвейер внутри компании): товарные страницы уходят в
        # markdown/LLM сразу по мере сохранения краулером (гейты — в _resolve_streaming_mode)
        stream_workers = []
        streamed_products = []
        page_queue = None
        use_streaming, cached_actual_name = self._resolve_streaming_mode(company_data)
        if use_streaming:
            company_data['product_extraction_name'] = cached_actual_name
            page_queue = asyncio.Queue()
            stream_workers = [
                asyncio.create_task(self._page_stream_worker(
                    page_queue, company_data, company_stats, company_dir, streamed_products))
                for _ in range(self.config.pipeline_page_workers)
            ]
            self.crawler.page_sink = page_queue
            log.info(f"Потоковая обработка включена: {self.config.pipeline_page_workers} воркеров, "
                     f"производитель закреплён: '{cached_actual_name}'")

        try:
            # ЧЕКПОИНТ 1: Начало обработки компании
            await self.checkpoint_manager.save_checkpoint(
                company_data=company_data,
                processed_urls=self._global_processed_urls,
                stage="company_started",
                progress={"status": "starting",
                    "is_update": is_update
                },
                company_stats=company_stats.to_dict()
            )
            
            # Этап 1: Краулинг
            log.info(f"Запуск краулинга для {company_data['website']}")
            crawl_result = await self.full_site_crawling(company_data, company_dir)
            result['crawling_result'] = crawl_result

            # Потоковая обработка: краул закончился — дожидаемся «хвоста» очереди
            # (выполняется и на ветке неуспешного краула, чтобы не оставить воркеров)
            if use_streaming:
                self.crawler.page_sink = None
                for _ in stream_workers:
                    await page_queue.put(None)
                await asyncio.gather(*stream_workers, return_exceptions=True)
                log.info(f"Потоковая обработка завершена: {len(streamed_products)} товаров "
                         f"извлечено параллельно с краулом")

            if crawl_result['status'] != 'success':
                result['status'] = 'crawling_failed'
                error_msg = f"Ошибка краулинга: {crawl_result.get('error')}"
                company_stats.errors.append(error_msg)
                result['errors'].append(error_msg)
                company_stats.finish()
                
                # Извлекаем HTTP статус из ошибки, если есть
                http_status = None
                error_text = str(crawl_result.get('error', ''))
                if 'status' in crawl_result.get('crawl_data', {}):
                    http_status = crawl_result['crawl_data'].get('status')
                
                # Сохраняем с детальной информацией
                await self.save_unavailable_site(
                    company_data=company_data,
                    error_msg=error_msg,
                    http_status=http_status,
                    error_type='crawling_error',
                    company_stats=company_stats
                )
                return result
            
            else:
                # Обновляем сайт компании
                working_url = crawl_result.get('working_url')
                original_website = company_data['website']
                
                if working_url and working_url != original_website:
                    # Сохраняем оригинальный URL для отслеживания
                    company_data['original_website'] = original_website
                    # Обновляем website на рабочий URL
                    company_data['website'] = working_url
                    log.info(f"Обновлен website компании: {company_data['original_name']} "
                            f"с {original_website} на {working_url}")
                else:
                    log.info(f"Website компании остался без изменений: {original_website}")

            stored_pages = crawl_result.get('stored_pages', [])
            product_pages = [p for p in stored_pages if p['category'] == 'product']
            distributor_pages = [p for p in stored_pages if p['category'] == 'distributor']
            other_pages = [p for p in stored_pages if p['category'] != 'product']

            # ЧЕКПОИНТ 2: Краулинг завершен
            await self.checkpoint_manager.save_checkpoint(
                company_data=company_data,
                processed_urls=self._global_processed_urls,
                stage="crawling_completed",
                progress={
                    "status": "crawling_done",
                    "pages_found": len(stored_pages),
                    "products_found": len(product_pages),
                    "distributors_found": len(distributor_pages),
                    "other_pages": len(other_pages),
                    "website_updated": 'original_website' in company_data,
                    "working_url": crawl_result.get('working_url', '')
                },
                company_stats=company_stats.to_dict()
            )
            
            log.info(f"Краулинг завершен. Всего страниц: {len(stored_pages)}, "
                f"товарных: {len(product_pages)}, дистрибьюторских: {len(distributor_pages)}, других: {len(other_pages)}")
            
            # Получаем domain_dirs для скачивания файлов
            domain_dirs = self._get_domain_dirs(company_data['website'], company_data, company_dir)
            
            # Получаем необработанные страницы из хранилища
            unprocessed_pages_from_storage = await self.crawler.temp_storage.get_unprocessed_pages(company_data['original_name'])
            
            # Объединяем страницы из краулера и необработанные из хранилища
            all_pages_dict = {}
            
            for page in unprocessed_pages_from_storage:
                normalized_url = page['metadata'].get('normalized_url')
                if normalized_url:
                    all_pages_dict[normalized_url] = {
                        'file_path': page['html_path'],
                        'url': page['metadata'].get('url', ''),
                        'category': page['metadata'].get('category', 'other'),
                        'normalized_url': normalized_url,
                        'timestamp': page['metadata'].get('saved_at', ''),
                        'metadata': page['metadata']
                    }
            
            for page in stored_pages:
                normalized_url = page.get('normalized_url')
                if normalized_url:
                    all_pages_dict[normalized_url] = page
            
            all_pages = list(all_pages_dict.values())
            # D85: коллапс типоразмеров chelaz.ru (вариант A) — оставляем общую карточку-диапазон
            # модели, размерные дочерние страницы того же каталога отбрасываем ДО извлечения.
            all_pages, _dropped_variants = collapse_chelaz_size_variants(all_pages)
            if _dropped_variants:
                _ex = [urlparse(p.get('url', '')).path.rsplit('/', 1)[-1] for p in _dropped_variants[:5]]
                log.info(f"Типоразмеры chelaz: свёрнуто {len(_dropped_variants)} дублей "
                         f"(размерные + алиас-каталоги), оставлены общие карточки семейств; примеры: {_ex}")
            company_stats.products_found = len([p for p in all_pages if p['category'] == 'product'])
            company_stats.distributors_found = len([p for p in all_pages if p['category'] == 'distributor'])
            # Счётчики обработанных страниц по категориям берём из фактического рабочего набора all_pages,
            # а не из stored_pages: туда попадают только новые/изменённые страницы (is_new), поэтому на
            # ре-крауле без очистки temp-хранилища счётчики обнулялись при реально обработанных страницах.
            company_stats.product_pages_processed = company_stats.products_found
            company_stats.distributor_pages_processed = company_stats.distributors_found
            company_stats.company_pages_processed = len([p for p in all_pages if p['category'] == 'contacts'])
            self.statistics.product_pages_processed += company_stats.product_pages_processed
            self.statistics.distributor_pages_processed += company_stats.distributor_pages_processed
            self.statistics.company_pages_processed += company_stats.company_pages_processed
            
            log.info(f"Начинаем обработку {len(all_pages)} страниц (из краулера: {len(stored_pages)}, из хранилища: {len(unprocessed_pages_from_storage)})")
            
                        # 1. Обработка страниц компании
            log.info("Получение актуального наименования компании через AI")
            company_info = await self.generate_company_card(company_data, company_dir, company_stats)
            
            if isinstance(company_info, str):
                try:
                    company_info = json.loads(company_info)
                except json.JSONDecodeError:
                    company_info = {"raw_text": company_info}
            elif company_info is None:
                company_info = {}
            if isinstance(company_info, dict) and "company" not in company_info and company_info:
                company_info = {"company": company_info}
                
            # Извлекаем актуальное наименование компании из AI-ответа
            actual_company_name = self._extract_actual_company_name(company_info, company_data['original_name'])
            company_data['actual_name'] = actual_company_name
            company_data['actual_safe_name'] = sanitize_company_name(actual_company_name)

            log.info(f"Актуальное наименование компании: '{actual_company_name}' (оригинальное: '{company_data['original_name']}')")

            # Кэшируем актуальное наименование для потоковой обработки следующего прогона
            self._save_actual_name_cache(company_data, actual_company_name)
            if use_streaming and cached_actual_name and actual_company_name != cached_actual_name:
                log.warning(f"Актуальное наименование изменилось: '{cached_actual_name}' -> '{actual_company_name}'. "
                            f"Товары этого прогона извлечены со старым именем (стабильность product_id); "
                            f"новое имя вступит в силу со следующего прогона")
            
            # 2. Обработка страниц дистрибьюторов
            log.info("Обработка дистрибьюторов")
            structured_distributors = await self.process_distributor_pages(company_data, company_dir, company_stats)
            result['structured_distributors'] = structured_distributors
            
            # 3. Параллельная обработка товаров и скачивание файлов (закомментировано). Сейчас последовательная (при достаточности RAM можно вернуть).
            # log.info("Параллельная обработка товаров и скачивание файлов")
            # download_task = asyncio.create_task(
            #     self._download_site_files_parallel(company_data['website'], domain_dirs)
            # )
            # processing_task = asyncio.create_task(
            #     self.process_stored_pages_with_checkpoints(all_pages, company_data, company_stats, company_dir)
            # )
            
            # # Ожидаем завершения обеих задач
            # downloaded_files, structured_products = await asyncio.gather(
            #     download_task, processing_task,
            #     return_exceptions=True
            # )
            
            log.info("Загрузка файлов сайта...")
            downloaded_files = await self._download_site_files_parallel(company_data['website'], domain_dirs)
            log.info("Обработка страниц товаров...")
            pages_for_processing = all_pages
            if use_streaming:
                # Уже обработанные потоком страницы не гоняем через финальный проход, чтобы
                # не искажать счётчик urls_skipped_duplicate; остатки (страницы из хранилища
                # прошлых прогонов, ретраи, сбои воркеров) обрабатываются как раньше
                pages_for_processing = [p for p in all_pages
                                        if p.get('normalized_url') not in self._global_processed_urls]
            structured_products = await self.process_stored_pages_with_checkpoints(pages_for_processing, company_data, company_stats, company_dir)
            if use_streaming and isinstance(structured_products, list):
                structured_products = streamed_products + structured_products

            # Обрабатываем возможные исключения
            if isinstance(downloaded_files, Exception):
                log.error(f"Ошибка при скачивании файлов: {downloaded_files}")
                downloaded_files = []
                company_stats.errors.append(f"Ошибка скачивания файлов: {downloaded_files}")
            
            if isinstance(structured_products, Exception):
                log.error(f"Ошибка при обработке страниц: {structured_products}")
                structured_products = []
                company_stats.errors.append(f"Ошибка обработки страниц: {structured_products}")
            
            # Обновляем результат с скачанными файлами
            if crawl_result.get('crawl_data'):
                crawl_result['crawl_data']['downloaded_files'] = downloaded_files
                crawl_result['files_count'] = len(downloaded_files)
            _site_files_n = len(downloaded_files)
            company_stats.files_found += _site_files_n
            company_stats.files_downloaded += _site_files_n
            self.statistics.total_files_found += _site_files_n
            self.statistics.total_files_downloaded += _site_files_n
            
            result['structured_products'] = structured_products
                        
            log.info(f"Обработано товаров: {len(structured_products)}, дистрибьюторов: {len(structured_distributors)}, скачано файлов: {len(downloaded_files)}")
            
            # ЧЕКПОИНТ 3: Обработка страниц завершена
            await self.checkpoint_manager.save_checkpoint(
                company_data=company_data,
                processed_urls=self._global_processed_urls,
                stage="page_processing_completed",
                progress={
                    "status": "processing_done",
                    "products_processed": len(structured_products),
                    "distributors_processed": len(structured_distributors),
                    "files_downloaded": len(downloaded_files)
                },
                company_stats=company_stats.to_dict()
            )
            # Очистка глобального кэша URL после сохранения чекпоинта
            await self._trim_global_url_cache()

            if structured_products:
                log.info(f"Начинаем индексирование {len(structured_products)} товаров")
                
                # ЧЕКПОИНТ 4: Начало индексирования
                await self.checkpoint_manager.save_checkpoint(
                    company_data=company_data,
                    processed_urls=self._global_processed_urls,
                    stage="indexing_started",
                    progress={
                        "status": "starting_indexing",
                        "products_to_index": len(structured_products)
                    },
                    company_stats=company_stats.to_dict()
                )
                
                # Добавляем в индекс и генерируем карточки
                await self.add_products_to_index(
                    structured_products, company_data, company_stats
                )
                
                # ЧЕКПОИНТ 5: Индексирование завершено
                await self.checkpoint_manager.save_checkpoint(
                    company_data=company_data,
                    processed_urls=self._global_processed_urls,
                    stage="indexing_completed",
                    progress={
                        "status": "indexing_done",
                        "products_indexed": len(structured_products),
                        "cards_generated": company_stats.cards_generated
                    },
                    company_stats=company_stats.to_dict()
                )

            else:
                log.warning("Не найдено товаров для индексирования")
                result['status'] = 'no_products_processed'
                result['errors'].append('Не удалось обработать ни одного товара')
            
            log.info(f"Обработка компании завершена. Данные добавлены в индексную базу.")
            
            # === ИНТЕГРАЦИЯ С GRAPH DB ===
            if self.config.graph_db_enable and self.graph_db_uploader:
                try:
                    log.info(f"Начинаем отправку данных в Graph DB для компании: {company_data['original_name']}")
                    
                    if company_info is None:
                        company_info = {}

                    if isinstance(company_info, dict) and "company" not in company_info and company_info:
                        company_info = {"company": company_info}
                    
                    log.info(f"Найдена AI-информация о компании: {bool(company_info)}")

                    # Подготавливаем информацию о файлах компании
                    company_files_info = await self._collect_company_files_for_graphdb(company_dir)
                    
                    # Форматируем пути в продуктах для Graph DB
                    products_for_graphdb = []
                    for product in structured_products:                        
                        if 'files' in product:
                            for file_info in product['files']:
                                if 'path' in file_info and os.path.isabs(file_info['path']):
                                    # Конвертируем абсолютный путь в относительный
                                    file_info['path'] = os.path.relpath(file_info['path'], self.config.documents_dir)
                        products_for_graphdb.append(product)
                    
                    # Отправляем данные в Graph DB
                    upload_success = await self.graph_db_uploader.upload_company_data(
                        company_data=company_data,
                        company_info=company_info,
                        products=products_for_graphdb,
                        distributors=structured_distributors,
                        company_files=company_files_info,
                        is_update=is_update
                    )
                    
                    if upload_success:
                        company_stats.graph_db_uploaded = True
                        company_stats.graph_db_products_sent = len(structured_products)
                        company_stats.graph_db_distributors_sent = len(structured_distributors)
                        company_stats.graph_db_files_sent = len(company_files_info)
                        
                        # Обновляем общую статистику
                        self.statistics.graph_db_companies_sent += 1
                        self.statistics.graph_db_products_sent += len(structured_products)
                        self.statistics.graph_db_distributors_sent += len(structured_distributors)
                        self.statistics.graph_db_files_sent += len(company_files_info)
                        self.statistics.graph_db_successful_sends += 1
                        
                        log.info(f"Данные успешно отправлены в Graph DB для компании {company_data['original_name']}")
                    else:
                        # Не все данные отправились (состояние сохранено в pending_manager). Нефатальная ошибка (валидация и т.п.)
                        company_stats.graph_db_uploaded = False
                        error_msg = f"Не удалось отправить данные в Graph DB для компании {company_data['original_name']}"
                        company_stats.graph_db_errors.append(error_msg)
                        company_stats.errors.append(error_msg)
                        result['errors'].append(error_msg)
                        # Данные не в графе — компания не может считаться успешной
                        result['status'] = 'graph_db_upload_failed'
                        self.statistics.graph_db_failed_sends += 1
                        log.warning(error_msg)

                except GraphDBFatalError as e:
                    # Бэкенд недоступен: состояние отправки сохранено в pending, сигнализируем оркестратору об остановке
                    company_stats.graph_db_uploaded = False
                    error_msg = f"Фатальная ошибка Graph DB: {e}"
                    company_stats.graph_db_errors.append(error_msg)
                    company_stats.errors.append(error_msg)
                    result['errors'].append(error_msg)
                    result['status'] = 'graph_db_fatal_error'
                    self.statistics.graph_db_failed_sends += 1
                    log.critical(error_msg)

                except Exception as e:
                    error_msg = f"Ошибка при отправке в Graph DB {e}"
                    log.error(error_msg)
                    log.error(traceback.format_exc())
                    company_stats.graph_db_errors.append(error_msg)
                    company_stats.errors.append(error_msg)
                    result['errors'].append(error_msg)
                    self.statistics.graph_db_failed_sends += 1

                    try:
                        await self._mark_company_for_graph_db_retry(company_data, error_msg)
                    except:
                        pass

            # Задержка между компаниями для Graph DB
            await asyncio.sleep(self.config.graph_db_delay_between_companies)
            
            # === КОНВЕРТАЦИЯ ФАЙЛОВ В PDF ===
            if self.config.enable_file_conversion:
                try:
                    log.info(f"Начинаем конвертацию файлов в PDF для компании: {company_data['original_name']}")
                    
                    # Устанавливаем флаг конвертации в статистике
                    company_stats.file_conversion_enabled = True
                    
                    # Запускаем конвертацию
                    conversion_success = await self._convert_company_files(company_dir, company_stats)
                    
                    if conversion_success:
                        company_stats.file_conversion_completed = True
                        log.info(f"Конвертация файлов завершена для компании: {company_data['original_name']}")
                    else:
                        error_msg = f"Ошибка конвертации файлов для компании {company_data['original_name']}"
                        company_stats.file_conversion_errors.append(error_msg)
                        company_stats.errors.append(error_msg)
                        result['errors'].append(error_msg)
                        log.warning(error_msg)
                        
                except Exception as e:
                    error_msg = f"Исключение при конвертации файлов: {e}"
                    log.error(error_msg)
                    company_stats.file_conversion_errors.append(error_msg)
                    company_stats.errors.append(error_msg)
                    result['errors'].append(error_msg)
            
            # === КОНЕЦ КОНВЕРТАЦИИ ФАЙЛОВ ===

            # ЧЕКПОИНТ 6: Обработка компании завершена
            await self.checkpoint_manager.save_checkpoint(
                company_data=company_data,
                processed_urls=self._global_processed_urls,
                stage="company_completed",
                progress={
                    "status": "company_processing_complete",
                    "final_products": len(structured_products),
                    "final_distributors": len(structured_distributors),
                    "final_cards": company_stats.cards_generated,
                    "graph_db_uploaded": company_stats.graph_db_uploaded,
                    "file_conversion_completed": company_stats.file_conversion_completed
                },
                company_stats=company_stats.to_dict()
            )
            
        except Exception as e:
            result['status'] = 'error'
            error_msg = f"Критическая ошибка обработки компании: {e}"
            result['errors'].append(error_msg)
            company_stats.errors.append(error_msg)
            
            await self.save_unavailable_site(
                company_data=company_data,
                error_msg=error_msg,
                error_type='processing_error',
                company_stats=company_stats
            )
                    
            # ЧЕКПОИНТ 7: Критическая ошибка
            await self.checkpoint_manager.save_checkpoint(
                company_data=company_data,
                processed_urls=self._global_processed_urls,
                stage="critical_error",
                progress={"status": "error", "error": str(e)},
                company_stats=company_stats.to_dict(),
                errors=[error_msg]
            )
            
            log.error(f"Критическая ошибка обработки компании {company_data['original_name']}: {e}")
            log.error(traceback.format_exc())

        finally:
            # Потоковая обработка: гарантированная остановка воркеров при любом исходе
            # (на штатном пути они уже завершены — cancel() тогда no-op)
            if stream_workers:
                self.crawler.page_sink = None
                for w in stream_workers:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*stream_workers, return_exceptions=True)

        # Профилирование: метрики прогона + черновик переписи (fail-open)
        self._finalize_profiling(result.get('crawling_result'), company_stats)

        company_stats.finish()
        result['company_statistics'] = company_stats.to_dict()

        self.statistics.companies_processed += 1
        if result['status'] == 'success':
            self.statistics.companies_success += 1
            # Очиста чекпоинта после успешной обработки
            await self.checkpoint_manager.clear_checkpoint()
            await self._trim_global_url_cache()
        else:
            self.statistics.companies_failed += 1
        
        return result
    
    def _extract_actual_company_name(self, company_info: Dict[str, Any], default_name: str) -> str:
        """Извлечение актуального наименования компании из AI-ответа"""
        try:
            if not company_info:
                return default_name
            
            # Проверяем разные форматы ответа
            if isinstance(company_info, dict):
                # Формат 1: {"company": {"Наименование компании": "..."}}
                if 'company' in company_info and isinstance(company_info['company'], dict):
                    actual_name = company_info['company'].get('Наименование компании', '')
                    if actual_name and actual_name.strip():
                        return actual_name.strip()
                
                # Формат 2: {"Наименование компании": "..."} напрямую
                actual_name = company_info.get('Наименование компании', '')
                if actual_name and actual_name.strip():
                    return actual_name.strip()
            
            # Если не удалось извлечь, используем оригинальное название
            log.warning(f"Не удалось извлечь актуальное наименование компании из AI-ответа. Используется оригинальное: {default_name}")
            return default_name
            
        except Exception as e:
            log.error(f"Ошибка извлечения актуального наименования компании: {e}")
            return default_name
        
    async def _convert_company_files(self, company_dir: str, company_stats=None) -> bool:
        """Конвертация файлов компании в PDF формат через LibreOffice"""
       
        try:
            log.info(f"Инициализация конвертера для директории: {company_dir}")
            
            converter = FileConverter(
                company_directory=company_dir,
                libreoffice_path=self.config.libreoffice_path,
                max_workers=self.config.converter_max_workers,
                log_file=self.config.converter_log_file,
                delete_originals=self.config.delete_originals_after_conversion,
                delete_empty_folders=self.config.delete_empty_folders,
                exclude_product_cards=True
            )
            
            # Запускаем конвертацию. run_conversion блокирующий (ThreadPoolExecutor +
            # soffice): вызванный из корутины напрямую, он замораживал весь event loop, и
            # сторож простоя D101 — корутина того же цикла — на всей стадии конвертации не
            # получал управления. Так прогон замер на 14 ч (25.08 15:39 -> 26.08 05:43).
            success = await asyncio.to_thread(converter.run_conversion)

            # Счётчики и тайминги конвертации (лист 2 + лист «Статистика по времени обработки»)
            _conv_results = getattr(converter, 'last_results', []) or []
            company_converted = sum(1 for r in _conv_results if r.success and not r.error)
            company_conv_failed = sum(1 for r in _conv_results if not r.success)
            if company_stats is not None:
                company_stats.files_converted = company_converted
                company_stats.files_conversion_failed = company_conv_failed
            for r in _conv_results:
                if r.success and not r.error and r.conversion_time > 0:
                    self.processing_tracker.add_clean('pdf', r.conversion_time)

            if success:
                log.info(f"Конвертация файлов завершена для компании: {os.path.basename(company_dir)}")
            else:
                log.warning(f"Конвертация файлов завершилась с ошибками для компании: {os.path.basename(company_dir)}")
            
            return success
            
        except FileNotFoundError as e:
            error_msg = f"LibreOffice не найден: {e}"
            log.error(error_msg)
            
            # Записываем ошибку в отдельный лог
            error_log_path = os.path.join(self.config.reports_dir, "libreoffice_errors.log")
            with open(error_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {error_msg}\n")
            
            return False
            
        except Exception as e:
            error_msg = f"Ошибка конвертации файлов: {e}"
            log.error(error_msg)
            return False
    
    async def generate_company_card(self, company_data: Dict[str, str], company_dir: str, 
                              company_stats: CompanyStatistics) -> Dict[str, Any]:
        """Генерация карточки компании на основе всех страниц компании"""
        log.info(f"Начинаем генерацию карточки компании: {company_data['original_name']}")
        
        try:
            # Получаем все страницы компании из временного хранилища
            company_pages = await self.crawler.temp_storage.get_company_files(
                company_data['original_name'], 
                category='contacts'  
            )
            
            if not company_pages:
                log.warning(f"Не найдено страниц компании для {company_data['original_name']}")
                return ""
            
            log.info(f"Найдено {len(company_pages)} страниц компании")            
                       
            # Объединяем контент всех страниц
            combined_content = await self._get_company_content(company_data['original_name'], company_pages)
            
            if not combined_content:
                log.error("Не удалось объединить контент страниц компании")
                return ""
            
            log.info("Обработка объединенного контента компании через AI")
            
            company_info, token_usage = await self.ai_client.extract_company_info(
                combined_content, 
                company_data['company_id'],
                chunk_info="Полный контент компании"
            )
            
            if not company_info:
                log.error("Не удалось получить информацию о компании через AI")
                return {}
            
            log.info(f"Успешно получена информация о компании (входные токенов: {token_usage['input_tokens']}, выходные: {token_usage['output_tokens']})")

            # Обновляем статистику токенов для LLM запросов
            company_stats.input_tokens += token_usage['input_tokens']
            company_stats.output_tokens += token_usage['output_tokens']
            self.statistics.total_input_tokens += token_usage['input_tokens']
            self.statistics.total_output_tokens += token_usage['output_tokens']

            # Сохраняем AI-текст компании
            await self.crawler.temp_storage.save_ai_text(
                company_name=company_data['original_name'],
                url="company_full_content",
                normalized_url="company_full_content",
                ai_text=company_info,
                text_type="company_info",
                metadata={
                    'company_id': company_data['company_id'],
                    'input_tokens': token_usage['input_tokens'],
                    'output_tokens': token_usage['output_tokens'],
                    'total_pages_processed': len(company_pages),
                    'processing_timestamp': datetime.now().isoformat()
                }
            )
            
            # Добавляем документ в векторную базу данных (если включена)
            embedding_tokens = 0
            if self.index_manager:
                log.info("Добавляем компанию в векторную базу данных")
                embedding_tokens = await self.index_manager.add_company_to_index(
                    company_info, company_data
                )
            
            # Обновляем статистику токенов для эмбеддингов
            company_stats.embedding_tokens += embedding_tokens
            self.statistics.total_embedding_tokens += embedding_tokens
            
            if self.index_manager:
                log.info(f"Компания добавлена в базу данных, использовано токенов эмбеддингов: {embedding_tokens}")
            log.info("Обработка информации о компании завершена.")

            if isinstance(company_info, str):
                try:
                    return json.loads(company_info)
                except json.JSONDecodeError:
                    return {"company_info": company_info}
            else:
                return company_info
                
        except Exception as e:
            log.error("Ошибка обработки информации о компании", exc_info=True)
            return {}
    
    async def _get_company_content(self, company_name: str, pages: List[Dict[str, Any]]) -> str:
        """Объединение контента страниц компании с конвертацией в Markdown"""
        combined_content = []
        for page in pages:
            try:
                storage_result = await self.crawler.temp_storage.load_cleaned_html(page['html_path'])
                if storage_result and storage_result['html_content']:
                    metadata = storage_result['metadata']
                    url = metadata.get('url', 'unknown_url')
                    normalized_url = metadata.get('normalized_url', '')
                    html = storage_result['html_content']
                    markdown = html_to_markdown(html, url=url, page_type='company')
                    # Сохраняем Markdown
                    await self._save_markdown_file(company_name, 'company', normalized_url, markdown)

                    combined_content.append(f"\n=== СТРАНИЦА: {url} ===\n{markdown}\n")
            except Exception as e:
                log.warning(f"Ошибка загрузки страницы компании {page.get('html_path', '')}: {e}")
                continue
        return "\n\n".join(combined_content)   

    async def _download_site_files_parallel(self, site_url: str, domain_dirs: Dict[str, str]) -> List[Dict[str, Any]]:
        """Параллельное скачивание файлов сайта"""
        try:
            # Получаем пути скачанных файлов
            downloaded_file_paths = await self.crawler._download_site_files(site_url, domain_dirs)
            log.info(f"Скачивание файлов завершено: {len(downloaded_file_paths)} файлов")
            
            # Преобразуем пути в информацию о файлах
            downloaded_files_info = []
            for file_path in downloaded_file_paths:
                if os.path.exists(file_path):
                    try:
                        # Получаем информацию о файле (без привязки к продукту)
                        file_info = await self.file_id_manager.get_file_info(file_path, product_id=None)
                        if file_info.get('exists'):
                            # Удаляем служебные поля
                            file_info.pop('exists', None)
                            file_info.pop('error', None)
                            downloaded_files_info.append(file_info)
                    except Exception as e:
                        log.warning(f"Ошибка получения информации о файле {file_path}: {e}")
            
            return downloaded_files_info
        except Exception as e:
            log.error(f"Ошибка при параллельном скачивании файлов: {e}")
            return []

    async def _collect_company_files_for_graphdb(self, company_dir: str) -> List[Dict[str, Any]]:
        """Сбор всех файлов компании для Graph DB без дублирования"""
        all_files = []
        processed_file_ids = set()
        
        company_folders_to_scan = [
            os.path.join(company_dir, "Certificates"),
            os.path.join(company_dir, "Documents"),
            os.path.join(company_dir, "Instructions"),
            os.path.join(company_dir, "Price_lists")
        ]
        
        # Сканируем файлы компании
        for folder in company_folders_to_scan:
            if os.path.exists(folder):
                for root, dirs, files in os.walk(folder):
                    for file_name in files:
                        file_path = os.path.join(root, file_name)
                        try:
                            file_info = await self.file_id_manager.get_file_info(file_path, product_id=None)
                            if file_info.get('exists'):
                                file_id = file_info.get('id')
                                if file_id and file_id not in processed_file_ids:
                                    # Форматируем путь (относительный от папки компании)
                                    relative_path = os.path.relpath(file_path, self.config.documents_dir)
                                    file_info['path'] = relative_path
                                    
                                    # Удаляем служебные поля
                                    file_info.pop('exists', None)
                                    file_info.pop('error', None)
                                    
                                    # Добавляем дату скачивания если нет
                                    if 'download_date' not in file_info:
                                        file_info['download_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    
                                    all_files.append(file_info)
                                    processed_file_ids.add(file_id)
                        except Exception as e:
                            log.warning(f"Ошибка обработки файла компании {file_path}: {e}")
        
        log.info(f"Собрано {len(all_files)} файлов компании для Graph DB (папки: Certificates, Documents, Instructions, Price_lists)")
        return all_files 
    
    async def generate_reports(self, results: List[Dict[str, Any]]) -> Dict[str, str]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        xlsx_report_path = self.xlsx_generator.generate_monitoring_report(
            results, self.statistics, self.config.reports_dir, timestamp
        )        
                
        return {
            'xlsx': xlsx_report_path            
        }
    
    async def get_checkpoint_status(self) -> Dict[str, Any]:
        """Получение текущего статуса чекпоинтов"""
        checkpoint_data = await self.checkpoint_manager.load_checkpoint()
        history = await self.checkpoint_manager.get_checkpoint_history()
        
        return {
            'has_active_checkpoint': checkpoint_data is not None,
            'current_checkpoint': checkpoint_data.__dict__ if checkpoint_data else None,
            'history_count': len(history),
            'recent_history': history[:5]  # Последние 5 чекпоинтов
        }

    async def force_checkpoint(self, company_data: Dict[str, str], reason: str = "manual"):
        """Принудительное создание чекпоинта"""
        await self.checkpoint_manager.save_checkpoint(
            company_data=company_data,
            processed_urls=self._global_processed_urls,
            stage=f"manual_checkpoint_{reason}",
            progress={"status": "manual_checkpoint", "reason": reason},
            metadata={"manual": True, "reason": reason}
        )
    
    async def print_performance_stats(self):
        """Вывод статистики производительности"""
        if hasattr(self, 'performance_monitor'):
            stats = self.performance_monitor.get_current_stats()
            recommendations = self.performance_monitor.get_recommendations()
            
            log.info("=== СТАТИСТИКА ПРОИЗВОДИТЕЛЬНОСТИ LLM ===")
            if stats:
                log.info(f"Запросов за 5 мин: {stats.get('requests_5min', 0)}")
                log.info(f"Успешных запросов: {stats.get('success_rate', 0)*100:.1f}%")
                log.info(f"Среднее время ответа: {stats.get('avg_response_time_seconds', 0):.2f} сек")
                log.info(f"Токенов в минуту: {stats.get('tokens_per_minute', 0):.0f}")
            else:
                log.info("Нет данных о производительности")
            
            if recommendations:
                log.info("=== РЕКОМЕНДАЦИИ ===")
                for rec in recommendations:
                    log.info(f"• {rec}")
        
        # Вывод статистики Graph DB
        if self.graph_db_uploader:
            self.graph_db_uploader.print_statistics()

    async def run_monitoring(self):
        log.info("Запуск системы мониторинга с Kafka управлением и чекпоинтами")
        
        companies = self.read_company_list()
        log.info(f"Найдено {len(companies)} компаний для обработки")

        # Загружаем существующие ID из индекса
        await self.load_existing_ids_from_index()

        if not companies:
            log.error("Не найдено компаний для обработки")
            return
        
        # Восстановление из расширенного чекпоинта
        checkpoint_data = await self.checkpoint_manager.load_checkpoint()
        if checkpoint_data:
            log.info(f"Восстановление с чекпоинта: {checkpoint_data.current_company['original_name']} "
                    f"(этап: {checkpoint_data.current_stage})")
            
            # Восстанавливаем обработанные URL
            self._global_processed_urls.update(checkpoint_data.processed_urls)            
        
            # Находим индекс компании для возобновления
            current_company_id = checkpoint_data.current_company['company_id']
            for i, company in enumerate(companies):
                if company['company_id'] == current_company_id:
                    self._current_regular_company_index = i
                    log.info(f"Продолжение с компании {company['original_name']} (индекс: {i})")
                    break
        else:
            self._current_regular_company_index = 0

        if not await self.resume_pending_graphdb_uploads():
            log.critical("Восстановление отложенных отправок не удалось. Остановка пайплайна.")
            return
        
        results = []        

        # Основной цикл обработки компаний
        while self._current_regular_company_index < len(companies):
            if self._stop_requested:
                log.info("Остановка мониторинга запрошена")
                break
            
            company = companies[self._current_regular_company_index]
            log.info(f"Обработка компании {self._current_regular_company_index+1}/{len(companies)}: {company['original_name']}")
            
            try:
                # Перед началом обработки компании проверяем, есть ли urgent задачи в Kafka
                urgent_processed = await self._process_pending_urgent_tasks()
                
                if urgent_processed:
                    log.info("Возобновление регулярной обработки после urgent задач")
                    # Сохраняем чекпоинт после urgent задач
                    await self.checkpoint_manager.save_checkpoint(
                        company_data=company,
                        processed_urls=self._global_processed_urls,
                        stage="after_urgent_tasks",
                        progress={
                            "status": "resuming_regular",
                            "current_company_index": self._current_regular_company_index,
                            "total_companies": len(companies)
                        }
                    )
                
                # Обрабатываем регулярную компанию
                company_result = await self.process_company(company)
                results.append(company_result)
                
                # Увеличиваем индекс только если компания успешно обработана
                if company_result.get('status') == 'success':
                    self._current_regular_company_index += 1
                    # Очищаем чекпоинт после успешной обработки
                    await self.checkpoint_manager.clear_checkpoint()
                else:
                    log.error(f"Ошибка обработки компании {company['original_name']}, пропускаем")
                    self._current_regular_company_index += 1  # Все равно переходим к следующей
                
                # Принудительная очистка памяти после каждой компании
                import gc
                gc.collect()
                
            except Exception as e:
                log.error(f"Критическая ошибка при обработке компании {company['original_name']}: {e}")
                results.append({
                    'company': company,
                    'status': 'critical_error',
                    'error': str(e),
                    'processed_at': datetime.now().isoformat()
                })
                self._current_regular_company_index += 1
                
                # Попытка восстановления
                recovery_success = await self._recover_from_crash(company)
                if not recovery_success:
                    log.error(f"Не удалось восстановить обработку компании {company['original_name']}")
                    break
        
        # После обработки всех регулярных компаний проверяем оставшиеся urgent задачи
        if self._kafka_initialized:
            await self._process_remaining_urgent_tasks()
        
        await self.print_performance_stats()
        report_paths = await self.generate_reports(results)
        self.statistics.print_statistics()
        
        # Финальная очистка чекпоинтов
        await self.checkpoint_manager.clear_checkpoint()
        
        log.info(f"Мониторинг завершен. Отчеты сохранены в: {report_paths}")
        
        return {
            'results': results,
            'statistics': self.statistics,
            'reports': report_paths
        }
    
    async def _process_pending_urgent_tasks(self) -> bool:
        """Обработка ожидающих urgent задач"""
        if not self._kafka_initialized:
            return False
        
        urgent_processed = False
        
        try:
            # Проверяем, есть ли urgent задачи
            urgent_task = await self.kafka_manager.get_urgent_task()
            
            while urgent_task:
                log.info(f"Начинаем обработку URGENT задачи: {urgent_task.task_id}")
                urgent_processed = True
                
                # Отправляем статус "в обработке"
                await self.kafka_manager.send_task_status(
                    urgent_task.task_id,
                    TaskStatus.PROCESSING,
                    f"Начата обработка компании: {urgent_task.company_data.get('original_name')}",
                    progress=0.0
                )
                
                try:
                    # Обрабатываем urgent компанию
                    result = await self.process_company(urgent_task.company_data, force_restart=True)
                    
                    # Отправляем финальный статус
                    if result.get('status') == 'success':
                        await self.kafka_manager.send_task_status(
                            urgent_task.task_id,
                            TaskStatus.COMPLETED,
                            f"Компания успешно обработана: {urgent_task.company_data.get('original_name')}",
                            progress=100.0
                        )
                        log.info(f"URGENT задача {urgent_task.task_id} успешно обработана")
                    else:
                        error_msg = result.get('error', 'Неизвестная ошибка')
                        await self.kafka_manager.send_task_status(
                            urgent_task.task_id,
                            TaskStatus.FAILED,
                            f"Ошибка обработки компании: {error_msg}",
                            progress=100.0
                        )
                        log.error(f"URGENT задача {urgent_task.task_id} завершена с ошибкой: {error_msg}")
                        
                        # Повторяем failed задачу
                        await self._retry_failed_urgent_task(urgent_task)
                        
                except Exception as e:
                    log.error(f"Критическая ошибка обработки URGENT задачи: {e}")
                    await self.kafka_manager.send_task_status(
                        urgent_task.task_id,
                        TaskStatus.FAILED,
                        f"Критическая ошибка: {str(e)}",
                        progress=100.0
                    )
                    # Повторяем failed задачу
                    await self._retry_failed_urgent_task(urgent_task)
                
                # Проверяем следующую urgent задачу
                urgent_task = await self.kafka_manager.get_urgent_task()
        
        except Exception as e:
            log.error(f"Ошибка при проверке urgent задач: {e}")
        
        return urgent_processed
    
    async def _retry_failed_urgent_task(self, urgent_task: TaskMessage):
        """Повторная обработка failed urgent задачи"""
        try:
            log.info(f"Повторная обработка failed urgent задачи: {urgent_task.task_id}")
            
            # Создаем новую задачу с тем же company_data
            retry_task_id = f"retry_{urgent_task.task_id}"
            
            retry_task = TaskMessage(
                task_id=retry_task_id,
                task_type=TaskType.URGENT,
                company_data=urgent_task.company_data,
                user_id=urgent_task.user_id,
                priority=20  # Высший приоритет для повторной обработки
            )
            
            # Отправляем в Kafka
            await self.kafka_manager.producer.send_and_wait(
                self.config.kafka_topic_urgent_tasks,
                value=retry_task.to_dict(),
                key=retry_task_id
            )
            
            log.info(f"Failed urgent задача {urgent_task.task_id} отправлена на повторную обработку как {retry_task_id}")
            
        except Exception as e:
            log.error(f"Ошибка при повторной отправке failed urgent задачи: {e}")
    
    async def _process_urgent_task(self, urgent_task: TaskMessage):
        """Обработка срочной задачи от пользователя"""
        log.info(f"Начало обработки URGENT задачи: {urgent_task.task_id}")
        
        try:
            # Отправляем статус "в обработке"
            await self.kafka_manager.send_task_status(
                urgent_task.task_id,
                TaskStatus.PROCESSING,
                f"Начата обработка компании: {urgent_task.company_data.get('original_name')}",
                progress=0.0
            )
            
            # Обрабатываем компанию
            result = await self.process_company(urgent_task.company_data, force_restart=True)
            
            # Отправляем финальный статус
            if result.get('status') == 'success':
                await self.kafka_manager.send_task_status(
                    urgent_task.task_id,
                    TaskStatus.COMPLETED,
                    f"Компания успешно обработана: {urgent_task.company_data.get('original_name')}",
                    progress=100.0
                )
                
                # Обновляем report_date в Graph DB
                if self.config.graph_db_enable:
                    try:
                        company_id = urgent_task.company_data.get('company_id')
                        if company_id:
                            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            success = await self.graph_db_uploader.update_company_report_date(
                                company_id, current_date
                            )
                            if success:
                                log.info(f"report_date обновлен для urgent компании {company_id}")
                    except Exception as e:
                        log.error(f"Ошибка обновления report_date для urgent компании: {e}")
                
                log.info(f"URGENT задача {urgent_task.task_id} успешно обработана")
            else:
                error_msg = result.get('error', 'Неизвестная ошибка')
                await self.kafka_manager.send_task_status(
                    urgent_task.task_id,
                    TaskStatus.FAILED,
                    f"Ошибка обработки компании: {error_msg}",
                    progress=100.0
                )
                log.error(f"URGENT задача {urgent_task.task_id} завершена с ошибкой: {error_msg}")
            
            return result
            
        except Exception as e:
            log.error(f"Критическая ошибка обработки URGENT задачи: {e}")
            await self.kafka_manager.send_task_status(
                urgent_task.task_id,
                TaskStatus.FAILED,
                f"Критическая ошибка: {str(e)}",
                progress=100.0
            )
            raise
    
    async def _process_remaining_urgent_tasks(self):
        """Обработка оставшихся urgent задач после завершения регулярных"""
        try:
            log.info("Проверка оставшихся urgent задач после завершения регулярных...")
            
            while True:
                urgent_task = await self.kafka_manager.get_urgent_task()
                if not urgent_task:
                    break
                
                log.info(f"Обработка оставшейся URGENT задачи: {urgent_task.task_id}")
                await self._process_urgent_task(urgent_task)
                
            log.info("Все urgent задачи обработаны")
            
        except Exception as e:
            log.error(f"Ошибка обработки оставшихся urgent задач: {e}")
    
    async def stop_gracefully(self):
        """Плавная остановка мониторинга"""
        self._stop_requested = True
        log.info("Graceful stop requested")
        
    async def _recover_from_crash(self, company_data: Dict, attempt: int = 1) -> bool:
        """Попытка восстановления после сбоя"""
        if attempt > self.config.recovery_max_attempts:
            return False
        
        log.warning(f"Попытка восстановления {attempt}/{self.config.recovery_max_attempts} для {company_data['original_name']}")
        
        try:
            # Очищаем временные данные
            self._global_processed_urls.clear()
            await self.crawler.temp_storage.cleanup_company_files(company_data['original_name'])
            
            # Переинициализируем краулер
            await self.close()
            await asyncio.sleep(5)
            await self.initialize()
            
            # Очищаем чекпоинт
            await self.checkpoint_manager.clear_checkpoint()
            return True
            
        except Exception as e:
            log.error(f"Ошибка восстановления (попытка {attempt}): {e}")
            return await self._recover_from_crash(company_data, attempt + 1)

async def main():
    config = Config.load_from_env()

    logs_dir = config.logs_dir    
    os.makedirs(logs_dir, exist_ok=True)
    log_file_path = os.path.join(logs_dir, "monitoring.log")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    # D101: пульс активности — по нему сторож зависаний отличает работу от простоя
    activity_heartbeat.install()
    
    log.info("Запуск системы мониторинга с приоритетной обработкой urgent задач")
    
    # Создаём KafkaManager с потребителем для urgent задач
    kafka_manager = KafkaTaskManager(config)
    await kafka_manager.initialize(need_urgent_consumer=True, need_status_consumer=False)

    monitoring_system = MonitoringSystem(config, kafka_manager)
    pipeline_orchestrator = PipelineOrchestrator(config, kafka_manager)
    
    try:
        await monitoring_system.initialize()
        await pipeline_orchestrator.initialize()
        companies = monitoring_system.read_company_list()
        
        # Чтение списка компаний
        companies = monitoring_system.read_company_list()
        log.info(f"Найдено {len(companies)} компаний для обработки")
        
        if not companies:
            log.error("Не найдено компаний для обработки")
            return
        
        await pipeline_orchestrator.run_with_priority(monitoring_system, companies)
        
        log.info("Мониторинг успешно завершен")
        
    except Exception as e:
        log.error(f"Критическая ошибка в системе мониторинга: {e}")
    finally:
        await pipeline_orchestrator.shutdown()
        await monitoring_system.close()
        await kafka_manager.close()

if __name__ == "__main__":
    asyncio.run(main())