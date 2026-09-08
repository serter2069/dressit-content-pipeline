"""Shared helpers for posting-hub adapters.

Stdlib only. Adapters open the hub DB directly (sqlite3) and upsert items
idempotently. The canonical schema is owned by db.py (CORE); we mirror it
here with CREATE TABLE IF NOT EXISTS so adapters work standalone/offline
and never conflict with CORE's initialization.

DB path resolution order: explicit argument > $POSTING_DB > default path.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = "/root/posting/data/posting.db"

# Hub item state machine ranks: new -> review -> approved/rejected
#   -> scheduled -> published/failed.
# Used to avoid downgrading a hub-side state (e.g. a user scheduled an
# item in the hub; a pipeline re-sync must not reset it back).
# 'archived' outranks everything: pipeline sources never know about
# hub-side hiding, so a sync must never resurrect an archived item.
STATE_RANK = {
    "new": 0,
    "review": 1,
    "approved": 2,
    "rejected": 2,
    "scheduled": 3,
    "published": 4,
    "failed": 4,
    "archived": 5,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
    alias TEXT PRIMARY KEY,
    name TEXT,
    pipeline_kind TEXT,
    pipeline_path TEXT,
    notes TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_alias TEXT,
    platform TEXT,
    handle TEXT,
    token_path TEXT,
    daily_limit INTEGER,
    publish_times_utc TEXT, -- json array
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items(
    id TEXT PRIMARY KEY,
    channel_id INTEGER,
    title TEXT,
    local_path TEXT,
    state TEXT,
    source_ref TEXT,
    scheduled_at TEXT,
    published_url TEXT,
    error TEXT,
    meta TEXT, -- json
    created_at TEXT,
    updated_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path(explicit: str | None = None) -> str:
    return explicit or os.environ.get("POSTING_DB") or DEFAULT_DB_PATH


def connect(path: str | None = None) -> sqlite3.Connection:
    p = db_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ensure_project(conn: sqlite3.Connection, alias: str, name: str,
                   pipeline_kind: str, pipeline_path: str) -> None:
    """Register the project row if missing (never overwrites)."""
    conn.execute(
        "INSERT OR IGNORE INTO projects(alias, name, pipeline_kind,"
        " pipeline_path, created_at) VALUES (?,?,?,?,?)",
        (alias, name, pipeline_kind, pipeline_path, utcnow()))


def find_channel_id(conn: sqlite3.Connection, project_alias: str,
                    platform: str = "youtube") -> int | None:
    """Look up the channel row registered for this project (via the hub
    API or seeds). Returns None when no channel is registered yet."""
    row = conn.execute(
        "SELECT id FROM channels WHERE project_alias=? AND platform=?"
        " AND active=1 ORDER BY id LIMIT 1",
        (project_alias, platform)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM channels WHERE project_alias=? AND active=1"
            " ORDER BY id LIMIT 1", (project_alias,)).fetchone()
    return row["id"] if row else None


def upsert_item(conn: sqlite3.Connection, *, item_id: str,
                channel_id: int | None, title: str | None,
                local_path: str | None, state: str,
                source_ref: str | None = None,
                scheduled_at: str | None = None,
                published_url: str | None = None,
                error: str | None = None,
                meta: dict | None = None) -> bool:
    """Insert or update one item. Returns True if the row changed.

    Idempotent: re-running with identical pipeline data performs an UPDATE
    that sets the same values (rowcount may still count it; we compare
    first and report only real changes). Never deletes.

    State guard: never downgrade an existing state to a lower rank unless
    the new state is terminal (published/failed) — pipeline sources don't
    know about hub-side scheduling.
    """
    now = utcnow()
    meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else None
    existing = conn.execute(
        "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO items(id, channel_id, title, local_path, state,"
            " source_ref, scheduled_at, published_url, error, meta,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, channel_id, title, local_path, state, source_ref,
             scheduled_at, published_url, error, meta_json, now, now))
        return True

    new_state = state
    old_rank = STATE_RANK.get(existing["state"], -1)
    new_rank = STATE_RANK.get(state, -1)
    if new_rank < old_rank and new_state not in ("published", "failed"):
        new_state = existing["state"]  # keep hub-side state, don't downgrade

    changed = (
        existing["channel_id"] != channel_id
        or existing["title"] != title
        or existing["local_path"] != local_path
        or existing["state"] != new_state
        or existing["source_ref"] != source_ref
        or (scheduled_at is not None and existing["scheduled_at"] != scheduled_at)
        or (published_url is not None and existing["published_url"] != published_url)
        or (error is not None and existing["error"] != error)
        or (meta_json is not None and existing["meta"] != meta_json)
    )
    if not changed:
        return False

    conn.execute(
        "UPDATE items SET channel_id=?, title=?, local_path=?, state=?,"
        " source_ref=?,"
        " scheduled_at=COALESCE(?, scheduled_at),"
        " published_url=COALESCE(?, published_url),"
        " error=COALESCE(?, error),"
        " meta=COALESCE(?, meta),"
        " updated_at=? WHERE id=?",
        (channel_id, title, local_path, new_state, source_ref,
         scheduled_at, published_url, error, meta_json, now, item_id))
    return True
