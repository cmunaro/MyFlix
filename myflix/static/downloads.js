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
  var selectionBusy = false;
  var pickerSequence = 0;
  var cancelPending = {};
  var retryButton = document.getElementById('retry-failed-downloads');
  var retryBusy = false;
  var cancelAllButton = document.getElementById('cancel-all-downloads');
  var cancelAllBusy = false;
  var cancelTargets = [];
  var unconfirmedDownloads = false;
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
    xhr.open(body === null ? 'GET' : 'POST', action === 'series-start' ? '/admin/downloads/series' : action === 'retry-failed' ? '/admin/downloads/retry-failed' : '/admin/vibravid/' + action, true);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.timeout = action === 'episodes' ? 310000 : 35000;
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
    if (queueBusy || cancelAllBusy) { return; }
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
      cancelTargets = [];
      unconfirmedDownloads = (data.retrying || []).some(function (item) { return !item.cancel_id; });
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
          info.appendChild(node('p', [item.priority ? 'Priority retry' : '', item.site, item.status, group === 'active' && isFinite(progress) && item.progress !== undefined ? progress.toFixed(1) + '%' : '', group === 'active' ? item.speed : ''].filter(Boolean).join(' · ')));
          if (group === 'failed') {
            info.appendChild(node('p', item.error || 'Download failed; the engine supplied no details.', 'error'));
            var following = node('a', 'Find this title', 'button secondary');
            following.href = '/admin?section=downloads&q=' + encodeURIComponent(item.series_name || item.title || '');
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
          var cancelId = item.cancel_id || ((group === 'active' || group === 'scheduled') ? item.id : null);
          if (cancelId) {
            cancelTargets.push(cancelId);
            var cancel = node('button', 'Cancel' + (group !== 'failed' && item.series_name ? ' series' : ' download'));
            cancel.type = 'button';
            cancel.setAttribute('data-job-id', cancelId);
            cancel.setAttribute('aria-label', 'Cancel ' + (item.title || 'download'));
            if (cancelPending[cancelId] || cancelAllBusy) { cancel.setAttribute('aria-disabled', 'true'); }
            if (String(cancelId) === focusedJob) { restore = cancel; }
            cancel.onclick = function () {
              if (cancelPending[cancelId] || cancelAllBusy || retryBusy) { return; }
              cancelPending[cancelId] = true;
              cancel.setAttribute('aria-disabled', 'true');
              api('cancel', {download_id: cancelId}, function (err) {
                queueMessage.textContent = err || (group === 'failed' ? 'Failed download cancelled.' : 'Cancellation requested, including related episodes.');
                delete cancelPending[cancelId];
                cancel.removeAttribute('aria-disabled');
                if (!err) { refreshQueue(queueMessage.textContent); }
              });
            };
            row.appendChild(cancel);
          }
          section.appendChild(row);
        });
        container.appendChild(section);
      });
      cancelAllButton.hidden = !cancelTargets.length;
      if (cancelAllButton.hidden && focused === cancelAllButton) { document.getElementById('refresh-downloads').focus(); }
      (data.checks || []).forEach(function (check) {
        total += 1;
        container.appendChild(node('p', 'Could not check new episodes for ' + check.series + ': ' + check.error, 'error'));
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
      message.textContent = 'Loading seasons and episodes… Large series can take several minutes.';
      api('episodes', {site: item.site, payload: payload}, function (error, data) {
        busy = false;
        if (!results.contains(article)) { return; }
        choose.removeAttribute('aria-disabled');
        if (error) { message.textContent = error; return; }
        if (!data || !Array.isArray(data.episodes) || !data.episodes.length) { message.textContent = 'No episodes available from this provider.'; return; }
        if (data.episodes.length > 20000 || data.episodes.some(function (episode) {
          return !episode || typeof episode.season !== 'number' || episode.season % 1 || episode.season < 0 || episode.season > 999 ||
            typeof episode.episode !== 'number' || episode.episode % 1 || episode.episode < 1 || episode.episode > 9999;
        })) { message.textContent = 'Provider episode numbering is unsupported.'; return; }
        var seasons = [];
        var grouped = {};
        var seen = {};
        data.episodes.forEach(function (episode) {
          var key = String(episode.season);
          if (!grouped[key]) { grouped[key] = []; seasons.push(Number(key)); }
          var identity = key + ':' + episode.episode;
          if (!seen[identity]) {
            seen[identity] = true;
            grouped[key].push(episode);
          }
        });
        seasons.sort(function (a, b) { return a - b; });
        var picker = node('div', undefined, 'episode-picker');
        var selections = [];
        var allLabel = node('label', 'All seasons', 'checkbox');
        var all = node('input'); all.type = 'checkbox'; all.checked = true;
        allLabel.insertBefore(all, allLabel.firstChild || null);
        picker.appendChild(allLabel);
        all.onchange = function () {
          selections.forEach(function (selection) {
            selection.episodes.forEach(function (entry) { if (!entry.checkbox.disabled) { entry.checkbox.checked = all.checked; } });
          });
          sync();
        };
        var submit = node('button', 'Download'); submit.type = 'button';
        var onlyNew = node('button', 'Only new episodes', 'secondary'); onlyNew.type = 'button';
        function sync() {
          var count = 0;
          selections.forEach(function (selection) {
            var selected = selection.episodes.filter(function (entry) { return entry.checkbox.checked; }).length;
            selection.checkbox.checked = selected === selection.episodes.length;
            selection.checkbox.indeterminate = selected > 0 && selected < selection.episodes.length;
            selection.checkbox.disabled = selection.episodes.every(function (entry) { return entry.checkbox.disabled; });
            count += selection.episodes.filter(function (entry) { return entry.checkbox.checked && !entry.checkbox.disabled; }).length;
          });
          var checked = selections.reduce(function (sum, selection) {
            return sum + selection.episodes.filter(function (entry) { return entry.checkbox.checked; }).length;
          }, 0);
          var total = selections.reduce(function (sum, selection) { return sum + selection.episodes.length; }, 0);
          all.checked = checked === total;
          all.indeterminate = checked > 0 && checked < total;
          all.disabled = selections.every(function (selection) { return selection.checkbox.disabled; });
          submit.setAttribute('aria-disabled', count ? 'false' : 'true');
        }
        seasons.forEach(function (number) {
          var section = node('div', undefined, 'episode-season');
          var header = node('div', undefined, 'episode-season-heading');
          var label = node('label', 'Season ' + number, 'checkbox');
          var checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = true;
          label.insertBefore(checkbox, label.firstChild || null);
          header.appendChild(label);
          var expand = node('button', '▸'); expand.type = 'button';
          var list = node('div', undefined, 'episode-list'); list.hidden = true;
          list.id = 'episode-list-' + (++pickerSequence);
          expand.setAttribute('aria-label', 'Expand season ' + number);
          expand.setAttribute('aria-expanded', 'false');
          expand.setAttribute('aria-controls', list.id);
          expand.onclick = function () {
            list.hidden = !list.hidden;
            expand.textContent = list.hidden ? '▸' : '▾';
            expand.setAttribute('aria-expanded', list.hidden ? 'false' : 'true');
            expand.setAttribute('aria-label', (list.hidden ? 'Expand' : 'Collapse') + ' season ' + number);
          };
          header.appendChild(expand); section.appendChild(header);
          var selection = {number: number, checkbox: checkbox, episodes: []};
          grouped[String(number)].sort(function (a, b) { return a.episode - b.episode; }).forEach(function (episode) {
            var episodeLabel = node('label', 'Episode ' + episode.episode + (episode.title ? ' · ' + episode.title : '') + (episode.available ? ' · In library' : episode.locked ? ' · Queued / request pending' : ''), 'checkbox');
            var input = node('input'); input.type = 'checkbox'; input.checked = true; input.disabled = Boolean(episode.available || episode.locked);
            episodeLabel.insertBefore(input, episodeLabel.firstChild || null);
            input.onchange = sync;
            selection.episodes.push({number: episode.episode, checkbox: input});
            list.appendChild(episodeLabel);
          });
          checkbox.onchange = function () {
            selection.episodes.forEach(function (entry) { if (!entry.checkbox.disabled) { entry.checkbox.checked = checkbox.checked; } });
            sync();
          };
          selections.push(selection); section.appendChild(list); picker.appendChild(section);
        });
        var actions = node('div', undefined, 'series-download-actions');
        actions.appendChild(submit); actions.appendChild(onlyNew); picker.appendChild(actions);
        sync();
        controls.appendChild(picker);
        choose.hidden = true;
        message.textContent = 'Select seasons or expand a season to choose individual episodes.';
        (all.disabled ? onlyNew : all).focus();
        function start(onlyNewEpisodes) {
          if (busy || selectionBusy) { return; }
          var selection = [];
          if (!onlyNewEpisodes) {
            selections.forEach(function (season) {
              season.episodes.forEach(function (entry) {
                if (entry.checkbox.checked && !entry.checkbox.disabled) { selection.push({season: season.number, episode: entry.number}); }
              });
            });
            if (!selection.length) { message.textContent = 'Select at least one episode.'; return; }
          }
          busy = selectionBusy = true;
          submit.setAttribute('aria-disabled', 'true'); onlyNew.setAttribute('aria-disabled', 'true');
          var action = onlyNewEpisodes ? onlyNew : submit;
          action.textContent = 'Saving…';
          Array.prototype.forEach.call(picker.querySelectorAll('input'), function (input) { input.disabled = true; });
          api('series-start', {series_id: data.series_id, source: data.source, selection: selection, only_new: onlyNewEpisodes}, function (error, result) {
            selectionBusy = false;
            if (error || !result || result.queued !== true) {
              message.textContent = (error || 'The selection was not acknowledged.') + ' Check the queue before trying again.';
              action.textContent = 'Request unconfirmed — check queue'; refreshQueue(); return;
            }
            message.textContent = onlyNewEpisodes ? 'Only future episodes of ' + item.title + ' will download automatically. Episodes listed now are skipped.' :
              'Queued ' + result.selected + ' episodes of ' + item.title + '. Future episodes will download automatically.';
            results.textContent = '';
            document.getElementById('search-downloads').focus();
            refreshQueue();
          });
        }
        submit.onclick = function () { start(false); };
        onlyNew.onclick = function () { start(true); };
      });
    };
    return article;
  }
  document.getElementById('download-search').onsubmit = function (event) {
    event.preventDefault();
    if (searchBusy || selectionBusy) { return; }
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
    if (retryBusy || cancelAllBusy || Object.keys(cancelPending).length) { return; }
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
  cancelAllButton.onclick = function () {
    if (cancelAllBusy || retryBusy || Object.keys(cancelPending).length || !cancelTargets.length) { return; }
    var targets = cancelTargets.slice();
    var unconfirmed = unconfirmedDownloads;
    cancelAllBusy = true;
    cancelAllButton.setAttribute('aria-disabled', 'true');
    retryButton.setAttribute('aria-disabled', 'true');
    function next() {
      var identity = targets.shift();
      queueMessage.textContent = 'Cancelling downloads…';
      api('cancel', {download_id: identity}, function (error) {
        if (!error && targets.length) { next(); return; }
        cancelAllBusy = false;
        cancelAllButton.removeAttribute('aria-disabled');
        retryButton.removeAttribute('aria-disabled');
        queueMessage.textContent = error || (unconfirmed ? 'Cancellation requested. Unconfirmed download requests require queue review.' : 'Cancellation requested for all current downloads.');
        refreshQueue(queueMessage.textContent);
      });
    }
    next();
  };
  refreshQueue();
  window.setInterval(function () { if (!document.hidden) { refreshQueue(); } }, 15000);
}());
