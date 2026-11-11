import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createContext, runInContext} from 'node:vm';

const modulePath = process.argv[2] || new URL('../web/interactions.js', import.meta.url);
const source = await readFile(modulePath, 'utf8');

class ClassList {
  values = new Set();
  add(...values) { values.forEach(value => this.values.add(value)); }
  remove(...values) { values.forEach(value => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const next = force ?? !this.values.has(value);
    if (next) this.values.add(value); else this.values.delete(value);
    return next;
  }
}

function harness({reduced = false} = {}) {
  const listeners = new Map(), nodes = new Map(), animations = [];
  const addListener = (map, name, listener) => {
    const values = map.get(name) || []; values.push(listener); map.set(name, values);
  };
  class Element {
    constructor(id = '', tag = 'DIV') {
      Object.assign(this, {id, tagName:tag, parentElement:null, dataset:{}, hidden:false, disabled:false,
        open:false, textContent:'', value:'', files:[], isConnected:true, children:[], attributes:new Map(),
        listeners:new Map(), classList:new ClassList(), style:{setProperty() {}, removeProperty() {}},
        activeAnimations:[], submitCalls:0, requestSubmitCalls:0});
    }
    addEventListener(name, listener) { addListener(this.listeners, name, listener); }
    removeEventListener(name, listener) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== listener)); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    hasAttribute(name) { return this.attributes.has(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    append(...children) { children.forEach(child => {child.parentElement = this; this.children.push(child);}); }
    contains(element) { return element === this || this.children.some(child => child.contains(element)); }
    matches(selector) {
      return selector.split(',').some(part => {
        const item = part.trim();
        if (item === `#${this.id}`) return true;
        if (item === this.tagName.toLowerCase()) return true;
        if (item === 'dialog[open]') return this.tagName === 'DIALOG' && this.open;
        if (item === '[hidden]') return this.hidden;
        if (item === 'input[type="file"]' || item === 'input[type=file]') return this.tagName === 'INPUT' && this.type === 'file';
        return item.startsWith('.') && this.classList.contains(item.slice(1));
      });
    }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    querySelector(selector) { return this.children.find(child => child.matches(selector)) || this.children.map(child => child.querySelector(selector)).find(Boolean) || null; }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    animate(frames, options) {
      const animation = {frames, options, cancelled:false, playState:'running', finished:Promise.resolve(),
        cancel() { this.cancelled = true; this.playState = 'idle'; }};
      this.activeAnimations.push(animation); animations.push(animation); return animation;
    }
    getAnimations() { return this.activeAnimations.filter(animation => !animation.cancelled); }
    focus() { document.activeElement = this; }
    scrollIntoView() {}
    submit() { this.submitCalls++; }
    requestSubmit() { this.requestSubmitCalls++; }
  }
  const get = (id, tag) => {
    if (!nodes.has(id)) nodes.set(id, new Element(id, tag));
    return nodes.get(id);
  };
  const document = {
    hidden:false, activeElement:null,
    addEventListener(name, listener) { addListener(listeners, name, listener); },
    removeEventListener(name, listener) { listeners.set(name, (listeners.get(name) || []).filter(item => item !== listener)); },
    querySelector(selector) {
      if (selector === 'dialog[open]' || selector === 'dialog:modal') return [...nodes.values()].find(item => item.tagName === 'DIALOG' && item.open) || null;
      if (selector.startsWith('#')) return nodes.get(selector.slice(1)) || null;
      return [...nodes.values()].find(item => item.matches(selector)) || null;
    },
    querySelectorAll(selector) { return [...nodes.values()].filter(item => item.matches(selector)); }
  };
  document.body = get('body', 'BODY');
  const shell = get('app-shell'), caseContent = get('case-content');
  const form = get('upload-form', 'FORM'), fileInput = get('document-file', 'INPUT'); fileInput.type = 'file';
  document.body.append(shell); shell.append(caseContent); caseContent.append(form); form.append(fileInput);
  document.body.append(get('notice'), get('notice-text'), get('access-view'), get('confirm-dialog', 'DIALOG'));
  const media = {matches:reduced, addEventListener:(name,listener) => addListener(listeners, `media-${name}`, listener)};
  const window = {matchMedia:() => media, addEventListener:(...args) => document.addEventListener(...args),
    removeEventListener:(...args) => document.removeEventListener(...args), setTimeout, clearTimeout};
  let networkCalls = 0;
  const context = createContext({window, document, navigator:{platform:'MacIntel'}, Element, HTMLElement:Element, HTMLInputElement:Element,
    setTimeout, clearTimeout, requestAnimationFrame:callback => callback(),
    fetch() { networkCalls++; throw new Error('Interaction helpers must not request data'); },
    console, Event:class Event {}, DataTransfer:class DataTransfer {
      constructor() { this.files=[]; this.items={add:file => this.files.push(file)}; }
    }});
  runInContext(source, context, {filename:String(modulePath)});
  const api = window.InvoiceClearUI;
  assert.ok(api, 'Module exposes window.InvoiceClearUI');
  const event = (type, target = fileInput, extra = {}) => ({type, target, currentTarget:document, defaultPrevented:false,
    preventDefault() { this.defaultPrevented=true; }, stopPropagation() {}, ...extra});
  const fire = async evt => {
    for (let element = evt.target; element; element = element.parentElement) {
      for (const listener of element.listeners.get(evt.type) || []) await listener(evt);
    }
    for (const listener of listeners.get(evt.type) || []) await listener(evt);
    for (let index=0; index<4; index++) await Promise.resolve();
    return evt;
  };
  const setReduced = value => { media.matches=value; (listeners.get('media-change') || []).forEach(listener => listener()); };
  return {api, context, document, window, get, form, fileInput, shell, animations, event, fire, setReduced, networkCalls:() => networkCalls};
}

let passed=0;
async function test(name, task) { await task(); passed++; process.stdout.write(`PASS ${name}\n`); }
const validFile = (name='purchase-order.pdf', size=1024) => ({name, size, type:name.endsWith('.txt') ? 'text/plain' : 'application/pdf', lastModified:1});
const noFailure = task => assert.doesNotThrow(task);

await test('file validation accepts only one nonempty PDF or TXT within five MiB', () => {
  const {api} = harness();
  for (const file of [validFile(), validFile('PO.PDF'), validFile('po.txt'), validFile('PO.TXT',5*1024*1024)]) assert.equal(api.fileError([file]),'');
  for (const files of [undefined, null, [], [validFile(),validFile('another.txt')], [validFile('po.png')], [validFile('po.pdf.exe')],
    [validFile('po.pdf',0)], [validFile('po.txt',5*1024*1024+1)]]) {
    assert.ok(typeof api.fileError(files)==='string' && api.fileError(files).length>0);
  }
});

await test('native motion is finite and rapid reveals cancel an earlier reveal', () => {
  const h = harness(), element=h.get('motion-target');
  h.api.reveal(element); h.api.reveal(element,-1);
  assert.ok(h.animations.length>=2, 'Supported reveal animates each explicit change');
  assert.equal(h.animations[0].cancelled,true, 'A newer reveal cancels its predecessor');
  h.api.spotlight(element);
  for (const animation of h.animations) {
    assert.ok(Number.isFinite(animation.options.duration) && animation.options.duration>0 && animation.options.duration<=1000);
    assert.ok(animation.options.iterations===undefined || animation.options.iterations===1, 'Motion never loops');
  }
});

await test('stage transitions cancel on tab change, reduced motion, and hidden page', async () => {
  const h = harness(), source = h.get('panel-source'), review = h.get('panel-review');
  h.api.init();
  h.api.stage(source); h.api.stage(review);
  assert.equal(h.animations[0].cancelled,true);
  assert.equal(h.animations[1].options.duration,180);
  h.setReduced(true);
  assert.equal(h.animations[1].cancelled,true);
  h.api.stage(source);
  assert.equal(h.animations.length,2);
  h.setReduced(false); h.api.stage(source);
  h.document.hidden=true;
  await h.fire(h.event('visibilitychange'));
  assert.equal(h.animations[2].cancelled,true);
  h.api.stage(review);
  assert.equal(h.animations.length,3);
  h.document.hidden=false;
  review.animate=() => {throw Error('Native animation unavailable');};
  noFailure(() => h.api.stage(review));
});

await test('reduced motion and unsupported or throwing native animation remain usable', () => {
  const reduced=harness({reduced:true}), reducedElement=reduced.get('motion-target');
  noFailure(() => reduced.api.reveal(reducedElement)); noFailure(() => reduced.api.spotlight(reducedElement));
  assert.equal(reduced.animations.length,0);
  const h=harness(), unsupported=h.get('unsupported'); unsupported.animate=undefined;
  noFailure(() => h.api.reveal(unsupported)); noFailure(() => h.api.spotlight(unsupported));
  const throwing=h.get('throwing'); throwing.animate=() => {throw Error('Native animation unavailable');};
  noFailure(() => h.api.reveal(throwing)); noFailure(() => h.api.spotlight(throwing));
  noFailure(() => h.api.reveal(null)); noFailure(() => h.api.spotlight(null));
});
await test('filed handoff feedback animates only existing artwork and respects reduced motion',()=>{
  for(const reduced of [false,true]) {
    const h=harness({reduced}), sheet=h.get('sheet'), artwork=h.get('artwork','IMG');
    artwork.classList.add('handoff-art');sheet.append(artwork);
    h.api.filed(sheet);
    assert.equal(h.animations.length,reduced ? 0 : 1);
    if(!reduced) assert.equal(h.animations[0].options.duration,360);
    noFailure(()=>h.api.filed(null));noFailure(()=>h.api.filed(h.get('no-art')));
    assert.equal(h.networkCalls(),0);
  }
});

await test('find shortcut accepts Cmd or Ctrl K and preserves all shortcut fences', async () => {
  const h=harness(); let finds=0;
  h.api.init({onFind:() => {finds++;}, onFiles() {assert.fail('Keyboard must not select a file');}});
  const key = extra => h.event('keydown',h.document.body,{key:'k',metaKey:false,ctrlKey:false,altKey:false,shiftKey:false,isComposing:false,repeat:false,...extra});
  for (const modifiers of [{metaKey:true},{ctrlKey:true}]) {
    const evt=await h.fire(key(modifiers)); assert.equal(evt.defaultPrevented,true);
  }
  assert.equal(finds,2);
  for (const extra of [{},{key:'f',metaKey:true},{metaKey:true,repeat:true},{metaKey:true,isComposing:true},
    {metaKey:true,altKey:true},{metaKey:true,shiftKey:true},{ctrlKey:true,defaultPrevented:true}]) await h.fire(key(extra));
  assert.equal(finds,2);
  h.get('confirm-dialog').open=true; await h.fire(key({metaKey:true})); assert.equal(finds,2);
  h.get('confirm-dialog').open=false; h.shell.hidden=true;
  await h.fire(key({ctrlKey:true})); assert.equal(finds,2);
  assert.equal(h.networkCalls(),0);
});

await test('drop and picker changes delegate selection only inside the upload form', async () => {
  const h=harness(), selections=[];
  h.api.init({onFind() {},onFiles:(files,form) => selections.push({files:Array.from(files),form})});
  const files=[validFile('retained.txt')];
  const drop=await h.fire(h.event('drop',h.form,{dataTransfer:{files,types:['Files']}}));
  assert.equal(drop.defaultPrevented,true, 'File drop stays in the workspace');
  assert.equal(selections.length,1); assert.equal(selections[0].files[0],files[0]); assert.equal(selections[0].form,h.form);
  h.fileInput.files=files; await h.fire(h.event('change',h.fileInput));
  assert.equal(selections.length,2); assert.equal(selections[1].form,h.form);
  const unrelated=h.get('other-file','INPUT'); unrelated.type='file'; unrelated.files=files;
  await h.fire(h.event('change',unrelated)); await h.fire(h.event('drop',unrelated,{dataTransfer:{files,types:['Files']}}));
  assert.equal(selections.length,2, 'Unrelated inputs and drops are not PO selections');
  h.form.setAttribute('aria-busy','true');
  await h.fire(h.event('drop',h.form,{dataTransfer:{files,types:['Files']}}));
  await h.fire(h.event('change',h.fileInput));
  h.form.removeAttribute('aria-busy'); h.form.hidden=true;
  await h.fire(h.event('drop',h.form,{dataTransfer:{files,types:['Files']}}));
  await h.fire(h.event('change',h.fileInput));
  assert.equal(selections.length,2, 'A busy or hidden upload form cannot accept a selection');
  assert.equal(h.form.submitCalls,0); assert.equal(h.form.requestSubmitCalls,0); assert.equal(h.networkCalls(),0);
});

await test('notice helpers tolerate supported, reduced and unavailable motion without network', () => {
  for (const reduced of [false,true]) {
    const h=harness({reduced}), notice=h.get('notice');
    notice.hidden=false;
    noFailure(() => h.api.showNotice()); noFailure(() => h.api.showNotice(true));
    noFailure(() => h.api.dismissNotice());
    assert.equal(notice.hidden,true);
    notice.animate=() => {throw Error('Animation failed');}; notice.hidden=false;
    noFailure(() => h.api.showNotice()); noFailure(() => h.api.dismissNotice());
    assert.equal(notice.hidden,true); assert.equal(h.networkCalls(),0);
  }
});

process.stdout.write(`${passed} interaction regression groups passed (VM behavior checks).\n`);
