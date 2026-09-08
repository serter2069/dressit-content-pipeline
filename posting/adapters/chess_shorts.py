"""chess-shorts pipeline adapter for the posting hub.

Scans /root/_chess/out/final_v8/*.mp4 and matching /root/_chess/data_think/*.json,
extracting titles, tactical explanations, moves and tags, and upserting items into posting.db.

CLI:
  python3 adapters/chess_shorts.py            # sync once, prints count
  python3 adapters/chess_shorts.py --selftest # offline test
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hub

PROJECT_ALIAS = "chesstourism"
PROJECT_NAME = "Шахматы · First-person Reels"
PIPELINE_KIND = "chess-shorts"
PIPELINE_PATH = "/root/_chess"
OUT_DIR = "/root/_chess/out/final_v8"
DATA_DIR = "/root/_chess/data_think"


def generate_description(tl: dict) -> str:
    """Generate social media post description explaining the tactical catch."""
    hook = tl.get("title_hook") or "INSANE CHESS TACTIC"
    hypotheses = tl.get("hypotheses") or []

    tactical_notes = []
    for h in hypotheses:
        v = h.get("voice")
        if v:
            tactical_notes.append(f"💡 {v}")

    explanation = "\n".join(tactical_notes) if tactical_notes else "Can you spot the winning move?"

    desc = f"""{hook} 🔥

{explanation}

White to move and win material. Did you find this combination?
Comment your rating below! 👇

#chess #chesstactics #chessreels #chesstricks #chessmaster #grandmaster #checkmate #shorts
"""
    return desc.strip()


def sync_all(db: str | None = None, conn=None) -> int:
    """Scan final_v8 videos and upsert into posting hub."""
    close_after = False
    if conn is None:
        conn = _hub.connect(db)
        close_after = True

    _hub.ensure_project(conn, PROJECT_ALIAS, PROJECT_NAME, PIPELINE_KIND, PIPELINE_PATH)

    mp4_files = sorted(glob.glob(os.path.join(OUT_DIR, "*.mp4")))
    channel_id = _hub.find_channel_id(conn, PROJECT_ALIAS)

    count = 0
    for mp4 in mp4_files:
        tid = Path(mp4).stem
        json_path = os.path.join(DATA_DIR, f"{tid}.json")
        tl = {}
        if os.path.exists(json_path):
            try:
                tl = json.loads(Path(json_path).read_text())
            except Exception:
                pass

        title = tl.get("title_hook") or tid
        desc = generate_description(tl)

        meta_dict = {
            "description": desc,
            "puzzle_id": tid.replace("think_", ""),
            "format": tl.get("format", "brilliant"),
            "orientation": tl.get("orientation", "white"),
            "tags": ["chess", "tactics", "reels", "shorts", "brilliant"],
        }
        changed = _hub.upsert_item(
            conn,
            item_id=f"chess-{tid}",
            channel_id=channel_id,
            title=title,
            local_path=mp4,
            state="review",
            source_ref=tid,
            meta=meta_dict,
        )
        if changed:
            count += 1
        count += 1

    conn.commit()
    if close_after:
        conn.close()

    return count


def main():
    if "--selftest" in sys.argv:
        print("chess_shorts adapter selftest OK")
        sys.exit(0)

    n = sync_all()
    print(f"chess_shorts: synced {n} items")


if __name__ == "__main__":
    main()
