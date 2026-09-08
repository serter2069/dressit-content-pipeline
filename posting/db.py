#!/usr/bin/env python3
"""posting hub — SQLite layer.

Schema and query helpers for the central posting dashboard.
Stdlib only (sqlite3, json, os, datetime). No network access.

DB location: /root/posting/data/posting.db, overridable via the
POSTING_DB environment variable (used by --selftest and tests).

Run `python3 db.py --selftest` for an offline self-check (temp DB).
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "posting.db")

# Item state machine: new -> review -> approved/rejected -> scheduled
#                     -> published/failed
# 'archived' is a side-state for hidden items (test junk, dead links,
# superseded renders): the dashboard excludes it by default; it can be
# restored to review.
ITEM_STATES = ("new", "review", "approved", "rejected",
               "scheduled", "published", "failed", "archived")

# Allowed transitions: current state -> set of next states.
# published is terminal. rejected may go back to review
# (e.g. after re-editing). scheduled may be sent back to approved
# (unschedule) or forward to published/failed. Anything except published
# can be archived; archived restores to review.
ALLOWED_TRANSITIONS = {
    "new": {"review", "archived"},
    "review": {"approved", "rejected", "archived"},
    "approved": {"scheduled", "rejected", "archived"},
    "rejected": {"review", "archived"},
    "scheduled": {"published", "failed", "approved", "archived"},
    "published": set(),
    "failed": {"scheduled", "archived"},
    "archived": {"review"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    alias         TEXT PRIMARY KEY,
    name          TEXT,
    pipeline_kind TEXT,
    pipeline_path TEXT,
    notes         TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_alias     TEXT NOT NULL,
    platform          TEXT NOT NULL,
    handle            TEXT,
    token_path        TEXT,
    daily_limit       INTEGER,
    publish_times_utc TEXT,  -- json array of "HH:MM" strings
    active            INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    channel_id    INTEGER,
    title         TEXT,
    local_path    TEXT,
    state         TEXT,
    source_ref    TEXT,
    scheduled_at  TEXT,
    published_url TEXT,
    error         TEXT,
    meta          TEXT,  -- json
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_alias TEXT,
    pipeline      TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT,  -- running | ok | error
    summary       TEXT,
    log           TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS content_fingerprints (
    item_id          TEXT PRIMARY KEY,
    video_path       TEXT,
    video_duration   REAL,
    video_hash       TEXT,
    video_meta       TEXT,  -- json
    audio_hash       TEXT,
    text_clean       TEXT,
    source_ref       TEXT,
    source_intervals TEXT,  -- json
    created_at       TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_channels_project ON channels(project_alias);
CREATE INDEX IF NOT EXISTS idx_items_channel ON items(channel_id);
CREATE INDEX IF NOT EXISTS idx_items_state ON items(state);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_alias);
CREATE INDEX IF NOT EXISTS idx_fingerprints_video_hash ON content_fingerprints(video_hash);
CREATE INDEX IF NOT EXISTS idx_fingerprints_video_path ON content_fingerprints(video_path);
"""


def db_path():
    """Resolve DB path, honoring the POSTING_DB env override."""
    return os.environ.get("POSTING_DB", DEFAULT_DB_PATH)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db(path=None):
    """Open (and initialize) the database. Returns a sqlite3.Connection."""
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _json_or_none(value):
    """Serialize a python object to JSON text, or pass through a raw string."""
    if value is None:
        return None
    if isinstance(value, str):
        # Validate it is JSON so we never store garbage in json columns.
        json.loads(value)
        return value
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------- projects

