/* ES5: the home dropdown shares remote input ownership with tv.js. */
(function () {
  var toggle = document.getElementById('notification-toggle');
  if (!toggle) { return; }
  var panel = document.getElementById('notifications');
  var list = document.getElementById('notification-list');
  var count = document.getElementById('notification-count');
  var clear = document.getElementById('notification-clear');
  var status = document.getElementById('notification-status');
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var loading = false, loaded = false, clearing = false, marking = false, refreshPending = false;
  function links() { return Array.prototype.slice.call(list.querySelectorAll('a')); }
  function updateCount() {
    var unread = links().filter(function (link) { return !link.notice.seen; }).length;
    count.textContent = unread;
    count.hidden = !unread;
    toggle.setAttribute('aria-label', 'Notifications (' + unread + ' unseen)');
    clear.disabled = clearing || loading || !links().length;
  }
  function post(action, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/notifications/' + action, true);
    xhr.timeout = 10000;
    xhr.setRequestHeader('X-CSRF-Token', token);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () { done(xhr.status === 200); };
    xhr.onerror = xhr.ontimeout = function () { done(false); };
    xhr.send(JSON.stringify(body));
  }
  function markVisible() {
    if (panel.hidden || document.hidden || loading || clearing || marking || refreshPending) { return; }
    var bounds = list.getBoundingClientRect();
    var top = Math.max(bounds.top, 0), bottom = Math.min(bounds.bottom, window.innerHeight);
    var visible = links().filter(function (link) {
      var rect = link.getBoundingClientRect();
      // Require the whole row, or a full scroll viewport for an oversized row.
      return !link.notice.seen && rect.bottom > rect.top && bottom > top &&
        Math.min(rect.bottom, bottom) - Math.max(rect.top, top) >= Math.min(rect.bottom - rect.top, bounds.bottom - bounds.top);
    }).slice(0, 100);
    if (!visible.length) { return; }
    marking = true;
    var ids = visible.map(function (link) { return link.notice.id; });
    post('seen', {ids: ids}, function (ok) {
      marking = false;
      if (!ok) { status.textContent = 'Could not save seen notifications. Scroll or reopen to retry.'; }
      else {
        links().forEach(function (link) { if (ids.indexOf(link.notice.id) !== -1) { link.notice.seen = true; } });
      }
      updateCount();
      if (refreshPending) { refreshPending = false; refresh(); }
      else if (ok) { markVisible(); }
    });
  }
  function close(restore) {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('notifications-open');
    if (restore) { toggle.focus(); }
  }
  function refresh() {
    if (loading || clearing) { return; }
    if (marking) { refreshPending = true; return; }
    loading = true;
    clear.disabled = true;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/notifications', true);
    xhr.timeout = 10000;
    function failed() {
      loading = false;
      if (!loaded) { list.textContent = 'Notifications unavailable. Close and reopen to retry.'; }
      updateCount();
    }
    xhr.onload = function () {
      if (xhr.status !== 200) { failed(); return; }
      var notices;
      try { notices = JSON.parse(xhr.responseText).notifications; }
      catch (error) { failed(); return; }
      var hadFocus = list.contains(document.activeElement);
      list.textContent = '';
      list.scrollTop = 0;
      notices.forEach(function (notice) {
        var link = document.createElement('a');
        link.href = notice.href;
        link.notice = notice;
        var message = document.createElement('strong');
        message.textContent = notice.message;
        var detail = document.createElement('span');
        detail.textContent = notice.detail || notice.created_at + ' UTC';
        link.appendChild(message); link.appendChild(detail); list.appendChild(link);
      });
      if (!notices.length) { list.textContent = 'No notifications yet.'; }
      loading = false; loaded = true;
      updateCount();
      if (!panel.hidden && hadFocus) { (links()[0] || list).focus(); }
      markVisible();
    };
    xhr.onerror = xhr.ontimeout = failed;
    xhr.send();
  }
  function open() {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    document.body.classList.add('notifications-open');
    // Refresh before focusing: cached rows must not become seen before sorting.
    refresh();
    list.focus();
  }
  clear.addEventListener('click', function () {
    if (clear.disabled) { return; }
    clearing = true;
    updateCount();
    status.textContent = '';
    post('clear', {}, function (ok) {
      clearing = false;
      if (ok) {
        list.textContent = 'No notifications yet.';
        if (!panel.hidden) { list.focus(); }
      } else { status.textContent = 'Could not clear notifications. Try again.'; }
      updateCount();
    });
  });
  toggle.addEventListener('click', function () { if (panel.hidden) { open(); } else { close(true); } });
  document.addEventListener('click', function (event) {
    if (!panel.hidden && !panel.contains(event.target) && !toggle.contains(event.target)) { close(false); }
  });
  document.addEventListener('focusin', function (event) {
    if (!panel.hidden && !panel.contains(event.target) && event.target !== toggle) { close(false); }
  });
  list.addEventListener('focusin', function (event) {
    if (event.target !== list && event.target.scrollIntoView) { event.target.scrollIntoView(false); }
    markVisible();
  });
  list.addEventListener('scroll', markVisible);
  window.addEventListener('resize', markVisible);
  document.addEventListener('visibilitychange', markVisible);
  window.MyFlixNotifications = {handleKey: function (event) {
    var code = event.keyCode, active = document.activeElement;
    if (panel.hidden) {
      if (active === toggle && (code === 13 || code === 40)) { open(); event.preventDefault(); return true; }
      return false;
    }
    if ([8, 27, 461, 10009].indexOf(code) !== -1) { close(true); }
    else if (code === 13 && active === toggle) { close(true); }
    else if (code >= 37 && code <= 40) {
      var items = links();
      if (!clear.disabled) { items.unshift(clear); }
      var index = items.indexOf(active);
      if (code === 38 && index <= 0) { toggle.focus(); }
      else if (code === 40) {
        if (items.length) { items[Math.min(index + 1, items.length - 1)].focus(); }
        else { list.scrollTop += 100; }
      } else if (code === 38) { items[index - 1].focus(); }
    } else { return false; }
    event.preventDefault(); return true;
  }};
  refresh();
  window.setInterval(function () { if (panel.hidden && !document.hidden) { refresh(); } }, 60000);
}());
