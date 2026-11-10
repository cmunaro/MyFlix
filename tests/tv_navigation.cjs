// Exercise remote navigation without a browser or a runtime dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('myflix/static/tv.js', 'utf8');
const css = fs.readFileSync('myflix/static/tv.css', 'utf8');
assert(!/\.tile:hover/.test(css), 'hover must not draw a second selection beside keyboard focus');
function screen(storage = {}, height = 900, options = {}) {
  const listeners = {};
  const windowListeners = {};
  const window = {pageYOffset: 0, innerHeight: height, scrollTo(x, y) { this.pageYOffset = y; },
    addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); }};
  if (options.pointer !== undefined) {
    window.matchMedia = query => ({matches: query.includes('reduced-motion') ? !!options.reducedMotion : options.pointer});
  }
  let document;
  function element(attrs = {}) {
    return {
      tagName: 'A', offsetWidth: 280, offsetHeight: 190, offsetLeft: 0, style: {}, handlers: {},
      addEventListener(name, fn) { this.handlers[name] = fn; },
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
    const rail = element({'aria-label': row === 0 ? 'Continue watching' : 'Season 1'}); rail.offsetLeft = 0; rail.clientWidth = 600; rail.scrollLeft = 0;
    rail.scrollWidth = row === 0 ? 2400 : 1200;
    rail.parentNode = {buttons: [], appendChild(el) { this.buttons.push(el); },
      getBoundingClientRect: () => ({top: 650 + row * 350 - window.pageYOffset, height: 310})};
    if (options.smooth) {
      rail.scrollTo = function (request) { this.lastRequest = request; };
    }
    rail.cards = Array.from({length: row === 0 ? 8 : 4}, (_, col) => {
      const card = element({'data-card': '', 'data-media-id': row * 10 + col});
      card.offsetLeft = col * 300 + 24; card.parentNode = rail;
      return card;
    });
    rail.querySelectorAll = () => rail.cards;
    rail.querySelector = selector => selector === '.tile-picture' ? {getBoundingClientRect: () => ({top: 700 + row * 350 - window.pageYOffset, height: 190})} : rail.cards[0];
    return rail;
  });
  document = {
    body: {className: 'tv-browse', classList: {add() {}}}, activeElement: null,
    documentElement: {style: options.smooth ? {scrollBehavior: ''} : {}},
    createElement() { const el = element(); el.tagName = 'BUTTON'; return el; },
    getElementById() { return null; },
    addEventListener(name, fn) { listeners[name] = fn; },
    querySelector(selector) { return selector === '.home-hero [data-initial-focus]' ? (options.homeHero === false ? null : hero) : selector === '[data-initial-focus]' ? hero : selector === '.tv-header a' ? header : null; },
    querySelectorAll(selector) { return selector === '[data-rail]' ? rails : [header, hero, ...rails.flatMap(r => r.cards)]; }
  };
  vm.runInNewContext(code, {document, window, navigator: {userAgent: options.userAgent || ''}, location: {pathname: options.pathname || '/', search: ''}, sessionStorage: {
    getItem(key) { return storage[key] || null; }, setItem(key, value) { storage[key] = value; }
  }});
  return {document, window, rails, hero, storage,
    resize() { (windowListeners.resize || []).forEach(fn => fn()); },
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
const restored = screen({...ui.storage});
assert.equal(restored.document.activeElement, restored.hero, 'home Resume wins over a saved Continue watching card');
assert.equal(restored.window.pageYOffset, 0, 'home loads at the hero instead of scrolling to a shelf');
restored.key(40);
restored.key(38);
assert.equal(restored.document.activeElement, restored.hero, 'Up from first shelf returns to featured Resume');
const withoutHero = screen(ui.storage, 900, {homeHero: false});
assert.equal(withoutHero.document.activeElement, withoutHero.rails[0].cards[6], 'home without a featured title retains shelf restoration');
const series = screen({}, 900, {pathname: '/series/1'});
series.key(40);
series.key(39);
const seriesReturn = screen(series.storage, 900, {pathname: '/series/1'});
assert.equal(seriesReturn.document.activeElement, seriesReturn.rails[0].cards[1], 'series pages retain saved episode focus');
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

for (const options of [{pointer: false}, {}, ...['VIDAA', 'Hisense', 'HiSmartTV', 'SmartTV', 'Smart-TV', 'HbbTV', 'Vewd'].map(userAgent => ({pointer: true, userAgent}))]) {
  const excluded = screen({}, 900, options);
  assert(excluded.rails.every(rail => rail.parentNode.buttons.length === 0), 'TV and touch browsers never create desktop controls');
  assert(excluded.rails.every(rail => !rail.handlers.scroll), 'excluded browsers receive no arrow handlers');
}
const desktop = screen({}, 900, {pointer: true});
const row = desktop.rails[0];
const [previous, next] = row.parentNode.buttons;
assert.equal(next.getAttribute('aria-label'), 'Next titles in Continue watching');
assert.equal(desktop.rails[1].parentNode.buttons[0].getAttribute('aria-label'), 'Previous titles in Season 1');
assert.equal(previous.getAttribute('aria-disabled'), 'true');
assert.equal(next.getAttribute('aria-disabled'), 'false');
assert.equal(next.style.top, '50px');
assert.equal(next.style.height, '190px');
next.focus();
const pageBeforeClick = desktop.window.pageYOffset;
next.handlers.click();
assert.equal(row.scrollLeft, 320, 'page scroll retains one thumbnail overlap');
assert.equal(desktop.document.activeElement, next, 'paging retains button focus');
assert.equal(desktop.window.pageYOffset, pageBeforeClick, 'paging does not scroll the page');
assert.equal(desktop.rails[1].scrollLeft, 0, 'shelves scroll independently');
for (let i = 0; i < 10; i++) next.handlers.click();
assert.equal(row.scrollLeft, 1800, 'repeated clicks stop at the last title');
assert.equal(next.getAttribute('aria-disabled'), 'true');
assert.equal(next.tabIndex, -1);
for (let i = 0; i < 10; i++) previous.handlers.click();
assert.equal(row.scrollLeft, 0, 'previous stops at the start');
row.scrollLeft = 500;
row.handlers.scroll();
assert.equal(previous.getAttribute('aria-disabled'), 'false', 'manual scrolling refreshes availability');
row.scrollLeft = 0;
row.clientWidth = row.scrollWidth;
desktop.resize();
assert.equal(previous.getAttribute('aria-disabled'), 'true');
assert.equal(next.getAttribute('aria-disabled'), 'true', 'fitting rows hide both arrows after resize');
const smooth = screen({}, 900, {pointer: true, smooth: true});
const smoothRow = smooth.rails[0];
const forward = smoothRow.parentNode.buttons[1];
forward.handlers.click();
forward.handlers.click();
assert.equal(smoothRow.lastRequest.left, 640, 'rapid clicks accumulate before smooth scrolling finishes');
assert.equal(smoothRow.lastRequest.behavior, 'smooth');
smoothRow.scrollLeft = 640;
smoothRow.handlers.scroll();
smoothRow.scrollLeft = 100;
smoothRow.handlers.wheel();
forward.handlers.click();
assert.equal(smoothRow.lastRequest.left, 420, 'manual input resets the queued destination');
const reduced = screen({}, 900, {pointer: true, smooth: true, reducedMotion: true});
reduced.rails[0].parentNode.buttons[1].handlers.click();
assert.equal(reduced.rails[0].scrollLeft, 320, 'reduced motion uses immediate scrolling');
assert.equal(reduced.rails[0].lastRequest, undefined);
console.log('Desktop shelf arrows passed: TV exclusion, paging, focus, boundaries, resize, smooth scrolling and reduced motion');
