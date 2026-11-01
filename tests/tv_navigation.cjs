// Exercise remote navigation without a browser or a runtime dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('myflix/static/tv.js', 'utf8');
const css = fs.readFileSync('myflix/static/tv.css', 'utf8');
assert(!/\.tile:hover/.test(css), 'hover must not draw a second selection beside keyboard focus');
function screen(storage = {}, height = 900) {
  const listeners = {};
  const window = {pageYOffset: 0, innerHeight: height, scrollTo(x, y) { this.pageYOffset = y; }};
  let document;
  function element(attrs = {}) {
    return {
      tagName: 'A', offsetWidth: 280, offsetHeight: 190, offsetLeft: 0,
      getAttribute(key) { return Object.hasOwn(attrs, key) ? String(attrs[key]) : null; },
      hasAttribute(key) { return Object.hasOwn(attrs, key); },
      setAttribute(key, value) { attrs[key] = String(value); },
      focus() { document.activeElement = this; if (listeners.focusin) listeners.focusin({target: this}); },
      closest(selector) { return selector === '[data-focus-row]' && this.hero ? {} : null; },
      getBoundingClientRect() { return {left: this.offsetLeft, top: 100 - window.pageYOffset, width: 280, height: 190}; }
    };
  }
  const hero = element(); hero.hero = true;
  const header = element();
  const rails = [0, 1].map((row) => {
    const rail = element(); rail.offsetLeft = 0; rail.clientWidth = 600; rail.scrollLeft = 0;
    rail.parentNode = {getBoundingClientRect: () => ({top: 650 + row * 350 - window.pageYOffset, height: 310})};
    rail.cards = Array.from({length: row === 0 ? 8 : 4}, (_, col) => {
      const card = element({'data-card': '', 'data-media-id': row * 10 + col});
      card.offsetLeft = col * 300 + 24; card.parentNode = rail;
      return card;
    });
    rail.querySelectorAll = () => rail.cards;
    rail.querySelector = () => rail.cards[0];
    return rail;
  });
  document = {
    body: {className: 'tv-browse'}, activeElement: null,
    getElementById() { return null; },
    addEventListener(name, fn) { listeners[name] = fn; },
    querySelector(selector) { return selector === '[data-initial-focus]' ? hero : selector === '.tv-header a' ? header : null; },
    querySelectorAll(selector) { return selector === '[data-rail]' ? rails : [header, hero, ...rails.flatMap(r => r.cards)]; }
  };
  vm.runInNewContext(code, {document, window, location: {pathname: '/', search: ''}, sessionStorage: {
    getItem(key) { return storage[key] || null; }, setItem(key, value) { storage[key] = value; }
  }});
  return {document, window, rails, hero, storage,
    move(target, x, y) { listeners.mousemove({target, clientX: x, clientY: y}); },
    key(code) { listeners.keydown({keyCode: code, preventDefault() {}}); }};
}
const ui = screen();
assert.equal(ui.document.activeElement, ui.hero, 'featured Play receives initial focus');
ui.key(40);
assert.equal(ui.document.activeElement, ui.rails[0].cards[0], 'Down enters first shelf');
for (let i = 0; i < 6; i += 1) ui.key(39);
assert.equal(ui.document.activeElement, ui.rails[0].cards[6], 'Right reaches off-screen titles');
assert(ui.rails[0].scrollLeft > 1000, 'shelf scroll follows focused title');
ui.key(40);
assert.equal(ui.document.activeElement, ui.rails[1].cards[3], 'Down clamps selection on a shorter shelf');
ui.key(38);
assert.equal(ui.document.activeElement, ui.rails[0].cards[6], 'Up restores the previous shelf selection');
const restored = screen(ui.storage);
assert.equal(restored.document.activeElement, restored.rails[0].cards[6], 'return restores browsing focus');
restored.key(38);
assert.equal(restored.document.activeElement, restored.hero, 'Up from first shelf returns to featured Play');
const discovery = screen();
discovery.rails[0].cards[0].setAttribute('data-discovery', '');
discovery.rails[0].cards[0].setAttribute('data-media-id', 'discover-tv-7');
discovery.key(40);
assert.equal(discovery.window.pageYOffset, 650 - (900 - 310) / 2, 'discovery shelf centers its full height');
assert.equal(JSON.parse(discovery.storage['myflix-focus:/']).id, 'discover-tv-7', 'discovery IDs stay distinct from library IDs');
const mixed = screen();
mixed.key(40);
const scrollBeforeHover = mixed.window.pageYOffset;
const hovered = mixed.rails[1].cards[0];
mixed.move({parentNode: hovered}, 100, 200);
assert.equal(mixed.document.activeElement, hovered, 'moving over artwork transfers the single DOM focus');
assert.equal(mixed.window.pageYOffset, scrollBeforeHover, 'pointer focus does not jump the page');
mixed.key(38);
const remoteCard = mixed.rails[0].cards[0];
assert.equal(mixed.document.activeElement, remoteCard);
mixed.move(hovered, 100, 200);
assert.equal(mixed.document.activeElement, remoteCard, 'stationary cursor cannot steal remote selection after scrolling');
mixed.move(hovered, 101, 200);
assert.equal(mixed.document.activeElement, hovered, 'real pointer movement can resume selection');
for (const height of [480, 720, 1080]) {
  const centered = screen({}, height);
  centered.key(40);
  let bounds = centered.rails[0].parentNode.getBoundingClientRect();
  assert.equal(bounds.top + bounds.height / 2, height / 2, 'library row centers artwork and captions');
  centered.rails[1].cards[0].focus();
  bounds = centered.rails[1].parentNode.getBoundingClientRect();
  assert.equal(bounds.top + bounds.height / 2, height / 2, 'native focus also centers the full row');
  assert(bounds.top >= 0 && bounds.top + bounds.height <= height, 'full row remains in viewport');
}
console.log('Remote navigation passed: shelves centered at 480p, 720p, 1080p; focus restoration and horizontal navigation');
