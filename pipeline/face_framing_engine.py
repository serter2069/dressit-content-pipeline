#!/usr/bin/env python3
"""
face_framing_engine.py

Bulletproof Face Tracking, Dynamic Auto-Framing & Hard-Gate Verification Engine
for TikTok/Reels 9:16 Vertical Video Production (DressIt Pipeline).

Guarantees:
1. Dynamic Face Detection & Tracking:
   - Multi-cascade detection (frontal default + frontal alt2 + profile left + profile right)
   - Resilient frame decoding for AV1 / VP9 / H264 / HEVC video streams.
   - For 16:9 widescreen (1920x1080): computes crop=w:h:x:y such that comedian's face is
     horizontally centered (|cx - 540| <= 80), in upper-middle third (cy in 350-650),
     and size >= 220px in 1080x1920 output.
   - For vertical video (608x1080, 480x854, etc.): computes scaling and centering parameters
     (scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:x:y) with zero black bars,
     adjusting zoom & offset to ensure comedian face is centered and prominent.
2. Automated HARD-GATE Validator:
   - verify_face_framing(video_path, start_t, end_t, step_sec=1.0):
     Checks 100% of samples:
     1) Face detected == True
     2) Face size >= 180x180 px (no wide shots)
     3) Face center X in [380, 700]
     4) Face center Y in [200, 850]
     Returns {"passed": bool, "samples": [...], "error": str or None}
3. Standalone Verification Tool:
   - python3 face_framing_engine.py verify <video_path> [--start <t>] [--end <t>]
   - Exports visual verification frames with bounding boxes to /root/dressit-pipeline/debug_frames/
"""

import argparse
import cv2
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("face_framing_engine")

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
DEFAULT_DEBUG_DIR = "/root/dressit-pipeline/debug_frames"

CASCADE_DEFAULT = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
CASCADE_ALT2 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
CASCADE_PROFILE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")


class FaceFramingValidationError(RuntimeError):
    """Raised when hard-gate face framing verification fails."""
    pass


