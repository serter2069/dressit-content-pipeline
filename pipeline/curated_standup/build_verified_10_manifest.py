#!/usr/bin/env python3
"""
build_verified_10_manifest.py

Renders, transcribes, verifies, and generates the final curated standup manifest
guaranteeing 100% face framing pass rate across all 10 distinct comedians.
"""

import os
import sys
import json
import subprocess
import tempfile

sys.path.insert(0, '/root/dressit-pipeline')
import face_framing_engine as ffe
from faster_whisper import WhisperModel

SEGMENTS_DIR = '/root/dressit-pipeline/curated_standup/segments'
MANIFEST_PATH = '/root/dressit-pipeline/curated_standup/manifest.json'
os.makedirs(SEGMENTS_DIR, exist_ok=True)

COMEDIANS_CONFIG = [
    {
        "id": 1,
        "comedian_key": "ianlara",
        "comedian_name": "Ian Lara",
        "topic": "Bitcoin dating / stock going up",
        "source_path": "/root/dressit-shorts/clips/VP2GWZomhE8.mp4",
        "start_ss": 4.5,
        "duration": 8.5,
        "filter_str": "scale=1169:2077:force_original_aspect_ratio=increase,crop=1080:1920:6:0",
        "setup_quote": "African-Latino, and if you've never heard of that, basically we're like the next big thing.",
        "punchline_quote": "They compare us to Bitcoin in 2011, it's like, invest now because the stock is going up!"
    },
    {
        "id": 2,
        "comedian_key": "taylor",
        "comedian_name": "Taylor Tomlinson",
        "topic": "Quitting dating losers",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/taylor_tomlinson_zEbrFZT2rjM.mp4",
        "start_ss": 13.5,
        "duration": 8.5,
        "filter_str": "scale=1119:1987:force_original_aspect_ratio=increase,crop=1080:1920:4:66",
        "setup_quote": "Losers are hard to quit. Losers are a lot like cigarettes.",
        "punchline_quote": "You have one in your mouth, you feel pretty cool, but everyone around you is like, ew, we're sad for you."
    },
    {
        "id": 3,
        "comedian_key": "nikki",
        "comedian_name": "Nikki Glaser",
        "topic": "Compatibility over chemistry",
        "source_path": "/root/dressit-shorts/clips/4Od7DrVAypc.mp4",
        "start_ss": 0.0,
        "duration": 8.0,
        "filter_str": "scale=1154:2049:force_original_aspect_ratio=increase,crop=1080:1920:'clip(72 + (74 - 72)*t/8.300, 0, in_w-out_w)':128",
        "setup_quote": "People love to ask me, like, chemistry or compatibility.",
        "punchline_quote": "And I'm like, compatibility 100% of the time. Do you want to move through the world similarly?"
    },
    {
        "id": 4,
        "comedian_key": "fortune",
        "comedian_name": "Fortune Feimster",
        "topic": "Mom's marriage pressure & cheerleaders",
        "source_path": "/root/dressit-shorts/clips/d3hy_XNU458.mp4",
        "start_ss": 30.0,
        "duration": 8.4,
        "filter_str": "scale=1097:1952:force_original_aspect_ratio=increase,crop=1080:1920:6:32",
        "setup_quote": "Ashley Davidson, she had long dark hair, she was on the cheerleading squad.",
        "punchline_quote": "I'm like, no, Mom, it still does not ring a bell."
    },
    {
        "id": 5,
        "comedian_key": "nate",
        "comedian_name": "Nate Bargatze",
        "topic": "Wife's ex on open water",
        "source_path": "/root/dressit-shorts/clips/wacNJSgPxUM.mp4",
        "start_ss": 74.2,
        "duration": 8.3,
        "filter_str": "scale=1154:2049:force_original_aspect_ratio=increase,crop=1080:1920:'clip(72 + (74 - 72)*t/8.300, 0, in_w-out_w)':128",
        "setup_quote": "My friends were like, you should've went and tried to fight him. Why didn't you go fight him?",
        "punchline_quote": "And I was like, well, I would've had to swim over to that fight."
    },
    {
        "id": 6,
        "comedian_key": "matteo",
        "comedian_name": "Matteo Lane",
        "topic": "Screening dating partners & grammar",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/matteo_lane_gay_straight.mp4",
        "start_ss": 29.0,
        "duration": 8.5,
        "filter_str": "crop=608:1080:772:0,scale=1080:1920:flags=lanczos",
        "setup_quote": "If I'm talking to you and you use good grammar, I will fuck you.",
        "punchline_quote": "If you know how to use a semicolon, get in this colon."
    },
    {
        "id": 7,
        "comedian_key": "aliwong",
        "comedian_name": "Ali Wong",
        "topic": "Dating vs Marriage freedom",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/ali_wong_CoNlSGGo0g4.mp4",
        "start_ss": 13.5,
        "duration": 8.5,
        "filter_str": "scale=1235:2195:force_original_aspect_ratio=increase,crop=1080:1920:0:0",
        "setup_quote": "Single people, if you're romantically involved with somebody... you could just leave.",
        "punchline_quote": "Move to another city and never see their stupid face again!"
    },
    {
        "id": 8,
        "comedian_key": "jared",
        "comedian_name": "Jared Freid",
        "topic": "Meeting on dating app Hinge",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/jared_freid_KPdG0MYUFAI.mp4",
        "start_ss": 64.0,
        "duration": 8.5,
        "filter_str": "scale=1458:2592:force_original_aspect_ratio=increase,crop=1080:1920:'clip(if(lt(t, 2.5), 180 - 180*t/2.5, 0), 0, in_w-out_w)':100",
        "setup_quote": "They both just went: you know, Hinge.",
        "punchline_quote": "Yeah, you met the love of your life on a dating website. How fucking crazy is that? Give it up for them!"
    },
    {
        "id": 9,
        "comedian_key": "gianmarco",
        "comedian_name": "Gianmarco Soresi",
        "topic": "Crowd work & dating disorders",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/gianmarco_soresi_LzQVU7bPEcw.mp4",
        "start_ss": 0.0,
        "duration": 8.5,
        "filter_str": "scale=1080:1920",
        "setup_quote": "Do you have any mental disorder?",
        "punchline_quote": "General ennui. Sorry, I think that's made up actually. I don't believe in ennui."
    },
    {
        "id": 10,
        "comedian_key": "ashley",
        "comedian_name": "Ashley Hesseltine",
        "topic": "Wedding excitement & vows",
        "source_path": "/root/dressit-shorts/clips/ikRIH3EpMLc.mp4",
        "start_ss": 19.5,
        "duration": 8.3,
        "filter_str": "scale=1154:2049:force_original_aspect_ratio=increase,crop=1080:1920:'clip(72 + (74 - 72)*t/8.300, 0, in_w-out_w)':128",
        "setup_quote": "I wanna see the decor, like I just wanna see like what they did in there, I'm dying for it. Okay, what's your answer?",
        "punchline_quote": "The fucking vows, I would die to see these vows!"
    }
]

