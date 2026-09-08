#!/usr/bin/env python3
"""
test_face_framing_engine.py

Comprehensive test suite for face_framing_engine.py.
Validates:
1. Valid framed clip: /root/dressit-shorts/clips/4Od7DrVAypc.mp4 on [1.0, 6.0] -> PASS (100%)
2. Valid framed clip: /root/dressit-shorts/clips/VP2GWZomhE8.mp4 on [4.2, 8.2] -> PASS (100%)
3. Bad clip (tiny ant wide shot): /root/dressit-shorts/clips/wacNJSgPxUM.mp4 on [7.0, 11.0] -> REJECT
4. Bad clip (no face / off-center): /root/dressit-shorts/clips/wacNJSgPxUM.mp4 on [1.0, 4.0] -> REJECT
5. Bad clip (unframed raw vertical without auto-framing): VP2GWZomhE8.mp4 on [4.2, 8.2] with auto_frame=False -> REJECT
6. 16:9 widescreen dynamic crop & framing: MF-8f7wpgxU.mp4 on [18.5, 22.5] -> PASS (100%)
7. Rendered 1080x1920 MP4 file verification with auto_frame=False -> PASS (100%)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/root/dressit-pipeline")
import face_framing_engine

def run_tests():
    print("=" * 80)
    print("RUNNING FACE FRAMING ENGINE COMPREHENSIVE TEST SUITE")
    print("=" * 80)

    results = []

    # Test 1: 4Od7DrVAypc on [1.0, 6.0] -> MUST PASS
    print("\n[TEST 1] Testing 4Od7DrVAypc.mp4 on [1.0, 6.0] (Expect: PASS)...")
    res1 = face_framing_engine.verify_face_framing(
        "/root/dressit-shorts/clips/4Od7DrVAypc.mp4",
        start_t=1.0, end_t=6.0, step_sec=1.0
    )
    assert res1["passed"] is True, f"Test 1 failed: expected PASS, got {res1['error']}"
    assert res1["passed_samples"] == res1["total_samples"] == 6
    print(f"  PASS: 100% samples passed ({res1['total_samples']}/{res1['total_samples']})")
    results.append({"test": "4Od7DrVAypc [1.0 - 6.0]", "expected": "PASS", "actual": "PASS", "details": res1})

    # Test 2: VP2GWZomhE8 on [4.2, 8.2] -> MUST PASS
    print("\n[TEST 2] Testing VP2GWZomhE8.mp4 on [4.2, 8.2] (Expect: PASS)...")
    res2 = face_framing_engine.verify_face_framing(
        "/root/dressit-shorts/clips/VP2GWZomhE8.mp4",
        start_t=4.2, end_t=8.2, step_sec=1.0
    )
    assert res2["passed"] is True, f"Test 2 failed: expected PASS, got {res2['error']}"
    assert res2["passed_samples"] == res2["total_samples"] == 5
    print(f"  PASS: 100% samples passed ({res2['total_samples']}/{res2['total_samples']})")
    results.append({"test": "VP2GWZomhE8 [4.2 - 8.2]", "expected": "PASS", "actual": "PASS", "details": res2})

    # Test 3: wacNJSgPxUM on [7.0, 11.0] -> MUST REJECT (tiny ant wide shot)
    print("\n[TEST 3] Testing wacNJSgPxUM.mp4 on [7.0, 11.0] (Expect: REJECT tiny ant wide shot)...")
    res3 = face_framing_engine.verify_face_framing(
        "/root/dressit-shorts/clips/wacNJSgPxUM.mp4",
        start_t=7.0, end_t=11.0, step_sec=1.0
    )
    assert res3["passed"] is False, "Test 3 failed: expected REJECT, got PASS"
    print(f"  REJECTED as expected: {res3['error']}")
    results.append({"test": "wacNJSgPxUM [7.0 - 11.0]", "expected": "REJECT", "actual": "REJECT", "reason": res3["error"]})

    # Test 4: wacNJSgPxUM on [1.0, 4.0] -> MUST REJECT (no face / off-center)
    print("\n[TEST 4] Testing wacNJSgPxUM.mp4 on [1.0, 4.0] (Expect: REJECT no face/off-center)...")
    res4 = face_framing_engine.verify_face_framing(
        "/root/dressit-shorts/clips/wacNJSgPxUM.mp4",
        start_t=1.0, end_t=4.0, step_sec=1.0
    )
    assert res4["passed"] is False, "Test 4 failed: expected REJECT, got PASS"
    print(f"  REJECTED as expected: {res4['error']}")
    results.append({"test": "wacNJSgPxUM [1.0 - 4.0]", "expected": "REJECT", "actual": "REJECT", "reason": res4["error"]})

    # Test 5: VP2GWZomhE8 on [4.2, 8.2] without auto-framing -> MUST REJECT (raw vertical dimensions)
    print("\n[TEST 5] Testing raw unframed VP2GWZomhE8.mp4 with auto_frame=False (Expect: REJECT)...")
    res5 = face_framing_engine.verify_face_framing(
        "/root/dressit-shorts/clips/VP2GWZomhE8.mp4",
        start_t=4.2, end_t=8.2, step_sec=1.0, auto_frame=False
    )
    assert res5["passed"] is False, "Test 5 failed: expected REJECT for unframed clip, got PASS"
    print(f"  REJECTED as expected: {res5['error']}")
    results.append({"test": "VP2GWZomhE8 [4.2 - 8.2] (auto_frame=False)", "expected": "REJECT", "actual": "REJECT", "reason": res5["error"]})

    # Test 6: 16:9 widescreen crop & scale: MF-8f7wpgxU on [18.5, 22.5] -> MUST PASS
    print("\n[TEST 6] Testing widescreen MF-8f7wpgxU.mp4 on [18.5, 22.5] (Expect: PASS)...")
    res6 = face_framing_engine.verify_face_framing(
        "/root/dressit-pipeline/source_clips/MF-8f7wpgxU.mp4",
        start_t=18.5, end_t=22.5, step_sec=1.0
    )
    assert res6["passed"] is True, f"Test 6 failed: expected PASS, got {res6['error']}"
    assert res6["passed_samples"] == res6["total_samples"] == 5
    print(f"  PASS: 100% samples passed ({res6['total_samples']}/{res6['total_samples']})")
    results.append({"test": "MF-8f7wpgxU widescreen [18.5 - 22.5]", "expected": "PASS", "actual": "PASS", "details": res6})

    # Test 7: Rendered 1080x1920 MP4 verification with --no-auto-frame -> MUST PASS
    print("\n[TEST 7] Testing rendered 1080x1920 MP4 with auto_frame=False (Expect: PASS)...")
    res7 = face_framing_engine.verify_face_framing(
        "/root/dressit-pipeline/output/test_framed_aliwong.mp4",
        start_t=0.0, end_t=4.0, step_sec=1.0, auto_frame=False
    )
    assert res7["passed"] is True, f"Test 7 failed: expected PASS, got {res7['error']}"
    assert res7["passed_samples"] == res7["total_samples"] == 4
    print(f"  PASS: 100% samples passed ({res7['total_samples']}/{res7['total_samples']})")
    results.append({"test": "Rendered test_framed_aliwong.mp4 (1080x1920)", "expected": "PASS", "actual": "PASS", "details": res7})

    print("\n" + "=" * 80)
    print("ALL 7 TEST SCENARIOS PASSED WITH ZERO REGRESSIONS!")
    print("=" * 80)

    report_path = Path("/root/dressit-pipeline/output/face_framing_test_results.json")
    report_path.write_text(json.dumps({
        "all_tests_passed": True,
        "total_tests": len(results),
        "results": [
            {
                "test": r["test"],
                "expected": r["expected"],
                "actual": r["actual"],
                "reason": r.get("reason"),
                "passed_rate": r.get("details", {}).get("pass_rate_pct", 0) if "details" in r else None
            } for r in results
        ]
    }, indent=2), encoding="utf-8")
    print(f"Report saved to {report_path}")

if __name__ == "__main__":
    run_tests()
