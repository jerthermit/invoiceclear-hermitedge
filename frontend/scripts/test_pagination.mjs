import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createContext, runInContext} from 'node:vm';

const source = await readFile(new URL(process.argv[2] || '../web/app.js', import.meta.url), 'utf8');
const invoice = index => ({
  id:`invoice-${index}`,invoice_number:`INV-${String(index).padStart(4,'0')}`,customer:`Customer ${index}`,
  customer_email:`billing${index}@example.test`,status:'missing_po',version:1,amount_cents:10000,
  currency:'USD',payment_status:'unpaid',due_date:'2026-12-01',issue_date:'2026-11-01',source_ref:'ledger',
  documents:[],checks:[],replies:[],request:{to:'billing@example.test',subject:'Purchase order',body:'Please send the PO.'}
});
const records = count => Array.from({length:count},(_,index) => invoice(index));
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no) => {resolve=yes;reject=no;}); return {promise,resolve,reject}; };
const settle = async () => { for (let index=0;index<16;index++) await Promise.resolve(); };

function harness(initialRecords = records(137)) {
  const nodes = new Map(), lists = new Map(), timers = new Map(), requests = [], scrolls = [];
  let rows = initialRecords, timerId = 0, responder;
  const document = {activeElement:null,hidden:false,body:null};
  const createNode = (id = '', dataset = {}) => {
    let markup = '';
    const classes = new Set();
    return {
      id,dataset,attributes:new Map(),listeners:new Map(),value:'',textContent:'',hidden:false,disabled:false,
      scrollTop:0,open:false,parentElement:null,isConnected:true,innerHTMLWrites:0,
      get innerHTML() { return markup; },
      set innerHTML(value) {
        markup=value; this.innerHTMLWrites++;
        if (id === 'queue-list') {
          const elements = [...String(value).matchAll(/data-invoice="([^"]+)"/g)].map(match => {
            const element=createNode('',{invoice:match[1]}); element.parentElement=this; return element;
          });
          lists.set('[data-invoice]',elements);
        }
      },
      addEventListener(type,listener) { this.listeners.set(type,listener); },
      setAttribute(name,value) { this.attributes.set(name,String(value)); },
      getAttribute(name) { return this.attributes.get(name); },
      removeAttribute(name) { this.attributes.delete(name); },
      focus() { document.activeElement=this; },
      closest(selector) {
        return selector.split(',').map(value => value.trim()).some(value =>
          value === `#${this.id}` || value === '[data-invoice]' && this.dataset.invoice !== undefined
        ) ? this : this.parentElement?.closest(selector) || null;
      },
      querySelector(selector) { return document.querySelector(selector); },
      querySelectorAll(selector) { return document.querySelectorAll(selector); },
      scrollIntoView() {},
      classList:{add(...values) {values.forEach(value=>classes.add(value));},remove(...values) {values.forEach(value=>classes.delete(value));},contains(value) {return classes.has(value);},toggle(value,force) {if(force ?? !classes.has(value)) classes.add(value);else classes.delete(value);}},
      style:{setProperty() {}}
    };
  };
  document.querySelector = selector => {
    const row = selector.match(/^\[data-invoice="([^"]+)"\]$/);
    if (row) return (lists.get('[data-invoice]') || []).find(element=>element.dataset.invoice===row[1]) || null;
    if (!nodes.has(selector)) nodes.set(selector,createNode(selector.startsWith('#') ? selector.slice(1) : ''));
    return nodes.get(selector);
  };
  document.querySelectorAll = selector => lists.get(selector) || [];
  document.body=createNode('body');
  const get = selector => document.querySelector(selector);
  for (const id of ['queue-previous','queue-next','queue-size','queue-page-form','queue-page','queue-page-total']) get(`#${id}`).parentElement=get('#queue-pagination');
  get('#queue-list').parentElement=get('#queue-scroll');
  get('#notice').hidden=true;
  const views = ['open','prepared','all'].map(view=>createNode('',{view}));
  lists.set('[data-view]',views);
  const window = {scrollY:0,confirm:()=>true,scrollTo(options) {scrolls.push(options);this.scrollY=options.top;},matchMedia:()=>({matches:true})};
  const sessionStorage={getItem:()=>null,setItem() {},removeItem() {}};
  const context=createContext({document,window,sessionStorage,URLSearchParams,setInterval() {},
    setTimeout(callback) {const id=++timerId;timers.set(id,callback);return id;},clearTimeout(id) {timers.delete(id);}});
  runInContext(source.replace(/\nruntime\(\);\s*$/,'') + '\nglobalThis.pagination = {state,loadQueue,renderQueue,syncQueueControls,advanceInvoice,nextQueueId,openInvoice,updateInvoice,pollWorkspace,setApi(fn) {api=fn;}};',context);
  const app=context.pagination;
  Object.assign(app.state,{token:'pagination-test',runtime:{mode:'private'}});
  const page = path => {
    const query = new URLSearchParams(path.split('?')[1]);
    const offset=Number(query.get('offset')),limit=Number(query.get('limit')),view=query.get('view'),q=query.get('q').toLowerCase();
    const matching=rows.filter(row=>(view==='all' || (view==='prepared' ? row.status==='prepared' : row.status!=='prepared')) && `${row.invoice_number} ${row.customer} ${row.customer_email}`.toLowerCase().includes(q));
    return {invoices:matching.slice(offset,offset+limit),total:matching.length,offset,has_more:offset+limit<matching.length};
  };
  const defaultResponder=async path => {
    if (path.startsWith('/api/invoices?')) return page(path);
    if (path==='/api/capture') return {enabled:true,available:true};
    const id=decodeURIComponent(path.slice('/api/invoices/'.length));
    const found=rows.find(row=>row.id===id);
    assert.ok(found,`Unexpected API route: ${path}`); return found;
  };
  responder=defaultResponder;
  app.setApi(async (path,options) => { requests.push({path,options}); assert.ok(!options?.method || options.method==='GET','Pagination must be read-only');return responder(path,options); });
  const fire=async (selector,type='click',target=get(selector)) => {
    const element=get(selector),listener=element.listeners.get(type);
    assert.ok(listener,`Missing ${type} listener on ${selector}`);
    await listener({target,currentTarget:element,preventDefault() {}});await settle();
  };
  return {app,get,document,window,requests,scrolls,views,fire,page,
    setRows(value) {rows=value;},setResponder(value) {responder=value;},defaultResponder,
    async timers() {const pending=[...timers.values()];timers.clear();for (const callback of pending) callback();await settle();}
  };
}

