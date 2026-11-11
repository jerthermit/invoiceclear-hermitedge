import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createContext, runInContext } from 'node:vm';

const source = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const nodes = new Map();
const nodeLists = new Map();
const node = () => ({
  innerHTML: '', textContent: '', value: '', hidden: false, disabled: false, dataset: {}, attributes: new Map(), listeners: new Map(),
  addEventListener(type, listener) { this.listeners.set(type, listener); },
  setAttribute(name, value) { this.attributes.set(name, value); },
  removeAttribute(name) { this.attributes.delete(name); },
  getAttribute(name) { return this.attributes.get(name); },
  querySelector(selector) { return document.querySelector(selector); },
  querySelectorAll(selector) { return nodeLists.get(selector) || []; },
  focus() { document.activeElement = this; },
  scrollIntoView() {}, classList: {toggle() {}},
  closest(selector) { return selector === '[data-case-tab]' && this.dataset.caseTab ? this : null; }
});
const document = {
  querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, node());
    return nodes.get(selector);
  },
  querySelectorAll(selector) { return nodeLists.get(selector) || []; }
};
let interactionHooks;
const ui = {init:hooks => {interactionHooks=hooks;}, reveal() {}, spotlight() {}, showNotice() {}, dismissNotice() {}, fileError:files => files?.length === 1 && /\.(pdf|txt)$/i.test(files[0].name) && files[0].size > 0 && files[0].size <= 5*1024*1024 ? '' : 'Choose one readable PDF or TXT.'};
const context = createContext({ document, window: {InvoiceClearUI:ui}, URLSearchParams, setInterval() {} });
runInContext(source.replace(/\nruntime\(\);\s*$/, '') +
  '\nglobalThis.workflow = { state, nextAction, journeySteps, renderCase, renderQueue, updateInvoice, accountingHtml, mailHtml, canApprove, canSendRequest, activateCaseTab, sourceMarkup, showDocumentPage, nextQueueId, advanceInvoice, operation, confirmDraftDiscard, refreshInvoice, refreshAccounting, verifyReceipt, syncReplies, applyCaseRefresh, loadConnections, renderConnections, captureHtml, setCapture, pollWorkspace, historyHtml, loadHistory, downloadSetup, investigationHtml, investigateCase, applyInvestigation, showInvestigationSource, investigationStale };', context);
