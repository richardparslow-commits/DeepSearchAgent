#!/usr/bin/env bash
# One-shot installer for the DeepSearchAgent unattended batch deployment.
#
#   sudo deploy/install.sh                     # install to /opt/va-legal-agent + systemd timer
#   sudo deploy/install.sh --cron              # install and print a cron line instead
#   deploy/install.sh --target "$HOME/vla"     # install to a user-owned dir (no root)
#
# Env overrides: TARGET, INSTALL_USER, CRON_MIN, CRON_HOUR.
#
# What it does:
#   1. Copies the project (code, deploy/, .env.example) into the target dir.
#   2. Creates a venv there and pip-installs the package (network required).
#   3. Creates .env from .env.example if missing (chmod 600) and an empty issues.txt.
#   4. Creates a dedicated system user (system targets only).
#   5. Installs the systemd unit + timer (or prints a cron line with --cron).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${TARGET:-/opt/va-legal-agent}"
USER_NAME="${INSTALL_USER:-vaagent}"
MODE_SYSTEMD=1
[[ "${1:-}" == "--cron" ]] && MODE_SYSTEMD=0

# System-level targets need root; user dirs can install without it.
IS_ROOT_TARGET=0
case "$TARGET" in
    /opt/*|/srv/*|/usr/local/*) IS_ROOT_TARGET=1 ;;
esac

if [[ "$IS_ROOT_TARGET" -eq 1 && "$(id -u)" -ne 0 ]]; then
    echo "error: $TARGET needs root. Re-run with sudo, or use --target for a user dir:" >&2
    echo "  $0 --target \"\$HOME/va-legal-agent\"" >&2
    exit 1
fi

echo "==> Installing DeepSearchAgent to $TARGET"
mkdir -p "$TARGET"

# 1. Copy the project, skipping local junk.
tar --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='logs' \
    --exclude='.bva_index' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.coverage' --exclude='mutants' --exclude='.freebuff' \
    --exclude='*.egg-info' -C "$ROOT" -cf - . | tar -C "$TARGET" -xf -

# 2. venv + package install.
if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
    echo "==> Creating venv and installing the package (needs PyPI access)"
    python3 -m venv "$TARGET/.venv"
    "$TARGET/.venv/bin/pip" install --upgrade pip
    "$TARGET/.venv/bin/pip" install -e "$TARGET"
fi

# 3. .env + issues.txt.
if [[ ! -f "$TARGET/.env" ]]; then
    cp "$TARGET/.env.example" "$TARGET/.env"
    echo "==> Created $TARGET/.env from .env.example — EDIT IT with your API keys." >&2
fi
chmod 600 "$TARGET/.env"

if [[ ! -f "$TARGET/issues.txt" ]]; then
    : > "$TARGET/issues.txt"
    echo "==> Created empty $TARGET/issues.txt — add one issue per line (optional TAB priority)." >&2
fi

# 4. Dedicated service user (system targets only).
if [[ "$IS_ROOT_TARGET" -eq 1 ]]; then
    if ! id -u "$USER_NAME" >/dev/null 2>&1; then
        useradd --system --home-dir "$TARGET" --shell /usr/sbin/nologin "$USER_NAME"
        echo "==> Created system user $USER_NAME."
    fi
    chown -R "$USER_NAME":"$USER_NAME" "$TARGET"
else
    USER_NAME="$(id -un)"
    echo "==> User-owned target; running as $USER_NAME (no system user created)."
fi

# 5. systemd units or cron line.
if [[ "$MODE_SYSTEMD" -eq 1 ]]; then
    if [[ "$IS_ROOT_TARGET" -ne 1 ]]; then
        echo "error: --target without a system dir cannot install systemd units; use --cron instead." >&2
        exit 1
    fi
    echo "==> Installing systemd units"
    sed "s|__TARGET__|$TARGET|g; s|__USER__|$USER_NAME|g" \
        "$ROOT/deploy/va-legal-agent-batch.service" > "/etc/systemd/system/va-legal-agent-batch.service"
    sed "s|__TARGET__|$TARGET|g; s|__USER__|$USER_NAME|g" \
        "$ROOT/deploy/va-legal-agent-batch.timer" > "/etc/systemd/system/va-legal-agent-batch.timer"
    systemctl daemon-reload
    systemctl enable --now va-legal-agent-batch.timer
    echo "==> Timer enabled: runs daily at 04:30 (+ random 10 min)."
    echo "    Test immediately with:  systemctl start va-legal-agent-batch.service"
    echo "    Watch it with:          journalctl -u va-legal-agent-batch.service -f"
else
    CRON_MIN="${CRON_MIN:-30}"
    CRON_HOUR="${CRON_HOUR:-4}"
    echo "==> Cron setup. Add this line to root's crontab (sudo crontab -e):"
    echo
    echo "  ${CRON_MIN} ${CRON_HOUR} * * * flock -n /var/lock/va-legal-agent-batch.lock runuser -u ${USER_NAME} -- ${TARGET}/deploy/run_batch.sh"
    echo
    echo "    flock prevents overlapping runs; runuser drops to the service user."
    echo "    Test immediately with:  ${TARGET}/deploy/run_batch.sh"
fi

# Sanity check: config resolves and the package imports.
"$TARGET/.venv/bin/python" -m va_legal_agent --show-config >/dev/null 2>&1 \
    && echo "==> Sanity check passed (config resolves)." \
    || echo "!! Sanity check failed — check $TARGET/.env and the venv." >&2

echo
echo "==> Next steps:"
echo "    1. Edit $TARGET/.env (providers, CourtListener/OpenAI keys)."
echo "    2. Fill $TARGET/issues.txt (one issue per line)."
echo "    3. Run $TARGET/deploy/run_batch.sh once manually to confirm."
echo "    4. Logs land in $TARGET/logs/ (newest 14 kept); journald captures stderr too."
