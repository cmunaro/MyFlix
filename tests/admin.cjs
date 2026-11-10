// Management interaction checks; native TV keyboards and CSS need device review.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function screen(tv = true) {
  const listeners = {}, pending = [], intervals = [];
  let document, incomingCatalog;
  class Element {
    constructor(tag, attrs = {}, rect = {}) {
      this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = [];
      this.rect = {left: 0, top: 0, width: 180, height: 60, ...rect};
      this.offsetWidth = this.rect.width; this.offsetHeight = this.rect.height;
      this.hidden = attrs.hidden !== undefined;
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
    get innerHTML() { return this.children.slice(); }
    set innerHTML(children) { this.textContent = ''; children.forEach(child => this.appendChild(child)); }
    addEventListener(name, handler) { (this.handlers ||= {})[name] = handler; }
    set hidden(value) { this._hidden = Boolean(value); this.offsetWidth = value ? 0 : this.rect.width; this.offsetHeight = value ? 0 : this.rect.height; }
    get hidden() { return this._hidden; }
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
      if (selector[0] === '[') {
        const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        return match && this.hasAttribute(match[1]) && (match[2] === undefined || this.getAttribute(match[1]) === match[2]);
      }
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
    click() { this.clicks++; if (this.disabled) return; if (this.type === 'checkbox') { this.checked = !this.checked; if (this.onchange) this.onchange(); } if (this.onclick) this.onclick(); }
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
  add(root, 'button', {id: 'retry-failed-downloads', hidden: ''}, {left: 500, top: 300});
  add(root, 'button', {id: 'cancel-all-downloads', hidden: ''}, {left: 620, top: 300});
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
    open(method, url) { this.method = method; this.url = url; this.action = url.split('/').pop(); }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.body = body && (this.headers['Content-Type'] === 'application/x-www-form-urlencoded' ? body : JSON.parse(body)); pending.push(this); }
  }
  const location = {pathname: '/admin', search: '', reloads: 0, reload() { this.reloads += 1; }};
  const context = {document, navigator: {userAgent: tv ? 'VIDAA Hisense' : 'Firefox'}, location,
    window: {location, confirm() { return location.confirmed !== false; }, pageYOffset: 120, scrollTo() {}, setInterval(fn) { intervals.push(fn); },
      addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
      DOMParser: class { parseFromString() { return {getElementById() { return incomingCatalog; }}; } }},
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
    catalogScript(incoming) { incomingCatalog = incoming; vm.runInNewContext(fs.readFileSync('myflix/static/catalog.js', 'utf8'), context); },
    libraryScript() { vm.runInNewContext(fs.readFileSync('myflix/static/library.js', 'utf8'), context); },
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
const seriesGroup = ui.add(ui.body, 'details'); seriesGroup.open = true;
const seriesSummary = ui.add(seriesGroup, 'summary');
const seasonGroup = ui.add(seriesGroup, 'details'); seasonGroup.open = true;
const seasonSummary = ui.add(seasonGroup, 'summary');
const episodeEditor = ui.add(seasonGroup, 'details'); episodeEditor.open = true;
const episodeSummary = ui.add(episodeEditor, 'summary');
const episodeSave = ui.add(episodeEditor, 'button'); episodeSave.focus();
ui.key(461); assert(!episodeEditor.open); assert(seriesGroup.open);
assert.equal(ui.document.activeElement, episodeSummary, 'Back closes episode editor before series');
ui.key(461); assert(!seasonGroup.open); assert(seriesGroup.open);
assert.equal(ui.document.activeElement, seasonSummary, 'second Back returns to season summary');
ui.key(461); assert(!seriesGroup.open);
assert.equal(ui.document.activeElement, seriesSummary, 'third Back returns to series summary');
const desktop = screen(false); desktop.query.focus();
assert(!desktop.key(37).prevented); assert(!desktop.key(13).prevented, 'desktop input behavior remains native');
const columns = screen(); columns.query.focus(); columns.key(40);
assert.equal(columns.document.activeElement, columns.refresh, 'vertical movement stays in the content column');

ui.downloads();
ui.reply('sites', {groups: [{label: 'Providers', options: [{value: '__all__', label: 'All providers'}]}]});
ui.reply('status', {slots: {active: 1, max: 1}, queued: 0});
assert(ui.el('download-connection').textContent.includes('1/1 working jobs'), 'status counts all working jobs');
const active = {active: [{id: 'job-1', title: 'I Simpson', type: 'Serie', season: 2, episode: 7, status: 'Downloading', progress: 40}], history: [{title: 'Old completed film'}]};
ui.reply('queue', active);
assert(ui.el('download-queue').textContent.includes('Season 2 · Episode 7'), 'active download identifies the actual season and episode');
assert(!ui.el('download-queue').textContent.includes('Old completed film'), 'history is absent');
assert(ui.el('download-queue').querySelector('progress'), 'active progress is visible');
const cancel = () => ui.el('download-queue').querySelector('button');
cancel().focus(); ui.intervals[0](); ui.reply('queue', active);
assert.equal(ui.document.activeElement, cancel(), 'refresh restores focused job action');
ui.reply('status', {slots: {active: 1, max: 1}, queued: 0});
for (const [fields, expected] of [
  [{season: '0', episode: '3'}, 'Season 0 · Episode 3'],
  [{season: '12', episode: '105'}, 'Season 12 · Episode 105'],
  [{season: null, episode: 4}, 'Episode 4'],
  [{season: 3, episode: null}, 'Season 3'],
  [{season: 0, episode: 0}, ''],
  [{season: null, episode: null}, ''],
  [{season: 'invalid', episode: 'unknown'}, ''],
  [{type: 'Film', season: 0, episode: 0}, '']
]) {
  ui.intervals[0]();
  ui.reply('queue', {active: [{...active.active[0], ...fields}]});
  const text = ui.el('download-queue').textContent;
  if (expected) { assert(text.includes(expected), 'episode metadata renders: ' + expected); }
  else { assert(!/Season |Episode /.test(text), 'missing metadata and films have no invented episode numbers'); }
  ui.reply('status', {slots: {active: 1, max: 1}, queued: 0});
}
cancel().click(); cancel().click();
assert.equal(ui.pending.filter(xhr => xhr.action === 'cancel').length, 1, 'pending cancellation cannot duplicate');
ui.reply('cancel', {cancelled: true}); ui.reply('queue', {history: active.history});
assert.equal(ui.document.activeElement, ui.refresh, 'finished job restores focus to Refresh');
assert(ui.el('download-queue').textContent.includes('Nothing downloading'), 'history does not suppress empty state');
ui.reply('status', {slots: {active: 0, max: 1}, queued: 0});
ui.search.focus(); ui.form.onsubmit({preventDefault() {}}); ui.form.onsubmit({preventDefault() {}});
assert.equal(ui.pending.filter(xhr => xhr.action === 'search').length, 1, 'search does not duplicate');
ui.reply('search', {results: [{title: 'I Simpson', site: 'example', is_movie: false, payload: {name: 'I Simpson', type: 'tv', id: 'simpson'}}]});
const choose = ui.el('download-results').querySelector('button');
assert.equal(ui.document.activeElement, choose, 'results receive focus when search action is focused');
assert.equal(ui.el('download-results').querySelectorAll('button').length, 1, 'series exposes only Choose episodes');
assert.equal(ui.el('download-results').querySelectorAll('input').length, 0, 'no library name field');
choose.click(); choose.click(); assert.equal(ui.pending.filter(xhr => xhr.action === 'episodes').length, 1);
const metadataRequest = ui.reply('episodes', {series_id: 7, episodes: [
  {season: 0, episode: 1, title: 'Special'},
  {season: 1, episode: 1, title: 'Pilot <script>safe</script>'},
  {season: 1, episode: 3, title: 'Third'},
  {season: 2, episode: 1}, {season: 2, episode: 2}
]});
assert.equal(metadataRequest.timeout, 310000, 'long metadata lookup has its own request budget');
assert.equal(metadataRequest.headers['X-CSRF-Token'], 'csrf-token');
const picker = ui.el('download-results').querySelector('.episode-picker');
const sections = picker.querySelectorAll('.episode-season');
const inputs = section => section.querySelectorAll('input');
assert(picker.querySelectorAll('input').every(input => input.checked), 'all seasons and episodes default selected');
const allSeasons = picker.querySelector('input');
assert.equal(ui.document.activeElement, allSeasons, 'all seasons receives remote focus');
ui.key(13);
assert(picker.querySelectorAll('input').every(input => !input.checked && !input.indeterminate), 'remote clears every season and episode');
ui.key(13);
assert(picker.querySelectorAll('input').every(input => input.checked && !input.indeterminate), 'remote selects every season and episode');
assert(!picker.querySelector('script'), 'episode title is safe text');
const arrow = sections[1].querySelector('button');
const list = sections[1].querySelector('.episode-list');
assert(list.hidden); arrow.click(); assert(!list.hidden); assert.equal(arrow.getAttribute('aria-expanded'), 'true');
arrow.click(); assert(list.hidden);
inputs(sections[1])[1].focus(); ui.key(13);
assert(!inputs(sections[1])[0].checked && inputs(sections[1])[0].indeterminate, 'remote toggles episode and sets mixed season state');
assert(!allSeasons.checked && allSeasons.indeterminate, 'episode choice sets mixed all seasons state');
inputs(sections[1])[0].click();
assert(inputs(sections[1]).every(input => input.checked), 'season selects all episodes');
assert(allSeasons.checked && !allSeasons.indeterminate, 'restoring season checks all seasons');
inputs(sections[1])[0].click();
assert(inputs(sections[1]).every(input => !input.checked), 'season clears all episodes');
inputs(sections[1])[2].click();
assert(inputs(sections[1])[0].indeterminate, 'individual selection updates season');
inputs(sections[0])[0].click();
inputs(sections[2])[0].click();
inputs(sections[1])[2].click();
const submit = picker.querySelectorAll('button').find(button => button.textContent === 'Download');
assert(!allSeasons.checked && !allSeasons.indeterminate, 'empty selection clears all seasons state');
submit.click(); assert(!ui.pending.some(xhr => xhr.action === 'download'), 'empty selection cannot submit');
inputs(sections[0])[0].click(); inputs(sections[1])[2].click(); inputs(sections[2])[0].click();
submit.focus(); submit.click(); submit.click();
assert.equal(ui.pending.filter(xhr => xhr.action === 'series').length, 1, 'only one series action in flight');
const request = ui.reply('series', {error: 'Connection uncertain. Check queue.'}, 502);
assert.equal(request.body.series_id, 7);
assert.equal(request.body.only_new, false);
assert.deepEqual(request.body.selection, [{season: 0, episode: 1}, {season: 1, episode: 3}, {season: 2, episode: 1}, {season: 2, episode: 2}]);
assert.equal(request.headers['X-CSRF-Token'], 'csrf-token');
submit.click(); assert(!ui.pending.some(xhr => xhr.action === 'series'), 'uncertain selection is never resubmitted');
assert.equal(ui.document.activeElement, submit, 'failed action retains remote focus');
assert(ui.el('download-results').children.length, 'uncertain submission preserves results');
const success = screen(); success.downloads();
success.form.onsubmit({preventDefault() {}});
success.reply('search', {results: [{title: 'Show', site: 'example', payload: {name: 'Show'}}]});
success.el('download-results').querySelector('button').click();
success.reply('episodes', {series_id: 1, episodes: [{season: 1, episode: 1}, {season: 1, episode: 2}]});
success.el('download-results').querySelectorAll('button').find(button => button.textContent === 'Download').click();
success.reply('series', {queued: true, selected: 2});
assert.equal(success.el('download-results').children.length, 0, 'accepted series clears search results');
assert.equal(success.document.activeElement, success.search, 'clearing results restores search focus');
assert(!success.pending.some(xhr => xhr.action === 'follow'), 'series action does not require a separate follow request');
const locks = screen(); locks.downloads();
locks.form.onsubmit({preventDefault() {}});
locks.reply('search', {results: [{title: 'Show', site: 'example', payload: {name: 'Show'}}]});
locks.el('download-results').querySelector('button').click();
locks.reply('episodes', {series_id: 5, episodes: [
  {season: 1, episode: 1, available: true, locked: true},
  {season: 1, episode: 2, available: true, locked: true},
  {season: 2, episode: 1, available: true, locked: true},
  {season: 2, episode: 2, available: false, locked: false}
]});
const lockedSeasons = locks.el('download-results').querySelectorAll('.episode-season');
const lockedInputs = lockedSeasons[0].querySelectorAll('input');
assert(lockedInputs.every(input => input.disabled && input.checked), 'downloaded season and episodes cannot be toggled');
lockedInputs[0].click(); lockedInputs[1].click();
assert(lockedInputs.every(input => input.checked), 'disabled checkboxes retain downloaded state');
const mixedInputs = lockedSeasons[1].querySelectorAll('input');
assert(!mixedInputs[0].disabled && mixedInputs[1].disabled && !mixedInputs[2].disabled, 'partially downloaded season keeps only missing episodes selectable');
const allLocked = locks.el('download-results').querySelector('.episode-picker').querySelector('input');
allLocked.click(); assert(mixedInputs[1].checked && !mixedInputs[2].checked, 'select all never changes downloaded episodes');
allLocked.click(); assert(mixedInputs[2].checked);
const newOnly = locks.el('download-results').querySelectorAll('button').find(button => button.textContent === 'Only new episodes');
newOnly.click(); newOnly.click();
assert.equal(locks.pending.filter(xhr => xhr.action === 'series').length, 1);
const newOnlyRequest = locks.reply('series', {queued: true, selected: 0, only_new: true});
assert.deepEqual(newOnlyRequest.body, {series_id: 5, selection: [], only_new: true}, 'Only new episodes ignores existing checked selections');
assert.equal(locks.el('download-results').children.length, 0, 'new-only action clears results');
assert(locks.el('download-message').textContent.includes('Episodes listed now are skipped'));
const errors = screen(); errors.downloads();
errors.reply('sites', {groups: []});
errors.reply('status', {slots: {active: 0, max: 4}, queued: 0});
const failedQueue = {active: [], scheduled: [], history: [
  {id: 'failed-1', cancel_id: 'failed:first', title: 'Show', season: 1, episode: 2, status: 'failed', progress: 100, speed: '29.55M/s', error: 'Merge failed <script>bad</script>'},
  {id: 'failed-2', cancel_id: 'failed:second', title: 'Show', season: 2, episode: 1, status: 'failed'},
  {title: 'Completed title', status: 'completed'}, {title: 'Cancelled title', status: 'cancelled'}
]};
errors.reply('queue', failedQueue);
const failedText = errors.el('download-queue').textContent;
assert(failedText.includes('Recent failures'));
assert(!failedText.includes('100.0%') && !failedText.includes('29.55M/s'), 'failed downloads do not retain transfer progress or speed');
assert(failedText.includes('Season 1 · Episode 2') && failedText.includes('Season 2 · Episode 1'));
assert(failedText.includes('Merge failed <script>bad</script>'), 'error is rendered as text');
assert(failedText.includes('Download failed; the engine supplied no details.'));
assert(!failedText.includes('Completed title') && !failedText.includes('Cancelled title'));
assert(!failedText.includes('Nothing downloading'), 'failure is visible instead of an empty queue');
assert.equal(errors.el('download-queue').querySelectorAll('button').length, 2, 'failed attempts can be cancelled');
assert(!errors.el('download-queue').querySelector('progress'), '100% failed jobs are not shown as completed progress');
assert(!errors.el('download-queue').querySelector('script'), 'provider errors cannot inject HTML');
errors.el('download-queue').querySelector('a').focus();
errors.intervals[0](); errors.reply('queue', failedQueue);
assert.equal(errors.document.activeElement, errors.el('download-queue').querySelector('a'), 'failed episode review retains focus');
assert.equal(errors.document.activeElement.href, '/admin?section=downloads&q=Show');
errors.reply('status', {slots: {active: 0, max: 4}, queued: 0});

const failedCancel = errors.el('download-queue').querySelector('button');
failedCancel.focus(); failedCancel.click(); failedCancel.click();
assert.equal(errors.pending.filter(xhr => xhr.action === 'cancel').length, 1);
const cancelledFailure = errors.reply('cancel', {cancelled: true});
assert.equal(cancelledFailure.body.download_id, 'failed:first');
assert.equal(cancelledFailure.headers['X-CSRF-Token'], 'csrf-token');
errors.reply('queue', failedQueue);
errors.reply('status', {slots: {active: 0, max: 4}, queued: 0});
assert.equal(errors.document.activeElement, errors.el('download-queue').querySelector('button'), 'failed cancellation retains focus during refresh');

const retryAll = errors.el('retry-failed-downloads');
assert(!retryAll.hidden, 'bulk retry is available when failures are shown');
retryAll.focus(); retryAll.click(); retryAll.click();
assert.equal(errors.pending.filter(xhr => xhr.action === 'retry-failed').length, 1, 'bulk retry cannot duplicate while pending');
const retryRequest = errors.reply('retry-failed', {queued: 2, skipped: 0});
assert.equal(retryRequest.headers['X-CSRF-Token'], 'csrf-token');
errors.reply('queue', {history: [], active: [{id: 'normal', title: 'Normal film'}], scheduled: [], retrying: [
  {id: 'retry:1', title: 'Show', season: 1, episode: 2, status: 'Priority retry queued', priority: true}
]});
errors.reply('status', {slots: {active: 1, max: 4}, queued: 0});
const retriedText = errors.el('download-queue').textContent;
assert(retriedText.indexOf('Priority retries') < retriedText.indexOf('Working jobs'), 'pending retries are at the top');
assert(!retriedText.includes('Recent failures') && !retriedText.includes('Merge failed'), 'retried failures are removed');
assert(retryAll.hidden, 'bulk retry hides once failures have moved into the queue');
assert.equal(errors.document.activeElement, errors.refresh, 'focus moves to Refresh when the action disappears');
assert(errors.el('queue-message').textContent.includes('Queued 2 priority retries.'), 'bulk retry result survives refresh');
retryAll.click(); errors.reply('retry-failed', {error: 'Connection uncertain. Check queue.'}, 502);
assert(!errors.pending.some(xhr => xhr.action === 'retry-failed'), 'failed bulk request is not automatically retried');
const cancelAll = errors.el('cancel-all-downloads');
errors.refresh.click();
errors.reply('queue', {active: [{id: 'active', title: 'Film'}], scheduled: [{id: 'selection:1:1:2', title: 'Show'}],
  history: [failedQueue.history[0]], retrying: [{id: 'retry:1', cancel_id: 'retry:1', title: 'Show'}]});
errors.reply('status', {slots: {active: 1, max: 4}, queued: 1});
assert(!cancelAll.hidden, 'cancel all appears beside retry for current jobs');
cancelAll.focus(); cancelAll.click(); cancelAll.click(); retryAll.click();
assert.equal(errors.pending.filter(xhr => xhr.action === 'cancel').length, 1, 'bulk cancellations run sequentially');
assert(!errors.pending.some(xhr => xhr.action === 'retry-failed'), 'retry cannot race bulk cancellation');
for (const identity of ['retry:1', 'active', 'selection:1:1:2', 'failed:first']) {
  const request = errors.reply('cancel', {cancelled: true});
  assert.equal(request.body.download_id, identity);
  assert.equal(request.headers['X-CSRF-Token'], 'csrf-token');
}
errors.reply('queue', {active: [], scheduled: [], history: [], retrying: []});
errors.reply('status', {slots: {active: 0, max: 4}, queued: 0});
assert(cancelAll.hidden);
assert.equal(errors.document.activeElement, errors.refresh);
assert(errors.el('queue-message').textContent.includes('all current downloads'));
errors.refresh.click();
errors.reply('queue', {active: [{id: 'active'}], scheduled: [{id: 'queued'}], history: []});
errors.reply('status', {slots: {active: 1, max: 4}, queued: 1});
cancelAll.click(); errors.reply('cancel', {error: 'Connection uncertain. Check queue.'}, 502);
assert(!errors.pending.some(xhr => xhr.action === 'cancel'), 'bulk cancellation stops after an uncertain error');
errors.reply('queue', {active: [{id: 'active'}], scheduled: [{id: 'queued'}], history: []});
errors.reply('status', {slots: {active: 1, max: 4}, queued: 1});
assert(errors.el('queue-message').textContent.includes('Connection uncertain'));

console.log('Admin behavior passed: remote fields and Back, native desktop inputs, current downloads and episode failures, refresh focus, episode selection, CSRF, and mutation guards');

const catalog = screen();
const watchStatus = catalog.add(catalog.body, 'p', {id: 'library-watch-status'});
const show = catalog.add(catalog.body, 'details'); show.open = true;
const season = catalog.add(show, 'details'); season.open = true;
const episode = catalog.add(season, 'details'); episode.open = false;
const episodeHeading = catalog.add(episode, 'summary');
const watch = catalog.add(episodeHeading, 'button', {
  'data-watch-url': '/admin/media/7/watched', 'data-watched': '0', 'data-episode-name': 'Show S1 E1'
});
watch.textContent = 'Mark as watched';
const availability = catalog.add(episodeHeading, 'span', {'data-watch-state': '', 'data-availability': 'Ready'});
availability.textContent = 'Ready';
catalog.libraryScript();
watch.focus();
let prevented = false, stopped = false;
watch.onclick({preventDefault() { prevented = true; }, stopPropagation() { stopped = true; }});
watch.click();
assert(prevented && stopped, 'watch action cancels summary activation');
assert.equal(catalog.pending.length, 1, 'repeated activation submits once');
assert.equal(catalog.pending[0].url, '/admin/media/7/watched');
assert.equal(catalog.pending[0].headers['X-CSRF-Token'], 'csrf-token');
assert.equal(catalog.pending[0].body.watched, true);
assert.equal(availability.textContent, 'Ready', 'state changes only after confirmation');
catalog.reply('watched', {watched: 1});
assert.equal(watch.textContent, 'Mark as unwatched');
assert.equal(availability.textContent, 'Watched · Ready');
assert(!episode.open && season.open && show.open, 'saving preserves expansion state');
assert.equal(catalog.document.activeElement, watch, 'saving preserves button focus');
assert.match(watchStatus.textContent, /Show S1 E1 marked as watched/);
watch.click();
assert.equal(catalog.pending[0].body.watched, false);
catalog.reply('watched', {error: 'Failed'}, 500);
assert.equal(watch.textContent, 'Mark as unwatched', 'failed save preserves current state');
assert.equal(availability.textContent, 'Watched · Ready');
assert.equal(catalog.pending.length, 0, 'failed saves are not automatically retried');
assert.match(watchStatus.textContent, /Could not save/);
watch.click(); catalog.reply('watched', {watched: 0});
assert.equal(watch.textContent, 'Mark as watched');
assert.equal(availability.textContent, 'Ready');
watch.click(); catalog.pending.pop().ontimeout();
assert(!watch.hasAttribute('aria-disabled'), 'timeouts release the action for another attempt');
assert.match(watchStatus.textContent, /Could not save/);
assert(!episode.open && season.open && show.open);
console.log('Library passed: nested season Back, collapsed episode watch action, focus preservation, state toggle, CSRF, duplicate and failure handling');

const live = screen();
live.location.href = '/admin';
function catalogFixture(parent, ids) {
  const root = live.add(parent, 'section', {id: 'catalog-live', 'data-catalog-url': '/api/catalog/ids', 'data-catalog-ids': JSON.stringify(ids)});
  live.add(root, 'p', {id: 'library-watch-status'});
  const series = live.add(root, 'details', {'data-catalog-key': 'series:Show'});
  live.add(series, 'summary');
  const season = live.add(series, 'details', {'data-catalog-key': 'season:Show:1'});
  const summary = live.add(season, 'summary');
  ids.forEach(id => {
    const item = live.add(season, 'details', {'data-catalog-key': 'media:' + id});
    const caption = live.add(item, 'summary');
    live.add(caption, 'button', {'data-watch-url': '/watch/' + id, 'data-watched': '0', 'data-episode-name': 'Show E' + id});
    const state = live.add(caption, 'span', {'data-watch-state': '', 'data-availability': 'Ready'});
    state.textContent = 'Ready';
    live.add(item, 'input');
  });
  return {root, series, season, summary};
}
const before = catalogFixture(live.body, [1]);
const after = catalogFixture(live.document.createElement('div'), [1, 2]);
before.series.open = before.season.open = true;
before.summary.focus();
live.libraryScript(); live.catalogScript(after.root);
const tickCatalog = live.intervals[0];
tickCatalog(); tickCatalog();
assert.equal(live.pending.length, 1, 'catalog checks do not overlap');
live.reply('ids', {ids: [1]});
assert.equal(live.pending.length, 0, 'unchanged catalog never reloads the page');
tickCatalog(); live.reply('ids', {ids: [1, 2]});
live.reply('admin', '<new catalog>');
assert.equal(before.root.querySelectorAll('[data-watch-url]').length, 2, 'newly imported episode appears without navigation');
assert(before.root.querySelectorAll('details').slice(0, 2).every(detail => detail.open), 'expanded series and season remain open');
assert.equal(live.document.activeElement, before.root.querySelectorAll('summary')[1], 'remote focus stays on the same season');
assert(before.root.querySelectorAll('[data-watch-url]').every(button => typeof button.onclick === 'function'), 'new episode watch controls are bound');
tickCatalog(); live.reply('ids', {ids: [2, 1]});
assert.equal(live.pending.length, 0, 'catalog order does not trigger another refresh');
const newestWatch = before.root.querySelectorAll('[data-watch-url]')[1];
newestWatch.click(); live.reply('2', {watched: true});
assert(live.el('library-watch-status').textContent.includes('marked as watched'), 'new watch action updates the live status element');
live.current.focus();
tickCatalog(); live.reply('ids', {ids: [1, 2, 3]}); live.reply('admin', '<new catalog>');
assert.equal(live.document.activeElement, live.current, 'focus outside the catalog is preserved');
tickCatalog(); live.reply('ids', {ids: [1, 2, 3]}); live.reply('admin', null, 502);
assert.equal(live.document.activeElement, live.current, 'failed refresh retains the current page');
live.document.hidden = true; tickCatalog(); assert.equal(live.pending.length, 0, 'hidden pages do not poll');
live.document.hidden = false;
tickCatalog(); live.reply('ids', {ids: [1, 2, 3]});
before.root.handlers.input();
live.reply('admin', '<new catalog>');
assert.equal(before.root.querySelectorAll('[data-watch-url]').length, 2, 'in-flight refresh never discards edits');
tickCatalog(); assert.equal(live.pending.length, 0, 'dirty forms defer further updates');
console.log('Catalog updates passed: new imports, unchanged lists, focus, expanded seasons, new controls, hidden pages, and unsaved edits');

const episodeUpdates = screen(); episodeUpdates.location.href = '/series/3';
function shelfFixture(parent, ids) {
  const root = episodeUpdates.add(parent, 'div', {id: 'catalog-live', 'data-catalog-url': '/api/catalog/ids?series_id=3', 'data-catalog-ids': JSON.stringify(ids)});
  const rail = episodeUpdates.add(root, 'div', {'data-rail': '', 'aria-label': 'Season 1'});
  ids.forEach(id => episodeUpdates.add(rail, 'a', {'data-card': '', 'data-media-id': String(id)}));
  return {root, rail};
}
const originalShelf = shelfFixture(episodeUpdates.body, [3]);
const focusedEpisode = originalShelf.rail.querySelector('a'); focusedEpisode.focus();
originalShelf.rail.scrollLeft = 275;
const updatedShelf = shelfFixture(episodeUpdates.document.createElement('div'), [2, 3]);
episodeUpdates.catalogScript(updatedShelf.root);
episodeUpdates.intervals[0]();
episodeUpdates.reply('ids?series_id=3', {ids: [2, 3]}); episodeUpdates.reply('3', '<series catalog>');
assert.equal(episodeUpdates.document.activeElement.getAttribute('data-media-id'), '3', 'series refresh preserves episode identity when positions change');
assert.equal(originalShelf.root.querySelector('[data-rail]').scrollLeft, 275, 'series shelf retains horizontal scroll');
console.log('Series updates passed: new episode cards, identity-based remote focus, and shelf scroll');

const missing = screen();
const missingStatus = missing.add(missing.body, 'p', {id: 'library-watch-status'});
const missingItem = missing.add(missing.body, 'details');
const missingSummary = missing.add(missingItem, 'summary');
const missingState = missing.add(missingSummary, 'span', {'data-watch-state': '', 'data-availability': 'Not downloaded'});
missingState.textContent = 'Not downloaded';
const getEpisode = missing.add(missingSummary, 'button', {'data-download-url': '/admin/downloads/series/1/1/2', 'data-episode-name': 'Show S1 E2'});
missing.libraryScript(); getEpisode.focus();
const clickEvent = {preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
getEpisode.onclick(clickEvent); getEpisode.click();
assert(clickEvent.prevented && clickEvent.stopped, 'download does not toggle the episode details');
assert.equal(missing.pending.length, 1, 'library download cannot duplicate requests');
assert(getEpisode.hasAttribute('data-action-pending'), 'catalog refresh waits only for an active request');
const episodeRequest = missing.reply('2', {queued: true});
assert.equal(episodeRequest.headers['X-CSRF-Token'], 'csrf-token');
assert.equal(getEpisode.textContent, 'Queued');
assert(missingState.textContent.includes('Queued'));
assert(!getEpisode.hasAttribute('data-action-pending'), 'queued state allows later catalog updates');
assert.equal(missing.document.activeElement, getEpisode, 'download acknowledgement preserves remote focus');
getEpisode.click(); assert.equal(missing.pending.length, 0, 'acknowledged selection cannot resubmit');
const badEpisode = missing.add(missingSummary, 'button', {'data-download-url': '/admin/downloads/series/1/1/3', 'data-episode-name': 'Show S1 E3'});
missing.libraryScript(); badEpisode.click(); missing.reply('3', {error: 'Check the queue'}, 502);
badEpisode.click(); assert.equal(missing.pending.length, 0, 'unconfirmed library request is not repeated');
assert(missingStatus.textContent.includes('Check the queue'));
console.log('Library downloads passed: exact episode, CSRF, duplicate guards, acknowledgement, safe errors, and focus');

const revisionUI = screen(); revisionUI.location.href = '/admin';
const revisionRoot = revisionUI.add(revisionUI.body, 'section', {id: 'catalog-live', 'data-catalog-url': '/api/catalog/ids', 'data-catalog-ids': '[]', 'data-catalog-revision': 'before'});
const revisionIncoming = revisionUI.document.createElement('section');
revisionIncoming.setAttribute('data-catalog-ids', '[]'); revisionIncoming.setAttribute('data-catalog-revision', 'after');
const disabledDownload = revisionUI.add(revisionIncoming, 'button', {'aria-disabled': 'true'});
disabledDownload.textContent = 'Download';
revisionUI.catalogScript(revisionIncoming);
revisionUI.intervals[0](); revisionUI.reply('ids', {ids: [], revision: 'after'}); revisionUI.reply('admin', '<missing episodes>');
assert.equal(revisionRoot.getAttribute('data-catalog-revision'), 'after', 'new provider episodes refresh even when media IDs do not change');
revisionUI.intervals[0]();
assert.equal(revisionUI.pending.length, 1, 'disabled episode controls do not block catalog updates');
revisionUI.reply('ids', {ids: [], revision: 'after'});
assert.equal(revisionUI.pending.length, 0, 'unchanged revision does not reload markup');
console.log('Library revision passed: missing episodes, download state changes, and disabled controls');

const newEpisodeSetting = screen();
const settingStatus = newEpisodeSetting.add(newEpisodeSetting.body, 'p', {id: 'library-watch-status'});
const toggleNew = newEpisodeSetting.add(newEpisodeSetting.body, 'input', {type: 'checkbox', 'data-new-episodes-url': '/admin/series/1/new-episodes'});
toggleNew.checked = true;
newEpisodeSetting.libraryScript(); toggleNew.focus(); toggleNew.click(); toggleNew.click();
assert.equal(newEpisodeSetting.pending.length, 1, 'new episode setting submits once while pending');
const settingRequest = newEpisodeSetting.reply('new-episodes', {enabled: false});
assert.deepEqual(settingRequest.body, {enabled: false});
assert.equal(settingRequest.headers['X-CSRF-Token'], 'csrf-token');
assert(!toggleNew.checked && !toggleNew.disabled);
assert.equal(newEpisodeSetting.document.activeElement, toggleNew, 'saved preference preserves focus');
assert(!toggleNew.hasAttribute('data-action-pending'));
toggleNew.click(); newEpisodeSetting.reply('new-episodes', {error: 'Could not save'}, 502);
assert(!toggleNew.checked, 'failed setting restores last acknowledged state');
assert(settingStatus.textContent.includes('Could not save'));
console.log('New episode preference passed: checkbox persistence, CSRF, duplicate guard, failure rollback, and focus');

const removal = screen();
const removalStatus = removal.add(removal.body, 'p', {id: 'library-watch-status'});
const deleteVideo = removal.add(removal.body, 'button', {'data-remove-url': '/admin/media/7/remove', 'data-remove-name': 'Show S1 E1'});
const deleteSeason = removal.add(removal.body, 'button', {'data-remove-url': '/admin/season/remove', 'data-remove-name': 'Show Season 1', 'data-remove-series': 'Show & Friends', 'data-remove-season': '1'});
removal.libraryScript();
removal.location.confirmed = false;
deleteVideo.click();
assert.equal(removal.pending.length, 0, 'cancelled confirmation never deletes');
removal.location.confirmed = true;
prevented = false; stopped = false;
deleteVideo.onclick({preventDefault() { prevented = true; }, stopPropagation() { stopped = true; }});
assert(prevented && stopped, 'delete cancels summary activation');
deleteVideo.click();
assert.equal(removal.pending.length, 1, 'delete sends only once');
assert.equal(removal.pending[0].headers['X-CSRF-Token'], 'csrf-token');
assert.equal(removal.pending[0].body, 'confirm=remove');
removal.reply('remove', {});
assert.equal(removal.location.reloads, 1, 'success refreshes sizes and availability');
deleteSeason.click();
assert.equal(removal.pending[0].body, 'confirm=remove&series=Show%20%26%20Friends&season=1');
removal.pending[0].ontimeout();
deleteSeason.click();
assert.equal(removal.pending.length, 1, 'uncertain delete never retries');
assert(removalStatus.textContent.includes('Reload Library'));

console.log('Library deletion passed: confirmation, CSRF, season scope, refresh, and no uncertain retries');

const cleanup = screen();
cleanup.add(cleanup.body, 'p', {id: 'library-watch-status'});
const cleanupOpen = cleanup.add(cleanup.body, 'button', {id: 'cleanup-open', 'data-cleanup-url': '/admin/cleanup'});
const cleanupDialog = cleanup.add(cleanup.body, 'div', {id: 'cleanup-dialog', hidden: ''});
const cleanupStatus = cleanup.add(cleanupDialog, 'p', {id: 'cleanup-status'});
const cleanupEpisodes = cleanup.add(cleanupDialog, 'input', {id: 'cleanup-episodes', type: 'checkbox'});
cleanup.add(cleanupDialog, 'span', {id: 'cleanup-episodes-label'});
const cleanupMovies = cleanup.add(cleanupDialog, 'input', {id: 'cleanup-movies', type: 'checkbox'});
cleanup.add(cleanupDialog, 'span', {id: 'cleanup-movies-label'});
const cleanupCancel = cleanup.add(cleanupDialog, 'button', {id: 'cleanup-cancel'});
const cleanupConfirm = cleanup.add(cleanupDialog, 'button', {id: 'cleanup-confirm'});
cleanup.libraryScript();
cleanupOpen.click();
assert(!cleanupDialog.hidden);
assert.equal(cleanup.document.activeElement, cleanupCancel);
assert(cleanupConfirm.disabled && cleanupEpisodes.disabled);
cleanup.reply('cleanup', {token: 'preview', episodes: {count: 2, size: '8.0 GB'}, movies: {count: 1, size: '4.0 GB'}});
assert(cleanup.el('cleanup-episodes-label').textContent.includes('free 8.0 GB'));
assert(cleanup.el('cleanup-movies-label').textContent.includes('free 4.0 GB'));
cleanup.key(38);
assert.equal(cleanup.document.activeElement, cleanupMovies, 'arrows stay inside popup');
cleanup.key(13);
assert(cleanupMovies.checked && !cleanupConfirm.disabled, 'remote OK selects checkbox once');
cleanup.key(9);
assert.equal(cleanup.document.activeElement, cleanupCancel, 'Tab stays inside popup');
cleanup.key(461);
assert(cleanupDialog.hidden, 'remote Back cancels');
assert.equal(cleanup.document.activeElement, cleanupOpen, 'cancel restores opener focus');
assert.equal(cleanup.pending.length, 0, 'cancel never deletes');
cleanupOpen.click(); cleanupCancel.click();
cleanup.reply('cleanup', {token: 'stale', episodes: {count: 1, size: '1.0 GB'}, movies: {count: 1, size: '2.0 GB'}});
assert(cleanupDialog.hidden, 'late preview cannot reopen popup');
cleanupOpen.click();
cleanup.reply('cleanup', {token: 'current', episodes: {count: 1, size: '1.0 GB'}, movies: {count: 0, size: '0 B'}});
assert(cleanupMovies.disabled && !cleanupMovies.checked);
assert(cleanup.el('cleanup-movies-label').textContent.includes('nothing to free'));
cleanupEpisodes.click(); cleanupConfirm.click(); cleanupConfirm.click();
assert.equal(cleanup.pending.length, 1, 'confirm submits once');
assert.equal(cleanup.pending[0].headers['X-CSRF-Token'], 'csrf-token');
assert.deepEqual(JSON.parse(JSON.stringify(cleanup.pending[0].body)), {categories: ['episodes'], token: 'current', confirm: true});
cleanup.key(461);
assert(!cleanupDialog.hidden, 'cannot cancel deletion after submission');
cleanup.reply('cleanup', {error: 'Library changed. Reopen Clean up.'}, 409);
assert(cleanupConfirm.disabled && !cleanupCancel.disabled);
assert(cleanupStatus.textContent.includes('Library changed'));
cleanupConfirm.click(); assert.equal(cleanup.pending.length, 0, 'stale preview cannot retry deletion');
cleanupCancel.click(); cleanupOpen.click();
cleanup.reply('cleanup', {token: 'fresh', episodes: {count: 1, size: '1.0 GB'}, movies: {count: 1, size: '2.0 GB'}});
cleanupEpisodes.click(); cleanupMovies.click(); cleanupConfirm.click();
assert.equal(cleanup.pending[0].body.categories.length, 2);
cleanup.reply('cleanup', {removed: 2});
assert.equal(cleanup.location.reloads, 1, 'cleanup success refreshes free space and library');
console.log('Watched cleanup passed: preview totals, remote focus, cancellation, selected categories, CSRF, stale previews, and duplicate guards');
