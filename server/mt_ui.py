#!/usr/bin/env python3
"""
mt-ui: tiny browser UI for manga translation.
Drag a chapter folder onto the page -> watch progress -> see translated viewer.

Runs on http://127.0.0.1:8002
Talks to the main translator backend at http://127.0.0.1:8000
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

API = "http://127.0.0.1:8000/translate/with-form/image/stream"
OUT_ROOT = Path.home() / "Downloads" / "mt-ui-out"
CACHE_DIR = OUT_ROOT / "cache"
METRICS_LOG = OUT_ROOT / "metrics.jsonl"
OUT_ROOT.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache eviction: keep total cache size under this. LRU by file mtime.
CACHE_MAX_GB = float(os.environ.get("MT_CACHE_MAX_GB", "10"))
CACHE_EVICT_TO_GB = CACHE_MAX_GB * 0.9  # evict down to 90% to avoid thrashing

def _log_metric(rec: dict):
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(METRICS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

def _cache_total_bytes() -> int:
    return sum(p.stat().st_size for p in CACHE_DIR.glob("*.png") if p.is_file())

def _cache_evict_if_needed():
    """LRU eviction: if total cache size > CACHE_MAX_GB, delete oldest .png+.meta pairs until under CACHE_EVICT_TO_GB."""
    max_bytes = int(CACHE_MAX_GB * 1024**3)
    target_bytes = int(CACHE_EVICT_TO_GB * 1024**3)
    total = _cache_total_bytes()
    if total <= max_bytes:
        return 0
    # Sort by mtime ASC = oldest first
    pngs = sorted(CACHE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    evicted = 0
    for png in pngs:
        if total <= target_bytes:
            break
        try:
            size = png.stat().st_size
            png.unlink()
            meta = png.with_suffix(".meta")
            if meta.exists():
                meta.unlink()
            total -= size
            evicted += 1
        except Exception:
            continue
    return evicted

def _cache_touch(path: Path):
    """Update mtime on cache hit so frequently-used stay alive."""
    try:
        path.touch()
    except Exception:
        pass

CONFIG = {
    "translator": {"translator": "custom_openai", "target_lang": "ENG", "no_text_lang_skip": False, "gpt_config": "/Users/lucaslow/manga-image-translator/gpt_config.yaml"},
    "detector":  {"detector": "default", "detection_size": 1536, "text_threshold": 0.5, "box_threshold": 0.7, "unclip_ratio": 2.3},
    "ocr":       {"ocr": "48px", "min_text_length": 0, "ignore_bubble": 0},
    "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1024, "inpainting_precision": "bf16"},
    "render":    {"renderer": "manga2eng", "alignment": "auto", "font_size_minimum": -1, "rtl": True},
    "kernel_size": 3, "mask_dilation_offset": 20,
}
MAX_RETRIES = 3
CONCURRENCY = 5

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory job state
jobs: dict[str, dict] = {}

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Manga Translator</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee; margin: 0; padding: 2rem; }
  h1 { margin: 0 0 1rem; }
  #drop { border: 3px dashed #555; padding: 4rem 2rem; text-align: center; cursor: pointer; transition: background 0.2s, border-color 0.2s; border-radius: 12px; }
  #drop.hover { background: #1c2c3c; border-color: #4af; }
  #drop p { margin: 0.5rem 0; color: #999; }
  #log { background: #000; color: #0f0; font-family: ui-monospace, monospace; padding: 1rem; margin-top: 1rem; height: 250px; overflow-y: scroll; font-size: 12px; border-radius: 6px; }
  #viewer { margin-top: 1rem; }
  #viewer img { display: block; margin: 0 auto; max-width: 900px; width: 100%; }
  button { background: #4af; color: white; border: 0; padding: 0.6rem 1.2rem; font-size: 14px; border-radius: 6px; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .row { display: flex; gap: 1rem; align-items: center; margin-top: 1rem; }
  small { color: #888; }
</style></head>
<body>
<h1>📖 Manga Translator</h1>
<small>⌘S the chapter page in Chrome as "Webpage, Complete", then drag the <code>_files</code> folder below.</small>

<div id="drop">
  <p style="font-size: 1.2rem;">📂 Drop chapter folder here</p>
  <p>or click to choose</p>
  <input type="file" id="picker" webkitdirectory multiple style="display:none">
</div>

<div class="row">
  <button id="go" disabled>Translate</button>
  <small id="info"></small>
</div>

<div id="stats" style="margin-top:1rem;padding:0.8rem 1rem;background:#1a1a1a;border-radius:6px;font-family:ui-monospace,monospace;font-size:12px;color:#bbb;display:flex;gap:1.5rem;flex-wrap:wrap;">
  <span>loading stats...</span>
</div>

<div id="log" style="display:none"></div>
<div id="viewer"></div>

<script>
async function refreshStats() {
  try {
    const r = await fetch('/stats');
    const s = await r.json();
    const c = await (await fetch('/cache/stats')).json();
    const byT = Object.entries(s.by_translator || {}).map(([k, v]) => `${k}=${v}`).join('  ') || '(none)';
    document.getElementById('stats').innerHTML = `
      <span><b>total</b> ${s.total || 0}</span>
      <span><b>avg</b> ${s.avg_translate_s_miss_only ?? 'n/a'}s</span>
      <span><b>cache hits</b> ${s.cache_hits || 0}</span>
      <span><b>cache misses</b> ${s.cache_misses || 0}</span>
      <span><b>failures</b> ${s.failures || 0}</span>
      <span><b>by translator</b> ${byT}</span>
      <span><b>cache disk</b> ${c.count || 0} files, ${c.total_mb || 0} MB</span>`;
  } catch (e) {
    document.getElementById('stats').textContent = 'stats unavailable: ' + e;
  }
}
refreshStats();
setInterval(refreshStats, 3000);

const drop = document.getElementById('drop');
const picker = document.getElementById('picker');
const go = document.getElementById('go');
const info = document.getElementById('info');
const logEl = document.getElementById('log');
const viewer = document.getElementById('viewer');
let files = [];

function pickFiles(fileList) {
  files = [...fileList].filter(f => /\\.(webp|jpg|jpeg|png)$/i.test(f.name) && f.size > 30_000);
  info.textContent = `${files.length} candidate images`;
  go.disabled = files.length === 0;
}

drop.addEventListener('click', () => picker.click());
picker.addEventListener('change', e => pickFiles(e.target.files));

drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('hover'); });
drop.addEventListener('dragleave', () => drop.classList.remove('hover'));
drop.addEventListener('drop', async e => {
  e.preventDefault();
  drop.classList.remove('hover');
  const items = [...e.dataTransfer.items];
  const out = [];
  async function walk(entry, path='') {
    if (entry.isFile) {
      await new Promise(r => entry.file(f => { f._path = path + f.name; out.push(f); r(); }));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const entries = await new Promise(r => reader.readEntries(r));
      for (const ent of entries) await walk(ent, path + entry.name + '/');
    }
  }
  for (const it of items) {
    const ent = it.webkitGetAsEntry && it.webkitGetAsEntry();
    if (ent) await walk(ent);
    else { const f = it.getAsFile(); if (f) out.push(f); }
  }
  pickFiles(out);
});

go.addEventListener('click', async () => {
  go.disabled = true;
  logEl.style.display = 'block';
  logEl.textContent = '';
  viewer.innerHTML = '';
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.name);
  const resp = await fetch('/start', { method: 'POST', body: fd });
  const { job_id } = await resp.json();

  const es = new EventSource(`/progress/${job_id}`);
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.log) {
      logEl.textContent += d.log + '\\n';
      logEl.scrollTop = logEl.scrollHeight;
    }
    if (d.done) {
      es.close();
      viewer.innerHTML = `<h2>Translated</h2>` + d.images.map(name =>
        `<img src="/result/${job_id}/${name}" loading="lazy">`).join('');
      go.disabled = false;
    }
  };
});
</script>
</body></html>
"""

