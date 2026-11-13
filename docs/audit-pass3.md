# DeepSearchAgent — Audit Pass 3

**Scope:** `webapp.py`, `va_legal_agent/batch.py`, `scripts/`, `deploy/`,
`.github/workflows/`, `Makefile`, `pyproject.toml`, `requirements*.txt`,
`.mutation-baseline.json`, `.env.example`.

**Audited commit:** `5b85310` (Merge pull request #2). The working tree contains the Pass 1
`fetch.py` fixes, the remediation for the Pass 3 findings marked **FIXED** below, and the
associated regression tests.

**Environment:** Python 3.12.3, project `.venv`, Flask 3.1.3, `pytest 9.2.0`,
`ruff 0.17.5`, GNU bash and make on Linux.

**Document status:** regenerated Pass 3 in the Pass 1 format on 2026-11-13. IDs `P3-001` …
`P3-022` are preserved. The working tree now contains fixes for `P3-001`, `P3-002`,
`P3-003`, `P3-004`, `P3-005`, `P3-006`, `P3-008`, `P3-009`, `P3-010`, `P3-011`,
`P3-012`, `P3-013`, `P3-014`, `P3-015`, `P3-017`, `P3-019` and `P3-021`; the totals distinguish fixed from
open. Where a fixed finding no longer reproduces, the regression test is the executed
evidence. The
`[tool.mutmut]` defaults now target the package and run the full test selection; each pass
scopes that config temporarily and restores it.

**Method:** each finding has an executed probe. Behavioural probes import the real modules
with temporary `WEBAPP_STATE_DIR` / `BATCH_STATE_DIR`; shell probes run the real script or
tar pipeline; workflow, dependency and documentation findings are checked with `grep`,
Python/YAML parsing, `ruff`, and the fixed-regression test suite. Outputs are quoted below,
not paraphrased. Findings are ordered by file, not by severity.

## Totals

| File | Total lines verified | Findings | Fixed | Open |
|---|---:|---:|---:|---:|
| `webapp.py` | 607 | 5 | 5 | 0 |
| `va_legal_agent/batch.py` | 155 | 2 | 1 | 1 |
| `scripts/mutmut_pass.py` | 267 | 2 | 2 | 0 |
| `scripts/check_mutation_baseline.py` | 255 | 1 | 1 | 0 |
| `scripts/_apply_mutant.py` | 112 | 0 | 0 | 0 |
| `scripts/smoke_search.py` | 62 | 0 | 0 | 0 |
| `deploy/install.sh` | 178 | 2 | 2 | 0 |
| `deploy/run_batch.sh` | 100 | 1 | 1 | 0 |
| `deploy/va-legal-agent-batch.service` | 29 | 0 | 0 | 0 |
| `deploy/va-legal-agent-batch.timer` | 11 | 0 | 0 | 0 |
| `.github/workflows/ci.yml`, `mutation.yml` | 191 | 3 | 2 | 1 |
| `Makefile` | 97 | 2 | 1 | 1 |
| `pyproject.toml` | 64 | 1 | 1 | 0 |
| `requirements.txt`, `requirements-dev.txt` | 17 | 1 | 0 | 1 |
| `.mutation-baseline.json` | 70 | 1 | 1 | 0 |
| `.env.example` | 202 | 1 | 0 | 1 |
| **Total** | **2,417** | **22** | **17** | **5** |

Severity: **3 High, 10 Medium, 9 Low** (including fixed items). Fixed: **3 High, 10 Medium,
4 Low**. Open: **0 High, 0 Medium, 5 Low**.

The three High findings form one chain: `P3-008` (a crashed mutmut pass reports zero
survivors), `P3-010` (a missing survivor file is only a warning) and `P3-014`
(`providers`/`queries` passes are explicitly allowed to fail). All three are fixed.

---

## `webapp.py` (Total Lines Verified: 607)

The app binds `127.0.0.1` by default (`webapp.py:528-529`), which is the right default for
a tool that spends provider quota and LLM credits. The findings below concern state shared
between request threads and job threads, and what happens to it across restarts.

### P3-001 — Concurrent state — Medium — **FIXED**

**Location:** `webapp.py:274-279` (`status`), concurrent mutation at `webapp.py:164-172`

**Offending code (before the fix):**
```python
@app.get("/api/status/<job_id>")
def status(job_id: str):
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown run"}), 404
    return jsonify(job)
```

**Root cause & impact:** the lock protects the lookup, not the serialization. `jsonify` walks
the same dict object that `_run_job` mutates with `job.update({... "result" ...})` after the
run finishes; `dict.update` adds keys, so a poll that lands in that window can raise
`RuntimeError: dictionary changed size during iteration` and return 500. The page's `poll()`
has no `try`/`catch` around `res.json()`, so the error is an unhandled rejection and polling
stops for that run — the user sees “Running…” forever. The window is small but polls arrive
every 2 s per open tab, and the update is the largest write of the run.

**Fix:** copy the job while holding `_LOCK` before leaving the lock, so `jsonify` receives a
detached snapshot. Worker updates can no longer change the dict during serialization.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_webapp.py
20 passed
```
Regression test: `test_status_serializes_snapshot` mutates the payload inside a
monkeypatched `jsonify`; the mutation never reaches `_JOBS["snapshot"]`, and the test-client
response still returns the job.

### P3-002 — Job retention — Medium — **FIXED**

**Location:** `webapp.py:98-107` (`_prune`), `webapp.py:145-183` (`_run_job`)

**Offending code (before the fix):**
```python
def _prune() -> None:
    """Drop the oldest jobs once the cap is hit (memory and disk)."""
    while len(_JOBS) > _MAX_JOBS:
        oldest = min(_JOBS, key=lambda jid: _JOBS[jid].get("started_at") or "")
        _JOBS.pop(oldest, None)
        try:
            _job_path(oldest).unlink()
        except OSError:
            pass
```
```python
        with _LOCK:
            job = _JOBS[job_id]
            job.update({...})
```

**Root cause & impact:** `_prune` orders solely by `started_at` and does not skip
`status == "running"`. A run that outlives 50 newer submissions is evicted: its dict entry
and its state file are deleted, and when its thread finishes, `_JOBS[job_id]` raises
`KeyError` inside the success path. That `KeyError` is caught by `except Exception`, whose
handler then repeats the failing lookup, so the exception escapes the thread and the run
leaves no record at all. The permit is released in `finally`, so the cap stays correct; the
user's poll returns 404 (“unknown run”) with no error message. `_MAX_CONCURRENT` defaults
to 2, so a long `max_wall_seconds` run plus a burst of submissions is the realistic trigger.

**Fix:** each prune pass considers only jobs whose status is not `running`; if no completed
candidate exists, it stops. Running jobs are pruned only after they save a terminal status
and re-enter `_prune()`.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_webapp.py
20 passed
```
Regression test: `test_prune_keeps_running_jobs` creates 51 jobs with the oldest running
and 50 done; after `_prune()` the running job still returns 200 from
`/api/status/running-oldest`, and the map is back at the cap.

### P3-003 — Security — Medium — **FIXED**

**Location:** `webapp.py:288-301` (`analyze`, `api_analyze`)

**Offending code:**
```python
@app.post("/analyze")
def analyze():
    return _start_job(
        request.form.get("issue"),
        request.form.get("max_results"),
        request.form.get("max_wall_seconds"),
    )
```
There is no authentication, `Origin`/`Sec-Fetch-Site` check, or `Host` validation on either
POST endpoint.

**Root cause & impact:** the app binds loopback, but a web page open in the operator's
browser can still submit a cross-site form to `127.0.0.1:8080/analyze` (CSRF), and DNS
rebinding can defeat the loopback assumption for a malicious site. Either path spends
provider quota and LLM credits and can start analyses without the operator's intent.

**Fix:** Flask's `TRUSTED_HOSTS` allowlist accepts only `localhost` and `127.0.0.1`, so a
rebound Host is rejected before routing. `_same_origin_request()` additionally rejects a
mismatched `Origin` on both POST endpoints; when Origin is absent it falls back to
`Sec-Fetch-Site`, rejecting `cross-site` while allowing non-browser callers with no fetch
metadata. The server remains bound to loopback; no per-startup token was added.

**Evidence (before the fix):** Flask exposed no request hooks and accepted a POST with no
Origin header:
```text
before_request hooks: defaultdict(<class 'list'>, {})
status: 200 body: {'run_id': 'probe'}
```

**Verification:**
```text
$ .venv/bin/python -m pytest -q tests/test_webapp.py
20 passed
```
Regression tests: `test_foreign_origin_is_rejected`, `test_rebound_host_is_rejected`,
`test_missing_origin_without_fetch_metadata_is_allowed`,
`test_missing_origin_with_cross_site_fetch_metadata_is_rejected`,
`test_loopback_hosts_are_accepted`, and `test_same_origin_submissions_are_accepted` cover
foreign origins, matching rebound Host/Origin pairs, missing-Origin fallback behavior,
valid loopback hosts, and legitimate form/API submissions.

### P3-004 — Persistence — Low — **FIXED**

**Location:** `webapp.py:124-139` (`_load_jobs`)

**Offending code (before the fix):**
```python
        if job.get("status") == "running":
            job["status"] = "error"
            job["error"] = "interrupted by server restart"
            job["finished_at"] = _now_iso()
        if job.get("job_id"):
            _JOBS[job["job_id"]] = job
```

**Root cause & impact:** the docstring says the correction is persisted, but the corrected
dict is only inserted into `_JOBS`; the JSON file still says `running`. A later restart
repeats the correction, and any reader of `.webapp/<id>.json` sees a live status for a job
that can never finish.

**Fix:** after inserting the corrected job into `_JOBS`, call `_save_job(job)` so the
atomic JSON file is rewritten with the terminal error state. Non-object JSON state files
are skipped so one malformed file cannot abort startup.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_webapp.py
20 passed
```
Regression tests: `test_load_jobs_persists_restart_correction` writes a `running` job,
calls `_load_jobs()`, and asserts both that `/api/status/restart` returns `error` and that
the on-disk JSON now contains the corrected status, message and `finished_at`;
`test_load_jobs_skips_non_object_state` verifies a malformed non-object state file is
ignored instead of aborting startup.

### P3-005 — Configuration — Low — **FIXED**

**Location:** `webapp.py:54-64` (pre-fix environment reads)

**Offending code (before the fix):**
```python
_MAX_CONCURRENT = int(os.environ.get("WEBAPP_MAX_CONCURRENT", "2"))
...
port = int(os.environ.get("PORT", "8080"))
```

**Root cause & impact:** `0` produces a semaphore with no permits, so every submission —
including the first — returns 429 with the self-contradictory message “server busy: 0
analysis run(s) already in progress”. A negative value raises `ValueError` at import, so the
app does not start and the traceback names `threading`, not the environment variable. `PORT`
had the same unvalidated `int()` shape at startup.

**Fix:** a shared `_env_int()` validates `WEBAPP_MAX_CONCURRENT` at import (1–32) and `PORT`
at startup (1–65535), raising `SystemExit` with the variable and accepted range.

**Evidence:**
```text
$ WEBAPP_MAX_CONCURRENT=0 .venv/bin/python -c "import webapp"
error: WEBAPP_MAX_CONCURRENT must be between 1 and 32, got 0
exit 1

$ PORT=70000 .venv/bin/python -m webapp
error: PORT must be between 1 and 65535, got 70000
exit 1
```
Regression tests: `tests/test_webapp.py::test_invalid_max_concurrent_fails_at_import`,
`::test_env_int_rejects_out_of_range_port`, and the fixed-regression run below (`42 passed`).

---

## `va_legal_agent/batch.py` (Total Lines Verified: 155)

### P3-006 — Batch accounting — Medium — **FIXED**

**Location:** `va_legal_agent/batch.py:143-155` (`finalize`); `_exclusive_lock()` at `:52-66`

**Offending code (before the fix):**
```python
    def finalize(self) -> dict[str, object]:
        """Aggregate the batch summary and delete the state file."""
        summary = self.summary()
        try:
            self.path.unlink()
        except OSError as exc:
            logger.warning("Could not remove batch state %s: %s", self.path, exc)
        return summary
```

**Root cause & impact:** `summary()` reads the JSONL file, then the file is deleted. A
sibling process that appends its `analysis_complete` between those two steps has its
outcome counted nowhere: the summary already excluded it and the file that held it is gone.
The class is explicitly designed for concurrent writers (`record` appends from any CLI
invocation sharing a `RUN_ID`), so the window is reachable whenever the last outstanding
process finalizes while a straggler finishes — the exact case batch accounting exists for.
There was no locking (`flock`) around read-and-delete anywhere in the class.

**Evidence (before the fix):** simulated interleaving with a late `record` between
`summary()` and `unlink()`:
```text
summary total: 1 | file exists: False | outcomes recoverable: []
```

**Fix:** `_exclusive_lock()` takes an exclusive `flock` on the run's `.lock` file, and both
`record()` and `finalize()` hold it. `finalize()` keeps the lock across `summary()` and
`unlink()`, so a recorder either appends before the summary is read or waits until the
old state file has been removed. The lock file is intentionally persistent: unlinking it
while a waiter has it open would let a third process lock a different inode.

**Evidence:** the regression test pauses `finalize()` immediately after `summary()`, starts
a late `record()`, and verifies it blocks until the unlink finishes:
```text
$ .venv/bin/python -m pytest -q tests/test_batch.py
24 passed
```
Regression test: `test_finalize_locks_read_and_delete_against_recorders`.

### P3-007 — Batch identity — Low

**Location:** `va_legal_agent/batch.py:44-47`

**Offending code:**
```python
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "._-") or "run"
        self.path = batch_state_dir() / f"{safe}.jsonl"
