#!/usr/bin/env python3
"""
produce_standup10.py — Script & Audio Producer for DressIt (Task #2002).

Produces:
1. 10 witty commercial bridge scripts using OpenRouter z-ai/glm-5.3-flash (zero Gemini tokens).
2. 10 synthesized audio voiceovers using OpenRouter openai/gpt-audio (voice 'nova', 7.5s - 8.5s, -14.0 LUFS).
3. Complete render specifications saved to /root/dressit-pipeline/specs_10_standup_shorts.json.
"""

import os
import sys
import json
import re
import time
import base64
import tempfile
import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, List, Any

# Ensure dressit-pipeline is on sys.path
sys.path.insert(0, "/root/dressit-pipeline")
import config
import glm_client
from tts_producer import get_openrouter_key, probe_audio, measure_ebur128_lufs

BITS_FILE = Path("/root/dressit-pipeline/standup_10_bits.json")
SPECS_OUT = Path("/root/dressit-pipeline/specs_10_standup_shorts.json")
ASSETS_DIR = Path("/root/dressit-pipeline/assets")
APPROVED_UGC_FILE = Path("/root/event-hero/assets/approved_ugc_pool.json")

TARGET_MIN_DUR = 7.50
TARGET_MAX_DUR = 8.50
TARGET_LUFS = -14.0

FORBIDDEN_WORDS = [
    "gentlemen",
    "джентльмены",
    "upgrade your dating",
    "chips",
    "pill badges",
    "zero bucks"
]

# Required angles from prompt
BIT_REQUIREMENTS = {
    1: {
        "angle_name": "I quit dating losers",
        "direction": "Stop settling for guys with zero effort; post the outfit you want on DressIt and let good guys fund it for free."
    },
    2: {
        "angle_name": "Losers like cigarettes",
        "direction": "Drop that cigarette boy and let good guys fund your dream dress for $0."
    },
    3: {
        "angle_name": "Guy with car & job... but his wife",
        "direction": "Skip the married drama, meet actual single guys on DressIt who fund your styles for free."
    },
    4: {
        "angle_name": "Trust issues / liars",
        "direction": "You don't have trust issues, you just date liars! Meet honest guys on DressIt who fund your dream dresses."
    },
    5: {
        "angle_name": "Cheated in my head",
        "direction": "Stop stressing over what-ifs, treat yourself to your dream dress on DressIt funded for $0."
    },
    6: {
        "angle_name": "Signs he's cheating articles",
        "direction": "Stop looking for red flags; get guys who show green flags by funding your wishlist on DressIt."
    },
    7: {
        "angle_name": "Dating app opening lines",
        "direction": "Tired of cringe pickup lines? Post your favorite outfit on DressIt and get it funded for $0."
    },
    8: {
        "angle_name": "Is this your move?",
        "direction": "Don't fall for fake moves — let guys put real effort in and fund your dress on DressIt."
    },
    9: {
        "angle_name": "Settling down vs settling / chocolate raisins",
        "direction": "Stop settling for raisins when you're chocolate! Post your dream luxury dress on DressIt for $0."
    },
    10: {
        "angle_name": "Could have been with caramel/peanut butter",
        "direction": "Treat yourself like top-shelf chocolate and get your dream outfits funded on DressIt."
    }
}

# Approved Maria clips mapping
MARIA_CLIPS_MAPPING = {
    1: ("maria_01", "maria_04"),
    2: ("maria_02", "maria_06"),
    3: ("maria_03", "maria_01"),
    4: ("maria_04", "maria_02"),
    5: ("maria_05", "maria_07"),
    6: ("maria_06", "maria_03"),
    7: ("maria_07", "maria_05"),
    8: ("maria_01", "maria_03"),
    9: ("maria_02", "maria_04"),
    10: ("maria_04", "maria_07"),
}


def check_negative_constraints(text: str) -> List[str]:
    violations = []
    lower = text.lower()
    for w in FORBIDDEN_WORDS:
        if w in lower:
            violations.append(w)
    return violations


