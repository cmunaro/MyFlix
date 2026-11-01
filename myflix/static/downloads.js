(function () {
  var root = document.getElementById('downloads');
  if (!root) { return; }
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var message = document.getElementById('download-message');
  var connection = document.getElementById('download-connection');
  var results = document.getElementById('download-results');
  var queueMessage = document.getElementById('queue-message');
  var queueBusy = false;
  var searchBusy = false;
  var cancelPending = {};
  function node(tag, text, className) {
    var el = document.createElement(tag);
    if (text !== undefined) { el.textContent = text; }
    if (className) { el.className = className; }
    return el;
  }
  function api(action, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(body === null ? 'GET' : 'POST', '/admin/vibravid/' + action, true);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.timeout = 35000;
    if (body !== null) { xhr.setRequestHeader('Content-Type', 'application/json'); xhr.setRequestHeader('X-CSRF-Token', token); }
    xhr.onload = function () {
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) { done('Could not read the response. Reload the page.'); return; }
      done(xhr.status === 200 ? null : (data.error || 'Request failed. Refresh the queue before retrying.'), data);
    };
    xhr.onerror = xhr.ontimeout = function () { done('Connection lost. Refresh the queue before retrying a download.'); };
    xhr.send(body === null ? null : JSON.stringify(body));
  }
  function refreshQueue() {
    if (queueBusy) { return; }
    queueBusy = true;
    api('queue', null, function (error, data) {
      queueBusy = false;
      if (error) { queueMessage.textContent = error; return; }
      queueMessage.textContent = '';
      var container = document.getElementById('download-queue');
      var focused = document.activeElement;
      var focusedJob = focused && container.contains(focused) ? focused.getAttribute('data-job-id') : null;
      var restore = null;
      container.textContent = '';
      var total = 0;
      ['active', 'scheduled'].forEach(function (group) {
        var items = data[group] || [];
        if (!items.length) { return; }
        var section = node('div', undefined, 'queue-group');
        section.appendChild(node('h3', group === 'active' ? 'Downloading now' : 'Up next'));
        items.forEach(function (item) {
          total += 1;
          var row = node('div', undefined, 'download-job');
          var info = node('div');
          info.appendChild(node('strong', item.title || 'Untitled download'));
          var progress = Number(item.progress);
          info.appendChild(node('p', [item.site, item.status, isFinite(progress) && item.progress !== undefined ? progress.toFixed(1) + '%' : '', item.speed].filter(Boolean).join(' · ')));
          if (isFinite(progress) && item.progress !== undefined) {
            var bar = node('progress'); bar.max = 100; bar.value = Math.max(0, Math.min(100, progress)); bar.setAttribute('aria-label', 'Download progress'); info.appendChild(bar);
          }
          row.appendChild(info);
          if (item.id) {
            var cancel = node('button', 'Cancel' + (item.series_name ? ' series' : ' download'));
            cancel.type = 'button';
            cancel.setAttribute('data-job-id', item.id);
            cancel.setAttribute('aria-label', 'Cancel ' + (item.title || 'download'));
            if (cancelPending[item.id]) { cancel.setAttribute('aria-disabled', 'true'); }
            if (String(item.id) === focusedJob) { restore = cancel; }
            cancel.onclick = function () {
              if (cancelPending[item.id]) { return; }
              cancelPending[item.id] = true;
              cancel.setAttribute('aria-disabled', 'true');
              api('cancel', {download_id: item.id}, function (err) {
                queueMessage.textContent = err || 'Cancellation requested, including related episodes.';
                delete cancelPending[item.id];
                cancel.removeAttribute('aria-disabled');
                if (!err) { refreshQueue(); }
              });
            };
            row.appendChild(cancel);
          }
          section.appendChild(row);
        });
        container.appendChild(section);
      });
      if (!total) { container.appendChild(node('p', 'Nothing downloading. Find a title above to get started.', 'queue-empty')); }
      if (focusedJob !== null) { (restore || document.getElementById('refresh-downloads')).focus(); }
    });
    api('status', null, function (error, data) {
      connection.textContent = error || ('Download engine connected · ' + data.slots.active + '/' + data.slots.max + ' active · ' + data.queued + ' queued' + (data.disk && data.disk.free !== undefined ? ' · ' + (data.disk.free / 1073741824).toFixed(1) + ' GB free' : ''));
    });
  }
  function resultCard(item) {
    var article = node('article', undefined, 'download-result');
    article.appendChild(node('h3', item.title || 'Untitled'));
    article.appendChild(node('p', [item.is_movie ? 'Film' : 'Series', item.year, item.site].filter(Boolean).join(' · ')));
    var controls = node('div', undefined, 'download-options');
    var choose = node('button', item.is_movie ? 'Download film' : 'Choose episodes');
    choose.type = 'button';
    controls.appendChild(choose);
    article.appendChild(controls);
    var payload = item.payload || {};
    var busy = false;
    payload.is_movie = Boolean(item.is_movie);
    function download(season, episodes) {
      if (busy) { return; }
      busy = true;
      choose.setAttribute('aria-disabled', 'true');
      choose.textContent = 'Sending…';
      var body = {site: item.site, payload: payload};
      if (season) { body.season = season; body.episodes = episodes; }
      api('download', body, function (error, data) {
        message.textContent = error || ('Queued: ' + (data.title || item.title));
        // On uncertain failures the user must inspect the queue before retrying.
        choose.textContent = error ? 'Request failed — check queue' : 'Queued';
        refreshQueue();
      });
    }
    choose.onclick = function () {
      if (busy) { return; }
      if (item.is_movie) { download(); return; }
      busy = true;
      choose.setAttribute('aria-disabled', 'true');
      api('seasons', {site: item.site, payload: payload}, function (error, data) {
        busy = false;
        choose.removeAttribute('aria-disabled');
        if (error) { message.textContent = error; return; }
        if (!data.seasons || !data.seasons.length) { message.textContent = 'No seasons available from this provider.'; return; }
        var label = node('label', 'Season');
        var season = node('select');
        season.setAttribute('aria-label', 'Season for ' + item.title);
        data.seasons.forEach(function (s) { var option = node('option', 'Season ' + s.number + ' · ' + s.episodes + ' episodes'); option.value = s.number; season.appendChild(option); });
        label.appendChild(season);
        var epLabel = node('label', 'Episodes');
        var episodes = node('input'); episodes.value = '*'; episodes.maxLength = 200; episodes.setAttribute('aria-label', 'Episodes: * for all, 1-4, or 1,3'); epLabel.appendChild(episodes);
        controls.insertBefore(label, choose); controls.insertBefore(epLabel, choose);
        choose.textContent = 'Download episodes';
        choose.onclick = function () { download(season.value, episodes.value); };
        if (document.activeElement === choose) { season.focus(); }
      });
    };
    return article;
  }
  document.getElementById('download-search').onsubmit = function (event) {
    event.preventDefault();
    if (searchBusy) { return; }
    searchBusy = true;
    var button = document.getElementById('search-downloads'); button.setAttribute('aria-disabled', 'true');
    message.textContent = 'Searching providers…'; results.textContent = '';
    api('search', {query: document.getElementById('download-query').value, site: document.getElementById('download-provider').value}, function (error, data) {
      searchBusy = false;
      button.removeAttribute('aria-disabled');
      if (error) { message.textContent = error; return; }
      var items = data.results || [];
      message.textContent = items.length + ' results' + (data.failed && data.failed.length ? ' · Some providers did not respond: ' + data.failed.join(', ') : '');
      items.forEach(function (item) { results.appendChild(resultCard(item)); });
      if (document.activeElement === button && items.length) { results.querySelector('button').focus(); }
    });
  };
  api('sites', null, function (error, data) {
    if (error) { connection.textContent = error; return; }
    var select = document.getElementById('download-provider'); select.textContent = '';
    (data.groups || []).forEach(function (group) {
      var options = node('optgroup'); options.label = group.label;
      (group.options || []).forEach(function (item) { var option = node('option', item.label); option.value = item.value; options.appendChild(option); });
      select.appendChild(options);
    });
  });
  document.getElementById('refresh-downloads').onclick = refreshQueue;
  refreshQueue();
  window.setInterval(function () { if (!document.hidden) { refreshQueue(); } }, 15000);
}());
