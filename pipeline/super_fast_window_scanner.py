#!/usr/bin/env python3
import os, sys, cv2
import numpy as np
sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe

def scan_candidate(src, comedian_name, window_dur=8.0):
    if not os.path.exists(src):
        return []
    
    cap = cv2.VideoCapture(src)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = total_frames / fps if fps > 0 else 0
    
    if dur < window_dur + 1.0:
        cap.release()
        return []
        
    aspect = w / h
    is_vertical = 0.50 <= aspect <= 0.60
    
    # Let's extract 1 frame every 0.5 seconds across the whole video in one pass!
    sample_step = 0.5
    times = np.arange(0.5, dur - 0.5, sample_step)
    
    # Store per-timestamp validity
    # True if face detected, centered, upper-middle, prominent
    valid_map = {}
    
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret or frame is None:
            valid_map[round(t, 2)] = False
            continue
            
        if is_vertical:
            if w != 1080 or h != 1920:
                frame = cv2.resize(frame, (1080, 1920), interpolation=cv2.INTER_LINEAR)
        else:
            # Widescreen center crop
            crop_w = int(h * 9 / 16)
            crop_x = (w - crop_w) // 2
            frame = frame[:, crop_x:crop_x+crop_w]
            frame = cv2.resize(frame, (1080, 1920), interpolation=cv2.INTER_LINEAR)
            
        # Detect face
        faces = ffe.detect_all_faces(frame, min_size=150, max_size=1000)
        primary = ffe.get_primary_face(faces, 1080, 1920)
        if not primary:
            valid_map[round(t, 2)] = False
            continue
            
        cx, cy, fw, fh = primary['cx'], primary['cy'], primary['w'], primary['h']
        if (380 <= cx <= 700) and (200 <= cy <= 900) and (max(fw, fh) >= 160):
            valid_map[round(t, 2)] = True
        else:
            valid_map[round(t, 2)] = False
            
    cap.release()
    
    # Now find contiguous blocks where ALL samples in [ss, ss+window_dur] are True!
    # A window of 8.0s has 8 verification samples (at ss+0.2 + i*1.0)
    passing_windows = []
    
    for ss in np.arange(0.0, dur - window_dur, 0.5):
        samples_ok = True
        for i in range(int(window_dur)):
            st = round(ss + 0.2 + i * 1.0, 2)
            # Find closest t in valid_map
            closest_t = min(valid_map.keys(), key=lambda x: abs(x - st))
            if not valid_map[closest_t]:
                samples_ok = False
                break
        if samples_ok:
            passing_windows.append(round(ss, 2))
            
    print(f"[{comedian_name:<16}] {w}x{h} ({dur:.1f}s): {len(passing_windows)} passing 8.0s windows found. (Valid frame ratio: {sum(valid_map.values())}/{len(valid_map)})")
    if passing_windows:
        print(f"   Sample valid start times: {passing_windows[:5]}")
    return passing_windows

targets = [
    # Curated sources:
    ("jared_freid", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4"),
    ("sam_morril", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4"),
    ("matteo_lane_uqxx", "/root/dressit-pipeline/curated_standup/sources/matteo_lane_uqxx.mp4"),
    ("mattrife", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4"),
    
    # Other vertical standups from dressit-shorts:
    ("ikRIH3EpMLc", "/root/dressit-shorts/clips/ikRIH3EpMLc.mp4"),
    ("bBNat52F5xs", "/root/dressit-shorts/clips/bBNat52F5xs.mp4"),
    ("ct9bdqkNbfk", "/root/dressit-shorts/clips/ct9bdqkNbfk.mp4"),
    ("mmSV7PdU6zk", "/root/dressit-shorts/clips/mmSV7PdU6zk.mp4"),
    ("Um7rZN7_VrQ", "/root/dressit-shorts/clips/Um7rZN7_VrQ.mp4"),
    ("UG5FQB2i6PU", "/root/dressit-shorts/clips/UG5FQB2i6PU.mp4"),
    ("TeHvkLu3Dz0", "/root/dressit-shorts/clips/TeHvkLu3Dz0.mp4"),
    ("OymBZ3NeExs", "/root/dressit-shorts/clips/OymBZ3NeExs.mp4"),
    ("lSflLzfWTUs", "/root/dressit-shorts/clips/lSflLzfWTUs.mp4"),
    ("KTiUpr2Mfsc", "/root/dressit-shorts/clips/KTiUpr2Mfsc.mp4"),
    ("qzsdeNEMtFw", "/root/dressit-shorts/clips/qzsdeNEMtFw.mp4"),
    ("tCWFULp8YN8", "/root/dressit-shorts/clips/tCWFULp8YN8.mp4"),
    ("uauhGiRnkVM", "/root/dressit-shorts/clips/uauhGiRnkVM.mp4"),
]

for name, path in targets:
    scan_candidate(path, name)
