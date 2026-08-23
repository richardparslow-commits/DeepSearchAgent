#!/usr/bin/env bash
# DeepSearchAgent unattended batch runner.
#
# Shared entry point used by the systemd service/timer and the cron line.
# Runs from the project root so the app's own dotenv loading picks up .env
# from the working directory (no shell-level sourcing needed).
#
# Behaviour:
#   MODE=auto (default)  -> --auto-run: executes what fits the current quota
#                           windows, waits for the CourtListener daily reset,
#                           and repeats until every issue has been run.
#   MODE=plan            -> --batch-dry-run only: writes the plan + retry file,
#                           no searches executed (safe to run anywhere, any time).
#
# Environment overrides (all optional):
#   ISSUES_FILE          path to the issues file (default: $ROOT/issues.txt)
#   LOG_DIR              directory for per-run logs (default: $ROOT/logs)
#   LOG_KEEP             how many recent logs to keep (default: 14)
#   MAX_BATCH_REQUESTS   cap total CourtListener requests per pass (optional)
#   PY                   python interpreter (default: $ROOT/.venv/bin/python)
#
# The run id defaults to batch-YYYYMMDD so all per-issue events in one daily
# pass correlate; set RUN_ID to override.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
ISSUES_FILE="${ISSUES_FILE:-$ROOT/issues.txt}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
LOG_KEEP="${LOG_KEEP:-14}"
MODE="${MODE:-auto}"
MAX_BATCH_REQUESTS="${MAX_BATCH_REQUESTS:-}"

# Correlate all invocations in one daily pass. The app reads RUN_ID from the
# environment for single-issue runs; auto-run emits its own summary lines, so
# this mainly labels the log and gives operators a stable handle per day.
export RUN_ID="${RUN_ID:-batch-$(date +%Y%m%d)}"

mkdir -p "$LOG_DIR"

if [[ ! -x "$PY" ]]; then
    echo "error: venv python not found at $PY (run deploy/install.sh first)" >&2
    exit 1
fi
if [[ ! -f "$ISSUES_FILE" ]]; then
    echo "error: issues file not found at $ISSUES_FILE" >&2
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
NOW="$(date +%Y-%m-%dT%H:%M:%S%z)"
LOGFILE="$LOG_DIR/batch-$STAMP.log"

args=()
if [[ "$MODE" == "plan" ]]; then
    args+=(--batch-dry-run)
else
    args+=(--auto-run)
fi
args+=(--issues-file "$ISSUES_FILE")
if [[ -n "$MAX_BATCH_REQUESTS" ]]; then
    args+=(--max-batch-requests "$MAX_BATCH_REQUESTS")
fi

{
    echo "=== DeepSearchAgent batch $NOW mode=$MODE run_id=$RUN_ID ==="
    echo "issues_file=$ISSUES_FILE log=$LOGFILE"
} >&2
{
    echo "=== DeepSearchAgent batch $NOW mode=$MODE run_id=$RUN_ID ==="
    echo "issues_file=$ISSUES_FILE"
} >>"$LOGFILE"

set +e
"$PY" -m va_legal_agent "${args[@]}" >>"$LOGFILE" 2>&1
status=$?
set -e

echo "batch finished with exit status $status (see $LOGFILE)" >&2
echo "batch finished with exit status $status" >>"$LOGFILE"

# Rotate: keep the newest LOG_KEEP logs, drop the rest.
if ls -1t "$LOG_DIR"/batch-*.log >/dev/null 2>&1; then
    ls -1t "$LOG_DIR"/batch-*.log | tail -n +$((LOG_KEEP + 1)) | xargs -r rm -f
fi

exit "$status"
