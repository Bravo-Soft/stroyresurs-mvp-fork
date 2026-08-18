# graph_db_uploader.py
import asyncio
import time
import aiofiles
import json
import os
import logging
import re
import aiohttp
from datetime import datetime
from typing import Dict, List, Any, Optional
import uuid

from config import Config
from graph_db_archiver import GraphDBDataArchiver

# Настройка логирования
log = logging.getLogger("graph_db_uploader")

VERIFICATION_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _normalize_customer_symbols(obj):
    """Заказчик: скрипт загрузки (на стороне заказчика) не читает × ≥ ≤ и значок градуса.
    Рекурсивно заменяем в строковых значениях И ключах: × → русская х (U+0445), ≥ → >=, ≤ → <=,
    температура «60 °C» → «60 C», прочие градусы (угол «90°») удаляются.
    Синхронно с docx_generator._clean_text / text_extractor._postprocess_markdown (RTF-карточка):
    LLM возвращает ° обратно в specifications, поэтому карточку чистит рендер, а граф — эта функция.
    Применяется к данным ПЕРЕД отправкой в граф, чтобы граф-данные не содержали нечитаемых символов."""
    if isinstance(obj, str):
        s = obj.replace('×', 'х').replace('≥', '>=').replace('≤', '<=')
        # Прекомпозированные ℃/℉ разворачиваем (внутри есть буква, нельзя просто удалить).
        s = s.replace('℃', '°C').replace('℉', 'F')
        # Градус, набранный НУЛЁМ в суперскрипте (<sup>0</sup>С → «^0С»): это значок, не степень.
        s = re.sub(r'\^0(?=[ \t]*[СCсc](?![А-Яа-яёЁA-Za-z]))', '°', s)
        # Температура: градус (опц. с пробелами) + С/C → « C» (пробел + латинская C).
        s = re.sub('[ \t]*[°˚º⁰\u030a][ \t]*[СCсc](?![А-Яа-яёЁA-Za-z])', ' C', s)
        # Любой оставшийся значок градуса (угол «90°», «300°ВУ») удаляем полностью.
        return re.sub('[°˚º⁰\u030a]', '', s)
    if isinstance(obj, list):
        return [_normalize_customer_symbols(v) for v in obj]
    if isinstance(obj, dict):
        return {_normalize_customer_symbols(k): _normalize_customer_symbols(v) for k, v in obj.items()}
    return obj

class GraphDBFatalError(Exception):
    """Исключение, сигнализирующее о фатальной сетевой ошибке при отправке в Graph DB."""
    pass

