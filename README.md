# Paperpulse

Paperpulse is a multi-user ArXiv research reader. It generates shared daily reports for
administrator-approved topics and a weekly synthesis for each topic. Visitors choose the topics
they care about without creating an account; optional passwordless email sign-in synchronizes
those choices and enables community proposals.

## What changed

The original Jekyll site has been replaced by a server-rendered FastAPI application backed by
SQLite. The existing ArXiv retrieval and OpenAI Agents SDK/Codex CLI summarizers are retained,
but report generation now runs independently for every active topic.

- Anonymous topic selection stored in a signed browser cookie
- Magic-link accounts for cross-browser synchronization
- Explicit magic-link confirmation so mail scanners cannot consume sign-in links
- Public topic catalog, topic archives, daily reports, and weekly reports
- Moderated topic, ArXiv category, and keyword proposals
- Administrator dashboard with curated topic creation and archiving
- SQLite persistence with Alembic migrations, foreign keys, and WAL mode
- Daily jobs at 06:00 UTC and Monday weekly jobs at 07:00 UTC
- Atomic job claims, stale-job recovery, and an administrator job history
- One-time import of the existing `config.yaml` topic and Jekyll posts

## Local development

Requirements: Docker and an existing Codex CLI login. Copy `.env.example` to `.env`, change
`SESSION_SECRET`, and set `ADMIN_EMAILS` to the address that should become the first admin.
Development defaults `MAGIC_LINK_DEBUG=true`, so the sign-in page displays the generated link
when SMTP is not configured. Never enable that option in production.

Build the application and run migrations:

```bash
docker compose up --build
```

The application is available at `http://localhost:4000`. The scheduler is opt-in during
development:

```bash
docker compose --profile scheduler up --build
```

Log the shared summarizer into Codex once. The named volume persists the login:

```bash
docker compose run --rm jobs codex login --device-auth
```

Run report jobs manually:

```bash
docker compose run --rm jobs python -m api.jobs daily
docker compose run --rm jobs python -m api.jobs weekly
```

The job runner uses `LLM_BACKEND=codex_cli` by default. `openai_api` remains supported when a
deployment explicitly configures it.

## Initial import

After migrations, import the original configuration and Jekyll posts once:

```bash
docker compose run --rm jobs python -m api.importer --topic-name "AI for Engineering"
```

The importer is idempotent. It creates an active topic from the categories and keywords in
`config.yaml`, imports existing files from `blog/_posts`, and registers redirects for their
original Jekyll URLs. Keep the `blog` directory until this import has completed and been checked.

Further topics such as AI General or 3D Printing should be created in the admin interface with
deliberately chosen categories and keywords.

## Accounts and administration

Visitors can choose topics immediately. Signing in merges the browser selection with the
account's saved subscriptions. New topics require a name, description, at least one valid ArXiv
category, and at least one keyword. Later category and keyword suggestions follow the same review
process.

Email addresses listed in the comma-separated `ADMIN_EMAILS` variable receive administrator
rights. The allowlist is checked on every request, so removing an address revokes administrator
access immediately. Administrators can:

- approve or reject proposed topics and their initial terms as one bundle;
- approve or reject later category and keyword suggestions individually;
- create active curated topics directly and add approved terms;
- archive topics without deleting historical reports.

Rejected proposals require a review reason and remain visible to their proposer.

## Production

Set all required values referenced by `docker-compose.prod.yml`, especially:

- `PUBLIC_BASE_URL` with the external HTTPS origin
- a long random `SESSION_SECRET`
- `ADMIN_EMAILS`
- `SMTP_HOST` and `SMTP_FROM`, plus credentials when required

Then start the migration, web, and scheduler services:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The web container binds to `127.0.0.1:4000` and exposes `/health` for liveness and `/ready` for
database and migration readiness. TLS termination can remain in the host reverse proxy. Production
cookies are secure, forwarded client addresses are accepted only through that local proxy boundary,
and magic-link debug output is disabled.

The deployment intentionally supports one web instance and one scheduler instance. SQLite WAL
mode permits their normal concurrent reads and writes, but this v1 is not intended for horizontal
scaling.

## Database maintenance and backups

The database is stored at `data/paperpulse.db`. Before upgrades, stop the web and scheduler or use
SQLite's online backup command to produce a consistent snapshot. Back up the database together
with `config.yaml`; Codex login state lives separately in the `codex-home` Docker volume.

Apply migrations manually when needed:

```bash
docker compose run --rm migrate alembic upgrade head
```

The web and job processes no longer create tables automatically. They require the migration service
to complete first, and the web process refuses to start when the database revision is behind.

For an online SQLite backup, create the destination outside `data/` and verify that it can be opened:

```bash
mkdir -p backups
sqlite3 data/paperpulse.db ".backup backups/paperpulse-$(date +%F).db"
sqlite3 backups/paperpulse-$(date +%F).db "PRAGMA integrity_check;"
```

Expired sign-in links and sessions are removed nightly by the scheduler. They can also be cleaned
manually with `docker compose run --rm jobs python -m api.jobs cleanup`.

Do not copy only the main `.db` file while live WAL writes are in progress unless the backup tool
also handles the WAL state.

## Local Python checks

Python 3.11 or newer is supported:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r api/requirements-dev.txt
pytest
ruff check api migrations
python -m compileall -q api migrations
```

The test suite covers authentication and CSRF flows, live administrator authorization, moderation,
subscriptions, query construction, concurrent daily and weekly execution, web personalization,
Markdown sanitization, and legacy import.

## Configuration boundary

`config.yaml` remains the source for branding and summarization persona/style. Topic-specific
categories and keywords move into SQLite and are managed through the application. Runtime secrets,
mail settings, database location, administrator identities, and backend selection remain environment
variables.

## License

MIT License
