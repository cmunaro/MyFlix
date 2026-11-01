(function () {
  var list = document.getElementById('following-lookups');
  var connection = document.getElementById('following-connection');
  if (!connection) { return; }
  var busy = false;
  var stopped = false;
  var timer;
  var pending;
  function node(tag, text, attribute) {
    var element = document.createElement(tag);
    if (text) { element.textContent = text; }
    if (attribute) { element.setAttribute(attribute, ''); }
    return element;
  }
  function text(element, value) {
    if (element.textContent !== value) { element.textContent = value; }
  }
  function create(item) {
    var card = node('div');
    card.setAttribute('data-lookup-id', item.id);
    card.appendChild(node('h3', item.series));
    var status = node('div');
    status.className = 'lookup-status';
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    status.appendChild(node('p', '', 'data-lookup-message'));
    status.appendChild(node('p', '', 'data-lookup-checked'));
    var progress = node('progress', '', 'data-lookup-progress');
    progress.setAttribute('aria-label', 'Seasons fetched');
    status.appendChild(progress);
    var error = node('p', '', 'data-lookup-error');
    error.className = 'error'; error.setAttribute('role', 'alert');
    status.appendChild(error);
    var complete = node('p', 'Episode list ready. ', 'data-lookup-complete');
    var link = node('a', 'View episodes in Following');
    link.href = '/admin?section=following';
    complete.appendChild(link);
    status.appendChild(complete);
    card.appendChild(status);
    list.appendChild(card);
    return card;
  }
  function render(item) {
    var card = document.querySelector('[data-lookup-id="' + item.id + '"]');
    if (!card && list) { card = create(item); }
    if (!card) { return; }
    text(card.querySelector('[data-lookup-message]'), item.message);
    text(card.querySelector('[data-lookup-checked]'), 'Last checked: ' + item.checked);
    var progress = card.querySelector('[data-lookup-progress]');
    progress.hidden = item.state !== 'fetching';
    if (item.seasons_total > 0) {
      progress.setAttribute('max', item.seasons_total);
      progress.setAttribute('value', item.seasons_done);
    } else {
      progress.removeAttribute('value');
      progress.removeAttribute('max');
    }
    card.querySelector('.lookup-status').setAttribute('aria-busy', item.state === 'fetching' ? 'true' : 'false');
    var error = card.querySelector('[data-lookup-error]');
    text(error, item.error || '');
    error.hidden = !item.error;
    var complete = card.querySelector('[data-lookup-complete]');
    complete.hidden = item.state !== 'complete' && !complete.contains(document.activeElement);
  }
  function schedule() {
    if (!stopped) { timer = window.setTimeout(refresh, 3000); }
  }
  function refresh() {
    window.clearTimeout(timer);
    if (stopped || busy) { return; }
    if (document.hidden) { schedule(); return; }
    busy = true;
    var xhr = new XMLHttpRequest();
    pending = xhr;
    xhr.open('GET', '/admin/following/status', true);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.timeout = 10000;
    function finish(error) {
      if (pending !== xhr) { return; }
      pending = null;
      busy = false;
      if (stopped) { return; }
      text(connection, error || '');
      schedule();
    }
    xhr.onload = function () {
      if (stopped || pending !== xhr) { return; }
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) { finish('Could not refresh episode-check status. Retrying…'); return; }
      if (xhr.status !== 200 || !data || !Array.isArray(data.subscriptions)) {
        finish('Could not refresh episode-check status. Retrying…'); return;
      }
      data.subscriptions.forEach(render);
      var empty = list && list.querySelector('[data-no-lookups]');
      if (empty) { empty.hidden = data.subscriptions.length > 0; }
      finish();
    };
    xhr.onerror = xhr.ontimeout = function () { finish('Connection lost. Episode-check status may be out of date. Retrying…'); };
    xhr.send(null);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { refresh(); } });
  window.addEventListener('pagehide', function () {
    stopped = true; window.clearTimeout(timer);
    var request = pending; pending = null; busy = false;
    if (request) { request.abort(); }
  });
  window.addEventListener('pageshow', function () { if (stopped) { stopped = false; refresh(); } });
  refresh();
}());