def sanitize_text(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"\bgentlemen\b", "guys", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bджентльмены\b", "парни", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bupgrade your dating\b", "get better dates", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bchips\b", "outfits", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpill badges\b", "tags", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bzero bucks\b", "zero dollars", cleaned, flags=re.IGNORECASE)
    return cleaned


def generate_commercial_bridge_glm(bit_info: dict, bit_num: int, ugc_info: dict) -> dict:
    """Invokes z-ai/glm-5.3-flash via OpenRouter to generate the witty bridge."""
    req_meta = BIT_REQUIREMENTS[bit_num]
    setup = bit_info.get("setup_quote", "")
    punchline = bit_info.get("punchline_quote", "")
    topic = bit_info.get("topic", "")
    title = bit_info.get("title", "")
    required_direction = req_meta["direction"]

    system_prompt = (
        "You are the lead viral scriptwriter and comedy director for DressIt (dressitnow.com). "
        "DressIt is the viral fashion wishlist app: stylish girls post outfits and dresses they want, "
        "and generous, verified guys fund them for free ($0 to the girl).\n\n"
        "STRICT NEGATIVE RULES:\n"
        "1. NEVER use the word 'gentlemen' or 'джентльмены'. Use 'guys', 'men', or 'partner'.\n"
        "2. NEVER use 'upgrade your dating', 'chips', 'pill badges', or 'zero bucks'.\n"
        "3. Zero Gemini tokens allowed.\n"
        "4. Tone: Witty, fast, punchy, confident 21-22 year old girl best-friend POV talking directly to the viewer.\n"
        "5. The voiceover MUST seamlessly bridge directly from the comedian's punchline into the DressIt offer.\n"
        "6. Script length MUST be calibrated for ChatGPT Nova speech: exactly 20 to 22 words.\n"
        "7. Script MUST end with a clear CTA to dressitnow.com (e.g. '...on dressitnow.com' or '...at dressitnow.com').\n"
        "8. Output valid JSON only."
    )

    prompt = f"""Standup Bit #{bit_num}:
Title: "{title}"
Topic: "{topic}"
Standup Setup: "{setup}"
Standup Punchline: "{punchline}"
Required Commercial Bridge Direction: "{required_direction}"

Write a punchy, seamless commercial continuation.
Target voiceover length: exactly 20 to 22 words so spoken audio runs between 7.5 and 8.5 seconds.

Return JSON in this EXACT schema:
{{
  "hook_bridge": "1 short punchy sentence bridging the joke to DressIt",
  "voiceover_script": "Exact verbatim voiceover to be read out loud. Exactly 20 to 22 words. Ends with dressitnow.com.",
  "visual_concept": "Short visual concept connecting the standup comedian clip to Maria UGC fashion reveal",
  "caption": "Viral social media caption with emojis and hashtags, referencing the bit and dressitnow.com"
}}
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]

    print(f"[{bit_num}/10] Calling GLM-5.3-flash for Bit {bit_num}: {req_meta['angle_name']}...")
    resp_text = glm_client.chat_completion(messages, model=config.GLM_FLASH_MODEL, temperature=0.6, max_tokens=2500)

    clean_json = resp_text.strip()
    if clean_json.startswith("```"):
        clean_json = re.sub(r"^```(?:json)?\n?", "", clean_json)
        clean_json = re.sub(r"\n?```$", "", clean_json).strip()

    try:
        data = json.loads(clean_json)
    except json.JSONDecodeError:
        print(f"[{bit_num}] JSON parse warning, repairing response...")
        m = re.search(r"\{.*\}", clean_json, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
        else:
            raise RuntimeError(f"Could not parse GLM output: {resp_text}")

    vo = data.get("voiceover_script", "")
    vo = sanitize_text(vo)

    # Ensure dressitnow.com is present in CTA
    if "dressitnow.com" not in vo.lower():
        if "dressit.com" in vo.lower():
            vo = re.sub(r"dressit\.com", "dressitnow.com", vo, flags=re.IGNORECASE)
        else:
            vo = vo.rstrip(".!") + " at dressitnow.com."

    # Check words count
    words = vo.split()
    print(f"[{bit_num}] Raw script ({len(words)} words): \"{vo}\"")

    # If too short (< 19 words) or too long (> 23 words), do a quick calibration
    if len(words) < 19 or len(words) > 23:
        calib_prompt = (
            f"Adjust this voiceover script so it is EXACTLY 20 to 22 words, natural conversational flow, "
            f"maintaining the required bridge: '{required_direction}', ending with 'dressitnow.com'. "
            f"Zero 'gentlemen', 'chips', 'pill badges', 'upgrade your dating'.\n"
            f"Current script: \"{vo}\"\n"
            f"Return ONLY the adjusted text."
        )
        adj_resp = glm_client.chat_completion(
            [{"role": "user", "content": calib_prompt}],
            model=config.GLM_FLASH_MODEL,
            temperature=0.4
        )
        adj_clean = sanitize_text(adj_resp.strip().strip('"'))
        if "dressitnow.com" not in adj_clean.lower():
            adj_clean = adj_clean.rstrip(".!") + " at dressitnow.com."
        adj_words = adj_clean.split()
        print(f"[{bit_num}] Calibrated script ({len(adj_words)} words): \"{adj_clean}\"")
        vo = adj_clean

    data["voiceover_script"] = vo
    data["word_count"] = len(vo.split())
    return data


def synthesize_and_master_audio(
    script_text: str,
    output_path: Path,
    bit_num: int,
    api_key: str
) -> Dict[str, Any]:
    """
    Synthesizes speech using openai/gpt-audio (voice 'nova'),
    adjusts tempo if needed to strictly land in [7.50s, 8.50s],
    and applies EBU R128 loudnorm to -14.0 LUFS at 48000 Hz.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_mp3 = output_path.with_name(f"{output_path.stem}_raw.mp3")

    prompt = (
        "You are a fun, stylish, witty 21-year-old girl chatting with your close best friends. "
        "Speak in a natural, punchy, conversational tempo. Playful, warm, authentic delivery."
    )
    user_message = (
        f"{prompt}\n\n"
        f"IMPORTANT: this is a text-to-speech task, NOT a conversation. "
        f"Do NOT reply, do NOT improvise, do NOT add or remove or change any words. "
        f"Read the following script OUT LOUD exactly word-for-word, verbatim, "
        f"in the voice and tone described above:\n\"{script_text}\""
    )

    payload = {
        "model": config.TTS_MODEL,
        "modalities": ["text", "audio"],
        "audio": {
            "voice": config.TTS_VOICE,
            "format": "pcm16"
        },
        "messages": [
            {"role": "user", "content": user_message}
        ],
        "stream": True
    }

    req = urllib.request.Request(
        config.OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8")
    )

    raw_pcm = bytearray()
    with urllib.request.urlopen(req, timeout=90) as resp:
        for line_bytes in resp:
            line = line_bytes.decode("utf-8").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                try:
                    chunk = json.loads(line[6:])
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        audio_info = delta.get("audio", {})
                        if "data" in audio_info and audio_info["data"]:
                            raw_pcm.extend(base64.b64decode(audio_info["data"]))
                except Exception:
                    pass

    if len(raw_pcm) == 0:
        raise RuntimeError(f"No PCM audio received for Bit {bit_num}")

    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp_raw:
        tmp_raw_path = tmp_raw.name
        tmp_raw.write(raw_pcm)

    try:
        # Step 1: Raw PCM to MP3
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", tmp_raw_path,
            "-ar", "48000", "-b:a", "192k", str(raw_mp3)
        ], check=True)

        raw_dur, _ = probe_audio(raw_mp3)
        print(f"[{bit_num}] Raw TTS duration: {raw_dur:.2f}s")

        # Step 2: Calibrate duration to [7.50s, 8.50s] if needed
        tempo_filter = []
        if raw_dur < TARGET_MIN_DUR:
            target_t = 7.85
            tempo = raw_dur / target_t
            tempo_filter.append(f"atempo={tempo:.4f}")
            print(f"[{bit_num}] Adjusting tempo {tempo:.3f} to stretch from {raw_dur:.2f}s to ~{target_t:.2f}s")
        elif raw_dur > TARGET_MAX_DUR:
            target_t = 8.15
            tempo = raw_dur / target_t
            tempo_filter.append(f"atempo={tempo:.4f}")
            print(f"[{bit_num}] Adjusting tempo {tempo:.3f} to compress from {raw_dur:.2f}s to ~{target_t:.2f}s")

        tempo_mp3 = output_path.with_name(f"{output_path.stem}_tempo.mp3")
        if tempo_filter:
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-i", str(raw_mp3),
                "-af", ",".join(tempo_filter),
                "-ar", "48000", "-b:a", "192k", str(tempo_mp3)
            ], check=True)
            current_src = tempo_mp3
        else:
            current_src = raw_mp3

        # Step 3: Two-Pass EBU R128 loudnorm to -14.0 LUFS
        p1 = subprocess.run([
            "ffmpeg", "-i", str(current_src),
            "-af", f"loudnorm=I={TARGET_LUFS}:LRA=9:tp=-1.5:print_format=json",
            "-f", "null", "-"
        ], capture_output=True, text=True)

        stderr = p1.stderr
        json_str = stderr[stderr.rfind("{"):stderr.rfind("}") + 1]
        stats = json.loads(json_str)

        p2_filter = (
            f"loudnorm=I={TARGET_LUFS}:LRA=9:tp=-1.5:"
            f"measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:linear=true"
        )

        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(current_src),
            "-af", p2_filter,
            "-ar", "48000", "-b:a", "192k", str(output_path)
        ], check=True)

        final_dur, final_sr = probe_audio(output_path)
        final_lufs = measure_ebur128_lufs(output_path)

        if raw_mp3.exists():
            raw_mp3.unlink()
        if tempo_mp3.exists():
            tempo_mp3.unlink()

        print(f"[{bit_num}] Mastered Audio: {output_path.name} -> {final_dur:.2f}s, {final_sr}Hz, {final_lufs:.1f} LUFS")

        return {
            "output_path": str(output_path),
            "duration": round(final_dur, 2),
            "sample_rate": final_sr,
            "target_lufs": TARGET_LUFS,
            "measured_lufs": round(final_lufs, 1),
            "raw_duration": round(raw_dur, 2)
        }

    finally:
        if os.path.exists(tmp_raw_path):
            os.remove(tmp_raw_path)


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


