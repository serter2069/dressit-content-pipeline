#!/usr/bin/env python3
"""dedup_engine.py — Multimodal Content Deduplication Engine for DressIt Posting Pipeline (Task #1955).

Provides high-performance, deterministic deduplication across four modalities:
1. Visual Fingerprinting:
   - Keyframe extraction at 10% (hook), 50% (middle), and 90% (outro) using ffmpeg.
   - 64-bit dHash (difference hash) on 9x8 grayscale frames.
   - Composite perceptual video hash and Hamming-distance-based similarity.
2. Audio Fingerprinting:
   - 16kHz mono PCM audio extraction.
   - Spectral energy representation (sub-band time-frequency matrix via STFT).
   - 128-bit binary energy difference hash and bitwise similarity.
   - Graceful handling of silent or absent audio tracks.
3. Text/Script Deduplication:
   - Unicode-aware normalization, URL/emoji/punctuation removal.
   - Word 3-gram Jaccard similarity (with fallback for short texts).
   - Efficient O(min(N,M)) space Levenshtein distance similarity.
   - Combined text similarity metric.
4. Source-Interval Tracking:
   - Parses `<source_id>:<start>-<end>` format and lists of intervals.
   - Computes overlap ratio normalized by minimum clip duration.
5. Decision Engine (evaluate_duplicate):
   - Hard duplicate rejection (visual >= 0.90 or source overlap >= 0.70 & text >= 0.80).
   - Soft duplicate detection (visual >= 0.75 or text >= 0.70) with 14-day cooldown & cross-channel rules.
   - Unique content classification & in-between handling.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import scipy.signal
from PIL import Image

log = logging.getLogger(__name__)

# Regular expressions for text cleaning
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_NON_ALPHANUM_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Boilerplate and channel branding patterns to strip in clean_text
_BOILERPLATE_PATTERNS = [
    # Full channel boilerplate phrases with optional prefixes/hashtags
    re.compile(r"[!|•·\-]?\s*free\s+concert\s+fits\s+on\s+dressit\s*(?:#\s*shorts)?\b", re.IGNORECASE),
    re.compile(r"[!|•·\-]?\s*free\s+concert\s+fits\b", re.IGNORECASE),
    # Content category tags
    re.compile(r"relationships\s*[·•\-]", re.IGNORECASE),
    re.compile(r"[·•\-]\s*relationships\b", re.IGNORECASE),
    re.compile(r"\brelationships\b", re.IGNORECASE),
    # Channel brand / domains / hashtags
    re.compile(r"[·•\-]\s*dressit\b", re.IGNORECASE),
    re.compile(r"\bdressit\s*[·•\-]", re.IGNORECASE),
    re.compile(r"\bdressitnow\.com\S*", re.IGNORECASE),
    re.compile(r"\bdressit\.com\S*", re.IGNORECASE),
    re.compile(r"\bdressit\b", re.IGNORECASE),
    re.compile(r"#\s*shorts\b", re.IGNORECASE),
]


# ============================================================================
# 1. Visual Fingerprinting (Keyframes + dHash)
# ============================================================================

def get_video_duration(video_path: str) -> float:
    """Retrieve video duration in seconds via ffprobe.
    
    Returns 0.0 if the file does not exist, probe fails, or duration is unparseable.
    """
    if not video_path or not os.path.exists(video_path):
        return 0.0

    try:
        # First attempt: format duration
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        dur_str = res.stdout.strip()
        if dur_str and dur_str != "N/A":
            dur = float(dur_str)
            if dur > 0:
                return dur

        # Second attempt: stream 0 duration
        cmd_stream = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        res_stream = subprocess.run(cmd_stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        dur_str2 = res_stream.stdout.strip()
        if dur_str2 and dur_str2 != "N/A":
            dur = float(dur_str2)
            if dur > 0:
                return dur
    except Exception as e:
        log.debug("ffprobe failed on %s: %s", video_path, e)

    return 0.0


def extract_keyframes(video_path: str, percentages: Tuple[float, ...] = (0.10, 0.50, 0.90)) -> List[Image.Image]:
    """Extract keyframes at specified fractions of the video duration.
    
    Returns a list of PIL Image objects (RGB). Empty list on failure.
    """
    dur = get_video_duration(video_path)
    if dur <= 0:
        return []

    frames: List[Image.Image] = []
    for p in percentages:
        ts = max(0.0, min(dur, dur * float(p)))
        cmd = [
            "ffmpeg", "-ss", f"{ts:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1"
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if proc.returncode == 0 and proc.stdout:
                img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
                frames.append(img)
            else:
                log.debug("ffmpeg frame extraction failed at %.2fs for %s", ts, video_path)
        except Exception as e:
            log.debug("Error extracting keyframe at %.2fs: %s", ts, e)

    return frames


def compute_dhash(image: Union[Image.Image, np.ndarray, str, Path]) -> str:
    """Compute 64-bit difference hash (dHash) for an image.
    
    1. Resizes image to 9x8 grayscale (width=9, height=8).
    2. Compares adjacent pixels horizontally: col[i] > col[i+1].
    3. Produces a 64-bit hex string (16 hex chars).
    """
    if isinstance(image, (str, Path)):
        img = Image.open(image)
    elif isinstance(image, np.ndarray):
        img = Image.fromarray(image)
    elif isinstance(image, Image.Image):
        img = image
    else:
        raise TypeError(f"Expected PIL Image, numpy array, or path, got {type(image)}")

    # 9 columns, 8 rows grayscale
    gray_resized = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    arr = np.asarray(gray_resized, dtype=np.int32)  # shape (8, 9)

    # 8 rows x 8 horizontal comparisons = 64 booleans
    diff = arr[:, :-1] > arr[:, 1:]

    # Pack 64 bits row-major
    val = 0
    for bit in diff.flatten():
        val = (val << 1) | int(bit)

    return f"{val:016x}"


def dhash_distance(h1: str, h2: str) -> int:
    """Hamming distance between two 64-bit dHash hex strings (0 to 64)."""
    if not h1 or not h2:
        return 64
    try:
        v1 = int(h1, 16)
        v2 = int(h2, 16)
        return (v1 ^ v2).bit_count()
    except ValueError:
        return 64


def dhash_similarity(h1: str, h2: str) -> float:
    """Similarity score between two 64-bit dHash hex strings in [0.0, 1.0]."""
    dist = dhash_distance(h1, h2)
    return max(0.0, min(1.0, 1.0 - (dist / 64.0)))


def compute_video_fingerprint(video_path: str) -> Dict[str, Any]:
    """Generate composite perceptual video hash from 3 keyframes (10%, 50%, 90%).
    
    Returns:
      {
        "duration": float,
        "k10": "16-hex-str",
        "k50": "16-hex-str",
        "k90": "16-hex-str",
        "composite": "k10:k50:k90"
      }
    """
    dur = get_video_duration(video_path)
    if dur <= 0:
        zero_hash = "0" * 16
        return {
            "duration": 0.0,
            "k10": zero_hash,
            "k50": zero_hash,
            "k90": zero_hash,
            "composite": f"{zero_hash}:{zero_hash}:{zero_hash}"
        }

    frames = extract_keyframes(video_path, percentages=(0.10, 0.50, 0.90))
    zero_hash = "0" * 16
    hashes: List[str] = []
    for f in frames:
        try:
            hashes.append(compute_dhash(f))
        except Exception:
            hashes.append(zero_hash)

    while len(hashes) < 3:
        hashes.append(hashes[-1] if hashes else zero_hash)

    h10, h50, h90 = hashes[0], hashes[1], hashes[2]
    return {
        "duration": round(dur, 3),
        "k10": h10,
        "k50": h50,
        "k90": h90,
        "composite": f"{h10}:{h50}:{h90}"
    }


def _extract_keyframe_hashes(fp: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """Extract (k10, k50, k90) dHash hex strings from video fingerprint dict."""
    if not isinstance(fp, dict):
        return None
    if "k10" in fp and "k50" in fp and "k90" in fp:
        return str(fp["k10"]), str(fp["k50"]), str(fp["k90"])
    comp = fp.get("composite", "")
    if isinstance(comp, str) and comp.count(":") == 2:
        parts = comp.split(":")
        return parts[0], parts[1], parts[2]
    return None


def video_similarity(fp1: Dict[str, Any], fp2: Dict[str, Any]) -> float:
    """Compute video visual similarity as the average similarity of the 3 keyframes.
    
    Formula: (sim10 + sim50 + sim90) / 3.0, where sim = 1.0 - (hamming_distance / 64.0).
    """
    if not isinstance(fp1, dict) or not isinstance(fp2, dict):
        return 0.0

    h1 = _extract_keyframe_hashes(fp1)
    h2 = _extract_keyframe_hashes(fp2)
    if not h1 or not h2:
        return 0.0

    sim10 = dhash_similarity(h1[0], h2[0])
    sim50 = dhash_similarity(h1[1], h2[1])
    sim90 = dhash_similarity(h1[2], h2[2])
    return round(float((sim10 + sim50 + sim90) / 3.0), 4)


# ============================================================================
# 2. Audio Fingerprinting (16kHz PCM + Spectral Energy Hash)
# ============================================================================

def extract_audio_samples(video_path: str, max_duration: float = 30.0) -> Optional[np.ndarray]:
    """Extract a 16kHz mono PCM audio slice from the video using ffmpeg.
    
    If video has no audio, is corrupt, or is silent, returns None.
    """
    if not video_path or not os.path.exists(video_path):
        return None

    cmd = [
        "ffmpeg", "-y",
        "-ss", "0",
        "-t", str(float(max_duration)),
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-f", "s16le",
        "pipe:1"
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if proc.returncode != 0 or not proc.stdout:
            return None

        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        # Check minimum length (>= 0.1s at 16kHz = 1600 samples)
        if len(samples) < 1600:
            return None

        # Check silence (amplitude threshold)
        if float(np.max(np.abs(samples))) < 1e-4 or float(np.std(samples)) < 1e-5:
            return None

        return samples
    except Exception as e:
        log.debug("Failed extracting audio samples for %s: %s", video_path, e)
        return None


def compute_audio_fingerprint(video_path_or_samples: Union[str, Path, np.ndarray, None]) -> Optional[str]:
    """Compute normalized spectral energy hash (128-bit hex string).
    
    Divides frequency into 16 log-spaced sub-bands across 9 time windows.
    Generates a 16x8 = 128-bit binary energy difference hash (32 hex chars).
    Returns None if audio is absent, silent, or invalid.
    """
    if video_path_or_samples is None:
        return None

    if isinstance(video_path_or_samples, (str, Path)):
        samples = extract_audio_samples(str(video_path_or_samples))
    elif isinstance(video_path_or_samples, np.ndarray):
        samples = video_path_or_samples
    else:
        return None

    if samples is None or len(samples) < 1600:
        return None

    if float(np.max(np.abs(samples))) < 1e-4 or float(np.std(samples)) < 1e-5:
        return None

    try:
        # Spectrogram: STFT with 512-point FFT, 256 hop
        f, times, Sxx = scipy.signal.spectrogram(
            samples, fs=16000, nperseg=512, noverlap=256
        )
        if Sxx.size == 0 or times.size < 2:
            return None

        # 16 frequency sub-bands (log-spaced between 80Hz and 7500Hz)
        freq_bins = np.logspace(np.log10(80.0), np.log10(7500.0), 17)
        # 9 uniform time windows -> 8 transitions
        time_edges = np.linspace(times[0], times[-1], 10)

        E = np.zeros((16, 9), dtype=np.float32)
        for i in range(16):
            f_mask = (f >= freq_bins[i]) & (f < freq_bins[i + 1])
            for j in range(9):
                t_mask = (times >= time_edges[j]) & (times <= time_edges[j + 1])
                sub = Sxx[f_mask][:, t_mask]
                E[i, j] = float(np.mean(sub)) if sub.size > 0 else 0.0

        # If energy matrix is virtually constant/silent
        if float(np.std(E)) < 1e-8:
            return None

        # Horizontal time difference across sub-bands: E[:, t] > E[:, t+1]
        diff = E[:, :-1] > E[:, 1:]  # shape (16, 8) -> 128 booleans

        val = 0
        for bit in diff.flatten():
            val = (val << 1) | int(bit)

        return f"{val:032x}"
    except Exception as e:
        log.debug("Failed computing audio fingerprint: %s", e)
        return None


def audio_similarity(afp1: Optional[str], afp2: Optional[str]) -> float:
    """Bitwise normalized match ratio between two audio energy hashes in [0.0, 1.0].
    
    Formula: 1.0 - (hamming_distance / total_bits).
    Returns 0.0 if either fingerprint is None or empty.
    """
    if not afp1 or not afp2:
        return 0.0

    try:
        v1 = int(afp1, 16)
        v2 = int(afp2, 16)
        total_bits = max(len(afp1), len(afp2)) * 4
        if total_bits <= 0:
            return 0.0
        dist = (v1 ^ v2).bit_count()
        return round(max(0.0, min(1.0, 1.0 - (dist / float(total_bits)))), 4)
    except Exception:
        return 0.0


# ============================================================================
# 3. Text/Script Deduplication (Cleaning, Jaccard 3-Gram, Levenshtein)
# ============================================================================

def clean_text(text: str) -> str:
    """Clean and normalize text:
    - Lowercase
    - Strip URLs (http://, https://, www.)
    - Strip common channel branding/boilerplate patterns
    - Strip emojis, punctuation, symbols (preserve Unicode letters, numbers, spaces)
    - Collapse multiple whitespace to single space and trim.
    """
    if not text or not isinstance(text, str):
        return ""

    t = _URL_RE.sub(" ", text)
    for pat in _BOILERPLATE_PATTERNS:
        t = pat.sub(" ", t)
    t = _NON_ALPHANUM_RE.sub(" ", t)
    t = _WHITESPACE_RE.sub(" ", t).strip().lower()
    return t


def tokenize(text: str) -> List[str]:
    """Tokenize cleaned text into word tokens."""
    c = clean_text(text)
    return c.split() if c else []


def _extract_ngrams(text: str) -> set:
    """Extract word 3-grams from text, with fallback to 1/2-grams and character 3-grams
    if text has fewer than 3 words.
    """
    tokens = tokenize(text)
    if len(tokens) >= 3:
        return {tuple(tokens[i:i + 3]) for i in range(len(tokens) - 2)}

    # Fallback for short texts (< 3 words)
    grams = set()
    for i in range(len(tokens)):
        grams.add((tokens[i],))
        if i + 1 < len(tokens):
            grams.add((tokens[i], tokens[i + 1]))

    c = clean_text(text)
    if len(c) >= 3:
        for i in range(len(c) - 2):
            grams.add(c[i:i + 3])
    elif c:
        grams.add(c)

    return grams


def jaccard_3gram(text1: str, text2: str) -> float:
    """Compute Jaccard similarity of 3-grams over tokens: |A ∩ B| / |A ∪ B|.
    
    If both strings are empty, returns 0.0.
    """
    c1 = clean_text(text1)
    c2 = clean_text(text2)
    if not c1 or not c2:
        return 0.0

    set1 = _extract_ngrams(c1)
    set2 = _extract_ngrams(c2)
    union = set1 | set2
    if not union:
        return 0.0

    return round(float(len(set1 & set2) / len(union)), 4)


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein distance using O(min(N, M)) space."""
    if s1 == s2:
        return 0
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)

    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1] * (len(s2) + 1)
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr[j + 1] = min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost)
        prev = curr
    return prev[len(s2)]


