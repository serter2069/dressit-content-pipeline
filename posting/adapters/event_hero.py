"""event-hero pipeline adapter for the posting hub.

Scans /root/event-hero/jobs/ (or job directories and output directory in /root/event-hero)
and upserts items into posting.db.

CLI:
  python3 adapters/event_hero.py            # sync once, prints count
  python3 adapters/event_hero.py --selftest # offline fixture test, exit 0
"""

import glob
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hub

PIPELINE_ROOT = os.environ.get("EVENT_HERO_PATH", "/root/event-hero")
PROJECT_ALIAS = "dressit-sf"
PROJECT_NAME = "DressIt · San Francisco"
PIPELINE_KIND = "custom"
PIPELINE_PATH = "/root/event-hero"
NOTES = "Городской канал DressIt San Francisco — анонсы концертов в SF/Bay Area, луки для девушек и аутро с бесплатными платьями на dressitnow.com"

DEFAULT_CHANNEL_HANDLE = "@dressitsf"
DEFAULT_TOKEN_PATH = "/root/dressit-shorts/token_youtube_wheretogosf.json"
DEFAULT_PUBLISH_TIMES = ["16:00", "21:00"]


def ensure_project(conn, alias: str = PROJECT_ALIAS, name: str = PROJECT_NAME,
                   pipeline_kind: str = PIPELINE_KIND,
                   pipeline_path: str = PIPELINE_PATH,
                   notes: str = NOTES) -> None:
    """Register or update project row."""
    conn.execute(
        """INSERT INTO projects (alias, name, pipeline_kind, pipeline_path, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(alias) DO UPDATE SET
             name = excluded.name,
             pipeline_kind = excluded.pipeline_kind,
             pipeline_path = excluded.pipeline_path,
             notes = COALESCE(excluded.notes, projects.notes)""",
        (alias, name, pipeline_kind, pipeline_path, notes, _hub.utcnow())
    )


