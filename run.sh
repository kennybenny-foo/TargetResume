#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/TargetResume"
APP_FILE="targetResume.py"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"

LOG_DIR="$HOME/TargetResume_logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/log.txt"

cd "$APP_DIR"

echo "==> Syncing code to origin/main..."
git fetch origin
git reset --hard origin/main
git clean -fd -e .venv -e TargetResume_logs -e log.txt

echo "==> DEPLOYED COMMIT:"
git log -1 --oneline

echo "==> Ensuring venv exists..."
if [ ! -d "$VENV" ]; then
  python -m venv "$VENV"
fi

echo "==> Installing deps..."
"$PY" -m pip install -U pip
if [ -f requirements.txt ]; then
  "$PY" -m pip install -r requirements.txt
fi

echo "==> Stopping old server..."
pkill -f "targetResume.py" || true

echo "==> Starting new server..."
nohup "$PY" "$APP_FILE" >> "$LOG" 2>&1 & disown || true

sleep 2
echo "==> Running processes:"
pgrep -af "targetResume.py" || (echo "FAILED to start"; tail -n 80 "$LOG"; exit 1)

echo "==> Last 40 log lines:"
tail -n 40 "$LOG" || true