def main():
    print("Loading Whisper model...")
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

    manifest = {
        "title": "Curated Standup Comedian Dating Clips Manifest",
        "total_clips": len(COMEDIANS_CONFIG),
        "target_format": "1080x1920 (9:16 vertical)",
        "duration_range_sec": "8.0s - 8.5s",
        "clips": []
    }

    all_passed = True

    for item in COMEDIANS_CONFIG:
        c_id = item["id"]
        key = item["comedian_key"]
        name = item["comedian_name"]
        src = item["source_path"]
        s_ss = item["start_ss"]
        dur = item["duration"]
        e_ss = s_ss + dur
        f_str = item["filter_str"]

        print(f"\n=======================================================")
        print(f"[{c_id}/10] Processing {name} ({key})")
        print(f"Source: {src}")
        print(f"Segment: {s_ss:.2f}s - {e_ss:.2f}s (duration: {dur:.2f}s)")
        print(f"Filter: {f_str}")
        print(f"=======================================================")

        if not os.path.exists(src):
            print(f"ERROR: Source file missing: {src}")
            sys.exit(1)

        info = ffe.get_video_info(src)
        out_segment_path = os.path.join(SEGMENTS_DIR, f"{c_id:02d}_{key}_segment.mp4")

        # 1. Render segment with guaranteed framing filter
        render_res = ffe.render_framed_clip(src, out_segment_path, start_t=s_ss, end_t=e_ss, filter_str=f_str)
        print(f"Rendered segment: {out_segment_path} ({render_res['duration']:.2f}s)")

        # 2. Extract word-level timestamps via faster_whisper
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = tf.name
        try:
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-i", out_segment_path,
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                wav_path
            ]
            subprocess.run(cmd, check=True)
            segments_gen, _ = whisper_model.transcribe(wav_path, word_timestamps=True)
            transcribed_segments = []
            words_list = []
            for seg in segments_gen:
                transcribed_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip()
                })
                if seg.words:
                    for w in seg.words:
                        words_list.append({
                            "word": w.word.strip(),
                            "start": round(w.start, 2),
                            "end": round(w.end, 2),
                            "probability": round(w.probability, 2)
                        })
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)

        # 3. Hard-gate face framing verification
        verify_res = ffe.verify_face_framing(out_segment_path, start_t=0.0, end_t=dur, step_sec=1.0, auto_frame=False, debug_dir=None)
        
        passed = verify_res["passed"]
        if not passed:
            all_passed = False
            print(f"FAILED: {name} face verification failed: {verify_res['error']}")
        else:
            print(f"PASSED: {name} 100% face framing verified ({verify_res['passed_samples']}/{verify_res['total_samples']})")

        metrics = {
            "overall_passed": verify_res["passed"],
            "total_samples": verify_res["total_samples"],
            "passed_samples": verify_res["passed_samples"],
            "pass_rate_pct": verify_res["pass_rate_pct"],
            "error": verify_res["error"],
            "framing_details": {
                "filter_str": f_str
            }
        }

        clip_entry = {
            "id": c_id,
            "comedian_key": key,
            "comedian_name": name,
            "topic": item["topic"],
            "source_file": src,
            "source_resolution": f"{info['width']}x{info['height']}",
            "rendered_segment_file": out_segment_path,
            "rendered_resolution": "1080x1920",
            "start_ss": s_ss,
            "duration": dur,
            "end_ss": e_ss,
            "setup_quote": item["setup_quote"],
            "punchline_quote": item["punchline_quote"],
            "transcription": {
                "full_text": " ".join([s["text"] for s in transcribed_segments]),
                "segments": transcribed_segments,
                "words": words_list
            },
            "face_verification": metrics
        }
        manifest["clips"].append(clip_entry)

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\n=======================================================")
    print(f"Manifest written to {MANIFEST_PATH}")
    print(f"All 10 clips overall_passed == True? -> {all_passed}")
    print(f"=======================================================")
    if not all_passed:
        sys.exit(1)

if __name__ == "__main__":
    main()
