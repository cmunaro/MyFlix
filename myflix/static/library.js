/* ES5: update episode history without toggling its details or losing focus. */
(function () {
  var status = document.getElementById('library-watch-status');
  if (!status) { return; }
  var cleanup = document.getElementById('cleanup-dialog');
  var cleanupToken = null, cleanupPending = false, cleanupGeneration = 0;
  function refresh() {
    status = document.getElementById('library-watch-status');
    var opener = document.getElementById('cleanup-open');
    if (opener && cleanup) { opener.onclick = openCleanup; }
    var buttons = document.querySelectorAll('[data-watch-url]');
    for (var i = 0; i < buttons.length; i += 1) {
      if (!buttons[i].getAttribute('data-watch-bound')) { bind(buttons[i]); }
    }
    var settings = document.querySelectorAll('[data-new-episodes-url]');
    for (var k = 0; k < settings.length; k += 1) {
      if (!settings[k].getAttribute('data-new-episodes-bound')) { bindNewEpisodes(settings[k]); }
    }
    var removals = document.querySelectorAll('[data-remove-url]');
    for (var r = 0; r < removals.length; r += 1) {
      if (!removals[r].getAttribute('data-remove-bound')) { bindRemove(removals[r]); }
    }
    var downloads = document.querySelectorAll('[data-download-url]');
    for (var j = 0; j < downloads.length; j += 1) {
      if (!downloads[j].getAttribute('data-download-bound')) { bindDownload(downloads[j]); }
    }
  }
  window.MyFlixLibrary = {refresh: refresh, cleanupOpen: function () { return Boolean(cleanup && !cleanup.hidden); }, handleKey: cleanupKey};
  if (cleanup) {
    document.getElementById('cleanup-cancel').onclick = closeCleanup;
    document.getElementById('cleanup-confirm').onclick = confirmCleanup;
    document.getElementById('cleanup-episodes').onchange = updateCleanup;
    document.getElementById('cleanup-movies').onchange = updateCleanup;
    document.addEventListener('focusin', function (event) {
      if (!cleanup.hidden && !cleanup.contains(event.target)) { document.getElementById('cleanup-cancel').focus(); }
    });
  }
  refresh();
  function cleanupMessage(message) { document.getElementById('cleanup-status').textContent = message; }
  function updateCleanup() {
    document.getElementById('cleanup-confirm').disabled = cleanupPending || !cleanupToken ||
      !(document.getElementById('cleanup-episodes').checked || document.getElementById('cleanup-movies').checked);
  }
  function closeCleanup() {
    if (cleanupPending) { return; }
    cleanupGeneration += 1;
    cleanup.hidden = true;
    document.getElementById('cleanup-open').focus();
  }
  function openCleanup() {
    var generation = ++cleanupGeneration;
    cleanupToken = null;
    cleanup.hidden = false;
    var kinds = ['episodes', 'movies'];
    for (var i = 0; i < kinds.length; i += 1) {
      var input = document.getElementById('cleanup-' + kinds[i]);
      input.checked = false; input.disabled = true;
      document.getElementById('cleanup-' + kinds[i] + '-label').textContent = 'Delete all watched ' + kinds[i];
    }
    updateCleanup();
    cleanupMessage('Calculating space to free…');
    document.getElementById('cleanup-cancel').focus();
    var request = new XMLHttpRequest();
    request.open('GET', document.getElementById('cleanup-open').getAttribute('data-cleanup-url'), true);
    request.timeout = 15000;
    request.onerror = request.ontimeout = function () {
      if (generation === cleanupGeneration) { cleanupMessage('Could not calculate cleanup space. Close and reopen Clean up to try again.'); }
    };
    request.onload = function () {
      if (generation !== cleanupGeneration) { return; }
      var data;
      try { data = JSON.parse(request.responseText); } catch (error) { request.onerror(); return; }
      if (request.status !== 200 || !data || !data.token || !data.episodes || !data.movies) { request.onerror(); return; }
      cleanupToken = data.token;
      for (var j = 0; j < kinds.length; j += 1) {
        var kind = kinds[j], group = data[kind];
        document.getElementById('cleanup-' + kind).disabled = !group.count;
        document.getElementById('cleanup-' + kind + '-label').textContent = 'Delete all watched ' + kind +
          (group.count ? ' — free ' + group.size : ' — nothing to free');
      }
      cleanupMessage('Choose which watched videos to delete.');
      updateCleanup();
    };
    request.send(null);
  }
  function confirmCleanup() {
    if (cleanupPending || !cleanupToken || document.getElementById('cleanup-confirm').disabled) { return; }
    var categories = [];
    if (document.getElementById('cleanup-episodes').checked) { categories.push('episodes'); }
    if (document.getElementById('cleanup-movies').checked) { categories.push('movies'); }
    cleanupPending = true;
    document.getElementById('cleanup-cancel').disabled = true;
    updateCleanup();
    cleanupMessage('Deleting selected watched videos…');
    var request = new XMLHttpRequest();
    request.open('POST', document.getElementById('cleanup-open').getAttribute('data-cleanup-url'), true);
    request.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
    request.setRequestHeader('Content-Type', 'application/json');
    request.timeout = 15000;
    function failed(message) {
      cleanupPending = false; cleanupToken = null;
      document.getElementById('cleanup-cancel').disabled = false;
      updateCleanup();
      cleanupMessage(message || 'Removal could not be confirmed. Reload Library to check remaining files.');
      document.getElementById('cleanup-cancel').focus();
    }
    request.onerror = request.ontimeout = function () { failed(); };
    request.onload = function () {
      var data;
      try { data = JSON.parse(request.responseText); } catch (error) { failed(); return; }
      if (request.status !== 200 || !data || typeof data.removed !== 'number') { failed(data && data.error); return; }
      window.location.reload();
    };
    request.send(JSON.stringify({categories: categories, token: cleanupToken, confirm: true}));
  }
  function cleanupKey(event) {
    if (!cleanup || cleanup.hidden) { return false; }
    var code = event.keyCode;
    if (code === 461 || code === 10009 || code === 8 || code === 27) { closeCleanup(); }
    else if (code === 13 || code === 32) {
      var active = document.activeElement;
      if (active && cleanup.contains(active) && !active.disabled) { active.click(); }
    } else if (code === 9 || (code >= 37 && code <= 40)) {
      var nodes = cleanup.querySelectorAll('input,button'), controls = [];
      for (var i = 0; i < nodes.length; i += 1) { if (!nodes[i].disabled) { controls.push(nodes[i]); } }
      if (controls.length) {
        var index = controls.indexOf(document.activeElement);
        var step = code === 37 || code === 38 || (code === 9 && event.shiftKey) ? -1 : 1;
        controls[(index + step + controls.length) % controls.length].focus();
      }
    } else { return true; }
    event.preventDefault();
    return true;
  }
  function bindRemove(button) {
    button.setAttribute('data-remove-bound', '1');
    var sent = false;
    button.onclick = function (event) {
      if (event) { event.preventDefault(); event.stopPropagation(); }
      if (sent || button.disabled || !window.confirm('Delete ' + button.getAttribute('data-remove-name') + ' from disk? Details and watch history will be kept.')) { return; }
      sent = true;
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('data-action-pending', '1');
      var request = new XMLHttpRequest();
      request.open('POST', button.getAttribute('data-remove-url'), true);
      request.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      request.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
      request.timeout = 15000;
      function failed() {
        button.removeAttribute('data-action-pending');
        status.textContent = 'Removal could not be confirmed. Reload Library to check files before trying again.';
      }
      request.onerror = request.ontimeout = failed;
      request.onload = function () {
        if (request.status !== 200) { failed(); return; }
        window.location.reload();
      };
      var body = 'confirm=remove';
      if (button.hasAttribute('data-remove-series')) {
        body += '&series=' + encodeURIComponent(button.getAttribute('data-remove-series')) +
          '&season=' + encodeURIComponent(button.getAttribute('data-remove-season'));
      }
      request.send(body);
    };
  }
  function bindNewEpisodes(input) {
    input.setAttribute('data-new-episodes-bound', '1');
    var busy = false;
    input.onchange = function () {
      if (busy) { return; }
      var enabled = input.checked;
      busy = true; input.disabled = true;
      input.setAttribute('data-action-pending', '1');
      var request = new XMLHttpRequest();
      request.open('POST', input.getAttribute('data-new-episodes-url'), true);
      request.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      request.setRequestHeader('Content-Type', 'application/json');
      request.timeout = 15000;
      function finished(error) {
        busy = false; input.disabled = false; input.removeAttribute('data-action-pending');
        if (error) { input.checked = !enabled; }
        status.textContent = error || (enabled ? 'New episodes will download automatically.' : 'Automatic downloads of new episodes are off.');
      }
      request.onerror = request.ontimeout = function () { finished('Could not confirm this setting. Reload Library to check it.'); };
      request.onload = function () {
        var data;
        try { data = JSON.parse(request.responseText); } catch (error) { request.onerror(); return; }
        if (request.status !== 200 || !data || typeof data.enabled !== 'boolean') { finished(data && data.error || 'Could not save this setting.'); return; }
        input.checked = enabled = data.enabled;
        finished();
      };
      request.send(JSON.stringify({enabled: enabled}));
    };
  }
  function bindDownload(button) {
    button.setAttribute('data-download-bound', '1');
    var sent = false;
    button.onclick = function (event) {
      if (event) { event.preventDefault(); event.stopPropagation(); }
      if (sent || button.disabled || button.getAttribute('aria-disabled') === 'true') { return; }
      sent = true;
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('data-action-pending', '1');
      var name = button.getAttribute('data-episode-name');
      var request = new XMLHttpRequest();
      status.textContent = 'Queuing ' + name + '…';
      request.open('POST', button.getAttribute('data-download-url'), true);
      request.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      request.setRequestHeader('Content-Type', 'application/json');
      request.timeout = 15000;
      function failed(message) {
        button.removeAttribute('data-action-pending');
        status.textContent = message || 'Request unconfirmed. Check Downloads before trying again.';
        button.textContent = 'Check Downloads';
      }
      request.onerror = request.ontimeout = function () { failed(); };
      request.onload = function () {
        var data;
        try { data = JSON.parse(request.responseText); } catch (error) { failed(); return; }
        if (request.status !== 200 || !data || data.queued !== true) { failed(data && data.error); return; }
        button.removeAttribute('data-action-pending');
        button.textContent = 'Queued';
        status.textContent = name + ' queued for download.';
        var state = button.parentNode.querySelector('[data-watch-state]');
        state.setAttribute('data-availability', 'Queued / downloading');
        state.textContent = 'Queued / downloading';
      };
      request.send('{}');
    };
  }
  function bind(button) {
    button.setAttribute('data-watch-bound', '1');
    var busy = false;
    button.onclick = function (event) {
      if (event) { event.preventDefault(); event.stopPropagation(); }
      if (busy) { return; }
      var watched = button.getAttribute('data-watched') !== '1';
      var name = button.getAttribute('data-episode-name');
      var request = new XMLHttpRequest();
      busy = true;
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('data-action-pending', '1');
      status.textContent = 'Saving ' + name + '…';
      function finished(error) {
        busy = false;
        button.removeAttribute('aria-disabled');
        button.removeAttribute('data-action-pending');
        status.textContent = error || name + (watched ? ' marked as watched.' : ' marked as unwatched.');
      }
      request.open('POST', button.getAttribute('data-watch-url'), true);
      request.setRequestHeader('Content-Type', 'application/json');
      request.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
      request.timeout = 15000;
      request.onerror = request.ontimeout = function () { finished('Could not save ' + name + '. Try again.'); };
      request.onload = function () {
        if (request.status < 200 || request.status >= 300) { request.onerror(); return; }
        var data;
        try { data = JSON.parse(request.responseText); } catch (error) { request.onerror(); return; }
        watched = Boolean(data.watched);
        button.setAttribute('data-watched', watched ? '1' : '0');
        button.textContent = watched ? 'Mark as unwatched' : 'Mark as watched';
        button.setAttribute('aria-label', button.textContent + ': ' + name);
        var state = button.parentNode.querySelector('[data-watch-state]');
        state.textContent = (watched ? 'Watched · ' : '') + state.getAttribute('data-availability');
        finished();
      };
      request.send(JSON.stringify({watched: watched}));
    };
  }
}());