class GraphDBPendingUploadManager:
    """Управление состоянием отложенных отправок в Graph DB."""

    def __init__(self, config: Config):
        self.config = config 
        self.pending_dir = config.graph_db_pending_dir
        os.makedirs(self.pending_dir, exist_ok=True)
        self._convert_old_pending_file(config)

    def _convert_old_pending_file(self, config: Config):
        """Конвертирует старый pending_graph_db_upload.json в новый формат."""
        old_file = os.path.join(config.base_dir, "pending_graph_db_upload.json")
        if not os.path.exists(old_file):
            return
        try:
            with open(old_file, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
            if not old_data:
                return  
            for entry in old_data:
                company_id = entry.get('company_id')
                if not company_id:
                    continue                
                # удаляем старый файл, т.к. при новом запуске система создаст свои pending состояния.
                pass
            # Переименовываем старый файл
            converted_file = old_file + ".converted"
            # Удаляем существующий файл, если он есть
            if os.path.exists(converted_file):
                os.remove(converted_file)
            os.rename(old_file, converted_file)
            log.info(f"Старый pending файл преобразован и сохранён как {converted_file}")
        except Exception as e:
            log.warning(f"Ошибка конвертации старого pending файла: {e}")

    def _get_state_path(self, company_id: str) -> str:
        return os.path.join(self.pending_dir, f"{company_id}.json")

    async def load_state(self, company_id: str) -> Optional[Dict[str, Any]]:
        path = self._get_state_path(company_id)
        if not os.path.exists(path):
            return None
        try:
            async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                return json.loads(await f.read())
        except Exception as e:
            log.error(f"Ошибка загрузки состояния для {company_id}: {e}")
            return None

    async def save_state(self, state: Dict[str, Any]):
        company_id = state.get('company_id')
        if not company_id:
            log.error("Нет company_id в состоянии")
            return
        path = self._get_state_path(company_id)
        try:
            async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(state, ensure_ascii=False, indent=2, default=str))
            log.debug(f"Сохранено состояние для {company_id}")
        except Exception as e:
            log.error(f"Ошибка сохранения состояния для {company_id}: {e}")

    async def delete_state(self, company_id: str):
        path = self._get_state_path(company_id)
        try:
            if os.path.exists(path):
                os.remove(path)
                log.debug(f"Удалено состояние для {company_id}")
        except Exception as e:
            log.error(f"Ошибка удаления состояния для {company_id}: {e}")

    async def list_pending_companies(self) -> List[str]:
        """Возвращает список company_id с неотправленными чанками."""
        pending = []
        try:
            for fname in os.listdir(self.pending_dir):
                if fname.endswith('.json'):
                    company_id = fname[:-5]
                    pending.append(company_id)
        except Exception as e:
            log.error(f"Ошибка списка pending компаний: {e}")
        return pending

    async def update_chunk_status(self, company_id: str, data_type: str, chunk_num: int,
                                  status: str, response_data: Optional[Dict] = None,
                                  increase_retry: bool = False):
        """Обновляет статус чанка (pending/success/failed) в состоянии."""
        state = await self.load_state(company_id)
        if not state:
            log.warning(f"Нет состояния для {company_id} при обновлении статуса")
            return
        
        if data_type == "company":
            state['pending']['company']['status'] = status
            if response_data:
                state['pending']['company']['response'] = response_data
        elif data_type == "products":
            for chunk in state['pending']['products']:
                if chunk['chunk_num'] == chunk_num:
                    chunk['status'] = status
                    if response_data:
                        chunk['response'] = response_data
                    if increase_retry and status == 'failed':
                        chunk['retry_count'] = chunk.get('retry_count', 0) + 1
                    break
        elif data_type == "distributors":
            for chunk in state['pending']['distributors']:
                if chunk['chunk_num'] == chunk_num:
                    chunk['status'] = status
                    if response_data:
                        chunk['response'] = response_data
                    if increase_retry and status == 'failed':
                        chunk['retry_count'] = chunk.get('retry_count', 0) + 1
                    break
                
        state['last_updated'] = datetime.now().isoformat()
        await self.save_state(state)

    async def create_or_load_state(self, company_id: str, company_name: str, is_update: bool,
                                   company_info: Dict, products: List, distributors: List,
                                   company_files: List) -> Dict[str, Any]:
        """Создаёт новое состояние или возвращает существующее."""
        state = await self.load_state(company_id)
        if state:
            for chunk in state['pending']['products']:
                if 'retry_count' not in chunk:
                    chunk['retry_count'] = 0
            for chunk in state['pending']['distributors']:
                if 'retry_count' not in chunk:
                    chunk['retry_count'] = 0
            log.info(f"Загружено существующее состояние для {company_id}, retry_count={state.get('retry_count',0)}")
            return state

        # Создаём новое состояние
        product_chunks = []
        if products and self.config.graph_db_enable_chunking:
            chunk_size = self.config.graph_db_chunk_size_products
            chunks = [products[i:i+chunk_size] for i in range(0, len(products), chunk_size)]
            for idx, chunk in enumerate(chunks, 1):
                product_chunks.append({
                    'chunk_num': idx,
                    'total_chunks': len(chunks),
                    'status': 'pending',
                    'data': chunk,
                    'retry_count': 0
                })
        else:
            if products:
                product_chunks.append({'chunk_num': 1, 'total_chunks': 1, 'status': 'pending', 'data': products, 'retry_count': 0})

        distributor_chunks = []
        if distributors and self.config.graph_db_enable_chunking:
            chunk_size = self.config.graph_db_chunk_size_distributors
            chunks = [distributors[i:i+chunk_size] for i in range(0, len(distributors), chunk_size)]
            for idx, chunk in enumerate(chunks, 1):
                distributor_chunks.append({
                    'chunk_num': idx,
                    'total_chunks': len(chunks),
                    'status': 'pending',
                    'data': chunk,
                    'retry_count': 0
                })
        else:
            if distributors:
                distributor_chunks.append({'chunk_num': 1, 'total_chunks': 1, 'status': 'pending', 'data': distributors, 'retry_count': 0})

        state = {
            'company_id': company_id,
            'company_name': company_name,
            'is_update': is_update,
            'created_at': datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat(),
            'retry_count': 0,
            'pending': {
                'company': {
                    'status': 'pending' if company_info else 'skipped',
                    'data': company_info
                },
                'products': product_chunks,
                'distributors': distributor_chunks,
                'company_files': company_files
            }
        }
        await self.save_state(state)
        return state

