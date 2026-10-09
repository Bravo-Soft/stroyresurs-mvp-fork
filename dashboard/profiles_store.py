# dashboard/profiles_store.py 1.0.0
# Профили сайтов (profiles/<домен>.yaml): чтение, правка, удаление, приём черновика переписи.
#
# Редактор — сырой YAML: оператор правит текст, сервер проверяет его тем же валидатором,
# что и пайплайн (site_profiles.profile_from_dict), и пишет файл как есть — комментарии и
# порядок ключей сохраняются. Единственная трансформация данных — приём черновика переписи.
#
# Правила:
#   * только stdlib; PyYAML и site_profiles импортируются лениво — панель обязана
#     подниматься без зависимостей пайплайна (без PyYAML чтение работает, запись -> 503);
#   * имя файла = канонический домен; путь проверяется по realpath внутри каталога
#     профилей (та же защита, что у purge._assert_safe_target); служебные _*.yaml
#     редактированию не подлежат;
#   * оптимистическая блокировка: версия = sha1 содержимого, PUT с чужой версией -> 409;
#   * тело файла в журнал не пишется — только домен, размер, версия, изменённые ключи.
#
# Пайплайн перечитывает каталог профилей перед каждой компанией
# (ProfileResolver.reload_if_changed), поэтому правка применяется со следующей компании
# бегущего прогона, а не при следующем старте, как настройки §6.

import hashlib
import logging
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from common import DOMAIN_RE, cfg_value

log = logging.getLogger("dashboard.profiles")

MAX_BYTES = 256 * 1024
ACCEPT_CONFIDENCE = 0.8   # порог приёмки черновика (profiles_drafts/README.md)


class ProfileError(RuntimeError):
    """Отказ операции (наружу — HTTP 409)."""


class ProfileConflict(ProfileError):
    """Версия файла разошлась или подтверждение не совпало (HTTP 409)."""


