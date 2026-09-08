# DressIt Production Video Generation Pipeline (v2.0)

Permanent, robust, production-grade video generation pipeline for DressIt vertical shorts (`1080x1920`, 30 fps), built for Task #1999.

---

## 1. Pipeline Architecture

The pipeline is organized into modular, independently testable Python components:

```
/root/dressit-pipeline/
├── config.py             # Centralized brand constants, audio targets, and styling rules
├── ugc_guard.py          # Direct Postgres DB & pool validator (moderation audit, live status, bans)
├── glm_client.py         # OpenRouter GLM-5.3-flash (+ fallback) client with strict Anti-Gemini guard
├── tts_producer.py       # OpenAI GPT-Audio wrapper (voice 'nova', 48kHz, -14.0 LUFS EBU R128)
├── assembler.py          # Master FFmpeg filtergraph engine (3ms microfade, UGC concat, ASS subtitles, logo)
├── cli.py                # Command-line interface (pool, verify-clip, generate-copy, render, deploy)
├── specs/                # Standardized JSON render specifications
│   ├── spec_short_01.json
│   └── spec_short_02.json
├── output/               # Rendered master MP4 videos and working files
└── README.md             # Architecture, brand rules, and operational documentation
```

---

## 2. Brand Guidelines & Visual Constraints

All future agents and video generation tasks must strictly follow these rules:

1. **Aspect Ratio & Resolution**:
   - Resolution: `1080x1920` (9:16 vertical).
   - Frame Rate: `30 fps`.
   - Video Codec: `H.264` (`libx264`), `crf 18`, `preset fast`, `pix_fmt yuv420p`.

2. **Persistent Clean Logo**:
   - Path: `/root/event-hero/assets/dressit_logo.png`.
   - Filtergraph: `scale=480:-1, overlay=(W-w)/2:130`.
   - Visible across the **entire** video duration.
   - Zero glowing edges, zero badges behind the logo.

3. **Subtitles (ASS Standard)**:
   - Font: `Inter Black` (fallback `Inter`).
   - Font Size: `82 pt`.
   - Text Color: Clean Pure White (`&H00FFFFFF&`).
   - Outline: Solid Black (`&H00000000&`), width `\bord10`.
   - Shadow: Solid Black (`&H00000000&`), depth `\shad3`.
   - Position: Lower third (`MarginV 380`, `Alignment 2`).
   - **STRICT NEGATIVE CONSTRAINT**: **STRICTLY NO RAINBOW, NEON, OR VARIEGATED WORD COLORS**. Subtitles must remain clean, crisp, and high-contrast white with solid black stroke.

4. **Visual Aesthetics**:
   - **ZERO chips, ZERO pill badges**.
   - **ZERO "Funded by Real Gentlemen" or "Upgrade your dating" text/overlays**.
   - Dynamic 2-cut UGC Outro Montage:
     * **Cut A** (3.6s - 3.8s): Clean reveal of first dress outfit.
     * **Cut B** (5.3s - 5.7s): Clean second outfit with subtle `1.05x` punch zoom.

5. **Copywriting & Tonality**:
   - Word **"gentlemen" / "джентльмены" is STRICTLY FORBIDDEN**.
   - Use natural colloquial language: `"guys"`, `"men"`, `"people"`, `"besties"`.
   - Direct comedic bridge from the standup punchline into the DressIt value proposition.
   - Clear, clean CTA: `dressitnow.com`.

---

## 3. Audio Engineering Constraints

1. **Target Loudness**:
   - Standard: **EBU R128**.
   - Integrated Loudness: **`-14.0 LUFS`** (tolerance $\pm 0.8$ LU).
   - True Peak: **`-1.5 dBFS`** maximum.
   - Loudness Range (LRA): `9.0 LU`.
   - Filtergraph: `loudnorm=I=-14:TP=-1.5:LRA=9`.

2. **Sample Rate**:
   - Standardized to **`48000 Hz`** across all audio processing (voiceover, BGM, and master render).

3. **Background Music (BGM)**:
   - Track: Funk instrumental (`/root/global-dressit-pilot/assets/bgm_funk.mp3`).
   - Ducking: Ducked to volume **`0.12`** during speech, with a clean `0.6s` fade-out at the end.

4. **Microfade on Standup Audio Cuts**:
   - Cut 1 audio start uses a **`3ms` microfade** (`afade=t=in:ss=0:d=0.003`).
   - **NEVER use 80ms fade-in** — 80ms clips the first vowel or consonant of the joke.
   - 3ms eliminates DC offset pops/clicks while preserving 100% of speech phonemes.

