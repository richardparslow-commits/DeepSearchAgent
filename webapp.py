"""Flask web app that runs the agent live from the browser.

Single page: type an issue, submit, and the app runs
``va_legal_agent.agent.analyze_cases_for_claim`` in a background thread,
polling the status until the analysis is ready and rendering it as text
(the app's own text renderer).

Production hardening:
- Served by **waitress** (pure-Python WSGI) instead of the Flask dev server
  when available; falls back to the dev server with a warning.
- **Job persistence**: every job is written atomically to
  ``WEBAPP_STATE_DIR`` (default ``.webapp/`` next to this file), so completed
  runs survive restarts. A job left in ``running`` at startup is marked
  ``error`` ("interrupted by server restart") — orphaned threads cannot
  survive a process restart.
- **Concurrent-run cap**: at most ``WEBAPP_MAX_CONCURRENT`` (default 2)
  analysis jobs run at once; extra submissions get HTTP 429.
- **Text or JSON output**: every run stores both renderings; a Text/JSON
  toggle on the page switches the display, and ``POST /api/analyze`` accepts
  the same request as JSON (``{"issue": ..., "max_results": ...,
  "max_wall_seconds": ...}``), returning ``{"run_id": ...}`` for polling via
  ``GET /api/status/<job_id>`` (the job carries ``result`` and
  ``result_json``).

Run it from the repo root with the venv:

    pip install -e ".[web]"      # flask + waitress
    PORT=8080 python -m webapp   # http://127.0.0.1:8080/

This module intentionally lives at the repo root, OUTSIDE the
``va_legal_agent`` package, so the package's 100%-coverage and mutation
gates are unaffected. Runs consume the same real quota as the CLI (search
providers, LLM key).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from va_legal_agent.__main__ import render_analysis
from va_legal_agent.agent import analyze_cases_for_claim
from va_legal_agent.config import get_settings

_APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("WEBAPP_STATE_DIR", str(_APP_DIR / ".webapp")))
_MAX_JOBS = 50
_RUNS_LIMIT = 20
_MAX_CONCURRENT = int(os.environ.get("WEBAPP_MAX_CONCURRENT", "2"))

_JOBS: dict[str, dict[str, object]] = {}
_LOCK = threading.Lock()
# One permit per analysis slot. Acquired (non-blocking) at submit time,
# released by the job thread when the run finishes — so the cap is exact and
# race-free across concurrent requests.
_SEM = threading.BoundedSemaphore(_MAX_CONCURRENT)

app = Flask(__name__)


def _now_iso() -> str:
    # Millisecond precision so the recent-runs list orders sub-second submissions.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _job_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def _save_job(job: dict[str, object]) -> None:
    """Atomically persist one job's state to disk.

    Caller must hold ``_LOCK`` (like all ``_JOBS`` mutations).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(job, fh)
        os.replace(tmp, _job_path(str(job["job_id"])))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _prune()


def _prune() -> None:
    """Drop the oldest jobs once the cap is hit (memory and disk)."""
    while len(_JOBS) > _MAX_JOBS:
        oldest = min(_JOBS, key=lambda jid: _JOBS[jid].get("started_at") or "")
        _JOBS.pop(oldest, None)
        try:
            _job_path(oldest).unlink()
        except OSError:
            pass


