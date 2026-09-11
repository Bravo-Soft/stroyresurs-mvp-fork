# config.py
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:    
    """Конфигурация системы мониторинга"""
    # Пути
    excel_path: str = '/home/user/stroy-resurs/mvp/Site_list/Site_list.xlsx'
    base_dir: str = '/home/user/stroy-resurs/mvp/Base'
    reports_dir: str = '/home/user/stroy-resurs/mvp/Reports'
    vector_db_path: str = '/home/user/stroy-resurs/mvp/VectorDB'
    documents_dir: str = '/home/user/stroy-resurs/mvp/Documents'
    logs_dir: str = '/home/user/stroy-resurs/mvp/logs'
#    base_dir: str = '/app/Base'
#    reports_dir: str = '/app/Reports'
#    vector_db_path: str = '/app/VectorDB'
#    documents_dir: str = '/app/Documents'
    
    # AI Tunnel API — эмбеддинги (text-embedding-3-small) и профилирование сайтов.
    ai_tunnel_url: str = 'https://api.aitunnel.ru/v1'
    ai_tunnel_api_key: str = 'sk-aitunnel-xy8z6s26DjHQh2pggVwernvQH6NMYUmA'

    # Ollama Cloud API — агрегатор LLM для SGR-извлечения товаров/компаний/дистрибьюторов
    # (ai_integration.AITunnelClient). Используется OpenAI-совместимый эндпоинт Ollama Cloud
    # (https://ollama.com/v1), ключ берётся из .env (OLLAMA_API_KEY).
    # ollama_model — id модели, как его отдаёт GET https://ollama.com/v1/models
    # (без суффикса ':cloud' — он только для CLI-загрузки).
    # Модель выбрана по итогам сравнения 8 кандидатов на реальном markdown (Анализ системы/_model_ab +
    # _prod_validation) по критерию СТРОГОЙ верности тексту (без перевода ключей) + стабильности:
    #   - gemini-3-flash-preview ПЕРЕВОДИТ русские заголовки («Высота»→«High») → отвергнут;
    #   - glm-5.2 верна тексту и быстрая (~6с/стр), но имена/классификация «плавают» (риск product_id);
    #   - deepseek-v4-flash верна тексту (0 переводов) И стабильнее (мягкий разброс имён, консистентная
    #     классификация, 0 сбоев парсинга) — ВЫБРАНА (стабильность важнее скорости). Минус — ~10× медленнее.
    # Архитектура/тюнинг пер-модельно (ai_integration): v4-flash → 2-вызовная SGR (single_1call у неё
    # ломается), thinking ВКЛ (prod_2call — её лучший конфиг), _clean_json_response.
    ollama_url: str = 'https://ollama.com/v1'
    ollama_api_key: str = field(default_factory=lambda: os.getenv('OLLAMA_API_KEY', ''))
    # 2026-07-27: прод переведён на gemma4:31b (полный офлайн-прогон 1142 стр. 42 комп. + A/B промпта:
    #   полнота ТХ медиана 1.00 vs deepseek, перевод ключей 0, дисциплина полей 0 нарушений;
    #   отчёт: mvp/Анализ системы/_gemma4_test/REPORT.md).
    # 2026-08-18: по решению заказчика прод переводился на deepseek-v4-flash:0731 (закреплённый тег,
    #   https://ollama.com/library/deepseek-v4-flash, 2-вызовная SGR). На тестовом прогоне 11 компаний
    #   выявлен «убегающий reasoning» (пустой content, ~7% страниц; лечение: max_tokens=16384 +
    #   анти-луп ретраи temp=0.7 — реализовано и сохранено в ai_integration._MODEL_TUNING).
    # 2026-08-19: для прогона-переписи 2082 возвращена gemma4:31b (single-call) — решение оператора.
    #   Возврат на deepseek: OLLAMA_MODEL=deepseek-v4-flash:0731 (или правка этой строки) —
    #   его тюнинг и анти-луп ретраи сохранены и включатся автоматически.
    ollama_model: str = 'gemma4:31b'

    # Параметры для фильтрации изображений
    min_image_size_kb = 9  # Минимальный размер изображений в КБ
    
    # Graph DB API настройки
    graph_db_api_url: str = "https://stroy-resurs-db.bravo-soft.ru/api/companies"
    graph_db_enable: bool = False
    graph_db_max_retries: int = 3
    graph_db_request_timeout: int = 600
    graph_db_chunk_size_products: int = 5 
    graph_db_chunk_size_distributors: int = 10  
    graph_db_save_json_on_error: bool = True
    graph_db_json_output_dir: str = "/home/user/stroy-resurs/mvp/GraphDB_JSON"
