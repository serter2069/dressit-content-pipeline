# DressIt Content Pipeline & Posting Hub

Central repository for generating, verifying, scheduling, and publishing vertical video content (TikTok, Instagram Reels, YouTube Shorts) for **DressIt** (`dressitnow.com`).

---

## Architecture Overview

This repository is split into two primary components:

1. **`pipeline/` — Automated Video Production Engine**
   - **Scripting & Copywriting**: OpenRouter API (`z-ai/glm-5.3-flash` / `z-ai/glm-5.3`) with zero Gemini quota consumption.
   - **Speech Synthesis**: ChatGPT Nova via OpenRouter `openai/gpt-audio`, mastered at 1.0x natural tempo and -14.0 LUFS (EBU R128).
   - **Word-Level Subtitle Alignment**: Audio analysis with `faster-whisper` and clean white Inter Black ASS styling (`\bord10\shad3\c&H00FFFFFF&`).
   - **Computer Vision & Face Tracking**: Automated face detection (OpenCV Haar frontal + profile cascade with multi-angle pass). Enforces dynamic crop centering (`|cx - 540| <= 80px`), upper-third positioning (`cy` in [200, 850]), and waist-up scale (`>= 180px`).
   - **Quality Hard-Gate Judge**: Scans videos every 2.0s across standup and UGC portions. Hard-rejects any clip with face drops or awkward framing.
   - **FFmpeg Compositor**: 1080x1920 (9:16 vertical, 30fps), persistent top logo (`dressit_logo.png`), ducked background music, and faststart streaming optimization.

2. **`posting/` — Posting Hub & Content Dashboard**
   - **Dashboard UI**: Central control room hosted at [https://posting.smartlaunchhub.com/](https://posting.smartlaunchhub.com/).
   - **Channel Architecture**: Multi-channel support (SF, LA, NYC, Miami, Global).
   - **State Machine**: `new -> review -> approved -> scheduled -> published` (with rejection and archiving).
   - **Deduplication Engine**: Visual dHash perceptual hashing + 16kHz spectral audio hash + 3-gram text Jaccard similarity to prevent duplicate posts across channels.
   - **Streaming Service**: Native HTTP 206 Partial Content video streaming for instant preview in browser.

---

## Directory Structure

```text
dressit-content-pipeline/
├── pipeline/
│   ├── config.py                       # Brand constants, resolutions, audio targets
│   ├── cli.py                          # Unified CLI (pool, render, deploy)
│   ├── face_framing_engine.py          # Dynamic face tracking & auto-crop calculator
│   ├── face_qc_judge.py                # 2-second interval automated QC judge
│   ├── glm_client.py                   # OpenRouter GLM-5.3 LLM wrapper
│   ├── tts_producer.py                 # OpenRouter Nova audio TTS & loudnorm (-14 LUFS)
│   ├── ugc_guard.py                    # Moderation validator for approved UGC pool
│   ├── assembler.py                    # Core FFmpeg compositor & filtergraph engine
│   ├── assemble_10_diverse_shorts.py   # Batch assembler for 10 diverse comedian shorts
│   ├── curated_standup/
│   │   └── manifest.json               # Harvested & verified 9:16 standup clips
│   ├── specs_10_diverse_shorts.json    # Full production specifications
│   └── README.md
├── posting/
│   ├── server.py                       # HTTP server (API & Web Dashboard)
│   ├── db.py                           # SQLite schema & query helpers
│   ├── dedup_engine.py                 # Multi-factor duplicate detection
│   ├── check_duplicate.py              # CLI tool for checking new video uniqueness
│   ├── approval/                       # Approval workflow & Telegram bot integration
│   ├── adapters/                       # Project sync adapters
│   ├── tests/                          # Selftests & unit tests
│   ├── POSTING.md                      # Detailed Posting Hub specification
│   └── README.md
└── README.md                           # This guide
```

---

## Quickstart for Developers

### 1. Requirements
- Python 3.10+
- FFmpeg 6.0+ (compiled with `libx264`, `libass`, `loudnorm`)
- OpenCV (`python3-opencv` or `opencv-python`)
- Git

### 2. Environment Setup
Set your OpenRouter API key:
```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

### 3. Running the Pipeline CLI
```bash
cd pipeline

# 1. View approved UGC pool
python3 cli.py pool

# 2. Render a short from spec
python3 cli.py render --spec specs_10_diverse_shorts.json --number 1

# 3. Run automated 2-second face QC judge on any video
python3 face_qc_judge.py /path/to/video.mp4
```

### 4. Running the Posting Hub locally
```bash
cd posting
python3 server.py --selftest
python3 server.py  # starts on 127.0.0.1:8478
```

---

## Guide for Assistants & Marketing Operations

### Accessing the Web Dashboard
1. Open [https://posting.smartlaunchhub.com/p/dressit-global](https://posting.smartlaunchhub.com/p/dressit-global) in your browser.
2. Log in using the team credentials provided by the admin.

### Daily Workflow:
1. **Review (`/p/dressit-global`)**:
   - Click on newly generated video cards to preview playback in high definition.
   - Verify that the video hook stops the scroll and the face is clearly centered.
2. **Approve / Reject**:
   - Click **Approve** if the video meets quality standards.
   - Click **Reject** if adjustments are needed.
3. **Schedule**:
   - Set publishing slots according to target timezone traffic peaks (e.g., 18:00–21:00 EST).

---

## Strict Brand & Quality Standards

- **Subtitles**: Clean White (`#FFFFFF`) with solid 10px black outline. Font: Inter Black. No distracting neon/rainbow word flashing.
- **Tone & Copy**: Natural, conversational vocabulary ("guys", "men", "partner"). **Strictly forbidden**: "джентльмены" / "gentlemen", "bucks", "chips", and cluttered promotional badges.
- **Audio**: Nova voiceover mastered to **-14.0 LUFS (EBU R128)**, true peak <= -1.5 dBFS. Background music ducked to 12% (-18dB) underneath speech.
- **UGC Footage**: Only use creator clips verified by payment audit log moderation (`post_moderation_approved`).
