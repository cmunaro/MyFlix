/* ES5: one background trailer, driven by selection without taking remote input. */
(function () {
  var panel = document.getElementById('discovery-preview');
  if (!panel) { return; }
  var picture = document.getElementById('preview-image');
  var title = document.getElementById('preview-title');
  var host = document.getElementById('preview-video');
  var status = document.getElementById('preview-status');
  var selected = null, timer = null, idleTimer = null, loadTimer = null;
  var player = null, frame = document.getElementById('preview-frame'), loading = false, generation = 0, ready = false, playerReady = false, disposed = false;
  var requestedId = '', seedId = '';
  var playing = false, mutedFallback = false, soundPending = false;
  var idleElapsed = false;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced && frame) { host.removeChild(frame); frame = null; }

  function message(text) {
    status.textContent = text;
    status.hidden = !text;
  }
  function hideUI() {
    if (idleElapsed && playing && selected && trailerId(selected)) { document.body.classList.add('trailer-idle'); }
  }
  function reveal() {
    document.body.classList.remove('trailer-idle');
  }
  function wake() {
    window.clearTimeout(idleTimer);
    idleElapsed = false;
    reveal();
    idleTimer = window.setTimeout(function () { idleElapsed = true; hideUI(); }, 3000);
  }
  function stopPlayback() {
    requestedId = '';
    playing = false;
    mutedFallback = false;
    soundPending = false;
    reveal();
    panel.classList.remove('trailer-playing');
    document.body.classList.remove('trailer-playing');
    if (playerReady && player) { player.stopVideo(); }
  }
  function destroyPlayer() {
    stopPlayback();
    generation += 1;
    playerReady = false;
    window.clearTimeout(loadTimer);
    loadTimer = null;
    if (player) { player.destroy(); player = null; }
    while (host.firstChild) { host.removeChild(host.firstChild); }
    frame = null;
  }
  function stop() {
    window.clearTimeout(timer);
    window.clearTimeout(idleTimer);
    idleElapsed = false;
    selected = null;
    ready = false;
    stopPlayback();
    message('');
    panel.hidden = true;
    document.body.classList.remove('discovery-active');
  }
  function failed(text) {
    stopPlayback();
    message(text);
  }
  function loadedId() {
    var match = /[?&]v=([A-Za-z0-9_-]{11})(?:[&#]|$)/.exec(player.getVideoUrl());
    return match ? match[1] : '';
  }
  function trailerId(card) {
    var id = card && card.getAttribute('data-trailer');
    return /^[A-Za-z0-9_-]{11}$/.test(id || '') ? id : '';
  }
  function prepare() {
    var id = trailerId(selected) || seedId;
    if (!id || document.hidden || reduced || disposed) { return; }
    var token = generation;
    if (!frame) {
      // Load the embed and API in parallel; the embed must not wait for the SDK.
      // Set permissions and referrer before navigation, rather than after onReady.
      frame = document.createElement('iframe');
      frame.setAttribute('tabindex', '-1');
      frame.setAttribute('aria-hidden', 'true');
      frame.setAttribute('title', 'Trailer preview');
      frame.setAttribute('allow', 'autoplay; encrypted-media');
      frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
      frame.src = 'https://www.youtube-nocookie.com/embed/' + id +
        '?enablejsapi=1&autoplay=0&controls=0&disablekb=1&playsinline=1&rel=0&origin=' + encodeURIComponent(window.location.origin);
      host.appendChild(frame);
    }
    if (!loadTimer && !playerReady) {
      loadTimer = window.setTimeout(function () {
        if (token === generation) {
          destroyPlayer();
          if (selected) { message('Trailer could not load. Open the title to watch it on YouTube.'); }
        }
      }, 15000);
    }
    if (!window.YT || !window.YT.Player) {
      if (!loading) {
        loading = true;
        var script = document.createElement('script');
        script.src = 'https://www.youtube.com/iframe_api';
        script.onerror = function () { loading = false; if (selected) { message('Trailer service unavailable.'); } };
        window.onYouTubeIframeAPIReady = function () { prepare(); play(); };
        document.head.appendChild(script);
      }
      return;
    }
    if (player) { return; }
    function current(event) {
      if (token !== generation || !selected || !ready || !requestedId) { return false; }
      var id = loadedId();
      return !id || id === requestedId;
    }
    player = new window.YT.Player(frame, {
      host: 'https://www.youtube-nocookie.com',
      events: {
        onReady: function (event) {
          if (token !== generation) { return; }
          window.clearTimeout(loadTimer);
          loadTimer = null;
          playerReady = true;
          play();
        },
        onStateChange: function (event) {
          if (!current(event)) { return; }
          if (event.data === 1) {
            var wasPlaying = playing;
            playing = true;
            panel.classList.add('trailer-playing');
            document.body.classList.add('trailer-playing');
            if (!wasPlaying) { hideUI(); }
          } else if (event.data === 0) {
            stopPlayback();
            message('');
          } else {
            playing = false;
            reveal();
          }
        },
        onError: function (event) {
          if (current(event)) { failed('This trailer is unavailable. Open the title to try it on YouTube.'); }
        },
        onAutoplayBlocked: function (event) {
          if (!current(event)) { return; }
          playing = false;
          reveal();
          soundPending = true;
          if (!mutedFallback) {
            mutedFallback = true;
            message('Browser muted the preview. Press a key or click for sound.');
            player.mute();
            player.playVideo();
          } else {
            message('Autoplay blocked. Press a key or click to start the trailer.');
          }
        }
      }
    });
  }
  function play() {
    if (!selected || !ready || document.hidden || reduced) { return; }
    var id = trailerId(selected);
    if (!id) { message('No trailer available for this title.'); return; }
    prepare();
    if (!playerReady || requestedId === id) { return; }
    requestedId = id;
    player.unMute();
    // Reuse the initialized iframe: changing a title must not reload YouTube's UI.
    if (loadedId() === id) { player.playVideo(); }
    else { player.loadVideoById(id); }
  }
  function select(card) {
    if (selected === card) { if (card) { wake(); } return; }
    stop();
    if (!card || document.hidden) { return; }
    selected = card;
    var src = card.getAttribute('data-preview-image');
    picture.hidden = !src;
    if (src) { picture.src = src; } else { picture.removeAttribute('src'); }
    title.textContent = card.getAttribute('data-preview-title');
    panel.hidden = false;
    document.body.classList.add('discovery-active');
    wake();
    if (reduced) { message('Automatic trailers disabled by reduced-motion preference.'); }
    prepare();
    timer = window.setTimeout(function () { ready = true; play(); }, 250);
  }
  function cardFor(element) {
    while (element && element !== document.body) {
      if (element.hasAttribute && element.hasAttribute('data-discovery')) { return element; }
      element = element.parentNode;
    }
    return null;
  }
  function interact() {
    wake();
    // Observe gestures without consuming keys owned by TV navigation.
    if (playerReady && soundPending && player && selected) {
      soundPending = false;
      mutedFallback = false;
      message('');
      player.unMute();
      player.playVideo();
    }
  }
  document.addEventListener('keydown', interact, true);
  document.addEventListener('mousedown', interact, true);
  document.addEventListener('touchstart', interact, true);
  document.addEventListener('mousemove', wake);
  document.addEventListener('wheel', wake);
  document.addEventListener('focusin', function (event) { select(cardFor(event.target)); });
  document.addEventListener('focusout', function (event) {
    if (!cardFor(event.relatedTarget)) { stop(); }
  });
  // tv.js moves pointer selection into DOM focus. Previews follow that one owner.
  document.addEventListener('visibilitychange', function () { if (document.hidden) { stop(); } });
  window.addEventListener('pagehide', function () { disposed = true; stop(); destroyPlayer(); });
  window.addEventListener('pageshow', function () { disposed = false; prepare(); select(cardFor(document.activeElement)); });
  var cards = document.querySelectorAll('[data-trailer]');
  for (var i = 0; i < cards.length && !seedId; i += 1) { seedId = trailerId(cards[i]); }
  // Warm one paused player while browsing, rather than paying startup per title.
  prepare();
  select(cardFor(document.activeElement));
}());
