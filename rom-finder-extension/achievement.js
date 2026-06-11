/**
 * ROM Finder — achievement-page content script
 * Injected on: https://retroachievements.org/achievement/*
 *
 * Floating panel to add the achievement you're viewing as a ROM Finder GOAL with
 * an optional deadline + event. The goal auto-completes once you unlock the
 * achievement in HARDCORE (the server reads your local RA mirror). Self-contained
 * (its own helpers) so it doesn't share scope with the game-page content.js.
 */
(function () {
  'use strict';

  const m = window.location.pathname.match(/\/achievement\/(\d+)/);
  if (!m) return;
  const achievementId = parseInt(m[1], 10);

  // --- scrape page context (RA is a SPA; values may arrive late) -------------

  function scrapeAchievementTitle() {
    for (const sel of ['h1', '#main h1', '#main h2', '.achievementtitle']) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) return el.textContent.trim();
    }
    // Fallback: page title is usually "{Achievement} · {Game} · RetroAchievements".
    const raw = document.title.replace(/\s*[-–|·•].*RetroAchievements.*$/i, '').trim();
    return raw.split(/\s*[·•|]\s*/)[0].trim();
  }

  function scrapeGame() {
    for (const link of document.querySelectorAll('a[href*="/game/"]')) {
      if (link.closest('nav, header, [role="navigation"], [role="menu"]')) continue;
      const mm = link.href.match(/\/game\/(\d+)/);
      if (mm) return { id: parseInt(mm[1], 10), title: link.textContent.trim() };
    }
    return { id: null, title: '' };
  }

  function scrapeSystemId() {
    for (const link of document.querySelectorAll('a[href*="/system/"]')) {
      if (link.closest('nav, header, [role="navigation"], [role="menu"]')) continue;
      const mm = link.href.match(/\/system\/(\d+)/);
      if (mm) return parseInt(mm[1], 10);
    }
    return null;
  }

  let achTitle = scrapeAchievementTitle();
  let game = scrapeGame();
  let systemId = scrapeSystemId();

  // --- panel -----------------------------------------------------------------

  const PANEL_ID = 'rf-ach-panel-root';
  const stale = document.getElementById(PANEL_ID);
  if (stale) stale.remove();

  const root = document.createElement('div');
  root.id = PANEL_ID;
  applyStyles(root, {
    position: 'fixed', bottom: '20px', right: '20px', zIndex: '2147483647',
    fontFamily: 'system-ui, -apple-system, sans-serif', fontSize: '13px', lineHeight: '1.4',
  });

  const toggleBtn = document.createElement('button');
  toggleBtn.textContent = '🎯 Set Goal';
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
  header.appendChild(el('span', '🎯 ROM Finder — Goal', { fontWeight: '700', fontSize: '13px', color: '#f1f5f9' }));
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  applyStyles(closeBtn, { background: 'none', border: 'none', color: '#64748b', fontSize: '18px', cursor: 'pointer', lineHeight: '1', padding: '0' });
  closeBtn.addEventListener('click', () => setOpen(false));
  header.appendChild(closeBtn);

  const bodyEl = document.createElement('div');
  applyStyles(bodyEl, { padding: '14px' });

  const achTitleEl = el('div', achTitle || `Achievement #${achievementId}`, { fontWeight: '600', color: '#f9a8d4', marginBottom: '2px', wordBreak: 'break-word' });
  const gameEl = el('div', game.title || 'Loading game…', { fontSize: '11px', color: '#94a3b8' });
  const idEl = el('div', `Achievement ID: ${achievementId}`, { fontSize: '11px', color: '#334155', marginTop: '2px', marginBottom: '12px' });

  const eventInput = inputField('Event (optional)', 'text', 'e.g. Collectathon');
  // Datalist of existing event names so an achievement goal lands in the SAME event
  // group as the game's master/beat goals (avoids near-miss typos splitting a group).
  const eventList = document.createElement('datalist');
  eventList.id = 'rf-ach-events';
  eventInput.input.setAttribute('list', 'rf-ach-events');
  eventInput.input.setAttribute('autocomplete', 'off');
  const dateInput = inputField('Deadline (optional)', 'date', '');

  const addBtn = document.createElement('button');
  addBtn.textContent = '🎯 Add as Goal';
  applyStyles(addBtn, {
    width: '100%', padding: '8px', background: '#db2777', color: '#fff', border: 'none',
    borderRadius: '6px', fontWeight: '600', fontSize: '12px', cursor: 'pointer', marginTop: '4px',
  });
  addBtn.addEventListener('mouseenter', () => { if (!addBtn.disabled) addBtn.style.background = '#be185d'; });
  addBtn.addEventListener('mouseleave', () => { if (!addBtn.disabled) addBtn.style.background = '#db2777'; });

  const statusEl = el('div', '', { fontSize: '11px', minHeight: '16px', marginTop: '8px', color: '#94a3b8' });

  bodyEl.appendChild(achTitleEl);
  bodyEl.appendChild(gameEl);
  bodyEl.appendChild(idEl);
  bodyEl.appendChild(eventInput.wrap);
  bodyEl.appendChild(eventList);
  bodyEl.appendChild(dateInput.wrap);
  bodyEl.appendChild(addBtn);
  bodyEl.appendChild(statusEl);

  panel.appendChild(header);
  panel.appendChild(bodyEl);
  root.appendChild(panel);
  root.appendChild(toggleBtn);
  document.body.appendChild(root);

  let open = false;
  function setOpen(v) { open = v; panel.style.display = open ? 'block' : 'none'; toggleBtn.textContent = open ? '✕ Close' : '🎯 Set Goal'; }
  toggleBtn.addEventListener('click', () => setOpen(!open));

  // Poll for late-rendered SPA content.
  (function waitForContent(attempts) {
    if (attempts <= 0) return;
    const t = scrapeAchievementTitle(), g = scrapeGame(), s = scrapeSystemId();
    if (t && t !== achTitle) { achTitle = t; achTitleEl.textContent = t; }
    if (g.id) { game = g; gameEl.textContent = g.title || `Game #${g.id}`; }
    if (s) systemId = s;
    if (!achTitle || !game.id) setTimeout(() => waitForContent(attempts - 1), 200);
  })(20);

  // Populate the event datalist from existing goals so the user can reuse an event name.
  (async function loadEvents() {
    try {
      const base = await baseUrl();
      const resp = await apiFetch(`${base}/api/events`);
      if (!resp.ok) return;
      const data = await resp.json();
      for (const name of (data.events || [])) {
        const opt = document.createElement('option');
        opt.value = name;
        eventList.appendChild(opt);
      }
    } catch (_) { /* server unreachable — free-text entry still works */ }
  })();

  // Pre-check: already a goal?
  (async function checkGoalStatus() {
    try {
      const base = await baseUrl();
      const resp = await apiFetch(`${base}/api/goal-status?ra_game_id=${game.id || 0}&achievement_id=${achievementId}`);
      if (!resp.ok) return;
      const st = await resp.json();
      if (st.completed) { markDone('✓ Goal already completed'); }
      else if (st.goal) { markDone('★ Already a goal'); }
    } catch (_) { /* server unreachable — leave the button active */ }
  })();

  function markDone(label) {
    addBtn.disabled = true;
    addBtn.textContent = label;
    addBtn.style.background = '#14532d';
    addBtn.style.cursor = 'default';
  }

  addBtn.addEventListener('click', async () => {
    if (!game.id) { statusEl.textContent = 'Still reading the game — try again in a moment.'; statusEl.style.color = '#fbbf24'; return; }
    addBtn.disabled = true;
    addBtn.textContent = 'Adding…';
    statusEl.textContent = '';
    try {
      const base = await baseUrl();
      const resp = await apiFetch(`${base}/api/goal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ra_game_id: game.id,
          game_title: game.title || `Game #${game.id}`,
          system: '',           // let the server resolve from system_id (canonical_system)
          system_id: systemId,
          objective: 'achievement',
          achievement_id: achievementId,
          achievement_title: achTitle || `Achievement #${achievementId}`,
          event_name: eventInput.input.value.trim(),
          deadline: dateInput.input.value,   // YYYY-MM-DD or ""
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (data.status === 'error') throw new Error(data.error || 'rejected');
      markDone(data.status === 'exists' ? '★ Already a goal' : '✓ Goal added');
      statusEl.textContent = data.status === 'exists'
        ? 'This achievement is already a goal.'
        : 'Tracking — it completes when you unlock it in hardcore.';
      statusEl.style.color = '#4ade80';
    } catch (err) {
      addBtn.disabled = false;
      addBtn.textContent = '🎯 Add as Goal';
      addBtn.style.background = '#db2777';
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
