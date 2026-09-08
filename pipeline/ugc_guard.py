#!/usr/bin/env python3
"""
ugc_guard.py — Direct UGC clip validator against Postgres DB and approved pools.

Strict Rules (Task #1999):
- Validates against:
  1. Postgres DB (postgres://dressit_user:R8a3wKgwLJ23asPGgs7UdvHy@127.0.0.1:5432/dressit)
  2. /root/event-hero/assets/approved_ugc_pool.json
  3. /root/event-hero/assets/maria_ugc/manifest.json
- `verify_clip(clip_path)` ensures:
  * Has `payment_audit_log` event `post_moderation_approved` (subject_type='post')
  * Post is live (`taken_down_at IS NULL AND deleted_at IS NULL`)
  * Is NOT `maria_10` (strictly banned Decathlon poncho / tonality violation)
  * Is NOT in `quarantine_unapproved` directory or list
- `get_approved_clips()`:
  * Returns list of all verified approved clips with comprehensive metadata.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

import config

logger = logging.getLogger("ugc_guard")


class UGCValidationError(ValueError):
    """Raised when a UGC clip fails compliance or moderation verification."""
    pass


def get_db_connection():
    """Establishes connection to DressIt PostgreSQL database."""
    return psycopg2.connect(config.DB_URI)


def _load_pool_and_manifest() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Loads approved_ugc_pool.json and manifest.json if present."""
    pool_data = {}
    if config.APPROVED_UGC_POOL_PATH.exists():
        try:
            pool_json = json.loads(config.APPROVED_UGC_POOL_PATH.read_text(encoding="utf-8"))
            for clip in pool_json.get("clips", []):
                clip_name = clip.get("clip")
                if clip_name:
                    pool_data[clip_name] = clip
                    pool_data[Path(clip_name).name] = clip
                if clip.get("path"):
                    pool_data[str(Path(clip["path"]).resolve())] = clip
        except Exception as e:
            logger.warning(f"Failed reading approved_ugc_pool.json: {e}")

    manifest_data = {}
    if config.MANIFEST_PATH.exists():
        try:
            m_list = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
            for item in m_list:
                fname = item.get("filename")
                if fname:
                    manifest_data[fname] = item
                    manifest_data[Path(fname).name] = item
                if item.get("local_path"):
                    manifest_data[str(Path(item["local_path"]).resolve())] = item
        except Exception as e:
            logger.warning(f"Failed reading manifest.json: {e}")

    return pool_data, manifest_data


