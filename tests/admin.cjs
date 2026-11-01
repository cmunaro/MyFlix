// Management interaction checks; native TV keyboards and CSS need device review.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function screen(tv = true) {
  const listeners = {}, pending = [], intervals = [];
  let document;
  class Element {
    constructor(tag, attrs = {}, rect = {}) {
      this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = [];
      this.rect = {left: 0, top: 0, width: 180, height: 60, ...rect};
      this.offsetWidth = this.rect.width; this.offsetHeight = this.rect.height;
      this.type = attrs.type || ''; this.value = ''; this.disabled = false;
      this.className = attrs.class || ''; this._text = ''; this.clicks = 0;
      this.classList = {add: name => { this.className += ' ' + name; }};
    }
    set textContent(value) {
      if (document && document.activeElement !== this && this.contains(document.activeElement)) document.activeElement = document.body;
      this.children.forEach(child => { child.parentNode = null; });
      this.children = []; this._text = value;
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    insertBefore(child, before) { child.parentNode = this; this.children.splice(this.children.indexOf(before), 0, child); }
    contains(child) { return child === this || this.children.some(el => el.contains(child)); }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    removeAttribute(key) { delete this.attrs[key]; }
    getAttribute(key) { return this.attrs[key] === undefined ? null : String(this.attrs[key]); }
    hasAttribute(key) { return this.attrs[key] !== undefined; }
    matches(selector) {
      if (selector === 'details[open]') return this.tagName === 'DETAILS' && this.open;
      if (selector[0] === '.') return this.className.split(' ').includes(selector.slice(1));
      if (selector[0] === '[') return this.hasAttribute(selector.slice(1, -1));
      return this.tagName.toLowerCase() === selector.toLowerCase();
    }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll('*')]).filter(child =>
        selector === '*' || selector.split(',').some(part => child.matches(part.trim())));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentNode ? this.parentNode.closest(selector) : null; }
    getBoundingClientRect() { return this.rect; }
    scrollIntoView() { this.scrolled = true; }
    focus() { document.activeElement = this; (listeners.focusin || []).forEach(fn => fn({target: this})); }
    click() { this.clicks++; if (this.type === 'checkbox') this.checked = !this.checked; if (this.onclick) this.onclick(); }
  }
  const body = new Element('body'); body.className = 'tv-admin';
  const add = (parent, tag, attrs, rect) => parent.appendChild(new Element(tag, attrs, rect));
  const sidebar = add(body, 'aside', {class: 'admin-sidebar'});
  const current = add(sidebar, 'a', {'aria-current': 'page', 'data-initial-focus': ''}, {left: 20, top: 100});
  const library = add(sidebar, 'a', {}, {left: 20, top: 190});
  const root = add(body, 'div', {id: 'downloads'});
  const form = add(root, 'form', {id: 'download-search'});
  const query = add(form, 'input', {id: 'download-query'}, {left: 300, top: 100});
  const provider = add(form, 'select', {id: 'download-provider'}, {left: 520, top: 100});
  const search = add(form, 'button', {id: 'search-downloads'}, {left: 740, top: 100});
  for (const id of ['download-message', 'download-connection', 'download-results', 'queue-message', 'download-queue']) add(root, 'div', {id});
  const refresh = add(root, 'button', {id: 'refresh-downloads'}, {left: 740, top: 300});
  const meta = {content: 'csrf-token'};
  document = {
    body, documentElement: new Element('html'), activeElement: body,
    createElement(tag) { return new Element(tag); },
    getElementById(id) { return body.querySelectorAll('*').find(el => el.getAttribute('id') === id) || null; },
    querySelector(selector) {
      if (selector.includes('meta[')) return meta;
      if (selector === '.admin-sidebar [aria-current]') return current;
      return body.querySelector(selector);
    },
    querySelectorAll(selector) { return body.querySelectorAll(selector); },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }
  };
  class XHR {
    constructor() { this.headers = {}; }
    open(method, url) { this.method = method; this.action = url.split('/').pop(); }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.body = body && JSON.parse(body); pending.push(this); }
  }
  const location = {pathname: '/admin', search: ''};
  const context = {document, navigator: {userAgent: tv ? 'VIDAA Hisense' : 'Firefox'}, location,
    window: {pageYOffset: 0, scrollTo() {}, setInterval(fn) { intervals.push(fn); }},
    sessionStorage: {getItem() { return null; }, setItem() {}}, XMLHttpRequest: XHR};
  vm.runInNewContext(fs.readFileSync('myflix/static/tv.js', 'utf8'), context);
  function key(code) {
    const event = {keyCode: code, target: document.activeElement, prevented: false, preventDefault() { this.prevented = true; }};
    (listeners.keydown || []).forEach(fn => fn(event)); return event;
  }
  function reply(action, data, status = 200) {
    const index = pending.findIndex(xhr => xhr.action === action);
    assert(index >= 0, `expected pending ${action}`);
    const xhr = pending.splice(index, 1)[0]; xhr.status = status; xhr.responseText = JSON.stringify(data); xhr.onload(); return xhr;
  }
  return {document, body, add, current, library, query, provider, search, refresh, form, key, location, pending, reply, intervals,
    downloads() { vm.runInNewContext(fs.readFileSync('myflix/static/downloads.js', 'utf8'), context); },
    el(id) { return document.getElementById(id); }};
}

