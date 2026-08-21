// Exercises the REAL renderDetail()/deskFormState()/deskRestoreForm() (plus
// their dependencies) straight out of web/index.html, against a small
// hand-rolled DOM stand-in — no jsdom, matching this repo's no-dependency
// ethos (see server/tb-atc-api.py's own header). The DOM stand-in is just
// enough to hold the desk pane's flat markup (div/span/button/textarea,
// class/id/data-* attributes, .value/.textContent/.style.display), not a
// general HTML engine.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const html = readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');

function extractConst(name){
  const re = new RegExp('const ' + name + ' = .*?;');
  const m = html.match(re);
  assert.ok(m, name + ' not found in web/index.html');
  return m[0];
}

function extractFn(name){
  const marker = 'function ' + name + '(';
  const start = html.indexOf(marker);
  assert.ok(start >= 0, name + ' not found in web/index.html');
  let depth = 0, i = html.indexOf('{', start), end = -1;
  for (; i < html.length; i++){
    if (html[i] === '{') depth++;
    else if (html[i] === '}'){ depth--; if (depth === 0){ end = i + 1; break; } }
  }
  assert.ok(end > start, 'could not bound ' + name);
  return html.slice(start, end);
}

const SOURCE = [
  'var deskFocus = null;',
  extractFn('esc'),
  extractFn('deskCountdown'),
  extractFn('deskAgo'),
  extractFn('deskDisplayList'),
  extractFn('deskThreadHtml'),
  extractFn('deskFocusEntry'),
  extractFn('deskRuleReady'),
  extractFn('deskSyncRuleButton'),
  extractFn('deskContent'),
  extractConst('DESK_FIELDS'),
  extractFn('deskFormState'),
  extractFn('deskRestoreForm'),
  extractFn('renderDetail'),
].join('\n');

// ---- minimal fake DOM: just enough surface for the functions above ----

function toKebab(camel){ return camel.replace(/[A-Z]/g, c => '-' + c.toLowerCase()); }
function toCamel(kebab){ return kebab.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

class ClassList {
  constructor(el){ this.el = el; }
  _set(){ return new Set((this.el.attrs['class'] || '').split(/\s+/).filter(Boolean)); }
  _write(set){ this.el.attrs['class'] = [...set].join(' '); }
  toggle(cls, force){
    const s = this._set();
    const on = force === undefined ? !s.has(cls) : !!force;
    on ? s.add(cls) : s.delete(cls);
    this._write(s);
    return on;
  }
  remove(cls){ const s = this._set(); s.delete(cls); this._write(s); }
  contains(cls){ return this._set().has(cls); }
}

class FakeEl {
  constructor(tag, doc){
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parent = null;
    this._value = '';
    this._text = '';
    this._style = {};
    this._disabled = false;
    this.selectionStart = null;
    this.selectionEnd = null;
    this.doc = doc;
  }
  get id(){ return this.attrs.id || ''; }
  get classList(){ return new ClassList(this); }
  get dataset(){
    const el = this;
    return new Proxy({}, {
      get(_, key){ return el.attrs['data-' + toKebab(String(key))]; },
      set(_, key, v){ el.attrs['data-' + toKebab(String(key))] = String(v); return true; },
      deleteProperty(_, key){ delete el.attrs['data-' + toKebab(String(key))]; return true; },
      has(_, key){ return ('data-' + toKebab(String(key))) in el.attrs; },
    });
  }
  get style(){ return this._style; }
  get value(){ return this._value; }
  set value(v){ this._value = v; }
  get disabled(){ return this._disabled; }
  set disabled(v){ this._disabled = !!v; }
  get textContent(){
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }
  set textContent(v){ this.children = []; this._text = String(v); }
  get innerHTML(){ throw new Error('innerHTML getter not implemented in this stub'); }
  set innerHTML(htmlStr){
    unregisterSubtree(this.doc, this);
    this.children = parseFragment(htmlStr, this.doc);
    registerSubtree(this.doc, this);
  }
  focus(){ this.doc.activeElement = this; }
  setSelectionRange(s, e){ this.selectionStart = s; this.selectionEnd = e; }
  addEventListener(){ /* not exercised: this suite drives state directly, not via dispatched events */ }
  querySelector(sel){ const r = this.querySelectorAll(sel); return r.length ? r[0] : null; }
  querySelectorAll(sel){
    const classes = sel.replace(/^\./, '').split('.');
    const out = [];
    const walk = el => {
      for (const c of el.children){
        if (c instanceof FakeEl){
          const has = classes.every(cls => c.classList.contains(cls));
          if (has) out.push(c);
          walk(c);
        }
      }
    };
    walk(this);
    return out;
  }
}

function registerSubtree(doc, el){
  for (const c of el.children){
    if (c instanceof FakeEl){
      if (c.id) doc.registry.set(c.id, c);
      registerSubtree(doc, c);
    }
  }
}
function unregisterSubtree(doc, el){
  for (const c of el.children){
    if (c instanceof FakeEl){
      if (c.id) doc.registry.delete(c.id);
      unregisterSubtree(doc, c);
    }
  }
}

const TAG_RE = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[a-zA-Z_:][-a-zA-Z0-9_:.]*(?:\s*=\s*(?:"[^"]*"|'[^']*'))?)*)\s*>/g;
const ATTR_RE = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g;
function decodeEntities(s){
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}
function parseFragment(htmlStr, doc){
  const root = { children: [] };
  const stack = [root];
  let last = 0;
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(htmlStr))){
    if (m.index > last){
      const text = htmlStr.slice(last, m.index);
      if (text) stack[stack.length - 1].children.push({ textContent: decodeEntities(text) });
    }
    const [, closing, tag, attrStr] = m;
    if (closing){
      stack.pop();
    } else {
      const el = new FakeEl(tag, doc);
      ATTR_RE.lastIndex = 0;
      let am;
      while ((am = ATTR_RE.exec(attrStr))){
        const [, aname, dq, sq] = am;
        el.attrs[aname] = dq !== undefined ? decodeEntities(dq) : (sq !== undefined ? decodeEntities(sq) : '');
      }
      stack[stack.length - 1].children.push(el);
      stack.push(el);
    }
    last = TAG_RE.lastIndex;
  }
  return root.children;
}

