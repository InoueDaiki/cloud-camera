#!/usr/bin/env bash
# 動画の退避 Lambda の zip を作る: <リポジトリ直下>/build/video_archiver.zip
# 中身: handler.py、archiver.py、common/(lambdas/common)、Pillow(arm64 / Python 3.13 用)、
#       最小構成の PyAV(lambdas/common/pyav_min。av/ と lib/。スプライト用のキーフレームのデコード)
# boto3 は Lambda のランタイムのものを使うので入れない。
# pyav_min は docker でビルドする(初回のみ数分。2 回目からは build/.cache/pyav_min のキャッシュを使う)。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/build/video_archiver.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mise exec -- uv pip install --quiet --target "$STAGE" \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all: \
  -r "$HERE/pyproject.toml"

bash "$ROOT/lambdas/common/pyav_min/build.sh" "$STAGE"

cp "$HERE/handler.py" "$HERE/archiver.py" "$STAGE/"
rsync -a --exclude tests --exclude .venv --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude pyproject.toml --exclude uv.lock --exclude README.md --exclude FRAMES.md --exclude pyav_min \
  "$ROOT/lambdas/common/" "$STAGE/common/"

rm -f "$STAGE/.lock"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
mkdir -p "$ROOT/build"
rm -f "$OUT"
(cd "$STAGE" && zip -qr9 "$OUT" .)
# 展開後のサイズ(Lambda の上限 250 MB = 262,144,000 バイト)。zip の中の各ファイルの大きさの合計で測る
TOTAL=$(python3 -c 'import sys, zipfile; print(sum(i.file_size for i in zipfile.ZipFile(sys.argv[1]).infolist()))' "$OUT")
echo "$OUT (zip $(du -h "$OUT" | cut -f1)、展開後 $((TOTAL / 1024 / 1024)) MB / 上限 250 MB)"
if [ "$TOTAL" -gt 262144000 ]; then
  echo "展開後のサイズが 250 MB を超えている" >&2
  exit 1
fi
