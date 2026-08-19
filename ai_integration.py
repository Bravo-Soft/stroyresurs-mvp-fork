# ai_integration.py
import aiohttp
import logging
from typing import Dict, Any, Optional, Tuple, List
import asyncio
import random
from rate_limiter import RateLimiter
import time
import tiktoken
import json
import re
import os
from models import ReasoningOutput, ProductOutput
from datetime import datetime
from contextlib import nullcontext

log = logging.getLogger("ai_integration")


def _coerce_bool(v) -> bool:
    """Приводит значение к bool. Модель под json_object иногда отдаёт строку "true"."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "да")
    return bool(v)


def _first(d: dict, *keys):
    """Возвращает первое не-None значение из d по списку альтернативных ключей."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _normalize_reasoning_keys(d: dict) -> dict:
    """Приводит «дрейфующие» имена ключей ответа SGR-REASONER к канонической схеме
    models.ReasoningOutput.

    Под response_format=json_object модель не обязана соблюдать имена ключей и нередко
    их переименовывает (step_1_is_product_page, step_2_extract_raw_blocks,
    step_4_extract_specifications) или уплощает (is_product_page/raw_h1 на верхнем уровне).
    Раньше код жёстко искал "step_1_page_classification" и при несовпадении молча считал
    страницу непродуктовой → ложный Trash_418#. Эта нормализация СПАСАЕТ такие ответы,
    не теряя данные и не уменьшая долю валидных ответов.
    """
    if not isinstance(d, dict):
        return d

    # 1. Классификация. Пропускаем дефолт, который мог вставить сам код.
    cls = None
    for k in ("step_1_page_classification", "step_1_is_product_page", "step_1_classification"):
        v = d.get(k)
        if isinstance(v, dict) and "is_product_page" in v and v.get("reason") != "Missing classification from model":
            cls = dict(v)
            break
    if cls is None and "is_product_page" in d:  # плоский вариант (ключи на верхнем уровне)
        cls = {
            "is_product_page": d.get("is_product_page"),
            "tech_params_count": d.get("tech_params_count", 0),
            "has_enough_params": d.get("has_enough_params"),
            "reason": d.get("reason") or d.get("reasoning", ""),
        }
    if isinstance(cls, dict):
        cls["is_product_page"] = _coerce_bool(cls.get("is_product_page"))
        cls.setdefault("reason", cls.get("reasoning", ""))
        cls.setdefault("tech_params_count", 0)

    # 2. Сырые блоки
    rb = _first(d, "step_3_extract_raw_blocks", "step_2_extract_raw_blocks", "raw_blocks")
    if not isinstance(rb, dict):
        rb = {
            "raw_h1": d.get("raw_h1", ""),
            "raw_sections": d.get("raw_sections", []),
            "raw_tables": d.get("raw_tables", []),
            "raw_lists": d.get("raw_lists", []),
        }

    # 3. Роутинг секций
    sr = _first(d, "step_5_section_routing", "step_3_semantic_routing",
                "step_5_semantic_routing", "section_routing", "semantic_routing")
    if not isinstance(sr, list):
        sr = []

    # 4. Спецификации
    sp = _first(d, "step_6_specifications_processing", "step_4_extract_specifications",
                "specifications_processing")
    if not isinstance(sp, dict):
        sp = {"spec_items": d.get("spec_items", []), "spec_tables": d.get("spec_tables", [])}

    images = _first(d, "step_7_collect_images", "collect_images", "images")
    if not isinstance(images, list):
        images = []
    variants = _first(d, "step_8_collect_variants", "collect_variants", "variants")
    if not isinstance(variants, list):
        variants = []
    name_norm = _first(d, "step_4_name_normalization", "name_normalization")
    err_routing = _first(d, "step_2_error_routing", "error_routing")

    return {
        "step_1_page_classification": cls if isinstance(cls, dict) else {
            "is_product_page": False,
            "reason": "Missing classification from model",
            "tech_params_count": 0,
            "has_enough_params": False,
        },
        "step_2_error_routing": err_routing if isinstance(err_routing, dict) else {"should_return_error": False, "error_json": ""},
        "step_3_extract_raw_blocks": rb,
        "step_4_name_normalization": name_norm if isinstance(name_norm, dict) else {"final_product_name": rb.get("raw_h1", "") if isinstance(rb, dict) else ""},
        "step_5_section_routing": sr,
        "step_6_specifications_processing": sp,
        "step_7_collect_images": images,
        "step_8_collect_variants": variants,
    }


def _all_balanced_objects(s: str) -> List[str]:
    """Все сбалансированные top-level {...} с учётом строк и экранирования."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(s[i:j + 1])
                        break
            j += 1
        i = j + 1
    return out


def _repair_invalid_escapes(s: str) -> str:
    """Чинит невалидные \\x внутри JSON-строк (типовая ошибка модели «Invalid \\escape»):
    обратный слэш не перед допустимым символом экранирования превращаем в \\\\."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _try_load(s: str) -> Optional[dict]:
    base = (s, re.sub(r",\s*]", "]", re.sub(r",\s*}", "}", s)))
    for cand in base + tuple(_repair_invalid_escapes(c) for c in base):
        try:
            o = json.loads(cand)
            if isinstance(o, dict):
                return o
        except Exception:
            pass
    return None


def _robust_json_extract(content: Optional[str]) -> Optional[dict]:
    """Достаёт JSON-объект из «грязного» ответа модели (нужно для gemini/glm, которые
    подмешивают reasoning <think>…</think> в content). Снимает <think>-блоки, ```-ограждения,
    берёт сбалансированный {...}, чинит висячие запятые. Возвращает dict или None.
    Перенесён из проверенного стенда A/B (Анализ системы/_model_ab/common.py)."""
    if not content:
        return None
    txt = content
    # 1) убрать reasoning-блоки
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S | re.I)
    txt = re.sub(r"<reasoning>.*?</reasoning>", "", txt, flags=re.S | re.I)
    # незакрытый <think> в начале → срезать до конца тега, если закрытие потерялось
    if "<think>" in txt.lower() and "</think>" not in txt.lower():
        txt = re.sub(r".*<think>", "", txt, flags=re.S | re.I)
    # 2) снять code-fences
    txt = re.sub(r"```(?:json)?", "", txt, flags=re.I)
    # 3) прямой разбор
    for cand in (txt, content):
        o = _try_load(cand)
        if o is not None:
            return o
    # 4) все сбалансированные объекты → лучший (с 'product'/'error', иначе максимум ключей)
    parsed = [o for o in (_try_load(b) for b in _all_balanced_objects(txt)) if o]
    if parsed:
        preferred = [o for o in parsed if "product" in o or "product_name" in o or "error" in o]
        pool = preferred or parsed
        return max(pool, key=lambda o: len(json.dumps(o)))
    return None


# Пер-модельный тюнинг Ollama Cloud (по итогам A/B, _model_ab/common.TUNING + REPORT §4):
#   effort       — reasoning_effort (или None → не слать); гасит «размышление», где оно вредит
#   thinking_off — слать ли thinking:{"type":"disabled"}
#   num_ctx      — размер контекста (или None)
#   robust       — нужен устойчивый парсер (_robust_json_extract) — модель подмешивает <think> в content
# Архитектура (single_1call vs 2-call) — отдельно, см. _SINGLE_CALL_MODELS.
_MODEL_TUNING = {
    "gemini-3-flash-preview": {"effort": "low",  "thinking_off": False, "num_ctx": None,  "robust": True},
    "glm-5.2":                {"effort": "none", "thinking_off": False, "num_ctx": None,  "robust": True},
    "deepseek-v3.2":          {"effort": "none", "thinking_off": False, "num_ctx": None,  "robust": True},
    "deepseek-v4-flash":      {"effort": None,   "thinking_off": False, "num_ctx": None,  "robust": False},
    # прод с 2026-08-18 (решение заказчика): тот же тюнинг, что у deepseek-v4-flash,
    # закреплённый тег 0731; архитектура — 2-вызовная SGR (в _SINGLE_CALL_MODELS не входит).
    # max_tokens=16384 — защита от «убегающего reasoning»: модель стохастически зацикливается
    # в размышлении, выжигает серверный бюджет 65536 ток. (~5 мин) и отдаёт ПУСТОЙ content
    # (прогон 2026-08-18: 119 пустых ответов). Успешные ответы укладываются в ~5k ток.,
    # кап 16384 обрывает зацикленную попытку в ~4 раза быстрее — её добивает ретрай.
    # thinking:{disabled} и reasoning_effort модель на Ollama ИГНОРИРУЕТ (проверено зондом).
    "deepseek-v4-flash:0731": {"effort": None,   "thinking_off": False, "num_ctx": None,  "robust": False,
                               "max_tokens": 16384},
    # gemma4 — прод с 2026-07-27 (полный прогон 1142 стр. + A/B: Анализ системы/_gemma4_test/REPORT.md).
    # effort=None (не-reasoning модель, reasoning_effort не слать), robust=True (оборачивает в ```json).
    "gemma4:31b":             {"effort": None,   "thinking_off": False, "num_ctx": None,  "robust": True},
    "gpt-oss:120b":           {"effort": "low",  "thinking_off": False, "num_ctx": None,  "robust": True},
    "gpt-oss:20b":            {"effort": "low",  "thinking_off": False, "num_ctx": None,  "robust": True},
    "qwen3-coder-next":       {"effort": None,   "thinking_off": False, "num_ctx": 32768, "robust": False},
    "minimax-m3":             {"effort": "none", "thinking_off": True,  "num_ctx": None,  "robust": True},
}
_DEFAULT_TUNING = {"effort": "none", "thinking_off": False, "num_ctx": None, "robust": True}


def _tuning_for(model: str) -> dict:
    return _MODEL_TUNING.get(model, _DEFAULT_TUNING)


def _effort_for_model(model: str) -> str:
    return _tuning_for(model)["effort"] or "none"


def _payload_tuning(model: str) -> dict:
    """Доп. поля payload по тюнингу модели: reasoning_effort / thinking / num_ctx."""
    t = _tuning_for(model)
    extra = {}
    if t["effort"]:
        extra["reasoning_effort"] = t["effort"]
    if t["thinking_off"]:
        extra["thinking"] = {"type": "disabled"}
    if t["num_ctx"]:
        extra["num_ctx"] = t["num_ctx"]
    if t.get("max_tokens"):
        extra["max_tokens"] = t["max_tokens"]
    return extra


# Модели, у которых единый вызов (single_1call) даёт корректный финальный JSON (отчёт A/B §4).
# Для остальных (deepseek-v3.2/v4-flash, qwen, minimax) наивное объединение промптов ломает вывод
# (модель печатает промежуточную step_-структуру) → используем 2-вызовную SGR.
_SINGLE_CALL_MODELS = {"gemini-3-flash-preview", "glm-5.2", "gemma4:31b"}

# D78: детерминированная нормализация латинских (машинных) ключей характеристик в русские.
# Сайты вида ekontaktor.ru (Next.js) отдают в разметке англ-блок data-props; извлечение иногда
# берёт его вместо русской таблицы. Промпт-правила это уменьшают, но LLM недетерминирован —
# ключи гарантируем детерминированно здесь (не трогая значения).
_SPEC_KEY_RU = {
    "aux_contacts": "Вспомогательные контакты",
    "coil_voltage_ac": "Напряжение катушки (переменный ток)",
    "coil_voltage_dc": "Напряжение катушки (постоянный ток)",
    "coil_voltage": "Напряжение катушки",
    "frequency": "Номинальная частота",
    "overall_dimensions": "Габаритные и установочные размеры",
    "nominal_voltage": "Номинальное напряжение",
    "rated_voltage": "Номинальное напряжение",
    "nominal_current": "Номинальный ток",
    "rated_current": "Номинальный ток",
    "poles": "Число полюсов",
    "number_of_poles": "Число полюсов",
}

def _ru_spec_key(k):
    if not isinstance(k, str):
        return k
    low = k.strip().lower()
    if low in _SPEC_KEY_RU:
        return _SPEC_KEY_RU[low]
    k2 = re.sub(r"\(\s*AC\s*\)", "(переменный ток)", k, flags=re.IGNORECASE)
    k2 = re.sub(r"\(\s*DC\s*\)", "(постоянный ток)", k2, flags=re.IGNORECASE)
    return k2

def _normalize_spec_keys_ru(final_dict):
    """D78: рекурсивно переименовывает латинские ключи specifications в русские (значения не трогаем)."""
    if not isinstance(final_dict, dict):
        return
    prod = final_dict.get("product")
    if not isinstance(prod, dict):
        return
    def walk(o):
        if isinstance(o, dict):
            return {_ru_spec_key(k): walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    if prod.get("specifications") is not None:
        prod["specifications"] = walk(prod["specifications"])


# Пост-фильтр цен в specifications (детерминированный, по образцу D78): критическая проверка №1
# промпта («цена только в price») нарушается LLM в ~1.4% карточек (столбцы «Цена, ₽», «Цена с НДС»
# у ВЕНТЛЭНД/Профиль — полный прогон gemma4 27.07). Промпт-правка отклонена по A/B (гиперприменение),
# гарантируем кодом. Регэксп строгий: «руб» только словом/с запятой — НЕ трогает «патрубок»/«трубопровод».
_PRICE_SPEC_KEY = re.compile(r"^\s*(цена|стоимость)\b|,\s*(₽|руб\.?\s*$|руб\b)", re.IGNORECASE)


def _strip_price_spec_keys(final_dict):
    """Удаляет из product.specifications ключи-цены (значения других ключей не трогаем)."""
    if not isinstance(final_dict, dict):
        return
    prod = final_dict.get("product")
    if not isinstance(prod, dict):
        return
    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()
                    if not (isinstance(k, str) and _PRICE_SPEC_KEY.search(k))}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    if prod.get("specifications") is not None:
        prod["specifications"] = walk(prod["specifications"])