def levenshtein_similarity(text1: str, text2: str) -> float:
    """Normalized Levenshtein similarity: 1.0 - (dist / max(len(text1), len(text2), 1)).
    
    Operates on cleaned strings. Returns 0.0 if either string is empty.
    """
    c1 = clean_text(text1)
    c2 = clean_text(text2)
    if not c1 or not c2:
        return 0.0

    dist = _levenshtein_distance(c1, c2)
    max_len = max(len(c1), len(c2), 1)
    return round(max(0.0, min(1.0, 1.0 - (dist / float(max_len)))), 4)


def text_similarity(text1: str, text2: str) -> float:
    """Combined text similarity metric: 0.5 * jaccard + 0.5 * lev_sim.
    
    Returns 0.0 if either text is empty.
    """
    c1 = clean_text(text1)
    c2 = clean_text(text2)
    if not c1 or not c2:
        return 0.0

    jacc = jaccard_3gram(c1, c2)
    lev = levenshtein_similarity(c1, c2)
    return round(float(0.5 * jacc + 0.5 * lev), 4)


# ============================================================================
# 4. Source-Interval Tracking
# ============================================================================

def parse_source_ref(source_ref: Union[str, List[Any], Dict[str, Any], None]) -> List[Dict[str, Any]]:
    """Parse source references in format `<source_id>:<start>-<end>` or lists/dicts.
    
    Examples:
      - 'oagNYHB3Kzk:29.2-38.0'
      - 'src1:10-20, src2:30-45'
      - [{'source_id': 'src1', 'start': 10.0, 'end': 20.0}]
      
    Returns list of dicts: [{'source_id': str, 'start': float, 'end': float, 'duration': float}]
    """
    if not source_ref:
        return []

    if isinstance(source_ref, dict):
        src_id = str(source_ref.get("source_id", "")).strip()
        try:
            st = float(source_ref.get("start", 0.0))
            en = float(source_ref.get("end", 0.0))
            st, en = min(st, en), max(st, en)
            dur = max(0.0, en - st)
            return [{"source_id": src_id, "start": st, "end": en, "duration": dur}] if src_id else []
        except (ValueError, TypeError):
            return []

    if isinstance(source_ref, list):
        out: List[Dict[str, Any]] = []
        for item in source_ref:
            out.extend(parse_source_ref(item))
        return out

    if isinstance(source_ref, str):
        s = source_ref.strip()
        if not s:
            return []
        # Support JSON string encoding
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                data = json.loads(s)
                return parse_source_ref(data)
            except Exception:
                pass

        parts = [p.strip() for p in re.split(r"[,;]+", s) if p.strip()]
        res: List[Dict[str, Any]] = []
        for part in parts:
            if ":" in part:
                src_id, times = part.rsplit(":", 1)
                src_id = src_id.strip()
                if "-" in times:
                    try:
                        s_str, e_str = times.split("-", 1)
                        st = float(s_str.strip())
                        en = float(e_str.strip())
                        st, en = min(st, en), max(st, en)
                        dur = max(0.0, en - st)
                        if src_id:
                            res.append({
                                "source_id": src_id,
                                "start": st,
                                "end": en,
                                "duration": dur
                            })
                    except ValueError:
                        continue
        return res

    return []


