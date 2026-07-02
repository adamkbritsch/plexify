(function () {
  // Shared HTML-escape — prevents XSS via server-provided strings (artist/album/etc.)
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  const dot = document.getElementById('status-dot');
  // F-10: a11y — make the dot keyboard-operable + screen-reader-announceable.
  if (dot) {
    dot.setAttribute('role', 'button');
    dot.setAttribute('tabindex', '0');
    dot.setAttribute('aria-expanded', 'false');
    dot.setAttribute('aria-label', 'Service health status');
    dot.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        dot.click();
      }
    });
  }
  const popover = document.getElementById('status-popover');
  if (!dot || !popover) return;
  let lastData = null;
  function setOverall(state) {
    dot.classList.remove('green', 'yellow', 'red');
    if (['green', 'yellow', 'red'].includes(state)) dot.classList.add(state);
  }
  function renderPopover(data) {
    if (!data || !data.services) {
      popover.innerHTML = '<div class="svc-detail">Status unknown — could not reach health endpoint.</div>';
      return;
    }
    const ALLOWED_STATES = new Set(['green', 'yellow', 'red', 'unknown']);
    popover.innerHTML = Object.entries(data.services).map(([name, info]) => {
      const nameSafe = esc((name[0] || '').toUpperCase() + name.slice(1));
      const stateSafe = ALLOWED_STATES.has(info.state) ? info.state : 'unknown';
      const detailSafe = esc(info.detail || '');
      return `
      <div class="svc-row">
        <span class="svc-name">${nameSafe}</span>
        <span class="svc-dot ${stateSafe}"></span>
        <span class="svc-detail">${detailSafe}</span>
      </div>`;
    }).join('');
  }
  async function poll() {
    try {
      const res = await fetch('/api/dashboard/health', { cache: 'no-store' });
      const data = await res.json();
      lastData = data;
      setOverall(data.overall);
      if (popover.classList.contains('open')) renderPopover(data);
    } catch (e) {
      setOverall('unknown'); lastData = null;
    }
  }
  dot.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); renderPopover(lastData); popover.classList.toggle('open'); });
  document.addEventListener('click', (e) => { if (popover.classList.contains('open') && !popover.contains(e.target)) popover.classList.remove('open'); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') popover.classList.remove('open'); });
  poll();
  setInterval(poll, 30000);
})();