class GraphDBDataFormatter:
    """Форматирование данных для Graph DB API"""  
    
    @staticmethod
    def format_company_data(company_data: Dict[str, str], company_info: Dict[str, Any], 
                      company_files: List[Dict[str, Any]], 
                      is_update: bool = False) -> Dict[str, Any]:
        """Форматирование данных компании для API"""
        
        # Извлекаем AI данные компании
        ai_company_info = company_info.get('company', {}) if isinstance(company_info, dict) else {}
        if not ai_company_info and isinstance(company_info, dict) and 'company' not in company_info:
            # Если структура другая, используем сам company_info как данные компании
            ai_company_info = company_info
        
        current_time = datetime.now().strftime(VERIFICATION_DATE_FMT)

        # Базовые данные компании
        formatted_company = {
            "id": company_data.get('company_id', ''),
            "Наименование компании": "",
            "Представительство в РФ": "",
            "Адрес": [],
            "Телефон": [],
            "E-mail": [],
            "Website": company_data.get('website', ''),
            "Описание": "",
            "Реквизиты": {},
            "files": [],
            "report_date": "",
            "status": "активен"
        }
        
        # Только для обновлений добавляем update_date
        if is_update:
            formatted_company["update_date"] = current_time
        else:
            # Для первичной загрузки добавляем оба поля
            formatted_company["upload_date"] = current_time
            formatted_company["update_date"] = ""

        # Наименование
        name_data = ai_company_info.get('Наименование компании', company_data.get('original_name', ''))
        if name_data:
            formatted_company["Наименование компании"] = name_data.strip()

        # Представительство
        representative_office = ai_company_info.get('Представительство в РФ', ai_company_info.get('Представительство в РФ', ''))
        if representative_office:
            formatted_company["Представительство в РФ"] = representative_office.strip()
            
        # Адрес
        address_data = ai_company_info.get('Адрес', ai_company_info.get('address', ''))
        if isinstance(address_data, str) and address_data:
            addresses = []
            for line in address_data.split('\n'):
                line = line.strip()
                if line and line not in addresses:
                    addresses.append(line)
            formatted_company["Адрес"] = addresses
        elif isinstance(address_data, list):
            formatted_company["Адрес"] = [addr.strip() for addr in address_data if addr.strip()]
        
        # Телефоны
        phone_data = ai_company_info.get('Телефон', ai_company_info.get('phone', ''))
        if isinstance(phone_data, str) and phone_data:
            phones = []
            for phone in re.split(r'[,;\n]+', phone_data):
                phone = phone.strip()
                if phone and phone not in phones:
                    phones.append(phone)
            formatted_company["Телефон"] = phones
        elif isinstance(phone_data, list):
            formatted_company["Телефон"] = [phone.strip() for phone in phone_data if phone.strip()]   
        
        # E-mail
        email_data = ai_company_info.get('E-mail', ai_company_info.get('email', ''))
        if isinstance(email_data, str) and email_data:
            emails = []
            for email in re.split(r'[,;\n]+', email_data):
                email = email.strip()
                if email and email not in emails:
                    emails.append(email)
            formatted_company["E-mail"] = emails
        elif isinstance(email_data, list):
            formatted_company["E-mail"] = [email.strip() for email in email_data if email.strip()]
        
        # Описание
        description = ai_company_info.get('Описание', ai_company_info.get('description', ''))
        if description:
            formatted_company["Описание"] = description.strip()
        
        # Реквизиты
        requisites = ai_company_info.get('Реквизиты', {})
        if not requisites or not isinstance(requisites, dict):
            requisites = {}
            for req_key in ['ИНН', 'ОГРН', 'КПП']:
                if req_key in ai_company_info:
                    requisites[req_key] = ai_company_info[req_key]
        
        formatted_company["Реквизиты"] = {
            "ИНН": requisites.get('ИНН', '').strip(),
            "ОГРН": requisites.get('ОГРН', '').strip(),
            "КПП": requisites.get('КПП', '').strip()
        }
        
        # Если website не найден в company_data, пробуем найти в AI данных
        if not formatted_company["Website"]:
            website = ai_company_info.get('Website', ai_company_info.get('website', ''))
            if website:
                formatted_company["Website"] = website.strip()
        
        # Добавляем файлы компании
        if company_files:
            formatted_files = []
            for file_info in company_files:
                if isinstance(file_info, dict) and 'id' in file_info:
                    formatted_file = {
                        "id": file_info['id'],
                        "path": file_info.get('path', ''),
                        "hash": file_info.get('hash', ''),
                        "download_date": file_info.get('download_date', '')
                    }
                    formatted_files.append(formatted_file)
            
            if formatted_files:
                formatted_company["files"] = formatted_files
        
        return _normalize_customer_symbols({
            "companies": [formatted_company],
            "verification_date": current_time,
            "is_update": is_update
        })
       
    @staticmethod
    def format_product_data(company_id: str, company_name: str, products: List[Dict[str, Any]], 
                          is_update: bool = False) -> Dict[str, Any]:
        """Форматирование данных продукта для API"""
        
        formatted_products = []
        current_time = datetime.now().strftime(VERIFICATION_DATE_FMT)

        for product in products:
            # Извлекаем AI данные продукта
            ai_data = product.get('ai_raw_text', {})
            if isinstance(ai_data, str):
                try:
                    ai_data = json.loads(ai_data)
                except:
                    ai_data = {}
            elif not isinstance(ai_data, dict):
                ai_data = {}
            
            product_data = ai_data.get('product', {})
            
            formatted_product = {
            "id": product.get('product_id', str(uuid.uuid4().int)[:16]),
            "product_name": product_data.get('product_name', product.get('name', '')),
            "tu": product_data.get('tu', ''),
            "price": product_data.get('price', ''),
            "description": product_data.get('description', ''),
            "specifications": product_data.get('specifications', {}),
            "colors": product_data.get('colors', []),
            "source_url": product.get('source_url', ''),            
            "complectation": product_data.get('complectation', ''),
            "compatibility": product_data.get('compatibility', ''),
            "applications": product_data.get('applications', ''),
            "advantages": product_data.get('advantages', ''),
            "instructions_manuals": product_data.get('instructions_manuals', ''),
            "exploitation": product_data.get('exploitation', ''),
            "storage": product_data.get('storage', ''),
            "security_measures": product_data.get('security_measures', ''),
            "dimensions": product_data.get('dimensions', ''),
            "weight": product_data.get('weight', ''),
            "variants": product_data.get('variants', []),  # список объектов
            "report_date": ""
        }
            
            # Только для обновлений добавляем update_date
            if is_update:
                formatted_product["update_date"] = current_time
            else:
                # Для первичной загрузки добавляем оба поля
                formatted_product["upload_date"] = current_time
                formatted_product["update_date"] = ""
            
            # Добавляем файлы продукта
            product_files = product.get('files', [])
            if product_files:
                formatted_files = []
                for file_info in product_files:
                    if isinstance(file_info, dict) and 'id' in file_info:
                        formatted_file = {
                            "id": file_info['id'],
                            "path": file_info.get('path', ''),
                            "hash": file_info.get('hash', ''),
                            "download_date": file_info.get('download_date', '')
                        }
                        formatted_files.append(formatted_file)
                
                if formatted_files:
                    formatted_product["files"] = formatted_files
            
            formatted_products.append(formatted_product)
        
        company_obj = {
            "id": company_id,
            "products": formatted_products
        }
        
        return _normalize_customer_symbols({
            "companies": [company_obj],
            "verification_date": current_time,
            "is_update": is_update
        })
    
    @staticmethod
    def format_distributor_data(company_id: str, company_name: str, 
                              distributors: List[Dict[str, Any]], 
                              is_update: bool = False) -> Dict[str, Any]:
        """Форматирование данных дистрибьюторов для API"""
        
        formatted_distributors = []
        
        current_time = datetime.now().strftime(VERIFICATION_DATE_FMT)

        for distributor in distributors:
            formatted_distributor = {
                "id": distributor.get('distributor_id', str(uuid.uuid4().int)[:8]),
                "Наименование": distributor.get('name', ''),
                "Регион": distributor.get('region', ''),
                "Адрес": distributor.get('address', '')[:200],
                "Телефон": distributor.get('phone', ''),
                "URL страницы": distributor.get('page_url', ''),
                "report_date": ""
            }
            
            # Только для обновлений добавляем update_date
            if is_update:
                formatted_distributor["update_date"] = current_time
            else:
                # Для первичной загрузки добавляем оба поля
                formatted_distributor["upload_date"] = current_time
                formatted_distributor["update_date"] = ""
            
            # Добавляем дополнительные поля если есть
            if distributor.get('email'):
                formatted_distributor["E-mail"] = distributor.get('email')
            
            formatted_distributors.append(formatted_distributor)
        
        company_obj = {
            "id": company_id,           
            "suppliers": formatted_distributors
        }
        
        return _normalize_customer_symbols({
            "companies": [company_obj],
            "verification_date": current_time,
            "is_update": is_update
        })

