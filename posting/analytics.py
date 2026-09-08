#!/usr/bin/env python3
"""posting hub — YouTube analytics snapshots.

Fetches per-channel stats (subscribers, views, video counts, recent
video performance) from the YouTube Data API and caches daily snapshots
in the posting hub SQLite DB so the dashboard renders without network.

Public interface (consumed by server.py):
    ensure_schema(conn)      -- create analytics tables if missing
    refresh(db_path) -> dict -- hit the live API, store snapshots, return
                                the overview dict
    overview(db_path) -> dict-- read cached DB data only (no network),
                                same dict shape, "refreshing": false

Run `python3 analytics.py` for a live refresh against the real DB.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "posting.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_channel_daily (
    date          TEXT,
    channel_id    INTEGER,
    yt_channel_id TEXT,
    title         TEXT,
    views         INTEGER,
    subs          INTEGER,
    video_count   INTEGER,
    likes         INTEGER,
    fetched_at    TEXT,
    PRIMARY KEY (date, channel_id)
);
CREATE TABLE IF NOT EXISTS analytics_videos (
    channel_id   INTEGER,
    video_id     TEXT,
    title        TEXT,
    published_at TEXT,
    views        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    fetched_at   TEXT,
    PRIMARY KEY (channel_id, video_id)
);
CREATE TABLE IF NOT EXISTS analytics_video_daily (
    date       TEXT,
    channel_id INTEGER,
    video_id   TEXT,
    views      INTEGER,
    likes      INTEGER,
    comments   INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (date, channel_id, video_id)
);
CREATE TABLE IF NOT EXISTS analytics_video_snapshots (
    channel_id INTEGER,
    video_id   TEXT,
    fetched_at TEXT,
    views      INTEGER,
    likes      INTEGER,
    comments   INTEGER,
    PRIMARY KEY (channel_id, video_id, fetched_at)
);
CREATE TABLE IF NOT EXISTS analytics_channel_snapshots (
    channel_id    INTEGER,
    fetched_at    TEXT,
    yt_channel_id TEXT,
    title         TEXT,
    views         INTEGER,
    subs          INTEGER,
    video_count   INTEGER,
    likes         INTEGER,
    PRIMARY KEY (channel_id, fetched_at)
);
"""

SNAPSHOT_KEEP = 2000
VIDEO_SNAPSHOT_KEEP = 500

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn):
    """Create the analytics tables if they do not exist, and make sure
    channels carries the (human-editable) account label column."""
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)")]
    if "account" not in cols:
        conn.execute("ALTER TABLE channels ADD COLUMN account TEXT")
    for table in ("analytics_channel_daily", "analytics_channel_snapshots"):
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % table)]
        if "avatar" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN avatar TEXT" % table)
        if "views_api" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN views_api INTEGER"
                         % table)
    # One-time, idempotent backfill: seed the snapshot series from the
    # existing daily rows (noon UTC) so historical dates show up in charts.
    conn.execute(
        "INSERT OR IGNORE INTO analytics_channel_snapshots "
        "(channel_id, fetched_at, yt_channel_id, title, views, subs, "
        "video_count, likes) "
        "SELECT d.channel_id, d.date || 'T12:00:00Z', d.yt_channel_id, "
        "d.title, d.views, d.subs, d.video_count, d.likes "
        "FROM analytics_channel_daily d "
        "WHERE NOT EXISTS (SELECT 1 FROM analytics_channel_snapshots s "
        "                  WHERE s.channel_id = d.channel_id "
        "                  AND substr(s.fetched_at, 1, 10) = d.date)")
    # Same for per-video daily rows: seed from the current analytics_videos
    # state so the new table has a starting point for every known video.
    conn.execute(
        "INSERT OR IGNORE INTO analytics_video_daily "
        "(date, channel_id, video_id, views, likes, comments, fetched_at) "
        "SELECT substr(fetched_at, 1, 10), channel_id, video_id, views, "
        "likes, comments, fetched_at FROM analytics_videos")


def _open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------- oauth

