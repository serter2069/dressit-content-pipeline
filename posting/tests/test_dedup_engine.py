#!/usr/bin/env python3
"""test_dedup_engine.py — Comprehensive Unit Tests for DressIt Dedup Engine (Task #1955)."""

import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
import numpy as np
import pytest
from PIL import Image

import dedup_engine
from dedup_engine import (
    get_video_duration,
    extract_keyframes,
    compute_dhash,
    dhash_distance,
    dhash_similarity,
    compute_video_fingerprint,
    video_similarity,
    extract_audio_samples,
    compute_audio_fingerprint,
    audio_similarity,
    clean_text,
    tokenize,
    jaccard_3gram,
    levenshtein_similarity,
    text_similarity,
    parse_source_ref,
    source_overlap_ratio,
    fingerprint_media,
    evaluate_duplicate,
    check_candidate_against_db,
)


# ---------------------------------------------------------------------------
# Visual Fingerprinting Tests
# ---------------------------------------------------------------------------

def test_get_video_duration_nonexistent():
    assert get_video_duration("/non/existent/path/vid.mp4") == 0.0
    assert get_video_duration("") == 0.0


def test_dhash_bitwise_exact():
    # Gradient image: 9 columns, 8 rows. Row pixels are strictly increasing: col[i] < col[i+1]
    # col[i] > col[i+1] is False everywhere -> 64 zeros
    arr_zeros = np.tile(np.linspace(0, 255, 9, dtype=np.uint8), (8, 1))
    h_zeros = compute_dhash(arr_zeros)
    assert h_zeros == "0000000000000000"
    assert len(h_zeros) == 16

    # Inverted gradient: col[i] > col[i+1] is True everywhere -> 64 ones
    arr_ones = 255 - arr_zeros
    h_ones = compute_dhash(arr_ones)
    assert h_ones == "ffffffffffffffff"
    assert len(h_ones) == 16

    # Hamming distance
    assert dhash_distance(h_zeros, h_zeros) == 0
    assert dhash_distance(h_ones, h_ones) == 0
    assert dhash_distance(h_zeros, h_ones) == 64

    # Similarity
    assert dhash_similarity(h_zeros, h_zeros) == 1.0
    assert dhash_similarity(h_zeros, h_ones) == 0.0
    # 32 bits flipped -> 0.50
    h_half = "00000000ffffffff"
    assert dhash_similarity(h_zeros, h_half) == 0.50


def test_compute_dhash_input_types():
    # Test PIL Image input
    img = Image.new("RGB", (100, 100), color=(128, 64, 32))
    h1 = compute_dhash(img)
    assert isinstance(h1, str) and len(h1) == 16

    # Test numpy array input
    arr = np.zeros((100, 100, 3), dtype=np.uint8)
    h2 = compute_dhash(arr)
    assert isinstance(h2, str) and len(h2) == 16

    # Test invalid input
    with pytest.raises(TypeError):
        compute_dhash(12345)


def test_video_similarity_composite():
    fp1 = {
        "k10": "0000000000000000",
        "k50": "0000000000000000",
        "k90": "0000000000000000",
        "composite": "0000000000000000:0000000000000000:0000000000000000"
    }
    # Identical
    assert video_similarity(fp1, fp1) == 1.0

    # Slight variation in k90 (1 bit flipped out of 3 keyframes)
    # k90 sim = 1.0 - 1/64 = 0.984375
    # avg = (1.0 + 1.0 + 0.984375) / 3 = 0.9948
    fp2 = {
        "k10": "0000000000000000",
        "k50": "0000000000000000",
        "k90": "0000000000000001",
        "composite": "0000000000000000:0000000000000000:0000000000000001"
    }
    sim = video_similarity(fp1, fp2)
    assert sim >= 0.99

    # Completely different
    fp3 = {
        "k10": "ffffffffffffffff",
        "k50": "ffffffffffffffff",
        "k90": "ffffffffffffffff",
        "composite": "ffffffffffffffff:ffffffffffffffff:ffffffffffffffff"
    }
    assert video_similarity(fp1, fp3) == 0.0

    # Malformed / empty fingerprints
    assert video_similarity({}, fp1) == 0.0
    assert video_similarity(None, fp1) == 0.0