class GraphDBJSONSaver:
    """Сохранение данных в JSON файлы при ошибках"""
    
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
    
    def save_failed_data(self, data_type: str, company_id: str, data: Dict[str, Any], 
                       chunk_num: int = 1, error: str = "") -> str:
        """Сохранение неудачных данных в JSON файл"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Создаем копию данных, чтобы не изменять оригинал
        data_with_error = data.copy()
        
        # Добавляем информацию об ошибке
        data_with_error['error_info'] = {
            'error': error,
            'timestamp': timestamp,
            'data_type': data_type,
            'chunk_num': chunk_num
        }
        
        filename = f"failed_{data_type}_{company_id}_chunk{chunk_num:03d}_{timestamp}.json"
        filepath = os.path.join(self.output_dir, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_with_error, f, ensure_ascii=False, indent=2)
            
            log.info(f"Сохранен файл с ошибкой: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Ошибка сохранения файла с ошибкой: {e}")
            return ""

class GraphDBUploader:
    """Класс для загрузки данных в Graph DB с архивацией"""
    
    def __init__(self, config: Config, processing_tracker=None):
        self.config = config        
        self.processing_tracker = processing_tracker
        self.api_url = f"{config.graph_db_api_url.rstrip('/')}/add"
        self.max_retries = config.graph_db_max_retries
        self.request_timeout = config.graph_db_request_timeout
        
        self.formatter = GraphDBDataFormatter()
        self.json_saver = GraphDBJSONSaver(config.graph_db_json_output_dir)
        self.archiver = GraphDBDataArchiver(config.graph_db_archive_dir)
        self.pending_manager = GraphDBPendingUploadManager(config)
        log.info(f"Инициализирован Graph DB архиватор в директории: {config.graph_db_archive_dir}")
        
        # Статистика
        self.stats = {
            'companies_sent': 0,
            'products_sent': 0,
            'distributors_sent': 0,
            'files_sent': 0,
            'successful_sends': 0,
            'failed_sends': 0,
            'chunks_created': 0,
            'archived_files': 0,
            'updates_performed': 0
        }
        
        # Кэш для хранения информации о чанках перед отправкой
        self._chunks_cache = {}
    
    def _is_network_error(self, error: Exception, response_code: int = None) -> bool:
        """
        Определяет, является ли ошибка сетевой (требует ретрая и фатальна после исчерпания попыток).
        """
        # Исключения клиента aiohttp, таймауты, ошибки соединения
        if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError)):
            return True
        # HTTP статусы 5xx (500, 502, 503, 504) и 429 считаем временными ошибками
        if response_code is not None and (response_code >= 500 or response_code == 429):
            return True
        return False
    
    async def send_data(self, data: Dict[str, Any], data_type: str, company_id: str,
                  company_name: str, chunk_num: int = 1, total_chunks: int = 1) -> bool:
        """Отправка данных на API с архивацией и улучшенной обработкой сетевых ошибок (включая 5xx, 429)."""
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }

        # 1. Архивируем данные перед отправкой
        archive_metadata = {
            "api_url": self.api_url,
            "data_type": data_type,
            "company_id": company_id,
            "company_name": company_name,
            "chunk_num": chunk_num,
            "total_chunks": total_chunks,
            "pre_send_timestamp": datetime.now().isoformat()
        }

        if data_type == "company":
            archive_path = self.archiver.archive_company_data(
                data, company_id, company_name, archive_metadata
            )
        elif data_type == "products":
            archive_path = self.archiver.archive_products_data(
                data, company_id, company_name, chunk_num, total_chunks, archive_metadata
            )
        elif data_type == "distributors":
            archive_path = self.archiver.archive_distributors_data(
                data, company_id, company_name, chunk_num, total_chunks, archive_metadata
            )
        else:
            archive_path = None

        if archive_path:
            self.stats['archived_files'] += 1
            log.info(f"Данные архивированы перед отправкой: {os.path.basename(archive_path)}")

        # Проверяем размер данных перед отправкой
        data_size = len(json.dumps(data).encode('utf-8'))
        log.info(f"Размер данных для отправки: {data_size / 1024:.2f} KB")

        if data_size > 1 * 1024 * 1024:  # Если больше 1MB
            log.warning(f"Данные слишком большие ({data_size / 1024 / 1024:.2f} MB)")

        last_error = ""
        response_code = None
        is_network_error_occurred = False
        _g_t0 = time.perf_counter()
        _g_backoff = 0.0

        for attempt in range(self.max_retries):
            try:
                log.info(f"Отправка данных на {self.api_url} (попытка {attempt + 1}/{self.max_retries})")

                # Логируем попытку отправки
                self.archiver.log_upload_attempt(
                    data_type=data_type,
                    company_id=company_id,
                    company_name=company_name,
                    attempt_num=attempt + 1,
                    total_attempts=self.max_retries,
                    success=False,
                    error_message="",
                    response_code=None
                )

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.api_url,
                        headers=headers,
                        json=data,
                        timeout=aiohttp.ClientTimeout(total=self.request_timeout)
                    ) as response:

                        response_code = response.status
                        response_text = await response.text()

                        if response.status == 200:
                            # Архивируем успешную отправку
                            response_info = {
                                "status_code": response.status,
                                "response_text": response_text[:500],
                                "attempts": attempt + 1,
                                "data_size_bytes": data_size
                            }

                            self.archiver.archive_sent_data(
                                data, data_type, company_id, company_name, response_info
                            )

                            # Обновляем лог
                            self.archiver.log_upload_attempt(
                                data_type=data_type,
                                company_id=company_id,
                                company_name=company_name,
                                attempt_num=attempt + 1,
                                total_attempts=self.max_retries,
                                success=True,
                                error_message="",
                                response_code=response.status
                            )

                            log.info(f"Успешный ответ от API: {response_text[:200]}...")
                            _g_tr = self.processing_tracker
                            if _g_tr:
                                _g_tr.add_clean('graph', time.perf_counter() - _g_t0 - _g_backoff)
                                if _g_backoff > 0:
                                    _g_tr.add_retry('graph', _g_backoff)
                            return True
                        else:
                            last_error = f"Ошибка API (статус {response.status}): {response_text}"
                            log.error(last_error)
                            # Считаем сетевыми ошибками: 5xx (включая 500) и 429 (Too Many Requests)
                            if self._is_network_error(None, response.status):
                                is_network_error_occurred = True
                            else:
                                # Не сетевая ошибка (например, 400, 404, 422) – не ретраим, сразу выходим
                                break

            except aiohttp.ClientError as e:
                last_error = f"Ошибка сети при отправке данных: {e}"
                log.error(last_error)
                is_network_error_occurred = True
            except asyncio.TimeoutError as e:
                last_error = f"Таймаут при отправке данных (попытка {attempt + 1})"
                log.error(last_error)
                is_network_error_occurred = True
            except ConnectionError as e:
                last_error = f"Ошибка соединения: {e}"
                log.error(last_error)
                is_network_error_occurred = True
            except Exception as e:
                last_error = f"Неожиданная ошибка при отправке данных: {e}"
                log.error(last_error)
                # Неожиданные ошибки не считаем сетевыми, не ретраим
                break

            # Если это сетевая ошибка (включая 5xx, 429) и есть ещё попытки, ждём и ретраим
            if is_network_error_occurred and attempt < self.max_retries - 1:
                delay = 2 ** attempt
                log.info(f"Сетевая ошибка (статус {response_code}), повтор через {delay} секунд (попытка {attempt + 2}/{self.max_retries})")
                _g_bt = time.perf_counter()
                await asyncio.sleep(delay)
                _g_backoff += time.perf_counter() - _g_bt
                continue
            else:
                # Попытки кончились или ошибка не сетевая – выходим из цикла
                break

        # Если все попытки неудачны, архивируем как неудачные данные
        error_info = {
            "error": last_error,
            "response_code": response_code,
            "max_attempts": self.max_retries,
            "data_size_bytes": data_size
        }

        self.archiver.archive_failed_data(
            data, data_type, company_id, company_name, error_info
        )

        # Если была сетевая ошибка и попытки исчерпаны – фатальная ошибка (требует остановки пайплайна)
        if is_network_error_occurred:
            log.critical(f"Фатальная сетевая ошибка после {self.max_retries} попыток: {last_error}")
            raise GraphDBFatalError(
                f"Фатальная сетевая ошибка Graph DB ({data_type}): {last_error}"
            )

        return False

    def split_into_chunks(self, data: List[Any], chunk_size: int) -> List[List[Any]]:
        """Разбиение данных на чанки"""
        if not data:
            return []
        
        chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
        
        # Сохраняем информацию о чанках для архивации
        for i, chunk in enumerate(chunks):
            chunk_key = f"chunk_{i+1}_of_{len(chunks)}"
            self._chunks_cache[chunk_key] = {
                "chunk_number": i + 1,
                "total_chunks": len(chunks),
                "items_in_chunk": len(chunk),
                "created_at": datetime.now().isoformat()
            }
        
        return chunks
    
    def get_current_verification_date(self) -> str:
        """Получение текущей даты и времени для verification_date"""
        return datetime.now().strftime(VERIFICATION_DATE_FMT)
    
    async def upload_company_data(self, company_data: Dict[str, str], company_info: Dict[str, Any],
                              products: List[Dict[str, Any]], distributors: List[Dict[str, Any]],
                              company_files: List[Dict[str, Any]], is_update: bool = False) -> bool:
        company_id = company_data.get('company_id', '')
        company_name = company_data.get('actual_name', company_data.get('original_name', ''))

        state = await self.pending_manager.create_or_load_state(
            company_id, company_name, is_update, company_info, products, distributors, company_files
        )

        overall_success = True
        had_fatal = False

        # ---- Отправка компании (статусы pending/failed -> отправляем) ----
        if self.config.graph_db_upload_company and company_info:
            pending_company = state['pending']['company']
            if pending_company['status'] in ('pending', 'failed'):
                log.info(f"Отправка данных компании (статус: {pending_company['status']})...")
                company_json = self.formatter.format_company_data(company_data, company_info, company_files, is_update)
                try:
                    success = await self.send_data(company_json, "company", company_id, company_name)
                    if success:
                        await self.pending_manager.update_chunk_status(company_id, "company", 1, "success")
                        self.stats['successful_sends'] += 1
                        self.stats['companies_sent'] += 1
                    else:
                        await self.pending_manager.update_chunk_status(company_id, "company", 1, "failed", increase_retry=True)
                        overall_success = False
                        self.stats['failed_sends'] += 1
                        self.json_saver.save_failed_data("company", company_id, company_json, error="Ошибка отправки компании")
                except GraphDBFatalError as e:
                    log.error(f"Фатальная сетевая ошибка при отправке компании: {e}")
                    await self.pending_manager.update_chunk_status(company_id, "company", 1, "failed", increase_retry=True)
                    overall_success = False
                    had_fatal = True
            elif pending_company['status'] == 'skipped':
                log.info("Данные компании отсутствуют (skipped), пропускаем")
            else:
                log.info(f"Данные компании уже успешно отправлены (status={pending_company['status']})")

        # ---- Отправка продуктов по чанкам ----
        if self.config.graph_db_upload_products and products:
            for chunk_info in state['pending']['products']:
                chunk_num = chunk_info['chunk_num']
                # Пропускаем уже успешные или превысившие лимит попыток
                if chunk_info['status'] == 'success':
                    log.debug(f"Чанк продуктов {chunk_num}/{chunk_info['total_chunks']} уже отправлен, пропускаем")
                    continue
                retry_limit = self.config.graph_db_max_retries_on_resume
                current_retries = chunk_info.get('retry_count', 0)
                if current_retries >= retry_limit:
                    log.error(f"Чанк продуктов {chunk_num} превысил лимит повторных попыток ({retry_limit}) — данные НЕ доставлены в граф")
                    overall_success = False
                    continue

                log.info(f"Отправка чанка продуктов {chunk_num}/{chunk_info['total_chunks']} ({len(chunk_info['data'])} продуктов), попытка {current_retries+1}")
                products_json = self.formatter.format_product_data(company_id, company_name, chunk_info['data'], is_update)
                try:
                    success = await self.send_data(products_json, "products", company_id, company_name,
                                                chunk_num, chunk_info['total_chunks'])
                    if success:
                        await self.pending_manager.update_chunk_status(company_id, "products", chunk_num, "success")
                        self.stats['successful_sends'] += 1
                        self.stats['products_sent'] += len(chunk_info['data'])
                    else:
                        await self.pending_manager.update_chunk_status(company_id, "products", chunk_num, "failed", increase_retry=True)
                        overall_success = False
                        self.stats['failed_sends'] += 1
                        self.json_saver.save_failed_data("products", company_id, products_json, chunk_num,
                                                        error="Ошибка отправки чанка продуктов")
                except GraphDBFatalError as e:
                    log.error(f"Фатальная сетевая ошибка при отправке чанка продуктов {chunk_num}: {e}")
                    await self.pending_manager.update_chunk_status(company_id, "products", chunk_num, "failed", increase_retry=True)
                    overall_success = False
                    had_fatal = True
                await asyncio.sleep(self.config.graph_db_delay_between_chunks)

        # ---- Отправка дистрибьюторов (аналогично) ----
        if self.config.graph_db_upload_distributors and distributors:
            for chunk_info in state['pending']['distributors']:
                chunk_num = chunk_info['chunk_num']
                if chunk_info['status'] == 'success':
                    log.debug(f"Чанк дистрибьюторов {chunk_num}/{chunk_info['total_chunks']} уже отправлен, пропускаем")
                    continue
                retry_limit = self.config.graph_db_max_retries_on_resume
                current_retries = chunk_info.get('retry_count', 0)
                if current_retries >= retry_limit:
                    log.error(f"Чанк дистрибьюторов {chunk_num} превысил лимит повторных попыток ({retry_limit}) — данные НЕ доставлены в граф")
                    overall_success = False
                    continue

                log.info(f"Отправка чанка дистрибьюторов {chunk_num}/{chunk_info['total_chunks']} ({len(chunk_info['data'])} дистрибьюторов), попытка {current_retries+1}")
                distributors_json = self.formatter.format_distributor_data(company_id, company_name, chunk_info['data'], is_update)
                try:
                    success = await self.send_data(distributors_json, "distributors", company_id, company_name,
                                                chunk_num, chunk_info['total_chunks'])
                    if success:
                        await self.pending_manager.update_chunk_status(company_id, "distributors", chunk_num, "success")
                        self.stats['successful_sends'] += 1
                        self.stats['distributors_sent'] += len(chunk_info['data'])
                    else:
                        await self.pending_manager.update_chunk_status(company_id, "distributors", chunk_num, "failed", increase_retry=True)
                        overall_success = False
                        self.stats['failed_sends'] += 1
                        self.json_saver.save_failed_data("distributors", company_id, distributors_json, chunk_num,
                                                        error="Ошибка отправки чанка дистрибьюторов")
                except GraphDBFatalError as e:
                    log.error(f"Фатальная сетевая ошибка при отправке чанка дистрибьюторов {chunk_num}: {e}")
                    await self.pending_manager.update_chunk_status(company_id, "distributors", chunk_num, "failed", increase_retry=True)
                    overall_success = False
                    had_fatal = True
                await asyncio.sleep(self.config.graph_db_delay_between_chunks)

        # ---- Статистика файлов ----
        total_files = len(company_files)
        for product in products:
            total_files += len(product.get('files', []))
        self.stats['files_sent'] = total_files

        # ---- Удаление состояния или сохранение (без увеличения общего retry_count) ----
        if overall_success:
            await self.pending_manager.delete_state(company_id)
            log.info(f"Все данные для {company_id} успешно отправлены, состояние удалено")
        else:
            # Общий retry_count больше не используем для блокировки, но увеличиваем для статистики (необязательно)
            # Чтобы не зацикливать компанию, пропускаем увеличение – иначе компания может быть заблокирована.
            # Просто обновляем last_updated
            state['last_updated'] = datetime.now().isoformat()
            await self.pending_manager.save_state(state)
            log.warning(f"Отправка для {company_id} завершена с ошибками, состояние сохранено для дальнейших попыток")

        # Если была фатальная сетевая ошибка – сигнализируем о необходимости остановки пайплайна
        if had_fatal:
            raise GraphDBFatalError(
                f"Фатальная сетевая ошибка при отправке данных компании {company_id}; "
                f"состояние сохранено в pending для повторной отправки"
            )
        # True только если ВСЕ данные доставлены в граф
        return overall_success

    async def resume_all_pending_uploads(self) -> bool:
        pending_companies = await self.pending_manager.list_pending_companies()
        if not pending_companies:
            log.info("Нет отложенных отправок для восстановления")
            return True

        log.info(f"Начинаем восстановление отправки для {len(pending_companies)} компаний")
        for company_id in pending_companies:
            state = await self.pending_manager.load_state(company_id)
            if not state:
                continue
            company_name = state.get('company_name', company_id)
            log.info(f"Восстановление отправки для компании {company_name} (retry_count={state.get('retry_count',0)})")

            company_data = {
                'company_id': company_id,
                'original_name': company_name,
                'actual_name': company_name,
                'website': state.get('website', '')
            }
            all_products = []
            for chunk in state['pending']['products']:
                all_products.extend(chunk['data'])
            all_distributors = []
            for chunk in state['pending']['distributors']:
                all_distributors.extend(chunk['data'])
            company_info = state['pending']['company']['data'] if state['pending']['company']['data'] else {}
            company_files = state['pending']['company_files']

            try:
                success = await self.upload_company_data(
                    company_data, company_info, all_products, all_distributors, company_files,
                    is_update=state.get('is_update', False)
                )
                if not success:
                    log.error(f"Не удалось восстановить отправку для {company_name}, но продолжаем со следующей компанией")
                    # Не прерываем пайплайн из-за одной компании
                    continue
            except GraphDBFatalError as e:
                log.critical(f"Фатальная сетевая ошибка при восстановлении {company_name}: {e}")
                return False
            except Exception as e:
                log.critical(f"Неожиданная ошибка при восстановлении {company_name}: {e}")
                return False

            await asyncio.sleep(self.config.graph_db_delay_between_companies)

        log.info("Восстановление отправки завершено (частично успешно или полностью)")
        return True
    
    def print_statistics(self):
        """Вывод статистики загрузки"""
        stats = f"""
