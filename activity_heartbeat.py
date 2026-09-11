# activity_heartbeat.py
"""Пульс активности процесса (D101).

Сторож зависаний в pipeline_orchestrator меряет ПРОСТОЙ, а не общее время компании:
крупные сайты честно обрабатываются до нескольких суток, и дедлайн на компанию резал бы их.

Признак живости — тот же, по которому зависание определяет оператор глазами
(«лог оборвался в 01:41»): любая запись любого модуля обновляет пульс. Поэтому пульс
реализован как logging-хендлер: он покрывает все стадии сразу (краул, LLM, генерация
документов, выгрузка) и не требует расставлять отметки по коду — а значит, ни одна
стадия не может быть забыта и ошибочно принята за зависание.

Ставится один раз при настройке логирования (main.py), читается сторожем.
"""
import logging
import time


class ActivityHeartbeat(logging.Handler):
    """Хендлер, который ничего не пишет — только запоминает момент последней записи."""

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self._last = time.monotonic()

    def emit(self, record):
        self._last = time.monotonic()

    def touch(self):
        """Явная отметка активности (например, старт компании — чтобы не унаследовать чужой простой)."""
        self._last = time.monotonic()

    def idle_seconds(self) -> float:
        """Сколько секунд не было ни одной записи в лог."""
        return time.monotonic() - self._last

    def is_installed(self) -> bool:
        """Подключён ли пульс к корневому логгеру.

        Без подключения он никогда не обновляется, и простой рос бы вечно — сторож
        принял бы за зависание любую живую компанию. Проверяется перед использованием.
        """
        return self in logging.getLogger().handlers


_heartbeat = ActivityHeartbeat()


def get_heartbeat() -> ActivityHeartbeat:
    """Пульс процесса (синглтон)."""
    return _heartbeat


def install(logger: logging.Logger = None) -> ActivityHeartbeat:
    """Подключить пульс к корневому логгеру. Идемпотентно."""
    logger = logger if logger is not None else logging.getLogger()
    if _heartbeat not in logger.handlers:
        logger.addHandler(_heartbeat)
    return _heartbeat
