# pipeline_orchestrator.py
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

from config import Config
from kafka_manager import KafkaTaskManager, TaskMessage, TaskStatus
from checkpoint_manager import CheckpointManager
from graph_db_uploader import GraphDBFatalError
from activity_heartbeat import get_heartbeat
from web_crawler import read_memory_usage_mb

log = logging.getLogger("pipeline_orchestrator")

class PipelineState(Enum):
    """Состояния пайплайна"""
    IDLE = "idle"
    PROCESSING_REGULAR = "processing_regular"
    PROCESSING_URGENT = "processing_urgent"
    PAUSED = "paused"
    STOPPED = "stopped"

class PipelineOrchestrator:
    """Оркестратор для управления переключением между regular и urgent задачами"""
    
    # D101: как часто сторож проверяет пульс активности.
    WATCHDOG_POLL_SECONDS = 30
    # D101: сколько ждать фактической отмены зависшей компании, прежде чем бросить задачу как есть.
    CANCEL_GRACE_SECONDS = 120
    # D101: потолок на пересоздание пула браузеров после брошенной компании.
    RECYCLE_TIMEOUT_SECONDS = 180

    def __init__(self, config: Config, kafka_manager: KafkaTaskManager):
        self.config = config
        self.kafka_manager = kafka_manager         
        self.checkpoint_manager = CheckpointManager(config)
        self.state = PipelineState.IDLE
        self.current_task: Optional[TaskMessage] = None
        self.regular_companies = []
        self.current_regular_index = 0

        # Флаги управления
        self._running = False
        self._urgent_queue_size = 0
        
    async def initialize(self):
        """Инициализация оркестратора"""
        try:
            # Проверяем, что KafkaManager инициализирован (но не инициализируем заново)
            log.info("PipelineOrchestrator инициализирован")
        except Exception as e:
            log.error(f"Ошибка инициализации PipelineOrchestrator: {e}")
            raise
    
    async def shutdown(self):
        """Корректное завершение работы"""
        self._running = False
        self.state = PipelineState.STOPPED
        # KafkaManager закрывается в main, здесь не закрываем
        log.info("PipelineOrchestrator остановлен")
    
    async def run_with_priority(self, monitoring_system, companies: list):
        """
        Запуск мониторинга с приоритетной обработкой urgent задач
        
        Args:
            monitoring_system: Экземпляр MonitoringSystem
            companies: Список компаний для регулярной обработки
        """
        self._running = True
        self.regular_companies = companies
        self.current_regular_index = 0
        run_results = []
        
        log.info(f"Запуск пайплайна с приоритетной обработкой urgent задач")
        log.info(f"Всего компаний для обработки: {len(companies)}")
        
        # Загрузка состояния из чекпоинта
        await self._restore_state(monitoring_system)
        
        # Восстановление отложенных отправок в Graph DB
        log.info("Перед началом обработки новых компаний проверяем отложенные отправки...")
        if not await monitoring_system.resume_pending_graphdb_uploads():
            log.critical("Восстановление отложенных отправок не удалось. Пайплайн останавливается.")
            self._running = False
            return
        
        # Основной цикл
        while self._running and self.current_regular_index < len(companies):
            try:
                # Проверяем urgent задачи перед каждой компанией
                await self._check_and_process_urgent_tasks(monitoring_system)
                
                # Обрабатываем регулярную компанию
                company = companies[self.current_regular_index]
                log.info(f"Обработка регулярной компании {self.current_regular_index+1}/{len(companies)}: {company['original_name']}")
                
                self.state = PipelineState.PROCESSING_REGULAR
                
                # Обработка компании под сторожем (может выбросить GraphDBFatalError)
                result = await self._process_company_watchdogged(monitoring_system, company)

                if result is None:
                    # D101: компания брошена по простою — переходим к следующей
                    self.current_regular_index += 1
                    continue

                if result.get('status') == 'graph_db_fatal_error':
                    # Бэкенд недоступен: индекс не двигаем — компания будет повторена после восстановления
                    log.critical("Фатальная ошибка Graph DB: пайплайн останавливается, отправка сохранена в pending")
                    self._running = False
                    break

                # Сохраняем прогресс
                self.current_regular_index += 1
                run_results.append(result)

                if result.get('status') == 'success':
                    # Очищаем чекпоинт после успешной обработки
                    await self.checkpoint_manager.clear_checkpoint()
                else:
                    log.warning(f"Компания {company['original_name']} обработана с ошибками")

                await self._recycle_browser_pool_if_memory_high(monitoring_system)

                # Пауза между компаниями (если настроено)
                if self.current_regular_index < len(companies):
                    await asyncio.sleep(self.config.graph_db_delay_between_companies)    
                
            except Exception as e:
                log.error(f"Критическая ошибка при обработке компании: {e}")
                self.current_regular_index += 1
                continue
        
        # После завершения всех регулярных задач проверяем оставшиеся urgent
        if self._running:
            await self._process_remaining_urgent_tasks(monitoring_system)
        
        # Формирование итогового XLSX-отчёта по результатам обработки компаний
        if run_results:
            try:
                report_paths = await monitoring_system.generate_reports(run_results)
                log.info(f"Итоговый отчёт мониторинга сформирован: {report_paths}")
            except Exception as e:
                log.error(f"Не удалось сформировать итоговый отчёт мониторинга: {e}")
        
        self.state = PipelineState.IDLE
        log.info("Пайплайн завершил работу")
    
    async def _process_company_watchdogged(self, monitoring_system, company_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Обработка компании под сторожем зависаний (D101).

        Зависание на одной компании раньше вешало весь прогон навсегда: краш процесса
        chromium оставлял Playwright-вызовы без ответа, семафор пула не возвращался,
        и пайплайн засыпал с 0% CPU (прогоны 19.08 «Ладога НПО» и 21.08 «РОВЕН»).

        Меряется ПРОСТОЙ, а не общее время компании: крупные сайты честно обрабатываются
        до нескольких суток, и дедлайн на компанию резал бы их. Пока система пишет в лог —
        компания работает сколько нужно; молчание дольше config.company_idle_timeout_seconds
        означает зависание: задача отменяется, пул браузеров пересоздаётся, прогон идёт дальше.

        Возвращает результат process_company либо None, если компания брошена по простою.
        Исключения самой обработки пробрасываются как раньше.
        """
        idle_limit = self.config.company_idle_timeout_seconds
        if not idle_limit:
            return await monitoring_system.process_company(company_data)

        heartbeat = get_heartbeat()
        if not heartbeat.is_installed():
            # Пульс не подключён (логирование настроено мимо main.setup): он никогда не
            # обновится, и сторож зарубил бы любую живую компанию. Работаем без сторожа.
            log.error("Пульс активности не подключён — сторож зависаний выключен для этого прогона")
            return await monitoring_system.process_company(company_data)

        name = company_data.get('original_name')
        heartbeat.touch()  # не наследуем простой, накопившийся до старта компании
        task = asyncio.ensure_future(monitoring_system.process_company(company_data))

        # asyncio.wait не отменяет задачу по таймауту и не бросает её исключение —
        # используем его как «подождать не дольше X», а решение принимаем по пульсу.
        while True:
            _, pending = await asyncio.wait({task}, timeout=self.WATCHDOG_POLL_SECONDS)
            if not pending:
                return task.result()
            idle = heartbeat.idle_seconds()
            if idle >= idle_limit:
                break

        log.critical(f"Компания {name}: нет активности {idle:.0f} с (порог {idle_limit} с) — "
                     f"считаем зависанием, бросаем и идём дальше")
        task.cancel()
        _, still_pending = await asyncio.wait({task}, timeout=self.CANCEL_GRACE_SECONDS)
        if still_pending:
            # Отмена не прошла (например, cleanup Playwright на умершем браузере тоже
            # не возвращается). Бросаем задачу как есть — прогон обязан идти дальше.
            log.error(f"Зависшая задача {name} не отменилась за {self.CANCEL_GRACE_SECONDS} с — оставляем её висеть")
        else:
            log.info(f"Зависшая задача {name} отменена")

        await self._recycle_browser_pool(monitoring_system)
        return None

    async def _recycle_browser_pool(self, monitoring_system):
        """
        D101: пересоздание пула браузеров после брошенной компании.
        Без этого следующая компания стартует на мёртвом браузере и с семафором,
        разрешения которого удерживает брошенная задача. Fail-open: если пересоздать
        не удалось, прогон продолжается (компании будут падать с ошибкой, а не молча висеть).
        """
        try:
            await asyncio.wait_for(
                monitoring_system.crawler.browser_pool.recycle(),
                timeout=self.RECYCLE_TIMEOUT_SECONDS
            )
        except Exception as e:
            log.error(f"Не удалось пересоздать пул браузеров: {type(e).__name__}: {e}")

    async def _recycle_browser_pool_if_memory_high(self, monitoring_system):
        """Пересоздание пула браузеров, когда память контейнера подошла к лимиту.

        Chromium живёт один на весь прогон (initialize/close вызываются по разу),
        и внутрикраульная очистка до его памяти не дотягивается — она чистит только
        питоновские структуры. Прогон 26.08-01.09 так и умер: OOM-киллер на 685-й
        компании из 1452. Граница между компаниями — безопасная точка: контексты
        уже возвращены в пул, ронять нечего.
        """
        memory_mb = read_memory_usage_mb()
        if memory_mb <= self.config.memory_cleanup_threshold_mb:
            return

        log.warning(f"Память контейнера {memory_mb:.0f}MB превысила порог "
                    f"{self.config.memory_cleanup_threshold_mb}MB — пересоздаём пул браузеров")
        await self._recycle_browser_pool(monitoring_system)
        log.info(f"Память контейнера после пересоздания пула: {read_memory_usage_mb():.0f}MB")

    async def _check_and_process_urgent_tasks(self, monitoring_system):
        """Проверка и обработка urgent задач – если есть, обрабатываем все."""
        try:
            # Сначала получаем первую задачу
            first_task = await self.kafka_manager.get_urgent_task()
            if first_task:
                log.info("Обнаружена urgent задача в очереди, начинаем обработку")
                # Сохраняем текущий прогресс регулярной обработки
                if self.current_regular_index < len(self.regular_companies):
                    current_company = self.regular_companies[self.current_regular_index]
                    await self._save_checkpoint(monitoring_system, current_company)
                # Обрабатываем все urgent задачи, начиная с полученной
                await self._process_all_urgent_tasks(monitoring_system, first_task)
                # Возобновляем регулярную обработку
                log.info("Возобновление регулярной обработки после urgent задач")
        except Exception as e:
            log.error(f"Ошибка при проверке urgent задач: {e}")
    
    async def _process_all_urgent_tasks(self, monitoring_system, first_task: Optional[TaskMessage] = None):
        """Обработка всех urgent задач в очереди, начиная с first_task, если она есть."""
        self.state = PipelineState.PROCESSING_URGENT

        # Собираем все задачи для обработки
        tasks_to_process = []
        if first_task:
            tasks_to_process.append(first_task)

        # Получаем остальные задачи из очереди
        while True:
            try:
                urgent_task = await self.kafka_manager.get_urgent_task()
                if not urgent_task:
                    break
                tasks_to_process.append(urgent_task)
            except Exception as e:
                log.error(f"Ошибка получения urgent задачи: {e}")
                continue

        # Обрабатываем все собранные задачи
        for urgent_task in tasks_to_process:
            try:
                log.info(f"Начало обработки URGENT задачи: {urgent_task.task_id}")
                # Отправляем статус "в обработке"
                await self.kafka_manager.send_task_status(
                    urgent_task.task_id,
                    TaskStatus.PROCESSING,
                    f"Начата обработка компании: {urgent_task.company_data.get('original_name')}",
                    progress=0.0
                )
                # Обрабатываем компанию под тем же сторожем, что и регулярную (D101)
                result = await self._process_company_watchdogged(monitoring_system, urgent_task.company_data)
                timed_out = result is None
                if timed_out:
                    result = {'status': 'error',
                              'error': f'зависание: нет активности дольше {self.config.company_idle_timeout_seconds} с'}
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
                    log.error(f"URGENT задача {urgent_task.task_id} завершена с ошибкой")
                    if timed_out:
                        # D101: повтор зависшей компании снова упрётся в сторож — только зря сожжёт ещё один порог простоя
                        log.error(f"URGENT задача {urgent_task.task_id} брошена по простою, повтор не назначаем")
                    else:
                        await self._retry_failed_urgent_task(urgent_task)
                    if result.get('status') == 'graph_db_fatal_error':
                        # Бэкенд недоступен — нет смысла обрабатывать остальные urgent задачи
                        log.critical("Фатальная ошибка Graph DB при urgent задаче: останавливаем обработку urgent очереди")
                        self._running = False
                        break
            except Exception as e:
                log.error(f"Ошибка обработки urgent задачи {urgent_task.task_id}: {e}")
                try:
                    await self.kafka_manager.send_task_status(
                        urgent_task.task_id,
                        TaskStatus.FAILED,
                        f"Критическая ошибка: {str(e)}",
                        progress=100.0
                    )
                except:
                    pass
                continue
    
    async def _process_remaining_urgent_tasks(self, monitoring_system):
        """Обработка оставшихся urgent задач после регулярных"""
        log.info("Проверка оставшихся urgent задач после завершения регулярных")
        await self._process_all_urgent_tasks(monitoring_system)
    
    async def _save_checkpoint(self, monitoring_system, company):
        """Сохранение чекпоинта"""
        try:
            await self.checkpoint_manager.save_checkpoint(
                company_data=company,
                processed_urls=monitoring_system._global_processed_urls,
                stage="paused_for_urgent",
                progress={
                    "status": "paused",
                    "current_company_index": self.current_regular_index,
                    "total_companies": len(self.regular_companies),
                    "urgent_queue_size": self._urgent_queue_size
                }
            )
            log.info(f"Чекпоинт сохранен перед обработкой urgent задач")
        except Exception as e:
            log.error(f"Ошибка сохранения чекпоинта: {e}")
    
    async def _restore_state(self, monitoring_system):
        """Восстановление состояния из чекпоинта"""
        checkpoint_data = await self.checkpoint_manager.load_checkpoint()
        
        if checkpoint_data:
            log.info(f"Восстановление состояния из чекпоинта: {checkpoint_data.current_stage}")

            # Восстанавливаем обработанные URL — но ТОЛЬКО для прерванной посреди
            # обработки компании. Чекпоинт стадии company_completed переживает останов
            # только из-за фатальной ошибки Graph DB (clear_checkpoint не достигается),
            # и его processed_urls — это ВСЕ страницы завершённой компании: их рестор
            # заставлял следующий прогон молча (debug-лог) пропускать эти страницы и
            # извлекать только дополнение — наборы товаров «вращались» между прогонами
            # (17.08: прогоны 199/260/95 товаров с пересечением 0 между соседними).
            if checkpoint_data.current_stage == 'company_completed':
                log.info("Чекпоинт стадии company_completed: обработанные URL не восстанавливаем "
                         "(компания была завершена; повторный прогон должен обработать её заново)")
            else:
                monitoring_system._global_processed_urls.update(checkpoint_data.processed_urls)
            
            # Находим индекс компании
            if 'current_company_index' in checkpoint_data.progress:
                self.current_regular_index = checkpoint_data.progress['current_company_index']
                log.info(f"Восстановлен индекс компании: {self.current_regular_index}")
    
    async def _retry_failed_urgent_task(self, urgent_task: TaskMessage):
        """Повторная отправка failed urgent задачи"""
        try:
            log.info(f"Повторная отправка failed urgent задачи: {urgent_task.task_id}")
            
            retry_task_id = f"retry_{urgent_task.task_id}"
            retry_task = TaskMessage(
                task_id=retry_task_id,
                task_type=urgent_task.task_type,
                company_data=urgent_task.company_data,
                user_id=urgent_task.user_id,
                priority=urgent_task.priority + 10  # Увеличиваем приоритет для повторной обработки
            )
            
            await self.kafka_manager.send_urgent_task(
                retry_task.company_data,
                retry_task.user_id
            )
            
            log.info(f"Failed задача отправлена на повторную обработку как {retry_task_id}")
            
        except Exception as e:
            log.error(f"Ошибка повторной отправки failed задачи: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Получение текущего статуса пайплайна"""
        return {
            "state": self.state.value,
            "running": self._running,
            "current_regular_index": self.current_regular_index,
            "total_regular_companies": len(self.regular_companies),
            "urgent_queue_size": self._urgent_queue_size,
            "current_task": self.current_task.task_id if self.current_task else None,
            "timestamp": datetime.now().isoformat()
        }