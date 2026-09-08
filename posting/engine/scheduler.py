#!/usr/bin/env python3
"""scheduler.py — human-like publish schedule with deterministic jitter
(proto fshorts#4).

Schedule is DEFINED per channel in the engine project config:

    "schedule": {
        "timezone": "America/Los_Angeles",
        "slots": ["11:00", "19:00"],
        "jitter_minutes": 45,
        "days": [0, 1, 2, 3, 4, 5, 6],   // weekdays, 0 = Monday
        "max_per_day": 2
    }

Actual publish time = slot + deterministic jitter, seed =
SHA256(channel|date|slot_index): the same value for everyone, forever
reproducible, visible a week ahead, but looks human from outside
(yesterday 19:07, today 19:38). No random.seed — the jitter derives
from the hash only.

Only APPROVED items publish: bind_job() attaches an item to the nearest
free computed slot and writes items.scheduled_at in the posting hub DB;
due_jobs() returns approved items whose scheduled_at is inside the
±30 min publish window. Pausing a channel = "days": [].

Channels without an engine project config fall back to their hub
channels.publish_times_utc row (UTC, default jitter) so the hub can
show one forward schedule across all projects.

CLI:
    python3 scheduler.py --plan <project_config.json> [--days 7]
    python3 scheduler.py --bind <project> <channel> <item_id>
    python3 scheduler.py --due
    python3 scheduler.py --selftest     # offline, temp DB
"""

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ENGINE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = ENGINE_DIR / "projects"
DEFAULT_DB_PATH = ENGINE_DIR.parent / "data" / "posting.db"

PUBLISH_WINDOW_MINUTES = 30
DEFAULT_JITTER_MINUTES = 30
DEFAULT_MAX_PER_DAY = 2
# binding states that hold a slot / can fire inside the window
BOUND_STATES = ("approved", "scheduled")


class ScheduleError(Exception):
    pass


def db_path():
    """Resolve DB path, honoring the POSTING_DB env override."""
    return os.environ.get("POSTING_DB", str(DEFAULT_DB_PATH))


def _connect():
    conn = sqlite3.connect(db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_minute(ts):
    """Normalize any ISO timestamp to 'YYYY-MM-DDTHH:MMZ' (UTC), or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _parse(ts):
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ------------------------------------------------------- deterministic core

def _jitter_offset(channel, date_iso, slot_index, jitter_minutes):
    """Deterministic jitter in [-jitter_minutes, +jitter_minutes].

    Seed = SHA256(channel|date|slot_index): stable across processes and
    machines, no global random state involved.
    """
    if jitter_minutes <= 0:
        return 0
    seed = "%s|%s|%d" % (channel, date_iso, slot_index)
    h = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8],
                       "big")
    return h % (2 * jitter_minutes + 1) - jitter_minutes


def compute_schedule(config_schedule, days_ahead=7, now=None):
    """Compute the forward schedule for a channel. Pure function.

    config_schedule keys: timezone, slots, jitter_minutes, days,
    max_per_day, channel (seed identity). Returns a list of dicts:
    {date, weekday, slot_index, slot, jitter_minutes_applied,
     planned_time_local, planned_time_utc} sorted chronologically.
    Paused channel ("days": []) -> empty list.
    """
    if not isinstance(config_schedule, dict):
        raise ScheduleError("schedule config must be an object")
    tz = ZoneInfo(config_schedule.get("timezone", "UTC"))
    slots = sorted(config_schedule.get("slots") or [])
    days = config_schedule.get("days")
    if days is None:
        days = list(range(7))
    days = set(int(d) for d in days)
    jitter = int(config_schedule.get("jitter_minutes", 0))
    max_per_day = int(config_schedule.get("max_per_day")
                      or max(len(slots), 1))
    channel = str(config_schedule.get("channel")
                  or config_schedule.get("timezone", "UTC"))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(tz).date()

    out = []
    for d in range(int(days_ahead)):
        day = today + timedelta(days=d)
        if day.weekday() not in days:
            continue
        for slot_index, slot in enumerate(slots[:max_per_day]):
            try:
                hh, mm = (int(x) for x in str(slot).split(":"))
            except ValueError:
                continue
            base_local = datetime(day.year, day.month, day.day, hh, mm,
                                  tzinfo=tz)
            j = _jitter_offset(channel, day.isoformat(), slot_index, jitter)
            planned_local = base_local + timedelta(minutes=j)
            planned_utc = planned_local.astimezone(timezone.utc)
            out.append({
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "slot_index": slot_index,
                "slot": slot,
                "jitter_minutes_applied": j,
                "planned_time_local": planned_local.strftime("%Y-%m-%d %H:%M"),
                "planned_time_utc": planned_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    return out


# --------------------------------------------------------- schedule lookup

def schedule_for(project, channel, conn=None):
    """Resolve the effective schedule config for a channel.

    1) engine project config projects/<project>.config.json -> "schedule";
    2) fallback: hub channels.publish_times_utc (UTC, default jitter).
    Returns None when neither defines slots (channel unscheduled/paused).
    """
    cfg_path = PROJECTS_DIR / ("%s.config.json" % project)
    if cfg_path.is_file():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            sched = cfg.get("schedule")
            if isinstance(sched, dict):
                sched = dict(sched)
                sched.setdefault("channel",
                                 cfg.get("channel") or channel or project)
                return sched
        except Exception:
            pass  # unreadable config -> try the DB fallback

    own = conn is None
    conn = conn or _connect()
    try:
        row = _find_channel(conn, project, channel)
        if row is None:
            return None
        try:
            times = json.loads(row["publish_times_utc"] or "[]")
        except (ValueError, TypeError):
            times = []
        if not times:
            return None
        return {
            "timezone": "UTC",
            "slots": times,
            "jitter_minutes": DEFAULT_JITTER_MINUTES,
            "days": list(range(7)),
            "max_per_day": row["daily_limit"] or DEFAULT_MAX_PER_DAY,
            "channel": "%s/%s" % (project, channel or row["id"]),
        }
    finally:
        if own:
            conn.close()


# -------------------------------------------------------------- db helpers

def _ensure_columns(conn):
    """Defensive: items.scheduled_at (added for fshorts#4; already in the
    canonical schema, ALTER only for older DBs)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "scheduled_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN scheduled_at TEXT")
        conn.commit()


