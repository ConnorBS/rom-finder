/**
 * ROM Finder — content script
 * Injected on: https://retroachievements.org/game/*
 *
 * Injects a floating panel that lets you:
 *   • Add the current game to your ROM Finder Wanted list
 *   • Search your enabled sources for ROM files
 */
(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // Parse page context
  // -------------------------------------------------------------------------

  const pathMatch = window.location.pathname.match(/\/game\/(\d+)/);
  if (!pathMatch) return;
  const gameId = parseInt(pathMatch[1], 10);

  function getGameTitle() {
    // RA uses several possible selectors over time
    const selectors = [
      'h3.longTitle',
      '.gameName',
      'h1.gameName',
      '#main h1',
      '#main h2',
      '#main h3',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) return cleanTitle(el.textContent.trim());
    }
    // Fall back to stripping " - RetroAchievements" from the page title
    const raw = document.title.replace(/\s*[-–|].*RetroAchievements.*$/i, '').trim();
    return cleanTitle(raw);
  }

  function cleanTitle(title) {
    // Strip "· RetroAchievements" or "- RetroAchievements" suffix
    title = title.replace(/\s*[·•\-]\s*RetroAchievements\s*$/i, '').trim();
    // Strip platform disambiguation in parens e.g. "(PlayStation 2)" or "(NES)"
    title = title.replace(
      /\s*\((?:PlayStation(?: \d+)?|PSP|PS\d|Nintendo\s+(?:64|DS|DSi|Switch)|SNES|NES|Famicom|Game\s*Boy(?:\s+(?:Advance|Color|Colour))?|GameCube|Wii(?:\s*U)?|Sega\s+(?:Genesis|Mega\s+Drive|CD|Saturn|32X|Dreamcast|Master\s+System)|Mega\s+Drive|Saturn|Dreamcast|Xbox(?:\s+(?:360|One|Series\s+[XS]))?|Atari\s+\d{4}|Game\s+Gear|TurboGrafx|PC\s*Engine|3DO|Jaguar|Lynx|Neo\s*Geo(?:\s+Pocket)?|WonderSwan|Virtual\s+Boy|Arcade|MSX|Amstrad|Apple\s+II|PC-\w+)\)/gi,
      ''
    ).trim();
    return title;
  }

  function getSystemInfo() {
    // The RA site nav lists every system (NES first) inside <nav>/<header>
    // elements that appear before the game content in the DOM.
    // Skip those and take the first system link that's in the page body.
    const allLinks = document.querySelectorAll('a[href*="/system/"]');
    for (const link of allLinks) {
      if (link.closest('nav, header, [role="navigation"], [role="menu"]')) continue;
      const name = link.textContent.trim();
      const m = link.href.match(/\/system\/(\d+)/);
      return { name, id: m ? parseInt(m[1], 10) : null };
    }
    return { name: '', id: null };
  }

  // RA uses client-side rendering — title/system elements may not exist yet.
  // Capture initial values (may be empty) and update the panel once they load.
  let gameTitle  = getGameTitle();
  let systemName = '';
  let systemId   = null;
  ({ name: systemName, id: systemId } = getSystemInfo());

  // -------------------------------------------------------------------------
  // Build the floating panel (all inline styles for isolation)
  // -------------------------------------------------------------------------

  const PANEL_ID = 'rf-panel-root';
  if (document.getElementById(PANEL_ID)) return; // already injected

  const root = document.createElement('div');
  root.id = PANEL_ID;
  applyStyles(root, {
    position: 'fixed',
    bottom: '20px',
    right: '20px',
    zIndex: '2147483647',
    fontFamily: 'system-ui, -apple-system, sans-serif',
    fontSize: '13px',
    lineHeight: '1.4',
  });

  // Toggle button (always visible)
  const toggleBtn = document.createElement('button');
  toggleBtn.textContent = '🎮 ROM Finder';
  applyStyles(toggleBtn, {
    display: 'block',
    marginLeft: 'auto',
    padding: '7px 14px',
    background: '#2563eb',
    color: '#fff',
    border: 'none',
    borderRadius: '20px',
    fontSize: '12px',
    fontWeight: '600',
    cursor: 'pointer',
    boxShadow: '0 2px 8px rgba(0,0,0,0.35)',
    transition: 'background 0.15s',
    whiteSpace: 'nowrap',
  });
  toggleBtn.addEventListener('mouseenter', () => toggleBtn.style.background = '#1d4ed8');
  toggleBtn.addEventListener('mouseleave', () => toggleBtn.style.background = '#2563eb');

  // Main panel (hidden until toggled)
  const panel = document.createElement('div');
  applyStyles(panel, {
    display: 'none',
    width: '340px',
    background: '#0f172a',
    border: '1px solid #1e293b',
    borderRadius: '10px',
    boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
    overflow: 'hidden',
    marginBottom: '8px',
    color: '#e2e8f0',
  });

  // Panel header
  const header = document.createElement('div');
  applyStyles(header, {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 14px',
    background: '#1e293b',
    borderBottom: '1px solid #334155',
  });
  const headerTitle = el('span', '🎮 ROM Finder', { fontWeight: '700', fontSize: '13px', color: '#f1f5f9' });
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  applyStyles(closeBtn, {
    background: 'none',
    border: 'none',
    color: '#64748b',
    fontSize: '18px',
    cursor: 'pointer',
    lineHeight: '1',
    padding: '0',
  });
  closeBtn.addEventListener('mouseenter', () => closeBtn.style.color = '#f1f5f9');
  closeBtn.addEventListener('mouseleave', () => closeBtn.style.color = '#64748b');
  header.appendChild(headerTitle);
  header.appendChild(closeBtn);

  // Panel body
  const body = document.createElement('div');
  applyStyles(body, { padding: '14px' });

  // Game info
  const gameInfoBlock = document.createElement('div');
  applyStyles(gameInfoBlock, { marginBottom: '12px' });
  const titleEl = el('div', gameTitle || `Game #${gameId}`, {
    fontWeight: '600',
    color: '#f1f5f9',
    marginBottom: '2px',
    wordBreak: 'break-word',
  });
  const systemEl = el('div', systemName || 'Unknown system', {
    fontSize: '11px',
    color: '#64748b',
  });
  const idEl = el('div', `RA Game ID: ${gameId}`, {
    fontSize: '11px',
    color: '#334155',
    marginTop: '2px',
  });
  gameInfoBlock.appendChild(titleEl);
  gameInfoBlock.appendChild(systemEl);
  gameInfoBlock.appendChild(idEl);

  // Add to Wanted button + status
  const addBtn = document.createElement('button');
  addBtn.textContent = '+ Add to Wanted';
  applyStyles(addBtn, {
    width: '100%',
    padding: '8px',
    background: '#2563eb',
    color: '#fff',
    border: 'none',
    borderRadius: '6px',
    fontWeight: '600',
    fontSize: '12px',
    cursor: 'pointer',
    marginBottom: '6px',
    transition: 'background 0.15s',
  });
  addBtn.addEventListener('mouseenter', () => { if (!addBtn.disabled) addBtn.style.background = '#1d4ed8'; });
  addBtn.addEventListener('mouseleave', () => { if (!addBtn.disabled) addBtn.style.background = '#2563eb'; });

  const addStatus = el('div', '', { fontSize: '11px', minHeight: '16px', marginBottom: '10px', color: '#94a3b8' });

  // Divider
  const divider = document.createElement('div');
  applyStyles(divider, { borderTop: '1px solid #1e293b', margin: '10px 0' });

  // Search section
  const searchLabel = el('div', 'Search Sources', {
    fontSize: '11px',
    fontWeight: '600',
    color: '#64748b',
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    marginBottom: '6px',
  });

  const searchRow = document.createElement('div');
  applyStyles(searchRow, { display: 'flex', gap: '6px', marginBottom: '8px' });

  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.placeholder = 'Search title...';
  searchInput.value = gameTitle || '';
  applyStyles(searchInput, {
    flex: '1',
    padding: '6px 9px',
    background: '#1e293b',
    border: '1px solid #334155',
    borderRadius: '5px',
    color: '#e2e8f0',
    fontSize: '12px',
    outline: 'none',
  });

  const searchBtn = document.createElement('button');
  searchBtn.textContent = 'Search';
  applyStyles(searchBtn, {
    padding: '6px 11px',
    background: '#334155',
    color: '#e2e8f0',
    border: 'none',
    borderRadius: '5px',
    fontSize: '12px',
    fontWeight: '600',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
    transition: 'background 0.15s',
  });
  searchBtn.addEventListener('mouseenter', () => searchBtn.style.background = '#475569');
  searchBtn.addEventListener('mouseleave', () => searchBtn.style.background = '#334155');

  searchRow.appendChild(searchInput);
  searchRow.appendChild(searchBtn);

  const resultsEl = document.createElement('div');
  applyStyles(resultsEl, { maxHeight: '260px', overflowY: 'auto' });

  // Assemble body
  body.appendChild(gameInfoBlock);
  body.appendChild(addBtn);
  body.appendChild(addStatus);
  body.appendChild(divider);
  body.appendChild(searchLabel);
  body.appendChild(searchRow);
  body.appendChild(resultsEl);

  panel.appendChild(header);
  panel.appendChild(body);
  root.appendChild(panel);
  root.appendChild(toggleBtn);
  document.body.appendChild(root);

  // RA is a SPA — the game title and system may not be in the DOM yet.
  // Poll for up to 4 seconds and update the panel once they appear.
  (function waitForContent(attempts) {
    if (attempts <= 0) return;
    const resolvedTitle = getGameTitle();
    const resolvedSys   = getSystemInfo();
    const gotTitle  = resolvedTitle && resolvedTitle !== `Game #${gameId}`;
    const gotSystem = resolvedSys.name;
    if (gotTitle || gotSystem) {
      if (gotTitle) {
        gameTitle = resolvedTitle;
        titleEl.textContent = resolvedTitle;
        if (!searchInput.value || searchInput.value === `Game #${gameId}`) {
          searchInput.value = resolvedTitle;
        }
      }
      if (gotSystem) {
        systemName = resolvedSys.name;
        systemId   = resolvedSys.id;
        systemEl.textContent = resolvedSys.name;
      }
      if (!gotTitle || !gotSystem) {
        setTimeout(() => waitForContent(attempts - 1), 200);
      }
    } else {
      setTimeout(() => waitForContent(attempts - 1), 200);
    }
  })(20);

  // -------------------------------------------------------------------------
  // Toggle logic
  // -------------------------------------------------------------------------

  let open = false;
  function setOpen(val) {
    open = val;
    panel.style.display = open ? 'block' : 'none';
    toggleBtn.textContent = open ? '✕ Close' : '🎮 ROM Finder';
  }
  toggleBtn.addEventListener('click', () => setOpen(!open));
  closeBtn.addEventListener('click', () => setOpen(false));

  // -------------------------------------------------------------------------
  // Add to Wanted
  // -------------------------------------------------------------------------

  addBtn.addEventListener('click', async () => {
    const items = await storageGet({ romFinderUrl: 'http://127.0.0.1:8080' });
    const base = items.romFinderUrl.replace(/\/$/, '');

    addBtn.disabled = true;
    addBtn.textContent = 'Adding…';
    addStatus.textContent = '';
    addStatus.style.color = '#94a3b8';

    try {
      const resp = await apiFetch(`${base}/api/wanted`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ra_game_id: gameId,
          game_title: gameTitle,
          system: systemName,
          system_id: systemId,
        }),
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      if (data.status === 'exists') {
        addBtn.textContent = '✓ Already in Wanted';
        addBtn.style.background = '#1e3a1e';
        addStatus.textContent = 'Already tracked in your Wanted list.';
        addStatus.style.color = '#4ade80';
      } else {
        addBtn.textContent = '✓ Added to Wanted';
        addBtn.style.background = '#14532d';
        addStatus.textContent = `Added "${data.game_title}" to your Wanted list.`;
        addStatus.style.color = '#4ade80';
      }
    } catch (err) {
      addBtn.disabled = false;
      addBtn.textContent = '+ Add to Wanted';
      addBtn.style.background = '#2563eb';
      addStatus.textContent = `Error: ${err.message}. Is ROM Finder running?`;
      addStatus.style.color = '#f87171';
    }
  });

  // -------------------------------------------------------------------------
  // Search sources
  // -------------------------------------------------------------------------

  async function runSearch() {
    const q = searchInput.value.trim();
    if (!q) return;

    const items = await storageGet({ romFinderUrl: 'http://127.0.0.1:8080' });
    const base = items.romFinderUrl.replace(/\/$/, '');

    searchBtn.disabled = true;
    searchBtn.textContent = '…';
    resultsEl.innerHTML = '';
    showResultsMsg('Searching enabled sources…', '#94a3b8');

    const params = new URLSearchParams({ q });
    const sysForSearch = systemName || '';
    if (sysForSearch) params.set('system', sysForSearch);

    try {
      const resp = await apiFetch(`${base}/api/search?${params}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const results = await resp.json();

      resultsEl.innerHTML = '';
      if (!results.length) {
        showResultsMsg('No results found. Try enabling more sources in Settings.', '#64748b');
        return;
      }

      for (const item of results) {
        resultsEl.appendChild(buildResultRow(item, base));
      }
    } catch (err) {
      resultsEl.innerHTML = '';
      showResultsMsg(`Error: ${err.message}`, '#f87171');
    } finally {
      searchBtn.disabled = false;
      searchBtn.textContent = 'Search';
    }
  }

  searchBtn.addEventListener('click', runSearch);
  searchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });

  function buildResultRow(item, base) {
    const row = document.createElement('div');
    applyStyles(row, {
      padding: '8px 0',
      borderBottom: '1px solid #1e293b',
    });

    const titleLine = document.createElement('div');
    applyStyles(titleLine, { display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px' });

    const titleSpan = el('span', item.title || item.identifier, {
      color: '#e2e8f0',
      fontWeight: '500',
      flex: '1',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
    });

    const sourceBadge = el('span', item._source_name || item.source_id || '', {
      fontSize: '10px',
      background: '#1e293b',
      color: '#64748b',
      padding: '1px 5px',
      borderRadius: '3px',
      flexShrink: '0',
    });

    titleLine.appendChild(titleSpan);
    titleLine.appendChild(sourceBadge);

    // Second line: region + link
    const metaLine = document.createElement('div');
    applyStyles(metaLine, { display: 'flex', alignItems: 'center', gap: '6px' });

    if (item.region) {
      metaLine.appendChild(el('span', item.region, { color: '#94a3b8', fontSize: '11px' }));
    }

    // External link
    let href = '';
    if (item.source_id === 'archive_org') href = `https://archive.org/details/${item.identifier}`;
    else if (item.source_id === 'vimm') href = item.url || `https://vimm.net/vault/${item.identifier}/`;

    if (href) {
      const link = document.createElement('a');
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = `${item.source_id === 'vimm' ? 'vimm.net' : 'archive.org'} ↗`;
      applyStyles(link, {
        fontSize: '11px',
        color: '#3b82f6',
        textDecoration: 'none',
      });
      link.addEventListener('mouseenter', () => link.style.textDecoration = 'underline');
      link.addEventListener('mouseleave', () => link.style.textDecoration = 'none');
      metaLine.appendChild(link);
    }

    // "Open in ROM Finder" link
    const rfLink = document.createElement('a');
    rfLink.href = `${base}/wanted`;
    rfLink.target = '_blank';
    rfLink.rel = 'noopener noreferrer';
    rfLink.textContent = 'ROM Finder ↗';
    applyStyles(rfLink, {
      fontSize: '11px',
      color: '#2563eb',
      textDecoration: 'none',
      marginLeft: 'auto',
    });
    rfLink.addEventListener('mouseenter', () => rfLink.style.textDecoration = 'underline');
    rfLink.addEventListener('mouseleave', () => rfLink.style.textDecoration = 'none');
    metaLine.appendChild(rfLink);

    row.appendChild(titleLine);
    row.appendChild(metaLine);
    return row;
  }

  function showResultsMsg(msg, color) {
    resultsEl.innerHTML = '';
    const p = el('p', msg, { color, fontSize: '11px', padding: '6px 0', margin: '0' });
    resultsEl.appendChild(p);
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  function el(tag, text, styles) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (styles) applyStyles(node, styles);
    return node;
  }

  function applyStyles(node, styles) {
    Object.assign(node.style, styles);
  }

  function storageGet(defaults) {
    return new Promise((resolve) => {
      chrome.storage.sync.get(defaults, resolve);
    });
  }

  /**
   * Proxy fetch through the background service worker to avoid mixed-content
   * blocks when the RA page is HTTPS but the local ROM Finder server is HTTP.
   */
  function apiFetch(url, options = {}) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: 'API_FETCH', url, options }, (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (resp.error) {
          reject(new Error(resp.error));
          return;
        }
        resolve({
          ok: resp.ok,
          status: resp.status,
          json: () => Promise.resolve(JSON.parse(resp.text)),
        });
      });
    });
  }
})();