#    graph_db_json_output_dir: str = "/app/GraphDB_JSON"
    graph_db_enable_chunking: bool = True
    graph_db_upload_products: bool = True
    graph_db_upload_distributors: bool = True
    graph_db_upload_company: bool = True
    graph_db_delay_between_chunks: float = 2.0  # клиентский троттлинг заливки; возвращено с 0.3 (перф-тюнинг 14.08) по решению оператора 17.08
    graph_db_delay_between_companies: float = 2.0  # возвращено с 0.3 (перф-тюнинг 14.08) по решению оператора 17.08

    # Graph DB Архивация
    graph_db_archive_dir: str = "/home/user/stroy-resurs/mvp/GraphDB_Arch"  # Директория для архивации данных
#    graph_db_archive_dir: str = "/app/GraphDB_Arch"  # Директория для архивации данных
    graph_db_archive_days_to_keep: int = 30  # Количество дней хранения архивов
    graph_db_enable_archiving: bool = True  # Включить архивацию отправляемых данных

    # Graph DB Pending
    graph_db_pending_dir: str = "/home/user/stroy-resurs/mvp/GraphDB_Pending"
#    graph_db_archive_dir: str = "/app/GraphDB_Pending"
    graph_db_max_retries_on_resume: int = 5
    graph_db_resume_delay_seconds: int = 5
    
    # Настройки конвертера файлов
    enable_file_conversion: bool = True
    libreoffice_path: str = "/usr/bin/soffice"
