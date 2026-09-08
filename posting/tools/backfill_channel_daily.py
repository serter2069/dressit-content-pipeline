"""One-off / idempotent backfill of analytics_channel_daily history from
analytics_video_daily for every channel that has video history."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics


def main():
    conn = analytics._open_db(analytics.DEFAULT_DB_PATH)
    try:
        channel_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM analytics_video_daily "
            "ORDER BY channel_id")]
        print("channels with video history: %s" % channel_ids)
        for channel_id in channel_ids:
            analytics._backfill_channel_daily(conn, channel_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM analytics_channel_daily "
                "WHERE channel_id = ?", (channel_id,)).fetchone()[0]
            print("channel %s: %s daily rows" % (channel_id, count))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
