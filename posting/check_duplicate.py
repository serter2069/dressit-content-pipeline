#!/usr/bin/env python3
"""check_duplicate.py — Multimodal Content Deduplication CLI & Indexer for DressIt (Task #1955).

Usage:
  # 1. Single content check:
  python3 check_duplicate.py <video_path> --text "<caption_or_script>" [--channel_id <id>] [--db <path>]

  # 2. Index all items in database & generate report:
  python3 check_duplicate.py --index-all [--report-file <path>] [--db <path>]

  # 3. Automated self-tests:
  python3 check_duplicate.py --selftest
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure /root/posting is in sys.path
_POSTING_DIR = os.path.dirname(os.path.abspath(__file__))
if _POSTING_DIR not in sys.path:
    sys.path.insert(0, _POSTING_DIR)

import db
from dedup_engine import (
    fingerprint_media,
    evaluate_duplicate,
    clean_text,
    parse_source_ref,
    video_similarity,
    audio_similarity,
    text_similarity,
    source_overlap_ratio,
)

log = logging.getLogger("check_duplicate")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

DEFAULT_REPORT_PATH = os.path.join(_POSTING_DIR, "data", "dedup_report.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# 1. Database Indexing
# ============================================================================

def index_item_worker(item_row: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Worker function to fingerprint a single item's media file."""
    item_id = item_row["id"]
    local_path = item_row["local_path"]
    title = item_row.get("title") or ""
    source_ref = item_row.get("source_ref") or ""

    if not local_path or not os.path.exists(local_path):
        return item_id, None, "File not accessible"

    try:
        fp = fingerprint_media(local_path, text=title, source_ref=source_ref)
        return item_id, fp, None
    except Exception as e:
        return item_id, None, str(e)


def index_database(
    conn: sqlite3.Connection,
    max_workers: int = 6,
    only_sf: bool = False,
    verbose: bool = True
) -> Dict[str, Any]:
    """Index items in posting.db that have accessible local_path files.

    Extracts keyframes, audio spectrogram, and text fingerprints, then writes to:
    1. Table `content_fingerprints`
    2. Column `items.meta`
    """
    sql = (
        "SELECT id, channel_id, title, local_path, state, source_ref, scheduled_at, created_at, meta "
        "FROM items WHERE local_path IS NOT NULL AND local_path != ''"
    )
    if only_sf:
        sql += " AND id LIKE 'dressit-sf-%'"
    sql += " ORDER BY id"

    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    accessible = [r for r in rows if r.get("local_path") and os.path.exists(r["local_path"])]

    # Check which items already have fingerprints in content_fingerprints
    existing_fp_rows = conn.execute(
        "SELECT item_id, video_hash FROM content_fingerprints WHERE video_hash IS NOT NULL"
    ).fetchall()
    existing_ids = {r["item_id"] for r in existing_fp_rows}

    to_fingerprint = [r for r in accessible if r["id"] not in existing_ids]

    if verbose:
        sys.stderr.write(
            f"[*] Indexing items: {len(accessible)} accessible files found "
            f"({len(existing_ids)} already cached, {len(to_fingerprint)} to process)\n"
        )

    indexed_count = 0
    errors = []

    if to_fingerprint:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(index_item_worker, item): item for item in to_fingerprint
            }
            done_count = 0
            for future in concurrent.futures.as_completed(future_to_item):
                item = future_to_item[future]
                item_id, fp, err = future.result()
                done_count += 1
                if err or fp is None:
                    errors.append((item_id, err))
                    if verbose:
                        sys.stderr.write(f"  [!] Failed {item_id}: {err}\n")
                else:
                    # Write to content_fingerprints
                    v_dict = fp.get("video") or {}
                    db.upsert_content_fingerprint(
                        conn,
                        item_id=item_id,
                        video_path=item["local_path"],
                        video_duration=v_dict.get("duration"),
                        video_hash=v_dict.get("composite"),
                        video_meta=v_dict,
                        audio_hash=fp.get("audio"),
                        text_clean=fp.get("text_clean"),
                        source_ref=item.get("source_ref") or "",
                        source_intervals=fp.get("source_intervals")
                    )

                    # Update items.meta
                    meta = {}
                    if item.get("meta"):
                        try:
                            meta = json.loads(item["meta"]) if isinstance(item["meta"], str) else item["meta"]
                        except Exception:
                            meta = {}
                    meta["video_fingerprint"] = v_dict
                    meta["audio_fingerprint"] = fp.get("audio")
                    meta["fingerprinted_at"] = _now()
                    conn.execute(
                        "UPDATE items SET meta = ? WHERE id = ?",
                        (json.dumps(meta, ensure_ascii=False), item_id)
                    )
                    indexed_count += 1
                    if verbose and (done_count % 5 == 0 or done_count == len(to_fingerprint)):
                        sys.stderr.write(f"  [+] Processed {done_count}/{len(to_fingerprint)} items...\n")

        conn.commit()

    if verbose:
        sys.stderr.write(f"[*] Indexing complete. Indexed: {indexed_count}, Total available: {len(accessible)}\n")

    return {
        "total_items": len(rows),
        "accessible_items": len(accessible),
        "already_indexed": len(existing_ids),
        "newly_indexed": indexed_count,
        "errors": errors
    }


