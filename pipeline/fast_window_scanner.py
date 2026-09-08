#!/usr/bin/env python3
import os, sys, cv2
import numpy as np
sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe

def scan_video(src, comedian_name, max_start=None):
    if not os.path.exists(src):
        print(f"File not found: {src}")
        return []
    
    cap = cv2.VideoCapture(src)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = total_frames / fps if fps > 0 else 0
    cap.release()
    
    print(f"\nScanning {comedian_name} ({os.path.basename(src)}): {w}x{h}, {dur:.1f}s, {fps:.1f}fps")
    
    # Pre-determine crop/scale
    # If 9:16 vertical (e.g. 608x1080, 480x852, 1080x1920)
    aspect = w / h
    is_vertical = 0.50 <= aspect <= 0.60
    
    window_dur = 8.0
    limit = min(dur - window_dur, max_start if max_start else dur - window_dur)
    
    # We want to find clean 8.0s windows
    best_windows = []
    
    # Step through candidate start times every 1.5s
    start_times = [s for s in np.arange(0.0, limit, 1.5)]
    
    for ss in start_times:
        # Check framing parameters
        try:
            framing = ffe.compute_framing_parameters(src, ss, ss + window_dur)
            filter_str = framing.get('filter_str', '')
        except Exception:
            continue
            
        # Sample 8 points at ss + 0.2 + i*1.0
        sample_times = [ss + 0.2 + i * 1.0 for i in range(8)]
        passed_samples = 0
        reasons = []
        
        for st in sample_times:
            frame = ffe.extract_frame(src, st)
            if frame is None:
                reasons.append(f"t={st:.1f}: frame extract failed")
                break
                
            # Apply crop/scale if needed
            # We can use ffmpeg or fast cv2 resize/crop based on framing
            # If vertical and close to 9:16, resize to 1080x1920
            if is_vertical:
                # scale to 1080x1920
                frame_1080 = cv2.resize(frame, (1080, 1920), interpolation=cv2.INTER_LINEAR)
            else:
                # Widescreen crop
                crop_x = framing.get('crop_x', (w - int(h * 9 / 16)) // 2)
                crop_w = framing.get('crop_w', int(h * 9 / 16))
                crop_y = framing.get('crop_y', 0)
                crop_h = framing.get('crop_h', h)
                cropped = frame[crop_y:crop_y+crop_h, crop_x:crop_x+crop_w]
                frame_1080 = cv2.resize(cropped, (1080, 1920), interpolation=cv2.INTER_LINEAR)
                
            # Detect face
            faces = ffe.detect_all_faces(frame_1080, min_size=150, max_size=1000)
            primary = ffe.get_primary_face(faces, 1080, 1920)
            if not primary:
                reasons.append(f"t={st:.1f}: no primary face (found {len(faces)} faces)")
                break
                
            cx = primary['cx']
            cy = primary['cy']
            fw = primary['w']
            fh = primary['h']
            
            # Check conditions
            if not (380 <= cx <= 700):
                reasons.append(f"t={st:.1f}: cx={cx} not in [380, 700]")
                break
            if not (200 <= cy <= 900):
                reasons.append(f"t={st:.1f}: cy={cy} not in [200, 900]")
                break
            if max(fw, fh) < 160:
                reasons.append(f"t={st:.1f}: size={max(fw, fh)} < 160")
                break
                
            passed_samples += 1
            
        if passed_samples == 8:
            print(f"  >>> PERFECT 100% PASS: ss={ss:.1f}s -> {ss+window_dur:.1f}s")
            best_windows.append((ss, window_dur, framing))
            if len(best_windows) >= 3:
                break
        else:
            # print first failure reason
            # print(f"  ss={ss:.1f}s: {passed_samples}/8 - {reasons[0] if reasons else 'failed'}")
            pass
            
    return best_windows

if __name__ == '__main__':
    targets = [
        # Comedians we need to verify:
        ("jared_freid", "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4"),
        ("sam_morril", "/root/dressit-pipeline/curated_standup/sources/sam_morril_O1It8FgMtGw.mp4"),
        ("matteo_lane_uqxx", "/root/dressit-pipeline/curated_standup/sources/matteo_lane_uqxx.mp4"),
        ("mattrife", "/root/dressit-pipeline/curated_standup/sources/matt_rife_0aakia-h2PQ.mp4"),
        # Other comedians in dressit-shorts/clips
        ("ikRIH3EpMLc", "/root/dressit-shorts/clips/ikRIH3EpMLc.mp4"),
        ("bBNat52F5xs", "/root/dressit-shorts/clips/bBNat52F5xs.mp4"),
        ("ct9bdqkNbfk", "/root/dressit-shorts/clips/ct9bdqkNbfk.mp4"),
        ("mmSV7PdU6zk", "/root/dressit-shorts/clips/mmSV7PdU6zk.mp4"),
        ("Um7rZN7_VrQ", "/root/dressit-shorts/clips/Um7rZN7_VrQ.mp4"),
        ("UG5FQB2i6PU", "/root/dressit-shorts/clips/UG5FQB2i6PU.mp4"),
        ("TeHvkLu3Dz0", "/root/dressit-shorts/clips/TeHvkLu3Dz0.mp4"),
    ]
    
    results = {}
    for name, path in targets:
        res = scan_video(path, name)
        results[name] = res
        
    print("\n" + "="*80)
    print("FINAL SUMMARY OF PERFECT 100% WINDOWS:")
    for name, wins in results.items():
        if wins:
            print(f"  {name:<20}: {len(wins)} windows found! First: ss={wins[0][0]:.1f}s")
        else:
            print(f"  {name:<20}: NO 100% windows")
