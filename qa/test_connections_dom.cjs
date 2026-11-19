/**
 * Unit tests for connection handlers in a simulated DOM, NOT visual/browser QA.
 * No provider credentials, real network, browser automation or paid calls.
 * Run: NODE_PATH=/path/to/dev-jsdom/node_modules node --test qa/test_connections_dom.cjs
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const repo = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(repo, 'static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'static/connections.js'), 'utf8');
const clone = (value) => JSON.parse(JSON.stringify(value));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const reply = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => clone(body) });
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
async function until(predicate, message = 'DOM state did not settle') {
  const deadline = Date.now() + 3500;
  while (!predicate()) { if (Date.now() > deadline) throw new Error(message); await sleep(10); }
}
function model(id, price_class = 'free', name = id) {
  return { id, name, price_class, price_label: { free: 'Gratuito', paid: 'Pago', unknown: 'Preço não informado' }[price_class], chat_compatible: true };
}
const defaults = [model('openrouter/free'), ...Array.from({ length: 229 }, (_, i) => model(`free/model-${String(i).padStart(3, '0')}`)), ...Array.from({ length: 80 }, (_, i) => model(`paid/model-${i}`, 'paid')), model('unknown/model', 'unknown')];
async function fixture(options = {}) {
  const calls = [];
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error.message));
  const dom = new JSDOM(html, { url: 'https://studio.example.test/', runScripts: 'outside-only', virtualConsole });
  const win = dom.window;
  const state = {
    providers: options.providers || [{ id: 'openrouter-test', name: 'OpenRouter', kind: 'openrouter', model_count: defaults.length, free_model_count: 230, free_router_available: true, key_saved: true }],
    models: options.models || defaults,
    selected: options.selected || {},
  };
  const list = (search) => {
    const q = search.get('q') || '';
    const cost = search.get('cost') || 'all';
    const offset = Number(search.get('offset') || 0);
    const limit = Number(search.get('limit') || 100);
    const matching = state.models.filter((m) => `${m.name} ${m.id}`.toLowerCase().includes(q.toLowerCase()));
    const counts = { all: matching.length, free: matching.filter((m) => m.price_class === 'free').length, paid: matching.filter((m) => m.price_class === 'paid').length, unknown: matching.filter((m) => m.price_class === 'unknown').length };
    const filtered = matching.filter((m) => cost === 'all' || m.price_class === cost);
    return { models: filtered.slice(offset, offset + limit), total: filtered.length, offset, limit, has_more: offset + limit < filtered.length, counts };
  };
  win.fetch = async (url, init = {}) => {
    const parsed = new URL(url, 'https://studio.example.test');
    const call = { pathname: parsed.pathname, search: parsed.searchParams, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null };
    calls.push(call);
    if (options.intercept) {
      const result = options.intercept(call, { state, list, reply });
      if (result !== undefined) return await result;
    }
    if (call.pathname === '/api/social/accounts') return reply({ networks: [] });
    if (call.pathname === '/api/creative/workflows') return reply({ workflows: [] });
    if (call.pathname === '/api/ai/catalog') return reply({ source: { url: 'https://github.com/open-free-llm-api/awesome-freellm-apis' }, notice: 'Cada provedor tem suas cotas. Use sua própria chave.', providers: [{ id: 'groq', name: 'Groq', kind: 'custom', endpoint: 'https://api.groq.com/openai/v1', key_url: 'https://console.groq.com/keys', price_note: 'Plano gratuito sujeito às condições da conta.', quota_note: 'Os limites dependem do modelo.' }] });
    if (call.pathname === '/api/ai/providers' && call.method === 'GET') return reply({ providers: state.providers, selected: state.selected, stages: { moments: 'Melhores momentos', titles: 'Títulos e descrições' } });
    if (call.pathname.endsWith('/models') && call.method === 'GET') return reply(list(call.search));
    if (call.pathname === '/api/ai/selection' && call.method === 'POST') {
      state.selected[call.body.stage] = { provider_id: call.body.provider_id, model_id: call.body.model_id, free_only: state.providers.find((p) => p.id === call.body.provider_id)?.kind === 'openrouter' && state.models.find((m) => m.id === call.body.model_id)?.price_class === 'free' };
      return reply({ stage: call.body.stage, model_id: call.body.model_id });
    }
    if (call.pathname.endsWith('/test') && call.method === 'POST') return reply({ ok: true, model_id: 'free/replied-model', message: 'A IA gratuita respondeu ao teste.' });
    if (call.pathname.endsWith('/free-defaults') && call.method === 'POST') {
      const id = call.pathname.split('/')[4];
      for (const stage of ['moments', 'titles']) state.selected[stage] = { provider_id: id, model_id: 'openrouter/free', free_only: true };
      return reply({ selected: state.selected, message: 'Seleção automática gratuita ativada para momentos e títulos.' });
    }
    if (call.pathname === '/api/ai/providers' && call.method === 'POST') return reply({ detail: 'Chave rejeitada pelo provedor (401/403)' }, 422);
    throw new Error(`Unexpected fixture request: ${call.method} ${call.pathname}`);
  };
  win.eval(script);
  await until(() => !win.document.querySelector('#model-picker').hasAttribute('aria-busy') && win.document.querySelector('#provider-preset').options.length > 1, 'initial connection data');
  return { dom, win, state, calls, errors, $: (selector) => win.document.querySelector(selector), change(selector, value, event = 'change') { const input = win.document.querySelector(selector); input.value = value; input.dispatchEvent(new win.Event(event, { bubbles: true })); }, close() { dom.window.close(); } };
}

test('presets fill the official endpoint and clear the previous key', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  f.$('#custom-key').value = 'placeholder-only';
  f.change('#provider-preset', 'groq');
  assert.equal(f.$('#custom-endpoint').value, 'https://api.groq.com/openai/v1');
  assert.equal(f.$('#custom-endpoint').readOnly, true);
  assert.equal(f.$('#custom-name').value, 'Groq');
  assert.equal(f.$('#preset-key-link').href, 'https://console.groq.com/keys');
  assert.equal(f.$('#custom-key').value, '');
  f.change('#provider-preset', 'custom');
  assert.equal(f.$('#custom-endpoint').readOnly, false);
  assert.equal(f.$('#custom-endpoint').value, '');
  assert.deepEqual(f.errors, []);
});

test('OpenRouter defaults to free models and loads the first 100', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  assert.equal(f.$('#model-cost').value, 'free');
  assert.equal(f.$('#model-choice').options.length, 100);
  assert.match(f.$('#model-choice').options[0].text, /Automático · somente gratuitos/);
  assert.ok([...f.$('#model-choice').options].every((option) => !option.value.startsWith('paid/')));
  assert.match(f.$('#model-count').textContent, /100 de 230/);
  assert.equal(f.$('#load-more-models').classList.contains('hidden'), false);
});

test('pagination appends another 100 without dropping the selected model', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  f.change('#model-choice', 'free/model-005');
  f.$('#load-more-models').click();
  await until(() => f.$('#model-choice').options.length === 200);
  assert.equal(f.$('#model-choice').value, 'free/model-005');
  assert.ok(f.calls.some((call) => call.pathname.endsWith('/models') && call.search.get('offset') === '100' && call.search.get('limit') === '100'));
  assert.match(f.$('#model-count').textContent, /200 de 230/);
});

test('price filter reaches the API and rapid search input is debounced', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  f.change('#model-cost', 'paid');
  await until(() => f.$('#model-choice').options.length === 80);
  assert.ok([...f.$('#model-choice').options].every((option) => option.value.startsWith('paid/')));
  const before = f.calls.length;
  f.change('#model-search', 'p', 'input');
  f.change('#model-search', 'paid/model-7', 'input');
  await until(() => f.$('#model-choice').options.length === 11 && !f.$('#model-choice').disabled);
  const searches = f.calls.slice(before).filter((call) => call.pathname.endsWith('/models'));
  assert.equal(searches.length, 1);
  assert.equal(searches[0].search.get('q'), 'paid/model-7');
  assert.equal(searches[0].search.get('cost'), 'paid');
});

test('a late response cannot replace the newer search even if transport ignores abort', async (t) => {
  const late = deferred();
  const f = await fixture({ intercept(call) {
    if (call.pathname.endsWith('/models') && call.search.get('q') === 'slow') return late.promise;
    if (call.pathname.endsWith('/models') && call.search.get('q') === 'fast') return reply({ models: [model('free/fast')], total: 1, has_more: false, counts: { free: 1 } });
  } }); t.after(() => f.close());
  f.change('#model-search', 'slow', 'input');
  await until(() => f.calls.some((call) => call.search.get('q') === 'slow'));
  f.change('#model-search', 'fast', 'input');
  await until(() => f.$('#model-choice').value === 'free/fast');
  late.resolve(reply({ models: [model('free/slow')], total: 1, has_more: false, counts: { free: 1 } }));
  await sleep(30);
  assert.equal(f.$('#model-choice').value, 'free/fast');
  assert.equal(f.$('#model-choice').disabled, false);
});

test('saving sends the intended provider, model and task and shows the persisted choice', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  f.change('#model-stage', 'titles');
  f.change('#model-choice', 'free/model-005');
  f.$('#save-model').click();
  await until(() => f.$('#model-feedback').textContent.includes('Modelo salvo para títulos'));
  const writes = f.calls.filter((call) => call.pathname === '/api/ai/selection');
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, { stage: 'titles', provider_id: 'openrouter-test', model_id: 'free/model-005' });
  assert.match(f.$('#selected-models').textContent, /free\/model-005/);
});

test('the free test button makes exactly one POST while busy', async (t) => {
  const pending = deferred();
  const f = await fixture({ intercept(call) { if (call.pathname.endsWith('/test')) return pending.promise; } }); t.after(() => f.close());
  const button = f.$('button[data-action="test"]');
  button.click(); button.click();
  assert.equal(button.disabled, true);
  assert.equal(f.calls.filter((call) => call.pathname.endsWith('/test') && call.method === 'POST').length, 1);
  pending.resolve(reply({ ok: true, model_id: 'free/replied-model', message: 'A IA gratuita respondeu ao teste.' }));
  await until(() => !button.disabled);
  assert.match(f.$('.provider-feedback').textContent, /free\/replied-model/);
  assert.match(f.$('.provider-feedback').textContent, /respondeu/);
});

test('invalid key feedback is clear and never echoes the submitted key', async (t) => {
  const f = await fixture(); t.after(() => f.close());
  const dummyKey = 'synthetic-invalid-key-do-not-echo';
  f.$('#openrouter-key').value = dummyKey;
  f.$('#openrouter-form').dispatchEvent(new f.win.Event('submit', { bubbles: true, cancelable: true }));
  await until(() => f.$('#connection-feedback').textContent.includes('Chave rejeitada'));
  assert.ok(!f.$('#connection-feedback').textContent.includes(dummyKey));
  assert.ok(!f.$('#model-feedback').textContent.includes(dummyKey));
  assert.equal(f.$('#openrouter-form button').disabled, false);
  assert.equal(f.calls.filter((call) => call.pathname === '/api/ai/providers' && call.method === 'POST').length, 1);
});

test('a custom provider using the reserved ID is not presented as automatic free routing', async (t) => {
  const f = await fixture({ providers: [{ id: 'custom-test', name: 'Custom', kind: 'custom', free_router_available: false, key_saved: true, model_count: 1, free_model_count: 0 }], models: [model('openrouter/free', 'unknown', 'Automático gratuito')], selected: { titles: { provider_id: 'custom-test', model_id: 'openrouter/free', free_only: true }, moments: { provider_id: 'custom-test', model_id: 'openrouter/free', free_only: true } } }); t.after(() => f.close());
  assert.equal(f.$('#model-cost').value, 'all');
  assert.match(f.$('#model-choice').options[0].text, /^Preço não informado · openrouter\/free$/);
  assert.match(f.$('#model-price-note').textContent, /não está confirmado/);
  assert.ok(!f.$('#selected-models').textContent.includes('Automático gratuito'));
  assert.equal(f.$('.free-active'), null);
  assert.equal(f.$('button[data-action="test"]'), null);
});

test('no matching free model disables saving and explains the empty result', async (t) => {
  const f = await fixture({ models: [model('only/paid', 'paid')] }); t.after(() => f.close());
  assert.equal(f.$('#model-choice').disabled, true);
  assert.equal(f.$('#save-model').disabled, true);
  assert.match(f.$('#model-count').textContent, /Preço não informado não é classificado como gratuito/);
  assert.deepEqual(f.errors, []);
});