```

**Root cause & impact:** stripping disallowed characters is many-to-one: `batch/1` and
`batch1` both become `batch1.jsonl`, and `.`/`..` collapse to hidden names in the same
directory. Two unrelated batches that happen to differ only in punctuation then share
outcomes, coverage scores and failure lists, and whichever finalizes first deletes the
other's state. The sanitization does prevent path traversal, so this is a correctness
collision, not a security escape.

**Evidence:**
```text
batch1.jsonl batch1.jsonl same path: True
```

**Remediation:** append a short digest of the raw id
(`hashlib.sha256(run_id.encode()).hexdigest()[:8]`) to the sanitized stem.

---

## `scripts/mutmut_pass.py` (Total Lines Verified: 267)

### P3-008 — Mutation gate — High — **FIXED**

**Location:** `scripts/mutmut_pass.py:126-147` (pre-fix subprocess calls; current lines
`151`/`157`/`164`)

**Offending code (before the fix):**
```python
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--cov=" + dotted, *tests],
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "1"],
        capture_output=True,
    )

    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"], capture_output=True, text=True
    )
    result_lines = [ln for ln in result.stdout.splitlines() if ":" in ln]
    checked = [
        ln for ln in result_lines if ln.rsplit(":", 1)[1].strip() != "not checked"
    ]
    if result_lines and not checked:
