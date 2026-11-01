(function () {
  var root = document.getElementById('downloads');
  if (!root) { return; }
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var message = document.getElementById('download-message');
  var connection = document.getElementById('download-connection');
  var results = document.getElementById('download-results');
  var queueMessage = document.getElementById('queue-message');
  var queueBusy = false;
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
      try { data = JSON.parse(xhr.responseText); } catch (e) { done('Session expired. Reload and sign in again.'); return; }
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
      container.textContent = '';
      var total = 0;
      ['active', 'scheduled', 'history'].forEach(function (group) {
        var items = data[group] || [];
        if (!items.length) { return; }
        container.appendChild(node('h3', group === 'active' ? 'Downloading' : group === 'scheduled' ? 'Queued' : 'Recent history'));
        items.forEach(function (item) {
          total += 1;
          var row = node('div', undefined, 'download-job');
          var info = node('div');
          info.appendChild(node('strong', item.title || 'Untitled download'));
          var progress = Number(item.progress);
          info.appendChild(node('p', [item.site, item.status, isFinite(progress) && item.progress !== undefined ? progress.toFixed(1) + '%' : '', item.speed].filter(Boolean).join(' · ')));
          row.appendChild(info);
          if (group !== 'history' && item.id) {
            var cancel = node('button', 'Cancel' + (item.series_name ? ' series' : ' download'));
            cancel.type = 'button';
            cancel.onclick = function () {
              cancel.disabled = true;
              api('cancel', {download_id: item.id}, function (err) {
                queueMessage.textContent = err || 'Cancellation requested, including related episodes.';
                cancel.disabled = false;
                if (!err) { refreshQueue(); }
              });
            };
            row.appendChild(cancel);
          }
          container.appendChild(row);
        });
      });
      if (!total) { container.appendChild(node('p', 'No downloads yet. Search for a title above.')); }
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
    payload.is_movie = Boolean(item.is_movie);
    function download(season, episodes) {
      choose.disabled = true;
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
      if (item.is_movie) { download(); return; }
      choose.disabled = true;
      api('seasons', {site: item.site, payload: payload}, function (error, data) {
        if (error) { message.textContent = error; choose.disabled = false; return; }
        if (!data.seasons || !data.seasons.length) { message.textContent = 'No seasons available from this provider.'; choose.disabled = false; return; }
        var label = node('label', 'Season');
        var season = node('select');
        data.seasons.forEach(function (s) { var option = node('option', 'Season ' + s.number + ' · ' + s.episodes + ' episodes'); option.value = s.number; season.appendChild(option); });
        label.appendChild(season);
        var epLabel = node('label', 'Episodes');
        var episodes = node('input'); episodes.value = '*'; episodes.maxLength = 200; episodes.setAttribute('aria-label', 'Episodes: * for all, 1-4, or 1,3'); epLabel.appendChild(episodes);
        controls.insertBefore(label, choose); controls.insertBefore(epLabel, choose);
        choose.textContent = 'Download episodes'; choose.disabled = false;
        choose.onclick = function () { download(season.value, episodes.value); };
      });
    };
    return article;
  }
  document.getElementById('download-search').onsubmit = function (event) {
    event.preventDefault();
    var button = document.getElementById('search-downloads'); button.disabled = true;
    message.textContent = 'Searching providers…'; results.textContent = '';
    api('search', {query: document.getElementById('download-query').value, site: document.getElementById('download-provider').value}, function (error, data) {
      button.disabled = false;
      if (error) { message.textContent = error; return; }
      var items = data.results || [];
      message.textContent = items.length + ' results' + (data.failed && data.failed.length ? ' · Some providers did not respond: ' + data.failed.join(', ') : '');
      items.forEach(function (item) { results.appendChild(resultCard(item)); });
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
