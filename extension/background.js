// Background worker. Fetches images through chrome.debugger because the CDNs block page-level fetches.

const attached = new Set();

async function attachIfNeeded(tabId) {
  if (attached.has(tabId)) return;
  await new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, "1.3", () => {
      if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
      else resolve();
    });
  });
  attached.add(tabId);
}

async function detachIfNeeded(tabId) {
  if (!attached.has(tabId)) return;
  try {
    await new Promise((resolve) => chrome.debugger.detach({ tabId }, resolve));
  } catch (e) { /* ignore */ }
  attached.delete(tabId);
}

function cdpCmd(tabId, method, params = {}) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
      else resolve(result);
    });
  });
}

// base64 to Uint8Array
function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Uses Network.loadNetworkResource, which is not subject to CORS.
async function fetchImageViaDebugger(tabId, url) {
  await attachIfNeeded(tabId);
  const { frameTree } = await cdpCmd(tabId, "Page.getFrameTree");
  const frameId = frameTree.frame.id;
  const res = await cdpCmd(tabId, "Network.loadNetworkResource", {
    frameId,
    url,
    options: { disableCache: false, includeCredentials: true },
  });
  if (!res.resource || !res.resource.success) {
    return { ok: false, error: `loadNetworkResource failed: status=${res.resource?.httpStatusCode || "?"} netError=${res.resource?.netErrorName || res.resource?.netError || "?"}` };
  }
  const status = res.resource.httpStatusCode || 0;
  if (status && status >= 400) {
    return { ok: false, error: `HTTP ${status} from CDN` };
  }
  const streamHandle = res.resource.stream;
  let accum = "";
  while (true) {
    const chunk = await cdpCmd(tabId, "IO.read", { handle: streamHandle });
    if (chunk.data) accum += chunk.base64Encoded ? chunk.data : btoa(chunk.data);
    if (chunk.eof) break;
  }
  await cdpCmd(tabId, "IO.close", { handle: streamHandle });
  const bytes = b64ToBytes(accum);
  if (bytes.length === 0) {
    return { ok: false, error: `empty body (status=${status}), the CDN probably refused the request` };
  }
  // Prefer the real Content-Type from response headers; fall back to image/webp.
  const headers = res.resource.httpHeaders || {};
  const ct = headers["Content-Type"] || headers["content-type"] || "image/webp";
  return { ok: true, buf: Array.from(bytes), ct };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab && sender.tab.id;

  if (msg.type === "fetchImage") {
    fetchImageViaDebugger(tabId, msg.url)
      .then(sendResponse)
      .catch(e => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }

  if (msg.type === "detach") {
    detachIfNeeded(tabId).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "translate") {
    const apiUrl = msg.apiUrl || "http://127.0.0.1:8002/translate-cached";
    const blob = new Blob([new Uint8Array(msg.buf)], { type: msg.ct });
    const fd = new FormData();
    fd.append("image", blob, msg.filename || "panel.webp");
    fd.append("config", JSON.stringify(msg.config));
    (async () => {
      try {
        const resp = await fetch(apiUrl, { method: "POST", body: fd });
        if (!resp.ok) {
          const text = await resp.text();
          sendResponse({ ok: false, error: `HTTP ${resp.status}: ${text.slice(0, 200)}` });
          return;
        }
        const ct = resp.headers.get("content-type") || "";
        // /translate-cached returns image/png directly (with X-MT-* headers)
        if (ct.startsWith("image/")) {
          const buf = new Uint8Array(await resp.arrayBuffer());
          const cacheStatus = resp.headers.get("x-mt-cache") || "miss";
          const translator = resp.headers.get("x-mt-translator") || "?";
          const tried = resp.headers.get("x-mt-tried") || "";
          sendResponse({ ok: true, buf: Array.from(buf), ct, cache: cacheStatus, translator, tried });
          return;
        }
        // Fallback: parse stream protocol if hitting :8000 direct
        const reader = resp.body.getReader();
        let buf = new Uint8Array(0);
        let resultBytes = null;
        let err = null;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const newBuf = new Uint8Array(buf.length + value.length);
          newBuf.set(buf, 0); newBuf.set(value, buf.length);
          buf = newBuf;
          while (buf.length >= 5) {
            const status = buf[0];
            const size = (buf[1] << 24) | (buf[2] << 16) | (buf[3] << 8) | buf[4];
            if (buf.length < 5 + size) break;
            const payload = buf.slice(5, 5 + size);
            buf = buf.slice(5 + size);
            if (status === 0) resultBytes = payload;
            else if (status === 2) err = new TextDecoder().decode(payload);
          }
          if (resultBytes !== null || err !== null) break;
        }
        if (err) sendResponse({ ok: false, error: err });
        else if (resultBytes) sendResponse({ ok: true, buf: Array.from(resultBytes), ct: "image/png" });
        else sendResponse({ ok: false, error: "no result in stream" });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }
});

// Detach if user closes tab
chrome.tabs.onRemoved.addListener((tabId) => attached.delete(tabId));

chrome.action.onClicked.addListener(async (tab) => {
  await chrome.tabs.sendMessage(tab.id, { type: "translatePage" });
});

// Context menu: right-click an image to translate it
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "mt-translate-image",
    title: "MT Translate this image",
    contexts: ["image"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "mt-translate-image" && info.srcUrl && tab) {
    await chrome.tabs.sendMessage(tab.id, { type: "translateOne", url: info.srcUrl });
  }
});

// Relay badge updates from content script (content scripts can't call chrome.action directly)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "setBadge" && sender.tab) {
    const tabId = sender.tab.id;
    chrome.action.setBadgeText({ tabId, text: msg.text || "" });
    if (msg.color) chrome.action.setBadgeBackgroundColor({ tabId, color: msg.color });
    sendResponse({ ok: true });
    return false;
  }
});
