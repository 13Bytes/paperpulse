# Paperpulse - Daily ArXiv Research Summariser

Paperpulse retrieves new papers from ArXiv every day, groups them into themes using the **OpenAI Agents SDK**, and publishes the result as a Jekyll blog post.

The search scope, summarisation persona, and blog branding are all controlled via `config.yaml` — no code changes needed.

## Features

- Configurable ArXiv search by category and/or keyword (see `config.yaml`)
- Two-stage summarisation pipeline: batch summaries → single merged post
- Automatic hyperlinks from the summary text back to the ArXiv paper pages
- Jekyll blog served via Docker; production deployment uses a built-in cron scheduler

## Requirements

- Docker (both dev and prod run entirely in containers)
- An OpenAI API key, or a persisted Codex CLI login for the experimental Codex backend

For local development outside Docker, use Python 3.11+ with a virtual environment and the packages in `api/requirements-dev.txt`.

## Configuration

All user-facing settings live in `config.yaml`:

| Section | What it controls |
|---|---|
| `blog` | Site title, tagline, and per-post front-matter title |
| `search.categories` | ArXiv category codes to include |
| `search.keywords` | Free-text keyword filters (matched against title and abstract) |
| `search.mode` | How categories and keywords are combined (`categories_and_keywords`, `categories_only`, `keywords_only`) |
| `summarization.persona` | Opening of the LLM system prompt — sets expertise framing |
| `summarization.style` | Tone and explanation style injected after the persona |

Secrets and environment-specific values stay in `.env` (never committed):

```
OPENAI_API_KEY=sk-...
PROJECT_ENV=dev          # dev | prod
PROJECT_DIR=/path/to/paperpulse
LLM_BACKEND=openai_api    # openai_api | codex_cli
MIXPANEL_TOKEN=...       # optional analytics
```

The default `openai_api` backend uses `OPENAI_API_KEY` and `OPENAI_MODEL` (default: `gpt-4o-mini`).

### LLM backends

#### OpenAI API backend

This is the default and production-stable path. Set:

```bash
LLM_BACKEND=openai_api
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

#### Codex CLI backend

This experimental backend runs `codex exec` inside the API container and reuses a persisted ChatGPT/Codex login from `CODEX_HOME`. It is intended for a trusted self-hosted server where the whole daily summariser uses the account of whoever performed setup.

Set:

```bash
LLM_BACKEND=codex_cli
CODEX_HOME=/app/.codex
CODEX_MODEL=             # optional; leave empty for Codex default
CODEX_TIMEOUT_SECONDS=900
```

Then build the API image and log in once:

```bash
docker compose build api
docker compose run --rm api-run codex login --device-auth
```

The compose files mount `/app/.codex` as a Docker named volume, so the login survives normal container recreation and image rebuilds. `docker compose down -v` removes named volumes and will require logging in again.

Treat `$CODEX_HOME/auth.json` like a password: do not commit it, paste it into tickets, or share it in chat.

## Development vs Production

### Development (`docker-compose.yml`)

Intended for local iteration. The Jekyll blog is served with `--livereload` so changes to `blog/` are reflected immediately.

`docker compose up` starts both the blog and a persistent API server:

| Service | Port | Purpose |
|---|---|---|
| `blog` | 4000 | Jekyll site with live-reload |
| `api` | 8000 | FastAPI server — handles manual pipeline triggers |

```bash
# First start, or after changing Python code / requirements
docker compose up --build

# Subsequent starts (config/template changes only)
docker compose up
```

> **Always use `--build`** after editing Python source files, `api/requirements.txt`, or the `Dockerfile`. Without it, Docker reuses the cached image and your changes won't be picked up.

#### Manual trigger (dev only)

The API server runs with `MANUAL_TRIGGERS_ALLOWED=true`, which activates a **▶ Run Update Now** button at the bottom of the blog home page (`http://localhost:4000`). Clicking it fires the full ArXiv → LLM → blog post pipeline in the background and polls for completion, showing live status feedback in the UI. Jekyll's live-reload then picks up the new post automatically.

