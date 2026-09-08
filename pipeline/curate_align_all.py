import whisperx
import json

wav = "/root/dressit-pipeline/oag_audio16k.wav"
with open("/root/dressit-pipeline/oag_transcript.json") as f:
    segments = json.load(f)

model_a, metadata = whisperx.load_align_model(language_code="en", device="cpu")
audio = whisperx.load_audio(wav)

result = whisperx.align(segments, model_a, metadata, audio, "cpu")

with open("/root/dressit-pipeline/all_aligned_words.json", "w") as f:
    json.dump(result["word_segments"], f, indent=2)

print(f"Done! Aligned {len(result['word_segments'])} words in all_aligned_words.json")