============================================================
СТАТИСТИКА GRAPH DB ЗАГРУЗКИ
============================================================
Компаний отправлено: {self.stats['companies_sent']}
Продуктов отправлено: {self.stats['products_sent']}
Дистрибьюторов отправлено: {self.stats['distributors_sent']}
Файлов отправлено: {self.stats['files_sent']}
Выполнено обновлений: {self.stats['updates_performed']}
Создано чанков: {self.stats['chunks_created']}
Успешных отправок: {self.stats['successful_sends']}
Неудачных отправок: {self.stats['failed_sends']}
Архивировано файлов: {self.stats['archived_files']}
============================================================
"""
        print(stats)
        log.info(stats)
        
        # Вывод статистики архива
        archive_stats = self.archiver.get_archive_stats()
        if archive_stats:
            archive_info = f"""
============================================================
СТАТИСТИКА АРХИВА GRAPH DB
============================================================
Всего архивировано файлов: {archive_stats.get('total_archived', 0)}
Успешно отправленных: {archive_stats.get('sent_count', 0)}
Неудачных отправок: {archive_stats.get('failed_count', 0)}
Общий размер архива: {archive_stats.get('total_size_mb', 0):.2f} MB
------------------------------------------------------------
По типам данных:
  Компании: {archive_stats.get('by_type', {}).get('companies', 0)}
  Продукты: {archive_stats.get('by_type', {}).get('products', 0)}
  Дистрибьюторы: {archive_stats.get('by_type', {}).get('distributors', 0)}
  Ошибки: {archive_stats.get('by_type', {}).get('failed', 0)}
