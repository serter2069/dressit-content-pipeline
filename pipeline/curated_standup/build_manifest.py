#!/usr/bin/env python3
"""
build_manifest.py

Assembles, frames, transcribes, and verifies 10 distinct comedian standup clips
into /root/dressit-pipeline/curated_standup/manifest.json.
"""

import os
import sys
import json
import subprocess
import tempfile
import cv2
import numpy as np

sys.path.insert(0, '/root/dressit-pipeline')
from face_framing_engine import (
    compute_framing_parameters,
    render_framed_clip,
    verify_face_framing,
    get_video_info,
    TARGET_WIDTH,
    TARGET_HEIGHT
)
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
        "start_ss": 4.0,
        "duration": 8.5,
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
        "setup_quote": "People love to ask me, like, chemistry or compatibility.",
        "punchline_quote": "And I'm like, compatibility 100% of the time. Do you want to move through the world similarly?"
    },
    {
        "id": 4,
        "comedian_key": "fortune",
        "comedian_name": "Fortune Feimster",
        "topic": "Mom's marriage pressure & minivans",
        "source_path": "/root/dressit-shorts/clips/d3hy_XNU458.mp4",
        "start_ss": 30.0,
        "duration": 8.0,
        "setup_quote": "My mom is like, you need to settle down and find someone.",
        "punchline_quote": "Because every time I see my family, they treat being single like an emergency!"
    },
    {
        "id": 5,
        "comedian_key": "nate",
        "comedian_name": "Nate Bargatze",
        "topic": "Marriage trapped in a good way",
        "source_path": "/root/dressit-shorts/clips/wacNJSgPxUM.mp4",
        "start_ss": 25.0,
        "duration": 7.8,
        "setup_quote": "This is what marriage feels like, by the way, what it feels like in here.",
        "punchline_quote": "Right now, it's just one person talking to me and it's like, I'm gonna get out of here!"
    },
    {
        "id": 6,
        "comedian_key": "matteo",
        "comedian_name": "Matteo Lane",
        "topic": "Guys & dating habits",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/matteo_lane_uqxx.mp4",
        "start_ss": 0.0,
        "duration": 8.0,
        "setup_quote": "It is wild, the kind of screening process women have to get through.",
        "punchline_quote": "My friend asked the guy: do you have friends? Like actual human friends?"
    },
    {
        "id": 7,
        "comedian_key": "jimmyoyang",
        "comedian_name": "Jimmy O. Yang",
        "topic": "Dating tall girls & heels",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/jimmy_o_yang_S5R2-jCjvs0.mp4",
        "start_ss": 0.6,
        "duration": 7.6,
        "setup_quote": "I've been dating a lot of tall girls lately because it makes me look successful.",
        "punchline_quote": "Tall women are beautiful, but some of them like to wear heels. That's just disrespect!"
    },
    {
        "id": 8,
        "comedian_key": "aliwong",
        "comedian_name": "Ali Wong",
        "topic": "Husband & relationship dynamic",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/ali_wong_CoNlSGGo0g4.mp4",
        "start_ss": 0.0,
        "duration": 8.0,
        "setup_quote": "Single people, if you're romantically involved with somebody and they reveal a personality trait you don't like?",
        "punchline_quote": "You could just leave! Move to another city and never see their stupid face again!"
    },
    {
        "id": 9,
        "comedian_key": "whitney",
        "comedian_name": "Whitney Cummings",
        "topic": "Catcallers vs men with real effort",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/whitney_cummings_N17QFrDfC3E.mp4",
        "start_ss": 5.5,
        "duration": 8.0,
        "setup_quote": "My new thing with catcallers is just do what they ask, because then they'll realize it's not what they want.",
        "punchline_quote": "Next time a guy is like, hey baby smile, just be like: ahhhhhh!"
    },
    {
        "id": 10,
        "comedian_key": "gianmarco",
        "comedian_name": "Gianmarco Soresi",
        "topic": "Modern dating",
        "source_path": "/root/dressit-pipeline/curated_standup/sources/gianmarco_soresi_LzQVU7bPEcw.mp4",
        "start_ss": 0.0,
        "duration": 8.0,
        "setup_quote": "Have years of dating left you tired and unfulfilled?",
        "punchline_quote": "Do you like spontaneity but prefer stability? Well, maybe it's time to try: settling!"
    }
]

def main():
    print("Initializing Whisper Model...")
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    
    manifest = {
        "title": "Curated Standup Comedian Dating Clips Manifest",
        "total_clips": len(COMEDIANS_CONFIG),
        "target_format": "1080x1920 (9:16 vertical)",
        "duration_range_sec": "8.0s - 9.0s",
        "clips": []
    }

    for item in COMEDIANS_CONFIG:
        c_id = item["id"]
        key = item["comedian_key"]
        name = item["comedian_name"]
        src = item["source_path"]
        s_ss = item["start_ss"]
        dur = item["duration"]
        e_ss = s_ss + dur

        print(f"\n=======================================================")
        print(f"[{c_id}/10] Processing {name} ({key})")
        print(f"Source: {src}")
        print(f"Segment: {s_ss:.2f}s - {e_ss:.2f}s (duration: {dur:.2f}s)")
        print(f"=======================================================")

        if not os.path.exists(src):
            print(f"ERROR: Source file missing: {src}")
            sys.exit(1)

        info = get_video_info(src)
        print(f"Source format: {info['width']}x{info['height']} ({'Widescreen' if info['is_widescreen'] else 'Vertical'})")

        out_segment_path = os.path.join(SEGMENTS_DIR, f"{c_id:02d}_{key}_segment.mp4")

        # 1. Compute auto-framing and render framed 1080x1920 vertical clip
        print("Computing framing parameters and rendering segment...")
        framing = compute_framing_parameters(src, s_ss, e_ss)
        print(f"Framing mode: {framing.get('mode')} | filter: {framing.get('filter_str')}")
        
        render_res = render_framed_clip(src, out_segment_path, start_t=s_ss, end_t=e_ss, filter_str=framing.get("filter_str"))
        print(f"Rendered segment: {out_segment_path} ({render_res['duration']:.2f}s)")

        # 2. Extract word-level aligned timestamps via faster_whisper
        print("Transcribing segment for word-level timestamps...")
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

        # 3. Verify face framing on the rendered 1080x1920 segment
        print("Verifying face framing with OpenCV...")
        verify_res = verify_face_framing(out_segment_path, start_t=0.0, end_t=dur, step_sec=1.0, auto_frame=False, debug_dir=None)
        
        metrics = {
            "overall_passed": verify_res["passed"],
            "total_samples": verify_res["total_samples"],
            "passed_samples": verify_res["passed_samples"],
            "pass_rate_pct": verify_res["pass_rate_pct"],
            "error": verify_res["error"],
            "framing_details": {
                "mode": framing.get("mode"),
                "crop": framing.get("crop"),
                "zoom": framing.get("zoom", 1.0),
                "filter_str": framing.get("filter_str")
            }
        }
        print(f"Face verification: {verify_res['passed_samples']}/{verify_res['total_samples']} samples passed ({verify_res['pass_rate_pct']}%)")

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
    print(f"SUCCESS: Saved verified manifest to {MANIFEST_PATH}")
    print(f"Total clips assembled: {len(manifest['clips'])}")
    print(f"=======================================================")

if __name__ == "__main__":
    main()