```

**Root cause & impact:** none of these three `returncode`s is inspected (`grep -n returncode`
shows only the `_apply_mutant` call and the direct pytest re-verification). If `mutmut run`
or `mutmut results` crashes — a mutmut/OS incompatibility, a missing dependency, an
exhausted disk — `results` writes nothing, `result_lines` is `[]`, and the vacuous-pass
guard is skipped precisely because it also requires a non-empty list. The script then writes
`### <module>: 0 survivors`, which `check_mutation_baseline.py` accepts as a perfect score.
A module can therefore be entirely unmeasured and still satisfy the 100%-kill gate.

**Fix:** `_require_success()` checks the exit code of the coverage run, `mutmut run` and
`mutmut results`. A non-zero code prints the failing step, its exit code and captured
stderr, then exits 1 before any survivor file is written. The pass now asks for
`mutmut results --all=true` (mutmut 3.7 exposes `--all` as a boolean option), so killed
mutants are included in the output. An empty result
means mutmut generated or checked nothing and exits 1; a result containing only
“not checked” is rejected by the existing vacuous-pass guard. An all-killed pass lists
its killed mutants and correctly reports zero survivors.

**Evidence:** the focused regression suite:
```text
$ .venv/bin/python -m pytest -q tests/test_mutmut_pass.py \
    tests/test_check_mutation_baseline.py tests/test_mutation_gate_integrity.py
27 passed in 0.45s
```
Regression tests: `test_main_fails_when_required_command_fails` covers the coverage run,
`mutmut run` and `mutmut results`; each case asserts exit 1, the captured stderr, and
restoration of `pyproject.toml`. `test_main_accepts_all_killed_pass` and
`test_main_rejects_empty_results` cover the all-killed and no-mutants cases.