def ensure_channel(conn, project_alias: str = PROJECT_ALIAS) -> int:
    """Find or register the default youtube channel for event-hero."""
    row = conn.execute(
        "SELECT id FROM channels WHERE project_alias = ? AND platform = 'youtube' AND active = 1 ORDER BY id LIMIT 1",
        (project_alias,)
    ).fetchone()
    if row:
        return row["id"] if isinstance(row, dict) or hasattr(row, "__getitem__") else row[0]

    cur = conn.execute(
        """INSERT INTO channels (project_alias, platform, handle, token_path, daily_limit, publish_times_utc, active)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        (project_alias, "youtube", DEFAULT_CHANNEL_HANDLE, DEFAULT_TOKEN_PATH, 2, json.dumps(DEFAULT_PUBLISH_TIMES))
    )
    return cur.lastrowid


def extract_script_info(script_path: str) -> dict:
    """Extract tags and hashtags from script.md if present."""
    if not os.path.isfile(script_path):
        return {}
    try:
        content = Path(script_path).read_text(encoding="utf-8")
    except Exception:
        return {}
    hashtags = []
    for line in content.splitlines():
        line_s = line.strip()
        if line_s.startswith("#") and not line_s.startswith("##") and not line_s.startswith("# "):
            tags = [w for w in line_s.split() if w.startswith("#")]
            hashtags.extend(tags)
    return {"tags": hashtags} if hashtags else {}


def parse_job_dir(job_dir: str) -> dict | None:
    """Parse a job directory into a Posting Hub item dictionary."""
    base_name = os.path.basename(job_dir.rstrip("/\\"))
    if not base_name or base_name in ("assets", "data", "out", "output", "logs", ".git", "venv", "__pycache__"):
        return None

    manifest_path = os.path.join(job_dir, "assembly_manifest.json")
    event_path = os.path.join(job_dir, "event_details.json")
    job_json_path = os.path.join(job_dir, "job.json")
    script_path = os.path.join(job_dir, "script.md")

    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    event_details = {}
    if os.path.isfile(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event_details = json.load(f)
        except Exception:
            pass

    job_data = {}
    if os.path.isfile(job_json_path):
        try:
            with open(job_json_path, "r", encoding="utf-8") as f:
                job_data = json.load(f)
        except Exception:
            pass

    jid = (manifest.get("job_id") or
           job_data.get("id") or
           job_data.get("job_id") or
           base_name)

    # Determine video path
    video_path = None
    rendered_info = manifest.get("rendered_preview") or {}
    if rendered_info.get("file") and os.path.isfile(rendered_info["file"]):
        video_path = rendered_info["file"]

    if not video_path:
        for fname in ("final.mp4", "assembled_preview.mp4", "output.mp4", "rendered.mp4", f"{jid}.mp4"):
            cand = os.path.join(job_dir, fname)
            if os.path.isfile(cand):
                video_path = cand
                break

    if not video_path:
        all_mp4s = sorted(glob.glob(os.path.join(job_dir, "*.mp4")))
        non_clips = [p for p in all_mp4s if not os.path.basename(p).startswith("clip")]
        if non_clips:
            video_path = non_clips[0]
        elif all_mp4s:
            video_path = all_mp4s[0]

    local_path = video_path if video_path else job_dir

    # Title
    verified_titles = {
        "job1": "Doja Cat at Chase Center SF! Free Concert Fits on DressIt #Shorts",
        "job2": "Sabrina Carpenter at Chase Center SF 2026! Concert Fits on DressIt #Shorts",
        "job3": "Olivia Rodrigo at Oakland Arena! Concert Outfits on DressIt #Shorts",
        "job4": "Gracie Abrams at Oakland Arena! Concert Outfits on DressIt #Shorts",
        "job5": "Phoebe Bridgers at Chase Center SF! Concert Fits on DressIt #Shorts",
        "job6": "Tyla Water Tour at Bill Graham Civic SF! Outfits on DressIt #Shorts",
        "job7": "Kacey Musgraves Deeper Well Tour SF! Outfits on DressIt #Shorts",
        "job8": "Hayley Williams at Shoreline SF! Outfits on DressIt #Shorts",
        "job9": "Kehlani Crash World Tour Bay Area! Concert Fits on DressIt #Shorts",
        "job10": "Young Miko at Chase Center SF! Concert Fits on DressIt #Shorts"
    }

    selected_event = event_details.get("selected_event") or {}
    if verified_titles.get(jid):
        title = verified_titles[jid]
    elif manifest.get("title"):
        title = manifest["title"]
    elif selected_event.get("artist") and selected_event.get("venue"):
        artist = selected_event["artist"]
        venue = selected_event["venue"]
        tour = selected_event.get("tour_name")
        if tour:
            title = f"{artist} · {tour} @ {venue}"
        else:
            title = f"{artist} @ {venue}"
    elif manifest.get("event"):
        event_name = manifest["event"]
        venue = manifest.get("venue")
        title = f"{event_name} @ {venue}" if venue else event_name
    elif job_data.get("title"):
        title = job_data["title"]
    elif job_data.get("event"):
        title = job_data["event"]
    else:
        title = f"DressIt SF · {jid}"

    # State
    raw_status = (
        rendered_info.get("status") or
        job_data.get("state") or
        job_data.get("status")
    )
    if raw_status:
        st = str(raw_status).lower()
        if st in ("ready", "rendered", "edited", "review"):
            state = "review"
        elif st in ("approved", "rejected", "scheduled", "published", "failed"):
            state = st
        elif st in ("error",):
            state = "failed"
        elif video_path:
            state = "review"
        else:
            state = "new"
    else:
        state = "review" if video_path else "new"

    # Meta
    script_info = extract_script_info(script_path)
    meta = {
        "job_id": jid,
        "pipeline_state": raw_status or ("ready" if video_path else "new"),
        "duration_seconds": manifest.get("duration_seconds") or rendered_info.get("duration"),
        "resolution": manifest.get("resolution") or rendered_info.get("resolution", "1080x1920"),
        "source": "event-hero",
    }
    if script_info.get("tags"):
        meta["tags"] = script_info["tags"]
    if manifest.get("tags"):
        meta["tags"] = manifest["tags"]
    if manifest.get("description"):
        meta["description"] = manifest["description"]
    if manifest.get("dressit_url"):
        meta["dressit_url"] = manifest["dressit_url"]
    if manifest.get("tickets_url"):
        meta["tickets_url"] = manifest["tickets_url"]
    if manifest.get("target_audience"):
        meta["target_audience"] = manifest["target_audience"]

    # Thumbnail auto-detection
    cand_thumb = os.path.join(job_dir, "thumbnail.jpg")
    thumbnail_path = None
    if os.path.isfile(cand_thumb):
        thumbnail_path = cand_thumb
    elif manifest.get("thumbnail_path") and os.path.isfile(manifest["thumbnail_path"]):
        thumbnail_path = manifest["thumbnail_path"]
    elif manifest.get("thumbnail") and os.path.isfile(manifest["thumbnail"]):
        thumbnail_path = manifest["thumbnail"]
    elif video_path:
        cand_v_thumb = os.path.join(os.path.dirname(video_path), "thumbnail.jpg")
        if os.path.isfile(cand_v_thumb):
            thumbnail_path = cand_v_thumb

    if thumbnail_path:
        meta["thumbnail_path"] = thumbnail_path
        meta["thumbnail"] = thumbnail_path

    if selected_event:
        meta["artist"] = selected_event.get("artist")
        meta["tour_name"] = selected_event.get("tour_name")
        meta["date"] = selected_event.get("date")
        meta["venue"] = selected_event.get("venue")
        meta["city"] = selected_event.get("city")
        meta["genre"] = selected_event.get("genre")
        meta["vibe"] = selected_event.get("vibe")
    elif manifest.get("event"):
        meta["event"] = manifest.get("event")
        meta["date"] = manifest.get("date")
        meta["venue"] = manifest.get("venue")

    yt = job_data.get("youtube") or {}
    video_id = yt.get("video_id") or job_data.get("youtube_id")
    published_url = f"https://youtube.com/shorts/{video_id}" if video_id else None
    scheduled_at = yt.get("publish_at") or job_data.get("scheduled_at")
    errors = job_data.get("errors") or []
    last_error = job_data.get("error") or (errors[-1].get("error") if errors else None)

    item_id = jid if jid.startswith("dressit-sf-") else f"dressit-sf-{jid}"

    return {
        "item_id": item_id,
        "title": title,
        "local_path": local_path,
        "state": state,
        "source_ref": jid,
        "scheduled_at": scheduled_at,
        "published_url": published_url,
        "error": last_error,
        "meta": meta,
    }


def parse_output_file(mp4_path: str) -> dict:
    """Parse an output video file from out/ or output/ directory."""
    stem = Path(mp4_path).stem
    json_path = os.path.splitext(mp4_path)[0] + ".json"
    meta_json = {}
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                meta_json = json.load(f)
        except Exception:
            pass

    jid = meta_json.get("job_id") or stem
    title = meta_json.get("title") or meta_json.get("event") or stem.replace("_", " ").title()
    item_id = jid if jid.startswith("dressit-sf-") else f"dressit-sf-{jid}"

    out_thumb = os.path.join(os.path.dirname(mp4_path), "thumbnail.jpg")
    if not os.path.isfile(out_thumb):
        cand1 = os.path.splitext(mp4_path)[0] + "_thumbnail.jpg"
        cand2 = os.path.splitext(mp4_path)[0] + ".jpg"
        if os.path.isfile(cand1):
            out_thumb = cand1
        elif os.path.isfile(cand2):
            out_thumb = cand2
        else:
            out_thumb = None

    meta = {
        "job_id": jid,
        "filename": os.path.basename(mp4_path),
        "source": "event-hero-out",
        **meta_json,
    }
    if out_thumb:
        meta["thumbnail_path"] = out_thumb
        meta["thumbnail"] = out_thumb

    return {
        "item_id": item_id,
        "title": title,
        "local_path": mp4_path,
        "state": meta_json.get("state", "review"),
        "source_ref": stem,
        "scheduled_at": meta_json.get("scheduled_at"),
        "published_url": meta_json.get("published_url"),
        "error": meta_json.get("error"),
        "meta": meta,
    }


def sync(db: str | None = None, pipeline_root: str | None = None) -> int:
    """Scan event-hero jobs and output directories and upsert items into posting.db."""
    root = pipeline_root or PIPELINE_ROOT
    if not os.path.isdir(root):
        return 0

    conn = _hub.connect(db)
    changed = 0
    try:
        ensure_project(conn, pipeline_path=root)
        channel_id = ensure_channel(conn)

        # 1. Discover subdirectories in jobs/
        job_dirs = []
        jobs_sub = os.path.join(root, "jobs")
        if os.path.isdir(jobs_sub):
            for entry in sorted(os.listdir(jobs_sub)):
                p = os.path.join(jobs_sub, entry)
                if os.path.isdir(p):
                    job_dirs.append(p)

        # 2. Discover job* or jobs directly under root
        IGNORE = {"assets", "data", "out", "output", "logs", ".git", "venv", "__pycache__", "jobs"}
        for entry in sorted(os.listdir(root)):
            if entry in IGNORE:
                continue
            p = os.path.join(root, entry)
            if os.path.isdir(p) and (
                entry.startswith("job") or
                os.path.isfile(os.path.join(p, "assembly_manifest.json")) or
                os.path.isfile(os.path.join(p, "event_details.json")) or
                os.path.isfile(os.path.join(p, "job.json"))
            ):
                if p not in job_dirs:
                    job_dirs.append(p)

        seen_item_ids = set()

        for jdir in job_dirs:
            item = parse_job_dir(jdir)
            if item:
                seen_item_ids.add(item["item_id"])
                if _hub.upsert_item(conn, channel_id=channel_id, **item):
                    changed += 1

        # 3. Discover video files in out/ or output/
        for out_name in ("out", "output"):
            out_dir = os.path.join(root, out_name)
            if os.path.isdir(out_dir):
                for mp4 in sorted(glob.glob(os.path.join(out_dir, "*.mp4"))):
                    out_item = parse_output_file(mp4)
                    if out_item["item_id"] not in seen_item_ids:
                        seen_item_ids.add(out_item["item_id"])
                        if _hub.upsert_item(conn, channel_id=channel_id, **out_item):
                            changed += 1

        conn.commit()
    finally:
        conn.close()

    return changed


sync_all = sync  # alias for callers expecting sync_all


def enqueue(title: str, notes: str | None = None, db: str | None = None,
            pipeline_root: str | None = None) -> str:
    """Create a new event-hero job from a hub delegate request."""
    root = pipeline_root or PIPELINE_ROOT
    jobs_dir = os.path.join(root, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_id = f"job_{ts}_{uuid.uuid4().hex[:4]}"
    job_dir = os.path.join(jobs_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    job_data = {
        "id": job_id,
        "title": title,
        "notes": notes,
        "state": "new",
        "created_at": _hub.utcnow(),
    }
    with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(job_data, f, indent=2, ensure_ascii=False)
    sync(db=db, pipeline_root=root)
    return f"event-hero-{job_id}"


def upload_job(job_dir: str, thumbnail_path: str | None = None, privacy: str = "public") -> dict:
    """Upload an event-hero job's video to YouTube Shorts using youtube_uploader.

    Auto-detects thumbnail.jpg in the job directory or next to the video if not explicitly provided.
    """
    if PIPELINE_ROOT not in sys.path:
        sys.path.insert(0, PIPELINE_ROOT)
    try:
        import youtube_uploader
    except ImportError as e:
        raise RuntimeError(f"Cannot import youtube_uploader from {PIPELINE_ROOT}: {e}")

    job_p = Path(job_dir)
    video_file = None
    for cand in ("final.mp4", "assembled_preview.mp4", "output.mp4", "rendered.mp4"):
        if (job_p / cand).is_file():
            video_file = job_p / cand
            break
    if not video_file:
        all_mp4s = sorted(glob.glob(os.path.join(str(job_p), "*.mp4")))
        non_clips = [p for p in all_mp4s if not os.path.basename(p).startswith("clip")]
        if non_clips:
            video_file = Path(non_clips[0])
        elif all_mp4s:
            video_file = Path(all_mp4s[0])

    if not video_file or not video_file.is_file():
        raise FileNotFoundError(f"No video file found in {job_dir}")

    # Auto-detect thumbnail.jpg if not passed
    if not thumbnail_path:
        cand_thumb = job_p / "thumbnail.jpg"
        if cand_thumb.is_file():
            thumbnail_path = str(cand_thumb)
        else:
            cand_v_thumb = video_file.parent / "thumbnail.jpg"
            if cand_v_thumb.is_file():
                thumbnail_path = str(cand_v_thumb)

    manifest_path = job_p / "assembly_manifest.json"
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    title = manifest.get("title", f"DressIt SF · {job_p.name}")
    desc = manifest.get("description", "")
    tags = manifest.get("tags", ["#DressIt", "#Shorts"])

    return upload_short(
        str(video_file),
        title,
        desc,
        tags,
        privacy=privacy,
        thumbnail_path=thumbnail_path
    )


def upload_short(*args, **kwargs) -> dict:
    """Proxy to event-hero youtube_uploader.upload_short with auto-detected thumbnail support."""
    if PIPELINE_ROOT not in sys.path:
        sys.path.insert(0, PIPELINE_ROOT)
    import youtube_uploader
    return youtube_uploader.upload_short(*args, **kwargs)


def _selftest() -> None:
    """Offline selftest in a temporary directory."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db_file = os.path.join(tmp, "posting.db")
        job1 = os.path.join(tmp, "job1")
        os.makedirs(job1, exist_ok=True)
        manifest_data = {
            "job_id": "job1",
            "event": "Test Concert Event",
            "rendered_preview": {
                "status": "ready",
                "file": os.path.join(job1, "assembled_preview.mp4")
            }
        }
        with open(os.path.join(job1, "assembly_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)
        with open(os.path.join(job1, "assembled_preview.mp4"), "w", encoding="utf-8") as f:
            f.write("fake video content")
        with open(os.path.join(job1, "thumbnail.jpg"), "w", encoding="utf-8") as f:
            f.write("fake thumbnail content")

        # First sync: 1 item changed
        n = sync(db=db_file, pipeline_root=tmp)
        assert n == 1, f"Expected 1 changed item, got {n}"

        # Re-sync: idempotent, 0 changed
        n2 = sync(db=db_file, pipeline_root=tmp)
        assert n2 == 0, f"Expected 0 changed items on rerun, got {n2}"

        # Enqueue: creates new job and syncs
        qid = enqueue("New Test Event", notes="Some notes", db=db_file, pipeline_root=tmp)
        assert qid.startswith("event-hero-job_"), f"Unexpected qid: {qid}"

        # Verify items in db
        c = _hub.connect(db_file)
        rows = c.execute("SELECT * FROM items ORDER BY id").fetchall()
        c.close()
        assert len(rows) == 2, f"Expected 2 items in db, got {len(rows)}"

        # Verify thumbnail auto-detection in meta
        job1_row = [r for r in rows if "job1" in (r["id"] if hasattr(r, "__getitem__") else r[0])][0]
        meta = json.loads(job1_row["meta"] if isinstance(job1_row, dict) or hasattr(job1_row, "__getitem__") else job1_row[9])
        assert meta.get("thumbnail_path") == os.path.join(job1, "thumbnail.jpg"), f"Expected thumbnail_path in meta, got {meta}"

    print("event_hero adapter selftest OK")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    n = sync()
    print(f"event_hero: synced {n} items")


if __name__ == "__main__":
    main()
