#!/usr/bin/env python3
"""
generate_10_scripts_glm.py

Generates 10 diverse, high-converting video concepts and scripts for DressIt
using OpenRouter `z-ai/glm-5.3-flash` (or `z-ai/glm-5.3`) and synthesizes their
commercial audio voiceovers with `openai/gpt-audio` (voice: 'nova', 7.5s - 8.5s duration, -14 LUFS).

Constraints:
1. LLM Engine: OpenRouter with model `z-ai/glm-5.3-flash` using /root/.openrouter.key. ZERO Gemini tokens.
2. TTS Engine: OpenRouter `openai/gpt-audio` (voice: 'nova', natural unhurried conversational tempo, 7.5s - 8.5s, -14 LUFS).
3. Tone & Wording:
   - Relatable, fun, conversational dating and style humor.
   - Natural vocabulary: "guys", "men", "partner", "date".
   - STRICTLY FORBIDDEN: "gentlemen", "upgrade your dating", "chips", "pill badges", "zero bucks".
4. UGC Visuals:
   - ONLY approved Maria UGC clips from /root/event-hero/assets/approved_ugc_pool.json:
     maria_01, maria_02, maria_03, maria_04, maria_05, maria_06, maria_07.
   - NEVER use maria_10 or quarantined clips (maria_13..21).
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

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = "z-ai/glm-5.3-flash"
TTS_MODEL = "openai/gpt-audio"
TTS_VOICE = "nova"

TARGET_MIN_DUR = 7.50
TARGET_MAX_DUR = 8.50
TARGET_LUFS = -14.0

FORBIDDEN_PHRASES = [
    "gentlemen",
    "upgrade your dating",
    "chips",
    "pill badges",
    "zero bucks"
]

APPROVED_UGC_FILE = Path("/root/event-hero/assets/approved_ugc_pool.json")
STANDUP_CLIP_PATH = "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4"
SPECS_OUT_PATH = Path("/root/dressit-pipeline/specs_10_shorts.json")
ASSETS_DIR = Path("/root/dressit-pipeline/assets")


def get_openrouter_key() -> str:
    key_file = Path("/root/.openrouter.key")
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    env_key = os.getenv("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    raise RuntimeError("OpenRouter API key not found in /root/.openrouter.key or OPENROUTER_API_KEY")


def load_approved_ugc_pool() -> dict:
    with open(APPROVED_UGC_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    pool = {}
    for clip in data.get("clips", []):
        name = clip["clip"]
        prefix = name.split("_")[0] + "_" + name.split("_")[1]  # e.g. maria_01
        pool[prefix] = clip
    return pool


def check_forbidden(text: str) -> list:
    lower = text.lower()
    found = []
    for p in FORBIDDEN_PHRASES:
        if p in lower:
            found.append(p)
    return found


def prompt_glm_for_scripts(api_key: str, ugc_pool: dict) -> list:
    """Prompts z-ai/glm-5.3-flash via OpenRouter to refine and finalize 10 scripts."""
    print(f"[LLM] Prompting {LLM_MODEL} via OpenRouter...")

    system_prompt = (
        "You are the Lead Creative & Script Producer for DressIt.\n"
        "DressIt is a viral shopping & dating wishlist app: women post outfits and dresses they love, and generous, high-effort guys fund them for $0 to the girl.\n"
        "Your task is to refine and finalize 10 high-converting video concepts and voiceover scripts for TikTok, Reels, and Shorts.\n\n"
        "STRICT TONE & LANGUAGE CONSTRAINTS:\n"
        "- Relatable, funny, conversational dating and style humor from a stylish 22-year-old female best-friend POV.\n"
        "- Natural vocabulary: 'guys', 'men', 'partner', 'date'.\n"
        "- STRICTLY FORBIDDEN WORDS (NEVER USE ANYWHERE): 'gentlemen', 'upgrade your dating', 'chips', 'pill badges', 'zero bucks'.\n"
        "- TTS CALIBRATION: Each voiceover script MUST be calibrated for an unhurried, natural conversational tempo lasting 7.5 to 8.5 seconds.\n"
        "- Optimum script length is between 18 and 24 words. Exactly 1 or 2 clean, punchy sentences.\n"
        "- Visuals must strictly reference approved Maria clips (maria_01 through maria_07 only; never maria_10 or quarantined clips).\n"
        "- Return ONLY valid JSON array with 10 objects."
    )

    concepts_input = [
        {
            "num": 1,
            "group": "Group 1: Dating & Red-Flag Comedy -> DressIt Bridge",
            "concept": "Standup clip from /root/dressit-shorts/clips/oagNYHB3Kzk.mp4 (Setup 4.18s-6.55s, Punchline 6.55s-12.4s: 'I quit dating losers!'). Bridge: Stop settling for low-effort boys; post the dress you want on DressIt and let guys fund it for $0.",
            "maria_clip": "maria_01"
        },
        {
            "num": 2,
            "group": "Group 1: Dating & Red-Flag Comedy -> DressIt Bridge",
            "concept": "Standup clip from /root/dressit-shorts/clips/oagNYHB3Kzk.mp4 (ss=19.10s-27.85s: 'Losers are like cigarettes...'). Bridge: Seriously, drop that cigarette boy and let good guys fund your dream dress for $0.",
            "maria_clip": "maria_02"
        },
        {
            "num": 3,
            "group": "Group 1: Dating & Red-Flag Comedy -> DressIt Bridge",
            "concept": "Standup clip from /root/dressit-shorts/clips/oagNYHB3Kzk.mp4 (ss=32.12s-40.52s: 'Guy with a car and a job... but his wife!'). Bridge: Skip the married drama, meet actual single guys who fund your outfits on DressIt.",
            "maria_clip": "maria_03"
        },
        {
            "num": 4,
            "group": "Group 2: Girl Math & Shopping Confessions",
            "concept": "Girl math: If a dress costs $250 and you wear it once, it costs $250. But if guys fund it on DressIt, it literally costs $0!",
            "maria_clip": "maria_04"
        },
        {
            "num": 5,
            "group": "Group 2: Girl Math & Shopping Confessions",
            "concept": "Stop staring at your Zara cart waiting for a sale. Post your wishlist on DressIt and get it funded tonight.",
            "maria_clip": "maria_05"
        },
        {
            "num": 6,
            "group": "Group 2: Girl Math & Shopping Confessions",
            "concept": "Buying a dress for someone else's wedding you didn't even want to go to? Let DressIt fund the outfit for $0.",
            "maria_clip": "maria_06"
        },
        {
            "num": 7,
            "group": "Group 3: Interactive Dress Rating & Voting",
            "concept": "Which dress should I get funded for Friday night? 1, 2, or 3 in the comments!",
            "maria_clips": ["maria_01", "maria_02", "maria_04"]
        },
        {
            "num": 8,
            "group": "Group 3: Interactive Dress Rating & Voting",
            "concept": "Rating 3 viral dresses! Drop your favorite in the comments and get yours funded on DressIt.",
            "maria_clips": ["maria_03", "maria_06", "maria_07"]
        },
        {
            "num": 9,
            "group": "Group 4: Dating Green Flags & Wishlist Storytime",
            "concept": "The biggest green flag in 2026: He doesn't ask what you want, he just funds your DressIt wishlist.",
            "maria_clip": "maria_07"
        },
        {
            "num": 10,
            "group": "Group 4: Dating Green Flags & Wishlist Storytime",
            "concept": "Tired of talking stages that lead nowhere? Post your favorite outfit on DressIt and meet guys who actually show effort.",
            "maria_clip": "maria_03"
        }
    ]

    prompt = (
        f"{system_prompt}\n\n"
        f"Input Concepts to finalize:\n{json.dumps(concepts_input, indent=2)}\n\n"
        f"Output JSON Schema:\n"
        "[\n"
        "  {\n"
        '    "id": "short_01",\n'
        '    "group": "Group 1: Dating & Red-Flag Comedy -> DressIt Bridge",\n'
        '    "title": "...",\n'
        '    "hook_text": "...",\n'
        '    "visual_concept": "...",\n'
        '    "used_maria_clips": ["maria_01"],\n'
        '    "voiceover_script": "...",\n'
        '    "cta_text": "free dress, good men -> dressit.com"\n'
        "  },\n"
        "  ...\n"
        "]\n\n"
        "Remember: voiceover_script MUST be 18-24 words, absolutely zero forbidden words!"
    )

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.6
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8")
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        res = json.loads(resp.read().decode("utf-8"))

    content = res["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content).strip()

    items = json.loads(content)
    print(f"[LLM] Received {len(items)} scripts from {LLM_MODEL}.")
    return items


def sanitize_and_calibrate_script(item: dict) -> dict:
    """Ensures forbidden words are absent and word counts are tuned for 7.5s - 8.5s speech."""
    script = item["voiceover_script"]
    forbidden = check_forbidden(script)
    if forbidden:
        print(f"[WARN] Forbidden phrase {forbidden} found in {item['id']}, replacing...")
        script = re.sub(r"\bgentlemen\b", "men", script, flags=re.IGNORECASE)
        script = re.sub(r"\bupgrade your dating\b", "find better dates", script, flags=re.IGNORECASE)
        script = re.sub(r"\bchips\b", "outfits", script, flags=re.IGNORECASE)
        script = re.sub(r"\bpill badges\b", "tags", script, flags=re.IGNORECASE)
        script = re.sub(r"\bzero bucks\b", "zero dollars", script, flags=re.IGNORECASE)
        item["voiceover_script"] = script

    words = script.split()
    item["word_count"] = len(words)
    return item


def synthesize_and_normalize(api_key: str, text: str, out_path: Path, short_id: str) -> dict:
    """
    Synthesizes speech using OpenRouter openai/gpt-audio with voice 'nova'.
    Then normalizes duration to [7.5s, 8.5s] and loudness to -14 LUFS.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_mp3 = out_path.with_name(f"{out_path.stem}_raw.mp3")

    prompt = (
        "You are a relatable, confident, stylish 22-year-old girl in New York talking naturally with close friends. "
        "Natural unhurried conversational tempo. Warm, playful, authentic tone."
    )
    user_message = (
        f"{prompt}\n\n"
        f"IMPORTANT: this is a text-to-speech task, NOT a conversation. "
        f"Do NOT reply, do NOT improvise, do NOT add or remove or change any words. "
        f"Read the following script OUT LOUD exactly word-for-word, verbatim, "
        f"in the voice and tone described above:\n\"{text}\""
    )

    payload = {
        "model": TTS_MODEL,
        "modalities": ["text", "audio"],
        "audio": {
            "voice": TTS_VOICE,
            "format": "pcm16"
        },
        "messages": [
            {"role": "user", "content": user_message}
        ],
        "stream": True
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
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
        raise RuntimeError(f"No PCM audio received for {short_id}")

    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp_raw:
        tmp_raw_path = tmp_raw.name
        tmp_raw.write(raw_pcm)

    try:
        # Step 1: PCM -> MP3
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", tmp_raw_path,
            "-ar", "44100", "-b:a", "192k", str(raw_mp3)
        ], check=True)

        raw_dur = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(raw_mp3)
        ]).decode().strip())
        print(f"[{short_id}] Raw TTS duration: {raw_dur:.2f}s")

        # Step 2: Calibrate duration to [7.5s, 8.5s] if necessary using atempo
        tempo_filter = []
        if raw_dur < TARGET_MIN_DUR:
            target_t = 7.80
            tempo = raw_dur / target_t
            tempo_filter.append(f"atempo={tempo:.4f}")
            print(f"[{short_id}] Adjusting tempo {tempo:.3f} to stretch to {target_t:.2f}s")
        elif raw_dur > TARGET_MAX_DUR:
            target_t = 8.10
            tempo = raw_dur / target_t
            tempo_filter.append(f"atempo={tempo:.4f}")
            print(f"[{short_id}] Adjusting tempo {tempo:.3f} to compress to {target_t:.2f}s")

        tempo_mp3 = out_path.with_name(f"{out_path.stem}_tempo.mp3")
        if tempo_filter:
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-i", str(raw_mp3),
                "-af", ",".join(tempo_filter),
                "-ar", "44100", "-b:a", "192k", str(tempo_mp3)
            ], check=True)
            current_src = tempo_mp3
        else:
            current_src = raw_mp3

        # Step 3: Two-Pass EBU R128 loudnorm to exactly -14 LUFS
        p1 = subprocess.run([
            "ffmpeg", "-i", str(current_src),
            "-af", f"loudnorm=I={TARGET_LUFS}:LRA=7:tp=-1:print_format=json",
            "-f", "null", "-"
        ], capture_output=True, text=True)

        stderr = p1.stderr
        json_str = stderr[stderr.rfind("{"):stderr.rfind("}") + 1]
        stats = json.loads(json_str)

        p2_filter = (
            f"loudnorm=I={TARGET_LUFS}:LRA=7:tp=-1:"
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
            "-ar", "44100", "-b:a", "192k", str(out_path)
        ], check=True)

        final_dur = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)
        ]).decode().strip())

        if raw_mp3.exists():
            raw_mp3.unlink()
        if tempo_mp3.exists():
            tempo_mp3.unlink()

        print(f"[{short_id}] Normalized Audio Saved: {out_path.name} (Duration: {final_dur:.2f}s, Target: -14 LUFS)")
        return {
            "duration": round(final_dur, 2),
            "lufs_target": TARGET_LUFS,
            "raw_duration": round(raw_dur, 2)
        }

    finally:
        if os.path.exists(tmp_raw_path):
            os.remove(tmp_raw_path)


