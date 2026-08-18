# archive_builder.py 1.1.0
"""
Модуль для создания архива отчетов и отправки на сервер.
Запуск отдельно от основного пайплайна мониторинга.
"""
import os
import sys
import shutil
import zipfile
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, Optional, Set
import logging
import asyncio
import aiohttp
import json
import traceback
from dataclasses import dataclass
from folder_renamer import FolderRenamer
from bot_report.api_client import ApiClient

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("report_archive.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

log = logging.getLogger("report_archive_builder")

@dataclass
class ArchiveConfig:
    """Конфигурация для создания архива отчетов"""
    
    # Пути к данным
    reports_dir: str = '/home/user/stroy-resurs/mvp/Documents/Reports'
    documents_dir: str = '/home/user/stroy-resurs/mvp/Documents'
    
    # Временные директории
    archive_temp_dir: str = '/home/user/stroy-resurs/mvp/ArchiveTemp'
       
    # Настройки архива
    max_archive_size_mb: int = 10000
    archive_name: str = "Отчёт_об_изменениях.zip"
    
    # Имена отчетов
    general_report_name: str = "Таблица общего отчёта об изменениях на сайтах компаний производителей.xlsx"
    detailed_report_name: str = "Таблица детального отчёта об изменениях на сайте компаний производителей.xlsx"
    
    def __post_init__(self):
        """Создание необходимых директорий"""
        os.makedirs(self.archive_temp_dir, exist_ok=True)

class ReportArchiveBuilder:
    """Класс для создания архива с отчетами и файлами компаний"""
    
    def __init__(self, config, api_client: ApiClient):
        self.config = config
        self.api_client = api_client
        self.reports_dir = Path(config.reports_dir)
        self.documents_dir = Path(config.documents_dir)
        self.archive_temp_dir = Path(config.archive_temp_dir)
        
        # Импортируем FolderRenamer        
        self.folder_renamer = FolderRenamer()
        
        # Русские названия для переименования
        self.folder_translations = {
            'Products': 'Товары',
            'Certificates': 'Сертификаты',
            'Documents': 'Документация',
            'Instructions': 'Инструкции',
            'Price_lists': 'Прайс-листы',
            # Папки в товарах
            'Documents_product': 'Файлы товара',
            'Images': 'Изображения товара'
        }
        self.files_to_delete = ['crawling_stats.json']
        log.info("ReportArchiveBuilder инициализирован")
        # Фильтр содержимого архива (ТЗ I.4.1.2): пользователю передаются PDF и RTF-карточки;
        # форматы из списка конвертации — только если PDF-версии рядом нет (чтобы не терять данные);
        # файлы без расширения, прочие форматы и служебный мусор не передаются.
        self.allowed_extensions = {'.pdf', '.rtf'}
        self.convertible_extensions = {'.doc', '.docx', '.xls', '.xlsx',
                                       '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
        self.blacklist_substrings = ['crawling_stats', 'politika-v-otnosenii', 'obrabotki-pdn']
        log.info(f"Директория отчетов: {self.reports_dir}")
        log.info(f"Директория документов: {self.documents_dir}")
        log.info(f"Используется ApiClient для работы с API")

    def _should_include_file(self, file_path: Path) -> bool:
        """Решение, должен ли файл попасть в архив пользователю (ТЗ I.4.1.2)"""
        name_lower = file_path.name.lower()
        if any(s in name_lower for s in self.blacklist_substrings):
            return False
        ext = file_path.suffix.lower()
        if ext in self.allowed_extensions:
            return True
        if ext in self.convertible_extensions:
            # Если рядом уже лежит сконвертированный PDF — оригинал является дублем
            if file_path.with_suffix('.pdf').exists():
                return False
            # Конвертация не состоялась — передаём оригинал, чтобы не потерять данные
            return True
        # Файлы без расширения и прочие форматы по ТЗ не передаются
        return False

    def _copytree_ignore(self, src, names):
        """ignore-колбэк для shutil.copytree: отфильтровывает файлы, не предназначенные пользователю"""
        src_path = Path(src)
        ignored = set()
        for name in names:
            p = src_path / name
            if p.is_file() and not self._should_include_file(p):
                ignored.add(name)
                log.debug(f"Файл отфильтрован из архива: {p}")
        return ignored

    def _delete_unwanted_files(self, folder_path: Path):
        """
        Удаление нежелательных файлов из папки компании
        """
        for file_name in self.files_to_delete:
            file_path = folder_path / file_name
            if file_path.exists():
                try:
                    file_path.unlink()
                    log.debug(f"Удален файл: {file_path}")
                except Exception as e:
                    log.warning(f"Не удалось удалить файл {file_path}: {e}")

    def _check_reports_exist(self) -> bool:
        """
        Проверка наличия общего отчета (детальный - опционально)
        """
        general_report = self.reports_dir / self.config.general_report_name
        if not general_report.exists():
            log.error(f"Общий отчет не найден: {general_report}")
            return False
        log.info("Общий отчет найден")
        return True
    
    def extract_companies_from_general_report(self) -> Dict[str, Dict]:
        """
        Извлечение информации о компаниях из общего отчета
        
        Returns:
            Dict с ключами company_id и значениями {
                'id': str,
                'name': str,
                'status': str,
                'has_changes': bool
            }
        """
        general_report_path = self.reports_dir / self.config.general_report_name
        
        if not general_report_path.exists():
            log.error(f"Общий отчет не найден: {general_report_path}")
            return {}
        
        try:
            df = pd.read_excel(general_report_path, sheet_name='Отчёт')
            
            companies = {}
            for _, row in df.iterrows():
                company_id = str(row['ID компании производителя']).strip()
                # pandas читает числовой ID с пропусками как float ('123.0') — приводим к целому виду
                if company_id.endswith('.0') and company_id[:-2].isdigit():
                    company_id = company_id[:-2]
                status = row['Наличие изменений на сайте компании производителя']
                
                # Определяем, есть ли изменения (только "Новая компания" и "Есть изменения")
                has_changes = status in ['Новая компания', 'Есть изменения']
                
                companies[company_id] = {
                    'id': company_id,
                    'name': row['Наименование компании производителя'],
                    'status': status,
                    'has_changes': has_changes,
                    'found_in_report': True
                }
            
            log.info(f"Из общего отчета извлечено {len(companies)} компаний")
            return companies
            
        except KeyError as e:
            log.error(f"В отчете отсутствует необходимый столбец: {e}")
            log.error("Проверьте структуру отчета (должен быть столбец 'ID компании производителя')")
            return {}
        except Exception as e:
            log.error(f"Ошибка чтения общего отчета: {e}")
            log.error(traceback.format_exc())
            return {}
    
    def find_company_folders(self, company_ids: Set[str]) -> Dict[str, Path]:
        """
        Поиск папок компаний по ID в директории Documents
        
        Args:
            company_ids: Set ID компаний из отчета
            
        Returns:
            Dict с ключами company_id и значениями Path к папкам
        """
        company_folders = {}
        
        if not self.documents_dir.exists():
            log.error(f"Директория Documents не существует: {self.documents_dir}")
            return {}
        
        # Создаем словарь для быстрого поиска по префиксам
        company_prefixes = {f"{company_id}_": company_id for company_id in company_ids}
        
        # Ищем папки, начинающиеся с ID компании
        for item in self.documents_dir.iterdir():
            if item.is_dir():
                folder_name = item.name
                
                # Проверяем все префиксы
                for prefix, company_id in company_prefixes.items():
                    if folder_name.startswith(prefix):
                        company_folders[company_id] = item
                        log.debug(f"Найдена папка для компании {company_id}: {folder_name}")
                        break
        
        log.info(f"Найдено {len(company_folders)} папок компаний из {len(company_ids)} в отчете")
        
        # Логируем компании, для которых не нашли папки
        missing_ids = company_ids - set(company_folders.keys())
        if missing_ids:
            log.warning(f"Не найдены папки для компаний: {', '.join(missing_ids)}")
        
        return company_folders
    
    def remove_id_from_folder_name(self, folder_name: str) -> str:
        """
        Удаление ID из названия папки и переименование на актуальное наименование компании
        """
        try:
            # Извлекаем ID компании из названия папки
            parts = folder_name.split('_')
            if len(parts) >= 2 and parts[0].startswith('company_'):
                company_id = parts[0]
                
                # Получаем актуальное имя компании из отчета
                if hasattr(self, 'companies_info') and company_id in self.companies_info:
                    company_info = self.companies_info[company_id]
                    # Используем актуальное имя компании из отчета
                    actual_name = company_info.get('name', '_'.join(parts[1:]))
                    
                    # Санитизируем имя для файловой системы
                    from product_utils import sanitize_filename
                    safe_name = sanitize_filename(actual_name)
                    
                    return safe_name
                else:
                    # Если нет информации в отчете, используем оригинальное имя
                    return '_'.join(parts[1:])
            else:
                # Если формат не соответствует, возвращаем как есть
                return folder_name
                
        except Exception as e:
            log.warning(f"Ошибка при извлечении имени компании из {folder_name}: {e}")
            return folder_name
    
    def _canonical_company_folder_name(self, company_id: str, company_info: Dict, fallback_name: str) -> str:
        """
        Каноничное имя папки компании в архиве: <ID>_<санитизированное имя из отчёта>.
        Формула обязана совпадать с PathFormatter.format_company_folder_path,
        иначе «Путь в архиве к файлу» из детального отчёта не разрешится в ZIP.
        """
        try:
            from product_utils import sanitize_company_name
            name = str(company_info.get('name', '') or '').strip()
            if name and name.lower() != 'nan' and company_id:
                return f"{company_id}_{sanitize_company_name(name)}"
        except Exception as e:
            log.warning(f"Не удалось построить каноничное имя папки для {company_id}: {e}")
        return fallback_name

    def is_folder_empty(self, folder_path: Path) -> bool:
        """
        Проверка, пуста ли папка
        """
        if not folder_path.exists() or not folder_path.is_dir():
            return True
        
        # Игнорируем скрытые файлы и системные файлы
        ignore_patterns = ['.', '~', 'Thumbs.db', '.DS_Store']
        
        for item in folder_path.iterdir():
            # Пропускаем скрытые и системные файлы
            if any(item.name.startswith(pattern) for pattern in ignore_patterns):
                continue
            
            # Проверяем, является ли элемент файлом или непустой директорией
            if item.is_file():
                return False
            elif item.is_dir():
                # Рекурсивно проверяем поддиректории
                if not self.is_folder_empty(item):
                    return False
        
        return True
    
    def _create_manufacturer_files_folder(self, source_folder: Path, dest_folder: Path) -> bool:
        """
        Создание папки "Файлы производителя" с подпапками
        """
        manufacturer_files_folder = dest_folder / "Файлы производителя"
        has_manufacturer_files = False
        
        # Папки для включения в "Файлы производителя"
        manufacturer_subfolders = ['Certificates', 'Documents', 'Instructions', 'Price_lists', 'CompanyFiles']
        
        for folder_name in manufacturer_subfolders:
            source_subfolder = source_folder / folder_name
            if source_subfolder.exists() and source_subfolder.is_dir() and not self.is_folder_empty(source_subfolder):
                if not has_manufacturer_files:
                    manufacturer_files_folder.mkdir(exist_ok=True)
                    has_manufacturer_files = True
                
                # Используем FolderRenamer для переименования
                renamed_name = self.folder_renamer.rename_path(folder_name)
                new_path = manufacturer_files_folder / renamed_name
                shutil.copytree(source_subfolder, new_path, dirs_exist_ok=True, ignore=self._copytree_ignore)
                log.debug(f"Создана папка: {new_path}")
        
        return has_manufacturer_files
    
    def _process_products_folder(self, source_products_folder: Path, dest_products_folder: Path):
        """
        Обработка папки Products (переименование в Товары и внутренних папок)
        """
        if not source_products_folder.exists() or not source_products_folder.is_dir():
            return
        
        dest_products_folder.mkdir(parents=True, exist_ok=True)
        
        for product_folder in source_products_folder.iterdir():
            if product_folder.is_dir():
                dest_product_folder = dest_products_folder / product_folder.name
                
                try:
                    shutil.copytree(product_folder, dest_product_folder, dirs_exist_ok=True, ignore=self._copytree_ignore)

                    for subfolder in dest_product_folder.iterdir():
                        if subfolder.is_dir():
                            # Прямое обращение к словарю переименования подпапок товара
                            new_name = self.folder_renamer.PRODUCT_SUBFOLDER_TRANSLATIONS.get(
                                subfolder.name, 
                                subfolder.name
                            )
                            if new_name != subfolder.name:
                                new_path = dest_product_folder / new_name
                                subfolder.rename(new_path)
                                log.debug(f"Переименована папка в товаре: {subfolder.name} -> {new_name}")
                                
                except Exception as e:
                    log.warning(f"Ошибка обработки папки товара {product_folder.name}: {e}")
    
    def rename_and_restructure_company_folder(self, source_folder: Path, dest_folder: Path, 
                                            company_info: Dict) -> bool:
        """
        Копирование и реструктуризация папки компании с переименованием
        """
        try:
            # Проверяем, что исходная папка существует и не пуста
            if not source_folder.exists() or not source_folder.is_dir():
                log.warning(f"Исходная папка не существует: {source_folder}")
                return False
            
            # Создаем целевую папку
            dest_folder.mkdir(parents=True, exist_ok=True)
            
            log.info(f"Обработка компании {company_info['id']} - {company_info['name']}")
            
            # 1. Копируем и реструктурируем папку Products (если есть)
            source_products = source_folder / 'Products'
            if source_products.exists() and source_products.is_dir():
                dest_products = dest_folder / 'Товары'  # Используем переименованное имя
                self._process_products_folder(source_products, dest_products)
            
            # 2. Создаем папку "Файлы производителя" с подпапками
            has_manufacturer_files = self._create_manufacturer_files_folder(source_folder, dest_folder)
            
            # 3. Копируем все остальные файлы и папки (кроме уже обработанных)
            processed_folders = ['Products', 'Certificates', 'Documents', 'Instructions', 'Price_lists', 'CompanyFiles']
            
            for item in source_folder.iterdir():
                if item.name in processed_folders:
                    continue

                if item.is_file() and item.name in self.files_to_delete:
                    continue

                try:
                    if item.is_dir():
                        # Копируем папку (с фильтром содержимого)
                        shutil.copytree(item, dest_folder / item.name, dirs_exist_ok=True, ignore=self._copytree_ignore)
                    elif item.is_file() and self._should_include_file(item):
                        # Копируем файл из корня папки компании
                        shutil.copy2(item, dest_folder / item.name)
                except Exception as e:
                    log.warning(f"Ошибка копирования {item}: {e}")
            
            log.info(f"Папка компании {company_info['id']} успешно обработана")
            return True
            
        except Exception as e:
            log.error(f"Ошибка обработки папки компании {company_info['id']}: {e}")
            log.error(traceback.format_exc())
            return False
    
    def create_archive_structure(self, companies_info: Dict[str, Dict], 
                           company_folders: Dict[str, Path]) -> Tuple[Optional[Path], Dict[str, str]]:
        """
        Создание временной структуры для архива
        
        Args:
            companies_info: Информация о компаниях из отчета
            company_folders: Найденные папки компаний
            
        Returns:
            Tuple: (Path к временной директории, Dict с информацией об обработке)
        """
        self.companies_info = companies_info
        
        processing_stats = {
            'total_companies': len(companies_info),
            'processed': 0,
            'skipped_no_folder': 0,
            'skipped_empty': 0,
            'skipped_no_changes': 0,
            'errors': 0
        }
        
        try:
            # Создаем уникальную временную директорию
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            temp_dir = self.archive_temp_dir / f"temp_archive_{timestamp}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            log.info(f"Создана временная директория: {temp_dir}")
            
            # Папка для отчетов
            reports_folder = temp_dir / "Отчёт об изменениях сайтов компаний производителей"
            reports_folder.mkdir(exist_ok=True)
            
            # Копируем общий отчет
            general_report = self.reports_dir / self.config.general_report_name
            try:
                shutil.copy2(general_report, reports_folder / general_report.name)
                log.info("Общий отчет скопирован в архив")
            except Exception as e:
                log.error(f"Ошибка копирования общего отчета: {e}")
                return None, processing_stats
            
            # Копируем детальный отчет, если он существует
            detailed_report = self.reports_dir / self.config.detailed_report_name
            if detailed_report.exists():
                try:
                    shutil.copy2(detailed_report, reports_folder / detailed_report.name)
                    log.info("Детальный отчет скопирован в архив")
                except Exception as e:
                    log.error(f"Ошибка копирования детального отчета: {e}")
            else:
                log.info("Детальный отчет отсутствует (нет изменений), не копируется")
            
            # Папка для дополнительных файлов
            additional_files_folder = temp_dir / "Дополнительные файлы к отчёту"
            additional_files_folder.mkdir(exist_ok=True)
            
            # Обрабатываем папки компаний
            for company_id, company_info in companies_info.items():
                status = company_info['status']
                has_changes = company_info['has_changes']
                company_name = company_info['name']
                
                # ИЗМЕНЕНИЕ: Пропускаем компании без изменений (статусы "Нет изменений", "Уже была в отчёте" и "Сайт не найден")
                if not has_changes:
                    processing_stats['skipped_no_changes'] += 1
                    log.debug(f"Компания '{company_name}' (ID: {company_id}) пропущена - статус '{status}' (без изменений)")
                    continue
                
                # Проверяем, есть ли папка компании
                if company_id not in company_folders:
                    processing_stats['skipped_no_folder'] += 1
                    log.warning(f"Папка не найдена для компании: {company_name} (ID: {company_id})")
                    continue
                
                source_folder = company_folders[company_id]
                
                # Проверяем, не пуста ли папка (для компаний с изменениями - копируем даже пустую, но лучше пропустить)
                # По желанию можно пропускать пустые папки, но оставим как есть, чтобы структура была создана
                if self.is_folder_empty(source_folder):
                    log.info(f"Папка компании пуста, но статус '{status}' требует включения: {company_name}")
                    # Можно продолжить, но папка будет создана пустой. Для уменьшения мусора - пропустим
                    processing_stats['skipped_empty'] += 1
                    continue
                
                # Каноничное имя папки в архиве: <ID>_<имя из отчёта> —
                # та же формула, что в path_formatter (колонка «Путь в архиве» детального отчёта)
                new_folder_name = self._canonical_company_folder_name(company_id, company_info, source_folder.name)
                dest_folder = additional_files_folder / new_folder_name
                
                # Копируем и реструктуризируем
                if self.rename_and_restructure_company_folder(source_folder, dest_folder, company_info):
                    processing_stats['processed'] += 1
                else:
                    processing_stats['errors'] += 1
            
            log.info(f"Статистика обработки компаний: {processing_stats}")
            
            # Проверяем, есть ли что архивировать
            if processing_stats['processed'] == 0:
                log.warning("Нет папок компаний для архивации")
            
            return temp_dir, processing_stats
            
        except Exception as e:
            log.error(f"Ошибка создания структуры архива: {e}")
            log.error(traceback.format_exc())
            return None, processing_stats
    
    def create_zip_archive(self, temp_dir: Path) -> Tuple[Optional[Path], Dict[str, any]]:
        """
        Создание ZIP архива из временной директории
        
        Args:
            temp_dir: Временная директория с подготовленными файлами
            
        Returns:
            Tuple: (Path к созданному ZIP архиву, Dict со статистикой архива)
        """
        archive_stats = {
            'total_size_mb': 0,
            'file_count': 0,
            'folder_count': 0,
            'success': False
        }
        
        try:
            # Определяем имя и путь для архива
            archive_path = self.archive_temp_dir / self.config.archive_name
            
            # Удаляем старый архив если существует
            if archive_path.exists():
                archive_path.unlink()
            
            log.info(f"Создание ZIP архива: {archive_path}")
            
            # Создаем ZIP архив
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                file_count = 0
                
                for root, dirs, files in os.walk(temp_dir):
                    archive_stats['folder_count'] += len(dirs)
                    
                    for file in files:
                        file_path = os.path.join(root, file)
                        # Вычисляем относительный путь для архива
                        arcname = os.path.relpath(file_path, temp_dir)
                        zipf.write(file_path, arcname)
                        file_count += 1
                
                archive_stats['file_count'] = file_count
            
            # Получаем статистику по архиву
            archive_size = archive_path.stat().st_size
            archive_stats['total_size_mb'] = archive_size / (1024 * 1024)
            archive_stats['success'] = True
            
            # Проверяем размер архива
            if archive_stats['total_size_mb'] > self.config.max_archive_size_mb:
                log.warning(f"Размер архива {archive_stats['total_size_mb']:.2f} МБ превышает лимит {self.config.max_archive_size_mb} МБ")
            
            log.info(f"ZIP архив создан успешно. Размер: {archive_stats['total_size_mb']:.2f} МБ, файлов: {archive_stats['file_count']}")
            return archive_path, archive_stats
            
        except Exception as e:
            log.error(f"Ошибка создания ZIP архива: {e}")
            log.error(traceback.format_exc())
            return None, archive_stats
    
    async def upload_archive_to_server(self, archive_path: Path, date_str: str, user_id: str) -> Tuple[bool, str, Optional[str]]:
        """
        Загрузка архива на сервер через ApiClient
        
        Args:
            archive_path: Путь к архиву
            date_str: Дата в формате ДД_ММ_ГГГГ
            user_id: ID пользователя
            
        Returns:
            Tuple[bool, str, Optional[str]]: (успех, сообщение, ссылка для скачивания)
        """
        return await self.api_client.upload_archive(archive_path, date_str, user_id)
    
    def cleanup_temp_files(self, temp_dir: Optional[Path] = None, 
                          archive_path: Optional[Path] = None):
        """
        Очистка временных файлов
        
        Args:
            temp_dir: Временная директория
            archive_path: Путь к архиву
        """
        try:
            # Удаляем временную директорию
            if temp_dir and temp_dir.exists() and temp_dir.is_dir():
                shutil.rmtree(temp_dir)
                log.info(f"Временная директория удалена: {temp_dir}")
            
            # Архив не удаляем - он должен сохраниться для отправки
            # После успешной отправки можно удалить, но оставим для отладки
                
        except Exception as e:
            log.warning(f"Ошибка при удалении временных файлов: {e}")
    
    async def build_and_upload_archive(self, date_str: str, user_id: str) -> Dict[str, any]:
        """
        Основной метод для создания и отправки архива
        
        Returns:
            Dict с результатами выполнения
        """
        result = {
            'success': False,
            'error': None,
            'processing_stats': {},
            'archive_stats': {},
            'upload_stats': {},
            'archive_path': None,
            'timestamp': datetime.now().isoformat()
        }
        
        log.info("=" * 60)
        log.info("Запуск создания архива с отчетами")
        log.info("=" * 60)
        
        # Шаг 1: Проверяем наличие отчетов (только общий)
        if not self._check_reports_exist():
            result['error'] = "Общий отчет не найден"
            log.error("Архив не создается: общий отчет не найден")
            return result
        
        # Шаг 2: Извлекаем информацию о компаниях из общего отчета
        companies_info = self.extract_companies_from_general_report()
        if not companies_info:
            result['error'] = "Не удалось извлечь информацию о компаниях из отчета"
            log.error("Архив не создается: нет данных о компаниях")
            return result
        
        # Шаг 3: Находим папки компаний
        company_folders = self.find_company_folders(set(companies_info.keys()))
        
        # Шаг 4: Создаем временную структуру для архива
        temp_dir, processing_stats = self.create_archive_structure(companies_info, company_folders)
        result['processing_stats'] = processing_stats
        
        if not temp_dir:
            result['error'] = "Не удалось создать временную структуру архива"
            log.error("Архив не создается: ошибка создания структуры")
            return result
        
        # Шаг 5: Создаем ZIP архив
        archive_path, archive_stats = self.create_zip_archive(temp_dir)
        result['archive_stats'] = archive_stats
        
        if not archive_path or not archive_stats['success']:
            result['error'] = "Не удалось создать ZIP архив"
            log.error("Архив не создается: ошибка создания ZIP")
            self.cleanup_temp_files(temp_dir)
            return result
        
        result['archive_path'] = str(archive_path)
        
        # Шаг 6: Отправляем архив на сервер через ApiClient
        success, message, download_url = await self.upload_archive_to_server(archive_path, date_str, user_id)
        result['upload_stats'] = {
            'success': success,
            'message': message,
            'download_url': download_url
        }
        
        # Шаг 7: Очищаем временные файлы (оставляем архив для проверки)
        self.cleanup_temp_files(temp_dir, None)
        
        # Определяем общий успех
        result['success'] = success
        
        # Логируем итоговый результат
        if result['success']:
            log.info("=" * 60)
            log.info("АРХИВ УСПЕШНО СОЗДАН И ОТПРАВЛЕН")
            log.info("=" * 60)
        else:
            log.error("=" * 60)
            log.error("СОЗДАНИЕ АРХИВА ЗАВЕРШЕНО С ОШИБКАМИ")
            log.error("=" * 60)
        
        # Выводим сводную статистику
        self._print_summary_statistics(result)
        
        return result
    
    def _print_summary_statistics(self, result: Dict[str, any]):
        """Вывод сводной статистики"""
        log.info("\n" + "=" * 60)
        log.info("СВОДНАЯ СТАТИСТИКА СОЗДАНИЯ АРХИВА")
        log.info("=" * 60)
        
        if 'processing_stats' in result:
            ps = result['processing_stats']
            log.info(f"Компании: всего {ps.get('total_companies', 0)}, "
                    f"обработано {ps.get('processed', 0)}, "
                    f"пропущено {ps.get('skipped_no_folder', 0) + ps.get('skipped_empty', 0) + ps.get('skipped_no_changes', 0)}")
        
        if 'archive_stats' in result:
            as_stats = result['archive_stats']
            log.info(f"Архив: {as_stats.get('file_count', 0)} файлов, "
                    f"{as_stats.get('folder_count', 0)} папок, "
                    f"размер {as_stats.get('total_size_mb', 0):.2f} МБ")
        
        if 'upload_stats' in result:
            us = result['upload_stats']
            if us.get('success'):
                log.info(f"Отправка: успешно, ссылка: {us.get('download_url', 'N/A')}")
            else:
                log.info(f"Отправка: ошибка - {us.get('message', 'неизвестно')}")
        
        log.info(f"Общий результат: {'УСПЕХ' if result.get('success') else 'ОШИБКА'}")
        log.info("=" * 60)


async def main():
    """
    Основная функция для запуска из командной строки
    """
    log.info("Запуск standalone модуля создания архива отчетов")
    
    try:
        # Создаем конфигурацию (для standalone запуска)
        from bot_report.bot_config import BotConfig
        config = BotConfig()
        
        # Создаем ApiClient
        api_client = ApiClient(config)
        
        # Создаем экземпляр архиватора
        archiver = ReportArchiveBuilder(config, api_client)
        
        # Для standalone запуска используем текущую дату и тестовый user_id
        date_str = datetime.now().strftime("%d_%m_%Y")
        user_id = "test_user"
        
        # Запускаем процесс создания и отправки архива
        result = await archiver.build_and_upload_archive(date_str, user_id)
        
        # Возвращаем код завершения
        return 0 if result.get('success', False) else 1
        
    except KeyboardInterrupt:
        log.info("Процесс прерван пользователем")
        return 130
    except Exception as e:
        log.error(f"Критическая ошибка: {e}")
        log.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)