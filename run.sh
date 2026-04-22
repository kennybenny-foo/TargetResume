#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/TargetResume"
APP_FILE="targetResume.py"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"

LOG_DIR="$HOME/TargetResume_logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/log.txt"

cd "$APP_DIR"

echo "==> Syncing code to $REMOTE/$BRANCH..."
git fetch "$REMOTE" "$BRANCH"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: Local changes detected. Commit or stash them before running this deploy script."
  exit 1
fi

LOCAL_COMMIT="$(git rev-parse HEAD)"
REMOTE_COMMIT="$(git rev-parse "$REMOTE/$BRANCH")"
BASE_COMMIT="$(git merge-base HEAD "$REMOTE/$BRANCH")"

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
  echo "==> Already up to date."
elif [ "$LOCAL_COMMIT" = "$BASE_COMMIT" ]; then
  git merge --ff-only "$REMOTE/$BRANCH"
else
  echo "ERROR: Branch has diverged from $REMOTE/$BRANCH. Pull or rebase manually before deploying."
  exit 1
fi

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