#!/usr/bin/env python3
"""runner.py — declarative pipeline engine (proto fshorts#5).

One shared runner for every video project: the pipeline is DATA
(specs/<name>.pipeline.json per content type), the channel is DATA
(projects/<name>.config.json), only genuinely new step kinds need code.

Job state machine mirrors fshorts' job.json shape (id/state/created_at/
topic/errors) so existing dashboards can reuse their logic. Jobs live in
their own root (default /root/posting/engine/jobs/<project>/), never in
/root/fshorts/jobs — the engine does NOT drive production fshorts jobs.

CLI:
    python3 runner.py --project <config.json|name> --tick
    python3 runner.py --project X --new <topic.json>
    python3 runner.py --project X --stats
    python3 runner.py --selftest          # offline, no network/hub

Stage execution: the stage's step name resolves to steps/<step>.json —
  type "subprocess": run command (fshorts contract: module --job <id>);
                     on exit 0, if the module did not flip the state itself,
                     the runner sets state = stage.to.
  type "stub":       no-op success; runner flips state (unwired steps).
  type "manual":     never auto-advanced (human gates like review->approved).
After a successful step an optional judge gate (stage.judge) runs via
judge.py; fail -> state "review" with the verdict recorded.
"""
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import judge as judge_mod
try:
    import scheduler as scheduler_mod
except Exception:  # scheduler is additive; the runner must work without it
    scheduler_mod = None

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "runner.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("runner")
if not log.handlers:
    log.addHandler(logging.StreamHandler())

