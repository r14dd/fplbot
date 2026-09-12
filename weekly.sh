#!/usr/bin/env bash
# Weekly FPL refresh. Set FPL_ENTRY to your manager id to sync the real squad.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> refreshing data"
python3 run.py refresh

if [[ -n "${FPL_ENTRY:-}" ]]; then
  echo "==> syncing squad for entry ${FPL_ENTRY}"
  python3 run.py sync --entry "${FPL_ENTRY}" || echo "  ! sync failed, using saved squad"
fi

echo "==> building report"
python3 run.py week --horizon "${FPL_HORIZON:-5}"

latest=$(ls -t reports/*.md 2>/dev/null | head -1 || true)
[[ -n "$latest" ]] && echo "==> $latest"
