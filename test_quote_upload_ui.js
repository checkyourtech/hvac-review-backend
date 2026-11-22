/* Offline DOM-contract tests: no browser, network, credentials or documents. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function interfaceFor(handler) {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.dataset = {}; }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    setAttribute() {}
    after(node) { this.parent.append(node); }
    remove() { this.parent.children = this.parent.children.filter(node => node !== this); }
  }
  const root = new Element('root');
  const elements = {};
  for (const id of ['quote-upload', 'quote-groups', 'upload-status', 'submit-quotes', 'add-quote']) {
    const node = new Element('div'); node.id = id; elements[id] = node; root.append(node);
  }
  elements['quote-upload'].dataset.quoteLimit = '1';
  elements['quote-upload'].elements = {customer_name: {value: 'Synthetic'}, customer_email: {value: 'synthetic@example.invalid'}, package: {value: 'tier1'}};
  const find = (node, check) => check(node) ? node : node.children.map(child => find(child, check)).find(Boolean);
  const timers = new Map(); let next = 0;
  const writes = [], requests = [];
  const window = {addEventListener(name, fn) { this[name] = fn; }};
  const context = {
    window, document: {
      getElementById(id) { return find(root, node => node.id === id); },
      createElement(tag) { return new Element(tag); }, createTextNode(text) { const node = new Element('text'); node.textContent = text; return node; },
      open() {}, write(text) { writes.push(text); }, close() {},
    },
    URL: {createObjectURL: () => 'local:preview', revokeObjectURL() {}},
    crypto: {randomUUID: () => 'synthetic-idempotency'}, AbortController,
    FormData: class { set() {} append() {} },
    setTimeout(fn, ms) { const id = ++next; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({url, options});
      const result = await handler(url, options);
      const status = result?._status || 200, body = result?._status ? result.body : result;
      return {ok: status < 400, status, async text() { return typeof body === 'string' ? body : JSON.stringify(body); }};
    },
  };
  vm.runInNewContext(fs.readFileSync('static/quote_upload.js', 'utf8'), context);
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  return {root, elements, window, requests, writes, timers, find, flush,
    configure(purchaseId='synthetic-order') { window.configureQuoteUpload({authorization: 'Bearer synthetic', purchaseId}); },
    async tick() { const timer = [...timers].find(([, value]) => value.ms < 15000); if (timer) { timers.delete(timer[0]); timer[1].fn(); } await flush(); },
  };
}
const queued = {job_id: 'job-one', state: 'queued', approximate_queue_position: 18};
const completed = {job_id: 'job-one', state: 'completed'};

test('reconnection lists saved jobs and automatically follows queued status', async () => {
  const ui = interfaceFor(url => url === '/jobs' ? {jobs: [queued]} : url.endsWith('/report') ? '<p>Saved report</p>' : completed);
  ui.configure(); await ui.flush();
  assert.match(ui.elements['upload-status'].textContent, /job-one.*queued/);
  assert.match(ui.elements['upload-status'].textContent, /Approximate queue position: 18/);
  assert.match(ui.elements['upload-status'].textContent, /not guaranteed/);
  await ui.tick();
  assert.deepEqual(ui.writes, ['<p>Saved report</p>']);
  assert(ui.requests.every(request => request.options.cache === 'no-store'));
});

test('late responses cannot expose an earlier signed-in customer', async () => {
  let release;
  const first = new Promise(resolve => { release = resolve; });
  const ui = interfaceFor((url, options) => options.headers['X-Purchase-ID'] === 'first' ? first : {jobs: []});
  ui.configure('first'); await ui.flush();
  ui.configure('second'); await ui.flush();
  release({jobs: [{job_id: 'private-first-job', state: 'completed'}]}); await ui.flush();
  assert.equal(ui.find(ui.root, node => node.textContent.includes('private-first-job')), undefined);
  assert.equal(ui.writes.length, 0);
});

test('failed uncertain work offers no analysis retry', async () => {
  const ui = interfaceFor(() => ({jobs: [{job_id: 'uncertain', state: 'failed', retry_allowed: false,
    error: {message: 'Contact support before retrying.'}}]}));
  ui.configure(); await ui.flush();
  assert.equal(ui.find(ui.root, node => node.textContent.includes('Retry analysis')), undefined);
  assert(ui.find(ui.root, node => node.textContent === 'Contact support before retrying.'));
  assert.equal(ui.requests.length, 1);
});

test('a failed poll retains the stable review ID and recovery list', async () => {
  let failed = false;
  const ui = interfaceFor(url => {
    if (url === '/jobs') return {jobs: [failed ? {...queued, state: 'failed'} : queued]};
    failed = true;
    return {...queued, state: 'failed', error: {message: 'Contact support.'}};
  });
  ui.configure(); await ui.flush(); await ui.tick();
  assert.match(ui.elements['upload-status'].textContent, /Review job-one: Contact support/);
  assert(ui.find(ui.root, node => node.textContent === 'Refresh saved reviews'));
});

test('completed report uses authenticated retrieval and retains report HTML', async () => {
  const ui = interfaceFor(url => url === '/jobs' ? {jobs: [completed]} : '<p>Report with browser-print PDF</p>');
  ui.configure(); await ui.flush();
  await ui.find(ui.root, node => node.textContent === 'Open saved report').onclick();
  assert.deepEqual(ui.writes, ['<p>Report with browser-print PDF</p>']);
  assert.equal(ui.requests[1].options.headers.Authorization, 'Bearer synthetic');
});

test('notification retry is offered only when permitted by backend', async () => {
  let retried = false;
  const ui = interfaceFor(url => {
    if (url.endsWith('delivery-retry')) { retried = true; return completed; }
    return {jobs: [{...completed, notification: {retry_allowed: !retried}}]};
  });
  ui.configure(); await ui.flush();
  await ui.find(ui.root, node => node.textContent === 'Retry service notification').onclick();
  assert.equal(ui.requests.filter(request => request.options.method === 'POST').length, 1);
  assert.equal(ui.find(ui.root, node => node.textContent === 'Retry service notification'), undefined);
});

test('page exit stops polling and drops in-memory authentication', async () => {
  const ui = interfaceFor(() => ({jobs: [queued]}));
  ui.configure(); await ui.flush();
  ui.window.pagehide(); await ui.tick();
  assert.equal(ui.requests.length, 1);
});

test('polling has a fixed upper bound and leaves a manual reconnect path', async () => {
  const ui = interfaceFor(url => url === '/jobs' ? {jobs: [queued]} : queued);
  ui.configure(); await ui.flush();
  for (let i = 0; i < 61; i++) {
    const entry = [...ui.timers][0];
    if (entry) { ui.timers.delete(entry[0]); entry[1].fn(); }
    await ui.flush();
  }
  assert.equal(ui.requests.filter(request => request.url === '/jobs/job-one').length, 60);
  assert.match(ui.elements['upload-status'].textContent, /Automatic refresh paused/);
  assert(ui.find(ui.root, node => node.textContent === 'Refresh saved reviews'));
  assert.equal(ui.timers.size, 0);
});

test('status text is not interpreted as HTML', async () => {
  const ui = interfaceFor(() => ({jobs: [{job_id: '<script>bad</script>', state: 'failed', error: {message: '<b>text</b>'}}]}));
  ui.configure(); await ui.flush();
  assert.equal(ui.writes.length, 0);
  assert(ui.find(ui.root, node => node.textContent === '<b>text</b>'));
});

test('submission displays durable confirmation and follows stable job ID', async () => {
  const ui = interfaceFor(url => url === '/jobs' ? {jobs: []} : url === '/upload' ? {_status: 202, body: queued} :
    url.endsWith('/report') ? '<p>Completed</p>' : completed);
  ui.configure(); await ui.flush();
  const input = ui.find(ui.elements['quote-groups'], node => node.tag === 'input' && node.multiple);
  input.files = [{name: 'synthetic.txt', size: 100}]; input.listeners.change();
  const submitted = ui.elements['quote-upload'].listeners.submit({preventDefault() {}});
  await ui.flush();
  assert.match(ui.elements['upload-status'].textContent, /Submission saved.*job-one/);
  assert.equal(ui.requests.find(request => request.url === '/upload').options.headers['Idempotency-Key'], 'synthetic-idempotency');
  await ui.tick(); await submitted;
  assert.deepEqual(ui.writes, ['<p>Completed</p>']);
});

test('lost acknowledgment keeps the submission key for unchanged-page replay', async () => {
  const ui = interfaceFor(url => { if (url === '/upload') throw new Error('Synthetic disconnect'); return {jobs: []}; });
  ui.configure(); await ui.flush();
  const input = ui.find(ui.elements['quote-groups'], node => node.tag === 'input' && node.multiple);
  input.files = [{name: 'synthetic.txt', size: 100}]; input.listeners.change();
  await ui.elements['quote-upload'].listeners.submit({preventDefault() {}});
  await ui.elements['quote-upload'].listeners.submit({preventDefault() {}});
  const posts = ui.requests.filter(request => request.url === '/upload');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
  assert.match(ui.elements['upload-status'].textContent, /check saved reviews/);
});
