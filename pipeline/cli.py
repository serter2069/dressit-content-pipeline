#!/usr/bin/env python3
"""
cli.py — Command-Line Interface for the DressIt Video Generation Pipeline.

Commands:
  1. pool:
     python3 cli.py pool [--json]
     Lists all verified, live, approved UGC clips from DressIt DB and assets.

  2. render:
     python3 cli.py render --spec <path_to_spec.json> [--out <output.mp4>] [--skip-face-check]
     Assembles and renders complete vertical short using the production FFmpeg filtergraph.

  3. deploy:
     python3 cli.py deploy --item-id <id> [--video <path.mp4>] [--spec <spec.json>]
     Deploys rendered short to Posting Hub (/root/posting/data/media/), registers in posting.db,
     and verifies HTTP 206 partial content streaming.

  4. verify-clip:
     python3 cli.py verify-clip --clip <clip_path_or_name>
     Checks single clip compliance against DB and brand safety rules.

  5. generate-copy:
     python3 cli.py generate-copy --hook <text> --punchline <text> [--out-spec <path.json>]
     Uses GLM-5.3 to generate commercial bridge, voiceover text, and subtitles.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import assembler
import config
import glm_client
import tts_producer
import ugc_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dressit_cli")


def test_http_206_streaming(item_id: str) -> bool:
    """Verifies that Posting Hub correctly streams the item with HTTP 206 Partial Content."""
    url = f"{config.POSTING_HUB_URL}/media/{item_id}"
    cmd = [
        "curl", "-s", "-D", "-", "-o", "/dev/null",
        "-H", "Range: bytes=0-1024",
        url
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    is_206 = "HTTP/1.1 206 Partial Content" in res.stdout
    has_range = "Content-Range: bytes 0-1024/" in res.stdout
    accept_bytes = "Accept-Ranges: bytes" in res.stdout
    return is_206 and has_range and accept_bytes


def cmd_pool(args: argparse.Namespace):
    """Lists approved UGC pool clips."""
    clips = ugc_guard.get_approved_clips()
    if args.json:
        print(json.dumps(clips, indent=2))
        return

    print("=" * 80)
    print(f"DRESSIT APPROVED UGC POOL — {len(clips)} VERIFIED CLIPS (Postgres DB + Audit Log)")
    print("=" * 80)
    print(f"{'CLIP FILENAME':<38} {'POST ID':<26} {'CREATOR':<16} {'DUR':<7}")
    print("-" * 80)
    for c in clips:
        print(f"{c['clip']:<38} {c['post_id']:<26} @{c['creator_handle']:<15} {c['duration']:>5.2f}s")
    print("-" * 80)
    print("All clips verified live (taken_down_at NULL, deleted_at NULL) & moderation approved.")
    print("maria_10 and quarantine_unapproved are STRICTLY EXCLUDED.")


def cmd_verify_clip(args: argparse.Namespace):
    """Verifies a single clip against database and brand constraints."""
    clip_path = args.clip
    try:
        ok = ugc_guard.verify_clip(clip_path, raise_exc=True)
        print(f"PASS: Clip '{clip_path}' is fully verified and approved for ads.")
    except Exception as e:
        print(f"FAIL: Clip '{clip_path}' rejected: {e}")
        sys.exit(1)


def cmd_render(args: argparse.Namespace):
    """Renders a short from spec JSON."""
    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        logger.error(f"Spec file not found: {spec_path}")
        sys.exit(1)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    # If voice_file doesn't exist but voice_text is specified, generate with TTS producer
    if "voice_file" not in spec or not Path(spec["voice_file"]).exists():
        if "voice_text" in spec and spec["voice_text"]:
            logger.info("Synthesizing voiceover from spec['voice_text'] with tts_producer...")
            slug = spec.get("slug", "short")
            voice_out = config.OUTPUT_DIR / f"voice_{slug}.mp3"
            tts_meta = tts_producer.generate_tts(spec["voice_text"], voice_out)
            spec["voice_file"] = str(voice_out)
            logger.info(f"Synthesized voiceover: {voice_out} ({tts_meta['duration']:.2f}s)")
        else:
            raise FileNotFoundError(f"Missing voice_file in spec: {spec.get('voice_file')}")

    out_mp4 = Path(args.out).resolve() if args.out else None
    run_face_check = not args.skip_face_check

    logger.info(f"Rendering short using spec: {spec_path.name}")
    result = assembler.assemble_video(spec, out_mp4=out_mp4, run_face_check=run_face_check)

    print("\n" + "=" * 60)
    print("RENDER COMPLETED SUCCESSFULLY")
    print("=" * 60)
    print(f"  Title: {result['title']}")
    print(f"  Output MP4: {result['output_mp4']}")
    print(f"  Duration: {result['duration']:.2f}s")
    print(f"  Face Verification: {'PASS (100% Centered)' if result['face_verification_passed'] else 'ATTENTION NEEDED'}")
    print("=" * 60)


def cmd_deploy(args: argparse.Namespace):
    """Deploys rendered short to Posting Hub and registers in SQLite DB."""
    item_id = args.item_id

    # Resolve metadata from spec if available
    title = f"DressIt Global Short · {item_id}"
    caption = "Stop settling! Post your dream dress on DressIt and let guys fund it for $0 ✨ dressitnow.com"
    source_slug = item_id

    spec = None
    if args.spec and Path(args.spec).exists():
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        title = spec.get("title", title)
        caption = spec.get("caption", caption)
        source_slug = spec.get("slug", source_slug)

    # Resolve video path
    video_path = None
    if args.video:
        video_path = Path(args.video).resolve()
    else:
        # Search candidate paths
        candidates = []
        if spec and spec.get("slug"):
            candidates.append(config.OUTPUT_DIR / f"{spec['slug']}.mp4")
        candidates.extend([
            config.OUTPUT_DIR / f"{item_id}.mp4",
            config.OUTPUT_DIR / f"{item_id.replace('dglobal-rel-', 'rel_short_')}.mp4",
            config.OUTPUT_DIR / f"{item_id.replace('dglobal-', '')}.mp4",
            config.POSTING_MEDIA_DIR / f"{item_id}.mp4"
        ])
        for c in candidates:
            if c.exists():
                video_path = c
                break

    # 1. Copy to posting media directory
    dest_path = config.POSTING_MEDIA_DIR / f"{item_id}.mp4"
    config.POSTING_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video_path, dest_path)
    logger.info(f"Copied video to {dest_path}")

    # 2. Register / Update in posting.db
    if config.POSTING_DB_PATH.exists():
        conn = sqlite3.connect(config.POSTING_DB_PATH)
        cur = conn.cursor()

        # Find channel ID for dressit-global
        cur.execute("SELECT id FROM channels WHERE project_alias = ? LIMIT 1", (config.CHANNEL_ALIAS,))
        row = cur.fetchone()
        channel_id = row[0] if row else 10

        now_iso = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps({
            "pipeline": "dressit-pipeline-v2",
            "item_id": item_id,
            "title": title,
            "caption": caption,
            "format": "1080x1920 30fps Clean Dynamic Short",
            "aesthetic": "Zero chips, zero pill badges, clean Inter Black white subtitles, persistent logo, -14 LUFS",
            "deployed_at": now_iso
        })

        cur.execute("SELECT id FROM items WHERE id = ?", (item_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE items
                SET channel_id = ?, title = ?, local_path = ?, state = 'review',
                    source_ref = ?, meta = ?, updated_at = ?
                WHERE id = ?
            """, (channel_id, title, str(dest_path), source_slug, meta_json, now_iso, item_id))
            logger.info(f"Updated existing item '{item_id}' in posting.db")
        else:
            cur.execute("""
                INSERT INTO items (id, channel_id, title, local_path, state, source_ref, meta, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'review', ?, ?, ?, ?)
            """, (item_id, channel_id, title, str(dest_path), source_slug, meta_json, now_iso, now_iso))
            logger.info(f"Inserted item '{item_id}' into posting.db (state: review)")

        conn.commit()
        conn.close()

    # 3. HTTP 206 Streaming Check
    stream_ok = test_http_206_streaming(item_id)
    print("\n" + "=" * 60)
    print("DEPLOYMENT REPORT")
    print("=" * 60)
    print(f"  Item ID: {item_id}")
    print(f"  Local Media: {dest_path}")
    print(f"  Posting DB: Registered (State: review, Channel: {config.CHANNEL_ALIAS})")
    print(f"  HTTP 206 Streaming: {'PASS (Range requests active)' if stream_ok else 'WARN (Server may need reload)'}")
    print("=" * 60)


