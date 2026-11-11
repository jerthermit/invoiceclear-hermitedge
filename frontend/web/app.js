"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const base = (window.__INVOICECLEAR_CONFIG__?.apiBaseUrl || "").replace(/\/$/, "");
const SESSION_KEY = "invoiceclear.session";
const labels = {missing_po:"Missing purchase order",needs_review:"Review document",mismatch:"PO discrepancy",ready:"Ready for handoff",prepared:"Handoff approved"};
const state = {runtime:null,token:null,view:"open",q:"",rows:[],total:0,hasMore:false,queueSequence:0,caseSequence:0,caseEdits:0,invoice:null,selectedId:null,caseTab:"review",docId:null,page:0,queueScroll:0,busy:false,operationKeys:new Map(),confirmation:null,connections:null,readiness:null,capture:null,capturePolling:false,history:null,syncOffset:0,caseDirty:false,replyPolling:false,replyPollError:"",caseOpen:false,nextId:null,navigationBusy:false};
state.investigationPending = new Map();
state.investigationErrors = new Map();
Object.assign(state,{queueOffset:0,queueLimit:10,queueLoading:false,queueContext:null,queueNeedsRefill:false,queueRemovedIndex:null,queueListScroll:0,sourceExpanded:false});
const uid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const valueOf = field => field && typeof field === "object" ? (field.value ?? "") : (field ?? "");
const reference = value => String(value ?? "").toUpperCase().trim().replace(/\s+/g, " ");
const displayValue = value => value == null || value === "" ? "—" : typeof value === "object" ? (value.message || value.label || value.value || "—") : String(value);
const blockerText = invoice => displayValue(invoice.blocker) === "—" ? "" : displayValue(invoice.blocker);
const issueLabel = invoice => invoice.work?.label || (invoice.status === "mismatch" ? (blockerText(invoice) || labels.mismatch) : (labels[invoice.status] || invoice.status));
const date = value => {
  if (!value) return "—";
  const parsed = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("en", {month:"short",day:"numeric",year:"numeric"});
};
const money = invoice => {
  try { return new Intl.NumberFormat("en", {style:"currency",currency:invoice.currency}).format(invoice.amount_cents / 100); }
  catch { return `${invoice.currency} ${(invoice.amount_cents / 100).toFixed(2)}`; }
};

class ApiError extends Error {
  constructor(message, status = 0, code = "") { super(message); this.status = status; this.code = code; }
}
async function api(path, options = {}) {
  const {method = "GET", body, publicRoute = false, blob = false} = options;
  if (!publicRoute && !state.token) throw new ApiError("Sign in to continue.", 401);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const headers = {Accept:blob ? "*/*" : "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (!publicRoute) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(base + path, {method,headers,body:body === undefined ? undefined : JSON.stringify(body),signal:controller.signal,cache:"no-store"});
    if (!response.ok) {
      const result = await response.json().catch(() => null);
      const fallback = response.status === 403 ? "You do not have permission to perform this action." : `Request failed (${response.status}). Try again.`;
      if (response.status === 401 && !publicRoute) showLogin("Your session has ended. Sign in to continue.");
      throw new ApiError(result?.error?.message || fallback, response.status, result?.error?.code);
    }
    return blob ? response.blob() : response.status === 204 ? null : response.json();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(error.name === "AbortError" ? "The request timed out. Try again; a repeated action will not create a duplicate." : "Could not reach the workspace. Check your connection and try again.");
  } finally { clearTimeout(timeout); }
}
function setError(id, message = "") { const el = $(id); if (el) { el.textContent = message; el.hidden = !message; } }
function notify(message, error = false) {
  $("#notice-text").textContent = message;
  $("#notice").classList.toggle("error", error);
  $("#notice").hidden = false;
  window.InvoiceClearUI?.showNotice(error);
}
function showLogin(message = "") {
  window.InvoiceClearUI?.dismissNotice();
  state.token = null;
  state.capture = state.connections = state.readiness = null;
  state.invoice = null;
  state.investigationErrors.clear();
  state.caseSequence++;
  state.queueSequence++;
  sessionStorage.removeItem(SESSION_KEY);
  $$('dialog[open]').forEach(dialog => dialog.close());
  $("#app-shell").hidden = true;
  $("#access-view").hidden = false;
  document.body.classList.remove("session-ready");
  if (state.runtime?.mode === "private") $("#access-password").value = "";
  $("#access-password").type = "password";
  $("#password-toggle").textContent = "Show";
  $("#password-toggle").setAttribute("aria-label", "Show password");
  setError("#access-error", message);
  $("#access-submit").disabled = !state.runtime;
  if (message) $("#access-email").focus();
}
async function runtime() {
  $("#runtime-retry").hidden = true;
  $("#access-submit").disabled = true;
  setError("#access-error");
  try {
    state.runtime = await api("/api/runtime", {publicRoute:true});
    $("#connections-open").hidden = state.runtime.mode !== "private";
    $$(".public-write-note").forEach(note => { note.hidden = state.runtime.mode !== "demo"; });
    if (state.runtime.mode === "demo" && state.runtime.demo_credentials) {
      $("#access-email").value = state.runtime.demo_credentials.email;
      $("#access-password").value = state.runtime.demo_credentials.password;
    }
    $("#access-submit").disabled = false;
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch { sessionStorage.removeItem(SESSION_KEY); }
    if (saved?.access_token) { state.token = saved.access_token; enterWorkspace(); }
  } catch (error) {
    setError("#access-error", error.message);
    $("#runtime-retry").hidden = false;
  }
}
function enterWorkspace() {
  $("#access-view").hidden = true;
  $("#app-shell").hidden = false;
  $("#notice").hidden = true;
  document.body.classList.add("session-ready");
  state.rows = [];
  state.invoice = null;
  state.selectedId = null;
  state.caseOpen = false;
  state.nextId = null;
  state.queueOffset = 0;
  state.queueContext = null;
  state.queueNeedsRefill = false;
  state.queueRemovedIndex = null;
  state.queueListScroll = 0;
  $("#workspace-main").classList.remove("case-open");
  $("#case-view").hidden = false;
  $("#queue-view").hidden = false;
  renderPlaceholder();
  loadQueue();
  if (state.runtime?.mode === "private") loadConnections().catch(error => { if (error.status !== 401) notify(error.message,true); });
  $("#workspace-main").focus({preventScroll:true});
}
$("#access-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#access-submit");
  if (button.disabled) return;
  button.disabled = true;
  setError("#access-error");
  try {
    const session = await api("/api/session", {method:"POST",publicRoute:true,body:{email:$("#access-email").value.trim(),password:$("#access-password").value}});
    state.token = session.access_token;
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    if (state.runtime?.mode === "private") $("#access-password").value = "";
    enterWorkspace();
  } catch (error) { setError("#access-error", error.message); }
  finally { button.disabled = false; }
});
$("#runtime-retry").addEventListener("click", runtime);
$("#password-toggle").addEventListener("click", event => {
  const input = $("#access-password"), show = input.type === "password";
  input.type = show ? "text" : "password";
  event.currentTarget.textContent = show ? "Hide" : "Show";
  event.currentTarget.setAttribute("aria-label", show ? "Hide password" : "Show password");
});
$("#logout").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await api("/api/session/logout", {method:"POST"}); showLogin(); }
  catch (error) { if (error.status !== 401) notify(error.message, true); }
  finally { button.disabled = false; }
});
$("#notice-dismiss").addEventListener("click", () => { $("#notice").hidden = true; window.InvoiceClearUI?.dismissNotice(); });