def _find_channel(conn, project, channel):
    return conn.execute(
        "SELECT * FROM channels WHERE project_alias = ? "
        "AND (handle IS ? OR platform = ?)",
        (project, channel, channel)).fetchone()


def _channel_id(conn, project, channel):
    """Channel id for (project, channel); creates a minimal row if missing
    so engine-era projects show up in the hub schedule."""
    row = _find_channel(conn, project, channel)
    if row:
        return row["id"]
    handle = channel if str(channel).startswith("@") else None
    platform = "youtube" if handle else str(channel or "youtube")
    cur = conn.execute(
        "INSERT INTO channels (project_alias, platform, handle, active) "
        "VALUES (?,?,?,1)", (project, platform, handle))
    conn.commit()
    return cur.lastrowid


def _taken_slots(conn, channel_id):
    """Minute-normalized scheduled_at values already bound on a channel."""
    rows = conn.execute(
        "SELECT scheduled_at FROM items WHERE channel_id = ? "
        "AND scheduled_at IS NOT NULL", (channel_id,)).fetchall()
    return {_norm_minute(r[0]) for r in rows} - {None}


# ---------------------------------------------------------------- binding

def next_free_slot(project, channel, conn, schedule=None, days_ahead=14,
                   now=None):
    """First computed slot not already taken by a bound item and not in
    the past. Returns the compute_schedule entry, or None."""
    schedule = schedule or schedule_for(project, channel, conn)
    if not schedule:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    row = _find_channel(conn, project, channel)
    taken = _taken_slots(conn, row["id"]) if row else set()
    for entry in compute_schedule(schedule, days_ahead=days_ahead, now=now):
        if _parse(entry["planned_time_utc"]) <= now:
            continue
        if _norm_minute(entry["planned_time_utc"]) in taken:
            continue
        return entry
    return None