def probe_clip_duration(clip_path: Path) -> float:
    """Uses ffprobe to obtain accurate duration in seconds."""
    if not clip_path.exists():
        return 0.0
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(clip_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def verify_clip(clip_path: str | Path, raise_exc: bool = False) -> bool:
    """
    Validates a UGC clip against brand safety rules and Postgres database records.

    Criteria:
    1. Not in banned list (e.g. maria_10).
    2. Not in quarantine_unapproved directory.
    3. Associated post exists in DressIt DB.
    4. Post is live (taken_down_at IS NULL and deleted_at IS NULL).
    5. Has payment_audit_log event 'post_moderation_approved'.

    Args:
        clip_path: Path or filename of the video clip.
        raise_exc: If True, raises UGCValidationError with failure reason.

    Returns:
        bool: True if clip passes all checks, False otherwise.
    """
    p = Path(clip_path)
    fname = p.name
    str_path = str(p.resolve()) if p.exists() else str(clip_path)

    # Check 1: Explicitly banned clips (maria_10)
    for banned in config.BANNED_CLIPS:
        if banned in fname or banned in str_path:
            msg = f"REJECTED: Clip '{fname}' matches banned list item '{banned}' (decathlon poncho / tone ban)."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

    # Check 2: Quarantined / unapproved directory check
    if config.QUARANTINE_DIR_NAME in str_path or config.QUARANTINE_DIR_NAME in str(p.parent):
        msg = f"REJECTED: Clip '{fname}' is located in quarantine directory '{config.QUARANTINE_DIR_NAME}'."
        if raise_exc:
            raise UGCValidationError(msg)
        return False

    quarantine_dir = config.MARIA_UGC_DIR / config.QUARANTINE_DIR_NAME
    if quarantine_dir.exists():
        quarantined_files = {f.name for f in quarantine_dir.iterdir() if f.is_file()}
        if fname in quarantined_files:
            msg = f"REJECTED: Clip '{fname}' is in quarantined unapproved files list."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

    # Resolve post_id from pool or manifest
    pool_data, manifest_data = _load_pool_and_manifest()
    post_id = None

    if fname in pool_data:
        post_id = pool_data[fname].get("post_id")
    elif str_path in pool_data:
        post_id = pool_data[str_path].get("post_id")
    elif fname in manifest_data:
        post_id = manifest_data[fname].get("dressit_post_id")
    elif str_path in manifest_data:
        post_id = manifest_data[str_path].get("dressit_post_id")

    # If post_id wasn't in cache, query Postgres DB by image_key or filename
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if not post_id:
            # Try matching filename substring against post_images.image_key
            cur.execute("""
                SELECT post_id FROM post_images
                WHERE image_key = %s OR image_key LIKE %s
                ORDER BY created_at DESC LIMIT 1;
            """, (fname, f"%{fname}%"))
            row = cur.fetchone()
            if row:
                post_id = row["post_id"]

        if not post_id:
            msg = f"REJECTED: Clip '{fname}' cannot be mapped to any known DressIt post_id."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

        # Check 3, 4, 5: Post status and payment_audit_log
        cur.execute("""
            SELECT 
                p.id,
                p.taken_down_at,
                p.deleted_at,
                EXISTS (
                    SELECT 1 FROM payment_audit_log pal
                    WHERE pal.subject_id = p.id
                      AND pal.subject_type = 'post'
                      AND pal.event_type = 'post_moderation_approved'
                ) AS is_approved
            FROM posts p
            WHERE p.id = %s;
        """, (post_id,))
        post_row = cur.fetchone()

        if not post_row:
            msg = f"REJECTED: Post '{post_id}' does not exist in posts table."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

        if post_row["taken_down_at"] is not None:
            msg = f"REJECTED: Post '{post_id}' was taken down at {post_row['taken_down_at']}."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

        if post_row["deleted_at"] is not None:
            msg = f"REJECTED: Post '{post_id}' was deleted at {post_row['deleted_at']}."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

        if not post_row["is_approved"]:
            msg = f"REJECTED: Post '{post_id}' lacks 'post_moderation_approved' event in payment_audit_log."
            if raise_exc:
                raise UGCValidationError(msg)
            return False

        return True

    except Exception as e:
        if raise_exc:
            raise UGCValidationError(f"DB error during UGC verification: {e}")
        logger.error(f"DB error verifying clip {fname}: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_approved_clips() -> List[Dict[str, Any]]:
    """
    Returns a comprehensive list of all verified, approved, live UGC clips
    available on disk for video assembly.
    """
    pool_data, manifest_data = _load_pool_and_manifest()
    approved_clips = []

    # Gather local clips from maria_ugc dir
    ugc_files = []
    if config.MARIA_UGC_DIR.exists():
        for ext in ("*.mp4", "*.mov"):
            ugc_files.extend(list(config.MARIA_UGC_DIR.glob(ext)))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Query all live approved posts
        cur.execute("""
            SELECT 
                p.id AS post_id,
                p.caption,
                p.taken_down_at,
                p.deleted_at,
                u.handle AS creator_handle,
                pi.image_key
            FROM payment_audit_log pal
            JOIN posts p ON pal.subject_id = p.id AND pal.subject_type = 'post'
            LEFT JOIN users u ON p.user_id = u.id
            LEFT JOIN post_images pi ON p.id = pi.post_id
            WHERE pal.event_type = 'post_moderation_approved'
              AND p.taken_down_at IS NULL
              AND p.deleted_at IS NULL;
        """)
        live_approved_posts = {row["post_id"]: row for row in cur.fetchall()}

        for fpath in sorted(ugc_files, key=lambda x: x.name):
            fname = fpath.name

            # Skip banned
            if any(b in fname for b in config.BANNED_CLIPS):
                continue

            # Skip quarantine
            if config.QUARANTINE_DIR_NAME in str(fpath):
                continue

            # Identify post_id
            post_id = None
            outfit_title = ""
            creator_handle = ""
            image_key = ""

            if fname in pool_data:
                entry = pool_data[fname]
                post_id = entry.get("post_id")
                outfit_title = entry.get("outfit_title", "")
                creator_handle = entry.get("creator_handle", "")
                image_key = entry.get("image_key", "")
            elif fname in manifest_data:
                entry = manifest_data[fname]
                post_id = entry.get("dressit_post_id")
                outfit_title = entry.get("caption", "")
                creator_handle = entry.get("handle", "")
                image_key = entry.get("image_key", "")

            if post_id and post_id in live_approved_posts:
                db_info = live_approved_posts[post_id]
                outfit_title = outfit_title or db_info.get("caption") or ""
                creator_handle = creator_handle or db_info.get("creator_handle") or ""
                image_key = image_key or db_info.get("image_key") or ""
                dur = probe_clip_duration(fpath)

                approved_clips.append({
                    "clip": fname,
                    "path": str(fpath.resolve()),
                    "post_id": post_id,
                    "creator_handle": creator_handle,
                    "outfit_title": outfit_title,
                    "duration": dur,
                    "image_key": image_key,
                    "approval_status": "approved",
                    "payment_audit_approved": True,
                    "is_live": True,
                    "banned": False
                })

    except Exception as e:
        logger.error(f"Error fetching approved clips: {e}")
        raise
    finally:
        if conn:
            conn.close()

    return approved_clips


if __name__ == "__main__":
    print("=" * 60)
    print("Testing ugc_guard.py against DressIt Postgres DB...")
    print("=" * 60)

    # 1. Test verify_clip on banned maria_10
    is_m10 = verify_clip("maria_10_mary_vid_uLwcVgz6.mov")
    print(f"Test verify_clip('maria_10'): {'FAIL (should be False)' if is_m10 else 'PASS (rejected as banned)'}")

    # 2. Test verify_clip on quarantined clip
    is_q = verify_clip("/root/event-hero/assets/maria_ugc/quarantine_unapproved/maria_13_vid_hpcTKFfkgAiobT2qJmxPv.mp4")
    print(f"Test verify_clip('quarantine clip'): {'FAIL (should be False)' if is_q else 'PASS (rejected quarantine)'}")

    # 3. Test verify_clip on valid maria_01
    is_m1 = verify_clip("/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4")
    print(f"Test verify_clip('maria_01'): {'PASS (approved & live)' if is_m1 else 'FAIL (should be True)'}")

    # 4. List all approved clips
    clips = get_approved_clips()
    print(f"\nTotal verified approved clips: {len(clips)}")
    for c in clips:
        print(f"  ✓ {c['clip']} ({c['duration']:.2f}s) | Post: {c['post_id']} | @{c['creator_handle']}")