function renderQueue() {
  $("#queue-list").setAttribute("aria-busy", "false");
  if (!state.rows.length) {
    const heading = state.q ? "No matching invoices." : state.view === "prepared" ? "Nothing prepared yet." : "No invoices here yet.";
    const note = state.q ? "Try a different invoice number or customer." : state.view === "prepared" ? "Prepared resolutions will appear here." : state.runtime?.mode === "private" ? "Connect QuickBooks or add an invoice." : "Add an invoice or import your CSV to begin.";
    $("#queue-list").innerHTML = `<div class="empty-state"><h3>${heading}</h3><p>${note}</p>${state.q ? '<button class="text-button" data-clear-search type="button">Clear search</button>' : ''}</div>`;
  } else {
    $("#queue-list").innerHTML = state.rows.map(invoice => `<button class="invoice-row" type="button" data-invoice="${esc(invoice.id)}" ${String(state.selectedId) === String(invoice.id) ? 'aria-current="true"' : ''} aria-label="Review invoice ${esc(invoice.invoice_number)} from ${esc(invoice.customer)}"><span class="row-id"><strong>${esc(invoice.invoice_number)}</strong></span><span class="row-customer">${esc(invoice.customer)}</span><span class="row-status">${esc(issueLabel(invoice))}</span><span class="row-due">${esc(date(invoice.due_date))}</span><span class="row-amount"><span>${esc(money(invoice))}</span><svg class="row-open" viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true"><path d="M4 10H16M11 5L16 10L11 15" stroke="currentColor" stroke-width="1.5"/></svg></span></button>`).join("");
  }
  $("#queue-count").textContent = state.total ? `${state.queueOffset + 1}–${state.queueOffset + state.rows.length} of ${state.total.toLocaleString("en")}` : "0 invoices";
  syncQueueControls();
  updateCaseNavigation();
}
function syncQueueControls() {
  const pages = Math.max(1,Math.ceil(state.total / state.queueLimit));
  $("#queue-previous").disabled = state.queueLoading || state.queueOffset === 0;
  $("#queue-next").disabled = state.queueLoading || !state.hasMore;
  $("#queue-size").disabled = state.queueLoading;
  $("#queue-page").disabled = state.queueLoading || pages === 1;
  $("#queue-page").max = String(pages);
  if (!state.queueLoading) {
    $("#queue-size").value = String(state.queueLimit);
    if (document.activeElement !== $("#queue-page")) $("#queue-page").value = String(Math.floor(state.queueOffset / state.queueLimit) + 1);
  }
  $("#queue-page-total").textContent = `of ${pages.toLocaleString("en")}`;
}
async function loadQueue({offset = state.queueOffset, limit = state.queueLimit, quiet = false, resetScroll = false, guard = null} = {}) {
  if (quiet && (state.queueLoading || document.activeElement?.closest?.("[data-invoice], #queue-pagination"))) return false;
  if (![10,25,50].includes(limit) || !Number.isSafeInteger(offset) || offset < 0) return false;
  const sequence = ++state.queueSequence;
  const token = state.token, view = state.view, q = state.q, context = JSON.stringify([view,q]);
  const focused = document.activeElement?.closest?.("#queue-pagination") ? document.activeElement : null;
  const sameContext = state.queueContext === context;
  const current = () => sequence === state.queueSequence && token === state.token && view === state.view && q === state.q && (!guard || guard());
  state.queueLoading = true;
  $("#queue-list").setAttribute("aria-busy", "true");
  if (!quiet) syncQueueControls();
  if (!sameContext && !quiet) { $("#queue-list").innerHTML = '<div class="loading-lines" aria-label="Loading invoices"><span></span><span></span><span></span></div>'; $("#queue-count").textContent = "Loading…"; }
  try {
    let result;
    for (let attempt = 0; attempt < 3; attempt++) {
      result = await api(`/api/invoices?${new URLSearchParams({view,q,offset:String(offset),limit:String(limit)})}`);
      if (!current()) return false;
      const last = Math.max(0,Math.floor((result.total - 1) / limit) * limit);
      if (offset <= last) break;
      if (attempt === 2) throw new ApiError("Invoices changed while loading. Try again.");
      offset = last;
    }
    if (quiet && document.activeElement?.closest?.("[data-invoice], #queue-pagination")) return false;
    const scroll = $("#queue-scroll"), scrollTop = scroll.scrollTop;
    const changedPage = offset !== state.queueOffset || limit !== state.queueLimit || !sameContext;
    state.rows = result.invoices;
    state.total = result.total;
    state.hasMore = result.has_more;
    state.queueOffset = offset;
    state.queueLimit = limit;
    state.queueContext = context;
    state.queueNeedsRefill = false;
    if (changedPage) { state.nextId = null; state.queueRemovedIndex = null; }
    state.queueLoading = false;
    if (!quiet) $("#queue-page").value = String(Math.floor(offset / limit) + 1);
    renderQueue();
    scroll.scrollTop = resetScroll || changedPage ? 0 : scrollTop;
    state.queueListScroll = scroll.scrollTop;
    return true;
  } catch (error) {
    if (!current() || error.status === 401) return false;
    if (!quiet && sameContext && state.rows.length) notify(error.message,true);
    else if (!quiet) {
      state.rows = []; state.total = 0; state.hasMore = false;
      $("#queue-count").textContent = "";
      $("#queue-list").innerHTML = `<div class="empty-state"><h3>Invoices could not load.</h3><p>${esc(error.message)}</p><button type="button" class="text-button" data-retry-queue>Try again</button></div>`;
    }
    return false;
  } finally {
    if (sequence === state.queueSequence) {
      state.queueLoading = false;
      $("#queue-list").setAttribute("aria-busy","false");
      syncQueueControls();
      if (!quiet && !state.caseOpen && focused && (document.activeElement === focused || document.activeElement === document.body)) (focused.disabled ? $("#queue-scroll") : focused).focus({preventScroll:true});
    }
  }
}
$$('[data-view]').forEach(button => button.addEventListener("click", () => {
  state.view = button.dataset.view;
  $$('[data-view]').forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  loadQueue({offset:0,resetScroll:true});
}));
let searchTimer;
$("#queue-search").addEventListener("input", event => {
  clearTimeout(searchTimer);
  const query = event.target.value;
  searchTimer = setTimeout(() => { state.q = query.trim(); loadQueue({offset:0,resetScroll:true}); }, 250);
});
$("#queue-previous").addEventListener("click", () => { if (!state.queueLoading) loadQueue({offset:Math.max(0,state.queueOffset - state.queueLimit),resetScroll:true}); });
$("#queue-next").addEventListener("click", () => { if (!state.queueLoading && state.hasMore) loadQueue({offset:state.queueOffset + state.queueLimit,resetScroll:true}); });
$("#queue-size").addEventListener("change", event => { if (!state.queueLoading) loadQueue({offset:0,limit:Number(event.target.value),resetScroll:true}); });
function chooseQueuePage(event) {
  event.preventDefault();
  if (state.queueLoading) return;
  const page = Number($("#queue-page").value), pages = Math.max(1,Math.ceil(state.total / state.queueLimit));
  if (!Number.isSafeInteger(page) || page < 1 || page > pages) { notify(`Choose a page between 1 and ${pages}.`,true); return; }
  loadQueue({offset:(page - 1) * state.queueLimit,resetScroll:true});
}
$("#queue-page-form").addEventListener("submit", chooseQueuePage);
$("#queue-page").addEventListener("change", chooseQueuePage);
$("#queue-list").addEventListener("click", event => {
  if (state.queueLoading) return;
  const row = event.target.closest("[data-invoice]");
  if (row) openInvoice(row.dataset.invoice);
  if (event.target.closest("[data-retry-queue]")) loadQueue();
  if (event.target.closest("[data-clear-search]")) { $("#queue-search").value = ""; state.q = ""; loadQueue({offset:0,resetScroll:true}); }
});
function historyHtml(history) {
  const entries = history?.events || [];
  return `${entries.length ? `<ol class="history-list">${entries.map(event=>`<li><div><strong>${esc(event.summary || "Recorded change")}</strong>${typeof event.details?.note === "string" && event.details.note ? `<p>${esc(event.details.note)}</p>` : ""}</div><div class="history-attribution"><time datetime="${esc(event.recorded_at)}">${esc(timestamp(event.recorded_at))}</time><span>${esc(event.actor === "public-fixture-seed" ? "Source records" : event.actor || "")}</span></div></li>`).join("")}</ol>` : history?.loading ? '<p role="status">Loading history…</p>' : history?.error ? "" : '<p class="history-empty">No changes recorded for this invoice.</p>'}${history?.error ? `<p class="field-error" role="alert">${esc(history.error)}</p><button class="text-button" type="button" data-history-retry>Try again</button>` : ""}${history?.has_more ? `<button class="text-button" type="button" data-history-more ${history.loading ? "disabled" : ""}>Earlier changes</button>` : ""}`;
}
async function loadHistory(append = false) {
  const invoice = state.invoice, content = $("#case-history-content");
  if (!invoice || !content || !$("#case-history")?.open) return;
  const same = state.history?.invoiceId === invoice.id && state.history.version === invoice.version;
  if (same && state.history.loading && state.history.container === content) return;
  if (!same || !append) state.history = {invoiceId:invoice.id,version:invoice.version,events:[],has_more:false,next_before:null};
  const history = state.history;
  history.container = content; history.loading = true; history.error = "";
  content.innerHTML = historyHtml(history);
  try {
    const query = new URLSearchParams({limit:"20"});
    if (append && history.next_before) query.set("before",String(history.next_before));
    const result = await api(`/api/invoices/${encodeURIComponent(invoice.id)}/history?${query}`);
    if (state.invoice?.id !== invoice.id || state.invoice.version !== invoice.version || content !== $("#case-history-content") || state.history !== history) return;
    history.events = append ? history.events.concat(result.events) : result.events;
    history.has_more = result.has_more; history.next_before = result.next_before;
  } catch(error) {
    if (error.status !== 401 && state.history === history) history.error = error.message;
  } finally {
    history.loading = false;
    if (state.history === history && state.invoice?.id === invoice.id && state.invoice.version === invoice.version && content === $("#case-history-content")) content.innerHTML = historyHtml(history);
  }
}
async function downloadBrief(button) {
  const invoice = state.invoice;
  if (invoice?.status !== "prepared") return;
  button.disabled = true; button.setAttribute("aria-busy","true");
  try { download(await api(`/api/invoices/${encodeURIComponent(invoice.id)}/brief`,{blob:true}),`${invoice.invoice_number}-billing-brief.html`); notify("Billing brief downloaded."); }
  catch(error) { if (error.status !== 401) notify(error.message,true); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); if (button.isConnected) button.focus({preventScroll:true}); }
}
$("#case-content").addEventListener("toggle",event=>{
  if (event.target.id !== "case-history" || !event.target.open) return;
  const current = state.history?.invoiceId === state.invoice?.id && state.history?.version === state.invoice?.version;
  if (current && !state.history.loading && !state.history.error) $("#case-history-content").innerHTML = historyHtml(state.history);
  else loadHistory();
},true);
function nextQueueId() {
  if (!state.invoice) return null;
  const index = state.rows.findIndex(item => String(item.id) === String(state.invoice.id));
  if (index >= 0) return state.rows[index + 1]?.id ?? null;
  if (state.nextId && state.rows.some(item => String(item.id) === String(state.nextId))) return state.nextId;
  if (state.queueRemovedIndex !== null) return state.rows[state.queueRemovedIndex]?.id ?? null;
  return state.rows[0]?.id ?? null;
}
function updateCaseNavigation() {
  const button = $("#case-next");
  if (!button) return;
  button.hidden = !state.caseOpen || !state.invoice || (!nextQueueId() && !state.hasMore);
  button.disabled = state.busy || state.navigationBusy;
  button.textContent = state.navigationBusy ? "Loading…" : "Next invoice";
}
async function advanceInvoice(button) {
  if (!state.caseOpen || !state.invoice || state.busy || state.navigationBusy) return;
  if (!mayLeaveCase()) return;
  const id = state.invoice.id, sequence = state.caseSequence, edits = state.caseEdits;
  const current = () => state.caseOpen && state.invoice?.id === id && state.caseSequence === sequence && state.caseEdits === edits && !state.busy;
  state.navigationBusy = true;
  button.disabled = true;
  try {
    if (!nextQueueId() && state.queueNeedsRefill && state.hasMore && !await loadQueue({guard:current})) return;
    if (!current()) return;
    if (!nextQueueId() && state.hasMore) {
      const previousOffset = state.queueOffset;
      if (!await loadQueue({offset:previousOffset + state.queueLimit,resetScroll:true,guard:current})) return;
      if (!current() || state.queueOffset <= previousOffset) return;
    }
    if (!current()) return;
    const nextId = nextQueueId();
    if (nextId) await openInvoice(nextId,{discardConfirmed:true});
  } finally { state.navigationBusy = false; updateCaseNavigation(); }
}
$("#case-next").addEventListener("click", event => advanceInvoice(event.currentTarget));
function renderPlaceholder() {
  $("#case-content").innerHTML = '<div class="case-placeholder"><h2>Select an invoice</h2></div>';
}
function mayLeaveCase() {
  if (state.busy) { notify("Wait for the current action to finish."); return false; }
  return !state.caseDirty || window.confirm("Leave these unsaved changes?");
}
async function openInvoice(id, {focus = true, discardConfirmed = false} = {}) {
  if (String(state.invoice?.id) === String(id)) { state.caseOpen = true; $("#workspace-main").classList.add("case-open"); updateCaseNavigation(); return; }
  if (state.busy || (!discardConfirmed && !mayLeaveCase())) return;
  const sequence = ++state.caseSequence;
  state.history = null;
  $("#notice").hidden = true;
  state.caseDirty = false;
  state.replyPollError = "";
  if (!state.caseOpen) { state.queueScroll = window.scrollY; state.queueListScroll = $("#queue-scroll").scrollTop; }
  state.caseOpen = true;
  const position = state.rows.findIndex(item => String(item.id) === String(id));
  state.nextId = position >= 0 ? state.rows[position + 1]?.id ?? null : null;
  state.queueRemovedIndex = null;
  state.invoice = null;
  state.selectedId = id;
  state.caseTab = "review";
  state.docId = null;
  state.page = 0;
  state.sourceExpanded = false;
  $("#queue-view").hidden = false;
  $("#case-view").hidden = false;
  $("#workspace-main").classList.add("case-open");
  renderQueue();
  $("#case-content").innerHTML = '<div class="case-loading" role="status" aria-label="Loading invoice"><div class="loading-lines"><span></span><span></span><span></span></div><span class="sr-only">Loading invoice</span></div>';
  window.scrollTo({top:0,behavior:"instant"});
  if (focus) $("#case-view").focus({preventScroll:true});
  try {
    const invoice = await api(`/api/invoices/${encodeURIComponent(id)}`);
    if (sequence !== state.caseSequence) return;
    state.invoice = invoice;
    const next = nextAction(invoice);
    state.caseTab = stageForNext(next);
    renderCase();
    window.InvoiceClearUI?.reveal($("#case-content"));
  } catch (error) {
    if (sequence !== state.caseSequence || error.status === 401) return;
    $("#case-content").innerHTML = `<div class="empty-state"><h3>Invoice could not load.</h3><p>${esc(error.message)}</p><button type="button" class="text-button" data-retry-case="${esc(id)}">Try again</button></div>`;
  }
}
async function returnToQueue() {
  if (!mayLeaveCase()) return false;
  const id = state.invoice?.id;
  const savedListScroll = state.queueListScroll;
  const sequence = ++state.caseSequence;
  state.caseOpen = false;
  state.invoice = null;
  state.caseDirty = false;
  state.queueRemovedIndex = null;
  $("#workspace-main").classList.remove("case-open");
  $("#notice").hidden = true;
  if (state.queueNeedsRefill) await loadQueue({guard:() => !state.caseOpen && state.caseSequence === sequence});
  if (state.caseOpen || state.caseSequence !== sequence) return false;
  window.scrollTo({top:state.queueScroll,behavior:"instant"});
  $("#queue-scroll").scrollTop = savedListScroll;
  state.queueListScroll = $("#queue-scroll").scrollTop;
  const row = $$('[data-invoice]').find(item => item.dataset.invoice === String(id));
  (row || $("#queue-scroll")).focus({preventScroll:true});
  window.InvoiceClearUI?.reveal($("#queue-view"), -1);
  return true;
}
$("#queue-back").addEventListener("click", returnToQueue);