def source_overlap_ratio(ref1: Union[str, List[Any], None], ref2: Union[str, List[Any], None]) -> float:
    """Compute clip overlap ratio between source references.
    
    Formula: max(0, min(end1, end2) - max(start1, start2)) / min(end1-start1, end2-start2).
    Returns maximum overlap ratio across matching source_ids in [0.0, 1.0].
    """
    list1 = parse_source_ref(ref1)
    list2 = parse_source_ref(ref2)
    if not list1 or not list2:
        return 0.0

    max_ratio = 0.0
    for i1 in list1:
        for i2 in list2:
            if i1["source_id"] == i2["source_id"]:
                overlap = max(0.0, min(i1["end"], i2["end"]) - max(i1["start"], i2["start"]))
                min_dur = min(i1["duration"], i2["duration"])
                if min_dur > 0:
                    ratio = overlap / min_dur
                    if ratio > max_ratio:
                        max_ratio = ratio

    return round(min(1.0, max(0.0, max_ratio)), 4)


# ============================================================================
# 5. Media Fingerprinting Pipeline & Evaluation
# ============================================================================

def fingerprint_media(video_path: str, text: str = "", source_ref: str = "") -> Dict[str, Any]:
    """Generate complete multimodal fingerprint bundle for video, text, and source reference.
    
    Returns:
      {
        "video_path": str,
        "video": dict,          # duration, k10, k50, k90, composite
        "audio": str | None,    # 128-bit hex spectral energy hash
        "text": str,            # raw script/text
        "text_clean": str,      # normalized text
        "source_ref": str,      # raw source interval ref
        "source_intervals": list# parsed interval records
      }
    """
    v_fp: Dict[str, Any] = {}
    a_fp: Optional[str] = None
    if video_path and os.path.exists(video_path):
        try:
            v_fp = compute_video_fingerprint(video_path)
        except Exception as e:
            log.debug("compute_video_fingerprint error: %s", e)
            v_fp = {}
        try:
            a_fp = compute_audio_fingerprint(video_path)
        except Exception as e:
            log.debug("compute_audio_fingerprint error: %s", e)
            a_fp = None

    return {
        "video_path": str(video_path) if video_path else "",
        "video": v_fp,
        "audio": a_fp,
        "text": text or "",
        "text_clean": clean_text(text) if text else "",
        "source_ref": source_ref or "",
        "source_intervals": parse_source_ref(source_ref) if source_ref else []
    }


