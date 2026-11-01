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
  var retryButton = document.getElementById('retry-failed-downloads');
  var retryBusy = false;
  function node(tag, text, className) {
    var el = document.createElement(tag);
    if (text !== undefined) { el.textContent = text; }
    if (className) { el.className = className; }
    return el;
  }
  function episodeDetails(item) {
    if (/^(film|movie)$/i.test(item.type || '')) { return ''; }
    var season = /^\d+$/.test(String(item.season)) ? Number(item.season) : null;
    var episode = /^\d+$/.test(String(item.episode)) ? Number(item.episode) : null;
    var details = [];
    if (season !== null && (season > 0 || episode > 0)) { details.push('Season ' + season); }
    if (episode !== null && episode > 0) { details.push('Episode ' + episode); }
    return details.join(' · ');
  }
  function api(action, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(body === null ? 'GET' : 'POST', action === 'follow' ? '/admin/follow' : action === 'retry-failed' ? '/admin/downloads/retry-failed' : '/admin/vibravid/' + action, true);
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
  function refreshQueue(notice) {
    if (queueBusy) { return; }
    queueBusy = true;
    api('queue', null, function (error, data) {
      queueBusy = false;
      if (error) { queueMessage.textContent = error; return; }
      queueMessage.textContent = typeof notice === 'string' ? notice : '';
      var container = document.getElementById('download-queue');
      var focused = document.activeElement;
      var focusedJob = focused && container.contains(focused) ? focused.getAttribute('data-job-id') : null;
      var restore = null;
      container.textContent = '';
      var total = 0;
      var failures = (data.history || []).filter(function (item) { return item.status === 'failed'; });
      retryButton.hidden = !failures.length;
      if (retryButton.hidden && focused === retryButton) { document.getElementById('refresh-downloads').focus(); }
      ['retrying', 'active', 'scheduled', 'failed'].forEach(function (group) {
        var items = group === 'failed' ? failures : data[group] || [];
        if (!items.length) { return; }
        var section = node('div', undefined, 'queue-group');
        section.appendChild(node('h3', group === 'retrying' ? 'Priority retries' : group === 'active' ? 'Working jobs' : group === 'scheduled' ? 'Up next' : 'Recent failures'));
        items.forEach(function (item) {
          total += 1;
          var row = node('div', undefined, 'download-job');
          var info = node('div');
          info.appendChild(node('strong', item.title || 'Untitled download'));
          var episode = episodeDetails(item);
          if (episode) { info.appendChild(node('p', episode)); }
          var progress = Number(item.progress);
          info.appendChild(node('p', [item.priority ? 'Priority retry' : '', item.site, item.status, group !== 'retrying' && isFinite(progress) && item.progress !== undefined ? progress.toFixed(1) + '%' : '', group !== 'retrying' ? item.speed : ''].filter(Boolean).join(' · ')));
          if (group === 'failed') {
            info.appendChild(node('p', item.error || 'Download failed; the engine supplied no details.', 'error'));
            var following = node('a', 'Review episodes in Following', 'button secondary');
            following.href = '/admin?section=following';
            var failureId = 'failed:' + item.id + ':' + item.season + ':' + item.episode;
            following.setAttribute('data-job-id', failureId);
            if (failureId === focusedJob) { restore = following; }
            info.appendChild(following);
          }
          if (group === 'retrying' && item.error) { info.appendChild(node('p', item.error, 'error')); }
          if (group !== 'failed' && group !== 'retrying' && isFinite(progress) && item.progress !== undefined) {
            var bar = node('progress'); bar.max = 100; bar.value = Math.max(0, Math.min(100, progress)); bar.setAttribute('aria-label', 'Download progress'); info.appendChild(bar);
          }
          row.appendChild(info);
          if (item.id && (group === 'active' || group === 'scheduled')) {
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
      connection.textContent = error || ('Download engine connected · ' + data.slots.active + '/' + data.slots.max + ' working jobs · ' + data.queued + ' queued' + (data.disk && data.disk.free !== undefined ? ' · ' + (data.disk.free / 1073741824).toFixed(1) + ' GB free' : ''));
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
    if (!item.is_movie) {
      var seriesLabel = node('label', 'Library series name (links watch history)');
      var seriesName = node('input'); seriesName.value = item.title || payload.name || ''; seriesName.maxLength = 300;
      seriesLabel.appendChild(seriesName); controls.appendChild(seriesLabel);
      var follow = node('button', 'Follow series'); follow.type = 'button'; controls.appendChild(follow);
      var following = false;
      follow.onclick = function () {
        if (following) { return; }
        following = true; follow.setAttribute('aria-disabled', 'true');
        api('follow', {site: item.site, payload: payload, series: seriesName.value}, function (error) {
          message.textContent = error || 'Following series. Episode lookup queued. Track fetching progress and any errors in Followed series checks below. Missing unwatched episodes will start downloading automatically after the episode list is ready; watched episodes and excluded seasons are skipped.';
          follow.textContent = error ? 'Check Following before retrying' : 'Following';
        });
      };
      var followingLink = node('a', 'Manage followed series', 'button secondary');
      followingLink.href = '/admin?section=following'; controls.appendChild(followingLink);
    }
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
  retryButton.onclick = function () {
    if (retryBusy) { return; }
    retryBusy = true;
    retryButton.setAttribute('aria-disabled', 'true');
    api('retry-failed', {}, function (error, data) {
      retryBusy = false;
      retryButton.removeAttribute('aria-disabled');
      queueMessage.textContent = error || ('Queued ' + data.queued + ' priority retries.' + (data.skipped ? ' ' + data.skipped + ' downloads could not be retried; review their details.' : ''));
      if (!error) {
        if (document.activeElement === retryButton) { document.getElementById('refresh-downloads').focus(); }
        refreshQueue(queueMessage.textContent);
      }
    });
  };
  refreshQueue();
  window.setInterval(function () { if (!document.hidden) { refreshQueue(); } }, 15000);
}());