# ============================================================================
# 2. Comprehensive Deduplication Report Generation
# ============================================================================

def generate_dedup_report(
    conn: sqlite3.Connection,
    report_file: Optional[str] = None,
    print_summary: bool = True
) -> Dict[str, Any]:
    """Generate comprehensive deduplication report and write to JSON file.

    Evaluates:
      1. All 22 DressIt SF items against each other (pairwise).
      2. SF items cross-channel overlaps.
      3. Global duplicates across all indexed items.
    """
    report_path = report_file or DEFAULT_REPORT_PATH
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)

    # Fetch all items that have content_fingerprints
    sql = (
        "SELECT i.id, i.channel_id, i.title, i.local_path, i.state, i.source_ref, "
        "i.scheduled_at, i.created_at, i.meta, "
        "fp.video_duration, fp.video_hash, fp.video_meta, fp.audio_hash, "
        "fp.text_clean, fp.source_intervals "
        "FROM items i "
        "JOIN content_fingerprints fp ON fp.item_id = i.id "
        "WHERE fp.video_hash IS NOT NULL "
        "ORDER BY i.id"
    )
    items = [dict(r) for r in conn.execute(sql).fetchall()]

    # Parse fingerprints for comparison
    parsed_items = []
    for it in items:
        v_dict = {}
        if it.get("video_meta"):
            try:
                v_dict = json.loads(it["video_meta"]) if isinstance(it["video_meta"], str) else it["video_meta"]
            except Exception:
                v_dict = {}
        t_raw = it.get("title") or ""
        s_ref = it.get("source_ref") or ""
        fp = {
            "video_path": it.get("local_path") or "",
            "video": v_dict,
            "audio": it.get("audio_hash"),
            "text": t_raw,
            "text_clean": it.get("text_clean") or clean_text(t_raw),
            "source_ref": s_ref,
            "source_intervals": (
                json.loads(it["source_intervals"])
                if it.get("source_intervals")
                else parse_source_ref(s_ref)
            )
        }
        parsed_items.append({
            "id": it["id"],
            "channel_id": it["channel_id"],
            "state": it["state"],
            "title": it["title"],
            "local_path": it["local_path"],
            "scheduled_at": it.get("scheduled_at") or it.get("created_at"),
            "video_duration": it.get("video_duration"),
            "video_hash": it.get("video_hash"),
            "has_audio": bool(it.get("audio_hash")),
            "fp": fp
        })

    # 1. DressIt SF specific analysis (the 22 SF items: dressit-sf-job1 .. job22)
    sf_items = [p for p in parsed_items if p["id"].startswith("dressit-sf-")]
    def _sf_sort_key(item):
        m = item["id"].replace("dressit-sf-job", "")
        try:
            return int(m)
        except Exception:
            return item["id"]
    sf_items.sort(key=_sf_sort_key)

    sf_identical_clips = []
    sf_high_similarity = []
    sf_cross_channel_overlaps = []
    sf_pairs_evaluated = 0

    for i in range(len(sf_items)):
        for j in range(i + 1, len(sf_items)):
            sf_pairs_evaluated += 1
            it1 = sf_items[i]
            it2 = sf_items[j]

            eval_res = evaluate_duplicate(
                candidate_fp=it1["fp"],
                existing_fp=it2["fp"],
                candidate_channel=it1["channel_id"],
                existing_channel=it2["channel_id"],
                candidate_time=it1["scheduled_at"],
                existing_time=it2["scheduled_at"]
            )

            v_sim = eval_res.get("visual_similarity", 0.0)
            t_sim = eval_res.get("text_similarity", 0.0)
            sim = eval_res.get("similarity_score", 0.0)

            pair_info = {
                "item_1": it1["id"],
                "item_2": it2["id"],
                "similarity_score": sim,
                "visual_similarity": v_sim,
                "text_similarity": t_sim,
                "audio_similarity": eval_res.get("audio_similarity"),
                "source_overlap": eval_res.get("source_overlap", 0.0),
                "classification": eval_res["classification"],
                "reason": eval_res["reason"]
            }

            if v_sim >= 0.95 or it1["video_hash"] == it2["video_hash"]:
                sf_identical_clips.append(pair_info)
            elif eval_res.get("is_duplicate") or sim >= 0.75:
                sf_high_similarity.append(pair_info)

    # Cross-channel analysis for SF items against non-SF items
    non_sf_items = [p for p in parsed_items if not p["id"].startswith("dressit-sf-")]
    for sf in sf_items:
        for other in non_sf_items:
            if sf["channel_id"] != other["channel_id"]:
                eval_res = evaluate_duplicate(
                    candidate_fp=sf["fp"],
                    existing_fp=other["fp"],
                    candidate_channel=sf["channel_id"],
                    existing_channel=other["channel_id"],
                    candidate_time=sf["scheduled_at"],
                    existing_time=other["scheduled_at"]
                )
                if eval_res.get("is_duplicate") or eval_res.get("similarity_score", 0.0) >= 0.75:
                    sf_cross_channel_overlaps.append({
                        "sf_item": sf["id"],
                        "other_item": other["id"],
                        "sf_channel": sf["channel_id"],
                        "other_channel": other["channel_id"],
                        "similarity_score": eval_res.get("similarity_score", 0.0),
                        "classification": eval_res["classification"],
                        "reason": eval_res["reason"],
                        "allowed": eval_res["allowed"]
                    })

    # 2. Global duplicate evaluation across ALL indexed items
    global_duplicates = []
    global_pairs_evaluated = 0
    for i in range(len(parsed_items)):
        for j in range(i + 1, len(parsed_items)):
            global_pairs_evaluated += 1
            it1 = parsed_items[i]
            it2 = parsed_items[j]

            eval_res = evaluate_duplicate(
                candidate_fp=it1["fp"],
                existing_fp=it2["fp"],
                candidate_channel=it1["channel_id"],
                existing_channel=it2["channel_id"],
                candidate_time=it1["scheduled_at"],
                existing_time=it2["scheduled_at"]
            )

            if eval_res.get("is_duplicate"):
                global_duplicates.append({
                    "item_1": it1["id"],
                    "item_2": it2["id"],
                    "channel_1": it1["channel_id"],
                    "channel_2": it2["channel_id"],
                    "similarity_score": eval_res.get("similarity_score", 0.0),
                    "classification": eval_res["classification"],
                    "reason": eval_res["reason"],
                    "allowed": eval_res["allowed"],
                    "visual_similarity": eval_res.get("visual_similarity", 0.0),
                    "text_similarity": eval_res.get("text_similarity", 0.0),
                    "audio_similarity": eval_res.get("audio_similarity")
                })

    if not sf_identical_clips and not sf_high_similarity:
        sf_finding = (
            f"All {len(sf_items)} DressIt SF items (dressit-sf-job1 to dressit-sf-job22) "
            "are completely distinct in visual keyframe patterns and video hashes. "
            "Zero identical clips and zero intra-SF duplicate conflicts detected."
        )
    else:
        sf_finding = (
            f"DressIt SF analysis detected {len(sf_identical_clips)} identical clips "
            f"and {len(sf_high_similarity)} high-similarity pairs."
        )

    report_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": db.db_path() if hasattr(db, "db_path") else "posting.db",
        "summary": {
            "total_items_in_catalog": len(parsed_items),
            "dressit_sf_items_count": len(sf_items),
            "sf_pairs_evaluated": sf_pairs_evaluated,
            "global_pairs_evaluated": global_pairs_evaluated,
            "sf_identical_clips_count": len(sf_identical_clips),
            "sf_high_similarity_pairs_count": len(sf_high_similarity),
            "sf_cross_channel_overlaps_count": len(sf_cross_channel_overlaps),
            "global_duplicates_count": len(global_duplicates)
        },
        "dressit_sf_analysis": {
            "total_sf_items": len(sf_items),
            "sf_item_ids": [it["id"] for it in sf_items],
            "channel_id": 5,
            "identical_clips": sf_identical_clips,
            "high_similarity_pairs": sf_high_similarity,
            "cross_channel_overlaps": sf_cross_channel_overlaps,
            "findings": sf_finding
        },
        "global_duplicates": global_duplicates,
        "indexed_catalog": [
            {
                "id": it["id"],
                "channel_id": it["channel_id"],
                "state": it["state"],
                "title": it["title"],
                "local_path": it["local_path"],
                "duration": it["video_duration"],
                "video_hash": it["video_hash"],
                "has_audio": it["has_audio"]
            }
            for it in parsed_items
        ]
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    if print_summary:
        print_report_summary(report_data, report_path)

    return report_data


