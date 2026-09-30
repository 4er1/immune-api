#!/usr/bin/env bash
# Trains the autoencoder on synthetic traffic. Needs numpy (pip install -e ".[train]").
# Usage: scripts/train_model.sh [extra python -m immune.train arguments]
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m immune.train --out model/model.json --sources 500 --duration 1800 \
  --hidden 3 --epochs 400 --seed "${IMMUNE_TRAIN_SEED:-42}" --percentile 98 --max-fpr 0.05 "$@"
