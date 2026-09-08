"""dressit-shorts pipeline adapter for the posting hub.

Reads /root/dressit-shorts/data/dressit.db read-only (sqlite3 URI mode=ro;
never imports dressit-shorts code) and upserts one hub item per clip:
id 'dshorts-<clipid>'.

Status mapping (clips.status -> hub item.state):
  discovered | downloaded | dedup_ok      -> new
  edited | pending | pending_approval     -> review
  approved                                -> approved
  rejected                                -> rejected (error = reject_reason)
  uploaded | published                    -> published (url from youtube_id)
  error                                   -> failed   (error = reject_reason)
  duplicate                               -> skipped (not a content item)

CLI:
  python3 adapters/dressit_shorts.py            # sync once, prints count
  python3 adapters/dressit_shorts.py --selftest # offline fixture test, exit 0
"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hub

PIPELINE_ROOT = os.environ.get("DRESSIT_SHORTS_PATH", "/root/dressit-shorts")
DB_REL = os.path.join("data", "dressit.db")
PROJECT_ALIAS = "dressit-shorts"

STATUS_MAP = {
    "discovered": "new",
    "downloaded": "new",
    "dedup_ok": "new",
    "edited": "review",
    "pending": "review",
    "pending_approval": "review",
    "approved": "approved",
    "rejected": "rejected",
    "uploaded": "published",
    "published": "published",
    "error": "failed",
}
SKIP_STATUSES = {"duplicate"}


def clip_to_item(clip: sqlite3.Row) -> dict | None:
    status = clip["status"]
    if status in SKIP_STATUSES:
        return None
    state = STATUS_MAP.get(status, "new")
    yt_id = clip["youtube_id"]
    published_url = (f"https://youtube.com/shorts/{yt_id}"
                     if yt_id and state == "published" else None)
    error = clip["reject_reason"] if state in ("rejected", "failed") else None
    return {
        "item_id": f"dshorts-{clip['id']}",
        "title": clip["title"] or clip["video_id"] or clip["source_url"],
        "local_path": clip["edited_path"] or clip["local_path"],
        "state": state,
        "source_ref": clip["source_url"],
        "published_url": published_url,
        "error": error,
        "meta": {
            "pipeline_status": status,
            "platform": clip["platform"],
            "video_id": clip["video_id"],
            "youtube_id": yt_id,
            "created_at": clip["created_at"],
        },
    }


def sync(db: str | None = None, pipeline_root: str | None = None) -> int:
    """Upsert all dressit-shorts clips into the hub DB. Returns changed count."""
    root = pipeline_root or PIPELINE_ROOT
    src_db = os.path.join(root, DB_REL)
    if not os.path.isfile(src_db):
        return 0
    changed = 0
    # Open the pipeline DB strictly read-only.
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        clips = src.execute("SELECT * FROM clips").fetchall()
    finally:
        src.close()
    with _hub.connect(db) as conn:
        _hub.ensure_project(conn, PROJECT_ALIAS, "dressit-shorts",
                            "dressit-sqlite", root)
        channel_id = _hub.find_channel_id(conn, PROJECT_ALIAS)
        for clip in clips:
            item = clip_to_item(clip)
            if item is None:
                continue
            if _hub.upsert_item(conn, channel_id=channel_id, **item):
                changed += 1
    return changed


def enqueue(title: str, notes: str | None = None, db: str | None = None,
            pipeline_root: str | None = None) -> str:
    """Record a delegate request as a note item in the hub DB.

    The dressit-shorts pipeline is owned by another session, so we never
    write into its DB — we only create a hub item (state 'new',
    meta {'requested': True}) that the pipeline owner can pick up.
    Returns the hub item id 'dshorts-req-<date>-<hex>'.
    """
    item_id = ("dshorts-req-"
               + datetime.now(timezone.utc).strftime("%Y%m%d")
               + "-" + uuid.uuid4().hex[:6])
    with _hub.connect(db) as conn:
        _hub.ensure_project(conn, PROJECT_ALIAS, "dressit-shorts",
                            "dressit-sqlite",
                            pipeline_root or PIPELINE_ROOT)
        channel_id = _hub.find_channel_id(conn, PROJECT_ALIAS)
        _hub.upsert_item(conn, item_id=item_id, channel_id=channel_id,
                         title=title, local_path=None, state="new",
                         source_ref="hub-delegate",
                         meta={"requested": True, "notes": notes})
    return item_id


def _selftest() -> None:
    """Offline test: temp fixture dressit.db + temp hub DB, assert mapping,
    counts and idempotency. No network, no real pipeline access."""
    import tempfile

    schema = """
    CREATE TABLE clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_url TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
        video_id TEXT, title TEXT, caption TEXT, transcript TEXT,
        local_path TEXT, edited_path TEXT, phashes TEXT, simhash INTEGER,
        status TEXT NOT NULL, reject_reason TEXT, youtube_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )"""

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "dressit-shorts")
        os.makedirs(os.path.join(root, "data"))
        src_db = os.path.join(root, "data", "dressit.db")
        hub_db = os.path.join(tmp, "posting.db")

        conn = sqlite3.connect(src_db)
        conn.execute(schema)
        fixtures = [
            ("https://x/1", "youtube", "v1", "Clip One", "/c/1.mp4", None,
             "discovered", None, None),
            ("https://x/2", "youtube", "v2", "Clip Two", "/c/2.mp4", None,
             "dedup_ok", None, None),
            ("https://x/3", "youtube", "v3", "Clip Three", "/c/3.mp4",
             "/e/3.mp4", "edited", None, None),
            ("https://x/4", "youtube", "v4", "Clip Four", "/c/4.mp4",
             "/e/4.mp4", "pending_approval", None, None),
            ("https://x/5", "youtube", "v5", "Clip Five", "/c/5.mp4",
             "/e/5.mp4", "approved", None, None),
            ("https://x/6", "youtube", "v6", "Clip Six", "/c/6.mp4", None,
             "rejected", "rejected by admin", None),
            ("https://x/7", "youtube", "v7", "Clip Seven", "/c/7.mp4",
             "/e/7.mp4", "uploaded", None, "YT123"),
            ("https://x/8", "youtube", "v8", "Clip Eight", "/c/8.mp4", None,
             "error", "upload failed: boom", None),
            ("https://x/9", "youtube", "v9", "Clip Dup", "/c/9.mp4", None,
             "duplicate", "dup of v1", None),
        ]
        for f in fixtures:
            conn.execute(
                "INSERT INTO clips(source_url, platform, video_id, title,"
                " local_path, edited_path, status, reject_reason, youtube_id,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?, '2026-09-02T00:00:00+00:00',"
                " '2026-09-02T00:00:00+00:00')", f)
        conn.commit()
        conn.close()

        n1 = sync(db=hub_db, pipeline_root=root)
        assert n1 == 8, n1  # duplicate skipped
        n2 = sync(db=hub_db, pipeline_root=root)
        assert n2 == 0, f"not idempotent: {n2}"

        with _hub.connect(hub_db) as c:
            rows = {r["id"]: r for r in c.execute("SELECT * FROM items")}
            assert len(rows) == 8
            assert "dshorts-9" not in rows  # duplicate skipped
            assert rows["dshorts-1"]["state"] == "new"
            assert rows["dshorts-2"]["state"] == "new"
            assert rows["dshorts-3"]["state"] == "review"
            assert rows["dshorts-3"]["local_path"] == "/e/3.mp4"  # edited preferred
            assert rows["dshorts-4"]["state"] == "review"
            assert rows["dshorts-5"]["state"] == "approved"
            assert rows["dshorts-6"]["state"] == "rejected"
            assert rows["dshorts-6"]["error"] == "rejected by admin"
            assert rows["dshorts-7"]["state"] == "published"
            assert (rows["dshorts-7"]["published_url"]
                    == "https://youtube.com/shorts/YT123")
            assert rows["dshorts-8"]["state"] == "failed"
            assert rows["dshorts-8"]["error"] == "upload failed: boom"
            assert rows["dshorts-1"]["source_ref"] == "https://x/1"
            assert c.execute(
                "SELECT 1 FROM projects WHERE alias='dressit-shorts'"
            ).fetchone()

        # missing DB must be a graceful no-op
        assert sync(db=hub_db, pipeline_root=os.path.join(tmp, "nope")) == 0

        # enqueue: records a requested note item, never touches pipeline DB
        item_id = enqueue("Requested topic", notes="please", db=hub_db)
        assert item_id.startswith("dshorts-req-"), item_id
        with _hub.connect(hub_db) as c:
            r = c.execute("SELECT * FROM items WHERE id=?",
                          (item_id,)).fetchone()
            assert r is not None and r["state"] == "new", r
            assert '"requested": true' in r["meta"], r["meta"]
            assert r["source_ref"] == "hub-delegate"

        print(f"dressit_shorts selftest OK (first={n1}, rerun={n2})")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(sync())
