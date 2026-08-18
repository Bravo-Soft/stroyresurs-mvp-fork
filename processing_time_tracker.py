# processing_time_tracker.py 1.0.0
# Ран-скоуп сбор пер-(компания, модуль) таймингов «чистой обработки» для листа
# «Статистика по времени обработки» (Вариант A: сетевой round-trip = обработка;
# из «чистого» времени исключаются ретраи и ожидания слота/семафора/rate-limit).
#
# Компании обрабатываются последовательно (внутри компании — конкуренция страниц),
# поэтому «текущая компания» держится указателем set_current_company().
# Исключения вложенных ожиданий реализованы через contextvars (безопасно при asyncio-
# конкуренции: каждый task видит свой держатель активного clean-спана).

import time
import threading
import contextvars
import statistics as _pystats
from contextlib import contextmanager
from collections import defaultdict

# Порядок и подписи модулей — единый источник для рендерера листа 4.
MODULE_ORDER = ['crawler', 'text', 'ai', 'cards', 'files', 'pdf', 'graph']
MODULE_LABELS = {
    'crawler': ('Краулер', 'страниц'),
    'text':    ('Извлечение текста', 'страниц'),
    'ai':      ('AI-извлечение', 'единиц'),
    'cards':   ('Генерация карточек', 'шт'),
    'files':   ('Скачивание файлов', 'файлов'),
    'pdf':     ('Конвертация в PDF', 'файлов'),
    'graph':   ('Загрузка в граф', 'чанков'),
}
# Ретраи структурно возможны только у этих модулей.
RETRY_MODULES = {'crawler', 'ai', 'graph'}

# Держатель [excluded_seconds] активного clean-спана текущего asyncio-task/потока.
_exclude_holder = contextvars.ContextVar('ptt_exclude_holder', default=None)


class ProcessingTimeTracker:
    """Потокобезопасный сборщик таймингов обработки по (компания, модуль)."""

    def __init__(self):
        self._clean = defaultdict(list)   # (company_id, module) -> [clean_seconds, ...]
        self._retry = defaultdict(float)  # (company_id, module) -> retry_seconds_total
        self._current = None
        self._lock = threading.Lock()

    # --- контекст текущей компании -------------------------------------------
    def set_current_company(self, company_id):
        self._current = str(company_id) if company_id is not None else None

    def _cid(self, company_id):
        return str(company_id) if company_id is not None else self._current

    # --- прямая запись --------------------------------------------------------
    def add_clean(self, module, seconds, company_id=None):
        cid = self._cid(company_id)
        if cid is None or seconds < 0:
            return
        with self._lock:
            self._clean[(cid, module)].append(seconds)

    def add_retry(self, module, seconds, company_id=None):
        cid = self._cid(company_id)
        if cid is None or seconds <= 0:
            return
        with self._lock:
            self._retry[(cid, module)] += seconds

    # --- контекст-менеджеры ---------------------------------------------------
    @contextmanager
    def clean_span(self, module, company_id=None):
        """Мерит чистое время одной единицы. Вложенные exclude()/retry_span()/
        attempt(retry=True) вычитаются из результата."""
        holder = [0.0]
        token = _exclude_holder.set(holder)
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            _exclude_holder.reset(token)
            self.add_clean(module, elapsed - holder[0], company_id)

    @contextmanager
    def exclude(self):
        """Исключить обёрнутое ожидание (слот браузера/семафор/rate-limit) из
        активного clean-спана. Вне clean-спана — no-op."""
        holder = _exclude_holder.get()
        if holder is None:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            holder[0] += time.perf_counter() - t0

    @contextmanager
    def retry_span(self, module, company_id=None):
        """Обёрнутое время → в ретраи модуля (и вычитается из активного clean-спана)."""
        holder = _exclude_holder.get()
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            if holder is not None:
                holder[0] += dt
            self.add_retry(module, dt, company_id)

    @contextmanager
    def attempt(self, module, company_id=None):
        """Одна попытка запроса. Если внутри выставить state['retry']=True — её
        время уходит в ретраи (и вычитается из чистого)."""
        state = {'retry': False}
        holder = _exclude_holder.get()
        t0 = time.perf_counter()
        try:
            yield state
        finally:
            dt = time.perf_counter() - t0
            if state['retry']:
                if holder is not None:
                    holder[0] += dt
                self.add_retry(module, dt, company_id)

    # --- сводки ---------------------------------------------------------------
    def company_summary(self, company_id, module):
        cid = str(company_id)
        with self._lock:
            xs = self._clean.get((cid, module), [])
            return {
                'count': len(xs),
                'total': sum(xs),
                'median': _pystats.median(xs) if xs else 0.0,
                'retry': self._retry.get((cid, module), 0.0),
            }

    def global_summary(self, module):
        with self._lock:
            xs = []
            retry = 0.0
            for (cid, m), lst in self._clean.items():
                if m == module:
                    xs.extend(lst)
            for (cid, m), r in self._retry.items():
                if m == module:
                    retry += r
            return {
                'count': len(xs),
                'total': sum(xs),
                'median': _pystats.median(xs) if xs else 0.0,
                'retry': retry,
            }

    def has_any_data(self):
        with self._lock:
            return bool(self._clean or self._retry)