# ---------------------------------------------------------------------------
# Audio Fingerprinting Tests
# ---------------------------------------------------------------------------

def test_audio_fingerprint_synthetic():
    sr = 16000
    t = np.linspace(0, 2.0, 2 * sr, endpoint=False)
    # 440 Hz tone with harmonic
    tone1 = (0.5 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    tone2 = tone1.copy()
    # 3500 Hz tone
    tone3 = (0.5 * np.sin(2 * np.pi * 3500 * t) + 0.2 * np.sin(2 * np.pi * 7000 * t)).astype(np.float32)

    afp1 = compute_audio_fingerprint(tone1)
    afp2 = compute_audio_fingerprint(tone2)
    afp3 = compute_audio_fingerprint(tone3)

    assert afp1 is not None and len(afp1) == 32
    assert afp1 == afp2
    assert audio_similarity(afp1, afp2) == 1.0

    sim_diff = audio_similarity(afp1, afp3)
    assert sim_diff < 0.70

    # Silent signal
    silence = np.zeros(16000, dtype=np.float32)
    assert compute_audio_fingerprint(silence) is None
    assert audio_similarity(None, afp1) == 0.0
    assert audio_similarity(afp1, None) == 0.0
    assert audio_similarity(None, None) == 0.0

    # Too short audio (< 0.1s = 1600 samples)
    too_short = np.random.randn(500).astype(np.float32)
    assert compute_audio_fingerprint(too_short) is None


# ---------------------------------------------------------------------------
# Text Deduplication Tests
# ---------------------------------------------------------------------------

def test_clean_text_and_tokenize():
    raw = "🔥 Check this: https://dressitnow.com/deal?ref=123 (50% OFF) on Silk Dresses! 👗 #fashion"
    cleaned = clean_text(raw)
    assert "https" not in cleaned
    assert "dressitnow" not in cleaned
    assert "🔥" not in cleaned
    assert "👗" not in cleaned
    assert "(" not in cleaned and ")" not in cleaned and "%" not in cleaned
    assert cleaned == "check this 50 off on silk dresses fashion"

    tokens = tokenize(raw)
    assert tokens == ["check", "this", "50", "off", "on", "silk", "dresses", "fashion"]

    # Russian Cyrillic support
    cyr = "Скидки до 70% на вечерние платья! 🌟 Заходи на https://dressit.ru #стиль"
    cleaned_cyr = clean_text(cyr)
    assert cleaned_cyr == "скидки до 70 на вечерние платья заходи на стиль"


def test_jaccard_3gram_cases():
    s1 = "The new summer dress collection features exquisite natural silk with vibrant floral prints."
    s2 = "The new summer dress collection features exquisite natural silk with vibrant floral prints."
    assert jaccard_3gram(s1, s2) == 1.0

    # 1 word change
    s3 = "The new summer dress collection features exquisite natural silk with subtle floral prints."
    assert jaccard_3gram(s1, s3) >= 0.50

    # Completely different text
    s_diff = "Rocket propulsion mechanics requires precise calculations of orbital maneuvers."
    assert jaccard_3gram(s1, s_diff) == 0.0

    # Fallback for short texts (< 3 words)
    assert jaccard_3gram("red dress", "red dress") == 1.0
    assert jaccard_3gram("red dress", "blue dress") > 0.0
    assert jaccard_3gram("red dress", "green shoes") < 0.20

    # Empty inputs
    assert jaccard_3gram("", "") == 0.0
    assert jaccard_3gram(s1, "") == 0.0


def test_levenshtein_similarity_cases():
    s1 = "exclusive designer dress"
    s2 = "exclusive designer dress"
    assert levenshtein_similarity(s1, s2) == 1.0

    # 1 character typo
    s3 = "exclusive designer dresz"
    sim = levenshtein_similarity(s1, s3)
    assert sim >= 0.95

    # Empty inputs
    assert levenshtein_similarity("", "") == 0.0
    assert levenshtein_similarity(s1, "") == 0.0


def test_text_similarity_combined():
    s1 = "Exclusive summer dress made of genuine Italian silk now on sale."
    s2 = "Exclusive summer dress made of genuine Italian silk now on sale."
    assert text_similarity(s1, s2) == 1.0

    s3 = "Completely unrelated article discussing the history of ancient Rome."
    assert text_similarity(s1, s3) < 0.25

    # Empty text
    assert text_similarity("", "") == 0.0
    assert text_similarity(s1, "") == 0.0


# ---------------------------------------------------------------------------
# Source Interval Tracking Tests
# ---------------------------------------------------------------------------

def test_parse_source_ref():
    ref_str = "oagNYHB3Kzk:29.2-38.0"
    parsed = parse_source_ref(ref_str)
    assert len(parsed) == 1
    assert parsed[0]["source_id"] == "oagNYHB3Kzk"
    assert abs(parsed[0]["start"] - 29.2) < 1e-4
    assert abs(parsed[0]["end"] - 38.0) < 1e-4
    assert abs(parsed[0]["duration"] - 8.8) < 1e-4

    # Multiple items
    multi = "src1:10.5-20.5, src2:30.0-45.0"
    p_multi = parse_source_ref(multi)
    assert len(p_multi) == 2
    assert p_multi[0]["source_id"] == "src1"
    assert p_multi[1]["source_id"] == "src2"

    # Inverted start and end (robustness check)
    inv = "src:50.0-40.0"
    p_inv = parse_source_ref(inv)
    assert p_inv[0]["start"] == 40.0
    assert p_inv[0]["end"] == 50.0
    assert p_inv[0]["duration"] == 10.0

    # Empty and invalid
    assert parse_source_ref("") == []
    assert parse_source_ref("invalid_no_colon") == []
    assert parse_source_ref(None) == []


def test_source_overlap_ratio():
    # Identical: 1.0
    assert source_overlap_ratio("clipA:10.0-20.0", "clipA:10.0-20.0") == 1.0

    # 50% overlap: [10-20] and [15-25] -> overlap=5.0, min_dur=10.0 -> 0.50
    assert source_overlap_ratio("clipA:10.0-20.0", "clipA:15.0-25.0") == 0.50

    # Nested containment: [10-30] and [12-22] -> overlap=10.0, min_dur=10.0 -> 1.0
    assert source_overlap_ratio("clipA:10.0-30.0", "clipA:12.0-22.0") == 1.0

    # No overlap
    assert source_overlap_ratio("clipA:10.0-20.0", "clipA:25.0-35.0") == 0.0

    # Different source IDs
    assert source_overlap_ratio("clipA:10.0-20.0", "clipB:10.0-20.0") == 0.0

    # Empty or invalid
    assert source_overlap_ratio("", "clipA:10-20") == 0.0
    assert source_overlap_ratio(None, None) == 0.0


# ---------------------------------------------------------------------------
# Duplicate Detection Decision Engine (evaluate_duplicate)
# ---------------------------------------------------------------------------

def test_evaluate_duplicate_hard_by_visual():
    fp1 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"}}
    # 2 bits different -> sim = 1.0 - 2/64 = 0.96875 >= 0.90
    fp2 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000003"}}
    res = evaluate_duplicate(fp1, fp2)
    assert res["is_duplicate"] is True
    assert res["classification"] == "hard_duplicate"
    assert res["allowed"] is False
    assert res["visual_similarity"] >= 0.90


def test_evaluate_duplicate_hard_by_source_and_text():
    fp1 = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},
        "text": "Stunning silk evening gown for red carpet occasions.",
        "source_ref": "source_raw:10.0-20.0"
    }
    fp2 = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "Stunning silk evening gown for red carpet occasions.",
        "source_ref": "source_raw:12.0-19.0"  # 100% overlap
    }
    res = evaluate_duplicate(fp1, fp2)
    assert res["is_duplicate"] is True
    assert res["classification"] == "hard_duplicate"
    assert res["allowed"] is False
    assert res["source_overlap"] >= 0.70
    assert res["text_similarity"] >= 0.80


