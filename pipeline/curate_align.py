import whisperx
import json
import subprocess
import os

wav = "/root/dressit-pipeline/oag_audio16k.wav"
with open("/root/dressit-pipeline/oag_transcript.json") as f:
    segments = json.load(f)

print("Loading alignment model...")
model_a, metadata = whisperx.load_align_model(language_code="en", device="cpu")
audio = whisperx.load_audio(wav)

# Candidate segments:
selected_segments = [segments[i] for i in [2, 3, 8, 18, 21, 22, 23]]
print(f"Aligning {len(selected_segments)} segments...")
result = whisperx.align(selected_segments, model_a, metadata, audio, "cpu")

with open("/root/dressit-pipeline/candidate_aligned_words.json", "w") as f:
    json.dump(result["word_segments"], f, indent=2)

print(f"Aligned {len(result['word_segments'])} words saved to candidate_aligned_words.json")
