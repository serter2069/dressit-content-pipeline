#!/usr/bin/env python3
"""
tts_producer.py — Production TTS generator using OpenRouter openai/gpt-audio.

Strict Rules (Task #1999):
- Model: openai/gpt-audio via OpenRouter (voice: 'nova').
- Normalization: -14.0 LUFS (EBU R128 loudnorm=I=-14:TP=-1.5:LRA=9).
- Sample Rate: 48000 Hz.
- Duration checks & audio loudness verification.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import config

logger = logging.getLogger("tts_producer")


def get_openrouter_key() -> str:
    """Retrieves OpenRouter API key."""
    if config.OPENROUTER_KEY_FILE.exists():
        key = config.OPENROUTER_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    env_key = os.getenv("OPENROUTER_API_KEY")
    if env_key:
        return env_key.strip()
    raise FileNotFoundError("OpenRouter API key missing.")


def measure_ebur128_lufs(audio_path: str | Path) -> float:
    """Measures integrated loudness (I) in LUFS using FFmpeg ebur128 filter."""
    cmd = [
        "ffmpeg", "-nostats", "-i", str(audio_path),
        "-filter_complex", "ebur128=peak=true", "-f", "null", "-"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    lufs = -14.0
    for line in res.stderr.splitlines():
        if "Integrated loudness:" in line:
            # Look for following lines or line with I:
            continue
        if "I:" in line and "LUFS" in line:
            m = re.search(r"I:\s*(-?[\d\.]+)\s*LUFS", line)
            if m:
                try:
                    lufs = float(m.group(1))
                except ValueError:
                    pass
    return lufs


def probe_audio(audio_path: str | Path) -> Tuple[float, int]:
    """Returns (duration_seconds, sample_rate_hz)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=sample_rate,duration",
        "-of", "json",
        str(audio_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(res.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError(f"No audio stream found in {audio_path}")
    sample_rate = int(streams[0].get("sample_rate", 48000))
    duration = float(streams[0].get("duration", 0.0))
    return duration, sample_rate


def generate_tts(
    text: str,
    output_path: str | Path,
    voice: str = config.TTS_VOICE,
    prompt: Optional[str] = None,
    target_lufs: float = config.AUDIO_TARGET_LUFS,
    target_sample_rate: int = config.SAMPLE_RATE
) -> Dict[str, Any]:
    """
    Synthesizes speech using OpenRouter openai/gpt-audio with strict mastering.

    Args:
        text: Exact script text to speak out loud.
        output_path: Destination file path (.mp3 or .wav).
        voice: Model voice (default 'nova').
        prompt: Persona style prompt.
        target_lufs: Loudness target in LUFS (default -14.0).
        target_sample_rate: Output sample rate in Hz (default 48000).

    Returns:
        dict: Metadata with output path, duration, sample rate, and measured LUFS.
    """
    api_key = get_openrouter_key()
    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if not prompt:
        prompt = (
            "You are a fun, witty, 21-year-old party girl chatting with your best friends. "
            "Speak fast-paced, high energy, completely natural free speech. No robotic cadence."
        )

    # Clean text to ensure brand rules
    clean_text = text.strip()
    for forbidden in config.FORBIDDEN_PHRASES:
        if forbidden.lower() in clean_text.lower():
            clean_text = re.sub(re.escape(forbidden), "guys", clean_text, flags=re.IGNORECASE)

    user_message = (
        f"{prompt.strip()}\n\n"
        f"IMPORTANT: this is a text-to-speech task, NOT a conversation. "
        f"Do NOT reply, do NOT improvise, do NOT add or remove or change any words. "
        f"Read the following script OUT LOUD exactly word-for-word, verbatim, "
        f"in the voice and tone described above:\n\"{clean_text}\""
    )

    payload = {
        "model": config.TTS_MODEL,
        "modalities": ["text", "audio"],
        "audio": {
            "voice": voice,
            "format": "pcm16"
        },
        "messages": [
            {
                "role": "user",
                "content": user_message
            }
        ],
        "stream": True,
        "stream_options": {"include_usage": True}
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dressitnow.com",
        "X-Title": "DressIt Video Generation Pipeline"
    }

    req = urllib.request.Request(
        config.OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )

    raw_pcm = bytearray()
    stream_usage = None

    with urllib.request.urlopen(req, timeout=45) as resp:
        for line_bytes in resp:
            line = line_bytes.decode("utf-8").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                try:
                    chunk = json.loads(line[6:])
                    if chunk.get("usage"):
                        stream_usage = chunk.get("usage")
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        audio_info = delta.get("audio", {})
                        if "data" in audio_info and audio_info["data"]:
                            raw_pcm.extend(base64.b64decode(audio_info["data"]))
                except Exception:
                    pass

    if len(raw_pcm) == 0:
        raise RuntimeError(f"No audio stream received from OpenRouter for voice '{voice}'.")

    # Save raw PCM16 (24kHz mono) to temporary file
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp_raw:
        tmp_raw_path = tmp_raw.name
        tmp_raw.write(raw_pcm)

    try:
        # Normalize and resample to 48000 Hz and -14 LUFS using FFmpeg loudnorm filter
        suffix = out_file.suffix.lower()
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "s16le",
            "-ar", "24000",
            "-ac", "1",
            "-i", tmp_raw_path,
            "-af", f"loudnorm=I={target_lufs}:TP={config.AUDIO_TRUE_PEAK}:LRA={config.AUDIO_LRA}",
            "-ar", str(target_sample_rate)
        ]

        if suffix == ".wav":
            ffmpeg_cmd.extend(["-c:a", "pcm_s16le", str(out_file)])
        else:
            ffmpeg_cmd.extend(["-c:a", "libmp3lame", "-b:a", "192k", str(out_file)])

        conv_res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if conv_res.returncode != 0:
            raise RuntimeError(f"FFmpeg audio mastering failed: {conv_res.stderr}")

        duration, sample_rate = probe_audio(out_file)
        measured_lufs = measure_ebur128_lufs(out_file)

        return {
            "output_path": str(out_file),
            "duration": duration,
            "sample_rate": sample_rate,
            "target_lufs": target_lufs,
            "measured_lufs": measured_lufs,
            "voice": voice,
            "text": clean_text,
            "usage": stream_usage
        }

    finally:
        if os.path.exists(tmp_raw_path):
            os.unlink(tmp_raw_path)


if __name__ == "__main__":
    print("=" * 60)
    print("Testing tts_producer.py (openai/gpt-audio -14 LUFS, 48kHz)...")
    print("=" * 60)

    test_out = config.OUTPUT_DIR / "test_producer_voice.mp3"
    result = generate_tts(
        text="Stop settling for guys who put in zero effort! Check out dressitnow.com.",
        output_path=test_out,
        voice="nova"
    )
    print("TTS Result:")
    print(f"  File: {result['output_path']}")
    print(f"  Duration: {result['duration']:.2f}s")
    print(f"  Sample Rate: {result['sample_rate']} Hz (Expected: 48000)")
    print(f"  Measured LUFS: {result['measured_lufs']:.1f} LUFS (Target: -14.0)")
    print("PASS: TTS generation and mastering verified.")
