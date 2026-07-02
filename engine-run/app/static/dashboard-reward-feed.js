// dashboard-reward-feed.js — loads /api/dashboard/reward-feed, renders rows, handles
// "show more" pagination + row clicks. refresh() pulls NEWLY-imported albums to the top
// with a slide-in highlight — that's the landing half of the "moves from Right now to
// Recently added" animation (dashboard-live.js fires dashboard:album-imported on success).
(function () {
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  const root = document.getElementById('feed-content');
  const moreWrap = document.getElementById('feed-more-wrap');
  if (!root) return;

  let offset = 0;
  const limit = 10;
  let firstLoad = true;
  const shownIds = new Set();

  function ago(iso) {
    if (!iso) return '';
    const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} min ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const d = Math.floor(hr / 24);
    if (d === 1) return 'yesterday';
    if (d < 7) return `${d} days ago`;
    return `${Math.floor(d / 7)}w ago`;
  }
  function fmtSize(bytes) {
    if (!bytes) return '';
    const mb = bytes / 1024 / 1024;
    if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
    return mb.toFixed(0) + ' MB';
  }
  function hueFor(artist, album) {
    let h = 0; const s = (artist || '') + '|' + (album || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 360;
  }

  // FLAT, per-SONG feed: each item from the API is one song (de-grouped from albums),
  // ordered by when the file actually landed. A track added to an existing album shows
  // up on its own — the album isn't re-listed with all its songs. (Album song-counts
  // live on the Albums page.)
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n <= 0) return '';
    if (n >= 1024 * 1024 * 1024) return (n / 1073741824).toFixed(1) + ' GB';
    if (n >= 1024 * 1024) return Math.round(n / 1048576) + ' MB';
    return Math.round(n / 1024) + ' KB';
  }
  function qualityChip(item) {
    const tier = (item.quality_tier || '').toString();
    const label = esc(item.quality_label || '');
    if (tier === 'hires') return '<span class="feed-q feed-q-hires" title="Hi-Res FLAC — ' + label + '">Hi-Res ' + label + '</span>';
    if (tier === 'cd')    return '<span class="feed-q feed-q-cd" title="CD-quality lossless FLAC — ' + label + '">FLAC ' + label + '</span>';
    if (tier === 'lossless') return '<span class="feed-q feed-q-cd" title="Lossless">' + (esc(item.codec) || 'Lossless') + '</span>';
    if (tier === 'lossy') return '<span class="feed-q feed-q-lossy" title="Lossy">' + (esc(item.codec) || 'Lossy') + '</span>';
    return '';
  }
  function songRow(item, isNew) {
    const idSafe = Number(item.id) || 0;
    const artistSafe = esc(item.artist || '?');
    const albumSafe = esc(item.album || '?');
    const songSafe = esc(item.song || item.name || '?');
    const srcRaw = (item.source || '').toString();
    const srcClass = /^[a-zA-Z0-9_-]+$/.test(srcRaw) ? srcRaw : 'unknown';
    const initials = esc((item.artist || '?').split(/\s+/).map(w => w[0] || '').join('').slice(0, 3).toUpperCase());
    const hue = hueFor(item.artist, item.album);
    const heart = item.liked ? '<span class="feed-heart" title="From your Spotify Liked Songs"><svg class="icn icn-s icn-fill" aria-hidden="true"><use href="/static/icons.svg#i-heart"/></svg></span> ' : '';
    const upgraded = item.was_upgraded ? '<span class="feed-upgrade" title="Re-acquired at higher quality (upgraded to hi-res)"><svg class="icn icn-s" aria-hidden="true"><use href="/static/icons.svg#i-arrow-up"/></svg> Upgraded</span>' : '';
    const sizeStr = fmtBytes(item.size_bytes);
    // audiophile detail line: quality · size · codec
    const specBits = [qualityChip(item), upgraded, sizeStr ? '<span class="feed-size">' + sizeStr + '</span>' : ''].filter(Boolean).join('');
    return `
      <div class="feed-row${isNew ? ' feed-row-new' : ''}${item.was_upgraded ? ' feed-row-upgraded' : ''}" data-album-id="${idSafe}" tabindex="0" role="link" aria-label="Open ${artistSafe} — ${albumSafe} in Plex">
        <div class="feed-art" style="--hue: ${hue}">
          <img src="/api/album-art/${idSafe}" alt="" data-initials="${initials}">
        </div>
        <div class="feed-meta">
          <strong>${heart}${songSafe}</strong>
          <small class="feed-sub">${artistSafe} &middot; ${albumSafe}</small>
          <div class="feed-specs">${specBits}</div>
        </div>
        <span class="feed-source ${srcClass}">${esc(srcRaw)}</span>
        <span class="feed-when" data-when="${esc(item.imported_at || '')}">${esc(ago(item.imported_at))}</span>
      </div>`;
  }
  function renderRow(item, isNew) {
    return songRow(item, isNew);
  }

  // Keep the "X ago" labels live without a refresh — recompute every 20s from the
  // stored imported_at timestamp (the row HTML is rendered once; only this text ticks).
  function tickRelativeTimes() {
    root.querySelectorAll('.feed-when[data-when]').forEach(el => {
      const iso = el.getAttribute('data-when');
      if (iso) el.textContent = ago(iso);
    });
  }
  setInterval(tickRelativeTimes, 20000);

  // Pagination — appends OLDER rows (the "show 10 more" button).
  async function loadPage() {
    try {
      const res = await fetch(`/api/dashboard/reward-feed?offset=${offset}&limit=${limit}`, { cache: 'no-store' });
      const data = await res.json();
      const items = data.items || [];
      if (firstLoad) {
        firstLoad = false;
        if (items.length === 0) {
          root.innerHTML = `<div class="feed-empty">Nothing imported yet. Library autofill is running — check back in a few minutes.</div>`;
          if (moreWrap) moreWrap.innerHTML = '';
          return;
        }
        root.innerHTML = '';
      }
      items.forEach(it => {
        const key = it.uid || it.id;
        if (!shownIds.has(key)) {
          shownIds.add(key);
          root.insertAdjacentHTML('beforeend', renderRow(it, false));
        }
      });
      offset += items.length;
      if (moreWrap) {
        if (items.length === limit) {
          moreWrap.innerHTML = `<div class="feed-more" id="feed-more">Show 10 more →</div>`;
          document.getElementById('feed-more').addEventListener('click', loadPage);
        } else {
          moreWrap.innerHTML = '';
        }
      }
    } catch (e) {
      if (firstLoad) root.innerHTML = `<div class="feed-empty">Couldn't load reward feed. <a href="#" onclick="location.reload();return false;">Retry?</a></div>`;
    }
  }

  // refresh — pull NEW imports to the TOP with a slide-in highlight (the landing
  // animation for an album that just succeeded in "Right now").
  async function refresh() {
    try {
      const res = await fetch(`/api/dashboard/reward-feed?offset=0&limit=${limit}`, { cache: 'no-store' });
      const data = await res.json();
      const fresh = (data.items || []).filter(it => !shownIds.has(it.uid || it.id));
      if (!fresh.length) return;
      const emptyEl = root.querySelector('.feed-empty');
      if (emptyEl) emptyEl.remove();
      // reverse so the newest ends up on top after successive afterbegin inserts
      fresh.slice().reverse().forEach(it => {
        shownIds.add(it.uid || it.id);
        root.insertAdjacentHTML('afterbegin', renderRow(it, true));
      });
      // bump offset so pagination doesn't re-serve these
      offset += fresh.length;
      // once the slide-in animation has played, drop the highlight class so the row
      // behaves like a normal feed row (hover, etc.)
      setTimeout(() => root.querySelectorAll('.feed-row-new').forEach(el => el.classList.remove('feed-row-new')), 1300);
    } catch (e) { /* ignore transient */ }
  }

  loadPage();
  window.dashboardFeed = { refresh, setAutoRefresh: () => {} };
  // When an album goes green in "Right now" it's popped from the live card BEFORE its
  // DB import finishes persisting (file moves take seconds), so a single refresh can race
  // the persist and miss it. Retry a few times to reliably catch it.
  window.addEventListener('dashboard:album-imported', () => {
    [700, 2500, 6000].forEach(d => setTimeout(refresh, d));
  });
  // Safety net: poll for new imports every 15s so EVERY success (incl. orphan-sweep
  // imports that never appear in "Right now") reliably lands in Recently Added.
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);

  // Delegated click + keydown → open in Plex
  function openRow(target) {
    const row = target.closest && target.closest('.feed-row[data-album-id]');
    if (!row) return;
    const id = Number(row.dataset.albumId) || 0;
    if (id > 0) window.open('/api/album-go/' + id, '_blank');
  }
  document.addEventListener('click', (e) => openRow(e.target));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest && e.target.closest('.feed-row[data-album-id]');
    if (!row) return;
    e.preventDefault();
    openRow(e.target);
  });
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (!img || img.tagName !== 'IMG' || !img.closest('.feed-art')) return;
    img.style.display = 'none';
    const initials = img.dataset.initials || '';
    if (initials) img.parentNode.textContent = initials;
  }, true);
})();
