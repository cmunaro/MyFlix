const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function screen(withList = true) {
  let document;
  class Element {
    constructor(tag) { this.tagName = tag; this.attrs = {}; this.children = []; this.hidden = false; this._text = ''; }
    set textContent(value) { this._text = value; this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    removeAttribute(name) { delete this.attrs[name]; }
    querySelector(selector) {
      const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      const matches = element => selector === '.lookup-status' ? element.className === 'lookup-status' :
        match ? Object.hasOwn(element.attrs, match[1]) && (match[2] === undefined || element.attrs[match[1]] === match[2]) : element.tagName === selector;
      for (const child of this.children) {
        if (matches(child)) return child;
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    focus() { document.activeElement = this; }
  }
  const root = new Element('main');
  const connection = root.appendChild(new Element('p'));
  let list;
  if (withList) { list = root.appendChild(new Element('div')); list.appendChild(new Element('p')).setAttribute('data-no-lookups', ''); }
  const button = root.appendChild(new Element('button'));
  const requests = [], timers = [], listeners = {};
  document = {
    hidden: false, activeElement: button,
    createElement: tag => new Element(tag),
    getElementById: id => id === 'following-connection' ? connection : id === 'following-lookups' ? list : null,
    querySelector: selector => root.querySelector(selector),
    addEventListener: (name, callback) => { listeners[name] = callback; }
  };
  class XHR {
    constructor() { requests.push(this); this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) { this.body = body; }
    abort() { this.aborted = true; }
  }
  const window = {
    setTimeout: callback => { timers.push(callback); return timers.length; },
    clearTimeout: () => {},
    addEventListener: (name, callback) => { listeners[name] = callback; }
  };
  vm.runInNewContext(fs.readFileSync('myflix/static/following.js', 'utf8'), {document, window, XMLHttpRequest: XHR});
  function reply(items, status = 200) {
    const request = requests.at(-1);
    request.status = status; request.responseText = JSON.stringify({subscriptions: items}); request.onload();
  }
  const card = () => root.querySelector('[data-lookup-id="1"]');
  return {document, connection, button, requests, timers, listeners, reply, card, list};
}

const ui = screen();
const item = {id: 1, series: 'I Simpson', state: 'queued', message: 'Episode lookup queued', error: '', seasons_total: 0, seasons_done: 0, checked: 'Not checked yet'};
assert.equal(ui.requests[0].method, 'GET');
assert.equal(ui.requests[0].url, '/admin/following/status');
assert.equal(ui.requests[0].body, null);
ui.reply([item]);
assert(ui.card().textContent.includes('Episode lookup queued'));
assert(ui.list.querySelector('[data-no-lookups]').hidden);
assert(ui.card().querySelector('progress').hidden);
const status = ui.card().querySelector('.lookup-status');
assert.equal(status.attrs['aria-live'], 'polite');
ui.timers.at(-1)();
ui.reply([{...item, state: 'fetching', message: 'Fetching episodes — loading season list…'}]);
const progress = ui.card().querySelector('progress');
assert(!progress.hidden);
assert(!Object.hasOwn(progress.attrs, 'value'), 'unknown totals use indeterminate progress');
assert.equal(status.attrs['aria-busy'], 'true');
ui.timers.at(-1)();
ui.reply([{...item, state: 'fetching', message: '2 of 37 seasons fetched', seasons_total: 37, seasons_done: 2}]);
assert.equal(progress.attrs.max, '37');
assert.equal(progress.attrs.value, '2');
assert.equal(ui.document.activeElement, ui.button, 'polling preserves remote focus');
const complete = ui.card().querySelector('[data-lookup-complete]');
ui.timers.at(-1)();
ui.reply([{...item, state: 'complete', message: 'Episode lookup complete — 805 episodes found', checked: '2026-11-01 19:00 UTC'}]);
assert(progress.hidden);
assert.equal(status.attrs['aria-busy'], 'false');
assert(!complete.hidden);
assert.equal(complete.querySelector('a').href, '/admin?section=following');
assert(ui.card().querySelector('[data-lookup-checked]').textContent.includes('19:00 UTC'));
complete.querySelector('a').focus();
ui.timers.at(-1)();
ui.reply([{...item, state: 'complete', message: 'Episode lookup complete — 805 episodes found'}]);
assert.equal(ui.document.activeElement, complete.querySelector('a'), 'completion link survives refresh');
ui.timers.at(-1)();
ui.reply([{...item, state: 'error', message: 'Episode lookup failed', error: '<script>provider error</script>'}]);
const error = ui.card().querySelector('[data-lookup-error]');
assert(!error.hidden);
assert.equal(error.textContent, '<script>provider error</script>');
assert(!error.querySelector('script'), 'errors use text rather than HTML');
assert(!complete.hidden, 'a focused completion link remains visible even when status changes');
ui.button.focus();
ui.timers.at(-1)();
ui.requests.at(-1).onerror();
assert(ui.connection.textContent.includes('out of date'));
assert(!error.hidden, 'connection errors preserve the last known lookup error');
ui.timers.at(-1)();
ui.reply([item]);
assert.equal(ui.connection.textContent, '');
assert(error.hidden);
const count = ui.requests.length;
ui.document.hidden = true;
ui.timers.at(-1)();
assert.equal(ui.requests.length, count, 'hidden pages pause polling');
ui.document.hidden = false;
ui.listeners.visibilitychange();
assert.equal(ui.requests.length, count + 1);
ui.listeners.visibilitychange();
assert.equal(ui.requests.length, count + 1, 'requests never overlap');
ui.reply([item]);
ui.listeners.pagehide();
ui.timers.at(-1)();
assert.equal(ui.requests.length, count + 1, 'leaving stops polling');
ui.listeners.pageshow();
assert.equal(ui.requests.length, count + 2, 'back navigation resumes polling');
const oldRequest = ui.requests.at(-1);
ui.listeners.pagehide();
assert(oldRequest.aborted, 'leaving aborts an in-flight read');
ui.listeners.pageshow();
assert.equal(ui.requests.length, count + 3, 'back navigation resumes even after leaving mid-request');
oldRequest.responseText = JSON.stringify({subscriptions: [{...item, message: 'STALE'}]});
oldRequest.status = 200; oldRequest.onload();
assert(!ui.card().textContent.includes('STALE'), 'abandoned reads cannot overwrite current status');
ui.reply([item]);
console.log('Following status passed: real and indeterminate progress, completion, safe errors, focus preservation, and polling lifecycle');
