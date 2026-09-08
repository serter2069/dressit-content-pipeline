#!/usr/bin/env python3
"""approval_server.py — shared URL-based approval endpoint for all pipelines.

Generalizes /root/fshorts/approval_server.py (fshorts-only) into a
multi-project service. The Minimesergeibot is bound to the dressit
production webhook, so inline Telegram buttons (callback_query) are
unavailable: bot captions carry signed URLs; the operator taps one and
this server flips the job state via the project's adapter (see adapters.py).

Routes:
    GET /a/<project>/<job_id>/<sig>   approve
    GET /r/<project>/<job_id>/<sig>   reject
    GET /g/<project>/<job_id>/<sig>   regenerate (state=new)
    GET /a/<job_id>/<sig>             LEGACY fshorts routes (project defaults
    GET /r/<job_id>/<sig>             to dressit-fshorts, legacy signature) —
    GET /g/<job_id>/<sig>             keeps old Telegram captions working.
    GET /healthz                      liveness probe

Signatures (sig = first 16 hex chars of HMAC-SHA256):
    new links:    HMAC(APPROVAL_SECRET, "<project>:<job_id>")
    legacy links: HMAC(APPROVAL_SECRET, "<job_id>")

Every decision is audited into posting.db (runs row + items.state flip when
a matching hub item exists).

Usage:
    python3 approval_server.py --serve       # listen (default)
    python3 approval_server.py --selftest    # offline self-test

Env (loaded from /root/.video-env, then /root/fshorts/.env, then ./.env;
real environment always wins):
    APPROVAL_SECRET    - required for --serve
    APPROVAL_PORT      - default 8477
    APPROVAL_BASE_URL  - public base for links (used by notify.py)
    POSTING_DB         - hub DB path (default /root/posting/data/posting.db)
    FSHORTS_JOBS_DIR   - fshorts jobs dir (default /root/fshorts/jobs)
"""

import argparse
import hashlib
import hmac
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapters

ROOT = Path(__file__).resolve().parent
ENV_FILES = (Path("/root/.video-env"), Path("/root/fshorts/.env"),
             ROOT / ".env")

ACTIONS = adapters.ACTIONS


def load_env_files():
    for path in ENV_FILES:
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[approval_server {now_iso()}] {msg}", flush=True)


def sig_for(secret, project, job_id):
    """New-format signature over '<project>:<job_id>'."""
    msg = f"{project}:{job_id}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]


def legacy_sig_for(secret, job_id):
    """Legacy fshorts signature over bare '<job_id>' (old captions)."""
    return hmac.new(secret.encode(), job_id.encode(), hashlib.sha256).hexdigest()[:16]


def links_for(secret, base_url, project, job_id):
    """Signed approve/reject/regenerate URLs for a (project, job)."""
    sig = sig_for(secret, project, job_id)
    base = base_url.rstrip("/")
    return {a: f"{base}/{a}/{project}/{job_id}/{sig}" for a in ACTIONS}


def apply_decision(project, job_id, letter):
    """Route a decision through the adapter registry + hub wiring.
    Returns (http_code, human message)."""
    adapter = adapters.get_adapter(project)
    if adapter is None:
        return 404, f"unknown project {project}"
    try:
        code, msg = adapter.apply(job_id, letter)
    except Exception as exc:
        code, msg = 500, f"error: {exc}"
    if code != 400:  # malformed ids are not decisions; don't audit them
        adapter.finalize(job_id, letter, code, msg)
    if code == 200:
        log(f"{project}/{job_id}: -> {ACTIONS[letter]} (via url)")
    else:
        log(f"{project}/{job_id}: {letter} -> HTTP {code}: {msg}")
    return code, msg


class Handler(BaseHTTPRequestHandler):
    secret = ""

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["healthz"]:
            code, msg = 200, "ok"
        elif len(parts) == 4 and parts[0] in ACTIONS:
            letter, project, job_id, sig = parts
            if not hmac.compare_digest(sig, sig_for(self.secret, project, job_id)):
                code, msg = 403, "bad signature"
            else:
                code, msg = apply_decision(project, job_id, letter)
        elif len(parts) == 3 and parts[0] in ACTIONS:
            letter, job_id, sig = parts
            if not hmac.compare_digest(sig, legacy_sig_for(self.secret, job_id)):
                code, msg = 403, "bad signature"
            else:
                code, msg = apply_decision(adapters.LEGACY_PROJECT, job_id, letter)
        else:
            code, msg = 400, "usage: /a|r|g/<project>/<job_id>/<sig>"
        body = (f"<html><body style='font-family:sans-serif;text-align:center;"
                f"padding-top:40px'><h2>{msg}</h2>"
                f"<p>You can close this tab.</p></body></html>").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass


def selftest():
    """Offline test: temp fshorts jobs dir + temp hub DB, ephemeral port."""
    import json
    import shutil
    import sqlite3
    import tempfile
    import urllib.error
    import urllib.request

    tmp = Path(tempfile.mkdtemp(prefix="approval_selftest_"))
    jobs_dir = tmp / "jobs"
    (jobs_dir / "20260905-ab12").mkdir(parents=True)
    job = {"id": "20260905-ab12", "state": "rendered",
           "script": {"hook": "h", "beats": [], "cta": "c"},
           "assets": {}, "render": {"final": "final.mp4"},
           "telegram": {"message_id": 1, "approved_by": None}, "errors": []}
    (jobs_dir / "20260905-ab12" / "job.json").write_text(json.dumps(job))

    db = str(tmp / "posting.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE projects(alias TEXT PRIMARY KEY, name TEXT,
            pipeline_kind TEXT, pipeline_path TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE channels(id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_alias TEXT, platform TEXT, handle TEXT, token_path TEXT,
            daily_limit INTEGER, publish_times_utc TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE items(id TEXT PRIMARY KEY, channel_id INTEGER, title TEXT,
            local_path TEXT, state TEXT, source_ref TEXT, scheduled_at TEXT,
            published_url TEXT, error TEXT, meta TEXT, created_at TEXT,
            updated_at TEXT);
        CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_alias TEXT, pipeline TEXT, started_at TEXT,
            finished_at TEXT, status TEXT, summary TEXT, log TEXT,
            created_at TEXT);
        INSERT INTO projects VALUES('dressit-fshorts','FS','fshorts',
            '/root/fshorts',NULL,'t');
        INSERT INTO projects VALUES('chesstourism','Chess','chess-shorts',
            '/root/_chess',NULL,'t');
        INSERT INTO items(id,state,source_ref) VALUES
            ('fshorts-20260905-ab12','review','20260905-ab12');
        INSERT INTO items(id,state,source_ref) VALUES
            ('chess-match_legals_mate','review','match_legals_mate');
    """)
    conn.commit()
    conn.close()

    secret = "test-secret"
    os.environ["POSTING_DB"] = db
    adapters.register("dressit-fshorts",
                      adapters.FShortsAdapter("dressit-fshorts",
                                              jobs_dir=str(jobs_dir)))
    Handler.secret = secret
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        # liveness + input validation
        assert get("/healthz") == 200
        assert get("/nope") == 400
        assert get("/a/dressit-fshorts/20260905-ab12/wrongsig") == 403
        assert get("/a/20260905-ab12/wrongsig") == 403
        assert get("/a/ghost-proj/job1/" + sig_for(secret, "ghost-proj", "job1")) == 404
        # legacy route: unknown job
        assert get("/a/20999999-zz99/" + legacy_sig_for(secret, "20999999-zz99")) == 404

        # NEW format: regenerate clears script/assets/render
        links = links_for(secret, base, "dressit-fshorts", "20260905-ab12")
        assert get(links["g"][len(base):]) == 200
        out = json.loads((jobs_dir / "20260905-ab12" / "job.json").read_text())
        assert out["state"] == "new" and "script" not in out and "render" not in out
        assert out["telegram"]["message_id"] is None
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT state FROM items WHERE id='fshorts-20260905-ab12'").fetchone()
        assert row[0] == "new", row

        # LEGACY format (old Telegram caption link): approve
        legacy = f"/a/20260905-ab12/{legacy_sig_for(secret, '20260905-ab12')}"
        assert get(legacy) == 200
        out = json.loads((jobs_dir / "20260905-ab12" / "job.json").read_text())
        assert out["state"] == "approved"
        row = conn.execute(
            "SELECT state FROM items WHERE id='fshorts-20260905-ab12'").fetchone()
        assert row[0] == "approved", row

        # hub-fallback adapter (chess item lives only in posting.db)
        chess = f"/r/chesstourism/match_legals_mate/" \
                f"{sig_for(secret, 'chesstourism', 'match_legals_mate')}"
        assert get(chess) == 200
        row = conn.execute(
            "SELECT state FROM items WHERE id='chess-match_legals_mate'").fetchone()
        assert row[0] == "rejected", row
        # hub-fallback 404 for unknown item
        miss = f"/a/chesstourism/nope/" + sig_for(secret, "chesstourism", "nope")
        assert get(miss) == 404

        # audit: one runs row per non-400 decision
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE pipeline='approval'").fetchone()[0]
        assert n_runs == 5, n_runs  # fshorts g + legacy a + chess r + chess miss + legacy-404
        conn.close()
    finally:
        srv.shutdown()
        adapters._REGISTRY.pop("dressit-fshorts", None)
        adapters.register("dressit-fshorts",
                          adapters.FShortsAdapter("dressit-fshorts"))
        os.environ.pop("POSTING_DB", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELFTEST PASS")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="shared URL approval server")
    ap.add_argument("--serve", action="store_true", help="run the server (default)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    load_env_files()
    secret = os.environ.get("APPROVAL_SECRET")
    if not secret:
        print("ERROR: APPROVAL_SECRET is not set "
              "(add it to /root/.video-env or /root/fshorts/.env)",
              file=sys.stderr)
        return 1
    port = int(os.environ.get("APPROVAL_PORT", "8477"))
    Handler.secret = secret
    log(f"listening on 0.0.0.0:{port} (projects: registry + hub fallback)")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
