"""Контрольная выборка: свежий источник → штатное извлечение → DOCX → доказательства."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import time
from urllib.parse import urlsplit
import zipfile

from .control_transport import NetworkPolicy, FreshTransport, Unavailable

LIMITS=dict(max_pages=20,fetch_seconds=30,model_seconds=180,html_bytes=5000000,
            response_bytes=4000000,host_delay=1,max_calls=20,max_tokens=200000,output_tokens=4096)


def now():return datetime.now(timezone.utc).isoformat()
def sha(value):return hashlib.sha256(value).hexdigest()
def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))


def validate_manifest(data):
    if not isinstance(data,dict) or set(data)-{'version','company','pages','model','limits','structure_report','site_hosts','insecure_hosts','asset_hosts'} or type(data.get('version')) is not int or data['version']!=1:
        raise ValueError('Неверный manifest контрольного прогона')
    company=data.get('company',{})
    if set(company)!={'name','url'} or not all(isinstance(v,str) and v for v in company.values()):raise ValueError('Нужны имя и URL компании')
    model=data.get('model',{})
    if set(model)!={'base_url','name','key_env'} or model.get('key_env')!='CONTROL_MODEL_API_KEY':
        raise ValueError('Модель: base_url, name, key_env=CONTROL_MODEL_API_KEY; секрет не записывается в manifest')
    from ai_integration import _SINGLE_CALL_MODELS
    if model['name'] not in _SINGLE_CALL_MODELS:raise ValueError('Контроль поддерживает штатные single-call модели')
    limits=LIMITS|data.get('limits',{})
    if set(limits)!=set(LIMITS) or any(type(v) not in (int,float) or not 0<v<10**9 for v in limits.values()):raise ValueError('Неверные лимиты контроля')
    if any(type(limits[k]) is not int for k in set(LIMITS)-{'host_delay'}):raise ValueError('Нужны целые лимиты')
    if limits['max_pages']>100 or limits['max_calls']>300:raise ValueError('Контроль не предназначен для полного обхода')
    host=urlsplit(company['url']).hostname
    if not host:raise ValueError('Неверный URL компании')
    hosts={host,host.removeprefix('www.'),'www.'+host.removeprefix('www.')}
    extra_hosts=data.get('site_hosts',[])
    insecure_hosts=data.get('insecure_hosts',[])
    root=host.removeprefix('www.')
    if not isinstance(extra_hosts,list) or any(not isinstance(h,str) or not re.fullmatch(r'[a-z0-9.-]+',h) or not h.endswith('.'+root) for h in extra_hosts):
        raise ValueError('Дополнительный хост должен быть явным поддоменом сайта')
    hosts.update(extra_hosts)
    asset_hosts=data.get('asset_hosts',[])
    if (not isinstance(asset_hosts,list) or len(asset_hosts)>10 or any(not isinstance(h,str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)+',h) or h in hosts for h in asset_hosts)):
        raise ValueError('Нужны явные внешние хосты ресурсов, без путей и шаблонов')
    if not isinstance(insecure_hosts,list) or any(not isinstance(h,str) or h not in hosts for h in insecure_hosts):
        raise ValueError('TLS-исключения требуют явных разрешённых хостов')
    policy=NetworkPolicy(hosts|set(asset_hosts),model['base_url'],insecure_hosts,asset_hosts=asset_hosts);policy.check_url(company['url']);policy.check_url(policy.model_url,True)
    pages=data.get('pages')
    if not isinstance(pages,list) or not pages or len(pages)>limits['max_pages']:raise ValueError('Непустая выборка должна помещаться в лимит')
    seen=set();splits={}
    for p in pages:
        if not isinstance(p,dict) or set(p)-{'url','role','split','reference','transport','interactions','expected','wait_for_selector','asset_hosts','fresh_http_render','network_interface'}:raise ValueError('Неизвестные поля страницы')
        if urlsplit(p['url']).hostname not in hosts:raise ValueError('Страница выборки должна принадлежать сайту, не хосту ресурсов')
        page_assets=p.get('asset_hosts',[])
        if not isinstance(page_assets,list) or any(h not in asset_hosts for h in page_assets):raise ValueError('Ресурсы страницы вне общего списка')
        policy.check_url(p['url'])
        identity=(p['url'],p.get('role','product'))
        if identity in seen:raise ValueError('URL с этой ролью повторяется в выборке')
        if p['url'] in splits and splits[p['url']]!=p.get('split','control'):raise ValueError('URL holdout пересекается с control')
        seen.add(identity);splits[p['url']]=p.get('split','control')
        if p.get('role','product') not in ('product','company','distributor','negative'):raise ValueError('Неверная роль')
        if p.get('split','control') not in ('control','holdout'):raise ValueError('Неверная выборка')
        if 'fresh_http_render' in p and (type(p['fresh_http_render']) is not bool or p.get('transport')!='browser' or urlsplit(p['url']).hostname not in ('v-kirpich.ru','www.v-kirpich.ru','ryazanbrick.ru','www.ryazanbrick.ru')):
            raise ValueError('Свежий HTTP-рендер разрешён только браузерной диагностике v-kirpich.ru/ryazanbrick.ru')
        if 'network_interface' in p and (not isinstance(p['network_interface'],str)
                or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}',p['network_interface'])
                or not p.get('fresh_http_render') or (urlsplit(p['url']).hostname or '').removeprefix('www.')!='ryazanbrick.ru'):
            raise ValueError('Сетевой интерфейс разрешён только свежему HTTP-рендеру ryazanbrick.ru')
        if p.get('transport','http') not in ('http','browser'):raise ValueError('Нужен свежий HTTP или браузер')
        if p.get('expected','unspecified') not in ('unspecified','product','not_product'):raise ValueError('Неверное ожидание')
        if not isinstance(p.get('interactions',[]),list) or any(not isinstance(s,str) or not s for s in p.get('interactions',[])) or len(p.get('interactions',[]))>32:raise ValueError('Неверные взаимодействия')
        if p.get('interactions') and p.get('transport')!='browser':raise ValueError('Взаимодействия требуют браузера')
    return limits,policy


def sandbox_check(output):
    # В production-копии либо в рабочем snapshot корень кода обязан быть read-only.
    source=Path(__file__).resolve().parents[1]
    if not os.statvfs(source).f_flag & os.ST_RDONLY:raise Unavailable('Нет обязательного read-only mount исходников')
    if os.statvfs(output).f_flag & os.ST_RDONLY:raise Unavailable('Каталог результатов read-only')
    from .profile_queue import workspace
    project=workspace().parent
    protected=[source,project/'mvp',project/'mvp_directories',Path('/home/user/stroy-resurs/mvp_directories')]
    checks={str(path):bool(os.statvfs(path).f_flag & os.ST_RDONLY) for path in protected if path.exists()}
    if not all(checks.values()):raise Unavailable('Продовые каталоги не защищены read-only mount')
    return dict(readonly_paths=checks,output=str(output),output_writable=True,graph_enabled=False,kafka_enabled=False)


def make_client(model,transport,limits,budget,folder):
    from ai_integration import AITunnelClient
    class EvidenceClient(AITunnelClient):
        def __init__(self):
            # Не скачиваем tiktoken и не используем глобальное окружение/кэш клиента.
            self.model=model['name'];self.encoding=None;self.processing_tracker=None
            self.rate_limiter=None;self.performance_monitor=None;self.max_retries=0
            self.current_source='';self.sent_sources=[];self.calls=[];self.last_error=None
        def _single_user_prompt(self,text,manufacturer,base_domain,page_url=''):
            self.current_source=text[:100000 if self.model=='gemma4-31b-nvfp4' else 120000]
            return super()._single_user_prompt(text,manufacturer,base_domain,page_url)
        async def _make_request_with_retry(self,payload):
            if budget['calls']>=limits['max_calls'] or budget['input_tokens']+budget['output_tokens']>=limits['max_tokens']:
                self.last_error='model_budget';return None,dict(input_tokens=0,output_tokens=0)
            payload=dict(payload);payload['max_tokens']=min(payload.get('max_tokens',limits['output_tokens']),limits['output_tokens'])
            # Проверяем доступный бюджет до запроса, оценка отдельно от usage сервера.
            def content_text(message):
                content=message.get('content','')
                if isinstance(content,str):return content
                return '\n'.join(part.get('text','') for part in content if part.get('type')=='text')
            estimated=sum(self.count_tokens(content_text(m)) for m in payload['messages'])
            estimated+=4096*sum(1 for m in payload['messages'] if isinstance(m.get('content'),list)
                                for part in m['content'] if part.get('type')=='image_url')
            if estimated+payload['max_tokens']>limits['max_tokens']-budget['input_tokens']-budget['output_tokens']:
                self.last_error='token_budget';return None,dict(input_tokens=0,output_tokens=0)
            index=len(self.calls);budget['calls']+=1
            save(folder/f'request_{index}.json',payload)
            user='\n'.join(content_text(m) for m in payload['messages'] if m['role']=='user')
            if self.current_source and self.current_source in user:self.sent_sources.append(self.current_source)
            elif self.current_source and re.search(r'^URL страницы: https?://(?:www\.)?rifar\.ru/buy/(?:sales-centers|shops)/?$', self.current_source, re.M):
                messages = [m for m in payload['messages'] if m['role'] == 'user']
                segment = content_text(messages[-1]) if len(messages) == 2 else ''
                offset = sum(map(len,self.sent_sources))
                if segment and self.current_source.startswith(segment,offset):
                    self.sent_sources.append(segment)
                else:
                    self.last_error = 'source_window_not_verified'
            else:self.last_error='source_window_not_verified'
            started=time.monotonic();entry=dict(request=f'request_{index}.json',started_at=now(),estimated_input_tokens=estimated)
            try:
                status,headers,body=await transport.request(transport.policy.model_url,model=True,payload=payload,key=os.environ.get(model['key_env'],''))
                (folder/f'response_{index}.json').write_bytes(body)
                entry.update(status=status,response=f'response_{index}.json')
                if status!=200:raise Unavailable('Модель HTTP '+str(status))
                result=json.loads(body)
                usage=result.get('usage') or {}
                known=all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens'))
                if not known:usage={}
                tokens=dict(input_tokens=usage.get('prompt_tokens',estimated),output_tokens=usage.get('completion_tokens',payload['max_tokens']))
                budget['input_tokens']+=tokens['input_tokens'];budget['output_tokens']+=tokens['output_tokens']
                if not known:budget['usage_estimated']=True
                entry['usage']=tokens;entry['usage_exact']=known
                return result,tokens
            except Exception as exc:
                # Не повторяем неоднозначный запрос: провайдер мог уже выполнить генерацию.
                budget['input_tokens']+=estimated;budget['output_tokens']+=payload['max_tokens']
                budget['usage_estimated']=True;self.last_error=type(exc).__name__+': '+str(exc)
                entry['error']=self.last_error
                return None,dict(input_tokens=0,output_tokens=0)
            finally:
                entry['seconds']=time.monotonic()-started;self.calls.append(entry);save(folder/'model_calls.json',self.calls)
    return EvidenceClient()


async def execute(manifest_path,profiles,output,*,transport=None,require_isolation=True):
    """Внешний запуск — только через очередь с bwrap; подмена транспорта для тестов явная."""
    from .profile_queue import contained,workspace,inventory
    from .profile_metrics import RunMetricsCollector
    from .census_collector import CensusCollector
    from .resolver import ProfileResolver
    from . import resolver as resolver_module
    from .markdown_validation import check_markdown
    import text_extractor
    from docx_generator import DOCXGenerator
    from profnastil_configurator import restore_configurator
    from product_preparation import prepare_product,count_numeric_characteristics
    from source_product_tables import compact_source_tables,source_family_catalog_reason
    from web_crawler import URLCategorizer
    output=contained(output,workspace());output.mkdir(parents=True,exist_ok=False)
    data=json.loads(Path(manifest_path).read_text());limits,policy=validate_manifest(data)
    rows=[];gaps=['semantic_review_pending'];cards=[];budget=dict(calls=0,input_tokens=0,output_tokens=0,usage_estimated=False)
    metrics=RunMetricsCollector(urlsplit(data['company']['url']).hostname,data['company']['name']);metrics.limits=limits
    resolver=ProfileResolver(profiles);profile=resolver.resolve(data['company']['url'])
    classifier=URLCategorizer();classifier.set_profile(profile)
    old_resolver=resolver_module._resolver;old_sink=text_extractor._census_sink
    resolver_module._resolver=resolver;text_extractor.set_census_sink(metrics.sink)
    started=time.monotonic();seen_final=set();processed=0;below=0
    class LocalDOCX(DOCXGenerator):
        async def _convert_to_rtf(self,path):return ''  # Сохраняем оригинал, не запускаем Office.
    def allowed(url):
        policy.check_url(url)
        category,_=classifier.categorize_url(url)
        if category in ('excluded','excluded2') or classifier.should_exclude_by_language(url):raise ValueError('URL исключён профилем/классификатором')
    try:
        if require_isolation:save(output/'isolation.json',sandbox_check(output))
        if not os.environ.get(data['model']['key_env']):raise Unavailable('Нет CONTROL_MODEL_API_KEY')
        if not any(p.get('split')=='holdout' for p in data['pages']):gaps.append('holdout_missing')
        transport=transport or FreshTransport(policy,limits)
        # Перед каждым HTTP/браузерным редиректом применяется и профильный запрет.
        transport.page_check=allowed
        for index,page in enumerate(data['pages']):
            folder=output/f'{index:04d}';folder.mkdir()
            row=dict(url=page['url'],folder=folder.name,role=page.get('role','product'),split=page.get('split','control'),started_at=now(),state='pending')
            rows.append(row);begin=time.monotonic()
            try:
                allowed(page['url'])
                fetch=await transport.fetch(page['url'],folder,page)
                html=fetch.pop('html');final=fetch['final_url'];allowed(final)
                (folder/'source.html').write_text(html)
                fetch['fetched_at']=now();fetch['html_sha256']=sha(html.encode());save(folder/'fetch.json',fetch)
                row.update(final_url=final,fetch='fetch.json',source_sha256=fetch['html_sha256'])
                metrics.record_fetch(final,row['role'],fetch['method']);metrics.record_pages([dict(url=final,category=row['role'])])
                identity=(final,row['role'])
                if identity in seen_final:
                    row.update(state='duplicate',reason='same_final_url');continue
                seen_final.add(identity)
                if fetch.get('visibility')!='screenshots_and_actions':gaps.append('static_visibility:'+page['url'])
                if fetch.get('blocked_requests'):gaps.append('browser_resources_blocked:'+page['url'])
                from web_crawler import page_error_reason
                error=page_error_reason(html)
                if error:raise Unavailable('Страница ошибки: '+error)
                kind='' if row['role'] in ('product','negative') else row['role']
                client=make_client(data['model'],transport,limits,budget,folder)
                supplement=''
                if row['role'] in ('product','negative'):
                    from source_image_text import public_image_markdown
                    async def fetch_image(url):
                        status,headers,body=await transport.request(url,image=True)
                        if status!=200:raise Unavailable('Изображение HTTP '+str(status))
                        return headers.get('Content-Type',headers.get('content-type','')).split(';')[0],body
                    supplement,_,image_sources=await public_image_markdown(html,final,client,folder/'image_text',fetch_image)
                    if image_sources:row['image_sources']='image_text/sources.json'
                md=text_extractor.html_to_markdown(html,final,kind)+supplement;(folder/'markdown.md').write_text(md)
                reference=page.get('reference',{})
                check=check_markdown(html,md,url=final,source_selectors=reference.get('selectors'),
                                     public_selectors=reference.get('public_selectors',[]),exclusions=reference.get('exclusions',[]))
                save(folder/'markdown_check.json',check);row['markdown_status']=check['status']
                if not reference.get('origin'):gaps.append('reference_origin_missing:'+page['url'])
                if check['status']=='fail':row.update(state='failed',reason='markdown_loss');continue
                if check['status']=='uncertain':gaps.append('markdown_uncertain:'+page['url'])
                compact=compact_source_tables(html,final) if row['role'] in ('product','negative') else html
                prompt_md=md if compact==html else text_extractor.html_to_markdown(compact,final,kind)+supplement
                source=f"Производитель: {data['company']['name']}\nURL страницы: {final}\n\n{prompt_md}"
                (folder/'prepared_source.txt').write_text(source)
                client.current_source=source
                if row['role'] in ('product','negative'):
                    reason=source_family_catalog_reason(html,final)
                    if reason:result={'error':'Trash_418#','source_rejection':reason}
                    else:result,_=await client.extract_product_info(source,data['company']['name'],data['company']['url'],page_url=final)
                elif len(source)>100000:
                    raise Unavailable('Слишком большой источник company/distributor для проверяемого одного запроса')
                elif row['role']=='company':result,_=await client.extract_company_info(source,'control')
                else:result,_=await client.extract_distributor_info(source,'control',data['company']['name'],str(folder))
                row['model_calls']=len(client.calls)
                save(folder/'extraction.json',result)
                if result is None or client.last_error:raise Unavailable(client.last_error or 'Нечитаемый ответ модели')
                if client.sent_sources:
                    if re.search(r'^URL страницы: https?://(?:www\.)?rifar\.ru/buy/(?:sales-centers|shops)/?$', source,re.M):
                        actual = ''.join(client.sent_sources)
                        if actual != source:
                            raise Unavailable('Контроль исходного текста Рифар обнаружил пропуск или дубликат')
                    else:
                        actual='\n\n'.join(client.sent_sources)
                    (folder/'model_input.txt').write_text(actual)
                    checked_input=check_markdown(html,md,url=final,source_selectors=reference.get('selectors'),
                        public_selectors=reference.get('public_selectors',[]),exclusions=reference.get('exclusions',[]),model_input=actual)
                    save(folder/'model_input_check.json',checked_input)
                    row['model_input_status']=checked_input['model_input']['status']
                    if checked_input['model_input']['status']=='fail':
                        if compact!=html:gaps.append('compacted_tables_restoration_review:'+final)
                        else:row.update(state='failed',reason='model_input_loss');continue
                if row['role'] not in ('product','negative'):
                    if not isinstance(result,dict) or result.get('error'):raise Unavailable('Ошибка структурированных данных')
                    row['state']='data_extracted';continue
                restore_configurator(result,html,final,data['company']['name'])
                if result.get('error')=='Trash_418#':
                    row.update(state='rejected',reason=result.get('source_rejection','model_not_product'))
                    if page.get('expected')=='product':row.update(state='failed',reason='expected_product_rejected')
                    continue
                if not isinstance(result.get('product'),dict):raise Unavailable('Нет объекта product')
                prepare_product(result,html,final,source);save(folder/'prepared_product.json',result)
                product=result['product'];count=count_numeric_characteristics(product)
                passed=bool(product.get('specifications')) and count>=3
                metrics.record_specs(count,passed);row['numeric_specs']=count
                if not passed:
                    below+=1;row.update(state='rejected',reason='below_numeric_gate')
                    if page.get('expected')=='product':row.update(state='failed',reason='expected_product_below_gate')
                    continue
                if page.get('expected')=='not_product' or row['role']=='negative':row.update(state='failed',reason='negative_became_product');continue
                processed+=1
                card_id=sha(final.encode())[:24];card_folder=folder/'card';card_folder.mkdir()
                path=await LocalDOCX().generate_product_card(dict(product_id=card_id,name=product.get('product_name',''),
                    json_data=result,source_url=final),dict(original_name=data['company']['name']),str(card_folder))
                if not path or not Path(path).is_file():row.update(state='failed',reason='card_generation_failed');continue
                card=dict(card_id=card_id,url=final,numeric_specs=count,path=str(Path(path).relative_to(output)),sha256=sha(Path(path).read_bytes()))
                cards.append(card);metrics.record_card(**card,source_sha256=fetch['html_sha256'])
                row.update(state='card_generated',card=card)
            except ValueError as exc:row.update(state='failed',reason=str(exc))
            except Exception as exc:
                row.update(state='uncertain',reason=type(exc).__name__+': '+str(exc));gaps.append('page_unavailable:'+page['url'])
                if 'fetch' not in row:metrics.record_fetch_fail(page['url'],row['role'])
            finally:
                row['seconds']=time.monotonic()-begin;save(folder/'page.json',row);save(output/'progress.json',rows)
    except Exception as exc:
        gaps.append('preflight:'+type(exc).__name__+': '+str(exc))
        for page in data['pages'][len(rows):]:rows.append(dict(url=page['url'],state='not_run',reason=str(exc)))
    finally:
        resolver_module._resolver=old_resolver;text_extractor.set_census_sink(old_sink)
    if data.get('structure_report'):
        structure=json.loads((Path(manifest_path).parent/data['structure_report']).read_text())
        keys={row['key']:row['url'] for row in structure['urls']};provided={r['url'] for r in rows}
        if any(keys[k] not in provided for k in structure['sample']):gaps.append('discovery_sample_missing')
        gaps.extend('discovery:'+str(g) for g in structure.get('coverage',{}).get('gaps',[]))
    metrics.record_output(dict(products_processed=processed,products_below_min_specs=below,cards_generated=len(cards),
                               products_failed=sum(r['state']=='failed' for r in rows)))
    totals=metrics.to_dict();totals['median_specs_per_card']=statistics.median(totals['card_spec_counts']) if cards else None
    totals['avg_specs_per_card']=round(statistics.mean(totals['card_spec_counts']),2) if cards else None
    totals['confidence']=CensusCollector.confidence(None,totals);totals['confidence_method']='existing_0.3F_0.4E_0.3V'
    totals['model_usage']=budget;totals['seconds']=time.monotonic()-started
    totals['card_spec_counts_complete']=len(cards)==len(totals['card_spec_counts'])==len(totals['card_sources'])
    if not cards:gaps.append('zero_cards_requires_review')
    if budget['usage_estimated']:gaps.append('model_usage_estimated')
    if budget['input_tokens']+budget['output_tokens']>limits['max_tokens']:gaps.append('actual_token_budget_exceeded')
    save(output/'metrics.json',totals)
    status='fail' if any(r['state']=='failed' for r in rows) else 'uncertain'
    summary=dict(version=1,status=status,scope='control_sample_not_profile_acceptance',created_at=now(),pages=rows,cards=cards,
                 uncertainties=sorted(set(gaps)),metrics='metrics.json',limits=limits,model=data['model'],
                 isolation=dict(graph=False,kafka=False,production_writes=False,required_readonly_mount=require_isolation),
                 manifest_sha256=sha(Path(manifest_path).read_bytes()),profile_sha256=inventory(profiles))
    summary['mode']='isolated_live' if require_isolation else 'injected_test_not_live'
    from .profile_queue import environment
    summary['environment']=environment()
    summary['run_id']=sha(str(output).encode())[:24]
    if (output.parent/'request.json').is_file():
        request=json.loads((output.parent/'request.json').read_text())
        summary['queue_provenance']={k:request[k] for k in ('job','attempt','stage','snapshot') if k in request}
    source=Path(__file__).resolve().parents[1]
    paths=[*source.glob('*.py'),*Path(__file__).parent.glob('*.py'),*(source/'Profile_Markdown').glob('*.py')]
    summary['code_sha256']={str(p.relative_to(source)):sha(p.read_bytes()) for p in paths}
    summary['prompt_system_sha256']=sorted({sha(message['content'].encode())
        for p in output.glob('*/request_*.json')
        for message in json.loads(p.read_text())['messages']
        if message['role']=='system' and isinstance(message['content'],str)})
    save(output/'summary.json',summary)
    (output/'SUMMARY.txt').write_text('Контрольный прогон: '+status+'\n'+json.dumps(dict(Counter(r['state'] for r in rows)),ensure_ascii=False)+'\nСмысловая приёмка профиля не выполнена.\n')
    hashes=inventory(output);save(output/'evidence_sha256.json',hashes)
    with zipfile.ZipFile(output/'evidence.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob('*')):
            if path.is_file() and path.name!='evidence.zip':archive.write(path,path.relative_to(output))
    return summary