let passed=0;
async function test(name,body) {await body();passed++;process.stdout.write(`PASS ${name}\n`);}
const ids = app => Array.from(app.state.rows,row=>row.id);
const query = request => new URLSearchParams(request.path.split('?')[1]);
const renderedRows = h => (h.get('#queue-list').innerHTML.match(/class="invoice-row"/g) || []).length;

await test('bounded pages replace rows and previous, next, jump and page sizes use server offsets',async()=>{
  const h=harness(),{app,get}=h;
  await app.loadQueue();
  assert.equal(app.state.queueOffset,0);assert.equal(renderedRows(h),10);
  assert.equal(get('#queue-previous').disabled,true);assert.equal(get('#queue-next').disabled,false);
  assert.match(get('#queue-count').textContent,/1\D+10 of 137/);
  await h.fire('#queue-next');assert.equal(app.state.queueOffset,10);assert.equal(renderedRows(h),10);
  assert.deepEqual(ids(app),records(20).slice(10).map(row=>row.id));
  await h.fire('#queue-previous');assert.equal(app.state.queueOffset,0);
  get('#queue-page').value='12';await h.fire('#queue-page-form','submit');assert.equal(app.state.queueOffset,110);
  assert.equal(query(h.requests.at(-1)).get('offset'),'110');
  get('#queue-page').value='3';await h.fire('#queue-page','change');assert.equal(app.state.queueOffset,20,'Mobile keyboard completion and native steppers must also navigate');
  for (const size of [25,50,10]) {
    get('#queue-size').value=String(size);await h.fire('#queue-size','change');
    assert.equal(app.state.queueOffset,0);assert.equal(app.state.queueLimit,size);assert.equal(renderedRows(h),size);
    assert.equal(query(h.requests.at(-1)).get('limit'),String(size));
  }
  const count=h.requests.length;
  for (const invalid of ['0','-1','1.5','999']) {get('#queue-page').value=invalid;await h.fire('#queue-page-form','submit');}
  assert.equal(h.requests.length,count,'Invalid jumps must not request an arbitrary offset');
});