def get_video_info(video_path: str) -> Dict[str, Any]:
    """Probes video metadata via ffprobe."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate,codec_name:format=duration",
        "-of", "json",
        video_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video_path}: {res.stderr}")

    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError(f"No video streams found in {video_path}")

    s = streams[0]
    width = int(s["width"])
    height = int(s["height"])
    codec_name = s.get("codec_name", "unknown")

    r_fps = s.get("r_frame_rate", "30/1")
    if "/" in r_fps:
        num, den = r_fps.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 30.0
    else:
        fps = float(r_fps) if r_fps else 30.0

    dur_str = s.get("duration") or data.get("format", {}).get("duration") or "0"
    duration = float(dur_str)

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "fps": fps,
        "codec_name": codec_name,
        "aspect_ratio": width / height if height > 0 else 1.0,
        "is_widescreen": (width / height > 1.0) if height > 0 else False,
        "is_vertical": (width / height <= 1.0) if height > 0 else False,
    }


def extract_frame(video_path: str, t_sec: float, info: Optional[Dict[str, Any]] = None) -> Optional[np.ndarray]:
    """
    Extracts a single BGR frame at timestamp t_sec.
    Tries OpenCV VideoCapture first for H264; falls back to ffmpeg pipe
    for AV1, VP9, or on cv2 seek/decode failures.
    """
    if info is None:
        info = get_video_info(video_path)

    w = info["width"]
    h = info["height"]
    codec = info.get("codec_name", "")

    if codec not in ["av1", "av01", "vp9"]:
        try:
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_sec * 1000.0))
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None and frame.shape == (h, w, 3):
                    if frame.mean() > 2.0:
                        return frame
        except Exception:
            pass

    # Bulletproof fallback: system ffmpeg extraction
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{t_sec:.3f}",
        "-i", video_path,
        "-vframes", "1",
        "-f", "image2pipe",
        "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24",
        "-"
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if len(p.stdout) == w * h * 3:
        frame = np.frombuffer(p.stdout, dtype=np.uint8).reshape((h, w, 3))
        return frame

    return None


def detect_all_faces(
    frame: np.ndarray,
    min_size: int = 35,
    max_size: int = 1500,
    max_y_ratio: float = 0.85
) -> List[Dict[str, Any]]:
    """
    Detects all candidate faces using 4 complementary cascade detectors:
    1. haarcascade_frontalface_alt2 (high accuracy)
    2. haarcascade_frontalface_default (classic frontal)
    3. haarcascade_profileface (profile left)
    4. haarcascade_profileface on flipped image (profile right)
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    max_y = int(h * max_y_ratio)

    candidates = []

    # 1. Frontal alt2
    f_alt2 = CASCADE_ALT2.detectMultiScale(
        gray, scaleFactor=1.08, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (x, y, fw, fh) in f_alt2:
        if y < max_y:
            candidates.append({"x": int(x), "y": int(y), "w": int(fw), "h": int(fh), "type": "alt2", "area": fw * fh})

    # 2. Frontal default
    f_def = CASCADE_DEFAULT.detectMultiScale(
        gray, scaleFactor=1.08, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (x, y, fw, fh) in f_def:
        if y < max_y:
            candidates.append({"x": int(x), "y": int(y), "w": int(fw), "h": int(fh), "type": "default", "area": fw * fh})

    # 3. Profile left
    p_faces = CASCADE_PROFILE.detectMultiScale(
        gray, scaleFactor=1.08, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (x, y, fw, fh) in p_faces:
        if y < max_y:
            candidates.append({"x": int(x), "y": int(y), "w": int(fw), "h": int(fh), "type": "profile_l", "area": fw * fh})

    # 4. Profile right (via horizontal flip)
    gray_flip = cv2.flip(gray, 1)
    pr_faces = CASCADE_PROFILE.detectMultiScale(
        gray_flip, scaleFactor=1.08, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (fx, fy, fw, fh) in pr_faces:
        if fy < max_y:
            orig_x = w - (fx + fw)
            candidates.append({"x": int(orig_x), "y": int(fy), "w": int(fw), "h": int(fh), "type": "profile_r", "area": fw * fh})

    # Deduplicate overlapping boxes
    candidates.sort(key=lambda c: c["area"], reverse=True)
    dedup: List[Dict[str, Any]] = []
    for cand in candidates:
        cx = cand["x"] + cand["w"] // 2
        cy = cand["y"] + cand["h"] // 2
        overlap = False
        for kept in dedup:
            kcx = kept["cx"]
            kcy = kept["cy"]
            thresh_x = max(cand["w"], kept["w"]) * 0.55
            thresh_y = max(cand["h"], kept["h"]) * 0.55
            if abs(cx - kcx) < thresh_x and abs(cy - kcy) < thresh_y:
                overlap = True
                break
        if not overlap:
            cand["cx"] = cx
            cand["cy"] = cy
            dedup.append(cand)

    return dedup


def get_primary_face(faces: List[Dict[str, Any]], frame_w: int, frame_h: int) -> Optional[Dict[str, Any]]:
    """
    Selects the primary comedian/speaker face from detected faces.
    Filters out torso false-positives, microphone/jewelry/hands artifacts, and audience members,
    preferring the prominent upper-middle focal face.

    Guarantees:
    - Never picks a tiny artifact (<180px) when a prominent speaker face (>300px) is present.
    - Allows large close-up faces (up to 1000px in 1080x1920 portrait video).
    - In 1080x1920 vertical video, speaker face center is expected in upper-middle [150, 850].
    """
    if not faces:
        return None

    # Determine acceptable Y zone for speaker face:
    # In 1080x1920 vertical video, speaker face center must be in [150, 850]
    # In source / landscape video, speaker face center is in [0.08 * H, 0.75 * H]
    max_allowed_cy = 850 if frame_h >= 1500 else int(frame_h * 0.75)
    min_allowed_cy = 150 if frame_h >= 1500 else int(frame_h * 0.08)

    in_zone = [f for f in faces if min_allowed_cy <= f["cy"] <= max_allowed_cy]
    candidates = in_zone if in_zone else faces

    # Discard full-frame boxes that enclose entire stage / body (>95% frame dimension)
    max_reasonable_dim = int(min(frame_w, frame_h) * 0.95)
    size_bounded = [f for f in candidates if max(f["w"], f["h"]) <= max_reasonable_dim]
    if size_bounded:
        candidates = size_bounded

    # False-Positive Filtering:
    # If candidate faces include a large face (>300px), NEVER pick a tiny face (<180px).
    max_cand_size = max([max(f["w"], f["h"]) for f in candidates], default=0)
    if max_cand_size > 300:
        large_cands = [f for f in candidates if max(f["w"], f["h"]) >= 180]
        if large_cands:
            candidates = large_cands
    elif frame_h >= 1500 and max_cand_size >= 180:
        # In rendered 1080x1920 portrait video, comedians are >= 180px; boxes < 160px are artifacts
        portrait_cands = [f for f in candidates if max(f["w"], f["h"]) >= 160]
        if portrait_cands:
            candidates = portrait_cands

    # Target framing: upper-middle focal area
    # In portrait 1080x1920: target cy ~ 500 (approx 0.26 * H), target cx = 540 (0.5 * W)
    target_cy = frame_h * 0.26
    target_cx = frame_w / 2.0

    def score(f: Dict[str, Any]) -> float:
        norm_dy = abs(f["cy"] - target_cy) / float(frame_h)
        norm_dx = abs(f["cx"] - target_cx) / float(frame_w)
        ref_size = 450.0 if frame_h >= 1500 else 200.0
        face_sz = float(max(f["w"], f["h"]))
        size_score = min(1.2, face_sz / ref_size)
        return (size_score * 2.0) - (1.5 * norm_dy + 1.0 * norm_dx)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def get_crop_x_at_t(framing: Dict[str, Any], t: float) -> int:
    """
    Evaluates the horizontal crop offset (x) at a given absolute timestamp t.
    Supports both static crops and dynamic linear / piecewise tracking.
    """
    if not framing:
        return 0
    crop_info = framing.get("crop", {})
    default_x = int(crop_info.get("x", 0))
    if not framing.get("is_dynamic", False):
        return default_x

    dp = framing.get("dynamic_params", {})
    t_start = dp.get("start_t", 0.0)
    dur = max(0.001, dp.get("duration", 1.0))
    max_crop_x = int(dp.get("max_crop_x", default_x))

    t_rel = max(0.0, min(dur, t - t_start))

    if dp.get("type") == "piecewise_3":
        t_mid = dp.get("t_mid", dur / 2.0)
        x0 = dp.get("x_start", default_x)
        x1 = dp.get("x_mid", default_x)
        x2 = dp.get("x_end", default_x)
        if t_rel < t_mid and t_mid > 0:
            x_val = x0 + (x1 - x0) * (t_rel / t_mid)
        else:
            denom = max(0.001, dur - t_mid)
            x_val = x1 + (x2 - x1) * ((t_rel - t_mid) / denom)
    else:
        # Standard linear tracking
        x0 = dp.get("x_start", default_x)
        x1 = dp.get("x_end", default_x)
        x_val = x0 + (x1 - x0) * (t_rel / dur)

    clamped_x = int(round(max(0, min(max_crop_x, x_val))))
    return clamped_x - (clamped_x % 2)


def compute_dynamic_framing_parameters(
    video_path: str,
    start_t: float,
    end_t: float,
    target_w: int = TARGET_WIDTH,
    target_h: int = TARGET_HEIGHT,
    sample_step: float = 0.5
) -> Dict[str, Any]:
    """
    Computes dynamic auto-framing parameters for video in range [start_t, end_t]:
    - Samples face positions at sample_step (default 0.5s) intervals.
    - If comedian movement span is small (max_cx - min_cx <= 120), uses a single centered crop with sufficient zoom.
    - If comedian movement span is large (> 120), calculates a smooth tracking x(t) expression
      or piecewise linear cuts for FFmpeg.
    - Ensures comedian face is prominent (size >= 200px in target_w x target_h), centered (|cx - 540| <= 100),
      and in upper-middle framing (cy in 250-700).
    """
    info = get_video_info(video_path)
    src_w = info["width"]
    src_h = info["height"]
    is_widescreen = info["is_widescreen"]

    duration = info["duration"]
    actual_start = max(0.0, start_t)
    actual_end = min(duration, end_t) if end_t > 0 else duration
    if actual_end <= actual_start:
        actual_end = actual_start + 1.0
    dur = max(0.1, actual_end - actual_start)

    sample_ts = []
    curr = actual_start
    while curr <= actual_end:
        sample_ts.append(round(curr, 3))
        curr += sample_step

    detected_faces = []
    for t in sample_ts:
        frame = extract_frame(video_path, t, info)
        if frame is not None:
            faces = detect_all_faces(frame, min_size=35)
            prim = get_primary_face(faces, src_w, src_h)
            if prim:
                prim["time"] = t
                detected_faces.append(prim)

    if not detected_faces:
        logger.warning(f"No face detected in {video_path} between {start_t}s and {end_t}s; using center fallback.")
        median_cx = src_w // 2
        median_cy = src_h // 3
        median_size = min(src_w, src_h) // 4
        min_cx = median_cx
        max_cx = median_cx
    else:
        all_cxs = [f["cx"] for f in detected_faces]
        median_cx = int(np.median(all_cxs))
        median_cy = int(np.median([f["cy"] for f in detected_faces]))
        median_size = int(np.median([max(f["w"], f["h"]) for f in detected_faces]))
        min_cx = min(all_cxs)
        max_cx = max(all_cxs)

    movement_span = int(max_cx - min_cx)
    target_aspect = target_w / target_h

    if is_widescreen:
        # Case A: 16:9 Widescreen -> Crop 9:16 window and scale to target_w x target_h
        desired_out_face_size = 250
        scale_needed = desired_out_face_size / max(median_size, 40)
        max_allowable_crop_h = int(target_h * median_size / 220.0)

        if max_allowable_crop_h >= src_h:
            crop_h = src_h
        else:
            crop_h = max(480, min(src_h, int(target_h / scale_needed)))

        crop_w = int(round(crop_h * target_aspect))
        crop_w -= (crop_w % 2)
        crop_h -= (crop_h % 2)

        # Vertical crop: upper-middle placement (target out_cy = 500)
        target_out_cy = 500
        ideal_crop_y = int(median_cy - target_out_cy * (crop_h / target_h))
        crop_y = max(0, min(src_h - crop_h, ideal_crop_y))
        crop_y -= (crop_y % 2)

        max_crop_x = max(0, src_w - crop_w)

        # Dynamic tracking vs static crop:
        if movement_span <= 120 or len(detected_faces) < 3:
            ideal_crop_x = int(median_cx - crop_w // 2)
            crop_x = max(0, min(max_crop_x, ideal_crop_x))
            crop_x -= (crop_x % 2)
            is_dynamic = False
            dynamic_params = {
                "start_t": actual_start,
                "end_t": actual_end,
                "duration": dur,
                "type": "static",
                "movement_span": movement_span,
                "x_start": crop_x,
                "x_end": crop_x,
                "max_crop_x": max_crop_x
            }
            filter_str = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={target_w}:{target_h}:flags=lanczos"
            mode = "widescreen_crop"
        else:
            is_dynamic = True
            mode = "widescreen_dynamic_crop"
            sorted_faces = sorted(detected_faces, key=lambda f: f["time"])
            start_cutoff = actual_start + dur * 0.30
            start_cands = [f["cx"] for f in sorted_faces if f["time"] <= start_cutoff]
            cx_start = int(np.median(start_cands)) if start_cands else sorted_faces[0]["cx"]

            end_cutoff = actual_end - dur * 0.30
            end_cands = [f["cx"] for f in sorted_faces if f["time"] >= end_cutoff]
            cx_end = int(np.median(end_cands)) if end_cands else sorted_faces[-1]["cx"]

            mid_cands = [f["cx"] for f in sorted_faces if start_cutoff < f["time"] < end_cutoff]
            cx_mid = int(np.median(mid_cands)) if mid_cands else (cx_start + cx_end) // 2

            x_start = max(0, min(max_crop_x, cx_start - crop_w // 2))
            x_end = max(0, min(max_crop_x, cx_end - crop_w // 2))
            x_mid = max(0, min(max_crop_x, cx_mid - crop_w // 2))
            x_start -= (x_start % 2)
            x_end -= (x_end % 2)
            x_mid -= (x_mid % 2)

            expected_mid = (x_start + x_end) // 2
            if abs(x_mid - expected_mid) <= 50:
                dynamic_type = "linear"
                t_mid = dur / 2.0
                x_expr = f"clip({x_start} + ({x_end} - {x_start})*t/{dur:.3f}, 0, in_w-out_w)"
            else:
                dynamic_type = "piecewise_3"
                t_mid = round(dur / 2.0, 3)
                x_expr = f"clip(if(lt(t, {t_mid}), {x_start} + ({x_mid} - {x_start})*t/{t_mid}, {x_mid} + ({x_end} - {x_mid})*(t - {t_mid})/({dur - t_mid:.3f})), 0, in_w-out_w)"

            crop_x = (x_start + x_end) // 2
            crop_x -= (crop_x % 2)
            dynamic_params = {
                "start_t": actual_start,
                "end_t": actual_end,
                "duration": dur,
                "type": dynamic_type,
                "movement_span": movement_span,
                "x_start": x_start,
                "x_mid": x_mid,
                "x_end": x_end,
                "t_mid": t_mid,
                "max_crop_x": max_crop_x
            }
            filter_str = f"crop={crop_w}:{crop_h}:'{x_expr}':{crop_y},scale={target_w}:{target_h}:flags=lanczos"

        projected_cx = int((median_cx - crop_x) * (target_w / crop_w))
        projected_cy = int((median_cy - crop_y) * (target_h / crop_h))
        projected_size = int(median_size * (target_h / crop_h))

        return {
            "mode": mode,
            "source_dimensions": {"width": src_w, "height": src_h},
            "crop": {"w": crop_w, "h": crop_h, "x": crop_x, "y": crop_y},
            "median_source_face": {"cx": median_cx, "cy": median_cy, "size": median_size},
            "projected_face": {"cx": projected_cx, "cy": projected_cy, "size": projected_size},
            "movement_span": movement_span,
            "is_dynamic": is_dynamic,
            "dynamic_params": dynamic_params,
            "samples_analyzed": len(sample_ts),
            "faces_found": len(detected_faces),
            "filter_str": filter_str
        }

    else:
        # Case B: Already vertical (e.g. 608x1080, 480x854, 720x1280)
        base_scale = max(target_w / src_w, target_h / src_h)
        base_size = int(round(median_size * base_scale))
        scaled_min_cx = min_cx * base_scale
        scaled_max_cx = max_cx * base_scale

        zoom = 1.0
        if base_size < 220:
            zoom = max(zoom, min(1.35, 240.0 / max(base_size, 50)))

        needed_crop_slack = 0.0
        if scaled_min_cx < 400:
            needed_crop_slack = max(needed_crop_slack, 400.0 - scaled_min_cx)
        if scaled_max_cx > 680:
            needed_crop_slack = max(needed_crop_slack, scaled_max_cx - 680.0)

        if needed_crop_slack > 0:
            needed_zoom = (target_w + 2.0 * needed_crop_slack) / target_w
            zoom = max(zoom, min(1.35, needed_zoom))

        zoom = round(zoom, 3)
        target_scale_w = int(round(target_w * zoom))
        target_scale_h = int(round(target_h * zoom))

        scale_factor = max(target_scale_w / src_w, target_scale_h / src_h)
        scaled_w = int(round(src_w * scale_factor))
        scaled_h = int(round(src_h * scale_factor))

        scaled_cx = int(round(median_cx * scale_factor))
        scaled_cy = int(round(median_cy * scale_factor))
        scaled_size = int(round(median_size * scale_factor))

        cur_min_cx = min_cx * scale_factor
        cur_max_cx = max_cx * scale_factor
        max_crop_x = max(0, scaled_w - target_w)

        # Vertical crop offset to keep face in upper-middle third (target cy ~ 480-550)
        if scaled_h > target_h:
            ideal_y = int(scaled_cy - 500)
            crop_y = max(0, min(scaled_h - target_h, ideal_y))
            crop_y -= (crop_y % 2)
        else:
            crop_y = 0

        # Horizontal offset & dynamic tracking for vertical:
        if scaled_w > target_w:
            ideal_x = scaled_cx - (target_w // 2)
            min_allow_x = max(0, int(cur_max_cx - 690))
            max_allow_x = min(scaled_w - target_w, int(cur_min_cx - 390))
            if min_allow_x <= max_allow_x:
                crop_x = int(max(min_allow_x, min(max_allow_x, ideal_x)))
            else:
                crop_x = int(max(0, min(scaled_w - target_w, ideal_x)))
            crop_x -= (crop_x % 2)

            scaled_movement = movement_span * scale_factor
            if scaled_movement > 120 and len(detected_faces) >= 3 and max_crop_x >= 50:
                is_dynamic = True
                mode = "vertical_dynamic_crop"
                sorted_faces = sorted(detected_faces, key=lambda f: f["time"])
                start_cands = [f["cx"] for f in sorted_faces if f["time"] <= actual_start + dur * 0.30]
                cx_start = int(np.median(start_cands)) if start_cands else sorted_faces[0]["cx"]
                end_cands = [f["cx"] for f in sorted_faces if f["time"] >= actual_end - dur * 0.30]
                cx_end = int(np.median(end_cands)) if end_cands else sorted_faces[-1]["cx"]

                x_start = max(0, min(max_crop_x, int(round(cx_start * scale_factor)) - target_w // 2))
                x_end = max(0, min(max_crop_x, int(round(cx_end * scale_factor)) - target_w // 2))
                x_start -= (x_start % 2)
                x_end -= (x_end % 2)

                x_expr = f"clip({x_start} + ({x_end} - {x_start})*t/{dur:.3f}, 0, in_w-out_w)"
                filter_str = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}:'{x_expr}':{crop_y}"
                dynamic_params = {
                    "start_t": actual_start,
                    "end_t": actual_end,
                    "duration": dur,
                    "type": "linear",
                    "movement_span": movement_span,
                    "x_start": x_start,
                    "x_end": x_end,
                    "max_crop_x": max_crop_x
                }
            else:
                is_dynamic = False
                mode = "vertical_scale_crop"
                dynamic_params = {
                    "start_t": actual_start,
                    "end_t": actual_end,
                    "duration": dur,
                    "type": "static",
                    "movement_span": movement_span,
                    "x_start": crop_x,
                    "x_end": crop_x,
                    "max_crop_x": max_crop_x
                }
                if zoom <= 1.01 and scaled_w == target_w and scaled_h == target_h and crop_x == 0 and crop_y == 0:
                    filter_str = f"scale={target_w}:{target_h}"
                elif zoom <= 1.01 and crop_x == (scaled_w - target_w) // 2 and crop_y == (scaled_h - target_h) // 2:
                    filter_str = f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}:(in_w-{target_w})/2:(in_h-{target_h})/2"
                else:
                    filter_str = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}:{crop_x}:{crop_y}"
        else:
            crop_x = 0
            is_dynamic = False
            mode = "vertical_scale_crop"
            dynamic_params = {
                "start_t": actual_start,
                "end_t": actual_end,
                "duration": dur,
                "type": "static",
                "movement_span": movement_span,
                "x_start": 0,
                "x_end": 0,
                "max_crop_x": 0
            }
            filter_str = f"scale={target_w}:{target_h}"

        projected_cx = scaled_cx - crop_x
        projected_cy = scaled_cy - crop_y
        projected_size = scaled_size

        return {
            "mode": mode,
            "source_dimensions": {"width": src_w, "height": src_h},
            "scaled_dimensions": {"width": scaled_w, "height": scaled_h},
            "zoom": zoom,
            "crop": {"x": crop_x, "y": crop_y, "w": target_w, "h": target_h},
            "median_source_face": {"cx": median_cx, "cy": median_cy, "size": median_size},
            "projected_face": {"cx": projected_cx, "cy": projected_cy, "size": projected_size},
            "movement_span": movement_span,
            "is_dynamic": is_dynamic,
            "dynamic_params": dynamic_params,
            "samples_analyzed": len(sample_ts),
            "faces_found": len(detected_faces),
            "filter_str": filter_str
        }


def compute_framing_parameters(
    video_path: str,
    start_t: float,
    end_t: float,
    target_w: int = TARGET_WIDTH,
    target_h: int = TARGET_HEIGHT,
    sample_step: float = 0.5
) -> Dict[str, Any]:
    """
    Computes auto-framing parameters (delegates to compute_dynamic_framing_parameters).
    Maintains full backwards compatibility for existing callers.
    """
    return compute_dynamic_framing_parameters(
        video_path=video_path,
        start_t=start_t,
        end_t=end_t,
        target_w=target_w,
        target_h=target_h,
        sample_step=sample_step
    )


def render_framed_clip(
    input_path: str,
    output_path: str,
    start_t: Optional[float] = None,
    end_t: Optional[float] = None,
    filter_str: Optional[str] = None
) -> Dict[str, Any]:
    """Renders a trimmed and auto-framed 1080x1920 vertical video."""
    info = get_video_info(input_path)
    s_t = max(0.0, start_t if start_t is not None else 0.0)
    e_t = min(info["duration"], end_t if end_t is not None else info["duration"])
    dur = max(0.1, e_t - s_t)

    if not filter_str:
        framing = compute_framing_parameters(input_path, s_t, e_t)
        filter_str = framing["filter_str"]
    else:
        framing = {}

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{s_t:.3f}",
        "-i", input_path,
        "-t", f"{dur:.3f}",
        "-vf", f"{filter_str},setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        str(out_p)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg render failed:\n{' '.join(cmd)}\n{res.stderr}")

    return {
        "output_path": str(out_p),
        "duration": dur,
        "filter_str": filter_str,
        "framing": framing
    }


def verify_face_framing(
    video_path: str,
    start_t: Optional[float] = None,
    end_t: Optional[float] = None,
    step_sec: float = 1.0,
    auto_frame: bool = True,
    raise_on_fail: bool = False,
    debug_dir: Optional[str] = DEFAULT_DEBUG_DIR
) -> Dict[str, Any]:
    """
    Automated HARD-GATE Validator for 1080x1920 TikTok vertical format:
    Samples frames every step_sec between start_t and end_t.
    If input video is not 1080x1920 and auto_frame is True, frames are transformed
    via compute_framing_parameters so the actual 1080x1920 output is validated.

    Strict Hard-Gate Checks for 100% of samples:
    1) Face detected? (Must be True for 100% of samples)
    2) Face size >= 180x180 px? (Must be True for 100% of samples, no wide shots)
    3) Face center X within [380, 700]? (Must be centered)
    4) Face center Y within [200, 850]?
    """
    info = get_video_info(video_path)
    duration = info["duration"]
    src_w = info["width"]
    src_h = info["height"]

    actual_start = max(0.0, start_t if start_t is not None else 0.0)
    actual_end = min(duration, end_t if end_t is not None else duration)

    if actual_end <= actual_start:
        actual_end = min(duration, actual_start + 1.0)

    is_target_dim = (src_w == TARGET_WIDTH and src_h == TARGET_HEIGHT)
    framing = None
    if not is_target_dim and auto_frame:
        framing = compute_framing_parameters(video_path, actual_start, actual_end)

    dbg_path = None
    if debug_dir:
        dbg_path = Path(debug_dir)
        dbg_path.mkdir(parents=True, exist_ok=True)

    video_stem = Path(video_path).stem
    samples = []
    overall_passed = True
    first_failure_reason = None

    curr_t = actual_start
    if curr_t == 0.0:
        curr_t = min(0.2, duration / 2.0 if duration > 0 else 0.2)

    sample_ts = []
    while curr_t <= actual_end:
        sample_ts.append(round(curr_t, 3))
        curr_t += step_sec

    if not sample_ts:
        sample_ts = [round((actual_start + actual_end) / 2.0, 3)]

    logger.info(f"Verifying face framing for {video_path} across [{actual_start:.2f}s - {actual_end:.2f}s] ({len(sample_ts)} samples, step={step_sec}s)")

    for t in sample_ts:
        raw_frame = extract_frame(video_path, t, info)
        if raw_frame is None:
            sample_rec = {
                "time_sec": t,
                "passed": False,
                "face_detected": False,
                "error": "Failed to decode video frame at timestamp"
            }
            samples.append(sample_rec)
            overall_passed = False
            if not first_failure_reason:
                first_failure_reason = f"t={t:.2f}s: Frame decode failure"
            continue

        if is_target_dim:
            eval_frame = raw_frame
        elif framing:
            crop_x = get_crop_x_at_t(framing, t)
            if "widescreen" in framing.get("mode", ""):
                c = framing["crop"]
                cropped = raw_frame[c["y"]:c["y"]+c["h"], crop_x:crop_x+c["w"]]
                eval_frame = cv2.resize(cropped, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_LANCZOS4)
            else:
                sw = framing["scaled_dimensions"]["width"]
                sh = framing["scaled_dimensions"]["height"]
                scaled = cv2.resize(raw_frame, (sw, sh), interpolation=cv2.INTER_LANCZOS4)
                cy = framing["crop"]["y"]
                eval_frame = scaled[cy:cy+TARGET_HEIGHT, crop_x:crop_x+TARGET_WIDTH]
        else:
            eval_frame = raw_frame

        eval_h, eval_w = eval_frame.shape[:2]

        # When evaluating a rendered 1080x1920 portrait video, set min_size=160
        # (comedians in portrait 1080x1920 are >= 180px, so any box <160px is an artifact, not the comedian)
        eval_min_size = 160 if (is_target_dim or eval_h >= 1500) else 50
        faces = detect_all_faces(eval_frame, min_size=eval_min_size)
        primary = get_primary_face(faces, eval_w, eval_h)

        sample_rec = {
            "time_sec": t,
            "face_detected": primary is not None,
            "face_box": None,
            "face_center": None,
            "face_size": None,
            "checks": {
                "face_detected": False,
                "size_ge_180": False,
                "center_x_in_380_700": False,
                "center_y_in_200_850": False
            },
            "passed": False,
            "failure_reasons": []
        }

        if primary is None:
            sample_rec["failure_reasons"].append("No face detected")
        else:
            px = primary["x"]
            py = primary["y"]
            pw = primary["w"]
            ph = primary["h"]
            pcx = primary["cx"]
            pcy = primary["cy"]
            face_size = max(pw, ph)

            sample_rec["face_box"] = [px, py, pw, ph]
            sample_rec["face_center"] = [pcx, pcy]
            sample_rec["face_size"] = face_size

            sample_rec["checks"]["face_detected"] = True

            if face_size >= 180:
                sample_rec["checks"]["size_ge_180"] = True
            else:
                sample_rec["failure_reasons"].append(f"Face size too small: {face_size}px < 180px (wide shot)")

            if 380 <= pcx <= 700:
                sample_rec["checks"]["center_x_in_380_700"] = True
            else:
                sample_rec["failure_reasons"].append(f"Face center X ({pcx}px) outside [380, 700] (off-center: offset={pcx-540:+d}px)")

            if 200 <= pcy <= 850:
                sample_rec["checks"]["center_y_in_200_850"] = True
            else:
                sample_rec["failure_reasons"].append(f"Face center Y ({pcy}px) outside [200, 850]")

            if not sample_rec["failure_reasons"]:
                sample_rec["passed"] = True

        if not sample_rec["passed"]:
            overall_passed = False
            if not first_failure_reason:
                first_failure_reason = f"t={t:.2f}s: {', '.join(sample_rec['failure_reasons'])}"

        if dbg_path:
            dbg_img = eval_frame.copy()
            status_color = (0, 255, 0) if sample_rec["passed"] else (0, 0, 255)

            cv2.line(dbg_img, (540, 0), (540, eval_h), (255, 255, 0), 1)
            cv2.line(dbg_img, (380, 0), (380, eval_h), (200, 200, 200), 1)
            cv2.line(dbg_img, (700, 0), (700, eval_h), (200, 200, 200), 1)
            cv2.line(dbg_img, (0, 200), (eval_w, 200), (200, 200, 200), 1)
            cv2.line(dbg_img, (0, 850), (eval_w, 850), (200, 200, 200), 1)

            if primary:
                cv2.rectangle(dbg_img, (primary["x"], primary["y"]),
                              (primary["x"] + primary["w"], primary["y"] + primary["h"]),
                              status_color, 3)
                cv2.circle(dbg_img, (primary["cx"], primary["cy"]), 7, status_color, -1)
                lbl = f"Face: {primary['w']}x{primary['h']} ({primary['type']})"
                cv2.putText(dbg_img, lbl, (primary["x"], max(25, primary["y"] - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)

            res_str = "PASS" if sample_rec["passed"] else "FAIL"
            header = f"[{res_str}] t={t:.2f}s | size={sample_rec.get('face_size', 'None')} | cx={primary['cx'] if primary else 'None'} | cy={primary['cy'] if primary else 'None'}"
            cv2.rectangle(dbg_img, (20, 20), (eval_w - 20, 90), (0, 0, 0), -1)
            cv2.putText(dbg_img, header, (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.75, status_color, 2)

            if sample_rec["failure_reasons"]:
                err_text = "; ".join(sample_rec["failure_reasons"])
                cv2.putText(dbg_img, f"Reason: {err_text[:70]}", (30, 85),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1)

            out_file = dbg_path / f"{video_stem}_verify_{t:.2f}s.jpg"
            cv2.imwrite(str(out_file), dbg_img)

        samples.append(sample_rec)

    total_samples = len(samples)
    passed_samples = sum(1 for s in samples if s["passed"])
    pass_rate = (passed_samples / total_samples * 100.0) if total_samples > 0 else 0.0

    result = {
        "passed": overall_passed,
        "video_path": video_path,
        "time_range": [actual_start, actual_end],
        "total_samples": total_samples,
        "passed_samples": passed_samples,
        "pass_rate_pct": round(pass_rate, 1),
        "error": None if overall_passed else first_failure_reason,
        "samples": samples
    }

    if not overall_passed and raise_on_fail:
        raise FaceFramingValidationError(f"Hard-gate face framing failed for {video_path}: {first_failure_reason}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Face Tracking, Dynamic Auto-Framing & Hard-Gate Verification Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_p = subparsers.add_parser("verify", help="Verify face framing on video")
    verify_p.add_argument("video_path", help="Path to video file")
    verify_p.add_argument("--start", type=float, default=None, help="Start time (sec)")
    verify_p.add_argument("--end", type=float, default=None, help="End time (sec)")
    verify_p.add_argument("--step", type=float, default=1.0, help="Sampling step (sec)")
    verify_p.add_argument("--no-auto-frame", action="store_true", help="Do not auto-frame non-1080x1920 inputs")
    verify_p.add_argument("--debug-dir", type=str, default=DEFAULT_DEBUG_DIR, help="Visual debug frames output directory")
    verify_p.add_argument("--raise-on-fail", action="store_true", help="Raise exception if validation fails")

    frame_p = subparsers.add_parser("auto-frame", help="Analyze video and compute framing filter")
    frame_p.add_argument("video_path", help="Path to video file")
    frame_p.add_argument("--start", type=float, default=0.0, help="Start time (sec)")
    frame_p.add_argument("--end", type=float, default=0.0, help="End time (sec)")
    frame_p.add_argument("--step", type=float, default=0.5, help="Sampling step (sec)")

    dyn_p = subparsers.add_parser("dynamic-frame", help="Compute dynamic auto-framing parameters")
    dyn_p.add_argument("video_path", help="Path to video file")
    dyn_p.add_argument("--start", type=float, default=0.0, help="Start time (sec)")
    dyn_p.add_argument("--end", type=float, default=0.0, help="End time (sec)")
    dyn_p.add_argument("--step", type=float, default=0.5, help="Sampling step (sec)")

    render_p = subparsers.add_parser("render", help="Render auto-framed 1080x1920 video")
    render_p.add_argument("video_path", help="Input video path")
    render_p.add_argument("output_path", help="Output 1080x1920 video path")
    render_p.add_argument("--start", type=float, default=None, help="Start time (sec)")
    render_p.add_argument("--end", type=float, default=None, help="End time (sec)")

    args = parser.parse_args()

    if args.command == "verify":
        res = verify_face_framing(
            args.video_path,
            start_t=args.start,
            end_t=args.end,
            step_sec=args.step,
            auto_frame=not args.no_auto_frame,
            raise_on_fail=args.raise_on_fail,
            debug_dir=args.debug_dir
        )
        print(json.dumps(res, indent=2))
        sys.exit(0 if res["passed"] else 1)

    elif args.command in ["auto-frame", "dynamic-frame"]:
        params = compute_dynamic_framing_parameters(
            args.video_path,
            start_t=args.start,
            end_t=args.end,
            sample_step=args.step
        )
        print(json.dumps(params, indent=2))

    elif args.command == "render":
        res = render_framed_clip(
            args.video_path,
            args.output_path,
            start_t=args.start,
            end_t=args.end
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
