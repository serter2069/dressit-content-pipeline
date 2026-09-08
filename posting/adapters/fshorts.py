"""fshorts pipeline adapter for the posting hub.

Scans /root/fshorts/jobs/*/job.json (read-only; never imports fshorts code)
and upserts one hub item per job: id 'fshorts-<jobid>'.

State mapping (fshorts job.state -> hub item.state):
  rendered + telegram.message_id set -> review    (sent to Telegram for review)
  rendered without message_id        -> new       (rendered but not sent yet)
  review                             -> review    (sent via approve_bot --job)
  approved                           -> approved
  published                          -> published (url from youtube.video_id,
                                                   scheduled_at from youtube.publish_at)
  rejected                           -> rejected
  everything else (new/scripted/voiced/assets/...) -> new

CLI:
  python3 adapters/fshorts.py            # sync once, prints count
  python3 adapters/fshorts.py --selftest # offline fixture test, exit 0
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hub

PIPELINE_ROOT = os.environ.get("FSHORTS_PATH", "/root/fshorts")
PROJECT_ALIAS = "dressit-fshorts"


def map_state(job: dict) -> str:
    state = job.get("state") or ""
    if state == "rendered":
        tg = job.get("telegram") or {}
        return "review" if tg.get("message_id") else "new"
    if state == "review":
        return "review"
    if state in ("approved", "rejected", "published"):
        return state
    return "new"


def job_to_item(job: dict, job_dir: str) -> dict:
    yt = job.get("youtube") or {}
    script = job.get("script") or {}
    topic = job.get("topic") or {}
    errors = job.get("errors") or []

    video_id = yt.get("video_id")
    published_url = (f"https://youtube.com/shorts/{video_id}"
                     if video_id else None)
    last_error = None
    if errors:
        last = errors[-1]
        last_error = last.get("error") or last.get("msg")

    return {
        "item_id": f"fshorts-{job.get('id') or os.path.basename(job_dir)}",
        "title": script.get("hook") or topic.get("title"),
        "local_path": job_dir,
        "state": map_state(job),
        "source_ref": job.get("id"),
        "scheduled_at": yt.get("publish_at"),
        "published_url": published_url,
        "error": last_error,
        "meta": {
            "pipeline_state": job.get("state"),
            "job_id": job.get("id"),
            "telegram_message_id": (job.get("telegram") or {}).get("message_id"),
            "created_at": job.get("created_at"),
        },
    }


def sync(db: str | None = None, pipeline_root: str | None = None) -> int:
    """Upsert all fshorts jobs into the hub DB. Returns count of changed rows."""
    root = pipeline_root or PIPELINE_ROOT
    jobs_dir = os.path.join(root, "jobs")
    if not os.path.isdir(jobs_dir):
        return 0
    changed = 0
    with _hub.connect(db) as conn:
        _hub.ensure_project(conn, PROJECT_ALIAS, "fshorts",
                            "fshorts-dirs", root)
        channel_id = _hub.find_channel_id(conn, PROJECT_ALIAS)
        for entry in sorted(os.listdir(jobs_dir)):
            job_json = os.path.join(jobs_dir, entry, "job.json")
            if not os.path.isfile(job_json):
                continue
            try:
                with open(job_json, encoding="utf-8") as f:
                    job = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"fshorts: skipping {job_json}: {e}", file=sys.stderr)
                continue
            item = job_to_item(job, os.path.join(jobs_dir, entry))
            if _hub.upsert_item(conn, channel_id=channel_id, **item):
                changed += 1
    return changed


def enqueue(title: str, notes: str | None = None, db: str | None = None,
            pipeline_root: str | None = None) -> str:
    """Create a new fshorts job from a hub delegate request.

    Mirrors ingest.create_job() (kept as a local copy — adapters never
    import fshorts code; config.json is read as plain data when present):
    writes jobs/<id>/job.json with state 'new' and topic
    {title, source: 'hub-delegate', items: []} (items are filled in later
    by the scriptgen step). Then syncs so the item is visible in the hub
    immediately. Returns the hub item id 'fshorts-<jobid>'.
    """
    root = pipeline_root or PIPELINE_ROOT
    cfg = {}
    try:
        with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    now = datetime.now(timezone.utc)
    job_id = now.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:4]
    topic = {"title": title, "source": "hub-delegate", "items": []}
    if notes:
        topic["notes"] = notes
    job = {
        "id": job_id,
        "state": "new",
        "created_at": now.isoformat(),
        "topic": topic,
        "script": None,
        "voice": {
            "engine": cfg.get("voice_engine", "gemini"),
            "voice": cfg.get("voice_name", "Leda"),
        },
        "music_mood": cfg.get("music_mood", "upbeat"),
        "assets": {},
        "render": None,
        "telegram": {"message_id": None, "approved_by": None},
        "youtube": {"video_id": None, "publish_at": None,
                    "pinned_comment_id": None},
        "errors": [],
    }
    job_dir = os.path.join(root, cfg.get("jobs_dir", "jobs"), job_id)
    os.makedirs(job_dir, exist_ok=False)
    with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)
    sync(db=db, pipeline_root=root)  # make the item visible in the hub now
    return f"fshorts-{job_id}"


def _selftest() -> None:
    """Offline test: temp fixture jobs + temp hub DB, assert counts and
    idempotency. No network, no real pipeline access."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "fshorts")
        db = os.path.join(tmp, "posting.db")

        def make_job(job_id, state, message_id=None, video_id=None,
                     publish_at=None, hook=None, topic_title="T"):
            d = os.path.join(root, "jobs", job_id)
            os.makedirs(d)
            job = {
                "id": job_id, "state": state,
                "created_at": "2026-09-01T00:00:00+00:00",
                "topic": {"title": topic_title},
                "script": {"hook": hook} if hook else None,
                "telegram": {"message_id": message_id},
                "youtube": {"video_id": video_id, "publish_at": publish_at},
                "errors": [],
            }
            with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as f:
                json.dump(job, f)

        make_job("j1", "new")                                        # new
        make_job("j2", "rendered")                                   # new (not sent)
        make_job("j3", "rendered", message_id=123)                   # review
        make_job("j4", "approved", message_id=124, hook="Hook!")     # approved
        make_job("j5", "published", message_id=125, video_id="abc",
                 publish_at="2026-09-02T14:00:00+00:00")             # published
        make_job("j6", "rejected", message_id=126)                   # rejected
        make_job("j7", "review", message_id=127)                     # review
        # a dir without job.json must be skipped
        os.makedirs(os.path.join(root, "jobs", "junk"))

        n1 = sync(db=db, pipeline_root=root)
        assert n1 == 7, n1
        n2 = sync(db=db, pipeline_root=root)
        assert n2 == 0, f"not idempotent: {n2}"

        with _hub.connect(db) as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT * FROM items")}
            assert len(rows) == 7
            assert rows["fshorts-j1"]["state"] == "new"
            assert rows["fshorts-j2"]["state"] == "new"
            assert rows["fshorts-j3"]["state"] == "review"
            assert rows["fshorts-j4"]["state"] == "approved"
            assert rows["fshorts-j4"]["title"] == "Hook!"
            assert rows["fshorts-j1"]["title"] == "T"
            assert rows["fshorts-j5"]["state"] == "published"
            assert (rows["fshorts-j5"]["published_url"]
                    == "https://youtube.com/shorts/abc")
            assert (rows["fshorts-j5"]["scheduled_at"]
                    == "2026-09-02T14:00:00+00:00")
            assert rows["fshorts-j6"]["state"] == "rejected"
            assert rows["fshorts-j7"]["state"] == "review"
            assert rows["fshorts-j3"]["channel_id"] is None  # no channel registered
            # project auto-registered
            assert conn.execute(
                "SELECT 1 FROM projects WHERE alias='dressit-fshorts'").fetchone()

            # state-guard: hub-side 'scheduled' must survive a pipeline
            # re-sync that still reports 'approved'
            conn.execute(
                "UPDATE items SET state='scheduled' WHERE id='fshorts-j4'")
            conn.commit()
        n3 = sync(db=db, pipeline_root=root)
        with _hub.connect(db) as conn:
            r = conn.execute(
                "SELECT state FROM items WHERE id='fshorts-j4'").fetchone()
            assert r["state"] == "scheduled", r["state"]

        # enqueue: creates a real job dir + hub item, returns 'fshorts-<jobid>'
        item_id = enqueue("Delegated topic", notes="do it", db=db,
                          pipeline_root=root)
        assert item_id.startswith("fshorts-"), item_id
        job_id = item_id[len("fshorts-"):]
        job_json = os.path.join(root, "jobs", job_id, "job.json")
        assert os.path.isfile(job_json), job_json
        with open(job_json, encoding="utf-8") as f:
            job = json.load(f)
        assert job["state"] == "new"
        assert job["topic"] == {"title": "Delegated topic",
                                "source": "hub-delegate", "items": [],
                                "notes": "do it"}
        with _hub.connect(db) as conn:
            r = conn.execute("SELECT * FROM items WHERE id=?",
                             (item_id,)).fetchone()
            assert r is not None and r["state"] == "new", r

        print("fshorts selftest OK "
              f"(first={n1}, rerun={n2}, guarded-rerun={n3})")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(sync())
