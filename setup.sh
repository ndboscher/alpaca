#!/usr/bin/env bash
# One-time setup for macOS / Linux. Run from the project folder: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3,10))'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and re-run." >&2
  exit 1
fi

echo "Using $($PY --version)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -e ".[dev]"

[ -f .env ] || { cp .env.example .env; echo "Created .env  -> add your Alpaca API keys"; }
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "Created config.yaml -> adjust strategy/risk settings"; }

cat <<MSG

Setup complete.
  1. Edit .env and paste your Alpaca PAPER API key and secret.
  2. ./run.sh check          # verify keys + options approval
  3. ./run.sh run --dry-run  # watch signals without placing orders
  4. ./run.sh run            # trade on your paper account
MSG
