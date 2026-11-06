const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function screen(blockFirstPlay = false, options = {}) {
  const elements = {}, timers = new Map(), requests = [], beacons = [], loads = [], fullscreenRequests = [];
  let document, now = 0, timerId = 0, created = 0;
  function element(id, parent = null) {
    const attrs = {}, listeners = {}, symbols = {};
    const el = {id, parentNode: parent, children: [], hidden: false, disabled: false, offsetWidth: 54, offsetHeight: 54,
      addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
      removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter(listener => listener !== fn); },
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      set textContent(value) { this.children.forEach(child => { child.parentNode = null; }); this.children = []; this._text = value; },
      get textContent() { return this._text || ''; },
      emit(name, event = {}) { event.target ||= this; event.type ||= name; (listeners[name] || []).forEach(fn => fn(event)); },
      setAttribute(key, value) { attrs[key] = String(value); }, getAttribute(key) { return attrs[key] ?? null; },
      hasAttribute(key) { return Object.hasOwn(attrs, key); },
      removeAttribute(key) { delete attrs[key]; },
      contains(target) { while (target) { if (target === this) return true; target = target.parentNode; } return false; },
      getBoundingClientRect() { return {left: 100, width: 800}; },
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
  element('player-loading', shell).hidden = true;
  for (const id of ['toggle-play', 'rewind', 'forward', 'mute', 'track-settings', 'next-episode', 'fullscreen', 'timeline', 'remaining-time', 'playback-progress']) element(id, overlay);
  const trackPanel = element('track-panel', shell); trackPanel.hidden = true;
  const audioGroup = element('audio-track-group', trackPanel), subtitleGroup = element('subtitle-track-group', trackPanel);
  const audioOptions = element('audio-track-options', audioGroup), subtitleOptions = element('subtitle-track-options', subtitleGroup);
  const closeTracks = element('close-tracks', trackPanel);
  element('track-message', trackPanel);
  trackPanel.querySelectorAll = () => [...audioOptions.children, ...subtitleOptions.children, closeTracks];
  const upNext = element('up-next', shell);
  const nextId = options.nextId === undefined ? '2' : options.nextId;
  upNext.setAttribute('data-next-id', nextId);
  upNext.hidden = true;
  element('next-prompt', upNext);
  for (const id of ['next-episode-info', 'episode-heading', 'episode-title', 'playing-title']) element(id, shell);
  elements['next-episode'].hidden = !nextId;
  const controls = ['back', 'center-play', 'timeline', 'toggle-play', 'rewind', 'forward', 'mute', 'track-settings', 'next-episode', 'fullscreen', 'next-prompt', 'close-tracks'].map(id => elements[id]);
  shell.querySelectorAll = selector => selector === '[data-player-overlay]' ? [header, center, overlay, trackPanel] : controls;
  document.createElement = () => element('created-' + ++created);
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
  const buffered = options.buffered || [[0, 200]];
  player.buffered = {length: buffered.length, start: i => buffered[i][0], end: i => buffered[i][1]};
  player.seekable = {length: 1, start: () => 0, end: () => 200};
  function trackList(items) {
    if (!items) return undefined;
    const list = element('track-list-' + ++created);
    list.length = items.length; items.forEach((track, index) => { list[index] = track; });
    return list;
  }
  player.audioTracks = trackList(options.audioTracks);
  player.textTracks = trackList(options.textTracks);
  player.setAttribute('data-id', '1'); player.setAttribute('data-watched', '0'); player.setAttribute('data-position', '120');
  player.play = () => {
    player.playCalls += 1;
    if (blockFirstPlay && player.playCalls === 1) return {catch: fn => fn()};
    player.paused = false; player.emit('play'); return {catch() {}};
  };
  player.pause = () => { player.paused = true; player.emit('pause'); };
  const emit = player.emit.bind(player);
  player.emit = (name, event) => { if (name === 'loadedmetadata') player.readyState = 1; emit(name, event); };
  player.load = () => {
    player.loadCalls += 1; player.readyState = 0; player.paused = true; player.ended = false; player.currentTime = 0;
    player.emit('emptied');
    if (options.nextTracks) {
      player.audioTracks = trackList(options.nextTracks.audioTracks); player.textTracks = trackList(options.nextTracks.textTracks);
    }
    player.emit('loadedmetadata');
  };
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
    key(keyCode, shiftKey = false) { let prevented = false; document.emit('keydown', {keyCode, shiftKey, preventDefault() { prevented = true; }}); return prevented; }
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

const mouseSeek = screen(false, {cachedMetadata: true, nextId: ''});
const seekBar = mouseSeek.elements.timeline;
function mouseDown(ui, clientX, extra = {}) {
  let prevented = false;
  ui.elements.timeline.emit('mousedown', {clientX, button: 0, detail: 1,
    preventDefault() { prevented = true; }, ...extra});
  return prevented;
}
function clickTimeline(clientX, extra = {}) {
  const prevented = mouseDown(mouseSeek, clientX, extra);
  mouseSeek.document.emit('mouseup', {clientX, button: 0, ...extra});
  // Firefox can consume click entirely; the seek must already be complete.
  return prevented;
}
assert.equal(clickTimeline(300), true, 'mouse-down cancels the native small-step range action');
assert.equal(mouseSeek.player.currentTime, 250, 'desktop track clicks jump directly to the chosen fraction');
assert.equal(seekBar.value, 250);
assert.equal(mouseSeek.elements['playback-progress'].value, 250, 'progress immediately reflects the seek');
assert.equal(mouseSeek.requests.at(-1).position, 250, 'clicked position is saved');
assert.equal(mouseSeek.player.paused, false, 'seeking preserves active playback');
mouseSeek.tick(3000);
assert(!mouseSeek.classes.has('controls-visible'), 'controls still hide after a mouse seek');
mouseSeek.player.pause();
clickTimeline(700);
assert.equal(mouseSeek.player.currentTime, 750, 'a paused video can also seek');
assert.equal(mouseSeek.player.paused, true, 'clicking the timeline does not resume a paused video');
clickTimeline(0);
assert.equal(mouseSeek.player.currentTime, 0, 'timeline coordinates clamp at the start');
clickTimeline(1000);
assert.equal(mouseSeek.player.currentTime, 1000, 'timeline coordinates clamp at the end');
clickTimeline(300, {button: 2});
seekBar.emit('click', {clientX: 300, button: 0, detail: 0});
assert.equal(mouseSeek.player.currentTime, 1000, 'secondary and keyboard-generated clicks do not seek');
mouseSeek.player.duration = Infinity;
clickTimeline(300);
assert.equal(mouseSeek.player.currentTime, 1000, 'unknown duration cannot produce an invalid seek');
mouseSeek.player.duration = 1000;
seekBar.getBoundingClientRect = () => ({left: 100, width: 0});
clickTimeline(300);
assert.equal(mouseSeek.player.currentTime, 1000, 'a hidden or collapsed timeline is ignored');
const beforeMetadata = screen();
assert.equal(mouseDown(beforeMetadata, 300), false);
assert.equal(beforeMetadata.player.currentTime, 0, 'clicks before metadata cannot seek');
const beforeTVClick = tv.player.currentTime;
assert.equal(mouseDown(tv, 300), false);
assert.equal(tv.player.currentTime, beforeTVClick, 'TV playback receives no desktop click-to-seek handler');
const unbuffered = screen(false, {cachedMetadata: true});
const waitingOverlay = unbuffered.elements['player-loading'];
assert.equal(mouseDown(unbuffered, 700), true);
unbuffered.document.emit('mouseup', {clientX: 700});
assert.equal(unbuffered.player.currentTime, 750, 'seek goes beyond both the buffered and reported seekable ranges');
assert.equal(unbuffered.elements['playback-progress'].value, 750, 'unbuffered target is shown immediately');
assert.equal(waitingOverlay.hidden, false, 'unbuffered seek immediately shows Loading');
assert.equal(unbuffered.player.getAttribute('aria-busy'), 'true');
unbuffered.player.seeking = true;
unbuffered.player.emit('seeking');
unbuffered.tick(3000);
assert.equal(waitingOverlay.hidden, false, 'loading survives the controls idle timeout');
assert.equal(unbuffered.elements.timeline.disabled, false, 'metadata-only readiness allows another seek during buffering');
mouseDown(unbuffered, 820);
unbuffered.document.emit('mouseup', {clientX: 820});
assert.equal(unbuffered.player.currentTime, 900, 'a newer unbuffered target replaces the previous one');
unbuffered.player.readyState = 2;
unbuffered.player.emit('canplay');
assert.equal(waitingOverlay.hidden, false, 'an earlier readiness event cannot dismiss a seek still in progress');
unbuffered.player.seeking = false;
unbuffered.player.emit('seeked');
assert.equal(waitingOverlay.hidden, true, 'the overlay clears when the requested frame is ready');
assert.equal(unbuffered.player.getAttribute('aria-busy'), 'false');
assert.equal(unbuffered.player.paused, false, 'buffering retains the playing state');
unbuffered.player.emit('waiting');
assert.equal(waitingOverlay.hidden, false, 'a playback buffer underrun also shows loading');
unbuffered.player.emit('playing');
assert.equal(waitingOverlay.hidden, true);
unbuffered.player.emit('waiting');
unbuffered.player.pause();
assert.equal(waitingOverlay.hidden, true, 'pausing an ordinary buffer underrun clears loading');
unbuffered.elements.timeline.value = 600;
unbuffered.elements.timeline.oninput();
assert.equal(unbuffered.player.currentTime, 600, 'dragging also seeks outside buffered data');
assert.equal(waitingOverlay.hidden, false, 'a paused seek shows loading until its frame arrives');
unbuffered.player.emit('seeked');
assert.equal(waitingOverlay.hidden, true);
assert.equal(unbuffered.player.paused, true, 'a paused seek does not start playback');
unbuffered.elements.timeline.value = 100;
unbuffered.elements.timeline.oninput();
assert.equal(waitingOverlay.hidden, true, 'already buffered seeks do not leave a loading overlay');
unbuffered.elements.forward.click();
assert.equal(waitingOverlay.hidden, true, 'buffered remote seeks use the same path');
unbuffered.elements.timeline.value = 700;
unbuffered.elements.timeline.oninput();
unbuffered.player.emit('error');
assert.equal(waitingOverlay.hidden, true, 'playback failures replace loading with the error message');
assert.equal(unbuffered.elements['player-status'].hidden, false);
unbuffered.elements.timeline.oninput();
unbuffered.player.emit('emptied');
assert.equal(waitingOverlay.hidden, true, 'changing the source clears the old buffering state');

const dragSeek = screen(false, {cachedMetadata: true});
assert.equal(mouseDown(dragSeek, 300), true);
assert.equal(dragSeek.player.currentTime, 250, 'the mouse-down position is applied before any native input');
dragSeek.document.emit('mousemove', {clientX: 580, preventDefault() {}});
assert.equal(dragSeek.player.currentTime, 600, 'dragging is mapped directly even without native input events');
dragSeek.tick(3000);
assert(dragSeek.classes.has('controls-visible'), 'dragging keeps controls visible');
dragSeek.document.emit('mouseup', {clientX: 660});
assert.equal(dragSeek.player.currentTime, 700, 'release uses the final coordinate even outside the thumb');
assert.equal(dragSeek.requests.at(-1).position, 700, 'dragged position is saved');
dragSeek.document.emit('mousemove', {clientX: 300, preventDefault() {}});
assert.equal(dragSeek.player.currentTime, 700, 'moving after release no longer seeks');
dragSeek.tick(3000);
assert(!dragSeek.classes.has('controls-visible'), 'controls hide again after dragging');
const touchSeek = screen(false, {cachedMetadata: true});
touchSeek.elements.timeline.emit('touchstart');
touchSeek.elements.timeline.value = 600;
touchSeek.elements.timeline.oninput();
touchSeek.document.emit('touchend');
assert.equal(touchSeek.player.currentTime, 600, 'touch input still uses the native range value');
assert.equal(touchSeek.requests.at(-1).position, 600);

assert(ui.elements['track-settings'].hidden, 'unsupported native track APIs do not expose a broken chooser');
const singleAudio = screen(false, {cachedMetadata: true, audioTracks: [{language: 'en', enabled: true}], textTracks: []});
assert(singleAudio.elements['track-settings'].hidden, 'a single audio track without subtitles offers no selection');
const english = {label: 'English', language: 'en', enabled: true};
const italian = {label: 'Italiano', language: 'it', enabled: false};
const metadata = {kind: 'metadata', label: 'Metadata', mode: 'hidden'};
const captions = {kind: 'captions', language: 'en', mode: 'disabled'};
const subtitles = {kind: 'subtitles', label: 'Italiano', language: 'it', mode: 'showing'};
const tracksUI = screen(false, {cachedMetadata: true, audioTracks: [english, italian], textTracks: [metadata, captions, subtitles]});
assert.equal(subtitles.mode, 'disabled', 'cached native/default subtitles start off');
assert.equal(metadata.mode, 'hidden', 'subtitle defaults do not affect metadata tracks');
const trackElements = tracksUI.elements;
const audioChoices = () => trackElements['audio-track-options'].children;
const subtitleChoices = () => trackElements['subtitle-track-options'].children;
assert(!trackElements['track-settings'].hidden, 'cached native tracks enable the chooser');
trackElements.fullscreen.click(); trackElements.mute.focus(); tracksUI.key(39);
assert.equal(tracksUI.document.activeElement, trackElements['track-settings'], 'remote Right reaches track settings');
tracksUI.key(13);
assert(!trackElements['track-panel'].hidden);
assert.equal(tracksUI.document.activeElement, audioChoices()[0]);
assert.equal(audioChoices()[0].textContent, 'English (en)');
assert.equal(audioChoices()[0].getAttribute('aria-pressed'), 'true');
tracksUI.key(40); tracksUI.key(13);
assert.equal(english.enabled, false); assert.equal(italian.enabled, true);
assert.equal(tracksUI.player.paused, false, 'selecting audio does not pause playback');
assert.equal(tracksUI.player.currentTime, 120, 'selecting audio does not seek or reload');
assert.equal(tracksUI.player.loadCalls, 0);
assert.equal(tracksUI.document.activeElement, audioChoices()[1], 'selection retains focus after rebuilding choices');
tracksUI.player.audioTracks.emit('change');
assert.equal(tracksUI.document.activeElement, audioChoices()[1], 'native change events preserve focused choice');
assert.equal(subtitleChoices().length, 3, 'only captions/subtitles appear, plus Off');
assert.equal(subtitleChoices()[0].getAttribute('aria-pressed'), 'true', 'Off is selected initially');
subtitleChoices()[1].focus(); tracksUI.key(32);
assert.equal(captions.mode, 'showing'); assert.equal(subtitles.mode, 'disabled');
tracksUI.player.emit('loadedmetadata');
assert.equal(captions.mode, 'showing', 'late metadata does not undo a manual subtitle choice');
assert.equal(metadata.mode, 'hidden', 'subtitle selection leaves metadata tracks alone');
assert.equal(tracksUI.player.paused, false, 'Space selects a menu item rather than toggling playback');
subtitleChoices()[0].click();
assert.equal(captions.mode, 'disabled'); assert.equal(subtitles.mode, 'disabled');
trackElements['close-tracks'].focus(); tracksUI.key(9);
assert.equal(tracksUI.document.activeElement, audioChoices()[0], 'Tab remains within track dialog');
tracksUI.key(9, true);
assert.equal(tracksUI.document.activeElement, trackElements['close-tracks']);
tracksUI.key(27);
assert(trackElements['track-panel'].hidden, 'Back closes the chooser first');
assert.equal(tracksUI.document.fullscreenElement, trackElements['player-shell'], 'closing track menu preserves fullscreen');
assert.equal(tracksUI.document.activeElement, trackElements['track-settings']);
trackElements['track-settings'].click(); subtitleChoices()[2].click(); tracksUI.tick(3000);
assert(trackElements['track-panel'].hidden, 'idle playback closes track menu even with a focused choice');
assert(!tracksUI.classes.has('controls-visible'));
assert.equal(tracksUI.document.activeElement, tracksUI.player);
assert.equal(subtitles.mode, 'showing', 'hiding controls does not disable selected subtitles');
trackElements['track-settings'].click(); trackElements['close-tracks'].click();
assert(trackElements['track-panel'].hidden, 'pointer Done closes chooser');

const late = screen(false, {cachedMetadata: true, audioTracks: [{enabled: true}], textTracks: []});
late.tick(3000);
late.player.audioTracks[1] = {enabled: false}; late.player.audioTracks.length = 2; late.player.audioTracks.emit('addtrack');
assert(!late.elements['track-settings'].hidden, 'tracks discovered after metadata enable the chooser');
assert(!late.classes.has('controls-visible'), 'track discovery does not wake idle controls');
late.elements['track-settings'].click(); late.player.audioTracks.length = 0; late.player.audioTracks.emit('removetrack');
assert(late.elements['track-settings'].hidden); assert(late.elements['track-panel'].hidden);
assert.equal(late.document.activeElement, late.player, 'removed tracks never leave focus in a hidden chooser');

const lockedAudio = {label: 'Unsupported', get enabled() { return false; }, set enabled(value) { if (value) throw Error('Unsupported'); }};
const unchangedAudio = {language: 'en', enabled: true};
const locked = screen(false, {cachedMetadata: true, audioTracks: [unchangedAudio, lockedAudio]});
locked.elements['track-settings'].click(); locked.elements['audio-track-options'].children[1].click();
assert.equal(unchangedAudio.enabled, true, 'failed selection preserves previous audio');
assert.match(locked.elements['track-message'].textContent, /could not switch/);

const subtitleOnly = screen(false, {cachedMetadata: true, textTracks: [{kind: 'subtitles', language: 'it', mode: 'disabled'}],
  nextTracks: {audioTracks: [{enabled: true}], textTracks: []}});
subtitleOnly.elements['track-settings'].click();
assert(subtitleOnly.elements['audio-track-group'].hidden);
assert.equal(subtitleOnly.elements['subtitle-track-options'].children.length, 2);
subtitleOnly.elements['next-episode'].click();
assert(subtitleOnly.elements['track-panel'].hidden, 'episode transitions dismiss old track choices');
assert(subtitleOnly.elements['track-settings'].hidden, 'track availability is recalculated for the next episode');
const initiallyShowing = {kind: 'captions', language: 'en', mode: 'showing'};
const nextShowing = {kind: 'subtitles', language: 'it', mode: 'showing'};
const defaults = screen(false, {textTracks: [initiallyShowing], nextTracks: {textTracks: [nextShowing]}});
defaults.player.emit('loadedmetadata');
assert.equal(initiallyShowing.mode, 'disabled', 'subtitles start off when metadata is not cached');
const lateSubtitle = {kind: 'subtitles', language: 'fr', mode: 'showing'};
defaults.player.textTracks[1] = lateSubtitle; defaults.player.textTracks.length = 2; defaults.player.textTracks.emit('addtrack');
assert.equal(lateSubtitle.mode, 'disabled', 'late-discovered subtitles also default off');
defaults.elements['track-settings'].click(); defaults.elements['subtitle-track-options'].children[1].click();
assert.equal(initiallyShowing.mode, 'showing', 'viewer can still enable subtitles');
defaults.elements['next-episode'].click();
assert.equal(nextShowing.mode, 'disabled', 'next episode resets subtitles to Off');
console.log('Player behavior passed: playback, fullscreen, episode transitions, native audio/subtitle selection, remote and pointer controls, dynamic tracks, failures and idle hiding');
