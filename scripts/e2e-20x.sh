#!/usr/bin/env bash
# M1 completion gate: the real dual-process E2E must pass 20 consecutive runs.
# Usage: scripts/e2e-20x.sh [runs]
set -uo pipefail
cd "$(dirname "$0")/.."

runs="${1:-20}"
failures=0

for i in $(seq 1 "$runs"); do
  if uv run pytest tests/e2e_local -q > "/tmp/autolab-e2e-run-$i.log" 2>&1; then
    summary=$(tail -1 "/tmp/autolab-e2e-run-$i.log")
    echo "run $i/$runs: PASS ($summary)"
  else
    failures=$((failures + 1))
    echo "run $i/$runs: FAIL — log: /tmp/autolab-e2e-run-$i.log"
    tail -20 "/tmp/autolab-e2e-run-$i.log"
  fi
done

echo "----"
if [ "$failures" -eq 0 ]; then
  echo "E2E GATE PASSED: $runs/$runs consecutive runs green"
  exit 0
fi
echo "E2E GATE FAILED: $failures/$runs runs failed"
exit 1
