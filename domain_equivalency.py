# domain_equivalency.py 1.0.0
import logging
import re
from collections import namedtuple
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

# idna даёт перевод IDN-хоста в A-label (крышев.рф -> xn--b1afoy4br.xn--p1ai).
# Без него остаётся встроенный кодек 'idna' (IDNA2003), а если и он не справился —
# хост возвращается как есть (fail-open).
try:
    import idna as _idna
except Exception:
    _idna = None

log = logging.getLogger("domain_equiv")

# Ответ одной HTTP-пробы кандидата: status/body/final_url либо описание ошибки.
HttpAnswer = namedtuple('HttpAnswer', 'status body final_url error tls_error')
# Вердикт по кандидату: подтверждён ли, какой URL брать, почему отказ, был ли вообще ответ.
CandidateProbe = namedtuple('CandidateProbe', 'ok url reason tls_error responded site_status')

_A_HREF_RE = re.compile(r'<a\s[^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)
# Мобильные зеркала: m.<домен> и т.п. — тот же сайт, но базой обхода должна оставаться
# десктопная форма (D272: на мобильном зеркале canonical и карта сайта ведут на апекс).
_MOBILE_PREFIXES = ('m.', 'mobile.', 'touch.')
_META_REFRESH_RE = re.compile(r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]*>', re.I)
_META_REFRESH_URL_RE = re.compile(r'url\s*=\s*["\']?([^"\'>;\s]+)', re.I)
_SCRIPT_RE = re.compile(r'<script\b[^>]*>(.*?)</script>', re.I | re.S)
_JS_REDIRECT_RE = re.compile(r'location\s*(?:\.\s*(?:replace|assign)\s*\(|\.\s*href\s*=|\s*=)', re.I)


def to_ascii_host(host: str) -> str:
    """A-label формы хоста: 'крышев.рф' -> 'xn--b1afoy4br.xn--p1ai' (D180).

    Сравнение доменов чисто строковое, поэтому у IDN-сайта, где Site_list содержит
    punycode, а разметка — кириллицу (или наоборот), каждая абсолютная внутренняя ссылка
    выглядит чужим хостом. Fail-open: ASCII-хост и любая ошибка перевода возвращают
    исходное значение."""
    if not host or host.isascii():
        return host
    if _idna is not None:
        try:
            return _idna.encode(host, uts46=True).decode('ascii')
        except Exception:
            pass
    try:
        return host.encode('idna').decode('ascii')
    except Exception:
        return host


def strip_www(host: str) -> str:
    """Срезает ТОЛЬКО ведущий www. (наивный replace('www.','') резал подстроку в любом
    месте хоста: nowww.ru -> noru)."""
    host = to_ascii_host((host or '').strip().lower().split(':')[0])
    return host[4:] if host.startswith('www.') else host


def desktop_host(host: str) -> str:
    """Десктопная форма хоста: срезает мобильный префикс m./mobile./touch."""
    host = strip_www(host)
    for prefix in _MOBILE_PREFIXES:
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def is_mobile_mirror(host: str, base_host: str) -> bool:
    """host — мобильное зеркало base_host (m.rmz.by при базе rmz.by)."""
    host, base_host = strip_www(host), strip_www(base_host)
    return host != base_host and desktop_host(host) == desktop_host(base_host)


def hosts_equivalent(host1: str, host2: str) -> bool:
    """Хосты одного сайта: апекс, www-форма и мобильное зеркало."""
    return desktop_host(host1) == desktop_host(host2) and bool(desktop_host(host1))


def registrable_domain(host: str) -> str:
    """Регистрируемый домен: два последних уровня (example.ru у shop.example.ru).
    Публичные суффиксы второго уровня (com.ru и т.п.) не разбираются — для списка
    компаний (.ru/.by/.com) этого достаточно."""
    parts = [p for p in strip_www(host).split('.') if p]
    return '.'.join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else '')


def same_registrable_domain(url1: str, url2: str) -> bool:
    """Оба адреса принадлежат одному registrable-домену (www/поддомен/мобильное зеркало)."""
    domain = registrable_domain(urlparse(url1 or '').netloc)
    return bool(domain) and domain == registrable_domain(urlparse(url2 or '').netloc)


