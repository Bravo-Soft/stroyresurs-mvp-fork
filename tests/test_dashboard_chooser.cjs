// Проверки реального JS страницы без браузера и обращений к живому пайплайну.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../dashboard/static/index.html'), 'utf8');
const companies = Array.from({length: 2004}, (_, i) => ({
  company_id: String(925700001 + i),
  name: `Компания ${String(i + 1).padStart(4, '0')}`,
  website: `https://company-${i + 1}.example`,
}));

async function chooser() {
  let context;
  const requests = [];
  const decode = value => value.replace(/&(quot|lt|gt|amp);/g,
    (_, key) => ({quot: '"', lt: '<', gt: '>', amp: '&'})[key]);
  const runHandler = (source, target) =>
    vm.compileFunction(source, [], {parsingContext: context}).call(target);
  function element() {
    return {
      value: '', checked: false, textContent: '', inputs: [],
      addEventListener() {},
      showModal() { this.open = true; },
      close() { this.open = false; },
      set innerHTML(value) {
        this.markup = value;
        this.inputs = Array.from(value.matchAll(/<input\b([^>]*)>/g), ([, attrs]) => ({
          value: decode(attrs.match(/\bvalue="([^"]*)"/)[1]),
          checked: /\bchecked\b/.test(attrs),
          onchange: attrs.match(/\bonchange="([^"]*)"/)?.[1],
        }));
      },
      get innerHTML() { return this.markup || ''; },
      querySelectorAll(selector) {
        if (selector === 'input:checked') return this.inputs.filter(i => i.checked);
        assert.equal(selector, 'input[type=checkbox]');
        return this.inputs;
      },
    };
  }
  const elements = Object.fromEntries(Array.from(html.matchAll(/\bid="([^"]+)"/g),
    ([, id]) => [id, element()]));
  elements['st-resume'].checked = true;
  context = vm.createContext({
    localStorage: {getItem: () => null},
    document: {
      getElementById(id) { assert.ok(elements[id], id); return elements[id]; },
      querySelectorAll(selector) { assert.equal(selector, 'nav button'); return []; },
      querySelector(selector) {
        assert.equal(selector, 'input[name=st-mode]:checked');
        return {value: 'full'};
      },
    },
    async fetch(url, options) {
      requests.push({url, ...options});
      assert.ok(['/api/v1/companies', '/api/v1/pipeline/start'].includes(url), url);
      const data = url.endsWith('/companies') ? {companies} : {pid: 123, mode: 'full'};
      return {status: 200, ok: true, text: async () => JSON.stringify(data)};
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  // Автообновление статуса не относится к выбору компаний.
  vm.runInContext('loadStatus = async () => {};', context);
  await context.openStart();
  return {
    context, elements, requests,
    ids: () => Array.from(context.selectedCompanyIds()),
    rows: () => elements['st-list'].inputs,
    filter(query) {
      elements['st-filter'].value = query;
      const tag = html.match(/<input\b[^>]*\bid="st-filter"[^>]*>/)[0];
      runHandler(tag.match(/\boninput="([^"]*)"/)[1], elements['st-filter']);
    },
    check(id, checked = true) {
      const input = elements['st-list'].inputs.find(i => i.value === id);
      assert.ok(input, `Компания ${id} видна`);
      input.checked = checked;
      runHandler(input.onchange, input);
    },
  };
}

test('выбор сохраняется между запросами, возвратом к компании и снятием галочки', async () => {
  const ui = await chooser();
  ui.filter('Компания 0001');
  ui.check(companies[0].company_id);
  ui.filter('Компания 0002');
  assert.deepEqual(ui.ids(), [companies[0].company_id]);
  ui.check(companies[1].company_id);
  assert.equal(ui.elements['st-count'].textContent, 'отмечено 2');
  ui.filter('Компания 0001');
  assert.equal(ui.rows()[0].checked, true);
  ui.check(companies[0].company_id, false);
  ui.filter('');
  assert.deepEqual(ui.ids(), [companies[1].company_id]);
  assert.equal(ui.rows().find(i => i.value === companies[1].company_id).checked, true);
});

test('30 компаний из 2004 передаются на запуск даже при пустой выдаче поиска', async () => {
  const ui = await chooser();
  const chosen = companies.slice(0, 30);
  for (const company of chosen) {
    ui.filter(company.name);
    ui.check(company.company_id);
  }
  ui.filter('нет такой компании');
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.elements['st-count'].textContent, 'отмечено 30');
  await ui.context.doStart();
  const request = ui.requests.find(r => r.url === '/api/v1/pipeline/start');
  assert.equal(request.method, 'POST');
  assert.deepEqual(JSON.parse(request.body), {
    mode: 'full', resume: true, company_ids: chosen.map(c => c.company_id),
  });
});

test('поиск по полному ID и его части; ID виден в строке результата', async () => {
  const ui = await chooser();
  ui.filter('  925700001  ');
  assert.deepEqual(ui.rows().map(i => i.value), ['925700001']);
  assert.match(ui.elements['st-list'].innerHTML, />[^<]*ID: 925700001/);
  ui.filter('92570000');
  assert.deepEqual(ui.rows().map(i => i.value), companies.slice(0, 9).map(c => c.company_id));
});

test('поиск по названию и сайту сохраняет нечувствительность к регистру', async () => {
  const ui = await chooser();
  ui.filter('  КОМПАНИЯ 0001  ');
  assert.deepEqual(ui.rows().map(i => i.value), [companies[0].company_id]);
  ui.filter('COMPANY-2.EXAMPLE');
  assert.deepEqual(ui.rows().map(i => i.value), [companies[1].company_id]);
});

test('Все и Снять меняют только видимые компании и сохраняют скрытый выбор', async () => {
  const ui = await chooser();
  ui.filter('Компания 0001');
  ui.check(companies[0].company_id);
  ui.filter('Компания 0002');
  ui.context.setAllCompanies(true);
  assert.deepEqual(ui.ids().sort(), [companies[0].company_id, companies[1].company_id]);
  ui.context.setAllCompanies(false);
  assert.deepEqual(ui.ids(), [companies[0].company_id]);
  ui.filter('нет такой компании');
  ui.context.setAllCompanies(false);
  assert.deepEqual(ui.ids(), [companies[0].company_id]);
  ui.filter('');
  assert.equal(ui.rows()[0].checked, true);
  assert.equal(ui.rows()[1].checked, false);
});

test('без фильтра Все и Снять работают со всем ситлистом', async () => {
  const ui = await chooser();
  ui.context.setAllCompanies(true);
  assert.equal(ui.ids().length, 2004);
  assert.equal(ui.rows().every(i => i.checked), true);
  ui.context.renderChooser();
  assert.equal(ui.rows().every(i => i.checked), true);
  ui.context.setAllCompanies(false);
  assert.deepEqual(ui.ids(), []);
  assert.equal(ui.rows().some(i => i.checked), false);
});

test('пустой выбор передаётся как null — сохраняется запуск всего ситлиста', async () => {
  const ui = await chooser();
  await ui.context.doStart();
  const request = ui.requests.find(r => r.url === '/api/v1/pipeline/start');
  assert.equal(JSON.parse(request.body).company_ids, null);
});

test('повторное открытие окна сохраняет выбор скрытой поиском компании', async () => {
  const ui = await chooser();
  ui.filter('Компания 0001');
  ui.check(companies[0].company_id);
  ui.filter('Компания 0002');
  ui.elements['dlg-start'].close();
  await ui.context.openStart();
  ui.filter('Компания 0001');
  assert.deepEqual(ui.ids(), [companies[0].company_id]);
  assert.equal(ui.rows()[0].checked, true);
});