def _cache_key(image_bytes: bytes, config_str: str) -> str:
    h = hashlib.sha256()
    h.update(image_bytes)
    h.update(b"\x00")
    h.update(config_str.encode("utf-8"))
    return h.hexdigest()

async def _backend_translate(image_bytes: bytes, config_str: str) -> tuple[Optional[bytes], Optional[str]]:
    """POST to backend stream endpoint, parse [status:1][size:4][data:N] format."""
    async with httpx.AsyncClient(timeout=600) as client:
        files = {"image": ("panel.webp", image_bytes, "application/octet-stream")}
        data = {"config": config_str}
        async with client.stream("POST", API, files=files, data=data) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode("utf-8", errors="replace")
                return None, f"HTTP {r.status_code}: {text[:200]}"
            buf = b""
            result_bytes = None
            err_msg = None
            async for chunk in r.aiter_bytes(65536):
                buf += chunk
                while len(buf) >= 5:
                    status = buf[0]
                    size = int.from_bytes(buf[1:5], "big")
                    if len(buf) < 5 + size:
                        break
                    payload = buf[5:5+size]
                    buf = buf[5+size:]
                    if status == 0:
                        result_bytes = payload
                    elif status == 2:
                        err_msg = payload.decode("utf-8", errors="replace")
                if result_bytes is not None or err_msg is not None:
                    break
            if err_msg:
                return None, err_msg
            if result_bytes:
                return result_bytes, None
            return None, "no result in stream"

