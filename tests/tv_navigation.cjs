// Exercise remote navigation without a browser or a runtime dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('myflix/static/tv.js', 'utf8');
function screen(storage = {}) {
  const listeners = {};
  const window = {pageYOffset: 0, scrollTo(x, y) { this.pageYOffset = y; }};
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
    rail.parentNode = {getBoundingClientRect: () => ({top: 650 + row * 350 - window.pageYOffset})};
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
  return {document, rails, hero, storage, key(code) { listeners.keydown({keyCode: code, preventDefault() {}}); }};
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
console.log('Remote navigation: 7 assertions passed');
