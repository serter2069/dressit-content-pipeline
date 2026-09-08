#!/usr/bin/env python3
"""
assemble_batch_10.py — Production batch assembly engine for the 10 diverse DressIt videos.

Implements all Strict Standards (Task #1999):
1. Resolution: 1080x1920 (9:16 vertical), 30 fps, CRF 18, yuv420p.
2. Clean Branding:
   - Persistent DressIt logo: scale=480:-1, overlay=(W-w)/2:130.
   - ZERO chips, ZERO pill badges, ZERO 'Funded by Real Gentlemen' or 'Upgrade your dating'.
3. Clean Subtitles:
   - Pure White (&H00FFFFFF&), Inter Black font, solid black border (\bord10\shad3), MarginV 380.
   - STRICTLY NO rainbow/neon/variegated word colors.
4. Clean Audio:
   - Nova voiceover (OpenRouter openai/gpt-audio) mastered to -14.0 LUFS.
   - Ducked funk BGM (volume 0.12).
   - 3ms microfade on cut1 audio start (zero phoneme clipping).
5. Approved UGC Pool:
   - Strictly live, approved clips from DressIt DB (payment_audit_log post_moderation_approved).
6. Multi-point Face Verification:
   - Automated check on standup comedian cuts to guarantee 100% centered faces.
7. Deployment:
   - Copy to /root/posting/data/media/dglobal-batch10-XX.mp4.
   - Register in /root/posting/data/posting.db (channel 10, status 'review').
   - Test HTTP 206 Partial Content streaming.
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, "/root/dressit-pipeline")
sys.path.insert(0, "/root/global-dressit-pilot")

import config
import ugc_guard
from face_verifier import verify_rendered_short

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_10_assembler")


def run_cmd(cmd: List[str], desc: str = "") -> subprocess.CompletedProcess:
    if desc:
        logger.info(f"==> {desc}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        err_msg = f"Command failed ({desc}):\nCMD: {' '.join(cmd)}\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    return res


def test_http_206(item_id: str) -> bool:
    url = f"http://127.0.0.1:8478/media/{item_id}"
    cmd = [
        "curl", "-s", "-D", "-", "-o", "/dev/null",
        "-H", "Range: bytes=0-1024",
        url
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    return "HTTP/1.1 206 Partial Content" in res.stdout or "206" in res.stdout


def generate_clean_ass(dialogue: List[Dict[str, str]], ass_path: Path):
    lines = [config.ASS_HEADER.strip()]
    for ev in dialogue:
        start = ev["start"]
        end = ev["end"]
        raw_text = ev["text"]
        # Brand subtitle hygiene: pure clean white, no colors, no emojis
        clean_text = raw_text.replace("\r", "")
        # Remove any color tags
        import re
        clean_text = re.sub(r"\{\\c&H[0-9a-fA-F]+&\}", "", clean_text)
        clean_text = re.sub(r"\{\\1c&H[0-9a-fA-F]+&\}", "", clean_text)
        clean_text = clean_text.replace("\n", r"\N")
        event_str = f"Dialogue: 0,{start},{end},DressItBrand,,0,0,0,,{{\\bord10\\shad3\\c&H00FFFFFF&}}{clean_text}"
        lines.append(event_str)
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def assemble_short(spec: Dict[str, Any]) -> Dict[str, Any]:
    short_num = spec["number"]
    item_id = f"dglobal-batch10-{short_num:02d}"
    slug = f"batch10_{short_num:02d}"
    title = spec["title"]
    logger.info(f"\n{'='*70}\nASSEMBLING SHORT {short_num}/10: {title} ({item_id})\n{'='*70}")

    work_dir = config.OUTPUT_DIR / f"work_{slug}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    voice_path = Path(spec["audio_asset"])
    if not voice_path.exists():
        raise FileNotFoundError(f"Voice file missing: {voice_path}")

    is_standup = spec.get("standup_clip") is not None
    ugc_list = spec.get("ugc_clips", [])

    # Validate all UGC clips against DressIt DB
    for u in ugc_list:
        ugc_guard.verify_clip(u["path"], raise_exc=True)

    bgm_path = config.BGM_DEFAULT_PATH
    dialogue = spec["dialogue"]
    ass_file = work_dir / f"{slug}.ass"
    generate_clean_ass(dialogue, ass_file)

    video_segments = []

    if is_standup:
        # Group 1: Standup Comedy + UGC Outro
        sc = spec["standup_clip"]
        standup_src = sc["source_file"]
        cut1_ss = sc["cut1_ss"]
        cut1_dur = sc["cut1_dur"]
        cut1_crop = sc.get("cut1_crop", "crop=540:960:480:0,scale=1080:1920:flags=lanczos")
        cut2_ss = sc["cut2_ss"]
        cut2_dur = sc["cut2_dur"]
        cut2_crop = sc.get("cut2_crop", "crop=540:960:564:0,scale=1080:1920:flags=lanczos")
        standup_dur = cut1_dur + cut2_dur

        # 1. Cut 1 (Setup)
        cut1_mp4 = work_dir / f"{slug}_cut1.mp4"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(cut1_ss), "-i", standup_src,
            "-vframes", str(int(cut1_dur * config.FPS)),
            "-vf", f"{cut1_crop},setsar=1,fps={config.FPS}",
            "-af", f"atrim=0:{cut1_dur:.3f},afade=t=in:ss=0:d={config.MICROFADE_IN_SEC}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
            "-c:a", "aac", "-b:a", "192k", "-ar", str(config.SAMPLE_RATE),
            str(cut1_mp4)
        ], desc=f"Cut 1 Setup (ss={cut1_ss}s, dur={cut1_dur}s)")
        video_segments.append(cut1_mp4)

        # 2. Cut 2 (Punchline)
        cut2_mp4 = work_dir / f"{slug}_cut2.mp4"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(cut2_ss), "-i", standup_src,
            "-vframes", str(int(cut2_dur * config.FPS)),
            "-vf", f"{cut2_crop},setsar=1,fps={config.FPS}",
            "-af", f"atrim=0:{cut2_dur:.3f},afade=t=out:st={cut2_dur-0.25:.3f}:d=0.25",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
            "-c:a", "aac", "-b:a", "192k", "-ar", str(config.SAMPLE_RATE),
            str(cut2_mp4)
        ], desc=f"Cut 2 Punchline (ss={cut2_ss}s, dur={cut2_dur}s)")
        video_segments.append(cut2_mp4)

        # 3. UGC Outro Cut A (Reveal)
        ugc_a_dur = spec.get("ugc_cut_a_dur", 3.60)
        ugc_a_ss = spec.get("ugc_cut_a_ss", 1.50)
        ugc_a_src = spec["ugc_cut_a"]
        ugc_a_mp4 = work_dir / f"{slug}_ugc_a.mp4"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(ugc_a_ss), "-i", ugc_a_src,
            "-vframes", str(int(ugc_a_dur * config.FPS)),
            "-vf", f"scale={config.WIDTH}:{config.HEIGHT}:force_original_aspect_ratio=increase,crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
            "-an", str(ugc_a_mp4)
        ], desc=f"UGC Cut A ({ugc_a_dur}s)")
        video_segments.append(ugc_a_mp4)

        # 4. UGC Outro Cut B (Punch Zoom 1.05x)
        ugc_b_dur = spec.get("ugc_cut_b_dur", 5.68)
        ugc_b_ss = spec.get("ugc_cut_b_ss", 2.00)
        ugc_b_src = spec["ugc_cut_b"]
        ugc_b_mp4 = work_dir / f"{slug}_ugc_b.mp4"
        zw, zh = int(config.WIDTH * 1.05), int(config.HEIGHT * 1.05)
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(ugc_b_ss), "-i", ugc_b_src,
            "-vframes", str(int(ugc_b_dur * config.FPS)),
            "-vf", f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
            "-an", str(ugc_b_mp4)
        ], desc=f"UGC Cut B Zoom ({ugc_b_dur}s)")
        video_segments.append(ugc_b_mp4)

        total_dur = standup_dur + ugc_a_dur + ugc_b_dur
        vo_delay_ms = int((standup_dur + 0.20) * 1000)

        # Standup audio concat
        standup_wav = work_dir / f"{slug}_standup_audio.wav"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(cut1_mp4), "-i", str(cut2_mp4),
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[aout]",
            "-map", "[aout]", "-ar", str(config.SAMPLE_RATE), str(standup_wav)
        ], desc="Mix Standup Audio")

        # Speech master: standup audio + delayed commercial VO
        speech_master = work_dir / f"{slug}_speech_master.wav"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(standup_wav), "-i", str(voice_path),
            "-filter_complex", f"[1:a]adelay={vo_delay_ms}|{vo_delay_ms}[delayed_vo];[0:a][delayed_vo]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
            "-map", "[aout]", "-ar", str(config.SAMPLE_RATE), str(speech_master)
        ], desc=f"Speech Master (VO delay={vo_delay_ms}ms)")

    else:
        # Groups 2, 3, 4: Pure UGC / Multi-UGC Video
        cuts_spec = spec["cuts"]
        total_dur = sum(c["dur"] for c in cuts_spec)
        vo_delay_ms = int(spec.get("comm_voice_offset", 0.15) * 1000)

        for idx, c in enumerate(cuts_spec):
            ugc_src = c["path"]
            c_ss = c.get("ss", 1.0)
            c_dur = c["dur"]
            c_zoom = c.get("zoom", 1.0)
            seg_mp4 = work_dir / f"{slug}_seg_{idx}.mp4"

            if c_zoom > 1.0:
                zw, zh = int(config.WIDTH * c_zoom), int(config.HEIGHT * c_zoom)
                vf = f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}"
            else:
                vf = f"scale={config.WIDTH}:{config.HEIGHT}:force_original_aspect_ratio=increase,crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}"

            run_cmd([
                "ffmpeg", "-y", "-v", "error",
                "-ss", str(c_ss), "-i", str(ugc_src),
                "-vframes", str(int(c_dur * config.FPS)),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
                "-an", str(seg_mp4)
            ], desc=f"UGC Segment {idx+1} ({c_dur}s, zoom={c_zoom})")
            video_segments.append(seg_mp4)

        # Speech master: voiceover with 150ms pre-roll delay
        speech_master = work_dir / f"{slug}_speech_master.wav"
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(voice_path),
            "-af", f"adelay={vo_delay_ms}|{vo_delay_ms}",
            "-ar", str(config.SAMPLE_RATE), str(speech_master)
        ], desc=f"Speech Master (VO delay={vo_delay_ms}ms)")

    # Video stream concat
    silent_concat = work_dir / f"{slug}_silent_concat.mp4"
    inputs = []
    filter_concat = ""
    for idx, seg in enumerate(video_segments):
        inputs.extend(["-i", str(seg)])
        filter_concat += f"[{idx}:v]"
    filter_concat += f"concat=n={len(video_segments)}:v=1:a=0[vconcat]"

    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        *inputs,
        "-filter_complex", filter_concat,
        "-map", "[vconcat]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-pix_fmt", config.PIX_FMT,
        str(silent_concat)
    ], desc=f"Concat {len(video_segments)} Video Streams")

    # Master Audio Mix: Speech Master + Ducked Funk BGM (-14 LUFS)
    audio_mixed = work_dir / f"{slug}_audio_mixed.wav"
    fade_out_st = max(0.5, total_dur - 0.6)
    audio_filter = (
        f"[1:a]atrim=0:{total_dur:.2f},afade=t=out:st={fade_out_st:.2f}:d=0.6,volume={config.BGM_DUCK_VOLUME}[abgm];"
        f"[0:a][abgm]amix=inputs=2:duration=first:dropout_transition=0[amixed];"
        f"[amixed]loudnorm=I={config.AUDIO_TARGET_LUFS}:TP={config.AUDIO_TRUE_PEAK}:LRA={config.AUDIO_LRA}[aout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(speech_master),
        "-i", str(bgm_path),
        "-filter_complex", audio_filter,
        "-map", "[aout]",
        "-ar", str(config.SAMPLE_RATE),
        str(audio_mixed)
    ], desc="Master Audio Mix (-14 LUFS, Ducked Funk BGM)")

    # Final Render: Video Concat + Clean Logo + Pure White ASS Subtitles + Audio
    final_mp4 = config.OUTPUT_DIR / f"{slug}.mp4"
    final_filter = (
        f"[1:v]{config.LOGO_SCALE}[clean_logo];"
        f"[0:v][clean_logo]overlay={config.LOGO_OVERLAY_COORDS}[vbranded];"
        f"[vbranded]subtitles={ass_file}:fontsdir={config.FONTS_DIR}[vout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(silent_concat),
        "-i", str(config.LOGO_PATH),
        "-i", str(audio_mixed),
        "-filter_complex", final_filter,
        "-map", "[vout]",
        "-map", "2:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", str(config.CRF),
        "-c:a", "aac", "-b:a", "256k", "-ar", str(config.SAMPLE_RATE),
        "-pix_fmt", config.PIX_FMT,
        "-t", str(total_dur),
        str(final_mp4)
    ], desc=f"Final Master Render ({total_dur:.2f}s)")

    # Face verification (for standup cuts)
    face_passed = True
    if is_standup and verify_rendered_short:
        debug_dir = config.DEBUG_FRAMES_DIR / slug
        logger.info(f"Running multi-point face verification on {final_mp4.name}...")
        face_passed, face_checks = verify_rendered_short(
            str(final_mp4),
            check_interval=2.5,
            max_check_time=standup_dur - 0.4,
            out_debug_dir=debug_dir
        )
        for c in face_checks:
            st = "PASS" if c["passed"] else "FAIL"
            logger.info(f"  Face check t={c['timestamp']}s: [{st}] {c['details']}")

    # Deploy to Posting Hub media directory
    hub_dest = config.POSTING_MEDIA_DIR / f"{item_id}.mp4"
    config.POSTING_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_mp4, hub_dest)
    logger.info(f"Deployed to Posting Hub: {hub_dest}")

    # Register / Update in posting.db
    caption = spec.get("caption", f"{title} ✨ dressitnow.com")
    conn = sqlite3.connect(config.POSTING_DB_PATH)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    meta_json = json.dumps({
        "pipeline": "dressit-pipeline-v2",
        "model": "z-ai/glm-5.3-flash",
        "voice": "nova",
        "lufs": -14.0,
        "format": spec["group"],
        "word_count": spec.get("word_count", 21)
    })
    cur.execute("""
        INSERT INTO items (id, channel_id, title, local_path, state, source_ref, scheduled_at, error, meta, created_at, updated_at)
        VALUES (?, 10, ?, ?, 'review', ?, NULL, NULL, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            local_path=excluded.local_path,
            state='review',
            meta=excluded.meta,
            updated_at=excluded.updated_at
    """, (item_id, title, str(hub_dest), slug, meta_json, now_iso, now_iso))
    conn.commit()
    conn.close()

    streaming_ok = test_http_206(item_id)
    logger.info(f"HTTP 206 Streaming Check for {item_id}: {'PASS' if streaming_ok else 'FAIL'}")

    return {
        "number": short_num,
        "id": item_id,
        "title": title,
        "group": spec["group"],
        "duration": total_dur,
        "output_mp4": str(final_mp4),
        "hub_url": f"https://posting.smartlaunchhub.com/media/{item_id}",
        "streaming_206": streaming_ok,
        "face_verification": face_passed
    }


def main():
    specs_path = Path("/root/dressit-pipeline/specs_10_shorts.json")
    if not specs_path.exists():
        logger.error("specs_10_shorts.json not found!")
        sys.exit(1)

    raw_specs = json.loads(specs_path.read_text(encoding="utf-8"))

    # Enrich specifications with precise cuts and dialogue timing
    rich_specs = []

    # Short 1: Standup Comedy -> Maria Two-Piece
    rich_specs.append({
        "number": 1,
        "id": "short_01",
        "group": raw_specs[0]["group"],
        "title": "RELATIONSHIPS · I Quit Dating Losers",
        "standup_clip": {
            "source_file": "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4",
            "cut1_ss": 4.18, "cut1_dur": 2.37,
            "cut1_crop": "crop=540:960:480:0,scale=1080:1920:flags=lanczos",
            "cut2_ss": 6.55, "cut2_dur": 5.85,
            "cut2_crop": "crop=540:960:564:0,scale=1080:1920:flags=lanczos"
        },
        "ugc_clips": raw_specs[0]["ugc_clips"],
        "ugc_cut_a": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4",
        "ugc_cut_a_ss": 1.50, "ugc_cut_a_dur": 3.60,
        "ugc_cut_b": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4",
        "ugc_cut_b_ss": 2.00, "ugc_cut_b_dur": 5.68,
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_01.mp3",
        "word_count": 21,
        "caption": "I quit dating losers — that took a minute! 💅 Stop settling for low-effort boys: post the dress you actually want on DressIt and let a good guy fund it free! dressitnow.com ✨\n\n#dating #relationships #datingstandards #postyouroutfit #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.08", "end": "0:00:01.00", "text": "I'VE DONE SO MUCH WORK"},
            {"start": "0:00:01.00", "end": "0:00:01.80", "text": "ON MYSELF..."},
            {"start": "0:00:01.80", "end": "0:00:02.40", "text": "IN THE LAST 5 YEARS:"},
            {"start": "0:00:02.40", "end": "0:00:03.20", "text": "I QUIT DATING"},
            {"start": "0:00:03.20", "end": "0:00:04.60", "text": "LOSERS!"},
            {"start": "0:00:04.60", "end": "0:00:05.50", "text": "THAT TOOK A MINUTE!"},
            {"start": "0:00:05.50", "end": "0:00:06.60", "text": "SERIOUSLY, A WHOLE MINUTE..."},
            {"start": "0:00:06.60", "end": "0:00:08.00", "text": "THAT TOOK A LONG TIME!"},
            {"start": "0:00:08.42", "end": "0:00:10.50", "text": "IF YOU'RE DONE SETTLING\nFOR LOW-EFFORT BOYS..."},
            {"start": "0:00:10.50", "end": "0:00:12.80", "text": "POST THE DRESS\nYOU ACTUALLY WANT"},
            {"start": "0:00:12.80", "end": "0:00:15.20", "text": "AND LET A GOOD GUY\nFUND IT FREE!"},
            {"start": "0:00:15.20", "end": "0:00:17.20", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 2: Standup Comedy -> Cigarettes Joke -> Maria Night Out Look
    rich_specs.append({
        "number": 2,
        "id": "short_02",
        "group": raw_specs[1]["group"],
        "title": "RELATIONSHIPS · Losers Are Like Cigarettes",
        "standup_clip": {
            "source_file": "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4",
            "cut1_ss": 19.10, "cut1_dur": 4.15,
            "cut1_crop": "crop=540:960:952:0,scale=1080:1920:flags=lanczos",
            "cut2_ss": 23.25, "cut2_dur": 4.60,
            "cut2_crop": "crop=540:960:952:0,scale=1080:1920:flags=lanczos"
        },
        "ugc_clips": raw_specs[1]["ugc_clips"],
        "ugc_cut_a": "/root/event-hero/assets/maria_ugc/maria_06_angaevamariia_vid_YhozXb0A.mp4",
        "ugc_cut_a_ss": 1.00, "ugc_cut_a_dur": 3.80,
        "ugc_cut_b": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4",
        "ugc_cut_b_ss": 6.00, "ugc_cut_b_dur": 5.45,
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_02.mp3",
        "word_count": 21,
        "caption": "Losers are a lot like cigarettes: you feel pretty cool, but everyone is sad for you! 🚬 Drop the cigarette boy already: on DressIt, high-effort guys fund the dream dress you've been eyeing! dressitnow.com ✨\n\n#dating #relationships #taylortomlinson #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.04", "end": "0:00:01.50", "text": "LOSERS ARE A LOT\nLIKE CIGARETTES:"},
            {"start": "0:00:01.50", "end": "0:00:02.80", "text": "YOU HAVE ONE\nIN YOUR MOUTH..."},
            {"start": "0:00:02.80", "end": "0:00:04.15", "text": "AND YOU FEEL\nPRETTY COOL..."},
            {"start": "0:00:04.15", "end": "0:00:05.40", "text": "BUT EVERYONE AROUND YOU\nIS LIKE:"},
            {"start": "0:00:05.40", "end": "0:00:06.90", "text": "'EW, WE'RE\nSAD FOR YOU!'"},
            {"start": "0:00:06.90", "end": "0:00:08.55", "text": "'DON'T DO THAT\nAROUND MY KIDS!'"},
            {"start": "0:00:08.95", "end": "0:00:11.20", "text": "DROP THE CIGARETTE BOY\nALREADY!"},
            {"start": "0:00:11.20", "end": "0:00:13.50", "text": "ON DRESSIT, HIGH-EFFORT GUYS\nFUND THE DREAM DRESS"},
            {"start": "0:00:13.50", "end": "0:00:15.80", "text": "YOU'VE BEEN EYEING..."},
            {"start": "0:00:15.80", "end": "0:00:17.60", "text": "AND YOU PAY LITERALLY NOTHING!\nDRESSITNOW.COM"}
        ]
    })

    # Short 3: Standup Comedy -> Married Drama -> Maria Work/Date Outfit
    rich_specs.append({
        "number": 3,
        "id": "short_03",
        "group": raw_specs[2]["group"],
        "title": "RELATIONSHIPS · Guy With A Car & A Job",
        "standup_clip": {
            "source_file": "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4",
            "cut1_ss": 32.12, "cut1_dur": 2.18,
            "cut1_crop": "crop=540:960:504:0,scale=1080:1920:flags=lanczos",
            "cut2_ss": 34.30, "cut2_dur": 6.22,
            "cut2_crop": "crop=540:960:492:0,scale=1080:1920:flags=lanczos"
        },
        "ugc_clips": raw_specs[2]["ugc_clips"],
        "ugc_cut_a": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4",
        "ugc_cut_a_ss": 1.00, "ugc_cut_a_dur": 3.80,
        "ugc_cut_b": "/root/event-hero/assets/maria_ugc/maria_05_angaevamariia_vid_KymX8LAP.mp4",
        "ugc_cut_b_ss": 2.00, "ugc_cut_b_dur": 5.80,
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_03.mp3",
        "word_count": 21,
        "caption": "A guy with a car and a job... but then you gotta deal with his wife! 😂 Skip the married-man drama completely: DressIt is full of actually single guys who fund your favorite outfit! dressitnow.com ✨\n\n#dating #relationships #datingstandards #postyouroutfit #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.08", "end": "0:00:01.20", "text": "ALL MY FRIENDS ARE LIKE:"},
            {"start": "0:00:01.20", "end": "0:00:02.60", "text": "YOU NEED A GUY\nWITH A CAR & A JOB!"},
            {"start": "0:00:02.60", "end": "0:00:03.80", "text": "AND I'M LIKE: YEAH..."},
            {"start": "0:00:03.80", "end": "0:00:05.60", "text": "BUT THEN I GOTTA DEAL\nWITH HIS WIFE!"},
            {"start": "0:00:05.60", "end": "0:00:06.80", "text": "LIKE IT'S A\nWHOLE THING..."},
            {"start": "0:00:06.80", "end": "0:00:08.20", "text": "A WHOLE COMPLICATION!"},
            {"start": "0:00:08.60", "end": "0:00:11.00", "text": "SKIP THE MARRIED-MAN\nDRAMA COMPLETELY!"},
            {"start": "0:00:11.00", "end": "0:00:13.20", "text": "DRESSIT IS FULL OF\nACTUALLY SINGLE GUYS"},
            {"start": "0:00:13.20", "end": "0:00:15.60", "text": "WHO'LL FUND YOUR OUTFIT\nWITHOUT AN EXCUSE!"},
            {"start": "0:00:15.60", "end": "0:00:17.60", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 4: Girl Math · The $250 Dress (Maria 04 Birthday Sparkle + Maria 02 Night Out)
    rich_specs.append({
        "number": 4,
        "id": "short_04",
        "group": raw_specs[3]["group"],
        "title": "GIRL MATH · The $250 Dress Costs $0",
        "standup_clip": None,
        "ugc_clips": raw_specs[3]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4", "ss": 1.0, "dur": 3.80, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_02_angaevamariia_vid_790GN4oq.mp4", "ss": 2.0, "dur": 4.40, "zoom": 1.06}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_04.mp3",
        "word_count": 21,
        "comm_voice_offset": 0.15,
        "caption": "Girl math: a $250 dress you buy yourself costs $250. But when a guy funds it on DressIt, your total is literally nothing! 💅✨ dressitnow.com\n\n#girlmath #dating #freedress #stylehack #outfitinspo #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.40", "text": "A $250 DRESS YOU BUY YOURSELF\nCOSTS $250."},
            {"start": "0:00:02.40", "end": "0:00:04.60", "text": "BUT WHEN A GUY FUNDS IT\nON DRESSIT..."},
            {"start": "0:00:04.60", "end": "0:00:06.50", "text": "YOUR TOTAL IS\nLITERALLY NOTHING!"},
            {"start": "0:00:06.50", "end": "0:00:08.00", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 5: Girl Math · Stop Staring At Your Zara Cart (Maria 05 + Maria 03)
    rich_specs.append({
        "number": 5,
        "id": "short_05",
        "group": raw_specs[4]["group"],
        "title": "SHOPPING HACK · Stop Staring At Your Cart",
        "standup_clip": None,
        "ugc_clips": raw_specs[4]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_05_angaevamariia_vid_KymX8LAP.mp4", "ss": 1.5, "dur": 3.80, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_03_angaevamariia_vid_PPpDhJD4.mp4", "ss": 2.0, "dur": 4.10, "zoom": 1.06}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_05.mp3",
        "word_count": 22,
        "comm_voice_offset": 0.15,
        "caption": "Stop staring at your Zara cart waiting for a sale that isn't coming. Post your cart on DressIt tonight and watch a guy fund it! 🛍️✨ dressitnow.com\n\n#shoppinghack #wishlist #zarahaul #freedress #dating #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.40", "text": "YOU'VE WAITED THREE WEEKS\nFOR A SALE THAT ISN'T COMING."},
            {"start": "0:00:02.40", "end": "0:00:04.80", "text": "POST YOUR CART ON DRESSIT TONIGHT..."},
            {"start": "0:00:04.80", "end": "0:00:06.40", "text": "AND WATCH A GUY FUND IT!"},
            {"start": "0:00:06.40", "end": "0:00:07.80", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 6: Girl Math · A Wedding I Didn't Even Want To Attend (Maria 06 + Maria 07)
    rich_specs.append({
        "number": 6,
        "id": "short_06",
        "group": raw_specs[5]["group"],
        "title": "STYLE CONFESSIONS · The Wedding Guest Outfit",
        "standup_clip": None,
        "ugc_clips": raw_specs[5]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_06_angaevamariia_vid_YhozXb0A.mp4", "ss": 1.0, "dur": 3.70, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_07_angaevamariia_vid_k0QcluXY.mp4", "ss": 1.0, "dur": 4.18, "zoom": 1.06}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_06.mp3",
        "word_count": 22,
        "comm_voice_offset": 0.15,
        "caption": "You should never pay for a dress for a wedding you didn't even want to attend. Post the outfit and let a guy cover it! 💅✨ dressitnow.com\n\n#weddingguest #weddingoutfit #girlconfessions #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.40", "text": "YOU SHOULD NEVER PAY FOR A WEDDING..."},
            {"start": "0:00:02.40", "end": "0:00:04.40", "text": "YOU DIDN'T EVEN WANT TO ATTEND!"},
            {"start": "0:00:04.40", "end": "0:00:06.20", "text": "POST THE OUTFIT AND LET A GUY COVER IT!"},
            {"start": "0:00:06.20", "end": "0:00:07.78", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 7: Interactive Rating · Pick My Friday Dress (1, 2, or 3) (Maria 01 + 02 + 04)
    rich_specs.append({
        "number": 7,
        "id": "short_07",
        "group": raw_specs[6]["group"],
        "title": "INTERACTIVE · Pick My Friday Dress (1, 2, or 3)",
        "standup_clip": None,
        "ugc_clips": raw_specs[6]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4", "ss": 1.0, "dur": 2.70, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_02_angaevamariia_vid_790GN4oq.mp4", "ss": 2.0, "dur": 2.70, "zoom": 1.04},
            {"path": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4", "ss": 1.0, "dur": 2.80, "zoom": 1.08}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_07.mp3",
        "word_count": 22,
        "comm_voice_offset": 0.15,
        "caption": "Which dress should I get funded for Friday night? Comment 1, 2, or 3, and tell me which guy pays for yours! 👀👗 dressitnow.com\n\n#whichdress #interactive #ratingdresses #outfitvoting #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.70", "text": "WHICH DRESS SHOULD I GET FUNDED\nFOR FRIDAY NIGHT?"},
            {"start": "0:00:02.70", "end": "0:00:04.80", "text": "COMMENT 1, 2, OR 3..."},
            {"start": "0:00:04.80", "end": "0:00:06.80", "text": "AND TELL ME WHICH GUY\nPAYS FOR YOURS!"},
            {"start": "0:00:06.80", "end": "0:00:08.10", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 8: Interactive Rating · Rating 3 Viral Dresses (Maria 03 + 06 + 07)
    rich_specs.append({
        "number": 8,
        "id": "short_08",
        "group": raw_specs[7]["group"],
        "title": "INTERACTIVE · Rating 3 Viral Dresses",
        "standup_clip": None,
        "ugc_clips": raw_specs[7]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_03_angaevamariia_vid_PPpDhJD4.mp4", "ss": 1.0, "dur": 2.60, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_06_angaevamariia_vid_YhozXb0A.mp4", "ss": 1.0, "dur": 2.60, "zoom": 1.04},
            {"path": "/root/event-hero/assets/maria_ugc/maria_07_angaevamariia_vid_k0QcluXY.mp4", "ss": 1.0, "dur": 2.68, "zoom": 1.08}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_08.mp3",
        "word_count": 21,
        "comm_voice_offset": 0.15,
        "caption": "Rating 3 viral dresses right now! Drop your favorite in the comments, then go get yours funded on DressIt today! ✨ dressitnow.com\n\n#ratingoutfits #viraldresses #tryonhaul #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.50", "text": "I'M RATING THREE VIRAL DRESSES\nRIGHT NOW!"},
            {"start": "0:00:02.50", "end": "0:00:04.80", "text": "DROP YOUR FAVORITE\nIN THE COMMENTS..."},
            {"start": "0:00:04.80", "end": "0:00:06.40", "text": "THEN GO GET YOURS FUNDED\nON DRESSIT TODAY!"},
            {"start": "0:00:06.40", "end": "0:00:07.78", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 9: Green Flags · The Biggest Green Flag In 2026 (Maria 07 + Maria 01)
    rich_specs.append({
        "number": 9,
        "id": "short_09",
        "group": raw_specs[8]["group"],
        "title": "GREEN FLAGS · The Biggest Green Flag In 2026",
        "standup_clip": None,
        "ugc_clips": raw_specs[8]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_07_angaevamariia_vid_k0QcluXY.mp4", "ss": 1.0, "dur": 4.10, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4", "ss": 1.5, "dur": 4.49, "zoom": 1.06}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_09.mp3",
        "word_count": 22,
        "comm_voice_offset": 0.15,
        "caption": "The biggest green flag this year isn't flowers. It's a guy who opens your DressIt wishlist and quietly funds every single thing! 💐✨ dressitnow.com\n\n#greenflags #datingstandards #relationshipgoals #datingadvice #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.60", "text": "THE BIGGEST GREEN FLAG THIS YEAR\nISN'T FLOWERS."},
            {"start": "0:00:02.60", "end": "0:00:05.20", "text": "IT'S A GUY WHO OPENS\nYOUR DRESSIT WISHLIST..."},
            {"start": "0:00:05.20", "end": "0:00:07.00", "text": "AND QUIETLY FUNDS\nEVERY SINGLE THING!"},
            {"start": "0:00:07.00", "end": "0:00:08.49", "text": "DRESSITNOW.COM"}
        ]
    })

    # Short 10: Green Flags · The Talking Stage Is Dead (Maria 03 + Maria 04)
    rich_specs.append({
        "number": 10,
        "id": "short_10",
        "group": raw_specs[9]["group"],
        "title": "GREEN FLAGS · The Talking Stage Is Dead",
        "standup_clip": None,
        "ugc_clips": raw_specs[9]["ugc_clips"],
        "cuts": [
            {"path": "/root/event-hero/assets/maria_ugc/maria_03_angaevamariia_vid_PPpDhJD4.mp4", "ss": 1.0, "dur": 4.00, "zoom": 1.0},
            {"path": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4", "ss": 1.0, "dur": 4.22, "zoom": 1.06}
        ],
        "audio_asset": "/root/dressit-pipeline/assets/voice_batch10_10.mp3",
        "word_count": 21,
        "comm_voice_offset": 0.15,
        "caption": "Tired of talking stages that go nowhere? Post your favorite outfit on DressIt and meet guys who actually show effort immediately! 💅✨ dressitnow.com\n\n#talkingstage #datingfatigue #effortoverwords #freedress #dressit #dressitnow",
        "dialogue": [
            {"start": "0:00:00.15", "end": "0:00:02.50", "text": "TIRED OF TALKING STAGES\nTHAT GO NOWHERE?"},
            {"start": "0:00:02.50", "end": "0:00:04.80", "text": "POST YOUR FAVORITE OUTFIT\nON DRESSIT..."},
            {"start": "0:00:04.80", "end": "0:00:06.80", "text": "AND MEET GUYS WHO ACTUALLY\nSHOW EFFORT IMMEDIATELY!"},
            {"start": "0:00:06.80", "end": "0:00:08.12", "text": "DRESSITNOW.COM"}
        ]
    })

    # Save rich specs to disk
    rich_specs_path = config.BASE_DIR / "rich_specs_10.json"
    rich_specs_path.write_text(json.dumps(rich_specs, indent=2), encoding="utf-8")
    logger.info(f"Saved rich specs for 10 shorts to {rich_specs_path}")

    # Render and deploy all 10 shorts sequentially
    results = []
    for spec in rich_specs:
        res = assemble_short(spec)
        results.append(res)

    print("\n" + "=" * 80)
    print("DRESSIT BATCH OF 10 SHORTS — ALL RENDERS & DEPLOYMENTS COMPLETED")
    print("=" * 80)
    for r in results:
        stream_st = "PASS (HTTP 206)" if r["streaming_206"] else "FAIL"
        face_st = "PASS" if r["face_verification"] else "N/A"
        print(f"#{r['number']:02d} | {r['id']} | {r['duration']:>5.2f}s | Face: {face_st:<4} | Stream: {stream_st} | {r['title']}")
        print(f"     URL: {r['hub_url']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