def _parse_expiry(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # google-auth compares expiry against a naive utcnow().
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _load_credentials(token_path):
    """Build google Credentials from a token JSON file, refreshing (and
    atomically writing back) the token when it is expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with open(token_path) as fh:
        data = json.load(fh)
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
        expiry=_parse_expiry(data.get("expiry")),
    )
    if not creds.valid:
        creds.refresh(Request())
        data["token"] = creds.token
        if creds.expiry is not None:
            exp = creds.expiry
            if exp.tzinfo is None:
                data["expiry"] = exp.isoformat() + "Z"
            else:
                data["expiry"] = exp.astimezone(timezone.utc).isoformat(
                    ).replace("+00:00", "Z")
        tmp_path = token_path + ".tmp"
        with open(tmp_path, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, token_path)
    return creds


# --------------------------------------------------------------- fetching

def _fetch_channel(conn, creds, channel_row, fetched_at):
    """Pull stats for one channel row and store snapshots in the DB."""
    from googleapiclient.discovery import build

    channel_id = channel_row["id"]
    youtube = build("youtube", "v3", credentials=creds,
                    cache_discovery=False)

    resp = youtube.channels().list(
        mine=True, part="snippet,statistics,contentDetails").execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError("channels.list returned no channel")
    ch = items[0]
    stats = ch.get("statistics") or {}
    yt_channel_id = ch.get("id")
    title = (ch.get("snippet") or {}).get("title")
    thumbnails = ((ch.get("snippet") or {}).get("thumbnails") or {})
    avatar = ((thumbnails.get("medium") or {}).get("url")
              or (thumbnails.get("default") or {}).get("url"))
    views_api = _to_int(stats.get("viewCount"))
    subs = _to_int(stats.get("subscriberCount"))
    video_count = _to_int(stats.get("videoCount"))
    uploads = ((ch.get("contentDetails") or {})
               .get("relatedPlaylists") or {}).get("uploads")

    views = 0
    likes = 0
    if uploads:
        pl = youtube.playlistItems().list(
            playlistId=uploads, part="contentDetails",
            maxResults=50).execute()
        ids = [(it.get("contentDetails") or {}).get("videoId")
               for it in pl.get("items") or []]
        ids = [v for v in ids if v]
        if ids:
            vids = youtube.videos().list(
                id=",".join(ids), part="snippet,statistics").execute()
            for v in vids.get("items") or []:
                vs = v.get("statistics") or {}
                v_likes = _to_int(vs.get("likeCount"))
                likes += v_likes
                views += _to_int(vs.get("viewCount"))
                conn.execute(
                    "INSERT OR REPLACE INTO analytics_videos "
                    "(channel_id, video_id, title, published_at, views, "
                    "likes, comments, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (channel_id, v.get("id"),
                     (v.get("snippet") or {}).get("title"),
                     (v.get("snippet") or {}).get("publishedAt"),
                     _to_int(vs.get("viewCount")), v_likes,
                     _to_int(vs.get("commentCount")), fetched_at))
                conn.execute(
                    "INSERT OR REPLACE INTO analytics_video_daily "
                    "(date, channel_id, video_id, views, likes, "
                    "comments, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    (fetched_at[:10], channel_id, v.get("id"),
                     _to_int(vs.get("viewCount")), v_likes,
                     _to_int(vs.get("commentCount")), fetched_at))
                conn.execute(
                    "INSERT OR REPLACE INTO analytics_video_snapshots "
                    "(channel_id, video_id, fetched_at, views, likes, "
                    "comments) VALUES (?,?,?,?,?,?)",
                    (channel_id, v.get("id"), fetched_at,
                     _to_int(vs.get("viewCount")), v_likes,
                     _to_int(vs.get("commentCount"))))
                conn.execute(
                    "DELETE FROM analytics_video_snapshots "
                    "WHERE channel_id = ? AND video_id = ? "
                    "AND fetched_at NOT IN (SELECT fetched_at "
                    "FROM analytics_video_snapshots "
                    "WHERE channel_id = ? AND video_id = ? "
                    "ORDER BY fetched_at DESC LIMIT ?)",
                    (channel_id, v.get("id"), channel_id, v.get("id"),
                     VIDEO_SNAPSHOT_KEEP))

    try:
        _backfill_video_history(conn, creds, channel_id, fetched_at)
    except Exception as exc:
        sys.stderr.write("video history backfill failed for channel %s: "
                         "%s: %s\n" % (channel_id, type(exc).__name__, exc))

    date = fetched_at[:10]
    conn.execute(
        "INSERT OR REPLACE INTO analytics_channel_daily "
        "(date, channel_id, yt_channel_id, title, views, subs, "
        "video_count, likes, fetched_at, avatar, views_api) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (date, channel_id, yt_channel_id, title, views, subs,
         video_count, likes, fetched_at, avatar, views_api))
    conn.execute(
        "INSERT OR REPLACE INTO analytics_channel_snapshots "
        "(channel_id, fetched_at, yt_channel_id, title, views, subs, "
        "video_count, likes, avatar, views_api) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (channel_id, fetched_at, yt_channel_id, title, views, subs,
         video_count, likes, avatar, views_api))
    conn.execute(
        "DELETE FROM analytics_channel_snapshots WHERE channel_id = ? "
        "AND fetched_at NOT IN (SELECT fetched_at "
        "FROM analytics_channel_snapshots WHERE channel_id = ? "
        "ORDER BY fetched_at DESC LIMIT ?)",
        (channel_id, channel_id, SNAPSHOT_KEEP))
    _finalize_yesterday(conn, channel_id, yt_channel_id, title, avatar)

    return {"yt_channel_id": yt_channel_id, "title": title,
            "views": views, "views_api": views_api, "subs": subs,
            "video_count": video_count, "likes": likes, "avatar": avatar}


def _cumulative_from_daily(total_now, rows):
    """Turn per-day views rows [(date, views)] into cumulative end-of-day
    views anchored at today: cum(last day) = total_now, going backwards
    subtracting each day's per-day views. Days missing from rows count
    as zero."""
    per_day = {day: _to_int(views) for day, views in rows}
    cum = _to_int(total_now)
    out = {}
    for day in sorted(per_day, reverse=True):
        out[day] = cum
        cum -= per_day[day]
    return out


def _backfill_video_history(conn, creds, channel_id, fetched_at):
    """Per-day views history via the YouTube Analytics API for every video
    of the channel, stored as cumulative end-of-day views (same semantics
    as the Data API snapshots). Runs only when the token carries the
    yt-analytics.readonly scope; silently skipped otherwise."""
    if ANALYTICS_SCOPE not in (creds.scopes or []):
        return
    from googleapiclient.discovery import build

    yta = build("youtubeAnalytics", "v2", credentials=creds,
                cache_discovery=False)
    today = fetched_at[:10]
    floor = (datetime.now(timezone.utc) - timedelta(days=90)
             ).strftime("%Y-%m-%d")
    videos = conn.execute(
        "SELECT video_id, published_at, views FROM analytics_videos "
        "WHERE channel_id = ?", (channel_id,)).fetchall()
    for v in videos:
        start = floor
        if v["published_at"] and v["published_at"][:10] > start:
            start = v["published_at"][:10]
        resp = yta.reports().query(
            ids="channel==MINE", startDate=start, endDate=today,
            metrics="views", dimensions="day",
            filters="video==%s" % v["video_id"]).execute()
        # YouTube omits days with zero views; fill the full calendar range
        # start..today so the cumulative series has no gaps.
        per_day = {row[0]: _to_int(row[1])
                   for row in resp.get("rows") or []}
        rows = []
        day_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(today, "%Y-%m-%d")
        while day_dt <= end_dt:
            key = day_dt.strftime("%Y-%m-%d")
            rows.append((key, per_day.get(key, 0)))
            day_dt += timedelta(days=1)
        cum = _cumulative_from_daily(v["views"], rows)
        existing = {r["date"]: r for r in conn.execute(
            "SELECT date, likes, comments FROM analytics_video_daily "
            "WHERE channel_id = ? AND video_id = ?",
            (channel_id, v["video_id"]))}
        # The Analytics API lags ~2 days: per-day rows for today and
        # yesterday are missing/zero, so the cumulative anchor would
        # attribute today's views to yesterday. Analytics owns only
        # finalized history — dates <= today-2; fresher days belong to
        # our own snapshots and the main Data API fetch path.
        cutoff = (datetime.strptime(today, "%Y-%m-%d")
                  - timedelta(days=1)).strftime("%Y-%m-%d")
        for day, views in cum.items():
            if day >= cutoff:
                continue
            old = existing.get(day)
            conn.execute(
                "INSERT OR REPLACE INTO analytics_video_daily "
                "(date, channel_id, video_id, views, likes, comments, "
                "fetched_at) VALUES (?,?,?,?,?,?,?)",
                (day, channel_id, v["video_id"], views,
                 _to_int(old["likes"]) if old else 0,
                 _to_int(old["comments"]) if old else 0, fetched_at))
    _backfill_channel_daily(conn, channel_id)


def _backfill_channel_daily(conn, channel_id):
    """Rebuild analytics_channel_daily history from the cumulative
    per-video end-of-day rows: views per date is SUM over the channel's
    videos. Finalized dates only (<= today-2; fresher days belong to
    our snapshots — see _backfill_video_history). Existing rows get
    views updated; new rows get zeros for subs/likes/video_count and
    channel identity copied from any existing row of the channel.
    Idempotent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)
              ).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT date, SUM(views) AS total FROM analytics_video_daily "
        "WHERE channel_id = ? AND date < ? GROUP BY date",
        (channel_id, cutoff)).fetchall()
    if not rows:
        return
    known = conn.execute(
        "SELECT yt_channel_id, title, avatar FROM analytics_channel_daily "
        "WHERE channel_id = ? ORDER BY fetched_at DESC LIMIT 1",
        (channel_id,)).fetchone()
    yt_channel_id = known["yt_channel_id"] if known else None
    title = known["title"] if known else None
    avatar = known["avatar"] if known else None
    existing = {r["date"] for r in conn.execute(
        "SELECT date FROM analytics_channel_daily WHERE channel_id = ?",
        (channel_id,))}
    for r in rows:
        if r["date"] in existing:
            conn.execute(
                "UPDATE analytics_channel_daily SET views = ? "
                "WHERE date = ? AND channel_id = ?",
                (_to_int(r["total"]), r["date"], channel_id))
        else:
            conn.execute(
                "INSERT INTO analytics_channel_daily "
                "(date, channel_id, yt_channel_id, title, views, subs, "
                "video_count, likes, fetched_at, avatar, views_api) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["date"], channel_id, yt_channel_id, title,
                 _to_int(r["total"]), 0, 0, 0,
                 r["date"] + "T12:00:00Z", avatar, None))


