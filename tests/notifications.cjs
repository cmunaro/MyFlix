const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {}, pending = [], classes = new Set();
let document;
function element(tag = 'DIV') {
  const attrs = {}, handlers = {}, children = [];
  let text = '';
  return {tagName: tag.toUpperCase(), hidden: false, children, handlers, scrollTop: 0,
    set textContent(value) { text = value; children.length = 0; }, get textContent() { return text; },
    setAttribute(key, value) { attrs[key] = value; }, getAttribute(key) { return attrs[key]; },
    appendChild(child) { children.push(child); child.parentNode = this; },
    addEventListener(name, fn) { handlers[name] = fn; },
    contains(target) { return target === this || children.some(child => child.contains(target)); },
    querySelectorAll(tag) { return children.filter(child => child.tagName === tag.toUpperCase()); },
    focus() {
      document.activeElement = this;
      for (let node = this; node; node = node.parentNode) { if (node.handlers.focusin) node.handlers.focusin({target: this}); }
      (listeners.focusin || []).forEach(fn => fn({target: this}));
    },
    getBoundingClientRect() {
      if (this === list) return {top: 100, bottom: 350};
      const index = list.children.indexOf(this);
      const top = 100 + index * 100 - list.scrollTop;
      return {top, bottom: top + 100};
    },
    scrollIntoView() {
      this.scrolled = true;
      const rect = this.getBoundingClientRect();
      if (rect.bottom > 350) list.scrollTop += rect.bottom - 350;
      else if (rect.top < 100) list.scrollTop -= 100 - rect.top;
    }
  };
}
const toggle = element('BUTTON'), panel = element(), list = element(), count = element('SPAN');
const clear = element('BUTTON'), status = element();
panel.hidden = true; panel.appendChild(clear); panel.appendChild(list); panel.appendChild(status);
const nodes = {'notification-toggle': toggle, notifications: panel, 'notification-list': list,
  'notification-count': count, 'notification-clear': clear, 'notification-status': status};
