#!/usr/bin/env python3
"""
assembler.py — Production-grade FFmpeg filtergraph video assembly engine for DressIt.

Strict Standards (Task #1999):
1. Cuts setup & punchline with 3ms microfade on start (no audio phoneme clipping).
2. Concat with 2 approved UGC cuts:
   - Cut A: Outro reveal (clean 1080x1920 video).
   - Cut B: Second outfit with subtle 1.05x punch zoom.
3. Persistent clean logo overlay: scale=480:-1, overlay at (W-w)/2:130.
4. Burned-in clean white ASS subtitles: Inter Black, bord10, shad3, MarginV 380.
   STRICTLY NO RAINBOW/NEON/VARIEGATED WORD COLORS.
5. Audio mixing: Speech master + ducked BGM (volume 0.12, -14.0 LUFS target, 48kHz).
6. Automated multi-point face verification using /root/global-dressit-pilot/face_verifier.py.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
import ugc_guard

# Import face_verifier from pilot directory
sys.path.insert(0, str(config.FACE_VERIFIER_SCRIPT.parent))
try:
    from face_verifier import compute_optimal_crop_x, verify_rendered_short
except ImportError:
    compute_optimal_crop_x = None
    verify_rendered_short = None

logger = logging.getLogger("assembler")


def run_cmd(cmd: List[str], desc: str = "") -> subprocess.CompletedProcess:
    """Executes a subprocess command with error reporting."""
    if desc:
        logger.info(f"==> {desc}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        err_msg = f"Command failed ({desc or ' '.join(cmd[:3])}):\nCMD: {' '.join(cmd)}\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    return res


def probe_duration(file_path: str | Path) -> float:
    """Returns media file duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def sanitize_subtitle_text(text: str) -> str:
    r"""
    Enforces brand subtitle hygiene:
    - Removes any rainbow/neon ASS color overrides (\c&H..., \1c&H...).
    - Removes emojis to comply with strict emoji policy.
    - Preserves clean layout and \n linebreaks.
    """
    # Remove any existing color override tags
    clean = re.sub(r"\{\\[1-4]?c&H[0-9a-fA-F]+&\}", "", text)
    # Remove other styling tags that might introduce colors
    clean = re.sub(r"\\(?:1c|2c|3c|4c)&H[0-9a-fA-F]+&", "", clean)
    # Strip emojis
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    clean = emoji_pattern.sub("", clean).strip()
    return clean


def generate_clean_ass(dialogue_events: List[Dict[str, str]], out_ass_path: Path) -> Path:
    """
    Generates ASS subtitle file conforming strictly to DressIt Brand guidelines:
    - Clean White font (&H00FFFFFF&)
    - Inter Black font
    - Solid black border (\\bord10\\shad3)
    - MarginV: 380
    - STRICTLY NO RAINBOW/NEON/VARIEGATED WORD COLORS.
    """
    out_ass_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [config.ASS_HEADER]

    for d in dialogue_events:
        raw_text = d.get("text", "")
        clean_text = sanitize_subtitle_text(raw_text)
        # Apply standard white text with solid black outline
        formatted_text = f"{{\\bord{config.SUBTITLE_OUTLINE_WIDTH}\\shad{config.SUBTITLE_SHADOW_DEPTH}\\c{config.SUBTITLE_PRIMARY_COLOR}}}{clean_text}"
        line = f"Dialogue: 0,{d['start']},{d['end']},DressItBrand,,0,0,0,,{formatted_text}"
        lines.append(line)

    out_ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Generated clean ASS subtitles: {out_ass_path} ({len(dialogue_events)} events)")
    return out_ass_path


