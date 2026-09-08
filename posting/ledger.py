#!/usr/bin/env python3
"""publish_ledger — persistent dedup ledger for published/planned content.

Shared contract (dressit#1924 + chess side): one row per
(project, channel, event_key). Checked BEFORE render so a duplicate
event never burns render money; marked on publish; listed in the hub.

Table lives in the posting hub DB (default: <this dir>/data/posting.db,
override via env POSTING_DB). Every function creates the table
defensively (CREATE TABLE IF NOT EXISTS) so parallel writers race safely.

Statuses: planned | rendered | approved | published | rejected | skipped_dup

API:
  record_seen(project, channel, event_key, title='', event_date='',
              content_hash='') -> 'new' | 'dup'
  mark_status(project, channel, event_key, status)
      (sets published_at when status='published')
  set_comment_id(project, channel, event_key, comment_id)
      (dressit#1931: id of the link comment posted under the video)
  is_publishable(project, channel, event_key) -> bool
      (False if row exists with status in ('rendered','approved','published'))
  list_ledger(project=None, status=None) -> list[dict]  (newest first)

CLI:
  python3 ledger.py --list [--project X] [--status Y]
  python3 ledger.py --check <project> <channel> <event_key>
  python3 ledger.py --selftest
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "posting.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT '',
  event_key TEXT NOT NULL,
  content_hash TEXT,
  title TEXT,
  event_date TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  first_seen_at TEXT NOT NULL,
  published_at TEXT,
  comment_id TEXT,
  UNIQUE(project, channel, event_key)
);
"""

STATUSES = ("planned", "rendered", "approved", "published",
            "rejected", "skipped_dup")

# statuses that make an event_key non-publishable (already in/through production)
BLOCKING_STATUSES = ("rendered", "approved", "published")


def db_path():
    """Resolve DB path, honoring the POSTING_DB env override."""
    return os.environ.get("POSTING_DB", DEFAULT_DB_PATH)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect():
    conn = sqlite3.connect(db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Defensive column adds for DBs created before a column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(publish_ledger)")}
    if "comment_id" not in cols:  # dressit#1931
        conn.execute("ALTER TABLE publish_ledger ADD COLUMN comment_id TEXT")


def record_seen(project, channel, event_key, title="", event_date="",
                content_hash=""):
    """INSERT OR IGNORE the event. Returns 'new' if inserted, 'dup' if the
    (project, channel, event_key) row already existed."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO publish_ledger
               (project, channel, event_key, content_hash, title, event_date,
                status, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)""",
            (project, channel or "", event_key, content_hash or None,
             title or None, event_date or None, _now()))
        conn.commit()
        return "new" if cur.rowcount > 0 else "dup"
    finally:
        conn.close()