def _finalize_yesterday(conn, channel_id, yt_channel_id=None,
                        title=None, avatar=None):
    """Latch yesterday's channel daily row to the first snapshot taken
    today (the honest end-of-yesterday value): the Analytics backfill
    owns only finalized history (<= today-2), so yesterday comes from
    our own snapshots. Runs when a snapshot from today exists and
    yesterday's row is missing or still carries a backfill/seed
    fetched_at (ends with T12:00:00Z); rows written by a live fetch
    are left alone. Idempotent within a day."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    snap = conn.execute(
        "SELECT fetched_at, views, subs, video_count, likes FROM "
        "analytics_channel_snapshots WHERE channel_id = ? "
        "AND substr(fetched_at, 1, 10) = ? ORDER BY fetched_at LIMIT 1",
        (channel_id, today)).fetchone()
    if not snap:
        return
    row = conn.execute(
        "SELECT fetched_at FROM analytics_channel_daily "
        "WHERE date = ? AND channel_id = ?",
        (yesterday, channel_id)).fetchone()
    if row and not (row["fetched_at"] or "").endswith("T12:00:00Z"):
        return
    if row:
        conn.execute(
            "UPDATE analytics_channel_daily SET views = ?, subs = ?, "
            "video_count = ?, likes = ?, fetched_at = ? "
            "WHERE date = ? AND channel_id = ?",
            (_to_int(snap["views"]), _to_int(snap["subs"]),
             _to_int(snap["video_count"]), _to_int(snap["likes"]),
             snap["fetched_at"], yesterday, channel_id))
    else:
        if title is None:
            known = conn.execute(
                "SELECT yt_channel_id, title, avatar FROM "
                "analytics_channel_daily WHERE channel_id = ? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (channel_id,)).fetchone()
            if known:
                yt_channel_id = yt_channel_id or known["yt_channel_id"]
                title = known["title"]
                avatar = avatar or known["avatar"]
        conn.execute(
            "INSERT INTO analytics_channel_daily "
            "(date, channel_id, yt_channel_id, title, views, subs, "
            "video_count, likes, fetched_at, avatar, views_api) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (yesterday, channel_id, yt_channel_id, title,
             _to_int(snap["views"]), _to_int(snap["subs"]),
             _to_int(snap["video_count"]), _to_int(snap["likes"]),
             snap["fetched_at"], avatar, None))


# --------------------------------------------------------------- overview

def _channel_deltas(views_now, series):
    """delta_24h is views_now minus the nearest snapshot in the 20-28h
    window (None when none exists there — an older baseline would make
    the "+24h" label a lie); delta_total is views_now minus the oldest
    snapshot (None when the series has fewer than 2 points)."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    too_old = (now - timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    delta_24h = None
    for point in reversed(series):
        if point["t"] <= cutoff:
            if point["t"] >= too_old:
                delta_24h = views_now - point["views"]
            break
    delta_total = (views_now - series[0]["views"]
                   if len(series) >= 2 else None)
    return delta_24h, delta_total


