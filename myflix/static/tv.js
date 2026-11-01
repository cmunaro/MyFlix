/* ES5: explicit shelf navigation, including cards outside the visible viewport. */
(function () {
  var fullscreenPending = false;
  var userAgent = typeof navigator === 'undefined' ? '' : navigator.userAgent || '';
  var adminPage = /tv-admin/.test(document.body.className || '');
  var editing = null;
  var tvPage = document.getElementById('player') || /tv-browse/.test(document.body.className || '') || adminPage;
  var isTV = Boolean(tvPage && /VIDAA|Hisense|HiSmartTV|SmartTV|Smart-TV|HbbTV|Vewd/i.test(userAgent));
  function fullscreenElement() { return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function enterFullscreen(target, onError) {
    if (fullscreenElement() || fullscreenPending) { return true; }
    var fn = target && (target.requestFullscreen || target.webkitRequestFullscreen || target.webkitRequestFullScreen);
    if (!fn) { return false; }
    fullscreenPending = true;
    function failed() { fullscreenPending = false; if (onError) { onError(); } }
    try {
      var result = fn === target.requestFullscreen ? fn.call(target, {navigationUI: 'hide'}) : fn.call(target);
      if (result && result.then) { result.then(function () { fullscreenPending = false; }, failed); }
      else { fullscreenPending = false; }
      return true;
    } catch (error) { failed(); return true; }
  }
  function exitFullscreen() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen || document.webkitCancelFullScreen;
    if (!fn) { return false; }
    var result = fn.call(document);
    if (result && result.catch) { result.catch(function () {}); }
    return true;
  }
  window.MyFlixTV = {isTV: isTV, fullscreenElement: fullscreenElement, enterFullscreen: enterFullscreen, exitFullscreen: exitFullscreen};
  if (isTV) {
    document.body.classList.add('tv-remote');
    function enterFromInteraction(event) {
      // Fullscreen must be requested during a real gesture, not a play/metadata
      // callback. Leave Back and the explicit fullscreen toggle under user control.
      var target = event.target || document.activeElement;
      while (target) { if (target.id === 'fullscreen') { return; } target = target.parentNode; }
      if (event.type === 'keydown' && [13, 32, 37, 38, 39, 40, 179, 415].indexOf(event.keyCode) === -1) { return; }
      enterFullscreen(document.getElementById('player-shell') || document.documentElement);
    }
    document.addEventListener('keydown', enterFromInteraction, true);
    document.addEventListener('click', enterFromInteraction, true);
  }
  function array(nodes) { return Array.prototype.slice.call(nodes); }
  function controls() {
    return array(document.querySelectorAll('a,button,input,select,summary,video')).filter(function (el) {
      return !el.disabled && el.offsetWidth && el.offsetHeight;
    });
  }
  function focus(el) {
    if (!el) { return; }
    el.focus();
    if (el.hasAttribute('data-card')) {
      var rail = el.parentNode;
      rail.scrollLeft = Math.max(0, el.offsetLeft - rail.offsetLeft - rail.clientWidth * 0.04);
      var section = rail.parentNode;
      window.scrollTo(0, window.pageYOffset + section.getBoundingClientRect().top - 90);
    } else if (el.closest && el.closest('.tv-header')) {
      window.scrollTo(0, 0);
    } else if (adminPage && el.scrollIntoView) {
      try { el.scrollIntoView({block: 'nearest', inline: 'nearest'}); }
      catch (e) { el.scrollIntoView(false); }
    }
  }
  function remember(el) {
    if (!el || !el.hasAttribute('data-card')) { return; }
    var rails = array(document.querySelectorAll('[data-rail]'));
    var rail = el.parentNode;
    rail.setAttribute('data-selected', array(rail.querySelectorAll('[data-card]')).indexOf(el));
    try { sessionStorage.setItem('myflix-focus:' + location.pathname + location.search, JSON.stringify({rail: rails.indexOf(rail), id: el.getAttribute('data-media-id')})); } catch (e) { /* Storage can be disabled on TVs. */ }
  }
  document.addEventListener('focusin', function (event) {
    if (editing && editing !== event.target) { editing.removeAttribute('data-editing'); editing = null; }
    remember(event.target);
  });
  document.addEventListener('keydown', function (event) {
    var code = event.keyCode, active = document.activeElement;
    // The player owns its controls and remote keys, including fullscreen.
    if (document.getElementById('player')) { return; }
    var back = code === 461 || code === 10009 || code === 8 || code === 27;
    if (adminPage && isTV && active && /INPUT|TEXTAREA|SELECT/.test(active.tagName)) {
      if (code === 13) {
        if (active.type === 'checkbox') { active.click(); }
        else if (editing === active) { editing.removeAttribute('data-editing'); editing = null; }
        else { editing = active; active.setAttribute('data-editing', 'true'); active.click(); }
        if (active.tagName !== 'SELECT') { event.preventDefault(); }
        return;
      }
      if (editing === active) {
        if (back) { editing.removeAttribute('data-editing'); editing = null; event.preventDefault(); }
        return;
      }
      if (!back && (code < 37 || code > 40)) { return; }
    } else if (active && /INPUT|TEXTAREA|SELECT/.test(active.tagName)) { return; }
    if (back) {
      if (adminPage) {
        var details = active && active.closest ? active.closest('details[open]') : null;
        if (details) { details.open = false; focus(details.querySelector('summary')); event.preventDefault(); return; }
        var current = document.querySelector('.admin-sidebar [aria-current]');
        if (current && active !== current) { focus(current); event.preventDefault(); return; }
      }
      if (fullscreenElement()) { exitFullscreen(); }
      else if (location.pathname !== '/' || location.search) { location.href = '/'; }
      event.preventDefault(); return;
    }
    if (code < 37 || code > 40) { return; }
    var rails = array(document.querySelectorAll('[data-rail]'));
    if (active && active.hasAttribute('data-card')) {
      var rail = active.parentNode;
      var cards = array(rail.querySelectorAll('[data-card]'));
      var index = cards.indexOf(active);
      if (code === 37 || code === 39) {
        focus(cards[index + (code === 37 ? -1 : 1)]);
      } else {
        var nextRail = rails[rails.indexOf(rail) + (code === 38 ? -1 : 1)];
        if (nextRail) {
          var nextCards = nextRail.querySelectorAll('[data-card]');
          var saved = nextRail.getAttribute('data-selected');
          focus(nextCards[Math.min(saved === null ? index : Number(saved), nextCards.length - 1)]);
        } else if (code === 38) {
          focus(document.querySelector('[data-initial-focus]') || document.querySelector('.tv-header a'));
          window.scrollTo(0, 0);
        }
      }
      event.preventDefault(); return;
    }
    if (code === 40 && active && active.closest && active.closest('[data-focus-row]') && rails.length) {
      focus(rails[0].querySelector('[data-card]')); event.preventDefault(); return;
    }
    var items = controls();
    if (!items.length) { return; }
    if (items.indexOf(active) === -1) { focus(items[0]); event.preventDefault(); return; }
    var rect = active.getBoundingClientRect();
    var x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    var best = null, score = Infinity;
    items.forEach(function (el) {
      if (el === active) { return; }
      if (adminPage && (code === 38 || code === 40) && active.closest && el.closest &&
          Boolean(active.closest('.admin-sidebar')) !== Boolean(el.closest('.admin-sidebar'))) { return; }
      var r = el.getBoundingClientRect();
      var dx = r.left + r.width / 2 - x, dy = r.top + r.height / 2 - y;
      var forward = code === 37 ? -dx : code === 39 ? dx : code === 38 ? -dy : dy;
      var sideways = code === 37 || code === 39 ? Math.abs(dy) : Math.abs(dx);
      if (forward > 5 && forward + sideways * 3 < score) { best = el; score = forward + sideways * 3; }
    });
    focus(best); event.preventDefault();
  });
  if (!document.getElementById('player') && (document.body.className.indexOf('tv-browse') !== -1 || adminPage)) {
    var target = document.querySelector('[autofocus]') || document.querySelector('[data-initial-focus]');
    if (!document.querySelector('[autofocus]')) {
      try {
        var saved = JSON.parse(sessionStorage.getItem('myflix-focus:' + location.pathname + location.search));
        if (saved) {
          var rail = document.querySelectorAll('[data-rail]')[saved.rail];
          if (rail) { var cards = rail.querySelectorAll('[data-card]'); for (var i = 0; i < cards.length; i += 1) { if (cards[i].getAttribute('data-media-id') === saved.id) { target = cards[i]; break; } } }
        }
      } catch (e) { /* Use the featured Play action when state cannot be restored. */ }
    }
    focus(target);
  }
}());