function handoffInstruction(invoice) {
  return invoice.billing_instruction || "";
}
function nextAction(invoice) {
  const accounting = invoice.integration?.accounting;
  const uncertainSend = invoice.integration?.messages?.some(message => message.direction === "outgoing" && ["pending","unknown"].includes(message.status));
  if (uncertainSend) return {title:"Check the Gmail send receipt",button:"Verify receipt",kind:"verify-send"};
  if (["pending","unknown"].includes(accounting?.action?.state)) return {title:"Check the QuickBooks update",button:"Verify receipt",kind:"verify-update"};
  if (accounting?.source_changed || accounting?.error) return {title:"QuickBooks needs review",note:accounting.error || "The source changed after intake.",button:"Refresh source",kind:"refresh"};
  if (accounting && accounting.payment_status !== "unpaid") return {title:accounting.payment_status === "paid" ? "No balance in QuickBooks" : "Partly settled in QuickBooks",button:"Refresh source",kind:"refresh"};
  if (invoice.status === "prepared") return {title:"Billing handoff approved",note:handoffInstruction(invoice),button:"Download handoff",kind:"handoff"};
  if (!(invoice.documents || []).length) {
    if (state.connections?.gmail?.connected && invoice.integration?.messages?.some(message => message.direction === "incoming" && message.status === "held")) return {title:"Message needs review",button:"Check replies",kind:"replies",upload:true};
    if ((invoice.replies || []).length) return {title:(invoice.replies || []).some(reply => reply.source === "gmail") ? "Customer reply received" : "Customer reply recorded",button:"Attach PO",kind:"received",upload:true};
    const sent = invoice.integration?.messages?.some(message => message.direction === "outgoing" && message.status === "sent");
    if (sent && state.connections?.gmail?.connected) return {title:"Waiting for the purchase order",button:"Check replies",kind:"replies",upload:true};
    return {title:"Purchase order missing",button:canSendRequest(invoice) ? "Review request" : "Copy PO request",kind:canSendRequest(invoice) ? "send" : "request",upload:true};
  }
  if (canApprove(invoice)) return {title:"Ready for billing handoff",note:handoffInstruction(invoice),button:"Approve handoff",kind:"approve"};
  if (!invoice.documents[invoice.documents.length - 1].reviewed) return {title:"Confirm the purchase order",button:"Review PO",kind:"review"};
  const failures = (invoice.checks || []).filter(check => check.status !== "pass");
  if (failures.length === 1 && failures[0].code === "po_reference") {
    const check = failures[0];
    return {title:"PO reference needs correction",note:`${check.invoice_value || "Missing reference"} → ${check.document_value}`,button:"Review correction",kind:"correction"};
  }
  const coverage = failures.length === 1 && failures[0].code === "po_coverage" ? failures[0] : null;
  const shortfall = coverage && Number.isInteger(coverage.available_cents) ? Math.max(0,invoice.amount_cents - coverage.available_cents) : null;
  return {title:shortfall ? `${money({...invoice,amount_cents:shortfall})} beyond PO coverage` : blockerText(invoice) || "Purchase order differs",button:canSendRequest(invoice) ? "Review request" : "Copy request",kind:canSendRequest(invoice) ? "send" : "request",upload:true};
}
function hasPendingExternal(invoice) { return ["pending","unknown"].includes(invoice.integration?.accounting?.action?.state) || Boolean(invoice.integration?.messages?.some(message => ["pending","unknown"].includes(message.status))); }
function canSendRequest(invoice) { return !hasPendingExternal(invoice) && Boolean(invoice.integration?.accounting && state.connections?.gmail?.connected && !invoice.integration.accounting.source_changed && !invoice.integration.accounting.error && invoice.integration.accounting.payment_status === "unpaid"); }
function canApprove(invoice) { const accounting = invoice.integration?.accounting; return !hasPendingExternal(invoice) && (!accounting || (!accounting.source_changed && !accounting.error && accounting.payment_status === "unpaid")) && invoice.status !== "prepared" && (invoice.documents || []).some(doc => doc.reviewed) && (invoice.checks || []).length > 0 && invoice.checks.every(check => check.status === "pass"); }
function checkValue(check, value, invoice) { return check.code === "po_coverage" && typeof value === "number" ? money({...invoice,amount_cents:value}) : displayValue(value); }
function stageForNext(next) {
  if (["request","send","replies","received","verify-send"].includes(next.kind)) return "correspondence";
  if (["refresh","verify-update"].includes(next.kind)) return "intake";
  return ["approve","handoff"].includes(next.kind) ? "handoff" : "review";
}
function journeySteps(invoice) {
  const docs = invoice.documents || [], doc = docs[docs.length - 1], next = nextAction(invoice);
  const messages = invoice.integration?.messages || [];
  const incoming = messages.some(item => item.direction === "incoming" && item.status !== "held");
  const held = messages.some(item => item.direction === "incoming" && item.status === "held");
  const sent = messages.some(item => item.direction === "outgoing" && item.status === "sent");
  const manualReply = (invoice.replies || []).some(item => item.source === "manual");
  const prepared = invoice.status === "prepared", active = stageForNext(next);
  const reviewed = Boolean(doc?.reviewed), checksPassed = reviewed && (invoice.checks || []).length > 0 && invoice.checks.every(check => check.status === "pass");
  return [
    {id:"intake",label:"Invoice",caption:invoice.integration?.accounting ? "QuickBooks Online" : "Source recorded",state:active === "intake" ? "current" : "complete"},
    {id:"correspondence",label:"Request",caption:next.kind === "verify-send" ? "Verify send receipt" : held ? "Message needs review" : incoming ? "Reply received" : sent ? "Request sent" : manualReply ? "Reply recorded" : docs.length ? "PO attached" : "PO needed",state:active === "correspondence" ? "current" : incoming || sent || manualReply ? "complete" : "upcoming"},
    {id:"review",label:"Review PO",caption:!doc ? "Awaiting document" : !reviewed ? "Confirm fields" : checksPassed ? "Checks passed" : "Discrepancy found",state:active === "review" ? "current" : checksPassed ? "complete" : "upcoming"},
    {id:"handoff",label:"Handoff",caption:prepared ? "Approved" : canApprove(invoice) ? "Approval required" : "Not ready",state:prepared ? "complete" : active === "handoff" ? "current" : "upcoming"}
  ];
}
const investigationSteps = {
  request_po:{label:"Review request",stage:"correspondence"},
  clarify_po:{label:"Review request",stage:"correspondence"},
  review_po:{label:"Review PO",stage:"review"},
  propose_reference:{label:"Review correction",stage:"review"},
  review_handoff:{label:"Review handoff",stage:"handoff"},
  verify_receipt:{label:"Review receipt",stage:"correspondence"},
  refresh_accounting:{label:"Review source",stage:"intake"}
};
function investigationStale(run, invoice = state.invoice) {
  return Boolean(run && (run.stale || !Number.isInteger(run.source_version) || run.source_version !== invoice?.version));
}
function investigationHtml(invoice) {
  const snapshot = invoice.investigation || {}, run = snapshot.latest;
  const pending = state.investigationPending.get(String(invoice.id));
  const active = pending?.sequence === state.caseSequence && pending.token === state.token;
  const savedIssue = state.investigationErrors.get(String(invoice.id));
  const issue = savedIssue?.version === invoice.version ? savedIssue : null;
  if (!snapshot.available && !run && !active && !issue) return "";
  const stale = investigationStale(run,invoice), running = run?.status === "running";
  const completed = run?.status === "completed", step = completed && !stale ? investigationSteps[run.next_step] : null;
  const action = snapshot.available ? `<button type="button" class="button quiet" data-investigate ${active ? "disabled" : ""}>${active ? "Investigating…" : running || issue?.unknown ? "Check result" : run ? "Investigate again" : "Investigate"}</button>` : "";
  const status = active ? "" : stale ? "Records changed. Investigate again." : ({running:"Investigation in progress.",abstained:"More evidence needed.",failed:"Investigation could not finish.",interrupted:"Investigation was interrupted."})[run?.status] || "";
  const citations = Array.isArray(run?.citations) ? run.citations.slice(0,12) : [];
  const trace = Array.isArray(run?.trace) ? run.trace.slice(0,16) : [];
  return `<div class="investigation-heading"><h3>Investigation</h3>${action}</div><div class="investigation-result" role="status" aria-live="polite" aria-atomic="false">${active ? `<p class="${action ? "sr-only" : "investigation-note"}">Investigating invoice.</p>` : ""}${issue ? `<p class="field-error">${esc(issue.message)}</p>` : ""}${run ? `${status ? `<p class="investigation-note${stale ? " investigation-stale" : ""}">${esc(status)}</p>` : ""}${run.summary ? `<p class="investigation-summary">${esc(run.summary)}</p>` : ""}${run.error ? `<p class="field-error">${esc(run.error)}</p>` : ""}${citations.length ? `<ul class="investigation-sources">${citations.map((citation,index) => `<li>${citation.document_id || citation.reply_id ? `<button type="button" class="source-link" data-investigation-source="${index}">${esc(citation.label || "Source record")}${citation.page ? ` · p. ${esc(citation.page)}` : ""}</button>` : `<strong>${esc(citation.label || "Source record")}</strong>`}<blockquote>${esc(citation.excerpt)}</blockquote></li>`).join("")}</ul>` : ""}${completed && run.draft ? `<details class="investigation-draft"><summary>Response draft</summary><pre>${esc(run.draft)}</pre><button type="button" class="text-button" data-copy-investigation ${stale ? "disabled" : ""}>Copy draft</button></details>` : ""}${step ? `<button type="button" class="text-button" data-investigation-next>${esc(step.label)} <span aria-hidden="true">→</span></button>` : ""}${trace.length ? `<details class="investigation-activity"><summary>Tool activity${run.model ? ` · ${esc(run.model)}` : ""}</summary><ol>${trace.map(item => `<li><span>${esc(item.tool || item.name || "Tool")}</span><span>${esc(item.status || "recorded")}</span></li>`).join("")}</ol></details>` : ""}` : ""}</div>`;
}
function renderInvestigation() {
  const section = $("#case-investigation");
  if (!section || !state.invoice) return;
  const focused = document.activeElement?.closest?.("#case-investigation"), activity = $(".investigation-activity",section)?.open, draft = $(".investigation-draft",section)?.open;
  section.innerHTML = investigationHtml(state.invoice);
  section.hidden = !section.innerHTML;
  if (activity) { const disclosure = $(".investigation-activity",section); if (disclosure) disclosure.open = true; }
  if (draft) { const disclosure = $(".investigation-draft",section); if (disclosure) disclosure.open = true; }
  if (focused) ($("[data-investigate]",section) || (section.hidden ? $("#case-view") : section))?.focus({preventScroll:true});
}
function applyInvestigation(snapshot, request) {
  if (state.token !== request.token || state.caseSequence !== request.sequence || String(state.invoice?.id) !== request.id || !state.caseOpen) return false;
  state.invoice.investigation = snapshot;
  renderInvestigation();
  return true;
}
async function investigateCase() {
  const invoice = state.invoice;
  if (!invoice || !state.caseOpen || !invoice.investigation?.available || state.busy) return;
  const id = String(invoice.id), existing = state.investigationPending.get(id);
  if (existing?.sequence === state.caseSequence && existing.token === state.token) return;
  const request = {id,sequence:state.caseSequence,token:state.token,version:invoice.version,previousRun:invoice.investigation.latest?.id};
  const identity = `investigation:${id}:${invoice.version}`, issue = state.investigationErrors.get(id);
  const uncertain = issue?.unknown && issue.version === invoice.version || invoice.investigation.latest?.status === "running";
  if (!uncertain && !window.confirm("Send this case’s saved records to TogetherAI for investigation?")) return;
  state.investigationPending.set(id,request);
  state.investigationErrors.delete(id);
  renderInvestigation();
  try {
    if (uncertain) {
      const snapshot = await api(`/api/invoices/${encodeURIComponent(id)}/investigation`);
      if (!applyInvestigation(snapshot,request)) return;
      if (snapshot.latest && (snapshot.latest.status === "running" || !issue?.unknown || snapshot.latest.id !== issue.previousRun)) {
        if (snapshot.latest.status !== "running") state.operationKeys.delete(identity);
        return;
      }
      if (!window.confirm("Retry this investigation using the same request?")) return;
    }
    if (!state.operationKeys.has(identity)) state.operationKeys.set(identity,uid());
    const run = await api(`/api/invoices/${encodeURIComponent(id)}/investigate`,{method:"POST",body:{expected_version:request.version,idempotency_key:state.operationKeys.get(identity)}});
    state.operationKeys.delete(identity);
    applyInvestigation({...invoice.investigation,latest:run},request);
  } catch (error) {
    if (error.status === 401) return;
    if (state.token !== request.token || state.caseSequence !== request.sequence || String(state.invoice?.id) !== id || !state.caseOpen) return;
    const unknown = Boolean(uncertain) || !error.status || error.status >= 500;
    state.investigationErrors.set(id,{unknown,message:error.message,version:request.version,previousRun:request.previousRun});
    if (unknown) {
      try {
        const snapshot = await api(`/api/invoices/${encodeURIComponent(id)}/investigation`);
        if (state.token !== request.token || state.caseSequence !== request.sequence || String(state.invoice?.id) !== id || !state.caseOpen) return;
        if (snapshot.latest && snapshot.latest.id !== request.previousRun) {
          state.investigationErrors.delete(id);
          applyInvestigation(snapshot,request);
          if (snapshot.latest.status !== "running") state.operationKeys.delete(identity);
        }
      } catch (recoveryError) { if (recoveryError.status === 401) return; }
    }
  } finally {
    if (state.investigationPending.get(id) === request) state.investigationPending.delete(id);
    if (state.token === request.token && state.caseSequence === request.sequence && String(state.invoice?.id) === id && state.caseOpen) renderInvestigation();
  }
}
function showInvestigationSource(index) {
  const invoice = state.invoice, citation = invoice?.investigation?.latest?.citations?.[Number(index)];
  if (!citation) return;
  if (citation.document_id) {
    const doc = invoice.documents?.find(item => String(item.id) === String(citation.document_id));
    const page = doc?.pages?.findIndex(item => item.number === citation.page);
    if (!doc || page == null || page < 0 || !doc.pages[page].text.includes(citation.excerpt)) { notify("Source changed. Investigate again.",true); return; }
    if (String(state.docId) !== String(doc.id)) {
      if (!mayLeaveCase()) return;
      state.docId = doc.id; state.page = page; state.sourceExpanded = false; state.caseDirty = false; renderCase();
    }
    showDocumentPage(doc,page,citation.excerpt);
    focusSection("#document-preview",".document-text");
  } else if (citation.reply_id) {
    const reply = [...(invoice.replies || []),...(invoice.integration?.messages || [])].find(item => String(item.id) === String(citation.reply_id));
    if (!reply || !String(reply.text || reply.body || "").includes(citation.excerpt)) { notify("Source changed. Investigate again.",true); return; }
    activateCaseTab("correspondence",true);
    const target = $$(".reply-list p",$("#request-section")).find(item => item.textContent === String(reply.text || reply.body || ""));
    target?.setAttribute("tabindex","-1");
    target?.scrollIntoView({block:"center",behavior:"instant"});
    target?.focus({preventScroll:true});
  }
}
function renderCase() {
  const invoice = state.invoice;
  if (!invoice) return;
  const docs = invoice.documents || [], prepared = invoice.status === "prepared" && Boolean(invoice.resolution), next = nextAction(invoice);
  if (!docs.some(doc => String(doc.id) === String(state.docId))) { state.docId = docs[docs.length - 1]?.id ?? null; state.page = 0; }
  const doc = docs.find(item => String(item.id) === String(state.docId)), latest = docs[docs.length - 1];
  const checks = invoice.checks || [], failed = checks.filter(check => check.status !== "pass");
  const currentDocument = !doc || String(doc.id) === String(latest?.id);
  const compareEvidence = currentDocument && Boolean(latest?.reviewed);
  const referenceOnly = compareEvidence && failed.length === 1 && failed[0].code === "po_reference";
  const proposalReference = invoice.proposed_po_reference || (referenceOnly ? valueOf(latest.fields?.po_number) : "");
  const proposalNote = referenceOnly ? `Reviewed purchase order confirms ${valueOf(latest.fields?.po_number)}; invoice records ${invoice.po_reference || "no PO reference"}.` : "";
  const upload = prepared ? "" : `<form class="upload-form" id="upload-form"><label class="field">Purchase order<input id="document-file" type="file" accept=".pdf,.txt,application/pdf,text/plain" aria-describedby="upload-hint" required><span id="upload-hint" class="upload-hint">Choose or drop one PDF or TXT · up to 5 MB</span></label><button class="button quiet" type="submit">Add document</button>${state.runtime?.mode === "demo" ? '<p class="upload-privacy">Files are visible to other visitors.</p>' : ""}<p class="field-error" id="upload-error" role="alert" hidden></p></form>`;
  const stages = journeySteps(invoice);
  const activeTab = stages.some(stage => stage.id === state.caseTab) ? state.caseTab : stageForNext(next);
  const panel = (name, content) => `<section class="case-panel" id="panel-${name}" role="tabpanel" aria-labelledby="tab-${name}" tabindex="0" ${activeTab === name ? "" : "hidden"}>${content}</section>`;
  const primary = kind => `<button class="button primary" type="button" data-next="${esc(kind || next.kind)}">${esc(kind === "handoff" ? "Download handoff" : kind === "approve" ? "Approve handoff" : next.button)}</button>`;
  const go = (stage,label) => `<button class="button primary" type="button" data-go-stage="${stage}">${label}</button>`;
  const stageHeading = (label,title) => `<header class="stage-heading"><h3>${esc(title)}</h3></header>`;
  const accounting = invoice.integration?.accounting;
  const payment = accounting ? ({paid:"No balance in QuickBooks",partial:"Partly settled in QuickBooks",unpaid:"Unpaid"}[accounting.payment_status] || "Needs source review") : "Unpaid";
  const invoiceSheet = `<aside class="invoice-sheet" aria-label="Source invoice"><dl class="invoice-facts"><div><dt>Original PO reference</dt><dd>${esc(invoice.po_reference || "Missing")}</dd></div>${invoice.proposed_po_reference ? `<div class="proposed"><dt>Proposed correction</dt><dd>${esc(invoice.proposed_po_reference)}</dd></div>` : ""}<div><dt>Currency</dt><dd>${esc(invoice.currency)}</dd></div><div><dt>Issued</dt><dd>${esc(date(invoice.issue_date))}</dd></div><div><dt>Due</dt><dd>${esc(date(invoice.due_date))}</dd></div><div><dt>Payment</dt><dd>${esc(payment)}</dd></div></dl></aside>`;
  const intake = `${stageHeading("", "Invoice details")}${invoiceSheet}<section class="intake-source"><dl class="source-facts">${accounting && !prepared ? "" : `<div><dt>Source</dt><dd>${accounting ? "QuickBooks Online" : "Recorded invoice"}</dd></div>`}<div><dt>Record</dt><dd>${esc(invoice.source_ref)}</dd></div><div><dt>Billing contact</dt><dd>${esc(invoice.customer_email)}</dd></div></dl>${prepared ? "" : accountingHtml(invoice,next.kind)}</section><div class="stage-action"><div><strong>${esc(next.title)}</strong>${next.kind === "refresh" && next.note ? `<p>${esc(next.note)}</p>` : ""}</div>${["refresh","verify-update"].includes(next.kind) ? primary() : go(stageForNext(next), next.kind === "handoff" ? "Open handoff" : next.kind === "approve" ? "Review handoff" : ["request","send","replies","received","verify-send"].includes(next.kind) ? "Open request" : "Review purchase order")}</div>`;
  const requestPrimary = ["request","send","replies","received","verify-send"].includes(next.kind);
  const correspondence = `${stageHeading("02 · Request", next.kind === "received" ? "Customer reply" : next.kind === "replies" ? "Awaiting purchase order" : "PO request")}<section id="request-section" aria-label="Customer correspondence">${next.kind === "received" ? `<details class="case-disclosure"><summary>Request draft</summary><div class="request-paper">${requestHtml(invoice,next.kind)}</div></details>` : `<div class="request-paper">${requestHtml(invoice,next.kind)}</div>`}${mailHtml(invoice,next.kind)}<ul class="reply-list">${(invoice.replies || []).filter(reply => reply.source === "manual").map(reply => `<li><time>${esc(date(reply.received_at))} · Recorded reply</time><p>${esc(reply.text)}</p></li>`).join("")}</ul>${prepared ? "" : `<details class="case-disclosure"><summary>Record a reply</summary><div class="disclosure-body"><form id="reply-form" class="reply-form"><label class="field">Customer reply<textarea name="text" rows="3" maxlength="5000" required></textarea></label><p id="reply-error" class="field-error" role="alert" hidden></p><button class="button quiet" type="submit">Save reply</button></form></div></details>`}</section><div class="stage-action"><div class="next-controls">${requestPrimary ? primary() : ""}${prepared ? go("handoff","Open handoff") : docs.length ? go("review","Review purchase order") : next.kind === "received" ? "" : '<button class="button quiet" type="button" data-next="upload">Attach PO</button>'}</div></div>`;
  const correction = prepared || !compareEvidence || !(failed.some(check => check.code === "po_reference") || invoice.proposed_po_reference) ? "" : `<details class="case-disclosure" id="proposal-section" ${referenceOnly ? "open" : ""}><summary>Correct PO reference</summary><div class="disclosure-body"><form id="proposal-form" class="proposal-form"><div class="form-grid"><label class="field">PO reference<input name="po_reference" value="${esc(proposalReference)}" maxlength="80" required></label><label class="field">Reason<textarea name="note" rows="2" minlength="10" maxlength="500" required>${esc(proposalNote)}</textarea></label></div><p id="proposal-error" class="field-error" role="alert" hidden></p><button class="button ${referenceOnly ? "primary" : "quiet"}" type="submit">Save correction</button></form></div></details>`;
const reviewTools = `${compareEvidence && failed.length ? `<div class="issue-focus">${comparisonHtml(invoice,failed,docs,true)}</div>` : ""}${correction}${compareEvidence && checks.some(check => check.status === "pass") ? `<details class="case-disclosure checks-disclosure"><summary>Checks<span>${checks.filter(check => check.status === "pass").length} of ${checks.length}</span></summary><div class="comparison-list">${comparisonHtml(invoice,checks.filter(check => check.status === "pass"),docs)}</div></details>` : ""}${currentDocument && (canApprove(invoice) || prepared) ? `<div class="stage-action">${go("handoff",prepared ? "Open handoff" : "Review handoff")}</div>` : compareEvidence && !referenceOnly && failed.length ? `<div class="stage-action">${go("correspondence","Request corrected PO")}</div>` : ""}`;
  const review = `${!doc ? stageHeading("","Attach purchase order") : ""}<section class="evidence-section" id="evidence-section" aria-label="Purchase order">${doc ? `${documentHtml(doc,prepared,reviewTools)}${upload ? `<details class="upload-disclosure" id="upload-section"><summary>Add purchase order</summary>${upload}</details>` : ""}` : `<div class="evidence-empty">${upload}</div>`}</section>`;
  const handoffReady = prepared || canApprove(invoice);
  const handoff = `${stageHeading("04 · Handoff", prepared ? "Billing handoff approved" : handoffReady ? "Approve the billing handoff" : "Handoff blocked")}<section class="handoff-sheet" data-approved="${prepared}">${handoffReady ? `<p class="billing-instruction">${esc(handoffInstruction(invoice))}</p><dl class="handoff-facts"><div><dt>PO reference</dt><dd>${esc(prepared ? invoice.resolution?.po_reference : invoice.proposed_po_reference || invoice.po_reference)}</dd></div><div class="handoff-evidence"><dt>Evidence</dt><dd>${esc((prepared ? docs.find(item => item.id === invoice.resolution?.document_id) : latest)?.filename || latest?.filename || "Approved purchase order")}<img class="handoff-art" src="/art/handoff-folio.svg" width="264" height="180" alt="" aria-hidden="true" loading="lazy"></dd></div>${prepared && invoice.resolution?.prepared_by ? `<div><dt>Approved by</dt><dd>${esc(invoice.resolution.prepared_by)}</dd></div>` : ""}</dl>` : `<p class="handoff-blocker">${esc(next.title)}</p>`}${prepared ? `<div class="handoff-record">${invoice.resolution?.prepared_at ? `<p>Approved ${esc(date(invoice.resolution.prepared_at))}</p>` : ""}${invoice.resolution?.note ? `<p>${esc(invoice.resolution.note)}</p>` : ""}</div>` : ""}<div class="stage-action">${handoffReady ? primary(prepared ? "handoff" : "approve") : go(["refresh","verify-update"].includes(next.kind) ? "intake" : next.kind === "verify-send" ? "correspondence" : "review",["refresh","verify-update"].includes(next.kind) ? "Review source" : next.kind === "verify-send" ? "Verify send receipt" : "Return to review")}${prepared ? '<button class="text-button" data-download-brief type="button">Billing brief</button><button class="text-button" data-reopen type="button">Reopen for review</button>' : ""}</div>${prepared ? accountingHtml(invoice,next.kind) : ""}</section>`;
  const investigation = investigationHtml(invoice);
  $("#case-content").innerHTML = `<header class="case-header"><div class="case-heading"><strong class="case-number">${esc(invoice.invoice_number)}</strong><h2>${esc(invoice.customer)}</h2></div><div class="case-amount">${esc(money(invoice))}<small>${esc(payment)}</small></div></header><section id="case-investigation" class="case-investigation" aria-label="Invoice investigation" tabindex="-1" ${investigation ? "" : "hidden"}>${investigation}</section><ol class="journey-nav" role="tablist" aria-label="Invoice workflow">${stages.map(stage => `<li role="presentation"><button class="journey-stop" type="button" role="tab" id="tab-${stage.id}" data-case-tab="${stage.id}" data-state="${stage.state}" aria-controls="panel-${stage.id}" aria-selected="${activeTab === stage.id}" tabindex="${activeTab === stage.id ? 0 : -1}"><span>${stage.label}</span></button></li>`).join("")}</ol><div class="document-spread"><div class="po-sheet">${panel("intake",intake)}${panel("correspondence",correspondence)}${panel("review",review)}${panel("handoff",handoff)}</div></div><details id="case-history" class="case-disclosure case-history"><summary>History</summary><div id="case-history-content" class="history-content"></div></details>`;
  updateCaseNavigation();
}
function comparisonHtml(invoice, checks, docs, compact = false) {
  return checks.map(check => {
    const source = docs.find(item => String(item.id) === String(check.document_id));
    const sourceLink = source ? `<button type="button" class="source-link comparison-source" data-source="${esc(source.id)}" data-field="${esc(({po_reference:"po_number",po_coverage:"amount",document_review:"po_number"})[check.code] || check.code)}" data-page="${esc(check.source_page || 1)}" title="${esc(source.filename)} · page ${esc(check.source_page || 1)}">${compact ? "View source" : `${esc(source.filename)}${check.source_page ? ` · page ${esc(check.source_page)}` : ''}`}</button>` : '';
    const title = compact && check.status === "fail" ? ({po_reference:"PO reference differs",po_coverage:"PO coverage is short",customer:"Customer differs",currency:"Currency differs"})[check.code] || check.label : check.label;
    const invoiceValue = checkValue(check,check.invoice_value,invoice), documentValue = checkValue(check,check.document_value,invoice);
    const showDifference = compact && check.status === "fail" && check.code === "po_reference";
    return `<article class="comparison-item" data-check="${esc(check.code)}"><div class="comparison-heading"><h4>${esc(title)}</h4>${compact && check.status === "fail" ? "" : `<span class="check-state ${esc(check.status)}">${check.status === "pass" ? "Matched" : check.status === "fail" ? "Difference" : "Needs review"}</span>`}${compact ? sourceLink : ''}</div><dl class="compare-pair"><div><dt>${check.code === "po_reference" && invoice.proposed_po_reference ? "Proposed reference" : "Invoice"}</dt><dd>${showDifference ? referenceDifference(invoiceValue,documentValue) : esc(invoiceValue)}</dd></div><div><dt>Purchase order</dt><dd>${showDifference ? referenceDifference(documentValue,invoiceValue) : esc(documentValue)}</dd></div></dl>${compact && check.code === "po_coverage" && Number.isInteger(check.reserved_cents) && check.reserved_cents > 0 && Number.isInteger(check.available_cents) ? `<p class="coverage-context" title="Prepared allocations in this workspace only. External PO usage is not verified."><strong>${esc(money({...invoice,amount_cents:check.available_cents}))} available here</strong><span>${esc(money({...invoice,amount_cents:check.reserved_cents}))} allocated</span></p>` : ""}${!compact && check.status !== "pass" && check.message ? `<p class="comparison-outcome">${esc(check.message)}</p>` : ''}${compact ? '' : sourceLink}</article>`;
  }).join("");
}
function referenceDifference(value, other) {
  const text = String(value), compared = String(other);
  let start = 0, end = text.length, otherEnd = compared.length;
  while (start < end && start < otherEnd && text[start] === compared[start]) start++;
  while (end > start && otherEnd > start && text[end - 1] === compared[otherEnd - 1]) { end--; otherEnd--; }
  if (start === end) return esc(text);
  return esc(text.slice(0,start)) + '<mark class="value-difference">' + esc(text.slice(start,end)) + "</mark>" + esc(text.slice(end));
}
function activateCaseTab(name, focus = false) {
  if (!["intake","correspondence","review","handoff"].includes(name)) return;
  const changed = state.caseTab !== name;
  state.caseTab = name;
  $$('[data-case-tab]').forEach(tab => {
    const selected = tab.dataset.caseTab === name;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  });
  $$('.case-panel').forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
  if (changed) window.InvoiceClearUI?.stage?.($(`#panel-${name}`));
}
function accountingHtml(invoice, primaryKind) {
  const accounting = invoice.integration?.accounting;
  if (!accounting) return "";
  const approvedReference = invoice.status === "prepared" ? reference(invoice.resolution?.po_reference) : "", matchesApproved = Boolean(approvedReference) && approvedReference === reference(accounting.po_reference);
  const uncertain = ["pending","unknown"].includes(accounting.action?.state), applied = matchesApproved && accounting.action?.state === "confirmed" && approvedReference === reference(accounting.action.reference) && accounting.applied_at;
  const canApply = approvedReference && !matchesApproved && !hasPendingExternal(invoice) && !accounting.source_changed && !accounting.error && accounting.payment_status === "unpaid" && state.connections?.qbo?.connected && state.connections.qbo.po_field_configured;
  return `<section class="accounting-record" aria-label="Accounting record"><div><strong>QuickBooks Online</strong><span>${esc(accounting.environment === "sandbox" ? "Sandbox" : "Production")} · ${esc(date(accounting.synced_at))}</span>${accounting.balance_cents != null ? `<small>Balance ${esc(money({...invoice,amount_cents:accounting.balance_cents}))} · PO ${esc(accounting.po_reference || "Missing")}</small>` : ""}${applied ? `<small>PO correction verified ${esc(date(applied))}</small>` : ""}</div><div class="next-controls">${canApply ? '<button type="button" class="button quiet" data-apply-reference>Update PO in QuickBooks</button>' : ""}${primaryKind !== "refresh" || invoice.status === "prepared" ? '<button type="button" class="text-button" data-refresh-accounting>Refresh</button>' : ""}${uncertain && primaryKind !== "verify-update" ? '<button type="button" class="text-button" data-reconcile-reference>Verify update receipt</button>' : ""}</div></section>`;
}
function mailReceiptHtml(message) {
  if (message.direction !== "outgoing" || message.status !== "sent" || !message.message_id || !message.thread_id) return "";
  return `<details class="provider-receipt"><summary>Gmail receipt</summary><dl><dt>Mailbox</dt><dd>${esc(message.mailbox)}</dd><dt>Message</dt><dd>${esc(message.message_id)}</dd><dt>Thread</dt><dd>${esc(message.thread_id)}</dd><dt>Sent</dt><dd>${esc(timestamp(message.sent_at))}</dd></dl></details>`;
}
function mailHtml(invoice, primaryKind) {
  const messages = invoice.integration?.messages || [];
  if (!invoice.integration?.accounting) return "";
  const uncertain = messages.some(message => message.direction === "outgoing" && ["pending","unknown"].includes(message.status));
  const rows = messages.map(message => `<li><time>${esc(date(message.sent_at || message.received_at || message.recorded_at || message.created_at))} · ${esc(message.direction === "incoming" ? message.status === "held" ? "Message needs review" : "Customer reply" : message.status === "sent" ? "Sent through Gmail" : "Sending needs review")}</time>${message.text || message.body ? `<p>${esc(message.text || message.body)}</p>` : ""}${mailReceiptHtml(message)}${message.error ? `<p class="field-error">${esc(message.error)}</p>` : ""}${message.attachments?.length ? `<ul class="mail-attachments">${message.attachments.map(attachment => `<li>${attachment.document_id ? `<button type="button" class="source-link" data-source="${esc(attachment.document_id)}" data-page="1">${esc(attachment.filename)}</button>` : esc(attachment.filename)}${attachment.error ? `<p class="field-error">${esc(attachment.error)}</p>` : ""}</li>`).join("")}</ul>` : ""}</li>`).join("");
  return `<div class="mail-actions">${!["send","replies"].includes(primaryKind) && canSendRequest(invoice) && invoice.status !== "prepared" && !uncertain ? '<button type="button" class="button quiet" data-send-request>Review &amp; send request</button>' : ""}${uncertain && primaryKind !== "verify-send" ? '<button type="button" class="text-button" data-reconcile-request>Verify Gmail receipt</button>' : ""}${state.connections?.gmail?.connected && primaryKind !== "replies" ? '<button type="button" class="text-button" data-sync-replies>Check replies</button>' : ""}</div>${state.replyPollError ? `<p class="field-error">${esc(state.replyPollError)}</p>` : ""}${rows ? `<ul class="reply-list">${rows}</ul>` : ""}`;
}
function documentHtml(doc, prepared, reviewTools = "") {
  const docs = state.invoice.documents, pages = doc.pages || [];
  const older = String(doc.id) !== String(docs[docs.length - 1]?.id);
  state.page = Math.max(0, Math.min(state.page, pages.length - 1));
  const page = pages[state.page], fields = (doc.reviewed ? doc.fields : doc.suggested_fields) || {};
  const origin = doc.extraction_error ? "Review source fields" : doc.extraction_method === "together" ? "TogetherAI suggestions" : "Document fields";
  const form = `<form class="review-fields" id="review-form">${doc.reviewed ? "" : `<h4>${origin}</h4>`}${doc.extraction_error ? `<p class="field-error">${esc(doc.extraction_error)}</p>` : ""}<div class="form-grid">${[["po_number","PO number"],["customer","Customer"],["currency","Currency"],["amount","PO amount"]].map(([name,label]) => `<div class="field-with-evidence"><label class="field">${label}<input name="${name}" value="${esc(valueOf(fields[name]))}" ${prepared ? "disabled" : ""} ${name === "amount" ? 'inputmode="decimal" pattern="[0-9]+(\\.[0-9]{1,2})?"' : ""} ${name === "currency" ? 'minlength="3" maxlength="3" pattern="[A-Za-z]{3}"' : 'maxlength="200"'} required></label>${fieldEvidence(doc,name)}</div>`).join("")}</div>${prepared ? "" : '<label class="field wide" data-correction-note hidden>Reason for correction<textarea name="note" rows="2" minlength="10" maxlength="500"></textarea></label><p class="field-error" id="review-error" role="alert" hidden></p><button class="button primary" type="submit">Confirm PO details</button>'}</form>`;
  const values = `<dl class="review-values">${[["po_number","PO number"],["customer","Customer"],["currency","Currency"],["amount","PO amount"]].map(([name,label]) => `<div><dt>${label}</dt><dd>${esc(valueOf(fields[name])) || "—"}</dd></div>`).join("")}</dl>`;
  const confirmed = `<div class="review-summary">${older ? "<h4>Earlier attachment</h4>" : ""}${values}${doc.reviewed_at ? `<p class="review-origin">Reviewed ${esc(date(doc.reviewed_at))}</p>` : older ? '<p class="review-origin">Unreviewed</p>' : ""}</div>`;
  const editor = older ? `${confirmed}<button class="button quiet" type="button" data-use-latest>Use latest PO</button>` : doc.reviewed ? `<details class="case-disclosure reviewed-details"><summary>PO details</summary><div class="disclosure-body">${prepared ? confirmed : form + (doc.reviewed_at ? `<p class="review-origin">Reviewed ${esc(date(doc.reviewed_at))}</p>` : "")}</div></details>` : form;
  const issue = !older && doc.reviewed ? state.invoice.checks?.find(check => check.status === "fail" && check.document_id === doc.id) : null;
  const issueField = issue ? ({po_reference:"po_number",po_coverage:"amount",document_review:"po_number"})[issue.code] || issue.code : "";
  const highlight = issue && Number(issue.source_page || 1) === Number(page?.number || 1) ? doc.review_evidence?.[issueField]?.snippet || "" : "";
  const chooser = docs.length > 1 ? `<label class="sr-only" for="document-select">Choose purchase order</label><select id="document-select">${docs.map(item => `<option value="${esc(item.id)}" ${String(item.id) === String(doc.id) ? "selected" : ""}>${esc(item.filename)}${item.reviewed ? "" : " · Unreviewed"}</option>`).join("")}</select>` : `<strong class="document-name">${esc(doc.filename)}</strong>`;
  const format = /\.pdf$/i.test(doc.filename) ? "PDF" : /\.txt$/i.test(doc.filename) ? "TXT" : "FILE";
  const result = !older && doc.reviewed && (prepared || canApprove(state.invoice)) ? `<h3 class="review-result">${prepared ? "Approved purchase order" : "Ready for handoff"}</h3>` : "";
  const expand = `<button type="button" class="source-expand" data-source-expand aria-expanded="${Boolean(state.sourceExpanded)}" aria-controls="document-preview" aria-label="${state.sourceExpanded ? "Restore side-by-side review" : "Expand source text"}"><svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true"><path d="M7 3H3V7M13 3H17V7M3 13V17H7M17 13V17H13" stroke="currentColor" stroke-width="1.4"/></svg></button>`;
  return `<div class="evidence-layout review-desk${state.sourceExpanded ? " source-expanded" : ""}"><section class="document-preview" id="document-preview" aria-label="Original purchase order"><div class="document-tools"><span class="document-format" aria-hidden="true">${format}</span>${chooser}${expand}</div><div class="preview-heading"><span>Source text</span><button type="button" class="text-button" data-download-doc="${esc(doc.id)}">Download original <span aria-hidden="true">↓</span></button></div><pre class="document-text" tabindex="0" aria-label="Purchase order source text">${sourceMarkup(page?.text || "No readable text. Add a text PDF or TXT document.",highlight)}</pre><div class="preview-pagination" ${pages.length > 1 ? "" : "hidden"}><button type="button" class="text-button" data-page-step="-1" ${state.page === 0 ? "disabled" : ""}>Previous</button><span>Page ${esc(page?.number || 1)} of ${Math.max(1,pages.length)}</span><button type="button" class="text-button" data-page-step="1" ${state.page >= pages.length - 1 ? "disabled" : ""}>Next</button></div></section><div class="review-controls">${result}${doc.reviewed && !older ? reviewTools + editor : editor + (older ? "" : reviewTools)}</div></div>`;
}
function fieldEvidence(doc, name) {
  const item = (doc.reviewed ? doc.review_evidence : doc.evidence)?.[name];
  if (!item?.page || !item.snippet) return "";
  return `<button class="field-evidence" type="button" data-source="${esc(doc.id)}" data-field="${esc(name)}" data-page="${esc(item.page)}" title="${esc(item.snippet)}" aria-label="View ${esc(name.replace(/_/g," "))} source, page ${esc(item.page)}">Source · p. ${esc(item.page)}</button>`;
}
function sourceMarkup(text, snippet = "") {
  const source = String(text ?? ""), passage = typeof snippet === "string" ? snippet : "";
  if (!source) return "";
  const index = passage ? source.indexOf(passage) : -1, end = index < 0 ? -1 : index + passage.length;
  let offset = 0;
  return source.split("\n").map(line => {
    const first = Math.max(0,index - offset), last = Math.min(line.length,end - offset);
    const content = index >= 0 && first < last ? esc(line.slice(0,first)) + '<mark class="source-highlight">' + esc(line.slice(first,last)) + "</mark>" + esc(line.slice(last)) : esc(line);
    offset += line.length + 1;
    return `<span class="source-line">${content}</span>`;
  }).join("\n");
}
function requestHtml(invoice, primaryKind) {
  const request = invoice.request;
  if (!request) return '<p class="form-note">The request draft is unavailable. Refresh this invoice to try again.</p><button class="text-button" type="button" data-refresh-case>Refresh invoice</button>';
  return `<dl class="draft-meta"><dt>To</dt><dd>${esc(request.to)}</dd><dt>Subject</dt><dd>${esc(request.subject)}</dd></dl><pre class="draft-body">${esc(request.body)}</pre><div class="draft-actions">${primaryKind === "request" ? '' : '<button class="text-button" type="button" data-copy-request>Copy draft</button>'}<button class="text-button" type="button" data-download-request>Download .eml</button></div>`;
}
function updateInvoice(invoice, {followNext = false} = {}) {
  const oldIndex = state.rows.findIndex(item => String(item.id) === String(invoice.id));
  const fits = state.view === "all" || (state.view === "prepared" ? invoice.status === "prepared" : invoice.status !== "prepared");
  if (oldIndex >= 0 && fits) state.rows[oldIndex] = invoice;
  else if (oldIndex >= 0) {
    if (String(state.invoice?.id) === String(invoice.id)) { state.nextId = state.rows[oldIndex + 1]?.id ?? null; state.queueRemovedIndex = oldIndex; }
    state.rows.splice(oldIndex,1); state.total = Math.max(0,state.total - 1); state.queueNeedsRefill = true;
  }
  renderQueue();
  if (state.invoice && String(state.invoice.id) === String(invoice.id)) {
    const latest = invoice.documents?.[invoice.documents.length - 1];
    const newDocument = latest && !(state.invoice.documents || []).some(doc => String(doc.id) === String(latest.id));
    state.invoice = invoice;
    state.caseDirty = false;
    if (followNext || newDocument) state.caseTab = stageForNext(nextAction(invoice));
    if (newDocument) { state.docId = latest.id; state.page = 0; }
    renderCase();
  }
}
async function action(actionName, parameters) {
  const invoice = state.invoice;
  const body = {action:actionName,expected_version:invoice.version,parameters};
  const identity = `${invoice.id}:${JSON.stringify(body)}`;
  if (!state.operationKeys.has(identity)) state.operationKeys.set(identity, uid());
  body.idempotency_key = state.operationKeys.get(identity);
  const result = await api(`/api/invoices/${encodeURIComponent(invoice.id)}/actions`, {method:"POST",body});
  state.operationKeys.delete(identity);
  updateInvoice(result.invoice,{followNext:actionName !== "record_reply"});
  return result;
}
async function recoverError(error, errorId, request = null) {
  if (error.status === 401) return;
  const current = () => request && state.invoice === request.invoice && state.caseSequence === request.sequence && state.token === request.token;
  const keepEdits = () => {
    const message = `${error.message} Your edits were kept. ${errorId === "#confirm-error" ? "Close this dialog and review the latest invoice." : "Review the latest invoice before trying again."}`;
    setError(errorId,message);
    const target = $(errorId);
    if (target && errorId !== "#confirm-error") target.innerHTML = `${esc(message)} <button type="button" class="text-button" data-refresh-case>Review latest</button>`;
    notify(message,true);
  };
  if (request && !current()) return;
  if (error.status === 409 && request) {
    if (state.caseDirty || state.caseEdits !== request.edits) { keepEdits(); return; }
    let refreshed = false;
    try {
      const invoice = await api(`/api/invoices/${encodeURIComponent(request.invoice.id)}`);
      if (!current()) return;
      if (state.caseDirty || state.caseEdits !== request.edits) { keepEdits(); return; }
      updateInvoice(invoice); refreshed = true;
    }
    catch (refreshError) {
      if (refreshError.status === 401 || !current()) return;
      if (state.caseDirty || state.caseEdits !== request.edits) { keepEdits(); return; }
      notify(refreshError.message, true);
    }
    const message = `${error.message}${refreshed ? " The latest invoice is now shown for review." : ""}`;
    setError(errorId, message);
    notify(message, true);
  } else {
    setError(errorId, error.message);
    const target = $(errorId);
    if (!target || target.closest("dialog:not([open]),[hidden]")) notify(error.message, true);
  }
}
async function operation(form, errorId, task) {
  if (state.busy) return;
  if (form.closest("#case-content") && !confirmDraftDiscard(form)) return;
  const request = state.invoice && (form.closest("#case-content") || form.id === "confirm-form") ? {invoice:state.invoice,sequence:state.caseSequence,edits:state.caseEdits,token:state.token} : null;
  state.busy = true;
  setError(errorId);
  const controls = $$('button[type="submit"]', form);
  const controlLabels = controls.map(control => control.textContent);
  controls.forEach(control => { control.disabled = true; if (form.id === "upload-form") control.textContent = "Reading purchase order…"; });
  const pendingLabel = {"review-form":"Saving review…","proposal-form":"Saving correction…","reply-form":"Saving reply…","entry-form":"Adding invoice…","import-form":"Importing invoices…","confirm-form":({send_request:"Sending request…",apply_reference:"Updating reference…",reopen:"Reopening handoff…",approve_resolution:"Approving handoff…"})[state.confirmation?.kind]}[form.id];
  if (pendingLabel) controls.forEach(control => { control.textContent = pendingLabel; });
  form.setAttribute("aria-busy", "true");
  try { await task(); }
  catch (error) { await recoverError(error, errorId, request); }
  finally { state.busy = false; controls.forEach((control,index) => { control.disabled = false; control.textContent = controlLabels[index]; }); form.removeAttribute("aria-busy"); updateCaseNavigation(); }
}
function confirmDraftDiscard(submittingForm = null) {
  if (!state.caseDirty) return true;
  const otherEdits = !submittingForm || $$('form[data-dirty]', $("#case-content")).some(form => form !== submittingForm);
  return !otherEdits || window.confirm(submittingForm ? "Save this form and discard unfinished changes in other forms?" : "Reload this invoice and discard unfinished changes?");
}
function focusCurrentStage() {
  const tab = $("#tab-" + state.caseTab);
  tab?.focus({preventScroll:true});
  tab?.scrollIntoView({block:"nearest",behavior:"instant"});
}
function focusSection(selector, inputSelector) {
  const section = $(selector);
  if (!section) return;
  const panel = section.closest(".case-panel");
  if (panel) activateCaseTab(panel.id.replace("panel-", ""));
  let ancestor = section;
  while (ancestor && ancestor !== $("#case-content")) { if (ancestor.tagName === "DETAILS") ancestor.open = true; ancestor = ancestor.parentElement; }
  const target = $(inputSelector || "input,textarea,button", section);
  const disclosure = target?.closest("details");
  if (disclosure) disclosure.open = true;
  section.scrollIntoView({block:"start",behavior:"instant"});
  if (target) target.focus({preventScroll:true});
  else panel?.focus({preventScroll:true});
}
function showDocumentPage(doc, index, snippet = "") {
  const pages = doc.pages || [];
  state.page = Math.max(0, Math.min(index, Math.max(0, pages.length - 1)));
  $("#document-preview .document-text").innerHTML = sourceMarkup(pages[state.page]?.text || "No readable text was extracted. Add a readable PDF or TXT document.", snippet);
  $(".preview-pagination span").textContent = `Page ${pages[state.page]?.number || 1} of ${Math.max(1,pages.length)}`;
  $('[data-page-step="-1"]').disabled = state.page === 0;
  $('[data-page-step="1"]').disabled = state.page >= pages.length - 1;
  if (snippet) {
    const mark = $("#document-preview .source-highlight");
    mark?.scrollIntoView({block:"nearest",behavior:"instant"});
    window.InvoiceClearUI?.spotlight(mark);
  }
}
$("#case-content").addEventListener("input", event => {
  const form = event.target.closest("form");
  if (!form) return;
  state.caseDirty = true; state.caseEdits++; form.dataset.dirty = "true";
  if (form.id === "review-form") {
    const doc = state.invoice.documents.find(item => String(item.id) === String(state.docId));
    const changed = ["po_number","customer","currency","amount"].some(name => {
      const original = String(valueOf(doc?.suggested_fields?.[name])).trim();
      const current = form.elements[name].value.trim();
      return original && original !== (name === "currency" ? current.toUpperCase() : current);
    });
    const note = $("[data-correction-note]",form);
    if (note) { note.hidden = !changed; form.elements.note.required = changed; }
  }
});
$("#case-content").addEventListener("keydown", event => {
  const tab = event.target.closest("[data-case-tab]");
  if (!tab || !["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = ["intake","correspondence","review","handoff"], index = tabs.indexOf(tab.dataset.caseTab);
  activateCaseTab(tabs[event.key === "Home" ? 0 : event.key === "End" ? 3 : (index + (event.key === "ArrowRight" ? 1 : 3)) % 4], true);
});
$("#case-content").addEventListener("change", event => {
  if (event.target.id === "document-select") {
    if (!mayLeaveCase()) { event.target.value = state.docId; return; }
    state.docId = event.target.value; state.page = 0; state.sourceExpanded = false; state.caseDirty = false; renderCase(); $("#document-select").focus({preventScroll:true});
  }
});
$("#case-content").addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  if (button.hasAttribute("data-source-expand")) {
    state.sourceExpanded = !state.sourceExpanded;
    $(".review-desk")?.classList.toggle("source-expanded",state.sourceExpanded);
    button.setAttribute("aria-expanded",String(state.sourceExpanded));
    button.setAttribute("aria-label",state.sourceExpanded ? "Restore side-by-side review" : "Expand source text");
    return;
  }
  if (button.hasAttribute("data-investigate")) { await investigateCase(); return; }
  if (button.hasAttribute("data-investigation-source")) { showInvestigationSource(button.dataset.investigationSource); return; }
  if (button.hasAttribute("data-investigation-next")) {
    const run = state.invoice?.investigation?.latest;
    if (run?.status !== "completed" || investigationStale(run) || state.busy) return;
    const step = investigationSteps[run.next_step];
    if (!step) return;
    const stage = run.next_step === "verify_receipt" && ["pending","unknown"].includes(state.invoice.integration?.accounting?.action?.state) ? "intake" : step.stage;
    activateCaseTab(stage,true);
    return;
  }
  if (button.hasAttribute("data-copy-investigation")) {
    const run = state.invoice?.investigation?.latest;
    if (run?.status !== "completed" || investigationStale(run) || !run.draft) return;
    button.disabled = true;
    try { await copyText(run.draft); notify("Draft copied."); }
    catch { notify("Could not copy. Select the draft text instead.",true); }
    finally { button.disabled = false; }
    return;
  }
  if (button.dataset.caseTab) { activateCaseTab(button.dataset.caseTab); return; }
  if (button.hasAttribute("data-history-more")) { await loadHistory(true); return; }
  if (button.hasAttribute("data-history-retry")) { await loadHistory(Boolean(state.history?.events.length)); return; }
  if (button.hasAttribute("data-download-brief")) { await downloadBrief(button); return; }
  if (button.dataset.goStage) { activateCaseTab(button.dataset.goStage, true); return; }
  if (button.dataset.retryCase) { const savedScroll = state.queueScroll; await openInvoice(button.dataset.retryCase); state.queueScroll = savedScroll; }
  if (button.hasAttribute("data-refresh-case")) await refreshInvoice(button);
  if (button.dataset.next) {
    const kind = button.dataset.next;
    if (kind === "upload" || kind === "received") { focusSection("#evidence-section", "#document-file"); $("#document-file")?.click(); }
    if (kind === "review") { const docs = state.invoice.documents, doc = docs[docs.length - 1]; if (doc && String(state.docId) !== String(doc.id)) { if (!mayLeaveCase()) return; state.docId = doc.id; state.page = 0; state.caseDirty = false; renderCase(); } focusSection("#review-form"); }
    if (kind === "verify-send") verifyReceipt(button,"reconcile-request");
    if (kind === "verify-update") verifyReceipt(button,"reconcile-reference");
    if (kind === "replies") syncReplies(button);
    if (kind === "refresh") refreshAccounting(button);
    if (kind === "send") { focusSection("#request-section"); openConfirmation("send_request"); }
    if (kind === "request") { focusSection("#request-section", "[data-copy-request]"); await copyRequest(button); }
    if (kind === "correction") {
      const doc = state.invoice.documents[state.invoice.documents.length - 1];
      const form = $("#proposal-form");
      if (form && doc?.reviewed) {
        if (!form.dataset.dirty) {
          form.elements.po_reference.value = valueOf(doc.fields.po_number);
          form.elements.note.value = `Reviewed purchase order confirms ${valueOf(doc.fields.po_number)}; invoice records ${state.invoice.po_reference || "no PO reference"}.`;
          form.dataset.dirty = "true";
          state.caseDirty = true;
          state.caseEdits++;
        }
        focusSection("#proposal-section", '[name="po_reference"]');
      }
    }
    if (kind === "approve") openConfirmation("approve_resolution");
    if (kind === "handoff") downloadHandoff(button);
  }
  if (button.hasAttribute("data-reopen")) openConfirmation("reopen");
  if (button.hasAttribute("data-send-request")) openConfirmation("send_request");
  if (button.hasAttribute("data-apply-reference")) openConfirmation("apply_reference");
  if (button.hasAttribute("data-refresh-accounting")) refreshAccounting(button);
  if (button.hasAttribute("data-sync-replies")) syncReplies(button);
  if (button.hasAttribute("data-reconcile-request")) verifyReceipt(button,"reconcile-request");
  if (button.hasAttribute("data-reconcile-reference")) verifyReceipt(button,"reconcile-reference");
  if (button.hasAttribute("data-use-latest")) {
    if (!mayLeaveCase()) return;
    const docs = state.invoice.documents;
    state.docId = docs[docs.length - 1]?.id ?? null;
    state.page = 0; state.sourceExpanded = false; state.caseDirty = false; renderCase(); focusCurrentStage();
  }
  if (button.dataset.source) {
    const doc = state.invoice.documents.find(item => String(item.id) === String(button.dataset.source));
    if (!doc) return;
    if (String(state.docId) !== String(doc.id)) { if (!mayLeaveCase()) return; state.docId = doc.id; state.page = 0; state.sourceExpanded = false; state.caseDirty = false; renderCase(); }
    const pageIndex = doc.pages?.findIndex(page => Number(page.number) === Number(button.dataset.page));
    const evidence = doc.reviewed ? doc.review_evidence : doc.evidence;
    showDocumentPage(doc, pageIndex >= 0 ? pageIndex : 0, evidence?.[button.dataset.field]?.snippet || "");
    focusSection("#document-preview", ".document-text");
  }
  if (button.dataset.pageStep) { const doc = state.invoice.documents.find(item => String(item.id) === String(state.docId)); if (doc) showDocumentPage(doc, state.page + Number(button.dataset.pageStep)); }
  if (button.dataset.downloadDoc) {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Downloading…";
    const doc = state.invoice.documents.find(item => String(item.id) === String(button.dataset.downloadDoc));
    try { download(await api(`/api/documents/${encodeURIComponent(doc.id)}/download`, {blob:true}), doc.filename); notify("Original document download started."); }
    catch (error) { if (error.status !== 401) notify(error.message,true); }
    finally { button.disabled = false; button.textContent = label; }
  }
  if (button.hasAttribute("data-copy-request")) await copyRequest(button);
  if (button.hasAttribute("data-download-request")) {
    const request = state.invoice.request, clean = text => String(text || "").replace(/[\r\n]/g," ");
    const eml = `To: ${clean(request.to)}\r\nSubject: ${clean(request.subject)}\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n${request.body.replace(/\r?\n/g,"\r\n")}`;
    download(new Blob([eml], {type:"message/rfc822;charset=utf-8"}), `${state.invoice.invoice_number}-customer-request.eml`);
  }
});
$("#case-content").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.target, data = Object.fromEntries(new FormData(form));
  if (form.id === "review-form") operation(form,"#review-error",async () => {
    const doc = state.invoice.documents.find(item => String(item.id) === String(state.docId));
    const fields = {po_number:data.po_number.trim(),customer:data.customer.trim(),currency:data.currency.trim().toUpperCase(),amount:data.amount.trim()};
    const corrected = Object.keys(fields).some(key => { const original = String(valueOf(doc.suggested_fields?.[key])).trim(); return original && original !== fields[key]; });
    if (corrected && data.note.trim().length < 10) throw new ApiError("Add a correction note of at least 10 characters when changing an extracted value.");
    await action("review_document",{document_id:doc.id,fields,note:data.note.trim()});
    notify("PO review saved.");
    if (nextAction(state.invoice).kind === "correction") focusSection("#proposal-section", '[name="po_reference"]');
    else focusCurrentStage();
  });
  if (form.id === "proposal-form") operation(form,"#proposal-error",async () => {
    await action("propose_reference",{po_reference:data.po_reference.trim(),note:data.note.trim()});
    notify("PO correction saved.");
    focusCurrentStage();
  });
  if (form.id === "reply-form") operation(form,"#reply-error",async () => {
    await action("record_reply",{text:data.text.trim()});
    activateCaseTab("correspondence");
    notify("Customer reply recorded.");
  });
  if (form.id === "upload-form") operation(form,"#upload-error",async () => {
    const file = $("#document-file").files[0];
    if (!file || !/\.(pdf|txt)$/i.test(file.name)) throw new ApiError("Choose a PDF or TXT document.");
    if (file.size > 5 * 1024 * 1024) throw new ApiError("This document exceeds 5 MB. Choose a smaller file.");
    if (!file.size) throw new ApiError("This document is empty. Choose a readable PDF or TXT.");
    const invoice = state.invoice;
    const content = await fileBase64(file);
    const identity = `${invoice.id}:upload:${invoice.version}:${file.name}:${file.size}:${file.lastModified}`;
    if (!state.operationKeys.has(identity)) state.operationKeys.set(identity,uid());
    const updated = await api(`/api/invoices/${encodeURIComponent(invoice.id)}/documents`,{method:"POST",body:{filename:file.name,content_base64:content,expected_version:invoice.version,idempotency_key:state.operationKeys.get(identity)}});
    state.operationKeys.delete(identity);
    state.docId = updated.documents?.[updated.documents.length - 1]?.id;
    state.page = 0;
    state.sourceExpanded = false;
    updateInvoice(updated,{followNext:true});
    notify("Document added for review.");
    focusSection("#review-form");
  });
});

