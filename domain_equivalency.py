# domain_equivalency.py 1.0.0
import logging
from urllib.parse import urlparse, urlunparse
import aiohttp
import asyncio
from typing import Optional

# curl_cffi даёт HTTP-клиент с браузерным TLS/HTTP2-отпечатком (impersonate).
# Нужен для проверки доступности URL на сайтах с фильтрацией по TLS-fingerprint
# (напр. media-lite): «голый» aiohttp они молча дропают — все кандидаты уходят в
# таймаут, и рабочий www-вариант ошибочно подменяется нерабочим доменом без www.
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
except Exception:
    _CurlAsyncSession = None
    _CURL_CFFI_AVAILABLE = False

log = logging.getLogger("domain_equiv")

class DomainEquivalencyManager:
    """Менеджер для определения эквивалентности доменов и нормализации URL"""
    
    def __init__(self, config):
        self.config = config
        
    def normalize_domain(self, domain: str) -> str:
        """Нормализация домена к каноническому виду"""
        if not domain:
            return domain
            
        # Удаляем порт если есть
        if ':' in domain:
            domain = domain.split(':')[0]
            
        # Приводим к нижнему регистру
        domain = domain.lower()
        
        # Домены, отдающие контент только на www (non-www → 404): канонизируем К www,
        # а не срезаем его, иначе краулер резолвит найденные ссылки в non-www и получает 404.
        bare = domain[4:] if domain.startswith('www.') else domain
        strict_www = {
            (d[4:] if d.lower().startswith('www.') else d).lower()
            for d in (getattr(self.config, 'strict_www_domains', None) or [])
        }
        if bare in strict_www:
            return 'www.' + bare

        # Обработка www (если включено в настройках)
        if self.config.domain_strict_www:
            if domain.startswith('www.'):
                domain = domain[4:]

        return domain
        
    def get_canonical_domain(self, url: str) -> str:
        """Получение канонического домена из URL"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            
            # Нормализуем домен
            return self.normalize_domain(domain)
        except Exception:
            return domain
            
    def get_canonical_url(self, url: str) -> str:
        """Получение канонической версии URL"""
        if not self.config.domain_equivalency_enabled:
            return url
            
        try:
            parsed = urlparse(url)
            
            # Нормализуем домен
            canonical_domain = self.get_canonical_domain(url)
            
            # Определяем схему (предпочтительно https если включено)
            if self.config.treat_http_https_as_same:
                scheme = 'https' if self.config.prefer_https else parsed.scheme
            else:
                scheme = parsed.scheme
            
            # Собираем URL обратно
            return urlunparse((
                scheme,
                canonical_domain,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment
            ))
        except Exception as e:
            log.warning(f"Ошибка получения канонического URL {url}: {e}")
            return url
            
    def _domain_group(self, domain: str) -> set:
        """Множество доменов, эквивалентных данному согласно config.equivalent_domains.

        Ключ группы и все домены из его списка считаются взаимно эквивалентными
        (напр. vmp-holding.ru <-> vmp-anticor.ru / vmp-plamcor.ru / vmp-goodline.ru).
        Возвращает нормализованные домены; если домен ни в одну группу не входит — {domain}."""
        normalized = self.normalize_domain(domain)
        groups = getattr(self.config, 'equivalent_domains', None) or {}
        for primary, related in groups.items():
            members = {self.normalize_domain(primary)}
            members.update(self.normalize_domain(d) for d in related)
            if normalized in members:
                return members
        return {normalized}

    def are_domains_equivalent(self, url1: str, url2: str) -> bool:
        """Проверка, являются ли домены эквивалентными"""
        if not self.config.domain_equivalency_enabled:
            return False

        try:
            domain1 = self.get_canonical_domain(url1)
            domain2 = self.get_canonical_domain(url2)

            # Сравниваем канонические домены
            if domain1 == domain2:
                return True

            # Домены из одной группы config.equivalent_domains тоже эквивалентны:
            # это позволяет краулеру переходить с основного домена на связанные
            # (напр. с vmp-holding.ru на vmp-anticor.ru и обратно)
            return domain2 in self._domain_group(domain1)
        except Exception:
            return False
            
    def get_preferred_scheme(self, url: str) -> str:
        """Получение предпочтительной схемы для URL"""
        if not self.config.treat_http_https_as_same:
            parsed = urlparse(url)
            return parsed.scheme
            
        return 'https' if self.config.prefer_https else 'http'
    
    async def find_working_url(self, url: str) -> Optional[str]:
        """
        Поиск рабочего URL с проверкой доступности:
        - проверяются схемы http и https
        - проверяются домены с www и без www (если исходный содержал www)
        - для каждого варианта создаётся новая сессия и коннектор
        - используется GET с заголовком Range: bytes=0-0 (вместо HEAD)
        - обрабатываются редиректы, возвращается конечный URL
        - таймаут 15 секунд
        """
        if not self.config.treat_http_https_as_same:
            return url

        parsed = urlparse(url)
        original_domain = parsed.netloc
        # Генерируем домены с www и без www
        domain_with_www = original_domain
        domain_without_www = original_domain.replace('www.', '')

        # Собираем все возможные кандидаты (схема + домен)
        candidates = []
        for domain in (domain_with_www, domain_without_www):
            for scheme in ('https', 'http'):
                candidates.append(f"{scheme}://{domain}")
        # Убираем дубликаты, сохраняя порядок появления
        unique_candidates = []
        for c in candidates:
            if c not in unique_candidates:
                unique_candidates.append(c)

        log.info(f"Проверка рабочих URL для {url}, кандидаты: {unique_candidates}")

        # Заголовок для запроса только первого байта (экономит трафик и серверное время)
        headers = {"Range": "bytes=0-0"}
        timeout_total = 15  # секунд

        for variant in unique_candidates:
            # Сначала пробуем браузерный TLS-отпечаток (curl_cffi impersonate): сайты
            # с фильтрацией по TLS-fingerprint режут «голый» aiohttp (таймаут на всех
            # кандидатах), из-за чего рабочий www-вариант не находится и происходит
            # откат на нерабочий домен без www.
            imp_url = await self._probe_variant_impersonate(variant, headers, timeout_total)
            if imp_url:
                log.info(f"Найден рабочий URL (impersonate): {imp_url} (исходный {variant})")
                return imp_url

            # Создаём свежий коннектор и сессию для каждого варианта,
            # чтобы ошибки SSL на одном варианте не влияли на другие
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=timeout_total)
            try:
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(variant, headers=headers, allow_redirects=True) as response:
                        if response.status < 400:
                            final_url = str(response.url)  # итоговый URL после всех редиректов
                            log.info(f"Найден рабочий URL: {final_url} (исходный {variant} -> {final_url}, статус: {response.status})")
                            return final_url
                        else:
                            log.debug(f"Вариант {variant} вернул статус {response.status}")
            except asyncio.TimeoutError:
                log.warning(f"Таймаут {timeout_total}с при проверке {variant}")
            except aiohttp.ClientConnectorError as e:
                log.warning(f"Ошибка соединения {variant}: {e}")
            except aiohttp.ClientSSLError as e:
                log.warning(f"SSL ошибка {variant}: {e}")
            except Exception as e:
                log.warning(f"Неизвестная ошибка при проверке {variant}: {type(e).__name__}: {e}")
            finally:
                # Явно закрываем коннектор, чтобы освободить ресурсы
                await connector.close()

        log.warning(f"Не найден рабочий URL для {url}, используем предпочтительную схему и домен без www")
        # fallback: возвращаем URL с предпочтительной схемой и доменом без www
        preferred_scheme = self.get_preferred_scheme(url)
        return f"{preferred_scheme}://{domain_without_www}"

    async def _probe_variant_impersonate(self, variant: str, headers: dict, timeout_total: int) -> Optional[str]:
        """Проба доступности варианта URL браузерным TLS-отпечатком (curl_cffi impersonate).

        Возвращает конечный URL после редиректов при статусе <400, иначе None. None и при
        отсутствии curl_cffi или при выключенном http_first — тогда отрабатывает штатный
        aiohttp-путь. Условие включения согласовано с краулером (http_first_enabled)."""
        if not (getattr(self.config, 'http_first_enabled', True) and _CURL_CFFI_AVAILABLE):
            return None
        impersonate = getattr(self.config, 'impersonate_profile', 'chrome')
        verify = getattr(self.config, 'http_verify_tls', True)
        sess = _CurlAsyncSession(impersonate=impersonate, verify=verify, timeout=timeout_total)
        try:
            resp = await sess.get(variant, headers=headers, allow_redirects=True)
            if resp.status_code < 400:
                return str(resp.url)
            log.debug(f"impersonate-проба: {variant} вернул статус {resp.status_code}")
        except Exception as e:
            log.debug(f"impersonate-проба не удалась для {variant}: {e}")
        finally:
            await sess.close()
        return None