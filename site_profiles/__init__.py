"""Система профилирования сайтов: декларативные YAML-профили (mvp/profiles/),
резолвер «URL -> профиль», перепись (census) и метрики прогона.

Публичный API:
    from site_profiles import get_resolver, SiteProfile
    profile = get_resolver().resolve(url)
"""
from .models import SiteProfile, profile_from_dict
from .resolver import ProfileResolver, get_resolver, reset_resolver, normalize_domain

__all__ = ['SiteProfile', 'profile_from_dict', 'ProfileResolver',
           'get_resolver', 'reset_resolver', 'normalize_domain']
