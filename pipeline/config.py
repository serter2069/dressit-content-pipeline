#!/usr/bin/env python3
"""
config.py — Centralized brand constants and technical configuration
for the DressIt Video Generation Pipeline.

Strict Standards (Task #1999):
- Resolution: 1080x1920 (9:16 vertical), 30 fps
- Persistent Logo: /root/event-hero/assets/dressit_logo.png (scale=480:-1, overlay at (W-w)/2:130)
- Subtitles: Clean White (&H00FFFFFF&), Inter Black font, solid black border (&H00000000&, \\bord10\\shad3), MarginV 380.
  STRICTLY NO RAINBOW / NEON / VARIEGATED WORD COLORS.
- Visual Aesthetics: ZERO chips, ZERO pill badges, ZERO 'Funded by Real Gentlemen' or 'Upgrade your dating'.
- Audio Target: -14.0 LUFS (EBU R128), BGM funk ducked to volume 0.12, 48000 Hz.
- Cut Micro-fade: 3ms on cut 1 audio start to eliminate phoneme clipping and DC offset clicks.
"""

import os
from pathlib import Path

# --- Video Specifications ---
WIDTH: int = 1080
HEIGHT: int = 1920
FPS: int = 30
PIX_FMT: str = "yuv420p"
CRF: int = 18
PRESET: str = "fast"

# --- Logo Configuration ---
# Persistent clean DressIt logo across entire video duration
LOGO_PATH: Path = Path("/root/event-hero/assets/dressit_logo.png")
LOGO_WIDTH: int = 480
LOGO_SCALE: str = f"scale={LOGO_WIDTH}:-1"
LOGO_OVERLAY_COORDS: str = "(W-w)/2:130"

# --- Subtitle Styling (Inter Black, Clean White, Solid Black Border) ---
# Strictly NO rainbow/neon/variegated word colors. Pure clean white text.
FONT_NAME: str = "Inter Black"
FONT_SIZE: int = 82
SUBTITLE_PRIMARY_COLOR: str = "&H00FFFFFF&"   # Clean White
SUBTITLE_SECONDARY_COLOR: str = "&H00FFFFFF&"
SUBTITLE_OUTLINE_COLOR: str = "&H00000000&"   # Solid Black Outline
SUBTITLE_BACK_COLOR: str = "&H00000000&"      # Black Shadow
SUBTITLE_OUTLINE_WIDTH: int = 10              # \\bord10
SUBTITLE_SHADOW_DEPTH: int = 3                # \\shad3
SUBTITLE_MARGIN_V: int = 380                  # Lower third placement
SUBTITLE_ALIGNMENT: int = 2                   # Bottom-center

# ASS Subtitle Header Template
ASS_HEADER: str = f"""[Script Info]
Title: DressIt Clean Brand Subtitles
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DressItBrand,{FONT_NAME},{FONT_SIZE},{SUBTITLE_PRIMARY_COLOR},{SUBTITLE_SECONDARY_COLOR},{SUBTITLE_OUTLINE_COLOR},{SUBTITLE_BACK_COLOR},-1,0,0,0,100,100,0,0,1,{SUBTITLE_OUTLINE_WIDTH},{SUBTITLE_SHADOW_DEPTH},{SUBTITLE_ALIGNMENT},40,40,{SUBTITLE_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Brand Negative Constraints (Forbidden Visuals & Copy)
FORBIDDEN_PHRASES = [
    "Funded by Real Gentlemen",
    "Upgrade your dating",
    "gentlemen",
    "джентльмены",
]

# --- Audio Constraints ---
SAMPLE_RATE: int = 48000
AUDIO_TARGET_LUFS: float = -14.0       # EBU R128 Target Loudness
AUDIO_TRUE_PEAK: float = -1.5          # Maximum True Peak (dBFS)
AUDIO_LRA: float = 9.0                 # Loudness Range Target
BGM_DEFAULT_PATH: Path = Path("/root/global-dressit-pilot/assets/bgm_funk.mp3")
BGM_DUCK_VOLUME: float = 0.12          # Ducked background music volume
MICROFADE_IN_SEC: float = 0.003        # 3ms microfade on cut1 audio start
AUDIO_FADE_OUT_SEC: float = 0.25       # Audio fadeout at cuts/end

# --- Database & UGC Guard Paths ---
DB_URI: str = "postgres://dressit_user:R8a3wKgwLJ23asPGgs7UdvHy@127.0.0.1:5432/dressit"
APPROVED_UGC_POOL_PATH: Path = Path("/root/event-hero/assets/approved_ugc_pool.json")
MARIA_UGC_DIR: Path = Path("/root/event-hero/assets/maria_ugc")
MANIFEST_PATH: Path = MARIA_UGC_DIR / "manifest.json"
QUARANTINE_DIR_NAME: str = "quarantine_unapproved"
BANNED_CLIPS: list[str] = ["maria_10"]

# --- AI & OpenRouter Settings ---
OPENROUTER_KEY_FILE: Path = Path("/root/.openrouter.key")
OPENROUTER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
GLM_FLASH_MODEL: str = "z-ai/glm-5.3-flash"
GLM_FALLBACK_MODEL: str = "z-ai/glm-5.3"
TTS_MODEL: str = "openai/gpt-audio"
TTS_VOICE: str = "nova"

# --- Posting Hub & Deployment ---
POSTING_DB_PATH: Path = Path("/root/posting/data/posting.db")
POSTING_MEDIA_DIR: Path = Path("/root/posting/data/media")
POSTING_HUB_URL: str = "http://127.0.0.1:8478"
CHANNEL_ALIAS: str = "dressit-global"

# --- Assets & Directories ---
FONTS_DIR: Path = Path("/root/global-dressit-pilot/assets/fonts")
FACE_VERIFIER_SCRIPT: Path = Path("/root/global-dressit-pilot/face_verifier.py")
FACE_CHECK_INTERVAL: float = 2.5
BASE_DIR: Path = Path("/root/dressit-pipeline")
OUTPUT_DIR: Path = BASE_DIR / "output"
DEBUG_FRAMES_DIR: Path = BASE_DIR / "debug_frames"
SPECS_DIR: Path = BASE_DIR / "specs"

# Ensure runtime directories exist
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
SPECS_DIR.mkdir(parents=True, exist_ok=True)
