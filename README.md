# Mindarr — Watch History Analyzer & Discovery Engine

A containerized analytics engine and recommendation dashboard that analyzes each user's
Plex watch history (enriched via Tautulli), identifies their genre and creator affinities,
queries TMDb for top-rated movies and TV shows they have **never watched before**, and
**1-click requests them via Overseerr**.

Deployable as a **TrueNAS SCALE Custom App** or standalone Docker container. Web-only.

---

## Features

- **Sign in with Plex**: Browser-based OAuth PIN login. The **first user to sign in becomes
  the Administrator/owner**; additional users are authorized if they are the server owner or
  a shared user of the configured Plex server.
- **Multi-user, per-account recommendations**: Watch data, taste profile, seen-index, and
  recommendations are scoped to the logged-in user.
- **Tautulli enrichment (optional)**: When configured, per-user watch history is pulled from
  Tautulli. If Tautulli is not configured — or a user has no Tautulli history — that user
  simply has no recommendations (nothing is pulled).
- **Viewing Trends & Taste Profiling**: Time-decayed genre affinity, rewatch/rating boosts,
  top directors and cast, and release-decade distribution.
- **TMDb Discovery Engine** with **strict unseen deduplication** against cached IMDb, TMDb,
  TVDb IDs and normalized titles.
- **Overseerr Integration**: 1-click request button on each recommendation card.
- **Modern Web Dashboard**: Responsive dark-mode UI with Chart.js charts, plus a background
  sync scheduler.
- **Installable PWA**: Add Mindarr to your home screen or desktop for an app-like experience,
  with offline fallback and a network-first service worker.
- **Sessions**: 1-hour session lifetime; the Plex token is validated at login.

---

## TrueNAS SCALE Deployment

1. On TrueNAS SCALE: **Apps** → **Discover Apps** → **Install Custom App**.
2. Set an **Application Name** (e.g. `mindarr`).
3. Paste the following Compose YAML:

```yaml
version: "3.8"

services:
  mindarr:
    container_name: mindarr
    image: mindarr:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - TZ=Europe/London
      - PLEX_URL=https://10.255.10.30:32400
      - OVERSEERR_URL=http://10.255.10.30:5055
      - TAUTULLI_URL=            # Optional: enables per-user history enrichment
      - CONFIG_DIR=/config
      - PORT=8080
      - HOST=0.0.0.0
      - AUTO_SYNC_HOURS=24
    volumes:
      - /mnt/tank/appdata/mindarr:/config
```
*(Replace the storage path with your dataset.)*

4. Click **Install**, then open `http://<truenas-ip>:8080`.

---

## Running (Docker / local)

```bash
# Docker
docker compose up

# Local
uvicorn plex_recommender.web.app:app --host 0.0.0.0 --port 8080
# or
python -m plex_recommender
```

---

## Web Dashboard Usage

1. Open `http://localhost:8080` and **Sign in with Plex**. The first account becomes the
   Administrator.
2. As the Administrator, open **Settings & Sync**:
   - Enter your free **TMDb API Key** ([themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)).
   - (Optional) Enter your **Overseerr** URL + API Key for 1-click requests.
   - (Optional) Enter your **Tautulli** URL + API Key to enable per-user watch history.
   - Click **Sync Now**.
3. Other authorized users can now sign in and see recommendations tailored to their own
   Tautulli history.
4. Visit **Dashboard & Trends** and **Unseen Recommendations**.

---

## Installing as an App (PWA)

Mindarr is an installable Progressive Web App:

- **Desktop (Chrome/Edge)**: click the install icon in the address bar, or use the
  **Install App** option in the user menu.
- **Android (Chrome)**: use the **Install App** option in the user menu, or the browser's
  "Add to Home screen" prompt.
- **iOS (Safari)**: tap **Share** → **Add to Home Screen**.

Once installed, Mindarr launches in its own standalone window/icon, and a lightweight
service worker keeps the app responsive and shows an offline page if your connection drops.
Caching is **network-first** — the app always prefers fresh data and only falls back to
cached content when offline.

---

## Caching & Sync Freshness

- Recommendations are cached in SQLite keyed by `user_key` + filters
  (`media_type:genre:language:min_rating:limit`).
- Cache entries store the `last_sync_time` version and are cleared whenever a sync completes,
  so newly watched titles and updated taste scores take effect immediately.
- Force a fresh query via the **↻ Refresh** link in the header.