def _sum_deltas(pairs):
    """Sum (delta_24h, delta_total) pairs skipping None channels; each
    total is None when no channel contributed to it."""
    d24 = [p[0] for p in pairs if p[0] is not None]
    dtot = [p[1] for p in pairs if p[1] is not None]
    return (sum(d24) if d24 else None,
            sum(dtot) if dtot else None)


def _build_overview(conn, refreshed=None):
    """Assemble the overview dict from cached rows.

    refreshed is either None (cache read) or a dict channel_id ->
    {"stats": {...}} / {"error": str} produced by a live refresh.
    """
    rows = conn.execute(
        "SELECT c.id, c.project_alias, c.handle, c.token_path, c.account "
        "FROM channels c WHERE c.active = 1 AND c.platform = 'youtube' "
        "AND c.token_path IS NOT NULL AND c.token_path != '' "
        "ORDER BY c.project_alias, c.id").fetchall()

    latest = {}
    for r in conn.execute(
            "SELECT d.* FROM analytics_channel_daily d "
            "JOIN (SELECT channel_id, MAX(fetched_at) AS mx "
            "      FROM analytics_channel_daily GROUP BY channel_id) m "
            "ON m.channel_id = d.channel_id AND m.mx = d.fetched_at"):
        latest[r["channel_id"]] = dict(r)

    channel_daily = {}
    for r in conn.execute(
            "SELECT channel_id, date, views, subs, video_count, likes "
            "FROM analytics_channel_daily "
            "ORDER BY channel_id, date"):
        channel_daily.setdefault(r["channel_id"], []).append(
            {"date": r["date"], "views": _to_int(r["views"]),
             "likes": _to_int(r["likes"]), "subs": _to_int(r["subs"]),
             "videos": _to_int(r["video_count"])})

    channel_series = {}
    for r in conn.execute(
            "SELECT channel_id, fetched_at, views, subs, video_count, "
            "likes FROM analytics_channel_snapshots "
            "ORDER BY channel_id, fetched_at"):
        channel_series.setdefault(r["channel_id"], []).append(
            {"t": r["fetched_at"], "views": _to_int(r["views"]),
             "likes": _to_int(r["likes"]), "subs": _to_int(r["subs"]),
             "videos": _to_int(r["video_count"])})

    video_rows = {}
    for r in conn.execute(
            "SELECT channel_id, video_id, title, published_at, views, "
            "likes, comments FROM analytics_videos "
            "ORDER BY channel_id, views DESC"):
        video_rows.setdefault(r["channel_id"], []).append(r)

    video_daily = {}
    for r in conn.execute(
            "SELECT channel_id, video_id, date, views "
            "FROM analytics_video_daily "
            "ORDER BY channel_id, video_id, date"):
        video_daily.setdefault(
            (r["channel_id"], r["video_id"]), []).append(
            {"date": r["date"], "views": _to_int(r["views"])})

    video_snapshots = {}
    for r in conn.execute(
            "SELECT channel_id, video_id, fetched_at, views "
            "FROM analytics_video_snapshots "
            "ORDER BY channel_id, video_id, fetched_at"):
        video_snapshots.setdefault(
            (r["channel_id"], r["video_id"]), []).append(
            {"t": r["fetched_at"], "views": _to_int(r["views"])})

    channel_videos = {}
    for cid, vids in video_rows.items():
        items = []
        for v in vids:
            key = (cid, v["video_id"])
            vd = video_daily.get(key, [])
            delta = (vd[-1]["views"] - vd[-2]["views"]
                     if len(vd) >= 2 else None)
            series = {p["t"]: p["views"] for p in
                      ({"t": d["date"] + "T12:00:00Z",
                        "views": d["views"]} for d in vd)}
            series.update({p["t"]: p["views"]
                           for p in video_snapshots.get(key, [])})
            items.append({"video_id": v["video_id"], "title": v["title"],
                          "published_at": v["published_at"],
                          "views": _to_int(v["views"]),
                          "likes": _to_int(v["likes"]),
                          "comments": _to_int(v["comments"]),
                          "url": "https://youtu.be/%s" % v["video_id"],
                          "thumb": ("https://i.ytimg.com/vi/%s/default.jpg"
                                    % v["video_id"]),
                          "daily": vd, "delta_24h": delta,
                          "series": [{"t": t, "views": series[t]}
                                     for t in sorted(series)]})
        channel_videos[cid] = items

    channels = []
    totals = {"views": 0, "subs": 0, "videos": 0, "likes": 0}
    account_stats = {}
    channel_deltas = {}
    for r in rows:
        entry = {"project_alias": r["project_alias"], "handle": r["handle"],
                 "account": r["account"] or "unknown",
                 "yt_channel_id": None, "title": None, "views": 0,
                 "subs": 0, "video_count": 0, "likes": 0, "error": None,
                 "avatar": None, "delta_24h": None, "delta_total": None,
                 "daily": channel_daily.get(r["id"], []),
                 "series": channel_series.get(r["id"], []),
                 "videos": channel_videos.get(r["id"], [])}
        live = (refreshed or {}).get(r["id"])
        snap = latest.get(r["id"])
        if live and "error" in live:
            entry["error"] = live["error"]
        if snap:
            entry["yt_channel_id"] = snap["yt_channel_id"]
            entry["title"] = snap["title"]
            entry["views"] = _to_int(snap["views"])
            entry["subs"] = _to_int(snap["subs"])
            entry["video_count"] = _to_int(snap["video_count"])
            entry["likes"] = _to_int(snap["likes"])
            entry["avatar"] = snap.get("avatar")
        if live and "stats" in live:
            s = live["stats"]
            entry.update(s)
        entry["delta_24h"], entry["delta_total"] = _channel_deltas(
            entry["views"], channel_series.get(r["id"], []))
        channel_deltas[r["id"]] = (entry["delta_24h"],
                                   entry["delta_total"])
        totals["views"] += entry["views"]
        totals["subs"] += entry["subs"]
        totals["videos"] += entry["video_count"]
        totals["likes"] += entry["likes"]
        if snap:
            acc = account_stats.setdefault(
                entry["account"],
                {"views": 0, "subs": 0, "videos": 0, "likes": 0,
                 "channels": 0, "channel_ids": []})
            acc["views"] += entry["views"]
            acc["subs"] += entry["subs"]
            acc["videos"] += entry["video_count"]
            acc["likes"] += entry["likes"]
            acc["channels"] += 1
            acc["channel_ids"].append(r["id"])
        channels.append(entry)

    accounts = []
    for name, acc in account_stats.items():
        daily_by_date = {}
        for cid in acc["channel_ids"]:
            for point in channel_daily.get(cid, []):
                slot = daily_by_date.setdefault(
                    point["date"],
                    {"date": point["date"], "views": 0, "likes": 0,
                     "subs": 0, "videos": 0})
                slot["views"] += point["views"]
                slot["likes"] += point["likes"]
                slot["subs"] += point["subs"]
                slot["videos"] += point["videos"]
        series_by_min = {}
        for cid in acc["channel_ids"]:
            per_min = {}
            for point in channel_series.get(cid, []):
                per_min[point["t"][:16]] = point
            for minute, point in per_min.items():
                slot = series_by_min.setdefault(
                    minute, {"views": 0, "likes": 0, "subs": 0,
                             "videos": 0})
                slot["views"] += point["views"]
                slot["likes"] += point["likes"]
                slot["subs"] += point["subs"]
                slot["videos"] += point["videos"]
        d24, dtot = _sum_deltas([channel_deltas[cid]
                                 for cid in acc["channel_ids"]
                                 if cid in channel_deltas])
        accounts.append({"account": name, "views": acc["views"],
                         "subs": acc["subs"], "videos": acc["videos"],
                         "likes": acc["likes"], "channels": acc["channels"],
                         "delta_24h": d24, "delta_total": dtot,
                         "daily": [daily_by_date[d]
                                   for d in sorted(daily_by_date)],
                         "series": [{"t": k + ":00Z",
                                     "views": v["views"],
                                     "likes": v["likes"],
                                     "subs": v["subs"],
                                     "videos": v["videos"]}
                                    for k, v in
                                    sorted(series_by_min.items())]})
    accounts.sort(key=lambda a: a["views"], reverse=True)

    totals["delta_24h"], totals["delta_total"] = _sum_deltas(
        list(channel_deltas.values()))

    daily = [{"date": r["date"], "views": _to_int(r["views"]),
              "subs": _to_int(r["subs"]), "videos": _to_int(r["videos"]),
              "likes": _to_int(r["likes"])}
             for r in conn.execute(
                 "SELECT date, SUM(views) AS views, SUM(subs) AS subs, "
                 "SUM(video_count) AS videos, SUM(likes) AS likes "
                 "FROM analytics_channel_daily GROUP BY date "
                 "ORDER BY date")]

    top_videos = [{"video_id": r["video_id"], "title": r["title"],
                   "handle": r["handle"],
                   "published_at": r["published_at"],
                   "views": _to_int(r["views"]),
                   "likes": _to_int(r["likes"]),
                   "comments": _to_int(r["comments"])}
                  for r in conn.execute(
                      "SELECT v.video_id, v.title, c.handle, "
                      "v.published_at, v.views, v.likes, v.comments "
                      "FROM analytics_videos v "
                      "JOIN channels c ON c.id = v.channel_id "
                      "ORDER BY v.views DESC LIMIT 30")]

    row = conn.execute(
        "SELECT MAX(fetched_at) AS mx FROM analytics_channel_daily"
        ).fetchone()
    # server.py flips "refreshing" to True while a refresh job runs;
    # the dict itself always reports the finished state.
    return {"refreshed_at": row["mx"] if row else None,
            "refreshing": False,
            "totals": totals, "channels": channels,
            "accounts": accounts,
            "daily": daily, "top_videos": top_videos}


