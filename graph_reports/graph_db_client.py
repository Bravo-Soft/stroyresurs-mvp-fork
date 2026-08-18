# graph_db_client 1.0.0
import aiohttp
import asyncio
import json
import logging
import os
from typing import Dict, List, Any, Optional

log = logging.getLogger("graph_db_client")

class GraphDBClient:
    """Клиент для работы с Graph DB API"""
    
    def __init__(self, config):
        self.config = config
        self.api_url = config.graph_db_api_url.rstrip('/')
        # Разделяем эндпоинты для получения и добавления данных
        self.get_url = f"{self.api_url}/get"  # Для получения данных
#        self.get_url = "http://192.168.0.123:4242/api/companies/get"  # Для получения данных
        self.add_url = f"{self.api_url}/add"  # Для добавления данных
        print(f"DEBUG: GraphDB GET URL: {self.get_url}")
        print(f"DEBUG: GraphDB ADD URL: {self.add_url}")
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.graph_db_request_timeout)
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _make_request(self, payload: Dict[str, Any], retry_count: int = 0) -> List[Dict[str, Any]]:
        """Выполнить запрос к API с повторными попытками"""
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        try:
            log.debug(f"Отправка запроса к Graph DB (GET): {json.dumps(payload, ensure_ascii=False)[:500]}...")
            log.debug(f"DEBUG: URL запроса: {self.get_url}")
            log.debug(f"DEBUG: Headers: {headers}")
            log.debug(f"DEBUG: Payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
            async with self.session.post(
                self.get_url,  # Используем эндпоинт /get для получения данных
                headers=headers,
                json=payload,
#                timeout=self.config.graph_db_request_timeout
                timeout=600 
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    log.debug(f"DEBUG: Полный ответ от Graph DB: {json.dumps(data, ensure_ascii=False, indent=2)}")
                    log.debug(f"Получен ответ от Graph DB: {len(data)} компаний")
                    return data
                else:
                    text = await response.text()
                    log.error(f"Ошибка Graph DB API (статус {response.status}): {text}")
                    raise Exception(f"Graph DB API error: {response.status} - {text}")
                    
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if retry_count < self.config.max_retries:
                log.warning(f"Повторная попытка {retry_count + 1}/{self.config.max_retries}: {e}")
                await asyncio.sleep(2 ** retry_count)
                return await self._make_request(payload, retry_count + 1)
            else:
                log.error(f"Превышено количество попыток: {e}")
                raise
    
    async def get_all_companies_data(self) -> List[Dict[str, Any]]:
        """Получить все данные по всем компаниям"""
        payload = {}
        return await self._make_request(payload)
    
    async def get_companies_data_on_date(self, valid_on_date: str) -> List[Dict[str, Any]]:
        """Получить данные на указанную дату"""
        payload = {
            "dates": {
                "valid_on_date": valid_on_date
            }
        }
        return await self._make_request(payload)
    
    async def get_changes_after_date(self, changes_after_date: str) -> List[Dict[str, Any]]:
        """Получить изменения после указанной даты"""
        payload = {
            "dates": {
                "changes_after_date": changes_after_date
            }
        }
        return await self._make_request(payload)
    
    async def get_company_by_id(self, company_id: str) -> List[Dict[str, Any]]:
        """Получить данные по конкретной компании"""
        payload = {
            "vector": {
                "id": company_id
            }
        }
        return await self._make_request(payload)
    
    async def get_companies_only(self) -> List[Dict[str, Any]]:
        """Получить только данные компаний (без продуктов и поставщиков)"""
        payload = {
            "recursion": {
                "use": False
            }
        }
        return await self._make_request(payload)
    
    async def get_companies_with_products_and_suppliers(self, valid_on_date: Optional[str] = None,
                                                       changes_after_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Получить данные компаний с продуктами и поставщиками"""
        payload = {}
        
        if valid_on_date and changes_after_date:
            payload["dates"] = {
                "valid_on_date": valid_on_date,
                "changes_after_date": changes_after_date
            }
        elif valid_on_date:
            payload["dates"] = {
                "valid_on_date": valid_on_date
            }
        elif changes_after_date:
            payload["dates"] = {
                "changes_after_date": changes_after_date
            }
        
        return await self._make_request(payload)
    
    async def _send_add_request(self, payload: Dict[str, Any], retry_count: int = 0) -> bool:
        """
        Отправка данных на endpoint /add для добавления/обновления
        """
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        try:
            log.debug(f"Отправка данных на {self.add_url}: {json.dumps(payload, ensure_ascii=False)[:500]}...")
            
            async with self.session.post(
                self.add_url,  # Используем эндпоинт /add для добавления данных
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
            
    async def update_company_report_date(self, company_id: str, report_date: str) -> bool:
        """
        Обновление report_date для компании
        
        Args:
            company_id: ID компании
            report_date: Новая дата отчета
            
        Returns:
            bool: Успех операции
        """
        try:
            payload = {
                "companies": [{
                    "id": company_id,
                    "report_date": report_date
                }],
                "verification_date": report_date
            }
            
            success = await self._send_add_request(payload)
            
            if success:
                log.info(f"report_date обновлен для компании {company_id}: {report_date}")
                return True
            else:
                log.error(f"Не удалось обновить report_date для компании {company_id}")
                return False
                
        except Exception as e:
            log.error(f"Ошибка обновления report_date для компании {company_id}: {e}")
            return False
