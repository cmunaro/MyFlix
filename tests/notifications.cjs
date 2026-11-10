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
    focus() { document.activeElement = this; (listeners.focusin || []).forEach(fn => fn({target: this})); },
    scrollIntoView() { this.scrolled = true; }
  };
}
const toggle = element('BUTTON'), panel = element(), list = element(), count = element('SPAN');
panel.hidden = true; panel.appendChild(list);
const nodes = {'notification-toggle': toggle, notifications: panel, 'notification-list': list, 'notification-count': count};
document = {activeElement: toggle, hidden: false,
  body: {classList: {add(name) { classes.add(name); }, remove(name) { classes.delete(name); }}},
  getElementById(id) { return nodes[id]; }, createElement: element,
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }};
let poll;
const window = {setInterval(fn) { poll = fn; }};
function XMLHttpRequest() { pending.push(this); }
XMLHttpRequest.prototype.open = function (method, url) { assert.equal(method, 'GET'); assert.equal(url, '/api/notifications'); };
XMLHttpRequest.prototype.send = function () {};
vm.runInNewContext(fs.readFileSync('myflix/static/notifications.js', 'utf8'), {document, window, XMLHttpRequest});
function respond(notifications) { const xhr = pending.shift(); xhr.status = 200; xhr.responseText = JSON.stringify({notifications}); xhr.onload(); }
function key(keyCode) { let prevented = false; const owned = window.MyFlixNotifications.handleKey({keyCode, preventDefault() { prevented = true; }}); assert.equal(owned, prevented); return owned; }
respond([]);
assert.equal(count.hidden, true);
assert.equal(list.textContent, 'No notifications yet.');
assert(key(13));
assert.equal(panel.hidden, false);
assert.equal(document.activeElement, list);
const notices = Array.from({length: 20}, (_, id) => ({id, message: '<Movie> downloaded', created_at: '2026-01-01', href: '/title/' + id}));
respond(notices);
assert.equal(count.textContent, 20);
assert.equal(list.children[0].children[0].textContent, '<Movie> downloaded', 'titles use text nodes');
key(40);
assert.equal(document.activeElement, list.children[0]);
for (let i = 0; i < 25; i++) key(40);
assert.equal(document.activeElement, list.children[19], 'remote stops at last notification');
key(38);
assert.equal(document.activeElement, list.children[18]);
assert.equal(key(13), false, 'native link activation owns Enter');
key(461);
assert.equal(panel.hidden, true);
assert.equal(document.activeElement, toggle, 'Back restores bell focus');
assert.equal(classes.has('notifications-open'), false);
toggle.handlers.click();
respond(notices);
(listeners.click || []).forEach(fn => fn({target: element()}));
assert.equal(panel.hidden, true, 'outside click closes dropdown');
toggle.focus(); key(13); respond([]); key(27);
assert.equal(document.activeElement, toggle);
poll();
const xhr = pending.shift(); xhr.onerror();
assert.equal(list.textContent, 'No notifications yet.', 'failed refresh keeps saved notices');
assert.equal(key(39), false, 'closed dropdown releases navigation');
console.log('Notifications passed: empty state, safe titles, refresh, remote navigation, Back and outside click');
