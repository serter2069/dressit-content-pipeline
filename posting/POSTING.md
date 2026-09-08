# Posting Hub — operator doc

`posting.smartlaunchhub.com` is the single dashboard/DB for ALL content-publishing
pipelines across projects (proto task dressit#1835). One place to see every project's
materials, channels, schedules and item statuses, and to approve/schedule/publish items.

## Architecture

```
browser ──https──▶ nginx (TLS + basic auth) ──▶ 127.0.0.1:8478
                                                   │  server.py  (Python 3.12 stdlib only,
                                                   │  http.server.ThreadingHTTPServer)
                                                   ├── db.py      schema + helpers
                                                   ├── adapters/  one adapter per pipeline
                                                   │              (fshorts, dressit-shorts, ...)
                                                   └── data/posting.db  (SQLite)
```

- **App**: `/root/posting/server.py` — stdlib only (sqlite3, http.server, json, urllib),
  no pip deps. Serves JSON API under `/api/*` and a single-page dashboard at `/`.
  Per-project shareable URLs: `/p/<alias>` boots the dashboard pre-filtered to
  that project (all tabs); unknown alias returns a 404 page listing valid
  project URLs (fshorts#6).
- **DB**: `/root/posting/data/posting.db`. Tables:
  - `projects(alias PK, name, pipeline_kind, pipeline_path, notes, created_at)`
  - `channels(id PK, project_alias, platform, handle, token_path, daily_limit, publish_times_utc json, active)`
  - `items(id PK, channel_id, title, local_path, state, source_ref, scheduled_at, published_url, error, meta json, created_at, updated_at)`
  - Item state machine: `new → review → approved/rejected → scheduled → published/failed`. Side-state `archived` (any state except published → archived → review): hidden junk; the dashboard and /api/overview exclude it by default, `?include_archived=1` or `state=archived` brings it back.
- **nginx**: vhost `/etc/nginx/sites-available/posting.smartlaunchhub.com`
  (symlinked into sites-enabled). 443 + http2, cert
  `/etc/letsencrypt/live/posting.smartlaunchhub.com/` (webroot-issued, auto-renews),
  basic auth via `/etc/nginx/.htpasswd_posting`, proxy_pass to `127.0.0.1:8478`.
  80 redirects to 443 except `/.well-known/acme-challenge/` (renewals).
- **systemd**: `/etc/systemd/system/posting-hub.service`
  (`Restart=always`, `RestartSec=3`). Manage:
  `systemctl status|restart|stop posting-hub`, logs: `journalctl -u posting-hub -f`.

## Credentials

- Dashboard login: user `sergei`, password in `/root/posting/CREDENTIALS.txt` (chmod 600,
  plaintext, root-only). Rotate with:
  `htpasswd -B /etc/nginx/.htpasswd_posting sergei` then update CREDENTIALS.txt.
- Per-channel platform tokens (YouTube etc.) live in the pipeline dirs and are only
  *referenced* by `channels.token_path` — never copied into the hub.
  - fshorts: `/root/fshorts/youtube_token.json`
  - dressit-shorts: `/root/dressit-shorts/token_youtube*.json`
- The hub itself has no auth; it only listens on 127.0.0.1. All auth is nginx-level.

## Thumbnails

`/thumb/<item_id>` resolves in this order (server.py `resolve_thumb_path`):
1. `meta.thumbnail_path` / `meta.thumbnail` (absolute path to an image file) — use
   this for pipelines whose videos share one directory (e.g. chess-events hype mp4s
   in `/root/_chess/out/events/`, thumbs in `out/events/thumbs/<slug>.jpg`).
2. `thumbnail.jpg` inside `local_path` (when it is a job directory).
3. `thumbnail.jpg` next to `local_path` (when it is a file).
Dashboard login user is `serter2069@gmail.com` (nginx htpasswd; pw in CREDENTIALS.txt).

## API cheat sheet

All JSON. Errors are `{"error": msg}` with a proper HTTP code.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/overview` | per-project counts by state |
| GET | `/api/projects` | list projects |
| GET | `/api/channels?project=<alias>` | list channels |
| GET | `/api/items?project=<alias>&state=<state>&limit=<n>` | list items |
| POST | `/api/sync` | run all adapters once, returns counts |
| POST | `/api/items/<id>/transition` | body `{"to": "approved|rejected|scheduled|published|failed", ...optional fields}` |
| POST | `/api/channels` | register a channel |

Examples (from the server itself, bypassing nginx):

```bash
curl -s http://127.0.0.1:8478/api/overview | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8478/api/sync
curl -s -X POST http://127.0.0.1:8478/api/items/fshorts:job42/transition \
     -H 'Content-Type: application/json' -d '{"to":"approved"}'
```

Through the public URL add basic auth: `curl -u sergei:<pw> https://posting.smartlaunchhub.com/api/overview`.

## Onboarding a new project

1. Register the project row (sqlite3 CLI is fine):

```bash
sqlite3 /root/posting/data/posting.db \
  "INSERT INTO projects(alias,name,pipeline_kind,pipeline_path,notes,created_at)
   VALUES('myproj','My Project','dirjobs|sqlite|custom','/root/myproj','notes',datetime('now'));"
```

2. Register its channels (API or SQL):

```bash
curl -s -X POST http://127.0.0.1:8478/api/channels -H 'Content-Type: application/json' -d '{
  "project_alias":"myproj","platform":"youtube","handle":"@mychan",
  "token_path":"/root/myproj/youtube_token.json","daily_limit":3,
  "publish_times_utc":["09:00","15:00","21:00"]}'
```

3. Write an adapter in `/root/posting/adapters/myproj.py` exposing `sync(db) -> dict`
   (look at the fshorts / dressit-shorts adapters for the pattern). Adapters must treat
   pipeline dirs as read-only and map pipeline-specific states onto hub item states.
4. `curl -X POST .../api/sync` and verify counts in the dashboard.

## Ops notes

- Smoke check after any change: `curl -u sergei:<pw> -o /dev/null -w '%{http_code}\n' https://posting.smartlaunchhub.com/api/overview` (expect 200).
  Last full check: `/root/posting/infra-check.log`.
- Cert renewals are handled by certbot's timer; the vhost keeps
  `/.well-known/acme-challenge/` on port 80 for that.
- Cron (root crontab, dressit#1836): `*/15` runs `pipeline.py --tick` in
  `/root/fshorts` (log `logs/cron.log`); offset 7 min runs
  `python3 /root/posting/adapters/sync_all.py` (log `/root/posting/logs/sync.log`).
- Every tick and every sync writes a row to the `runs` table
  (`project_alias`, `pipeline`, `started_at`/`finished_at`, `status`, `summary`, `log`).
  Hub writes from the pipelines are best-effort and never break a tick.
- Adapters treat pipeline dirs as read-only, with two exceptions:
  `adapters/fshorts.py enqueue()` creates new fshorts job dirs (mirroring
  `ingest.create_job`), and `fshorts/pipeline.py` carries hub instrumentation.
  Never modify `/root/dressit-shorts` — its DB is read-only for the hub.
- proto-hub node API (localhost:3901) is unrelated; do not touch.


## Connected Projects
- `dressit-sf` → YouTube `@dressitsf` (DressIt · San Francisco). Path: `/root/event-hero/`. Schedule: hourly jitter — one Short every 60–120 min (randomized) via `/root/event-hero/sf_hourly_scheduler.py` + cron `*/15` (state `sf_scheduler_state.json`, log `logs/sf_scheduler.log`); auto-parks until 07:05 UTC on YouTube quota exhaustion (~6 uploads/day cap at 10k units). SF concert drops, outfit guides for girls 18-28, ChatGPT Nova TTS, zero emojis, 50% referral promo link `https://dressitnow.com/?ref=sf50`. Descriptions auto-filled at upload (promo link, Ticketmaster, artist/tour/venue/date, hashtags).
- `chesstourism` → YouTube `@chesstourevents` (ChessTour Events, channel ID `UCfZDjlXnhvpmXhHOKpNSP6w`, account `goodsgrey@gmail.com`). Path: `/root/_chess/`. Schedule: 90-min jittered interval via `/root/_chess/chess_events_scheduler.py` + cron `*/15` (state `chess_scheduler_state.json`, log `logs/chess_scheduler.log`); auto-parks on YouTube quota exhaustion until ~07:05 UTC. Covers global amateur chess tournaments (dates, prizes, registration links, pinned comment with official URL + chesstourism.com). Zero emojis in titles, descriptions, and comments.
- (удалены fshorts#8, 2026-09-05: `dressit-fshorts`, `dressit-shorts`,
  `street-interview-sf` — проекты выведены из хаба вместе с контентом;
  адаптеры fshorts/dressit_shorts в sync_all.py и fshorts-tick в crontab
  отключены с маркером `DISABLED fshorts#8`.)

## Multi-City Architecture
Each city is managed as an independent project within Posting Hub with its own local channel, referral promo, and concert pipeline. All four city channels post on a jittered schedule — one Short every 30–120 min (randomized) via `/root/event-hero/sf_hourly_scheduler.py` (SF) and `/root/event-hero/city_hourly_scheduler.py --city <la|nyc|miami>` (cron `*/15`, per-city state files, logs `logs/city_scheduler_<city>.log`). NOTE: all 4 channel tokens share ONE GCP project → shared ~10k units/day ≈ 6 uploads/day total; schedulers park until 07:05 UTC on quotaExceeded. City descriptions carry per-city ref links (`?ref=la50` / `nyc50` / `miami50`) via `uploader_city.CITY_REF_LINKS`.
- `dressit-sf`: Live — San Francisco (@dressitsf, promo code `sf50` → https://dressitnow.com/?ref=sf50)
- `dressit-la`: Live — Los Angeles (@dressitla, promo code `la50` → https://dressitnow.com/?ref=la50)
- `dressit-nyc`: Live — New York (@dressitnewyork, promo code `nyc50` → https://dressitnow.com/?ref=nyc50)
- `dressit-miami`: Live — Miami (@dressitmiami, promo code `miami50` → https://dressitnow.com/?ref=miami50)

### Project Deep Dive: `dressit-sf` (DressIt · San Francisco)

The flagship city deployment connecting upcoming Bay Area concerts with curated DressIt outfits.

#### Channel & Credentials
- Platform: YouTube Shorts
- Channel Name: DressIt San Francisco / DressIt · San Francisco
- Handle: `@dressitsf`
- Channel ID: `UC5MuCQuCotqzD4DbIJsdCfA`
- Token / Credentials Path: `/root/dressit-shorts/token_youtube_wheretogosf.json`
- Directory / Source Path: `/root/event-hero/`
- Publishing Schedule: 2 Shorts/day at 16:00 UTC and 21:00 UTC

#### Visual Sequence Standard
Every Short follows the strict 15-second rhythm to guarantee high thumbstop rate, viewer retention, and conversion:
- **0.0 – 2.5s (Hook)**: Real authentic close-up of the specific artist's face from her interview or red carpet (speaking/smiling into camera), OR a clear close-up of her face singing live on stage. (Never generic stock; must establish instant artist recognition).
- **2.5 – 5.8s (Concert Beat)**: Real live stage concert performance of this artist singing her hit song (dynamic lighting, stage presence).
- **5.8 – 8.2s (Vibe & Atmosphere)**: Arena crowd singing along, flashlights in the dark, energy of live music fans.
- **8.2 – 15.0s (Conversion & CTA)**: High-production DressIt fitting room outro with animated DressIt logo overlay and spoken call-to-action directing to `dressitnow.com`.

#### Voiceover Standard
- Engine: ChatGPT Nova (`openai/gpt-audio` via `/root/event-hero/tts_chatgpt.py` using OpenRouter).
- Persona / Style: Natural, vibrant 21-year-old girl friend talking excitedly about the upcoming concert.
- Scripting Rules:
  - Spoken hook includes strictly verified concert date, city, and venue (e.g., "Chase Center", "Bill Graham Civic Auditorium").
  - Clear, natural call-to-action mentioning DressIt outfit inspiration and the 50% first-look bonus.

#### Captions & Subtitles Standard
- Style: Single stream subtitle in lower third.
- Vertical Margin: `MarginV 360` (ensures zero overlap with YouTube UI buttons/player controls).
- Typography: Inter Black, 98pt font size, high-contrast white text with clean black outline/shadow.
- Date Formatting: Explicit four-digit year `2026` in the date beat (e.g., "April 18, 2026").
- Emoji Policy: Strict ZERO emojis across all assets — zero emojis in captions, zero emojis in video overlays, zero emojis in titles, zero emojis in descriptions, zero emojis in pinned comments.

#### Referral & Conversion Mechanics
- Primary Promo Link: `https://dressitnow.com/?ref=sf50`
- Promo Code: `sf50` (50% bonus credits on first published look).
- Tracking & Admin: All activations and conversions monitored via `admin.dressitnow.com/referrals`.

#### Pipeline Execution Commands
- Primary Generation Pipeline: `python3 /root/event-hero/concert_pipeline.py`
- Manual Video Upload: `python3 /root/event-hero/youtube_uploader.py --video <path> --title <title> --description <desc>`
- TTS Generation: `python3 /root/event-hero/tts_chatgpt.py "<text>" /tmp/output.mp3`
- Adapter Sync to Hub: `python3 /root/posting/adapters/sync_all.py` (or via `POST /api/sync`)
