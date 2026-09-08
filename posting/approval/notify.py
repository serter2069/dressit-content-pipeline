#!/usr/bin/env python3
"""notify.py — send a rendered video to Telegram for URL-based review.

Any pipeline can call this once a job is rendered. The message carries the
video plus a caption with the scenario text and three signed URLs
(✅ approve / ❌ reject / 🔁 regenerate) pointing at the shared approval
service (approval_server.py). Inline buttons are impossible because the
bot is bound to the dressit production webhook — URL links only.

Library use from a pipeline:

    import sys; sys.path.insert(0, "/root/posting/approval")
    from notify import send_for_review
    message_id = send_for_review(
        project="dressit-fshorts",
        job_id=job["id"],
        video_path="/root/fshorts/jobs/<id>/final.mp4",
        caption_text=f"{hook}\n\n{cta}",
    )

Text-only alerts (no video, no review links — e.g. ops warnings):

    from notify import send_text
    send_text("пул UGC исчерпан, нужна новая партия")

CLI:
    python3 notify.py <project> <job_id> <video_path> [caption...]
    python3 notify.py --selftest     # offline, mocked Telegram API

Env (loaded from /root/.video-env, /root/fshorts/.env, ./.env):
    TELEGRAM_BOT_TOKEN   - required
    TELEGRAM_CHAT_ID     - required
    APPROVAL_SECRET      - required (signs the URLs)
    APPROVAL_BASE_URL    - public base of the approval service
                           (default http://95.217.84.161:8477)
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval_server  # for links_for / load_env_files

API_BASE = "https://api.telegram.org"
DEFAULT_BASE_URL = "http://95.217.84.161:8477"
CAPTION_LIMIT = 1024  # Telegram caption limit


def approval_links_block(project, job_id, secret=None, base_url=None):
    """Signed approve/reject/regenerate URLs for the caption."""
    secret = secret or os.environ.get("APPROVAL_SECRET")
    base_url = base_url or os.environ.get("APPROVAL_BASE_URL", DEFAULT_BASE_URL)
    if not secret or not job_id:
        return ""
    links = approval_server.links_for(secret, base_url, project, job_id)
    return (f"\n\n✅ Approve: {links['a']}"
            f"\n❌ Reject: {links['r']}"
            f"\n🔁 Regenerate: {links['g']}")


def build_caption(project, job_id, caption_text, secret=None, base_url=None):
    caption = (caption_text or "").strip()
    caption += f"\n\n[{project}] Job: {job_id}"
    caption += approval_links_block(project, job_id, secret, base_url)
    return caption[:CAPTION_LIMIT]


def tg_send_video(token, chat_id, video_path, caption):
    """POST sendVideo; returns the decoded `result`. Raises on failure."""
    if requests is None:
        raise RuntimeError("python package 'requests' is not installed")
    form = {"chat_id": chat_id, "caption": caption,
            "supports_streaming": "true"}
    with open(video_path, "rb") as fh:
        resp = requests.post(f"{API_BASE}/bot{token}/sendVideo",
                             data=form, files={"video": fh}, timeout=120)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendVideo failed: "
                           f"{data.get('description', data)}")
    return data.get("result", {})


def tg_send_message(token, chat_id, text):
    """POST sendMessage (text only, no video); returns the decoded `result`.
    Raises on failure."""
    if requests is None:
        raise RuntimeError("python package 'requests' is not installed")
    resp = requests.post(f"{API_BASE}/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text,
                               "disable_web_page_preview": True},
                         timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: "
                           f"{data.get('description', data)}")
    return data.get("result", {})


def send_text(text, token=None, chat_id=None):
    """Generic text-only Telegram alert (no video, no review links) using the
    same credentials as send_for_review (/root/.video-env, /root/fshorts/.env).
    Returns message_id."""
    approval_server.load_env_files()
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set "
                           "(check /root/.video-env)")
    result = tg_send_message(token, chat_id, text)
    return result.get("message_id")


def send_for_review(project, job_id, video_path, caption_text,
                    token=None, chat_id=None, secret=None, base_url=None):
    """Send the rendered video with signed review links. Returns message_id."""
    approval_server.load_env_files()
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set "
                           "(check /root/.video-env)")
    if not Path(video_path).is_file():
        raise FileNotFoundError(video_path)
    caption = build_caption(project, job_id, caption_text, secret, base_url)
    result = tg_send_video(token, chat_id, video_path, caption)
    return result.get("message_id")


def selftest():
    """Offline: mocked Telegram API, asserts caption + links. No network."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="notify_selftest_"))
    video = tmp / "final.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    sent = []

    def fake_send(token, chat_id, video_path, caption):
        assert Path(video_path).is_file()
        sent.append(caption)
        return {"message_id": 4242}

    real = tg_send_video
    globals()["tg_send_video"] = fake_send
    try:
        mid = send_for_review(
            project="dressit-fshorts", job_id="20260905-ab12",
            video_path=str(video),
            caption_text="5 fall fits under $50\n\nLinks in the pinned comment",
            token="FAKE", chat_id="555",
            secret="selftest-secret", base_url="http://example.test:8477",
        )
        assert mid == 4242
        cap = sent[0]
        assert "5 fall fits under $50" in cap
        assert "[dressit-fshorts] Job: 20260905-ab12" in cap
        sig = approval_server.sig_for("selftest-secret", "dressit-fshorts",
                                      "20260905-ab12")
        for letter in "arg":
            url = f"http://example.test:8477/{letter}/dressit-fshorts/20260905-ab12/{sig}"
            assert url in cap, f"missing {url} in caption:\n{cap}"
        # caption limit respected
        long_mid = build_caption("p", "j", "x" * 5000, "s", "http://b")
        assert len(long_mid) <= CAPTION_LIMIT
    finally:
        globals()["tg_send_video"] = real
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELFTEST PASS")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Telegram review notifier")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("args", nargs="*",
                    help="project job_id video_path [caption...]")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if len(args.args) < 3:
        ap.print_help()
        return 1
    project, job_id, video_path = args.args[:3]
    caption_text = " ".join(args.args[3:])
    mid = send_for_review(project, job_id, video_path, caption_text)
    print(f"sent for review: {project}/{job_id} message_id={mid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