const ui = screen();
assert.equal(ui.document.activeElement, ui.current, 'current management view receives focus');
ui.key(39); assert.equal(ui.document.activeElement, ui.query, 'Right enters search input');
ui.key(39); assert.equal(ui.document.activeElement, ui.provider, 'selectors participate in navigation');
ui.key(13); assert(ui.provider.hasAttribute('data-editing'), 'OK starts native selection');
ui.key(40); assert.equal(ui.document.activeElement, ui.provider, 'arrows stay with native dropdown while editing');
assert(ui.key(461).prevented); assert(!ui.provider.hasAttribute('data-editing'), 'Back ends editing');
ui.key(39); assert.equal(ui.document.activeElement, ui.search, 'navigation resumes after editing');
ui.query.focus(); ui.key(13); assert.equal(ui.query.clicks, 1, 'OK activates text input once');
assert(!ui.key(37).prevented, 'editing retains native caret behavior');
ui.key(10009); ui.key(10009); assert.equal(ui.document.activeElement, ui.current, 'Back returns to navigation');
ui.key(10009); assert.equal(ui.location.href, '/', 'Back from navigation returns to watching');
const details = ui.add(ui.body, 'details'); details.open = true;
const summary = ui.add(details, 'summary'); const save = ui.add(details, 'button');
save.focus(); ui.key(461); assert(!details.open); assert.equal(ui.document.activeElement, summary, 'Back closes catalog editor');
const checkbox = ui.add(ui.body, 'input', {type: 'checkbox'});
checkbox.focus(); ui.key(13); assert.equal(checkbox.checked, true); assert.equal(checkbox.clicks, 1, 'OK toggles once');
const desktop = screen(false); desktop.query.focus();
assert(!desktop.key(37).prevented); assert(!desktop.key(13).prevented, 'desktop input behavior remains native');
const columns = screen(); columns.query.focus(); columns.key(40);
assert.equal(columns.document.activeElement, columns.refresh, 'vertical movement stays in the content column');

ui.downloads();
ui.reply('sites', {groups: [{label: 'Providers', options: [{value: '__all__', label: 'All providers'}]}]});
ui.reply('status', {slots: {active: 1, max: 1}, queued: 0});
const active = {active: [{id: 'job-1', title: 'I Simpson', status: 'Downloading', progress: 40}], history: [{title: 'Old completed film'}]};
ui.reply('queue', active);
assert(!ui.el('download-queue').textContent.includes('Old completed film'), 'history is absent');
assert(ui.el('download-queue').querySelector('progress'), 'active progress is visible');
const cancel = () => ui.el('download-queue').querySelector('button');
cancel().focus(); ui.intervals[0](); ui.reply('queue', active);
assert.equal(ui.document.activeElement, cancel(), 'refresh restores focused job action');
ui.reply('status', {slots: {active: 1, max: 1}, queued: 0});
cancel().click(); cancel().click();
assert.equal(ui.pending.filter(xhr => xhr.action === 'cancel').length, 1, 'pending cancellation cannot duplicate');
ui.reply('cancel', {cancelled: true}); ui.reply('queue', {history: active.history});
assert.equal(ui.document.activeElement, ui.refresh, 'finished job restores focus to Refresh');
assert(ui.el('download-queue').textContent.includes('Nothing downloading'), 'history does not suppress empty state');
ui.reply('status', {slots: {active: 0, max: 1}, queued: 0});
ui.search.focus(); ui.form.onsubmit({preventDefault() {}}); ui.form.onsubmit({preventDefault() {}});
assert.equal(ui.pending.filter(xhr => xhr.action === 'search').length, 1, 'search does not duplicate');
ui.reply('search', {results: [{title: 'I Simpson', site: 'example', is_movie: false, payload: {name: 'I Simpson'}}]});
const choose = ui.el('download-results').querySelector('button');
assert.equal(ui.document.activeElement, choose, 'results receive focus when search action is focused');
choose.click(); choose.click(); assert.equal(ui.pending.filter(xhr => xhr.action === 'seasons').length, 1);
ui.reply('seasons', {seasons: [{number: 1, episodes: 13}]});
assert.equal(ui.document.activeElement.tagName, 'SELECT', 'season selection receives focus');
choose.focus(); choose.click(); choose.click();
assert.equal(ui.pending.filter(xhr => xhr.action === 'download').length, 1, 'download mutation cannot duplicate');
const request = ui.reply('download', {error: 'Connection uncertain. Check queue.'}, 502);
assert.equal(request.headers['X-CSRF-Token'], 'csrf-token');
choose.click(); assert(!ui.pending.some(xhr => xhr.action === 'download'), 'uncertain download is never retried');
assert.equal(ui.document.activeElement, choose, 'pending and failed action retain remote focus');
console.log('Admin behavior passed: remote fields and Back, native desktop inputs, current queue only, refresh focus, episode selection, CSRF, and mutation guards');