await test('shrinking totals clamp an out-of-range page and retain an accurate final-page range',async()=>{
  const h=harness();await h.app.loadQueue({offset:130});
  assert.equal(renderedRows(h),7);assert.equal(h.get('#queue-next').disabled,true);
  h.setRows(records(31));h.requests.length=0;
  await h.app.loadQueue();
  assert.deepEqual(h.requests.map(request=>query(request).get('offset')),['130','30']);
  assert.equal(h.app.state.queueOffset,30);assert.equal(renderedRows(h),1);
  assert.equal(h.get('#queue-page').value,'4');assert.match(h.get('#queue-count').textContent,/31\D+31 of 31/);
  h.setRows([]);await h.app.loadQueue();
  assert.equal(h.app.state.queueOffset,0);assert.equal(renderedRows(h),0);assert.equal(h.get('#queue-count').textContent,'0 invoices');
  assert.equal(h.get('#queue-previous').disabled,true);assert.equal(h.get('#queue-next').disabled,true);
});

await test('search, clear search and filters reset the selected page and list scroll',async()=>{
  const h=harness();await h.app.loadQueue({offset:40});h.get('#queue-scroll').scrollTop=300;
  h.get('#queue-search').value=' Customer 7 ';await h.fire('#queue-search','input');await h.timers();
  assert.equal(h.app.state.q,'Customer 7');assert.equal(h.app.state.queueOffset,0);assert.equal(h.get('#queue-scroll').scrollTop,0);
  assert.equal(query(h.requests.at(-1)).get('q'),'Customer 7');assert.equal(h.app.state.total,11);
  const clear={closest:selector=>selector==='[data-clear-search]' ? clear : null};
  await h.fire('#queue-list','click',clear);assert.equal(h.app.state.q,'');assert.equal(h.app.state.total,137);
  await h.app.loadQueue({offset:30});h.get('#queue-scroll').scrollTop=200;
  const prepared=h.views.find(view=>view.dataset.view==='prepared');
  await prepared.listeners.get('click')();await settle();
  assert.equal(h.app.state.view,'prepared');assert.equal(h.app.state.queueOffset,0);assert.equal(h.get('#queue-scroll').scrollTop,0);
  assert.equal(h.app.state.total,0);assert.match(h.get('#queue-list').innerHTML,/Nothing prepared yet/);
});

await test('out-of-order page successes, errors and session responses cannot replace newer rows',async()=>{
  const h=harness();await h.app.loadQueue();
  const older=deferred(),newer=deferred();
  h.setResponder(path=>query({path}).get('offset')==='10' ? older.promise : newer.promise);
  const first=h.app.loadQueue({offset:10}),second=h.app.loadQueue({offset:20});
  newer.resolve(h.page('/api/invoices?view=open&q=&offset=20&limit=10'));await second;
  older.resolve(h.page('/api/invoices?view=open&q=&offset=10&limit=10'));await first;
  assert.equal(h.app.state.queueOffset,20);assert.equal(ids(h.app)[0],'invoice-20');
  const staleError=deferred();h.setResponder(()=>staleError.promise);
  const pendingError=h.app.loadQueue({offset:30});h.setResponder(h.defaultResponder);await h.app.loadQueue({offset:40});
  staleError.reject(new Error('Obsolete failure'));await pendingError;
  assert.equal(h.app.state.queueOffset,40);assert.equal(h.get('#notice').hidden,true);
  const session=deferred();h.setResponder(()=>session.promise);const pendingSession=h.app.loadQueue({offset:50});
  h.app.state.token='new-session';session.resolve(h.page('/api/invoices?view=open&q=&offset=50&limit=10'));await pendingSession;
  assert.equal(h.app.state.queueOffset,40);assert.equal(ids(h.app)[0],'invoice-40');
});