def redirect_shim_target(html: str, max_len: int) -> Optional[str]:
    """Редирект-шим: короткое тело, вся задача которого — увести на другой адрес
    (<meta http-equiv="Refresh"> либо единственный <script> с location=/location.replace/
    setTimeout). Возвращает адрес назначения ('' если он не распознан) либо None, если
    это не шим (D232 parkgroup.ru, D238 tormax.ru)."""
    body = html or ''
    if len(body) > max_len:
        return None
    meta = _META_REFRESH_RE.search(body)
    if meta:
        target = _META_REFRESH_URL_RE.search(meta.group(0))
        return target.group(1) if target else ''
    scripts = _SCRIPT_RE.findall(body)
    if len(scripts) == 1 and _JS_REDIRECT_RE.search(scripts[0]):
        target = re.search(r'["\'](https?://[^"\']+)["\']', scripts[0])
        return target.group(1) if target else ''
    return None


def normalize_site_url(raw) -> str:
    """Нормализация адреса компании из Site_list (D147).

    Нет схемы -> подставляем https://; при пустом netloc весь текст считается хостом.
    Возвращает '' если хоста нет и после нормализации — краулить такой адрес нельзя."""
    text = str(raw or '').strip()
    if not text:
        return ''
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', text):
        text = 'https://' + text.lstrip('/')
    parsed = urlparse(text)
    if not parsed.netloc or '.' not in parsed.netloc or ' ' in parsed.netloc:
        return ''
    return text


def _is_tls_error_text(text: str) -> bool:
    """Отказ curl_cffi именно по сертификату (а не по отказу соединения)."""
    low = (text or '').lower()
    if 'certificate' in low:
        return True
    return 'ssl' in low and 'failed to connect' not in low and 'could not connect' not in low


def _has_internal_links(html: str, host: str) -> bool:
    """Есть ли в теле ссылки навигации на собственный хост (или относительные).

    У заглушки хостера все <a href> ведут на сайт хостера, а у дефолтного vhost
    («Welcome!», 8 байт) ссылок нет вовсе — это и отличает их от живого сайта."""
    for href in _A_HREF_RE.findall(html or ''):
        href = href.strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:', 'data:')):
            continue
        netloc = urlparse(href).netloc
        if not netloc or hosts_equivalent(netloc, host):
            return True
    return False


