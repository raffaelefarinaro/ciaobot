// The Mac button opens the latest release page. When the latest release carries
// the Ciaobot DMG, the button links to that file instead. Without JavaScript or
// network access, the button keeps the release page link.
(function () {
  var links = document.querySelectorAll('[data-mac-client]');
  if (!links.length || !window.fetch) return;
  fetch('https://api.github.com/repos/raffaelefarinaro/ciaobot/releases/latest')
    .then(function (response) {
      if (!response.ok) throw new Error(response.status);
      return response.json();
    })
    .then(function (release) {
      var asset = release.assets.find(function (item) {
        return /^Ciaobot-.*\.dmg$/.test(item.name);
      });
      if (!asset) return;
      links.forEach(function (link) { link.href = asset.browser_download_url; });
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
