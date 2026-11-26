"""Тесты замирания пайплайна на стадии PDF-конвертации (прогон-2, 21-26.08).

Прогон замер на 14 часов (25.08 15:39:32 -> 26.08 05:43:48) внутри file_converter,
и сторож простоя D101 этого не увидел. Причин две, обе проверяются здесь.

  * FileConverter создаётся на каждую компанию, а логгер модульный и общий: хендлеры
    копились от компании к компании. На 466-й компании одна строка писалась в
    file_conversion.log 464 раза (файл распух до 2.9 ГБ), и столько же дескрипторов
    файла оставались открытыми при лимите процесса `Max open files` = 1024.
  * run_conversion() вызывался из корутины синхронно и блокировал event loop на всё
    время конвертации, поэтому сторож простоя (корутина того же цикла) на всей стадии
    конвертации был структурно слеп — независимо от порога.
"""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import file_converter
from file_converter import FileConverter

COMPANIES = 5           # сколько компаний проходит через конвертер в тесте
LOG_NAME = 'file_conversion.log'


@pytest.fixture(autouse=True)
def clean_converter_logger():
    """Логгер модульный — снимаем следы соседних тестов до и после."""
    def _clear():
        for handler in file_converter.log.handlers[:]:
            file_converter.log.removeHandler(handler)
            handler.close()

    _clear()
    yield
    _clear()


def _converter_for_new_company(root: Path, index: int) -> FileConverter:
    """Конвертер очередной компании: в бою у всех компаний общий родитель (Documents/)."""
    company_dir = root / f'company_{index}'
    company_dir.mkdir(parents=True)
    # soffice в dev-окружении нет, а _check_dependencies требует существующий путь
    return FileConverter(company_directory=str(company_dir), libreoffice_path=sys.executable)


def test_handlers_not_accumulated(tmp_path):
    """Хендлеры не копятся от компании к компании."""
    for i in range(COMPANIES):
        _converter_for_new_company(tmp_path, i)

    file_handlers = [h for h in file_converter.log.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1, f'FileHandler накопилось: {len(file_handlers)} на {COMPANIES} компаний'


def test_line_written_once(tmp_path):
    """Одна запись в лог = одна строка в файле, сколько бы компаний ни прошло."""
    for i in range(COMPANIES):
        _converter_for_new_company(tmp_path, i)

    file_converter.log.info('маркер')
    for handler in file_converter.log.handlers:
        handler.flush()

    written = (tmp_path / LOG_NAME).read_text(encoding='utf-8')
    assert written.count('маркер') == 1, f'строка записана {written.count("маркер")} раз'


def test_log_file_descriptors_not_leaked(tmp_path):
    """Каждый незакрытый FileHandler держит дескриптор; лимит процесса — 1024."""
    def _open_log_fds() -> int:
        opened = 0
        for fd in os.listdir('/proc/self/fd'):
            try:
                if os.readlink(f'/proc/self/fd/{fd}').endswith(LOG_NAME):
                    opened += 1
            except OSError:
                pass  # дескриптор закрылся между listdir и readlink
        return opened

    for i in range(COMPANIES):
        _converter_for_new_company(tmp_path, i)

    assert _open_log_fds() == 1, f'дескрипторы {LOG_NAME} утекают: открыто {_open_log_fds()}'


def test_conversion_does_not_block_event_loop(tmp_path, monkeypatch):
    """Пока идёт конвертация, event loop продолжает крутиться — иначе сторож простоя слеп."""
    import main

    CONVERSION_SECONDS = 0.5
    TICK_SECONDS = 0.05

    class BlockingConverter:
        """Заглушка LibreOffice: run_conversion блокирует поток, как ThreadPoolExecutor в бою."""
        last_results = []

        def __init__(self, **kwargs):
            pass

        def run_conversion(self):
            time.sleep(CONVERSION_SECONDS)
            return True

    monkeypatch.setattr(main, 'FileConverter', BlockingConverter)

    system = SimpleNamespace(
        config=SimpleNamespace(
            libreoffice_path='/usr/bin/soffice',
            converter_max_workers=4,
            converter_log_file=LOG_NAME,
            delete_originals_after_conversion=True,
            delete_empty_folders=True,
            reports_dir=str(tmp_path),
        ),
        processing_tracker=SimpleNamespace(add_clean=lambda *args: None),
    )

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(TICK_SECONDS)
                ticks += 1

        beating = asyncio.ensure_future(heartbeat())
        success = await main.MonitoringSystem._convert_company_files(system, str(tmp_path))
        beating.cancel()
        return success, ticks

    success, ticks = asyncio.run(run())

    assert success is True
    expected = CONVERSION_SECONDS / TICK_SECONDS
    assert ticks >= expected / 2, f'event loop стоял во время конвертации: тиков {ticks}, ожидалось ~{expected:.0f}'