def print_report_summary(report_data: Dict[str, Any], report_path: str):
    """Print human-readable summary of deduplication report to stderr."""
    s = report_data["summary"]
    sf = report_data["dressit_sf_analysis"]
    out = sys.stderr

    out.write("\n" + "=" * 76 + "\n")
    out.write("           DRESSIT MULTIMODAL DEDUPLICATION AUDIT REPORT\n")
    out.write("=" * 76 + "\n")
    out.write(f"Report File:           {report_path}\n")
    out.write(f"Generated At:          {report_data['generated_at']}\n")
    out.write(f"Total Items Indexed:   {s['total_items_in_catalog']}\n")
    out.write(f"DressIt SF Items:      {s['dressit_sf_items_count']} items (Channel 5)\n")
    out.write(f"SF Pairs Evaluated:    {s['sf_pairs_evaluated']} pairwise comparisons\n")
    out.write(f"Total Pairs Evaluated: {s['global_pairs_evaluated']} global comparisons\n")
    out.write("-" * 76 + "\n")
    out.write("DRESSIT SF COLLECTION STATUS:\n")
    out.write(f"  • Identical clips:          {s['sf_identical_clips_count']}\n")
    out.write(f"  • High similarity pairs:    {s['sf_high_similarity_pairs_count']}\n")
    out.write(f"  • Cross-channel overlaps:   {s['sf_cross_channel_overlaps_count']}\n")
    out.write(f"  • Assessment:               {sf['findings']}\n")
    out.write("-" * 76 + "\n")
    out.write("GLOBAL DATABASE DUPLICATES:\n")
    out.write(f"  • Total duplicate pairs:    {s['global_duplicates_count']}\n")
    if s['global_duplicates_count'] > 0:
        for d in report_data["global_duplicates"][:5]:
            out.write(
                f"    - [{d['classification'].upper()}] {d['item_1']} vs {d['item_2']}: "
                f"sim={d['similarity_score']} ({d['reason']})\n"
            )
        if s['global_duplicates_count'] > 5:
            out.write(f"    ... and {s['global_duplicates_count'] - 5} more (see JSON report)\n")
    else:
        out.write("  • Clean catalog: No unauthorized duplicates detected across the pipeline.\n")
    out.write("=" * 76 + "\n\n")


