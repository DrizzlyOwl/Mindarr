# AGENTS.md

Guidance for agents and maintainers working on **Mindarr** (package: `plex_recommender`).

## Overview
FastAPI web app that analyzes per-user Plex watch history (**primary source: Plex API**,
optionally enriched via Tautulli), builds a taste profile, and recommends unseen
movies/shows via TMDb, with optional 1-click Overseerr requests.

## Tech stack
- Python >=3.9, FastAPI + Uvicorn, Jinja2 templates (`plex_recommender/web/templates`),
  Tailwind CSS (compiled ahead-of-time to `plex_recommender/web/static/css/tailwind.css`,
  committed to the repo — see README for rebuild steps; no CDN/JIT at runtime)
- SQLite via stdlib `sqlite3` (raw SQL, no ORM) in `db/`
- plexapi + `requests` (Plex, TMDb, Overseerr, Tautulli, plex.tv OAuth)
- APScheduler for background sync
- pytest (`tests/`); config in `pyproject.toml` (`pythonpath=["."]`, `testpaths=["tests"]`)

## Layout
- `plex_recommender/config.py` — `Settings` singleton; env loaded from `config/.env`;
  `save_setting()` persists to `.env`. Snapshots real OS/Docker env vars at import
  (`ENV_MANAGED_KEYS`); `settings.is_locked(key)` reports env-managed (locked) settings
  and `save_setting()` is a no-op for them.
- `plex_recommender/db/` — package split by concern (schema + connection factory in
  `__init__.py`, migrations in `migrations.py`, shared helpers in `_util.py`, and
  data access in `media.py`, `watch.py`, `users.py`, `recommendations.py`,
  `engagement.py` (votes/dismissals), `jobs.py` (job_history), `logs.py` (system_logs)).
  Import from the specific submodule, e.g.
  `from plex_recommender.db.watch import get_user_media_items`.
- `plex_recommender/auth.py` — Plex OAuth PIN flow, plex.tv identity/access, shared user discovery
- `plex_recommender/sync.py` — shared Plex library metadata scan + per-user history
  (Plex API primary, Tautulli enrichment), shared user auto-registration
- `plex_recommender/jobs.py` — `JobManager` coordinating background tasks (`sync`,
  `recommendations`), state tracking, and chaining
- `plex_recommender/analyzer.py` — `TasteAnalyzer` (genre/creator/decade scoring, recency
  decay, narrative `summary` with cited metrics + data-source provenance)
- `plex_recommender/recommender.py` — `ContentRecommender` (TMDb discovery, seen-exclusion, cache)
- `plex_recommender/community.py` — anonymized cross-user leaderboard/comparison/badges,
  sourced from Tautulli when configured or the local DB otherwise (`/community` route)
- `plex_recommender/poster_cache.py` — local on-disk TMDb poster image caching with TTL sweep
- `plex_recommender/health.py` — persisted integration health status (TMDb/Overseerr/Tautulli)
  backed by `app_settings`; written by the `healthcheck` background job, read by the
  admin health banner (`layout.html`) and the Settings page status badges.
- `plex_recommender/logs.py` — `LogHandler`, a `logging.Handler` that persists app log
  records into the `system_logs` table for the admin-only `/logs` viewer
- `plex_recommender/discovery/` — `tmdb.py`, `overseerr.py`, `tautulli.py`
- `plex_recommender/web/app.py` — routes, sessions, scheduler lifespan, sync concurrency guard
- `plex_recommender/web/templates/` — `layout.html`, `login.html`, `index.html`,
  `recommendations.html`, `settings.html`, `jobs.html`, `system.html`, `welcome.html`,
  `community.html`, `logs.html`, `sources.html`
- `plex_recommender/__main__.py` — `python -m plex_recommender` launches uvicorn

## Conventions
- **API clients:** class-based, mirroring `discovery/overseerr.py` — constructor reads
  `base_url`/`api_key` from `settings` with fallback, `_get_headers()`/`_request()` helpers,
  `test_connection() -> (bool, str)`, `is_configured() -> bool`, module-level singleton at
  bottom. `api_key`/`base_url` are `@property` that read `settings` live (not a snapshot at
  construction), so config changes (e.g. a corrected API key) take effect without a process
  restart. Use `logging.getLogger(__name__)` and `requests` timeouts. Never raise to callers
  for optional integrations — degrade gracefully.