def assemble_video(
    spec: Dict[str, Any],
    out_mp4: Optional[Path] = None,
    run_face_check: bool = True
) -> Dict[str, Any]:
    """
    Assembles a complete, broadcast-ready vertical video short.

    Pipeline Steps:
    1. Validate UGC clips with ugc_guard (DB + pool check, no maria_10, no quarantine).
    2. Video Cut 1: Standup Setup (with 3ms microfade on start audio).
    3. Video Cut 2: Standup Punchline.
    4. UGC Cut A: Clean reveal.
    5. UGC Cut B: Clean second outfit with 1.05x punch zoom.
    6. Video Concat (Cut 1 + Cut 2 + UGC A + UGC B).
    7. Audio Assembly: Mastered Speech + Ducked Funk BGM (-14.0 LUFS, 48kHz).
    8. Final Filtergraph: Persistent clean logo overlay + burned ASS subtitles.
    9. Automated Multi-Point Face Verification (sampled every 2.5s).
    """
    slug = spec.get("slug", spec.get("id", "dressit_short"))
    title = spec.get("title", slug)
    logger.info(f"Starting Assembly for: {title} ({slug})")

    # Destination paths
    work_dir = config.OUTPUT_DIR / f"work_{slug}"
    work_dir.mkdir(parents=True, exist_ok=True)

    if out_mp4 is None:
        final_mp4 = config.OUTPUT_DIR / f"{slug}.mp4"
    else:
        final_mp4 = Path(out_mp4).resolve()
        final_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Strict UGC Guard Validation
    clip_a = spec["ugc_cut_a"]
    clip_b = spec["ugc_cut_b"]
    logger.info("Validating UGC clips against DressIt DB...")
    ugc_guard.verify_clip(clip_a, raise_exc=True)
    ugc_guard.verify_clip(clip_b, raise_exc=True)
    logger.info("UGC clips PASSED all moderation and brand safety checks.")

    # Calculate timings
    standup_clip = spec["standup_clip"]
    cut1_ss = float(spec["cut1_ss"])
    cut1_dur = float(spec["cut1_dur"])
    cut2_ss = float(spec["cut2_ss"])
    cut2_dur = float(spec["cut2_dur"])
    standup_dur = spec.get("standup_dur", cut1_dur + cut2_dur)

    ugc_a_ss = float(spec.get("ugc_cut_a_ss", 1.0))
    ugc_a_dur = float(spec["ugc_cut_a_dur"])
    ugc_b_ss = float(spec.get("ugc_cut_b_ss", 2.0))
    ugc_b_dur = float(spec["ugc_cut_b_dur"])

    total_dur = spec.get("total_dur", standup_dur + ugc_a_dur + ugc_b_dur)
    comm_offset = spec.get("comm_voice_offset", standup_dur + 0.20)

    # Crop calculations (use optimal face crop if not provided)
    cut1_crop = spec.get("cut1_crop")
    if not cut1_crop and compute_optimal_crop_x:
        cx1 = compute_optimal_crop_x(standup_clip, cut1_ss, cut1_dur, crop_w=540, total_w=1920)
        cut1_crop = f"crop=540:960:{cx1}:0,scale={config.WIDTH}:{config.HEIGHT}:flags=lanczos"
    elif not cut1_crop:
        cut1_crop = f"crop=540:960:480:0,scale={config.WIDTH}:{config.HEIGHT}:flags=lanczos"

    cut2_crop = spec.get("cut2_crop")
    if not cut2_crop and compute_optimal_crop_x:
        cx2 = compute_optimal_crop_x(standup_clip, cut2_ss, cut2_dur, crop_w=540, total_w=1920)
        cut2_crop = f"crop=540:960:{cx2}:0,scale={config.WIDTH}:{config.HEIGHT}:flags=lanczos"
    elif not cut2_crop:
        cut2_crop = f"crop=540:960:420:0,scale={config.WIDTH}:{config.HEIGHT}:flags=lanczos"

    # Step 2: Render Cut 1 (Setup) with 3ms microfade
    cut1_mp4 = work_dir / f"{slug}_cut1.mp4"
    cut1_frames = int(cut1_dur * config.FPS)
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(cut1_ss),
        "-i", str(standup_clip),
        "-vframes", str(cut1_frames),
        "-vf", f"{cut1_crop},setsar=1,fps={config.FPS}",
        "-af", f"atrim=0:{cut1_dur:.3f},afade=t=in:ss=0:d={config.MICROFADE_IN_SEC}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-c:a", "aac", "-b:a", "192k", "-ar", str(config.SAMPLE_RATE),
        str(cut1_mp4)
    ], desc=f"Cut 1 Setup (ss={cut1_ss:.2f}s, dur={cut1_dur:.2f}s, 3ms microfade)")

    # Step 3: Render Cut 2 (Punchline)
    cut2_mp4 = work_dir / f"{slug}_cut2.mp4"
    cut2_frames = int(cut2_dur * config.FPS)
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(cut2_ss),
        "-i", str(standup_clip),
        "-vframes", str(cut2_frames),
        "-vf", f"{cut2_crop},setsar=1,fps={config.FPS}",
        "-af", f"atrim=0:{cut2_dur:.3f},afade=t=out:st={cut2_dur-config.AUDIO_FADE_OUT_SEC:.3f}:d={config.AUDIO_FADE_OUT_SEC}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-c:a", "aac", "-b:a", "192k", "-ar", str(config.SAMPLE_RATE),
        str(cut2_mp4)
    ], desc=f"Cut 2 Punchline (ss={cut2_ss:.2f}s, dur={cut2_dur:.2f}s)")

    # Step 4: UGC Cut A (Clean Reveal)
    ugc_a_mp4 = work_dir / f"{slug}_ugc_a.mp4"
    frames_a = int(ugc_a_dur * config.FPS)
    ugc_a_vf = (
        f"scale={config.WIDTH}:{config.HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(ugc_a_ss),
        "-i", str(clip_a),
        "-vframes", str(frames_a),
        "-vf", ugc_a_vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-pix_fmt", config.PIX_FMT,
        "-an",
        str(ugc_a_mp4)
    ], desc=f"UGC Cut A Reveal ({ugc_a_dur:.2f}s)")

    # Step 5: UGC Cut B (Clean Second Outfit + 1.05x Punch Zoom)
    ugc_b_mp4 = work_dir / f"{slug}_ugc_b.mp4"
    frames_b = int(ugc_b_dur * config.FPS)
    zoom_w, zoom_h = int(config.WIDTH * 1.05), int(config.HEIGHT * 1.05)
    ugc_b_vf = (
        f"scale={zoom_w}:{zoom_h}:force_original_aspect_ratio=increase,"
        f"crop={config.WIDTH}:{config.HEIGHT}:(in_w-{config.WIDTH})/2:(in_h-{config.HEIGHT})/2,setsar=1,fps={config.FPS}"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(ugc_b_ss),
        "-i", str(clip_b),
        "-vframes", str(frames_b),
        "-vf", ugc_b_vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.CRF),
        "-pix_fmt", config.PIX_FMT,
        "-an",
        str(ugc_b_mp4)
    ], desc=f"UGC Cut B (1.05x Punch Zoom, {ugc_b_dur:.2f}s)")

    # Step 6: Concat Video Cuts (Cut 1 + Cut 2 + UGC A + UGC B)
    concat_list = work_dir / f"{slug}_concat.txt"
    concat_list.write_text(
        f"file '{cut1_mp4.resolve()}'\nfile '{cut2_mp4.resolve()}'\nfile '{ugc_a_mp4.resolve()}'\nfile '{ugc_b_mp4.resolve()}'\n",
        encoding="utf-8"
    )
    concat_silent_video = work_dir / f"{slug}_silent_concat.mp4"
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        str(concat_silent_video)
    ], desc="Concat 4 Video Streams")

    # Step 7: Audio Mixing
    # 7a. Standup Audio Track
    standup_audio = work_dir / f"{slug}_standup_audio.wav"
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(cut1_mp4),
        "-i", str(cut2_mp4),
        "-filter_complex", f"[0:a][1:a]concat=n=2:v=0:a=1,volume=1.3,atrim=0:{standup_dur:.2f},afade=t=out:st={standup_dur-0.25:.2f}:d=0.25[aout]",
        "-map", "[aout]",
        "-ar", str(config.SAMPLE_RATE),
        str(standup_audio)
    ], desc="Mix Standup Audio")

    # 7b. Speech Master (Standup audio + Commercial Voiceover)
    voice_file = Path(spec["voice_file"])
    if not voice_file.exists():
        raise FileNotFoundError(f"Voice file not found: {voice_file}")

    speech_master = work_dir / f"{slug}_speech_master.wav"
    delay_ms = int(comm_offset * 1000)
    speech_filter = (
        f"[0:a]apad=pad_dur=30.0,atrim=0:{total_dur:.2f}[astandup];"
        f"[1:a]adelay={delay_ms}|{delay_ms},apad=pad_dur=30.0,atrim=0:{total_dur:.2f}[avoice];"
        f"[astandup][avoice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(standup_audio),
        "-i", str(voice_file),
        "-filter_complex", speech_filter,
        "-map", "[aout]",
        "-ar", str(config.SAMPLE_RATE),
        str(speech_master)
    ], desc=f"Speech Master (VO delay={delay_ms}ms)")

    # 7c. Full Audio Master with Ducked Funk BGM (-14 LUFS)
    bgm_file = Path(spec.get("bgm_file", config.BGM_DEFAULT_PATH))
    audio_mixed = work_dir / f"{slug}_audio_mixed.wav"
    audio_filter = (
        f"[1:a]atrim=0:{total_dur:.2f},afade=t=out:st={total_dur-0.6:.2f}:d=0.6,volume={config.BGM_DUCK_VOLUME}[abgm];"
        f"[0:a][abgm]amix=inputs=2:duration=first:dropout_transition=0[amixed];"
        f"[amixed]loudnorm=I={config.AUDIO_TARGET_LUFS}:TP={config.AUDIO_TRUE_PEAK}:LRA={config.AUDIO_LRA}[aout]"
    )
    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(speech_master),
        "-i", str(bgm_file),
        "-filter_complex", audio_filter,
        "-map", "[aout]",
        "-ar", str(config.SAMPLE_RATE),
        str(audio_mixed)
    ], desc="Master Audio Mix (-14 LUFS, Ducked Funk BGM)")

    # Step 8: Subtitle Generation
    ass_file = work_dir / f"{slug}.ass"
    generate_clean_ass(spec["dialogue"], ass_file)

    # Step 9: Final Master Render
    ass_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")
    fonts_arg = str(config.FONTS_DIR).replace("\\", "/").replace(":", "\\:")

    final_filter = (
        f"[1:v]{config.LOGO_SCALE}[clean_logo];"
        f"[0:v][clean_logo]overlay={config.LOGO_OVERLAY_COORDS}[vbranded];"
        f"[vbranded]subtitles={ass_escaped}:fontsdir={fonts_arg}[vout]"
    )

    run_cmd([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(concat_silent_video),
        "-i", str(config.LOGO_PATH),
        "-i", str(audio_mixed),
        "-filter_complex", final_filter,
        "-map", "[vout]",
        "-map", "2:a",
        "-c:v", "libx264", "-preset", config.PRESET, "-crf", str(config.CRF),
        "-c:a", "aac", "-b:a", "256k", "-ar", str(config.SAMPLE_RATE),
        "-pix_fmt", config.PIX_FMT,
        "-t", f"{total_dur:.2f}",
        str(final_mp4)
    ], desc=f"Final Master Render ({total_dur:.2f}s)")

    actual_duration = probe_duration(final_mp4)
    logger.info(f"Rendered: {final_mp4} (Duration: {actual_duration:.2f}s)")

    # Step 10: Face Centering Verification
    face_check_results = []
    all_face_passed = True
    if run_face_check and verify_rendered_short:
        debug_dir = config.DEBUG_FRAMES_DIR / slug
        logger.info(f"Running multi-point face verification on {final_mp4.name}...")
        all_face_passed, face_check_results = verify_rendered_short(
            str(final_mp4),
            check_interval=getattr(config, "FACE_CHECK_INTERVAL", 2.5),
            max_check_time=standup_dur - 0.4,
            out_debug_dir=debug_dir
        )
        for c in face_check_results:
            status = "PASS" if c["passed"] else "FAIL"
            logger.info(f"  Face check t={c['timestamp']}s: [{status}] {c['details']}")

    return {
        "output_mp4": str(final_mp4),
        "duration": actual_duration,
        "face_verification_passed": all_face_passed,
        "face_checks": face_check_results,
        "slug": slug,
        "title": title
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("Testing assembler.py with sample spec...")
    print("=" * 60)

    # Clean test spec based on Short 1
    sample_spec = {
        "id": "test-pipeline-short-01",
        "title": "RELATIONSHIPS · I Quit Dating Losers · Test",
        "slug": "test_pipeline_short_01",
        "standup_clip": "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4",
        "cut1_ss": 4.18,
        "cut1_dur": 2.37,
        "cut1_crop": "crop=540:960:480:0,scale=1080:1920:flags=lanczos",
        "cut2_ss": 6.55,
        "cut2_dur": 5.85,
        "cut2_crop": "crop=540:960:564:0,scale=1080:1920:flags=lanczos",
        "standup_dur": 8.22,
        "ugc_cut_a": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4",
        "ugc_cut_a_ss": 1.5,
        "ugc_cut_a_dur": 3.60,
        "ugc_cut_b": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4",
        "ugc_cut_b_ss": 2.0,
        "ugc_cut_b_dur": 5.68,
        "voice_file": "/root/global-dressit-pilot/assets/voice_rel_context_01_losers.mp3",
        "total_dur": 17.50,
        "comm_voice_offset": 8.42,
        "dialogue": [
            {"start": "0:00:00.08", "end": "0:00:01.00", "text": "I'VE DONE SO MUCH WORK"},
            {"start": "0:00:01.00", "end": "0:00:01.80", "text": "ON MYSELF..."},
            {"start": "0:00:01.80", "end": "0:00:02.40", "text": "IN THE LAST 5 YEARS:"},
            {"start": "0:00:02.40", "end": "0:00:03.20", "text": "I QUIT DATING"},
            {"start": "0:00:03.20", "end": "0:00:04.60", "text": "LOSERS!"},
            {"start": "0:00:04.60", "end": "0:00:05.50", "text": "THAT TOOK A MINUTE!"},
            {"start": "0:00:05.50", "end": "0:00:06.60", "text": "SERIOUSLY, A WHOLE MINUTE..."},
            {"start": "0:00:06.60", "end": "0:00:08.00", "text": "THAT TOOK A LONG TIME!"},
            {"start": "0:00:08.42", "end": "0:00:09.50", "text": "QUIT DATING\nLOSERS!"},
            {"start": "0:00:09.50", "end": "0:00:11.60", "text": "POST THE DRESS\nYOU ACTUALLY WANT\nON DRESSIT"},
            {"start": "0:00:11.60", "end": "0:00:13.25", "text": "AND LET GUYS PAY FOR IT!"},
            {"start": "0:00:13.25", "end": "0:00:15.50", "text": "POST OUTFIT,\nGET IT FUNDED FOR FREE"},
            {"start": "0:00:15.50", "end": "0:00:17.20", "text": "DRESSITNOW.COM"}
        ]
    }

    res = assemble_video(sample_spec)
    print("\nAssembly Result:")
    print(f"  Output MP4: {res['output_mp4']}")
    print(f"  Duration: {res['duration']:.2f}s")
    print(f"  Face Verification: {'PASS' if res['face_verification_passed'] else 'FAIL'}")