await test('failed page changes preserve the current page and restore usable controls',async()=>{
  const h=harness();await h.app.loadQueue({offset:10});const before=ids(h.app),markup=h.get('#queue-list').innerHTML;
  h.setResponder(async()=>{throw new Error('Connection unavailable <unsafe>');});
  assert.equal(await h.app.loadQueue({offset:20}),false);
  assert.deepEqual(ids(h.app),before);assert.equal(h.app.state.queueOffset,10);assert.equal(h.get('#queue-list').innerHTML,markup);
  assert.equal(h.app.state.queueLoading,false);assert.equal(h.get('#queue-previous').disabled,false);assert.equal(h.get('#queue-next').disabled,false);
  assert.equal(h.get('#queue-size').disabled,false);assert.equal(h.get('#queue-list').getAttribute('aria-busy'),'false');
  h.app.state.q='new search';await h.app.loadQueue({offset:0});
  assert.equal(renderedRows(h),0);assert.match(h.get('#queue-list').innerHTML,/Invoices could not load/);
  assert.doesNotMatch(h.get('#queue-list').innerHTML,/<unsafe>/);assert.match(h.get('#queue-list').innerHTML,/&lt;unsafe&gt;/);
});

await test('quiet refreshes retain the current page and scroll and defer while pagination or a row has focus',async()=>{
  const h=harness();await h.app.loadQueue({offset:30});h.get('#queue-scroll').scrollTop=219;
  h.requests.length=0;await h.app.loadQueue({quiet:true});
  assert.equal(query(h.requests[0]).get('offset'),'30');assert.equal(h.get('#queue-scroll').scrollTop,219);
  for (const selector of ['#queue-page','#queue-size','[data-invoice="invoice-34"]']) {
    const focused=h.get(selector);focused.value='8';focused.focus();h.requests.length=0;
    await h.app.pollWorkspace();
    assert.deepEqual(h.requests.map(request=>request.path),['/api/capture']);
    assert.equal(h.document.activeElement,focused);assert.equal(focused.value,'8');assert.equal(h.app.state.queueOffset,30);
  }
  h.document.activeElement=null;
  const response=deferred();h.setResponder(()=>response.promise);const pending=h.app.loadQueue({quiet:true});
  const page=h.get('#queue-page'),markup=h.get('#queue-list').innerHTML;page.value='9';page.focus();
  const updated=h.page('/api/invoices?view=open&q=&offset=30&limit=10');updated.invoices=updated.invoices.map(row=>({...row,version:9,customer:'Changed during refresh'}));
  response.resolve(updated);await pending;
  assert.equal(page.value,'9');assert.equal(h.document.activeElement,page);assert.equal(h.get('#queue-list').innerHTML,markup);
  assert.equal(h.app.state.rows[0].version,1,'A quiet response must defer if focus moved into pagination while loading');
});

await test('next invoice crosses a page boundary without accumulating rows or confirming a draft twice',async()=>{
  const h=harness();await h.app.loadQueue();
  Object.assign(h.app.state,{invoice:invoice(9),selectedId:'invoice-9',caseOpen:true,caseDirty:true,caseSequence:4});
  let confirmations=0;h.window.confirm=()=>{confirmations++;return true;};h.requests.length=0;
  await h.app.advanceInvoice(h.get('#case-next'));
  assert.equal(confirmations,1);assert.equal(h.app.state.invoice.id,'invoice-10');assert.equal(h.app.state.queueOffset,10);
  assert.equal(renderedRows(h),10);assert.equal(h.requests.length,2);
  assert.equal(query(h.requests[0]).get('offset'),'10');assert.equal(h.requests[1].path,'/api/invoices/invoice-10');
});

await test('declining next-case navigation retains the page and draft without requesting another page',async()=>{
  const h=harness();await h.app.loadQueue();
  Object.assign(h.app.state,{invoice:invoice(9),caseOpen:true,caseDirty:true});h.get('#case-content').innerHTML='Unsaved PO draft';
  h.window.confirm=()=>false;h.requests.length=0;
  await h.app.advanceInvoice(h.get('#case-next'));
  assert.equal(h.requests.length,0);assert.equal(h.app.state.queueOffset,0);assert.equal(h.app.state.invoice.id,'invoice-9');
  assert.equal(h.app.state.caseDirty,true);assert.equal(h.get('#case-content').innerHTML,'Unsaved PO draft');
});

