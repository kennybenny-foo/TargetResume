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
git clean -fd -e .venv -e TargetResume_logs -e log.txt -e .env

echo "==> DEPLOYED COMMIT:"
git log -1 --oneline

echo "==> Ensuring venv exists..."
if [ ! -d "$VENV" ]; then
  python -m venv "$VENV"
fi

echo "==> Installing deps..."
"$PY" -m pip install -U pip
if [ -f requirement.txt ]; then
  "$PY" -m pip install -r requirement.txt
fi

echo "==> Loading environment variables..."
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
else
  echo "WARNING: .env file not found in $APP_DIR"
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: OPENAI_API_KEY is not set"
  exit 1
fi

if [ -z "${MONGODB_URI:-}" ]; then
  echo "ERROR: MONGODB_URI is not set"
  exit 1
fi

echo "==> Stopping old server..."
pkill -f "$APP_FILE" || true

echo "==> Starting new server..."
nohup env OPENAI_API_KEY="$OPENAI_API_KEY" MONGODB_URI="$MONGODB_URI" "$PY" "$APP_FILE" >> "$LOG" 2>&1 & disown

sleep 2
echo "==> Running processes:"
pgrep -af "$APP_FILE" || (echo "FAILED to start"; tail -n 80 "$LOG"; exit 1)

echo "==> Last 40 log lines:"
tail -n 40 "$LOG" || true