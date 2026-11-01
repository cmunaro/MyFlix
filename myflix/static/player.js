/* The player owns overlay visibility, focus and media keys. ES5 for VIDAA. */
(function () {
  var player = document.getElementById('player');
  var shell = document.getElementById('player-shell');
  var platform = window.MyFlixTV;
  var status = document.getElementById('player-status');
  var toggle = document.getElementById('toggle-play');
  var center = document.getElementById('center-play');
  var timeline = document.getElementById('timeline');
  var progress = document.getElementById('playback-progress');
  var remaining = document.getElementById('remaining-time');
  var mute = document.getElementById('mute');
  var rewind = document.getElementById('rewind');
  var forward = document.getElementById('forward');
  var nextLink = document.getElementById('next-episode');
  var nextPrompt = document.getElementById('next-prompt');
  var upNext = document.getElementById('up-next');
  var nextId = upNext.getAttribute('data-next-id');
  var overlays = Array.prototype.slice.call(shell.querySelectorAll('[data-player-overlay]'));
  var controls = Array.prototype.slice.call(shell.querySelectorAll('button,a,input')).filter(function (el) {
    return overlays.some(function (overlay) { return overlay.contains(el); });
  });
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var mediaId = player.getAttribute('data-id');
  var initialized = false, lastSave = 0, pendingSave = null;
  var switchingEpisode = false, autoAdvanceAttempted = false;
  var visible = true, scrubbing = false, hideTimer, noticeKind = '';
  // Keep native controls as the no-JavaScript fallback. Never display two UIs.
  player.controls = false;
  document.body.classList.add('custom-player');

  function label(button, text) { button.setAttribute('aria-label', text); button.title = text; }
  function canSeek() { return player.readyState >= 1 && isFinite(player.duration) && player.duration > 0; }
  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), rest = seconds % 60;
    return (hours ? hours + ':' + (minutes < 10 ? '0' : '') : '') + minutes + ':' + (rest < 10 ? '0' : '') + rest;
  }
  function render() {
    label(toggle, player.paused ? 'Play' : 'Pause');
    toggle.querySelector('.play-symbol').hidden = !player.paused;
    toggle.querySelector('.pause-symbol').hidden = player.paused;
    center.hidden = !player.paused;
    if (!scrubbing) { timeline.value = canSeek() ? player.currentTime : 0; }
    timeline.max = canSeek() ? player.duration : 0;
    timeline.disabled = !canSeek();
    rewind.disabled = forward.disabled = !canSeek();
    progress.max = canSeek() ? player.duration : 1;
    progress.value = canSeek() ? player.currentTime : 0;
    remaining.textContent = canSeek() ? '−' + clock(player.duration - player.currentTime) : '0:00';
    timeline.setAttribute('aria-valuetext', clock(player.currentTime || 0) + ' of ' + clock(canSeek() ? player.duration : 0));
    label(mute, player.muted ? 'Unmute' : 'Mute');
    mute.querySelector('.volume-symbol').hidden = player.muted;
    mute.querySelector('.muted-symbol').hidden = !player.muted;
    // On short clips use the final 10%, otherwise the last 30 seconds.
    var showNext = nextId && canSeek() && player.currentTime > 0 &&
      player.duration - player.currentTime <= Math.min(30, player.duration * 0.1) && !switchingEpisode;
    if (!showNext && upNext.contains(document.activeElement)) { player.focus(); }
    upNext.hidden = !showNext;
  }
  function showControls() {
    visible = true;
    document.body.classList.add('controls-visible');
    overlays.forEach(function (el) { el.setAttribute('aria-hidden', 'false'); });
    controls.forEach(function (el) { el.removeAttribute('tabindex'); });
    window.clearTimeout(hideTimer);
    if (!player.paused && !scrubbing) { hideTimer = window.setTimeout(hideControls, 3000); }
  }
  function hideControls(force) {
    if (!force && (player.paused || scrubbing)) { return; }
    window.clearTimeout(hideTimer);
    // A focused button must not keep the overlay alive or receive hidden clicks.
    if (overlays.some(function (el) { return el.contains(document.activeElement); })) { player.focus(); }
    visible = false;
    document.body.classList.remove('controls-visible');
    overlays.forEach(function (el) { el.setAttribute('aria-hidden', 'true'); });
    controls.forEach(function (el) { el.setAttribute('tabindex', '-1'); });
  }
  function notify(text, kind) {
    noticeKind = kind || '';
    status.textContent = text;
    status.hidden = !text;
    if (text) { showControls(); }
  }
  function play() {
    var result = player.play();
    if (result && result.catch) {
      result.catch(function () { render(); showControls(); });
    }
  }
  function togglePlay() { if (player.paused) { play(); } else { player.pause(); } }
  function seek(offset) {
    if (canSeek()) { player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + offset)); render(); }
    showControls();
  }
  function save(explicit) {
    if (!canSeek()) { return; }
    if (pendingSave && typeof explicit !== 'boolean') { return; }
    var savedId = mediaId;
    var body = {position: Math.min(player.currentTime, player.duration), duration: player.duration};
    if (typeof explicit === 'boolean') { body.watched = explicit; }
    var request = new XMLHttpRequest();
    request.open('POST', '/api/progress/' + savedId, true);
    request.setRequestHeader('Content-Type', 'application/json');
    request.setRequestHeader('X-CSRF-Token', token);
    request.timeout = 15000;
    pendingSave = request;
    request.onload = function () {
      if (pendingSave === request) { pendingSave = null; }
      if (savedId !== mediaId) { return; }
      if (request.status !== 200) { notify('Could not save your place. Reload the page and try again.', 'save'); return; }
      if (noticeKind === 'save') { notify(''); }
      render();
    };
    request.onerror = request.ontimeout = function () {
      if (pendingSave === request) { pendingSave = null; }
      if (savedId === mediaId) { notify('Connection lost. Your place will be saved when the server returns.', 'save'); }
    };
    request.send(JSON.stringify(body));
  }
  function initializeMedia() {
    if (initialized || !canSeek()) { return; }
    initialized = true;
    var resume = parseFloat(player.getAttribute('data-position')) || 0;
    if (resume > 0 && resume < player.duration - 5) { player.currentTime = resume; }
    render();
    play(); // A blocked autoplay leaves the central Play action available.
  }
  player.addEventListener('loadedmetadata', initializeMedia);
  player.addEventListener('durationchange', function () { initializeMedia(); render(); });
  player.addEventListener('play', function () { document.body.classList.add('playing'); if (noticeKind !== 'save') { notify(''); } render(); showControls(); });
  player.addEventListener('pause', function () { document.body.classList.remove('playing'); render(); showControls(); save(); });
  player.addEventListener('ended', function () {
    render(); showControls(); save(true);
    if (nextId && !autoAdvanceAttempted) { autoAdvanceAttempted = true; advanceEpisode(); }
  });
  player.addEventListener('timeupdate', function () {
    render();
    if (Date.now() - lastSave > 10000) { lastSave = Date.now(); save(); }
  });
  player.addEventListener('volumechange', render);
  player.addEventListener('error', function () { notify('This file could not play. Check the file and TV codec support. MP4 with H.264 video and AAC audio is recommended; MyFlix does not transcode.', 'playback'); });
  toggle.onclick = center.onclick = togglePlay;
  player.onclick = togglePlay;
  rewind.onclick = function () { seek(-10); };
  forward.onclick = function () { seek(10); };
  function advanceEpisode(event) {
    if (event) { event.preventDefault(); }
    if (!nextId || switchingEpisode) { return; }
    switchingEpisode = true;
    save();
    player.pause();
    render();
    notify('Loading next episode…', 'next');
    var request = new XMLHttpRequest();
    request.open('GET', '/api/playback/' + nextId, true);
    request.timeout = 15000;
    request.onload = function () {
      if (request.status !== 200) { failed(); return; }
      var data;
      try { data = JSON.parse(request.responseText); } catch (error) { failed(); return; }
      if (!data || !data.id || !data.stream_url || !data.watch_url) { failed(); return; }
      mediaId = String(data.id);
      player.setAttribute('data-id', mediaId);
      player.setAttribute('data-position', data.position || 0);
      player.setAttribute('aria-label', data.title);
      nextId = data.next ? String(data.next.id) : '';
      nextLink.hidden = !nextId;
      nextLink.href = nextPrompt.href = data.next ? data.next.watch_url : '#';
      upNext.setAttribute('data-next-id', nextId);
      document.getElementById('next-episode-info').textContent = data.next ? 'S' + data.next.season + ':E' + data.next.episode + ' · ' + data.next.title : '';
      document.getElementById('episode-heading').textContent = data.series ? data.series + ' · S' + data.season + ':E' + data.episode : 'Now playing';
      document.getElementById('episode-title').textContent = data.title;
      document.getElementById('playing-title').textContent = (data.series ? 'S' + data.season + ':E' + data.episode + ' · ' : '') + data.title;
      document.title = data.title + ' · MyFlix';
      if (window.history && window.history.replaceState) { window.history.replaceState(null, '', data.watch_url); }
      initialized = false; autoAdvanceAttempted = false; switchingEpisode = false;
      lastSave = 0; pendingSave = null;
      notify('');
      player.src = data.stream_url;
      player.load();
      player.focus();
      render();
      initializeMedia();
    };
    function failed() {
      switchingEpisode = false;
      notify('Could not load the next episode. Select Next episode to try again.', 'next');
      render();
    }
    request.onerror = request.ontimeout = failed;
    request.send();
  }
  nextLink.onclick = nextPrompt.onclick = advanceEpisode;
  mute.onclick = function () { player.muted = !player.muted; render(); showControls(); };
  timeline.oninput = function () {
    if (canSeek()) { player.currentTime = Number(timeline.value); render(); }
    showControls();
  };
  function beginScrub() { scrubbing = true; showControls(); }
  function endScrub() { if (scrubbing) { scrubbing = false; render(); showControls(); save(); } }
  timeline.addEventListener('mousedown', beginScrub);
  timeline.addEventListener('touchstart', beginScrub);
  document.addEventListener('mouseup', endScrub);
  document.addEventListener('touchend', endScrub);
  timeline.addEventListener('change', endScrub);
  function fullscreen() {
    if (platform.fullscreenElement()) { platform.exitFullscreen(); return; }
    function unavailable() { notify('Fullscreen is unavailable in this browser.', 'fullscreen'); }
    if (platform.enterFullscreen(shell, unavailable)) { return; }
    if (player.webkitEnterFullscreen) {
      try { player.webkitEnterFullscreen(); } catch (error) { unavailable(); }
    } else { unavailable(); }
  }
  document.getElementById('fullscreen').onclick = fullscreen;
  function fullscreenChanged() { label(document.getElementById('fullscreen'), platform.fullscreenElement() ? 'Exit fullscreen' : 'Fullscreen'); showControls(); }
  document.addEventListener('fullscreenchange', fullscreenChanged);
  document.addEventListener('webkitfullscreenchange', fullscreenChanged);
  // VIDAA's virtual pointer can emit movement while a remote key is held.
  // It must not keep revealing playback chrome in remote-navigation mode.
  document.addEventListener('mousemove', function () { if (!platform.isTV) { showControls(); } });
  document.addEventListener('touchstart', showControls);
  document.addEventListener('focusin', function (event) { if (event.target !== player) { showControls(); } });
  document.addEventListener('keydown', function (event) {
    var code = event.keyCode, active = document.activeElement;
    var onControl = active && active !== player && shell.contains(active);
    if (code === 27 || code === 461 || code === 10009 || code === 8) {
      event.preventDefault();
      if (platform.fullscreenElement()) { platform.exitFullscreen(); }
      else if (visible && !player.paused) { hideControls(true); }
      else { location.href = '/'; }
      return;
    }
    if (code === 415) { play(); }
    else if (code === 19 || code === 413) { player.pause(); }
    else if (code === 13 && onControl && active !== timeline) { active.click(); }
    else if (code === 179 || code === 32 || (code === 13 && !onControl)) { togglePlay(); }
    else if (code === 412 || code === 417 || ((code === 37 || code === 39) && (!onControl || active === timeline))) {
      seek(code === 37 || code === 412 ? -10 : 10);
    } else if (code === 38 || code === 40) {
      showControls();
      (code === 38 ? timeline : !upNext.hidden ? nextPrompt : toggle).focus();
    } else if ((code === 37 || code === 39) && onControl) {
      var buttons = controls.filter(function (el) { return !el.disabled && !el.hidden && el.offsetWidth && el.offsetHeight; });
      if (!upNext.hidden) { buttons.push(nextPrompt); }
      var next = buttons[buttons.indexOf(active) + (code === 37 ? -1 : 1)];
      if (next) { next.focus(); }
    } else { showControls(); return; }
    event.preventDefault();
    showControls();
  });
  // Beacon survives navigation and includes the same CSRF check as regular saves.
  window.addEventListener('pagehide', function () {
    if (canSeek() && navigator.sendBeacon) {
      var payload = new FormData();
      payload.append('csrf', token);
      payload.append('position', String(Math.min(player.currentTime, player.duration)));
      payload.append('duration', String(player.duration));
      navigator.sendBeacon('/api/progress/' + mediaId, payload);
    }
  });
  render();
  showControls();
  player.focus();
  // Metadata can already be cached before deferred JavaScript attaches listeners.
  initializeMedia();
}());
