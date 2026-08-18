# file_converter.py 1.0.0
import os
import logging
from pathlib import Path
from typing import List, Optional
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading
from dataclasses import dataclass
from datetime import datetime
import json
import tempfile
import uuid
import shutil

# Настройка логирования
log = logging.getLogger("file_converter")

@dataclass
class ConversionResult:
    """Результат конвертации файла"""
    input_file: str
    output_file: str
    success: bool
    error: Optional[str] = None
    conversion_time: float = 0.0

class FileConverter:
    """Конвертер файлов в PDF формат через LibreOffice"""
    
    # Поддерживаемые форматы LibreOffice
    SUPPORTED_EXTENSIONS = {
        '.doc', '.docx', '.xls', '.xlsx', '.rtf',
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'
    }
    
    def __init__(self, 
                 company_directory: str,
                 libreoffice_path: str = "/usr/bin/soffice",
                 max_workers: int = 4,
                 log_file: str = "file_conversion.log",
                 delete_originals: bool = True,
                 delete_empty_folders: bool = True,
                 exclude_product_cards: bool = True):
        """
        Инициализация конвертера
        
        Args:
            company_directory: Директория компании
            libreoffice_path: Путь к LibreOffice
            max_workers: Максимальное количество потоков
            log_file: Файл для логирования
            delete_originals: Удалять оригиналы после конвертации
            delete_empty_folders: Удалять пустые папки
        """
        self.company_directory = Path(company_directory)
        self.libreoffice_path = Path(libreoffice_path)
        self.max_workers = max_workers
        self.delete_originals = delete_originals
        self.delete_empty_folders = delete_empty_folders
        self.exclude_product_cards = exclude_product_cards
        self._conversion_errors = []
        self.last_results = []
        
        # Настройка логирования
        self._setup_logging(log_file)
        
        # Проверка зависимостей
        self._check_dependencies()
    
    def _setup_logging(self, log_file: str):
        """Настройка логирования"""
        log.setLevel(logging.INFO)
        
        # Форматтер
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Файловый обработчик
        log_file_path = Path(self.company_directory).parent / log_file
        file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
        
        # Консольный обработчик
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        log.addHandler(console_handler)
    
    def _check_dependencies(self):
        """Проверка необходимых зависимостей"""
        if not self.libreoffice_path.exists():
            error_msg = f"LibreOffice не найден по пути: {self.libreoffice_path}"
            log.error(error_msg)
            self._conversion_errors.append(error_msg)
            raise FileNotFoundError(error_msg)
    
    def discover_files_to_convert(self) -> List[Path]:
        """
        Обнаружение всех файлов для конвертации в директории компании
        
        Returns:
            Список путей к файлам для конвертации
        """
        files_to_convert = []
        
        if not self.company_directory.exists():
            log.error(f"Директория компании не существует: {self.company_directory}")
            return files_to_convert
        
        # Сканируем всю структуру директории компании
        for root, dirs, files in os.walk(self.company_directory):
            for file in files:
                file_path = Path(root) / file
                if self.exclude_product_cards and self._is_product_card_rtf(file_path):
                    log.info(f"Пропускаем карточку товара: {file_path}")
                    continue
                if self._is_supported_file(file_path):
                    files_to_convert.append(file_path)
        
        log.info(f"Найдено файлов для конвертации: {len(files_to_convert)}")
        return files_to_convert
    
    def _is_product_card_rtf(self, file_path: Path) -> bool:
        """Проверка, является ли файл карточкой товара (RTF в папке Products)"""
        try:
            if file_path.suffix.lower() != '.rtf':
                return False
            
            relative_path = file_path.relative_to(self.company_directory)
            path_parts = relative_path.parts

            if len(path_parts) != 3:
                return False
            
            if path_parts[0].lower() != 'products':
                return False
            
            if len(path_parts) == 3 and path_parts[2].lower().endswith('.rtf'):
                log.debug(f"Найден RTF файл в папке товара: {file_path}")
                return True
            
            return False
        
        except ValueError:
            return False
        except Exception as e:
            log.warning(f"Ошибка при проверке пути {file_path}: {e}")
            return False
            
        
    def _is_supported_file(self, file_path: Path) -> bool:
        """
        Проверка, поддерживается ли файл для конвертации
        
        Args:
            file_path: Путь к файлу
            
        Returns:
            True если файл поддерживается
        """
        extension = file_path.suffix.lower()
        return extension in self.SUPPORTED_EXTENSIONS
    
    def _get_output_pdf_path(self, input_file: Path) -> Path:
        """
        Генерация пути для выходного PDF файла
        
        Args:
            input_file: Входной файл
            
        Returns:
            Путь для PDF файла
        """
        return input_file.with_suffix('.pdf')
    
    def convert_file(self, input_file: Path, output_file: Path) -> ConversionResult:
        """
        Конвертация файлов через LibreOffice
        
        Args:
            input_file: Входной файл
            output_file: Выходной PDF файл
            
        Returns:
            Результат конвертации
        """
        start_time = datetime.now()
        # Инициализация ДО try: finally ссылается на переменную, и исключение раньше
        # строки присваивания давало UnboundLocalError, маскировавший исходную ошибку
        unique_profile_dir = None

        try:
            if output_file.exists():
                return ConversionResult(
                    input_file=str(input_file),
                    output_file=str(output_file),
                    success=True,
                    error="PDF уже существует",
                    conversion_time=0.0
                )
            
            output_file.parent.mkdir(parents=True, exist_ok=True)
            
            unique_profile_dir = f"/tmp/lo_profile_{uuid.uuid4()}"
            
            command = [
                str(self.libreoffice_path),
                '--headless',
                '--invisible',
                '--nodefault',
                '--nofirststartwizard',
                '--nolockcheck',
                '--nologo',
                f'-env:UserInstallation=file://{unique_profile_dir}',
                '--convert-to', 'pdf',
                '--outdir', str(output_file.parent),
                str(input_file)
            ]
            
            # Вызов LibreOffice
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=300,         
                check=True
            )
            
            conversion_time = (datetime.now() - start_time).total_seconds()
            
            if output_file.exists():
                log.info(f"Успешно сконвертирован: {input_file} -> {output_file}")
                if self.delete_originals and input_file.exists():
                    input_file.unlink()
                    log.info(f"Удален оригинальный файл: {input_file}")
                
                return ConversionResult(
                    input_file=str(input_file),
                    output_file=str(output_file),
                    success=True,
                    conversion_time=conversion_time
                )
            else:
                error_msg = f"PDF файл не создан после конвертации: {output_file}"
                log.error(f"{error_msg}. stderr: {result.stderr}")
                self._conversion_errors.append(error_msg)
                return ConversionResult(
                    input_file=str(input_file),
                    output_file=str(output_file),
                    success=False,
                    error=error_msg,
                    conversion_time=conversion_time
                )
                
        except subprocess.TimeoutExpired:
            error_msg = f"Таймаут конвертации файла: {input_file}"
            log.error(error_msg)
            self._conversion_errors.append(error_msg)
            return ConversionResult(
                input_file=str(input_file),
                output_file=str(output_file),
                success=False,
                error=error_msg,
                conversion_time=(datetime.now() - start_time).total_seconds()
            )
        except subprocess.CalledProcessError as e:
            error_msg = f"Ошибка LibreOffice для файла {input_file}: {e}"
            log.error(f"{error_msg}. stderr: {e.stderr}")
            self._conversion_errors.append(error_msg)
            return ConversionResult(
                input_file=str(input_file),
                output_file=str(output_file),
                success=False,
                error=error_msg,
                conversion_time=(datetime.now() - start_time).total_seconds()
            )
        except Exception as e:
            error_msg = f"Неожиданная ошибка при конвертации {input_file}: {e}"
            log.error(error_msg)
            self._conversion_errors.append(error_msg)
            return ConversionResult(
                input_file=str(input_file),
                output_file=str(output_file),
                success=False,
                error=error_msg,
                conversion_time=(datetime.now() - start_time).total_seconds()
            )
        finally:
            # Удаляем временный профиль ЛЮБОМ случае
            if unique_profile_dir and os.path.exists(unique_profile_dir):
                shutil.rmtree(unique_profile_dir, ignore_errors=True)
    
    def convert_single_file(self, input_file: Path) -> ConversionResult:
        """
        Конвертация одного файла
        
        Args:
            input_file: Путь к входному файлу
            
        Returns:
            Результат конвертации
        """
        output_file = self._get_output_pdf_path(input_file)
        
        # Пропускаем если уже PDF
        if input_file.suffix.lower() == '.pdf':
            return ConversionResult(
                input_file=str(input_file),
                output_file=str(output_file),
                success=True,
                error="Уже PDF формат",
                conversion_time=0.0
            )
        
        # Проверяем, является ли файл карточкой товара
        if self.exclude_product_cards and self._is_product_card_rtf(input_file):
            return ConversionResult(
                input_file=str(input_file),
                output_file=str(output_file),
                success=True,
                error="Карточка товара (RTF в Products) - исключена из конвертации",
                conversion_time=0.0
            )
        
        return self.convert_file(input_file, output_file)
    
    def remove_empty_folders(self, directory: Path = None):
        """
        Рекурсивное удаление пустых папок
        
        Args:
            directory: Директория для проверки (по умолчанию - директория компании)
        """
        if directory is None:
            directory = self.company_directory
        
        if not directory.exists():
            return
        
        # Рекурсивно проверяем все поддиректории
        for item in directory.iterdir():
            if item.is_dir():
                self.remove_empty_folders(item)
        
        # Удаляем текущую директорию если она пуста
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                log.info(f"Удалена пустая папка: {directory}")
        except Exception as e:
            log.warning(f"Не удалось удалить папку {directory}: {e}")
    
    def run_conversion(self) -> bool:
        """
        Запуск процесса конвертации для всех файлов компании
        
        Returns:
            True если конвертация завершена (даже с ошибками)
        """
        log.info(f"Запуск конвертации файлов для компании: {self.company_directory}")
        
        try:
            # Обнаружение файлов для конвертации
            files_to_convert = self.discover_files_to_convert()
            
            if not files_to_convert:
                log.info(f"Не найдено файлов для конвертации в: {self.company_directory}")
                return True
            
            log.info(f"Начинаем конвертацию {len(files_to_convert)} файлов")
            
            # Конвертация файлов в многопоточном режиме
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                results = list(executor.map(self.convert_single_file, files_to_convert))
            self.last_results = results
            
            # Статистика
            successful = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            
            log.info(f"Конвертация завершена. Успешно: {successful}, Ошибок: {failed}")
            
            # Удаляем пустые папки если настроено
            if self.delete_empty_folders:
                self.remove_empty_folders()
            
            # Логируем ошибки в отдельный файл
            if self._conversion_errors:
                error_log_path = self.company_directory.parent / "conversion_errors.log"
                with open(error_log_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n=== Конвертация компании: {self.company_directory.name} ===\n")
                    f.write(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    for error in self._conversion_errors:
                        f.write(f"{error}\n")
                    f.write("=" * 50 + "\n")
                
                log.warning(f"Ошибки конвертации сохранены в: {error_log_path}")
            
            return True
            
        except Exception as e:
            log.error(f"Критическая ошибка конвертации: {e}")
            return False

def main():
    """Основная функция для запуска конвертера"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Конвертер файлов в PDF')
    parser.add_argument('--company-dir', required=True, 
                       help='Директория компании')
    parser.add_argument('--libreoffice-path', 
                       default="/usr/bin/soffice",
                       help='Путь к LibreOffice')
    parser.add_argument('--max-workers', type=int, default=4,
                       help='Максимальное количество потоков')
    parser.add_argument('--log-file', default='file_conversion.log',
                       help='Файл для логирования')
    parser.add_argument('--delete-originals', action='store_true', default=True,
                       help='Удалять оригиналы после конвертации')
    parser.add_argument('--delete-empty-folders', action='store_true', default=True,
                       help='Удалять пустые папки')
    
    args = parser.parse_args()
    
    # Создание и запуск конвертера
    converter = FileConverter(
        company_directory=args.company_dir,
        libreoffice_path=args.libreoffice_path,
        max_workers=args.max_workers,
        log_file=args.log_file,
        delete_originals=args.delete_originals,
        delete_empty_folders=args.delete_empty_folders
    )
    
    # Запуск конвертации
    success = converter.run_conversion()
    
    # Возвращаем код выхода
    return 0 if success else 1

if __name__ == "__main__":
    exit(main())
