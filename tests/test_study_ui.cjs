const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const elements = new Map();
  const keys = {};
  const buttons = Array.from({ length: 4 }, () => ({ disabled: false, addEventListener() {} }));
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { style: {}, textContent: '', value: '',
        classList: { add: v => classes.add(v), remove: v => classes.delete(v),
          contains: v => classes.has(v), toggle(v, force) {
            if (force === undefined) force = !classes.has(v);
            if (force) classes.add(v); else classes.delete(v);
          } }, addEventListener() {} });
    }
    return elements.get(id);
  }
  let send;
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: element,
      querySelectorAll: selector => selector === '.rate-btn' ? buttons : [],
      addEventListener: (type, listener) => { keys[type] = listener; } },
    Date, URLSearchParams, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      if (url === '/api/status') return { json: async () => ({
        overall: { total: 1 }, batches: [], new_waiting: 1, review_due: 0,
      }) };
      requests.push(JSON.parse(options.body));
      return new Promise((resolve, reject) => { send = { resolve, reject }; });
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  const state = (version = 'first') => ({
    done: false, version, base_total: 2, rated_count: 0, queue_left: 1,
    card: { card_id: 42, direction: 'zh2en', item_type: 'phrase', en: 'look after', zh: '照顾' },
  });
  context.renderState(state());
  context.flipCard();
  return { context, element, buttons, requests, keys, state, send: () => send };
}

test('rapid clicks submit exactly one rating with the displayed card and version', async () => {
  const f = fixture();
  const pending = f.context.submitRating(3);
  await f.context.submitRating(4);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].card_id, 42);
  assert.equal(f.requests[0].version, 'first');
  assert(f.buttons.every(btn => btn.disabled));
  f.send().resolve({ ok: true, status: 200, json: async () => f.state('second') });
  await pending;
  assert(f.buttons.every(btn => !btn.disabled));
  assert(f.element('card-back').classList.contains('hidden'));
});

test('failed request keeps the card available for retry with the same version', async () => {
  const f = fixture();
  const pending = f.context.submitRating(3);
  f.send().reject(new Error('network failure'));
  await pending;
  assert.equal(f.element('study-error').textContent, 'network failure');
  assert(f.buttons.every(btn => !btn.disabled));
  const retry = f.context.submitRating(3);
  assert.equal(f.requests[1].version, 'first');
  f.send().resolve({ ok: true, status: 200, json: async () => f.state('second') });
  await retry;
});

test('stale response refreshes the card and requires flipping before another rating', async () => {
  const f = fixture();
  const pending = f.context.submitRating(3);
  f.send().resolve({ status: 409, ok: false,
    json: async () => ({ error: '请重新翻面评分', state: f.state('second') }) });
  await pending;
  await f.context.submitRating(4);
  assert.equal(f.requests.length, 1);
  assert.equal(f.element('study-error').textContent, '请重新翻面评分');
  f.context.flipCard();
  const next = f.context.submitRating(4);
  assert.equal(f.requests[1].version, 'second');
  f.send().resolve({ ok: true, status: 200, json: async () => f.state('third') });
  await next;
});

test('holding a rating key does not submit more ratings', () => {
  const f = fixture();
  f.keys.keydown({ repeat: true, key: '3', target: { closest: () => null } });
  assert.equal(f.requests.length, 0);
});

test('rating response does not pull the user back after leaving study', async () => {
  const f = fixture();
  const pending = f.context.submitRating(3);
  f.context.switchView('home');
  f.send().resolve({ ok: true, status: 200, json: async () => ({ done: true, summary: {} }) });
  await pending;
  assert(f.element('view-study').classList.contains('hidden'));
  assert(!f.element('view-home').classList.contains('hidden'));
});
