// playlist-preview.js — expand a playlist row to see its tracks with filters.
(function () {
  // Per-details state: { items: [], total: N, filters: {...} }
  const STATE = new WeakMap();

  function esc(s) {
    return String(s || '').replace(/[<>"'&]/g, c => ({"<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","&":"&amp;"}[c]));
  }

  function statusLabel(item) {
    if (item.in_plex) return {cls: 'st-plex', text: plxIcon('i-check'), title: 'matched in Plex'};
    const a = item.acq_status;
    if (a === 'downloading') return {cls: 'st-downloading', text: '⇣', title: 'downloading now'};
    if (a === 'abandoned') return {cls: 'st-abandoned', text: plxIcon('i-x'), title: 'abandoned (failed many times)'};
    if (a === 'queued') return {cls: 'st-queued', text: '⋯', title: 'queued for download'};
    if (a === 'imported') return {cls: 'st-imported', text: '⇩', title: 'imported (Plex matcher pending)'};
    return {cls: 'st-none', text: '○', title: 'not yet in Plex'};
  }

  function filterAndSort(items, filters) {
    const q = (filters.search || '').toLowerCase().trim();
    const status = filters.status || 'all';
    const sortBy = filters.sort || 'position';
    let out = items.filter(it => {
      if (q) {
        const hay = (it.title + ' ' + it.artist + ' ' + it.album).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (status === 'in_plex' && !it.in_plex) return false;
      if (status === 'not_in_plex' && it.in_plex) return false;
      if (status === 'downloading' && it.acq_status !== 'downloading') return false;
      if (status === 'abandoned' && it.acq_status !== 'abandoned') return false;
      return true;
    });
    const cmp = (a, b, key) => (a[key] || '').toLowerCase().localeCompare((b[key] || '').toLowerCase());
    if (sortBy === 'title') out.sort((a, b) => cmp(a, b, 'title'));
    else if (sortBy === 'artist') out.sort((a, b) => cmp(a, b, 'artist'));
    else if (sortBy === 'added_at') out.sort((a, b) => (b.added_at || '').localeCompare(a.added_at || ''));
    // 'position' is the default (items already in order)
    return out;
  }

  function renderRows(filtered) {
    if (!filtered.length) {
      return '<div class="preview-empty">No tracks match the current filters.</div>';
    }
    let html = '<table class="preview-tracks"><thead><tr>' +
      '<th>#</th><th>Title</th><th>Artist</th><th>Album</th><th title="Plex / acquisition status">Plex</th>' +
      '</tr></thead><tbody>';
    filtered.forEach(t => {
      const st = statusLabel(t);
      const cls = t.in_plex ? 'in-plex' : 'not-in-plex';
      html += `<tr class="${cls}">
        <td class="muted">${t.position + 1}</td>
        <td>${esc(t.title)}</td>
        <td>${esc(t.artist)}</td>
        <td class="muted">${esc(t.album)}</td>
        <td class="${st.cls} small" title="${st.title}">${st.text}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    return html;
  }

  function renderFilterBar(state) {
    const f = state.filters;
    const pill = (val, label) =>
      `<button class="filter-pill ${f.status === val ? 'active' : ''}" data-status="${val}">${label}</button>`;
    return `
      <div class="preview-filters">
        <input type="search" class="filter-search" placeholder="Search title / artist / album…" value="${esc(f.search || '')}">
        <div class="filter-pills">
          ${pill('all', 'All')}
          ${pill('in_plex', plxIcon('i-check') + ' In Plex')}
          ${pill('not_in_plex', '○ Pending')}
          ${pill('downloading', '⇣ Downloading')}
          ${pill('abandoned', plxIcon('i-x') + ' Abandoned')}
        </div>
        <select class="filter-sort">
          <option value="position" ${f.sort === 'position' ? 'selected' : ''}>Position (Spotify order)</option>
          <option value="title" ${f.sort === 'title' ? 'selected' : ''}>Title A→Z</option>
          <option value="artist" ${f.sort === 'artist' ? 'selected' : ''}>Artist A→Z</option>
          <option value="added_at" ${f.sort === 'added_at' ? 'selected' : ''}>Recently added</option>
        </select>
        <button class="filter-clear" title="Reset filters">Clear</button>
        <span class="filter-count muted small"></span>
      </div>
      <div class="preview-rows"></div>
      <div class="preview-more-wrap"></div>
    `;
  }

  function applyFilters(details) {
    const state = STATE.get(details);
    if (!state) return;
    const filtered = filterAndSort(state.items, state.filters);
    const rowsHost = details.querySelector('.preview-rows');
    if (rowsHost) rowsHost.innerHTML = renderRows(filtered);
    const countEl = details.querySelector('.filter-count');
    if (countEl) {
      countEl.textContent = filtered.length === state.items.length
        ? `${state.items.length} of ${state.total} loaded`
        : `${filtered.length} of ${state.items.length} loaded (filtered) • ${state.total} total`;
    }
  }

  function attachFilterEvents(details) {
    const state = STATE.get(details);
    const sb = details.querySelector('.filter-search');
    if (sb) {
      sb.addEventListener('input', () => {
        state.filters.search = sb.value;
        applyFilters(details);
      });
    }
    details.querySelectorAll('.filter-pill').forEach(p => {
      p.addEventListener('click', () => {
        state.filters.status = p.dataset.status;
        details.querySelectorAll('.filter-pill').forEach(pp =>
          pp.classList.toggle('active', pp.dataset.status === p.dataset.status));
        applyFilters(details);
      });
    });
    const sortSel = details.querySelector('.filter-sort');
    if (sortSel) {
      sortSel.addEventListener('change', () => {
        state.filters.sort = sortSel.value;
        applyFilters(details);
      });
    }
    const clearBtn = details.querySelector('.filter-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        state.filters = {search: '', status: 'all', sort: 'position'};
        if (sb) sb.value = '';
        details.querySelectorAll('.filter-pill').forEach(p =>
          p.classList.toggle('active', p.dataset.status === 'all'));
        if (sortSel) sortSel.value = 'position';
        applyFilters(details);
      });
    }
  }

  async function loadAll(pairId, body, details) {
    body.innerHTML = '<div class="preview-loading">Loading tracks…</div>';
    let items = [];
    let total = 0;
    let offset = 0;
    const limit = 500;
    while (true) {
      try {
        const res = await fetch(`/api/playlists/${pairId}/preview?offset=${offset}&limit=${limit}`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        items = items.concat(data.items || []);
        total = data.total;
        if (!data.items || data.items.length < limit) break;
        offset += data.items.length;
        if (offset >= 5000) break;  // safety cap
      } catch (e) {
        body.innerHTML = `<div class="preview-error">Couldn't load: ${esc(e.message)}</div>`;
        return;
      }
    }
    const state = {items, total, filters: {search: '', status: 'all', sort: 'position'}};
    STATE.set(details, state);
    body.innerHTML = renderFilterBar(state);
    attachFilterEvents(details);
    applyFilters(details);
  }

  document.addEventListener('toggle', (e) => {
    if (!(e.target instanceof HTMLDetailsElement)) return;
    if (!e.target.classList.contains('playlist-preview')) return;
    if (!e.target.open) return;
    const body = e.target.querySelector('.playlist-preview-body');
    if (body.dataset.loaded === '1') return;
    body.dataset.loaded = '1';
    loadAll(e.target.dataset.pairId, body, e.target);
  }, true);
})();
