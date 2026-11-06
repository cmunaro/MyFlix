/* ES5: update episode history without toggling its details or losing focus. */
(function () {
  var status = document.getElementById('library-watch-status');
  if (!status) { return; }
  var buttons = document.querySelectorAll('[data-watch-url]');
  for (var i = 0; i < buttons.length; i += 1) { bind(buttons[i]); }
  function bind(button) {
    var busy = false;
    button.onclick = function (event) {
      if (event) { event.preventDefault(); event.stopPropagation(); }
      if (busy) { return; }
      var watched = button.getAttribute('data-watched') !== '1';
      var name = button.getAttribute('data-episode-name');
      var request = new XMLHttpRequest();
      busy = true;
      button.setAttribute('aria-disabled', 'true');
      status.textContent = 'Saving ' + name + '…';
      function finished(error) {
        busy = false;
        button.removeAttribute('aria-disabled');
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
