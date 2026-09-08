# posting hub

Central dashboard and SQLite store for all content-publishing pipelines
(posting.smartlaunchhub.com). Pure Python 3.12 stdlib — no pip deps.

## Layout

- `db.py` — schema + query helpers + seed data. Owns the DB.
- `server.py` — HTTP server: JSON API under `/api/*`, single-page
  dashboard at `/`.
- `adapters/` — per-pipeline sync adapters (owned by the ADAPTERS agent);
  `sync_all.py` is shelled out by `POST /api/sync`.
- `data/posting.db` — SQLite database.

## Run

```bash
python3 /root/posting/server.py            # binds 127.0.0.1:8478
```

nginx proxies posting.smartlaunchhub.com to 127.0.0.1:8478 and handles
basic auth — the app itself has none and binds loopback only.

## Database

Path: `data/posting.db` (override with env `POSTING_DB`, used by tests).

- `projects(alias PK, name, pipeline_kind, pipeline_path, notes, created_at)`
- `channels(id PK, project_alias, platform, handle, token_path,
  daily_limit, publish_times_utc /*json array*/, active)`
- `items(id PK, channel_id, title, local_path, state, source_ref,
  scheduled_at, published_url, error, meta /*json*/, created_at, updated_at)`
- `runs(id PK, project_alias, pipeline, started_at, finished_at,
  status /*running|ok|error*/, summary, log, created_at)` — pipeline run log

Item state machine:

```
new -> review -> approved -> scheduled -> published
             \-> rejected -> review        \-> failed -> scheduled

archived: side-state for hidden items (test junk, dead links);
anything but published -> archived, archived -> review. Hidden by
default in the dashboard and /api/overview; see ?include_archived=1.
```

## API (all JSON; errors are `{"error": msg}` with proper codes)

- `GET /api/overview` — per-project counts by state
- `GET /api/projects`
- `GET /api/channels?project=ALIAS` (project filter optional)
- `GET /api/items?project=&state=&limit=` (limit 1..1000, default 200)
- `GET /api/runs?project=&limit=` — runs newest first (limit 1..500,
  default 50)
- `GET /media/<item_id>` — streams the item's video (`video/mp4`) with
  HTTP Range support (200/206/416; 404 JSON when missing). Resolution:
  fshorts items store the job dir in `local_path` → `<dir>/final.mp4`;
  dressit-shorts items store the file path directly
- `POST /api/sync` — runs `adapters/sync_all.py` (120s timeout) and
  returns its stdout JSON; 502 when the script is missing/failed
- `POST /api/runs` — record a run; body `{"project_alias", "pipeline",
  "status"?, "summary"?, "log"?, "started_at"?, "finished_at"?}` →
  `{"id"}`; status defaults to `ok`, must be `running|ok|error`
- `POST /api/delegate` — body `{"project_alias", "title", "notes"?}`;
  loads the project's adapter (`fshorts` → `adapters.fshorts`,
  `dressit-shorts` → `adapters.dressit_shorts`) and calls
  `enqueue(title, notes)` when defined → `{"queued": "<item id>"}`;
  501 `{"error": "project has no enqueue"}` otherwise
- `POST /api/items/<id>/transition` — body `{"to": "approved|rejected|
  scheduled|published|failed", "scheduled_at"?, "published_url"?,
  "error"?, "meta"?}`; validates against the state machine
- `POST /api/channels` — register/update a channel; body needs
  `project_alias` + `platform` (optional: `handle`, `token_path`,
  `daily_limit`, `publish_times_utc`, `active`)

## Selftests (offline, temp DB, no network)

```bash
python3 db.py --selftest
python3 server.py --selftest
```
