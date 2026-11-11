# api_client.py
import aiohttp
import asyncio
import logging
from datetime import datetime
import json
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

log = logging.getLogger("api_client")

def ensure_utf8_filename(filename: str) -> str:
    try:
        filename.encode('utf-8')
        return filename
    except UnicodeEncodeError as e:
        log.warning(f"Проблема с кодировкой имени файла '{filename}': {e}")
        return "report.zip"
    
class ApiClient:
    """Клиент для работы с API сервера"""
    
    def __init__(self, config):
        self.config = config
        self.upload_url = config.report_upload_url
        self.delete_file_url = config.report_delete_file_url
        self.delete_dir_url = config.report_delete_dir_url
        self.download_url = config.report_download_url
        self.add_url = config.update_url
    
    def _format_download_url(self, date_str: str, user_id: str, filename: str) -> str:
        """
        Формирует корректный URL для скачивания файла.
        Backend ожидает:
        - путь: /download/{date}/{user_id}/{encoded_filename}
        - encoded_filename: URL-encoded
        """
        base_url = self.download_url.rstrip('/')

        # backend принимает encoded имя файла
        encoded_filename = quote(filename, safe='')

        return f"{base_url}/{date_str}/{user_id}/{encoded_filename}"

    
    async def _create_session(self):
        """Создание новой сессии"""
        return aiohttp.ClientSession()
    
    async def _send_add_request(self, payload: Dict[str, Any], retry_count: int = 0) -> bool:
        """
        Отправка данных на endpoint /add для добавления/обновления
        
        Args:
            payload: Данные для отправки
            retry_count: Количество повторных попыток
            
        Returns:
            bool: Успех операции
        """
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        try:
            log.debug(f"Отправка данных на {self.add_url}: {json.dumps(payload, ensure_ascii=False)[:500]}...")
            
            async with await self._create_session() as session:
                async with session.post(
                    self.add_url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.graph_db_request_timeout
                ) as response:
                    
                    if response.status == 200:
                        log.debug(f"Успешный ответ от API /add: {await response.text()}")
                        return True
                    else:
                        text = await response.text()
                        log.error(f"Ошибка API /add (статус {response.status}): {text}")
                        return False
                        
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if retry_count < getattr(self.config, 'max_retries', 3):
                log.warning(f"Повторная попытка {retry_count + 1}/{getattr(self.config, 'max_retries', 3)}: {e}")
                await asyncio.sleep(2 ** retry_count)
                return await self._send_add_request(payload, retry_count + 1)
            else:
                log.error(f"Превышено количество попыток: {e}")
                return False
    
    async def confirm_report_version(self, company_id, report_date, version):
        return await self._send_add_request({
            "companies": [{"id": company_id, "report_date": report_date,
                           "report_version": version}],
            "verification_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    async def upload_archive(self, archive_path: Path, date_str: str, user_id: str) -> Tuple[bool, str, Optional[str]]:
        """
        Загрузка архива на сервер.
        ВАЖНО:
        - backend сам декодирует имя файла
        - поэтому НЕЛЬЗЯ отправлять encoded filename
        """
        try:
            if not archive_path.exists():
                return False, "Архив не найден", None

            upload_path = f"{date_str}/{user_id}"
            full_url = f"{self.upload_url}/{upload_path}"

            archive_size_mb = archive_path.stat().st_size / (1024 * 1024)
            log.info(f"Загрузка архива: {archive_path.name} ({archive_size_mb:.2f} МБ) -> {full_url}")

            async with await self._create_session() as session:
                with open(archive_path, 'rb') as f:
                    form_data = aiohttp.FormData()
                    filename = archive_path.name  # Оригинальное имя файла

                    # Не кодируем имя файла
                    form_data.add_field(
                        'file',
                        f,
                        filename=filename,
                        content_type='application/x-zip-compressed'
                    )

                    async with session.post(full_url, data=form_data) as response:
                        response_text = await response.text()

                        if response.status == 200:
                            download_url = self._format_download_url(date_str, user_id, filename)
                            log.info(f"Архив успешно загружен. Ссылка: {download_url}")
                            return True, "Архив успешно загружен", download_url

                        elif response.status == 413:
                            log.error(
                                "Загрузка архива отклонена (HTTP 413): размер=%.2f МБ; "
                                "настройка бота=%s МБ; Server=%r; Content-Type=%r; ответ=%r",
                                archive_size_mb, self.config.max_archive_size_mb,
                                response.headers.get('Server'),
                                response.headers.get('Content-Type'), response_text[:2000]
                            )
                            return False, (
                                f"Сервер или прокси отклонил архив размером {archive_size_mb:.2f} МБ "
                                f"(HTTP 413). В настройках бота указано {self.config.max_archive_size_mb} МБ; "
                                "фактический лимит загрузки нужно проверить на сервере."
                            ), None

                        elif response.status == 500:
                            return False, f"Ошибка сервера: {response_text}", None

                        else:
                            return False, f"Ошибка {response.status}: {response_text}", None

        except aiohttp.ClientError as e:
            log.error(f"Сетевая ошибка при загрузке архива: {e}")
            return False, f"Сетевая ошибка: {str(e)}", None

        except Exception as e:
            log.error(f"Ошибка при загрузке архива: {e}")
            return False, f"Внутренняя ошибка: {str(e)}", None

        
    async def delete_user_directory(self, date_str: str, user_id: str) -> bool:
        """
        Удаление директории пользователя на сервере
        
        Args:
            date_str: Дата в формате ДД_ММ_ГГГГ
            user_id: ID пользователя
            
        Returns:
            bool: Успех операции
        """
        try:
            # Формируем путь для удаления
            directory_1 = date_str
            directory_2 = user_id
            delete_path = f"{directory_1}/{directory_2}"            
            
            full_url = f"{self.delete_dir_url}/{delete_path}"
            
            log.info(f"Удаление директории: {delete_path}")
            
            async with await self._create_session() as session:
                async with session.delete(full_url) as response:
                    if response.status == 200:
                        log.info(f"Директория успешно удалена: {delete_path}")
                        return True
                    elif response.status == 404:
                        log.warning(f"Директория не найдена: {delete_path}")
                        return True  # Если нет директории, считаем успехом
                    else:
                        response_text = await response.text()
                        log.error(f"Ошибка удаления директории {response.status}: {response_text}")
                        return False
                        
        except Exception as e:
            log.error(f"Ошибка при удалении директории: {e}")
            return False
    
    async def delete_archive_file(self, date_str: str, user_id: str, filename: str) -> bool:
        """
        Удаление файла архива на сервере.
        Backend ожидает encoded filename в URL.
        """
        try:
            encoded_filename = quote(filename, safe='')
            delete_path = f"{date_str}/{user_id}/{encoded_filename}"
            full_url = f"{self.delete_file_url}/{delete_path}"

            log.info(f"Удаление файла: {delete_path}")

            async with await self._create_session() as session:
                async with session.delete(full_url) as response:
                    if response.status == 200:
                        log.info(f"Файл успешно удален: {delete_path}")
                        return True

                    elif response.status == 404:
                        log.warning(f"Файл не найден: {delete_path}")
                        return True

                    else:
                        response_text = await response.text()
                        log.error(f"Ошибка удаления файла {response.status}: {response_text}")
                        return False

        except Exception as e:
            log.error(f"Ошибка при удалении файла: {e}")
            return False