PUBLISH_WINDOW_MINUTES = 30
HUB_DB_DEFAULT = "/root/posting/data/posting.db"
HUB_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_alias TEXT,
    pipeline TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    summary TEXT,
    log TEXT,
    created_at TEXT
);
"""


class ConfigError(Exception):
    pass


# ------------------------------------------------------------ config loading

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_project(ref):
    """Load a project config by path or by name from projects/."""
    path = Path(ref)
    if not path.is_file():
        path = ROOT / "projects" / f"{ref}.config.json"
    if not path.is_file():
        raise ConfigError(f"project config not found: {ref}")
    cfg = load_json(path)
    # hard rule: a project config is variables-only, never pipeline structure
    for banned in ("stages", "steps"):
        if banned in cfg:
            raise ConfigError(
                f"project config {path.name} must not define '{banned}' — "
                "pipeline structure lives in the spec, projects only carry variables")
    if not cfg.get("name"):
        raise ConfigError(f"project config {path.name} has no 'name'")
    cfg["_path"] = str(path)
    return cfg


def load_spec(project):
    """Resolve the pipeline spec referenced by the project config."""
    ref = project.get("pipeline")
    if not isinstance(ref, dict):
        raise ConfigError(f"project {project['name']}: 'pipeline' must be an object")
    if ref.get("path"):
        spec = load_json(ref["path"])
    else:
        spec_path = ROOT / "specs" / f"{ref.get('ref')}.pipeline.json"
        if not spec_path.is_file():
            raise ConfigError(f"pipeline spec not found: {spec_path}")
        spec = load_json(spec_path)
        if ref.get("ref") and spec.get("name") != ref["ref"]:
            raise ConfigError(
                f"spec name {spec.get('name')!r} != pipeline ref {ref['ref']!r}")
        if ref.get("version") and spec.get("version") != ref["version"]:
            raise ConfigError(
                f"spec version {spec.get('version')!r} != required {ref['version']!r}")
    if not isinstance(spec.get("stages"), list) or not spec["stages"]:
        raise ConfigError("pipeline spec has no stages")
    spec["_dir"] = str(Path(ref.get("path") or
                              ROOT / "specs" / f"{ref.get('ref')}.pipeline.json").parent)
    return spec


def make_ctx(project_ref):
    project = load_project(project_ref)
    spec = load_spec(project)
    jobs_root = Path(project.get("jobs_root") or ROOT / "jobs" / project["name"])
    if not jobs_root.is_absolute():
        jobs_root = ROOT / jobs_root
    jobs_root.mkdir(parents=True, exist_ok=True)
    return {"project": project, "spec": spec, "jobs_root": jobs_root}


def load_step_manifest(name):
    path = ROOT / "steps" / f"{name}.json"
    if not path.is_file():
        raise ConfigError(f"unknown step {name!r}: no steps/{name}.json manifest")
    return load_json(path)


# ---------------------------------------------------------------- job files

def load_job(job_dir):
    with open(Path(job_dir) / "job.json", encoding="utf-8") as f:
        return json.load(f)


def save_job(job_dir, job):
    with open(Path(job_dir) / "job.json", "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)


def append_error(job_dir, job, message):
    job.setdefault("errors", []).append(
        {"at": datetime.now(timezone.utc).isoformat(), "error": message})
    save_job(job_dir, job)


def _iter_job_dirs(root):
    root = Path(root)
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "job.json").is_file():
            yield d


def new_job(ctx, topic_path):
    topic = load_json(topic_path)
    if not isinstance(topic, dict) or not topic:
        raise ConfigError("topic must be a non-empty JSON object")
    now = datetime.now(timezone.utc)
    job_id = now.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:4]
    job = {
        "id": job_id,
        "state": "new",
        "created_at": now.isoformat(),
        "project": ctx["project"]["name"],
        "pipeline": ctx["spec"]["name"] + "@" + ctx["spec"].get("version", "?"),
        "topic": topic,
        "vars": ctx["project"].get("variables", {}),
        "errors": [],
        "judges": [],
    }
    job_dir = ctx["jobs_root"] / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    save_job(job_dir, job)
    log.info("created job %s (project %s)", job_id, ctx["project"]["name"])
    print(job_id)
    return job_dir


# -------------------------------------------------------------- daily limits

def _today_bounds():
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0), now


def generated_today(root):
    start, _ = _today_bounds()
    n = 0
    for job_dir in _iter_job_dirs(root) or []:
        try:
            if datetime.fromisoformat(load_job(job_dir)["created_at"]) >= start:
                n += 1
        except Exception:
            continue
    return n


def published_today(root):
    start, _ = _today_bounds()
    n = 0
    for job_dir in _iter_job_dirs(root) or []:
        try:
            job = load_job(job_dir)
            if job["state"] == "published":
                at = (job.get("youtube") or {}).get("publish_at")
                if at and datetime.fromisoformat(at) >= start:
                    n += 1
        except Exception:
            continue
    return n


def in_publish_window(slots, now=None):
    now = now or datetime.now(timezone.utc)
    for t in slots:
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except Exception:
            continue
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now < slot + timedelta(minutes=PUBLISH_WINDOW_MINUTES):
            return True
    return False


# ------------------------------------------------------- scheduled publish
# fshorts#4: when the project config carries a "schedule" block, publishing
# is driven by per-item binding (scheduler.py): an approved job is bound to
# the nearest free computed slot (items.scheduled_at in posting.db) and the
# publish stage fires only inside the ±30 min window of that slot, capped
# by schedule.max_per_day. Without "schedule" the legacy static
# schedule_slots_utc window applies. All hub access is best-effort.

def _schedule_channel(ctx):
    return ctx["project"].get("channel") or ctx["project"]["name"]


def _publish_gate_open(ctx, job):
    sched = ctx["project"].get("schedule")
    if not sched or scheduler_mod is None:
        return in_publish_window(ctx["project"].get("schedule_slots_utc", []))
    project, channel = ctx["project"]["name"], _schedule_channel(ctx)
    try:
        if not scheduler_mod.is_due(project, channel, job["id"]):
            return False
        max_per_day = int(sched.get("max_per_day", 2))
        published = scheduler_mod.published_today(
            project, channel, tz_name=sched.get("timezone", "UTC"))
        if published >= max_per_day:
            log.info("job %s: max_per_day %d reached (%d published), skipping",
                     job["id"], max_per_day, published)
            return False
        return True
    except Exception as e:  # hub outage must never crash the tick
        log.warning("job %s: schedule gate error (closed): %s", job["id"], e)
        return False


def _bind_approved_jobs(ctx):
    """Bind every approved job to its nearest free slot (best-effort)."""
    if scheduler_mod is None or not ctx["project"].get("schedule"):
        return
    project, channel = ctx["project"]["name"], _schedule_channel(ctx)
    for job_dir in _iter_job_dirs(ctx["jobs_root"]) or []:
        try:
            job = load_job(job_dir)
            if job.get("state") != "approved":
                continue
            res = scheduler_mod.bind_job(project, channel, job["id"])
            if res and res.get("slot"):
                log.info("job %s bound to slot %s",
                         job["id"], res["scheduled_at"])
        except Exception as e:
            log.warning("bind_job for %s failed (ignored): %s", job_dir, e)


# ------------------------------------------------------------ stage execution

def _fmt(value, job, ctx):
    return str(value).format(
        job_id=job["id"], job_dir=str(ctx["jobs_root"] / job["id"]),
        jobs_root=str(ctx["jobs_root"]), engine_root=str(ROOT))


def run_step(ctx, stage, job):
    """Execute the stage's step. Returns (ok, error_message)."""
    manifest = load_step_manifest(stage["step"])
    stype = manifest.get("type", "subprocess")
    if stype == "manual":
        return None  # sentinel: never auto-advanced
    if stype == "stub" or manifest.get("stub"):
        log.info("job %s: step %s is stubbed (%s), no-op success",
                 job["id"], stage["step"], manifest.get("note", "not wired"))
        return True, None
    if stype != "subprocess":
        return False, f"step {stage['step']}: unknown type {stype!r}"
    if not manifest.get("command"):
        return False, f"step {stage['step']}: subprocess manifest has no command"
    job_dir = ctx["jobs_root"] / job["id"]
    cmd = [_fmt(c, job, ctx) for c in manifest["command"]]
    env = dict(os.environ)
    env.update({
        "ENGINE_JOB_ID": job["id"],
        "ENGINE_JOB_DIR": str(job_dir),
        "ENGINE_JOBS_ROOT": str(ctx["jobs_root"]),
        "ENGINE_PROJECT": ctx["project"]["name"],
        "ENGINE_VARS": json.dumps(ctx["project"].get("variables", {}),
                                  ensure_ascii=False),
    })
    for k, v in (manifest.get("env") or {}).items():
        env[k] = _fmt(v, job, ctx)
    cwd = _fmt(manifest.get("cwd", str(ROOT)), job, ctx)
    log.info("job %s: run %s (cwd=%s)", job["id"], cmd, cwd)
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=int(manifest.get("timeout", 600)))
    except Exception as e:
        return False, f"{stage['step']} crashed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (f"{stage['step']} exited {proc.returncode}: "
                       f"{tail[-1] if tail else 'no output'}")
    return True, None