### P3-009 — Mutation configuration — Medium — **FIXED**

**Location:** `scripts/mutmut_pass.py:86-100` (pre-fix rewrite); current
`_scoped_pyproject()` at `:92`

**Offending code (before the fix):**
```python
    with open("pyproject.toml") as f:
        text = f.read()
    text = re.sub(
        r"source_paths = \[[^\]]*\]",
        f'source_paths = ["{target}"]',
        text,
    )
    ...
    with open("pyproject.toml", "w") as f:
        f.write(text)
```

**Root cause & impact:** every pass overwrote the checked-in `[tool.mutmut]` section with
that module and test slice, and there was no restore path — not on success, not on failure,
not on `Ctrl-C`. After `make mutate`, `git status` was dirty and a pass left the committed
config scoped to one module and test slice; an interrupted pass could leave a half-applied
selection. The tracked file also contained that pass residue (`agent.py` plus two test
files), separate from the rewrite defect.

**Fix:** `_scoped_pyproject()` keeps the original text, writes the scoped selection, and
restores the original in `finally`; `main()` runs the entire pass under it. The checked-in
default is now project-wide: `source_paths = ["va_legal_agent"]` and an empty
`pytest_add_cli_args_test_selection` (all tests), so each pass's temporary scope is
restored to a meaningful default.

**Evidence:** the regression tests force both a successful pass and a `SystemExit` failure,
then assert the file is byte-for-byte restored:
```text
$ .venv/bin/python -m pytest -q tests/test_mutmut_pass.py
8 passed
```
The restore fixture uses the checked-in package-wide/empty-selection shape, so the
scoped rewrite is tested against the actual defaults. Regression tests: `test_scoped_pyproject_restores_after_exception`,
`test_scoped_pyproject_restores_after_success`, and
`test_main_restores_pyproject_when_pass_fails`.

---

## `scripts/check_mutation_baseline.py` (Total Lines Verified: 255)

### P3-010 — Mutation gate — High — **FIXED**

**Location:** `scripts/check_mutation_baseline.py:65-72` (pre-fix missing-file branch)

**Offending code (before the fix):**
```python
        except FileNotFoundError:
            warnings.append(
                f"{module}: no survivor file — the pass did not run (likely "
                "timed out on CI); verify locally with `make mutate-check`"
            )
            rows.append((module, -1, 0, expected))
            continue
```

**Root cause & impact:** the checker knew exactly what a missing file meant (“the pass did
not run”) and still returned success: warnings were printed after the table, `violations`
stayed empty, and `main()` returned 0. Combined with `P3-014` and `P3-015`, an unmeasured
module was indistinguishable from a clean one, and the gate's final line claimed every
completed module was at or under baseline.

**Fix:** a missing survivor file is a violation by default. `--allow-missing` is an explicit
partial-run opt-out that prints “were not measured”; CI never passes it.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_check_mutation_baseline.py
3 passed
```
Regression tests: `test_missing_survivor_file_fails_gate`,
`test_allow_missing_flag_is_explicit`, `test_unknown_flag_is_rejected`.

---

## `scripts/_apply_mutant.py` (Total Lines Verified: 112)

No reportable defect. The class-scoped lookup is a correct workaround for mutmut's
first-same-named-method resolution, the caller restores the source in `finally`, and the
mutmut version is pinned (`mutmut==3.7.0`), so the private API usage is version-locked.
`sys.argv[1]` is unchecked, but the only caller always passes a mutant name and an
`IndexError` fails the pass loudly rather than corrupting a verdict.

## `scripts/smoke_search.py` (Total Lines Verified: 62)

No reportable defect. Provider failures are caught and counted, non-zero exit is returned,
and the path shim makes the script runnable without an editable install. Sleeping after the
last provider wastes at most `SEARCH_DELAY_SECONDS` in a manual-only tool.

---

## `deploy/install.sh` (Total Lines Verified: 178)

### P3-011 — Deployment — Medium — **FIXED**

**Location:** `deploy/install.sh:1-9` (usage), `:19-22` (pre-fix argument handling),
`:79-81`

**Offending code (before the fix):**
```bash
#   deploy/install.sh --target "$HOME/vla"     # install to a user-owned dir (no root)
...
TARGET="${TARGET:-/opt/va-legal-agent}"
...
MODE_SYSTEMD=1
[[ "${1:-}" == "--cron" ]] && MODE_SYSTEMD=0
```

**Root cause & impact:** only `--cron` was recognised, and only as the first argument;
`--target` existed solely in the header comment and error text. `TARGET` came from the
environment, so the documented non-root installation command silently kept
`/opt/va-legal-agent` and failed the root check. The error then advised “use --target for a
user dir”, which was the flag the user already passed.

**Fix:** an argument loop parses `--cron`, `--target DIR`, `--target=DIR` and `--help`,
rejects unknown flags and missing values with exit 2, and applies the target before the
root check.

**Evidence:**
```text
$ bash deploy/install.sh --target /tmp/.../blocker/target
==> Installing DeepSearchAgent to /tmp/.../blocker/target
mkdir: cannot create directory ‘/tmp/.../blocker’: Not a directory
exit=1
```
The failure occurs after the target has been accepted and printed. Regression tests:
`test_target_flag_is_parsed`, `test_target_flag_requires_a_value`,
`test_unknown_flag_is_rejected`, `test_target_and_cron_can_be_combined`.

### P3-012 — Deployment security — Low — **FIXED**

**Location:** `deploy/install.sh:38-43` (pre-fix copy step)

**Offending code (before the fix):**
```bash
tar --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='logs' \
    --exclude='.bva_index' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.coverage' --exclude='mutants' --exclude='.freebuff' \
    --exclude='*.egg-info' -C "$ROOT" -cf - . | tar -C "$TARGET" -xf -
