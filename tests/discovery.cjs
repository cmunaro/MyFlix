// Trailer selection and lifecycle without network, browser, or production dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('myflix/static/discovery.js', 'utf8');
assert.match(fs.readFileSync('myflix/static/tv.css', 'utf8'), /\.trailer-idle[^{}]+\{[^}]*transition:\s*opacity 2s;/,
  'idle UI fades out over two seconds');

function screen(reduced = false, serverRendered = false) {
  const events = {}, windowEvents = {}, timers = new Map(), players = [], scripts = [];
  const classes = new Set();
  let nextTimer = 0, now = 0;
  function element(attrs = {}) {
    const children = [], listeners = {};
    return {hidden: false, listeners, children,
      classList: {add: value => classes.add(value), remove: value => classes.delete(value)},
      get firstChild() { return children[0]; },
      appendChild(child) { children.push(child); },
      removeChild(child) { children.splice(children.indexOf(child), 1); },
      setAttribute(key, value) { attrs[key] = value; },
      removeAttribute(key) { delete attrs[key]; },
      getAttribute(key) { return attrs[key] || null; },
      hasAttribute(key) { return Object.hasOwn(attrs, key); },
      addEventListener(name, fn) { listeners[name] = fn; }
    };
  }
  const panel = element(), picture = element(), title = element(), host = element(), status = element();
  const serverFrame = serverRendered ? element({'allow': 'autoplay; encrypted-media', 'tabindex': '-1'}) : null;
  if (serverFrame) {
    serverFrame.src = 'https://www.youtube-nocookie.com/embed/abcdefghijk?enablejsapi=1&autoplay=0';
    host.appendChild(serverFrame);
  }
  panel.hidden = true;
  const cards = ['abcdefghijk', '12345678901', ''].map((id, i) => element({
    'data-discovery': '', 'data-trailer': id, 'data-preview-title': `Title ${i}`,
    'data-preview-image': 'https://simkl.in/fanart/12/abc_medium.jpg'
  }));
  const document = {hidden: false, activeElement: null, body: element(),
    getElementById(id) { return {'discovery-preview': panel, 'preview-image': picture, 'preview-title': title, 'preview-video': host, 'preview-status': status, 'preview-frame': serverFrame}[id]; },
    querySelectorAll() { return cards; }, createElement() { return element(); },
    addEventListener(name, fn) { events[name] = fn; },
    head: {appendChild(script) { scripts.push(script); }}
  };
  const window = {location: {origin: 'http://myflix:8090'},
    matchMedia() { return {matches: reduced}; },
    setTimeout(fn, delay) { timers.set(++nextTimer, {fn, when: now + delay}); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(name, fn) { windowEvents[name] = fn; }
  };
  function loadAPI() {
    window.YT = {Player: function (mount, options) {
      assert.equal(mount.getAttribute('allow'), 'autoplay; encrypted-media', 'permissions set before player initialization');
      const instance = {options, muted: false, played: false, destroyed: false, stopped: 0, loads: [],
        videoId: /embed\/([^?]+)/.exec(mount.src)[1], frame: mount,
        destroy() { this.destroyed = true; }, getIframe() { return this.frame; },
        mute() { this.muted = true; }, unMute() { this.muted = false; },
        playVideo() { this.played = true; },
        getVideoUrl() { return 'https://www.youtube.com/watch?v=' + this.videoId; },
        stopVideo() { this.played = false; this.stopped += 1; },
        loadVideoById(id) { this.videoId = id; this.loads.push(id); this.played = true; }
      };
      players.push(instance);
      return instance;
    }};
    window.onYouTubeIframeAPIReady();
  }
  vm.runInNewContext(code, {document, window});
  return {document, window, panel, host, cards, players, scripts, classes, loadAPI, events, status, windowEvents, serverFrame,
    focus(index) { events.focusin({target: cards[index]}); },
    tick(ms = 1000) {
      const end = now + ms;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.when <= end).sort((a, b) => a[1].when - b[1].when)[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.when; timers.delete(id); timer.fn();
      }
      now = end;
    },
    ready(player) { player.options.events.onReady({target: player}); }
  };
}