def _load_jobs() -> None:
    """Load persisted jobs at startup.

    Jobs found in ``running`` state were orphaned by a restart — their thread
    cannot survive the process, so they are marked ``error`` with an explicit
    "interrupted by server restart" message (and the correction is persisted).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") == "running":
            job["status"] = "error"
            job["error"] = "interrupted by server restart"
            job["finished_at"] = _now_iso()
        if job.get("job_id"):
            _JOBS[job["job_id"]] = job
    _prune()


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _run_job(
    job_id: str,
    issue: str,
    max_results: int,
    max_wall_seconds: float | None,
) -> None:
    """Execute one analysis in a background thread and record the outcome."""
    try:
        analysis = analyze_cases_for_claim(
            issue,
            max_results=max_results,
            max_wall_seconds=max_wall_seconds,
        )
        result = render_analysis(analysis, "text")
        result_json = analysis.model_dump(mode="json")
        with _LOCK:
            job = _JOBS[job_id]
            job.update(
                {
                    "status": "done",
                    "result": result,
                    "result_json": result_json,
                    "finished_at": _now_iso(),
                }
            )
            _save_job(job)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the page
        with _LOCK:
            job = _JOBS[job_id]
            job.update(
                {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": _now_iso(),
                }
            )
            _save_job(job)
    finally:
        _SEM.release()


@app.get("/")
def index() -> str:
    return render_template_string(_INDEX_HTML)


def _start_job(
    issue: str | None,
    max_results_raw: object,
    max_wall_raw: object,
) -> tuple[dict[str, object], int]:
    """Validate inputs and start a background analysis job.

    Shared by the form (``/analyze``) and JSON (``/api/analyze``) entry
    points; returns ``(payload, status_code)``. The concurrency permit is
    acquired here and released by the job thread, so the cap is exact even
    across both endpoints.
    """
    issue = (issue or "").strip()
    if not issue:
        return {"error": "issue is required"}, 400

    max_results = _int_or_none(max_results_raw)
    if max_results is None:
        max_results = get_settings().search_max_results
    if max_results < 1:
        return {"error": "max_results must be >= 1"}, 400
    max_wall_seconds = _float_or_none(max_wall_raw)

    if not _SEM.acquire(blocking=False):
        return (
            {
                "error": (
                    f"server busy: {_MAX_CONCURRENT} analysis run(s) already in "
                    "progress — try again when one finishes"
                )
            },
            429,
        )

    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "issue": issue,
            "status": "running",
            "started_at": _now_iso(),
        }
        _save_job(_JOBS[job_id])

    try:
        threading.Thread(
            target=_run_job,
            args=(job_id, issue, max_results, max_wall_seconds),
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 - never leak a concurrency permit
        _SEM.release()
        with _LOCK:
            _JOBS.pop(job_id, None)
            try:
                _job_path(job_id).unlink()
            except OSError:
                pass
        return {"error": "could not start the run"}, 500
    return {"run_id": job_id}, 200


@app.post("/analyze")
def analyze():
    return _start_job(
        request.form.get("issue"),
        request.form.get("max_results"),
        request.form.get("max_wall_seconds"),
    )


@app.post("/api/analyze")
def api_analyze():
    """JSON entry point: ``{"issue": ..., "max_results": N, "max_wall_seconds": N}``."""
    data = request.get_json(silent=True) or {}
    return _start_job(
        data.get("issue"),
        data.get("max_results"),
        data.get("max_wall_seconds"),
    )


@app.get("/api/status/<job_id>")
def status(job_id: str):
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown run"}), 404
    return jsonify(job)


@app.get("/api/runs")
def runs():
    """Recent jobs, newest first (drives the page's Recent runs list)."""
    with _LOCK:
        items = [
            {
                "job_id": job.get("job_id"),
                "issue": job.get("issue"),
                "status": job.get("status"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "error": job.get("error"),
            }
            for job in _JOBS.values()
        ]
    items.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return jsonify(items[:_RUNS_LIMIT])


def _serve(host: str, port: int) -> None:
    try:
        from waitress import serve
    except ImportError:
        print(
            "warning: waitress not installed — using the Flask dev server "
            "(pip install -e \".[web]\" for production serving)",
            file=sys.stderr,
        )
        app.run(host=host, port=port, threaded=True, use_reloader=False)
        return
    print(
        f"Serving on http://{host}:{port} (waitress, "
        f"max {_MAX_CONCURRENT} concurrent analysis job(s))"
    )
    serve(app, host=host, port=port, threads=8)


# Load any persisted jobs before serving requests (including under the Flask
# test client, so tests see the same restart behavior as production).
_load_jobs()


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSearchAgent — live</title>
<style>
  :root { --bg:#0f1419; --panel:#161d26; --panel2:#1c2530; --border:#263142;
          --text:#d7dee8; --muted:#8b98a9; --accent:#58a6ff; --green:#3fb950;
          --red:#f85149; --mono:"SF Mono",Menlo,Consolas,monospace; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:900px; margin:0 auto; padding:32px 24px 80px; }
  h1 { margin:0 0 4px; font-size:24px; } h1 .a { color:var(--accent); }
  .sub { color:var(--muted); margin:0 0 22px; }
  .card { background:var(--panel); border:1px solid var(--border);
          border-radius:10px; padding:16px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }
  input[type=text], textarea, input[type=number] { width:100%; padding:9px 11px;
          background:var(--panel2); border:1px solid var(--border); border-radius:7px;
          color:var(--text); font-size:14px; font-family:var(--mono); }
  textarea { resize:vertical; min-height:64px; }
  .row { display:flex; gap:12px; } .row > div { flex:1; }
  button { margin-top:16px; padding:10px 22px; border:0; border-radius:7px;
          background:var(--accent); color:#06131f; font-weight:600; font-size:14px;
          cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
  pre { font-family:var(--mono); font-size:12.5px; line-height:1.5; background:var(--panel);
        border:1px solid var(--border); border-radius:8px; padding:14px 16px;
        overflow-x:auto; white-space:pre; }
  #status { color:var(--muted); font-size:13px; margin:10px 0; }
  #status.running { color:var(--accent); } #status.error { color:var(--red); }
  .runs-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .run-row { display:flex; align-items:center; gap:10px; padding:7px 10px; border-radius:6px;
             cursor:pointer; border:1px solid transparent; }
  .run-row:hover { background:var(--panel2); border-color:var(--border); }
  .run-issue { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
  .run-badge { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); }
  .run-badge.done { color:var(--green); border-color:#2c5138; background:#14251b; }
  .run-badge.running { color:var(--accent); }
  .run-badge.error { color:var(--red); }
  .run-time { font-size:11px; color:var(--muted); font-family:var(--mono); }
  .empty { color:var(--muted); font-size:13px; padding:6px 0; }
  button.secondary { background:var(--panel2); color:var(--text); border:1px solid var(--border);
                     margin-top:0; padding:4px 12px; font-size:12px; font-weight:500; }
  .note { color:var(--muted); font-size:12.5px; margin-top:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>DeepSearchAgent <span class="a">— live</span></h1>
  <p class="sub">Run a veterans-compensation legal research analysis straight from the browser.
  Runs consume real provider quota and the configured LLM key. At most 2 runs execute at once;
  finished runs are persisted and survive server restarts. Pick text or JSON output below.</p>

  <div class="card">
    <form id="form">
      <label for="issue">Issue to research</label>
      <textarea id="issue" name="issue" placeholder="e.g. service connection for tinnitus"></textarea>
      <div class="row">
        <div>
          <label for="max_results">Max results</label>
          <input id="max_results" name="max_results" type="number" min="1" value="5">
        </div>
        <div>
          <label for="max_wall_seconds">Max wall time (seconds, optional)</label>
          <input id="max_wall_seconds" name="max_wall_seconds" type="number" min="1"
                 placeholder="blank = unlimited">
        </div>
      </div>
      <label>Output format</label>
      <div class="row" style="gap:18px">
        <label style="margin:0"><input type="radio" name="format" value="text" checked> Text</label>
        <label style="margin:0"><input type="radio" name="format" value="json"> JSON</label>
      </div>
      <button id="submit" type="submit">Run analysis</button>
    </form>
    <div id="status"></div>
  </div>

  <div class="card">
    <div class="runs-head">
      <div style="font-size:15px;color:var(--accent);font-weight:600">Recent runs</div>
      <button type="button" id="refresh" class="secondary">Refresh</button>
    </div>
    <div id="runs"><div class="empty">Loading…</div></div>
  </div>

  <pre id="result" hidden></pre>
  <p class="note">Research support derived from public decisions, not legal advice.
  Status updates and the final report appear above; the page polls every 2 seconds.</p>
</div>
<script>
  const form = document.getElementById('form');
  const statusEl = document.getElementById('status');
  const resultEl = document.getElementById('result');
  const btn = document.getElementById('submit');

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    btn.disabled = true;
    statusEl.className = 'running';
    statusEl.textContent = 'Starting run\u2026';
    resultEl.hidden = true;
    let res;
    try {
      res = await fetch('/analyze', { method: 'POST', body: new FormData(form) });
    } catch (e) {
      statusEl.textContent = 'Request failed: ' + e;
      btn.disabled = false;
      return;
    }
    const data = await res.json();
    if (!res.ok) {
      statusEl.className = 'error';
      statusEl.textContent = data.error || ('HTTP ' + res.status);
      btn.disabled = false;
      return;
    }
    loadRuns();
    poll(data.run_id);
  });

  function selectedFormat() {
    const el = document.querySelector('input[name="format"]:checked');
    return el ? el.value : 'text';
  }

  async function poll(runId) {
    statusEl.textContent = 'Running\u2026';
    const res = await fetch('/api/status/' + runId);
    const data = await res.json();
    if (data.status === 'running') {
      setTimeout(() => poll(runId), 2000);
      return;
    }
    btn.disabled = false;
    if (data.status === 'done') {
      statusEl.className = '';
      statusEl.textContent = 'Done (' + shortId(runId) + ').';
      if (selectedFormat() === 'json' && data.result_json) {
        resultEl.textContent = JSON.stringify(data.result_json, null, 2);
      } else {
        resultEl.textContent = data.result;
      }
      resultEl.hidden = false;
    } else {
      statusEl.className = 'error';
      statusEl.textContent = 'Run failed: ' + (data.error || 'unknown error');
    }
    loadRuns();
  }

  const runsEl = document.getElementById('runs');

  function esc(value) {
    const d = document.createElement('div');
    d.textContent = value == null ? '' : String(value);
    return d.innerHTML;
  }

  function shortId(id) { return (id || '').slice(0, 8); }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  async function loadRuns() {
    try {
      const res = await fetch('/api/runs');
      const jobs = await res.json();
      if (!Array.isArray(jobs) || jobs.length === 0) {
        runsEl.innerHTML = '<div class="empty">No runs yet.</div>';
        return;
      }
      runsEl.innerHTML = jobs.map(j => {
        const badge = j.status === 'done' ? 'done' : (j.status === 'running' ? 'running' : 'error');
        return '<div class="run-row" data-id="' + esc(j.job_id) + '">' +
          '<span class="run-issue">' + esc(j.issue) + '</span>' +
          '<span class="run-badge ' + badge + '">' + badge + '</span>' +
          '<span class="run-time">' + esc(fmtTime(j.started_at)) + '</span>' +
          '</div>';
      }).join('');
    } catch (e) {
      runsEl.innerHTML = '<div class="empty">Could not load runs.</div>';
    }
  }

  runsEl.addEventListener('click', (ev) => {
    const row = ev.target.closest('.run-row');
    if (row) poll(row.dataset.id);
  });

  document.getElementById('refresh').addEventListener('click', loadRuns);
  loadRuns();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    _serve("127.0.0.1", port)
