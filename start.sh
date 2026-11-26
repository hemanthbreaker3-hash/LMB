#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)"
cd "$APP_DIR"

# Ensure cbgram SQLite session files have a stable, writable location & Rust core is active.
export CBGRAM_SESSION_DIR="${CBGRAM_SESSION_DIR:-${WZGRAM_SESSION_DIR:-$APP_DIR/.sessions}}"
export CBGRAM_RUST_CORE="${CBGRAM_RUST_CORE:-1}"
export CBGRAM_UVLOOP="${CBGRAM_UVLOOP:-1}"
export CBGRAM_MAX_CONCURRENT="${CBGRAM_MAX_CONCURRENT:-512}"
export CBGRAM_WORKERS="${CBGRAM_WORKERS:-64}"
export BOT_THREAD_POOL_SIZE="${BOT_THREAD_POOL_SIZE:-256}"
mkdir -p "$CBGRAM_SESSION_DIR"
chmod 700 "$CBGRAM_SESSION_DIR" 2>/dev/null || true

# VPS installs use ./venv; the supplied container image installs into /wzvenv.
# Heroku sets DYNO and runs this process from the project root.
PYTHON=""
for candidate in \
  "$APP_DIR/venv/bin/python" \
  "$APP_DIR/.venv/bin/python" \
  "/wzvenv/bin/python"; do
  if [[ -x "$candidate" ]]; then
    PYTHON="$candidate"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  # Keep a missing VPS virtualenv from silently starting with system Python.
  if [[ -f "$APP_DIR/deploy.vps" && -z "${DYNO:-}" && -z "${PORT:-}" ]]; then
    echo "ERROR: venv/bin/python is missing. Run ./deploy.vps to prepare the VPS environment." >&2
    exit 1
  fi
  PYTHON="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "ERROR: No usable Python interpreter found. Check the Docker/Heroku build logs." >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
printf 'Starting Video Tool with Python: %s\n' "$PYTHON"
exec "$PYTHON" -u -m bot "$@"
