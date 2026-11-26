/* Offline execution of the actual Velo templates and Render bootstrap. No network. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const source = file => fs.readFileSync(file, 'utf8').replace(/^import .*;\n/gm, '').replace(/export const /g, 'const ');

function velo({member=id(20), orders=[], configured=true, fetchResult=null}={}) {
  const calls = [], permissions = [], paging = [];
  const context = {Permissions: {SiteMember: 'member'}, webMethod(permission, fn) { permissions.push(permission); return fn; },
    currentMember: {async getMember() { return member ? {_id: member} : undefined; }},
    orders: {async listCurrentMemberOrders(filters, sort, page) { paging.push(page); return orders.slice(page.skip, page.skip + page.limit); }},
    secrets: {async getSecretValue(name) { assert.equal(name, 'CYT_WIX_BRIDGE_KEY'); return {value: 'a1'.repeat(32)}; }},
    elevate: fn => fn, setTimeout, clearTimeout,
    async fetch(url, options) { calls.push({url, options}); if (fetchResult instanceof Error) throw fetchResult;
      return fetchResult || {ok: true, async json() { return {purchaseId: JSON.parse(options.body).purchaseId, authorization: 'Bearer synthetic', expiresAt: 100}; }}; }
  };
  let code = source('wix/quoteAnalyzer.web.js');
  if (configured) code = code.replace("TIER1_PLAN_ID = ''", `TIER1_PLAN_ID = '${id(2)}'`).replace("TIER2_PLAN_ID = ''", `TIER2_PLAN_ID = '${id(3)}'`);
  vm.runInNewContext(code + '\nthis.api = {openReview, listReviewPurchases};', context);
  return {...context.api, calls, permissions, paging};
}
const order = (n, changes={}) => ({_id: id(n), buyer: {memberId: id(20)}, planId: id(2),
  createdDate: '2026-11-25T00:00:00Z', status: 'ACTIVE', ...changes});

test('Velo methods require SiteMember and missing plan IDs fail closed', async () => {
  const v = velo({configured: false});
  assert.deepEqual(v.permissions, ['member', 'member']);
  await assert.rejects(v.openReview(id(10)), /not configured/);
  await assert.rejects(v.listReviewPurchases(), /not configured/);
  assert.equal(v.calls.length, 0);
});

test('Velo derives identity from currentMember, forwards no browser tier or URLs', async () => {
  const v = velo();
  const proof = await v.openReview(id(10), {memberId: id(21), package: 'tier3'});
  assert.equal(proof.authorization, 'Bearer synthetic');
  const request = v.calls[0];
  assert.equal(request.url, 'https://check-your-tech-closed.onrender.com/wix/handoff');
  assert.deepEqual(JSON.parse(request.options.body), {purchaseId: id(10), memberId: id(20)});
  assert(!JSON.stringify(proof).includes('a1'.repeat(32)));
});

test('anonymous member and malformed selector cause no handoff request', async () => {
  const v = velo({member: null}); await assert.rejects(v.openReview(id(10))); assert.equal(v.calls.length, 0);
  const other = velo(); await assert.rejects(other.openReview('https://untrusted.invalid')); assert.equal(other.calls.length, 0);
});

test('selector enumeration uses plan IDs, ownership and bounded pagination', async () => {
  const page = Array.from({length: 100}, (_, i) => order(100 + i, {planId: id(99)}));
  page.push(order(10), order(11, {planId: id(3)}), order(12, {buyer: {memberId: id(21)}}), order(13, {planId: id(99), planName: 'In-Depth Quote Review'}));
  const v = velo({orders: page}); const rows = await v.listReviewPurchases();
  assert.equal(rows.length, 2); assert.equal(rows[1].tier, 'tier2');
  assert.equal(v.paging.length, 2);
  assert(rows.every(row => !('buyer' in row) && !('email' in row)));
});

test('pending newest purchase stays selectable rather than silently authorizing an old paid order', async () => {
  const v = velo({orders: [order(11, {status: 'PENDING'}), order(10)]});
  const result = await v.listReviewPurchases();
  assert.equal(result[0].purchaseId, id(11)); assert.equal(result[0].status, 'PENDING');
});

test('Velo sanitizes provider exceptions and rejects mismatched handoff', async () => {
  await assert.rejects(velo({fetchResult: new Error('sensitive transport request')}).openReview(id(10)), error => !error.message.includes('sensitive'));
  const v = velo({fetchResult: {ok: true, async json() { return {purchaseId: id(99), authorization: 'Bearer invalid', expiresAt: 100}; }}});
  await assert.rejects(v.openReview(id(10)), /could not be verified/);
});

function bridge(handler=async () => ({ok: true, async text() { return '<p>Upload UI</p>'; }})) {
  const calls=[], posted=[], configured=[], elements={};
  const element = () => ({dataset: {}, listeners:{}, options:[], value:'', textContent:'', srcdoc:'',
    addEventListener(name, fn) { this.listeners[name]=fn; }, append(item) { this.options.push(item); if (!this.value) this.value=item.value; }});
  for (const name of ['review-frame', 'wix-status', 'review-order', 'review-reconnect']) elements[name]=element();
  elements['review-frame'].dataset.parentOrigin='https://www.checkyourtech.info';
  elements['review-frame'].contentWindow={configureQuoteUpload: credentials => configured.push(credentials)};
  const parent={postMessage(data, origin) { posted.push({data, origin}); }};
  let listener, sequence=0;
  const context={window:{parent, confirm:()=>true, addEventListener(_, fn) { listener=fn; }},
    document:{getElementById:name=>elements[name], createElement:element},
    crypto:{randomUUID:()=>id(++sequence)}, AbortController,
    setInterval:()=>1, clearInterval(){}, setTimeout:()=>2, clearTimeout(){},
    async fetch(url, options) { calls.push({url, options}); return handler(url, options); }};
  vm.runInNewContext(fs.readFileSync('static/wix_connect.js','utf8'),context);
  const nonce=posted[0].data.nonce;
  const send=(data, overrides={})=>listener({source:parent,origin:'https://www.checkyourtech.info',data:{nonce,...data},...overrides});
  const list=()=>send({type:'cyt-orders',purchases:[{purchaseId:id(10),tier:'tier1',createdDate:'today',status:'ACTIVE'},
    {purchaseId:id(11),tier:'tier2',createdDate:'today',status:'ACTIVE'}]});
  const credentials=(changes={})=>send({type:'cyt-credentials',purchaseId:id(10),authorization:'Bearer synthetic',
    requestId:posted.at(-1).data.requestId,...changes});
  return {calls,posted,configured,elements,send,list,credentials,context};
}

test('bridge accepts only exact parent origin, source and nonce', async () => {
  const b=bridge();
  for (const overrides of [{origin:'https://evil.invalid'},{source:{} }]) {
    await b.send({type:'cyt-orders',purchases:[]}, overrides);
  }
  await b.send({type:'cyt-orders',nonce:'wrong',purchases:[]});
  assert.equal(b.elements['review-order'].options.length,0); assert.equal(b.calls.length,0);
  assert.equal(b.posted[0].origin,'https://www.checkyourtech.info');
});

test('bridge fetches protected upload HTML using headers, initializes unchanged upload UI', async () => {
  const b=bridge(); await b.list(); await b.credentials();
  b.elements['review-frame'].onload();
  assert.equal(b.calls[0].url,'/upload-page');
  assert.equal(b.calls[0].options.headers.Authorization,'Bearer synthetic');
  assert.equal(b.calls[0].options.cache,'no-store');
  assert.equal(b.calls[0].options.redirect,'error');
  assert.equal(b.configured[0].purchaseId,id(10));
  assert.equal(b.calls.length,1);
});

test('wrong order, stale request and duplicate credentials are ignored', async () => {
  const b=bridge(); await b.list();
  await b.credentials({purchaseId:id(99)}); await b.credentials({requestId:'stale'});
  assert.equal(b.calls.length,0);
  await b.credentials(); await b.credentials(); assert.equal(b.calls.length,1);
});

test('unmatched receipt selector does not fall back to newest purchase', async () => {
  const b=bridge();
  await b.send({type:'cyt-orders',selected:id(99),purchases:[{purchaseId:id(10),tier:'tier1'}]});
  assert.equal(b.posted.filter(p=>p.data.type==='cyt-select').length,0);
});

test('late upload-page response cannot leak earlier account/order after selection change', async () => {
  let release;
  const b=bridge(()=>new Promise(resolve=>{release=resolve;})); await b.list();
  const pending=b.credentials(); await flush();
  b.elements['review-order'].value=id(11); b.elements['review-order'].listeners.change();
  release({ok:true,async text(){return '<p>Old private UI</p>';}}); await pending;
  assert.equal(b.elements['review-frame'].srcdoc,''); assert.equal(b.configured.length,0);
});

test('API failures never display raw provider contents or incomplete HTML', async () => {
  const b=bridge(async()=>({ok:false,async text(){throw new Error('must not read');}}));
  await b.list(); await b.credentials();
  assert.equal(b.elements['review-frame'].srcdoc,'');
  assert.match(b.elements['wix-status'].textContent,/unavailable or expired/);
});

test('sign-out clears report frame and invalidates outstanding handoff', async () => {
  const b=bridge(); await b.list(); await b.credentials();
  await b.send({type:'cyt-clear'});
  assert.equal(b.elements['review-frame'].srcdoc,'');
  assert.equal(b.elements['review-reconnect'].disabled,true);
  await b.credentials(); assert.equal(b.calls.length,1);
});

test('Wix page uses existing component, clears on logout and ignores late sessions', async () => {
  let ready,onMessage,onLogout,release;
  const posted=[]; const frame={onMessage(fn){onMessage=fn;},postMessage(data){posted.push(data);},src:''};
  const controls={'#htmlReport':frame,'#uploadButton1':{collapse(){this.collapsed=true;}}};
  const $w=name=>controls[name]; $w.onReady=fn=>{ready=fn;};
  const context={$w,wixLocationFrontend:{query:{}},authentication:{onLogout(fn){onLogout=fn;}},
    async listReviewPurchases(){return [{purchaseId:id(10),tier:'tier1'}];},
    openReview(){return new Promise(resolve=>{release=resolve;});}};
  vm.runInNewContext(source('wix/UploadQuote.page.js'),context); ready();
  await onMessage({data:{type:'cyt-ready',nonce:'n'}});
  const pending=onMessage({data:{type:'cyt-select',nonce:'n',requestId:'r',purchaseId:id(10)}});
  await flush(); onLogout(); release({purchaseId:id(10),authorization:'Bearer private'}); await pending;
  assert.equal(posted.filter(p=>p.type==='cyt-credentials').length,0);
  assert.equal(frame.src,'https://check-your-tech-closed.onrender.com/wix/connect');
  assert(controls['#uploadButton1'].collapsed);
});

test('Thank You carries only order selector, with no plan-name authorization', async () => {
  let ready;const destinations=[];
  const context={$w:{onReady(fn){ready=fn;}},wixLocationFrontend:{query:{orderId:id(10)},to(value){destinations.push(value);}},
    async listReviewPurchases(){return [{purchaseId:id(10),tier:'tier1'}];}};
  vm.runInNewContext(source('wix/ThankYou.page.js').replace("UPLOAD_PAGE_PATH = ''","UPLOAD_PAGE_PATH = '/synthetic-upload'"),context);
  await ready();assert.equal(destinations[0],'/synthetic-upload?orderId='+id(10));
  assert(!destinations[0].includes('package')); assert(!destinations[0].includes('Bearer'));
  context.wixLocationFrontend.query.orderId=id(99); await ready();
  assert.equal(destinations[1],'/synthetic-upload?orderId='+id(99));
});
