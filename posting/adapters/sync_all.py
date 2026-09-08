"""Run all posting-hub adapters once and print a JSON summary.

Usage (works from any cwd):
  python3 adapters/sync_all.py            # sync both pipelines
  python3 adapters/sync_all.py --selftest # offline fixture test, exit 0

Output: {"fshorts": N, "dressit-shorts": M} — number of hub items
inserted/updated by each adapter (0 on a no-change re-run).

DB path: $POSTING_DB or /root/posting/data/posting.db. Intended to be
called by cron and by the hub's POST /api/sync endpoint.
"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chess_shorts
import dressit_shorts
import event_hero
import fshorts

DEFAULT_DB_PATH = "/root/posting/data/posting.db"

# Contract table (dressit#1836): one row per pipeline/sync run.
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_alias TEXT,
    pipeline TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    summary TEXT,
    log TEXT,
    created_at TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_run(db: str | None, status: str, summary: str, log_text: str,
               started_at: str, finished_at: str) -> None:
    """Insert one runs row via direct sqlite3 (creates the table if missing)."""
    path = db or os.environ.get("POSTING_DB") or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(RUNS_SCHEMA)
        conn.execute(
            "INSERT INTO runs(project_alias, pipeline, started_at,"
            " finished_at, status, summary, log, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("hub", "sync", started_at, finished_at, status, summary,
             log_text[-4000:] if log_text else None, _utcnow()))
        conn.commit()
    finally:
        conn.close()


def sync_all(db: str | None = None) -> dict:
    # DISABLED fshorts#8 2026-09-05: проекты dressit-fshorts и dressit-shorts
    # удалены из хаба вместе с контентом; их адаптеры (fshorts.sync,
    # dressit_shorts.sync) отключены, иначе sync реанимировал бы проекты.
    # Re-enable: вернуть две строки ниже + раскомментировать fshorts-tick
    # в crontab (маркер "DISABLED fshorts#8").
    return {
        # "fshorts": fshorts.sync(db=db),                  # DISABLED fshorts#8
        # "dressit-shorts": dressit_shorts.sync(db=db),    # DISABLED fshorts#8
        "chess-shorts": chess_shorts.sync_all(db=db),
        "event-hero": event_hero.sync(db=db),
    }


def main() -> int:
    """Run the sync, record a runs row, print the JSON summary."""
    started = _utcnow()
    errbuf = io.StringIO()
    status = "ok"
    try:
        with contextlib.redirect_stderr(errbuf):
            result = sync_all()
        summary = json.dumps(result, ensure_ascii=False)
    except Exception as e:
        status = "error"
        summary = json.dumps({"error": f"{type(e).__name__}: {e}"})
        traceback.print_exc(file=errbuf)
    finished = _utcnow()
    try:
        record_run(None, status, summary, errbuf.getvalue(), started, finished)
    except Exception as e:  # run logging must never fail the sync itself
        print(f"sync_all: failed to record run: {e}", file=sys.stderr)
    print(summary)
    return 0 if status == "ok" else 1


def _selftest() -> None:
    """Offline: run both adapters' own fixture selftests, then a combined
    sync against a shared temp hub DB through the fixture pipelines is
    covered inside each adapter; here we verify sync_all's wiring with
    empty (nonexistent) pipeline roots — a graceful zero-count run."""
    import tempfile

    fshorts._selftest()
    dressit_shorts._selftest()
    event_hero._selftest()

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "posting.db")
        orig_c = chess_shorts.OUT_DIR
        orig_e = event_hero.PIPELINE_ROOT
        try:
            chess_shorts.OUT_DIR = os.path.join(tmp, "no-chess")
            event_hero.PIPELINE_ROOT = os.path.join(tmp, "no-eventhero")
            result = sync_all(db=db)
            # fshorts / dressit-shorts adapters are DISABLED (fshorts#8),
            # so sync_all only returns the two live pipelines.
            assert result == {
                "chess-shorts": 0,
                "event-hero": 0,
            }, result
            json.dumps(result)  # must be JSON-serializable
        finally:
            chess_shorts.OUT_DIR = orig_c
            event_hero.PIPELINE_ROOT = orig_e

        # record_run: one row per sync, ok + error paths, table auto-created
        import sqlite3 as _s3
        record_run(db, "ok", json.dumps(result), "tail", "t0", "t1")
        record_run(db, "error", '{"error": "boom"}', "x" * 5000, "t0", "t1")
        conn = _s3.connect(db)
        conn.row_factory = _s3.Row
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2, rows
        assert rows[0]["project_alias"] == "hub"
        assert rows[0]["pipeline"] == "sync"
        assert rows[0]["status"] == "ok"
        assert json.loads(rows[0]["summary"]) == result
        assert len(rows[1]["log"]) == 4000  # log tail capped

    print("sync_all selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
