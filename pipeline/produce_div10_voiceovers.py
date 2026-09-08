#!/usr/bin/env python3
"""
produce_div10_voiceovers.py — Synthesize and master 10 commercial voiceovers
for diverse comedian standup shorts (Task #2003).

Strict Rules:
1. Engine: Strictly use /root/event-hero/tts_chatgpt.py (model: openai/gpt-audio via OpenRouter, voice: 'nova').
2. Pace: Natural, calm, unhurried 1.0x conversational commercial tempo. DO NOT apply ffmpeg atempo or rushing! Each track ~8.5s - 10.5s.
3. Prompt: "You are a stylish, charming young woman sharing a clever, relatable dating tip with a girlfriend. Speak with natural energy, warmth, and effortless confidence. Calm, crystal-clear conversational commercial tempo."
4. Loudness: EBU R128 loudnorm to -14.0 LUFS (TP <= -1.5 dBFS, LRA 9) at 48000 Hz stereo.
5. Tone: Zero 'gentlemen' (use 'guys', 'men', 'partner'), zero 'bucks' (use 'free'), zero 'chips'.
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path

# Add /root/event-hero to path for tts_chatgpt
sys.path.insert(0, "/root/event-hero")
from tts_chatgpt import synthesize_chatgpt

PROMPT = (
    "You are a stylish, charming young woman sharing a clever, relatable dating tip with a girlfriend. "
    "Speak with natural energy, warmth, and effortless confidence. Calm, crystal-clear conversational commercial tempo."
)

VOICE = "nova"
ASSETS_DIR = Path("/root/dressit-pipeline/assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

TRACKS = [
    {
        "id": 1,
        "comedian": "aliwong",
        "output_filename": "voice_div10_01_aliwong.mp3",
        "text": "Skip the bossy relationship drama completely. Post your dream outfit on DressIt and let sweet guys fund it for free at dressitnow.com!"
    },
    {
        "id": 2,
        "comedian": "iliza",
        "output_filename": "voice_div10_02_iliza.mp3",
        "text": "Tired of low effort hey texts from guys? Post your dream dress on DressIt and let men who actually show effort fund it for free at dressitnow.com!"
    },
    {
        "id": 3,
        "comedian": "nikki",
        "output_filename": "voice_div10_03_nikki.mp3",
        "text": "Stop pretending you do not care about your style. Post the exact outfit you want on DressIt and let great guys fund it for free at dressitnow.com!"
    },
    {
        "id": 4,
        "comedian": "matteo",
        "output_filename": "voice_div10_04_matteo.mp3",
        "text": "Stop dating boys who are stuck in their video games. Post your dream outfit on DressIt and meet guys who actually treat you right at dressitnow.com!"
    },
    {
        "id": 5,
        "comedian": "fortune",
        "output_filename": "voice_div10_05_fortune.mp3",
        "text": "You do not need to be Ashley Davidson to get the best wardrobe. Post your favorite dress on DressIt and let generous guys fund it for free at dressitnow.com!"
    },
    {
        "id": 6,
        "comedian": "ianlara",
        "output_filename": "voice_div10_06_ianlara.mp3",
        "text": "Invest in someone who actually invests in you. Post your style on DressIt and let quality guys fund your outfits for free at dressitnow.com!"
    },
    {
        "id": 7,
        "comedian": "nate",
        "output_filename": "voice_div10_07_nate.mp3",
        "text": "Leave all that awkward ex energy in the past. Post your favorite outfit on DressIt and let good guys fund it with zero drama at dressitnow.com!"
    },
    {
        "id": 8,
        "comedian": "jared",
        "output_filename": "voice_div10_08_jared.mp3",
        "text": "Dating really is better when you look and feel amazing. Post your dream dress on DressIt and let guys fund it for free at dressitnow.com!"
    },
    {
        "id": 9,
        "comedian": "taylor",
        "output_filename": "voice_div10_09_taylor.mp3",
        "text": "Stop settling for guys who put in zero effort. Post your dream outfit on DressIt and let good guys fund it for free at dressitnow.com!"
    },
    {
        "id": 10,
        "comedian": "mattrife",
        "output_filename": "voice_div10_10_mattrife.mp3",
        "text": "Say goodbye to red flag drama once and for all. Post your style on DressIt and meet green flag guys who fund your outfits for free at dressitnow.com!"
    }
]


def probe_audio(path: Path) -> dict:
    """Uses ffprobe to extract stream & format metadata."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=sample_rate,channels,channel_layout,codec_name,duration:format=duration,size,bit_rate",
        "-of", "json",
        str(path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(res.stdout)
    stream = info.get("streams", [{}])[0]
    fmt = info.get("format", {})
    return {
        "duration": float(stream.get("duration") or fmt.get("duration") or 0.0),
        "sample_rate": int(stream.get("sample_rate", 0)),
        "channels": int(stream.get("channels", 0)),
        "channel_layout": stream.get("channel_layout", "stereo"),
        "codec_name": stream.get("codec_name", "mp3"),
        "size_bytes": int(fmt.get("size", 0)),
        "bit_rate": int(fmt.get("bit_rate", 0))
    }


def measure_ebur128(path: Path) -> dict:
    """Measures EBU R128 integrated loudness, True Peak, and LRA."""
    cmd = [
        "ffmpeg", "-nostats", "-i", str(path),
        "-filter_complex", "ebur128=peak=true",
        "-f", "null", "-"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    stderr = res.stderr

    i_val = None
    lra_val = None
    tp_val = None

    lines = stderr.splitlines()
    for idx, line in enumerate(lines):
        if "Integrated loudness:" in line:
            for next_line in lines[idx+1:idx+4]:
                if "I:" in next_line:
                    parts = next_line.split("I:")
                    if len(parts) > 1:
                        try:
                            i_val = float(parts[1].replace("LUFS", "").strip())
                        except ValueError:
                            pass
        if "Loudness range:" in line:
            for next_line in lines[idx+1:idx+4]:
                if "LRA:" in next_line:
                    parts = next_line.split("LRA:")
                    if len(parts) > 1:
                        try:
                            lra_val = float(parts[1].replace("LU", "").strip())
                        except ValueError:
                            pass
        if "True peak:" in line:
            for next_line in lines[idx+1:idx+4]:
                if "Peak:" in next_line:
                    parts = next_line.split("Peak:")
                    if len(parts) > 1:
                        try:
                            tp_val = float(parts[1].replace("dBFS", "").strip())
                        except ValueError:
                            pass

    return {
        "integrated_loudness_lufs": i_val,
        "loudness_range_lra": lra_val,
        "true_peak_dbfs": tp_val
    }


def master_audio(raw_path: Path, out_path: Path) -> None:
    """
    Applies EBU R128 loudnorm to -14.0 LUFS (TP <= -1.5 dBFS, LRA 9)
    at 48000 Hz stereo 192k MP3.
    """
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(raw_path),
        "-af", "loudnorm=I=-14.0:TP=-1.5:LRA=9.0",
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(out_path)
    ]
    subprocess.run(cmd, check=True)


def main():
    print("=" * 70)
    print("DressIt Global Content — Commercial Voiceover Synthesis (Task #2003)")
    print(f"Engine: /root/event-hero/tts_chatgpt.py (openai/gpt-audio, voice: {VOICE})")
    print("Mastering: EBU R128 loudnorm to -14.0 LUFS, TP <= -1.5 dBFS, 48000 Hz stereo")
    print("=" * 70)

    summary_results = []

    for t in TRACKS:
        track_id = t["id"]
        comedian = t["comedian"]
        out_filename = t["output_filename"]
        text = t["text"]
        final_path = ASSETS_DIR / out_filename
        raw_path = ASSETS_DIR / f"raw_div10_{track_id:02d}_{comedian}.mp3"

        print(f"\n[{track_id}/10] Processing Track {track_id} ({comedian})...")
        print(f"Script: \"{text}\"")

        # Step 1: Synthesize with tts_chatgpt.py
        t0 = time.time()
        raw_duration = synthesize_chatgpt(text, str(raw_path), voice=VOICE, prompt=PROMPT)
        gen_time = time.time() - t0
        print(f"  -> Synthesized in {gen_time:.1f}s. Raw duration: {raw_duration:.2f}s")

        # Step 2: Master with EBU R128 loudnorm at 48000 Hz stereo
        master_audio(raw_path, final_path)
        print(f"  -> Mastered to {final_path.name}")

        # Step 3: Probe with ffprobe
        probe_info = probe_audio(final_path)
        ebur_info = measure_ebur128(final_path)

        # Step 4: Cleanup raw files
        if raw_path.exists():
            raw_path.unlink()
        raw_usage = raw_path.with_suffix(".usage.json")
        if raw_usage.exists():
            final_usage = final_path.with_suffix(".usage.json")
            raw_usage.rename(final_usage)

        track_summary = {
            "track_id": track_id,
            "comedian": comedian,
            "output_filename": out_filename,
            "output_path": str(final_path),
            "script_text": text,
            "voice": VOICE,
            "duration": probe_info["duration"],
            "sample_rate": probe_info["sample_rate"],
            "channels": probe_info["channels"],
            "channel_layout": probe_info["channel_layout"],
            "codec": probe_info["codec_name"],
            "size_bytes": probe_info["size_bytes"],
            "bit_rate": probe_info["bit_rate"],
            "measured_lufs": ebur_info["integrated_loudness_lufs"],
            "true_peak_dbfs": ebur_info["true_peak_dbfs"],
            "lra": ebur_info["loudness_range_lra"]
        }
        summary_results.append(track_summary)

        print(f"  -> Verified: {probe_info['duration']:.2f}s | {probe_info['sample_rate']}Hz | {probe_info['channels']}ch | {ebur_info['integrated_loudness_lufs']} LUFS | TP: {ebur_info['true_peak_dbfs']} dBFS")

    # Save summary JSON
    summary_path = ASSETS_DIR / "voice_div10_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "task_id": 2003,
            "title": "10 Diverse Comedian Standup Voiceovers",
            "voice": VOICE,
            "model": "openai/gpt-audio",
            "prompt": PROMPT,
            "target_lufs": -14.0,
            "target_sample_rate": 48000,
            "target_channels": 2,
            "tracks": summary_results
        }, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print(f"All 10 tracks successfully synthesized and mastered!")
    print(f"Metadata saved to: {summary_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