# Fallback chain, tried in order until one succeeds.
FALLBACK_CHAIN = ["custom_openai"]  # Gemini disabled (free tier exhausted); re-add "gemini" when quota resets or billing on

@app.post("/translate-cached")
async def translate_cached(image: UploadFile = File(...), config: str = Form("{}")):
    """Cache-on-write proxy with auto-fallback.
    Tries each translator in FALLBACK_CHAIN until one succeeds.
    Response headers:
      X-MT-Cache: hit|miss
      X-MT-Translator: gemini|custom_openai|... (the one that actually produced the result)
      X-MT-Tried: comma-separated list of all translators attempted
      X-MT-Error: last error message (only on full failure)
    """
    image_bytes = await image.read()
    try:
        config_dict = json.loads(config) if config else {}
    except Exception:
        config_dict = {}

    # Client's preferred translator first, then the chain
    requested = (config_dict.get("translator", {}) or {}).get("translator", FALLBACK_CHAIN[0])
    chain = [requested] + [t for t in FALLBACK_CHAIN if t != requested]

    t_start = time.time()
    image_size = len(image_bytes)

    # Exact-match cache lookup against the requested config (fastest path on re-reads)
    primary_key = _cache_key(image_bytes, config)
    primary_path = CACHE_DIR / f"{primary_key}.png"
    if primary_path.exists() and primary_path.stat().st_size > 1000:
        # Sidecar holds which translator originally produced this
        meta_path = CACHE_DIR / f"{primary_key}.meta"
        used = meta_path.read_text().strip() if meta_path.exists() else "?"
        _cache_touch(primary_path)  # LRU: mark as recently used
        _log_metric({
            "key": primary_key[:8], "translator": used, "cache": "hit",
            "duration_s": round(time.time() - t_start, 3),
            "in_kb": image_size // 1024, "out_kb": primary_path.stat().st_size // 1024,
        })
        return Response(
            content=primary_path.read_bytes(),
            media_type="image/png",
            headers={"X-MT-Cache": "hit", "X-MT-Translator": used, "X-MT-Tried": used},
        )

    tried = []
    last_err = None
    for t in chain:
        # Build a config variant with this translator
        variant = json.loads(config) if config else {}
        variant.setdefault("translator", {})["translator"] = t
        variant_str = json.dumps(variant)

        # Per-translator cache (so a later request with explicit `t` is also a hit)
        vk = _cache_key(image_bytes, variant_str)
        vp = CACHE_DIR / f"{vk}.png"
        if vp.exists() and vp.stat().st_size > 1000:
            (CACHE_DIR / f"{primary_key}.meta").write_text(t)
            # Hard link or copy so primary_key is satisfied too
            try:
                if not primary_path.exists():
                    primary_path.write_bytes(vp.read_bytes())
            except Exception:
                pass
            tried.append(t)
            return Response(
                content=vp.read_bytes(),
                media_type="image/png",
                headers={"X-MT-Cache": "hit", "X-MT-Translator": t, "X-MT-Tried": ",".join(tried)},
            )

        tried.append(t)
        result_bytes, err = await _backend_translate(image_bytes, variant_str)
        if result_bytes:
            vp.write_bytes(result_bytes)
            primary_path.write_bytes(result_bytes)
            (CACHE_DIR / f"{primary_key}.meta").write_text(t)
            _cache_evict_if_needed()
            _log_metric({
                "key": primary_key[:8], "translator": t, "cache": "miss",
                "tried": tried, "duration_s": round(time.time() - t_start, 3),
                "in_kb": image_size // 1024, "out_kb": len(result_bytes) // 1024,
            })
            return Response(
                content=result_bytes,
                media_type="image/png",
                headers={"X-MT-Cache": "miss", "X-MT-Translator": t, "X-MT-Tried": ",".join(tried)},
            )
        last_err = err

    # All translators failed
    _log_metric({
        "key": primary_key[:8], "translator": None, "cache": "fail",
        "tried": tried, "duration_s": round(time.time() - t_start, 3),
        "in_kb": image_size // 1024, "error": (last_err or "")[:200],
    })
    return JSONResponse(
        {"error": f"all translators failed", "tried": tried, "last_error": last_err},
        status_code=502,
        headers={"X-MT-Tried": ",".join(tried), "X-MT-Error": (last_err or "")[:200]},
    )

