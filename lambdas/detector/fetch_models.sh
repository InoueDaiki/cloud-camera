#!/usr/bin/env bash
# モデルを用意する(*.onnx は gitignore 済み。build.sh の前に 1 回実行する)。
#
# 1. YOLOX(Apache-2.0)の公式 ONNX を models/ に取得し、SHA-256 を確かめる
#    出典: Megvii-BaseDetection/YOLOX のリリース 0.1.1rc0
# 2. INT8 量子化のキャリブレーション画像(darknet の data/、パブリックドメイン)を models/calib/ に取得する
# 3. tools/prepare_model.py で入力可変版(*.dyn.onnx)と INT8 版(*.int8.onnx、*.int8s8.onnx)を作る
#
# 使い方: lambdas/detector/fetch_models.sh            # 既定は yolox_nano と yolox_tiny
#        MODELS="yolox_s" lambdas/detector/fetch_models.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/models/calib"
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

CALIB="https://raw.githubusercontent.com/pjreddie/darknet/master/data"
for f in person.jpg kite.jpg dog.jpg horses.jpg giraffe.jpg eagle.jpg; do
  [ -f "calib/$f" ] || curl -fsSL -o "calib/$f" "$CALIB/$f"
done

cd "$HERE"
for m in ${MODELS:-yolox_nano yolox_tiny}; do
  if [ ! -f "models/$m.int8s8.onnx" ]; then
    mise exec -- uv run python tools/prepare_model.py "models/$m.onnx" --calib-dir models/calib --s8s8 2>&1 | grep -v WARN
  fi
done
ls -la models/*.onnx