def upsert_project(conn, alias, name=None, pipeline_kind=None,
                   pipeline_path=None, notes=None):
    """Insert or update a project row keyed by alias."""
    existing = conn.execute(
        "SELECT alias FROM projects WHERE alias = ?", (alias,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE projects SET name = ?, pipeline_kind = ?, "
            "pipeline_path = ?, notes = ? WHERE alias = ?",
            (name, pipeline_kind, pipeline_path, notes, alias))
    else:
        conn.execute(
            "INSERT INTO projects (alias, name, pipeline_kind, "
            "pipeline_path, notes, created_at) VALUES (?,?,?,?,?,?)",
            (alias, name, pipeline_kind, pipeline_path, notes, _now()))
    conn.commit()
    return get_project(conn, alias)


def get_project(conn, alias):
    row = conn.execute(
        "SELECT * FROM projects WHERE alias = ?", (alias,)).fetchone()
    return _row_to_dict(row)


def list_projects(conn):
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY alias").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- channels

def upsert_channel(conn, project_alias, platform, handle=None,
                   token_path=None, daily_limit=None,
                   publish_times_utc=None, active=1, channel_id=None):
    """Insert or update a channel.

    Match key is (project_alias, platform, handle) unless channel_id is
    given. publish_times_utc may be a python list or a JSON string.
    Returns the channel id.
    """
    times_json = _json_or_none(publish_times_utc)
    if channel_id is not None:
        row = conn.execute(
            "SELECT id FROM channels WHERE id = ?", (channel_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM channels WHERE project_alias = ? "
            "AND platform = ? AND handle IS ?",
            (project_alias, platform, handle)).fetchone()
    if row:
        cid = row["id"]
        conn.execute(
            "UPDATE channels SET project_alias = ?, platform = ?, "
            "handle = ?, token_path = ?, daily_limit = ?, "
            "publish_times_utc = ?, active = ? WHERE id = ?",
            (project_alias, platform, handle, token_path, daily_limit,
             times_json, int(bool(active)), cid))
    else:
        cur = conn.execute(
            "INSERT INTO channels (project_alias, platform, handle, "
            "token_path, daily_limit, publish_times_utc, active) "
            "VALUES (?,?,?,?,?,?,?)",
            (project_alias, platform, handle, token_path, daily_limit,
             times_json, int(bool(active))))
        cid = cur.lastrowid
    conn.commit()
    return cid


def get_channel(conn, channel_id):
    row = conn.execute(
        "SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    d = _row_to_dict(row)
    if d and d.get("publish_times_utc"):
        try:
            d["publish_times_utc"] = json.loads(d["publish_times_utc"])
        except (ValueError, TypeError):
            pass
    return d


def list_channels(conn, project_alias=None):
    sql = "SELECT * FROM channels"
    params = ()
    if project_alias:
        sql += " WHERE project_alias = ?"
        params = (project_alias,)
    sql += " ORDER BY project_alias, id"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("publish_times_utc"):
            try:
                d["publish_times_utc"] = json.loads(d["publish_times_utc"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


# ------------------------------------------------------------------- items

def upsert_item(conn, item_id, channel_id=None, title=None, local_path=None,
                state=None, source_ref=None, scheduled_at=None,
                published_url=None, error=None, meta=None):
    """Insert or update an item keyed by id. Returns the row as a dict.

    On insert, state defaults to 'new'. On update, only non-None fields
    are changed. Use transition_item() for state changes that must be
    validated against the state machine.
    """
    if state is not None and state not in ITEM_STATES:
        raise ValueError("unknown item state: %r" % state)
    meta_json = _json_or_none(meta)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO items (id, channel_id, title, local_path, state, "
            "source_ref, scheduled_at, published_url, error, meta, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, channel_id, title, local_path, state or "new",
             source_ref, scheduled_at, published_url, error, meta_json,
             now, now))
    else:
        fields, params = [], []
        for col, val in (("channel_id", channel_id), ("title", title),
                         ("local_path", local_path), ("state", state),
                         ("source_ref", source_ref),
                         ("scheduled_at", scheduled_at),
                         ("published_url", published_url),
                         ("error", error), ("meta", meta_json)):
            if val is not None:
                fields.append(col + " = ?")
                params.append(val)
        fields.append("updated_at = ?")
        params.append(now)
        params.append(item_id)
        conn.execute("UPDATE items SET %s WHERE id = ?" % ", ".join(fields),
                     params)
    conn.commit()
    return get_item(conn, item_id)


def get_item(conn, item_id):
    row = conn.execute("SELECT * FROM items WHERE id = ?",
                       (item_id,)).fetchone()
    return _row_to_dict(row)


# Review-first ordering for the default dashboard view.
STATE_SORT = ("review", "new", "approved", "scheduled",
              "published", "rejected", "failed", "archived")

_STATE_SORT_SQL = "CASE i.state " + " ".join(
    "WHEN '%s' THEN %d" % (s, n) for n, s in enumerate(STATE_SORT)) + \
    " ELSE %d END" % len(STATE_SORT)


def list_items(conn, project_alias=None, state=None, limit=200,
               include_archived=False):
    """List items, optionally filtered by project and/or state.

    Archived items are hidden unless include_archived is true or the
    state filter explicitly asks for them. Default order is review-first
    (review queue on top), then most recently updated.
    """
    sql = ("SELECT i.* FROM items i "
           "LEFT JOIN channels c ON c.id = i.channel_id")
    where, params = [], []
    if project_alias:
        where.append("c.project_alias = ?")
        params.append(project_alias)
    if state:
        where.append("i.state = ?")
        params.append(state)
    elif not include_archived:
        where.append("i.state != 'archived'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY %s, i.updated_at DESC LIMIT ?" % _STATE_SORT_SQL
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_items_by_state(conn, project_alias=None, include_archived=False):
    """Return {state: count} for a project (or all projects)."""
    sql = ("SELECT i.state, COUNT(*) AS n FROM items i "
           "LEFT JOIN channels c ON c.id = i.channel_id")
    where, params = [], []
    if project_alias:
        where.append("c.project_alias = ?")
        params.append(project_alias)
    if not include_archived:
        where.append("i.state != 'archived'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY i.state"
    return {r["state"]: r["n"]
            for r in conn.execute(sql, params).fetchall()}


def transition_item(conn, item_id, to_state, scheduled_at=None,
                    published_url=None, error=None, meta=None):
    """Validate and apply an item state transition.

    Returns the updated item dict. Raises ValueError on unknown item,
    unknown state, or a transition not allowed by the state machine.
    Optional fields are written through when provided; moving into
    'scheduled' clears error, moving into 'published' clears error and
    stamps published_url when given.
    """
    item = get_item(conn, item_id)
    if item is None:
        raise ValueError("unknown item: %r" % item_id)
    if to_state not in ITEM_STATES:
        raise ValueError("unknown item state: %r" % to_state)
    allowed = ALLOWED_TRANSITIONS.get(item["state"], set())
    if to_state not in allowed:
        raise ValueError("transition %s -> %s not allowed"
                         % (item["state"], to_state))
    updates = {"state": to_state}
    if scheduled_at is not None:
        updates["scheduled_at"] = scheduled_at
    if published_url is not None:
        updates["published_url"] = published_url
    if to_state in ("scheduled", "published", "approved"):
        updates["error"] = None  # cleared below via explicit SQL
    if error is not None:
        updates["error"] = error
    if meta is not None:
        updates["meta"] = meta

    # upsert_item skips None values, so error clearing needs direct SQL.
    now = _now()
    cols, params = ["state = ?", "updated_at = ?"], [to_state, now]
    if scheduled_at is not None:
        cols.append("scheduled_at = ?")
        params.append(scheduled_at)
    if published_url is not None:
        cols.append("published_url = ?")
        params.append(published_url)
    if error is not None:
        cols.append("error = ?")
        params.append(error)
    elif to_state in ("scheduled", "published", "approved"):
        cols.append("error = NULL")
    if meta is not None:
        cols.append("meta = ?")
        params.append(_json_or_none(meta))
    params.append(item_id)
    conn.execute("UPDATE items SET %s WHERE id = ?" % ", ".join(cols), params)
    conn.commit()
    return get_item(conn, item_id)


# ---------------------------------------- content fingerprints & deduplication

def upsert_content_fingerprint(conn, item_id, video_path=None,
                               video_duration=None, video_hash=None,
                               video_meta=None, audio_hash=None,
                               text_clean=None, source_ref=None,
                               source_intervals=None):
    """Insert or update a content fingerprint row keyed by item_id."""
    video_meta_json = _json_or_none(video_meta)
    intervals_json = _json_or_none(source_intervals)
    now = _now()
    existing = conn.execute(
        "SELECT item_id FROM content_fingerprints WHERE item_id = ?",
        (item_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO content_fingerprints (item_id, video_path, video_duration, "
            "video_hash, video_meta, audio_hash, text_clean, source_ref, "
            "source_intervals, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, video_path, video_duration, video_hash, video_meta_json,
             audio_hash, text_clean, source_ref, intervals_json, now, now))
    else:
        fields, params = [], []
        for col, val in (("video_path", video_path),
                         ("video_duration", video_duration),
                         ("video_hash", video_hash),
                         ("video_meta", video_meta_json),
                         ("audio_hash", audio_hash),
                         ("text_clean", text_clean),
                         ("source_ref", source_ref),
                         ("source_intervals", intervals_json)):
            if val is not None:
                fields.append(col + " = ?")
                params.append(val)
        fields.append("updated_at = ?")
        params.append(now)
        params.append(item_id)
        conn.execute(
            "UPDATE content_fingerprints SET %s WHERE item_id = ?"
            % ", ".join(fields), params)
    conn.commit()
    return get_content_fingerprint(conn, item_id)


def get_content_fingerprint(conn, item_id):
    """Fetch content fingerprint row by item_id."""
    row = conn.execute(
        "SELECT * FROM content_fingerprints WHERE item_id = ?",
        (item_id,)).fetchone()
    return _row_to_dict(row)


def list_content_fingerprints(conn):
    """List all content fingerprint rows."""
    rows = conn.execute(
        "SELECT * FROM content_fingerprints ORDER BY item_id").fetchall()
    return [_row_to_dict(r) for r in rows]


def check_duplicate_candidate(conn, video_path=None, text="", channel_id=None,
                              scheduled_at=None, source_ref="",
                              check_states=("review", "approved", "scheduled", "published"),
                              exclude_item_id=None, candidate_fp=None):
    """Evaluate candidate content against existing items in DB using dedup_engine.

    Returns:
      {
        "is_duplicate": bool,
        "duplicate_of": str or None,
        "similarity_score": float,
        "reason": str,
        "classification": str,
        "allowed": bool,
        "visual_similarity": float,
        "text_similarity": float,
        "audio_similarity": float or None,
        "source_overlap": float,
        "matches": list
      }
    """
    from dedup_engine import fingerprint_media, evaluate_duplicate, clean_text, parse_source_ref

    # 1. Build candidate fingerprint
    if candidate_fp is None:
        cached = None
        if video_path:
            row = conn.execute(
                "SELECT * FROM content_fingerprints WHERE video_path = ? LIMIT 1",
                (video_path,)).fetchone()
            if row:
                cached = _row_to_dict(row)

        if cached and cached.get("video_meta"):
            try:
                v_meta = json.loads(cached["video_meta"]) if isinstance(cached["video_meta"], str) else cached["video_meta"]
            except Exception:
                v_meta = {}
            candidate_fp = {
                "video_path": str(video_path or ""),
                "video": v_meta,
                "audio": cached.get("audio_hash"),
                "text": text or "",
                "text_clean": clean_text(text) if text else "",
                "source_ref": source_ref or cached.get("source_ref") or "",
                "source_intervals": parse_source_ref(source_ref or cached.get("source_ref") or "")
            }
        else:
            candidate_fp = fingerprint_media(video_path or "", text=text or "", source_ref=source_ref or "")

    # 2. Query target items in DB
    placeholders = ",".join("?" for _ in check_states)
    sql = (
        "SELECT i.id, i.channel_id, i.title, i.local_path, i.source_ref, "
        "i.scheduled_at, i.created_at, i.meta, "
        "fp.video_meta, fp.audio_hash, fp.text_clean, fp.source_intervals "
        "FROM items i "
        "LEFT JOIN content_fingerprints fp ON fp.item_id = i.id "
        f"WHERE i.state IN ({placeholders})"
    )
    params = list(check_states)
    if exclude_item_id:
        sql += " AND i.id != ?"
        params.append(exclude_item_id)

    rows = conn.execute(sql, params).fetchall()

    duplicates = []
    max_sim = 0.0

    for r in rows:
        row_dict = dict(r)
        meta = {}
        if row_dict.get("meta"):
            try:
                meta = json.loads(row_dict["meta"]) if isinstance(row_dict["meta"], str) else row_dict["meta"]
            except Exception:
                meta = {}

        # Reconstruct existing fingerprint from content_fingerprints or meta
        v_dict = {}
        if row_dict.get("video_meta"):
            try:
                v_dict = json.loads(row_dict["video_meta"]) if isinstance(row_dict["video_meta"], str) else row_dict["video_meta"]
            except Exception:
                v_dict = {}
        elif meta.get("video_fingerprint"):
            v_dict = meta["video_fingerprint"]
        elif meta.get("video"):
            v_dict = meta["video"]

        a_hash = row_dict.get("audio_hash") or meta.get("audio_fingerprint") or meta.get("audio")
        t_raw = row_dict.get("title") or meta.get("text") or ""
        s_ref = row_dict.get("source_ref") or meta.get("source_ref") or ""

        existing_fp = {
            "video": v_dict,
            "audio": a_hash,
            "text": t_raw,
            "text_clean": row_dict.get("text_clean") or (clean_text(t_raw) if t_raw else ""),
            "source_ref": s_ref,
            "source_intervals": parse_source_ref(s_ref) if s_ref else []
        }

        exist_time = row_dict.get("scheduled_at") or row_dict.get("created_at")
        eval_res = evaluate_duplicate(
            candidate_fp=candidate_fp,
            existing_fp=existing_fp,
            candidate_channel=channel_id,
            existing_channel=row_dict.get("channel_id"),
            candidate_time=scheduled_at,
            existing_time=exist_time
        )
        sim = eval_res.get("similarity_score", 0.0)
        if sim > max_sim:
            max_sim = sim

        if eval_res.get("is_duplicate"):
            eval_res["existing_item_id"] = row_dict["id"]
            duplicates.append(eval_res)

    if duplicates:
        # Prioritize disallowed duplicates (hard duplicate or cooldown violation)
        disallowed = [d for d in duplicates if not d.get("allowed", True)]
        if disallowed:
            disallowed.sort(key=lambda d: d.get("similarity_score", 0.0), reverse=True)
            primary = disallowed[0]
        else:
            duplicates.sort(key=lambda d: d.get("similarity_score", 0.0), reverse=True)
            primary = duplicates[0]

        return {
            "is_duplicate": True,
            "duplicate_of": primary["existing_item_id"],
            "similarity_score": primary["similarity_score"],
            "classification": primary["classification"],
            "reason": primary["reason"],
            "allowed": primary["allowed"],
            "visual_similarity": primary.get("visual_similarity", 0.0),
            "text_similarity": primary.get("text_similarity", 0.0),
            "audio_similarity": primary.get("audio_similarity"),
            "source_overlap": primary.get("source_overlap", 0.0),
            "matches": duplicates
        }

    return {
        "is_duplicate": False,
        "duplicate_of": None,
        "similarity_score": round(max_sim, 4),
        "classification": "unique",
        "reason": "Unique content: no duplicate found in database",
        "allowed": True,
        "visual_similarity": 0.0,
        "text_similarity": 0.0,
        "audio_similarity": None,
        "source_overlap": 0.0,
        "matches": []
    }


def check_duplicate_item(conn, item_id, scheduled_at=None,
                         check_states=("approved", "scheduled", "published"),
                         exclude_item_id=None):
    """Check an existing item in DB against other items in DB."""
    item = get_item(conn, item_id)
    if item is None:
        raise ValueError("unknown item: %r" % item_id)

    cf = get_content_fingerprint(conn, item_id)
    meta = {}
    if item.get("meta"):
        try:
            meta = json.loads(item["meta"]) if isinstance(item["meta"], str) else item["meta"]
        except Exception:
            meta = {}

    candidate_fp = None
    if cf and cf.get("video_meta"):
        from dedup_engine import clean_text, parse_source_ref
        try:
            v_meta = json.loads(cf["video_meta"]) if isinstance(cf["video_meta"], str) else cf["video_meta"]
        except Exception:
            v_meta = {}
        t_raw = item.get("title") or ""
        s_ref = item.get("source_ref") or ""
        candidate_fp = {
            "video_path": item.get("local_path") or "",
            "video": v_meta,
            "audio": cf.get("audio_hash"),
            "text": t_raw,
            "text_clean": cf.get("text_clean") or clean_text(t_raw),
            "source_ref": s_ref,
            "source_intervals": parse_source_ref(s_ref) if s_ref else []
        }
    elif meta.get("video_fingerprint"):
        from dedup_engine import clean_text, parse_source_ref
        t_raw = item.get("title") or ""
        s_ref = item.get("source_ref") or ""
        candidate_fp = {
            "video_path": item.get("local_path") or "",
            "video": meta["video_fingerprint"],
            "audio": meta.get("audio_fingerprint"),
            "text": t_raw,
            "text_clean": clean_text(t_raw),
            "source_ref": s_ref,
            "source_intervals": parse_source_ref(s_ref) if s_ref else []
        }
    elif item.get("local_path") and os.path.exists(item["local_path"]):
        from dedup_engine import fingerprint_media
        candidate_fp = fingerprint_media(
            item["local_path"],
            text=item.get("title") or "",
            source_ref=item.get("source_ref") or ""
        )
        upsert_content_fingerprint(
            conn, item_id,
            video_path=item.get("local_path"),
            video_duration=candidate_fp["video"].get("duration"),
            video_hash=candidate_fp["video"].get("composite"),
            video_meta=candidate_fp["video"],
            audio_hash=candidate_fp.get("audio"),
            text_clean=candidate_fp.get("text_clean"),
            source_ref=item.get("source_ref") or "",
            source_intervals=candidate_fp.get("source_intervals")
        )
        meta["video_fingerprint"] = candidate_fp["video"]
        meta["audio_fingerprint"] = candidate_fp.get("audio")
        conn.execute("UPDATE items SET meta = ? WHERE id = ?",
                     (_json_or_none(meta), item_id))
        conn.commit()

    time_val = scheduled_at or item.get("scheduled_at") or item.get("created_at")
    return check_duplicate_candidate(
        conn,
        video_path=item.get("local_path"),
        text=item.get("title") or "",
        channel_id=item.get("channel_id"),
        scheduled_at=time_val,
        source_ref=item.get("source_ref") or "",
        check_states=check_states,
        exclude_item_id=exclude_item_id or item_id,
        candidate_fp=candidate_fp
    )


# -------------------------------------------------------------------- runs

RUN_STATUSES = ("running", "ok", "error")


def insert_run(conn, project_alias, pipeline, status=None, summary=None,
               log=None, started_at=None, finished_at=None):
    """Record a pipeline run. Returns the new run id.

    started_at/finished_at are optional ISO timestamps supplied by the
    caller; created_at is always stamped here. status defaults to 'ok'.
    """
    if not project_alias or not pipeline:
        raise ValueError("project_alias and pipeline are required")
    if status is not None and status not in RUN_STATUSES:
        raise ValueError("unknown run status: %r" % status)
    cur = conn.execute(
        "INSERT INTO runs (project_alias, pipeline, started_at, "
        "finished_at, status, summary, log, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (project_alias, pipeline, started_at, finished_at,
         status or "ok", summary, log, _now()))
    conn.commit()
    return cur.lastrowid


def list_runs(conn, project_alias=None, limit=50):
    """List runs newest first, optionally filtered by project."""
    sql = "SELECT * FROM runs"
    params = []
    if project_alias:
        sql += " WHERE project_alias = ?"
        params.append(project_alias)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- overview

def overview(conn, include_archived=False):
    """Per-project summary: project row + item counts by state.

    Archived items are excluded from counts/total unless
    include_archived is true (the dashboard uses the default so hidden
    junk never inflates the chips)."""
    out = []
    for p in list_projects(conn):
        counts = count_items_by_state(conn, p["alias"],
                                      include_archived=include_archived)
        chans = [
            {"platform": c["platform"], "handle": c["handle"]}
            for c in list_channels(conn, p["alias"])
        ]
        out.append({
            "alias": p["alias"],
            "name": p["name"],
            "pipeline_kind": p["pipeline_kind"],
            "pipeline_path": p["pipeline_path"],
            "notes": p["notes"],
            "total": sum(counts.values()),
            "states": counts,
            "channels": chans,
        })
    return out


# -------------------------------------------------------------------- seed

SEED_PROJECTS = (
    {
        "alias": "dressit-fshorts",
        "name": "Платья · AI fashion Shorts",
        "pipeline_kind": "fshorts",
        "pipeline_path": "/root/fshorts",
        "notes": "Оригинальные Shorts про вирусные платья и бюджетные "
                 "находки. Материал: стоковые видео Pexels + фото товаров, "
                 "сценарий пишет LLM (Gemini Flash), озвучка — клон голоса "
                 "Сергея (ElevenLabs v3), монтаж ffmpeg с karaoke-субтитрами "
                 "и музыкой. CTA ведёт на Dressit («хочешь платье "
                 "бесплатно»).",
        "channels": [
            {"platform": "youtube", "handle": "@решимость",
             "token_path": "/root/fshorts/youtube_token.json",
             "daily_limit": 2,
             "publish_times_utc": ["14:00", "18:00", "22:00"]},
        ],
    },
    {
        "alias": "dressit-shorts",
        "name": "Отношения · ремиксы клипов",
        "pipeline_kind": "dressit-shorts",
        "pipeline_path": "/root/dressit-shorts",
        "notes": "Перемонтаж трендовых men-vs-women клипов: yt-dlp тянет "
                 "кандидатов через домашний IP (SOCKS), дедуп по "
                 "pHash/simhash, freeze-frame хук + зумы + субтитры whisper, "
                 "в конце 2-секундный эндкард Dressit.",
        "channels": [
            {"platform": "youtube", "handle": "@решимость2",
             "token_path": "/root/dressit-shorts/token_youtube_schemy.json",
             "daily_limit": 3,
             "publish_times_utc": ["13:00", "17:00", "21:00"]},
        ],
    },
)


def seed(conn):
    """Idempotently insert the seed projects and channels."""
    for p in SEED_PROJECTS:
        upsert_project(conn, p["alias"], name=p["name"],
                       pipeline_kind=p["pipeline_kind"],
                       pipeline_path=p["pipeline_path"], notes=p["notes"])
        for ch in p["channels"]:
            upsert_channel(conn, p["alias"], **ch)


# --------------------------------------------------------------- selftest

def _selftest():
    """Offline self-check against a temporary database. No network."""
    tmp = tempfile.mkdtemp(prefix="posting-db-selftest-")
    os.environ["POSTING_DB"] = os.path.join(tmp, "test.db")
    conn = get_db()

    # Seed
    seed(conn)
    projects = list_projects(conn)
    assert [p["alias"] for p in projects] == ["dressit-fshorts",
                                              "dressit-shorts"], projects
    assert projects[0]["name"] == "Платья · AI fashion Shorts"
    assert "Pexels" in projects[0]["notes"]
    assert projects[1]["name"] == "Отношения · ремиксы клипов"
    assert "yt-dlp" in projects[1]["notes"]
    # Seed twice: must stay idempotent
    seed(conn)
    assert len(list_projects(conn)) == 2
    chans = list_channels(conn)
    assert len(chans) == 2, chans

    ch1 = [c for c in chans if c["project_alias"] == "dressit-fshorts"][0]
    assert ch1["daily_limit"] == 2
    assert ch1["publish_times_utc"] == ["14:00", "18:00", "22:00"], ch1

    # Items
    upsert_item(conn, "it-1", channel_id=ch1["id"], title="clip one")
    upsert_item(conn, "it-2", channel_id=ch1["id"], title="clip two")
    item = get_item(conn, "it-1")
    assert item["state"] == "new", item

    # Walk the happy path: new -> review -> approved -> scheduled -> published
    transition_item(conn, "it-1", "review")
    transition_item(conn, "it-1", "approved")
    transition_item(conn, "it-1", "scheduled",
                    scheduled_at="2026-09-02T14:00:00Z")
    item = transition_item(conn, "it-1", "published",
                           published_url="https://youtu.be/abc")
    assert item["state"] == "published"
    assert item["published_url"] == "https://youtu.be/abc"

    # Reject path + error field
    transition_item(conn, "it-2", "review")
    item = transition_item(conn, "it-2", "rejected", error="bad voiceover")
    assert item["state"] == "rejected" and item["error"] == "bad voiceover"
    # rejected can return to review
    transition_item(conn, "it-2", "review")

    # Archive path: hidden by default, restorable to review
    upsert_item(conn, "it-junk", channel_id=ch1["id"], title="junk")
    item = transition_item(conn, "it-junk", "archived")
    assert item["state"] == "archived"
    assert all(i["id"] != "it-junk"
               for i in list_items(conn, project_alias="dressit-fshorts"))
    assert any(i["id"] == "it-junk"
               for i in list_items(conn, project_alias="dressit-fshorts",
                                   include_archived=True))
    assert any(i["id"] == "it-junk"
               for i in list_items(conn, project_alias="dressit-fshorts",
                                   state="archived"))
    ov_inc = overview(conn, include_archived=True)
    fs_inc = [o for o in ov_inc if o["alias"] == "dressit-fshorts"][0]
    assert fs_inc["states"].get("archived") == 1, fs_inc
    assert overview(conn)[0]["states"].get("archived") is None

    # Illegal transitions must raise
    for bad in (("it-1", "review"),        # terminal published
                ("it-2", "published"),     # review -> published skips steps
                ("nope", "review"),        # unknown item
                ("it-2", "bogus")):        # unknown state
        try:
            transition_item(conn, bad[0], bad[1])
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for %r" % (bad,))

    # Query helpers
    ov = overview(conn)
    fs = [o for o in ov if o["alias"] == "dressit-fshorts"][0]
    assert fs["states"].get("published") == 1, fs
    assert fs["states"].get("review") == 1, fs
    assert fs["total"] == 2, fs

    items = list_items(conn, project_alias="dressit-fshorts",
                       state="published")
    assert len(items) == 1 and items[0]["id"] == "it-1", items

    # meta round-trip
    upsert_item(conn, "it-3", channel_id=ch1["id"], title="m",
                meta={"job": "x"})
    assert json.loads(get_item(conn, "it-3")["meta"]) == {"job": "x"}

    # archived restores to review
    item = transition_item(conn, "it-junk", "review")
    assert item["state"] == "review"

    # Runs: insert/list round-trip, newest first, per-project filter
    r1 = insert_run(conn, "dressit-fshorts", "fshorts", status="ok",
                    summary="rendered 2 jobs", log="line1\nline2",
                    started_at="2026-09-02T10:00:00Z",
                    finished_at="2026-09-02T10:05:00Z")
    r2 = insert_run(conn, "dressit-fshorts", "fshorts", status="running")
    r3 = insert_run(conn, "dressit-shorts", "dressit-shorts", status="error",
                    summary="boom")
    runs = list_runs(conn, project_alias="dressit-fshorts")
    assert [r["id"] for r in runs] == [r2, r1], runs  # newest first
    assert runs[1]["summary"] == "rendered 2 jobs"
    assert runs[1]["status"] == "ok"
    assert runs[1]["log"] == "line1\nline2"
    assert runs[0]["status"] == "running"
    assert runs[0]["finished_at"] is None
    assert runs[0]["created_at"]
    assert len(list_runs(conn)) == 3
    only = list_runs(conn, project_alias="dressit-shorts")
    assert len(only) == 1 and only[0]["id"] == r3
    assert list_runs(conn, limit=1)[0]["id"] == r3
    # status defaults to ok; bogus status raises
    rid = insert_run(conn, "dressit-fshorts", "fshorts")
    row = conn.execute("SELECT status FROM runs WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "ok"
    for bad_args in ((None, "fshorts"), ("dressit-fshorts", None),
                     ("dressit-fshorts", "fshorts", "bogus-status")):
        try:
            insert_run(conn, *bad_args)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for %r" % (bad_args,))

    # Content fingerprints & Deduplication checks
    fake_v = {"duration": 10.0, "k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000", "composite": "0000000000000000:0000000000000000:0000000000000000"}
    upsert_content_fingerprint(
        conn, "it-1",
        video_path="/dummy/path1.mp4",
        video_duration=10.0,
        video_hash=fake_v["composite"],
        video_meta=fake_v,
        text_clean="clip one",
        source_ref="job1"
    )
    fp_row = get_content_fingerprint(conn, "it-1")
    assert fp_row is not None and fp_row["video_hash"] == fake_v["composite"]
    assert len(list_content_fingerprints(conn)) == 1

    # Candidate check against it-1 (which is 'published')
    cand_dup = check_duplicate_candidate(
        conn,
        text="clip one",
        source_ref="job1",
        candidate_fp={
            "video": fake_v,
            "audio": None,
            "text": "clip one",
            "text_clean": "clip one",
            "source_ref": "job1",
            "source_intervals": []
        }
    )
    assert cand_dup["is_duplicate"] is True
    assert cand_dup["duplicate_of"] == "it-1"
    assert cand_dup["classification"] == "hard_duplicate"

    # Unique candidate check
    fake_unique_v = {"duration": 10.0, "k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff", "composite": "ffffffffffffffff:ffffffffffffffff:ffffffffffffffff"}
    cand_uniq = check_duplicate_candidate(
        conn,
        text="totally unique text",
        source_ref="unique_source",
        candidate_fp={
            "video": fake_unique_v,
            "audio": None,
            "text": "totally unique text",
            "text_clean": "totally unique text",
            "source_ref": "unique_source",
            "source_intervals": []
        }
    )
    assert cand_uniq["is_duplicate"] is False
    assert cand_uniq["classification"] == "unique"

    conn.close()
    print("db.py selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        # Default action: initialize the real DB and seed it.
        c = get_db()
        seed(c)
        print("initialized %s (%d projects)" % (db_path(),
                                                len(list_projects(c))))
