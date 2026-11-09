// One install action per platform. macOS offers the native client and the engine
// command; Windows has its PowerShell command; Linux links to its admin guide.
(function () {
  document.querySelectorAll('[data-install]').forEach(function (box) {
    var select = box.querySelector('[data-install-os]');
    var code = box.querySelector('code');
    var label = box.querySelector('[data-install-label]');
    var note = box.querySelector('[data-install-note]');
    var commandBox = box.querySelector('[data-install-command]');
    var guide = box.querySelector('[data-install-guide]');
    var macClient = box.querySelector('[data-mac-client]');
    var macNote = box.querySelector('[data-mac-client-note]');
    if (!select || !code) return;

    function show() {
      var option = select.options[select.selectedIndex];
      var platform = option.value;
      var command = option.dataset.cmd || '';
      var guideURL = option.dataset.url || '';
      box.dataset.platform = platform;
      code.textContent = command;
      if (label) label.textContent = option.dataset.label;
      if (commandBox) commandBox.hidden = !command;
      if (note) note.hidden = platform !== 'windows';
      if (macClient) macClient.hidden = platform !== 'mac';
      if (macNote) macNote.hidden = platform !== 'mac';
      if (guide) {
        guide.hidden = !guideURL;
        if (guideURL) guide.href = guideURL;
      }
    }

    var ua = navigator.userAgentData;
    var platform = (ua && ua.platform) || navigator.platform || navigator.userAgent || '';
    if (/win/i.test(platform)) select.value = 'windows';
    else if (/linux/i.test(platform) && !/android/i.test(navigator.userAgent)) select.value = 'linux';
    select.addEventListener('change', show);
    select.closest('.install-os').hidden = false;
    show();
  });
})();

// Resolve the newest native Mac DMG, including prereleases. Without JavaScript
// or network access, the button still opens the client's releases page.
(function () {
  var links = document.querySelectorAll('[data-mac-client]');
  if (!links.length || !window.fetch) return;
  fetch('https://api.github.com/repos/raffaelefarinaro/ciaobot-mac/releases?per_page=20')
    .then(function (response) {
      if (!response.ok) throw new Error(response.status);
      return response.json();
    })
    .then(function (releases) {
      for (var i = 0; i < releases.length; i += 1) {
        if (releases[i].draft) continue;
        var asset = releases[i].assets.find(function (item) { return /\.dmg$/i.test(item.name); });
        if (!asset) continue;
        links.forEach(function (link) { link.href = asset.browser_download_url; });
        break;
      }
    })
    .catch(function () {});
})();

// Copy-to-clipboard for the install command and the example prompts.
(function () {
  function copy(text, done) {
    var ok = navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject();
    ok.then(done, function () {
      var t = document.createElement('textarea');
      t.value = text; document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); done(); } catch (e) {}
      t.remove();
    });
  }
  document.querySelectorAll('[data-copy]').forEach(function (b) {
    b.addEventListener('click', function () {
      copy(document.getElementById(b.dataset.copy).textContent, function () {
        b.textContent = 'Copied';
        setTimeout(function () { b.textContent = 'Copy'; }, 1600);
      });
    });
  });
  document.querySelectorAll('.prompt').forEach(function (p) {
    p.setAttribute('role', 'button');
    p.setAttribute('tabindex', '0');
    p.setAttribute('aria-label', 'Copy prompt: ' + p.textContent.trim());
    function go() {
      copy(p.textContent.trim(), function () {
        p.classList.add('copied');
        setTimeout(function () { p.classList.remove('copied'); }, 1400);
      });
    }
    p.addEventListener('click', go);
    p.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
})();

// Play the product demo automatically unless the visitor prefers reduced motion.
// Native controls keep pause, replay and fullscreen available without JavaScript.
(function () {
  var video = document.querySelector('[data-product-demo]');
  if (!video) return;
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function syncMotion() {
    video.autoplay = !motion.matches;
    if (motion.matches) video.pause();
    else video.play().catch(function () {}); // Autoplay may be blocked; controls remain available.
  }
  motion.addEventListener('change', syncMotion);
  syncMotion();
})();
