#!/usr/bin/env python3
"""Run the yt-analytics reauth flow for all posting-hub YouTube tokens
using run_local_server (browser lives on this same machine via noVNC).

For each token: print the channel, open the consent URL in the :111
chrome, block until Google redirects to the local listener, then merge
and atomically write the new token back. Progress goes to stdout.

Usage: DISPLAY=:111 python3 reauth_all_local.py
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
PORT = 8777

TOKENS = [
    ("/root/dressit-shorts/token_youtube_wheretogosf.json", "@dressitsf"),
    ("/root/dressit-shorts/token_youtube_la.json", "@dressitla"),
    ("/root/dressit-shorts/token_youtube_nyc.json", "@dressitnewyork"),
    ("/root/dressit-shorts/token_youtube_miami.json", "@dressitmiami"),
    ("/root/dressit-shorts/token_youtube_official.json", "@DressIt-m6d"),
    ("/root/_chess/youtube_token.json", "@ChessTourGo"),
    ("/root/_chess/youtube_token_chess_events.json", "@chesstourevents"),
]


def save_token(token_path, data, creds):
    data["token"] = creds.token
    if creds.refresh_token:
        data["refresh_token"] = creds.refresh_token
    exp = creds.expiry
    if exp is not None:
        if exp.tzinfo is None:
            data["expiry"] = exp.isoformat() + "Z"
        else:
            data["expiry"] = exp.astimezone(
                __import__("datetime").timezone.utc).isoformat(
                ).replace("+00:00", "Z")
    data["scopes"] = list(creds.scopes or data.get("scopes") or [])
    tmp_path = token_path + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp_path, token_path)


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    only = sys.argv[1:] or None
    for token_path, handle in TOKENS:
        if only and token_path not in only:
            continue
        with open(token_path) as fh:
            data = json.load(fh)
        scopes = list(data.get("scopes") or [])
        if ANALYTICS_SCOPE in scopes:
            print("SKIP %s (%s) — scope already present" % (handle,
                  token_path), flush=True)
            continue
        scopes.append(ANALYTICS_SCOPE)
        client_config = {"installed": {
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": data.get("token_uri",
                                  "https://oauth2.googleapis.com/token"),
            "redirect_uris": ["http://localhost"]}}
        flow = InstalledAppFlow.from_client_config(client_config, scopes)
        flow.redirect_uri = "http://127.0.0.1:%d/" % PORT
        auth_url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true",
            prompt="consent")
        print("WAIT %s — логинься в аккаунт %s и жми Разрешить" %
              (handle, handle), flush=True)
        subprocess.Popen(["google-chrome", "--no-sandbox", "--disable-gpu",
                          "--password-store=basic",
                          "--user-data-dir=/root/"
                          "_vnc-ytanalytics/chrome-profile", auth_url],
                         env={**os.environ, "DISPLAY": ":111"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hit = {}

        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hit["url"] = "http://127.0.0.1:%d%s" % (PORT, self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("OK! Token saved. Следующий канал "
                                 "откроется сам.".encode())

            def log_message(self, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
        srv.timeout = 600
        srv.handle_request()
        srv.server_close()
        if "url" not in hit:
            print("TIMEOUT %s — никто не подтвердил за 10 минут" % handle,
                  flush=True)
            continue
        try:
            flow.fetch_token(authorization_response=hit["url"])
        except Exception as exc:
            print("FAIL %s — %s: %s" % (handle, type(exc).__name__, exc),
                  flush=True)
            continue
        save_token(token_path, data, flow.credentials)
        print("DONE %s — token updated with yt-analytics.readonly" % handle,
              flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