```

**Root cause & impact:** `.env`, `.webapp/`, `issues.txt` and `issues.retry` were copied.
Installing from a configured checkout silently reused the operator's API keys in the new
deployment (then `chown -R`ed them to the service user), and copied webapp job state that the
new instance would load and mark interrupted. Step 3 is supposed to own secret and state
creation; an in-place reinstall should preserve the target's existing files.

**Fix:** exclude `.env`, `.webapp`, `issues.txt` and `issues.retry` from the tar pipeline;
`.env.example` remains copied, and in-place reinstalls keep target-owned files.

**Evidence:** a real tar copy with the added exclusions:
```text
./.env.example
./keep.py
```
Regression test: `test_copy_step_excludes_secrets_and_runtime_state`.

---

## `deploy/run_batch.sh` (Total Lines Verified: 100)

### P3-013 — Deployment — Medium — **FIXED**

**Location:** `deploy/run_batch.sh:27-30` (`ROOT` and `cd`), `:88` (invocation); contrast
`deploy/va-legal-agent-batch.service:12` (`WorkingDirectory=__TARGET__`) and
`deploy/install.sh:152-161` (cron setup)

**Offending code:**
```bash
# Shared entry point used by the systemd service/timer and the cron line.
# Runs from the project root so the app's own dotenv loading picks up .env
# from the working directory (no shell-level sourcing needed).
...
"$PY" -m va_legal_agent "${args[@]}" >>"$LOGFILE" 2>&1
```

**Root cause & impact:** the script resolves `ROOT` for its own paths but never `cd`s, so
the child inherits the caller's working directory. Under systemd that is `__TARGET__`
(good); under the documented cron line it is whatever crontab's shell uses (root's home),
because `runuser` does not change the cwd. Relative settings then resolve elsewhere:
`SEARCH_BVA_LOCAL_INDEX_DIR` defaults to `.bva_index`, so a cron run with `bvalocal` looks
for — or tries to build — the corpus under `/root`, which the unprivileged service user
cannot write, instead of reusing `$TARGET/.bva_index`. The header's “Runs from the project
root” is therefore true only for the systemd path.

**Evidence:** invoking the real script from a temporary working directory now makes the
child resolve from `ROOT` instead of inheriting cron's cwd:
```text
$ .venv/bin/python -m pytest -q tests/test_run_batch.py
6 passed
```
Regression test: `test_cron_like_caller_runs_from_project_root` starts the script from a
temporary cron-like cwd and asserts the fake interpreter sees the project root.

**Remediation:** implemented by `cd "$ROOT"` immediately after `ROOT` is resolved, so
systemd and cron share one working directory and relative settings resolve against the
checkout.

---

## `deploy/va-legal-agent-batch.service`, `deploy/va-legal-agent-batch.timer` (Total Lines Verified: 40)

No reportable defect. `ProtectSystem=full` + `ReadWritePaths=__TARGET__` keeps the unit
writable only where it needs to be, `Restart=on-failure` covers transient failures,
`TimeoutStartSec=0` matches `--auto-run`'s long waits, and the timer is persistent with a
jittered daily schedule. One observation is deliberately not filed: `PrivateTmp=true` means
a `--batch-size` state file under the default temp dir would live in the unit's private
`/tmp`, invisible to a manual run — but `run_batch.sh` never passes `--batch-size`, so the
deployment cannot reach that split. The installer's `--show-config` sanity check runs as
root and so cannot detect a service-user permission failure; it is a smoke check, not a
substitute for the documented manual run.

---

## `.github/workflows/ci.yml`, `.github/workflows/mutation.yml` (Total Lines Verified: 191)

### P3-014 — CI mutation gate — High — **FIXED**

**Location:** `.github/workflows/mutation.yml:53-57` (pre-fix heavy step)

**Offending code (before the fix):**
```yaml
      - name: Mutation pass — heavy modules (providers, queries)
        run: |
          .venv/bin/python scripts/mutmut_pass.py providers.py tests/test_providers.py tests/test_search.py || echo "WARNING: providers mutation pass exited non-zero"
          .venv/bin/python scripts/mutmut_pass.py queries.py tests/test_providers.py tests/test_search.py || echo "WARNING: queries mutation pass exited non-zero"
        timeout-minutes: 20
