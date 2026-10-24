#!/usr/bin/env bash
# ベンチマーク用の zip(一時関数 cc-tmp-detect-bench 用)を作る。本番の build.sh に、
# 比較するモデル一式、ベンチマーク用フレーム(tools/make_bench_frames.py)、tools/bench.py を足したもの。
#   ARCH=arm64|x86_64 lambdas/detector/tools/build_bench.sh → build/cc-tmp-detect-bench-$ARCH.zip
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ARCH="${ARCH:-arm64}"
EXTRA="$ROOT/build/bench-extra"
rm -rf "$EXTRA"
mkdir -p "$EXTRA"
# FRAMES_DIR(動画から 1 秒ごとに切り出した連番 JPEG)があればそれを、なければ darknet の画像を使う
if [ -n "${FRAMES_DIR:-}" ]; then
  (cd "$HERE" && mise exec -- uv run python tools/make_bench_frames.py "$FRAMES_DIR" "$EXTRA/bench_frames" --sequence)
else
  (cd "$HERE" && mise exec -- uv run python tools/make_bench_frames.py models/calib "$EXTRA/bench_frames")
fi
cp "$HERE/tools/bench.py" "$EXTRA/"
ARCH="$ARCH" EXTRA_DIR="$EXTRA" OUT_ZIP="$ROOT/build/cc-tmp-detect-bench-$ARCH.zip" \
  MODELS="${MODELS:-yolox_nano.dyn yolox_nano.int8 yolox_tiny.dyn yolox_tiny.int8 yolox_tiny.int8s8 yolox_s.int8}" \
  "$HERE/build.sh"
