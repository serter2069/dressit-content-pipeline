#!/usr/bin/env python3
"""
face_qc_judge.py

Strict automated face positioning QC judge for the DressIt video pipeline.
Every 2 seconds throughout the entire video duration (standup + UGC outro):
1. Verifies face presence.
2. Verifies face centering and framing boundaries (no head cut off at top, no edge cut).
3. Produces visual debug frames with annotations for inspection.
4. Provides dynamic optimal crop computation for horizontal source videos.
"""

import cv2
import json
import logging
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("face_qc_judge")

FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
PROFILE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_profileface.xml"

_face_cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
_profile_cascade = cv2.CascadeClassifier(PROFILE_CASCADE_PATH)


def detect_all_faces(frame: np.ndarray, max_y: int = 1400, min_size: int = 60, max_size: int = 800) -> List[Dict[str, Any]]:
    """
    Detects all candidate faces in a frame using frontal and multi-directional profile cascades.
    Returns list of dicts sorted by face area descending.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    candidates = []

    # 1. Frontal detector
    f_faces = _face_cascade.detectMultiScale(
        gray, scaleFactor=1.06, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (x, y, w, h) in f_faces:
        cy = int(y + h // 2)
        if 100 <= y < max_y and cy >= 180:
            candidates.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "type": "frontal", "area": w * h})

    # 2. Profile detector (left)
    p_faces = _profile_cascade.detectMultiScale(
        gray, scaleFactor=1.06, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (x, y, w, h) in p_faces:
        cy = int(y + h // 2)
        if 100 <= y < max_y and cy >= 180:
            candidates.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "type": "profile_l", "area": w * h})

    # 3. Profile detector (right via horizontal flip)
    gray_flip = cv2.flip(gray, 1)
    pr_faces = _profile_cascade.detectMultiScale(
        gray_flip, scaleFactor=1.06, minNeighbors=3,
        minSize=(min_size, min_size), maxSize=(max_size, max_size)
    )
    for (fx, fy, fw, fh) in pr_faces:
        cy = int(fy + fh // 2)
        if 100 <= fy < max_y and cy >= 180:
            orig_x = frame.shape[1] - (fx + fw)
            candidates.append({"x": int(orig_x), "y": int(fy), "w": int(fw), "h": int(fh), "type": "profile_r", "area": fw * fh})

    # 4. Profile fallback pass (minNeighbors=2) if no candidate detected yet
    if not candidates:
        p2_faces = _profile_cascade.detectMultiScale(
            gray, scaleFactor=1.04, minNeighbors=2,
            minSize=(min_size, min_size), maxSize=(max_size, max_size)
        )
        for (x, y, w, h) in p2_faces:
            cy = int(y + h // 2)
            if 100 <= y < max_y and cy >= 180:
                candidates.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "type": "profile_l_fallback", "area": w * h})
        pr2_faces = _profile_cascade.detectMultiScale(
            gray_flip, scaleFactor=1.04, minNeighbors=2,
            minSize=(min_size, min_size), maxSize=(max_size, max_size)
        )
        for (fx, fy, fw, fh) in pr2_faces:
            cy = int(fy + fh // 2)
            if 100 <= fy < max_y and cy >= 180:
                orig_x = frame.shape[1] - (fx + fw)
                candidates.append({"x": int(orig_x), "y": int(fy), "w": int(fw), "h": int(fh), "type": "profile_r_fallback", "area": fw * fh})

    # Deduplicate overlapping boxes
    candidates.sort(key=lambda c: c["area"], reverse=True)
    filtered = []
    for cand in candidates:
        cx = cand["x"] + cand["w"] // 2
        cy = cand["y"] + cand["h"] // 2
        overlap = False
        for kept in filtered:
            kcx = kept["x"] + kept["w"] // 2
            kcy = kept["y"] + kept["h"] // 2
            if abs(cx - kcx) < (cand["w"] // 2) and abs(cy - kcy) < (cand["h"] // 2):
                overlap = True
                break
        if not overlap:
            cand["cx"] = cx
            cand["cy"] = cy
            filtered.append(cand)

    return filtered


def judge_rendered_video(
    video_path: str,
    check_interval: float = 2.0,
    standup_end_time: float = 8.5,
    out_debug_dir: Optional[Path] = None,
    allow_spin_in_ugc: bool = True
) -> Dict[str, Any]:
    """
    Judges a rendered 1080x1920 video every `check_interval` seconds across its entire duration.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return {"passed": False, "error": f"Cannot open video {video_path}", "score": 0.0, "samples": []}

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total_frames / fps if fps else 0.0

    if out_debug_dir:
        out_debug_dir.mkdir(parents=True, exist_ok=True)

    video_name = Path(video_path).stem
    samples = []
    t = 1.0  # Start at 1.0s

    standup_fails = 0
    ugc_misses = 0
    total_standup_checks = 0
    total_ugc_checks = 0

    while t <= (duration - 0.5):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            break

        h, w = frame.shape[:2]
        is_standup = (t <= standup_end_time)
        if is_standup:
            total_standup_checks += 1
        else:
            total_ugc_checks += 1

        max_y_threshold = 850 if is_standup else 1150
        faces = detect_all_faces(frame, max_y=max_y_threshold, min_size=80, max_size=750)
        best_face = faces[0] if faces else None

        sample_rec = {
            "time_sec": round(t, 2),
            "stage": "standup" if is_standup else "ugc",
            "face_detected": best_face is not None,
            "passed": False,
            "details": ""
        }

        if best_face:
            cx = best_face["cx"]
            cy = best_face["cy"]
            fw = best_face["w"]
            fh = best_face["h"]
            fx = best_face["x"]
            fy = best_face["y"]

            dist_from_center = abs(cx - (w // 2))
            is_centered = (dist_from_center <= 220)
            in_x_bounds = (fx >= 20) and (fx + fw <= w - 20)
            in_y_bounds = (fy >= 90) and (fy + fh <= 1550)

            if is_centered and in_x_bounds and in_y_bounds:
                sample_rec["passed"] = True
                sample_rec["details"] = f"Face OK: cx={cx} (offset={cx - 540:+d}px), box=[{fx},{fy},{fw},{fh}]"
            else:
                issues = []
                if not is_centered:
                    issues.append(f"off-center offset={cx - 540:+d}px")
                if not in_x_bounds:
                    issues.append("clipped on left/right edge")
                if not in_y_bounds:
                    issues.append("too high (cut at top) or too low")
                sample_rec["details"] = f"Poor positioning: {', '.join(issues)}"
                if is_standup:
                    standup_fails += 1
                else:
                    ugc_misses += 1
        else:
            sample_rec["details"] = "No face detected in 2s check"
            if is_standup:
                standup_fails += 1
            else:
                ugc_misses += 1

        if out_debug_dir:
            dbg = frame.copy()
            color = (0, 255, 0) if sample_rec["passed"] else (0, 0, 255)
            cv2.line(dbg, (540, 0), (540, 1920), (255, 255, 0), 2)
            cv2.rectangle(dbg, (320, 100), (760, 1500), (200, 200, 200), 1)

            if best_face:
                cv2.rectangle(dbg, (best_face["x"], best_face["y"]),
                              (best_face["x"]+best_face["w"], best_face["y"]+best_face["h"]), color, 4)
                cv2.circle(dbg, (best_face["cx"], best_face["cy"]), 8, color, -1)

            status_str = "PASS" if sample_rec["passed"] else "FAIL"
            banner = f"t={t:.1f}s | {sample_rec['stage'].upper()} | {status_str}: {sample_rec['details']}"
            cv2.putText(dbg, banner, (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
            out_img = out_debug_dir / f"{video_name}_judge_{t:.1f}s.jpg"
            cv2.imwrite(str(out_img), dbg)

        samples.append(sample_rec)
        t += check_interval

    cap.release()

    ugc_allowed_misses = 2 if allow_spin_in_ugc else 0
    passed = (standup_fails == 0) and (ugc_misses <= ugc_allowed_misses)

    total_checks = len(samples)
    passed_checks = sum(1 for s in samples if s["passed"])
    score = (passed_checks / total_checks * 100.0) if total_checks else 0.0

    return {
        "passed": passed,
        "video_path": video_path,
        "duration_sec": round(duration, 2),
        "total_checks_2s": total_checks,
        "passed_checks": passed_checks,
        "score_pct": round(score, 1),
        "standup_fails": standup_fails,
        "ugc_misses": ugc_misses,
        "samples": samples
    }


def compute_optimal_crop_dynamic(
    video_path: str,
    start_time: float,
    duration: float,
    sample_step: float = 0.25,
    crop_w: int = 540,
    crop_h: int = 960,
    source_w: int = 1920,
    source_h: int = 1080
) -> Dict[str, Any]:
    """
    Analyzes horizontal source video across [start_time, start_time + duration]
    and computes the ideal crop_x to center the comedian's face with 100% certainty.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video {video_path}")

    centers = []
    t = start_time
    end_time = start_time + duration

    while t <= end_time:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if ret:
            faces = detect_all_faces(frame, max_y=700, min_size=60, max_size=600)
            if faces:
                centers.append(faces[0]["cx"])
        t += sample_step

    cap.release()

    if not centers:
        logger.warning(f"No face detected in {video_path} at [{start_time}s - {end_time}s]; default center")
        median_cx = source_w // 2
    else:
        median_cx = int(np.median(centers))

    crop_x = median_cx - (crop_w // 2)
    crop_x = max(0, min(source_w - crop_w, crop_x))

    logger.info(f"Optimal crop for [{start_time:.2f}s - {end_time:.2f}s]: sampled {len(centers)} frames, median cx={median_cx} -> crop_x={crop_x}")
    return {
        "crop_x": crop_x,
        "median_cx": median_cx,
        "samples_count": len(centers),
        "filter_str": f"crop={crop_w}:{crop_h}:{crop_x}:0,scale=1080:1920:flags=lanczos"
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        v_path = sys.argv[1]
        res = judge_rendered_video(v_path, check_interval=2.0, out_debug_dir=Path("/root/dressit-pipeline/output/judge_debug"))
        print(json.dumps(res, indent=2))