# ------------------------------------------------------------------- api

def refresh(db_path=DEFAULT_DB_PATH):
    """Fetch fresh stats from YouTube for every active youtube channel,
    store snapshots in the DB, and return the overview dict."""
    conn = _open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM channels WHERE active = 1 "
            "AND platform = 'youtube' AND token_path IS NOT NULL "
            "AND token_path != '' ORDER BY project_alias, id").fetchall()
        fetched_at = _now()
        results = {}
        for r in rows:
            try:
                creds = _load_credentials(r["token_path"])
                results[r["id"]] = {
                    "stats": _fetch_channel(conn, creds, r, fetched_at)}
            except Exception as exc:  # one bad channel must not kill run
                results[r["id"]] = {"error": "%s: %s"
                                    % (type(exc).__name__, exc)}
        conn.commit()
        return _build_overview(conn, refreshed=results)
    finally:
        conn.close()


def overview(db_path=DEFAULT_DB_PATH):
    """Return the overview dict built from cached DB data (no network)."""
    conn = _open_db(db_path)
    try:
        return _build_overview(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    ov = refresh(path)
    print(json.dumps({"refreshed_at": ov["refreshed_at"],
                      "totals": ov["totals"]}, ensure_ascii=False, indent=2))
    for c in ov["channels"]:
        if c["error"]:
            print("%-24s %-18s ERROR: %s"
                  % (c["project_alias"], c["handle"], c["error"]))
        else:
            print("%-24s %-18s subs=%-8d views=%-12d videos=%-5d likes=%d"
                  % (c["project_alias"], c["handle"], c["subs"],
                     c["views"], c["video_count"], c["likes"]))
    print("accounts:")
    for a in ov["accounts"]:
        print("  %-24s channels=%-3d views=%d"
              % (a["account"], a["channels"], a["views"]))