def test_evaluate_duplicate_soft_same_channel_cooldown_violation():
    # 14 bits flipped -> sim = 1.0 - 14/64 = 0.78125 (>= 0.75 and < 0.90)
    h_soft = f"{0x3fff:016x}"
    fp1 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"}}
    fp2 = {"video": {"k10": h_soft, "k50": h_soft, "k90": h_soft}}

    # Same channel, 3 days difference (< 14 days)
    res = evaluate_duplicate(
        fp1, fp2,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-07T12:00:00Z",
        existing_time="2026-09-04T12:00:00Z"
    )
    assert res["is_duplicate"] is True
    assert res["classification"] == "soft_duplicate"
    assert res["allowed"] is False
    assert "violation" in res["reason"].lower()


def test_evaluate_duplicate_soft_cross_channel_allowed():
    h_soft = f"{0x3fff:016x}"
    fp1 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"}}
    fp2 = {"video": {"k10": h_soft, "k50": h_soft, "k90": h_soft}}

    # Different channels, same time
    res = evaluate_duplicate(
        fp1, fp2,
        candidate_channel="dressit_la",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-07T12:00:00Z",
        existing_time="2026-09-07T12:00:00Z"
    )
    assert res["is_duplicate"] is True
    assert res["classification"] == "soft_duplicate"
    assert res["allowed"] is True
    assert "cross-channel" in res["reason"].lower()


