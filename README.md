# mt-stack

Translates Korean manhwa and Japanese manga panels into English, in place, on the page you are reading. A Chrome extension sends each panel to a local pipeline (detect text, OCR, translate with a local LLM, inpaint, re-typeset) and swaps the image. Everything runs on your machine. Nothing is uploaded anywhere.

It is a personal tool built around [manga-image-translator](https://github.com/zyddnys/manga-image-translator). This repo holds the glue: the caching proxy, the extension, the service management, the tuned prompt and the install script. It ships no manga and no model weights.

History: the first working version was written on 25 and 26 May 2026 and has run as launchd services on my machine since. The May commits here are reconstructed from the original files' modification times. The repo itself was created and packaged in November 2026.

## Measured on an M2 Max, 64 GB

| | |
|---|---|
| Cold chapter, 70 panels | about 3 min |
| Same chapter again (cache) | 5 to 10 s |
| Per panel, miss / hit | about 11 s / under 0.5 s |

Model: `aya-expanse:32b` through Ollama (about 21 GB resident). Numbers are from the author's own runs, logged per panel to `metrics.jsonl`.

## Pieces

```
extension/      Chrome MV3 extension: floating button, auto-scroll, 5 panels in flight, retry failed
server/mt_ui.py cache proxy on :8002 in front of the translator on :8000, LRU eviction, /stats, drag-a-folder UI
server/mt_warmup.py   loads the model once at login so the first panel is not cold
config/gpt_config.yaml   the translation prompt, tuned for manhwa register and name consistency
scripts/        mt-start, mt-stop, mt-status, mt-translate (batch a folder), translate_folder.py
launchd/        service templates, rendered by install.sh
```

Request path: page image, then the extension fetches the bytes (via `chrome.debugger` `Network.loadNetworkResource`, because the image CDNs block page-context fetches), then `POST /translate-cached`. The cache key is SHA-256 over the image bytes and the config, so changing the model or settings never serves a stale translation.

## Install

Needs a working manga-image-translator checkout (default `~/manga-image-translator`, override with `MT_HOME`) and Ollama.

```sh
./install.sh
ollama pull aya-expanse:32b
# chrome://extensions, Developer mode, Load unpacked, pick ./extension
mt-start
```

`.env.example` shows the settings manga-image-translator reads. No real API key is needed for local Ollama.

## Tests

```sh
~/manga-image-translator/venv/bin/python -m unittest discover -s tests -v
```

Covers the cache key, LRU eviction, path handling and a scan that fails if a hardcoded home directory or a key pattern appears in the repo.

## Limits

- One machine, one user. Services bind to 127.0.0.1 and have no auth.
- The `chrome.debugger` route shows Chrome's "being debugged" bar while a chapter translates.
- OCR is tuned for Korean, Japanese and English. Other scripts work with lower accuracy.
- Translation quality is that of the local model. Expect occasional wrong names and tone slips.
- Use it on content you have the right to read. It is a reading aid, not a way to redistribute translations.

MIT licensed.
