# temp_storage_manager.py 1.0.0
import json
from site_profiles.quick_check import emit as quick_check_event
import asyncio
from uuid import uuid4
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple, Callable
from pathlib import Path
import hashlib
import aiofiles

from html_comparison import COMPARISON_VERSION, html_comparison_hash

from product_interactions import product_state_key
from config import Config
from product_utils import sanitize_filename

config = Config()

log = logging.getLogger("temp_storage")

from product_extraction_revision import NUMERIC_GATE_REVISION, product_extraction_revision

class TempStorageManager:
    """Менеджер временного хранилища для очищенного HTML с разделением по папками"""
    
    def __init__(self, base_dir: str = "temp_html_storage", max_size_mb: int | None = None, config: Config = None,
                 url_normalizer: Optional[Callable[[str], str]] = None):
        self.base_dir = Path(base_dir)
        self.max_size_mb = max_size_mb if max_size_mb is not None else config.temp_storage_max_size_mb
        self.max_size_bytes = self.max_size_mb * 1024 * 1024
        self.config = config
        self.crawl_run_id = None
        self._write_lock = asyncio.Lock()
        self.url_normalizer = url_normalizer
        self._url_index = {}
        self._url_index_paths = {}
        self._url_index_lock = asyncio.Lock()
        
        self.base_dir.mkdir(exist_ok=True)
        self.folder_mapping = {             
            "product": "Products",
            "not_product": "Not_products",
            "company_info": "Company_info",      
            "distributor_info": "Distributor_info" 
        }
        log.info(f"Инициализировано временное хранилище: {self.base_dir}, макс. размер: {self.max_size_mb}MB")

    def _get_company_subdir(self, company_name: str, category: str) -> Path:
        """Получить путь к поддиректории компании по категории"""
        company_dir = self.base_dir / self._sanitize_filename(company_name)        
        
        if category == 'product':
            subdir = company_dir / "Product_pages"
        elif category == 'distributor':
            subdir = company_dir / "Distributor_pages"
        elif category in ['contacts', 'main_page']:
            subdir = company_dir / "Company_pages"           
        else:
            subdir = company_dir / "Other_pages"
                    
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir

    def _generate_filename(self, normalized_url: str, content_hash: str) -> str:
        """Генерация имени файла на основе нормализованного URL и хеша контента"""
        url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
        content_hash_short = content_hash[:16]
        safe_url = self._sanitize_filename(normalized_url[:50])
        return f"{safe_url}_{url_hash}_{content_hash_short}"

    def reset_url_index(self):
        """Следующий доступ заново увидит архив и применит актуальные правила URL."""
        self._url_index.clear()
        self._url_index_paths.clear()

    def _metadata_identity(self, metadata: Dict) -> str:
        # Старый normalized_url мог потерять /ru, query или регистр пути.
        address = metadata.get('url') or metadata.get('normalized_url') or ''
        if self.url_normalizer:
            return self.url_normalizer(address)
        address = address.strip()
        key = product_state_key(address)
        return address.split('#', 1)[0] + ('#' + key if key else '')

    def _current_metadata(self, metadata: Dict) -> Dict:
        result = dict(metadata)
        identity = self._metadata_identity(metadata)
        if identity:
            result['normalized_url'] = identity
        return result

    async def _ensure_url_index(self, subdir: Path):
        async with self._url_index_lock:
            if subdir in self._url_index:
                return
            index = {}
            for meta_path in subdir.glob('*.json'):
                html_path = meta_path.with_suffix('.html')
                if not html_path.exists():
                    continue
                try:
                    async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                        metadata = json.loads(await f.read())
                    identity = self._metadata_identity(metadata)
                    if identity:
                        index.setdefault(identity, set()).add(html_path)
                        self._url_index_paths[html_path] = identity
                except Exception as e:
                    log.warning(f"Ошибка индексации метаданных {meta_path}: {e}")
            self._url_index[subdir] = index

    async def _write_metadata(self, meta_path: Path, metadata: Dict):
        """Читатели видят целый JSON; при ошибке временный sidecar остаётся для диагностики."""
        temporary = meta_path.with_name(f'.{meta_path.name}.{uuid4().hex}.tmp')
        async with aiofiles.open(temporary, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(metadata, ensure_ascii=False, indent=2))
        temporary.replace(meta_path)
        # Уже построенный индекс обновляем без повторного сканирования всей компании.
        async with self._url_index_lock:
            index = self._url_index.get(meta_path.parent)
            if index is not None:
                html_path = meta_path.with_suffix('.html')
                previous = self._url_index_paths.get(html_path)
                if previous in index:
                    index[previous].discard(html_path)
                    if not index[previous]:
                        del index[previous]
                identity = self._metadata_identity(metadata)
                if identity:
                    index.setdefault(identity, set()).add(html_path)
                    self._url_index_paths[html_path] = identity

    async def save_cleaned_html(self, url: str, normalized_url: str, html_content: str,
                              company_name: str, metadata: Optional[Dict] = None, category: str = 'other') -> Tuple[str, bool, Dict]:
        async with self._write_lock:
            saved = await self._save_cleaned_html(url, normalized_url, html_content, company_name, metadata, category)
        quick_check_event('page', url=url, role=category, html=html_content,
                          path=saved[0], is_new=saved[1])
        return saved

    async def _save_cleaned_html(self, url: str, normalized_url: str, html_content: str, 
                              company_name: str, metadata: Optional[Dict] = None, category: str = 'other') -> Tuple[str, bool, Dict]:
        """Сохранение очищенного HTML во временное хранилище с разделением по папкам"""
        try:
            normalized_url = self._metadata_identity({'url': url, 'normalized_url': normalized_url})
            # Получаем поддиректорию в зависимости от категории
            company_subdir = self._get_company_subdir(company_name, category)
            
            # Вычисляем хеш контента
            content_hash = hashlib.sha256(html_content.encode()).hexdigest()
            comparison_hash = html_comparison_hash(html_content)
            
            # Ищем существующие файлы для этого URL
            existing_files = await self._find_existing_files(normalized_url, company_name, category)
            
            is_new = False
            change_history = []
            old_content_hash = None
            
            if existing_files:
                # Загружаем метаданные существующего файла
                existing_meta_path = existing_files[0].with_suffix('.json')
                if existing_meta_path.exists():
                    async with aiofiles.open(existing_meta_path, 'r', encoding='utf-8') as f:
                        existing_metadata = json.loads(await f.read())
                    
                    old_content_hash = existing_metadata.get('content_hash')
                    
                    # Старые архивы сравниваем тем же алгоритмом без массового сброса AI.
                    old_comparison_hash = existing_metadata.get('comparison_hash')
                    if existing_metadata.get('comparison_version') != COMPARISON_VERSION:
                        async with aiofiles.open(existing_files[0], 'r', encoding='utf-8') as f:
                            old_comparison_hash = html_comparison_hash(await f.read())
                    if old_content_hash == content_hash or old_comparison_hash == comparison_hash:
                        # Содержимое прежнее: сохраняем результат AI, отмечаем проверку
                        # текущего прохода и актуальные адреса после редиректа.
                        existing_metadata.update(metadata or {})
                        existing_metadata.update(comparison_hash=comparison_hash,
                                                 comparison_version=COMPARISON_VERSION,
                                                 last_observed_content_hash=content_hash)
                        existing_metadata.update(url=url, normalized_url=normalized_url,
                                                 checked_at=datetime.now().isoformat())
                        if self.crawl_run_id:
                            existing_metadata['crawl_run_id'] = self.crawl_run_id
                        await self._write_metadata(existing_meta_path, existing_metadata)
                        log.info(f"Контент не изменился для {normalized_url}")
                        return str(existing_files[0]), False, existing_metadata
                    
                    # Контент изменился - сохраняем историю
                    change_history = existing_metadata.get('change_history', [])
                    change_history.append({
                        'timestamp': datetime.now().isoformat(),
                        'old_content_hash': old_content_hash,
                        'new_content_hash': content_hash,
                        'change_type': 'content_modified'
                    })
                    
                    # Предыдущие версии остаются в архиве. Рабочие выборки ниже
                    # выбирают только последнюю проверенную версию каждого URL.

            # Генерируем новое имя файла
            filename_base = self._generate_filename(normalized_url, content_hash)
            html_path = company_subdir / f"{filename_base}.html"
            if html_path.exists():
                # Контент мог вернуться к одной из исторических версий: не затираем её.
                filename_base += '_' + datetime.now().strftime('%Y%m%d%H%M%S%f')
                html_path = company_subdir / f"{filename_base}.html"
            meta_path = company_subdir / f"{filename_base}.json"
            
            # Сохраняем HTML контент
            async with aiofiles.open(html_path, 'w', encoding='utf-8') as f:
                await f.write(html_content)
            
            # Подготавливаем метаданные
            meta = {
                'url': url,
                'normalized_url': normalized_url,
                'company_name': company_name,
                'category': category,
                'content_hash': content_hash,
                'comparison_hash': comparison_hash,
                'comparison_version': COMPARISON_VERSION,
                'file_size': len(html_content),
                'saved_at': datetime.now().isoformat(),
                'processed': False,
                'processed_at': None,
                'checkpoints': {
                    'html_saved': True,
                    'ai_processed': False,
                    'indexed': False,
                    'card_generated': False
                },
                'change_history': change_history,
                'is_new_content': old_content_hash != content_hash if old_content_hash else True,
                'storage_path': str(html_path)
            }
            
            if metadata:
                meta.update(metadata)
            
            meta['checked_at'] = datetime.now().isoformat()
            if self.crawl_run_id:
                meta['crawl_run_id'] = self.crawl_run_id

            # Сохраняем метаданные
            await self._write_metadata(meta_path, meta)
            
            # Проверяем размер хранилища
            await self._cleanup_if_needed()
            
            is_new = not existing_files or old_content_hash != content_hash
            log.info(f"Сохранен очищенный HTML: {html_path} (новый: {is_new}, категория: {category})")
            
            return str(html_path), is_new, meta
            
        except Exception as e:
            log.error(f"Ошибка сохранения очищенного HTML: {e}")
            return "", False, {}

    async def _find_existing_files(self, normalized_url: str, company_name: str, category: str) -> List[Path]:
        """Ищем по фактическому адресу, включая архивы со старым lossy хешем имени."""
        try:
            subdir = self._get_company_subdir(company_name, category)
            await self._ensure_url_index(subdir)
            identity = self._metadata_identity({'url': normalized_url})
            candidates = list(self._url_index[subdir].get(identity, ()))
            versions = {}
            for html_path in candidates:
                if not html_path.exists():
                    continue
                async with aiofiles.open(html_path.with_suffix('.json'), 'r', encoding='utf-8') as f:
                    meta = json.loads(await f.read())
                if self._metadata_identity(meta) == identity:
                    versions[html_path] = meta.get('checked_at') or meta.get('saved_at', '')
            return sorted(versions, key=lambda path: (versions[path], path.name), reverse=True)[:1]
        except Exception as e:
            log.error(f"Ошибка поиска существующих файлов: {e}")
            return []

    async def load_cleaned_html(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Загрузка очищенного HTML из временного хранилища"""
        try:
            path = Path(file_path)
            if not path.exists():
                return None
            
            # Загружаем HTML
            async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                html_content = await f.read()
            
            # Загружаем метаданные
            meta_path = path.with_suffix('.json')
            if meta_path.exists():
                async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                    metadata = json.loads(await f.read())
            else:
                metadata = {}
            
            return {
                'html_content': html_content,
                'file_path': str(path),
                'metadata': self._current_metadata(metadata)
            }
            
        except Exception as e:
            log.error(f"Ошибка загрузки очищенного HTML: {e}")
            return None

    async def update_checkpoint(self, file_path: str, checkpoint: str, status: bool = True):
        async with self._write_lock:
            await self._update_checkpoint(file_path, checkpoint, status)

    async def _update_checkpoint(self, file_path: str, checkpoint: str, status: bool = True):
        """Обновление checkpoint обработки"""
        try:
            meta_path = Path(file_path).with_suffix('.json')
            if not meta_path.exists():
                return
            
            async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                metadata = json.loads(await f.read())
            
            metadata['checkpoints'][checkpoint] = status
            if checkpoint == 'ai_processed' and status:
                metadata['processed'] = True
                metadata['processed_at'] = datetime.now().isoformat()
            
            await self._write_metadata(meta_path, metadata)
                
        except Exception as e:
            log.error(f"Ошибка обновления checkpoint {checkpoint} для {file_path}: {e}")

    async def save_product_result(self, file_path: str, product: Dict[str, Any]):
        async with self._write_lock:
            await self._save_product_result(file_path, product)

    async def _save_product_result(self, file_path: str, product: Dict[str, Any]):
        """Результат конкретной версии HTML для повторного прохода без вызова модели."""
        meta_path = Path(file_path).with_suffix('.json')
        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
            metadata = json.loads(await f.read())
        metadata['product_result'] = {key: value for key, value in product.items()
                                      if key != 'metadata'}
        await self._write_metadata(meta_path, metadata)

    async def mark_as_processed(self, file_path: str):
        """Пометить файл как обработанный"""
        await self.update_checkpoint(file_path, 'ai_processed', True)

    async def get_pending_files(self, company_name: str, category: str = None) -> List[Dict[str, Any]]:
        """Необработанные актуальные версии, проверенные в текущем проходе."""
        return [item for item in await self.get_company_files(company_name, category)
                if not item['metadata'].get('checkpoints', {}).get('ai_processed', False)]

    async def is_page_processed(self, normalized_url: str, company_name: str, category: str) -> bool:
        """Проверка, была ли страница уже обработана"""
        try:
            existing_files = await self._find_existing_files(normalized_url, company_name, category)
            for file_path in existing_files:
                meta_path = file_path.with_suffix('.json')
                if meta_path.exists():
                    async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                        metadata = json.loads(await f.read())
                    if metadata.get('checkpoints', {}).get('ai_processed', False):
                        return True
            return False
        except Exception as e:
            log.error(f"Ошибка проверки статуса обработки: {e}")
            return False

    async def mark_as_ai_processed(self, file_path: str, ai_text_path: str = None):
        """Пометить страницу как обработанную AI"""
        async with self._write_lock:
            await self._update_checkpoint(file_path, 'ai_processed', True)
            if ai_text_path:
                meta_path = Path(file_path).with_suffix('.json')
                if meta_path.exists():
                    async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                        metadata = json.loads(await f.read())
                
                    revision = product_extraction_revision(metadata.get('url', ''))
                    if revision and metadata.get('category') == 'product':
                        if metadata.get('product_extraction_revision') != revision:
                            metadata.pop('product_result', None)
                        metadata['product_extraction_revision'] = revision
                    if metadata.get('category') == 'product':
                        metadata['numeric_gate_revision'] = NUMERIC_GATE_REVISION
                    metadata['ai_text_path'] = ai_text_path
                    metadata['ai_processed_at'] = datetime.now().isoformat()
                
                    await self._write_metadata(meta_path, metadata)

    async def get_company_files(self, company_name: str, category: str = None) -> List[Dict[str, Any]]:
        """Актуальная версия каждого URL; при активном проходе — только его страницы."""
        try:
            files = []
            
            # Если указана категория, ищем только в соответствующей папке
            if category:
                subdirs = [self._get_company_subdir(company_name, category)]
            else:
                # Ищем во всех подпапках компании
                subdirs = [
                    self._get_company_subdir(company_name, 'product'),
                    self._get_company_subdir(company_name, 'contacts'),
                    self._get_company_subdir(company_name, 'distributor'),
                    self._get_company_subdir(company_name, 'other')
                ]
            
            for subdir in subdirs:
                if not subdir.exists():
                    continue
                    
                for meta_path in subdir.glob("*.json"):
                    try:
                        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                            metadata = json.loads(await f.read())
                        
                        html_path = meta_path.with_suffix('.html')
                        if html_path.exists():
                            files.append({
                                'html_path': str(html_path),
                                'metadata': self._current_metadata(metadata)
                            })
                            
                    except Exception as e:
                        log.warning(f"Ошибка чтения метаданных {meta_path}: {e}")
                        continue
            
            latest = {}
            for item in files:
                meta = item['metadata']
                key = (meta.get('normalized_url') or item['html_path'], meta.get('category'))
                previous = latest.get(key)
                stamp = meta.get('checked_at') or meta.get('saved_at', '')
                previous_meta = previous['metadata'] if previous else {}
                if previous is None or stamp > (previous_meta.get('checked_at') or previous_meta.get('saved_at', '')):
                    latest[key] = item
            return [item for item in latest.values()
                    if not self.crawl_run_id or item['metadata'].get('crawl_run_id') == self.crawl_run_id]
            
        except Exception as e:
            log.error(f"Ошибка получения файлов компании: {e}")
            return []

    async def _cleanup_if_needed(self):
        """Контроль размера без удаления версий, необходимых для возобновления."""
        try:
            total_size = await self._get_total_size()
            
            if total_size > self.max_size_bytes:
                log.warning(f"Превышен лимит хранилища: {total_size / 1024 / 1024:.2f} MB")
                log.warning("Автоудаление отключено: HTML и результаты AI нужны для возобновления обхода")
                
        except Exception as e:
            log.error(f"Ошибка очистки хранилища: {e}")

    async def _get_total_size(self) -> int:
        """Получить общий размер хранилища"""
        total_size = 0
        for file_path in self.base_dir.rglob("*"):
            if file_path.is_file():
                total_size += file_path.stat().st_size
        return total_size

    async def _remove_oldest_files(self):
        """Удаление самых старых файлов (только обработанных)"""
        try:
            all_files = []
            for file_path in self.base_dir.rglob("*.json"):
                if file_path.is_file():
                    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                        metadata = json.loads(await f.read())
                    
                    # Удаляем только обработанные файлы
                    if metadata.get('checkpoints', {}).get('ai_processed', False):
                        html_path = file_path.with_suffix('.html')
                        if html_path.exists():
                            all_files.append((file_path, html_path.stat().st_mtime))
            
            # Сортируем по дате изменения (старые сначала)
            all_files.sort(key=lambda x: x[1])
            
            # Удаляем самые старые файлы пока не освободим 20% пространства
            target_size = self.max_size_bytes * 0.8
            current_size = await self._get_total_size()
            
            for meta_path, _ in all_files:
                if current_size <= target_size:
                    break
                    
                html_path = meta_path.with_suffix('.html')
                try:
                    # Удаляем оба файла
                    html_size = html_path.stat().st_size if html_path.exists() else 0
                    meta_size = meta_path.stat().st_size
                    
                    if html_path.exists():
                        html_path.unlink()
                    meta_path.unlink()
                    
                    current_size -= (html_size + meta_size)
                    log.info(f"Удален старый файл: {html_path}")
                    
                except Exception as e:
                    log.warning(f"Не удалось удалить файл {html_path}: {e}")
                    
        except Exception as e:
            log.error(f"Ошибка удаления старых файлов: {e}")

    def _sanitize_filename(self, filename: str) -> str:        
        return sanitize_filename(filename)    

    async def save_ai_text(self, company_name: str, url: str, normalized_url: str, 
                    ai_text: Any, text_type: str = "product",
                    metadata: Optional[Dict] = None) -> str:
        """Сохранение AI текста в хранилище временных файлов с автоматической конвертацией dict → string"""
        try:
            if isinstance(ai_text, dict):
                ai_content = json.dumps(ai_text, ensure_ascii=False, indent=2)
            else:
                log.warning(f"AI текст не является словарем, тип: {type(ai_text)}")
                ai_content = json.dumps({"error": "Invalid data type", "type": str(type(ai_text))}, ensure_ascii=False, indent=2)
            
            # С пустым specifications - not_product
            if text_type == "product" and isinstance(ai_text, dict):
                product_data = ai_text.get('product', {})
                if isinstance(product_data, dict):
                    specifications = product_data.get('specifications', {})
                    if specifications == {}:
                        log.info(f"Обнаружен пустой specifications, меняем тип с product на not_product")
                        text_type = "not_product"
            
            # Создаем основную папку компании
            company_dir = self.base_dir / self._sanitize_filename(company_name)
            company_dir.mkdir(exist_ok=True)
            
            # Создаем папку AI_text в корне директории компании
            ai_text_dir = company_dir / "AI_text"
            ai_text_dir.mkdir(exist_ok=True)           
            
            # Получаем целевую папку или используем тип текста как fallback
            target_folder_name = self.folder_mapping.get(text_type, text_type.capitalize())
            final_ai_text_dir = ai_text_dir / target_folder_name
            final_ai_text_dir.mkdir(exist_ok=True)

            # Вычисляем хеш контента для имени файла
            content_hash = hashlib.sha256(ai_content.encode()).hexdigest()[:16]
            url_hash = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]

            # Генерируем имя файла
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_url = self._sanitize_filename(normalized_url[:30])
            filename = f"{text_type}_{content_hash}_{url_hash}_{timestamp}.txt"
            
            file_path = final_ai_text_dir / filename
            
            # Подготавливаем полный контент с метаданными
            full_content = f"""URL: {url}
                            Нормализованный URL: {normalized_url}
                            Тип текста: {text_type}
                            Компания: {company_name}
                            Папка: {target_folder_name}
                            Дата сохранения: {datetime.now().isoformat()}
                            Хеш контента: {content_hash}
                            {'='*60}

                            {ai_content}
                            """
            
            # Добавляем метаданные если есть
            if metadata:
                full_content += f"\n{'='*60}\nМЕТАДАННЫЕ:\n"
                for key, value in metadata.items():
                    full_content += f"{key}: {value}\n"
            
            # Сохраняем файл
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(full_content)
            
            log.info(f"Сохранен AI текст: {file_path} (тип: {text_type}, папка: {target_folder_name})")
            return str(file_path)
            
        except Exception as e:
            log.error(f"Ошибка сохранения AI текста: {e}")
            return ""

    async def load_ai_text(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Загрузка AI текста из файла"""
        try:
            path = Path(file_path)
            if not path.exists():
                return None
            
            async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                content = await f.read()
            
            # Парсим метаданные из файла
            metadata = {}
            ai_text = content
            lines = content.split('\n')
            
            # Извлекаем метаданные из заголовка
            for i, line in enumerate(lines):
                if line.startswith('URL:'):
                    metadata['url'] = line.replace('URL:', '').strip()
                elif line.startswith('Нормализованный URL:'):
                    metadata['normalized_url'] = line.replace('Нормализованный URL:', '').strip()
                elif line.startswith('Тип текста:'):
                    metadata['text_type'] = line.replace('Тип текста:', '').strip()
                elif line.startswith('Компания:'):
                    metadata['company_name'] = line.replace('Компания:', '').strip()
                elif line.startswith('Категория:'):
                    metadata['category'] = line.replace('Категория:', '').strip()
                elif line.startswith('Дата сохранения:'):
                    metadata['saved_at'] = line.replace('Дата сохранения:', '').strip()
                elif line.startswith('Хеш контента:'):
                    metadata['content_hash'] = line.replace('Хеш контента:', '').strip()
                elif line.startswith('=' * 60):
                    # Нашли разделитель, следующий блок - AI текст
                    if i + 1 < len(lines):
                        ai_text = '\n'.join(lines[i+1:])
                        # Убираем блок метаданных если есть
                        if 'МЕТАДАННЫЕ:' in ai_text:
                            ai_text_parts = ai_text.split('МЕТАДАННЫЕ:')
                            ai_text = ai_text_parts[0].strip()
                            if len(ai_text_parts) > 1:
                                # Парсим дополнительные метаданные
                                meta_lines = ai_text_parts[1].split('\n')
                                for meta_line in meta_lines:
                                    if ':' in meta_line:
                                        key, value = meta_line.split(':', 1)
                                        metadata[key.strip()] = value.strip()
                    break
            
            return {
                'ai_text': ai_text,
                'file_path': str(path),
                'metadata': metadata
            }
            
        except Exception as e:
            log.error(f"Ошибка загрузки AI текста: {e}")
            return None

    async def get_company_ai_texts(self, company_name: str, text_type: str = None, category: str = None) -> List[Dict[str, Any]]:
        """Получение всех AI текстов компании"""
        try:
            ai_texts = []
            
            company_dir = self.base_dir / self._sanitize_filename(company_name)
            ai_text_dir = company_dir / "AI_text"
                
            if not ai_text_dir.exists():
                return []
            
            # Определяем, в каких папках искать
            if text_type:
                # Ищем в конкретной папке
                target_folder = self.folder_mapping.get(text_type, text_type.capitalize())
                search_dirs = [ai_text_dir / target_folder]
            else:
                # Ищем во всех подпапках AI_text
                search_dirs = [ai_text_dir / folder for folder in self.folder_mapping.values()]
            
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                
                for file_path in search_dir.glob("*.txt"):
                    try:
                        ai_data = await self.load_ai_text(str(file_path))
                        if ai_data:
                            ai_texts.append(ai_data)
                    except Exception as e:
                        log.warning(f"Ошибка чтения AI текста {file_path}: {e}")
                        continue
            
            return ai_texts
            
        except Exception as e:
            log.error(f"Ошибка получения AI текстов компании: {e}")
            return []   
            
    async def get_unprocessed_pages(self, company_name: str, category: str = None) -> List[Dict[str, Any]]:
        """Получить все необработанные страницы (ai_processed = false)"""
        return await self.get_pending_files(company_name, category)

    async def get_processing_status(self, company_name: str) -> Dict[str, Any]:
        """Получить полную статистику обработки компании"""
        try:
            all_files = await self.get_company_files(company_name)
            total_files = len(all_files)
            
            processed_files = 0
            total_size_bytes = 0
            
            for file_info in all_files:
                metadata = file_info['metadata']
                if metadata.get('checkpoints', {}).get('ai_processed', False):
                    processed_files += 1
                
                file_size = metadata.get('file_size', 0)
                total_size_bytes += file_size
            
            return {
                'total_files': total_files,
                'processed_files': processed_files,
                'unprocessed_files': total_files - processed_files,
                'completion_percentage': (processed_files / total_files * 100) if total_files > 0 else 0,
                'total_size_mb': total_size_bytes / 1024 / 1024,
                'processing_timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            log.error(f"Ошибка получения статуса обработки: {e}")
            return {}

    async def force_reprocess_page(self, normalized_url: str, company_name: str, category: str):
        """Принудительно пометить страницу для повторной обработки"""
        try:
            existing_files = await self._find_existing_files(normalized_url, company_name, category)
            for file_path in existing_files:
                meta_path = file_path.with_suffix('.json')
                if meta_path.exists():
                    async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                        metadata = json.loads(await f.read())
                    
                    # Сбрасываем флаги обработки
                    metadata['checkpoints']['ai_processed'] = False
                    metadata['processed'] = False
                    metadata['processed_at'] = None
                    
                    # Добавляем запись в историю изменений
                    change_history = metadata.get('change_history', [])
                    change_history.append({
                        'timestamp': datetime.now().isoformat(),
                        'action': 'forced_reprocess',
                        'reason': 'manual_reprocessing_request'
                    })
                    metadata['change_history'] = change_history
                    
                    async with aiofiles.open(meta_path, 'w', encoding='utf-8') as f:
                        await f.write(json.dumps(metadata, ensure_ascii=False, indent=2))
                    
                    log.info(f"Страница помечена для повторной обработки: {normalized_url}")
                    
        except Exception as e:
            log.error(f"Ошибка принудительной повторной обработки: {e}")

    async def get_storage_stats(self, company_name: str) -> Dict[str, Any]:
        """Получение статистики хранилища для отладки"""
        try:
            company_dir = self.base_dir / self._sanitize_filename(company_name)
            if not company_dir.exists():
                return {"error": "Company directory not found"}
            
            stats = {
                "company": company_name,
                "total_files": 0,
                "product_pages": 0,
                "company_pages": 0,
                "contacts_pages": 0,     
                "distributor_pages": 0,  
                "ai_texts": 0,
                "processed_pages": 0,
                "unprocessed_pages": 0,
                "ai_texts_by_type": {}
            }
            
            # Считаем файлы в поддиректориях
            product_dir = company_dir / "Product_pages"
            company_dir_path = company_dir / "Company_pages"
            ai_text_dir = company_dir / "AI_text"
            disributor_dir = company_dir / "Distributor_pages"

            
            if product_dir.exists():
                product_files = list(product_dir.glob("*.html"))
                stats["product_pages"] = len(product_files)
                stats["total_files"] += len(product_files)
                
                # Считаем обработанные
                for file_path in product_files:
                    meta_path = file_path.with_suffix('.json')
                    if meta_path.exists():
                        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                            metadata = json.loads(await f.read())
                        if metadata.get('checkpoints', {}).get('ai_processed', False):
                            stats["processed_pages"] += 1
            
            if company_dir_path.exists():
                company_files = list(company_dir_path.glob("*.html"))
                stats["company_pages"] = len(company_files)
                stats["total_files"] += len(company_files)
                
                # Считаем обработанные
                for file_path in company_files:
                    meta_path = file_path.with_suffix('.json')
                    if meta_path.exists():
                        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                            metadata = json.loads(await f.read())
                        if metadata.get('checkpoints', {}).get('ai_processed', False):
                            stats["processed_pages"] += 1
            
            if disributor_dir.exists():
                disributor_files = list(disributor_dir.glob("*.html"))
                stats["product_pages"] = len(disributor_files)
                stats["total_files"] += len(disributor_files)
                
                # Считаем обработанные
                for file_path in disributor_files:
                    meta_path = file_path.with_suffix('.json')
                    if meta_path.exists():
                        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                            metadata = json.loads(await f.read())
                        if metadata.get('checkpoints', {}).get('ai_processed', False):
                            stats["processed_pages"] += 1

            # Считаем AI тексты
            if ai_text_dir.exists():
                ai_folders = ["Distributor_info", "Company_info", "Products", "Not_products"]
                for folder in ai_folders:
                    folder_path = ai_text_dir / folder
                    if folder_path.exists():
                        ai_files = list(folder_path.glob("*.txt"))
                        stats["ai_texts"] += len(ai_files)
                        stats["ai_texts_by_type"][folder] = len(ai_files)
            
            stats["unprocessed_pages"] = stats["total_files"] - stats["processed_pages"]
            
            return stats
            
        except Exception as e:
            log.error(f"Ошибка получения статистики хранилища: {e}")
            return {"error": str(e)}
    
    async def get_distributor_pages(self, company_name: str) -> List[Dict[str, Any]]:
        """Получить все страницы дистрибьюторов компании"""
        return await self.get_company_files(company_name, category='distributor')

    async def has_new_unprocessed_pages(self, company_name: str, category: str = None) -> bool:
        """
        Проверяет, есть ли у компании новые необработанные страницы.
        Возвращает True только если есть страницы с is_new_content=True и ai_processed=False
        """
        try:
            # Получаем все необработанные страницы
            unprocessed_pages = await self.get_unprocessed_pages(company_name, category)
            
            for page in unprocessed_pages:
                metadata = page['metadata']
                # Страница должна быть НОВОЙ (is_new_content=True) 
                # И еще не обработанной AI (ai_processed=False)
                if metadata.get('is_new_content', False) and \
                   not metadata.get('checkpoints', {}).get('ai_processed', False):
                    return True
            
            return False
        except Exception as e:
            log.error(f"Ошибка проверки новых необработанных страниц: {e}")
            # В случае ошибки считаем, что есть новые страницы (безопасный подход)
            return True

    async def get_new_unprocessed_pages(self, company_name: str, category: str = None) -> List[Dict[str, Any]]:
        """
        Получение только НОВЫХ необработанных страниц для указанной категории.
        Если категория не указана, возвращает все новые страницы.
        """
        try:
            all_pages = await self.get_unprocessed_pages(company_name, category)
            new_pages = []
            
            for page in all_pages:
                metadata = page['metadata']
                if metadata.get('is_new_content', False) and \
                   not metadata.get('checkpoints', {}).get('ai_processed', False):
                    new_pages.append(page)
            
            return new_pages
        except Exception as e:
            log.error(f"Ошибка получения новых страниц: {e}")
            return []