- **DB:** raw SQL; open via `get_connection()` (Row factory), commit + close each op.
  JSON-encode list fields (genres/directors/etc.). Use `INSERT OR IGNORE`/`ON CONFLICT`.
- **Config:** add new settings to both `__init__` and `reload()`; env keys UPPER_SNAKE,
  attributes lower_snake. Secrets stored in `config/.env`, never committed. Values set via
  real OS/Docker env vars are locked (see `ENV_MANAGED_KEYS` / `settings.is_locked`);
  Settings UI disables those inputs.
- **Local Plex over HTTPS:** SSL verification is scoped per-host via
  `http_client.make_session(base_url)` — disabled only for LAN/private/loopback
  hosts or `.local`/`localhost` (typical for a self-signed local Plex server),
  auto-suppressing the `urllib3` insecure-request warning; public hosts (e.g.
  plex.tv) always verify. Use this factory instead of constructing
  `requests.Session()` directly for any Plex/local-network HTTP call.

## Multi-user model (Overseerr-style)
- Login via Plex OAuth PIN; identity from `plex.tv /users/account.json`.
- **First user to log in becomes the Administrator/owner**; the admin token doubles as
  the server `PLEX_TOKEN` (library metadata scan + access checks). Admin configures all
  integrations in Settings.
- Non-admins authorized only if owner or a shared user of the configured server's
  `machineIdentifier` (`check_user_access`).
- **Sessions:** Starlette `SessionMiddleware`, **1-hour TTL**. Plex token validated
  **only at login**; revocation exposure bounded by session expiry (no per-request ping).
- **Scoping:** watch data, taste profile, seen-index, and recommendations cache are
  **per `user_key`** (Plex.tv account id). Rich media metadata is shared in `media_items`
  (joined by `item_id`); the per-user relation lives in `user_media`.

## Onboarding (`/welcome`, first-visit tour)
- `users.onboarded_at` / `users.last_seen_at` (nullable TIMESTAMPs) track onboarding
  completion and login recency. `db.touch_last_seen(user_key)` is called once per login
  (in the `/api/auth/poll` callback, both admin and shared-user branches) and returns the
  *previous* `last_seen_at`, which is stashed in the session as `previous_last_seen` for a
  one-time "Welcome back" caption on the next dashboard render (only shown if > 24h old).
  `db.mark_onboarded(user_key)` is idempotent (no-ops if already set).
- **Admin first-run wizard (`GET /welcome`, `POST /welcome/complete`):** an `onboarding_gate`
  HTTP middleware in `web/app.py` redirects an admin to `/welcome` whenever
  `not onboarded_at` and setup is incomplete (`admin_setup_incomplete()`: missing
  `TMDB_API_KEY`, missing `PLEX_MACHINE_ID`, or no `last_sync_time`). The gate allowlists
  `/welcome`, `/logout`, `/login`, `/api/*`, `/static/*` to avoid redirect loops. The wizard
  is a client-side stepper (Plex → TMDb → optional Overseerr/Tautulli → initial sync) that
  saves each step by POSTing the relevant fields to the existing `/api/settings/save`
  endpoint (no new save endpoints) — unlisted `Form(None)` fields are treated as "no
  change" by that handler, so partial per-step submissions are safe. "Skip setup for now"
  and the final "Take me to my recommendations" both call `POST /welcome/complete`, which
  marks the current user onboarded unconditionally.
  - **Middleware ordering matters:** `SessionMiddleware` must be registered (via
    `app.add_middleware`) *after* `onboarding_gate` is registered (via
    `@app.middleware("http")`), since the last-registered middleware becomes outermost and
    needs to run first to populate `request.session` before the gate reads it.
- **First-visit welcome tour (shared users only):** a dismissible 3-slide modal in
  `layout.html`, gated by `show_first_visit_tour` (`True` when the user is non-admin and
  `onboarded_at IS NULL`). Admins never see it — the wizard is their tour. Dismissing
  (click, `Esc`, or finishing) fires `POST /welcome/complete` via `fetch`.
- Non-admin users with no synced history yet see a "your picks are being prepared" waiting
  state on `/` instead of the admin's "Connect Plex Server" empty state, with a live sync
  progress bar (polls `/api/sync/status`) or a 60s soft-poll if no sync is running.

