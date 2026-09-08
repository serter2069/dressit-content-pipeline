#!/usr/bin/env python3
"""posting hub — HTTP server.

Serves a JSON API under /api/* and a single-page dashboard at /.
Stdlib only (http.server, json, urllib, subprocess). Basic auth is
handled by nginx in front; this server binds 127.0.0.1:8478 only.

Endpoints:
  GET  /api/overview                  per-project counts by state
  GET  /api/projects                  list projects
  GET  /api/channels?project=ALIAS    list channels (project optional)
  GET  /api/items?project=&state=&limit=
  GET  /api/runs?project=&limit=      pipeline run log, newest first
  GET  /api/ledger?project=&status=   publish dedup ledger, newest first (cap 200)
  GET  /api/schedule?days=7&project=  forward publish schedule (project optional)
  GET  /api/analytics                 channel/video stats overview (analytics.py;
                                      503 when the module is unavailable)
  POST /api/analytics/refresh         re-pull stats from YouTube, return overview
  GET  /p/<alias>                     dashboard pre-filtered to one project
                                      (fshorts#6; unknown alias -> 404 page
                                      listing valid project URLs)
  GET  /media/<item_id>               stream an item's video (Range OK)
  POST /api/sync                      run adapters/sync_all.py once
  POST /api/runs                      record a pipeline run
  POST /api/delegate                  {"project_alias","title","notes"}
                                      -> adapter enqueue() when defined
  POST /api/items/<id>/transition     {"to": "...", ...optional fields}
  POST /api/channels                  register/update a channel

Run `python3 server.py --selftest` for an offline self-check (temp DB,
ephemeral port, no network beyond loopback).
"""

import html
import importlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import ledger  # noqa: E402

# fshorts#10: YouTube stats overview (analytics.py). Optional — the hub
# must keep serving (503 on /api/analytics*) if the module is absent.
try:
    import analytics as analytics_mod  # noqa: E402
except Exception:
    analytics_mod = None

# fshorts#4: forward schedule (engine/scheduler.py). Optional — the hub
# must keep serving even if the engine tree is absent.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "engine"))
try:
    import scheduler as scheduler_mod  # noqa: E402
except Exception:
    scheduler_mod = None

HOST = "127.0.0.1"
PORT = 8478
SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "adapters", "sync_all.py")
SYNC_TIMEOUT_S = 120

# pipeline_kind (projects table) -> adapter module for /api/delegate
ADAPTER_MODULES = {
    "fshorts": "adapters.fshorts",
    "dressit-shorts": "adapters.dressit_shorts",
    "event-hero": "adapters.event_hero",
    "custom": "adapters.event_hero",
}

# "bytes=start-end", "bytes=start-", "bytes=-suffix"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def resolve_media_path(item):
    """Resolve the video file for an item from its recorded local_path."""
    local_path = (item or {}).get("local_path")
    if not local_path:
        return None
    base = os.path.realpath(local_path)
    if os.path.isfile(base):
        return base
    if os.path.isdir(base):
        cand = os.path.realpath(os.path.join(base, "final.mp4"))
        if os.path.dirname(cand) == base and os.path.isfile(cand):
            return cand
    return None


