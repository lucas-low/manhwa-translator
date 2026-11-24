// Content script: floating button, status pill, retry, badge, context menu.
(() => {
  const API = "http://127.0.0.1:8002/translate-cached";
  const MIN_PIXELS = 500_000;
  const CONCURRENCY = 5;

  // Only the LLM prompt differs per language. All three use OCR 48px, which detected
  // Korean, Japanese and Chinese in testing. The upstream Config enum accepts
  // {32px, 48px, 48px_ctc, mocr}. ocr_large exists as a model file but is not registered.
  const LANG_PRESETS = {
    ko: { label: "한 KO", ocr: "48px" },
    ja: { label: "あ JA", ocr: "48px" },
    zh: { label: "中 ZH", ocr: "48px" },
  };
  const LANG_STORAGE_KEY = "mt-source-lang";

  function getLang() {
    try { const v = localStorage.getItem(LANG_STORAGE_KEY); if (v && LANG_PRESETS[v]) return v; } catch {}
    return "ko";  // default
  }
  function setLang(v) {
    if (!LANG_PRESETS[v]) return;
    try { localStorage.setItem(LANG_STORAGE_KEY, v); } catch {}
  }

  function buildConfig(lang) {
    const p = LANG_PRESETS[lang] || LANG_PRESETS.ko;
    return {
      translator: { translator: "custom_openai", target_lang: "ENG", no_text_lang_skip: false },
      detector:   { detector: "default", detection_size: 1536, text_threshold: 0.5, box_threshold: 0.7, unclip_ratio: 2.3 },
      ocr:        { ocr: p.ocr, min_text_length: 0, ignore_bubble: 0 },
      inpainter:  { inpainter: "lama_mpe", inpainting_size: 1024, inpainting_precision: "bf16" },
      render:     { renderer: "manga2eng", alignment: "auto", font_size_minimum: -1, rtl: true },
      kernel_size: 3, mask_dilation_offset: 20,
    };
  }

  // Track failed images so the Retry button can target them
  const failedImages = new Set();
  let running = false;

  function makeUI() {
    if (document.getElementById("mt-btn")) return;
    const btn = document.createElement("button");
    btn.id = "mt-btn";
    btn.textContent = "Translate Chapter";
    Object.assign(btn.style, {
      position: "fixed", right: "20px", bottom: "20px", zIndex: 2147483647,
      background: "#4af", color: "#fff", border: "0", padding: "12px 18px",
      borderRadius: "999px", fontSize: "14px", fontWeight: "600",
      boxShadow: "0 6px 20px rgba(0,0,0,0.4)", cursor: "pointer",
      fontFamily: "-apple-system, sans-serif",
    });
    btn.onclick = () => {
      if (!ctxValid()) {
        status("Extension was reloaded. Refresh the page (⌘R) to use it");
        return;
      }
      runOnImages(collectImages());
    };
    document.body.appendChild(btn);

    // Language picker (segmented control, persisted to localStorage)
    const langBar = document.createElement("div");
    langBar.id = "mt-lang";
    Object.assign(langBar.style, {
      position: "fixed", right: "20px", bottom: "120px", zIndex: 2147483647,
      display: "flex", gap: "4px", background: "rgba(0,0,0,0.85)",
      padding: "4px", borderRadius: "999px",
      boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
      fontFamily: "-apple-system, sans-serif",
    });
    const current = getLang();
    for (const [k, v] of Object.entries(LANG_PRESETS)) {
      const seg = document.createElement("button");
      seg.dataset.lang = k;
      seg.textContent = v.label;
      Object.assign(seg.style, {
        background: k === current ? "#4af" : "transparent",
        color: "#fff", border: "0", padding: "6px 12px",
        borderRadius: "999px", fontSize: "12px", fontWeight: "600",
        cursor: "pointer",
      });
      seg.onclick = () => {
        setLang(k);
        for (const s of langBar.querySelectorAll("button")) {
          s.style.background = s.dataset.lang === k ? "#4af" : "transparent";
        }
        status(`source language: ${v.label}`);
      };
      langBar.appendChild(seg);
    }
    document.body.appendChild(langBar);

    const retry = document.createElement("button");
    retry.id = "mt-retry";
    retry.textContent = "Retry Failed";
    Object.assign(retry.style, {
      position: "fixed", right: "200px", bottom: "20px", zIndex: 2147483647,
      background: "#e55", color: "#fff", border: "0", padding: "12px 18px",
      borderRadius: "999px", fontSize: "14px", fontWeight: "600",
      boxShadow: "0 6px 20px rgba(0,0,0,0.4)", cursor: "pointer",
      fontFamily: "-apple-system, sans-serif", display: "none",
    });
    retry.onclick = () => {
      const targets = [...failedImages].filter(img => document.body.contains(img));
      if (!targets.length) return;
      // Clear done marker so they get re-tried
      targets.forEach(img => { delete img.dataset.mtDone; });
      failedImages.clear();
      retry.style.display = "none";
      runOnImages(targets);
    };
    document.body.appendChild(retry);

    const pill = document.createElement("div");
    pill.id = "mt-status";
    Object.assign(pill.style, {
      position: "fixed", right: "20px", bottom: "70px", zIndex: 2147483647,
      background: "rgba(0,0,0,0.85)", color: "#0f0", padding: "8px 14px",
      borderRadius: "8px", fontSize: "12px", fontFamily: "ui-monospace, monospace",
      display: "none", maxWidth: "320px", whiteSpace: "pre-wrap",
    });
    document.body.appendChild(pill);
  }
  const status = (s) => {
    const el = document.getElementById("mt-status");
    if (!el) return;
    el.style.display = "block";
    el.textContent = s;
  };
  const ctxValid = () => { try { return !!chrome.runtime.id; } catch { return false; } };
  const setBadge = (text, color) => {
    if (!ctxValid()) return;  // extension was reloaded, nothing to do
    try { chrome.runtime.sendMessage({ type: "setBadge", text: text || "", color: color || "#4af" }); }
    catch { /* extension context invalidated */ }
  };

  // Lazy loaders use IntersectionObserver, scroll listeners, or framework re-renders
  // that insert <img> nodes. Step a viewport at a time, wait for in-flight loads
  // and watch DOM mutations to cover all three.
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function isCandidateImg(img) {
    if (!img.src || img.src.startsWith("data:") || img.src.startsWith("blob:")) return false;
    // Heuristic: large enough to be a panel OR still loading (naturalWidth=0).
    // In-flight images are included so they are not dropped before they load.
    if (img.naturalWidth === 0) return true;  // not loaded yet, keep watching
    return img.naturalWidth * img.naturalHeight >= MIN_PIXELS;
  }

  async function waitForImageLoads(imgs, perImageTimeoutMs = 15000) {
    // Resolve once the image is decoded; give up on timeout or error.
    const tasks = imgs.map(async img => {
      if (img.complete && img.naturalWidth > 0) {
        try { await img.decode(); } catch {}
        return { img, ok: true };
      }
      return await new Promise(resolve => {
        let done = false;
        const finish = (ok) => { if (done) return; done = true; cleanup(); resolve({ img, ok }); };
        const onLoad = () => finish(true);
        const onErr = () => finish(false);
        const cleanup = () => {
          img.removeEventListener("load", onLoad);
          img.removeEventListener("error", onErr);
        };
        img.addEventListener("load", onLoad);
        img.addEventListener("error", onErr);
        setTimeout(() => finish(false), perImageTimeoutMs);
      });
    });
    return Promise.all(tasks);
  }

  async function scrollAndLoadAllPanels(progressFn) {
    // Track new <img> nodes added during scroll.
    const seen = new WeakSet();
    [...document.images].forEach(img => seen.add(img));
    const mutationFound = [];
    const obs = new MutationObserver(muts => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.tagName === "IMG") {
            if (!seen.has(n)) { seen.add(n); mutationFound.push(n); }
          } else {
            for (const sub of n.querySelectorAll?.("img") || []) {
              if (!seen.has(sub)) { seen.add(sub); mutationFound.push(sub); }
            }
          }
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    try {
      const startY = window.scrollY;
      // Scroll down 80% of a viewport per pass.
      const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
      let lastDocHeight = 0;
      let lastCandidateCount = 0;
      let stableScans = 0;
      const STABILITY_REQUIRED = 3;
      const MAX_PASSES = 200;  // hard cap for absurd chapters

      for (let pass = 0; pass < MAX_PASSES; pass++) {
        const targetY = Math.min(document.body.scrollHeight, window.scrollY + step);
        window.scrollTo(0, targetY);
        await sleep(180);  // let the browser fire IO/scroll listeners

        // At the bottom, count passes with no new images.
        const atBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 4;
        const candidates = [...document.images].filter(isCandidateImg);
        progressFn?.(candidates.length, atBottom, pass);

        if (atBottom) {
          // Wait briefly for the last-row IO callbacks to fire.
          await sleep(400);
          const inFlight = candidates.filter(i => !i.complete || i.naturalWidth === 0);
          if (inFlight.length) {
            // Wait for in-flight images, capped per image.
            await waitForImageLoads(inFlight, 15000);
          }
          // Re-scan after the wait. Include any mutation-found nodes.
          const after = [...new Set([...document.images, ...mutationFound])].filter(isCandidateImg);
          const docH = document.body.scrollHeight;
          if (after.length === lastCandidateCount && docH === lastDocHeight) {
            stableScans++;
            if (stableScans >= STABILITY_REQUIRED) break;
            // Nudge the scroll to trigger scroll-listener loaders.
            window.scrollBy(0, -50); await sleep(120);
            window.scrollBy(0, 50);  await sleep(120);
          } else {
            stableScans = 0;
            lastCandidateCount = after.length;
            lastDocHeight = docH;
          }
        }
      }

      // Scroll back to the top.
      window.scrollTo(0, startY);
      await sleep(150);

      // One more decode-await for anything still mid-load.
      const finalCandidates = [...new Set([...document.images, ...mutationFound])].filter(isCandidateImg);
      const stillLoading = finalCandidates.filter(i => !i.complete || i.naturalWidth === 0);
      if (stillLoading.length) {
        await waitForImageLoads(stillLoading, 10000);
      }
    } finally {
      obs.disconnect();
    }
  }

  function collectImages() {
    // Only fully-loaded panel-sized images that we haven't translated yet.
    return [...document.images].filter(img => {
      if (img.dataset.mtDone) return false;
      if (!img.src || img.src.startsWith("data:") || img.src.startsWith("blob:")) return false;
      if (img.naturalWidth === 0) return false;  // still loading or broken, reported separately
      return img.naturalWidth * img.naturalHeight >= MIN_PIXELS;
    });
  }

  function findUnloadedPanels() {
    // Panels that would qualify if they had loaded. Reported to the user.
    return [...document.images].filter(img => {
      if (img.dataset.mtDone) return false;
      if (!img.src || img.src.startsWith("data:") || img.src.startsWith("blob:")) return false;
      return img.naturalWidth === 0 || (img.complete === false);
    });
  }

  // Read pixels of a loaded <img> via canvas. Returns {ok, bytes, ct} or {ok:false}.
  // Fails (ok:false) on a CORS-tainted canvas; the caller falls back to a network fetch.
  async function extractViaCanvas(img) {
    try {
      if (!img.complete || img.naturalWidth === 0) return { ok: false, reason: "not loaded" };
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const blob = await new Promise((res, rej) => {
        try { canvas.toBlob(b => b ? res(b) : rej(new Error("toBlob returned null")), "image/png"); }
        catch (e) { rej(e); }  // SecurityError if tainted
      });
      const buf = new Uint8Array(await blob.arrayBuffer());
      if (buf.length === 0) return { ok: false, reason: "empty blob" };
      return { ok: true, bytes: Array.from(buf), ct: "image/png" };
    } catch (e) {
      // Usually a SecurityError from a tainted canvas on cross-origin images.
      return { ok: false, reason: String(e.message || e) };
    }
  }

  // Try a same-origin / credentials fetch in the content script context.
  // Works when CDN allows credentialed requests with the page's Referer.
  async function extractViaFetch(url) {
    try {
      const r = await fetch(url, { credentials: "include", referrer: location.href, mode: "cors" });
      if (!r.ok) return { ok: false, reason: `HTTP ${r.status}` };
      const ct = r.headers.get("content-type") || "image/webp";
      const buf = new Uint8Array(await r.arrayBuffer());
      if (buf.length === 0) return { ok: false, reason: "empty body" };
      return { ok: true, bytes: Array.from(buf), ct };
    } catch (e) {
      return { ok: false, reason: String(e.message || e) };
    }
  }

  // Tries canvas, then page fetch, then chrome.debugger.
  // Canvas needs no network but only works on same-origin or CORS-enabled images.
  // Page fetch works for CDNs that accept credentialed requests from this origin.
  // The debugger attaches devtools, so it goes last, but it works on most cross-origin CDNs.
  async function extractImageBytes(img) {
    const tries = [];

    const c = await extractViaCanvas(img);
    tries.push(`canvas:${c.ok ? "ok" : c.reason}`);
    if (c.ok) return { ok: true, buf: c.bytes, ct: c.ct, via: "canvas", tries };

    const f = await extractViaFetch(img.src);
    tries.push(`fetch:${f.ok ? "ok" : f.reason}`);
    if (f.ok) return { ok: true, buf: f.bytes, ct: f.ct, via: "fetch", tries };

    const d = await ask({ type: "fetchImage", url: img.src });
    tries.push(`debugger:${d?.ok ? `ok(${d.buf?.length || 0}b)` : (d?.error || "?")}`);
    if (d?.ok && d.buf && d.buf.length > 100) {  // sanity: real images are >100 bytes
      return { ok: true, buf: d.buf, ct: d.ct, via: "debugger", tries };
    }
    return { ok: false, error: `all fetch methods failed (${tries.join(" | ")})` };
  }

  function ask(msg) {
    if (!ctxValid()) return Promise.resolve({ ok: false, error: "extension reloaded, refresh the page (⌘R)" });
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, (r) => resolve(r || { ok: false, error: "no response" })); }
      catch (e) { resolve({ ok: false, error: String(e) }); }
    });
  }

  async function translateOne(img, idx, total, config) {
    img.dataset.mtDone = "1";
    img.title = "";  // clear any previous error
    failedImages.delete(img);
    const origBorder = img.style.border;
    img.style.border = "4px solid orange";
    const filename = img.src.split("/").pop().split("?")[0];
    try {
      const fetched = await extractImageBytes(img);
      if (!fetched || !fetched.ok) {
        const err = fetched?.error || "fetch failed";
        img.style.border = "4px solid red";
        img.title = `MT error: ${err}`;
        failedImages.add(img);
        return { ok: false, err };
      }
      let last;
      for (let attempt = 1; attempt <= 3; attempt++) {
        const r = await ask({
          type: "translate",
          apiUrl: API,
          buf: fetched.buf,
          ct: fetched.ct,
          filename,
          config: config,
        });
        if (r && r.ok) {
          const blob = new Blob([new Uint8Array(r.buf)], { type: r.ct });
          const url = URL.createObjectURL(blob);
          img.src = url;
          img.removeAttribute("srcset");
          // Color by translator used: green = primary (Gemini), gold = fallback
          const usedPrimary = r.translator === "gemini" || r.tried === r.translator;
          const color = usedPrimary ? "limegreen" : "gold";
          img.style.border = `4px solid ${color}`;
          const cacheLabel = r.cache === "hit" ? "cache" : "fresh";
          const triedLabel = (r.tried && r.tried !== r.translator) ? ` (after ${r.tried.split(',').slice(0, -1).join(' > ')})` : "";
          img.title = `MT: ${r.translator} • ${cacheLabel}${triedLabel}`;
          setTimeout(() => { img.style.border = origBorder; }, 1500);
          return { ok: true, cache: r.cache, translator: r.translator };
        }
        last = r;
        await new Promise(rs => setTimeout(rs, 1000 * Math.pow(2, attempt - 1)));
      }
      const err = last?.error || "unknown";
      img.style.border = "4px solid red";
      img.title = `MT error: ${err}`;
      failedImages.add(img);
      return { ok: false, err };
    } catch (e) {
      img.style.border = "4px solid red";
      img.title = `MT exception: ${e}`;
      failedImages.add(img);
      return { ok: false, err: String(e) };
    }
  }

  async function runOnImages(imgs) {
    if (running) return;
    running = true;
    const btn = document.getElementById("mt-btn");
    const retryBtn = document.getElementById("mt-retry");
    btn.disabled = true; btn.style.opacity = "0.6";
    if (imgs === collectImages()) {
      // Full-page run: scroll in steps and wait for every panel to load.
      await scrollAndLoadAllPanels((n, atBottom, pass) => {
        status(`scrolling pass ${pass+1}… found ${n} panels${atBottom ? " (at bottom, waiting for loads)" : ""}`);
      });
      imgs = collectImages();
      const unloaded = findUnloadedPanels();
      if (unloaded.length) {
        // Tell the user about panels that never loaded.
        console.warn(`[MT] ${unloaded.length} panel-like images never finished loading`, unloaded);
        status(`${unloaded.length} panels failed to load and were skipped. Scroll over them and click Retry`);
        unloaded.forEach(i => { i.style.outline = "3px dashed #e55"; });
      }
    }
    if (!imgs.length) {
      const unloaded = findUnloadedPanels();
      const hint = unloaded.length
        ? `\n${unloaded.length} images are still loading. Scroll past them and try again.`
        : "\nscroll through the page so panels load, then click again.";
      status(`no panels found (need images >700kpx).${hint}`);
      btn.disabled = false; btn.style.opacity = "1";
      running = false;
      return;
    }
    const lang = getLang();
    const config = buildConfig(lang);
    status(`found ${imgs.length} panels, translating (${LANG_PRESETS[lang].label}, concurrency ${CONCURRENCY})...`);
    setBadge(`0/${imgs.length}`, "#4af");
    let done = 0, failed = 0, cacheHits = 0;
    const byTranslator = {};  // {gemini: N, custom_openai: M, ...}
    const t0 = Date.now();
    const queue = imgs.map((img, i) => [img, i + 1, imgs.length]);
    async function worker() {
      while (queue.length) {
        const [img, idx, total] = queue.shift();
        const r = await translateOne(img, idx, total, config);
        done++;
        if (!r.ok) failed++;
        else {
          if (r.cache === "hit") cacheHits++;
          const t = r.translator || "?";
          byTranslator[t] = (byTranslator[t] || 0) + 1;
        }
        const elapsed = ((Date.now() - t0) / 1000).toFixed(0);
        const LABEL = { custom_openai: "Local", gemini: "Gem", groq: "Groq", deepseek: "DS" };
        const tStr = Object.entries(byTranslator).map(([k, v]) => `${v}${(LABEL[k]||k)[0]}`).join(" ");
        status(`${done}/${imgs.length} done (${failed} fail) | ${tStr} | ${elapsed}s`);
        setBadge(`${done}/${imgs.length}`, failed ? "#e55" : "#4af");
      }
    }
    await Promise.all(Array(CONCURRENCY).fill(0).map(worker));
    const elapsed = ((Date.now() - t0) / 1000).toFixed(0);
    const LABEL_FULL = { custom_openai: "Local (aya/qwen)", gemini: "Gemini", groq: "Groq", deepseek: "DeepSeek" };
    const breakdown = Object.entries(byTranslator).map(([k, v]) => `${v} ${LABEL_FULL[k]||k}`).join(", ");
    status(`Done in ${elapsed}s. ${imgs.length - failed}/${imgs.length} ok\n${breakdown}${cacheHits ? `, ${cacheHits} cached` : ""}`);
    setBadge(failed ? `${failed}!` : "OK", failed ? "#e55" : "#4a4");
    setTimeout(() => setBadge("", "#4af"), 8000);

    if (failed > 0) {
      retryBtn.textContent = `Retry ${failed} Failed`;
      retryBtn.style.display = "inline-block";
    }

    chrome.runtime.sendMessage({ type: "detach" });
    btn.disabled = false; btn.style.opacity = "1";
    running = false;
  }

  // Single-image translate (context menu right-click)
  async function translateOneByUrl(url) {
    const img = [...document.querySelectorAll("img")].find(i => i.src === url);
    if (!img) {
      status(`could not locate img element for ${url.split("/").pop()}`);
      return;
    }
    if (img.dataset.mtDone) delete img.dataset.mtDone;
    failedImages.delete(img);
    await runOnImages([img]);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "translatePage") runOnImages(collectImages());
    else if (msg.type === "translateOne") translateOneByUrl(msg.url);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", makeUI, { once: true });
  } else {
    makeUI();
  }
})();