## Watch history & sync (`sync.py`)
- **Two sources per user, both optional/graceful:**
  1. **Plex API (primary):** `sync_plex_user_history()` uses the admin/server token.
     Plex play history is keyed by a **server-local** `systemAccount` id, NOT the plex.tv
     account id. `_resolve_plex_account_id()` maps `user_key` → local id: owner/admin is
     account id `1`; others matched by username/email/title. No match ⇒ pull nothing.
  2. **Tautulli (enrichment):** see Tautulli section below.
- `sync_library_metadata()` scans Plex libraries into the shared `media_items` cache only.
- **Episode rollup:** episodes are attributed to their parent **show** (genres/cast/creators
  live at show level) for the taste profile; the raw play event is still recorded against
  the watched item.
- **Source provenance:** `watch_events.source` is `'plex'` or `'tautulli'`; `event_key`
  includes the source so both coexist. `get_watch_source_breakdown()` powers the Taste
  Profile card's data-source `<details>`.
- **Metadata preservation:** `upsert_media_item` keeps existing `genres`/`directors`/`actors`
  when an incoming upsert has empty lists (avoids wiping good data on a failed metadata fetch).
- **Concurrency:** a single sync runs at a time. `web/app.py` guards with `sync_lock` +
  `sync_state`; `POST /api/sync` returns **409** if a sync is already active (scheduled job
  skips if one is running). The nav Sync button reflects/blocks on `is_syncing`.
- **Analyzer dates:** `parse_date()` returns naive datetimes (tz stripped) so comparisons
  with `datetime.now()` never raise.

## Tautulli (optional enrichment)
- Configured by admin (`TAUTULLI_URL`, `TAUTULLI_API_KEY`). If **not configured**, no
  enrichment occurs. If a user has **no match/history** in Tautulli, pull **nothing**.
- Only enriches when Tautulli **monitors the linked Plex server** (`monitors_server()` vs
  `PLEX_MACHINE_ID`); Settings warns on a server mismatch. History filtered by `machine_id`.
- Client (`discovery/tautulli.py`) uses `/api/v2?cmd=...&out_type=json`; key commands:
  `get_server_info` (pms_identifier), `get_users`, `get_history`, `get_metadata`
  (GUIDs + title/year/genres/directors/actors). Reads `base_url`/`api_key` live from
  `settings` via properties (config changes take effect without restart).

## Admin pages (under Settings, admin-only)
- Sub-nav tabs: **Configuration** (`/settings`), **Jobs** (`/settings/jobs`),
  **System** (`/settings/system`).
- **Jobs:** `job_history` table records every manual/scheduled/chained sync and recommendation
  run (status, duration, detail). `JobManager` manages active tasks, scheduling, and UI
  actions via `POST /api/jobs/run` and `GET /api/jobs/status`.
- **System:** DB footprint + per-table row counts (`get_database_stats`), per-user data
  summary with last-seen (`get_user_data_summary`), and per-user data wipe
  (`clear_user_data`, keeps the account, resets watch data + recs).

## Integration health monitoring
- `JobManager.run_healthcheck()` (job id `healthcheck`, scheduled every
  `HEALTHCHECK_INTERVAL_HOURS`, default 1h) tests TMDb/Overseerr/Tautulli connectivity via
  each client's `test_connection()` and persists results via `health.record_health()`.
- **TMDb is required** and always checked (a missing/invalid key is itself the failure).
  **Overseerr/Tautulli are optional**: checked only when `is_configured()`; otherwise their
  health is cleared (`health.clear_health()`), never flagged unhealthy, so admins who
  deliberately skip an optional integration aren't nagged.
- **Tautulli's server-mismatch check is folded in**: a reachable Tautulli that
  `monitors_server()` a different Plex server than the one linked here is recorded as
  unhealthy (same condition Settings already warns about live, now also caught proactively).
- Persisted in `app_settings` as `health_<name>_ok/message/checked_at` (name: `tmdb`,
  `overseerr`, `tautulli`) — no schema migration needed.
- Surfaced via a persistent (non-dismissible) admin-only banner in `layout.html`, rendered
  from the Jinja global `get_unhealthy_integrations()` (registered in `web/app.py`), plus a
  live TMDb status badge on the Settings page (mirroring the existing Overseerr/Tautulli
  badges) computed fresh on every page load via `tmdb.test_connection()`.

