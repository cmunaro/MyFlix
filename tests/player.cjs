const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function screen(blockFirstPlay = false, options = {}) {
  const elements = {}, timers = new Map(), requests = [], beacons = [], loads = [], fullscreenRequests = [];
  let document, now = 0, timerId = 0;
  function element(id, parent = null) {
    const attrs = {}, listeners = {}, symbols = {};
    const el = {id, parentNode: parent, hidden: false, disabled: false, offsetWidth: 54, offsetHeight: 54,
      addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
      emit(name, event = {}) { event.target ||= this; event.type ||= name; (listeners[name] || []).forEach(fn => fn(event)); },
      setAttribute(key, value) { attrs[key] = String(value); }, getAttribute(key) { return attrs[key] ?? null; },
      hasAttribute(key) { return Object.hasOwn(attrs, key); },
      removeAttribute(key) { delete attrs[key]; },
      contains(target) { while (target) { if (target === this) return true; target = target.parentNode; } return false; },
      focus() { document.activeElement = this; document.emit('focusin', {target: this}); },
      click() { if (!this.disabled && this.onclick) this.onclick({preventDefault() {}}); },
      querySelector(selector) { return symbols[selector] ||= {hidden: false}; }
    };
    elements[id] = el; return el;
  }
  document = element('document');
  const classes = new Set(['tv-player', 'controls-visible']);
  document.body = {classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)}};
  const shell = element('player-shell');
  const player = element('player', shell);
  const header = element('header', shell), overlay = element('overlay', shell);
  const center = element('center-play', shell);
  element('back', header);
  element('player-status', shell);
  for (const id of ['toggle-play', 'rewind', 'forward', 'mute', 'next-episode', 'fullscreen', 'timeline', 'remaining-time', 'playback-progress']) element(id, overlay);
  const upNext = element('up-next', shell);
  const nextId = options.nextId === undefined ? '2' : options.nextId;
  upNext.setAttribute('data-next-id', nextId);
  upNext.hidden = true;
  element('next-prompt', upNext);
  for (const id of ['next-episode-info', 'episode-heading', 'episode-title', 'playing-title']) element(id, shell);
  elements['next-episode'].hidden = !nextId;
  const controls = ['back', 'center-play', 'timeline', 'toggle-play', 'rewind', 'forward', 'mute', 'next-episode', 'fullscreen', 'next-prompt'].map(id => elements[id]);
  shell.querySelectorAll = selector => selector === '[data-player-overlay]' ? [header, center, overlay] : controls;
  document.getElementById = id => elements[id] || null;
  document.querySelector = () => ({content: 'csrf-token'});
  document.querySelectorAll = () => [];
  document.exitFullscreen = () => { document.fullscreenElement = null; document.emit('fullscreenchange'); };
  function requestFullscreen(options) {
    fullscreenRequests.push(options);
    document.fullscreenElement = shell; document.emit('fullscreenchange');
  }
  shell.requestFullscreen = requestFullscreen;
  if (options.prefixedFullscreen) {
    delete shell.requestFullscreen;
    shell.webkitRequestFullscreen = function () { fullscreenRequests.push(arguments.length); document.webkitFullscreenElement = shell; document.emit('webkitfullscreenchange'); };
    delete document.exitFullscreen;
    document.webkitExitFullscreen = () => { document.webkitFullscreenElement = null; document.emit('webkitfullscreenchange'); };
  }
  if (options.fullscreenDenied) { shell.requestFullscreen = () => { fullscreenRequests.push('denied'); throw Error('Denied'); }; }
  Object.assign(player, {paused: true, currentTime: 0, duration: 1000, controls: true, muted: false, playCalls: 0, readyState: options.cachedMetadata ? 1 : 0, loadCalls: 0});
  player.setAttribute('data-id', '1'); player.setAttribute('data-watched', '0'); player.setAttribute('data-position', '120');
  player.play = () => {
    player.playCalls += 1;
    if (blockFirstPlay && player.playCalls === 1) return {catch: fn => fn()};
    player.paused = false; player.emit('play'); return {catch() {}};
  };
  player.pause = () => { player.paused = true; player.emit('pause'); };
  const emit = player.emit.bind(player);
  player.emit = (name, event) => { if (name === 'loadedmetadata') player.readyState = 1; emit(name, event); };
  player.load = () => { player.loadCalls += 1; player.readyState = 0; player.paused = true; player.ended = false; player.currentTime = 0; player.emit('loadedmetadata'); };
  const location = {href: '/watch/1', pathname: '/watch/1', search: ''};
  const window = {addEventListener: (name, fn) => document.addEventListener(name, fn),
    setTimeout(fn, delay) { timers.set(++timerId, {fn, at: now + delay}); return timerId; },
    clearTimeout: id => timers.delete(id), history: {replaceState(a, b, url) { location.href = url; }}};
  function XHR() {
    this.open = (method, url) => { this.method = method; this.url = url; };
    this.setRequestHeader = () => {};
    this.respond = () => {
      this.status = options.nextError ? 404 : 200;
      this.responseText = JSON.stringify({id: 2, title: 'Second episode', series: 'Show', season: 1, episode: 2,
        position: 0, stream_url: '/media/2', watch_url: '/watch/2', next: null});
      this.onload();
    };
    this.send = body => {
      if (this.method === 'GET') { loads.push(this); if (!options.deferNext) this.respond(); }
      else { requests.push({...JSON.parse(body), url: this.url}); this.status = 200; this.onload(); }
    };
  }
  const context = {document, window, location, navigator: {userAgent: options.userAgent || '', sendBeacon: (...args) => beacons.push(args)}, XMLHttpRequest: XHR,
    FormData: class { append() {} }};
  // Run both scripts: only the player may handle its keys, including fullscreen.
  vm.runInNewContext(fs.readFileSync('myflix/static/tv.js', 'utf8'), context);
  vm.runInNewContext(fs.readFileSync('myflix/static/player.js', 'utf8'), context);
  return {elements, player, document, classes, requests, beacons, location, loads, fullscreenRequests,
    tick(ms) { now += ms; for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); } },
    key(keyCode) { let prevented = false; document.emit('keydown', {keyCode, preventDefault() { prevented = true; }}); return prevented; }
  };
}
const ui = screen();
const {player, elements, classes, document} = ui;
assert.equal(player.controls, false, 'native controls are disabled when custom controls own playback');
player.emit('loadedmetadata');
assert.equal(player.currentTime, 120, 'resume position is restored');
assert.equal(player.paused, false, 'play starts after metadata is ready');
elements.rewind.click();
assert.equal(player.currentTime, 110, 'pointer rewind button seeks ten seconds');
elements.forward.click();
assert.equal(player.currentTime, 120, 'pointer forward button seeks ten seconds');
elements.forward.focus();
assert(ui.key(13), 'remote OK explicitly activates the forward button');
assert.equal(player.currentTime, 130);
elements.rewind.focus();
ui.key(13);
assert.equal(player.currentTime, 120, 'remote OK activates rewind exactly once');
elements['toggle-play'].focus();
ui.tick(3000);
assert(!classes.has('controls-visible'), 'controls fade while a button had focus');
assert.equal(document.activeElement, player, 'hidden controls return focus to the video');
assert.equal(elements.overlay.getAttribute('aria-hidden'), 'true');
assert.equal(elements['toggle-play'].getAttribute('tabindex'), '-1', 'hidden buttons leave tab order');
player.emit('timeupdate');
assert(!classes.has('controls-visible'), 'progress saves do not reveal the overlay');
assert.equal(ui.requests.at(-1).position, 120, 'resume saves remain functional');
ui.key(39);
assert.equal(player.currentTime, 130, 'remote Right seeks ten seconds');
assert(classes.has('controls-visible'), 'remote input reveals controls');
ui.tick(3000);
ui.key(32);
assert.equal(player.paused, true, 'Space pauses');
ui.tick(5000);
assert(classes.has('controls-visible'), 'pause keeps playback actions accessible');
assert.equal(elements['center-play'].hidden, false);
ui.key(13);
assert.equal(player.paused, false, 'OK resumes');
elements.fullscreen.click();
assert.equal(document.fullscreenElement, elements['player-shell'], 'fullscreen includes the custom overlay');
elements.mute.focus();
assert.equal(ui.key(13), true, 'remote OK activates the focused control without a duplicate native click');
assert.equal(player.paused, false, 'OK on another fullscreen control does not toggle playback');
assert.equal(player.muted, true);
ui.tick(3000);
assert.equal(document.activeElement, player, 'fullscreen controls also release focus on timeout');
ui.key(27);
assert.equal(document.fullscreenElement, null, 'Back exits fullscreen first');
ui.key(27);
assert(!classes.has('controls-visible'), 'Back dismisses the overlay before leaving');
ui.key(27);
assert.equal(ui.location.href, '/');
const blocked = screen(true);
blocked.player.emit('loadedmetadata');
assert.equal(blocked.player.paused, true, 'blocked autoplay remains paused');
assert(blocked.classes.has('controls-visible'));
blocked.elements['center-play'].click();
assert.equal(blocked.player.paused, false, 'central Play works after blocked autoplay');

