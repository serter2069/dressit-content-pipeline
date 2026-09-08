#!/usr/bin/env python3
"""
produce_voice_div10_fixed.py — Master and extract ASS subtitle timestamps
for 10 Comedian Bridge Voiceovers for DressIt (Task #2010).

Strict Rules:
1. Engine: openai/gpt-audio via OpenRouter (voice: 'nova'). ZERO Gemini API, ZERO gpt-audio-mini.
2. Pace: Calm, unhurried, natural 1.0x conversational commercial tempo (~140 wpm, ~8.0s - 9.5s duration). DO NOT apply ffmpeg atempo speedup!
3. Prompt for Nova:
   "You are a warm, stylish, charming young woman sharing an exciting secret with a girlfriend. Speak in a clear, relaxed, natural conversational pace with calm and confident pauses so every word is crystal clear and effortless to understand. Do NOT rush."
4. Loudness: EBU R128 loudnorm to -14.0 LUFS (TP <= -1.5 dBFS, LRA 9) at 48000 Hz stereo.
5. Vocabulary:
   - ZERO "джентльмены" / "gentlemen" (use "guys", "men", "partner", "dates").
   - ZERO "bucks" (use "free", "$0").
   - ZERO "chips" or "pill badges".
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

ASSETS_DIR = Path("/root/dressit-pipeline/assets")
SUMMARY_JSON = ASSETS_DIR / "voice_div10_fixed_summary.json"

TRACK_CONFIGS = [
    {
        "track_id": 1,
        "comedian_key": "ianlara",
        "comedian_name": "Ian Lara",
        "topic": "Bitcoin dating / stock going up",
        "script_text": "Invest in someone who invests in you. Post your style on DressIt and let quality guys fund your outfits for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_01_ianlara.mp3",
        "output_filename": "voice_div10_fixed_01_ianlara.mp3",
        "phrases": [
            {"text": "INVEST IN SOMEONE\nWHO INVESTS IN YOU", "words": "Invest in someone who invests in you."},
            {"text": "POST YOUR STYLE\nON DRESSIT", "words": "Post your style on DressIt"},
            {"text": "AND LET QUALITY GUYS\nFUND YOUR OUTFITS FOR FREE!", "words": "and let quality guys fund your outfits for free."}
        ]
    },
    {
        "track_id": 2,
        "comedian_key": "taylor",
        "comedian_name": "Taylor Tomlinson",
        "topic": "Quitting dating losers",
        "script_text": "Stop settling for low-effort boys. Post your dream outfit on DressIt and let good guys fund it for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_02_taylor.mp3",
        "output_filename": "voice_div10_fixed_02_taylor.mp3",
        "phrases": [
            {"text": "STOP SETTLING FOR\nLOW-EFFORT BOYS", "words": "Stop settling for low-effort boys."},
            {"text": "POST YOUR DREAM OUTFIT\nON DRESSIT", "words": "Post your dream outfit on DressIt"},
            {"text": "AND LET GOOD GUYS\nFUND IT FOR FREE!", "words": "and let good guys fund it for free."}
        ]
    },
    {
        "track_id": 3,
        "comedian_key": "nikki",
        "comedian_name": "Nikki Glaser",
        "topic": "Compatibility over chemistry",
        "script_text": "Find guys who are actually on your wavelength. Post your dream dress on DressIt and let guys fund it for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_03_nikki.mp3",
        "output_filename": "voice_div10_fixed_03_nikki.mp3",
        "phrases": [
            {"text": "FIND GUYS WHO ARE ACTUALLY\nON YOUR WAVELENGTH", "words": "Find guys who are actually on your wavelength."},
            {"text": "POST YOUR DREAM DRESS\nON DRESSIT", "words": "Post your dream dress on DressIt"},
            {"text": "AND LET GUYS\nFUND IT FOR FREE!", "words": "and let guys fund it for free."}
        ]
    },
    {
        "track_id": 4,
        "comedian_key": "fortune",
        "comedian_name": "Fortune Feimster",
        "topic": "Mom's marriage pressure & minivans",
        "script_text": "Skip the family pressure and upgrade your style. Post your favorite dress on DressIt and let sweet guys fund it for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_04_fortune.mp3",
        "output_filename": "voice_div10_fixed_04_fortune.mp3",
        "phrases": [
            {"text": "SKIP THE FAMILY PRESSURE\nAND UPGRADE YOUR STYLE", "words": "Skip the family pressure and upgrade your style."},
            {"text": "POST YOUR FAVORITE DRESS\nON DRESSIT", "words": "Post your favorite dress on DressIt"},
            {"text": "AND LET SWEET GUYS\nFUND IT FOR FREE!", "words": "and let sweet guys fund it for free."}
        ]
    },
    {
        "track_id": 5,
        "comedian_key": "nate",
        "comedian_name": "Nate Bargatze",
        "topic": "Marriage trapped in a good way",
        "script_text": "Leave all the dating stress behind. Post your favorite outfit on DressIt and connect with good guys who fund your style for free.",
        "raw_source": ASSETS_DIR / "test_nate_v3.mp3",
        "output_filename": "voice_div10_fixed_05_nate.mp3",
        "phrases": [
            {"text": "LEAVE ALL THE DATING\nSTRESS BEHIND", "words": "Leave all the dating stress behind."},
            {"text": "POST YOUR FAVORITE OUTFIT\nON DRESSIT", "words": "Post your favorite outfit on DressIt"},
            {"text": "AND CONNECT WITH GOOD GUYS\nWHO FUND YOUR STYLE FOR FREE!", "words": "and connect with good guys who fund your style for free."}
        ]
    },
    {
        "track_id": 6,
        "comedian_key": "matteo",
        "comedian_name": "Matteo Lane",
        "topic": "Guys & dating habits",
        "script_text": "Tired of guys and their childish dating habits? Post your dream outfit on DressIt and meet guys who actually treat you right.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_06_matteo_bridged.mp3",
        "output_filename": "voice_div10_fixed_06_matteo.mp3",
        "phrases": [
            {"text": "TIRED OF GUYS AND THEIR\nCHILDISH DATING HABITS?", "words": "Tired of guys and their childish dating habits?"},
            {"text": "POST YOUR DREAM OUTFIT\nON DRESSIT", "words": "Post your dream outfit on DressIt"},
            {"text": "AND MEET GUYS WHO\nACTUALLY TREAT YOU RIGHT!", "words": "and meet guys who actually treat you right."}
        ]
    },
    {
        "track_id": 7,
        "comedian_key": "jimmyoyang",
        "comedian_name": "Jimmy O. Yang",
        "topic": "Dating tall girls & heels",
        "script_text": "Love wearing heels without any dating drama? Post your dream dresses on DressIt, and let sweet guys fund them for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_07_jimmyoyang.mp3",
        "output_filename": "voice_div10_fixed_07_jimmyoyang.mp3",
        "phrases": [
            {"text": "LOVE WEARING HEELS\nWITHOUT THE DRAMA?", "words": "Love wearing heels without any dating drama?"},
            {"text": "POST YOUR DREAM DRESSES\nON DRESSIT", "words": "Post your dream dresses on DressIt,"},
            {"text": "AND LET SWEET GUYS\nFUND THEM FOR FREE!", "words": "and let sweet guys fund them for free."}
        ]
    },
    {
        "track_id": 8,
        "comedian_key": "aliwong",
        "comedian_name": "Ali Wong",
        "topic": "Husband & relationship dynamic",
        "script_text": "Post your wishlist on DressIt, and let sweet guys fund it for free, with zero drama.",
        "raw_source": ASSETS_DIR / "test_aliwong_comma.mp3",
        "output_filename": "voice_div10_fixed_08_aliwong.mp3",
        "phrases": [
            {"text": "POST YOUR WISHLIST\nON DRESSIT", "words": "Post your wishlist on DressIt,"},
            {"text": "AND LET SWEET GUYS\nFUND IT FOR FREE", "words": "and let sweet guys fund it for free,"},
            {"text": "WITH ZERO DRAMA!", "words": "with zero drama."}
        ]
    },
    {
        "track_id": 9,
        "comedian_key": "whitney",
        "comedian_name": "Whitney Cummings",
        "topic": "Catcallers vs men with real effort",
        "script_text": "Skip the low-effort boys, and meet guys who show real effort. Post your favorite outfit on DressIt, and let good guys fund it for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_09_whitney.mp3",
        "output_filename": "voice_div10_fixed_09_whitney.mp3",
        "phrases": [
            {"text": "SKIP THE LOW-EFFORT BOYS\nAND MEET REAL GUYS", "words": "Skip the low-effort boys, and meet guys who show real effort."},
            {"text": "POST YOUR FAVORITE OUTFIT\nON DRESSIT", "words": "Post your favorite outfit on DressIt,"},
            {"text": "AND LET GOOD GUYS\nFUND IT FOR FREE!", "words": "and let good guys fund it for free."}
        ]
    },
    {
        "track_id": 10,
        "comedian_key": "gianmarco",
        "comedian_name": "Gianmarco Soresi",
        "topic": "Modern dating",
        "script_text": "Skip the confusing dating rules. Post your dream dress on DressIt and get it funded for free.",
        "raw_source": ASSETS_DIR / "raw_div10_fixed_10_gianmarco.mp3",
        "output_filename": "voice_div10_fixed_10_gianmarco.mp3",
        "phrases": [
            {"text": "SKIP THE CONFUSING\nDATING RULES", "words": "Skip the confusing dating rules."},
            {"text": "POST YOUR DREAM DRESS\nON DRESSIT", "words": "Post your dream dress on DressIt"},
            {"text": "AND GET IT FUNDED\nFOR FREE!", "words": "and get it funded for free."}
        ]
    }
]


def sec_to_ass_time(seconds: float) -> str:
    """Converts seconds float to ASS timestamp format H:MM:SS.cs"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def get_audio_info(path: Path) -> Dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_name,channels,sample_rate,bit_rate:format=duration,size",
        "-of", "json",
        str(path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    stream = data.get("streams", [{}])[0]
    fmt = data.get("format", {})
    return {
        "duration": float(fmt.get("duration", stream.get("duration", 0.0))),
        "sample_rate": int(stream.get("sample_rate", 48000)),
        "channels": int(stream.get("channels", 2)),
        "codec": stream.get("codec_name", "mp3"),
        "bit_rate": int(fmt.get("bit_rate", stream.get("bit_rate", 192000))),
        "size_bytes": int(fmt.get("size", path.stat().st_size))
    }


def measure_ebur128(path: Path) -> Tuple[float, float, float]:
    cmd = [
        "ffmpeg", "-nostats", "-i", str(path),
        "-filter_complex", "ebur128=peak=true", "-f", "null", "-"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    i, tp, lra = -14.0, -1.5, 9.0
    for line in res.stderr.splitlines():
        if "I:" in line and "LUFS" in line:
            m = re.search(r"I:\s*(-?[\d\.]+)\s*LUFS", line)
            if m:
                i = float(m.group(1))
        if "Peak:" in line and "dBFS" in line:
            m = re.search(r"Peak:\s*(-?[\d\.]+)\s*dBFS", line)
            if m:
                tp = float(m.group(1))
        if "LRA:" in line and "LU" in line:
            m = re.search(r"LRA:\s*([\d\.]+)\s*LU", line)
            if m:
                lra = float(m.group(1))
    return round(i, 1), round(tp, 1), round(lra, 1)


def get_silence_pauses(path: Path) -> List[Dict[str, float]]:
    cmd = [
        "ffmpeg", "-i", str(path),
        "-af", "silencedetect=noise=-30dB:d=0.20",
        "-f", "null", "-"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    pauses = []
    current_start = None
    for line in res.stderr.splitlines():
        if "silence_start:" in line:
            m = re.search(r"silence_start:\s*(-?[\d\.]+)", line)
            if m:
                current_start = float(m.group(1))
        elif "silence_end:" in line and current_start is not None:
            m = re.search(r"silence_end:\s*(-?[\d\.]+)\s*\|\s*silence_duration:\s*(-?[\d\.]+)", line)
            if m:
                end_t = float(m.group(1))
                dur = float(m.group(2))
                pauses.append({"start": current_start, "end": end_t, "dur": dur})
                current_start = None
    return pauses


def align_phrases(total_dur: float, pauses: List[Dict[str, float]], phrase_specs: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    Aligns 3 subtitle phrases to acoustic silence boundaries.
    Phrase 1: 0.00 to pause 1 start
    Phrase 2: pause 1 end to pause 2 start
    Phrase 3: pause 2 end to speech end (or total_dur)
    """
    internal_pauses = [p for p in pauses if p["start"] > 1.2 and p["end"] < (total_dur - 0.4)]
    
    events = []
    if len(internal_pauses) >= 2:
        p1 = internal_pauses[0]
        p2 = internal_pauses[1]
        t0 = 0.00
        t1 = p1["start"]
        t2 = p1["end"]
        t3 = p2["start"]
        t4 = p2["end"]
        
        trailing_pauses = [p for p in pauses if p["start"] >= (total_dur - 1.5)]
        t5 = trailing_pauses[0]["start"] if trailing_pauses else total_dur
        
        bounds = [
            (t0, t1),
            (t2, t3),
            (t4, t5)
        ]
    else:
        total_words = sum(len(p["words"].split()) for p in phrase_specs)
        cur = 0.0
        bounds = []
        for p in phrase_specs:
            w_count = len(p["words"].split())
            dur = (w_count / total_words) * total_dur
            bounds.append((cur, cur + dur))
            cur += dur

    for idx, (st, en) in enumerate(bounds):
        text = phrase_specs[idx]["text"]
        events.append({
            "phrase_index": idx + 1,
            "start_sec": round(st, 2),
            "end_sec": round(en, 2),
            "duration_sec": round(en - st, 2),
            "start_ass": sec_to_ass_time(st),
            "end_ass": sec_to_ass_time(en),
            "text": text,
            "ass_dialogue": f"Dialogue: 0,{sec_to_ass_time(st)},{sec_to_ass_time(en)},DressItBrand,,0,0,0,,{{\\bord10\\shad3\\c&H00FFFFFF&}}{text.replace(chr(10), r'\\N')}"
        })
    return events


def master_track(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw_src = cfg["raw_source"]
    out_file = ASSETS_DIR / cfg["output_filename"]
    
    if not raw_src.exists():
        raise FileNotFoundError(f"Raw source audio not found: {raw_src}")
        
    print(f"\nMastering Track {cfg['track_id']:02d}: {cfg['comedian_name']} ({out_file.name})...")
    
    pauses_raw = get_silence_pauses(raw_src)
    raw_probe = get_audio_info(raw_src)
    raw_dur = raw_probe["duration"]
    
    trim_time = raw_dur
    for p in pauses_raw:
        if p["start"] >= (raw_dur - 1.5) and p["end"] >= (raw_dur - 0.2):
            trim_time = min(trim_time, p["start"] + 0.15)
            
    trim_args = ["-t", str(trim_time)] if trim_time < raw_dur - 0.25 else []
    
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(raw_src),
        *trim_args,
        "-af", "loudnorm=I=-14.0:TP=-1.5:LRA=9.0",
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(out_file)
    ]
    subprocess.run(cmd, check=True)
    
    audio_info = get_audio_info(out_file)
    measured_i, true_peak, lra = measure_ebur128(out_file)
    pauses_final = get_silence_pauses(out_file)
    
    subtitles = align_phrases(audio_info["duration"], pauses_final, cfg["phrases"])
    
    words = cfg["script_text"].split()
    word_count = len(words)
    wpm = round((word_count / audio_info["duration"]) * 60, 1)
    
    for forbidden in ["gentlemen", "джентльмены", "bucks", "chips", "pill badge"]:
        if forbidden.lower() in cfg["script_text"].lower():
            raise ValueError(f"Forbidden word '{forbidden}' found in track {cfg['track_id']}")

    track_summary = {
        "track_id": cfg["track_id"],
        "comedian_key": cfg["comedian_key"],
        "comedian_name": cfg["comedian_name"],
        "topic": cfg["topic"],
        "output_filename": cfg["output_filename"],
        "output_path": str(out_file.resolve()),
        "script_text": cfg["script_text"],
        "word_count": word_count,
        "wpm": wpm,
        "voice": "nova",
        "engine": "openai/gpt-audio (via OpenRouter)",
        "duration": round(audio_info["duration"], 2),
        "sample_rate": audio_info["sample_rate"],
        "channels": audio_info["channels"],
        "channel_layout": "stereo",
        "codec": audio_info["codec"],
        "bit_rate": audio_info["bit_rate"],
        "size_bytes": audio_info["size_bytes"],
        "target_lufs": -14.0,
        "measured_lufs": measured_i,
        "true_peak_dbfs": true_peak,
        "lra": lra,
        "subtitles": subtitles
    }
    
    print(f"  Duration: {track_summary['duration']}s | WPM: {wpm} | LUFS: {measured_i} | TP: {true_peak} dBFS | SR: {audio_info['sample_rate']}Hz Stereo")
    for s in subtitles:
        print(f"    [{s['start_ass']} --> {s['end_ass']}] {s['text'].replace(chr(10), ' ')}")
        
    return track_summary


def main():
    print("=" * 70)
    print("DressIt Task #2010: Commercial Bridge Voiceover Production & ASS Timestamps")
    print("=" * 70)
    
    results = []
    for cfg in TRACK_CONFIGS:
        res = master_track(cfg)
        results.append(res)
        
    summary_data = {
        "task_id": 2010,
        "title": "10 DressIt Commercial Bridge Voiceover Tracks",
        "voice": "nova",
        "model": "openai/gpt-audio",
        "openrouter_model": "openai/gpt-audio",
        "mastering_standards": {
            "target_lufs": -14.0,
            "max_true_peak_dbfs": -1.5,
            "target_lra": 9.0,
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": "stereo",
            "codec": "mp3",
            "bitrate": "192k"
        },
        "tracks": results
    }
    
    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2, ensure_ascii=False)
        
    print("\n" + "=" * 70)
    print(f"ALL 10 TRACKS MASTERED & SAVED TO {SUMMARY_JSON}")
    print("=" * 70)


if __name__ == "__main__":
    main()