const ui = screen();
assert.equal(ui.scripts.length, 1, 'API begins loading with the home page');
assert.equal(ui.host.children.length, 1, 'embed navigation starts before API download finishes');
const coldFrame = ui.host.firstChild;
const coldSource = coldFrame.src;
assert(coldSource.includes('autoplay=0'), 'parallel startup never plays hidden video');
ui.loadAPI();
const first = ui.players[0];
assert.equal(first.frame, coldFrame, 'API attaches to the already loading iframe');
assert.equal(first.frame.src, coldSource, 'attaching API does not restart embed navigation');
assert(first.frame.src.includes('/embed/abcdefghijk?enablejsapi=1'));
ui.ready(first);
assert(!first.played, 'warming the player never starts video or audio');
assert(ui.panel.hidden);
assert.equal(first.frame.getAttribute('tabindex'), '-1', 'iframe cannot take remote focus');
ui.focus(0);
ui.tick(249);
assert(!first.played, 'briefly crossed cards do not start playback');
ui.tick(1);
assert(first.played, 'ready player starts at 250 ms, not one second');
assert(!first.muted, 'sound is enabled by default');
first.options.events.onStateChange({data: 1});
assert(ui.classes.has('trailer-playing'));
assert(!ui.classes.has('trailer-idle'), 'playback starting does not skip the last-input grace period');
ui.tick(2749);
assert(!ui.classes.has('trailer-idle'), 'UI stays visible until three seconds after selection');
ui.tick(1);
assert(ui.classes.has('trailer-idle'), 'fade begins three seconds after selection, not playback');
ui.events.mousemove();
assert(!ui.classes.has('trailer-idle'), 'movement reveals browsing UI');
ui.tick(2999);
assert(!ui.classes.has('trailer-idle'), 'UI stays visible during interaction grace period');
ui.tick(1);
assert(ui.classes.has('trailer-idle'), 'idle playback hides focused cards again');
ui.focus(1);
assert(!first.played, 'changing selection immediately stops audio and video');
assert(!first.destroyed, 'changing title retains the initialized iframe');
first.options.events.onStateChange({data: 1});
assert(!ui.classes.has('trailer-playing'), 'old events cannot reveal a trailer during selection delay');
ui.tick(249);
assert.equal(first.loads.length, 0);
ui.tick(1);
assert.equal(ui.players.length, 1, 'second title reuses the same player');
assert.deepEqual(first.loads, ['12345678901']);
first.videoId = 'abcdefghijk';
first.options.events.onStateChange({data: 1});
first.options.events.onError({data: 150});
assert(!ui.classes.has('trailer-playing'), 'late old-video events cannot reveal or fail the new selection');
assert(ui.status.hidden);
first.videoId = '12345678901';
first.options.events.onAutoplayBlocked({});
assert(first.muted, 'blocked sound retries muted playback');
assert(ui.status.textContent.includes('Press a key or click for sound'));
first.options.events.onStateChange({data: 1});
assert(!ui.classes.has('trailer-idle'));
ui.tick(2750);
assert(ui.classes.has('trailer-idle'));
ui.events.keydown({keyCode: 37});
assert(!first.muted, 'remote gesture restores sound');
assert(!ui.classes.has('trailer-idle'), 'remote gesture restores UI without consuming input');
assert(ui.status.hidden);
first.options.events.onStateChange({data: 1});
assert(!ui.classes.has('trailer-idle'), 'playback event after interaction does not immediately hide UI');
first.options.events.onAutoplayBlocked({});
first.options.events.onAutoplayBlocked({});
assert(!ui.classes.has('trailer-idle'), 'fully blocked playback keeps UI visible');
assert(ui.status.textContent.includes('Autoplay blocked'));
ui.focus(2); ui.tick(250);
assert(!first.played, 'missing trailer stops old playback');
assert(ui.status.textContent.includes('No trailer available'));
assert.equal(ui.players.length, 1);
ui.focus(0); ui.tick(250);
assert(first.played);
ui.document.hidden = true;
ui.events.visibilitychange();
assert(!first.played, 'background tab stops video and sound');
assert(ui.panel.hidden);
ui.windowEvents.pagehide();
assert(first.destroyed, 'leaving the page releases the iframe');
first.options.events.onStateChange({data: 1});
assert(!ui.classes.has('trailer-playing'), 'callbacks after destruction cannot change UI');

const rapid = screen();
rapid.focus(0); rapid.tick(100);
rapid.focus(1);
rapid.loadAPI();
const warmed = rapid.players[0];
rapid.ready(warmed);
assert(!warmed.played, 'late API readiness respects the newest selection delay');
rapid.tick(249);
assert(!warmed.played);
rapid.tick(1);
assert(warmed.played);
assert.equal(warmed.videoId, '12345678901');
for (let i = 0; i < 20; i += 1) { rapid.focus(i % 2); rapid.tick(250); }
assert.equal(rapid.players.length, 1, 'browsing many titles never reconstructs a ready player');

