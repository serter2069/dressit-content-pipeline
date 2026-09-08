"""One-off refill of analytics_video_daily history: re-runs the YouTube
Analytics per-day backfill for every channel whose token carries the
yt-analytics.readonly scope, then rebuilds analytics_channel_daily.
Needed after the gap-filling fix (zero-view days were missing)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics


def _token_has_analytics_scope(token_path):
    try:
        with open(token_path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return analytics.ANALYTICS_SCOPE in (data.get("scopes") or [])


def main():
    conn = analytics._open_db(analytics.DEFAULT_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT * FROM channels WHERE active = 1 "
            "AND platform = 'youtube' AND token_path IS NOT NULL "
            "AND token_path != '' ORDER BY project_alias, id").fetchall()
        fetched_at = analytics._now()
        for r in rows:
            if not _token_has_analytics_scope(r["token_path"]):
                print("channel %s: no yt-analytics scope, skip" % r["id"])
                continue
            try:
                creds = analytics._load_credentials(r["token_path"])
                analytics._backfill_video_history(
                    conn, creds, r["id"], fetched_at)
                analytics._backfill_channel_daily(conn, r["id"])
                count = conn.execute(
                    "SELECT COUNT(*) FROM analytics_video_daily "
                    "WHERE channel_id = ?", (r["id"],)).fetchone()[0]
                print("channel %s: refilled, %s video daily rows"
                      % (r["id"], count))
            except Exception as exc:
                print("channel %s: ERROR %s: %s"
                      % (r["id"], type(exc).__name__, exc))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