def build_full_specifications(glm_items: list, ugc_pool: dict) -> list:
    """Builds comprehensive specification dicts for all 10 shorts."""
    full_specs = []

    standup_meta = {
        "short_01": {
            "source_file": STANDUP_CLIP_PATH,
            "start_sec": 4.18,
            "end_sec": 12.40,
            "duration_sec": 8.22,
            "setup_timing": "4.18s-6.55s",
            "punchline_timing": "6.55s-12.40s",
            "quote": "I quit dating losers!"
        },
        "short_02": {
            "source_file": STANDUP_CLIP_PATH,
            "start_sec": 19.10,
            "end_sec": 27.85,
            "duration_sec": 8.75,
            "setup_timing": "19.10s-22.50s",
            "punchline_timing": "22.50s-27.85s",
            "quote": "Losers are like cigarettes..."
        },
        "short_03": {
            "source_file": STANDUP_CLIP_PATH,
            "start_sec": 32.12,
            "end_sec": 40.52,
            "duration_sec": 8.40,
            "setup_timing": "32.12s-35.80s",
            "punchline_timing": "35.80s-40.52s",
            "quote": "Guy with a car and a job... but his wife!"
        }
    }

    for idx, item in enumerate(glm_items):
        s_id = item["id"]
        maria_ids = item.get("used_maria_clips", [])
        if isinstance(maria_ids, str):
            maria_ids = [maria_ids]

        resolved_clips = []
        for mid in maria_ids:
            if mid in ugc_pool:
                c = ugc_pool[mid]
                resolved_clips.append({
                    "clip_id": mid,
                    "filename": c["clip"],
                    "path": c["path"],
                    "post_id": c.get("post_id"),
                    "outfit_title": c.get("outfit_title", ""),
                    "creator_handle": c.get("creator_handle", "")
                })

        spec = {
            "id": s_id,
            "number": idx + 1,
            "group": item.get("group", ""),
            "title": item.get("title", f"Short {idx+1}"),
            "hook_text": item.get("hook_text", ""),
            "standup_clip": standup_meta.get(s_id, None),
            "ugc_clips": resolved_clips,
            "visual_concept": item.get("visual_concept", ""),
            "voiceover_script": item.get("voiceover_script", ""),
            "word_count": len(item.get("voiceover_script", "").split()),
            "cta_text": item.get("cta_text", "free dress, good men -> dressit.com"),
            "audio_asset": f"/root/dressit-pipeline/assets/voice_batch10_{idx+1:02d}.mp3",
            "audio_duration_sec": None,
            "target_lufs": -14.0,
            "tts_voice": TTS_VOICE
        }
        full_specs.append(spec)

    return full_specs


