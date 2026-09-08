#!/usr/bin/env python3
"""judge.py — native LLM-judge step for the declarative pipeline engine.

Calls OpenRouter chat completions (temperature 0) with a rubric + an artifact
from the job dir, forces a strict JSON verdict {score, violations, pass},
writes judge_result.json into the job dir and returns the verdict to the
runner. The model is just a config parameter (text judges use e.g.
z-ai/glm-5.3; multimodal judges can point at z-ai/glm-5.3-flash).

Used by runner.py as a library (run_judge), or standalone:
    python3 judge.py --job-dir <dir> --config <judge.json>
    python3 judge.py --selftest        # offline, no network/keys

Judge config shape (see SPEC.md):
    {"model": "...", "rubric": "<inline>" | "rubric_file": "<path>",
     "artifact": "@script" | "<file relative to job dir>" |
                 {"frames_from": "<video.mp4>", "at": [0.5, 2.0, 5.0]},
     "threshold": 7.0, "retries": 1,
     "vars": {"name": "value"}   # {{name}} placeholders in the rubric}

A dict artifact switches to multimodal mode: frames are extracted with
ffmpeg (scaled to 'width' px, default 720, JPEG) and sent as image_url
data URIs alongside the rubric text.

Never print or log the API key.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VIDEO_ENV = Path("/root/.video-env")
KEY_FILE = Path("/root/.openrouter.key")
DEFAULT_FRAME_WIDTH = 720

SYSTEM_PROMPT = (
    "You are a strict content-quality judge for short-form video pipelines. "
    "Evaluate the artifact against the rubric. Respond with ONLY a JSON object "
    "(no markdown, no commentary) of exactly this shape: "
    '{"score": <number 0-10>, "violations": ["<short reason>", ...], '
    '"pass": <true|false>}. '
    "score 10 = flawless; violations lists every concrete rubric breach; "
    "pass = whether the artifact is good enough to publish."
)


class JudgeError(Exception):
    """Transport/parse failure of the judge itself (not a 'fail' verdict)."""


def _load_api_key():
    """Resolve the OpenRouter key. Never printed or logged."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    try:
        for line in VIDEO_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            k, _, v = line.partition("=")
            if k.strip() == "OPENROUTER_API_KEY" and v.strip():
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    raise JudgeError("OPENROUTER_API_KEY not found in env, /root/.video-env "
                     "or /root/.openrouter.key")


def parse_verdict(text):
    """Strictly parse the model's JSON verdict. Raises JudgeError on garbage."""
    if not isinstance(text, str) or not text.strip():
        raise JudgeError("empty judge response")
    candidate = text.strip()
    # strip markdown fences if the model wrapped the JSON anyway
    m = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # last resort: decode the first balanced {...} object in the text
        start = candidate.find("{")
        if start < 0:
            raise JudgeError(f"no JSON object in judge response: {text[:200]!r}")
        try:
            data, _ = json.JSONDecoder().raw_decode(candidate[start:])
        except json.JSONDecodeError as e:
            raise JudgeError(f"unparseable judge response: {e}: {text[:200]!r}")
    if not isinstance(data, dict):
        raise JudgeError("judge verdict is not a JSON object")
    try:
        score = float(data["score"])
    except (KeyError, TypeError, ValueError):
        raise JudgeError(f"judge verdict has no numeric score: {data!r}")
    if not 0 <= score <= 10:
        raise JudgeError(f"judge score out of range 0-10: {score}")
    violations = data.get("violations", [])
    if not isinstance(violations, list):
        raise JudgeError(f"judge violations is not a list: {violations!r}")
    violations = [str(v) for v in violations]
    model_pass = data.get("pass")
    if not isinstance(model_pass, bool):
        raise JudgeError(f"judge pass is not a bool: {model_pass!r}")
    return {"score": score, "violations": violations, "model_pass": model_pass}


