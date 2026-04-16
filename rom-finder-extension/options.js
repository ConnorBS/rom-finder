const urlInput = document.getElementById('url');
const saveBtn  = document.getElementById('save');
const testBtn  = document.getElementById('test');
const statusEl = document.getElementById('status');

// Restore saved value
chrome.storage.sync.get({ romFinderUrl: 'http://127.0.0.1:8080' }, (items) => {
  urlInput.value = items.romFinderUrl;
});

saveBtn.addEventListener('click', () => {
  const url = urlInput.value.trim().replace(/\/$/, '');
  if (!url) {
    show('Enter a URL.', 'err');
    return;
  }
  chrome.storage.sync.set({ romFinderUrl: url }, () => {
    show('Saved.', 'ok');
  });
});

testBtn.addEventListener('click', async () => {
  const url = urlInput.value.trim().replace(/\/$/, '');
  if (!url) { show('Enter a URL first.', 'err'); return; }

  show('Connecting...', 'checking');

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);

  try {
    const resp = await fetch(url + '/api/ping', { signal: controller.signal });
    clearTimeout(timer);
    if (resp.ok) {
      show('Connected — ROM Finder is running.', 'ok');
    } else {
      show(`Server responded with HTTP ${resp.status}.`, 'err');
    }
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      show('Connection timed out. Is ROM Finder running?', 'err');
    } else {
      show('Could not connect. Is ROM Finder running?', 'err');
    }
  }
});

function show(msg, cls) {
  statusEl.textContent = msg;
  statusEl.className = cls;
}