def _parse_time(t: Any) -> Optional[datetime]:
    """Parse datetime from datetime object, timestamp float/int, or ISO 8601 string."""
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, timezone.utc)
    if isinstance(t, str):
        s = t.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    return dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
    return None


def evaluate_duplicate(
    candidate_fp: Dict[str, Any],
    existing_fp: Dict[str, Any],
    candidate_channel: Optional[Union[int, str]] = None,
    existing_channel: Optional[Union[int, str]] = None,
    candidate_time: Any = None,
    existing_time: Any = None
) -> Dict[str, Any]:
    """Evaluate candidate fingerprint against an existing fingerprint under DressIt rules.
    
    Rules:
      - HARD DUPLICATE (Rejection):
        video_visual_similarity >= 0.90 OR (source_clip_overlap >= 0.70 AND text_similarity >= 0.80).
        allowed = False, is_duplicate = True.
      - SOFT DUPLICATE (Warning / Cross-channel variation):
        video_visual_similarity >= 0.75 OR text_similarity >= 0.70.
        Allowed ONLY with >= 14 day cooldown OR on different channels.
        If on the same channel AND < 14 day cooldown (or same date), violation: allowed = False.
        Otherwise allowed = True, is_duplicate = True.
      - UNIQUE:
        video_visual_similarity < 0.70 AND text_similarity < 0.60 (and source overlap < 0.70).
        allowed = True, is_duplicate = False.
      - IN-BETWEEN:
        Below duplicate thresholds, allowed = True, is_duplicate = False.
        
    Returns:
      {
        "is_duplicate": bool,
        "classification": str,      # "hard_duplicate" | "soft_duplicate" | "unique" | "in_between"
        "similarity_score": float,  # Peak similarity across modalities
        "visual_similarity": float,
        "text_similarity": float,
        "audio_similarity": float | None,
        "source_overlap": float,
        "reason": str,
        "allowed": bool
      }
    """
    # 1. Visual similarity
    v1 = candidate_fp.get("video") or {}
    v2 = existing_fp.get("video") or {}
    v_sim = video_similarity(v1, v2)

    # Template outro normalization: if similarity is elevated solely by an identical outro card
    # (k90 >= 0.90) while hook (k10) and middle (k50) are distinctly different (< 0.75),
    # compute effective visual similarity from the creative content (k10 & k50).
    if v_sim >= 0.75:
        h1 = _extract_keyframe_hashes(v1)
        h2 = _extract_keyframe_hashes(v2)
        if h1 and h2:
            s10 = dhash_similarity(h1[0], h2[0])
            s50 = dhash_similarity(h1[1], h2[1])
            s90 = dhash_similarity(h1[2], h2[2])
            if s90 >= 0.90 and s10 < 0.75 and s50 < 0.75:
                v_sim = round(float((s10 + s50) / 2.0), 4)

    # 2. Text similarity
    t1 = candidate_fp.get("text", "")
    t2 = existing_fp.get("text", "")
    t_sim = text_similarity(t1, t2) if (clean_text(t1) and clean_text(t2)) else 0.0

    # 3. Audio similarity
    a1 = candidate_fp.get("audio")
    a2 = existing_fp.get("audio")
    a_sim = audio_similarity(a1, a2) if (a1 and a2) else None

    # 4. Source overlap
    s1 = candidate_fp.get("source_ref") or candidate_fp.get("source_intervals", "")
    s2 = existing_fp.get("source_ref") or existing_fp.get("source_intervals", "")
    s_overlap = source_overlap_ratio(s1, s2)

    # Composite similarity score: peak modality signal
    signals = [v_sim, t_sim, s_overlap]
    if a_sim is not None:
        signals.append(a_sim)
    similarity_score = round(max(signals), 4)

    # Channel comparison
    same_channel = True
    if candidate_channel is not None and existing_channel is not None:
        same_channel = (str(candidate_channel).strip().lower() == str(existing_channel).strip().lower())

    # Cooldown calculation
    days_diff: Optional[float] = None
    dt_cand = _parse_time(candidate_time)
    dt_exist = _parse_time(existing_time)
    if dt_cand is not None and dt_exist is not None:
        days_diff = abs((dt_cand - dt_exist).total_seconds()) / 86400.0

    # Evaluate against rules
    # Rule 1: HARD DUPLICATE (Rejection)
    is_hard = (v_sim >= 0.90) or (s_overlap >= 0.70 and t_sim >= 0.80)
    if is_hard:
        reasons = []
        if v_sim >= 0.90:
            reasons.append(f"visual similarity {v_sim:.2f} >= 0.90")
        if s_overlap >= 0.70 and t_sim >= 0.80:
            reasons.append(f"source overlap {s_overlap:.2f} >= 0.70 with text similarity {t_sim:.2f} >= 0.80")
        return {
            "is_duplicate": True,
            "classification": "hard_duplicate",
            "similarity_score": similarity_score,
            "visual_similarity": v_sim,
            "text_similarity": t_sim,
            "audio_similarity": a_sim,
            "source_overlap": s_overlap,
            "reason": f"Hard duplicate rejection: {'; '.join(reasons)}",
            "allowed": False
        }

    # Rule 2: SOFT DUPLICATE (Warning / Cross-channel variation)
    is_soft = (v_sim >= 0.75) or (t_sim >= 0.70)
    if is_soft:
        if not same_channel:
            # Cross-channel posting permitted
            return {
                "is_duplicate": True,
                "classification": "soft_duplicate",
                "similarity_score": similarity_score,
                "visual_similarity": v_sim,
                "text_similarity": t_sim,
                "audio_similarity": a_sim,
                "source_overlap": s_overlap,
                "reason": (f"Soft duplicate allowed: cross-channel variation "
                           f"(candidate: '{candidate_channel}' vs existing: '{existing_channel}')"),
                "allowed": True
            }

        # If visual_similarity < 0.75 and source_overlap < 0.50:
        # text similarity alone must NOT cause allowed = False unless text_similarity >= 0.95 (exact text duplicate).
        # Different videos with different visuals/artists must remain allowed = True.
        if v_sim < 0.75 and s_overlap < 0.50 and t_sim < 0.95:
            return {
                "is_duplicate": True,
                "classification": "soft_duplicate",
                "similarity_score": similarity_score,
                "visual_similarity": v_sim,
                "text_similarity": t_sim,
                "audio_similarity": a_sim,
                "source_overlap": s_overlap,
                "reason": (f"Soft duplicate allowed: different media (visual: {v_sim:.2f} < 0.75, "
                           f"source overlap: {s_overlap:.2f} < 0.50) with text similarity {t_sim:.2f} < 0.95"),
                "allowed": True
            }

        if days_diff is not None and days_diff >= 14.0:
            # 14-day cooldown satisfied
            return {
                "is_duplicate": True,
                "classification": "soft_duplicate",
                "similarity_score": similarity_score,
                "visual_similarity": v_sim,
                "text_similarity": t_sim,
                "audio_similarity": a_sim,
                "source_overlap": s_overlap,
                "reason": (f"Soft duplicate allowed: 14-day cooldown satisfied "
                           f"({days_diff:.1f} days elapsed >= 14 days)"),
                "allowed": True
            }

        # For unscheduled items in review (candidate_time or existing_time is None),
        # do NOT fail cooldown for soft text matches.
        if (candidate_time is None or existing_time is None) and (v_sim < 0.75 and t_sim < 0.95):
            return {
                "is_duplicate": True,
                "classification": "soft_duplicate",
                "similarity_score": similarity_score,
                "visual_similarity": v_sim,
                "text_similarity": t_sim,
                "audio_similarity": a_sim,
                "source_overlap": s_overlap,
                "reason": (f"Soft duplicate allowed: unscheduled item in review "
                           f"without cooldown date (text similarity: {t_sim:.2f})"),
                "allowed": True
            }

        # Violation: same channel within cooldown (< 14 days or same date)
        diff_text = f"{days_diff:.1f} days" if days_diff is not None else "same date / unconfigured"
        return {
            "is_duplicate": True,
            "classification": "soft_duplicate",
            "similarity_score": similarity_score,
            "visual_similarity": v_sim,
            "text_similarity": t_sim,
            "audio_similarity": a_sim,
            "source_overlap": s_overlap,
            "reason": (f"Soft duplicate violation: same channel '{candidate_channel}' "
                       f"within cooldown ({diff_text} < 14 days)"),
            "allowed": False
        }

    # Rule 3: UNIQUE
    is_unique = (v_sim < 0.70) and (t_sim < 0.60) and (s_overlap < 0.70)
    if is_unique:
        return {
            "is_duplicate": False,
            "classification": "unique",
            "similarity_score": similarity_score,
            "visual_similarity": v_sim,
            "text_similarity": t_sim,
            "audio_similarity": a_sim,
            "source_overlap": s_overlap,
            "reason": "Unique content: visual < 0.70, text < 0.60, source overlap < 0.70",
            "allowed": True
        }

    # Rule 4: IN-BETWEEN CASES
    return {
        "is_duplicate": False,
        "classification": "in_between",
        "similarity_score": similarity_score,
        "visual_similarity": v_sim,
        "text_similarity": t_sim,
        "audio_similarity": a_sim,
        "source_overlap": s_overlap,
        "reason": (f"In-between variation: below duplicate thresholds "
                   f"(visual: {v_sim:.2f}, text: {t_sim:.2f}, source overlap: {s_overlap:.2f})"),
        "allowed": True
    }