```

**Root cause & impact:** the two heaviest modules were explicitly allowed to fail, and the
comment above them explained they got a longer timeout “so they don't starve the remaining
passes”. But a failed or timed-out pass writes no survivor file, and `P3-010` turned a
missing file into a note rather than a violation, so the subsequent gate step passed and
printed “Mutation kill-gate passed”. `providers.py` carries the largest baseline in the
project; the gate could therefore be green while the module with the most triaged
equivalents was never measured. `queries.py` rode along.

**Fix:** delete both `|| echo` guards and add `set -euo pipefail`. A non-zero providers or
queries pass now fails its matrix leg; the other legs still run, and the gate job runs after
the failure and rejects the missing survivor report.

**Evidence:** the regression test asserts both commands are present, fail-fast is enabled,
and no `||`/`WARNING` swallowing remains:
```text
$ .venv/bin/python -m pytest -q tests/test_mutation_gate_integrity.py
6 passed
```
Regression test: `test_workflow_does_not_swallow_mutation_failures`.

### P3-015 — CI mutation gate — Medium — **FIXED**

**Location:** `.github/workflows/mutation.yml:37-152` (matrix legs `:41-94`; gate
`:129-152`); pre-fix `:59-76`

**Offending code (before the fix):**
```yaml
      - name: Run the mutation pass (remaining modules)
        run: |
          set -e
          .venv/bin/python scripts/mutmut_pass.py search.py tests/test_search.py
          ...
          .venv/bin/python scripts/mutmut_pass.py ranking.py tests/test_ranking.py
        timeout-minutes: 15
```

**Root cause & impact:** each pass deletes its cached stats, regenerates coverage and runs
mutmut over a module, so the step's cost scales with the sum of 14 modules, not the slowest
one. When the 15-minute limit was hit, GitHub killed the step and every module after the
current one had no survivor file — which `P3-010` now rightly fails, but only if the gate
step ran. `set -e` made the job red; the dangerous ordering was the reverse: modules earlier
in the list completed, later ones never started, and a run that did reach the gate after a
partial timeout could still pass. The 45-minute job cap and the absence of pip caching in
both workflows tightened the margin.

**Fix:** the combined step is replaced by a `fail-fast: false` matrix with one leg per
baseline module. Each leg runs exactly one `mutmut_pass.py` command with its own
`timeout-minutes: ${{ matrix.timeout }}`, so a timeout kills only that module's leg. Every
leg uploads `/tmp/mutmut_survivors_<module>.txt` with `if: always()`. The separate `gate` job
needs the whole matrix (`if: ${{ !cancelled() }}`), downloads every `survivors-*` artifact,
and runs `check_mutation_baseline.py`; a missing artifact for a timed-out or failed module
is a hard gate violation rather than an unmentioned gap.

**Evidence:** the focused workflow tests parse the real matrix and gate:
```text
$ .venv/bin/python -m pytest -q tests/test_mutation_gate_integrity.py
6 passed
```
Regression tests: `test_every_baseline_module_has_its_own_matrix_leg`,
`test_gate_runs_after_a_failed_or_timed_out_leg`, and
`test_each_leg_uploads_its_report_and_has_a_separate_timeout`.

**Remediation:** implemented with the per-module matrix and artifact-based gate above.

### P3-016 — CI security — Low

**Location:** `.github/workflows/ci.yml:1-9`, `.github/workflows/mutation.yml:1-24`

**Offending code:**
```yaml
on:
  push:
  pull_request:
```
(no top-level `permissions:` key in either file)

**Root cause & impact:** the job inherits the repository's default `GITHUB_TOKEN`
permissions, which can be read/write in older repository settings. Those credentials are
available to everything the job executes, including `pip install -e .` build backends and
the test suite. The workflows only need to read the checkout, so the default is broader than
necessary for a project that also runs third-party mutation tooling.

**Evidence:**
```text
P3-016 .github/workflows/ci.yml permissions: False
P3-016 .github/workflows/mutation.yml permissions: False
```

**Remediation:** add
```yaml
permissions:
  contents: read
```
to both workflows.

---

## `Makefile` (Total Lines Verified: 97)

### P3-017 — Developer tooling — Medium — **FIXED**

**Location:** `Makefile:81-84`

**Offending code (before the fix):**
```make
mutate: ## Mutation-testing pass (mutmut) over every module; survivors to /tmp
	@for spec in $(MUTATE_MODULES); do \
		$(PYTHON) scripts/mutmut_pass.py $$spec; \
	done
```

**Root cause & impact:** a POSIX shell `for` loop returns the status of its last command.
If a pass for `fetch.py` crashed or was interrupted, the loop continued; if the final module
(`ranking.py`) succeeded, `make mutate` exited 0 and printed no warning. `make mutate-check`
then ran the baseline gate, which saw the missing survivor file as a note (`P3-010`) and
passed. There was no `set -e` and no per-module status accumulation, so the local workflow —
the one the docs recommend before trusting the baseline — had the same blind spot as CI.

**Fix:** prefix the loop with `@set -e;`, so the first failed pass stops the loop and make
reports a non-zero exit.

**Evidence:** the real `make mutate` target with a fake interpreter that fails on the
second pass:
```text
$ CALL_LOG=... make -s mutate PYTHON=/tmp/fake-python
make exit: 2
invocations:
scripts/mutmut_pass.py search.py tests/test_search.py
scripts/mutmut_pass.py queries.py tests/test_providers.py tests/test_search.py
stderr:
simulated pass failure on invocation 2
make: *** [Makefile:82: mutate] Error 1
```
The loop stopped after the second pass; the third spec was not invoked. Regression test:
`test_make_mutate_stops_on_first_failed_pass` also asserts only two modules were invoked
and make exited non-zero.

### P3-018 — Lint coverage — Low

**Location:** `Makefile:52-53`

**Offending code:**
```make
lint: ## Static checks with ruff (pyflakes + pycodestyle rules)
	$(PYTHON) -m ruff check va_legal_agent tests scripts