def run_judge_gate(ctx, stage, job, job_dir):
    """Run the stage's judge gate. Returns 'pass' | 'fail' | 'error'."""
    jcfg = stage.get("judge")
    if not jcfg:
        return "pass"
    if jcfg.get("mode") == "stub":
        result = {"at": datetime.now(timezone.utc).isoformat(),
                  "stub": True, "pass": True,
                  "note": jcfg.get("note", "judge not wired yet"),
                  "model": jcfg.get("model")}
        with open(job_dir / "judge_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        job.setdefault("judges", []).append({"stage": stage["name"],
                                             "stub": True, "pass": True})
        save_job(job_dir, job)
        log.info("job %s: judge gate %s stubbed, auto-pass", job["id"], stage["name"])
        return "pass"
    attempts = 1 + int(jcfg.get("retries", 0))
    last_err = None
    for attempt in range(attempts):
        try:
            result = judge_mod.run_judge(job_dir, jcfg,
                                         spec_dir=ctx["spec"].get("_dir"))
            break
        except judge_mod.JudgeError as e:
            last_err = e
            log.warning("job %s: judge attempt %d/%d failed: %s",
                        job["id"], attempt + 1, attempts, e)
    else:
        append_error(job_dir, job, f"judge gate {stage['name']} error: {last_err}")
        return "error"
    job.setdefault("judges", []).append({
        "stage": stage["name"], "model": result.get("model"),
        "score": result.get("score"), "pass": result["pass"],
        "violations": result.get("violations", []),
    })
    save_job(job_dir, job)
    log.info("job %s: judge gate %s score=%s pass=%s",
             job["id"], stage["name"], result.get("score"), result["pass"])
    return "pass" if result["pass"] else "fail"