class FakeDocument {
  constructor(){ this.activeElement = null; this.registry = new Map(); }
  getElementById(id){ return this.registry.get(id) || null; }
}

function makeSandbox({ decisions, followups }){
  const doc = new FakeDocument();
  const detailEl = new FakeEl('div', doc);
  const sandbox = {
    document: doc,
    detailEl,
    DATA: { decisions },
    followups,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);
  return sandbox;
}

function decision(id, opts = {}){
  return { id, question: id + '?', raiserId: 'agent:x', raisedAt: 0, deadlineAt: Date.now() + 3_600_000,
           supersedes: null, note: null, options: ['yes', 'no'], ...opts };
}

// ---- 1. picking an option and typing a rationale survives a same-key rebuild ----
{
  const sandbox = makeSandbox({ decisions: [decision('D1')], followups: [] });
  sandbox.deskFocus = 'D1';
  sandbox.renderDetail();

  const opt = sandbox.detailEl.querySelectorAll('.deskopt').find(b => b.dataset.label === 'yes');
  assert.ok(opt, 'option button rendered');
  opt.classList.toggle('on', true);
  sandbox.document.getElementById('deskrationale').value = 'Because it is the safer default.';
  sandbox.document.getElementById('deskresponse').focus();
  sandbox.document.getElementById('deskresponse').setSelectionRange(2, 2);

  // Simulate a data poll re-render of the SAME decision.
  sandbox.renderDetail();

  assert.equal(sandbox.detailEl.querySelector('.deskopt.on')?.dataset.label, 'yes',
    'picked option must survive a same-key rebuild');
  assert.equal(sandbox.document.getElementById('deskrationale').value,
    'Because it is the safer default.', 'typed rationale must survive a same-key rebuild');
}

// ---- 2. an open follow-up box survives a same-key rebuild ----
{
  const sandbox = makeSandbox({ decisions: [decision('D1')], followups: [] });
  sandbox.deskFocus = 'D1';
  sandbox.renderDetail();

  sandbox.document.getElementById('deskaskbox').style.display = 'flex';   // "ask a follow-up" clicked
  sandbox.document.getElementById('deskaskq').value = 'give me the ELI5';

  sandbox.renderDetail();

  assert.equal(sandbox.document.getElementById('deskaskbox').style.display, 'flex',
    'the open ask-a-follow-up box must survive a same-key rebuild');
  assert.equal(sandbox.document.getElementById('deskaskq').value, 'give me the ELI5',
    'typed follow-up question must survive a same-key rebuild');
}

// ---- 3. switching to a DIFFERENT decision starts clean, no bleed-through ----
{
  const sandbox = makeSandbox({ decisions: [decision('D1'), decision('D2')], followups: [] });
  sandbox.deskFocus = 'D1';
  sandbox.renderDetail();
  sandbox.detailEl.querySelectorAll('.deskopt')[0].classList.toggle('on', true);
  sandbox.document.getElementById('deskrationale').value = 'notes for D1';

  sandbox.deskFocus = 'D2';
  sandbox.renderDetail();

  assert.equal(sandbox.detailEl.querySelector('.deskopt.on'), null,
    "a different decision's pane must not inherit D1's picked option");
  assert.equal(sandbox.document.getElementById('deskrationale').value, '',
    "a different decision's pane must not inherit D1's typed text");
}

console.log('test_desk_form_state.mjs: all assertions passed');
