"""Adapter registry for the shared approval service.

Each adapter knows how to apply an approval decision (approve / reject /
regenerate) to one project's job storage.

Adapter interface
-----------------
An adapter is any object with:

    def apply(self, job_id: str, action: str) -> tuple[int, str]

where ``action`` is one of ``"a"`` (approve), ``"r"`` (reject),
``"g"`` (regenerate). Returns ``(http_code, human_message)`` — 200 on
success, 404 for an unknown job, 400 for a malformed job id.

To plug in a new pipeline:
  1. subclass BaseAdapter (or just provide ``apply``),
  2. register it: ``register("my-project", MyAdapter())``,
  3. make sure the project exists in posting.db projects table.

Projects without a native approval state in their storage (event-hero city
channels, chess, dressit-shorts) fall back to HubItemAdapter, which flips
the item's state in the posting hub DB only. The pipeline itself decides
what to do with a hub item that moved to 'approved'/'rejected'/'new'
(typically: its sync/publish loop picks it up).
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "/root/posting/data/posting.db"
FSHORTS_JOBS_DIR = os.environ.get("FSHORTS_JOBS_DIR", "/root/fshorts/jobs")

ACTIONS = {"a": "approved", "r": "rejected", "g": "new"}
ACTION_NAMES = {"a": "approve", "r": "reject", "g": "regenerate"}

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")  # path traversal guard


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> str:
    return os.environ.get("POSTING_DB", DEFAULT_DB_PATH)


def valid_job_id(job_id: str) -> bool:
    return bool(job_id) and bool(JOB_ID_RE.match(job_id))


# --------------------------------------------------------------- hub helpers

def _connect():
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def project_exists(project: str) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM projects WHERE alias = ?", (project,)
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _find_item(conn, project: str, job_id: str):
    """Locate the hub item for (project, job_id).

    Item id conventions differ per pipeline (fshorts-<id>, dressit-sf-<id>,
    chess-<id>, dshorts-<n>), so match by exact id, '<anything>-<job_id>'
    suffix, or source_ref — preferring items whose channel belongs to the
    project.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.state, c.project_alias
        FROM items i LEFT JOIN channels c ON c.id = i.channel_id
        WHERE i.id = ? OR i.source_ref = ? OR i.id LIKE ?
        """,
        (job_id, job_id, f"%-{job_id}"),
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        if row["project_alias"] == project:
            return row
    return rows[0]


def update_hub_item(project: str, job_id: str, new_state: str) -> bool:
    """Flip the hub item state. Returns True when an item was updated."""
    try:
        with _connect() as conn:
            row = _find_item(conn, project, job_id)
            if row is None:
                return False
            conn.execute(
                "UPDATE items SET state = ?, updated_at = ? WHERE id = ?",
                (new_state, utcnow(), row["id"]),
            )
            conn.commit()
            return True
    except sqlite3.Error as exc:
        print(f"[approval hub] item update failed for {project}/{job_id}: {exc}",
              file=sys.stderr, flush=True)
        return False


def log_run(project: str, job_id: str, action: str, status: str,
            summary: str) -> None:
    """Append an audit row to the hub runs table."""
    try:
        now = utcnow()
        with _connect() as conn:
            conn.execute(
                """INSERT INTO runs (project_alias, pipeline, started_at,
                                     finished_at, status, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project, "approval", now, now, status, summary, now),
            )
            conn.commit()
    except sqlite3.Error as exc:
        print(f"[approval hub] run log failed for {project}/{job_id}: {exc}",
              file=sys.stderr, flush=True)


# ------------------------------------------------------------------ adapters

class BaseAdapter:
    """Interface + hub wiring shared by all adapters."""

    project: str = ""

    def apply(self, job_id: str, action: str) -> tuple[int, str]:
        raise NotImplementedError

    def finalize(self, job_id: str, action: str, code: int, msg: str):
        """Hub wiring after a decision: item state + runs audit row."""
        state = ACTIONS[action]
        item_updated = update_hub_item(self.project, job_id, state)
        log_run(self.project, job_id, action,
                "ok" if code == 200 else "error",
                f"{ACTION_NAMES[action]} {job_id}: {msg}"
                + ("" if item_updated else " (no hub item matched)"))


class FShortsAdapter(BaseAdapter):
    """fshorts jobs: /root/fshorts/jobs/<id>/job.json state machine.

    Mirrors the original /root/fshorts/approval_server.py logic:
      approve -> state=approved
      reject  -> state=rejected
      regen   -> state=new, script/assets/render cleared,
                 telegram.message_id reset
    """

    def __init__(self, project: str = "dressit-fshorts",
                 jobs_dir: str | None = None):
        self.project = project
        self.jobs_dir = Path(jobs_dir or
                             os.environ.get("FSHORTS_JOBS_DIR", FSHORTS_JOBS_DIR))

    def apply(self, job_id: str, action: str) -> tuple[int, str]:
        if not valid_job_id(job_id):
            return 400, "bad job id"
        job_json = self.jobs_dir / job_id / "job.json"
        if not job_json.is_file():
            return 404, f"unknown job {job_id}"
        job = json.loads(job_json.read_text())
        new_state = ACTIONS[action]
        tg = job.setdefault("telegram", {"message_id": None, "approved_by": None})
        tg["approved_by"] = "url"
        if action == "g":
            for key in ("script", "assets", "render"):
                job.pop(key, None)
            tg["message_id"] = None
        job["state"] = new_state
        tmp = job_json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False))
        tmp.replace(job_json)
        return 200, f"job {job_id} -> {new_state}"


class HubItemAdapter(BaseAdapter):
    """Fallback for projects without native approval storage: flips the
    item state in posting.db only (event-hero cities, chess, dressit-shorts).

    The project's own pipeline loop is expected to watch hub item states and
    act (publish approved, re-render new)."""

    def __init__(self, project: str):
        self.project = project

    def apply(self, job_id: str, action: str) -> tuple[int, str]:
        if not valid_job_id(job_id):
            return 400, "bad job id"
        with _connect() as conn:
            row = _find_item(conn, self.project, job_id)
        if row is None:
            return 404, f"unknown item {job_id} for project {self.project}"
        new_state = ACTIONS[action]
        if not update_hub_item(self.project, job_id, new_state):
            return 500, "hub update failed"
        return 200, f"item {row['id']} -> {new_state} (hub)"


# ------------------------------------------------------------------ registry

LEGACY_PROJECT = "dressit-fshorts"  # project for legacy /a/<job>/<sig> links

_REGISTRY: dict[str, BaseAdapter] = {}


def register(project: str, adapter: BaseAdapter) -> None:
    adapter.project = project
    _REGISTRY[project] = adapter


def get_adapter(project: str) -> BaseAdapter | None:
    """Resolve an adapter for a project alias.

    Explicitly registered adapters win; any other project present in
    posting.db gets the hub-item fallback.
    """
    adapter = _REGISTRY.get(project)
    if adapter is not None:
        return adapter
    if project_exists(project):
        return HubItemAdapter(project)
    return None


register(LEGACY_PROJECT, FShortsAdapter(LEGACY_PROJECT))
