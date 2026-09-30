#!/usr/bin/env bash
# Packages the Lambda deployment zip: handler + immune package (inference side only) + model.json.
# Usage: scripts/build_lambda.sh [path/to/model.json]
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${1:-model/model.json}"
BUILD=build
OUT="$BUILD/app.zip"

if [ ! -f "$MODEL" ]; then
  echo "error: model file not found at $MODEL - run 'python -m immune.train' first (or scripts/train_model.sh)" >&2
  exit 1
fi

rm -rf "$BUILD/pkg" && mkdir -p "$BUILD/pkg/immune"
cp lambda/handler.py "$BUILD/pkg/"
# Only the inference-side modules ship: train.py (needs numpy) and traffic.py/simulate.py
# (synthetic-data generation, only used for training/evaluation) never run inside Lambda.
for f in __init__.py features.py model.py store.py guard.py; do
  cp "immune/$f" "$BUILD/pkg/immune/$f"
done
find "$BUILD/pkg" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
cp "$MODEL" "$BUILD/pkg/model.json"

python3 -c "
import sys
sys.path.insert(0, '$BUILD/pkg')
from immune.model import Autoencoder
Autoencoder.load('$BUILD/pkg/model.json')
print('model.json passes its integrity check')
"

( cd "$BUILD/pkg" && rm -f "../../$OUT" && zip -qr "../../$OUT" . -x '*.pyc' )
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
