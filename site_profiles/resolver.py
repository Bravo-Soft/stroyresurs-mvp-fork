"""Загрузка YAML-профилей и резолюция «URL -> SiteProfile».

Правила резолюции (в порядке приоритета):
  1. точное совпадение канонического домена (netloc без www, нижний регистр);
  2. алиас из crawl.equivalent_domains какого-либо профиля;
  3. суффикс из crawl.subdomain_collapse ('*.hms.ru' -> профиль hms.ru).
Нет совпадения (или профили не загружаются) -> SiteProfile.default(domain):
поведение пайплайна не отличается от «до профилирования».
"""
import logging
from pathlib import Path
from urllib.parse import urlparse

from .models import SiteProfile, profile_from_dict

logger = logging.getLogger(__name__)

# Данные профилей лежат рядом с кодом mvp: mvp/profiles/*.yaml (см. design-doc).
DEFAULT_PROFILES_DIR = Path(__file__).resolve().parents[1] / 'profiles'


def _to_ascii_host(host: str) -> str:
    """A-label формы хоста ('крышев.рф' -> 'xn--b1afoy4br.xn--p1ai'), fail-open.

    Дубликат to_ascii_host из domain_equivalency: пакет site_profiles намеренно не
    зависит от модулей краулера (его резолвер тянет и text_extractor)."""
    if not host or host.isascii():
        return host
    try:
        import idna
        return idna.encode(host, uts46=True).decode('ascii')
    except Exception:
        pass
    try:
        return host.encode('idna').decode('ascii')
    except Exception:
        return host


def normalize_domain(url_or_host: str) -> str:
    """Канонический домен: netloc без www, нижний регистр, A-label. Принимает URL или
    голый хост. D180: профиль IDN-сайта должен резолвиться и по кириллической форме
    хоста, и по punycode."""
    if not url_or_host:
        return ''
    s = url_or_host.strip().lower()
    if '://' in s:
        s = urlparse(s).netloc
    else:
        # протокол-относительная форма //host/... либо голый хост / host/path без схемы
        s = s.lstrip('/').split('/', 1)[0]
    if s.startswith('www.'):
        s = s[4:]
    return _to_ascii_host(s.split(':', 1)[0])


class ProfileResolver:
    def __init__(self, profiles_dir=None):
        self.profiles_dir = Path(profiles_dir) if profiles_dir else DEFAULT_PROFILES_DIR
        self._profiles = None       # domain -> SiteProfile
        self._aliases = None        # alias-domain -> SiteProfile
        self._suffixes = None       # list[('.hms.ru', SiteProfile)]
        self.load_errors = {}       # файл -> список ошибок валидации (для диагностики)

    def _ensure_loaded(self):
        if self._profiles is not None:
            return
        self._profiles, self._aliases, self._suffixes = {}, {}, []
        if not self.profiles_dir.is_dir():
            return
        try:
            import yaml
        except ImportError:
            logger.warning('PyYAML не установлен — профили сайтов не загружены, '
                           'используется generic-поведение')
            return
        for path in sorted(self.profiles_dir.glob('*.yaml')):
            if path.name.startswith('_'):
                continue  # _schema.yaml, _clusters.yaml — не профили
            try:
                data = yaml.safe_load(path.read_text(encoding='utf-8'))
            except Exception as e:
                self.load_errors[path.name] = [f'ошибка чтения YAML: {e}']
                logger.warning(f'Профиль {path.name} не прочитан ({e}) — пропущен')
                continue
            profile, errors = profile_from_dict(data)
            if errors:
                self.load_errors[path.name] = errors
                logger.warning(f'Профиль {path.name} невалиден — пропущен: {"; ".join(errors)}')
                continue
            self._profiles[profile.domain] = profile
            for alias in profile.crawl.equivalent_domains:
                self._aliases[normalize_domain(alias)] = profile
            for pattern in profile.crawl.subdomain_collapse:
                suffix = pattern.lstrip('*')  # '*.hms.ru' -> '.hms.ru'
                if suffix.startswith('.'):
                    self._suffixes.append((suffix, profile))
        if self._profiles:
            logger.info(f'Загружено профилей сайтов: {len(self._profiles)} '
                        f'из {self.profiles_dir}')

    def resolve(self, url_or_host: str) -> SiteProfile:
        """Профиль для URL/хоста; при отсутствии — SiteProfile.default (generic)."""
        self._ensure_loaded()
        domain = normalize_domain(url_or_host)
        if not domain:
            return SiteProfile.default('')
        profile = self._profiles.get(domain) or self._aliases.get(domain)
        if profile is not None:
            return profile
        for suffix, suffix_profile in self._suffixes:
            if domain.endswith(suffix):
                return suffix_profile
        return SiteProfile.default(domain)

    def known_domains(self):
        self._ensure_loaded()
        return sorted(self._profiles)

    def reload(self):
        """Сбросить кэш (тесты, точечное пере-профилирование)."""
        self._profiles = self._aliases = self._suffixes = None
        self.load_errors = {}


_resolver = None


def get_resolver(profiles_dir=None) -> ProfileResolver:
    """Синглтон-резолвер (text_extractor — модульные функции, объект не протащить).

    profiles_dir учитывается только при первом вызове (иначе игнорируется).
    """
    global _resolver
    if _resolver is None:
        _resolver = ProfileResolver(profiles_dir)
    return _resolver


def reset_resolver():
    """Полный сброс синглтона (для тестов)."""
    global _resolver
    _resolver = None