5. **Voiceover Engine**:
   - Model: `openai/gpt-audio` via OpenRouter (voice: `nova`).
   - Style: Fast-paced, high energy, witty, completely natural conversational cadence.

---

## 4. UGC Guard & Moderation Model

The UGC Guard (`ugc_guard.py`) performs direct real-time database validation:

1. **Database Source**:
   - PostgreSQL URI: `postgres://dressit_user:R8a3wKgwLJ23asPGgs7UdvHy@127.0.0.1:5432/dressit`.
2. **Moderation Status**:
   - Post MUST have a row in `payment_audit_log` with `event_type = 'post_moderation_approved'` (`subject_type = 'post'`).
3. **Live Status**:
   - Post MUST be active: `posts.taken_down_at IS NULL AND posts.deleted_at IS NULL`.
4. **Banned Content**:
   - Clip `maria_10_mary_vid_uLwcVgz6.mov` is **strictly banned** (Decathlon poncho / tonality violation).
   - Any clip inside `quarantine_unapproved` is **strictly rejected**.
5. **Approved Clips Pool**:
   - 12 verified live clips available in `/root/event-hero/assets/maria_ugc/`.

---

## 5. Script & Copywriting Engine (GLM-5.3)

- **Engine**: OpenRouter `z-ai/glm-5.3-flash` with automatic fallback to `z-ai/glm-5.3`.
- **API Key**: Loaded securely from `/root/.openrouter.key`.
- **Anti-Gemini Policy**: Calling Google Gemini API for script or copy generation is **strictly prohibited**. The client contains hard architectural exceptions (`GeminiProhibitedError`) if Gemini is referenced.

---

## 6. Multi-Point Face Verification

- **Script**: `/root/global-dressit-pilot/face_verifier.py`.
- **Methodology**: Samples frames every `2.5 seconds` across the comedian standup section.
- **Detector**: Multi-cascade OpenCV (Frontal + Left Profile + Right Profile).
- **Centering Threshold**: Face center `cx` must be within `240 px` of the frame horizontal midpoint (`x=540`), and within safe frame bounds ($30 \le x \le 1050$).
- Bounding-box debug frames are automatically archived to `debug_frames/<slug>/`.

---

## 7. CLI Quick Reference

The pipeline includes a unified CLI (`cli.py`):

```bash
# 1. View verified approved UGC clips from PostgreSQL DB
python3 /root/dressit-pipeline/cli.py pool

# 2. Check single clip compliance
python3 /root/dressit-pipeline/cli.py verify-clip --clip maria_01_angaevamariia_vid_qYVNgIgi.mp4

# 3. Generate script & copy with GLM-5.3
python3 /root/dressit-pipeline/cli.py generate-copy \
  --hook "I quit dating losers!" \
  --punchline "That took a minute!"

# 4. Render a video short from a specification JSON
python3 /root/dressit-pipeline/cli.py render --spec specs/spec_short_01.json

# 5. Deploy rendered video to Posting Hub & verify HTTP 206 streaming
python3 /root/dressit-pipeline/cli.py deploy --item-id dglobal-rel-01-losers --spec specs/spec_short_01.json
```

---

## 8. Spec File JSON Format

```json
{
  "id": "dglobal-rel-01-losers",
  "title": "RELATIONSHIPS · I Quit Dating Losers · DressIt",
  "slug": "rel_short_01_losers",
  "standup_clip": "/root/dressit-shorts/clips/oagNYHB3Kzk.mp4",
  "cut1_ss": 4.18,
  "cut1_dur": 2.37,
  "cut2_ss": 6.55,
  "cut2_dur": 5.85,
  "ugc_cut_a": "/root/event-hero/assets/maria_ugc/maria_04_angaevamariia_vid_28hF5F0z.mp4",
  "ugc_cut_a_ss": 1.5,
  "ugc_cut_a_dur": 3.60,
  "ugc_cut_b": "/root/event-hero/assets/maria_ugc/maria_01_angaevamariia_vid_qYVNgIgi.mp4",
  "ugc_cut_b_ss": 2.0,
  "ugc_cut_b_dur": 5.68,
  "voice_file": "/root/global-dressit-pilot/assets/voice_rel_context_01_losers.mp3",
  "total_dur": 17.50,
  "comm_voice_offset": 8.42,
  "caption": "Social media caption...",
  "dialogue": [
    {"start": "0:00:00.08", "end": "0:00:01.00", "text": "I'VE DONE SO MUCH WORK"},
    {"start": "0:00:01.00", "end": "0:00:01.80", "text": "ON MYSELF..."}
  ]
}
```