def bind_job(project, channel, item_id, conn=None, schedule=None, now=None):
    """Attach an item to the nearest free computed slot.

    Writes items.scheduled_at (creating a minimal approved item/channel
    row when the engine job is not mirrored in the hub yet). Idempotent:
    an item that already has scheduled_at keeps it. Returns
    {"item_id", "scheduled_at", "slot"} or None when no schedule/slot.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        _ensure_columns(conn)
        schedule = schedule or schedule_for(project, channel, conn)
        if not schedule:
            return None
        cid = _channel_id(conn, project, channel)
        item = conn.execute("SELECT * FROM items WHERE id = ?",
                            (item_id,)).fetchone()
        if item and item["scheduled_at"]:
            return {"item_id": item_id, "scheduled_at": item["scheduled_at"],
                    "slot": None}
        entry = next_free_slot(project, channel, conn, schedule=schedule,
                               now=now)
        if entry is None:
            return None
        if item is None:
            conn.execute(
                "INSERT INTO items (id, channel_id, title, state, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (item_id, cid, item_id, "approved", _now(), _now()))
        conn.execute(
            "UPDATE items SET scheduled_at = ?, updated_at = ? "
            "WHERE id = ?", (entry["planned_time_utc"], _now(), item_id))
        conn.commit()
        return {"item_id": item_id,
                "scheduled_at": entry["planned_time_utc"], "slot": entry}
    finally:
        if own:
            conn.close()


def due_jobs(conn=None, now=None, window_minutes=PUBLISH_WINDOW_MINUTES):
    """Approved/scheduled items whose scheduled_at is within the
    ±window_minutes publish window around now."""
    own = conn is None
    conn = conn or _connect()
    try:
        _ensure_columns(conn)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        rows = conn.execute(
            "SELECT i.*, c.project_alias AS project_alias, "
            "c.handle AS channel_handle, c.platform AS channel_platform "
            "FROM items i LEFT JOIN channels c ON c.id = i.channel_id "
            "WHERE i.state IN (%s) AND i.scheduled_at IS NOT NULL"
            % ",".join("?" * len(BOUND_STATES)), BOUND_STATES).fetchall()
        out = []
        for r in rows:
            try:
                sched = _parse(r["scheduled_at"])
            except ValueError:
                continue
            if abs((sched - now).total_seconds()) <= window_minutes * 60:
                out.append(dict(r))
        return out
    finally:
        if own:
            conn.close()


def is_due(project, channel, item_id, conn=None, now=None,
           window_minutes=PUBLISH_WINDOW_MINUTES):
    """True when the item is bound and its slot is inside the window."""
    own = conn is None
    conn = conn or _connect()
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        due = due_jobs(conn=conn, now=now, window_minutes=window_minutes)
        for r in due:
            if r["id"] == item_id and (r["project_alias"] in (None, project)):
                return True
        return False
    finally:
        if own:
            conn.close()


def published_today(project, channel, conn=None,
                    tz_name="UTC", now=None):
    """Items of the channel published on the current local day (by
    items.updated_at, the closest timestamp the hub schema has)."""
    own = conn is None
    conn = conn or _connect()
    try:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(tz_name or "UTC")
        local_now = now.astimezone(tz)
        day_start = local_now.replace(hour=0, minute=0, second=0,
                                      microsecond=0)
        lo = day_start.astimezone(timezone.utc)
        hi = day_start + timedelta(days=1)
        hi = hi.astimezone(timezone.utc)
        row = _find_channel(conn, project, channel)
        if row is None:
            return 0
        n = conn.execute(
            "SELECT COUNT(*) FROM items WHERE channel_id = ? "
            "AND state = 'published' AND updated_at >= ? AND updated_at < ?",
            (row["id"], lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
             hi.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
        return int(n)
    finally:
        if own:
            conn.close()


# -------------------------------------------------------------- hub feed

def hub_schedule(conn, days=7, now=None):
    """One forward schedule across all projects for the hub dashboard.

    Per channel: computed slots joined with bound items, plus unbound
    approved items (ждёт слота) and items in review (ждёт approve).
    """
    _ensure_columns(conn)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    chans = conn.execute(
        "SELECT * FROM channels ORDER BY project_alias, id").fetchall()
    by_day = {}
    channels_out = []
    for ch in chans:
        project, handle = ch["project_alias"], (ch["handle"] or ch["platform"])
        sched = schedule_for(project, handle, conn)
        chan_info = {"project": project, "channel": handle,
                     "channel_id": ch["id"],
                     "timezone": (sched or {}).get("timezone", "UTC"),
                     "paused": not (sched and sched.get("days"))}
        channels_out.append(chan_info)
        if not sched:
            continue
        bound = {}
        for r in conn.execute(
                "SELECT id, title, state, scheduled_at FROM items "
                "WHERE channel_id = ? AND scheduled_at IS NOT NULL",
                (ch["id"],)).fetchall():
            bound.setdefault(_norm_minute(r["scheduled_at"]), dict(r))
        for entry in compute_schedule(sched, days_ahead=days, now=now):
            item = bound.get(_norm_minute(entry["planned_time_utc"]))
            status = "free"
            if item:
                if item["state"] == "published":
                    status = "published"
                else:
                    try:
                        diff = abs((_parse(item["scheduled_at"])
                                    - now).total_seconds())
                        status = ("publish_window"
                                  if diff <= PUBLISH_WINDOW_MINUTES * 60
                                  else "scheduled")
                    except ValueError:
                        status = "scheduled"
            by_day.setdefault(entry["date"], []).append({
                "time_local": entry["planned_time_local"],
                "time_utc": entry["planned_time_utc"],
                "timezone": sched.get("timezone", "UTC"),
                "project": project,
                "channel": handle,
                "slot_index": entry["slot_index"],
                "slot": entry["slot"],
                "jitter_minutes_applied": entry["jitter_minutes_applied"],
                "item": item,
                "status": status,
            })
    unbound = [dict(r) for r in conn.execute(
        "SELECT i.id, i.title, i.state, c.project_alias AS project, "
        "COALESCE(c.handle, c.platform) AS channel FROM items i "
        "LEFT JOIN channels c ON c.id = i.channel_id "
        "WHERE i.state IN ('approved', 'review') "
        "AND i.scheduled_at IS NULL ORDER BY i.updated_at DESC").fetchall()]
    days_out = [{"date": d, "entries": sorted(
        by_day[d], key=lambda e: (e["time_utc"], e["channel"]))}
        for d in sorted(by_day)]
    return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": days_out, "channels": channels_out,
            "unbound": unbound}


# ------------------------------------------------------------------- CLI

def _print_plan(config_path, days):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    sched = cfg.get("schedule")
    if not isinstance(sched, dict):
        raise ScheduleError("%s: no 'schedule' block" % config_path)
    sched = dict(sched)
    sched.setdefault("channel", cfg.get("channel") or cfg.get("name"))
    entries = compute_schedule(sched, days_ahead=days)
    tz_name = sched.get("timezone", "UTC")
    print("schedule for %s (%s) — %d day(s), jitter ±%s min"
          % (cfg.get("channel") or cfg.get("name"), tz_name, days,
             sched.get("jitter_minutes", 0)))
    print("%-10s %-3s %-5s %6s  %-16s %s"
          % ("date", "wd", "slot", "jitter", "local " + tz_name, "utc"))
    for e in entries:
        print("%-10s %-3d %-5s %+6d  %-16s %s"
              % (e["date"], e["weekday"], e["slot"],
                 e["jitter_minutes_applied"], e["planned_time_local"],
                 e["planned_time_utc"]))
    if not entries:
        print("(channel paused or no slots)")
    return 0


def _selftest():
    """Offline self-check: temp DB via POSTING_DB, deterministic-jitter
    assertions. No network."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="scheduler-selftest-")
    os.environ["POSTING_DB"] = os.path.join(tmp, "test.db")

    sched = {"timezone": "America/Los_Angeles",
             "slots": ["11:00", "19:00"], "jitter_minutes": 45,
             "days": [0, 1, 2, 3, 4, 5, 6], "max_per_day": 2,
             "channel": "@selftest"}
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

    # deterministic: same inputs -> identical plan, twice
    a = compute_schedule(sched, days_ahead=7, now=now)
    b = compute_schedule(sched, days_ahead=7, now=now)
    assert a == b and len(a) == 14, len(a)
    # different dates -> jitter varies (not a constant offset)
    jitters = {e["jitter_minutes_applied"] for e in a}
    assert len(jitters) > 1, jitters
    # always within ±jitter of the base slot
    for e in a:
        assert abs(e["jitter_minutes_applied"]) <= 45, e
        base = datetime.strptime(e["date"] + " " + e["slot"],
                                 "%Y-%m-%d %H:%M")
        local = datetime.strptime(e["planned_time_local"], "%Y-%m-%d %H:%M")
        delta = (local - base).total_seconds() / 60
        assert delta == e["jitter_minutes_applied"], e
    # different channel identity -> different plan
    other = compute_schedule(dict(sched, channel="@other"), 7, now)
    assert [e["planned_time_utc"] for e in other] != \
           [e["planned_time_utc"] for e in a]
    # paused channel -> empty plan
    assert compute_schedule(dict(sched, days=[]), 7, now) == []
    # max_per_day caps the slots per day
    capped = compute_schedule(dict(sched, max_per_day=1), 7, now)
    assert all(e["slot_index"] == 0 for e in capped) and len(capped) == 7

    conn = _connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_alias TEXT NOT NULL, platform TEXT NOT NULL,
        handle TEXT, token_path TEXT, daily_limit INTEGER,
        publish_times_utc TEXT, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, channel_id INTEGER, title TEXT,
        local_path TEXT, state TEXT, source_ref TEXT, scheduled_at TEXT,
        published_url TEXT, error TEXT, meta TEXT,
        created_at TEXT, updated_at TEXT);
    """)
    conn.execute(
        "INSERT INTO channels (project_alias, platform, handle, "
        "daily_limit) VALUES ('selftest', 'youtube', '@selftest', 2)")
    conn.commit()

    # engine project config in a temp projects dir -> schedule_for picks it up
    global PROJECTS_DIR
    orig_projects = PROJECTS_DIR
    PROJECTS_DIR = Path(tmp) / "projects"
    PROJECTS_DIR.mkdir()
    with open(PROJECTS_DIR / "selftest.config.json", "w",
              encoding="utf-8") as f:
        json.dump({"name": "selftest", "channel": "@selftest",
                   "schedule": sched}, f)

    # bind two items: nearest free slot each, second lands on a later slot
    r1 = bind_job("selftest", "@selftest", "job-a", conn=conn,
                  schedule=sched, now=now)
    assert r1 and r1["scheduled_at"], r1
    first_slot = r1["scheduled_at"]
    r2 = bind_job("selftest", "@selftest", "job-b", conn=conn,
                  schedule=sched, now=now)
    assert r2["scheduled_at"] != first_slot, (r1, r2)
    # idempotent: rebinding keeps the original slot
    r1b = bind_job("selftest", "@selftest", "job-a", conn=conn,
                   schedule=sched, now=now)
    assert r1b["scheduled_at"] == first_slot
    # bound items start as approved with scheduled_at written
    row = conn.execute("SELECT state, scheduled_at FROM items "
                       "WHERE id = 'job-a'").fetchone()
    assert row[0] == "approved" and row[1] == first_slot

    # due_jobs: inside ±30 min -> hit; outside -> miss
    due = due_jobs(conn=conn, now=_parse(first_slot) + timedelta(minutes=10))
    assert any(r["id"] == "job-a" for r in due), due
    assert not due_jobs(conn=conn,
                        now=_parse(first_slot) + timedelta(minutes=31))
    assert is_due("selftest", "@selftest", "job-a", conn=conn,
                  now=_parse(first_slot)) is True
    assert is_due("selftest", "@selftest", "job-a", conn=conn, now=now) is False

    # hub feed: slot with a bound item is 'scheduled', free slot is 'free'
    feed = hub_schedule(conn, days=3, now=now)
    flat = [e for d in feed["days"] for e in d["entries"]]
    assert any(e["status"] == "scheduled" for e in flat), flat
    assert any(e["status"] == "free" for e in flat), flat
    # window status flips to publish_window around the bound time
    feed2 = hub_schedule(conn, days=3,
                         now=_parse(first_slot) + timedelta(minutes=5))
    flat2 = [e for d in feed2["days"] for e in d["entries"]]
    assert any(e["status"] == "publish_window" for e in flat2), flat2

    # fallback schedule from channels.publish_times_utc (no engine config)
    conn.execute(
        "INSERT INTO channels (project_alias, platform, handle, "
        "daily_limit, publish_times_utc) VALUES "
        "('legacy', 'youtube', '@legacy', 3, '[\"13:00\", \"17:00\"]')")
    conn.commit()
    fb = schedule_for("legacy", "@legacy", conn)
    assert fb["timezone"] == "UTC" and fb["slots"] == ["13:00", "17:00"], fb
    assert fb["max_per_day"] == 3

    # published_today counts only today's published items (channel tz)
    conn.execute(
        "UPDATE items SET state = 'published', updated_at = ? "
        "WHERE id = 'job-a'", (_now(),))
    conn.commit()
    assert published_today("selftest", "@selftest", conn=conn,
                           tz_name="America/Los_Angeles") == 1
    assert published_today("selftest", "@selftest", conn=conn,
                           tz_name="America/Los_Angeles",
                           now=datetime(2026, 1, 1,
                                        tzinfo=timezone.utc)) == 0

    conn.close()
    PROJECTS_DIR = orig_projects
    print("scheduler.py selftest OK")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return _selftest()
    if "--plan" in args:
        days = 7
        if "--days" in args:
            days = int(args[args.index("--days") + 1])
        try:
            return _print_plan(args[args.index("--plan") + 1], days)
        except (ScheduleError, OSError, ValueError) as e:
            print("plan error: %s" % e, file=sys.stderr)
            return 2
    if "--bind" in args:
        i = args.index("--bind")
        res = bind_job(args[i + 1], args[i + 2], args[i + 3])
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res else 1
    if "--due" in args:
        print(json.dumps(due_jobs(), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
