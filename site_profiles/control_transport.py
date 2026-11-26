"""Ограниченный свежий HTTP/браузерный ввод контрольного прогона."""
import asyncio
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit, urljoin, parse_qs

import aiohttp

FORBIDDEN_PORTS = {4242,4243,4244,5432,5500,5510,6379,7687,9092}
DOCUMENTS = ('.pdf','.doc','.docx','.xls','.xlsx','.zip','.rtf','.ppt','.pptx')


class Unavailable(RuntimeError):
    """Нет подтверждённого результата; это не успешная проверка и не дефект профиля."""


def browser_address(addresses):
    """Предпочитаем IPv4; IPv6 в правилах Chromium требует квадратных скобок."""
    address = min(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
    return '[' + address + ']' if ':' in address else address


def public_dealer_read(method, request_url, page_url):
    """Штатный публичный список RVUL; не разрешает отправку иных форм Bitrix."""
    target = urlsplit(request_url)
    query = parse_qs(target.query)
    return (method == 'POST' and urlsplit(page_url).hostname in ('rvul.ru', 'www.rvul.ru')
            and target.hostname == 'rvul.ru' and target.path == '/bitrix/services/main/ajax.php'
            and query == {'c': ['novox:dealers.map'], 'action': ['getDealers'], 'mode': ['class']})


class NetworkPolicy:
    def __init__(self, hosts, model_url, insecure_hosts=(), *, asset_hosts=()):
        self.hosts=set(hosts);self.model_url=model_url.rstrip('/')+'/chat/completions'
        self.asset_hosts=set(asset_hosts)
        if not self.asset_hosts <= self.hosts:raise ValueError('Хост ресурсов вне разрешённого списка')
        self.insecure_hosts=set(insecure_hosts)
        if self.asset_hosts & self.insecure_hosts:raise ValueError('TLS-исключение для ресурсов запрещено')
        if not self.insecure_hosts <= self.hosts:
            raise ValueError('TLS-исключение вне разрешённых хостов')
        self.addresses=set();self.resolved={}

    def check_browser_request(self, url, method, resource_type, asset_hosts=()):
        self.check_url(url)
        if urlsplit(url).hostname in self.asset_hosts:
            if urlsplit(url).hostname not in asset_hosts:raise ValueError('Ресурс не требуется этой странице')
            if method != 'GET' or resource_type not in ('script','stylesheet','image','font','xhr','fetch'):
                raise ValueError('Внешний хост разрешён только для чтения ресурсов страницы')

    def check_url(self,url,model=False):
        p=urlsplit(url)
        port=p.port or (443 if p.scheme=='https' else 80)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or port in FORBIDDEN_PORTS:
            raise ValueError('Запрещённый адрес/порт')
        if model:
            if url!=self.model_url:raise ValueError('Разрешён только заданный endpoint модели')
        elif p.hostname not in self.hosts or port not in (80,443) or p.path.lower().endswith(DOCUMENTS):
            raise ValueError('Страница вне разрешённого периметра')
        return p.hostname,port

    async def resolve(self,url,model=False):
        host,port=self.check_url(url,model)
        key=(host,port)
        if key not in self.resolved:
            values=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
            ips=sorted({v[4][0] for v in values})
            if not ips or (not model and any(not ipaddress.ip_address(ip).is_global for ip in ips)):
                raise ValueError('Сайт разрешается в непубличный IP')
            self.resolved[key]=ips
            self.addresses.update((ip,port) for ip in ips)
        return self.resolved[key]

    def network_audit(self,event,args):
        if event=='socket.getaddrinfo':
            host=args[0];port=args[1]
            model=urlsplit(self.model_url)
            if host not in self.hosts and host!=model.hostname:raise PermissionError('DNS вне периметра')
            if port in FORBIDDEN_PORTS:raise PermissionError('Порт интеграции запрещён')
        elif event=='socket.connect':
            address=args[1]
            if not isinstance(address,tuple) or (address[0],address[1]) not in self.addresses:
                raise PermissionError('Соединение вне разрешённых адресов')
        elif event in ('socket.bind','socket.sendto'):raise PermissionError('Bind/UDP запрещены')


class PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self,policy):self.policy=policy
    async def resolve(self,host,port=0,family=socket.AF_INET):
        ips=self.policy.resolved.get((host,port))
        if not ips:raise ValueError('Нет проверенного DNS для запроса')
        return [dict(hostname=host,host=ip,port=port,family=socket.AF_INET6 if ':' in ip else socket.AF_INET,
                     proto=socket.IPPROTO_TCP,flags=socket.AI_NUMERICHOST) for ip in ips]
    async def close(self):pass


