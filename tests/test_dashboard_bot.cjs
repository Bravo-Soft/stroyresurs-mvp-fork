// Реальные обработчики страницы; HTTP и DOM заменены локальными объектами.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../dashboard/static/index.html'), 'utf8');
const flush = () => new Promise(setImmediate);

function page() {
  function element(tag = '') {
    let classes = new Set((tag.match(/\bclass="([^"]*)"/)?.[1] || '').split(/\s+/));
    return {
      checked: /\bchecked\b/.test(tag), disabled: /\bdisabled\b/.test(tag),
      value: '', textContent: '', innerHTML: '', scrollHeight: 500,
      inlineHandler: tag.match(/\bonclick="([^"]*)"/)?.[1],
      tbody: {innerHTML: ''},
      classList: {add: c => classes.add(c), remove: c => classes.delete(c),
        contains: c => classes.has(c)},
      set className(value) { classes = new Set(value.split(/\s+/)); },
      get className() { return [...classes].join(' '); },
      addEventListener() {},
      querySelector(selector) { assert.equal(selector, 'tbody'); return this.tbody; },
      click() { return this.onclick(); },
    };
  }
  const body = html.split('<script>')[0];
  const elements = Object.fromEntries(Array.from(body.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g),
    ([tag, id]) => [id, element(tag)]));
  const nav = Array.from(body.matchAll(/<button\b([^>]*data-screen="([^"]+)"[^>]*)>([^<]+)<\/button>/g),
    ([tag, , screen, label]) => Object.assign(element(tag), {dataset: {screen}, label}));
  const requests = [], intervals = [];
  const data = {
    status: {available: true, pending: false, phase: 'idle', error: null},
    bot: {lines: ['Бот: запрос пользователя', '<script>текст лога</script>']},
    rebuild: {lines: ['Сборка готова']},
    failure: null,
    delay: null,
    companies: [],
  };
  const context = vm.createContext({
    localStorage: {getItem: () => null},
    setInterval(callback, delay) { intervals.push({callback, delay}); return intervals.length; },
    document: {
      getElementById(id) { assert.ok(elements[id], id); return elements[id]; },
      querySelectorAll(selector) {
        if (selector === 'nav button') return nav;
        assert.equal(selector, '.screen');
        return Object.values(elements).filter(e => e.classList.contains('screen'));
      },
      querySelector(selector) {
        assert.equal(selector, 'nav button.active');
        return nav.find(n => n.classList.contains('active'));
      },
    },
    async fetch(url, options) {
      requests.push({url, ...options});
      assert.equal(options.headers.Authorization, 'Bearer test-operator');
      let result;
      if (url === '/api/v1/bot/status') result = data.status;
      else if (url === '/api/v1/logs/tail?lines=300&source=all') result = data.all || data.bot;
      else if (url === '/api/v1/logs/tail?lines=300&source=bot') result = data.bot;
      else if (url === '/api/v1/logs/tail?lines=300&source=monitoring') result = data.monitoring;
      else if (url === '/api/v1/logs/tail?lines=300&source=stdout') result = data.stdout;
      else if (url === '/api/v1/logs/tail?lines=300&source=bot-rebuild') result = data.rebuild;
      else if (url === '/api/v1/bot/restart') result = {pending: true, phase: 'building'};
      else if (url === '/api/v1/metrics/companies') result = {companies: data.companies};
      else assert.fail('Неожиданный запрос: ' + url);
      const failure = data.failure && data.failure(url);
      if (data.delay) await data.delay(url);
      return {status: failure ? failure.status : 200, ok: !failure,
        text: async () => JSON.stringify(failure ? {detail: failure.detail} : result)};
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  vm.runInContext(`TOKEN = 'test-operator';
    loadSiteList = loadStatus = loadSummary = loadCheckpoint = loadHealth = async () => {};`, context);
  for (const item of Object.values(elements)) {
    if (item.inlineHandler) item.onclick = () => vm.runInContext(item.inlineHandler, context);
  }
  return {context, elements, nav, requests, intervals, data,
    open: name => nav.find(n => n.dataset.screen === name).click()};
}

test('порядок вкладок: Обзор, Прогон, Компания, Бот, Настройки, Журнал', () => {
  const ui = page();
  assert.deepEqual(ui.nav.map(n => n.label), ['Обзор', 'Прогон', 'Компания', 'Бот', 'Настройки', 'Журнал']);
});

test('открытие вкладки загружает статус, логи бота и логи пересборки', async () => {
  const ui = page();
  ui.open('bot');
  await flush();
  assert.equal(ui.elements['s-bot'].classList.contains('active'), true);
  assert.equal(ui.elements['s-overview'].classList.contains('active'), false);
  assert.equal(ui.requests.length, 3);
  assert.equal(ui.elements['bot-log-pre'].textContent,
    'Бот: запрос пользователя\n<script>текст лога</script>');
  assert.equal(ui.elements['bot-log-pre'].innerHTML, '');
  assert.equal(ui.elements['bot-rebuild-pre'].textContent, 'Сборка готова');
  assert.equal(ui.elements['btn-bot-restart'].disabled, false);
});

test('автообновление каждые 10 с действует только на активной вкладке', async () => {
  const ui = page();
  ui.context.boot();
  ui.open('bot');
  await flush();
  const tick = ui.intervals.find(i => i.delay === 10000).callback;
  ui.requests.length = 0;
  tick();
  await flush();
  assert.equal(ui.requests.length, 3);
  ui.elements['bot-auto'].checked = false;
  ui.requests.length = 0;
  tick();
  await flush();
  assert.deepEqual(ui.requests.map(r => r.url), ['/api/v1/bot/status']);
  ui.open('company');
  ui.requests.length = 0;
  tick();
  await flush();
  assert.equal(ui.requests.length, 0);
});

test('ручное обновление работает с выключенным автообновлением', async () => {
  const ui = page();
  ui.elements['bot-auto'].checked = false;
  await ui.context.loadBot();
  assert.equal(ui.requests.length, 3);
});

test('пересборка отправляет только POST бота и блокирует кнопку до завершения', async () => {
  const ui = page();
  await ui.context.doBotRestart();
  const posts = ui.requests.filter(r => r.method === 'POST');
  assert.deepEqual(posts.map(r => r.url), ['/api/v1/bot/restart']);
  assert.equal(ui.elements['btn-bot-restart'].disabled, true);
  assert.equal(ui.elements['bot-state'].textContent, 'Пересборка…');
  ui.data.status.phase = 'succeeded';
  await ui.context.loadBotStatus();
  assert.equal(ui.elements['btn-bot-restart'].disabled, false);
  assert.equal(ui.elements['bot-state'].textContent, 'Бот пересобран и запущен');
});

test('отсутствие Docker и ошибка пересборки отображаются без сообщения об успехе', async () => {
  const ui = page();
  Object.assign(ui.data.status, {available: false, detail: 'Нет доступа к Docker'});
  await ui.context.loadBotStatus();
  assert.equal(ui.elements['btn-bot-restart'].disabled, true);
  assert.equal(ui.elements['bot-status-detail'].textContent, 'Нет доступа к Docker');
  Object.assign(ui.data.status, {available: true, phase: 'failed', error: 'Ошибка сборки'});
  await ui.context.loadBotStatus();
  assert.equal(ui.elements['bot-state'].textContent, 'Ошибка пересборки или запуска');
  assert.equal(ui.elements['bot-status-detail'].textContent, 'Ошибка сборки');
});

test('отсутствующий файл и ошибка чтения логов видны оператору', async () => {
  const ui = page();
  ui.data.bot = {lines: [], detail: 'файл ещё не создан'};
  await ui.context.loadBotLog();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'файл ещё не создан');
  ui.data.failure = () => ({status: 503, detail: 'Лог недоступен'});
  await ui.context.loadBotLog();
  assert.match(ui.elements['bot-log-pre'].textContent, /Не удалось загрузить.*Лог недоступен/);
});

test('повторный POST с ответом 409 сохраняет состояние текущей пересборки', async () => {
  const ui = page();
  Object.assign(ui.data.status, {pending: true, phase: 'starting'});
  ui.data.failure = url => url.endsWith('/restart') ? {status: 409, detail: 'Уже выполняется'} : null;
  await ui.context.doBotRestart();
  assert.equal(ui.elements['btn-bot-restart'].disabled, true);
  assert.equal(ui.elements['bot-state'].textContent, 'Запуск…');
  assert.match(ui.elements['bot-msg'].innerHTML, /Уже выполняется/);
});

test('настоящая кнопка Обновить читает новые строки при выключенном автообновлении', async () => {
  const ui = page();
  ui.elements['bot-auto'].checked = false;
  await ui.elements['btn-bot-refresh'].click();
  ui.data.bot = {lines: ['Нажатие кнопки', 'Инвайт удалён'], modified_at: '2026-10-23T08:05:01'};
  ui.data.rebuild = {lines: ['Пересборка завершена'], modified_at: '2026-10-23T08:05:02'};
  await ui.elements['btn-bot-refresh'].click();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Нажатие кнопки\nИнвайт удалён');
  assert.equal(ui.elements['bot-rebuild-pre'].textContent, 'Пересборка завершена');
  assert.match(ui.elements['bot-log-updated'].textContent, /Прочитано:.*Файл изменён: 2026-10-23 08:05:01/);
  assert.ok(ui.requests.filter(r => r.url.includes('/logs/tail')).every(r => r.cache === 'no-store'));
});

test('автообновление показывает появившиеся записи и прекращает чтение после снятия флажка', async () => {
  const ui = page();
  ui.context.boot(); ui.open('bot'); await flush();
  const tick = ui.intervals.find(i => i.delay === 10000).callback;
  ui.data.bot = {lines: ['Новая операция бота']};
  tick(); await flush();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Новая операция бота');
  ui.elements['bot-auto'].checked = false;
  ui.data.bot = {lines: ['Следующая операция']};
  tick(); await flush();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Новая операция бота');
  await ui.elements['btn-bot-refresh'].click();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Следующая операция');
});

test('запоздавший ответ не заменяет свежие записи старым логом', async () => {
  const ui = page();
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  ui.data.bot = {lines: ['Старый лог']};
  ui.data.delay = url => url.endsWith('source=bot') ? wait : undefined;
  const oldRequest = ui.context.loadBotLog();
  ui.data.delay = null;
  ui.data.bot = {lines: ['Свежий лог']};
  await ui.context.loadBotLog();
  release(); await oldRequest;
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Свежий лог');
});

test('после ошибки чтения кнопка Обновить восстанавливает содержимое и время', async () => {
  const ui = page();
  ui.data.failure = () => ({status: 503, detail: 'Временная ошибка'});
  await ui.elements['btn-bot-refresh'].click();
  assert.equal(ui.elements['bot-log-updated'].textContent, 'Ошибка обновления');
  ui.data.failure = null; ui.data.bot = {lines: ['Запись после восстановления']};
  await ui.elements['btn-bot-refresh'].click();
  assert.equal(ui.elements['bot-log-pre'].textContent, 'Запись после восстановления');
  assert.match(ui.elements['bot-log-updated'].textContent, /^Прочитано:/);
});

test('таблица Прогон выводит режимы и сохраняет полный текст длинного статуса', async () => {
  const ui = page();
  ui.data.companies = ['initial', 'update', null].map(processing_mode => ({
    name: 'Лаборатория огнезащиты НПФ, ООО', company_id: '896711983', kind: 'regular',
    status: 'no_products_processed', processing_mode, duration_seconds: 540,
    pages: {company: 1}, products: 0, errors: ['Нет товаров'],
  }));
  await ui.context.loadRunCompanies();
  const markup = ui.elements['run-table'].tbody.innerHTML;
  assert.match(markup, /Первичная загрузка/);
  assert.match(markup, /Обновление/);
  assert.match(markup, /Не указан/);
  assert.equal((markup.match(/no_products_processed/g) || []).length, 3);
  assert.equal((markup.match(/<td>/g) || []).length, 27);
  assert.match(html, /#run-table th \{ white-space:normal;/);
  assert.match(html, /#run-table \.pill \{[^}]*white-space:normal/);
  assert.match(html, /#run-table th, #run-table td \{[^}]*overflow-wrap:anywhere/);
});

test('на вкладке Бот доступны только бот и формирование отчётов, текст лога безопасен', async () => {
  const ui = page();
  ui.data.bot = {lines: ['report_service: Отчёт', 'Бот: запрос <script>'],
    detail: 'Бот: часть файлов ещё не создана'};
  await ui.context.loadBotLog();
  assert.match(ui.requests.at(-1).url, /source=bot$/);
  assert.equal(ui.elements['bot-log-pre'].textContent, ui.data.bot.lines.join('\n'));
  assert.equal(ui.elements['bot-log-pre'].innerHTML, '');
  assert.match(ui.elements['bot-log-updated'].textContent, /часть файлов ещё не создана/);
  const selector = html.match(/<select id="bot-log-source"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...selector.matchAll(/value="([^"]+)"/g)].map(m => m[1]), ['bot']);
});

test('источники пайплайна переключаются в блоке Прогон', async () => {
  const ui = page();
  ui.data.monitoring = {lines: ['ai_integration: извлечение', 'docx_generator: карточка']};
  ui.data.stdout = {lines: ['Ошибка запуска пайплайна']};
  for (const [source, expected] of [['monitoring', ui.data.monitoring], ['stdout', ui.data.stdout]]) {
    ui.elements['log-source'].value = source;
    await ui.context.loadLogTail();
    assert.equal(ui.elements['log-pre'].textContent, expected.lines.join('\n'));
    assert.ok(ui.requests.some(r => r.url.endsWith('source=' + source)));
  }
});

test('отложенный ответ бота не заменяет журнал пайплайна', async () => {
  const ui = page();
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  ui.data.delay = url => url.endsWith('source=bot') ? wait : undefined;
  const pending = ui.context.loadBotLog();
  ui.elements['log-source'].value = 'monitoring';
  ui.data.monitoring = {lines: ['web_crawler: свежий лог']};
  await ui.context.loadLogTail();
  release(); await pending;
  assert.equal(ui.elements['log-pre'].textContent, 'web_crawler: свежий лог');
  assert.equal(ui.elements['bot-log-pre'].textContent, ui.data.bot.lines.join('\n'));
});