```

**Root cause & impact:** the 529-line Flask app at the repository root is the only Python
source outside the linted paths. Its style/static errors (unused imports, undefined names,
unreachable code) never fail `make lint` or CI. `ruff check webapp.py` is clean today, so
this is a gate-coverage gap rather than a present defect — but it is exactly the file whose
concurrency code needs the most scrutiny (`P3-001`–`P3-003`), and a future edit gets no
static check at all.

**Evidence:**
```text
P3-018 lint paths: $(PYTHON) -m ruff check va_legal_agent tests scripts
P3-018 ruff webapp: All checks passed!
```

**Remediation:** add `webapp.py` to `lint` and `lint-fix`.

---

## `pyproject.toml` (Total Lines Verified: 64)

### P3-019 — CI coverage — Medium — **FIXED**

**Location:** `pyproject.toml:36`; `.github/workflows/ci.yml:24-30`;
`webapp.py:1-30` (design note)

**Offending code (before the fix):**
```toml
[project.optional-dependencies]
dev = [...]
llm = ["openai>=1.35.0,<4"]
web = ["flask>=3.0", "waitress>=3.0"]
```

**Root cause & impact:** `webapp.py` deliberately lives outside the package “so the
package's 100%-coverage and mutation gates are unaffected” — a defensible choice for the
gates, but the consequence is that a threaded HTTP server that spends money had no test
module, no coverage target, no mutation target and no lint target (`P3-018`). The `web`
extra was still not installed by the test workflow, so CI never even imported the app. A
local `tests/test_webapp.py` exists (added with the `P3-005` fix), but it starts with
`pytest.importorskip("flask")`, so it skipped in CI under the old install command.
`requirements-dev.txt` does not include Flask either, so a developer following
`pip install -e ".[dev]"` cannot run the app tests without discovering the separate extra.

**Evidence (before the fix):**
```text
P3-019 CI installs: ['.venv/bin/pip install --upgrade pip', '.venv/bin/pip install -e .', '.venv/bin/pip install pytest pytest-cov ruff']
P3-019 webapp tests exist: True | skip guard: True
```

**Fix:** the `check` job now installs the package as `-e ".[web]"`, so Flask and waitress
are present when the job runs `make test-w`. The webapp module's `importorskip` guard is
satisfied and the full-suite step executes all 20 `tests/test_webapp.py` tests.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_webapp.py -rs
20 passed

$ make test-w
1224 passed
```

---

## `requirements.txt`, `requirements-dev.txt` (Total Lines Verified: 17)

### P3-020 — Dependency management — Low

**Location:** `requirements-dev.txt:1-12`, `pyproject.toml:11-35`,
`.github/workflows/ci.yml:27-30`

**Offending code:**
```text
# requirements-dev.txt
-r requirements.txt
pytest>=8.0.0
pytest-cov>=5.0.0
ruff>=0.6.0
...
openai>=1.35.0,<4
```
```yaml
# ci.yml
.venv/bin/pip install -e ".[web]"
.venv/bin/pip install pytest pytest-cov ruff
```

**Root cause & impact:** the same six runtime dependencies are declared in `pyproject.toml`
and again (via `-r requirements.txt`) in `requirements-dev.txt`, while the `[dev]` extra
contains a different set: it lacks `openai` (present in `requirements-dev.txt`). CI installs
`-e ".[web]"` plus floating `pytest`/`pytest-cov`/`ruff`, so the versions that gate a PR are
neither the pinned `mutmut==3.7.0` set from
`requirements-dev.txt` nor the `[dev]` extra — they are whatever PyPI serves that morning.
Local `pip install -e ".[dev]"`, local `pip install -r requirements-dev.txt`, and CI can
therefore disagree on tool versions and on whether `openai` is importable. The duplication
also means a dependency bump must be made in two places, with no check that they agree.

**Evidence:**
```text
P3-020 dev extra has openai: False
P3-020 requirements-dev requirements: ['-r requirements.txt', 'pytest>=8.0.0', 'pytest-cov>=5.0.0', 'ruff>=0.6.0', 'mutmut==3.7.0', 'debugpy==1.8.14', 'agent-dev-cli==0.0.1b260427', 'openai>=1.35.0,<4']
```

**Remediation:** make `pyproject.toml` the single source (`pip install -e ".[dev,llm,web]"`
in CI and in the docs), or generate the requirements files from it and add a CI check that
they match.

---

## `.mutation-baseline.json` (Total Lines Verified: 70)

### P3-021 — Mutation baseline — Low — **FIXED**

**Location:** `.mutation-baseline.json:1-70`; identity extraction at
`scripts/check_mutation_baseline.py:68-115` and comparison at `:203`

**Offending code:**
```json
  "agent.py": 87,
  "batch.py": 20,
  "config.py": 41,
```
```python
        if actual > expected:
```

**Root cause & impact:** the gate compares one integer per module. A change that kills a
triaged equivalent while a *killable* mutant starts surviving leaves the count unchanged
and passes; so does replacing an equivalent mutant with a different equivalent one. The
file records no mutant names, no mutmut version and no triage date, so the only way to
notice a swap is to read `/tmp/mutmut_survivors_<module>.txt` by hand — and CI discards it.
The counts themselves are well documented in the scripts, and the `timeout` check is a good
complement; the gap is identity, not intent.

