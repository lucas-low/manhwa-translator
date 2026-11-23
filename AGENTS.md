# AGENTS.md

Chrome extension plus a local pipeline that translates manhwa and manga panels in place. It is glue around manga-image-translator (upstream, not vendored) and Ollama. Read [README.md](README.md) for what it does and how a panel travels.

## Commands

| Task | Command |
|---|---|
| Test | `~/manga-image-translator/venv/bin/python -m unittest discover -s tests -v` |
| Install services | `./install.sh` |
| Start, stop, inspect | `mt-start`, `mt-stop`, `mt-status` |
| Batch a folder | `mt-translate <folder>` |
| Clear the cache | `curl -X POST http://127.0.0.1:8002/cache/clear` |

Use the upstream venv Python for tests. `pytest` is not installed there, so tests are plain `unittest`.

## Layout

- `server/mt_ui.py` is the cache proxy and dashboard on `:8002`. It sits in front of the upstream translator on `:8000`.
- `server/mt_warmup.py` pre-loads models at login.
- `extension/` is the MV3 client. `config/gpt_config.yaml` is the prompt.
- `scripts/` holds the zsh commands. `launchd/` holds plist templates with `__REPO__` and `__MT_HOME__` placeholders that `install.sh` fills in.

## Conventions

- Settings come from the environment (`MT_HOME`, `MT_OUT_ROOT`, `MT_CACHE_MAX_GB`, `MT_GPT_CONFIG`). No hardcoded home paths. A test enforces this.
- No secrets in the repo. A test scans for key-shaped strings.
- Python 3.12, FastAPI and httpx on the server, plain JS in the extension, zsh for scripts.
- Commit messages use conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`).

## Boundaries

Always: run the test suite before committing. Keep `README.md` and this file in step with behavior changes.

Ask first: running `install.sh`, `mt-start` or `mt-stop`. They change launchd services and load or free about 22 GB of model memory on the owner's machine.

Never: read, copy or commit `~/manga-image-translator/.env`. Never commit anything from `MT_OUT_ROOT`, model weights or manga images.

## Gotchas

- The cache key is SHA-256 over the image bytes and the request config JSON. It does not include the Ollama model or the contents of `gpt_config.yaml`. After changing either, clear the cache or old translations come back.
- Service labels are `dev.mt-stack.*` for installs from this repo. The author's older local setup used `com.lucaslow.*`.
- Image fetching goes through `chrome.debugger` on purpose. Plain `fetch` from the page is blocked by the image CDNs.