def advance_job(ctx, job_dir):
    """Advance one job by one stage if eligible. Returns True on progress."""
    job_dir = Path(job_dir)
    job = load_job(job_dir)
    state = job.get("state")
    stage = next((s for s in ctx["spec"]["stages"] if s.get("from") == state), None)
    if not stage:
        log.info("job %s: state %r is terminal/manual, skipping", job.get("id"), state)
        return False

    limits = ctx["project"].get("daily_limits", {})
    if stage.get("daily_limit") == "generate" and \
            generated_today(ctx["jobs_root"]) >= int(limits.get("generate", 10)):
        log.info("job %s: daily generate limit reached, skipping", job["id"])
        return False
    if stage.get("daily_limit") == "publish":
        if published_today(ctx["jobs_root"]) >= int(limits.get("publish", 2)):
            log.info("job %s: daily publish limit reached, skipping", job["id"])
            return False
    if stage.get("publish_window") and not _publish_gate_open(ctx, job):
        log.info("job %s: outside publish window, skipping", job["id"])
        return False

    step_res = run_step(ctx, stage, job)
    if step_res is None:  # manual step
        log.info("job %s: stage %s is manual, skipping", job["id"], stage["name"])
        return False
    ok, err = step_res
    if not ok:
        append_error(job_dir, job, err)
        log.error("job %s: %s", job["id"], err)
        if stage.get("on_fail", "retry") == "hold":
            job = load_job(job_dir)
            job["state"] = "review"
            save_job(job_dir, job)
            return True
        return False

    # step succeeded: flip the state if the module did not do it itself
    job = load_job(job_dir)
    if job.get("state") == stage["from"]:
        job["state"] = stage["to"]
        save_job(job_dir, job)

    # judge gate sits between stages: fail blocks the pipeline at 'review'
    gate = run_judge_gate(ctx, stage, job, job_dir)
    if gate == "fail":
        job = load_job(job_dir)
        job["state"] = "review"
        save_job(job_dir, job)
        log.info("job %s: judge gate failed -> review", job["id"])
    elif gate == "error":
        job = load_job(job_dir)
        job["state"] = stage["from"]  # retry the whole stage next tick
        save_job(job_dir, job)
        return False
    return True


# ------------------------------------------------------------------ hub runs
# Posting-hub instrumentation, mirroring fshorts/pipeline.py: every tick is
# one row in the hub's `runs` table. Best-effort: a hub outage never breaks
# a tick. POSTING_DB env var overrides the DB path (used by selftest).

def _hub_db_path():
    return os.environ.get("POSTING_DB", HUB_DB_DEFAULT)


def _hub_run_start(ctx):
    try:
        import sqlite3
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(_hub_db_path())
        try:
            conn.execute(HUB_RUNS_SCHEMA)
            cur = conn.execute(
                "INSERT INTO runs(project_alias, pipeline, started_at,"
                " status, created_at) VALUES (?,?,?,?,?)",
                (ctx["project"]["name"],
                 f"engine-tick:{ctx['spec']['name']}", now, "running", now))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        log.warning("hub run-start failed (ignored): %s", e)
        return None


def _hub_run_finish(run_id, status, summary, log_tail):
    if run_id is None:
        return
    try:
        import sqlite3
        conn = sqlite3.connect(_hub_db_path())
        try:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, summary=?, log=?"
                " WHERE id=?",
                (status, datetime.now(timezone.utc).isoformat(), summary,
                 log_tail, run_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("hub run-finish failed (ignored): %s", e)


def _tick_log_tail(limit=4000):
    try:
        p = LOG_DIR / "runner.log"
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit * 2))
            return f.read().decode("utf-8", "replace")[-limit:]
    except Exception:
        return ""


# -------------------------------------------------------------------- ticks

def tick(ctx):
    run_id = _hub_run_start(ctx)
    transitions = {}
    n = 0
    status = "ok"
    try:
        for job_dir in _iter_job_dirs(ctx["jobs_root"]) or []:
            try:
                before = load_job(job_dir).get("state")
                if advance_job(ctx, job_dir):
                    n += 1
                    after = load_job(job_dir).get("state")
                    key = f"{before}->{after}"
                    transitions[key] = transitions.get(key, 0) + 1
            except Exception as e:  # one bad job must never kill the tick
                log.exception("job dir %s failed: %s", job_dir, e)
        _bind_approved_jobs(ctx)
        log.info("tick done: %d job(s) advanced", n)
    except Exception as e:
        status = "error"
        log.exception("tick failed: %s", e)
    summary = json.dumps({"advanced": n, "transitions": transitions})
    _hub_run_finish(run_id, status, summary, _tick_log_tail())
    print(f"tick: advanced={n} {transitions}")
    return n