def resolve_thumb_path(item):
    """Resolve the thumbnail image file for an item.

    Order: explicit meta.thumbnail_path / meta.thumbnail (adapters may set a
    per-item image), then the conventional thumbnail.jpg next to local_path.
    """
    meta = (item or {}).get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    for key in ("thumbnail_path", "thumbnail"):
        cand = meta.get(key)
        if cand and isinstance(cand, str) and os.path.isfile(cand):
            return os.path.realpath(cand)

    local_path = (item or {}).get("local_path")
    if not local_path:
        return None
    base = os.path.realpath(local_path)
    if os.path.isdir(base):
        cand = os.path.realpath(os.path.join(base, "thumbnail.jpg"))
        if os.path.dirname(cand) == base and os.path.isfile(cand):
            return cand
    elif os.path.isfile(base):
        cand = os.path.realpath(os.path.join(os.path.dirname(base), "thumbnail.jpg"))
        if os.path.isfile(cand):
            return cand
    return None

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>posting hub</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f5f6f8; color: #1f2328; font: 14px/1.45 -apple-system,
         "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  header { padding: 6px 16px; border-bottom: 1px solid #e1e4e8;
           background: #ffffff; display: flex; align-items: center;
           gap: 12px; flex-wrap: wrap; position: sticky; top: 0; z-index: 5; }
  header .meta { color: #57606a; font-size: 12px; }
  header .spacer { margin-left: auto; }
  .tabs { display: flex; gap: 4px; }
  .tab { font-size: 13px; padding: 4px 14px; border-radius: 6px; }
  .tab.sel { border-color: #0969da; color: #0969da; background: #ddf4ff; }
  body.analytics .sidebar { display: none; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .stat { background: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px;
          box-shadow: 0 1px 3px rgba(31,35,40,.07); padding: 10px 16px;
          min-width: 130px; }
  .stat-num { font-size: 22px; font-weight: 700; color: #1f2328; }
  .stat-label { font-size: 11px; color: #57606a; text-transform: uppercase;
                letter-spacing: .03em; }
  .layout { display: flex; min-height: calc(100vh - 49px); }
  .sidebar { width: 320px; padding: 12px; }
  .main { flex: 1; padding: 16px 20px; overflow-x: auto; }
  .proj { padding: 10px 12px; border: 1px solid #e1e4e8; border-radius: 8px;
          margin-bottom: 8px; cursor: pointer; background: #ffffff;
          box-shadow: 0 1px 2px rgba(31,35,40,.06); }
  .proj:hover { border-color: #0969da; }
  .proj.sel { border-color: #0969da; box-shadow: 0 0 0 2px #ddf4ff; }
  .proj .name { color: #1f2328; font-weight: 600; }
  .proj .kind { color: #57606a; font-size: 12px; }
  .chanlinks { margin-top: 3px; font-size: 12px; }
  .chanlinks a { color: #0969da; text-decoration: none; }
  .chanlinks a:hover { text-decoration: underline; }
  .counts { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .chip, .pill { display: inline-block; padding: 1px 9px; border-radius: 10px;
                 font-size: 11px; font-weight: 600; }
  .chip b, .pill b { font-weight: 700; }
  .st-new       { background: #eaeef2; color: #57606a; }
  .st-review    { background: #fff3cd; color: #9a6700; }
  .st-approved  { background: #ddf4ff; color: #0969da; }
  .st-scheduled { background: #fbefff; color: #8250df; }
  .st-published { background: #dafbe1; color: #1a7f37; }
  .st-rejected, .st-failed { background: #ffebe9; color: #cf222e; }
  .st-archived  { background: #f6f8fa; color: #8c959f;
                  text-decoration: line-through; }
  .p-running { background: #ddf4ff; color: #0969da;
               animation: pulse 1.2s ease-in-out infinite; }
  .p-ok, .p-success { background: #dafbe1; color: #1a7f37; }
  .p-error, .p-failed { background: #ffebe9; color: #cf222e; }
  .p-idle, .p-unknown { background: #eaeef2; color: #57606a; }
  .p-planned { background: #eaeef2; color: #57606a; }
  .p-rendered { background: #fff3cd; color: #9a6700; }
  .p-approved { background: #ddf4ff; color: #0969da; }
  .p-published { background: #dafbe1; color: #1a7f37; }
  .p-rejected, .p-skipped_dup { background: #ffebe9; color: #cf222e; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
  .card { background: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px;
          box-shadow: 0 1px 3px rgba(31,35,40,.07); padding: 12px 14px;
          margin-top: 10px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid
           #eaeef2; font-size: 13px; vertical-align: top; }
  th { color: #57606a; font-weight: 600; font-size: 12px;
       text-transform: uppercase; letter-spacing: .03em; }
  h2 { font-size: 15px; color: #1f2328; margin: 4px 0 2px; }
  .chans { color: #1f2328; font-size: 13px; }
  .chan { margin-bottom: 6px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
          monospace; font-size: 11.5px; color: #57606a; }
  button { background: #ffffff; color: #1f2328; border: 1px solid #d0d7de;
           border-radius: 6px; padding: 3px 10px; font-size: 12px;
           cursor: pointer; margin-right: 4px; }
  button:hover { border-color: #8c959f; background: #f5f6f8; }
  button.ok { color: #1a7f37; border-color: #1a7f37; }
  button.bad { color: #cf222e; border-color: #cf222e; }
  a { color: #0969da; text-decoration: none; }
  .muted { color: #6e7781; }
  .err { color: #cf222e; padding: 6px 0; font-size: 12.5px; }
  .errdet summary { color: #cf222e; cursor: pointer; font-size: 12.5px; }
  .t-bold { font-weight: 600; color: #1f2328; }
  .future { color: #8250df; font-weight: 700; }
  pre.log, pre.inline { background: #f6f8fa; border: 1px solid #e1e4e8;
        border-radius: 6px; padding: 8px 10px; margin: 6px 0;
        max-height: 280px; overflow: auto; white-space: pre-wrap;
        font-size: 12px; color: #1f2328; font-family: ui-monospace,
        SFMono-Regular, Menlo, Consolas, monospace; }
  pre.inline { display: inline-block; margin: 2px 0; max-height: 160px;
               vertical-align: top; }
  .kv { display: grid; grid-template-columns: 130px 1fr; gap: 3px 12px;
        font-size: 12.5px; margin: 4px 0; }
  video { display: block; width: 360px; max-width: 100%; margin: 4px 0;
          background: #000; border-radius: 6px; }
  .cover-thumb { width: 44px; height: 78px; object-fit: cover; border-radius: 4px;
                 border: 1px solid #d0d7de; display: inline-block; vertical-align: middle;
                 background: #eaeef2; cursor: pointer; transition: transform 0.15s ease; }
  .cover-thumb:hover { transform: scale(1.08); box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
  tr.player td { padding: 4px 10px; background: #f8fafc; }
  tr.detail td { background: #fafbfc; }
  select { background: #ffffff; color: #1f2328; border: 1px solid #d0d7de;
           border-radius: 6px; padding: 3px 6px; }
  .projhead { margin: 2px 0 6px; font-size: 13px; }
  .projhead .chanlinks { display: inline; margin: 0 0 0 8px; }
  details.spoiler { margin-top: 14px; }
  details.spoiler > summary { cursor: pointer; color: #57606a;
    font-size: 15px; font-weight: 600; list-style: none;
    padding: 2px 0; user-select: none; }
  details.spoiler > summary::-webkit-details-marker { display: none; }
  details.spoiler > summary::before { content: "▸"; display: inline-block;
    margin-right: 6px; transition: transform .12s ease; }
  details.spoiler[open] > summary::before { transform: rotate(90deg); }
  details.spoiler > summary:hover { color: #1f2328; }
  /* dressit#1965: analytics tab (overview + channel drill-down) */
  .an-head { display: flex; align-items: center; gap: 10px;
             flex-wrap: wrap; margin: 4px 0 2px; }
  .an-head h2 { margin: 0; }
  .an-seg { display: inline-flex; border: 1px solid #d0d7de;
            border-radius: 6px; overflow: hidden; }
  .an-seg button { border: 0; border-radius: 0; margin: 0;
                   padding: 3px 12px; }
  .an-seg button.sel { background: #ddf4ff; color: #0969da;
                       font-weight: 600; }
  button:disabled { opacity: .55; cursor: default; }
  .an-ava { width: 20px; height: 20px; border-radius: 50%;
            vertical-align: middle; margin-right: 8px; object-fit: cover;
            background: #eaeef2; }
  tr.an-row { cursor: pointer; }
  tr.an-row:hover td { background: #f6f8fa; }
  tr.an-total td { font-weight: 600; border-top: 2px solid #d0d7de; }
  .an-sub { font-size: 11px; margin-top: 2px; }
  .an-chart { position: relative; }
  .an-bar { fill: #0969da; }
  .an-tip { position: absolute; top: 4px; left: 0;
    transform: translateX(-50%); background: #ffffff;
    border: 1px solid #d0d7de; border-radius: 6px;
    box-shadow: 0 8px 24px rgba(140, 149, 159, 0.2); padding: 6px 8px;
    font-size: 12px; color: #1f2328; white-space: nowrap;
    pointer-events: none; z-index: 30; }
  .cost-badge { display: inline-block; padding: 1px 6px; border-radius: 6px;
                font-size: 11px; font-weight: 600; background: #eef2ff;
                color: #3730a3; border: 1px solid #c7d2fe; margin-left: 6px; }
  .cost-badge .tokens { font-weight: 400; color: #4b5563; margin-left: 3px; }
</style>
</head>
<body>
<header>
  <div class="tabs">
    <button class="tab sel" id="tab-items"
            onclick="setTab('items')">Items</button>
    <button class="tab" id="tab-runs" onclick="setTab('runs')">Runs</button>
    <button class="tab" id="tab-ledger"
            onclick="setTab('ledger')">Дедуп</button>
    <button class="tab" id="tab-schedule"
            onclick="setTab('schedule')">Расписание</button>
    <button class="tab" id="tab-analytics"
            onclick="setTab('analytics')">Analytics</button>
  </div>
  <span class="spacer"></span>
  <span class="meta" id="meta"></span>
  <select id="stateFilter">
    <option value="">all states</option>
    <option>new</option><option>review</option><option>approved</option>
    <option>rejected</option><option>scheduled</option>
    <option>published</option><option>failed</option>
    <option>archived</option>
  </select>
  <button onclick="sync()">Sync now</button>
  <button onclick="load()">Refresh</button>
</header>
<div class="layout">
  <div class="sidebar" id="projects"></div>
  <div class="main" id="main"><span class="muted">select a project</span></div>
</div>
<script>
// fshorts#6: the server replaces BOOT_PROJECT on /p/<alias> pages, so the
// dashboard boots pre-filtered to that project (null on the root page).
const BOOT_PROJECT = null;
let selected = BOOT_PROJECT;
let projects = [];
let tab = "items";
const openLogs = new Set();    // run ids with expanded log (survives refresh)
const openDetail = new Set();  // item ids with expanded detail row
const openMeta = new Set();    // item ids with meta JSON shown
const openPlay = new Set();    // item ids with video player shown
let showIdleRuns = false;      // runs tab: also show no-op ticks/syncs

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.error || ("HTTP " + r.status));
    err.status = r.status;
    err.data = j;
    throw err;
  }
  return j;
}

// ISO -> readable local time, e.g. "Sep 2, 14:30"
function fmtDT(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d.getTime())) return String(s);
  const mon = d.toLocaleString("en-US", {month: "short"});
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return mon + " " + d.getDate() + ", " + hh + ":" + mm;
}

function fmtDur(started, finished) {
  if (!started || !finished) return "";
  const ms = new Date(finished).getTime() - new Date(started).getTime();
  if (isNaN(ms) || ms < 0) return "";
  return Math.round(ms / 1000) + "s";
}

function prettyJSON(s) {
  if (s == null || s === "") return "";
  try { return JSON.stringify(JSON.parse(s), null, 2); }
  catch (e) { return String(s); }
}

function formatTokenCount(n) {
  if (typeof n !== "number" || isNaN(n)) return String(n || 0);
  if (n >= 1000) {
    const k = n / 1000;
    return (Number.isInteger(k) ? k.toString() : k.toFixed(1).replace(/\.0$/, "")) + "k";
  }
  return String(n);
}

function costBadgeHtml(metaRaw) {
  if (!metaRaw) return "";
  let meta = metaRaw;
  if (typeof meta === "string") {
    try { meta = JSON.parse(meta); }
    catch (e) { return ""; }
  }
  if (!meta || typeof meta !== "object") return "";

  const cost = typeof meta.cost_usd === "number" ? meta.cost_usd : (meta.cost_usd ? parseFloat(meta.cost_usd) : null);
  const tokens = meta.tokens && typeof meta.tokens === "object" ? meta.tokens : null;

  if (cost === null && !tokens) return "";

  const costStr = cost !== null && !isNaN(cost) ? `$${cost.toFixed(3)}` : "";

  let tokensStr = "";
  if (tokens) {
    const parts = [];
    const lowerKeys = {};
    for (const [k, v] of Object.entries(tokens)) {
      lowerKeys[k.toLowerCase()] = v;
    }
    if (lowerKeys.glm != null) parts.push(`GLM: ${formatTokenCount(lowerKeys.glm)}`);
    if (lowerKeys.audio != null) parts.push(`Audio: ${formatTokenCount(lowerKeys.audio)}`);
    if (lowerKeys.judge != null) parts.push(`Judge: ${formatTokenCount(lowerKeys.judge)}`);
    for (const [k, v] of Object.entries(tokens)) {
      if (!["glm", "audio", "judge"].includes(k.toLowerCase()) && v != null) {
        parts.push(`${k.toUpperCase()}: ${formatTokenCount(v)}`);
      }
    }
    if (parts.length > 0) {
      tokensStr = `<span class="tokens">(${esc(parts.join(", "))})</span>`;
    }
  }

  if (!costStr && !tokensStr) return "";
  return `<span class="cost-badge">${esc(costStr)}${tokensStr}</span>`;
}

function setTab(t) {
  tab = t;
  for (const id of ["items", "runs", "ledger", "schedule", "analytics"]) {
    document.getElementById("tab-" + id).className =
      "tab" + (t === id ? " sel" : "");
  }
  document.getElementById("stateFilter").style.display =
    t === "items" ? "" : "none";
  // Analytics is a global all-projects view: no project sidebar.
  document.body.classList.toggle("analytics", t === "analytics");
  renderMain();
}

async function load() {
  try {
    const [ov, runs] = await Promise.all([
      api("/api/overview"), api("/api/runs?limit=20")]);
    projects = ov.projects || [];
    if (!selected && projects.length) selected = projects[0].alias;
    updateTitle();
    renderHeader(runs.runs || []);
    renderProjects();
    if (selected || tab === "analytics") await renderMain();
  } catch (e) {
    document.getElementById("meta").textContent = "error: " + e.message;
  }
}

function renderHeader(runs) {
  const syncRun = runs.find(r => r.pipeline === "sync");
  document.getElementById("meta").textContent =
    (syncRun
      ? "last sync " + fmtDT(syncRun.started_at || syncRun.created_at) +
        " (" + (syncRun.status || "?") + ")"
      : "no sync runs yet") +
    " \u00b7 updated " + new Date().toLocaleTimeString();
}

function chanUrl(c) {
  if (c.platform === "youtube" && c.handle && c.handle.startsWith("@"))
    return "https://www.youtube.com/" + c.handle;
  return null;
}

function chanLinks(chans) {
  const links = (chans || []).map(c => {
    const u = chanUrl(c);
    if (!u) return "";
    return `<a href="${esc(u)}" target="_blank" rel="noopener"
      onclick="event.stopPropagation()">${esc(c.handle)} &#8599;</a>`;
  }).filter(Boolean).join(" &middot; ");
  return links ? `<div class="chanlinks">${links}</div>` : "";
}

function renderProjects() {
  const el = document.getElementById("projects");
  el.innerHTML = projects.map(p => {
    const chips = Object.entries(p.states || {}).map(([s, n]) =>
      `<span class="chip st-${esc(s)}">${esc(s)} <b>${n}</b></span>`).join("");
    return `<div class="proj ${p.alias === selected ? "sel" : ""}"
      onclick="select('${esc(p.alias)}')">
      <div class="name">${esc(p.name || p.alias)}</div>
      <div class="kind">${esc(p.pipeline_kind || "")} &middot; total ${p.total}</div>
      ${chanLinks(p.channels)}
      <div class="counts">${chips || '<span class="muted">no items</span>'}</div>
    </div>`;
  }).join("");
}

function projectUrl(alias) { return "/p/" + encodeURIComponent(alias); }

function updateTitle() {
  const proj = projects.find(p => p.alias === selected);
  document.title = proj
    ? (proj.name || proj.alias) + " · posting hub" : "posting hub";
}

// Small project header (name + channels) shown above project-scoped tabs.
function projHeaderHtml() {
  const proj = projects.find(p => p.alias === selected);
  if (!proj) return "";
  return `<div class="projhead"><b>${esc(proj.name || proj.alias)}</b>
    <span class="mono muted">${esc(proj.alias)}</span>
    ${chanLinks(proj.channels)}</div>`;
}

function select(alias, push) {
  selected = alias;
  updateTitle();
  renderProjects();
  renderMain();
  if (push !== false && location.pathname !== projectUrl(alias))
    history.pushState({project: alias}, "", projectUrl(alias));
}

// Back/forward: the URL carries the project, restore selection from it.
window.addEventListener("popstate", () => {
  const m = location.pathname.match(/^\\/p\\/([^/]+)/);
  const alias = m ? decodeURIComponent(m[1]) : null;
  if (alias && projects.some(p => p.alias === alias)) selected = alias;
  updateTitle();
  renderProjects();
  renderMain();
});

async function renderMain() {
  if (tab === "runs") return renderRuns();
  if (tab === "ledger") return renderLedger();
  if (tab === "schedule") return renderSchedule();
  if (tab === "analytics") return renderAnalytics();
  return renderItems();
}

// fshorts#4: one forward schedule across all projects (engine scheduler).
function schedPill(status) {
  const map = {
    "scheduled":       ["st-scheduled", "запланировано"],
    "publish_window":  ["p-running", "окно публикации"],
    "published":       ["st-published", "опубликовано"],
    "free":            ["p-idle", "свободно"],
    "approved":        ["p-approved", "ждёт слота"],
    "review":          ["st-review", "ждёт approve"],
  };
  const m = map[status] || ["p-unknown", status || ""];
  return `<span class="pill ${m[0]}">${esc(m[1])}</span>`;
}

async function renderSchedule() {
  const main = document.getElementById("main");
  try {
    const j = await api("/api/schedule?days=7" +
      (selected ? "&project=" + encodeURIComponent(selected) : ""));
    const days = (j.days || []).map(d => {
      const rows = d.entries.map(e => `<tr>
          <td class="t-bold">${esc(e.time_local)}</td>
          <td class="muted">${esc(e.timezone)}</td>
          <td>${esc(e.project)} &middot; ${esc(e.channel)}</td>
          <td>${e.item
            ? `<span class="t-bold">${esc(e.item.title || e.item.id)}</span>
               <div class="mono">${esc(e.item.id)}</div>`
            : '<span class="muted">&mdash;</span>'}</td>
          <td>${schedPill(e.status)}</td>
        </tr>`).join("");
      return `<h2>${esc(d.date)}</h2>
        <div class="card"><table><thead><tr>
          <th>время</th><th>тайзона</th><th>канал</th>
          <th>item</th><th>статус</th>
        </tr></thead><tbody>
        ${rows || '<tr><td colspan="5" class="muted">нет слотов</td></tr>'}
        </tbody></table></div>`;
    }).join("");
    const unbound = (j.unbound || []).map(u => `<tr>
        <td>${esc(u.project || "")} &middot; ${esc(u.channel || "")}</td>
        <td><span class="t-bold">${esc(u.title || u.id)}</span>
            <div class="mono">${esc(u.id)}</div></td>
        <td>${schedPill(u.state)}</td>
      </tr>`).join("");
    const unboundHtml = unbound ? `
      <h2>без слота</h2>
      <div class="card"><table><thead><tr>
        <th>канал</th><th>item</th><th>статус</th>
      </tr></thead><tbody>${unbound}</tbody></table></div>` : "";
    main.innerHTML = `
      ${projHeaderHtml()}
      <h2>расписание &middot; неделя вперёд &middot; ${esc(
        selected || "все проекты")}</h2>
      ${days || '<div class="card muted">нет активных расписаний</div>'}
      ${unboundHtml}`;
  } catch (e) {
    main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

// fshorts#10: global all-projects YouTube stats (analytics.py).
function fmtNum(n) {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a >= 1e6) return (n / 1e6).toFixed(1).replace(/\\.0$/, "") + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(1).replace(/\\.0$/, "") + "K";
  return String(Math.round(n));
}

// dressit#1957: exact value with thousands separators for tooltips
// ("1 340") — fmtNum's "1.3K" rounding hides the real number there.
function fmtExact(n) {
  n = Math.round(Number(n) || 0);
  const s = String(Math.abs(n)).replace(/\\B(?=(\\d{3})+(?!\\d))/g,
    "\\u00a0");
  return (n < 0 ? "-" : "") + s;
}

const RU_MON = ["янв", "фев", "мар", "апр", "май", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек"];
function fmtDayShort(ms) {
  const d = new Date(ms);
  return d.getUTCDate() + " " + RU_MON[d.getUTCMonth()];
}

// ===== dressit#1965: analytics tab — full rewrite ======================

// Two screens in one tab, no reload: overview (all channels) and a
// channel drill-down. All math is client-side from GET /api/analytics:
// channels[].daily = cumulative end-of-day snapshots, channels[].series =
// today's intraday snapshots.
const CHART_PALETTE = ["#0969da", "#1a7f37", "#8250df", "#cf222e",
  "#9a6700", "#0550ae", "#d4a72c", "#57606a"];
let anJ = null;      // last /api/analytics payload (instant re-renders)
let anPeriod = 7;    // 7 = «7 дней», 0 = «всё время»; survives re-render
let anChan = null;   // drilled-down channel handle; null = overview
let anBarsCur = [];  // bars of the chart on screen right now (for hover)

function anDateStr(ms) { return new Date(ms).toISOString().slice(0, 10); }
function anTodayStr() { return anDateStr(Date.now()); }

// Per-day NEW views {date: delta} for anything with daily[]/series[]
// (a channel or a video). First known day contributes 0 — its cumulative
// predates our history. Today comes from series (partial) unless daily
// already has an entry for it.
function anDayDeltas(c) {
  const ds = (c.daily || [])
    .map(d => ({date: d.date, v: Number(d.views) || 0}))
    .filter(d => d.date)
    .sort((a, b) => a.date < b.date ? -1 : 1);
  const out = {};
  ds.forEach((d, i) => {
    out[d.date] = i ? Math.max(0, d.v - ds[i - 1].v) : 0;
  });
  const today = anTodayStr();
  if (!ds.some(d => d.date === today)) {
    const pts = (c.series || [])
      .filter(p => p.t && String(p.t).slice(0, 10) === today)
      .map(p => Number(p.views) || 0);
    if (pts.length) {
      let base = null;
      for (let i = ds.length - 1; i >= 0; i--)
        if (ds[i].date < today) { base = ds[i].v; break; }
      out[today] = Math.max(0,
        pts[pts.length - 1] - (base != null ? base : pts[0]));
    }
  }
  return out;
}

// New views in the selected period. «7 дней» = 7 календарных дней,
// включая сегодня (сегодня — частично); дельта телескопируется к
// lastKnown - cum(сегодня-7), а при короткой истории — к
// lastKnown - самая ранняя точка. «Всё время» = полная кумулятива.
function anPeriodNew(c) {
  if (anPeriod === 0) return Number(c.views) || 0;
  const m = anDayDeltas(c);
  const t1 = Date.parse(anTodayStr() + "T00:00:00Z");
  let s = 0;
  for (let t = t1 - 6 * 864e5; t <= t1; t += 864e5)
    s += m[anDateStr(t)] || 0;
  return s;
}

// «За период» для видео: per-video daily из YouTube Analytics у новых
// каналов лагает/неполная — плоские нули там означают «нет данных», а не
// честный ноль. Дельту считаем только когда история покрывает начало
// периода (для 7д: earliest(v.daily) <= сегодня-7); «всё время» = views
// при любой непустой истории. Иначе null -> «—».
function anVidPeriodNew(v) {
  const dates = (v.daily || []).map(d => d.date).filter(Boolean).sort();
  if (!dates.length) return null;
  if (anPeriod === 0) return Number(v.views) || 0;
  if (dates[0] > anDateStr(Date.now() - 7 * 864e5)) return null;
  return anPeriodNew(v);
}

// Consecutive UTC days (no gaps) covered by the chart, ending today.
function anDaysList(chans) {
  const t1 = Date.parse(anTodayStr() + "T00:00:00Z");
  let t0 = t1 - 6 * 864e5;
  if (anPeriod === 0) {
    let min = null;
    chans.forEach(c => {
      (c.daily || []).forEach(d => {
        if (d.date && (!min || d.date < min)) min = d.date; });
      (c.series || []).forEach(p => {
        const d = String(p.t || "").slice(0, 10);
        if (d && (!min || d < min)) min = d; });
    });
    if (min) {
      const t = Date.parse(min + "T00:00:00Z");
      if (!isNaN(t)) t0 = t;
    }
  }
  const out = [];
  for (let t = t0; t <= t1; t += 864e5) out.push(t);
  return out;
}

function anBarsFor(chans) {
  const deltas = chans.map(c => anDayDeltas(c));
  const today = anTodayStr();
  return anDaysList(chans).map(t => {
    const date = anDateStr(t);
    let v = 0;
    deltas.forEach(m => { v += m[date] || 0; });
    return {date: date, ms: t, val: v, today: date === today};
  });
}

// Compact SVG bar chart (720x200 viewBox): one bar per day, today is
// translucent. Hover darkens the bar and pins a tooltip to its center.
function anBarsSvg(bars) {
  const W = 720, H = 200, P = 8;
  const n = bars.length;
  const slot = (W - 2 * P) / n;
  const bw = Math.max(2, Math.min(48, slot * 0.65));
  const vMax = Math.max.apply(null, bars.map(b => b.val).concat([1]));
  const yf = v => H - P - v / (vMax * 1.05) * (H - 2 * P);
  const rects = bars.map((b, i) => {
    const cx = P + i * slot + slot / 2;
    const y = yf(b.val);
    return `<rect class="an-bar" x="${(cx - bw / 2).toFixed(1)}" ` +
      `y="${y.toFixed(1)}" width="${bw.toFixed(1)}" ` +
      `height="${(H - P - y).toFixed(1)}" rx="2"` +
      (b.today ? ` fill-opacity="0.45" stroke="#0969da" stroke-width="1"` +
        ` stroke-dasharray="3 2"` : "") + `/>`;
  }).join("");
  const step = Math.max(1, Math.ceil(n / 10));
  const labels = bars.map((b, i) => i % step ? "" :
    `<text x="${(P + i * slot + slot / 2).toFixed(1)}" y="${H - 2}" ` +
    `font-size="10" fill="#6e7781" text-anchor="middle">${esc(
      fmtDayShort(b.ms))}</text>`).join("");
  return `<div class="an-chart" onmousemove="anBarHover(event)" ` +
    `onmouseleave="anBarLeave(event)">` +
    `<svg viewBox="0 0 ${W} ${H}" ` +
    `style="width:100%;max-width:900px;display:block">` +
    `<line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" ` +
    `stroke="#d0d7de" stroke-width="1"/>` +
    `<text x="${P}" y="12" font-size="10" fill="#6e7781">${fmtNum(
      Math.round(vMax * 1.05))}</text>` +
    rects + labels + `</svg>` +
    `<div class="an-tip" style="display:none"></div></div>`;
}

function anBarWrap(evt) {
  const t = evt.target;
  return t && t.closest ? t.closest(".an-chart") : null;
}

function anBarHover(evt) {
  const wrap = anBarWrap(evt);
  if (!wrap || !anBarsCur.length) return;
  const svg = wrap.querySelector("svg");
  const rect = svg.getBoundingClientRect();
  if (!rect.width) return;
  const W = 720, P = 8, n = anBarsCur.length;
  let mx = (evt.clientX - rect.left) / rect.width * W;
  if (!isFinite(mx)) return;
  const slot = (W - 2 * P) / n;
  const bi = Math.max(0, Math.min(n - 1, Math.floor((mx - P) / slot)));
  svg.querySelectorAll(".an-bar").forEach((r, i) =>
    r.setAttribute("fill", i === bi ? "#0550ae" : "#0969da"));
  const b = anBarsCur[bi];
  const tip = wrap.querySelector(".an-tip");
  tip.innerHTML = esc(fmtDayShort(b.ms)) + " &middot; " +
    fmtExact(b.val) + " просмотров";
  tip.style.display = "block";
  let px = (P + bi * slot + slot / 2) / W * rect.width;
  const half = tip.offsetWidth / 2;
  tip.style.left = Math.max(half, Math.min(rect.width - half, px)) + "px";
}

function anBarLeave(evt) {
  const wrap = anBarWrap(evt);
  if (!wrap) return;
  const tip = wrap.querySelector(".an-tip");
  if (tip) tip.style.display = "none";
  wrap.querySelectorAll(".an-bar").forEach(r =>
    r.setAttribute("fill", "#0969da"));
}

function anSegHtml() {
  return `<span class="an-seg">` +
    `<button class="${anPeriod === 7 ? "sel" : ""}" ` +
    `onclick="anSetPeriod(7)">7 дней</button>` +
    `<button class="${anPeriod === 0 ? "sel" : ""}" ` +
    `onclick="anSetPeriod(0)">Всё время</button></span>`;
}

function anHeadHtml(j) {
  return `<div class="an-head"><h2>Аналитика</h2>
    <span class="muted" style="font-size:12px">${j.refreshed_at
      ? "обновлено " + esc(fmtDT(j.refreshed_at))
      : "снапшотов пока нет"}</span>
    <button onclick="refreshAnalytics()" ${j.refreshing
      ? "disabled" : ""}>${j.refreshing
      ? "Обновляется&hellip;" : "Обновить"}</button>
    ${anSegHtml()}</div>`;
}

function anStat(label, val, sub) {
  return `<div class="stat"><div class="stat-num">${fmtExact(val)}</div>
    <div class="stat-label">${esc(label)}</div>${sub || ""}</div>`;
}

function anAvaImg(url, size) {
  return `<img class="an-ava" src="${esc(url)}" alt="" ` +
    (size ? `style="width:${size}px;height:${size}px" ` : "") +
    `referrerpolicy="no-referrer" loading="lazy" ` +
    `onerror="this.style.display='none'">`;
}

// ЭКРАН 1 — обзор всех каналов.
function anOverviewHtml(j) {
  const t = j.totals || {};
  const chans = (j.channels || []).filter(c => !c.error);
  const errs = (j.channels || []).filter(c => c.error);
  const today = anTodayStr();
  const yStr = anDateStr(Date.now() - 864e5);
  let todayNew = 0, yNew = 0;
  const withNew = chans.map(c => {
    const m = anDayDeltas(c);
    todayNew += m[today] || 0;
    yNew += m[yStr] || 0;
    return {c: c, nw: anPeriodNew(c)};
  }).sort((a, b) => b.nw - a.nw);
  const periodNew = anPeriod === 0
    ? (Number(t.views) || 0)
    : withNew.reduce((s, x) => s + x.nw, 0);
  // Сравнение с предыдущими 7 днями — только когда там есть данные.
  let cmp = "";
  if (anPeriod === 7) {
    let prev = 0, has = false;
    chans.forEach(c => {
      const m = anDayDeltas(c);
      for (let k = 13; k >= 7; k--) {
        const v = m[anDateStr(Date.now() - k * 864e5)];
        if (v != null) { has = true; prev += v; }
      }
    });
    if (has && prev > 0) {
      const pct = Math.round((periodNew - prev) / prev * 100);
      cmp = `<div class="an-sub" style="color:` +
        `${pct >= 0 ? "#1a7f37" : "#cf222e"}">${pct >= 0 ? "+" : ""}` +
        `${pct}% к пред. 7 дням</div>`;
    }
  }
  const bars = anBarsFor(chans);
  anBarsCur = bars;
  const rows = withNew.map(x => {
    const c = x.c;
    return `<tr class="an-row" data-h="${esc(c.handle || "")}" ` +
      `onclick="anOpenChan(this.getAttribute('data-h'))">
      <td>${c.avatar ? anAvaImg(c.avatar) : ""}<b>${esc(
          c.handle || "")}</b>
        <div class="muted" style="font-size:11px">${esc(
          c.title || "")}</div></td>
      <td>${fmtExact(c.views)}</td>
      <td>${fmtExact(x.nw)}</td>
      <td>${fmtExact(c.subs)}</td>
      <td>${fmtExact(c.video_count)}</td></tr>`;
  }).join("");
  const errRows = errs.map(c => `<tr>
    <td class="muted">${esc(c.handle || c.title || "канал")}</td>
    <td colspan="4"><span class="err" style="padding:0">${esc(
      c.error)}</span></td></tr>`).join("");
  const sum = k => chans.reduce((s, c) => s + (Number(c[k]) || 0), 0);
  const totalRow = chans.length ? `<tr class="an-total">
    <td>Итого</td><td>${fmtExact(sum("views"))}</td>
    <td>${fmtExact(periodNew)}</td><td>${fmtExact(sum("subs"))}</td>
    <td>${fmtExact(sum("video_count"))}</td></tr>` : "";
  return anHeadHtml(j) + `
    <div class="stats">
      ${anStat("Просмотры", periodNew, cmp)}
      ${anStat("Сегодня", todayNew)}
      ${anStat("Вчера", yNew)}
      ${anStat("Подписчики", t.subs)}
    </div>
    <div class="card">${anBarsSvg(bars)}</div>
    <div class="card"><table><thead><tr>
      <th>Канал</th><th>Просмотры</th><th>За период</th>
      <th>Подписчики</th><th>Видео</th>
    </tr></thead><tbody>
    ${rows || '<tr><td colspan="5" class="muted">нет каналов</td></tr>'}
    ${errRows}${totalRow}
    </tbody></table></div>`;
}

// ЭКРАН 2 — drill-down по одному каналу.
function anChannelHtml(j, handle) {
  const c = (j.channels || []).find(x => x.handle === handle);
  if (!c) { anChan = null; return anOverviewHtml(j); }
  const bars = anBarsFor([c]);
  anBarsCur = bars;
  const vids = (Array.isArray(c.videos) ? c.videos : []).slice()
    .sort((a, b) => (Number(b.views) || 0) - (Number(a.views) || 0));
  const rows = vids.map(v => {
    const url = v.url ||
      (v.video_id ? "https://youtu.be/" + v.video_id : "");
    const title = esc(v.title || v.video_id || "");
    const per = anVidPeriodNew(v);
    return `<tr>
      <td>${url
        ? `<a href="${esc(url)}" target="_blank" rel="noopener">${title}</a>`
        : title}</td>
      <td class="muted">${esc(fmtDT(v.published_at))}</td>
      <td>${fmtExact(v.views)}</td>
      <td>${per == null
        ? '<span class="muted">—</span>' : fmtExact(per)}</td>
      <td>${fmtExact(v.likes)}</td>
      <td>${fmtExact(v.comments)}</td></tr>`;
  }).join("");
  return `<div class="an-head">
      <button onclick="anBack()">&#8592; Все каналы</button>
      ${c.avatar ? anAvaImg(c.avatar, 28) : ""}
      <h2>${esc(c.handle || "")}</h2>
      <span class="muted">${esc(c.title || "")}</span>
      ${anSegHtml()}
    </div>
    <div class="card">${anBarsSvg(bars)}</div>
    <div class="stats">
      ${anStat("Просмотры всего", c.views)}
      ${anStat("За период", anPeriodNew(c))}
      ${anStat("Подписчики", c.subs)}
    </div>
    <div class="card"><table><thead><tr>
      <th>Название</th><th>Дата</th><th>Просмотры</th><th>За период</th>
      <th>Лайки</th><th>Комменты</th>
    </tr></thead><tbody>
    ${rows || '<tr><td colspan="6" class="muted">нет данных по видео</td></tr>'}
    </tbody></table></div>`;
}

function anRender() {
  if (!anJ) return;
  document.getElementById("main").innerHTML =
    anChan ? anChannelHtml(anJ, anChan) : anOverviewHtml(anJ);
}
function anSetPeriod(p) { anPeriod = p; anRender(); }
function anOpenChan(h) { anChan = h; anRender(); }
function anBack() { anChan = null; anRender(); }

async function renderAnalytics() {
  const main = document.getElementById("main");
  try {
    anJ = await api("/api/analytics");
    anRender();
    // A refresh is running server-side: poll until fresh data lands.
    if (anJ.refreshing)
      setTimeout(() => { if (tab === "analytics") renderAnalytics(); }, 5000);
  } catch (e) {
    main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

async function refreshAnalytics() {
  try {
    await api("/api/analytics/refresh", {method: "POST"});
  } catch (e) { alert("analytics refresh failed: " + e.message); }
  if (tab === "analytics") renderAnalytics();
}

async function renderLedger() {
  const main = document.getElementById("main");
  try {
    const j = await api("/api/ledger" +
      (selected ? "?project=" + encodeURIComponent(selected) : ""));
    const rows = (j.ledger || []).map(r => `<tr>
        <td class="mono">${r.id}</td>
        <td>${esc(r.project || "")}</td>
        <td>${esc(r.channel || "")}</td>
        <td class="mono">${esc(r.event_key || "")}</td>
        <td>${esc(r.title || "")}</td>
        <td class="muted">${esc(r.event_date || "")}</td>
        <td><span class="pill p-${esc(r.status || "unknown")}">${esc(
            r.status || "")}</span></td>
        <td class="muted" title="${esc(r.first_seen_at || "")}">${fmtDT(
            r.first_seen_at)}</td>
        <td class="muted" title="${esc(r.published_at || "")}">${fmtDT(
            r.published_at)}</td>
      </tr>`).join("");
    main.innerHTML = `
      ${projHeaderHtml()}
      <h2>дедуп ledger &middot; ${esc(selected || "all projects")}</h2>
      <div class="card"><table><thead><tr>
        <th>id</th><th>project</th><th>channel</th><th>event_key</th>
        <th>title</th><th>event date</th><th>status</th>
        <th>first seen</th><th>published</th>
      </tr></thead><tbody>
      ${rows || '<tr><td colspan="9" class="muted">ledger empty</td></tr>'}
      </tbody></table></div>`;
  } catch (e) {
    main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

function errHtml(e) {
  if (!e) return "";
  const s = String(e);
  if (s.length > 140) {
    return `<details class="errdet"><summary>${esc(s.slice(0, 140))}\u2026</summary>
      <pre class="log">${esc(s)}</pre></details>`;
  }
  return `<div class="err">${esc(s)}</div>`;
}

async function renderItems() {
  const main = document.getElementById("main");
  try {
    const [ch, it] = await Promise.all([
      api("/api/channels?project=" + encodeURIComponent(selected)),
      api("/api/items?project=" + encodeURIComponent(selected) +
          "&limit=100" + stateParam()),
    ]);
    const chanById = {};
    for (const c of (ch.channels || [])) chanById[c.id] = c;
    const chans = (ch.channels || []).map(c => {
      const u = chanUrl(c);
      const label = esc(c.handle || c.platform);
      const head = u
        ? `<a href="${esc(u)}" target="_blank" rel="noopener"><b>${label}</b> &#8599;</a>`
        : `<b>${label}</b>`;
      return `<div class="chan">${head}
        &middot; ${esc(c.platform)}
        &middot; limit ${c.daily_limit}/day
        &middot; times ${esc((c.publish_times_utc || []).join(" "))} UTC
        &middot; <span class="mono">${esc(c.token_path || "")}</span>
        &middot; ${c.active
          ? '<span class="pill st-published">active</span>'
          : '<span class="pill st-new">inactive</span>'}</div>`;
    }).join("");
    const rows = (it.items || []).map(i => {
      const acts = (i.state === "review"
        ? `<button class="ok" onclick="transition('${esc(i.id)}','approved')">Approve</button>
           <button class="bad" onclick="transition('${esc(i.id)}','rejected')">Reject</button>`
        : "") + (i.state === "archived"
        ? `<button onclick="transition('${esc(i.id)}','review')">Restore</button>`
        : i.state === "published" ? ""
        : `<button onclick="transition('${esc(i.id)}','archived')">Archive</button>`);
      const playOpen = openPlay.has(i.id);
      const play = i.local_path
        ? `<button id="pbtn-${esc(i.id)}" title="play video" onclick="togglePlay('${esc(i.id)}')">${playOpen ? "■ Скрыть" : "&#9654; Play"}</button>` : "";
      const detOpen = openDetail.has(i.id);
      const detBtn = `<button
        onclick="toggleDetail('${esc(i.id)}')">${detOpen
          ? "hide details" : "details"}</button>`;
      const url = i.published_url
        ? `<a href="${esc(i.published_url)}" target="_blank" rel="noopener"
             title="${esc(i.published_url)}">${esc(
               i.published_url.length > 42
                 ? i.published_url.slice(0, 42) + "\u2026"
                 : i.published_url)} &#8599;</a>` : "";
      const future = i.scheduled_at &&
        new Date(i.scheduled_at).getTime() > Date.now();
      const sched = i.scheduled_at
        ? `<span class="${future ? "future" : "muted"}"
             title="${esc(i.scheduled_at)}">${fmtDT(i.scheduled_at)}</span>`
        : "";
      const chan = chanById[i.channel_id];
      const chanName = chan ? (chan.handle || chan.platform) : "";
      const metaOpen = openMeta.has(i.id);
      const metaBtn = i.meta
        ? `<button onclick="toggleMeta('${esc(i.id)}')">${metaOpen
             ? "hide meta" : "show meta"}</button>` : "";
      const metaPre = (i.meta && metaOpen)
        ? `<pre class="log">${esc(prettyJSON(i.meta))}</pre>` : "";
       const detailRow = detOpen
        ? `<tr class="detail"><td colspan="7">
             <div class="kv">
               <span class="k">id</span>
               <span class="mono">${esc(i.id)}</span>
               <span class="k">project</span>
               <span>${esc(selected)}</span>
               <span class="k">cost</span>
               <span>${costBadgeHtml(i.meta) || '<span class="muted">—</span>'}</span>
               <span class="k">created</span>
               <span>${fmtDT(i.created_at)}
                 <span class="mono">${esc(i.created_at || "")}</span></span>
               <span class="k">updated</span>
               <span>${fmtDT(i.updated_at)}
                 <span class="mono">${esc(i.updated_at || "")}</span></span>
               <span class="k">local_path</span>
               <span class="mono">${esc(i.local_path || "")}</span>
               <span class="k">meta</span><span>${metaBtn}</span>
             </div>${metaPre}</td></tr>` : "";
      const playerRow = i.local_path
        ? `<tr class="player" id="pl-${esc(i.id)}" style="${playOpen ? "" : "display:none"}">
             <td colspan="7">${playOpen ? playerHtml(i.id) : ""}</td></tr>` : "";
      return `<tr>
        <td style="width:56px; padding:6px; text-align:center;">
          <a href="/thumb/${encodeURIComponent(i.id)}" target="_blank" title="View Full YouTube Thumbnail (1080x1920)">
            <img class="cover-thumb" src="/thumb/${encodeURIComponent(i.id)}" alt="cover" onerror="this.style.opacity='0.2'">
          </a>
        </td>
        <td><span class="t-bold">${esc(i.title || i.id)}</span>${costBadgeHtml(i.meta)}
            <div class="mono">${esc(i.id)}</div>${errHtml(i.error)}</td>
        <td>${esc(chanName)}</td>
        <td><span class="pill st-${esc(i.state)}">${esc(i.state)}</span></td>
        <td>${sched}</td>
        <td>${url}</td>
        <td>${acts}${play}${detBtn}</td>
      </tr>${detailRow}${playerRow}`;
    }).join("");
    const proj = projects.find(p => p.alias === selected) || {};
    main.innerHTML = `
      <h2>${esc(proj.name || selected)}</h2>
      <div class="mono muted">${esc(selected)}</div>
      ${proj.notes
        ? `<div class="card"><div class="muted">${esc(proj.notes)}</div></div>`
        : ""}
      <div class="card chans">${chans ||
        '<span class="muted">no channels</span>'}</div>
      <div class="card"><table><thead><tr>
        <th style="width:56px;">cover</th><th>title</th><th>channel</th><th>state</th>
        <th>scheduled</th><th>published</th><th></th>
      </tr></thead><tbody>
      ${rows || '<tr><td colspan="7" class="muted">Пока пусто — пайплайн ещё не принёс контент</td></tr>'}
      </tbody></table></div>`;
    for (const pid of openPlay) {
      setupPlayer(pid);
    }
  } catch (e) {
    main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

function toggleDetail(id) {
  if (openDetail.has(id)) openDetail.delete(id); else openDetail.add(id);
  renderItems();
}

function toggleMeta(id) {
  if (openMeta.has(id)) openMeta.delete(id); else openMeta.add(id);
  renderItems();
}

function playerHtml(id) {
  const enc = encodeURIComponent(id);
  return `<div style="display:flex; align-items:flex-start; gap:16px; padding:10px 0;">
    <div style="position:relative; width:280px; max-width:280px; height:500px; background:#000; border-radius:8px; overflow:hidden; box-shadow:0 2px 10px rgba(0,0,0,0.3); flex-shrink:0;">
      <video id="vid-${esc(id)}" controls playsinline webkit-playsinline preload="metadata"
        style="width:100%; height:100%; object-fit:contain; background:#000; display:block;"
        src="/media/${enc}"></video>
      <div id="vspin-${esc(id)}" style="position:absolute; inset:0; background:rgba(0,0,0,0.65); display:flex; align-items:center; justify-content:center; color:#fff; font-size:14px; font-weight:500; pointer-events:none; z-index:2;">Загрузка...</div>
      <div id="voverlay-${esc(id)}" style="position:absolute; inset:0; background:rgba(0,0,0,0.5); display:none; align-items:center; justify-content:center; z-index:3; cursor:pointer;" onclick="playVideo('${esc(id)}')">
        <button type="button" onclick="playVideo('${esc(id)}')" style="padding:10px 18px; font-size:13px; font-weight:600; background:rgba(0,0,0,0.85); color:#fff; border:1px solid rgba(255,255,255,0.4); border-radius:6px; cursor:pointer;">▶ Нажмите для воспроизведения</button>
      </div>
      <div id="verr-${esc(id)}" style="position:absolute; inset:0; background:rgba(20,20,20,0.92); display:none; flex-direction:column; align-items:center; justify-content:center; padding:16px; text-align:center; color:#ff6b6b; font-size:13px; z-index:4;"></div>
    </div>
    <div style="padding-top:10px;">
      <a href="/media/${enc}" target="_blank" rel="noopener" style="font-weight:600; font-size:13px; color:#0969da;">Прямой поток видео в новой вкладке &#8599;</a>
      <div class="muted" style="margin-top:8px; font-size:12px;">Вертикальный формат Shorts (1080x1920)</div>
    </div>
  </div>`;
}

function setupPlayer(id) {
  const v = document.getElementById("vid-" + id);
  if (!v) return;
  const sp = document.getElementById("vspin-" + id);
  const ov = document.getElementById("voverlay-" + id);
  const er = document.getElementById("verr-" + id);

  v.onwaiting = () => {
    if (sp) {
      sp.textContent = "Буферизация...";
      sp.style.display = "flex";
    }
  };
  v.onplaying = () => {
    if (sp) sp.style.display = "none";
    if (ov) ov.style.display = "none";
  };
  v.oncanplay = () => {
    if (sp) sp.style.display = "none";
  };
  v.onerror = () => {
    if (sp) sp.style.display = "none";
    if (ov) ov.style.display = "none";
    if (er) {
      let msg = "Ошибка воспроизведения";
      if (v.error) {
        switch (v.error.code) {
          case 1: msg = "Загрузка видео прервана (код 1)"; break;
          case 2: msg = "Сетевая ошибка при загрузке видео (код 2)"; break;
          case 3: msg = "Ошибка декодирования видео (код 3)"; break;
          case 4: msg = "Формат или кодек видео не поддерживается (код 4)"; break;
          default: msg = "Ошибка видео (" + v.error.code + ")"; break;
        }
      }
      er.innerHTML = `<div style="font-weight:600; margin-bottom:8px;">${esc(msg)}</div>` +
        `<a href="/media/${encodeURIComponent(id)}" target="_blank">Открыть напрямую в новой вкладке &#8599;</a>`;
      er.style.display = "flex";
    }
  };

  const p = v.play();
  if (p && p.catch) {
    p.catch(e => {
      if (sp) sp.style.display = "none";
      if (ov) ov.style.display = "flex";
    });
  }
}

function playVideo(id) {
  const v = document.getElementById("vid-" + id);
  if (!v) return;
  const sp = document.getElementById("vspin-" + id);
  const ov = document.getElementById("voverlay-" + id);
  if (sp) {
    sp.textContent = "Буферизация...";
    sp.style.display = "flex";
  }
  const p = v.play();
  if (p && p.catch) {
    p.catch(e => {
      if (sp) sp.style.display = "none";
      if (ov) ov.style.display = "flex";
      console.warn("Play rejected:", e);
    });
  }
}

function togglePlay(id) {
  const row = document.getElementById("pl-" + id);
  const btn = document.getElementById("pbtn-" + id);
  if (!row) return;
  if (row.style.display === "none") {
    openPlay.add(id);
    if (btn) btn.innerHTML = "■ Скрыть";
    const cell = row.firstElementChild;
    cell.innerHTML = playerHtml(id);
    row.style.display = "";
    setupPlayer(id);
  } else {
    openPlay.delete(id);
    if (btn) btn.innerHTML = "&#9654; Play";
    const v = row.querySelector("video");
    if (v) v.pause();
    row.style.display = "none";
    row.firstElementChild.innerHTML = "";
  }
}

function summaryHtml(s) {
  if (!s) return "";
  let parsed = null;
  try { parsed = JSON.parse(s); } catch (e) { parsed = null; }
  if (parsed && typeof parsed === "object") {
    return `<pre class="inline">${esc(JSON.stringify(parsed, null, 2))}</pre>`;
  }
  return esc(s);
}

async function renderRuns() {
  const main = document.getElementById("main");
  try {
    const j = await api("/api/runs?limit=100" +
      (selected ? "&project=" + encodeURIComponent(selected) : ""));
    const all = j.runs || [];
    const idle = all.filter(isIdleRun).length;
    const list = showIdleRuns ? all : all.filter(r => !isIdleRun(r));
    const rows = list.map(r => {
      const open = openLogs.has(r.id);
      const logBtn = r.log
        ? `<button onclick="toggleLog(${r.id})">${open
             ? "hide log" : "log"}</button>` : "";
      const logRow = (r.log && open)
        ? `<tr><td colspan="8"><pre class="log">${esc(r.log)}</pre></td></tr>`
        : "";
      return `<tr>
        <td class="mono">${r.id}</td>
        <td>${esc(r.project_alias || "")}</td>
        <td>${esc(r.pipeline || "")}</td>
        <td class="muted" title="${esc(r.started_at || "")}">${fmtDT(
            r.started_at || r.created_at)}</td>
        <td class="muted" title="${esc(r.finished_at || "")}">${fmtDT(
            r.finished_at)}</td>
        <td class="muted">${fmtDur(r.started_at, r.finished_at)}</td>
        <td><span class="pill p-${esc(r.status || "unknown")}">${esc(
            r.status || "")}</span></td>
        <td>${summaryHtml(r.summary)} ${logBtn}</td>
      </tr>${logRow}`;
    }).join("");
    main.innerHTML = `
      ${projHeaderHtml()}
      <h2>runs &middot; ${esc(selected)}
        <button style="margin-left:10px"
          onclick="showIdleRuns=!showIdleRuns;renderRuns()">${showIdleRuns
            ? "скрыть холостые" : "показать холостые (" + idle + ")"}</button>
      </h2>
      <div class="card"><table><thead><tr>
        <th>id</th><th>project</th><th>pipeline</th><th>started</th>
        <th>finished</th><th>dur</th><th>status</th><th>summary</th>
      </tr></thead><tbody>
      ${rows || '<tr><td colspan="8" class="muted">no runs</td></tr>'}
      </tbody></table></div>`;
  } catch (e) {
    main.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

// A run is "idle" (noise) when it did nothing: a *-tick with
// {"advanced": 0} or a sync where every counter is 0.
function isIdleRun(r) {
  if (!r.summary) return false;
  let s;
  try { s = JSON.parse(r.summary); } catch (e) { return false; }
  if (!s || typeof s !== "object" || Array.isArray(s)) return false;
  const pipe = r.pipeline || "";
  if (pipe.indexOf("tick") >= 0 && (s.advanced || 0) === 0) return true;
  if (pipe === "sync" &&
      Object.values(s).every(v => v === 0)) return true;
  return false;
}

function toggleLog(id) {
  if (openLogs.has(id)) openLogs.delete(id); else openLogs.add(id);
  renderRuns();
}

function stateParam() {
  const v = document.getElementById("stateFilter").value;
  return v ? "&state=" + encodeURIComponent(v) : "";
}

async function transition(id, to) {
  try {
    await api("/api/items/" + encodeURIComponent(id) + "/transition", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({to}),
    });
    await load();
  } catch (e) {
    if (e.status === 409 || (e.message && e.message.includes("Duplicate content detected"))) {
      if (confirm("Duplicate warning: " + e.message + ". Force approve this video?")) {
        try {
          await api("/api/items/" + encodeURIComponent(id) + "/transition?force=1", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({to: to, force: true}),
          });
          await load();
        } catch (err) {
          alert(err.message);
        }
      }
    } else {
      alert(e.message);
    }
  }
}

async function sync() {
  try {
    const r = await api("/api/sync", {method: "POST"});
    alert("sync: " + JSON.stringify(r));
  } catch (e) { alert("sync failed: " + e.message); }
  await load();
}

document.getElementById("stateFilter").onchange = renderMain;
load();
// Auto-refresh the active tab only; never clobber a playing or open video.
setInterval(() => {
  if (openPlay.size > 0 || document.querySelector("#main video")) return;
  load();
}, 30000);
</script>
</body>
</html>
"""


# fshorts#6: marker line in DASHBOARD_HTML replaced at serve time with the
# bootstrapped project alias (null on the root page).
BOOT_MARKER = "const BOOT_PROJECT = null;"


def render_dashboard(alias):
    """Dashboard HTML with BOOT_PROJECT set so it boots pre-filtered."""
    return DASHBOARD_HTML.replace(
        BOOT_MARKER, "const BOOT_PROJECT = %s;" % json.dumps(alias))


def project_404_page(conn, alias):
    """Friendly 404 for /p/<unknown>: lists every valid project URL."""
    items = "".join(
        '<li><a href="/p/{a}">/p/{a}</a> &middot; {n}</li>'.format(
            a=html.escape(p["alias"], quote=True),
            n=html.escape(p["name"] or p["alias"]))
        for p in db.list_projects(conn))
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>проект не найден · posting hub</title>
<style>
  body {{ background: #f5f6f8; color: #1f2328; font: 14px/1.45 -apple-system,
         "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 40px 20px; }}
  .card {{ background: #fff; border: 1px solid #e1e4e8; border-radius: 8px;
           max-width: 560px; margin: 0 auto; padding: 20px 24px;
           box-shadow: 0 1px 3px rgba(31,35,40,.07); }}
  h1 {{ font-size: 17px; margin-bottom: 6px; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
           monospace; font-size: 12.5px; color: #57606a; }}
  li {{ margin: 4px 0; }}
  a {{ color: #0969da; text-decoration: none; }}
</style>
</head>
<body>
<div class="card">
  <h1>Проект не найден</h1>
  <p>Нет проекта с alias <span class="mono">{alias}</span>.
     Доступные проекты:</p>
  <ul>
    {items}
  </ul>
  <p><a href="/">&larr; все проекты</a></p>
</div>
</body>
</html>""".format(alias=html.escape(alias or ""), items=items)


def _json_body(handler):
    """Parse the JSON request body; returns {} for empty bodies."""
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def run_sync():
    """Run the adapters sync script once; tolerate its absence.

    Returns (ok, payload_dict).
    """
    if not os.path.exists(SYNC_SCRIPT):
        return False, {"error": "sync script not found: %s" % SYNC_SCRIPT}
    env = dict(os.environ)  # pass POSTING_DB through when overridden
    try:
        proc = subprocess.run(
            [sys.executable, SYNC_SCRIPT],
            capture_output=True, text=True, timeout=SYNC_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return False, {"error": "sync timed out after %ds" % SYNC_TIMEOUT_S}
    if proc.returncode != 0:
        return False, {"error": "sync exited %d: %s"
                       % (proc.returncode, (proc.stderr or "").strip()[:500])}
    try:
        return True, json.loads(proc.stdout)
    except ValueError:
        return False, {"error": "sync did not return JSON",
                       "stdout": (proc.stdout or "")[:500]}


# fshorts#10: serialize analytics.refresh — it hits the YouTube API for
# every channel (~10-30s), so never run two at once.
ANALYTICS_REFRESH_LOCK = threading.Lock()
_analytics_refresh_running = False


def _analytics_today_missing(ov):
    """True when the daily series has no row for today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return not any(d.get("date") == today for d in (ov.get("daily") or []))


def _start_analytics_refresh():
    """Kick off analytics.refresh in a daemon thread if none is running.

    Returns True when a refresh is running after the call (started here
    or already in progress).
    """
    global _analytics_refresh_running
    if analytics_mod is None:
        return False
    with ANALYTICS_REFRESH_LOCK:
        if _analytics_refresh_running:
            return True
        _analytics_refresh_running = True

    def _run():
        global _analytics_refresh_running
        try:
            analytics_mod.refresh(db.db_path())
        except Exception:
            pass  # background refresh is best-effort; GET stays cache-only
        finally:
            with ANALYTICS_REFRESH_LOCK:
                _analytics_refresh_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def _run_analytics_refresh_sync():
    """Run analytics.refresh in the request thread, serialized with
    background runs. Returns the overview dict, or None when a refresh
    is already in progress."""
    global _analytics_refresh_running
    with ANALYTICS_REFRESH_LOCK:
        if _analytics_refresh_running:
            return None
        _analytics_refresh_running = True
    try:
        return analytics_mod.refresh(db.db_path())
    finally:
        with ANALYTICS_REFRESH_LOCK:
            _analytics_refresh_running = False


class Handler(BaseHTTPRequestHandler):
    server_version = "posting-hub/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):  # quieter logs: one line, no UA spam
        sys.stderr.write("%s - %s\n" % (self.address_string(),
                                        fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def _error(self, code, msg):
        self._send(code, {"error": msg})

    # -- routing ---------------------------------------------------------

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            conn = db.get_db()
            try:
                if path.startswith("/media/"):
                    self._send_media(conn, unquote(path[len("/media/"):]), head_only=True)
                elif path.startswith("/thumb/"):
                    self._send_thumb(conn, unquote(path[len("/thumb/"):]), head_only=True)
                else:
                    self._error(404, "not found: %s" % path)
            finally:
                conn.close()
        except Exception as e:
            self._error(500, str(e))

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            conn = db.get_db()
            try:
                if path == "/" or path == "/index.html":
                    self._send_html(render_dashboard(None))
                elif path.startswith("/p/"):
                    # fshorts#6: per-project shareable URL.
                    alias = unquote(path[len("/p/"):]).strip("/")
                    if alias and db.get_project(conn, alias) is not None:
                        self._send_html(render_dashboard(alias))
                    else:
                        self._send_html(project_404_page(conn, alias),
                                        code=404)
                elif path == "/api/overview":
                    inc = (qs.get("include_archived") or ["0"])[0] in \
                        ("1", "true")
                    self._send(200, {"projects": db.overview(
                        conn, include_archived=inc)})
                elif path == "/api/projects":
                    self._send(200, {"projects": db.list_projects(conn)})
                elif path == "/api/channels":
                    self._send(200, {"channels": db.list_channels(
                        conn, project_alias=(qs.get("project") or [None])[0])})
                elif path == "/api/items":
                    limit = int((qs.get("limit") or ["200"])[0])
                    limit = max(1, min(limit, 1000))
                    state = (qs.get("state") or [None])[0]
                    if state and state not in db.ITEM_STATES:
                        self._error(400, "unknown state: %s" % state)
                        return
                    inc = (qs.get("include_archived") or ["0"])[0] in \
                        ("1", "true")
                    self._send(200, {"items": db.list_items(
                        conn, project_alias=(qs.get("project") or [None])[0],
                        state=state, limit=limit, include_archived=inc)})
                elif path == "/api/runs":
                    limit = int((qs.get("limit") or ["50"])[0])
                    limit = max(1, min(limit, 500))
                    self._send(200, {"runs": db.list_runs(
                        conn, project_alias=(qs.get("project") or [None])[0],
                        limit=limit)})
                elif path == "/api/ledger":
                    status = (qs.get("status") or [None])[0]
                    if status and status not in ledger.STATUSES:
                        self._error(400, "unknown ledger status: %s" % status)
                        return
                    self._send(200, {"ledger": ledger.list_ledger(
                        project=(qs.get("project") or [None])[0],
                        status=status)[:200]})
                elif path == "/api/schedule":
                    if scheduler_mod is None:
                        self._error(501, "engine scheduler not available")
                        return
                    days = int((qs.get("days") or ["7"])[0])
                    days = max(1, min(days, 14))
                    out = scheduler_mod.hub_schedule(conn, days=days)
                    # fshorts#6: optional project filter for /p/<alias> pages
                    proj = (qs.get("project") or [None])[0]
                    if proj:
                        out["days"] = [
                            {"date": d["date"],
                             "entries": [e for e in d["entries"]
                                         if e["project"] == proj]}
                            for d in out["days"]]
                        out["days"] = [d for d in out["days"] if d["entries"]]
                        out["unbound"] = [u for u in out["unbound"]
                                          if u["project"] == proj]
                        out["channels"] = [c for c in out["channels"]
                                           if c["project"] == proj]
                    self._send(200, out)
                elif path == "/api/analytics":
                    if analytics_mod is None:
                        self._error(503, "analytics module unavailable")
                        return
                    try:
                        ov = analytics_mod.overview(db.db_path())
                        # Stale cache (no row for today) -> refresh in a
                        # background thread; the frontend polls while the
                        # response carries "refreshing": true.
                        if not ov.get("refreshing") and \
                                _analytics_today_missing(ov):
                            if _start_analytics_refresh():
                                ov["refreshing"] = True
                        self._send(200, ov)
                    except Exception as e:
                        self._error(500, str(e))
                elif path.startswith("/media/"):
                    self._send_media(conn, unquote(path[len("/media/"):]))
                elif path.startswith("/thumb/"):
                    self._send_thumb(conn, unquote(path[len("/thumb/"):]))
                else:
                    self._error(404, "not found: %s" % path)
            finally:
                conn.close()
        except Exception as e:  # never leak a bare 500 without a message
            self._error(500, str(e))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/sync":
                ok, payload = run_sync()
                self._send(200 if ok else 502, payload)
                return
            if path == "/api/analytics/refresh":
                if analytics_mod is None:
                    self._error(503, "analytics module unavailable")
                    return
                try:
                    ov = _run_analytics_refresh_sync()
                    if ov is None:
                        # a refresh is already in flight; report the cache
                        ov = analytics_mod.overview(db.db_path())
                        ov["refreshing"] = True
                    self._send(200, ov)
                except Exception as e:
                    self._error(500, str(e))
                return

            body = _json_body(self)
            conn = db.get_db()
            try:
                if path == "/api/channels":
                    self._post_channel(conn, body)
                elif path == "/api/runs":
                    self._post_run(conn, body)
                elif path == "/api/delegate":
                    self._post_delegate(conn, body)
                elif path == "/api/check-duplicate":
                    self._post_check_duplicate(conn, body)
                elif (path.startswith("/api/items/")
                      and path.endswith("/transition")):
                    item_id = path[len("/api/items/"):-len("/transition")]
                    self._post_transition(conn, item_id, body)
                else:
                    self._error(404, "not found: %s" % path)
            finally:
                conn.close()
        except json.JSONDecodeError:
            self._error(400, "invalid JSON body")
        except Exception as e:
            self._error(500, str(e))

    # -- handlers ------------------------------------------------------

    def _post_channel(self, conn, body):
        project = body.get("project_alias") or body.get("project")
        platform = body.get("platform")
        if not project or not platform:
            self._error(400, "project_alias and platform are required")
            return
        if db.get_project(conn, project) is None:
            self._error(404, "unknown project: %s" % project)
            return
        cid = db.upsert_channel(
            conn, project, platform,
            handle=body.get("handle"),
            token_path=body.get("token_path"),
            daily_limit=body.get("daily_limit"),
            publish_times_utc=body.get("publish_times_utc"),
            active=body.get("active", 1))
        self._send(200, {"channel": db.get_channel(conn, cid)})

    def _post_check_duplicate(self, conn, body):
        video_path = body.get("video_path")
        text = body.get("text", "")
        channel_id = body.get("channel_id")
        source_ref = body.get("source_ref", "")
        scheduled_at = body.get("scheduled_at") or body.get("time")

        if not video_path and not text and not source_ref:
            self._error(400, "video_path, text, or source_ref is required")
            return

        try:
            res = db.check_duplicate_candidate(
                conn,
                video_path=video_path,
                text=text,
                channel_id=channel_id,
                scheduled_at=scheduled_at,
                source_ref=source_ref,
                check_states=("review", "approved", "scheduled", "published")
            )
            self._send(200, res)
        except Exception as e:
            self._error(500, f"deduplication check error: {e}")

    def _post_transition(self, conn, item_id, body):
        to = body.get("to")
        if not to:
            self._error(400, "missing 'to'")
            return

        # Check override flag: ?force=1 or body force: 1
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        force_qs = qs.get("force", ["0"])[0] in ("1", "true", "True", "yes")
        force_body = body.get("force") in (1, True, "1", "true", "True", "yes")
        force = force_qs or force_body

        if not force and to in ("approved", "scheduled"):
            item = db.get_item(conn, item_id)
            if item is not None:
                sched = body.get("scheduled_at") or item.get("scheduled_at")
                try:
                    dedup = db.check_duplicate_item(
                        conn,
                        item_id=item_id,
                        scheduled_at=sched,
                        check_states=("approved", "scheduled", "published"),
                        exclude_item_id=item_id
                    )
                    if dedup.get("is_duplicate") and not dedup.get("allowed", True):
                        self._send(409, {
                            "error": "Duplicate content detected",
                            "duplicate_of": dedup.get("duplicate_of"),
                            "reason": dedup.get("reason"),
                            "classification": dedup.get("classification"),
                            "similarity_score": dedup.get("similarity_score")
                        })
                        return
                except Exception as e:
                    log.warning("Dedup check error on item %s transition: %s", item_id, e)

        try:
            item = db.transition_item(
                conn, item_id, to,
                scheduled_at=body.get("scheduled_at"),
                published_url=body.get("published_url"),
                error=body.get("error"),
                meta=body.get("meta"))
        except ValueError as e:
            msg = str(e)
            code = 404 if msg.startswith("unknown item") else 400
            self._error(code, msg)
            return
        self._send(200, {"item": item})

    def _post_run(self, conn, body):
        project = body.get("project_alias") or body.get("project")
        pipeline = body.get("pipeline")
        if not project or not pipeline:
            self._error(400, "project_alias and pipeline are required")
            return
        try:
            run_id = db.insert_run(
                conn, project, pipeline,
                status=body.get("status"),
                summary=body.get("summary"),
                log=body.get("log"),
                started_at=body.get("started_at"),
                finished_at=body.get("finished_at"))
        except ValueError as e:
            self._error(400, str(e))
            return
        self._send(200, {"id": run_id})

    def _post_delegate(self, conn, body):
        alias = body.get("project_alias") or body.get("project")
        title = body.get("title")
        if not alias or not title:
            self._error(400, "project_alias and title are required")
            return
        project = db.get_project(conn, alias)
        if project is None:
            self._error(404, "unknown project: %s" % alias)
            return
        module_name = ADAPTER_MODULES.get(project.get("pipeline_kind") or "")
        enqueue = None
        if module_name:
            try:
                enqueue = getattr(importlib.import_module(module_name),
                                  "enqueue", None)
            except Exception:
                enqueue = None
        if not callable(enqueue):
            self._error(501, "project has no enqueue")
            return
        try:
            queued = enqueue(title, body.get("notes"))
        except Exception as e:
            self._error(500, "enqueue failed: %s" % e)
            return
        self._send(200, {"queued": queued})

    # -- media -----------------------------------------------------------

    def _send_media(self, conn, item_id, head_only=False):
        """Stream an item's video file with HTTP Range support."""
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
            item = db.get_item(conn, item_id)
            path = resolve_media_path(item)
            if path is None or not os.path.isfile(path):
                self._error(404, "no media for item: %s" % item_id)
                return
            guess_mime, _ = mimetypes.guess_type(path)
            mime = guess_mime or ("audio/mpeg" if path.endswith(".mp3") else "video/mp4")
            self.send_response(200)
            self.send_header("X-Accel-Redirect", "/_internal_media" + path)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", "inline")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            return

        item = db.get_item(conn, item_id)
        path = resolve_media_path(item)
        if path is None or not os.path.isfile(path):
            self._error(404, "no media for item: %s" % item_id)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        partial = False
        range_header = self.headers.get("Range")
        if range_header:
            m = RANGE_RE.match(range_header.strip())
            if m and (m.group(1) or m.group(2)):
                s, e = m.group(1), m.group(2)
                if s == "":
                    # suffix range: the last N bytes
                    start = max(0, size - int(e))
                else:
                    start = int(s)
                    if e != "":
                        end = min(int(e), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        guess_mime, _ = mimetypes.guess_type(path)
        mime = guess_mime or ("audio/mpeg" if path.endswith(".mp3") else "video/mp4")
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if partial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head_only or getattr(self, "command", "") == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break  # client went away mid-stream
                remaining -= len(chunk)

    def _send_thumb(self, conn, item_id, head_only=False):
        """Serve the thumbnail image for an item."""
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
            item = db.get_item(conn, item_id)
            path = resolve_thumb_path(item)
            if path is None or not os.path.isfile(path):
                self._error(404, "no thumbnail for item: %s" % item_id)
                return
            guess_mime, _ = mimetypes.guess_type(path)
            mime = guess_mime or "image/jpeg"
            self.send_response(200)
            self.send_header("X-Accel-Redirect", "/_internal_media" + path)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            return

        item = db.get_item(conn, item_id)
        path = resolve_thumb_path(item)
        if path is None or not os.path.isfile(path):
            self._error(404, "no thumbnail for item: %s" % item_id)
            return
        try:
            size = os.path.getsize(path)
            guess_mime, _ = mimetypes.guess_type(path)
            mime = guess_mime or "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            if head_only or getattr(self, "command", "") == "HEAD":
                return
            with open(path, "rb") as f:
                data = f.read()
            self.wfile.write(data)
        except Exception as e:
            self._error(500, str(e))


def make_server(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    db.get_db().close()  # ensure schema exists at startup
    srv = make_server()
    print("posting hub listening on http://%s:%d" % (HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------- selftest

def _selftest():
    """Offline self-check: temp DB via POSTING_DB, ephemeral port, loopback
    HTTP calls via urllib. No external network."""
    tmp = os.path.join(tempfile.mkdtemp(prefix="posting-srv-selftest-"),
                       "test.db")
    os.environ["POSTING_DB"] = tmp
    conn = db.get_db()
    db.seed(conn)
    cid = db.list_channels(conn, "dressit-fshorts")[0]["id"]
    db.upsert_item(conn, "st-1", channel_id=cid, title="selftest clip")
    db.transition_item(conn, "st-1", "review")
    conn.close()

    srv = make_server(port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % port

    def get(path):
        req = urllib.request.Request(base + path)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def post(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type":
                                              "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get_raw(path, headers=None):
        req = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # Media fixtures: a dir-based item (fshorts style: <dir>/final.mp4)
    # and an item with no local_path at all.
    media_dir = tempfile.mkdtemp(prefix="posting-media-selftest-")
    video = bytes(range(256)) * 4  # 1024 deterministic bytes
    with open(os.path.join(media_dir, "final.mp4"), "wb") as f:
        f.write(video)
    conn = db.get_db()
    db.upsert_item(conn, "st-media", channel_id=cid, title="media clip",
                   local_path=media_dir)
    db.upsert_item(conn, "st-nomedia", channel_id=cid, title="no media")
    db.transition_item(conn, "st-nomedia", "archived")  # hidden junk
    conn.close()

    try:
        # Dashboard
        with urllib.request.urlopen(base + "/") as r:
            page = r.read().decode("utf-8")
            assert r.status == 200 and "posting hub" in page
            assert "Расписание" in page  # fshorts#4 schedule tab
            assert "const BOOT_PROJECT = null;" in page  # no pre-filter

        # fshorts#6: per-project URL /p/<alias>
        st, h, b = get_raw("/p/dressit-fshorts")
        page = b.decode("utf-8")
        assert st == 200 and \
            'const BOOT_PROJECT = "dressit-fshorts";' in page, (st, page[:200])
        st, h, b = get_raw("/p/ghost")
        page = b.decode("utf-8")
        assert st == 404 and "/p/dressit-fshorts" in page and \
            "/p/dressit-shorts" in page, (st, page[:200])

        st, j = get("/api/overview")
        assert st == 200 and isinstance(j["projects"], list), j
        assert {p["alias"] for p in j["projects"]} == {
            "dressit-fshorts", "dressit-shorts"}
        fs = [p for p in j["projects"]
              if p["alias"] == "dressit-fshorts"][0]
        assert fs["states"].get("review") == 1, fs

        st, j = get("/api/projects")
        assert st == 200 and len(j["projects"]) == 2

        st, j = get("/api/channels?project=dressit-shorts")
        assert st == 200 and len(j["channels"]) == 1
        assert j["channels"][0]["handle"] == "@решимость2"
        assert j["channels"][0]["publish_times_utc"] == ["13:00", "17:00",
                                                         "21:00"]

        st, j = get("/api/items?project=dressit-fshorts&state=review")
        assert st == 200 and len(j["items"]) == 1
        assert j["items"][0]["id"] == "st-1"

        st, j = get("/api/items?state=bogus")
        assert st == 400 and "error" in j

        # Archived items: hidden by default, visible on demand
        st, j = get("/api/items?project=dressit-fshorts&limit=50")
        assert st == 200 and all(i["id"] != "st-nomedia" for i in j["items"])
        st, j = get("/api/items?project=dressit-fshorts&include_archived=1")
        assert st == 200 and any(i["id"] == "st-nomedia" for i in j["items"])
        st, j = get("/api/items?project=dressit-fshorts&state=archived")
        assert st == 200 and [i["id"] for i in j["items"]] == ["st-nomedia"]
        st, j = get("/api/overview")
        assert st == 200 and all(
            "archived" not in (p["states"] or {}) for p in j["projects"])
        st, j = get("/api/overview?include_archived=1")
        assert st == 200
        fs = [p for p in j["projects"]
              if p["alias"] == "dressit-fshorts"][0]
        assert fs["states"].get("archived") == 1, fs
        # restore path: archived -> review
        st, j = post("/api/items/st-nomedia/transition", {"to": "review"})
        assert st == 200 and j["item"]["state"] == "review", j
        st, j = post("/api/items/st-nomedia/transition", {"to": "archived"})
        assert st == 200 and j["item"]["state"] == "archived", j

        # Transition via API: review -> approved
        st, j = post("/api/items/st-1/transition", {"to": "approved"})
        assert st == 200 and j["item"]["state"] == "approved", j

        # Check-duplicate endpoint tests
        st, j = post("/api/check-duplicate", {})
        assert st == 400 and "error" in j

        st, j = post("/api/check-duplicate", {
            "text": "selftest clip",
            "source_ref": "job1"
        })
        assert st == 200 and "is_duplicate" in j

        # Deduplication transition hook:
        # Create a duplicate item st-dup with matching fingerprint in DB
        c_test = db.get_db()
        db.upsert_item(c_test, "st-dup", channel_id=cid, title="selftest clip", source_ref="job1")
        db.transition_item(c_test, "st-dup", "review")
        fake_v = {"duration": 10.0, "k10": "1111111111111111", "k50": "1111111111111111", "k90": "1111111111111111", "composite": "1111111111111111:1111111111111111:1111111111111111"}
        db.upsert_content_fingerprint(c_test, "st-1", video_hash=fake_v["composite"], video_meta=fake_v, text_clean="selftest clip")
        db.upsert_content_fingerprint(c_test, "st-dup", video_hash=fake_v["composite"], video_meta=fake_v, text_clean="selftest clip")
        c_test.close()

        # Attempt transition st-dup -> approved (must return 409 Conflict)
        st, j = post("/api/items/st-dup/transition", {"to": "approved"})
        assert st == 409 and j.get("error") == "Duplicate content detected", (st, j)
        assert j.get("duplicate_of") == "st-1", j

        # Transition st-dup -> approved with ?force=1 override (must return 200 OK)
        st, j = post("/api/items/st-dup/transition?force=1", {"to": "approved"})
        assert st == 200 and j["item"]["state"] == "approved", (st, j)

        # Illegal transition must 400
        st, j = post("/api/items/st-1/transition", {"to": "review"})
        assert st == 400 and "error" in j

        # Unknown item must 404
        st, j = post("/api/items/nope/transition", {"to": "approved"})
        assert st == 404

        # Channel registration
        st, j = post("/api/channels", {
            "project_alias": "dressit-fshorts", "platform": "tiktok",
            "handle": "@resh", "daily_limit": 1,
            "publish_times_utc": ["10:00"]})
        assert st == 200 and j["channel"]["platform"] == "tiktok", j

        # Unknown project must 404
        st, j = post("/api/channels", {"project_alias": "ghost",
                                       "platform": "tiktok"})
        assert st == 404

        # Sync endpoint: tolerate missing adapters script (502) and, when
        # the ADAPTERS agent has installed it, a successful JSON run (200).
        st, j = post("/api/sync")
        if os.path.exists(SYNC_SCRIPT):
            assert st == 200 and isinstance(j, dict) and "error" not in j, \
                (st, j)
        else:
            assert st == 502 and "error" in j, (st, j)

        # 404 shape
        st, j = get("/api/nonexistent")
        assert st == 404 and "error" in j

        # Schedule API (fshorts#4): fallback schedule derived from
        # channels.publish_times_utc, deterministic across calls
        if scheduler_mod is not None:
            st, j = get("/api/schedule?days=7")
            assert st == 200 and isinstance(j["days"], list) and j["days"], \
                (st, j)
            flat = [e for d in j["days"] for e in d["entries"]]
            assert {e["project"] for e in flat} >= {
                "dressit-fshorts", "dressit-shorts"}, flat[:3]
            assert all(e["status"] in ("free", "scheduled",
                                       "publish_window", "published")
                       for e in flat)
            # st-1 was approved above and has no scheduled_at -> unbound
            assert any(u["id"] == "st-1" for u in j["unbound"]), j["unbound"]
            st2, j2 = get("/api/schedule?days=7")
            assert st2 == 200
            assert [e["time_utc"] for d in j2["days"] for e in d["entries"]] \
                == [e["time_utc"] for d in j["days"] for e in d["entries"]]
            st, j = get("/api/schedule?days=99")  # capped, still valid
            assert st == 200
            # fshorts#6: project filter used by /p/<alias> schedule tab
            st, j = get("/api/schedule?days=7&project=dressit-fshorts")
            assert st == 200
            flat = [e for d in j["days"] for e in d["entries"]]
            assert flat and all(e["project"] == "dressit-fshorts"
                                for e in flat), flat[:3]
            assert all(u["project"] == "dressit-fshorts"
                       for u in j["unbound"]), j["unbound"][:3]

        # Runs API: insert then list, newest first, project filter
        st, j = post("/api/runs", {
            "project_alias": "dressit-fshorts", "pipeline": "fshorts",
            "status": "ok", "summary": "rendered 2 jobs",
            "log": "line1\nline2", "started_at": "2026-09-02T10:00:00Z",
            "finished_at": "2026-09-02T10:05:00Z"})
        assert st == 200 and isinstance(j.get("id"), int), (st, j)
        run1 = j["id"]
        st, j = post("/api/runs", {"project_alias": "dressit-fshorts",
                                   "pipeline": "fshorts",
                                   "status": "running"})
        assert st == 200 and j["id"] > run1, (st, j)
        st, j = get("/api/runs?project=dressit-fshorts&limit=10")
        assert st == 200 and len(j["runs"]) == 2, (st, j)
        assert j["runs"][0]["status"] == "running"  # newest first
        assert j["runs"][1]["id"] == run1
        assert j["runs"][1]["summary"] == "rendered 2 jobs"
        assert j["runs"][1]["log"] == "line1\nline2"
        st, j = get("/api/runs?project=dressit-shorts")
        assert st == 200 and j["runs"] == [], (st, j)
        st, j = post("/api/runs", {"project_alias": "dressit-fshorts"})
        assert st == 400 and "error" in j, (st, j)  # missing pipeline
        st, j = post("/api/runs", {"project_alias": "dressit-fshorts",
                                   "pipeline": "fshorts", "status": "bogus"})
        assert st == 400 and "error" in j, (st, j)

        # Media: full 200, range 206, out-of-range 416, missing 404
        st, h, b = get_raw("/media/st-media")
        assert st == 200 and b == video, (st, len(b))
        assert h.get("Accept-Ranges") == "bytes", h
        assert h.get("Content-Type") == "video/mp4", h
        st, h, b = get_raw("/media/st-media", {"Range": "bytes=0-9"})
        assert st == 206 and b == video[:10], (st, len(b))
        assert h.get("Content-Range") == "bytes 0-9/%d" % len(video), h
        st, h, b = get_raw("/media/st-media", {"Range": "bytes=1000-"})
        assert st == 206 and b == video[1000:], (st, len(b))
        st, h, b = get_raw("/media/st-media", {"Range": "bytes=-4"})
        assert st == 206 and b == video[-4:], (st, b)
        st, h, b = get_raw("/media/st-media", {"Range": "bytes=99999-"})
        assert st == 416, (st, b)
        st, h, b = get_raw("/media/st-nomedia")
        assert st == 404 and b"error" in b, (st, b)
        st, h, b = get_raw("/media/ghost-item")
        assert st == 404 and b"error" in b, (st, b)
        st, h, b = get_raw("/media/..%2F..%2Fetc%2Fpasswd")
        assert st == 404, (st, b)  # traversal attempt: unknown item id

        # Media: X-Accel-Redirect when proxied
        st, h, b = get_raw("/media/st-media", {"X-Forwarded-For": "127.0.0.1"})
        assert st == 200 and b == b"", (st, b)
        assert h.get("X-Accel-Redirect") == "/_internal_media" + os.path.realpath(os.path.join(media_dir, "final.mp4")), h
        assert h.get("Content-Type") == "video/mp4", h
        assert h.get("Content-Disposition") == "inline", h

        # Media: HEAD request (direct)
        req_head = urllib.request.Request(base + "/media/st-media", method="HEAD")
        with urllib.request.urlopen(req_head) as r:
            assert r.status == 200 and r.read() == b""
            assert r.headers.get("Content-Length") == str(len(video)), dict(r.headers)
            assert r.headers.get("Content-Type") == "video/mp4"

        # Media: HEAD request (proxied)
        req_head_proxied = urllib.request.Request(base + "/media/st-media",
                                                  headers={"X-Real-IP": "127.0.0.1"},
                                                  method="HEAD")
        with urllib.request.urlopen(req_head_proxied) as r:
            assert r.status == 200 and r.read() == b""
            assert r.headers.get("X-Accel-Redirect") == "/_internal_media" + os.path.realpath(os.path.join(media_dir, "final.mp4"))

        # Delegate: unknown project -> 404, kind without adapter -> 501
        conn = db.get_db()
        db.upsert_project(conn, "ghost-proj", name="ghost",
                          pipeline_kind="ghost-kind")
        conn.close()
        st, j = post("/api/delegate", {"project_alias": "nope",
                                       "title": "t"})
        assert st == 404 and "error" in j, (st, j)
        st, j = post("/api/delegate", {"project_alias": "ghost-proj",
                                       "title": "t", "notes": "n"})
        assert st == 501 and j["error"] == "project has no enqueue", (st, j)
        st, j = post("/api/delegate", {"project_alias": "ghost-proj"})
        assert st == 400 and "error" in j, (st, j)  # missing title
    finally:
        srv.shutdown()
        srv.server_close()

    print("server.py selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
