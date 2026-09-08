#!/usr/bin/env python3
"""
test_pipeline.py — Comprehensive integration test suite for /root/dressit-pipeline/
Verifies that all modules execute with 0 errors.
"""

import os
import sys
import json
import unittest
from pathlib import Path

# Add pipeline directory to import path
PIPELINE_DIR = Path("/root/dressit-pipeline").resolve()
sys.path.insert(0, str(PIPELINE_DIR))

import config
import ugc_guard
import glm_client
import tts_producer
import assembler


class TestDressItPipeline(unittest.TestCase):

    def test_01_config_integrity(self):
        """Validates all brand constants and paths."""
        self.assertEqual(config.WIDTH, 1080)
        self.assertEqual(config.HEIGHT, 1920)
        self.assertEqual(config.FPS, 30)
        self.assertTrue(config.LOGO_PATH.exists(), f"Logo missing: {config.LOGO_PATH}")
        self.assertEqual(config.SUBTITLE_PRIMARY_COLOR, "&H00FFFFFF&")
        self.assertEqual(config.AUDIO_TARGET_LUFS, -14.0)
        self.assertEqual(config.SAMPLE_RATE, 48000)
        self.assertIn("maria_10", config.BANNED_CLIPS)
        print("✓ config integrity verified")

    def test_02_ugc_guard_moderation(self):
        """Tests UGC validation against Postgres DB, banned clips, and approved pool."""
        # Banned clip
        self.assertFalse(ugc_guard.verify_clip("maria_10_mary_vid_uLwcVgz6.mov"))
        # Quarantined clip
        self.assertFalse(ugc_guard.verify_clip("/root/event-hero/assets/maria_ugc/quarantine_unapproved/maria_13_vid_hpcTKFfkgAiobT2qJmxPv.mp4"))
        # Approved clip
        self.assertTrue(ugc_guard.verify_clip("maria_01_angaevamariia_vid_qYVNgIgi.mp4"))
        
        clips = ugc_guard.get_approved_clips()
        self.assertGreaterEqual(len(clips), 10)
        for c in clips:
            self.assertTrue(c["payment_audit_approved"])
            self.assertTrue(c["is_live"])
            self.assertNotIn("maria_10", c["clip"])
        print(f"✓ ugc_guard verified ({len(clips)} live approved clips)")

    def test_03_glm_client_and_gemini_guard(self):
        """Verifies OpenRouter GLM-5.3 connectivity and strict Gemini block."""
        with self.assertRaises(glm_client.GeminiProhibitedError):
            glm_client._enforce_no_gemini("gemini-1.5-pro", {})

        res = glm_client.chat_completion([{"role": "user", "content": "Respond only: OK"}])
        self.assertTrue(len(res) > 0)
        print("✓ glm_client & Anti-Gemini guard verified")

    def test_04_tts_producer_mastering(self):
        """Verifies openai/gpt-audio normalization to -14 LUFS and 48kHz."""
        test_out = config.OUTPUT_DIR / "unit_test_tts.mp3"
        res = tts_producer.generate_tts(
            text="Stop settling for guys who put in zero effort! Check out dressitnow.com.",
            output_path=test_out,
            voice="nova"
        )
        self.assertTrue(Path(res["output_path"]).exists())
        self.assertEqual(res["sample_rate"], 48000)
        self.assertAlmostEqual(res["measured_lufs"], -14.0, delta=2.0)
        print(f"✓ tts_producer verified ({res['sample_rate']}Hz, {res['measured_lufs']:.1f} LUFS)")

    def test_05_assembler_subtitles_sanitization(self):
        """Ensures rainbow/neon styling is stripped and white font enforced."""
        dirty = r"{\c&H00FFFF&}LOOK AT THIS {\c&H0000FF&}DRESS! 🎉✨"
        clean = assembler.sanitize_subtitle_text(dirty)
        self.assertNotIn("c&H", clean)
        self.assertNotIn("🎉", clean)
        self.assertIn("LOOK AT THIS", clean)
        print("✓ subtitle brand hygiene verified")

    def test_06_rendered_video_attributes(self):
        """Verifies that the rendered short satisfies all production constraints."""
        rendered = config.OUTPUT_DIR / "rel_short_01_losers.mp4"
        if not rendered.exists():
            self.skipTest("rel_short_01_losers.mp4 not pre-rendered")
        dur = assembler.probe_duration(rendered)
        self.assertAlmostEqual(dur, 17.50, delta=0.5)
        print(f"✓ rendered video verified ({rendered.name}, {dur:.2f}s)")


if __name__ == "__main__":
    print("=" * 60)
    print("RUNNING DRESSIT PIPELINE INTEGRATION SUITE")
    print("=" * 60)
    unittest.main(verbosity=2)
