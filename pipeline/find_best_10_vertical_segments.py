#!/usr/bin/env python3
"""
find_best_10_vertical_segments.py

Scans candidate windows in 11 vertical comedian sources to find 8.0s - 8.5s windows
with 100% face framing pass rate.
"""

import os
import sys
import json

sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe

SOURCES = [
    {
        "id": 1,
        "key": "ianlara",
        "name": "Ian Lara",
        "path": "/root/dressit-shorts/clips/VP2GWZomhE8.mp4",
        "candidate_starts": [4.5, 4.0, 5.0],
        "dur": 8.5,
    },
    {
        "id": 2,
        "key": "taylor",
        "name": "Taylor Tomlinson",
        "path": "/root/dressit-pipeline/curated_standup/sources/taylor_tomlinson_zEbrFZT2rjM.mp4",
        "candidate_starts": [13.5, 14.0, 15.0, 1.0, 5.0],
        "dur": 8.5,
    },
    {
        "id": 3,
        "key": "nikki",
        "name": "Nikki Glaser",
        "path": "/root/dressit-shorts/clips/4Od7DrVAypc.mp4",
        "candidate_starts": [0.0, 1.0, 2.0],
        "dur": 8.0,
    },
    {
        "id": 4,
        "key": "fortune",
        "name": "Fortune Feimster",
        "path": "/root/dressit-shorts/clips/d3hy_XNU458.mp4",
        "candidate_starts": [0.0, 2.0, 4.0, 8.0, 10.8, 12.0, 15.0, 20.0, 25.0, 30.0],
        "dur": 8.4,
    },
    {
        "id": 5,
        "key": "nate",
        "name": "Nate Bargatze",
        "path": "/root/dressit-shorts/clips/wacNJSgPxUM.mp4",
        "candidate_starts": [0.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0],
        "dur": 8.2,
    },
    {
        "id": 6,
        "key": "matteo",
        "name": "Matteo Lane",
        "path": "/root/dressit-pipeline/curated_standup/sources/matteo_lane_uqxx.mp4",
        "candidate_starts": [0.0, 2.0, 5.0, 10.0, 15.0, 20.0],
        "dur": 8.5,
    },
    {
        "id": 7,
        "key": "mattrife",
        "name": "Matt Rife",
        "path": "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4",
        "candidate_starts": [0.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0],
        "dur": 8.5,
    },
    {
        "id": 8,
        "key": "aliwong",
        "name": "Ali Wong",
        "path": "/root/dressit-pipeline/curated_standup/sources/ali_wong_CoNlSGGo0g4.mp4",
        "candidate_starts": [0.0, 1.5, 3.0, 5.0, 10.0, 15.0, 20.0],
        "dur": 8.5,
    },
    {
        "id": 9,
        "key": "jared",
        "name": "Jared Freid",
        "path": "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4",
        "candidate_starts": [0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 64.5],
        "dur": 8.5,
    },
    {
        "id": 10,
        "key": "gianmarco",
        "name": "Gianmarco Soresi",
        "path": "/root/dressit-pipeline/curated_standup/sources/gianmarco_soresi_LzQVU7bPEcw.mp4",
        "candidate_starts": [0.0, 2.0, 5.0, 10.0, 15.0, 20.0, 25.0],
        "dur": 8.5,
    },
    {
        "id": 11,
        "key": "sammorril",
        "name": "Sam Morril",
        "path": "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4",
        "candidate_starts": [0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0],
        "dur": 8.5,
    },
]

print("Scanning candidate windows for 11 comedians...")
best_windows = {}

for s in SOURCES:
    print(f"\n--- Checking {s['name']} ({s['key']}) ---")
    if not os.path.exists(s['path']):
        print(f"  FILE NOT FOUND: {s['path']}")
        continue
    
    best = None
    for start in s['candidate_starts']:
        end = start + s['dur']
        try:
            res = ffe.verify_face_framing(s['path'], start_t=start, end_t=end, step_sec=1.0, auto_frame=True)
            pct = res.get('pass_rate_pct', 0.0)
            passed = res.get('passed', False)
            passed_s = res.get('passed_samples', 0)
            tot_s = res.get('total_samples', 0)
            print(f"  start={start:.1f}s, dur={s['dur']}s -> {passed} ({pct:.1f}%, {passed_s}/{tot_s} samples)")
            if best is None or pct > best['pct']:
                best = {'start': start, 'dur': s['dur'], 'pct': pct, 'passed': passed}
            if passed and pct == 100.0:
                best = {'start': start, 'dur': s['dur'], 'pct': pct, 'passed': passed}
                break
        except Exception as e:
            print(f"  start={start:.1f}s -> ERROR: {e}")
    
    best_windows[s['key']] = best
    if best:
        print(f"==> BEST for {s['name']}: start={best['start']:.1f}s, pass={best['passed']} ({best['pct']:.1f}%)")

print("\n" + "="*80)
print("FINAL SUMMARY OF SCANNED WINDOWS:")
for k, v in best_windows.items():
    if v:
        print(f"{k:<12}: start={v['start']:.1f}s dur={v['dur']:.1f}s pass={v['passed']} ({v['pct']:.1f}%)")
