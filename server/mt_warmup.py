#!/usr/bin/env python3
"""
mt-warmup: fire a dummy translate to pre-load detector/OCR/inpainter/LLM models.
Runs once at login via launchd. Saves ~60s on first real translation of the day.
"""
import os
import io
import json
import sys
import time
from PIL import Image, ImageDraw

import requests

# Path to the manhwa/manga prompt config. Override with MT_GPT_CONFIG.
GPT_CONFIG = os.environ.get("MT_GPT_CONFIG", os.path.expanduser("~/.config/mt-stack/gpt_config.yaml"))

SERVER = "http://127.0.0.1:8000"
HEALTH = f"{SERVER}/"
TRANSLATE = f"{SERVER}/translate/with-form/image/stream"

CONFIG = {
    "translator": {
        "translator": "custom_openai",
        "target_lang": "ENG",
        "gpt_config": GPT_CONFIG,
    },
    "detector":  {"detector": "default", "detection_size": 1536, "text_threshold": 0.5, "box_threshold": 0.7, "unclip_ratio": 2.3},
    "ocr":       {"ocr": "48px", "min_text_length": 0, "ignore_bubble": 0},
    "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1024, "inpainting_precision": "bf16"},
    "render":    {"renderer": "manga2eng", "alignment": "auto", "font_size_minimum": -1, "rtl": True},
    "kernel_size": 3, "mask_dilation_offset": 20,
}

def wait_for_server(timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(HEALTH, timeout=2)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def make_dummy_image() -> bytes:
    """Small image with Korean text so OCR + LLM both warm up."""
    img = Image.new("RGB", (800, 600), "white")
    d = ImageDraw.Draw(img)
    d.text((100, 250), "안녕하세요\n반갑습니다", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def main():
    print(f"[warmup] waiting for server at {SERVER}...", flush=True)
    if not wait_for_server():
        print("[warmup] server never came up, aborting", flush=True)
        sys.exit(1)
    print("[warmup] server up, firing dummy translate", flush=True)
    img = make_dummy_image()
    t0 = time.time()
    try:
        r = requests.post(
            TRANSLATE,
            files={"image": ("warmup.png", img, "image/png")},
            data={"config": json.dumps(CONFIG)},
            timeout=600,
            stream=True,
        )
        # Drain the stream so the server processes fully
        total = 0
        for chunk in r.iter_content(65536):
            total += len(chunk) if chunk else 0
        print(f"[warmup] done in {time.time()-t0:.1f}s, status={r.status_code}, drained={total}B", flush=True)
    except Exception as e:
        print(f"[warmup] failed: {e}", flush=True)

if __name__ == "__main__":
    main()