def test_evaluate_duplicate_soft_cooldown_satisfied():
    h_soft = f"{0x3fff:016x}"
    fp1 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"}}
    fp2 = {"video": {"k10": h_soft, "k50": h_soft, "k90": h_soft}}

    # Same channel, 15 days difference (>= 14 days)
    res = evaluate_duplicate(
        fp1, fp2,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc",
        candidate_time="2026-09-20T12:00:00Z",
        existing_time="2026-09-05T12:00:00Z"
    )
    assert res["is_duplicate"] is True
    assert res["classification"] == "soft_duplicate"
    assert res["allowed"] is True
    assert "cooldown satisfied" in res["reason"].lower()


def test_evaluate_duplicate_unique():
    fp1 = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "Stunning silk dress with summer floral patterns.",
        "source_ref": "dress_clip_1:10-20"
    }
    fp2 = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},
        "text": "Space exploration rocket launched successfully from Cape Canaveral.",
        "source_ref": "nasa_feed:100-200"
    }
    res = evaluate_duplicate(fp1, fp2)
    assert res["is_duplicate"] is False
    assert res["classification"] == "unique"
    assert res["allowed"] is True


def test_evaluate_duplicate_in_between():
    # 18 bits flipped -> 1.0 - 18/64 = 0.71875 (< 0.75, but >= 0.70)
    h_between = f"{0x3ffff:016x}"
    fp1 = {"video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"}}
    fp2 = {
        "video": {"k10": h_between, "k50": h_between, "k90": h_between},
        "text": "Unrelated topic",
        "source_ref": "different:0-10"
    }
    res = evaluate_duplicate(fp1, fp2)
    assert res["is_duplicate"] is False
    assert res["classification"] == "in_between"
    assert res["allowed"] is True


# ---------------------------------------------------------------------------
# Synthetic Video Pipeline End-to-End
# ---------------------------------------------------------------------------

