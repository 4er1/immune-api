#!/usr/bin/env bash
# Replays synthetic normal + attack traffic through the guard and prints per-attack-kind stats
# (blocked_share, cost_avoided_share, ...). Used by CI as a build gate - see check_thresholds.py.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
python3 -m immune.simulate --model "${1:-model/model.json}" --seed "${IMMUNE_EVAL_SEED:-123}" \
  --normal 300 --per-attack 20 | tee build/eval_summary.json