============================================================
"""
            print(archive_info)
            log.info(archive_info)
    
    def get_archive_summary(self) -> Dict[str, Any]:
        """Получение сводной информации об архиве"""
        return self.archiver.get_archive_stats()
    
    def export_archive_log(self, output_file: str = None) -> str:
        """Экспорт лога архива в CSV файл"""
        import csv
        from datetime import datetime
        
        if not output_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = os.path.join(self.archiver.archive_dir, f"archive_export_{timestamp}.csv")
        
        log_file = os.path.join(self.archiver.archive_dir, "logs", "upload_log.jsonl")
        
        if not os.path.exists(log_file):
            log.warning(f"Лог файл не найден: {log_file}")
            return ""
        
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = [json.loads(line) for line in f if line.strip()]
            
            if not logs:
                log.warning("Лог файл пуст")
                return ""
            
            # Создаем CSV файл
            with open(output_file, 'w', encoding='utf-8', newline='') as f:
                fieldnames = ['timestamp', 'data_type', 'company_id', 'company_name', 
                            'attempt', 'total_attempts', 'success', 'error_message', 
                            'response_code']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(logs)
            
            log.info(f"Лог архива экспортирован в: {output_file}")
            return output_file
            
        except Exception as e:
            log.error(f"Ошибка экспорта лога архива: {e}")
            return ""