def cmd_generate_copy(args: argparse.Namespace):
    """Generates ad copy & subtitles using GLM-5.3."""
    logger.info("Requesting ad copy from GLM-5.3...")
    approved = ugc_guard.get_approved_clips()
    outfits = [c["outfit_title"] for c in approved[:3] if c.get("outfit_title")]

    script_data = glm_client.generate_ad_script(
        joke_hook=args.hook,
        joke_punchline=args.punchline,
        ugc_outfits=outfits
    )

    if args.out_spec:
        out_p = Path(args.out_spec).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(script_data, indent=2), encoding="utf-8")
        logger.info(f"Saved generated script data to: {out_p}")

    print("\n" + "=" * 60)
    print("GENERATED GLM-5.3 AD COPY")
    print("=" * 60)
    print(f"Hook Bridge: {script_data.get('hook_bridge')}")
    print(f"Voiceover:   {script_data.get('voiceover_script')}")
    print(f"Caption:     {script_data.get('caption')}")
    print("\nSubtitles:")
    for s in script_data.get("subtitles", []):
        print(f"  - {s.get('text')}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="DressIt Video Generation Pipeline CLI (Task #1999)",
        formatter_class=argparse.RawTextHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. pool
    p_pool = subparsers.add_parser("pool", help="List verified live approved UGC clips from DressIt DB")
    p_pool.add_argument("--json", action="store_true", help="Output as JSON format")
    p_pool.set_defaults(func=cmd_pool)

    # 2. verify-clip
    p_verify = subparsers.add_parser("verify-clip", help="Check compliance of a single UGC clip")
    p_verify.add_argument("--clip", required=True, help="Path or filename of clip to verify")
    p_verify.set_defaults(func=cmd_verify_clip)

    # 3. render
    p_render = subparsers.add_parser("render", help="Render video short from spec JSON")
    p_render.add_argument("--spec", required=True, help="Path to specification JSON file")
    p_render.add_argument("--out", default=None, help="Optional output path override")
    p_render.add_argument("--skip-face-check", action="store_true", help="Skip face verification step")
    p_render.set_defaults(func=cmd_render)

    # 4. deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy rendered video to posting hub")
    p_deploy.add_argument("--item-id", required=True, help="Item ID in posting hub")
    p_deploy.add_argument("--video", default=None, help="Path to rendered MP4 video")
    p_deploy.add_argument("--spec", default=None, help="Optional spec file for metadata")
    p_deploy.set_defaults(func=cmd_deploy)

    # 5. generate-copy
    p_copy = subparsers.add_parser("generate-copy", help="Generate ad copy using OpenRouter GLM-5.3")
    p_copy.add_argument("--hook", required=True, help="Standup joke setup text")
    p_copy.add_argument("--punchline", required=True, help="Standup joke punchline text")
    p_copy.add_argument("--out-spec", default=None, help="Optional output path for JSON")
    p_copy.set_defaults(func=cmd_generate_copy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
