# Paperpulse

Paperpulse is a multi-user research reader for ArXiv. It collects papers for curated topics,
publishes daily reports, and produces a weekly synthesis of the most important developments.
Readers can follow topics anonymously or sign in with a passwordless email link to synchronize
their subscriptions across browsers.

## Features

- Topic-based ArXiv discovery using configurable categories and keywords
- Daily research reports and weekly cross-report synthesis
- Anonymous subscriptions stored in a signed browser cookie
- Passwordless accounts with single-use magic links
- Personalized archives and public topic pages
- Community proposals for topics, categories, and keywords
- Administrator moderation, topic management, and job history
- Codex CLI and OpenAI API summarization backends
- Atomic job claims, stale-job recovery, and structured job logging
- SQLite persistence with Alembic migrations, foreign keys, and WAL mode

## Architecture

The web application uses FastAPI, Jinja templates, and SQLAlchemy. Reports, accounts,
subscriptions, moderation state, and job history are stored in SQLite. Alembic manages the
database schema.

Scheduled work runs separately from the web process. Daily jobs retrieve papers for each active
topic and create reports; weekly jobs synthesize the previous Monday-through-Sunday reporting
period. The default summarization backend invokes the Codex CLI, while the OpenAI Agents SDK is
available through `LLM_BACKEND=openai_api`.

## Quick start

Requirements:

- Docker with Docker Compose
- A Codex CLI login, or an OpenAI API key when using the API backend

Copy the example environment file and configure at least `SESSION_SECRET` and `ADMIN_EMAILS`:

```bash
cp .env.example .env
```

For local development without SMTP, explicitly set `MAGIC_LINK_DEBUG=true`. This displays sign-in
links in the application and must not be enabled in production.

Build the application, apply migrations, and start the web service:

```bash
docker compose up --build
```

Paperpulse is available at `http://localhost:4000`.

Start the scheduler when automatic report generation is needed:

```bash
docker compose --profile scheduler up --build
```

## Summarization backends

### Codex CLI

Codex CLI is the default backend. Authenticate the shared `codex-home` volume once:

```bash
docker compose run --rm jobs codex login --device-auth
```

The main settings are:

- `LLM_BACKEND=codex_cli`
- `CODEX_HOME=/app/.codex`
- `CODEX_MODEL` for an optional model override
- `CODEX_TIMEOUT_SECONDS` for the job timeout

### OpenAI API

Set the following values to use the OpenAI Agents SDK backend:

```bash
LLM_BACKEND=openai_api
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-4o-mini
```

## Report jobs

Run jobs manually through the jobs service:

```bash
docker compose run --rm jobs python -m api.jobs daily
docker compose run --rm jobs python -m api.jobs weekly
docker compose run --rm jobs python -m api.jobs cleanup
```

The scheduler runs daily reports at 06:00 UTC and weekly reports on Monday at 07:00 UTC. Each
daily report covers the most recently completed 06:00-to-06:00 UTC window. Paper downloads are
shared between topics with identical queries during a batch.

Expired sign-in links and sessions are cleaned up nightly. Job claims prevent duplicate work and
allow abandoned runs to be recovered safely.

## Accounts and administration

Visitors can select topics without an account. Signing in merges the browser selection with the
account's saved subscriptions.

Email addresses in the comma-separated `ADMIN_EMAILS` variable receive administrator access. The
allowlist is evaluated on every request, so removing an address revokes access immediately.
Administrators can:

- create, edit, and archive curated topics;
- approve or reject proposed topics;
- review category and keyword suggestions;
- trigger reports manually;
- inspect report-job history.

Topics require a name, description, at least one valid ArXiv category, and at least one keyword.
Rejected proposals include a review reason visible to their proposer.

## Configuration

`config.yaml` defines site branding and the summarization persona and style. Topics and their
ArXiv categories and keywords are managed through the application and stored in the database.

Runtime configuration is supplied through environment variables. Important values include:

- `DATABASE_URL`
- `PUBLIC_BASE_URL`
- `SESSION_SECRET`
- `SESSION_COOKIE_SECURE`
- `ADMIN_EMAILS`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and optional SMTP credentials
- `MAGIC_LINK_DEBUG`
- `LLM_BACKEND` and its backend-specific settings
- `LOG_LEVEL`

See `.env.example` and the Compose files for the complete configuration.

## Production

Configure the required values referenced by `docker-compose.prod.yml`, including:

- an HTTPS `PUBLIC_BASE_URL`;
- a long, random `SESSION_SECRET`;
- the administrator email allowlist;
- SMTP delivery settings.

Then start the migration, web, and scheduler services:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The web container binds to `127.0.0.1:4000`. A host reverse proxy can provide TLS termination.
The application exposes `/health` for liveness and `/ready` for database and migration readiness.
Production cookies are secure by default, and magic-link debug output is disabled.

The provided deployment is designed for one web instance and one scheduler instance. SQLite WAL
mode supports this workload, but horizontal scaling or higher write volume should use a
client/server database.

## Database operations

The default database is stored at `data/paperpulse.db`. Apply migrations manually with:

```bash
docker compose run --rm migrate alembic upgrade head
```

Web and job processes require the expected schema revision before starting.

For an online SQLite backup, write the snapshot outside `data/` and verify its integrity:

```bash
mkdir -p backups
sqlite3 data/paperpulse.db ".backup backups/paperpulse-$(date +%F).db"
sqlite3 backups/paperpulse-$(date +%F).db "PRAGMA integrity_check;"
```

Back up `config.yaml` with the database. Codex authentication is stored separately in the
`codex-home` Docker volume. Do not copy only the main database file while live WAL writes are in
progress unless the backup tool also captures the WAL state.

## Local Python development

Python 3.11 or newer is supported:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r api/requirements-dev.txt
pytest
ruff check api migrations
python -m compileall -q api migrations
```

The test suite covers authentication, CSRF protection, authorization, moderation, subscriptions,
ArXiv query construction, concurrent job execution, personalization, and Markdown sanitization.

## License

MIT License
