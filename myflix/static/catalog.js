/* ES5: refresh imported titles without navigating away or discarding edits. */
(function () {
  var root = document.getElementById('catalog-live');
  if (!root || !window.DOMParser) { return; }
  var busy = false, dirty = false;
  var controls = 'a,button,input,select,summary';
  function array(nodes) { return Array.prototype.slice.call(nodes); }
  function signature(ids) { return ids.slice().sort(function (a, b) { return a - b; }).join(','); }
  var known = signature(JSON.parse(root.getAttribute('data-catalog-ids')));
  function editing() {
    return dirty || (root.contains(document.activeElement) && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) ||
      Boolean(root.querySelector('[aria-disabled="true"]'));
  }
  root.addEventListener('input', function () { dirty = true; });
  root.addEventListener('change', function () { dirty = true; });
  function get(url, done) {
    var request = new XMLHttpRequest();
    request.open('GET', url, true);
    request.timeout = 15000;
    request.onload = function () { done(request.status === 200 ? request.responseText : null); };
    request.onerror = request.ontimeout = function () { done(null); };
    request.send(null);
  }
  function key(element) { return element.getAttribute('data-catalog-key'); }
  function apply(incoming) {
    var focused = document.activeElement;
    var inside = root.contains(focused);
    var owner = inside ? focused : root;
    while (owner && owner !== root && !key(owner)) { owner = owner.parentNode; }
    var ownerKey = owner && owner !== root ? key(owner) : null;
    var focusIndex = inside ? array((owner || root).querySelectorAll(controls)).indexOf(focused) : -1;
    var mediaId = inside && focused.getAttribute('data-media-id');
    var focusId = inside && focused.id;
    var opened = array(root.querySelectorAll('details[open]')).map(key);
    var rails = {};
    array(root.querySelectorAll('[data-rail]')).forEach(function (rail) { rails[rail.getAttribute('aria-label')] = rail.scrollLeft; });
    var left = window.pageXOffset || 0, top = window.pageYOffset || 0;
    root.innerHTML = incoming.innerHTML;
    known = signature(JSON.parse(incoming.getAttribute('data-catalog-ids')));
    root.setAttribute('data-catalog-ids', incoming.getAttribute('data-catalog-ids'));
    var replacementOwner = root;
    array(root.querySelectorAll('[data-catalog-key]')).forEach(function (element) {
      if (opened.indexOf(key(element)) !== -1) { element.open = true; }
      if (key(element) === ownerKey) { replacementOwner = element; }
    });
    if (window.MyFlixLibrary) { window.MyFlixLibrary.refresh(); }
    if (window.MyFlixTV && window.MyFlixTV.refreshShelves) { window.MyFlixTV.refreshShelves(); }
    if (inside) {
      var target = focusId ? document.getElementById(focusId) : null;
      if (mediaId) {
        array(root.querySelectorAll('[data-media-id]')).forEach(function (card) {
          if (card.getAttribute('data-media-id') === mediaId) { target = card; }
        });
      }
      target = target || replacementOwner.querySelectorAll(controls)[focusIndex] || root.querySelector(controls);
      if (target) { target.focus(); }
    }
    array(root.querySelectorAll('[data-rail]')).forEach(function (rail) {
      var position = rails[rail.getAttribute('aria-label')];
      if (position !== undefined) { rail.scrollLeft = position; }
    });
    window.scrollTo(left, top);
  }
  function refresh() {
    if (busy || document.hidden || editing()) { return; }
    busy = true;
    get(root.getAttribute('data-catalog-url'), function (text) {
      var data;
      try { data = text && JSON.parse(text); } catch (error) { busy = false; return; }
      if (!data || !Array.isArray(data.ids) || signature(data.ids) === known) { busy = false; return; }
      get(window.location.href, function (html) {
        busy = false;
        if (!html || document.hidden || editing()) { return; }
        var incoming;
        try { incoming = new window.DOMParser().parseFromString(html, 'text/html').getElementById('catalog-live'); }
        catch (error) { return; }
        if (incoming) { apply(incoming); }
      });
    });
  }
  window.setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', refresh);
  window.addEventListener('pageshow', refresh);
}());
