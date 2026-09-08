#!/usr/bin/env python3
"""fshorts#25 — YouTube Analytics API re-auth for posting hub channels.

Re-authorizes the 6 active YouTube channels with the ADDITIONAL scope
yt-analytics.readonly (existing youtube.upload + youtube.force-ssl are
kept, incremental auth). New tokens are written to /root/posting/tokens/
(chmod 600); the original token files in /root/dressit-shorts and
/root/_chess are never touched.

Modes:
  serve              run the local OAuth receiver on 127.0.0.1:8487 and
                     walk through all 6 channels in one browser session
                     (open http://127.0.0.1:8487/ inside the noVNC browser)
  --check [alias]    verify yt-analytics access for new token(s): calls
                     youtubeAnalytics.reports.query (ids==channel==MINE,
                     metrics=views, dimensions=day, last 28 days) and
                     prints the row count per channel
  --status           list channels, expected YouTube ids, token state
"""

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CLIENT_SECRETS = "/root/fshorts/youtube_client_secrets.json"
TOKENS_DIR = "/root/posting/tokens"
DB_PATH = "/root/posting/data/posting.db"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8487
REDIRECT_URI = "http://127.0.0.1:%d/callback" % LISTEN_PORT

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

PAGE = """<!doctype html><meta charset="utf-8"><title>YT Analytics re-auth</title>
<style>
body{font-family:sans-serif;max-width:760px;margin:40px auto;padding:0 16px}
td,th{padding:8px 12px;border-bottom:1px solid #ddd;text-align:left}
.ok{color:#090;font-weight:bold}.bad{color:#c00;font-weight:bold}
a.btn{display:inline-block;background:#1a73e8;color:#fff;padding:10px 18px;
border-radius:6px;text-decoration:none;font-size:16px}
.note{background:#fff8e1;border:1px solid #e0c060;padding:10px 14px;
border-radius:6px;margin:16px 0}
</style>"""


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def channels():
    """Active youtube channels that analytics.py refreshes (have token_path),
    joined with the last known YouTube channel id/title for verification."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT c.id, c.project_alias, c.handle, c.account, "
            "c.token_path, "
            "(SELECT d.yt_channel_id FROM analytics_channel_daily d "
            " WHERE d.channel_id = c.id "
            " ORDER BY d.fetched_at DESC LIMIT 1) AS yt_channel_id, "
            "(SELECT d.title FROM analytics_channel_daily d "
            " WHERE d.channel_id = c.id "
            " ORDER BY d.fetched_at DESC LIMIT 1) AS title "
            "FROM channels c WHERE c.active = 1 AND c.platform = 'youtube' "
            "AND c.token_path IS NOT NULL AND c.token_path != '' "
            "ORDER BY c.project_alias, c.id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def token_path_for(alias):
    return os.path.join(TOKENS_DIR, alias + ".json")


def _new_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS, scopes=SCOPES, redirect_uri=REDIRECT_URI)


def _save_token(alias, creds):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
    }
    path = token_path_for(alias)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _authorized_channel(creds):
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=creds,
                    cache_discovery=False)
    resp = youtube.channels().list(mine=True, part="snippet").execute()
    items = resp.get("items") or []
    if not items:
        return None, None
    ch = items[0]
    return ch.get("id"), (ch.get("snippet") or {}).get("title")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("reauth: " + fmt % args + "\n")

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/":
            self._index()
        elif url.path == "/auth":
            self._auth(qs.get("ch", [""])[0])
        elif url.path == "/callback":
            self._callback(qs)
        else:
            self._send("not found", status=404, ctype="text/plain")

    def _index(self):
        chans = channels()
        rows = []
        for ch in chans:
            alias = ch["project_alias"]
            done = os.path.exists(token_path_for(alias))
            state = ('<span class="ok">готово ✓</span>' if done
                     else '<a class="btn" href="/auth?ch=%s">'
                          'Авторизовать</a>' % alias)
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td>"
                        "<td>%s</td></tr>"
                        % (alias, ch["handle"] or "", ch["account"] or "",
                           state))
        pending = [c for c in chans
                   if not os.path.exists(token_path_for(c["project_alias"]))]
        hint = ("<div class='note'>Осталось каналов: %d. Для каждого нажми "
                "«Авторизовать», войди в Google-аккаунт <b>этого канала</b> "
                "и нажми «Продолжить» на экране согласия.</div>"
                % len(pending) if pending else
                "<div class='note ok'>Все 6 каналов авторизованы. "
                "Можно закрыть вкладку и сообщить оркестратору.</div>")
        self._send(PAGE + "<h1>YouTube Analytics — переавторизация "
                   "(fshorts#25)</h1>" + hint +
                   "<table><tr><th>Канал</th><th>Хендл</th>"
                   "<th>Аккаунт</th><th></th></tr>%s</table>" % "".join(rows))

    def _auth(self, alias):
        chans = {c["project_alias"]: c for c in channels()}
        if alias not in chans:
            self._send(PAGE + "<p class='bad'>Unknown channel alias: %s</p>"
                       "<p><a href='/'>← назад</a></p>" % alias, status=400)
            return
        flow = _new_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true",
            prompt="consent", state=alias)
        self.send_response(302)
        self.send_header("Location", auth_url)
        self.end_headers()

    def _callback(self, qs):
        alias = qs.get("state", [""])[0]
        if qs.get("error"):
            self._send(PAGE + "<p class='bad'>Google вернул ошибку: %s</p>"
                       "<p><a href='/'>← назад</a></p>"
                       % qs["error"][0], status=400)
            return
        code = qs.get("code", [""])[0]
        chans = {c["project_alias"]: c for c in channels()}
        ch = chans.get(alias)
        if not code or ch is None:
            self._send(PAGE + "<p class='bad'>Bad callback (no code or "
                       "unknown state).</p><p><a href='/'>← назад</a></p>",
                       status=400)
            return
        flow = _new_flow()
        try:
            flow.fetch_token(code=code)
        except Exception as exc:
            self._send(PAGE + "<p class='bad'>Token exchange failed: %s</p>"
                       "<p><a href='/'>← назад</a></p>" % exc, status=500)
            return
        creds = flow.credentials
        if not creds.refresh_token:
            self._send(PAGE + "<p class='bad'>Google не вернул "
                       "refresh_token — попробуй ещё раз.</p>"
                       "<p><a href='/'>← назад</a></p>", status=500)
            return
        try:
            yt_id, yt_title = _authorized_channel(creds)
        except Exception as exc:
            self._send(PAGE + "<p class='bad'>channels.list failed: %s</p>"
                       "<p><a href='/'>← назад</a></p>" % exc, status=500)
            return
        if ch["yt_channel_id"] and yt_id != ch["yt_channel_id"]:
            self._send(
                PAGE + "<h2 class='bad'>Не тот канал!</h2>"
                "<p>Ты вошёл в канал <b>%s</b> (<code>%s</code>), "
                "а нужен <b>%s</b> (<code>%s</code>).</p>"
                "<p>Токен НЕ сохранён. Выйди из этого Google-аккаунта "
                "или выбери другой аккаунт на экране входа.</p>"
                "<p><a class='btn' href='/auth?ch=%s'>Попробовать снова "
                "(%s)</a> <a href='/'>← к списку</a></p>"
                % (yt_title, yt_id, ch["title"] or ch["handle"],
                   ch["yt_channel_id"], alias, alias), status=409)
            return
        path = _save_token(alias, creds)
        nxt = next((c["project_alias"] for c in channels()
                    if not os.path.exists(
                        token_path_for(c["project_alias"]))), None)
        nxt_html = ("<p><a class='btn' href='/auth?ch=%s'>Следующий канал: "
                    "%s →</a></p>" % (nxt, nxt) if nxt else
                    "<p class='ok'><b>Все каналы авторизованы!</b> "
                    "Сообщи оркестратору.</p>")
        self._send(PAGE + "<h2 class='ok'>OK: %s</h2>"
                   "<p>Канал: <b>%s</b> (<code>%s</code>). "
                   "Токен сохранён в <code>%s</code>.</p>%s"
                   "<p><a href='/'>← к списку</a></p>"
                   % (alias, yt_title, yt_id, path, nxt_html))


def serve():
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print("reauth receiver on http://%s:%d/ (%d channels)"
          % (LISTEN_HOST, LISTEN_PORT, len(channels())))
    server.serve_forever()


def check(only=None):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=27)
    ok = True
    for ch in channels():
        alias = ch["project_alias"]
        if only and alias != only:
            continue
        path = token_path_for(alias)
        if not os.path.exists(path):
            print("%-16s NO TOKEN (%s)" % (alias, path))
            ok = False
            continue
        try:
            with open(path) as fh:
                data = json.load(fh)
            creds = Credentials(
                token=data.get("token"),
                refresh_token=data.get("refresh_token"),
                token_uri=data.get("token_uri"),
                client_id=data.get("client_id"),
                client_secret=data.get("client_secret"),
                scopes=data.get("scopes"))
            yta = build("youtubeAnalytics", "v2", credentials=creds,
                        cache_discovery=False)
            resp = yta.reports().query(
                ids="channel==MINE", startDate=start.isoformat(),
                endDate=end.isoformat(), metrics="views",
                dimensions="day").execute()
            rows = resp.get("rows") or []
            total = sum(int(r[1]) for r in rows)
            print("%-16s rows=%-3d %s..%s total_views=%d"
                  % (alias, len(rows), rows[0][0] if rows else "-",
                     rows[-1][0] if rows else "-", total))
        except Exception as exc:
            print("%-16s ERROR: %s: %s" % (alias, type(exc).__name__, exc))
            ok = False
    return ok


def status():
    for ch in channels():
        alias = ch["project_alias"]
        new = token_path_for(alias)
        print("%-16s %-18s %-24s expected=%s (%s)\n  old: %s\n  new: %s %s"
              % (alias, ch["handle"], ch["account"],
                 ch["yt_channel_id"] or "?", ch["title"] or "?",
                 ch["token_path"], new,
                 "EXISTS" if os.path.exists(new) else "pending"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--check" in args:
        idx = args.index("--check")
        only = args[idx + 1] if idx + 1 < len(args) else None
        sys.exit(0 if check(only) else 1)
    elif "--status" in args:
        status()
    else:
        serve()