const motion = screen(true);
motion.focus(0); motion.tick();
assert.equal(motion.scripts.length, 0, 'reduced motion prevents prewarming and autoplay');
assert.equal(motion.host.children.length, 0, 'reduced motion also prevents early iframe loading');
assert(!motion.panel.hidden);
// Observers receive no input-cancelling methods: calling preventDefault would fail above.
const pointer = screen();
pointer.focus(0);
assert(!pointer.panel.hidden);
assert(!pointer.cards[1].listeners.mouseenter, 'hover cannot start a different trailer behind the focused selection');
pointer.events.focusout({relatedTarget: null}); pointer.tick(250);
pointer.loadAPI(); pointer.ready(pointer.players[0]);
assert(pointer.panel.hidden);
assert(!pointer.players[0].played, 'leaving selection cancels pending playback even if API loads later');

const unavailable = screen();
unavailable.focus(0); unavailable.loadAPI();
unavailable.tick(15000);
assert(unavailable.players[0].destroyed, 'unresponsive iframe times out to artwork');
assert(unavailable.status.textContent.includes('could not load'));
assert(!unavailable.classes.has('trailer-idle'));
unavailable.focus(1); unavailable.tick(250);
const replacement = unavailable.players[1];
unavailable.ready(unavailable.players[0]);
assert(!replacement.played, 'late readiness from destroyed iframe cannot start a replacement');
unavailable.ready(replacement);
assert(replacement.played);

const errors = screen();
errors.focus(0); errors.loadAPI(); errors.ready(errors.players[0]); errors.tick(250);
const reusable = errors.players[0];
reusable.options.events.onError({data: 150});
assert(!reusable.played);
assert(errors.status.textContent.includes('unavailable'));
assert(!errors.classes.has('trailer-idle'));
errors.focus(1); errors.tick(250);
assert.equal(errors.players.length, 1, 'unavailable video does not discard a healthy player');
assert(reusable.played);
const delayed = screen();
delayed.loadAPI(); delayed.ready(delayed.players[0]); delayed.focus(0);
delayed.tick(3500);
assert(!delayed.classes.has('trailer-idle'), 'elapsed idle time cannot hide UI while trailer is still loading');
delayed.events.mousemove();
delayed.tick(1000);
delayed.players[0].options.events.onStateChange({data: 1});
assert(!delayed.classes.has('trailer-idle'), 'input during loading resets the deadline');
delayed.tick(1999);
assert(!delayed.classes.has('trailer-idle'));
delayed.tick(1);
assert(delayed.classes.has('trailer-idle'), 'deadline is three seconds from last input, not video start');
delayed.players[0].options.events.onStateChange({data: 3});
assert(!delayed.classes.has('trailer-idle'), 'buffering reveals UI');
delayed.tick(4000);
delayed.players[0].options.events.onStateChange({data: 1});
assert(delayed.classes.has('trailer-idle'), 'late playback fades without adding another three-second delay');
delayed.focus(2); delayed.tick(5000);
assert(!delayed.classes.has('trailer-idle'), 'an item without a trailer never fades the UI');
const early = screen(false, true);
const originalURL = early.serverFrame.src;
assert.equal(early.host.children.length, 1, 'existing HTML iframe is adopted without duplication');
early.loadAPI(); early.ready(early.players[0]);
assert.equal(early.players[0].frame, early.serverFrame, 'API reuses the iframe loaded by the HTML parser');
assert.equal(early.serverFrame.src, originalURL, 'initializing previews preserves the in-flight iframe request');
assert(!early.players[0].played, 'eager HTML loading does not autoplay');
early.focus(0); early.tick(250);
assert(early.players[0].played);
const earlyReduced = screen(true, true);
assert.equal(earlyReduced.host.children.length, 0, 'reduced motion removes the preloaded iframe and disables previews');
assert.equal(earlyReduced.scripts.length, 0);
const abandoned = screen();
abandoned.windowEvents.pagehide();
assert.equal(abandoned.host.children.length, 0);
abandoned.loadAPI();
assert.equal(abandoned.players.length, 0, 'late API download cannot restart a closed page');
assert.equal(abandoned.host.children.length, 0);
const noAPI = screen();
noAPI.focus(0); noAPI.tick(15000);
assert.equal(noAPI.host.children.length, 0, 'SDK timeout removes the early iframe');
assert(noAPI.status.textContent.includes('could not load'));
console.log('Discovery previews passed: parallel cold start, 250 ms delay, player reuse, rapid selection, stale events, sound, UI fade, timeout, errors, reduced motion');