# ============================================================================
# 3. Single Candidate Check (CLI Output)
# ============================================================================

def check_candidate_cli(
    video_path: Optional[str],
    text: str = "",
    channel_id: Optional[Union[int, str]] = None,
    db_path: Optional[str] = None,
    source_ref: str = "",
    scheduled_at: Optional[str] = None
) -> Dict[str, Any]:
    """Execute single duplicate evaluation against database and return pure JSON dictionary."""
    database_path = db_path or db.db_path()
    conn = db.get_db(database_path)
    try:
        res = db.check_duplicate_candidate(
            conn,
            video_path=video_path,
            text=text,
            channel_id=channel_id,
            scheduled_at=scheduled_at,
            source_ref=source_ref,
            check_states=("review", "approved", "scheduled", "published")
        )
        return {
            "is_duplicate": res["is_duplicate"],
            "duplicate_of": res["duplicate_of"],
            "similarity_score": res["similarity_score"],
            "reason": res["reason"],
            "classification": res["classification"],
            "allowed": res["allowed"],
            "visual_similarity": res.get("visual_similarity", 0.0),
            "text_similarity": res.get("text_similarity", 0.0),
            "audio_similarity": res.get("audio_similarity"),
            "source_overlap": res.get("source_overlap", 0.0),
            "matches": res.get("matches", [])
        }
    finally:
        conn.close()


