"""Результат загрузки и изолированные свидетельства транспортов одной попытки."""
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any, Optional
import math
import time
import re
from bs4 import BeautifulSoup
from data_island import has_product_island


@dataclass
class FetchOutcome:
    requested_url: str
    final_url: str = ''
    transport: str = ''
    status: Optional[int] = None
    reason: str = 'empty'
    retryable: bool = True
    retry_after: float = 0.0
    content: Any = None
    evidence: str = ''


fetch_observer = ContextVar('fetch_observer', default=None)


def retry_after_seconds(value, now=None) -> float:
    """Retry-After: секунды либо HTTP-date. Прошедшая дата даёт ноль."""
    if value is None:
        return 0.0
    try:
        text = str(value).strip()
        if re.fullmatch(r'\d+', text):
            seconds = float(text)
        else:
            date = parsedate_to_datetime(text)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - (time.time() if now is None else now)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def is_soft_not_found(html) -> bool:
    """Отсутствующая страница, а не временная заглушка или одинаковые товары."""
    if not html or has_product_island(html):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    pattern = re.compile(
        r'^(?:ошибка|error)\s*[: -]?\s*(?:404|410)\s*[.!]?$|'
        r'^(?:404|410)\s*[-:–—]?\s*(?:not found|gone|страница не найдена|страница удалена)(?:\b|$)|'
        r'^(?:page not found|not found|gone|страница не найдена|страница удалена)(?:[.!]|$)', re.I)
    return any(pattern.search(' '.join(tag.stripped_strings)) for tag in soup.find_all(['title', 'h1']))


def observe_fetch(url, transport, *, status=None, final_url='', headers=None,
                  html='', reason=None, error=None):
    """Записывает факт ответа в контекст текущей загрузки, без общего last_error."""
    retryable = True
    if reason is None:
        if error is not None:
            if isinstance(error, TimeoutError) or 'timeout' in type(error).__name__.lower():
                reason = 'timeout'
            elif isinstance(error, OSError) or type(error).__module__.startswith(('aiohttp', 'curl_cffi', 'playwright')):
                reason = 'network_error'
            else:
                reason, retryable = 'internal_error', False
        elif status in (404, 410):
            reason, retryable = ('not_found' if status == 404 else 'gone'), False
        elif status == 408:
            reason = 'timeout'
        elif status in (None, 0):
            reason = 'empty'
        elif status == 429:
            reason = 'rate_limited'
        elif status is not None and status >= 500:
            reason = 'unavailable'
        elif status in (401, 403):
            reason = 'blocked'
        elif status == 200:
            reason, retryable = 'ok', False
        else:
            reason, retryable = 'http_error', False
    else:
        retryable = reason not in ('ok', 'not_found', 'gone', 'soft_not_found', 'out_of_scope', 'internal_error', 'http_error')
    if status == 200 and reason == 'ok':
        # Импорт в момент вызова: общая проверка HTML принадлежит краулеру.
        from web_crawler import page_error_reason
        rejected = page_error_reason(html)
        if rejected:
            reason = ('soft_not_found' if is_soft_not_found(html) else
                      'blocked' if rejected == 'challenge' else
                      'empty' if rejected == 'empty' else 'unavailable')
            retryable = reason != 'soft_not_found'
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    outcome = FetchOutcome(url, final_url or url, transport, status, reason, retryable,
                           retry_after_seconds(lowered.get('retry-after')), evidence=html if reason == 'soft_not_found' else '')
    observer = fetch_observer.get()
    if observer is not None:
        observer(outcome)
    return outcome