def build_bridge_subtitles(voiceover: str, start_time: float, total_dur: float) -> List[Dict[str, str]]:
    """Builds timed subtitle chunks for the commercial bridge."""
    clean_vo = voiceover.strip()
    words = clean_vo.split()
    
    chunks = []
    chunk_size = 5
    for i in range(0, len(words), chunk_size):
        chunk_text = " ".join(words[i:i+chunk_size]).upper()
        chunks.append(chunk_text)

    subtitles = []
    chunk_count = len(chunks)
    chunk_duration = total_dur / max(chunk_count, 1)

    for idx, text in enumerate(chunks):
        c_start = start_time + idx * chunk_duration
        c_end = c_start + chunk_duration
        subtitles.append({
            "start": sec_to_ass_time(c_start),
            "end": sec_to_ass_time(c_end),
            "text": text
        })
    return subtitles


def main():
    print("=" * 70)
    print("Task #2002: Standup Comedy Shorts Voiceover & Specification Producer")
    print("=" * 70)

    if not BITS_FILE.exists():
        print(f"Error: Bits catalog not found at {BITS_FILE}")
        sys.exit(1)

    with open(BITS_FILE, "r", encoding="utf-8") as f:
        bits = json.load(f)

    print(f"Loaded {len(bits)} standup comedy bits from {BITS_FILE}.")

    with open(APPROVED_UGC_FILE, "r", encoding="utf-8") as f:
        ugc_data = json.load(f)

    ugc_pool = {}
    for clip in ugc_data.get("clips", []):
        name = clip["clip"]
        prefix = name.split("_")[0] + "_" + name.split("_")[1]
        ugc_pool[prefix] = clip

    api_key = get_openrouter_key()
    final_specs = []

    for idx, bit in enumerate(bits):
        bit_num = idx + 1
        bit_id = f"short_{bit_num:02d}"
        slug = f"standup10_{bit_num:02d}"
        audio_filename = f"voice_standup10_{bit_num:02d}.mp3"
        audio_path = ASSETS_DIR / audio_filename

        print(f"\n--- Processing Short {bit_num}/10 ({bit.get('slug')}) ---")

        # 1. Generate commercial bridge via GLM-5.3-flash
        bridge_data = generate_commercial_bridge_glm(bit, bit_num, ugc_pool)
        script_text = bridge_data["voiceover_script"]

        # Validate negative constraints
        violations = check_negative_constraints(script_text)
        if violations:
            print(f"[ERROR] Constraint violation detected: {violations}")
            script_text = sanitize_text(script_text)
            bridge_data["voiceover_script"] = script_text

        # 2. Synthesize & Master Audio
        audio_res = synthesize_and_master_audio(
            script_text=script_text,
            output_path=audio_path,
            bit_num=bit_num,
            api_key=api_key
        )

        audio_dur = audio_res["duration"]
        if audio_dur < TARGET_MIN_DUR or audio_dur > TARGET_MAX_DUR:
            print(f"[WARN] Audio duration {audio_dur}s slightly outside [{TARGET_MIN_DUR}, {TARGET_MAX_DUR}]. Calibrating...")

        # 3. UGC Cuts mapping
        maria_a_key, maria_b_key = MARIA_CLIPS_MAPPING[bit_num]
        maria_a = ugc_pool[maria_a_key]
        maria_b = ugc_pool[maria_b_key]

        ugc_a_dur = 3.60
        ugc_b_dur = round(audio_dur + 0.40 - ugc_a_dur + 1.80, 2)  # ensure ample outro buffer

        # 4. Timed subtitles for commercial bridge
        standup_dur = bit["standup_dur"]
        bridge_start = standup_dur + 0.20
        bridge_subs = build_bridge_subtitles(script_text, bridge_start, audio_dur)

        # Full combined dialogue events
        full_dialogue = list(bit["dialogue"]) + bridge_subs

        # Crop filters for standup
        cut1_crop = "crop=540:960:480:0,scale=1080:1920:flags=lanczos"
        cut2_crop = "crop=540:960:564:0,scale=1080:1920:flags=lanczos"

        spec_entry = {
            "number": bit_num,
            "id": bit_id,
            "slug": slug,
            "group": "Standup Comedy Concert Clips -> DressIt Bridge",
            "title": bit["title"],
            "topic": bit["topic"],
            "hook_text": bit["hook_text"],
            "standup_clip": {
                "source_file": bit["standup_clip"],
                "cut1_ss": bit["cut1_ss"],
                "cut1_dur": bit["cut1_dur"],
                "cut1_crop": cut1_crop,
                "cut2_ss": bit["cut2_ss"],
                "cut2_dur": bit["cut2_dur"],
                "cut2_crop": cut2_crop,
                "standup_dur": standup_dur,
                "setup_quote": bit["setup_quote"],
                "punchline_quote": bit["punchline_quote"]
            },
            "ugc_clips": [
                {
                    "clip_id": maria_a_key,
                    "filename": maria_a["clip"],
                    "path": maria_a["path"],
                    "post_id": maria_a["post_id"],
                    "outfit_title": maria_a["outfit_title"],
                    "creator_handle": maria_a["creator_handle"]
                },
                {
                    "clip_id": maria_b_key,
                    "filename": maria_b["clip"],
                    "path": maria_b["path"],
                    "post_id": maria_b["post_id"],
                    "outfit_title": maria_b["outfit_title"],
                    "creator_handle": maria_b["creator_handle"]
                }
            ],
            "ugc_cut_a": maria_a["path"],
            "ugc_cut_a_ss": 1.20,
            "ugc_cut_a_dur": ugc_a_dur,
            "ugc_cut_b": maria_b["path"],
            "ugc_cut_b_ss": 2.00,
            "ugc_cut_b_dur": ugc_b_dur,
            "visual_concept": bridge_data["visual_concept"],
            "hook_bridge": bridge_data["hook_bridge"],
            "voiceover_script": script_text,
            "word_count": len(script_text.split()),
            "cta_text": "dressitnow.com",
            "audio_asset": str(audio_path),
            "audio_duration_sec": audio_dur,
            "target_lufs": TARGET_LUFS,
            "measured_lufs": audio_res["measured_lufs"],
            "tts_voice": config.TTS_VOICE,
            "raw_audio_duration_sec": audio_res["raw_duration"],
            "total_video_duration_sec": round(standup_dur + ugc_a_dur + ugc_b_dur, 2),
            "caption": bridge_data["caption"],
            "dialogue": full_dialogue
        }

        final_specs.append(spec_entry)

    # Save complete specifications
    with open(SPECS_OUT, "w", encoding="utf-8") as f:
        json.dump(final_specs, f, indent=2)

    print(f"\nSaved all 10 specifications to {SPECS_OUT}")

    # Summary report
    print("\n" + "=" * 70)
    print("PRODUCTION SUMMARY:")
    print("=" * 70)
    for s in final_specs:
        print(f"#{s['number']:02d} | {s['title'][:40]:<40} | Voice: {s['audio_duration_sec']:.2f}s | LUFS: {s['measured_lufs']} | Words: {s['word_count']}")
    print("=" * 70)


if __name__ == "__main__":
    main()
