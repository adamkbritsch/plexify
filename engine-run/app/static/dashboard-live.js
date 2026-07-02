// dashboard-live.js — polls /api/dashboard/live every 2s and renders the "Right now"
// card with a KEYED diff (one .live-strip per row_id) so albums can animate in/out:
//   • success → green flash, slides down/right + fades (and tells the feed to pull it in)
//   • fail    → turns red, slides left + fades
// Rows only ever leave with a definite green/red outcome — never a plain fade.
(function () {
  const root = document.getElementById('live-content');
  if (!root) return;
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }
  const cssEsc = (window.CSS && CSS.escape) ? CSS.escape : (s => String(s).replace(/[^\w-]/g, '\\$&'));

  let timer = null;
  let prevRows = {};            // row_id -> last data (to detect departures)
  const animating = new Set();  // row_ids mid-departure-animation
  const pendingDepart = {};     // row_id -> ts: left the card, awaiting a confirmed outcome

  function fmtSecs(s) {
    if (s == null) return '—';
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60), r = s % 60;
    return r === 0 ? (m + 'm') : (m + 'm ' + r + 's');
  }
  function fmtIdle(data) {
    const qd = data.queue_depth || 0;
    const nx = data.next_picker_tick_in_seconds;
    let parts = ['Queue idle'];
    if (nx !== null && nx !== undefined) parts.push('next picker tick in ' + nx + 's');
    if (qd > 0) parts.push('<a href="/library-autofill" style="color: var(--plex)">' + (Number(qd) || 0) + ' albums waiting</a>');
    else parts.push('all caught up');
    return '<div class="live-idle">' + parts.join(' · ') + '</div>';
  }
  function pct(done, total) {
    if (!total || total <= 0) return 0;
    return Math.max(0, Math.min(100, (done / total) * 100));
  }
  function qualityChip(q) {
    if (q === 'HI_RES_LOSSLESS') return '<span class="live-qchip live-q-hires">Hi-Res</span>';
    if (q === 'LOSSLESS') return '<span class="live-qchip live-q-cd">CD</span>';
    return '';
  }
  function sourceChip(src) {
    if (src === 'soulseek') return '<span class="live-source live-source-soulseek">Soulseek</span>';
    if (src === 'squid') return '<span class="live-source live-source-squid">squid.wtf</span>';
    if (src === 'spotiflac') return '<span class="live-source live-source-spotiflac">SpotiFLAC</span>';
    if (src === 'telegram') return '<span class="live-source live-source-telegram">Telegram</span>';
    return '';
  }

  // Inner HTML of a strip (no wrapper) — re-rendered in place each poll; the progress
  // bar has a CSS width transition so it slides smoothly rather than jumping.
  function stripInner(d) {
    const titleParts = [esc(d.artist || ''), esc(d.album || '')].filter(Boolean).join(' — ');
    const initials = esc((d.artist || '?').slice(0, 4));
    const done = d.tracks_done || 0, total = d.tracks_total || 0, p = pct(done, total);
    const qChip = qualityChip(d.quality_acquired || d.quality_target);
    const sChip = sourceChip(d.source);
    const upChip = d.upgrading ? '<span class="live-q-upgrading" title="Re-acquiring this album at higher quality (upgrading to hi-res)"><svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-arrow-up"/></svg> Upgrading</span>' : '';
    const songs = d.song_names || [];
    const songLine = songs.length
      ? '<div class="live-songs"><svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-music"/></svg> ' + songs.slice(0, 6).map(esc).join(', ') +
        (songs.length > 6 ? ' <span class="live-songs-more">+' + (songs.length - 6) + ' more</span>' : '') + '</div>'
      : '';
    const sub = [
      'row #' + (Number(d.row_id) || '?'),
      total > 0 ? (done + ' / ' + total + ' tracks') : '',
      d.elapsed_seconds != null ? (esc(fmtSecs(d.elapsed_seconds)) + ' elapsed') : '',
      d.eta_seconds != null ? ('~' + esc(fmtSecs(d.eta_seconds)) + ' left') : '',
    ].filter(Boolean).join(' · ');
    return (
      '<div class="live-art">' + initials + '</div>' +
      '<div class="live-meta">' +
        '<div class="live-title">' + (titleParts || 'Downloading…') + qChip + sChip + upChip + '</div>' +
        songLine +
        '<div class="live-sub">' + sub + '</div>' +
        '<div class="live-progress" title="' + done + ' of ' + (total || '?') + ' tracks">' +
          '<div class="live-progress-bar" style="width:' + p.toFixed(1) + '%"></div>' +
        '</div>' +
      '</div>'
    );
  }
  function makeStrip(d) {
    const el = document.createElement('div');
    el.className = 'live-strip';
    el.dataset.rowId = String(d.row_id);
    el.innerHTML = stripInner(d);
    return el;
  }
  function findStrip(rid) {
    return root.querySelector('.live-strip[data-row-id="' + cssEsc(String(rid)) + '"]');
  }
  function animateDeparture(rid, outcome) {
    const el = findStrip(rid);
    if (!el || animating.has(rid)) return;
    animating.add(rid);
    // upgrade (low-end FLAC → hi-res) glows GOLD; success green; fail red.
    const cls = outcome === 'fail' ? 'live-strip-fail'
              : outcome === 'upgrade' ? 'live-strip-upgrade'
              : 'live-strip-success';
    el.classList.add(cls);
    setTimeout(() => { try { el.remove(); } catch (e) {} animating.delete(rid); }, outcome === 'fail' ? 850 : 950);
  }

  async function poll() {
    let data;
    try {
      data = await (await fetch('/api/dashboard/live', { cache: 'no-store' })).json();
    } catch (e) {
      if (!root.querySelector('.live-strip')) root.innerHTML = '<div class="live-empty">Couldn\'t reach live endpoint.</div>';
      dispatchUpdated(); return;
    }
    const dls = data.downloads || (data.downloading ? [data.downloading] : []);
    const outcomes = {}; (data.recent_outcomes || []).forEach(o => { outcomes[String(o.row_id)] = o; });
    const nowIds = new Set(dls.map(d => String(d.row_id)));

    // Departures: a row left the live card. DON'T guess — only go GREEN on a CONFIRMED
    // success (which means it's imported → it WILL be in Recently Added), RED on a confirmed
    // fail. The DB import finishes a beat after the row leaves, so while the outcome isn't
    // known we keep the strip in a "finishing" state and re-check on the next poll.
    Object.keys(prevRows).forEach(rid => {
      if (!nowIds.has(rid) && !animating.has(rid) && !(rid in pendingDepart)) {
        pendingDepart[rid] = Date.now();
      }
    });
    Object.keys(pendingDepart).forEach(rid => {
      if (nowIds.has(rid)) { delete pendingDepart[rid]; return; }  // re-picked / requeued
      const o = outcomes[rid];
      if (o && (o.outcome === 'success' || o.outcome === 'upgrade')) {
        animateDeparture(rid, o.outcome);
        delete pendingDepart[rid];
        try { window.dispatchEvent(new CustomEvent('dashboard:album-imported', { detail: o })); } catch (e) {}
      } else if (o && o.outcome === 'fail') {
        animateDeparture(rid, 'fail');
        delete pendingDepart[rid];
      } else if (Date.now() - pendingDepart[rid] > 25000) {
        // No outcome after 25s — give up quietly rather than claim a false success.
        const el = findStrip(rid); if (el) { try { el.remove(); } catch (e) {} }
        delete pendingDepart[rid];
      } else {
        const el = findStrip(rid); if (el) el.classList.add('live-strip-finishing');
      }
    });

    // Upserts: update existing strips in place; append new ones with an enter animation.
    if (dls.length > 0) root.querySelectorAll('.live-idle, .live-empty').forEach(n => n.remove());
    dls.forEach(d => {
      const el = findStrip(d.row_id);
      if (el && !animating.has(String(d.row_id))) {
        el.innerHTML = stripInner(d);
      } else if (!el) {
        if (!root.querySelector('.live-strip')) {
          root.querySelectorAll('.live-idle, .live-empty').forEach(n => n.remove());
        }
        const ns = makeStrip(d);
        ns.classList.add('live-strip-enter');
        root.appendChild(ns);
      }
    });

    prevRows = {}; dls.forEach(d => { prevRows[String(d.row_id)] = d; });

    // Idle text only when nothing is showing AND nothing is mid-animation.
    if (dls.length === 0 && root.querySelectorAll('.live-strip').length === 0) {
      root.innerHTML = fmtIdle(data);
    }
    dispatchUpdated();
  }

  function dispatchUpdated() {
    try { window.dispatchEvent(new CustomEvent('dashboard:updated', { detail: { src: 'live' } })); } catch (e) {}
  }

  function start() { if (timer) return; poll(); timer = setInterval(poll, 2000); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); else start(); });

  window.dashboardLive = {
    refresh: () => poll(),
    setAutoRefresh: (on) => { if (on) start(); else stop(); }
  };
  start();
})();