def stats(ctx):
    by_state = {}
    for job_dir in _iter_job_dirs(ctx["jobs_root"]) or []:
        try:
            state = load_job(job_dir).get("state", "unknown")
        except Exception:
            state = "corrupt"
        by_state[state] = by_state.get(state, 0) + 1
    limits = ctx["project"].get("daily_limits", {})
    return {
        "project": ctx["project"]["name"],
        "pipeline": ctx["spec"]["name"] + "@" + ctx["spec"].get("version", "?"),
        "jobs_root": str(ctx["jobs_root"]),
        "jobs_by_state": by_state,
        "total": sum(by_state.values()),
        "generated_today": f"{generated_today(ctx['jobs_root'])}/{limits.get('generate', 10)}",
        "published_today": f"{published_today(ctx['jobs_root'])}/{limits.get('publish', 2)}",
    }


# ------------------------------------------------------------------ selftest

def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        spec = {
            "name": "selftest", "version": "0.0.1",
            "stages": [
                {"name": "make", "from": "new", "to": "made", "step": "stub",
                 "daily_limit": "generate"},
                {"name": "check", "from": "made", "to": "checked", "step": "stub",
                 "judge": {"mode": "stub", "model": "selftest",
                           "note": "offline selftest"}},
                {"name": "ship", "from": "checked", "to": "published",
                 "step": "stub", "daily_limit": "publish",
                 "publish_window": True},
            ],
        }
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        project = {
            "name": "selftest",
            "pipeline": {"path": str(spec_path)},
            "jobs_root": str(tmp / "jobs"),
            "variables": {"foo": "bar"},
            "schedule_slots_utc": [],  # empty -> never inside a publish window
            "daily_limits": {"generate": 10, "publish": 2},
        }
        project_path = tmp / "project.json"
        project_path.write_text(json.dumps(project), encoding="utf-8")

        orig_db = os.environ.get("POSTING_DB")
        os.environ["POSTING_DB"] = str(tmp / "hub.db")
        try:
            ctx = make_ctx(str(project_path))

            # project configs may never carry pipeline structure
            bad = dict(project, stages=[])
            bad_path = tmp / "bad.json"
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            try:
                load_project(str(bad_path))
                raise AssertionError("stages in project config must be rejected")
            except ConfigError:
                pass

            topic_path = tmp / "topic.json"
            topic_path.write_text(json.dumps({"title": "t"}), encoding="utf-8")
            d1 = new_job(ctx, topic_path)
            d2 = new_job(ctx, topic_path)
            assert load_job(d1)["state"] == "new"

            assert tick(ctx) == 2  # new->made for both
            assert load_job(d1)["state"] == "made"

            assert tick(ctx) == 2  # made->checked, stub judge gate auto-passes
            job = load_job(d1)
            assert job["state"] == "checked", job
            assert job["judges"][-1]["stub"] is True
            jr = load_json(Path(d1) / "judge_result.json")
            assert jr["stub"] is True and jr["pass"] is True

            # publish gate: no slots -> never inside a window -> no advance
            n = tick(ctx)
            assert load_job(d1)["state"] == "checked"
            log.info("selftest publish-window tick advanced %d (expected 0)", n)
            assert n == 0

            # hub logging: three ticks -> three closed runs rows
            import sqlite3
            conn = sqlite3.connect(os.environ["POSTING_DB"])
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
            conn.close()
            assert len(rows) == 3, rows
            last = rows[-1]
            assert last["project_alias"] == "selftest"
            assert last["pipeline"] == "engine-tick:selftest"
            assert last["status"] == "ok" and last["finished_at"]
            summary = json.loads(rows[1]["summary"])
            assert summary["transitions"] == {"made->checked": 2}, summary

            rep = stats(ctx)
            assert rep["jobs_by_state"] == {"checked": 2} and rep["total"] == 2
            assert rep["generated_today"].split("/")[0] == "2"

            # publish window logic
            assert in_publish_window(["14:00"], datetime(2026, 9, 1, 14, 5,
                                          tzinfo=timezone.utc)) is True
            assert in_publish_window(["14:00"], datetime(2026, 9, 1, 15, 0,
                                          tzinfo=timezone.utc)) is False
            assert d2.name != d1.name
        finally:
            if orig_db is None:
                os.environ.pop("POSTING_DB", None)
            else:
                os.environ["POSTING_DB"] = orig_db

    print("runner.py selftest OK")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if "--project" not in args:
        print(__doc__)
        return 1
    ref = args[args.index("--project") + 1]
    try:
        ctx = make_ctx(ref)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if "--tick" in args:
        tick(ctx)
        return 0
    if "--new" in args:
        new_job(ctx, args[args.index("--new") + 1])
        return 0
    if "--stats" in args:
        print(json.dumps(stats(ctx), indent=2, ensure_ascii=False))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