function openConfirmation(kind) {
  if (state.busy || !state.invoice || (kind === "approve_resolution" && !canApprove(state.invoice))) return;
  if (state.caseDirty) { notify("Save your unfinished changes before approving an action.", true); return; }
  const reopening = kind === "reopen", sending = kind === "send_request", applying = kind === "apply_reference";
  state.confirmation = {kind,id:state.invoice.id,version:state.invoice.version};
  $("#confirm-heading").textContent = sending ? "Send this request?" : applying ? "Update this PO reference?" : reopening ? "Reopen this handoff?" : "Approve this handoff?";
  $("#confirm-description").textContent = sending ? "Send this exact request from the connected Gmail account." : applying ? "Change only the approved PO reference in QuickBooks. Invoice amount and payment are not changed; the PO attachment remains in your handoff." : reopening ? "Return this prepared case to review and retain its previous approval record." : "No email is sent and QuickBooks is unchanged.";
  $("#confirm-summary").innerHTML = `<strong>${esc(state.invoice.invoice_number)} · ${esc(state.invoice.customer)}</strong>${esc(money(state.invoice))}<p>${esc(sending ? `To: ${state.invoice.request.to}` : handoffInstruction(state.invoice))}</p>${sending ? `<pre class="confirm-request">${esc(state.invoice.request.subject)}\n\n${esc(state.invoice.request.body)}</pre>` : ""}`;
  $("#confirm-note-label").hidden = sending || applying;
  $("#confirm-note-label").firstChild.textContent = reopening ? "Reason for reopening " : "Approval note ";
  $("#confirm-note-label .optional").textContent = reopening ? "required" : "optional";
  $("#confirm-note").value = "";
  $("#confirm-note").required = reopening;
  $("#confirm-submit").textContent = sending ? "Send through Gmail" : applying ? "Update PO reference" : reopening ? "Reopen for review" : "Approve handoff";
  setError("#confirm-error");
  $("#confirm-dialog").showModal();
  $(reopening ? "#confirm-note" : "#confirm-dialog [data-close]").focus();
}
$("#confirm-form").addEventListener("submit", event => {
  event.preventDefault();
  operation(event.target,"#confirm-error",async () => {
    const confirmation = state.confirmation;
    if (!state.invoice || state.invoice.id !== confirmation.id || state.invoice.version !== confirmation.version) throw new ApiError("The invoice changed. Close this dialog and review the latest version.");
    const note = $("#confirm-note").value.trim();
    if (confirmation.kind === "reopen" && !note) throw new ApiError("Add the reason for reopening this resolution.");
    if (confirmation.kind === "send_request" || confirmation.kind === "apply_reference") await externalAction(confirmation.kind === "send_request" ? "send-request" : "apply-reference");
    else await action(confirmation.kind,{note});
    $("#confirm-dialog").close();
    if (confirmation.kind === "approve_resolution" && state.invoice?.status === "prepared") window.InvoiceClearUI?.filed?.($(".handoff-sheet"));
    notify(confirmation.kind === "send_request" ? "Gmail confirmed the request was sent." : confirmation.kind === "apply_reference" ? "PO reference verified in QuickBooks." : confirmation.kind === "reopen" ? "Reopened for review." : "Handoff approved.");
    $("#case-view").focus({preventScroll:true});
  });
});
$$('[data-close]').forEach(button => button.addEventListener("click", () => $("#" + button.dataset.close).close()));
$("#entry-open").addEventListener("click", () => { setError("#entry-error"); $("#entry-dialog").showModal(); });
$("#import-open").addEventListener("click", () => {
  setError("#import-error");
  $("#source-files-download").hidden = state.runtime?.mode !== "demo";
  $("#import-dialog").showModal();
});
$("#source-files-download").addEventListener("click", async event => {
  if (state.runtime?.mode !== "demo") return;
  const button = event.currentTarget;
  button.disabled = true;
  setError("#import-error");
  try { download(await api("/api/public/source-files", {blob:true}), "invoiceclear-source-files.zip"); }
  catch (error) { if (error.status !== 401) setError("#import-error",error.message); }
  finally { button.disabled = false; }
});
$("#entry-form").addEventListener("submit", async event => {
  event.preventDefault();
  let createdId = null;
  await operation(event.target,"#entry-error",async () => {
    const data = Object.fromEntries(new FormData(event.target));
    Object.keys(data).forEach(key => { data[key] = data[key].trim(); });
    data.currency = data.currency.toUpperCase();
    const invoice = await api("/api/invoices",{method:"POST",body:data});
    createdId = invoice.id;
    $("#entry-dialog").close();
    event.target.reset();
    await loadQueue();
    notify("Invoice added.");
  });
  if (createdId) await openInvoice(createdId);
});
$("#import-form").addEventListener("submit", event => {
  event.preventDefault();
  operation(event.target,"#import-error",async () => {
    const file = $("#import-file").files[0];
    if (!file || !/\.csv$/i.test(file.name)) throw new ApiError("Choose a CSV file using the invoice template.");
    if (file.size > 1024 * 1024) throw new ApiError("Choose a CSV file smaller than 1 MB.");
    const result = await api("/api/invoices/import",{method:"POST",body:{csv:await file.text()}});
    $("#import-dialog").close();
    event.target.reset();
    await loadQueue();
    notify(`${result.created} invoice${result.created === 1 ? "" : "s"} imported${result.duplicates ? `; ${result.duplicates} existing duplicate${result.duplicates === 1 ? "" : "s"} skipped` : ""}.`);
  });
});
$("#template-download").addEventListener("click", () => {
  const csv = "invoice_number,customer,customer_email,amount,currency,issue_date,due_date,po_reference,source_ref\r\nSTA-2026-2101,Mercer Valley Home Health LLC,ap@mercervalleyhomehealth.example,18450.00,USD,2026-08-16,2026-09-15,MVH-2026-2181,HB-20260816-US / MVH-04018 / Columbus East / Clinical float pool / Net 30\r\n";
  download(new Blob([csv],{type:"text/csv;charset=utf-8"}),"invoiceclear-import-template.csv");
});
async function externalAction(route) {
  const invoice = state.invoice;
  const body = {expected_version:invoice.version,parameters:{}};
  const identity = `${invoice.id}:${route}:${invoice.version}`;
  if (!state.operationKeys.has(identity)) state.operationKeys.set(identity,uid());
  body.idempotency_key = state.operationKeys.get(identity);
  const result = await api(`/api/invoices/${encodeURIComponent(invoice.id)}/${route}`,{method:"POST",body});
  state.operationKeys.delete(identity);
  if (state.invoice?.id === invoice.id) updateInvoice(result.invoice,{followNext:true});
  return result;
}
function applyCaseRefresh(invoice, request, quiet = false) {
  if (state.invoice !== request.invoice || state.caseSequence !== request.sequence) return false;
  if (state.busy || state.caseEdits !== request.edits) {
    if (!quiet) notify("Update received. Your edits were kept; refresh after saving.");
    return false;
  }
  if (quiet && state.replyPollError === request.replyError && JSON.stringify(invoice) === JSON.stringify(request.invoice)) return false;
  updateInvoice(invoice);
  return true;
}
async function refreshInvoice(button, route = "", message = "") {
  if (!state.invoice || state.busy) return;
  if (!confirmDraftDiscard()) return;
  const request = {invoice:state.invoice,sequence:state.caseSequence,edits:state.caseEdits,replyError:state.replyPollError};
  button.disabled = true;
  try {
    const result = await api(`/api/invoices/${encodeURIComponent(request.invoice.id)}${route ? "/" + route : ""}`,route ? {method:"POST",body:{}} : {});
    if (applyCaseRefresh(route ? result.invoice : result,request) && message) notify(message);
  }
  catch (error) { if (error.status !== 401 && state.invoice === request.invoice && state.caseSequence === request.sequence) notify(error.message,true); }
  finally { button.disabled = false; }
}
async function verifyReceipt(button, route) {
  await refreshInvoice(button,route,"Provider receipt verified.");
}
async function refreshAccounting(button) {
  await refreshInvoice(button,"refresh-accounting");
}
async function syncReplies(button, quiet = false) {
  if (!state.invoice || state.busy || state.replyPolling || (quiet && state.caseDirty)) return;
  if (!quiet && !confirmDraftDiscard()) return;
  state.replyPolling = true;
  const request = {invoice:state.invoice,sequence:state.caseSequence,edits:state.caseEdits,replyError:state.replyPollError};
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/invoices/${encodeURIComponent(request.invoice.id)}/sync-replies`,{method:"POST",body:{}});
    if (state.invoice !== request.invoice || state.caseSequence !== request.sequence) return;
    state.replyPollError = result.failures?.map(failure => failure.reason).join(" ") || "";
    if (applyCaseRefresh(result.invoice,request,quiet) && !quiet) {
      if (state.replyPollError) notify(state.replyPollError,true);
      else notify(`${result.added || 0} new repl${result.added === 1 ? "y" : "ies"}${result.documents_added ? `; ${result.documents_added} document(s) added for review` : ""}.`);
    }
  } catch (error) { if (error.status !== 401 && state.invoice === request.invoice && state.caseSequence === request.sequence) { state.replyPollError = error.message; if (!quiet) notify(error.message,true); else if (!state.caseDirty && !state.busy && state.caseEdits === request.edits && request.replyError !== state.replyPollError) renderCase(); } }
  finally { state.replyPolling = false; if (button) button.disabled = false; }
}
async function loadConnections({readiness = false} = {}) {
  const token = state.token, sequence = (state.connectionsSequence || 0) + 1;
  state.connectionsSequence = sequence;
  const results = await Promise.all([api("/api/connections"), api("/api/capture"), ...(readiness ? [api("/api/readiness")] : [])]);
  if (sequence !== state.connectionsSequence || token !== state.token) return;
  state.connections = results[0];
  state.capture = results[1];
  if (readiness) state.readiness = results[2];
  renderConnections();
  if (state.invoice && !state.caseDirty) renderCase();
}
function connectionSetupHtml(provider, item) {
  const labels = {qbo:"QuickBooks",gmail:"Gmail",together:"TogetherAI"};
  const guide = provider === "qbo" ? "https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0" : provider === "gmail" ? "https://developers.google.com/identity/protocols/oauth2/web-server" : "https://docs.together.ai/docs/quickstart";
  const note = provider === "qbo" ? "Create an Intuit app and connect your company. Invoice sync also needs the PO custom-field DefinitionId." : provider === "gmail" ? "Enable Gmail API, create a web OAuth client and allow your test mailbox before connecting." : "Add the backend key to enable Investigate. Document extraction can stay on rules.";
  const environmentNames = [...new Set([...(item.required_environment || []), ...(provider === "qbo" ? ["QBO_ENVIRONMENT","QBO_PO_FIELD_ID"] : provider === "together" ? ["TOGETHER_API_KEY","TOGETHER_MODEL","INVOICECLEAR_AGENT_ENABLED"] : [])])];
  return `<details class="connection-setup"><summary>${labels[provider]} setup</summary><div class="connection-setup-body"><p>${note}</p>${item.callback_url ? `<div class="callback-address"><span>OAuth callback</span><code>${esc(item.callback_url)}</code></div>` : provider !== "together" ? '<p>Set the backend’s public origin to generate the exact callback.</p>' : ""}<ul class="environment-list">${environmentNames.map(name=>`<li><code>${esc(name)}</code>${name === "QBO_PO_FIELD_ID" && !item.po_field_configured ? "<span>Needed for sync</span>" : (item.missing_environment || []).includes(name) ? "<span>Required</span>" : ""}</li>`).join("")}</ul><a class="text-button" href="${guide}" target="_blank" rel="noopener noreferrer">Provider setup <span aria-hidden="true">↗</span></a></div></details>`;
}
function renderConnections() {
  const ready = state.readiness, connections = state.connections;
  $("#connections-setup-download").disabled = !ready;
  if (!ready || !connections) { $("#connections-content").innerHTML = '<p role="status">Loading connections…</p>'; return; }
  const privateAccess = ready.mode === "private" && ready.external_checks_allowed;
  const rows = ["qbo","gmail","together"].map(provider => {
    const item = ready.providers?.[provider] || {}, account = provider === "together" ? item : connections[provider] || item;
    const label = {qbo:"QuickBooks Online",gmail:"Gmail",together:"TogetherAI"}[provider];
    const check = item.verification || {}, checked = check.status === "passed";
    const connected = Boolean(account.connected), configured = Boolean(item.configured);
    const canVerify = privateAccess && configured && (provider === "together" ? item.selected : connected);
    let description = !privateAccess && provider !== "together" ? "Not connected" : provider === "together" && !item.selected ? "Rules extraction selected" : !configured ? "Backend setup needed" : connected && provider !== "together" ? account.account || "Account connected" : checked ? "Extraction checked" : provider === "together" ? "Not checked" : "Not connected";
    if (provider === "together" && ready.investigation) description += ` · ${ready.investigation.available ? "Investigation available" : ready.investigation.enabled ? "Investigation needs setup" : "Investigation off"}`;
    return `<section class="connection-item"><div class="connection-row"><div><h3>${label}</h3><p>${esc(description)}${connected && provider === "qbo" ? ` · ${esc(account.environment === "sandbox" ? "Sandbox" : "Production")}` : ""}</p>${provider === "together" && item.selected && item.model ? `<small class="connection-model">${esc(item.model)}</small>` : ""}${provider === "qbo" && connected && !item.po_field_configured ? '<p class="field-error">Choose the invoice PO custom field before syncing.</p>' : ""}${check.checked_at ? `<p class="${checked ? "connection-checked" : "field-error"}">${esc(check.message)}<time datetime="${esc(check.checked_at)}">${esc(timestamp(check.checked_at))} · this server session</time></p>` : ""}${account.error ? `<p class="field-error">${esc(account.error)}</p>` : ""}</div><div class="connection-actions">${privateAccess && provider !== "together" && !connected && configured ? `<button type="button" class="button quiet" data-connect-provider="${provider}">Connect</button>` : ""}${canVerify ? `<button type="button" class="button quiet" data-verify-provider="${provider}">${provider === "together" ? "Check extraction" : "Check access"}</button>` : ""}${privateAccess && provider !== "together" && connected ? `<button type="button" class="text-button" data-disconnect-provider="${provider}">Disconnect</button>` : ""}</div></div>${connectionSetupHtml(provider,item)}</section>`;
  }).join("");
  const capture = captureHtml(privateAccess);
  const database = ready.storage?.database, backups = ready.storage?.backups, usage = ready.usage || {};
  const storage = database?.status === "ok" ? "Available" : database?.error || "Needs attention";
  const backup = backups?.enabled ? ({ok:"Local snapshot available",pending:"First local snapshot pending",degraded:"Snapshot needs attention"})[backups.status] || "Snapshot needs attention" : "Local backups off";
  $("#connections-content").innerHTML = `${!privateAccess ? '<p class="connection-scope">Account connections require a private workspace.</p>' : ""}${rows}${capture}${privateAccess && connections.qbo?.connected && ready.providers?.qbo?.po_field_configured ? `<div class="connection-sync"><button type="button" class="button primary" data-sync-invoices>${state.syncOffset ? "Sync next batch" : "Sync invoices"}</button><p id="sync-result" role="status"></p></div>` : ""}<dl class="workspace-support"><div><dt>Files</dt><dd>Text PDF or TXT · 5 MB · 20 PDF pages. Image-only scans need readable text.</dd></div><div><dt>Scope</dt><dd>One workspace. Approval prepares billing; it does not record payment.</dd></div><div><dt>Storage</dt><dd>${esc(storage)} · ${esc(backup)}. Keep an off-site backup.${backups?.error ? `<span class="field-error">${esc(backups.error)}</span>` : ""}</dd></div>${ready.providers?.together?.selected || ready.investigation?.enabled ? `<div><dt>Model usage</dt><dd>${Number.isInteger(usage.calls) ? `${usage.calls} of ${esc(usage.call_limit)} model calls today` : "Usage unavailable"}<span>${Number.isInteger(usage.tokens) ? `${usage.tokens.toLocaleString()} of ${Number(usage.token_limit).toLocaleString()} tokens accounted. ` : ""}Includes conservative reservations, not a billing total.</span></dd></div>` : ""}</dl><details class="connection-release"><summary>Before public account onboarding</summary><p>Google may require verification for Gmail read access. Intuit production access is separate from sandbox access. Account consent and live checks are still required.</p><p><a href="https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification" target="_blank" rel="noopener noreferrer">Google requirements ↗</a> <a href="https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0" target="_blank" rel="noopener noreferrer">Intuit requirements ↗</a></p></details>`;
}
function captureHtml(privateAccess) {
  if (!privateAccess) return "";
  const capture = state.capture;
  if (!capture) return '<section class="connection-item"><div class="connection-row"><h3>Inbox capture</h3><p>Unavailable. Check again.</p></div></section>';
  return `<section class="connection-item"><div class="connection-row"><div><h3>Inbox capture</h3><p>${capture.enabled ? capture.running ? "Checking request threads…" : "On · pending request threads" : "Off"}</p>${!capture.available ? '<p>Connect QuickBooks and Gmail to start.</p>' : ""}${capture.last_error ? `<p class="field-error">${esc(capture.last_error)}</p>` : ""}</div><button type="button" class="button quiet" data-capture-enabled="${capture.enabled ? "false" : "true"}" ${!capture.enabled && !capture.available ? "disabled" : ""}>${capture.enabled ? "Stop capture" : "Start capture"}</button></div>${capture.last_run_at ? `<details class="connection-setup"><summary>Last capture</summary><div class="connection-setup-body"><p><time datetime="${esc(capture.last_run_at)}">${esc(timestamp(capture.last_run_at))}</time></p><p>${Number(capture.checked) || 0} checked · ${Number(capture.replies_added) || 0} replies · ${Number(capture.documents_added) || 0} documents</p>${capture.pending_cases ? `<p>${Number(capture.pending_cases)} held for receipt verification.</p>` : ""}${(capture.failures || []).map(item=>`<p class="field-error">${esc(item.invoice_number)} — ${esc(item.error)}</p>`).join("")}</div></details>` : ""}</section>`;
}
async function setCapture(enabled) {
  if (state.readiness?.mode !== "private" || !state.readiness.external_checks_allowed) throw new ApiError("Inbox capture requires a private workspace.");
  if (enabled && !window.confirm(`Check pending request threads in the background? New PO attachments will be extracted for review.${state.readiness.providers?.together?.selected ? " Attachments will be sent to TogetherAI and may incur provider charges." : ""} Stopping leaves any current case check to finish.`)) return;
  state.capture = await api("/api/capture",{method:"POST",body:{enabled}});
  renderConnections();
}
function timestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "Unknown date" : parsed.toLocaleString("en",{month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"});
}
function downloadSetup() {
  const ready = state.readiness;
  if (!ready) return;
  const qbo = ready.providers?.qbo || {}, gmail = ready.providers?.gmail || {};
  const backendOrigin = qbo.callback_url ? new URL(qbo.callback_url).origin : base || window.location.origin;
  const lines = ["InvoiceClear — connection setup", "", "Origins below describe this installation. For deployment, replace loopback URLs with your Railway backend and Vercel frontend HTTPS origins, then register matching OAuth callbacks.", "", "BACKEND · Railway variables", "INVOICECLEAR_MODE=private", "RAILWAY_RUN_UID=0", "INVOICECLEAR_EMAIL=", "INVOICECLEAR_PASSWORD=", "INVOICECLEAR_DB_PATH=/data/invoiceclear-private.sqlite3", `INVOICECLEAR_ALLOWED_ORIGINS=${window.location.origin}`, `INVOICECLEAR_PUBLIC_URL=${backendOrigin}`, `INVOICECLEAR_FRONTEND_URL=${window.location.origin}`, "INVOICECLEAR_TOKEN_KEY=", "INVOICECLEAR_BACKUPS_ENABLED=true", `INVOICECLEAR_PROVIDER=${ready.providers?.together?.selected ? "together" : "rules"}`, ...(ready.providers?.together?.selected ? ["TOGETHER_API_KEY=", `TOGETHER_MODEL=${ready.providers.together.model || ""}`] : ["# Add the key to enable Investigate. Choose together only for model-based extraction.", "# TOGETHER_API_KEY=", `# TOGETHER_MODEL=${ready.providers?.together?.model || ""}`]), "QBO_CLIENT_ID=", "QBO_CLIENT_SECRET=", "QBO_ENVIRONMENT=sandbox", "QBO_PO_FIELD_ID=", "GOOGLE_CLIENT_ID=", "GOOGLE_CLIENT_SECRET=", "", "FRONTEND · Vercel build variable", `INVOICECLEAR_API_BASE_URL=${backendOrigin}`, "", "Mount a persistent backend volume at /data. Use one backend replica.", "The image launcher prepares the root-owned mount, then starts the API as UID/GID 10001.", "Keep the default image start command; do not bypass the launcher.", "Use a unique workspace password of at least 16 characters.", "Generate one Fernet token-encryption key and retain it securely with recovery credentials.", "Configure these values on the backend, not in browser fields or frontend code.", "This template contains no keys. The app does not load this file automatically.", "", "OAUTH CALLBACKS", qbo.callback_url || `${backendOrigin}/api/connections/qbo/callback`, gmail.callback_url || `${backendOrigin}/api/connections/gmail/callback`, "", "Connect the company/mailbox with provider consent, then use Check access.", "Check extraction sends a fixed non-customer PO to TogetherAI and may incur a small provider charge.", "Checks do not send email, import invoices, update accounting or approve a handoff.", "Google and Intuit public onboarding requirements apply separately.", "", "RELEASE CHECK", "Run make -C backend release. Check /api/health after deploying.", "Exercise invoice intake, a wrong-reference correction, an insufficient-coverage hold and the downloaded billing brief.", "Verify a permitted Gmail thread and separately approved QuickBooks PO update in a private workspace.", "Confirm restart-safe sign-in, retained records and off-site backup recovery before client use."];
  const keepInvestigationOff = ready.providers?.together?.selected && ready.investigation?.enabled === false;
  lines.splice(lines.indexOf("QBO_CLIENT_ID="),0,"# The backend key enables Investigate unless explicitly disabled.",`${keepInvestigationOff ? "" : "# "}INVOICECLEAR_AGENT_ENABLED=false`);
  if (ready.investigation?.enabled) {
    const keyLine = lines.indexOf("# TOGETHER_API_KEY=");
    const modelLine = lines.findIndex(line => line.startsWith("# TOGETHER_MODEL="));
    if (keyLine >= 0) lines[keyLine] = "TOGETHER_API_KEY=";
    if (modelLine >= 0) lines[modelLine] = `TOGETHER_MODEL=${ready.investigation.model || ready.providers?.together?.model || ""}`;
  }
  download(new Blob([lines.join("\n")+"\n"],{type:"text/plain;charset=utf-8"}),"invoiceclear-connection-setup.txt");
}
async function openConnections() {
  if (state.runtime?.mode !== "private") return;
  setError("#connections-error"); $("#connections-dialog").showModal();
  $("#connections-content").innerHTML = '<p role="status">Loading connections…</p>';
  try { await loadConnections({readiness:true}); } catch (error) { if (error.status !== 401) setError("#connections-error",error.message); }
}
$("#connections-open").addEventListener("click",openConnections);
$("#connections-refresh").addEventListener("click", async event => {
  event.currentTarget.disabled = true; setError("#connections-error");
  try { await loadConnections({readiness:true}); } catch (error) { if (error.status !== 401) setError("#connections-error",error.message); }
  finally { event.currentTarget.disabled = false; }
});
$("#connections-setup-download").addEventListener("click",downloadSetup);
$("#connections-content").addEventListener("click", async event => {
  const button = event.target.closest("button"); if (!button || button.disabled) return;
  button.disabled = true; setError("#connections-error");
  try {
    if (button.hasAttribute("data-capture-enabled")) { await setCapture(button.dataset.captureEnabled === "true"); return; }
    if ((button.dataset.connectProvider || button.dataset.disconnectProvider) && !confirmDraftDiscard()) return;
    if (button.dataset.verifyProvider) {
      if (button.dataset.verifyProvider === "together" && !window.confirm("Run one extraction check with a fixed, non-customer PO? This uses your TogetherAI account and may incur a provider charge.")) return;
      let issue;
      try { await api(`/api/connections/${button.dataset.verifyProvider}/verify`,{method:"POST",body:{}}); } catch(error) { issue = error; }
      if (issue?.status === 401) throw issue;
      await loadConnections({readiness:true});
      if (issue) throw issue;
    } else if (button.dataset.connectProvider) {
      const result = await api(`/api/connections/${button.dataset.connectProvider}/start`,{method:"POST",body:{}});
      const url = new URL(result.url);
      if (url.protocol !== "https:" || !["appcenter.intuit.com","accounts.google.com"].includes(url.hostname)) throw new ApiError("The provider returned an invalid authorization address.");
      window.location.assign(url.href);
    } else if (button.dataset.disconnectProvider) {
      if (!window.confirm("Disconnect this account? Stored invoices, approvals and messages will remain.")) return;
      state.connections = await api(`/api/connections/${button.dataset.disconnectProvider}/disconnect`,{method:"POST",body:{}});
      state.syncOffset = 0; await loadConnections({readiness:true});
    } else if (button.hasAttribute("data-sync-invoices")) {
      const result = await api("/api/connections/qbo/sync",{method:"POST",body:{offset:state.syncOffset}});
      state.syncOffset = result.has_more ? result.next_offset : 0;
      $("#sync-result").textContent = `${result.created} added · ${result.refreshed} refreshed · ${Array.isArray(result.skipped) ? result.skipped.length : result.skipped} skipped${result.has_more ? ". More invoices available." : ""}`;
      if (Array.isArray(result.skipped) && result.skipped.length) $("#sync-result").textContent += " " + [...new Set(result.skipped.map(item => item.reason))].join(" ");
      button.textContent = result.has_more ? "Sync next batch" : "Sync invoices";
      await loadQueue();
    }
  } catch (error) { if (error.status !== 401) setError("#connections-error",error.message); }
  finally { button.disabled = false; }
});
async function pollWorkspace() {
  if (document.hidden || !state.token || state.runtime?.mode !== "private" || state.busy || state.caseDirty || state.capturePolling || state.replyPolling) return;
  state.capturePolling = true;
  const token = state.token;
  try {
    const capture = await api("/api/capture");
    if (token !== state.token) return;
    state.capture = capture;
    if ($("#connections-dialog")?.open) renderConnections();
    if (state.busy || state.caseDirty) return;
    if (capture.enabled) {
      // The server owns provider polling; the browser refreshes only local records.
      if (state.caseOpen && state.invoice) {
        const request = {invoice:state.invoice,sequence:state.caseSequence,edits:state.caseEdits,replyError:state.replyPollError};
        const invoice = await api(`/api/invoices/${encodeURIComponent(request.invoice.id)}`);
        if (token === state.token) applyCaseRefresh(invoice,request,true);
      } else if (!document.activeElement?.closest?.("[data-invoice], #queue-pagination")) await loadQueue({quiet:true});
    } else if (state.caseOpen && state.connections?.gmail?.connected && state.invoice?.integration?.messages?.some(message => message.direction === "outgoing" && message.status === "sent")) await syncReplies(null,true);
  } catch (error) { if (error.status !== 401 && token === state.token && $("#connections-dialog")?.open) setError("#connections-error",error.message); }
  finally { state.capturePolling = false; }
}
setInterval(pollWorkspace,60000);
async function copyRequest(button) {
  const request = state.invoice?.request;
  if (!request) return notify("Refresh the invoice to load its request draft.",true);
  button.disabled = true;
  try { await copyText(`To: ${request.to}\nSubject: ${request.subject}\n\n${request.body}`); notify("Request copied."); }
  catch { notify("The draft could not be copied. Download the .eml file instead.",true); }
  finally { button.disabled = false; }
}
async function downloadHandoff(button) {
  const invoice = state.invoice;
  button.disabled = true;
  button.setAttribute("aria-busy","true");
  try { download(await api(`/api/invoices/${encodeURIComponent(invoice.id)}/handoff`,{blob:true}),`${invoice.invoice_number}-billing-handoff.zip`); notify("Billing handoff downloaded."); }
  catch (error) { if (error.status !== 401) notify(error.message,true); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); if (button.isConnected) button.focus({preventScroll:true}); }
}
async function exportPrepared(button) {
  if (button.disabled) return;
  button.disabled = true;
  try { download(await api("/api/export.csv",{blob:true}),"invoiceclear-prepared-resolutions.csv"); notify("Prepared resolutions exported."); }
  catch (error) { if (error.status !== 401) notify(error.message,true); }
  finally { button.disabled = false; }
}
$("#export").addEventListener("click", event => exportPrepared(event.currentTarget));
function download(blob, filename) {
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url;
  link.download = filename.replace(/[\\/\x00-\x1f]/g,"_");
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}
function fileBase64(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new ApiError("The document could not be read. Choose it again."));
    reader.readAsDataURL(file);
  });
}
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return; }
    catch { /* Use the selection fallback when browser clipboard access is unavailable. */ }
  }
  const active = document.activeElement, input = document.createElement("textarea");
  input.value = text;
  input.className = "sr-only";
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  active?.focus({preventScroll:true});
  if (!copied) throw new Error("Copy failed");
}
window.InvoiceClearUI?.init({
  async onFind() {
    if (state.busy || (state.caseOpen && !await returnToQueue())) return;
    $("#queue-search").focus({preventScroll:true});
    $("#queue-search").select();
  },
  onFiles(files,form,dropped) {
    if (state.busy) return;
    const input = $("#document-file",form), message = window.InvoiceClearUI.fileError(files);
    if (message) { setError("#upload-error",message); if (!dropped) input.value = ""; return; }
    if (dropped) {
      try { const selection = new DataTransfer(); selection.items.add(files[0]); input.files = selection.files; }
      catch { setError("#upload-error","Choose the document using the file picker."); return; }
    }
    setError("#upload-error");
    state.caseDirty = true; state.caseEdits++; form.dataset.dirty = "true";
    if (dropped) $('button[type="submit"]',form)?.focus({preventScroll:true});
  }
});
runtime();
