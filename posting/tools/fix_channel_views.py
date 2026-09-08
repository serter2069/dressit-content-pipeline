#!/usr/bin/env python3
"""One-time fix for proto dressit#1950.

Channel-level views stored before the fix came from the YouTube
channels.list statistics.viewCount counter, which lags badly (returns
0 for months on new channels). The trustworthy number is SUM(views)
over the channel's videos in analytics_videos. analytics.py now writes
that sum, so every historical channel-level total is unreliable and
gets wiped here; refresh() immediately writes a corrected baseline.

Steps: back up posting.db, truncate analytics_channel_daily and
analytics_channel_snapshots, run analytics.refresh().
"""

import os
import shutil
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import analytics  # noqa: E402

DB_PATH = os.path.join(BASE_DIR, "data", "posting.db")
BACKUP_PATH = DB_PATH + ".bak-20260907-channelviews"


def main():
    if not os.path.exists(BACKUP_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print("backup written: %s" % BACKUP_PATH)
    else:
        print("backup already exists: %s" % BACKUP_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        for table in ("analytics_channel_daily",
                      "analytics_channel_snapshots"):
            cur = conn.execute("DELETE FROM %s" % table)
            print("%s: deleted %d rows" % (table, cur.rowcount))
        conn.commit()
    finally:
        conn.close()

    overview = analytics.refresh(DB_PATH)
    print("refresh done, refreshed_at=%s" % overview["refreshed_at"])
    for ch in overview["channels"]:
        if ch["error"]:
            print("%-24s ERROR: %s" % (ch["project_alias"], ch["error"]))
        else:
            print("%-24s views=%-10d views_api=%s"
                  % (ch["project_alias"], ch["views"],
                     ch.get("views_api")))


if __name__ == "__main__":
    main()
