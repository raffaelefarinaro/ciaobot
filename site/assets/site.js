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

// Live GitHub stars and app downloads. Unauthenticated API (60 requests/hour
// per visitor), so results are cached for ten minutes in sessionStorage.
(function () {
  var box = document.getElementById('repo-stats');
  if (!box || !window.fetch) return;
  var REPO = 'https://api.github.com/repos/raffaelefarinaro/ciaobot';
  var KEY = 'ciaobot-repo-stats';
  function show(d) {
    var fmt = new Intl.NumberFormat('en', { notation: d.downloads > 9999 ? 'compact' : 'standard' });
    box.querySelector('[data-stat="stars"]').textContent = fmt.format(d.stars);
    box.querySelector('[data-stat="downloads"]').textContent = fmt.format(d.downloads);
    box.hidden = false;
  }
  try {
    var c = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (c && Date.now() - c.at < 600000) return show(c);
  } catch (e) {}
  function releases(page, total) {
    return fetch(REPO + '/releases?per_page=100&page=' + page).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (list) {
      list.forEach(function (rel) {
        rel.assets.forEach(function (a) {
          // Count the app itself (installs and updates), not the installer
          // script, signatures or update manifests fetched alongside it.
          if (/\.app\.tar\.gz$|\.dmg$/.test(a.name)) total += a.download_count;
        });
      });
      return list.length === 100 && page < 5 ? releases(page + 1, total) : total;
    });
  }
  Promise.all([
    fetch(REPO).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
    releases(1, 0)
  ]).then(function (res) {
    var d = { stars: res[0].stargazers_count, downloads: res[1], at: Date.now() };
    try { sessionStorage.setItem(KEY, JSON.stringify(d)); } catch (e) {}
    show(d);
  }).catch(function () {});
})();