#    libreoffice_path: str = "/bin/soffice"
    converter_max_workers: int = 4  # Среднее количество потоков; >4 — только с изоляцией профилей soffice
    converter_timeout: int = 300  # 5 минут таймаут
    delete_empty_folders: bool = True
    delete_originals_after_conversion: bool = True
    converter_log_file: str = "file_conversion.log"
    exclude_product_cards_rtf = True
        
    # Настройки краулера
    max_pages_per_site: int = 250  # общий лимит страниц/сайт для диагностического прогона (Анализ системы/Краулер.md §7)
    max_product_pages_per_site: int = 50  # верхняя граница продуктовых страниц/сайт; лимит учитывается при постановке URL в очередь (Краулер.md §4,§7)
    max_depth: int = 6  
    request_delay: float = 0.3  
    max_pagination_depth: int = 40  
    max_playwright_retries: int = 3
    # D101: сторож зависаний. Меряется ПРОСТОЙ (время без единой записи в лог), а не общее
    # время компании: крупные сайты честно обрабатываются до нескольких суток, и дедлайн на
    # компанию резал бы их. Порог 30 мин обоснован замерами: самая длинная пауза здоровой
    # системы — 560 с (ожидание LLM), p99.99 пауз = 335 с, самый долгий одиночный блокирующий
    # вызов — запрос к LLM с таймаутом 900 с. Простой сверх порога = зависание: компания
    # бросается, пул браузеров пересоздаётся, прогон идёт дальше. 0 отключает сторож.
    company_idle_timeout_seconds: int = 1800
    memory_check_interval_pages: int = 50  
    memory_cleanup_threshold_mb: int = 20000  # порог RSS до очистки; держать НИЖЕ mem_limit контейнера (~24 ГБ)
    max_concurrent_contexts: int = 25
    # P04 U2: минимум URL хоста, по которым определяется признак «плоский сайт»
    # (нет ни одного товарного/каталожного сегмента). Значение консервативное:
    # на меньшей выборке признак ненадёжен, порог живым прогоном не измерен.
    flat_site_min_urls: int = 5
    # P04 U1: предохранитель «фильтр съел сайт». Если по домену доля URL, отсеянных
    # глобальным exclude-списком (или языковым фильтром), достигла порога, а очередь
    # обхода опустела — фильтр отключается до конца компании и отложенные URL
    # возвращаются в обход. Пороги консервативные, живым прогоном не измерены.
    exclude_failopen_ratio: float = 0.8      # доля отсеянных URL, при которой фильтр снимается
    exclude_failopen_min_urls: int = 20      # меньше этого числа URL доля недостоверна
    exclude_failopen_max_deferred: int = 500 # потолок буфера отложенных URL (память)
    # P04 U4: доля товарной квоты, которую занимают только адреса, похожие на КАРТОЧКУ
    # (глубина >= 2 с товарным слагом, идентификатор товара в query, лист .htm/.html).
    # Листинги каталога неотличимы от карточек по URL и выбирали все 50 слотов раньше,
    # чем обход доходил до карточек глубины 2-3 (D100). Значение консервативное и
    # живым прогоном не измерено; 0 отключает резерв (прежнее поведение).
    product_quota_card_reserve: float = 0.3
    # P04 U4: приоритет очереди для ссылок, найденных в товарных сетках листинга
    # (ProductGridParser). Выше диапазона карты сайта (10..13), потому что сетка —
    # подтверждённая разметкой карточка, а карта сайта отдаёт вперемешку и листинги.
    product_grid_link_priority: int = 14
    # P04 U3: потолок страниц второго прохода товарного извлечения (роли
    # category/price_list/other). Запускается, только если товарных карточек нет
    # совсем, и стоит денег LLM, поэтому значение консервативное; 0 отключает проход.
    second_pass_max_pages: int = 15
    
    # Настройки HTTP-first слоя (curl_cffi impersonate + cookie-warmup).
    # Требуется пакет curl_cffi (pip install curl_cffi); без него слой авто-отключается.
    http_first_enabled: bool = True          # пробовать HTTP-impersonate до Playwright
    antibot_in_fetch_enabled: bool = True    # при antibot-челлендже делать cookie-warmup и повтор
    impersonate_profile: str = "chrome"      # TLS/HTTP2-отпечаток curl_cffi
    http_verify_tls: bool = True             # проверка TLS-сертификата в HTTP-first
    http_timeout: int = 30                   # таймаут HTTP-first запроса, сек
    warmup_delay_seconds: float = 1.0        # пауза между прогревом главной и повтором цели
    http_min_content: int = 800              # минимум символов HTML, чтобы счесть HTTP-ответ достаточным
    js_render_text_threshold: int = 300      # порог текста для needs_javascript
    # Гейт «оболочки» товарной страницы: минимум видимого текста, ниже которого HTTP-ответ
    # считается пустой оболочкой и страница эскалируется в Playwright. Обоснование порога —
    # в data_island.looks_like_product_shell. 0 отключает гейт (прежнее поведение).
    product_min_text: int = 1000

    # Stealth Playwright-контекст (обход детекта автоматизации антибот-системами).
    stealth_context_enabled: bool = True     # реалистичный контекст вместо голого new_context()
    stealth_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )  # держать согласованным с impersonate_profile и актуальной версией Chrome

    # Настройки карты сайта
    sitemap_discovery_enabled = True
    sitemap_timeout_seconds = 10
    sitemap_max_urls = 500
    sitemap_max_depth = 6
    sitemap_retry_attempts = 1
    sitemap_retry_delay = 2
    sitemap_initial_batch_size: int = 500
    sitemap_processing_batch_size: int = 500
    # D144/D208: сколько <url> карты сайта категоризуется перед ранжированием и кэпом
    # sitemap_max_urls. Кэп теперь применяется ПОСЛЕ категоризации, а не в порядке
    # документа, поэтому разбирать приходится больше адресов; потолок нужен, чтобы карта
    # на десятки тысяч записей не съела 30-секундный таймаут фазы карты. Консервативно
    # выше любой карты корпуса прогона 2082 (максимум — 10 498 <loc> у kpp33.ru).
    sitemap_max_scan_urls: int = 20000
    # P02 U1: потолок переиспользования слота товарной квоты за компанию. Слот теперь
    # возвращается на путях неуспеха товарной страницы (нет содержимого, HTML < 300
    # символов, 404/410); без потолка сайт со сплошными отказами крутил бы квоту, пока
    # не упрётся в max_pages_per_site. Консервативно = одна полная квота.
    product_quota_reuse_limit: int = 50
    # P02 U1: сколько товарных адресов КАРТЫ САЙТА (глубина 0) подряд могут не отдать
    # содержимое, прежде чем хвост карты вычищается из очереди, а слоты возвращаются.
    # Выше порога soft-404 (stale_sitemap_dup_threshold = 10): жёсткий отказ бывает и от
    # таймаутов медленного сайта, и ложное срабатывание здесь дороже пропущенного.
    stale_sitemap_hard_fail_threshold: int = 15
    # D154: сколько подкарт индекса разбирать. 0 = все (прежний жёсткий срез [:5]
    # отрезал товарные инфоблоки Bitrix, которые нумеруются последними). При ненулевом
    # значении первыми берутся карты с product/catalog/shop/iblock в имени.
    sitemap_max_submaps: int = 0
    # Потолок времени на всю фазу карты сайта (обнаружение + разбор всех подкарт).
    # Был захардкожен 30 с при срезе в 5 подкарт; после снятия среза объём разбора у
    # сайтов с большими индексами вырос, поэтому поднят консервативно.
    sitemap_phase_timeout_seconds: int = 90
    
    # Настройки для обработки динамических страниц
    dynamic_content_enabled: bool = True
    dynamic_tab_selectors: list = field(default_factory=lambda: [
        "button", "[role='tab']", ".tab", ".tabs__item", ".tabs-item",
        ".contacts__tabs-item", ".city-tab"
    ])
    dynamic_accordion_selectors: list = field(default_factory=lambda: [
        ".accordion__header", ".accordion__title", ".toggle", ".expand",
        ".show-more", ".contacts__details-toggle", ".requisites__toggle"
    ])
    dynamic_click_delay_ms: int = 400
    dynamic_save_tabs_separately: bool = True
    # D01: домены, у которых ТОВАРНЫЕ страницы держат характеристики в AJAX-вкладках
    # (контент вкладки подгружается по клику и отсутствует в первичном DOM).
    # Для них товарная страница рендерится браузером с кликами по вкладкам, контент
    # вкладок СКЛЕИВАЕТСЯ в один HTML (не отдельными #tab-страницами — иначе дедуп
    # по имени товара теряет характеристики, регресс РУФ-135 попытки №1).
    ajax_product_tabs_domains: list = field(default_factory=lambda: [
        'tizol.com',     # МБОР: вкладка «Характеристики» (D01)
        # cooltech.ru НЕ добавлять: его вкладки лежат скрытыми в DOM (не AJAX),
        # а «Размеры» = чертёж с легендой A–G, которую адаптер D22 убирает намеренно
    ])
    # D82: домены на Bitrix-шаблоне, где блок «Исполнение и цена» (типоразмеры/фасеты/цена
    # конкретного изделия) рендерится AngularJS отдельным AJAX-запросом product_offers.php и
    # в статическом HTML пуст (тег <product-container product-container="N">). Для таких доменов
    # краулер дотягивает /local/ajax/product_offers.php?section_id=N&lang=ru и вклеивает таблицу
    # исполнений в HTML до конвертации в markdown — иначе страница теряет типоразмеры и отсеивается
    # гейтом ≥3 числовых характеристик (betar.ru: 9 товаров -> Trash_418#, variants пусты).
    bitrix_offers_domains: list = field(default_factory=lambda: [
        'betar.ru',
    ])
    # Селекторы панели с контентом активной вкладки (для склейки)
    dynamic_tab_panel_selectors: list = field(default_factory=lambda: [
        "[role='tabpanel']", ".tab-content", ".tabs-content", ".tab-pane",
        ".tabs__content", ".tab__content", ".tab-body",
        ".product-page__section-content",  # tizol.com (Vue): панель активной вкладки товара
    ])

    # Настройки фильтрации печатных версий
    filter_print_versions: bool = True
    print_version_params: list = field(default_factory=lambda: ['print', 'view', 'mode', 'format', 'action'])
    print_version_values: list = field(default_factory=lambda: ['y', '1', 'on', 'yes', 'true', 'print'])
    print_version_paths: list = field(default_factory=lambda: [
        '/print/', '/print-version/', '/print_page/', '/printview/', '/printable/'
    ])

    # Слова-исключения (N7): товар не собирается, если его название содержит одно из этих слов
    # (сопоставление по целому слову, регистронезависимо). Список заказчика по ПРОМ-ТОВАР.РФ —
    # бытовые товары, не относящиеся к стройматериалам. Глобальный; расширять/чистить здесь.
    excluded_product_name_patterns: list = field(default_factory=lambda: [
        'губка', 'марля', 'мыло', 'салфетка'
    ])
    
    # Параллельная обработка
    max_concurrent_companies: int = 3
    max_concurrent_pages: int = 20
    
    # Настройки файлов
    max_file_size_mb: int = 30  
    temp_storage_max_size_mb: int = 512000  
        
    # Настройки векторной БД
    vector_db_enable: bool = False  # Заливка данных в векторную БД + создание эмбеддингов; по умолчанию упразднено
    embedding_model: str = "text-embedding-3-small"
    collection_name: str = "website_products"
    
    # Настройки токенов и стоимости
    price_per_million_input_tokens: float = 53.76
    price_per_million_output_tokens: float = 80.64
    price_per_million_embedding_tokens: float = 3.6   
    
    # Настройки генерации карточек
    product_cards_dir: str = '/home/user/stroy-resurs/mvp/Documents'    
