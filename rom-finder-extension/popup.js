chrome.storage.sync.get({ romFinderUrl: 'http://127.0.0.1:8080' }, async (items) => {
  const base = items.romFinderUrl.replace(/\/$/, '');

  document.getElementById('url-display').textContent = base;

  document.getElementById('open-wanted').addEventListener('click', () => {
    chrome.tabs.create({ url: `${base}/wanted` });
  });

  document.getElementById('options-link').addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });

  const dot  = document.getElementById('dot');
  const text = document.getElementById('status-text');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);

  try {
    const resp = await fetch(`${base}/api/ping`, { signal: controller.signal });
    clearTimeout(timer);
    if (resp.ok) {
      dot.className  = 'dot ok';
      text.textContent = 'Running';
    } else {
      dot.className  = 'dot err';
      text.textContent = `HTTP ${resp.status}`;
    }
  } catch {
    clearTimeout(timer);
    dot.className  = 'dot err';
    text.textContent = 'Not running';
  }
});