class ProfileValidationError(ProfileError):
    """Текст не является валидным профилем (HTTP 422). errors — «путь: проблема»."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__('; '.join(self.errors))


class ProfilesUnavailable(RuntimeError):
    """Нет PyYAML в окружении панели или каталог профилей не настроен (HTTP 503)."""


# ------------------------------ каталоги и имена ------------------------------

def profiles_dir():
    """Каталог профилей — та же лестница, что у карточки компании."""
    path = cfg_value('profiles_dir', 'PROFILES_DIR')
    if not path:
        base_dir = cfg_value('base_dir', 'BASE_DIR')
        path = os.path.join(base_dir, 'profiles') if base_dir else None
    if not path:
        raise ProfilesUnavailable('Каталог профилей не настроен (PROFILES_DIR)')
    return path


def drafts_dir():
    return cfg_value('profiles_drafts_dir', 'PROFILES_DRAFTS_DIR') or None


def domain_for_website(website):
    """Канонический домен сайта компании: как у резолвера пайплайна."""
    try:
        from site_profiles import normalize_domain
        return normalize_domain(website or '')
    except ImportError:
        s = (website or '').strip().lower()
        if '://' in s:
            s = urlparse(s).netloc
        else:
            s = s.lstrip('/').split('/', 1)[0]
        if s.startswith('www.'):
            s = s[4:]
        return s.split(':', 1)[0]


def valid_domain(domain):
    """Домен как имя файла: нижний регистр, только буквы/цифры/дефис/точки."""
    domain = (domain or '').strip().lower()
    if domain.startswith('_'):
        raise ProfileError('Служебные файлы _*.yaml профилями не являются')
    if not DOMAIN_RE.match(domain):
        raise ProfileError(f'Некорректный домен профиля: «{domain}»')
    return domain


def path_for(domain):
    """<profiles_dir>/<домен>.yaml — строго внутри каталога профилей."""
    domain = valid_domain(domain)
    root = os.path.realpath(profiles_dir())
    path = os.path.realpath(os.path.join(root, f'{domain}.yaml'))
    if path == root or os.path.commonpath([root, path]) != root:
        raise ProfileError(f'Цель вне каталога профилей: {domain}')
    return path


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        raise ProfilesUnavailable('PyYAML не установлен в окружении Диспетчерской — '
                                  'проверка и запись профилей недоступны')


def _version(text):
    """Отпечаток содержимого — ETag для PUT (стабилен при записи того же текста)."""
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16] if text is not None else None


def _read_text(path):
    """Текст файла или None, если его нет."""
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            return f.read()
    except FileNotFoundError:
        return None


def _normalize_text(text):
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    return text if text.endswith('\n') else text + '\n'


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(tmp, path)


def _load_dict(text):
    """dict из YAML или None (битый YAML / не объект / нет PyYAML)."""
    try:
        data = _yaml().safe_load(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _modified_at(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds')
    except OSError:
        return None


# ------------------------------ чтение ----------------------------------------

def schema_hint():
    """profiles/_schema.yaml — эталон с комментариями, подсказка в редакторе."""
    try:
        with open(os.path.join(profiles_dir(), '_schema.yaml'), 'r', encoding='utf-8') as f:
            return f.read()
    except (OSError, ProfilesUnavailable):
        return ''


def _draft(domain):
    """Черновик переписи <drafts_dir>/<домен>.yaml — fail-open."""
    d = drafts_dir()
    if not d:
        return {'exists': False}
    path = os.path.join(d, f'{domain}.yaml')
    text = _read_text(path)
    if text is None:
        return {'exists': False}
    data = _load_dict(text) or {}
    confidence = data.get('confidence')
    product_cards = (data.get('baseline') or {}).get('product_cards')
    return {
        'exists': True, 'path': path, 'text': text, 'version': _version(text),
        'modified_at': _modified_at(path),
        'confidence': confidence, 'product_cards': product_cards,
        'profile_version': data.get('profile_version'),
        'accept_ready': bool(isinstance(confidence, (int, float))
                             and confidence >= ACCEPT_CONFIDENCE
                             and isinstance(product_cards, int) and product_cards > 0),
    }


def read(domain):
    """Профиль для редактора: текст, версия, валидность, схема, черновик переписи."""
    domain = valid_domain(domain)
    try:
        path = path_for(domain)
    except ProfilesUnavailable as e:
        return {'domain': domain, 'exists': False, 'available': False, 'detail': str(e)}
    text = _read_text(path)
    row = {
        'domain': domain, 'exists': text is not None, 'available': True,
        'dir': os.path.dirname(path), 'file': os.path.basename(path), 'path': path,
        'text': text or '', 'version': _version(text),
        'bytes': len(text.encode('utf-8')) if text is not None else 0,
        'modified_at': _modified_at(path) if text is not None else None,
        'valid': False, 'errors': [], 'yaml_available': True,
        'schema_hint': schema_hint(),
        'draft': _draft(domain),
    }
    if text is not None:
        try:
            row['errors'] = validate(domain, text)
            row['valid'] = not row['errors']
        except ProfilesUnavailable as e:
            row['valid'] = None
            row['yaml_available'] = False
            row['errors'] = [str(e)]
    return row


# ------------------------------ валидация -------------------------------------

def validate(domain, text):
    """Ошибки профиля («путь: проблема»); пустой список — валиден."""
    domain = valid_domain(domain)
    yaml = _yaml()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f'YAML: {e}']
    if not isinstance(data, dict):
        return ['профиль: ожидается объект YAML']
    from site_profiles import profile_from_dict
    _, errors = profile_from_dict(data)
    file_domain = data.get('domain')
    if isinstance(file_domain, str) and file_domain and domain_for_website(file_domain) != domain:
        errors.append(f'domain: в файле «{file_domain}», ожидается «{domain}» '
                      f'(имя файла — ключ резолюции)')
    # profile_from_dict проверяет только «список строк»; битый regex ломал бы краул тихо
    sections = data.get('sections') if isinstance(data.get('sections'), dict) else {}
    for key in ('product_url_patterns', 'product_url_antipatterns'):
        for pattern in sections.get(key) or []:
            if isinstance(pattern, str):
                try:
                    re.compile(pattern)
                except re.error as e:
                    errors.append(f'sections.{key}: битый regex «{pattern}» ({e})')
    return errors


# ------------------------------ запись ----------------------------------------

def save(domain, text, expected_version=None, issued_by='ui'):
    """Записать профиль. expected_version — версия, которую видел редактор (None = новый файл)."""
    domain = valid_domain(domain)
    path = path_for(domain)
    text = _normalize_text(text)
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ProfileValidationError([f'файл: больше {MAX_BYTES // 1024} КБ — это не профиль'])
    errors = validate(domain, text)
    if errors:
        raise ProfileValidationError(errors)
    before = _read_text(path)
    if _version(before) != expected_version:
        raise ProfileConflict('Файл изменён на диске другим редактором — перечитайте профиль '
                              'и повторите правку')
    _atomic_write(path, text)
    before_keys = _load_dict(before) if before is not None else {}
    after_keys = _load_dict(text) or {}
    before_keys = before_keys or {}
    changed = sorted(k for k in set(before_keys) | set(after_keys)
                     if before_keys.get(k) != after_keys.get(k))
    log.info(f"Профиль {domain}.yaml {'создан' if before is None else 'сохранён'} "
             f"({issued_by}): изменено ключей — {len(changed)}")
    return {
        'ok': True, 'created': before is None, 'domain': domain,
        'file': os.path.basename(path), 'path': path,
        'bytes': len(text.encode('utf-8')), 'version': _version(text),
        'modified_at': _modified_at(path), 'changed_keys': changed,
    }


def delete(domain, confirm_domain, issued_by='ui'):
    """Удалить профиль; подтверждение — домен, введённый оператором."""
    domain = valid_domain(domain)
    if (confirm_domain or '').strip().lower() != domain:
        raise ProfileConflict('Подтверждение не совпадает с доменом — операция отменена')
    path = path_for(domain)
    text = _read_text(path)
    if text is None:
        raise ProfileError(f'Профиля {domain}.yaml нет')
    os.remove(path)
    log.info(f"Профиль {domain}.yaml удалён ({issued_by})")
    return {'ok': True, 'domain': domain, 'file': os.path.basename(path),
            'bytes': len(text.encode('utf-8')), 'version': _version(text)}


def accept_draft(domain, expected_version=None, issued_by='ui'):
    """Перенести черновик переписи в профиль: без census_evidence, source census -> mixed,
    profile_version = текущая на диске + 1. Черновик остаётся на месте (след переписи)."""
    domain = valid_domain(domain)
    yaml = _yaml()
    draft = _draft(domain)
    if not draft['exists']:
        raise ProfileError(f'Черновика переписи для {domain} нет')
    data = _load_dict(draft['text'])
    if data is None:
        raise ProfileValidationError(['черновик: не читается как объект YAML'])
    data.pop('census_evidence', None)
    data['domain'] = domain
    current_text = _read_text(path_for(domain))
    current = (_load_dict(current_text) if current_text is not None else None) or {}
    data['profile_version'] = (current.get('profile_version') or 0) + 1
    if data.get('source') == 'census':
        data['source'] = 'mixed'
    strip = lambda d: {k: v for k, v in d.items() if k != 'profile_version'}
    if current and strip(current) == strip(data):
        raise ProfileConflict('Черновик уже принят: профиль совпадает с ним')
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    result = save(domain, text, expected_version, issued_by)
    result['confidence'] = draft.get('confidence')
    result['profile_version'] = data['profile_version']
    return result