#    product_cards_dir: str = '/app/Documents'      
    
    # Система восстановления
    checkpoint_file: str = 'processing_checkpoint.json'  
    recovery_max_attempts: int = 3
    
    # Настройка чекпоинтов
    checkpoint_frequency_minutes: int = 30
    checkpoint_frequency_pages: int = 20
    checkpoint_enable_stages: bool = True
    checkpoint_keep_history: bool = True
    checkpoint_max_history: int = 10
    
    # Настройки оптимизации LLM
    rate_limit_requests_per_10s: int = 60
    max_concurrent_llm_requests: int = 3   # Ollama Cloud Pro: не более 3 одновременных запросов к LLM
    llm_request_timeout: int = 30

    # Настройки потоковой обработки страниц (конвейер внутри компании):
    # товарные страницы уходят в markdown/LLM сразу по мере сохранения краулером,
    # не дожидаясь конца краула. Включается только при наличии кэша actual_name
    # (Base/ActualNames) — иначе product_id зависел бы от ещё не известного имени
    # производителя; chelaz.ru всегда идёт старым батч-путём (D85 требует полного
    # списка страниц до извлечения).
    pipeline_streaming_enabled: bool = True
    pipeline_page_workers: int = 6  # воркеры конвейера: 3 слота LLM + запас на markdown/скачивания
    # Общий лимит одновременных скачиваний файлов/картинок (FileDownloadManager):
    # при стриминге скачивания идут параллельно с краулом того же домена, поэтому
    # суммарная нагрузка на сайт ограничена: краул <= max_concurrent_pages + файлы <= этот лимит.
    max_concurrent_file_downloads: int = 5

    # Настройки домена
    ignore_subdomains: bool = True  # Игнорировать поддомены (включая www)
    domain_strict_www: bool = True  # Считать www.example.com и example.com одним доменом
    log_skipped_subdomains: bool = False  # Логировать пропущенные поддомены
    treat_http_https_as_same: bool = True  # Считать http и https одним сайтом
    domain_equivalency_enabled: bool = True  # Включить распознавание эквивалентных доменов
    prefer_https: bool = True  # Предпочитать https при выборе схемы

    equivalent_domains: dict = field(default_factory=lambda: {
        "vmp-holding.ru": ["vmp-anticor.ru", "vmp-plamcor.ru", "vmp-goodline.ru"]
    })

    # D69: пер-доменное исключение разделов URL (скоуплено ПО ДОМЕНУ, не глобально).
    # Ключ — домен (netloc без www), значение — подстроки пути, которые для ЭТОГО домена не
    # краулятся. У pspcom.ru раздел /catalog/build/ — девелоперское направление (жилые
    # микрорайоны/посёлки: «Микрорайон Гагарина» с таблицей «технико-экономических показателей»
    # ошибочно шёл товаром), а не стройматериалы. Глобально сегмент 'build' исключать НЕЛЬЗЯ
    # (на других сайтах это может быть продуктовый раздел). Пустой для прочих доменов → нулевой регресс.
    exclude_url_patterns_by_domain: dict = field(default_factory=lambda: {
        "pspcom.ru": ["/catalog/build/"],
        # D89: раздел /practice/ на pktmt.ru — «Применение» (информационные статьи «что
        # такое ППУ», «утепление балкона/кровли/откосов» …), а НЕ товары. Статьи с ТХ
        # ошибочно проходили как товарные карточки. Товары лежат в /produktsiya/.
        "pktmt.ru": ["/practice/"],
        # D98: aerobel.ru — краулер обходил весь сайт вширь и жёг бюджет max_pages на
        # некоммерческих разделах (архив новостей, портфолио проектов/объектов, услуги,
        # ЛК/корзина, производство) через медленный Playwright → прогон ~3ч, извлечение
        # начиналось только после докраула до лимита. Товары — в /catalog/, дилеры — в
        # /cooperation/ (where___List, ~700 записей), контакты — /contacts//company//about/;
        # эти разделы НЕ исключаем. /news/ безопасно исключить: список дилеров на
        # /cooperation/, а /news/uvazhaemye_partnery — лишь новость-анонс, не список.
        "aerobel.ru": ["/news/", "/our_projects/", "/our_objects/", "/services/",
                       "/policy/", "/personal/", "/manufacturing/"],
    })

    # Домены, отдающие контент ТОЛЬКО на www. У pktmt.ru non-www 301-редиректит в
    # /index.php → 404 (и подстраницы, и главная-подпуть). Канонизатор по умолчанию
    # срезает www (domain_strict_www) → effective_url для резолва ссылок становится
    # non-www → все найденные товарные ссылки уходят на non-www и отдают 404 (0 товаров).
    # Для доменов из списка канонизируем К www (а не срезаем его). Ключ — «голый» домен
    # без www. Скоуплено ПО ДОМЕНУ; пусто для прочих → нулевой регресс.
    strict_www_domains: list = field(default_factory=lambda: ["pktmt.ru"])

    # Выбор рабочего URL компании (P05 U1): кандидат подтверждается содержимым, а не одним
    # кодом ответа. Отклик живых сайтов не измерялся — значения выбраны консервативно.
    working_url_probe_timeout: int = 20   # таймаут одной пробы кандидата, сек (было жёстко 15)
    working_url_min_content: int = 300    # тело короче — заглушка, а не сайт; совпадает с
                                          # порогом сохранения страницы в краулере
    # Маркеры заглушек и панелей хостера в теле ответа (нижний регистр, подстрока).
    # Кандидат с таким телом не рабочий: живой сайт компании лежит на соседнем кандидате
    # (D148 reg.ru, D205 Beget, D258 ISPmanager, D241 дефолтный vhost).
    hoster_stub_markers: list = field(default_factory=lambda: [
        "домен не привязан к хостингу",   # reg.ru
        "домен не прилинкован",           # Beget
        "/ispmgr",                        # ISPmanager
        "/manimg/",                       # ISPmanager
        "<title>authorization</title>",   # страница входа ISPmanager
        "defaultwebpage.cgi",             # дефолтная страница cPanel
        "web server's default page",      # дефолтная страница Plesk
        "welcome to nginx!",              # дефолтный vhost nginx
        "apache2 ubuntu default page",    # дефолтный vhost apache
    ])

    # Редиректы и смена хоста при выборе базы обхода (P05 U2).
    working_url_shim_max_len: int = 2000  # тело короче — ищем в нём редирект-шим (meta refresh
                                          # или единственный <script> с location); длина шимов
                                          # не измерена, порог взят с запасом к 189 б parkgroup
    # Домены хостеров: конечный хост редиректа в этом списке (или путь /parking) — припаркованный
    # домен, а не переезд сайта (D167 vh444.timeweb.ru/parking).
    parking_hoster_domains: list = field(default_factory=lambda: [
        "timeweb.ru", "timeweb.cloud", "reg.ru", "beget.com", "beget.ru",
    ])

    # Профилирование сайтов (декларативные YAML-профили + перепись/метрики).
    # Пер-доменные словари выше (ajax_product_tabs_domains, bitrix_offers_domains,
    # exclude_url_patterns_by_domain, strict_www_domains, equivalent_domains) —
    # DEPRECATED-fallback: их содержимое перенесено в profiles/<домен>.yaml,
    # словари остаются на переходный период (выпиливание — фаза 2).
    profiles_dir: str = '/home/user/stroy-resurs/mvp/profiles'
    profiles_drafts_dir: str = '/home/user/stroy-resurs/mvp/profiles_drafts'
    profile_metrics_dir: str = '/home/user/stroy-resurs/mvp/Base/profile_metrics'
    census_enabled: bool = False          # режим переписи: писать черновики профилей
    profile_accept_confidence: float = 0.8  # порог автопринятия черновика (пост-обработка)

    # Настройки Kafka
    kafka_bootstrap_servers: str = "192.168.0.15:9092"
    kafka_topic_regular_tasks: str = "regular_tasks"
    kafka_topic_urgent_tasks: str = "urgent_tasks"
    kafka_topic_task_status: str = "task_status"
    kafka_topic_pipeline_control: str = "pipeline_control"
    kafka_consumer_group: str = "monitoring_pipeline"
    kafka_auto_offset_reset: str = "earliest"
    kafka_max_poll_interval_ms: int = 7200000      # 2 часа
    kafka_session_timeout_ms: int = 3600000        # 1 час
    kafka_heartbeat_interval_ms: int = 300000      # 5 минут
        
    # Настройки управления задачами
    kafka_check_urgent_interval: float = 5.0
    kafka_max_urgent_queue: int = 10  

    @property
    def id_system_enabled(self) -> bool:
        return True
    
    @property
    def max_files_per_product(self) -> int:
        return 50
    
    @property
    def id_validation_enabled(self) -> bool:
        return True
        
    @classmethod
    def load_from_env(cls):
        """Загрузка конфигурации из переменных окружения"""
        config = cls()
        config.excel_path = os.getenv('EXCEL_PATH', config.excel_path)
        config.ai_tunnel_api_key = os.getenv('AI_TUNNEL_API_KEY', config.ai_tunnel_api_key)       
        # Ollama Cloud (SGR-извлечение)
        config.ollama_url = os.getenv('OLLAMA_URL', config.ollama_url)
        config.ollama_api_key = os.getenv('OLLAMA_API_KEY', config.ollama_api_key)
        config.ollama_model = os.getenv('OLLAMA_MODEL', config.ollama_model)
        config.graph_db_api_url = os.getenv('GRAPH_DB_API_URL', config.graph_db_api_url)
        config.graph_db_enable = os.getenv('GRAPH_DB_ENABLE', str(config.graph_db_enable)).lower() == 'true'
        config.vector_db_enable = os.getenv('VECTOR_DB_ENABLE', str(config.vector_db_enable)).lower() == 'true'
        config.graph_db_pending_dir = os.getenv('GRAPH_DB_PENDING_DIR', config.graph_db_pending_dir)
        config.graph_db_max_retries_on_resume = int(os.getenv('GRAPH_DB_MAX_RETRIES_ON_RESUME', config.graph_db_max_retries_on_resume))
        config.graph_db_resume_delay_seconds = int(os.getenv('GRAPH_DB_RESUME_DELAY_SECONDS', config.graph_db_resume_delay_seconds))
        config.enable_file_conversion = os.getenv('ENABLE_FILE_CONVERSION', str(config.enable_file_conversion)).lower() == 'true'
        config.libreoffice_path = os.getenv('LIBREOFFICE_PATH', config.libreoffice_path)
        config.treat_http_https_as_same = os.getenv('TREAT_HTTP_HTTPS_AS_SAME', str(config.treat_http_https_as_same)).lower() == 'true'
        config.domain_equivalency_enabled = os.getenv('DOMAIN_EQUIVALENCY_ENABLED', str(config.domain_equivalency_enabled)).lower() == 'true'
        config.prefer_https = os.getenv('PREFER_HTTPS', str(config.prefer_https)).lower() == 'true'
        
        # Загружаем настройки Kafka
        config.kafka_bootstrap_servers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', config.kafka_bootstrap_servers)
        config.kafka_topic_regular_tasks = os.getenv('KAFKA_TOPIC_REGULAR_TASKS', config.kafka_topic_regular_tasks)
        config.kafka_topic_urgent_tasks = os.getenv('KAFKA_TOPIC_URGENT_TASKS', config.kafka_topic_urgent_tasks)
        config.kafka_consumer_group = os.getenv('KAFKA_CONSUMER_GROUP', config.kafka_consumer_group)
        # Рычаг конкуренции краула. Окружение = 14 vCPU / 53 ГБ (не полный Threadripper); выбран бюджет 1/2 ≈
        # 7 CPU / 26 ГБ (mem_limit mvp ~24 ГБ) -> дефолт = 20 (кормит 3 слота LLM с запасом). Больше — через env.
        config.max_concurrent_pages = int(os.getenv('MAX_CONCURRENT_PAGES', config.max_concurrent_pages))
        # D101: порог простоя для сторожа зависаний (сек). 0 отключает сторож.
        config.company_idle_timeout_seconds = int(os.getenv('COMPANY_IDLE_TIMEOUT_SECONDS', config.company_idle_timeout_seconds))
        # Гейт «оболочки» товарной страницы (симв. видимого текста). 0 отключает.
        config.product_min_text = int(os.getenv('PRODUCT_MIN_TEXT', config.product_min_text))

        # Настройки потоковой обработки страниц
        config.pipeline_streaming_enabled = os.getenv('PIPELINE_STREAMING_ENABLED', str(config.pipeline_streaming_enabled)).lower() == 'true'
        config.pipeline_page_workers = int(os.getenv('PIPELINE_PAGE_WORKERS', config.pipeline_page_workers))
        config.max_concurrent_file_downloads = int(os.getenv('MAX_CONCURRENT_FILE_DOWNLOADS', config.max_concurrent_file_downloads))

        # Профилирование сайтов
        config.census_enabled = os.getenv('CENSUS_ENABLED', str(config.census_enabled)).lower() == 'true'

        return config