const cached = screen(false, {cachedMetadata: true});
assert.equal(cached.player.currentTime, 120, 'cached metadata initializes resume without waiting for an event');
assert.equal(cached.player.playCalls, 1);
cached.elements.forward.click();
assert.equal(cached.player.currentTime, 130, 'seek buttons work when metadata arrived before the script');
cached.player.emit('loadedmetadata');
assert.equal(cached.player.currentTime, 130, 'a late metadata event must not reset a user seek');
cached.player.currentTime = 5;
cached.elements.rewind.click();
assert.equal(cached.player.currentTime, 0, 'rewind clamps at the start');
cached.player.currentTime = 995;
cached.elements.forward.click();
assert.equal(cached.player.currentTime, 1000, 'forward clamps at the end');

const prompt = screen(false, {cachedMetadata: true});
prompt.player.currentTime = 969;
prompt.player.emit('timeupdate');
assert.equal(prompt.elements['up-next'].hidden, true);
prompt.player.currentTime = 970;
prompt.player.emit('timeupdate');
assert.equal(prompt.elements['up-next'].hidden, false, 'up-next prompt appears in the final 30 seconds');
prompt.tick(3000);
assert.equal(prompt.elements['up-next'].hidden, false, 'up-next stays available when normal controls hide');
assert.notEqual(prompt.elements['next-prompt'].getAttribute('tabindex'), '-1');
prompt.player.currentTime = 900;
prompt.player.emit('timeupdate');
assert.equal(prompt.elements['up-next'].hidden, true, 'rewinding away from the end dismisses the prompt');
prompt.player.currentTime = 970;
prompt.player.emit('timeupdate');
prompt.elements.fullscreen.click();
prompt.key(40);
assert.equal(prompt.document.activeElement, prompt.elements['next-prompt'], 'remote Down reaches up-next');
prompt.key(13);
assert.equal(prompt.player.getAttribute('data-id'), '2');
assert.equal(prompt.player.src, '/media/2');
assert.equal(prompt.player.paused, false, 'selecting up-next starts the episode');
assert.equal(prompt.location.href, '/watch/2');
assert.equal(prompt.document.fullscreenElement, prompt.elements['player-shell'], 'episode transitions preserve fullscreen');