const { workflow } = context;
const connect = () => { workflow.state.connections = { qbo: { connected: true, po_field_configured: true }, gmail: { connected: true } }; };
const fixture = () => ({
  id: 'invoice-1', status: 'prepared', invoice_number: 'INV-1', customer: 'Example', amount_cents: 10000, currency: 'USD',
  payment_status: 'unpaid', po_reference: 'PO OLD', source_ref: 'Ledger', resolution: { po_reference: 'PO NEW' },
  documents: [], checks: [], replies: [], request: { to: 'ap@example.example', subject: 'Purchase order', body: 'Please send the PO.' },
  integration: { accounting: { po_reference: 'PO OLD', payment_status: 'unpaid', applied_at: '2026-10-01', action: { state: 'confirmed', reference: 'PO OLD' } }, messages: [] }
});
const poDocument = (method = 'together', reviewed = false) => {
  const fields = { po_number: 'PO NEW', customer: 'Example', currency: 'USD', amount: '100.00' };
  const snippets = { po_number: 'PO number: PO NEW', customer: 'Customer: Example', currency: 'Currency: USD', amount: 'PO amount: 100.00' };
  const evidence = Object.fromEntries(Object.entries(snippets).map(([name, snippet]) => [name, { page: 1, snippet }]));
  return { id: 'doc-1', filename: 'Purchase order.txt', extraction_method: method, extraction_error: null,
    suggested_fields: fields, fields: reviewed ? fields : {}, evidence, review_evidence: reviewed ? evidence : {}, reviewed,
    pages: [{ number: 1, text: Object.values(snippets).join('\n') }] };
};
const reviewedInvoice = () => {
  const invoice = fixture(); invoice.status = 'ready'; invoice.resolution = null; invoice.proposed_po_reference = 'PO NEW';
  invoice.documents = [poDocument('rules', true)];
  invoice.checks = ['customer', 'currency', 'po_reference', 'po_coverage'].map(code => ({ code, label: code, status: 'pass',
    document_id: 'doc-1', source_page: 1, invoice_value: 'Matched', document_value: 'Matched' }));
  return invoice;
};
const render = invoice => { workflow.state.invoice = invoice; workflow.state.docId = null; workflow.state.page = 0; workflow.renderCase(); return nodes.get('#case-content').innerHTML; };
const count = (html, pattern) => (html.match(pattern) || []).length;
const decodeSource = text => text.replace(/&(?:amp|lt|gt|quot|#39);/g, entity => ({ '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" })[entity]);
const retainedSource = html => decodeSource(html.replace(/<span class="source-line">|<\/span>|<mark class="source-highlight">|<\/mark>/g, ''));
const highlightedSource = html => [...html.matchAll(/<mark class="source-highlight">([\s\S]*?)<\/mark>/g)].map(match => decodeSource(match[1]));
let passed = 0;
const test = (name, task) => { connect(); task(); passed++; process.stdout.write(`PASS ${name}\n`); };

test('each new active approval permits a different reference despite a prior receipt', () => {
  const invoice = fixture();
  for (const approved of ['PO NEW', 'PO NEXT']) {
    invoice.resolution.po_reference = approved;
    const html = render(invoice);
    assert.match(html, /data-apply-reference/);
    assert.doesNotMatch(html, /PO correction verified/);
    invoice.integration.accounting.po_reference = approved;
  }
});
test('case and whitespace normalization avoids duplicate updates and scopes receipts', () => {
  const invoice = fixture(); invoice.integration.accounting.po_reference = '  po\t new  ';
  assert.doesNotMatch(render(invoice), /data-apply-reference/);
  assert.doesNotMatch(render(invoice), /PO correction verified/);
  invoice.integration.accounting.action.reference = 'po  new';
  assert.match(render(invoice), /PO correction verified/);
  invoice.integration.accounting.applied_at = null;
  assert.doesNotMatch(render(invoice), /PO correction verified/);
});
test('active approval, source, unpaid state and connected field guards remain', () => {
  for (const mutate of [i => { i.status = 'ready'; }, i => { i.resolution = null; }, i => { i.resolution.po_reference = ' '; },
    i => { i.integration.accounting.source_changed = true; }, i => { i.integration.accounting.error = 'Unavailable'; },
    i => { i.integration.accounting.payment_status = 'paid'; }, i => { i.integration.accounting.payment_status = 'partial'; },
    () => { workflow.state.connections.qbo.connected = false; }, () => { workflow.state.connections.qbo.po_field_configured = false; }]) {
    connect(); const invoice = fixture(); mutate(invoice); assert.doesNotMatch(render(invoice), /data-apply-reference/);
  }
});
test('pending and unknown updates hold application with one verification action', () => {
  for (const state of ['pending', 'unknown']) {
    const invoice = fixture(); invoice.integration.accounting.action.state = state;
    const html = render(invoice); assert.doesNotMatch(html, /data-apply-reference/);
    assert.equal(count(html, /data-next="verify-update"|data-reconcile-reference/g), 1);
    assert.match(workflow.accountingHtml(invoice, 'handoff'), /data-reconcile-reference/);
  }
});
test('pending and unknown sends hold application with one verification action', () => {
  for (const status of ['pending', 'unknown']) {
    const invoice = fixture(); invoice.integration.messages = [{ direction: 'outgoing', status }];
    const html = render(invoice); assert.doesNotMatch(html, /data-apply-reference/);
    assert.equal(count(html, /data-next="verify-send"|data-reconcile-request/g), 1);
    assert.match(workflow.mailHtml(invoice, 'handoff'), /data-reconcile-request/);
    assert.equal(workflow.canApprove(invoice), false); assert.equal(workflow.canSendRequest(invoice), false);
  }
});
test('pending provider actions block approval despite reviewed evidence and passing checks', () => {
  for (const status of ['pending', 'unknown']) {
    for (const provider of ['qbo', 'gmail']) {
      const invoice = reviewedInvoice();
      assert.equal(workflow.canApprove(invoice), true);
      if (provider === 'qbo') invoice.integration.accounting.action.state = status;
      else invoice.integration.messages = [{ direction: 'outgoing', status }];
      const snapshot = JSON.stringify(invoice), html = render(invoice);
      assert.equal(workflow.canApprove(invoice), false);
      assert.doesNotMatch(html, /data-next="approve"/); assert.match(html, /Handoff blocked/);
      assert.doesNotMatch(html, /class="review-result">Ready for handoff/);
      assert.equal(count(html, new RegExp('data-next="verify-' + (provider === 'qbo' ? 'update' : 'send') + '"', 'g')), 1);
      assert.equal(JSON.stringify(invoice), snapshot);
    }
  }
});
test('a second provider verification stays available while the primary verifies Gmail', () => {
  const invoice = fixture(); invoice.integration.accounting.action.state = 'unknown';
  invoice.integration.messages = [{ direction: 'outgoing', status: 'unknown' }];
  const html = render(invoice); assert.match(html, /data-next="verify-send"/);
  assert.match(html, /data-reconcile-reference/); assert.doesNotMatch(html, /data-reconcile-request/);
});
test('one send action and retained secondary send when document review is primary', () => {
  const invoice = fixture(); invoice.status = 'missing_po'; invoice.resolution = null;
  assert.equal(count(render(invoice), /data-next="send"|data-send-request/g), 1);
  invoice.status = 'needs_review'; invoice.documents = [{ id: 'doc-1', filename: 'PO.txt', reviewed: false, pages: [] }];
  const html = render(invoice); assert.match(html, /id="review-form"/); assert.match(html, /data-send-request/);
  for (const control of ['data-copy-request', 'data-download-request', 'data-download-doc', 'data-sync-replies', 'id="review-form"']) assert.ok(html.includes(control));
});
test('document, mail and comparison sources stay escaped', () => {
  const malicious = '\"><script>alert(1)</script>&'; const invoice = fixture();
  invoice.documents = [{ id: malicious, filename: malicious, reviewed: true, fields: { po_number: malicious }, pages: [{ number: 1, text: malicious }] }];
  invoice.checks = [{ code: 'po_reference', status: 'fail', label: malicious, document_id: malicious, source_page: 1, document_value: malicious }];
  invoice.request.body = malicious; invoice.source_ref = malicious;
  invoice.integration.messages = [{ direction: 'incoming', text: malicious, attachments: [{ document_id: malicious, filename: malicious }] }];
  const html = render(invoice); assert.doesNotMatch(html, /<script>/); assert.match(html, /&quot;&gt;&lt;script&gt;/);
  assert.match(html, /data-source="&quot;&gt;&lt;script&gt;/); assert.match(html, /data-download-doc="&quot;&gt;&lt;script&gt;/);
});
test('worklist displays the recorded next step without inferring a missing PO from summary data', () => {
  const invoice = fixture(); invoice.work = {kind:'replies',label:'Awaiting purchase order'};
  workflow.state.rows = [invoice]; workflow.state.total = 1;
  workflow.renderQueue();
  assert.match(nodes.get('#queue-list').innerHTML, /Awaiting purchase order/);
  assert.doesNotMatch(nodes.get('#queue-list').innerHTML, /Handoff approved/);
  invoice.work.label = '<script>Review receipt</script>';
  workflow.renderQueue();
  assert.match(nodes.get('#queue-list').innerHTML, /&lt;script&gt;Review receipt&lt;\/script&gt;/);
  assert.doesNotMatch(nodes.get('#queue-list').innerHTML, /<script>/);
});
test('confirmed Gmail receipts are inspectable and escaped; unknown sends never claim a receipt', () => {
  const invoice = fixture();
  const message = {direction:'outgoing',status:'sent',mailbox:'finance@example.test',message_id:'sent-123<script>',thread_id:'thread-456',sent_at:'2026-10-26T02:00:00Z'};
  invoice.integration.messages = [message];
  let markup = workflow.mailHtml(invoice,'replies');
  assert.match(markup, /Gmail receipt/); assert.match(markup, /finance@example.test/);
  assert.match(markup, /sent-123&lt;script&gt;/); assert.match(markup, /thread-456/); assert.doesNotMatch(markup, /<script>/);
  for (const status of ['pending','unknown','failed']) {
    message.status = status; markup = workflow.mailHtml(invoice,'replies');
    assert.doesNotMatch(markup, /class="provider-receipt"/);
  }
});
test('linked intake shows one source identity and one recovery action', () => {
  const invoice = reviewedInvoice(); invoice.request = null; invoice.integration.accounting.error = 'Refresh needed';
  const html = render(invoice); assert.match(html, /data-refresh-case/);
  assert.equal(count(html, /data-next="refresh"/g), 1);
  assert.doesNotMatch(html, /data-refresh-accounting/);
  assert.equal(count(html, /QuickBooks Online/g), 1);
  assert.match(html, /Refresh needed/);
  assert.match(render(fixture()), /data-refresh-accounting/, 'Prepared cases still have the accounting refresh');
});
const journey = ['intake', 'correspondence', 'review', 'handoff'];
const tabs = journey.map(name => Object.assign(node(), { dataset: { caseTab: name }, id: 'tab-' + name }));
const panels = tabs.map(tab => Object.assign(node(), { id: 'panel-' + tab.dataset.caseTab }));
panels.forEach(panel => nodes.set('#' + panel.id, panel));
nodeLists.set('[data-case-tab]', tabs); nodeLists.set('.case-panel', panels);
const assertActiveTab = name => {
  assert.equal(workflow.state.caseTab, name);
  for (const tab of tabs) {
    const selected = tab.dataset.caseTab === name;
    assert.equal(tab.getAttribute('aria-selected'), String(selected)); assert.equal(tab.tabIndex, selected ? 0 : -1);
  }
  assert.deepEqual(panels.filter(panel => !panel.hidden).map(panel => panel.id), ['panel-' + name]);
};
test('tabs retain dirty inputs and change only visibility, accessibility and focus', () => {
  const input = document.querySelector('#review-form input'), draft = document.querySelector('#reply-form textarea');
  input.value = 'PO edited'; draft.value = 'Unsent reply'; workflow.state.caseDirty = true;
  const markup = nodes.get('#case-content').innerHTML;
  for (const name of ['correspondence', 'intake', 'handoff', 'review']) {
    workflow.activateCaseTab(name, true); assertActiveTab(name);
    assert.equal(document.activeElement, tabs.find(tab => tab.dataset.caseTab === name));
    assert.equal(document.querySelector('#review-form input'), input);
    assert.equal(document.querySelector('#reply-form textarea'), draft);
    assert.equal(input.value, 'PO edited'); assert.equal(draft.value, 'Unsent reply');
    assert.equal(workflow.state.caseDirty, true); assert.equal(nodes.get('#case-content').innerHTML, markup);
  }
  workflow.activateCaseTab('invalid', true); assertActiveTab('review');
});
test('tab keyboard navigation wraps and supports Home and End without intercepting form input', () => {
  const keydown = nodes.get('#case-content').listeners.get('keydown');
  for (const [from, key, to] of [['intake', 'ArrowRight', 'correspondence'], ['correspondence', 'ArrowRight', 'review'],
    ['review', 'ArrowRight', 'handoff'], ['handoff', 'ArrowRight', 'intake'], ['intake', 'ArrowLeft', 'handoff'],
    ['handoff', 'Home', 'intake'], ['intake', 'End', 'handoff']]) {
    workflow.activateCaseTab(from); let prevented = false;
    keydown({ target: tabs.find(tab => tab.dataset.caseTab === from), key, preventDefault() { prevented = true; } });
    assert.equal(prevented, true); assertActiveTab(to);
    assert.equal(document.activeElement, tabs.find(tab => tab.dataset.caseTab === to));
  }
  const noIntercept = () => assert.fail('Native key behavior must be retained');
  keydown({ target: tabs[3], key: 'Enter', preventDefault: noIntercept }); assertActiveTab('handoff');
  keydown({ target: node(), key: 'Home', preventDefault: noIntercept }); assertActiveTab('handoff');
});
test('stage motion runs only on a changed tab without replacing fields', () => {
  const panel = nodes.get('#panel-review'), entered = [];
  const originalStage = ui.stage;
  const content = nodes.get('#case-content'), original = content.innerHTML;
  try {
    ui.stage = target => entered.push(target);
    workflow.state.caseTab = 'intake'; workflow.activateCaseTab('review');
    assert.deepEqual(entered,[panel]);
    assert.equal(content.innerHTML, original);
    workflow.activateCaseTab('review'); assert.equal(entered.length,1);
    workflow.renderCase(); assert.equal(entered.length,1,'Refreshing case markup must not replay entrance motion');
    ui.stage = undefined;
    workflow.state.caseTab = 'intake'; assert.doesNotThrow(() => workflow.activateCaseTab('review'));
  } finally {
    ui.stage = originalStage; content.innerHTML = original;
  }
});
test('rendered invoices have four linked journey tabs and exactly one visible panel', () => {
  for (const name of journey) {
    workflow.state.caseTab = name; const html = render(fixture());
    assert.equal(count(html, /role="tab"/g), 4); assert.equal(count(html, /role="tabpanel"/g), 4);
    assert.match(html, /class="journey-nav"/); assert.equal(count(html, /class="journey-stop[^" ]*(?: [^"]*)?"/g), 4);
    assert.doesNotMatch(html, /class="next-action"/);
    const renderedTabs = [...html.matchAll(/<button[^>]+role="tab"[^>]+>/g)].map(match => match[0]);
    assert.equal(renderedTabs.filter(tab => tab.includes('aria-selected="true"')).length, 1);
    assert.ok(renderedTabs.find(tab => tab.includes('aria-selected="true"')).includes('id="tab-' + name + '"'));
    const renderedPanels = [...html.matchAll(/<section[^>]+role="tabpanel"[^>]*>/g)].map(match => match[0]);
    assert.equal(renderedPanels.filter(panel => !/\shidden(?:\s|>)/.test(panel)).length, 1);
    for (const panel of renderedPanels) assert.match(panel, /aria-labelledby="tab-(intake|correspondence|review|handoff)"/);
  }
  workflow.state.caseTab = 'review';
});
test('source highlighting matches original text exactly and escapes source markup', () => {
  const text = 'Before <script>alert("source")</script> & after';
  const markup = workflow.sourceMarkup(text, '<script>alert("source")</script>');
  assert.equal(retainedSource(markup), text);
  assert.deepEqual(highlightedSource(markup), ['<script>alert("source")</script>']);
  assert.doesNotMatch(markup, /<script>/); assert.match(markup, /&lt;script&gt;alert\(&quot;source&quot;\)/);
  for (const snippet of ['', 'missing', '<SCRIPT>alert("source")</SCRIPT>', '&lt;script&gt;', 'Before  <script>']) {
    const unmatched = workflow.sourceMarkup(text, snippet);
    assert.equal(retainedSource(unmatched), text); assert.deepEqual(highlightedSource(unmatched), []);
  }
  assert.equal(retainedSource(workflow.sourceMarkup(null, '<script>')), '');
});
test('source line layout preserves original blank lines, tabs, CRLF and trailing newlines', () => {
  const text = '\tBefore &lt;source&gt;\r\n\r\n  Amount <100> & \'quoted\'\t\r\n';
  const markup = workflow.sourceMarkup(text);
  assert.equal(retainedSource(markup), text);
  assert.equal(count(markup, /class="source-line"/g), text.split('\n').length);
  assert.deepEqual(highlightedSource(markup), []); assert.doesNotMatch(markup, /<100>/);
});
test('reference differences emphasize only changed characters without rewriting evidence', () => {
  const invoice = reviewedInvoice(); invoice.status = 'mismatch'; invoice.proposed_po_reference = null;
  const check = invoice.checks.find(item => item.code === 'po_reference');
  Object.assign(check, {label:'PO reference',status:'fail',invoice_value:'CF-2026-071',document_value:'CF-2026-017'});
  const snapshot = JSON.stringify(invoice), html = render(invoice);
  assert.match(html, /PO reference differs/);
  assert.match(html, /CF-2026-0<mark class="value-difference">71<\/mark>/);
  assert.match(html, /CF-2026-0<mark class="value-difference">17<\/mark>/);
  assert.equal(count(html, /class="value-difference"/g), 2);
  assert.equal(JSON.stringify(invoice), snapshot);
  Object.assign(check, {invoice_value:'PO-<OLD>&',document_value:'PO-<NEW>&'});
  const escaped = render(invoice);
  assert.doesNotMatch(escaped, /<OLD>|<NEW>/);
  assert.match(escaped, /PO-&lt;<mark class="value-difference">OLD<\/mark>&gt;&amp;/);
  assert.match(escaped, /PO-&lt;<mark class="value-difference">NEW<\/mark>&gt;&amp;/);
});
test('multiline highlighting marks only the first exact retained passage', () => {
  const snippet = 'PO number: <PO & "NEW">\nCurrency: USD';
  const text = 'Original source\n' + snippet + '\nUnchanged remainder\n' + snippet + '\n';
  const markup = workflow.sourceMarkup(text, snippet);
  assert.equal(retainedSource(markup), text);
  assert.deepEqual(highlightedSource(markup), snippet.split('\n'));
  for (const mismatch of [snippet.toLowerCase(), snippet.replace('\n', ' '), snippet.replace('&', '&amp;')]) {
    const unmatched = workflow.sourceMarkup(text, mismatch);
    assert.equal(retainedSource(unmatched), text); assert.deepEqual(highlightedSource(unmatched), []);
  }
});
test('coverage conflicts show retained workspace allocations without an irrelevant reference edit', () => {
  const invoice = reviewedInvoice(); invoice.status = 'mismatch'; invoice.proposed_po_reference = null; invoice.po_reference = 'PO NEW';
  const coverage = invoice.checks.find(item => item.code === 'po_coverage');
  Object.assign(coverage, {status:'fail',invoice_value:10000,document_value:10000,available_cents:6000,reserved_cents:4000});
  const snapshot = JSON.stringify(invoice), html = render(invoice);
  assert.match(html, /PO coverage is short/); assert.match(html, /\$60\.00 available here/); assert.match(html, /\$40\.00 allocated/);
  assert.match(html, /External PO usage is not verified/);
  assert.doesNotMatch(html, /id="proposal-section"|data-next="approve"/);
  assert.match(html, /Request corrected PO/); assert.equal(JSON.stringify(invoice), snapshot);
});
test('review shows each conflict once and keeps only passing checks in its disclosure', () => {
  const invoice = reviewedInvoice(); invoice.status = 'mismatch'; invoice.proposed_po_reference = null;
  invoice.checks.find(check => check.code === 'po_reference').status = 'fail';
  const markup = render(invoice);
  assert.equal(count(markup,/data-check="po_reference"/g),1);
  const disclosure = markup.match(/class="case-disclosure checks-disclosure"[\s\S]*?<\/details>/)?.[0];
  assert.ok(disclosure); assert.match(disclosure,/3 of 4/);
  assert.doesNotMatch(disclosure,/data-check="po_reference"/);
  for (const code of ['customer','currency','po_coverage']) assert.match(disclosure,new RegExp('data-check="'+code+'"'));
  invoice.checks.forEach(check => { check.status = 'fail'; });
  const allFailed = render(invoice);
  assert.doesNotMatch(allFailed,/checks-disclosure|data-next="approve"/);
  assert.equal(count(allFailed,/data-check=/g),4);
});
test('source page changes preserve dirty forms and drafts and clamp pagination bounds', () => {
  const input = document.querySelector('#review-form input'), draft = document.querySelector('#reply-form textarea');
  input.value = 'PO unsaved'; draft.value = 'Unsaved reply'; workflow.state.caseDirty = true;
  const markup = nodes.get('#case-content').innerHTML;
  const doc = { pages: [{ number: 1, text: 'First page' }, { number: 2, text: '<source>Second page</source>' }] };
  for (const [index, page, text, previousDisabled, nextDisabled] of [[1, 1, '<source>Second page</source>', false, true],
    [-10, 0, 'First page', true, false], [100, 1, '<source>Second page</source>', false, true]]) {
    workflow.showDocumentPage(doc, index);
    assert.equal(workflow.state.page, page);
    assert.equal(document.querySelector('#document-preview .document-text').innerHTML, workflow.sourceMarkup(text));
    assert.equal(document.querySelector('.preview-pagination span').textContent, 'Page ' + (page + 1) + ' of 2');
    assert.equal(document.querySelector('[data-page-step="-1"]').disabled, previousDisabled);
    assert.equal(document.querySelector('[data-page-step="1"]').disabled, nextDisabled);
    assert.equal(document.querySelector('#review-form input'), input); assert.equal(document.querySelector('#reply-form textarea'), draft);
    assert.equal(input.value, 'PO unsaved'); assert.equal(draft.value, 'Unsaved reply');
    assert.equal(workflow.state.caseDirty, true); assert.equal(nodes.get('#case-content').innerHTML, markup);
  }
  workflow.showDocumentPage(doc, 1, '<source>Second page</source>');
  const highlightedPage = document.querySelector('#document-preview .document-text').innerHTML;
  assert.equal(retainedSource(highlightedPage), '<source>Second page</source>');
  assert.deepEqual(highlightedSource(highlightedPage), ['<source>Second page</source>']);
  assert.equal(document.querySelector('#review-form input'), input); assert.equal(document.querySelector('#reply-form textarea'), draft);
  assert.equal(input.value, 'PO unsaved'); assert.equal(draft.value, 'Unsaved reply');
  assert.equal(workflow.state.caseDirty, true); assert.equal(nodes.get('#case-content').innerHTML, markup);
  workflow.showDocumentPage({ pages: [] }, 10);
  assert.equal(workflow.state.page, 0); assert.match(document.querySelector('#document-preview .document-text').innerHTML, /No readable text/);
  assert.equal(document.querySelector('[data-page-step="-1"]').disabled, true);
  assert.equal(document.querySelector('[data-page-step="1"]').disabled, true);
});
test('offline request has one copy action and retains its email download', () => {
  const invoice = fixture(); invoice.status = 'missing_po'; invoice.resolution = null; invoice.integration = null;
  workflow.state.connections = null; const html = render(invoice);
  assert.equal(count(html, /data-next="request"|data-copy-request/g), 1); assert.match(html, /data-download-request/);
});
test('one invoice journey follows evidence and approval state rather than a fake completion sequence', () => {
  const invoice = fixture(); invoice.status = 'missing_po'; invoice.resolution = null;
  const current = () => Array.from(workflow.journeySteps(invoice), step => step).find(step => step.state === 'current')?.id;
  const assertSteps = () => {
    const steps = Array.from(workflow.journeySteps(invoice), step => step);
    assert.deepEqual(steps.map(step => step.id), journey);
    assert.ok(steps.every(step => step.label && step.caption && ['complete', 'current', 'upcoming'].includes(step.state)));
    assert.ok(steps.filter(step => step.state === 'current').length <= 1);
  };
  assertSteps(); assert.equal(current(), 'correspondence');
  invoice.documents = [poDocument()]; invoice.status = 'needs_review';
  assertSteps(); assert.equal(current(), 'review');
  Object.assign(invoice, reviewedInvoice()); assertSteps(); assert.equal(current(), 'handoff');
  invoice.status = 'prepared'; invoice.resolution = { po_reference: 'PO NEW', document_id: 'doc-1' };
  assertSteps(); assert.equal(workflow.journeySteps(invoice).find(step => step.id === 'handoff').caption, 'Approved');
  invoice.integration.accounting.error = 'Source unavailable'; assertSteps(); assert.equal(current(), 'intake');
});
test('confirmed request moves a missing-PO case to replies without a duplicate send action', () => {
  const invoice = fixture(); invoice.status = 'missing_po'; invoice.resolution = null;
  assert.equal(workflow.nextAction(invoice).kind, 'send');
  invoice.integration.messages = [{ direction: 'outgoing', status: 'sent', state: 'confirmed', message_id: 'msg-1',
    thread_id: 'thread-1', to: invoice.request.to, subject: invoice.request.subject, text: invoice.request.body }];
  assert.equal(workflow.nextAction(invoice).kind, 'replies');
  const html = render(invoice);
  assert.equal(count(html, /data-next="replies"|data-sync-replies/g), 1);
  assert.doesNotMatch(html, /data-next="send"|data-send-request/);
  assert.match(html, /Sent through Gmail/); assert.match(html, /data-next="upload"/);
  assert.equal(workflow.canApprove(invoice), false);
  invoice.integration.messages[0].status = 'unknown';
  assert.equal(workflow.nextAction(invoice).kind, 'verify-send');
  assert.equal(count(render(invoice), /data-next="verify-send"|data-reconcile-request/g), 1);
});
test('unreviewed TogetherAI fields show real source evidence and cannot authorize handoff', () => {
  const invoice = fixture(); invoice.status = 'needs_review'; invoice.resolution = null;
  invoice.documents = [poDocument('together')];
  invoice.checks = [{ code: 'po_reference', label: 'PO reference', status: 'hold', document_id: 'doc-1', source_page: 1 }];
  const snapshot = JSON.stringify(invoice); const html = render(invoice);
  assert.match(html, /TogetherAI suggestions/); assert.match(html, /id="review-form"/);
  assert.equal(count(html, /class="field-evidence"/g), 4);
  for (const [name, evidence] of Object.entries(invoice.documents[0].evidence)) {
    const button = [...html.matchAll(/<button[^>]+class="field-evidence"[^>]*>[\s\S]*?<\/button>/g)].find(match => match[0].includes(`data-field="${name}"`))?.[0];
    assert.ok(button, `Source button is linked to ${name}`);
    assert.match(button, /data-source="doc-1"/); assert.match(button, /data-page="1"/);
    assert.match(button, /title="[^"]+"/); assert.match(button, /aria-label="[^"]+"/);
    assert.ok(button.includes(evidence.snippet), 'The evidence context remains available to assistive technology');
    assert.match(button, />Source · p\.\s*1<\/button>/);
  }
  for (const snippet of ['PO number: PO NEW', 'Customer: Example', 'Currency: USD', 'PO amount: 100.00']) assert.ok(html.includes(snippet));
  assert.equal(workflow.canApprove(invoice), false); assert.doesNotMatch(html, /data-next="approve"/);
  assert.equal(JSON.stringify(invoice), snapshot, 'Rendering must not review a document or alter source records');
});
test('older purchase orders are read-only and never display the latest comparison as their own', () => {
  const invoice = reviewedInvoice(); invoice.status = 'mismatch';
  const older = { ...poDocument(), id: 'doc-old', filename: 'Earlier purchase order.txt', pages: [{ number: 1, text: 'Archived & <source>' }] };
  invoice.documents.unshift(older);
  invoice.checks.find(check => check.code === 'po_reference').status = 'fail';
  invoice.checks.find(check => check.code === 'po_reference').invoice_value = 'Latest comparison sentinel';
  render(invoice); workflow.state.docId = older.id;
  for (const reviewed of [false, true]) {
    older.reviewed = reviewed; workflow.renderCase();
    const html = nodes.get('#case-content').innerHTML;
    const review = html.match(/id="panel-review"[\s\S]*?(?=id="panel-handoff")/)?.[0];
    assert.ok(review); assert.match(review, /Earlier purchase order\.txt/);
    assert.match(review, /Archived &amp; &lt;source&gt;/); assert.match(review, /data-use-latest/);
    assert.doesNotMatch(html, /id="review-form"|id="proposal-form"/);
    assert.doesNotMatch(review, /Latest comparison sentinel|All checks|class="issue-focus"/);
    assert.equal(workflow.state.docId, older.id);
  }
  workflow.state.docId = 'doc-1'; workflow.renderCase();
  const current = nodes.get('#case-content').innerHTML;
  assert.match(current, /id="review-form"/); assert.match(current, /id="proposal-form"/);
  assert.match(current, /Latest comparison sentinel/); assert.match(current, /class="review-controls"/);
  assert.doesNotMatch(current, /data-use-latest/);
});
test('rules extraction and provider failure never impersonate successful AI review', () => {
  const invoice = fixture(); invoice.status = 'needs_review'; invoice.resolution = null; invoice.documents = [poDocument('rules')];
  let html = render(invoice); assert.match(html, /Document fields/); assert.doesNotMatch(html, /TogetherAI suggestions/);
  invoice.documents[0] = { ...poDocument('together'), suggested_fields: {}, evidence: {}, extraction_error: 'Provider timeout; review the source.' };
  html = render(invoice); assert.match(html, /Provider timeout; review the source\./);
  assert.equal(count(html, /class="field-evidence"/g), 0);
  assert.equal(workflow.canApprove(invoice), false); assert.doesNotMatch(html, /data-next="approve"/);
});
test('deterministic conflicts block approval and keep reference correction separate from the source invoice', () => {
  for (const code of ['customer', 'currency', 'po_reference', 'po_coverage']) {
    const invoice = reviewedInvoice(); invoice.status = 'mismatch';
    invoice.checks.find(check => check.code === code).status = 'fail';
    const original = JSON.stringify(invoice), html = render(invoice);
    assert.equal(workflow.canApprove(invoice), false); assert.doesNotMatch(html, /data-next="approve"/);
    assert.match(html, /class="issue-focus"/);
    if (code === 'po_reference') { assert.match(html, /id="proposal-section" open/); assert.match(html, /Save correction/); }
    else assert.match(html, /data-go-stage="correspondence">Request corrected PO/);
    assert.equal(JSON.stringify(invoice), original);
  }
});
test('manual prepared handoff remains unpaid and offers no implied external completion', () => {
  const invoice = reviewedInvoice(); invoice.integration = null; invoice.status = 'prepared';
  invoice.resolution = { po_reference: 'PO NEW', document_id: 'doc-1', prepared_at: '2026-10-01' };
  workflow.state.caseTab = 'handoff'; const original = JSON.stringify(invoice), html = render(invoice);
  assert.equal(count(html, /data-next="handoff"/g), 1);
  assert.match(html, /Unpaid/); assert.match(source, /No email is sent and QuickBooks is unchanged\./);
  assert.doesNotMatch(html, /data-apply-reference|data-send-request|PO correction verified|Sent through Gmail/);
  assert.equal(JSON.stringify(invoice), original); assert.equal(invoice.payment_status, 'unpaid');
  assert.doesNotMatch(workflow.journeySteps(invoice).find(step => step.id === 'handoff').caption, /paid|collected|synced|sent|resolved/i);
});
test('new evidence and saved resolutions advance the same invoice; unchanged refreshes retain the selected stage', () => {
  const missing = fixture(); missing.status = 'missing_po'; missing.resolution = null; missing.version = 1;
  workflow.state.invoice = missing; workflow.state.view = 'open'; workflow.state.rows = [missing]; workflow.state.total = 1;
  workflow.state.caseTab = 'correspondence'; workflow.state.docId = null; workflow.state.page = 8;
  const received = { ...missing, status: 'needs_review', version: 2, documents: [poDocument()] };
  workflow.updateInvoice(received);
  assert.equal(workflow.state.invoice, received); assert.equal(workflow.state.rows[0], received);
  assert.equal(workflow.state.caseTab, 'review'); assert.equal(workflow.state.docId, 'doc-1'); assert.equal(workflow.state.page, 0);
  assert.equal(workflow.canApprove(received), false);
  workflow.activateCaseTab('intake');
  const refreshed = { ...received, version: 3 };
  workflow.updateInvoice(refreshed);
  assert.equal(workflow.state.caseTab, 'intake'); assert.equal(workflow.state.docId, 'doc-1');
  const ready = { ...reviewedInvoice(), version: 4 };
  workflow.updateInvoice(ready, { followNext: true });
  assert.equal(workflow.state.caseTab, 'handoff'); assert.equal(workflow.state.invoice, ready);
  assert.match(nodes.get('#case-content').innerHTML, /data-next="approve"/);
  const prepared = { ...ready, status: 'prepared', version: 5, resolution: { po_reference: 'PO NEW', document_id: 'doc-1' } };
  workflow.updateInvoice(prepared, { followNext: true });
  assert.equal(workflow.state.caseTab, 'handoff'); assert.equal(workflow.state.invoice, prepared);
  assert.equal(workflow.state.rows.length, 0); assert.equal(workflow.state.total, 0);
  assert.match(nodes.get('#case-content').innerHTML, /data-next="handoff"/);
  assert.equal(prepared.payment_status, 'unpaid');
  workflow.activateCaseTab('correspondence'); workflow.updateInvoice({ ...prepared, version: 6 });
  assert.equal(workflow.state.caseTab, 'correspondence');
  workflow.updateInvoice({ ...prepared, id: 'another-invoice' }, { followNext: true });
  assert.equal(workflow.state.invoice.id, 'invoice-1'); assert.equal(workflow.state.caseTab, 'correspondence');
});
test('next invoice follows queue position and preserves the successor when approval removes the active row', () => {
  const rows = ['first', 'middle', 'last'].map(id => ({ ...reviewedInvoice(), id }));
  workflow.state.rows = rows.slice(); workflow.state.nextId = null;
  workflow.state.invoice = rows[0]; assert.equal(workflow.nextQueueId(), 'middle');
  workflow.state.invoice = rows[1]; assert.equal(workflow.nextQueueId(), 'last');
  workflow.state.invoice = rows[2]; assert.ok(!workflow.nextQueueId(), 'The final loaded row must not wrap to the start');
  workflow.state.invoice = rows[1]; workflow.state.view = 'open'; workflow.state.total = 3;
  workflow.updateInvoice({ ...rows[1], status: 'prepared', resolution: { po_reference: 'PO NEW', document_id: 'doc-1' } });
  assert.deepEqual(Array.from(workflow.state.rows, invoice => invoice.id), ['first', 'last']);
  assert.equal(workflow.nextQueueId(), 'last', 'Approval retains the original successor rather than returning to the first row');
  workflow.state.nextId = null; workflow.state.queueRemovedIndex = null; workflow.state.invoice = { ...fixture(), id: 'removed' };
  assert.equal(workflow.nextQueueId(), 'first', 'A case already absent from the queue can start with its first remaining row');
});
{
  const button = document.querySelector('#case-next'); let loads = 0, opened = [], apiCalls = 0;
  context.navigationHooks = {
    api: () => { apiCalls++; assert.fail('Moving to the next case must not write to an external provider'); },
    notify() {},
    loadQueue: async options => { assert.equal(options.offset, workflow.state.queueOffset + workflow.state.queueLimit); loads++; workflow.state.rows = [{ id: 'next-page' }]; workflow.state.queueOffset = options.offset; workflow.state.hasMore = false; return true; },
    openInvoice: async id => { opened.push(id); }
  };
  runInContext('globalThis.navigationOriginals = {api,notify,loadQueue,openInvoice}; api = navigationHooks.api; notify = navigationHooks.notify; loadQueue = navigationHooks.loadQueue; openInvoice = navigationHooks.openInvoice;', context);
  const reset = () => {
    workflow.state.invoice = { ...fixture(), id: 'active' }; workflow.state.rows = [{ id: 'earlier' }, { id: 'active' }];
    workflow.state.nextId = null; workflow.state.hasMore = true; workflow.state.busy = false; workflow.state.caseOpen = true; workflow.state.caseSequence = 30;
    Object.assign(workflow.state,{queueOffset:0,queueLimit:10,queueRemovedIndex:null,queueNeedsRefill:false,caseDirty:false});
    loads = 0; opened = [];
  };
  try {
    reset(); await workflow.advanceInvoice(button);
    assert.equal(loads, 1); assert.deepEqual(opened, ['next-page']); assert.equal(button.disabled, false);
    for (const changeCase of [() => { workflow.state.invoice = { id: 'different' }; }, () => { workflow.state.caseSequence++; }]) {
      reset(); context.navigationHooks.loadQueue = async () => { loads++; workflow.state.rows.push({ id: 'next-page' }); changeCase(); return true; };
      runInContext('loadQueue = navigationHooks.loadQueue;', context);
      await workflow.advanceInvoice(button);
      assert.equal(loads, 1); assert.deepEqual(opened, [], 'An obsolete page response must not move a different active case');
      assert.equal(button.disabled, false);
    }
    reset(); workflow.state.busy = true; await workflow.advanceInvoice(button);
    assert.equal(loads, 0); assert.deepEqual(opened, []); assert.equal(button.disabled, false);
    assert.equal(apiCalls, 0);
    passed++; process.stdout.write('PASS next-case pagination respects busy state and ignores navigation after the active case changes\n');
  } finally {
    workflow.state.busy = false;
    runInContext('({api,notify,loadQueue,openInvoice} = navigationOriginals); delete globalThis.navigationOriginals; delete globalThis.navigationHooks;', context);
  }
}
{
  const input = document.querySelector('#review-form input'), draft = document.querySelector('#reply-form textarea');
  input.value = 'PO navigation edit'; draft.value = 'Reply navigation edit';
  const markup = nodes.get('#case-content').innerHTML, button = document.querySelector('#case-next'); let prompts = 0;
  workflow.state.invoice = { ...fixture(), id: 'active' }; workflow.state.rows = [{ id: 'active' }, { id: 'next' }];
  workflow.state.nextId = null; workflow.state.hasMore = false; workflow.state.caseDirty = true; workflow.state.caseOpen = true;
  context.window.confirm = () => { prompts++; return false; };
  await workflow.advanceInvoice(button);
  assert.equal(prompts, 1); assert.equal(workflow.state.invoice.id, 'active'); assert.equal(workflow.state.caseDirty, true);
  assert.equal(input.value, 'PO navigation edit'); assert.equal(draft.value, 'Reply navigation edit');
  assert.equal(nodes.get('#case-content').innerHTML, markup); assert.equal(button.disabled, false);
  passed++; process.stdout.write('PASS next-case navigation retains unfinished fields when the leave confirmation is declined\n');
}
{
  const review = node(), reply = node(), caseContent = nodes.get('#case-content');
  review.dataset.dirty = 'true'; reply.dataset.dirty = 'true';
  const poInput = document.querySelector('#review-form input'); poInput.value = 'PO unfinished';
  reply.closest = selector => selector === '#case-content' ? caseContent : null;
  nodeLists.set('form[data-dirty]', [review, reply]);
  workflow.state.caseDirty = true; workflow.state.busy = false;
  let calls = 0, prompts = 0;
  context.window.confirm = () => { prompts++; return false; };
  const markup = caseContent.innerHTML;
  await workflow.operation(reply, '#reply-error', () => { calls++; });
  assert.equal(calls, 0); assert.equal(prompts, 1);
  assert.equal(poInput.value, 'PO unfinished'); assert.equal(caseContent.innerHTML, markup);
  assert.equal(workflow.state.caseDirty, true); assert.equal(workflow.state.busy, false);
  context.window.confirm = () => { prompts++; return true; };
  await workflow.operation(reply, '#reply-error', () => { calls++; });
  assert.equal(calls, 1); assert.equal(prompts, 2); assert.equal(workflow.state.busy, false);
  nodeLists.set('form[data-dirty]', [reply]);
  context.window.confirm = () => assert.fail('Saving the only unfinished form needs no discard prompt');
  await workflow.operation(reply, '#reply-error', () => { calls++; }); assert.equal(calls, 2);
  nodeLists.delete('form[data-dirty]');
  passed++; process.stdout.write('PASS saving a reply cannot silently discard an unfinished PO edit\n');
}
{
  let prompts = 0;
  context.window.confirm = () => { prompts++; return false; };
  workflow.state.caseDirty = true; workflow.state.invoice = fixture(); workflow.state.busy = false;
  const button = node();
  await workflow.refreshAccounting(button);
  await workflow.verifyReceipt(button, 'reconcile-request');
  await workflow.syncReplies(button);
  assert.equal(prompts, 3); assert.equal(button.disabled, false); assert.equal(workflow.state.caseDirty, true);
  await workflow.syncReplies(button, true); assert.equal(prompts, 3);
  workflow.state.caseDirty = false;
  assert.equal(workflow.confirmDraftDiscard(), true); assert.equal(prompts, 3);
  passed++; process.stdout.write('PASS refresh and receipt actions respect unfinished edits; background polling stays quiet\n');
}
{
  const operations = [
    ['invoice refresh', button => workflow.refreshInvoice(button)],
    ['accounting refresh', button => workflow.refreshAccounting(button)],
    ['Gmail receipt', button => workflow.verifyReceipt(button, 'reconcile-request')],
    ['QuickBooks receipt', button => workflow.verifyReceipt(button, 'reconcile-reference')],
    ['manual reply sync', button => workflow.syncReplies(button)],
    ['background reply sync', button => workflow.syncReplies(button, true)]
  ];
  const content = nodes.get('#case-content'), input = document.querySelector('#reply-form textarea'), form = node();
  form.id = 'reply-form';
  const edit = value => {
    input.value = value;
    content.innerHTML = '<form>UNSAVED: ' + value + '</form>';
    content.listeners.get('input')({target:{closest:selector => selector === 'form' ? form : null}});
  };
  let complete, fail, notices;
  context.refreshHooks = {api: (_path, options = {}) => new Promise((resolve, reject) => { complete = result => resolve(options.method === 'POST' ? result : result.invoice); fail = reject; }), notify: message => notices.push(message)};
  runInContext('globalThis.refreshOriginals = {api,notify}; api = refreshHooks.api; notify = refreshHooks.notify;', context);
  context.window.confirm = () => true;
  const setup = () => {
    connect();
    const invoice = {...fixture(), version:1};
    Object.assign(workflow.state, {invoice,rows:[invoice],view:'all',total:1,caseDirty:false,caseEdits:0,caseSequence:80,busy:false,replyPolling:false,replyPollError:''});
    content.innerHTML = 'INITIAL CASE'; input.value = ''; notices = [];
    return {invoice,button:node(),result:{invoice:{...invoice,version:2},added:1}};
  };
  try {
    for (const [name, invoke] of operations) {
      const {invoice,button,result} = setup(), pending = invoke(button);
      edit('Typed while ' + name + ' was loading');
      const markup = content.innerHTML, value = input.value;
      complete(result); await pending;
      assert.equal(workflow.state.invoice, invoice, name);
      assert.equal(workflow.state.caseDirty, true, name + ' must retain the dirty flag');
      assert.equal(content.innerHTML, markup, name + ' must retain the form DOM');
      assert.equal(input.value, value); assert.equal(button.disabled, false);
      if (name.startsWith('background')) assert.deepEqual(notices, []);
      else assert.match(notices.join(' '), /edits.*kept|kept.*edits/i);
    }
    passed++; process.stdout.write('PASS delayed refresh and receipt responses preserve edits begun during the request\n');
    for (const [name, invoke] of operations) {
      const {button,result} = setup(), pending = invoke(button);
      complete(result); await pending;
      assert.equal(workflow.state.invoice, result.invoice, name);
      assert.equal(workflow.state.caseDirty, false); assert.equal(button.disabled, false);
    }
    passed++; process.stdout.write('PASS unedited refresh and receipt responses still update the active invoice\n');
    for (const [name, invoke] of operations.filter(([name]) => !name.startsWith('background'))) {
      const {button,result} = setup(); edit('Existing edit accepted for discard');
      const pending = invoke(button); complete(result); await pending;
      assert.equal(workflow.state.invoice, result.invoice, name);
      assert.equal(workflow.state.caseDirty, false); assert.equal(button.disabled, false);
      const next = setup(); edit('Existing edit accepted for discard');
      const waiting = invoke(next.button); edit('Additional edit after confirming');
      const markup = content.innerHTML;
      complete(next.result); await waiting;
      assert.equal(workflow.state.invoice, next.invoice, name);
      assert.equal(workflow.state.caseDirty, true); assert.equal(content.innerHTML, markup);
    }
    passed++; process.stdout.write('PASS explicit discard confirmation still permits a refresh when no new edits follow\n');
    for (const [name, invoke] of operations) {
      for (const change of ['saved','navigation','busy']) {
        const {invoice,button,result} = setup(), pending = invoke(button);
        if (change === 'saved') workflow.state.invoice = {...invoice,version:3};
        if (change === 'navigation') workflow.state.caseSequence++;
        if (change === 'busy') workflow.state.busy = true;
        const current = workflow.state.invoice; content.innerHTML = 'NEWER CASE DOM';
        complete(result); await pending;
        assert.equal(workflow.state.invoice, current, name + ': ' + change);
        assert.equal(content.innerHTML, 'NEWER CASE DOM'); assert.equal(button.disabled, false);
        if (change !== 'busy') assert.deepEqual(notices, []);
      }
      const {button} = setup(), pending = invoke(button);
      edit('Keep this draft after failure');
      const markup = content.innerHTML;
      fail(new Error('Connection unavailable')); await pending;
      assert.equal(workflow.state.caseDirty, true); assert.equal(content.innerHTML, markup);
      assert.equal(button.disabled, false);
    }
    passed++; process.stdout.write('PASS obsolete responses, in-flight saves and provider failures cannot overwrite the active case\n');
  } finally {
    Object.assign(workflow.state, {caseDirty:false,busy:false,replyPolling:false});
    runInContext('({api,notify} = refreshOriginals); delete globalThis.refreshOriginals; delete globalThis.refreshHooks;', context);
  }
}
const [html, css, folioCss] = await Promise.all(['index.html', 'styles.css', 'folio.css'].map(file => readFile(new URL(`../web/${file}`, import.meta.url), 'utf8')));
{
  const form = document.querySelector('#entry-form'), dialog = document.querySelector('#entry-dialog'), button = node();
  form.id = 'entry-form'; button.textContent = 'Add invoice';
  nodeLists.set('button[type="submit"]', [button]);
  let closes = 0, resets = 0, queueLoads = 0, notices = 0, opened = [], shouldFail = false, prevented = 0;
  dialog.close = () => { closes++; }; form.reset = () => { resets++; };
  const previousFormData = context.FormData;
  context.FormData = class { *[Symbol.iterator]() { yield ['invoice_number', ' INV-created ']; yield ['currency', ' usd ']; } };
  context.entryHooks = {
    api: async (path, options) => {
      assert.equal(workflow.state.busy, true); assert.equal(path, '/api/invoices'); assert.equal(options.method, 'POST');
      assert.equal(options.body.invoice_number, 'INV-created'); assert.equal(options.body.currency, 'USD');
      if (shouldFail) throw new Error('Invoice creation failed');
      return { id: 'created-42' };
    },
    loadQueue: async () => { assert.equal(workflow.state.busy, true); queueLoads++; },
    notify: message => { assert.equal(message, 'Invoice added.'); notices++; },
    openInvoice: async id => { assert.equal(workflow.state.busy, false, 'Opening inside operation would trigger the busy guard'); opened.push(id); }
  };
  runInContext('globalThis.entryOriginals = {api,loadQueue,notify,openInvoice}; api = entryHooks.api; loadQueue = entryHooks.loadQueue; notify = entryHooks.notify; openInvoice = entryHooks.openInvoice;', context);
  try {
    workflow.state.busy = false; workflow.state.caseDirty = false;
    const submit = form.listeners.get('submit');
    await submit({ target: form, preventDefault() { prevented++; } });
    assert.deepEqual(opened, ['created-42']); assert.equal(queueLoads, 1); assert.equal(closes, 1); assert.equal(resets, 1); assert.equal(notices, 1);
    assert.equal(workflow.state.busy, false); assert.equal(button.disabled, false);
    shouldFail = true;
    await submit({ target: form, preventDefault() { prevented++; } });
    assert.deepEqual(opened, ['created-42'], 'Failed creation must not navigate');
    assert.equal(queueLoads, 1); assert.equal(closes, 1); assert.equal(resets, 1); assert.equal(notices, 1); assert.equal(prevented, 2);
    assert.equal(nodes.get('#entry-error').textContent, 'Invoice creation failed');
    assert.equal(workflow.state.busy, false); assert.equal(button.disabled, false);
    passed++; process.stdout.write('PASS Add invoice opens the returned record after the busy guard clears; failed creation stays in the form\n');
  } finally {
    runInContext('({api,loadQueue,notify,openInvoice} = entryOriginals); delete globalThis.entryOriginals; delete globalThis.entryHooks;', context);
    context.FormData = previousFormData; nodeLists.delete('button[type="submit"]');
  }
}
{
  const form = node(), button = node(); form.id = 'upload-form'; button.textContent = 'Add document';
  nodeLists.set('button[type="submit"]', [button]); workflow.state.busy = false;
  try {
    for (const fail of [false, true]) {
      await workflow.operation(form, '#upload-error', async () => {
        assert.equal(workflow.state.busy, true); assert.equal(button.disabled, true);
        assert.equal(button.textContent, 'Reading purchase order…'); assert.equal(form.getAttribute('aria-busy'), 'true');
        if (fail) throw new Error('Document provider unavailable');
      });
      assert.equal(workflow.state.busy, false); assert.equal(button.disabled, false); assert.equal(button.textContent, 'Add document');
      assert.equal(form.getAttribute('aria-busy'), undefined);
    }
    assert.equal(nodes.get('#upload-error').textContent, 'Document provider unavailable');
    passed++; process.stdout.write('PASS purchase-order reading state restores the button label and controls on success and failure\n');
  } finally { nodeLists.delete('button[type="submit"]'); }
}
test('static brand, footer, export and reduced motion contract remains', () => {
  assert.match(html, /<title>InvoiceClear<\/title>/);
  assert.match(html, /class="product-subname">AI Agent for Invoice Resolution<\/p>/);
  assert.match(html, /class="brand-subname">AI Agent for Invoice Resolution<\/span>/);
  assert.match(css, /\.brand-subname\{[^}]*white-space:normal/);
  assert.match(html, /class="brand-lockup"[^\n]*hermit-edge-mark\.png[^\n]*InvoiceClear/);
  assert.match(html, /<footer class="site-footer">© 2026 Emman Ermitaño\. All rights reserved\.<\/footer>/);
  assert.match(html, /id="export"/); assert.match(css, /padding-bottom:var\(--footer\)/);
  assert.match(css, /prefers-reduced-motion/);
  assert.doesNotMatch(css, /\.case-panel\{[^}]*animation:/);
  assert.doesNotMatch(html, /Keep confidential records in a private workspace/);
  assert.doesNotMatch(html, /Public access\. Uploads are shared\.|public-access-note/);
  assert.match(html, /Records are visible to other visitors\./);
  assert.match(html, /class="col-status">Next step</);
});
test('worklist navigation cues remain decorative and invoice values remain escaped', () => {
  const previous = {...workflow.state};
  try {
    workflow.state.rows = [{...fixture(),customer:'<img src=x onerror=alert(1)>',invoice_number:'INV-<script>'}];
    workflow.state.total = 1; workflow.state.hasMore = false;
    workflow.renderQueue();
    const markup = nodes.get('#queue-list').innerHTML;
    assert.match(markup,/class="row-open"[^>]*aria-hidden="true"/);
    assert.match(markup,/aria-label="Review invoice INV-&lt;script&gt;/);
    assert.match(markup,/&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.doesNotMatch(markup,/<img|<script|tabindex="0"/);
    assert.match(html,/<label class="queue-search"><svg[^>]*aria-hidden="true"/);
  } finally {Object.assign(workflow.state,previous);}
});
test('unconfigured investigation stays out of the customer workflow', () => {
  const invoice = {...reviewedInvoice(),investigation:{available:false,reason:'Configure <backend> & account',latest:null}};
  const markup = workflow.investigationHtml(invoice);
  assert.equal(markup, '');
  assert.doesNotMatch(source,/Configure TogetherAI|data-investigation-setup/);
});
test('material styling keeps native motion bounded and phone fields readable', () => {
  assert.match(css,/dialog\[open\]\{animation:dialog-enter \.2s/);
  assert.match(css,/@keyframes dialog-enter/);
  assert.match(css,/@media\(max-width:720px\)[^\n]*font-size:16px/);
  assert.match(css,/@media\(prefers-contrast:more\)/);
  assert.match(css,/caret-color:var\(--violet\)/);
  assert.doesNotMatch(css,/animation:[^;}]*infinite|backdrop-filter:[^;}]*20px/);
});
test('unchanged passive refresh preserves the case DOM, focus and disclosures', () => {
  const invoice = {...reviewedInvoice(),version:8}, content = nodes.get('#case-content');
  Object.assign(workflow.state,{invoice,rows:[invoice],view:'all',caseSequence:61,caseEdits:4,caseDirty:false,busy:false,replyPollError:''});
  const request = {invoice,sequence:61,edits:4,replyError:''};
  const focused = document.querySelector('#proposal-form input'); focused.focus();
  content.innerHTML = 'Retained expanded controls';
  assert.equal(workflow.applyCaseRefresh(JSON.parse(JSON.stringify(invoice)),request,true),false);
  assert.equal(workflow.state.invoice,invoice); assert.equal(content.innerHTML,'Retained expanded controls');
  assert.equal(document.activeElement,focused);
  const changed = JSON.parse(JSON.stringify(invoice)); changed.checks[0].document_value = 'Updated evidence';
  assert.equal(workflow.applyCaseRefresh(changed,request,true),true,'Same-version derived checks must still update');
  assert.equal(workflow.state.invoice,changed); assert.notEqual(content.innerHTML,'Retained expanded controls');
  const next = {invoice:changed,sequence:61,edits:4,replyError:''};
  assert.equal(workflow.applyCaseRefresh(changed,next,false),true,'An explicit refresh remains available');
});
{
  const invoice = {...reviewedInvoice(),version:10}, content = nodes.get('#case-content');
  Object.assign(workflow.state,{invoice,rows:[invoice],view:'all',caseSequence:62,caseEdits:0,caseDirty:false,busy:false,replyPolling:false,replyPollError:''});
  context.quietReplyApi = async () => { throw new Error('Mailbox temporarily unavailable'); };
  runInContext('globalThis.quietReplyOriginalApi = api; api = quietReplyApi;',context);
  try {
    await workflow.syncReplies(null,true); assert.match(content.innerHTML,/Mailbox temporarily unavailable/);
    content.innerHTML = 'Retained controls after first error';
    await workflow.syncReplies(null,true); assert.equal(content.innerHTML,'Retained controls after first error');
    context.quietReplyApi = async () => ({invoice:JSON.parse(JSON.stringify(invoice)),failures:[]});
    runInContext('api = quietReplyApi;',context);
    await workflow.syncReplies(null,true);
    assert.equal(workflow.state.replyPollError,''); assert.doesNotMatch(content.innerHTML,/Mailbox temporarily unavailable/);
    passed++; process.stdout.write('PASS repeated passive errors retain controls and successful recovery clears the error\n');
  } finally { runInContext('api = quietReplyOriginalApi; delete globalThis.quietReplyOriginalApi; delete globalThis.quietReplyApi;',context); }
}
test('visible provenance labels are absent while internal credentials and access boundaries remain', () => {
  assert.doesNotMatch(html, /\b(?:synthetic|sample|demo)\b/i); assert.doesNotMatch(html, /id="access-mode"/);
  assert.match(source, /state\.runtime\.mode === "demo" && state\.runtime\.demo_credentials/);
  assert.match(source, /const privateAccess = ready\.mode === "private" && ready\.external_checks_allowed/);
  assert.match(html.match(/<button id="connections-open"[^>]*>/)?.[0] || '', /\shidden(?:\s|>)/);
  assert.match(source, /state\.runtime\.demo_credentials\.email/); assert.match(source, /state\.runtime\.demo_credentials\.password/);
});
{
  const fixtureCsv = await readFile(new URL('../../backend/fixtures/public/invoices.csv', import.meta.url), 'utf8');
  const previousRuntime = workflow.state.runtime, previousBlob = context.Blob, captured = [];
  context.Blob = Blob;
  context.templateDownload = (blob,name) => captured.push({blob,name});
  runInContext('globalThis.originalTemplateDownload = download; download = templateDownload;',context);
  try {
    for (const mode of ['demo','private']) {
      workflow.state.runtime = {mode};
      nodes.get('#template-download').listeners.get('click')();
      const {blob,name} = captured.at(-1), csv = await blob.text();
      assert.equal(name,'invoiceclear-import-template.csv');
      assert.equal(blob.type,'text/csv;charset=utf-8');
      assert.equal(csv.replace(/\r\n/g,'\n'),fixtureCsv.replace(/\r\n/g,'\n'));
      const [header,row,...extra] = csv.trimEnd().split(/\r?\n/).map(line => line.split(','));
      assert.equal(header.length,9); assert.equal(row.length,9); assert.deepEqual(extra,[]);
      assert.deepEqual(Object.fromEntries(header.map((key,index) => [key,row[index]])),{
        invoice_number:'STA-2026-2101',customer:'Mercer Valley Home Health LLC',
        customer_email:'ap@mercervalleyhomehealth.example',amount:'18450.00',currency:'USD',
        issue_date:'2026-08-16',due_date:'2026-09-15',po_reference:'MVH-2026-2181',
        source_ref:'HB-20260816-US / MVH-04018 / Columbus East / Clinical float pool / Net 30'
      });
      assert.match(row[2],/@[^@]+\.example$/);
      assert.doesNotMatch(csv,/Example Company|INV-001/);
    }
    assert.equal(captured.length,2);
    passed++; process.stdout.write('PASS CSV template downloads the matching nine-column source invoice in both workspace modes\n');
  } finally {
    workflow.state.runtime = previousRuntime; context.Blob = previousBlob;
    runInContext('download = originalTemplateDownload; delete globalThis.originalTemplateDownload; delete globalThis.templateDownload;',context);
  }
}
test('dimensional artwork is local, decorative and independent of sign-in controls', () => {
  assert.match(html, /<link rel="stylesheet" href="\/folio\.css">/);
  const artwork = html.slice(html.indexOf('<div class="access-art"'), html.indexOf('<div class="access-entry"'));
  assert.match(artwork, /aria-hidden="true"/);
  assert.equal(count(artwork, /class="folio-leaf /g), 3);
  assert.doesNotMatch(artwork, /<(?:input|button|canvas|iframe)|tabindex=|https?:/);
  assert.match(folioCss, /pointer-events:\s*none/);
  assert.match(folioCss, /transform-style:\s*preserve-3d/);
  assert.doesNotMatch(folioCss, /infinite|@import|https?:\/\//);
});
test('folio motion is finite and its small-screen grid cannot expand to unscaled paper height', () => {
  assert.match(folioCss, /grid-template-rows:\s*minmax\(0,\s*1fr\)/);
  assert.match(folioCss, /max-width:\s*720px/);
  assert.match(folioCss, /max-width:\s*440px/);
  assert.match(folioCss, /prefers-reduced-motion:\s*reduce[\s\S]*animation:\s*none/);
  const durations = [...folioCss.matchAll(/animation:\s*folio-[\w-]+\s+([\d.]+)s/g)];
  assert.equal(durations.length, 6);
  assert.ok(durations.every(match => Number(match[1]) <= 3));
  assert.doesNotMatch(css + folioCss, /access-motif|alignment-line|align-once/);
});
const readinessFixture = (mode = 'private') => {
  const provider = extra => ({ configured: false, connected: false, callback_url: '', required_environment: [], missing_environment: [],
    verification: { status: 'not_checked', checked_at: null, message: '' }, ...extra });
  return {
    mode, external_checks_allowed: mode === 'private',
    providers: {
      qbo: provider({ environment: 'sandbox', po_field_configured: false, required_environment: ['QBO_CLIENT_ID', 'QBO_CLIENT_SECRET'] }),
      gmail: provider({ required_environment: ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'] }),
      together: provider({ selected: false, model: 'meta-llama/test', required_environment: ['TOGETHER_API_KEY'] })
    },
    storage: { database: { status: 'ok' }, backups: { enabled: false, status: 'not_configured' } },
    usage: { calls: 0, tokens: 0, call_limit: 100, token_limit: 500000 },
    limits: { document_bytes: 5242880, pdf_pages: 20, model_timeout_seconds: 20 }
  };
};
{
  const originalReady = workflow.state.readiness, originalLocation = context.window.location;
  const captured = [];
  context.Blob = Blob; context.URL = URL;
  context.window.location = {origin:'https://frontend.example.test'};
  context.setupDownload = (blob,name) => captured.push({blob,name});
  runInContext('globalThis.originalSetupDownload = download; download = setupDownload;',context);
  try {
    for (const selected of [false,true]) {
      const ready = readinessFixture(); ready.providers.together.selected = selected;
      ready.investigation = {enabled:false,available:false};
      ready.providers.together.model = 'chosen-json-model';
      ready.providers.together.secret = 'NEVER_INCLUDE_PROVIDER_SECRET';
      ready.providers.qbo.callback_url = 'https://backend.example.test/api/connections/qbo/callback';
      workflow.state.readiness = ready; workflow.downloadSetup();
      const {blob,name} = captured.at(-1), text = await blob.text();
      assert.equal(name,'invoiceclear-connection-setup.txt');
      const active = text.split('\n').filter(line => /^[A-Z][A-Z_]*=/.test(line)).join('\n');
      assert.match(active,new RegExp('INVOICECLEAR_PROVIDER='+(selected ? 'together' : 'rules')));
      assert.equal(/^TOGETHER_API_KEY=/m.test(active),selected);
      assert.equal(/^TOGETHER_MODEL=/m.test(active),selected);
      assert.equal(/^INVOICECLEAR_AGENT_ENABLED=false$/m.test(active),selected);
      assert.match(active,/RAILWAY_RUN_UID=0/); assert.match(active,/INVOICECLEAR_DB_PATH=\/data\//);
      assert.match(text,/Run make -C backend release/); assert.match(text,/API as UID\/GID 10001/);
      assert.doesNotMatch(text,/NEVER_INCLUDE_PROVIDER_SECRET|Run make check/);
    }
    const agentReady = readinessFixture();
    agentReady.providers.together.selected = false;
    agentReady.investigation = {enabled:true,available:true,model:'one-shared-tool-model'};
    workflow.state.readiness = agentReady; workflow.downloadSetup();
    const agentSetup = await captured.at(-1).blob.text();
    assert.match(agentSetup,/^INVOICECLEAR_PROVIDER=rules$/m);
    assert.doesNotMatch(agentSetup,/^INVOICECLEAR_AGENT_ENABLED=/m);
    assert.match(agentSetup,/^# INVOICECLEAR_AGENT_ENABLED=false$/m);
    assert.match(agentSetup,/^TOGETHER_API_KEY=$/m);
    assert.match(agentSetup,/^TOGETHER_MODEL=one-shared-tool-model$/m);
    passed++; process.stdout.write('PASS agent-only setup exports active credentials without enabling redundant extraction calls\n');
    workflow.state.readiness = null; workflow.downloadSetup(); assert.equal(captured.length,3);
    passed++; process.stdout.write('PASS setup download preserves extraction selection, optional keys and safe container guidance\n');
  } finally {
    workflow.state.readiness = originalReady; context.window.location = originalLocation;
    runInContext('download = originalSetupDownload; delete globalThis.originalSetupDownload; delete globalThis.setupDownload;',context);
  }
}
const providerActions = (markup, action) => [...markup.matchAll(new RegExp(`data-${action}-provider="([^"]+)"`, 'g'))].map(match => match[1]).sort();
const renderReadiness = readiness => {
  workflow.state.runtime = { mode: readiness.mode };
  workflow.state.readiness = readiness;
  if (readiness.mode === "private") workflow.state.connections = {qbo:{...readiness.providers.qbo},gmail:{...readiness.providers.gmail}};
  workflow.renderConnections();
  return nodes.get('#connections-content').innerHTML;
};
test('public readiness never exposes provider actions, including stale private connection state', () => {
  const readiness = readinessFixture('demo');
  for (const provider of Object.values(readiness.providers)) Object.assign(provider, { configured: true, connected: true, selected: true });
  workflow.state.connections = { qbo: { configured: true, connected: true, po_field_configured: true }, gmail: { configured: true, connected: true } };
  const original = JSON.stringify(readiness), markup = renderReadiness(readiness);
  assert.match(markup, /QuickBooks Online/); assert.match(markup, /Gmail/); assert.match(markup, /Together/);
  assert.doesNotMatch(markup, /data-(?:connect|disconnect|verify)-provider|data-sync-invoices|data-capture-enabled/);
  assert.equal(JSON.stringify(readiness), original, 'Rendering readiness must not manufacture provider verification');
});
test('private provider actions require actual configuration, grants and the selected extraction path', () => {
  const readiness = readinessFixture();
  readiness.providers.qbo.configured = true;
  readiness.providers.gmail.connected = true;
  Object.assign(readiness.providers.together, { selected: true, configured: true });
  let markup = renderReadiness(readiness);
  assert.deepEqual(providerActions(markup, 'connect'), ['qbo']);
  assert.deepEqual(providerActions(markup, 'verify'), ['together']);
  readiness.providers.qbo.connected = true;
  Object.assign(readiness.providers.gmail, { configured: true, connected: false });
  readiness.providers.together.selected = false;
  markup = renderReadiness(readiness);
  assert.deepEqual(providerActions(markup, 'connect'), ['gmail']);
  assert.deepEqual(providerActions(markup, 'verify'), ['qbo']);
  Object.assign(readiness.providers.gmail, { connected: true });
  readiness.providers.qbo.configured = false;
  markup = renderReadiness(readiness);
  assert.deepEqual(providerActions(markup, 'connect'), []);
  assert.deepEqual(providerActions(markup, 'verify'), ['gmail']);
});
test('configuration and saved grants do not impersonate successful provider verification', () => {
  const readiness = readinessFixture();
  for (const provider of Object.values(readiness.providers)) Object.assign(provider, { configured: true, connected: true, selected: true });
  const original = JSON.stringify(readiness), markup = renderReadiness(readiness);
  assert.doesNotMatch(markup, />\s*(?:Verified|Verification passed|Check passed)\s*</i);
  assert.match(markup, /not checked|not verified/i);
  assert.equal(JSON.stringify(readiness), original);
});
test('setup exposes the PO field and model settings without gating a read-only access check on the mapping', () => {
  const readiness = readinessFixture();
  Object.assign(readiness.providers.qbo, { configured: true, connected: true });
  readiness.providers.qbo.missing_environment = ['QBO_CLIENT_SECRET'];
  const original = JSON.stringify(readiness), markup = renderReadiness(readiness);
  assert.match(markup, /QBO_ENVIRONMENT/); assert.match(markup, /QBO_PO_FIELD_ID<\/code><span>Needed for sync/);
  assert.match(markup, /TOGETHER_MODEL/); assert.match(markup, /QBO_CLIENT_SECRET<\/code><span>Required/);
  assert.ok(providerActions(markup, 'verify').includes('qbo'));
  assert.doesNotMatch(markup, /data-sync-invoices/);
  assert.equal(JSON.stringify(readiness), original);
});
test('Connections retains the next-batch action after rerender and resets it for a completed sync', () => {
  const readiness = readinessFixture();
  Object.assign(readiness.providers.qbo, { configured: true, connected: true, po_field_configured: true });
  try {
    workflow.state.syncOffset = 100;
    for (let renderCount = 0; renderCount < 2; renderCount++) {
      const markup = renderReadiness(readiness);
      assert.match(markup, /data-sync-invoices>Sync next batch<\/button>/);
      assert.equal(workflow.state.syncOffset, 100);
    }
    workflow.state.syncOffset = 0;
    assert.match(renderReadiness(readiness), /data-sync-invoices>Sync invoices<\/button>/);
  } finally { workflow.state.syncOffset = 0; }
});
test('prepared handoffs expose one brief download and unapproved cases expose none', () => {
  const invoice = reviewedInvoice(); invoice.integration = null;
  assert.doesNotMatch(render(invoice), /data-download-brief/);
  invoice.status = 'prepared'; invoice.resolution = { po_reference: 'PO NEW', document_id: 'doc-1' };
  const original = JSON.stringify(invoice), markup = render(invoice);
  assert.equal(count(markup, /data-download-brief/g), 1);
  assert.equal(count(markup, /data-next="handoff"/g), 1);
  assert.equal(JSON.stringify(invoice), original); assert.equal(invoice.payment_status, 'unpaid');
  invoice.resolution = null;
  assert.doesNotMatch(render(invoice), /data-download-brief/, 'An inconsistent prepared status cannot expose a brief without an active approval');
});
test('history shows recorded summaries without arbitrary payloads or fabricated events', () => {
  const markup = workflow.historyHtml({events:[{summary:'PO <confirmed>',actor:'reviewer <script>bad</script>',recorded_at:'2026-10-21T01:00:00Z',details:{note:'Checked <source>',secret:'NEVER_RENDER',raw_response:'NEVER_RENDER'}}]});
  assert.match(markup,/PO &lt;confirmed&gt;/); assert.match(markup,/Checked &lt;source&gt;/);
  assert.doesNotMatch(markup,/<script>|NEVER_RENDER/);
  const sourceEvent={summary:'Source document retained',actor:'public-fixture-seed',recorded_at:'2026-09-02T14:00:00Z'};
  const sourceHistory=workflow.historyHtml({events:[sourceEvent]});
  assert.match(sourceHistory,/Source records/);assert.doesNotMatch(sourceHistory,/public-fixture-seed/);
  assert.equal(sourceEvent.actor,'public-fixture-seed','Readable attribution never rewrites original provenance');
  assert.match(workflow.historyHtml({events:[]}),/No changes recorded/);
  assert.doesNotMatch(workflow.historyHtml({events:[]}),/history-list/);
});
{
  const ready = readinessFixture(), paths = [];
  workflow.state.invoice = null; workflow.state.readiness = null; workflow.state.token = 'local-test-token';
  context.releaseApi = async path => { paths.push(path); return path === '/api/readiness' ? ready : path === '/api/capture' ? {enabled:false,available:false} : {qbo:ready.providers.qbo,gmail:ready.providers.gmail}; };
  runInContext('globalThis.releaseOriginalApi = api; api = releaseApi;',context);
  try {
    await workflow.loadConnections(); assert.deepEqual(paths,['/api/connections','/api/capture']); assert.equal(workflow.state.readiness,null);
    paths.length = 0; await workflow.loadConnections({readiness:true});
    assert.deepEqual(paths,['/api/connections','/api/capture','/api/readiness']); assert.equal(workflow.state.readiness,ready);
    passed++; process.stdout.write('PASS readiness is fetched only when explicitly requested and uses authenticated routes\n');
  } finally { runInContext('api = releaseOriginalApi; delete globalThis.releaseOriginalApi; delete globalThis.releaseApi;',context); }
}
{
  const panel = document.querySelector('#case-history-content'); document.querySelector('#case-history').open = true;
  workflow.state.invoice = {id:'history-current',version:3}; workflow.state.history = null; workflow.state.caseDirty = true;
  const input = document.querySelector('#review-form input'); input.value = 'UNSAVED';
  let resolveOld; context.releaseHistoryApi = () => new Promise(resolve => { resolveOld = resolve; });
  runInContext('globalThis.historyOriginalApi = api; api = releaseHistoryApi;',context);
  try {
    const pending = workflow.loadHistory(); workflow.state.invoice = {id:'history-other',version:1}; workflow.state.history = null; panel.innerHTML = 'Other invoice';
    resolveOld({events:[{summary:'Stale result'}],has_more:false,next_before:null}); await pending;
    assert.equal(panel.innerHTML,'Other invoice'); assert.equal(workflow.state.history,null);
    const paths = []; context.releaseHistoryApi = async path => { paths.push(path); return {events:[{id:8,summary:'Recorded approval',actor:'finance',recorded_at:'2026-10-21T01:00:00Z'}],has_more:true,next_before:8}; };
    runInContext('api = releaseHistoryApi;',context); await workflow.loadHistory(); await workflow.loadHistory(true);
    assert.match(paths[1],/before=8/); assert.match(panel.innerHTML,/Recorded approval/);
    assert.equal(input.value,'UNSAVED'); assert.equal(workflow.state.caseDirty,true);
    passed++; process.stdout.write('PASS history ignores stale invoices, paginates and preserves unfinished fields\n');
  } finally { workflow.state.caseDirty = false; runInContext('api = historyOriginalApi; delete globalThis.historyOriginalApi; delete globalThis.releaseHistoryApi;',context); }
}
test('capture stays in private setup and escapes actual intake errors', () => {
  workflow.state.capture = {enabled:true,available:true,running:false,last_run_at:'2026-10-26T10:00:00Z',checked:2,replies_added:1,documents_added:1,
    failures:[{invoice_number:'INV <script>',error:'Held <evidence>'}]};
  assert.equal(workflow.captureHtml(false),'');
  const markup = workflow.captureHtml(true);
  assert.match(markup,/Stop capture/); assert.match(markup,/INV &lt;script&gt;/);
  assert.match(markup,/Held &lt;evidence&gt;/); assert.doesNotMatch(markup,/<script>/);
  workflow.state.capture = {enabled:false,available:false};
  assert.match(workflow.captureHtml(true),/data-capture-enabled="true" disabled/);
});
{
  const paths = [], prompts = [];
  workflow.state.readiness = readinessFixture();
  workflow.state.readiness.providers.together.selected = true;
  context.captureApi = async (path,options) => { paths.push([path,options]); return {enabled:options.body.enabled,available:true}; };
  runInContext('globalThis.captureOriginalApi = api; api = captureApi;',context);
  const confirm = context.window.confirm;
  try {
    context.window.confirm = message => { prompts.push(message); return false; };
    await workflow.setCapture(true); assert.equal(paths.length,0);
    assert.match(prompts[0],/TogetherAI.*charges/);
    context.window.confirm = () => true;
    await workflow.setCapture(true); assert.equal(paths[0][0],'/api/capture'); assert.equal(paths[0][1].body.enabled,true);
    await workflow.setCapture(false); assert.equal(paths[1][1].body.enabled,false);
    workflow.state.readiness.mode = 'demo';
    await assert.rejects(workflow.setCapture(true),/private workspace/);
    assert.equal(paths.length,2);
    passed++; process.stdout.write('PASS capture requires private authority and explicit cost-aware opt-in; stopping needs no new consent\n');
  } finally { context.window.confirm = confirm; runInContext('api = captureOriginalApi; delete globalThis.captureOriginalApi; delete globalThis.captureApi;',context); }
}
{
  const paths = [];
  workflow.state.runtime = {mode:'private'}; workflow.state.token = 'capture-test';
  workflow.state.caseOpen = true; workflow.state.busy = false; workflow.state.caseDirty = false;
  workflow.state.capturePolling = workflow.state.replyPolling = false;
  const invoice = reviewedInvoice(); workflow.state.invoice = invoice;
  context.capturePollApi = async path => { paths.push(path); return path === '/api/capture' ? {enabled:true,available:true} : {...invoice,version:9}; };
  runInContext('globalThis.capturePollOriginalApi = api; api = capturePollApi;',context);
  try {
    await workflow.pollWorkspace();
    assert.deepEqual(paths,['/api/capture','/api/invoices/invoice-1']); assert.equal(workflow.state.invoice.version,9);
    paths.length = 0; workflow.state.caseDirty = true;
    await workflow.pollWorkspace(); assert.equal(paths.length,0);
    workflow.state.caseDirty = false;
    let resolveInvoice, invoiceRequested;
    const requested = new Promise(resolve => { invoiceRequested = resolve; });
    context.capturePollApi = async path => {
      if (path === '/api/capture') return {enabled:true,available:true};
      invoiceRequested(); return new Promise(resolve => { resolveInvoice = resolve; });
    };
    runInContext('api = capturePollApi;',context);
    const original = workflow.state.invoice, pending = workflow.pollWorkspace();
    await requested; workflow.state.caseEdits++; workflow.state.caseDirty = true;
    resolveInvoice({...original,version:10}); await pending;
    assert.equal(workflow.state.invoice,original); assert.equal(workflow.state.caseDirty,true);
    passed++; process.stdout.write('PASS server capture refreshes local records only and preserves edits made before or during refresh\n');
  } finally { workflow.state.caseDirty = false; runInContext('api = capturePollOriginalApi; delete globalThis.capturePollOriginalApi; delete globalThis.capturePollApi;',context); }
}
{
  const content = nodes.get('#case-content'), input = document.querySelector('#review-form input'), form = node();
  form.id = 'review-form'; form.dataset.dirty = 'true';
  form.closest = selector => selector === '#case-content' ? content : null;
  const setup = (dirty = true) => {
    const invoice = {...reviewedInvoice(),version:4};
    Object.assign(workflow.state,{invoice,rows:[invoice],view:'all',total:1,caseSequence:150,caseEdits:7,caseDirty:dirty,busy:false,token:'conflict-test'});
    content.innerHTML = '<form>Retained draft</form>'; input.value = 'PO unsaved';
    nodeLists.set('form[data-dirty]',dirty ? [form] : []);
    return invoice;
  };
  const conflict = {status:409,message:'Invoice changed <unsafe>',code:'stale_version'};
  let requests = 0, resolveRefresh;
  context.conflictApi = async () => { requests++; return new Promise(resolve => { resolveRefresh = resolve; }); };
  context.conflictNotify = () => {};
  runInContext('globalThis.conflictOriginals = {api,notify}; api = conflictApi; notify = conflictNotify;',context);
  try {
    const original = setup();
    await workflow.operation(form,'#review-error',async () => { throw conflict; });
    assert.equal(requests,0); assert.equal(workflow.state.invoice,original); assert.equal(workflow.state.caseDirty,true);
    assert.equal(content.innerHTML,'<form>Retained draft</form>'); assert.equal(input.value,'PO unsaved');
    const error = document.querySelector('#review-error').innerHTML;
    assert.match(error,/data-refresh-case/); assert.match(error,/&lt;unsafe&gt;/); assert.doesNotMatch(error,/<unsafe>/);
    assert.equal(workflow.state.busy,false);
    passed++; process.stdout.write('PASS stale saves preserve unfinished fields and offer an explicit safe refresh\n');
    const unedited = setup(false);
    const pending = workflow.operation(form,'#review-error',async () => { throw conflict; });
    await new Promise(setImmediate);
    assert.equal(requests,1);
    workflow.state.caseDirty = true; workflow.state.caseEdits++; input.value = 'Edit begun during recovery';
    resolveRefresh({...unedited,version:5}); await pending;
    assert.equal(workflow.state.invoice,unedited); assert.equal(input.value,'Edit begun during recovery');
    assert.equal(workflow.state.caseDirty,true); assert.equal(content.innerHTML,'<form>Retained draft</form>');
    passed++; process.stdout.write('PASS edits begun during conflict recovery are not overwritten by its delayed response\n');
    const clean = setup(false);
    const cleanRecovery = workflow.operation(form,'#review-error',async () => { throw conflict; });
    await new Promise(setImmediate); resolveRefresh({...clean,version:5}); await cleanRecovery;
    assert.equal(workflow.state.invoice.version,5); assert.equal(workflow.state.caseDirty,false);
    const before = requests, entry = node(); entry.id = 'entry-form';
    await workflow.operation(entry,'#entry-error',async () => { throw conflict; });
    assert.equal(requests,before,'A duplicate new invoice must not refresh an unrelated active case');
    passed++; process.stdout.write('PASS clean conflicts refresh safely; entry conflicts never replace an unrelated case\n');
    setup(false);
    const oldRecovery = workflow.operation(form,'#review-error',async () => { throw conflict; });
    await new Promise(setImmediate);
    const other = {...reviewedInvoice(),id:'another-case',version:2};
    workflow.state.invoice = other; workflow.state.caseSequence++; content.innerHTML = 'Another case';
    resolveRefresh({...clean,version:6}); await oldRecovery;
    assert.equal(workflow.state.invoice,other); assert.equal(content.innerHTML,'Another case');
    passed++; process.stdout.write('PASS obsolete conflict recovery cannot replace a different active case\n');
  } finally {
    workflow.state.caseDirty = false; nodeLists.delete('form[data-dirty]');
    runInContext('({api,notify} = conflictOriginals); delete globalThis.conflictOriginals; delete globalThis.conflictApi; delete globalThis.conflictNotify;',context);
  }
}
test('held source messages remain actionable without claiming a captured customer reply',()=>{
  const invoice = fixture(); invoice.status='missing_po'; invoice.resolution=null;
  invoice.integration.messages=[{direction:'incoming',status:'held',recorded_at:'2026-10-27T10:00:00Z',error:'Retry <source> from the request thread.'}];
  assert.equal(workflow.nextAction(invoice).title,'Message needs review');
  assert.equal(workflow.nextAction(invoice).kind,'replies');
  assert.equal(workflow.journeySteps(invoice)[1].caption,'Message needs review');
  const html=workflow.mailHtml(invoice,'replies');
  assert.match(html,/Message needs review/); assert.match(html,/Oct 27, 2026/); assert.match(html,/Retry &lt;source&gt;/);
  assert.doesNotMatch(html,/Customer reply|Sent through Gmail/); assert.equal(workflow.canApprove(invoice),false);
});
test('investigation requires actual configuration and has no fabricated findings or tool trace',()=>{
  const invoice = {...reviewedInvoice(),version:4,investigation:{available:false,reason:'Enable TogetherAI <backend>',latest:null}};
  const html=workflow.investigationHtml(invoice);
  assert.equal(html,'');
  assert.doesNotMatch(html,/data-investigate(?:\s|>)|Tool activity|investigation-summary|data-investigation-next/);
});
const investigationRun = (changes={})=>({id:'investigation-1',invoice_id:'invoice-1',source_version:4,status:'completed',model:'Together model',summary:'The reference differs.',next_step:'propose_reference',draft:'Please confirm the revised PO.',citations:[{source_id:'document:doc-1:page:1',label:'Purchase order.txt',document_id:'doc-1',page:1,excerpt:'PO number: PO NEW'}],trace:[{tool:'read_document_page',status:'ok'},{tool:'read_checks',status:'ok'},{tool:'finish_investigation',status:'ok'}],error:null,stale:false,...changes});
test('findings, evidence, model and actual tool activity are escaped and remain proposals',()=>{
  const hostile='<script>unsafe</script>', run=investigationRun({summary:hostile,draft:hostile,model:hostile,citations:[{label:hostile,excerpt:hostile,document_id:'doc-1',page:1}],trace:[{tool:hostile,status:hostile,hidden:'MUST NOT SHOW'}]});
  const invoice={...reviewedInvoice(),version:4,investigation:{available:true,latest:run}};
  const html=workflow.investigationHtml(invoice);
  assert.match(html,/&lt;script&gt;unsafe&lt;\/script&gt;/); assert.doesNotMatch(html,/<script>|MUST NOT SHOW/);
  assert.match(html,/data-investigation-source="0"/); assert.match(html,/data-copy-investigation/);
  assert.match(html,/data-investigation-next/); assert.doesNotMatch(html,/data-next="approve"|data-send-request|data-apply-reference/);
});
test('changed source versions and abstention disable suggested actions without hiding evidence',()=>{
  for(const change of [{source_version:3},{stale:true},{source_version:null},{status:'abstained'}]){
    const invoice={...reviewedInvoice(),version:4,investigation:{available:true,latest:investigationRun(change)}};
    const html=workflow.investigationHtml(invoice);
    assert.doesNotMatch(html,/data-investigation-next/);
    if(change.status!=='abstained') {assert.match(html,/Records changed/);assert.match(html,/data-copy-investigation disabled/);}
    else {assert.match(html,/More evidence needed/);assert.doesNotMatch(html,/data-copy-investigation/);}
    assert.match(html,/PO number: PO NEW/);
  }
});
{
  const originalConfirm=context.window.confirm, content=nodes.get('#case-content'), section=document.querySelector('#case-investigation');
  let serial=0,consents=0,paths=[];
  context.crypto={randomUUID:()=>`investigation-key-${++serial}`};
  context.window.confirm=()=>{consents++;return true;};
  const setup=()=>{
    const invoice={...reviewedInvoice(),version:4,investigation:{available:true,reason:'',latest:null}};
    Object.assign(workflow.state,{invoice,caseOpen:true,caseSequence:200,caseEdits:9,caseDirty:true,busy:false,token:'investigation-session'});
    workflow.state.investigationPending.clear();workflow.state.investigationErrors.clear();workflow.state.operationKeys.clear();
    content.innerHTML='<form>Unfinished review</form>';paths=[];consents=0;
    return invoice;
  };
  runInContext('globalThis.investigationOriginalApi=api;',context);
  try{
    let resolveRun;
    const invoice=setup(), before=JSON.stringify(invoice);
    context.investigationApi=async(path,options)=>{paths.push({path,options});return new Promise(resolve=>{resolveRun=resolve;});};
    runInContext('api=investigationApi;',context);
    const pending=workflow.investigateCase();
    await workflow.investigateCase();
    assert.equal(paths.length,1);assert.equal(consents,1);assert.match(section.innerHTML,/Investigating…/);
    assert.match(section.innerHTML,/<p class="sr-only">Investigating invoice\.<\/p>/);
    assert.doesNotMatch(section.innerHTML,/Checking saved records|Investigation in progress/);
    workflow.state.caseEdits++;document.querySelector('#review-form input').value='Unsaved correction';
    resolveRun(investigationRun());await pending;
    assert.equal(content.innerHTML,'<form>Unfinished review</form>');assert.equal(workflow.state.caseDirty,true);
    assert.equal(document.querySelector('#review-form input').value,'Unsaved correction');assert.equal(workflow.state.caseEdits,10);
    assert.equal(invoice.amount_cents,JSON.parse(before).amount_cents);assert.equal(invoice.payment_status,'unpaid');assert.equal(invoice.version,4);
    assert.equal(invoice.investigation.latest.id,'investigation-1');assert.equal(paths[0].options.body.expected_version,4);
    assert.ok(paths[0].options.body.idempotency_key);assert.equal(workflow.state.operationKeys.size,0);
    passed++;process.stdout.write('PASS investigation preserves dirty form DOM and authoritative fields, with one consent and one concurrent request\n');
    const firstKey=paths[0].options.body.idempotency_key;
    context.investigationApi=async(path,options)=>{paths.push({path,options});return investigationRun({id:'investigation-2'});};
    runInContext('api=investigationApi;',context);await workflow.investigateCase();
    assert.equal(consents,2);assert.notEqual(paths[1].options.body.idempotency_key,firstKey);
    passed++;process.stdout.write('PASS a deliberate new investigation requests consent and a fresh idempotency key\n');
    setup();context.window.confirm=()=>false;await workflow.investigateCase();assert.equal(paths.length,0);
    context.window.confirm=()=>{consents++;return true;};
    passed++;process.stdout.write('PASS declining TogetherAI data consent performs no provider request\n');
    setup();let resolveObsolete;
    context.investigationApi=async()=>new Promise(resolve=>{resolveObsolete=resolve;});runInContext('api=investigationApi;',context);
    const obsolete=workflow.investigateCase();
    const other={...reviewedInvoice(),id:'another-invoice',version:2,investigation:{available:true,latest:null}};
    workflow.state.invoice=other;workflow.state.caseSequence++;content.innerHTML='Another case';section.innerHTML='Another investigation';
    resolveObsolete(investigationRun());await obsolete;
    assert.equal(workflow.state.invoice,other);assert.equal(other.investigation.latest,null);assert.equal(section.innerHTML,'Another investigation');
    passed++;process.stdout.write('PASS obsolete investigation responses cannot replace another case or its findings\n');
    const changed=setup();let resolveChanged;
    context.investigationApi=async()=>new Promise(resolve=>{resolveChanged=resolve;});runInContext('api=investigationApi;',context);
    const changing=workflow.investigateCase();changed.version=5;resolveChanged(investigationRun());await changing;
    assert.match(section.innerHTML,/Records changed/);assert.doesNotMatch(section.innerHTML,/data-investigation-next/);
    passed++;process.stdout.write('PASS records changed during investigation retain the finding but disable stale next steps\n');
    setup();let reads=0,posts=0;
    context.investigationApi=async(path,options)=>{
      paths.push({path,options});
      if(options?.method==='POST'){posts++;throw {status:0,message:'Connection lost'};}
      reads++;return {available:true,latest:investigationRun({status:reads===1?'running':'completed'})};
    };runInContext('api=investigationApi;',context);
    await workflow.investigateCase();assert.equal(posts,1);assert.equal(reads,1);assert.match(section.innerHTML,/Check result/);
    await workflow.investigateCase();assert.equal(posts,1);assert.equal(reads,2);assert.equal(consents,1);
    assert.equal(workflow.state.invoice.investigation.latest.status,'completed');assert.equal(workflow.state.operationKeys.size,0);
    passed++;process.stdout.write('PASS an unknown request recovers its stored running result without dispatching or charging again\n');
    setup();let postAttempts=0;
    context.investigationApi=async(path,options)=>{
      paths.push({path,options});
      if(options?.method==='POST'){postAttempts++;if(postAttempts===1)throw {status:0,message:'Request timed out'};return investigationRun();}
      return {available:true,latest:null};
    };runInContext('api=investigationApi;',context);
    await workflow.investigateCase();await workflow.investigateCase();
    const postPaths=paths.filter(item=>item.options?.method==='POST');
    assert.equal(postPaths.length,2);assert.equal(postPaths[0].options.body.idempotency_key,postPaths[1].options.body.idempotency_key);
    assert.equal(paths[2].path,'/api/invoices/invoice-1/investigation');assert.equal(paths[2].options,undefined);
    passed++;process.stdout.write('PASS unknown retries read stored state first and retain the exact original idempotency key\n');
    setup();context.investigationApi=async()=>{throw {status:409,message:'Records changed <unsafe>'};};runInContext('api=investigationApi;',context);
    await workflow.investigateCase();assert.equal(content.innerHTML,'<form>Unfinished review</form>');assert.equal(workflow.state.caseDirty,true);
    assert.match(section.innerHTML,/Records changed &lt;unsafe&gt;/);assert.equal(workflow.state.investigationPending.size,0);
    passed++;process.stdout.write('PASS investigation failures keep drafts and provide an escaped local error with restored controls\n');
    setup();const sourceInvoice=workflow.state.invoice;sourceInvoice.investigation.latest=investigationRun();workflow.state.docId='doc-1';
    workflow.showInvestigationSource(0);assert.equal(content.innerHTML,'<form>Unfinished review</form>');assert.equal(workflow.state.caseDirty,true);
    assert.match(document.querySelector('#document-preview .document-text').innerHTML,/source-highlight/);
    sourceInvoice.investigation.latest.citations[0].excerpt='Not in the retained original';
    const sourceBefore=document.querySelector('#document-preview .document-text').innerHTML;
    workflow.showInvestigationSource(0);assert.equal(document.querySelector('#document-preview .document-text').innerHTML,sourceBefore);
    passed++;process.stdout.write('PASS investigation source links highlight only retained passages without replacing current document edits\n');
  }finally{
    context.window.confirm=originalConfirm;workflow.state.investigationPending.clear();workflow.state.investigationErrors.clear();workflow.state.caseDirty=false;
    runInContext('api=investigationOriginalApi;delete globalThis.investigationOriginalApi;delete globalThis.investigationApi;',context);
  }
}
test('a recorded reply advances to attaching its PO without asking for the same request again',()=>{
  const invoice={...fixture(),status:'missing_po',resolution:null,documents:[],replies:[{id:'manual-reply',source:'manual',text:'The PO is attached.',received_at:'2026-08-20T10:00:00Z'}],integration:{accounting:null,messages:[]}};
  const next=workflow.nextAction(invoice);
  assert.equal(next.kind,'received');assert.equal(next.button,'Attach PO');
  const markup=render(invoice);
  assert.equal(count(markup,/data-next="received"/g),1);
  assert.equal(count(markup,/data-next="upload"/g),0);
  assert.doesNotMatch(markup,/data-next="request"|data-next="send"|data-next="replies"/);
  assert.match(markup,/<summary>Request draft<\/summary>/);
  invoice.documents=[poDocument('rules',false)];invoice.status='needs_review';
  assert.equal(workflow.nextAction(invoice).kind,'review');
});
{
  let uploads=0,requests=0;
  const file=document.querySelector('#document-file'), previousClick=file.click;
  file.click=()=>{uploads++;};
  const button=node();button.dataset.next='received';button.hasAttribute=()=>false;
  context.receivedHooks={api:async()=>{requests++;throw Error('Unexpected provider request');},focusSection:()=>{}};
  runInContext('globalThis.receivedOriginals={api,focusSection};({api,focusSection}=receivedHooks);',context);
  try{
    await nodes.get('#case-content').listeners.get('click')({target:{closest:()=>button}});
    assert.equal(uploads,1);assert.equal(requests,0);
    passed++;process.stdout.write('PASS attaching a recorded reply document never dispatches Gmail sync or another request\n');
  }finally{
    file.click=previousClick;
    runInContext('({api,focusSection}=receivedOriginals);delete globalThis.receivedOriginals;delete globalThis.receivedHooks;',context);
  }
}
{
  const button=node();button.hasAttribute=name=>name==='data-source-expand';
  const form=nodes.get('#review-form') || node(), retained=form.innerHTML;
  workflow.state.caseDirty=true;workflow.state.sourceExpanded=false;
  await nodes.get('#case-content').listeners.get('click')({target:{closest:()=>button}});
  assert.equal(workflow.state.sourceExpanded,true);assert.equal(button.getAttribute('aria-expanded'),'true');
  assert.equal(workflow.state.caseDirty,true);assert.equal(form.innerHTML,retained);
  await nodes.get('#case-content').listeners.get('click')({target:{closest:()=>button}});
  assert.equal(workflow.state.sourceExpanded,false);assert.equal(button.getAttribute('aria-expanded'),'false');
  workflow.state.caseDirty=false;
  passed++;process.stdout.write('PASS source expansion changes retained-node presentation without discarding edits\n');
}
{
  const input=nodes.get('#document-file'), form=node(), oldFile={name:'existing.txt',size:40};
  input.files=[oldFile];workflow.state.caseDirty=false;workflow.state.busy=false;
  const edits=workflow.state.caseEdits;
  interactionHooks.onFiles([{name:'bad.exe',size:100}],form,true);
  assert.equal(input.files[0],oldFile);assert.equal(workflow.state.caseEdits,edits);assert.equal(workflow.state.caseDirty,false);
  context.DataTransfer=class {constructor(){this.files=[];this.items={add:file=>this.files.push(file)};}};
  const valid={name:'purchase-order.pdf',size:100};
  interactionHooks.onFiles([valid],form,true);
  assert.equal(input.files[0],valid);assert.equal(form.dataset.dirty,'true');assert.equal(workflow.state.caseDirty,true);assert.equal(workflow.state.caseEdits,edits+1);
  workflow.state.busy=true;interactionHooks.onFiles([oldFile],form,true);assert.equal(input.files[0],valid);
  workflow.state.busy=false;workflow.state.caseDirty=false;
  passed++;process.stdout.write('PASS valid drops mark the selected file dirty while invalid and busy drops retain the current selection\n');
}
{
  const originalConfirm=context.window.confirm, search=nodes.get('#queue-search');
  workflow.state.caseOpen=true;workflow.state.caseDirty=true;context.window.confirm=()=>false;
  document.activeElement=null;await interactionHooks.onFind();
  assert.equal(workflow.state.caseOpen,true);assert.equal(workflow.state.caseDirty,true);assert.notEqual(document.activeElement,search);
  workflow.state.caseOpen=false;workflow.state.caseDirty=false;search.select=()=>{};
  await interactionHooks.onFind();assert.equal(document.activeElement,search);
  context.window.confirm=originalConfirm;
  passed++;process.stdout.write('PASS keyboard find preserves a declined draft and focuses search only after safe navigation\n');
}
{
  const button=node();button.textContent='Save';nodeLists.set('button[type="submit"]',[button]);
  workflow.state.busy=false;workflow.state.invoice=null;workflow.state.caseDirty=false;
  try {
    for (const [id,label] of [['review-form','Saving review…'],['proposal-form','Saving correction…'],['reply-form','Saving reply…'],['entry-form','Adding invoice…'],['import-form','Importing invoices…'],['confirm-form','Approving handoff…']]) {
      const form=node();form.id=id;workflow.state.confirmation={kind:'approve_resolution'};
      for (const failure of [false,true]) {
        await workflow.operation(form,'#upload-error',async()=>{
          assert.equal(button.textContent,label);assert.equal(button.disabled,true);assert.equal(form.getAttribute('aria-busy'),'true');
          if(failure) throw Error('Action failed');
        });
        assert.equal(button.textContent,'Save');assert.equal(button.disabled,false);assert.equal(form.getAttribute('aria-busy'),undefined);
      }
    }
    passed++;process.stdout.write('PASS action-specific pending labels restore after successful and failed requests\n');
  } finally {nodeLists.delete('button[type="submit"]');workflow.state.confirmation=null;}
}
{
  const mark=document.querySelector('#document-preview .source-highlight');
  const previousScroll=mark.scrollIntoView, previousSpotlight=ui.spotlight;let scrolls=0,emphases=0;
  mark.scrollIntoView=()=>{scrolls++;};ui.spotlight=element=>{assert.equal(element,mark);emphases++;};
  const doc=poDocument();
  workflow.showDocumentPage(doc,0,'PO number: PO NEW');
  assert.equal(scrolls,1);assert.equal(emphases,1);
  workflow.showDocumentPage(doc,0);
  assert.equal(scrolls,1);assert.equal(emphases,1);
  mark.scrollIntoView=previousScroll;ui.spotlight=previousSpotlight;
  passed++;process.stdout.write('PASS explicit source jumps reveal and emphasize the passage without pulsing ordinary page changes\n');
}
test('handoff artwork is decorative and appears only with supported handoff evidence',()=>{
  const invoice=reviewedInvoice();
  let markup=render(invoice);
  assert.match(markup,/<img class="handoff-art" src="\/art\/handoff-folio\.svg"[^>]*alt=""[^>]*aria-hidden="true"/);
  assert.match(markup,/<section class="handoff-sheet" data-approved="false">/);
  invoice.status='prepared';invoice.resolution={document_id:'doc-1',po_reference:'PO NEW'};
  markup=render(invoice);assert.match(markup,/<section class="handoff-sheet" data-approved="true">/);
  invoice.status='missing_po';invoice.resolution=null;invoice.documents=[];invoice.checks=[];
  assert.doesNotMatch(render(invoice),/<img class="handoff-art"/);
});
test('physical folio details remain decorative and do not add looping or pointer motion',()=>{
  assert.match(html,/class="folio-seam"/);assert.match(html,/class="folio-fastener"/);
  assert.match(folioCss,/\/art\/paper-grain\.svg/);
  assert.doesNotMatch(folioCss,/infinite|pointermove|mousemove/);
  assert.match(source,/confirmation\.kind === "approve_resolution" && state\.invoice\?\.status === "prepared"\) window\.InvoiceClearUI\?\.filed/);
});
test('saved investigation findings remain inspectable when new runs are unavailable',()=>{
  const invoice={...reviewedInvoice(),version:4,investigation:{available:false,reason:'Backend configuration unavailable',latest:investigationRun()}};
  const markup=workflow.investigationHtml(invoice);
  assert.match(markup,/The reference differs\./);assert.match(markup,/data-investigation-source/);
  assert.doesNotMatch(markup,/Configure|data-investigate(?:\s|>)|Backend configuration unavailable/);
  workflow.state.invoice=invoice;workflow.renderCase();
  const tag=nodes.get('#case-content').innerHTML.match(/<section id="case-investigation"[^>]*>/)?.[0];
  assert.ok(tag);assert.doesNotMatch(tag,/\shidden(?:\s|>)/);
});
test('only enabled investigations show a new-run action and disabled empty sections have no visual strip',()=>{
  const invoice={...reviewedInvoice(),version:4,investigation:{available:true,latest:null}};
  let markup=render(invoice);
  assert.match(markup,/data-investigate/);assert.doesNotMatch(markup.match(/<section id="case-investigation"[^>]*>/)[0],/\shidden(?:\s|>)/);
  invoice.investigation.available=false;
  markup=render(invoice);
  assert.match(markup.match(/<section id="case-investigation"[^>]*>/)[0],/\shidden(?:\s|>)/);
  assert.doesNotMatch(markup,/Configure TogetherAI|data-investigate(?:\s|>)/);
});
{
  const notes=[node(),node()], previousRuntime=workflow.state.runtime;
  const previousStorage=context.sessionStorage;
  context.sessionStorage={getItem:()=>null,removeItem(){}};
  nodeLists.set('.public-write-note',notes);
  runInContext('globalThis.customerOriginalApi=api;',context);
  try {
    for(const mode of ['demo','private']) {
      context.customerApi=async()=>({mode,demo_credentials:{email:'finance@example.example',password:'fixture-password'}});
      runInContext('api=customerApi;',context);
      await runInContext('runtime()',context);
      assert.equal(nodes.get('#connections-open').hidden,mode!=='private');
      assert.ok(notes.every(note=>note.hidden===(mode!=='demo')));
    }
    assert.doesNotMatch(html.slice(html.indexOf('id="access-view"'),html.indexOf('id="app-shell"')), /public-write-note|Public access|Uploads are shared/);
    assert.match(source,/async function openConnections\(\) \{\s*if \(state\.runtime\?\.mode !== "private"\) return;/);
    passed++;process.stdout.write('PASS public customer view hides connection setup and shows privacy only at record entry\n');
  } finally {
    workflow.state.runtime=previousRuntime;context.sessionStorage=previousStorage;nodeLists.delete('.public-write-note');
    runInContext('api=customerOriginalApi;delete globalThis.customerOriginalApi;delete globalThis.customerApi;',context);
  }
}
process.stdout.write(`${passed} workflow regression groups passed (VM and static source checks).\n`);