await test('case identity, sequence and edits independently invalidate an in-flight boundary load',async()=>{
  for (const change of [state=>{state.invoice=invoice(4);},state=>{state.caseSequence++;},state=>{state.caseEdits++;state.caseDirty=true;}]) {
    const h=harness();await h.app.loadQueue();Object.assign(h.app.state,{invoice:invoice(9),caseOpen:true,caseSequence:3});
    const response=deferred();h.setResponder(()=>response.promise);h.requests.length=0;
    const pending=h.app.advanceInvoice(h.get('#case-next'));change(h.app.state);const active=h.app.state.invoice;
    response.resolve(h.page('/api/invoices?view=open&q=&offset=10&limit=10'));await pending;
    assert.equal(h.app.state.invoice,active);assert.equal(h.app.state.queueOffset,0);assert.equal(ids(h.app)[0],'invoice-0');
    assert.equal(h.requests.length,1);assert.equal(h.app.state.navigationBusy,false);assert.equal(h.app.state.queueLoading,false);
  }
});

await test('approval at a page boundary refills before advancing so the immediate successor is not skipped',async()=>{
  const data=records(21),h=harness(data);await h.app.loadQueue();
  Object.assign(h.app.state,{invoice:data[9],selectedId:data[9].id,caseOpen:true,caseSequence:7});
  const prepared={...data[9],status:'prepared',version:2,resolution:{po_reference:'PO-9'}};
  data[9]=prepared;h.app.updateInvoice(prepared);h.requests.length=0;
  assert.equal(h.app.state.rows.length,9);assert.equal(h.app.nextQueueId(),null);
  await h.app.advanceInvoice(h.get('#case-next'));
  assert.equal(h.app.state.invoice.id,'invoice-10');assert.equal(h.app.state.queueOffset,0);assert.equal(renderedRows(h),10);
  assert.equal(query(h.requests[0]).get('offset'),'0');assert.equal(h.requests[1].path,'/api/invoices/invoice-10');
});

await test('back to invoices restores page, query, list scroll and selected-row focus',async()=>{
  const h=harness();h.app.state.q='Customer';await h.app.loadQueue({offset:10});
  h.window.scrollY=37;h.get('#queue-scroll').scrollTop=260;
  await h.app.openInvoice('invoice-14');h.get('#queue-scroll').scrollTop=0;
  await h.fire('#queue-back');
  assert.equal(h.app.state.caseOpen,false);assert.equal(h.app.state.invoice,null);assert.equal(h.app.state.queueOffset,10);
  assert.equal(h.app.state.q,'Customer');assert.equal(h.get('#queue-scroll').scrollTop,260);assert.equal(h.window.scrollY,37);
  assert.equal(h.document.activeElement.dataset.invoice,'invoice-14');
});

await test('back after approval clamps the emptied last page and restores saved scroll with a visible focus target',async()=>{
  const data=records(21),h=harness(data);await h.app.loadQueue({offset:20});
  h.get('#queue-scroll').scrollTop=170;await h.app.openInvoice('invoice-20');
  const prepared={...data[20],status:'prepared',version:2,resolution:{po_reference:'PO-20'}};
  data[20]=prepared;h.app.updateInvoice(prepared);h.get('#queue-scroll').scrollTop=0;
  await h.fire('#queue-back');
  assert.equal(h.app.state.queueOffset,10);assert.equal(renderedRows(h),10);assert.equal(h.app.state.caseOpen,false);
  assert.equal(h.get('#queue-scroll').scrollTop,170);assert.equal(h.document.activeElement,h.get('#queue-scroll'));
});

await test('declining back keeps the unfinished case, list page and scroll intact',async()=>{
  const h=harness();await h.app.loadQueue({offset:10});await h.app.openInvoice('invoice-14');
  h.app.state.caseDirty=true;h.get('#case-content').innerHTML='Retained correction';h.window.confirm=()=>false;h.requests.length=0;
  await h.fire('#queue-back');
  assert.equal(h.app.state.caseOpen,true);assert.equal(h.app.state.invoice.id,'invoice-14');assert.equal(h.app.state.queueOffset,10);
  assert.equal(h.app.state.caseDirty,true);assert.equal(h.get('#case-content').innerHTML,'Retained correction');assert.equal(h.requests.length,0);
});

process.stdout.write(`${passed} pagination regression groups passed.\n`);