const autoplay = screen(false, {cachedMetadata: true, deferNext: true});
autoplay.player.currentTime = 1000; autoplay.player.paused = true; autoplay.player.ended = true;
autoplay.player.emit('ended');
autoplay.player.emit('ended');
assert.equal(autoplay.loads.length, 1, 'end events queue one automatic transition');
assert(autoplay.requests.some(r => r.url === '/api/progress/1' && r.watched === true), 'completed episode is saved as watched');
autoplay.loads[0].respond();
assert.equal(autoplay.player.getAttribute('data-id'), '2');
assert.equal(autoplay.player.paused, false, 'the next episode plays automatically in the same video element');
autoplay.player.emit('timeupdate');
assert.equal(autoplay.requests.at(-1).url, '/api/progress/2', 'progress follows the new episode');
assert.equal(autoplay.elements['next-episode'].hidden, true, 'final episode has no next control');
autoplay.player.currentTime = 1000; autoplay.player.paused = true; autoplay.player.ended = true;
autoplay.player.emit('ended');
assert.equal(autoplay.loads.length, 1, 'final episode stops rather than looping');

const failed = screen(false, {cachedMetadata: true, nextError: true});
failed.player.currentTime = 1000; failed.player.paused = true; failed.player.ended = true;
failed.player.emit('ended'); failed.player.emit('ended');
assert.equal(failed.loads.length, 1, 'failed automatic transitions are not retried in a loop');
assert.equal(failed.player.getAttribute('data-id'), '1');
assert.match(failed.elements['player-status'].textContent, /Could not load/);
const film = screen(false, {cachedMetadata: true, nextId: ''});
film.player.currentTime = 995; film.player.emit('timeupdate'); film.player.emit('ended');
assert.equal(film.elements['up-next'].hidden, true, 'films have no episode prompt or autoplay');
assert.equal(film.loads.length, 0);

