#!/usr/bin/env bash
# YOLOX(Apache-2.0)の公式 ONNX を models/ に取得し、SHA-256 を確認する。
# 出典: Megvii-BaseDetection/YOLOX のリリース 0.1.1rc0
# 使い方: ./fetch_models.sh            # 既定は yolox_nano と yolox_tiny
#        MODELS="yolox_s" ./fetch_models.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/models"
cd "$HERE/models"
BASE="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
for m in ${MODELS:-yolox_nano yolox_tiny}; do
  if [ ! -f "$m.onnx" ]; then
    echo "取得: $m.onnx"
    curl -fsSL -o "$m.onnx.tmp" "$BASE/$m.onnx"
    mv "$m.onnx.tmp" "$m.onnx"
  fi
  grep " $m.onnx\$" "$HERE/models.sha256" | shasum -a 256 -c -
done