def mark_status(project, channel, event_key, status):
    """Set the status of an existing (or new) ledger row.
    Sets published_at when status='published'."""
    if status not in STATUSES:
        raise ValueError("unknown ledger status: %r (allowed: %s)"
                         % (status, ", ".join(STATUSES)))
    conn = _connect()
    try:
        published_at = _now() if status == "published" else None
        cur = conn.execute(
            """UPDATE publish_ledger
               SET status = ?,
                   published_at = COALESCE(?, published_at)
               WHERE project = ? AND channel = ? AND event_key = ?""",
            (status, published_at, project, channel or "", event_key))
        if cur.rowcount == 0:
            # defensive: never lose a status mark for an unseen event
            conn.execute(
                """INSERT OR IGNORE INTO publish_ledger
                   (project, channel, event_key, status, first_seen_at,
                    published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project, channel or "", event_key, status, _now(),
                 published_at))
        conn.commit()
    finally:
        conn.close()


def set_comment_id(project, channel, event_key, comment_id):
    """dressit#1931: record the id of the link comment posted under the
    published video (audit trail; NULL until the comment step ran)."""
    conn = _connect()
    try:
        conn.execute(
            """UPDATE publish_ledger SET comment_id = ?
               WHERE project = ? AND channel = ? AND event_key = ?""",
            (comment_id, project, channel or "", event_key))
        conn.commit()
    finally:
        conn.close()


def get_row(project, channel, event_key):
    """Return the ledger row as dict, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM publish_ledger
               WHERE project = ? AND channel = ? AND event_key = ?""",
            (project, channel or "", event_key)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_publishable(project, channel, event_key):
    """False if a row exists with status in BLOCKING_STATUSES
    (rendered/approved/published). Unknown event_key -> True."""
    row = get_row(project, channel, event_key)
    if row is None:
        return True
    return row["status"] not in BLOCKING_STATUSES


def list_ledger(project=None, status=None):
    """Ledger rows newest first. Optional project/status filters."""
    conn = _connect()
    try:
        sql = "SELECT * FROM publish_ledger"
        clauses, params = [], []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _selftest():
    """Offline self-check against a throwaway DB (POSTING_DB override)."""
    tmp = tempfile.mkdtemp(prefix="ledger-selftest-")
    os.environ["POSTING_DB"] = os.path.join(tmp, "test.db")

    assert record_seen("p1", "@c1", "ev1", title="Show", event_date="2026-10-01") == "new"
    assert record_seen("p1", "@c1", "ev1") == "dup"
    # same event_key, other channel -> independent row
    assert record_seen("p1", "@c2", "ev1") == "new"
    # same channel, other project -> independent row
    assert record_seen("p2", "@c1", "ev1") == "new"

    assert is_publishable("p1", "@c1", "ev1") is True   # planned
    assert is_publishable("p1", "@c1", "nope") is True  # unknown

    mark_status("p1", "@c1", "ev1", "rendered")
    assert is_publishable("p1", "@c1", "ev1") is False
    mark_status("p1", "@c1", "ev1", "published")
    row = get_row("p1", "@c1", "ev1")
    assert row["status"] == "published" and row["published_at"], row
    first_published_at = row["published_at"]

    # dressit#1931: comment_id round-trip (also exercises _migrate on a
    # fresh db where the column comes from SCHEMA)
    assert row["comment_id"] is None
    set_comment_id("p1", "@c1", "ev1", "Ugx123abc")
    assert get_row("p1", "@c1", "ev1")["comment_id"] == "Ugx123abc"
    # re-marking published keeps the original published_at
    mark_status("p1", "@c1", "ev1", "published")
    assert get_row("p1", "@c1", "ev1")["published_at"] == first_published_at

    mark_status("p1", "@c2", "ev1", "rejected")
    assert is_publishable("p1", "@c2", "ev1") is True  # rejected is retryable

    # mark_status on unseen event creates the row defensively
    mark_status("p3", "@c3", "ev9", "skipped_dup")
    assert get_row("p3", "@c3", "ev9")["status"] == "skipped_dup"

    rows = list_ledger(project="p1")
    assert len(rows) == 2 and rows[0]["id"] > rows[1]["id"]  # newest first
    assert len(list_ledger(status="published")) == 1

    try:
        mark_status("p1", "@c1", "ev1", "bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    print("ledger selftest OK (%d rows in temp db)" % len(list_ledger()))


def main():
    ap = argparse.ArgumentParser(description="publish dedup ledger")
    ap.add_argument("--list", action="store_true", help="list ledger rows")
    ap.add_argument("--project", help="filter by project alias")
    ap.add_argument("--status", help="filter by status")
    ap.add_argument("--check", nargs=3,
                    metavar=("PROJECT", "CHANNEL", "EVENT_KEY"),
                    help="check whether an event is publishable")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline self-check against a temp DB")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return
    if args.check:
        project, channel, event_key = args.check
        row = get_row(project, channel, event_key)
        publishable = is_publishable(project, channel, event_key)
        print(json.dumps({
            "project": project, "channel": channel, "event_key": event_key,
            "publishable": publishable,
            "blocking_statuses": list(BLOCKING_STATUSES),
            "row": row,
        }, ensure_ascii=False, indent=2))
        sys.exit(0 if publishable else 1)
    if args.list:
        print(json.dumps(list_ledger(project=args.project, status=args.status),
                         ensure_ascii=False, indent=2))
        return
    ap.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
