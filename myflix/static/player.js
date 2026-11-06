/* The player owns overlay visibility, focus and media keys. ES5 for VIDAA. */
(function () {
  var player = document.getElementById('player');
  var shell = document.getElementById('player-shell');
  var platform = window.MyFlixTV;
  var status = document.getElementById('player-status');
  var loading = document.getElementById('player-loading');
  var toggle = document.getElementById('toggle-play');
  var center = document.getElementById('center-play');
  var timeline = document.getElementById('timeline');
  var progress = document.getElementById('playback-progress');
  var remaining = document.getElementById('remaining-time');
  var mute = document.getElementById('mute');
  var rewind = document.getElementById('rewind');
  var forward = document.getElementById('forward');
  var trackButton = document.getElementById('track-settings');
  var trackPanel = document.getElementById('track-panel');
  var closeTrackButton = document.getElementById('close-tracks');
  var audioOptions = document.getElementById('audio-track-options');
  var subtitleOptions = document.getElementById('subtitle-track-options');
  var trackMessage = document.getElementById('track-message');
  var trackLists = [];
  var subtitlesChosen = false;
  var nextLink = document.getElementById('next-episode');
  var nextPrompt = document.getElementById('next-prompt');
  var upNext = document.getElementById('up-next');
  var nextId = upNext.getAttribute('data-next-id');
  var overlays = Array.prototype.slice.call(shell.querySelectorAll('[data-player-overlay]'));
  var controls = Array.prototype.slice.call(shell.querySelectorAll('button,a,input')).filter(function (el) {
    return !trackPanel.contains(el) && overlays.some(function (overlay) { return overlay.contains(el); });
  });
  var token = document.querySelector('meta[name="csrf-token"]').content;
  var mediaId = player.getAttribute('data-id');
  var initialized = false, lastSave = 0, pendingSave = null;
  var switchingEpisode = false, autoAdvanceAttempted = false;
  var visible = true, scrubbing = false, mouseScrubbing = false, hideTimer, noticeKind = '';
  // Keep native controls as the no-JavaScript fallback. Never display two UIs.
  player.controls = false;
  document.body.classList.add('custom-player');

  function label(button, text) { button.setAttribute('aria-label', text); button.title = text; }
  function tracks(kind) {
    var list = kind === 'audio' ? player.audioTracks : player.textTracks;
    var result = [], i;
    for (i = 0; list && i < list.length; i += 1) {
      if (kind === 'audio' || list[i].kind === 'subtitles' || list[i].kind === 'captions') { result.push(list[i]); }
    }
    return result;
  }
  function trackControls() { return Array.prototype.slice.call(trackPanel.querySelectorAll('button')); }
  function closeTracks(restoreFocus) {
    var focused = trackPanel.contains(document.activeElement);
    trackPanel.hidden = true;
    trackButton.setAttribute('aria-expanded', 'false');
    if (focused || restoreFocus) { (restoreFocus && !trackButton.hidden ? trackButton : player).focus(); }
  }
  function selectTrack(kind, selected) {
    var available = tracks(kind), previous = [], property = kind === 'audio' ? 'enabled' : 'mode';
    if (selected && available.indexOf(selected) === -1) { refreshTracks(); return; }
    available.forEach(function (track) { previous.push(track[property]); });
    try {
      // Enable the chosen audio first so a failed switch cannot leave silence.
      if (kind === 'audio') { selected.enabled = true; }
      available.forEach(function (track) {
        track[property] = kind === 'audio' ? track === selected : track === selected ? 'showing' : 'disabled';
      });
      if (available.some(function (track) {
        return track[property] !== (kind === 'audio' ? track === selected : track === selected ? 'showing' : 'disabled');
      })) { throw new Error('Track selection unavailable'); }
      if (kind !== 'audio') { subtitlesChosen = true; }
      trackMessage.textContent = '';
    } catch (error) {
      available.forEach(function (track, index) { try { track[property] = previous[index]; } catch (ignored) {} });
      trackMessage.textContent = 'This browser could not switch the selected track.';
    }
    refreshTracks();
    showControls();
  }
  function refreshTracks() {
    var audio = tracks('audio'), subtitles = tracks('subtitles');
    // Override native/default subtitle activation until the viewer chooses a
    // subtitle setting for this video, including tracks discovered later.
    if (!subtitlesChosen) {
      subtitles.forEach(function (track) {
        if (track.mode !== 'disabled') { try { track.mode = 'disabled'; } catch (ignored) {} }
      });
    }
    var active = document.activeElement, focused = trackPanel.contains(active);
    var restore = null;
    trackButton.hidden = audio.length < 2 && !subtitles.length;
    if (trackButton.hidden) {
      closeTracks(false);
      if (active === trackButton) { player.focus(); }
      return;
    }
    if (trackPanel.hidden) { return; }
    audioOptions.textContent = ''; subtitleOptions.textContent = '';
    document.getElementById('audio-track-group').hidden = !audio.length;
    document.getElementById('subtitle-track-group').hidden = !subtitles.length;
    function option(container, kind, track, text, selected) {
      var button = document.createElement('button');
      button.type = 'button'; button.textContent = text;
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
      button.myflixTrack = track; button.myflixTrackKind = kind;
      button.onclick = function () { selectTrack(kind, track); };
      container.appendChild(button);
      if (focused && active.myflixTrackKind === kind && active.myflixTrack === track) { restore = button; }
    }
    function name(track, index, prefix) {
      var text = track.label || track.language || prefix + ' ' + (index + 1);
      return track.label && track.language && track.label !== track.language ? text + ' (' + track.language + ')' : text;
    }
    audio.forEach(function (track, index) { option(audioOptions, 'audio', track, name(track, index, 'Audio'), Boolean(track.enabled)); });
    if (subtitles.length) {
      option(subtitleOptions, 'subtitles', null, 'Off', !subtitles.some(function (track) { return track.mode === 'showing'; }));
      subtitles.forEach(function (track, index) { option(subtitleOptions, 'subtitles', track, name(track, index, 'Subtitles'), track.mode === 'showing'); });
    }
    if (focused && active !== closeTrackButton) { (restore || trackControls()[0] || closeTrackButton).focus(); }
  }
  function bindTrackEvents() {
    trackLists.forEach(function (list) {
      if (list.removeEventListener) { ['addtrack', 'removetrack', 'change'].forEach(function (event) { list.removeEventListener(event, refreshTracks); }); }
    });
    trackLists = [player.audioTracks, player.textTracks].filter(function (list) { return list && list.addEventListener; });
    trackLists.forEach(function (list) { ['addtrack', 'removetrack', 'change'].forEach(function (event) { list.addEventListener(event, refreshTracks); }); });
    refreshTracks();
  }
  trackButton.onclick = function () {
    if (trackButton.hidden) { return; }
    trackPanel.hidden = false;
    trackButton.setAttribute('aria-expanded', 'true');
    trackMessage.textContent = '';
    refreshTracks(); showControls();
    (trackControls()[0] || closeTrackButton).focus();
  };
  closeTrackButton.onclick = function () { closeTracks(true); showControls(); };
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
    closeTracks(false);
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
  function buffering(active) {
    loading.hidden = !active;
    player.setAttribute('aria-busy', active ? 'true' : 'false');
  }
  function bufferedAt(position) {
    var ranges = player.buffered, i;
    for (i = 0; ranges && i < ranges.length; i += 1) {
      if (position >= ranges.start(i) && position < ranges.end(i)) { return true; }
    }
    return false;
  }
  function seekTo(position) {
    if (!canSeek() || !isFinite(position)) { return; }
    position = Math.max(0, Math.min(player.duration, position));
    // Assign the requested time even outside buffered/seekable ranges. The
    // native media loader requests the relevant bytes from /media using Range.
    player.currentTime = position;
    buffering(!bufferedAt(position));
    render();
  }
  function frameReady() {
    if (!player.seeking && player.readyState >= 2) { buffering(false); }
  }
  function seek(offset) {
    seekTo(player.currentTime + offset);
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
    if (resume > 0 && resume < player.duration - 5) { seekTo(resume); }
    render();
    play(); // A blocked autoplay leaves the central Play action available.
  }
  player.addEventListener('loadedmetadata', function () { initializeMedia(); bindTrackEvents(); });
  player.addEventListener('emptied', function () { buffering(false); subtitlesChosen = false; closeTracks(false); trackButton.hidden = true; });
  player.addEventListener('seeking', function () { buffering(player.readyState < 2 || !bufferedAt(player.currentTime)); });
  player.addEventListener('waiting', function () { if (!player.paused && !player.ended) { buffering(true); } });
  player.addEventListener('stalled', function () { if (!player.paused && player.readyState < 3) { buffering(true); } });
  ['seeked', 'canplay', 'loadeddata', 'playing'].forEach(function (event) { player.addEventListener(event, frameReady); });
  player.addEventListener('durationchange', function () { initializeMedia(); render(); });
  player.addEventListener('play', function () { document.body.classList.add('playing'); if (noticeKind !== 'save') { notify(''); } render(); showControls(); });
  player.addEventListener('pause', function () { if (!player.seeking) { buffering(false); } document.body.classList.remove('playing'); render(); showControls(); save(); });
  player.addEventListener('ended', function () {
    buffering(false);
    render(); showControls(); save(true);
    if (nextId && !autoAdvanceAttempted) { autoAdvanceAttempted = true; advanceEpisode(); }
  });
  player.addEventListener('timeupdate', function () {
    render();
    if (Date.now() - lastSave > 10000) { lastSave = Date.now(); save(); }
  });
  player.addEventListener('volumechange', render);
  player.addEventListener('error', function () { buffering(false); notify('This file could not play. Check the file and TV codec support. MP4 with H.264 video and AAC audio is recommended; MyFlix does not transcode.', 'playback'); });
  toggle.onclick = center.onclick = togglePlay;
  player.onclick = togglePlay;
  rewind.onclick = function () { seek(-10); };
  forward.onclick = function () { seek(10); };
  function advanceEpisode(event) {
    if (event) { event.preventDefault(); }
    if (!nextId || switchingEpisode) { return; }
    switchingEpisode = true;
    closeTracks(false);
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
      subtitlesChosen = false;
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
    seekTo(Number(timeline.value));
    showControls();
  };
  function seekFromMouse(event) {
    if (!canSeek() || !isFinite(event.clientX)) { return false; }
    var bounds = timeline.getBoundingClientRect();
    if (bounds.width <= 0) { return false; }
    var fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    seekTo(fraction * player.duration);
    showControls();
    return true;
  }
  if (!platform.isTV) {
    // Own the complete mouse gesture. Firefox's native range track can step
    // on mouse-down and consume the later click, undoing click-only seeking.
    timeline.addEventListener('mousedown', function (event) {
      if (event.button !== 0 || !seekFromMouse(event)) { return; }
      event.preventDefault();
      mouseScrubbing = true;
      beginScrub();
      timeline.focus();
    });
    document.addEventListener('mousemove', function (event) {
      if (mouseScrubbing) { event.preventDefault(); seekFromMouse(event); }
    });
  } else {
    timeline.addEventListener('mousedown', beginScrub);
  }
  function beginScrub() { scrubbing = true; showControls(); }
  function endScrub() { if (scrubbing) { scrubbing = false; render(); showControls(); save(); } }
  timeline.addEventListener('touchstart', beginScrub);
  document.addEventListener('mouseup', function (event) {
    if (mouseScrubbing) { seekFromMouse(event); mouseScrubbing = false; }
    endScrub();
  });
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
    if (!trackPanel.hidden) {
      var options = trackControls(), index = options.indexOf(active);
      if (code === 27 || code === 461 || code === 10009 || code === 8) { closeTracks(true); }
      else if (code === 13 || code === 32) { if (index !== -1) { active.click(); } }
      else if (code === 9 || (code >= 37 && code <= 40)) {
        var backwards = code === 37 || code === 38 || (code === 9 && event.shiftKey);
        options[(index + (backwards ? -1 : 1) + options.length) % options.length].focus();
      } else { options = null; }
      if (options) { event.preventDefault(); showControls(); return; }
    }
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
  bindTrackEvents();
  showControls();
  player.focus();
  // Metadata can already be cached before deferred JavaScript attaches listeners.
  initializeMedia();
}());
