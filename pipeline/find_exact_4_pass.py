#!/usr/bin/env python3
import sys, os, subprocess
sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe

candidates = [
    # Fortune Feimster
    ("fortune_30", "/root/dressit-shorts/clips/d3hy_XNU458.mp4", 30.0, 8.0),
    ("fortune_35", "/root/dressit-shorts/clips/d3hy_XNU458.mp4", 35.0, 8.0),
    ("fortune_0", "/root/dressit-shorts/clips/d3hy_XNU458.mp4", 0.0, 8.0),
    
    # Matt Rife
    ("mattrife_23", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4", 23.5, 8.0),
    ("mattrife_43", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4", 43.0, 8.0),
    ("mattrife_55", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4", 55.0, 8.0),

    # Jared Freid
    ("jared_0", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4", 0.0, 8.0),
    ("jared_64", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4", 64.0, 8.0),
    ("jared_70", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4", 70.0, 8.0),

    # Sam Morril
    ("sammorril_0", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4", 0.0, 8.0),
    ("sammorril_10", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4", 10.0, 8.0),
    ("sammorril_20", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4", 20.0, 8.0),

    # Nate Bargatze
    ("nate_30", "/root/dressit-shorts/clips/wacNJSgPxUM.mp4", 30.0, 8.0),
    ("nate_45", "/root/dressit-shorts/clips/wacNJSgPxUM.mp4", 45.0, 8.0),
    ("nate_60", "/root/dressit-shorts/clips/wacNJSgPxUM.mp4", 60.0, 8.0),

    # Other vertical clips in /root/dressit-shorts/clips/
    ("ikRIH3EpMLc_0", "/root/dressit-shorts/clips/ikRIH3EpMLc.mp4", 0.0, 8.0),
    ("bBNat52F5xs_0", "/root/dressit-shorts/clips/bBNat52F5xs.mp4", 0.0, 8.0),
    ("0Awxjh2_0", "/root/dressit-shorts/clips/0Awxjh2-pqw.mp4", 0.0, 8.0),
    ("2JjFj0McyaI_0", "/root/dressit-shorts/clips/2JjFj0McyaI.mp4", 0.0, 8.0),
]

passed_candidates = []

for tag, src, ss, dur in candidates:
    if not os.path.exists(src):
        continue
    out = f"/tmp/check_{tag}.mp4"
    try:
        framing = ffe.compute_framing_parameters(src, ss, ss + dur)
        ffe.render_framed_clip(src, out, ss, ss + dur, filter_str=framing['filter_str'])
        res = ffe.verify_face_framing(out, auto_frame=False)
        pct = res['pass_rate_pct']
        ok = res['passed']
        print(f"{tag:<16}: pass={ok} ({pct:.1f}%, {res['passed_samples']}/{res['total_samples']}) err={res['error']}")
        if ok and pct == 100.0:
            passed_candidates.append((tag, src, ss, dur, framing))
    except Exception as e:
        print(f"{tag:<16}: ERROR {e}")

print("\n" + "="*80)
print(f"PASSED CANDIDATES: {len(passed_candidates)}")
for p in passed_candidates:
    print(f"  {p[0]}: src={p[1]} ss={p[2]} dur={p[3]}")