@app.get("/stats")
async def stats():
    """Aggregate metrics from JSONL log."""
    if not METRICS_LOG.exists():
        return {"total": 0}
    by_t = {}
    durations = []
    cache_hits = 0
    cache_misses = 0
    fails = 0
    total = 0
    with open(METRICS_LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            total += 1
            if r.get("cache") == "hit":
                cache_hits += 1
            elif r.get("cache") == "miss":
                cache_misses += 1
            elif r.get("cache") == "fail":
                fails += 1
            t = r.get("translator") or "(failed)"
            by_t[t] = by_t.get(t, 0) + 1
            d = r.get("duration_s")
            if d is not None and r.get("cache") == "miss":
                durations.append(d)
    avg = round(sum(durations) / len(durations), 2) if durations else None
    return {
        "total": total,
        "by_translator": by_t,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "failures": fails,
        "avg_translate_s_miss_only": avg,
        "log_path": str(METRICS_LOG),
    }

@app.get("/cache/stats")
async def cache_stats():
    files = list(CACHE_DIR.glob("*.png"))
    total_bytes = sum(f.stat().st_size for f in files)
    return {"count": len(files), "total_mb": round(total_bytes / 1024 / 1024, 1)}

@app.post("/cache/clear")
async def cache_clear():
    n = 0
    for f in CACHE_DIR.glob("*.png"):
        f.unlink()
        n += 1
    for f in CACHE_DIR.glob("*.meta"):
        f.unlink()
    return {"cleared": n}

@app.post("/cache/evict")
async def cache_evict():
    """Force LRU eviction now."""
    n = _cache_evict_if_needed()
    return {"evicted": n, "current_mb": round(_cache_total_bytes() / 1024 / 1024, 1), "max_gb": CACHE_MAX_GB}

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML

@app.post("/start")
async def start(request: Request, files: list[UploadFile] = File(...)):
    job_id = str(int(time.time() * 1000))
    job_dir = OUT_ROOT / job_id
    in_dir = job_dir / "in"
    out_dir = job_dir / "out"
    in_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    saved = []
    for f in files:
        name = Path(f.filename).name
        dst = in_dir / name
        with open(dst, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
        saved.append(dst)
    jobs[job_id] = {"in_dir": in_dir, "out_dir": out_dir, "queue": asyncio.Queue(), "task": None}
    jobs[job_id]["task"] = asyncio.create_task(_run_job(job_id, saved))
    return {"job_id": job_id, "count": len(saved)}

def natural_key(p):
    return [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", p.stem)]

async def _translate_one(client: httpx.AsyncClient, p: Path, out_dir: Path, config_json: str):
    out_path = out_dir / (p.stem + ".png")
    if out_path.exists() and out_path.stat().st_size > 1000:
        return p, out_path, "skip", 0.0
    t0 = time.time()
    with open(p, "rb") as f:
        content = f.read()
    # Check global cache first
    key = _cache_key(content, config_json)
    cache_path = CACHE_DIR / f"{key}.png"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        out_path.write_bytes(cache_path.read_bytes())
        return p, out_path, "cache", time.time() - t0
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result_bytes, err = await _backend_translate(content, config_json)
            if result_bytes:
                out_path.write_bytes(result_bytes)
                cache_path.write_bytes(result_bytes)
                tag = "ok" if attempt == 1 else f"ok (retry {attempt})"
                return p, out_path, tag, time.time() - t0
            last_err = err
        except Exception as e:
            last_err = f"exception: {e}"
        if attempt < MAX_RETRIES:
            await asyncio.sleep(2 ** (attempt - 1))
    return p, None, f"failed: {last_err}", time.time() - t0

async def _run_job(job_id: str, files: list[Path]):
    job = jobs[job_id]
    q: asyncio.Queue = job["queue"]
    out_dir: Path = job["out_dir"]
    files.sort(key=natural_key)
    total = len(files)
    config_json = json.dumps(CONFIG)
    t_start = time.time()
    await q.put({"log": f"Found {total} images. Translating with concurrency {CONCURRENCY}..."})

    sem = asyncio.Semaphore(CONCURRENCY)
    results = {}
    async with httpx.AsyncClient() as client:
        async def worker(idx, p):
            async with sem:
                r = await _translate_one(client, p, out_dir, config_json)
                _, out_path, status, dur = r
                wall = time.time() - t_start
                tag = "OK" if status.startswith("ok") else ("SKIP" if status == "skip" else "FAIL")
                await q.put({"log": f"[{idx}/{total}] {p.name} -- {tag} ({dur:.1f}s) | wall {wall:.0f}s"})
                if status.startswith("fail"):
                    await q.put({"log": f"    {status}"})
                results[idx] = out_path
        tasks = [asyncio.create_task(worker(i, p)) for i, p in enumerate(files, 1)]
        await asyncio.gather(*tasks, return_exceptions=True)

    translated = [results[i] for i in sorted(results) if results[i] is not None]
    total_time = time.time() - t_start
    await q.put({"log": f"Done in {total_time:.1f}s. {len(translated)}/{total} translated."})
    await q.put({"done": True, "images": [p.name for p in translated]})

@app.get("/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    q: asyncio.Queue = jobs[job_id]["queue"]
    async def stream():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("done"):
                break
    return StreamingResponse(stream(), media_type="text/event-stream")

@app.get("/result/{job_id}/{filename}")
async def result(job_id: str, filename: str):
    if job_id not in jobs:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    path = jobs[job_id]["out_dir"] / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="info")
