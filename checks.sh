#!/usr/bin/env bash
# The gate: a commit only lands if this exits 0.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> build"
python3 -m compileall -q run.py fplbot

echo "==> scenarios"
python3 -B -m unittest discover -s scenarios
