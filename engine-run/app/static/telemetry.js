// telemetry.js — lightweight, whole-app UI interaction logging.
//
// Captures (via event delegation, so EVERY button/link/scroll is covered without
// touching individual elements):
//   • click       — what was clicked (tag/id/class/text/href/aria/data-album), and
//                    a `dead:true` flag when the click landed on something NOT
//                    interactive (a strong "user expected this to do something" signal)
//   • key         — Enter/Space activation of custom widgets (role=button, cards…)
//   • submit      — form submissions (which action)
//   • scroll      — throttled scroll depth + per-page max depth reached
//   • pageview    — path, referrer, viewport
//   • dwell       — seconds spent on the page (sent on hide/unload)
//
// Events are batched and POSTed to /api/telemetry (sendBeacon on unload). This is a
// self-hosted, single-user app: it's usage data to improve the UI. Fails silently and
// never interferes with the page.
(function () {
  if (window.__plexiTelemetry) return;
  window.__plexiTelemetry = true;

  var SESSION = (function () {
    try {
      var s = sessionStorage.getItem('plexi_sess');
      if (!s) { s = Date.now().toString(36) + Math.random().toString(36).slice(2, 8); sessionStorage.setItem('plexi_sess', s); }
      return s;
    } catch (e) { return 'nosess'; }
  })();
  var PAGE = location.pathname;
  var T0 = Date.now();
  var queue = [];
  var maxScroll = 0;

  function push(ev) {
    ev.type = ev.type || 'event';
    ev.t = Date.now();
    ev.p = PAGE;
    ev.s = SESSION;
    queue.push(ev);
    if (queue.length >= 25) flush(false);
  }

  function flush(useBeacon) {
    if (!queue.length) return;
    var batch = queue; queue = [];
    var body = JSON.stringify({ events: batch });
    try {
      if (useBeacon && navigator.sendBeacon) {
        navigator.sendBeacon('/api/telemetry', new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/api/telemetry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true }).catch(function () {});
      }
    } catch (e) {}
  }

  var INTERACTIVE = 'button, a, input, select, textarea, label, summary, [role="button"], [role="link"], [tabindex], [data-album-id], .lib-card, .feed-row, .live-strip, .toggle-row, .lib-filter, .cover-opt, .dashctl-btn, .srch-pill, .feed-more, .lib-complete-btn';

  function describe(el) {
    if (!el || !el.closest) return {};
    var interactive = el.closest(INTERACTIVE);
    var t = interactive || el;
    var text = '';
    try { text = (t.getAttribute('aria-label') || (t.textContent || '').replace(/\s+/g, ' ').trim()); } catch (e) {}
    var cls = (t.className && typeof t.className === 'string') ? t.className.slice(0, 90) : undefined;
    var d = {
      tag: (t.tagName || '').toLowerCase() || undefined,
      id: t.id || undefined,
      cls: cls,
      txt: text ? text.slice(0, 70) : undefined,
      href: (t.getAttribute && t.getAttribute('href')) || undefined,
      name: (t.getAttribute && t.getAttribute('name')) || undefined,
      role: (t.getAttribute && t.getAttribute('role')) || undefined,
      album: (t.getAttribute && t.getAttribute('data-album-id')) || undefined
    };
    if (!interactive) d.dead = true;  // clicked a non-interactive thing
    return d;
  }

  document.addEventListener('click', function (e) {
    push(Object.assign({ type: 'click' }, describe(e.target)));
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var d = describe(e.target);
    if (d.tag && ['input', 'textarea', 'select', 'button', 'a'].indexOf(d.tag) === -1 && (d.role || d.album)) {
      push(Object.assign({ type: 'key', key: e.key === ' ' ? 'Space' : 'Enter' }, d));
    }
  }, true);

  document.addEventListener('submit', function (e) {
    var f = e.target || {};
    push({ type: 'submit', id: f.id || undefined, action: (f.getAttribute && f.getAttribute('action')) || undefined });
  }, true);

  var scrollTimer = null;
  window.addEventListener('scroll', function () {
    var doc = document.documentElement;
    var total = (doc.scrollHeight - doc.clientHeight) || 1;
    var depth = Math.min(100, Math.max(0, Math.round((doc.scrollTop / total) * 100)));
    if (depth > maxScroll) maxScroll = depth;
    if (scrollTimer) return;
    scrollTimer = setTimeout(function () {
      scrollTimer = null;
      push({ type: 'scroll', depth: depth, max: maxScroll });
    }, 600);
  }, { passive: true });

  push({ type: 'pageview', ref: document.referrer || undefined, vw: window.innerWidth, vh: window.innerHeight });

  setInterval(function () { flush(false); }, 10000);
  document.addEventListener('visibilitychange', function () { if (document.hidden) { push({ type: 'dwell', secs: Math.round((Date.now() - T0) / 1000), max_scroll: maxScroll }); flush(true); } });
  window.addEventListener('pagehide', function () { push({ type: 'dwell', secs: Math.round((Date.now() - T0) / 1000), max_scroll: maxScroll, final: true }); flush(true); });
})();