class FreshTransport:
    def __init__(self,policy,limits):
        self.policy=policy;self.limits=limits;self.last=0;self.page_check=None

    async def request(self,url,*,model=False,payload=None,key='',image=False,browser_resource=False,network_interface=None):
        if image:
            from source_image_text import _sellex_table
        if image and (model or (not urlsplit(url).path.lower().endswith(('.png','.jpg','.jpeg','.webp'))
                                and not _sellex_table(url))):
            raise ValueError('Недопустимый источник изображения')
        if not model and not image and not browser_resource and self.page_check:self.page_check(url)
        await self.policy.resolve(url,model)
        timeout=self.limits['model_seconds'] if model else self.limits['fetch_seconds']
        maximum=self.limits['response_bytes'] if model else self.limits['html_bytes']
        if image:maximum=min(maximum,5*1024*1024)
        headers={'Cache-Control':'no-cache, no-store','Pragma':'no-cache','User-Agent':'StroyResurs-ProfileControl/1'}
        if model:headers['Authorization']='Bearer '+key
        connector_options = {}
        if network_interface is not None:
            if model or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', network_interface):
                raise ValueError('Недопустимый сетевой интерфейс для HTTP-рендера')
            def bound_socket(address_info):
                family, kind, protocol, _, _ = address_info
                sock = socket.socket(family, kind, protocol)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, network_interface.encode() + b'\0')
                except Exception:
                    sock.close()
                    raise
                return sock
            connector_options['socket_factory'] = bound_socket
        connector=aiohttp.TCPConnector(resolver=PinnedResolver(self.policy), **connector_options)
        async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=timeout),trust_env=False) as session:
            verify_tls = model or urlsplit(url).hostname not in self.policy.insecure_hosts
            async with session.request('POST' if model else 'GET',url,json=payload,headers=headers,allow_redirects=False,ssl=None if verify_tls else False) as response:
                chunks=[];size=0
                async for part in response.content.iter_chunked(65536):
                    size+=len(part)
                    if size>maximum:raise Unavailable('Лимит размера ответа')
                    chunks.append(part)
                return response.status,dict(response.headers),b''.join(chunks)

    async def render_request(self,url,folder,resource_type,network_interface,records):
        """Retry transient gateway replies before extraction, keeping each body/hash."""
        asset=resource_type in ('stylesheet','script','image','font')
        attempts=3 if (resource_type=='document' and (urlsplit(url).hostname or '').removeprefix('www.')=='ryazanbrick.ru') else 1
        for attempt in range(attempts):
            status,headers,body=await self.request(url,browser_resource=asset,network_interface=network_interface)
            import hashlib
            records.append(dict(url=url,status=status,resource_type=resource_type,attempt=attempt+1,
                                bytes=len(body),sha256=hashlib.sha256(body).hexdigest()))
            if resource_type=='document':
                (folder/('http_document_'+str(len(records))+'.html')).write_bytes(body)
            if status not in (502,503,504) or attempt+1==attempts:
                return status,headers,body
            await asyncio.sleep(max(1,self.limits['host_delay']))

    async def fetch(self,url,folder,page):
        delay=self.limits['host_delay']-(time.monotonic()-self.last)
        if delay>0:await asyncio.sleep(delay)
        self.last=time.monotonic()
        if page.get('transport','http')=='browser':return await self.browser(url,folder,page)
        current=url;redirects=[]
        for _ in range(6):
            status,headers,body=await self.request(current)
            redirects.append(dict(url=current,status=status))
            if status in (301,302,303,307,308):
                current=urljoin(current,headers.get('Location',headers.get('location','')))
                self.policy.check_url(current);continue
            if status!=200:raise Unavailable('HTTP '+str(status))
            ctype=headers.get('Content-Type',headers.get('content-type','')).lower()
            if 'html' not in ctype:raise Unavailable('Ответ не HTML: '+ctype)
            from bs4 import UnicodeDammit
            decoded=UnicodeDammit(body,is_html=True)
            text=decoded.unicode_markup
            if text is None:raise Unavailable('Не удалось декодировать HTML')
            (folder/'response.body').write_bytes(body)
            return dict(html=text,final_url=current,method='fresh_http',redirects=redirects,
                        visibility='static_html_only',content_type=ctype,encoding=decoded.original_encoding,
                        tls_exception=urlsplit(current).hostname in self.policy.insecure_hosts)
        raise Unavailable('Лимит редиректов')

    async def browser(self,url,folder,page):
        from playwright.async_api import async_playwright
        http1 = (urlsplit(url).hostname or '').removeprefix('www.') in ('konord.com','rvul.ru')
        mappings=[]
        for host in sorted(self.policy.hosts):
            try:
                ips = await self.policy.resolve('https://' + host)
            except socket.gaierror:
                if host == urlsplit(url).hostname:
                    raise
                mappings.append('MAP ' + host + ' ~NOTFOUND')
            else:
                mappings.append('MAP ' + host + ' ' + browser_address(ips))
        blocked=[];actions=[];http_resources=[]
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,
                env={k:v for k,v in os.environ.items() if k!='CONTROL_MODEL_API_KEY'},
                args=['--no-sandbox','--disable-dev-shm-usage',
                *(['--disable-http2'] if http1 else []),
                '--host-resolver-rules='+','.join(mappings)])
            try:
                context=await browser.new_context(service_workers='block',accept_downloads=False,
                    **({'user_agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'} if http1 else {}),
                    ignore_https_errors=urlsplit(url).hostname in self.policy.insecure_hosts,
                    extra_http_headers={'Cache-Control':'no-cache, no-store','Pragma':'no-cache'})
                if not hasattr(context,'route_web_socket'):raise Unavailable('Нет блокировки WebSocket в установленном Playwright')
                await context.route_web_socket('**/*',lambda ws:ws.close())
                async def route(request_route):
                    request=request_route.request
                    try:
                        self.policy.check_browser_request(request.url, request.method, request.resource_type, page.get('asset_hosts', []))
                        if urlsplit(url).hostname in self.policy.insecure_hosts and urlsplit(request.url).hostname not in self.policy.insecure_hosts:
                            raise ValueError('Хост вне явного TLS-исключения браузерного контекста')
                        if request.resource_type=='document' and self.page_check:self.page_check(request.url)
                        if request.method not in ('GET','HEAD') and not public_dealer_read(request.method, request.url, url):
                            raise ValueError('Метод браузера запрещён')
                    except ValueError:
                        blocked.append(dict(url=request.url,method=request.method))
                        await request_route.abort();return
                    if page.get('fresh_http_render') and request.method=='GET' and (urlsplit(url).hostname or '').removeprefix('www.') in ('v-kirpich.ru','ryazanbrick.ru'):
                        # Live HTTP bodies, not saved HTML. The same hostname/TLS/DNS
                        # perimeter applies to every request and redirect.
                        if urlsplit(request.url).path.startswith('/gde-kupit/black-list/'):
                            blocked.append(dict(url=request.url,method=request.method));await request_route.abort();return
                        status,headers,body=await self.render_request(request.url,folder,request.resource_type,page.get('network_interface'),http_resources)
                        forwarded={k:v for k,v in headers.items() if k.lower() not in ('content-length','content-encoding','transfer-encoding','connection')}
                        await request_route.fulfill(status=status,headers=forwarded,body=body)
                        return
                    await request_route.continue_()
                await context.route('**/*',route)
                tab=await context.new_page();tab.set_default_timeout(self.limits['fetch_seconds']*1000)
                tab.set_default_navigation_timeout(self.limits['fetch_seconds']*1000)
                response=await tab.goto(url,wait_until='domcontentloaded')
                if response is None or response.status!=200:raise Unavailable('Браузер не получил HTTP 200')
                dealer_list = (urlsplit(url).hostname in ('rvul.ru','www.rvul.ru')
                    and urlsplit(url).path == '/gde-kupit/'
                    and page.get('wait_for_selector') == '.dynamic-map-list__item')
                # setList публикует список за один синхронный проход после getDealers.
                # Фоновые запросы карты не определяют готовность контактных данных.
                fubag = urlsplit(url).hostname == 'fubag.ru'
                if fubag:
                    # Live stock and checkout widgets keep the network busy after
                    # product and office data have already been rendered.
                    content = {'product': '.ordered-block.char .product-details-card__spec-value',
                               'company': '.left_block_store', 'negative': '#pagetitle'}[page['role']]
                    await tab.locator(content).first.wait_for(state='visible')
                if not dealer_list and not fubag:
                    await tab.wait_for_load_state('networkidle')
                    if (urlsplit(url).hostname or '').removeprefix('www.')=='albanydoorsystems.ru' and await tab.locator('.tabs__content_active').count():
                        await tab.wait_for_function("""() => [...document.querySelectorAll('.tabs__content_active')].every(el => {
                            const s=getComputedStyle(el);
                            return s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity)>=.99;
                        })""")
                selectors=page.get('reference',{}).get('selectors') or ['body']
                await tab.locator(page.get('wait_for_selector',selectors[0])).first.wait_for(state='visible')
                # visible у Playwright допускает opacity=0. Дожидаемся реального
                # появления выбранного содержимого, не меняя CSS страницы.
                await tab.wait_for_function("""selector => {
                    let node=document.querySelector(selector);
                    if (!node) return false;
                    for (;node;node=node.parentElement) {
                        const style=getComputedStyle(node);
                        if (Number(style.opacity)<0.99 || style.visibility==='hidden'
                            || style.display==='none') return false;
                    }
                    return true;
                }""", arg=page.get('wait_for_selector',selectors[0]))
                await tab.screenshot(path=str(folder/'before.png'),full_page=True)
                for selector in page.get('interactions',[]):
                    target=tab.locator(selector)
                    if await target.count()!=1:raise Unavailable('Неоднозначный контроль: '+selector)
                    # A preceding public tab click may still be animating.
                    # Await actual visibility; never reveal a hidden element by CSS.
                    try:
                        await target.wait_for(state='visible',timeout=min(10000,self.limits['fetch_seconds']*1000))
                    except Exception as exc:
                        raise Unavailable('Невидимый контроль после ожидания: '+selector) from exc
                    is_djtab = await target.evaluate("el => el.matches('.djtabs-title[data-tab-no]')")
                    await target.click();actions.append(selector)
                    await tab.wait_for_load_state('networkidle')
                    if (urlsplit(url).hostname or '').removeprefix('www.')=='albanydoorsystems.ru':
                        await tab.wait_for_function("""() => [...document.querySelectorAll('.tabs__content_active')].every(el => {
                            const s=getComputedStyle(el);
                            return s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity)>=.99;
                        })""")
                    if is_djtab:
                        await tab.wait_for_function("""selector => {
                            const control = document.querySelector(selector);
                            const group = control.closest('.djtabs');
                            const panel = [...group.querySelectorAll('.djtabs-body')].find(
                                el => el.dataset.tabNo === control.dataset.tabNo);
                            return panel && getComputedStyle(panel).opacity === '1'
                                && panel.getBoundingClientRect().height > 0;
                        }""", arg=selector)
                    # Для нескольких раскрытий сохраняется каждое публичное
                    # состояние: последнее раскрытие может закрыть предыдущее.
                    if (urlsplit(url).hostname or '').removeprefix('www.') in ('knauf.ru','emufta.com'):
                        await tab.wait_for_function("""selector => {
                            const control=document.querySelector(selector);
                            const panel=control.closest('.expandable, .faq-accordion__item');
                            return panel && (panel.classList.contains('open') || panel.classList.contains('active'));
                        }""", arg=selector)
                        await tab.screenshot(path=str(folder/f'interaction_{len(actions):02d}.png'),full_page=True)
                        (folder/f'interaction_{len(actions):02d}.html').write_text(await tab.content())
                font_rendering = None
                if (urlsplit(url).hostname or '').removeprefix('www.') == 'alumil-yug.ru':
                    # Fixed-height Calibri blocks require evidence from this
                    # isolated browser, not from a separate diagnostic process.
                    session=await context.new_cdp_session(tab)
                    await session.send('DOM.enable');await session.send('CSS.enable')
                    doc=await session.send('DOM.getDocument')
                    nodes=await session.send('DOM.querySelectorAll',{'nodeId':doc['root']['nodeId'],'selector':'#text1 font, #text1 b'})
                    fonts=[]
                    for node_id in nodes['nodeIds']:
                        used=await session.send('CSS.getPlatformFontsForNode',{'nodeId':node_id})
                        fonts.extend(used.get('fonts',[]))
                    geometry=await tab.evaluate('''() => {
                        const box=document.querySelector('#text1');
                        if (!box) return null;
                        const rect=box.getBoundingClientRect();
                        return {box:rect.toJSON(),paragraphs:[...box.querySelectorAll('.wpmd > div')].map(el=>({
                            text:el.innerText.trim(),rect:el.getBoundingClientRect().toJSON(),
                            fully_inside:el.getBoundingClientRect().bottom<=rect.bottom}))};
                    }''')
                    font_rendering=dict(fontconfig=os.environ.get('FONTCONFIG_FILE'),fonts=fonts,geometry=geometry)
                    (folder/'font_rendering.json').write_text(json.dumps(font_rendering,ensure_ascii=False,indent=2))
                    if geometry and any(p['text'] and not p['fully_inside'] for p in geometry['paragraphs']):
                        raise Unavailable('Публичный текст Alumil обрезан: см. font_rendering.json')
                    if page.get('role')=='product' and not any(f['familyName'] in ('Carlito','Calibri') for f in fonts):
                        raise Unavailable('Не подтверждён шрифт Calibri/Carlito для Alumil')
                from product_interactions import capture_public_product_links_html
                html=await capture_public_product_links_html(tab)
                if len(html.encode())>self.limits['html_bytes']:raise Unavailable('Лимит DOM')
                self.policy.check_url(tab.url)
                if self.page_check:self.page_check(tab.url)
                await tab.screenshot(path=str(folder/'after.png'),full_page=True)
                return dict(html=html,final_url=tab.url,method='fresh_http_browser_render' if page.get('fresh_http_render') else 'fresh_browser',visibility='screenshots_and_actions',
                            network_interface=page.get('network_interface'),
                            browser_http1=http1,http_resource_responses=http_resources,font_rendering=font_rendering,
                            content_readiness='public_dealer_list' if dealer_list else 'networkidle_and_selector',
                            interactions=actions,blocked_requests=blocked,
                            tls_exception=urlsplit(url).hostname in self.policy.insecure_hosts)
            finally:await browser.close()