def main():
    print("=== DressIt 10 Video Concepts & Audio Voiceover Generator ===")
    api_key = get_openrouter_key()
    ugc_pool = load_approved_ugc_pool()
    print(f"Loaded {len(ugc_pool)} approved Maria UGC clips from {APPROVED_UGC_FILE}")

    glm_items = prompt_glm_for_scripts(api_key, ugc_pool)
    if len(glm_items) != 10:
        print(f"[WARN] GLM returned {len(glm_items)} items instead of 10.")

    cleaned_items = [sanitize_and_calibrate_script(it) for it in glm_items]
    specs = build_full_specifications(cleaned_items, ugc_pool)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n=== Synthesizing & Normalizing 10 Voiceovers with openai/gpt-audio ===")
    for spec in specs:
        s_id = spec["id"]
        out_file = Path(spec["audio_asset"])
        script_text = spec["voiceover_script"]
        print(f"\nProcessing {s_id}: \"{script_text}\" ({spec['word_count']} words)")

        audio_res = synthesize_and_normalize(api_key, script_text, out_file, s_id)
        spec["audio_duration_sec"] = audio_res["duration"]
        spec["raw_audio_duration_sec"] = audio_res["raw_duration"]

        assert TARGET_MIN_DUR <= spec["audio_duration_sec"] <= TARGET_MAX_DUR, (
            f"Duration {spec['audio_duration_sec']} out of bounds [{TARGET_MIN_DUR}, {TARGET_MAX_DUR}] for {s_id}"
        )

    SPECS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SPECS_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(specs, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Saved complete specifications to {SPECS_OUT_PATH}")

    print("\n" + "=" * 80)
    print(f"{'ID':<10} | {'DUR (s)':<8} | {'WORDS':<6} | {'VOICEOVER SCRIPT'}")
    print("-" * 80)
    for s in specs:
        print(f"{s['id']:<10} | {s['audio_duration_sec']:<8.2f} | {s['word_count']:<6} | {s['voiceover_script']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
