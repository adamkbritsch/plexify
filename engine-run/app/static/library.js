// library.js — Plex-style Library: filter / type (Albums|Artists) / sort dropdowns,
// album grid + artist grid, and an on-demand "Condense Library" run (the album rulebook).
(function () {
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  const grid = document.getElementById('lib-grid');
  const moreWrap = document.getElementById('lib-more-wrap');
  const search = document.getElementById('lib-search');
  const countEl = document.getElementById('lib-count');
  if (!grid) return;

  const limit = 24;
  let offset = 0, total = 0, q = '', filter = 'all', sort = 'recent', source = '', type = 'albums', loading = false, firstLoad = true;

  function hueFor(a, b) { let h = 0; const s = (a || '') + '|' + (b || ''); for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h % 360; }
  function ago(iso) {
    if (!iso) return '';
    const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (sec < 60) return sec + 's ago';
    const m = Math.floor(sec / 60); if (m < 60) return m + 'm ago';
    const h = Math.floor(m / 60); if (h < 24) return h + 'h ago';
    const d = Math.floor(h / 24); if (d < 7) return d + 'd ago';
    return Math.floor(d / 7) + 'w ago';
  }

  function albumTile(it) {
    const id = Number(it.id) || 0;
    const initials = esc((it.artist || '?').split(/\s+/).map(w => w[0] || '').join('').slice(0, 3).toUpperCase());
    const hue = hueFor(it.artist, it.album);
    const tc = Number(it.track_count) || 0, total = Number(it.total_tracks) || 0;
    const countStr = (!it.completed && total > 0) ? (tc + ' / ' + total + ' tracks') : (tc ? (tc + ' track' + (tc !== 1 ? 's' : '')) : '');
    const meta = [countStr, it.source || '', it.imported_at ? ago(it.imported_at) : ''].filter(Boolean).join(' · ');
    const badge = it.locked
      ? '<span class="lib-check lib-locked" title="Complete — locked"><svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-lock"/></svg></span>'
      : it.completed
      ? '<span class="lib-check" title="All songs present">&#10003;</span>'
      : '<span class="lib-pending" title="' + (total ? (tc + ' of ' + total + ' songs') : 'some songs') + '">' + (total ? esc(tc + '/' + total) : '&#8943;') + '</span>';
    return `
      <div class="lib-card ${it.completed ? 'completed' : 'partial'}" data-album-id="${id}" tabindex="0" role="link" aria-label="Open ${esc(it.artist || '?')} — ${esc(it.album || '?')} in Plex">
        <div class="lib-art" style="--hue: ${hue}"><img src="/api/album-art/${id}" alt="" data-initials="${initials}">${badge}</div>
        <div class="lib-cap"><strong>${esc(it.album || '?')}</strong><small>${esc(it.artist || '?')}</small><small class="muted">${esc(meta)}</small></div>
      </div>`;
  }
  function artistTile(g) {
    const initials = esc((g.artist || '?').split(/\s+/).map(w => w[0] || '').join('').slice(0, 3).toUpperCase());
    const hue = hueFor(g.artist, '');
    return `
      <div class="lib-card lib-artist" data-artist="${esc(g.artist)}" tabindex="0" role="link" aria-label="Show ${esc(g.artist)} albums">
        <div class="lib-art lib-art-round" style="--hue: ${hue}"><img src="/api/album-art/${Number(g.id) || 0}" alt="" data-initials="${initials}"></div>
        <div class="lib-cap"><strong>${esc(g.artist)}</strong><small class="muted">${g.albums} album${g.albums !== 1 ? 's' : ''}</small></div>
      </div>`;
  }

  function libUrl(off, lim, srt) {
    return `/api/library/albums?offset=${off}&limit=${lim}&q=${encodeURIComponent(q)}&filter=${filter}&sort=${srt}&source=${source}`;
  }
  async function loadAlbums(reset) {
    if (reset) { offset = 0; firstLoad = true; grid.style.display = ''; grid.innerHTML = '<div class="feed-empty">Loading…</div>'; }
    const res = await fetch(libUrl(offset, limit, sort));
    const data = await res.json();
    total = data.total || 0; const items = data.items || [];
    if (firstLoad) { firstLoad = false; grid.innerHTML = items.length ? '' : '<div class="feed-empty">No albums found.</div>'; }
    items.forEach(it => grid.insertAdjacentHTML('beforeend', albumTile(it)));
    offset += items.length;
    countEl.textContent = total ? (total + ' album' + (total !== 1 ? 's' : '')) : '';
    moreWrap.innerHTML = (offset < total) ? '<div class="feed-more" id="lib-more">Show more →</div>' : '';
    const mb = document.getElementById('lib-more');
    if (mb) mb.addEventListener('click', () => { loading = false; loading = true; loadAlbums(false).finally(() => loading = false); });
  }
  async function loadArtists() {
    grid.style.display = ''; grid.innerHTML = '<div class="feed-empty">Loading\u2026</div>'; moreWrap.innerHTML = '';
    const res = await fetch(`/api/library/artists?q=${encodeURIComponent(q)}&filter=${filter}&source=${source}`);
    const data = await res.json(); const groups = data.items || [];
    grid.innerHTML = groups.length ? groups.map(artistTile).join('') : '<div class="feed-empty">No artists found.</div>';
    countEl.textContent = groups.length + ' artist' + (groups.length !== 1 ? 's' : '');
  }
  function songTableRow(it) {
    const tid = esc(it.track_id || '');
    const dis = !!it.disputed;
    let act;
    if (dis) act = '<span class="lib-disp-tag">disputed</span>';
    else if (!it.on_disk) act = '<span class="muted">missing</span>';
    else act = `<button type="button" class="lib-dispute-btn" data-track="${tid}" title="Mark as the wrong audio \u2014 removes it and re-acquires from another source">Dispute</button>`;
    return `<tr class="${dis ? 'st-disputed' : (it.on_disk ? '' : 'st-missing')}">
      <td class="lib-c-title">${esc(it.title || '?')}</td>
      <td class="lib-c-artist">${esc(it.artist || '?')}</td>
      <td class="lib-c-src">${esc(it.source || '')}</td>
      <td class="lib-c-act">${act}</td>
    </tr>`;
  }
  async function loadSongs(reset) {
    if (reset) { offset = 0; firstLoad = true; grid.style.display = 'block'; grid.innerHTML = '<div class="feed-empty">Loading\u2026</div>'; }
    const res = await fetch(`/api/library/songs?offset=${offset}&limit=50&q=${encodeURIComponent(q)}&filter=${filter}`);
    const data = await res.json(); total = data.total || 0; const items = data.items || [];
    if (firstLoad) {
      firstLoad = false;
      if (!items.length) { grid.innerHTML = '<div class="feed-empty">No songs found.</div>'; countEl.textContent = ''; moreWrap.innerHTML = ''; return; }
      grid.innerHTML = '<table class="lib-songtable"><thead><tr><th>Title</th><th>Artist</th><th>Source</th><th></th></tr></thead><tbody></tbody></table>';
    }
    const tb = grid.querySelector('.lib-songtable tbody');
    if (tb) items.forEach(it => tb.insertAdjacentHTML('beforeend', songTableRow(it)));
    offset += items.length;
    countEl.textContent = total ? (total + ' song' + (total !== 1 ? 's' : '')) : '';
    moreWrap.innerHTML = (offset < total) ? '<div class="feed-more" id="lib-more">Show more \u2192</div>' : '';
    const mb = document.getElementById('lib-more');
    if (mb) mb.addEventListener('click', () => { loading = true; loadSongs(false).finally(() => loading = false); });
  }
  function reload() {
    if (loading) return; loading = true;
    (type === 'songs' ? loadSongs(true) : type === 'artists' ? loadArtists() : loadAlbums(true)).catch(() => {
      if (firstLoad) grid.innerHTML = '<div class="feed-empty">Couldn\'t load the library.</div>';
    }).finally(() => { loading = false; });
  }

  // ---- album detail (in-app drill-down) + dispute ----
  const detail = document.getElementById('lib-detail');
  const plexbar = document.querySelector('.lib-plexbar');
  function fmtDur(s){ s = Math.round(s || 0); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
  function showDetail(on){
    // toggle display directly: .lib-grid/.lib-plexbar set their own display, which
    // overrides the [hidden] attribute, so .hidden wouldn't actually hide them.
    if (plexbar) plexbar.style.display = on ? 'none' : '';
    if (condMsg) condMsg.style.display = on ? 'none' : '';
    grid.style.display = on ? 'none' : '';
    moreWrap.style.display = on ? 'none' : '';
    if (detail) { detail.hidden = false; detail.style.display = on ? '' : 'none'; }
    window.scrollTo(0, 0);
  }
  function closeDetail(){ showDetail(false); if (detail) detail.innerHTML = ''; }
  async function openAlbum(rowId){
    if (!detail) return;
    detail.innerHTML = '<div class="feed-empty">Loading\u2026</div>'; showDetail(true);
    try {
      const d = await (await fetch('/api/library/album/' + rowId)).json();
      detail.innerHTML = renderAlbumDetail(d);
    } catch (e) {
      detail.innerHTML = '<div class="lib-detail-head"><button type="button" class="lib-back">\u2190 Back</button></div><div class="feed-empty">Couldn\'t load the album.</div>';
    }
  }
  function renderAlbumDetail(d){
    const tracks = d.tracks || [];
    const cover = d.cover_id ? ('/api/album-art/' + d.cover_id) : '';
    const rows = tracks.map(t => {
      const dis = !!t.disputed;
      const tid = t.track_id ? esc(t.track_id) : '';
      const fp = esc(t.file_path || '');
      const act = dis ? '<span class="lib-disp-tag">disputed</span>'
        : `<button type="button" class="lib-dispute-btn" data-track="${tid}" data-row="${d.row_id}" data-file="${fp}" title="Mark as the wrong audio \u2014 removes it and re-acquires from another source">Dispute</button>`;
      return `<tr class="${dis ? 'st-disputed' : ''}">
        <td class="num lib-track-no">${t.track_no || ''}</td>
        <td class="lib-c-title">${esc(t.title || '?')}</td>
        <td class="lib-c-act">${act}</td>
      </tr>`;
    }).join('');
    return `
      <div class="lib-detail-head"><button type="button" class="lib-back">\u2190 Back</button></div>
      <div class="lib-detail-meta">
        <div class="lib-detail-cover"><img src="${cover}" alt=""></div>
        <div class="lib-detail-info">
          <small class="muted">${esc(d.artist || '?')}</small>
          <h2>${esc(d.album || '?')}</h2>
          <small class="muted">${tracks.length} track${tracks.length !== 1 ? 's' : ''}${d.source ? ' \u00b7 ' + esc(d.source) : ''}${d.locked ? ' \u00b7 locked' : ''}</small>
          <div class="lib-detail-actions"><a href="/api/album-go/${d.row_id}" target="_blank">Open in Plex \u2192</a></div>
        </div>
      </div>
      ${rows ? '<table class="lib-tracktable"><thead><tr><th class="num">#</th><th>Title</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>' : '<div class="feed-empty">No tracks on disk.</div>'}`;
  }
  function doDispute(db){
    if (!db || db.disabled) return;
    const tid = db.getAttribute('data-track');
    const rid = db.getAttribute('data-row');
    const fp = db.getAttribute('data-file');
    if (!tid && !(rid && fp)) return;
    if (!window.confirm('Mark this song as the WRONG audio?\n\nIt will be removed and re-downloaded from a different source \u2014 the source it came from gets blacklisted for this song.')) return;
    const _o = db.textContent; db.disabled = true; db.textContent = '\u2026';
    const body = tid ? { track_id: tid } : { row_id: rid, file_path: fp };
    fetch('/library/dispute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(r => r.json()).then(d => {
        if (d.ok) {
          db.textContent = 'disputed'; db.classList.add('done');
          const row = db.closest('.lib-songrow') || db.closest('.lib-trackrow') || db.closest('tr');
          if (row) { row.classList.add('st-disputed'); const st = row.querySelector('.lib-song-state') || row.querySelector('.lib-track-state'); if (st) { st.textContent = 're-acquiring (avoiding ' + (d.source || '?') + ')'; st.classList.add('disputed'); } }
        } else { db.disabled = false; db.textContent = _o; alert('Dispute failed: ' + (d.error || 'unknown')); }
      }).catch(() => { db.disabled = false; db.textContent = _o; });
  }
  if (detail) detail.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('.lib-back')) { closeDetail(); return; }
    const db = e.target.closest && e.target.closest('.lib-dispute-btn');
    if (db) { e.preventDefault(); doDispute(db); return; }
  });

  // open album / drill into artist
  grid.addEventListener('click', (e) => {
    const db = e.target.closest && e.target.closest('.lib-dispute-btn');
    if (db) {
      e.preventDefault(); e.stopPropagation();
      const tid = db.getAttribute('data-track');
      if (!tid || db.disabled) return;
      if (!window.confirm('Mark this song as the WRONG audio?\n\nIt will be removed and re-downloaded from a different source \u2014 the source it came from gets blacklisted for this song.')) return;
      const _o = db.textContent; db.disabled = true; db.textContent = '\u2026';
      fetch('/library/dispute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: tid }) })
        .then(r => r.json()).then(d => {
          if (d.ok) {
            db.textContent = 'disputed'; db.classList.add('done');
            const row = db.closest('.lib-songrow');
            if (row) { const st = row.querySelector('.lib-song-state'); if (st) { st.textContent = 're-acquiring (avoiding ' + (d.source || '?') + ')'; st.classList.add('disputed'); } }
          } else { db.disabled = false; db.textContent = _o; alert('Dispute failed: ' + (d.error || 'unknown')); }
        }).catch(() => { db.disabled = false; db.textContent = _o; });
      return;
    }
    const art = e.target.closest && e.target.closest('.lib-artist[data-artist]');
    if (art) {
      q = art.getAttribute('data-artist') || ''; if (search) search.value = q;
      setDD('type', 'albums', 'Albums'); type = 'albums'; reload(); return;
    }
    const c = e.target.closest && e.target.closest('.lib-card[data-album-id]');
    if (c) { const id = Number(c.dataset.albumId) || 0; if (id > 0) openAlbum(id); }
  });
  grid.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.target.click(); } });
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (!img || img.tagName !== 'IMG' || !img.closest('.lib-art')) return;
    img.style.display = 'none'; const ini = img.dataset.initials || '';
    if (ini) img.parentNode.textContent = ini;
  }, true);

  // ---- dropdowns ----
  function setDD(which, val, label) {
    const dd = document.querySelector('.lib-dd[data-dd="' + which + '"]'); if (!dd) return;
    dd.querySelectorAll('.lib-dd-opt').forEach(x => x.classList.toggle('active', x.getAttribute('data-val') === val));
    if (label) dd.querySelector('.lib-dd-cur').textContent = label;
  }
  document.querySelectorAll('.lib-dd').forEach(dd => {
    const btn = dd.querySelector('.lib-dd-btn'), menu = dd.querySelector('.lib-dd-menu'), cur = dd.querySelector('.lib-dd-cur');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      document.querySelectorAll('.lib-dd-menu').forEach(m => { if (m !== menu) m.hidden = true; });
      menu.hidden = !menu.hidden;
    });
    menu.addEventListener('click', (e) => {
      const o = e.target.closest('.lib-dd-opt'); if (!o) return; e.stopPropagation();
      menu.querySelectorAll('.lib-dd-opt').forEach(x => x.classList.remove('active')); o.classList.add('active');
      cur.textContent = o.textContent; menu.hidden = true;
      const which = dd.getAttribute('data-dd'), val = o.getAttribute('data-val');
      if (which === 'filter') filter = val;
      else if (which === 'type') type = val;
      else if (which === 'sort') sort = val;
      reload();
    });
  });
  document.addEventListener('click', () => document.querySelectorAll('.lib-dd-menu').forEach(m => m.hidden = true));

  let t = null;
  if (search) search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { q = search.value.trim(); reload(); }, 300); });

  // ---- Condense Library (the album rulebook on demand) ----
  const condBtn = document.getElementById('lib-condense');
  const condMsg = document.getElementById('lib-condense-msg');
  function showMsg(html, kind) { if (!condMsg) return; condMsg.innerHTML = html; condMsg.className = 'lib-complete-msg ' + (kind || ''); condMsg.hidden = false; }
  function pollCondense() {
    fetch('/library/condense-status').then(r => r.json()).then(d => {
      if (!d || d.state === 'idle') return;
      const totals = 'combined ' + (d.merged || 0) + ' · de-duped ' + (d.deduped || 0) + ' · split ' + (d.rehomed || 0) + ' · tiles ' + (d.tiles || 0);
      if (d.state === 'running') {
        showMsg('<svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-hourglass"></use></svg> ' + esc(d.phase || 'Condensing…') + ' &middot; ' + totals, 'ok');
        setTimeout(pollCondense, 2000);
      } else if (d.state === 'done') {
        showMsg('<svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-check"></use></svg> Condensed — ' + totals + '. Refreshing…', 'ok');
        if (condBtn) condBtn.disabled = false;
        reload();
      } else {
        showMsg(plxIcon('i-alert') + ' ' + esc(d.error || 'failed'), 'warn'); if (condBtn) condBtn.disabled = false;
      }
    }).catch(() => { if (condBtn) condBtn.disabled = false; });
  }
  if (condBtn) condBtn.addEventListener('click', () => {
    condBtn.disabled = true;
    showMsg('<svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-hourglass"></use></svg> Starting…', 'ok');
    fetch('/library/condense-now', { method: 'POST', headers: { 'X-Requested-With': 'fetch' } })
      .then(r => r.json()).then(() => pollCondense())
      .catch(() => { condBtn.disabled = false; showMsg('Couldn\'t reach the server.', 'warn'); });
  });
  // if a condense is already running (e.g. page reload), resume the indicator
  pollCondense();

  reload();
})();