document = {activeElement: toggle, hidden: false,
  body: {classList: {add(name) { classes.add(name); }, remove(name) { classes.delete(name); }}},
  getElementById(id) { return nodes[id]; }, createElement: element,
  querySelector() { return {content: 'csrf'}; },
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }};
let poll;
const window = {innerHeight: 800, setInterval(fn) { poll = fn; }, addEventListener(name, fn) { listeners[name] = [fn]; }};
function XMLHttpRequest() { pending.push(this); }
XMLHttpRequest.prototype.open = function (method, url) { this.method = method; this.url = url; };
XMLHttpRequest.prototype.setRequestHeader = function (name, value) { (this.headers ||= {})[name] = value; };
XMLHttpRequest.prototype.send = function (body) { this.body = body; };
vm.runInNewContext(fs.readFileSync('myflix/static/notifications.js', 'utf8'), {document, window, XMLHttpRequest});
function respond(notifications) {
  const xhr = pending.shift(); assert.equal(xhr.method, 'GET'); assert.equal(xhr.url, '/api/notifications');
  xhr.status = 200; xhr.responseText = JSON.stringify({notifications}); xhr.onload();
}
function mutation(action, ids, ok = true) {
  const xhr = pending.shift(); assert.equal(xhr.method, 'POST'); assert.equal(xhr.url, '/api/notifications/' + action);
  assert.equal(xhr.headers['X-CSRF-Token'], 'csrf');
  if (ids) assert.deepEqual(JSON.parse(xhr.body).ids, ids);
  xhr.status = ok ? 200 : 500; xhr.onload();
}
function key(keyCode) { let prevented = false; const owned = window.MyFlixNotifications.handleKey({keyCode, preventDefault() { prevented = true; }}); assert.equal(owned, prevented); return owned; }
const notices = Array.from({length: 60}, (_, id) => ({id: id + 1, seen: false, message: '<Movie> downloaded', created_at: '2026-01-01', href: '/title/' + id}));
respond(notices);
assert.equal(pending.length, 0, 'closed polling never marks rows seen');
assert.equal(count.textContent, 60);
assert(key(13));
assert.equal(panel.hidden, false);
assert.equal(document.activeElement, list);
respond(notices);
assert.equal(list.children[0].children[0].textContent, '<Movie> downloaded', 'titles use text nodes');
mutation('seen', [1, 2]);
assert.equal(count.textContent, 58);
assert.equal(list.children[2].notice.seen, false, 'partial row remains unseen');
assert.equal(list.children[59].notice.seen, false, 'offscreen row remains unseen');
assert.equal(pending.length, 0);
list.scrollTop = 200; list.handlers.scroll(); mutation('seen', [3, 4]);
assert.equal(count.textContent, 56);
assert.equal(list.children[0].notice.id, 1, 'marking does not reorder the open list');
key(38); assert.equal(document.activeElement, clear, 'Up from first row reaches Clear all');
assert.equal(key(13), false, 'native button activation owns Enter');
key(40); assert.equal(document.activeElement, list.children[0]);
for (let i = 0; i < 65; i++) key(40);
assert.equal(document.activeElement, list.children[59], 'remote stops at last notification');
while (pending.length) {
  const ids = JSON.parse(pending[0].body).ids;
  mutation('seen', ids);
}
key(38); assert.equal(document.activeElement, list.children[58]);
assert.equal(key(13), false, 'native link activation owns Enter');
key(461);
assert.equal(panel.hidden, true);
assert.equal(document.activeElement, toggle, 'Back restores bell focus');
assert.equal(classes.has('notifications-open'), false);
const reordered = notices.map(n => ({...n, seen: n.id !== 60})).sort((a, b) => a.seen - b.seen);
toggle.handlers.click(); respond(reordered);
assert.equal(list.children[0].notice.id, 60, 'reopening uses unseen-first server order');
assert.equal(list.scrollTop, 0, 'reopening resets scroll');
mutation('seen', [60], false);
assert.equal(list.children[0].notice.seen, false, 'failed save stays unseen');
assert.match(status.textContent, /Could not save/);
list.handlers.scroll(); mutation('seen', [60]);
clear.focus(); clear.handlers.click();
assert.equal(clear.disabled, true);
mutation('clear', null, false);
assert.equal(list.children.length, 60, 'failed clear keeps list');
assert.match(status.textContent, /Could not clear/);
clear.handlers.click(); mutation('clear');
assert.equal(list.textContent, 'No notifications yet.');
assert.equal(count.hidden, true);
assert.equal(clear.disabled, true);
assert.equal(document.activeElement, list, 'clearing restores usable focus');
(listeners.click || []).forEach(fn => fn({target: element()}));
assert.equal(panel.hidden, true, 'outside click closes dropdown');
toggle.focus(); key(13); key(27); respond(notices);
assert.equal(pending.length, 0, 'response after closing never marks rows seen');
poll(); pending.shift().onerror();
assert.equal(list.children.length, 60, 'failed refresh keeps saved notices');
assert.equal(key(39), false, 'closed dropdown releases navigation');
// Hidden documents, viewport clipping, and scrolling also guard visibility.
toggle.focus(); key(13); document.hidden = true; respond(notices);
assert.equal(pending.length, 0, 'background document never marks seen');
document.hidden = false; window.innerHeight = 180;
listeners.visibilitychange[0]();
assert.equal(pending.length, 0, 'viewport-clipped row stays unseen');
window.innerHeight = 800; listeners.resize[0](); mutation('seen', [1, 2]);
// Reopening waits for the pending seen write, so sorting includes that write.
list.scrollTop = 200; list.handlers.scroll();
assert.equal(pending.length, 1);
key(27); toggle.focus(); key(13);
assert.equal(pending.length, 1, 'refresh waits for pending seen mutation');
mutation('seen', [3, 4]);
respond(notices.map(n => ({...n, seen: true})));
assert.equal(pending.length, 0);
clear.handlers.click(); key(27); mutation('clear');
assert.equal(document.activeElement, toggle, 'late clear response does not focus a hidden list');
console.log('Notifications passed: visible-only seen state, long lists, unseen-first reopen, clear, CSRF, failures and remote focus');
