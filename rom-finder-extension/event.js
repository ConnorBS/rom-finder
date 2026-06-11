/**
 * ROM Finder — event-page content script
 * Injected on: https://retroachievements.org/event/*
 *
 * Floating panel to import a whole RA event as ROM Finder GOALS in one request:
 * every (non-placeholder) achievement becomes an achievement goal, recorded as an
 * auto-sync event that re-checks nightly for newly-added achievements. Self-contained.
 */
(function () {
  'use strict';

  const m = window.location.pathname.match(/\/event\/(\d+)/);
  if (!m) return;
  const eventId = parseInt(m[1], 10);

  function scrapeTitle() {
    for (const sel of ['h1', '#main h1', '#main h2']) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) return el.textContent.trim();
    }
    return document.title.replace(/\s*[-–|·•].*RetroAchievements.*$/i, '').trim();
  }

  let eventTitle = scrapeTitle();

  const PANEL_ID = 'rf-event-panel-root';
  const stale = document.getElementById(PANEL_ID);
  if (stale) stale.remove();

  const root = document.createElement('div');
  root.id = PANEL_ID;
  applyStyles(root, {
    position: 'fixed', bottom: '20px', right: '20px', zIndex: '2147483647',
    fontFamily: 'system-ui, -apple-system, sans-serif', fontSize: '13px', lineHeight: '1.4',
  });

  const toggleBtn = document.createElement('button');
  toggleBtn.textContent = '🎯 Import Event';
  applyStyles(toggleBtn, {
    display: 'block', marginLeft: 'auto', padding: '7px 14px', background: '#db2777',
    color: '#fff', border: 'none', borderRadius: '20px', fontSize: '12px', fontWeight: '600',
    cursor: 'pointer', boxShadow: '0 2px 8px rgba(0,0,0,0.35)', whiteSpace: 'nowrap',
  });
  toggleBtn.addEventListener('mouseenter', () => toggleBtn.style.background = '#be185d');
  toggleBtn.addEventListener('mouseleave', () => toggleBtn.style.background = '#db2777');

  const panel = document.createElement('div');
  applyStyles(panel, {
    display: 'none', width: '320px', background: '#0f172a', border: '1px solid #1e293b',
    borderRadius: '10px', boxShadow: '0 8px 32px rgba(0,0,0,0.6)', overflow: 'hidden',
    marginBottom: '8px', color: '#e2e8f0',
  });

  const header = document.createElement('div');
  applyStyles(header, {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '10px 14px', background: '#1e293b', borderBottom: '1px solid #334155',
  });
  header.appendChild(el('span', '🎯 ROM Finder — Import Event', { fontWeight: '700', fontSize: '13px', color: '#f1f5f9' }));
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  applyStyles(closeBtn, { background: 'none', border: 'none', color: '#64748b', fontSize: '18px', cursor: 'pointer', lineHeight: '1', padding: '0' });
  closeBtn.addEventListener('click', () => setOpen(false));
  header.appendChild(closeBtn);

  const bodyEl = document.createElement('div');
  applyStyles(bodyEl, { padding: '14px' });

  const titleEl = el('div', eventTitle || `Event #${eventId}`, { fontWeight: '600', color: '#f9a8d4', marginBottom: '2px', wordBreak: 'break-word' });
  const idEl = el('div', `Event ID: ${eventId}`, { fontSize: '11px', color: '#334155', marginBottom: '12px' });
  bodyEl.appendChild(titleEl);
  bodyEl.appendChild(idEl);

  const nameInput = inputField('Event name (optional)', 'text', 'defaults to the RA title');
  const eventList = document.createElement('datalist');
  eventList.id = 'rf-import-events';
  nameInput.input.setAttribute('list', 'rf-import-events');
  nameInput.input.setAttribute('autocomplete', 'off');
  const dateInput = inputField('Deadline (optional)', 'date', '');

  const incWrap = document.createElement('label');
  applyStyles(incWrap, { display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: '#94a3b8', margin: '2px 0 10px' });
  const incBox = document.createElement('input');
  incBox.type = 'checkbox';
  incBox.checked = true;
  incWrap.appendChild(incBox);
  incWrap.appendChild(document.createTextNode("Include ones I've already earned"));

  const importBtn = document.createElement('button');
  importBtn.textContent = '🎯 Import all achievements';
  applyStyles(importBtn, {
    width: '100%', padding: '8px', background: '#db2777', color: '#fff', border: 'none',
    borderRadius: '6px', fontWeight: '600', fontSize: '12px', cursor: 'pointer',
  });
  importBtn.addEventListener('mouseenter', () => { if (!importBtn.disabled) importBtn.style.background = '#be185d'; });
  importBtn.addEventListener('mouseleave', () => { if (!importBtn.disabled) importBtn.style.background = '#db2777'; });

  const statusEl = el('div', '', { fontSize: '11px', minHeight: '16px', marginTop: '8px', color: '#94a3b8' });

  bodyEl.appendChild(nameInput.wrap);
  bodyEl.appendChild(eventList);
  bodyEl.appendChild(dateInput.wrap);
  bodyEl.appendChild(incWrap);
  bodyEl.appendChild(importBtn);
  bodyEl.appendChild(statusEl);

  panel.appendChild(header);
  panel.appendChild(bodyEl);
  root.appendChild(panel);
  root.appendChild(toggleBtn);
  document.body.appendChild(root);

  let open = false;
  function setOpen(v) { open = v; panel.style.display = open ? 'block' : 'none'; toggleBtn.textContent = open ? '✕ Close' : '🎯 Import Event'; }
  toggleBtn.addEventListener('click', () => setOpen(!open));

  // Poll for late-rendered SPA title.
  (function waitForTitle(attempts) {
    if (attempts <= 0) return;
    const t = scrapeTitle();
    if (t && t !== eventTitle && !/RetroAchievements/i.test(t)) { eventTitle = t; titleEl.textContent = t; }
    setTimeout(() => waitForTitle(attempts - 1), 250);
  })(16);

  // Populate the event-name datalist from existing goals' events.
  (async function loadEvents() {
    try {
      const base = await baseUrl();
      const resp = await apiFetch(`${base}/api/events`);
      if (!resp.ok) return;
      for (const name of ((await resp.json()).events || [])) {
        const opt = document.createElement('option');
        opt.value = name;
        eventList.appendChild(opt);
      }
    } catch (_) { /* offline — free text still works */ }
  })();

  importBtn.addEventListener('click', async () => {
    importBtn.disabled = true;
    importBtn.textContent = 'Importing…';
    statusEl.textContent = '';
    try {
      const base = await baseUrl();
      const resp = await apiFetch(`${base}/api/import-event`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ra_game_id: eventId,
          event_name: nameInput.input.value.trim(),
          deadline: dateInput.input.value,
          include_completed: incBox.checked,
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (data.status === 'error') throw new Error(data.error || 'rejected');
      importBtn.textContent = '✓ Imported';
      importBtn.style.background = '#14532d';
      statusEl.textContent = `Imported ${data.created} of ${data.total_achievements} for “${data.event}” — `
        + `${data.skipped_existing} already tracked, ${data.skipped_done} already done, `
        + `${data.skipped_placeholder} unpublished/placeholder skipped (nightly sync adds upcoming weeks).`;
      statusEl.style.color = '#4ade80';
    } catch (err) {
      importBtn.disabled = false;
      importBtn.textContent = '🎯 Import all achievements';
      importBtn.style.background = '#db2777';
      statusEl.textContent = `Error: ${err.message}. Is ROM Finder running?`;
      statusEl.style.color = '#f87171';
    }
  });

  // --- helpers ---------------------------------------------------------------

  function inputField(label, type, placeholder) {
    const wrap = document.createElement('div');
    applyStyles(wrap, { marginBottom: '8px' });
    wrap.appendChild(el('div', label, { fontSize: '11px', color: '#64748b', marginBottom: '3px' }));
    const input = document.createElement('input');
    input.type = type;
    if (placeholder) input.placeholder = placeholder;
    applyStyles(input, {
      width: '100%', padding: '6px 9px', background: '#1e293b', border: '1px solid #334155',
      borderRadius: '5px', color: '#e2e8f0', fontSize: '12px', outline: 'none', boxSizing: 'border-box',
    });
    wrap.appendChild(input);
    return { wrap, input };
  }
  function el(tag, text, styles) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (styles) applyStyles(node, styles);
    return node;
  }
  function applyStyles(node, styles) { Object.assign(node.style, styles); }
  function baseUrl() {
    return new Promise((resolve) => {
      chrome.storage.sync.get({ romFinderUrl: 'http://127.0.0.1:8080' }, (items) => {
        resolve(items.romFinderUrl.replace(/\/$/, ''));
      });
    });
  }
  function apiFetch(url, options = {}) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: 'API_FETCH', url, options }, (resp) => {
        if (chrome.runtime.lastError) { reject(new Error(chrome.runtime.lastError.message)); return; }
        if (resp.error) { reject(new Error(resp.error)); return; }
        resolve({ ok: resp.ok, status: resp.status, json: () => Promise.resolve(JSON.parse(resp.text)) });
      });
    });
  }
})();
