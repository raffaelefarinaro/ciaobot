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


// Colour theme: follows the OS until the visitor picks one. The choice is a
// per-browser convenience; the page renders correctly without storage.
(function () {
  var root = document.documentElement;
  function current() {
    return root.dataset.theme || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  }
  document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
    function label() {
      button.setAttribute('aria-label', current() === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
    }
    label();
    // Without a stored choice the theme follows the OS, so the label must too.
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', label);
    button.addEventListener('click', function () {
      root.dataset.theme = current() === 'light' ? 'dark' : 'light';
      try { localStorage.setItem('ciao-site-theme', root.dataset.theme); } catch (e) {}
      label();
    });
  });
})();

// The home page mark: as the visitor scrolls, the C spins and its two dots
// glide onto its open ends and spread like clay until the ring is closed.
// Reduced motion keeps the static mark. Geometry matches web/src/components/CiaoMark.vue.
(function () {
  var stage = document.querySelector('[data-ring-scroll]');
  if (!stage) return;
  var spin = stage.querySelector('[data-ring-spin]');
  var segA = stage.querySelector('[data-ring-a]');
  var segB = stage.querySelector('[data-ring-b]');
  var dotA = stage.querySelector('[data-dot-a]');
  var dotB = stage.querySelector('[data-dot-b]');
  var CX = 630, CY = 616, R = 295, A0 = -72.48, A1 = 62.0, MEET = (A0 + A1) / 2;
  var DA = { a: -48.4, d: 361, r: 91 }, DB = { a: 38.3, d: 297, r: 56 };
  function rad(d) { return d * Math.PI / 180; }
  function pt(a) { return [CX + R * Math.cos(rad(a)), CY + R * Math.sin(rad(a))]; }
  function arc(a, b) {
    var p = pt(a), q = pt(b);
    return 'M' + p[0] + ' ' + p[1] + 'A' + R + ' ' + R + ' 0 0 ' + (b > a ? 1 : 0) + ' ' + q[0] + ' ' + q[1];
  }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function clamp(t) { return Math.max(0, Math.min(1, t)); }
  function smooth(t) { t = clamp(t); return t * t * (3 - 2 * t); }
  function inOut(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  var LAND = 0.35; // share of the scroll spent landing the dots on the C's ends
  function draw(p) {
    // 1. Each dot glides onto its end of the C and settles to the stroke's width.
    var land = smooth(p / LAND);
    // 2. Then it pushes back out along the opening, spreading into the ring
    //    until the two meet. The growing arc starts exactly where the dot sits,
    //    so nothing pops in.
    var grow = inOut(clamp((p - LAND) / (1 - LAND)));
    var tipA = A0 + (MEET - A0) * grow, tipB = A1 + (MEET - A1) * grow;
    segA.setAttribute('d', grow > 0 ? arc(A0, tipA) : '');
    segB.setAttribute('d', grow > 0 ? arc(tipB, A1) : '');
    [[DA, dotA, A0, tipA], [DB, dotB, A1, tipB]].forEach(function (d) {
      var dot = d[0], el = d[1];
      var ang = grow > 0 ? d[3] : lerp(dot.a, d[2], land);
      var dist = grow > 0 ? R : lerp(dot.d, R, land);
      el.setAttribute('cx', CX + dist * Math.cos(rad(ang)));
      el.setAttribute('cy', CY + dist * Math.sin(rad(ang)));
      el.setAttribute('r', lerp(dot.r, 55, land));
    });
    spin.setAttribute('transform', 'rotate(' + (360 * inOut(p)) + ' ' + CX + ' ' + CY + ')');
  }
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var shown = 0, target = 0, frame = 0;
  function measure() {
    var box = stage.getBoundingClientRect();
    var span = stage.offsetHeight - window.innerHeight;
    if (span < 200) span = stage.offsetHeight;
    return clamp(-box.top / span);
  }
  function tick() {
    // Follow the scroll with a little inertia so wheel steps read as one motion.
    shown += (target - shown) * 0.14;
    if (Math.abs(target - shown) < 0.0005) shown = target;
    draw(shown);
    frame = shown === target ? 0 : requestAnimationFrame(tick);
  }
  function update() {
    if (reduce.matches) { cancelAnimationFrame(frame); frame = 0; shown = target = 0; draw(0); return; }
    target = measure();
    if (!frame) frame = requestAnimationFrame(tick);
  }
  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  reduce.addEventListener('change', update);
  update();
})();

// Home lead: the agent name cycles Claude Code → Codex → OpenCode. Screen
// readers get the full list once; reduced motion shows that list as text.
(function () {
  var box = document.querySelector('[data-rotator]');
  var text = document.querySelector('[data-rotator-text]');
  if (!box || !text) return;
  var words = Array.prototype.slice.call(box.children);
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (reduce.matches) { box.hidden = true; text.classList.remove('visually-hidden'); return; }
  var i = 0;
  function fit(el) { box.style.width = el.getBoundingClientRect().width + 'px'; }
  fit(words[0]);
  setInterval(function () {
    if (document.hidden) return;
    var out = words[i];
    i = (i + 1) % words.length;
    var next = words[i];
    words.forEach(function (w) { if (w !== out && w !== next) w.classList.remove('is-on', 'is-off'); });
    out.classList.remove('is-on'); out.classList.add('is-off');
    next.classList.remove('is-off'); next.classList.add('is-on');
    fit(next);
    // reset the outgoing word below, without animating, once it has left
    setTimeout(function () {
      out.style.transition = 'none'; out.classList.remove('is-off');
      void out.offsetWidth; out.style.transition = '';
    }, 560);
  }, 2400);
  window.addEventListener('resize', function () { fit(words[i]); });
})();

// Phone menu: the toggle opens the nav under the header; Escape or a link closes it.
(function () {
  var button = document.querySelector('[data-menu-toggle]');
  var header = document.querySelector('.site-header');
  if (!button || !header) return;
  function set(open) {
    header.classList.toggle('is-open', open);
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  }
  button.addEventListener('click', function () { set(!header.classList.contains('is-open')); });
  header.querySelectorAll('.site-nav a').forEach(function (a) { a.addEventListener('click', function () { set(false); }); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && header.classList.contains('is-open')) { set(false); button.focus(); }
  });
})();

// GitHub stars and total release downloads, read from the public API. Hidden
// until both numbers load, so a rate limit or offline visit shows nothing.
(function () {
  var box = document.querySelector('[data-gh-stats]');
  if (!box || !window.fetch) return;
  var api = 'https://api.github.com/repos/raffaelefarinaro/ciaobot';
  function short(n) {
    return n >= 10000 ? Math.round(n / 1000) + 'k' : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n);
  }
  function json(url) {
    return fetch(url).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  Promise.all([json(api), json(api + '/releases?per_page=100')])
    .then(function (data) {
      var downloads = 0;
      data[1].forEach(function (release) {
        (release.assets || []).forEach(function (asset) {
          // The engine's update check fetches the manifest and its signature; those are not downloads.
          if (!/\.(json|sig)$/.test(asset.name)) downloads += asset.download_count || 0;
        });
      });
      box.querySelector('[data-gh-stars]').textContent = short(data[0].stargazers_count);
      box.querySelector('[data-gh-downloads]').textContent = short(downloads);
      box.hidden = false;
    })
    .catch(function () {});
})();