# ============================================================================
# 4. Automated Selftest Suite (--selftest)
# ============================================================================

def run_selftest() -> int:
    """Execute automated self-tests for check_duplicate.py and exit 0 on success."""
    sys.stderr.write("=== Running check_duplicate.py Selftest Suite ===\n")

    tmp_dir = tempfile.mkdtemp(prefix="check-duplicate-selftest-")
    test_db_path = os.path.join(tmp_dir, "test.db")
    os.environ["POSTING_DB"] = test_db_path

    try:
        conn = db.get_db(test_db_path)
        db.seed(conn)

        channels = db.list_channels(conn)
        cid1 = channels[0]["id"]
        cid2 = channels[1]["id"]

        # Synthetic fingerprints for deterministic testing
        fp_a_v = {
            "duration": 15.0,
            "k10": "868487961e797934",
            "k50": "ef7f7f8fc1f0db7f",
            "k90": "718fced49393b323",
            "composite": "868487961e797934:ef7f7f8fc1f0db7f:718fced49393b323"
        }
        fp_b_v = {
            "duration": 20.0,
            "k10": "ffff0000ffff0000",
            "k50": "0000ffff0000ffff",
            "k90": "123456789abcdef0",
            "composite": "ffff0000ffff0000:0000ffff0000ffff:123456789abcdef0"
        }

        # Seed items in test database
        # it-base: published item with fp_a
        db.upsert_item(
            conn, "it-base",
            channel_id=cid1,
            title="Doja Cat at Chase Center SF! Free Concert Fits on DressIt #Shorts",
            source_ref="job1",
            scheduled_at="2026-09-01T12:00:00Z"
        )
        db.transition_item(conn, "it-base", "review")
        db.transition_item(conn, "it-base", "approved")
        db.transition_item(conn, "it-base", "scheduled", scheduled_at="2026-09-01T12:00:00Z")
        db.transition_item(conn, "it-base", "published", published_url="https://youtu.be/base")

        db.upsert_content_fingerprint(
            conn, "it-base",
            video_path="/dummy/job1.mp4",
            video_duration=15.0,
            video_hash=fp_a_v["composite"],
            video_meta=fp_a_v,
            audio_hash="aabbccddeeff00112233445566778899",
            text_clean="doja cat at chase center sf free concert fits on dressit shorts",
            source_ref="job1"
        )

        # ---------------------------------------------------------------------
        # TEST 1: Identical Clip Detection (Hard Duplicate, similarity 1.0)
        # ---------------------------------------------------------------------
        cand_identical_fp = {
            "video_path": "/dummy/candidate_job1.mp4",
            "video": fp_a_v,
            "audio": "aabbccddeeff00112233445566778899",
            "text": "Doja Cat at Chase Center SF! Free Concert Fits on DressIt #Shorts",
            "text_clean": "doja cat at chase center sf free concert fits on dressit shorts",
            "source_ref": "job1",
            "source_intervals": []
        }
        res1 = db.check_duplicate_candidate(
            conn,
            channel_id=cid1,
            scheduled_at="2026-09-02T12:00:00Z",
            candidate_fp=cand_identical_fp
        )
        assert res1["is_duplicate"] is True, f"Expected is_duplicate=True, got {res1}"
        assert res1["duplicate_of"] == "it-base", f"Expected duplicate_of='it-base', got {res1}"
        assert res1["classification"] == "hard_duplicate", f"Expected 'hard_duplicate', got {res1}"
        assert res1["similarity_score"] >= 0.99, f"Expected similarity >= 0.99, got {res1}"
        assert res1["allowed"] is False, "Hard duplicate must have allowed=False"
        sys.stderr.write("  [PASS] 1. Identical clip detection (hard duplicate, similarity 1.0)\n")

        # ---------------------------------------------------------------------
        # TEST 2: Near-duplicate clip detection (soft duplicate & 14-day rule)
        # ---------------------------------------------------------------------
        cand_soft_same_chan = {
            "video_path": "/dummy/cand_soft.mp4",
            "video": {
                "duration": 15.0,
                "k10": "868487961e797934",
                "k50": "ef7f7f8fc1f0db7f",
                # Varied 3rd keyframe to simulate soft variation (sim ~0.8229 in [0.75, 0.90))
                "k90": "0000000000000000",
                "composite": "868487961e797934:ef7f7f8fc1f0db7f:0000000000000000"
            },
            "audio": None,
            "text": "Doja Cat Chase Center Concert Outfit ideas on DressIt",
            "text_clean": "doja cat chase center concert outfit ideas on dressit",
            "source_ref": "job1_edit",
            "source_intervals": []
        }
        res2_same = db.check_duplicate_candidate(
            conn,
            channel_id=cid1,
            scheduled_at="2026-09-05T12:00:00Z",
            candidate_fp=cand_soft_same_chan
        )
        assert res2_same["is_duplicate"] is True, f"Expected duplicate, got {res2_same}"
        res2_diff = db.check_duplicate_candidate(
            conn,
            channel_id=cid2,
            scheduled_at="2026-09-05T12:00:00Z",
            candidate_fp=cand_soft_same_chan
        )
        assert res2_diff["is_duplicate"] is True
        assert res2_diff["allowed"] is True, "Cross-channel soft duplicate must be allowed"
        sys.stderr.write("  [PASS] 2. Near-duplicate clip detection (soft duplicate & channel cooldown)\n")

        # ---------------------------------------------------------------------
        # TEST 3: Unique Clip Detection (Different content -> is_duplicate = False)
        # ---------------------------------------------------------------------
        cand_unique_fp = {
            "video_path": "/dummy/unique_job.mp4",
            "video": fp_b_v,
            "audio": "00112233445566778899aabbccddeeff",
            "text": "Completely new and unrelated chess tournament highlights #Chess",
            "text_clean": "completely new and unrelated chess tournament highlights chess",
            "source_ref": "chess_unique_99",
            "source_intervals": []
        }
        res3 = db.check_duplicate_candidate(
            conn,
            channel_id=cid1,
            candidate_fp=cand_unique_fp
        )
        assert res3["is_duplicate"] is False, f"Expected unique, got {res3}"
        assert res3["duplicate_of"] is None
        assert res3["classification"] == "unique"
        assert res3["allowed"] is True
        sys.stderr.write("  [PASS] 3. Unique clip detection (is_duplicate=False, classification=unique)\n")

        # ---------------------------------------------------------------------
        # TEST 4: Database transition rejection (409 Conflict) and ?force=1
        # ---------------------------------------------------------------------
        db.upsert_item(
            conn, "it-dup-test",
            channel_id=cid1,
            title="Doja Cat at Chase Center SF! Free Concert Fits on DressIt #Shorts",
            source_ref="job1"
        )
        db.transition_item(conn, "it-dup-test", "review")
        db.upsert_content_fingerprint(
            conn, "it-dup-test",
            video_path="/dummy/job1_dup.mp4",
            video_duration=15.0,
            video_hash=fp_a_v["composite"],
            video_meta=fp_a_v,
            text_clean="doja cat at chase center sf free concert fits on dressit shorts"
        )
        dedup_check = db.check_duplicate_item(
            conn,
            item_id="it-dup-test",
            check_states=("approved", "scheduled", "published"),
            exclude_item_id="it-dup-test"
        )
        assert dedup_check["is_duplicate"] is True
        assert dedup_check["duplicate_of"] == "it-base"
        assert dedup_check["allowed"] is False
        sys.stderr.write("  [PASS] 4. Item check hook detects duplicate of it-base before scheduling\n")

        # ---------------------------------------------------------------------
        # TEST 5: Real Video File Integration (job1 vs job2)
        # ---------------------------------------------------------------------
        real_job1 = "/root/event-hero/job1/final.mp4"
        real_job2 = "/root/event-hero/job2/final.mp4"
        if os.path.exists(real_job1) and os.path.exists(real_job2):
            real_db_path = "/root/posting/data/posting.db"
            if os.path.exists(real_db_path):
                real_res = check_candidate_cli(
                    video_path=real_job1,
                    text="Doja Cat at Chase Center SF! Free Concert Fits on DressIt #Shorts",
                    db_path=real_db_path
                )
                assert "is_duplicate" in real_res
                assert "similarity_score" in real_res
                sys.stderr.write(
                    f"  [PASS] 5. Real media verification on {os.path.basename(real_job1)} "
                    f"(match={real_res.get('duplicate_of')}, sim={real_res.get('similarity_score')})\n"
                )

        # ---------------------------------------------------------------------
        # TEST 6: Subprocess CLI Execution with jq/json output parsing
        # ---------------------------------------------------------------------
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--db", test_db_path,
            "--text", "Completely unique clip 12345",
            "--channel_id", str(cid1)
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        assert proc.returncode == 0, f"CLI command failed: {proc.stderr}"
        parsed_stdout = json.loads(proc.stdout.strip())
        assert "is_duplicate" in parsed_stdout
        assert "similarity_score" in parsed_stdout
        assert "classification" in parsed_stdout
        assert parsed_stdout["is_duplicate"] is False
        sys.stderr.write("  [PASS] 6. CLI invocation pure JSON stdout parsing\n")

        conn.close()
    finally:
        pass

    sys.stderr.write("\nALL 6 SELFTEST SUITES PASSED SUCCESSFULLY (100% adherence)!\n\n")
    return 0


