/**
 * ROM Finder — background service worker
 *
 * Content scripts injected into HTTPS pages can't make HTTP fetch calls
 * (mixed-content block). All API requests are proxied through here instead,
 * since service workers aren't subject to that restriction.
 *
 * Also handles SPA navigation on RA: when the URL changes client-side to a
 * game page, Chrome won't fire the manifest content_script injection, so we
 * re-inject manually via webNavigation.onHistoryStateUpdated.
 */

const GAME_PATH_RE = /\/game\/\d+/;

chrome.webNavigation.onHistoryStateUpdated.addListener(
  (details) => {
    if (!GAME_PATH_RE.test(details.url)) return;
    chrome.scripting.executeScript({
      target: { tabId: details.tabId, frameId: details.frameId },
      files: ['content.js'],
    });
  },
  { url: [{ hostEquals: 'retroachievements.org' }] }
);

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== 'API_FETCH') return false;

  const { url, options } = message;

  fetch(url, options)
    .then(async (resp) => {
      const text = await resp.text();
      sendResponse({ ok: resp.ok, status: resp.status, text });
    })
    .catch((err) => {
      sendResponse({ ok: false, status: 0, text: '', error: err.message });
    });

  // Return true to keep the message channel open for the async response
  return true;
});