# ============================================================================
# 6. Database / Ledger Integration Helpers
# ============================================================================

def check_candidate_against_db(
    candidate_fp: Dict[str, Any],
    db_conn: Any,
    project: Optional[str] = None,
    candidate_channel: Optional[Union[int, str]] = None,
    candidate_time: Any = None
) -> List[Dict[str, Any]]:
    """Scan items table in posting.db for duplicates of candidate_fp.
    
    Returns list of duplicate evaluation findings where classification != 'unique'.
    """
    findings: List[Dict[str, Any]] = []
    try:
        # Query items with meta or source_ref
        cursor = db_conn.execute(
            "SELECT id, channel_id, title, source_ref, scheduled_at, created_at, meta FROM items"
        )
        for row in cursor.fetchall():
            row_dict = dict(row)
            meta_json = row_dict.get("meta") or "{}"
            try:
                meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
            except Exception:
                meta = {}

            existing_fp = {
                "video": meta.get("video_fingerprint") or meta.get("video") or {},
                "audio": meta.get("audio_fingerprint") or meta.get("audio"),
                "text": row_dict.get("title") or meta.get("text") or "",
                "source_ref": row_dict.get("source_ref") or meta.get("source_ref") or ""
            }

            exist_time = row_dict.get("scheduled_at") or row_dict.get("created_at")
            res = evaluate_duplicate(
                candidate_fp=candidate_fp,
                existing_fp=existing_fp,
                candidate_channel=candidate_channel,
                existing_channel=row_dict.get("channel_id"),
                candidate_time=candidate_time,
                existing_time=exist_time
            )

            if res["classification"] != "unique":
                res["existing_item_id"] = row_dict.get("id")
                findings.append(res)
    except Exception as e:
        log.warning("DB duplicate scan failed: %s", e)

    return findings