The trigger is intentionally dev-only:
- The button is rendered only when `JEKYLL_ENV=development`
- The `/trigger` route is **not registered at all** when `MANUAL_TRIGGERS_ALLOWED` is unset or `false` — it returns 404 and cannot be reached via Postman or any other client
- Production compose does not set this variable

To invoke the pipeline without the UI:
```bash
# One-shot run (no HTTP server)
docker compose run --rm api-run
```

In `dev` mode (`PROJECT_ENV=dev`), retrieved papers are cached to a pickle file (`data/papers-<date>.pkl`). Re-running within the same day skips the ArXiv API call and reuses the cache, so only the LLM call is made on subsequent triggers.

The `.env` file is bind-mounted read-only into the container so `python-dotenv` can load it automatically.

If `LLM_BACKEND=codex_cli`, the dev API server and one-shot runner both use the same persisted `codex-home` Docker volume.

### Local Python development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r api/requirements-dev.txt
```

Useful local checks:

```bash
pytest
ruff check api
python -m compileall -q api
```

### Production (`docker-compose.prod.yml`)

Intended for a server deployment (e.g. an LXC container behind Nginx Proxy Manager). Key differences from dev:

| Aspect | Dev | Prod |
|---|---|---|
| Jekyll image | Pre-built `jekyll/jekyll:4` | Custom image built from `Dockerfile.jekyll` |
| Jekyll command | `jekyll serve --livereload` | Built as a static site; served by Jekyll's production server |
| API invocation | HTTP server + manual trigger button | Automatic — `supercronic` runs the crontab on schedule |
| Cron schedule | — | Daily at 06:00 UTC (`api/crontab`) |
| `.env` mount | Yes (read-only) | No — env vars are passed directly in the compose file |
| Restart policy | — | `unless-stopped` on both services |
| Networking | Default bridge | Named `web` bridge network |

SSL termination and domain routing are handled externally by Nginx Proxy Manager; this stack only exposes port 4000 on the host.

```bash
# Start the production stack (detached)
docker compose -f docker-compose.prod.yml up -d --build
```

Set `OPENAI_API_KEY` and `OPENAI_MODEL` (optional) in the server environment or a secrets manager before starting when using `LLM_BACKEND=openai_api`. For `LLM_BACKEND=codex_cli`, run `docker compose -f docker-compose.prod.yml run --rm api codex login --device-auth` once so the production `codex-home` volume contains a valid Codex login.

## Project Structure

```
config.yaml              # All user-facing configuration
pyproject.toml           # Project metadata plus pytest/ruff config
docker-compose.yml       # Dev stack
docker-compose.prod.yml  # Production stack
api/
  main.py                # Orchestrator — wires ArXiv → agent → blog post
  server.py              # FastAPI app — exposes /trigger when MANUAL_TRIGGERS_ALLOWED=true
  arxiv_client.py        # ArXiv API queries
  agent.py               # OpenAI Agents SDK: summariser + combiner agents
  codex_agent.py         # Codex CLI backend: summariser + combiner via codex exec
  summary_backend.py     # Selects openai_api or codex_cli backend
  file_handler.py        # Paper pickle cache (dev mode)
  webs.py                # Renders the Jekyll markdown post
  settings.py            # Env var loading, prompt builders, query builder
  models.py              # Shared typed data structures
  utils.py               # PDF extraction and text utilities
  crontab                # Cron schedule for production (supercronic)
  requirements.txt
  requirements-dev.txt
  tests/
    test_main.py         # Unit tests for core logic
    test_server.py       # FastAPI route tests
blog/                    # Jekyll site — posts written here by the API
```

## Running Tests

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r api/requirements-dev.txt
pytest
```

## License

MIT License
