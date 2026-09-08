#!/usr/bin/env python3
import sys, os, subprocess
sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe

candidates = [
    ("aliwong", "/root/dressit-pipeline/curated_standup/sources/ali_wong_CoNlSGGo0g4.mp4", 1.5, 8.0),
    ("matteo", "/root/dressit-pipeline/curated_standup/sources/matteo_lane_uqxx.mp4", 0.0, 8.0),
    ("mattrife", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4", 0.0, 8.0),
    ("jared", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4", 0.0, 8.0),
    ("gianmarco", "/root/dressit-pipeline/curated_standup/sources/gianmarco_soresi_LzQVU7bPEcw.mp4", 0.0, 8.0),
    ("sammorril", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4", 0.0, 8.0),
]

for tag, src, ss, dur in candidates:
    out = f"/tmp/test_scale_{tag}.mp4"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(ss), "-t", str(dur),
        "-i", src,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy",
        out
    ]
    subprocess.run(cmd, check=True)
    res = ffe.verify_face_framing(out, auto_frame=False)
    print(f"{tag:<12}: pass={res['passed']} ({res['pass_rate_pct']}%, {res['passed_samples']}/{res['total_samples']}) error={res['error']}")

