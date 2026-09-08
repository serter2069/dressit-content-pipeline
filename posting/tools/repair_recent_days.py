"""One-off repair of yesterday's daily rows. The video-history backfill
anchored its cumulative series at today's totals while the YouTube
Analytics per-day rows for today/yesterday were still missing (API lag),
so today's views were attributed to yesterday. Re-anchor yesterday to
the first snapshot taken today (the true end-of-yesterday value):
per-video rows from analytics_video_snapshots, per-channel rows via
analytics._finalize_yesterday. Backs up the DB before touching it."""
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics

BACKUP_PATH = analytics.DEFAULT_DB_PATH + ".bak-20260907-repair"


def main():
    if not os.path.exists(BACKUP_PATH):
        shutil.copyfile(analytics.DEFAULT_DB_PATH, BACKUP_PATH)
    print("backup: %s" % BACKUP_PATH)
    conn = analytics._open_db(analytics.DEFAULT_DB_PATH)
    try:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        print("today=%s yesterday=%s" % (today, yesterday))

        snaps = conn.execute(
            "SELECT s.channel_id, s.video_id, s.fetched_at, s.views "
            "FROM analytics_video_snapshots s "
            "JOIN (SELECT channel_id, video_id, MIN(fetched_at) AS first "
            "      FROM analytics_video_snapshots "
            "      WHERE substr(fetched_at, 1, 10) = ? "
            "      GROUP BY channel_id, video_id) f "
            "ON s.channel_id = f.channel_id AND s.video_id = f.video_id "
            "AND s.fetched_at = f.first", (today,)).fetchall()
        updated = inserted = 0
        for s in snaps:
            cur = conn.execute(
                "UPDATE analytics_video_daily SET views = ?, "
                "fetched_at = ? WHERE date = ? AND channel_id = ? "
                "AND video_id = ?",
                (analytics._to_int(s["views"]), s["fetched_at"],
                 yesterday, s["channel_id"], s["video_id"]))
            if cur.rowcount:
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO analytics_video_daily "
                    "(date, channel_id, video_id, views, likes, "
                    "comments, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    (yesterday, s["channel_id"], s["video_id"],
                     analytics._to_int(s["views"]), 0, 0,
                     s["fetched_at"]))
                inserted += 1
        print("video_daily[%s]: %s updated, %s inserted"
              % (yesterday, updated, inserted))

        channel_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM analytics_channel_snapshots "
            "WHERE substr(fetched_at, 1, 10) = ?", (today,))]
        for channel_id in channel_ids:
            analytics._finalize_yesterday(conn, channel_id)
        print("channel_daily[%s]: finalized for channels %s"
              % (yesterday, channel_ids))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