class AITunnelClient:
    """Клиент для работы с AI Tunnel API с повторными попытками"""
    
    def __init__(self, api_url: str, api_key: str, rate_limiter: Optional[RateLimiter] = None, 
                 max_concurrent_requests: int = 40, performance_monitor: Optional[Any] = None,
                 model: str = "deepseek-v3.2", processing_tracker: Optional[Any] = None):
        self.processing_tracker = processing_tracker
        self.api_url = api_url.rstrip('/') + '/chat/completions'
        self.api_key = api_key
        self.model = model
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.rate_limiter = rate_limiter
        self.performance_monitor = performance_monitor
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        # Настройки повторных попыток
        self.max_retries = 5
        self.retry_status_codes = {500, 429, 502, 503}
        self.base_delay = 1  # начальная задержка в секундах
    
        # Инициализация токенайзера для подсчета токенов
        try:
            self.encoding = tiktoken.encoding_for_model("deepseek-v3.2")
        except:
            try:
                self.encoding = tiktoken.get_encoding("cl100k_base")
            except Exception as e:
                # get_encoding при пустом кэше СКАЧИВАЕТ BPE-файл с
                # openaipublic.blob.core.windows.net — без DNS/сети из контейнера это
                # роняло весь пайплайн на старте. Токенайзер используется только для
                # нарезки текста на чанки, поэтому хватает грубой оценки ~4 символа/токен.
                log.warning(f"tiktoken недоступен ({e}), используем грубую оценку токенов (~4 символа/токен)")
                self.encoding = None

    def count_tokens(self, text: str) -> int:
        """Подсчет токенов в тексте"""
        if self.encoding is None:
            return max(1, len(text) // 4)
        return len(self.encoding.encode(text))
    
    async def _make_request_with_retry(self, data: Dict[str, Any]) -> Tuple[Optional[Dict], Dict[str, int]]:
        """Выполнение запроса с rate limiting и повторными попытками"""
        
        for attempt in range(self.max_retries + 1):
            start_time = time.time()
            try:
                # Применяем семафор для ограничения параллельных запросов
                async with self.semaphore:
                    # Применяем rate limiting если настроен
                    if self.rate_limiter:
                        _ptt = self.processing_tracker
                        with (_ptt.exclude() if _ptt else nullcontext()):
                            await self.rate_limiter.acquire()
                    
                    # Таймаут поднят с дефолтных 300с: на страницах-гигантах gemma4 генерирует
                    # >300с; обрыв клиента оставляет «осиротевшую» генерацию, занимающую слот
                    # аккаунта Ollama (полный прогон 27.07: 39 стр. душили слоты ретраями).
                    timeout = aiohttp.ClientTimeout(total=900)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(self.api_url, headers=self.headers, json=data) as response:
                            end_time = time.time()

                            if response.status == 200:
                                result = await response.json()
                                usage = result.get('usage', {})
                                token_usage = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
                                
                                # Записываем метрики успешного запроса
                                if self.performance_monitor:
                                    await self.performance_monitor.record_request(
                                        start_time, end_time, True, token_usage, response.status
                                    )
                                
                                return result, {
                                    'input_tokens': usage.get('prompt_tokens', 0),
                                    'output_tokens': usage.get('completion_tokens', 0)
                                }
                            
                            # Обработка ошибок
                            end_time = time.time()
                            error_text = await response.text()
                            
                            # Записываем метрики неудачного запроса
                            if self.performance_monitor:
                                await self.performance_monitor.record_request(
                                    start_time, end_time, False, 0, response.status
                                )
                            
                            # Проверяем, нужно ли повторять запрос
                            if response.status in self.retry_status_codes and attempt < self.max_retries:
                                delay = self._calculate_exponential_delay(attempt)
                                
                                log.warning(
                                    f"Попытка {attempt + 1}/{self.max_retries} не удалась. "
                                    f"Статус: {response.status}. Повтор через {delay} сек. Ошибка: {error_text}"
                                )
                                
                                with (self.processing_tracker.retry_span('ai') if self.processing_tracker else nullcontext()):
                                    await asyncio.sleep(delay)
                                continue
                            
                            else:
                                log.error(f"Ошибка API после {attempt + 1} попыток: {response.status} - {error_text}")
                                return None, {'input_tokens': 0, 'output_tokens': 0}
                                
            except Exception as e:
                end_time = time.time()
                # Записываем метрики для исключений
                if self.performance_monitor:
                    await self.performance_monitor.record_request(
                        start_time, end_time, False, 0, 0
                    )
                
                if attempt < self.max_retries:
                    delay = self._calculate_exponential_delay(attempt)
                    log.warning(f"Ошибка, попытка {attempt + 1}. Повтор через {delay} сек: {e}")
                    with (self.processing_tracker.retry_span('ai') if self.processing_tracker else nullcontext()):
                        await asyncio.sleep(delay)
                    continue
                else:
                    log.error(f"Ошибка после {attempt + 1} попыток: {e}")
                    return None, {'input_tokens': 0, 'output_tokens': 0}
        
        return None, {'input_tokens': 0, 'output_tokens': 0}
    
    def _calculate_exponential_delay(self, attempt: int) -> float:
        """Расчет экспоненциальной задержки с добавлением случайности"""
        delay = self.base_delay * (2 ** attempt) + random.uniform(0, 1)
        return min(delay, 60)  # Максимальная задержка 60 секунд
    
    def _split_large_file_by_sentences(self, content: str, max_tokens: int = 40000) -> List[str]:
        """Разбивка большого файла на части по абзацам"""
        parts = []
        
        # Разбиваем на абзацы по двойным переносам строк
        paragraphs = content.split('\n\n')
        
        current_part = []
        current_tokens = 0
        
        for paragraph in paragraphs:
            # Добавляем обратно двойной перенос строки для сохранения структуры
            if current_part:
                paragraph_with_separator = '\n\n' + paragraph
                paragraph_tokens = self.count_tokens(paragraph_with_separator)
            else:
                paragraph_tokens = self.count_tokens(paragraph)
            
            # Если абзац уже превышает лимит, разбиваем по безопасным разделителям
            if paragraph_tokens > max_tokens:
                log.warning(f"Абзац слишком большой ({paragraph_tokens} токенов), разбиваем по безопасным разделителям")
                subparts = self._split_by_safe_delimiters(paragraph, max_tokens)
                for subpart in subparts:
                    subpart_tokens = self.count_tokens(subpart)
                    
                    # Проверяем, не превысит ли добавление подчасти лимит
                    if current_tokens + subpart_tokens > max_tokens and current_part:
                        parts.append('\n\n'.join(current_part))
                        current_part = [subpart]
                        current_tokens = subpart_tokens
                    else:
                        current_part.append(subpart)
                        current_tokens += subpart_tokens
                continue
            
            # Проверяем, не превысит ли добавление абзаца лимит
            if current_tokens + paragraph_tokens > max_tokens and current_part:
                parts.append('\n\n'.join(current_part))
                current_part = [paragraph]
                current_tokens = paragraph_tokens
            else:
                current_part.append(paragraph)
                current_tokens += paragraph_tokens
        
        # Добавляем последнюю часть, если она есть
        if current_part:
            parts.append('\n\n'.join(current_part))
        
        return parts
    
    def _split_by_safe_delimiters(self, text: str, max_tokens: int = 40000) -> List[str]:
        """Разбивка текста по безопасным разделителям"""
        # Безопасные разделители в порядке приоритета
        safe_delims = ['\n\n', '\n', '>', ' ', ',', ';']
        
        parts = []
        remaining_text = text
        
        while remaining_text:
            # Если оставшийся текст уже меньше лимита
            if self.count_tokens(remaining_text) <= max_tokens:
                parts.append(remaining_text)
                break
            
            # Находим позицию для разбивки
            split_pos = self._find_safe_split_position(remaining_text, max_tokens, safe_delims)
            
            if split_pos > 0:
                part = remaining_text[:split_pos]
                remaining_text = remaining_text[split_pos:]
                parts.append(part)
            else:
                # Если не нашли безопасного разделителя, вынужденно разбиваем по max_tokens
                log.warning("Не найден безопасный разделитель, вынужденное разбиение")
                approx_chars = int(max_tokens * 3.5)  # Примерное соотношение токены/символы
                if approx_chars < len(remaining_text):
                    parts.append(remaining_text[:approx_chars])
                    remaining_text = remaining_text[approx_chars:]
                else:
                    parts.append(remaining_text)
                    break
        
        return parts
    
    def _find_safe_split_position(self, text: str, max_tokens: int, safe_delims: List[str]) -> int:
        """Найти безопасную позицию для разбивки текста"""
        # Приблизительное количество символов для max_tokens (примерно 3.5 символа на токен)
        approx_chars = int(max_tokens * 3.5)
        
        # Начинаем поиск с приблизительной позиции
        start_pos = min(approx_chars, len(text) - 1)
        
        # Ищем безопасный разделитель, двигаясь назад от start_pos
        for lookback in range(start_pos, max(0, start_pos - 2000), -1):
            # Проверяем все безопасные разделители
            for delimiter in safe_delims:
                if delimiter and lookback >= len(delimiter):
                    # Проверяем, заканчивается ли текст на этом разделителе в текущей позиции
                    if text[lookback - len(delimiter):lookback] == delimiter:
                        # Убедимся, что после разделителя не начинается HTML-тег
                        if lookback < len(text) and text[lookback] != '<':
                            split_pos = lookback
                            # Проверяем токены до этой позиции
                            if self.count_tokens(text[:split_pos]) <= max_tokens:
                                return split_pos
        
        # Если не нашли безопасный разделитель, ищем вперед
        for lookahead in range(start_pos, min(len(text), start_pos + 2000)):
            for delimiter in safe_delims:
                if delimiter and lookahead + len(delimiter) <= len(text):
                    if text[lookahead:lookahead + len(delimiter)] == delimiter:
                        split_pos = lookahead + len(delimiter)
                        if self.count_tokens(text[:split_pos]) <= max_tokens:
                            return split_pos
        
        # Если не нашли, возвращаем 0 для вынужденного разбиения
        return 0
    
    def _chunk_text_by_tokens(self, text: str, max_tokens: int = 40000) -> List[str]:
        """Разбивка текста (Markdown) на чанки по границам файлов и безопасным разделителям"""
        chunks = []
        
        # Разбиваем по границам файлов
        file_parts = text.split('\n<!-- === Конец файла:')
        
        current_chunk = []
        current_chunk_tokens = 0
        
        for i, part in enumerate(file_parts):
            # Восстанавливаем маркер конца файла (кроме первой части)
            if i > 0:
                part = '<!-- === Конец файла:' + part
            
            part_tokens = self.count_tokens(part)
            
            if part_tokens > max_tokens:
                log.warning(f"Файл {i} слишком большой ({part_tokens} токенов), разбиваем на подчасти")
                
                # Разбиваем большой файл на подчасти
                subparts = self._split_large_file_by_sentences(part, max_tokens)
                
                for j, subpart in enumerate(subparts):
                    subpart_tokens = self.count_tokens(subpart)
                    
                    # Проверяем, не превысит ли добавление подчасти лимит
                    if current_chunk_tokens + subpart_tokens > max_tokens and current_chunk:
                        chunks.append('\n'.join(current_chunk))
                        current_chunk = [subpart]
                        current_chunk_tokens = subpart_tokens
                    else:
                        current_chunk.append(subpart)
                        current_chunk_tokens += subpart_tokens
                    
                    log.debug(f"  Подчасть {j+1}/{len(subparts)}: {subpart_tokens} токенов")
                
                continue

            # Если добавление файла превысит лимит, сохраняем текущий чанк и начинаем новый
            if current_chunk_tokens + part_tokens > max_tokens and current_chunk:
                chunks.append('\n'.join(current_chunk))
                current_chunk = [part]
                current_chunk_tokens = part_tokens
            else:
                current_chunk.append(part)
                current_chunk_tokens += part_tokens
        
        # Добавляем последний чанк
        if current_chunk:
            chunks.append('\n'.join(current_chunk))
        
        log.info(f"Markdown разбит на {len(chunks)} чанков по границам файлов")
        # Проверяем, что все чанки не превышают лимит
        for i, chunk in enumerate(chunks):
            chunk_tokens = self.count_tokens(chunk)
            if chunk_tokens > max_tokens:
                log.warning(f"Чанк {i} все еще превышает лимит ({chunk_tokens} токенов), "
                           f"применяем дополнительное разбиение")
                
                subparts = self._split_by_safe_delimiters(chunk, max_tokens)
                chunks[i:i+1] = subparts
        
        final_chunk_count = len(chunks)
        final_max_tokens = max([self.count_tokens(chunk) for chunk in chunks]) if chunks else 0
        
        log.info(f"Итоговое разбиение: {final_chunk_count} чанков, "
                f"максимальный размер: {final_max_tokens} токенов")
        
        return chunks

    def _clean_json_response(self, content: str) -> str:
        """
        Подготовка ответа модели к json.loads.
        Сначала пробуем распарсить ответ КАК ЕСТЬ: под response_format=json_object модель
        почти всегда возвращает валидный JSON, и «чинить» его НЕЛЬЗЯ. Прежние правки ломали
        корректные строки: replace('\\"','"') уничтожал экранированные кавычки
        (\\"PLAZAS\\" -> "PLAZAS"), а подстановка одинарных кавычек после двоеточия портила
        текст значений (reason: "...: '...'").
        Безопасные починки применяем ТОЛЬКО к реально невалидному ответу и только такие, что
        не меняют содержимое строк: вырезать JSON-объект из обёрток и убрать висячие запятые.
        """
        if not content:
            return content

        # Валидный JSON не трогаем (обычный случай)
        try:
            json.loads(content)
            return content
        except Exception:
            pass

        # Ответ невалиден — применяем только безопасные починки
        try:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            json_str = match.group(0) if match else content
            # Висячие запятые перед } и ]
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            return json_str
        except Exception as e:
            log.warning(f"Ошибка при очистке JSON: {e}")
            return content
    
    def _save_failed_response(self, company_id: str, company_dir: Optional[str], chunk_number: int, content: str):
        """Сохранение оригинального ответа модели в случае ошибки парсинга"""
        try:
            # Формируем путь для сохранения ошибок (фолбэк — общий каталог логов)
            if company_dir:
                error_dir = os.path.join(str(company_dir), "AI_text", "Distributor_info", "Error")
            else:
                error_dir = os.path.join("logs", "ai_failed_responses", str(company_id))
            
            # Создаем директорию, если она не существует
            os.makedirs(error_dir, exist_ok=True)
            
            # Формируем имя файла
            timestamp = int(time.time())
            filename = f"error_chunk_{chunk_number}_{timestamp}.json"
            filepath = os.path.join(error_dir, filename)
            
            # Сохраняем содержимое
            with open(filepath, 'w', encoding='utf-8') as f:
                # Сохраняем информацию об ошибке и оригинальный ответ
                error_info = {
                    "error": "JSONDecodeError",
                    "chunk_number": chunk_number,
                    "timestamp": timestamp,
                    "company_id": company_id,
                    "original_response": content,
                    "original_response_length": len(content)
                }
                json.dump(error_info, f, ensure_ascii=False, indent=2)
            
            log.info(f"Оригинальный ответ для чанка {chunk_number} сохранен в: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Ошибка при сохранении ответа: {e}")
            return None
            
    async def extract_distributor_info(self, text: str, company_id: str, manufacturer: str,
                                       company_dir: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
        """Извлечение информации о дистрибьюторах из Markdown-текста"""
        
        try:
            text_chunks = self._chunk_text_by_tokens(text, 40000)
            
            all_distributors_data = {"suppliers": []}
            total_input_tokens = 0
            total_output_tokens = 0
            successful_chunks = 0
            
            for i, chunk in enumerate(text_chunks, 1):
                # Добавляем задержку между чанками
                if i > 1:
                    await asyncio.sleep(1)
                
                chunk_tokens = self.count_tokens(chunk)
                log.info(f"Обработка чанка {i}/{len(text_chunks)} для дистрибьюторов, токенов: {chunk_tokens}")
                
                # Пропускаем слишком большие чанки
                if chunk_tokens > 70000:
                    log.warning(f"Чанк {i} слишком большой ({chunk_tokens} токенов), пропускаем")
                    continue
                prompt = f"""Analyze the text and extract information ONLY about russian dealers and distributors. Return a response in structured text format.
                            DO NOT include information about the manufacturing company in the list of suppliers. Manufacturing company: "{manufacturer}" 
                            Analyze this text carefully: (chunk {i+1}/{len(text_chunks)}):
                            {chunk}"""
                
                try:
                    data = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": """Role: You are a specialized AI assistant for accurately extracting structured information about dealers and suppliers from Markdown.
                                                            Task: Carefully analyze the provided Markdown and extract all information ONLY about dealers and suppliers.
                                                            The page may contain mixed information: information about the parent company and information about representative offices/suppliers/branches. 
                                                            It is necessary to classify this information correctly. 
                                                            Structure and display ONLY information about representative offices/suppliers/branches:
                                                            - The primary company address and the supplier address are ALWAYS DIFFERENT.
                                                            - If there are no suppliers, DO NOT ADD the primary company to the suppliers, return {"suppliers": []}.

                                                            **IMPORTANT: Your entire response MUST be a single, valid JSON object. Do NOT wrap it in Markdown code fences (```json ... ```). Do NOT add any text before or after the JSON. The JSON must be complete (not truncated).**

                                                            1. Data Selection Criteria
                                                            1.1 Mixed content: If the page contains mixed information (information about the parent company and information about representative offices/suppliers/branches), only information contained in the "Official Dealers," "Our Representative Offices," "Representative Office in ...," "Representative in ...," and "Branches" blocks should be classified as supplier information.
                                                            **Blocks such as "Склады и хранилища" (warehouses) should NOT be considered supplier information unless explicitly labeled as dealer/representative.**
                                                            1.2 **Do NOT include data about the parent company in the final response. The parent company name is provided in the user request for reference only and is not output.**
                                                            1.3 Geographical Filter: Only dealers and suppliers whose extracted information **contains a clear address within the Russian Federation (Russia)** are included in the output data.  
                                                                - **An address is considered Russian if it includes: "Россия", "РФ", "Russian Federation", or a name of a Russian region (e.g., "Московская область", "Республика Татарстан") or Russian city (e.g., "Москва", "Санкт-Петербург").**
                                                                - **If no address is present for a supplier, do NOT include that supplier, even if the phone code is Russian.**
                                                            1.4 Processing Missing Information: If any information from the required fields is missing from the Markdown, leave the corresponding field blank as an empty string ("").
                                                            1.5 Extraction Principle: Extract only information that is clearly presented in the text. Do not make assumptions, interpret, or generate data.
                                                            1.6 **Exclude personal employee contacts: if a phone number or email clearly belongs to a specific person (e.g., "Иванов Иван: +7 123 456 78 90"), do NOT include it.**
                                                            1.7 **Exclude INTERNAL structural units of the manufacturer (D48): sales departments, divisions and desks («отдел продаж», «дивизион», «департамент», «управление», «служба сбыта», «Экспорт», «Центр», «Восток», «Юг» and similar unit labels), even if they have their own phones/emails. A supplier MUST be a SEPARATE legal entity with its OWN company name different from the manufacturer's. Do NOT include a record if: (a) it has no own legal name and is labeled only as a unit/direction; (b) its name CONTAINS the manufacturer's base name — check this LITERALLY: for manufacturer «Салаватстекло, АО» the records «Салаватстекло Волга, АО» and «Салаватстекло Каспий, ООО» are its structural units/affiliates and MUST NOT be output as suppliers; (c) its address equals the manufacturer's headquarters address. Information about such units belongs to the COMPANY info (extract_company_info), not to suppliers.**

                                                            2. Output Data Requirements
                                                            2.1. Format:
                                                            Strictly adhere to the following text structure for each retrieved record.

                                                            Наименование:
                                                            - Full name of the company in the format: legal name, organizational and legal form 
                                                            Example: Московский насосный завод №1, ЗАО
                                                                    Еврохим-Пенза, ООО
                                                                    Завод Водоприбор, ОАО
                                                                    Завод Знамя труда, АО
                                                                    ПКФ Тепло, ООО
                                                                    Дантекс рус, ООО
                                                                    ЭТМ, АО

                                                            - If there is no organizational and legal form, don't invent one.

                                                            - If this is a foreign company, then after the form of ownership, you must indicate the region separated by a comma (Ukraine and Belarus are also considered foreign manufacturers).
                                                            Example: Керамин, ОАО, Беларусь
                                                                    Ceramica La Escandella, Испания
                                                                    M.H. KOREA LTD, Корея
                                                            (Note: The geographic filter for suppliers requires a Russian address. Foreign companies will be excluded by that filter. This rule is kept for completeness.)

                                                            - If the name of the organization contains such concepts as:
                                                                • Группа Компаний 
                                                                • Производственный Комплекс
                                                                • Торговый Дом 
                                                                • Научно Производственное Предприятие 
                                                            They are added as abbreviations to the legal name.  
                                                            Example:  Raw text: ООО «Группа Компаний Демидов» 
                                                                        MUST be: Демидов ГК, ООО
                                                                        Raw text: ООО «ПК ДОНТЭС» 
                                                                        MUST be: Донтэс ПК, ООО
                                                                        Raw text: Общество с ограниченной ответственностью «ТД «Евротрейдинг» 
                                                                        MUST be: Евротрейдинг ТД, ООО
                                                                        Raw text: ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ НАУЧНО-ПРОИЗВОДСТВЕННОЕ ПРЕДПРИЯТИЕ "ЗАВОД СТЕКЛОПЛАСТИКОВЫХ ТРУБ" 
                                                                        MUST be: Завод стеклопластиковых труб НПП, ООО                  

                                                            Регион: **Extract the name of the Russian region (subject of the federation) from the address.**
                                                                    **Examples: "Московская область", "г. Москва", "Краснодарский край", "Республика Башкортостан".**
                                                                    **If the region is not specified in the address, leave empty string ("").**

                                                            Адрес: Address format: Postcode (if any), Country, Region (if any), City, Street, Building, apartment, office, etc. Take information only from the incoming text, don't make anything up.
                                                                If the postal code is not specified, write the address in the following format: Country, Region (if any), City, Street, Building, apartment, office, etc.

                                                            Телефон: **Apply the following normalization algorithm:**
                                                                    1. Keep only digits and the '+' character. Remove all spaces, dashes, parentheses, and other symbols.
                                                                    2. If the number starts with '8', replace that '8' with '+7'.
                                                                    3. If the number has no country code and is 10–11 digits long, add '+7' at the beginning.
                                                                    4. Format as: `+7` space `<area/operator code>` space `<subscriber number>`.
                                                                    5. Split the subscriber number into groups of 2–3 digits, separated by single spaces.
                                                                    6. Example: 8 (42622) 71-00-5 → +7 42622 710 05
                                                                    7. Example: +7 812 6770791 → +7 812 677 07 91
                                                                    8. Example: (495) 961-01-60 → +7 495 961 01 60
                                                                    9. Example: 8 800 775 17 71 → +7 800 775 17 71
                                                                    **If a supplier has multiple phone numbers, combine them into a single string separated by commas: "Номер1, Номер2".**

                                                            E-mail: **If multiple emails, combine with commas. If none, empty string.**

                                                            URL страницы: **Extract the full URL from the Markdown if explicitly present (e.g., from a link like [site](http://example.com) or plain text http://...).**
                                                                        **Do NOT attempt to combine base domain and relative path. If no URL is present, leave empty string ("").**

                                                            **Deduplication: If the same supplier appears multiple times (identical name and address), include only one record. Compare normalized names and addresses.**

                                                            // Repeat block for each subsequent record
                                                            Return the answer ONLY in the form of valid JSON without any explanations or additional characters.

                                                            **Example of correct output (single supplier):**
                                                            {
                                                            "suppliers": [
                                                                {
                                                                "Наименование": "ЭТМ, АО",
                                                                "Регион": "г. Москва",
                                                                "Адрес": "123456, Россия, г. Москва, ул. Тверская, д. 1",
                                                                "Телефон": "+7 495 123 45 67",
                                                                "E-mail": "info@etm.ru",
                                                                "URL страницы": "https://example.com/dealers"
                                                                }
                                                            ]
                                                            }

                                                            **Example with multiple suppliers:**
                                                            {
                                                            "suppliers": [
                                                                {
                                                                "Наименование": "Дантекс рус, ООО",
                                                                "Регион": "Московская область",
                                                                "Адрес": "Россия, Московская область, г. Подольск, ул. Ленина, 10",
                                                                "Телефон": "+7 495 987 65 43",
                                                                "E-mail": "sales@dantex.ru",
                                                                "URL страницы": ""
                                                                },
                                                                {
                                                                "Наименование": "ПКФ Тепло, ООО",
                                                                "Регион": "Краснодарский край",
                                                                "Адрес": "350000, Россия, Краснодарский край, г. Краснодар, ул. Красная, 5",
                                                                "Телефон": "+7 861 234 56 78",
                                                                "E-mail": "",
                                                                "URL страницы": "https://example.com/teplo"
                                                                }
                                                            ]
                                                            }

                                                            2.2. Checking:
                                                            Be careful with your geographic filter: Make sure all retrieved records are actually from Russia, and ignore records that don't meet this criterion.
                                                            **If a supplier has a Russian address but the region field is ambiguous, leave "Регион" empty and still include the supplier.**

                                                            2.3. Action in case of missing data:
                                                            If no suitable records are found in the Markdown that satisfy the geographic filter, return the response {"suppliers": []}.

                                                            **FORBIDDEN:**
                                                            - Do not wrap the JSON in Markdown code blocks (```json ... ```).
                                                            - Do not add any extra text, explanations, or comments before or after the JSON.
                                                            - Do not abbreviate or shorten any extracted values.
                                                            - Do not invent data that is not present in the input.
                                                            - Do not include the parent company or any entity without a Russian address.
                                """},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.2,
                        "reasoning_effort": _effort_for_model(self.model),
                        "timeout": 120,
                        "response_format": {"type": "json_object"}
                    }
                
                    result, token_usage = await self._make_request_with_retry(data)
                    
                    if result:
                        content = result['choices'][0]['message']['content']
                        try:
                            # Устойчивый разбор (gemini подмешивает <think> в content)
                            chunk_data = _robust_json_extract(content)
                            if chunk_data is None:
                                raise json.JSONDecodeError("robust parse failed", content or "", 0)
                            if isinstance(chunk_data, dict):
                                # Проверяем наличие suppliers в ответе
                                if 'suppliers' in chunk_data:
                                    all_distributors_data['suppliers'].extend(chunk_data['suppliers'])
                                    successful_chunks += 1
                                    log.info(f"Чанк {i} успешно обработан, найдено {len(chunk_data['suppliers'])} поставщиков")
                                else:
                                    # Если нет suppliers, создаем структуру с ошибкой
                                    chunk_data = {"error": "No suppliers found", "chunk_number": i}
                                    all_distributors_data['suppliers'].append(chunk_data)
                                    log.warning(f"Чанк {i}: не найдены поставщики")
                        except json.JSONDecodeError as e:
                            saved_filepath = self._save_failed_response(company_id, company_dir, i, content)
                            log.error(f"Ошибка парсинга JSON в чанке {i}: {e}")
                            chunk_data = {"error": f"JSONDecodeError: {str(e)}", "raw_content": content[:500], "chunk_number": i}
                            all_distributors_data['suppliers'].append(chunk_data)
                        
                        total_input_tokens += token_usage['input_tokens']
                        total_output_tokens += token_usage['output_tokens']
                    else:
                        log.error(f"Ошибка обработки чанка {i} для дистрибьюторов")
                        all_distributors_data['suppliers'].append({"error": "API request failed", "chunk_number": i})
                        
                except Exception as e:
                    log.error(f"Ошибка извлечения информации о дистрибьюторах (чанк {i}): {e}")
                    all_distributors_data['suppliers'].append({"error": f"Processing error: {str(e)}", "chunk_number": i})
                    continue
            
            log.info(f"Успешно обработано {successful_chunks}/{len(text_chunks)} чанков дистрибьюторов")
            
            return all_distributors_data, {'input_tokens': total_input_tokens, 'output_tokens': total_output_tokens}
                
        except Exception as e:
            log.error(f"Критическая ошибка в extract_distributor_info: {e}")
            return {"error": f"Critical error: {str(e)}"}, {'input_tokens': 0, 'output_tokens': 0}

    def _reasoning_user_prompt(self, text: str, manufacturer: str, base_domain: str) -> str:
        """Пользовательский промпт SGR-REASONER (правила классификации и чтения Markdown).
        Вынесен в метод, чтобы один и тот же текст правил использовался и в 2-вызовном пути
        (_sgr_reasoning), и в едином single-call (_sgr_single) — verbatim, без дублирования."""
        return f"""Проанализируй Markdown и верни СТРОГО ОДИН JSON-объект. Запиши результат
    каждого шага ПОД ТОЧНО УКАЗАННЫМ ИМЕНЕМ КЛЮЧА (имена ключей менять/уплощать ЗАПРЕЩЕНО).

    Manufacturer (только для справки): {manufacturer}
    Base domain: {base_domain}
    Текст: {text[:100000]}

    "step_1_page_classification" (объект): классификация страницы. ТОЧНО посчитай технические
    характеристики и реши, продуктовая ли страница.
    ТЕХНИЧЕСКАЯ ХАРАКТЕРИСТИКА — это ИМЕНОВАННЫЙ параметр товара, значение которого ОБЯЗАТЕЛЬНО
    содержит ЧИСЛО (единица измерения — опционально): плотность 1300 кг/м^3, прочность М-100,
    морозостойкость F-100, толщина стенки 40 мм, габаритные размеры 390×90×188 мм, масса 9 кг,
    количество в 1 м^2 13 шт. Параметр БЕЗ числа (например «цвет белый», «материал — сталь»,
    «теплопроводность» без значения) характеристикой НЕ считается — параметрический поиск в системе
    завязан именно на ЧИСЛОВЫЕ значения характеристик.
    ПРАВИЛА ПОДСЧЁТА tech_params_count (соблюдай СТРОГО):
    - считай ТОЛЬКО характеристики с ЧИСЛОВЫМ значением; параметр без числа НЕ учитывается;
    - считай КАЖДЫЙ РАЗЛИЧНЫЙ параметр РОВНО ОДИН РАЗ, даже если он повторяется в названии, описании,
      таблице и списке одновременно;
    - габаритные размеры вида AAA×BBB×CCC считаются ПО ЧИСЛУ ОСЕЙ — каждое число отдельной характеристикой:
      390×90×188 мм = 3, 12×12×123 = 3, 60×60×1,5 = 3; двухосевые 60×60 = 2; одиночный размер 100 мм = 1;
    - УЧИТЫВАЙ характеристику, даже если она указана не отдельной строкой, а в НАЗВАНИИ товара, в ИМЕНАХ
      ВАРИАНТОВ или в ЗАГОЛОВКАХ СТОЛБЦОВ таблицы вариантов. Например, в вариантах «Профиль Z100 (1,0 - Zn) /
      (1,2 - Zn)» числа 1,0/1,2 — это ТОЛЩИНА (характеристика); столбцы «Масса, кг» и «Длина, мм» — это масса и
      длина (по одной характеристике на столбец);
    - СОСТАВНОЕ ОБОЗНАЧЕНИЕ С РАСШИФРОВКОЙ (D60): если дано условное обозначение изделия (напр.
      «35х30х0,1- I 20х13»), а рядом его ЛЕГЕНДА «где N — <название параметра>» (35 — наружный диаметр, мм;
      30 — количество пар мембран, шт; 0,1 — толщина стенки, мм; I — тип 1, внутренний диаметр 22 мм), то
      КАЖДЫЙ расшифрованный числовой параметр считается ОТДЕЛЬНОЙ характеристикой (здесь: наружный диаметр,
      количество пар мембран, толщина стенки, внутренний диаметр = 4). Обозначение целиком за одну
      характеристику НЕ считай и как «код» не отбрасывай;
    - НЕ считай характеристиками: цену, артикул, цвет/RAL, количество для заказа, скидки/акции, телефоны,
      адреса, кнопки, ссылки и названия разделов; количество товара в упаковке/на поддоне — это логистика, НЕ характеристика.
    КЛАССИФИКАЦИЯ строго по числу (без исключений):
    - tech_params_count >= 3  → is_product_page=true,  has_enough_params=true;
    - tech_params_count <  3  → is_product_page=false, has_enough_params=false (страница НЕ продуктовая).
    НИКОГДА не повышай класс «при сомнении» или «чтобы не потерять товар». Если различных числовых характеристик
    меньше трёх — is_product_page=false. Всегда соблюдай тождество is_product_page = (tech_params_count >= 3).
    ИСКЛЮЧЕНИЕ (типоразмерный ряд — это точное правило, а НЕ «при сомнении»): если на странице есть H1-НАЗВАНИЕ
    КОНКРЕТНОГО ИЗДЕЛИЯ и таблица ТИПОРАЗМЕРОВ этого изделия — несколько строк, где каждая строка есть размер/модель
    ОДНОГО И ТОГО ЖЕ товара с числовыми значениями (диаметр, сечение, длина, толщина стенки и т.п.), — то страница
    ТОВАРНАЯ: установи is_product_page=true, has_enough_params=true и tech_params_count не меньше 3, ДАЖЕ ЕСЛИ
    различных НАЗВАНИЙ числовых характеристик всего два (например столбцы «Диаметр, мм» и «Толщина стали, мм»).
    Такой размерный ряд — полноценные данные о товаре, терять его ЗАПРЕЩЕНО. Исключение НЕ распространяется на
    страницы-КАТЕГОРИИ (перечень ссылок на разные товары без единого H1-изделия и без таблицы типоразмеров одного
    изделия) — они остаются непродуктовыми.
    ТАБЛИЦА ИСПОЛНЕНИЙ/КОНФИГУРАЦИЙ (D82, точное правило, приоритетнее гейта ≥3, НЕ «при сомнении»): если есть
    H1-НАЗВАНИЕ КОНКРЕТНОГО ИЗДЕЛИЯ и таблица под заголовком «Исполнение»/«Исполнение и цена»/«Модификации», где
    КАЖДАЯ СТРОКА — отдельное исполнение (модификация/типоразмер) ОДНОГО И ТОГО ЖЕ изделия, а СТОЛБЦЫ — его
    параметры (диаметр условного прохода/Ду, монтажная длина, метрологический класс, место установки в трубопровод,
    интерфейс, монтажный комплект, максимальный расход Qmax и т.п.), — это ТИПОРАЗМЕРНЫЙ РЯД: установи
    is_product_page=true, has_enough_params=true и tech_params_count не меньше 3, ДАЖЕ ЕСЛИ различных ЧИСЛОВЫХ
    столбцов меньше трёх (напр. числовой только «Диаметр условного прохода»). Перечень исполнений с их параметрами —
    полноценные данные о товаре, терять его ЗАПРЕЩЕНО. Правило действует, ТОЛЬКО когда строки суть разные исполнения
    ОДНОГО базового изделия (общий H1), и НЕ распространяется на страницы-КАТЕГОРИИ (перечень ссылок на разные
    изделия) — они остаются непродуктовыми.
    КОМПЛЕКТАЦИОННО-РАЗМЕРНАЯ ТАБЛИЦА ИЗДЕЛИЯ (D70, точное правило, НЕ «при сомнении»): если есть
    H1-НАЗВАНИЕ КОНКРЕТНОГО ИЗДЕЛИЯ и таблица его КОМПЛЕКТАЦИИ/СОСТАВА, где строки — составные части
    ОДНОГО изделия, а в ячейках даны МАТЕРИАЛ и ГАБАРИТНЫЙ РАЗМЕР в мм (AAAхBBBхCCC, в т.ч. с
    кириллической «х»: 2100х74х30) со штуками/погонными метрами (шт, п.м), — габаритные размеры из
    этой таблицы СЧИТАЙ характеристиками ПО ЧИСЛУ ОСЕЙ (2100х74х30 = 3), а страницу ТОВАРНОЙ
    (is_product_page=true, has_enough_params=true, tech_params_count>=3); терять такой погонаж/комплект
    ЗАПРЕЩЕНО. Правило НЕ действует на НЕДВИЖИМОСТЬ/ЗАСТРОЙКУ (H1 — ЖК/микрорайон/жилой квартал/посёлок;
    показатели «площадь застройки»/«общая площадь квартир»/«количество квартир»/«машино-мест» в кв.м/Га —
    это НЕ товар) и уступает правилам D51/D55/D56 (каталоги/группы/городские SEO-посадки).
    ГОРОДСКАЯ SEO-ПОСАДКА (D55, приоритетнее исключения о типоразмерном ряде): страница, чей <title>/H1
    построен как «Купить <категорию> С ДОСТАВКОЙ В <Город>» («Купить блочки с доставкой в Городец»,
    «…с доставкой в Арзамас») — региональный ДУБЛЬ каталожной страницы: is_product_page=false
    (Trash_418#), даже если на ней есть таблица товаров с размерами и ценами. Товары собираются с их
    собственных страниц. НЕ путать с настоящей товарной страницей, где «купить в <Городе>» — часть
    маркетинговой фразы заголовка КОНКРЕТНОГО изделия («Фундаментный блок ФБС 9.3.6-Т купить в Нижнем
    Новгороде, цена») — там есть имя конкретного изделия и БЕЗ оборота «с доставкой в».
    КАТАЛОЖНАЯ СВОДКА СЕМЕЙСТВ (D56, Ареопаг): страница из блока/блоков-«визиток» семейств изделий —
    название семейства + картинка + несколько СВОДНЫХ параметров-диапазонов («Подача от 0,4 л/ч»,
    «Подача до 7600 л/ч», «Мощность 0,25-7,5 кВт») + ссылка на собственную страницу/подкаталог семейства
    («Подробнее», [название](/pumps/...)), БЕЗ развёрнутых разделов самого изделия (нет описания
    назначения/конструкции/таблиц конкретных модификаций) — это КАТАЛОГ: is_product_page=false
    (Trash_418#). НЕ путать со страницей товара-семейства: у неё, кроме сводных параметров, есть
    РАЗВЁРНУТОЕ описание изделия (назначение, конструкция, разделы, таблицы, модельный ряд) — она товар.
    КОНТР-ПРИЗНАК СТРАНИЦЫ-ГРУППЫ (D51, приоритетнее исключения о типоразмерном ряде): если модели/типы на
    странице представлены ССЫЛКАМИ на ИХ СОБСТВЕННЫЕ отдельные страницы (markdown-ссылки вида
    [РКФ-3/1-М1](/product/...), [тип 1](...), карточки-плитки с ссылками) — у каждого типа ЕСТЬ своя товарная
    страница, и текущая страница является КАТАЛОЖНОЙ ГРУППОЙ: is_product_page=false (Trash_418#), даже если на
    ней есть сводная таблица характеристик типов. Карточки создаются по отдельным страницам типов, а карточка
    «на группу» — дубль. Исключение о типоразмерном ряде применяй ТОЛЬКО когда типоразмеры даны таблицей/списком
    БЕЗ ссылок на собственные страницы этих типов.
    Поля: {{"is_product_page": true|false, "tech_params_count": <целое число>, "has_enough_params": true|false, "reason": "<перечисли посчитанные характеристики>"}}.
    ВАЖНО: is_product_page и has_enough_params — БУЛЕВЫ значения true/false (НЕ строки "true"/"false").

    "step_3_extract_raw_blocks" (объект): сырые блоки.
    {{"raw_h1": "<главный заголовок # H1>",
      "raw_sections": [{{"heading": "...", "content": "..."}}, ...] — все значимые секции (## , ### и блоки без заголовка),
      "raw_tables": [{{"headers": ["col1", "col2"], "rows": [["val1", "val2"], ...]}}, ...] — ВСЕ таблицы Markdown,
      "raw_lists": [["элемент", ...], ...] — все маркированные/нумерованные списки без маркеров}}.

    "step_5_section_routing" (массив): для каждой значимой секции —
    {{"source_heading": "...", "semantic_route": "<одно из: description, applications, advantages, instructions_manuals, exploitation, storage, security_measures, complectation, compatibility, dimensions, weight, specifications, additional_info>", "routed_text": "<текст секции как есть>"}}.
    Область применения → applications; преимущества → advantages; комплектация/упаковка → complectation;
    совместимость → compatibility; хранение → storage; эксплуатация → exploitation; меры безопасности → security_measures.
    Не уверен → additional_info.
    СРОК СЛУЖБЫ, срок/гарантийный срок ГАРАНТИИ и обозначения нормативов (ГОСТ …, ГОСТ Р …, ТУ …) —
    ТЕРЯТЬ ЗАПРЕЩЕНО (D15), даже если они даны ОТДЕЛЬНЫМИ СТРОКАМИ после таблицы характеристик без
    собственного заголовка: маршрутизируй их в exploitation, сохраняя обозначение норматива дословно.
    routed_text ВСЕГДА переноси ЦЕЛИКОМ — ВСЕ абзацы секции дословно; усечение секции до первого
    абзаца, её пересказ или сжатие ЗАПРЕЩЕНЫ (D15).
    Устройство/конструкция/принцип работы товара + материалы деталей (корпус, вал, рабочее колесо,
    подшипники, уплотнение; серый чугун, углеродистая/нержавеющая сталь) → description (это описание
    изделия). Разделы «Конструкция и устройство», «Устройство и принцип работы», «Принцип работы»,
    «Конструкция» ТЕРЯТЬ ЗАПРЕЩЕНО и НЕЛЬЗЯ класть в additional_info (D61).
    ОБЯЗАТЕЛЬНО обработай ВСЕ секции ДО САМОГО КОНЦА документа (D44 — частая ошибка):
    разделы в хвосте страницы — «Меры безопасности», «Способы нанесения» (безвоздушное/воздушное/кисть),
    «Сушка»/«Время высыхания» (в т.ч. таблицы-матрицы по температурам), «Хранение» — ТЕРЯТЬ ЗАПРЕЩЕНО.
    Каждая значимая секция страницы обязана попасть либо в step_5_section_routing, либо (таблицы) в step_6;
    таблицы режимов нанесения/сушки клади в step_6 как spec_tables.

    "step_6_specifications_processing" (объект): технические характеристики.
    {{"spec_items": [{{"key": "...", "value": "..."}}, ...], "spec_tables": [{{"object_key": "...", "object_value": {{"Параметр": "значение", ...}}}}, ...]}}.
    НЕ нормализуй единицы и НЕ переводи: ключи (key/object_key) и заголовки столбцов копируй ДОСЛОВНО как в
    Markdown, на языке оригинала (напр. «Высота, h» оставляй «Высота, h», а НЕ «High, h»).
    Таблицу, где каждая строка — отдельное обозначение/модель (D400, D500, EI30 и т.п.),
    разложи по spec_tables (строка → объект). Таблицу с одной строкой, где первый столбец не идентификатор модели,
    добавь в spec_items плоскими парами.
    ПРАВИЛО «ЗНАЧЕНИЕ ЯЧЕЙКИ — ЦЕЛИКОМ» (D50, частая ошибка): если в ячейке таблицы ПЕРЕЧЕНЬ значений
    («400; 630», «400(630)», «0,55; 0,7; 1,0») — переноси ВЕСЬ перечень ДОСЛОВНО. Сокращать перечень до
    одного значения (например, только до модели, которой посвящена страница) ЗАПРЕЩЕНО: страницы серий
    описывают несколько исполнений, и урезание создаёт расхождение между карточками одной серии.
    ПРАВИЛО «НЕ ТЕРЯЙ НИ ОДНОГО СТОЛБЦА» (обязательно, частая ошибка): если в таблице НЕСКОЛЬКО строк и
    ТРИ ИЛИ БОЛЕЕ содержательных столбца (пустые столбцы-разделители не в счёт) — например
    «Параметр | Варианты | Стандарт» или «Диаметр | Толщина | Площадь» — раскладывай её ТОЛЬКО в spec_tables:
    object_key = значение первого содержательного столбца этой строки (имя параметра/модели/размер),
    object_value = словарь ВСЕХ прочих столбцов {{«<заголовок столбца>»: «<значение из этой строки>», ...}}.
    НЕ клади такую таблицу в spec_items плоскими парами: там помещается лишь ОДНО значение, и лишние
    столбцы-значения (напр. «Стандарт», «по умолчанию», второй/третий столбец) будут ПОТЕРЯНЫ.
    ПРАВИЛО «РАЗМЕРНАЯ ТАБЛИЦА — ПОСТРОЧНО И ПОЛНОСТЬЮ» (D53, Р-ВЕНТ): таблицу размерного ряда
    (строки = конкретные размеры/диаметры изделия) переноси в spec_tables ПОСТРОЧНО и ЦЕЛИКОМ:
    object_key = значение идентифицирующего столбца, а если его нет — составной размер из первых
    столбцов («100×100 мм», «Диаметр 100 мм»); object_value = остальные столбцы строки.
    СТРОГО ЗАПРЕЩЕНЫ три искажения:
    (а) АГРЕГАЦИЯ в диапазоны: сжимать 58 строк конкретных размеров в «Боковая сторона: 100-2000,
        Площадь: 0,2-13,72» — подмена данных, замена значений недопустима;
    (б) ВЫНОС отдельных строк: одна строка таблицы (напр. 400×800) не должна уезжать в плоские
        spec_items, когда остальные строки лежат объектами — ВСЕ строки в одной общей структуре
        с одинаковым набором ключей;
    (в) ТРАНСПОНИРОВАНИЕ в перечни: превращать таблицу 14 строк × 3 столбца в три плоских ключа
        «Диаметр: 100, 125, …», «Длина: 125, 125, …» — сопоставление значений по строкам теряется.
    Каждый содержательный столбец таблицы ОБЯЗАН попасть в object_value. ИСКЛЮЧЕНИЕ: столбец, ПУСТОЙ во
    ВСЕХ строках данных (обычно подпись убранного конвертером изображения/чертежа: «Чертеж…», «Схема…»,
    «Рисунок…», «Фото…»), содержательным НЕ является — пропусти его, НЕ включай в object_value. Частично
    пустой столбец (значение есть хотя бы у одной строки) сохраняй у ВСЕХ строк как есть.

    "step_7_collect_images" (массив): полные URL изображений продукта из Markdown (![alt](url)).

    "step_8_collect_variants" (массив): ТОЛЬКО потребительские варианты выбора (цвет, размер, конфигурация).
    НЕ помещай сюда технические таблицы обозначений/спецификаций — они идут ТОЛЬКО в step_6_specifications_processing.
    Если потребительских вариантов нет → [].

    ПРОТИВ ДУБЛИРОВАНИЯ (важно для стабильности и экономии токенов): размещай каждый крупный блок ОДИН раз.
    Крупные таблицы — в raw_tables (и, если это спецификации, в spec_tables); в routed_text для секции-таблицы
    НЕ повторяй её содержимое целиком — дай краткую ссылку (например, "см. таблицу в raw_tables").

    ВЫВОДИ ТОЛЬКО JSON c ключами: step_1_page_classification, step_3_extract_raw_blocks, step_5_section_routing,
    step_6_specifications_processing, step_7_collect_images, step_8_collect_variants. НИКАКОГО ТЕКСТА ВНЕ JSON.
    """

    def _reasoning_system_prompt(self) -> str:
        """Системный промпт SGR-REASONER. Используется и в 2-вызовном пути, и в single-call."""
        return """Ты — SGR-REASONER. Извлекаешь сырые данные из Markdown и делаешь базовую классификацию, без сложного форматирования.
    Возвращай СТРОГО ОДИН JSON-объект, используя ТОЧНО эти ключи верхнего уровня: step_1_page_classification, step_3_extract_raw_blocks, step_5_section_routing, step_6_specifications_processing, step_7_collect_images, step_8_collect_variants. НЕ переименовывай и НЕ уплощай ключи.
    is_product_page и has_enough_params — БУЛЕВЫ значения true/false, не строки.
    Ключевое правило классификации: посчитай РАЗЛИЧНЫЕ технические характеристики, значение которых содержит ЧИСЛО
    (параметр без числа НЕ считается; габаритные размеры AAA×BBB×CCC — ПО ЧИСЛУ ОСЕЙ, напр. 12×12×123 = 3;
    каждый параметр считается один раз). Если их 3 и более →
    is_product_page=true, has_enough_params=true; если меньше 3 → is_product_page=false, has_enough_params=false.
    Решай СТРОГО по числу: НЕ повышай класс «при сомнении» и НЕ включай страницу «чтобы не потерять товар».
    Соблюдай тождество is_product_page = (tech_params_count >= 3).
    ИСКЛЮЧЕНИЕ: страница с H1-названием конкретного изделия и таблицей его ТИПОРАЗМЕРОВ (несколько строк-размеров
    одного товара с числами: диаметр/сечение/длина/толщина) — ТОВАРНАЯ (is_product_page=true, tech_params_count>=3),
    даже если различных названий характеристик всего два; это НЕ относится к страницам-категориям (список ссылок без
    таблицы размеров одного изделия).
    Не добавляй и не изменяй значения. Не форматируй единицы измерения. Просто извлеки как есть.
    СОХРАНЯЙ ЯЗЫК ОРИГИНАЛА: имена характеристик (ключи), заголовки столбцов таблиц и ЛЮБОЙ извлечённый текст
    копируй ДОСЛОВНО как в Markdown. НИКОГДА не переводи на другой язык: «Высота» оставляй «Высота» (НЕ «High»),
    «Ширина» — «Ширина» (НЕ «Width»), «Длина» — «Длина» (НЕ «Length»), «Вес»/«Масса» — как в источнике.
    Игнорируй блоки "Похожие товары", "Рекомендуем", "С этим покупают" — они не являются частью продукта."""

    async def _sgr_reasoning(self, text: str, manufacturer: str, base_domain: str) -> Tuple[Optional[Dict], Dict[str, int], bool]:
        """Первый промпт: извлечение сырых данных и классификация из Markdown."""
        prompt = self._reasoning_user_prompt(text, manufacturer, base_domain)
        system = self._reasoning_system_prompt()
        # Температуры попыток: 0.2 -> 0.1 -> 0.7. Третья, «горячая», ломает
        # дегенеративное зацикливание reasoning-модели (пустой content/повторы):
        # низкая температура луп воспроизводит, повышение сбивает его.
        _attempt_temps = (0.2, 0.1, 0.7)
        for attempt, _temp in enumerate(_attempt_temps):
            data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "temperature": _temp,
                "timeout": 80,
                "response_format": {"type": "json_object"}
            }
            data.update(_payload_tuning(self.model))
            result, token_usage = await self._make_request_with_retry(data)
            if not result:
                continue
            content = result['choices'][0]['message']['content']
            try:
                if _tuning_for(self.model)["robust"]:
                    reasoning_dict = _robust_json_extract(content)
                    if reasoning_dict is None:
                        raise ValueError("robust parse failed")
                else:
                    cleaned = self._clean_json_response(content)
                    try:
                        reasoning_dict = json.loads(cleaned)
                    except Exception:
                        # D35: локальный ремонт (невалидные \escape, ограждения, <think>-шум)
                        # прежде чем засчитывать попытку неудачной
                        reasoning_dict = _robust_json_extract(content)
                        if reasoning_dict is None:
                            raise
                # Базовая валидация наличия обязательных полей
                if not isinstance(reasoning_dict, dict):
                    raise ValueError("Response is not a dict")
                # Нормализуем «дрейфующие»/уплощённые ключи к канонической схеме ReasoningOutput.
                # Это спасает ответы, где модель переименовала ключи, и устраняет ложный Trash_418#.
                reasoning_dict = _normalize_reasoning_keys(reasoning_dict)
                # Подстраховка: если классификации нет даже после нормализации — дефолт
                if "step_1_page_classification" not in reasoning_dict:
                    log.warning("Missing step_1_page_classification, treating as non-product")
                    reasoning_dict["step_1_page_classification"] = {
                        "is_product_page": False,
                        "reason": "Missing classification from model",
                        "tech_params_count": 0,
                        "has_enough_params": False
                    }
                # Дополнительная валидация через Pydantic (опционально, можно раскомментировать)
                # ReasoningOutput(**reasoning_dict)
                return reasoning_dict, token_usage, True
            except Exception as e:
                log.error(f"SGR reasoning validation error (attempt {attempt+1}): {e}")
                self._save_sgr_error_response("reasoning", content, attempt)
                if attempt < len(_attempt_temps) - 1:
                    continue
                else:
                    return None, token_usage, False
        return None, token_usage, False

    def _formatter_system_prompt(self) -> str:
        """Системный промпт SGR-FORMATTER (правила форматирования и схема финального JSON).
        Вынесен в метод, чтобы один и тот же текст правил использовался и в 2-вызовном пути
        (_sgr_formatter), и в едином single-call (_sgr_single) — verbatim, без дублирования."""
        return """Ты — SGR-FORMATTER. Твоя задача — преобразовать структурированные сырые данные (результат работы SGR-REASONER) в финальный JSON продукта, строго следуя правилам ниже.

    Входные данные содержат:
    - step_1_page_classification (is_product_page, reason, tech_params_count, has_enough_params) — справочно; в форматтере НЕ обрабатывается (классификация страницы — на этапе reasoner)
    - step_2_error_routing — справочно; в форматтере НЕ обрабатывается
    - step_3_extract_raw_blocks (raw_h1, raw_sections, raw_tables, raw_lists)
    - step_4_name_normalization (может содержать только final_product_name=raw_h1; готового разбора noun/adjectives/brand/gost НЕТ — product_name формируешь сам из raw_h1 по правилам ниже)
    - step_5_section_routing (массив {source_heading, semantic_route, routed_text})
    - step_6_specifications_processing (spec_items: [{key, value}], spec_tables: [{object_key, object_value}])
    - step_7_collect_images (список URL)
    - step_8_collect_variants (массив объектов вариантов)

    Ты должен:
    1. Сформировать product_name по шаблону, используя raw_h1 и правила нормализации (см. ниже).
    2. Извлечь ТУ (tu), цену (price), артикул (article) из текста (raw_sections или спецификаций).
    3. Обработать спецификации:
    - Преобразовать spec_items в плоский словарь (ключ → значение), но применить hard routing (запрещённые ключи отправлять в другие поля).
    - Преобразовать spec_tables в объекты: каждая строка таблицы → отдельный объект в specifications, где ключ = object_key, значение = object_value (словарь параметров).
    - Применить особые случаи (таблицы огнестойкости EI30, таблицы с одной строкой и т.д.).
    4. Распределить routed_text из step_5_section_routing по полям: description, applications, advantages, instructions_manuals, exploitation, storage, security_measures, complectation, compatibility, additional_info. Габариты и вес/масса товара — это технические характеристики: помещай их в specifications, а поля dimensions/weight оставляй пустыми (см. раздел «ГАБАРИТЫ / ВЕС»).
    5. Собрать изображения (images) — дополнить base_domain, если URL относительный.
    6. Собрать варианты (variants) — если есть.
    7. Заполнить остальные поля (colors, complectation и т.д.) из соответствующих секций.
    8. Удалить пустые поля, пустые строки, пустые словари, пустые массивы.
    9. Вывести ТОЛЬКО финальный JSON, без пояснений, без markdown.

    ================================================================================
    ПРАВИЛА ФОРМИРОВАНИЯ PRODUCT_NAME (строго)
    ================================================================================

    Шаблон: [noun] [adjectives...] [brand/model] [size] [ГОСТ] (manufacturer)

    - noun — тип продукта (существительное). Определи из raw_h1 или из контекста.
    - adjectives — прилагательные (до трёх), согласованные с noun, идут строго после noun.
    - brand/model — торговая марка, модель, размеры (как в raw_h1).
    - ГОСТ — добавляется ТОЛЬКО если номер ГОСТ написан В САМОМ raw_h1 (в заголовке товара).
      НИКОГДА не бери ГОСТ из описания/характеристик/фраз «соответствует ГОСТ», «по ГОСТ» — подробно см. блок «ГОСТ В ИМЕНИ» ниже.
    - manufacturer — из параметра manufacturer, переданного в запросе (если непустой).

    Правила:
    - Первое слово — ВСЕГДА существительное-ТИП изделия в ИМЕНИТЕЛЬНОМ падеже («Насос», «Блок», «Станция»,
      «Установка», «Задвижка», «Клапан», «Гидроаккумулятор»…). СТРОГО ЗАПРЕЩЕНО ставить первым словом
      прилагательное, бренд, модель, артикул или типоразмер. Если в raw_h1 тип идёт не первым — ПЕРЕСТАВЬ:
      «Вертикальный многоступенчатый насос MULTI35» → «Насос вертикальный многоступенчатый MULTI35»;
      «MULTI35 насос» → «Насос … MULTI35»; «KIT 08 блок контроля потока» → «Блок контроля потока KIT 08».
      Аббревиатура-ТИП товара («ГЭС», «ЖБИ») считается существительным-типом и ставится первым словом.
      НО буквенно-цифровой КОД МОДЕЛИ (буквы, сразу за которыми идут цифры: «КТП6642», «КТ6023БС», «ВПК2110») —
      это НЕ тип, а модель/артикул: тип-существительное бери ИЗ КОНТЕКСТА страницы (категория/крошки/URL/бренд),
      сам код НЕ раскрывай и первым словом НЕ ставь (подробнее — спецслучай «КТП» ниже).
      СОСТАВНОЙ тип с квалификатором-существительным (D54, требование заказчика): в сочетаниях вида
      «Дизель генератор», «Бензин генератор» ТИП изделия — «Генератор», а слово топлива/привода перед ним —
      квалификатор, НЕ тип: «Дизель генератор 1600 кВт двигатель Cummins…» → «Генератор дизельный 1600 кВт
      двигатель Cummins…»; «Дизель-генераторная установка» → «Установка дизель-генераторная».
      Если явного существительного-типа в raw_h1 нет — определи тип изделия ПО СМЫСЛУ и поставь его первым словом.
    - Прилагательные после noun, в правильной грамматической форме (род, число, падеж).
    - Кавычки — прямые двойные (").
    - Удали символы ®, °.
    - «ё» замени на «е».
    - «Грунт» → «Грунтовка» (кроме «грунт-эмаль»).
    - «Шпаклевка» → «Шпатлевка».
    - Не дублируй бренд.
    - Не добавляй "под заказ", "нет в наличии".
    - Длина имени ≤ 255 символов (включая manufacturer в скобках). Если превышает — применяй КАСКАД СОКРАЩЕНИЯ
      в строгом порядке (порядок менять нельзя):
      1) убирай наименее важные прилагательные, СНАЧАЛА самые длинные; сохраняй грамматическое согласование
         остальных; НЕ удаляй закавыченные имена ("Эврика", "КАРАТ"), brand/model, размер, ГОСТ, manufacturer;
      2) если всё ещё длинно — оставь только ОДНО самое важное по смыслу прилагательное.
      ЗАПРЕЩЕНО: произвольно обрезать строку, добавлять «…», выдумывать новые аббревиатуры, удалять brand/model
      (если в raw_h1 нет короткой формы), удалять ГОСТ (если он в raw_h1), удалять manufacturer, нарушать
      согласование или порядок элементов шаблона, добавлять новые слова.

    Примеры корректных имён:
    - "Задвижка 30с941нж Ду50 Ру16 ГОСТ 12345-67 (Завод, ООО)"
    - "Ванна чугунная "Эврика" 170x75 (Завод Универсал, АО)"
    - "Компрессор винтовой Atlas Copco GA 11 - 8.5 PACK (Atlas Copco, Швеция)"

    ГОСТ В ИМЕНИ — ЖЁСТКОЕ ПРАВИЛО (САМАЯ ЧАСТАЯ ОШИБКА, читай внимательно):
    Добавляй ГОСТ в product_name ТОЛЬКО если номер ГОСТ присутствует В САМОМ raw_h1 (заголовке H1 товара).
    raw_h1 — ЕДИНСТВЕННЫЙ допустимый источник ГОСТ для имени. Всё остальное игнорируй:
    - НЕ бери ГОСТ из description, specifications, spec_items, spec_tables, routed_text, raw_sections,
      raw_lists, additional_info, tu — ни из какого поля, кроме raw_h1;
    - фразы в описании/характеристиках вида «соответствует ГОСТ X», «изготовлено/произведено по ГОСТ X»,
      «согласно ГОСТ X», «отвечает требованиям ГОСТ X», «ГОСТ X "<название стандарта>"» — это сведения
      о соответствии стандарту, а НЕ часть названия товара: добавлять ГОСТ в имя на их основании ЗАПРЕЩЕНО;
    - даже если ГОСТ встречается в тексте много раз — если его НЕТ в raw_h1, в product_name ГОСТ БЫТЬ НЕ ДОЛЖНО.

    Форматы ГОСТ, которые распознаём именно в raw_h1: ГОСТ XXXX-YY(YY), ГОСТ XXXXX-YY(YY),
    ГОСТ Р XXXX-YY(YY), ГОСТ ISO ..., ГОСТ Р ISO ..., ГОСТ EN ..., ГОСТ IEC ...,
    с точкой ГОСТ X.XXX-YYYY, ГОСТ XX.XXX-YYYY и т.п.

    Примеры (следуй СТРОГО):
    - raw_h1 = "Раковина малыш ГОСТ 6787-2001"          → имя СОДЕРЖИТ "ГОСТ 6787-2001" (ГОСТ есть в заголовке).
    - raw_h1 = "Раковина малыш", в описании "соответствует ГОСТ 18297-96" → имя БЕЗ ГОСТ.
    - raw_h1 = "Чугунная ванна «Бриз» 170×75", в тексте "ГОСТ 18297-96 «Приборы санитарные...»"
      → правильно: "Ванна чугунная «Бриз» 170×75 (Завод Универсал, АО)" — БЕЗ ГОСТ.

    СПЕЦСЛУЧАИ ИМЕНИ (соблюдай):
    - Доменные аббревиатуры раскрывай в тип-существительное ПО КОНТЕКСТУ СТРАНИЦЫ, а не по самой аббревиатуре
      (одна и та же аббревиатура у разных производителей значит разное):
      • «КТП» → «Подстанция комплектная трансформаторная» — раскрывай ТОЛЬКО если страница о трансформаторных
        подстанциях (категория/крошки/URL/текст это подтверждают): raw_h1 «2КТП ПК с коридорами обслуживания
        1000 кВА» в каталоге подстанций → «Подстанции комплектные трансформаторные 2КТП ПК с коридорами обслуживания 1000 кВА».
      • Если «КТП»/«КТ» — часть КОДА МОДЕЛИ (буквы+цифры: «КТП6642-У3», «КТ6023БС»), а страница о ДРУГОМ изделии
        (контакторы, выключатели и т.п. — видно по категории/крошкам/URL «/kontaktory-...»/бренду «Электроконтактор»),
        это КОД, НЕ тип: тип-существительное бери из контекста («Контактор», «Выключатель»), код оставь как модель:
        «КТП6642-У3» в каталоге контакторов → «Контактор КТП6642-У3» (НЕ «Подстанции комплектные трансформаторные»).
    - «ГЭС» = «Гидроэлектростанция» — это СУЩЕСТВИТЕЛЬНОЕ (тип товара): ставь «ГЭС» ПЕРВЫМ словом, все прилагательные/модель — после.
      Пример: «Микро ГЭС водопогружная GS-20» → «ГЭС водопогружная микро GS-20».
    - Сохраняй размеры/типоразмеры из raw_h1 ТОЧНО: «Задвижки 30с941нж Ду50 Ру16 (PN 1,6 МПа)» НЕ сокращай до «Задвижка 30с941нж».
    - Закавыченные имена остаются в кавычках и НЕ переносятся перед noun: «Клей "Липкая лента" Акрилит-43», а не «Клей липкая Акрилит-43».

    ================================================================================
    ПРАВИЛА ОБРАБОТКИ СПЕЦИФИКАЦИЙ (specifications)
    ================================================================================

    0. ЯЗЫК КЛЮЧЕЙ И ВЫБОР ТАБЛИЦЫ (критично): ключи характеристик — ВСЕГДА на русском.
       Если в источнике есть машинный блок характеристик с ЛАТИНСКИМИ ключами
       (coil_voltage_ac, coil_voltage_dc, aux_contacts, overall_dimensions, nominal_voltage,
       nominal_current, frequency, poles, number_of_poles и т.п.) — это служебный дубль данных
       сайта, а НЕ карточка. ИГНОРИРУЙ его и бери характеристики из РУССКОЙ таблицы
       «| Параметр | Значение |» (она полнее). Латинских (английских) ключей в specifications
       быть НЕ должно. Если русской таблицы в источнике нет — переведи ключи на русский по смыслу.

    1. Запрещённые ключи (НЕ класть в specifications):
    - Назначение → applications
    - Состав (ингредиенты) → description или additional_info
    - Применение / Способ применения → instructions_manuals
    - Рекомендации (по применению) → instructions_manuals
    - Инструкции (пошаговый способ применения) → instructions_manuals; наименования и ссылки на документы/сертификаты → ИГНОРИРОВАТЬ ПОЛНОСТЬЮ (см. раздел «ДОКУМЕНТЫ/ССЫЛКИ» ниже)
    - Условия хранения → storage
    - Условия эксплуатации → exploitation
    - Меры безопасности → security_measures
    - Преимущества → advantages
    - Совместимость → compatibility
    - Тара, упаковка, количество в упаковке/на поддоне («Количество на поддоне», «уложены по N шт») → complectation
    - Цена → price (только в поле price)
    - Минимальная партия, минимальный заказ → additional_info
    - Гарантия/гарантийный срок: про эксплуатацию или срок службы → exploitation; про условия хранения → storage; иначе → additional_info
    ВНИМАНИЕ: «Количество в 1 м^2», «в 1 м^3», «на 1 м.пог», «толщина (стенки)», «теплопроводность» и т.п.,
    а также ВЕС/МАССА и ГАБАРИТНЫЕ РАЗМЕРЫ товара — это технические характеристики, они ОСТАЮТСЯ в specifications
    (НЕ путать с количеством в упаковке/на поддоне и тарой — они → complectation).

    2. Для spec_items (простые пары ключ-значение):
    - Если ключ не запрещён, добавить в specifications как {key: value}.
    - ПРОТИВОРЕЧИЕ ИСТОЧНИКА (D42): если ОДИН И ТОТ ЖЕ ключ встречается в разных блоках страницы с
      РАЗНЫМИ значениями (напр. «Средний срок службы»: в таблице характеристик «18», в текстовом блоке
      надёжности «15 лет») — выводи ключ ОДИН раз со значением из ТАБЛИЦЫ характеристик; второе значение
      отбрось (дублировать ключ с конфликтом запрещено).
    - ПАРАМЕТРЫ РАЗНЫХ ЧАСТЕЙ ТОВАРА (D54, ТСС; ИСКЛЮЧЕНИЕ из правила D42): если таблица характеристик
      разбита на СЕКЦИИ («Общие характеристики», «Двигатель Cummins KTA50-G16B», «Генератор TSS-SA-1600» —
      заголовки ### в markdown), одинаково названные параметры в разных секциях — это параметры РАЗНЫХ
      ЧАСТЕЙ изделия, а НЕ дубли одного факта. Терять их ЗАПРЕЩЕНО. Сохраняй ВСЕ секции:
      «Общие характеристики» → плоские spec_items как есть; каждую именованную секцию части товара →
      объект spec_tables: object_key = заголовок секции («Двигатель Cummins KTA50-G16B»),
      object_value = все параметры секции {«Мощность номинальная, кВт»: «1760», ...}.
      Пример: общая «Мощность номинальная, кВт»=1600 остаётся в плоских характеристиках, а
      «Мощность номинальная, кВт»=1760 — внутри объекта «Двигатель Cummins KTA50-G16B». Подменять
      общее значение значением части товара (1600→1760) — грубая ошибка.
    - Нормализовать единицы измерения к КАРЕТНОЙ форме степени '^' (НЕ юникод-надстрочные): м2 → м^2, кг/м3 → кг/м^3, кг/м2 → кг/м^2, Вт/м·К → сохранить как есть. В выводе НИКОГДА не используй символы ²/³ — только '^' (на случай, если этап экстракции пропустил степень).

    3. Для spec_tables (многострочные таблицы), где КАЖДАЯ строка — отдельная марка/обозначение/модель:
    - Каждая строка → ОТДЕЛЬНЫЙ объект внутри specifications; ключ = значение идентифицирующего столбца
      («Наименование», марка «D500»/«Марка 350», предел огнестойкости «EI30» и т.п.), значение = объект из
      остальных столбцов (ключ→значение). НИКАКИХ массивов, группировок, слияний и обёрток-категорий.
    - Пример: {"object_key": "EI30", "object_value": {"Толщина": "3", "Расход": "4.7"}} → "EI30": {"Толщина": "3", "Расход": "4.7"}
    - Пример (таблица марок D500/D600):
      "Марка по средней плотности D500": {"Средняя плотность (кг/м^3)": "476-525", "Класс бетона по прочности на сжатие": "B2,5", "Коэффициент теплопроводности, Вт/м К": "0,130", "Марка по морозостойкости (цикл)": "F35; F100"},
      "Марка по средней плотности D600": {"Средняя плотность (кг/м^3)": "576-625", "Класс бетона по прочности на сжатие": "B3,5", ...}

    3a. ОСОБЫЙ СЛУЧАЙ — таблица с ОДНОЙ строкой данных, где первый столбец НЕ идентификатор модели
    (а значение параметра, напр. «IET90 - 90 мин.», «1-я группа»): НЕ создавай вложенный объект с этим значением
    как ключом. Вместо этого разложи в ПЛОСКИЕ пары key-value: ключи = заголовки столбцов, значения = ячейки строки.
      Пример. Таблица: | Предел огнестойкости | Глубина заделки, мм | Расход, кг |
                       | IET90 - 90 мин. (1,5 часа) | 200 | расчет |
      ПРАВИЛЬНО: {"Предел огнестойкости": "IET90 - 90 мин. (1,5 часа)", "Глубина заделки, мм": "200", "Расход, кг": "расчет"}
      ЗАПРЕЩЕНО: {"IET90 - 90 мин. (1,5 часа)": {"Глубина заделки, мм": "200", "Расход, кг": "расчет"}}
    Это правило имеет приоритет над «каждая строка → объект», когда строка не является отдельной маркой/моделью.

    3b. ПУСТЫЕ СТОЛБЦЫ-ПОДПИСИ ИЗОБРАЖЕНИЙ — НЕ ВКЛЮЧАЙ В specifications. Столбец таблицы, значение которого
    ПУСТО во ВСЕХ строках данных (как правило — столбец с изображением/чертежом, который конвертер убрал:
    «Чертеж…», «Схема…», «Рисунок…», «Фото…», «Изображение…»), НЕ включай в specifications вовсе — ни ключом
    во вложенном объекте, ни плоской парой.
      Пример. Таблица: | Тип | Чертеж тройника | Параметры по умолчанию |
                       | Тип 1 |   | H = 60 мм ... |   (столбец «Чертеж тройника» пуст у ВСЕХ строк)
      ПРАВИЛЬНО: {"Тип 1": {"Параметры по умолчанию": "H = 60 мм ..."}}  — без ключа «Чертеж тройника».
      ЗАПРЕЩЕНО: {"Тип 1": {"Чертеж тройника": "", "Параметры по умолчанию": "H = 60 мм ..."}}.
    ВАЖНО (не сломай группировку): убирай столбец ТОЛЬКО если он пуст У ВСЕХ строк. ЧАСТИЧНО пустой столбец
    (значение есть хотя бы у одной строки, а у других пусто — напр. «Ток, А, 1~230В»: "" у трёхфазных
    насосов) СОХРАНЯЙ у ВСЕХ строк: сигнатуры ключей вложенных объектов должны оставаться ОДИНАКОВЫМИ для
    группировки, поэтому такие пустые ячейки НЕ удаляй.

    4. Для таблиц огнестойкости (EI30, EI60, EI150): применяй то же различение, что в п.3/3a.
    - НЕСКОЛЬКО строк, где EI30/EI60/EI150 — идентификаторы строк (отдельные пределы) → каждая строка отдельным
      объектом с ключами "EI30 (30 мин.)", "EI60 (60 мин.)" и т.д. (как в п.3).
    - ОДНА строка данных, где предел огнестойкости — значение параметра в первом столбце (напр. "IET90 - 90 мин.",
      "1-я группа") → плоские пары key-value (как в п.3a), БЕЗ вложенного объекта с этим значением как ключом.

    4a. ТРАНСПОНИРОВАННАЯ таблица: ось-параметр в ПЕРВОЙ СТРОКЕ (D47, пример — система огнезащиты ET ВЕНТ).
    Если ПЕРВАЯ строка таблицы — ИМЯ ПАРАМЕТРА с рядом его значений по колонкам
    («Предел огнестойкости, EI, мин. | 30 | 60 | 90 | 120 | 150 | 180 | 240»), а последующие строки —
    параметры/материалы со значениями в тех же колонках, то:
    - ключи вложенных объектов = значения оси ВМЕСТЕ с её коротким именем: "EI 30 мин.", "EI 60 мин.", …
      Имя оси-параметра ТЕРЯТЬ или заменять генериком («Параметр») ЗАПРЕЩЕНО;
    - object_value каждого ключа = словарь {«имя строки»: «значение этой колонки»} для ВСЕХ последующих строк
      («Нагрузка на защищаемую конструкцию, кг/м^2», «МБОР-5Ф, м^2», …, «ПЛАЗАС (Толщина слоя, мм)»,
      «ПЛАЗАС (Расход, кг)»); пустые ячейки пропускай;
    - так каждый предел несёт ПОЛНЫЙ набор (нагрузка + материалы) и общие строки не теряются при разбиении
      таблицы на части при рендере.
    Пример: "EI 90 мин.": {"Нагрузка на защищаемую конструкцию, кг/м^2": "3,4", "МБОР-8Ф, м^2": "1,1",
    "ПЛАЗАС (Толщина слоя, мм)": "2,0", "ПЛАЗАС (Расход, кг)": "2,8"}

    4b. СОСТАВНОЕ ОБОЗНАЧЕНИЕ С ЛЕГЕНДОЙ → РАЗВЕРНИ В ХАРАКТЕРИСТИКИ (D60, Саранск): если значение/ячейка
    содержит условное обозначение изделия ВМЕСТЕ с расшифровкой «<обозначение>, где N — <название параметра>»,
    разложи расшифровку в ОТДЕЛЬНЫЕ пары key→value; имена параметров бери из ЛЕГЕНДЫ ДОСЛОВНО, а обозначение
    сохрани дополнительно ключом «Обозначение». Пример. Ячейка «35х30х0,1- I 20х13, где 35 — наружный диаметр
    сильфона, мм; 30 — количество пар мембран, шт; 0,1 — толщина стенки сильфона, мм; I — тип 1 (внутренний
    диаметр 22 мм); II — тип 2 (внутренний диаметр 18,5 мм)» →
    {"Обозначение": "35х30х0,1- I 20х13", "Наружный диаметр, мм": "35", "Количество пар мембран, шт": "30",
    "Толщина стенки, мм": "0,1", "Внутренний диаметр (тип 1), мм": "22", "Внутренний диаметр (тип 2), мм": "18,5"}.
    Если далее идут ДРУГИЕ строки-обозначения того же изделия в том же формате AxBxC (напр. «125х15х0,2,
    внутренний диаметр — 107 мм») — примени ту же расшифровку к КАЖДОЙ (первое число — наружный диаметр,
    второе — количество пар мембран, третье — толщина стенки) плюс явно указанный внутренний диаметр, и вынеси
    КАЖДУЮ отдельным объектом spec_tables (object_key = обозначение или марка сплава, object_value =
    расшифрованные пары). Обозначение НЕ должно оставаться ЕДИНСТВЕННОЙ характеристикой — числа из легенды
    обязаны попасть в specifications.

    ================================================================================
    ГАБАРИТЫ / ВЕС / КОЛИЧЕСТВО В УПАКОВКЕ — РАЗМЕЩЕНИЕ (СТРОГО)
    ================================================================================

    Спец-параметры (ищи их в spec_items, raw_sections, raw_h1, raw_lists):
    - ГАБАРИТНЫЕ РАЗМЕРЫ товара — «Размеры», «Габариты», «ДхШхВ», формат AAA×BBB×CCC мм (напр. 390×90×188 мм).
    - ВЕС / МАССА единицы товара — «Вес», «Масса» (напр. 9 кг).
    - КОЛИЧЕСТВО В УПАКОВКЕ — «Количество на поддоне», «в упаковке», «в пачке», «уложены по N шт».
      (НЕ относи сюда «Количество в 1 м^2», «в 1 м^3», «на 1 м.пог» — это обычные характеристики для specifications.)

    ГАБАРИТЫ и ВЕС/МАССА — ЭТО ТЕХНИЧЕСКИЕ ХАРАКТЕРИСТИКИ. ВСЕГДА размещай их в блоке технических
    характеристик (specifications) обычными парами ключ→значение:
    - габаритные размеры → пара в specifications: ключ из источника («Размеры»/«Габариты»; если ключа в
      источнике нет — «Габаритные размеры»), значение как есть (напр. "390×90×188 мм");
    - вес / масса → пара в specifications: ключ из источника («Вес»/«Масса»; если ключа нет — «Масса»),
      значение как есть (напр. "9 кг").
    Если габаритов/значений веса несколько (составной товар) — добавь их несколькими парами в specifications.
    ВЫДЕЛЕННЫЕ поля dimensions и weight ВСЕГДА ОСТАВЛЯЙ ПУСТЫМИ (""): НЕ выноси туда вес/габариты и НЕ
    дублируй их там — каждое значение присутствует РОВНО ОДИН РАЗ, строкой в таблице характеристик.

    ТАБЛИЧНЫЕ габариты/вес (как у Агроскон-ЖБИ): если габариты (столбцы/строки «Длина»/«Ширина»/«Высота»/
    «Размеры») и/или вес («Вес, тн»/«Масса») заданы в МНОГОСТРОЧНОЙ таблице характеристик — у КАЖДОЙ
    марки/модели/типоразмера свои значения (напр. «Длина, l», «Ширина, b», «Высота, h», «Вес, тн» по маркам
    6 К.7 / 1П.7 / ЭДД.1.7) — они ОСТАЮТСЯ В ТАБЛИЦЕ (specifications) КАК ЕСТЬ. СТРОГО ЗАПРЕЩЕНО: удалять
    столбцы «Вес»/«Длина»/«Ширина»/«Высота»; собирать искусственные «Габариты» вида AAA×BBB×CCC из
    отдельных столбцов Длина/Ширина/Высота.

    КОЛИЧЕСТВО В УПАКОВКЕ / ТАРА → complectation (напр. "Уложены на поддоны по 72 шт."), НЕ в specifications.
    В specifications ключей «Количество на поддоне»/тары/упаковки быть НЕ должно.

    Каждое значение размещается РОВНО В ОДНОМ поле — дублирование ЗАПРЕЩЕНО: габариты и вес/масса — строкой
    в specifications (поля dimensions/weight пустые), упаковка — в complectation.
    ВАЖНО: габариты/вес/упаковку НИКОГДА не теряй. Если они есть на странице — они ОБЯЗАТЕЛЬНО присутствуют
    в результате ровно один раз: габариты и вес/масса — строкой в specifications, упаковка — в complectation.
    Не удаляй их «в никуда».
    НЕ собирай искусственный description из габаритов, веса, упаковки, прайса или характеристик.
    Если на странице нет отдельного связного описательного текста о товаре — оставь description пустым ("").
    Прайс, условия доставки, акции, согласие на обработку данных, телефоны — это НЕ description;
    при необходимости помещай их в additional_info, а цену — в price.

    ================================================================================
    РАСПРЕДЕЛЕНИЕ СЕКЦИЙ (из step_5_section_routing)
    ================================================================================

    Для каждого элемента section_routing:
    - semantic_route определяет целевое поле:
    * "description" → description (МАКСИМУМ 1–3 предложения; избыток распредели по смыслу в другие поля — см. п.5 «КРИТИЧЕСКИХ ПРОВЕРОК»)
    * "applications" → applications
    * "advantages" → advantages
    * "instructions_manuals" → instructions_manuals
    * "exploitation" → exploitation
    * "storage" → storage
    * "security_measures" → security_measures
    * "complectation" → complectation
    * "compatibility" → compatibility
    * "dimensions" / "weight" → значения габаритов и веса/массы помещай ПАРАМИ в specifications (раздел
      «ГАБАРИТЫ / ВЕС»); поля dimensions и weight ОСТАВЛЯЙ ПУСТЫМИ ("") — значения в них не дублируй.
    * "specifications" → добавлять в specifications (уже обработано)
    * "additional_info" → additional_info.text

    - Если routed_text содержит список (маркированный или нумерованный), преобразуй в многострочную строку с маркерами "- " в начале каждой строки.
    - Объединяй несколько секций с одинаковым semantic_route через перенос строки (\\n), сохраняя порядок.
    - Если поле уже заполнено из других источников (например, dimensions из спецификаций), НЕ дублируй.

    КЛАССИФИКАЦИЯ ПО СМЫСЛУ (ОБЯЗАТЕЛЬНО, даже без заголовка и без semantic_route):
    Распредели ВЕСЬ содержательный текст по полям ПО СМЫСЛУ, а не только по явному заголовку.
    Если абзац описывает назначение/где и для чего применяется товар — он идёт в applications, даже если
    заголовка «Назначение»/«Область применения» нет. Аналогично по смыслу:
    - способ применения / подготовка / монтаж / нанесение / эксплуатация по шагам → instructions_manuals;
    - преимущества / особенности / достоинства → advantages;
    - совместимость с материалами/системами → compatibility;
    - комплектация / упаковка / состав комплекта → complectation;
    - условия и СРОКИ эксплуатации/службы → exploitation;  условия/срок хранения → storage;
    - меры безопасности → security_measures.
    Только если смысл абзаца не подходит НИ К ОДНОМУ профильному полю — клади его в additional_info.text.

    СРОКИ СЛУЖБЫ/ЭКСПЛУАТАЦИИ → exploitation (важно, частая ошибка):
    «Срок службы», «Гарантийный срок эксплуатации», «Гарантийный срок» (про эксплуатацию, не про хранение),
    условия эксплуатации — ВСЕГДА в exploitation, НЕ в additional_info.
    Пример: «Гарантийный срок эксплуатации – 3 года со дня ввода в эксплуатацию, но не более 5 лет с момента
    отгрузки предприятием-изготовителем. Срок службы не менее 10 лет.» → ЦЕЛИКОМ в exploitation.
    В exploitation идут и ВНЕШНИЕ УСЛОВИЯ эксплуатации (распознавай по смыслу, даже в таблицах):
    температура окружающего воздуха, влажность, тип атмосферы, высота над уровнем моря, скорость ветра,
    климатическая категория и категория размещения, агрессивная среда (пыль/песок), климатические зоны,
    условия транспортировки и монтажа. Пример: «Температурный режим от −45 ºС до +60 ºС. Влажность не более 80%.»

    ИНСТРУКЦИИ/СПОСОБ ПРИМЕНЕНИЯ → instructions_manuals — ТОЛЬКО детальный процедурный текст
    (подготовка поверхности, замешивание, нанесение, выдержка, рекомендации по применению).
    Пример ВЕРНОГО содержимого:
    «Поверхность основания тщательно очистить от пыли, масел, краски и других веществ, снижающих адгезию.
    Сухую смесь затворить водой (количество — в штампе на мешке); влить воду в ёмкость 30–100 л, засыпать смесь
    и перемешать миксером до однородности, выдержать паузу 3 мин и перемешать повторно.»
    Если под заголовком «Способ применения»/«Инструкция» только название документа без шагов — instructions_manuals = "".

    ДОКУМЕНТЫ/ССЫЛКИ — ИГНОРИРОВАТЬ ПОЛНОСТЬЮ (КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО включать в любое поле):
    Наименования и ссылки на документы НЕ помещай НИ В ОДНО поле (ни в instructions_manuals, ни в additional_info,
    ни в description, ни в любое другое): технический паспорт, сертификат/декларация соответствия, протокол/отчёт
    испытаний, рекомендации к применению в виде документа, инструкция-документ, и любые маркеры скачивания
    «Скачать»/«СКАЧАТЬ ДОКУМЕНТ» вместе со ссылками на файлы (.pdf, .doc, .docx, .xls, URL вида (/upload/...),
    (/images/...)). Это служебные ссылки, а НЕ данные о товаре — отбрось их целиком.
    Примеры (отбросить полностью, НЕ переносить никуда):
    - «Технический паспорт Скачать (/upload/iblock/441/...ПаспортАвто.-С. двухрядный....doc)»
    - «Сертификат соответствия Скачать (/upload/iblock/69f/Сертификат соответствия КНУ-С.PDF)»
    - «Рекомендации к применению конвекторов СКАЧАТЬ ДОКУМЕНТ (/images/Рекомендации....pdf)»
    Если под заголовком «Способ применения»/«Инструкция» дан ТОЛЬКО заголовок документа без пошагового текста —
    оставь instructions_manuals пустым ("").

    ================================================================================
    ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ
    ================================================================================

    - tu (ТУ): извлекай ТОЛЬКО строки официальных форматов:
        ТУ XX.XX.XX-XXX-XXXXXXXX-YYYY (ОКПД 2) или ТУ XXXX-XXX-XXXXXXXX-YYYY (ОКП);
        допустимы спец-форматы с индексом «ТУ» (военные/ИТ/ЕСПД): ТУ X1234.567890-01-12, ТУ ХХХХ.XXXXXX-XX.
        Строка ОБЯЗАНА содержать «ТУ» как часть номера. Если несколько — каждый с новой строки (\\n). Если нет — "".
        СТРОГО ЗАПРЕЩЕНО считать за ТУ (это НЕ ТУ):
        - строки БЕЗ «ТУ»: 3.501.3-183, серия 3.503.1-96, Р 400.98.15-3AIII, F1 p(l)-5-100;
        - ГОСТ (напр. ГОСТ 6482-88); серии, проекты, каталожные номера, коды документации;
        - строки с «ТУ» в НЕВЕРНОМ формате: «ТУ № 5», «product ТУ», «ТУ 123», «ИБПД.528000.010ТУ», «1БП.204.301ТУ».
        ИСТОЧНИКИ ТУ — проверь ВСЕ, пропуск ТУ при его наличии на странице это ошибка (D45):
        (а) строка/подпись сразу ПОД НАЗВАНИЕМ товара (часто полужирная: **ТУ 23.99.19-018-08621635-2020**),
        включая скобку «(Взамен ТУ ...)» — берётся действующий номер, а не заменённые;
        (б) упоминание внутри описания/текста секций;
        (в) колонка «Техническая документация» / «Нормативный документ» таблицы параметров — если в ячейках
        строк стоят коды ТУ, собери уникальные коды в tu (каждый с новой строки).
        ТОЛЬКО ТУ САМОГО ТОВАРА: ТУ ДРУГИХ изделий/компонентов, упомянутых на странице
        (разбавитель, грунтовка, клей, отвердитель — напр. «СОЛЬВ-УР (ТУ 2319-032-…)» в таблице
        способов нанесения), в tu НЕ брать. При нескольких кандидатах приоритет — ТУ из позиции (а),
        под названием товара.
    - price: найди цену (например, "1 200 руб", "1200.00") и преобразуй в строку с плавающей точкой: "1200.00".
      Источник цены — поле/блок «Цена», «Исполнение и цена» или столбец «Цена, ₽/руб.». Если задан ДИАПАЗОН
      («от 6400 до 7600 руб.») или у разных исполнений разные цены — возьми МИНИМАЛЬНУЮ и запиши числом ("6400.00").
      Цену НЕ теряй: если блок «Цена» на странице есть — поле price ОБЯЗАНО быть заполнено.
    - article: артикул (любая строка "Артикул: XXX" или "Код: XXX").
    - colors: массив цветов продукта (если есть). Бери из raw_lists или routed_text, если это перечень цветов (например, RAL ...). В colors — только сами обозначения/названия цветов.
    - images: дополни base_domain, если URL относительный. Удали дубликаты.
    - variants: возьми из step_8_collect_variants ТОЛЬКО потребительские варианты (цвет, размер, конфигурация), нормализуй структуру (каждый вариант — объект с ключами Цвет, Размер, Модель, Серия, Другие параметры). Технические таблицы обозначений/спецификаций сюда НЕ помещай — они уже в specifications.
      СТРОГО ЗАПРЕЩЕНО помещать в variants информацию об упаковке: вес/объём/размер тары и варианты фасовки
      («7 кг, ведро», «15 кг, ведро», «45 кг, ведро») → ТОЛЬКО в complectation. Если секция называется «Варианты»,
      но перечисляет разные веса/объёмы фасовки — это НЕ потребительский вариант, а упаковка → complectation.
      Дублировать данные из specifications и указывать цены в variants ЗАПРЕЩЕНО.
    - complectation (комплектация / состав комплекта / упаковка): маркированный список — КАЖДАЯ позиция
      С НОВОЙ СТРОКИ. Формат строки СТРОГО: «- Название N шт.;» — в начале ТОЛЬКО «- » (короткое тире
      и пробел, никаких номеров и других знаков), затем название, затем через пробел количество («N шт.»,
      если оно есть), в КОНЦЕ КАЖДОЙ строки точка с запятой «;». ОЧИСТИ исходный текст псевдотаблицы:
      УБЕРИ порядковые номера строк (одиночные ведущие «1», «2», «3» — это индекс строки, а НЕ количество)
      и лишние переводы строк/пустые строки. Пример: из «1\n\nКабельные вводы\n\n1 шт.\n\n2\n\n
      Обратный клапан\n\n1 шт.» получи:
      «- Кабельные вводы 1 шт.;
      - Обратный клапан 1 шт.;»
      Если у позиции нет количества — только название и «;». НЕ путай порядковый номер строки с количеством
      и НЕ трогай числа внутри названий («3-ходовой клапан» остаётся как есть). Это правило для complectation
      имеет приоритет над общим правилом списков (раздел «РАСПРЕДЕЛЕНИЕ СЕКЦИЙ»).

    ================================================================================
    КРИТИЧЕСКИЕ ПРОВЕРКИ ПЕРЕД ВЫВОДОМ (соблюдай строго)
    ================================================================================
    0. ЯЗЫК ОРИГИНАЛА: НЕ переводи ничего. Имена характеристик (ключи specifications), заголовки и любой
       текст оставляй ДОСЛОВНО как в исходных данных (напр. «Высота, h» — это «Высота, h», а НЕ «High, h»;
       «Ширина»≠«Width», «Длина»≠«Length»). Перевод ключей/значений на другой язык ЗАПРЕЩЁН.
    1. ЦЕНА — строго и ТОЛЬКО в поле price (строка-число, напр. "382.00" или "6400.00"). Если цен несколько
       (разные варианты/исполнения) или задан диапазон «от X до Y руб.» — возьми МИНИМАЛЬНУЮ (X) в price.
       КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО помещать цену, диапазон цен, слова «Цена»/«руб»/«₽»/«Стоимость» или столбец
       стоимости в specifications, variants, description, additional_info, advantages и ЛЮБОЕ другое поле,
       кроме price. Убери ключи/столбцы цены и стоимости из specifications и variants; если исходная таблица
       исполнений содержала колонку/блок цены — цена уходит в price, а из прочих полей удаляется полностью.
    2. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ДУБЛИРОВАНИЯ: каждый факт/значение присутствует РОВНО В ОДНОМ поле.
       - Габариты и вес/масса товара идут СТРОКАМИ в specifications; поля dimensions и weight при этом ПУСТЫЕ
         (""), дублировать в них значения ЗАПРЕЩЕНО. Упаковка/тара → complectation, и ключей «Количество на
         поддоне»/тары в specifications быть НЕ должно.
       - Если таблица обозначений ушла в specifications — её НЕ должно быть в variants.
       - В specifications НЕ должно быть ключа-подписи изображения/чертежа с пустым значением
         («Чертеж…», «Схема…», «Рисунок…», «Фото…»): столбец, пустой во ВСЕХ строках, не включается
         (см. п.3b «ПУСТЫЕ СТОЛБЦЫ-ПОДПИСИ»); частично пустые ячейки под-словарей одинаковой сигнатуры оставляй.
       - Если список цветов ушёл в colors — не повторяй его в additional_info.
       - Любое значение, попавшее в specifications (типоразмеры, габариты, вес, любые числовые
         характеристики), НЕ повторяй НИ В ОДНОМ текстовом поле — ни в advantages, ни в applications,
         ни в exploitation, instructions_manuals, storage, compatibility, security_measures, additional_info,
         ни в description. Если такое значение уже есть строкой в specifications, а в текстовом поле стоит
         его дубль — УДАЛИ строку-дубль ЦЕЛИКОМ, сохранив остальной текст поля связным (маркированный пункт
         вида «- Типоразмер мойки: от 400×400×250 до 500×500×300 мм.» или «- Габариты каркаса: …» просто
         убери из списка, прочие пункты оставь на месте). Если характеристика с числовым значением упомянута
         ТОЛЬКО в текстовом поле (в specifications её ещё нет, в т.ч. как сводный диапазон «от … до …» поверх
         детальных строк) — НЕ выбрасывай её: ПЕРЕНЕСИ в specifications парой «Ключ»: «значение»
         (напр. «Типоразмер мойки»: «от 400×400×250 до 500×500×300 мм»), а из текстового поля удали.
       - description НЕ должен повторять габариты, вес, упаковку, прайс или содержимое specifications.
       Перед выводом пройди по всем полям и удали повторяющиеся данные, оставив каждое значение ровно один раз.
    3. УБЕРИ разметку Markdown из текстовых полей (**жирный**, *курсив*, #, ![](), []()), сохранив структуру
       (списки — строками с "- " в начале, переносы строк, абзацы).
    4. product_name: добавь "(<manufacturer>)" в конце, если manufacturer передан и его там ещё нет.
    5. ПОЛЕ description — КРАТКОЕ общее описание товара, МАКСИМУМ 1–3 предложения. Это самое важное ограничение
       для description. Если описательного текста больше — оставь в description только 1–3 вводных предложения
       об общей сути/назначении товара, а ОСТАЛЬНОЕ распредели по смыслу в профильные поля (applications,
       advantages, instructions_manuals, exploitation, compatibility, storage, security_measures …), НЕ в description.
       ИСКЛЮЧЕНИЕ (D61): техническое описание УСТРОЙСТВА/КОНСТРУКЦИИ изделия и МАТЕРИАЛОВ деталей (тип насоса/
       агрегата, корпус, вал, рабочее колесо, подшипники, уплотнение; серый чугун, углеродистая/нержавеющая сталь,
       «морское исполнение» и т.п.) — это СУТЬ изделия, а не реклама: помещай его в description ПОЛНОСТЬЮ, лимит
       1–3 предложения на него НЕ распространяется. Такой текст НЕЛЬЗЯ выбрасывать и НЕЛЬЗЯ класть в additional_info.
       В description ТОЛЬКО связный текст о сути/назначении товара. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО помещать в description:
       - строки прайса/доставки/акций («от N шт.», «самовывоз», «с доставкой», «руб», «залоговая стоимость», «АКЦИ…»)
         → цена идёт в price, остальное в additional_info;
       - сведения об упаковке/поддоне («уложены на поддоны по N шт») → complectation;
       - габаритные размеры (AAA×BBB×CCC мм), вес/массу и любые значения, уже попавшие в specifications;
       - подробные условия применения/эксплуатации (давление, температура теплоносителя по шагам) → instructions_manuals/exploitation.
       НЕ копируй в description «сырой» блок со страницы целиком. Если связного описательного предложения о товаре
       нет — поставь description="" (пустая строка).
    6. ГОСТ в product_name: проверь, что номер ГОСТ присутствует в имени ТОЛЬКО если он был в raw_h1.
       Если в raw_h1 ГОСТ НЕТ (а в описании/характеристиках есть «соответствует ГОСТ», «по ГОСТ X»,
       «ГОСТ X "<название стандарта>"») — УБЕРИ ГОСТ из product_name. raw_h1 — единственный источник ГОСТ для имени.
    7. ТРЕТЬЕ ЛИЦО (обезличивание): во ВСЕХ текстовых полях (description, applications, advantages,
       instructions_manuals, exploitation, storage, security_measures, complectation, compatibility,
       additional_info) УБЕРИ речь от первого лица и самоназвания продавца/магазина/сайта. Заменяй их
       на нейтральное «производитель» (или на имя производителя, переданное в запросе), сохраняя смысл
       и грамматику (род, число, падеж):
       - «мы», «мы производим/предлагаем/выпускаем/гарантируем» → «производитель производит/предлагает/…»;
       - «наш/наша/наше/наши…» («наша компания», «наш завод», «наш магазин», «на нашем сайте», «наши
         специалисты») → «производитель» / «у производителя» / «специалисты производителя»;
       - «у нас», «купить у нас», «обращайтесь к нам» → «у производителя», либо УДАЛИ фразу, если это
         чистый призыв/реклама без данных о товаре.
       НЕ вводи местоимения второго лица («вы», «вам») и не выдумывай фактов. Если фраза целиком рекламная
       («звоните», «лучшие цены только у нас») — просто удали её. Правило применяй по смыслу, а не только к
       дословным «мы»/«наш».
    8. PRODUCT_NAME — ПЕРВОЕ СЛОВО: убедись, что product_name начинается с существительного-типа изделия в
       именительном падеже, а НЕ с прилагательного/бренда/модели/артикула/типоразмера. Если первым идёт
       прилагательное или марка — переставь так, чтобы тип изделия («Насос», «Блок», «Станция», «Установка»…)
       стоял первым словом, а прилагательные/бренд/модель — после него (напр. «Вертикальный многоступенчатый
       насос MULTI35» → «Насос вертикальный многоступенчатый MULTI35»).

    ================================================================================
    ОБЩИЕ ТРЕБОВАНИЯ К ВЫВОДУ
    ================================================================================

    - Все поля, перечисленные в output schema, ДОЛЖНЫ присутствовать. Если данных нет, то:
        * для строк → ""
        * для словарей → {}
        * для массивов → []
    - Не удаляй ключи из схемы.
    - Не добавляй лишних ключей.
    - Выводи ТОЛЬКО JSON объект, начинающийся с "{" и заканчивающийся "}".
    - НИКАКОГО текста до или после JSON, НИКАКОГО markdown (```json), НИКАКИХ объяснений.

    Схема финального JSON:
    {
    "product": {
        "product_name": "",
        "tu": "",
        "price": "",
        "article": "",
        "description": "",
        "specifications": {},
        "colors": [],
        "images": [],
        "variants": [],
        "complectation": "",
        "compatibility": "",
        "applications": "",
        "advantages": "",
        "instructions_manuals": "",
        "exploitation": "",
        "storage": "",
        "security_measures": "",
        "dimensions": "",
        "weight": "",
        "additional_info": {"text": ""}
    }
    }
    """

    async def _sgr_formatter(self, reasoning_dict: Dict, base_domain: str = "", manufacturer: str = "") -> Tuple[Optional[Dict], Dict[str, int], bool]:
        """
        Второй промпт: применяет все правила нормализации, форматирования имени,
        обработки таблиц, роутинга секций и т.д. на основе структурированного reasoning.
        """
        # Сериализуем reasoning в компактный JSON (для передачи модели)
        reasoning_json = json.dumps(reasoning_dict, ensure_ascii=False, indent=2)
        system_prompt = self._formatter_system_prompt()

        user_prompt = f"""Base domain (для формирования абсолютных URL изображений): {base_domain}
    Manufacturer (добавь в конце product_name в скобках, если непустой): {manufacturer}

    Вот сырые данные от SGR-REASONER. Преобразуй их в финальный JSON продукта, строго следуя правилам выше.

    Reasoning JSON:
    {reasoning_json}
    """

        # Две попытки: 0.2, затем «горячая» 0.7 — сбивает дегенеративное зацикливание
        # reasoning-модели (пустой content, см. комментарий в _MODEL_TUNING).
        _attempt_temps = (0.2, 0.7)
        token_usage = {'input_tokens': 0, 'output_tokens': 0}
        for attempt, _temp in enumerate(_attempt_temps):
            data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": _temp,
                "timeout": 80,
                "response_format": {"type": "json_object"}
            }
            data.update(_payload_tuning(self.model))
            result, _usage = await self._make_request_with_retry(data)
            for _k in ('input_tokens', 'output_tokens'):
                token_usage[_k] += (_usage or {}).get(_k, 0)
            if not result:
                continue
            content = result['choices'][0]['message']['content']
            try:
                if _tuning_for(self.model)["robust"]:
                    final_dict = _robust_json_extract(content)
                    if final_dict is None:
                        raise ValueError("robust parse failed")
                else:
                    cleaned = self._clean_json_response(content)
                    try:
                        final_dict = json.loads(cleaned)
                    except Exception:
                        # D35: локальный ремонт перед отказом (см. _sgr_reasoning)
                        final_dict = _robust_json_extract(content)
                        if final_dict is None:
                            raise
                if "product" not in final_dict:
                    final_dict = {"product": final_dict}
                # Опциональная валидация через Pydantic (можно включить позже)
                # ProductOutput(**final_dict["product"])
                return final_dict, token_usage, True
            except Exception as e:
                log.error(f"SGR formatter JSON error (attempt {attempt+1}): {e}")
                self._save_sgr_error_response("formatter", content, attempt)
        return None, token_usage, False

    def _save_sgr_error_response(self, stage: str, content: str, attempt: int) -> None:
        """Сохраняет ошибочный ответ SGR в файл для отладки."""
        try:
            log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            timestamp = int(time.time())
            filename = f"sgr_error_{stage}_{timestamp}_attempt{attempt+1}.json"
            filepath = os.path.join(log_dir, filename)
            error_data = {
                "stage": stage,
                "attempt": attempt,
                "timestamp": datetime.now().isoformat(),
                "content": content
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(error_data, f, ensure_ascii=False, indent=2)
            log.info(f"Сохранён ошибочный SGR ответ: {filepath}")
        except Exception as e:
            log.error(f"Не удалось сохранить ошибочный SGR ответ: {e}")    
            
    def _single_system_prompt(self) -> str:
        """Системный промпт единого вызова (single_1call): правила REASONER + правила и схема
        FORMATTER (текст правил — verbatim из соответствующих методов) + override-блок, который
        задаёт архитектуру вывода (без промежуточной step_-структуры). Победитель A/B (REPORT.md §7)."""
        override = (
            "\n\n================================================================================\n"
            "АРХИТЕКТУРА ВЫВОДА (важно): НЕ выводи промежуточный JSON со step_1..step_8. "
            "Мысленно выполни классификацию и извлечение по правилам REASONER, затем выведи "
            "ТОЛЬКО финальный JSON товара по схеме FORMATTER ниже. Если по правилам классификации "
            'страница НЕ товарная (различных числовых характеристик < 3) — верни ровно {"error": "Trash_418#"}.'
        )
        return self._reasoning_system_prompt() + "\n\n" + self._formatter_system_prompt() + override

    def _single_user_prompt(self, text: str, manufacturer: str, base_domain: str) -> str:
        """Пользовательский промпт единого вызова: правила REASONER без хвостовой инструкции о
        выводе step_-структуры (это архитектура, а не правило извлечения)."""
        ru = self._reasoning_user_prompt(text, manufacturer, base_domain)
        return ru.split("ВЫВОДИ ТОЛЬКО JSON")[0].rstrip()

    async def _sgr_single(self, text: str, manufacturer: str, base_domain: str) -> Tuple[Optional[Dict], Dict[str, int]]:
        """Единый вызов извлечения товара (single_1call) — победитель A/B для gemini-3-flash-preview.
        Один POST вместо двух (REASONER+FORMATTER): temperature=0, reasoning_effort=low, устойчивый
        парсер (модель подмешивает <think> в content). Возвращает:
          {"product": {...}}      — товар;
          {"error": "Trash_418#"} — страница не товарная (вердикт модели);
          None                    — сетевая ошибка или нечитаемый ответ (D35): страница
                                    не помечается обработанной и будет повторена."""
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._single_system_prompt()},
                {"role": "user", "content": self._single_user_prompt(text, manufacturer, base_domain)},
            ],
            "temperature": 0.0,
            "reasoning_effort": _effort_for_model(self.model),
            "timeout": 150,
            "response_format": {"type": "json_object"},
        }
        result, token_usage = await self._make_request_with_retry(data)
        if not result:
            return None, token_usage
        content = result['choices'][0]['message']['content']
        fd = _robust_json_extract(content)
        if not isinstance(fd, dict):
            # D35: сбой парсинга ≠ «не товар». Возвращаем None (как сетевую ошибку) —
            # страница НЕ помечается обработанной и будет переизвлечена следующим прогоном.
            log.error("SGR single: не удалось разобрать JSON ответа модели, страница будет повторена")
            self._save_sgr_error_response("single", content or "", 0)
            return None, token_usage
        if fd.get("error") == "Trash_418#":
            return {"error": "Trash_418#"}, token_usage
        final_dict = fd if "product" in fd else {"product": fd}
        _normalize_spec_keys_ru(final_dict)  # D78: латинские ключи ТХ -> русские (детерминированно)
        _strip_price_spec_keys(final_dict)   # цена только в price: ключи-цены из ТХ (детерминированно)
        return final_dict, token_usage

    async def extract_product_info(self, text: str, manufacturer: str, base_domain: str, use_sgr: bool = True) -> Tuple[Optional[Dict], Dict[str, int]]:
        """Извлечение структурированной информации о товаре из Markdown. Архитектура — ПЕР-МОДЕЛЬНО
        (Анализ системы/_model_ab/REPORT.md §4): gemini/glm работают единым вызовом (_sgr_single),
        остальным (deepseek/qwen/minimax) нужен 2-вызовный SGR (_sgr_reasoning→_sgr_formatter),
        т.к. наивный single_1call у них печатает промежуточную step_-структуру вместо товара."""
        if self.model in _SINGLE_CALL_MODELS:
            return await self._sgr_single(text, manufacturer, base_domain)

        # 2-вызовная SGR (REASONER → гейт is_product → FORMATTER)
        reasoning_dict, token_usage1, valid_reasoning = await self._sgr_reasoning(text, manufacturer, base_domain)
        if not valid_reasoning or not reasoning_dict:
            # D35: сбой парсинга/сети ≠ «не товар»: None → страница не помечается
            # обработанной и будет переизвлечена (Trash_418# — только вердикт модели)
            return None, token_usage1
        if not reasoning_dict.get("step_1_page_classification", {}).get("is_product_page", False):
            return {"error": "Trash_418#"}, token_usage1
        final_dict, token_usage2, valid_formatter = await self._sgr_formatter(reasoning_dict, base_domain, manufacturer)
        total_tokens = {
            'input_tokens': token_usage1.get('input_tokens', 0) + token_usage2.get('input_tokens', 0),
            'output_tokens': token_usage1.get('output_tokens', 0) + token_usage2.get('output_tokens', 0),
        }
        if not valid_formatter or not final_dict:
            # D35: см. выше — сбой форматтера не является классификацией страницы
            return None, total_tokens
        _normalize_spec_keys_ru(final_dict)  # D78: латинские ключи ТХ -> русские (детерминированно)
        _strip_price_spec_keys(final_dict)   # цена только в price: ключи-цены из ТХ (детерминированно)
        return final_dict, total_tokens
    
    async def extract_company_info(self, text: str, company_id: str, chunk_info: str = "") -> Tuple[Optional[str], Dict[str, int]]:
        """Извлечение информации о компании из объединенного контента или чанков"""
    
        prompt = f"""Analyze the text and extract information about ONLY the primary manufacturer. Return a response in structured text format.
                    Extract absolutely all addresses and phone numbers.
                    If an address or phone number is associated with an office, warehouse, or other location, please indicate this. Return a response in structured text format.
                    company_id: "{company_id}"
                    Text for analysis: ({chunk_info}): {text}"""        
        try:
            data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": """Role: You are a specialized AI assistant that accurately extracts structured information about the primary manufacturer from Markdown code.
                                                    Task: Carefully analyze the provided Markdown and extract all information about the primary manufacturer.
                                                    The page may contain mixed data: information about the primary manufacturer and information about representative offices/suppliers/branches/dealers.
                                                    You must correctly classify this information. 
                                                    Structure and display ONLY information about the primary manufacturer:
                                                    - The primary company address and the supplier address are ALWAYS DIFFERENT.
                                                    - If there are no suppliers, DO NOT ADD the primary company to the suppliers.
                                                    Use only text present in the input data.
                                                    Preserve all tables and lists exactly (table rows remain rows; list items remain list items).
                                                    Don't make up values.
                                                    Replace spaces with single spaces.

                                                    **IMPORTANT: Your entire response MUST be a single, valid JSON object. Do NOT wrap it in Markdown code fences (```json ... ```). Do NOT add any text before or after the JSON. The JSON must be complete (not truncated).**

                                                    Recommended blocks (use if present in the input data):

                                                    Наименование компании: 
                                                    When extracting or generating the company name, follow these rules strictly:
                                                    • The company name must consist only of:
                                                        - the legal name,
                                                        - the organizational and legal form,
                                                        - the country/region (for foreign companies only).
                     
                                                    • Do not invent an organizational and legal form if it is missing in the source text.
                                                        Example: Московский насосный завод №1, ЗАО
                                                                Еврохим-Пенза, ООО
                                                                Завод Водоприбор, ОАО
                                                                Завод Знамя труда, АО
                     
                                                    • For foreign companies (including Ukraine and Belarus), append the country/region after the legal form, separated by a comma.                                                        
                                                            Example: Керамин, ОАО, Беларусь
                                                                Ceramica La Escandella, Испания
                                                                M.H. KOREA LTD, Корея

                                                    • If the raw name contains any of the following organizational descriptors, then they must be converted to abbreviations only:
                                                        • Группа Компаний = ГК, 
                                                        • Производственный Комплекс = ПК,
                                                        • Производственная Компания = ПК,
                                                        • Промышленная группа = ПГ,
                                                        • Торговый Дом = ТД, 
                                                        • Научно Производственное Предприятие = НПП.
                                                    These abbreviations are placed after the normalized company name and before the legal form.
                                                    Only these abbreviations are allowed.  
                                                    Do not create new abbreviations.
                                                    Do not expand abbreviations into full descriptive names.  
                                                        Example: ООО «Группа Компаний Демидов» = Демидов ГК, ООО
                                                            ООО «Производственная компания ДОНТЭС» = Донтэс ПК, ООО
                                                            Общество с ограниченной ответственностью «Торговый дом «Евротрейдинг» = Евротрейдинг ТД, ООО
                                                            ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ НАУЧНО-ПРОИЗВОДСТВЕННОЕ ПРЕДПРИЯТИЕ "ЗАВОД СТЕКЛОПЛАСТИКОВЫХ ТРУБ" = Завод стеклопластиковых труб НПП, ООО   
                                                    
                                                    Strict prohibition:  
                                                        The company name must not contain descriptions, explanations, product types, activities, or marketing phrases.
                                                        Only the legal name + legal form (+ country if foreign).
                                                        If the raw text contains a long descriptive name (e.g., “Завод по производству…”), but the legal name is short (e.g., “АО «Агроскон‑ЖБИ»”), use only the legal name.
                                                            Example: 
                                                            INCORRECT: АО «Агроскон‑ЖБИ» — Завод по производству железобетонных изделий и конструкций
                                                            CORRECT: Агроскон‑ЖБИ, АО
                     
                                                    ID производителя = company_id  
                                                    **Extract this value from a field named exactly "company_id" or "ID производителя" in the input. If no such field exists, set to an empty string ("").**

                                                    Представительство в РФ:
                                                    Only two values are possible: "Отсутствует" or "Присутствует".
                                                        Geographic filter:
                                                        - If the company's address is located within the Russian Federation (Russia), assign the value "Присутствует".
                                                        - If the company's address is located outside the Russian Federation (Russia), assign the value "Отсутствует".
                                                        **Note: Use any address field (legal, actual, postal) that belongs to the primary manufacturer. If at least one address is inside Russia → "Присутствует".**

                                                    Адрес: address1;
                                                        address2; etc.
                                                        Address format: Postcode (if any), Country, Region (if any), City, Street, Building, apartment, office, etc. Take information only from the incoming text, don't make anything up.
                                                        If the postal code is not specified, write the address in the following format: Country, Region (if any), City, Street, Building, apartment, office, etc.

                                                    Телефон: phone1;
                                                            phone2;
                                                            etc.

                                                    **PHONE NUMBER RULES (strictly follow this algorithm):**
                                                        1. Extract all telephone numbers from the input (including those inside department blocks).
                                                        2. For each number, keep only digits and the '+' character. Remove all spaces, dashes, parentheses, and other symbols.
                                                        3. If the number starts with '8' (Russian domestic format), replace that '8' with '+7'.
                                                        4. If the number has no country code and is 10-11 digits long, assume Russian and add '+7' at the beginning.
                                                        5. Format the result as: `+7` space `<area/operator code>` space `<subscriber number>`.
                                                        - The area/operator code consists of the next 3–5 digits (use the original grouping if possible, otherwise take 3 digits for Moscow/SPB, 5 for other regions).
                                                        - The remaining subscriber digits must be split into groups of 2 or 3 digits, separated by single spaces. Do not use any other separators.
                                                        - Example: `+7 812 677 07 91` (812 – area code, 677 07 91 – subscriber)
                                                        - Example: `+7 42622 710 05` (42622 – area code, 710 05 – subscriber)
                                                        6. If a phone number belongs to a department, prepend the department name and a colon: `"Отдел продаж: +7 495 123 45 67"`.
                                                        7. **IMPORTANT: Never truncate phone numbers. Always preserve all digits.**

                                                    WHAT TO INCLUDE vs EXCLUDE (CRITICAL — applies to BOTH phones AND e-mails):
                                                        INCLUDE only contacts of the COMPANY as a whole or of its IMPERSONAL units:
                                                        general/reception numbers, department numbers (отдел продаж, отдел сбыта, бухгалтерия,
                                                        снабжение, сервис), warehouses (склад), branches / representative offices by city,
                                                        and functional e-mails (info@, sales@, zakaz@, office@, trade@ ...).
                                                        **STRUCTURAL DIVISIONS (требование заказчика, парное к правилу D48 промпта дистрибьюторов):
                                                        internal sales divisions/directions of the manufacturer («дивизион», «Экспорт», «Центр»,
                                                        «Восток», «Юг», региональные управления сбыта, представительства САМОГО производителя)
                                                        are NOT suppliers — their addresses/phones/e-mails MUST be included HERE, in the company
                                                        info, labeled with the unit name: "Дивизион Центр: +7 ..." / address entries prefixed
                                                        the same way.**
                                                        STRICTLY FORBIDDEN — NEVER extract a phone or e-mail that belongs to a SPECIFIC PERSON.
                                                        A contact is PERSONAL (and must be omitted ENTIRELY — number, extension and e-mail) if it
                                                        is placed next to a person's name (any of Фамилия/Имя/Отчество, e.g. "Иванов И.И.",
                                                        "Людмила Юрьевна Дохолян") and/or an individual job title (менеджер, специалист,
                                                        руководитель, директор по ..., заместитель директора, начальник отдела, инженер, оператор).
                                                        This includes extension numbers ("доб. 1207") that reach that specific person.
                                                        Rule of thumb: if, after removing the person's name/title, a generic unit remains
                                                        (e.g. "Отдел продаж", a city office) — keep the UNIT's number; if the contact is reachable
                                                        only via the named person — DROP it completely. When unsure whether a contact is personal
                                                        or belongs to the company, OMIT it.

                                                        EXCLUDE example (personal — DO NOT extract anything from this block):
                                                            Людмила Юрьевна Дохолян
                                                            Менеджер по продажам
                                                            8 (4722) 749-375, доб. 1207
                                                            Белгород
                                                        Another EXCLUDE example: Михайлова Вероника / +7 958 523-72-87 / mv@alfapol.ru
                                                        INCLUDE example (company / city branch — extract):
                                                            Белгород
                                                            Адрес: ул. Макаренко, д. 29
                                                            Телефон: +7 930 060 62 45, +7 4722 749 375
                                                            Электронная почта: zakaz@aerobel.ru

                                                    E-mail: email1; 
                                                            email2;
                                                            etc.
                                                    Website: main website, if any
                                                    Описание: company description.
                                                    ТРЕТЬЕ ЛИЦО (обезличивание) — обязательно для поля «Описание»: убери речь от
                                                    первого лица и самоназвания продавца/магазина/сайта, заменяя их на нейтральное
                                                    «производитель» (или на имя компании) с сохранением смысла и грамматики (род,
                                                    число, падеж):
                                                    - «мы», «мы предлагаем/производим/выпускаем/гарантируем» → «производитель
                                                      предлагает/производит/…»;
                                                    - «наш/наша/наше/наши» («наш магазин», «наш интернет-магазин», «на нашем сайте»,
                                                      «наши специалисты») → «производитель» / «у производителя» / «специалисты
                                                      производителя»;
                                                    - «у нас», «обращайтесь к нам» → «у производителя», либо удали, если это чистый
                                                      рекламный призыв без фактов.
                                                    НЕ вводи второе лицо («вы», «вам») и не выдумывай фактов. Чисто рекламные фразы
                                                    удали. Применяй по смыслу, а не только к дословным «мы»/«наш».
                                                    Реквизиты: ALL company details available on the page.
                                                        For example:                                                
                                                        Полное наименование предприятие	Общество с ограниченной ответственностью Производственная фирма «Челнинский арматурный завод»
                                                        Сокращенное наименование предприятия	ООО ПФ «Челнинский арматурный завод»
                                                        Юридический адрес	423800, Республика Татарстан,
                                                        г. Набережные Челны, ул. Шлюзовая, зд. 30
                                                        Почтовый адрес	423800, Республика Татарстан,
                                                        г. Набережные Челны, ул. Шлюзовая, зд. 30
                                                        
                                                        ИНН	1650086286
                                                        ОГРН	1021602017381
                                                        ОКПО	57248197
                                                        ОКТМО	92730000
                                                        ОКВЭД	29.13, 28.52, 51.54
                                                        Платежные реквизиты:
                                                        Р/сч. 40702810100220000142
                                                        К/сч.30101810145250000411
                                                        Филиал «Центральный » Банк ВТБ (ПАО) г. Москва 
                                                        ИНН 7702070139, КПП 263443001,  БИК 044525411

                                                        Отгрузочные реквизиты:
                                                        Контейнерная отгрузка:
                                                        Станция Владикавказ Северо-Кавказской железной дороги
                                                        Код станции: 538708, код предприятия 1340

                                                        Директор	Гришин Александр Владимирович
                                                        Ж/д реквизиты	Ст.Нижнекамск, код.ст. 647800 КБШ код пр.7122, грузополучатель ООО ПФ «Челнинский арматурный завод`

                                                    RULES FOR CONTACT BLOCKS WITH DEPARTMENT NAMES

                                                        * If contact information is located in a block with a name
                                                        (for example: Главный офис, Розничный отдел, Склад, Нефтегазовая отрасль, Транспортное строительство, Отдел розничных продаж),
                                                        you MUST include contact information for ALL departments and employees present in the input data.
                                                        This description must be written for all contact information in that block, including:
                                                            - addresses,
                                                            - phone numbers,
                                                            - emails.

                                                        * Each entry must preserve the department name in the format:
                                                        "Department name: value"
                                                        * If a department name is missing, creating one is prohibited.
                                                        * If a department has multiple phone numbers or emails, list them separated by commas.
                                                        Example:
                                                        Raw data:
                                                        Почтовый и фактический адрес:
                                                        Россия, 624223, Свердловская область, г. Нижняя Тура, ул. Малышева, 59

                                                        #### Нефтегазовая отрасль
                                                        +7 (343) 385-80-88,
                                                        
                                                        [peskov@fmp.ru](mailto:peskov@fmp.ru)
                                                        [ng@fmp.ru](mailto:ng@fmp.ru) 

                                                        #### Транспортное строительство
                                                        +7 (812) 640-55-16,
                                                        +7 (495) 411-99-11
                                                        
                                                        [kanev@fmp.ru](mailto:kanev@fmp.ru)

                                                        Отдел розничных продаж
                                                        8 (962) 312-88-40. Адрес: Свердловская обл., г. Нижняя Тура, ул. Говорова, 9.
                                                        srt@tizol.ru
                                                        etc.
                                                        Result:
                                                        {
                                                            "company": {
                                                                "Наименование компании": "ТИЗОЛ, АО",
                                                                "ID производителя": "",
                                                                "Представительство в РФ": "Присутствует",
                                                                "Адрес": [
                                                                    "Почтовый и фактический адрес: Россия, 624223, Свердловская область, г. Нижняя Тура, ул. Малышева, 59",
                                                                    "Отдел розничных продаж: Россия, Свердловская обл., г. Нижняя Тура, ул. Говорова, 9"
                                                                ],
                                                                "Телефон": [
                                                                    "Приемная: +7 8172 27 52 67",
                                                                    "Нефтегазовая отрасль: +7 343 385 80 88",
                                                                    "Транспортное строительство: +7 812 640 55 16, +7 495 411 99 11"
                                                                    "Отдел розничных продаж: +7 962 312 88 40",
                                                                ],
                                                                "E-mail": [
                                                                    "Приемная: sekretar@tizol.ru",
                                                                    "Нефтегазовая отрасль: peskov@fmp.ru, ng@fmp.ru"
                                                                    "Транспортное строительство: kanev@fmp.ru"
                                                                    "Отдел розничных продаж: srt@tizol.ru",
                                                                ],
                                                                "Website": "",
                                                                "Описание": "",
                                                                "Реквизиты": {
                                                                    "ИНН": "",
                                                                    "ОГРН": "",
                                                                    "КПП": "",
                                                                }
                                                            }
                                                        }

                                                    PROCESSING RULES:
                                                    1. Separate primary company information from suppliers/branches/dealers/regional office information:
                                                    Information contained in the «Официальные дилеры», «Наши представительства», «Представительство в ...», «Представитель в ...», «Филиалы», «Региональный представитель», «Склады и хранилища» etc. blocks should NOT be considered company information and you should NOT reflect it in the final structured data.
                                                    2. Keep ALL information about the primary manufacturer, but organize it logically.
                                                    3. For addresses: group by city/region as they appear; preserve the original order if multiple addresses.
                                                    4. For contacts: **remove duplicate phone numbers and email addresses across the entire company. If an identical phone number appears in two different departments, keep only the first occurrence (the one with the department name that appears earlier in the text).**
                                                    5. **Ensure that the output JSON is syntactically valid: escape double quotes inside strings with backslash (`\`), escape backslashes, and never truncate the output. If the input data is very long, still produce a complete JSON.**

                                                    **FORBIDDEN:**
                                                    - Do not wrap the JSON in Markdown code blocks (```json ... ```).
                                                    - Do not add any extra text, explanations, or comments before or after the JSON.
                                                    - Do not abbreviate or shorten any extracted values.
                                                    - Do not invent data that is not present in the input.

                                                    Output format (valid JSON only – copy this structure exactly, filling only the keys that exist in input):
                                                    {
                                                        "company": {
                                                            "Наименование компании": "",
                                                            "ID производителя": "",
                                                            "Представительство в РФ": "",
                                                            "Адрес": [],
                                                            "Телефон": [],
                                                            "E-mail": [],
                                                            "Website": "",
                                                            "Описание": "",
                                                            "Реквизиты": {
                                                                "ИНН": "",
                                                                "ОГРН": "",
                                                                "КПП": "",
                                                                etc.
                                                            }
                                                        }
                                                    }
                        """},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "reasoning_effort": _effort_for_model(self.model),
                "response_format": {"type": "json_object"}
            }

            result, token_usage = await self._make_request_with_retry(data)

            if result:
                content = result['choices'][0]['message']['content']
                try:
                    # Устойчивый разбор (gemini подмешивает <think> в content)
                    company_data = _robust_json_extract(content)
                    if company_data is None:
                        raise json.JSONDecodeError("robust parse failed", content or "", 0)
                    return company_data, token_usage
                except json.JSONDecodeError as e:
                    log.error(f"Ошибка парсинга JSON для компании: {e}")
                    return {
                        "error": "JSONDecodeError",
                        "raw_content": content[:500] if content else "Empty response"
                    }, token_usage
            else:
                return None, token_usage
                
        except Exception as e:
            log.error(f"Ошибка извлечения информации о компании (combined): {e}")
            return None, {'input_tokens': 0, 'output_tokens': 0}
        
        # ai_integration.py (дополнения)

    async def get_embedding(self, text: str, model: str = "text-embedding-3-small") -> Optional[List[float]]:
        """
        Получение эмбеддинга текста через API AITunnel.
        """
        endpoint = self.api_url.replace('/chat/completions', '/embeddings')
        data = {
            "model": model,
            "input": text
        }
        for attempt in range(self.max_retries + 1):
            try:
                async with self.semaphore:
                    if self.rate_limiter:
                        await self.rate_limiter.acquire()
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(endpoint, headers=self.headers, json=data) as response:
                            if response.status == 200:
                                result = await response.json()
                                embedding = result['data'][0]['embedding']
                                return embedding
                            elif response.status in self.retry_status_codes and attempt < self.max_retries:
                                delay = self._calculate_exponential_delay(attempt)
                                log.warning(f"Ошибка эмбеддинга {response.status}, повтор через {delay}с")
                                await asyncio.sleep(delay)
                                continue
                            else:
                                log.error(f"Ошибка получения эмбеддинга: {response.status}")
                                return None
            except Exception as e:
                if attempt < self.max_retries:
                    delay = self._calculate_exponential_delay(attempt)
                    log.warning(f"Исключение при получении эмбеддинга: {e}, повтор через {delay}с")
                    await asyncio.sleep(delay)
                else:
                    log.error(f"Не удалось получить эмбеддинг после {self.max_retries} попыток: {e}")
                    return None
        return None

    async def chat_completion(self, system_prompt: str, user_prompt: str, 
                               model: Optional[str] = None,
                               temperature: float = 0.2,
                               response_format: Optional[dict] = None) -> Tuple[Optional[str], Dict[str, int]]:
        """
        Универсальный метод для чат-завершений.
        Возвращает (content, token_usage).
        """
        model = model or self.model
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        data = {
            "model": model,
            "messages": messages,
            "temperature": temperature
        }
        if response_format:
            data["response_format"] = response_format
        
        result, token_usage = await self._make_request_with_retry(data)
        if result:
            content = result['choices'][0]['message']['content']
            return content, token_usage
        return None, token_usage