def test_synthetic_video_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        vpath = os.path.join(td, "synthetic.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            vpath
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        dur = get_video_duration(vpath)
        assert 1.9 <= dur <= 2.2

        kfs = extract_keyframes(vpath)
        assert len(kfs) == 3

        vfp = compute_video_fingerprint(vpath)
        assert "composite" in vfp
        assert vfp["k10"] != "0" * 16

        bundle = fingerprint_media(vpath, text="DressIt Summer Collection", source_ref="oagNYHB3Kzk:10.0-20.0")
        assert bundle["video_path"] == vpath
        assert bundle["video"]["composite"] == vfp["composite"]
        assert bundle["audio"] is not None and len(bundle["audio"]) == 32
        assert bundle["text_clean"] == "summer collection"
        assert len(bundle["source_intervals"]) == 1


# ---------------------------------------------------------------------------
# Database Integration Tests
# ---------------------------------------------------------------------------

def test_check_candidate_against_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE items (
        id TEXT PRIMARY KEY,
        channel_id INTEGER,
        title TEXT,
        source_ref TEXT,
        scheduled_at TEXT,
        created_at TEXT,
        meta TEXT
    )
    """)

    # Insert an existing item
    existing_meta = json.dumps({
        "video_fingerprint": {
            "k10": "0000000000000000",
            "k50": "0000000000000000",
            "k90": "0000000000000000",
            "composite": "0000000000000000:0000000000000000:0000000000000000"
        }
    })
    conn.execute("""
    INSERT INTO items (id, channel_id, title, source_ref, scheduled_at, created_at, meta)
    VALUES ('item_1', 1, 'Elegant Silk Dress', 'source_clip:10-20', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', ?)
    """, (existing_meta,))

    # Candidate with nearly identical video on same channel (violating duplicate)
    cand_fp = {
        "video": {
            "k10": "0000000000000000",
            "k50": "0000000000000000",
            "k90": "0000000000000001",
            "composite": "0000000000000000:0000000000000000:0000000000000001"
        },
        "text": "Different text",
        "source_ref": "other:0-10"
    }

    findings = check_candidate_against_db(
        candidate_fp=cand_fp,
        db_conn=conn,
        candidate_channel=1,
        candidate_time="2026-09-05T00:00:00Z"
    )

    assert len(findings) == 1
    assert findings[0]["existing_item_id"] == "item_1"
    assert findings[0]["classification"] == "hard_duplicate"
    assert findings[0]["allowed"] is False


# ---------------------------------------------------------------------------
# Boilerplate Stripping & Soft Duplicate Refinement Tests (Proto Task #2014)
# ---------------------------------------------------------------------------

def test_clean_text_boilerplate_stripping():
    """Verify clean_text strips channel branding and boilerplate patterns."""
    cases = [
        ("! Free Concert Fits on DressIt #Shorts", ""),
        ("Free Concert Fits on DressIt", ""),
        ("Free Concert Fits", ""),
        ("#shorts", ""),
        ("#Shorts", ""),
        ("dressitnow.com", ""),
        ("dressit", ""),
        ("· DressIt", ""),
        ("RELATIONSHIPS ·", ""),
        ("Ken Carson at Bill Graham Civic Auditorium! Free Concert Fits on DressIt #Shorts",
         "ken carson at bill graham civic auditorium"),
        ("Madeon at Bill Graham Civic Auditorium! Free Concert Fits on DressIt #Shorts",
         "madeon at bill graham civic auditorium"),
        ("RELATIONSHIPS · Signs you are in love · DressIt #Shorts",
         "signs you are in love"),
    ]
    for raw, expected in cases:
        assert clean_text(raw) == expected, f"Failed for raw={raw!r}: got {clean_text(raw)!r}, expected {expected!r}"


def test_different_concert_videos_same_venue_not_disallowed_duplicate():
    """Verify two different concert videos at the same venue are NOT flagged as disallowed duplicates."""
    cand_title = "Ken Carson at Bill Graham Civic Auditorium! Free Concert Fits on DressIt #Shorts"
    exist_title = "Madeon at Bill Graham Civic Auditorium! Free Concert Fits on DressIt #Shorts"

    # Distinct visual fingerprints (different artists/performances)
    fp_cand = {
        "video": {
            "k10": "0000000000000000",
            "k50": "0000000000000000",
            "k90": "0000000000000000",
            "composite": "0000000000000000:0000000000000000:0000000000000000"
        },
        "text": cand_title,
        "source_ref": "clip_ken:0-15",
        "source_intervals": []
    }
    fp_exist = {
        "video": {
            "k10": "ffffffffffffffff",
            "k50": "ffffffffffffffff",
            "k90": "ffffffffffffffff",
            "composite": "ffffffffffffffff:ffffffffffffffff:ffffffffffffffff"
        },
        "text": exist_title,
        "source_ref": "clip_madeon:0-15",
        "source_intervals": []
    }

    # Case 1: Unscheduled items in review (candidate_time=None, existing_time=None)
    res_unscheduled = evaluate_duplicate(
        fp_cand, fp_exist,
        candidate_channel="dressit_sf",
        existing_channel="dressit_sf",
        candidate_time=None,
        existing_time=None
    )
    assert res_unscheduled["allowed"] is True
    assert not (res_unscheduled["is_duplicate"] and not res_unscheduled["allowed"])

    # Case 2: Scheduled within 14-day window (e.g. 1 day apart)
    res_scheduled = evaluate_duplicate(
        fp_cand, fp_exist,
        candidate_channel="dressit_sf",
        existing_channel="dressit_sf",
        candidate_time="2026-09-08T12:00:00Z",
        existing_time="2026-09-07T12:00:00Z"
    )
    assert res_scheduled["allowed"] is True
    assert not (res_scheduled["is_duplicate"] and not res_scheduled["allowed"])


def test_soft_text_match_unscheduled_items_allowed():
    """Verify that when candidate_time or existing_time is None, soft text matches do NOT fail cooldown."""
    fp_cand = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "Artist A Performing Live at Madison Square Garden Special Stage",
        "source_ref": "srcA:0-10"
    }
    fp_exist = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},
        "text": "Artist B Performing Live at Madison Square Garden Special Stage",
        "source_ref": "srcB:0-10"
    }

    res = evaluate_duplicate(
        fp_cand, fp_exist,
        candidate_channel="dressit_nyc",
        existing_channel="dressit_nyc",
        candidate_time=None,
        existing_time=None
    )
    assert res["allowed"] is True


def test_text_similarity_alone_does_not_block_different_media():
    """Verify that if visual_similarity < 0.75 and source_overlap < 0.50, text similarity alone (< 0.95)
    must NOT cause allowed = False even within cooldown on same channel."""
    fp_cand = {
        "video": {"k10": "0000000000000000", "k50": "0000000000000000", "k90": "0000000000000000"},
        "text": "The Greatest Show on Earth Live from the Grand Ballroom Tonight",
        "source_ref": "media1:0-10"
    }
    fp_exist = {
        "video": {"k10": "ffffffffffffffff", "k50": "ffffffffffffffff", "k90": "ffffffffffffffff"},
        "text": "The Greatest Show on Earth Live from the Grand Ballroom Friday",
        "source_ref": "media2:0-10"
    }

    res = evaluate_duplicate(
        fp_cand, fp_exist,
        candidate_channel="dressit_main",
        existing_channel="dressit_main",
        candidate_time="2026-09-08T10:00:00Z",
        existing_time="2026-09-07T10:00:00Z"  # 1 day diff (< 14 days)
    )
    assert res["visual_similarity"] < 0.75
    assert res["source_overlap"] < 0.50
    assert res["text_similarity"] >= 0.70
    assert res["text_similarity"] < 0.95
    assert res["allowed"] is True


def test_template_outro_does_not_cause_false_positive_duplicate():
    """Verify that two videos with distinct hook (k10) and middle (k50) are NOT flagged as duplicates
    even if both use the exact same template outro card (k90 identical)."""
    # Two videos with distinct body (~0.64 keyframe similarity) but identical outro card
    fp_cand = {
        "video": {
            "k10": "5b6501000660ec0d",
            "k50": "0008400000161181",
            "k90": "70703c7d6dcbaa73",
            "composite": "5b6501000660ec0d:0008400000161181:70703c7d6dcbaa73"
        },
        "text": "Steve Aoki Live at Bill Graham Civic Auditorium",
        "source_ref": "video_steve:0-15"
    }
    fp_exist = {
        "video": {
            "k10": "010000a01bc46c23",
            "k50": "83010200c1a08a84",
            "k90": "70703c7d6dcbaa73",
            "composite": "010000a01bc46c23:83010200c1a08a84:70703c7d6dcbaa73"
        },
        "text": "KAYTRANADA at Greek Theatre",
        "source_ref": "video_kay:0-15"
    }

    res = evaluate_duplicate(
        fp_cand, fp_exist,
        candidate_channel=5,
        existing_channel=5,
        candidate_time="2026-09-08T12:00:00Z",
        existing_time="2026-09-08T10:00:00Z"
    )
    # Raw average would be 0.7656, but normalized effective visual similarity is ~0.648
    assert res["visual_similarity"] < 0.75
    assert res["is_duplicate"] is False or res["allowed"] is True
    assert res["allowed"] is True


