/* ES5: the home dropdown shares remote input ownership with tv.js. */
(function () {
  var toggle = document.getElementById('notification-toggle');
  if (!toggle) { return; }
  var panel = document.getElementById('notifications');
  var list = document.getElementById('notification-list');
  var count = document.getElementById('notification-count');
  var loading = false, loaded = false;
  function links() { return Array.prototype.slice.call(list.querySelectorAll('a')); }
  function close(restore) {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('notifications-open');
    if (restore) { toggle.focus(); }
  }
  function refresh() {
    if (loading) { return; }
    loading = true;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/notifications', true);
    xhr.timeout = 10000;
    function failed() {
      loading = false;
      if (!loaded) { list.textContent = 'Notifications unavailable. Close and reopen to retry.'; }
    }
    xhr.onload = function () {
      if (xhr.status !== 200) { failed(); return; }
      var notices;
      try { notices = JSON.parse(xhr.responseText).notifications; }
      catch (error) { failed(); return; }
      var index = links().indexOf(document.activeElement);
      list.textContent = '';
      notices.forEach(function (notice) {
        var link = document.createElement('a');
        link.href = notice.href;
        var message = document.createElement('strong');
        message.textContent = notice.message;
        var detail = document.createElement('span');
        detail.textContent = notice.detail || notice.created_at + ' UTC';
        link.appendChild(message); link.appendChild(detail); list.appendChild(link);
      });
      if (!notices.length) { list.textContent = 'No notifications yet.'; }
      count.textContent = notices.length;
      count.hidden = !notices.length;
      toggle.setAttribute('aria-label', 'Notifications (' + notices.length + ')');
      loading = false; loaded = true;
      if (!panel.hidden && index !== -1) { (links()[index] || list).focus(); }
    };
    xhr.onerror = xhr.ontimeout = failed;
    xhr.send();
  }
  function open() {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    document.body.classList.add('notifications-open');
    (links()[0] || list).focus();
    refresh();
  }
  toggle.addEventListener('click', function () { if (panel.hidden) { open(); } else { close(true); } });
  document.addEventListener('click', function (event) {
    if (!panel.hidden && !panel.contains(event.target) && !toggle.contains(event.target)) { close(false); }
  });
  document.addEventListener('focusin', function (event) {
    if (!panel.hidden && !panel.contains(event.target) && event.target !== toggle) { close(false); }
  });
  list.addEventListener('focusin', function (event) {
    if (event.target !== list && event.target.scrollIntoView) { event.target.scrollIntoView(false); }
  });
  window.MyFlixNotifications = {handleKey: function (event) {
    var code = event.keyCode, active = document.activeElement;
    if (panel.hidden) {
      if (active === toggle && (code === 13 || code === 40)) { open(); event.preventDefault(); return true; }
      return false;
    }
    if ([8, 27, 461, 10009].indexOf(code) !== -1) { close(true); }
    else if (code === 13 && active === toggle) { close(true); }
    else if (code >= 37 && code <= 40) {
      var items = links(), index = items.indexOf(active);
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
