#!/usr/bin/env bash
# Run any zdte command inside the project's virtualenv, e.g. ./run.sh run --dry-run
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/zdte ]; then
  echo "Not set up yet - run ./setup.sh first." >&2
  exit 1
fi
exec .venv/bin/zdte "${@:-run}"
