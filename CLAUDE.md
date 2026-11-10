# mt-stack

## Overview
Chrome extension plus local pipeline that translates manhwa/manga panels in place. Glue around manga-image-translator (upstream, not vendored) and Ollama. Personal tool.

## Stack
- Python 3.12 (the upstream venv at ~/manga-image-translator/venv), FastAPI, httpx, Chrome MV3 extension (plain JS), launchd, zsh

## Commands
- Install: `./install.sh`
- Test: `~/manga-image-translator/venv/bin/python -m unittest discover -s tests -v`
- Start/stop/status: `mt-start`, `mt-stop`, `mt-status`
- Logs: `/tmp/mt-ui.log`, `/tmp/mt-translator.log`

## Architecture
`server/mt_ui.py` cache proxy and UI (:8002) in front of upstream (:8000). `extension/` client. `config/gpt_config.yaml` prompt. Settings come from env (MT_HOME, MT_OUT_ROOT, MT_CACHE_MAX_GB, MT_GPT_CONFIG), never hardcoded paths.

## Conventions
- No secrets in the repo. ~/manga-image-translator/.env holds a GEMINI key and must never be copied. The test suite scans for key patterns.
- No hardcoded /Users paths. A test enforces it.