## Database tables
`users` (has `onboarded_at`, `last_seen_at`), `user_media`, `media_items`, `watch_events`
(has `source`), `seen_identifiers` (PK includes `user_key`), `recommendations_cache`,
`job_history`, `app_settings`, `user_dismissals`, `user_votes`, `system_logs`.
`db.migrations.run_all()` (called from `db.init_db()`) handles additive column
migrations (e.g. `watch_events.source`) and one-time data reconciliation.

## Recommendations cache
- Keyed by `user_key` + filters; invalidated when `last_sync_time` changes. Clear via
  `clear_recommendations_cache()` after a sync.

## Running
- Web: `uvicorn plex_recommender.web.app:app --host 0.0.0.0 --port 8080`
- Docker: `docker compose up` (entry launches uvicorn; **CLI has been removed**)
- Tests: `pytest`

## PWA (installable app)
- `plex_recommender/web/static/manifest.json` — web app manifest (name, icons,
  `standalone` display, theme/background `#0b0f19`, shortcuts to `/`,
  `/recommendations`, `/community/`).
- `plex_recommender/web/static/sw.js` — service worker, served at root (`/sw.js`,
  not `/static/sw.js`) via a dedicated FastAPI route so `Service-Worker-Allowed: /`
  gives it full-app scope. **Caching strategy is network-first**: every GET tries
  the network, caches the response on success, and only falls back to the cache
  (or the precached `/offline` page for navigations) when the network fails.
  Non-GET requests and cross-origin requests are never intercepted.
- `plex_recommender/web/templates/offline.html` — standalone (no layout
  inheritance) offline fallback page precached by the service worker.
- Icons live in `plex_recommender/web/static/icons/` (`icon.svg`, `icon-192.png`,
  `icon-512.png`, `icon-512-maskable.png`, `apple-touch-icon.png`, favicons).
- Routes `/manifest.json` (+ `/manifest.webmanifest` alias), `/sw.js`, and
  `/offline` are allowlisted in `_ONBOARDING_GATE_ALLOWLIST` in `web/app.py` so
  they're reachable pre-login and during first-run onboarding.
- `layout.html` and `login.html` both register the service worker and include
  the manifest/icon/theme-color meta tags; `layout.html` additionally wires up
  a `beforeinstallprompt`-driven "Install App" action in the user dropdown and
  mobile menu.
- When bumping the app version, also update the `CACHE_VERSION` constant in
  `sw.js` so clients pick up a fresh cache instead of reusing stale entries.

## Releases & versioning
- Canonical version lives in `plex_recommender/__init__.py` (`__version__`) and
  must be kept in sync with `pyproject.toml`'s `[project] version`.
- **Every version bump must be tagged and published as a GitHub release.**
  After merging the version bump:
  1. `git tag -a vX.Y.Z -m "vX.Y.Z: <short summary>"`
  2. `git push origin main --tags` (or push the tag explicitly)
  3. `gh release create vX.Y.Z --title "vX.Y.Z: <short summary>" --notes "<changelog>"`
- Do not tag/release without an accompanying version bump commit, and don't bump
  the version without eventually tagging + releasing it.

## Gotchas
- No CLI — the app is web-only; don't reintroduce `cli.py` entry points.
- Keep optional-integration failures non-fatal to the sync/recommendation flow.
- When adding a setting, update `config.py` (both methods), `settings.html`, and
  `save_settings` in `web/app.py`.
- Server-wide/global watch data was intentionally discarded in the multi-user migration;
  data is per-user going forward.
- Plex history filters on the **server-local** account id, never the plex.tv `user_key`.
- `SESSION_SECRET` auto-generates into `config/.env` on first run; changing it invalidates
  all active sessions.
- `SESSION_HTTPS_ONLY` (default `false`) sets the `Secure` flag on the session cookie.
  Only enable it when the app is served over TLS (directly or behind a reverse proxy);
  enabling it over plain HTTP silently breaks login since the browser will refuse to
  send the cookie back.
- **Tailwind is compiled, not CDN.** Editing a template's classes has no visible effect
  until you recompile `plex_recommender/web/static/css/tailwind.css` (see README) and
  commit the updated file — there is no runtime JIT fallback anymore.