const tv = screen(false, {cachedMetadata: true, userAgent: 'Mozilla/5.0 (SmartTV; Linux; Hisense VIDAA)'});
assert(tv.classes.has('tv-remote'), 'TV pages enable remote mode and its cursor suppression');
assert.equal(tv.fullscreenRequests.length, 0, 'fullscreen waits for a user gesture');
tv.key(39);
assert.equal(tv.document.fullscreenElement, tv.elements['player-shell']);
assert.equal(tv.fullscreenRequests[0].navigationUI, 'hide', 'TV fullscreen requests hidden browser navigation');
tv.tick(3000);
tv.document.emit('mousemove');
assert(!tv.classes.has('controls-visible'), 'virtual-pointer motion does not wake playback chrome');
tv.key(27);
assert.equal(tv.document.fullscreenElement, null, 'Back exits fullscreen without immediately entering it again');
assert.equal(tv.fullscreenRequests.length, 1);
tv.document.emit('click', {target: tv.elements.fullscreen});
tv.elements.fullscreen.click();
assert.equal(tv.fullscreenRequests.length, 2, 'explicit fullscreen toggles are not duplicated by automatic handling');

const prefixed = screen(false, {cachedMetadata: true, userAgent: 'Hisense VIDAA', prefixedFullscreen: true});
prefixed.key(13);
assert.equal(prefixed.document.webkitFullscreenElement, prefixed.elements['player-shell']);
assert.equal(prefixed.fullscreenRequests[0], 0, 'legacy fullscreen APIs receive no unsupported options');
prefixed.key(27);
assert.equal(prefixed.document.webkitFullscreenElement, null);
const denied = screen(false, {cachedMetadata: true, userAgent: 'Hisense VIDAA', fullscreenDenied: true});
assert.doesNotThrow(() => denied.key(39));
assert.equal(denied.player.currentTime, 130, 'unsupported fullscreen does not break remote seeking');
const desktop = screen(false, {cachedMetadata: true});
desktop.key(39);
assert.equal(desktop.fullscreenRequests.length, 0, 'desktop playback does not enter fullscreen automatically');
desktop.tick(3000); desktop.document.emit('mousemove');
assert(desktop.classes.has('controls-visible'), 'desktop mouse interaction still reveals controls');
console.log('Player behavior passed: seeking with cached metadata and remote OK, auto-hide, resume, fullscreen, up-next, autoplay and transition failures');
