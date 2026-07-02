// dashboard-controls.js — wires the top control bar.
(function () {
  const refreshBtn = document.getElementById('dashctl-refresh');
  const runBtn     = document.getElementById('dashctl-run');
  const pauseBtn   = document.getElementById('dashctl-pause');
  const pauseLbl   = document.getElementById('dashctl-pause-lbl');
  const pauseIco   = document.getElementById('dashctl-pause-ico');
  const autoChk    = document.getElementById('dashctl-autorefresh');
  const status     = document.getElementById('dashctl-status');
  const inflightEl = document.getElementById('dashctl-inflight');
  const queueEl    = document.getElementById('dashctl-queue');
  const nextEl     = document.getElementById('dashctl-next');
  const slotsEl    = document.getElementById('dashctl-slots');
  const coverageEl = document.getElementById('dashctl-coverage');
  const covWrap    = document.getElementById('dashctl-cov-wrap');
  let prevCoverage = null;
  // F-8: comprehensive guard — bail if ANY required element is missing.
  const _required = [refreshBtn, runBtn, pauseBtn, pauseLbl, pauseIco, autoChk, status, inflightEl, queueEl, nextEl, slotsEl];
  if (!_required.every(Boolean)) return;

  let lastUpdated = Date.now();
  let paused = false;

  function setStatus(msg, transient) {
    status.innerHTML = msg;
    if (transient) setTimeout(() => { tickStatus(); }, 2200);
  }
  function tickStatus() {
    const s = Math.max(0, Math.floor((Date.now() - lastUpdated) / 1000));
    status.textContent = s < 2 ? 'just now' : (s + 's ago');
  }
  setInterval(tickStatus, 1000);

  window.addEventListener('dashboard:updated', () => {
    lastUpdated = Date.now();
    tickStatus();
  });

  async function fetchStatus() {
    try {
      const r = await fetch('/api/picker/status', { cache: 'no-store' });
      const d = await r.json();
      paused = !!d.paused;
      pauseLbl.textContent = paused ? 'Resume picker' : 'Pause picker';
      pauseIco.innerHTML = '<svg class="icn" aria-hidden="true"><use href="/static/icons.svg#i-' + (paused ? 'play' : 'pause') + '"/></svg>';
      pauseBtn.classList.toggle('dashctl-btn-active', paused);
      // ZF: surface circuit-breaker cooldown
      const cd = d.cooldown || {};
      if (cd.in_cooldown && cd.seconds_remaining > 0) {
        const mins = Math.ceil(cd.seconds_remaining / 60);
        setStatus(plxIcon('i-alert') + ` providers down — paused ${mins}m`, false);
      }
      // Surface why nothing is downloading when Plex is streaming a video.
      let banner = document.getElementById('dashctl-streaming');
      if (d.streaming_paused) {
        if (!banner) {
          banner = document.createElement('div');
          banner.id = 'dashctl-streaming';
          banner.className = 'dashctl-streaming';
          const card = document.getElementById('dash-controls');
          if (card) card.appendChild(banner);
        }
        const n = d.streaming_sessions || 1;
        banner.innerHTML = plxIcon('i-pause') + ' Downloads paused — Plex is streaming ' + n + ' video' + (n !== 1 ? 's' : '') + '. They resume automatically when playback stops.';
        banner.style.display = '';
      } else if (banner) {
        banner.style.display = 'none';
      }
      inflightEl.textContent = (d.in_flight != null) ? d.in_flight : '—';
      queueEl.textContent    = (d.queue_depth != null) ? d.queue_depth : '—';
      const _slots = d.max_instances, _inf = d.in_flight;
      if (d.streaming_paused) nextEl.textContent = 'paused (streaming)';
      else if (paused) nextEl.textContent = 'paused';
      else if (_slots != null && _inf != null && _inf >= _slots) nextEl.textContent = 'slots full';
      else nextEl.textContent = (d.next_run_in_seconds != null ? d.next_run_in_seconds + 's' : '—');
      slotsEl.textContent    = (_slots != null) ? ('auto ' + _slots + (d.concurrency_ceiling != null ? ' / ' + d.concurrency_ceiling : '')) : '—';
      // Plex coverage — with a fun pop + rising "▲ +N%" flourish when it climbs.
      if (coverageEl && d.plex_coverage_pct != null) {
        const cov = d.plex_coverage_pct;
        coverageEl.textContent = cov + '%';
        if (prevCoverage != null && cov > prevCoverage && covWrap) {
          covWrap.classList.remove('cov-bump');
          void covWrap.offsetWidth;                 // reflow → restart animation
          covWrap.classList.add('cov-bump');
          let fl = covWrap.querySelector('.cov-rise');
          if (!fl) { fl = document.createElement('span'); fl.className = 'cov-rise'; covWrap.appendChild(fl); }
          fl.textContent = '▲ +' + (cov - prevCoverage) + '%';
          fl.classList.remove('show'); void fl.offsetWidth; fl.classList.add('show');
        }
        prevCoverage = cov;
      }
    } catch (e) {}
  }
  fetchStatus();
  setInterval(fetchStatus, 3000);

  refreshBtn.addEventListener('click', () => {
    setStatus('refreshing…', true);
    if (window.dashboardLive && typeof window.dashboardLive.refresh === 'function') window.dashboardLive.refresh();
    if (window.dashboardFeed && typeof window.dashboardFeed.refresh === 'function') window.dashboardFeed.refresh();
    fetchStatus();
  });

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    setStatus('firing picker…', true);
    try {
      const r = await fetch('/api/picker/run-now', { method: 'POST', headers: {'X-Requested-With':'fetch'} });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      setStatus(plxIcon('i-check') + ' picker fired', true);
      setTimeout(() => {
        if (window.dashboardLive && typeof window.dashboardLive.refresh === 'function') window.dashboardLive.refresh();
        fetchStatus();
      }, 600);
    } catch (e) {
      setStatus(plxIcon('i-x') + ' ' + plxEsc(e.message), true);
    } finally {
      setTimeout(() => { runBtn.disabled = false; }, 1500);
    }
  });

  pauseBtn.addEventListener('click', async () => {
    pauseBtn.disabled = true;
    const action = paused ? 'resume' : 'pause';
    setStatus(action + 'ing picker…', true);
    try {
      const r = await fetch('/api/picker/' + action, { method: 'POST', headers: {'X-Requested-With':'fetch'} });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      paused = !!d.paused;
      setStatus(plxIcon('i-check') + ' ' + (paused ? 'paused' : 'resumed'), true);
      fetchStatus();
    } catch (e) {
      setStatus(plxIcon('i-x') + ' ' + plxEsc(e.message), true);
    } finally {
      setTimeout(() => { pauseBtn.disabled = false; }, 800);
    }
  });

  autoChk.addEventListener('change', () => {
    const on = autoChk.checked;
    if (window.dashboardLive && typeof window.dashboardLive.setAutoRefresh === 'function') window.dashboardLive.setAutoRefresh(on);
    if (window.dashboardFeed && typeof window.dashboardFeed.setAutoRefresh === 'function') window.dashboardFeed.setAutoRefresh(on);
    setStatus(on ? 'auto-refresh ON' : 'auto-refresh OFF', true);
  });
})();
