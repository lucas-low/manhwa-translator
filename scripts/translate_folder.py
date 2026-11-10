#!/usr/bin/env python3
"""
Translate every image in a folder via the local manga-image-translator backend.
Concurrent + fast-tier settings.
Usage:
    python3 translate-folder.py <input_folder> [output_folder]
"""
import sys
import os
import re
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Path to the manhwa/manga prompt config. Override with MT_GPT_CONFIG.
GPT_CONFIG = os.environ.get("MT_GPT_CONFIG", os.path.expanduser("~/.config/mt-stack/gpt_config.yaml"))

API = "http://127.0.0.1:8000/translate/with-form/image/stream"
CONCURRENCY = 5  # parallel POSTs (server queues internally; client overlap still helps)

CONFIG = {
    "translator": {
        "translator": "custom_openai",
        "target_lang": "ENG",
        "no_text_lang_skip": False,
        "gpt_config": GPT_CONFIG,
    },
    "detector":  {"detector": "default", "detection_size": 1536, "text_threshold": 0.5, "box_threshold": 0.7, "unclip_ratio": 2.3},
    "ocr":       {"ocr": "48px", "min_text_length": 0, "ignore_bubble": 0},
    "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1024, "inpainting_precision": "bf16"},
    "render":    {"renderer": "manga2eng", "alignment": "auto", "font_size_minimum": -1, "rtl": True},
    "kernel_size": 3,
    "mask_dilation_offset": 20,
}

VALID_EXT = {".webp", ".jpg", ".jpeg", ".png"}

def natural_key(p):
    name = p.stem
    return [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", name)]

MAX_RETRIES = 3

def _attempt_translate(p, config_json):
    """Single attempt. Returns (result_bytes, error_msg)."""
    with open(p, "rb") as f:
        files = {"image": (p.name, f, "application/octet-stream")}
        data = {"config": config_json}
        r = requests.post(API, files=files, data=data, timeout=600, stream=True)
    if not r.ok:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    buf = b""
    result_bytes = None
    err_msg = None
    for chunk in r.iter_content(chunk_size=65536):
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
        return None, f"stream error: {err_msg[:200]}"
    if not result_bytes:
        return None, "no result in stream"
    return result_bytes, None

def translate_one(p, out_dir, config_json, idx, total):
    out_path = out_dir / (p.stem + ".png")
    if out_path.exists() and out_path.stat().st_size > 1000:
        return (idx, p, out_path, "skip", 0)
    t0 = time.time()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result_bytes, err = _attempt_translate(p, config_json)
            if result_bytes:
                out_path.write_bytes(result_bytes)
                tag = "ok" if attempt == 1 else f"ok (retry {attempt})"
                return (idx, p, out_path, tag, time.time()-t0)
            last_err = err
        except Exception as e:
            last_err = f"exception: {e}"
        # Backoff before retry (except after final attempt)
        if attempt < MAX_RETRIES:
            time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s
    return (idx, p, None, f"failed after {MAX_RETRIES} attempts: {last_err}", time.time()-t0)

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    in_dir = Path(sys.argv[1]).expanduser().resolve()
    if not in_dir.is_dir():
        print(f"Not a folder: {in_dir}")
        sys.exit(1)
    out_dir = Path(sys.argv[2]).expanduser().resolve() if len(sys.argv) > 2 else in_dir.parent / (in_dir.name + "_translated")
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = [p for p in in_dir.iterdir() if p.suffix.lower() in VALID_EXT]
    candidates = [p for p in candidates if p.stat().st_size > 30_000]
    candidates.sort(key=natural_key)
    total = len(candidates)
    print(f"Found {total} candidate images in {in_dir}")
    print(f"Writing translated images to {out_dir}")
    print(f"Concurrency: {CONCURRENCY}, inpainter: lama@1024, detection: 1536, fp16\n")

    t_start = time.time()
    config_json = json.dumps(CONFIG)
    results = {}  # idx -> (path or None, status)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(translate_one, p, out_dir, config_json, i, total): i
                   for i, p in enumerate(candidates, 1)}
        done_count = 0
        for fut in as_completed(futures):
            idx, p, out_path, status, dur = fut.result()
            done_count += 1
            tag = "OK" if status == "ok" else ("SKIP" if status == "skip" else "FAIL")
            elapsed = time.time() - t_start
            print(f"[{done_count}/{total}] {p.name} -- {tag} ({dur:.1f}s) | wall {elapsed:.0f}s")
            if status not in ("ok", "skip"):
                print(f"    {status}")
            results[idx] = (p, out_path, status)

    # Build ordered list
    ordered = [results[i] for i in sorted(results.keys()) if results[i][1] is not None]
    translated = [r[1] for r in ordered]

    viewer = out_dir / "viewer.html"
    panels_html = "\n".join(
        f'<img src="{p.name}" alt="{p.stem}" loading="lazy" style="display:block;margin:0 auto;max-width:100%;">'
        for p in translated
    )
    viewer.write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Translated {in_dir.name}</title>
<style>body{{margin:0;background:#000;font-family:sans-serif;color:#888;}} h1{{padding:1rem;}} img{{max-width:900px;}}</style>
</head><body><h1>Translated: {in_dir.name} -- {len(translated)} panels</h1>{panels_html}</body></html>
""")
    total_time = time.time() - t_start
    print(f"\nDone in {total_time:.1f}s. {len(translated)}/{total} translated.")
    print(f"Viewer: file://{viewer}")
    os.system(f"open '{viewer}'")

if __name__ == "__main__":
    main()
