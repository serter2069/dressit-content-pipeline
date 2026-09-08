#!/usr/bin/env python3
"""test_check_duplicate.py — Comprehensive Integration Tests for check_duplicate CLI and Server hooks."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import pytest

_POSTING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _POSTING_DIR not in sys.path:
    sys.path.insert(0, _POSTING_DIR)

import db
import check_duplicate


@pytest.fixture
def test_db_conn(tmp_path):
    """Fixture providing an isolated initialized SQLite database."""
    db_file = str(tmp_path / "test_posting.db")
    os.environ["POSTING_DB"] = db_file
    conn = db.get_db(db_file)
    db.seed(conn)
    yield conn
    conn.close()


def test_identical_clip_detection(test_db_conn):
    """Demonstrate detection of an identical clip -> hard duplicate, similarity 1.0."""
    channels = db.list_channels(test_db_conn)
    cid = channels[0]["id"]

    v_fp = {
        "duration": 20.0,
        "k10": "868487961e797934",
        "k50": "ef7f7f8fc1f0db7f",
        "k90": "718fced49393b323",
        "composite": "868487961e797934:ef7f7f8fc1f0db7f:718fced49393b323"
    }

    db.upsert_item(test_db_conn, "dressit-job1", channel_id=cid, title="SF Fit 1", source_ref="job1")
    db.transition_item(test_db_conn, "dressit-job1", "review")
    db.transition_item(test_db_conn, "dressit-job1", "approved")
    db.upsert_content_fingerprint(
        test_db_conn, "dressit-job1",
        video_hash=v_fp["composite"],
        video_meta=v_fp,
        audio_hash="1122334455667788",
        text_clean="sf fit 1",
        source_ref="job1"
    )

    # Candidate with identical fingerprint
    cand_fp = {
        "video_path": "/path/candidate.mp4",
        "video": v_fp,
        "audio": "1122334455667788",
        "text": "SF Fit 1",
        "text_clean": "sf fit 1",
        "source_ref": "job1",
        "source_intervals": []
    }

    eval_res = db.check_duplicate_candidate(
        test_db_conn,
        channel_id=cid,
        candidate_fp=cand_fp
    )

    assert eval_res["is_duplicate"] is True
    assert eval_res["duplicate_of"] == "dressit-job1"
    assert eval_res["classification"] == "hard_duplicate"
    assert eval_res["similarity_score"] == 1.0
    assert eval_res["allowed"] is False


def test_near_duplicate_and_soft_rules(test_db_conn):
    """Demonstrate detection of near-duplicate clip (soft duplicate cooldown vs cross-channel)."""
    channels = db.list_channels(test_db_conn)
    cid1 = channels[0]["id"]
    cid2 = channels[1]["id"]

    v_base = {
        "duration": 20.0,
        "k10": "868487961e797934",
        "k50": "ef7f7f8fc1f0db7f",
        "k90": "718fced49393b323",
        "composite": "868487961e797934:ef7f7f8fc1f0db7f:718fced49393b323"
    }

    db.upsert_item(test_db_conn, "job-base", channel_id=cid1, title="Base Concert Fit", source_ref="job1", scheduled_at="2026-09-01T10:00:00Z")
    db.transition_item(test_db_conn, "job-base", "review")
    db.transition_item(test_db_conn, "job-base", "approved")
    db.upsert_content_fingerprint(
        test_db_conn, "job-base",
        video_hash=v_base["composite"],
        video_meta=v_base,
        text_clean="base concert fit"
    )

    # Near duplicate video: sim = 0.8229 in [0.75, 0.90)
    v_near = {
        "duration": 20.0,
        "k10": "868487961e797934",
        "k50": "ef7f7f8fc1f0db7f",
        "k90": "0000000000000000",
        "composite": "868487961e797934:ef7f7f8fc1f0db7f:0000000000000000"
    }

    cand_fp = {
        "video_path": "/path/near.mp4",
        "video": v_near,
        "audio": None,
        "text": "Base Concert Fit Alternative",
        "text_clean": "base concert fit alternative",
        "source_ref": "job1_v2",
        "source_intervals": []
    }

    # Same channel, within 14 days -> violation
    res_same = db.check_duplicate_candidate(
        test_db_conn,
        channel_id=cid1,
        scheduled_at="2026-09-05T10:00:00Z",
        candidate_fp=cand_fp
    )
    assert res_same["is_duplicate"] is True
    assert res_same["classification"] == "soft_duplicate"
    assert res_same["allowed"] is False

    # Different channel -> allowed
    res_diff = db.check_duplicate_candidate(
        test_db_conn,
        channel_id=cid2,
        scheduled_at="2026-09-05T10:00:00Z",
        candidate_fp=cand_fp
    )
    assert res_diff["is_duplicate"] is True
    assert res_diff["classification"] == "soft_duplicate"
    assert res_diff["allowed"] is True


def test_unique_clip_detection(test_db_conn):
    """Demonstrate detection of unique clip -> is_duplicate = False."""
    channels = db.list_channels(test_db_conn)
    cid = channels[0]["id"]

    v_base = {
        "duration": 20.0,
        "k10": "868487961e797934",
        "k50": "ef7f7f8fc1f0db7f",
        "k90": "718fced49393b323",
        "composite": "868487961e797934:ef7f7f8fc1f0db7f:718fced49393b323"
    }
    db.upsert_item(test_db_conn, "job-base", channel_id=cid, title="Base Fit", source_ref="job1")
    db.transition_item(test_db_conn, "job-base", "review")
    db.transition_item(test_db_conn, "job-base", "approved")
    db.upsert_content_fingerprint(test_db_conn, "job-base", video_hash=v_base["composite"], video_meta=v_base)

    # Distinct candidate
    v_unique = {
        "duration": 25.0,
        "k10": "0000ffff0000ffff",
        "k50": "ffff0000ffff0000",
        "k90": "5555555555555555",
        "composite": "0000ffff0000ffff:ffff0000ffff0000:5555555555555555"
    }
    cand_unique = {
        "video_path": "/path/unique.mp4",
        "video": v_unique,
        "audio": None,
        "text": "Completely new fashion look for winter season in Milan",
        "text_clean": "completely new fashion look for winter season in milan",
        "source_ref": "unique_source_99",
        "source_intervals": []
    }
    res_unique = db.check_duplicate_candidate(
        test_db_conn,
        channel_id=cid,
        candidate_fp=cand_unique
    )
    assert res_unique["is_duplicate"] is False
    assert res_unique["duplicate_of"] is None
    assert res_unique["classification"] == "unique"
    assert res_unique["allowed"] is True


def test_cli_json_stdout_exact_invocation(tmp_path):
    """Test exact invocation of check_duplicate.py via subprocess and JSON parsing."""
    db_file = str(tmp_path / "cli_test.db")
    conn = db.get_db(db_file)
    db.seed(conn)
    conn.close()

    cmd = [
        sys.executable,
        os.path.join(_POSTING_DIR, "check_duplicate.py"),
        "--db", db_file,
        "--text", "Test script text description",
        "--channel_id", "1"
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert proc.returncode == 0, proc.stderr

    # stdout MUST be valid parseable JSON
    out_json = json.loads(proc.stdout.strip())
    assert "is_duplicate" in out_json
    assert "duplicate_of" in out_json
    assert "similarity_score" in out_json
    assert "classification" in out_json
    assert "reason" in out_json