class DomainEquivalencyManager:
    """Менеджер для определения эквивалентности доменов и нормализации URL"""
    
    def __init__(self, config):
        self.config = config
        # Машинный статус последнего выбора базы обхода (hoster_stub / http_only / no_host …);
        # краулер кладёт его в stats['site_status'].
        self.last_site_status = None
        # Пер-сайтовый рычаг crawl.strict_www из профиля текущей компании (P01 U3 п.6);
        # None = действует только config.strict_www_domains.
        self.profile_strict_www = None
        # P05 U3: пер-сайтовый периметр из профиля — домен профиля и его
        # crawl.equivalent_domains (зеркала, бренд-домены группы) плюс суффиксы
        # crawl.subdomain_collapse ('*.gexa.ru'). Пусто = только config.equivalent_domains.
        self.profile_domains = set()
        self.profile_suffixes = ()

    def normalize_domain(self, domain: str) -> str:
        """Нормализация домена к каноническому виду"""
        if not domain:
            return domain
            
        # Удаляем порт если есть
        if ':' in domain:
            domain = domain.split(':')[0]
            
        # Приводим к нижнему регистру и к A-label (D180: 'крышев.рф' и
        # 'xn--b1afoy4br.xn--p1ai' — один домен)
        domain = to_ascii_host(domain.lower())

        # Домены, отдающие контент только на www (non-www → 404): канонизируем К www,
        # а не срезаем его, иначе краулер резолвит найденные ссылки в non-www и получает 404.
        bare = domain[4:] if domain.startswith('www.') else domain
        strict_www = {
            (d[4:] if d.lower().startswith('www.') else d).lower()
            for d in (getattr(self.config, 'strict_www_domains', None) or [])
        }
        if self.profile_strict_www:
            strict_www.add(self.profile_strict_www)
        if bare in strict_www:
            return 'www.' + bare

        # Обработка www (если включено в настройках)
        if self.config.domain_strict_www:
            if domain.startswith('www.'):
                domain = domain[4:]

        return domain
        
    def set_profile_strict_www(self, domain: Optional[str]) -> None:
        """P01 U3 п.6: профильное поле crawl.strict_www. Домен из профиля отдаёт контент
        только на www — канонизируем к www без правки config (профиль прошлой компании не
        протекает: краулер сбрасывает значение в None на каждой компании)."""
        self.profile_strict_www = strip_www(domain) if domain else None

    def set_profile_domains(self, domain: Optional[str], equivalent_domains=None,
                            subdomain_collapse=None) -> None:
        """P05 U3 п.1: периметр обхода из профиля сайта. До сих пор crawl.equivalent_domains
        и crawl.subdomain_collapse влияли только на выбор профиля (resolver.py), а гейт
        периметра читал исключительно config.equivalent_domains. Сбрасывается на каждой
        компании (профиль прошлой не протекает)."""
        domains = {self.normalize_domain(domain)} if domain else set()
        for alias in (equivalent_domains or []):
            if alias:
                domains.add(self.normalize_domain(alias))
        suffixes = []
        for pattern in (subdomain_collapse or []):
            suffix = str(pattern or '').lstrip('*').lower()
            if suffix.startswith('.') and len(suffix) > 1:
                suffixes.append('.' + self.normalize_domain(suffix[1:]))
        self.profile_domains = domains
        self.profile_suffixes = tuple(suffixes)

    def _in_profile_scope(self, domain: str) -> bool:
        """Хост входит в пер-сайтовый периметр профиля: домен профиля или его алиас
        (crawl.equivalent_domains) либо суффикс crawl.subdomain_collapse."""
        if not domain:
            return False
        if domain in self.profile_domains:
            return True
        return any(domain.endswith(suffix) for suffix in self.profile_suffixes)

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
        if normalized in self.profile_domains:
            # P05 U3: allowlist профиля (зеркало хостера, бренд-домены группы) —
            # такая же группа, как config.equivalent_domains, но пер-сайтовая
            return set(self.profile_domains)
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

            # Сравниваем канонические домены. hosts_equivalent добавляет мобильное
            # зеркало (m./mobile./touch.) — тот же сайт, что и апекс (P05 U3 п.2, D272).
            if domain1 == domain2 or hosts_equivalent(domain1, domain2):
                return True

            # P05 U3: пер-сайтовый периметр из профиля (crawl.equivalent_domains,
            # crawl.subdomain_collapse) — каталог на поддомене/бренд-домене группы
            if self._in_profile_scope(domain1) and self._in_profile_scope(domain2):
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
        Поиск рабочего URL компании.

        - адрес из Site_list нормализуется (нет схемы -> https://), путь сохраняется;
        - кандидаты: «схема + хост как в Site_list» первой парой, затем остальные
          комбинации схема x www;
        - кандидат подтверждается ТОЛЬКО ответом 200 с содержательным телом без маркеров
          заглушки хостера; 206 на наш Range перепроверяется запросом без Range;
        - конечный URL после редиректов принимается только при эквивалентном хосте (апекс/www;
          мобильное зеркало m./mobile./touch. схлопывается к десктопной форме), смена хоста —
          в INFO, база обхода остаётся на домене компании;
        - сначала все кандидаты с проверкой TLS; отпавшие ИМЕННО по сертификату
          перепроверяются вторым проходом без проверки TLS (с пометкой «TLS-имя не совпало»),
          чтобы сайт с истёкшим сертификатом на своём домене не потерялся, но и не перебивал
          кандидата с валидным TLS;
        - фолбэк — адрес из Site_list (схема, www и путь как в файле);
        - результат каждой пробы пишется в INFO, таймаут и отказ соединения различаются.
        """
        self.last_site_status = None
        if not self.config.treat_http_https_as_same:
            return url

        original_url = normalize_site_url(url)
        if not original_url:
            log.error(f"Адрес компании {url!r} не содержит хоста — рабочий URL не определён")
            self.last_site_status = 'no_host'
            return url

        candidates = self._build_candidates(original_url)
        log.info(f"Проверка рабочих URL для {url}, кандидаты: {candidates}")

        tls_failed = []   # кандидаты, отпавшие по несовпадению TLS-имени
        weak_url = None   # ответил 200, но не прошёл гейт содержательности
        status = None

        for variant in candidates:
            probe = await self._probe_candidate(variant, verify_tls=True)
            if probe.ok:
                return self._accept_candidate(probe.url, variant, original_url)
            if probe.tls_error:
                tls_failed.append(variant)
            if probe.site_status and status is None:
                status = probe.site_status
            if probe.responded and weak_url is None:
                weak_url = variant

        for variant in tls_failed:
            probe = await self._probe_candidate(variant, verify_tls=False)
            if probe.ok:
                log.warning(f"Кандидат {variant} подтверждён без проверки TLS (TLS-имя не совпало)")
                return self._accept_candidate(probe.url, variant, original_url)
            if probe.site_status and status is None:
                status = probe.site_status
            if probe.responded and weak_url is None:
                weak_url = variant

        self.last_site_status = status
        if weak_url and weak_url != candidates[0]:
            log.warning(f"Ни один кандидат не подтверждён содержимым для {url}; "
                        f"берём единственный ответивший {weak_url}")
            return weak_url
        log.warning(f"Не найден рабочий URL для {url}, откат на адрес из Site_list: {original_url}")
        return original_url

    async def fetch_page_body(self, url: str) -> str:
        """Одиночная проба произвольного адреса: тело ответа 200 либо пустая строка.
        Нужна автодетекту зеркала (P05 U3): подтвердить, что внешний хост отдаёт тот же
        сайт, можно только заглянув на его главную."""
        answer = await self._probe_http(url, verify_tls=True, use_range=False)
        if answer.status is None and answer.tls_error:
            answer = await self._probe_http(url, verify_tls=False, use_range=False)
        return answer.body if answer.status == 200 else ''

    def _build_candidates(self, original_url: str) -> list:
        """Кандидаты из ИСХОДНОГО адреса: путь сохраняется (/ru/, /ru_RU/…), первой идёт
        пара «схема из Site_list + хост из Site_list» (D140, D148)."""
        parsed = urlparse(original_url)
        host = parsed.netloc.lower()
        path = parsed.path if parsed.path not in ('', '/') else ''
        other_host = host[4:] if host.startswith('www.') else 'www.' + host
        other_scheme = 'http' if parsed.scheme == 'https' else 'https'
        candidates = []
        for candidate_host in (host, other_host):
            for scheme in (parsed.scheme, other_scheme):
                variant = f"{scheme}://{candidate_host}{path}"
                if variant not in candidates:
                    candidates.append(variant)
        return candidates

    def _accept_candidate(self, final_url: str, variant: str, original_url: str) -> str:
        """Подтверждённый кандидат: что вернуть краулеру и какой статус запомнить."""
        chosen = final_url or variant
        if urlparse(chosen).scheme == 'http' and urlparse(original_url).scheme == 'https':
            # Сайт живёт только по http, хотя в Site_list https (D241, D258)
            self.last_site_status = 'http_only'
        log.info(f"Найден рабочий URL: {chosen} (кандидат {variant})")
        return chosen

    async def _probe_candidate(self, variant: str, verify_tls: bool) -> CandidateProbe:
        """Проба кандидата с гейтами подтверждения: только 200 и содержательное тело."""
        tls_note = '' if verify_tls else ' [без проверки TLS]'
        answer = await self._probe_http(variant, verify_tls, use_range=True)
        if answer.status == 206:
            # 206 на наш Range ничего не говорит о сайте: дефолтный vhost хостера отдаёт
            # 1 байт и выглядит «рабочим» (D114, D241). Перепроверяем запросом без Range.
            log.info(f"Кандидат {variant}: ответ 206 на Range — повторяем пробу без Range")
            answer = await self._probe_http(variant, verify_tls, use_range=False)

        if answer.status is None:
            log.info(f"Кандидат {variant}: проба не удалась ({answer.error}){tls_note}")
            return CandidateProbe(False, variant, answer.error, answer.tls_error, False, None)
        if answer.status != 200:
            log.info(f"Кандидат {variant}: статус {answer.status} — не подтверждён{tls_note}")
            return CandidateProbe(False, variant, f'статус {answer.status}', False, True, None)

        requested_host = urlparse(variant).netloc
        final_host = urlparse(answer.final_url or variant).netloc
        if final_host and not hosts_equivalent(final_host, requested_host):
            # Редирект увёл на чужой хост: парковка хостера или сайт-редиректор. Базой обхода
            # такой адрес не становится, обход остаётся на домене компании (D167).
            site_status = self._foreign_host_status(answer.final_url)
            log.info(f"Кандидат {variant}: редирект сменил хост на {final_host} "
                     f"({site_status}) — кандидат не подтверждён{tls_note}")
            return CandidateProbe(False, variant, f'редирект на чужой хост {final_host}',
                                  False, True, site_status)

        reason, site_status = self._content_verdict(answer.body, variant)
        if reason:
            log.info(f"Кандидат {variant}: 200, но {reason} — не подтверждён{tls_note}")
            return CandidateProbe(False, variant, reason, False, True, site_status)

        chosen = answer.final_url or variant
        if final_host and is_mobile_mirror(final_host, requested_host):
            # UA-зависимый 301 на мобильное зеркало: контент тот же, но у зеркала canonical и
            # карта сайта ведут на апекс — базой обхода оставляем десктопную форму (D272).
            log.info(f"Кандидат {variant}: сайт увёл пробу на мобильное зеркало {final_host} — "
                     f"базой обхода остаётся {variant}")
            chosen = variant

        log.info(f"Кандидат {variant}: 200, тело {len(answer.body)} симв. — подтверждён{tls_note}")
        return CandidateProbe(True, chosen, '', False, True, None)

    def _foreign_host_status(self, final_url: str) -> str:
        """Куда увёл редирект: припаркованный домен (хост хостера или путь /parking) или
        просто чужой хост."""
        parsed = urlparse(final_url or '')
        host = strip_www(parsed.netloc)
        if '/parking' in (parsed.path or '').lower():
            return 'parked_domain'
        for hoster in (getattr(self.config, 'parking_hoster_domains', None) or []):
            hoster = strip_www(hoster)
            if hoster and (host == hoster or host.endswith('.' + hoster)):
                return 'parked_domain'
        return 'foreign_host'

    def _content_verdict(self, body: str, variant: str):
        """Гейт содержательности кандидата. Возвращает (причина отказа, машинный статус);
        пустая причина = кандидат рабочий."""
        marker = self._hoster_stub_marker(body)
        if marker:
            return f'тело — заглушка/панель хостера (маркер «{marker}»)', 'hoster_stub'
        shim_target = redirect_shim_target(
            body, getattr(self.config, 'working_url_shim_max_len', 2000))
        if shim_target is not None:
            # Цель шима на чужом домене автоматически не обходится: нужен актуальный адрес
            # в Site_list (D232, D238).
            return (f'страница является редирект-шимом на {shim_target or "другой адрес"}',
                    'redirect_shim')
        min_content = getattr(self.config, 'working_url_min_content', 300)
        if len(body or '') < min_content:
            return f'тело {len(body or "")} симв. короче порога {min_content}', None
        if not _has_internal_links(body, urlparse(variant).netloc):
            return 'в теле нет ссылок на собственный хост', None
        return '', None

    def _hoster_stub_marker(self, body: str) -> Optional[str]:
        """Первый сработавший маркер заглушки/панели хостера в теле ответа."""
        low = (body or '').lower()
        for marker in (getattr(self.config, 'hoster_stub_markers', None) or []):
            if marker and marker.lower() in low:
                return marker
        return None

    async def _probe_http(self, variant: str, verify_tls: bool, use_range: bool) -> HttpAnswer:
        """Один HTTP-запрос кандидата: сначала браузерный TLS-отпечаток (curl_cffi
        impersonate) — сайты с фильтрацией по TLS-fingerprint режут «голый» aiohttp;
        aiohttp остаётся фолбэком, когда curl_cffi недоступен или не дошёл до ответа."""
        headers = self._probe_headers()
        if use_range:
            headers["Range"] = "bytes=0-0"
        answer = await self._probe_variant_impersonate(variant, headers, verify_tls)
        if answer is not None and (answer.status is not None or answer.tls_error):
            return answer
        if answer is not None:
            log.info(f"Кандидат {variant}: impersonate-проба не дошла до ответа ({answer.error}), "
                     f"пробуем aiohttp")
        return await self._probe_variant_aiohttp(variant, headers, verify_tls)

    def _probe_headers(self) -> dict:
        """Заголовки пробы: без явного русского языка и десктопного клиента сайт уводит
        пробу на англоязычный микросайт (D140) или на мобильное зеркало (D272) —
        curl_cffi impersonate=chrome шлёт свой дефолтный en-US."""
        headers = {'Accept-Language': 'ru-RU,ru;q=0.9', 'sec-ch-ua-mobile': '?0'}
        user_agent = getattr(self.config, 'stealth_user_agent', None)
        if user_agent:
            headers['User-Agent'] = user_agent
        return headers

    async def _probe_variant_impersonate(self, variant: str, headers: dict,
                                         verify_tls: bool) -> Optional[HttpAnswer]:
        """Проба варианта URL браузерным TLS-отпечатком (curl_cffi impersonate).

        None — если curl_cffi недоступен или http_first выключен; тогда отрабатывает
        штатный aiohttp-путь. Условие включения согласовано с краулером (http_first_enabled)."""
        if not (getattr(self.config, 'http_first_enabled', True) and _CURL_CFFI_AVAILABLE):
            return None
        impersonate = getattr(self.config, 'impersonate_profile', 'chrome')
        timeout_total = getattr(self.config, 'working_url_probe_timeout', 20)
        sess = _CurlAsyncSession(impersonate=impersonate, verify=verify_tls, timeout=timeout_total)
        try:
            resp = await sess.get(variant, headers=headers, allow_redirects=True)
            return HttpAnswer(resp.status_code, resp.text or '', str(resp.url), None, False)
        except Exception as e:
            text = f'{type(e).__name__}: {e}'
            if _is_tls_error_text(text):
                return HttpAnswer(None, '', variant, f'TLS-имя не совпало: {text[:160]}', True)
            return HttpAnswer(None, '', variant, text[:160], False)
        finally:
            await sess.close()

    async def _probe_variant_aiohttp(self, variant: str, headers: dict,
                                     verify_tls: bool) -> HttpAnswer:
        """Проба варианта URL через aiohttp. Свежий коннектор на каждый вариант, чтобы
        ошибка TLS на одном не влияла на другие; таймаут и отказ соединения различаются."""
        timeout_total = getattr(self.config, 'working_url_probe_timeout', 20)
        connector = aiohttp.TCPConnector() if verify_tls else aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=timeout_total)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(variant, headers=headers, allow_redirects=True) as response:
                    body = await response.text(errors='replace')
                    return HttpAnswer(response.status, body, str(response.url), None, False)
        except asyncio.TimeoutError:
            return HttpAnswer(None, '', variant, f'таймаут {timeout_total}с', False)
        except aiohttp.ClientSSLError as e:
            return HttpAnswer(None, '', variant, f'TLS-имя не совпало: {e}', True)
        except aiohttp.ClientConnectorError as e:
            return HttpAnswer(None, '', variant, f'отказ соединения: {e}', False)
        except Exception as e:
            return HttpAnswer(None, '', variant, f'{type(e).__name__}: {e}', False)
        finally:
            await connector.close()