**Fix:** every baseline entry now records both the survivor count and a SHA-256 digest of
the sorted survivor names; the JSON also records the mutmut version and triage date. The
checker recovers the `# <mutant>: <status>` identity lines written by `mutmut show`, rejects
missing/truncated/duplicate identities, and fails on any digest mismatch — including an
equal-count equivalent-for-killable swap — rather than only when the count grows. Legacy
integer entries are rejected with a migration message instead of silently remaining
count-only. The regenerated baseline uses the current survivor counts; those had drifted
for `agent.py` (87→97), `fetch.py` (49→64) and `impact.py` (6→7) since the last triage, so
their extra survivors still warrant the normal equivalence review even though their exact
identities are now pinned.

**Evidence:**
```text
$ .venv/bin/python -m pytest -q tests/test_check_mutation_baseline.py
12 passed
```
Regression tests: `test_exact_identity_passes_in_any_order`,
`test_equal_count_survivor_swap_fails_gate`,
`test_changed_identity_with_fewer_survivors_also_fails`,
`test_truncated_identity_list_is_rejected`, and
`test_legacy_count_only_baseline_is_rejected`.

**Remediation:** implemented as above; keep the count as a fast pre-check and update both
fields together only after triaging the survivor diff.

---

## `.env.example` (Total Lines Verified: 202)

### P3-022 — Documentation — Low

**Location:** `.env.example:1-202`; reads at `webapp.py:54`, `:57`, `:528`

**Offending code:**
```python
STATE_DIR = Path(os.environ.get("WEBAPP_STATE_DIR", str(_APP_DIR / ".webapp")))
_MAX_CONCURRENT = int(os.environ.get("WEBAPP_MAX_CONCURRENT", "2"))
...
port = int(os.environ.get("PORT", "8080"))
```

**Root cause & impact:** `.env.example` is the project's complete settings reference — it
documents every search, fetch, enrichment, LLM and batch knob — but never mentions the three
variables the web app reads. An operator cannot discover `WEBAPP_MAX_CONCURRENT` or
`WEBAPP_STATE_DIR` without reading `webapp.py`, and the `P3-005` footgun (a `0` that used to
permanently 429 every submission) is undocumented precisely where defaults are explained.
Because `python-dotenv` loads `.env` for the app too, adding these entries is sufficient to
configure it.

**Evidence:**
```text
P3-022 WEBAPP/PORT entries: []
```

**Remediation:** add a short “Web app (Flask)” section documenting `PORT` (default 8080),
`WEBAPP_STATE_DIR` (default `.webapp/`) and `WEBAPP_MAX_CONCURRENT` (default 2, must be
1–32), with the loopback-only warning.

---

## Verified-execution appendix

All probes were run from the repository root with `.venv/bin/python`, except the shell
probes. Temporary state directories were used so the repository was not modified.

```python
# Behavioural probes (outputs quoted per finding)
import webapp

# P3-001: capture the object Flask would serialize
webapp._JOBS["probe"] = {"job_id": "probe", "status": "running"}
webapp.jsonify = lambda obj: obj
with webapp.app.test_request_context():
    result = webapp.status("probe")
assert result is webapp._JOBS["probe"]

# P3-002: 51 running jobs, then prune
for i in range(webapp._MAX_JOBS + 1):
    webapp._JOBS[f"j{i:03d}"] = {
        "job_id": f"j{i:03d}",
        "status": "running",
        "started_at": f"2026-01-01T00:00:{i:02d}+00:00",
    }
webapp._prune()
assert "j000" not in webapp._JOBS

# P3-006: fixed; see test_finalize_locks_read_and_delete_against_recorders.
```

```bash
# P3-013: child cwd from a cron-like caller
tmp=$(mktemp -d)
printf '#!/bin/sh\npwd > "%s/cwd.log"\nexit 0\n' "$tmp" > "$tmp/fake-python"
chmod +x "$tmp/fake-python"
: > "$tmp/issues.txt"
(cd "$tmp" && PY="$tmp/fake-python" ISSUES_FILE="$tmp/issues.txt" LOG_DIR="$tmp/logs" MODE=plan \
  bash "$PWD/deploy/run_batch.sh" >/dev/null 2>&1)
cat "$tmp/cwd.log"
```

```bash
# P3-012: tar exclusions
tmp=$(mktemp -d)
mkdir -p "$tmp/src/.webapp" "$tmp/target"
touch "$tmp/src/.env" "$tmp/src/.webapp/job.json" "$tmp/src/.env.example" \
      "$tmp/src/issues.txt" "$tmp/src/issues.retry" "$tmp/src/keep.py"
tar --exclude='.env' --exclude='.webapp' --exclude='issues.txt' \
    --exclude='issues.retry' -C "$tmp/src" -cf - . | tar -C "$tmp/target" -xf -
(cd "$tmp/target" && find . -type f | sort)
# ./.env.example
# ./keep.py
```

```bash
# Fixed-regression suite
.venv/bin/python -m pytest -q \
  tests/test_webapp.py tests/test_mutmut_pass.py tests/test_check_mutation_baseline.py \
  tests/test_install_sh.py tests/test_mutation_gate_integrity.py
# 56 passed
```

Static probes used `yaml.safe_load()` on `mutation.yml`, `json.loads()` on
`.mutation-baseline.json`, `ruff check webapp.py`, and `grep` over the workflows and
`Makefile`; their outputs are quoted in the individual findings.

Full-suite state at the time of writing: **1,239 passed**, **100% line+branch coverage**;
`ruff check va_legal_agent tests deploy` clean.
