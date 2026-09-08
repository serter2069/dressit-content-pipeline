#!/usr/bin/env python3
"""
assemble_10_diverse_shorts.py

Production assembly & automated QC engine for 10 Diverse Standup Comedy Concert Shorts.
Features:
1. 10 DIFFERENT live standup comedians on stage.
2. 3ms microfade on Cut 1 start (never clipping the hook word).
3. Clean White Inter Black ASS subtitles (\bord10\shad3\c&H00FFFFFF&).
4. Persistent clean DressIt logo (scale=480:-1, (W-w)/2:130) with zero chips/pill badges.
5. Strict 2-second automated Face QC Judge (face_qc_judge.py) across entire video duration.
6. OpenRouter openai/gpt-audio nova voiceover (-14.0 LUFS) + ducked funk BGM (vol 0.12).
7. Deployment to Posting Hub (/root/posting/data/media/) with HTTP 206 streaming test.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, "/root/dressit-pipeline")
sys.path.insert(0, "/root/global-dressit-pilot")
sys.path.insert(0, "/root/posting")

import config
import ugc_guard
import db as posting_db
from face_qc_judge import judge_rendered_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("assemble_10_diverse")

SPECS_FILE = Path("/root/dressit-pipeline/specs_10_diverse_shorts.json")
POSTING_MEDIA_DIR = Path("/root/posting/data/media")
POSTING_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR = Path("/root/dressit-pipeline/output/face_qc_debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


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
    item_id = spec["id"]
    slug = spec["slug"]
    title = spec["title"]
    comedian = spec.get("comedian", "Standup Comedian")
    logger.info(f"\n{'='*70}\nASSEMBLING DIVERSE SHORT {short_num}/10: {comedian} — {title} ({item_id})\n{'='*70}")

    hub_mp4 = POSTING_MEDIA_DIR / f"{item_id}.mp4"
    final_mp4 = config.OUTPUT_DIR / f"{slug}.mp4"
    sc = spec["standup_clip"]
    standup_dur = sc["cut1_dur"] + sc["cut2_dur"]

    if hub_mp4.exists() and final_mp4.exists() and test_http_206(item_id):
        qc = judge_rendered_video(str(final_mp4), check_interval=2.0, standup_end_time=standup_dur, allow_spin_in_ugc=True)
        if qc["passed"]:
            logger.info(f"Short {short_num} ({item_id}) already rendered and passed Face QC ({qc['score_pct']}%). Skipping re-render.")
            return {
                "short_num": short_num,
                "comedian": comedian,
                "item_id": item_id,
                "title": title,
                "duration": qc["duration_sec"],
                "final_path": str(final_mp4),
                "hub_path": str(hub_mp4),
                "hub_url": f"https://posting.smartlaunchhub.com/media/{item_id}",
                "stream_206": True,
                "face_qc": qc
            }

    work_dir = config.OUTPUT_DIR / f"work_{slug}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    voice_path = Path(spec["audio_asset"])
    if not voice_path.exists():
        raise FileNotFoundError(f"Voice file missing: {voice_path}")

    # Validate UGC clips against DressIt DB
    ugc_guard.verify_clip(spec["ugc_cut_a"], raise_exc=True)
    ugc_guard.verify_clip(spec["ugc_cut_b"], raise_exc=True)

    sc = spec["standup_clip"]
    standup_src = sc["source_file"]
    cut1_ss = sc["cut1_ss"]
    cut1_dur = sc["cut1_dur"]
    cut1_crop = sc.get("cut1_crop", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2")
    cut2_ss = sc["cut2_ss"]
    cut2_dur = sc["cut2_dur"]
    cut2_crop = sc.get("cut2_crop", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2")
    standup_dur = cut1_dur + cut2_dur

    video_segments = []

    # 1. Cut 1 Setup with 3ms microfade (NEVER clip hook phonemes)
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
    ], desc=f"Cut 1 Setup ({comedian}, ss={cut1_ss}s, dur={cut1_dur}s, 3ms microfade)")
    video_segments.append(cut1_mp4)

    # 2. Cut 2 Punchline
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
    ], desc=f"Cut 2 Punchline ({comedian}, ss={cut2_ss}s, dur={cut2_dur}s)")
    video_segments.append(cut2_mp4)

    # 3. UGC Cut A (Reveal)
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
    ], desc=f"UGC Cut A Reveal ({ugc_a_dur}s)")
    video_segments.append(ugc_a_mp4)

    # 4. UGC Cut B (Punch Zoom 1.05x)
    audio_dur = spec.get("audio_duration_sec", 8.0)
    ugc_b_dur = spec.get("ugc_cut_b_dur", max(5.4, audio_dur - ugc_a_dur + 0.8))
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
    ], desc=f"UGC Cut B Punch Zoom ({ugc_b_dur:.2f}s)")
    video_segments.append(ugc_b_mp4)

    total_dur = standup_dur + ugc_a_dur + ugc_b_dur
    vo_delay_ms = int((standup_dur + 0.20) * 1000)

    # Standup Audio Concat
    standup_wav = work_dir / f"{slug}_standup_audio.wav"
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(cut1_mp4), "-i", str(cut2_mp4),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[aout]",
        "-map", "[aout]", "-ar", str(config.SAMPLE_RATE), str(standup_wav)
    ], desc="Mix Standup Audio")

    # Speech Master: Standup Audio + Delayed Nova VO
    speech_master = work_dir / f"{slug}_speech_master.wav"
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(standup_wav), "-i", str(voice_path),
        "-filter_complex", f"[1:a]adelay={vo_delay_ms}|{vo_delay_ms}[delayed_vo];[0:a][delayed_vo]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
        "-map", "[aout]", "-ar", str(config.SAMPLE_RATE), str(speech_master)
    ], desc=f"Speech Master (VO delay={vo_delay_ms}ms)")

    # Video Stream Concat (no audio)
    concat_list = work_dir / f"{slug}_concat.txt"
    with open(concat_list, "w") as f:
        for seg in video_segments:
            f.write(f"file '{seg.resolve()}'\n")

    raw_video = work_dir / f"{slug}_raw_video.mp4"
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "copy", "-an", str(raw_video)
    ], desc="Concat Video Streams")

    # Master Audio Mix: Speech Master + Ducked Funk BGM (-14.0 LUFS)
    master_audio = work_dir / f"{slug}_master_audio.wav"
    bgm_path = config.BGM_DEFAULT_PATH
    bgm_filter = (
        f"[1:a]atrim=0:{total_dur:.2f},afade=t=out:st={total_dur-0.6:.2f}:d=0.6,volume={config.BGM_DUCK_VOLUME}[abgm];"
        f"[0:a][abgm]amix=inputs=2:duration=first:dropout_transition=0[amixed];"
        f"[amixed]loudnorm=I={config.AUDIO_TARGET_LUFS}:TP={config.AUDIO_TRUE_PEAK}:LRA={config.AUDIO_LRA}[aout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(speech_master), "-i", str(bgm_path),
        "-filter_complex", bgm_filter,
        "-map", "[aout]", "-ar", str(config.SAMPLE_RATE), str(master_audio)
    ], desc="Master Audio Mix (-14.0 LUFS + Ducked BGM)")

    # Subtitles
    ass_file = work_dir / f"{slug}.ass"
    generate_clean_ass(spec["dialogue"], ass_file)

    # Final Filtergraph: Video + Scaled Persistent Logo + Burned ASS + Master Audio
    final_mp4 = config.OUTPUT_DIR / f"{slug}.mp4"
    ass_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")
    fonts_arg = str(config.FONTS_DIR).replace("\\", "/").replace(":", "\\:")

    final_filter = (
        f"[1:v]{config.LOGO_SCALE}[clean_logo];"
        f"[0:v][clean_logo]overlay={config.LOGO_OVERLAY_COORDS}[vbranded];"
        f"[vbranded]subtitles={ass_escaped}:fontsdir={fonts_arg}[vout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(raw_video),
        "-i", str(config.LOGO_PATH),
        "-i", str(master_audio),
        "-filter_complex", final_filter,
        "-map", "[vout]", "-map", "2:a",
        "-t", f"{total_dur:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-pix_fmt", config.PIX_FMT,
        "-c:a", "aac", "-b:a", "256k", "-ar", str(config.SAMPLE_RATE),
        "-movflags", "+faststart",
        str(final_mp4)
    ], desc=f"Final Assembly -> {final_mp4.name} ({total_dur:.2f}s)")

    # 5. Strict 2-Second Face QC Judge Pass
    logger.info(f"Running strict 2-second face QC judge on {final_mp4.name}...")
    judge_res = judge_rendered_video(
        str(final_mp4),
        check_interval=2.0,
        standup_end_time=standup_dur,
        out_debug_dir=DEBUG_DIR / slug,
        allow_spin_in_ugc=True
    )
    logger.info(f"Face QC Result: Passed={judge_res['passed']}, Score={judge_res['score_pct']}%, Standup Fails={judge_res['standup_fails']}, UGC Misses={judge_res['ugc_misses']}")

    # Copy to Posting Hub
    hub_mp4 = POSTING_MEDIA_DIR / f"{item_id}.mp4"
    shutil.copyfile(final_mp4, hub_mp4)
    logger.info(f"Copied to Posting Hub: {hub_mp4}")

    # Upsert into posting.db
    caption = spec.get("caption", f"{title} ✨ #dressit #dating #fashion")
    conn = posting_db.get_db()
    try:
        posting_db.upsert_item(
            conn,
            item_id=item_id,
            channel_id=10,  # dressit-global
            title=title,
            local_path=str(hub_mp4.resolve()),
            state="review",
            source_ref=f"{sc['source_file'].split('/')[-1]}:{cut1_ss:.2f}-{cut2_ss+cut2_dur:.2f}",
            meta={
                "comedian": comedian,
                "caption": caption,
                "script": spec["voiceover_script"],
                "topic": spec.get("topic", "Relationships"),
                "duration_sec": total_dur,
                "face_qc_score": judge_res["score_pct"],
                "face_qc_passed": judge_res["passed"]
            }
        )
        conn.commit()
    finally:
        conn.close()

    # Test HTTP 206 Partial Content
    stream_ok = test_http_206(item_id)
    logger.info(f"Posting Hub HTTP 206 streaming for {item_id}: {'PASS' if stream_ok else 'FAIL'}")

    return {
        "short_num": short_num,
        "comedian": comedian,
        "item_id": item_id,
        "title": title,
        "duration": total_dur,
        "final_path": str(final_mp4),
        "hub_path": str(hub_mp4),
        "hub_url": f"https://posting.smartlaunchhub.com/media/{item_id}",
        "stream_206": stream_ok,
        "face_qc": judge_res
    }


def main():
    with open(SPECS_FILE, "r") as f:
        specs = json.load(f)

    logger.info(f"Loaded {len(specs)} diverse standup short specifications from {SPECS_FILE}")

    results = []
    for spec in specs:
        res = assemble_short(spec)
        results.append(res)

    summary_file = config.OUTPUT_DIR / "assemble_10_diverse_summary.json"
    with open(summary_file, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"\n{'='*80}\nBATCH ASSEMBLY COMPLETE FOR ALL 10 DIVERSE COMEDIANS\n{'='*80}")
    for r in results:
        qc = r["face_qc"]
        status = "PASSED" if (r["stream_206"] and qc["passed"]) else "FLAGGED"
        logger.info(f"[{status}] #{r['short_num']:02d} {r['comedian']:18s} | Dur: {r['duration']:.2f}s | FaceQC: {qc['score_pct']}% | URL: {r['hub_url']}")


if __name__ == "__main__":
    main()
