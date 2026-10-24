#!/usr/bin/env bash
# 動画の退避 Lambda の zip を作る: <リポジトリ直下>/build/video_archiver.zip
# 中身: handler.py、archiver.py、common/(lambdas/common)、Pillow(arm64 / Python 3.13 用)
# boto3 は Lambda のランタイムのものを使うので入れない。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/build/video_archiver.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mise exec -- uv pip install --quiet --target "$STAGE" \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all: \
  -r "$HERE/pyproject.toml"

cp "$HERE/handler.py" "$HERE/archiver.py" "$STAGE/"
rsync -a --exclude tests --exclude .venv --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude pyproject.toml --exclude uv.lock --exclude README.md \
  "$ROOT/lambdas/common/" "$STAGE/common/"

rm -f "$STAGE/.lock"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
mkdir -p "$ROOT/build"
rm -f "$OUT"
(cd "$STAGE" && zip -qr9 "$OUT" .)
echo "$OUT ($(du -h "$OUT" | cut -f1))"