def resolve_artifact(job_dir, artifact):
    """Return (label, text) for the artifact to judge.

    '@script' / '@<field>' -> that field of job.json (JSON-serialized);
    anything else -> a file path relative to the job dir.
    """
    job_dir = Path(job_dir)
    if artifact.startswith("@"):
        field = artifact[1:]
        with open(job_dir / "job.json", encoding="utf-8") as f:
            job = json.load(f)
        if field not in job or job[field] in (None, "", {}):
            raise JudgeError(f"job.json field {field!r} is empty, nothing to judge")
        return artifact, json.dumps(job[field], ensure_ascii=False, indent=2)
    path = job_dir / artifact
    if not path.is_file():
        raise JudgeError(f"artifact file not found: {path}")
    return artifact, path.read_text(encoding="utf-8", errors="replace")


def apply_vars(text, vars_):
    """Substitute {{name}} placeholders in a rubric with config variables."""
    for k, v in (vars_ or {}).items():
        text = text.replace("{{" + str(k) + "}}", str(v))
    return text


def extract_frames(video_path, at_seconds, width=DEFAULT_FRAME_WIDTH):
    """Extract JPEG frames from a video at the given timestamps.

    Returns [{"t": float, "b64": str}]; raises JudgeError on ffmpeg failure.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise JudgeError(f"frames_from video not found: {video_path}")
    tmp = Path(tempfile.mkdtemp(prefix="judge_frames_"))
    try:
        frames = []
        for i, t in enumerate(at_seconds):
            out = tmp / f"frame_{i}.jpg"
            res = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(float(t)),
                 "-i", str(video_path), "-vframes", "1",
                 "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(out)],
                capture_output=True, text=True)
            if res.returncode != 0 or not out.is_file():
                raise JudgeError(
                    f"ffmpeg frame extract failed at {t}s on {video_path}: "
                    f"{res.stderr[-300:]}")
            frames.append({
                "t": float(t),
                "b64": base64.b64encode(out.read_bytes()).decode("ascii"),
            })
        return frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_artifact_content(job_dir, artifact):
    """Return (label, content) for the artifact to judge.

    Text mode (str artifact): content is a plain string (see resolve_artifact).
    Multimodal mode (dict artifact {"frames_from": "<video>", "at": [...]}):
    content is a list of OpenRouter image_url parts with base64 data URIs.
    """
    if isinstance(artifact, dict):
        if "frames_from" in artifact:
            video = Path(artifact["frames_from"])
            if not video.is_absolute():
                video = Path(job_dir) / video
            at = artifact.get("at") or [0.5, 2.0, 5.0]
            width = int(artifact.get("width", DEFAULT_FRAME_WIDTH))
            frames = extract_frames(video, at, width)
            parts = [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{fr['b64']}"}}
                for fr in frames
            ]
            return f"frames:{video.name}@{at}", parts
        raise JudgeError(f"unknown artifact spec: {artifact!r}")
    return resolve_artifact(job_dir, artifact)


def resolve_rubric(jcfg, spec_dir=None):
    """Inline 'rubric' wins; otherwise load 'rubric_file' (spec dir, then cwd)."""
    if jcfg.get("rubric"):
        return str(jcfg["rubric"])
    ref = jcfg.get("rubric_file")
    if not ref:
        raise JudgeError("judge config has neither 'rubric' nor 'rubric_file'")
    candidates = [Path(ref)]
    if spec_dir:
        candidates.insert(0, Path(spec_dir) / ref)
    candidates.insert(0, ROOT / ref)
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise JudgeError(f"rubric file not found: {ref}")


def call_openrouter(api_key, model, messages, timeout=120):
    """One chat-completions call at temperature 0. Returns (content, usage) tuple."""
    payload = {
        "model": model,
        "temperature": 0,
        "messages": messages,
    }
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            return content, usage
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise JudgeError(f"openrouter HTTP {e.code}: {detail}")
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise JudgeError(f"openrouter call failed: {last_err}")


def run_judge(job_dir, jcfg, spec_dir=None, http_fn=None):
    """Execute one judge gate. Returns the result dict; writes judge_result.json.

    Raises JudgeError on infrastructure failure (caller decides retry policy).
    A below-threshold verdict is NOT an exception — it is result["pass"]=False.
    Final pass/fail is threshold-based: score >= threshold (the model's own
    'pass' is recorded as model_pass for audit).

    http_fn overrides the transport (signature like call_openrouter) — used by
    the offline selftest; production callers leave it None.
    """
    job_dir = Path(job_dir)
    model = jcfg.get("model")
    if not model:
        raise JudgeError("judge config has no 'model'")
    threshold = float(jcfg.get("threshold", 7.0))
    rubric = apply_vars(resolve_rubric(jcfg, spec_dir), jcfg.get("vars"))
    label, artifact_content = build_artifact_content(
        job_dir, jcfg.get("artifact", "@script"))

    rubric_block = (
        f"## Rubric\n{rubric}\n\n"
        f"## Artifact under review ({label})\n"
    )
    tail = f"\nPass threshold is {threshold}/10. Respond with the JSON verdict only."
    if isinstance(artifact_content, str):
        user_content = f"{rubric_block}{artifact_content}{tail}"
    else:
        user_content = [{
            "type": "text",
            "text": (f"{rubric_block}{len(artifact_content)} frame(s) from the "
                     f"video are attached as images, in chronological order.{tail}"),
        }] + artifact_content
    caller = http_fn or call_openrouter
    resp = caller(
        _load_api_key(), model,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user_content}],
    )
    if isinstance(resp, (tuple, list)) and len(resp) == 2:
        content, usage = resp
    else:
        content = resp
        usage = {}
    if not isinstance(usage, dict):
        usage = {}

    verdict = parse_verdict(content)
    result = {
        "at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "artifact": label,
        "threshold": threshold,
        "score": verdict["score"],
        "violations": verdict["violations"],
        "model_pass": verdict["model_pass"],
        "pass": verdict["score"] >= threshold,
        "usage": usage,
        "raw": content[:2000],
    }
    with open(job_dir / "judge_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def selftest():
    """Offline: strict parser behaviour, artifact/rubric resolution."""
    import tempfile

    v = parse_verdict('{"score": 8.5, "violations": [], "pass": true}')
    assert v["score"] == 8.5 and v["model_pass"] is True
    v = parse_verdict('```json\n{"score": 3, "violations": ["too long"], "pass": false}\n```')
    assert v["score"] == 3 and v["violations"] == ["too long"]
    v = parse_verdict('verdict: {"score": 6, "violations": [], "pass": true} thanks')
    assert v["score"] == 6
    for bad in ("", "not json", '{"score": 42, "violations": [], "pass": true}',
                '{"score": 5, "violations": [], "pass": "yes"}',
                '{"violations": [], "pass": true}'):
        try:
            parse_verdict(bad)
            raise AssertionError(f"should have raised: {bad!r}")
        except JudgeError:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        job_dir = Path(tmp)
        (job_dir / "job.json").write_text(json.dumps(
            {"id": "t", "state": "scripted", "script": {"hook": "hi"}}))
        label, text = resolve_artifact(job_dir, "@script")
        assert label == "@script" and "hook" in text
        (job_dir / "notes.txt").write_text("hello")
        label, text = resolve_artifact(job_dir, "notes.txt")
        assert text == "hello"
        try:
            resolve_artifact(job_dir, "@missing")
            raise AssertionError("should have raised")
        except JudgeError:
            pass
        (job_dir / "rubric.md").write_text("be nice")
        assert resolve_rubric({"rubric_file": "rubric.md"}, spec_dir=tmp) == "be nice"
        assert resolve_rubric({"rubric": "inline"}) == "inline"

        # vars substitution
        assert apply_vars("city={{expected_city}}", {"expected_city": "Miami"}) \
            == "city=Miami"
        assert apply_vars("no vars", None) == "no vars"

        # full run_judge with a mocked transport (offline, no API key needed)
        def fake_http(api_key, model, messages, timeout=120):
            return '{"score": 8, "violations": [], "pass": true}'
        res = run_judge(job_dir, {"model": "fake/model", "rubric": "be nice",
                                  "artifact": "@script", "threshold": 7.0},
                        http_fn=fake_http)
        assert res["pass"] is True and res["score"] == 8
        assert res["usage"] == {}
        assert (job_dir / "judge_result.json").is_file()
        saved = json.loads((job_dir / "judge_result.json").read_text(encoding="utf-8"))
        assert saved["usage"] == {}

        # test http_fn returning (content, usage) tuple
        fake_usage = {"prompt_tokens": 120, "completion_tokens": 15, "total_tokens": 135, "cost": 0.0002}
        def fake_http_usage(api_key, model, messages, timeout=120):
            return '{"score": 9, "violations": [], "pass": true}', fake_usage
        res_u = run_judge(job_dir, {"model": "fake/model", "rubric": "be nice",
                                    "artifact": "@script", "threshold": 7.0},
                          http_fn=fake_http_usage)
        assert res_u["pass"] is True and res_u["score"] == 9
        assert res_u["usage"] == fake_usage
        saved_u = json.loads((job_dir / "judge_result.json").read_text(encoding="utf-8"))
        assert saved_u["usage"] == fake_usage

        # test call_openrouter extraction of (content, usage) with mocked urlopen
        class _MockResp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": '{"score": 10, "violations": [], "pass": true}'}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60, "cost": 0.00015}
                }).encode("utf-8")

        import unittest.mock
        with unittest.mock.patch("urllib.request.urlopen", return_value=_MockResp()):
            mock_content, mock_usage = call_openrouter("fake-key", "fake-model", [{"role": "user", "content": "hi"}])
            assert "score" in mock_content
            assert mock_usage["total_tokens"] == 60
            assert mock_usage["cost"] == 0.00015

        # multimodal mode: synthetic video -> frames -> image_url parts
        vid = job_dir / "clip.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc=duration=3:size=320x240:rate=10",
             "-pix_fmt", "yuv420p", str(vid)],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-300:]
        frames = extract_frames(vid, [0.2, 1.0], width=160)
        assert len(frames) == 2 and all(len(f["b64"]) > 100 for f in frames)
        label, parts = build_artifact_content(
            job_dir, {"frames_from": "clip.mp4", "at": [0.2, 1.0], "width": 160})
        assert label.startswith("frames:clip.mp4")
        assert all(p["type"] == "image_url" and
                   p["image_url"]["url"].startswith("data:image/jpeg;base64,")
                   for p in parts)

        seen = {}

        def fake_http_mm(api_key, model, messages, timeout=120):
            seen["user"] = messages[-1]["content"]
            return '{"score": 5, "violations": ["x"], "pass": false}'
        res = run_judge(job_dir, {"model": "fake/mm",
                                  "rubric": "city={{expected_city}}",
                                  "vars": {"expected_city": "Miami"},
                                  "artifact": {"frames_from": "clip.mp4",
                                               "at": [0.2], "width": 160},
                                  "threshold": 7.0},
                        http_fn=fake_http_mm)
        assert res["pass"] is False and res["model_pass"] is False
        content = seen["user"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text" and "city=Miami" in content[0]["text"]
        assert len(content) == 2  # 1 text part + 1 frame
    print("judge.py selftest OK")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--job-dir" in args and "--config" in args:
        job_dir = args[args.index("--job-dir") + 1]
        with open(args[args.index("--config") + 1], encoding="utf-8") as f:
            jcfg = json.load(f)
        try:
            result = run_judge(job_dir, jcfg)
        except JudgeError as e:
            print(f"judge error: {e}", file=sys.stderr)
            return 2
        print(json.dumps({k: result[k] for k in
                          ("model", "score", "pass", "violations")},
                         indent=2, ensure_ascii=False))
        return 0 if result["pass"] else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