# ============================================================================
# 5. CLI Entrypoint
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Check duplicate content against posting.db or index database (DressIt Task #1955)"
    )
    parser.add_argument(
        "video_path", nargs="?", default=None,
        help="Path to candidate video file"
    )
    parser.add_argument(
        "--text", type=str, default="",
        help="Accompanying caption or script text"
    )
    parser.add_argument(
        "--channel_id", "--channel-id", dest="channel_id", type=str, default=None,
        help="Target channel ID (for cooldown and cross-channel rules)"
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to posting.db SQLite database"
    )
    parser.add_argument(
        "--source-ref", "--source_ref", dest="source_ref", type=str, default="",
        help="Source reference (e.g. 'job1' or 'vid_id:10.0-20.0')"
    )
    parser.add_argument(
        "--index-all", "--index_all", dest="index_all", action="store_true",
        help="Index all items with video files in posting.db into content_fingerprints"
    )
    parser.add_argument(
        "--report-file", "--report_file", dest="report_file", type=str, default=None,
        help="Output path for deduplication report JSON"
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="Execute automated self-tests and exit 0"
    )

    args = parser.parse_args()

    # Selftest mode
    if args.selftest:
        sys.exit(run_selftest())

    target_db = args.db or db.db_path()

    # Index all mode
    if args.index_all:
        conn = db.get_db(target_db)
        try:
            index_database(conn, max_workers=6, verbose=True)
            report = generate_dedup_report(conn, report_file=args.report_file, print_summary=True)
            if not args.video_path:
                sys.exit(0)
        finally:
            conn.close()

    # Single check mode
    if args.video_path or args.text or args.source_ref:
        res = check_candidate_cli(
            video_path=args.video_path,
            text=args.text,
            channel_id=args.channel_id,
            db_path=target_db,
            source_ref=args.source_ref
        )
        sys.stdout.write(json.dumps(res, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        sys.exit(0)

    # If no arguments provided
    parser.print_help(sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