# ============================================================================
# 7. Comprehensive Selftest Suite (--selftest)
# ============================================================================

def run_selftest() -> int:
    """Execute complete unit and integration test suite with synthetic data."""
    print("=== Running dedup_engine.py Selftest Suite ===")
    test_count = 0

    def ok(name: str):
        nonlocal test_count
        test_count += 1
        print(f"  [PASS] {name}")

    # 1. dHash Tests
    # -------------------------------------------------------------
    grad = np.tile(np.linspace(0, 255, 9, dtype=np.uint8), (8, 1))
    h_grad = compute_dhash(grad)
    assert len(h_grad) == 16, f"Expected 16 hex chars, got {len(h_grad)}"
    assert h_grad == "0000000000000000", f"Expected all zeros, got {h_grad}"

    grad_inv = 255 - grad
    h_inv = compute_dhash(grad_inv)
    assert h_inv == "ffffffffffffffff", f"Expected all ones, got {h_inv}"

    assert dhash_distance(h_grad, h_grad) == 0
    assert dhash_similarity(h_grad, h_grad) == 1.0
    assert dhash_distance(h_grad, h_inv) == 64
    assert dhash_similarity(h_grad, h_inv) == 0.0

    # Small noise test
    noisy = grad.copy()
    noisy[0, 1] = 0  # 1 bit flip
    h_noisy = compute_dhash(noisy)
    assert dhash_similarity(h_grad, h_noisy) >= 0.95
    ok("1. dHash bitwise accuracy, distance, and similarity")

    # 2. Audio Fingerprint Tests (Synthetic Audio)
    # -------------------------------------------------------------
    sr = 16000
    t = np.linspace(0, 3.0, 3 * sr, endpoint=False)
    sig1 = (0.5 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    sig2 = sig1.copy()
    sig3 = (0.5 * np.sin(2 * np.pi * 2500 * t) + 0.3 * np.sin(2 * np.pi * 5000 * t)).astype(np.float32)

    afp1 = compute_audio_fingerprint(sig1)
    afp2 = compute_audio_fingerprint(sig2)
    afp3 = compute_audio_fingerprint(sig3)

    assert afp1 is not None and len(afp1) == 32, f"Expected 32 hex chars, got {afp1}"
    assert afp1 == afp2, "Identical audio signals must produce identical hashes"
    assert audio_similarity(afp1, afp2) == 1.0
    sim_diff = audio_similarity(afp1, afp3)
    assert sim_diff < 0.70, f"Expected low similarity for different frequencies, got {sim_diff}"

    # Silent signal must return None
    silent = np.zeros(16000, dtype=np.float32)
    assert compute_audio_fingerprint(silent) is None
    assert audio_similarity(None, afp1) == 0.0
    assert audio_similarity(None, None) == 0.0
    ok("2. Audio fingerprint generation, similarity, and silence handling")

    # 3. Video Pipeline Tests (Synthetic ffmpeg Videos)
    # -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        vpath1 = os.path.join(td, "test1.mp4")
        vpath2 = os.path.join(td, "test2.mp4")
        vpath_noaudio = os.path.join(td, "noaudio.mp4")

        # Create test video 1 (testsrc + 440Hz sine, 2 seconds)
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            vpath1
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Create test video 2 (testsrc2 with different colors/pattern)
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1800:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            vpath2
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Create video without audio
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:duration=1:size=160x120:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            vpath_noaudio
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Test duration
        dur1 = get_video_duration(vpath1)
        assert 1.9 <= dur1 <= 2.2, f"Expected ~2.0s duration, got {dur1}"

        # Test keyframes
        kfs = extract_keyframes(vpath1, percentages=(0.10, 0.50, 0.90))
        assert len(kfs) == 3, f"Expected 3 keyframes, got {len(kfs)}"
        for f in kfs:
            assert isinstance(f, Image.Image)

        # Test video fingerprints
        vfp1 = compute_video_fingerprint(vpath1)
        assert "k10" in vfp1 and "k50" in vfp1 and "k90" in vfp1 and "composite" in vfp1
        assert video_similarity(vfp1, vfp1) == 1.0

        vfp2 = compute_video_fingerprint(vpath2)
        v_sim = video_similarity(vfp1, vfp2)
        assert v_sim < 0.70, f"Expected low similarity between different videos, got {v_sim}"

        # Test audio extraction from video
        samples = extract_audio_samples(vpath1)
        assert samples is not None and len(samples) > 16000

        # Video without audio track
        assert extract_audio_samples(vpath_noaudio) is None
        assert compute_audio_fingerprint(vpath_noaudio) is None

        # Full fingerprint_media test
        bundle = fingerprint_media(vpath1, text="Test DressIt video! 🔥", source_ref="clip1:10.0-20.0")
        assert bundle["video"]["k10"] == vfp1["k10"]
        assert bundle["audio"] is not None
        assert bundle["text_clean"] == "test video"
        assert len(bundle["source_intervals"]) == 1

    ok("3. Synthetic video pipeline, keyframe extraction, and audio slice extraction")

    # 4. Text/Script Cleaning and Tokenization Tests
    # -------------------------------------------------------------
    raw_text = "Look at this amazing dress! 🔥👗 Check https://dressitnow.com/sale #Dress (50% OFF) --- 2026!"
    cleaned = clean_text(raw_text)
    assert cleaned == "look at this amazing dress check dress 50 off 2026", f"Got '{cleaned}'"

    tokens = tokenize(raw_text)
    assert tokens == ["look", "at", "this", "amazing", "dress", "check", "dress", "50", "off", "2026"]

    # Russian / Cyrillic text cleaning
    cyr = "Отличное платье для вечеринки! 👗 Смотри тут: https://dressit.ru/deal #Платье 2026."
    cleaned_cyr = clean_text(cyr)
    assert cleaned_cyr == "отличное платье для вечеринки смотри тут платье 2026"
    ok("4. Multilingual text cleaning, URL/emoji removal, and tokenization")

    # 5. Jaccard 3-Gram & Levenshtein Similarity Tests
    # -------------------------------------------------------------
    s1 = "The elegant summer dress is crafted from fine silk and available in limited stock."
    s2 = "The elegant summer dress is crafted from fine silk and available in limited stock."
    s3 = "The elegant summer dress is crafted from fine silk and available in limited quantity."
    s4 = "Completely different topic about automobile repair and mechanics."

    assert jaccard_3gram(s1, s2) == 1.0
    assert levenshtein_similarity(s1, s2) == 1.0
    assert text_similarity(s1, s2) == 1.0

    # Minor variation (1 word difference)
    j_sim = jaccard_3gram(s1, s3)
    l_sim = levenshtein_similarity(s1, s3)
    assert j_sim >= 0.70, f"Expected high Jaccard for near duplicate, got {j_sim}"
    assert l_sim >= 0.85, f"Expected high Levenshtein, got {l_sim}"
    assert text_similarity(s1, s3) >= 0.85

    # Completely different
    assert jaccard_3gram(s1, s4) == 0.0
    assert text_similarity(s1, s4) < 0.20

    # Short texts (< 3 words fallback)
    assert jaccard_3gram("red dress", "red dress") == 1.0
    assert jaccard_3gram("red dress", "blue shoes") < 0.30

    # Empty text edge cases
    assert jaccard_3gram("", "") == 0.0
    assert levenshtein_similarity("", "") == 0.0
    assert text_similarity("", "") == 0.0
    assert text_similarity(s1, "") == 0.0
    ok("5. Jaccard 3-gram, Levenshtein, short-text fallback, and empty cases")

    # 6. Source-Interval Tracking Tests
    # -------------------------------------------------------------
    # Parsing single reference
    p1 = parse_source_ref("oagNYHB3Kzk:29.2-38.0")
    assert len(p1) == 1
    assert p1[0]["source_id"] == "oagNYHB3Kzk"
    assert abs(p1[0]["start"] - 29.2) < 1e-4
    assert abs(p1[0]["end"] - 38.0) < 1e-4
    assert abs(p1[0]["duration"] - 8.8) < 1e-4

    # Multiple refs in one string
    p2 = parse_source_ref("srcA:10.0-20.0, srcB:30.0-40.0")
    assert len(p2) == 2

    # JSON input
    p_json = parse_source_ref('[{"source_id": "vidX", "start": 5, "end": 15}]')
    assert len(p_json) == 1 and p_json[0]["source_id"] == "vidX"

    # Overlap tests
    # Identical: 1.0
    assert source_overlap_ratio("src:10.0-20.0", "src:10.0-20.0") == 1.0

    # 50% overlap: [10-20] and [15-25] -> overlap=5.0, min_dur=10.0 -> 0.50
    assert source_overlap_ratio("src:10.0-20.0", "src:15.0-25.0") == 0.50

    # Full containment: [10-30] and [12-20] -> overlap=8.0, min_dur=8.0 -> 1.0
    assert source_overlap_ratio("src:10.0-30.0", "src:12.0-20.0") == 1.0

    # No overlap
    assert source_overlap_ratio("src:10.0-20.0", "src:25.0-35.0") == 0.0

    # Different source IDs
    assert source_overlap_ratio("srcA:10.0-20.0", "srcB:10.0-20.0") == 0.0
    ok("6. Source interval parsing and normalized overlap ratio")

    # 7. Duplicate Detection Decision Engine (evaluate_duplicate)
    # -------------------------------------------------------------
    # Case A: Hard Duplicate by Visual Similarity >= 0.90
    fp_cand = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "Unique text here",
        "source_ref": "vid1:10-20"
    }
    fp_exist_a = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000001"},  # 1 bit diff -> sim = 0.995
        "text": "Completely different text",
        "source_ref": "vid2:50-60"
    }
    res_a = evaluate_duplicate(fp_cand, fp_exist_a)
    assert res_a["is_duplicate"] is True
    assert res_a["classification"] == "hard_duplicate"
    assert res_a["allowed"] is False
    assert res_a["visual_similarity"] >= 0.90

    # Case B: Hard Duplicate by Source Overlap >= 0.70 AND Text >= 0.80
    fp_exist_b = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},  # low visual sim
        "text": "The elegant summer dress is crafted from fine silk and available in limited stock.",
        "source_ref": "vid1:11-19"   # overlap 8 / 8 = 1.0 >= 0.70
    }
    fp_cand_b = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "The elegant summer dress is crafted from fine silk and available in limited stock.",
        "source_ref": "vid1:10-20"
    }
    res_b = evaluate_duplicate(fp_cand_b, fp_exist_b)
    assert res_b["is_duplicate"] is True
    assert res_b["classification"] == "hard_duplicate"
    assert res_b["allowed"] is False
    assert res_b["source_overlap"] >= 0.70
    assert res_b["text_similarity"] >= 0.80

    # Case C: Soft Duplicate - Same channel within cooldown (< 14 days) -> Violation
    # Make visual sim = 0.80 (>= 0.75 and < 0.90)
    # 64 bits with 13 bits flipped gives distance 13 -> sim = 1.0 - 13/64 = 0.7969
    # 0x1fff has 13 bits set
    h_soft = f"{0x1fff:016x}"
    fp_exist_soft = {
        "video": {"k10": h_soft, "k50": h_soft, "k90": h_soft},
        "text": "Different text entirely",
        "source_ref": "other:0-10"
    }
    res_c = evaluate_duplicate(
        fp_cand, fp_exist_soft,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-07T00:00:00Z",
        existing_time="2026-09-02T00:00:00Z"  # 5 days ago (< 14 days)
    )
    assert res_c["is_duplicate"] is True
    assert res_c["classification"] == "soft_duplicate"
    assert res_c["allowed"] is False
    assert "violation" in res_c["reason"].lower()

    # Case D: Soft Duplicate - Different channels -> Allowed
    res_d = evaluate_duplicate(
        fp_cand, fp_exist_soft,
        candidate_channel="dressit_la",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-07T00:00:00Z",
        existing_time="2026-09-02T00:00:00Z"
    )
    assert res_d["is_duplicate"] is True
    assert res_d["classification"] == "soft_duplicate"
    assert res_d["allowed"] is True
    assert "cross-channel" in res_d["reason"].lower()

    # Case E: Soft Duplicate - Same channel, >= 14 days cooldown -> Allowed
    res_e = evaluate_duplicate(
        fp_cand, fp_exist_soft,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-25T00:00:00Z",
        existing_time="2026-09-01T00:00:00Z"  # 24 days >= 14 days
    )
    assert res_e["is_duplicate"] is True
    assert res_e["classification"] == "soft_duplicate"
    assert res_e["allowed"] is True
    assert "cooldown satisfied" in res_e["reason"].lower()

    # Case F: Unique Content (visual < 0.70, text < 0.60, source overlap < 0.70)
    fp_exist_unique = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},  # sim = 0.0
        "text": "Astronomy and space telescopes exploring distant galaxies",
        "source_ref": "unrelated:100-200"
    }
    res_f = evaluate_duplicate(
        fp_cand, fp_exist_unique,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc"
    )
    assert res_f["is_duplicate"] is False
    assert res_f["classification"] == "unique"
    assert res_f["allowed"] is True

    # Case G: In-Between Variation (e.g. visual sim 0.72)
    # 18 bits flipped -> 1.0 - 18/64 = 0.71875
    h_between = f"{0x3ffff:016x}"
    fp_exist_between = {
        "video": {"k10": h_between, "k50": h_between, "k90": h_between},
        "text": "Astronomy and space telescopes exploring distant galaxies",
        "source_ref": "unrelated:100-200"
    }
    res_g = evaluate_duplicate(
        fp_cand, fp_exist_between,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc"
    )
    assert res_g["is_duplicate"] is False
    assert res_g["classification"] == "in_between"
    assert res_g["allowed"] is True
    ok("7. Decision engine rules: hard duplicate, soft duplicate cooldown/cross-channel, unique, and in-between")

    # 8. Edge Cases and Resilience
    # -------------------------------------------------------------
    # Missing files, empty dicts, malformed inputs
    assert get_video_duration("/non/existent/path.mp4") == 0.0
    assert extract_keyframes("/non/existent/path.mp4") == []
    empty_vfp = compute_video_fingerprint("/non/existent/path.mp4")
    assert empty_vfp["duration"] == 0.0
    assert video_similarity({}, {}) == 0.0
    assert parse_source_ref(None) == []
    assert parse_source_ref("bad-format") == []
    assert source_overlap_ratio("", "") == 0.0
    assert clean_text("") == ""
    assert clean_text(None) == ""
    assert tokenize("") == []

    res_empty = evaluate_duplicate({}, {})
    assert res_empty["classification"] == "unique"
    assert res_empty["allowed"] is True
    ok("8. Edge cases, corrupt/missing paths, and empty representations handled gracefully")

    print(f"\nALL {test_count} TEST SUITES PASSED SUCCESSFULLY (100% adherence)!")
    return 0


# ============================================================================
# 8. CLI Entrypoint
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="DressIt Multimodal Deduplication Engine (Task #1955)"
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="Run comprehensive test suite and exit 0 on success"
    )
    parser.add_argument(
        "--fingerprint", type=str, metavar="VIDEO_PATH",
        help="Generate fingerprint JSON bundle for video file"
    )
    parser.add_argument(
        "--text", type=str, default="",
        help="Accompanying script or caption text for fingerprinting"
    )
    parser.add_argument(
        "--source-ref", type=str, default="",
        help="Source interval reference (e.g., 'oagNYHB3Kzk:29.2-38.0')"
    )
    parser.add_argument(
        "--compare", nargs=2, metavar=("VIDEO1", "VIDEO2"),
        help="Compare two video files and output similarity metrics"
    )

    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    if args.fingerprint:
        fp = fingerprint_media(args.fingerprint, text=args.text, source_ref=args.source_ref)
        print(json.dumps(fp, indent=2, ensure_ascii=False))
        sys.exit(0)

    if args.compare:
        v1, v2 = args.compare
        fp1 = fingerprint_media(v1)
        fp2 = fingerprint_media(v2)
        res = evaluate_duplicate(fp1, fp2)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        sys.exit(0)

    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
