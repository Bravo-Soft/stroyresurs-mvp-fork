"""Версионированные общие правила, шаблоны и содержательные блоки профиля."""
import copy
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import soupsieve
import yaml
from bs4 import BeautifulSoup


class ProfileRuleError(ValueError):
    """Неполные или неоднозначные правила: нельзя выдавать успешное извлечение."""


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ProfileRuleError(f'YAML: повторяющийся или нестроковый ключ {key!r}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(text):
    value = yaml.load(text, Loader=UniqueLoader)
    # Циклические YAML-алиасы не являются деревом настроек.
    json.dumps(value, default=str)
    return value


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def compose(data, directory):
    """Общие → группа → сайт; происхождение полей и SHA256 входят в диагностику."""
    if not isinstance(data, dict):
        raise ProfileRuleError('профиль: ожидается объект')
    layers = []
    ref = data.get('extends') or {}
    dependencies = {}
    if ref:
        if data.get('schema_version') != 2 or not isinstance(ref, dict):
            raise ProfileRuleError('extends требует schema_version: 2 и объект ссылки')
        if set(ref) - {'rules_version', 'group', 'group_version'}:
            raise ProfileRuleError('extends: неизвестные поля')
        path = Path(directory) / '_rules.yaml'
        raw = path.read_text(encoding='utf-8')
        rules = load_yaml(raw)
        if not isinstance(rules, dict) or set(rules) - {'schema_version','version','defaults','groups'}:
            raise ProfileRuleError('_rules.yaml: неверная структура')
        if type(rules.get('schema_version')) is not int or rules['schema_version'] != 2:
            raise ProfileRuleError('_rules.yaml: неподдерживаемая схема')
        if type(ref.get('rules_version')) is not int or ref['rules_version'] < 1 or type(rules.get('version')) is not int or ref['rules_version'] != rules['version']:
            raise ProfileRuleError('extends.rules_version: версия общих правил не совпадает')
        layers.append(('defaults', rules.get('defaults', {})))
        groups = rules.get('groups', {})
        if not isinstance(groups, dict):
            raise ProfileRuleError('_rules.yaml.groups: ожидается объект')
        if ref.get('group') is not None:
            name = ref['group']
            if not isinstance(name, str) or name not in groups:
                raise ProfileRuleError('extends.group: неизвестная группа')
            group = groups[name]
            if not isinstance(group, dict) or set(group) != {'version','settings'}:
                raise ProfileRuleError('группа: нужны version и settings')
            if type(ref.get('group_version')) is not int or ref['group_version'] < 1 or type(group['version']) is not int or group['version'] != ref['group_version']:
                raise ProfileRuleError('extends.group_version: версия группы не совпадает')
            layers.append(('group:'+name, group['settings']))
        elif 'group_version' in ref:
            raise ProfileRuleError('extends.group_version без group')
        dependencies[str(path)] = hashlib.sha256(raw.encode()).hexdigest()
    effective, origins = {}, {}
    def record(value, prefix, origin):
        for key, item in value.items():
            field = prefix + key
            if isinstance(item, dict):
                if item:
                    record(item, field+'.', origin)
            else:
                for old in list(origins):
                    if old == field or old.startswith(field+'.'):
                        origins.pop(old)
                origins[field] = origin
    for origin, settings in layers:
        if not isinstance(settings, dict) or set(settings) - {'crawl','sections','extract'}:
            raise ProfileRuleError(f'{origin}: разрешены только crawl, sections, extract')
        effective = merge(effective, settings)
        record(settings, '', origin)
    effective = merge(effective, data)
    record(data, '', 'site')
    return effective, dict(origins=origins, dependencies=dependencies, effective_sha256=digest(effective), source=data)


def validate_v2(data, profile):
    """Дополнительные проверки v2; v1 сохраняет историческую совместимость."""
    errors = []
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', profile.domain) or '.' not in profile.domain or '..' in profile.domain:
        errors.append('domain: нужен канонический домен ASCII/punycode без схемы и пути')
    def css(value, label):
        if value is None: return
        try:
            if not isinstance(value, str) or not value.strip(): raise ValueError('пустой CSS')
            soupsieve.compile(value)
        except Exception as exc: errors.append(f'{label}: неверный CSS ({exc})')
    def regex(value, label):
        try:
            if not isinstance(value, str) or not value: raise ValueError('пустой regex')
            re.compile(value)
        except Exception as exc: errors.append(f'{label}: неверный regex ({exc})')
    def extraction(extract, label):
        from .models import _build_extract
        local = []
        parsed = _build_extract(extract, local)
        errors.extend(f'{label}: {e}' for e in local)
        if parsed.cluster is not None or parsed.llm_program is not None or parsed.tier in ('jsonld','cms'):
            errors.append(label+': cluster/llm_program/jsonld/cms не исполняются; используйте extends и tier llm/custom')
        md = parsed.markdown
        for name in ('product_container','company_container','distributor_container'):
            css(getattr(md,name), label+'.'+name)
        for selector in md.remove_selectors: css(selector,label+'.remove_selectors')
        if md.max_len is not None and (md.max_len < 1 or (md.min_len is not None and md.max_len < md.min_len)):
            errors.append(label+': max_len должен быть положительным и не меньше min_len')
        if parsed.custom_module is not None:
            name = parsed.custom_module
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',name) or not (Path(__file__).resolve().parents[1]/'Profile_Markdown'/(name+'.py')).is_file():
                errors.append(label+': модуль адаптера отсутствует или имеет недопустимое имя')
        names=set()
        for block in md.content_blocks:
            if not isinstance(block,dict) or set(block)-{'name','selector','required'}:
                errors.append(label+': неверная структура content_blocks');continue
            name=block.get('name')
            if not isinstance(name,str) or not name.strip() or name in names:
                errors.append(label+': имена блоков должны быть непустыми и уникальными')
            else:names.add(name)
            css(block.get('selector') or '',label+'.content_blocks')
            if type(block.get('required',False)) is not bool:errors.append(label+': required должен быть bool')
    extraction(data.get('extract',{}),'extract')
    if type(data.get('profile_version',1)) is not int or data.get('profile_version',1)<1:
        errors.append('profile_version: требуется положительное целое')
    ref=data.get('extends',{})
    if not isinstance(ref,dict) or set(ref)-{'rules_version','group','group_version'}:
        errors.append('extends: неверная структура')
    for key in ('product_url_patterns','product_url_antipatterns'):
        for value in getattr(profile.sections,key):regex(value,'sections.'+key)
    for key in ('contact_ready_selector','tab_selector','tab_panel_selector'):
        css(getattr(profile.crawl,key),'crawl.'+key)
    if profile.crawl.inline_products:
        inline=profile.crawl.inline_products
        for value in [inline.item_selector,inline.name_selector,inline.name_prefix_selector]+inline.content_selectors:css(value,'crawl.inline_products')
    names=set()
    for rule in profile.extract.page_rules:
        if not isinstance(rule,dict) or set(rule)-{'name','priority','match','extract'}:
            errors.append('page_rules: неверная структура правила');continue
        name=rule.get('name')
        if not isinstance(name,str) or not name.strip() or name in names:errors.append('page_rules: имена должны быть уникальны')
        else:names.add(name)
        if type(rule.get('priority',0)) is not int:errors.append('page_rules.priority: требуется целое')
        match=rule.get('match')
        if not isinstance(match,dict) or not match or set(match)-{'page_types','path_regex','selector'}:
            errors.append('page_rules.match: неверные условия');continue
        types=match.get('page_types',['product','company','distributor'])
        if not isinstance(types,list) or not types or any(t not in ('product','company','distributor') for t in types):errors.append('page_rules.match.page_types: неверные типы')
        if 'path_regex' in match:regex(match['path_regex'],'page_rules.match.path_regex')
        if 'selector' in match:css(match['selector'] or '','page_rules.match.selector')
        override=rule.get('extract',{})
        if not isinstance(override,dict) or 'page_rules' in override:
            errors.append('page_rules.extract: вложенные page_rules запрещены');continue
        base=asdict(profile.extract);base.pop('page_rules')
        extraction(merge(base,override),'page_rules.'+str(name))
    return errors


def select_page(profile, url, page_type, soup):
    """Возвращает копию профиля; общий объект резолвера никогда не изменяется."""
    from .models import _build_extract
    matched=[]
    for rule in profile.extract.page_rules:
        match=rule['match']
        if (page_type or 'product') not in match.get('page_types',['product','company','distributor']):continue
        if 'path_regex' in match and not re.search(match['path_regex'],urlsplit(url).path):continue
        if 'selector' in match and soup.select_one(match['selector']) is None:continue
        matched.append(rule)
    chosen=None
    if matched:
        priority=max(r.get('priority',0) for r in matched)
        best=[r for r in matched if r.get('priority',0)==priority]
        if len(best)!=1:raise ProfileRuleError('Неоднозначные шаблоны: '+', '.join(r['name'] for r in best))
        chosen=best[0]
    result=copy.deepcopy(profile)
    if chosen:
        base=asdict(profile.extract);base.pop('page_rules')
        errors=[];result.extract=_build_extract(merge(base,chosen.get('extract',{})),errors)
        if errors:raise ProfileRuleError('; '.join(errors))
    info=dict(profile.diagnostics)
    info['origins']=dict(info.get('origins',{}))
    if chosen:
        def record(values, prefix):
            for key,value in values.items():
                if isinstance(value,dict):record(value,prefix+key+'.')
                else:info['origins'][prefix+key]='page_rule:'+chosen['name']
        record(chosen.get('extract',{}),'extract.')
    info['selected_rule']=chosen['name'] if chosen else None
    info['effective_extract_sha256']=digest(asdict(result.extract))
    if result.extract.custom_module:
        path=Path(__file__).resolve().parents[1]/'Profile_Markdown'/(result.extract.custom_module+'.py')
        try:
            info['adapter_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProfileRuleError('Адаптер недоступен: '+str(path)) from exc
    result.diagnostics=info
    return result


def collect_blocks(soup, markdown, page_type, keep_hidden):
    """Объединяет выбранные блоки без дублей; вложенный блок уступает родителю."""
    work=BeautifulSoup(str(soup),'html.parser')
    for tag in list(work.select('script,style,noscript,template')):
        if tag.attrs is not None:tag.decompose()
    if not keep_hidden:
        for tag in list(work.select('[hidden],[style]')):
            if tag.attrs is not None and (tag.has_attr('hidden') or re.search(r'display\s*:\s*none',tag.get('style',''),re.I)):tag.decompose()
    container=getattr(markdown, {'company':'company_container','distributor':'distributor_container'}.get(page_type,'product_container'))
    blocks=([dict(name='container',selector=container,required=True)] if container else [])+markdown.content_blocks
    nodes=[]
    for block in blocks:
        selected=list(work.select(block['selector']))
        if block.get('required',False) and not any(t.get_text(strip=True) or t.find('img') for t in selected):
            raise ProfileRuleError('Отсутствует обязательный блок: '+block['name'])
        nodes.extend(selected)
    identities={id(n) for n in nodes};seen=set();parts=[]
    for node in nodes:
        if id(node) in seen or any(id(parent) in identities for parent in node.parents):continue
        seen.add(id(node));parts.append(str(node))
    if not parts:raise ProfileRuleError('Не найден ни один содержательный блок')
    return BeautifulSoup('\n'.join(parts),'html.parser')
